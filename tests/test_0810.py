"""0.8.10: nothing lost, reviewers that see the whole change. A job stopped by a limit (duration, turns, a looping or
saturated model) keeps its work and can be resumed; the agent commits step by step."""
import threading, time
import pytest
from sqlalchemy import select, update
from app import db, jobs, work
from runner import config, messages
from runner import main as rmain
from .conftest import ADMIN, REQ, REVIEWER, web


def _runner(monkeypatch, run_cls, saved=True):
    events = []

    class Panel:
        def event(self, *a, **k):
            events.append(k)

    monkeypatch.setattr(rmain, "runner_for", lambda ctx: run_cls)
    r = rmain.Runner.__new__(rmain.Runner)
    r.panel, r.ex, r.gateway = Panel(), type("E", (), {"kill": lambda *a: None})(), type("G", (), {"lock": threading.Lock()})()
    r.settle = r.report_usage = lambda *a: None
    r.save_progress = lambda ctx: saved
    item = {"intent": {"kind": "start", "payload": {}}, "model": {"id": "m"},
            "job": {"ref": "FAB-1", "title": "T", "branch": "b", "session_id": "sess", "request": "x"}, "repo": {"slug": "t/r"}}
    ctx = rmain.Ctx(item)
    ctx.ex = r.ex
    return r, ctx, events


def _run(error, limit=None):
    class Run:
        def __init__(self, ctx, panel, prompt, resume=None, system=None):
            self.ctx, self.prompt = ctx, prompt
            self.cost = self.tokens = 0
            self.session_id, self.cancelled, self.stalled, self.result = "sess", False, False, ""
            self.error, self.limit = error, limit

        def run(self):
            return self
    return Run


# ---------- the runner: a limit interrupts, it does not fail ----------

def test_the_duration_limit_interrupts_the_job_with_its_work_saved(monkeypatch):
    r, ctx, _ = _runner(monkeypatch, _run("maximum duration exceeded (60 min)", limit="duration"))
    with pytest.raises(rmain.Interrupted, match="maximum duration") as e:
        r.agent(ctx, "go")
    assert e.value.saved is True and isinstance(e.value, rmain.Failed)


def test_the_last_turn_limit_interrupts_instead_of_failing(monkeypatch):
    r, ctx, _ = _runner(monkeypatch, _run("the agent stopped on an error: error_max_turns"), saved=False)
    with pytest.raises(rmain.Interrupted, match="did not finish after 3 runs") as e:
        r.agent(ctx, "go")
    assert ctx.turn_resumes == config.MAX_TURN_RESUMES and e.value.saved is False


def test_a_looping_model_interrupts_too(monkeypatch):
    r, ctx, _ = _runner(monkeypatch, _run("the model stopped 3 times in a row at its output limit", limit="loops"))
    with pytest.raises(rmain.Interrupted):
        r.agent(ctx, "go")


def test_an_ordinary_error_still_fails(monkeypatch):
    r, ctx, _ = _runner(monkeypatch, _run("the agent exited with code 2: boom"))
    with pytest.raises(rmain.Failed) as e:
        r.agent(ctx, "go")
    assert not isinstance(e.value, rmain.Interrupted)


def test_an_interrupted_order_is_reported_as_such(monkeypatch):
    events = []
    r = rmain.Runner.__new__(rmain.Runner)
    r.panel = type("P", (), {"event": lambda self, *a, **k: events.append(k)})()
    r.ex, r.running, r.lock, r.active = type("E", (), {"kill": lambda *a: None})(), {}, threading.Lock(), 1
    r.relay = type("R", (), {"forget": lambda *a: None})()
    r.close = lambda ctx, kind: None
    r.start = lambda ctx: (_ for _ in ()).throw(rmain.Interrupted("maximum duration exceeded (60 min)", True))
    item = {"intent": {"kind": "start", "payload": {}}, "model": {"id": "m"}, "job": {"ref": "FAB-1", "title": "T", "branch": "b"},
            "repo": {"slug": "t/r"}}
    r.execute(item)
    kinds = [e.get("type") for e in events]
    assert "interrupted" in kinds and "failed" not in kinds
    assert next(e for e in events if e.get("type") == "interrupted")["saved"] is True


def test_save_progress_commits_what_the_agent_left(monkeypatch):
    calls = []

    def jgit(ctx, args, check=True, **k):
        calls.append(args[0])
        out = {"status": " M src/a.py\n", "diff": "src/a.py\n", "rev-list": "2\n"}.get(args[0], "")
        return type("P", (), {"stdout": out, "returncode": 0})()
    monkeypatch.setattr(rmain, "jgit", jgit)
    r = rmain.Runner.__new__(rmain.Runner)
    r.setup_excludes = lambda ctx: []
    r.fork = lambda ctx: "base"
    assert r.save_progress(type("C", (), {"ref": "FAB-1"})()) is True
    assert calls == ["status", "add", "diff", "commit", "rev-list"]


def test_a_failed_save_never_hides_the_cause(monkeypatch):
    events = []

    def jgit(*a, **k):
        raise RuntimeError("container gone")
    monkeypatch.setattr(rmain, "jgit", jgit)
    r = rmain.Runner.__new__(rmain.Runner)
    r.panel = type("P", (), {"event": lambda self, *a, **k: events.append(k)})()
    assert r.save_progress(type("C", (), {"ref": "FAB-1"})()) is False
    assert "could not be committed" in str(events[0]["message"])


def test_the_resumed_agent_is_told_where_it_is_and_to_commit():
    m = messages.resume_interrupted({"ref": "FAB-1"}, "skip the TLS test", "Alex")
    assert "work in progress, saved by the runner" in m and "Commit after each step" in m
    assert "<note>\nskip the TLS test\n</note>" in m and "Alex adds" in m
    assert "<note>" not in messages.resume_interrupted({"ref": "FAB-1"})


def test_the_playbook_asks_to_commit_step_by_step_and_for_fast_tests():
    text = open(rmain.os.path.join(rmain.os.path.dirname(rmain.__file__), "playbook.md")).read()
    assert "Commit everything, step by step" in text and "Fast tests" in text


# ---------- the dashboard: interrupted, resumable, workspace kept ----------

def _claimed(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    work.create_runner("r1", "container", "alice")
    with db.tx() as c:
        run = db.one(c, select(db.runners).where(db.runners.c.name == "r1"))
    assert work.claim(run)["job"]["ref"] == ref
    return ref, run


def test_an_interrupted_job_can_be_resumed_where_it_stopped(repo):
    ref, run = _claimed(repo)
    work.report(run, ref, {"type": "interrupted", "reason": "maximum duration exceeded (60 min)", "saved": True, "attempts": 1})
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and j["resumable"] and j["status_text"].startswith("Interrupted")
    jobs.resume_interrupted(REQ, ref, "the SMTP part works, finish the tests")
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "implementing" and not j["resumable"]
    with db.tx() as c:
        it = db.rows(c, select(db.intents).where(db.intents.c.job_id == j["id"]).order_by(db.intents.c.id))[-1]
    assert it["kind"] == "resume" and it["payload"]["interrupted"] and it["payload"]["note"].startswith("the SMTP part")


def test_only_an_interrupted_job_can_be_resumed(repo):
    ref, run = _claimed(repo)
    work.report(run, ref, {"type": "failed", "reason": "checks failed after 3 attempts"})
    with pytest.raises(jobs.Refused, match="cannot be resumed"):
        jobs.resume_interrupted(ADMIN, ref)


def test_an_interrupted_workspace_is_kept_a_week(repo):
    ref, run = _claimed(repo)
    work.report(run, ref, {"type": "interrupted", "reason": "x", "saved": True})
    with db.tx() as c:
        c.execute(update(db.intents).values(state="done"))
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(updated_at=time.time() - 2 * 3600))
    assert jobs.workspaces_to_purge([ref]) == []
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(archived=True))
    assert jobs.workspaces_to_purge([ref]) == [ref]


def test_the_resume_button_is_shown_on_an_interrupted_job(repo):
    ref, run = _claimed(repo)
    client = web("admin")
    work.report(run, ref, {"type": "interrupted", "reason": "x", "saved": True})
    html = client.get(f"/jobs/{ref}").text
    assert f'action="/jobs/{ref}/resume"' in html
    r = client.post(f"/jobs/{ref}/resume", data={"note": ""})
    assert r.status_code in (302, 303)
    assert jobs.get(ADMIN, ref)["stage"] == "implementing"


# ---------- vLLM: its own thinking limit when the Anthropic one is refused ----------

def test_vllm_gets_its_own_thinking_limit_when_the_anthropic_one_is_refused():
    import httpx, json
    from runner import gateway
    from .test_085 import BODY, LOCAL, Ctx, start
    seen = []

    def vllm(req):
        d = json.loads(req.content); seen.append(d)
        return httpx.Response(422, json={"detail": "extra"}) if "thinking" in d and isinstance(d["thinking"], dict) and "budget_tokens" in d["thinking"] \
            else httpx.Response(200, json={"ok": 1})
    url, gw = start({"t": Ctx({**LOCAL, "base_url": "http://vllm:8000/v1"})}.get, httpx.MockTransport(vllm))
    for _ in range(2):
        assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json={**BODY, "max_tokens": 32000}).status_code == 200
    assert [("thinking_token_budget" in d) for d in seen] == [False, True, True]     # refused, retried with vLLM's, then direct
    assert seen[-1]["thinking_token_budget"] == config.THINKING_BUDGET
    gw.server.shutdown()


def test_a_server_refusing_both_limits_is_relayed_without_any():
    import httpx, json
    from .test_085 import BODY, LOCAL, Ctx, start
    seen = []

    def strict(req):
        d = json.loads(req.content); seen.append(d)
        return httpx.Response(422) if ("thinking_token_budget" in d or "budget_tokens" in json.dumps(d.get("thinking") or {})) \
            else httpx.Response(200, json={"ok": 1})
    url, gw = start({"t": Ctx({**LOCAL, "base_url": "http://other:8000/v1"})}.get, httpx.MockTransport(strict))
    for _ in range(2):
        assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json={**BODY, "max_tokens": 32000}).status_code == 200
    assert len(seen) == 4 and "thinking_token_budget" not in seen[-1]    # 3 tries the first time, then plain directly
    gw.server.shutdown()


# ---------- reviewers: whole points, no lockfile, a diff sized to the model, a strict second try ----------

def test_a_reviewer_point_is_no_longer_cut_at_400_characters():
    from runner import review
    long = "Must fix: " + "inject the database " * 40
    v, _, points, _ = review.parse('{"verdict": "needs_changes", "summary": "s", "points": ["%s"]}' % long)
    assert v == "needs_changes" and points[0] == long and len(long) > 400


def test_lockfiles_are_recognized_wherever_they_are():
    from runner import review
    assert review.LOCKFILE.search("package-lock.json") and review.LOCKFILE.search("web/yarn.lock") and review.LOCKFILE.search("go.sum")
    assert not review.LOCKFILE.search("src/lock.py") and not review.LOCKFILE.search("package.json")


def test_the_diff_follows_the_reviewers_context_window(monkeypatch):
    from runner import review
    monkeypatch.setattr(config, "REVIEW_MAX_DIFF", 0)
    monkeypatch.setattr(config, "REVIEW_MAX_TOKENS_SET", False)
    sizes = {"qwen": 131072, "small": 16384, "unknown": None}
    monkeypatch.setattr(review, "window", lambda m: 200000 if m.get("provider") == "anthropic" else sizes[m["id"]])
    qwen = {"id": "qwen", "provider": "openai"}
    assert review.answer_tokens(qwen) == 16384                                    # an eighth of 131k
    assert review.max_diff([qwen]) == int((131072 - 16384 - 6000) * 3 * 0.8) > 150000
    assert review.max_diff([qwen, {"id": "small"}]) == 20000                       # the smallest window rules, never below 20k
    assert review.max_diff([qwen, {"id": "unknown"}]) == review.DEFAULT_MAX_DIFF   # unknown: the former 60k
    assert review.max_diff([{"id": "claude-sonnet-5-5", "provider": "anthropic"}]) == 400000
    assert review.answer_tokens({**qwen, "review_max_tokens": 12000}) == 12000     # the provider's setting wins
    monkeypatch.setattr(config, "REVIEW_MAX_DIFF", 90000)
    assert review.max_diff([qwen]) == 90000                                        # set by hand: kept


def test_an_unreadable_review_is_asked_again_strictly_without_thinking(monkeypatch):
    from runner import review
    calls = []

    def ask(model, system, prompt, timeout, transport=None, think=True):
        calls.append((think, prompt.endswith(review.STRICT)))
        return ("Let me think... the code looks fine" if think else '{"verdict": "ok", "summary": "fine", "points": []}'), 0.0, False
    monkeypatch.setattr(review, "ask", ask)
    ctx = type("C", (), {"ref": "FAB-1", "title": "T", "job": {"request": "r"}, "repo": {}, "model": {"id": "q", "provider": "openai",
                         "type": "compatible", "base_url": "http://q/v1"}})()
    assert review.run(ctx, "code", "diff")["verdict"] == "undetermined"
    assert review.run(ctx, "code", "diff", strict=True)["verdict"] == "ok"
    assert calls == [(True, False), (False, True)]


def test_a_truncated_diff_makes_every_verdict_undetermined(monkeypatch):
    r = rmain.Runner.__new__(rmain.Runner)
    r.panel = type("P", (), {"event": lambda self, *a, **k: None})()
    r.report_usage = lambda ctx: None
    verdicts = iter([{"role": "architecture", "label": "architecture", "verdict": "needs_changes", "summary": "s",
                      "points": ["Must fix: I cannot see src/db.js"], "proof": "", "cost": 0},
                     {"role": "code", "label": "code", "verdict": "ok", "summary": "s", "points": [], "proof": "", "cost": 0}])
    r._review_once = lambda ctx, role, diff, strict=False: next(verdicts)
    r.replay = lambda ctx, cmd: (1, "")
    ctx = type("C", (), {"ref": "FAB-1", "cancelled": False, "budget": 0, "base_cost": 0, "review_limit": 150000,
                         "review_model": {"id": "q"}, "review_models": {}, "model": {"id": "q"}})()
    out = r._reviews(ctx, "abc", ["architecture", "code"], "diff", True)
    assert [o["verdict"] for o in out] == ["undetermined", "undetermined"]
    assert "150000 characters read" in out[0]["summary"] and out[0]["points"] == ["Must fix: I cannot see src/db.js"]


def test_the_size_gate_leaves_lockfiles_out(repo):
    ref, run = _claimed(repo)
    work.report(run, ref, {"type": "pr", "number": 3, "url": "http://git/pr/3", "files": ["package-lock.json", "src/app.js"],
                           "additions": 2681, "deletions": 2, "generated": 876})
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        repo_row = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        gates = work.evaluate(c, j, repo_row)
    size = next(g for g in gates["limits"] if g["key"] == "size") if isinstance(gates, dict) else next(g for g in gates if g["key"] == "size")
    assert str(size["detail"]).startswith("1807 lines out of") and "lockfiles not counted: 876 lines" in str(size["detail"])


# ---------- profiles: onboarding and translation, ready-made profiles ----------

def _profile(name, onboarding):
    from app import profiles
    with db.tx() as c:
        profiles.save(c, "test", "", name, {"agent": {"model": "claude-sonnet-5-5"}, "review": {"model": "gemma-4-26b-a4b"},
                                            "study": {"model": "claude-sonnet-5-5"}, "onboarding": {"model": onboarding}}, new=True)


def test_translation_uses_the_job_profiles_onboarding_model():
    from app import translate
    _profile("Mixed", "qwen3.8-27b")
    with db.tx() as c:
        assert translate.model_id(c, {"models": {"profile": "Mixed"}}) == ""         # not offered: no model
        db.set_setting(c, "translation", {"offer": True}, "test")
        assert translate.model_id(c, {"models": {"profile": "Mixed"}}) == "qwen3.8-27b"
        assert translate.model_id(c, {"models": {}}) == "claude-opus-5-5"             # no profile: the default model
        db.set_setting(c, "agents", {"harness": "claude-code", "model": "", "profile": "mixed"}, "test")
        assert translate.model_id(c, {"models": {}}) == "qwen3.8-27b"                 # the default profile's


def test_a_translation_model_chosen_before_keeps_translating_jobs_without_a_profile():
    from app import translate
    _profile("Mixed", "qwen3.8-27b")
    with db.tx() as c:
        db.set_setting(c, "translation", {"model": "gemma-4-26b-a4b"}, "test")       # as 0.8.9 stored it
        assert translate.offered(c)
        assert translate.model_id(c, {"models": {}}) == "gemma-4-26b-a4b"            # not silently a billed model
        assert translate.model_id(c, {"models": {"profile": "Mixed"}}) == "qwen3.8-27b"


def test_ready_made_profiles_take_the_available_models():
    from app import profiles
    with db.tx() as c:
        q = profiles.preset(c, "quality")
        e = profiles.preset(c, "economy")
        l = profiles.preset(c, "local")
        assert profiles.preset(c, "nope") is None
    assert q["roles"]["agent"]["model"] == "claude-opus-5-5" and q["roles"]["onboarding"]["model"] == "claude-haiku-5-5"
    assert e["roles"]["agent"]["model"] == "claude-haiku-5-5" and e["roles"]["review_code"]["model"] == "claude-sonnet-5-5"
    assert e["roles"]["review"]["model"] == l["roles"]["agent"]["model"] == "gemma-4-26b-a4b" and not l["missing"]


def test_the_settings_page_offers_presets_and_a_translation_checkbox():
    cl = web("admin")
    page = cl.get("/settings?tab=agents").text
    assert "Offer to translate jobs" in page and "preset=economy" in page and "Advanced: one model per reviewer" in page
    assert "Onboarding and translation" in page and "Translation model" not in page
    filled = cl.get("/settings?tab=agents&preset=quality").text
    assert 'value="Quality"' in filled
    r = cl.post("/settings/translation", data={"offer": "1"})
    assert r.status_code in (302, 303)
    with db.tx() as c:
        assert db.get_setting(c, "translation")[0] == {"offer": True}
