"""Alembic takes the trusted database URL from V2C_DATABASE_URL."""

import os
from alembic import context
from sqlalchemy import engine_from_config, pool
from v2c.models import Base

config = context.config
if os.getenv("V2C_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["V2C_DATABASE_URL"].replace("%", "%%"))
target_metadata = Base.metadata


def offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    offline()
else:
    online()
