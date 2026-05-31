"""
Lead API routes — intake endpoints for single leads and CSV batch upload.

POST /api/v1/leads       — Submit a single lead (JSON)
POST /api/v1/leads/batch — Upload CSV file with multiple leads
GET  /api/v1/leads       — List leads with filters
GET  /api/v1/leads/{id}  — Get full lead detail
"""

import csv
import io
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.lead import Lead
from app.schemas.lead import (
    BatchRejection,
    LeadBatchResponse,
    LeadCreateRequest,
    LeadDetailResponse,
    LeadListResponse,
    LeadResponse,
)
from app.services.validation import generate_payload_hash, validate_lead
from app.tasks.lead_pipeline import process_lead
from app.core.rate_limiter import limiter

router = APIRouter(prefix="/api/v1", tags=["Leads"])


@router.post("/leads", response_model=LeadResponse, status_code=201)
@limiter.limit("60/minute")
async def create_lead(
    request: Request,
    lead_request: LeadCreateRequest,
    db: AsyncSession = Depends(get_async_session),
):
    """Submit a single lead for processing.

    Validates synchronously, then queues for async AI enrichment + scoring.
    No LLM calls happen in this handler — all AI work is background.

    Args:
        request: Lead data (name, email, company, message, source).
        db: Async database session.

    Returns:
        LeadResponse: Confirmation with lead_id and queued status.
    """
    # Validate synchronously
    is_valid, failure_reason, payload_hash = await validate_lead(
        name=lead_request.name,
        email=lead_request.email,
        company=lead_request.company,
        message=lead_request.message,
        db=db,
        source=lead_request.source or "",
    )

    if not is_valid:
        # For duplicates, don't store — the original lead is already in the DB
        if failure_reason and "DUPLICATE_LEAD" in failure_reason:
            logger.warning(
                "Lead rejected (duplicate)",
                email=lead_request.email,
                reason=failure_reason,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "status": "REJECTED",
                    "reason": failure_reason,
                },
            )

        # For non-duplicate rejections, store with a unique hash suffix
        import uuid as _uuid
        reject_hash = (payload_hash or generate_payload_hash(
            lead_request.email, lead_request.company, lead_request.message,
            name=lead_request.name, source=lead_request.source or ""
        )) + f"_rejected_{_uuid.uuid4().hex[:8]}"

        rejected_lead = Lead(
            raw_payload=lead_request.model_dump(),
            email=lead_request.email,
            name=lead_request.name,
            company=lead_request.company,
            message=lead_request.message,
            source=lead_request.source,
            payload_hash=reject_hash,
            status="REJECTED",
            failure_reason=failure_reason,
        )
        db.add(rejected_lead)
        await db.flush()

        logger.warning(
            "Lead rejected",
            lead_id=str(rejected_lead.id),
            email=lead_request.email,
            reason=failure_reason,
        )

        raise HTTPException(
            status_code=422,
            detail={
                "lead_id": str(rejected_lead.id),
                "status": "REJECTED",
                "reason": failure_reason,
            },
        )

    # Create valid lead
    lead = Lead(
        raw_payload=lead_request.model_dump(),
        email=lead_request.email,
        name=lead_request.name,
        company=lead_request.company,
        message=lead_request.message,
        source=lead_request.source,
        payload_hash=payload_hash,
        status="VALIDATED",
    )
    db.add(lead)
    await db.flush()

    lead_id = str(lead.id)
    logger.info("Lead created and validated", lead_id=lead_id, email=lead_request.email)

    # Queue for async processing (no LLM in request handler)
    process_lead.delay(lead_id)

    return LeadResponse(
        lead_id=lead.id,
        status="QUEUED",
        message="Lead accepted and queued for AI enrichment and scoring",
        queued_at=datetime.now(timezone.utc),
    )


@router.post("/leads/batch", response_model=LeadBatchResponse)
@limiter.limit("10/minute")
async def create_leads_batch(
    request: Request,
    file: UploadFile = File(..., description="CSV file with columns: name, email, company, message, source"),
    db: AsyncSession = Depends(get_async_session),
):
    """Upload a CSV file with multiple leads.

    Each row is validated individually. Valid leads are queued for
    async processing. Returns a summary of queued vs rejected leads.

    Expected CSV columns: name, email, company, message, source (optional)

    Args:
        file: CSV file upload.
        db: Async database session.

    Returns:
        LeadBatchResponse: Summary with total, queued count, and rejections.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))

    queued = 0
    rejections: list[BatchRejection] = []

    for row_num, row in enumerate(reader, start=2):  # Start at 2 (header is row 1)
        name = row.get("name", "").strip()
        email = row.get("email", "").strip()
        company = row.get("company", "").strip()
        message = row.get("message", "").strip()
        source = row.get("source", "csv_upload").strip() or "csv_upload"

        # Validate
        is_valid, failure_reason, payload_hash = await validate_lead(
            name=name, email=email, company=company, message=message, db=db,
        )

        if not is_valid:
            rejections.append(BatchRejection(row=row_num, email=email or None, reason=failure_reason))

            # Store rejected lead
            try:
                import uuid as _uuid
                rejected_lead = Lead(
                    raw_payload=dict(row),
                    email=email or "unknown@invalid.com",
                    name=name or "Unknown",
                    company=company or "Unknown",
                    message=message or "",
                    source=source,
                    payload_hash=(payload_hash or generate_payload_hash(
                        email or "unknown", company or "unknown", message or "",
                        name=name or "", source=source or ""
                    )) + f"_rejected_{_uuid.uuid4().hex[:8]}",
                    status="REJECTED",
                    failure_reason=failure_reason,
                )
                db.add(rejected_lead)
                await db.flush()
            except Exception as e:
                logger.warning(f"Failed to store rejected lead from CSV row {row_num}: {e}")
            continue

        # Create valid lead
        lead = Lead(
            raw_payload=dict(row),
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

        # Queue for processing
        process_lead.delay(str(lead.id))
        queued += 1

    total = queued + len(rejections)
    logger.info(f"Batch upload completed: {queued}/{total} queued, {len(rejections)} rejected")

    return LeadBatchResponse(total=total, queued=queued, rejected=rejections)


@router.get("/leads", response_model=LeadListResponse)
async def list_leads(
    status: str | None = Query(None, description="Filter by status (e.g., COMPLETE, FAILED, REJECTED)"),
    flag_for_review: bool | None = Query(None, description="Filter flagged leads"),
    limit: int = Query(20, ge=1, le=100, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    db: AsyncSession = Depends(get_async_session),
):
    """List leads with optional filters and pagination.

    Args:
        status: Filter by pipeline status.
        flag_for_review: Filter by review flag.
        limit: Results per page (max 100).
        offset: Pagination offset.
        db: Async database session.

    Returns:
        LeadListResponse: Paginated list of leads.
    """
    query = select(Lead)

    if status:
        if status.lower() == "qualified":
            # "qualified" is a special pseudo-status mapping to SALES_QUEUE
            from app.models.routing import RoutingDecision
            query = query.join(RoutingDecision).where(RoutingDecision.queue == "SALES_QUEUE")
        else:
            query = query.where(Lead.status == status.upper())
            
    if flag_for_review is not None:
        query = query.where(Lead.flag_for_review == flag_for_review)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    leads = result.scalars().all()

    return LeadListResponse(
        leads=[
            {
                "id": lead.id,
                "email": lead.email,
                "name": lead.name,
                "company": lead.company,
                "status": lead.status,
                "flag_for_review": lead.flag_for_review,
                "created_at": lead.created_at,
            }
            for lead in leads
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/leads/{lead_id}", response_model=LeadDetailResponse)
async def get_lead_detail(
    lead_id: UUID,
    db: AsyncSession = Depends(get_async_session),
):
    """Get full lead detail including enrichment, score, routing, and timeline.

    Args:
        lead_id: UUID of the lead.
        db: Async database session.

    Returns:
        LeadDetailResponse: Complete lead record with all pipeline data.
    """
    result = await db.execute(
        select(Lead).where(Lead.id == lead_id)
    )
    lead = result.scalar_one_or_none()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    return LeadDetailResponse.model_validate(lead)
