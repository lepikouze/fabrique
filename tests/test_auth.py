"""Connexion OIDC générique : provider simulé (aucun réseau), rôles tirés des groupes, refus et mauvaise configuration."""
import base64, hashlib, time
from urllib.parse import parse_qs, urlsplit
import httpx, pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select
from starlette.testclient import TestClient
from app import auth, config, db, main

ISS = "https://idp.test/realms/interne"
KEY = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
HTML = {"accept": "text/html,application/xhtml+xml"}


class FakeIdP:
    def __init__(self):
        self.codes, self.claims = {}, {}

    def handler(self, req):
        u = str(req.url)
        if u == ISS + "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "authorization_endpoint": ISS + "/auth", "token_endpoint": ISS + "/token",
                                             "jwks_uri": ISS + "/jwks", "end_session_endpoint": ISS + "/logout",
                                             "id_token_signing_alg_values_supported": ["RS256"]})
        if u == ISS + "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if u == ISS + "/token":
            form = parse_qs(req.content.decode())
            challenge, nonce = self.codes.pop(form["code"][0])
            verifier = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            if verifier != challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            now = int(time.time())
            claims = {"iss": ISS, "aud": "fabrique", "sub": "u1", "iat": now, "exp": now + 300, "nonce": nonce, **self.claims}
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "expires_in": 300,
                                             "id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY)})
        return httpx.Response(404)


@pytest.fixture
def idp(monkeypatch):
    p = FakeIdP()
    for k, v in dict(OIDC_ISSUER=ISS, OIDC_CLIENT_ID="fabrique", OIDC_CLIENT_SECRET="s3cret", SESSION_SECRET="x" * 40,
                     PUBLIC_URL="https://fabrique.test", OIDC_ADMIN_GROUPS=["fabrique-admins"], OIDC_VERIFIER_GROUPS=["fabrique-verif"],
                     OIDC_REQUESTER_GROUPS=[], FABRIQUE_ADMINS=[]).items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(auth, "TRANSPORT", httpx.MockTransport(p.handler))
    auth.invalidate()
    yield p
    auth.invalidate()


def web():
    return TestClient(main.app, base_url="https://testserver", follow_redirects=False)


def login(cl, idp, user="alice", groups=()):
    q = parse_qs(urlsplit(cl.get("/auth/login", params={"next": "/inbox", "sso": 1}).headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"]
    idp.codes["c1"] = (q["code_challenge"][0], q["nonce"][0])
    idp.claims = {"preferred_username": user, "name": user.title(), "email": f"{user}@x.test", "groups": list(groups)}
    return cl.get("/auth/callback", params={"code": "c1", "state": q["state"][0]})


def test_protected_until_login(idp):
    cl = web()
    r = cl.get("/jobs", headers=HTML)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=%2Fjobs")
    assert cl.get("/events/stream").status_code == 401
    assert cl.get("/health").status_code == 200


@pytest.mark.parametrize("groups,role", [(["fabrique-admins"], "admin"), (["fabrique-verif", "autre"], "verifier"),
                                         (["autre"], "requester"), (["fabrique-verif", "fabrique-admins"], "admin")])
def test_roles_from_groups(idp, groups, role):
    cl = web()
    r = login(cl, idp, groups=groups)
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    page = cl.get("/jobs", headers=HTML)
    assert page.status_code == 200 and auth.ROLE_LABEL[role] in page.text
    with db.tx() as c:
        u = db.one(c, select(db.users).where(db.users.c.username == "alice"))
    assert u["role"] == role and u["groups"] == groups
    assert (cl.get("/settings").status_code == 200) == (role == "admin")


def test_requester_groups_restrict_access(idp, monkeypatch):
    monkeypatch.setattr(config, "OIDC_REQUESTER_GROUPS", ["fabrique-demandeurs"])
    auth.invalidate()
    cl = web()
    r = login(cl, idp, user="intrus", groups=["autre"])
    assert r.status_code == 403 and "aucun rôle" in r.text
    assert cl.get("/jobs", headers=HTML).status_code == 303


def test_break_glass_admin(idp, monkeypatch):
    monkeypatch.setattr(config, "FABRIQUE_ADMINS", ["alice"])
    cl = web()
    login(cl, idp, groups=[])
    assert cl.get("/settings").status_code == 200
    with db.tx() as c:
        assert db.one(c, select(db.users.c.role_source))["role_source"].startswith("secours")


def test_role_switch_refused_when_oidc_active(idp):
    cl = web()
    login(cl, idp, groups=["autre"])
    cl.cookies.set("fabrique_as", "admin")
    assert cl.get("/settings").status_code == 403           # le cookie de démonstration est ignoré
    r = cl.post("/dev/as", data={"role": "admin"})
    assert "fabrique_as" not in r.cookies


def test_env_locks_fields_in_settings(idp):
    cl = web()
    login(cl, idp, groups=["fabrique-admins"])
    t = cl.get("/settings?tab=oidc").text
    assert "Connexion active" in t and "environnement" in t and "s3cret" not in t
    cl.post("/settings/oidc", data={"issuer": "https://ailleurs.test", "client_id": "x", "scopes": "openid"})
    auth.invalidate()
    assert auth.conf()["issuer"] == ISS and auth.conf()["client_id"] == "fabrique"


def test_misconfigured_is_closed(idp, monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "court")
    r = web().get("/jobs", headers=HTML)
    assert r.status_code == 503 and "mal configurée" in r.text


def test_logout_goes_to_provider(idp):
    cl = web()
    login(cl, idp, groups=["fabrique-admins"])
    r = cl.post("/auth/logout")
    from urllib.parse import parse_qs, urlsplit
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == ISS + "/logout"                        # déconnexion chez le provider…
    assert q["post_logout_redirect_uri"] == [config.PUBLIC_URL + "/auth/login?out=1"] and q["client_id"] == [auth.conf()["client_id"]]
    assert cl.get("/jobs", headers=HTML).status_code == 303
    page = cl.get("/auth/login?out=1")                                                       # …puis retour sur la page de la Fabrique
    assert page.status_code == 200 and "Tu es déconnecté" in page.text and "Se connecter en SSO" in page.text


def test_safe_next():
    assert auth.safe_next("//evil.com") == "/" and auth.safe_next("https://evil.com") == "/" and auth.safe_next("/jobs?x=1") == "/jobs?x=1"


def _uid(name):
    with db.tx() as c:
        return db.one(c, select(db.users.c.id).where(db.users.c.username == name))["id"]


def test_roles_managed_in_panel_without_groups(idp, monkeypatch):
    for k in ("OIDC_ADMIN_GROUPS", "OIDC_VERIFIER_GROUPS"):
        monkeypatch.setattr(config, k, [])
    monkeypatch.setattr(config, "FABRIQUE_ADMINS", ["alice"])
    auth.invalidate()
    admin, bob = web(), web()
    login(admin, idp, user="alice")
    admin.post("/settings/access", data={"default_role": "none"})
    # Nouveau compte : en attente, seule « Mon accès » est ouverte
    login(bob, idp, user="bob")
    assert bob.get("/jobs", headers=HTML).headers["location"] == "/access"
    assert "attend qu'un admin" in bob.get("/access").text
    assert bob.post("/new", data={"title": "x", "need": "y"}).status_code == 403
    # Il demande un accès vérificateur ; l'admin le voit dans sa boîte de réception et l'accorde
    bob.post("/access", data={"role": "verifier", "note": "je relis les PR"})
    inbox = admin.get("/inbox").text
    assert "Demandes d'accès" in inbox and "je relis les PR" in inbox
    admin.post(f"/users/{_uid('bob')}/role", data={"role": "verifier"})
    auth.forget_role()
    assert bob.get("/jobs", headers=HTML).status_code == 200          # sans se reconnecter
    assert bob.get("/settings").status_code == 403
    # Le rôle choisi dans le panneau tient aux connexions suivantes, même avec des groupes différents
    bob2 = web(); login(bob2, idp, user="bob", groups=["n-importe"])
    with db.tx() as c:
        u = db.one(c, select(db.users).where(db.users.c.username == "bob"))
    assert (u["role"], u["role_source"], u["requested_role"]) == ("verifier", "panneau", None)


def test_admin_cannot_change_own_role_and_requester_default(idp, monkeypatch):
    monkeypatch.setattr(config, "OIDC_ADMIN_GROUPS", [])
    monkeypatch.setattr(config, "FABRIQUE_ADMINS", ["alice"])
    auth.invalidate()
    cl = web(); login(cl, idp, user="alice")
    r = cl.post(f"/users/{_uid('alice')}/role", data={"role": "requester"})
    assert "propre rôle" in cl.get(r.headers["location"]).text
    other = web(); login(other, idp, user="carla")
    assert other.get("/jobs", headers=HTML).status_code == 200      # défaut : demandeur directement
    cl.post(f"/users/{_uid('carla')}/role", data={"refuse": "1"})
    cl.post(f"/users/{_uid('carla')}/role", data={"role": "none"})
    auth.forget_role()
    assert other.get("/jobs", headers=HTML).headers["location"] == "/access"
    assert web().post(f"/users/{_uid('carla')}/role", data={"role": "admin"}).status_code in (401, 303)


# ---------- premier démarrage et comptes locaux ----------

@pytest.fixture
def closed(monkeypatch):
    """Installation neuve réelle : ni mode ouvert, ni OIDC."""
    monkeypatch.setattr(config, "OPEN", False)
    monkeypatch.setattr(config, "SETUP_CODE", "AAAA-BBBB-CCCC-DDDD")
    monkeypatch.setattr(config, "SESSION_SECRET", "s" * 40)
    auth._setup.update(code="", bad=0); auth._local["at"] = 0.0; auth._fails.clear()
    yield
    auth._setup.update(code="", bad=0); auth._local["at"] = 0.0; auth._fails.clear()


GOOD = "Un-Mot-De-Passe-2026"


def test_fresh_install_serves_only_setup_and_needs_the_code(closed):
    cl = web()
    assert auth.mode() == "setup"
    r = cl.get("/jobs", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"] == "/auth/setup"
    assert cl.get("/api/x").status_code == 503 and cl.get("/health").status_code == 200
    assert "Premier démarrage" in cl.get("/auth/setup").text
    bad = cl.post("/auth/setup", data={"code": "0000-0000-0000-0000", "login": "admin", "password": GOOD, "password2": GOOD})
    assert bad.status_code == 403 and auth.mode() == "setup"
    weak = cl.post("/auth/setup", data={"code": "AAAA-BBBB-CCCC-DDDD", "login": "admin", "password": "court", "password2": "court"})
    assert weak.status_code == 400 and "trop faible" in weak.text
    ok = cl.post("/auth/setup", data={"code": "aaaa-bbbb-cccc-dddd", "login": "admin", "name": "Admin", "password": GOOD, "password2": GOOD})
    assert ok.status_code == 303 and auth.mode() == "on"
    assert cl.get("/settings", headers={"accept": "text/html"}).status_code == 200            # connecté, admin
    with db.tx() as c:
        u = db.one(c, select(db.users).where(db.users.c.username == "admin"))
    assert u["role"] == "admin" and u["password_hash"].startswith("scrypt$") and GOOD not in u["password_hash"]
    assert web().post("/auth/setup", data={"code": "AAAA-BBBB-CCCC-DDDD", "login": "autre", "password": GOOD, "password2": GOOD}).status_code == 303
    with db.tx() as c:
        assert not db.one(c, select(db.users).where(db.users.c.username == "autre"))          # l'installation ne se rejoue pas


def test_password_login_logout_and_lockout(closed):
    auth.create_local("admin", "Admin", GOOD, "admin", "")
    cl = web()
    r = cl.get("/jobs", headers={"accept": "text/html"})
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    assert "Mot de passe" in cl.get("/auth/login").text
    assert cl.post("/auth/password", data={"login": "admin", "password": "faux"}).status_code == 401
    assert cl.post("/auth/password", data={"login": "inconnu", "password": GOOD}).status_code == 401
    assert cl.post("/auth/password", data={"login": "admin", "password": GOOD, "next": "/jobs"}).headers["location"] == "/jobs"
    assert cl.get("/settings", headers={"accept": "text/html"}).status_code == 200
    assert cl.post("/auth/logout").headers["location"] == "/auth/login?out=1"
    assert cl.get("/settings", headers={"accept": "text/html"}).status_code == 303
    for _ in range(5):
        cl.post("/auth/password", data={"login": "admin", "password": "faux"})
    assert cl.post("/auth/password", data={"login": "admin", "password": GOOD}).status_code == 429   # verrouillé un quart d'heure


def test_local_admin_is_the_break_glass_when_oidc_is_broken(closed, monkeypatch):
    auth.create_local("admin", "Admin", GOOD, "admin", "")
    monkeypatch.setattr(config, "OIDC_ISSUER", "https://sso.test/")      # OIDC incomplète (pas d'identifiant client)
    auth.invalidate()
    assert auth.problems() and auth.mode() == "on" and not auth.oidc_on()
    cl = web()
    assert cl.post("/auth/password", data={"login": "admin", "password": GOOD}).status_code == 303
    assert cl.get("/settings", headers={"accept": "text/html"}).status_code == 200


def test_oidc_login_cannot_take_over_a_local_account(idp, monkeypatch):
    auth.create_local("alice", "Alice", GOOD, "admin", "")
    cl = web()
    r = login(cl, idp, user="alice")
    assert r.status_code == 403


def test_admin_creates_local_accounts_and_user_changes_password(closed):
    auth.create_local("admin", "Admin", GOOD, "admin", "")
    cl = web(); cl.post("/auth/password", data={"login": "admin", "password": GOOD})
    r = cl.post("/users/local", data={"login": "vera", "name": "Vera", "role": "verifier", "password": "Encore-Un-Bon-9"})
    assert "créé" in cl.get(r.headers["location"]).text
    v = web(); assert v.post("/auth/password", data={"login": "vera", "password": "Encore-Un-Bon-9"}).status_code == 303
    assert v.post("/users/local", data={"login": "x1x", "role": "admin", "password": GOOD}).headers["location"].startswith("/jobs")   # pas admin
    assert v.post("/account/password", data={"old": "faux", "new": "Tout-Nouveau-Mot-7"}).status_code == 303
    v.post("/account/password", data={"old": "Encore-Un-Bon-9", "new": "Tout-Nouveau-Mot-7"})
    assert web().post("/auth/password", data={"login": "vera", "password": "Tout-Nouveau-Mot-7"}).status_code == 303


def _copy(cl):
    other = web()
    other.cookies.set(auth.COOKIE, cl.cookies.get(auth.COOKIE))
    return other


def _in(cl):
    return cl.get("/jobs", headers={"accept": "text/html"}).status_code == 200


def test_sessions_are_revoked_server_side(closed):
    auth.create_local("admin", "Admin", GOOD, "admin", "")
    auth.create_local("vera", "Vera", "Encore-Un-Bon-9", "verifier", "")
    with db.tx() as c:
        vid = db.one(c, select(db.users.c.id).where(db.users.c.username == "vera"))["id"]
    adm = web(); adm.post("/auth/password", data={"login": "admin", "password": GOOD})
    v = web(); v.post("/auth/password", data={"login": "vera", "password": "Encore-Un-Bon-9"})
    # 1. se déconnecter tue aussi un cookie copié
    stolen = _copy(v)
    assert _in(v) and _in(stolen)
    v.post("/auth/logout")
    assert not _in(stolen)
    # 2. un admin ferme les sessions d'un compte ; seul un admin le peut
    v.post("/auth/password", data={"login": "vera", "password": "Encore-Un-Bon-9"})
    assert _in(v) and v.post(f"/users/{vid}/logout").headers["location"].startswith("/jobs") and _in(v)
    r = adm.post(f"/users/{vid}/logout")
    assert "fermées" in adm.get(r.headers["location"]).text and not _in(v) and _in(adm)
    # 3. changer son mot de passe ferme les autres sessions, pas celle qui vient de le changer
    v.post("/auth/password", data={"login": "vera", "password": "Encore-Un-Bon-9"})
    other = _copy(v)
    v.post("/account/password", data={"old": "Encore-Un-Bon-9", "new": "Tout-Nouveau-Mot-7"})
    assert _in(v) and not _in(other)
    assert "Sessions fermées" in adm.get("/activity").text


def test_jobs_view_preference_follows_the_account(closed):
    auth.create_local("admin", "Admin", GOOD, "admin", "")
    auth.create_local("vera", "Vera", "Encore-Un-Bon-9", "verifier", "")
    a = web(); a.post("/auth/password", data={"login": "admin", "password": GOOD})
    page = lambda cl: cl.get("/jobs?view=board", headers={"accept": "text/html"}).text
    assert 'value="stages" role="menuitemradio" aria-checked="true"' in page(a)                # défaut : par étape
    a.post("/prefs/jobs", data={"board": "compact", "list": "flat"})
    assert 'value="compact" role="menuitemradio" aria-checked="true"' in page(a)
    other = web(); other.post("/auth/password", data={"login": "admin", "password": GOOD})   # autre navigateur, même compte
    assert 'value="compact" role="menuitemradio" aria-checked="true"' in page(other)
    assert 'value="flat" role="menuitemradio" aria-checked="true"' in page(other)
    v = web(); v.post("/auth/password", data={"login": "vera", "password": "Encore-Un-Bon-9"})
    assert 'value="stages" role="menuitemradio" aria-checked="true"' in page(v)                # un autre compte garde le défaut
    with db.tx() as c:
        assert db.one(c, select(db.users.c.prefs).where(db.users.c.username == "admin"))["prefs"] == {"jobs": {"board": "compact", "list": "flat"}}


def test_secrets_are_generated_when_env_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    a = config._secret("SESSION_SECRET")
    assert len(a) >= 48 and config._secret("SESSION_SECRET") == a and "SESSION_SECRET" in config.GENERATED
    assert oct((tmp_path / "secrets.json").stat().st_mode & 0o777) == "0o600"
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    assert config._secret("SESSION_SECRET") == "x" * 40                                       # l'environnement l'emporte
