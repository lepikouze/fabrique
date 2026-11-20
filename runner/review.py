"""Relectures : un relecteur par rôle, sur le diff vérifié, sans aucun droit d'écriture.
Rôles : code et sécurité toujours ; architecture et infra selon les fichiers touchés. Le verdict est lu dans un bloc JSON ;
une réponse illisible vaut « indéterminé » (jamais « ok »).

Deux modes (REVIEW_MODE) :
- « direct » (défaut) : le runner envoie UNE requête au modèle (consignes + demande + diff) et lit le verdict. Pas de boucle
  d'agent, pas d'outils : rapide, même sur un modèle local qui sert une requête à la fois.
- « agent » : Claude Code en lecture seule (Read, Glob, Grep) dans l'espace du job ; il peut ouvrir d'autres fichiers.
Le mode direct demande un modèle local (API compatible OpenAI) ou une clé d'API Anthropic ; sinon, repli sur « agent »."""
import json, logging, os, re
import httpx
from . import config
from .agent import base_env, model_env
from .messages import data

log = logging.getLogger("runner.review")
HERE = os.path.dirname(__file__)
TEMPLATE = open(os.path.join(HERE, "review.md"), encoding="utf-8").read()
ROLES = {
    "code": ("code", "la justesse : le changement fait-il ce que la demande veut, sans casser l'existant ? Cas limites, "
                     "erreurs non gérées, tests qui ne testent rien, code mort."),
    "securite": ("sécurité", "les failles : entrées non validées, injection, secrets écrits en dur, droits trop larges, "
                             "données personnelles exposées, dépendances douteuses ajoutées."),
    "archi": ("architecture", "la structure : le changement respecte-t-il les modules existants, crée-t-il des dépendances "
                              "ou un couplage inutiles, une duplication, une API publique fragile ?"),
    "infra": ("infra et exploitation", "le déploiement : conteneur, CI, migrations, configuration, ressources, journaux ; "
                                       "ce qui peut casser en production, et le retour arrière."),
}
INFRA = re.compile(r"(^|/)(Dockerfile|docker-compose[^/]*|compose[^/]*\.ya?ml|\.forgejo/|\.gitea/|\.github/|\.gitlab-ci\.yml|k8s/|helm/|"
                   r"terraform/|.*\.tf$|migrations?/|alembic|.*\.env.*|Makefile|\.fabrique/)", re.I)
ARCHI = re.compile(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|package\.json|setup\.(py|cfg)|go\.mod|Cargo\.toml)$|__init__\.py$")


def roles_for(files):
    roles = ["code", "securite"]
    new_dirs = {f.split("/")[0] for f in files if "/" in f}
    if any(ARCHI.search(f) for f in files) or len(files) > 8 or len(new_dirs) > 3:
        roles.append("archi")
    if any(INFRA.search(f) for f in files):
        roles.append("infra")
    return roles


def parse(text):
    """Dernier objet JSON de la réponse qui porte un verdict : (verdict, résumé, points, preuve) ; sinon indéterminé."""
    dec, found = json.JSONDecoder(), None
    text = text or ""
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                d, _ = dec.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(d, dict) and "verdict" in d:
                found = d
    if found:
        v = str(found.get("verdict", "")).strip().lower().replace("à", "a").replace(" ", "_")
        if v in ("ok", "a_revoir", "bloquant"):
            return (v, str(found.get("resume") or "")[:2000], [str(p)[:400] for p in (found.get("points") or [])][:12],
                    str(found.get("preuve") or "").strip()[:1000] if v == "bloquant" else "")
    return "indetermine", "Réponse du relecteur illisible : à relire par un humain.", [], ""


OUTILS = {"direct": "Tu n'as aucun outil : tout ce que tu peux relire est dans le diff ci-dessous. S'il est tronqué, dis-le dans "
                     "ton résumé ; ne devine pas le reste.",
          "agent": "Le diff est fourni ci-dessous : lis un fichier seulement si le diff ne suffit pas, et réponds vite."}


REVIEW_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def mode(model):
    if config.REVIEW_MODE != "direct":
        return "agent"
    if model.get("provider") == "anthropic":
        return "direct" if (model.get("key") or config.ANTHROPIC_API_KEY) else "agent"
    return "direct" if model.get("base_url") else "agent"


def prompts(ctx, role, diff, how):
    label, mission = ROLES[role]
    system = TEMPLATE.format(role_label=label, mission=mission, outils=OUTILS[how])
    extra = ""
    report = getattr(ctx, "security_report", "") or ""
    if role == "securite" and report:
        # Résultats du contrôle automatique (Betterleaks, registres de paquets) : des indices à vérifier, pas des verdicts.
        extra = (f"<controle_automatique>\n{data(report[:4000])}\n</controle_automatique>\n\nLe bloc ci-dessus vient d'outils "
                 "automatiques. Ce sont des indices : vérifie chacun dans le diff. Écarte les faux positifs (une valeur factice dans "
                 "un test, par exemple) en le disant dans un point, et reprends les vrais problèmes dans tes points.\n\n")
    if role == "code":
        extra = ("Commence tes points par un point par critère de « Terminé quand » (ou, s'il n'est pas précisé, par exigence de la "
                 "demande), sous cette forme exacte : « Critère traité : … », « Critère non traité : … » ou « Critère pas clair : … », "
                 "suivi d'une courte explication tirée du diff.\n\n")
    prompt = (f"<demande>\nTitre : {data(ctx.title)}\n\n{data(ctx.job['request'])}\n\nTerminé quand : {data(ctx.job.get('done_when')) or '(non précisé)'}\n</demande>\n\n"
              f"<diff>\n{data(diff)}\n</diff>\n\n{extra}Relis pour ton rôle ({label}) et réponds uniquement par le bloc JSON demandé.")
    return label, system, prompt


EFFORT = {1: "trivial", 2: "léger", 3: "moyen", 4: "conséquent", 5: "lourd"}


def effort(lines, files, protected=False):
    """Effort de relecture, de 1 à 5, calculé sur la taille du changement (sans modèle) ; +1 si un chemin protégé est touché."""
    n = 1 if lines <= 30 and files <= 2 else 2 if lines <= 120 and files <= 5 else 3 if lines <= 300 and files <= 10 else 4 if lines <= 800 else 5
    return min(5, n + (1 if protected else 0))


CRITERION = re.compile(r"^\s*crit[èe]re\s+(trait[ée]|non\s+trait[ée]|pas\s+clair)\s*:\s*(.+)$", re.I | re.S)


def criteria(points):
    """Points du relecteur code qui évaluent un critère de la demande : [(état, texte)], état = ok | non | flou."""
    out = []
    for p in points or []:
        m = CRITERION.match(str(p))
        if m:
            k = m.group(1).lower()
            out.append(("non" if k.startswith("non") else "flou" if k.startswith("pas") else "ok", m.group(2).strip()))
    return out


CAUSE_CUT = "réponse coupée par la limite de jetons du modèle avant le verdict"
CAUSE_NOJSON = "aucun verdict JSON dans la réponse du modèle"


def ask(model, system, prompt, timeout, transport=None, think=True):
    """Une requête au modèle ; renvoie (texte, coût en $, coupée par la limite de jetons ?). think=False coupe la réflexion
    d'un modèle local « thinking » (llama.cpp, vLLM : chat_template_kwargs.enable_thinking)."""
    if model.get("provider") == "anthropic":
        with httpx.Client(base_url=config.ANTHROPIC_API_URL, timeout=timeout, transport=transport) as h:
            r = h.post("/v1/messages", headers={"x-api-key": model.get("key") or config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                       json={"model": model["id"], "max_tokens": config.REVIEW_MAX_TOKENS, "system": system,
                             "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            m = r.json()
        u = m.get("usage") or {}
        pin, pout, _ = config.price(model["id"])
        cost = (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
        cut = m.get("stop_reason") == "max_tokens"
        if cut:
            log.warning("relecture coupée par la limite de jetons (REVIEW_MAX_TOKENS=%s)", config.REVIEW_MAX_TOKENS)
        return "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text"), cost, cut
    base = model["base_url"].rstrip("/")
    base = base if base.endswith("/v1") else base + "/v1"
    with httpx.Client(timeout=timeout, transport=transport) as h:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        if model.get("type") == "openai":   # API officielle : modèles à raisonnement, pas de température, autre nom de limite
            body = {"model": model["id"], "max_completion_tokens": config.REVIEW_MAX_TOKENS, "messages": msgs,
                    "reasoning_effort": "medium" if think else "low"}
        else:
            body = {"model": model["id"], "temperature": 0, "max_tokens": config.REVIEW_MAX_TOKENS, "messages": msgs}
            if not think:
                body["chat_template_kwargs"] = {"enable_thinking": False}
        r = h.post(base + "/chat/completions", headers={"Authorization": f"Bearer {model.get('key') or 'local'}"}, json=body)
        r.raise_for_status()
        m = r.json()
    ch = (m.get("choices") or [{}])[0]
    u = m.get("usage") or {}
    cost = 0.0
    if model.get("type") == "openai":
        pin, pout, _ = config.price(model["id"])
        cost = (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
    cut = ch.get("finish_reason") == "length"   # la réflexion du modèle a épuisé la limite avant le verdict
    if cut:
        log.warning("relecture coupée par la limite de jetons (REVIEW_MAX_TOKENS=%s)", config.REVIEW_MAX_TOKENS)
    return str((ch.get("message") or {}).get("content") or ""), cost, cut


def run(ctx, role, diff, transport=None):
    rm = getattr(ctx, "review_model", None) or ctx.model   # modèle des relecteurs (profil), sinon celui de l'agent
    how = mode(rm)
    label, system, prompt = prompts(ctx, role, diff, how)
    if how == "direct":
        note = ""
        try:
            text, cost, cut = ask(rm, system, prompt, config.REVIEW_TIMEOUT, transport)
            if cut and rm.get("type", "compatible") == "compatible" and rm.get("provider") != "anthropic":
                # Le modèle a tourné en rond dans sa réflexion : même relecture, réflexion coupée, plutôt que pas de verdict.
                log.warning("%s : relecture %s : réflexion trop longue, nouvelle demande sans réflexion", ctx.ref, role)
                text, cost2, cut = ask(rm, system, prompt, config.REVIEW_TIMEOUT, transport, think=False)
                cost += cost2
                note = "Verdict rendu sans réflexion : celle du modèle dépassait la limite de jetons."
        except httpx.TimeoutException:
            return unreadable(role, f"le modèle n'a pas répondu en {config.REVIEW_TIMEOUT} s")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("%s : relecture %s impossible (%s)", ctx.ref, role, type(e).__name__)
            return unreadable(role, f"modèle injoignable ou en erreur ({type(e).__name__})")
        verdict, summary, points, proof = parse(text)
        if verdict == "indetermine":
            return unreadable(role, CAUSE_CUT if cut else CAUSE_NOJSON, cost)
        return {"role": role, "label": label, "verdict": verdict, "summary": summary, "points": points + ([note] if note else []),
                "proof": proof, "cost": cost}
    menv, local = model_env(rm, ctx.ex.gateway_url(), ctx.fab_token)
    # Le vérificateur ne doit rien au job qu'il juge : programme lancé par son chemin absolu, PATH du système seulement
    # (pas les dossiers du HOME du job), dossier personnel neuf et lecture d'un export du commit vérifié, tous deux
    # préparés par le runner après l'arrêt des processus du job (Runner.reviews).
    where, home = getattr(ctx, "review_dir", None) or ctx.repo_dir, getattr(ctx, "review_home", None) or ctx.home
    env = base_env(home, {"FAB_WORKSPACE": where, "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", **menv})
    env["PATH"] = REVIEW_PATH
    cmd = [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--max-turns", "8",
                        "--allowedTools", "Read Glob Grep", "--disallowedTools", "Edit Write MultiEdit Bash WebFetch WebSearch Task",
                        "--append-system-prompt", system, "--model", rm["id"]]
    try:
        p = ctx.ex.run(ctx, cmd, env, where, config.REVIEW_TIMEOUT)
        out = json.loads(p.stdout or "{}")
    except (RuntimeError, ValueError) as e:
        log.warning("%s : relecture %s illisible (%s)", ctx.ref, role, type(e).__name__)
        return unreadable(role, f"relecteur interrompu ({type(e).__name__})")
    verdict, summary, points, proof = parse(out.get("result", ""))
    cost = 0.0 if local else float(out.get("total_cost_usd") or 0)
    if verdict == "indetermine":
        return unreadable(role, "délai dépassé" if p.returncode == 124 else CAUSE_NOJSON, cost)
    return {"role": role, "label": label, "verdict": verdict, "summary": summary, "points": points, "proof": proof, "cost": cost}


def unreadable(role, cause, cost=0.0):
    """Relecture sans verdict exploitable : jamais « ok » ; la cause est dite, pour que l'humain sache quoi faire."""
    label = ROLES[role][0]
    return {"role": role, "label": label, "verdict": "indetermine", "cause": cause, "points": [], "proof": "", "cost": cost,
            "summary": f"Relecture illisible : {cause}. Ce n'est pas un avis négatif : aucun verdict n'a été rendu."}


def confirm(r, replay):
    """Un « bloquant » n'est retenu que si sa preuve, rejouée par le runner dans l'espace du job, échoue.
    replay(cmd) -> (code de sortie, sortie). Sinon il devient « à revoir » (ni faux blocage, ni « ok »)."""
    if r["verdict"] != "bloquant":
        return r
    if not r.get("proof"):
        return {**r, "verdict": "a_revoir", "summary": "Bloquant non démontré (aucune preuve fournie) : " + r["summary"]}
    code, out = replay(r["proof"])
    shown = f"Preuve rejouée par le runner : `{r['proof'][:200]}` → code {code}"
    if code == 0:
        return {**r, "verdict": "a_revoir", "summary": "Bloquant non démontré (la preuve réussit) : " + r["summary"],
                "points": r["points"] + [shown]}
    return {**r, "points": r["points"] + [shown + (f" : {out.strip()[-300:]}" if out.strip() else "")]}
