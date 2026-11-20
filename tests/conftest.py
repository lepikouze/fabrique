"""Environnement de test isolé : une base SQLite neuve par test (migrations Alembic appliquées), pas de simulateur."""
import os, tempfile
os.environ["FABRIQUE_DATA"] = tempfile.mkdtemp(prefix="fabrique-test-")
os.environ["FABRIQUE_DEMO"] = "0"
os.environ["FABRIQUE_SIMULATOR"] = "0"
os.environ["FABRIQUE_NOTIFY"] = "0"
os.environ["FABRIQUE_OPEN"] = "1"   # mode ouvert : la plupart des tests n'ont pas à se connecter

import pytest
from starlette.testclient import TestClient
from app import auth, config, db, main

ADMIN = {"user": "alice", "name": "Alice", "role": "admin"}
VERIF = {"user": "vera", "name": "Vera", "role": "verifier"}
REQ = {"user": "rémi", "name": "Rémi", "role": "requester"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    db.reset_engine(); auth.invalidate()
    db.migrate()
    # Une installation neuve n'a ni modèle local ni forge par défaut : les tests en déclarent (noms fictifs).
    local = [{"id": m, "name": m, "provider": "openai", "base_url": "http://modele.test:8080/v1"} for m in ("gemma-4-26b-a4b", "qwen3.8-27b", "gpt-oss-120b")]
    claude = [{"id": m, "name": n, "provider": "anthropic", "base_url": ""} for m, n in (
        ("claude-opus-5-5", "Claude Opus 5.5"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5"))]
    with db.tx() as c:
        db.set_setting(c, "models", claude + local, "test")
        db.set_setting(c, "agents", {"harness": "claude-code", "model": "claude-opus-5-5"}, "test")
        db.set_setting(c, "git", [{"name": "Forge de test", "type": "forgejo", "base_url": "https://git.test", "token_env": "FORGEJO_TOKEN"}], "test")
    yield
    db.reset_engine(); auth.invalidate()


def web(role=None):
    cl = TestClient(main.app, base_url="http://testserver", follow_redirects=False)
    if role:
        cl.cookies.set("fabrique_as", role)
    return cl


@pytest.fixture
def repo():
    from sqlalchemy import insert
    import time
    with db.tx() as c:
        return c.execute(insert(db.repos).values(provider="forgejo", slug="alice/essai", name="Essai", is_default=True, onboarding="ready",
                                                 settings={"merge_policy": "off"}, created_at=time.time())).inserted_primary_key[0]


@pytest.fixture
def demo_on(monkeypatch):
    """Mode démonstration actif et données affichées."""
    from app import jobs
    monkeypatch.setattr(config, "DEMO", True)
    with db.tx() as c:
        db.set_setting(c, "demo", {"visible": True}, "test")
    jobs.forget_demo()
    yield
    jobs.forget_demo()
