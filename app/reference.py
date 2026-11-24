"""Reference requests, replayed before a version is published: a version ships only if they pass.

Run inside the dashboard container (it files the requests the way the "New request" form does, as an existing account,
then follows them in the database until they end):

    python -m app.reference --repo team/test-repo --user alice                  # local model: small change + stop
    python -m app.reference --repo team/test-repo --user alice --changes        # + "Ask for changes" on the PR
    python -m app.reference --repo team/test-repo --user alice --module --profile economy   # + a module (paid model)

What is checked:
- small: a small change with its test reaches a PR ready for review (or merged, on a repo that merges on its own), checks green;
- stop: a job stopped while it works ends "stopped" within a minute, and no job container stays behind;
- changes: "Ask for changes" on the small PR brings the same PR back, ready, after a second attempt (needs a repo whose
  merges are decided by a human);
- module: a whole module (data, page, tests) reaches a PR ready for review.
The requests stay in Fabrique afterwards: refuse their PRs (the runner closes them) and archive them."""
import argparse, sys, time
from sqlalchemy import select
from . import db, jobs

SMALL = ("Reference: add a slugify helper",
         "Add a function slugify(text) to a new module refcheck/slug.py (create the package if needed): lowercase, accents removed, "
         "every run of characters other than a-z and 0-9 replaced by a single hyphen, no hyphen at either end. Add its tests.",
         "pytest passes, with tests for accents, spaces, punctuation and an empty string.")
STOP = ("Reference: stop while working",
        "Add a function word_count(text) to a new module refcheck/words.py, with tests. (This request is stopped on purpose by the "
        "reference run.)", "pytest passes.")
MODULE = ("Reference: a small module",
          "Add a small 'notes' feature to the repository: a SQLite table (id, title, body, created_at) created at start, functions "
          "add_note, list_notes (newest first) and delete_note in a new module refcheck/notes.py, and their tests (empty list, add "
          "then list, delete, ordering). Keep the existing code style.",
          "pytest passes, with the four tests listed.")
CHANGES_NOTE = "Also handle None: the function returns an empty string for None, with a test."


def _user(c, name):
    r = db.one(c, select(db.users).where(db.users.c.username == name))
    if not r:
        sys.exit(f"unknown account: {name}")
    return {"user": r["username"], "name": r["name"] or r["username"], "email": r["email"], "role": r["role"], "groups": r["groups"]}


def _job(ref):
    with db.tx() as c:
        return db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))


def wait(ref, done, timeout, every=10):
    """Waits until done(job) is true or the job ends; returns the job."""
    end = time.time() + timeout
    while time.time() < end:
        j = _job(ref)
        if done(j) or j["stage"] in ("failed", "closed", "merged", "done"):
            return j
        time.sleep(every)
    return _job(ref)


def ready(j):
    return j["merge_state"] == "ready" and j["stage"] == "pr"


def report(name, ok, j, why=""):
    cost = f"{float(j['cost'] or 0):.3f} $" if j else "—"
    print(f"{'PASS' if ok else 'FAIL'}  {name:<8} {j['ref'] if j else '—':<8} stage={j['stage'] if j else '—':<12} "
          f"checks={j['checks_ok'] if j else 0}/{j['checks_total'] if j else 0} cost={cost} {why}")
    return ok


def main(argv=None):
    a = argparse.ArgumentParser(description="Replays Fabrique's reference requests.")
    a.add_argument("--repo", required=True, help="slug of a test repo already onboarded")
    a.add_argument("--user", required=True, help="account the requests are filed as (reviewer or admin)")
    a.add_argument("--model", default="", help="model of the requests (default: the repo's or the global setting)")
    a.add_argument("--profile", default="", help="model profile of the module request")
    a.add_argument("--changes", action="store_true", help="also ask for changes on the small PR")
    a.add_argument("--module", action="store_true", help="also run the module request (meant for a paid model)")
    a.add_argument("--timeout", type=int, default=3600, help="seconds allowed per request")
    o = a.parse_args(argv)
    with db.tx() as c:
        user = _user(c, o.user)
        repo = db.one(c, select(db.repos).where(db.repos.c.slug == o.repo))
    if not repo:
        sys.exit(f"unknown repo: {o.repo}")
    results = []
    # Each run asks for new code (a name of its own): the same request twice would be answered "already done".
    tag = time.strftime("%Y%m%d%H%M")
    small_req = tuple(t.replace("refcheck/slug.py", f"refcheck/slug_{tag}.py").replace("slugify(", f"slugify_{tag}(") for t in SMALL)
    stop_req = tuple(t.replace("refcheck/words.py", f"refcheck/words_{tag}.py") for t in STOP)
    module_req = tuple(t.replace("refcheck/notes.py", f"refcheck/notes_{tag}.py") for t in MODULE)

    # small change
    ref = jobs.create(user, *small_req, repo["id"], model=o.model)
    print(f"... small    {ref} filed")
    j = wait(ref, ready, o.timeout)
    green = j["checks_ok"] == j["checks_total"] and j["checks_total"] > 0
    # A repo set to "merge if confident" merges on its own when every gate holds: that is a success too.
    results.append(report("small", (ready(j) or j["stage"] == "merged") and green, j))
    small = j

    # stop while working
    ref = jobs.create(user, *stop_req, repo["id"], model=o.model)
    print(f"... stop     {ref} filed")
    j = wait(ref, lambda j: j["stage"] in ("implementing", "investigating", "triage") and j["activity"] == "active", 900, every=5)
    if j["activity"] == "active":
        jobs.cancel(user, ref)
        asked = time.time()
        j = wait(ref, lambda j: j["stage"] == "failed", 120, every=3)
        took = time.time() - asked
        results.append(report("stop", j["stage"] == "failed" and took <= 90, j, f"stopped in {took:.0f} s"))
    else:
        results.append(report("stop", False, j, "never started working"))

    # ask for changes on the small PR
    if o.changes:
        if ready(small):
            jobs.decide(user, small["ref"], "changes", CHANGES_NOTE)
            print(f"... changes  {small['ref']} changes requested")
            time.sleep(15)
            j = wait(small["ref"], lambda j: ready(j) and (j["attempts"] or 1) >= 2, o.timeout)
            results.append(report("changes", ready(j) and (j["attempts"] or 1) >= 2 and j["pr_number"] == small["pr_number"], j,
                                  f"same PR #{j['pr_number']}"))
        else:
            why = ("the repo merged the PR on its own (merge policy): use a repo whose merges are decided by a human"
                   if small["stage"] == "merged" else "no ready PR to ask changes on")
            results.append(report("changes", False, small, why))

    # module
    if o.module:
        ref = jobs.create(user, *module_req, repo["id"], model="" if o.profile else o.model, profile=o.profile)
        print(f"... module   {ref} filed")
        j = wait(ref, ready, o.timeout)
        results.append(report("module", ready(j) and j["checks_ok"] == j["checks_total"] and j["checks_total"] > 0, j))

    print("\nRESULT:", "every reference request passes" if all(results) else "at least one reference request fails")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
