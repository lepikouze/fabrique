"""Base de données : SQLAlchemy Core (aucun SQL propre à un moteur), migrations Alembic (app/migrations).
SQLite au départ ; PostgreSQL plus tard en changeant seulement DATABASE_URL."""
import logging
import json, os, time
from contextlib import contextmanager
from sqlalchemy import (JSON, Boolean, Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, event, false, insert, select, update)
from . import config

meta = MetaData()

users = Table(
    "users", meta,
    Column("id", Integer, primary_key=True),
    Column("username", String(120), nullable=False, unique=True),
    Column("name", String(120), nullable=False, default=""),
    Column("email", String(200), nullable=False, default=""),
    Column("role", String(20), nullable=False, default="requester"),   # none (en attente) | requester | verifier | admin
    Column("role_source", String(40), nullable=False, default=""),     # « panneau » (choisi par un admin), groupe OIDC, défaut, secours
    Column("requested_role", String(20)),                              # demande d'accès en attente
    Column("request_note", Text),
    Column("requested_at", Float),
    Column("groups", JSON, nullable=False, default=list),
    Column("created_at", Float, nullable=False),
    Column("last_login", Float),
    Column("prefs", JSON),                                             # préférences personnelles (affichage des jobs)
    Column("sessions_after", Float),                                   # sessions ouvertes avant cet instant : révoquées
    Column("password_hash", String(300)),                              # compte local (secours) : mot de passe haché (scrypt)
)

repos = Table(
    "repos", meta,
    Column("id", Integer, primary_key=True),
    Column("provider", String(20), nullable=False),        # forgejo | gitea | gitlab | github
    Column("git_id", String(60), nullable=False, default=""),  # provider Git précis (Settings → Providers Git), voir app/forges.py
    Column("slug", String(200), nullable=False),
    Column("name", String(120), nullable=False),
    Column("default_branch", String(80), nullable=False, default="main"),
    Column("is_default", Boolean, nullable=False, default=False),
    Column("accepts_jobs", Boolean, nullable=False, default=True),
    Column("onboarding", String(20), nullable=False, default="pending"),   # pending | analyzing | ready | failed
    Column("recipe", JSON),                                 # contenu de .fabrique/setup.json une fois l'onboarding fait
    Column("settings", JSON, nullable=False, default=dict),  # harness, model, merge_policy, protected_paths, size_limit, budget_job
    Column("created_at", Float, nullable=False),
    Column("last_activity", Float),
    UniqueConstraint("git_id", "slug", name="uq_repos_git_slug"),   # un même chemin peut exister chez deux providers
)

notifications = Table(
    "notifications", meta,
    Column("id", Integer, primary_key=True),
    Column("channel", String(40), nullable=False),            # identifiant du canal (réglage « channels »)
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
    Column("kind", String(20), nullable=False, default="vm"),   # vm | conteneur | kubernetes
    Column("online", Boolean, nullable=False, default=False),
    Column("last_seen", Float),
    Column("capacity", Integer, nullable=False, default=2),
    Column("in_flight", Integer, nullable=False, default=0),
    Column("harnesses", JSON, nullable=False, default=list),   # [{"name": "Claude Code", "version": "…"}]
    Column("metrics", JSON, nullable=False, default=dict),     # cpu, mem, disk
    Column("version", String(40), nullable=False, default=""),
    Column("token_hash", String(64)),                          # empreinte SHA-256 du jeton du runner (le jeton n'est jamais stocké)
    Column("revoked", Boolean, nullable=False, default=False, server_default=false()),
    Column("tags", JSON, nullable=False, default=list, server_default="[]"),   # étiquettes déclarées par le runner (RUNNER_TAGS)
)

jobs = Table(
    "jobs", meta,
    Column("id", Integer, primary_key=True),
    Column("ref", String(40), nullable=False, unique=True),
    Column("title", String(200), nullable=False),
    Column("request", Text, nullable=False, default=""),
    Column("done_when", Text, nullable=False, default=""),
    Column("requester", String(120), nullable=False),
    Column("source", String(20), nullable=False, default="formulaire"),
    Column("repo_id", Integer, ForeignKey("repos.id"), nullable=False),
    Column("stage", String(20), nullable=False, default="queued"),
    Column("activity", String(10), nullable=False, default="idle"),   # active | idle
    Column("status_text", String(240), nullable=False, default=""),
    Column("summary", Text, nullable=False, default=""),
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
    Column("models", JSON),                                    # profil : {profile, effort, review_model, review_effort}, figé à la création
    Column("report_seen", Float),                              # étude : rapport marqué comme lu (sort de l'inbox)                                    # mode étude : rapport rendu par l'agent
    Column("session_id", String(80)),                          # session de l'agent (reprise après une réponse)
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("diff", JSON),                                      # {"files": [...], "additions": n, "deletions": n} de la PR
    Column("cancel_requested", String(120)),                   # qui a demandé l'arrêt (le runner tue l'agent et confirme)
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
    Column("summary", Text, nullable=False, default=""),
    Column("at", Float, nullable=False),
)

# Flux d'événements : sert à la fois aux « événements en direct » d'un job et au temps réel (SSE) du panneau.
events = Table(
    "events", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), index=True),
    Column("at", Float, nullable=False),
    Column("kind", String(30), nullable=False),
    Column("message", Text, nullable=False, default=""),
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
    Column("limits", JSON, nullable=False, default=list),   # [{"name", "ok", "detail"}] : barrières évaluées par le code
    Column("at", Float, nullable=False),
)

audit = Table(
    "audit", meta,
    Column("id", Integer, primary_key=True),
    Column("at", Float, nullable=False, index=True),
    Column("actor", String(120), nullable=False),
    Column("action", String(60), nullable=False),
    Column("job_id", Integer, ForeignKey("jobs.id")),
    Column("target", String(200), nullable=False, default=""),
    Column("detail", Text, nullable=False, default=""),
)

# Relectures : un agent relecteur par rôle (code, sécurité, architecture, infra), en lecture seule, sur le sha vérifié.
reviews = Table(
    "reviews", meta,
    Column("id", Integer, primary_key=True),
    Column("job_id", Integer, ForeignKey("jobs.id"), nullable=False, index=True),
    Column("sha", String(40), nullable=False),
    Column("role", String(20), nullable=False),              # code | securite | archi | infra
    Column("verdict", String(20), nullable=False),           # ok | a_revoir | bloquant | indetermine
    Column("summary", Text, nullable=False, default=""),
    Column("points", JSON, nullable=False, default=list),
    Column("model", String(80), nullable=False, default=""),
    Column("at", Float, nullable=False),
)

# File de commandes panneau -> runners (idempotentes, avec bail) : démarrer, reprendre après une réponse, fusionner.
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
    """Type du système de fichiers qui porte ce chemin (point de montage le plus long qui le contient) ; '' si inconnu."""
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
    """Mode du journal SQLite. WAL (rapide) sur un disque local ; rollback (« delete ») sur un système de fichiers réseau :
    WAL y repose sur une mémoire partagée que NFS ne fournit pas, et les accès s'y bloquent (pages qui tournent sans fin).
    FABRIQUE_SQLITE_JOURNAL=wal|delete force le choix."""
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
        _engine = create_engine(url, future=True, json_serializer=lambda o: json.dumps(o, ensure_ascii=False), **kw)
        if url.startswith("sqlite"):
            mode = journal_mode(url.split("///", 1)[1])
            if mode == "delete":
                logging.getLogger("fabrique.db").warning(
                    "base sur un système de fichiers réseau : journal SQLite en mode rollback (WAL n'y fonctionne pas). "
                    "Pour de meilleures performances, place le volume des données sur un stockage local ou bloc.")

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
    """Applique les migrations Alembic jusqu'à la dernière version."""
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


# Réglages globaux : valeurs par défaut, puis base (modifiable dans le panneau).
DEFAULTS = {
    "agents": {"harness": "claude-code", "model": "", "profile": ""},   # aucun modèle tant que l'installation n'en a pas choisi
    "models": [],
    "profiles": [],                              # profils de modèles : un modèle et un effort par rôle (app/profiles.py)
    "git": [],
    "limits": {"budget_job": 5.0, "budget_day": 30.0, "max_parallel": 2, "size_limit": 300, "watchdog_min": 10,
               "protected_paths": ["**/migrations/**", "**/auth/**", "**/permissions/**", ".forgejo/**", ".gitea/**", ".gitlab-ci.yml",
                                   ".github/**", "Dockerfile", "**/.env*", ".fabrique/**"]},
    "access": {"default_role": "requester"},
    "oidc_secret": {},                           # secret du client OIDC saisi dans les réglages : {"v": valeur chiffrée}
    "demo": {"visible": False},                # données de démonstration affichées ou masquées (FABRIQUE_DEMO=1)   # rôle d'un nouveau compte : requester, ou none (en attente d'un admin)
    "oidc": {"issuer": "", "client_id": "", "scopes": "openid profile email", "groups_claim": "groups",
             "admin_groups": [], "verifier_groups": [], "requester_groups": []},
}


def get_setting(c, key):
    r = one(c, select(settings).where(settings.c.key == key))
    if r is None:
        return DEFAULTS.get(key), None
    return r["value"], r


def set_setting(c, key, value, by):
    t = now()
    if one(c, select(settings.c.key).where(settings.c.key == key)):
        c.execute(update(settings).where(settings.c.key == key).values(value=value, updated_by=by, updated_at=t))
    else:
        c.execute(insert(settings).values(key=key, value=value, updated_by=by, updated_at=t))


def log_event(c, job_id, kind, message):
    c.execute(insert(events).values(job_id=job_id, at=now(), kind=kind, message=message))


def log_audit(c, actor, action, job_id=None, target="", detail=""):
    c.execute(insert(audit).values(at=now(), actor=actor, action=action, job_id=job_id, target=target, detail=detail))
    # Toute action d'audit déclenche aussi un rafraîchissement temps réel.
    c.execute(insert(events).values(job_id=job_id, at=now(), kind="audit", message=action))
