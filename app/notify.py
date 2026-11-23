"""Notifications (phase 5): generic webhook, Microsoft Teams, Jira (notification only) and SMTP email.

One channel = one type, one configuration, its secrets (encrypted in the database, write-only, like API keys) and the
events it subscribes to. An event is first written to an OUTGOING QUEUE (notifications table), in the same
transaction as what caused it; an outgoing thread from the dashboard then delivers it, with new retries. A channel
in failure therefore never blocks a job."""
from .i18n import M
import hashlib, hmac, json, logging, re, smtplib, ssl, threading, time
from email.message import EmailMessage
from urllib.parse import urlsplit
import httpx
from sqlalchemy import insert, select, update
from . import config, db, providers, i18n

log = logging.getLogger("fabrique.notify")
TRANSPORT = None   # tests: simulated httpx transport
SMTP = smtplib     # tests: simulated module

EVENTS = {"question": M("Question asked by an agent"), "ready": M("PR ready for review"), "failed": M("Job failed"),
          "merged": M("Merge done"), "study": M("Study delivered"), "repo_request": M("Repo to create (approval)"), "repo_waiting": M("Repo waiting")}
TYPES = {"webhook": M("Generic webhook"), "teams": "Microsoft Teams", "jira": "Jira", "smtp": "E-mail (SMTP)"}
SECRET_FIELDS = {"webhook": ("url", "hmac"), "teams": ("url",), "jira": ("token",), "smtp": ("password",)}
CONFIG_FIELDS = {"webhook": (), "teams": (), "jira": ("base_url", "auth", "user", "project", "create", "issue_type"),
                 "smtp": ("host", "port", "security", "user", "sender", "to")}
DELAYS = (30, 120, 600, 1800)   # retries: 30 s, 2 min, 10 min, 30 min, then give up
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Invalid(Exception):
    pass


# ---------- channels ----------

def channels(c):
    return [dict(ch, has_secret={k: bool(v) for k, v in (ch.get("secret_keys") or {}).items()})
            for ch in (db.get_setting(c, "channels")[0] or [])]


def _secrets(ch):
    raw = providers.unseal(ch.get("secret") or "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def save(c, by, f):
    """f: form. Secret fields left empty keep the stored value (write-only)."""
    cid, typ, name = str(f.get("cid") or "").strip().lower(), str(f.get("type") or ""), str(f.get("name") or "").strip()[:80]
    if not cid and name:   # new channel: the internal identifier is derived from the name (the user only enters the name)
        import unicodedata
        base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).strip("-")[:32] or "channel"
        taken = {ch["id"] for ch in channels(c)}
        cid = next(x for x in [base] + [f"{base}-{i}" for i in range(2, 100)] if x not in taken)
    if typ not in TYPES or not _ID.match(cid) or not name:
        raise Invalid(M("Invalid channel: identifier (lowercase letters, digits, hyphens), type and name."))
    old = next((ch for ch in channels(c) if ch["id"] == cid), None)
    if old and old["type"] != typ:
        raise Invalid(M("A channel's type cannot be changed: create another channel."))
    events = [e for e in (f.getlist("events") if hasattr(f, "getlist") else f.get("events", [])) if e in EVENTS]
    cfg = {k: str(f.get(k) or "").strip()[:300] for k in CONFIG_FIELDS[typ]}
    sec = _secrets(old) if old else {}
    # New destination (Jira address, SMTP server): stored secrets do not follow it, they must be re-entered.
    dest = {"jira": ("base_url",), "smtp": ("host",)}.get(typ, ())
    if old and any((old.get("cfg") or {}).get(k) != cfg.get(k) for k in dest):
        sec = {}
    for k in SECRET_FIELDS[typ]:
        v = str(f.get(k) or "").strip()
        if v:
            sec[k] = v[:2000]
    if old and not sec and typ == "smtp" and _secrets(old).get("password") and cfg.get("user"):
        raise Invalid(M("The SMTP server changes: enter the password again."))
    # validations per type
    if typ in ("webhook", "teams"):
        u = urlsplit(sec.get("url", ""))
        if u.scheme != "https" or not u.hostname:
            raise Invalid(M("Invalid webhook address (https required)."))
    if typ == "jira":
        if urlsplit(cfg["base_url"]).scheme != "https" or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", cfg["project"]) or not sec.get("token"):
            raise Invalid(M("Jira: https address, project key (e.g. FAB) and token are required."))
        cfg["auth"] = cfg["auth"] if cfg["auth"] in ("cloud", "datacenter") else "cloud"
        if cfg["auth"] == "cloud" and "@" not in cfg["user"]:
            raise Invalid(M("Jira Cloud: give the e-mail of the token's account."))
        cfg["create"] = "1" if cfg["create"] == "1" else ""
        cfg["issue_type"] = cfg["issue_type"] or "Task"
    if typ == "smtp":
        if not cfg["host"] or not cfg["sender"] or "@" not in cfg["sender"] or not [a for a in re.split(r"[,;\s]+", cfg["to"]) if "@" in a]:
            raise Invalid(M("SMTP: server, sender and at least one recipient."))
        cfg["security"] = cfg["security"] if cfg["security"] in ("none", "starttls", "ssl") else "starttls"
        cfg["port"] = str(int(cfg["port"])) if cfg["port"].isdigit() else {"ssl": "465", "starttls": "587", "none": "25"}[cfg["security"]]
    if sec and not config.SECRET_KEY:
        raise Invalid(M("FABRIQUE_SECRET_KEY is not set: the channel's secrets cannot be stored."))
    ch = {"id": cid, "type": typ, "name": name, "enabled": f.get("enabled") == "1", "events": events, "cfg": cfg,
          "secret": providers.seal(json.dumps(sec)) if sec else "", "secret_keys": {k: bool(sec.get(k)) for k in SECRET_FIELDS[typ]}}
    rest = [{k: v for k, v in x.items() if k != "has_secret"} for x in channels(c) if x["id"] != cid]
    db.set_setting(c, "channels", rest + [ch], by)
    db.log_audit(c, by, M("Settings: notification channel"), target=cid, detail=f"{TYPES[typ]} · {', '.join(events) or M('no event')}")


def remove(c, by, cid):
    db.set_setting(c, "channels", [{k: v for k, v in x.items() if k != "has_secret"} for x in channels(c) if x["id"] != cid], by)
    db.log_audit(c, by, M("Settings: channel removed"), target=cid)


# ---------- events ----------

def message(event, job=None, repo=None, detail=""):
    """Content common to all channels: title, text, link."""
    base = config.PUBLIC_URL
    ref, title = (job or {}).get("ref", ""), (job or {}).get("title", "")
    slug = (repo or {}).get("slug", "")
    url = f"{base}/jobs?job={ref}" if ref else (f"{base}/repos?repo={repo['id']}" if repo else base)
    t = {"question": M("{ref} is waiting for your answer", ref=ref), "ready": M("{ref}: PR ready for review", ref=ref), "failed": M("{ref} failed", ref=ref),
         "merged": M("{ref} merged", ref=ref), "study": M("{ref}: study delivered", ref=ref), "repo_request": M("Repo to create: {slug}", slug=slug), "repo_waiting": M("Repo waiting: {slug}", slug=slug),
         "test": M("Fabrique notification test")}[event]
    text = (f"{title}\n{detail}" if title else detail).strip()
    return {"event": event, "title": t, "text": text[:1500], "url": url, "ref": ref, "job_title": title, "repo": slug,
            "pr_url": (job or {}).get("pr_url") or "", "at": time.time()}


def written(c, payload, repo=None):
    """A notification is sent to other readers: it is written in the language of the written texts (setting of
    the instance, or the repo), not in that of the person who triggered the event."""
    lang = i18n.job_lang(c, repo)
    if lang != i18n.DEFAULT:
        payload = dict(payload, title=i18n.tr(payload["title"], lang), text=i18n.tr(payload["text"], lang))
    return dict(payload, lang=lang)


def teams_text(t):
    """Inert text for a Teams card (TextBlock interprets a subset of Markdown): no links or formatting."""
    return re.sub(r"([\\\[\]()*_`#<>|~])", r"\\\1", str(t or "")) or " "


def jira_text(t):
    """Inert text for Jira (wiki markup): {noformat} block, no links, mentions or images."""
    return "{noformat}\n" + str(t or "").replace("{noformat}", "{ noformat }") + "\n{noformat}"


def emit(c, event, job=None, repo=None, detail=""):
    """Queues the event for each active channel subscribed to it (within the caller's transaction)."""
    if event not in EVENTS:
        return 0
    t, n = time.time(), 0
    for ch in channels(c):
        if ch.get("enabled") and event in (ch.get("events") or []):
            c.execute(insert(db.notifications).values(channel=ch["id"], event=event, payload=written(c, message(event, job, repo, detail), repo),
                                                      state="pending", attempts=0, last_error="", created_at=t, next_try=t))
            n += 1
    return n


# ---------- sending ----------

def _client():
    return httpx.Client(timeout=20, transport=TRANSPORT)


def _jira_key(project, p):
    # The ticket is chosen by the request title (written by the requester), never by text from the agent.
    m = re.search(rf"\b{re.escape(project)}-\d+\b", p.get("job_title", ""))
    return m.group(0) if m else ""


def deliver(ch, p):
    """Sends a message to a channel; raises an explicit exception in case of failure."""
    sec, cfg, typ = _secrets(ch), ch.get("cfg") or {}, ch["type"]
    if typ == "webhook":
        body = json.dumps({"source": "fabrique", **p}, ensure_ascii=False).encode()
        h = {"Content-Type": "application/json"}
        if sec.get("hmac"):
            h["X-Fabrique-Signature"] = "sha256=" + hmac.new(sec["hmac"].encode(), body, hashlib.sha256).hexdigest()
        with _client() as cl:
            cl.post(sec["url"], content=body, headers=h).raise_for_status()
    elif typ == "teams":
        card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
                "body": [{"type": "TextBlock", "text": p["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
                         {"type": "TextBlock", "text": teams_text(p["text"]), "wrap": True}],
                "actions": [{"type": "Action.OpenUrl", "title": i18n.tr(M("Open in Fabrique"), p.get("lang")), "url": p["url"]}]
                + ([{"type": "Action.OpenUrl", "title": i18n.tr(M("See the PR"), p.get("lang")), "url": p["pr_url"]}] if p.get("pr_url") else [])}
        with _client() as cl:
            cl.post(sec["url"], json={"type": "message", "attachments": [
                {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}).raise_for_status()
    elif typ == "jira":
        base, project = cfg["base_url"].rstrip("/"), cfg["project"]
        auth = (cfg["user"], sec["token"]) if cfg.get("auth") == "cloud" else None
        h = {} if auth else {"Authorization": f"Bearer {sec['token']}"}
        body = f"{jira_text(p['title'])}\n{jira_text(p['text'])}\n\n{p['url']}"
        key = _jira_key(project, p)
        with _client() as cl:
            if key:
                cl.post(f"{base}/rest/api/2/issue/{key}/comment", json={"body": body}, auth=auth, headers=h).raise_for_status()
            elif cfg.get("create") == "1":
                cl.post(f"{base}/rest/api/2/issue", auth=auth, headers=h, json={"fields": {
                    "project": {"key": project}, "summary": p["title"][:250], "description": body,
                    "issuetype": {"name": cfg.get("issue_type") or "Task"}}}).raise_for_status()
            # otherwise: no ticket cited and creation disabled -> nothing to do (not an error)
    elif typ == "smtp":
        to = [a for a in re.split(r"[,;\s]+", cfg["to"]) if "@" in a]
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = f"[Fabrique] {p['title']}", cfg["sender"], ", ".join(to)
        msg.set_content(f"{p['title']}\n\n{p['text']}\n\n{p['url']}" + (f"\nPR : {p['pr_url']}" if p.get("pr_url") else ""))
        port, sec_mode = int(cfg.get("port") or 587), cfg.get("security", "starttls")
        ctx = ssl.create_default_context()
        if sec_mode == "ssl":
            s = SMTP.SMTP_SSL(cfg["host"], port, timeout=20, context=ctx)
        else:
            s = SMTP.SMTP(cfg["host"], port, timeout=20)
        try:
            if sec_mode == "starttls":
                s.starttls(context=ctx)
            if cfg.get("user"):
                s.login(cfg["user"], sec.get("password", ""))
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    else:
        raise Invalid(M("unknown channel type: {typ}", typ=typ))


def _err(e):
    if isinstance(e, httpx.HTTPStatusError):
        return M("the service answered {status_code}", status_code=e.response.status_code)
    return f"{type(e).__name__} : {str(e)[:200]}"


def send_test(cid):
    with db.tx() as c:
        ch = next((x for x in channels(c) if x["id"] == cid), None)
    if not ch:
        raise Invalid(M("Unknown channel."))
    try:
        deliver(ch, message("test", detail=M("If you are reading this message, the channel works.")))
    except Exception as e:
        raise Invalid(M("Test failed: {e}", e=_err(e)))


def flush(now=None):
    """Delivers what is owed in the queue; returns the number of messages sent."""
    now = now or time.time()
    sent = 0
    with db.tx() as c:
        chs = {ch["id"]: ch for ch in channels(c)}
        due = db.rows(c, select(db.notifications).where(db.notifications.c.state == "pending", db.notifications.c.next_try <= now)
                      .order_by(db.notifications.c.id).limit(50))
    for n in due:
        ch = chs.get(n["channel"])
        vals = {}
        if not ch:
            vals = dict(state="failed", last_error=M("channel deleted"))
        else:
            try:
                deliver(ch, n["payload"])
                vals, sent = dict(state="sent", sent_at=time.time(), attempts=n["attempts"] + 1), sent + 1
            except Exception as e:
                a = n["attempts"] + 1
                vals = dict(attempts=a, last_error=_err(e), state="failed" if a > len(DELAYS) else "pending",
                            next_try=now + DELAYS[min(a, len(DELAYS)) - 1])
                log.warning("notification %s to %s: %s", n["event"], n["channel"], vals["last_error"])
        with db.tx() as c:
            c.execute(update(db.notifications).where(db.notifications.c.id == n["id"]).values(**vals))
    return sent


def recent(limit=20):
    with db.tx() as c:
        return db.rows(c, select(db.notifications).order_by(db.notifications.c.id.desc()).limit(limit))


_stop = threading.Event()


def start():
    """Dashboard sending thread: drains the queue every 5 seconds."""
    def loop():
        while not _stop.wait(5):
            try:
                flush()
            except Exception:
                log.exception("notification queue")
    threading.Thread(target=loop, name="notifications", daemon=True).start()


def stop():
    _stop.set()
