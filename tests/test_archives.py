"""Archives (0.7.3): job numbers are never reassigned, search page, permanent deletion confirmed by input,
automatic archiving, cleaning of the runner's working directory."""
import time
import pytest
from sqlalchemy import select, update
from app import db, jobs, work
from .conftest import ADMIN, REQ, REVIEWER, web


def finished(repo, title, stage="merged", user=ADMIN, archived=True, **vals):
    ref = jobs.create(user, title, "Besoin : " + title, "", repo)
    with db.tx() as c:
        c.execute(update(db.intents).values(state="done"))
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(stage=stage, archived=archived, **vals))
    return ref


def test_a_deleted_number_is_never_given_again(repo):
    a, b = finished(repo, "Un"), finished(repo, "Deux")
    assert (a, b) == ("FAB-1", "FAB-2")
    jobs.delete_job(ADMIN, b, "alice/essai/FAB-2")
    assert jobs.create(ADMIN, "Trois", "B", "", repo) == "FAB-3"                  # not FAB-2: commits still point to it
    with db.tx() as c:
        assert db.get_setting(c, "job_seq")[0] == {"n": 3}


def test_counter_starts_from_the_highest_existing_number(repo):
    finished(repo, "Un")
    with db.tx() as c:
        c.execute(update(db.jobs).values(ref="FAB-41"))
        c.execute(db.settings.delete().where(db.settings.c.key == "job_seq"))    # installation before the counter
    assert jobs.create(ADMIN, "Suivant", "B", "", repo) == "FAB-42"


def test_search_by_words_repo_outcome_and_regex(repo):
    finished(repo, "Export the links as CSV")
    finished(repo, "Import HTML bookmarks", stage="failed")
    finished(repo, "Full-text search", merge_state="refused", stage="pr")
    finished(repo, "Encore en cours", archived=False)
    rows, total, err, page = jobs.search_archives(ADMIN)
    assert total == 3 and not err and [j["outcome"] for j in rows] == ["refused", "failed", "merged"]
    find = lambda **kw: [j["title"] for j in jobs.search_archives(ADMIN, **kw)[0]]
    assert find(q="links csv") == ["Export the links as CSV"] and find(q="LINKS  export") == ["Export the links as CSV"]
    assert find(q="FAB-2") == ["Import HTML bookmarks"] and find(q="alice/essai") and not find(q="introuvable")
    assert find(state="failed") == ["Import HTML bookmarks"] and find(state="refused") == ["Full-text search"]
    assert sorted(find(q="^FAB-[12]$", regex=True)) == ["Export the links as CSV", "Import HTML bookmarks"]   # anchored to the reference
    assert find(q="export|import", regex=True) and not find(repo=999)
    rows, total, err, _ = jobs.search_archives(ADMIN, q="(", regex=True)
    assert rows == [] and "Invalid regular expression" in err                                       # poorly written pattern: message, no 500 error
    assert jobs.search_archives(REQ)[1] == 0                                      # a requester only sees their own jobs


def test_pagination(repo, monkeypatch):
    monkeypatch.setattr(jobs, "ARCHIVE_PAGE", 2)
    for i in range(5):
        finished(repo, f"Job {i}")
    rows, total, _, page = jobs.search_archives(ADMIN, page=3)
    assert total == 5 and len(rows) == 1 and page == 3
    assert jobs.search_archives(ADMIN, page=99)[3] == 3                           # beyond the end: last page


def test_delete_needs_admin_archive_and_the_typed_phrase(repo):
    ref = finished(repo, "À supprimer")
    with pytest.raises(jobs.Refused, match="admins"):
        jobs.delete_job(REVIEWER, ref, "alice/essai/FAB-1")
    for wrong in ("", "FAB-1", "alice/essai/fab-1", "alice/essai/FAB-2"):
        with pytest.raises(jobs.Refused, match="type exactly alice/essai/FAB-1"):
            jobs.delete_job(ADMIN, ref, wrong)
    live = finished(repo, "Encore visible", archived=False)
    with pytest.raises(jobs.Refused, match="archive it first"):
        jobs.delete_job(ADMIN, live, "alice/essai/FAB-2")
    with db.tx() as c:
        jid = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref))["id"]
    assert jobs.delete_job(ADMIN, ref, "  alice/essai/FAB-1 ") == ref
    with db.tx() as c:
        assert not db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        for t in db.meta.sorted_tables:                                             # nothing points to the job anymore
            if "job_id" in t.c:
                assert not db.rows(c, select(t).where(t.c.job_id == jid)), t.name
        audit = db.rows(c, select(db.audit).order_by(db.audit.c.id))
    last = audit[-1]
    assert last["action"] == "Job deleted" and last["target"] == ref and "À supprimer" in last["detail"] and last["actor"] == "alice"
    assert any(a["action"] == "Request created" and a["job_id"] is None for a in audit)   # the old job audit is kept, detached


def test_archives_page_search_restore_and_delete_through_the_web(repo):
    a, b = finished(repo, "Exporter en CSV"), finished(repo, "Import HTML bookmarks")
    cl = web()
    page = cl.get("/archives").text
    assert "Search the archives" in page and a in page and b in page and 'data-delete-phrase="alice/essai/FAB-1"' in page
    part = cl.get("/archives?q=csv&partial=1").text
    assert a in part and b not in part and "<html" not in part                    # search while typing: the list only
    assert "Invalid regular expression" in cl.get("/archives?q=(&regex=1").text
    r = cl.post(f"/jobs/{a}/delete", data={"typed": "mauvais"}, headers={"referer": "http://testserver/archives?q=csv"})
    assert r.headers["location"] == "/archives?q=csv" and "Wrong confirmation" in cl.get(r.headers["location"]).text
    r = cl.post(f"/jobs/{a}/delete", data={"typed": "alice/essai/FAB-1"})
    assert "permanently deleted" in cl.get(r.headers["location"]).text and a not in cl.get("/archives").text.split("arch-list")[1]
    r = cl.post(f"/jobs/{b}/archive", data={"restore": "1"}, headers={"referer": "http://testserver/archives"})
    assert r.headers["location"] == "/archives" and not jobs.get(ADMIN, b)["archived"]
    v = web("reviewer")                                                            # a reviewer sees the page, without the Delete button
    finished(repo, "Autre")
    assert "data-delete-ref" not in v.get("/archives").text
    assert "admins" in v.get(v.post("/jobs/FAB-3/delete", data={"typed": "alice/essai/FAB-3"}).headers["location"]).text
    assert jobs.get(ADMIN, "FAB-3")["archived"]


def test_bulk_restore_and_delete(repo):
    refs = [finished(repo, f"Job {i}") for i in range(3)]
    cl = web()
    r = cl.post("/archives/bulk", data={"action": "delete", "ref": refs[:2], "typed": "supprimer 3 jobs"})
    assert "Wrong confirmation" in cl.get(r.headers["location"]).text and jobs.search_archives(ADMIN)[1] == 3
    r = cl.post("/archives/bulk", data={"action": "delete", "ref": refs[:2], "typed": "Supprimer 2 jobs"})
    assert "2 job(s) permanently deleted." in cl.get(r.headers["location"]).text
    r = cl.post("/archives/bulk", data={"action": "restore", "ref": refs[2:]})
    assert "1 job(s) restored." in cl.get(r.headers["location"]).text and jobs.search_archives(ADMIN)[1] == 0
    v = web("reviewer")                                                            # same client: the message follows the redirection
    assert "admins" in v.get(v.post("/archives/bulk", data={"action": "delete", "ref": ["FAB-3"], "typed": "supprimer 1 jobs"}).headers["location"]).text


def test_auto_archive_only_touches_old_merged_jobs_and_read_studies(repo):
    old = time.time() - 40 * 86400
    merged = finished(repo, "Mergé ancien", archived=False, updated_at=old)
    recent = finished(repo, "Mergé récent", archived=False)
    failed = finished(repo, "Échec ancien", stage="failed", archived=False, updated_at=old)
    read = finished(repo, "Étude lue", stage="done", archived=False, updated_at=old, report_seen=old)
    unread = finished(repo, "Étude non lue", stage="done", archived=False, updated_at=old)
    assert jobs.auto_archive() == 0                                                # disabled by default
    with db.tx() as c:
        lim, _ = db.get_setting(c, "limits")
        db.set_setting(c, "limits", {**lim, "auto_archive_days": 30}, "test")
    assert jobs.auto_archive() == 2
    state = {r: jobs.get(ADMIN, r)["archived"] for r in (merged, recent, failed, read, unread)}
    assert state == {merged: True, recent: False, failed: False, read: True, unread: False}
    cl = web()
    cl.post("/settings/limits", data={"budget_job": "5", "budget_day": "30", "max_parallel": "2", "size_limit": "300", "watchdog_min": "10",
                                       "auto_archive_days": "14", "protected_paths": ""})
    with db.tx() as c:
        assert db.get_setting(c, "limits")[0]["auto_archive_days"] == 14


def test_runner_is_told_which_workspaces_to_purge(repo):
    old = time.time() - 7200
    done = finished(repo, "Terminé depuis longtemps", archived=False, updated_at=old)
    fresh = finished(repo, "Tout juste terminé", archived=False)
    running = jobs.create(ADMIN, "En cours", "B", "", repo)
    gone = finished(repo, "Supprimé")
    jobs.delete_job(ADMIN, gone, f"alice/essai/{gone}")
    names = [done, fresh, running, gone, "AUTRE-7", "../etc", ".mirrors"]
    assert jobs.workspaces_to_purge(names) == [done, gone]                         # finished for more than an hour, or deleted
    tok = work.create_runner("runner-test", "container", "alice")
    res = work.heartbeat(work.runner_for(tok), {"host": "h", "workspaces": names})
    assert res["purge"] == [done, gone] and work.heartbeat(work.runner_for(tok), {"host": "h"})["purge"] == []


def test_archived_job_opens_in_the_side_panel_and_comes_back_to_the_archives(repo, demo_on):
    ref = finished(repo, "Exporter en CSV")
    cl = web()
    page = cl.get(f"/archives?q=csv&job={ref}").text
    assert f'data-row data-ref="{ref}"' in page and f'href="/archives?q=csv&amp;job={ref}"' in page       # the line opens the dashboard
    panel = page.split('id="panel"')[1]
    assert 'data-expand' in panel and f'href="/jobs/{ref}"' in panel and 'href="/archives" title="Close' in panel
    assert f'data-delete-ref="{ref}"' in panel and 'data-delete-phrase="alice/essai/FAB-1"' in panel   # delete from the record
    full = cl.get(f"/jobs/{ref}").text                                                                 # vue en grand : retour aux Archives
    assert '<a href="/archives" class="crumb">Archives</a>' in full and f'href="/archives?job={ref}"' in full and 'id="del-dlg"' in full
    live = finished(repo, "Encore visible", archived=False)
    full = cl.get(f"/jobs/{live}").text                                                                # job not archived: as before
    assert '<a href="/jobs" class="crumb">Jobs</a>' in full and 'data-delete-ref="' not in full
    assert 'id="del-dlg"' not in web("reviewer").get("/archives").text                                 # the window only exists for an admin
    with db.tx() as c:                                                                                 # an archived demo job
        c.execute(update(db.jobs).where(db.jobs.c.ref == ref).values(source="demo", requester="camille"))
    rows = cl.get("/archives?q=camille").text
    assert ">demo</span>" in rows and 'data-delete-ref="' not in rows.split('id="arch-list"')[1].split('id="panel"')[0]


def test_the_search_survives_a_round_trip_through_the_full_view(repo):
    ref = finished(repo, "Exporter en CSV")
    full = web().get(f"/jobs/{ref}?back=q%3Dcsv%26state%3Dmerged%26regex%3D1%26pirate%3Dx").text
    assert f'href="/archives?q=csv&amp;state=merged&amp;regex=1&amp;job={ref}"' in full                # reduce: search and dashboard
    assert 'href="/archives?q=csv&amp;state=merged&amp;regex=1" title="Close' in full                 # close: search only
    assert 'href="/archives?q=csv&amp;state=merged&amp;regex=1" class="crumb"' in full and "pirate" not in full
    live = finished(repo, "Encore visible", archived=False)
    assert 'href="/jobs?job=' in web().get(f"/jobs/{live}?back=q%3Dcsv").text                          # job not archived: unchanged
