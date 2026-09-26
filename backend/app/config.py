"""Application settings, read from environment variables with dev defaults.

Loads `backend/.env` if present (stdlib parser, no dependency). Never commit
real credentials — `.env` is gitignored; `.env.example` documents the shape.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

JWT_SECRET_PLACEHOLDERS = {
    "change-this-to-32-plus-byte-secret-in-prod",
    "your-32-plus-character-secret-here",
    "your_jwt_secret",
    "change-me",
    "dev-secret-change-me",
}
LOCAL_JWT_SECRET = "supportdesk-local-only-development-secret-32-bytes"


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _is_deployment() -> bool:
    render = os.getenv("RENDER", "").strip().lower()
    environment = os.getenv("ENVIRONMENT", "").strip().lower()
    return render in {"1", "true", "yes", "on"} or environment in {
        "production",
        "prod",
        "staging",
    }


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./supportdesk.db")


def _jwt_secret_value() -> str:
    raw_value = os.getenv("JWT_SECRET")
    if raw_value is None:
        if _is_deployment():
            raise RuntimeError("JWT_SECRET is required in deployment")
        return LOCAL_JWT_SECRET

    value = raw_value.strip()
    if value in JWT_SECRET_PLACEHOLDERS:
        raise RuntimeError("JWT_SECRET must not be a placeholder")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    jwt_secret: str = _jwt_secret_value()
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))

    # --- AI provider: "stub" (offline) | "cloudflare" (Workers AI) | "openai" ---
    ai_provider: str = os.getenv("AI_PROVIDER", "stub")
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")

    # Guardrail on unsafe LLM output: "reject" (return 502, keep ticket) default,
    # or "fallback" (return a neutral draft instead of the blocked one).
    ai_guardrail_mode: str = os.getenv("AI_GUARDRAIL_MODE", "reject")

    # Knowledge base directory for RAG
    knowledge_dir: str = os.getenv("KNOWLEDGE_DIR", "")

    # Embedder: "bow" (hashed bag-of-words, offline default) | "hf" (sentence-transformers, lazy-load, BoW fallback)
    ai_embed_provider: str = os.getenv("AI_EMBED_PROVIDER", "bow")

    # Guest (unauthenticated) ticket submissions are rate limited per peer IP.
    # Set the limit to 0 to disable. Authenticated submitters are not counted:
    # they are already attributable to an account.
    ticket_create_rate_limit: int = int(os.getenv("TICKET_CREATE_RATE_LIMIT", "10"))
    ticket_create_rate_window_s: float = float(
        os.getenv("TICKET_CREATE_RATE_WINDOW_S", "3600")
    )

    # Email provider: "stub" | "log" | "smtp"
    email_provider: str = os.getenv("EMAIL_PROVIDER", "stub")
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_timeout_s: float = float(os.getenv("SMTP_TIMEOUT_S", "5"))

    # Workflow confidence thresholds
    workflow_high_confidence: float = float(os.getenv("WORKFLOW_HIGH_CONFIDENCE", "0.85"))
    workflow_low_confidence: float = float(os.getenv("WORKFLOW_LOW_CONFIDENCE", "0.60"))

    # Cloudflare Workers AI
    cloudflare_account_id: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cloudflare_api_token: str = os.getenv("CLOUDFLARE_API_TOKEN", "")
    cloudflare_model: str = os.getenv("CLOUDFLARE_MODEL", "@cf/meta/llama-3.1-8b-instruct")

    # OpenAI-compatible (kept as an alternative provider)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    alembic_migrate: bool = _as_bool(
        os.getenv("ALEMBIC_MIGRATE"), default=_is_deployment()
    )

    # One-time prod bootstrap secret. Empty = bootstrap endpoint disabled.
    bootstrap_token: str = os.getenv("BOOTSTRAP_TOKEN", "")

    # Inbound webhook HMAC secret. Empty = skip signature check in dev/test
    # (log warning); in deployment an empty secret rejects with 403.
    inbound_webhook_secret: str = os.getenv("INBOUND_WEBHOOK_SECRET", "")

    cors_origins: list[str] = field(
        default_factory=lambda: _split_origins(
            os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://localhost:3000"
                if not _is_deployment()
                else "",
            )
        )
    )


settings = Settings()

if len(settings.jwt_secret) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters")

parsed = urlparse(settings.database_url)
if not parsed.scheme:
    raise RuntimeError("DATABASE_URL must have a scheme")
if parsed.scheme not in {"sqlite", "postgresql", "postgres", "mysql"}:
    raise RuntimeError(f"Unsupported database dialect: {parsed.scheme}")
if _is_deployment() and parsed.scheme not in {"postgresql", "postgres"}:
    raise RuntimeError("DATABASE_URL must use PostgreSQL in deployment")
