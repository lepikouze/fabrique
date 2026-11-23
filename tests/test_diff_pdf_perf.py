"""0.7.5: diff of the job read in the dashboard, study report in PDF, number of reads in the database per page."""
import io, re, time
import pytest
from sqlalchemy import event, select, update
from app import db, demo, diffview, jobs, pdf, work
from .conftest import ADMIN, REQ, REVIEWER, web

DIFF = """diff --git a/app/a.py b/app/a.py
index 111..222 100644
--- a/app/a.py
+++ b/app/a.py
@@ -1,3 +1,4 @@ def f():
 x = 1
-y = 2
+y = 3
+z = "<script>alert(1)</script>"
 return x
diff --git a/auth/droits.py b/auth/droits.py
new file mode 100644
index 000..333
--- /dev/null
+++ b/auth/droits.py
@@ -0,0 +1,2 @@
+un
+++ deux
\\ No newline at end of file
diff --git a/img.png b/img.png
index 1..2 100644
Binary files a/img.png and b/img.png differ
diff --git a/vieux.py b/vieux.py
deleted file mode 100644
index 1..0
--- a/vieux.py
+++ /dev/null
@@ -1 +0,0 @@
-fin
"""


def api(tok):
    cl = web()
    cl.headers["Authorization"] = f"Bearer {tok}"
    return cl


def claimed(repo, sha="abcdef123456", user=REQ, title="Exporter"):
    """A job taken by a runner, with evidence at the given sha; returns (ref, runner client)."""
    ref = jobs.create(user, title, "En CSV", "testé", repo)
    a = api(work.create_runner(f"runner-{ref.lower()}", "container", "alice"))
    assert a.post("/api/runner/claim").json()["job"]["ref"] == ref
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "evidence", "sha": sha, "checks": [{"name": "tests", "command": "pytest", "ok": True}]}).status_code == 200
    return ref, a


# ---------- diff splitting ----------

def test_parse_counts_lines_per_file_and_keeps_headers_lookalikes_as_content():
    fs = diffview.parse(DIFF)
    assert [(f["path"], f["status"], f["add"], f["rem"]) for f in fs] == [
        ("app/a.py", "modified", 2, 1), ("auth/droits.py", "added", 2, 0), ("img.png", "binary", 0, 0), ("vieux.py", "deleted", 0, 1)]
    a = [l for h in fs[0]["hunks"] for l in h["lines"]]
    assert a[0] == ("ctx", 1, 1, "x = 1") and a[1] == ("rem", 2, None, "y = 2") and a[2] == ("add", None, 2, "y = 3") and a[-1] == ("ctx", 3, 4, "return x")
    b = [l for h in fs[1]["hunks"] for l in h["lines"]]
    assert b[1] == ("add", None, 2, "++ deux")                # an added line starting with "++" is not a header
    assert b[2][0] == "note" and fs[2]["binary"] and not fs[2]["hunks"]
    assert diffview.parse("") == [] and diffview.parse("n'importe quoi\n@@ -1 +1 @@\n") == []


def test_clip_cuts_at_a_line_end_and_says_so(monkeypatch):
    monkeypatch.setattr(diffview, "MAX_TEXT", 40)
    text, cut = diffview.clip("diff --git a/x b/x\n" + "+ligne\n" * 20)
    assert cut and len(text) <= 40 and text.endswith("+ligne")
    assert diffview.clip("court\r\n") == ("court\n", False)


# ---------- reception, rights, display ----------

def test_runner_diff_is_stored_shown_in_the_job_and_escaped(repo):
    ref, a = claimed(repo)
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef1234567890", "text": DIFF, "truncated": False}).status_code == 200
    j = jobs.get(ADMIN, ref)
    p = jobs.patch_info(j)
    assert p["sha"] == "abcdef123456" and (p["n"], p["add"], p["rem"]) == (4, 4, 2) and not p["truncated"]
    panel = web().get(f"/jobs/{ref}/panel").text
    assert "Changes" in panel and f'data-diff="/jobs/{ref}/diff?sha=abcdef123456"' in panel and "app/a.py" in panel
    assert "Changes" in web().get(f"/jobs/{ref}").text                      # vue en grand
    r = web().get(f"/jobs/{ref}/diff?sha=abcdef123456")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text and "<script>" not in r.text       # repo content: always escaped
    assert r.text.count('<details class="dfile"') == 4 and "Binary file" in r.text
    assert re.search(r'auth/droits\.py</span><span class="pill">added</span><span class="pill prot"', r.text)   # **/auth/** is protected
    assert 'class="pill prot"' not in r.text.split("app/a.py")[1].split("auth/droits.py")[0]


def test_diff_follows_the_rights_of_the_job_and_the_verified_sha(repo):
    ref, a = claimed(repo)
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF})
    assert web("requester").get(f"/jobs/{ref}/diff").status_code == 404        # open mode: simulated "requester" is not the request
    assert jobs.diff_view(REQ, ref) and jobs.diff_view({"user": "autre", "name": "A", "role": "requester"}, ref) is None
    assert jobs.diff_view(REVIEWER, ref)["files"][0]["path"] == "app/a.py"
    assert jobs.diff_view(REVIEWER, ref, "000000000000") is None                  # address of an old diff: refused
    # New commit verified: the old diff is no longer shown, neither on the job page nor at its address.
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "evidence", "sha": "999999999999", "checks": []})
    assert jobs.patch_info(jobs.get(ADMIN, ref)) is None and jobs.diff_view(ADMIN, ref) is None
    assert "Changements" not in web().get(f"/jobs/{ref}/panel").text
    # The runner returns the diff of the new commit: it replaces the old one (one line per job).
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "999999999999", "text": "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-a\n+b\n"})
    assert [f["path"] for f in jobs.diff_view(ADMIN, ref)["files"]] == ["b.py"]
    with db.tx() as c:
        assert len(db.rows(c, select(db.diffs))) == 1


def test_invalid_or_oversized_diff(repo, monkeypatch):
    ref, a = claimed(repo)
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "pas-un-sha", "text": DIFF}).status_code == 403
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": ["x"]}).status_code == 403
    monkeypatch.setattr(diffview, "MAX_TEXT", 200)
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF}).status_code == 200
    with db.tx() as c:
        row = db.one(c, select(db.diffs))
    assert row["truncated"] and len(row["text"]) <= 200
    assert "truncated" in web().get(f"/jobs/{ref}/panel").text and "Diff truncated" in web().get(f"/jobs/{ref}/diff").text


def test_a_study_has_no_diff_and_deleting_a_job_deletes_its_diff(repo):
    ref, a = claimed(repo)
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF})
    with db.tx() as c:
        c.execute(update(db.jobs).values(source="study"))
    assert jobs.patch_info(jobs.get(ADMIN, ref)) is None and jobs.diff_view(ADMIN, ref) is None
    with db.tx() as c:
        c.execute(update(db.intents).values(state="done"))
        c.execute(update(db.jobs).values(source="form", stage="merged", archived=True))
    jobs.delete_job(ADMIN, ref, f"alice/essai/{ref}")
    with db.tx() as c:
        assert db.rows(c, select(db.diffs)) == []


def test_demo_jobs_with_a_pr_have_a_fake_diff(demo_on):
    demo.seed()
    with db.tx() as c:
        withpr = db.rows(c, select(db.jobs).where(db.jobs.c.pr_number.is_not(None), db.jobs.c.verified_sha.is_not(None)))
        assert withpr and len(db.rows(c, select(db.diffs))) == len(withpr)
        c.execute(db.diffs.delete())
    demo.backfill_diffs()                                                       # base demo before 0.7.5
    j = jobs.get(ADMIN, withpr[0]["ref"])
    p = jobs.patch_info(j)
    assert p and p["n"] == 3 and jobs.review_effort(j)["files"] == 3
    assert "to_csv" in web().get(f"/jobs/{j['ref']}/diff").text


# ---------- repo setting: no diff for a confidential repo ----------

def test_repo_setting_turns_the_diff_off_and_deletes_what_was_stored(repo):
    ref, a = claimed(repo)
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF})
    cl = web()
    page = cl.get(f"/repos?repo={repo}").text
    assert 'name="diff" value="1" checked' in page and "confidential repo" in page          # enabled by default
    # A form that does not check the box (other client, old tab) does not change the setting.
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "accepts_jobs": "1"})
    assert jobs.patch_info(jobs.get(ADMIN, ref))
    with db.tx() as c:
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(pr_url="https://git.example.org/alice/essai/pulls/3", pr_number=3))
    # Unchecked box: the already copied diff is deleted, the job page says so and points to the Git host.
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "accepts_jobs": "1", "diff_field": "1"})
    with db.tx() as c:
        assert db.rows(c, select(db.diffs)) == []
        assert db.one(c, select(db.repos.c.settings).where(db.repos.c.id == repo))["settings"]["diff"] is False
        assert any(r["action"] == "Diff turned off for the repo" and "1 diff(s)" in (r["detail"] or "") for r in db.rows(c, select(db.audit)))
    assert 'name="diff" value="1" checked' not in cl.get(f"/repos?repo={repo}").text
    j = jobs.get(ADMIN, ref)
    assert jobs.patch_info(j) is None and jobs.diff_off(j) and jobs.diff_view(ADMIN, ref) is None
    for url in (f"/jobs/{ref}/panel", f"/jobs/{ref}"):
        html = cl.get(url).text
        assert "The diff is not kept in Fabrique for this repo" in html and "pulls/3/files" in html and "data-diff=" not in html
    assert cl.get(f"/jobs/{ref}/diff").status_code == 404
    # The dashboard keeps nothing, even if a runner sends it anyway; the job is not affected by this.
    assert a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF}).status_code == 200
    with db.tx() as c:
        assert db.rows(c, select(db.diffs)) == []
    # Rechecked: subsequent diffs are kept (the old one is not recreated).
    cl.post(f"/repos/{repo}", data={"merge_policy": "off", "accepts_jobs": "1", "diff_field": "1", "diff": "1"})
    assert jobs.patch_info(jobs.get(ADMIN, ref)) is None
    a.post(f"/api/runner/jobs/{ref}/event", json={"type": "diff", "sha": "abcdef123456", "text": DIFF})
    assert jobs.patch_info(jobs.get(ADMIN, ref))["n"] == 4


def test_the_claim_tells_the_runner_whether_the_repo_keeps_diffs(repo):
    jobs.create(REQ, "Un", "x", "y", repo)
    a = api(work.create_runner("r-un", "container", "alice"))
    assert a.post("/api/runner/claim").json()["repo"]["diff"] is True
    with db.tx() as c:
        c.execute(update(db.repos).where(db.repos.c.id == repo).values(settings={"merge_policy": "off", "diff": False}))
    jobs.create(REQ, "Deux", "x", "y", repo)
    b = api(work.create_runner("r-deux", "container", "alice"))
    assert b.post("/api/runner/claim").json()["repo"]["diff"] is False


def test_only_admins_change_the_diff_setting(repo):
    web("reviewer").post(f"/repos/{repo}", data={"merge_policy": "off", "diff_field": "1"})
    with db.tx() as c:
        assert "diff" not in db.one(c, select(db.repos.c.settings).where(db.repos.c.id == repo))["settings"]


# ---------- study report in PDF ----------

MD = """## Réponse courte

Oui : **faisable** en une *demi-journée* ; coût ≤ 2 $ → voir [la RFC](https://www.rfc-editor.org/rfc/rfc4180) et `csv.writer`.

1. Premier constat « entre guillemets »
   - sous-point – avec tiret
2. Second constat ✅

| Option | Effort |
|---|---|
| A. Serveur | 0,5 jour |

```python
def f():
    return "é"
```

> Limite : rien n'a été lancé.

Mal fermé : **gras sans fin et <b>balise</b> & [lien](javascript:alert(1))
"""


def test_markdown_is_cut_into_blocks_and_unsafe_markup_is_inert():
    kinds = [b[0] for b in pdf.blocks(MD)]
    assert kinds == ["h", "p", "li", "li", "li", "table", "code", "quote", "p"]
    assert pdf.blocks(MD)[3][1] == 1 and pdf.blocks(MD)[5][1] == [["Option", "Effort"], ["A. Serveur", "0,5 jour"]]
    out = pdf.inline("**gras** *ital* `x<y` [la RFC](https://example.org/a?b=1&c=2) [piège](javascript:alert(1)) <b>brut</b>")
    assert "<b>gras</b>" in out and "<i>ital</i>" in out and "x&lt;y" in out and 'href="https://example.org/a?b=1&amp;c=2"' in out
    assert "javascript" in out and 'href="javascript' not in out and "&lt;b&gt;brut&lt;/b&gt;" in out
    assert pdf.clean("a\u00adb ✅ ⚠\ufe0f → ≥ « é » 🚀") == "ab ✓ ⚠ → ≥ « é » □"      # outside font: equivalent or square, never a hole


def test_pdf_report_route_gives_a_real_pdf_with_accents(repo):
    ref = jobs.create(REQ, "Étude : exporter en CSV — coût", "Est-ce faisable ?", "", repo, study=True)
    cl = web()
    assert cl.get(f"/jobs/{ref}/report.pdf").status_code == 404                # no report yet
    with db.tx() as c:
        c.execute(update(db.jobs).values(report=MD, stage="done"))
    r = cl.get(f"/jobs/{ref}/report.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF-")
    assert r.headers["content-disposition"] == f'attachment; filename="{ref}-etude.pdf"' and r.headers["cache-control"] == "no-store"
    assert web("requester").get(f"/jobs/{ref}/report.pdf").status_code == 404   # same rights as the job
    assert b"/FontFile2" in r.content                                            # embedded font (accents, arrows)
    for page in ("/jobs/{}/panel", "/inbox/{}/panel"):
        html = cl.get(page.format(ref)).text
        assert f'href="/jobs/{ref}/report.pdf"' in html and f'href="/jobs/{ref}/report.md"' in html
    pypdf = pytest.importorskip("pypdf")                                         # text review, if the tool is there
    text = pypdf.PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert "Étude : exporter en CSV — coût" in text and "≤ 2 $ →" in text and "« entre guillemets »" in text and "alice/essai" in text


LIMITS = {"section": "limits", "budget_job": "5", "budget_day": "30", "max_parallel": "2", "size_limit": "300", "watchdog_min": "10",
          "auto_archive_days": "0", "protected_paths": ".fabrique/**"}


def test_report_downloads_can_be_turned_off_one_by_one(repo):
    ref = jobs.create(REQ, "Study", "Est-ce faisable ?", "", repo, study=True)
    with db.tx() as c:
        c.execute(update(db.jobs).values(report=MD, stage="done"))
    cl = web()
    st = cl.get("/settings?tab=limits").text
    assert 'name="report_pdf" value="1" checked' in st and 'name="report_md" value="1" checked' in st      # the two by default
    both = lambda: (cl.get(f"/jobs/{ref}/report.pdf").status_code, cl.get(f"/jobs/{ref}/report.md").status_code)
    assert both() == (200, 200)
    # A budget record that does not include the checkboxes does not cut anything.
    cl.post("/settings/limits", data=LIMITS)
    assert both() == (200, 200)
    # PDF seul.
    cl.post("/settings/limits", data={**LIMITS, "report_fields": "1", "report_pdf": "1"})
    assert both() == (200, 404)
    for page in (f"/jobs/{ref}/panel", f"/inbox/{ref}/panel", f"/jobs/{ref}"):
        html = cl.get(page).text
        assert f'href="/jobs/{ref}/report.pdf"' in html and "report.md" not in html
    # Markdown only: the button then carries the full label.
    cl.post("/settings/limits", data={**LIMITS, "report_fields": "1", "report_md": "1"})
    assert both() == (404, 200)
    html = cl.get(f"/jobs/{ref}/panel").text
    assert "report.pdf" not in html and "Download the report (Markdown)" in html
    assert 'name="report_pdf" value="1" checked' not in cl.get("/settings?tab=limits").text
    # None: no button, the report remains readable in the file.
    cl.post("/settings/limits", data={**LIMITS, "report_fields": "1"})
    assert both() == (404, 404)
    html = cl.get(f"/jobs/{ref}/panel").text
    assert "report.pdf" not in html and "report.md" not in html and "Study report" in html
    # The other limits have not changed, and a record without the checkboxes keeps the choice made.
    cl.post("/settings/limits", data=LIMITS)
    assert both() == (404, 404)


def test_pdf_never_fails_on_odd_markdown():
    for md in ("", "****", "| a |\n|---|\n", "```\nnon fermé", "- " * 50, "<para>&amp;</para>", "*a **b* c**", "[x](http://a b)", "# " + "x" * 5000,
               "\n".join(f"| c{i} | " + "mot " * 80 + "|" for i in range(40)), "a\x00b\x07c",
               "| a | b |\n|---|---|\n| " + "mot " * 4000 + " | 2 |", "###### " + "t " * 400 + "\n\nsuite", "x" * 3000):
        assert pdf.report("T" * 3000, "m", md).startswith(b"%PDF-")


# ---------- lectures en base par page ----------

class Count:
    def __enter__(self):
        self.n = 0
        self.hook = lambda *a: setattr(self, "n", self.n + 1)
        event.listen(db.engine(), "before_cursor_execute", self.hook)
        return self

    def __exit__(self, *a):
        event.remove(db.engine(), "before_cursor_execute", self.hook)


def test_pages_read_the_database_a_bounded_number_of_times(demo_on):
    """0.7.5 watchdog: a page does not review the list of jobs three times nor the settings twenty times. The bounds
    leave a little margin; an overflow signals repeated reading to be corrected (see db.cached)."""
    demo.seed()
    with db.tx() as c:
        ref = db.one(c, select(db.jobs.c.ref).where(db.jobs.c.archived.is_(False)).order_by(db.jobs.c.id))["ref"]
    cl = web()
    bounds = {"/jobs": 10, "/jobs?view=list": 10, "/inbox": 22, "/archives": 11, "/repos": 13, "/settings": 14, "/activity": 10,
              "/runners": 14, "/new": 11, f"/jobs/{ref}/panel": 14, f"/jobs/{ref}": 19}
    seen = {}
    for url, limit in bounds.items():
        assert cl.get(url).status_code == 200
        with Count() as q:
            assert cl.get(url).status_code == 200
        seen[url] = q.n
    assert all(seen[u] <= bounds[u] for u in bounds), {u: (seen[u], bounds[u]) for u in bounds if seen[u] > bounds[u]}
    n = max(1, len(jobs.listing(ADMIN)))
    with db.tx() as c:                                                           # twice as many jobs: not an extra read
        for j in db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False))):
            c.execute(db.jobs.insert().values({k: v for k, v in j.items() if k != "id"} | {"ref": j["ref"] + "-bis"}))
    assert len(jobs.listing(ADMIN)) == 2 * n
    with Count() as q:
        assert cl.get("/jobs").status_code == 200
    assert q.n == seen["/jobs"]


def test_request_cache_never_serves_stale_data(repo):
    """The cache only lives for the duration of a read request: a write is visible from the next request, and nothing
    is kept outside the request."""
    cl = web()
    assert "Premier" not in cl.get("/jobs").text
    jobs.create(REQ, "Premier", "B", "", repo)
    assert "Premier" in cl.get("/jobs").text
    assert cl.post("/settings/limits", data={"budget_job": "7", "budget_day": "30", "max_parallel": "2", "size_limit": "300", "watchdog_min": "10",
                                             "protected_paths": "x/**"}).status_code == 303
    assert 'value="7' in cl.get("/settings?tab=limits").text
    with db.tx() as c:
        v, _ = db.get_setting(c, "limits")
        v["budget_job"] = 99                                                     # modifying what we received does not affect anything else
        assert db.get_setting(c, "limits")[0]["budget_job"] == 7
    assert db._request.get() is None
