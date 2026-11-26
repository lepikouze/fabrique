"""0.8.5: a local model can no longer think forever (thinking budget), loops and full contexts stop the run with their
cause, the watchdog no longer restarts an agent whose model is still answering, Claude Code knows the model's window."""
import json, socket, time
import httpx
from runner import agent, config, gateway, messages


class Ctx:
    ref = "FAB-1"

    def __init__(self, model):
        self.model = model


LOCAL = {"id": "gemma", "provider": "openai", "type": "compatible", "base_url": "http://llm:8080/v1", "key": ""}
BODY = {"model": "gemma", "max_tokens": 32000, "stream": True, "messages": [{"role": "user", "content": "x"}],
        "thinking": {"type": "adaptive"}}


def start(lookup, transport, **kw):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    gw = gateway.Gateway(lookup, port, "127.0.0.1", transport=transport, **kw)
    gw.serve()
    return f"http://127.0.0.1:{port}", gw


def test_thinking_budget_stays_below_the_response_limit():
    assert gateway.thinking_budget_for(32000, 8000) == 8000
    assert gateway.thinking_budget_for(6000, 8000) == 6000 - gateway.MIN_THINKING   # room left to answer
    assert gateway.thinking_budget_for(1500, 8000) == 0                              # short internal request: none
    assert gateway.thinking_budget_for(None, 8000) == 0 and gateway.thinking_budget_for(32000, 0) == 0
    out = json.loads(gateway.for_compatible(json.dumps(BODY).encode(), "", 8000))
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert "thinking" not in json.loads(gateway.for_compatible(json.dumps(BODY).encode(), ""))   # without budget: as before


def test_budget_disabled_relays_without_thinking(monkeypatch):
    monkeypatch.setattr(config, "THINKING_BUDGET", 0)
    seen = []
    url, gw = start({"t": Ctx(LOCAL)}.get, httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={})))
    httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY)
    assert "thinking" not in seen[-1]
    gw.server.shutdown()


def test_a_server_refusing_the_budget_is_asked_again_without_it_and_remembered():
    seen = []

    def upstream(req):
        d = json.loads(req.content); seen.append(d)
        return httpx.Response(400, json={"error": "unknown field thinking"}) if "thinking" in d else httpx.Response(200, json={"ok": 1})
    url, gw = start({"t": Ctx(LOCAL)}.get, httpx.MockTransport(upstream))
    r = httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY)
    assert r.status_code == 200 and r.json() == {"ok": 1}
    assert "thinking" in seen[0] and "thinking" not in seen[1]
    httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY)
    assert len(seen) == 3 and "thinking" not in seen[2]          # remembered: no second refusal
    gw.server.shutdown()


def test_responses_cut_at_the_limit_in_a_row_stop_the_run():
    cut = b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"max_tokens"}}\n\n'
    done = b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason": "end_turn"}}\n\n'
    replies = [cut, cut, done, cut, cut, cut]
    stops = []
    url, gw = start({"t": Ctx(LOCAL)}.get,
                    httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=replies.pop(0))),
                    on_runaway=lambda ctx, n: stops.append(n))
    for _ in range(6):
        httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY)
    assert stops == [3]                                           # the normal response in the middle reset the count
    assert gateway.cut_at_limit(b'{"finish_reason": "length"}') and not gateway.cut_at_limit(done)
    gw.server.shutdown()


def test_the_runner_stops_only_a_run_in_progress():
    from runner.main import Runner
    killed = []

    class R:
        done = False

        def _kill(self, why, limit=None):
            killed.append(str(why))
            assert limit == "loops"     # 0.8.10: a looping model interrupts the job, its work is kept
    ctx = Ctx(LOCAL); ctx.run = R()
    Runner.runaway(None, ctx, 3)
    assert killed and "3 times in a row" in killed[0]
    ctx.run.done = True
    Runner.runaway(None, ctx, 4)
    assert len(killed) == 1


def test_a_request_in_progress_counts_as_activity():
    ctx = Ctx(LOCAL)
    during = {}

    def upstream(req):
        during["busy"] = ctx.gw_busy
        return httpx.Response(200, json={})
    url, gw = start({"t": ctx}.get, httpx.MockTransport(upstream))
    before = time.time()
    httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY)
    assert during["busy"] == 1 and ctx.gw_busy == 0 and ctx.gw_seen >= before
    gw.server.shutdown()


def test_watchdog_waits_while_the_model_answers(monkeypatch):
    monkeypatch.setattr(config, "WATCHDOG_SECONDS", 1)

    class Ex:
        killed = 0

        def gateway_url(self):
            return "http://gw"

        def relay_url(self):
            return "http://relay"

        def kill(self, ctx):
            Ex.killed += 1

        def stream(self, ctx, cmd, env, cwd, on_line):
            ctx.gw_busy = 1
            for _ in range(5):                     # 2.5 s without a line from the agent, but the model streams
                ctx.gw_seen = time.time(); time.sleep(0.5)
            ctx.gw_busy = 0
            return 0, ""
    ctx = Ctx(LOCAL)
    ctx.ex, ctx.home, ctx.fab_token, ctx.repo_dir, ctx.watchdog_min, ctx.base_cost, ctx.budget = Ex(), "/tmp", "t", "/tmp", 1, 0.0, 0
    monkeypatch.setattr(agent.Run, "context_env", lambda self: {})
    run = agent.Run(ctx, type("P", (), {"event": lambda *a, **k: None, "usage": lambda *a, **k: None})(), "go")
    run._usage = lambda final=False: None
    run.run()
    assert not run.stalled and Ex.killed == 0


def test_context_window_read_from_llama_cpp_and_given_to_claude_code(monkeypatch):
    gateway._ctx_cache.clear()
    asked = []

    def props(req):
        asked.append(req.url.path)
        if req.url.path == "/upstream/gemma/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        return httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(props))
    assert gateway.context_size(LOCAL, client) == 131072
    assert gateway.context_size(LOCAL, client) == 131072 and len(asked) == 1           # cached
    assert gateway.context_size({**LOCAL, "key": "sk"}, client) is None                # hosted provider: not asked
    assert gateway.context_size({"id": "claude", "provider": "anthropic", "type": "anthropic", "key": "k"}, client) is None
    monkeypatch.setattr(gateway, "context_size", lambda m: 131072)
    run = agent.Run(Ctx(LOCAL), None, "go"); run.local = True
    assert run.context_env() == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "131072"}
    run.local = False
    assert run.context_env() == {}


def test_output_limit_error_is_named():
    err = "the agent stopped on an error: API Error: Claude's response exceeded the 32000 output token maximum. To configure"
    assert messages.model_trouble(err) == "cut"
    assert messages.model_trouble("API Error: 500 Context size has been exceeded") == "busy"


def test_system_messages_inside_the_conversation_move_to_the_system_prompt():
    body = {"model": "qwen", "max_tokens": 32000, "system": [{"type": "text", "text": "A"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                         {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
                         {"role": "assistant", "content": "ok"}, {"role": "system", "content": "late"}]}
    out = json.loads(gateway.for_compatible(json.dumps(body).encode()))
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert [b["text"] for b in out["system"]] == ["A", "# Environment", "late"]
    out = json.loads(gateway.for_compatible(json.dumps({**body, "system": "S"}).encode()))
    assert [b["text"] for b in out["system"]] == ["S", "# Environment", "late"]
    plain = json.dumps({"model": "q", "messages": [{"role": "user", "content": "x"}]}).encode()
    assert gateway.for_compatible(plain) is plain                                   # nothing to move: untouched


def test_context_window_read_from_vllm():
    gateway._ctx_cache.clear()

    def vllm(req):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "other", "max_model_len": 4096},
                                                                       {"id": "gemma", "max_model_len": 65536}]})
        return httpx.Response(404)
    assert gateway.context_size(LOCAL, httpx.Client(transport=httpx.MockTransport(vllm))) == 65536


def test_vllm_validation_error_on_the_budget_falls_back_too():
    seen = []

    def upstream(req):
        d = json.loads(req.content); seen.append(d)
        return httpx.Response(422, json={"detail": "extra"}) if "thinking" in d else httpx.Response(200, json={"ok": 1})
    url, gw = start({"t": Ctx({**LOCAL, "base_url": "http://vllm:8000/v1"})}.get, httpx.MockTransport(upstream))
    assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "t"}, json=BODY).status_code == 200
    assert "thinking" not in seen[-1]
    gw.server.shutdown()


def test_a_provider_identifier_typed_with_capitals_still_leads_to_its_model_list(monkeypatch):
    from tests.test_web import web
    from app import config as app_config, providers
    monkeypatch.setattr(app_config, "SECRET_KEY", "une-cle-maitre-de-test")
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [{"id": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5"}]})))
    cl = web()
    r = cl.post("/settings/providers", data={"action": "save", "type": "anthropic", "pid": "Claude", "name": "Claude",
                                             "base_url": "", "key": "sk-ant-test"})
    assert r.headers["location"].endswith("load=claude")           # the stored identifier, not the typed one
    page = cl.get(r.headers["location"]).text
    assert "claude-sonnet-5-5" in page and "Unknown provider" not in page
