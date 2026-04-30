"""Alembic environment configuration for PGMRec.

Reads the database URL from PGMREC_DATABASE_URL (via pydantic-settings) so
that the same migrations work for both SQLite (dev) and PostgreSQL (production).

Usage (from backend/ directory):
    alembic upgrade head          # apply all pending migrations
    alembic downgrade -1          # roll back one migration
    alembic revision --autogenerate -m "add_foo"  # generate a new migration
"""
import sys
from pathlib import Path

# Ensure the backend package is importable from this file's location
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import PGMRec models so autogenerate can detect schema changes
from app.db.models import Base  # noqa: F401
from app.config.settings import get_settings

# Alembic Config object (provides access to alembic.ini values)
config = context.config

# Resolve the database URL from PGMRec settings
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret alembic.ini logging config (if present)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — no DB connection required.
    Generates SQL scripts instead of executing them.
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
    """Run migrations in 'online' mode — connects to the DB and applies changes."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
