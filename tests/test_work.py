"""API des runners : tokens, file de commandes avec bail, événements limités aux jobs détenus, barrières de merge."""
import time
import pytest
from sqlalchemy import select, update
from app import db, jobs, work
from .conftest import ADMIN, REQ, VERIF, web


@pytest.fixture
def runner():
    tok = work.create_runner("runner-test", "conteneur", "alice")
    return tok


def api(tok):
    cl = web()
    cl.headers["Authorization"] = f"Bearer {tok}"
    return cl


def test_token_shown_once_and_only_hash_stored(runner):
    with db.tx() as c:
        r = db.one(c, select(db.runners).where(db.runners.c.name == "runner-test"))
    assert r["token_hash"] and runner not in str(r)
    assert web().post("/api/runner/heartbeat", json={}).status_code == 403
    assert api("fab_faux").post("/api/runner/claim").status_code == 403
    assert api(runner).post("/api/runner/heartbeat", json={"host": "h", "capacity": 2, "harnesses": [{"name": "Claude Code", "version": "2"}]}).json()["ok"]


def test_ui_creates_runner_and_shows_token_once():
    cl = web()
    r = cl.post("/runners", data={"name": "runner-ui", "kind": "conteneur"})
    assert r.status_code == 200 and "fab_" in r.text and r.headers["cache-control"] == "no-store"
    assert "fab_" not in cl.get("/runners").text
    assert web("verifier").post("/runners", data={"name": "x1"}).status_code == 303


def test_claim_gives_job_once_and_events_only_on_owned_jobs(runner, repo):
    ref = jobs.create(REQ, "Exporter", "En CSV", "testé", repo)
    other = work.create_runner("runner-2", "vm", "alice")
    a, b = api(runner), api(other)
    item = a.post("/api/runner/claim").json()
    assert item["intent"]["kind"] == "start" and item["job"]["ref"] == ref and item["repo"]["slug"] == "alice/essai"
    assert item["model"]["id"] == "claude-opus-5-5" and "request" in item["job"]
    assert b.post("/api/runner/claim").status_code == 204
    assert b.post(f"/api/runner/jobs/{ref}/event", json={"type": "stage", "stage": "triage", "summary": "x"}).status_code == 403
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "stage", "stage": "triage", "summary": "compris"}).status_code == 200
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "stage", "stage": "merged"}).status_code == 403   # étape réservée
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "triage" and j["runner"]["name"] == "runner-test"


def test_expired_lease_requeues_then_fails(runner, repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    a = api(runner)
    for i in range(work.MAX_ATTEMPTS):
        assert a.post("/api/runner/claim").json()["intent"]["attempt"] == i + 1
        with db.tx() as c:
            c.execute(update(db.intents).values(lease_until=time.time() - 1))
    a.post("/api/runner/claim")
    assert jobs.get(ADMIN, ref)["stage"] == "failed"


def test_question_answer_resume_cycle(runner, repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    a = api(runner)
    a.post("/api/runner/claim")
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "question", "question": "A ou B ?", "session_id": "sess-1"})
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "intent_done"})
    assert jobs.get(REQ, ref)["stage"] == "waiting"
    jobs.answer(REQ, ref, "B")
    item = a.post("/api/runner/claim").json()
    assert item["intent"]["kind"] == "resume" and item["intent"]["payload"]["answer"] == "B" and item["job"]["session_id"] == "sess-1"


def _to_pr(a, ref, files, add=10, confident=True, checks_ok=True, reviews=("ok", "ok")):
    with db.tx() as c:   # seule la commande de ce job reste en file
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(update(db.intents).where(db.intents.c.job_id != jid).values(state="done"))
    assert a.post("/api/runner/claim").json()["job"]["ref"] == ref
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "decision", "confident": confident, "verdict": "v", "interpretation": "i"})
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "evidence", "sha": "abcdef1234567", "checks": [
        {"name": "Tests", "command": "pytest", "ok": checks_ok, "duration_ms": 10, "output": "ok"}]})
    for role, verdict in zip(("code", "securite"), reviews):
        a.post(f"/api/runner/jobs/{ref}/event", json={"type": "review", "sha": "abcdef1234567", "role": role, "verdict": verdict, "summary": "s"})
    return a.post(f"/api/runner/jobs/{ref}/event", json={"type": "pr", "number": 7, "url": "https://git.test/x/pulls/7",
                                                         "files": files, "additions": add, "deletions": 0, "attempts": 1})


def _policy(repo, p):
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={"merge_policy": p}))


def test_confident_policy_merges_only_when_every_barrier_holds(runner, repo):
    _policy(repo, "confident")
    a = api(runner)
    ok = jobs.create(REQ, "Bon", "B", "", repo)
    assert _to_pr(a, ok, ["carnet/liens.py"]).status_code == 200
    j = jobs.get(ADMIN, ok)
    assert j["merge_state"] == "approved" and j["stage"] == "pr" and all(l["ok"] for l in j["decision"]["limits"])
    for name, kw in [("chemin", dict(files=["app/auth/x.py"])), ("taille", dict(files=["a.py"], add=900)),
                     ("avis", dict(files=["a.py"], confident=False)), ("checks", dict(files=["a.py"], checks_ok=False)),
                     ("recipe", dict(files=[".fabrique/setup.json"])), ("verifying", dict(files=["a.py"], reviews=("ok", "bloquant")))]:
        ref = jobs.create(REQ, name, "B", "", repo)
        _to_pr(a, ref, **kw)
        j = jobs.get(ADMIN, ref)
        assert j["merge_state"] == "ready", name
        assert [i for i in jobs.inbox(VERIF) if i["job"]["ref"] == ref and i["kind"] == "review"], name


def test_trial_policy_never_merges(runner, repo):
    _policy(repo, "trial")
    ref = jobs.create(REQ, "T", "B", "", repo)
    _to_pr(api(runner), ref, ["a.py"])
    j = jobs.get(ADMIN, ref)
    assert j["merge_state"] == "ready" and "aurait mergé" in j["status_text"]


def test_human_approval_enqueues_pinned_merge(runner, repo):
    a = api(runner)
    ref = jobs.create(REQ, "T", "B", "", repo)
    _to_pr(a, ref, ["a.py"])
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "intent_done"})
    jobs.decide(VERIF, ref, "approve")
    item = a.post("/api/runner/claim").json()
    assert item["intent"]["kind"] == "merge" and item["intent"]["payload"]["sha"] == "abcdef123456"
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "merged", "sha": "f00ba7"})
    assert jobs.get(ADMIN, ref)["stage"] == "merged"


def test_revoked_runner_loses_access_and_lease(runner, repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    api(runner).post("/api/runner/claim")
    with db.tx() as c:
        rid = db.one(c, select(db.runners.c.id).where(db.runners.c.name == "runner-test"))["id"]
    work.revoke_runner(rid, "alice")
    assert api(runner).post("/api/runner/heartbeat", json={}).status_code == 403
    with db.tx() as c:
        assert db.one(c, select(db.intents.c.state))["state"] == "pending"


def test_demo_simulator_ignores_real_jobs(repo, demo_on):
    from app import demo
    ref = jobs.create(REQ, "Vrai", "B", "", repo)
    for _ in range(10):
        demo.tick()
    assert jobs.get(ADMIN, ref)["stage"] == "queued"


@pytest.mark.parametrize("path,hit", [("app/auth/x.py", True), ("auth/x.py", True), (".forgejo/ci.yml", True), ("db/migrations/1.py", True),
                                      ("Dockerfile", True), ("src/Dockerfile.dev", False), ("carnet/liens.py", False), (".env.local", True)])
def test_protected_globs(path, hit):
    pats = ["**/auth/**", ".forgejo/**", "**/migrations/**", "Dockerfile", "**/.env*"]
    assert bool(work.protected_hits([path], pats)) == hit


def test_pr_event_returns_decision_for_the_pr_comment(runner, repo):
    _policy(repo, "trial")
    ref = jobs.create(REQ, "T", "B", "", repo)
    r = _to_pr(api(runner), ref, ["a.py"])
    d = r.json()["decision"]
    assert d["policy"] == "Essai" and any(l["name"] == "Vérification" and l["ok"] for l in d["limits"])


def test_daily_budget_holds_new_work_but_not_merges(runner, repo):
    from app import db as _db
    with _db.tx() as c:
        _db.set_setting(c, "limits", dict(_db.DEFAULTS["limits"], budget_day=1.0, budget_job=1.0), "t")
    old = jobs.create(REQ, "Cher", "B", "", repo)
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == old).values(cost=2.0))
        c.execute(update(db.intents).values(state="done"))
    new = jobs.create(REQ, "Nouveau", "B", "", repo)
    a = api(runner)
    assert a.post("/api/runner/claim").status_code == 204
    assert "Budget du jour" in jobs.get(ADMIN, new)["status_text"]
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == old))["id"]
        work.enqueue(c, jid, "merge", {"sha": "abc"})
    assert a.post("/api/runner/claim").json()["intent"]["kind"] == "merge"


def test_cancel_queued_job_immediately_and_running_job_via_runner(runner, repo):
    q = jobs.create(REQ, "Queue", "B", "", repo)
    with pytest.raises(jobs.Refused, match="reviewers"):
        jobs.cancel(REQ, q)
    jobs.cancel(VERIF, q)
    assert jobs.get(ADMIN, q)["stage"] == "failed"
    r = jobs.create(REQ, "En cours", "B", "", repo)
    a = api(runner)
    assert a.post("/api/runner/claim").json()["job"]["ref"] == r
    assert a.post("/api/runner/heartbeat", json={}).json()["cancel"] == []
    jobs.cancel(VERIF, r)
    assert a.post("/api/runner/heartbeat", json={}).json()["cancel"] == [r]
    a.post(f"/api/runner/jobs/{r}/event", json={"type": "cancelled"})
    j = jobs.get(ADMIN, r)
    assert j["stage"] == "failed" and "Vera" in j["status_text"]


def test_runner_api_refuses_requests_through_the_proxy(runner):
    cl = api(runner)
    assert cl.post("/api/runner/heartbeat", json={}, headers={"x-forwarded-for": "10.0.0.9"}).status_code == 403
    assert cl.post("/api/runner/heartbeat", json={}).status_code == 200


def test_resume_payload_carries_question_and_answer(runner, repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    a = api(runner)
    a.post("/api/runner/claim")
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "question", "question": "A ou B ?", "session_id": "s"})
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "intent_done"})
    jobs.answer(REQ, ref, "B")
    p = a.post("/api/runner/claim").json()["intent"]["payload"]
    assert p["question"] == "A ou B ?" and p["answer"] == "B"


def test_runner_tags_route_jobs_like_gitlab_tags(runner, repo):
    from sqlalchemy import update
    from app import db, jobs, work
    tok = runner
    with db.tx() as c:
        r = db.one(c, db.repos.select().where(db.repos.c.id == repo))
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings=dict(r["settings"] or {}, tags=["gpu"])))
    ref = jobs.create({"user": "alice", "name": "Alice", "role": "admin"}, "T", "B", "", repo)
    me = work.runner_for(tok)
    work.heartbeat(me, {"host": "h", "tags": ["interne"]})
    assert work.claim(me) is None                                               # il manque l'étiquette « gpu »
    work.heartbeat(me, {"host": "h", "tags": "interne, GPU, mauvais_tag!"})
    item = work.claim(me)
    assert item and item["job"]["ref"] == ref
    with db.tx() as c:
        assert db.one(c, db.runners.select().where(db.runners.c.id == me["id"]))["tags"] == ["gpu", "interne"]


def test_claim_carries_the_git_token_set_in_the_panel(runner, repo, monkeypatch):
    from app import config, providers
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")
    ref = jobs.create(REQ, "T", "B", "", repo)
    a = api(runner)
    with db.tx() as c:
        db.set_setting(c, "git", [{"name": "Forge", "type": "forgejo", "base_url": "https://git.test", "token": providers.seal("token-robot")}], "test")
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(update(db.intents).where(db.intents.c.job_id != jid).values(state="done"))
    item = a.post("/api/runner/claim").json()
    assert item["forge"] == {"type": "forgejo", "base_url": "https://git.test", "token": "token-robot"}


def test_integrated_runner_is_registered_once_and_its_token_works(tmp_path, monkeypatch):
    from app import config
    f = tmp_path / "token-runner"
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_FILE", str(f))
    work.bootstrap_runner()
    tok = f.read_text().strip()
    assert tok.startswith("fab_") and api(tok).post("/api/runner/heartbeat", json={"host": "h"}).status_code == 200
    work.bootstrap_runner()                                                    # redémarrage : rien de plus
    assert f.read_text().strip() == tok
    with db.tx() as c:
        assert [r["name"] for r in db.rows(c, select(db.runners.c.name))] == ["runner-integre"]


def test_start_page_lists_what_is_left(repo):
    page = web().get("/start").text
    assert "Setup" in page and "Un runner en ligne" in page and "Un premier repo" in page
    assert web("requester").get("/start").status_code == 403


def test_rotating_a_runner_token_keeps_the_runner_and_kills_the_old_token(runner):
    cl = web()
    with db.tx() as c:
        rid = db.one(c, select(db.runners.c.id).where(db.runners.c.name == "runner-test"))["id"]
    assert web("verifier").post(f"/runners/{rid}/rotate").status_code == 303             # réservé aux admins
    r = cl.post(f"/runners/{rid}/rotate")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    new = "fab_" + r.text.split("fab_", 1)[1].split("<")[0].split('"')[0].strip()
    assert new != runner and "fab_" not in cl.get("/runners").text                       # affiché une seule fois
    assert api(runner).post("/api/runner/heartbeat", json={}).status_code == 403         # l'ancien ne passe plus
    assert api(new).post("/api/runner/heartbeat", json={}).json()["name"] == "runner-test"
    with db.tx() as c:
        assert len(db.rows(c, select(db.runners).where(db.runners.c.name == "runner-test"))) == 1
    assert "Token du runner renouvelé" in cl.get("/activity").text
    work.revoke_runner(rid, "alice")
    assert "introuvable" in cl.get(cl.post(f"/runners/{rid}/rotate").headers["location"]).text


def test_daily_budget_reached_during_a_job_is_reported_to_its_runner(runner, repo):
    ref = jobs.create(REQ, "En cours", "B", "", repo)
    a = api(runner)
    assert a.post("/api/runner/claim").json()["intent"]["kind"] == "start"
    assert a.post("/api/runner/heartbeat", json={}).json()["over_budget"] == []          # pas de budget du jour : rien
    with db.tx() as c:
        db.set_setting(c, "limits", dict(db.DEFAULTS["limits"], budget_day=1.0), "t")
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(cost=0.5))
    assert a.post("/api/runner/heartbeat", json={}).json()["over_budget"] == []          # sous le budget
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(cost=1.5))
    assert a.post("/api/runner/heartbeat", json={}).json()["over_budget"] == [ref]
    other = work.create_runner("runner-2", "vm", "alice")
    assert api(other).post("/api/runner/heartbeat", json={}).json()["over_budget"] == []   # seulement les jobs qu'il détient
