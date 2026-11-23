"""Alembic environment: the metadata comes from app.db; the database address comes from the configuration (or from the caller)."""
from alembic import context
from sqlalchemy import engine_from_config, pool
from app import config, db

cfg = context.config
url = cfg.get_main_option("sqlalchemy.url") or config.DATABASE_URL


def run():
    if context.is_offline_mode():
        context.configure(url=url, target_metadata=db.meta, literal_binds=True, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    eng = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with eng.connect() as conn:
        # render_as_batch: ALTER TABLE statements also work with SQLite.
        context.configure(connection=conn, target_metadata=db.meta, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run()
