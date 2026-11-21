"""Jobs : étapes, lectures filtrées par rôle, inbox, actions humaines (demande, réponse, décision, archive).
Au lot 1 il n'y a pas encore de runner : les actions sont enregistrées (audit + événement) et seront exécutées par le runner
au lot 2. Le simulateur de démonstration (demo.py) joue le rôle du runner pour montrer le cycle complet."""
import re, time
from sqlalchemy import and_, func, insert, or_, select, true, update
from . import config, db

# (clé, libellé, couleur). L'orange de marque est réservé aux actions ; l'ambre signale « attend ta réponse ».
STAGES = [
    ("queued", "Queue", "#8a8f98"),
    ("triage", "Triage", "#7aa2f7"),
    ("spec", "Spec à valider", "#c4a7ff"),
    ("investigating", "Investigation", "#a78bfa"),
    ("implementing", "Implémentation", "#5b8def"),
    ("verifying", "Checks", "#2dd4bf"),
    ("review", "Vérification", "#e879c9"),
    ("pr", "PR · CI", "#60a5fa"),
    ("waiting", "Attend ta réponse", "#f5b83d"),
    ("merged", "Mergé", "#4ade80"),
    ("done", "Étude rendue", "#86efac"),
    ("failed", "Échec", "#f87171"),
]
STAGE = {k: {"key": k, "label": l, "color": c} for k, l, c in STAGES}
ORDER = [k for k, _, _ in STAGES]
FLOW = ["triage", "investigating", "implementing", "verifying", "review", "pr", "merged"]   # étapes de la chronologie
STUDY_FLOW = ["triage", "investigating", "done"]                                            # … et celles d'une étude
FINAL = ("merged", "done", "failed")
ACTIVE = {"triage", "investigating", "implementing", "verifying", "review", "pr"}
HARNESSES = {"claude-code": "Claude Code", "codex": "Codex"}
READY = {"claude-code", "codex"}   # agents que le runner sait lancer ; les autres sont affichés « bientôt »
POLICIES = {"off": ("Désactivée", "Chaque PR attend ta vérification. La Fabrique n'évalue rien."),
            "trial": ("Essai", "La Fabrique évalue et affiche « aurait mergé », sans merger."),
            "confident": ("Merge si confiant", "La Fabrique merge seul quand toutes les barrières tiennent : checks verts, "
                                                "vérification OK, premier essai, aucun protected path, taille respectée.")}
INBOX_KIND = {"question": ("Question", "#f5b83d"), "review": ("Prête à relire", "#60a5fa"),
              "spec": ("Spec à valider", "#c4a7ff"), "study": ("Étude rendue", "#86efac"), "failed": ("Échec", "#f87171")}


_demo = {"at": 0.0, "v": None}


def demo_visible():
    """Données de démonstration affichées ? (réglage, seulement si FABRIQUE_DEMO=1 ; cache de 2 s)."""
    if not config.DEMO:
        return False
    if _demo["v"] is None or time.time() - _demo["at"] > 2:
        with db.tx() as c:
            v, _ = db.get_setting(c, "demo")
        _demo.update(at=time.time(), v=bool((v or {}).get("visible")))
    return _demo["v"]


def forget_demo():
    _demo["v"] = None


class Refused(Exception):
    """Action refusée (droits, état du job) : message lisible pour l'utilisateur."""


def _visible(user):
    """Filtre SQL : un requester ne voit que ses demandes ; les jobs de démonstration seulement s'ils sont affichés."""
    from .auth import can
    conds = [] if can(user, "see_all") else [db.jobs.c.requester == user["user"]]
    if not demo_visible():
        conds.append(db.jobs.c.source != "démo")
    return and_(true(), *conds)


def _repo_names(c):
    return db.cached("repos", lambda: {r["id"]: r for r in db.rows(c, select(db.repos))})


def _runners(c):
    return db.cached("runners", lambda: {r["id"]: r for r in db.rows(c, select(db.runners))})


def listing(user, repo=None, archived=False):
    """Jobs visibles, du plus récemment modifié au plus ancien. Dans une requête de page, la liste n'est lue qu'une fois :
    la page, les compteurs du pied et l'inbox s'en servent tous (lecture seule : personne ne la modifie)."""
    key = ("jobs", user.get("user"), user.get("role"), repo or None, bool(archived))
    return db.cached(key, lambda: _listing(user, repo, archived))


def _listing(user, repo, archived):
    with db.tx() as c:
        q = select(db.jobs).where(_visible(user), db.jobs.c.archived == archived)
        if repo:
            q = q.where(db.jobs.c.repo_id == repo)
        js = db.rows(c, q.order_by(db.jobs.c.updated_at.desc()))
        repos = _repo_names(c)
        runners = _runners(c)
        ag = {}
        for r in db.rows(c, select(db.agents.c.job_id, func.count().label("n")).where(db.agents.c.state == "running").group_by(db.agents.c.job_id)):
            ag[r["job_id"]] = r["n"]
        lastq = {}
        for r in db.rows(c, select(db.questions.c.job_id, db.questions.c.question).where(db.questions.c.answer.is_(None)).order_by(db.questions.c.id)):
            lastq[r["job_id"]] = r["question"]
    for j in js:
        _decorate(j, repos, runners)
        j["open_question"] = lastq.get(j["id"])
        j["agents_running"] = ag.get(j["id"], 0)
    return js


def _decorate(j, repos, runners):
    j["stage_info"] = STAGE.get(j["stage"], STAGE["queued"])
    j["repo"] = repos.get(j["repo_id"])
    j["runner"] = runners.get(j["runner_id"]) if j.get("runner_id") else None
    j["harness_label"] = HARNESSES.get(j["harness"], j["harness"])
    j["activity_label"] = activity_label(j)


def activity_label(j):
    """Ce que fait le job, en un mot. « en pause » est réservé à ce qui est suspendu : une question sans réponse, ou le
    budget du jour atteint."""
    from .work import PAUSED
    st = j["stage"]
    if st in FINAL:
        return "terminé"
    if st == "waiting" or j.get("status_text") == PAUSED:
        return "en pause"
    if j.get("activity") == "active":
        return "actif"
    return "en file" if st == "queued" else "en attente"


def by_stage(js):
    groups = {k: [] for k in ORDER}
    for j in js:
        groups.setdefault(j["stage"], []).append(j)
    return [(STAGE[k], groups[k]) for k in ORDER]


# Vue condensée (préférence personnelle) : quatre familles au lieu d'une colonne par étape.
FAMILIES = [("wait", "En attente", "#8a8f98", ("queued",)),
            ("run", "En cours", "#5b8def", ("triage", "investigating", "implementing", "verifying", "review")),
            ("you", "À toi", "#f5b83d", ("spec", "pr", "waiting")),
            ("end", "Terminé", "#4ade80", ("merged", "done", "failed"))]
FAMILY_OF = {s: k for k, _, _, stages in FAMILIES for s in stages}


def by_family(js):
    groups = {k: [] for k, _, _, _ in FAMILIES}
    for j in js:
        groups[FAMILY_OF.get(j["stage"], "run")].append(j)
    return [({"key": k, "label": l, "color": c}, groups[k]) for k, l, c, _ in FAMILIES]


def get(user, ref):
    """Le job complet (question, chronologie, agents, preuves, décision, audit, événements) ou None s'il n'est pas visible."""
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref, _visible(user)))
        if not j:
            return None
        jid = j["id"]
        _decorate(j, _repo_names(c), _runners(c))
        j["questions"] = db.rows(c, select(db.questions).where(db.questions.c.job_id == jid).order_by(db.questions.c.asked_at))
        tl = db.rows(c, select(db.timeline).where(db.timeline.c.job_id == jid).order_by(db.timeline.c.at))
        j["agents"] = db.rows(c, select(db.agents).where(db.agents.c.job_id == jid).order_by(db.agents.c.id))
        j["evidence"] = db.rows(c, select(db.evidence).where(db.evidence.c.job_id == jid).order_by(db.evidence.c.id))
        j["decision"] = db.one(c, select(db.decisions).where(db.decisions.c.job_id == jid))
        j["reviews"] = db.rows(c, select(db.reviews).where(db.reviews.c.job_id == jid).order_by(db.reviews.c.at.desc()))
        j["audit"] = db.rows(c, select(db.audit).where(db.audit.c.job_id == jid).order_by(db.audit.c.at.desc()))
        j["events"] = db.rows(c, select(db.events).where(db.events.c.job_id == jid, db.events.c.kind != "audit")
                              .order_by(db.events.c.id.desc()).limit(200))
        # Diff envoyé par le runner : seulement son résumé ici (le texte est lu à la demande, voir diff_view).
        j["patch"] = db.one(c, select(db.diffs.c.sha, db.diffs.c.truncated, db.diffs.c.files, db.diffs.c.at).where(db.diffs.c.job_id == jid))
    j["open_question"] = next((q for q in j["questions"] if q["answer"] is None), None)
    j["timeline"] = _timeline(j, tl)
    return j


def patch_info(j):
    """Résumé du diff à afficher dans la fiche du job, ou None : il doit correspondre au commit vérifié (un diff plus ancien
    ne se montre pas), et une étude n'en a pas."""
    p = j.get("patch")
    if not p or not j.get("verified_sha") or p["sha"] != j["verified_sha"] or j.get("source") == "étude" or diff_off(j):
        return None
    files = p["files"] or []
    return {"sha": p["sha"], "truncated": p["truncated"], "files": files, "n": len(files), "add": sum(f.get("add", 0) for f in files),
            "rem": sum(f.get("rem", 0) for f in files)}


def repo_keeps_diff(settings):
    """Réglage du repo « Afficher le diff dans la Fabrique » : activé par défaut. Désactivé pour un repo confidentiel, dont
    le code ne doit pas être copié dans la base de la Fabrique : le runner n'envoie rien et le dashboard ne garde rien."""
    return (settings or {}).get("diff") is not False


def diff_off(j):
    """Vrai si le repo du job ne conserve pas de diff (la fiche le dit, et renvoie vers la PR)."""
    return not repo_keeps_diff((j.get("repo") or {}).get("settings"))


def report_formats(c=None):
    """Formats dans lesquels un rapport d'étude se télécharge (Settings → Budgets et barrières) : les deux par défaut."""
    def load(cx):
        lim = db.get_setting(cx, "limits")[0] or {}
        return {"md": lim.get("report_md") is not False, "pdf": lim.get("report_pdf") is not False}
    if c is not None:
        return load(c)
    with db.tx() as cx:
        return load(cx)


def diff_view(user, ref, sha=""):
    """Diff complet d'un job, découpé par fichier, avec les fichiers protégés signalés. Mêmes droits que la lecture du job ;
    None si le job n'est pas visible, n'a pas de diff, ou si le diff ne correspond plus au commit vérifié."""
    from . import diffview, work
    with db.tx() as c:
        j = db.one(c, select(db.jobs.c.id, db.jobs.c.repo_id, db.jobs.c.verified_sha, db.jobs.c.source).where(db.jobs.c.ref == ref, _visible(user)))
        if not j or not j["verified_sha"] or j["source"] == "étude":
            return None
        row = db.one(c, select(db.diffs).where(db.diffs.c.job_id == j["id"]))
        if not row or row["sha"] != j["verified_sha"] or (sha and sha != row["sha"]):
            return None
        repo = db.one(c, select(db.repos.c.settings).where(db.repos.c.id == j["repo_id"])) or {}
        if not repo_keeps_diff(repo.get("settings")):
            return None
        limits, _ = db.get_setting(c, "limits")
    patterns = ((repo.get("settings") or {}).get("protected_paths") or limits["protected_paths"]) + [".fabrique/**"]
    paths = [f["path"] for f in (row["files"] or [])]
    return diffview.view(row, work.protected_hits(paths, patterns))


def _timeline(j, tl):
    """Étapes passées (avec résumé), courante, et à venir (grisées)."""
    seen = [dict(t, state="done") for t in tl]
    cur = j["stage"]
    flow = STUDY_FLOW if j.get("source") == "étude" else FLOW
    if seen and cur not in FINAL:
        seen[-1]["state"] = "current" if seen[-1]["stage"] == cur else "done"
    done_stages = {t["stage"] for t in tl}
    future = []
    if cur not in FINAL:
        idx = flow.index(cur) if cur in flow else 0
        future = [{"stage": s, "state": "todo", "summary": "", "at": None} for s in flow[idx + 1:] if s not in done_stages]
    for t in seen + future:
        t["info"] = STAGE.get(t["stage"], STAGE["queued"])
    return seen + future


# Ce qu'un humain doit regarder lui-même quand une relecture n'a pas rendu de verdict.
REVIEW_FOCUS = {"code": "la justesse : le changement fait-il ce que dit la demande ? cas limites, erreurs non gérées, tests qui testent vraiment",
                "securite": "les failles : entrées non validées, injection, secret écrit en dur, droits trop larges",
                "archi": "la structure : modules respectés, dépendances ou couplage inutiles, duplication",
                "infra": "le déploiement : conteneur, CI, migrations, configuration, retour arrière"}


def changes_url(j):
    """Lien direct vers le diff de la PR chez le provider (onglet des changements)."""
    url, prov = j.get("pr_url") or "", ((j.get("repo") or {}).get("provider") or "")
    if not url.startswith(("https://", "http://")):
        return ""
    return url + ("/diffs" if prov == "gitlab" else "/files")


def to_check(j):
    """Pour chaque barrière rouge : quoi vérifier soi-même avant d'approuver."""
    from .work import ROLES, VERDICTS
    d = j.get("decision") or {}
    cur = [r for r in j.get("reviews") or [] if r["sha"] == j.get("verified_sha")]
    out = []
    for l in d.get("limits") or []:
        if l["ok"]:
            continue
        n = l["name"]
        if n == "Vérification":
            for r in sorted(cur, key=lambda r: r["role"]):
                if r["verdict"] == "indetermine":
                    out.append({"title": f"Vérification {ROLES.get(r['role'], r['role'])} sans verdict",
                                "text": (r["summary"] or "Aucun verdict rendu.") + f" Relis toi-même {REVIEW_FOCUS.get(r['role'], '')} "
                                        "dans les changements, ou relance la vérification."})
                elif r["verdict"] != "ok":
                    pts = " ; ".join(r["points"] or []) or r["summary"] or ""
                    out.append({"title": f"Vérification {ROLES.get(r['role'], r['role'])} : {VERDICTS[r['verdict']]}", "text": pts})
            if not cur or not {"code", "securite"} <= {r["role"] for r in cur}:
                out.append({"title": "Vérification incomplète", "text": "Code et sécurité sont exigés au sha vérifié : relance la vérification."})
        elif n == "Checks verts":
            if "aucun check" in l["detail"]:
                out.append({"title": "Aucun check", "text": "La recipe du repo n'a pas encore de checks (repo vierge) : rien n'a été "
                            "vérifié automatiquement. Lis les changements et leurs tests ; après le merge, l'onboarding proposera la recipe."})
            else:
                out.append({"title": "Checks", "text": "Ouvre la section Preuves du job : la sortie de chaque check en échec y est dépliable."})
        elif n == "Premier essai":
            out.append({"title": "Plusieurs tentatives", "text": f"{l['detail']} : regarde ce que la correction a changé, et pourquoi."})
        elif n == "Protected paths":
            out.append({"title": "Fichiers protégés", "text": l["detail"] + " : ces fichiers demandent une vérification humaine attentive."})
        elif n == "Taille":
            out.append({"title": "PR volumineuse", "text": l["detail"] + " : au-delà de la limite, le merge seul est interdite."})
        elif n == "Avis de l'agent":
            out.append({"title": "L'agent n'est pas confiant", "text": "Lis son interprétation et ses hypothèses dans le job."})
    for e in j.get("evidence") or []:
        if e.get("sha") == j.get("verified_sha") and security_warn(e):
            why = ("le job a tenté de joindre un site hors de la liste blanche du proxy : une dépendance à autoriser, ou un agent "
                   "trompé." if e.get("name") == "Réseau" else "secret écrit en dur, ou paquet introuvable ou tout juste publié.")
            out.append({"title": (e.get("output") or "").split("\n", 1)[0], "text": f"Ouvre la ligne {e.get('name')} dans les Preuves "
                        f"du job : {why} Ce contrôle ne bloque pas : à toi de juger."})
    return out


EFFORT = {1: "trivial", 2: "léger", 3: "moyen", 4: "conséquent", 5: "lourd"}


def review_effort(j):
    """Effort de relecture de 1 à 5 (même calcul que le runner pour la PR), ou None tant qu'il n'y a pas de diff."""
    d = j.get("diff") or {}
    files = d.get("files") or []
    if not files:
        return None
    lines = int(d.get("additions") or 0) + int(d.get("deletions") or 0)
    n = 1 if lines <= 30 and len(files) <= 2 else 2 if lines <= 120 and len(files) <= 5 else 3 if lines <= 300 and len(files) <= 10 else 4 if lines <= 800 else 5
    prot = any(not l["ok"] for l in ((j.get("decision") or {}).get("limits") or []) if l["name"] == "Protected paths")
    n = min(5, n + (1 if prot else 0))
    return {"n": n, "label": EFFORT[n], "files": len(files), "add": int(d.get("additions") or 0), "rem": int(d.get("deletions") or 0)}


def security_warn(e):
    """Ligne « Sécurité » ou « Réseau » des preuves (contrôles du runner, à titre d'information) qui signale quelque chose :
    des alertes, ou des sorties réseau refusées."""
    first = (e.get("output") or "").split("\n", 1)[0]
    return (e.get("name") == "Sécurité" and "alerte" in first) or (e.get("name") == "Réseau" and "refusée(s)" in first)


def inbox(user):
    """Ce qui attend un humain. Un requester ne voit que les questions sur ses propres demandes."""
    from .auth import can
    items = []
    for j in listing(user):
        kind = None
        if j["stage"] == "waiting" and j["open_question"]:
            kind, why = "question", j["open_question"]
        elif j["stage"] == "done" and j.get("report") and not j.get("report_seen"):
            kind, why = "study", "Le rapport d'étude est rendu : lis-le, puis marque-le comme lu ou fais-en une demande."
        elif not can(user, "see_all"):
            continue
        elif j["stage"] == "spec":
            kind, why = "spec", j["status_text"] or "La spec attend ta validation avant l'investigation."
        elif j["merge_state"] == "ready":
            kind, why = "review", j["status_text"] or "Prête à relire."
        elif j["stage"] == "failed":
            kind, why = "failed", j["status_text"] or "Le job a échoué."
        if kind:
            items.append({"job": j, "kind": kind, "label": INBOX_KIND[kind][0], "color": INBOX_KIND[kind][1], "why": why})
    order = {"question": 0, "spec": 1, "review": 2, "study": 3, "failed": 4}
    return sorted(items, key=lambda i: (order[i["kind"]], -i["job"]["updated_at"]))


def access_requests():
    """Demandes d'accès en attente (vues par les admins). Lue une fois par page : le compteur du menu et l'inbox s'en servent."""
    def load():
        with db.tx() as c:
            return db.rows(c, select(db.users).where(db.users.c.requested_role.is_not(None)).order_by(db.users.c.requested_at))
    return db.cached("access_requests", load)


def counters(user):
    from .auth import can
    js = listing(user)
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    today = [j for j in js if j["updated_at"] >= start]
    return {
        "active": sum(1 for j in js if j["stage"] in ACTIVE and j["activity"] == "active"),
        "idle": sum(1 for j in js if j["stage"] in ACTIVE and j["activity"] != "active"),
        "agents": sum(j["agents_running"] for j in js),
        "tokens": sum(j["tokens"] for j in today),
        "spend": sum(j["cost"] for j in today),
        "today": len(today),
        "need": len(inbox(user)) + (len(access_requests()) if can(user, "settings") else 0),
        "total": len(js),
    }


def archived_count(user):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(db.jobs).where(_visible(user), db.jobs.c.archived.is_(True))).scalar()


def is_demo_repo(r):
    return bool((r.get("settings") or {}).get("demo"))


def repos_for_requests():
    with db.tx() as c:
        rs = db.rows(c, select(db.repos).where(db.repos.c.accepts_jobs.is_(True)).order_by(db.repos.c.is_default.desc(), db.repos.c.name))
    return [r for r in rs if demo_visible() or not is_demo_repo(r)]


def _next_ref(c):
    """Numéro du prochain job. Un compteur qui ne redescend jamais (réglage « job_seq ») : un numéro supprimé n'est pas
    réattribué, car des commits (« Fabrique-Job: FAB-14 ») et l'audit y renvoient pour toujours."""
    seq = int((db.get_setting(c, "job_seq")[0] or {}).get("n") or 0)
    if not seq:   # première fois : on part du plus grand numéro déjà attribué
        for r in db.rows(c, select(db.jobs.c.ref)):
            m = re.fullmatch(re.escape(config.JOB_PREFIX) + r"-(\d+)", r["ref"] or "")
            seq = max(seq, int(m.group(1))) if m else seq
    n = seq + 1
    while db.one(c, select(db.jobs.c.id).where(db.jobs.c.ref == f"{config.JOB_PREFIX}-{n}")):
        n += 1
    db.set_setting(c, "job_seq", {"n": n}, "fabrique")
    return f"{config.JOB_PREFIX}-{n}"


def create(user, title, request, done_when, repo_id, harness="", model="", source="formulaire", kind="start", study=False, profile=""):
    from .auth import can
    title, request, done_when = " ".join(title.split())[:200], request.strip()[:8000], done_when.strip()[:2000]
    if not title or not request:
        raise Refused("Le titre et le besoin sont obligatoires.")
    if (harness or model or profile) and not can(user, "see_all"):
        harness = model = profile = ""   # le demandeur ne choisit ni l'agent, ni le modèle, ni le profil : réglages du dépôt
    if harness and harness not in HARNESSES:
        raise Refused("Agent inconnu.")
    if harness and harness not in READY:
        raise Refused(f"{HARNESSES[harness]} n'est pas encore disponible.")
    if study and source == "formulaire":
        source = "étude"   # étude : l'agent rend un rapport, le runner ne pousse rien
    with db.tx() as c:
        return insert_job(c, user, title, request, done_when, repo_id, harness, model, source, kind, profile)


def insert_job(c, user, title, request, done_when, repo_id, harness="", model="", source="formulaire", kind="start", profile=""):
    """Création d'un job dans une transaction déjà ouverte (formulaire, onboarding, relance automatique)."""
    t = time.time()
    repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id, db.repos.c.accepts_jobs.is_(True)))
    if not repo:
        raise Refused("Ce repo n'accepte pas de nouvelles demandes.")
    if repo["onboarding"] not in ("ready", "blank") and not is_blank(repo) and source not in ("onboarding", "démo"):
        raise Refused("Ce repo n'est pas encore prêt : l'onboarding doit d'abord être mergé.")
    glob, _ = db.get_setting(c, "agents")
    limits, _ = db.get_setting(c, "limits")
    rs = repo["settings"] or {}
    from . import providers, profiles
    eff_h = harness or rs.get("harness") or glob["harness"]
    if profile and not profiles.get(c, profile):
        raise Refused("Profil de modèles inconnu.")
    eff_m, extra = profiles.resolve(c, rs, source, model, profile)
    if not eff_m and source == "démo":
        eff_m = "modèle-de-démonstration"               # données fictives : aucun agent ne tourne, aucun modèle n'est requis
    if not eff_m:
        raise Refused(providers.default_problem(c))
    if eff_h not in READY:
        raise Refused(f"L'agent retenu ({HARNESSES.get(eff_h, eff_h)}, réglage du repo ou global) n'est pas encore disponible : "
                      "choisis Claude Code dans les settings.")
    known = {m["id"]: m for m in providers.models_with_provider(c)}
    if model and model not in known:
        raise Refused("Modèle inconnu.")
    if eff_m in known and not providers.compatible(known[eff_m], eff_h):
        raise Refused(f"{HARNESSES.get(eff_h, eff_h)} ne sait pas utiliser {known[eff_m]['name']} : choisis un modèle compatible.")
    ref = _next_ref(c)
    jid = c.execute(insert(db.jobs).values(
        ref=ref, title=title, request=request, done_when=done_when, requester=user["user"], source=source, repo_id=repo["id"],
        stage="queued", activity="idle", status_text="En attente d'un runner", branch=f"fabrique/{ref}",
        harness=harness or rs.get("harness") or glob["harness"], model=eff_m, models=extra,
        budget=float(rs.get("budget_job") or limits["budget_job"]), created_at=t, updated_at=t)).inserted_primary_key[0]
    c.execute(update(db.repos).where(db.repos.c.id == repo["id"]).values(last_activity=t))
    if source != "démo":
        from .work import enqueue
        enqueue(c, jid, kind)
    db.log_event(c, jid, "créé", f"Demande déposée par {user['name']}")
    db.log_audit(c, user["user"], "Demande créée", jid, ref, f"repo {repo['slug']}")
    return ref


def _job_for_action(c, user, ref):
    j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref, _visible(user)))
    if not j:
        raise Refused("Job introuvable.")
    return j


def answer(user, ref, text):
    from .auth import can
    text = text.strip()[:4000]
    if not text:
        raise Refused("La réponse est vide.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if not (can(user, "answer") or (can(user, "answer_own") and j["requester"] == user["user"])):
            raise Refused("Tu ne peux pas répondre à cette question.")
        q = db.one(c, select(db.questions).where(db.questions.c.job_id == j["id"], db.questions.c.answer.is_(None)))
        if not q or j["stage"] != "waiting":
            raise Refused("Aucune question en attente sur ce job.")
        t = time.time()
        c.execute(update(db.questions).where(db.questions.c.id == q["id"]).values(answer=text, answered_by=user["user"], answered_at=t))
        # La réponse est une donnée pour l'agent, jamais une consigne de plus haut niveau : le runner l'encadre (lot 2).
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="triage", activity="idle", updated_at=t,
                                                                         status_text="Réponse reçue · reprise au tri"))
        c.execute(insert(db.timeline).values(job_id=j["id"], stage="triage", at=t, summary="Reprise avec la réponse reçue."))
        if j["source"] != "démo":
            from .work import enqueue
            enqueue(c, j["id"], "resume", {"answer": text, "question": q["question"], "by": user["name"]})
        db.log_event(c, j["id"], "réponse", f"Réponse de {user['name']} : l'agent reprend au tri")
        db.log_audit(c, user["user"], "Question répondue", j["id"], ref, text[:200])


ONBOARD_TEXT = ("Écrire la recipe .fabrique/setup.json du repo {slug} : commandes d'installation, checks (tests, lint, "
                "typage, build) et variables d'environnement nécessaires, prouvée en la lançant. Ne modifier aucun autre fichier.",
                "La recipe est la seule modification ; le runner la rejoue dans un conteneur neuf ; un humain merge la PR.")


def is_blank(repo):
    """Repo initialisé par la Fabrique : sa branche principale porte déjà une recipe (vide). Il accepte des demandes et le
    reste quoi que devienne son onboarding, qui ne fait qu'essayer d'écrire une recipe plus riche."""
    return bool(repo and (repo.get("settings") or {}).get("vierge"))


def remove_repo(user, repo_id):
    """Retire un repo de la Fabrique. Rien n'est supprimé sur la forge. Possible tant que le repo n'a servi qu'à son
    onboarding : dès qu'une demande y a été déposée, son historique compte et le repo se ferme au lieu de disparaître."""
    from sqlalchemy import delete
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused("Seuls les admins retirent un repo.")
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
        if not repo:
            raise Refused("Repo introuvable.")
        js = db.rows(c, select(db.jobs.c.id, db.jobs.c.source, db.jobs.c.stage).where(db.jobs.c.repo_id == repo_id))
        if any(j["source"] != "onboarding" for j in js):
            raise Refused("Ce repo a déjà reçu des demandes : son historique est gardé. Pour l'arrêter, décoche "
                          "« Accepte les nouvelles demandes ».")
        ids = [j["id"] for j in js]
        if ids and db.one(c, select(db.intents.c.id).where(db.intents.c.job_id.in_(ids), db.intents.c.state == "leased")):
            raise Refused("L'onboarding de ce repo est en cours sur un runner : attends sa fin, ou arrête le job, puis réessaie.")
        if ids:
            for t in reversed(db.meta.sorted_tables):
                if "job_id" in t.c:
                    c.execute(delete(t).where(t.c.job_id.in_(ids)))
            c.execute(delete(db.jobs).where(db.jobs.c.id.in_(ids)))
        c.execute(delete(db.repos).where(db.repos.c.id == repo_id))
        if repo["is_default"]:
            other = db.one(c, select(db.repos.c.id).order_by(db.repos.c.name).limit(1))
            if other:
                c.execute(update(db.repos).where(db.repos.c.id == other["id"]).values(is_default=True))
        db.log_audit(c, user["user"], "Repo retiré", target=repo["slug"], detail="rien n'est supprimé sur la forge")
    return repo["name"]


def onboard(user, repo_id):
    """Lancer (ou relancer) l'onboarding d'un repo : un job dont l'agent n'écrit que la recipe .fabrique/setup.json."""
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused("Seuls les admins lancent l'onboarding d'un repo.")
    with db.tx() as c:
        return onboard_in(c, user, repo_id)


def onboard_in(c, user, repo_id, kind="onboard"):
    repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
    if repo and (repo["settings"] or {}).get("to_create") and kind == "onboard":
        kind = "create"     # la création validée n'a pas abouti : on la relance, pas un onboarding
    if not repo or repo["onboarding"] in ("ready", "analyzing"):
        raise Refused("Ce repo est déjà prêt ou en cours d'onboarding.")
    # Un seul job d'onboarding par dépôt : on relance le dernier au lieu d'en empiler de nouveaux.
    last = db.one(c, select(db.jobs).where(db.jobs.c.repo_id == repo_id, db.jobs.c.source == "onboarding", db.jobs.c.archived.is_(False))
                  .order_by(db.jobs.c.id.desc()).limit(1))
    from .work import enqueue
    if last and last["stage"] != "merged":
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == last["id"], db.intents.c.state.in_(["pending", "leased"]))):
            return last["ref"]                                  # un essai est déjà en file : rien à ajouter
        t = time.time()
        # Une relance repart avec l'agent et le modèle en vigueur : c'est souvent un réglage corrigé qui la motive.
        glob, _ = db.get_setting(c, "agents")
        rs = repo["settings"] or {}
        from . import profiles
        model, extra = profiles.resolve(c, rs, last["source"])
        enqueue(c, last["id"], kind)
        c.execute(update(db.jobs).where(db.jobs.c.id == last["id"]).values(
            harness=rs.get("harness") or glob["harness"], model=model or last["model"], models=extra,
            stage="queued", activity="idle", updated_at=t,
            status_text={"init": "Initialisation du repo", "create": "Création relancée"}.get(kind, "Onboarding relancé") + " · en attente d'un runner"))
        if kind == "create":
            c.execute(update(db.repos).where(db.repos.c.id == repo_id).values(onboarding="creating"))
        if kind == "onboard" and not is_blank(repo):
            c.execute(update(db.repos).where(db.repos.c.id == repo_id).values(onboarding="pending"))
        db.log_audit(c, user["user"], "Initialisation du repo validée" if kind == "init" else "Onboarding relancé", last["id"], target=repo["slug"])
        return last["ref"]
    if kind == "init":
        raise Refused("Aucun onboarding ouvert pour ce repo.")
    req, done = ONBOARD_TEXT
    return insert_job(c, user, f"Onboarding de {repo['slug']}", req.format(slug=repo["slug"]), done, repo_id, source="onboarding", kind="onboard")


REPO_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def request_repo(user, provider, name, description=""):
    """Demande de création d'un repo (reviewer ou admin) : rien n'est créé avant la validation d'un admin."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused("Seuls les reviewers et les admins demandent la création d'un repo.")
    name, description = name.strip().lower(), description.strip()[:300]
    if not REPO_NAME.match(name):
        raise Refused("Nom invalide : minuscules, chiffres, points, tirets (100 caractères au plus).")
    from . import forges
    with db.tx() as c:
        g = forges.resolve(c, provider) or {}                   # provider précis (son identifiant) ; un type seul reste accepté
        typ = g.get("type") or provider
        if typ not in forges.TYPES:
            raise Refused("Provider inconnu.")
        if not g:
            g = next((x for x in forges.listing(c) if x["type"] == typ), {})
        ns = (g.get("creation") or (db.get_setting(c, "creation")[0] or {}).get(typ) or "").strip()
        if not ns:
            raise Refused("La création de repos n'est pas activée pour ce provider : renseigne « Organisation des nouveaux repos » dans "
                          "Settings → Providers Git.")
        slug, gid = f"{ns}/{name}", g.get("id", "")
        if db.one(c, select(db.repos.c.id).where(db.repos.c.slug == slug, db.repos.c.git_id == gid)):
            raise Refused(f"Le repo {slug} est déjà connu de la Fabrique.")
        rid = c.execute(insert(db.repos).values(provider=typ, git_id=gid, slug=slug, name=name, onboarding="requested", accepts_jobs=False,
                                                settings={"merge_policy": "off", "description": description, "requested_by": user["user"],
                                                          "to_create": True},
                                                created_at=time.time())).inserted_primary_key[0]
        db.log_audit(c, user["user"], "Création de repo demandée", target=slug, detail=description)
        from . import notify
        notify.emit(c, "repo_request", repo={"id": rid, "slug": slug},
                    detail=f"Demandé par {user.get('name') or user['user']}" + (f" : « {description} »" if description else "") + ". À valider dans la inbox.")
        return rid


def can_decide_repo(user, repo):
    """Un admin valide toujours ; un reviewer valide la demande d'un autre (jamais la sienne)."""
    from .auth import can
    if can(user, "repos_edit"):
        return True
    return can(user, "decide") and (repo["settings"] or {}).get("requested_by") != user["user"]


def repo_requests():
    with db.tx() as c:   # tirées de la liste des repos déjà lue pour la page, quand il y en a une
        return sorted((r for r in _repo_names(c).values() if r["onboarding"] == "requested"), key=lambda r: r["created_at"])


def decide_repo(user, rid, approve, note=""):
    """Validation humaine (admin) : le runner crée le repo dans le groupe dédié, l'initialise, et il devient « vierge »."""
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == rid, db.repos.c.onboarding == "requested"))
        if not repo:
            raise Refused("Cette demande n'attend plus de décision.")
        if not can_decide_repo(user, repo):
            raise Refused("La création doit être validée par un admin, ou par un autre reviewer que celui qui l'a demandée.")
        if not approve:
            c.execute(db.repos.delete().where(db.repos.c.id == rid))
            db.log_audit(c, user["user"], "Création de repo refusée", target=repo["slug"], detail=note[:300])
            return None
        c.execute(update(db.repos).where(db.repos.c.id == rid).values(onboarding="creating", accepts_jobs=True))
        db.log_audit(c, user["user"], "Création de repo validée", target=repo["slug"], detail=note[:300])
        rs = repo["settings"] or {}
        return insert_job(c, user, f"Création de {repo['slug']}",
                          f"Créer le repo {repo['slug']} (privé) puis l'initialiser : README et recipe vide. "
                          f"Description : {rs.get('description') or '—'}",
                          "Le repo existe chez le provider et le dashboard le marque « vierge ».", rid, source="onboarding", kind="create")


def init_repo(user, repo_id):
    """Repo vide : un humain valide l'initialisation (premier commit poussé sur la branche principale par le runner)."""
    from .auth import can
    if not can(user, "repos_edit"):
        raise Refused("Seuls les admins initialisent un repo.")
    with db.tx() as c:
        repo = db.one(c, select(db.repos).where(db.repos.c.id == repo_id))
        if not repo or repo["onboarding"] != "empty":
            raise Refused("Seul un repo vide peut être initialisé.")
        return onboard_in(c, user, repo_id, kind="init")


def rereview(user, ref):
    """Relancer la vérification au sha vérifié (reviewer ou admin), sans relancer l'agent."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused("Seuls les reviewers et les admins relancent la vérification.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["merge_state"] != "ready" or not j["verified_sha"] or not j["pr_number"] or j["source"] == "démo":
            raise Refused("Rien à relire : la PR n'attend pas de décision.")
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            raise Refused("Une commande est déjà en cours pour ce job.")
        from .work import enqueue
        enqueue(c, j["id"], "review", {"sha": j["verified_sha"]})
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="reviewing", updated_at=time.time(),
                                                                         status_text="Vérification relancée · en attente du runner"))
        db.log_audit(c, user["user"], "Vérification relancée", j["id"], j["verified_sha"] or "")


def decide(user, ref, verdict, note=""):
    """Approuver ou refuser un merge (reviewer ou admin). Le merge elle-même sera faite par le dashboard, épinglée au sha."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused("Seuls les reviewers et les admins décident d'un merge.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["merge_state"] != "ready":
            raise Refused("Cette PR n'attend pas de décision.")
        t = time.time()
        if verdict == "approve":
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="approved", updated_at=t,
                                                                             status_text="Merge approuvée · en attente du runner"))
            if j["source"] != "démo":
                from .work import enqueue
                enqueue(c, j["id"], "merge", {"sha": j["verified_sha"]})
            db.log_audit(c, user["user"], "Merge approuvée", j["id"], j["verified_sha"] or "", note[:300] or "Approuvée dans la inbox")
        elif verdict == "refuse":
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(merge_state="refused", updated_at=t,
                                                                             status_text="Merge refusée"))
            db.log_audit(c, user["user"], "Merge refusée", j["id"], j["verified_sha"] or "", note[:300])
        else:
            raise Refused("Décision inconnue.")


def validate_spec(user, ref, ok, note=""):
    from .auth import can
    if not can(user, "decide"):
        raise Refused("Seuls les reviewers et les admins valident une spec.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["stage"] != "spec":
            raise Refused("Ce job n'a pas de spec à valider.")
        t = time.time()
        if ok:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="investigating", activity="idle", updated_at=t,
                                                                             status_text="Spec validée · investigation"))
            c.execute(insert(db.timeline).values(job_id=j["id"], stage="investigating", at=t, summary="Spec validée par un humain."))
            db.log_audit(c, user["user"], "Spec validée", j["id"], ref, note[:300])
        else:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                             status_text="Spec refusée" + (f" : {note[:180]}" if note else "")))
            db.log_audit(c, user["user"], "Spec refusée", j["id"], ref, note[:300])


def cancel(user, ref):
    """Arrêter un job en cours. Sans runner dessus : arrêt immédiat. Avec un runner : il tue l'agent puis confirme."""
    from .auth import can
    if not can(user, "decide"):
        raise Refused("Seuls les reviewers et les admins arrêtent un job.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["stage"] in FINAL:
            raise Refused("Ce job est déjà terminé.")
        t = time.time()
        leased = db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state == "leased"))
        c.execute(update(db.intents).where(db.intents.c.job_id == j["id"], db.intents.c.state == "pending")
                  .values(state="done", done_at=t, note="arrêt demandé"))
        if leased:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(cancel_requested=user["name"], updated_at=t,
                                                                             status_text="Arrêt demandé : le runner arrête l'agent"))
            db.log_audit(c, user["user"], "Arrêt demandé", j["id"], ref)
            return f"Arrêt de {ref} demandé : le runner arrête l'agent."
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(stage="failed", activity="idle", updated_at=t,
                                                                         status_text=f"Arrêté par {user['name']}"))
        c.execute(insert(db.timeline).values(job_id=j["id"], stage="failed", at=t, summary=f"Arrêté par {user['name']} avant tout travail du runner."))
        db.log_audit(c, user["user"], "Job arrêté", j["id"], ref)
        return f"{ref} arrêté."


def archive(user, ref, restore=False):
    from .auth import can
    if not can(user, "archive"):
        raise Refused("Seuls les reviewers et les admins archivent.")
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if not restore and j["stage"] not in FINAL and j["merge_state"] != "refused":
            raise Refused("Seuls les jobs terminés s'archivent.")
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(archived=not restore, updated_at=time.time()))
        db.log_audit(c, user["user"], "Job restauré" if restore else "Job archivé", j["id"], ref)


# ---------- archives : recherche, suppression définitive, archivage automatique, ménage des dossiers ----------

ARCHIVE_PAGE = 50
OUTCOMES = {"merged": "Mergé", "failed": "Échec", "refused": "Refusé", "done": "Étude rendue"}


def outcome(j):
    """Issue d'un job terminé, pour les archives : mergé, échec, refusé ou étude rendue."""
    return "refused" if j.get("merge_state") == "refused" and j["stage"] != "merged" else j["stage"] if j["stage"] in OUTCOMES else "failed"


def search_archives(user, q="", repo=0, state="", regex=False, page=1):
    """Jobs archivés visibles par l'utilisateur, du plus récent au plus ancien. q cherche dans la référence, le titre, la
    demande et le demandeur : texte simple (tous les mots, sans tenir compte de la casse) ou expression régulière.
    Renvoie (lignes de la page, total, erreur de motif). Le motif est borné : il s'applique à des textes courts, côté
    serveur, pour qu'une expression mal écrite ne bloque pas le dashboard."""
    q = (q or "").strip()[:200]
    with db.tx() as c:
        sel = select(db.jobs).where(_visible(user), db.jobs.c.archived.is_(True))
        if repo:
            sel = sel.where(db.jobs.c.repo_id == repo)
        js = db.rows(c, sel.order_by(db.jobs.c.updated_at.desc()))
        repos = _repo_names(c)
        runners = _runners(c)
    for j in js:
        _decorate(j, repos, runners)
        j["outcome"] = outcome(j)
    if state in OUTCOMES:
        js = [j for j in js if j["outcome"] == state]
    err = ""
    if q:
        fields = lambda j: [str(x or "") for x in (j["ref"], j["title"], j["request"][:4000], j["requester"], (j["repo"] or {}).get("slug"))]
        hay = lambda j: " ".join(fields(j))
        if regex:
            try:
                pat = re.compile(q, re.I)   # champ par champ : un motif ancré (^FAB-1[0-9]$) vise la référence seule
                js = [j for j in js if any(pat.search(f) for f in fields(j))]
            except re.error as e:
                err, js = f"Expression régulière invalide : {e}", []
        else:
            words = q.lower().split()
            js = [j for j in js if all(w in hay(j).lower() for w in words)]
    total = len(js)
    page = max(1, min(int(page or 1), max(1, -(-total // ARCHIVE_PAGE))))
    return js[(page - 1) * ARCHIVE_PAGE: page * ARCHIVE_PAGE], total, err, page


def delete_phrase(j):
    """Ce qu'il faut taper pour confirmer la suppression d'un job : « repo/FAB-3 »."""
    return f"{(j.get('repo') or {}).get('slug') or ''}/{j['ref']}"


def delete_job(user, ref, typed):
    """Suppression DÉFINITIVE d'un job archivé : admin seulement, confirmée en tapant « repo/FAB-3 ». Le job et tout son
    contenu disparaissent ; l'audit reste (ses lignes sont détachées du job) et une ligne dit qui a supprimé quoi. Rien
    n'est touché sur la forge. Le dossier de travail est nettoyé par le runner à son prochain passage."""
    from sqlalchemy import delete
    from .auth import can
    if not can(user, "settings"):
        raise Refused("Seuls les admins suppriment un job.")
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        if not j or (j["source"] == "démo"):
            raise Refused("Job introuvable.")
        if not j["archived"]:
            raise Refused("Seul un job archivé se supprime : archive-le d'abord.")
        if db.one(c, select(db.intents.c.id).where(db.intents.c.job_id == j["id"], db.intents.c.state.in_(["pending", "leased"]))):
            raise Refused("Une commande est encore en cours sur ce job : attends sa fin, puis réessaie.")
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"])) or {}
        want = f"{repo.get('slug') or ''}/{j['ref']}"
        if (typed or "").strip() != want:
            raise Refused(f"Confirmation incorrecte : tape exactement {want} pour supprimer ce job.")
        c.execute(update(db.audit).where(db.audit.c.job_id == j["id"]).values(job_id=None))   # l'audit survit au job
        for t in reversed(db.meta.sorted_tables):
            if "job_id" in t.c and t.name != "audit":
                c.execute(delete(t).where(t.c.job_id == j["id"]))
        c.execute(delete(db.jobs).where(db.jobs.c.id == j["id"]))
        db.log_audit(c, user["user"], "Job supprimé", None, ref, f"{j['title'][:120]} · {repo.get('slug') or '—'} · demandé par {j['requester']}")
    return ref


def auto_archive(now=None):
    """Archive les jobs mergés, et les études déjà lues, terminés depuis plus de N jours (Settings → Budgets et barrières ;
    0 : jamais). Les échecs et les PR refusées restent visibles : ils demandent souvent une suite. Renvoie le nombre archivé."""
    now = now or time.time()
    with db.tx() as c:
        days = int((db.get_setting(c, "limits")[0] or {}).get("auto_archive_days") or 0)
        if days <= 0:
            return 0
        old = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source != "démo",
                                               db.jobs.c.updated_at < now - days * 86400,
                                               or_(db.jobs.c.stage == "merged", and_(db.jobs.c.stage == "done", db.jobs.c.report_seen.is_not(None)))))
        for j in old:
            c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(archived=True))
            db.log_audit(c, "fabrique", "Job archivé", j["id"], j["ref"], f"archivage automatique après {days} jour(s)")
    return len(old)


def workspaces_to_purge(names, now=None):
    """Parmi les dossiers de travail qu'un runner dit avoir (un par job), ceux qu'il peut supprimer : le job n'existe plus
    (supprimé), ou il est terminé depuis plus d'une heure sans commande en attente. Seuls les noms de CETTE installation
    (préfixe des jobs) sont considérés."""
    now = now or time.time()
    pat = re.compile(re.escape(config.JOB_PREFIX) + r"-\d+")
    names = [n for n in dict.fromkeys(str(x) for x in (names or [])) if pat.fullmatch(n)][:500]
    if not names:
        return []
    with db.tx() as c:
        known = {j["ref"]: j for j in db.rows(c, select(db.jobs).where(db.jobs.c.ref.in_(names)))}
        busy = {r["job_id"] for r in db.rows(c, select(db.intents.c.job_id).where(db.intents.c.state.in_(["pending", "leased"])))}
    out = []
    for n in names:
        j = known.get(n)
        if j is None or (j["stage"] in FINAL and j["id"] not in busy and (j["updated_at"] or 0) < now - 3600):
            out.append(n)
    return out


def mark_seen(user, ref):
    """Étude lue : elle sort de l'inbox (elle reste dans les jobs)."""
    with db.tx() as c:
        j = _job_for_action(c, user, ref)
        if j["source"] != "étude" or not j.get("report"):
            raise Refused("Ce job n'a pas de rapport d'étude.")
        c.execute(update(db.jobs).where(db.jobs.c.id == j["id"], db.jobs.c.report_seen.is_(None)).values(report_seen=time.time()))


def activity(limit=300):
    with db.tx() as c:
        js = db.rows(c, select(db.jobs.c.id, db.jobs.c.ref, db.jobs.c.source))
        rs = db.rows(c, select(db.audit).order_by(db.audit.c.at.desc()).limit(limit * 3))
    refs = {r["id"]: r["ref"] for r in js}
    hidden = set() if demo_visible() else {r["id"] for r in js if r["source"] == "démo"}
    out = [r for r in rs if r["job_id"] not in hidden and not (hidden and r["action"] == "Données de démonstration")][:limit]
    for r in out:
        r["ref"] = refs.get(r["job_id"])
    return out


def last_event_id():
    with db.tx() as c:
        return c.execute(select(func.max(db.events.c.id))).scalar() or 0
