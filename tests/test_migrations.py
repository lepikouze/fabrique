"""Database schema. 0.8.0 starts from a single baseline migration: there is no upgrade path from 0.7.x."""
import os
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from app import config, db, i18n


def alembic():
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(db.__file__), "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
    return cfg


def test_an_empty_database_gets_the_whole_schema():
    versions = [f for f in os.listdir(os.path.join(os.path.dirname(db.__file__), "migrations", "versions")) if f.endswith(".py")]
    assert sorted(versions) == ["0001_baseline.py", "0002_resumable.py"]
    insp = inspect(db.engine())
    assert set(insp.get_table_names()) - {"alembic_version"} == set(db.meta.tables)
    for name, table in db.meta.tables.items():                         # the migration and the models describe the same columns
        assert {c["name"] for c in insp.get_columns(name)} == {c.name for c in table.columns}, name
    assert "lang" in {c["name"] for c in insp.get_columns("jobs")} and "translations" in insp.get_table_names()


def test_the_baseline_can_be_undone_and_applied_again():
    command.downgrade(alembic(), "base")
    db.reset_engine()
    assert set(inspect(db.engine()).get_table_names()) <= {"alembic_version"}
    command.upgrade(alembic(), "head")
    db.reset_engine()
    assert set(db.meta.tables) <= set(inspect(db.engine()).get_table_names())


def test_a_new_installation_writes_in_english():
    with db.tx() as c:
        c.execute(db.settings.delete().where(db.settings.c.key == "languages"))    # the tests set French; a new installation has nothing
        assert db.get_setting(c, "languages")[0] == {"jobs": "en"} and i18n.job_lang(c) == "en"
