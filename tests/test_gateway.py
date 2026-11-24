"""Key gateway: the agent presents its job token, the provider receives the real key; nothing else passes through."""
import socket, threading
import httpx
from runner import agent, config, gateway


class Ctx:
    ref = "FAB-1"

    def __init__(self, model):
        self.model = model


def start(lookup, transport):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    gw = gateway.Gateway(lookup, port, "127.0.0.1", transport=transport)
    gw.serve()
    return f"http://127.0.0.1:{port}", gw


def test_gateway_swaps_job_token_for_real_key_and_streams():
    seen = {}

    def upstream(req):
        seen["url"], seen["key"], seen["auth"] = str(req.url), req.headers.get("x-api-key"), req.headers.get("authorization")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"event: a\ndata: 1\n\nevent: b\ndata: 2\n\n")
    jobs = {"tok-job": Ctx({"id": "claude-opus-5-5", "provider": "anthropic", "type": "anthropic", "key": "sk-ant-VRAIE"})}
    url, gw = start(jobs.get, httpx.MockTransport(upstream))
    r = httpx.post(url + "/m/v1/messages?beta=true", headers={"x-api-key": "tok-job", "anthropic-version": "2023-06-01"}, json={"x": 1})
    assert r.status_code == 200 and r.text == "event: a\ndata: 1\n\nevent: b\ndata: 2\n\n"
    assert seen["url"] == "https://api.anthropic.com/v1/messages?beta=true" and seen["key"] == "sk-ant-VRAIE" and not seen["auth"]
    gw.server.shutdown()


def test_gateway_refuses_unknown_token_and_relays_keyless_local_server():
    called = []
    jobs = {"tok-local": Ctx({"id": "gemma", "provider": "openai", "type": "compatible", "base_url": "http://m:8080/v1", "key": ""})}
    url, gw = start(jobs.get, httpx.MockTransport(lambda req: called.append(req) or httpx.Response(200)))
    assert httpx.post(url + "/m/v1/messages", headers={"x-api-key": "faux"}).status_code == 403
    assert not called
    # local server without key: also relayed (without authentication header), so that jobs do not have to join it
    assert httpx.post(url + "/m/v1/messages", headers={"authorization": "Bearer tok-local"}, json={}).status_code == 200
    assert str(called[0].url) == "http://m:8080/v1/messages" and "authorization" not in called[0].headers
    gw.server.shutdown()


def test_compatible_server_with_key_gets_bearer_on_its_root():
    seen = {}
    jobs = {"t": Ctx({"id": "llama3", "provider": "openai", "type": "compatible", "base_url": "http://vllm:8000/v1", "key": "k-vllm"})}
    url, gw = start(jobs.get, httpx.MockTransport(lambda req: seen.update(url=str(req.url), auth=req.headers.get("authorization")) or httpx.Response(200, json={})))
    httpx.post(url + "/m/v1/chat/completions", headers={"authorization": "Bearer t"}, json={})
    assert seen == {"url": "http://vllm:8000/v1/chat/completions", "auth": "Bearer k-vllm"}
    gw.server.shutdown()


def test_agent_env_never_contains_the_real_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    env, local = agent.model_env({"id": "claude-opus-5-5", "provider": "anthropic", "type": "anthropic", "key": "sk-ant-VRAIE"},
                                 "http://fabrique-runner:7072", "tok-job")
    assert env["ANTHROPIC_BASE_URL"] == "http://fabrique-runner:7072/m" and env["ANTHROPIC_API_KEY"] == "tok-job" and not local
    assert "sk-ant-VRAIE" not in str(env)
    env, local = agent.model_env({"id": "gemma", "provider": "openai", "base_url": "http://m:8080/v1", "key": ""}, "http://gw", "tok")
    assert env["ANTHROPIC_BASE_URL"] == "http://gw/m" and local                            # local without key: via the gateway too


# Actual body sent by Claude Code 2.1.322 (captured), reduced to what matters here.
CLAUDE_CODE_BODY = {"model": "qwen3-latest", "max_tokens": 32000, "stream": True, "messages": [{"role": "user", "content": "x"}],
                    "thinking": {"type": "adaptive"}, "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
                    "output_config": {"effort": "high"}}


def test_anthropic_only_parameters_are_removed_for_a_compatible_server_only():
    import json
    seen = []
    jobs = {"tok-local": Ctx({"id": "qwen3-latest", "provider": "openai", "type": "compatible", "base_url": "http://vllm:9090/v1", "key": ""}),
            "tok-claude": Ctx({"id": "claude-opus-5-5", "provider": "anthropic", "type": "anthropic", "key": "sk-ant-VRAIE"})}
    url, gw = start(jobs.get, httpx.MockTransport(lambda req: seen.append(json.loads(req.content)) or httpx.Response(200, json={})))
    httpx.post(url + "/m/v1/messages?beta=true", headers={"x-api-key": "tok-local"}, json=CLAUDE_CODE_BODY)
    local = seen[-1]
    assert "context_management" not in local and "output_config" not in local   # vLLM: "Unexpected reasoning effort"
    assert local["thinking"] == {"type": "enabled", "budget_tokens": config.THINKING_BUDGET}   # 0.8.5: explicit budget
    assert local["model"] == "qwen3-latest" and local["messages"] == CLAUDE_CODE_BODY["messages"] and local["stream"] is True
    httpx.post(url + "/m/v1/messages?beta=true", headers={"x-api-key": "tok-claude"}, json=CLAUDE_CODE_BODY)
    assert seen[-1] == CLAUDE_CODE_BODY                                                                    # Anthropic : intact
    gw.server.shutdown()


def test_for_compatible_keeps_other_output_settings_and_non_json_bodies():
    import json
    out = json.loads(gateway.for_compatible(json.dumps({"output_config": {"effort": "high", "format": {"type": "json"}}}).encode()))
    assert out == {"output_config": {"format": {"type": "json"}}}
    assert gateway.for_compatible(b"pas du json") == b"pas du json" and gateway.for_compatible(b"[1]") == b"[1]"
    same = json.dumps({"model": "m"}).encode()
    assert gateway.for_compatible(same) is same


def test_abnormally_long_model_answers_are_reported():
    """A very long or very large response (model loop up to its output limit) leaves a line in the job."""
    assert gateway.slow_message(14, 78_000, "m") is None
    msg = gateway.slow_message(427, 4_034_235, "gemma-4-26b-a4b")
    assert "gemma-4-26b-a4b" in msg and "7 min 07 s" in msg and "3.8 MB" in msg and "loop" in msg
    assert "45 s" in gateway.slow_message(45, 2_000_000, "m") and "500 kB" in gateway.slow_message(130, 512_000, "m")
