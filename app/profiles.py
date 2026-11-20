"""Profils de modèles : un modèle par rôle, réutilisable d'un repo à l'autre (l'effort est celui du provider du modèle).

Rôles : l'agent (tri, enquête, implémentation, corrections, dans une seule session), les relecteurs (vérifications
code, sécurité, architecture, infra), les études et l'onboarding. Chaque rôle nomme son modèle : on voit tout d'un coup
d'œil. (Un profil d'avant cette règle, avec un rôle vide, reprend le modèle de l'agent pour ce rôle.)
Le profil est facultatif : sans profil, un job démarre comme avant, avec le modèle du repo ou le modèle global.

Ordre pour le modèle principal d'un job : choix sur la demande > profil (demande, repo, puis global) > modèle du repo >
modèle global. Ce qui est retenu est figé à la création du job (jobs.model et jobs.models)."""
import re
from . import db

ROLES = {"agent": ("Agent", "Tri, enquête, implémentation, corrections : une seule session, un seul modèle"),
         "review": ("Relecteurs", "Vérifications code, sécurité, architecture et infra, avec un contexte neuf"),
         "study": ("Études", "Rapports d'étude : beaucoup de lecture, aucune modification"),
         "onboarding": ("Onboarding", "Lecture d'un repo et écriture de sa recipe, rejouée ensuite par le runner")}
# Exemples affichés sous chaque rôle : le modèle le plus en rapport avec le travail demandé (Claude, puis local).
EXAMPLES = {"agent": "Claude Opus 5.5 · en local : Qwen3.8 27B. Le travail le plus long et le plus exigeant.",
            "review": "Claude Sonnet 5.5 · en local : Gemma 4 26B. Idéalement une autre famille que l'agent : un second regard.",
            "study": "Claude Sonnet 5.5, ou Opus pour une étude difficile · en local : Qwen3.8 27B.",
            "onboarding": "Claude Haiku 5.5 · en local : Gemma 4 26B. Tâche simple, vérifiée par le runner."}
INHERIT = "provider"   # effort : réglage du provider (le niveau défini sur le provider compatible)
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Invalid(ValueError):
    pass


def efforts():
    from .providers import EFFORTS
    return {INHERIT: "Réglage du provider", **EFFORTS}


def listing(c):
    return [p for p in (db.get_setting(c, "profiles")[0] or []) if isinstance(p, dict) and p.get("id")]


def get(c, pid):
    return next((p for p in listing(c) if p["id"] == pid), None) if pid else None


def usage(c, pid):
    """Où un profil sert : réglage global, repos."""
    glob, _ = db.get_setting(c, "agents")
    repos = [r["slug"] for r in db.rows(c, db.repos.select()) if (r["settings"] or {}).get("profile") == pid]
    return {"global": glob.get("profile") == pid, "repos": repos}


def slug(name, taken=()):
    """Identifiant interne déduit du nom (jamais montré) : minuscules sans accents, tirets ; unique."""
    import unicodedata
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:36] or "profil"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def save(c, by, pid, name, roles, new=False):
    """roles : {rôle: {"model": id}}. Un modèle doit être proposé dans les Settings. Sans pid (nouveau profil),
    l'identifiant est déduit du nom ; un profil renommé garde le sien (les repos qui l'utilisent le gardent aussi).
    L'effort ne se règle plus par profil : c'est celui du provider du modèle."""
    from .providers import models_with_provider
    name = " ".join((name or "").split())[:60]
    if not name:
        raise Invalid("Donne un nom au profil.")
    if any(p["name"].lower() == name.lower() and p["id"] != (pid or "") for p in listing(c)):
        raise Invalid(f"Un profil s'appelle déjà « {name} ».")
    pid = (pid or "").strip().lower() or slug(name, {p["id"] for p in listing(c)})
    if not _ID.match(pid):
        raise Invalid("Profil invalide.")
    known = {m["id"] for m in models_with_provider(c)}
    clean = {}
    for role in ROLES:
        r = roles.get(role) or {}
        model = str(r.get("model") or "")
        if model and model not in known:
            raise Invalid(f"{ROLES[role][0]} : modèle inconnu ({model}).")
        clean[role] = {"model": model, "effort": INHERIT}
    missing = [ROLES[r][0] for r in ROLES if not clean[r]["model"]]
    if missing:
        raise Invalid("Choisis un modèle pour chaque rôle (manque : " + ", ".join(missing) + ").")
    items = listing(c)
    if new and any(p["id"] == pid for p in items):
        raise Invalid(f"Le profil « {name} » existe déjà.")
    items = [p for p in items if p["id"] != pid] + [{"id": pid, "name": name, "roles": clean}]
    db.set_setting(c, "profiles", sorted(items, key=lambda p: p["name"].lower()), by)
    db.log_audit(c, by, "Settings : profil de modèles", detail=f"{name} ({pid})")


def remove(c, by, pid):
    """Retire un profil. Les repos qui l'utilisaient, et le réglage global, reviennent à leur modèle."""
    p = get(c, pid)
    if not p:
        raise Invalid("Profil inconnu.")
    db.set_setting(c, "profiles", [x for x in listing(c) if x["id"] != pid], by)
    glob, _ = db.get_setting(c, "agents")
    if glob.get("profile") == pid:
        db.set_setting(c, "agents", {**glob, "profile": ""}, by)
    for r in db.rows(c, db.repos.select()):
        rs = dict(r["settings"] or {})
        if rs.get("profile") == pid:
            rs.pop("profile")
            c.execute(db.repos.update().where(db.repos.c.id == r["id"]).values(settings=rs))
    db.log_audit(c, by, "Settings : profil retiré", detail=f"{p['name']} ({pid})")


def parse_choice(v):
    """Liste « Modèle ou profil » : « m:<modèle> », « p:<profil> », ou vide (réglage hérité). Renvoie (modèle, profil)."""
    v = str(v or "")
    if v.startswith("p:"):
        return "", v[2:]
    if v.startswith("m:"):
        return v[2:], ""
    return "", ""


def role_of(source):
    return "study" if source == "étude" else "onboarding" if source == "onboarding" else "agent"


def resolve(c, repo_settings, source, job_model="", job_profile=""):
    """Modèles d'un nouveau job. Chaque niveau (demande, repo, global) choisit SOIT un modèle (un seul pour tout), SOIT un
    profil (un modèle par rôle) ; le premier niveau qui a choisi l'emporte. Renvoie (modèle principal,
    {profile, effort, review_model, review_effort}). Effort « provider » : réglage du provider, rien à forcer."""
    glob, _ = db.get_setting(c, "agents")
    rs = repo_settings or {}
    single, prof = "", None
    if job_model or job_profile:
        single, prof = job_model, (get(c, job_profile) if not job_model else None)
    elif get(c, rs.get("profile")):
        prof = get(c, rs.get("profile"))
    elif rs.get("model"):
        single = rs["model"]
    elif get(c, glob.get("profile")):
        prof = get(c, glob.get("profile"))
    else:
        single = glob.get("model") or ""
    if not prof:
        return single, {"profile": "", "effort": INHERIT, "review_model": "", "review_effort": INHERIT}
    roles = prof.get("roles") or {}
    agent, mine = roles.get("agent") or {}, roles.get(role_of(source)) or {}
    model = mine.get("model") or agent.get("model") or rs.get("model") or glob.get("model") or ""
    effort = INHERIT                                     # l'effort est celui du provider du modèle (réglé sur le provider)
    rv = roles.get("review") or {}
    review = rv.get("model") or ""
    return model, {"profile": prof["name"], "effort": INHERIT, "review_model": review if review != model else "", "review_effort": INHERIT}


def apply_effort(runtime, effort):
    """Effort forcé par le profil sur la description runtime d'un modèle (serveurs compatibles seulement)."""
    if runtime.get("type") == "compatible" and effort not in (None, INHERIT):
        runtime = {**runtime, "effort": effort}
    return runtime
