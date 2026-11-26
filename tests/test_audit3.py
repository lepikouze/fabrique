"""Non-regression of the security audit from 11/15/2026: one test per fixed flaw (C1 to C8) and per closed track (V3, V4)."""
import json, os, socket, stat, threading, time
import httpx
import pytest
from sqlalchemy import select
from app import auth, config, db, jobs, work
from runner import agent as ragent
from runner import config as rconf
from runner import executor, gateway, messages, review
from runner import main as rmain
from .conftest import ADMIN, REQ, web
from .test_auth import closed  # noqa: F401  (fixture)
from .test_runner import setup, step  # noqa: F401  (fixture)
from .test_work import _policy, _to_pr, api, runner  # noqa: F401  (fixture)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------- C1 : token files are never readable by all ----------

def test_bootstrap_runner_token_file_is_group_only_and_follows_rotation(tmp_path, monkeypatch):
    path = str(tmp_path / "runner-token")
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_FILE", path)
    work.bootstrap_runner()
    first = open(path).read().strip()
    assert _mode(path) == 0o640 and work.runner_for(first)["name"] == "built-in-runner"
    os.chmod(path, 0o644)                                    # file left by an old version
    work.bootstrap_runner()
    assert _mode(path) == 0o640 and open(path).read().strip() == first      # tightened, token unchanged
    with db.tx() as c:
        rid = db.one(c, select(db.runners.c.id).where(db.runners.c.name == "built-in-runner"))["id"]
    _, new = work.rotate_runner(rid, "alice")
    assert open(path).read().strip() == new != first and _mode(path) == 0o640   # the file follows the renewal
    assert work.runner_for(first) is None and work.runner_for(new)["name"] == "built-in-runner"


def test_launcher_token_file_is_owner_only(tmp_path, monkeypatch):
    from launcher import main as L
    path = str(tmp_path / "jeton")
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    monkeypatch.setenv("LAUNCHER_TOKEN_FILE", path)
    tok = L._token()
    assert len(tok) >= 32 and _mode(path) == 0o600
    os.chmod(path, 0o644)                                    # file left by an old version
    assert L._token() == tok and _mode(path) == 0o600


# ---------- C2 : never an empty session secret ----------

def test_dashboard_refuses_to_start_without_a_usable_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    good = config._secret("SESSION_SECRET")
    assert len(good) >= 48 and not os.path.exists(str(tmp_path / "secrets.json.tmp"))     # atomic write, nothing is left behind
    (tmp_path / "secrets.json").write_text("{pas du json")
    with pytest.raises(SystemExit, match="SESSION_SECRET"):
        config._secret("SESSION_SECRET")                                                   # corrupted file: refusal, no empty secret
    (tmp_path / "secrets.json").write_text("")
    assert len(config._secret("SESSION_SECRET")) >= 48                                     # empty file (failed write): regenerated
    ro = tmp_path / "lecture-seule"; ro.mkdir(); ro.chmod(0o500)
    monkeypatch.setattr(config, "DATA_DIR", str(ro))
    if os.geteuid() != 0:
        with pytest.raises(SystemExit):
            config._secret("SESSION_SECRET")                                               # directory not writable: refused
    ro.chmod(0o700)


def test_local_accounts_are_closed_when_the_session_secret_is_weak(closed, monkeypatch):  # noqa: F811
    from itsdangerous import TimestampSigner
    import base64
    auth.create_local("admin", "Admin", "Un-Mot-De-Passe-2026", "admin", "")
    cl = web()
    assert auth.mode() == "on"
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    auth.invalidate()
    assert auth.mode() == "ko"
    forged = TimestampSigner("").sign(base64.b64encode(json.dumps({"user": "admin", "iat": time.time(), "exp": time.time() + 600}).encode())).decode()
    cl.cookies.set(auth.COOKIE, forged)
    assert cl.get("/settings?tab=users", headers={"accept": "text/html"}).status_code == 503   # forged session: nothing opens
    monkeypatch.setattr(config, "SESSION_SECRET", "court")
    auth.invalidate()
    assert auth.mode() == "ko"


# ---------- C3: the "Protected paths" gate does not rely on a truncated list ----------

def test_protected_paths_fail_closed_on_what_cannot_be_compared():
    pats = ["**/.env", "**/migrations/**", ".github/**"]
    assert work.protected_hits(["src/a.py"], pats) == []
    long_env = "/".join(["d" * 60] * 5) + "/.env"
    assert len(long_env) > work.MAX_PATH and work.protected_hits([long_env[:work.MAX_PATH]], pats)      # truncated path: affected
    assert work.protected_hits(["db/migrations/0002\n.sql"], pats) and work.protected_hits([".github/workflows/x\n.yml"], pats)
    assert work.protected_hits(["a\tb.py"], pats)                                                       # control character


def test_more_files_than_the_dashboard_keeps_never_auto_merges(runner, repo):  # noqa: F811
    _policy(repo, "confident")
    a = api(runner)
    ok = jobs.create(REQ, "Petit", "B", "", repo)
    _to_pr(a, ok, ["assets/a.png"])
    assert jobs.get(ADMIN, ok)["merge_state"] == "approved"                                             # witness: everything holds
    big = jobs.create(REQ, "Gros", "B", "", repo)
    _to_pr(a, big, [f"assets/img_{i:04d}.png" for i in range(500)] + ["src/auth/login.py"], add=2)
    j = jobs.get(ADMIN, big)
    gate = next(l for l in j["decision"]["limits"] if l["name"] == "Protected paths")
    assert j["merge_state"] == "ready" and not gate["ok"] and "incomplete list" in gate["detail"]
    with db.tx() as c:
        assert not db.rows(c, select(db.intents).where(db.intents.c.kind == "merge", db.intents.c.state == "pending",
                                                       db.intents.c.job_id == j["id"]))
    nl = jobs.create(REQ, "Retour à la ligne", "B", "", repo)
    _to_pr(a, nl, ["db/migrations/0002\n.sql"])
    assert jobs.get(ADMIN, nl)["merge_state"] == "ready"


# ---------- C4 : no runner identifier in the job environment ----------

def test_no_runner_credential_ever_enters_the_job_environment(monkeypatch):
    monkeypatch.setattr(rconf, "ANTHROPIC_API_KEY", "sk-secret-du-runner")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "jeton-abonnement-du-runner")
    assert not hasattr(rconf, "CLAUDE_CODE_OAUTH_TOKEN")
    shapes = [{"id": "claude-x", "provider": "anthropic", "type": "anthropic", "key": ""},
              {"id": "claude-x", "provider": "anthropic", "type": "anthropic", "key": "sk-cle-du-dashboard"},
              {"id": "gpt-x", "provider": "openai", "type": "openai", "key": "sk-openai", "base_url": "https://api.openai.test/v1"},
              {"id": "local", "provider": "openai", "type": "compatible", "key": "", "base_url": "http://modele.test:8080/v1"}]
    for m in shapes:
        env, _ = ragent.model_env(m, "http://runner:7072", "jeton-du-job")
        blob = json.dumps(env)
        assert "sk-secret-du-runner" not in blob and "jeton-abonnement-du-runner" not in blob and "sk-cle-du-dashboard" not in blob \
            and "sk-openai" not in blob, m
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    monkeypatch.setattr(rconf, "ANTHROPIC_API_KEY", "")
    env, _ = ragent.model_env(shapes[0], "http://runner:7072", "jeton-du-job")
    assert env == {"ANTHROPIC_MODEL": "claude-x"}                                          # without key: nothing, not even a fallback


def test_anthropic_model_without_a_key_fails_before_the_agent_starts(setup, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rconf, "ANTHROPIC_API_KEY", "")
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "no API key" in j["status_text"]


# ---------- C5 / C6 : key gateway ----------

class GwCtx:
    ref = "FAB-1"

    def __init__(self, budget=0.0, base=0.0):
        self.model = {"id": "claude-haiku-4-5", "provider": "anthropic", "type": "anthropic", "key": "sk-DUMMY"}
        self.budget, self.base_cost, self.gw_spent, self.gw_mark = budget, base, 0.0, 0.0


def _gw(ctx, handler):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    gw = gateway.Gateway({"tok": ctx}.get, port, "127.0.0.1", transport=httpx.MockTransport(handler))
    gw.serve()
    return gw, port


def _raw(port, method, path):
    """Raw request: the path is sent as is (no normalization on the client side)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=b'{"model":"claude-haiku-4-5"}' if method == "POST" else None, headers={"x-api-key": "tok", "content-type": "application/json"})
    r = c.getresponse(); r.read(); c.close()
    return r.status


def test_gateway_only_relays_exact_endpoints_in_post():
    seen = []

    def up(req):
        seen.append((req.method, req.url.raw_path.decode()))
        return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})
    gw, port = _gw(GwCtx(), up)
    for method, path in [("POST", "/m/v1/files"), ("POST", "/m/v1/messages/../files"), ("GET", "/m/v1/messages/../models"),
                         ("POST", "/m/v1/messagesX/../files"), ("POST", "/m/v1/messages/batches"), ("GET", "/m/v1/messages"),
                         ("GET", "/m/v1/messages/batches/b1/results"), ("POST", "/m/v1/messages/%2e%2e/files"),
                         ("DELETE", "/m/v1/messages")]:
        assert _raw(port, method, path) == 403, (method, path)
    assert seen == []                                                                       # nothing reached the Git provider
    assert _raw(port, "POST", "/m/v1/messages?beta=true") == 200 and _raw(port, "POST", "/m/v1/messages/count_tokens") == 200
    assert seen == [("POST", "/v1/messages?beta=true"), ("POST", "/v1/messages/count_tokens")]
    gw.server.shutdown()


def test_gateway_charges_answers_without_usage_and_holds_concurrent_calls():
    ctx = GwCtx(budget=1.0)
    gw, port = _gw(ctx, lambda req: httpx.Response(200, text="x" * 4000))                  # paid response without usage counter
    assert _raw(port, "POST", "/m/v1/messages") == 200 and ctx.gw_spent > 0                 # never zero
    gw.server.shutdown()

    ctx = GwCtx(budget=1.0, base=0.99)                                                      # un centime de marge

    def slow(req):
        time.sleep(0.4)
        return httpx.Response(200, json={"usage": {"input_tokens": 200000, "output_tokens": 0}})
    gw, port = _gw(ctx, slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(_raw(port, "POST", "/m/v1/messages"))) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert out.count(200) == 1 and out.count(402) == 11 and getattr(ctx, "gw_reserved", 0) == 0   # a single pass on the last cent
    gw.server.shutdown()


def test_spend_made_during_checks_reaches_the_job_cost(setup, repo, monkeypatch):  # noqa: F811
    """A check calls the model with the job token: this expense, measured by the gateway outside of any agent run,
    must be included in the job cost (and thus in the daily budget)."""
    r, forge, panel = setup
    real = rmain.job

    def spending(ctx, cmd, **kw):
        ctx.gw_spent += 3.0                                   # what the gateway measured during this check
        return real(ctx, cmd, **kw)
    monkeypatch.setattr(rmain, "job", spending)
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["cost"] >= 3.0, j["cost"]


def test_spend_is_reported_even_when_the_job_fails(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup

    def spend_then_fail(ctx, cmd, **kw):
        ctx.gw_spent += 2.0
        raise rmain.Failed("check interrompu")
    monkeypatch.setattr(rmain, "job", spend_then_fail)
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and j["cost"] >= 2.0, j["cost"]     # reported before the order closure


def test_a_malformed_request_holds_no_budget():
    import http.client
    ctx = GwCtx(budget=1.0)
    gw, port = _gw(ctx, lambda req: httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}}))
    for bad in ("abc", "-5", str(gateway.MAX_BODY + 1)):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest("POST", "/m/v1/messages"); c.putheader("x-api-key", "tok"); c.putheader("Content-Length", bad); c.endheaders()
        assert c.getresponse().status in (400, 413), bad
        c.close()
    assert getattr(ctx, "gw_reserved", 0.0) == 0.0 and ctx.gw_spent == 0.0
    assert _raw(port, "POST", "/m/v1/messages/count_tokens") == 200 and ctx.gw_spent == 0.0   # token counting: free
    gw.server.shutdown()


# ---------- C7 : the runner's git no longer reads the agent's repo configuration ----------

class FakeDocker(executor.Local):
    """Simulated "job container" executor: same effects as the local workshop, but it logs what it is told to execute."""
    name = "docker"

    def __init__(self):
        super().__init__()
        self.argv = []

    def run(self, ctx, argv, env, cwd, timeout):
        self.argv.append(list(argv))
        return super().run(ctx, argv, env, cwd, timeout)


def test_git_on_the_agent_repository_runs_in_the_job_container(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    r.ex = FakeDocker()
    in_runner = []
    real_sh = rmain.sh

    def spy(ctx, cmd, **kw):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            in_runner.append(cmd[1])
        return real_sh(ctx, cmd, **kw)
    monkeypatch.setattr(rmain, "sh", spy)
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"
    in_job = {a[1] for a in r.ex.argv if a[0] == "git"}
    assert {"status", "bundle"} <= in_job                                 # agent repo: job side
    assert set(in_runner) <= {"clone", "remote", "checkout"}, in_runner   # runner side: only before the agent run


def test_job_side_git_environment_fits_the_launcher_limits(monkeypatch):
    from launcher import main as L
    monkeypatch.setattr(rconf, "JOB_PROXY", "http://proxy:3128")
    env = ragent.base_env("/work/FAB-1/home", rmain.job_git_env())
    assert len(env) <= 48, len(env)                                       # the launcher refuses beyond 64 variables
    assert all(L.ENV_KEY.match(k) and k not in L.FORBIDDEN_ENV and len(v) <= 20_000 for k, v in env.items())
    cfg = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert cfg["core.hooksPath"] == "/dev/null" and cfg["commit.gpgSign"] == "false" and cfg["gpg.program"] == "false"


def test_a_workspace_that_cannot_be_emptied_is_never_reused(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    first = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "pr"
    real = rmain.sh

    def stubborn(ctx, cmd, **kw):                                         # emptying has no effect (folder locked by the agent)
        if isinstance(cmd, list) and "wipe" in cmd:
            return executor.Result(0)
        return real(ctx, cmd, **kw)
    monkeypatch.setattr(rmain, "sh", stubborn)
    jobs.rereview(ADMIN, first)                                           # rebuilds the workspace of the same job
    step(r, panel)
    with db.tx() as c:
        last = db.rows(c, select(db.events).where(db.events.c.job_id == jobs.get(ADMIN, first)["id"]).order_by(db.events.c.id.desc()))[0]
    assert "cannot be emptied" in json.dumps([dict(e) for e in jobs.get(ADMIN, first)["events"]], default=str, ensure_ascii=False) \
        or "cannot be emptied" in jobs.get(ADMIN, first)["status_text"], last


def test_commit_bundle_must_be_a_regular_file(setup, repo, monkeypatch, tmp_path):  # noqa: F811
    r, forge, panel = setup
    secret = tmp_path / "ailleurs"; secret.write_text("pas un paquet")
    real = rmain.jgit

    def swap(ctx, args, **kw):
        p = real(ctx, args, **kw)
        if args[0] == "bundle":                                           # a job process replaces the package with a link
            path = args[3]
            os.remove(path); os.symlink(str(secret), path)
        return p
    monkeypatch.setattr(rmain, "jgit", swap)
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "commit bundle unreadable" in j["status_text"]


# ---------- C8: the reviewer in agent mode must owe nothing to the job it reviews ----------

def test_agent_mode_reviewer_uses_system_path_fresh_home_and_pinned_export(monkeypatch, tmp_path):
    monkeypatch.setattr(rconf, "REVIEW_MODE", "agent")
    monkeypatch.setattr(rconf, "ANTHROPIC_API_KEY", "sk-factice")
    calls = []

    class Ex:
        def gateway_url(self):
            return "http://runner:7072"

        def run(self, ctx, argv, env, cwd, timeout):
            calls.append((argv, env, cwd))
            return executor.Result(0, json.dumps({"result": json.dumps({"verdict": "ok", "resume": "r", "points": []}), "total_cost_usd": 0.1}))

    class C:
        ref, title, fab_token = "FAB-1", "T", "jeton-du-job"
        job = {"request": "B", "done_when": ""}
        model = {"id": "claude-x", "provider": "anthropic", "type": "anthropic", "key": ""}
        home, repo_dir = str(tmp_path / "home"), str(tmp_path / "repo")
        review_dir, review_home = str(tmp_path / ".review-abc"), str(tmp_path / ".review-home-abc")
        ex = Ex()
    out = review.run(C, "code", "diff")
    argv, env, cwd = calls[0]
    assert out["verdict"] == "ok" and os.path.isabs(argv[0]) and argv[0] == rconf.CLAUDE_BIN
    assert env["PATH"] == review.REVIEW_PATH and C.home not in env["PATH"]                 # no job folder in the PATH
    assert env["HOME"] == C.review_home and cwd == C.review_dir and env["FAB_WORKSPACE"] == C.review_dir
    assert os.path.isabs(rconf.CODEX_BIN)


def test_reviewer_reads_an_export_of_the_verified_commit(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    seen = {}
    real = review.run

    def spy(ctx, role, diff, strict=False):
        seen[role] = (ctx.review_dir, ctx.review_home, sorted(os.listdir(ctx.review_dir)),
                      os.path.exists(os.path.join(ctx.review_dir, "brouillon-du-job.txt")))
        return real(ctx, role, diff)
    monkeypatch.setattr(review, "run", spy)
    real_checks = r.checks

    def checks(ctx):   # a check leaves an uncommitted file in the working tree
        out = real_checks(ctx)
        open(os.path.join(ctx.repo_dir, "brouillon-du-job.txt"), "w").write("x")
        return out
    r.checks = checks
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"
    rdir, rhome, files, leaked = seen["code"]
    assert "notebook.py" in files and not leaked and ".git" not in files            # the verified commit, not the working tree
    assert not {".claude", ".mcp.json", "CLAUDE.md"} & set(files)                 # neither the project settings of the judged commit
    assert os.path.basename(rdir).startswith(".review-") and not os.path.exists(rdir) and not os.path.exists(rhome)   # cleaned


def test_nothing_survives_a_replayed_proof(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    order = []
    real_job, real_kill = rmain.job, r.ex.kill
    monkeypatch.setattr(rmain, "job", lambda ctx, cmd, **kw: (order.append("job:" + cmd), real_job(ctx, cmd, **kw))[1])
    r.ex.kill = lambda ctx: (order.append("kill"), real_kill(ctx))[1]
    ref = jobs.create(REQ, "Réparer BLOQUER", "B", "", repo)               # a reviewer blocks, with evidence to replay
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"
    proofs = [i for i, e in enumerate(order) if e.startswith("job:grep")]
    assert proofs and all(order[i + 1] == "kill" for i in proofs), order     # each replayed evidence is followed by a process shutdown
    first_check = next(i for i, e in enumerate(order) if e.startswith("job:python3"))
    assert "kill" in order[first_check:]                                     # and the check processes are stopped before the review


# ---------- V3: an "ok" on a truncated diff does not apply to the whole ----------

def test_ok_on_a_truncated_diff_is_not_an_ok(setup, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rconf, "REVIEW_MAX_DIFF", 40)
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    latest = {rv["role"]: rv for rv in j["reviews"] if rv["sha"] == j["verified_sha"]}
    assert {v["verdict"] for v in latest.values()} == {"undetermined"} and "review incomplete" in latest["code"]["summary"]
    assert not {l["name"]: l["ok"] for l in j["decision"]["limits"]}["Review"]


# ---------- V4: no form of link survives in the inert rendered text ----------

@pytest.mark.parametrize("text", ["[Voir les journaux][1]\n\n[1]: //evil.example/login", "[label]\n\n[label]: /autre/depot/pulls/1",
                                  "voir www.evil.example/login", "&commat;admin &num;12 https&colon;//evil.example",
                                  "[x](https://evil.example) @admin #12 ![i](u) <b>"])
def test_inert_leaves_no_link_form(text):
    out = messages.inert(text)
    for live in ("][", "]:", "](", "www.", "://", "@a", "#1", "![", "<", "&commat;", "&num;", "&colon;"):
        assert live not in out, (live, out)
    assert messages.quote(text).startswith("> ")
