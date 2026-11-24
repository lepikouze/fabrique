"""Fabrique — dashboard (control plane): inbox, jobs (board / list, side panel that can be expanded),
new request, runners, repos, settings, activity. Real-time via SSE (/events/stream)."""
from .i18n import M
import asyncio, datetime as dt, json, logging, os, re, time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlencode, quote, urlsplit
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update
from . import auth, config, db, demo, forges, i18n, jobs, notify, profiles, providers, translate, work, challenge, prices

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = os.path.dirname(__file__)


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
    if os.environ.get("FABRIQUE_PRICES", "1") == "1":
        prices.start()
    yield
    demo.stop()
    notify.stop()
    prices.stop()


app = FastAPI(title="Fabrique", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


def hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def when(ts):
    """14:03 today, otherwise "Nov 9"."""
    if not ts:
        return "—"
    d = dt.datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == dt.date.today() else i18n.short_date(d)


def since(ts):
    """Readable age: "just now", "12 min ago", "3 h ago", "2 d ago"."""
    if not ts:
        return "—"
    return i18n.since(ts, "since")


def long_date(ts):
    return i18n.long_date(dt.datetime.fromtimestamp(ts))


def ago(ts):
    return i18n.since(ts, "ago")


def tokens(n):
    return i18n.tokens(n)


def money(x):
    return i18n.money(x)


def duration(ms):
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.0f} s" if ms >= 10000 else f"{ms / 1000:.1f} s"


i18n.install(templates.env)
templates.env.filters.update(hm=hm, hms=hms, when=when, since=since, long_date=long_date, ago=ago, tokens=tokens, money=money, duration=duration)
def _git_name(repo):
    """Git provider name of a repo ("Internal GitLab"), for display."""
    with db.tx() as c:
        return forges.label(c, dict(repo))


templates.env.globals.update(PRICE_KNOWN=prices.known, DONE_WHEN=jobs.done_when_of, DEFAULT_DONE=jobs.DEFAULT_DONE, GIT_NAME=_git_name, CAN_DECIDE_REPO=jobs.can_decide_repo, HARNESSES_FOR=providers.harnesses_for, READY=jobs.READY, TO_CHECK=jobs.to_check, SECURITY_WARN=jobs.security_warn, DELETE_PHRASE=jobs.delete_phrase, REVIEW_EFFORT=jobs.review_effort, PATCH=jobs.patch_info, DIFF_OFF=jobs.diff_off, REPORT_FORMATS=jobs.report_formats, CHANGES_URL=jobs.changes_url, VERSION=config.VERSION, REVIEW_ROLES=work.ROLES, REVIEW_VERDICTS=work.VERDICTS, STAGE=jobs.STAGE, HARNESSES=jobs.HARNESSES, POLICIES=jobs.POLICIES, ROLE_LABEL=auth.ROLE_LABEL,
                             can=auth.can, PROVIDERS={"forgejo": "Forgejo", "gitea": "Gitea", "gitlab": "GitLab", "github": "GitHub"},
                             ONB={"pending": ("Onboarding", "var(--blue)"), "analyzing": ("Onboarding", "var(--blue)"),
                                  "requested": (M("Creation to approve"), "var(--amber)"), "creating": (M("Creation"), "var(--blue)"),
                                  "waiting": (M("Waiting for the repo"), "var(--amber)"), "empty": (M("Empty repo"), "var(--amber)"),
                                  "blank": (M("Blank"), "var(--blue)"),
                                  "ready": (M("Ready"), "var(--ok)"), "failed": (M("Failed"), "var(--bad)")})
auth.templates = templates

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}
app.add_middleware(auth.AuthLayer)
app.add_middleware(db.RequestCache)   # added last: the outermost one, it also covers authentication
app.include_router(auth.router)


@app.middleware("http")
async def language(request: Request, call_next):
    """Request language: the person's choice (cookie), otherwise their browser, otherwise the instance language."""
    cookie = request.cookies.get(i18n.COOKIE, "")
    if cookie in i18n.LANGS:
        lang = cookie
    else:
        codes = (p.split(";")[0].strip().lower()[:2] for p in request.headers.get("accept-language", "").split(","))
        lang = next((code for code in codes if code in i18n.LANGS), None) or i18n.default_lang()
    token = i18n.use(lang, choice=cookie)
    i18n.show_translations(None)
    i18n.original(request.query_params.get("orig") == "1")      # the reader asked for the original texts of a job
    try:
        return await call_next(request)
    finally:
        i18n._lang.reset(token)


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    """Non-token anti-CSRF: a request that modifies state must come from the dashboard itself."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        host = request.headers.get("host", "")
        sfs = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin") or request.headers.get("referer")
        if sfs in ("cross-site", "same-site") or (origin and urlsplit(origin).netloc != host):
            return JSONResponse({"error": M("request from another origin refused")}, status_code=403)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if (o := auth.provider_origin()) and resp.headers.get("content-security-policy") == SECURITY_HEADERS["Content-Security-Policy"]:
        resp.headers["content-security-policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace("form-action 'self'", f"form-action 'self' {o}")
    return resp


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
FLASH = "fabrique_flash"


def back(url, ok=None, err=None):
    """Redirection after a form, with a message (short-lived cookie, read once)."""
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
    """Known account but without a role (awaiting an admin): only the "My access" page is open."""


def user_of(request, pending_ok=False):
    u = auth.who(request)
    if not u:
        raise PermissionError
    if u["role"] not in auth.ROLES and not pending_ok:
        raise PendingAccess
    return u


def start_steps():
    """Setup of a new installation: what remains to be done before the first job (for admins)."""
    with db.tx() as c:
        runners = db.rows(c, select(db.runners.c.online, db.runners.c.token_hash))
        provs = [p for p in providers.listing(c) if p.get("has_key") or p.get("type") == "compatible"]
        models = db.get_setting(c, "models")[0] or []
        model_ok = bool(models) and not providers.default_problem(c)
        git = db.get_setting(c, "git")[0] or []
        repos = db.rows(c, select(db.repos.c.onboarding, db.repos.c.recipe, db.repos.c.settings))
    real = [r for r in repos if not jobs.is_demo_repo(r)]  # demo data never counts as real setup
    live = [r for r in runners if r["token_hash"]]
    return [
        (M("Administrator account"), True, "/settings?tab=users", M("Created at first start; it is also the fallback account.")),
        (M("A runner online"), any(r["online"] for r in live), "/runners", M("The built-in runner registers itself; it shows up online in less than a minute.")),
        (M("A default model"), bool(provs) and model_ok, "/settings?tab=agents",
         M("Add a provider (Anthropic, OpenAI or local server), its key, tick the models to offer, then choose the default agent and model.")),
        (M("A Git provider"), any(g.get("token") for g in git), "/settings?tab=git", M("Git host address and the token of a dedicated bot account.")),
        (M("A first repo"), any(r["onboarding"] in ("ready", "blank") or r["recipe"] is not None for r in real), "/repos", M("Add a repo: its onboarding starts on its own; then merge the recipe PR.")),
    ]


def render(request, name, page, status=200, **kw):
    u = user_of(request)
    todo = 0
    if u.get("role") == "admin" and not config.DEMO:
        todo = sum(1 for s in start_steps() if not s[1])
    access_request = None
    if u.get("role") not in ("admin", None) and auth.mode() == "on":   # role request in progress, for the menu window
        with db.tx() as c:
            me = db.one(c, select(db.users.c.requested_role, db.users.c.request_note).where(db.users.c.username == u.get("user", "")))
        if me and me["requested_role"]:
            access_request = {"role": me["requested_role"], "note": me["request_note"]}
    prefs = view_prefs(request, u)
    chosen = getattr(request.state, "account_lang", None)     # the choice kept on the account follows the person from one computer to another
    if chosen in i18n.LANGS:
        i18n.use(chosen, choice=chosen)
    ctx = dict(user=u, page=page, mode=auth.mode(), access_request=access_request, demo=jobs.demo_visible(), demo_mode=config.DEMO, flash=_flash(request),
               view_prefs=prefs,
               counters=jobs.counters(u), start_todo=todo, **kw)
    resp = templates.TemplateResponse(request, name, ctx, status_code=status)
    if ctx["flash"]:
        resp.delete_cookie(FLASH)
    if chosen in i18n.LANGS and request.cookies.get(i18n.COOKIE) != chosen:
        _lang_cookie(resp, chosen)
    return resp


def _lang_cookie(resp, lang):
    resp.set_cookie(i18n.COOKIE, lang, max_age=365 * 86400, httponly=True, samesite="lax", secure=config.PUBLIC_URL.startswith("https://"))


def forbidden(request, what=None):
    return render(request, "denied.html", "denied", status=403, what=what or M("this page"))


PREFS = "fabrique_jobs"   # without an account (open mode): the display preference remains in the browser
PREF_CHOICES = {"board": ("stages", "compact"), "list": ("grouped", "flat")}


def _clean_prefs(got):
    return {k: got.get(k) if got.get(k) in v else v[0] for k, v in PREF_CHOICES.items()}


def view_prefs(request, u=None):
    """{'board': 'stages' | 'compact', 'list': 'grouped' | 'flat'} ; by default one column and one group per step.
    The preference is kept on the account: it follows the person from one computer to another."""
    u = u or auth.who(request) or {}
    if not u.get("local") and u.get("user"):
        with db.tx() as c:
            row = db.one(c, select(db.users.c.prefs).where(db.users.c.username == u["user"]))
        if row is not None:
            request.state.account_lang = (row["prefs"] or {}).get("lang")
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
    if flat:   # flat list: one line per job, per last activity; filter by stage family
        shown = [j for j in js if not fam or jobs.FAMILY_OF.get(j["stage"]) == fam]
        groups = [(None, shown)]
    else:
        groups = jobs.by_family(js) if compact else jobs.by_stage(js)
    return render(request, "jobs.html", "jobs", view=view, groups=groups, js=js, repo=repo, repos=repo_list, compact=compact, flat=flat,
                  fam=fam, families=jobs.FAMILIES, detail=detail, archived=bool(archived), archived_n=jobs.archived_count(u), sel=job)


@app.post("/prefs/jobs")
def prefs_jobs(request: Request, board: str = Form(""), list_: str = Form("", alias="list")):
    """Each user chooses their job view (user menu); nothing is imposed on others."""
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
        return render(request, "denied.html", "denied", status=404, what=M("this job"))
    # Came from Archives with a search in progress: it is kept to return to it (only its known parameters).
    q = parse_qs(back[:600])
    back_qs = urlencode({k: q[k][0][:200] for k in ("q", "repo", "state", "regex", "page") if q.get(k)})
    return render(request, "job_full.html", "jobs", j=j, back_qs=back_qs)


@app.post("/jobs/{ref}/translate")
async def job_translate(request: Request, ref: str):
    """Translates what the job's agents wrote into the reader's language, with the model chosen in the settings.
    Never automatic: only when the reader asks. Anyone who can see the job can ask. The translation runs in the background:
    the page comes back at once, says "translation in progress", and shows the texts as they arrive; a second click
    while it runs starts nothing."""
    u = user_of(request)
    here = _safe_back(request, f"/jobs/{ref}")
    j = jobs.get(u, ref)
    if not j:
        return forbidden(request, M("this job"))
    lang = i18n.current()
    with db.tx() as c:
        if not translate.model_id(c):
            return back(here, err=M("No translation model is chosen (Settings → Agents and models)."))
    if not translate.start(u, j, lang):
        return back(here, ok=M("A translation of this job is already in progress."))
    return back(here, ok=M("Translation started: the texts appear as soon as they are translated."))


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
    auto = not cur and bool(items)   # nothing selected: the first element is open by default (except on small screens)
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


def _new_form(request, u, pre, status=200, error="", ask=None):
    """Request form. Each repo comes with what will apply (agent, model, budget, merge), to decide with
    full knowledge; "pre" keeps the input (study to be extended, or refused request to be corrected)."""
    repos = jobs.repos_for_requests()
    with db.tx() as c:
        models, _ = db.get_setting(c, "models")
        glob, _ = db.get_setting(c, "agents")
        limits, _ = db.get_setting(c, "limits")
        profs = profiles.listing(c)
        for r in repos:
            r["resolved"] = profiles.resolve(c, r["settings"] or {}, "form")
    names = {m["id"]: m["name"] for m in models}
    for r in repos:
        rs = r["settings"] or {}
        h, (m, extra) = rs.get("harness") or glob["harness"], r["resolved"]
        r["info"] = {"agent": jobs.HARNESSES.get(h, h), "model": names.get(m, m), "profile": extra["profile"],
                     "review": names.get(extra["review_model"], extra["review_model"]), "budget": float(rs.get("budget_job") or limits["budget_job"]),
                     "policy": jobs.POLICIES.get(rs.get("merge_policy", "off"), jobs.POLICIES["off"])[0]}
    sel = pre.get("repo_id") or next((r["id"] for r in repos if r["is_default"]), repos[0]["id"] if repos else 0)
    return render(request, "new.html", "new", status=status, repos=repos, models=models, pre=pre, sel_repo=sel, error=error, profs=profs,
                  ask=ask or [], short=challenge.SHORT_CHARS, short_words=challenge.SHORT_WORDS)


@app.get("/new", response_class=HTMLResponse)
def new_page(request: Request):
    u = user_of(request)
    pre = {"ref": "", "title": "", "need": "", "done_when": "", "repo_id": 0, "study": False, "harness": "", "model": "", "profile": ""}
    src = request.query_params.get("from", "")
    if src:   # "Make a request" from a submitted study: pre-filled form, reviewed by a human before submission
        j = jobs.get(u, src)
        if j and j["source"] == "study" and j.get("report"):
            need = f"{j['request']}\n\n" + i18n.tr(M("Conclusions of study {ref}:", ref=j['ref'])) + f"\n{j['report']}"
            pre.update(ref=j["ref"], title=i18n.tr(M("Follow-up to {ref}: {title}", ref=j["ref"], title=j["title"]))[:200], need=need[:8000], repo_id=j["repo_id"])
    return _new_form(request, u, pre)


@app.post("/new")
def new_post(request: Request, title: str = Form(""), need: str = Form(""), done_when: str = Form(""), repo_id: int = Form(0),
             harness: str = Form(""), model: str = Form(""), study: str = Form(""), profile: str = Form(""), choice: str = Form(""),
             challenged: str = Form(""), cq: list[str] = Form([]), ca: list[str] = Form([])):
    u = user_of(request)
    if choice:
        model, profile = profiles.parse_choice(choice)
    pre = {"ref": "", "title": title[:200], "need": need[:8000], "done_when": done_when[:2000], "repo_id": repo_id,
           "study": bool(study), "harness": harness, "model": model, "profile": profile}
    if challenged:
        # Second pass: the requester answered the questions (their answers complete the need) or sends anyway.
        need = challenge.merge_answers(need, cq, ca)
    elif not study and title.strip() and need.strip():
        # A change request is read once against a fixed grid by the model that would run it; unclear, the requester sees
        # one to three questions, completes or sends anyway. Never a refusal; a model that does not answer is not waited for.
        with db.tx() as c:
            repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
            ask = challenge.check(c, u, title, need, done_when, repo, harness, model, profile) if repo else []
        if ask:
            return _new_form(request, u, pre, ask=ask)
    try:
        ref = jobs.create(u, title, need, done_when, repo_id, harness, model, study=bool(study), profile=profile)
    except jobs.Refused as e:   # input is preserved: nothing to retype
        return _new_form(request, u, dict(pre, need=need[:8000]), status=400, error=str(e))
    ok = M("{p} {ref} filed. It is waiting for a runner.", p=M('Study') if study else M('Request'), ref=ref)
    if not study and not done_when.strip():   # a warning, never a refusal: the default criterion applies
        ok = ok + M(" No “Done when” criterion was given: the default one applies.")
    return back(f"/jobs?job={ref}", ok=ok)


@app.post("/jobs/{ref}/answer")
def answer_post(request: Request, ref: str, answer: str = Form("")):
    u = user_of(request)
    try:
        jobs.answer(u, ref, answer)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok=M("Answer sent. The job resumes."))


@app.post("/jobs/{ref}/decide")
def decide_post(request: Request, ref: str, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        jobs.decide(u, ref, verdict, note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok={"approve": M("Merge approved."), "changes": M("Changes requested: the agent resumes on the same branch.")}.get(verdict, M("Merge refused: the PR will be closed on the Git host.")))


@app.post("/jobs/{ref}/reviews")
def rereview_post(request: Request, ref: str):
    u = user_of(request)
    try:
        jobs.rereview(u, ref)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok=M("Review rerun: the runner reviews the same sha again, without restarting the agent."))


@app.post("/jobs/{ref}/spec")
def spec_post(request: Request, ref: str, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        jobs.validate_spec(u, ref, verdict == "approve", note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok=M("Spec approved.") if verdict == "approve" else M("Spec refused."))


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
    if urlsplit(origin).path == "/archives":   # from the archives page: stay there, including search
        return back(origin, ok=M("{ref} restored.", ref=ref) if restore else M("{ref} archived.", ref=ref))
    # Return to the same view (table or list, repo filter, archives), without the job that just left the list.
    q = parse_qs(urlsplit(origin).query)
    keep = {k: q[k][0] for k in ("view", "repo", "archived") if q.get(k)}
    return back("/jobs" + ("?" + urlencode(keep) if keep else ""), ok=M("{ref} restored.", ref=ref) if restore else M("{ref} archived.", ref=ref))


@app.get("/archives", response_class=HTMLResponse)
def archives_page(request: Request, q: str = "", repo: int = 0, state: str = "", regex: int = 0, page: int = 1, partial: int = 0, job: str = ""):
    """Archived jobs: search (text or regular expression), filters by repo and by outcome, pagination."""
    u = user_of(request)
    rows, total, err, page = jobs.search_archives(u, q, repo, state, bool(regex), page)
    with db.tx() as c:
        repos = [r for r in db.rows(c, select(db.repos).order_by(db.repos.c.name)) if jobs.demo_visible() or not jobs.is_demo_repo(r)]
    pages = max(1, -(-total // jobs.ARCHIVE_PAGE))
    qs = urlencode({k: v for k, v in (("q", q.strip()), ("repo", repo or ""), ("state", state), ("regex", 1 if regex else "")) if v})
    ctx = dict(rows=rows, total=total, err=err, pg=page, pages=pages, q=q.strip(), repo=repo, state=state, regex=bool(regex),
               repos=repos, qs=qs, OUTCOMES=jobs.OUTCOMES, PHRASE=jobs.delete_phrase, all_n=jobs.archived_count(u),
               detail=jobs.get(u, job) if job and not partial else None, sel=job)
    if partial:   # search while typing: only the list is returned
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
    return back(origin if urlsplit(origin).path == "/archives" else "/archives", ok=M("{ref} permanently deleted.", ref=ref))


@app.post("/archives/bulk")
async def archives_bulk(request: Request):
    """Multiple archived jobs at once: restore (reviewers), or delete (admins, confirmed by "delete N jobs")."""
    u = user_of(request)
    f = await request.form()
    refs = [str(r) for r in f.getlist("ref")][:200]
    origin = _safe_back(request, "/archives")
    origin = origin if urlsplit(origin).path == "/archives" else "/archives"
    if not refs:
        return back(origin, err=M("No job selected."))
    action, done, errors = f.get("action"), 0, []
    if action == "delete":
        if not auth.can(u, "settings"):
            return back(origin, err=M("Only admins delete a job."))
        if str(f.get("typed") or "").strip().lower() != f"supprimer {len(refs)} jobs":
            return back(origin, err=M("Wrong confirmation: type exactly “supprimer {count} jobs”.", count=len(refs)))
    for ref in refs:
        try:
            if action == "delete":
                j = jobs.get(u, ref)
                jobs.delete_job(u, ref, jobs.delete_phrase(j))   # the grouped phrase was verified above
            elif action == "restore":
                jobs.archive(u, ref, restore=True)
            else:
                return back(origin, err=M("Unknown action."))
            done += 1
        except jobs.Refused as e:
            errors.append(f"{ref}: {e}")
    ok = M("{n} job(s) permanently deleted.", n=done) if action == "delete" else M("{n} job(s) restored.", n=done)
    return back(origin, ok=ok if done else None, err=" ".join(errors)[:400] or None)


@app.get("/jobs/{ref}/report.md")
def study_report(request: Request, ref: str):
    """Study report, in Markdown, to download (same permissions as job reading)."""
    u = user_of(request)
    try:
        j = jobs.get(u, ref)
    except jobs.Refused:
        return Response(i18n.tr(M("Job not found.")), status_code=404, media_type="text/plain; charset=utf-8")
    if not j or not j.get("report"):
        return Response(M("This job has no report."), status_code=404, media_type="text/plain; charset=utf-8")
    if not jobs.report_formats()["md"]:
        return Response(M("Markdown download is disabled."), status_code=404, media_type="text/plain; charset=utf-8")
    body = f"# {j['title']}\n\n_{j['ref']} · {i18n.tr(M('study delivered by Fabrique'))}_\n\n{j['report']}\n"
    name = re.sub(r"[^A-Za-z0-9-]", "", j["ref"]) + "-etude.md"
    return Response(body, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/jobs/{ref}/report.pdf")
def study_report_pdf(request: Request, ref: str):
    """Study report, in PDF, to download (same permissions as job reading)."""
    u = user_of(request)
    try:
        j = jobs.get(u, ref)
    except jobs.Refused:
        j = None
    if not j or not j.get("report"):
        return Response(M("This job has no report."), status_code=404, media_type="text/plain; charset=utf-8")
    if not jobs.report_formats()["pdf"]:
        return Response(M("PDF download is disabled."), status_code=404, media_type="text/plain; charset=utf-8")
    from . import pdf
    lt = time.localtime(j["updated_at"])
    meta = " · ".join(x for x in ((j.get("repo") or {}).get("slug"), j["ref"], i18n.tr(M("study delivered on {tm_mday} {p} {tm_year}", tm_mday=lt.tm_mday, p=i18n.catalogue(i18n.DEFAULT).meta['months'][lt.tm_mon - 1], tm_year=lt.tm_year))) if x)
    data = pdf.report(j["title"], meta, j["report"], footer=f"Fabrique · {j['ref']}")
    name = re.sub(r"[^A-Za-z0-9-]", "", j["ref"]) + "-etude.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/jobs/{ref}/diff", response_class=HTMLResponse)
def job_diff(request: Request, ref: str, sha: str = ""):
    """Diff of the job at the verified commit, split by file (fragment loaded by the job page; same permissions as the job)."""
    u = user_of(request)
    d = jobs.diff_view(u, ref, sha[:12])
    if not d:
        return HTMLResponse(M("<p class=\"muted\">This diff is no longer available: the verified commit has changed. Reload the page.</p>"), status_code=404)
    return templates.TemplateResponse(request, "_diff.html", {"d": d, "ref": ref}, headers={"Cache-Control": "private, no-store"})


@app.post("/jobs/{ref}/seen")
def seen_post(request: Request, ref: str):
    u = user_of(request)
    try:
        jobs.mark_seen(u, ref)
    except jobs.Refused as e:
        return back("/inbox", err=str(e))
    return back("/inbox", ok=M("Study {ref} marked as read.", ref=ref))


@app.get("/runners", response_class=HTMLResponse)
def runners_page(request: Request, new_token: str = "", new_name: str = ""):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, M("the runners"))
    with db.tx() as c:
        work.reconcile(c)
        rs = db.rows(c, select(db.runners).where(db.runners.c.revoked.is_(False)).order_by(db.runners.c.online.desc(), db.runners.c.name))
        if not jobs.demo_visible():
            rs = [r for r in rs if r["token_hash"]]
        pending = db.rows(c, select(db.intents).where(db.intents.c.state.in_(["pending", "leased"])))
    return render(request, "runners.html", "runners", runners=rs, pending=pending, new_token=new_token, new_name=new_name)


@app.post("/runners", response_class=HTMLResponse)
def runner_add(request: Request, name: str = Form(""), kind: str = Form(M("container"))):
    """The token is displayed only once, in this response (never stored in plain text, never in a URL)."""
    u = user_of(request)
    if not auth.can(u, "runners_edit"):
        return back("/runners", err=M("Only admins add a runner."))
    try:
        token = work.create_runner(name, kind, u["user"])
    except jobs.Refused as e:
        return back("/runners", err=str(e))
    resp = runners_page(request, new_token=token, new_name=name.strip())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/runners/{rid}/rotate", response_class=HTMLResponse)
def runner_rotate(request: Request, rid: int):
    """Renews a runner token; as during creation, it is only displayed once, in this response."""
    u = user_of(request)
    if not auth.can(u, "runners_edit"):
        return back("/runners", err=M("Only admins renew a token."))
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
        return back("/runners", err=M("Only admins revoke a runner."))
    try:
        work.revoke_runner(rid, u["user"])
    except jobs.Refused as e:
        return back("/runners", err=str(e))
    return back("/runners", ok=M("Runner revoked: its token no longer works."))


# ---------- Runners API (Bearer token, no session) ----------

def _runner(request):
    if config.RUNNER_API_INTERNAL and (request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip")):
        raise work.Denied("runner API restricted to the internal network")
    h = request.headers.get("authorization", "")
    r = work.runner_for(h[7:].strip() if h.lower().startswith("bearer ") else "")
    if not r:
        raise work.Denied("invalid or revoked runner token")
    return r


@app.exception_handler(work.Denied)
async def _denied(request, exc):
    return JSONResponse({"error": str(exc)}, status_code=403)


@app.post("/api/runner/heartbeat")
async def api_heartbeat(request: Request):
    r = _runner(request)
    res = await run_in_threadpool(work.heartbeat, r, i18n.revive_tree(await request.json()))
    return {"ok": True, "name": r["name"], **res}


@app.post("/api/runner/claim")
async def api_claim(request: Request):
    r = _runner(request)
    item = await run_in_threadpool(work.claim, r)
    return item or Response(status_code=204)


@app.post("/api/runner/jobs/{ref}/event")
async def api_event(request: Request, ref: str):
    r = _runner(request)
    body = i18n.revive_tree(await request.json())
    if not isinstance(body, dict):
        raise work.Denied("invalid event")
    res = await run_in_threadpool(work.report, r, ref, body)
    return {"ok": True, **({"decision": res} if res else {})}


@app.get("/repos", response_class=HTMLResponse)
def repos_page(request: Request, repo: int = 0):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, M("the repos"))
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
        onb = {}   # last onboarding job for each repo (status, what it is waiting for)
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
        return back("/repos", err=M("Only admins add a repo."))
    slug = slug.strip().strip("/")
    with db.tx() as c:
        gp = forges.resolve(c, provider)                         # specific provider; a single type designates the first of that type
    typ, gid = (gp["type"], gp["id"]) if gp else (provider, "")
    if typ not in forges.TYPES or not slug or "/" not in slug or len(slug) > 200 or any(ch in slug for ch in " \"'<>\\"):
        return back("/repos", err=M("Invalid provider or repo path (expected form: group/project)."))
    with db.tx() as c:
        if db.one(c, select(db.repos.c.id).where(db.repos.c.slug == slug, db.repos.c.git_id == gid)):
            return back("/repos", err=M("This repo is already known for this provider."))
        g = gp if gp and gp.get("token") else None
    if g:   # known token from the dashboard: the path is checked immediately, rather than letting the onboarding fail
        try:
            why = providers.git_repo_check(typ, g["base_url"], providers.unseal(g["token"]), slug)
        except providers.Invalid:
            why = None
        if why:
            return back("/repos", err=M("Repo not added: ") + why)
    with db.tx() as c:
        first = not db.one(c, select(db.repos.c.id))
        rid = c.execute(insert(db.repos).values(provider=typ, git_id=gid, slug=slug, name=(name.strip() or slug.split("/")[-1])[:120],
                                                is_default=first, onboarding="pending", settings={"merge_policy": "off"},
                                                created_at=time.time())).inserted_primary_key[0]
        db.log_audit(c, u["user"], M("Repo added"), target=slug, detail=M("onboarding requested"))
    try:
        ref = jobs.onboard(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=M("Repo added. Onboarding started ({ref}): the recipe will arrive as a PR, to review and merge.", ref=ref))


@app.post("/repos/request")
def repo_request(request: Request, provider: str = Form(""), name: str = Form(""), description: str = Form("")):
    u = user_of(request)
    try:
        rid = jobs.request_repo(u, provider, name, description)
    except jobs.Refused as e:
        return back("/repos", err=str(e))
    return back(f"/repos?repo={rid}", ok=M("Request recorded: an admin must approve it (inbox) before anything is created."))


@app.post("/repos/{rid}/decide")
def repo_decide(request: Request, rid: int, verdict: str = Form(""), note: str = Form("")):
    u = user_of(request)
    try:
        ref = jobs.decide_repo(u, rid, verdict == "approve", note)
    except jobs.Refused as e:
        return back(_safe_back(request, "/inbox"), err=str(e))
    return back(_safe_back(request, "/inbox"), ok=M("Creation approved ({ref}): the runner creates and initializes the repo.", ref=ref) if ref else M("Request refused."))


@app.post("/repos/{rid}/init")
def repo_init(request: Request, rid: int):
    u = user_of(request)
    try:
        ref = jobs.init_repo(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=M("Initialization approved ({ref}): the runner pushes the first commit.", ref=ref))


@app.post("/repos/{rid}/onboard")
def repo_onboard(request: Request, rid: int):
    u = user_of(request)
    try:
        ref = jobs.onboard(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back(f"/repos?repo={rid}", ok=M("Onboarding started ({ref}).", ref=ref))


@app.post("/repos/{rid}/remove")
def repo_remove(request: Request, rid: int):
    u = user_of(request)
    try:
        name = jobs.remove_repo(u, rid)
    except jobs.Refused as e:
        return back(f"/repos?repo={rid}", err=str(e))
    return back("/repos", ok=M("{name} is removed from Fabrique. Nothing was deleted on the Git host.", name=name))


@app.post("/repos/{rid}")
async def repo_save(request: Request, rid: int):
    u = user_of(request)
    if not auth.can(u, "repos_edit"):
        return back(f"/repos?repo={rid}", err=M("Only admins change a repo."))
    f = await request.form()
    with db.tx() as c:
        r = db.one(c, select(db.repos).where(db.repos.c.id == rid))
        if not r:
            return back("/repos", err=M("Repo not found."))
        if f.get("action") == "default":
            c.execute(update(db.repos).values(is_default=False))
            c.execute(update(db.repos).where(db.repos.c.id == rid).values(is_default=True))
            db.log_audit(c, u["user"], M("Default repo"), target=r["slug"])
            return back(f"/repos?repo={rid}", ok=M("{name} is the default repo.", name=r['name']))
        models, _ = db.get_setting(c, "models")
        s = dict(r["settings"] or {})
        harness, policy = f.get("harness", ""), f.get("merge_policy", "off")
        model, prof = profiles.parse_choice(f.get("choice")) if "choice" in f else (f.get("model", ""), str(f.get("profile") or ""))
        if harness and harness not in jobs.READY or model and model not in {m["id"] for m in models} or policy not in jobs.POLICIES \
                or prof and not profiles.get(c, prof):
            return back(f"/repos?repo={rid}", err=M("Invalid setting."))
        try:
            size = int(f.get("size_limit") or 0)
            budget = float(str(f.get("budget_job") or "0").replace(",", "."))
        except ValueError:
            return back(f"/repos?repo={rid}", err=M("Size and budget must be numbers."))
        if not (0 <= size <= 100000 and 0 <= budget <= 1000):
            return back(f"/repos?repo={rid}", err=M("Size or budget out of range."))
        paths = [p.strip() for p in str(f.get("protected_paths") or "").splitlines() if p.strip()][:100]
        s.update(harness=harness, model=model, profile=prof, merge_policy=policy, size_limit=size or None, budget_job=budget or None, protected_paths=paths,
                 tags=work.clean_tags(f.get("tags")))
        dropped = 0
        if f.get("diff_field") == "1":   # the checkbox is in this form: if unchecked, it is not sent
            keep = f.get("diff") == "1"
            if not keep and jobs.repo_keeps_diff(s):
                # Repo declared confidential: what has already been copied to the database is deleted immediately.
                ids = select(db.jobs.c.id).where(db.jobs.c.repo_id == rid)
                dropped = c.execute(db.diffs.delete().where(db.diffs.c.job_id.in_(ids))).rowcount or 0
                db.log_audit(c, u["user"], M("Diff turned off for the repo"), target=r["slug"], detail=M("{dropped} diff(s) deleted from the database", dropped=dropped))
            elif keep and not jobs.repo_keeps_diff(s):
                db.log_audit(c, u["user"], M("Diff turned on for the repo"), target=r["slug"])
            s["diff"] = keep
        if "lang" in f:                  # job language of this repo; empty: the application's
            wl = str(f.get("lang") or "")
            s["lang"] = wl if wl in i18n.LANGS else ""
        name = str(f.get("name") or r["name"]).strip()[:120] or r["name"]
        c.execute(update(db.repos).where(db.repos.c.id == rid).values(settings=s, name=name, accepts_jobs=f.get("accepts_jobs") == "1"))
        db.log_audit(c, u["user"], M("Repo settings"), target=r["slug"], detail=M("merge: {p}", p=jobs.POLICIES[policy][0]))
    return back(f"/repos?repo={rid}", ok=M("Repo settings saved."))


SECTIONS = [("agents", M("Agents and models")), ("git", M("Git providers")), ("oidc", M("Sign-in (OIDC)")), ("limits", M("Budgets and gates")),
            ("lang", M("Job language")), ("notifications", "Notifications"), ("users", M("Users and roles")), ("demo", M("Demo"))]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tab: str = "agents"):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return forbidden(request, M("the settings"))
    sections = [s for s in SECTIONS if s[0] != "demo" or config.DEMO]   # without FABRIQUE_DEMO=1, no Demo tab
    tab = tab if tab in dict(sections) else "agents"
    data = {}
    with db.tx() as c:
        for k in ("agents", "models", "git", "limits", "oidc", "access", "demo", "creation", "languages", "translation"):
            data[k] = db.get_setting(c, k)
        data["models"] = (providers.models_with_provider(c), data["models"][1])
        data["git"] = (forges.listing(c), data["git"][1])
        provs = providers.listing(c)
        chans = notify.channels(c)
        probes, _ = db.get_setting(c, "probes")
        loaded, load_err = None, ""
        lp = request.query_params.get("load", "")
        model_prices, prices_info = {}, dict(prices.fetched(c), off=not prices.URL)
        if tab == "agents" and lp:
            try:
                loaded = providers.fetch_models(c, lp)
            except providers.Invalid as e:
                load_err = str(e)
            pv = next((p for p in provs if p["id"] == lp), {})
            for m in loaded or []:
                model_prices[m["id"]] = prices.resolve(c, m["id"], None, local=(pv.get("type") == "compatible" and not pv.get("has_key")))
        users = db.rows(c, select(db.users).order_by(db.users.c.role.desc(), db.users.c.username))
        default_problem = providers.default_problem(c) if tab == "agents" else ""
        profs = profiles.listing(c)
        prof_usage = {p["id"]: profiles.usage(c, p["id"]) for p in profs} if tab == "agents" else {}
    return render(request, "settings.html", "settings", tab=tab, sections=sections, data=data, users=users, provs=provs,
                  ptypes=providers.TYPES, efforts=providers.EFFORTS, model_prices=model_prices, prices_info=prices_info, loaded=loaded, load_err=load_err, load_pid=lp, probes=probes or {},
                  edit=next((p for p in provs if p["id"] == request.query_params.get("edit", "")), None),
                  git_edit=next((g for g in data["git"][0] if g["id"] == request.query_params.get("editgit", "")), None),
                  default_problem=default_problem, profs=profs, prof_usage=prof_usage, ROLES=profiles.ROLES, pefforts=profiles.efforts(),
                  EXAMPLES=profiles.EXAMPLES, REVIEWERS=profiles.REVIEWERS,
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
        return back("/jobs", err=M("Only admins test a channel."))
    try:
        notify.send_test(cid)
    except notify.Invalid as e:
        return back("/settings?tab=notifications", err=str(e))
    return back("/settings?tab=notifications", ok=M("Test message sent."))


@app.post("/settings/{section}")
async def settings_save(request: Request, section: str):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err=M("Only admins change the settings."))
    f = await request.form()
    url = f"/settings?tab={section}"
    with db.tx() as c:
        if section == "agents":
            models = {x["id"]: x for x in providers.models_with_provider(c)}
            m, prof = profiles.parse_choice(f.get("choice")) if "choice" in f else (f.get("model", ""), str(f.get("profile") or ""))
            p = profiles.get(c, prof) if prof else None
            if prof and not p:
                return back(url, err=M("Unknown model profile."))
            if p:
                m = p["roles"]["agent"]["model"]     # the full profile: its agent model also serves as the default model
            h = f.get("harness", "")
            if h not in jobs.HARNESSES or m not in models:
                return back(url, err=M("Unknown agent or model."))
            if h not in jobs.READY:
                return back(url, err=M("{p} is not available yet.", p=jobs.HARNESSES[h]))
            if not providers.compatible(models[m], h):
                return back(url, err=M("{p} cannot use {name}: choose a compatible model.", p=jobs.HARNESSES[h], name=models[m]['name']))
            db.set_setting(c, "agents", {"harness": h, "model": m, "profile": prof}, u["user"])
            db.log_audit(c, u["user"], M("Settings: agents"), detail=f"{jobs.HARNESSES[h]} · {m}" + (f" · profile {prof}" if prof else ""))
        elif section == "profiles":
            url = "/settings?tab=agents"
            pid = str(f.get("pid") or "").strip()
            try:
                if f.get("action") == "remove":
                    profiles.remove(c, u["user"], pid)
                    return back(url + "#profiles", ok=M("Profile removed."))
                profiles.save(c, u["user"], pid if f.get("edit") == "1" else "", f.get("name", ""),
                              {**{k: {"model": f.get(f"model:{k}", "")} for k in profiles.ROLES},
                               **{f"review_{k}": {"model": f.get(f"model:review_{k}", "")} for k in profiles.REVIEWERS}}, new=f.get("edit") != "1")
            except profiles.Invalid as e:
                return back(url + "#profile", err=str(e))
            return back(url + "#profiles", ok=M("Profile saved."))
        elif section == "providers":
            act, pid = ("probe" if f.get("probe") else f.get("action", "")), str(f.get("pid") or "").strip()
            url = "/settings?tab=agents"
            try:
                if act == "save":
                    existed = any(p["id"] == pid.lower() for p in providers.listing(c))
                    # The stored identifier (lowercase) leads to the model list: "Claude" typed, "claude" stored.
                    pid = providers.save(c, u["user"], pid, f.get("type", ""), f.get("name", ""), f.get("base_url", ""), f.get("key", ""),
                                         clear_key=f.get("clear_key") == "1", effort=f.get("effort", ""),
                                         thinking=f.get("thinking"))
                    if existed:
                        return back(url, ok=M("Provider changed."))
                    return back(f"/settings?tab=agents&load={pid}", ok=M("Provider saved: tick the models to offer."))
                if act == "remove":
                    providers.remove(c, u["user"], pid)
                    return back(url, ok=M("Provider removed."))
                if act == "enable":
                    probes, _ = db.get_setting(c, "probes")
                    chosen = []
                    for mid in f.getlist("model")[:40]:
                        pr = ((probes or {}).get(pid) or {}).get(mid) or {}
                        apis = [a for a in ("anthropic", "openai") if pr.get(a)] if pr else []
                        chosen.append({"id": mid, "name": f.get(f"name:{mid}") or mid, "apis": apis, "max_jobs": f.get(f"max:{mid}"),
                                       "price": f.get(f"price:{mid}")})
                    providers.enable(c, u["user"], pid, chosen)
                    return back(url, ok=M("{count} model(s) offered for this provider.", count=len(chosen)))
                if act == "probe":
                    mid = str(f.get("probe") or "")[:120]
                    res = providers.probe(c, pid, mid)
                    probes, _ = db.get_setting(c, "probes")
                    probes = dict(probes or {})
                    probes.setdefault(pid, {})[mid] = dict(res, at=time.time())
                    db.set_setting(c, "probes", probes, u["user"])
                    ok = [lbl for api, lbl in (("anthropic", "Claude Code"), ("openai", "Codex")) if res[api]]
                    return back(f"/settings?tab=agents&load={pid}", ok=f"{mid} : " + (", ".join(ok) + " ✓" if ok else M("no tool call succeeded")))
            except providers.Invalid as e:
                return back(url, err=str(e))
            return back(url, err=M("Unknown action."))
        elif section == "channels":
            url = "/settings?tab=notifications"
            try:
                if f.get("action") == "remove":
                    notify.remove(c, u["user"], str(f.get("cid") or ""))
                    return back(url, ok=M("Channel removed."))
                notify.save(c, u["user"], f)
            except notify.Invalid as e:
                return back(url + (f"&chan={f.get('cid')}" if f.get("cid") else ""), err=str(e))
            return back(url, ok=M("Channel saved. Use “Send a test” to check."))
        elif section == "creation":
            ns = {p: re.sub(r"[^A-Za-z0-9._/-]", "", str(f.get(p) or "").strip().strip("/"))[:100] for p in forges.TYPES}
            db.set_setting(c, "creation", ns, u["user"])
            db.log_audit(c, u["user"], M("Settings: creation groups"), detail=", ".join(f"{k}={v or '—'}" for k, v in ns.items()))
            url = "/settings?tab=git"
        elif section == "git":
            # Several providers per type, each designated by its name (app/forges.py). The robot account token is encrypted
            # in the database, write-only; it is handed to the runner for each job of its repos. Without a token here, the runner
            # uses its environment variables.
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
                old = db.get_setting(c, "limits")[0] or {}
                if f.get("report_fields") == "1":   # both checkboxes are in this form: if unchecked, a checkbox is not sent
                    lim["report_md"], lim["report_pdf"] = f.get("report_md") == "1", f.get("report_pdf") == "1"
                else:
                    lim["report_md"], lim["report_pdf"] = old.get("report_md") is not False, old.get("report_pdf") is not False
            except (TypeError, ValueError):
                return back(url, err=M("Budgets and limits must be numbers."))
            if not (0 < lim["budget_job"] <= lim["budget_day"] <= 10000 and 1 <= lim["max_parallel"] <= 64 and 1 <= lim["size_limit"] <= 100000
                    and 2 <= lim["watchdog_min"] <= 240 and 0 <= lim["auto_archive_days"] <= 3650):
                return back(url, err=M("Values out of range (a job's budget cannot exceed the day's)."))
            db.set_setting(c, "limits", lim, u["user"])
            db.log_audit(c, u["user"], M("Settings: budgets and gates"), detail=M("${budget_job}/job · ${budget_day}/day", budget_job=lim['budget_job'], budget_day=lim['budget_day']))
        elif section == "access":
            d = f.get("default_role")
            if d not in ("requester", "none"):
                return back("/settings?tab=users", err=M("Invalid choice."))
            db.set_setting(c, "access", {"default_role": d}, u["user"])
            db.log_audit(c, u["user"], M("Settings: new accounts"), detail=auth.ROLE_LABEL[d])
            url = "/settings?tab=users"
        elif section == "translation":
            mid = str(f.get("model") or "")
            if mid and not any(m["id"] == mid for m in providers.models_with_provider(c)):
                return back("/settings?tab=agents", err=M("Unknown model."))
            db.set_setting(c, "translation", {"model": mid}, u["user"])
            db.log_audit(c, u["user"], M("Settings: translation model"), detail=mid or M("none", _ctx="model"))
            url = "/settings?tab=agents#translation"
        elif section == "lang":
            jl = str(f.get("jobs") or "")
            if jl not in i18n.LANGS:
                return back("/settings?tab=lang", err=M("Invalid choice."))
            db.set_setting(c, "languages", {"jobs": jl}, u["user"])
            db.log_audit(c, u["user"], M("Settings: job language"), detail=i18n.LANGS[jl])
        elif section == "demo":
            vis = f.get("visible") == "1"
            db.set_setting(c, "demo", {"visible": vis}, u["user"])
            db.log_audit(c, u["user"], M("Settings: demo"), detail=M("shown") if vis else M("hidden"))
            jobs.forget_demo()
            url = "/settings?tab=demo"
        elif section == "oidc":
            cur, _ = db.get_setting(c, "oidc")
            new = dict(db.DEFAULTS["oidc"], **(cur or {}))
            locked = auth.locked()
            for k in ("issuer", "client_id", "scopes", "groups_claim"):
                if k not in locked and k in f:
                    new[k] = str(f.get(k) or "").strip()[:300]
            for k in ("admin_groups", "reviewer_groups", "requester_groups"):
                if k not in locked and k in f:
                    new[k] = _lines(f.get(k))
            if new["issuer"] and urlsplit(new["issuer"]).scheme not in ("http", "https"):
                return back(url, err=M("The issuer must be an http(s) address."))
            if "openid" not in (new["scopes"] or "").split():
                return back(url, err=M("Scopes must contain “openid”."))
            sec = str(f.get("client_secret") or "").strip()
            if sec and not config.OIDC_CLIENT_SECRET:   # write-only; empty = keep the recorded value
                try:
                    db.set_setting(c, "oidc_secret", {"v": providers.seal(sec[:500])}, u["user"])
                except providers.Invalid as e:
                    return back(url, err=str(e))
            if new["issuer"] and not auth.has_local_admin(fresh=True) and not config.FABRIQUE_ADMINS and not config.OPEN:
                return back(url, err=M("First create a local admin account (Users and roles): it is the fallback if single sign-on is misconfigured."))
            db.set_setting(c, "oidc", new, u["user"])
            db.log_audit(c, u["user"], "Settings : connexion OIDC", detail=new["issuer"] or "mode local")
            auth.invalidate()
        else:
            return back("/settings", err=M("Unknown section."))
    return back(url, ok=M("Settings saved."))


@app.post("/settings-test/git")
async def git_test(request: Request):
    """Testing a Git provider before saving it: nothing is written. The entered token is used for the test; an empty field means
    the token already recorded for this type is used, and only for the address for which it was recorded."""
    u = user_of(request)
    if not auth.can(u, "settings"):
        return JSONResponse({"ok": False, "msg": M("Only admins test a provider.")}, status_code=403)
    f = await request.form()
    typ, base, tok, gid = (str(f.get(k) or "").strip() for k in ("type", "base_url", "token", "gid"))
    base = base.rstrip("/")[:200]
    if typ not in forges.TYPES or urlsplit(base).scheme != "https":
        return JSONResponse({"ok": False, "msg": M("Choose a type and enter an https address.")})
    if not tok:
        with db.tx() as c:
            old = forges.resolve(c, gid or typ) or {}
        if not old.get("token"):
            return JSONResponse({"ok": False, "msg": M("Enter the bot account's token to test it.")})
        if old.get("base_url") != base:
            return JSONResponse({"ok": False, "msg": M("The address has changed: enter the token again (a stored token is never sent to a new address).")})
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
        return back("/jobs", err=M("Only admins test sign-in."))
    ok, msg = await auth.discover(issuer or auth.conf()["issuer"])
    if "application/json" in request.headers.get("accept", ""):   # test from the page: the input is not lost
        return JSONResponse({"ok": ok, "msg": msg + ("" if not ok else M(" Remember to click “Save”."))})
    return back("/settings?tab=oidc", ok=msg if ok else None, err=None if ok else msg)


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    u = user_of(request)
    if not auth.can(u, "see_infra"):
        return forbidden(request, M("the activity log"))
    return render(request, "activity.html", "activity", rows=jobs.activity())


@app.post("/dev/as")
def dev_as(request: Request, role: str = Form("")):
    """Local mode only: see the interface with another role (demo). No effect if the OIDC connection is active."""
    if auth.mode() != "off":
        return back("/jobs", err=M("Unavailable while OIDC sign-in is active."))
    r = back(_safe_back(request, "/jobs"))
    if role in auth.ROLES:
        r.set_cookie("fabrique_as", role, httponly=True, samesite="lax", max_age=86400)
    else:
        r.delete_cookie("fabrique_as")
    return r


@app.get("/events/stream")
async def stream(request: Request):
    """Real time: a "change" message as soon as an event is written to the database (light polling every second)."""
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
                if idle >= 12:
                    idle = 0
                    yield "event: ping\ndata: 1\n\n"   # heard by the page: a silent leader tab is detected by the others
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.exception_handler(PendingAccess)
async def _pending(request, exc):
    if request.method == "GET" and not request.url.path.startswith(("/events/", "/api/")):
        return RedirectResponse("/access", status_code=303)
    return JSONResponse({"error": M("account waiting for approval")}, status_code=403)


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
        return back("/access", err=M("In local mode there is no account to upgrade."))
    if role not in auth.ROLES or (u["role"] in auth.ROLES and auth.ROLES.index(role) <= auth.ROLES.index(u["role"])):
        return back("/access" if u["role"] not in auth.ROLES else _safe_back(request, "/jobs"), err=M("Choose a role higher than your current one."))
    with db.tx() as c:
        c.execute(update(db.users).where(db.users.c.username == u["user"]).values(
            requested_role=role, request_note=note.strip()[:500], requested_at=time.time()))
        db.log_audit(c, u["user"], M("Access request"), target=auth.ROLE_LABEL[role], detail=note.strip()[:200])
    # From the menu window: return to the page from which the request came; account without a role: its "My access" page.
    to = "/access" if u["role"] not in auth.ROLES else _safe_back(request, "/jobs")
    return back(to, ok=M("Request sent. An admin will see it in their inbox."))


@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return forbidden(request, M("the setup page"))
    return render(request, "start.html", "start", steps=start_steps())


@app.post("/users/local")
async def user_local(request: Request):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err=M("Only admins create accounts."))
    f = await request.form()
    try:
        login = auth.create_local(f.get("login"), f.get("name"), str(f.get("password") or ""), str(f.get("role") or ""), u["user"])
    except ValueError as e:   # we keep the input (except the password): nothing to retype
        keep = urlencode({"lu_login": str(f.get("login") or "")[:40], "lu_name": str(f.get("name") or "")[:120], "lu_role": str(f.get("role") or "")[:20]})
        return back("/settings?tab=users&" + keep, err=str(e))
    return back("/settings?tab=users", ok=M("Local account “{login}” created.", login=login))


def _local_row(u):
    with db.tx() as c:
        row = db.one(c, select(db.users).where(db.users.c.username == (u or {}).get("user", "")))
    return row if row and row.get("password_hash") else None


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    """My account: accessible to all roles (the password of a local account does not depend on the Settings, which are reserved for admins)."""
    u = user_of(request)
    view_prefs(request, u)
    chosen = getattr(request.state, "account_lang", None) or request.cookies.get(i18n.COOKIE, "")
    return render(request, "account.html", "account", local_account=bool(_local_row(u)), chosen_lang=chosen if chosen in i18n.LANGS else "")


@app.post("/account/lang")
def account_lang(request: Request, lang: str = Form("")):
    """Interface language, chosen by each user. Kept on the account (it follows the person) and in a cookie."""
    u = user_of(request)
    lang = lang if lang in i18n.LANGS else ""
    if not u.get("local") and u.get("user"):
        with db.tx() as c:
            row = db.one(c, select(db.users.c.id, db.users.c.prefs).where(db.users.c.username == u["user"]))
            if row:
                prefs = dict(row["prefs"] or {})
                prefs.pop("lang", None)
                if lang:
                    prefs["lang"] = lang
                c.execute(update(db.users).where(db.users.c.id == row["id"]).values(prefs=prefs))
    r = back(_safe_back(request, "/jobs"), ok=M("Language saved."))
    if lang:
        _lang_cookie(r, lang)
    else:
        r.delete_cookie(i18n.COOKIE)
    return r


@app.post("/account/password")
async def account_password(request: Request):
    u = user_of(request)
    f = await request.form()
    row = _local_row(u)
    if not row:
        return back("/account", err=M("This account has no local password."))
    if not auth.check_password(str(f.get("old") or ""), row["password_hash"]):
        return back("/account", err=M("Wrong current password."))
    try:
        auth.set_password(row["username"], str(f.get("new") or ""), u["user"])
    except ValueError as e:
        return back("/account", err=str(e))
    # Other sessions for the account are closed; this one is reopened so as not to disconnect the person who just changed it.
    auth.revoke_sessions(row["username"], u["user"], M("password changed"))
    if request.scope.get("session"):
        request.session["iat"] = time.time()
    return back("/account", ok=M("Password changed. This account's other sessions are closed."))


@app.post("/users/{uid}/logout")
def user_logout(request: Request, uid: int):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err=M("Only admins close an account's sessions."))
    with db.tx() as c:
        target = db.one(c, select(db.users).where(db.users.c.id == uid))
    if not target:
        return back("/settings?tab=users", err=M("Account not found."))
    auth.revoke_sessions(target["username"], u["user"], M("closed by an admin"))
    return back("/settings?tab=users", ok=M("Sessions of {p} closed: they will have to sign in again.", p=target['name'] or target['username']))


@app.post("/users/{uid}/role")
def user_role(request: Request, uid: int, role: str = Form(""), refuse: int = Form(0)):
    u = user_of(request)
    if not auth.can(u, "settings"):
        return back("/jobs", err=M("Only admins change roles."))
    with db.tx() as c:
        target = db.one(c, select(db.users).where(db.users.c.id == uid))
        if not target:
            return back("/settings?tab=users", err=M("Account not found."))
        if refuse:
            c.execute(update(db.users).where(db.users.c.id == uid).values(requested_role=None, request_note=None, requested_at=None))
            db.log_audit(c, u["user"], M("Access request refused"), target=target["username"])
            return back(_safe_back(request, "/settings?tab=users"), ok=M("Request from {name} refused.", name=target['name']))
        if role not in ("none",) + auth.ROLES:
            return back("/settings?tab=users", err=M("Unknown role."))
        if target["username"] == u["user"]:
            return back("/settings?tab=users", err=M("You cannot change your own role: ask another admin."))
        c.execute(update(db.users).where(db.users.c.id == uid).values(role=role, role_source="dashboard", requested_role=None,
                                                                      request_note=None, requested_at=None))
        db.log_audit(c, u["user"], M("Role changed"), target=target["username"], detail=f"{auth.ROLE_LABEL[target['role']]} → {auth.ROLE_LABEL[role]}")
    auth.forget_role(target["username"])
    return back(_safe_back(request, "/settings?tab=users"), ok=f"{target['name']} : {auth.ROLE_LABEL[role]}.")


@app.exception_handler(PermissionError)
async def _no_user(request, exc):
    return JSONResponse({"error": "sign-in required"}, status_code=401)
