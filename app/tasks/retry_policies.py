"""
Retry policies for Celery tasks.

Defines per-task retry configuration including max retries,
backoff strategy, and which exceptions trigger retries.
"""

# --- Retry configuration for the lead pipeline task ---
LEAD_PIPELINE_RETRY_POLICY = {
    "max_retries": 3,
    "default_retry_delay": 1,       # Initial delay: 1 second
    "retry_backoff": True,           # Enable exponential backoff (1s → 2s → 4s)
    "retry_backoff_max": 30,         # Cap backoff at 30 seconds
    "retry_jitter": True,            # Add random jitter to prevent thundering herd
}

# --- Retry configuration for LLM calls (within the task) ---
LLM_RETRY_POLICY = {
    "max_attempts": 3,
    "initial_delay_seconds": 1.0,
    "backoff_multiplier": 2.0,       # 1s → 2s → 4s
    "max_delay_seconds": 10.0,
}

# --- Retry configuration for database operations ---
DB_RETRY_POLICY = {
    "max_attempts": 3,
    "initial_delay_seconds": 0.5,
    "backoff_multiplier": 2.0,       # 0.5s → 1s → 2s
    "max_delay_seconds": 5.0,
}
