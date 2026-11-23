"""Runner: guard hook, relay, and a full cycle (start, question, answer, checks, PR, merge) with a fake
Claude Code and a local Git repo, against the real dashboard (TestClient)."""
import json, os, socket, subprocess, sys
import pytest
from sqlalchemy import select, update
from app import db, jobs, work
from runner import config as rconf
from runner import main as rmain
from runner.panel import Panel
from .conftest import ADMIN, REQ, REVIEWER, web

HERE = os.path.dirname(__file__)
GUARD = os.path.join(HERE, "..", "runner", "guard.py")


def guard(tool, inp, cwd="/work/FAB-1/repo"):
    p = subprocess.run([sys.executable, GUARD], input=json.dumps({"tool_name": tool, "tool_input": inp, "cwd": cwd}),
                       capture_output=True, text=True, env={"FAB_WORKSPACE": cwd, "PATH": os.environ["PATH"]})
    return p.returncode


@pytest.mark.parametrize("cmd,blocked", [("git push origin main", True), ("git remote -v", True), ("git config user.email x", True),
                                         ("curl http://10.0.0.1", True), ("printenv", True), ("cat /proc/1/environ", True),
                                         ("echo $ANTHROPIC_API_KEY", True), ("sudo ls", True), ("git commit -am x", False),
                                         (".venv/bin/python -m pytest -q", False), ("python3 -m venv .venv", False), ("git diff HEAD~1", False)])
def test_guard_bash(cmd, blocked):
    assert (guard("Bash", {"command": cmd}) == 2) == blocked


@pytest.mark.parametrize("tool,path,blocked", [("Read", "/work/FAB-1/repo/a.py", False), ("Read", "a.py", False), ("Read", "/etc/passwd", True),
                                               ("Edit", "../../FAB-2/repo/a.py", True), ("Write", ".git/config", True), ("Read", ".git/HEAD", False),
                                               ("WebFetch", "", True)])
def test_guard_files(tool, path, blocked):
    assert (guard(tool, {"file_path": path, "url": "x"}) == 2) == blocked


# ---------- cycle complet ----------

def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class FakeForge:
    def __init__(self, bare):
        self.bare, self.prs, self.merged = bare, {}, []

    def clone_url(self, slug):
        return self.bare

    def git_env(self):
        return {}

    def pr_url(self, slug, n):
        return f"https://git.test/{slug}/pulls/{n}"

    def open_pr(self, slug, head, base, title, body):
        n = next((k for k, v in self.prs.items() if v["head"] == head), len(self.prs) + 1)    # one PR per branch, like a real Git host
        self.prs[n] = {"head": head, "title": title, "body": body}
        return n

    def pr(self, slug, n):
        sha = subprocess.run(["git", "rev-parse", self.prs[n]["head"]], cwd=self.bare, capture_output=True, text=True).stdout.strip()
        return {"head": {"sha": sha}}

    def merge(self, slug, n, sha, title, message=""):
        self.merged.append((n, sha))
        self.merge_message = message
        return "deadbeefcafe"

    def upsert_comment(self, slug, n, body):
        self.comments = getattr(self, "comments", {})
        self.comments[n] = body


@pytest.fixture
def setup(tmp_path, monkeypatch, repo):
    src = tmp_path / "src"; src.mkdir()
    (src / ".fabrique").mkdir()
    (src / ".fabrique" / "setup.json").write_text(json.dumps({"setup": [], "checks": {"tests": "python3 -c 'import notebook; assert notebook.ok()'"}}))
    (src / "notebook.py").write_text("def ok():\n    return False\n")
    _git("init", "-q", "-b", "main", cwd=src); _git("add", "-A", cwd=src); _git("commit", "-qm", "init", cwd=src)
    bare = str(tmp_path / "essai.git")
    _git("clone", "-q", "--bare", str(src), bare, cwd=tmp_path)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    # Dummy key: the model goes through the key gateway (an Anthropic model without a key is refused); reviewers in agent mode
    # (the fake claude), since direct mode would call the provider.
    for k, v in dict(WORK_DIR=str(tmp_path / "work"), CLAUDE_BIN=os.path.join(HERE, "fake", "claude"), RELAY_PORT=port, AGENT_UID="",
                     FIX_ATTEMPTS=1, ANTHROPIC_API_KEY="sk-factice-de-test", REVIEW_MODE="agent").items():
        monkeypatch.setattr(rconf, k, v)
    tok = work.create_runner("runner-e2e", "container", "alice")
    panel = Panel.__new__(Panel)
    panel.h = web(); panel.h.headers["Authorization"] = f"Bearer {tok}"
    forge = FakeForge(bare)
    r = rmain.Runner(panel=panel, forge=forge)
    r.relay.serve()
    os.makedirs(rconf.WORK_DIR, exist_ok=True)
    return r, forge, panel


def step(r, panel):
    item = panel.claim()
    assert item, "rien dans la file"
    r.active += 1
    r.execute(item)
    return item


def test_full_cycle_question_pr_merge(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok() QUESTION", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "waiting" and j["questions"][0]["question"] == "A ou B ?" and j["session_id"] == "sess-42"
    jobs.answer(REQ, ref, "B")
    item = step(r, panel)
    assert item["intent"]["kind"] == "resume"
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and j["pr_number"] == 1
    assert j["evidence"] and all(e["ok"] for e in j["evidence"]) and j["evidence"][0]["ran_by"] == "runner"
    assert j["decision"]["confident"] and j["diff"]["files"] == ["notebook.py"]
    assert j["cost"] > 0 and j["agents"][0]["name"] == "main session"
    assert any(e["kind"] == "edit" for e in j["events"])
    # the branch was pushed by the runner (not by the agent)
    assert subprocess.run(["git", "rev-parse", "--verify", f"fabrique/{ref}"], cwd=forge.bare, capture_output=True).returncode == 0
    body = forge.prs[1]["body"]                                               # review guide in the PR
    assert "### For the reviewer" in body and "**Review effort: 1/5 (trivial)** · 1 file(s)" in body
    assert "**Where to start**: `notebook.py`" in body and "**Reviews**:" in body
    jobs.decide(REVIEWER, ref, "approve")
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "merged" and forge.merged and forge.merged[0][1].startswith(j["verified_sha"])
    assert forge.merge_message == f"Fabrique-Job: {ref}"                      # the merge commit refers back to its job


def test_failing_checks_get_one_fix_then_pr(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "CASSER puis réparer", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["attempts"] == 2
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert lim["Checks green"] and not lim["First attempt"]


def test_merge_refused_if_pr_moved(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    step(r, panel)
    with db.tx() as c:
        c.execute(update(db.jobs).values(verified_sha="0000000"))
    jobs.decide(REVIEWER, ref, "approve")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "changed" in j["status_text"] and not forge.merged


def test_missing_recipe_fails_cleanly(setup, repo, tmp_path):
    r, forge, panel = setup
    src = tmp_path / "src"
    _git("rm", "-q", ".fabrique/setup.json", cwd=src); _git("commit", "-qm", "sans recipe", cwd=src)
    _git("push", "-q", forge.bare, "main", cwd=src)
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    step(r, panel)
    assert "recipe" in jobs.get(ADMIN, ref)["status_text"]


def test_reviews_run_and_a_blocking_review_gets_one_fix_round(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer BLOQUER", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["attempts"] == 2
    latest = {rv["role"]: rv["verdict"] for rv in j["reviews"] if rv["sha"] == j["verified_sha"]}
    assert latest == {"code": "ok", "security": "ok"}
    assert any(rv["verdict"] == "blocking" for rv in j["reviews"])          # the first review remains in the history
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert lim["Review"] and not lim["First attempt"]
    assert "Fabrique's decision" in forge.comments[1] and "Review" in forge.comments[1]


@pytest.mark.parametrize("title", ["Réparer SANSPREUVE", "Réparer PREUVEVIDE"])
def test_unproven_blocking_review_is_downgraded_without_fix_round(setup, repo, title):
    r, forge, panel = setup
    ref = jobs.create(REQ, title, "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["attempts"] == 1                            # pas de ronde de correction
    verdicts = {rv["verdict"] for rv in j["reviews"]}
    assert verdicts == {"needs_changes"}                                              # neither blocking nor "ok"
    assert all("not demonstrated" in rv["summary"] for rv in j["reviews"])
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert not lim["Review"]                                                 # merge alone remains impossible


def test_unreadable_review_is_retried_once(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ILLISIBLE", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert {rv["verdict"] for rv in j["reviews"]} == {"ok"}
    assert any("; retrying" in (e["message"] or "") for e in j["events"])


def test_unreadable_review_says_why_and_can_be_rerun(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ILLISIBLE TOUJOURS", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["merge_state"] == "ready" and {rv["verdict"] for rv in j["reviews"]} == {"undetermined"}
    assert all("no JSON verdict" in rv["summary"] and "not a negative opinion" in rv["summary"] for rv in j["reviews"])
    todo = jobs.to_check(j)
    assert any("No verdict from the code review" == t["title"] and "correctness" in t["text"] for t in todo)
    for url in (f"/inbox/{ref}/panel", f"/jobs/{ref}"):
        html = web("admin").get(url).text
        assert "To check before approving" in html, url
        assert "Rerun the review" in html and f"/jobs/{ref}/reviews" in html
    assert f'data-copy="#ev-{ref}"' in web("admin").get(f"/jobs/{ref}").text                 # "copy log" button
    with pytest.raises(jobs.Refused):
        jobs.rereview(REQ, ref)                                                  # a requester does not restart
    jobs.rereview(REVIEWER, ref)
    assert jobs.get(ADMIN, ref)["merge_state"] == "reviewing"
    with pytest.raises(jobs.Refused):
        jobs.decide(REVIEWER, ref, "approve")                                        # no decision during the review
    with db.tx() as c:                                                           # the request no longer contains the magic word
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(title="Réparer"))
    attempts = jobs.get(ADMIN, ref)["attempts"]
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and j["attempts"] == attempts
    cur = {rv["role"]: rv["verdict"] for rv in j["reviews"] if rv["sha"] == j["verified_sha"]}
    assert cur == {"code": "ok", "security": "ok"}
    assert {l["name"]: l["ok"] for l in j["decision"]["limits"]}["Review"]
    assert "Review: code: OK" in forge.comments[j["pr_number"]] or "✅ **Review**" in forge.comments[j["pr_number"]]


def test_agent_gives_up_cleanly(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "ABANDON", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "chaîne d'outils" in j["status_text"]


def test_watchdog_nudges_a_silent_agent_once(setup, repo, monkeypatch):
    monkeypatch.setattr(rconf, "WATCHDOG_SECONDS", 2)
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer MUET", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and any(e["kind"] == "watchdog" for e in j["events"])


def test_stop_from_the_panel_kills_the_agent(setup, repo):
    import threading
    r, forge, panel = setup
    ref = jobs.create(REQ, "Repair LONG", "B", "", repo)
    item = panel.claim()
    jobs.cancel(REVIEWER, ref)
    threading.Timer(2, lambda: [r.cancel(x) for x in panel.heartbeat({}).get("cancel", [])]).start()
    r.active += 1
    r.execute(item)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "Stopped by Vera" in j["status_text"]


def test_daily_budget_pauses_the_agent_and_the_job_resumes_where_it_was(setup, repo):
    import threading
    from sqlalchemy import select, update
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer LONG", "B", "", repo)
    item = panel.claim()
    with db.tx() as c:
        db.set_setting(c, "limits", dict(db.DEFAULTS["limits"], budget_day=1.0), "t")
    cher = jobs.create(REQ, "Cher", "B", "", repo)                                         # another job ate the daily budget
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == cher).values(cost=1.5, stage="merged"))
        c.execute(update(db.intents).where(db.intents.c.state == "pending").values(state="done"))
    marker = os.path.join(rconf.WORK_DIR, ref, "repo", "brouillon.txt")
    threading.Timer(1, lambda: open(marker, "w").write("travail en cours")).start()       # work not yet committed
    threading.Timer(2, lambda: r.orders(panel.heartbeat({}))).start()
    r.active += 1
    r.execute(item)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] != "failed" and j["status_text"] == work.PAUSED and j["session_id"] == "sess-42"
    with db.tx() as c:
        its = db.rows(c, select(db.intents).where(db.intents.c.state == "pending"))
    assert [(i["kind"], i["payload"]) for i in its] == [("resume", {"pause": "budget"})]
    assert panel.claim() is None and os.path.exists(marker)                                # budget still exceeded: the resume waits
    with db.tx() as c:
        db.set_setting(c, "limits", dict(db.DEFAULTS["limits"], budget_day=50.0), "t")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and os.path.exists(marker)                                   # resume on the same workspace


def test_study_renders_a_report_and_pushes_nothing(setup, repo):
    r, forge, panel = setup
    heads = lambda: subprocess.run(["git", "for-each-ref"], cwd=forge.bare, capture_output=True, text=True).stdout
    before = heads()
    ref = jobs.create(REQ, "Pourquoi ok() est faux ?", "Explique", "", repo, study=True)
    item = step(r, panel)
    assert item["job"]["source"] == "study"
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "done" and j["status_text"] == "Study delivered" and "ok() renvoie False" in j["report"]
    assert heads() == before and not forge.prs and not j["pr_number"] and not j["evidence"]     # no branch, no PR, no checks
    assert [t["stage"] for t in j["timeline"]] == ["triage", "investigating", "done"]
    page = web("admin").get(f"/jobs?job={ref}").text
    assert "Study report" in page and "&lt;script&gt;" in page and "<script>alert(1)" not in page   # inert agent text
    assert f"/new?from={ref}" in page
    form = web("admin").get(f"/new?from={ref}").text
    assert f"Follow-up to {ref}: " in form and "Conclusions of study" in form and "ok() renvoie False" in form
    assert "Follow-up to" not in web("admin").get("/new?from=FAB-999").text
    assert "Follow-up to" not in web("requester").get(f"/new?from={ref}").text                  # another study remains invisible
    with pytest.raises(work.Denied):                                                        # an ordinary job does not return a report
        other = jobs.create(REQ, "Réparer LONG", "B", "", repo)
        panel.claim()
        work.report(_runner_row(), other, {"type": "study", "report": "x"})


def test_study_question_then_report_and_missing_report_fails(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Étudier QUESTION", "Quoi ?", "", repo, study=True)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "waiting"
    jobs.answer(REQ, ref, "Tout le module")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "done" and j["report"]
    ref2 = jobs.create(REQ, "Étudier SANSRAPPORT", "Quoi ?", "", repo, study=True)
    step(r, panel)
    j2 = jobs.get(ADMIN, ref2)
    assert j2["stage"] == "failed" and "no study report" in j2["status_text"]


def _runner_row():
    with db.tx() as c:
        return db.one(c, select(db.runners).where(db.runners.c.name == "runner-e2e"))


def _drop_recipe(forge, tmp_path):
    w = tmp_path / "drop"
    _git("clone", "-q", forge.bare, str(w), cwd=tmp_path)
    _git("rm", "-q", "-r", ".fabrique", cwd=w)
    _git("commit", "-qm", "sans recipe", cwd=w)
    _git("push", "-q", "origin", "main", cwd=w)


def _new_repo(slug, onboarding="pending"):
    import time
    from sqlalchemy import insert
    with db.tx() as c:
        return c.execute(insert(db.repos).values(provider="forgejo", slug=slug, name=slug, onboarding=onboarding,
                                                 settings={"merge_policy": "confident"}, created_at=time.time())).inserted_primary_key[0]


def test_onboarding_writes_recipe_runner_replays_it_and_human_merges(setup, tmp_path):
    r, forge, panel = setup
    _drop_recipe(forge, tmp_path)
    rid = _new_repo("alice/nouveau")
    with pytest.raises(jobs.Refused):
        jobs.create(REQ, "Réparer", "B", "", rid)                                 # no job before onboarding
    with pytest.raises(jobs.Refused):
        jobs.onboard(REVIEWER, rid)                                                  # admin only
    ref = jobs.onboard(ADMIN, rid)
    item = step(r, panel)
    assert item["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and j["diff"]["files"] == [".fabrique/setup.json"]
    assert j["evidence"] and all(e["ok"] for e in j["evidence"])                  # recipe replayed by the runner
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert "Review" not in lim and not lim["Protected paths"]               # never a standalone merge, even in "confident" mode
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert repo["onboarding"] == "analyzing" and repo["recipe"]["checks"] == {"tests": "python3 -c 'import notebook'"}
    assert repo["recipe"]["env"][0]["name"] == "API_KEY"
    jobs.decide(REVIEWER, ref, "approve")
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "ready"
    jobs.create(REQ, "Réparer", "B", "", rid)                                     # the repo now accepts jobs


def test_a_recipe_without_checks_can_still_be_merged_after_human_approval(setup, tmp_path):
    """Project without tests or build: the recipe declares no check. The verified commit must still be recorded,
    otherwise the approved merge fails ("no verified sha")."""
    r, forge, panel = setup
    _drop_recipe(forge, tmp_path)
    rid = _new_repo("alice/sanscheck")
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["verified_sha"] and not j["evidence"]
    jobs.decide(ADMIN, ref, "approve")
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "merged"
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "ready"


def test_onboarding_keeps_only_the_recipe(setup, tmp_path):
    r, forge, panel = setup
    _drop_recipe(forge, tmp_path)
    rid = _new_repo("alice/touche")
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["diff"]["files"] == [".fabrique/setup.json"]          # code and .venv excluded
    assert any("left out" in (e["message"] or "") and "notebook.py" in e["message"] and ".venv" in e["message"] for e in j["events"])


def test_runner_never_commits_environment_folders(setup, repo, tmp_path):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    real = r.agent

    def agent_leaving_env(ctx, *a, **k):
        res = real(ctx, *a, **k)
        os.makedirs(os.path.join(ctx.repo_dir, ".venv", "bin"), exist_ok=True)
        open(os.path.join(ctx.repo_dir, ".venv", "bin", "activate"), "w").write("x")
        open(os.path.join(ctx.repo_dir, "oublie.py"), "w").write("x = 1\n")
        return res
    r.agent = agent_leaving_env
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert "oublie.py" in j["diff"]["files"] and not any(f.startswith(".venv") for f in j["diff"]["files"])


def test_onboarding_with_existing_recipe_is_immediate(setup):
    r, forge, panel = setup
    rid = _new_repo("alice/deja")
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "merged" and not forge.prs
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert repo["onboarding"] == "ready" and repo["recipe"]["checks"]


def test_recipe_validation():
    ok = rmain.validate_recipe({"setup": ["pip install -r r.txt"], "checks": {"tests": "pytest"}, "env": [{"name": "X", "purpose": "p"}]})
    assert ok["checks"] == {"tests": "pytest"}
    for bad in ({"setup": "pip"}, {"checks": {"Tests!": "x"}}, {"env": [{"name": "X", "value": "secret"}]}, {"system": ["libpq"]},
                {"checks": {f"c{i}": "x" for i in range(9)}}):
        with pytest.raises(ValueError):
            rmain.validate_recipe(bad)


def test_onboarding_waits_for_access_or_first_commit_then_retries_same_job(setup, tmp_path, monkeypatch):
    r, forge, panel = setup
    empty = str(tmp_path / "vide.git")
    _git("init", "-q", "--bare", empty, cwd=tmp_path)
    rid = _new_repo("alice/vide")
    monkeypatch.setattr(forge, "clone_url", lambda slug: str(tmp_path / "absent.git"))     # no access
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "waiting"
    assert j["stage"] == "queued" and j["status_text"].startswith("Waiting for access") and "collaborator" in j["status_text"]
    assert jobs.onboard(ADMIN, rid) == ref                                       # "Retry": same job, not a new one
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)                  # access granted, but empty repo
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["status_text"].startswith("Nothing to analyse")
    assert jobs.onboard(ADMIN, rid) == ref
    assert jobs.onboard(ADMIN, rid) == ref                                       # repeated clicks: only one attempt in the queue
    with db.tx() as c:
        n = len(db.rows(c, select(db.intents).where(db.intents.c.state == "pending")))
        assert n == 1
    step(r, panel)
    # new automatic attempt: nothing until the delay has passed, then queued
    assert panel.claim() is None
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(updated_at=0))
    monkeypatch.setattr(forge, "clone_url", lambda slug: forge.bare)            # first commit pushed (with recipe)
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "ready"
        assert len(db.rows(c, select(db.jobs).where(db.jobs.c.source == "onboarding", db.jobs.c.repo_id == rid))) == 1
    html = web("admin").get(f"/repos?repo={rid}").text
    assert "Ready" in html


def test_empty_repo_is_initialised_after_human_ok_then_onboarded_after_first_merge(setup, tmp_path, monkeypatch):
    r, forge, panel = setup
    empty = str(tmp_path / "neuf.git")
    _git("init", "-q", "--bare", "-b", "main", empty, cwd=tmp_path)
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)
    monkeypatch.setattr(forge, "bare", empty)
    rid = _new_repo("alice/neuf")
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "empty"
    assert "Initialize it" in jobs.get(ADMIN, ref)["status_text"]
    assert "Initialize the repo" in web("admin").get(f"/repos?repo={rid}").text
    with pytest.raises(jobs.Refused):
        jobs.init_repo(REVIEWER, rid)                                                 # validation by an admin
    assert jobs.init_repo(ADMIN, rid) == ref
    item = step(r, panel)
    assert item["intent"]["kind"] == "init"
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=empty, capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".fabrique/setup.json", "README.md"]
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "blank"
    assert jobs.get(ADMIN, ref)["stage"] == "merged"
    # first request in the empty repo: PR without checks, therefore human review
    first = jobs.create(REQ, "Réparer", "Crée notebook.py avec ok()", "", rid)
    step(r, panel)
    j = jobs.get(ADMIN, first)
    lim = {l["name"]: l for l in j["decision"]["limits"]}
    assert j["stage"] == "pr" and not lim["Checks green"]["ok"] and "no check" in lim["Checks green"]["detail"]
    jobs.decide(REVIEWER, first, "approve")
    step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "merged"
    # the merge restarts onboarding: the agent writes the real recipe
    with db.tx() as c:
        onb = db.rows(c, select(db.jobs).where(db.jobs.c.repo_id == rid, db.jobs.c.source == "onboarding").order_by(db.jobs.c.id))
    assert len(onb) == 2 and onb[1]["stage"] == "queued"
    item = step(r, panel)
    assert item["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb[1]["ref"])
    assert j["stage"] == "pr" and j["diff"]["files"] == [".fabrique/setup.json"]          # empty recipe replaced by the real one


def test_a_repo_with_only_a_readme_is_initialised_like_an_empty_one(setup, tmp_path, monkeypatch):
    """Repo created on the Git host with its README (or a license): no project to analyze, so no onboarding by the agent."""
    r, forge, panel = setup
    bare_repo, src = str(tmp_path / "readme.git"), tmp_path / "depart"
    _git("init", "-q", "--bare", "-b", "main", bare_repo, cwd=tmp_path)
    src.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=src)
    (src / "README.md").write_text("# Mon projet\n")
    (src / "LICENSE").write_text("MIT\n")
    (src / ".gitignore").write_text("*.pyc\n")
    _git("add", "-A", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "Initial commit", cwd=src)
    _git("push", "-q", bare_repo, "main", cwd=src)
    monkeypatch.setattr(forge, "clone_url", lambda slug: bare_repo)
    monkeypatch.setattr(forge, "bare", bare_repo)
    rid = _new_repo("alice/readme")
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "empty"     # and not "failed"
    assert jobs.get(ADMIN, ref)["status_text"].startswith("Nothing to analyse")
    assert "Initialize the repo" in web("admin").get(f"/repos?repo={rid}").text
    assert jobs.init_repo(ADMIN, rid) == ref
    assert step(r, panel)["intent"]["kind"] == "init"
    ls = lambda: sorted(subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare_repo, capture_output=True, text=True).stdout.split())
    assert ls() == [".fabrique/setup.json", ".gitignore", "LICENSE", "README.md"]
    readme = subprocess.run(["git", "show", "main:README.md"], cwd=bare_repo, capture_output=True, text=True).stdout
    assert readme == "# Mon projet\n"                                                               # the existing README is kept
    assert subprocess.run(["git", "rev-list", "--count", "main"], cwd=bare_repo, capture_output=True, text=True).stdout.strip() == "2"
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "blank"
    assert jobs.get(ADMIN, ref)["stage"] == "merged"


def test_a_repo_with_real_files_is_not_taken_for_an_empty_one():
    from runner import main as rm
    ok = lambda names: all("/" not in f and rm.BARE_OK.match(f) for f in names)
    assert ok(["README.md", "LICENSE", ".gitignore", "readme", "Licence.txt", "CHANGELOG.md"])
    for real in ("main.py", "package.json", "Makefile", "src/README.md", "README.md.sh", "Dockerfile", ".gitlab-ci.yml"):
        assert not ok(["README.md", real]), real


def _commit_on_main(bare_repo, tmp_path, name, content=None):
    """Pushes a commit directly to main of the test repo (adds the file, or removes it if content is None)."""
    w = tmp_path / f"w-{name}-{'add' if content is not None else 'rm'}"
    _git("clone", "-q", bare_repo, str(w), cwd=tmp_path)
    if content is None:
        (w / name).unlink()
    else:
        (w / name).write_text(content)
    _git("add", "-A", cwd=w)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", f"{name}", cwd=w)
    _git("push", "-q", "origin", "HEAD:main", cwd=w)


def test_a_blank_repo_stays_usable_whatever_happens_to_its_onboarding(setup, tmp_path, monkeypatch):
    """The workflow of a first installation: empty repo, initialization, first request, then an onboarding that
    does not succeed (project without tests: the agent gives up). The repo must never get stuck."""
    r, forge, panel = setup
    empty = str(tmp_path / "neuf.git")
    _git("init", "-q", "--bare", "-b", "main", empty, cwd=tmp_path)
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)
    monkeypatch.setattr(forge, "bare", empty)
    rid = _new_repo("alice/neuf")
    state = lambda: db_repo(rid)["onboarding"]

    def db_repo(i):
        with db.tx() as c:
            return db.one(c, select(db.repos).where(db.repos.c.id == i))
    ref0 = jobs.onboard(ADMIN, rid); step(r, panel)
    jobs.init_repo(ADMIN, rid); step(r, panel)
    assert state() == "blank" and jobs.is_blank(db_repo(rid))
    first = jobs.create(REQ, "Réparer", "Crée notebook.py avec ok()", "", rid); step(r, panel)
    jobs.decide(REVIEWER, first, "approve"); step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "merged"
    with db.tx() as c:
        onb = db.rows(c, select(db.jobs).where(db.jobs.c.repo_id == rid, db.jobs.c.source == "onboarding").order_by(db.jobs.c.id))[-1]
    assert onb["ref"] != ref0 and onb["stage"] == "queued" and state() == "blank"          # restarted, without blocking the repo
    why = [e for e in jobs.get(ADMIN, onb["ref"])["events"] if e["kind"] == "onboarding" and e["message"].startswith("started by Fabrique after")]
    assert len(why) == 1                                                                    # the explanation is written only once
    second = jobs.create(REQ, "Réparer encore", "B", "", rid)                               # request accepted during onboarding
    _commit_on_main(empty, tmp_path, "NOTHING_TO_CHECK", "x\n")                             # the onboarding agent will abandon
    assert step(r, panel)["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "failed" and "stays usable" in j["status_text"]
    assert state() == "blank"                                                               # neither "failed" nor "not ready"
    page = web("admin").get(f"/repos?repo={rid}").text
    assert "Blank repo" in page and "Last onboarding attempt" in page and f'action="/repos/{rid}/onboard"' in page
    step(r, panel)
    assert jobs.get(ADMIN, second)["stage"] == "pr"                                         # the request succeeds normally
    # onboarding is retried after the following merge; this time it succeeds, and the recipe PR does not prevent anything
    _commit_on_main(empty, tmp_path, "NOTHING_TO_CHECK", None)
    jobs.decide(REVIEWER, second, "approve"); step(r, panel)
    assert jobs.get(ADMIN, second)["stage"] == "merged"
    assert step(r, panel)["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "pr" and j["verified_sha"] and state() == "blank"
    third = jobs.create(REQ, "Troisième", "B", "", rid)                                     # always usable, recipe PR pending
    step(r, panel)
    jobs.decide(REVIEWER, third, "approve"); step(r, panel)
    assert jobs.get(ADMIN, third)["stage"] == "merged" and panel.claim() is None           # no new onboarding: a PR is pending
    jobs.decide(ADMIN, onb["ref"], "approve")
    item = step(r, panel)
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "merged" and state() == "ready" and not jobs.is_blank(db_repo(rid)), (j["stage"], j["status_text"], state())


def test_init_writes_nothing_as_the_runner_user():
    """Regression: in production the agent has a different uid than the runner; every file in the workspace is created by
    sh() (under the agent's user), never by the runner itself (otherwise "Permission denied")."""
    import inspect
    src = inspect.getsource(rmain.Runner.init_repo)
    assert "open(" not in src and "makedirs(ctx.repo_dir" not in src


def test_job_image_from_recipe_and_detection(tmp_path):
    for files, img in ((["go.mod"], "go"), (["pom.xml"], "java"), (["package.json"], "node"),
                       (["package.json", "pyproject.toml"], "python"), ([], "python")):
        d = tmp_path / img / str(len(files)); d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("x")
        assert rmain.detect_image(str(d)) == img, files
    assert rmain.validate_recipe({"image": "node", "setup": ["npm ci"], "checks": {"tests": "npm test"}})["image"] == "node"
    with pytest.raises(ValueError):
        rmain.validate_recipe({"image": "registre/malveillant:latest", "setup": []})


def test_docker_executor_asks_the_launcher_for_the_recipe_image():
    import httpx
    from runner import executor
    seen = []
    ex = executor.Docker(base="http://launcher", token="t" * 40,
                         transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={})))

    class C:
        ref, image = "FAB-1", "java"
    ex.start(C())
    assert seen == [{"image": "java"}]


def test_java_proxy_options(monkeypatch):
    from runner import agent
    monkeypatch.setattr(rconf, "JOB_PROXY", "http://fabrique-proxy:3128")
    env = agent.base_env("/tmp/h")
    assert "-Dhttps.proxyHost=fabrique-proxy -Dhttps.proxyPort=3128" in env["MAVEN_OPTS"] and env["GRADLE_OPTS"] == env["MAVEN_OPTS"]


def test_codex_job_runs_through_the_same_chain(setup, repo, monkeypatch):
    r, forge, panel = setup
    monkeypatch.setattr(rconf, "CODEX_BIN", os.path.join(HERE, "fake", "codex"))
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    with db.tx() as c:                                                          # job agent: Codex, OpenAI model
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(harness="codex", model="gpt-5-mini"))
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["session_id"] == "thr-codex-1" and j["evidence"] and all(e["ok"] for e in j["evidence"])
    assert any(e["kind"] == "bash" and "ls" in e["message"] for e in j["events"])
    # Agent run: 50,000 input tokens + 50,000 in cache + 10,000 output, at gpt-5-mini rate; the reviewers
    # (launched here in agent mode with the same fake Codex) count IN ADDITION to the job cost (C11 audit).
    agent_cost = (50000 * 0.25 + 50000 * 0.025 + 10000 * 2) / 1e6
    assert j["cost"] > agent_cost + 1e-4


def test_preparing_a_job_never_kills_other_jobs_processes(setup, repo, monkeypatch):
    """FAB-266 Regression: in docker mode, or if another job is running, the preparation does not kill any agent processes."""
    r, forge, panel = setup
    killed = []
    monkeypatch.setattr(rmain, "kill_agent_leftovers", lambda: killed.append(1))
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    r.active = 1                                       # another job is already running
    step(r, panel)
    assert not killed
    assert jobs.get(ADMIN, ref)["stage"] == "pr"       # and the job will still complete (cloning included)
    r.ex.name = "docker"
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    r.active = 0
    step(r, panel)
    assert not killed and jobs.get(ADMIN, ref)["stage"] == "pr"
    r.ex.name = "local"


def test_repo_creation_needs_admin_validation_then_runner_creates_and_initialises(setup, tmp_path, monkeypatch):
    r, forge, panel = setup
    empty = str(tmp_path / "cree.git")
    _git("init", "-q", "--bare", "-b", "main", empty, cwd=tmp_path)
    created = []
    monkeypatch.setattr(forge, "create_repo", lambda ns, name, desc="": created.append((ns, name, desc)) or "created", raising=False)
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)
    with pytest.raises(jobs.Refused, match="Repo creation"):
        jobs.request_repo(REVIEWER, "forgejo", "notes-api")                          # no group configured
    with db.tx() as c:
        db.set_setting(c, "creation", {"forgejo": "fabrique"}, "alice")
    with pytest.raises(jobs.Refused):
        jobs.request_repo(REQ, "forgejo", "notes-api")                           # a requester does not request a repo
    with pytest.raises(jobs.Refused):
        jobs.request_repo(REVIEWER, "forgejo", "Nom Invalide")
    rid = jobs.request_repo(REVIEWER, "forgejo", "notes-api", "petite API")
    assert "fabrique/notes-api" in web("admin").get("/inbox").text and not created and panel.claim() is None
    with pytest.raises(jobs.Refused):
        jobs.decide_repo(REVIEWER, rid, True)                                        # one does not validate their own request
    assert "Create the repo" in web("admin").get(f"/repos?repo={rid}").text    # buttons on the repo page
    # 1st attempt: the provider refuses (unauthorized token) → failure; "Relaunch creation" recreates, not an onboarding
    monkeypatch.setattr(forge, "create_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")), raising=False)
    ref = jobs.decide_repo(ADMIN, rid, True)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "failed" and "Restart the creation" in web("admin").get(f"/repos?repo={rid}").text
    monkeypatch.setattr(forge, "create_repo", lambda ns, name, desc="": created.append((ns, name, desc)) or "created", raising=False)
    assert jobs.onboard(ADMIN, rid) == ref
    item = step(r, panel)
    assert item["intent"]["kind"] == "create" and created == [("fabrique", "notes-api", "petite API")]
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=empty, capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".fabrique/setup.json", "README.md"]
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert repo["onboarding"] == "blank" and repo["accepts_jobs"] and jobs.get(ADMIN, ref)["stage"] == "merged"
    # refusal: the request disappears, nothing is created
    rid2 = jobs.request_repo(REVIEWER, "forgejo", "autre")
    jobs.decide_repo({"user": "vic", "name": "Vic", "role": "reviewer"}, rid2, False, "pas besoin")   # another reviewer
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid2)) is None
    assert len(created) == 1


def test_codex_stage_inferred_from_shell_writes():
    from runner import agent
    assert agent.WRITES.search("/bin/sh -lc \"sed -i 's/a/b/' app.py\"") and not agent.WRITES.search("python3 -m pytest")


def test_job_path_reaches_language_toolchains():
    from runner import agent
    path = agent.base_env("/work/FAB-1/home")["PATH"].split(":")
    for d in ("/usr/local/go/bin", "/opt/java/openjdk/bin", "/usr/local/bin", "/usr/bin", "/work/FAB-1/home/go/bin"):
        assert d in path                                                         # seen on FAB-275: "go: not found"


def test_review_effort_criteria_and_tool_hints():
    """Review guide: effort calculated without model, request criteria read in the code reviewer points, and
    automatic check results given to the security reviewer as indices."""
    from runner import review
    assert [review.effort(*a) for a in ((10, 1), (100, 4), (250, 8), (700, 12), (5000, 40))] == [1, 2, 3, 4, 5]
    assert review.effort(10, 1, protected=True) == 2 and review.effort(5000, 40, protected=True) == 5
    pts = ["Criterion met: pytest passe sur les deux tests.", "criterion not met : la liste vide n'est pas couverte",
           "Criterion unclear: « rapide » n'est pas mesurable", "Un autre point sans rapport"]
    assert review.criteria(pts) == [("ok", "pytest passe sur les deux tests."), ("no", "la liste vide n'est pas couverte"),
                                    ("unclear", "« rapide » n'est pas mesurable")]

    class Ctx:
        title, job = "T", {"request": "R", "done_when": "D"}
        security_report = "Contrôle de sécurité : 1 alerte(s)\nSecrets écrits en dur :\n  ⚠ app.py ligne 2 (règle stripe-access-token)"
    _, _, sec = review.prompts(Ctx(), "security", "diff", "direct")
    _, _, code = review.prompts(Ctx(), "code", "diff", "direct")
    assert "<automatic_check>" in sec and "stripe-access-token" in sec and "Discard false positives" in sec
    assert "<automatic_check>" not in code and "Criterion not met" in code
    Ctx.security_report = ""
    assert "<automatic_check>" not in review.prompts(Ctx(), "security", "diff", "direct")[2]


def test_network_line_in_the_evidence_warns_on_refusals():
    """Job network egress in the evidence: informative, with alert if there were refusals; nothing if the launcher does not know."""
    from runner import main as rm

    class Ex:
        def __init__(self, r):
            self.r = r
        def net(self, ctx):
            return self.r
    run = rm.Runner.__new__(rm.Runner)
    ctx = type("C", (), {"ref": "FAB-1"})()
    run.ex = Ex({"known": True, "allowed": [["pypi.org", 3]], "denied": [["evil.test", 2]]})
    n = run.network(ctx)
    assert n["name"] == "Network" and n["kind"] == "network" and n["ok"] and n["warn"] and n["output"].startswith("Network egress: 2 refused, 3 allowed")
    assert "⚠ evil.test (2×)" in n["output"] and "· pypi.org (3×)" in n["output"]
    run.ex = Ex({"known": True, "allowed": [["pypi.org", 1]], "denied": []})
    n = run.network(ctx)
    assert not n["warn"] and n["output"].startswith("Network egress: 1 allowed, none refused")
    run.ex = Ex(None)
    assert run.network(ctx) is None
    from app import jobs
    warn = {"name": "Network", "kind": "network", "warn": True, "sha": "abc1234", "output": "Network egress: 2 refused, 3 allowed\n…"}
    assert jobs.security_warn(warn) and not jobs.security_warn(dict(warn, warn=False, output="Network egress: 3 allowed, none refused"))
    todo = jobs.to_check({"verified_sha": "abc1234", "evidence": [warn], "decision": {"limits": []}})
    assert todo[0]["title"].startswith("Network egress: 2 refused") and "proxy's allowlist" in todo[0]["text"]


def test_runner_purges_only_finished_workspaces(tmp_path, monkeypatch):
    """Cleanup: the runner deletes the folder and the mirror of a finished job; never a job in progress, never outside the folder."""
    from runner import main as rm
    tmp_path = tmp_path / "travail"
    tmp_path.mkdir()
    monkeypatch.setattr(rm.config, "WORK_DIR", str(tmp_path))
    monkeypatch.setattr(rm, "as_agent", lambda: [])
    for n in ("FAB-1", "FAB-2", ".mirrors/FAB-1.git", ".mirrors/FAB-2.git", "autre"):
        (tmp_path / n).mkdir(parents=True)
        (tmp_path / n / "f").write_text("x")
    run = rm.Runner.__new__(rm.Runner)
    run.running = {"FAB-2": object()}
    assert run.workspaces() == ["FAB-1"]                                           # neither the job in progress, nor the other folders
    for ref in ("FAB-1", "FAB-2", "../autre", "autre", ".mirrors"):
        run.purge(ref)
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.iterdir())
    assert left == [".mirrors", "FAB-2", "autre"] and [p.name for p in (tmp_path / ".mirrors").iterdir()] == ["FAB-2.git"]


def test_runner_sends_the_diff_of_the_verified_commit(setup, repo, monkeypatch):
    """0.7.5: with the PR, the runner sends the diff of the verified commit (read in its mirror); the dashboard displays it."""
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    p = jobs.patch_info(j)
    assert j["stage"] == "pr" and p and p["sha"] == j["verified_sha"] and [f["path"] for f in p["files"]] == ["notebook.py"] and not p["truncated"]
    lines = [l for h in jobs.diff_view(REVIEWER, ref)["files"][0]["hunks"] for l in h["lines"]]
    assert any(k == "rem" and "False" in t for k, _, _, t in lines) and any(k == "add" and "True" in t for k, _, _, t in lines)
    assert (p["add"], p["rem"]) == (j["diff"]["additions"], j["diff"]["deletions"])     # same counters as the gates
    # Review restarted: the diff is sent back, truncated at the runner ceiling and reported.
    monkeypatch.setattr(rconf, "DIFF_MAX", 120)
    jobs.rereview(REVIEWER, ref)
    step(r, panel)
    with db.tx() as c:
        row = db.one(c, select(db.diffs))
    assert row["truncated"] and len(row["text"]) <= 120 and row["sha"] == jobs.get(ADMIN, ref)["verified_sha"]


def test_a_dashboard_that_refuses_the_diff_does_not_stop_the_job(setup, repo, monkeypatch):
    """An older dashboard does not know the "diff" event: the job still goes through to its PR."""
    r, forge, panel = setup
    real = panel.event
    monkeypatch.setattr(panel, "event", lambda ref, **ev: None if ev.get("type") == "diff" else real(ref, **ev), raising=False)
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and jobs.patch_info(j) is None


def test_confidential_repo_sends_no_diff(setup, repo):
    """Repo setting "Show diff in Fabrique" unchecked: the runner sends nothing, the job succeeds anyway."""
    r, forge, panel = setup
    with db.tx() as c:
        s0 = dict(db.one(c, select(db.repos.c.settings).where(db.repos.c.id == repo))["settings"] or {})
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={**s0, "diff": False}))
    sent = []
    real = panel.event
    panel.event = lambda ref, **ev: (sent.append(ev.get("type")), real(ref, **ev))[1]
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and jobs.patch_info(j) is None and "pr" in sent and "diff" not in sent
    with db.tx() as c:
        assert db.rows(c, select(db.diffs)) == []


def test_diff_max_zero_sends_no_diff(setup, repo, monkeypatch):
    """FABRIQUE_DIFF_MAX=0 : no diff leaves the runner (repos where the code must not be copied); the job succeeds."""
    r, forge, panel = setup
    monkeypatch.setattr(rconf, "DIFF_MAX", 0)
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and jobs.patch_info(j) is None
    with db.tx() as c:
        assert db.rows(c, select(db.diffs)) == []


def _branch_messages(forge, ref):
    return subprocess.run(["git", "log", "--format=%B%x01", f"main..fabrique/{ref}"], cwd=forge.bare, capture_output=True, text=True).stdout


@pytest.mark.parametrize("mode,confident,verdict,line", [
    ("NOOPINION", True, "given when asked", "asked once"),
    ("TEXTOPINION", True, "written, not run", "wrote its opinion in its answer"),
    ("NEVEROPINION", False, "", "gave no opinion, even when asked")])
def test_runner_asks_once_for_a_missing_opinion_then_reads_the_written_one(setup, repo, mode, confident, verdict, line):
    """A model sometimes writes `fab decision …` in its answer instead of running it (seen on real jobs): the opinion no
    longer depends on that. The runner asks once; failing that it reads the written command; failing that it says so."""
    r, forge, panel = setup
    ref = jobs.create(REQ, f"Fix ok() {mode}", "ok() must return True", "the check passes", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["pr_number"] == 1
    assert bool(j["decision"]["confident"]) is confident and (j["decision"]["verdict"] or "") == verdict
    log = [str(e["message"]) for e in j["events"]]
    assert any(line in m for m in log)
    assert sum("asked once" in m for m in log) == (0 if mode == "TEXTOPINION" else 1)   # at most once; not at all when it was written
    if mode == "TEXTOPINION":
        assert j["decision"]["interpretation"] == "read from the text" and j["decision"]["assumptions"] == ["one gap"]


def test_every_job_commit_carries_the_job_trailer_whatever_the_agent_wrote(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Fix ok()", "ok() must return True", "the check passes", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    bodies = [b for b in _branch_messages(forge, ref).split("\x01") if b.strip()]
    assert bodies and all(f"Fabrique-Job: {ref}" in b for b in bodies)        # the fake agent's commit had no trailer
    assert all(b.count("Fabrique-Job:") == 1 for b in bodies)
    head = subprocess.run(["git", "rev-parse", f"fabrique/{ref}"], cwd=forge.bare, capture_output=True, text=True).stdout.strip()
    assert head.startswith(j["verified_sha"])                                 # the verified commit is the rewritten one


def test_opinion_written_as_text_is_read_without_running_anything():
    from runner import messages
    ok = messages.opinion_from_text('All good.\n\n`fab decision --confident no --interpretation "a b" --assumption "x" --assumption "y" --verdict "not sure; $(rm -rf /)"`')
    assert ok == {"confident": False, "verdict": "not sure; $(rm -rf /)", "interpretation": "a b", "assumptions": ["x", "y"], "notes": "", "recommendation": ""}
    for bad in ("", "no command here", "fab decision --confident perhaps --verdict x --interpretation y", 'fab decision --verdict "unclosed'):
        assert messages.opinion_from_text(bad) is None


def test_a_question_written_in_the_answer_is_asked_all_the_same(setup, repo):
    """Seen on a real job: for a vague request the agent wrote `fab question "…"` in its answer instead of running it; the
    job then failed for lack of change. The runner reads it: the job waits for the requester, as if the command had run."""
    r, forge, panel = setup
    ref = jobs.create(REQ, "Improve it WRITTENQUESTION", "Improve it.", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "waiting" and j["questions"][0]["question"] == "What should be improved? (A) the speed; (B) the error messages?"
    assert any("wrote its question in its answer" in str(e["message"]) for e in j["events"])
    jobs.answer(REQ, ref, "B")
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"


def test_written_commands_read_only_the_last_one_and_never_a_placeholder():
    from runner import messages as m
    assert m.written_command('fab step triage "x"\nfab question "Which one do you want? (A) a; (B) b"') == ("question", "Which one do you want? (A) a; (B) b")
    assert m.written_command('I give up.\n`fab fail "the toolchain is broken, pip cannot install anything"`') == ("fail", "the toolchain is broken, pip cannot install anything")
    assert m.written_command('fab question "… (A) …; (B) …?"') is None          # the template of the instructions, copied
    assert m.written_command('fab question "short"') is None and m.written_command("I will not use fab questionnaire") is None
    assert m.written_command('fab question "first question, long enough"\nthen\nfab decision --confident yes --verdict v --interpretation i')[0] == "decision"


# ---- 0.8.2 ----------------------------------------------------------------------------------------------------------

def test_job_resumed_after_main_moved_reaches_the_pr(setup, repo, tmp_path):
    """A job waits for an answer while other pull requests are merged: the main branch has moved when it resumes. The new
    head of main does not exist in the job's workspace; every command run there against it failed ("bad object")."""
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok() QUESTION", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "waiting"
    # the waiting job's directory is not among those the runner may delete, however long the wait
    assert ref in r.workspaces() and jobs.workspaces_to_purge(r.workspaces(), now=__import__("time").time() + 7 * 86400) == []
    other = tmp_path / "other"
    _git("clone", "-q", forge.bare, str(other), cwd=tmp_path)
    (other / "elsewhere.py").write_text("x = 1\n")
    _git("add", "-A", cwd=other); _git("commit", "-qm", "another pull request, merged meanwhile", cwd=other); _git("push", "-q", "origin", "main", cwd=other)
    jobs.answer(REQ, ref, "B")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr", j["status_text"]
    assert j["diff"]["files"] == ["notebook.py"]              # the job's own change only, not what main gained meanwhile
    assert all(e["ok"] for e in j["evidence"])


def test_no_change_with_an_opinion_closes_the_job_instead_of_failing(setup, repo):
    """The requester answers "drop it": the agent gives its opinion and changes nothing. A clean end, in an explicit state."""
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok() QUESTION", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    jobs.answer(REQ, ref, "A: DROPIT")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "closed" and j["status_text"] == "Closed without change", (j["stage"], j["status_text"])
    assert "dropped at the requester's request" in j["timeline"][-1]["summary"]
    assert not forge.prs and jobs.outcome(j) == "closed"
    assert ref not in [x["ref"] for x in jobs.inbox(ADMIN) if x.get("kind") == "failed"]


def test_no_change_without_an_opinion_still_fails(setup, repo, monkeypatch):
    r, forge, panel = setup
    monkeypatch.setattr(rmain.Runner, "ensure_opinion", lambda self, ctx: True)
    monkeypatch.setattr(rmain.Runner, "agent", lambda self, ctx, *a, **k: True)
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "no change" in j["status_text"]


def test_default_done_when_criterion(setup, repo):
    """No "Done when" given with a change request: the default criterion reaches the agent, the reviewers and the pull
    request, marked as the default one; a study gets none."""
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "ok() doit renvoyer True", "", repo)
    j = jobs.get(ADMIN, ref)
    assert jobs.done_when_of(j) == (jobs.DEFAULT_DONE, True) and jobs.done_when_of(dict(j, done_when="x")) == ("x", False)
    assert jobs.done_when_of(dict(j, source="study")) == ("", False)
    item = step(r, panel)
    assert item["job"]["done_when"] == str(jobs.DEFAULT_DONE) and item["job"]["done_when_default"] is True
    body = forge.prs[1]["body"]
    assert str(jobs.DEFAULT_DONE) in body and "_Default criterion: none was given with the request._" in body
    page = web("admin").get(f"/jobs/{ref}/panel").text
    assert "default criterion" in page and "nothing unrelated is modified" in page


def test_texts_worded_by_the_runner_are_stored_as_facts(setup, repo):
    """A line the runner words itself reaches the database with its template and values, not as a plain sentence."""
    from app import i18n
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok() NOOPINION", "ok() doit renvoyer True", "le check passe", repo)
    step(r, panel)
    msgs = [e["message"] for e in jobs.get(ADMIN, ref)["events"]]
    asked = [m for m in msgs if "asked once" in m]
    assert asked and all(isinstance(m, i18n.Msg) and m.tid == "the agent finished without giving its opinion: asked once" for m in asked), msgs
    workshop = [m for m in msgs if "workshop" in m]
    assert workshop and isinstance(workshop[0], i18n.Msg)
    assert i18n.tr(asked[0], "fr") != str(asked[0])               # worded again for a French reader


def test_a_saturated_model_makes_the_job_wait_then_resume(setup, repo, monkeypatch):
    """Real case (0.8.2): "API Error: 500 Context size has been exceeded" while two jobs shared a local model: the job failed.
    It now waits and resumes its session; the wait is visible in the job, as a fact."""
    monkeypatch.setattr(rconf, "MODEL_WAITS", [1, 1])
    r, forge, panel = setup
    seen = []
    real = panel.event
    def spy(ref, **ev):
        if ev.get("type") == "model_wait":
            seen.append(jobs.get(ADMIN, ref) or {})
        res = real(ref, **ev)
        if ev.get("type") == "model_wait":
            seen.append(jobs.get(ADMIN, ref))
        return res
    monkeypatch.setattr(panel, "event", spy)
    ref = jobs.create(REQ, "Repair SATURATED", "ok() must return True", "the check passes", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready", j["status_text"]
    waiting = seen[1]
    assert waiting["status_text"] == "Waiting for the model (attempt 1 of 2, next try in 1 s)" and waiting["stage"] != "failed"
    lines = [str(e["message"]) for e in j["events"] if e["kind"] == "runner"]
    assert any(l.startswith("the model server is saturated or unreachable: the job waits 1 s then resumes where it was (attempt 1 of 2)") for l in lines)
    assert "the agent resumes after waiting for the model" in lines
    with db.tx() as c:   # stored as facts (template and values), not as sentences
        raw = [x[0] for x in c.exec_driver_sql("select message from events where kind = 'runner'")]
    assert any(m.startswith("\x1e") and '"the model server is saturated' in m and '"secs": 1' in m for m in raw)


def test_a_model_that_stays_saturated_fails_with_an_explicit_reason(setup, repo, monkeypatch):
    monkeypatch.setattr(rconf, "MODEL_WAITS", [1, 1])
    r, forge, panel = setup
    ref = jobs.create(REQ, "Repair ALWAYSSATURATED", "B", "the check passes", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed"
    assert "the model server stayed saturated or unreachable after 2 attempts" in j["status_text"]
    assert "limit the simultaneous jobs on this model" in j["status_text"]
    assert sum(1 for e in j["events"] if "the job waits" in str(e["message"])) == 2


def test_a_conversation_larger_than_the_context_is_not_retried(setup, repo, monkeypatch):
    monkeypatch.setattr(rconf, "MODEL_WAITS", [1, 1])
    r, forge, panel = setup
    ref = jobs.create(REQ, "Repair TOOLARGE", "B", "the check passes", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "no longer fits in the model's context, waiting would not help" in j["status_text"]
    assert not any("the job waits" in str(e["message"]) for e in j["events"])


@pytest.mark.parametrize("error,kind", [
    ("API Error: 500 Context size has been exceeded.", "busy"), ("API Error: 429 rate_limit_error", "busy"),
    ("API Error: 503 Service Unavailable", "busy"), ("API Error: 529 overloaded_error", "busy"), ("Loading model", "busy"),
    ("API Error: Request timed out.", "busy"), ("API Error: Connection error.", "busy"),
    ("API Error: 400 the request exceeds the available context size, try increasing it", "too_large"),
    ("API Error: 400 prompt is too long: 210000 tokens > 200000 maximum", "too_large"),
    ("API Error: 401 invalid x-api-key", None), ("the agent exited with code 1: boom", None), ("budget exceeded (2.00 $)", None)])
def test_model_trouble_is_told_apart(error, kind):
    from runner import messages
    assert messages.model_trouble(error) == kind


def test_a_merge_refused_for_a_moment_is_tried_again(setup, repo, monkeypatch):
    """Real case (0.8.3 trial): two pull requests merged at the same time, the second got "409 PushRejected" from the Git
    host and the job ended on an unexpected runner error."""
    monkeypatch.setattr(rconf, "MERGE_RETRY_WAITS", [1, 1])
    r, forge, panel = setup
    ref = jobs.create(REQ, "Repair", "B", "the check passes", repo)
    step(r, panel)
    refusals, real = [], forge.merge
    def flaky(*a, **k):
        if len(refusals) < 2:
            refusals.append(1)
            raise RuntimeError("merge refused by Forgejo (409): PushRejected without remote error message")
        return real(*a, **k)
    monkeypatch.setattr(forge, "merge", flaky)
    jobs.decide(REVIEWER, ref, "approve")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "merged" and len(refusals) == 2
    assert sum(1 for e in j["events"] if "refused the merge for now" in str(e["message"])) == 2


def test_a_merge_refused_every_time_fails_cleanly_and_can_be_approved_again(setup, repo, monkeypatch):
    monkeypatch.setattr(rconf, "MERGE_RETRY_WAITS", [1])
    r, forge, panel = setup
    ref = jobs.create(REQ, "Repair", "B", "the check passes", repo)
    step(r, panel)
    def never(*a, **k):
        raise RuntimeError("merge refused by Forgejo (409): PushRejected without remote error message")
    monkeypatch.setattr(forge, "merge", never)
    jobs.decide(REVIEWER, ref, "approve")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "the Git host refused the merge after 2 attempts" in j["status_text"]
    assert "runner error" not in j["status_text"] and "PushRejected" in j["status_text"]
