"""Alembic environment for the store. `store.migrate` passes the models' metadata in `config.attributes`."""

from alembic import context
from sqlalchemy import create_engine

config = context.config
engine = create_engine(config.get_main_option("sqlalchemy.url"))
try:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=config.attributes.get("metadata"))
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
