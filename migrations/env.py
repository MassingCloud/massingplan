"""Alembic environment.

The database URL comes from `Settings`, not from `alembic.ini`. One source for
it means `flask db upgrade`, `massingplan check` and the running app cannot
disagree about which database they are talking to -- a disagreement whose
symptom is "the migration ran but the app still sees the old schema".
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from massingplan.config import Settings
from massingplan.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", Settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        # SQLite cannot ALTER most things; batch mode rebuilds the table around
        # the change. Without it every column alteration fails on the database
        # developers actually run.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
