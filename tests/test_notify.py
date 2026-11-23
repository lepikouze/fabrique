"""Stage 5: notification channels (webhook, Teams, Jira, SMTP), sending file and new retries."""
import hashlib, hmac, json, time
import httpx
import pytest
from sqlalchemy import select
from app import config, db, jobs, notify
from .conftest import REQ, web
from .test_work import _to_pr, api, runner  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "une-cle-maitre-de-test")


@pytest.fixture
def sent(monkeypatch):
    got = []

    def fake(req):
        got.append(req)
        return httpx.Response(200 if "panne" not in str(req.url) else 503, json={})
    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(fake))
    return got


def add(cl, **f):
    base = {"cid": "c1", "type": "webhook", "name": "Channel", "enabled": "1", "events": ["question", "ready", "failed"]}
    return cl.post("/settings/channels", data={**base, **f})


def queue():
    with db.tx() as c:
        return db.rows(c, select(db.notifications).order_by(db.notifications.c.id))


def test_webhook_secrets_are_write_only_and_signed(sent):
    cl = web()
    r = add(cl, url="http://pas-https.test/h")
    assert "https required" in cl.get(r.headers["location"]).text
    add(cl, url="https://hook.test/h", hmac="s3cret-signature")
    page = cl.get("/settings?tab=notifications").text
    assert "hook.test" not in page and "s3cret" not in page and "Channel" in page
    with db.tx() as c:
        assert "hook.test" not in json.dumps(db.get_setting(c, "channels")[0])            # encoded in base
    add(cl, name="Renommé")                                                                # empty secrets: kept
    r = cl.post("/settings/channels/c1/test")
    assert "sent" in cl.get(r.headers["location"]).text
    req = sent[-1]
    assert str(req.url) == "https://hook.test/h" and json.loads(req.content)["event"] == "test"
    want = "sha256=" + hmac.new(b"s3cret-signature", req.content, hashlib.sha256).hexdigest()
    assert req.headers["X-Fabrique-Signature"] == want


def test_only_admins_manage_channels(sent):
    r = web("reviewer").post("/settings/channels", data={"cid": "x", "type": "webhook", "name": "x", "url": "https://h.test"})
    assert r.status_code in (302, 303)
    with db.tx() as c:
        assert not db.get_setting(c, "channels")[0]


def test_events_are_queued_per_subscription_and_delivered(runner, repo, sent):  # noqa: F811
    cl = web()
    add(cl, cid="teams", type="teams", name="Équipe", url="https://teams.test/wf", events=["ready"])
    add(cl, cid="tout", type="webhook", name="Tout", url="https://hook.test/h", events=["question", "ready", "failed"])
    add(cl, cid="pause", type="webhook", name="Pause", url="https://hook.test/p", events=["question"], enabled="")
    ref = jobs.create(REQ, "FAB-1 Un titre", "B", "", repo)
    a = api(runner)
    _to_pr(a, ref, ["app.py"])
    a.post("/api/runner/claim")
    q = [(n["channel"], n["event"]) for n in queue()]
    assert ("teams", "ready") in q and ("tout", "ready") in q and not any(ch == "pause" for ch, _ in q)
    assert notify.flush() == len(q)
    card = next(json.loads(r.content) for r in sent if r.url.host == "teams.test")
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard" and ref in content["body"][0]["text"]
    assert any(x["url"] == "https://git.test/x/pulls/7" for x in content["actions"])
    assert all(n["state"] == "sent" for n in queue())


def test_failed_delivery_retries_with_backoff_then_gives_up(sent):
    cl = web()
    add(cl, cid="panne", url="https://panne.test/h", events=["repo_request"])
    with db.tx() as c:
        assert notify.emit(c, "repo_request", repo={"id": 1, "slug": "fabrique/x"}) == 1
    t = time.time() + 1
    notify.flush(t)
    n = queue()[0]
    assert n["state"] == "pending" and n["attempts"] == 1 and "503" in n["last_error"] and n["next_try"] == t + 30
    assert notify.flush(t + 10) == 0 and queue()[0]["attempts"] == 1                       # not yet due
    for d in notify.DELAYS:
        t += d
        notify.flush(t)
    assert queue()[0]["state"] == "failed" and queue()[0]["attempts"] == len(notify.DELAYS) + 1
    assert "failed" in cl.get("/settings?tab=notifications").text


def test_jira_comments_on_cited_issue_or_creates_one(sent):
    cl = web()
    r = add(cl, cid="jira", type="jira", name="Jira", base_url="https://j.test", project="FAB", auth="cloud", user="pas-un-mail", token="t0k")
    assert "e-mail" in cl.get(r.headers["location"]).text
    add(cl, cid="jira", type="jira", name="Jira", base_url="https://j.test", project="FAB", auth="cloud", user="bot@ex.fr", token="t0k")
    ch = chan("jira")
    notify.deliver(ch, notify.message("ready", job={"ref": "FAB-0007", "title": "Corrige FAB-42"}))
    assert sent[-1].url.path == "/rest/api/2/issue/FAB-42/comment" and sent[-1].headers["authorization"].startswith("Basic ")
    n = len(sent)
    notify.deliver(ch, notify.message("ready", job={"ref": "X", "title": "Sans ticket"}))
    assert len(sent) == n                                                                  # creation disabled: nothing
    add(cl, cid="jira", type="jira", name="Jira", base_url="https://j.test", project="FAB", auth="datacenter", user="", create="1")
    ch = chan("jira")
    notify.deliver(ch, notify.message("failed", job={"ref": "X", "title": "Sans ticket"}))
    body = json.loads(sent[-1].content)
    assert sent[-1].url.path == "/rest/api/2/issue" and body["fields"]["project"]["key"] == "FAB"
    assert sent[-1].headers["authorization"] == "Bearer t0k"                                # token kept (write-only)


def chan(cid):
    with db.tx() as c:
        return next(x for x in notify.channels(c) if x["id"] == cid)


class FakeSMTP:
    log = []

    def __init__(self, host, port, timeout=None, context=None):
        self.log.append(("connect", host, port, "ssl" if context else "plain"))

    def starttls(self, context=None):
        self.log.append(("starttls",))

    def login(self, u, p):
        self.log.append(("login", u, p))

    def send_message(self, m):
        body = m.get_body(preferencelist=("plain",)) or m
        self.log.append(("send", m["To"], m["Subject"], body.get_content()))

    def quit(self):
        pass


def test_smtp_starttls_login_and_recipients(monkeypatch):
    FakeSMTP.log = []
    monkeypatch.setattr(notify, "SMTP", type("M", (), {"SMTP": FakeSMTP, "SMTP_SSL": FakeSMTP}))
    cl = web()
    add(cl, cid="mail", type="smtp", name="Mail", host="smtp.ex.fr", security="starttls", user="bot", password="pw",
        sender="fab@ex.fr", to="a@ex.fr; b@ex.fr", events=["failed"])
    r = cl.post("/settings/channels/mail/test")
    assert "sent" in cl.get(r.headers["location"]).text
    assert FakeSMTP.log[0] == ("connect", "smtp.ex.fr", 587, "plain") and ("starttls",) in FakeSMTP.log
    assert ("login", "bot", "pw") in FakeSMTP.log
    send = FakeSMTP.log[-1]
    assert send[1] == "a@ex.fr, b@ex.fr" and send[2].startswith("[Fabrique]") and config.PUBLIC_URL in send[3]
    assert "pw" not in cl.get("/settings?tab=notifications&chan=mail").text


def test_stored_secrets_do_not_follow_a_new_destination(sent, monkeypatch):
    monkeypatch.setattr(notify, "SMTP", type("M", (), {"SMTP": FakeSMTP, "SMTP_SSL": FakeSMTP}))
    cl = web()
    add(cl, cid="jira", type="jira", name="Jira", base_url="https://j.test", project="FAB", auth="datacenter", token="t0k")
    add(cl, cid="jira", type="jira", name="Jira bis", base_url="https://j.test", project="FAB", auth="datacenter")   # same address: token kept
    assert chan("jira")["name"] == "Jira bis"
    r = add(cl, cid="jira", type="jira", name="Jira ter", base_url="https://ailleurs.test", project="FAB", auth="datacenter")
    assert "token are required" in cl.get(r.headers["location"]).text and chan("jira")["cfg"]["base_url"] == "https://j.test"
    smtp = dict(cid="mail", type="smtp", name="Mail", security="starttls", user="bot", sender="fab@ex.fr", to="a@ex.fr")
    add(cl, host="smtp.ex.fr", password="pw", **smtp)
    add(cl, host="smtp.ex.fr", **dict(smtp, name="Mail bis"))                             # same server: password kept
    assert chan("mail")["name"] == "Mail bis"
    r = add(cl, host="smtp.ailleurs.fr", **smtp)
    assert "enter the password again" in cl.get(r.headers["location"]).text and chan("mail")["cfg"]["host"] == "smtp.ex.fr"


def test_settings_page_renders_edit_form(sent):
    cl = web()
    add(cl, cid="t", type="teams", name="team", url="https://teams.test/wf")
    page = cl.get("/settings?tab=notifications&chan=t").text
    assert "Edit “team”" in page and "set: leave empty to keep" in page and "teams.test" not in page
