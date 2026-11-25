"""Model providers: Anthropic, OpenAI, or compatible server (llama.cpp, vLLM, Ollama).

An admin declares a provider (type, address, key), loads the actual list of their models and checks those to propose.
Keys are WRITE-ONLY: encrypted in the database with the FABRIQUE_SECRET_KEY master key, never re-displayed.
They only leave the dashboard towards the runner, on the private network, at the moment it takes a job; the runner keeps
them for its key gateway: the job container never sees them.

Each activated model states which APIs it speaks ("anthropic" for Claude Code, "openai" for Codex): the dashboard only
proposes compatible agent + model pairs."""
from .i18n import M
import base64, hashlib, re, time
from urllib.parse import urlsplit
import httpx
from cryptography.fernet import Fernet, InvalidToken
from . import config, db, prices

TYPES = {"anthropic": ("Anthropic", "https://api.anthropic.com"), "openai": ("OpenAI", "https://api.openai.com/v1"),
         "compatible": (M("OpenAI-compatible (llama.cpp, vLLM, Ollama)"), "")}
HARNESS_API = {"claude-code": "anthropic", "codex": "openai"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


TRANSPORT = None   # tests: simulated httpx transport


class Invalid(Exception):
    pass


# ---------- keys ----------

def _fernet():
    if not config.SECRET_KEY:
        raise Invalid(M("FABRIQUE_SECRET_KEY is not set: an API key cannot be stored."))
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


# ---------- reading ----------

def listing(c):
    """Providers (without plaintext keys). First read: deduced from already declared models."""
    provs, _ = db.get_setting(c, "providers")
    if provs is None or provs == []:
        provs = []
        models, _ = db.get_setting(c, "models")
        if any(m.get("provider") == "anthropic" for m in models):
            provs.append({"id": "anthropic", "type": "anthropic", "name": "Anthropic", "base_url": TYPES["anthropic"][1], "key": ""})
        for base in sorted({m.get("base_url") for m in models if m.get("provider") != "anthropic" and m.get("base_url")}):
            host = urlsplit(base).hostname or "local"
            provs.append({"id": "local-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:30], "type": "compatible",
                          "name": M("Local server ({host})", host=host), "base_url": base, "key": ""})
    return [dict(p, has_key=bool(p.get("key"))) for p in provs]


def models_with_provider(c):
    """Activated models, completed with the provider and the APIs they speak (old models: deduced)."""
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
    """Codex: OpenAI models only (it struggles with a local model); Claude Code: Anthropic and compatible
    servers (llama.cpp, vLLM...) that speak the Anthropic API."""
    if harness == "codex":
        return model.get("provider_type") == "openai"
    return "anthropic" in (model.get("apis") or [])


def harnesses_for(model):
    return [h for h in HARNESS_API if compatible(model, h)]


def runtime(c, model_id):
    """What the runner receives for a job: type, address, decrypted key (private network only)."""
    m = next((x for x in models_with_provider(c) if x["id"] == model_id), None)
    if not m:
        return {"id": model_id, "provider": "anthropic", "base_url": "", "key": ""}
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or []) if x["id"] == m["provider_id"]), {})
    typ = m.get("provider_type") or ("anthropic" if m.get("provider") == "anthropic" else "compatible")
    return {"id": m["id"], "provider": "anthropic" if typ == "anthropic" else "openai", "type": typ,
            "base_url": (p.get("base_url") or m.get("base_url") or "") if typ != "anthropic" else "",
            "key": unseal(p.get("key", "")), "apis": m["apis"], "effort": p.get("effort", "medium") if typ == "compatible" else "",
            "thinking_budget": p.get("thinking") if typ == "compatible" else None,
            "review_max_tokens": p.get("review_tokens") if typ == "compatible" else None,
            # Price sent with the model, so that the runner counts the job cost right (None: unknown, the runner's table).
            "price": prices.resolve(c, m["id"], m, local=(typ == "compatible" and not p.get("key")))}


# ---------- modification ----------

EFFORTS = {"": M("Server default"), "low": M("Low"), "medium": M("Medium"), "high": M("High"), "xhigh": M("Maximum")}


def save(c, by, pid, typ, name, base_url, key, keep_key=True, clear_key=False, effort=None, thinking=None, review_tokens=None):
    """Creates or changes a provider; returns its identifier as stored (lowercase)."""
    pid, name, base_url = pid.strip().lower(), name.strip()[:80], (base_url.strip() or TYPES.get(typ, ("", ""))[1]).rstrip("/")
    if typ not in TYPES or not _ID.match(pid) or not name:
        raise Invalid(M("Invalid provider: identifier (lowercase letters, digits, hyphens), type and name."))
    if urlsplit(base_url).scheme not in ("http", "https") or not urlsplit(base_url).hostname:
        raise Invalid(M("Invalid address (http or https)."))
    if typ in ("anthropic", "openai") and not urlsplit(base_url).scheme == "https":
        raise Invalid(M("Official APIs are reached over https."))
    provs = [p for p in listing(c)]
    old = next((p for p in provs if p["id"] == pid), None)
    moved = bool(old) and old.get("base_url") != base_url   # new address: the registered key does not follow it
    if moved and old.get("key") and not key.strip() and not clear_key:
        raise Invalid(M("The address changes: enter the key again (a stored key is never sent to a new address)."))
    sealed = seal(key.strip()) if key.strip() else ("" if clear_key else (old.get("key", "") if old and keep_key else ""))
    # Effort level (thinking duration): compatible servers only; Anthropic and OpenAI keep the agent setting.
    if effort is None:   # field missing: keep the setting; new provider or provider from before this setting: "medium"
        eff = (old or {}).get("effort", "medium")
    else:
        eff = str(effort or "").strip().lower()
    eff = eff if typ == "compatible" and eff in EFFORTS else ""
    # Thinking budget (compatible servers only): tokens of thinking per agent response; empty = the runner's setting.
    if thinking is None:   # field missing: keep the setting
        think = (old or {}).get("thinking")
    else:
        t = str(thinking).strip()
        if t and not (t.isdigit() and int(t) <= 64000):
            raise Invalid(M("Thinking budget: a number of tokens between 0 and 64000, or empty."))
        think = int(t) if t else None
    think = think if typ == "compatible" else None
    # Reviewers' answer limit, thinking included (compatible servers only): a model that thinks a lot (Qwen) needs more than
    # the runner's REVIEW_MAX_TOKENS, or its verdict comes without thinking. Empty = the runner's setting.
    if review_tokens is None:
        rtok = (old or {}).get("review_tokens")
    else:
        t = str(review_tokens).strip()
        if t and not (t.isdigit() and 1000 <= int(t) <= 64000):
            raise Invalid(M("Reviewers' limit: a number of tokens between 1000 and 64000, or empty."))
        rtok = int(t) if t else None
    rtok = rtok if typ == "compatible" else None
    provs = [p for p in provs if p["id"] != pid] + [{"id": pid, "type": typ, "name": name, "base_url": base_url, "key": sealed, "effort": eff,
                                                    "thinking": think, "review_tokens": rtok}]
    db.set_setting(c, "providers", [{k: v for k, v in p.items() if k != "has_key"} for p in provs], by)
    db.log_audit(c, by, M("Settings: model provider"), target=pid,
                 detail=f"{TYPES[typ][0]} · {base_url}" + (M(" · key replaced") if key.strip() else M(" · key deleted") if clear_key else ""))
    return pid


def default_problem(c, harness=None, model=None):
    """Why a job cannot start with this model ('' if all is well): says what to set, and where."""
    agents, _ = db.get_setting(c, "agents")
    mid = model or agents.get("model") or ""
    if not mid:
        return M("No default model: add a model provider, then choose the model in Settings → Agents and models.")
    m = next((x for x in models_with_provider(c) if x["id"] == mid), None)
    if not m:
        return M("Model “{mid}” is no longer offered: choose the default model in Settings → Agents and models.", mid=mid)
    p = next((x for x in listing(c) if x["id"] == m.get("provider_id")), None)
    if p and p["type"] in ("anthropic", "openai") and not p.get("has_key"):
        return (M("The selected model ({name}) comes from provider “{name2}”, which has no key: enter it, or choose another default model in Settings → Agents and models.", name=m['name'], name2=p['name']))
    return ""


def remove(c, by, pid):
    models, _ = db.get_setting(c, "models")
    agents, _ = db.get_setting(c, "agents")
    used = [m["id"] for m in models_with_provider(c) if m["provider_id"] == pid]
    if agents.get("model") in used:
        raise Invalid(M("The default model comes from this provider: choose another one before removing it."))
    db.set_setting(c, "models", [m for m in models if m["id"] not in used], by)
    db.set_setting(c, "providers", [{k: v for k, v in p.items() if k != "has_key"} for p in listing(c) if p["id"] != pid], by)
    db.log_audit(c, by, M("Settings: provider removed"), target=pid, detail=M("{count} model(s) removed", count=len(used)))


def max_jobs(value):
    """The "simultaneous jobs" of a model as entered: an integer from 1 to 99, or None (empty, zero, anything else: no limit)."""
    try:
        n = int(str(value or "").strip())
    except ValueError:
        return None
    return n if 1 <= n <= 99 else None


def enable(c, by, pid, chosen):
    """chosen: [{id, name, apis, max_jobs}] — models checked for this provider (replaces the previous selection)."""
    p = next((x for x in listing(c) if x["id"] == pid), None)
    if not p:
        raise Invalid(M("Unknown provider."))
    models = [m for m in models_with_provider(c) if m["provider_id"] != pid]
    for m in chosen[:40]:
        mid = str(m["id"])[:120]
        apis = [a for a in m.get("apis") or [] if a in ("anthropic", "openai")] or (
            ["anthropic"] if p["type"] == "anthropic" else ["openai"] if p["type"] == "openai" else ["anthropic", "openai"])
        entry = {"id": mid, "name": str(m.get("name") or mid)[:80], "provider_id": pid,
                 "provider": "anthropic" if p["type"] == "anthropic" else "openai",
                 "base_url": "" if p["type"] == "anthropic" else p["base_url"], "apis": apis}
        limit = max_jobs(m.get("max_jobs"))
        if limit:
            entry["max_jobs"] = limit   # jobs using this model at the same time; absent: no limit
        if m.get("price") is not None:   # price entered by hand ($ per million tokens: input, output, cached input)
            try:
                p_ = prices.parse_manual(m.get("price"))
            except ValueError:
                raise Invalid(M("Price of {model}: three numbers, $ per million tokens: input / output / cached input (for example 2 / 10 / 0.1), or empty.", model=mid))
            if p_:
                entry["price"] = p_
        models.append(entry)
    agents, _ = db.get_setting(c, "agents")
    before = {m["id"] for m in models_with_provider(c)}
    if agents.get("model") in before and agents["model"] not in {m["id"] for m in models}:
        raise Invalid(M("The default model would be removed: keep it ticked, or change the default model first."))
    db.set_setting(c, "models", [{k: v for k, v in m.items() if k not in ("provider_type",)} for m in models], by)
    if agents.get("model") not in before:
        # No valid default model yet: the first enabled model that the agent knows how to use becomes it.
        first = next((m for m in models if compatible(m, agents.get("harness") or "claude-code")), None)
        if first:
            db.set_setting(c, "agents", {"harness": agents.get("harness") or "claude-code", "model": first["id"]}, by)
            db.log_audit(c, by, M("Settings: agents"), detail=M("default model: {id} (first model enabled)", id=first['id']))
    db.log_audit(c, by, M("Settings: models enabled"), target=pid, detail=", ".join(m["id"] for m in chosen[:12]) or M("none", _ctx="model"))


# ---------- calls to the provider (from the dashboard, at an admin's request) ----------

def _client(p, key, transport=None):
    h = {"anthropic-version": "2023-06-01", "x-api-key": key} if p["type"] == "anthropic" else (
        {"Authorization": f"Bearer {key}"} if key else {})
    return httpx.Client(timeout=30, headers=h, transport=transport or TRANSPORT)


def _v1(p):
    b = p["base_url"].rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


def fetch_models(c, pid, transport=None):
    """Actual list of models from the provider: [{id, name}] (explicit error otherwise)."""
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or listing(c)) if x["id"] == pid), None)
    if not p:
        raise Invalid(M("Unknown provider."))
    key = unseal(p.get("key", ""))
    if p["type"] in ("anthropic", "openai") and not key:
        raise Invalid(M("First add this provider's API key."))
    try:
        with _client(p, key, transport) as h:
            r = h.get(_v1(p) + "/models", params={"limit": 200} if p["type"] == "anthropic" else None)
    except httpx.HTTPError as e:
        raise Invalid(M("Provider unreachable ({why}).", why=type(e).__name__))
    if r.status_code in (401, 403):
        raise Invalid(M("Key refused by the provider."))
    if r.status_code != 200:
        raise Invalid(M("Unexpected response from the provider ({status_code}).", status_code=r.status_code))
    out = []
    for m in (r.json().get("data") or []):
        mid = str(m.get("id") or "")
        if not mid:
            continue
        if p["type"] == "openai" and not re.match(r"^(gpt-|o\d|codex)", mid):   # no images, audio or embeddings
            continue
        if p["type"] == "openai" and re.search(r"(audio|realtime|transcribe|tts|image|search|embedding)", mid):
            continue
        out.append({"id": mid, "name": str(m.get("display_name") or mid)})
    return sorted(out, key=lambda m: m["id"])


def probe(c, pid, model_id, transport=None):
    """Actual tool call attempt by each API: {"anthropic": bool, "openai": bool} (Claude Code, Codex)."""
    provs, _ = db.get_setting(c, "providers")
    p = next((x for x in (provs or listing(c)) if x["id"] == pid), None)
    if not p:
        raise Invalid(M("Unknown provider."))
    key = unseal(p.get("key", ""))
    tool = {"name": "read_file", "description": "Reads a file of the repo", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}
    ask = "Read the file README.md with the read_file tool."
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
            # Codex only speaks the Responses API: that is what is tried here, with a tool call.
            try:
                r = h.post(_v1(p) + "/responses", json={"model": model_id, "input": ask, "max_output_tokens": 800,
                                                         "tools": [dict(type="function", **tool)]})
                out = r.json().get("output") or [] if r.status_code == 200 else []
                res["openai"] = any(o.get("type") == "function_call" for o in out)
            except (httpx.HTTPError, ValueError):
                pass
    return res


def git_repo_check(typ, base, token, slug, transport=None):
    """None if the repo exists and can be read with this token; otherwise the reason, to be displayed. None also when the Git host does not respond:
    the addition must not depend on a temporary outage (the onboarding will say what the situation is)."""
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
        return M("the Git host refuses access to “{slug}” with this token ({status_code}): check its rights on this repo.", slug=slug, status_code=r.status_code)
    if r.status_code == 404:
        return (M("“{slug}” cannot be found at this provider, or is not visible to the token's account. Check the full path (group/subgroup/project), copying it from the repo's clone address.", slug=slug))
    return None


# ---------- Git providers: testing a robot account token (at an admin's request, nothing is recorded) ----------

def git_check(typ, base, token, org="", transport=None):
    """(ok, message): does the Git host respond to this token, for which account, and does the organization of the new repos exist?"""
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
                return False, M("The Git host refuses this token ({status_code}): it is wrong, expired, or not allowed to read its own account.", status_code=r.status_code)
            if r.status_code != 200:
                return False, M("Unexpected response from the Git host ({status_code}): check the type and the address.", status_code=r.status_code)
            try:
                login = str(r.json().get(who) or "")[:80]
            except ValueError:
                return False, M("The Git host did not answer in JSON: check the type and the address.")
            if not login:
                return False, M("The Git host answered, but without an account name: check the chosen type.")
            msg = M("Connection successful: the token belongs to account “{login}”.", login=login)
            if org:
                from urllib.parse import quote
                g = h.get(api + org_path.format(quote(org, safe="")))
                if g.status_code != 200:
                    return False, msg + M(" However “{org}” cannot be found or is not visible to this account ({status_code}): creating repos would fail.", org=org, status_code=g.status_code)
                msg += M(" “{org}” is visible to this account.", org=org)
            return True, msg + M(" Write access is checked at the first onboarding.")
    except httpx.HTTPError as e:
        return False, M("Git host unreachable from the dashboard ({name}): check the address.", name=type(e).__name__)


def git_repo_search(typ, base, token, q, transport=None, limit=20):
    """Repos the bot account can see whose path contains q: [{slug, private, branch, description}]. Server side only: the
    token never reaches the browser. Raises Invalid when the Git host refuses or cannot be reached."""
    from urllib.parse import quote
    q = (q or "").strip()[:100]
    host = urlsplit(base).hostname or ""
    if typ in ("forgejo", "gitea"):
        api, headers = base + "/api/v1", {"Authorization": "token " + token}
        url, params = api + "/repos/search", {"q": q, "limit": limit, "sort": "updated", "order": "desc"}
    elif typ == "gitlab":
        api, headers = base + "/api/v4", {"PRIVATE-TOKEN": token}
        url, params = api + "/projects", {"membership": "true", "search": q, "search_namespaces": "true", "simple": "true",
                                          "per_page": limit, "order_by": "last_activity_at"}
    else:
        api = "https://api.github.com" if host in ("github.com", "www.github.com") else base + "/api/v3"
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
        url, params = api + "/user/repos", {"per_page": 100, "sort": "updated"}
    try:
        with httpx.Client(timeout=10, headers=headers, follow_redirects=False, transport=transport) as h:
            r = h.get(url, params=params)
    except httpx.HTTPError as e:
        raise Invalid(M("Git host unreachable from the dashboard ({name}).", name=type(e).__name__))
    if r.status_code in (401, 403):
        raise Invalid(M("The Git host refuses the bot's token ({status_code}).", status_code=r.status_code))
    if r.status_code != 200:
        raise Invalid(M("Unexpected response from the Git host ({status_code}).", status_code=r.status_code))
    try:
        data = r.json()
    except ValueError:
        raise Invalid(M("The Git host did not answer in JSON."))
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict):
            continue
        slug = str(x.get("full_name") or x.get("path_with_namespace") or "")
        if not slug or (q and q.lower() not in slug.lower() and q.lower() not in str(x.get("name") or "").lower()):
            continue
        private = bool(x.get("private")) if "private" in x else x.get("visibility") not in (None, "public")
        out.append({"slug": slug[:200], "private": private, "branch": str(x.get("default_branch") or "")[:100],
                    "description": " ".join(str(x.get("description") or "").split())[:120]})
        if len(out) >= limit:
            break
    return out


# ---------- a plain request to a model, from the dashboard (translation of a job's texts) ----------

PRICES = {"claude-opus": (5, 25), "claude-sonnet": (3, 15), "claude-haiku": (1, 5), "gpt-5": (1.25, 10)}   # $ per million tokens, in / out


def price(model_id):
    best = max((k for k in PRICES if model_id.startswith(k)), key=len, default=None)
    return PRICES[best] if best else (5, 25)


def complete(model, system, prompt, max_tokens=1000, timeout=60, transport=None):
    """One request, no tools: (text, cost in $). `model` is what runtime() returns. A local model costs nothing, and its
    thinking is switched off: a translation does not need it, and it would use up the answer."""
    key = model.get("key") or ""
    with httpx.Client(timeout=timeout, transport=transport or TRANSPORT) as h:
        if model.get("provider") == "anthropic":
            base = (model.get("base_url") or "https://api.anthropic.com").rstrip("/")
            r = h.post(base + "/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                       json={"model": model["id"], "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            m = r.json()
            u, (pin, pout) = m.get("usage") or {}, price(model["id"])
            return ("".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text"),
                    (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6)
        base = (model.get("base_url") or "").rstrip("/")
        base = base if base.endswith("/v1") else base + "/v1"
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        official = model.get("type") == "openai"
        body = ({"model": model["id"], "max_completion_tokens": max_tokens + 2000, "messages": msgs, "reasoning_effort": "low"} if official
                else {"model": model["id"], "temperature": 0, "max_tokens": max_tokens, "messages": msgs,
                      "chat_template_kwargs": {"enable_thinking": False}})
        r = h.post(base + "/chat/completions", headers={"Authorization": f"Bearer {key or 'local'}"}, json=body)
        r.raise_for_status()
        m = r.json()
    u, cost = m.get("usage") or {}, 0.0
    if official:
        pin, pout = price(model["id"])
        cost = (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
    return str(((m.get("choices") or [{}])[0].get("message") or {}).get("content") or ""), cost

