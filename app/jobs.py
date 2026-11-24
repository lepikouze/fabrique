"""Jobs: stages, reads filtered by role, inbox, human actions (request, response, decision, archive).
In phase 1 there was no runner yet: actions are recorded (audit + event) and will be executed by the runner
in phase 2. The demo simulator (demo.py) plays the role of the runner to show the complete cycle."""
from .i18n import M
import re, time
from sqlalchemy import and_, func, insert, or_, select, true, update
from . import config, db, i18n

# (key, label, color). Brand orange is reserved for actions; amber signals "waiting for your response".
STAGES = [
    ("queued", "Queue", "#8a8f98"),
    ("triage", M("Triage"), "#7aa2f7"),
    ("spec", M("Spec to approve"), "#c4a7ff"),
    ("investigating", M("Investigation"), "#a78bfa"),
    ("implementing", M("Implementation"), "#5b8def"),
    ("verifying", M("Checks"), "#2dd4bf"),
    ("review", M("Review"), "#e879c9"),
    ("pr", M("PR · CI"), "#60a5fa"),
    ("waiting", M("Waiting for your answer"), "#f5b83d"),
    ("merged", M("Merged"), "#4ade80"),
    ("done", M("Study delivered"), "#86efac"),
    ("closed", M("Closed without change"), "#8a8f98"),
    ("failed", M("Failed"), "#f87171"),
]
STAGE = {k: {"key": k, "label": l, "color": c} for k, l, c in STAGES}
ORDER = [k for k, _, _ in STAGES]
FLOW = ["triage", "investigating", "implementing", "verifying", "review", "pr", "merged"]   # timeline stages
STUDY_FLOW = ["triage", "investigating", "done"]                                            # ... and those of a study
FINAL = ("merged", "done", "closed", "failed")
ACTIVE = {"triage", "investigating", "implementing", "verifying", "review", "pr"}
HARNESSES = {"claude-code": "Claude Code", "codex": "Codex"}
READY = {"claude-code", "codex"}   # agents that the runner knows how to launch; others are displayed "coming soon"
POLICIES = {"off": (M("Disabled"), M("Every PR waits for your review. Fabrique evaluates nothing.")),
            "trial": (M("Trial"), M("Fabrique evaluates and shows “would have merged”, without merging.")),
            "confident": (M("Merge if confident"), M("Fabrique merges on its own when every gate holds: checks green, review OK, first attempt, no protected path, size respected."))}
INBOX_KIND = {"question": ("Question", "#f5b83d"), "review": (M("Ready for review"), "#60a5fa"),
              "spec": (M("Spec to approve"), "#c4a7ff"), "study": (M("Study delivered"), "#86efac"), "failed": (M("Failed"), "#f87171")}


# Applied when a change request was filed without a "Done when" criterion: universal, with no side effect. The code
# reviewer judges it like any other criterion; it is shown as the default one wherever the criterion appears.
DEFAULT_DONE = M("The change does what the request asks, the repository's existing checks still pass, and nothing unrelated is modified.")


def done_when_of(j, lang=None):
    """(criterion, is it the default one) of a job. The default only applies to change requests: a study has its own outline."""
    if (j.get("done_when") or "").strip():
        return j["done_when"], False
    if j.get("source") in ("study", "onboarding", "demo"):
        return "", False
    return i18n.tr(DEFAULT_DONE, lang) if lang else DEFAULT_DONE, True


_demo = {"at": 0.0, "v": None}


def demo_visible():
    """Display demo data? (setting, only if FABRIQUE_DEMO=1; 2s cache)."""
    if not config.DEMO:
        return False
    if _demo["v"] is None or time.time() - _demo["at"] > 2:
        with db.tx() as c:
            v, _ = db.get_setting(c, "demo")
        _demo.update(at=time.time(), v=bool((v or {}).get("visible")))
    return _demo["v"]


def forget_demo():
    _demo["v"] = None


class Refused(Exception):
    """Action refused (permissions, job status): human-readable message for the user."""


def _visible(user):
    """SQL filter: a requester only sees their requests; demo jobs only if they are displayed."""
    from .auth import can
    conds = [] if can(user, "see_all") else [db.jobs.c.requester == user["user"]]
    if not demo_visible():
        conds.append(db.jobs.c.source != "demo")
    return and_(true(), *conds)


def _repo_names(c):
    return db.cached("repos", lambda: {r["id"]: r for r in db.rows(c, select(db.repos))})


def _runners(c):
    return db.cached("runners", lambda: {r["id"]: r for r in db.rows(c, select(db.runners))})


def listing(user, repo=None, archived=False):
    """Jobs visible, from most recently modified to oldest. In a page request, the list is read only once:
    the page, the footer counters, and the inbox all use it (read-only: no one modifies it)."""
    key = ("jobs", user.get("user"), user.get("role"), repo or None, bool(archived))
    return db.cached(key, lambda: _listing(user, repo, archived))


def _listing(user, repo, archived):
    with db.tx() as c:
        q = select(db.jobs).where(_visible(user), db.jobs.c.archived == archived)
        if repo:
            q = q.where(db.jobs.c.repo_id == repo)
        js = db.rows(c, q.order_by(db.jobs.c.updated_at.desc()))
        repos = _repo_names(c)
        runners = _runners(c)
        ag = {}
        for r in db.rows(c, select(db.agents.c.job_id, func.count().label("n")).where(db.agents.c.state == "running").group_by(db.agents.c.job_id)):
            ag[r["job_id"]] = r["n"]
        lastq = {}
        for r in db.rows(c, select(db.questions.c.job_id, db.questions.c.question).where(db.questions.c.answer.is_(None)).order_by(db.questions.c.id)):
            lastq[r["job_id"]] = r["question"]
    for j in js:
        _decorate(j, repos, runners)
        j["open_question"] = lastq.get(j["id"])
        j["agents_running"] = ag.get(j["id"], 0)
    return js


def _decorate(j, repos, runners):
    j["stage_info"] = STAGE.get(j["stage"], STAGE["queued"])
    j["repo"] = repos.get(j["repo_id"])
    j["runner"] = runners.get(j["runner_id"]) if j.get("runner_id") else None
    j["harness_label"] = HARNESSES.get(j["harness"], j["harness"])
    j["activity_label"] = activity_label(j)


def activity_label(j):
    """What the job does, in one word. "on pause" is reserved for what is suspended: an unanswered question, or the
    daily budget reached."""
    from .work import PAUSED
    st = j["stage"]
    if st in FINAL:
        return M("done", _ctx="job")
    if st == "waiting" or j.get("status_text") == PAUSED:
        return M("paused")
    if j.get("activity") == "active":
        return M("active")
    return M("queued") if st == "queued" else M("waiting")


def by_stage(js):
    groups = {k: [] for k in ORDER}
    for j in js:
        groups.setdefault(j["stage"], []).append(j)
    return [(STAGE[k], groups[k]) for k in ORDER]


# Compact view (personal preference): four families instead of one column per step.
FAMILIES = [("wait", M("Waiting"), "#8a8f98", ("queued",)),
            ("run", M("Running"), "#5b8def", ("triage", "investigating", "implementing", "verifying", "review")),
            ("you", M("Yours"), "#f5b83d", ("spec", "pr", "waiting")),
            ("end", M("Done"), "#4ade80", ("merged", "done", "closed", "failed"))]
FAMILY_OF = {s: k for k, _, _, stages in FAMILIES for s in stages}


def by_family(js):
    groups = {k: [] for k, _, _, _ in FAMILIES}
    for j in js:
        groups[FAMILY_OF.get(j["stage"], "run")].append(j)
    return [({"key": k, "label": l, "color": c}, groups[k]) for k, l, c, _ in FAMILIES]


def get(user, ref):
    """The complete job (question, timeline, agents, evidence, decision, audit, events) or None if not visible."""
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref, _visible(user)))
        if not j:
            return None
        jid = j["id"]
        _decorate(j, _repo_names(c), _runners(c))
        j["questions"] = db.rows(c, select(db.questions).where(db.questions.c.job_id == jid).order_by(db.questions.c.asked_at))
        tl = db.rows(c, select(db.timeline).where(db.timeline.c.job_id == jid).order_by(db.timeline.c.at))
        j["agents"] = db.rows(c, select(db.agents).where(db.agents.c.job_id == jid).order_by(db.agents.c.id))
        j["evidence"] = db.rows(c, select(db.evidence).where(db.evidence.c.job_id == jid).order_by(db.evidence.c.id))
        j["decision"] = db.one(c, select(db.decisions).where(db.decisions.c.job_id == jid))
        j["reviews"] = db.rows(c, select(db.reviews).where(db.reviews.c.job_id == jid).order_by(db.reviews.c.at.desc()))
        j["audit"] = db.rows(c, select(db.audit).where(db.audit.c.job_id == jid).order_by(db.audit.c.at.desc()))
        j["events"] = db.rows(c, select(db.events).where(db.events.c.job_id == jid, db.events.c.kind != "audit")
                              .order_by(db.events.c.id.desc()).limit(200))
        # Diff sent by the runner: only its summary here (the text is read on demand, see diff_view).
        j["patch"] = db.one(c, select(db.diffs.c.sha, db.diffs.c.truncated, db.diffs.c.files, db.diffs.c.at).where(db.diffs.c.job_id == jid))
    j["open_question"] = next((q for q in j["questions"] if q["answer"] is None), None)
    j["timeline"] = _timeline(j, tl)
    from . import translate
    with db.tx() as c:          # reader in another language than the job: stored translations, shown unless the original is asked for
        translate.attach(c, j, show=not i18n.original())
    return j


def patch_info(j):
    """Summary of the diff to display in the job card, or None: it must correspond to the verified commit (an older
    diff is not shown), and a study does not have one."""
    p = j.get("patch")
    if not p or not j.get("verified_sha") or p["sha"] != j["verified_sha"] or j.get("source") == "study" or diff_off(j):
        return None
    files = p["files"] or []
    return {"sha": p["sha"], "truncated": p["truncated"], "files": files, "n": len(files), "add": sum(f.get("add", 0) for f in files),
            "rem": sum(f.get("rem", 0) for f in files)}


def repo_keeps_diff(settings):
    """'Show diff in Fabrique' repo setting: enabled by default. Disabled for a confidential repo, whose
    code must not be copied into the Fabrique database: the runner sends nothing and the dashboard keeps nothing."""
    return (settings or {}).get("diff") is not False


def diff_off(j):
    """True if the job repo does not keep a diff (the card states this and links to the PR)."""
    return not repo_keeps_diff((j.get("repo") or {}).get("settings"))


def report_formats(c=None):
    """Formats in which a study report is downloaded (Settings → Budgets and gates): both by default."""
    def load(cx):
        lim = db.get_setting(cx, "limits")[0] or {}
        return {"md": lim.get("report_md") is not False, "pdf": lim.get("report_pdf") is not False}
    if c is not None:
        return load(c)
    with db.tx() as cx:
        return load(cx)


def diff_view(user, ref, sha=""):
    """Full diff of a job, split by file, with protected files flagged. Same permissions as job reading;
    None if the job is not visible, has no diff, or if the diff no longer matches the verified commit."""
    from . import diffview, work
    with db.tx() as c:
        j = db.one(c, select(db.jobs.c.id, db.jobs.c.repo_id, db.jobs.c.verified_sha, db.jobs.c.source).where(db.jobs.c.ref == ref, _visible(user)))
        if not j or not j["verified_sha"] or j["source"] == "study":
            return None
        row = db.one(c, select(db.diffs).where(db.diffs.c.job_id == j["id"]))
        if not row or row["sha"] != j["verified_sha"] or (sha and sha != row["sha"]):
            return None
        repo = db.one(c, select(db.repos.c.settings).where(db.repos.c.id == j["repo_id"])) or {}
        if not repo_keeps_diff(repo.get("settings")):
            return None
        limits, _ = db.get_setting(c, "limits")
    patterns = ((repo.get("settings") or {}).get("protected_paths") or limits["protected_paths"]) + [".fabrique/**"]
    paths = [f["path"] for f in (row["files"] or [])]
    return diffview.view(row, work.protected_hits(paths, patterns))


def _timeline(j, tl):
    """Past steps (with summary), current, and upcoming (grayed out)."""
    seen = [dict(t, state="done") for t in tl]
    cur = j["stage"]
    flow = STUDY_FLOW if j.get("source") == "study" else FLOW
    if seen and cur not in FINAL:
        seen[-1]["state"] = "current" if seen[-1]["stage"] == cur else "done"
    done_stages = {t["stage"] for t in tl}
    future = []
    if cur not in FINAL:
        idx = flow.index(cur) if cur in flow else 0
        future = [{"stage": s, "state": "todo", "summary": "", "at": None} for s in flow[idx + 1:] if s not in done_stages]
    for t in seen + future:
        t["info"] = STAGE.get(t["stage"], STAGE["queued"])
    return seen + future


# What a human must look at themselves when a review has not returned a verdict.
REVIEW_FOCUS = {"code": M("correctness: does the change do what the request says? edge cases, unhandled errors, tests that really test"),
                "security": M("vulnerabilities: unvalidated input, injection, hard-coded secret, rights that are too broad"),
                "architecture": M("structure: modules respected, needless dependencies or coupling, duplication"),
                "infra": M("deployment: container, CI, migrations, configuration, rollback")}


def changes_url(j):
    """Direct link to the PR diff at the provider (changes tab)."""
    url, prov = j.get("pr_url") or "", ((j.get("repo") or {}).get("provider") or "")
    if not url.startswith(("https://", "http://")):
        return ""
    return url + ("/diffs" if prov == "gitlab" else "/files")


def to_check(j):
    """For each red gate: what to check yourself before approving."""
    from .work import ROLES, VERDICTS
    d = j.get("decision") or {}
    cur = [r for r in j.get("reviews") or [] if r["sha"] == j.get("verified_sha")]
    out = []
    for l in d.get("limits") or []:
        if l["ok"]:
            continue
        n = l.get("key")
        if n == "review":
            for r in sorted(cur, key=lambda r: r["role"]):
                # The reviewers' own texts go apart ("parts"), each one as written: the page shows them one by one, so that a
                # translation asked by the reader replaces them (a joined string would no longer match any translated text).
                if r["verdict"] == "undetermined":
                    out.append({"title": M("No verdict from the {get} review", get=ROLES.get(r['role'], r['role'])),
                                "parts": [r["summary"]] if r["summary"] else [],
                                "text": (M("No verdict given.") if not r["summary"] else "") + M(" Review {get} yourself in the changes, or rerun the review.", get=REVIEW_FOCUS.get(r['role'], ''))})
                elif r["verdict"] != "ok":
                    must = [p for p in r["points"] or [] if re.match(r"^\s*\**\s*must\s+fix", p, re.I)]
                    out.append({"title": M("{get} review: {p}", get=ROLES.get(r['role'], r['role']), p=VERDICTS[r['verdict']]),
                                "parts": must or list(r["points"] or []) or ([r["summary"]] if r["summary"] else []), "text": ""})
            if not cur or not {"code", "security"} <= {r["role"] for r in cur}:
                out.append({"title": M("Review incomplete"), "text": M("Code and security are required on the verified sha: rerun the review.")})
        elif n == "checks":
            if l.get("no_checks"):
                out.append({"title": M("No check"), "text": M("The repo's recipe has no checks yet (blank repo): nothing was verified automatically. Read the changes and their tests; after the merge, onboarding will propose the recipe.")})
            else:
                out.append({"title": M("Checks"), "text": M("Open the Evidence section of the job: the output of each failed check can be unfolded there.")})
        elif n == "first_attempt":
            out.append({"title": M("Several attempts"), "text": M("{detail}: look at what the fix changed, and why.", detail=l['detail'])})
        elif n == "protected":
            out.append({"title": M("Protected files"), "text": l["detail"] + M(": these files need careful human review.")})
        elif n == "tests":
            out.append({"title": M("Existing tests modified"), "text": l["detail"] + M(": check that the change does not weaken or remove a test to make the checks pass.")})
        elif n == "size":
            out.append({"title": M("Large PR"), "text": l["detail"] + M(": beyond the limit, merging on its own is not allowed.")})
        elif n == "opinion":
            out.append({"title": M("The agent is not confident"), "text": M("Read its interpretation and its assumptions in the job.")})
    for e in j.get("evidence") or []:
        if e.get("sha") == j.get("verified_sha") and security_warn(e):
            why = (M("the job tried to reach a site outside the proxy's allowlist: a dependency to allow, or an agent that was tricked.") if e.get("kind") == "network" else M("hard-coded secret, or a package that cannot be found or was only just published."))
            out.append({"title": (e.get("output") or "").split("\n", 1)[0], "text": M("Open the {name} line in the job's Evidence: {why} This check does not block: it is your call.", name=e.get('name'), why=why)})
    return out


EFFORT = {1: "trivial", 2: M("light"), 3: M("medium"), 4: M("substantial"), 5: M("heavy")}


def review_effort(j):
    """Review effort from 1 to 5 (same calculation as the runner for the PR), or None until there is a diff."""
    d = j.get("diff") or {}
    files = d.get("files") or []
    if not files:
        return None
    lines = int(d.get("additions") or 0) + int(d.get("deletions") or 0)
    n = 1 if lines <= 30 and len(files) <= 2 else 2 if lines <= 120 and len(files) <= 5 else 3 if lines <= 300 and len(files) <= 10 else 4 if lines <= 800 else 5
    prot = any(not l["ok"] for l in ((j.get("decision") or {}).get("limits") or []) if l.get("key") == "protected")
    n = min(5, n + (1 if prot else 0))
    return {"n": n, "label": EFFORT[n], "files": len(files), "add": int(d.get("additions") or 0), "rem": int(d.get("deletions") or 0)}


def security_warn(e):
    """A security or network line of the evidence (runner checks, for information) that reports something: alerts, or
    refused network egress. The runner says so itself (kind, warn): nothing is read from the text of the report."""
    return e.get("kind") in ("security", "network") and bool(e.get("warn"))


def inbox(user):
    """What awaits a human. A requester only sees questions about their own requests."""
    from .auth import can
    items = []
    for j in listing(user):
        kind = None
        if j["stage"] == "waiting" and j["open_question"]:
            kind, why = "question", j["open_question"]
        elif j["stage"] == "done" and j.get("report") and not j.get("report_seen"):
            kind, why = "study", M("The study report is delivered: read it, then mark it as read or turn it into a request.")
        elif not can(user, "see_all"):
            continue
        elif j["stage"] == "spec":
            kind, why = "spec", j["status_text"] or M("The spec is waiting for your approval before the investigation.")
        elif j["merge_state"] == "ready":
            kind, why = "review", j["status_text"] or M("Ready for review.")
        elif j["stage"] == "failed":
            kind, why = "failed", j["status_text"] or M("The job failed.")
        if kind:
            items.append({"job": j, "kind": kind, "label": INBOX_KIND[kind][0], "color": INBOX_KIND[kind][1], "why": why})
    order = {"question": 0, "spec": 1, "review": 2, "study": 3, "failed": 4}
    return sorted(items, key=lambda i: (order[i["kind"]], -i["job"]["updated_at"]))


def access_requests():
    """Pending access requests (viewed by admins). Read once per page: the menu counter and the inbox use it."""
    def load():
        with db.tx() as c:
            return db.rows(c, select(db.users).where(db.users.c.requested_role.is_not(None)).order_by(db.users.c.requested_at))
    return db.cached("access_requests", load)


def counters(user):
    from .auth import can
    js = listing(user)
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    today = [j for j in js if j["updated_at"] >= start]
    return {
        "active": sum(1 for j in js if j["stage"] in ACTIVE and j["activity"] == "active"),
        "idle": sum(1 for j in js if j["stage"] in ACTIVE and j["activity"] != "active"),
        "agents": sum(j["agents_running"] for j in js),
        "tokens": sum(j["tokens"] for j in today),
        "spend": sum(j["cost"] for j in today),
        "today": len(today),
        "need": len(inbox(user)) + (len(access_requests()) if can(user, "settings") else 0),
        "total": len(js),
    }


def archived_count(user):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(db.jobs).where(_visible(user), db.jobs.c.archived.is_(True))).scalar()


def is_demo_repo(r):
    return bool((r.get("settings") or {}).get("demo"))


def repos_for_requests():
    with db.tx() as c:
        rs = db.rows(c, select(db.repos).where(db.repos.c.accepts_jobs.is_(True)).order_by(db.repos.c.is_default.desc(), db.repos.c.name))
    return [r for r in rs if demo_visible() or not is_demo_repo(r)]


def _next_ref(c):
    """Next job number. A counter that never decreases (setting "job_seq"): a deleted number is not
    reassigned, because commits ("Fabrique-Job: FAB-14") and the audit refer to it forever."""
    seq = int((db.get_setting(c, "job_seq")[0] or {}).get("n") or 0)
    if not seq:   # first time: we start from the highest number already assigned
        for r in db.rows(c, select(db.jobs.c.ref)):
            m = re.fullmatch(re.escape(config.JOB_PREFIX) + r"-(\d+)", r["ref"] or "")
            seq = max(seq, int(m.group(1))) if m else seq
    n = seq + 1
    while db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == f"{config.JOB_PREFIX}-{n}")):
        n += 1
    db.set_setting(c, "job_seq", {"n": n}, "fabrique")
    return f"{config.JOB_PREFIX}-{n}"


def create(user, title, request, done_when, repo_id, harness="", model="", source="form", kind="start", study=False, profile=""):
    from .auth import can
    title, request, done_when = " ".join(title.split())[:200], request.strip()[:8000], done_when.strip()[:2000]
    if not title or not request:
        raise Refused(M("The title and the need are required."))
    if (harness or model or profile) and not can(user, "see_all"):
        harness = model = profile = ""   # the requester chooses neither the agent, nor the model, nor the profile: repo settings
    if harness and harness not in HARNESSES:
        raise Refused(M("Unknown agent."))
    if harness and harness not in READY:
        raise Refused(M("{p} is not available yet.", p=HARNESSES[harness]))
    if study and source == "form":
        source = "study"   # study: the agent returns a report, the runner pushes nothing
    with db.tx() as c:
        return insert_job(c, user, title, request, done_when, repo_id, harness, model, source, kind, profile)


def insert_job(c, user, title, request, done_when, repo_id, harness="", model="", source="form", kind="start", profile=""):
    """Creation of a job in an already open transaction (form, onboarding, automatic restart)."""
    t = time.time()
    repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id, db.repos.c.accepts_jobs.is_(True)))
    if not repo:
        raise Refused(M("This repo does not accept new requests."))
    if repo["onboarding"] not in ("ready", "blank") and not is_blank(repo) and source not in ("onboarding", "demo"):
        raise Refused(M("This repo is not ready yet: onboarding must be merged first."))
    glob, _ = db.get_setting(c, "agents")
    limits, _ = db.get_setting(c, "limits")
    rs = repo["settings"] or {}
    from . import providers, profiles
    eff_h = harness or rs.get("harness") or glob["harness"]
    if profile and not profiles.get(c, profile):
        raise Refused(M("Unknown model profile."))
    eff_m, extra = profiles.resolve(c, rs, source, model, profile)
    if not eff_m and source == "demo":
        eff_m = "demo-model"               # dummy data: no agent is running, no model is required
    if not eff_m:
        raise Refused(providers.default_problem(c))
    if eff_h not in READY:
        raise Refused(M("The selected agent ({eff_h}, repo or global setting) is not available yet: choose Claude Code in the settings.", eff_h=HARNESSES.get(eff_h, eff_h)))
    known = {m["id"]: m for m in providers.models_with_provider(c)}
    if model and model not in known:
        raise Refused(M("Unknown model."))
    if eff_m in known and not providers.compatible(known[eff_m], eff_h):
        raise Refused(M("{eff_h} cannot use {name}: choose a compatible model.", eff_h=HARNESSES.get(eff_h, eff_h), name=known[eff_m]['name']))
    ref = _next_ref(c)
    jid = c.execute(insert(db.jobs).values(
        ref=ref, title=title, request=request, done_when=done_when, requester=user["user"], source=source, repo_id=repo["id"],
        stage="queued", activity="idle", status_text=M("Waiting for a runner"), branch=f"fabrique/{ref}",
        harness=harness or rs.get("harness") or glob["harness"], model=eff_m, models=extra, lang=i18n.job_lang(c, repo),
        budget=float(rs.get("budget_job") or limits["budget_job"]), created_at=t, updated_at=t)).inserted_primary_key[0]
    c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(last_activity=t))
    if source != "demo":
        from .work import enqueue
        enqueue(c, jid, kind)
    db.log_event(c, jid, M("created"), M("Request filed by {name}", name=user['name']))
    db.log_audit(c, user["user"], M("Request created"), jid, ref, f"repo {repo['slug']}")
    return ref


def _job_for_action(c, user, ref):
    j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref, _visible(user)))
    if not j:
        raise Refused(M("Job not found."))
    return j


def answer(user, ref, text):
    from .auth import can
    text = text.strip()[:4000]
    if not text:
        raise Refused(M("The answer is empty."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if not (can(user, "answer") or (can(user, "answer_own") and j["requester"] == user["user"])):
            raise Refused(M("You cannot answer this question."))
        q = db.one(c, select(db.questions).where(db.questions.c.job_id == j["id"], db.questions.c.answer.is_(None)))
        if not q or j["stage"] != "waiting":
            raise Refused(M("No question is waiting on this job."))
        t = time.time()
        c.execute(update(db.questions).where(db.questions.c.id == q["id"]).values(answer=text, answered_by=user["user"], answered_at=t))
        # The response is data for the agent, never a higher-level instruction: the runner frames it (phase 2).
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="triage", activity="idle", updated_at=t,
                                                                         status_text=M("Answer received · resuming at triage")))
        c.execute(insert(db.timeline).values(job_id=j["id"], stage="triage", at=t, summary=M("Resuming with the answer received.")))
        if j["source"] != "demo":
            from .work import enqueue
            enqueue(c, j["id"], "resume", {"answer": text, "question": q["question"], "by": user["name"]})
        db.log_event(c, j["id"], M("answer"), M("Answer from {name}: the agent resumes at triage", name=user['name']))
        db.log_audit(c, user["user"], M("Question answered"), j["id"], ref, text[:200])


ONBOARD_TEXT = ("Write the repo {slug}'s recipe .fabrique/setup.json: installation commands, checks (tests, lint, typing, build) and required environment variables, proven by running it. Change no other file.",
                M("The recipe is the only change; the runner replays it in a fresh container; a human merges the PR."))


def is_blank(repo):
    """Repo initialized by Fabrique: its main branch already contains a recipe (empty). It accepts requests, and
    keeps doing so whatever becomes of its onboarding, which only tries to write a richer recipe."""
    return bool(repo and (repo.get("settings") or {}).get("blank"))


def remove_repo(user, repo_id):
    """Removes a repo from Fabrique. Nothing is deleted on the Git host. Possible as long as the repo was only used for
    its onboarding: as soon as a request has been submitted, its history counts and the repo is closed instead of disappearing."""
    from sqlalchemy import delete
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused(M("Only admins remove a repo."))
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
        if not repo:
            raise Refused(M("Repo not found."))
        js = db.rows(c, select(db.jobs.c.id, db.jobs.c.source, db.jobs.c.stage).where(db.jobs.c.repo_id == repo_id))
        if any(j["source"] != "onboarding" for j in js):
            raise Refused(M("This repo has already received requests: its history is kept. To stop it, untick “Accepts new requests”."))
        ids = [j["id"] for j in js]
        if ids and db.one(c, select(db.intents.c.id).where(db.intents.c.job_id.in_(ids), db.intents.c.state == "leased")):
            raise Refused(M("This repo's onboarding is running on a runner: wait for it to end, or stop the job, then try again."))
        if ids:
            for t in reversed(db.meta.sorted_tables):
                if "job_id" in t.c:
                    c.execute(delete(t).where(t.c.job_id.in_(ids)))
            c.execute(delete(db.jobs).where(db.jobs.c.id.in_(ids)))
        c.execute(delete(db.repos).where(db.repos.c.id == repo_id))
        if repo["is_default"]:
            other = db.one(c, select(db.repos.c.id).order_by(db.repos.c.name).limit(1))
            if other:
                c.execute(update(db.repos).where(db.repos.c.id == other["id"]).values(is_default=True))
        db.log_audit(c, user["user"], M("Repo removed"), target=repo["slug"], detail=M("nothing is deleted on the Git host"))
    return repo["name"]


def onboard(user, repo_id):
    """Launch (or relaunch) a repo's onboarding: a job where the agent only writes the .fabrique/setup.json recipe."""
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused(M("Only admins start a repo's onboarding."))
    with db.tx() as c:
        return onboard_in(c, user, repo_id)


def onboard_in(c, user, repo_id, kind="onboard"):
    repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
    if repo and (repo["settings"] or {}).get("to_create") and kind == "onboard":
        kind = "create"     # the validated creation did not succeed: we relaunch it, not an onboarding
    if not repo or repo["onboarding"] in ("ready", "analyzing"):
        raise Refused(M("This repo is already ready or being onboarded."))
    # Only one onboarding job per repo: we relaunch the last one instead of stacking new ones.
    last = db.one(c, select(db.jobs).where(db.jobs.c.repo_id == repo_id, db.jobs.c.source == "onboarding", db.jobs.c.archived.is_(False))
                  .order_by(db.jobs.c.id.desc()).limit(1))
    from .work import enqueue
    if last and last["stage"] != "merged":
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == last["id"], db.intents.c.state.in_(["pending", "leased"]))):
            return last["ref"]                                  # an attempt is already queued: nothing to add
        t = time.time()
        # A retry starts with the current agent and model: it is often a corrected setting that motivates it.
        glob, _ = db.get_setting(c, "agents")
        rs = repo["settings"] or {}
        from . import profiles
        model, extra = profiles.resolve(c, rs, last["source"])
        enqueue(c, last["id"], kind)
        c.execute(update(db.jobs).where(db.jobs.c.id == last["id"]).values(
            harness=rs.get("harness") or glob["harness"], model=model or last["model"], models=extra,
            stage="queued", activity="idle", updated_at=t,
            status_text={"init": M("Repo initialization"), "create": M("Creation restarted")}.get(kind, M("Onboarding restarted")) + M(" · waiting for a runner")))
        if kind == "create":
            c.execute(update(db.repos).where(db.repos.c.id == repo_id).values(onboarding="creating"))
        if kind == "onboard" and not is_blank(repo):
            c.execute(update(db.repos).where(db.repos.c.id == repo_id).values(onboarding="pending"))
        db.log_audit(c, user["user"], M("Repo initialization approved") if kind == "init" else M("Onboarding restarted"), last["id"], target=repo["slug"])
        return last["ref"]
    if kind == "init":
        raise Refused(M("No onboarding open for this repo."))
    req, done = ONBOARD_TEXT
    return insert_job(c, user, f"Onboarding of {repo['slug']}", req.format(slug=repo["slug"]), done, repo_id, source="onboarding", kind="onboard")


REPO_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def request_repo(user, provider, name, description=""):
    """Repo creation request (reviewer or admin): nothing is created before admin validation."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused(M("Only reviewers and admins request the creation of a repo."))
    name, description = name.strip().lower(), description.strip()[:300]
    if not REPO_NAME.match(name):
        raise Refused(M("Invalid name: lowercase letters, digits, dots, hyphens (100 characters at most)."))
    from . import forges
    with db.tx() as c:
        g = forges.resolve(c, provider) or {}                   # precise provider (its identifier); a single type is still accepted
        typ = g.get("type") or provider
        if typ not in forges.TYPES:
            raise Refused("Unknown provider.")
        if not g:
            g = next((x for x in forges.listing(c) if x["type"] == typ), {})
        ns = (g.get("creation") or (db.get_setting(c, "creation")[0] or {}).get(typ) or "").strip()
        if not ns:
            raise Refused(M("Repo creation is not enabled for this provider: fill in “Organization for new repos” in Settings → Git providers."))
        slug, gid = f"{ns}/{name}", g.get("id", "")
        if db.one(c, select(db.repos.c.id).where(db.repos.c.slug == slug, db.repos.c.git_id == gid)):
            raise Refused(M("Repo {slug} is already known to Fabrique.", slug=slug))
        rid = c.execute(insert(db.repos).values(provider=typ, git_id=gid, slug=slug, name=name, onboarding="requested", accepts_jobs=False,
                                                settings={"merge_policy": "off", "description": description, "requested_by": user["user"],
                                                          "to_create": True},
                                                created_at=time.time())).inserted_primary_key[0]
        db.log_audit(c, user["user"], M("Repo creation requested"), target=slug, detail=description)
        from . import notify
        notify.emit(c, "repo_request", repo={"id": rid, "slug": slug},
                    detail=M("Requested by {p}", p=user.get('name') or user['user']) + (M(": “{description}”", description=description) if description else "") + M(". To approve in the inbox."))
        return rid


def can_decide_repo(user, repo):
    """An admin always validates; a reviewer validates the request of another (never their own)."""
    from .auth import can
    if can(user, "repos_edit"):
        return True
    return can(user, "decide") and (repo["settings"] or {}).get("requested_by") != user["user"]


def repo_requests():
    with db.tx() as c:   # taken from the list of repos already read for the page, when there is one
        return sorted((r for r in _repo_names(c).values() if r["onboarding"] == "requested"), key=lambda r: r["created_at"])


def decide_repo(user, rid, approve, note=""):
    """Human validation (admin): the runner creates the repo in the dedicated group, initializes it, and it becomes "blank"."""
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid, db.repos.c.onboarding == "requested"))
        if not repo:
            raise Refused(M("This request is no longer waiting for a decision."))
        if not can_decide_repo(user, repo):
            raise Refused(M("The creation must be approved by an admin, or by a reviewer other than the one who requested it."))
        if not approve:
            c.execute(db.repos.delete().where(db.repos.c.id == rid))
            db.log_audit(c, user["user"], M("Repo creation refused"), target=repo["slug"], detail=note[:300])
            return None
        c.execute(update(db.repos).where(db.repos.c.id == rid).values(onboarding="creating", accepts_jobs=True))
        db.log_audit(c, user["user"], M("Repo creation approved"), target=repo["slug"], detail=note[:300])
        rs = repo["settings"] or {}
        return insert_job(c, user, M("Creation of {slug}", slug=repo['slug']),
                          M("Create the repo {slug} (private), then initialize it: README and empty recipe. Description: {p}", slug=repo['slug'], p=rs.get('description') or '—'),
                          M("The repo exists at the provider and the dashboard marks it “blank”."), rid, source="onboarding", kind="create")


def init_repo(user, repo_id):
    """Empty repo: a human validates the initialization (first commit pushed to the main branch by the runner)."""
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused(M("Only admins initialize a repo."))
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
        if not repo or repo["onboarding"] != "empty":
            raise Refused(M("Only an empty repo can be initialized."))
        return onboard_in(c, user, repo_id, kind="init")


def rereview(user, ref):
    """Relaunch the review at the verified sha (reviewer or admin), without relaunching the agent."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused(M("Only reviewers and admins rerun the review."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["merge_state"] != "ready" or not j["verified_sha"] or not j["pr_number"] or j["source"] == "demo":
            raise Refused(M("Nothing to review: the PR is not waiting for a decision."))
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            raise Refused(M("An order is already in progress for this job."))
        from .work import enqueue
        enqueue(c, j["id"], "review", {"sha": j["verified_sha"]})
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="reviewing", updated_at=time.time(),
                                                                         status_text=M("Review rerun · waiting for the runner")))
        db.log_audit(c, user["user"], M("Review rerun"), j["id"], j["verified_sha"] or "")


def decide(user, ref, verdict, note=""):
    """Approve or refuse a merge (reviewer or admin). The merge itself will be performed by the dashboard, pinned to the sha."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused(M("Only reviewers and admins decide on a merge."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["merge_state"] != "ready":
            raise Refused(M("This PR is not waiting for a decision."))
        t = time.time()
        if verdict == "approve":
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="approved", updated_at=t,
                                                                             status_text=M("Merge approved · waiting for the runner")))
            if j["source"] != "demo":
                from .work import enqueue
                enqueue(c, j["id"], "merge", {"sha": j["verified_sha"]})
            db.log_audit(c, user["user"], M("Merge approved"), j["id"], j["verified_sha"] or "", note[:300] or M("Approved in the inbox"))
        elif verdict == "refuse":
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="refused", updated_at=t,
                                                                             status_text=M("Merge refused")))
            if j["source"] != "demo" and j["pr_number"]:
                from .work import enqueue
                enqueue(c, j["id"], "close", {"note": note[:1000]})   # the PR is closed on the Git host, with the reason
            db.log_audit(c, user["user"], M("Merge refused"), j["id"], j["verified_sha"] or "", note[:300])
        elif verdict == "changes":
            # The agent resumes on the same branch with the reviews of the verified commit and the reviewer's note; checks,
            # reviews and the same PR follow. Never on a demo job (no runner behind it).
            if j["source"] in ("demo", "study"):
                raise Refused(M("Changes cannot be requested on this job."))
            rv = db.rows(c, select(db.reviews).where(db.reviews.c.job_id == j["id"], db.reviews.c.sha == (j["verified_sha"] or "")))
            reviews = [{"role": r["role"], "verdict": r["verdict"], "summary": r["summary"] or "", "points": list(r["points"] or [])[:12]}
                       for r in rv if r["verdict"] != "ok"]
            if not reviews and not note.strip():
                raise Refused(M("Say what to change: the reviews ask for nothing, write it in the comment."))
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="", stage="implementing", activity="idle", updated_at=t,
                                                                             status_text=M("Changes requested · waiting for a runner")))
            c.execute(insert(db.timeline).values(job_id=j["id"], stage="implementing", at=t,
                                                 summary=M("Changes requested by {name}.", name=user["name"])))
            from .work import enqueue
            enqueue(c, j["id"], "fix", {"note": note[:1000], "reviews": reviews, "by": user["name"]})
            db.log_audit(c, user["user"], M("Changes requested"), j["id"], j["verified_sha"] or "", note[:300])
        else:
            raise Refused(M("Unknown decision."))


def validate_spec(user, ref, ok, note=""):
    from .auth import can
    if not can(user, "decide"):
        raise Refused(M("Only reviewers and admins approve a spec."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["stage"] != "spec":
            raise Refused(M("This job has no spec to approve."))
        t = time.time()
        if ok:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="investigating", activity="idle", updated_at=t,
                                                                             status_text=M("Spec approved · investigation")))
            c.execute(insert(db.timeline).values(job_id=j["id"], stage="investigating", at=t, summary=M("Spec approved by a human.")))
            db.log_audit(c, user["user"], M("Spec approved"), j["id"], ref, note[:300])
        else:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                             status_text=M("Spec refused") + (f" : {note[:180]}" if note else "")))
            db.log_audit(c, user["user"], M("Spec refused"), j["id"], ref, note[:300])


def cancel(user, ref):
    """Stop an ongoing job. Without a runner on it: immediate stop. With a runner: it kills the agent then confirms."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused(M("Only reviewers and admins stop a job."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["stage"] in FINAL:
            raise Refused(M("This job is already finished."))
        t = time.time()
        leased = db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state == "leased"))
        c.execute(update(db.intents).where(db.intents.c.job_id == j["id"], db.intents.c.state == "pending")
                  .values(state="done", done_at=t, note=M("stop requested")))
        if leased:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(cancel_requested=user["name"], updated_at=t,
                                                                             status_text=M("Stop requested: the runner is stopping the agent")))
            db.log_audit(c, user["user"], M("Stop requested"), j["id"], ref)
            return M("Stop of {ref} requested: the runner is stopping the agent.", ref=ref)
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                         status_text=M("Stopped by {name}", name=user['name'])))
        c.execute(insert(db.timeline).values(job_id=j["id"], stage="failed", at=t, summary=M("Stopped by {name} before the runner did any work.", name=user['name'])))
        db.log_audit(c, user["user"], M("Job stopped"), j["id"], ref)
        return M("{ref} stopped.", ref=ref)


def archive(user, ref, restore=False):
    from .auth import can
    if not can(user, "archive"):
        raise Refused(M("Only reviewers and admins archive."))
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if not restore and j["stage"] not in FINAL and j["merge_state"] != "refused":
            raise Refused(M("Only finished jobs can be archived."))
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(archived=not restore, updated_at=time.time()))
        db.log_audit(c, user["user"], M("Job restored") if restore else M("Job archived"), j["id"], ref)


# ---------- archives: search, permanent deletion, automatic archiving, folder cleanup ----------

ARCHIVE_PAGE = 50
OUTCOMES = {"merged": M("Merged"), "failed": M("Failed"), "refused": M("Refused", _ctx="job"), "done": M("Study delivered"),
            "closed": M("Closed without change")}


def outcome(j):
    """Outcome of a finished job, for the archives: merged, failed, refused or study delivered."""
    return "refused" if j.get("merge_state") == "refused" and j["stage"] != "merged" else j["stage"] if j["stage"] in OUTCOMES else "failed"


def search_archives(user, q="", repo=0, state="", regex=False, page=1):
    """Archived jobs visible by the user, from newest to oldest. q searches in the reference, title, the
    request and the requester: plain text (all words, case-insensitive) or regular expression.
    Returns (page lines, total, pattern error). The pattern is bounded: it applies to short texts, server-side,
    so that a poorly written expression does not block the dashboard."""
    q = (q or "").strip()[:200]
    with db.tx() as c:
        sel = select(db.jobs).where(_visible(user), db.jobs.c.archived.is_(True))
        if repo:
            sel = sel.where(db.jobs.c.repo_id == repo)
        js = db.rows(c, sel.order_by(db.jobs.c.updated_at.desc()))
        repos = _repo_names(c)
        runners = _runners(c)
    for j in js:
        _decorate(j, repos, runners)
        j["outcome"] = outcome(j)
    if state in OUTCOMES:
        js = [j for j in js if j["outcome"] == state]
    err = ""
    if q:
        fields = lambda j: [str(x or "") for x in (j["ref"], j["title"], j["request"][:4000], j["requester"], (j["repo"] or {}).get("slug"))]
        hay = lambda j: " ".join(fields(j))
        if regex:
            try:
                pat = re.compile(q, re.I)   # field by field: an anchored pattern (^FAB-1[0-9]$) targets the reference only
                js = [j for j in js if any(pat.search(f) for f in fields(j))]
            except re.error as e:
                err, js = M("Invalid regular expression: {e}", e=e), []
        else:
            words = q.lower().split()
            js = [j for j in js if all(w in hay(j).lower() for w in words)]
    total = len(js)
    page = max(1, min(int(page or 1), max(1, -(-total // ARCHIVE_PAGE))))
    return js[(page - 1) * ARCHIVE_PAGE: page * ARCHIVE_PAGE], total, err, page


def delete_phrase(j):
    """What to type to confirm job deletion: "repo/FAB-3"."""
    return f"{(j.get('repo') or {}).get('slug') or ''}/{j['ref']}"


def delete_job(user, ref, typed):
    """PERMANENT deletion of an archived job: admin only, confirmed by typing "repo/FAB-3". The job and all its
    content disappear; the audit remains (its lines are detached from the job) and a line states who deleted what. Nothing
    is touched on the Git host. The working directory is cleaned up by the runner on its next pass."""
    from sqlalchemy import delete
    from .auth import can
    if not can(user, "settings"):
        raise Refused(M("Only admins delete a job."))
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        if not j or (j["source"] == "demo"):
            raise Refused(M("Job not found."))
        if not j["archived"]:
            raise Refused(M("Only an archived job can be deleted: archive it first."))
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            raise Refused(M("An order is still in progress on this job: wait for it to end, then try again."))
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"])) or {}
        want = f"{repo.get('slug') or ''}/{j['ref']}"
        if (typed or "").strip() != want:
            raise Refused(M("Wrong confirmation: type exactly {want} to delete this job.", want=want))
        c.execute(update(db.audit).where(db.audit.c.job_id == j["id"]).values(job_id=None))   # the audit outlives the job
        for t in reversed(db.meta.sorted_tables):
            if "job_id" in t.c and t.name != "audit":
                c.execute(delete(t).where(t.c.job_id == j["id"]))
        c.execute(delete(db.jobs).where(db.jobs.c.id == j["id"]))
        db.log_audit(c, user["user"], M("Job deleted"), None, ref, M("{p} · {p2} · requested by {requester}", p=j['title'][:120], p2=repo.get('slug') or '—', requester=j['requester']))
    return ref


def auto_archive(now=None):
    """Archives merged jobs, and studies already read, finished for more than N days (Settings → Budgets and gates;
    0: never). Failures and refused PRs remain visible: they often require follow-up. Returns the number archived."""
    now = now or time.time()
    with db.tx() as c:
        days = int((db.get_setting(c, "limits")[0] or {}).get("auto_archive_days") or 0)
        if days <= 0:
            return 0
        old = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source != "demo",
                                               db.jobs.c.updated_at < now - days * 86400,
                                               or_(db.jobs.c.stage == "merged", and_(db.jobs.c.stage == "done", db.jobs.c.report_seen.is_not(None)))))
        for j in old:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(archived=True))
            db.log_audit(c, "fabrique", M("Job archived"), j["id"], j["ref"], M("automatic archiving after {days} day(s)", days=days))
    return len(old)


def workspaces_to_purge(names, now=None):
    """Among the working directories that a runner says it has (one per job), those it can delete: the job no longer exists
    (deleted), or it has been finished for more than one hour without a pending order. Only the names of THIS installation
    (job prefix) are considered."""
    now = now or time.time()
    pat = re.compile(re.escape(config.JOB_PREFIX) + r"-\d+")
    names = [n for n in dict.fromkeys(str(x) for x in (names or [])) if pat.fullmatch(n)][:500]
    if not names:
        return []
    with db.tx() as c:
        known = {j["ref"]: j for j in db.rows(c, select(db.jobs).where(db.jobs.c.ref.in_(names)))}
        busy = {r["job_id"] for r in db.rows(c, select(db.intents.c.job_id).where(db.intents.c.state.in_(["pending", "leased"])))}
    out = []
    for n in names:
        j = known.get(n)
        if j is None or (j["stage"] in FINAL and j["id"] not in busy and (j["updated_at"] or 0) < now - 3600):
            out.append(n)
    return out


def mark_seen(user, ref):
    """Study read: it leaves the inbox (it remains in the jobs)."""
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["source"] != "study" or not j.get("report"):
            raise Refused(M("This job has no study report."))
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"], db.jobs.c.report_seen.is_(None)).values(report_seen=time.time()))


def activity(limit=300):
    with db.tx() as c:
        js = db.rows(c, select(db.jobs.c.id, db.jobs.c.ref, db.jobs.c.source))
        rs = db.rows(c, select(db.audit).order_by(db.audit.c.at.desc()).limit(limit * 3))
    refs = {r["id"]: r["ref"] for r in js}
    hidden = set() if demo_visible() else {r["id"] for r in js if r["source"] == "demo"}
    out = [r for r in rs if r["job_id"] not in hidden and not (hidden and r["action"] == M("Demo data"))][:limit]
    for r in out:
        r["ref"] = refs.get(r["job_id"])
    return out


def last_event_id():
    with db.tx() as c:
        return c.execute(select(func.max(db.events.c.id))).scalar() or 0
