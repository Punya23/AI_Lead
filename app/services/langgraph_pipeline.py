"""
LangGraph pipeline — Multi-Agent orchestration for lead processing.

Architecture:
    Celery handles: distribution, retry, acks_late, dead-lettering
    LangGraph handles: orchestration logic, state transitions, conditional routing

    These are two separate concerns. Celery is the reliable execution engine.
    LangGraph is the workflow definition.
"""

from typing import TypedDict, Any, Optional

from langgraph.graph import StateGraph, END
from loguru import logger

from app.models.lead import Lead
from app.schemas.enrichment import (
    EnrichmentResult,
    IntentUrgencyResult,
    CompanyContextResult,
    CategorizationResult,
)


# --- Pipeline State ---

class PipelineState(TypedDict):
    """State passed between LangGraph nodes.

    This is the "context object" that flows through the graph.
    Each node reads what it needs and writes its output.
    """
    # Input fields
    lead_id: str
    session: Any              # SQLAlchemy sync session
    lead: Any                 # Lead ORM object
    message: str
    domain: str
    checkpoint: dict          # Lightweight resume checkpoints from DB
    current_status: str       # Current lead status

    # Agent outputs (accumulated sequentially)
    intent_result: Optional[IntentUrgencyResult]
    research_result: Optional[CompanyContextResult]
    categorization_result: Optional[CategorizationResult]
    
    # Combined DB persistence object (created after categorization)
    enrichment_result: Optional[EnrichmentResult]

    # Downstream outputs
    scoring_result: Any       # ScoringResult from score_lead()
    queue: Optional[str]      # Final routing destination
    
    # Error tracking
    error: Optional[str]
    retry_count: int


# --- Stage Order ---

STAGE_ORDER = ["RECEIVED", "VALIDATED", "ENRICHED", "SCORED", "ROUTED", "COMPLETE"]


def _stage_index(status: str) -> int:
    """Get the index of a status in the pipeline order."""
    try:
        return STAGE_ORDER.index(status)
    except ValueError:
        return -1


# --- Graph Nodes ---

def enrichment_agent_node(state: PipelineState) -> PipelineState:
    from app.services.enrichment import run_enrichment_agent
    try:
        result = run_enrichment_agent(state["session"], state["lead"], state["message"])
        state["intent_result"] = result
        state["error"] = None
    except Exception as e:
        state["error"] = f"enrichment_agent_failed: {str(e)}"
    return state


def research_agent_node(state: PipelineState) -> PipelineState:
    from app.services.enrichment import run_research_agent
    try:
        result = run_research_agent(state["session"], state["lead"], state["domain"], state["message"])
        state["research_result"] = result
        state["error"] = None
    except Exception as e:
        state["error"] = f"research_agent_failed: {str(e)}"
    return state


def categorization_agent_node(state: PipelineState) -> PipelineState:
    from app.services.enrichment import run_categorization_agent
    from app.api.routes.stream import publish_pipeline_event
    from app.services.vector_store import add_lead_embedding
    try:
        result = run_categorization_agent(
            state["session"], 
            state["lead"], 
            state["intent_result"], 
            state["research_result"], 
            state["message"]
        )
        state["enrichment_result"] = result
        state["current_status"] = "ENRICHED"
        state["error"] = None
        
        publish_pipeline_event(state["lead_id"], "enrichment", "SUCCESS", {
            "category": result.lead_category,
            "intent": result.estimated_intent,
            "urgency": result.urgency_level,
        })
        
        # Store embedding for semantic dedup (non-blocking)
        try:
            add_lead_embedding(state["lead_id"], state["message"])
        except Exception as e:
            logger.debug(f"Embedding storage skipped: {e}")
            
    except Exception as e:
        state["error"] = f"categorization_agent_failed: {str(e)}"
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
        state["error"] = "Enrichment record missing for ENRICHED lead"
        
    return state


def scoring_agent_node(state: PipelineState) -> PipelineState:
    """Scoring node — deterministic Python scoring."""
    from app.services.scoring import score_lead
    from app.api.routes.stream import publish_pipeline_event

    try:
        scoring_result = score_lead(state["session"], state["lead"], state["enrichment_result"])
        state["session"].commit()

        publish_pipeline_event(state["lead_id"], "scoring", "SUCCESS", {
            "score": scoring_result.lead_score,
            "confidence": scoring_result.confidence_score,
        })

        state["scoring_result"] = scoring_result
        state["current_status"] = "SCORED"
        state["error"] = None
    except Exception as e:
        state["error"] = f"scoring_agent_failed: {str(e)}"
        
    return state


def routing_agent_node(state: PipelineState) -> PipelineState:
    """Routing node — threshold-based queue assignment."""
    from app.services.routing import route_lead
    from app.api.routes.stream import publish_pipeline_event
    from sqlalchemy import select
    from app.models.score import Score as ScoreModel

    try:
        if state.get("scoring_result"):
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
        state["error"] = None
    except Exception as e:
        state["error"] = f"routing_agent_failed: {str(e)}"
        
    return state


def error_node(state: PipelineState) -> PipelineState:
    """Handles node-level failures by propagating to Celery or falling back."""
    log = logger.bind(lead_id=state["lead_id"], node="error_node")
    
    # Check if this is an LLM enrichment error on the final retry attempt
    is_llm_error = state.get("error") and any(
        x in state["error"] for x in ["enrichment_agent", "research_agent", "categorization_agent"]
    )
    
    if is_llm_error and state.get("retry_count", 0) >= 2:
        log.warning(f"Max retries reached for LLM. Using deterministic fallback. Error was: {state['error']}")
        from app.services.llm_client import get_fallback_enrichment
        state["enrichment_result"] = get_fallback_enrichment()
        state["current_status"] = "ENRICHED"
        state["error"] = None
        return state
        
    log.error(f"LangGraph Error Node triggered: {state['error']}")
    # We raise to Celery. Celery will retry or dead-letter it.
    raise Exception(state["error"])


def check_error_fallback(state: PipelineState) -> str:
    """If error was cleared by fallback, continue to scoring."""
    if not state.get("error"):
        return "continue"
    return END


# --- Conditional Edge Functions ---

def check_agent_error(state: PipelineState) -> str:
    """Route to error_node if error is set."""
    return "error_handler" if state.get("error") else "continue"


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
    """Build and compile the LangGraph pipeline."""
    graph = StateGraph(PipelineState)

    # Add nodes
    graph.add_node("enrichment_agent", enrichment_agent_node)
    graph.add_node("research_agent", research_agent_node)
    graph.add_node("categorization_agent", categorization_agent_node)
    
    graph.add_node("load_enrichment", load_existing_enrichment)
    graph.add_node("scoring_agent", scoring_agent_node)
    graph.add_node("routing_agent", routing_agent_node)
    graph.add_node("error_node", error_node)

    # Entry point
    graph.set_conditional_entry_point(
        should_enrich_or_skip,
        {"enrich": "enrichment_agent", "load_enrichment": "load_enrichment"},
    )

    # Sequential Agents with Error Routing
    graph.add_conditional_edges("enrichment_agent", check_agent_error, {"continue": "research_agent", "error_handler": "error_node"})
    graph.add_conditional_edges("research_agent", check_agent_error, {"continue": "categorization_agent", "error_handler": "error_node"})
    graph.add_conditional_edges("categorization_agent", check_agent_error, {"continue": "scoring_agent", "error_handler": "error_node"})
    
    # Load Existing (if skipped)
    graph.add_conditional_edges("load_enrichment", check_agent_error, {"continue": "scoring_agent", "error_handler": "error_node"})

    # Error node fallback routing
    graph.add_conditional_edges("error_node", check_error_fallback, {"continue": "scoring_agent", END: END})

    # Scoring & Routing with Error Routing
    graph.add_conditional_edges("scoring_agent", check_agent_error, {"continue": "routing_agent", "error_handler": "error_node"})
    graph.add_conditional_edges("routing_agent", check_agent_error, {"continue": END, "error_handler": "error_node"})

    return graph.compile()
