"""Providers de modèles : Anthropic, OpenAI, ou serveur compatible (llama.cpp, vLLM, Ollama).

Un admin déclare un provider (type, adresse, clé), charge la liste réelle de ses modèles et coche ceux à proposer.
Les clés sont en ÉCRITURE SEULE : chiffrées dans la base avec la clé maître FABRIQUE_SECRET_KEY, jamais réaffichées.
Elles ne sortent du dashboard que vers le runner, sur le réseau privé, au moment où il prend un job ; le runner les garde
pour sa key gateway : le conteneur du job ne les voit jamais.

Chaque modèle activé dit quelles API il parle (« anthropic » pour Claude Code, « openai » pour Codex) : le dashboard ne
propose que les couples agent + modèle compatibles."""
import base64, hashlib, re, time
from urllib.parse import urlsplit
import httpx
from cryptography.fernet import Fernet, InvalidToken
from . import config, db

TYPES = {"anthropic": ("Anthropic", "https://api.anthropic.com"), "openai": ("OpenAI", "https://api.openai.com/v1"),
         "compatible": ("Compatible OpenAI (llama.cpp, vLLM, Ollama)", "")}
HARNESS_API = {"claude-code": "anthropic", "codex": "openai"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


TRANSPORT = None   # tests : transport httpx simulé


class Invalid(Exception):
    pass


# ---------- clés ----------

def _fernet():
    if not config.SECRET_KEY:
        raise Invalid("FABRIQUE_SECRET_KEY n'est pas définie : impossible d'enregistrer une clé d'API.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest()))


def seal(key):
    return _fernet().encrypt(key.encode()).decode() if key else ""


def unseal(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Invalid):
        return ""


# ---------- lecture ----------

def listing(c):
    """Providers (sans les clés en clair). Première lecture : déduits des modèles déjà déclarés."""
    provs, _ = db.get_setting(c, "providers")
    if provs is None or provs == []:
        provs = []
        models, _ = db.get_setting(c, "models")
        if any(m.get("provider") == "anthropic" for m in models):
            provs.append({"id": "anthropic", "type": "anthropic", "name": "Anthropic", "base_url": TYPES["anthropic"][1], "key": ""})
        for base in sorted({m.get("base_url") for m in models if m.get("provider") != "anthropic" and m.get("base_url")}):
            host = urlsplit(base).hostname or "local"
            provs.append({"id": "local-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:30], "type": "compatible",
                          "name": f"Serveur local ({host})", "base_url": base, "key": ""})
    return [dict(p, has_key=bool(p.get("key"))) for p in provs]


def models_with_provider(c):
    """Modèles activés, complétés du provider et des API qu'ils parlent (anciens modèles : déduits)."""
    models, _ = db.get_setting(c, "models")
    provs = {p["id"]: p for p in listing(c)}
    out = []
    for m in models:
        pid = m.get("provider_id") or ("anthropic" if m.get("provider") == "anthropic" else next(
            (p["id"] for p in provs.values() if p["base_url"] == m.get("base_url")), ""))
        p = provs.get(pid) or {}
        apis = m.get("apis") or (["anthropic"] if p.get("type") == "anthropic" else ["openai"] if p.get("type") == "openai"
                                 else ["anthropic", "openai"])
        out.append(dict(m, provider_id=pid, provider_type=p.get("type", ""), apis=apis))
    return out


def compatible(model, harness):
    """Codex : modèles d'OpenAI uniquement (avec un modèle local il s'en sort mal) ; Claude Code : Anthropic et serveurs
    compatibles (llama.cpp, vLLM…) qui parlent l'API d'Anthropic."""
    if harness == "codex":
        return model.get("provider_type") == "openai"
    return "anthropic" in (model.get("apis") or [])


def harnesses_for(model):
    return [h for h in HARNESS_API if compatible(model, h)]


def runtime(c, model_id):
    """Ce que le runner reçoit pour un job : type, adresse, clé déchiffrée (réseau privé seulement)."""
    m = next((x for x in models_with_provider(c) if x["id"] == model_id), None)
    if not m:
        return {"id": model_id, "provider": "anthropic", "base_url": "", "key": ""}
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or []) if x["id"] == m["provider_id"]), {})
    typ = m.get("provider_type") or ("anthropic" if m.get("provider") == "anthropic" else "compatible")
    return {"id": m["id"], "provider": "anthropic" if typ == "anthropic" else "openai", "type": typ,
            "base_url": (p.get("base_url") or m.get("base_url") or "") if typ != "anthropic" else "",
            "key": unseal(p.get("key", "")), "apis": m["apis"], "effort": p.get("effort", "medium") if typ == "compatible" else ""}


# ---------- modification ----------

EFFORTS = {"": "Défaut du serveur", "low": "Bas (low)", "medium": "Moyen (medium)", "high": "Élevé (high)", "xhigh": "Maximal (xhigh)"}


def save(c, by, pid, typ, name, base_url, key, keep_key=True, clear_key=False, effort=None):
    pid, name, base_url = pid.strip().lower(), name.strip()[:80], (base_url.strip() or TYPES.get(typ, ("", ""))[1]).rstrip("/")
    if typ not in TYPES or not _ID.match(pid) or not name:
        raise Invalid("Provider invalide : identifiant (minuscules, chiffres, tirets), type et nom.")
    if urlsplit(base_url).scheme not in ("http", "https") or not urlsplit(base_url).hostname:
        raise Invalid("Adresse invalide (http ou https).")
    if typ in ("anthropic", "openai") and not urlsplit(base_url).scheme == "https":
        raise Invalid("Les API officielles se joignent en https.")
    provs = [p for p in listing(c)]
    old = next((p for p in provs if p["id"] == pid), None)
    moved = bool(old) and old.get("base_url") != base_url   # nouvelle adresse : la clé enregistrée ne la suit pas
    if moved and old.get("key") and not key.strip() and not clear_key:
        raise Invalid("L'adresse change : saisis à nouveau la clé (une clé enregistrée n'est jamais envoyée à une nouvelle adresse).")
    sealed = seal(key.strip()) if key.strip() else ("" if clear_key else (old.get("key", "") if old and keep_key else ""))
    # Niveau d'effort (durée de réflexion) : serveurs compatibles seulement ; Anthropic et OpenAI gardent le réglage de l'agent.
    if effort is None:   # champ absent : on garde le réglage ; nouveau provider ou provider d'avant ce réglage : « medium »
        eff = (old or {}).get("effort", "medium")
    else:
        eff = str(effort or "").strip().lower()
    eff = eff if typ == "compatible" and eff in EFFORTS else ""
    provs = [p for p in provs if p["id"] != pid] + [{"id": pid, "type": typ, "name": name, "base_url": base_url, "key": sealed, "effort": eff}]
    db.set_setting(c, "providers", [{k: v for k, v in p.items() if k != "has_key"} for p in provs], by)
    db.log_audit(c, by, "Settings : provider de modèles", target=pid,
                 detail=f"{TYPES[typ][0]} · {base_url}" + (" · clé remplacée" if key.strip() else " · clé supprimée" if clear_key else ""))


def default_problem(c, harness=None, model=None):
    """Pourquoi un job ne peut pas partir avec ce modèle ('' si tout va bien) : dit quoi régler, et où."""
    agents, _ = db.get_setting(c, "agents")
    mid = model or agents.get("model") or ""
    if not mid:
        return "Aucun modèle par défaut : ajoute un provider de modèles puis choisis le modèle dans Settings → Agents et modèles."
    m = next((x for x in models_with_provider(c) if x["id"] == mid), None)
    if not m:
        return f"Le modèle « {mid} » n'est plus proposé : choisis le modèle par défaut dans Settings → Agents et modèles."
    p = next((x for x in listing(c) if x["id"] == m.get("provider_id")), None)
    if p and p["type"] in ("anthropic", "openai") and not p.get("has_key"):
        return (f"Le modèle retenu ({m['name']}) vient du provider « {p['name']} », qui n'a pas de clé : saisis-la, ou choisis "
                "un autre modèle par défaut dans Settings → Agents et modèles.")
    return ""


def remove(c, by, pid):
    models, _ = db.get_setting(c, "models")
    agents, _ = db.get_setting(c, "agents")
    used = [m["id"] for m in models_with_provider(c) if m["provider_id"] == pid]
    if agents.get("model") in used:
        raise Invalid("Le modèle par défaut vient de ce provider : choisis-en un autre avant de le retirer.")
    db.set_setting(c, "models", [m for m in models if m["id"] not in used], by)
    db.set_setting(c, "providers", [{k: v for k, v in p.items() if k != "has_key"} for p in listing(c) if p["id"] != pid], by)
    db.log_audit(c, by, "Settings : provider retiré", target=pid, detail=f"{len(used)} modèle(s) retiré(s)")


def enable(c, by, pid, chosen):
    """chosen : [{id, name, apis}] — modèles cochés pour ce provider (remplace la sélection précédente)."""
    p = next((x for x in listing(c) if x["id"] == pid), None)
    if not p:
        raise Invalid("Provider inconnu.")
    models = [m for m in models_with_provider(c) if m["provider_id"] != pid]
    for m in chosen[:40]:
        mid = str(m["id"])[:120]
        apis = [a for a in m.get("apis") or [] if a in ("anthropic", "openai")] or (
            ["anthropic"] if p["type"] == "anthropic" else ["openai"] if p["type"] == "openai" else ["anthropic", "openai"])
        models.append({"id": mid, "name": str(m.get("name") or mid)[:80], "provider_id": pid,
                       "provider": "anthropic" if p["type"] == "anthropic" else "openai",
                       "base_url": "" if p["type"] == "anthropic" else p["base_url"], "apis": apis})
    agents, _ = db.get_setting(c, "agents")
    before = {m["id"] for m in models_with_provider(c)}
    if agents.get("model") in before and agents["model"] not in {m["id"] for m in models}:
        raise Invalid("Le modèle par défaut serait retiré : garde-le coché, ou change d'abord le modèle par défaut.")
    db.set_setting(c, "models", [{k: v for k, v in m.items() if k not in ("provider_type",)} for m in models], by)
    if agents.get("model") not in before:
        # Pas encore de modèle par défaut valable : le premier modèle activé que l'agent sait utiliser le devient.
        first = next((m for m in models if compatible(m, agents.get("harness") or "claude-code")), None)
        if first:
            db.set_setting(c, "agents", {"harness": agents.get("harness") or "claude-code", "model": first["id"]}, by)
            db.log_audit(c, by, "Settings : agents", detail=f"modèle par défaut : {first['id']} (premier modèle activé)")
    db.log_audit(c, by, "Settings : modèles activés", target=pid, detail=", ".join(m["id"] for m in chosen[:12]) or "aucun")


# ---------- appels au fournisseur (depuis le panneau, à la demande d'un admin) ----------

def _client(p, key, transport=None):
    h = {"anthropic-version": "2023-06-01", "x-api-key": key} if p["type"] == "anthropic" else (
        {"Authorization": f"Bearer {key}"} if key else {})
    return httpx.Client(timeout=30, headers=h, transport=transport or TRANSPORT)


def _v1(p):
    b = p["base_url"].rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


def fetch_models(c, pid, transport=None):
    """Liste réelle des modèles du provider : [{id, name}] (erreur explicite sinon)."""
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or listing(c)) if x["id"] == pid), None)
    if not p:
        raise Invalid("Provider inconnu.")
    key = unseal(p.get("key", ""))
    if p["type"] in ("anthropic", "openai") and not key:
        raise Invalid("Ajoute d'abord la clé d'API de ce provider.")
    try:
        with _client(p, key, transport) as h:
            r = h.get(_v1(p) + "/models", params={"limit": 200} if p["type"] == "anthropic" else None)
    except httpx.HTTPError as e:
        raise Invalid(f"Provider injoignable ({type(e).__name__}).")
    if r.status_code in (401, 403):
        raise Invalid("Clé refusée par le provider.")
    if r.status_code != 200:
        raise Invalid(f"Réponse inattendue du provider ({r.status_code}).")
    out = []
    for m in (r.json().get("data") or []):
        mid = str(m.get("id") or "")
        if not mid:
            continue
        if p["type"] == "openai" and not re.match(r"^(gpt-|o\d|codex)", mid):   # pas d'images, d'audio, d'embeddings
            continue
        if p["type"] == "openai" and re.search(r"(audio|realtime|transcribe|tts|image|search|embedding)", mid):
            continue
        out.append({"id": mid, "name": str(m.get("display_name") or mid)})
    return sorted(out, key=lambda m: m["id"])


def probe(c, pid, model_id, transport=None):
    """Essai réel d'appel d'outil par chaque API : {"anthropic": bool, "openai": bool} (Claude Code, Codex)."""
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or listing(c)) if x["id"] == pid), None)
    if not p:
        raise Invalid("Provider inconnu.")
    key = unseal(p.get("key", ""))
    tool = {"name": "lire_fichier", "description": "Lit un fichier du repo", "parameters": {
        "type": "object", "properties": {"chemin": {"type": "string"}}, "required": ["chemin"]}}
    ask = "Lis le fichier README.md avec l'outil lire_fichier."
    res = {"anthropic": False, "openai": False}
    with _client(p, key, transport) as h:
        h.timeout = httpx.Timeout(180)
        if p["type"] in ("anthropic", "compatible"):
            try:
                r = h.post(_v1(p) + "/messages", headers={"anthropic-version": "2023-06-01", "x-api-key": key or "local"},
                           json={"model": model_id, "max_tokens": 400, "messages": [{"role": "user", "content": ask}],
                                 "tools": [{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}]})
                res["anthropic"] = r.status_code == 200 and any(b.get("type") == "tool_use" for b in r.json().get("content") or [])
            except (httpx.HTTPError, ValueError):
                pass
        if p["type"] in ("openai", "compatible"):
            # Codex ne parle que l'API Responses : c'est elle qu'on essaie, avec un appel d'outil.
            try:
                r = h.post(_v1(p) + "/responses", json={"model": model_id, "input": ask, "max_output_tokens": 800,
                                                         "tools": [dict(type="function", **tool)]})
                out = r.json().get("output") or [] if r.status_code == 200 else []
                res["openai"] = any(o.get("type") == "function_call" for o in out)
            except (httpx.HTTPError, ValueError):
                pass
    return res


def git_repo_check(typ, base, token, slug, transport=None):
    """None si le repo existe et se lit avec ce token ; sinon la raison, à afficher. None aussi quand la forge ne répond
    pas : l'ajout ne doit pas dépendre d'une panne passagère (l'onboarding dira ce qu'il en est)."""
    from urllib.parse import quote
    host = urlsplit(base).hostname or ""
    if typ == "gitlab":
        url, headers = f"{base}/api/v4/projects/{quote(slug, safe='')}", {"PRIVATE-TOKEN": token}
    elif typ in ("forgejo", "gitea"):
        url, headers = f"{base}/api/v1/repos/{slug}", {"Authorization": "token " + token}
    else:
        api = "https://api.github.com" if host in ("github.com", "www.github.com") else base + "/api/v3"
        url, headers = f"{api}/repos/{slug}", {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    try:
        with httpx.Client(timeout=10, headers=headers, follow_redirects=False, transport=transport) as h:
            r = h.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return None
    if r.status_code in (401, 403):
        return f"la forge refuse l'accès à « {slug} » avec ce token ({r.status_code}) : vérifie ses droits sur ce repo."
    if r.status_code == 404:
        return (f"« {slug} » est introuvable chez ce provider, ou invisible pour le compte du token. Vérifie le chemin complet "
                "(groupe/sous-groupe/projet), en le copiant depuis l'adresse de clonage du repo.")
    return None


# ---------- providers Git : essai du token d'un compte robot (à la demande d'un admin, rien n'est enregistré) ----------

def git_check(typ, base, token, org="", transport=None):
    """(ok, message) : la forge répond-elle à ce token, pour quel compte, et l'organisation des nouveaux repos existe-t-elle ?"""
    host = urlsplit(base).hostname or ""
    if typ in ("forgejo", "gitea"):
        api, headers, who, org_path = base + "/api/v1", {"Authorization": "token " + token}, "login", "/orgs/{}"
    elif typ == "gitlab":
        api, headers, who, org_path = base + "/api/v4", {"PRIVATE-TOKEN": token}, "username", "/groups/{}"
    else:
        api = "https://api.github.com" if host in ("github.com", "www.github.com") else base + "/api/v3"
        headers, who, org_path = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, "login", "/orgs/{}"
    try:
        with httpx.Client(timeout=10, headers=headers, follow_redirects=False, transport=transport) as h:
            r = h.get(api + "/user")
            if r.status_code in (401, 403):
                return False, f"La forge refuse ce token ({r.status_code}) : il est faux, expiré, ou n'a pas le droit de lire son propre compte."
            if r.status_code != 200:
                return False, f"Réponse inattendue de la forge ({r.status_code}) : vérifie le type et l'adresse."
            try:
                login = str(r.json().get(who) or "")[:80]
            except ValueError:
                return False, "La forge n'a pas répondu en JSON : vérifie le type et l'adresse."
            if not login:
                return False, "La forge a répondu, mais sans nom de compte : vérifie le type choisi."
            msg = f"Connexion réussie : le token est celui du compte « {login} »."
            if org:
                from urllib.parse import quote
                g = h.get(api + org_path.format(quote(org, safe="")))
                if g.status_code != 200:
                    return False, msg + f" En revanche « {org} » est introuvable ou invisible pour ce compte ({g.status_code}) : la création de repos échouerait."
                msg += f" « {org} » est visible pour ce compte."
            return True, msg + " Les droits d'écriture se vérifient au premier onboarding."
    except httpx.HTTPError as e:
        return False, f"Forge injoignable depuis le dashboard ({type(e).__name__}) : vérifie l'adresse."
