"""Première installation : modèle par défaut, providers Git (tester, modifier), ajout et retrait de repo."""
import httpx
from sqlalchemy import select
from app import db, jobs, main, providers
from .conftest import ADMIN, REQ, web


def _bare():
    """Une installation neuve : aucun provider de modèles, aucun modèle, aucun modèle par défaut."""
    with db.tx() as c:
        db.set_setting(c, "models", db.DEFAULTS["models"], "test")
        db.set_setting(c, "agents", db.DEFAULTS["agents"], "test")


def test_a_fresh_install_has_no_model_and_says_what_to_do():
    _bare()
    with db.tx() as c:
        assert providers.listing(c) == [] and "Aucun modèle par défaut" in providers.default_problem(c)
    step = next(s for s in main.start_steps() if s[0] == "Un modèle par défaut")
    assert step[1] is False
    page = web("admin").get("/settings?tab=agents").text
    assert "Aucun job ne peut démarrer" in page and "Anthropic" not in page.split("Providers de modèles")[1].split("<table")[1].split("</table>")[0]


def test_the_first_enabled_model_becomes_the_default():
    _bare()
    with db.tx() as c:
        providers.save(c, "alice", "local", "compatible", "local", "http://modele.test:8080/v1", "")
        providers.enable(c, "alice", "local", [{"id": "gemma-test", "name": "Gemma", "apis": ["anthropic", "openai"]}])
        assert db.get_setting(c, "agents")[0] == {"harness": "claude-code", "model": "gemma-test"}
        assert providers.default_problem(c) == ""
        providers.enable(c, "alice", "local", [{"id": "gemma-test", "name": "Gemma", "apis": ["anthropic"]},
                                                 {"id": "autre", "name": "Autre", "apis": ["anthropic"]}])
        assert db.get_setting(c, "agents")[0]["model"] == "gemma-test"          # un défaut déjà choisi ne bouge pas
    assert next(s for s in main.start_steps() if s[0] == "Un modèle par défaut")[1] is True


def test_a_default_model_without_key_is_explained():
    with db.tx() as c:                                                           # banc : modèle Anthropic par défaut, sans clé
        assert "n'a pas de clé" in providers.default_problem(c)


def test_no_job_starts_without_a_model(repo):
    with db.tx() as c:
        db.set_setting(c, "agents", {"harness": "claude-code", "model": ""}, "test")
    try:
        jobs.create(REQ, "T", "B", "", repo)
        assert False, "aurait dû refuser"
    except jobs.Refused as e:
        assert "Aucun modèle par défaut" in str(e)


def test_relaunching_an_onboarding_takes_the_current_model():
    cl = web("admin")
    assert cl.post("/repos", data={"provider": "forgejo", "slug": "equipe/neuf", "name": "Neuf"}).status_code == 303
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.slug == "equipe/neuf"))
        j = db.one(c, select(db.jobs).where(db.jobs.c.repo_id == r["id"]))
        assert j["model"] == "claude-opus-5-5"
        c.execute(db.intents.delete().where(db.intents.c.job_id == j["id"]))     # l'essai a échoué : plus rien en file
        c.execute(db.jobs.update().where(db.jobs.c.id == j["id"]).values(stage="failed"))
        c.execute(db.repos.update().where(db.repos.c.id == r["id"]).values(onboarding="failed"))
        db.set_setting(c, "agents", {"harness": "claude-code", "model": "gemma-4-26b-a4b"}, "test")
    assert jobs.onboard(ADMIN, r["id"]) == j["ref"]
    with db.tx() as c:
        assert db.one(c, select(db.jobs).where(db.jobs.c.id == j["id"]))["model"] == "gemma-4-26b-a4b"


def _forge(handler):
    return httpx.MockTransport(handler)


def test_git_check_names_the_account_and_the_organisation():
    seen = []

    def h(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        if req.url.path == "/api/v1/user":
            return httpx.Response(200, json={"login": "fabrique-bot"}) if req.headers["authorization"] == "token bon" else httpx.Response(401)
        return httpx.Response(200 if req.url.path == "/api/v1/orgs/fabrique" else 404, json={})
    ok, msg = providers.git_check("forgejo", "https://git.test", "bon", "fabrique", transport=_forge(h))
    assert ok and "fabrique-bot" in msg and "« fabrique » est visible" in msg
    ok, msg = providers.git_check("forgejo", "https://git.test", "bon", "ailleurs", transport=_forge(h))
    assert not ok and "introuvable" in msg
    ok, msg = providers.git_check("forgejo", "https://git.test", "faux", transport=_forge(h))
    assert not ok and "refuse ce token" in msg
    assert all(u.startswith("https://git.test/") for u, _ in seen)


def test_git_check_speaks_each_forge_dialect():
    def h(req):
        if req.url.host == "gitlab.test":
            assert req.url.path == "/api/v4/user" and req.headers["private-token"] == "t"
            return httpx.Response(200, json={"username": "robot"})
        assert str(req.url) == "https://api.github.com/user" and req.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json={"login": "robot"})
    assert providers.git_check("gitlab", "https://gitlab.test", "t", transport=_forge(h))[0]
    assert providers.git_check("github", "https://github.com", "t", transport=_forge(h))[0]


def test_git_test_endpoint_writes_nothing_and_never_sends_a_stored_token_elsewhere(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "git_check", lambda typ, base, tok, org="": (calls.append((typ, base, tok, org)), (True, "ok"))[1])
    with db.tx() as c:
        db.set_setting(c, "git", [{"name": "F", "type": "forgejo", "base_url": "https://git.test", "token": providers.seal("secret-enregistré"), "creation": ""}], "test")
        before = db.get_setting(c, "git")[0]
    cl = web("admin")
    assert cl.post("/settings-test/git", data={"type": "forgejo", "base_url": "https://git.test", "token": "", "creation": "org"}).json()["ok"]
    assert calls[-1] == ("forgejo", "https://git.test", "secret-enregistré", "org")
    r = cl.post("/settings-test/git", data={"type": "forgejo", "base_url": "https://ailleurs.test", "token": ""}).json()
    assert not r["ok"] and "saisis à nouveau le token" in r["msg"] and len(calls) == 1
    assert not cl.post("/settings-test/git", data={"type": "forgejo", "base_url": "http://git.test", "token": "x"}).json()["ok"]
    assert web("requester").post("/settings-test/git", data={"type": "forgejo", "base_url": "https://git.test", "token": "x"}).status_code == 403
    with db.tx() as c:
        assert db.get_setting(c, "git")[0] == before


def test_git_provider_can_be_edited_and_repos_are_added_by_provider_name():
    cl = web("admin")
    with db.tx() as c:
        db.set_setting(c, "git", [{"name": "forgejo-home", "type": "forgejo", "base_url": "https://git.test", "token": providers.seal("t0k"), "creation": ""}], "test")
    page = cl.get("/settings?tab=git&editgit=forgejo-home").text
    assert 'value="forgejo-home"' in page and 'value="https://git.test"' in page and "t0k" not in page and "data-git-test" in page
    assert cl.post("/settings/git", data={"gid": "forgejo-home", "type": "forgejo", "name": "Forge maison", "base_url": "https://git.test", "token": "", "creation": "fabrique"}).status_code == 303
    with db.tx() as c:
        g = db.get_setting(c, "git")[0][0]
    assert g["name"] == "Forge maison" and g["creation"] == "fabrique" and providers.unseal(g["token"]) == "t0k"
    repos = cl.get("/repos").text
    assert "Forge maison (Forgejo)" in repos and "(GitLab)" not in repos


def test_a_repo_is_removed_only_while_it_has_no_requests(repo):
    cl = web("admin")
    cl.post("/repos", data={"provider": "forgejo", "slug": "equipe/neuf", "name": "Neuf"})
    with db.tx() as c:
        rid = db.one(c, select(db.repos).where(db.repos.c.slug == "equipe/neuf"))["id"]
        jid = db.one(c, select(db.jobs).where(db.jobs.c.repo_id == rid))["id"]
    assert "Retirer le repo" in cl.get(f"/repos?repo={rid}").text
    assert web("requester").post(f"/repos/{rid}/remove").status_code in (303, 403)
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))            # un requester ne retire rien
        c.execute(db.intents.update().where(db.intents.c.job_id == jid).values(state="leased"))
    cl.post(f"/repos/{rid}/remove")
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == rid))            # onboarding en cours sur un runner : refusé
        c.execute(db.intents.update().where(db.intents.c.job_id == jid).values(state="pending"))
    assert cl.post(f"/repos/{rid}/remove").headers["location"] == "/repos"
    with db.tx() as c:
        assert not db.one(c, select(db.repos).where(db.repos.c.id == rid))
        assert not db.one(c, select(db.jobs).where(db.jobs.c.id == jid)) and not db.one(c, select(db.intents).where(db.intents.c.job_id == jid))
    jobs.create(REQ, "T", "B", "", repo)                                           # un repo qui a reçu une demande garde son historique
    cl.post(f"/repos/{repo}/remove")
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == repo))


def test_setup_keeps_the_repo_step_once_a_repo_has_a_recipe(repo):
    step = lambda: next(s for s in main.start_steps() if s[0] == "Un premier repo")[1]
    assert step() is True
    with db.tx() as c:                       # repo vierge repassé en onboarding après sa première merge
        c.execute(db.repos.update().where(db.repos.c.id == repo).values(onboarding="analyzing", recipe={"setup": [], "checks": {}}))
    assert step() is True
    with db.tx() as c:
        c.execute(db.repos.update().where(db.repos.c.id == repo).values(onboarding="pending", recipe=None))
    assert step() is False


def test_assumptions_stay_a_list_of_sentences_from_the_agent_to_the_page():
    from types import SimpleNamespace
    from runner.relay import Relay
    from app import work
    sent = []
    relay = Relay(SimpleNamespace(event=lambda ref, **kw: sent.append(kw)), 0)
    ctx = SimpleNamespace(ref="FAB-1", stages=set(), decision=None)
    tok = relay.register(ctx)
    relay.handle(tok, {"cmd": "decision", "confident": True, "verdict": "ok", "interpretation": "i",
                       "assumptions": ["Le dépôt est vide.", "  ", "Python 3.12 suffit."], "notes": ""})
    assert sent[-1]["assumptions"] == ["Le dépôt est vide.", "Python 3.12 suffit."]
    relay.handle(tok, {"cmd": "decision", "confident": True, "verdict": "ok", "interpretation": "i", "assumptions": "Une seule phrase."})
    assert sent[-1]["assumptions"] == ["Une seule phrase."]
    # côté dashboard : une chaîne reçue d'un ancien runner reste une hypothèse entière, jamais des lettres
    assert work._assumptions("['Le dépôt est vide.']") == ["['Le dépôt est vide.']"]
    assert work._assumptions(["a b", "", "c d"]) == ["a b", "c d"] and work._assumptions(None) == []


def test_a_failed_onboarding_can_be_relaunched_even_when_the_repo_has_a_recipe(repo):
    cl = web("admin")
    with db.tx() as c:
        c.execute(db.repos.update().where(db.repos.c.id == repo).values(onboarding="failed", recipe={"setup": [], "checks": {}}))
    page = cl.get(f"/repos?repo={repo}").text
    assert f'action="/repos/{repo}/onboard"' in page and "Relancer l’onboarding" in page and "n'a pas abouti" in page.replace("&#39;", "'")
    with db.tx() as c:
        c.execute(db.repos.update().where(db.repos.c.id == repo).values(onboarding="ready"))
    assert f'action="/repos/{repo}/onboard"' not in cl.get(f"/repos?repo={repo}").text


def test_demo_data_can_be_seeded_on_a_fresh_install_without_any_model(monkeypatch):
    from app import demo
    _bare()
    with db.tx() as c:
        c.execute(db.repos.delete())
    monkeypatch.setattr("app.config.DEMO", True)
    demo.seed()
    with db.tx() as c:
        assert db.rows(c, select(db.jobs).where(db.jobs.c.source == "démo"))


def test_integrated_runner_can_be_registered_from_a_provided_token(monkeypatch):
    """Kubernetes : le token du runner intégré vient d'un Secret partagé, pas d'un fichier partagé."""
    from app import config, work
    tok = "fab_" + "k" * 43
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_TOKEN", tok)
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_FILE", "")
    work.bootstrap_runner()
    r = work.runner_for(tok)
    assert r and r["name"] == "runner-integre" and r["kind"] == "kubernetes"
    _, new = work.rotate_runner(r["id"], "alice")                # token renouvelé dans l'interface…
    work.bootstrap_runner()                                       # …un redémarrage ne remet pas l'ancien en service
    assert work.runner_for(tok) is None and work.runner_for(new)["name"] == "runner-integre"
    with db.tx() as c:
        assert len(db.rows(c, select(db.runners))) == 1
    monkeypatch.setattr(config, "BOOTSTRAP_RUNNER_TOKEN", "trop-court")
    work.bootstrap_runner()                                       # token invalide : ignoré, sans erreur


def test_outbound_proxy_reaches_the_mirror_git_but_never_a_job(monkeypatch):
    """Proxy d'entreprise : le git du miroir (runner) le suit ; l'environnement d'un job ne le reçoit jamais."""
    from runner import main as rm, agent as ragent, config as rconf
    for k in rm.PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    assert rm.outbound_proxy_env() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("NO_PROXY", ".interne.test")
    assert rm.outbound_proxy_env() == {"HTTPS_PROXY": "http://proxy.test:3128", "NO_PROXY": ".interne.test"}
    monkeypatch.setattr(rconf, "JOB_PROXY", "http://fabrique-proxy:3128")
    env = ragent.base_env("/work/FAB-1/home")
    assert env["HTTPS_PROXY"] == "http://fabrique-proxy:3128" and "proxy.test" not in str(env)


def test_egress_proxy_chains_to_an_upstream_proxy(tmp_path):
    import os, subprocess
    fake = tmp_path / "bin"; fake.mkdir()
    (fake / "tinyproxy").write_text('#!/bin/sh\ncat "$3"\n'); (fake / "tinyproxy").chmod(0o755)
    def run(**env):
        e = {"PATH": f"{fake}:{os.environ['PATH']}", "PROXY_ALLOW": r"^pypi\.org$", **env}
        return subprocess.run(["sh", "proxy/entrypoint.sh"], env=e, capture_output=True, text=True)
    assert "Upstream" not in run().stdout
    out = run(PROXY_UPSTREAM="http://proxy.test:3128/", PROXY_NO_UPSTREAM=".interne.test, 10.0.0.0/8,depot").stdout
    assert "Upstream http proxy.test:3128" in out and 'Upstream none ".interne.test"' in out and 'Upstream none "depot"' in out
    assert "10.0.0.0" not in out.split("Upstream http")[1]                    # une plage d'adresses n'est pas un domaine
    assert "FilterDefaultDeny Yes" in out                                      # la liste blanche reste en vigueur
    bad = run(PROXY_UPSTREAM="http://user:secret@proxy.test:3128")
    assert bad.returncode != 0 and "secret" not in bad.stdout


def test_sqlite_avoids_wal_on_a_network_filesystem(tmp_path, monkeypatch):
    """WAL repose sur une mémoire partagée que NFS ne fournit pas : la base s'y bloque. Mode rollback sur NFS."""
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay rw 0 0\n10.0.0.5:/export/pvc-1 /data nfs4 rw,relatime 0 0\ntmpfs /data/tmp tmpfs rw 0 0\n")
    assert db.fs_type("/data", str(mounts)) == "nfs4" and db.fs_type("/data/sous/dossier", str(mounts)) == "nfs4"
    assert db.fs_type("/data/tmp/x", str(mounts)) == "tmpfs" and db.fs_type("/opt", str(mounts)) == "overlay"
    monkeypatch.delenv("FABRIQUE_SQLITE_JOURNAL", raising=False)
    monkeypatch.setattr(db, "fs_type", lambda p, mounts="": "nfs4")
    assert db.journal_mode("/data/fabrique.db") == "delete"
    monkeypatch.setattr(db, "fs_type", lambda p, mounts="": "ext4")
    assert db.journal_mode("/data/fabrique.db") == "wal"
    monkeypatch.setenv("FABRIQUE_SQLITE_JOURNAL", "delete")
    assert db.journal_mode("/data/fabrique.db") == "delete"


def test_the_database_works_in_rollback_journal_mode(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setenv("FABRIQUE_SQLITE_JOURNAL", "delete")
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path}/r.db")
    db.reset_engine(); db.migrate()
    with db.tx() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
        db.set_setting(c, "limits", db.DEFAULTS["limits"], "test")
    with db.tx() as c:
        assert db.get_setting(c, "limits")[0]["size_limit"] == db.DEFAULTS["limits"]["size_limit"]


def test_a_file_written_on_another_node_is_waited_for(tmp_path):
    """NFS : le fichier écrit par le Pod du job peut apparaître au runner avec un délai ; on l'attend au lieu d'échouer."""
    from runner import main as rm
    target = tmp_path / "FAB-1" / ".tete.bundle"
    target.parent.mkdir()
    naps = []

    def nap(s):
        naps.append(s)
        if len(naps) == 3:
            target.write_bytes(b"x")          # visible au troisième essai
    assert rm.appeared(str(target), wait=30, sleep=nap) and len(naps) == 3
    assert rm.appeared(str(target), wait=30, sleep=nap) and len(naps) == 3         # déjà visible : aucune attente
    assert not rm.appeared(str(tmp_path / "jamais"), wait=0, sleep=nap)


def test_local_model_effort_is_set_on_compatible_providers_only():
    _bare()
    with db.tx() as c:
        providers.save(c, "alice", "local", "compatible", "local", "http://vllm.test:9090/v1", "", effort="medium")
        providers.enable(c, "alice", "local", [{"id": "qwen3", "name": "Qwen3", "apis": ["anthropic"]}])
        assert providers.runtime(c, "qwen3")["effort"] == "medium"
        providers.save(c, "alice", "local", "compatible", "local", "http://vllm.test:9090/v1", "")       # sans le champ : gardé
        assert providers.runtime(c, "qwen3")["effort"] == "medium"
        providers.save(c, "alice", "local", "compatible", "local", "http://vllm.test:9090/v1", "", effort="")
        assert providers.runtime(c, "qwen3")["effort"] == ""                                               # vide = défaut du serveur
        providers.save(c, "alice", "local", "compatible", "local", "http://vllm.test:9090/v1", "", effort="n'importe")
        assert providers.runtime(c, "qwen3")["effort"] == ""
        providers.save(c, "alice", "anthropic", "anthropic", "Anthropic", "https://api.anthropic.com", "sk-x", effort="low")
        assert all(p.get("effort", "") == "" for p in providers.listing(c) if p["type"] == "anthropic")
        providers.save(c, "alice", "autre", "compatible", "autre", "http://llama.test:8080/v1", "")     # nouveau, sans champ : moyen
        assert next(p for p in providers.listing(c) if p["id"] == "autre")["effort"] == "medium"


def test_gateway_sends_the_provider_effort_to_a_compatible_server():
    import json
    from runner import gateway
    body = json.dumps({"model": "q", "output_config": {"effort": "high"}, "thinking": {"type": "adaptive"}}).encode()
    assert json.loads(gateway.for_compatible(body, "medium")) == {"model": "q", "output_config": {"effort": "medium"}}
    assert json.loads(gateway.for_compatible(body, "")) == {"model": "q"}
    assert json.loads(gateway.for_compatible(json.dumps({"model": "q"}).encode(), "low")) == {"model": "q", "output_config": {"effort": "low"}}


def test_a_repo_path_is_checked_at_the_provider_when_added(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "git_repo_check", lambda typ, base, tok, slug: calls.append(slug) or (
        None if slug == "groupe/bon" else "« groupe/faux » est introuvable chez ce provider"))
    with db.tx() as c:
        db.set_setting(c, "git", [{"name": "GL", "type": "gitlab", "base_url": "https://gitlab.test", "token": providers.seal("t"), "creation": ""}], "test")
    cl = web("admin")
    r = cl.post("/repos", data={"provider": "gitlab", "slug": "groupe/faux", "name": ""})
    with db.tx() as c:
        assert not db.one(c, select(db.repos).where(db.repos.c.slug == "groupe/faux"))
    assert r.headers["location"] == "/repos"
    cl.post("/repos", data={"provider": "gitlab", "slug": "groupe/bon", "name": ""})
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.slug == "groupe/bon"))
    assert calls == ["groupe/faux", "groupe/bon"]


def test_git_repo_check_reasons():
    def h(req):
        return httpx.Response({"/api/v4/projects/g%2Fok": 200, "/api/v4/projects/g%2Fprive": 403}.get(req.url.raw_path.decode(), 404))
    t = httpx.MockTransport(h)
    assert providers.git_repo_check("gitlab", "https://gl.test", "x", "g/ok", transport=t) is None
    assert "refuse" in providers.git_repo_check("gitlab", "https://gl.test", "x", "g/prive", transport=t)
    assert "introuvable" in providers.git_repo_check("gitlab", "https://gl.test", "x", "-g/faux", transport=t)
    def down(req):
        raise httpx.ConnectError("panne")
    assert providers.git_repo_check("gitlab", "https://gl.test", "x", "g/ok", transport=httpx.MockTransport(down)) is None


def test_study_report_downloads_as_markdown(repo):
    with db.tx() as c:
        c.execute(db.jobs.update().values(report="## Réponse courte\nOui."))
    ref = jobs.create(REQ, "Étudier X", "B", "", repo)
    with db.tx() as c:
        c.execute(db.jobs.update().where(db.jobs.c.ref == ref).values(report="## Réponse courte\nOui.", stage="done"))
    r = web("admin").get(f"/jobs/{ref}/rapport.md")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"] and r.text.startswith("# Étudier X") and "Oui." in r.text
    assert web("admin").get("/jobs/FAB-999/rapport.md").status_code == 404
    panel = web("admin").get(f"/inbox/{ref}/panel")
    assert panel.status_code == 200 and f'href="/jobs/{ref}/rapport.md" download' in panel.text


def test_demo_tab_only_in_demo_mode(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DEMO", False)
    assert "tab=demo" not in web("admin").get("/settings").text
    monkeypatch.setattr(config, "DEMO", True)
    assert "tab=demo" in web("admin").get("/settings").text


def test_oidc_discovery_test_answers_json_without_saving(monkeypatch):
    from app import auth
    async def disco(issuer):
        return True, "Découverte réussie."
    monkeypatch.setattr(auth, "discover", disco)
    r = web("admin").post("/settings-test/oidc", data={"issuer": "https://sso.test/realms/x"}, headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["ok"] and "Enregistrer" in r.json()["msg"]
    with db.tx() as c:
        assert (db.get_setting(c, "oidc")[0] or {}).get("issuer", "") == ""


def test_public_url_without_scheme_gets_https():
    import importlib, os
    from app import config
    old = os.environ.get("PUBLIC_URL")
    try:
        os.environ["PUBLIC_URL"] = "fabrique.exemple.org"
        assert importlib.reload(config).PUBLIC_URL == "https://fabrique.exemple.org"
        os.environ["PUBLIC_URL"] = "http://interne:8080"
        assert importlib.reload(config).PUBLIC_URL == "http://interne:8080"
    finally:
        if old is None:
            os.environ.pop("PUBLIC_URL", None)
        else:
            os.environ["PUBLIC_URL"] = old
        importlib.reload(config)


def test_login_page_puts_sso_first_and_folds_the_local_form():
    from app import auth
    tpl = auth.templates.get_template("auth.html")
    html = tpl.render(kind="login", sso=True, detail="", next="/", keep={}, PW_RULE="")
    assert html.index("Se connecter en SSO") < html.index("Connexion locale") < html.index('name="password"')
    assert '<details class="auth-local"' in html and "auth-local\" open" not in html.replace(" >", ">")
    assert "<details class=\"auth-local\" open" in tpl.render(kind="login", sso=True, detail="Mot de passe incorrect.", next="/", keep={}, PW_RULE="")
    html = tpl.render(kind="login", sso=False, detail="", next="/", keep={}, PW_RULE="")
    assert "Se connecter en SSO" not in html and 'name="password"' in html and "auth-local" not in html
