"""Relais local entre la commande `fab` de l'agent et le panneau. Chaque job reçoit un jeton aléatoire ; un jeton ne
peut écrire que sur son job, et seulement les commandes prévues (étapes de l'agent, question, avis, note)."""
import json, logging, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("runner.relay")
STAGES = {"triage": "Tri", "investigating": "Enquête", "implementing": "Implémentation"}


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
            return 403, "jeton inconnu"
        cmd, ref = body.get("cmd"), ctx.ref
        if cmd == "stage" and body.get("stage") in STAGES:
            ctx.stages.add(body["stage"])
            self.panel.event(ref, type="stage", stage=body["stage"], summary=str(body.get("summary", ""))[:2000])
            return 200, f"Étape « {STAGES[body['stage']]} » enregistrée."
        if cmd == "question":
            q = str(body.get("question", "")).strip()
            if not q:
                return 400, "question vide"
            ctx.question = q[:4000]
            return 200, "Question enregistrée : termine maintenant ta réponse, le demandeur répondra et tu reprendras."
        if cmd == "decision":
            # Avis de l'agent : types et tailles bornés ici (mêmes limites que le panneau), avant tout usage par le runner.
            cap = {"verdict": 1000, "interpretation": 4000, "notes": 4000, "recommendation": 1000}
            ctx.decision = {"confident": body.get("confident") is True or str(body.get("confident")).lower() in ("true", "oui", "1")}
            ctx.decision.update({k: (str(body[k])[:n] if body.get(k) is not None else None) for k, n in cap.items()})
            # Les hypothèses restent une liste (une par ligne à l'écran) : jamais une chaîne, qui serait découpée en lettres.
            hyp = body.get("assumptions")
            hyp = [hyp] if isinstance(hyp, str) else hyp if isinstance(hyp, list) else []
            ctx.decision["assumptions"] = [str(h).strip()[:400] for h in hyp if str(h).strip()][:10]
            self.panel.event(ref, type="decision", **ctx.decision)
            return 200, "Avis enregistré. Le runner vérifie, pousse et ouvre la PR : tu peux terminer."
        if cmd == "failed":
            ctx.gave_up = str(body.get("reason", "")).strip()[:400] or "l'agent abandonne sans raison"
            return 200, "Abandon enregistré : termine maintenant."
        if cmd == "controle":   # hook de contrôle (controle.py) : une ligne dans l'activité, rien d'autre
            self.panel.event(ref, type="log", kind="contrôle", message=str(body.get("message", ""))[:1000])
            return 200, "Noté."
        if cmd == "note":
            self.panel.event(ref, type="log", kind="agent", message=str(body.get("message", ""))[:1000])
            return 200, "Note enregistrée."
        return 400, "commande inconnue"

    def serve(self):
        relay = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 64_000)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    code, msg = relay.handle(self.headers.get("X-Fab-Token", ""), body if isinstance(body, dict) else {})
                except Exception as e:
                    code, msg = 400, f"requête invalide ({type(e).__name__})"
                data = json.dumps({"message": msg}, ensure_ascii=False).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer((self.host, self.port), H)
        threading.Thread(target=srv.serve_forever, name="relay", daemon=True).start()
        return srv
