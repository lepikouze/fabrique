"""Local relay between the agent's `fab` command and the dashboard. Each job gets a random token; a token can only
write to its own job, and only the planned commands (agent steps, question, opinion, note). The replies go to the agent
(a model): they are in English whatever the job language."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("runner.relay")
STAGES = {"triage": "Triage", "investigating": "Investigation", "implementing": "Implementation"}


def record_decision(ctx, panel, ref, body):
    """Agent's opinion: types and sizes bounded here (same limits as the dashboard), before any use by the runner. Also
    used by the runner when it reads an opinion the agent wrote as text."""
    cap = {"verdict": 1000, "interpretation": 4000, "notes": 4000, "recommendation": 1000}
    ctx.decision = {"confident": body.get("confident") is True or str(body.get("confident")).lower() in ("true", "yes", "1")}
    ctx.decision.update({k: (str(body[k])[:n] if body.get(k) is not None else None) for k, n in cap.items()})
    # Assumptions remain a list (one per line on screen): never a string, which would be split into letters.
    hyp = body.get("assumptions")
    hyp = [hyp] if isinstance(hyp, str) else hyp if isinstance(hyp, list) else []
    ctx.decision["assumptions"] = [str(h).strip()[:400] for h in hyp if str(h).strip()][:10]
    panel.event(ref, type="decision", **ctx.decision)


class Relay:
    def __init__(self, panel, port, host="127.0.0.1"):
        self.panel, self.port, self.host, self.jobs, self.lock = panel, port, host, {}, threading.Lock()

    def register(self, ctx):
        tok = secrets.token_urlsafe(24)
        with self.lock:
            self.jobs[tok] = ctx
        return tok

    def ctx_for(self, tok):
        with self.lock:
            return self.jobs.get(tok)

    def forget(self, tok):
        with self.lock:
            self.jobs.pop(tok, None)

    def handle(self, tok, body):
        with self.lock:
            ctx = self.jobs.get(tok)
        if not ctx:
            return 403, "unknown token"
        cmd, ref = body.get("cmd"), ctx.ref
        if cmd == "stage" and body.get("stage") in STAGES:
            ctx.stages.add(body["stage"])
            self.panel.event(ref, type="stage", stage=body["stage"], summary=str(body.get("summary", ""))[:2000])
            return 200, f"Step \"{STAGES[body['stage']]}\" recorded."
        if cmd == "question":
            q = str(body.get("question", "")).strip()
            if not q:
                return 400, "empty question"
            ctx.question = q[:4000]
            return 200, "Question recorded: finish your answer now, the requester will answer and you will resume."
        if cmd == "decision":
            record_decision(ctx, self.panel, ref, body)
            return 200, "Opinion recorded. The runner verifies, pushes and opens the PR: you can finish."
        if cmd == "failed":
            ctx.gave_up = str(body.get("reason", "")).strip()[:400] or "the agent gives up without a reason"
            return 200, "Giving up recorded: finish now."
        if cmd == "check":   # security check hook (security_check.py): one line in the activity, nothing else
            self.panel.event(ref, type="log", kind=M("check"), message=str(body.get("message", ""))[:1000])
            return 200, "Noted."
        if cmd == "note":
            self.panel.event(ref, type="log", kind="agent", message=str(body.get("message", ""))[:1000])
            return 200, "Note recorded."
        return 400, "unknown command"

    def serve(self):
        relay = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 64_000)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    code, msg = relay.handle(self.headers.get("X-Fab-Token", ""), body if isinstance(body, dict) else {})
                except Exception as e:
                    code, msg = 400, f"invalid request ({type(e).__name__})"
                data = json.dumps({"message": msg}, ensure_ascii=False).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=srv.serve_forever, name="relay", daemon=True).start()
        return srv
