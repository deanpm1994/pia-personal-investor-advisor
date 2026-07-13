"""Alembic environment for PIA application-owned schema history."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, create_engine

from pia_api.core.config import Settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# P2.1 deliberately owns no application tables. Future approved model metadata is
# registered here; Supabase-managed schemas, including auth, are never targeted.
target_metadata = MetaData(schema="public")


def get_url() -> str:
    """Return the server-only database URL without logging credentials."""
    return Settings().database_url


def run_migrations_offline() -> None:
    """Run migrations without creating a database connection."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="public",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured server-only database."""
    engine = create_engine(get_url(), pool_pre_ping=True)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema="public",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
