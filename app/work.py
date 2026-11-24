"""Dashboard side of the real work: order queue (intents) for runners, events reported by runners,
merge gates evaluated by code (never by the agent).

A runner authenticates via its token (fingerprint in database), takes an order with a lease, and can only write to the
jobs for which it holds an order. An expired lease (silent runner) puts the order back in the queue; after 3 attempts, the job fails."""
from .i18n import M, Msg
import os, hashlib, re, secrets, time
from sqlalchemy import and_, insert, or_, select, update
from . import config, db, diffview, jobs, notify, i18n

LEASE = 600            # seconds; extended at each runner heartbeat
MAX_ATTEMPTS = 3
AGENT_STAGES = {"triage", "investigating", "implementing"}   # steps the agent can announce itself
import logging
log = logging.getLogger("fabrique.work")


class Denied(Exception):
    pass


# ---------- Runner tokens ----------

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_runner(name, kind, by):
    """Creates a runner and returns its token, displayed only once."""
    name = name.strip()[:60]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,59}", name):
        raise jobs.Refused(M("Invalid runner name (lowercase letters, digits, . _ -)."))
    token = "fab_" + secrets.token_urlsafe(32)
    with db.tx() as c:
        if db.one(c, select(db.runners.c.id).where(db.runners.c.name == name)):
            raise jobs.Refused(M("This runner name already exists."))
        c.execute(insert(db.runners).values(name=name, host="", kind=kind if kind in ("vm", M("container"), "kubernetes") else M("container"),
                                            online=False, capacity=1, harnesses=[], metrics={}, version="", token_hash=_hash(token)))
        db.log_audit(c, by, M("Runner added"), target=name)
    return token


def rotate_runner(rid, by):
    """New token for an existing runner (same name, same settings); the old one stops working immediately."""
    token = "fab_" + secrets.token_urlsafe(32)
    with db.tx() as c:
        r = db.one(c, select(db.runners).where(db.runners.c.id == rid, db.runners.c.revoked.is_(False)))
        if not r or not r["token_hash"]:
            raise jobs.Refused(M("Runner not found."))
        c.execute(update(db.runners).where(db.runners.c.id == rid).values(token_hash=_hash(token), online=False))
        db.log_audit(c, by, M("Runner token renewed"), target=r["name"])
    # Built-in runner (all-in-one): if the shared file contains the old token of THIS runner, it follows; otherwise it
    # would be left with a dead token until human intervention. The runner rereads it on its next startup.
    path = config.BOOTSTRAP_RUNNER_FILE
    try:
        if path and os.path.exists(path) and _hash(open(path).read().strip()) == r["token_hash"]:
            _write_bootstrap(path, token)
    except OSError as e:
        log.error("built-in runner: cannot update %s (%s)", path, e)
    return r["name"], token


def revoke_runner(rid, by):
    with db.tx() as c:
        r = db.one(c, select(db.runners).where(db.runners.c.id == rid))
        if not r:
            raise jobs.Refused(M("Runner not found."))
        c.execute(update(db.runners).where(db.runners.c.id == rid).values(revoked=True, online=False, token_hash=None))
        c.execute(update(db.intents).where(db.intents.c.runner_id == rid, db.intents.c.state == "leased")
                  .values(state="pending", runner_id=None, lease_until=None))
        db.log_audit(c, by, M("Runner revoked"), target=r["name"])


def _write_bootstrap(path, tok):
    """Token file of the built-in runner: readable by the owner (the dashboard) and its group, which the runner
    receives as an additional group (compose: group_add) and which the agent user does not have. Never
    readable by everyone."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    _tighten(path)


def _tighten(path):
    """0640, dashboard group: also applies to a file created by an old version (0644, other group)."""
    try:
        os.chown(path, -1, os.getgid())
    except OSError:
        pass
    os.chmod(path, 0o640)


INTEGRATED = "built-in-runner"


def bootstrap_runner_token():
    """Kubernetes: registers the built-in runner with the token provided by the environment (a Secret also given to the runner).
    Once only: if a built-in runner already exists, nothing changes — a token renewed in the interface is therefore
    never replaced by the old one upon restart (the new token must then be updated in the Secret)."""
    tok = config.BOOTSTRAP_RUNNER_TOKEN
    if not tok:
        return
    if not re.fullmatch(r"fab_[A-Za-z0-9_-]{32,200}", tok):
        log.error("built-in runner: invalid FABRIQUE_BOOTSTRAP_RUNNER_TOKEN (expected: fab_ followed by at least 32 characters)")
        return
    with db.tx() as c:
        if db.one(c, select(db.runners.c.id).where(db.runners.c.name == INTEGRATED)):
            return
        c.execute(insert(db.runners).values(name=INTEGRATED, host="", kind="kubernetes", online=False, capacity=1, harnesses=[],
                                            metrics={}, version="", token_hash=_hash(tok)))
        db.log_audit(c, "fabrique", M("Runner added"), target=INTEGRATED, detail=M("token supplied by the environment"))
    log.info("built-in runner “%s” registered (token supplied by the environment)", INTEGRATED)


def bootstrap_runner():
    """All-in-one Compose: creates the built-in runner once and places its token in the shared file."""
    bootstrap_runner_token()
    path = config.BOOTSTRAP_RUNNER_FILE
    if not path:
        return
    try:
        if os.path.exists(path) and open(path).read().strip():
            _tighten(path)   # file created by an older version (0644): tightened at startup
            return
        with db.tx() as c:
            taken = {r["name"] for r in db.rows(c, select(db.runners.c.name))}
        name = next(n for n in ["built-in-runner"] + [f"built-in-runner-{i}" for i in range(2, 50)] if n not in taken)
        tok = create_runner(name, M("container"), "fabrique")
        _write_bootstrap(path, tok)
        log.info("built-in runner “%s” registered (token left for the runner)", name)
    except OSError as e:
        log.error("built-in runner: cannot write %s (%s)", path, e)


def runner_for(token):
    if not token or not token.startswith("fab_"):
        return None
    with db.tx() as c:
        return db.one(c, select(db.runners).where(db.runners.c.token_hash == _hash(token), db.runners.c.revoked.is_(False)))


# ---------- Order queue ----------

def enqueue(c, job_id, kind, payload=None):
    c.execute(insert(db.intents).values(job_id=job_id, kind=kind, payload=payload or {}, state="pending", created_at=time.time()))


PAUSED = M("Daily budget reached: resuming tomorrow, or as soon as the budget is raised")


def spent_today(c):
    """Daily spend (jobs touched since midnight), for the daily budget."""
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return sum(r["cost"] or 0 for r in db.rows(c, select(db.jobs.c.cost).where(db.jobs.c.updated_at >= start, db.jobs.c.source != "demo")))


def clean_tags(v):
    """Tags: lowercase letters, digits, hyphens; 10 at most."""
    raw = v if isinstance(v, list) else str(v or "").replace("\n", ",").split(",")
    return sorted({t for t in (str(x).strip().lower()[:30] for x in raw) if re.fullmatch(r"[a-z0-9][a-z0-9-]*", t)})[:10]


def runner_fits(runner_tags, repo):
    """A repo that requires tags only goes to a runner that carries them all."""
    need = set((repo["settings"] or {}).get("tags") or [])
    return need <= set(runner_tags or [])


_last_auto_archive = 0.0


def heartbeat(runner, info):
    """Records the runner state; returns the jobs it must stop."""
    t = time.time()
    with db.tx() as c:
        c.execute(update(db.runners).where(db.runners.c.id == runner["id"]).values(
            online=True, last_seen=t, host=str(info.get("host") or "")[:200], version=str(info.get("version") or "")[:40],
            capacity=max(1, min(16, int(info.get("capacity") or 1))), in_flight=max(0, int(info.get("in_flight") or 0)),
            harnesses=[{"name": str(h.get("name"))[:40], "version": str(h.get("version"))[:40]} for h in (info.get("harnesses") or [])[:8]],
            metrics={k: info.get("metrics", {}).get(k) for k in ("cpu", "mem", "disk")},
            tags=clean_tags(info.get("tags"))))
        c.execute(update(db.intents).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased")
                  .values(lease_until=t + LEASE))
        held = select(db.intents.c.job_id).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased")
        stop = [r["ref"] for r in db.rows(c, select(db.jobs.c.ref).where(db.jobs.c.cancel_requested.is_not(None), db.jobs.c.id.in_(held)))]
        # Daily budget exceeded: ongoing agent runs stop (already decided merges proceed).
        limits, _ = db.get_setting(c, "limits")
        over = []
        if float(limits.get("budget_day") or 0) > 0 and spent_today(c) >= float(limits["budget_day"]):
            working = select(db.intents.c.job_id).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased",
                                                        db.intents.c.kind.in_(("start", "resume", "onboard", "fix")))
            over = [r["ref"] for r in db.rows(c, select(db.jobs.c.ref).where(db.jobs.c.id.in_(working)))]
    # Cleanup: the runner occasionally announces its working directories; we tell it which ones are no longer needed (job
    # deleted, or finished for more than an hour). And automatic archiving occurs at most once per hour.
    purge = jobs.workspaces_to_purge(info.get("workspaces")) if info.get("workspaces") else []
    global _last_auto_archive
    if t - _last_auto_archive > 3600:
        _last_auto_archive = t
        try:
            jobs.auto_archive(t)
        except Exception as e:   # must never prevent a heartbeat
            log.warning("automatic archiving failed (%s)", type(e).__name__)
    return {"cancel": stop, "over_budget": over, "purge": purge}


ONBOARD_RETRY = (120, 900)       # new attempt at a pending onboarding: 2 min for the first half hour, then 15 min
ONBOARD_GIVE_UP = 24 * 3600      # beyond that, the onboarding fails (it can always be restarted)


def _retry_onboarding(c, t):
    """Pending onboardings (access, empty repo): requeued automatically, on the same job."""
    q = (select(db.jobs).join(db.repos, db.repos.c.id == db.jobs.c.repo_id)
         .where(db.jobs.c.source == "onboarding", db.repos.c.onboarding.in_(["waiting", "empty"]), db.jobs.c.stage == "queued",
                db.jobs.c.archived.is_(False)))
    for j in db.rows(c, q):
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            continue
        age = t - j["created_at"]
        if age > ONBOARD_GIVE_UP:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                             status_text=M("Onboarding abandoned after waiting 24 h: restart it from the repo")))
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="failed"))
            continue
        if t - j["updated_at"] >= ONBOARD_RETRY[0 if age < 1800 else 1]:
            enqueue(c, j["id"], "onboard")
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(updated_at=t))


def reconcile(c):
    """Runners silent for 2 minutes: offline. Expired leases: order put back in the queue, or job failed after 3 attempts."""
    t = time.time()
    _retry_onboarding(c, t)
    c.execute(update(db.runners).where(db.runners.c.online.is_(True), db.runners.c.last_seen < t - 120).values(online=False))
    for it in db.rows(c, select(db.intents).where(db.intents.c.state == "leased", db.intents.c.lease_until < t)):
        if it["attempts"] >= MAX_ATTEMPTS:
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="failed", done_at=t, note=M("lease expired too often")))
            c.execute(update(db.jobs).where(db.jobs.c.id == it["job_id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                                  status_text=M("Runner lost during the work")))
            db.log_audit(c, "fabrique", M("Failed"), it["job_id"], detail=M("the runner stopped responding (3 attempts)"))
        else:
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="pending", runner_id=None, lease_until=None))
            db.log_event(c, it["job_id"], "runner", M("lease expired: order put back in the queue"))


MODEL_KINDS = ("start", "resume", "onboard", "review", "fix")   # orders that make a model work


def model_caps(c):
    """{model id: simultaneous jobs allowed} for the models that have a limit (Settings → Agents and models)."""
    models, _ = db.get_setting(c, "models")
    return {m["id"]: int(m["max_jobs"]) for m in models or [] if m.get("max_jobs")}


def models_in_use(c):
    """{model id: jobs at work right now}. What counts is the job's AGENT model, for the orders a runner is executing;
    the reviewers of a profile, which may use another model for a short call, are not counted."""
    out = {}
    for r in db.rows(c, select(db.jobs.c.model).select_from(db.intents.join(db.jobs, db.jobs.c.id == db.intents.c.job_id))
                     .where(db.intents.c.state == "leased", db.intents.c.kind.in_(MODEL_KINDS))):
        out[r["model"]] = out.get(r["model"], 0) + 1
    return out


def claim(runner):
    """The oldest waiting order, with everything needed to execute it; None if there is nothing."""
    t = time.time()
    with db.tx() as c:
        reconcile(c)
        limits, _ = db.get_setting(c, "limits")
        q = select(db.intents).where(db.intents.c.state == "pending").order_by(db.intents.c.id)
        if spent_today(c) >= float(limits["budget_day"]):
            # Daily budget reached: only already decided merges pass; the rest waits until tomorrow.
            for r in db.rows(c, select(db.intents.c.job_id).where(db.intents.c.state == "pending", db.intents.c.kind.not_in(("merge", "close")))):
                c.execute(update(db.jobs).where(db.jobs.c.id == r["job_id"], db.jobs.c.status_text != PAUSED)
                          .values(status_text=PAUSED))
            q = q.where(db.intents.c.kind.in_(("merge", "close")))   # neither costs anything
        me = db.one(c, select(db.runners).where(db.runners.c.id == runner["id"]))
        it = None
        caps, busy = model_caps(c), None
        for cand in db.rows(c, q.limit(100)):
            rj = db.one(c, select(db.jobs.c.repo_id, db.jobs.c.model, db.jobs.c.stage, db.jobs.c.status_text).where(db.jobs.c.id == cand["job_id"]))
            rp = db.one(c, select(db.repos).where(db.repos.c.id == rj["repo_id"])) if rj else None
            if not (rp is None or runner_fits((me or {}).get("tags"), rp)):
                continue
            cap = caps.get(rj["model"]) if rj and cand["kind"] in MODEL_KINDS else None
            if cap:
                # "Simultaneous jobs" of the model: this job waits its turn, in order of arrival (a held order stays the
                # oldest for its model), and the runner goes on to the orders of the other models.
                if busy is None:
                    busy = models_in_use(c)
                if busy.get(rj["model"], 0) >= cap:
                    wait = M("Waiting for a free slot on {model} ({n} job(s) at a time)", model=rj["model"], n=cap)
                    if rj["status_text"] != wait and rj["stage"] in ("queued", "waiting", "triage"):
                        c.execute(update(db.jobs).where(db.jobs.c.id == cand["job_id"]).values(status_text=wait))
                    continue
            it = cand
            break
        if not it:
            return None
        n = c.execute(update(db.intents).where(db.intents.c.id == it["id"], db.intents.c.state == "pending").values(
            state="leased", runner_id=runner["id"], lease_until=t + LEASE, attempts=it["attempts"] + 1)).rowcount
        if not n:
            return None
        j = db.one(c, select(db.jobs).where(db.jobs.c.id == it["job_id"]))
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        from . import providers, profiles
        # With the decrypted key: private network dashboard ↔ runner only. Effort and reviewers: those from the profile, frozen
        # at job creation (jobs.models); a job before profiles has only one model.
        mx = j.get("models") or {}
        model = profiles.apply_effort(providers.runtime(c, j["model"]), mx.get("effort"))
        review_model = (profiles.apply_effort(providers.runtime(c, mx["review_model"]), mx.get("review_effort")) if mx.get("review_model")
                        else profiles.apply_effort(model, mx.get("review_effort")) if mx.get("review_effort") not in (None, profiles.INHERIT, mx.get("effort"))
                        else None)
        review_models = {k: providers.runtime(c, v) for k, v in (mx.get("review_models") or {}).items() if isinstance(v, str) and v}
        # Git provider of the repo, if its token is set in the dashboard (otherwise the runner uses its variables).
        from . import forges   # the provider OF THIS repo (never another provider of the same type), if it has a token
        g = forges.for_repo(c, repo)
        g = g if g and g.get("token") else None
        forge = {"type": g["type"], "base_url": g["base_url"], "token": providers.unseal(g["token"])} if g else None
        wlang = j["lang"] if j.get("lang") in i18n.LANGS else i18n.job_lang(c, repo)   # the job's language, fixed when it was filed
        if it["kind"] == "onboard" and not jobs.is_blank(repo):   # a blank repo remains blank (and usable) during its onboarding
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="analyzing"))
        if it["kind"] == "resume" and (it["payload"] or {}).get("pause"):
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(updated_at=t, activity="active",
                                                                             status_text=M("Resumed after the pause by ") + runner["name"]))
        if it["kind"] in ("start", "onboard", "init", "create", "fix"):
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(runner_id=runner["id"], updated_at=t, activity="active",
                                                                             status_text=M("Picked up by ") + runner["name"]))
        db.log_event(c, j["id"], "runner", M("{name} takes the order “{kind}”", name=runner['name'], kind=it['kind']))
    rs = repo["settings"] or {}
    # No criterion given with a change request: the default one, in the language of the job (what the agent, the reviewers
    # and the pull request read).
    done, default_done = jobs.done_when_of(j, j.get("lang") or i18n.written_lang(c, repo))
    return {"intent": {"id": it["id"], "kind": it["kind"], "payload": it["payload"], "attempt": it["attempts"] + 1},
            "job": dict({k: j[k] for k in ("ref", "title", "request", "done_when", "requester", "source", "branch", "harness", "budget",
                                           "session_id", "verified_sha", "pr_number", "pr_url", "attempts", "cost", "tokens")},
                        done_when=str(done), done_when_default=default_done),
            "model": model,
            "review_model": review_model,
            "review_models": review_models,
            "forge": forge,
            "repo": {"provider": repo["provider"], "slug": repo["slug"], "default_branch": repo["default_branch"],
                     "description": str(rs.get("description") or "")[:300],
                     "protected_paths": rs.get("protected_paths") or limits["protected_paths"],
                     "size_limit": rs.get("size_limit") or limits["size_limit"],
                     "diff": jobs.repo_keeps_diff(rs),
                     "lang": wlang},   # False: confidential repo, the runner does not send the diff
            "limits": {"watchdog_min": int(limits.get("watchdog_min") or 10)}}


def _owned(c, runner, ref):
    """The job, if this runner holds an ongoing order on it; otherwise refusal."""
    j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
    if not j:
        raise Denied("unknown job")
    it = db.one(c, select(db.intents).where(db.intents.c.job_id == j["id"], db.intents.c.runner_id == runner["id"],
                                            db.intents.c.state == "leased"))
    if not it:
        raise Denied(M("no order from this runner in progress on this job"))
    return j, it


# ---------- Merge gates (code, not agent) ----------

ROLES = {"code": "code", "security": M("security"), "architecture": "architecture", "infra": M("infrastructure and operations")}
VERDICTS = {"ok": "OK", "needs_changes": M("to review again"), "blocking": M("blocking"), "undetermined": M("undetermined")}

def _glob_re(pat):
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pat.startswith("**", i):
            out += ".*"; i += 2
        elif pat[i] == "*":
            out += "[^/]*"; i += 1
        elif pat[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pat[i]); i += 1
    return re.compile(out + r"\Z", re.DOTALL)   # DOTALL: "**" also covers a filename containing a newline


MAX_FILES, MAX_PATH = 500, 300   # list of files in a PR kept by the dashboard: beyond that, the gate relies on nothing


def protected_hits(files, patterns):
    """Protected files touched. A path that the gate cannot compare faithfully counts as touched, so
    a human can decide: path that is not raw (git quotes, renaming "a => b", braces, backslash),
    path truncated to the kept length, control character (newline, tab...)."""
    res = [_glob_re(p.strip().lstrip("/")) for p in patterns if p.strip()]
    odd = lambda f: (f.startswith('"') or " => " in f or "{" in f or "\\" in f or len(f) >= MAX_PATH
                     or any(ord(ch) < 32 or ord(ch) == 127 for ch in f))
    return sorted({f for f in files if odd(f) or any(r.match(f) for r in res)})


def evaluate(c, j, repo):
    """Gates evaluated on the actual state: evidence at the PR sha, attempts, protected paths, size, agent's opinion."""
    limits, _ = db.get_setting(c, "limits")
    rs = repo["settings"] or {}
    diff = j["diff"] or {}
    files = diff.get("files") or []
    size = int(diff.get("additions") or 0) + int(diff.get("deletions") or 0)
    size_limit = int(rs.get("size_limit") or limits["size_limit"])
    hits = protected_hits(files, (rs.get("protected_paths") or limits["protected_paths"]) + [".fabrique/**"])
    if diff.get("truncated"):   # incomplete list: a protected file may be found in what is missing
        hits = [M("more than {max_files} files: incomplete list", max_files=MAX_FILES)] + hits
    ev = db.rows(c, select(db.evidence).where(db.evidence.c.job_id == j["id"], db.evidence.c.sha == (j["verified_sha"] or ""),
                                              db.evidence.c.ran_by == "runner"))
    ok_checks = bool(ev) and all(e["ok"] for e in ev)
    d = db.one(c, select(db.decisions).where(db.decisions.c.job_id == j["id"]))
    rv = db.rows(c, select(db.reviews).where(db.reviews.c.job_id == j["id"], db.reviews.c.sha == (j["verified_sha"] or "")))
    roles = {r["role"] for r in rv}
    rv_ok = {"code", "security"} <= roles and all(r["verdict"] == "ok" for r in rv)
    rv_detail = ", ".join(f"{ROLES.get(r['role'], r['role'])} : {VERDICTS.get(r['verdict'], r['verdict'])}" for r in rv) or M("no review")
    reviews = [] if j["source"] == "onboarding" else [{"key": "review", "name": M("Review"), "ok": rv_ok, "detail": rv_detail}]
    return [
        {"key": "checks", "name": M("Checks green"), "ok": ok_checks, "no_checks": not ev,
         "detail": M("{sum}/{count} on sha {p}, rerun by the runner", sum=sum(e['ok'] for e in ev), count=len(ev), p=j['verified_sha'] or '—') if ev
                   else M("no check in the recipe (blank repo or no tests): human review")},
        *reviews,
        {"key": "first_attempt", "name": M("First attempt"), "ok": (j["attempts"] or 0) <= 1, "detail": M("no new attempt") if (j["attempts"] or 0) <= 1 else M("{n} attempts", n=j['attempts'])},
        {"key": "protected", "name": M("Protected paths"), "ok": not hits, "detail": M("no protected file touched") if not hits else M("touches {files}", files=", ".join(hits[:4]))},
        {"key": "tests", "name": M("Existing tests"), "ok": not diff.get("tests_changed"),
         "detail": M("none modified") if not diff.get("tests_changed") else M("modifies {files}", files=", ".join((diff.get("tests_changed") or [])[:4]))},
        {"key": "size", "name": M("Size"), "ok": size <= size_limit, "detail": M("{size} lines out of {size_limit}", size=size, size_limit=size_limit)},
        {"key": "opinion", "name": M("Agent's opinion"), "ok": bool(d and d["confident"]), "detail": M("confident") if d and d["confident"] else M("not confident or no opinion")},
    ]


# ---------- Events sent by the runner ----------

def _stage(c, j, stage, summary, status=""):
    t = time.time()
    c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage=stage, updated_at=t, status_text=status[:240] or jobs.STAGE[stage]["label"],
                                                                     activity="active" if stage in jobs.ACTIVE else "idle"))
    c.execute(insert(db.timeline).values(job_id=j["id"], stage=stage, at=t, summary=summary[:2000]))
    db.log_event(c, j["id"], stage, jobs.STAGE[stage]["label"])


def _txt(v, n=2000):
    """A text received from a runner, bounded. A text the runner worded itself (a fact: template and values) is kept as it
    is, so that it is worded again in each reader's language; cutting it would turn it back into a plain sentence."""
    if isinstance(v, Msg) and len(v) <= max(n, 2000):
        return v
    return str(v or "").strip()[:n]


def _cut(v, n):
    return v if isinstance(v, Msg) else v[:n]


def _assumptions(v):
    """The agent's assumptions: always a list of sentences. A string received as is (old runner) stays a single
    assumption instead of being split into letters."""
    if isinstance(v, str):
        v = [v]
    return [t for t in (_txt(a, 400) for a in (v if isinstance(v, list) else [])[:10]) if t]


def onboard_wait_text(repo, reason):
    """What the human must do on the provider side so that onboarding resumes (new automatic attempt)."""
    br = repo["default_branch"]
    if reason == "empty":
        return (M("Nothing to analyse: the repo is empty, or contains only a README or a licence. Initialize it from the repo's page (Fabrique pushes an empty recipe to {br}), or push the project's code yourself; Fabrique tries again on its own.", br=br))
    if reason == "branch":
        return M("Waiting: branch {br} does not exist. Create it, or fix the repo's main branch; automatic retry.", br=br)
    who = reason.split(":", 1)[1] or M("the runner's account")
    how = {"forgejo": M("add “{who}” as a collaborator with Write access", who=who),
           "gitea": M("add “{who}” as a collaborator with Write access", who=who),
           "gitlab": M("give “{who}” the Developer role on the project (or use a group token)", who=who),
           "github": M("add this repo to the runner's fine-grained token (Contents, Pull requests, Issues: read and write)")}.get(repo["provider"], M("give it write access"))
    return M("Waiting for access: the runner's account cannot see this repo. {p}; Fabrique tries again on its own.", p=how[0].upper() + how[1:])


def _recipe(r):
    """Bounded copy of the recipe reported by the runner (display only: the runner always reads the main branch again)."""
    return {**({"image": _txt(r["image"], 20)} if r.get("image") else {}),
            "setup": [_txt(x, 500) for x in (r.get("setup") or [])[:20]],
            "checks": {_txt(k, 20): _txt(v, 500) for k, v in list((r.get("checks") or {}).items())[:8]},
            "env": [{"name": _txt(e.get("name"), 64), "purpose": _txt(e.get("purpose"), 200), "required": bool(e.get("required"))}
                    for e in (r.get("env") or [])[:30] if isinstance(e, dict)]}


def report(runner, ref, ev):
    """Applies an event from the runner. Types: stage, question, paused, study, decision, evidence, pr, usage, log, intent_done, merged, failed."""
    kind = ev.get("type")
    t = time.time()
    result = None
    with db.tx() as c:
        j, it = _owned(c, runner, ref)
        jid = j["id"]
        if kind == "stage":
            st = ev.get("stage")
            if st not in AGENT_STAGES | {"verifying", "review"}:
                raise Denied(M("step refused"))
            _stage(c, j, st, _txt(ev.get("summary")), _txt(ev.get("status"), 240))
        elif kind == "question":
            q = _txt(ev.get("question"), 4000)
            if not q:
                raise Denied("empty question")
            c.execute(insert(db.questions).values(job_id=jid, question=q, asked_at=t))
            _stage(c, j, "waiting", M("Question asked to the requester."), M("Question asked"))
            notify.emit(c, "question", job=j, detail=q)
            if ev.get("session_id"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(session_id=_txt(ev["session_id"], 80)))
        elif kind == "paused":
            # Daily budget reached during agent run: the job does not fail, a resume waits in the queue (claim holds it
            # back as long as the daily budget is exceeded).
            if ev.get("session_id"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(session_id=_txt(ev["session_id"], 80)))
            if not db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == jid, db.intents.c.state == "pending")):
                enqueue(c, jid, "resume", {"pause": "budget"})
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(activity="idle", status_text=PAUSED))
            db.log_event(c, jid, "runner", M("daily budget reached: agent paused, the job will resume where it was"))
            db.log_audit(c, "fabrique", M("Job paused"), jid, detail=M("daily budget reached"))
        elif kind == "model_wait":
            # Model server saturated, loading or unreachable: the runner keeps the workspace and the session, waits and
            # resumes on its own. The job shows it instead of looking stuck.
            n, total, secs = int(ev.get("attempt") or 1), int(ev.get("attempts") or 1), int(ev.get("seconds") or 0)
            if ev.get("session_id"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(session_id=_txt(ev["session_id"], 80)))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(
                activity="idle", updated_at=t, status_text=M("Waiting for the model (attempt {n} of {total}, next try in {secs} s)", n=n, total=total, secs=secs)))
            db.log_event(c, jid, "runner", M("the model server is saturated or unreachable: the job waits {secs} s then resumes where it was (attempt {n} of {total}): {why}",
                                             secs=secs, n=n, total=total, why=_txt(ev.get("why"), 300)))
        elif kind == "model_back":
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(activity="active", updated_at=t, status_text=jobs.STAGE[j["stage"]]["label"]))
            db.log_event(c, jid, "runner", M("the agent resumes after waiting for the model"))
        elif kind == "study":
            if j["source"] != "study":
                raise Denied(M("this job is not a study"))
            text = _txt(ev.get("report"), 40000)
            if not text:
                raise Denied("empty report")
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(report=text))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            _stage(c, j, "done", M("Study report delivered: nothing was pushed, no PR."), M("Study delivered"))
            db.log_audit(c, "fabrique", M("Study delivered"), jid, detail=M("{count} characters", count=len(text)))
            notify.emit(c, "study", job=j, detail=M("The study report is available in the job."))
        elif kind == "decision":
            # Without agent recommendation, Fabrique defaults to "confident"; it is worded for each reader, in their language.
            reco = _txt(ev.get("recommendation"), 240) or (M("The agent recommends merging.") if ev.get("confident") else M("The agent prefers a human review."))
            vals = dict(recommendation=reco, interpretation=_txt(ev.get("interpretation"), 4000),
                        assumptions=_assumptions(ev.get("assumptions")), confident=bool(ev.get("confident")),
                        verdict=_txt(ev.get("verdict"), 1000), notes=_txt(ev.get("notes"), 2000), at=t)
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            if db.one(c, select(db.decisions.c.job_id).where(db.decisions.c.job_id == jid)):
                c.execute(update(db.decisions).where(db.decisions.c.job_id == jid).values(policy=policy, **vals))
            else:
                c.execute(insert(db.decisions).values(job_id=jid, policy=policy, limits=[], **vals))
            db.log_event(c, jid, M("decision"), M("agent's opinion: confident") if vals["confident"] else M("agent's opinion: not confident"))
        elif kind == "evidence":
            sha = _txt(ev.get("sha"), 40)
            checks = ev.get("checks") or []
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or not isinstance(checks, list):
                raise Denied("invalid evidence")
            c.execute(db.evidence.delete().where(db.evidence.c.job_id == jid, db.evidence.c.sha == sha[:12]))
            for ch in checks[:20]:
                c.execute(insert(db.evidence).values(job_id=jid, sha=sha[:12], name=_txt(ch.get("name"), 60), command=_txt(ch.get("command"), 300),
                                                     ok=bool(ch.get("ok")), duration_ms=int(ch.get("duration_ms") or 0), warn=bool(ch.get("warn")),
                                                     kind=ch.get("kind") if ch.get("kind") in ("security", "network") else "check",
                                                     output=_txt(ch.get("output"), 20000), ran_by="runner", at=t))
            ok = sum(1 for ch in checks if ch.get("ok"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(verified_sha=sha[:12], checks_ok=ok, checks_total=len(checks), updated_at=t))
            db.log_event(c, jid, M("evidence"), M("{ok}/{count} checks on sha {p}, rerun by the runner", ok=ok, count=len(checks), p=sha[:12]))
        elif kind == "pr":
            n, url = int(ev.get("number") or 0), _txt(ev.get("url"), 300)
            if n <= 0 or not url.startswith(("https://", "http://")):
                raise Denied("invalid PR")
            raw = ev.get("files") or []
            if not isinstance(raw, list):
                raise Denied("invalid PR")
            # Paths kept as git provides them (neither cleaned nor silently discarded); anything extra is reported.
            tc = ev.get("tests_changed") if isinstance(ev.get("tests_changed"), list) else []
            diff = {"files": [str(f)[:MAX_PATH] for f in raw[:MAX_FILES]], "truncated": len(raw) > MAX_FILES,
                    "additions": int(ev.get("additions") or 0), "deletions": int(ev.get("deletions") or 0),
                    "tests_changed": [str(f)[:MAX_PATH] for f in tc[:50]]}
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(pr_number=n, pr_url=url, diff=diff, updated_at=t,
                                                                         attempts=max(1, int(ev.get("attempts") or 1))))
            j = db.one(c, select(db.jobs).where(db.jobs.c.id == jid))
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            if j["source"] == "onboarding" and isinstance(ev.get("recipe"), dict):
                c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(recipe=_recipe(ev["recipe"])))
            limits = evaluate(c, j, repo)
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            if not db.one(c, select(db.decisions.c.job_id).where(db.decisions.c.job_id == jid)):
                c.execute(insert(db.decisions).values(job_id=jid, policy=policy, at=t, confident=False, limits=[],
                                                      recommendation=M("The agent gave no opinion.")))
            c.execute(update(db.decisions).where(db.decisions.c.job_id == jid).values(limits=limits, policy=policy))
            all_ok = all(l["ok"] for l in limits)
            # Never an automatic merge for a job stopped by a human, nor for an onboarding (the recipe always goes through a human).
            if policy == "confident" and all_ok and not j["cancel_requested"] and j["source"] != "onboarding":
                state, status = "approved", M("Every gate holds: Fabrique merges")
                enqueue(c, jid, "merge", {"sha": j["verified_sha"]})
                db.log_audit(c, "fabrique", M("Merge decided"), jid, j["verified_sha"] or "", M("Merge if confident · every gate holds"))
            elif policy == "trial":
                state, status = "ready", (M("Trial: would have merged · ready for review") if all_ok else M("Trial: would not have merged · ready for review"))
                db.log_audit(c, "fabrique", M("Merge decision"), jid, j["verified_sha"] or "", status)
            else:
                state, status = "ready", M("Ready for review")
                db.log_audit(c, "fabrique", M("Merge decision"), jid, j["verified_sha"] or "", jobs.POLICIES[policy][0])
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(merge_state=state))
            result = {"policy": jobs.POLICIES[policy][0], "status": status, "limits": limits}
            wl = i18n.job_lang(c, repo)
            if wl != i18n.DEFAULT:           # the runner copies this decision into the PR comment: language of the written texts
                result = {"policy": i18n.tr(result["policy"], wl), "status": i18n.tr(status, wl),
                          "limits": [dict(l, name=i18n.tr(l["name"], wl), detail=i18n.tr(l["detail"], wl)) for l in limits]}
            if state == "ready":
                ok = sum(1 for l in limits if l["ok"])
                notify.emit(c, "ready", job=dict(j, pr_url=url), detail=M("{status} · gates {ok}/{count}", status=status, ok=ok, count=len(limits))
                            + "".join(f"\n✗ {l['name']} : {l['detail']}" for l in limits if not l["ok"]))
            _stage(c, j, "pr", _txt(ev.get("summary"), 300) or M("PR #{n} opened, {files} file(s), +{add} −{rem}.", n=n, files=len(diff['files']), add=diff['additions'], rem=diff['deletions']),
                   status)
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(activity="idle"))
        elif kind == "diff":
            # Verified commit diff, for reading in the dashboard. Text, kept as is and capped: it is used by
            # no gate (the gates rely on the file list of the "pr" event).
            sha = _txt(ev.get("sha"), 40)
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or not isinstance(ev.get("text"), str):
                raise Denied("invalid diff")
            rs = db.one(c, select(db.repos.c.settings).where(db.repos.c.id == j["repo_id"])) or {}
            # Repo whose code must not be copied here (repo setting): nothing is kept, even if a runner sends it
            # (setting changed during the job, runner modified). Accepted without error: the job continues.
            if jobs.repo_keeps_diff(rs.get("settings")):
                text, cut = diffview.clip(ev["text"])
                vals = dict(sha=sha[:12], text=text, truncated=bool(cut or ev.get("truncated")), files=diffview.summary(text), at=t)
                if db.one(c, select(db.diffs.c.job_id).where(db.diffs.c.job_id == jid)):
                    c.execute(update(db.diffs).where(db.diffs.c.job_id == jid).values(**vals))
                else:
                    c.execute(insert(db.diffs).values(job_id=jid, **vals))
        elif kind == "usage":
            cost, tok = float(ev.get("cost") or 0), int(ev.get("tokens") or 0)
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(cost=max(0.0, cost), tokens=max(0, tok), updated_at=t,
                                                                         session_id=_txt(ev.get("session_id"), 80) or j["session_id"]))
            for a in (ev.get("agents") or [])[:30]:
                name = _txt(a.get("name"), 80) or M("main session")
                row = db.one(c, select(db.agents.c.id).where(db.agents.c.job_id == jid, db.agents.c.name == name))
                vals = dict(task=_txt(a.get("task"), 240), model=_txt(a.get("model"), 80), tokens=int(a.get("tokens") or 0),
                            cost=float(a.get("cost") or 0), state="running" if a.get("running") else "done")
                if row:
                    c.execute(update(db.agents).where(db.agents.c.id == row["id"]).values(**vals))
                else:
                    c.execute(insert(db.agents).values(job_id=jid, name=name, **vals))
        elif kind == "review":
            role, verdict = _txt(ev.get("role"), 20), _txt(ev.get("verdict"), 20)
            sha = _txt(ev.get("sha"), 40)[:12]
            if role not in ROLES or verdict not in VERDICTS or not sha:
                raise Denied(M("invalid review"))
            c.execute(db.reviews.delete().where(db.reviews.c.job_id == jid, db.reviews.c.sha == sha, db.reviews.c.role == role))
            c.execute(insert(db.reviews).values(job_id=jid, sha=sha, role=role, verdict=verdict, summary=_txt(ev.get("summary"), 2000),
                                                points=[_txt(p, 400) for p in (ev.get("points") or [])[:12]], model=_txt(ev.get("model"), 80), at=t))
            db.log_event(c, jid, M("review"), M("{role}: {verdict}", role=ROLES[role], verdict=VERDICTS[verdict]))
        elif kind == "cancelled":
            who = j["cancel_requested"] or M("a reviewer")
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(cancel_requested=None))
            _stage(c, j, "failed", M("Stopped at the request of {who}; the runner killed the agent.", who=who), M("Stopped by {who}", who=who))
            db.log_audit(c, "fabrique", M("Job stopped"), jid, detail=M("requested by {who}, confirmed by the runner", who=who))
        elif kind == "log":
            db.log_event(c, jid, _txt(ev.get("kind"), 30) or "agent", _txt(ev.get("message"), 1000))
        elif kind == "onboard_wait":
            reason = _txt(ev.get("reason"), 120)
            if j["source"] != "onboarding" or not (reason in ("empty", "branch") or reason.startswith("access:")):
                raise Denied("invalid wait")
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            text = onboard_wait_text(repo, reason)
            if repo["onboarding"] not in ("waiting", "empty"):
                notify.emit(c, "repo_waiting", repo=repo, detail=text)
            c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(onboarding="empty" if reason == "empty" else "waiting"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(stage="queued", activity="idle", status_text=text, updated_at=t))
            db.log_event(c, jid, "onboarding", text)
        elif kind == "initialized":
            if j["source"] != "onboarding":
                raise Denied("invalid initialization")
            r0 = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            rs0 = {k: v for k, v in (r0["settings"] or {}).items() if k != "to_create"}
            rs0["blank"] = True      # the main branch contains a recipe (empty): the repo is usable immediately
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="blank", settings=rs0,
                                                                                    recipe={"setup": [], "checks": {}, "env": []}))
            _stage(c, j, "merged", M("Repo initialized (empty recipe, and a README if there was none). Blank repo: ready for a first request; onboarding will be restarted after the first merge."), M("Repo initialized"))
            db.log_audit(c, "fabrique", M("Repo initialized"), jid, detail=_txt(ev.get("sha"), 40)[:12])
        elif kind == "onboarded":
            if j["source"] != "onboarding" or not isinstance(ev.get("recipe"), dict):
                raise Denied("invalid onboarding")
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="ready", recipe=_recipe(ev["recipe"])))
            _stage(c, j, "merged", M("Recipe already present on the main branch: repo ready, nothing to merge."), M("Repo ready"))
            db.log_audit(c, "fabrique", M("Onboarding finished"), jid, detail=M("existing recipe reused"))
        elif kind == "merged":
            sha = _txt(ev.get("sha"), 40)
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            if j["source"] == "onboarding":
                rs1 = {k: v for k, v in (repo["settings"] or {}).items() if k != "blank"}
                vals = {"onboarding": "ready", "settings": rs1}
                c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(**vals))
                db.log_audit(c, "fabrique", M("Onboarding finished"), jid, detail=M("recipe merged: the repo accepts jobs"))
            elif repo["onboarding"] == "blank" and not db.one(c, select(db.jobs.c.id).where(
                    db.jobs.c.repo_id == repo["id"], db.jobs.c.source == "onboarding", db.jobs.c.stage == "pr", db.jobs.c.archived.is_(False))):
                # (a recipe PR is already open waiting for a human: we do not recreate it at every merge)
                # First merge in a blank repo: the project exists, the onboarding agent can write its real recipe.
                ref_onb = jobs.onboard_in(c, {"user": "fabrique", "name": "Fabrique"}, repo["id"])
                db.log_event(c, jid, "onboarding", M("first merge in a blank repo: onboarding started ({ref_onb})", ref_onb=ref_onb))
                onb = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref_onb))
                # Explains in the onboarding job itself why it starts without anyone having requested it. Only once:
                # two PRs merged one after another restart the same onboarding, without repeating the explanation.
                if onb and not db.one(c, select(db.events.c.id).where(db.events.c.job_id == onb["id"], db.events.c.kind == "onboarding",
                                                                       db.events.c.message.like('%"m": "started by Fabrique after %'))):
                    db.log_event(c, onb["id"], "onboarding", M("started by Fabrique after {ref}, the first merge of this blank repo: the project now exists, the agent writes its real recipe (installation and checks). PR to review and merge.", ref=j['ref']))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(merge_state="merged"))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            _stage(c, j, "merged", M("Merged into {p}, pinned to the verified sha {verified_sha}.", p=ev.get('branch') or 'main', verified_sha=j['verified_sha']),
                   M("Merged by Fabrique"))
            notify.emit(c, "merged", job=j, detail=M("Merged into {p} (verified sha {verified_sha}).", p=ev.get('branch') or 'main', verified_sha=j['verified_sha']))
            db.log_audit(c, "fabrique", M("Merged"), jid, j["verified_sha"] or "", f"squash → {sha[:12]}")
        elif kind == "failed":
            reason = _txt(ev.get("reason"), 400) or M("failed")
            blank = False
            if j["source"] == "onboarding":
                r1 = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
                blank = jobs.is_blank(r1)
                if blank:    # repo already initialized: it stays blank and usable, the onboarding will be retried after a merge
                    c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="blank"))
                    reason = (reason[:300] + M(" — no effect on the repo, which stays usable; onboarding will be retried after the next merge."))
                else:
                    c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"], db.repos.c.onboarding != "ready").values(onboarding="failed"))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            if ev.get("attempts"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(attempts=max(1, min(20, int(ev["attempts"])))))
            _stage(c, j, "failed", reason, M("Failed: ") + _cut(reason, 200))
            db.log_audit(c, "fabrique", M("Failed"), jid, detail=reason)
            notify.emit(c, "failed", job=j, detail=reason)
        elif kind == "closed":
            # The agent gave its opinion and changed nothing (request already covered, dropped at the requester's request…):
            # an explicit end, not a failure. Nothing to review, nothing to merge.
            reason = _txt(ev.get("reason"), 400)
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            _stage(c, j, "closed", M("The agent changed nothing and said why: {reason}", reason=reason) if reason else M("The agent changed nothing."),
                   M("Closed without change"))
            db.log_audit(c, "fabrique", M("Job closed without change"), jid, detail=reason)
        elif kind == "intent_done":
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="done", done_at=t, note=_txt(ev.get("note"), 400)))
            if ev.get("attempts") is not None:
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(attempts=int(ev["attempts"])))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid, db.jobs.c.stage.notin_(list(jobs.ACTIVE))).values(activity="idle"))
        else:
            raise Denied("unknown event type")
    return result
