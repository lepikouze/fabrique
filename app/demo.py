"""Données et simulateur de démonstration (FABRIQUE_DEMO=1). Le simulateur joue le rôle du runner du lot 2 : il fait
avancer les jobs d'étape en étape, écrit chronologie, événements, agents, preuves et décisions, pour montrer le dashboard
en mouvement. Tout ce qu'il produit est fictif ; un bandeau le signale dans l'interface."""
import hashlib, logging, random, threading, time
from sqlalchemy import func, insert, select, update
from . import config, db, diffview, jobs

log = logging.getLogger("fabrique.demo")
RNG = random.Random(7)

POOL = [
    ("Exporter les liens en CSV", "Un bouton « Exporter » sur la liste télécharge tous les liens (titre, adresse, tags, date) en CSV.",
     "Le fichier s'ouvre dans un tableur avec les accents corrects, et un test couvre l'export."),
    ("Recherche plein texte", "Chercher dans le titre, l'adresse et la note de chaque lien, sans recharger la page.",
     "Taper trois lettres filtre la liste ; un test couvre la recherche."),
    ("Tags sur les liens", "Pouvoir poser plusieurs tags sur un lien et filtrer la liste par tag.",
     "Ajout, retrait et filtre fonctionnent, avec des tests."),
    ("Page 404 personnalisée", "Une page d'erreur sobre avec un lien vers l'accueil.", "La page s'affiche pour toute adresse inconnue."),
    ("Vérifier les liens morts chaque nuit", "Une tâche planifiée teste chaque lien et marque ceux qui ne répondent plus.",
     "Les liens morts portent un badge ; la tâche est testée sans réseau."),
    ("Import d'un fichier de favoris HTML", "Importer l'export de favoris de Firefox ou Chrome.", "Les dossiers deviennent des tags."),
    ("Raccourcis clavier", "J/K pour naviguer, / pour chercher, N pour un nouveau lien.", "Les raccourcis sont listés dans l'aide."),
    ("Compteur de clics par lien", "Compter les ouvertures de chaque lien et trier par popularité.", "Le tri « populaires » existe."),
    ("Mode sombre", "Suivre le réglage du système, avec un bouton pour forcer clair ou sombre.", "Le choix est mémorisé."),
    ("Partager une liste par lien", "Générer un lien en lecture seule vers une tag.", "Le lien expire après 7 jours."),
]
QUESTIONS = [
    "L'export doit-il inclure les liens archivés ? (A) oui, avec une colonne « archivé » ; (B) non, seulement les liens actifs.",
    "Pour la recherche : (A) filtrage dans le navigateur, simple et instantané ; (B) recherche côté serveur, qui tiendra mieux "
    "au-delà de 10 000 liens. Laquelle ?",
    "Les tags doivent-elles être sensibles à la casse (« Urgent » et « urgent » distinctes) ?",
]
CHECKS = [("Tests", "pytest -q", "48 passed in 6.21s"), ("Lint", "ruff check .", "All checks passed!"),
          ("Typage", "mypy app", "Success: no issues found in 23 source files"), ("Build", "docker build -t essai .", "naming to docker.io/library/essai done")]


def _sha(s):
    return hashlib.sha1(s.encode()).hexdigest()[:7]


DEMO_DIFF = """diff --git a/carnet/export.py b/carnet/export.py
new file mode 100644
index 0000000..3b1f2aa
--- /dev/null
+++ b/carnet/export.py
@@ -0,0 +1,14 @@
+# @TITRE@ (job @REF@, données fictives de démonstration).
+import csv, io
+
+COLONNES = ("titre", "adresse", "tags", "date")
+
+
+def en_csv(liens):
+    # Les liens en CSV : s'ouvre dans un tableur avec les accents corrects.
+    sortie = io.StringIO()
+    w = csv.writer(sortie, delimiter=";")
+    w.writerow(COLONNES)
+    for l in liens:
+        w.writerow([l.titre, l.adresse, ",".join(l.tags), l.date.isoformat()])
+    return sortie.getvalue()
diff --git a/carnet/vues.py b/carnet/vues.py
index 7c2d9e1..a90b4f3 100644
--- a/carnet/vues.py
+++ b/carnet/vues.py
@@ -1,6 +1,7 @@
 from fastapi import APIRouter, Response

 from . import depot
+from .export import en_csv

 routeur = APIRouter()

@@ -18,3 +19,9 @@ def liste(tag: str = ""):
     liens = depot.tous(tag=tag or None)
-    return {"liens": liens}
+    return {"liens": liens, "total": len(liens)}
+
+
+@routeur.get("/liens.csv")
+def export():
+    return Response(en_csv(depot.tous()), media_type="text/csv; charset=utf-8",
+                    headers={"Content-Disposition": "attachment; filename=liens.csv"})
diff --git a/tests/test_export.py b/tests/test_export.py
new file mode 100644
index 0000000..5d0c1be
--- /dev/null
+++ b/tests/test_export.py
@@ -0,0 +1,11 @@
+from datetime import date
+from types import SimpleNamespace as Lien
+
+from carnet.export import en_csv
+
+
+def test_les_accents_et_les_tags_sont_gardes():
+    liens = [Lien(titre="Café de l'équipe", adresse="https://exemple.org", tags=["veille", "à lire"], date=date(2026, 11, 3))]
+    lignes = en_csv(liens).splitlines()
+    assert lignes[0].endswith("titre;adresse;tags;date")
+    assert lignes[1] == "Café de l'équipe;https://exemple.org;veille,à lire;2026-11-03"
"""


def _demo_diff(c, j, sha, t):
    """Diff fictif d'un job de démonstration : même forme que celui d'un vrai runner (texte, résumé, compteurs du job)."""
    text = DEMO_DIFF.replace("@TITRE@", j["title"]).replace("@REF@", j["ref"])
    files = diffview.summary(text)
    c.execute(db.diffs.delete().where(db.diffs.c.job_id == j["id"]))
    c.execute(insert(db.diffs).values(job_id=j["id"], sha=sha, text=text, truncated=False, files=files, at=t))
    return {"files": [f["path"] for f in files], "truncated": False, "additions": sum(f["add"] for f in files),
            "deletions": sum(f["rem"] for f in files)}


def backfill_diffs():
    """Base de démonstration créée avant la 0.7.5 : les jobs fictifs qui ont déjà une PR reçoivent leur diff fictif."""
    t = time.time()
    with db.tx() as c:
        have = {r["job_id"] for r in db.rows(c, select(db.diffs.c.job_id))}
        for j in db.rows(c, select(db.jobs).where(db.jobs.c.source == "démo", db.jobs.c.pr_number.is_not(None), db.jobs.c.verified_sha.is_not(None))):
            if j["id"] not in have:
                c.execute(update(db.jobs).where(db.jobs.c.id == j["id"]).values(diff=_demo_diff(c, j, j["verified_sha"], t)))


def seed():
    """Remplit une base vide avec des repos, deux runners et une douzaine de jobs à toutes les étapes."""
    with db.tx() as c:
        if c.execute(select(func.count()).select_from(db.repos)).scalar():
            seeded = True
        else:
            seeded = False
    if seeded:
        backfill_diffs()
        return False
    with db.tx() as c:
        t = time.time()
        base = {"merge_policy": "confident", "size_limit": 300}
        r1 = c.execute(insert(db.repos).values(provider="forgejo", slug="equipe/carnet-de-liens", name="Carnet de liens (essai)", is_default=True,
                       onboarding="ready", settings=dict(base), created_at=t - 86400 * 3, last_activity=t,
                       recipe={"setup": ["pip install -r requirements.txt"], "checks": {k.lower(): cmd for k, cmd, _ in CHECKS}})).inserted_primary_key[0]
        r2 = c.execute(insert(db.repos).values(provider="forgejo", slug="equipe/veille", name="Veille", onboarding="ready",
                       settings={"merge_policy": "trial", "demo": True}, created_at=t - 86400 * 2, last_activity=t - 7200,
                       recipe={"setup": ["pip install -r app/requirements.txt"], "checks": {"tests": "pytest -q tests"}})).inserted_primary_key[0]
        c.execute(insert(db.repos).values(provider="gitlab", slug="demo/portail-interne", name="Portail interne (GitLab)", onboarding="pending",
                  accepts_jobs=False, settings={"merge_policy": "off", "demo": True}, created_at=t - 3600))
        run1 = c.execute(insert(db.runners).values(name="runner-1", host="runner-1.exemple", kind="vm", online=True, last_seen=t, capacity=3,
                         harnesses=[{"name": "Claude Code", "version": "2.1.301"}],
                         metrics={"cpu": 18, "mem": [5.1, 16], "disk": [61, 120]}, version="lot1-démo")).inserted_primary_key[0]
        c.execute(insert(db.runners).values(name="runner-conteneur", host="serveur-2", kind="conteneur", online=False, last_seen=t - 3 * 3600,
                  capacity=2, harnesses=[{"name": "Codex", "version": "0.164.0"}], metrics={}, version="lot4"))
        db.log_audit(c, "fabrique", "Données de démonstration", detail="repos, runners et jobs fictifs")
    plan = [("merged", r1), ("merged", r1), ("pr", r1), ("pr", r2), ("waiting", r1), ("review", r1), ("verifying", r1),
            ("implementing", r1), ("investigating", r1), ("spec", r1), ("triage", r2), ("queued", r1), ("failed", r1)]
    for i, (stage, repo) in enumerate(plan):
        title, req, done = POOL[i % len(POOL)]
        ref = jobs.create({"user": "camille", "name": "Camille", "role": "admin"}, title, req, done, repo, source="démo")
        _drive_to(ref, stage, run1, i)
    return True


def _drive_to(ref, target, runner_id, i):
    """Fait passer un job par toutes les étapes jusqu'à « target » (pour la graine)."""
    path = {"queued": [], "triage": ["triage"], "spec": ["triage", "spec"], "investigating": ["triage", "investigating"],
            "implementing": ["triage", "investigating", "implementing"], "verifying": ["triage", "investigating", "implementing", "verifying"],
            "review": ["triage", "investigating", "implementing", "verifying", "review"],
            "pr": ["triage", "investigating", "implementing", "verifying", "review", "pr"],
            "waiting": ["triage", "waiting"], "merged": ["triage", "investigating", "implementing", "verifying", "review", "pr", "merged"],
            "failed": ["triage", "investigating", "implementing", "failed"]}[target]
    for s in path:
        step(ref, s, runner_id, seed_index=i)


def _summary(stage, j):
    return {
        "triage": f"La demande est claire : « {j['title']} ». Critère de fin repris tel quel ; risque faible, pas de spec nécessaire.",
        "spec": "Le changement touche le modèle de données : la spec propose une migration et attend ta validation.",
        "investigating": "Lecture du code : les routes sont dans app/main.py, les modèles dans app/db.py ; deux tests existants à étendre.",
        "implementing": "Implémentation sur la branche, 3 fichiers modifiés, tests ajoutés.",
        "verifying": "Recipe du repo lancée sur le commit de tête par le runner.",
        "review": "Vérification en parallèle : code, sécurité, architecture. Aucun point bloquant.",
        "pr": "PR ouverte, CI verte. Décision de merge écrite.",
        "merged": "Mergé sur main, épinglé au sha vérifié.",
        "failed": "Bloqué : la variable LINKS_DB_URL manque dans la recipe du repo.",
        "waiting": "La demande laisse un choix ouvert : question posée.",
    }[stage]


def step(ref, stage, runner_id=None, seed_index=None):
    """Passe le job à l'étape donnée en écrivant ce qu'un vrai runner écrirait."""
    t = time.time()
    with db.tx() as c:
        j = db.one(c, select(db.jobs).where(db.jobs.c.ref == ref))
        repo = db.one(c, select(db.repos).where(db.repos.c.id == j["repo_id"]))
        jid = j["id"]
        cost = round(j["cost"] + RNG.uniform(0.05, 0.6), 2)
        tokens = j["tokens"] + RNG.randint(40_000, 400_000)
        vals = dict(stage=stage, updated_at=t, cost=cost, tokens=tokens, activity="active" if stage in jobs.ACTIVE else "idle")
        if runner_id and not j["runner_id"]:
            vals["runner_id"] = runner_id
        status = {"triage": "Triage en cours", "spec": "Spec à valider", "investigating": "Investigation", "implementing": "Écrit le code",
                  "verifying": "Checks en cours", "review": "Vérification en cours", "pr": "En attente des checks",
                  "waiting": "Question posée", "merged": "Mergé par la Fabrique", "failed": "Bloqué : variable manquante"}[stage]
        if stage == "triage" and not db.one(c, select(db.agents.c.id).where(db.agents.c.job_id == jid)):
            c.execute(insert(db.agents).values(job_id=jid, name="session principale", task=j["title"], model=j["model"],
                                               tokens=tokens // 2, cost=cost * 0.8))
            db.log_event(c, jid, "session", f"démarrée dans /work/{ref} ({j['branch']})")
        if stage == "investigating":
            c.execute(insert(db.agents).values(job_id=jid, parent_id=None, name="Explore", task="Repérer le code concerné",
                                               model="claude-haiku-5-5", tokens=RNG.randint(200_000, 900_000), cost=0.04, state="done"))
            db.log_event(c, jid, "agent", "sous-agent Explore lancé puis terminé")
        if stage == "waiting":
            q = QUESTIONS[(seed_index or 0) % len(QUESTIONS)]
            c.execute(insert(db.questions).values(job_id=jid, question=q, asked_at=t))
            db.log_event(c, jid, "question", "question posée au requester")
        if stage == "verifying":
            sha = _sha(ref + str(t))
            for name, cmd, out in CHECKS:
                c.execute(insert(db.evidence).values(job_id=jid, sha=sha, name=name, command=cmd, ok=True,
                                                     duration_ms=RNG.randint(400, 21000), output=f"$ {cmd}\n{out}", ran_by="runner", at=t))
            vals.update(verified_sha=sha, checks_ok=len(CHECKS), checks_total=len(CHECKS))
            db.log_event(c, jid, "preuve", f"{len(CHECKS)}/{len(CHECKS)} checks au sha {sha}, relancés par le runner")
        if stage == "pr":
            n = 10 + jid
            vals.update(pr_number=n, pr_url=f"https://git.exemple.org/{repo['slug']}/pulls/{n}")
            if j["verified_sha"]:
                vals["diff"] = _demo_diff(c, j, j["verified_sha"], t)
            policy = (repo["settings"] or {}).get("merge_policy", "off")
            size = RNG.randint(40, 260)
            limits = [{"name": "Checks verts", "ok": True, "detail": f"{j['checks_ok'] or 4}/{j['checks_total'] or 4} au sha {j['verified_sha'] or '—'}"},
                      {"name": "Vérification", "ok": True, "detail": "code, sécurité, architecture : OK"},
                      {"name": "Premier essai", "ok": True, "detail": "aucune nouvelle tentative"},
                      {"name": "Protected paths", "ok": True, "detail": "aucun fichier protégé touché"},
                      {"name": "Taille", "ok": size <= 300, "detail": f"{size} lignes sur 300"}]
            confident = (seed_index or jid) % 3 != 1
            c.execute(insert(db.decisions).values(
                job_id=jid, policy=policy, at=t, confident=confident, limits=limits,
                recommendation="L'agent recommande de merger." if confident else "L'agent préfère une vérification humaine.",
                interpretation=f"La demande « {j['title']} » est comprise comme : {j['request']}",
                assumptions=["Pas de changement du format de la base.", "Les textes de l'interface restent en français."],
                verdict="Confiant : le critère de fin est couvert par des tests qui passent." if confident else
                        "Pas confiant : le comportement sur mobile n'a pas pu être vérifié.",
                notes="Navigateur sans interface indisponible sur le runner : rendu couvert par des tests de gabarit."))
            auto = policy == "confident" and confident and all(l["ok"] for l in limits)
            vals["merge_state"] = "waiting_checks" if auto else "ready"
            if not auto:
                status = "Prête à relire" if policy != "trial" else "Essai : aurait mergé" if confident else "Prête à relire"
            db.log_event(c, jid, "pr", f"PR #{n} ouverte : {j['title']}")
            db.log_audit(c, "fabrique", "Décision de merge", jid, vals.get("verified_sha") or j["verified_sha"] or "",
                         f"{jobs.POLICIES[policy][0]} · {'confiant' if confident else 'pas confiant'}")
        if stage == "merged":
            vals.update(merge_state="merged", activity="idle")
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
            db.log_event(c, jid, "mergé", f"PR #{j['pr_number'] or ''} mergée (squash), épinglée à {j['verified_sha']}")
            db.log_audit(c, "fabrique", "Mergé", jid, j["verified_sha"] or "", "Mergé par la Fabrique (squash)")
        if stage == "failed":
            c.execute(update(db.agents).where(db.agents.c.job_id == jid).values(state="done"))
        vals["status_text"] = status
        c.execute(update(db.jobs).where(db.jobs.c.id == jid).values(**vals))
        c.execute(update(db.agents).where(db.agents.c.job_id == jid, db.agents.c.name == "session principale")
                  .values(tokens=tokens, cost=cost))
        c.execute(insert(db.timeline).values(job_id=jid, stage=stage, at=t, summary=_summary(stage, j)))
        db.log_event(c, jid, stage, jobs.STAGE[stage]["label"])


NEXT = {"queued": "triage", "triage": "investigating", "investigating": "implementing", "implementing": "verifying",
        "verifying": "review", "review": "pr"}


MAX_LIVE = 12        # jobs de démonstration affichés au plus (non archivés) : la démo ne grossit pas sans fin
MAX_ARCHIVED = 30    # jobs de démonstration archivés gardés ; les plus anciens sont supprimés (données fictives)
HUMAN_WAIT = 180     # secondes : au-delà, le simulateur répond, valide ou approuve à la place d'un humain absent
ANSWERS = ["(A), merci.", "Plutôt (B).", "Non, insensible à la casse."]


def _renew(live, t):
    """Renouvellement : un job qui attend un humain depuis HUMAN_WAIT reçoit sa réponse ; au plafond, le plus ancien job
    terminé est archivé pour laisser la place à une nouvelle demande. Renvoie True s'il a fait quelque chose."""
    stale = sorted((j for j in live if t - (j["updated_at"] or t) >= HUMAN_WAIT), key=lambda j: j["updated_at"] or 0)
    for j in stale:
        if j["stage"] == "waiting":
            with db.tx() as c:
                c.execute(update(db.questions).where(db.questions.c.job_id == j["id"], db.questions.c.answered_at.is_(None))
                          .values(answer=RNG.choice(ANSWERS), answered_by="camille", answered_at=t))
                db.log_event(c, j["id"], "réponse", "réponse du requester (démonstration)")
            step(j["ref"], "investigating")
            return True
        if j["stage"] == "spec":
            with db.tx() as c:
                db.log_event(c, j["id"], "spec", "spec validée (démonstration)")
            step(j["ref"], "investigating")
            return True
        if j["stage"] == "pr" and j["merge_state"] == "ready":
            with db.tx() as c:
                db.log_audit(c, "camille", "Merge approuvé", j["id"], j["verified_sha"] or "", "approbation de démonstration")
            step(j["ref"], "merged")
            return True
    if len(live) >= MAX_LIVE:
        done = sorted((j for j in live if j["stage"] in ("merged", "failed")), key=lambda j: j["updated_at"] or 0)
        if done:
            with db.tx() as c:
                c.execute(update(db.jobs).where(db.jobs.c.id == done[0]["id"]).values(archived=True, updated_at=t))
                db.log_event(c, done[0]["id"], "archivé", "archivé par le simulateur (renouvellement de la démonstration)")
            _trim_archived()
            return True
    return False


def _trim_archived():
    """Ne garde que les MAX_ARCHIVED jobs de démonstration archivés les plus récents : sans cela, une démo laissée allumée
    remplirait les archives de jobs fictifs. Ne touche jamais un vrai job."""
    from sqlalchemy import delete
    with db.tx() as c:
        old = [r["id"] for r in db.rows(c, select(db.jobs.c.id).where(db.jobs.c.archived.is_(True), db.jobs.c.source == "démo")
                                        .order_by(db.jobs.c.updated_at.desc()).offset(MAX_ARCHIVED).limit(500))]
        if not old:
            return 0
        for t in reversed(db.meta.sorted_tables):
            if "job_id" in t.c:
                c.execute(delete(t).where(t.c.job_id.in_(old)))
        c.execute(delete(db.jobs).where(db.jobs.c.id.in_(old)))
    return len(old)


def tick():
    """Un pas du simulateur : fait avancer un job, merge ce qui a été approuvé, renouvelle la démo (réponses à la place d'un
    humain absent, archivage des plus anciens jobs terminés) et ajoute parfois une demande, sans dépasser MAX_LIVE jobs.
    En pause quand les données de démonstration sont masquées."""
    if not jobs.demo_visible():
        return
    t = time.time()
    with db.tx() as c:
        live = db.rows(c, select(db.jobs).where(db.jobs.c.archived.is_(False), db.jobs.c.source == "démo"))   # jamais les vrais jobs
        run = db.one(c, select(db.runners.c.id).where(db.runners.c.online.is_(True), db.runners.c.token_hash.is_(None)))
        c.execute(update(db.runners).where(db.runners.c.online.is_(True), db.runners.c.token_hash.is_(None)).values(
            last_seen=t, in_flight=sum(1 for j in live if j["stage"] in jobs.ACTIVE),
            metrics={"cpu": RNG.randint(8, 64), "mem": [round(RNG.uniform(4, 9), 1), 16], "disk": [61, 120]}))
    rid = run["id"] if run else None
    for j in live:
        if j["merge_state"] in ("approved", "waiting_checks"):
            return step(j["ref"], "merged", rid)
    if _renew(live, t):
        return
    movable = [j for j in live if j["stage"] in NEXT]
    if len(live) < MAX_LIVE and len([j for j in live if j["stage"] == "queued"]) < 1 and RNG.random() < 0.5:
        title, req, done = RNG.choice(POOL)
        with db.tx() as c:
            repo = db.one(c, select(db.repos.c.id).where(db.repos.c.is_default.is_(True)))
        if repo:
            jobs.create({"user": "camille", "name": "Camille", "role": "admin"}, title, req, done, repo["id"], source="démo")
            return
    if not movable:
        return
    j = RNG.choice(movable)
    nxt = NEXT[j["stage"]]
    if j["stage"] == "triage" and RNG.random() < 0.15:
        with db.tx() as c:
            asked = db.one(c, select(db.questions.c.id).where(db.questions.c.job_id == j["id"]))
        if not asked:
            nxt = "waiting"
    step(j["ref"], nxt, rid, seed_index=j["id"])


_stop = threading.Event()


def start():
    def loop():
        while not _stop.wait(config.DEMO_TICK):
            try:
                tick()
            except Exception:
                log.exception("simulateur de démonstration")
    _stop.clear()
    threading.Thread(target=loop, name="demo", daemon=True).start()


def stop():
    _stop.set()
