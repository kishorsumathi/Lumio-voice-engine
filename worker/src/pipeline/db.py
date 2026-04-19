"""Database session factory and helpers."""
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_db_url
from .models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionFactory = None


def _get_engine():
    global _engine
    if _engine is None:
        url = get_db_url()
        _engine = create_engine(
            url,
            pool_size=5,
            max_overflow=2,
            pool_pre_ping=True,       # verify connection health before use
            pool_recycle=1800,        # recycle connections every 30 min
            echo=False,
        )
        logger.info("Database engine created")
    return _engine


def _get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=_get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    factory = _get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_tables() -> None:
    """Create all tables if they don't exist (used for local dev / first deploy)."""
    engine = _get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database tables ensured")


def health_check() -> bool:
    """Return True if the database is reachable."""
    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("DB health check failed: %s", e)
        return False
