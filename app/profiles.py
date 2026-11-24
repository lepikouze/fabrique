"""Model profiles: one model per role, reusable from one repo to another (the effort is that of the model provider).

Roles: the agent (triage, investigation, implementation, corrections, in a single session), reviewers (code,
security, architecture, infra reviews), studies, and onboarding. Each role names its model: everything is visible
at a glance. (A profile before this rule, with an empty role, uses the agent model for that role.)
The profile is optional: without a profile, a job starts as before, with the repo model or the global model.

Order for a job's main model: choice on the request > profile (request, repo, then global) > repo model >
global model. What is selected is frozen at job creation (jobs.model and jobs.models)."""
from .i18n import M
import re
from . import db

ROLES = {"agent": ("Agent", M("Triage, investigation, implementation, fixes: one session, one model")),
         "review": (M("Reviewers"), M("Code, security, architecture and infrastructure reviews, with a fresh context")),
         "study": (M("Studies"), M("Study reports: a lot of reading, no change")),
         "onboarding": ("Onboarding", M("Reading a repo and writing its recipe, then replayed by the runner"))}
# Examples displayed under each role: the model most relevant to the requested work (Claude, then local).
EXAMPLES = {"agent": M("Claude Opus 5.5 · local: Qwen3.8 27B. The longest and most demanding work."),
            "review": M("Claude Sonnet 5.5 · local: Gemma 4 26B. Ideally another family than the agent's: a second opinion."),
            "study": M("Claude Sonnet 5.5, or Opus for a hard study · local: Qwen3.8 27B."),
            "onboarding": M("Claude Haiku 5.5 · local: Gemma 4 26B. A simple task, checked by the runner.")}
# Optional model per reviewer: empty = the "Reviewers" model. A stronger model where it counts (code, security), a local one
# elsewhere: a weak reviewer approves everything (FAB-28), and its reviews cost little since each is a single request.
REVIEWERS = {"code": M("Code reviewer"), "security": M("Security reviewer"), "architecture": M("Architecture reviewer"),
             "infra": M("Infrastructure reviewer")}
INHERIT = "provider"   # effort: provider setting (the level defined on the compatible provider)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Invalid(ValueError):
    pass


def efforts():
    from .providers import EFFORTS
    return {INHERIT: M("Provider setting"), **EFFORTS}


def listing(c):
    return [p for p in (db.get_setting(c, "profiles")[0] or []) if isinstance(p, dict) and p.get("id")]


def get(c, pid):
    return next((p for p in listing(c) if p["id"] == pid), None) if pid else None


def usage(c, pid):
    """Where a profile is used: global setting, repos."""
    glob, _ = db.get_setting(c, "agents")
    repos = [r["slug"] for r in db.rows(c, db.repos.select()) if (r["settings"] or {}).get("profile") == pid]
    return {"global": glob.get("profile") == pid, "repos": repos}


def slug(name, taken=()):
    """Internal identifier deduced from the name (never shown): lowercase without accents, hyphens; unique."""
    import unicodedata
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:36] or M("profile")
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def save(c, by, pid, name, roles, new=False):
    """roles : {role: {"model": id}}. A model must be proposed in the Settings. Without pid (new profile),
    the identifier is derived from the name; a renamed profile keeps its own (repos using it keep theirs too).
    Effort is no longer set by profile: it is that of the model provider."""
    from .providers import models_with_provider
    name = " ".join((name or "").split())[:60]
    if not name:
        raise Invalid(M("Give the profile a name."))
    if any(p["name"].lower() == name.lower() and p["id"] != (pid or "") for p in listing(c)):
        raise Invalid(M("A profile is already called “{name}”.", name=name))
    pid = (pid or "").strip().lower() or slug(name, {p["id"] for p in listing(c)})
    if not _ID.match(pid):
        raise Invalid(M("Invalid profile."))
    known = {m["id"] for m in models_with_provider(c)}
    clean = {}
    for role in ROLES:
        r = roles.get(role) or {}
        model = str(r.get("model") or "")
        if model and model not in known:
            raise Invalid(M("{p}: unknown model ({model}).", p=ROLES[role][0], model=model))
        clean[role] = {"model": model, "effort": INHERIT}
    for rv in REVIEWERS:
        model = str((roles.get(f"review_{rv}") or {}).get("model") or "")
        if model and model not in known:
            raise Invalid(M("{p}: unknown model ({model}).", p=REVIEWERS[rv], model=model))
        if model:
            clean[f"review_{rv}"] = {"model": model, "effort": INHERIT}
    missing = [ROLES[r][0] for r in ROLES if not clean[r]["model"]]
    if missing:
        raise Invalid(M("Choose a model for each role (missing: ") + ", ".join(missing) + ").")
    items = listing(c)
    if new and any(p["id"] == pid for p in items):
        raise Invalid(M("Profile “{name}” already exists.", name=name))
    items = [p for p in items if p["id"] != pid] + [{"id": pid, "name": name, "roles": clean}]
    db.set_setting(c, "profiles", sorted(items, key=lambda p: p["name"].lower()), by)
    db.log_audit(c, by, M("Settings: model profile"), detail=f"{name} ({pid})")


def remove(c, by, pid):
    """Removes a profile. Repos that used it, and the global setting, revert to their model."""
    p = get(c, pid)
    if not p:
        raise Invalid(M("Unknown profile."))
    db.set_setting(c, "profiles", [x for x in listing(c) if x["id"] != pid], by)
    glob, _ = db.get_setting(c, "agents")
    if glob.get("profile") == pid:
        db.set_setting(c, "agents", {**glob, "profile": ""}, by)
    for r in db.rows(c, db.repos.select()):
        rs = dict(r["settings"] or {})
        if rs.get("profile") == pid:
            rs.pop("profile")
            c.execute(db.repos.update().where(db.repos.c.id == r["id"]).values(settings=rs))
    db.log_audit(c, by, M("Settings: profile removed"), detail=f"{p['name']} ({pid})")


def parse_choice(v):
    """"Model or profile" list: "m:<model>", "p:<profile>", or empty (inherited setting). Returns (model, profile)."""
    v = str(v or "")
    if v.startswith("p:"):
        return "", v[2:]
    if v.startswith("m:"):
        return v[2:], ""
    return "", ""


def role_of(source):
    return "study" if source == "study" else "onboarding" if source == "onboarding" else "agent"


def resolve(c, repo_settings, source, job_model="", job_profile=""):
    """Models for a new job. Each level (request, repo, global) chooses EITHER a model (only one for all), OR a
    profile (one model per role); the first level that has chosen wins. Returns (main model,
    {profile, effort, review_model, review_effort}). Provider effort: provider setting, nothing to force."""
    glob, _ = db.get_setting(c, "agents")
    rs = repo_settings or {}
    single, prof = "", None
    if job_model or job_profile:
        single, prof = job_model, (get(c, job_profile) if not job_model else None)
    elif get(c, rs.get("profile")):
        prof = get(c, rs.get("profile"))
    elif rs.get("model"):
        single = rs["model"]
    elif get(c, glob.get("profile")):
        prof = get(c, glob.get("profile"))
    else:
        single = glob.get("model") or ""
    if not prof:
        return single, {"profile": "", "effort": INHERIT, "review_model": "", "review_effort": INHERIT}
    roles = prof.get("roles") or {}
    agent, mine = roles.get("agent") or {}, roles.get(role_of(source)) or {}
    model = mine.get("model") or agent.get("model") or rs.get("model") or glob.get("model") or ""
    effort = INHERIT                                     # the effort is that of the model provider (set on the provider)
    rv = roles.get("review") or {}
    review = rv.get("model") or ""
    per = {k: (roles.get(f"review_{k}") or {}).get("model") for k in REVIEWERS}
    per = {k: v for k, v in per.items() if v and v != (review or model)}
    return model, {"profile": prof["name"], "effort": INHERIT, "review_model": review if review != model else "", "review_effort": INHERIT,
                   **({"review_models": per} if per else {})}


def apply_effort(runtime, effort):
    """Effort forced by the profile on the runtime description of a model (compatible servers only)."""
    if runtime.get("type") == "compatible" and effort not in (None, INHERIT):
        runtime = {**runtime, "effort": effort}
    return runtime
