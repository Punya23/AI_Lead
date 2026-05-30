# Geta.ai — AI-Powered Lead Processing Pipeline

## Engineering Philosophy

This system is designed around three principles:

1. **Reliability over features** — Every operation is idempotent, every failure is retried, every lead has a complete audit trail. The system degrades gracefully, not catastrophically.

2. **Determinism where it matters** — The LLM extracts signals; Python code does the math. Same lead → same score → same queue. Always. This is testable, auditable, and debuggable.

3. **Observability by default** — Every request gets a correlation ID that flows from HTTP intake through Celery tasks to the final database write. At 3 AM, you need to trace one request, not grep by timestamp.

## Architecture

```
Input (REST/CSV/Webhook)
  → Validation (sync, in HTTP request — fast rejection)
  → Celery Queue (async via Redis)
  → AI Enrichment (Gemini 2.0 Flash — structured JSON output)
  → Deterministic Scoring (Python weighted math, 0-100)
  → Intelligent Routing (configurable threshold-based)
  → Full audit trail (per-stage execution logs with timing)
```

### Why This Architecture?

| Decision | Reasoning |
|----------|-----------|
| **Sync validation, async pipeline** | Validation is fast (~1ms). Rejecting bad leads immediately is better UX than "accepted, will reject later". LLM calls take 3-6 seconds — blocking the HTTP thread is unacceptable. |
| **Dual SQLAlchemy engines** | FastAPI needs async (asyncpg). Celery needs sync (psycopg2). Using `asyncio.run()` inside Celery creates event loop leaks. Two engines, one DB, shared models. |
| **Deterministic scoring (NOT LLM)** | LLMs are non-deterministic even at temperature 0. Production scoring must be testable: `assert score == 100`. The LLM extracts qualitative signals; Python does quantitative math. |
| **Content-based dedup** | `SHA-256(email + company + message)`. No timestamp window = no edge cases. The UNIQUE constraint is belt-and-suspenders with the application check. |
| **Fallback on LLM failure** | Instead of stopping, use conservative defaults (category=Unknown, urgency=Low) and flag for review. A low-scored lead in ARCHIVE is recoverable. A stuck lead in limbo is not. |

## Reliability Features

### Pipeline State Machine (Idempotent Resume)

```
RECEIVED → VALIDATED → ENRICHED → SCORED → ROUTED → COMPLETE
                                                      ↓
                                                    FAILED
                                                      ↓
                                              flag_for_review=true
                                              dead_lettered_at=now()
```

On Celery task retry, the pipeline checks the lead's **current status** and resumes from the last successful stage. This means:
- A lead enriched but not scored → only runs scoring + routing
- A lead already complete → returns immediately
- No stage ever executes twice

### Failure Recovery Matrix

| Failure | Detection | Recovery | Max Retries |
|---------|-----------|----------|-------------|
| LLM Timeout | `TimeoutError` | Exponential backoff (1s → 2s → 4s) | 3 |
| Malformed JSON | `JSONDecodeError` / Pydantic validation | Retry with corrective prompt | 3 |
| Rate Limit (429) | HTTP status or simulated | Backoff + jitter | 3 |
| DB Connection | `OperationalError` | Pool retry → Celery retry | 3 |
| Duplicate Lead | `payload_hash` UNIQUE check | Reject immediately | 0 |
| Worker crash | `acks_late` + `reject_on_worker_lost` | Task re-queued automatically | 3 |
| All retries exhausted | `MaxRetriesExceededError` | Dead-letter + flag for human review | — |

### Failure Simulation

The system includes a built-in failure simulator for demonstrating recovery:

```bash
# Enable in .env
SIMULATE_FAILURES=true
FAILURE_RATE_LLM_TIMEOUT=0.15     # 15% of enrichments timeout
FAILURE_RATE_MALFORMED_RESPONSE=0.10  # 10% return bad JSON
FAILURE_RATE_RATE_LIMIT=0.10       # 10% hit rate limits
```

This isn't just a toggle — each failure type has its own probability, and the simulator triggers the **exact same code paths** as real failures. The retry logic, fallback behavior, and dead-lettering all work identically.

## Observability

### Correlation ID Tracing

Every request gets a UUID correlation ID that appears in:
- HTTP response headers (`X-Correlation-ID`)
- All structured log lines
- Celery task context
- Execution log records

```bash
# Trace a request end-to-end
curl -v POST http://localhost:8000/api/v1/leads -d '...'
# Response header: X-Correlation-ID: abc-123-def
docker compose logs | grep "abc-123-def"
```

### Health Checks (Kubernetes-Pattern)

| Endpoint | Purpose | Checks |
|----------|---------|--------|
| `GET /health/live` | Liveness probe | Is the process alive? |
| `GET /health/ready` | Readiness probe | Can we accept traffic? (DB + Redis) |
| `GET /health` | Combined | DB + Redis + Worker + Queue depth |

The combined health check returns queue depth and worker count — an operator can see at a glance if leads are piling up because the worker is down.

### Execution Timeline

Every pipeline stage is logged with:
- Stage name and status (STARTED/SUCCESS/FAILED/RETRYING)
- Attempt number
- Duration in milliseconds
- Error message and traceback (on failure)

```bash
curl http://localhost:8000/api/v1/admin/logs/<lead-id>
```

## Quick Start

```bash
# 1. Clone and configure
git clone <repo-url> && cd geta-lead-pipeline
cp .env.example .env
# Edit .env — set GOOGLE_API_KEY

# 2. Start (one command)
docker compose up --build

# 3. Run migrations
docker compose exec api alembic upgrade head

# 4. Submit a lead
curl -X POST http://localhost:8000/api/v1/leads \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sarah Chen",
    "email": "sarah@techflow.io",
    "company": "TechFlow Solutions",
    "message": "We process 1000+ tickets daily and need AI automation.",
    "source": "website_form"
  }'

# 5. Check results (after ~5 seconds)
curl http://localhost:8000/api/v1/leads/<lead-id>
```

## API Reference

### Lead Intake
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/leads` | Submit single lead |
| `POST` | `/api/v1/leads/batch` | Upload CSV |
| `POST` | `/api/v1/webhooks/lead` | Webhook (202 Accepted) |

### Query & Admin
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/leads` | List leads (filterable) |
| `GET` | `/api/v1/leads/{id}` | Full detail with enrichment + score + routing |
| `GET` | `/api/v1/admin/queue-status` | Pipeline processing stats |
| `GET` | `/api/v1/admin/logs/{id}` | Execution timeline |
| `GET` | `/api/v1/admin/failures` | Failed & flagged leads |
| `GET` | `/api/v1/admin/stats/routing` | Routing distribution |

## Testing

```bash
# 72 tests covering validation, scoring, pipeline, and retry logic
pytest tests/ -v

# Test categories:
# - test_validation.py: email format, spam detection, dedup hashing, gibberish
# - test_scoring.py: intent signals, determinism, max score, qualification
# - test_pipeline.py: scoring+routing integration, threshold boundaries
# - test_retry.py: fallback enrichment, failure simulator, exception hierarchy
```

Key test that proves determinism:
```python
def test_same_input_same_score(self):
    """Run scoring 10 times — must produce identical results every time."""
    scores = set()
    for _ in range(10):
        result = score_lead(session, lead, enrichment)
        scores.add(result.lead_score)
    assert len(scores) == 1  # All 10 runs produced the same score
```

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Backend | FastAPI | Async-first, auto-generated docs, Pydantic validation |
| Database | PostgreSQL + SQLAlchemy 2.0 | JSONB for flexible schemas, ACID for state consistency |
| Task Queue | Redis + Celery | Reliable task delivery, `acks_late` for crash recovery |
| AI/LLM | Google Gemini 2.0 Flash | Structured JSON output, fast inference |
| Logging | loguru | Structured JSON logs, context binding for correlation IDs |
| Containers | Docker Compose | One-command reproducible environment |

## Project Structure

```
geta-lead-pipeline/
├── app/
│   ├── api/routes/          # HTTP handlers (leads, webhooks, admin, health)
│   ├── core/                # Config, database, logging, exceptions, middleware
│   ├── models/              # SQLAlchemy ORM (5 tables)
│   ├── schemas/             # Pydantic request/response schemas
│   ├── services/            # Business logic (validation, enrichment, scoring, routing)
│   └── tasks/               # Celery task definitions + retry policies
├── alembic/                 # Database migrations
├── tests/                   # 72 unit tests
├── scripts/                 # Demo data and automation scripts
├── docker/                  # Dockerfiles (API + Worker)
├── ARCHITECTURE.md          # Detailed design decisions with tradeoffs
├── DEBUGGING.md             # Operational runbook for diagnosing failures
└── docker-compose.yml       # One-command startup
```

## What I'd Add With More Time

1. **Circuit breaker on LLM calls** — Stop calling Gemini after N consecutive failures, cooldown period
2. **Prometheus metrics** — Request latency, queue depth, LLM call duration histograms
3. **Celery Flower** — Visual task monitoring dashboard
4. **Webhook delivery confirmation** — Verify downstream systems received routed leads
5. **Load testing with Locust** — Pipeline throughput benchmarking under sustained load
6. **Semantic dedup** — Embedding similarity instead of exact hash for near-duplicate detection
7. **Multi-provider LLM fallback** — If Gemini is down, fall back to OpenAI
8. **Blue/green deployment support** — Zero-downtime schema migrations
