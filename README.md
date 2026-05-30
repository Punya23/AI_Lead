# Geta.ai — AI-Powered Lead Processing Pipeline

> **Zero-config startup**: `docker compose up --build` — works with or without a Gemini API key.

## Engineering Philosophy

1. **Reliability over features** — Every operation is idempotent, every failure is retried, every lead has a complete audit trail. The system degrades gracefully, not catastrophically.

2. **Determinism where it matters** — The LLM extracts signals; Python code does the math. Same lead → same score → same queue. Always. This is testable, auditable, and debuggable.

3. **Observability by default** — Every request gets a correlation ID that flows from HTTP intake through Celery tasks to the final database write.

## Architecture

```
Input (REST/CSV/Webhook)
  → Validation (sync, in HTTP request — fast rejection)
  → Celery Queue (async via Redis)
  → LangGraph Pipeline (declarative state machine)
    → AI Enrichment (Gemini 2.0 Flash or mock fallback)
    → Semantic Dedup (ChromaDB vector similarity)
    → Deterministic Scoring (Python weighted math, 0-100)
    → Intelligent Routing (configurable threshold-based)
  → Notifications (Slack/Discord webhooks)
  → SSE Stream (real-time pipeline events)
  → Full audit trail (per-stage execution logs with timing)
```

### Key Design Decisions

| Decision | Reasoning |
|----------|-----------|
| **Sync validation, async pipeline** | Validation is fast (~1ms). Rejecting bad leads immediately is better UX than "accepted, will reject later". LLM calls take 3-6 seconds — blocking the HTTP thread is unacceptable. |
| **Dual SQLAlchemy engines** | FastAPI needs async (asyncpg). Celery needs sync (psycopg2). Using `asyncio.run()` inside Celery creates event loop leaks. Two engines, one DB, shared models. |
| **Deterministic scoring (NOT LLM)** | LLMs are non-deterministic even at temperature 0. Production scoring must be testable: `assert score == 100`. The LLM extracts qualitative signals; Python does quantitative math. |
| **LangGraph orchestration** | Celery handles task dispatch + retry; LangGraph handles pipeline logic as a declarative state graph. Each node is idempotent — skips if the lead already passed that stage. |
| **Mock enrichment fallback** | If `GOOGLE_API_KEY` is missing, the pipeline uses a deterministic keyword-based enrichment engine (urgency detection, pain point extraction, intent classification). **The full pipeline works without any API key.** |
| **Content-based dedup** | `SHA-256(email + company + message)`. No timestamp window = no edge cases. The UNIQUE constraint is belt-and-suspenders with the application check. |

## Mock Mode (No API Key Required)

When `GOOGLE_API_KEY` is missing or set to the placeholder value, the system automatically switches to **mock enrichment mode**:

```json
// GET /health
{
  "status": "healthy",
  "enrichment_mode": "mock (no GOOGLE_API_KEY)",
  ...
}
```

Mock enrichment is **not a placeholder** — it's a rule-based analysis engine that:
- Detects urgency from time-related keywords (ASAP, deadline, 30 days, etc.)
- Extracts pain points from cost/problem language ($2M/year, manual process, etc.)
- Classifies intent from action keywords (demo, pricing, pilot, deploy)
- Categorizes the company from domain and message context

The full pipeline (validation → enrichment → scoring → routing → notifications) works identically in both modes.

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
| No API key | `_is_api_key_configured()` | Auto-switch to mock enrichment | — |

### Failure Simulation

```bash
# Enable in .env
SIMULATE_FAILURES=true
FAILURE_RATE_LLM_TIMEOUT=0.15     # 15% of enrichments timeout
FAILURE_RATE_MALFORMED_RESPONSE=0.10  # 10% return bad JSON
FAILURE_RATE_RATE_LIMIT=0.10       # 10% hit rate limits
```

Each failure type triggers the **exact same code paths** as real failures.

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
| `GET /health` | Combined | DB + Redis + Worker + Queue depth + Enrichment mode |

### SSE Streaming (Real-Time Pipeline Events)

```bash
curl -N http://localhost:8000/api/v1/stream/pipeline
# Streams pipeline events as they happen:
# event: pipeline_event
# data: {"lead_id": "abc", "stage": "enrichment", "status": "SUCCESS", ...}
```

### Execution Timeline

Every pipeline stage is logged with:
- Stage name and status (STARTED/SUCCESS/FAILED/RETRYING)
- Attempt number and duration in milliseconds
- Error message and traceback (on failure)

```bash
curl http://localhost:8000/api/v1/admin/logs/<lead-id>
```

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/Punya23/AI_Lead.git && cd geta-lead-pipeline
cp .env.example .env
# Optional: set GOOGLE_API_KEY for real AI enrichment
# (Works without it — uses mock enrichment)

# 2. Start (one command)
docker compose up --build

# 3. Submit a lead
curl -X POST http://localhost:8000/api/v1/leads \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sarah Chen",
    "email": "sarah@techflow.io",
    "company": "TechFlow Solutions",
    "message": "We process 1000+ tickets daily and need AI automation. Budget approved for Q3.",
    "source": "website_form"
  }'

# 4. Check results (after ~5 seconds)
curl http://localhost:8000/api/v1/leads/<lead-id>

# 5. Check system health
curl http://localhost:8000/health

# 6. Watch pipeline events in real-time
curl -N http://localhost:8000/api/v1/stream/pipeline
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
| `GET` | `/api/v1/leads` | List leads (filterable by status) |
| `GET` | `/api/v1/leads/{id}` | Full detail: enrichment + score + routing + logs |
| `GET` | `/api/v1/admin/queue-status` | Pipeline processing stats |
| `GET` | `/api/v1/admin/logs/{id}` | Execution timeline |
| `GET` | `/api/v1/admin/failures` | Failed & flagged leads |
| `GET` | `/api/v1/admin/stats/routing` | Routing distribution |
| `GET` | `/api/v1/stream/pipeline` | SSE real-time events |

### Interactive Docs
Swagger UI at `http://localhost:8000/docs`

## Testing

**102 tests** — 72 unit tests + 30 live integration tests.

```bash
# Run unit tests only (no Docker needed)
pytest tests/test_validation.py tests/test_scoring.py tests/test_pipeline.py tests/test_retry.py -v

# Run integration tests (requires Docker containers running)
pytest tests/test_integration.py -v

# Run everything
pytest tests/ -v
```

### Test Coverage

| File | Tests | Type | What It Tests |
|------|-------|------|---------------|
| `test_validation.py` | 17 | Unit | Email format, required fields, disposable domains, spam, gibberish, hashing |
| `test_scoring.py` | 16 | Unit | Intent clarity, urgency, pain points, company, message quality, determinism |
| `test_pipeline.py` | 12 | Unit | Scoring→routing integration, threshold boundaries, custom config |
| `test_retry.py` | 15 | Unit | Fallback enrichment, failure simulation, exception hierarchy, dedup |
| `test_integration.py` | 30 | **Integration** | **Live Docker API + real Gemini LLM — full pipeline end-to-end** |

### Integration Tests (Real-World Scenarios)

The integration tests hit the **full running system** — no mocks:

- **High-intent B2B leads** (enterprise, startup, healthcare) → verify pipeline completes
- **Medium/low-intent leads** → verify scoring range makes sense
- **Spam/gibberish/disposable emails** → verify instant rejection (never enters queue)
- **Exact duplicate detection** → verify hash-based dedup
- **Webhook flexibility** → verify non-standard field names are handled
- **CSV batch upload** → verify 3-lead file is processed
- **Admin endpoints** → verify queue status, failures, routing stats
- **Pipeline audit trail** → verify enrichment + score + routing + execution logs all exist
- **Scoring determinism** → verify same input produces valid score range
- **Rate limiting** → verify 429 behavior
- **SSE streaming** → verify event stream and heartbeat

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
| Workflow | LangGraph | Declarative state machine for pipeline orchestration |
| Vector DB | ChromaDB | Semantic near-duplicate detection via embeddings |
| Rate Limiting | slowapi + Redis | Per-endpoint rate limiting |
| Notifications | Slack/Discord webhooks | Non-blocking via httpx |
| Streaming | SSE (Server-Sent Events) | Real-time pipeline events via Redis Pub/Sub |
| Logging | loguru | Structured JSON logs, context binding for correlation IDs |
| Containers | Docker Compose | One-command reproducible environment |

## Bonus Features Implemented

| Feature | Status | Implementation |
|---------|--------|----------------|
| LangGraph workflow | ✅ | Declarative `StateGraph` for idempotent pipeline orchestration |
| Vector DB (ChromaDB) | ✅ | Semantic near-duplicate detection using Gemini `text-embedding-004` |
| Slack/Discord notifications | ✅ | Non-blocking webhook delivery via `httpx` on pipeline events |
| Rate limiting | ✅ | `slowapi` with Redis backend — per-endpoint limits |
| SSE streaming | ✅ | Real-time pipeline events via Redis Pub/Sub |
| Docker Compose | ✅ | One-command startup with health checks and volume persistence |
| Admin dashboard API | ✅ | Queue status, failures, routing stats, execution logs |
| Mock enrichment | ✅ | Keyword-based fallback — **works without any API key** |
| Redis queue | ✅ | Celery task broker + rate limit store + pub/sub |
| Deployment-ready | ✅ | Health probes, env config, graceful degradation |

## Project Structure

```
geta-lead-pipeline/
├── app/
│   ├── api/routes/          # HTTP handlers (leads, webhooks, admin, health, stream)
│   ├── core/                # Config, database, logging, exceptions, middleware
│   ├── models/              # SQLAlchemy ORM (5 tables)
│   ├── schemas/             # Pydantic request/response schemas
│   ├── services/            # Business logic (validation, enrichment, scoring, routing,
│   │                        #   llm_client, langgraph_pipeline, vector_store, notifications)
│   └── tasks/               # Celery task definitions + retry policies
├── alembic/                 # Database migrations
├── tests/                   # 102 tests (72 unit + 30 integration)
├── docker/                  # Dockerfiles (API + Worker)
├── ARCHITECTURE.md          # Detailed design decisions with tradeoffs
├── DEBUGGING.md             # Operational runbook for diagnosing failures
└── docker-compose.yml       # One-command startup
```

## What I'd Add With More Time

1. **Circuit breaker on LLM calls** — Stop calling Gemini after N consecutive failures, cooldown period
2. **Prometheus metrics** — Request latency, queue depth, LLM call duration histograms
3. **Celery Flower** — Visual task monitoring dashboard
4. **n8n integration** — External workflow orchestration for lead routing to CRMs
5. **Load testing with Locust** — Pipeline throughput benchmarking under sustained load
6. **Multi-provider LLM fallback** — If Gemini is down, fall back to OpenAI
7. **Blue/green deployment support** — Zero-downtime schema migrations
