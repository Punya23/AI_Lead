"""
Scoring service — Step 4 of the pipeline.

DETERMINISTIC scoring using Python code, NOT an LLM.
Same input always produces the same output.

Weighted signals (total: 0-100):
- Intent clarity:         0-25 points
- Urgency signal:         0-20 points
- Company completeness:   0-20 points
- Pain point specificity: 0-20 points
- Message quality:        0-15 points
"""

import re
import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.execution_log import ExecutionLog
from app.models.lead import Lead
from app.models.score import Score
from app.schemas.enrichment import EnrichmentResult
from app.schemas.score import ScoringResult


# =============================================================================
# Signal Scoring Functions — each returns (points, max_points)
# =============================================================================

INTENT_SCORES = {
    "Demo Request": 25,
    "Pricing": 20,
    "Partnership": 20,
    "Technical Inquiry": 15,
    "Unknown": 5,
    "Spam": 0,
}

URGENCY_SCORES = {
    "High": 20,
    "Medium": 12,
    "Low": 5,
}


def score_intent_clarity(enrichment: EnrichmentResult) -> int:
    """Score based on estimated intent from enrichment.

    Args:
        enrichment: AI enrichment result.

    Returns:
        int: Points awarded (0-25).
    """
    return INTENT_SCORES.get(enrichment.estimated_intent, 5)


def score_urgency_signal(enrichment: EnrichmentResult) -> int:
    """Score based on urgency level from enrichment.

    Args:
        enrichment: AI enrichment result.

    Returns:
        int: Points awarded (0-20).
    """
    return URGENCY_SCORES.get(enrichment.urgency_level, 5)


def score_company_completeness(lead: Lead) -> int:
    """Score based on completeness of company/lead information.

    Signals:
    - Has company name (5 points)
    - Message length > 50 chars (5 points)
    - Has source channel (5 points)
    - Email is corporate (not gmail/yahoo/hotmail) (5 points)

    Args:
        lead: Lead ORM instance.

    Returns:
        int: Points awarded (0-20).
    """
    points = 0

    # Has company name
    if lead.company and len(lead.company.strip()) > 1:
        points += 5

    # Message is substantial
    if lead.message and len(lead.message.strip()) > 50:
        points += 5

    # Has source
    if lead.source and lead.source.strip():
        points += 5

    # Corporate email (not free provider)
    free_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "protonmail.com"}
    email_domain = lead.email.lower().split("@")[-1]
    if email_domain not in free_domains:
        points += 5

    return points


def score_pain_point_specificity(enrichment: EnrichmentResult) -> int:
    """Score based on number and quality of identified pain points.

    3+ pain points = 20, 2 = 14, 1 = 7, 0 = 0

    Args:
        enrichment: AI enrichment result.

    Returns:
        int: Points awarded (0-20).
    """
    count = len(enrichment.pain_points)
    if count >= 3:
        return 20
    elif count == 2:
        return 14
    elif count == 1:
        return 7
    return 0


def score_message_quality(lead: Lead) -> int:
    """Score based on message quality heuristics.

    Signals:
    - Length (0-5): longer messages show more intent
    - Specificity (0-5): contains numbers, metrics, or technical terms
    - Actionability (0-5): contains action words

    Args:
        lead: Lead ORM instance.

    Returns:
        int: Points awarded (0-15).
    """
    points = 0
    message = lead.message.strip()

    # Length score
    if len(message) > 200:
        points += 5
    elif len(message) > 100:
        points += 3
    elif len(message) > 50:
        points += 1

    # Specificity — contains numbers or metrics
    if re.search(r'\d+', message):
        points += 3
    if any(word in message.lower() for word in ["api", "integration", "workflow", "automation", "pipeline", "production"]):
        points += 2

    # Actionability — contains action/decision words
    action_words = ["need", "looking for", "want to", "interested in", "evaluating", "planning", "budget", "timeline"]
    if any(word in message.lower() for word in action_words):
        points += 5
    elif any(word in message.lower() for word in ["help", "question", "wondering", "curious"]):
        points += 2

    return min(points, 15)  # Cap at 15


# =============================================================================
# Confidence Calculation
# =============================================================================

def calculate_confidence(enrichment: EnrichmentResult, total_score: int) -> float:
    """Calculate confidence score based on data completeness.

    Higher confidence when:
    - Enrichment fields are not "Unknown"
    - Pain points are identified
    - Score is not in ambiguous range (40-70)

    Args:
        enrichment: AI enrichment result.
        total_score: Calculated lead score (0-100).

    Returns:
        float: Confidence score (0.0-1.0).
    """
    confidence = 0.5  # Base confidence

    # Known category boosts confidence
    if enrichment.lead_category != "Unknown":
        confidence += 0.1
    if enrichment.estimated_intent != "Unknown":
        confidence += 0.15
    if enrichment.urgency_level != "Unknown":
        confidence += 0.05

    # Pain points boost confidence
    if len(enrichment.pain_points) >= 2:
        confidence += 0.1
    elif len(enrichment.pain_points) >= 1:
        confidence += 0.05

    # Extreme scores are more confident than middle scores
    if total_score >= 80 or total_score <= 20:
        confidence += 0.1
    elif total_score >= 70 or total_score <= 30:
        confidence += 0.05

    return min(round(confidence, 2), 1.0)


# =============================================================================
# Disqualification Flags
# =============================================================================

def get_disqualification_flags(enrichment: EnrichmentResult, lead: Lead) -> list[str]:
    """Identify factors that reduce lead quality.

    Args:
        enrichment: AI enrichment result.
        lead: Lead ORM instance.

    Returns:
        list[str]: List of disqualification reasons.
    """
    flags = []

    if enrichment.estimated_intent == "Spam":
        flags.append("Intent classified as Spam")
    if enrichment.estimated_intent == "Unknown":
        flags.append("Intent could not be determined")
    if enrichment.lead_category == "Unknown":
        flags.append("Company category unknown")
    if len(enrichment.pain_points) == 0:
        flags.append("No specific pain points identified")
    if len(lead.message.strip()) < 30:
        flags.append("Message too brief for reliable analysis")

    free_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}
    if lead.email.lower().split("@")[-1] in free_domains:
        flags.append("Personal email used (not corporate)")

    return flags


# =============================================================================
# Qualification Reason (Template-Generated — NOT LLM)
# =============================================================================

SIGNAL_DESCRIPTIONS = {
    "intent_clarity": "intent clarity",
    "urgency_signal": "urgency signal",
    "company_completeness": "company completeness",
    "pain_point_specificity": "pain point specificity",
    "message_quality": "message quality",
}

SIGNAL_MAX_POINTS = {
    "intent_clarity": 25,
    "urgency_signal": 20,
    "company_completeness": 20,
    "pain_point_specificity": 20,
    "message_quality": 15,
}


def generate_qualification_reason(
    breakdown: dict,
    enrichment: EnrichmentResult,
    total_score: int,
) -> str:
    """Generate a human-readable qualification reason from the scoring breakdown.

    Template-based, NOT LLM-generated. Fully deterministic.

    Args:
        breakdown: Points per signal.
        enrichment: AI enrichment result.
        total_score: Total score (0-100).

    Returns:
        str: 2-sentence qualification explanation.
    """
    # Find top 2 scoring signals
    sorted_signals = sorted(breakdown.items(), key=lambda x: x[1], reverse=True)
    top_signals = sorted_signals[:2]

    # Build first sentence: overall assessment
    if total_score >= 70:
        level = "High-intent"
    elif total_score >= 40:
        level = "Medium-intent"
    else:
        level = "Low-intent"

    first_sentence = (
        f"{level} {enrichment.estimated_intent.lower()} lead from "
        f"{enrichment.company_type} company ({enrichment.lead_category})."
    )

    # Build second sentence: top signal explanations
    signal_parts = []
    for signal_name, points in top_signals:
        max_pts = SIGNAL_MAX_POINTS.get(signal_name, 0)
        desc = SIGNAL_DESCRIPTIONS.get(signal_name, signal_name)
        signal_parts.append(f"{desc} ({points}/{max_pts})")

    second_sentence = f"Strongest signals: {' and '.join(signal_parts)}."

    return f"{first_sentence} {second_sentence}"


# =============================================================================
# Main Scoring Function
# =============================================================================

def score_lead(session: Session, lead: Lead, enrichment: EnrichmentResult) -> ScoringResult:
    """Calculate deterministic score for a lead using weighted signals.

    Same input ALWAYS produces the same output. No randomness, no LLM.

    Args:
        session: Sync SQLAlchemy session (Celery worker).
        lead: Lead ORM instance.
        enrichment: Validated enrichment result.

    Returns:
        ScoringResult: Complete scoring output with breakdown.

    Side effects:
        - Creates Score record in DB
        - Creates ExecutionLog record in DB
        - Updates lead status to SCORED
    """
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, stage="scoring")
    start_time = time.time()

    # Log execution start
    exec_log = ExecutionLog(
        lead_id=lead.id,
        stage="scoring",
        status="STARTED",
        attempt_number=1,
    )
    session.add(exec_log)
    session.flush()

    try:
        # Calculate each signal
        breakdown = {
            "intent_clarity": score_intent_clarity(enrichment),
            "urgency_signal": score_urgency_signal(enrichment),
            "company_completeness": score_company_completeness(lead),
            "pain_point_specificity": score_pain_point_specificity(enrichment),
            "message_quality": score_message_quality(lead),
        }

        total_score = sum(breakdown.values())
        confidence = calculate_confidence(enrichment, total_score)
        disqualification_flags = get_disqualification_flags(enrichment, lead)
        qualification_reason = generate_qualification_reason(breakdown, enrichment, total_score)

        scoring_result = ScoringResult(
            lead_score=total_score,
            confidence_score=confidence,
            qualification_reason=qualification_reason,
            disqualification_flags=disqualification_flags,
            scoring_breakdown=breakdown,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        # Persist score
        score_record = Score(
            lead_id=lead.id,
            lead_score=scoring_result.lead_score,
            confidence_score=scoring_result.confidence_score,
            qualification_reason=scoring_result.qualification_reason,
            disqualification_flags=scoring_result.disqualification_flags,
            scoring_breakdown=scoring_result.scoring_breakdown,
        )
        session.add(score_record)

        # Update execution log
        exec_log.status = "SUCCESS"
        exec_log.duration_ms = duration_ms

        # Update lead status
        lead.status = "SCORED"
        lead.updated_at = datetime.now(timezone.utc)

        session.flush()

        log.info(
            "Scoring completed",
            score=total_score,
            confidence=confidence,
            duration_ms=duration_ms,
            breakdown=breakdown,
        )

        return scoring_result

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        log.error("Scoring failed", error=str(e), error_type=type(e).__name__)

        exec_log.status = "FAILED"
        exec_log.duration_ms = duration_ms
        exec_log.error_message = str(e)
        session.flush()

        raise
