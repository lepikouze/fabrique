"""Live updates: what the page needs from the server (a counter that moves with every event, fragments that change),
and the rules of the page's own protocol between tabs. The browser side is exercised by tools/live_check.py."""
import os, re
from app import db, jobs, work
from .conftest import ADMIN, REQ, web

JS = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "app.js"), encoding="utf-8").read()


def test_every_runner_event_moves_the_counter_and_changes_the_fragments(repo):
    ref = jobs.create(REQ, "Suivre un job", "voir avancer", "ça avance", repo)
    tok = work.create_runner("runner-live", "container", "alice")
    cl = web("admin")
    r = web(); r.headers["Authorization"] = f"Bearer {tok}"
    assert r.post("/api/runner/claim", json={}).status_code == 200
    seen, pages, panels = [jobs.last_event_id()], [cl.get("/jobs").text], [cl.get(f"/jobs/{ref}/panel").text]
    for ev in ({"type": "stage", "stage": "triage", "summary": "tri"}, {"type": "log", "kind": "bash", "message": "ls"},
               {"type": "stage", "stage": "implementing", "summary": "code"}, {"type": "log", "kind": "edit", "message": "a.py"}):
        assert r.post(f"/api/runner/jobs/{ref}/event", json=ev).status_code == 200
        seen.append(jobs.last_event_id()); pages.append(cl.get("/jobs").text); panels.append(cl.get(f"/jobs/{ref}/panel").text)
    assert seen == sorted(set(seen)) and len(seen) == 5                       # the stream sends one "change" per event
    assert len(set(panels)) == 5                                               # the open job shows every one of them
    assert pages[0] != pages[1] and pages[1] != pages[3]                       # the list follows the stages


def test_a_tab_never_depends_blindly_on_another_tab():
    """0.8.1 and before: a tab left open with a lost session (or an older version of the page) kept the "leader" lock and
    relayed nothing; every other tab stayed frozen behind a green light. The rules that prevent it must stay in the page."""
    live = JS[JS.index("// Real time."):]
    assert '"fabrique-live-2"' in live and '"fabrique-live"' not in live      # older pages no longer count
    assert 'postMessage("hello")' in live and re.search(r"if \(heard < asked\) \{ alone\(\); return; \}", live)
    assert re.search(r"setInterval\(function \(\) \{[^}]*heard < asked[\s\S]{0,200}\}, 3000\)", live)   # asked again and again, 3 s to answer
    assert re.search(r'if \(m\.data === "off"\) \{ alone\(\); return; \}', live)                          # a leader in trouble is not waited for
    assert re.search(r"multiplexed = /\^h\[23\]/", live) and "if (!multiplexed &&" in live              # HTTP/2 and 3: one connection per tab
    assert "live.dataset.role" in live and "live.title" in live                                          # the indicator tells its state
