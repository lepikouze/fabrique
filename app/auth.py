"""Generic OIDC connection (Keycloak, Authentik, Entra ID...) : "authorization code" flow + PKCE S256 (Authlib), state and nonce,
id_token verified (issuer, audience, expiration, JWKS signature), signed session (fabrique_session cookie).

Roles: chosen in the dashboard by an admin (Settings › Users and roles); otherwise, OIDC groups if a
mapping is configured (useful for the office with Keycloak), otherwise default role for new accounts (requester, or
awaiting validation). The role is read again from the database on each request: a change applies without reconnection.

Settings: the environment provides the secrets (OIDC_CLIENT_SECRET, SESSION_SECRET) and can lock other fields;
otherwise they are configured in the dashboard (Settings › Connection). The role is recalculated at each login from the
token groups: admin > reviewer > requester. No one in any known group -> access denied (except empty requester
groups: any authenticated account is a requester). FABRIQUE_ADMINS: accounts that are always admin (fallback).

Local accounts: on first startup, the /auth/setup page creates the admin account (strong username and password), upon
presentation of the installation code written in the logs. This account remains the fallback if OIDC fails. Other
local accounts can be created by an admin. Hashed passwords (scrypt), five attempts per fifteen minutes.

Neither OIDC nor local admin account -> installation incomplete (nothing else is served). Incomplete OIDC without a local account ->
everything is closed (503). Open mode (any visitor is admin) only exists with FABRIQUE_OPEN=1, for development."""
from .i18n import M
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
ROLES = ("requester", "reviewer", "admin")
ROLE_LABEL = {"none": M("Waiting"), "requester": "Requester", "reviewer": "Reviewer", "admin": "Admin"}
ENV_FIELDS = {"issuer": "OIDC_ISSUER", "client_id": "OIDC_CLIENT_ID", "scopes": "OIDC_SCOPES", "groups_claim": "OIDC_GROUPS_CLAIM",
              "admin_groups": "OIDC_ADMIN_GROUPS", "reviewer_groups": "OIDC_REVIEWER_GROUPS", "requester_groups": "OIDC_REQUESTER_GROUPS"}
TRANSPORT = None     # tests: httpx.MockTransport (simulated provider)
templates = None
router = APIRouter()
_cache = {"at": 0.0, "conf": None}


def locked():
    """Fields fixed by the environment (displayed as locked in the settings)."""
    return {k for k, env in ENV_FIELDS.items() if getattr(config, env)}


def conf(fresh=False):
    """Effective OIDC settings: environment > base > defaults. Short cache (2 s): read on every request."""
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
    """OIDC client secret: environment variable if it exists, otherwise value entered in the settings (encrypted)."""
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
        p.append(M("SESSION_SECRET missing or too short (at least {min_secret} characters)", min_secret=MIN_SECRET))
    if not cf.get("client_id"):
        p.append(M("client identifier missing"))
    if not client_secret():
        p.append(M("client secret missing (enter it below, or set the OIDC_CLIENT_SECRET variable)"))
    if urlsplit(cf["issuer"]).scheme not in ("http", "https"):
        p.append(M("invalid issuer"))
    if urlsplit(config.PUBLIC_URL).scheme not in ("http", "https"):
        p.append(M("invalid PUBLIC_URL"))
    return p


# ---------- local accounts (emergency admin account, created on first startup) ----------

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
_local = {"at": 0.0, "has": False}
_setup = {"code": "", "bad": 0}
_fails = {}   # username -> (number of failures, time of the first): 5 attempts per quarter of an hour


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


PW_RULE = (M("At least 12 characters, with at least three kinds among: lowercase, uppercase, digits, other characters (for example - _ ! ?). It must not contain the username."))


def password_problem(pw, login=""):
    """Strong password: at least 12 characters, three character classes out of four, and not containing the username."""
    kinds = sum(bool(re.search(x, pw)) for x in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    if len(pw) < 12:
        return M("at least 12 characters")
    if len(pw) > 200:
        return M("at most 200 characters")
    if kinds < 3:
        return M("at least three kinds among: lowercase, uppercase, digits, other characters")
    if login and login.lower() in pw.lower():
        return M("it must not contain the username")
    return ""


def has_local_admin(fresh=False):
    """An admin account with a password exists (2 s cache)."""
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
        raise ValueError(M("Username: 3 to 40 characters, lowercase letters, digits, dot, hyphen."))
    why = password_problem(password or "", login)
    if why:
        raise ValueError(M("Password too weak: {why}.", why=why))
    if role not in ROLES:
        raise ValueError(M("Unknown role."))
    t = time.time()
    with db.tx() as c:
        if db.one(c, select(db.users.c.id).where(db.users.c.username == login)):
            raise ValueError(M("This account already exists."))
        c.execute(insert(db.users).values(username=login, name=(name or login).strip()[:120], email="", groups=[], role=role,
                                          role_source=M("local account"), created_at=t, password_hash=hash_password(password)))
        db.log_audit(c, by or login, M("Local account created"), target=login, detail=ROLE_LABEL[role])
    _local["at"] = 0.0
    forget_role(login)
    return login


def set_password(login, password, by):
    why = password_problem(password or "", login)
    if why:
        raise ValueError(M("Password too weak: {why}.", why=why))
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == login, db.users.c.password_hash.is_not(None)).values(password_hash=hash_password(password)))
        db.log_audit(c, by, M("Password changed"), target=login)


def setup_code():
    """Installation code: proves that the person creating the first admin account has access to the container logs."""
    if not _setup["code"]:
        _setup["code"] = config.SETUP_CODE or "-".join(secrets.token_hex(2).upper() for _ in range(4))
        _setup["bad"] = 0
    return _setup["code"]


def announce_setup():
    log.warning("FIRST START: open %s/auth/setup and enter the setup code: %s", config.PUBLIC_URL, setup_code())


def oidc_on():
    cf = conf()
    return bool(cf["issuer"]) and not problems(cf)


def mode():
    """'on': mandatory session (OIDC and/or local account); 'setup': new installation, create the admin account;
    'ko': OIDC misconfigured and no local account (everything refused); 'off': open mode, development only."""
    cf = conf()
    if cf["issuer"] and not problems(cf):
        return "on"
    # Local accounts and installation: never with a missing or too short session secret (a session could be forged).
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
        log.warning("FABRIQUE_OPEN=1: open mode, every visitor is an admin (development only)")
    elif m == "setup":
        announce_setup()
    elif m == "ko":
        log.error("sign-in misconfigured (%s): every page answers 503", " ; ".join(problems()))
    else:
        log.info("sign-in active (%s)", conf()["issuer"] if oidc_on() else "local accounts")
    for name in sorted(config.GENERATED):
        log.warning("%s missing from the environment: generated and kept with the data (see Settings)", name)


def provider_origin():
    if not oidc_on():
        return None
    u = urlsplit(conf()["issuer"])
    return f"{u.scheme}://{u.netloc}" if u.scheme in ("http", "https") and u.netloc and not any(c in u.netloc for c in " ;'\"") else None


def is_public(path):
    """Without session: health, static files, and the runners API (authenticated by the runner token, not by OIDC)."""
    return path in ("/health",) or path.startswith(("/static/", "/api/runner/"))


def valid(session):
    """Session present, not expired, and opened after the last account revocation (logout, password change,
    "Logout" by an admin): a copied cookie does not survive these events."""
    try:
        if not (session and session.get("user")) or float(session.get("exp", 0)) <= time.time():
            return False
        after = _state(session["user"])[1]
        return not after or float(session.get("iat", 0)) >= after
    except (TypeError, ValueError):
        return False


LOCAL = {"user": "local", "name": M("Local user"), "role": "admin", "local": True}


_roles = {}


def _state(username):
    """(role, session revocation timestamp) of the account, in database (one-second cache): a change made by an
    admin applies to the next request."""
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
    """All open sessions of the account cease to be valid, on all browsers."""
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == username).values(sessions_after=time.time()))
        if audit:
            db.log_audit(c, by, M("Sessions closed"), target=username, detail=why)
    forget_role(username)


def forget_role(username=None):
    _roles.clear() if username is None else _roles.pop(username, None)


def who(request):
    """{'user', 'name', 'role', 'local'} of the logged-in person. In local mode: the local user, whose role can be
    simulated (fabrique_as cookie) to see the interface of a requester or a reviewer."""
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
    """Rights by role. The requester only sees their own requests (filtered in the queries)."""
    r = (user or {}).get("role")
    need = {"request": "requester", "answer_own": "requester",
            "see_all": "reviewer", "answer": "reviewer", "decide": "reviewer", "archive": "reviewer", "see_infra": "reviewer",
            "settings": "admin", "repos_edit": "admin", "runners_edit": "admin"}[action]
    return r in ROLES and ROLES.index(r) >= ROLES.index(need)


def role_for(username, email, groups, existing=None, cf=None):
    """(role, source) at login. Order: fallback > admin choice in the dashboard > OIDC groups > default role.
    (None, reason): refusal (requester groups set and the person is not in them)."""
    cf = cf or conf()
    if (username or "").lower() in config.FABRIQUE_ADMINS or (email or "").lower() in config.FABRIQUE_ADMINS:
        return "admin", "break-glass (FABRIQUE_ADMINS)"
    if existing and existing.get("role_source") == "dashboard":
        return existing["role"], "dashboard"
    g = {x.lower() for x in groups}
    for role, key in (("admin", "admin_groups"), ("reviewer", "reviewer_groups"), ("requester", "requester_groups")):
        hit = sorted(g & {x.lower() for x in cf.get(key) or []})
        if hit:
            return role, f"group {hit[0]}"
    if cf.get("requester_groups"):
        return None, M("no authorized group")
    # A role coming from an OIDC group or from FABRIQUE_ADMINS is only valid as long as its source applies: otherwise, default role.
    src = (existing or {}).get("role_source") or ""
    if existing and not src.startswith(("group", "break-glass")):
        return existing["role"], src or M("default", _ctx="role")
    with db.tx() as c:
        access, _ = db.get_setting(c, "access")
    role = access.get("default_role") if access.get("default_role") in ("requester", "none") else "requester"
    return role, M("default for new accounts")


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
                JSONResponse({"error": M("setup to finish")}, status_code=503)
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
            resp = JSONResponse({"error": "sign-in required"}, status_code=401)
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
                JSONResponse({"error": M("sign-in misconfigured: access closed")}, status_code=503)
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
        raise ValueError(M("unexpected issuer in the discovery document: {iss}", iss=f"{iss!r}"))
    return {"iss": {"essential": True, "values": [iss]}, "exp": {"essential": True}, "aud": {"essential": True}}


async def discover(issuer):
    """Test of discovery from settings: (ok, message). No secret data is sent."""
    import httpx
    u = issuer.strip().rstrip("/") + "/.well-known/openid-configuration"
    if urlsplit(u).scheme not in ("http", "https"):
        return False, M("the issuer must start with https://")
    try:
        async with httpx.AsyncClient(timeout=8, transport=TRANSPORT) as h:
            r = await h.get(u)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        return False, M("discovery failed: {name}", name=type(e).__name__)
    if (d.get("issuer") or "").rstrip("/") != issuer.strip().rstrip("/"):
        return False, M("the advertised issuer ({issuer}) does not match", issuer=f"{d.get('issuer')!r}")
    missing = [k for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if not d.get(k)]
    if missing:
        return False, M("missing endpoints: ") + ", ".join(missing)
    return True, M("discovery OK · ") + ", ".join(sorted(set(d.get("grant_types_supported") or ["authorization_code"]))[:3])


def _remember(user, name, email, groups, role, source):
    t = time.time()
    with db.tx() as c:
        if db.one(c, select(db.users.c.id).where(db.users.c.username == user)):
            c.execute(update(db.users).where(db.users.c.username == user).values(
                name=name, email=email, groups=groups, role=role, role_source=source, last_login=t))
        else:
            c.execute(insert(db.users).values(username=user, name=name, email=email, groups=groups, role=role,
                                              role_source=source, created_at=t, last_login=t))
        db.log_audit(c, user, M("Sign-in"), detail=f"{ROLE_LABEL[role]} · {source}")


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
    kept = {k: str(f.get(k) or "")[:120] for k in ("code", "login", "name")}   # nothing to retype after a refusal (except the password)
    if not hmac.compare_digest(str(f.get("code") or "").strip().upper().encode(), setup_code().upper().encode()):
        _setup["bad"] += 1
        if _setup["bad"] >= 10:   # too many attempts: new code, written in the logs
            _setup["code"] = ""
            announce_setup()
        return page(request, "setup", 403, M("Wrong setup code. It is in the dashboard container's logs."), **kept)
    if str(f.get("password") or "") != str(f.get("password2") or ""):
        return page(request, "setup", 400, M("The two passwords are not the same."), **kept)
    try:
        login_ = create_local(f.get("login"), f.get("name"), str(f.get("password") or ""), "admin", "")
    except ValueError as e:
        return page(request, "setup", 400, str(e), **kept)
    _setup["code"] = ""
    log.info("setup finished: admin account “%s” created", login_)
    _open_session(request, login_, str(f.get("name") or login_).strip()[:120] or login_, "local")
    return RedirectResponse("/start", status_code=303)


@router.post("/auth/password")
async def password_login(request: Request):
    f = await request.form()
    login_, pw, nxt = str(f.get("login") or "").strip().lower(), str(f.get("password") or ""), safe_next(str(f.get("next") or "/"))
    n, since = _fails.get(login_, (0, 0.0))
    if n >= 5 and time.time() - since < 900:
        return page(request, "login", 429, M("Too many attempts: try again in a quarter of an hour."))
    with db.tx() as c:
        u = db.one(c, select(db.users).where(db.users.c.username == login_))
    stored = (u or {}).get("password_hash")
    ok = check_password(pw, stored or hash_password("dummy-password"))   # same cost, whether the account exists or not
    if not (u and stored and ok and u["role"] != "none"):
        _fails[login_] = (n + 1 if time.time() - since < 900 else 1, since if n and time.time() - since < 900 else time.time())
        with db.tx() as c:
            db.log_audit(c, login_[:60] or "?", M("Sign-in refused"), detail=M("password"))
        return page(request, "login", 401, M("Wrong username or password."))
    _fails.pop(login_, None)
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.id == u["id"]).values(last_login=time.time()))
        db.log_audit(c, login_, M("Sign-in"), detail=M("{role} · password", role=ROLE_LABEL[u['role']]))
    _open_session(request, login_, u["name"] or login_, "local")
    return RedirectResponse(nxt, status_code=303)


@router.get("/auth/login")
async def login(request: Request, next: str = "/", sso: int = 0, out: int = 0):
    nxt = safe_next(next)
    if mode() != "on" or valid(request.session):
        return RedirectResponse(nxt, status_code=303)
    # After a logout (out=1), we stay on the Fabrique login page, even without a local account: no
    # immediate redirect to the identity provider.
    if not oidc_on() or (has_local_accounts() and not sso) or (out and not sso):
        return templates.TemplateResponse(request, "auth.html", {"bare": True, "page": "auth", "kind": "login", "detail": "",
                                                                 "next": nxt, "sso": oidc_on(), "out": bool(out)})
    request.session.clear()
    request.session["next"] = nxt
    try:
        return await client().authorize_redirect(request, config.PUBLIC_URL + "/auth/callback")
    except Exception as e:
        log.error("OIDC provider unreachable: %s", e)
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
        log.warning("OIDC sign-in refused: %s", e)
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
        log.warning("OIDC sign-in refused: %r is a local account", user or email)
        return page(request, "denied", 403)
    role, source = role_for(user, email, groups, existing)
    if not (user or email) or role is None:
        log.warning("account without a role: %r (%r) groups=%r", user, email, groups)
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
    if valid(request.session):   # logging out closes the session everywhere: a copied cookie is worth nothing
        revoke_sessions(request.session["user"], request.session["user"], M("sign-out"), audit=False)
    request.session.clear()
    end = None
    back_to = "/auth/login?out=1"   # return to the Fabrique login page, with "You are logged out"
    if local or not oidc_on():
        return RedirectResponse(back_to, status_code=303)
    try:
        end = (await client().load_server_metadata()).get("end_session_endpoint")
    except Exception as e:
        log.warning("OIDC discovery unavailable at sign-out: %s", e)
    if not (end and urlsplit(end).scheme in ("http", "https")):
        return RedirectResponse(back_to, status_code=303)
    # Provider-side logout (OpenID RP-Initiated Logout), then return to Fabrique: the return address must be
    # declared with the provider (Keycloak: "Valid post logout redirect URIs"; Authentik: redirection URI).
    from urllib.parse import urlencode
    q = urlencode({"post_logout_redirect_uri": config.PUBLIC_URL + back_to, "client_id": conf()["client_id"]})
    return RedirectResponse(end + ("&" if "?" in end else "?") + q, status_code=303)


@router.get("/auth/logged-out")
def logged_out(request: Request):
    return page(request, "logged_out")
