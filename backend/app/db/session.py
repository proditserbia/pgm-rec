"""SQLAlchemy engine and session factory for PGMRec."""
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from ..config.settings import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """
    Enable WAL journal mode and other reliability pragmas for SQLite.

    - WAL mode:       allows concurrent readers while a write is in progress,
                      eliminates most "database is locked" errors under load.
    - synchronous=NORMAL: good balance between durability and write speed.
    - busy_timeout:   wait up to 5 seconds before raising OperationalError on
                      a locked database, instead of failing immediately.
    """
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url

        if url.startswith("sqlite"):
            # SQLite — no connection pool needed; enable WAL for concurrency safety
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
            )
            _apply_sqlite_pragmas(engine)
        else:
            # PostgreSQL (or any other RDBMS)
            # pool_pre_ping re-validates stale connections from the pool before use,
            # preventing errors after network hiccups or DB restarts.
            engine = create_engine(
                url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )

        _engine = engine
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _SessionLocal


def init_db() -> None:
    """Create all tables (idempotent).  Used for SQLite dev/test only.
    For PostgreSQL, run: alembic upgrade head."""
    Base.metadata.create_all(bind=get_engine())


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a DB session and closes it on exit."""
    SessionLocal = get_session_factory()
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
