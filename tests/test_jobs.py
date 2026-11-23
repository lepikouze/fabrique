"""Human-side job cycle: request, question and answer, merge decision, spec, archive; inbox by role."""
import pytest
from sqlalchemy import insert, select, update
from app import i18n, db, demo, jobs
from .conftest import ADMIN, REQ, REVIEWER


def _set(ref, **vals):
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(**vals))


def _ask(ref, q="A ou B ?"):
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(insert(db.questions).values(job_id=jid, question=q, asked_at=1.0))
    _set(ref, stage="waiting")


def test_create_uses_repo_then_global_settings(repo):
    ref = jobs.create(REQ, "Exporter en CSV", "Un bouton export.", "Un test couvre l'export.", repo)
    j = jobs.get(ADMIN, ref)
    assert ref == "FAB-1" and j["stage"] == "queued" and j["branch"] == "fabrique/FAB-1"
    assert j["harness"] == "claude-code" and j["model"] == "claude-opus-5-5" and j["budget"] == 5.0
    assert j["requester"] == "rémi" and j["audit"][0]["action"] == "Request created"


def test_requester_cannot_pick_agent_or_model(repo):
    ref = jobs.create(REQ, "T", "B", "", repo, harness="codex", model="gpt-oss-120b")
    j = jobs.get(ADMIN, ref)
    assert (j["harness"], j["model"]) == ("claude-code", "claude-opus-5-5")
    ref2 = jobs.create(REVIEWER, "T", "B", "", repo, harness="claude-code", model="qwen3.8-27b")
    assert (jobs.get(ADMIN, ref2)["harness"], jobs.get(ADMIN, ref2)["model"]) == ("claude-code", "qwen3.8-27b")
    with pytest.raises(jobs.Refused):
        jobs.create(REVIEWER, "T", "B", "", repo, harness="opencode", model="qwen3.8-27b")      # agent inconnu


@pytest.mark.parametrize("kw,msg", [(dict(title="", need="x"), "required"), (dict(title="x", need=""), "required"),
                                    (dict(title="x", need="y", harness="cursor"), "Unknown agent"),
                                    (dict(title="x", need="y", model="gpt-9"), "Unknown model")])
def test_create_refusals(repo, kw, msg):
    with pytest.raises(jobs.Refused, match=msg):
        jobs.create(ADMIN, kw["title"], kw["need"], "", repo, kw.get("harness", ""), kw.get("model", ""))


def test_closed_repo_refuses_requests(repo):
    with db.tx() as c:
        c.execute(update(db.repos).values(accepts_jobs=False))
    with pytest.raises(jobs.Refused, match="does not accept"):
        jobs.create(ADMIN, "T", "B", "", repo)


def test_requester_sees_only_own_jobs(repo):
    mine = jobs.create(REQ, "À moi", "B", "", repo)
    other = jobs.create(REVIEWER, "À Vera", "B", "", repo)
    assert [j["ref"] for j in jobs.listing(REQ)] == [mine]
    assert jobs.get(REQ, other) is None
    assert {j["ref"] for j in jobs.listing(REVIEWER)} == {mine, other}


def test_answer_resumes_at_triage(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _ask(ref)
    assert [i["kind"] for i in jobs.inbox(REQ)] == ["question"]
    jobs.answer(REQ, ref, "B, merci")
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "triage" and j["questions"][0]["answer"] == "B, merci" and j["questions"][0]["answered_by"] == "rémi"
    assert jobs.inbox(REQ) == []
    with pytest.raises(jobs.Refused, match="No question"):
        jobs.answer(REQ, ref, "encore")


def test_requester_cannot_answer_someone_elses_question(repo):
    ref = jobs.create(REVIEWER, "T", "B", "", repo)
    _ask(ref)
    with pytest.raises(jobs.Refused, match="Job not found"):
        jobs.answer(REQ, ref, "je réponds quand même")
    jobs.answer(ADMIN, ref, "ok")


def test_merge_decision_only_for_reviewers(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234")
    assert [i["kind"] for i in jobs.inbox(REVIEWER)] == ["review"]
    assert jobs.inbox(REQ) == []          # the requester does not see the PRs to review
    with pytest.raises(jobs.Refused, match="reviewers"):
        jobs.decide(REQ, ref, "approve")
    jobs.decide(REVIEWER, ref, "approve", "RAS")
    j = jobs.get(ADMIN, ref)
    assert j["merge_state"] == "approved" and j["audit"][0]["action"] == "Merge approved" and j["audit"][0]["actor"] == "vera"
    with pytest.raises(jobs.Refused, match="is not waiting for"):
        jobs.decide(REVIEWER, ref, "approve")


def test_spec_validation(repo):
    a, b = jobs.create(REQ, "A", "B", "", repo), jobs.create(REQ, "B", "B", "", repo)
    _set(a, stage="spec"); _set(b, stage="spec")
    jobs.validate_spec(REVIEWER, a, True)
    jobs.validate_spec(ADMIN, b, False, "trop large")
    assert jobs.get(ADMIN, a)["stage"] == "investigating"
    assert jobs.get(ADMIN, b)["stage"] == "failed" and "trop large" in jobs.get(ADMIN, b)["status_text"]


def test_archive_only_finished_jobs(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    with pytest.raises(jobs.Refused, match="finished"):
        jobs.archive(REVIEWER, ref)
    _set(ref, stage="merged")
    with pytest.raises(jobs.Refused, match="reviewers"):
        jobs.archive(REQ, ref)
    jobs.archive(REVIEWER, ref)
    assert jobs.listing(ADMIN) == [] and jobs.archived_count(ADMIN) == 1
    jobs.archive(REVIEWER, ref, restore=True)
    assert len(jobs.listing(ADMIN)) == 1


def test_timeline_shows_future_stages(repo, demo_on):
    ref = jobs.create(REQ, "T", "B", "", repo)
    demo.step(ref, "triage")
    tl = jobs.get(ADMIN, ref)["timeline"]
    assert tl[0]["state"] == "current" and tl[0]["stage"] == "triage"
    assert [t["stage"] for t in tl[1:]] == ["investigating", "implementing", "verifying", "review", "pr", "merged"]


def test_review_effort_shown_with_the_merge_decision():
    assert jobs.review_effort({"diff": {}}) is None
    e = jobs.review_effort({"diff": {"files": ["a.py", "b.py", "c.py"], "additions": 90, "deletions": 10}})
    assert (e["n"], e["label"], e["files"], e["add"], e["rem"]) == (2, "light", 3, 90, 10)
    prot = {"diff": {"files": ["a.py"], "additions": 5, "deletions": 0}, "decision": {"limits": [{"key": "protected", "name": "Protected paths", "ok": False}]}}
    assert jobs.review_effort(prot)["n"] == 2                                     # protected path: one step further
    named = dict(prot, decision={"limits": [{"key": "size", "name": "Protected paths", "ok": False}]})
    assert jobs.review_effort(named)["n"] == 1                                    # the key decides, never the displayed name


def test_demo_renews_itself_when_nobody_answers(demo_on):
    demo.seed()
    old = __import__("time").time() - demo.HUMAN_WAIT - 5
    with db.tx() as c:                                                            # no one has responded for a long time
        c.execute(update(db.jobs).where(db.jobs.c.source == "demo").values(updated_at=old))
    for _ in range(300):
        demo.tick()
        with db.tx() as c:
            live = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source == "demo"))
            c.execute(update(db.jobs).where(db.jobs.c.source == "demo").values(updated_at=old))
        assert len(live) <= max(demo.MAX_LIVE, 13)                                # the seed has 13: never more
    with db.tx() as c:
        live = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source == "demo"))
        archived = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(True), db.jobs.c.source == "demo"))
        answered = db.rows(c, select(db.questions).where(db.questions.c.answered_by == "camille"))
    assert len(live) <= demo.MAX_LIVE and archived and answered                   # renewed: answers and archiving
    with db.tx() as c:                                                            # the ready PRs have been approved
        assert db.rows(c, select(db.audit).where(db.audit.c.detail == i18n.M("demo approval")))
    assert {j["ref"] for j in jobs.listing(ADMIN)} == {j["ref"] for j in live}
    assert len(archived) <= demo.MAX_ARCHIVED                                     # the demo archives do not grow either


def test_demo_seed_and_simulator_run(demo_on):
    assert demo.seed() is True and demo.seed() is False
    for _ in range(40):
        demo.tick()
    js = jobs.listing(ADMIN)
    stages = {j["stage"] for j in js}
    assert "merged" in stages and len(js) <= demo.MAX_LIVE                       # ceiling: the demo does not grow indefinitely
    merged = next(j for j in js if j["stage"] == "merged")
    full = jobs.get(ADMIN, merged["ref"])
    assert full["evidence"] and full["decision"] and full["agents"]
    c = jobs.counters(ADMIN)
    assert c["total"] == len(js) and c["spend"] > 0


def test_events_feed_moves_on_change(repo):
    before = jobs.last_event_id()
    jobs.create(REQ, "T", "B", "", repo)
    assert jobs.last_event_id() > before


def test_default_agent_not_ready_is_refused_clearly(repo):
    from app import db
    with db.tx() as c:
        db.set_setting(c, "agents", {"harness": "opencode", "model": "qwen3.8-27b"}, "alice")
    with pytest.raises(jobs.Refused):
        jobs.create(REVIEWER, "T", "B", "", repo)
