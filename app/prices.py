"""Model prices ($ per million tokens: input, output, cached input), sent to the runner with each model so that the cost of a
job is right. Order: a price entered by hand on the model (Settings → model providers) > the public list fetched every week >
the table below > unknown. A local server without a key costs 0.

The public list is the one LiteLLM maintains (several hundred models). Its address is FABRIQUE_PRICES_URL; empty: nothing is
fetched (closed network), the table below and the prices entered by hand remain."""
import logging, os, threading, time
import httpx
from . import db

log = logging.getLogger("fabrique.prices")
URL = os.environ.get("FABRIQUE_PRICES_URL",
                     "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json").strip()
EVERY = 7 * 24 * 3600
# Keep in step with runner/config.py (PRICES): the longest prefix wins.
BUILTIN = {"claude-fable-5-2": (8, 40, 0.2), "claude-mythos-5-2": (8, 40, 0.2), "claude-fable-5": (10, 50, 1.0), "claude-mythos-5": (10, 50, 1.0),
           "claude-opus-5-5": (4, 20, 0.2), "claude-opus-5": (5, 25, 0.5), "claude-opus-4": (5, 25, 0.5),
           "claude-sonnet-5-5": (2, 10, 0.1), "claude-sonnet-5": (2, 10, 0.2), "claude-sonnet-4": (3, 15, 0.3),
           "claude-haiku-5-5": (0.1, 0.5, 0.01), "claude-haiku-4": (1, 5, 0.1),
           "gpt-5-nano": (0.05, 0.4, 0.005), "gpt-5-mini": (0.25, 2, 0.025), "gpt-5": (1.25, 10, 0.125)}


def parse_manual(text):
    """'2 / 10 / 0.1' (input, output, cached input, $ per million tokens) -> [2.0, 10.0, 0.1]; empty -> None. The cached price
    may be left out: then a tenth of the input price."""
    t = str(text or "").replace(",", ".").replace("$", " ").strip()
    if not t:
        return None
    parts = [x for x in t.replace("/", " ").split() if x]
    try:
        vals = [float(x) for x in parts]
    except ValueError:
        raise ValueError("price")
    if len(vals) == 2:
        vals.append(vals[0] / 10)
    if len(vals) != 3 or any(v < 0 or v > 10000 for v in vals):
        raise ValueError("price")
    return [round(v, 6) for v in vals]


def fetched(c):
    """Information on the last fetch: {at, source, count} (the list itself is kept apart, read once per fetch)."""
    return db.get_setting(c, "prices")[0] or {}


_memo = {"at": None, "table": {}}


def table(c):
    at = fetched(c).get("at")
    if at != _memo["at"]:
        _memo["table"], _memo["at"] = (db.get_setting(c, "prices_table")[0] or {}) if at else {}, at
    return _memo["table"]


def from_list(table, model_id):
    """Price of a model in the public list (per token there), converted to $ per million; None if absent."""
    for key in (model_id, f"anthropic/{model_id}", f"openai/{model_id}"):
        e = table.get(key) if isinstance(table, dict) else None
        if isinstance(e, dict) and isinstance(e.get("input_cost_per_token"), (int, float)) and isinstance(e.get("output_cost_per_token"), (int, float)):
            cache = e.get("cache_read_input_token_cost")
            cache = cache if isinstance(cache, (int, float)) else e["input_cost_per_token"] / 10
            return [round(e["input_cost_per_token"] * 1e6, 6), round(e["output_cost_per_token"] * 1e6, 6), round(cache * 1e6, 6)]
    return None


def resolve(c, model_id, entry=None, local=False):
    """[input, output, cached input] in $ per million tokens, or None if unknown."""
    if local:
        return [0.0, 0.0, 0.0]
    manual = (entry or {}).get("price")
    if isinstance(manual, list) and len(manual) == 3:
        return manual
    got = from_list(table(c), model_id)
    if got:
        return got
    best = max((k for k in BUILTIN if (model_id or "").startswith(k)), key=len, default=None)
    return list(BUILTIN[best]) if best else None


def refresh(transport=None, now=None):
    """Fetches the public list and keeps only the prices (a few hundred lines). Never raises: a failure is logged and the
    previous list stays."""
    if not URL:
        return False
    try:
        with httpx.Client(timeout=30, transport=transport, follow_redirects=True) as h:
            r = h.get(URL)
            r.raise_for_status()
            raw = r.json()
    except Exception as e:
        log.warning("price list not fetched (%s)", type(e).__name__)
        return False
    table = {k: {f: v[f] for f in ("input_cost_per_token", "output_cost_per_token", "cache_read_input_token_cost") if isinstance(v.get(f), (int, float))}
             for k, v in (raw.items() if isinstance(raw, dict) else []) if isinstance(v, dict) and isinstance(v.get("input_cost_per_token"), (int, float))}
    with db.tx() as c:
        db.set_setting(c, "prices_table", table, "fabrique")
        db.set_setting(c, "prices", {"at": now or time.time(), "source": URL, "count": len(table)}, "fabrique")
    log.info("price list fetched: %d models", len(table))
    return True


def due(c, now=None):
    return bool(URL) and (now or time.time()) - float(fetched(c).get("at") or 0) >= EVERY


_stop = threading.Event()


def start():
    """Checks every six hours whether the weekly fetch is due."""
    def loop():
        while not _stop.is_set():
            try:
                with db.tx() as c:
                    go = due(c)
                if go:
                    refresh()
            except Exception as e:
                log.warning("price list check failed (%s)", type(e).__name__)
            _stop.wait(6 * 3600)
    _stop.clear()
    threading.Thread(target=loop, name="prices", daemon=True).start()


def stop():
    _stop.set()


def known(model_id):
    """True if the cost of this model is counted with a known price (for the job page)."""
    from . import providers
    with db.tx() as c:
        m = next((x for x in providers.models_with_provider(c) if x["id"] == model_id), None)
        if not m:
            return resolve(c, model_id) is not None
        p = next((x for x in providers.listing(c) if x["id"] == m.get("provider_id")), {})
        return resolve(c, model_id, m, local=(p.get("type") == "compatible" and not p.get("has_key"))) is not None
