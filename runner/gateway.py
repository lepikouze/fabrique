"""Key gateway: the agent, in its container, talks to its model provider THROUGH the runner, using its
ephemeral job token. The runner replaces this token with the real API key (received from the dashboard) and
relays the request, including the streaming response. The key never leaves the runner: a compromised agent can
neither read it nor send it elsewhere.

Path: /m/<suite> -> <job provider root>/<suite> (Claude Code adds /v1/messages, Codex /v1/responses...)."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from . import config

log = logging.getLogger("runner.gateway")
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
       "content-length", "host", "authorization", "x-api-key", "accept-encoding"}


def upstream(model):
    """(provider root, authentication headers) for the job model; (None, None) if nothing to relay.
    A compatible local server WITHOUT a key (llama.cpp, vLLM) also goes through the gateway, without a header:
    jobs thus never need to reach the model machine (it is not in the proxy allowlist)."""
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


# Only the endpoints that agents need (Claude Code: Messages API and its token counting; Codex: Responses
# API; compatible servers: Chat Completions). EXACT path comparison, in POST only: no sub-resource
# (batches, files…), no "..", no other method. The upstream address is built from the constant, never from the received path.
PATHS = ("/v1/messages", "/v1/messages/count_tokens", "/v1/responses", "/v1/chat/completions")
RESERVE = 0.02   # dollars reserved per ongoing call, at least: simultaneous calls cannot all go through on the last cent
USAGE = re.compile(rb'"(input_tokens|output_tokens|cache_read_input_tokens|cache_creation_input_tokens|cached_tokens|prompt_tokens|completion_tokens)"\s*:\s*(\d+)')
MAX_SCAN = 4 * 1024 * 1024   # start of the response kept for counters; the end is too (TAIL), where a stream terminates them
TAIL = 1024 * 1024
MAX_BODY = 64 * 1024 * 1024   # body of an agent request: beyond this, it is refused (the runner does not put it in memory)


def estimate(model_id, sent, received, model=None):
    """Estimated cost of a paid call whose response carries no usage counter (usage-less stream, truncated response...):
    about four bytes per token. A call accepted by a paid provider never counts as zero."""
    pin, pout, _ = config.price(model_id or "", model)
    return (sent / 4 * pin + received / 4 * pout) / 1e6


def cost_of(model_id, data, model=None):
    """Cost of a provider response, based on the usage counters it contains (JSON or SSE stream; counters
    are cumulative, we keep the largest of each)."""
    u = {}
    for k, v in USAGE.findall(data):
        u[k.decode()] = max(u.get(k.decode(), 0), int(v))
    pin, pout, pcache = config.price(model_id or "", model)
    inp = u.get("input_tokens", u.get("prompt_tokens", 0))
    cached = u.get("cache_read_input_tokens", 0) + u.get("cached_tokens", 0)
    if "cached_tokens" in u:   # OpenAI: cached tokens are included in input_tokens
        inp -= u["cached_tokens"]
    return (inp * pin + u.get("output_tokens", u.get("completion_tokens", 0)) * pout + cached * pcache
            + u.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1e6


# Parameters specific to the Anthropic API that Claude Code adds itself to each request (effort level, reasoning
# "adaptive", context management). A compatible server (vLLM, llama.cpp) ignores them, or refuses them: vLLM responds
# "Unexpected reasoning effort high". They are removed before relaying to a compatible server; the model keeps
# its own reasoning behavior.
ANTHROPIC_ONLY = ("thinking", "context_management")


def for_compatible(body, effort="", thinking_budget=0):
    """Agent request body, adapted for a compatible server. The Claude Code effort level is replaced by
    the one set on the provider ("effort"), or removed if there is none. With a thinking budget, the Anthropic
    "thinking" setting is replaced by an explicit budget that llama.cpp applies (the thinking stops, the model answers):
    without it, a small model may think until its output limit and never answer. Returns the original body if there is
    nothing to change or if it is not JSON."""
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
    if hoist_system(d):
        changed = True
    budget = thinking_budget_for(d.get("max_tokens"), thinking_budget)
    if budget:
        d["thinking"] = {"type": "enabled", "budget_tokens": budget}; changed = True
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


def hoist_system(d):
    """Moves the "system" messages found inside the conversation to the system prompt, at the beginning. Claude Code
    places one in the middle (its environment block); many chat templates refuse it (Qwen: "System message must be at the
    beginning", HTTP 500 on the first request). True if something moved."""
    msgs = d.get("messages")
    if not isinstance(msgs, list) or not any(isinstance(m, dict) and m.get("role") == "system" for m in msgs):
        return False
    moved = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                moved.append({"type": "text", "text": c})
            elif isinstance(c, list):
                moved += [b for b in c if isinstance(b, dict) and b.get("type") == "text"]
    sysp = d.get("system")
    blocks = ([{"type": "text", "text": sysp}] if isinstance(sysp, str) and sysp else list(sysp) if isinstance(sysp, list) else [])
    d["system"] = blocks + moved
    d["messages"] = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
    return True


MIN_THINKING = 1024   # below, no budget is sent (short internal requests: titles, summaries)


def thinking_budget_for(max_tokens, budget):
    """Thinking budget to send, kept below the response limit so that the model has room to answer; 0: none."""
    if not budget or not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return 0
    b = min(int(budget), max_tokens - MIN_THINKING)
    return b if b >= MIN_THINKING else 0


CUT = re.compile(rb'"(stop_reason"\s*:\s*"max_tokens|finish_reason"\s*:\s*"length)"')


def cut_at_limit(data):
    """True if a model response (JSON or SSE stream) stopped at its output limit instead of finishing."""
    return bool(CUT.search(data))


_ctx_cache = {}


def context_size(model, client=None, ttl=600):
    """Context size of a local model server WITHOUT a key: llama.cpp (directly or behind llama-swap) publishes it in
    /props, vLLM in /v1/models (max_model_len). None if unknown (another server, unreachable). Claude Code does not know the window of a model it does not
    know: without it, it never summarizes the conversation and the server ends up refusing or cutting the responses."""
    root, auth = upstream(model)
    if not root or auth or model.get("provider") == "anthropic":
        return None
    key = (root, model.get("id"))
    hit = _ctx_cache.get(key)
    if hit and time.time() - hit[1] < ttl:
        return hit[0]
    n = None
    c = client or httpx.Client(timeout=5)
    try:
        for url in (f"{root}/upstream/{model.get('id')}/props", f"{root}/props"):
            try:
                r = c.get(url)
                if r.status_code != 200:
                    continue
                d = r.json()
                v = (d.get("default_generation_settings") or {}).get("n_ctx") or d.get("n_ctx")
                if isinstance(v, int) and v > 0:
                    n = v
                    break
            except (httpx.HTTPError, ValueError, AttributeError):
                continue
        if n is None:   # vLLM
            try:
                r = c.get(f"{root}/v1/models")
                if r.status_code == 200:
                    for m in r.json().get("data") or []:
                        v = m.get("max_model_len") if isinstance(m, dict) else None
                        if m.get("id") == model.get("id") and isinstance(v, int) and v > 0:
                            n = v
                            break
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
    finally:
        if client is None:
            c.close()
    _ctx_cache[key] = (n, time.time())
    return n


def uses_gateway(model):
    return upstream(model)[0] is not None


def pick(ctx, model_id):
    """Model of the job targeted by a request: that of the agent, or that of the reviewers (profile) when the request
    specifies it. An undeclared Claude model name (Claude Code small internal model) goes to the job's Anthropic model.
    Never a model other than those of the job: the received name only chooses between them."""
    models = [ctx.model] + [m for m in [getattr(ctx, "review_model", None), *(getattr(ctx, "review_models", None) or {}).values()]
                            if m and m is not ctx.model]
    exact = next((m for m in models if m.get("id") == model_id), None)
    if exact:
        return exact
    if str(model_id or "").startswith("claude"):
        return next((m for m in models if m.get("provider") == "anthropic"), ctx.model)
    return ctx.model


SLOW_SECONDS = 120          # beyond, the model response is reported in the job log
SLOW_BYTES = 1024 * 1024    # or beyond this size: the model is likely looping until its output limit


def slow_message(seconds, size, model_id):
    """Log line for an abnormally long response, or None if it is ordinary."""
    if seconds < SLOW_SECONDS and size < SLOW_BYTES:
        return None
    mins, secs = divmod(int(seconds), 60)
    dur = f"{mins} min {secs:02d} s" if mins else f"{secs} s"
    big = f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size // 1024} kB"
    return (M("response from model {model_id} abnormally long: {dur}, {big}. Often a loop (thinking or repeated text) up to the output limit; the job went on.", model_id=model_id, dur=dur, big=big))


class Gateway:
    def __init__(self, lookup, port, host="127.0.0.1", transport=None, on_slow=None, on_runaway=None):
        self.lookup, self.port, self.host = lookup, port, host
        self.on_slow = on_slow   # called with (ctx, message) when a response is abnormally long
        self.on_runaway = on_runaway   # called with (ctx, streak) when responses keep stopping at the output limit
        self.no_budget = set()   # compatible servers that refused the thinking budget: relayed without it
        self.client = httpx.Client(timeout=httpx.Timeout(30, read=config.JOB_TIMEOUT), transport=transport)
        self.lock = threading.Lock()   # budget: test, reservation and counting of a job are done in one go

    def runaway(self, ctx, cut):
        """Counts the agent responses cut at the output limit in a row; beyond config.RUNAWAY_STREAK, on_runaway stops
        the run. A response that finishes normally resets the count."""
        with self.lock:
            ctx.gw_cut = getattr(ctx, "gw_cut", 0) + 1 if cut else 0
            streak = ctx.gw_cut
        if cut and self.on_runaway and config.RUNAWAY_STREAK and streak >= config.RUNAWAY_STREAK:
            try:
                self.on_runaway(ctx, streak)
            except Exception as e:
                log.warning("cannot stop a looping run (%s)", type(e).__name__)

    def serve(self):
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _relay(self):
                tok = self.headers.get("x-api-key") or (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                ctx = gw.lookup(tok) if tok else None
                if not ctx or not self.path.startswith("/m/"):
                    return self._plain(403, "unknown job token")
                path, _, query = self.path[2:].partition("?")
                if self.command != "POST" or path not in PATHS:
                    return self._plain(403, M("endpoint not allowed"))
                # Body read and bounded BEFORE any reservation: a malformed or never sent request retains nothing.
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._plain(400, "invalid Content-Length")
                if n < 0 or n > MAX_BODY:
                    return self._plain(413, M("request too large"))
                body = self.rfile.read(n) if n else None
                try:
                    asked = json.loads(body or b"{}").get("model")
                except (ValueError, AttributeError):
                    asked = None
                asked = asked if isinstance(asked, str) else None
                m = pick(ctx, asked)
                root, auth = upstream(m)
                if not root:
                    return self._plain(403, M("no key for this job's provider"))
                paid = (m.get("type") or ("anthropic" if m.get("provider") == "anthropic" else "compatible")) in ("anthropic", "openai")
                # Job budget applied here: the gateway sees ALL paid calls made with the job token, at any
                # moment (agent run, checks, reviewers). Expense = cost already counted + expense measured since
                # + ongoing calls (reserved), so that simultaneous calls do not collectively exceed the limit.
                if path.endswith("/count_tokens"):
                    paid = False   # token counting: free at the provider, neither billed nor reserved here
                with gw.lock:
                    spent = (getattr(ctx, "base_cost", 0.0) + getattr(ctx, "gw_spent", 0.0) - getattr(ctx, "gw_mark", 0.0)
                             + getattr(ctx, "gw_reserved", 0.0))
                    if paid and getattr(ctx, "budget", 0) > 0 and spent >= ctx.budget:
                        return self._plain(402, M("job budget reached"))
                    hold = max(RESERVE, getattr(ctx, "gw_last", 0.0)) if paid else 0.0
                    ctx.gw_reserved = getattr(ctx, "gw_reserved", 0.0) + hold
                seen, tail, total, counted, status = bytearray(), bytearray(), 0, False, 0

                def count():   # compatible servers (local): cost 0, as in the agent flow
                    nonlocal counted
                    if counted:
                        return
                    counted = True
                    with gw.lock:
                        ctx.gw_reserved = max(0.0, getattr(ctx, "gw_reserved", 0.0) - hold)
                        if paid and 200 <= status < 300:
                            # The dashboard's price applies to the job model; another model asked by name (Claude Code's small internal
                            # model) is priced by its own name.
                            pm = m if model_id == m.get("id") else None
                            cost = cost_of(model_id, bytes(seen) + bytes(tail), pm) or estimate(model_id, len(body or b""), total, pm)
                            ctx.gw_spent = getattr(ctx, "gw_spent", 0.0) + cost
                            ctx.gw_last = cost
                model_id = asked or m.get("id")
                plain = body
                budgeted = False
                if not paid and body and path.startswith("/v1/messages"):
                    plain = for_compatible(body, m.get("effort") or "")
                    body = plain
                    # The provider's own budget (Settings → model providers), otherwise the runner's.
                    tb = m.get("thinking_budget") if m.get("thinking_budget") is not None else config.THINKING_BUDGET
                    if root not in gw.no_budget and tb:
                        body = for_compatible(plain, m.get("effort") or "", tb)
                        budgeted = body is not plain
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                headers.update(auth)
                started = time.time()
                with gw.lock:   # activity seen by the watchdog: a model still answering is not a silent agent
                    ctx.gw_busy = getattr(ctx, "gw_busy", 0) + 1
                    ctx.gw_seen = started
                try:
                    r = gw.client.send(gw.client.build_request("POST", root + path, params=query or None, headers=headers,
                                                               content=body), stream=True)
                    if budgeted and r.status_code in (400, 422):
                        # A compatible server that does not take the thinking budget (400, or 422 for vLLM's validation):
                        # asked again without it,
                        # and remembered for the following requests.
                        r.read(); r.close()
                        gw.no_budget.add(root)
                        log.info("%s: thinking budget refused by the model server, relayed without it", ctx.ref)
                        r = gw.client.send(gw.client.build_request("POST", root + path, params=query or None,
                                                                   headers=headers, content=plain), stream=True)
                    try:
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
                                    ctx.gw_seen = time.time()
                                    if len(seen) < MAX_SCAN:
                                        seen += chunk
                                    else:   # beyond: we keep the end, where a long stream places its final counters
                                        tail += chunk
                                        if len(tail) > 2 * TAIL:
                                            del tail[:-TAIL]
                                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                                    self.wfile.flush()
                            count()   # before the end of the response: the following job call already sees this expense
                            if 200 <= status < 300 and path.startswith("/v1/messages") and not path.endswith("/count_tokens"):
                                gw.runaway(ctx, cut_at_limit(bytes(seen[-TAIL:]) + bytes(tail)))
                            msg = slow_message(time.time() - started, total, model_id) if gw.on_slow else None
                            if msg:
                                try:
                                    gw.on_slow(ctx, msg)
                                except Exception as e:
                                    log.warning("cannot report a long response (%s)", type(e).__name__)
                            self.wfile.write(b"0\r\n\r\n")
                        finally:
                            count()   # interrupted response: what was received is still counted
                    finally:
                        r.close()
                except httpx.HTTPError as e:
                    log.warning("%s: provider unreachable (%s)", ctx.ref, type(e).__name__)
                    self._plain(502, "provider unreachable")
                finally:
                    count()
                    with gw.lock:
                        ctx.gw_busy = max(0, getattr(ctx, "gw_busy", 0) - 1)
                        ctx.gw_seen = time.time()

            def _plain(self, code, msg):
                data = msg.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = _relay   # everything except POST is refused (403) by _relay

            def log_message(self, *a):   # never headers or body in the logs
                pass

        srv = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=srv.serve_forever, name="gateway", daemon=True).start()
        self.server = srv
        return srv
