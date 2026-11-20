"""Notifications (lot 5) : webhook générique, Microsoft Teams, Jira (notification seulement) et e-mail SMTP.

Un channel = un type, une configuration, ses secrets (chiffrés en base, en écriture seule, comme les clés d'API) et les
événements auxquels il s'abonne. Un événement est d'abord écrit dans une FILE D'ENVOI (table notifications), dans la même
transaction que ce qui l'a causé ; un fil d'envoi du dashboard le délivre ensuite, avec nouvelles tentatives. Un channel en
panne ne bloque donc jamais un job."""
import hashlib, hmac, json, logging, re, smtplib, ssl, threading, time
from email.message import EmailMessage
from urllib.parse import urlsplit
import httpx
from sqlalchemy import insert, select, update
from . import config, db, providers

log = logging.getLogger("fabrique.notify")
TRANSPORT = None   # tests : transport httpx simulé
SMTP = smtplib     # tests : module simulé

EVENTS = {"question": "Question posée par un agent", "ready": "PR prête à relire", "failed": "Job en échec",
          "merged": "Merge fait", "study": "Étude rendue", "repo_request": "Repo à créer (validation)", "repo_waiting": "Repo en attente"}
TYPES = {"webhook": "Webhook générique", "teams": "Microsoft Teams", "jira": "Jira", "smtp": "E-mail (SMTP)"}
SECRET_FIELDS = {"webhook": ("url", "hmac"), "teams": ("url",), "jira": ("token",), "smtp": ("password",)}
CONFIG_FIELDS = {"webhook": (), "teams": (), "jira": ("base_url", "auth", "user", "project", "create", "issue_type"),
                 "smtp": ("host", "port", "security", "user", "sender", "to")}
DELAYS = (30, 120, 600, 1800)   # nouvelles tentatives : 30 s, 2 min, 10 min, 30 min, puis abandon
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Invalid(Exception):
    pass


# ---------- canaux ----------

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
    """f : formulaire. Les champs secrets laissés vides gardent la valeur enregistrée (écriture seule)."""
    cid, typ, name = str(f.get("cid") or "").strip().lower(), str(f.get("type") or ""), str(f.get("name") or "").strip()[:80]
    if not cid and name:   # nouveau canal : l'identifiant interne est dérivé du nom (l'utilisateur ne saisit que le nom)
        import unicodedata
        base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).strip("-")[:32] or "channel"
        taken = {ch["id"] for ch in channels(c)}
        cid = next(x for x in [base] + [f"{base}-{i}" for i in range(2, 100)] if x not in taken)
    if typ not in TYPES or not _ID.match(cid) or not name:
        raise Invalid("Channel invalide : identifiant (minuscules, chiffres, tirets), type et nom.")
    old = next((ch for ch in channels(c) if ch["id"] == cid), None)
    if old and old["type"] != typ:
        raise Invalid("Le type d'un channel ne se change pas : crée un autre channel.")
    events = [e for e in (f.getlist("events") if hasattr(f, "getlist") else f.get("events", [])) if e in EVENTS]
    cfg = {k: str(f.get(k) or "").strip()[:300] for k in CONFIG_FIELDS[typ]}
    sec = _secrets(old) if old else {}
    # Nouvelle destination (adresse Jira, serveur SMTP) : les secrets enregistrés ne la suivent pas, il faut les ressaisir.
    dest = {"jira": ("base_url",), "smtp": ("host",)}.get(typ, ())
    if old and any((old.get("cfg") or {}).get(k) != cfg.get(k) for k in dest):
        sec = {}
    for k in SECRET_FIELDS[typ]:
        v = str(f.get(k) or "").strip()
        if v:
            sec[k] = v[:2000]
    if old and not sec and typ == "smtp" and _secrets(old).get("password") and cfg.get("user"):
        raise Invalid("Le serveur SMTP change : saisis à nouveau le mot de passe.")
    # validations par type
    if typ in ("webhook", "teams"):
        u = urlsplit(sec.get("url", ""))
        if u.scheme != "https" or not u.hostname:
            raise Invalid("Adresse du webhook invalide (https obligatoire).")
    if typ == "jira":
        if urlsplit(cfg["base_url"]).scheme != "https" or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", cfg["project"]) or not sec.get("token"):
            raise Invalid("Jira : adresse https, clé de projet (ex. FAB) et token obligatoires.")
        cfg["auth"] = cfg["auth"] if cfg["auth"] in ("cloud", "datacenter") else "cloud"
        if cfg["auth"] == "cloud" and "@" not in cfg["user"]:
            raise Invalid("Jira Cloud : indique l'e-mail du compte du token.")
        cfg["create"] = "1" if cfg["create"] == "1" else ""
        cfg["issue_type"] = cfg["issue_type"] or "Task"
    if typ == "smtp":
        if not cfg["host"] or not cfg["sender"] or "@" not in cfg["sender"] or not [a for a in re.split(r"[,;\s]+", cfg["to"]) if "@" in a]:
            raise Invalid("SMTP : serveur, expéditeur et au moins un destinataire.")
        cfg["security"] = cfg["security"] if cfg["security"] in ("none", "starttls", "ssl") else "starttls"
        cfg["port"] = str(int(cfg["port"])) if cfg["port"].isdigit() else {"ssl": "465", "starttls": "587", "none": "25"}[cfg["security"]]
    if sec and not config.SECRET_KEY:
        raise Invalid("FABRIQUE_SECRET_KEY n'est pas définie : impossible d'enregistrer les secrets du channel.")
    ch = {"id": cid, "type": typ, "name": name, "enabled": f.get("enabled") == "1", "events": events, "cfg": cfg,
          "secret": providers.seal(json.dumps(sec)) if sec else "", "secret_keys": {k: bool(sec.get(k)) for k in SECRET_FIELDS[typ]}}
    rest = [{k: v for k, v in x.items() if k != "has_secret"} for x in channels(c) if x["id"] != cid]
    db.set_setting(c, "channels", rest + [ch], by)
    db.log_audit(c, by, "Settings : channel de notification", target=cid, detail=f"{TYPES[typ]} · {', '.join(events) or 'aucun événement'}")


def remove(c, by, cid):
    db.set_setting(c, "channels", [{k: v for k, v in x.items() if k != "has_secret"} for x in channels(c) if x["id"] != cid], by)
    db.log_audit(c, by, "Settings : channel retiré", target=cid)


# ---------- événements ----------

def message(event, job=None, repo=None, detail=""):
    """Contenu commun à tous les channels : titre, texte, lien."""
    base = config.PUBLIC_URL
    ref, title = (job or {}).get("ref", ""), (job or {}).get("title", "")
    slug = (repo or {}).get("slug", "")
    url = f"{base}/jobs?job={ref}" if ref else (f"{base}/repos?repo={repo['id']}" if repo else base)
    t = {"question": f"{ref} attend ta réponse", "ready": f"{ref} : PR prête à relire", "failed": f"{ref} a échoué",
         "merged": f"{ref} mergé", "study": f"{ref} : étude rendue", "repo_request": f"Repo à créer : {slug}", "repo_waiting": f"Repo en attente : {slug}",
         "test": "Essai de notification de la Fabrique"}[event]
    text = (f"{title}\n{detail}" if title else detail).strip()
    return {"event": event, "title": t, "text": text[:1500], "url": url, "ref": ref, "job_title": title, "repo": slug,
            "pr_url": (job or {}).get("pr_url") or "", "at": time.time()}


def teams_text(t):
    """Texte inerte pour une carte Teams (TextBlock interprète un sous-ensemble de Markdown) : pas de lien ni de mise en forme."""
    return re.sub(r"([\\\[\]()*_`#<>|~])", r"\\\1", str(t or "")) or " "


def jira_text(t):
    """Texte inerte pour Jira (balisage wiki) : bloc {noformat}, sans lien, mention ni image."""
    return "{noformat}\n" + str(t or "").replace("{noformat}", "{ noformat }") + "\n{noformat}"


def emit(c, event, job=None, repo=None, detail=""):
    """Met l'événement en file pour chaque channel actif qui s'y abonne (dans la transaction de l'appelant)."""
    if event not in EVENTS:
        return 0
    t, n = time.time(), 0
    for ch in channels(c):
        if ch.get("enabled") and event in (ch.get("events") or []):
            c.execute(insert(db.notifications).values(channel=ch["id"], event=event, payload=message(event, job, repo, detail),
                                                      state="pending", attempts=0, last_error="", created_at=t, next_try=t))
            n += 1
    return n


# ---------- envoi ----------

def _client():
    return httpx.Client(timeout=20, transport=TRANSPORT)


def _jira_key(project, p):
    # Le ticket est choisi par le titre de la demande (écrit par le demandeur), jamais par un texte de l'agent.
    m = re.search(rf"\b{re.escape(project)}-\d+\b", p.get("job_title", ""))
    return m.group(0) if m else ""


def deliver(ch, p):
    """Envoie un message sur un channel ; lève une exception explicite en cas d'échec."""
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
                "actions": [{"type": "Action.OpenUrl", "title": "Ouvrir dans la Fabrique", "url": p["url"]}]
                + ([{"type": "Action.OpenUrl", "title": "Voir la PR", "url": p["pr_url"]}] if p.get("pr_url") else [])}
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
            # sinon : aucun ticket cité et création désactivée -> rien à faire (pas une erreur)
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
        raise Invalid(f"type de channel inconnu : {typ}")


def _err(e):
    if isinstance(e, httpx.HTTPStatusError):
        return f"le service a répondu {e.response.status_code}"
    return f"{type(e).__name__} : {str(e)[:200]}"


def send_test(cid):
    with db.tx() as c:
        ch = next((x for x in channels(c) if x["id"] == cid), None)
    if not ch:
        raise Invalid("Channel inconnu.")
    try:
        deliver(ch, message("test", detail="Si tu lis ce message, le channel fonctionne."))
    except Exception as e:
        raise Invalid(f"Échec de l'essai : {_err(e)}")


def flush(now=None):
    """Délivre ce qui est dû dans la file ; renvoie le nombre de messages envoyés."""
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
            vals = dict(state="failed", last_error="channel supprimé")
        else:
            try:
                deliver(ch, n["payload"])
                vals, sent = dict(state="sent", sent_at=time.time(), attempts=n["attempts"] + 1), sent + 1
            except Exception as e:
                a = n["attempts"] + 1
                vals = dict(attempts=a, last_error=_err(e), state="failed" if a > len(DELAYS) else "pending",
                            next_try=now + DELAYS[min(a, len(DELAYS)) - 1])
                log.warning("notification %s vers %s : %s", n["event"], n["channel"], vals["last_error"])
        with db.tx() as c:
            c.execute(update(db.notifications).where(db.notifications.c.id == n["id"]).values(**vals))
    return sent


def recent(limit=20):
    with db.tx() as c:
        return db.rows(c, select(db.notifications).order_by(db.notifications.c.id.desc()).limit(limit))


_stop = threading.Event()


def start():
    """Fil d'envoi du dashboard : vide la file toutes les 5 secondes."""
    def loop():
        while not _stop.wait(5):
            try:
                flush()
            except Exception:
                log.exception("file de notifications")
    threading.Thread(target=loop, name="notifications", daemon=True).start()


def stop():
    _stop.set()
