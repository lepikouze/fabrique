"""0.8.7: what the first real jobs taught (FAB-22 to FAB-28) — stop that really stops, changes asked on a PR, a refused PR
closed on the Git host, the turn limit that resumes, reviewers that sort their points and one model per reviewer, existing
tests flagged, right prices and a live cost that never goes down, a thinking budget per provider, background translation."""
import json, socket, time
import httpx, pytest
from sqlalchemy import insert, select, update
from app import db, jobs, prices, profiles, providers, work
from runner import agent, config, forge, gateway, review
from runner import main as rmain
from .conftest import ADMIN, REQ, REVIEWER


def _set(ref, **vals):
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(**vals))


def _review(ref, role, verdict, points, sha="abc1234"):
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(insert(db.reviews).values(job_id=jid, sha=sha, role=role, verdict=verdict, summary=f"{role} summary",
                                            points=points, model="m", at=time.time()))
    return jid


def _intents(jid):
    with db.tx() as c:
        return [(i["kind"], i["payload"]) for i in db.rows(c, select(db.intents).where(db.intents.c.job_id == jid).order_by(db.intents.c.id))]


# ---------- changes asked, refusal that closes the PR ----------

def test_asking_for_changes_sends_the_reviews_and_the_note_back_to_the_agent(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234", pr_number=7)
    jid = _review(ref, "security", "needs_changes", ["Must fix: the address keeps user:password@"])
    _review(ref, "code", "ok", ["Later: a docstring"])
    jobs.decide(REVIEWER, ref, "changes", "also strip the token from the log")
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "implementing" and j["merge_state"] == "" and "Changes requested" in j["status_text"]
    kind, payload = _intents(jid)[-1]
    assert kind == "fix" and payload["note"] == "also strip the token from the log" and payload["by"]
    assert [r["role"] for r in payload["reviews"]] == ["security"]          # only the reviews that ask for something
    assert j["audit"][0]["action"] == "Changes requested"


def test_changes_need_something_to_change(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234", pr_number=7)
    _review(ref, "code", "ok", [])
    with pytest.raises(jobs.Refused, match="Say what to change"):
        jobs.decide(REVIEWER, ref, "changes", "")


def test_a_refused_merge_closes_the_pr_on_the_git_host(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234", pr_number=7)
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
    jobs.decide(REVIEWER, ref, "refuse", "superseded by PR 8")
    assert jobs.get(ADMIN, ref)["merge_state"] == "refused"
    assert _intents(jid)[-1] == ("close", {"note": "superseded by PR 8"})


def test_close_and_merge_pass_when_the_daily_budget_is_reached(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234", pr_number=7)
    jobs.decide(REVIEWER, ref, "refuse", "")
    with db.tx() as c:
        lim, _ = db.get_setting(c, "limits")
        db.set_setting(c, "limits", {**lim, "budget_day": 0.01}, "test")
        c.execute(update(db.jobs).values(cost=1.0, updated_at=time.time()))
        c.execute(update(db.intents).where(db.intents.c.kind == "start").values(state="done"))
    work.create_runner("r1", "container", "alice")
    with db.tx() as c:
        run = db.one(c, select(db.runners).where(db.runners.c.name == "r1"))
    got = work.claim(run)
    assert got and got["intent"]["kind"] == "close"


def test_forgejo_closes_a_pr_with_its_reason():
    seen = []

    def api(req):
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        return httpx.Response(201 if req.method == "POST" else 200, json={})
    f = forge.Forgejo("https://git.example", "tok", transport=httpx.MockTransport(api))
    f.close_pr("team/app", 7, "Closed without merging.")
    assert seen == [("POST", "/api/v1/repos/team/app/issues/7/comments", {"body": "Closed without merging."}),
                    ("PATCH", "/api/v1/repos/team/app/pulls/7", {"state": "closed"})]


def test_gitlab_closes_an_mr():
    seen = []
    g = forge.GitLab("https://gl.example", "tok", transport=httpx.MockTransport(
        lambda req: seen.append((req.method, req.url.raw_path.decode(), json.loads(req.content or b"{}"))) or httpx.Response(200, json={})))
    g.close_pr("grp/app", 3, "")
    assert seen == [("PUT", "/api/v4/projects/grp%2Fapp/merge_requests/3", {"state_event": "close"})]


# ---------- the stop really stops ----------

def test_the_launcher_stops_with_the_shells_builtin_kill():
    from launcher import main as launcher
    assert launcher.KILL_ALL == ["sh", "-c", "kill -9 -1"]    # job images have no kill program


# ---------- turn limit, live cost ----------

def test_turn_limit_resumes_the_session_then_fails_with_its_cause(monkeypatch):
    calls = []

    class Run:
        def __init__(self, ctx, panel, prompt, resume=None, system=None):
            self.ctx, self.prompt, self.resume = ctx, prompt, resume
            self.cost = self.tokens = 0
            self.session_id, self.cancelled, self.stalled, self.result = "sess", False, False, ""
            self.error = "the agent stopped on an error: error_max_turns"

        def run(self):
            calls.append(self.prompt)
            return self

    class Panel:
        def event(self, *a, **k):
            calls.append(k.get("message")) if k.get("kind") == "runner" else None

    monkeypatch.setattr(rmain, "runner_for", lambda ctx: Run)
    r = rmain.Runner.__new__(rmain.Runner)
    r.panel, r.ex, r.gateway = Panel(), type("E", (), {"kill": lambda *a: None})(), type("G", (), {"lock": __import__("threading").Lock()})()
    r.settle = r.report_usage = lambda *a: None
    item = {"intent": {"kind": "start", "payload": {}}, "model": {"id": "m"},
            "job": {"ref": "FAB-1", "title": "T", "branch": "b", "session_id": "sess", "request": "x"}, "repo": {"slug": "t/r"}}
    ctx = rmain.Ctx(item)
    ctx.ex = r.ex
    with pytest.raises(rmain.Failed, match="did not finish after 3 runs"):
        r.agent(ctx, "go")
    logs = [c for c in calls if isinstance(c, str) and c.startswith("turn limit of the agent")]
    assert len(logs) == 2 and ctx.turn_resumes == config.MAX_TURN_RESUMES


def test_live_usage_is_counted_once_per_model_response_and_never_goes_down():
    shown = []
    ctx = type("C", (), {"ref": "FAB-1", "model": {"id": "claude-sonnet-5-5"}, "base_cost": 0.0, "base_tokens": 0, "title": "T",
                          "budget": 0, "repo": {}, "stages": set(), "repo_dir": "/tmp"})()
    run = agent.Run(ctx, type("P", (), {"event": lambda self, *a, **k: shown.append(k.get("cost")) if k.get("type") == "usage" else None})(), "go")
    run.local, run._last_usage = False, time.time()
    use = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for block in ("thinking", "text", "tool_use"):    # one line per content block, the same usage repeated
        run._line(json.dumps({"type": "assistant", "message": {"id": "msg_1", "usage": use, "content": [{"type": block}]}}))
    assert run.tokens == 1010 and abs(run.cost - (10 * 2 + 1000 * 10) / 1e6) < 1e-9
    run._usage()
    run.cost = 0.001                                  # the provider's total corrects the estimate downwards
    run._usage()
    assert shown[-1] == shown[-2]                     # the figure shown does not go down during the run


# ---------- prices ----------

def test_prices_order_manual_list_table_unknown(monkeypatch):
    with db.tx() as c:
        db.set_setting(c, "prices_table", {"claude-new-9": {"input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5}}, "t")
        db.set_setting(c, "prices", {"at": 1.0, "count": 1}, "t")
        assert prices.resolve(c, "claude-new-9") == [3.0, 15.0, 0.3]                      # public list (per token there)
        assert prices.resolve(c, "claude-haiku-5-5") == [0.1, 0.5, 0.01]                   # built-in table, Haiku 5.5 (not 4.5)
        assert prices.resolve(c, "claude-haiku-5-5", {"price": [1, 2, 3]}) == [1, 2, 3]   # entered by hand
        assert prices.resolve(c, "mystery-model") is None                                 # unknown, said as such
        assert prices.resolve(c, "gemma", local=True) == [0.0, 0.0, 0.0]
    assert prices.parse_manual("2 / 10 / 0,1") == [2.0, 10.0, 0.1] and prices.parse_manual("4 20") == [4.0, 20.0, 0.4]
    assert prices.parse_manual("") is None
    with pytest.raises(ValueError):
        prices.parse_manual("cheap")


def test_price_list_is_fetched_and_kept_small(monkeypatch):
    monkeypatch.setattr(prices, "URL", "https://prices.example/list.json")
    raw = {"claude-x": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6, "mode": "chat", "max_tokens": 9},
           "sample_spec": {"note": "no price"}}
    assert prices.refresh(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=raw)), now=100.0)
    with db.tx() as c:
        assert prices.fetched(c) == {"at": 100.0, "source": "https://prices.example/list.json", "count": 1}
        assert prices.resolve(c, "claude-x") == [1.0, 5.0, 0.1]
        assert not prices.due(c, now=100.0 + 3600) and prices.due(c, now=100.0 + prices.EVERY)
    assert not prices.refresh(transport=httpx.MockTransport(lambda req: httpx.Response(500)))   # a failure keeps the old list


def test_the_runner_uses_the_price_sent_with_the_model():
    assert config.price("claude-haiku-5-5") == (0.1, 0.5, 0.01)
    assert config.price("claude-haiku-5-5", {"price": [9, 9, 9]}) == (9.0, 9.0, 9.0)
    assert config.price_known("claude-opus-5-5") and not config.price_known("mystery") and config.price_known("mystery", {"price": [1, 1, 0]})


def test_a_manual_price_is_kept_on_the_model(repo):
    with db.tx() as c:
        pid = providers.listing(c)[0]["id"] if providers.listing(c) else None
    if not pid:
        pytest.skip("no provider in the test data")
    with db.tx() as c:
        keep = [{"id": m["id"], "name": m["name"]} for m in providers.models_with_provider(c) if m["provider_id"] == pid and m["id"] != "claude-sonnet-5-5"]
        providers.enable(c, "t", pid, keep + [{"id": "claude-sonnet-5-5", "name": "Sonnet", "price": "2 / 10 / 0.1"}])
        m = next(x for x in providers.models_with_provider(c) if x["id"] == "claude-sonnet-5-5")
        assert m["price"] == [2.0, 10.0, 0.1] and providers.runtime(c, "claude-sonnet-5-5")["price"] == [2.0, 10.0, 0.1]
        with pytest.raises(providers.Invalid, match="Price of"):
            providers.enable(c, "t", pid, keep + [{"id": "claude-sonnet-5-5", "price": "cheap"}])


# ---------- thinking budget per provider ----------

def test_a_providers_thinking_budget_wins_over_the_runners(monkeypatch):
    seen = []
    model = {"id": "qwen", "provider": "openai", "type": "compatible", "base_url": "http://llm:8080/v1", "key": "", "thinking_budget": 2000}
    ctx = type("C", (), {"ref": "FAB-1", "model": model})()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    gw = gateway.Gateway({"t": ctx}.get, port, "127.0.0.1",
                         transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={})))
    gw.serve()
    httpx.post(f"http://127.0.0.1:{port}/m/v1/messages", headers={"x-api-key": "t"},
               json={"model": "qwen", "max_tokens": 32000, "messages": [{"role": "user", "content": "x"}]})
    model["thinking_budget"] = 0
    httpx.post(f"http://127.0.0.1:{port}/m/v1/messages", headers={"x-api-key": "t"},
               json={"model": "qwen", "max_tokens": 32000, "messages": [{"role": "user", "content": "x"}]})
    gw.server.shutdown()
    assert seen[0]["thinking"] == {"type": "enabled", "budget_tokens": 2000} and "thinking" not in seen[1]


def test_thinking_budget_is_validated_and_kept(repo):
    with db.tx() as c:
        providers.save(c, "t", "local-s1", "compatible", "S1", "http://llm:8080/v1", "", thinking="3000")
        assert next(p for p in providers.listing(c) if p["id"] == "local-s1")["thinking"] == 3000
        providers.save(c, "t", "local-s1", "compatible", "S1", "http://llm:8080/v1", "")            # field missing: kept
        assert next(p for p in providers.listing(c) if p["id"] == "local-s1")["thinking"] == 3000
        with pytest.raises(providers.Invalid, match="Thinking budget"):
            providers.save(c, "t", "local-s1", "compatible", "S1", "http://llm:8080/v1", "", thinking="lots")


# ---------- reviewers ----------

def test_the_verdict_follows_the_labels_of_the_points():
    v = review.parse('{"verdict": "needs_changes", "summary": "s", "points": ["Later: a docstring", "Later: naming"]}')
    assert v[0] == "ok"                                                   # nothing to fix before merging
    v = review.parse('{"verdict": "ok", "summary": "s", "points": ["Must fix: user:password@ is shown to others"]}')
    assert v[0] == "needs_changes"
    v = review.parse('{"verdict": "needs_changes", "summary": "s", "points": ["no label at all"]}')
    assert v[0] == "needs_changes"                                        # an old-style answer keeps its verdict


def test_a_profile_can_give_each_reviewer_its_model(repo):
    with db.tx() as c:
        models = [m["id"] for m in providers.models_with_provider(c)]
    if len(models) < 2:
        pytest.skip("two models are needed")
    a, b = models[0], models[1]
    with db.tx() as c:
        profiles.save(c, "t", "", "Mixed", {"agent": {"model": a}, "review": {"model": a}, "study": {"model": a}, "onboarding": {"model": a},
                                            "review_security": {"model": b}}, new=True)
        pid = profiles.listing(c)[0]["id"]
        model, extra = profiles.resolve(c, {}, "form", "", pid)
    assert model == a and extra["review_models"] == {"security": b}


def test_a_reviewer_uses_its_own_model_when_the_profile_gives_one():
    ctx = type("C", (), {"model": {"id": "agent"}, "review_model": {"id": "rev"}, "review_models": {"security": {"id": "sec"}}})()
    assert review.reviewer(ctx, "security")["id"] == "sec" and review.reviewer(ctx, "code")["id"] == "rev"


def test_modified_existing_tests_are_recognised():
    t = rmain.TEST_FILE
    for f in ("tests/test_home.py", "app/tests/x.py", "src/foo.test.ts", "pkg/foo_test.go", "web/__tests__/a.js", "src/FooTest.java"):
        assert t.search(f), f
    for f in ("app/main.py", "docs/testing.md", "attest.py"):
        assert not t.search(f), f


def test_existing_tests_modified_is_a_gate_and_a_point_to_check(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234",
         diff={"files": ["tests/test_home.py"], "additions": 3, "deletions": 2, "truncated": False, "tests_changed": ["tests/test_home.py"]})
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        repo_row = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        gates = work.evaluate(c, j, repo_row)
    g = next(x for x in gates if x["key"] == "tests")
    assert not g["ok"] and "tests/test_home.py" in g["detail"]
    with db.tx() as c:
        c.execute(insert(db.decisions).values(job_id=j["id"], policy="off", at=1.0, confident=True, limits=gates))
    checks = jobs.to_check(jobs.get(ADMIN, ref))
    assert any(k["title"] == "Existing tests modified" for k in checks)


def test_review_points_to_check_stay_separate_for_translation(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    _set(ref, stage="pr", merge_state="ready", verified_sha="abc1234")
    jid = _review(ref, "security", "needs_changes", ["Must fix: one", "Later: two"])
    with db.tx() as c:
        c.execute(insert(db.decisions).values(job_id=jid, policy="off", at=1.0, confident=True,
                                              limits=[{"key": "review", "name": "Review", "ok": False, "detail": ""}]))
    k = next(x for x in jobs.to_check(jobs.get(ADMIN, ref)) if "security" in x["title"])
    assert k["parts"] == ["Must fix: one"] and k["text"] == ""          # each text as written; "Later" points are not to check
