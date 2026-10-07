"""Unit tests for the DB_PREFER_IPV4 DSN pinning (``app.database._resolve_ipv4``).

The helper is a pure function over the DSN string: it must leave SQLite and
non-Postgres URLs untouched, append ``hostaddr=<ipv4>`` for Postgres hosts with
an A record, preserve existing query params, and fall back to the unchanged URL
when the host has no A record. No network or DB is touched.
"""

import socket

from app import database


def _pg_url(**overrides):
    parts = {
        "host": "db.example.com",
        "query": "",
    }
    parts.update(overrides)
    base = f"postgresql://user:pw@{parts['host']}:5432/app"
    return f"{base}?{parts['query']}" if parts["query"] else base


def test_sqlite_url_untouched(monkeypatch):
    url = "sqlite:////tmp/test.db"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve")))
    assert database._resolve_ipv4(url) == url


def test_non_postgres_scheme_untouched(monkeypatch):
    url = "mysql://user:pw@db.example.com:3306/app"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve")))
    assert database._resolve_ipv4(url) == url


def test_appends_hostaddr_for_ipv4_host(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, family=0, *a, **k: [(family, 1, 6, "", ("93.184.216.34", 0))],
    )
    out = database._resolve_ipv4(_pg_url())
    assert "hostaddr=93.184.216.34" in out
    assert "db.example.com" in out  # logical host preserved for logs/errors


def test_preserves_existing_query_params(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, family=0, *a, **k: [(family, 1, 6, "", ("10.1.2.3", 0))],
    )
    out = database._resolve_ipv4(_pg_url(query="sslmode=require"))
    assert "hostaddr=10.1.2.3" in out
    assert "sslmode=require" in out


def test_no_a_record_returns_url_unchanged(monkeypatch):
    def _raise(host, port, family=0, *a, **k):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    url = _pg_url()
    assert database._resolve_ipv4(url) == url


def test_prefer_ipv4_flag_defaults_off(monkeypatch):
    # The test suite never sets DB_PREFER_IPV4, so the import-time settings
    # singleton must have the flag off (Render/Neon unaffected by default).
    monkeypatch.delenv("DB_PREFER_IPV4", raising=False)
    assert database._prefer_ipv4() is False


def test_ipv4_flag_bool_parsing():
    # Only parsing logic behind the DB_PREFER_IPV4 Settings field.
    import app.config as cfg

    for truthy in ("1", "true", "yes", "on", " TRUE "):
        assert cfg._as_bool(truthy, default=False) is True
    for falsy in ("0", "false", "no", "off", ""):
        assert cfg._as_bool(falsy, default=True) is False
    assert cfg._as_bool(None, default=False) is False
    assert cfg._as_bool(None, default=True) is True
