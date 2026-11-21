"""Les migrations Alembic produisent exactement le schéma déclaré dans app/db.py (aucune différence à générer)."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app import db


def test_schema_matches_metadata():
    with db.engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), db.meta)
    assert diff == []


def test_0012_marks_blank_repos_and_unblocks_the_initialised_ones(tmp_path, monkeypatch):
    """Repos d'avant la 0.5.0 : un vierge est marqué ; un initialisé puis bloqué par son onboarding redevient vierge ;
    un repo jamais initialisé (onboarding en échec) et un repo prêt ne bougent pas."""
    import time
    from alembic import command
    from sqlalchemy import insert, select
    from app import config, db
    cfg = db.alembic_config() if hasattr(db, "alembic_config") else None
    if cfg is None:
        from alembic.config import Config
        import os
        cfg = Config(); cfg.set_main_option("script_location", os.path.join(os.path.dirname(db.__file__), "migrations"))
        cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
    command.downgrade(cfg, "0011")
    t = time.time()
    with db.tx() as c:
        ids = {}
        for slug, onb, rec in (("a/vierge", "blank", {"setup": [], "checks": {}}), ("a/bloque", "failed", {"image": "node", "setup": [], "checks": {}}),
                               ("a/jamais", "failed", None), ("a/pret", "ready", {"setup": ["true"], "checks": {"t": "true"}})):
            # SQL écrit à la main : le schéma de la révision 0011, pas celui d'aujourd'hui (git_id n'existe pas encore)
            import json as _json
            from sqlalchemy import text as _text
            c.execute(_text("INSERT INTO repos (provider, slug, name, default_branch, is_default, accepts_jobs, onboarding, recipe, settings, created_at) "
                            "VALUES ('forgejo', :s, :s, 'main', 0, 1, :o, :r, :st, :t)"),
                      {"s": slug, "o": onb, "r": _json.dumps(rec) if rec is not None else None, "st": _json.dumps({"merge_policy": "trust"}), "t": t})
            ids[slug] = c.execute(_text("SELECT id FROM repos WHERE slug = :s"), {"s": slug}).scalar()
        for i, (slug, stage, text) in enumerate((("a/bloque", "merged", "Repo initialisé"), ("a/bloque", "failed", "Échec : l'agent abandonne"),
                                                 ("a/jamais", "failed", "Échec : clonage impossible"), ("a/pret", "merged", "Repo prêt"))):
            c.execute(insert(db.jobs).values(ref=f"M-{i}", title="Onboarding", request="r", done_when="", requester="fabrique", source="onboarding",
                                             repo_id=ids[slug], stage=stage, activity="idle", status_text=text, branch=f"fabrique/M-{i}",
                                             harness="claude-code", model="m", budget=5.0, created_at=t, updated_at=t))
    command.upgrade(cfg, "head")
    db.reset_engine()
    with db.tx() as c:
        r = {x["slug"]: x for x in db.rows(c, select(db.repos))}
    assert r["a/vierge"]["onboarding"] == "blank" and r["a/vierge"]["settings"] == {"merge_policy": "trust", "vierge": True}
    assert r["a/bloque"]["onboarding"] == "blank" and r["a/bloque"]["settings"]["vierge"] and r["a/bloque"]["settings"]["merge_policy"] == "trust"
    assert r["a/bloque"]["recipe"] == {"setup": [], "checks": {}, "env": []}
    assert r["a/jamais"]["onboarding"] == "failed" and "vierge" not in (r["a/jamais"]["settings"] or {})
    assert r["a/pret"]["onboarding"] == "ready" and "vierge" not in (r["a/pret"]["settings"] or {}) and r["a/pret"]["recipe"]["checks"] == {"t": "true"}


def test_0014_links_repos_to_their_git_provider_and_allows_the_same_path_twice(tmp_path):
    """Avant la 0.7.1 : un provider par type, sans identifiant. Après : chaque provider a un identifiant tiré de son nom,
    chaque repo est rattaché au provider de son type, et un même chemin peut exister chez deux providers."""
    import json, time
    from alembic import command
    from alembic.config import Config
    import os
    from sqlalchemy import select, text
    from app import config, db
    cfg = Config(); cfg.set_main_option("script_location", os.path.join(os.path.dirname(db.__file__), "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
    command.downgrade(cfg, "0013")
    t = time.time()
    with db.tx() as c:
        c.execute(text("DELETE FROM settings WHERE key = 'git'"))
        c.execute(text("INSERT INTO settings (key, value, updated_by, updated_at) VALUES ('git', :v, 't', :t)"),
                  {"v": json.dumps([{"name": "Forge de l'équipe", "type": "forgejo", "base_url": "https://git.test", "token": ""},
                                    {"name": "GitLab Interne", "type": "gitlab", "base_url": "https://gl.test", "token": ""}]), "t": t})
        for slug, prov in (("a/b", "forgejo"), ("g/p", "gitlab")):
            c.execute(text("INSERT INTO repos (provider, slug, name, default_branch, is_default, accepts_jobs, onboarding, settings, created_at) "
                           "VALUES (:p, :s, :s, 'main', 0, 1, 'ready', '{}', :t)"), {"p": prov, "s": slug, "t": t})
    command.upgrade(cfg, "head")
    db.reset_engine()
    with db.tx() as c:
        git = db.get_setting(c, "git")[0]
        repos = {r["slug"]: r for r in db.rows(c, select(db.repos))}
        assert [g["id"] for g in git] == ["forge-de-l-equipe", "gitlab-interne"]
        assert repos["a/b"]["git_id"] == "forge-de-l-equipe" and repos["g/p"]["git_id"] == "gitlab-interne"
        c.execute(db.repos.insert().values(provider="gitlab", git_id="autre-gitlab", slug="g/p", name="p", onboarding="ready",
                                           settings={}, created_at=t))                                      # même chemin, autre provider


def test_0015_adds_the_diff_table_and_can_be_undone(tmp_path):
    """0.7.5 : une table à part pour le diff du job (une ligne par job) ; le retour arrière la retire sans toucher aux jobs."""
    import os, time
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, insert, select
    from app import config, db
    cfg = Config(); cfg.set_main_option("script_location", os.path.join(os.path.dirname(db.__file__), "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
    assert "diffs" in inspect(db.engine()).get_table_names()
    command.downgrade(cfg, "0014")
    db.reset_engine()
    assert "diffs" not in inspect(db.engine()).get_table_names() and "jobs" in inspect(db.engine()).get_table_names()
    command.upgrade(cfg, "head")
    db.reset_engine()
    t = time.time()
    with db.tx() as c:
        rid = c.execute(insert(db.repos).values(provider="forgejo", slug="a/b", name="B", onboarding="ready", settings={}, created_at=t)).inserted_primary_key[0]
        jid = c.execute(insert(db.jobs).values(ref="FAB-1", title="T", request="R", requester="alice", repo_id=rid, branch="fabrique/FAB-1",
                                               created_at=t, updated_at=t)).inserted_primary_key[0]
        c.execute(insert(db.diffs).values(job_id=jid, sha="abcdef123456", at=t))
        row = db.one(c, select(db.diffs))
    assert row["text"] == "" and row["truncated"] is False and row["files"] == []
