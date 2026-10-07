"""Database engine and session management.

SQLite is the local dev/test default; production points DATABASE_URL at PostgreSQL.
"""

import logging
import socket
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _resolve_ipv4(url: str) -> str:
    """Pin the host to an IPv4 address when ``DB_PREFER_IPV4`` is set.

    Some environments (e.g. a kind cluster) have no IPv6 route, but the DB
    hostname still resolves to an AAAA record. psycopg may then pick the IPv6
    address and fail with "Network is unreachable". When enabled, this resolves
    the host's A records and appends ``hostaddr=<ipv4>`` so the driver dials
    IPv4 directly. If no A record exists the URL is returned unchanged.

    Off by default: Render/Neon and the test suite are unaffected unless the
    operator sets ``DB_PREFER_IPV4=true``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        return url

    try:
        infos = socket.getaddrinfo(
            parsed.hostname, parsed.port or 5432, family=socket.AF_INET
        )
    except socket.gaierror as exc:
        logger.warning(
            "DB_PREFER_IPV4: no A record for %s (%s); using hostname as-is",
            parsed.hostname,
            exc,
        )
        return url

    ipv4 = infos[0][4][0]
    # hostaddr overrides the hostname for the TCP dial only; the URL keeps its
    # logical host so logs and error messages still name the real DB host.
    new_query = f"hostaddr={ipv4}"
    if parsed.query:
        new_query += f"&{parsed.query}"
    return urlunparse(parsed._replace(query=new_query))


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


def _prefer_ipv4() -> bool:
    return settings.db_prefer_ipv4


_raw_url = _resolve_database_url(settings.database_url)
db_url = _resolve_ipv4(_raw_url) if _prefer_ipv4() else _raw_url
engine = create_engine(db_url, **_engine_kwargs(db_url))
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
