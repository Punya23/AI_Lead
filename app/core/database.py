"""
Dual SQLAlchemy engine setup.

- Async engine (asyncpg) → used by FastAPI route handlers
- Sync engine (psycopg2) → used by Celery worker tasks

This dual-engine pattern is necessary because Celery runs tasks
synchronously. Using async SQLAlchemy inside Celery tasks causes
'RuntimeError: no running event loop'.
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


# =============================================================================
# Base Model — shared by all ORM models
# =============================================================================
class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


# =============================================================================
# Async Engine + Session (for FastAPI)
# =============================================================================
async_engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=settings.DEBUG,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # Detect stale connections
    pool_recycle=300,     # Recycle connections every 5 minutes
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncSession:
    """FastAPI dependency — yields an async database session.

    Usage in routes:
        async def my_route(db: AsyncSession = Depends(get_async_session)):
            ...

    Returns:
        AsyncSession: An async SQLAlchemy session that auto-closes.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# =============================================================================
# Sync Engine + Session (for Celery workers)
# =============================================================================
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=settings.DEBUG,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    class_=Session,
    expire_on_commit=False,
)


def get_sync_session() -> Session:
    """Celery task helper — returns a sync database session.

    Usage in tasks:
        with get_sync_session() as session:
            lead = session.get(Lead, lead_id)
            ...

    Returns:
        Session: A sync SQLAlchemy session that auto-closes.
    """
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_sync_session_ctx() -> Session:
    """Context-manager version for use in Celery tasks.

    Usage:
        session = get_sync_session_ctx()
        try:
            lead = session.get(Lead, lead_id)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    Returns:
        Session: A sync SQLAlchemy session.
    """
    return SyncSessionLocal()
