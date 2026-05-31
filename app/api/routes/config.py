import json
import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter(prefix="/config", tags=["config"])

class SimulationRequest(BaseModel):
    simulate_failures: bool

@router.post("/simulate-failures")
async def set_simulate_failures(payload: SimulationRequest):
    """Toggle LLM failure simulation dynamically using Redis."""
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await r.set("SIMULATE_FAILURES", "true" if payload.simulate_failures else "false")
        return {"status": "success", "simulate_failures": payload.simulate_failures}
    finally:
        await r.aclose()
