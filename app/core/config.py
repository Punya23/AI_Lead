"""
Application configuration via pydantic-settings.

All values are loaded from environment variables (or .env file).
Zero hardcoded credentials — everything is configurable.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore Docker-only vars (POSTGRES_USER, etc.)
    )

    # --- Application ---
    APP_NAME: str = "geta-lead-pipeline"
    APP_ENV: str = "development"
    DEBUG: bool = True

    # --- Database ---
    DATABASE_URL_ASYNC: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/leads_db"
    DATABASE_URL_SYNC: str = "postgresql+psycopg2://postgres:postgres@postgres:5432/leads_db"

    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Celery ---
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/1"

    # --- LLM (Google Gemini) ---
    GOOGLE_API_KEY: str = ""
    LLM_MODEL: str = "gemini-2.5-flash"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_RETRIES: int = 3
    LLM_TIMEOUT_SECONDS: int = 30

    # --- Routing Thresholds ---
    ROUTING_HIGH_THRESHOLD: int = 70
    ROUTING_MEDIUM_THRESHOLD: int = 40

    # --- Failure Simulation ---
    SIMULATE_FAILURES: bool = False
    FAILURE_RATE_LLM_TIMEOUT: float = 0.15
    FAILURE_RATE_MALFORMED_RESPONSE: float = 0.10
    FAILURE_RATE_DB_ERROR: float = 0.05
    FAILURE_RATE_RATE_LIMIT: float = 0.10

    # --- Rate Limiting ---
    RATE_LIMIT_PER_MINUTE: int = 60

    # --- Notifications (empty = disabled) ---
    SLACK_WEBHOOK_URL: str = ""
    DISCORD_WEBHOOK_URL: str = ""

    # --- Semantic Dedup (ChromaDB) ---
    ENABLE_SEMANTIC_DEDUP: bool = True
    SEMANTIC_SIMILARITY_THRESHOLD: float = 0.85
    CHROMA_PERSIST_DIR: str = "/app/data/chroma"

    # --- Server ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000


# Singleton instance — import this everywhere
settings = Settings()
