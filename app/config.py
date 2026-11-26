"""Settings read from the environment at startup. Settings modifiable from the dashboard (non-secret OIDC, roles,
agents, budgets...) are stored in the database (settings table); the environment provides secrets and default values.
A value set by the environment is displayed as locked in the settings."""
import os


def _env(k, d=""):
    return os.environ.get(k, d).strip()


def _list(k):
    return [x.strip().lower() for x in _env(k).replace(";", ",").split(",") if x.strip()]


DATA_DIR = _env("FABRIQUE_DATA", "/data")
GENERATED = set()   # secrets absent from the environment, generated in the data directory (noted in the settings)


def _secret(env):
    """Secret provided by the environment (recommended: it stays separate from the database). Otherwise it is generated once and kept
    in DATA_DIR/secrets.json (readable by the application only), so that an installation can start without a setting."""
    import json, secrets
    v = _env(env)
    if v:
        return v
    path = os.path.join(DATA_DIR, "secrets.json")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
        if not isinstance(data, dict):
            raise ValueError("unexpected content")
        if not data.get(env):
            data[env] = secrets.token_urlsafe(48)
            # Atomic write: temporary file then replacement. An interrupted write (disk full, shutdown)
            # never leaves an empty or half-written file in place of the old one.
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        GENERATED.add(env)
        return data[env]
    except (OSError, ValueError) as e:
        # Never an empty secret: without a session secret, anyone could sign their own session. The dashboard refuses
        # to start rather than running without protection.
        raise SystemExit(f"{env}: neither an environment variable nor a usable secret in {path} ({e}). Provide {env} through "
                         "the environment, or make this directory writable (and delete the file if it is empty or damaged).")
# Database address: SQLite by default; PostgreSQL later (postgresql+psycopg://...) without changing the code.
DATABASE_URL = _env("DATABASE_URL") or f"sqlite:///{os.path.join(DATA_DIR, 'fabrique.db')}"
DEMO = _env("FABRIQUE_DEMO", "0") == "1"            # data and demonstration simulator
DEMO_TICK = float(_env("FABRIQUE_DEMO_TICK", "6"))   # seconds between two simulator steps
PUBLIC_URL_RAW = _env("PUBLIC_URL", "http://localhost:8080").rstrip("/")
# Input without protocol ("fabrique.example.org"): https, otherwise the session cookie would not be restricted to HTTPS and
# the links in notifications, as well as the OIDC return address, would be wrong.
PUBLIC_URL = PUBLIC_URL_RAW if "://" in PUBLIC_URL_RAW else ("https://" + PUBLIC_URL_RAW.lstrip("/") if PUBLIC_URL_RAW else PUBLIC_URL_RAW)
JOB_PREFIX = _env("FABRIQUE_PREFIX", "FAB")
# Runner API rejected when the request passed through a reverse proxy (all-in-one case, where the runner is on the
# internal network); 0 when runners are on other machines and arrive via the reverse proxy.
RUNNER_API_INTERNAL = _env("FABRIQUE_RUNNER_API_INTERNAL", "1") == "1"
APP_VERSION = "0.8.10"                                   # number of the delivered version
BUILD = _env("FABRIQUE_VERSION", "dev")              # commit sha, provided to the image build
VERSION = f"v{APP_VERSION} · {BUILD}"

# OIDC Connection. The client secret and the session secret come only from the environment, never from the database.
OIDC_ISSUER = _env("OIDC_ISSUER")
OIDC_CLIENT_ID = _env("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = _env("OIDC_CLIENT_SECRET")
OIDC_SCOPES = _env("OIDC_SCOPES")
OIDC_GROUPS_CLAIM = _env("OIDC_GROUPS_CLAIM")
OIDC_ADMIN_GROUPS = _list("OIDC_ADMIN_GROUPS")
OIDC_REVIEWER_GROUPS = _list("OIDC_REVIEWER_GROUPS")
OIDC_REQUESTER_GROUPS = _list("OIDC_REQUESTER_GROUPS")
# Always admin accounts (fallback if group matching is misconfigured).
FABRIQUE_ADMINS = _list("FABRIQUE_ADMINS")
SESSION_SECRET = _secret("SESSION_SECRET")
SECRET_KEY = _secret("FABRIQUE_SECRET_KEY")           # encrypts secrets stored in the database (model keys, channels...)
# Built-in runner (all-in-one compose): at startup, the dashboard creates a runner and writes its token to this file, on a
# volume shared with the runner only. Empty: runners are registered manually (Runners -> New runner).
BOOTSTRAP_RUNNER_FILE = _env("FABRIQUE_BOOTSTRAP_RUNNER_FILE")
# Kubernetes: the dashboard and the runner do not share a file. The built-in runner's token is provided to both by a
# Secret; the dashboard registers the runner with this token on the first startup.
BOOTSTRAP_RUNNER_TOKEN = _env("FABRIQUE_BOOTSTRAP_RUNNER_TOKEN")
# Open mode (development and local demo ONLY): no sign-in, every visitor is an admin.
OPEN = _env("FABRIQUE_OPEN", "0") == "1"
SETUP_CODE = _env("FABRIQUE_SETUP_CODE")              # setup code set by the environment; otherwise generated and written in the logs
OIDC_SESSION_HOURS = float(_env("OIDC_SESSION_HOURS", "12"))
