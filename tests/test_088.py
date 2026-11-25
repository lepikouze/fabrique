"""0.8.8: what the first install at work taught — repos suggested while adding one, the provider form filled from the start,
ticked models kept through "Test", "Setup" shown in demo mode, files produced by the installation never committed by the
runner, a reviewers' limit per provider, information lines that are not checks, a board that no longer crushes its cards."""
import json, os, subprocess, time
import httpx, pytest
from sqlalchemy import insert, select, update
from app import config as app_config, db, jobs, providers, work
from runner import config, review
from runner import main as rmain
from .conftest import ADMIN, REQ, web


# ---------- repo suggestions ----------

def _api(payload):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=payload)
    return seen, httpx.MockTransport(handler)


def test_forgejo_suggests_the_repos_the_bot_sees():
    seen, t = _api({"ok": True, "data": [{"full_name": "team/infra-dash", "private": True, "default_branch": "main", "description": "Infra  board"},
                                         {"full_name": "team/other", "private": False, "default_branch": "main"}]})
    got = providers.git_repo_search("forgejo", "https://git.example", "tok", "infra", transport=t)
    assert got == [{"slug": "team/infra-dash", "private": True, "branch": "main", "description": "Infra board"}]
    assert seen[0].url.path == "/api/v1/repos/search" and seen[0].headers["authorization"] == "token tok"


def test_gitlab_and_github_suggestions():
    _, t = _api([{"path_with_namespace": "grp/sub/app", "visibility": "internal", "default_branch": "dev"}])
    assert providers.git_repo_search("gitlab", "https://gl.example", "tok", "app", transport=t)[0] == \
        {"slug": "grp/sub/app", "private": True, "branch": "dev", "description": ""}
    _, t = _api([{"full_name": "me/site", "private": False, "default_branch": "main"}, {"full_name": "me/other", "private": True}])
    assert [r["slug"] for r in providers.git_repo_search("github", "https://github.com", "tok", "sit", transport=t)] == ["me/site"]


def test_a_refused_token_is_said_not_hidden():
    t = httpx.MockTransport(lambda req: httpx.Response(401))
    with pytest.raises(providers.Invalid, match="refuses the bot's token"):
        providers.git_repo_search("forgejo", "https://git.example", "tok", "x", transport=t)


def test_the_search_route_needs_a_token_and_a_query(repo):
    cl = web()
    assert cl.get("/repos/search?provider=forgejo&q=a").json() == {"items": []}            # one letter: nothing asked
    r = cl.get("/repos/search?provider=nothing&q=abc").json()
    assert r["items"] == [] and "type the full path" in r["msg"]
    assert "data-repo-search" in cl.get("/repos").text


# ---------- settings page ----------

def test_ticked_models_stay_ticked_when_the_page_comes_back(repo, monkeypatch):
    monkeypatch.setattr(app_config, "SECRET_KEY", "une-cle-maitre-de-test")
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [{"id": "qwen3-latest"}, {"id": "other-model"}]})))
    cl = web()
    cl.post("/settings/providers", data={"action": "save", "type": "compatible", "pid": "office", "name": "Office", "base_url": "http://vllm:9090/v1", "key": ""})
    page = cl.get("/settings?tab=agents&load=office&tick=qwen3-latest").text
    i = page.index('value="qwen3-latest"')
    assert "checked" in page[i:i + 120] and "checked" not in page[page.index('value="other-model"'):page.index('value="other-model"') + 120]


def test_setup_is_shown_in_demo_mode_too(demo_on, monkeypatch):
    cl = web()
    assert 'href="/start"' in cl.get("/jobs").text          # demo data never counts as real setup: steps remain


def test_reviewers_limit_per_provider(repo):
    with db.tx() as c:
        providers.save(c, "t", "office", "compatible", "Office", "http://vllm:9090/v1", "", review_tokens="20000")
        assert next(p for p in providers.listing(c) if p["id"] == "office")["review_tokens"] == 20000
        with pytest.raises(providers.Invalid, match="Reviewers' limit"):
            providers.save(c, "t", "office", "compatible", "Office", "http://vllm:9090/v1", "", review_tokens="10")
    seen = []
    t = httpx.MockTransport(lambda req: seen.append(json.loads(req.content)) or httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}))
    review.ask({"id": "qwen", "provider": "openai", "type": "compatible", "base_url": "http://vllm:9090/v1", "review_max_tokens": 20000}, "s", "p", 10, t)
    review.ask({"id": "qwen", "provider": "openai", "type": "compatible", "base_url": "http://vllm:9090/v1"}, "s", "p", 10, t)
    assert seen[0]["max_tokens"] == 20000 and seen[1]["max_tokens"] == config.REVIEW_MAX_TOKENS


# ---------- information lines are not checks ----------

def test_security_and_network_lines_do_not_count_as_checks(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
    w = work.create_runner("r1", "container", "alice")
    with db.tx() as c:
        run = db.one(c, select(db.runners).where(db.runners.c.name == "r1"))
    assert work.claim(run)["job"]["ref"] == ref
    work.report(run, ref, {"type": "evidence", "sha": "abc1234567890", "checks": [
        {"name": "Security", "command": "security_check.py", "ok": True, "kind": "security", "output": "nothing"},
        {"name": "Network", "command": "proxy log", "ok": True, "kind": "network", "output": "nothing"}]})
    j = jobs.get(ADMIN, ref)
    assert j["checks_total"] == 0 and j["checks_ok"] == 0
    with db.tx() as c:
        row = db.one(c, select(db.jobs).where(db.jobs.c.id == jid))
        gates = work.evaluate(c, row, db.one(c, select(db.repos).where(db.repos.c.id == row["repo_id"])))
    g = next(x for x in gates if x["key"] == "checks")
    assert not g["ok"] and g["no_checks"]                    # "no check in the recipe: human review", not "2/2 green"


# ---------- files produced by the installation ----------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_files_produced_by_the_installation_are_not_committed_by_the_runner(tmp_path, monkeypatch):
    repo_dir = tmp_path / "FAB-9" / "repo"
    repo_dir.mkdir(parents=True)
    _git(repo_dir, "init", "-q", "-b", "main")
    (repo_dir / "app.js").write_text("one\n")
    _git(repo_dir, "add", "."); _git(repo_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    monkeypatch.setattr(config, "WORK_DIR", str(tmp_path))
    os.makedirs(tmp_path / ".mirrors", exist_ok=True)
    monkeypatch.setattr(rmain, "as_agent", lambda: [])
    monkeypatch.setattr(rmain, "sh", lambda ctx, cmd, **k: subprocess.run(cmd, cwd=ctx.repo_dir, capture_output=True, text=True))
    ctx = type("C", (), {"ref": "FAB-9", "repo_dir": str(repo_dir), "home": str(tmp_path), "ex": type("E", (), {"name": "local"})()})()
    (repo_dir / "package-lock.json").write_text('{"lock": 1}\n')     # written by the recipe's installation
    (repo_dir / "deps").mkdir(); (repo_dir / "deps" / "x.txt").write_text("x")
    r = rmain.Runner.__new__(rmain.Runner)
    r.snapshot_setup(ctx)
    (repo_dir / "app.js").write_text("two\n")                         # the agent's work
    (repo_dir / "new.js").write_text("new\n")
    ex = r.setup_excludes(ctx)
    assert ":(exclude,literal)package-lock.json" in ex and ":(exclude,literal)deps" in ex
    assert not any("app.js" in e or "new.js" in e for e in ex)
    (repo_dir / "package-lock.json").write_text('{"lock": 2}\n')     # the agent changed it on purpose: committed
    assert ":(exclude,literal)package-lock.json" not in r.setup_excludes(ctx)


# ---------- board ----------

def test_cards_show_a_short_sha_and_no_check_when_there_is_none(repo):
    ref = jobs.create(REQ, "T", "B", "", repo)
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(verified_sha="1710d10c6937", checks_ok=0, checks_total=0, stage="pr"))
    page = web().get("/jobs").text
    assert "1710d10" in page and "1710d10c6937</span>" not in page and "no check" in page
