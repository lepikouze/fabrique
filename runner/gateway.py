"""Passerelle de clés : l'agent, dans son conteneur, parle à son fournisseur de modèle À TRAVERS le runner, avec le jeton
éphémère de son job. Le runner remplace ce jeton par la vraie clé d'API (reçue du panneau) et relaie la requête, réponse
en flux comprise. La clé ne quitte jamais le runner : un agent piégé ne peut ni la lire ni l'envoyer ailleurs.

Chemin : /m/<suite> -> <racine du fournisseur du job>/<suite> (Claude Code ajoute /v1/messages, Codex /v1/responses…)."""
import json, logging, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from . import config

log = logging.getLogger("runner.gateway")
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
       "content-length", "host", "authorization", "x-api-key", "accept-encoding"}


def upstream(model):
    """(racine du fournisseur, en-têtes d'authentification) pour le modèle d'un job ; (None, None) si rien à relayer.
    Un serveur local compatible SANS clé (llama.cpp, vLLM) passe aussi par la passerelle, sans en-tête : les jobs n'ont
    ainsi jamais besoin d'atteindre la machine du modèle (elle n'est pas dans la liste blanche du proxy)."""
    typ = model.get("type") or ("anthropic" if model.get("provider") == "anthropic" else "compatible")
    key = model.get("key") or (config.ANTHROPIC_API_KEY if typ == "anthropic" else "")
    if not key:
        base = (model.get("base_url") or "").rstrip("/")
        if typ == "compatible" and base:
            return (base[:-3] if base.endswith("/v1") else base), {}
        return None, None
    if typ == "anthropic":
        return config.ANTHROPIC_API_URL.rstrip("/"), {"x-api-key": key}
    base = (model.get("base_url") or "").rstrip("/")
    return (base[:-3] if base.endswith("/v1") else base), {"authorization": f"Bearer {key}"}


# Seuls les points d'entrée dont les agents ont besoin (Claude Code : API Messages et son comptage de jetons ; Codex : API
# Responses ; serveurs compatibles : Chat Completions). Comparaison EXACTE du chemin, en POST seulement : ni sous-ressource
# (lots, fichiers…), ni « .. », ni autre méthode. L'adresse amont est bâtie à partir de la constante, jamais du chemin reçu.
PATHS = ("/v1/messages", "/v1/messages/count_tokens", "/v1/responses", "/v1/chat/completions")
RESERVE = 0.02   # dollars réservés par appel en cours, au moins : des appels simultanés ne passent pas tous sur le dernier centime
USAGE = re.compile(rb'"(input_tokens|output_tokens|cache_read_input_tokens|cache_creation_input_tokens|cached_tokens|prompt_tokens|completion_tokens)"\s*:\s*(\d+)')
MAX_SCAN = 4 * 1024 * 1024   # début de la réponse gardé pour les compteurs ; la fin l'est aussi (TAIL), où un flux les termine
TAIL = 1024 * 1024
MAX_BODY = 64 * 1024 * 1024   # corps d'une requête d'agent : au-delà, refusée (le runner ne la met pas en mémoire)


def estimate(model_id, sent, received):
    """Coût estimé d'un appel payant dont la réponse ne porte aucun compteur d'usage (flux sans usage, réponse coupée…) :
    environ quatre octets par jeton. Un appel accepté par un fournisseur payant ne compte jamais pour zéro."""
    pin, pout, _ = config.price(model_id or "")
    return (sent / 4 * pin + received / 4 * pout) / 1e6


def cost_of(model_id, data):
    """Coût d'une réponse du fournisseur, d'après les compteurs d'usage qu'elle contient (JSON ou flux SSE ; les compteurs
    sont cumulatifs, on garde le plus grand de chaque)."""
    u = {}
    for k, v in USAGE.findall(data):
        u[k.decode()] = max(u.get(k.decode(), 0), int(v))
    pin, pout, pcache = config.price(model_id or "")
    inp = u.get("input_tokens", u.get("prompt_tokens", 0))
    cached = u.get("cache_read_input_tokens", 0) + u.get("cached_tokens", 0)
    if "cached_tokens" in u:   # OpenAI : les jetons en cache sont inclus dans input_tokens
        inp -= u["cached_tokens"]
    return (inp * pin + u.get("output_tokens", u.get("completion_tokens", 0)) * pout + cached * pcache
            + u.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1e6


# Paramètres propres à l'API d'Anthropic que Claude Code ajoute de lui-même à chaque requête (niveau d'effort, réflexion
# « adaptive », gestion du contexte). Un serveur compatible (vLLM, llama.cpp) les ignore, ou les refuse : vLLM répond
# « Unexpected reasoning effort high ». Ils sont retirés avant de relayer vers un serveur compatible ; le modèle garde
# son propre comportement de réflexion.
ANTHROPIC_ONLY = ("thinking", "context_management")


def for_compatible(body, effort=""):
    """Corps d'une requête de l'agent, adapté à un serveur compatible. Le niveau d'effort de Claude Code est remplacé par
    celui réglé sur le provider (« effort »), ou retiré s'il n'y en a pas. Renvoie le corps d'origine s'il n'y a rien à
    changer ou s'il n'est pas du JSON."""
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(d, dict):
        return body
    changed = False
    for k in ANTHROPIC_ONLY:
        if k in d:
            d.pop(k); changed = True
    oc = d.get("output_config")
    if effort:
        oc = oc if isinstance(oc, dict) else {}
        if oc.get("effort") != effort:
            oc["effort"] = effort; d["output_config"] = oc; changed = True
    elif isinstance(oc, dict) and "effort" in oc:
        oc.pop("effort"); changed = True
        if not oc:
            d.pop("output_config")
    return json.dumps(d, ensure_ascii=False).encode() if changed else body


def uses_gateway(model):
    return upstream(model)[0] is not None


def pick(ctx, model_id):
    """Modèle du job visé par une requête : celui de l'agent, ou celui des relecteurs (profil) quand la requête le
    nomme. Un nom de modèle Claude non déclaré (petit modèle interne de Claude Code) va au modèle Anthropic du job.
    Jamais un autre modèle que ceux du job : le nom reçu ne choisit qu'entre eux."""
    models = [ctx.model] + [m for m in [getattr(ctx, "review_model", None)] if m and m is not ctx.model]
    exact = next((m for m in models if m.get("id") == model_id), None)
    if exact:
        return exact
    if str(model_id or "").startswith("claude"):
        return next((m for m in models if m.get("provider") == "anthropic"), ctx.model)
    return ctx.model


SLOW_SECONDS = 120          # au-delà, la réponse du modèle est signalée dans le journal du job
SLOW_BYTES = 1024 * 1024    # ou au-delà de cette taille : le modèle tourne sans doute en boucle jusqu'à sa limite de sortie


def slow_message(seconds, size, model_id):
    """Ligne du journal pour une réponse anormalement longue, ou None si elle est ordinaire."""
    if seconds < SLOW_SECONDS and size < SLOW_BYTES:
        return None
    mins, secs = divmod(int(seconds), 60)
    dur = f"{mins} min {secs:02d} s" if mins else f"{secs} s"
    big = f"{size / 1048576:.1f} Mo".replace(".", ",") if size >= 1048576 else f"{size // 1024} Ko"
    return (f"réponse du modèle {model_id} anormalement longue : {dur}, {big}. Souvent une boucle (réflexion ou texte répété) "
            "jusqu'à la limite de sortie ; le job a continué.")


class Gateway:
    def __init__(self, lookup, port, host="127.0.0.1", transport=None, on_slow=None):
        self.lookup, self.port, self.host = lookup, port, host
        self.on_slow = on_slow   # appelé avec (ctx, message) quand une réponse est anormalement longue
        self.client = httpx.Client(timeout=httpx.Timeout(30, read=config.JOB_TIMEOUT), transport=transport)
        self.lock = threading.Lock()   # budget : test, réservation et comptage d'un job se font d'un seul tenant

    def serve(self):
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _relay(self):
                tok = self.headers.get("x-api-key") or (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                ctx = gw.lookup(tok) if tok else None
                if not ctx or not self.path.startswith("/m/"):
                    return self._plain(403, "jeton de job inconnu")
                path, _, query = self.path[2:].partition("?")
                if self.command != "POST" or path not in PATHS:
                    return self._plain(403, "point d'entrée non autorisé")
                # Corps lu et borné AVANT toute réservation : une requête mal formée ou jamais envoyée ne retient rien.
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._plain(400, "Content-Length invalide")
                if n < 0 or n > MAX_BODY:
                    return self._plain(413, "requête trop grande")
                body = self.rfile.read(n) if n else None
                try:
                    asked = json.loads(body or b"{}").get("model")
                except (ValueError, AttributeError):
                    asked = None
                asked = asked if isinstance(asked, str) else None
                m = pick(ctx, asked)
                root, auth = upstream(m)
                if not root:
                    return self._plain(403, "aucune clé pour le fournisseur de ce job")
                paid = (m.get("type") or ("anthropic" if m.get("provider") == "anthropic" else "compatible")) in ("anthropic", "openai")
                # Budget du job appliqué ici : la passerelle voit TOUS les appels payants faits avec le jeton du job, à tout
                # moment (passage de l'agent, checks, vérificateurs). Dépense = coût déjà compté + dépense mesurée depuis
                # + appels en cours (réservés), pour que des appels simultanés ne franchissent pas ensemble la limite.
                if path.endswith("/count_tokens"):
                    paid = False   # comptage de jetons : gratuit chez le fournisseur, ni facturé ni réservé ici
                with gw.lock:
                    spent = (getattr(ctx, "base_cost", 0.0) + getattr(ctx, "gw_spent", 0.0) - getattr(ctx, "gw_mark", 0.0)
                             + getattr(ctx, "gw_reserved", 0.0))
                    if paid and getattr(ctx, "budget", 0) > 0 and spent >= ctx.budget:
                        return self._plain(402, "budget du job atteint")
                    hold = max(RESERVE, getattr(ctx, "gw_last", 0.0)) if paid else 0.0
                    ctx.gw_reserved = getattr(ctx, "gw_reserved", 0.0) + hold
                seen, tail, total, counted, status = bytearray(), bytearray(), 0, False, 0

                def count():   # serveurs compatibles (locaux) : coût 0, comme dans le flux de l'agent
                    nonlocal counted
                    if counted:
                        return
                    counted = True
                    with gw.lock:
                        ctx.gw_reserved = max(0.0, getattr(ctx, "gw_reserved", 0.0) - hold)
                        if paid and 200 <= status < 300:
                            cost = cost_of(model_id, bytes(seen) + bytes(tail)) or estimate(model_id, len(body or b""), total)
                            ctx.gw_spent = getattr(ctx, "gw_spent", 0.0) + cost
                            ctx.gw_last = cost
                model_id = asked or m.get("id")
                if not paid and body and path.startswith("/v1/messages"):
                    body = for_compatible(body, m.get("effort") or "")
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                headers.update(auth)
                started = time.time()
                try:
                    with gw.client.stream("POST", root + path, params=query or None, headers=headers, content=body) as r:
                        status = r.status_code
                        self.send_response(r.status_code)
                        for k, v in r.headers.items():
                            if k.lower() not in HOP and k.lower() != "content-encoding":
                                self.send_header(k, v)
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        try:
                            for chunk in r.iter_bytes():
                                if chunk:
                                    total += len(chunk)
                                    if len(seen) < MAX_SCAN:
                                        seen += chunk
                                    else:   # au-delà : on garde la fin, où un long flux place ses compteurs définitifs
                                        tail += chunk
                                        if len(tail) > 2 * TAIL:
                                            del tail[:-TAIL]
                                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                                    self.wfile.flush()
                            count()   # avant la fin de la réponse : l'appel suivant du job voit déjà cette dépense
                            msg = slow_message(time.time() - started, total, model_id) if gw.on_slow else None
                            if msg:
                                try:
                                    gw.on_slow(ctx, msg)
                                except Exception as e:
                                    log.warning("signalement d'une réponse longue impossible (%s)", type(e).__name__)
                            self.wfile.write(b"0\r\n\r\n")
                        finally:
                            count()   # réponse interrompue : ce qui a été reçu est compté quand même
                except httpx.HTTPError as e:
                    log.warning("%s : fournisseur injoignable (%s)", ctx.ref, type(e).__name__)
                    self._plain(502, "fournisseur injoignable")
                finally:
                    count()

            def _plain(self, code, msg):
                data = msg.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = _relay   # tout sauf POST est refusé (403) par _relay

            def log_message(self, *a):   # jamais d'en-têtes ni de corps dans les journaux
                pass

        srv = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=srv.serve_forever, name="passerelle", daemon=True).start()
        self.server = srv
        return srv
