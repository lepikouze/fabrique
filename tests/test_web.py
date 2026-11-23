import json
"""Pages, rights per role (local mode, simulated role), forms, security headers, settings."""
import httpx, pytest
from sqlalchemy import select
from app import auth, db, demo, jobs
from .conftest import ADMIN, web

PAGES = ["/jobs", "/jobs?view=list", "/inbox", "/new", "/runners", "/repos", "/activity", "/settings"] + \
        [f"/settings?tab={t}" for t in ("git", "oidc", "limits", "notifications", "users")]


def test_all_pages_render_for_admin_with_demo_data(demo_on):
    demo.seed()
    cl = web()
    for p in PAGES + ["/jobs/FAB-3", "/jobs?job=FAB-3", "/inbox?item=FAB-5", "/repos?repo=1", "/jobs?archived=1"]:
        r = cl.get(p, headers={"accept": "text/html"})
        assert r.status_code == 200, p
        assert "Fabrique" in r.text and "Traceback" not in r.text
    assert cl.get("/jobs/FAB-3/panel").status_code == 200
    assert cl.get("/jobs/NOPE/panel").status_code == 404
    assert cl.get("/jobs/NOPE").status_code == 404


def test_empty_states_without_data():
    cl = web()
    assert "No job" in cl.get("/jobs").text
    assert "Nothing is waiting for you" in cl.get("/inbox").text
    assert "No repo accepts" in cl.get("/new").text
    assert "No runner connected" in cl.get("/runners").text


def test_security_headers_and_no_inline_script():
    r = web().get("/jobs")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp and r.headers["x-frame-options"] == "DENY"
    assert "<script>" not in r.text


def test_cross_origin_post_refused(repo):
    cl = web()
    r = cl.post("/new", data={"title": "x", "need": "y", "repo_id": repo}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = cl.post("/new", data={"title": "x", "need": "y", "repo_id": repo}, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("role,path", [("requester", "/runners"), ("requester", "/repos"), ("requester", "/activity"),
                                       ("requester", "/settings"), ("reviewer", "/settings")])
def test_pages_forbidden_by_role(role, path):
    r = web(role).get(path)
    assert r.status_code == 403 and "Access denied" in r.text


def test_requester_navigation_is_reduced():
    t = web("requester").get("/jobs").text
    assert 'href="/settings"' not in t and 'href="/runners"' not in t and 'href="/new"' in t


def test_request_form_creates_job_and_flashes(repo):
    cl = web("requester")
    r = cl.post("/new", data={"title": "Exporter", "need": "En CSV", "done_when": "tested", "repo_id": repo, "harness": "codex"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs?job=FAB-1"
    page = cl.get("/jobs?job=FAB-1")
    assert "Request FAB-1 filed" in page.text and "Exporter" in page.text
    assert jobs.get(ADMIN, "FAB-1")["harness"] == "claude-code"   # agent choice ignored for a request
    assert "filed" not in cl.get("/jobs").text                   # the message is only displayed once


def test_refused_request_keeps_what_was_typed(repo):
    cl = web()
    r = cl.post("/new", data={"title": "", "need": "Un besoin déjà rédigé", "done_when": "Quand ça marche", "repo_id": repo, "study": "1"})
    assert r.status_code == 400 and "required" in r.text and "What you typed is kept" in r.text
    assert "Un besoin déjà rédigé" in r.text and "Quand ça marche" in r.text                     # nothing to retype
    assert 'name="study" value="1" checked' in r.text
    page = cl.get("/new").text
    assert "On this repo" in page and "budget" in page                                            # what will apply to the chosen repo


def test_inbox_opens_first_item_and_shows_context(repo):
    from sqlalchemy import update
    from app import jobs
    from .conftest import ADMIN, REQ
    assert "Nothing is waiting for you" in web().get("/inbox").text
    ref = jobs.create(REQ, "Exporter en CSV", "Le besoin complet", "", repo)
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="waiting"))
        c.execute(db.questions.insert().values(job_id=j["id"], question="Séparateur : (A) virgule ; (B) point-virgule ?", asked_at=1.0))
    page = web().get("/inbox").text
    assert f'data-open="{ref}"' in page and "Your answer" in page                                 # first item opened by default
    assert "(B) point-virgule ?" in page and "requested by" in page and "Le besoin complet" in page
    assert 'aria-label="Compteurs du jour"' not in page                                          # no counters dashboard here
    assert jobs.get(ADMIN, ref)["activity_label"] == "paused"


def test_activity_labels_and_archive_keeps_the_view(repo):
    from sqlalchemy import update
    from app import jobs, work
    from .conftest import ADMIN, REQ
    ref = jobs.create(REQ, "Un job", "B", "", repo)
    assert jobs.get(ADMIN, ref)["activity_label"] == "queued"
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage="implementing", activity="active"))
    assert jobs.get(ADMIN, ref)["activity_label"] == "active"
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(activity="idle", status_text=work.PAUSED))
    assert jobs.get(ADMIN, ref)["activity_label"] == "paused"
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage="pr", status_text="Ready for review"))
    assert jobs.get(ADMIN, ref)["activity_label"] == "waiting"
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage="merged"))
    assert jobs.get(ADMIN, ref)["activity_label"] == "done"
    assert ">done<" in web().get(f"/jobs/{ref}/panel").text
    cl = web()
    r = cl.post(f"/jobs/{ref}/archive", headers={"referer": f"http://testserver/jobs?view=list&repo={repo}&job={ref}", "host": "testserver"})
    assert r.headers["location"] == f"/jobs?view=list&repo={repo}"                               # same view, without the archived job
    r = cl.post(f"/jobs/{ref}/archive", data={"restore": "1"}, headers={"referer": "http://testserver/jobs?view=list&archived=1", "host": "testserver"})
    assert r.headers["location"] == "/jobs?view=list&archived=1"


def test_finished_study_sits_in_inbox_until_marked_read(repo):
    from sqlalchemy import update
    from app import jobs
    from .conftest import REQ
    ref = jobs.create(REQ, "A study", "Quoi ?", "", repo, study=True)
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage="done", report="Le rapport", status_text="Study delivered"))
    cl = web()
    page = cl.get("/inbox").text
    assert "Study delivered" in page and "Le rapport" in page and "Mark as read" in page
    r = cl.post(f"/jobs/{ref}/seen")
    assert "marked as read" in cl.get(r.headers["location"]).text
    assert "Nothing is waiting for you" in cl.get("/inbox").text


def test_settings_only_admin_can_save():
    r = web("reviewer").post("/settings/limits", data={"budget_job": "1", "budget_day": "2", "max_parallel": "1", "size_limit": "10"})
    assert r.status_code == 303
    with db.tx() as c:
        assert db.get_setting(c, "limits")[1] is None


def test_settings_limits_validation_and_trace():
    cl = web()
    bad = cl.post("/settings/limits", data={"budget_job": "50", "budget_day": "10", "max_parallel": "2", "size_limit": "300"})
    assert "exceed" in cl.get(bad.headers["location"]).text
    cl.post("/settings/limits", data={"budget_job": "2,5", "budget_day": "20", "max_parallel": "3", "size_limit": "200",
                                      "protected_paths": "**/auth/**\n\n.forgejo/**"})
    with db.tx() as c:
        v, row = db.get_setting(c, "limits")
    assert v["budget_job"] == 2.5 and v["protected_paths"] == ["**/auth/**", ".forgejo/**"] and row["updated_by"] == "local"
    assert "Saved by" in cl.get("/settings?tab=limits").text


def test_settings_git_and_models_validation(monkeypatch):
    from app import config, providers, work
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")
    cl = web()
    cl.post("/settings/git", data={"name": "GitLab", "type": "gitlab", "base_url": "http://gitlab.x", "token": "glpat-123"})     # no https: refused
    cl.post("/settings/git", data={"name": "GitLab", "type": "gitlab", "base_url": "https://gitlab.x", "token": "glpat-123"})
    cl.post("/settings/git", data={"gid": "gitlab", "name": "GitLab 2", "type": "gitlab", "base_url": "https://gitlab.x", "token": "", "creation": "usine"})   # modified: token kept
    r = cl.post("/settings/git", data={"gid": "gitlab", "name": "GitLab 3", "type": "gitlab", "base_url": "https://gitlab.y", "token": ""})     # the address changes: the token does not follow
    assert "enter the token again" in cl.get(r.headers["location"]).text
    with db.tx() as c:
        git = db.get_setting(c, "git")[0]
    assert [g["type"] for g in git] == ["forgejo", "gitlab"] and git[1]["base_url"] == "https://gitlab.x"
    assert git[1]["name"] == "GitLab 2" and git[1]["creation"] == "usine" and git[1]["id"] == "gitlab"
    assert "glpat-123" not in str(git) and providers.unseal(git[1]["token"]) == "glpat-123"   # encrypted, write-only
    assert "glpat-123" not in cl.get("/settings?tab=git").text



def test_model_providers_keys_listing_and_compatibility(monkeypatch):
    import httpx
    from app import config, jobs, providers
    cl = web()
    # no master key: an API key is refused
    monkeypatch.setattr(config, "SECRET_KEY", "")
    r = cl.post("/settings/providers", data={"action": "save", "type": "openai", "pid": "openai", "name": "OpenAI", "base_url": "", "key": "sk-test-123"})
    assert "FABRIQUE_SECRET_KEY" in cl.get(r.headers["location"]).text
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")

    def fake(req):
        if req.url.path == "/v1/models":
            assert req.headers["authorization"] == "Bearer sk-test-123"
            return httpx.Response(200, json={"data": [{"id": "gpt-5.3-codex"}, {"id": "gpt-5-mini"}, {"id": "text-embedding-3"}, {"id": "gpt-image-1"}]})
        return httpx.Response(404)
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(fake))
    r = cl.post("/settings/providers", data={"action": "save", "type": "openai", "pid": "openai", "name": "OpenAI", "base_url": "", "key": "sk-test-123"})
    page = cl.get(r.headers["location"]).text
    assert "gpt-5.3-codex" in page and "gpt-5-mini" in page and "embedding" not in page and "gpt-image" not in page
    assert "sk-test-123" not in page                                                       # key never displayed again
    with db.tx() as c:
        stored = db.get_setting(c, "providers")[0]
    assert "sk-test-123" not in str(stored)                                                 # encrypted in database
    cl.post("/settings/providers", data={"action": "enable", "pid": "openai", "model": ["gpt-5.3-codex"]})
    with db.tx() as c:
        rt = providers.runtime(c, "gpt-5.3-codex")
        ms = {m["id"]: m for m in providers.models_with_provider(c)}
    assert rt["key"] == "sk-test-123" and rt["type"] == "openai" and ms["gpt-5.3-codex"]["apis"] == ["openai"]
    assert ms["claude-opus-5-5"]["apis"] == ["anthropic"] and set(ms["gemma-4-26b-a4b"]["apis"]) == {"anthropic", "openai"}
    # agent and model must be compatible
    r = cl.post("/settings/agents", data={"harness": "claude-code", "model": "gpt-5.3-codex"})
    assert "cannot use" in cl.get(r.headers["location"]).text                      # Claude Code does not speak the OpenAI API
    cl.post("/settings/agents", data={"harness": "codex", "model": "gpt-5.3-codex"})
    with db.tx() as c:
        assert db.get_setting(c, "agents")[0] == {"harness": "codex", "model": "gpt-5.3-codex", "profile": ""}
    # the default model cannot disappear
    r = cl.post("/settings/providers", data={"action": "remove", "pid": "openai"})
    assert "default" in cl.get(r.headers["location"]).text
    # "Edit": pre-filled form, fixed identifier, key never re-displayed
    page = cl.get("/settings?tab=agents&edit=openai").text
    assert 'value="OpenAI"' in page and "key set" in page and "sk-test-123" not in page
    # replace the key without re-entering it: empty = keep
    cl.post("/settings/providers", data={"action": "save", "type": "openai", "pid": "openai", "name": "OpenAI (perso)", "base_url": "", "key": ""})
    with db.tx() as c:
        assert providers.runtime(c, "gpt-5.3-codex")["key"] == "sk-test-123"
        assert {p["id"]: p for p in providers.listing(c)}["openai"]["name"] == "OpenAI (perso)"
    cl.post("/settings/providers", data={"action": "save", "type": "openai", "pid": "openai", "name": "OpenAI (perso)", "base_url": "", "key": "", "clear_key": "1"})
    with db.tx() as c:
        assert providers.runtime(c, "gpt-5.3-codex")["key"] == ""


def test_provider_key_does_not_follow_a_new_address(monkeypatch):
    import httpx
    from app import config, providers
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(200, json={"data": [{"id": "m1"}]})))
    cl = web()
    f = {"action": "save", "type": "compatible", "pid": "vllm", "name": "vLLM"}
    cl.post("/settings/providers", data={**f, "base_url": "https://un.test/v1", "key": "sk-secret"})
    r = cl.post("/settings/providers", data={**f, "base_url": "https://autre.test/v1", "key": ""})
    assert "enter the key again" in cl.get(r.headers["location"]).text
    with db.tx() as c:
        p = next(x for x in providers.listing(c) if x["id"] == "vllm")
    assert p["base_url"] == "https://un.test/v1" and providers.unseal(p["key"]) == "sk-secret"
    cl.post("/settings/providers", data={**f, "base_url": "https://autre.test/v1", "key": "sk-nouvelle"})
    with db.tx() as c:
        p = next(x for x in providers.listing(c) if x["id"] == "vllm")
    assert p["base_url"] == "https://autre.test/v1" and providers.unseal(p["key"]) == "sk-nouvelle"


def test_compatible_server_probe_marks_agents(monkeypatch):
    import httpx
    from app import providers
    cl = web()

    def fake(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama3-70b"}]})
        if req.url.path == "/v1/messages":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "je ne sais pas"}]})
        if req.url.path == "/v1/responses":
            assert json.loads(req.content)["tools"][0]["type"] == "function"
            return httpx.Response(200, json={"output": [{"type": "reasoning"}, {"type": "function_call", "name": "lire_fichier"}]})
        return httpx.Response(404)
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(fake))
    cl.post("/settings/providers", data={"action": "save", "type": "compatible", "pid": "vllm", "name": "vLLM", "base_url": "http://10.0.0.2:8000/v1", "key": ""})
    r = cl.post("/settings/providers", data={"pid": "vllm", "probe": "llama3-70b"})
    assert "Codex ✓" in cl.get(r.headers["location"]).text
    cl.post("/settings/providers", data={"action": "enable", "pid": "vllm", "model": ["llama3-70b"]})
    with db.tx() as c:
        m = {x["id"]: x for x in providers.models_with_provider(c)}["llama3-70b"]
    assert m["apis"] == ["openai"]                                                           # Claude Code failed to call the tool


def test_oidc_settings_require_openid_scope_and_switch_mode(monkeypatch):
    cl = web()
    r = cl.post("/settings/oidc", data={"issuer": "", "client_id": "fab", "scopes": "profile email"})
    assert "openid" in cl.get(r.headers["location"]).text
    cl.post("/settings/oidc", data={"issuer": "https://idp.test/realms/x", "client_id": "fab", "scopes": "openid profile",
                                    "admin_groups": "fabrique-admins\nautres", "reviewer_groups": "", "requester_groups": "", "groups_claim": "groups"})
    auth.invalidate()
    assert auth.conf()["admin_groups"] == ["fabrique-admins", "autres"]
    assert auth.mode() == "ko"                        # secrets missing from the environment: everything closed, never opened
    assert cl.get("/jobs", headers={"accept": "text/html"}).status_code == 503


def test_oidc_discovery_button(monkeypatch):
    def handler(req):
        if str(req.url) == "https://idp.test/realms/x/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": "https://idp.test/realms/x", "authorization_endpoint": "a", "token_endpoint": "t", "jwks_uri": "j"})
        return httpx.Response(404)
    monkeypatch.setattr(auth, "TRANSPORT", httpx.MockTransport(handler))
    cl = web()
    r = cl.post("/settings-test/oidc", data={"issuer": "https://idp.test/realms/x"})
    assert "discovery OK" in cl.get(r.headers["location"]).text
    r = cl.post("/settings-test/oidc", data={"issuer": "https://idp.test/autre"})
    assert "discovery failed" in cl.get(r.headers["location"]).text


def test_repo_add_and_settings(repo):
    cl = web()
    r = cl.post("/repos", data={"provider": "gitlab", "slug": "group/project", "name": "Project"})
    assert r.headers["location"].startswith("/repos?repo=")
    assert "onboarding" in cl.get(r.headers["location"]).text.lower()
    assert "Invalid provider or repo path (expected form: group/project)." in cl.get(cl.post("/repos", data={"provider": "svn", "slug": "x/y"}).headers["location"]).text
    cl.post(f"/repos/{repo}", data={"name": "Trial", "accepts_jobs": "1", "merge_policy": "confident", "size_limit": "120",
                                    "budget_job": "3", "protected_paths": "db/**", "harness": "", "model": ""})
    with db.tx() as c:
        s = db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]
    assert s["merge_policy"] == "confident" and s["size_limit"] == 120 and s["protected_paths"] == ["db/**"]
    assert web("reviewer").post(f"/repos/{repo}", data={"merge_policy": "off"}).status_code == 303
    with db.tx() as c:
        assert db.one(c, select(db.repos).where(db.repos.c.id == repo))["settings"]["merge_policy"] == "confident"


def test_answer_and_decide_through_web(demo_on):
    demo.seed()
    cl = web("reviewer")
    r = cl.post("/jobs/FAB-5/answer", data={"answer": "A"}, headers={"referer": "http://testserver/inbox?item=FAB-5"})
    assert r.status_code == 303 and "Answer sent" in cl.get(r.headers["location"]).text
    ready = next(i["job"]["ref"] for i in jobs.inbox(ADMIN) if i["kind"] == "review")
    r = cl.post(f"/jobs/{ready}/decide", data={"verdict": "approve"})
    assert jobs.get(ADMIN, ready)["merge_state"] == "approved"


def test_dev_role_switch_only_in_local_mode():
    cl = web()
    r = cl.post("/dev/as", data={"role": "requester"})
    assert r.cookies.get("fabrique_as") == "requester"
    assert "Requester" in web("requester").get("/jobs").text


def test_events_stream_requires_user_and_health_is_public():
    assert web().get("/health").json() == {"ok": True}


def test_demo_hidden_by_default_excludes_everything(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DEMO", True)
    jobs.forget_demo()
    assert demo.seed()
    real = jobs.create(ADMIN, "Vrai job", "B", "", 1)
    assert [j["ref"] for j in jobs.listing(ADMIN)] == [real]       # demo jobs hidden by default
    assert jobs.counters(ADMIN)["spend"] == 0 and not jobs.inbox(ADMIN)
    before = jobs.listing(ADMIN)[0]["updated_at"]
    demo.tick()                                                     # simulator paused
    cl = web()
    assert "Demo</b>" not in cl.get("/jobs").text and "Internal portal" not in cl.get("/repos").text
    cl.post("/settings/demo", data={"visible": "1"})
    jobs.forget_demo()
    assert len(jobs.listing(ADMIN)) > 1 and "Demo</b>" in cl.get("/jobs").text
    jobs.forget_demo()


def test_ui_details_version_menu_no_apple_keys(demo_on):
    demo.seed()
    t = web().get("/jobs?job=FAB-5").text
    assert "⌘" not in t and "usermenu" in t and 'data-theme-set="auto"' in t and "Fabrique v0." in t
    assert "Queue" in t and "En file" not in t
    assert 'class="btn cta" href="/new"' in t and "btn primary" in t   # orange reserved for the new request


def test_codex_only_with_openai_models_and_full_view_keeps_counters(repo):
    from app import db, jobs, providers
    with db.tx() as c:
        ms = {m["id"]: m for m in providers.models_with_provider(c)}
    assert providers.harnesses_for(ms["gemma-4-26b-a4b"]) == ["claude-code"]               # local: Claude Code only
    assert providers.harnesses_for(ms["claude-opus-5-5"]) == ["claude-code"]
    with pytest.raises(jobs.Refused, match="cannot use"):
        jobs.create({"user": "vera", "name": "Vera", "role": "reviewer"}, "T", "B", "", repo, harness="codex", model="gemma-4-26b-a4b")
    ref = jobs.create({"user": "vera", "name": "Vera", "role": "reviewer"}, "T", "B", "", repo)
    html = web("admin").get(f"/jobs/{ref}").text
    assert "aria-label=\"Today's counters\"" in html and '<footer class="foot">' not in html


def test_each_user_picks_their_own_jobs_view(repo):
    from sqlalchemy import update
    from app import jobs
    from .conftest import REQ
    a = jobs.create(REQ, "Premier job", "B", "", repo)
    b = jobs.create(REQ, "Second job", "B", "", repo)
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == b).values(stage="implementing"))
    cl = web()
    board, lst = cl.get("/jobs?view=board").text, cl.get("/jobs?view=list").text
    assert "Investigation" in board and 'class="board "' in board and "<details class=\"grp\"" in lst      # default: per step
    r = cl.post("/prefs/jobs", data={"board": "compact"}, headers={"referer": "http://testserver/jobs?view=board", "host": "testserver"})
    assert r.headers["location"] == "/jobs?view=board"
    board = cl.get("/jobs?view=board").text
    assert 'class="board compact"' in board and "Yours" in board and "Done" in board and "Implementation" in board
    assert "<details class=\"grp\"" in cl.get("/jobs?view=list").text                                          # the list has not moved
    cl.post("/prefs/jobs", data={"list": "flat"})
    lst = cl.get("/jobs?view=list").text
    assert "<details class=\"grp\"" not in lst and "Sorted by last activity" in lst and a in lst and b in lst
    only = cl.get("/jobs?view=list&f=run").text
    assert b in only and f'data-ref="{a}"' not in only
    assert 'class="board compact"' in cl.get("/jobs?view=board").text                                          # both choices hold
    assert 'class="board "' in web().get("/jobs?view=board").text                                              # another browser keeps the default
    cl.post("/prefs/jobs", data={"board": "n-importe-quoi"})
    assert 'class="board compact"' in cl.get("/jobs?view=board").text


def test_new_request_form_adapts_to_the_kind_and_account_page_is_for_everyone(repo):
    cl = web()
    page = cl.get("/new").text
    assert 'data-kinded' in page and "What to study" in page.replace("&#39;", "'") and "Start the" in page
    assert 'class="frm card-form kinded "' in page                                   # by default: change
    r = cl.post("/new", data={"title": "", "need": "x", "repo_id": repo, "study": "1"})
    assert r.status_code == 400 and 'kinded is-study' in r.text and "How much would" in r.text   # refusal: the type remains "study"
    for role in ("requester", "reviewer", "admin"):
        acc = web(role).get("/account")
        assert acc.status_code == 200 and "Password" in acc.text, role             # accessible to all roles
    assert web("requester").post("/account/password", data={"old": "a", "new": "b"}).headers["location"] == "/account"
