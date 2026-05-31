"""
Webhook endpoint — accepts any JSON payload and queues immediately.

POST /api/v1/webhooks/lead — Returns 202 Accepted, processes async.
Never blocks on LLM calls. Returns immediately.
"""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.lead import Lead
from app.services.validation import generate_payload_hash, validate_lead
from app.tasks.lead_pipeline import process_lead
from app.core.rate_limiter import limiter

router = APIRouter(prefix="/api/v1", tags=["Webhooks"])


@router.post("/webhooks/lead", status_code=202)
@limiter.limit("60/minute")
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_async_session),
):
    """Receive a lead via webhook.

    Accepts any JSON payload, extracts lead fields, validates,
    and queues for async processing. Returns 202 immediately —
    never blocks on LLM or heavy processing.

    Args:
        request: Raw HTTP request with JSON body.
        db: Async database session.

    Returns:
        dict: Confirmation with lead_id and received status.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"received": False, "error": "Invalid JSON payload"}

    # Extract fields from webhook payload (flexible key mapping)
    name = (
        payload.get("name")
        or payload.get("full_name")
        or payload.get("contact_name")
        or "Unknown"
    )
    email = (
        payload.get("email")
        or payload.get("email_address")
        or payload.get("contact_email")
        or ""
    )
    company = (
        payload.get("company")
        or payload.get("company_name")
        or payload.get("organization")
        or "Unknown"
    )
    message = (
        payload.get("message")
        or payload.get("body")
        or payload.get("description")
        or payload.get("inquiry")
        or ""
    )
    source = payload.get("source", "webhook")

    # Validate
    is_valid, failure_reason, payload_hash = await validate_lead(
        name=name, email=email, company=company, message=message, db=db, source=source
    )

    if not is_valid:
        # For duplicates, don't store — original already exists
        if failure_reason and "DUPLICATE_LEAD" in failure_reason:
            logger.warning("Webhook lead rejected (duplicate)", email=email, reason=failure_reason)
            return {
                "received": True,
                "status": "REJECTED",
                "reason": failure_reason,
            }

        # For non-duplicate rejections, store with unique hash suffix
        import uuid as _uuid
        reject_hash = (payload_hash or generate_payload_hash(
            email or "unknown", company or "unknown", message or "",
            name=name or "", source=source or ""
        )) + f"_rejected_{_uuid.uuid4().hex[:8]}"

        rejected_lead = Lead(
            raw_payload=payload,
            email=email or "unknown@invalid.com",
            name=name,
            company=company,
            message=message or "",
            source=source,
            payload_hash=reject_hash,
            status="REJECTED",
            failure_reason=failure_reason,
        )
        db.add(rejected_lead)
        await db.flush()

        logger.warning("Webhook lead rejected", email=email, reason=failure_reason)

        return {
            "received": True,
            "lead_id": str(rejected_lead.id),
            "status": "REJECTED",
            "reason": failure_reason,
        }

    # Create lead
    lead = Lead(
        raw_payload=payload,
        email=email,
        name=name,
        company=company,
        message=message,
        source=source,
        payload_hash=payload_hash,
        status="VALIDATED",
    )
    db.add(lead)
    await db.flush()

    lead_id = str(lead.id)
    logger.info("Webhook lead received and queued", lead_id=lead_id, email=email)

    # Queue for async processing
    process_lead.delay(lead_id)

    return {
        "received": True,
        "lead_id": lead_id,
        "status": "QUEUED",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
