"""Database engine and session management.

SQLite is the local dev/test default; production points DATABASE_URL at PostgreSQL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


def _resolve_database_url(url: str) -> str:
    """Ensure PostgreSQL URLs specify an available driver (fallback to psycopg2 if psycopg3 not present)."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            try:
                import psycopg2  # noqa: F401
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            except ImportError:
                pass
    return url


db_url = _resolve_database_url(settings.database_url)
engine = create_engine(db_url, **_engine_kwargs(db_url))
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
