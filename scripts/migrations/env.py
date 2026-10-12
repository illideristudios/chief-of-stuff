"""Alembic environment for the store. `store.migrate` passes the models' metadata in `config.attributes`."""

from alembic import context
from sqlalchemy import create_engine

config = context.config
engine = create_engine(config.get_main_option("sqlalchemy.url"), connect_args={"timeout": 30})
try:
    with engine.connect() as connection:
        # WAL persists in the file. Set it during serial startup, not on each writer's connection:
        # changing the journal mode can return SQLITE_BUSY without honoring the busy timeout.
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.commit()  # End SQLAlchemy's autobegin before Alembic manages the migration transaction.
        context.configure(connection=connection, target_metadata=config.attributes.get("metadata"))
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
