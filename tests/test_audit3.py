"""Non-régression de l'audit de sécurité du 15/11/2026 : un test par faille corrigée (C1 à C8) et par piste fermée (V3, V4)."""
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


# ---------- C1 : les fichiers de tokens ne sont jamais lisibles par tous ----------

def test_bootstrap_runner_token_file_is_group_only_and_follows_rotation(tmp_path, monkeypatch):
    path = str(tmp_path / "jeton-runner")
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_FILE", path)
    work.bootstrap_runner()
    first = open(path).read().strip()
    assert _mode(path) == 0o640 and work.runner_for(first)["name"] == "runner-integre"
    os.chmod(path, 0o644)                                    # fichier laissé par une ancienne version
    work.bootstrap_runner()
    assert _mode(path) == 0o640 and open(path).read().strip() == first      # resserré, token inchangé
    with db.tx() as c:
        rid = db.one(c, select(db.runners.c.id).where(db.runners.c.name == "runner-integre"))["id"]
    _, new = work.rotate_runner(rid, "alice")
    assert open(path).read().strip() == new != first and _mode(path) == 0o640   # le fichier suit le renouvellement
    assert work.runner_for(first) is None and work.runner_for(new)["name"] == "runner-integre"


def test_launcher_token_file_is_owner_only(tmp_path, monkeypatch):
    from lanceur import main as L
    path = str(tmp_path / "jeton")
    monkeypatch.delenv("LAUNCHER_TOKEN", raising=False)
    monkeypatch.setenv("LAUNCHER_TOKEN_FILE", path)
    tok = L._token()
    assert len(tok) >= 32 and _mode(path) == 0o600
    os.chmod(path, 0o644)                                    # fichier laissé par une ancienne version
    assert L._token() == tok and _mode(path) == 0o600


# ---------- C2 : jamais de secret de session vide ----------

def test_dashboard_refuses_to_start_without_a_usable_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    good = config._secret("SESSION_SECRET")
    assert len(good) >= 48 and not os.path.exists(str(tmp_path / "secrets.json.tmp"))     # écriture atomique, rien ne traîne
    (tmp_path / "secrets.json").write_text("{pas du json")
    with pytest.raises(SystemExit, match="SESSION_SECRET"):
        config._secret("SESSION_SECRET")                                                   # fichier abîmé : refus, pas de secret vide
    (tmp_path / "secrets.json").write_text("")
    assert len(config._secret("SESSION_SECRET")) >= 48                                     # fichier vide (écriture ratée) : régénéré
    ro = tmp_path / "lecture-seule"; ro.mkdir(); ro.chmod(0o500)
    monkeypatch.setattr(config, "DATA_DIR", str(ro))
    if os.geteuid() != 0:
        with pytest.raises(SystemExit):
            config._secret("SESSION_SECRET")                                               # dossier non inscriptible : refus
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
    assert cl.get("/settings?tab=users", headers={"accept": "text/html"}).status_code == 503   # session forgée : rien ne s'ouvre
    monkeypatch.setattr(config, "SESSION_SECRET", "court")
    auth.invalidate()
    assert auth.mode() == "ko"


# ---------- C3 : la barrière « Protected paths » ne se fie pas à une liste coupée ----------

def test_protected_paths_fail_closed_on_what_cannot_be_compared():
    pats = ["**/.env", "**/migrations/**", ".github/**"]
    assert work.protected_hits(["src/a.py"], pats) == []
    long_env = "/".join(["d" * 60] * 5) + "/.env"
    assert len(long_env) > work.MAX_PATH and work.protected_hits([long_env[:work.MAX_PATH]], pats)      # chemin coupé : touché
    assert work.protected_hits(["db/migrations/0002\n.sql"], pats) and work.protected_hits([".github/workflows/x\n.yml"], pats)
    assert work.protected_hits(["a\tb.py"], pats)                                                       # caractère de contrôle


def test_more_files_than_the_dashboard_keeps_never_auto_merges(runner, repo):  # noqa: F811
    _policy(repo, "confident")
    a = api(runner)
    ok = jobs.create(REQ, "Petit", "B", "", repo)
    _to_pr(a, ok, ["assets/a.png"])
    assert jobs.get(ADMIN, ok)["merge_state"] == "approved"                                             # témoin : tout tient
    big = jobs.create(REQ, "Gros", "B", "", repo)
    _to_pr(a, big, [f"assets/img_{i:04d}.png" for i in range(500)] + ["src/auth/login.py"], add=2)
    j = jobs.get(ADMIN, big)
    gate = next(l for l in j["decision"]["limits"] if l["name"] == "Protected paths")
    assert j["merge_state"] == "ready" and not gate["ok"] and "liste incomplète" in gate["detail"]
    with db.tx() as c:
        assert not db.rows(c, select(db.intents).where(db.intents.c.kind == "merge", db.intents.c.state == "pending",
                                                       db.intents.c.job_id == j["id"]))
    nl = jobs.create(REQ, "Retour à la ligne", "B", "", repo)
    _to_pr(a, nl, ["db/migrations/0002\n.sql"])
    assert jobs.get(ADMIN, nl)["merge_state"] == "ready"


# ---------- C4 : aucun identifiant du runner dans l'environnement d'un job ----------

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
    assert env == {"ANTHROPIC_MODEL": "claude-x"}                                          # sans clé : rien, pas même un repli


def test_anthropic_model_without_a_key_fails_before_the_agent_starts(setup, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rconf, "ANTHROPIC_API_KEY", "")
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "aucune clé d'API" in j["status_text"]


# ---------- C5 / C6 : passerelle de clés ----------

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
    """Requête brute : le chemin part tel quel (pas de normalisation côté client)."""
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
    assert seen == []                                                                       # rien n'a atteint le fournisseur
    assert _raw(port, "POST", "/m/v1/messages?beta=true") == 200 and _raw(port, "POST", "/m/v1/messages/count_tokens") == 200
    assert seen == [("POST", "/v1/messages?beta=true"), ("POST", "/v1/messages/count_tokens")]
    gw.server.shutdown()


def test_gateway_charges_answers_without_usage_and_holds_concurrent_calls():
    ctx = GwCtx(budget=1.0)
    gw, port = _gw(ctx, lambda req: httpx.Response(200, text="x" * 4000))                  # réponse payante sans compteur d'usage
    assert _raw(port, "POST", "/m/v1/messages") == 200 and ctx.gw_spent > 0                 # jamais zéro
    gw.server.shutdown()

    ctx = GwCtx(budget=1.0, base=0.99)                                                      # un centime de marge

    def slow(req):
        time.sleep(0.4)
        return httpx.Response(200, json={"usage": {"input_tokens": 200000, "output_tokens": 0}})
    gw, port = _gw(ctx, slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(_raw(port, "POST", "/m/v1/messages"))) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert out.count(200) == 1 and out.count(402) == 11 and getattr(ctx, "gw_reserved", 0) == 0   # un seul passe sur le dernier centime
    gw.server.shutdown()


def test_spend_made_during_checks_reaches_the_job_cost(setup, repo, monkeypatch):  # noqa: F811
    """Un check appelle le modèle avec le jeton du job : cette dépense, mesurée par la passerelle hors de tout passage
    d'agent, doit se retrouver dans le coût du job (donc dans le budget du jour)."""
    r, forge, panel = setup
    real = rmain.job

    def spending(ctx, cmd, **kw):
        ctx.gw_spent += 3.0                                   # ce que la passerelle a mesuré pendant ce check
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
    assert j["stage"] == "failed" and j["cost"] >= 2.0, j["cost"]     # remontée avant la clôture de la commande


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
    assert _raw(port, "POST", "/m/v1/messages/count_tokens") == 200 and ctx.gw_spent == 0.0   # comptage de jetons : gratuit
    gw.server.shutdown()


# ---------- C7 : le git du runner ne lit plus la configuration du dépôt de l'agent ----------

class FakeDocker(executor.Local):
    """Exécuteur « conteneur de job » simulé : mêmes effets que l'atelier local, mais il note ce qu'on lui fait exécuter."""
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
    assert {"status", "bundle"} <= in_job                                 # dépôt de l'agent : côté job
    assert set(in_runner) <= {"clone", "remote", "checkout"}, in_runner   # côté runner : seulement avant le passage de l'agent


def test_job_side_git_environment_fits_the_launcher_limits(monkeypatch):
    from lanceur import main as L
    monkeypatch.setattr(rconf, "JOB_PROXY", "http://proxy:3128")
    env = ragent.base_env("/work/FAB-1/home", rmain.job_git_env())
    assert len(env) <= 48, len(env)                                       # le lanceur refuse au-delà de 64 variables
    assert all(L.ENV_KEY.match(k) and k not in L.FORBIDDEN_ENV and len(v) <= 20_000 for k, v in env.items())
    cfg = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert cfg["core.hooksPath"] == "/dev/null" and cfg["commit.gpgSign"] == "false" and cfg["gpg.program"] == "false"


def test_a_workspace_that_cannot_be_emptied_is_never_reused(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    first = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    assert jobs.get(ADMIN, first)["stage"] == "pr"
    real = rmain.sh

    def stubborn(ctx, cmd, **kw):                                         # le vidage n'a aucun effet (dossier verrouillé par l'agent)
        if isinstance(cmd, list) and "wipe" in cmd:
            return executor.Result(0)
        return real(ctx, cmd, **kw)
    monkeypatch.setattr(rmain, "sh", stubborn)
    jobs.rereview(ADMIN, first)                                           # reconstruit l'espace de travail du même job
    step(r, panel)
    with db.tx() as c:
        last = db.rows(c, select(db.events).where(db.events.c.job_id == jobs.get(ADMIN, first)["id"]).order_by(db.events.c.id.desc()))[0]
    assert "impossible à vider" in json.dumps([dict(e) for e in jobs.get(ADMIN, first)["events"]], default=str, ensure_ascii=False) \
        or "impossible à vider" in jobs.get(ADMIN, first)["status_text"], last


def test_commit_bundle_must_be_a_regular_file(setup, repo, monkeypatch, tmp_path):  # noqa: F811
    r, forge, panel = setup
    secret = tmp_path / "ailleurs"; secret.write_text("pas un paquet")
    real = rmain.jgit

    def swap(ctx, args, **kw):
        p = real(ctx, args, **kw)
        if args[0] == "bundle":                                           # un processus du job remplace le paquet par un lien
            path = args[3]
            os.remove(path); os.symlink(str(secret), path)
        return p
    monkeypatch.setattr(rmain, "jgit", swap)
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    assert j["stage"] == "failed" and "paquet de commits" in j["status_text"]


# ---------- C8 : le vérificateur en mode agent ne doit rien au job qu'il juge ----------

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
        review_dir, review_home = str(tmp_path / ".verif-abc"), str(tmp_path / ".verif-home-abc")
        ex = Ex()
    out = review.run(C, "code", "diff")
    argv, env, cwd = calls[0]
    assert out["verdict"] == "ok" and os.path.isabs(argv[0]) and argv[0] == rconf.CLAUDE_BIN
    assert env["PATH"] == review.REVIEW_PATH and C.home not in env["PATH"]                 # aucun dossier du job dans le PATH
    assert env["HOME"] == C.review_home and cwd == C.review_dir and env["FAB_WORKSPACE"] == C.review_dir
    assert os.path.isabs(rconf.CODEX_BIN)


def test_reviewer_reads_an_export_of_the_verified_commit(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    seen = {}
    real = review.run

    def spy(ctx, role, diff):
        seen[role] = (ctx.review_dir, ctx.review_home, sorted(os.listdir(ctx.review_dir)),
                      os.path.exists(os.path.join(ctx.review_dir, "brouillon-du-job.txt")))
        return real(ctx, role, diff)
    monkeypatch.setattr(review, "run", spy)
    real_checks = r.checks

    def checks(ctx):   # un check laisse un fichier non commité dans l'arbre de travail
        out = real_checks(ctx)
        open(os.path.join(ctx.repo_dir, "brouillon-du-job.txt"), "w").write("x")
        return out
    r.checks = checks
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"
    rdir, rhome, files, leaked = seen["code"]
    assert "carnet.py" in files and not leaked and ".git" not in files            # le commit vérifié, pas l'arbre de travail
    assert not {".claude", ".mcp.json", "CLAUDE.md"} & set(files)                 # ni les réglages de projet du commit jugé
    assert os.path.basename(rdir).startswith(".verif-") and not os.path.exists(rdir) and not os.path.exists(rhome)   # nettoyé


def test_nothing_survives_a_replayed_proof(setup, repo, monkeypatch):  # noqa: F811
    r, forge, panel = setup
    order = []
    real_job, real_kill = rmain.job, r.ex.kill
    monkeypatch.setattr(rmain, "job", lambda ctx, cmd, **kw: (order.append("job:" + cmd), real_job(ctx, cmd, **kw))[1])
    r.ex.kill = lambda ctx: (order.append("kill"), real_kill(ctx))[1]
    ref = jobs.create(REQ, "Réparer BLOQUER", "B", "", repo)               # un vérificateur bloque, avec une preuve à rejouer
    step(r, panel)
    assert jobs.get(ADMIN, ref)["stage"] == "pr"
    proofs = [i for i, e in enumerate(order) if e.startswith("job:grep")]
    assert proofs and all(order[i + 1] == "kill" for i in proofs), order     # chaque preuve rejouée est suivie d'un arrêt des processus
    first_check = next(i for i, e in enumerate(order) if e.startswith("job:python3"))
    assert "kill" in order[first_check:]                                     # et les processus des checks sont arrêtés avant la vérification


# ---------- V3 : un « ok » sur un diff tronqué ne vaut pas pour le tout ----------

def test_ok_on_a_truncated_diff_is_not_an_ok(setup, repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(rconf, "REVIEW_MAX_DIFF", 40)
    r, forge, panel = setup
    ref = jobs.create(REQ, "Réparer ok()", "B", "", repo)
    step(r, panel)
    j = jobs.get(ADMIN, ref)
    latest = {rv["role"]: rv for rv in j["reviews"] if rv["sha"] == j["verified_sha"]}
    assert {v["verdict"] for v in latest.values()} == {"indetermine"} and "vérification incomplète" in latest["code"]["summary"]
    assert not {l["name"]: l["ok"] for l in j["decision"]["limits"]}["Vérification"]


# ---------- V4 : aucune forme de lien ne survit dans le texte rendu inerte ----------

@pytest.mark.parametrize("text", ["[Voir les journaux][1]\n\n[1]: //evil.example/login", "[label]\n\n[label]: /autre/depot/pulls/1",
                                  "voir www.evil.example/login", "&commat;admin &num;12 https&colon;//evil.example",
                                  "[x](https://evil.example) @admin #12 ![i](u) <b>"])
def test_inert_leaves_no_link_form(text):
    out = messages.inert(text)
    for live in ("][", "]:", "](", "www.", "://", "@a", "#1", "![", "<", "&commat;", "&num;", "&colon;"):
        assert live not in out, (live, out)
    assert messages.quote(text).startswith("> ")
