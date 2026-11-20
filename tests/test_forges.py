"""Providers Git multiples (0.7.1) : plusieurs par type, désignés par leur nom ; chaque repo rattaché à son provider."""
import pytest
from sqlalchemy import select, update
from app import config, db, forges, jobs, providers, work
from .conftest import ADMIN, web


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")
    monkeypatch.setattr(providers, "git_repo_check", lambda *a, **k: None)          # pas de réseau : le chemin est réputé exister


def add(name, base, token, typ="gitlab"):
    with db.tx() as c:
        return forges.save(c, "alice", "", name, typ, base, token, "")


def test_same_address_twice_with_two_tokens_and_a_third_gitlab(key):
    a = add("GitLab interne (confidentiel)", "https://gl.interne", "tok-confidentiel")
    b = add("GitLab interne", "https://gl.interne", "tok-complet")
    c_ = add("GitLab partenaire", "https://gl.partenaire", "tok-partenaire")
    assert (a, b, c_) == ("gitlab-interne-confidentiel", "gitlab-interne", "gitlab-partenaire")
    with db.tx() as c:
        assert len([g for g in forges.listing(c) if g["type"] == "gitlab"]) == 3
    with pytest.raises(forges.Invalid, match="sert déjà"):
        add("GitLab encore", "https://gl.autre", "tok-complet")                         # un token, un seul provider
    with pytest.raises(forges.Invalid, match="s'appelle déjà"):
        add("gitlab INTERNE", "https://gl.autre", "tok-x")                               # un nom, un seul provider


def test_each_repo_gets_the_token_of_its_own_provider(key):
    add("GitLab interne (confidentiel)", "https://gl.interne", "tok-confidentiel")
    add("GitLab interne", "https://gl.interne", "tok-complet")
    cl = web()
    for gid in ("gitlab-interne-confidentiel", "gitlab-interne"):                     # même chemin chez deux providers
        r = cl.post("/repos", data={"provider": gid, "slug": "equipe/secret", "name": "Secret"})
        assert "Repo ajouté" in cl.get(r.headers["location"]).text
    assert "déjà connu" in cl.get(cl.post("/repos", data={"provider": "gitlab-interne", "slug": "equipe/secret"}).headers["location"]).text
    with db.tx() as c:
        repos = db.rows(c, select(db.repos).order_by(db.repos.c.id))
        assert [(r["git_id"], r["provider"]) for r in repos] == [("gitlab-interne-confidentiel", "gitlab"), ("gitlab-interne", "gitlab")]
        c.execute(update(db.repos).values(onboarding="ready"))
    tok = work.create_runner("runner-test", "conteneur", "alice")
    me = work.runner_for(tok)
    seen = {}
    for _ in range(2):
        item = work.claim(me)
        seen[item["job"]["ref"]] = item["forge"]["token"]
        with db.tx() as c:
            c.execute(update(db.intents).where(db.intents.c.state == "leased").values(state="done"))
    assert sorted(seen.values()) == ["tok-complet", "tok-confidentiel"]                 # chaque repo, son token


def test_names_shown_instead_of_types_and_removal_guarded(key, repo):
    gid = add("GitLab partenaire", "https://gl.partenaire", "tok-f")
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(provider="gitlab", git_id=gid))
    cl = web()
    assert "GitLab partenaire · alice/essai" in cl.get(f"/repos?repo={repo}").text
    r = cl.post("/settings/git", data={"remove": gid})
    assert "sert encore à 1 repo" in cl.get(r.headers["location"]).text
    page = cl.get("/repos").text
    assert f'<option value="{gid}">GitLab partenaire (GitLab)</option>' in page


def test_old_settings_without_id_still_work(repo):
    with db.tx() as c:                                                               # réglage d'avant la 0.7.1 : pas d'identifiant
        db.set_setting(c, "git", [{"name": "Forge", "type": "forgejo", "base_url": "https://git.test", "token": ""}], "t")
        assert forges.listing(c)[0]["id"] == "forge"
        r = db.one(c, select(db.repos).where(db.repos.c.id == repo))
        assert forges.for_repo(c, dict(r))["name"] == "Forge" and forges.label(c, dict(r)) == "Forge"   # rattaché par son type


def test_repo_creation_request_goes_to_the_chosen_provider(key):
    with db.tx() as c:
        forges.save(c, "alice", "", "GitLab interne", "gitlab", "https://gl.interne", "tok-a", "fabrique")
        forges.save(c, "alice", "", "GitLab partenaire", "gitlab", "https://gl.partenaire", "tok-b", "usine")
    rid = jobs.request_repo(ADMIN, "gitlab-partenaire", "api-notes", "")
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.id == rid))
    assert (r["slug"], r["git_id"], r["provider"]) == ("usine/api-notes", "gitlab-partenaire", "gitlab")


def test_gitea_is_a_provider_type_like_forgejo(key):
    import httpx
    gid = add("Gitea maison", "https://gitea.test", "tok-gitea", typ="gitea")
    seen = []
    def h(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        return httpx.Response(200, json={"login": "robot"})
    assert providers.git_check("gitea", "https://gitea.test", "t", transport=httpx.MockTransport(h))[0]
    assert seen[0] == ("https://gitea.test/api/v1/user", "token t")
    assert "Gitea" in web().get("/settings?tab=git").text and gid == "gitea-maison"
