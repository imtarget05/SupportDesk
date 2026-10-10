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

    # Pin Postgres connections to IPv4 (appends hostaddr=<ipv4> to the DSN).
    # For networks without IPv6 routing (e.g. kind) where the DB hostname has
    # AAAA records. Off by default — Render/Neon and tests are unaffected.
    db_prefer_ipv4: bool = _as_bool(os.getenv("DB_PREFER_IPV4"), default=False)

    # Guardrail on unsafe LLM output: "reject" (return 502, keep ticket) default,
    # or "fallback" (return a neutral draft instead of the blocked one).
    ai_guardrail_mode: str = os.getenv("AI_GUARDRAIL_MODE", "reject")

    # Monthly AI spend cap in USD — a hard stop checked before every paid
    # provider call, summing the cost recorded on ai_call_traces for the
    # current calendar month (see services/budget.py).
    #   > 0   — that many USD per calendar month; reaching it refuses paid calls
    #   <= 0  — cap disabled (explicit opt-out)
    # Default is the $10/project/month approved in docs/DECISIONS.md D5/D8.
    ai_monthly_budget_usd: float = float(os.getenv("AI_MONTHLY_BUDGET_USD", "10"))

    # Knowledge base directory for RAG
    knowledge_dir: str = os.getenv("KNOWLEDGE_DIR", "")

    # Embedder: "bow" (hashed bag-of-words, offline default) | "hf" (sentence-transformers, lazy-load, BoW fallback)
    ai_embed_provider: str = os.getenv("AI_EMBED_PROVIDER", "bow")

    # Distributed rate limiting for guest (anonymous) submissions.
    #   empty (default) — the limiter stays in-process: one instance, no
    #                      extra service, and no shared counter.
    #   REDIS_URL       — the counter lives in Redis, so every worker and
    #                      replica behind the ingress draws on one allowance.
    #                      This is the multi-instance case the in-process
    #                      dict cannot cover.
    #   UPSTASH_REDIS_URL — accepted as an alias for serverless Redis
    #                      (Upstash names its variable that way).
    # A Redis that is unimportable or unreachable degrades to the in-process
    # guard instead of failing the request the limiter protects.
    redis_url: str = os.getenv("REDIS_URL") or os.getenv("UPSTASH_REDIS_URL", "")
    # Connect/read budget for one rate-limit round trip, in seconds. Kept small
    # because a limiter that waits is a limiter that adds latency to the
    # request it is guarding, and the fallback is always available.
    redis_connect_timeout_s: float = float(os.getenv("REDIS_CONNECT_TIMEOUT_S", "0.5"))

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

    # Anthropic (Claude). ANTHROPIC_MODEL defaults to a current Claude model.
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    anthropic_max_tokens: int = int(os.getenv("ANTHROPIC_MAX_TOKENS", "800"))

    # OpenTelemetry GenAI tracing. Off by default: the app stays free of
    # exporter side effects unless this is switched on, and the in-process
    # counters in services/metrics.py keep working either way.
    otel_enabled: bool = _as_bool(os.getenv("OTEL_ENABLED"), default=False)
    otel_service_name: str = os.getenv("OTEL_SERVICE_NAME", "supportdesk-ai")
    otel_otlp_endpoint: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    # Vector store backend for the knowledge base.
    #   llama_index — in-process LlamaIndex index (default, no extra service)
    #   qdrant      — Qdrant client; falls back to llama_index if unreachable
    ai_vector_store: str = os.getenv("AI_VECTOR_STORE", "llama_index")
    qdrant_url: str = os.getenv("QDRANT_URL", "")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "support_kb")
    # Local on-disk path used when Qdrant runs in embedded mode.
    qdrant_path: str = os.getenv("QDRANT_PATH", "")

    # Temporal durable workflows. Off by default so tests and dev runs need no
    # Temporal server; the in-process runner in app/workflows/local_runner.py
    # executes the same definitions when this is false.
    temporal_enabled: bool = _as_bool(os.getenv("TEMPORAL_ENABLED"), default=False)
    temporal_host: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    temporal_namespace: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    temporal_task_queue: str = os.getenv("TEMPORAL_TASK_QUEUE", "supportdesk")
    # Per-activity timeout for one workflow stage, in seconds.
    workflow_stage_timeout_s: int = int(os.getenv("WORKFLOW_STAGE_TIMEOUT_S", "120"))

    # Max tool-calling steps for the agent loop, so a confused model cannot
    # spin forever.
    tool_max_steps: int = int(os.getenv("TOOL_MAX_STEPS", "6"))

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
valid_schemes = {"sqlite", "postgresql", "postgres", "mysql"}
if not any(parsed.scheme == s or parsed.scheme.startswith(s + "+") for s in valid_schemes):
    raise RuntimeError(f"Unsupported database dialect: {parsed.scheme}")
if _is_deployment() and not any(parsed.scheme == s or parsed.scheme.startswith(s + "+") for s in {"postgresql", "postgres"}):
    raise RuntimeError("DATABASE_URL must use PostgreSQL in deployment")
