"""Database: SQLAlchemy Core (no engine-specific SQL), Alembic migrations (app/migrations).
SQLite at first; PostgreSQL later by only changing DATABASE_URL."""
import logging
import contextvars, copy, json, os, time
from contextlib import contextmanager
from sqlalchemy import (JSON, Boolean, Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, event, false, insert, select, update)
from sqlalchemy.types import TypeDecorator
from . import config, i18n


class Worded(TypeDecorator):
    """A text column that may hold what Fabrique words itself: such a text is stored as a fact (its template and its
    values, see i18n.pack) and comes back as a Msg, which each reader sees in their own language. Any other text (what
    a person or an agent wrote) is stored and returned as it is."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return i18n.pack(value)

    def process_result_value(self, value, dialect):
        return i18n.revive(value)


meta = MetaData()

users = Table(
    "users", meta,
    Column("id", Integer, primary_key=True),
    Column("username", String(120), nullable=False, unique=True),
    Column("name", String(120), nullable=False, default=""),
    Column("email", String(200), nullable=False, default=""),
    Column("role", String(20), nullable=False, default="requester"),   # none (pending) | requester | reviewer | admin
    Column("role_source", String(40), nullable=False, default=""),     # "dashboard" (chosen by an admin), OIDC group, default, fallback
    Column("requested_role", String(20)),                              # access request pending
    Column("request_note", Text),
    Column("requested_at", Float),
    Column("groups", JSON, nullable=False, default=list),
    Column("created_at", Float, nullable=False),
    Column("last_login", Float),
    Column("prefs", JSON),                                             # personal preferences (job display)
    Column("sessions_after", Float),                                   # sessions open before this moment: revoked
    Column("password_hash", String(300)),                              # local account (fallback): hashed password (scrypt)
)

repos = Table(
    "repos", meta,
    Column("id", Integer, primary_key=True),
    Column("provider", String(20), nullable=False),        # forgejo | gitea | gitlab | github
    Column("git_id", String(60), nullable=False, default=""),  # specific Git provider (Settings → Git providers), see app/forges.py
    Column("slug", String(200), nullable=False),
    Column("name", String(120), nullable=False),
    Column("default_branch", String(80), nullable=False, default="main"),
    Column("is_default", Boolean, nullable=False, default=False),
    Column("accepts_jobs", Boolean, nullable=False, default=True),
    Column("onboarding", String(20), nullable=False, default="pending"),   # pending | analyzing | ready | failed
    Column("recipe", JSON),                                 # content of .fabrique/setup.json once onboarding is done
    Column("settings", JSON, nullable=False, default=dict),  # harness, model, merge_policy, protected_paths, size_limit, budget_job
    Column("created_at", Float, nullable=False),
    Column("last_activity", Float),
    UniqueConstraint("git_id", "slug", name="uq_repos_git_slug"),   # the same path can exist in two providers
)

notifications = Table(
    "notifications", meta,
    Column("id", Integer, primary_key=True),
    Column("channel", String(40), nullable=False),            # channel identifier ("channels" setting)
    Column("event", String(30), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("state", String(12), nullable=False, index=True),  # pending | sent | failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("last_error", String(300), nullable=False, default=""),
    Column("created_at", Float, nullable=False),
    Column("next_try", Float, nullable=False),
    Column("sent_at", Float),
)

runners = Table(
    "runners", meta,
    Column("id", Integer, primary_key=True),
    Column("name", String(120), nullable=False, unique=True),
    Column("host", String(200), nullable=False, default=""),
    Column("kind", String(20), nullable=False, default="vm"),   # vm | container | kubernetes
    Column("online", Boolean, nullable=False, default=False),
    Column("last_seen", Float),
    Column("capacity", Integer, nullable=False, default=2),
    Column("in_flight", Integer, nullable=False, default=0),
    Column("harnesses", JSON, nullable=False, default=list),   # [{"name": "Claude Code", "version": "…"}]
    Column("metrics", JSON, nullable=False, default=dict),     # cpu, mem, disk
    Column("version", String(40), nullable=False, default=""),
    Column("token_hash", String(64)),                          # SHA-256 fingerprint of the runner token (the token is never stored)
    Column("revoked", Boolean, nullable=False, default=False, server_default=false()),
    Column("tags", JSON, nullable=False, default=list, server_default="[]"),   # tags declared by the runner (RUNNER_TAGS)
)

jobs = Table(
    "jobs", meta,
    Column("id", Integer, primary_key=True),
    Column("ref", String(40), nullable=False, unique=True),
    Column("title", String(200), nullable=False),
    Column("request", Text, nullable=False, default=""),
    Column("done_when", Text, nullable=False, default=""),
    Column("requester", String(120), nullable=False),
    Column("source", String(20), nullable=False, default="form"),
    Column("repo_id", Integer, ForeignKey("repos.id"), nullable=False),
    Column("stage", String(20), nullable=False, default="queued"),
    Column("activity", String(10), nullable=False, default="idle"),   # active | idle
    Column("status_text", Worded, nullable=False, default=""),
    Column("summary", Worded, nullable=False, default=""),
    Column("harness", String(40), nullable=False, default=""),
    Column("model", String(80), nullable=False, default=""),
    Column("runner_id", Integer, ForeignKey("runners.id")),
    Column("branch", String(120), nullable=False, default=""),
    Column("pr_number", Integer),
    Column("pr_url", String(300)),
    Column("cost", Float, nullable=False, default=0.0),
    Column("tokens", Integer, nullable=False, default=0),
    Column("budget", Float, nullable=False, default=0.0),
    Column("verified_sha", String(40)),
    Column("checks_ok", Integer, nullable=False, default=0),
    Column("checks_total", Integer, nullable=False, default=0),
    Column("merge_state", String(20), nullable=False, default=""),   # "" | waiting_checks | ready | approved | refused | merged
    Column("archived", Boolean, nullable=False, default=False),
    Column("report", Text),
    Column("lang", String(8), nullable=False, default=""),    # job language when it was filed (what agents and Fabrique write for people)
    Column("models", JSON),                                    # profile: {profile, effort, review_model, review_effort}, frozen at creation
    Column("report_seen", Float),                              # study: report marked as read (leaves the inbox)                                    # study mode: report submitted by the agent
    Column("session_id", String(80)),                          # agent session (resumed after a response)
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("diff", JSON),                                      # {"files": [...], "additions": n, "deletions": n} of the PR
    Column("cancel_requested", String(120)),                   # who requested the stop (the runner kills the agent and confirms)
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index("ix_jobs_stage", "stage"),
)

questions = Table(
    "questions", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("question", Text, nullable=False),
    Column("asked_at", Float, nullable=False),
    Column("answer", Text),
    Column("answered_by", String(120)),
    Column("answered_at", Float),
)

timeline = Table(
    "timeline", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("stage", String(20), nullable=False),
    Column("summary", Worded, nullable=False, default=""),
    Column("at", Float, nullable=False),
)

# Event stream: used for both job "live events" and dashboard real-time (SSE).
events = Table(
    "events", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), index=True),
    Column("at", Float, nullable=False),
    Column("kind", String(30), nullable=False),
    Column("message", Worded, nullable=False, default=""),
)

agents = Table(
    "agents", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("parent_id", Integer),
    Column("name", String(80), nullable=False),
    Column("task", String(240), nullable=False, default=""),
    Column("model", String(80), nullable=False, default=""),
    Column("tokens", Integer, nullable=False, default=0),
    Column("cost", Float, nullable=False, default=0.0),
    Column("state", String(20), nullable=False, default="running"),   # running | done
)

evidence = Table(
    "evidence", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("sha", String(40), nullable=False),
    Column("name", String(60), nullable=False),
    Column("command", String(300), nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("output", Text, nullable=False, default=""),
    Column("ran_by", String(20), nullable=False, default="runner"),   # runner | agent
    Column("kind", String(20), nullable=False, default="check"),      # check (a recipe check) | security | network (for information)
    Column("warn", Boolean, nullable=False, default=False),           # an informative line that reports something
    Column("at", Float, nullable=False),
)

decisions = Table(
    "decisions", meta,
    Column("job_id", Integer, ForeignKey("jobs.id"), primary_key=True),
    Column("policy", String(20), nullable=False),          # off | trial | confident
    Column("recommendation", String(240), nullable=False, default=""),
    Column("interpretation", Text, nullable=False, default=""),
    Column("assumptions", JSON, nullable=False, default=list),
    Column("confident", Boolean, nullable=False, default=False),
    Column("verdict", Text, nullable=False, default=""),
    Column("notes", Text, nullable=False, default=""),
    Column("limits", JSON, nullable=False, default=list),   # [{"name", "ok", "detail"}] : gates evaluated by the code
    Column("at", Float, nullable=False),
)

translations = Table(
    "translations", meta,                                     # machine translations of what a job's agents wrote, kept per text and language
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("lang", String(8), nullable=False),
    Column("source_hash", String(64), nullable=False),        # sha256 of the source text: a text that changes is translated again
    Column("text", Text, nullable=False),
    Column("model", String(80), nullable=False, default=""),
    Column("cost", Float, nullable=False, default=0.0),
    Column("at", Float, nullable=False),
    UniqueConstraint("job_id", "lang", "source_hash", name="uq_translations_job_lang_source"),
)

audit = Table(
    "audit", meta,
    Column("id", Integer, primary_key=True),
    Column("at", Float, nullable=False, index=True),
    Column("actor", String(120), nullable=False),
    Column("action", Worded, nullable=False),
    Column("job_id", Integer, ForeignKey("jobs.id")),
    Column("target", String(200), nullable=False, default=""),
    Column("detail", Worded, nullable=False, default=""),
)

# Reviews: one reviewer agent per role (code, security, architecture, infra), read-only, on the verified sha.
reviews = Table(
    "reviews", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("sha", String(40), nullable=False),
    Column("role", String(20), nullable=False),              # code | security | architecture | infra
    Column("verdict", String(20), nullable=False),           # ok | needs_changes | blocking | undetermined
    Column("summary", Worded, nullable=False, default=""),
    Column("points", JSON, nullable=False, default=list),
    Column("model", String(80), nullable=False, default=""),
    Column("at", Float, nullable=False),
)

# Job diff at the verified sha, sent by the runner with the PR (the dashboard never reads the code itself). Text capped;
# "files" summarizes each file for display without re-reading all the text. One line per job: the latest diff replaces the old one.
diffs = Table(
    "diffs", meta,
    Column("job_id", Integer, ForeignKey("jobs.id"), primary_key=True),
    Column("sha", String(40), nullable=False),
    Column("text", Text, nullable=False, default=""),
    Column("truncated", Boolean, nullable=False, default=False),
    Column("files", JSON, nullable=False, default=list),     # [{"path", "add", "rem", "status"}]
    Column("at", Float, nullable=False),
)

# Order queue, dashboard -> runners (idempotent, with a lease): start, resume after a response, merge.
intents = Table(
    "intents", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("kind", String(20), nullable=False),               # start | resume | merge
    Column("payload", JSON, nullable=False, default=dict),
    Column("state", String(20), nullable=False, default="pending"),   # pending | leased | done | failed
    Column("runner_id", Integer, ForeignKey("runners.id")),
    Column("lease_until", Float),
    Column("attempts", Integer, nullable=False, default=0),
    Column("note", Text, nullable=False, default=""),
    Column("created_at", Float, nullable=False),
    Column("done_at", Float),
)

settings = Table(
    "settings", meta,
    Column("key", String(60), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_by", String(120), nullable=False, default=""),
    Column("updated_at", Float, nullable=False),
)

_engine = None


NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p"}


def fs_type(path, mounts="/proc/mounts"):
    """Type of the file system hosting this path (the longest mount point containing it); '' if unknown."""
    path, best, typ = os.path.realpath(path), "", ""
    try:
        for line in open(mounts):
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt = parts[1].replace("\\040", " ")
            if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                best, typ = mnt, parts[2]
    except OSError:
        return ""
    return typ


def journal_mode(db_path):
    """SQLite journal mode. WAL (fast) on a local disk; rollback ("delete") on a network file system:
    WAL relies on shared memory which NFS does not provide, and access will hang (pages spinning endlessly).
    FABRIQUE_SQLITE_JOURNAL=wal|delete forces the choice."""
    forced = (os.environ.get("FABRIQUE_SQLITE_JOURNAL") or "").strip().lower()
    if forced in ("wal", "delete"):
        return forced
    return "delete" if fs_type(os.path.dirname(os.path.abspath(db_path)) or ".") in NETWORK_FS else "wal"


def engine():
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        kw = {}
        if url.startswith("sqlite"):
            os.makedirs(os.path.dirname(url.split("///", 1)[1]) or ".", exist_ok=True)
            kw["connect_args"] = {"check_same_thread": False, "timeout": 15}
        _engine = create_engine(url, future=True, json_serializer=lambda o: json.dumps(i18n.pack_tree(o), ensure_ascii=False),
                                json_deserializer=lambda s: i18n.revive_tree(json.loads(s)), **kw)
        if url.startswith("sqlite"):
            mode = journal_mode(url.split("///", 1)[1])
            if mode == "delete":
                logging.getLogger("fabrique.db").warning(
                    "database on a network file system: SQLite journal in rollback mode (WAL does not work there). "
                    "For better performance, put the data volume on local or block storage.")

            @event.listens_for(_engine, "connect")
            def _pragmas(dbapi, _):
                c = dbapi.cursor()
                c.execute(f"PRAGMA journal_mode={mode.upper()}"); c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=15000")
                c.close()
    return _engine


def reset_engine():
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def tx():
    with engine().begin() as c:
        yield c


def migrate():
    """Applies Alembic migrations up to the latest version."""
    from alembic import command
    from alembic.config import Config
    here = os.path.dirname(__file__)
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    cfg.set_main_option("sqlalchemy.url", config.DATABASE_URL)
    command.upgrade(cfg, "head")


def rows(c, stmt):
    return [dict(r._mapping) for r in c.execute(stmt)]


def one(c, stmt):
    r = c.execute(stmt).first()
    return dict(r._mapping) if r else None


def now():
    return time.time()


# Global settings: default values, then the database (editable in the dashboard).
DEFAULTS = {
    "agents": {"harness": "claude-code", "model": "", "profile": ""},   # no model until the installation has chosen one
    "models": [],
    "profiles": [],                              # model profiles: one model and one effort per role (app/profiles.py)
    "git": [],
    "limits": {"budget_job": 5.0, "budget_day": 30.0, "max_parallel": 2, "size_limit": 300, "watchdog_min": 10,
               "protected_paths": ["**/migrations/**", "**/auth/**", "**/permissions/**", ".forgejo/**", ".gitea/**", ".gitlab-ci.yml",
                                   ".github/**", "Dockerfile", "**/.env*", ".fabrique/**"]},
    "access": {"default_role": "requester"},
    "translation": {"model": ""},               # model that translates a job's texts on request ("" : no translation offered)
    "languages": {"jobs": "en"},                # job language: what Fabrique and its agents write for people (overridable per repo)
    "oidc_secret": {},                           # OIDC client secret entered in the settings: {"v": encrypted value}
    "demo": {"visible": False},                # demo data displayed or hidden (FABRIQUE_DEMO=1)   # role of a new account: requester, or none (pending admin)
    "oidc": {"issuer": "", "client_id": "", "scopes": "openid profile email", "groups_claim": "groups",
             "admin_groups": [], "reviewer_groups": [], "requester_groups": []},
}


# Cache for an HTTP read request (GET): a page calls the same reads multiple times (settings, job list
# for the page, counters and the inbox). Each read is expensive when the database is on network storage: it is done
# once per request. Outside of a request (runner, simulator, direct tests) and for any request that modifies: no cache.
_request = contextvars.ContextVar("fabrique_request_cache", default=None)


class RequestCache:
    """ASGI Middleware: opens a read cache for the duration of a GET page request."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("GET", "HEAD") or scope.get("path", "").startswith(("/events/", "/api/", "/static/")):
            return await self.app(scope, receive, send)
        token = _request.set({})
        try:
            await self.app(scope, receive, send)
        finally:
            _request.reset(token)


def cached(key, load):
    """Result of load(), kept for the current request; simple call outside of a request."""
    box = _request.get()
    if box is None:
        return load()
    if key not in box:
        box[key] = load()
    return box[key]


def forget_cache():
    box = _request.get()
    if box:
        box.clear()


def get_setting(c, key):
    if _request.get() is not None:   # in a page request: all settings in a single read (the table is small)
        allv = cached("settings", lambda: {r["key"]: r for r in rows(c, select(settings))})
        r = allv.get(key)
        if r is None:
            return copy.deepcopy(DEFAULTS.get(key)), None
        r = copy.deepcopy(r)         # the caller can modify what it receives without touching the cache
        return r["value"], r
    r = one(c, select(settings).where(settings.c.key == key))
    if r is None:
        return DEFAULTS.get(key), None
    return r["value"], r


def set_setting(c, key, value, by):
    forget_cache()
    t = now()
    if one(c, select(settings.c.key).where(settings.c.key == key)):
        c.execute(update(settings).where(settings.c.key == key).values(value=value, updated_by=by, updated_at=t))
    else:
        c.execute(insert(settings).values(key=key, value=value, updated_by=by, updated_at=t))


def log_event(c, job_id, kind, message):
    c.execute(insert(events).values(job_id=job_id, at=now(), kind=kind, message=message))


def log_audit(c, actor, action, job_id=None, target="", detail=""):
    c.execute(insert(audit).values(at=now(), actor=actor, action=action, job_id=job_id, target=target, detail=detail))
    # Any audit action also triggers a real-time refresh.
    c.execute(insert(events).values(job_id=job_id, at=now(), kind="audit", message=action))
