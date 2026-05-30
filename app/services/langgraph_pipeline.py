"""
LangGraph pipeline — graph-based orchestration for lead processing.

Why LangGraph over manual state machine:
    The manual approach uses if/else chains to check lead status and
    decide which stage to run next. This works, but it's:
    - Hard to visualize the flow
    - Easy to introduce bugs when adding new stages
    - Not declaratively expressing the workflow intent

    LangGraph makes the pipeline a FIRST-CLASS GRAPH where:
    - Each stage is a node
    - Conditional edges express the idempotent resume logic
    - The graph is self-documenting and visualizable
    - Adding a new stage is: add_node() + add_edge()

Architecture:
    Celery handles: distribution, retry, acks_late, dead-lettering
    LangGraph handles: orchestration logic, state transitions, conditional routing

    These are two separate concerns. Celery is the reliable execution engine.
    LangGraph is the workflow definition.
"""

from typing import TypedDict, Any

from langgraph.graph import StateGraph, END
from loguru import logger

from app.models.lead import Lead
from app.schemas.enrichment import EnrichmentResult


# --- Pipeline State ---

class PipelineState(TypedDict):
    """State passed between LangGraph nodes.

    This is the "context object" that flows through the graph.
    Each node reads what it needs and writes its output.
    """
    lead_id: str
    session: Any              # SQLAlchemy sync session (not serializable, graph-local)
    lead: Any                 # Lead ORM object
    current_status: str       # Current lead status for idempotent routing
    enrichment_result: EnrichmentResult | None
    scoring_result: Any       # ScoringResult from score_lead()
    queue: str | None         # Final routing destination
    error: str | None         # Error message if any stage fails


# --- Stage Order ---

STAGE_ORDER = ["RECEIVED", "VALIDATED", "ENRICHED", "SCORED", "ROUTED", "COMPLETE"]


def _stage_index(status: str) -> int:
    """Get the index of a status in the pipeline order."""
    try:
        return STAGE_ORDER.index(status)
    except ValueError:
        return -1


# --- Graph Nodes ---
# Each node calls the existing service functions (NO code duplication).
# Nodes receive state, do work, and return updated state.

def enrich_node(state: PipelineState) -> PipelineState:
    """Enrichment node — calls Gemini for AI enrichment."""
    from app.services.enrichment import enrich_lead
    from app.api.routes.stream import publish_pipeline_event

    log = logger.bind(lead_id=state["lead_id"], node="enrich")
    log.info("LangGraph: executing enrichment node")

    enrichment_result = enrich_lead(state["session"], state["lead"])
    state["session"].commit()

    publish_pipeline_event(state["lead_id"], "enrichment", "SUCCESS", {
        "category": enrichment_result.lead_category,
        "intent": enrichment_result.estimated_intent,
        "urgency": enrichment_result.urgency_level,
    })

    state["enrichment_result"] = enrichment_result
    state["current_status"] = "ENRICHED"

    # Store embedding for semantic dedup (non-blocking)
    try:
        from app.services.vector_store import add_lead_embedding
        add_lead_embedding(state["lead_id"], state["lead"].message)
    except Exception as e:
        log.debug(f"Embedding storage skipped: {e}")

    return state


def load_existing_enrichment(state: PipelineState) -> PipelineState:
    """Load enrichment from DB when stage was already completed."""
    from sqlalchemy import select
    from app.models.enrichment import Enrichment as EnrichmentModel

    log = logger.bind(lead_id=state["lead_id"], node="load_enrichment")
    log.info("LangGraph: loading existing enrichment")

    existing = state["session"].execute(
        select(EnrichmentModel).where(EnrichmentModel.lead_id == state["lead"].id)
    )
    enrichment_model = existing.scalar_one_or_none()

    if enrichment_model:
        state["enrichment_result"] = EnrichmentResult(
            lead_category=enrichment_model.lead_category,
            company_type=enrichment_model.company_type,
            estimated_intent=enrichment_model.estimated_intent,
            urgency_level=enrichment_model.urgency_level,
            pain_points=enrichment_model.pain_points,
            ai_summary=enrichment_model.ai_summary,
        )
    else:
        raise ValueError("Enrichment record missing for ENRICHED lead")

    return state


def score_node(state: PipelineState) -> PipelineState:
    """Scoring node — deterministic Python scoring."""
    from app.services.scoring import score_lead
    from app.api.routes.stream import publish_pipeline_event

    log = logger.bind(lead_id=state["lead_id"], node="score")
    log.info("LangGraph: executing scoring node")

    scoring_result = score_lead(state["session"], state["lead"], state["enrichment_result"])
    state["session"].commit()

    publish_pipeline_event(state["lead_id"], "scoring", "SUCCESS", {
        "score": scoring_result.lead_score,
        "confidence": scoring_result.confidence_score,
    })

    state["scoring_result"] = scoring_result
    state["current_status"] = "SCORED"
    return state


def route_node(state: PipelineState) -> PipelineState:
    """Routing node — threshold-based queue assignment."""
    from app.services.routing import route_lead
    from app.api.routes.stream import publish_pipeline_event
    from sqlalchemy import select
    from app.models.score import Score as ScoreModel

    log = logger.bind(lead_id=state["lead_id"], node="route")
    log.info("LangGraph: executing routing node")

    # Get score
    if state["scoring_result"]:
        lead_score = state["scoring_result"].lead_score
    else:
        existing_score = state["session"].execute(
            select(ScoreModel).where(ScoreModel.lead_id == state["lead"].id)
        )
        score_model = existing_score.scalar_one_or_none()
        lead_score = score_model.lead_score if score_model else 0

    queue = route_lead(state["session"], state["lead"], lead_score)
    state["session"].commit()

    publish_pipeline_event(state["lead_id"], "routing", "SUCCESS", {
        "queue": queue,
        "score": lead_score,
    })

    state["queue"] = queue
    state["current_status"] = "COMPLETE"
    return state


# --- Conditional Edge Functions ---
# These implement the idempotent resume logic as graph routing.

def should_enrich_or_skip(state: PipelineState) -> str:
    """Decide whether to run enrichment or skip (already done)."""
    if _stage_index(state["current_status"]) < _stage_index("ENRICHED"):
        return "enrich"
    return "load_enrichment"


def should_score_or_skip(state: PipelineState) -> str:
    """Decide whether to run scoring or skip (already done)."""
    if _stage_index(state["current_status"]) < _stage_index("SCORED"):
        return "score"
    return "route_or_end"


def should_route_or_end(state: PipelineState) -> str:
    """Decide whether to run routing or end (already done)."""
    if _stage_index(state["current_status"]) < _stage_index("ROUTED"):
        return "route"
    return END


# --- Graph Builder ---

def build_pipeline_graph() -> Any:
    """Build and compile the LangGraph pipeline.

    Returns a compiled graph that can be invoked with:
        result = graph.invoke(initial_state)

    Graph structure:
        START → should_enrich_or_skip?
                ├─ enrich → should_score_or_skip?
                └─ load_enrichment → should_score_or_skip?
                                      ├─ score → should_route_or_end?
                                      └─ route_or_end (skip)
                                                   ├─ route → END
                                                   └─ END (skip)
    """
    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("enrich", enrich_node)
    graph.add_node("load_enrichment", load_existing_enrichment)
    graph.add_node("score", score_node)
    graph.add_node("route", route_node)

    # Entry point: decide whether to enrich
    graph.set_conditional_entry_point(
        should_enrich_or_skip,
        {"enrich": "enrich", "load_enrichment": "load_enrichment"},
    )

    # After enrichment (either fresh or loaded): decide whether to score
    graph.add_conditional_edges(
        "enrich",
        should_score_or_skip,
        {"score": "score", "route_or_end": "route"},
    )
    graph.add_conditional_edges(
        "load_enrichment",
        should_score_or_skip,
        {"score": "score", "route_or_end": "route"},
    )

    # After scoring: decide whether to route
    graph.add_conditional_edges(
        "score",
        should_route_or_end,
        {"route": "route", END: END},
    )

    # After routing: done
    graph.add_edge("route", END)

    return graph.compile()
