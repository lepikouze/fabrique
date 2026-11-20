"""Runner : hook de garde, relay, et un cycle complet (démarrage, question, réponse, checks, PR, merge) avec un faux
Claude Code et un repo Git local, contre le vrai dashboard (TestClient)."""
import json, os, socket, subprocess, sys
import pytest
from sqlalchemy import select, update
from app import db, jobs, work
from runner import config as rconf
from runner import main as rmain
from runner.panel import Panel
from .conftest import ADMIN, REQ, VERIF, web

HERE = os.path.dirname(__file__)
GARDE = os.path.join(HERE, "..", "runner", "garde.py")


def garde(tool, inp, cwd="/work/FAB-1/repo"):
    p = subprocess.run([sys.executable, GARDE], input=json.dumps({"tool_name": tool, "tool_input": inp, "cwd": cwd}),
                       capture_output=True, text=True, env={"FAB_WORKSPACE": cwd, "PATH": os.environ["PATH"]})
    return p.returncode


@pytest.mark.parametrize("cmd,blocked", [("git push origin main", True), ("git remote -v", True), ("git config user.email x", True),
                                         ("curl http://10.0.0.1", True), ("printenv", True), ("cat /proc/1/environ", True),
                                         ("echo $ANTHROPIC_API_KEY", True), ("sudo ls", True), ("git commit -am x", False),
                                         (".venv/bin/python -m pytest -q", False), ("python3 -m venv .venv", False), ("git diff HEAD~1", False)])
def test_guard_bash(cmd, blocked):
    assert (garde("Bash", {"command": cmd}) == 2) == blocked


@pytest.mark.parametrize("tool,path,blocked", [("Read", "/work/FAB-1/repo/a.py", False), ("Read", "a.py", False), ("Read", "/etc/passwd", True),
                                               ("Edit", "../../FAB-2/repo/a.py", True), ("Write", ".git/config", True), ("Read", ".git/HEAD", False),
                                               ("WebFetch", "", True)])
def test_guard_files(tool, path, blocked):
    assert (garde(tool, {"file_path": path, "url": "x"}) == 2) == blocked


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
        n = next((k for k, v in self.prs.items() if v["head"] == head), len(self.prs) + 1)    # une PR par branche, comme une vraie forge
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
    (src / ".fabrique" / "setup.json").write_text(json.dumps({"setup": [], "checks": {"tests": "python3 -c 'import carnet; assert carnet.ok()'"}}))
    (src / "carnet.py").write_text("def ok():\n    return False\n")
    _git("init", "-q", "-b", "main", cwd=src); _git("add", "-A", cwd=src); _git("commit", "-qm", "init", cwd=src)
    bare = str(tmp_path / "essai.git")
    _git("clone", "-q", "--bare", str(src), bare, cwd=tmp_path)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    # Clé factice : le modèle passe par la passerelle (un modèle Anthropic sans clé est refusé) ; vérificateurs en mode agent
    # (le faux claude), puisque le mode direct appellerait le fournisseur.
    for k, v in dict(WORK_DIR=str(tmp_path / "work"), CLAUDE_BIN=os.path.join(HERE, "fake", "claude"), RELAY_PORT=port, AGENT_UID="",
                     FIX_ATTEMPTS=1, ANTHROPIC_API_KEY="sk-factice-de-test", REVIEW_MODE="agent").items():
        monkeypatch.setattr(rconf, k, v)
    tok = work.create_runner("runner-e2e", "conteneur", "alice")
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
    assert j["decision"]["confident"] and j["diff"]["files"] == ["carnet.py"]
    assert j["cost"] > 0 and j["agents"][0]["name"] == "session principale"
    assert any(e["kind"] == "edit" for e in j["events"])
    # la branche a été poussée par le runner (pas par l'agent)
    assert subprocess.run(["git", "rev-parse", "--verify", f"fabrique/{ref}"], cwd=forge.bare, capture_output=True).returncode == 0
    body = forge.prs[1]["body"]                                               # guide de relecture dans la PR
    assert "### Pour relire" in body and "**Effort de relecture : 1/5 (trivial)** · 1 fichier(s)" in body
    assert "**Par où commencer** : `carnet.py`" in body and "**Vérifications** :" in body
    jobs.decide(VERIF, ref, "approve")
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "merged" and forge.merged and forge.merged[0][1].startswith(j["verified_sha"])
    assert forge.merge_message == f"Fabrique-Job: {ref}"                      # le commit de fusion renvoie à son job


def test_failing_checks_get_one_fix_then_pr(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "CASSER puis réparer", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["attempts"] == 2
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert lim["Checks verts"] and not lim["Premier essai"]


def test_merge_refused_if_pr_moved(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    step(r, panel)
    with db.tx() as c:
        c.execute(update(db.jobs).values(verified_sha="0000000"))
    jobs.decide(VERIF, ref, "approve")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "a changé" in j["status_text"] and not forge.merged


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
    assert latest == {"code": "ok", "securite": "ok"}
    assert any(rv["verdict"] == "bloquant" for rv in j["reviews"])          # la première relecture reste dans l'historique
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert lim["Vérification"] and not lim["Premier essai"]
    assert "Décision de la Fabrique" in forge.comments[1] and "Vérification" in forge.comments[1]


@pytest.mark.parametrize("title", ["Réparer SANSPREUVE", "Réparer PREUVEVIDE"])
def test_unproven_blocking_review_is_downgraded_without_fix_round(setup, repo, title):
    r, forge, panel = setup
    ref = jobs.create(REQ, title, "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["attempts"] == 1                            # pas de ronde de correction
    verdicts = {rv["verdict"] for rv in j["reviews"]}
    assert verdicts == {"a_revoir"}                                              # ni bloquant, ni « ok »
    assert all("non démontré" in rv["summary"] for rv in j["reviews"])
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert not lim["Vérification"]                                                 # la fusion seule reste impossible


def test_unreadable_review_is_retried_once(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ILLISIBLE", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert {rv["verdict"] for rv in j["reviews"]} == {"ok"}
    assert any("nouvelle tentative" in (e["message"] or "") for e in j["events"])


def test_unreadable_review_says_why_and_can_be_rerun(setup, repo):
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ILLISIBLE TOUJOURS", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["merge_state"] == "ready" and {rv["verdict"] for rv in j["reviews"]} == {"indetermine"}
    assert all("aucun verdict JSON" in rv["summary"] and "pas un avis négatif" in rv["summary"] for rv in j["reviews"])
    todo = jobs.to_check(j)
    assert any("Vérification code sans verdict" == t["title"] and "justesse" in t["text"] for t in todo)
    for url in (f"/inbox/{ref}/panel", f"/jobs/{ref}"):
        html = web("admin").get(url).text
        assert "À vérifier avant d&#39;approuver" in html or "À vérifier avant d'approuver" in html, url
        assert "Relancer la vérification" in html and f"/jobs/{ref}/reviews" in html
    assert f'data-copy="#ev-{ref}"' in web("admin").get(f"/jobs/{ref}").text                 # bouton « copier le journal »
    with pytest.raises(jobs.Refused):
        jobs.rereview(REQ, ref)                                                  # un demandeur ne relance pas
    jobs.rereview(VERIF, ref)
    assert jobs.get(ADMIN, ref)["merge_state"] == "reviewing"
    with pytest.raises(jobs.Refused):
        jobs.decide(VERIF, ref, "approve")                                        # pas de décision pendant la relecture
    with db.tx() as c:                                                           # la demande ne contient plus le mot magique
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(title="Réparer"))
    attempts = jobs.get(ADMIN, ref)["attempts"]
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and j["attempts"] == attempts
    cur = {rv["role"]: rv["verdict"] for rv in j["reviews"] if rv["sha"] == j["verified_sha"]}
    assert cur == {"code": "ok", "securite": "ok"}
    assert {l["name"]: l["ok"] for l in j["decision"]["limits"]}["Vérification"]
    assert "Vérification : code : OK" in forge.comments[j["pr_number"]] or "✅ **Vérification**" in forge.comments[j["pr_number"]]


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
    ref = jobs.create(REQ, "Réparer LONG", "B", "", repo)
    item = panel.claim()
    jobs.cancel(VERIF, ref)
    threading.Timer(2, lambda: [r.cancel(x) for x in panel.heartbeat({}).get("cancel", [])]).start()
    r.active += 1
    r.execute(item)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "Arrêté par Vera" in j["status_text"]


def test_daily_budget_pauses_the_agent_and_the_job_resumes_where_it_was(setup, repo):
    import threading
    from sqlalchemy import select, update
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer LONG", "B", "", repo)
    item = panel.claim()
    with db.tx() as c:
        db.set_setting(c, "limits", dict(db.DEFAULTS["limits"], budget_day=1.0), "t")
    cher = jobs.create(REQ, "Cher", "B", "", repo)                                         # un autre job a mangé le budget du jour
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == cher).values(cost=1.5, stage="merged"))
        c.execute(update(db.intents).where(db.intents.c.state == "pending").values(state="done"))
    marker = os.path.join(rconf.WORK_DIR, ref, "repo", "brouillon.txt")
    threading.Timer(1, lambda: open(marker, "w").write("travail en cours")).start()       # du travail pas encore commité
    threading.Timer(2, lambda: r.orders(panel.heartbeat({}))).start()
    r.active += 1
    r.execute(item)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] != "failed" and j["status_text"] == work.PAUSED and j["session_id"] == "sess-42"
    with db.tx() as c:
        its = db.rows(c, select(db.intents).where(db.intents.c.state == "pending"))
    assert [(i["kind"], i["payload"]) for i in its] == [("resume", {"pause": "budget"})]
    assert panel.claim() is None and os.path.exists(marker)                                # budget toujours dépassé : la reprise attend
    with db.tx() as c:
        db.set_setting(c, "limits", dict(db.DEFAULTS["limits"], budget_day=50.0), "t")
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and os.path.exists(marker)                                   # reprise sur le même espace de travail


def test_study_renders_a_report_and_pushes_nothing(setup, repo):
    r, forge, panel = setup
    heads = lambda: subprocess.run(["git", "for-each-ref"], cwd=forge.bare, capture_output=True, text=True).stdout
    before = heads()
    ref = jobs.create(REQ, "Pourquoi ok() est faux ?", "Explique", "", repo, study=True)
    item = step(r, panel)
    assert item["job"]["source"] == "étude"
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "done" and j["status_text"] == "Étude rendue" and "ok() renvoie False" in j["report"]
    assert heads() == before and not forge.prs and not j["pr_number"] and not j["evidence"]     # ni branche, ni PR, ni checks
    assert [t["stage"] for t in j["timeline"]] == ["triage", "investigating", "done"]
    page = web("admin").get(f"/jobs?job={ref}").text
    assert "Rapport d'étude" in page and "&lt;script&gt;" in page and "<script>alert(1)" not in page   # texte de l'agent inerte
    assert f"/new?from={ref}" in page
    form = web("admin").get(f"/new?from={ref}").text
    assert f"Suite de {ref}" in form and "Conclusions de l" in form and "ok() renvoie False" in form
    assert "Suite de" not in web("admin").get("/new?from=FAB-999").text
    assert "Suite de" not in web("requester").get(f"/new?from={ref}").text                  # l'étude d'un autre reste invisible
    with pytest.raises(work.Denied):                                                        # un job ordinaire ne rend pas de rapport
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
    assert j2["stage"] == "failed" and "aucun rapport" in j2["status_text"]


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
        jobs.create(REQ, "Réparer", "B", "", rid)                                 # pas de job avant l'onboarding
    with pytest.raises(jobs.Refused):
        jobs.onboard(VERIF, rid)                                                  # admin seulement
    ref = jobs.onboard(ADMIN, rid)
    item = step(r, panel)
    assert item["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["merge_state"] == "ready" and j["diff"]["files"] == [".fabrique/setup.json"]
    assert j["evidence"] and all(e["ok"] for e in j["evidence"])                  # recette rejouée par le runner
    lim = {l["name"]: l["ok"] for l in j["decision"]["limits"]}
    assert "Vérification" not in lim and not lim["Protected paths"]               # jamais de fusion seule, même en « confiant »
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert repo["onboarding"] == "analyzing" and repo["recipe"]["checks"] == {"tests": "python3 -c 'import carnet'"}
    assert repo["recipe"]["env"][0]["name"] == "API_KEY"
    jobs.decide(VERIF, ref, "approve")
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "ready"
    jobs.create(REQ, "Réparer", "B", "", rid)                                     # le dépôt accepte maintenant les jobs


def test_a_recipe_without_checks_can_still_be_merged_after_human_approval(setup, tmp_path):
    """Projet sans tests ni build : la recipe ne déclare aucun check. Le commit vérifié doit quand même être enregistré,
    sinon le merge approuvé échoue (« pas de sha vérifié »)."""
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
    assert j["stage"] == "pr" and j["diff"]["files"] == [".fabrique/setup.json"]          # code et .venv écartés
    assert any("écartés" in (e["message"] or "") and "carnet.py" in e["message"] and ".venv" in e["message"] for e in j["events"])


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
    monkeypatch.setattr(forge, "clone_url", lambda slug: str(tmp_path / "absent.git"))     # pas d'accès
    ref = jobs.onboard(ADMIN, rid)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "waiting"
    assert j["stage"] == "queued" and j["status_text"].startswith("En attente d'accès") and "collaborateur" in j["status_text"]
    assert jobs.onboard(ADMIN, rid) == ref                                       # « Réessayer » : même job, pas un nouveau
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)                  # accès donné, mais dépôt vide
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["status_text"].startswith("Rien à analyser")
    assert jobs.onboard(ADMIN, rid) == ref
    assert jobs.onboard(ADMIN, rid) == ref                                       # clics répétés : un seul essai en file
    with db.tx() as c:
        n = len(db.rows(c, select(db.intents).where(db.intents.c.state == "pending")))
        assert n == 1
    step(r, panel)
    # nouvel essai automatique : rien tant que le délai n'est pas passé, puis remis en file
    assert panel.claim() is None
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(updated_at=0))
    monkeypatch.setattr(forge, "clone_url", lambda slug: forge.bare)            # premier commit poussé (avec recette)
    step(r, panel)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "ready"
        assert len(db.rows(c, select(db.jobs).where(db.jobs.c.source == "onboarding", db.jobs.c.repo_id == rid))) == 1
    html = web("admin").get(f"/repos?repo={rid}").text
    assert "Prêt" in html


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
    assert "Initialise-le" in jobs.get(ADMIN, ref)["status_text"]
    assert "Initialiser le repo" in web("admin").get(f"/repos?repo={rid}").text
    with pytest.raises(jobs.Refused):
        jobs.init_repo(VERIF, rid)                                                 # validation par un admin
    assert jobs.init_repo(ADMIN, rid) == ref
    item = step(r, panel)
    assert item["intent"]["kind"] == "init"
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=empty, capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".fabrique/setup.json", "README.md"]
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "blank"
    assert jobs.get(ADMIN, ref)["stage"] == "merged"
    # première demande dans le dépôt vierge : PR sans contrôles, donc relecture humaine
    first = jobs.create(REQ, "Réparer", "Crée carnet.py avec ok()", "", rid)
    step(r, panel)
    j = jobs.get(ADMIN, first)
    lim = {l["name"]: l for l in j["decision"]["limits"]}
    assert j["stage"] == "pr" and not lim["Checks verts"]["ok"] and "aucun check" in lim["Checks verts"]["detail"]
    jobs.decide(VERIF, first, "approve")
    step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "merged"
    # la fusion relance l'onboarding : l'agent écrit la vraie recette
    with db.tx() as c:
        onb = db.rows(c, select(db.jobs).where(db.jobs.c.repo_id == rid, db.jobs.c.source == "onboarding").order_by(db.jobs.c.id))
    assert len(onb) == 2 and onb[1]["stage"] == "queued"
    item = step(r, panel)
    assert item["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb[1]["ref"])
    assert j["stage"] == "pr" and j["diff"]["files"] == [".fabrique/setup.json"]          # recette vide remplacée par la vraie


def test_a_repo_with_only_a_readme_is_initialised_like_an_empty_one(setup, tmp_path, monkeypatch):
    """Repo créé sur la forge avec son README (ou une licence) : pas de projet à analyser, donc pas d'onboarding par l'agent."""
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
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))["onboarding"] == "empty"     # et non « failed »
    assert jobs.get(ADMIN, ref)["status_text"].startswith("Rien à analyser")
    assert "Initialiser le repo" in web("admin").get(f"/repos?repo={rid}").text
    assert jobs.init_repo(ADMIN, rid) == ref
    assert step(r, panel)["intent"]["kind"] == "init"
    ls = lambda: sorted(subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare_repo, capture_output=True, text=True).stdout.split())
    assert ls() == [".fabrique/setup.json", ".gitignore", "LICENSE", "README.md"]
    readme = subprocess.run(["git", "show", "main:README.md"], cwd=bare_repo, capture_output=True, text=True).stdout
    assert readme == "# Mon projet\n"                                                               # le README existant est gardé
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
    """Pousse un commit directement sur main du dépôt de test (ajoute le fichier, ou le retire si content est None)."""
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
    """Le parcours d'une première installation : repo vide, initialisation, première demande, puis un onboarding qui
    n'aboutit pas (projet sans tests : l'agent abandonne). Le repo ne doit jamais se bloquer."""
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
    first = jobs.create(REQ, "Réparer", "Crée carnet.py avec ok()", "", rid); step(r, panel)
    jobs.decide(VERIF, first, "approve"); step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "merged"
    with db.tx() as c:
        onb = db.rows(c, select(db.jobs).where(db.jobs.c.repo_id == rid, db.jobs.c.source == "onboarding").order_by(db.jobs.c.id))[-1]
    assert onb["ref"] != ref0 and onb["stage"] == "queued" and state() == "blank"          # relancé, sans bloquer le repo
    why = [e for e in jobs.get(ADMIN, onb["ref"])["events"] if e["kind"] == "onboarding" and e["message"].startswith("lancé par la Fabrique après")]
    assert len(why) == 1                                                                    # l'explication n'est écrite qu'une fois
    second = jobs.create(REQ, "Réparer encore", "B", "", rid)                               # demande acceptée pendant l'onboarding
    _commit_on_main(empty, tmp_path, "RIEN_A_VERIFIER", "x\n")                             # l'agent d'onboarding abandonnera
    assert step(r, panel)["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "failed" and "reste utilisable" in j["status_text"]
    assert state() == "blank"                                                               # ni « failed », ni « pas prêt »
    page = web("admin").get(f"/repos?repo={rid}").text
    assert "Repo vierge" in page and "Dernier essai d" in page and f'action="/repos/{rid}/onboard"' in page
    step(r, panel)
    assert jobs.get(ADMIN, second)["stage"] == "pr"                                         # la demande aboutit normalement
    # l'onboarding est retenté après la merge suivante ; cette fois il aboutit, et la PR de recipe n'empêche rien
    _commit_on_main(empty, tmp_path, "RIEN_A_VERIFIER", None)
    jobs.decide(VERIF, second, "approve"); step(r, panel)
    assert jobs.get(ADMIN, second)["stage"] == "merged"
    assert step(r, panel)["intent"]["kind"] == "onboard"
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "pr" and j["verified_sha"] and state() == "blank"
    third = jobs.create(REQ, "Troisième", "B", "", rid)                                     # toujours utilisable, PR de recipe en attente
    step(r, panel)
    jobs.decide(VERIF, third, "approve"); step(r, panel)
    assert jobs.get(ADMIN, third)["stage"] == "merged" and panel.claim() is None           # pas de nouvel onboarding : une PR attend
    jobs.decide(ADMIN, onb["ref"], "approve")
    item = step(r, panel)
    j = jobs.get(ADMIN, onb["ref"])
    assert j["stage"] == "merged" and state() == "ready" and not jobs.is_blank(db_repo(rid)), (j["stage"], j["status_text"], state())


def test_init_writes_nothing_as_the_runner_user():
    """Régression : en production l'agent a un autre uid que le runner ; tout fichier du workspace est créé par
    sh() (sous l'utilisateur de l'agent), jamais par le runner lui-même (sinon « Permission denied »)."""
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
    ex = executor.Docker(base="http://lanceur", token="t" * 40,
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
    with db.tx() as c:                                                          # agent du job : Codex, modèle OpenAI
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(harness="codex", model="gpt-5-mini"))
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "pr" and j["session_id"] == "thr-codex-1" and j["evidence"] and all(e["ok"] for e in j["evidence"])
    assert any(e["kind"] == "bash" and "ls" in e["message"] for e in j["events"])
    # Passage de l'agent : 50 000 jetons d'entrée + 50 000 en cache + 10 000 en sortie, au tarif gpt-5-mini ; les relecteurs
    # (lancés ici en mode agent avec le même faux Codex) comptent EN PLUS dans le coût du job (audit C11).
    agent_cost = (50000 * 0.25 + 50000 * 0.025 + 10000 * 2) / 1e6
    assert j["cost"] > agent_cost + 1e-4


def test_preparing_a_job_never_kills_other_jobs_processes(setup, repo, monkeypatch):
    """Régression FAB-266 : en mode docker, ou si un autre job tourne, la préparation ne tue aucun processus de l'agent."""
    r, forge, panel = setup
    killed = []
    monkeypatch.setattr(rmain, "kill_agent_leftovers", lambda: killed.append(1))
    ref = jobs.create(REQ, "Réparer", "B", "", repo)
    r.active = 1                                       # un autre job tourne déjà
    step(r, panel)
    assert not killed
    assert jobs.get(ADMIN, ref)["stage"] == "pr"       # et le job va quand même au bout (clonage compris)
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
    monkeypatch.setattr(forge, "create_repo", lambda ns, name, desc="": created.append((ns, name, desc)) or "créé", raising=False)
    monkeypatch.setattr(forge, "clone_url", lambda slug: empty)
    with pytest.raises(jobs.Refused, match="création de repos"):
        jobs.request_repo(VERIF, "forgejo", "api-notes")                          # aucun groupe configuré
    with db.tx() as c:
        db.set_setting(c, "creation", {"forgejo": "fabrique"}, "alice")
    with pytest.raises(jobs.Refused):
        jobs.request_repo(REQ, "forgejo", "api-notes")                           # un demandeur ne demande pas de dépôt
    with pytest.raises(jobs.Refused):
        jobs.request_repo(VERIF, "forgejo", "Nom Invalide")
    rid = jobs.request_repo(VERIF, "forgejo", "api-notes", "petite API")
    assert "fabrique/api-notes" in web("admin").get("/inbox").text and not created and panel.claim() is None
    with pytest.raises(jobs.Refused):
        jobs.decide_repo(VERIF, rid, True)                                        # on ne valide pas sa propre demande
    assert 'Créer le repo' in web("admin").get(f"/repos?repo={rid}").text    # boutons sur la fiche du dépôt
    # 1er essai : le fournisseur refuse (jeton sans droit) → échec ; « Relancer la création » recrée, pas un onboarding
    monkeypatch.setattr(forge, "create_repo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")), raising=False)
    ref = jobs.decide_repo(ADMIN, rid, True)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "failed" and "Relancer la création" in web("admin").get(f"/repos?repo={rid}").text
    monkeypatch.setattr(forge, "create_repo", lambda ns, name, desc="": created.append((ns, name, desc)) or "créé", raising=False)
    assert jobs.onboard(ADMIN, rid) == ref
    item = step(r, panel)
    assert item["intent"]["kind"] == "create" and created == [("fabrique", "api-notes", "petite API")]
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=empty, capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".fabrique/setup.json", "README.md"]
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert repo["onboarding"] == "blank" and repo["accepts_jobs"] and jobs.get(ADMIN, ref)["stage"] == "merged"
    # refus : la demande disparaît, rien n'est créé
    rid2 = jobs.request_repo(VERIF, "forgejo", "autre")
    jobs.decide_repo({"user": "vic", "name": "Vic", "role": "verifier"}, rid2, False, "pas besoin")   # un autre vérificateur
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
        assert d in path                                                         # vu sur FAB-275 : « go: not found »


def test_review_effort_criteria_and_tool_hints():
    """Guide de relecture : effort calculé sans modèle, critères de la demande lus dans les points du relecteur code, et
    résultats du contrôle automatique donnés au relecteur sécurité comme indices."""
    from runner import review
    assert [review.effort(*a) for a in ((10, 1), (100, 4), (250, 8), (700, 12), (5000, 40))] == [1, 2, 3, 4, 5]
    assert review.effort(10, 1, protected=True) == 2 and review.effort(5000, 40, protected=True) == 5
    pts = ["Critère traité : pytest passe sur les deux tests.", "critere non traité : la liste vide n'est pas couverte",
           "Critère pas clair : « rapide » n'est pas mesurable", "Un autre point sans rapport"]
    assert review.criteria(pts) == [("ok", "pytest passe sur les deux tests."), ("non", "la liste vide n'est pas couverte"),
                                    ("flou", "« rapide » n'est pas mesurable")]

    class Ctx:
        title, job = "T", {"request": "R", "done_when": "D"}
        security_report = "Contrôle de sécurité : 1 alerte(s)\nSecrets écrits en dur :\n  ⚠ app.py ligne 2 (règle stripe-access-token)"
    _, _, sec = review.prompts(Ctx(), "securite", "diff", "direct")
    _, _, code = review.prompts(Ctx(), "code", "diff", "direct")
    assert "<controle_automatique>" in sec and "stripe-access-token" in sec and "Écarte les faux positifs" in sec
    assert "<controle_automatique>" not in code and "Critère non traité" in code
    Ctx.security_report = ""
    assert "<controle_automatique>" not in review.prompts(Ctx(), "securite", "diff", "direct")[2]


def test_network_line_in_the_evidence_warns_on_refusals():
    """Sorties réseau du job dans les preuves : informatif, avec alerte s'il y a eu des refus ; rien si le lanceur ne sait pas."""
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
    assert n["name"] == "Réseau" and n["ok"] and n["warn"] and n["output"].startswith("Sorties réseau : 2 refusée(s), 3 autorisée(s)")
    assert "⚠ evil.test (2 fois)" in n["output"] and "· pypi.org (3 fois)" in n["output"]
    run.ex = Ex({"known": True, "allowed": [["pypi.org", 1]], "denied": []})
    n = run.network(ctx)
    assert not n["warn"] and n["output"].startswith("Sorties réseau : 1 autorisée(s), aucun refus")
    run.ex = Ex(None)
    assert run.network(ctx) is None
    from app import jobs
    warn = {"name": "Réseau", "sha": "abc1234", "output": "Sorties réseau : 2 refusée(s), 3 autorisée(s)\n…"}
    assert jobs.security_warn(warn) and not jobs.security_warn(dict(warn, output="Sorties réseau : 3 autorisée(s), aucun refus"))
    todo = jobs.to_check({"verified_sha": "abc1234", "evidence": [warn], "decision": {"limits": []}})
    assert todo[0]["title"].startswith("Sorties réseau : 2 refusée(s)") and "liste blanche du proxy" in todo[0]["text"]


def test_runner_purges_only_finished_workspaces(tmp_path, monkeypatch):
    """Ménage : le runner supprime le dossier et le miroir d'un job terminé ; jamais un job en cours, jamais hors du dossier."""
    from runner import main as rm
    tmp_path = tmp_path / "travail"
    tmp_path.mkdir()
    monkeypatch.setattr(rm.config, "WORK_DIR", str(tmp_path))
    monkeypatch.setattr(rm, "as_agent", lambda: [])
    for n in ("FAB-1", "FAB-2", ".miroirs/FAB-1.git", ".miroirs/FAB-2.git", "autre"):
        (tmp_path / n).mkdir(parents=True)
        (tmp_path / n / "f").write_text("x")
    run = rm.Runner.__new__(rm.Runner)
    run.running = {"FAB-2": object()}
    assert run.workspaces() == ["FAB-1"]                                           # ni le job en cours, ni les autres dossiers
    for ref in ("FAB-1", "FAB-2", "../autre", "autre", ".miroirs"):
        run.purge(ref)
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.iterdir())
    assert left == [".miroirs", "FAB-2", "autre"] and [p.name for p in (tmp_path / ".miroirs").iterdir()] == ["FAB-2.git"]
