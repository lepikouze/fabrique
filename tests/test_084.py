"""0.8.4: demo data never counts as real setup, the demo link-notebook repo is flagged and hidden, sober HTML e-mails."""
from sqlalchemy import select
from app import config, db, demo, jobs, main, notify


def _step(name):
    return next(s for s in main.start_steps() if s[0] == name)[1]


def test_demo_data_never_counts_as_real_setup(monkeypatch):
    with db.tx() as c:
        db.set_setting(c, "git", [], "test")
        db.set_setting(c, "models", db.DEFAULTS["models"], "test")
        db.set_setting(c, "agents", db.DEFAULTS["agents"], "test")
        c.execute(db.repos.delete())
    monkeypatch.setattr(config, "DEMO", True)
    assert demo.seed() is True
    assert _step("A Git provider") is False      # a demo repo must not tick the Git provider step
    assert _step("A first repo") is False        # demo repos are excluded from the real repo count
    assert _step("A default model") is False
    assert _step("Administrator account") is True


def test_demo_link_notebook_is_flagged_and_follows_the_visibility(monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    assert demo.seed() is True
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.slug == "team/link-notebook"))
    assert jobs.is_demo_repo(r)                   # the bug: this repo used to lack the demo flag
    with db.tx() as c:
        db.set_setting(c, "demo", {"visible": False}, "test")
    jobs.forget_demo()
    assert "team/link-notebook" not in [x["slug"] for x in jobs.repos_for_requests()]
    with db.tx() as c:
        db.set_setting(c, "demo", {"visible": True}, "test")
    jobs.forget_demo()
    assert "team/link-notebook" in [x["slug"] for x in jobs.repos_for_requests()]


def test_email_html_is_sober_translated_and_escapes_user_text():
    h = notify.email_html({"title": "FAB-1: PR ready", "text": "line a\nline b",
                           "url": "https://f.example/jobs?job=FAB-1", "pr_url": "https://g/x/pulls/1", "lang": "fr"})
    assert "Fabrique" in h and "Ouvrir dans la Fabrique" in h and "Voir la pull request" in h and "<br>" in h
    bad = notify.email_html({"title": "<script>alert(1)</script>", "text": "a & b", "url": "https://f", "lang": "en"})
    assert "<script>" not in bad and "&lt;script&gt;" in bad and "a &amp; b" in bad and "Open in Fabrique" in bad
