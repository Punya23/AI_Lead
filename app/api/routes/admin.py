"""
Admin API routes — observability, monitoring, and queue status.

GET /api/v1/admin/queue-status      — Active, queued, failed counts
GET /api/v1/admin/logs/{lead_id}    — Execution timeline for a lead
GET /api/v1/admin/failures          — All failed/flagged leads
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.models.execution_log import ExecutionLog
from app.models.lead import Lead
from app.schemas.lead import ExecutionLogEntry

router = APIRouter(prefix="/api/v1/admin", tags=["Admin & Observability"])


@router.get("/queue-status")
async def get_queue_status(
    db: AsyncSession = Depends(get_async_session),
):
    """Get pipeline processing statistics.

    Returns:
        dict: Counts of leads by status, plus totals.
    """
    # Count leads by status
    result = await db.execute(
        select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
    )
    status_counts = dict(result.all())

    # Count flagged leads
    flagged_result = await db.execute(
        select(func.count(Lead.id)).where(Lead.flag_for_review == True)  # noqa: E712
    )
    flagged_count = flagged_result.scalar()

    total = sum(status_counts.values())
    active = status_counts.get("VALIDATED", 0) + status_counts.get("ENRICHED", 0) + status_counts.get("SCORED", 0)

    return {
        "total_processed": total,
        "active": active,
        "queued": status_counts.get("VALIDATED", 0),
        "enriched": status_counts.get("ENRICHED", 0),
        "scored": status_counts.get("SCORED", 0),
        "completed": status_counts.get("COMPLETE", 0),
        "failed": status_counts.get("FAILED", 0),
        "rejected": status_counts.get("REJECTED", 0),
        "flagged_for_review": flagged_count,
        "status_breakdown": status_counts,
    }


@router.get("/logs/{lead_id}", response_model=list[ExecutionLogEntry])
async def get_lead_execution_logs(
    lead_id: UUID,
    db: AsyncSession = Depends(get_async_session),
):
    """Get the full execution timeline for a specific lead.

    Returns all execution log entries in chronological order.
    This is the primary debugging tool for investigating pipeline issues.

    Args:
        lead_id: UUID of the lead.
        db: Async database session.

    Returns:
        list[ExecutionLogEntry]: Chronological execution log.
    """
    result = await db.execute(
        select(ExecutionLog)
        .where(ExecutionLog.lead_id == lead_id)
        .order_by(ExecutionLog.created_at.asc())
    )
    logs = result.scalars().all()

    return [ExecutionLogEntry.model_validate(log) for log in logs]


@router.get("/failures")
async def get_failures(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_session),
):
    """Get all failed and flagged-for-review leads.

    Returns leads that need attention — either failed after max retries
    or had enrichment fallback applied.

    Args:
        limit: Results per page.
        offset: Pagination offset.
        db: Async database session.

    Returns:
        dict: Paginated list of failed/flagged leads.
    """
    query = (
        select(Lead)
        .where(
            (Lead.status == "FAILED") | (Lead.flag_for_review == True)  # noqa: E712
        )
        .order_by(Lead.created_at.desc())
    )

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Paginate
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    leads = result.scalars().all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "failures": [
            {
                "id": str(lead.id),
                "email": lead.email,
                "company": lead.company,
                "status": lead.status,
                "failure_reason": lead.failure_reason,
                "flag_for_review": lead.flag_for_review,
                "flag_reason": lead.flag_reason,
                "dead_lettered_at": lead.dead_lettered_at.isoformat() if lead.dead_lettered_at else None,
                "created_at": lead.created_at.isoformat(),
            }
            for lead in leads
        ],
    }


@router.get("/stats/routing")
async def get_routing_stats(
    db: AsyncSession = Depends(get_async_session),
):
    """Get routing distribution statistics.

    Returns:
        dict: Count of leads per routing queue.
    """
    from app.models.routing import RoutingDecision

    result = await db.execute(
        select(RoutingDecision.queue, func.count(RoutingDecision.id))
        .group_by(RoutingDecision.queue)
    )
    distribution = dict(result.all())

    total_routed = sum(distribution.values())

    return {
        "total_routed": total_routed,
        "distribution": distribution,
        "percentages": {
            queue: round(count / total_routed * 100, 1) if total_routed > 0 else 0
            for queue, count in distribution.items()
        },
    }


@router.get("/analytics")
async def get_analytics(
    db: AsyncSession = Depends(get_async_session),
):
    """Get high-level business analytics (useful for dashboards).

    Returns:
        dict: Aggregated metrics including total, qualified, and rejected counts.
    """
    from app.models.routing import RoutingDecision

    # Total leads
    total_result = await db.execute(select(func.count(Lead.id)))
    total_leads = total_result.scalar()

    # Rejected leads (validation failures + duplicates)
    rejected_result = await db.execute(
        select(func.count(Lead.id)).where(Lead.status == "REJECTED")
    )
    rejected_leads = rejected_result.scalar()

    # Routing stats
    routing_result = await db.execute(
        select(RoutingDecision.queue, func.count(RoutingDecision.id))
        .group_by(RoutingDecision.queue)
    )
    routing_counts = dict(routing_result.all())

    sales_queue = routing_counts.get("SALES_QUEUE", 0)
    nurture_queue = routing_counts.get("NURTURE_QUEUE", 0)
    archive = routing_counts.get("ARCHIVE", 0)

    return {
        "total_leads": total_leads,
        "qualified": sales_queue,  # For recruiters/dashboard: qualified means sent to sales
        "rejected": rejected_leads,
        "sales_queue": sales_queue,
        "nurture_queue": nurture_queue,
        "archived": archive,
    }
