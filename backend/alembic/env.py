from logging.config import fileConfig

import os

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Import all models so Alembic can detect them for autogenerate
from app.database import Base  # noqa: E402
from app.models import User, Ticket, Message, TicketEmbedding, AIPrediction, AIEvaluation  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Single source of truth: DATABASE_URL env var overrides alembic.ini.
# Without this, `alembic upgrade head` always migrates the sqlite default
# from alembic.ini even when the app engine points at Postgres.
_db_url = os.getenv("DATABASE_URL")
if _db_url:
    if _db_url.startswith("postgres://"):
        _db_url = "postgresql://" + _db_url[len("postgres://"):]
    if _db_url.startswith("postgresql://"):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            try:
                import psycopg2  # noqa: F401
                _db_url = "postgresql+psycopg2://" + _db_url[len("postgresql://"):]
            except ImportError:
                pass
    config.set_main_option("sqlalchemy.url", _db_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined as the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def _is_postgresql_url(url: str) -> bool:
    return url.lower().startswith(("postgresql://", "postgres://", "postgresql+"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just an URL
    and not an Engine, though an Engine is acceptable here as well.
    By skipping the Engine creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        database_url = config.get_main_option("sqlalchemy.url", "")
        use_postgresql = _is_postgresql_url(database_url)

        try:
            context.configure(
                connection=connection, target_metadata=target_metadata
            )

            # Transaction-scoped advisory lock taken INSIDE the migration
            # transaction. A session-level pg_advisory_lock runs in its own
            # implicit transaction, which breaks the migration transaction on
            # Neon (the DDL appears to run but never persists); the xact
            # variant is bound to this transaction and auto-releases on commit.
            with context.begin_transaction():
                if use_postgresql:
                    connection.exec_driver_sql(
                        "SELECT pg_advisory_xact_lock(hashtext('supportdesk_migrations'))"
                    )
                context.run_migrations()
        except Exception:
            raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
