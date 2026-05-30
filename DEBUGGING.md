# Debugging & Operational Runbook

This document is for the engineer who gets paged at 3 AM because leads stopped processing.

## Quick Diagnosis Flowchart

```
Lead stuck? → Check health endpoint
  ├─ DB down?     → Check postgres container logs
  ├─ Redis down?  → Check redis container, memory usage
  ├─ Worker down? → Check worker logs, restart worker
  └─ All healthy? → Check the specific lead's execution log
```

## Common Failure Scenarios

### 1. "Leads are accepted but never processed"

**Symptom**: API returns `QUEUED` but status never changes to `COMPLETE`.

**Diagnosis**:
```bash
# Check if worker is running
docker compose logs worker --tail=20

# Check if the task is registered
docker compose exec worker celery -A app.tasks.celery_app inspect registered

# Check queue depth (tasks waiting)
docker compose exec redis redis-cli LLEN leads

# Check if worker is consuming from the right queue
docker compose exec worker celery -A app.tasks.celery_app inspect active_queues
```

**Root causes**:
- Worker not consuming from `leads` queue → verify `-Q default,leads` in worker CMD
- Task not registered → check `autodiscover_tasks` and explicit import in `celery_app.py`
- Worker crashed and didn't restart → check `docker compose ps`

### 2. "Lead is stuck at ENRICHED but never scored"

**Symptom**: Lead status is `ENRICHED` — enrichment succeeded but scoring/routing didn't run.

**Diagnosis**:
```bash
# Get the lead's execution timeline
curl http://localhost:8000/api/v1/admin/logs/<lead-id> | python3 -m json.tool

# Check if the worker crashed mid-pipeline
docker compose logs worker | grep <lead-id>
```

**Recovery**: The pipeline is **idempotent** — it checks the current status and resumes from the last successful stage. Simply re-queue the lead:

```bash
# Re-trigger processing (the pipeline will skip enrichment and start at scoring)
docker compose exec worker python3 -c "
from app.tasks.lead_pipeline import process_lead
process_lead.delay('<lead-id>')
"
```

### 3. "LLM enrichment keeps failing"

**Symptom**: Lead status is `VALIDATED`, execution logs show `enrichment: FAILED` with retries.

**Diagnosis**:
```bash
# Check all failed leads
curl http://localhost:8000/api/v1/admin/failures | python3 -m json.tool

# Check specific lead's error trail
curl http://localhost:8000/api/v1/admin/logs/<lead-id> | python3 -m json.tool
```

**Common LLM failure causes**:
| Error | Cause | Fix |
|-------|-------|-----|
| `LLMTimeoutError` | Gemini API slow/down | Will retry 3x with exponential backoff (1s → 2s → 4s) |
| `LLMMalformedResponseError` | JSON parse failed | Retries with corrective prompt suffix |
| `LLMRateLimitError` | Too many API calls | Backoff + jitter, consider adding a rate limiter |
| `GOOGLE_API_KEY not set` | Missing env var | Set `GOOGLE_API_KEY` in `.env` |

**After max retries**: The lead gets conservative fallback defaults (category=Unknown, urgency=Low), scores low, routes to ARCHIVE, and is flagged with `flag_for_review=true`.

### 4. "Duplicate leads are getting 500 errors"

**Symptom**: Submitting the same lead twice causes an Internal Server Error.

**Diagnosis**: Check if the `payload_hash` UNIQUE constraint is being violated:
```bash
docker compose logs api | grep "UniqueViolationError"
```

**How dedup works**:
1. `SHA-256(email.lower() + company.lower() + message)` → 64-char hash
2. Before INSERT, we query for existing hash in DB
3. If found → reject immediately with reference to original lead ID
4. UNIQUE constraint on `payload_hash` is the safety net (belt + suspenders)

### 5. "Health check returns 503"

**Symptom**: `/health` returns `{"status": "degraded"}`.

**Diagnosis**: Check which dependency is down:
```bash
curl http://localhost:8000/health | python3 -m json.tool
# Look at checks.database, checks.redis, checks.celery_worker
```

**Note**: The API will still accept leads even if the worker is down. They queue in Redis and process when the worker recovers. This is by design — the API's job is intake, not processing.

---

## Tracing a Request End-to-End

Every request gets a `correlation_id` (UUID) that flows through:

```
Client Request
  → X-Correlation-ID header (or auto-generated)
  → All log lines include correlation_id
  → Celery task receives correlation_id
  → Execution logs record it
  → Response includes X-Correlation-ID header
```

**To trace a specific request**:
```bash
# 1. Get the correlation ID from the response header
curl -v POST http://localhost:8000/api/v1/leads -d '...'
# Look for: X-Correlation-ID: <uuid>

# 2. Search all logs for that ID
docker compose logs | grep "<correlation-id>"

# 3. Check execution timeline
curl http://localhost:8000/api/v1/admin/logs/<lead-id>
```

---

## Key Metrics to Monitor

| Metric | Where | Alert Threshold |
|--------|-------|-----------------|
| Queue depth | `GET /health` → `queue_depth.total_pending` | > 100 |
| Worker count | `GET /health` → `celery_worker.workers_online` | < 1 |
| Failed leads | `GET /api/v1/admin/failures` | Any new entries |
| DB latency | `GET /health` → `database.latency_ms` | > 100ms |
| Redis latency | `GET /health` → `redis.latency_ms` | > 50ms |
| Flagged for review | `GET /api/v1/admin/queue-status` → `flagged_for_review` | Any new entries |

---

## Database Queries for Debugging

```sql
-- All leads stuck in processing (not COMPLETE/FAILED/REJECTED)
SELECT id, email, status, created_at, updated_at
FROM leads
WHERE status NOT IN ('COMPLETE', 'FAILED', 'REJECTED')
ORDER BY created_at;

-- Leads that took too long to process
SELECT l.id, l.email, l.status,
       EXTRACT(EPOCH FROM (l.updated_at - l.created_at)) as processing_seconds
FROM leads l
WHERE l.status = 'COMPLETE'
ORDER BY processing_seconds DESC
LIMIT 10;

-- Dead-lettered leads (hit max retries)
SELECT id, email, failure_reason, flag_reason, dead_lettered_at
FROM leads
WHERE dead_lettered_at IS NOT NULL;

-- Enrichment failures by type
SELECT el.error_message, COUNT(*) as count
FROM execution_logs el
WHERE el.stage = 'enrichment' AND el.status = 'FAILED'
GROUP BY el.error_message
ORDER BY count DESC;
```

---

## Environment Variables Reference

| Variable | Default | What breaks if wrong |
|----------|---------|---------------------|
| `DATABASE_URL_ASYNC` | `postgresql+asyncpg://...` | API can't serve any route that touches DB |
| `DATABASE_URL_SYNC` | `postgresql+psycopg2://...` | Worker can't process any lead |
| `REDIS_URL` | `redis://redis:6379/0` | Celery can't dispatch tasks |
| `GOOGLE_API_KEY` | (none) | All enrichments fail → fallback defaults → low scores |
| `SIMULATE_FAILURES` | `false` | If `true`, random failures are injected (for demos only!) |
