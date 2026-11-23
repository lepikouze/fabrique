"""Data and demo simulator (FABRIQUE_DEMO=1). The simulator plays the role of the phase 2 runner: it moves
jobs forward from stage to stage, writes timeline, events, agents, evidence and decisions, to show the dashboard
in motion. Everything it produces is fictitious; a banner indicates this in the interface."""
from .i18n import M
import hashlib, logging, random, threading, time
from sqlalchemy import func, insert, select, update
from . import config, db, diffview, jobs

log = logging.getLogger("fabrique.demo")
RNG = random.Random(7)

POOL = [
    (M("Export the links as CSV"), M("An “Export” button on the list downloads all links (title, address, tags, date) as CSV."),
     M("The file opens in a spreadsheet with correct accents, and a test covers the export.")),
    (M("Full-text search"), M("Search the title, address and note of each link, without reloading the page."),
     M("Typing three letters filters the list; a test covers the search.")),
    (M("Tags on links"), M("Put several tags on a link and filter the list by tag."),
     M("Adding, removing and filtering work, with tests.")),
    (M("Custom 404 page"), M("A plain error page with a link to the home page."), M("The page is shown for any unknown address.")),
    (M("Check dead links every night"), M("A scheduled task tests each link and marks those that no longer answer."),
     M("Dead links carry a badge; the task is tested without network.")),
    (M("Import an HTML bookmarks file"), M("Import the bookmark export of Firefox or Chrome."), M("Folders become tags.")),
    (M("Keyboard shortcuts"), M("J/K to navigate, / to search, N for a new link."), M("The shortcuts are listed in the help.")),
    (M("Click counter per link"), M("Count how often each link is opened and sort by popularity."), M("The “popular” sort exists.")),
    (M("Dark mode"), M("Follow the system setting, with a button to force light or dark."), M("The choice is remembered.")),
    (M("Share a list by link"), M("Generate a read-only link to a tag."), M("The link expires after 7 days.")),
]
QUESTIONS = [
    M("Should the export include archived links? (A) yes, with an “archived” column; (B) no, active links only."),
    M("For the search: (A) filtering in the browser, simple and instant; (B) server-side search, which will hold up better beyond 10,000 links. Which one?"),
    M("Should tags be case-sensitive (“Urgent” and “urgent” distinct)?"),
]
CHECKS = [("Tests", "pytest -q", "48 passed in 6.21s"), ("Lint", "ruff check .", "All checks passed!"),
          (M("Typing"), "mypy app", "Success: no issues found in 23 source files"), ("Build", "docker build -t trial .", "naming to docker.io/library/trial done")]


def _sha(s):
    return hashlib.sha1(s.encode()).hexdigest()[:7]


DEMO_DIFF = """diff --git a/notebook/export.py b/notebook/export.py
new file mode 100644
index 0000000..3b1f2aa
--- /dev/null
+++ b/notebook/export.py
@@ -0,0 +1,14 @@
+# @TITLE@ (job @REF@, made-up demo data).
+import csv, io
+
+COLUMNS = ("title", "address", "tags", "date")
+
+
+def to_csv(links):
+    # The links as CSV: opens in a spreadsheet with the accents right.
+    out = io.StringIO()
+    w = csv.writer(out, delimiter=";")
+    w.writerow(COLUMNS)
+    for l in links:
+        w.writerow([l.title, l.address, ",".join(l.tags), l.date.isoformat()])
+    return out.getvalue()
diff --git a/notebook/views.py b/notebook/views.py
index 7c2d9e1..a90b4f3 100644
--- a/notebook/views.py
+++ b/notebook/views.py
@@ -1,6 +1,7 @@
 from fastapi import APIRouter, Response

 from . import store
+from .export import to_csv

 router = APIRouter()

@@ -18,3 +19,9 @@ def listing(tag: str = ""):
     links = store.every(tag=tag or None)
-    return {"links": links}
+    return {"links": links, "total": len(links)}
+
+
+@router.get("/links.csv")
+def export():
+    return Response(to_csv(store.every()), media_type="text/csv; charset=utf-8",
+                    headers={"Content-Disposition": "attachment; filename=links.csv"})
diff --git a/tests/test_export.py b/tests/test_export.py
new file mode 100644
index 0000000..5d0c1be
--- /dev/null
+++ b/tests/test_export.py
@@ -0,0 +1,11 @@
+from datetime import date
+from types import SimpleNamespace as Link
+
+from notebook.export import to_csv
+
+
+def test_accents_and_tags_are_kept():
+    links = [Link(title="Team coffee", address="https://example.org", tags=["watch", "to read"], date=date(2026, 11, 3))]
+    lines = to_csv(links).splitlines()
+    assert lines[0].endswith("title;address;tags;date")
+    assert lines[1] == "Team coffee;https://example.org;watch,to read;2026-11-03"
"""


def _demo_diff(c, j, sha, t):
    """Fictitious diff of a demo job: same form as a real runner's (text, summary, job counters)."""
    text = DEMO_DIFF.replace("@TITLE@", j["title"]).replace("@REF@", j["ref"])
    files = diffview.summary(text)
    c.execute(db.diffs.delete().where(db.diffs.c.job_id == j["id"]))
    c.execute(insert(db.diffs).values(job_id=j["id"], sha=sha, text=text, truncated=False, files=files, at=t))
    return {"files": [f["path"] for f in files], "truncated": False, "additions": sum(f["add"] for f in files),
            "deletions": sum(f["rem"] for f in files)}


def backfill_diffs():
    """Demo database created before 0.7.5: fictitious jobs that already have a PR receive their fictitious diff."""
    t = time.time()
    with db.tx() as c:
        have = {r["job_id"] for r in db.rows(c, select(db.diffs.c.job_id))}
        for j in db.rows(c, select(db.jobs).where(db.jobs.c.source == "demo", db.jobs.c.pr_number.is_not(None), db.jobs.c.verified_sha.is_not(None))):
            if j["id"] not in have:
                c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(diff=_demo_diff(c, j, j["verified_sha"], t)))


def seed():
    """Fills an empty database with repos, two runners and a dozen jobs at all stages."""
    with db.tx() as c:
        if c.execute(select(func.count()).select_from(db.repos)).scalar():
            seeded = True
        else:
            seeded = False
    if seeded:
        backfill_diffs()
        return False
    with db.tx() as c:
        t = time.time()
        base = {"merge_policy": "confident", "size_limit": 300}
        r1 = c.execute(insert(db.repos).values(provider="forgejo", slug="team/link-notebook", name=M("Link notebook (trial)"), is_default=True,
                       onboarding="ready", settings=dict(base, demo=True), created_at=t - 86400 * 3, last_activity=t,
                       recipe={"setup": ["pip install -r requirements.txt"], "checks": {k.lower(): cmd for k, cmd, _ in CHECKS}})).inserted_primary_key[0]
        r2 = c.execute(insert(db.repos).values(provider="forgejo", slug="team/watch", name=M("Watch"), onboarding="ready",
                       settings={"merge_policy": "trial", "demo": True}, created_at=t - 86400 * 2, last_activity=t - 7200,
                       recipe={"setup": ["pip install -r app/requirements.txt"], "checks": {"tests": "pytest -q tests"}})).inserted_primary_key[0]
        c.execute(insert(db.repos).values(provider="gitlab", slug="demo/internal-portal", name=M("Internal portal (GitLab)"), onboarding="pending",
                  accepts_jobs=False, settings={"merge_policy": "off", "demo": True}, created_at=t - 3600))
        run1 = c.execute(insert(db.runners).values(name="runner-1", host="runner-1.example", kind="vm", online=True, last_seen=t, capacity=3,
                         harnesses=[{"name": "Claude Code", "version": "2.1.301"}],
                         metrics={"cpu": 18, "mem": [5.1, 16], "disk": [61, 120]}, version=M("batch1-demo"))).inserted_primary_key[0]
        c.execute(insert(db.runners).values(name="runner-container", host="server-2", kind=M("container"), online=False, last_seen=t - 3 * 3600,
                  capacity=2, harnesses=[{"name": "Codex", "version": "0.164.0"}], metrics={}, version="lot4"))
        db.log_audit(c, "fabrique", M("Demo data"), detail=M("made-up repos, runners and jobs"))
    plan = [("merged", r1), ("merged", r1), ("pr", r1), ("pr", r2), ("waiting", r1), ("review", r1), ("verifying", r1),
            ("implementing", r1), ("investigating", r1), ("spec", r1), ("triage", r2), ("queued", r1), ("failed", r1)]
    for i, (stage, repo) in enumerate(plan):
        title, req, done = POOL[i % len(POOL)]
        ref = jobs.create({"user": "camille", "name": "Camille", "role": "admin"}, title, req, done, repo, source="demo")
        _drive_to(ref, stage, run1, i)
    return True


def _drive_to(ref, target, runner_id, i):
    """Moves a job through all stages until "target" (for the seed)."""
    path = {"queued": [], "triage": ["triage"], "spec": ["triage", "spec"], "investigating": ["triage", "investigating"],
            "implementing": ["triage", "investigating", "implementing"], "verifying": ["triage", "investigating", "implementing", "verifying"],
            "review": ["triage", "investigating", "implementing", "verifying", "review"],
            "pr": ["triage", "investigating", "implementing", "verifying", "review", "pr"],
            "waiting": ["triage", "waiting"], "merged": ["triage", "investigating", "implementing", "verifying", "review", "pr", "merged"],
            "failed": ["triage", "investigating", "implementing", "failed"]}[target]
    for s in path:
        step(ref, s, runner_id, seed_index=i)


def _summary(stage, j):
    return {
        "triage": M("The request is clear: “{title}”. Done criterion taken as is; low risk, no spec needed.", title=j['title']),
        "spec": M("The change touches the data model: the spec proposes a migration and waits for your approval."),
        "investigating": M("Reading the code: routes are in app/main.py, models in app/db.py; two existing tests to extend."),
        "implementing": M("Implementation on the branch, 3 files changed, tests added."),
        "verifying": M("Repo recipe run on the head commit by the runner."),
        "review": M("Parallel review: code, security, architecture. No blocking point."),
        "pr": M("PR opened, CI green. Merge decision written."),
        "merged": M("Merged into main, pinned to the verified sha."),
        "failed": M("Blocked: the LINKS_DB_URL variable is missing from the repo's recipe."),
        "waiting": M("The request leaves a choice open: question asked."),
    }[stage]


def step(ref, stage, runner_id=None, seed_index=None):
    """Moves the job to the given stage by writing what a real runner would write."""
    t = time.time()
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        jid = j["id"]
        cost = round(j["cost"] + RNG.uniform(0.05, 0.6), 2)
        tokens = j["tokens"] + RNG.randint(40_000, 400_000)
        vals = dict(stage=stage, updated_at=t, cost=cost, tokens=tokens, activity="active" if stage in jobs.ACTIVE else "idle")
        if runner_id and not j["runner_id"]:
            vals["runner_id"] = runner_id
        status = {"triage": M("Triage in progress"), "spec": M("Spec to approve"), "investigating": M("Investigation"), "implementing": M("Writing the code"),
                  "verifying": M("Checks running"), "review": M("Review in progress"), "pr": M("Waiting for the checks"),
                  "waiting": M("Question asked"), "merged": M("Merged by Fabrique"), "failed": M("Blocked: missing variable")}[stage]
        if stage == "triage" and not db.one(c, select(db.agents.c.id).where(db.agents.c.job_id == jid)):
            c.execute(insert(db.agents).values(job_id=jid, name=M("main session"), task=j["title"], model=j["model"],
                                               tokens=tokens // 2, cost=cost * 0.8))
            db.log_event(c, jid, "session", M("started in /work/{ref} ({branch})", ref=ref, branch=j['branch']))
        if stage == "investigating":
            c.execute(insert(db.agents).values(job_id=jid, parent_id=None, name="Explore", task=M("Locate the relevant code"),
                                               model="claude-haiku-5-5", tokens=RNG.randint(200_000, 900_000), cost=0.04, state="done"))
            db.log_event(c, jid, "agent", M("Explore sub-agent started, then finished"))
        if stage == "waiting":
            q = QUESTIONS[(seed_index or 0) % len(QUESTIONS)]
            c.execute(insert(db.questions).values(job_id=jid, question=q, asked_at=t))
            db.log_event(c, jid, "question", M("question asked to the requester"))
        if stage == "verifying":
            sha = _sha(ref + str(t))
            for name, cmd, out in CHECKS:
                c.execute(insert(db.evidence).values(job_id=jid, sha=sha, name=name, command=cmd, ok=True,
                                                     duration_ms=RNG.randint(400, 21000), output=f"$ {cmd}\n{out}", ran_by="runner", at=t))
            vals.update(verified_sha=sha, checks_ok=len(CHECKS), checks_total=len(CHECKS))
            db.log_event(c, jid, M("evidence"), M("{count}/{count2} checks on sha {sha}, rerun by the runner", count=len(CHECKS), count2=len(CHECKS), sha=sha))
        if stage == "pr":
            n = 10 + jid
            vals.update(pr_number=n, pr_url=f"https://git.example.org/{repo['slug']}/pulls/{n}")
            if j["verified_sha"]:
                vals["diff"] = _demo_diff(c, j, j["verified_sha"], t)
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            size = RNG.randint(40, 260)
            limits = [{"name": M("Checks green"), "ok": True, "detail": M("{p}/{p2} on sha {p3}", p=j['checks_ok'] or 4, p2=j['checks_total'] or 4, p3=j['verified_sha'] or '—')},
                      {"name": M("Review"), "ok": True, "detail": M("code, security, architecture: OK")},
                      {"name": M("First attempt"), "ok": True, "detail": M("no new attempt")},
                      {"name": "Protected paths", "ok": True, "detail": M("no protected file touched")},
                      {"name": M("Size"), "ok": size <= 300, "detail": M("{size} lines out of 300", size=size)}]
            confident = (seed_index or jid) % 3 != 1
            c.execute(insert(db.decisions).values(
                job_id=jid, policy=policy, at=t, confident=confident, limits=limits,
                recommendation=M("The agent recommends merging.") if confident else M("The agent prefers a human review."),
                interpretation=M("The request “{title}” is understood as: {request}", title=j['title'], request=j['request']),
                assumptions=[M("No change to the database format."), M("Interface texts stay in French.")],
                verdict=M("Confident: the done criterion is covered by passing tests.") if confident else
                        M("Not confident: the behaviour on mobile could not be verified."),
                notes=M("Headless browser unavailable on the runner: rendering covered by template tests.")))
            auto = policy == "confident" and confident and all(l["ok"] for l in limits)
            vals["merge_state"] = "waiting_checks" if auto else "ready"
            if not auto:
                status = M("Ready for review") if policy != "trial" else M("Trial: would have merged") if confident else M("Ready for review")
            db.log_event(c, jid, "pr", M("PR #{n} opened: {title}", n=n, title=j['title']))
            db.log_audit(c, "fabrique", M("Merge decision"), jid, vals.get("verified_sha") or j["verified_sha"] or "",
                         M("{policy} · {state}", policy=jobs.POLICIES[policy][0], state=M("confident") if confident else M("not confident")))
        if stage == "merged":
            vals.update(merge_state="merged", activity="idle")
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            db.log_event(c, jid, M("merged"), M("PR #{p} merged (squash), pinned to {verified_sha}", p=j['pr_number'] or '', verified_sha=j['verified_sha']))
            db.log_audit(c, "fabrique", M("Merged"), jid, j["verified_sha"] or "", M("Merged by Fabrique (squash)"))
        if stage == "failed":
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
        vals["status_text"] = status
        c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(**vals))
        c.execute(update(db.agents).where(db.agents.c.job_id == jid, db.agents.c.name == M("main session"))
                  .values(tokens=tokens, cost=cost))
        c.execute(insert(db.timeline).values(job_id=jid, stage=stage, at=t, summary=_summary(stage, j)))
        db.log_event(c, jid, stage, jobs.STAGE[stage]["label"])


NEXT = {"queued": "triage", "triage": "investigating", "investigating": "implementing", "implementing": "verifying",
        "verifying": "review", "review": "pr"}


MAX_LIVE = 12        # demo jobs displayed at most (not archived): the demo does not grow indefinitely
MAX_ARCHIVED = 30    # archived demo jobs kept; the oldest are deleted (dummy data)
HUMAN_WAIT = 180     # seconds: beyond this, the simulator responds, validates or approves on behalf of an absent human
ANSWERS = [M("(A), thanks."), M("Rather (B)."), M("No, case-insensitive.")]


def _renew(live, t):
    """Renewal: a job waiting for a human since HUMAN_WAIT receives its response; at the ceiling, the oldest
    completed job is archived to make room for a new request. Returns True if it did something."""
    stale = sorted((j for j in live if t - (j["updated_at"] or t) >= HUMAN_WAIT), key=lambda j: j["updated_at"] or 0)
    for j in stale:
        if j["stage"] == "waiting":
            with db.tx() as c:
                c.execute(update(db.questions).where(db.questions.c.job_id == j["id"], db.questions.c.answered_at.is_(None))
                          .values(answer=RNG.choice(ANSWERS), answered_by="camille", answered_at=t))
                db.log_event(c, j["id"], M("answer"), M("requester's answer (demo)"))
            step(j["ref"], "investigating")
            return True
        if j["stage"] == "spec":
            with db.tx() as c:
                db.log_event(c, j["id"], "spec", M("spec approved (demo)"))
            step(j["ref"], "investigating")
            return True
        if j["stage"] == "pr" and j["merge_state"] == "ready":
            with db.tx() as c:
                db.log_audit(c, "camille", M("Merge approved"), j["id"], j["verified_sha"] or "", M("demo approval"))
            step(j["ref"], "merged")
            return True
    if len(live) >= MAX_LIVE:
        done = sorted((j for j in live if j["stage"] in ("merged", "failed")), key=lambda j: j["updated_at"] or 0)
        if done:
            with db.tx() as c:
                c.execute(update(db.jobs).where(db.jobs.c.id == done[0]["id"]).values(archived=True, updated_at=t))
                db.log_event(c, done[0]["id"], M("archived"), M("archived by the simulator (demo renewal)"))
            _trim_archived()
            return True
    return False


def _trim_archived():
    """Only keeps the most recent MAX_ARCHIVED archived demo jobs: otherwise, a demo left running
    would fill the archives with dummy jobs. Never touch a real job."""
    from sqlalchemy import delete
    with db.tx() as c:
        old = [r["id"] for r in db.rows(c, select(db.jobs.c.id).where(db.jobs.c.archived.is_(True), db.jobs.c.source == "demo")
                                        .order_by(db.jobs.c.updated_at.desc()).offset(MAX_ARCHIVED).limit(500))]
        if not old:
            return 0
        for t in reversed(db.meta.sorted_tables):
            if "job_id" in t.c:
                c.execute(delete(t).where(t.c.job_id.in_(old)))
        c.execute(delete(db.jobs).where(db.jobs.c.id.in_(old)))
    return len(old)


def tick():
    """A simulator step: advances a job, merges what has been approved, renews the demo (responses on behalf of
    an absent human, archiving the oldest completed jobs) and sometimes adds a request, without exceeding MAX_LIVE jobs.
    Paused when demo data is hidden."""
    if not jobs.demo_visible():
        return
    t = time.time()
    with db.tx() as c:
        live = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source == "demo"))   # never real jobs
        run = db.one(c, select(db.runners.c.id).where(db.runners.c.online.is_(True), db.runners.c.token_hash.is_(None)))
        c.execute(update(db.runners).where(db.runners.c.online.is_(True), db.runners.c.token_hash.is_(None)).values(
            last_seen=t, in_flight=sum(1 for j in live if j["stage"] in jobs.ACTIVE),
            metrics={"cpu": RNG.randint(8, 64), "mem": [round(RNG.uniform(4, 9), 1), 16], "disk": [61, 120]}))
    rid = run["id"] if run else None
    for j in live:
        if j["merge_state"] in ("approved", "waiting_checks"):
            return step(j["ref"], "merged", rid)
    if _renew(live, t):
        return
    movable = [j for j in live if j["stage"] in NEXT]
    if len(live) < MAX_LIVE and len([j for j in live if j["stage"] == "queued"]) < 1 and RNG.random() < 0.5:
        title, req, done = RNG.choice(POOL)
        with db.tx() as c:
            repo = db.one(c, select(db.repos.c.id).where(db.repos.c.is_default.is_(True)))
        if repo:
            jobs.create({"user": "camille", "name": "Camille", "role": "admin"}, title, req, done, repo["id"], source="demo")
            return
    if not movable:
        return
    j = RNG.choice(movable)
    nxt = NEXT[j["stage"]]
    if j["stage"] == "triage" and RNG.random() < 0.15:
        with db.tx() as c:
            asked = db.one(c, select(db.questions.c.id).where(db.questions.c.job_id == j["id"]))
        if not asked:
            nxt = "waiting"
    step(j["ref"], nxt, rid, seed_index=j["id"])


_stop = threading.Event()


def start():
    def loop():
        while not _stop.wait(config.DEMO_TICK):
            try:
                tick()
            except Exception:
                log.exception(M("demo simulator"))
    _stop.clear()
    threading.Thread(target=loop, name="demo", daemon=True).start()


def stop():
    _stop.set()
