"""Fabrique — dashboard (plan de check) : inbox, jobs (tableau / liste, dashboard latéral agrandissable),
nouvelle demande, runners, repos, settings, activité. Temps réel par SSE (/events/stream)."""
import asyncio, datetime as dt, json, logging, os, re, time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, quote, urlsplit
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update
from . import auth, config, db, demo, forges, jobs, notify, profiles, providers, work

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = os.path.dirname(__file__)
MOIS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")


@asynccontextmanager
async def lifespan(app):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    db.migrate()
    auth.log_startup()
    work.bootstrap_runner()
    if config.DEMO:
        demo.seed()
        if os.environ.get("FABRIQUE_SIMULATOR", "1") == "1":
            demo.start()
    if os.environ.get("FABRIQUE_NOTIFY", "1") == "1":
        notify.start()
    yield
    demo.stop()
    notify.stop()


app = FastAPI(title="Fabrique", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


def hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def quand(ts):
    """14:03 aujourd'hui, sinon « 9 nov. »."""
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == dt.date.today() else f"{d.day} {MOIS[d.month - 1]}"


def depuis(ts):
    """Âge lisible : « à l'instant », « depuis 12 min », « depuis 3 h », « depuis 2 j »."""
    if not ts:
        return "—"
    s = max(0, time.time() - ts)
    if s < 90:
        return "à l'instant"
    if s < 3600:
        return f"depuis {int(s // 60)} min"
    if s < 86400:
        return f"depuis {int(s // 3600)} h"
    return f"depuis {int(s // 86400)} j"


def date_longue(ts):
    d = dt.datetime.fromtimestamp(ts)
    return f"{d.day} {MOIS[d.month - 1]} {d.year} à {d:%H:%M}"


def il_y_a(ts):
    s = max(0, time.time() - (ts or 0))
    return "à l'instant" if s < 60 else f"il y a {int(s // 60)} min" if s < 3600 else f"il y a {int(s // 3600)} h" if s < 86400 else f"il y a {int(s // 86400)} j"


def tokens(n):
    n = n or 0
    return (f"{n / 1e6:.1f}".replace(".", ",") + "\u202fM") if n >= 1e6 else f"{n / 1e3:.0f}\u202fk" if n >= 1e3 else str(n)


def money(x):
    return f"{x or 0:.2f}".replace(".", ",") + "\u202f$"


def duree(ms):
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.0f} s" if ms >= 10000 else f"{ms / 1000:.1f} s"


templates.env.filters.update(hm=hm, hms=hms, quand=quand, depuis=depuis, date_longue=date_longue, il_y_a=il_y_a, tokens=tokens, money=money, duree=duree)
def _git_name(repo):
    """Nom du provider Git d'un repo (« GitLab interne »), pour l'affichage."""
    with db.tx() as c:
        return forges.label(c, dict(repo))


templates.env.globals.update(GIT_NAME=_git_name, CAN_DECIDE_REPO=jobs.can_decide_repo, HARNESSES_FOR=providers.harnesses_for, READY=jobs.READY, TO_CHECK=jobs.to_check, SECURITY_WARN=jobs.security_warn, DELETE_PHRASE=jobs.delete_phrase, REVIEW_EFFORT=jobs.review_effort, CHANGES_URL=jobs.changes_url, VERSION=config.VERSION, REVIEW_ROLES=work.ROLES, REVIEW_VERDICTS=work.VERDICTS, STAGE=jobs.STAGE, HARNESSES=jobs.HARNESSES, POLICIES=jobs.POLICIES, ROLE_LABEL=auth.ROLE_LABEL,
                             can=auth.can, PROVIDERS={"forgejo": "Forgejo", "gitea": "Gitea", "gitlab": "GitLab", "github": "GitHub"},
                             ONB={"pending": ("Onboarding", "var(--blue)"), "analyzing": ("Onboarding", "var(--blue)"),
                                  "requested": ("Création à valider", "var(--amber)"), "creating": ("Création", "var(--blue)"),
                                  "waiting": ("En attente du repo", "var(--amber)"), "empty": ("Repo vide", "var(--amber)"),
                                  "blank": ("Vierge", "var(--blue)"),
                                  "ready": ("Prêt", "var(--ok)"), "failed": ("Échec", "var(--bad)")})
auth.templates = templates

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}
app.add_middleware(auth.AuthLayer)
app.include_router(auth.router)


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    """Anti-CSRF sans token : une requête qui modifie l'état doit venir du dashboard lui-même."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        host = request.headers.get("host", "")
        sfs = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin") or request.headers.get("referer")
        if sfs in ("cross-site", "same-site") or (origin and urlsplit(origin).netloc != host):
            return JSONResponse({"error": "requête d'une autre origine refusée"}, status_code=403)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if (o := auth.provider_origin()) and resp.headers.get("content-security-policy") == SECURITY_HEADERS["Content-Security-Policy"]:
        resp.headers["content-security-policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace("form-action 'self'", f"form-action 'self' {o}")
    return resp


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
FLASH = "fabrique_flash"


def back(url, ok=None, err=None):
    """Redirection après un formulaire, avec un message (cookie de courte durée, lu une fois)."""
    r = RedirectResponse(url, status_code=303)
    if ok or err:
        r.set_cookie(FLASH, quote(json.dumps({"ok": ok, "err": err}, ensure_ascii=False)), max_age=30, httponly=True, samesite="lax",
                     secure=config.PUBLIC_URL.startswith("https://"))
    return r


def _flash(request):
    raw = request.cookies.get(FLASH)
    if not raw:
        return None
    try:
        from urllib.parse import unquote
        d = json.loads(unquote(raw))
        return {"ok": str(d.get("ok") or "")[:200], "err": str(d.get("err") or "")[:200]}
    except (ValueError, AttributeError):
        return None


class PendingAccess(Exception):
    """Compte connu mais sans rôle (en attente d'un admin) : seule la page « Mon accès » est ouverte."""


def user_of(request, pending_ok=False):
    u = auth.who(request)
    if not u:
        raise PermissionError
    if u["role"] not in auth.ROLES and not pending_ok:
        raise PendingAccess
    return u


def start_steps():
    """Setup d'une installation neuve : ce qui reste à faire avant le premier job (pour les admins)."""
    with db.tx() as c:
        runners = db.rows(c, select(db.runners.c.online, db.runners.c.token_hash))
        provs = [p for p in providers.listing(c) if p.get("has_key") or p.get("type") == "compatible"]
        models = db.get_setting(c, "models")[0] or []
        model_ok = bool(models) and not providers.default_problem(c)
        git = db.get_setting(c, "git")[0] or []
        repos = db.rows(c, select(db.repos.c.onboarding, db.repos.c.recipe))
    live = [r for r in runners if r["token_hash"]]
    return [
        ("Compte administrateur", True, "/settings?tab=users", "Créé au premier démarrage ; c'est aussi le compte de secours."),
        ("Un runner en ligne", any(r["online"] for r in live), "/runners", "Le runner intégré s'enregistre seul ; il apparaît en ligne en moins d'une minute."),
        ("Un modèle par défaut", bool(provs) and model_ok, "/settings?tab=agents",
         "Ajoute un provider (Anthropic, OpenAI ou serveur local), sa clé, coche les modèles à proposer, puis choisis l'agent et le modèle par défaut."),
        ("Un provider Git", any(g.get("token") for g in git) or bool(repos), "/settings?tab=git", "Adresse de la forge et token d'un compte robot dédié."),
        ("Un premier repo", any(r["onboarding"] in ("ready", "blank") or r["recipe"] is not None for r in repos), "/repos", "Ajoute un repo : son onboarding démarre seul, puis merge la PR de recipe."),
    ]


def render(request, name, page, status=200, **kw):
    u = user_of(request)
    todo = 0
    if u.get("role") == "admin" and not config.DEMO:
        todo = sum(1 for s in start_steps() if not s[1])
    access_request = None
    if u.get("role") not in ("admin", None) and auth.mode() == "on":   # demande de rôle en cours, pour la fenêtre du menu
        with db.tx() as c:
            me = db.one(c, select(db.users.c.requested_role, db.users.c.request_note).where(db.users.c.username == u.get("user", "")))
        if me and me["requested_role"]:
            access_request = {"role": me["requested_role"], "note": me["request_note"]}
    ctx = dict(user=u, page=page, mode=auth.mode(), access_request=access_request, demo=jobs.demo_visible(), demo_mode=config.DEMO, flash=_flash(request),
               view_prefs=view_prefs(request, u),
               counters=jobs.counters(u), start_todo=todo, **kw)
    resp = templates.TemplateResponse(request, name, ctx, status_code=status)
    if ctx["flash"]:
        resp.delete_cookie(FLASH)
    return resp


def forbidden(request, what="cette page"):
    return render(request, "denied.html", "denied", status=403, what=what)


PREFS = "fabrique_jobs"   # sans compte (mode ouvert) : la préférence d'affichage reste dans le navigateur
PREF_CHOICES = {"board": ("stages", "compact"), "list": ("grouped", "flat")}


def _clean_prefs(got):
    return {k: got.get(k) if got.get(k) in v else v[0] for k, v in PREF_CHOICES.items()}


def view_prefs(request, u=None):
    """{'board': 'stages' | 'compact', 'list': 'grouped' | 'flat'} ; par défaut une colonne et un groupe par étape.
    La préférence est gardée sur le compte : elle suit la personne d'un poste à l'autre."""
    u = u or auth.who(request) or {}
    if not u.get("local") and u.get("user"):
        with db.tx() as c:
            row = db.one(c, select(db.users.c.prefs).where(db.users.c.username == u["user"]))
        if row is not None:
            return _clean_prefs((row["prefs"] or {}).get("jobs") or {})
    return _clean_prefs(dict(p.split(":", 1) for p in (request.cookies.get(PREFS) or "").split(",") if ":" in p))


def save_view_prefs(request, u, cur, resp):
    if not u.get("local") and u.get("user"):
        with db.tx() as c:
            row = db.one(c, select(db.users.c.id, db.users.c.prefs).where(db.users.c.username == u["user"]))
            if row:
                c.execute(update(db.users).where(db.users.c.id == row["id"]).values(prefs=dict(row["prefs"] or {}, jobs=cur)))
                return
    resp.set_cookie(PREFS, ",".join(f"{k}:{v}" for k, v in cur.items()), max_age=365 * 86400, httponly=True, samesite="lax",
                    secure=config.PUBLIC_URL.startswith("https://"))


def _safe_back(request, fallback):
    ref = request.headers.get("referer") or ""
    p = urlsplit(ref)
    return auth.safe_next(p.path + ("?" + p.query if p.query else "")) if p.netloc == request.headers.get("host") else fallback


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def home():
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, view: str = "board", repo: int = 0, job: str = "", archived: int = 0, f: str = ""):
    u = user_of(request)
    view = "list" if view == "list" else "board"
    prefs = view_prefs(request, u)
    compact, flat = prefs["board"] == "compact" and view == "board", prefs["list"] == "flat" and view == "list"
    js = jobs.listing(u, repo=repo or None, archived=bool(archived))
    detail = jobs.get(u, job) if job else None
    with db.tx() as c:
        repo_list = db.rows(c, select(db.repos).order_by(db.repos.c.name))
    fam = f if f in jobs.FAMILY_OF.values() else ""
    if flat:   # liste à plat : une ligne par job, par dernière activité ; filtre par famille d'étapes
        shown = [j for j in js if not fam or jobs.FAMILY_OF.get(j["stage"]) == fam]
        groups = [(None, shown)]
    else:
        groups = jobs.by_family(js) if compact else jobs.by_stage(js)
    return render(request, "jobs.html", "jobs", view=view, groups=groups, js=js, repo=repo, repos=repo_list, compact=compact, flat=flat,
                  fam=fam, families=jobs.FAMILIES, detail=detail, archived=bool(archived), archived_n=jobs.archived_count(u), sel=job)


@app.post("/prefs/jobs")
def prefs_jobs(request: Request, board: str = Form(""), list_: str = Form("", alias="list")):
    """Chacun choisit sa vue des jobs (menu utilisateur) ; rien n'est imposé aux autres."""
    u = user_of(request)
    cur = view_prefs(request, u)
    if board in PREF_CHOICES["board"]:
        cur["board"] = board
    if list_ in PREF_CHOICES["list"]:
        cur["list"] = list_
    r = RedirectResponse(_safe_back(request, "/jobs"), status_code=303)
    save_view_prefs(request, u, cur, r)
    return r


@app.get("/jobs/{ref}", response_class=HTMLResponse)
def job_full(request: Request, ref: str, back: str = ""):
    u = user_of(request)
    j = jobs.get(u, ref)
    if not j:
        return render(request, "denied.html", "denied", status=404, what="ce job")
    # Venu des Archives avec une recherche en cours : on la garde pour y revenir (seulement ses paramètres connus).
    q = parse_qs(back[:600])
    back_qs = urlencode({k: q[k][0][:200] for k in ("q", "repo", "state", "regex", "page") if q.get(k)})
    return render(request, "job_full.html", "jobs", j=j, back_qs=back_qs)


@app.get("/jobs/{ref}/panel", response_class=HTMLResponse)
def job_panel(request: Request, ref: str):
    u = user_of(request)
    j = jobs.get(u, ref)
    if not j:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_job_panel.html", {"j": j, "user": u, "full": False})


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, item: str = ""):
    u = user_of(request)
    items = jobs.inbox(u)
    cur = next((i for i in items if i["job"]["ref"] == item), None)
    auto = not cur and bool(items)   # rien de choisi : le premier élément est ouvert d'office (sauf sur petit écran)
    if auto:
        cur = items[0]
    detail = jobs.get(u, cur["job"]["ref"]) if cur else None
    reqs = jobs.access_requests() if auth.can(u, "settings") else []
    repo_reqs = [r for r in jobs.repo_requests() if jobs.can_decide_repo(u, r)] if auth.can(u, "decide") else []
    return render(request, "inbox.html", "inbox", items=items, cur=cur, detail=detail, sel=cur["job"]["ref"] if cur else "", auto=auto,
                  access_reqs=reqs, repo_reqs=repo_reqs)


@app.get("/inbox/{ref}/panel", response_class=HTMLResponse)
def inbox_panel(request: Request, ref: str):
    u = user_of(request)
    cur = next((i for i in jobs.inbox(u) if i["job"]["ref"] == ref), None)
    if not cur:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_inbox_panel.html", {"cur": cur, "j": jobs.get(u, ref), "user": u})


def _new_form(request, u, pre, status=200, error=""):
    """Formulaire de demande. Chaque repo vient avec ce qui s'appliquera (agent, modèle, budget, merge), pour décider en
    connaissance de cause ; « pre » garde la saisie (étude à prolonger, ou demande refusée à corriger)."""
    repos = jobs.repos_for_requests()
    with db.tx() as c:
        models, _ = db.get_setting(c, "models")
        glob, _ = db.get_setting(c, "agents")
        limits, _ = db.get_setting(c, "limits")
        profs = profiles.listing(c)
        for r in repos:
            r["resolved"] = profiles.resolve(c, r["settings"] or {}, "formulaire")
    names = {m["id"]: m["name"] for m in models}
    for r in repos:
        rs = r["settings"] or {}
        h, (m, extra) = rs.get("harness") or glob["harness"], r["resolved"]
        r["info"] = {"agent": jobs.HARNESSES.get(h, h), "model": names.get(m, m), "profile": extra["profile"],
                     "review": names.get(extra["review_model"], extra["review_model"]), "budget": float(rs.get("budget_job") or limits["budget_job"]),
                     "policy": jobs.POLICIES.get(rs.get("merge_policy", "off"), jobs.POLICIES["off"])[0]}
    sel = pre.get("repo_id") or next((r["id"] for r in repos if r["is_default"]), repos[0]["id"] if repos else 0)
    return render(request, "new.html", "new", status=status, repos=repos, models=models, pre=pre, sel_repo=sel, error=error, profs=profs)


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request):
    u = user_of(request)
    pre = {"ref": "", "title": "", "need": "", "done_when": "", "repo_id": 0, "study": False, "harness": "", "model": "", "profile": ""}
    src = request.query_params.get("from", "")
    if src:   # « En faire une demande » depuis une étude rendue : formulaire prérempli, relu par un humain avant envoi
        j = jobs.get(u, src)
        if j and j["source"] == "étude" and j.get("report"):
            need = f"{j['request']}\n\nConclusions de l'étude {j['ref']} :\n{j['report']}"
            pre.update(ref=j["ref"], title=f"Suite de {j['ref']} : {j['title']}"[:200], need=need[:8000], repo_id=j["repo_id"])
    return _new_form(request, u, pre)


@app.post("/new")
def new_post(request: Request, title: str = Form(""), need: str = Form(""), done_when: str = Form(""), repo_id: int = Form(0),
             harness: str = Form(""), model: str = Form(""), study: str = Form(""), profile: str = Form(""), choice: str = Form("")):
    u = user_of(request)
    if choice:
        model, profile = profiles.parse_choice(choice)
    try:
        ref = jobs.create(u, title, need, done_when, repo_id, harness, model, study=bool(study), profile=profile)
    except jobs.Refused as e:   # la saisie est gardée : rien à retaper
        pre = {"ref": "", "title": title[:200], "need": need[:8000], "done_when": done_when[:2000], "repo_id": repo_id,
               "study": bool(study), "harness": harness, "model": model, "profile": profile}
        return _new_form(request, u, pre, status=400, error=str(e))
    return back(f"/jobs?job={ref}", ok=f"{'Étude' if study else 'Demande'} {ref} déposée. Elle attend un runner.")


@app.post("/jobs/{ref}/answer")
def answer_post(request: Request, ref: str, answer: str = Form("")):
    u = user_of(request)
    try:
        jobs.answer(u, ref, answer)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok="Réponse envoyée. Le job reprend.")


@app.post("/jobs/{ref}/decide")
def decide_post(request: Request, ref: str, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        jobs.decide(u, ref, verdict, note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok="Merge approuvée." if verdict == "approve" else "Merge refusée.")


@app.post("/jobs/{ref}/reviews")
def rereview_post(request: Request, ref: str):
    u = user_of(request)
    try:
        jobs.rereview(u, ref)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok="Vérification relancée : le runner relit le même sha, sans relancer l'agent.")


@app.post("/jobs/{ref}/spec")
def spec_post(request: Request, ref: str, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        jobs.validate_spec(u, ref, verdict == "approve", note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok="Spec validée." if verdict == "approve" else "Spec refusée.")


@app.post("/jobs/{ref}/cancel")
def cancel_post(request: Request, ref: str):
    u = user_of(request)
    try:
        msg = jobs.cancel(u, ref)
    except jobs.Refused as e:
        return back(_safe_back(request, "/jobs"), err=str(e))
    return back(_safe_back(request, "/jobs"), ok=msg)


@app.post("/jobs/{ref}/archive")
def archive_post(request: Request, ref: str, restore: int = Form(0)):
    u = user_of(request)
    try:
        jobs.archive(u, ref, restore=bool(restore))
    except jobs.Refused as e:
        return back(_safe_back(request, "/jobs"), err=str(e))
    origin = _safe_back(request, "/jobs")
    if urlsplit(origin).path == "/archives":   # depuis la page des archives : on y reste, recherche comprise
        return back(origin, ok=f"{ref} restauré." if restore else f"{ref} archivé.")
    # Retour à la même vue (tableau ou liste, filtre de repo, archives), sans le job qui vient de quitter la liste.
    q = parse_qs(urlsplit(origin).query)
    keep = {k: q[k][0] for k in ("view", "repo", "archived") if q.get(k)}
    return back("/jobs" + ("?" + urlencode(keep) if keep else ""), ok=f"{ref} restauré." if restore else f"{ref} archivé.")


@app.get("/archives", response_class=HTMLResponse)
def archives_page(request: Request, q: str = "", repo: int = 0, state: str = "", regex: int = 0, page: int = 1, partial: int = 0, job: str = ""):
    """Jobs archivés : recherche (texte ou expression régulière), filtres par repo et par issue, pagination."""
    u = user_of(request)
    rows, total, err, page = jobs.search_archives(u, q, repo, state, bool(regex), page)
    with db.tx() as c:
        repos = [r for r in db.rows(c, select(db.repos).order_by(db.repos.c.name)) if jobs.demo_visible() or not jobs.is_demo_repo(r)]
    pages = max(1, -(-total // jobs.ARCHIVE_PAGE))
    qs = urlencode({k: v for k, v in (("q", q.strip()), ("repo", repo or ""), ("state", state), ("regex", 1 if regex else "")) if v})
    ctx = dict(rows=rows, total=total, err=err, pg=page, pages=pages, q=q.strip(), repo=repo, state=state, regex=bool(regex),
               repos=repos, qs=qs, OUTCOMES=jobs.OUTCOMES, PHRASE=jobs.delete_phrase, all_n=jobs.archived_count(u),
               detail=jobs.get(u, job) if job and not partial else None, sel=job)
    if partial:   # recherche pendant la frappe : seule la liste est renvoyée
        return templates.TemplateResponse(request, "_archives_list.html", {**ctx, "user": u, "can": auth.can})
    return render(request, "archives.html", "jobs", **ctx)


@app.post("/jobs/{ref}/delete")
def job_delete(request: Request, ref: str, typed: str = Form("")):
    u = user_of(request)
    origin = _safe_back(request, "/archives")
    try:
        jobs.delete_job(u, ref, typed)
    except jobs.Refused as e:
        return back(origin if urlsplit(origin).path == "/archives" else "/archives", err=str(e))
    return back(origin if urlsplit(origin).path == "/archives" else "/archives", ok=f"{ref} supprimé définitivement.")


@app.post("/archives/bulk")
async def archives_bulk(request: Request):
    """Plusieurs jobs archivés d'un coup : restaurer (reviewers), ou supprimer (admins, confirmé par « supprimer N jobs »)."""
    u = user_of(request)
    f = await request.form()
    refs = [str(r) for r in f.getlist("ref")][:200]
    origin = _safe_back(request, "/archives")
    origin = origin if urlsplit(origin).path == "/archives" else "/archives"
    if not refs:
        return back(origin, err="Aucun job sélectionné.")
    action, done, errors = f.get("action"), 0, []
    if action == "delete":
        if not auth.can(u, "settings"):
            return back(origin, err="Seuls les admins suppriment un job.")
        if str(f.get("typed") or "").strip().lower() != f"supprimer {len(refs)} jobs":
            return back(origin, err=f"Confirmation incorrecte : tape exactement « supprimer {len(refs)} jobs ».")
    for ref in refs:
        try:
            if action == "delete":
                j = jobs.get(u, ref)
                jobs.delete_job(u, ref, jobs.delete_phrase(j))   # la phrase groupée a été vérifiée ci-dessus
            elif action == "restore":
                jobs.archive(u, ref, restore=True)
            else:
                return back(origin, err="Action inconnue.")
            done += 1
        except jobs.Refused as e:
            errors.append(f"{ref} : {e}")
    verb = "supprimé(s) définitivement" if action == "delete" else "restauré(s)"
    return back(origin, ok=f"{done} job(s) {verb}." if done else None, err=" ".join(errors)[:400] or None)


@app.get("/jobs/{ref}/rapport.md")
def study_report(request: Request, ref: str):
    """Rapport d'une étude, en Markdown, à télécharger (mêmes droits que la lecture du job)."""
    u = user_of(request)
    try:
        j = jobs.get(u, ref)
    except jobs.Refused:
        return Response("Job introuvable.", status_code=404, media_type="text/plain; charset=utf-8")
    if not j or not j.get("report"):
        return Response("Ce job n'a pas de rapport.", status_code=404, media_type="text/plain; charset=utf-8")
    body = f"# {j['title']}\n\n_{j['ref']} · étude rendue par la Fabrique_\n\n{j['report']}\n"
    name = re.sub(r"[^A-Za-z0-9-]", "", j["ref"]) + "-etude.md"
    return Response(body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/jobs/{ref}/seen")
def seen_post(request: Request, ref: str):
    u = user_of(request)
    try:
        jobs.mark_seen(u, ref)
    except jobs.Refused as e:
        return back("/inbox", err=str(e))
    return back("/inbox", ok=f"Étude {ref} marquée comme lue.")


@app.get("/runners", response_class=HTMLResponse)
def runners_page(request: Request, new_token: str = "", new_name: str = ""):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, "les runners")
    with db.tx() as c:
        work.reconcile(c)
        rs = db.rows(c, select(db.runners).where(db.runners.c.revoked.is_(False)).order_by(db.runners.c.online.desc(), db.runners.c.name))
        if not jobs.demo_visible():
            rs = [r for r in rs if r["token_hash"]]
        pending = db.rows(c, select(db.intents).where(db.intents.c.state.in_(["pending", "leased"])))
    return render(request, "runners.html", "runners", runners=rs, pending=pending, new_token=new_token, new_name=new_name)


@app.post("/runners", response_class=HTMLResponse)
def runner_add(request: Request, name: str = Form(""), kind: str = Form("conteneur")):
    """Le token est affiché une seule fois, dans cette réponse (jamais stocké en clair, jamais dans une URL)."""
    u = user_of(request)
    if not auth.can(u, "runners_edit"):
        return back("/runners", err="Seuls les admins ajoutent un runner.")
    try:
        token = work.create_runner(name, kind, u["user"])
    except jobs.Refused as e:
        return back("/runners", err=str(e))
    resp = runners_page(request, new_token=token, new_name=name.strip())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/runners/{rid}/rotate", response_class=HTMLResponse)
def runner_rotate(request: Request, rid: int):
    """Renouvelle le token d'un runner ; comme à la création, il n'est affiché qu'une fois, dans cette réponse."""
    u = user_of(request)
    if not auth.can(u, "runners_edit"):
        return back("/runners", err="Seuls les admins renouvellent un token.")
    try:
        name, token = work.rotate_runner(rid, u["user"])
    except jobs.Refused as e:
        return back("/runners", err=str(e))
    resp = runners_page(request, new_token=token, new_name=name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/runners/{rid}/revoke")
def runner_revoke(request: Request, rid: int):
    u = user_of(request)
    if not auth.can(u, "runners_edit"):
        return back("/runners", err="Seuls les admins révoquent un runner.")
    try:
        work.revoke_runner(rid, u["user"])
    except jobs.Refused as e:
        return back("/runners", err=str(e))
    return back("/runners", ok="Runner révoqué : son token ne fonctionne plus.")


# ---------- API des runners (jeton Bearer, pas de session) ----------

def _runner(request):
    if config.RUNNER_API_INTERNAL and (request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")):
        raise work.Denied("API des runners réservée au réseau interne")
    h = request.headers.get("authorization", "")
    r = work.runner_for(h[7:].strip() if h.lower().startswith("bearer ") else "")
    if not r:
        raise work.Denied("token de runner invalide ou révoqué")
    return r


@app.exception_handler(work.Denied)
async def _denied(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=403)


@app.post("/api/runner/heartbeat")
async def api_heartbeat(request: Request):
    r = _runner(request)
    res = await run_in_threadpool(work.heartbeat, r, await request.json())
    return {"ok": True, "name": r["name"], **res}


@app.post("/api/runner/claim")
async def api_claim(request: Request):
    r = _runner(request)
    item = await run_in_threadpool(work.claim, r)
    return item or Response(status_code=204)


@app.post("/api/runner/jobs/{ref}/event")
async def api_event(request: Request, ref: str):
    r = _runner(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise work.Denied("événement invalide")
    res = await run_in_threadpool(work.report, r, ref, body)
    return {"ok": True, **({"decision": res} if res else {})}


@app.get("/repos", response_class=HTMLResponse)
def repos_page(request: Request, repo: int = 0):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, "les repos")
    with db.tx() as c:
        rs = [r for r in db.rows(c, select(db.repos).order_by(db.repos.c.is_default.desc(), db.repos.c.name))
              if jobs.demo_visible() or not jobs.is_demo_repo(r)]
        open_n = {}
        for j in db.rows(c, select(db.jobs.c.repo_id, db.jobs.c.stage).where(db.jobs.c.archived.is_(False))):
            if j["stage"] not in jobs.FINAL:
                open_n[j["repo_id"]] = open_n.get(j["repo_id"], 0) + 1
        limits, _ = db.get_setting(c, "limits")
        agents_s, _ = db.get_setting(c, "agents")
        models, _ = db.get_setting(c, "models")
        git = forges.listing(c)
        profs = profiles.listing(c)
        onb = {}   # dernier job d'onboarding de chaque dépôt (état, ce qu'il attend)
        for j in db.rows(c, select(db.jobs.c.repo_id, db.jobs.c.ref, db.jobs.c.stage, db.jobs.c.status_text)
                         .where(db.jobs.c.source == "onboarding", db.jobs.c.archived.is_(False)).order_by(db.jobs.c.id)):
            onb[j["repo_id"]] = j
    cur = next((r for r in rs if r["id"] == repo), None)
    return render(request, "repos.html", "repos", repos=rs, open_n=open_n, cur=cur, limits=limits, agents_s=agents_s, models=models,
                  git=git, sel=repo, onb=onb, profs=profs)


@app.post("/repos")
def repo_add(request: Request, provider: str = Form(""), slug: str = Form(""), name: str = Form("")):
    u = user_of(request)
    if not auth.can(u, "repos_edit"):
        return back("/repos", err="Seuls les admins ajoutent un repo.")
    slug = slug.strip().strip("/")
    with db.tx() as c:
        gp = forges.resolve(c, provider)                         # provider précis ; un type seul désigne le premier de ce type
    typ, gid = (gp["type"], gp["id"]) if gp else (provider, "")
    if typ not in forges.TYPES or not slug or "/" not in slug or len(slug) > 200 or any(ch in slug for ch in " \"'<>\\"):
        return back("/repos", err="Provider ou chemin du repo invalide (forme attendue : groupe/projet).")
    with db.tx() as c:
        if db.one(c, select(db.repos.c.id).where(db.repos.c.slug == slug, db.repos.c.git_id == gid)):
            return back("/repos", err="Ce repo est déjà connu pour ce provider.")
        g = gp if gp and gp.get("token") else None
    if g:   # token connu du dashboard : on vérifie le chemin tout de suite, plutôt que de laisser l'onboarding échouer
        try:
            why = providers.git_repo_check(typ, g["base_url"], providers.unseal(g["token"]), slug)
        except providers.Invalid:
            why = None
        if why:
            return back("/repos", err="Repo non ajouté : " + why)
    with db.tx() as c:
        first = not db.one(c, select(db.repos.c.id))
        rid = c.execute(insert(db.repos).values(provider=typ, git_id=gid, slug=slug, name=(name.strip() or slug.split("/")[-1])[:120],
                                                is_default=first, onboarding="pending", settings={"merge_policy": "off"},
                                                created_at=time.time())).inserted_primary_key[0]
        db.log_audit(c, u["user"], "Repo ajouté", target=slug, detail="onboarding demandé")
    try:
        ref = jobs.onboard(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=f"Repo ajouté. Onboarding lancé ({ref}) : la recipe arrivera en PR, à relire et merger.")


@app.post("/repos/request")
def repo_request(request: Request, provider: str = Form(""), name: str = Form(""), description: str = Form("")):
    u = user_of(request)
    try:
        rid = jobs.request_repo(u, provider, name, description)
    except jobs.Refused as e:
        return back("/repos", err=str(e))
    return back(f"/repos?repo={rid}", ok="Demande enregistrée : un admin doit la valider (inbox) avant toute création.")


@app.post("/repos/{rid}/decide")
def repo_decide(request: Request, rid: int, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        ref = jobs.decide_repo(u, rid, verdict == "approve", note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok=f"Création validée ({ref}) : le runner crée et initialise le repo." if ref else "Demande refusée.")


@app.post("/repos/{rid}/init")
def repo_init(request: Request, rid: int):
    u = user_of(request)
    try:
        ref = jobs.init_repo(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=f"Initialisation validée ({ref}) : le runner pousse le premier commit.")


@app.post("/repos/{rid}/onboard")
def repo_onboard(request: Request, rid: int):
    u = user_of(request)
    try:
        ref = jobs.onboard(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=f"Onboarding lancé ({ref}).")


@app.post("/repos/{rid}/remove")
def repo_remove(request: Request, rid: int):
    u = user_of(request)
    try:
        name = jobs.remove_repo(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back("/repos", ok=f"{name} est retiré de la Fabrique. Rien n'a été supprimé sur la forge.")


@app.post("/repos/{rid}")
async def repo_save(request: Request, rid: int):
    u = user_of(request)
    if not auth.can(u, "repos_edit"):
        return back(f"/repos?repo={rid}", err="Seuls les admins modifient un repo.")
    f = await request.form()
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.id == rid))
        if not r:
            return back("/repos", err="Repo introuvable.")
        if f.get("action") == "default":
            c.execute(update(db.repos).values(is_default=False))
            c.execute(update(db.repos).where(db.repos.c.id == rid).values(is_default=True))
            db.log_audit(c, u["user"], "Repo par défaut", target=r["slug"])
            return back(f"/repos?repo={rid}", ok=f"{r['name']} est le repo par défaut.")
        models, _ = db.get_setting(c, "models")
        s = dict(r["settings"] or {})
        harness, policy = f.get("harness", ""), f.get("merge_policy", "off")
        model, prof = profiles.parse_choice(f.get("choice")) if "choice" in f else (f.get("model", ""), str(f.get("profile") or ""))
        if harness and harness not in jobs.READY or model and model not in {m["id"] for m in models} or policy not in jobs.POLICIES \
                or prof and not profiles.get(c, prof):
            return back(f"/repos?repo={rid}", err="Réglage invalide.")
        try:
            size = int(f.get("size_limit") or 0)
            budget = float(str(f.get("budget_job") or "0").replace(",", "."))
        except ValueError:
            return back(f"/repos?repo={rid}", err="La taille et le budget doivent être des nombres.")
        if not (0 <= size <= 100000 and 0 <= budget <= 1000):
            return back(f"/repos?repo={rid}", err="Taille ou budget hors limites.")
        paths = [p.strip() for p in str(f.get("protected_paths") or "").splitlines() if p.strip()][:100]
        s.update(harness=harness, model=model, profile=prof, merge_policy=policy, size_limit=size or None, budget_job=budget or None, protected_paths=paths,
                 tags=work.clean_tags(f.get("tags")))
        name = str(f.get("name") or r["name"]).strip()[:120] or r["name"]
        c.execute(update(db.repos).where(db.repos.c.id == rid).values(settings=s, name=name, accepts_jobs=f.get("accepts_jobs") == "1"))
        db.log_audit(c, u["user"], "Settings du repo", target=r["slug"], detail=f"merge : {jobs.POLICIES[policy][0]}")
    return back(f"/repos?repo={rid}", ok="Settings du repo enregistrés.")


SECTIONS = [("agents", "Agents et modèles"), ("git", "Providers Git"), ("oidc", "Connexion (OIDC)"), ("limits", "Budgets et barrières"),
            ("notifications", "Notifications"), ("users", "Utilisateurs et rôles"), ("demo", "Démonstration")]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tab: str = "agents"):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return forbidden(request, "les settings")
    sections = [s for s in SECTIONS if s[0] != "demo" or config.DEMO]   # sans FABRIQUE_DEMO=1, pas d'onglet Démonstration
    tab = tab if tab in dict(sections) else "agents"
    data = {}
    with db.tx() as c:
        for k in ("agents", "models", "git", "limits", "oidc", "access", "demo", "creation"):
            data[k] = db.get_setting(c, k)
        data["models"] = (providers.models_with_provider(c), data["models"][1])
        data["git"] = (forges.listing(c), data["git"][1])
        provs = providers.listing(c)
        chans = notify.channels(c)
        probes, _ = db.get_setting(c, "probes")
        loaded, load_err = None, ""
        lp = request.query_params.get("load", "")
        if tab == "agents" and lp:
            try:
                loaded = providers.fetch_models(c, lp)
            except providers.Invalid as e:
                load_err = str(e)
        users = db.rows(c, select(db.users).order_by(db.users.c.role.desc(), db.users.c.username))
        default_problem = providers.default_problem(c) if tab == "agents" else ""
        profs = profiles.listing(c)
        prof_usage = {p["id"]: profiles.usage(c, p["id"]) for p in profs} if tab == "agents" else {}
    return render(request, "settings.html", "settings", tab=tab, sections=sections, data=data, users=users, provs=provs,
                  ptypes=providers.TYPES, efforts=providers.EFFORTS, loaded=loaded, load_err=load_err, load_pid=lp, probes=probes or {},
                  edit=next((p for p in provs if p["id"] == request.query_params.get("edit", "")), None),
                  git_edit=next((g for g in data["git"][0] if g["id"] == request.query_params.get("editgit", "")), None),
                  default_problem=default_problem, profs=profs, prof_usage=prof_usage, ROLES=profiles.ROLES, pefforts=profiles.efforts(),
                  EXAMPLES=profiles.EXAMPLES,
                  prof_edit=next((p for p in profs if p["id"] == request.query_params.get("profile", "")), None),
                  chans=chans, ch_edit=next((x for x in chans if x["id"] == request.query_params.get("chan", "")), None),
                  notif_types=notify.TYPES, notif_events=notify.EVENTS, notif_recent=notify.recent() if tab == "notifications" else [],
                  secret_key_set=bool(config.SECRET_KEY), generated=sorted(config.GENERATED), PW_RULE=auth.PW_RULE,
                  lu={k: request.query_params.get("lu_" + k, "") for k in ("login", "name", "role")}, HARNESS_API=providers.HARNESS_API,
                  oidc=auth.conf(fresh=True), oidc_locked=auth.locked(), oidc_problems=auth.problems(auth.conf()) if auth.conf()["issuer"] else [],
                  secret_set=bool(auth.client_secret()), secret_env=bool(config.OIDC_CLIENT_SECRET), session_set=len(config.SESSION_SECRET) >= auth.MIN_SECRET,
                  callback=config.PUBLIC_URL + "/auth/callback", admins_env=config.FABRIQUE_ADMINS, test=request.query_params.get("test"))


def _lines(v):
    return [x.strip() for x in str(v or "").replace(",", "\n").splitlines() if x.strip()][:50]


@app.post("/settings/channels/{cid}/test")
def channel_test(request: Request, cid: str):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins testent un channel.")
    try:
        notify.send_test(cid)
    except notify.Invalid as e:
        return back("/settings?tab=notifications", err=str(e))
    return back("/settings?tab=notifications", ok="Message d'essai envoyé.")


@app.post("/settings/{section}")
async def settings_save(request: Request, section: str):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins modifient les settings.")
    f = await request.form()
    url = f"/settings?tab={section}"
    with db.tx() as c:
        if section == "agents":
            models = {x["id"]: x for x in providers.models_with_provider(c)}
            m, prof = profiles.parse_choice(f.get("choice")) if "choice" in f else (f.get("model", ""), str(f.get("profile") or ""))
            p = profiles.get(c, prof) if prof else None
            if prof and not p:
                return back(url, err="Profil de modèles inconnu.")
            if p:
                m = p["roles"]["agent"]["model"]     # le profil complet : le modèle de son agent sert aussi de modèle par défaut
            h = f.get("harness", "")
            if h not in jobs.HARNESSES or m not in models:
                return back(url, err="Agent ou modèle inconnu.")
            if h not in jobs.READY:
                return back(url, err=f"{jobs.HARNESSES[h]} n'est pas encore disponible.")
            if not providers.compatible(models[m], h):
                return back(url, err=f"{jobs.HARNESSES[h]} ne sait pas utiliser {models[m]['name']} : choisis un modèle compatible.")
            db.set_setting(c, "agents", {"harness": h, "model": m, "profile": prof}, u["user"])
            db.log_audit(c, u["user"], "Settings : agents", detail=f"{jobs.HARNESSES[h]} · {m}" + (f" · profil {prof}" if prof else ""))
        elif section == "profiles":
            url = "/settings?tab=agents"
            pid = str(f.get("pid") or "").strip()
            try:
                if f.get("action") == "remove":
                    profiles.remove(c, u["user"], pid)
                    return back(url + "#profils", ok="Profil retiré.")
                profiles.save(c, u["user"], pid if f.get("edit") == "1" else "", f.get("name", ""),
                              {k: {"model": f.get(f"model:{k}", "")} for k in profiles.ROLES}, new=f.get("edit") != "1")
            except profiles.Invalid as e:
                return back(url + "#profil", err=str(e))
            return back(url + "#profils", ok="Profil enregistré.")
        elif section == "providers":
            act, pid = ("probe" if f.get("probe") else f.get("action", "")), str(f.get("pid") or "").strip()
            url = "/settings?tab=agents"
            try:
                if act == "save":
                    existed = any(p["id"] == pid for p in providers.listing(c))
                    providers.save(c, u["user"], pid, f.get("type", ""), f.get("name", ""), f.get("base_url", ""), f.get("key", ""),
                                   clear_key=f.get("clear_key") == "1", effort=f.get("effort", ""))
                    if existed:
                        return back(url, ok="Provider modifié.")
                    return back(f"/settings?tab=agents&load={pid}", ok="Provider enregistré : coche les modèles à proposer.")
                if act == "remove":
                    providers.remove(c, u["user"], pid)
                    return back(url, ok="Provider retiré.")
                if act == "enable":
                    probes, _ = db.get_setting(c, "probes")
                    chosen = []
                    for mid in f.getlist("model")[:40]:
                        pr = ((probes or {}).get(pid) or {}).get(mid) or {}
                        apis = [a for a in ("anthropic", "openai") if pr.get(a)] if pr else []
                        chosen.append({"id": mid, "name": f.get(f"name:{mid}") or mid, "apis": apis})
                    providers.enable(c, u["user"], pid, chosen)
                    return back(url, ok=f"{len(chosen)} modèle(s) proposé(s) pour ce provider.")
                if act == "probe":
                    mid = str(f.get("probe") or "")[:120]
                    res = providers.probe(c, pid, mid)
                    probes, _ = db.get_setting(c, "probes")
                    probes = dict(probes or {})
                    probes.setdefault(pid, {})[mid] = dict(res, at=time.time())
                    db.set_setting(c, "probes", probes, u["user"])
                    ok = [lbl for api, lbl in (("anthropic", "Claude Code"), ("openai", "Codex")) if res[api]]
                    return back(f"/settings?tab=agents&load={pid}", ok=f"{mid} : " + (", ".join(ok) + " ✓" if ok else "aucun appel d'outil réussi"))
            except providers.Invalid as e:
                return back(url, err=str(e))
            return back(url, err="Action inconnue.")
        elif section == "channels":
            url = "/settings?tab=notifications"
            try:
                if f.get("action") == "remove":
                    notify.remove(c, u["user"], str(f.get("cid") or ""))
                    return back(url, ok="Channel retiré.")
                notify.save(c, u["user"], f)
            except notify.Invalid as e:
                return back(url + (f"&chan={f.get('cid')}" if f.get("cid") else ""), err=str(e))
            return back(url, ok="Channel enregistré. Utilise « Envoyer un test » pour vérifier.")
        elif section == "creation":
            ns = {p: re.sub(r"[^A-Za-z0-9._/-]", "", str(f.get(p) or "").strip().strip("/"))[:100] for p in forges.TYPES}
            db.set_setting(c, "creation", ns, u["user"])
            db.log_audit(c, u["user"], "Settings : groupes de création", detail=", ".join(f"{k}={v or '—'}" for k, v in ns.items()))
            url = "/settings?tab=git"
        elif section == "git":
            # Plusieurs providers par type, chacun désigné par son nom (app/forges.py). Le jeton du compte robot est chiffré
            # en base, en écriture seule ; il est remis au runner pour chaque job de SES repos. Sans jeton ici, le runner
            # utilise ses variables d'environnement.
            try:
                if f.get("remove") is not None:
                    forges.remove(c, u["user"], str(f.get("remove")))
                else:
                    grp = re.sub(r"[^A-Za-z0-9._/-]", "", str(f.get("creation") or "").strip().strip("/"))[:100]
                    forges.save(c, u["user"], str(f.get("gid") or ""), str(f.get("name") or ""), str(f.get("type") or ""),
                                str(f.get("base_url") or ""), str(f.get("token") or "").strip(), grp, clear_token=f.get("clear_token") == "1")
            except forges.Invalid as e:
                return back(url, err=str(e))
        elif section == "limits":
            try:
                lim = {"budget_job": float(str(f.get("budget_job")).replace(",", ".")), "budget_day": float(str(f.get("budget_day")).replace(",", ".")),
                       "max_parallel": int(f.get("max_parallel")), "size_limit": int(f.get("size_limit")),
                       "watchdog_min": int(f.get("watchdog_min") or 10), "auto_archive_days": int(f.get("auto_archive_days") or 0),
                       "protected_paths": [p.strip() for p in str(f.get("protected_paths") or "").splitlines() if p.strip()][:100]}
            except (TypeError, ValueError):
                return back(url, err="Les budgets et limites doivent être des nombres.")
            if not (0 < lim["budget_job"] <= lim["budget_day"] <= 10000 and 1 <= lim["max_parallel"] <= 64 and 1 <= lim["size_limit"] <= 100000
                    and 2 <= lim["watchdog_min"] <= 240 and 0 <= lim["auto_archive_days"] <= 3650):
                return back(url, err="Valeurs hors limites (le budget d'un job ne peut pas dépasser celui du jour).")
            db.set_setting(c, "limits", lim, u["user"])
            db.log_audit(c, u["user"], "Settings : budgets et barrières", detail=f"{lim['budget_job']} $/job · {lim['budget_day']} $/jour")
        elif section == "access":
            d = f.get("default_role")
            if d not in ("requester", "none"):
                return back("/settings?tab=users", err="Choix invalide.")
            db.set_setting(c, "access", {"default_role": d}, u["user"])
            db.log_audit(c, u["user"], "Settings : nouveaux comptes", detail=auth.ROLE_LABEL[d])
            url = "/settings?tab=users"
        elif section == "demo":
            vis = f.get("visible") == "1"
            db.set_setting(c, "demo", {"visible": vis}, u["user"])
            db.log_audit(c, u["user"], "Settings : démonstration", detail="affichée" if vis else "masquée")
            jobs.forget_demo()
            url = "/settings?tab=demo"
        elif section == "oidc":
            cur, _ = db.get_setting(c, "oidc")
            new = dict(db.DEFAULTS["oidc"], **(cur or {}))
            locked = auth.locked()
            for k in ("issuer", "client_id", "scopes", "groups_claim"):
                if k not in locked and k in f:
                    new[k] = str(f.get(k) or "").strip()[:300]
            for k in ("admin_groups", "verifier_groups", "requester_groups"):
                if k not in locked and k in f:
                    new[k] = _lines(f.get(k))
            if new["issuer"] and urlsplit(new["issuer"]).scheme not in ("http", "https"):
                return back(url, err="L'émetteur doit être une adresse http(s).")
            if "openid" not in (new["scopes"] or "").split():
                return back(url, err="Les scopes doivent contenir « openid ».")
            sec = str(f.get("client_secret") or "").strip()
            if sec and not config.OIDC_CLIENT_SECRET:   # écriture seule ; vide = on garde la valeur enregistrée
                try:
                    db.set_setting(c, "oidc_secret", {"v": providers.seal(sec[:500])}, u["user"])
                except providers.Invalid as e:
                    return back(url, err=str(e))
            if new["issuer"] and not auth.has_local_admin(fresh=True) and not config.FABRIQUE_ADMINS and not config.OPEN:
                return back(url, err="Crée d'abord un compte admin local (Utilisateurs et rôles) : c'est le secours si la connexion unique est mal réglée.")
            db.set_setting(c, "oidc", new, u["user"])
            db.log_audit(c, u["user"], "Settings : connexion OIDC", detail=new["issuer"] or "mode local")
            auth.invalidate()
        else:
            return back("/settings", err="Section inconnue.")
    return back(url, ok="Settings enregistrés.")


@app.post("/settings-test/git")
async def git_test(request: Request):
    """Essai d'un provider Git avant de l'enregistrer : rien n'est écrit. Le token saisi sert à l'essai ; champ vide, c'est
    le token déjà enregistré pour ce type qui sert, et seulement vers l'adresse pour laquelle il a été enregistré."""
    u = user_of(request)
    if not auth.can(u, "settings"):
        return JSONResponse({"ok": False, "msg": "Seuls les admins testent un provider."}, status_code=403)
    f = await request.form()
    typ, base, tok, gid = (str(f.get(k) or "").strip() for k in ("type", "base_url", "token", "gid"))
    base = base.rstrip("/")[:200]
    if typ not in forges.TYPES or urlsplit(base).scheme != "https":
        return JSONResponse({"ok": False, "msg": "Choisis un type et saisis une adresse en https."})
    if not tok:
        with db.tx() as c:
            old = forges.resolve(c, gid or typ) or {}
        if not old.get("token"):
            return JSONResponse({"ok": False, "msg": "Saisis le token du compte robot pour le tester."})
        if old.get("base_url") != base:
            return JSONResponse({"ok": False, "msg": "L'adresse a changé : saisis à nouveau le token (un token enregistré n'est jamais envoyé à une nouvelle adresse)."})
        try:
            tok = providers.unseal(old["token"])
        except providers.Invalid as e:
            return JSONResponse({"ok": False, "msg": str(e)})
    grp = re.sub(r"[^A-Za-z0-9._/-]", "", str(f.get("creation") or "").strip().strip("/"))[:100]
    ok, msg = await run_in_threadpool(providers.git_check, typ, base, tok[:500], grp)
    return JSONResponse({"ok": ok, "msg": msg}, headers={"Cache-Control": "no-store"})


@app.post("/settings-test/oidc")
async def oidc_test(request: Request, issuer: str = Form("")):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins testent la connexion.")
    ok, msg = await auth.discover(issuer or auth.conf()["issuer"])
    if "application/json" in request.headers.get("accept", ""):   # essai depuis la page : la saisie n'est pas perdue
        return JSONResponse({"ok": ok, "msg": msg + ("" if not ok else " Pense à cliquer sur « Enregistrer ».")})
    return back("/settings?tab=oidc", ok=msg if ok else None, err=None if ok else msg)


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, "le journal d'activité")
    return render(request, "activity.html", "activity", rows=jobs.activity())


@app.post("/dev/as")
def dev_as(request: Request, role: str = Form("")):
    """Mode local uniquement : voir l'interface avec un autre rôle (démonstration). Sans effet si la connexion OIDC est active."""
    if auth.mode() != "off":
        return back("/jobs", err="Indisponible quand la connexion OIDC est active.")
    r = back(_safe_back(request, "/jobs"))
    if role in auth.ROLES:
        r.set_cookie("fabrique_as", role, httponly=True, samesite="lax", max_age=86400)
    else:
        r.delete_cookie("fabrique_as")
    return r


@app.get("/events/stream")
async def stream(request: Request):
    """Temps réel : un message « change » dès qu'un événement est écrit en base (sondage léger toutes les secondes)."""
    user_of(request)
    last = await run_in_threadpool(jobs.last_event_id)

    async def gen():
        nonlocal last
        yield "retry: 3000\n\n"
        idle = 0
        while not await request.is_disconnected():
            cur = await run_in_threadpool(jobs.last_event_id)
            if cur != last:
                last, idle = cur, 0
                yield f"event: change\ndata: {cur}\n\n"
            else:
                idle += 1
                if idle >= 15:
                    idle = 0
                    yield ": ping\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.exception_handler(PendingAccess)
async def _pending(request, exc):
    if request.method == "GET" and not request.url.path.startswith(("/events/", "/api/")):
        return RedirectResponse("/access", status_code=303)
    return JSONResponse({"error": "compte en attente de validation"}, status_code=403)


@app.get("/access", response_class=HTMLResponse)
def access_page(request: Request):
    u = user_of(request, pending_ok=True)
    with db.tx() as c:
        me = db.one(c, select(db.users).where(db.users.c.username == u["user"]))
    resp = templates.TemplateResponse(request, "access.html", {"user": u, "me": me, "mode": auth.mode(), "flash": _flash(request)})
    if _flash(request):
        resp.delete_cookie(FLASH)
    return resp


@app.post("/access")
def access_post(request: Request, role: str = Form(""), note: str = Form("")):
    u = user_of(request, pending_ok=True)
    if auth.mode() != "on":
        return back("/access", err="En mode local, il n'y a pas de compte à faire évoluer.")
    if role not in auth.ROLES or (u["role"] in auth.ROLES and auth.ROLES.index(role) <= auth.ROLES.index(u["role"])):
        return back("/access" if u["role"] not in auth.ROLES else _safe_back(request, "/jobs"), err="Choisis un rôle supérieur à ton rôle actuel.")
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == u["user"]).values(
            requested_role=role, request_note=note.strip()[:500], requested_at=time.time()))
        db.log_audit(c, u["user"], "Demande d'accès", target=auth.ROLE_LABEL[role], detail=note.strip()[:200])
    # Depuis la fenêtre du menu : retour à la page d'où vient la demande ; compte sans rôle : sa page « Mon accès ».
    to = "/access" if u["role"] not in auth.ROLES else _safe_back(request, "/jobs")
    return back(to, ok="Demande envoyée. Un admin la verra dans son inbox.")


@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return forbidden(request, "la mise en route")
    return render(request, "start.html", "start", steps=start_steps())


@app.post("/users/local")
async def user_local(request: Request):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins créent des comptes.")
    f = await request.form()
    try:
        login = auth.create_local(f.get("login"), f.get("name"), str(f.get("password") or ""), str(f.get("role") or ""), u["user"])
    except ValueError as e:   # on garde la saisie (sauf le mot de passe) : rien à retaper
        keep = urlencode({"lu_login": str(f.get("login") or "")[:40], "lu_name": str(f.get("name") or "")[:120], "lu_role": str(f.get("role") or "")[:20]})
        return back("/settings?tab=users&" + keep, err=str(e))
    return back("/settings?tab=users", ok=f"Compte local « {login} » créé.")


def _local_row(u):
    with db.tx() as c:
        row = db.one(c, select(db.users).where(db.users.c.username == (u or {}).get("user", "")))
    return row if row and row.get("password_hash") else None


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    """Mon compte : accessible à tous les rôles (le mot de passe d'un compte local ne dépend pas des Settings, réservés aux admins)."""
    u = user_of(request)
    return render(request, "account.html", "account", local_account=bool(_local_row(u)))


@app.post("/account/password")
async def account_password(request: Request):
    u = user_of(request)
    f = await request.form()
    row = _local_row(u)
    if not row:
        return back("/account", err="Ce compte n'a pas de mot de passe local.")
    if not auth.check_password(str(f.get("old") or ""), row["password_hash"]):
        return back("/account", err="Mot de passe actuel incorrect.")
    try:
        auth.set_password(row["username"], str(f.get("new") or ""), u["user"])
    except ValueError as e:
        return back("/account", err=str(e))
    # Les autres sessions du compte tombent ; celle-ci est rouverte pour ne pas déconnecter la personne qui vient de changer.
    auth.revoke_sessions(row["username"], u["user"], "mot de passe changé")
    if request.scope.get("session"):
        request.session["iat"] = time.time()
    return back("/account", ok="Mot de passe changé. Les autres sessions de ce compte sont fermées.")


@app.post("/users/{uid}/logout")
def user_logout(request: Request, uid: int):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins ferment les sessions d'un compte.")
    with db.tx() as c:
        target = db.one(c, select(db.users).where(db.users.c.id == uid))
    if not target:
        return back("/settings?tab=users", err="Compte introuvable.")
    auth.revoke_sessions(target["username"], u["user"], "fermées par un admin")
    return back("/settings?tab=users", ok=f"Sessions de {target['name'] or target['username']} fermées : il faudra se reconnecter.")


@app.post("/users/{uid}/role")
def user_role(request: Request, uid: int, role: str = Form(""), refuse: int = Form(0)):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err="Seuls les admins changent les rôles.")
    with db.tx() as c:
        target = db.one(c, select(db.users).where(db.users.c.id == uid))
        if not target:
            return back("/settings?tab=users", err="Compte introuvable.")
        if refuse:
            c.execute(update(db.users).where(db.users.c.id == uid).values(requested_role=None, request_note=None, requested_at=None))
            db.log_audit(c, u["user"], "Demande d'accès refusée", target=target["username"])
            return back(_safe_back(request, "/settings?tab=users"), ok=f"Demande de {target['name']} refusée.")
        if role not in ("none",) + auth.ROLES:
            return back("/settings?tab=users", err="Rôle inconnu.")
        if target["username"] == u["user"]:
            return back("/settings?tab=users", err="Tu ne peux pas changer ton propre rôle : demande à un autre admin.")
        c.execute(update(db.users).where(db.users.c.id == uid).values(role=role, role_source="panneau", requested_role=None,
                                                                      request_note=None, requested_at=None))
        db.log_audit(c, u["user"], "Rôle changé", target=target["username"], detail=f"{auth.ROLE_LABEL[target['role']]} → {auth.ROLE_LABEL[role]}")
    auth.forget_role(target["username"])
    return back(_safe_back(request, "/settings?tab=users"), ok=f"{target['name']} : {auth.ROLE_LABEL[role]}.")


@app.exception_handler(PermissionError)
async def _no_user(request, exc):
    return JSONResponse({"error": "connexion requise"}, status_code=401)
