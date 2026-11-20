"""Côté dashboard du travail réel : file de commandes (intents) pour les runners, événements remontés par les runners,
barrières de merge évaluées par le code (jamais par l'agent).

Un runner s'authentifie par son token (empreinte en base), prend une commande avec un bail, et ne peut écrire que sur les
jobs dont il détient une commande. Un bail expiré (runner muet) remet la commande en file ; après 3 essais, le job échoue."""
import os, hashlib, re, secrets, time
from sqlalchemy import and_, insert, or_, select, update
from . import config, db, jobs, notify

LEASE = 600            # secondes ; prolongé à chaque battement de cœur du runner
MAX_ATTEMPTS = 3
AGENT_STAGES = {"triage", "investigating", "implementing"}   # étapes que l'agent peut annoncer lui-même
import logging
log = logging.getLogger("fabrique.work")


class Denied(Exception):
    pass


# ---------- Jetons des runners ----------

def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_runner(name, kind, by):
    """Crée un runner et renvoie son token, affiché une seule fois."""
    name = name.strip()[:60]
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,59}", name):
        raise jobs.Refused("Nom de runner invalide (minuscules, chiffres, . _ -).")
    token = "fab_" + secrets.token_urlsafe(32)
    with db.tx() as c:
        if db.one(c, select(db.runners.c.id).where(db.runners.c.name == name)):
            raise jobs.Refused("Ce nom de runner existe déjà.")
        c.execute(insert(db.runners).values(name=name, host="", kind=kind if kind in ("vm", "conteneur", "kubernetes") else "conteneur",
                                            online=False, capacity=1, harnesses=[], metrics={}, version="", token_hash=_hash(token)))
        db.log_audit(c, by, "Runner ajouté", target=name)
    return token


def rotate_runner(rid, by):
    """Nouveau token pour un runner existant (même nom, mêmes réglages) ; l'ancien cesse de fonctionner tout de suite."""
    token = "fab_" + secrets.token_urlsafe(32)
    with db.tx() as c:
        r = db.one(c, select(db.runners).where(db.runners.c.id == rid, db.runners.c.revoked.is_(False)))
        if not r or not r["token_hash"]:
            raise jobs.Refused("Runner introuvable.")
        c.execute(update(db.runners).where(db.runners.c.id == rid).values(token_hash=_hash(token), online=False))
        db.log_audit(c, by, "Token du runner renouvelé", target=r["name"])
    # Runner intégré (tout-en-un) : si le fichier partagé contient bien l'ancien token de CE runner, il suit ; sinon il
    # resterait avec un token mort jusqu'à une main humaine. Le runner le relit à son prochain démarrage.
    path = config.BOOTSTRAP_RUNNER_FILE
    try:
        if path and os.path.exists(path) and _hash(open(path).read().strip()) == r["token_hash"]:
            _write_bootstrap(path, token)
    except OSError as e:
        log.error("runner intégré : impossible de mettre à jour %s (%s)", path, e)
    return r["name"], token


def revoke_runner(rid, by):
    with db.tx() as c:
        r = db.one(c, select(db.runners).where(db.runners.c.id == rid))
        if not r:
            raise jobs.Refused("Runner introuvable.")
        c.execute(update(db.runners).where(db.runners.c.id == rid).values(revoked=True, online=False, token_hash=None))
        c.execute(update(db.intents).where(db.intents.c.runner_id == rid, db.intents.c.state == "leased")
                  .values(state="pending", runner_id=None, lease_until=None))
        db.log_audit(c, by, "Runner révoqué", target=r["name"])


def _write_bootstrap(path, tok):
    """Fichier du token du runner intégré : lisible par le propriétaire (le dashboard) et par son groupe, que le runner
    reçoit en groupe supplémentaire (compose : group_add) et que l'utilisateur de l'agent n'a pas. Jamais lisible par tous."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    _tighten(path)


def _tighten(path):
    """0640, groupe du dashboard : vaut aussi pour un fichier créé par une ancienne version (0644, autre groupe)."""
    try:
        os.chown(path, -1, os.getgid())
    except OSError:
        pass
    os.chmod(path, 0o640)


INTEGRATED = "runner-integre"


def bootstrap_runner_token():
    """Kubernetes : enregistre le runner intégré avec le token fourni par l'environnement (un Secret donné aussi au runner).
    Une seule fois : si un runner intégré existe déjà, rien ne change — un token renouvelé dans l'interface n'est donc
    jamais remplacé par l'ancien au redémarrage (il faut alors reporter le nouveau token dans le Secret)."""
    tok = config.BOOTSTRAP_RUNNER_TOKEN
    if not tok:
        return
    if not re.fullmatch(r"fab_[A-Za-z0-9_-]{32,200}", tok):
        log.error("runner intégré : FABRIQUE_BOOTSTRAP_RUNNER_TOKEN invalide (attendu : fab_ suivi d'au moins 32 caractères)")
        return
    with db.tx() as c:
        if db.one(c, select(db.runners.c.id).where(db.runners.c.name == INTEGRATED)):
            return
        c.execute(insert(db.runners).values(name=INTEGRATED, host="", kind="kubernetes", online=False, capacity=1, harnesses=[],
                                            metrics={}, version="", token_hash=_hash(tok)))
        db.log_audit(c, "fabrique", "Runner ajouté", target=INTEGRATED, detail="token fourni par l'environnement")
    log.info("runner intégré « %s » enregistré (token fourni par l'environnement)", INTEGRATED)


def bootstrap_runner():
    """Compose tout-en-un : crée le runner intégré une fois et dépose son token dans le fichier partagé."""
    bootstrap_runner_token()
    path = config.BOOTSTRAP_RUNNER_FILE
    if not path:
        return
    try:
        if os.path.exists(path) and open(path).read().strip():
            _tighten(path)   # fichier créé par une ancienne version (0644) : resserré au démarrage
            return
        with db.tx() as c:
            taken = {r["name"] for r in db.rows(c, select(db.runners.c.name))}
        name = next(n for n in ["runner-integre"] + [f"runner-integre-{i}" for i in range(2, 50)] if n not in taken)
        tok = create_runner(name, "conteneur", "fabrique")
        _write_bootstrap(path, tok)
        log.info("runner intégré « %s » enregistré (token déposé pour le runner)", name)
    except OSError as e:
        log.error("runner intégré : impossible d'écrire %s (%s)", path, e)


def runner_for(token):
    if not token or not token.startswith("fab_"):
        return None
    with db.tx() as c:
        return db.one(c, select(db.runners).where(db.runners.c.token_hash == _hash(token), db.runners.c.revoked.is_(False)))


# ---------- File de commandes ----------

def enqueue(c, job_id, kind, payload=None):
    c.execute(insert(db.intents).values(job_id=job_id, kind=kind, payload=payload or {}, state="pending", created_at=time.time()))


PAUSED = "Budget du jour atteint : reprise demain, ou dès que le budget est relevé"


def spent_today(c):
    """Dépense du jour (jobs touchés depuis minuit), pour le budget par jour."""
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return sum(r["cost"] or 0 for r in db.rows(c, select(db.jobs.c.cost).where(db.jobs.c.updated_at >= start, db.jobs.c.source != "démo")))


def clean_tags(v):
    """Tags : minuscules, chiffres, tirets ; au plus 10."""
    raw = v if isinstance(v, list) else str(v or "").replace("\n", ",").split(",")
    return sorted({t for t in (str(x).strip().lower()[:30] for x in raw) if re.fullmatch(r"[a-z0-9][a-z0-9-]*", t)})[:10]


def runner_fits(runner_tags, repo):
    """Un repo qui exige des tags ne va qu'à un runner qui les porte toutes."""
    need = set((repo["settings"] or {}).get("tags") or [])
    return need <= set(runner_tags or [])


_last_auto_archive = 0.0


def heartbeat(runner, info):
    """Enregistre l'état du runner ; renvoie les jobs qu'il doit arrêter."""
    t = time.time()
    with db.tx() as c:
        c.execute(update(db.runners).where(db.runners.c.id == runner["id"]).values(
            online=True, last_seen=t, host=str(info.get("host") or "")[:200], version=str(info.get("version") or "")[:40],
            capacity=max(1, min(16, int(info.get("capacity") or 1))), in_flight=max(0, int(info.get("in_flight") or 0)),
            harnesses=[{"name": str(h.get("name"))[:40], "version": str(h.get("version"))[:40]} for h in (info.get("harnesses") or [])[:8]],
            metrics={k: info.get("metrics", {}).get(k) for k in ("cpu", "mem", "disk")},
            tags=clean_tags(info.get("tags"))))
        c.execute(update(db.intents).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased")
                  .values(lease_until=t + LEASE))
        held = select(db.intents.c.job_id).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased")
        stop = [r["ref"] for r in db.rows(c, select(db.jobs.c.ref).where(db.jobs.c.cancel_requested.is_not(None), db.jobs.c.id.in_(held)))]
        # Budget du jour dépassé : les passages d'agent en cours s'arrêtent (les merges déjà décidés passent).
        limits, _ = db.get_setting(c, "limits")
        over = []
        if float(limits.get("budget_day") or 0) > 0 and spent_today(c) >= float(limits["budget_day"]):
            working = select(db.intents.c.job_id).where(db.intents.c.runner_id == runner["id"], db.intents.c.state == "leased",
                                                        db.intents.c.kind.in_(("start", "resume", "onboard")))
            over = [r["ref"] for r in db.rows(c, select(db.jobs.c.ref).where(db.jobs.c.id.in_(working)))]
    # Ménage : le runner annonce de temps en temps ses dossiers de travail ; on lui dit lesquels ne servent plus (job
    # supprimé, ou terminé depuis plus d'une heure). Et l'archivage automatique passe au plus une fois par heure.
    purge = jobs.workspaces_to_purge(info.get("workspaces")) if info.get("workspaces") else []
    global _last_auto_archive
    if t - _last_auto_archive > 3600:
        _last_auto_archive = t
        try:
            jobs.auto_archive(t)
        except Exception as e:   # ne doit jamais empêcher un battement de cœur
            log.warning("archivage automatique impossible (%s)", type(e).__name__)
    return {"cancel": stop, "over_budget": over, "purge": purge}


ONBOARD_RETRY = (120, 900)       # nouvel essai d'un onboarding en attente : 2 min la première demi-heure, puis 15 min
ONBOARD_GIVE_UP = 24 * 3600      # au-delà, l'onboarding échoue (on peut toujours le relancer)


def _retry_onboarding(c, t):
    """Onboardings en attente (accès, repo vide) : remis en file tout seuls, sur le même job."""
    q = (select(db.jobs).join(db.repos, db.repos.c.id == db.jobs.c.repo_id)
         .where(db.jobs.c.source == "onboarding", db.repos.c.onboarding.in_(["waiting", "empty"]), db.jobs.c.stage == "queued",
                db.jobs.c.archived.is_(False)))
    for j in db.rows(c, q):
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            continue
        age = t - j["created_at"]
        if age > ONBOARD_GIVE_UP:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                             status_text="Onboarding abandonné après 24 h d'attente : relance-le depuis le repo"))
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="failed"))
            continue
        if t - j["updated_at"] >= ONBOARD_RETRY[0 if age < 1800 else 1]:
            enqueue(c, j["id"], "onboard")
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(updated_at=t))


def reconcile(c):
    """Runners muets depuis 2 minutes : hors ligne. Baux expirés : commande remise en file, ou job en échec après 3 essais."""
    t = time.time()
    _retry_onboarding(c, t)
    c.execute(update(db.runners).where(db.runners.c.online.is_(True), db.runners.c.last_seen < t - 120).values(online=False))
    for it in db.rows(c, select(db.intents).where(db.intents.c.state == "leased", db.intents.c.lease_until < t)):
        if it["attempts"] >= MAX_ATTEMPTS:
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="failed", done_at=t, note="bail expiré trop souvent"))
            c.execute(update(db.jobs).where(db.jobs.c.id == it["job_id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                                  status_text="Runner perdu pendant le travail"))
            db.log_audit(c, "fabrique", "Échec", it["job_id"], detail="le runner a cessé de répondre (3 essais)")
        else:
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="pending", runner_id=None, lease_until=None))
            db.log_event(c, it["job_id"], "runner", "bail expiré : commande remise en file")


def claim(runner):
    """La plus ancienne commande en attente, avec tout ce qu'il faut pour l'exécuter ; None s'il n'y a rien."""
    t = time.time()
    with db.tx() as c:
        reconcile(c)
        limits, _ = db.get_setting(c, "limits")
        q = select(db.intents).where(db.intents.c.state == "pending").order_by(db.intents.c.id)
        if spent_today(c) >= float(limits["budget_day"]):
            # Budget du jour atteint : seules les fusions déjà décidées passent ; le reste attend demain.
            for r in db.rows(c, select(db.intents.c.job_id).where(db.intents.c.state == "pending", db.intents.c.kind != "merge")):
                c.execute(update(db.jobs).where(db.jobs.c.id == r["job_id"], db.jobs.c.status_text != PAUSED)
                          .values(status_text=PAUSED))
            q = q.where(db.intents.c.kind == "merge")
        me = db.one(c, select(db.runners).where(db.runners.c.id == runner["id"]))
        it = None
        for cand in db.rows(c, q.limit(100)):
            rj = db.one(c, select(db.jobs.c.repo_id).where(db.jobs.c.id == cand["job_id"]))
            rp = db.one(c, select(db.repos).where(db.repos.c.id == rj["repo_id"])) if rj else None
            if rp is None or runner_fits((me or {}).get("tags"), rp):
                it = cand
                break
        if not it:
            return None
        n = c.execute(update(db.intents).where(db.intents.c.id == it["id"], db.intents.c.state == "pending").values(
            state="leased", runner_id=runner["id"], lease_until=t + LEASE, attempts=it["attempts"] + 1)).rowcount
        if not n:
            return None
        j = db.one(c, select(db.jobs).where(db.jobs.c.id == it["job_id"]))
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        from . import providers, profiles
        # Avec la clé déchiffrée : réseau privé panneau ↔ runner seulement. Effort et relecteurs : ceux du profil, figés
        # à la création du job (jobs.models) ; un job d'avant les profils n'a qu'un modèle.
        mx = j.get("models") or {}
        model = profiles.apply_effort(providers.runtime(c, j["model"]), mx.get("effort"))
        review_model = (profiles.apply_effort(providers.runtime(c, mx["review_model"]), mx.get("review_effort")) if mx.get("review_model")
                        else profiles.apply_effort(model, mx.get("review_effort")) if mx.get("review_effort") not in (None, profiles.INHERIT, mx.get("effort"))
                        else None)
        # Fournisseur Git du dépôt, si son jeton est réglé dans le panneau (sinon le runner utilise ses variables).
        from . import forges   # le provider DE CE repo (jamais un autre provider du même type), s'il a un token
        g = forges.for_repo(c, repo)
        g = g if g and g.get("token") else None
        forge = {"type": g["type"], "base_url": g["base_url"], "token": providers.unseal(g["token"])} if g else None
        if it["kind"] == "onboard" and not jobs.is_blank(repo):   # un repo vierge reste vierge (et utilisable) pendant son onboarding
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="analyzing"))
        if it["kind"] == "resume" and (it["payload"] or {}).get("pause"):
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(updated_at=t, activity="active",
                                                                             status_text="Reprise après la pause par " + runner["name"]))
        if it["kind"] in ("start", "onboard", "init", "create"):
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(runner_id=runner["id"], updated_at=t, activity="active",
                                                                             status_text="Pris en charge par " + runner["name"]))
        db.log_event(c, j["id"], "runner", f"{runner['name']} prend la commande « {it['kind']} »")
    rs = repo["settings"] or {}
    return {"intent": {"id": it["id"], "kind": it["kind"], "payload": it["payload"], "attempt": it["attempts"] + 1},
            "job": {k: j[k] for k in ("ref", "title", "request", "done_when", "requester", "source", "branch", "harness", "budget",
                                      "session_id", "verified_sha", "pr_number", "pr_url", "attempts", "cost", "tokens")},
            "model": model,
            "review_model": review_model,
            "forge": forge,
            "repo": {"provider": repo["provider"], "slug": repo["slug"], "default_branch": repo["default_branch"],
                     "description": str(rs.get("description") or "")[:300],
                     "protected_paths": rs.get("protected_paths") or limits["protected_paths"],
                     "size_limit": rs.get("size_limit") or limits["size_limit"]},
            "limits": {"watchdog_min": int(limits.get("watchdog_min") or 10)}}


def _owned(c, runner, ref):
    """Le job, si ce runner détient une commande en cours dessus ; sinon refus."""
    j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
    if not j:
        raise Denied("job inconnu")
    it = db.one(c, select(db.intents).where(db.intents.c.job_id == j["id"], db.intents.c.runner_id == runner["id"],
                                            db.intents.c.state == "leased"))
    if not it:
        raise Denied("aucune commande en cours de ce runner sur ce job")
    return j, it


# ---------- Barrières de fusion (code, pas agent) ----------

ROLES = {"code": "code", "securite": "sécurité", "archi": "architecture", "infra": "infra et exploitation"}
VERDICTS = {"ok": "OK", "a_revoir": "à revoir", "bloquant": "bloquant", "indetermine": "indéterminé"}

def _glob_re(pat):
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pat.startswith("**", i):
            out += ".*"; i += 2
        elif pat[i] == "*":
            out += "[^/]*"; i += 1
        elif pat[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pat[i]); i += 1
    return re.compile(out + r"\Z", re.DOTALL)   # DOTALL : « ** » couvre aussi un nom de fichier contenant un retour à la ligne


MAX_FILES, MAX_PATH = 500, 300   # liste des fichiers d'une PR gardée par le dashboard : au-delà, la barrière ne se fie à rien


def protected_hits(files, patterns):
    """Fichiers protégés touchés. Un chemin que la barrière ne peut pas comparer fidèlement compte comme touché, pour
    qu'un humain décide : chemin qui n'est pas brut (guillemets de git, renommage « a => b », accolades, antislash),
    chemin coupé à la longueur gardée, caractère de contrôle (retour à la ligne, tabulation…)."""
    res = [_glob_re(p.strip().lstrip("/")) for p in patterns if p.strip()]
    odd = lambda f: (f.startswith('"') or " => " in f or "{" in f or "\\" in f or len(f) >= MAX_PATH
                     or any(ord(ch) < 32 or ord(ch) == 127 for ch in f))
    return sorted({f for f in files if odd(f) or any(r.match(f) for r in res)})


def evaluate(c, j, repo):
    """Barrières évaluées sur l'état réel : preuves au sha de la PR, essais, protected paths, taille, avis de l'agent."""
    limits, _ = db.get_setting(c, "limits")
    rs = repo["settings"] or {}
    diff = j["diff"] or {}
    files = diff.get("files") or []
    size = int(diff.get("additions") or 0) + int(diff.get("deletions") or 0)
    size_limit = int(rs.get("size_limit") or limits["size_limit"])
    hits = protected_hits(files, (rs.get("protected_paths") or limits["protected_paths"]) + [".fabrique/**"])
    if diff.get("truncated"):   # liste incomplète : un fichier protégé peut se trouver dans ce qui manque
        hits = [f"plus de {MAX_FILES} fichiers : liste incomplète"] + hits
    ev = db.rows(c, select(db.evidence).where(db.evidence.c.job_id == j["id"], db.evidence.c.sha == (j["verified_sha"] or ""),
                                              db.evidence.c.ran_by == "runner"))
    ok_checks = bool(ev) and all(e["ok"] for e in ev)
    d = db.one(c, select(db.decisions).where(db.decisions.c.job_id == j["id"]))
    rv = db.rows(c, select(db.reviews).where(db.reviews.c.job_id == j["id"], db.reviews.c.sha == (j["verified_sha"] or "")))
    roles = {r["role"] for r in rv}
    rv_ok = {"code", "securite"} <= roles and all(r["verdict"] == "ok" for r in rv)
    rv_detail = ", ".join(f"{ROLES.get(r['role'], r['role'])} : {VERDICTS.get(r['verdict'], r['verdict'])}" for r in rv) or "aucune vérification"
    reviews = [] if j["source"] == "onboarding" else [{"name": "Vérification", "ok": rv_ok, "detail": rv_detail}]
    return [
        {"name": "Checks verts", "ok": ok_checks,
         "detail": f"{sum(e['ok'] for e in ev)}/{len(ev)} au sha {j['verified_sha'] or '—'}, relancés par le runner" if ev
                   else "aucun check dans la recipe (repo vierge ou sans tests) : vérification humaine"},
        *reviews,
        {"name": "Premier essai", "ok": (j["attempts"] or 0) <= 1, "detail": "aucune nouvelle tentative" if (j["attempts"] or 0) <= 1 else f"{j['attempts']} tentatives"},
        {"name": "Protected paths", "ok": not hits, "detail": "aucun fichier protégé touché" if not hits else "touche " + ", ".join(hits[:4])},
        {"name": "Taille", "ok": size <= size_limit, "detail": f"{size} lignes sur {size_limit}"},
        {"name": "Avis de l'agent", "ok": bool(d and d["confident"]), "detail": "confiant" if d and d["confident"] else "pas confiant ou pas d'avis"},
    ]


# ---------- Événements envoyés par le runner ----------

def _stage(c, j, stage, summary, status=""):
    t = time.time()
    c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage=stage, updated_at=t, status_text=status[:240] or jobs.STAGE[stage]["label"],
                                                                     activity="active" if stage in jobs.ACTIVE else "idle"))
    c.execute(insert(db.timeline).values(job_id=j["id"], stage=stage, at=t, summary=summary[:2000]))
    db.log_event(c, j["id"], stage, jobs.STAGE[stage]["label"])


def _txt(v, n=2000):
    return str(v or "").strip()[:n]


def _assumptions(v):
    """Hypothèses de l'agent : toujours une liste de phrases. Une chaîne reçue telle quelle (ancien runner) reste une seule
    hypothèse au lieu d'être découpée en lettres."""
    if isinstance(v, str):
        v = [v]
    return [t for t in (_txt(a, 400) for a in (v if isinstance(v, list) else [])[:10]) if t]


def onboard_wait_text(repo, reason):
    """Ce que l'humain doit faire côté provider pour que l'onboarding reprenne (nouvel essai automatique)."""
    br = repo["default_branch"]
    if reason == "vide":
        return (f"Rien à analyser : le repo est vide, ou ne contient qu'un README ou une licence. Initialise-le depuis la page du "
                f"repo (la Fabrique pousse une recipe vide sur {br}), ou pousse toi-même le code du projet ; la Fabrique réessaie toute seule.")
    if reason == "branche":
        return f"En attente : la branche {br} n'existe pas. Crée-la, ou corrige la branche principale du repo ; nouvel essai automatique."
    who = reason.split(":", 1)[1] or "du runner"
    how = {"forgejo": f"ajoute « {who} » en collaborateur avec le droit Écriture",
           "gitea": f"ajoute « {who} » en collaborateur avec le droit Écriture",
           "gitlab": f"donne à « {who} » le rôle Developer sur le projet (ou utilise un token de groupe)",
           "github": "ajoute ce repo au token fin du runner (Contents, Pull requests, Issues : lecture et écriture)"}.get(repo["provider"], "donne-lui l'accès en écriture")
    return f"En attente d'accès : le compte du runner ne voit pas ce repo. {how[0].upper() + how[1:]} ; la Fabrique réessaie toute seule."


def _recipe(r):
    """Copie bornée de la recipe remontée par le runner (affichage seulement : le runner relit toujours la branche principale)."""
    return {**({"image": _txt(r["image"], 20)} if r.get("image") else {}),
            "setup": [_txt(x, 500) for x in (r.get("setup") or [])[:20]],
            "checks": {_txt(k, 20): _txt(v, 500) for k, v in list((r.get("checks") or {}).items())[:8]},
            "env": [{"name": _txt(e.get("name"), 64), "purpose": _txt(e.get("purpose"), 200), "required": bool(e.get("required"))}
                    for e in (r.get("env") or [])[:30] if isinstance(e, dict)]}


def report(runner, ref, ev):
    """Applique un événement du runner. Types : stage, question, paused, study, decision, evidence, pr, usage, log, intent_done, merged, failed."""
    kind = ev.get("type")
    t = time.time()
    result = None
    with db.tx() as c:
        j, it = _owned(c, runner, ref)
        jid = j["id"]
        if kind == "stage":
            st = ev.get("stage")
            if st not in AGENT_STAGES | {"verifying", "review"}:
                raise Denied("étape refusée")
            _stage(c, j, st, _txt(ev.get("summary")), _txt(ev.get("status"), 240))
        elif kind == "question":
            q = _txt(ev.get("question"), 4000)
            if not q:
                raise Denied("question vide")
            c.execute(insert(db.questions).values(job_id=jid, question=q, asked_at=t))
            _stage(c, j, "waiting", "Question posée au requester.", "Question posée")
            notify.emit(c, "question", job=j, detail=q)
            if ev.get("session_id"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(session_id=_txt(ev["session_id"], 80)))
        elif kind == "paused":
            # Budget du jour atteint en cours de passage : le job n'échoue pas, une reprise attend dans la file (claim la
            # retient tant que le budget du jour est dépassé).
            if ev.get("session_id"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(session_id=_txt(ev["session_id"], 80)))
            if not db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == jid, db.intents.c.state == "pending")):
                enqueue(c, jid, "resume", {"pause": "budget"})
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(activity="idle", status_text=PAUSED))
            db.log_event(c, jid, "runner", "budget du jour atteint : agent mis en pause, le job reprendra où il en était")
            db.log_audit(c, "fabrique", "Job en pause", jid, detail="budget du jour atteint")
        elif kind == "study":
            if j["source"] != "étude":
                raise Denied("ce job n'est pas une étude")
            text = _txt(ev.get("report"), 40000)
            if not text:
                raise Denied("rapport vide")
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(report=text))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            _stage(c, j, "done", "Rapport d'étude rendu : rien n'a été poussé, aucune PR.", "Étude rendue")
            db.log_audit(c, "fabrique", "Étude rendue", jid, detail=f"{len(text)} caractères")
            notify.emit(c, "study", job=j, detail="Le rapport d'étude est disponible dans le job.")
        elif kind == "decision":
            vals = dict(recommendation=_txt(ev.get("recommendation"), 240), interpretation=_txt(ev.get("interpretation"), 4000),
                        assumptions=_assumptions(ev.get("assumptions")), confident=bool(ev.get("confident")),
                        verdict=_txt(ev.get("verdict"), 1000), notes=_txt(ev.get("notes"), 2000), at=t)
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            if db.one(c, select(db.decisions.c.job_id).where(db.decisions.c.job_id == jid)):
                c.execute(update(db.decisions).where(db.decisions.c.job_id == jid).values(policy=policy, **vals))
            else:
                c.execute(insert(db.decisions).values(job_id=jid, policy=policy, limits=[], **vals))
            db.log_event(c, jid, "décision", "avis de l'agent : " + ("confiant" if vals["confident"] else "pas confiant"))
        elif kind == "evidence":
            sha = _txt(ev.get("sha"), 40)
            checks = ev.get("checks") or []
            if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or not isinstance(checks, list):
                raise Denied("preuve invalide")
            c.execute(db.evidence.delete().where(db.evidence.c.job_id == jid, db.evidence.c.sha == sha[:12]))
            for ch in checks[:20]:
                c.execute(insert(db.evidence).values(job_id=jid, sha=sha[:12], name=_txt(ch.get("name"), 60), command=_txt(ch.get("command"), 300),
                                                     ok=bool(ch.get("ok")), duration_ms=int(ch.get("duration_ms") or 0),
                                                     output=_txt(ch.get("output"), 20000), ran_by="runner", at=t))
            ok = sum(1 for ch in checks if ch.get("ok"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(verified_sha=sha[:12], checks_ok=ok, checks_total=len(checks), updated_at=t))
            db.log_event(c, jid, "preuve", f"{ok}/{len(checks)} checks au sha {sha[:12]}, relancés par le runner")
        elif kind == "pr":
            n, url = int(ev.get("number") or 0), _txt(ev.get("url"), 300)
            if n <= 0 or not url.startswith(("https://", "http://")):
                raise Denied("PR invalide")
            raw = ev.get("files") or []
            if not isinstance(raw, list):
                raise Denied("PR invalide")
            # Chemins gardés tels que git les donne (ni nettoyés ni silencieusement écartés) ; ce qui dépasse est signalé.
            diff = {"files": [str(f)[:MAX_PATH] for f in raw[:MAX_FILES]], "truncated": len(raw) > MAX_FILES,
                    "additions": int(ev.get("additions") or 0), "deletions": int(ev.get("deletions") or 0)}
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(pr_number=n, pr_url=url, diff=diff, updated_at=t,
                                                                         attempts=max(1, int(ev.get("attempts") or 1))))
            j = db.one(c, select(db.jobs).where(db.jobs.c.id == jid))
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            if j["source"] == "onboarding" and isinstance(ev.get("recipe"), dict):
                c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(recipe=_recipe(ev["recipe"])))
            limits = evaluate(c, j, repo)
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            if not db.one(c, select(db.decisions.c.job_id).where(db.decisions.c.job_id == jid)):
                c.execute(insert(db.decisions).values(job_id=jid, policy=policy, at=t, confident=False, limits=[],
                                                      recommendation="L'agent n'a pas donné d'avis."))
            c.execute(update(db.decisions).where(db.decisions.c.job_id == jid).values(limits=limits, policy=policy))
            all_ok = all(l["ok"] for l in limits)
            # Jamais de fusion seule pour un job arrêté par un humain, ni pour un onboarding (la recette passe toujours par un humain).
            if policy == "confident" and all_ok and not j["cancel_requested"] and j["source"] != "onboarding":
                state, status = "approved", "Toutes les barrières tiennent : la Fabrique merge"
                enqueue(c, jid, "merge", {"sha": j["verified_sha"]})
                db.log_audit(c, "fabrique", "Merge décidée", jid, j["verified_sha"] or "", "Merge si confiant · toutes les barrières tiennent")
            elif policy == "trial":
                state, status = "ready", "Essai : " + ("aurait mergé" if all_ok else "n'aurait pas mergé") + " · prête à relire"
                db.log_audit(c, "fabrique", "Décision de merge", jid, j["verified_sha"] or "", status)
            else:
                state, status = "ready", "Prête à relire"
                db.log_audit(c, "fabrique", "Décision de merge", jid, j["verified_sha"] or "", jobs.POLICIES[policy][0])
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(merge_state=state))
            result = {"policy": jobs.POLICIES[policy][0], "status": status, "limits": limits}
            if state == "ready":
                ok = sum(1 for l in limits if l["ok"])
                notify.emit(c, "ready", job=dict(j, pr_url=url), detail=f"{status} · barrières {ok}/{len(limits)}"
                            + "".join(f"\n✗ {l['name']} : {l['detail']}" for l in limits if not l["ok"]))
            _stage(c, j, "pr", _txt(ev.get("summary"), 300) or f"PR #{n} ouverte, {len(diff['files'])} fichier(s), +{diff['additions']} −{diff['deletions']}.",
                   status)
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(activity="idle"))
        elif kind == "usage":
            cost, tok = float(ev.get("cost") or 0), int(ev.get("tokens") or 0)
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(cost=max(0.0, cost), tokens=max(0, tok), updated_at=t,
                                                                         session_id=_txt(ev.get("session_id"), 80) or j["session_id"]))
            for a in (ev.get("agents") or [])[:30]:
                name = _txt(a.get("name"), 80) or "session principale"
                row = db.one(c, select(db.agents.c.id).where(db.agents.c.job_id == jid, db.agents.c.name == name))
                vals = dict(task=_txt(a.get("task"), 240), model=_txt(a.get("model"), 80), tokens=int(a.get("tokens") or 0),
                            cost=float(a.get("cost") or 0), state="running" if a.get("running") else "done")
                if row:
                    c.execute(update(db.agents).where(db.agents.c.id == row["id"]).values(**vals))
                else:
                    c.execute(insert(db.agents).values(job_id=jid, name=name, **vals))
        elif kind == "review":
            role, verdict = _txt(ev.get("role"), 20), _txt(ev.get("verdict"), 20)
            sha = _txt(ev.get("sha"), 40)[:12]
            if role not in ROLES or verdict not in VERDICTS or not sha:
                raise Denied("vérification invalide")
            c.execute(db.reviews.delete().where(db.reviews.c.job_id == jid, db.reviews.c.sha == sha, db.reviews.c.role == role))
            c.execute(insert(db.reviews).values(job_id=jid, sha=sha, role=role, verdict=verdict, summary=_txt(ev.get("summary"), 2000),
                                                points=[_txt(p, 400) for p in (ev.get("points") or [])[:12]], model=_txt(ev.get("model"), 80), at=t))
            db.log_event(c, jid, "vérification", f"{ROLES[role]} : {VERDICTS[verdict]}")
        elif kind == "cancelled":
            who = j["cancel_requested"] or "un reviewer"
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(cancel_requested=None))
            _stage(c, j, "failed", f"Arrêté à la demande de {who} ; le runner a tué l'agent.", f"Arrêté par {who}")
            db.log_audit(c, "fabrique", "Job arrêté", jid, detail=f"demandé par {who}, confirmé par le runner")
        elif kind == "log":
            db.log_event(c, jid, _txt(ev.get("kind"), 30) or "agent", _txt(ev.get("message"), 1000))
        elif kind == "onboard_wait":
            reason = _txt(ev.get("reason"), 120)
            if j["source"] != "onboarding" or not (reason in ("vide", "branche") or reason.startswith("acces:")):
                raise Denied("attente invalide")
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            text = onboard_wait_text(repo, reason)
            if repo["onboarding"] not in ("waiting", "empty"):
                notify.emit(c, "repo_waiting", repo=repo, detail=text)
            c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(onboarding="empty" if reason == "vide" else "waiting"))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(stage="queued", activity="idle", status_text=text, updated_at=t))
            db.log_event(c, jid, "onboarding", text)
        elif kind == "initialized":
            if j["source"] != "onboarding":
                raise Denied("initialisation invalide")
            r0 = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            rs0 = {k: v for k, v in (r0["settings"] or {}).items() if k != "to_create"}
            rs0["vierge"] = True      # la branche principale porte une recipe (vide) : le repo est utilisable dès maintenant
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="blank", settings=rs0,
                                                                                    recipe={"setup": [], "checks": {}, "env": []}))
            _stage(c, j, "merged", "Repo initialisé (recipe vide, et un README s'il n'y en avait pas). Repo vierge : prêt pour une première demande ; "
                                   "l'onboarding sera relancé après la première merge.", "Repo initialisé")
            db.log_audit(c, "fabrique", "Repo initialisé", jid, detail=_txt(ev.get("sha"), 40)[:12])
        elif kind == "onboarded":
            if j["source"] != "onboarding" or not isinstance(ev.get("recipe"), dict):
                raise Denied("onboarding invalide")
            c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="ready", recipe=_recipe(ev["recipe"])))
            _stage(c, j, "merged", "Recipe déjà présente sur la branche principale : repo prêt, rien à merger.", "Repo prêt")
            db.log_audit(c, "fabrique", "Onboarding terminé", jid, detail="recipe existante reprise")
        elif kind == "merged":
            sha = _txt(ev.get("sha"), 40)
            repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
            if j["source"] == "onboarding":
                rs1 = {k: v for k, v in (repo["settings"] or {}).items() if k != "vierge"}
                vals = {"onboarding": "ready", "settings": rs1}
                c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(**vals))
                db.log_audit(c, "fabrique", "Onboarding terminé", jid, detail="recipe mergée : le repo accepte les jobs")
            elif repo["onboarding"] == "blank" and not db.one(c, select(db.jobs.c.id).where(
                    db.jobs.c.repo_id == repo["id"], db.jobs.c.source == "onboarding", db.jobs.c.stage == "pr", db.jobs.c.archived.is_(False))):
                # (une PR de recipe déjà ouverte attend un humain : on ne la refait pas à chaque merge)
                # Première fusion dans un dépôt vierge : le projet existe, l'agent d'onboarding peut écrire sa vraie recette.
                ref_onb = jobs.onboard_in(c, {"user": "fabrique", "name": "la Fabrique"}, repo["id"])
                db.log_event(c, jid, "onboarding", f"première merge dans un repo vierge : onboarding lancé ({ref_onb})")
                onb = db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == ref_onb))
                # Dit sur le job d'onboarding lui-même pourquoi il démarre sans que personne l'ait demandé. Une seule fois :
                # deux PR mergées coup sur coup relancent le même onboarding, sans répéter l'explication.
                if onb and not db.one(c, select(db.events.c.id).where(db.events.c.job_id == onb["id"], db.events.c.kind == "onboarding",
                                                                       db.events.c.message.like("lancé par la Fabrique après %"))):
                    db.log_event(c, onb["id"], "onboarding", f"lancé par la Fabrique après {j['ref']}, première merge de ce repo vierge : "
                                 "le projet existe maintenant, l'agent écrit sa vraie recipe (installation et checks). PR à relire et merger.")
            c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(merge_state="merged"))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            _stage(c, j, "merged", f"Mergé sur {ev.get('branch') or 'main'}, épinglé au sha vérifié {j['verified_sha']}.",
                   "Mergé par la Fabrique")
            notify.emit(c, "merged", job=j, detail=f"Mergé sur {ev.get('branch') or 'main'} (sha vérifié {j['verified_sha']}).")
            db.log_audit(c, "fabrique", "Mergé", jid, j["verified_sha"] or "", f"squash → {sha[:12]}")
        elif kind == "failed":
            reason = _txt(ev.get("reason"), 400) or "échec"
            blank = False
            if j["source"] == "onboarding":
                r1 = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
                blank = jobs.is_blank(r1)
                if blank:    # repo déjà initialisé : il reste vierge et utilisable, l'onboarding sera retenté après une merge
                    c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"]).values(onboarding="blank"))
                    reason = (reason[:300] + " — sans effet sur le repo, qui reste utilisable ; l'onboarding sera retenté après la prochaine merge.")
                else:
                    c.execute(update(db.repos).where(db.repos.c.id == j["repo_id"], db.repos.c.onboarding != "ready").values(onboarding="failed"))
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            if ev.get("attempts"):
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(attempts=max(1, min(20, int(ev["attempts"])))))
            _stage(c, j, "failed", reason, "Échec : " + reason[:200])
            db.log_audit(c, "fabrique", "Échec", jid, detail=reason)
            notify.emit(c, "failed", job=j, detail=reason)
        elif kind == "intent_done":
            c.execute(update(db.intents).where(db.intents.c.id == it["id"]).values(state="done", done_at=t, note=_txt(ev.get("note"), 400)))
            if ev.get("attempts") is not None:
                c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(attempts=int(ev["attempts"])))
            c.execute(update(db.jobs).where(db.jobs.c.id == jid, db.jobs.c.stage.notin_(list(jobs.ACTIVE))).values(activity="idle"))
        else:
            raise Denied("type d'événement inconnu")
    return result
