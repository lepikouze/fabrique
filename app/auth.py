"""Connexion OIDC générique (Keycloak, Authentik, Entra ID…) : flux « authorization code » + PKCE S256 (Authlib), state et nonce,
id_token vérifié (émetteur, audience, expiration, signature JWKS), session signée (cookie fabrique_session).

Rôles : choisis dans le dashboard par un admin (Settings › Utilisateurs et rôles) ; à défaut, groupes OIDC si une
correspondance est réglée (utile au bureau avec Keycloak), sinon rôle par défaut des nouveaux comptes (requester, ou en
attente de validation). Le rôle est relu en base à chaque requête : un changement s'applique sans reconnexion.

Settings : l'environnement fournit les secrets (OIDC_CLIENT_SECRET, SESSION_SECRET) et peut verrouiller les autres champs ;
sinon ils se règlent dans le dashboard (Settings › Connexion). Le rôle est recalculé à chaque connexion depuis les groupes
du token : admin > reviewer > requester. Personne dans aucun groupe connu -> accès refusé (sauf groupes requester vides :
tout compte authentifié est requester). FABRIQUE_ADMINS : comptes toujours admin (secours).

Comptes locaux : au premier démarrage, la page /auth/setup crée le compte admin (identifiant et mot de passe fort), sur
présentation du code d'installation écrit dans les journaux. Ce compte reste le secours si l'OIDC tombe. D'autres comptes
locaux peuvent être créés par un admin. Mots de passe hachés (scrypt), cinq essais par quart d'heure.

Ni OIDC ni compte admin local -> installation à terminer (rien d'autre n'est servi). OIDC incomplète sans compte local ->
tout est fermé (503). Le mode ouvert (tout visiteur admin) n'existe qu'avec FABRIQUE_OPEN=1, pour le développement."""
import base64, hashlib, hmac, logging, re, secrets, time
from urllib.parse import quote, urlsplit
from fastapi import APIRouter, Request
from sqlalchemy import insert, select, update
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, RedirectResponse
from . import config, db

log = logging.getLogger("fabrique.auth")
COOKIE = "fabrique_session"
MIN_SECRET = 32
ROLES = ("requester", "verifier", "admin")
ROLE_LABEL = {"none": "En attente", "requester": "Requester", "verifier": "Reviewer", "admin": "Admin"}
ENV_FIELDS = {"issuer": "OIDC_ISSUER", "client_id": "OIDC_CLIENT_ID", "scopes": "OIDC_SCOPES", "groups_claim": "OIDC_GROUPS_CLAIM",
              "admin_groups": "OIDC_ADMIN_GROUPS", "verifier_groups": "OIDC_VERIFIER_GROUPS", "requester_groups": "OIDC_REQUESTER_GROUPS"}
TRANSPORT = None     # tests : httpx.MockTransport (fournisseur simulé)
templates = None
router = APIRouter()
_cache = {"at": 0.0, "conf": None}


def locked():
    """Champs fixés par l'environnement (affichés verrouillés dans les settings)."""
    return {k for k, env in ENV_FIELDS.items() if getattr(config, env)}


def conf(fresh=False):
    """Settings OIDC effectifs : environnement > base > défauts. Cache court (2 s) : lu à chaque requête."""
    if not fresh and _cache["conf"] is not None and time.time() - _cache["at"] < 2:
        return _cache["conf"]
    with db.tx() as c:
        stored, _ = db.get_setting(c, "oidc")
    cf = dict(db.DEFAULTS["oidc"], **(stored or {}))
    for k, env in ENV_FIELDS.items():
        v = getattr(config, env)
        if v:
            cf[k] = v
    cf["issuer"] = (cf.get("issuer") or "").strip()
    _cache.update(at=time.time(), conf=cf)
    return cf


def invalidate():
    _cache["conf"] = None


def client_secret():
    """Secret du client OIDC : variable d'environnement si elle existe, sinon valeur saisie dans les settings (chiffrée)."""
    if config.OIDC_CLIENT_SECRET:
        return config.OIDC_CLIENT_SECRET
    from . import providers
    with db.tx() as c:
        sealed, _ = db.get_setting(c, "oidc_secret")
    return providers.unseal((sealed or {}).get("v") or "") if sealed else ""


def problems(cf=None):
    cf = cf or conf()
    p = []
    if len(config.SESSION_SECRET) < MIN_SECRET:
        p.append(f"SESSION_SECRET absent ou trop court ({MIN_SECRET} caractères au moins)")
    if not cf.get("client_id"):
        p.append("identifiant client manquant")
    if not client_secret():
        p.append("secret client manquant (à saisir ci-dessous, ou variable OIDC_CLIENT_SECRET)")
    if urlsplit(cf["issuer"]).scheme not in ("http", "https"):
        p.append("émetteur invalide")
    if urlsplit(config.PUBLIC_URL).scheme not in ("http", "https"):
        p.append("PUBLIC_URL invalide")
    return p


# ---------- comptes locaux (compte admin de secours, créé au premier démarrage) ----------

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
_local = {"at": 0.0, "has": False}
_setup = {"code": "", "bad": 0}
_fails = {}   # identifiant -> (nombre d'échecs, heure du premier) : 5 essais par quart d'heure


def hash_password(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt$15$8$1${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def check_password(pw, stored):
    try:
        _, n, r, p, salt, h = (stored or "").split("$")
        got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=2 ** int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


PW_RULE = ("12 caractères au moins, avec au moins trois familles parmi : minuscules, majuscules, chiffres, autres caractères "
           "(par exemple - _ ! ?). Il ne doit pas contenir l'identifiant.")


def password_problem(pw, login=""):
    """Mot de passe fort : 12 caractères au moins, trois familles de caractères sur quatre, sans l'identifiant."""
    kinds = sum(bool(re.search(x, pw)) for x in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if len(pw) < 12:
        return "12 caractères au moins"
    if len(pw) > 200:
        return "200 caractères au plus"
    if kinds < 3:
        return "au moins trois familles parmi : minuscules, majuscules, chiffres, autres caractères"
    if login and login.lower() in pw.lower():
        return "il ne doit pas contenir l'identifiant"
    return ""


def has_local_admin(fresh=False):
    """Un compte admin avec mot de passe existe (cache de 2 s)."""
    if not fresh and time.time() - _local["at"] < 2:
        return _local["has"]
    with db.tx() as c:
        r = db.one(c, select(db.users.c.id).where(db.users.c.password_hash.is_not(None), db.users.c.role == "admin"))
    _local.update(at=time.time(), has=bool(r))
    return _local["has"]


def has_local_accounts():
    with db.tx() as c:
        return bool(db.one(c, select(db.users.c.id).where(db.users.c.password_hash.is_not(None))))


def create_local(login, name, password, role, by):
    login = (login or "").strip().lower()
    if not LOGIN_RE.match(login):
        raise ValueError("Identifiant : 3 à 40 caractères, minuscules, chiffres, point, tiret.")
    why = password_problem(password or "", login)
    if why:
        raise ValueError("Mot de passe trop faible : " + why + ".")
    if role not in ROLES:
        raise ValueError("Rôle inconnu.")
    t = time.time()
    with db.tx() as c:
        if db.one(c, select(db.users.c.id).where(db.users.c.username == login)):
            raise ValueError("Ce compte existe déjà.")
        c.execute(insert(db.users).values(username=login, name=(name or login).strip()[:120], email="", groups=[], role=role,
                                          role_source="compte local", created_at=t, password_hash=hash_password(password)))
        db.log_audit(c, by or login, "Compte local créé", target=login, detail=ROLE_LABEL[role])
    _local["at"] = 0.0
    forget_role(login)
    return login


def set_password(login, password, by):
    why = password_problem(password or "", login)
    if why:
        raise ValueError("Mot de passe trop faible : " + why + ".")
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == login, db.users.c.password_hash.is_not(None)).values(password_hash=hash_password(password)))
        db.log_audit(c, by, "Mot de passe changé", target=login)


def setup_code():
    """Code d'installation : prouve que la personne qui crée le premier compte admin a accès aux journaux du conteneur."""
    if not _setup["code"]:
        _setup["code"] = config.SETUP_CODE or "-".join(secrets.token_hex(2).upper() for _ in range(4))
        _setup["bad"] = 0
    return _setup["code"]


def announce_setup():
    log.warning("PREMIER DÉMARRAGE : ouvre %s/auth/setup et saisis le code d'installation : %s", config.PUBLIC_URL, setup_code())


def oidc_on():
    cf = conf()
    return bool(cf["issuer"]) and not problems(cf)


def mode():
    """'on' : session obligatoire (OIDC et/ou compte local) ; 'setup' : installation neuve, créer le compte admin ;
    'ko' : OIDC mal configurée et aucun compte local (tout refusé) ; 'off' : mode ouvert, développement seulement."""
    cf = conf()
    if cf["issuer"] and not problems(cf):
        return "on"
    # Comptes locaux et installation : jamais avec un secret de session absent ou trop court (une session se forgerait).
    weak = len(config.SESSION_SECRET) < MIN_SECRET
    if has_local_admin():
        return "ko" if weak else "on"
    if cf["issuer"]:
        return "ko"
    if config.OPEN:
        return "off"
    return "ko" if weak else "setup"


def log_startup():
    m = mode()
    if m == "off":
        log.warning("FABRIQUE_OPEN=1 : mode ouvert, tout visiteur est admin (développement seulement)")
    elif m == "setup":
        announce_setup()
    elif m == "ko":
        log.error("connexion mal configurée (%s) : toutes les pages répondent 503", " ; ".join(problems()))
    else:
        log.info("connexion active (%s)", conf()["issuer"] if oidc_on() else "comptes locaux")
    for name in sorted(config.GENERATED):
        log.warning("%s absent de l'environnement : généré et gardé avec les données (voir Settings)", name)


def provider_origin():
    if not oidc_on():
        return None
    u = urlsplit(conf()["issuer"])
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc and not any(c in u.netloc for c in " ;'\"") else None


def is_public(path):
    """Sans session : santé, fichiers statiques, et l'API des runners (authentifiée par le token du runner, pas par OIDC)."""
    return path in ("/health",) or path.startswith(("/static/", "/api/runner/"))


def valid(session):
    """Session présente, non expirée, et ouverte après la dernière révocation du compte (déconnexion, changement de mot de
    passe, « Déconnecter » par un admin) : un cookie copié ne survit pas à ces événements."""
    try:
        if not (session and session.get("user")) or float(session.get("exp", 0)) <= time.time():
            return False
        after = _state(session["user"])[1]
        return not after or float(session.get("iat", 0)) >= after
    except (TypeError, ValueError):
        return False


LOCAL = {"user": "local", "name": "Utilisateur local", "role": "admin", "local": True}


_roles = {}


def _state(username):
    """(rôle, instant de révocation des sessions) du compte, en base (cache d'une seconde) : un changement fait par un
    admin s'applique à la requête suivante."""
    hit = _roles.get(username)
    if hit and time.time() - hit[0] < 1:
        return hit[1]
    with db.tx() as c:
        r = db.one(c, select(db.users.c.role, db.users.c.sessions_after).where(db.users.c.username == username))
    st = (r["role"], r["sessions_after"]) if r else (None, None)
    _roles[username] = (time.time(), st)
    return st


def stored_role(username):
    return _state(username)[0]


def revoke_sessions(username, by, why, audit=True):
    """Toutes les sessions ouvertes du compte cessent de valoir, sur tous les navigateurs."""
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == username).values(sessions_after=time.time()))
        if audit:
            db.log_audit(c, by, "Sessions fermées", target=username, detail=why)
    forget_role(username)


def forget_role(username=None):
    _roles.clear() if username is None else _roles.pop(username, None)


def who(request):
    """{'user', 'name', 'role', 'local'} de la personne connectée. En mode local : l'utilisateur local, dont le rôle peut être
    simulé (cookie fabrique_as) pour voir l'interface d'un requester ou d'un reviewer."""
    m = mode()
    if m == "off":
        as_role = request.cookies.get("fabrique_as")
        return dict(LOCAL, role=as_role) if as_role in ROLES else dict(LOCAL)
    s = request.scope.get("session")
    if m == "on" and valid(s):
        role = stored_role(s["user"]) or "none"
        if s["user"].lower() in config.FABRIQUE_ADMINS:
            role = "admin"
        return {"user": s["user"], "name": s.get("name") or s["user"], "role": role, "local": False, "via": s.get("via") or "oidc"}
    return None


def can(user, action):
    """Droits par rôle. Le requester ne voit que ses propres demandes (filtré dans les requêtes)."""
    r = (user or {}).get("role")
    need = {"request": "requester", "answer_own": "requester",
            "see_all": "verifier", "answer": "verifier", "decide": "verifier", "archive": "verifier", "see_infra": "verifier",
            "settings": "admin", "repos_edit": "admin", "runners_edit": "admin"}[action]
    return r in ROLES and ROLES.index(r) >= ROLES.index(need)


def role_for(username, email, groups, existing=None, cf=None):
    """(rôle, source) à la connexion. Ordre : secours > choix d'un admin dans le dashboard > groupes OIDC > rôle par défaut.
    (None, raison) : refus (groupes requester réglés et la personne n'y est pas)."""
    cf = cf or conf()
    if (username or "").lower() in config.FABRIQUE_ADMINS or (email or "").lower() in config.FABRIQUE_ADMINS:
        return "admin", "secours (FABRIQUE_ADMINS)"
    if existing and existing.get("role_source") == "panneau":
        return existing["role"], "panneau"
    g = {x.lower() for x in groups}
    for role, key in (("admin", "admin_groups"), ("verifier", "verifier_groups"), ("requester", "requester_groups")):
        hit = sorted(g & {x.lower() for x in cf.get(key) or []})
        if hit:
            return role, f"groupe {hit[0]}"
    if cf.get("requester_groups"):
        return None, "aucun groupe autorisé"
    # Un rôle venu d'un groupe OIDC ou de FABRIQUE_ADMINS ne vaut que tant que sa source s'applique : sinon, rôle par défaut.
    src = (existing or {}).get("role_source") or ""
    if existing and not src.startswith(("groupe", "secours")):
        return existing["role"], src or "défaut"
    with db.tx() as c:
        access, _ = db.get_setting(c, "access")
    role = access.get("default_role") if access.get("default_role") in ("requester", "none") else "requester"
    return role, "défaut des nouveaux comptes"


def safe_next(n):
    if not n or not n.startswith("/") or n.startswith(("//", "/\\", "/auth/")) or any(c == "\\" or ord(c) < 32 for c in n):
        return "/"
    s = urlsplit(n)
    return "/" if s.scheme or s.netloc else n


def _wants_html(request):
    p = request.url.path
    return request.method in ("GET", "HEAD") and not p.startswith(("/api/", "/events/")) and "text/html" in request.headers.get("accept", "")


def page(request, kind, status=200, detail="", **keep):
    ctx = {"bare": True, "page": "auth", "kind": kind, "detail": detail, "next": "/", "sso": kind == "login" and oidc_on(),
           "PW_RULE": PW_RULE, "keep": keep}
    return templates.TemplateResponse(request, "auth.html", ctx, status_code=status)


class _Guard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        session = scope.get("session")
        if mode() == "setup" and not scope["path"].startswith("/auth/setup"):
            request = Request(scope)
            resp = RedirectResponse("/auth/setup", status_code=303) if _wants_html(request) else \
                JSONResponse({"error": "installation à terminer"}, status_code=503)
            return await resp(scope, receive, send)
        if scope["path"].startswith("/auth/") or valid(session):
            return await self.app(scope, receive, send)
        if session:
            session.clear()
        request = Request(scope)
        if _wants_html(request):
            target = scope["path"] + ("?" + request.url.query if request.url.query else "")
            resp = RedirectResponse("/auth/login?next=" + quote(safe_next(target), safe=""), status_code=303)
        else:
            resp = JSONResponse({"error": "connexion requise"}, status_code=401)
        await resp(scope, receive, send)


class AuthLayer:
    def __init__(self, app):
        self.app, self._sm = app, {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_public(scope["path"]):
            return await self.app(scope, receive, send)
        m = mode()
        if m == "off":
            return await self.app(scope, receive, send)
        if m == "ko":
            request = Request(scope)
            resp = page(request, "misconfigured", 503) if _wants_html(request) else \
                JSONResponse({"error": "connexion mal configurée : accès fermé"}, status_code=503)
            return await resp(scope, receive, send)
        key = (config.SESSION_SECRET, config.PUBLIC_URL, config.OIDC_SESSION_HOURS)
        if key not in self._sm:
            self._sm = {key: SessionMiddleware(_Guard(self.app), secret_key=config.SESSION_SECRET, session_cookie=COOKIE,
                                               max_age=int(config.OIDC_SESSION_HOURS * 3600), same_site="lax",
                                               https_only=config.PUBLIC_URL.startswith("https://"))}
        await self._sm[key](scope, receive, send)


_clients = {}


def client():
    cf = conf()
    sec = client_secret()
    key = (cf["issuer"], cf["client_id"], cf["scopes"], sec, id(TRANSPORT))
    if key not in _clients:
        from authlib.integrations.starlette_client import OAuth
        kw = {"scope": cf["scopes"] or "openid profile email", "code_challenge_method": "S256", "timeout": 10}
        if TRANSPORT is not None:
            kw["transport"] = TRANSPORT
        _clients.clear()
        _clients[key] = OAuth().register("oidc", client_id=cf["client_id"], client_secret=sec,
                                         server_metadata_url=cf["issuer"].rstrip("/") + "/.well-known/openid-configuration", client_kwargs=kw)
    return _clients[key]


async def _claims_options(cl):
    iss = (await cl.load_server_metadata()).get("issuer", "")
    if iss.rstrip("/") != conf()["issuer"].rstrip("/"):
        raise ValueError(f"émetteur inattendu dans la découverte : {iss!r}")
    return {"iss": {"essential": True, "values": [iss]}, "exp": {"essential": True}, "aud": {"essential": True}}


async def discover(issuer):
    """Test de la découverte depuis les settings : (ok, message). Aucune donnée secrète n'est envoyée."""
    import httpx
    u = issuer.strip().rstrip("/") + "/.well-known/openid-configuration"
    if urlsplit(u).scheme not in ("http", "https"):
        return False, "l'émetteur doit commencer par https://"
    try:
        async with httpx.AsyncClient(timeout=8, transport=TRANSPORT) as h:
            r = await h.get(u)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return False, f"découverte impossible : {type(e).__name__}"
    if (d.get("issuer") or "").rstrip("/") != issuer.strip().rstrip("/"):
        return False, f"l'émetteur annoncé ({d.get('issuer')!r}) ne correspond pas"
    missing = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if not d.get(k)]
    if missing:
        return False, "points d'accès manquants : " + ", ".join(missing)
    return True, "découverte OK · " + ", ".join(sorted(set(d.get("grant_types_supported") or ["authorization_code"]))[:3])


def _remember(user, name, email, groups, role, source):
    t = time.time()
    with db.tx() as c:
        if db.one(c, select(db.users.c.id).where(db.users.c.username == user)):
            c.execute(update(db.users).where(db.users.c.username == user).values(
                name=name, email=email, groups=groups, role=role, role_source=source, last_login=t))
        else:
            c.execute(insert(db.users).values(username=user, name=name, email=email, groups=groups, role=role,
                                              role_source=source, created_at=t, last_login=t))
        db.log_audit(c, user, "Connexion", detail=f"{ROLE_LABEL[role]} · {source}")


def _open_session(request, user, name, via):
    request.session.clear()
    request.session.update({"user": user, "name": name, "via": via, "iat": time.time(), "exp": time.time() + config.OIDC_SESSION_HOURS * 3600})


@router.get("/auth/setup")
def setup_page(request: Request):
    if mode() != "setup":
        return RedirectResponse("/", status_code=303)
    setup_code()
    return page(request, "setup")


@router.post("/auth/setup")
async def setup_do(request: Request):
    if mode() != "setup":
        return RedirectResponse("/", status_code=303)
    f = await request.form()
    kept = {k: str(f.get(k) or "")[:120] for k in ("code", "login", "name")}   # rien à retaper après un refus (sauf le mot de passe)
    if not hmac.compare_digest(str(f.get("code") or "").strip().upper().encode(), setup_code().upper().encode()):
        _setup["bad"] += 1
        if _setup["bad"] >= 10:   # trop d'essais : nouveau code, écrit dans les journaux
            _setup["code"] = ""
            announce_setup()
        return page(request, "setup", 403, "Code d'installation incorrect. Il est dans les journaux du conteneur du dashboard.", **kept)
    if str(f.get("password") or "") != str(f.get("password2") or ""):
        return page(request, "setup", 400, "Les deux mots de passe ne sont pas identiques.", **kept)
    try:
        login_ = create_local(f.get("login"), f.get("name"), str(f.get("password") or ""), "admin", "")
    except ValueError as e:
        return page(request, "setup", 400, str(e), **kept)
    _setup["code"] = ""
    log.info("installation terminée : compte admin « %s » créé", login_)
    _open_session(request, login_, str(f.get("name") or login_).strip()[:120] or login_, "local")
    return RedirectResponse("/start", status_code=303)


@router.post("/auth/password")
async def password_login(request: Request):
    f = await request.form()
    login_, pw, nxt = str(f.get("login") or "").strip().lower(), str(f.get("password") or ""), safe_next(str(f.get("next") or "/"))
    n, since = _fails.get(login_, (0, 0.0))
    if n >= 5 and time.time() - since < 900:
        return page(request, "login", 429, "Trop d'essais : réessaie dans un quart d'heure.")
    with db.tx() as c:
        u = db.one(c, select(db.users).where(db.users.c.username == login_))
    stored = (u or {}).get("password_hash")
    ok = check_password(pw, stored or hash_password("mot-de-passe-factice"))   # même coût, que le compte existe ou non
    if not (u and stored and ok and u["role"] != "none"):
        _fails[login_] = (n + 1 if time.time() - since < 900 else 1, since if n and time.time() - since < 900 else time.time())
        with db.tx() as c:
            db.log_audit(c, login_[:60] or "?", "Connexion refusée", detail="mot de passe")
        return page(request, "login", 401, "Identifiant ou mot de passe incorrect.")
    _fails.pop(login_, None)
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.id == u["id"]).values(last_login=time.time()))
        db.log_audit(c, login_, "Connexion", detail=f"{ROLE_LABEL[u['role']]} · mot de passe")
    _open_session(request, login_, u["name"] or login_, "local")
    return RedirectResponse(nxt, status_code=303)


@router.get("/auth/login")
async def login(request: Request, next: str = "/", sso: int = 0, out: int = 0):
    nxt = safe_next(next)
    if mode() != "on" or valid(request.session):
        return RedirectResponse(nxt, status_code=303)
    # Après une déconnexion (out=1), on reste sur la page de connexion de la Fabrique, même sans compte local : pas de
    # renvoi immédiat vers le provider d'identité.
    if not oidc_on() or (has_local_accounts() and not sso) or (out and not sso):
        return templates.TemplateResponse(request, "auth.html", {"bare": True, "page": "auth", "kind": "login", "detail": "",
                                                                 "next": nxt, "sso": oidc_on(), "out": bool(out)})
    request.session.clear()
    request.session["next"] = nxt
    try:
        return await client().authorize_redirect(request, config.PUBLIC_URL + "/auth/callback")
    except Exception as e:
        log.error("provider OIDC injoignable : %s", e)
        return page(request, "error", 502)


@router.get("/auth/callback")
async def callback(request: Request):
    if not oidc_on():
        return RedirectResponse("/", status_code=303)
    try:
        cl = client()
        token = await cl.authorize_access_token(request, claims_options=await _claims_options(cl))
        info = token.get("userinfo")
        if not info:
            raise ValueError("id_token absent")
    except Exception as e:
        log.warning("connexion OIDC refusée : %s", e)
        request.session.clear()
        return page(request, "error", 400)
    user = (info.get("preferred_username") or "").strip()
    email = (info.get("email") or "").strip().lower()
    raw = info.get(conf().get("groups_claim") or "groups") or []
    groups = [str(g)[:120] for g in (raw if isinstance(raw, list) else [raw])][:100]
    nxt = safe_next(request.session.get("next"))
    request.session.clear()
    with db.tx() as c:
        existing = db.one(c, select(db.users).where(db.users.c.username == (user or email)))
    if existing and existing.get("password_hash"):
        log.warning("connexion OIDC refusée : %r est un compte local", user or email)
        return page(request, "denied", 403)
    role, source = role_for(user, email, groups, existing)
    if not (user or email) or role is None:
        log.warning("compte sans rôle : %r (%r) groupes=%r", user, email, groups)
        return page(request, "denied", 403)
    name = str(info.get("name") or user or email)[:80]
    _remember(user or email, name, email, groups, role, source)
    forget_role(user or email)
    now = int(time.time())
    request.session.update(user=user or email, name=name, role=role, login=now, iat=time.time(), exp=now + int(config.OIDC_SESSION_HOURS * 3600))
    return RedirectResponse(nxt, status_code=303)


@router.post("/auth/logout")
async def logout(request: Request):
    if mode() != "on":
        return RedirectResponse("/", status_code=303)
    local = request.session.get("via") == "local"
    if valid(request.session):   # se déconnecter ferme la session partout : un cookie copié ne vaut plus rien
        revoke_sessions(request.session["user"], request.session["user"], "déconnexion", audit=False)
    request.session.clear()
    end = None
    back_to = "/auth/login?out=1"   # retour sur la page de connexion de la Fabrique, avec « Tu es déconnecté »
    if local or not oidc_on():
        return RedirectResponse(back_to, status_code=303)
    try:
        end = (await client().load_server_metadata()).get("end_session_endpoint")
    except Exception as e:
        log.warning("découverte OIDC indisponible à la déconnexion : %s", e)
    if not (end and urlsplit(end).scheme in ("http", "https")):
        return RedirectResponse(back_to, status_code=303)
    # Déconnexion côté provider (OpenID RP-Initiated Logout), puis retour à la Fabrique : l'adresse de retour doit être
    # déclarée chez le provider (Keycloak : « Valid post logout redirect URIs » ; Authentik : URI de redirection).
    from urllib.parse import urlencode
    q = urlencode({"post_logout_redirect_uri": config.PUBLIC_URL + back_to, "client_id": conf()["client_id"]})
    return RedirectResponse(end + ("&" if "?" in end else "?") + q, status_code=303)


@router.get("/auth/logged-out")
def logged_out(request: Request):
    return page(request, "logged_out")
