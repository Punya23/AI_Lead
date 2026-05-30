# Architecture — AI Lead Processing Pipeline

## System Overview

This system is a **multi-stage AI-powered pipeline** that processes inbound leads from intake to routing. It's designed for reliability: every operation is logged, every failure is retried, and every lead has a complete audit trail.

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     INPUT LAYER                              │
│  POST /api/v1/leads      (single JSON lead)                 │
│  POST /api/v1/leads/batch (CSV upload)                      │
│  POST /api/v1/webhooks/lead (webhook → 202 Accepted)        │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                 VALIDATION (sync, in request)                │
│  1. Required fields check                                    │
│  2. Email format validation                                  │
│  3. Disposable email domain blocklist                        │
│  4. Spam keyword detection                                   │
│  5. Gibberish text detection                                 │
│  6. Duplicate check (SHA-256 content hash vs DB)            │
│                                                              │
│  Invalid → REJECTED (stored with raw_payload + reason)       │
│  Valid   → Celery task queue                                 │
└──────────────────────┬──────────────────────────────────────┘
                       │ (async via Redis)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                CELERY WORKER PIPELINE                         │
│                                                              │
│  Stage 1: AI ENRICHMENT (Google Gemini 2.0 Flash)           │
│  ├── Structured JSON output (response_mime_type)             │
│  ├── Few-shot prompting for consistency                      │
│  ├── Retry with corrective prompt on parse failure           │
│  └── Fallback defaults if all retries fail                   │
│                                                              │
│  Stage 2: DETERMINISTIC SCORING (Python code, NOT LLM)      │
│  ├── Intent clarity:         0-25 points                     │
│  ├── Urgency signal:         0-20 points                     │
│  ├── Company completeness:   0-20 points                     │
│  ├── Pain point specificity: 0-20 points                     │
│  ├── Message quality:        0-15 points                     │
│  └── TOTAL: 0-100 (same input = same output, always)        │
│                                                              │
│  Stage 3: INTELLIGENT ROUTING (configurable thresholds)     │
│  ├── score >= 70 → SALES_QUEUE                               │
│  ├── score 40-69 → NURTURE_QUEUE                             │
│  └── score < 40  → ARCHIVE                                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   PERSISTENCE (PostgreSQL)                    │
│  leads           — core data + pipeline state machine        │
│  enrichments     — AI outputs + raw LLM response             │
│  scores          — scoring breakdown (JSONB)                 │
│  routing_decisions — queue + reason + score at routing       │
│  execution_logs  — per-stage audit trail                     │
└─────────────────────────────────────────────────────────────┘
```

## Key Engineering Decisions

### 1. Why Hybrid Scoring (LLM + Python) Instead of Pure LLM?

**Decision**: LLM extracts qualitative signals (intent, urgency, pain points). Python code does quantitative scoring with deterministic weighted math.

**Why**:
- The assignment requires scoring to be "deterministic" and "explainable"
- LLMs are non-deterministic even at temperature 0 (due to float parallelism, batching, model updates)
- With deterministic Python scoring, we can:
  - Write unit tests that assert exact scores
  - Guarantee the same lead always gets the same score
  - Show the evaluator exactly how points were awarded via `scoring_breakdown`
- Using the LLM only for signal extraction is also cheaper (one API call instead of two) and faster

**Tradeoff**: Less flexible — adding new scoring signals requires code changes. But for a production system, that's actually a feature (changes are reviewed, tested, deployed).

### 2. Why Dual SQLAlchemy Engines (Async + Sync)?

**Decision**: FastAPI routes use async SQLAlchemy (asyncpg). Celery workers use sync SQLAlchemy (psycopg2).

**Why**:
- Celery tasks run in a synchronous execution context
- Using `async def` with `await` inside Celery tasks causes `RuntimeError: no running event loop`
- Wrapping async calls in `asyncio.run()` creates new event loops per call (resource leak)
- The dual-engine pattern is standard in production FastAPI + Celery systems

**Tradeoff**: Two database connection strings, two session factories. But they share the same ORM models and the same database.

### 3. Why Content-Based Dedup (No Timestamp)?

**Decision**: `SHA-256(email.lower() + company.lower() + message)` with a UNIQUE database constraint.

**Why**:
- A timestamp window (e.g., "same lead within 24 hours") creates edge cases at window boundaries
- Pure content hashing means: if the content is identical, it's a duplicate. Period.
- The UNIQUE constraint on `payload_hash` means the database enforces dedup, not just application logic

**Tradeoff**: A legitimate lead who sends the exact same message twice (different intent) would be blocked. In practice, this is extremely rare and the correct behavior.

### 4. Why Sync Validation, Async Pipeline?

**Decision**: Validation runs synchronously in the HTTP request handler. Enrichment/scoring/routing run asynchronously in Celery.

**Why**:
- Validation is fast (regex, hash lookup, string matching) — no reason to defer it
- Immediate rejection feedback is better UX than "accepted, will reject later"
- LLM calls take 1-5 seconds — blocking the HTTP request would be unacceptable
- The webhook endpoint returns 202 immediately per HTTP semantics

### 5. Why Fallback Enrichment Instead of Pipeline Stop?

**Decision**: When LLM enrichment fails after all retries, use conservative fallback defaults (category=Unknown, intent=Unknown, urgency=Low) and continue the pipeline with `flag_for_review=true`.

**Why**:
- Stopping the pipeline entirely means the lead is lost until manual intervention
- Conservative fallback defaults will route the lead to ARCHIVE (low score) — a safe default
- `flag_for_review=true` surfaces it in the admin dashboard for human review
- The raw lead data is preserved, so enrichment can be retried later

**Tradeoff**: The lead gets a low score that may not reflect its true quality. But a low-scored lead that's flagged for review is better than a lead stuck in limbo.

## Failure Taxonomy

| Failure Type | Detection | Recovery | Max Retries | After Max |
|-------------|-----------|----------|-------------|-----------|
| LLM Timeout | `httpx.TimeoutException` | Exponential backoff (1s → 2s → 4s) | 3 | Fallback enrichment + flag |
| Malformed JSON | `json.JSONDecodeError` or Pydantic `ValidationError` | Corrective prompt suffix | 3 | Fallback enrichment + flag |
| Rate Limit (429) | HTTP status code or simulated | Exponential backoff + jitter | 3 | Fallback enrichment + flag |
| DB Connection | `OperationalError` | Connection pool retry → Celery task retry | 3 | Task FAILED + dead-letter |
| Duplicate Lead | `payload_hash` UNIQUE violation | Reject immediately | 0 | REJECTED status |
| Partial Execution | State machine check on retry | Resume from last successful stage | 3 | FAILED + flag_for_review |

## Pipeline State Machine

```
RECEIVED → VALIDATED → ENRICHED → SCORED → ROUTED → COMPLETE
                                                      ↓
                                                    FAILED
                                                      ↓
                                              flag_for_review=true
                                              dead_lettered_at=now()
```

On Celery task retry, the pipeline checks the lead's current status and **resumes from the last successful stage**. This ensures:
- No stage is executed twice (idempotency)
- Completed enrichments are not re-fetched from the LLM
- Completed scores are not recalculated
- State is preserved even if the worker crashes mid-pipeline

## Database Schema

5 tables, all with UUID primary keys and timezone-aware timestamps:

- **leads**: Core lead data, pipeline state machine, review flags
- **enrichments**: AI-generated category, intent, urgency, pain points, summary
- **scores**: Deterministic score (0-100) with full signal breakdown
- **routing_decisions**: Queue assignment with reasoning
- **execution_logs**: Per-stage audit trail (started/success/failed/retrying)

## What I'd Do Differently With More Time

1. **Replace Celery with LangGraph** — State machine orchestration is better suited to LangGraph's graph-based approach
2. **Add a simple admin dashboard** — React frontend showing pipeline metrics, lead list, failure queue
3. **Implement WebSocket/SSE streaming** — Real-time lead processing updates for the dashboard
4. **Add MX record validation** — Verify email domains actually accept mail (reduces false positives)
5. **Implement rate limiting** — Per-IP throttling on intake endpoints to prevent abuse
6. **Add Celery Flower** — Visual task monitoring for debugging worker issues
7. **Load testing** — Locust or k6 to benchmark pipeline throughput under load
8. **Semantic dedup** — Use embedding similarity instead of exact hash for near-duplicate detection
9. **Multi-provider LLM fallback** — If Gemini is down, fall back to OpenAI or Anthropic
10. **Webhook delivery confirmation** — Verify downstream systems received the routed lead
