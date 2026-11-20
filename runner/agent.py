"""Lancement de l'agent (Claude Code en mode non interactif) et lecture de son flux d'événements.
L'agent tourne par l'exécuteur (atelier local sous AGENT_UID, ou conteneur jetable du job) avec un environnement minimal :
ni jeton Git, ni jeton du panneau. Pour un modèle local (llama.cpp / vLLM avec l'API Messages), Claude Code est dirigé
vers ce serveur et le coût vaut 0."""
import json, logging, os, re, threading, time
from urllib.parse import urlsplit
from . import config
from .executor import as_agent  # noqa: F401  (réexporté pour les anciens appels)

log = logging.getLogger("runner.agent")
HERE = os.path.dirname(__file__)
PLAYBOOK = open(os.path.join(HERE, "playbook.md"), encoding="utf-8").read()
ONBOARDING = open(os.path.join(HERE, "onboarding.md"), encoding="utf-8").read()
STUDY = open(os.path.join(HERE, "etude.md"), encoding="utf-8").read()
TOOLS = "Read Edit Write MultiEdit Glob Grep Bash TodoWrite Task"
NEVER = "WebFetch WebSearch AskUserQuestion ExitPlanMode EnterPlanMode"   # pas de web, pas de question dans la conversation


def base_env(home, extra=None):
    name, email = (config.GIT_AUTHOR.split("<", 1) + [""])[:2]
    # Chemins des outils de chaque image de job (Go, JDK) et des installations de l'utilisateur, en plus du système.
    path = f"{home}/go/bin:{home}/.local/bin:/usr/local/go/bin:/go/bin:/opt/java/openjdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env = {"PATH": path, "HOME": home, "LANG": "C.UTF-8", "TZ": "Europe/Paris",
           "GIT_AUTHOR_NAME": name.strip(), "GIT_AUTHOR_EMAIL": email.strip(" >"), "GIT_COMMITTER_NAME": name.strip(),
           "GIT_COMMITTER_EMAIL": email.strip(" >"), "GIT_TERMINAL_PROMPT": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if config.JOB_PROXY:   # conteneur du job : toute sortie passe par le proxy en liste blanche, sauf le relais fab
        relay = urlsplit(config.RELAY_PUBLIC_URL).hostname or ""
        env.update({"HTTP_PROXY": config.JOB_PROXY, "HTTPS_PROXY": config.JOB_PROXY, "http_proxy": config.JOB_PROXY,
                    "https_proxy": config.JOB_PROXY, "NO_PROXY": relay, "no_proxy": relay})
        u = urlsplit(config.JOB_PROXY)   # Maven et Gradle ignorent HTTP(S)_PROXY : options Java équivalentes
        if u.hostname:
            jp = " ".join(f"-D{s}.proxyHost={u.hostname} -D{s}.proxyPort={u.port or 3128}" for s in ("http", "https"))
            env.update({"MAVEN_OPTS": jp, "GRADLE_OPTS": jp})
    env.update(extra or {})
    return env


def model_env(model, gateway=None, token=None):
    """Variables qui choisissent le modèle. L'agent passe par la passerelle de clés du runner avec le jeton de son job :
    la vraie clé n'entre jamais dans le conteneur, et aucun autre identifiant du runner non plus."""
    from .gateway import uses_gateway
    local = model.get("provider") != "anthropic"
    if gateway and token and uses_gateway(model):
        env = {"ANTHROPIC_BASE_URL": gateway.rstrip("/") + "/m", "ANTHROPIC_API_KEY": token, "ANTHROPIC_MODEL": model["id"]}
        if local:
            env.update({"ANTHROPIC_SMALL_FAST_MODEL": model["id"], "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "API_TIMEOUT_MS": "600000"})
        return env, local
    if not local:
        # Pas de clé relayable par la passerelle : aucun identifiant du runner n'entre dans le job (ni clé, ni jeton
        # d'abonnement). Le runner refuse ce cas avant de lancer l'agent (Runner.prepare).
        return {"ANTHROPIC_MODEL": model["id"]}, False
    base = (model.get("base_url") or "").rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    return {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": "local", "ANTHROPIC_MODEL": model["id"],
            "ANTHROPIC_SMALL_FAST_MODEL": model["id"], "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "600000"}, True


class Run:
    """Un passage de l'agent. Remonte les outils utilisés et la consommation ; coupe si budget ou durée dépassés."""

    def __init__(self, ctx, panel, prompt, resume=None, system=None):
        self.ctx, self.panel, self.prompt, self.resume = ctx, panel, prompt, resume
        self.system = system or PLAYBOOK
        self.session_id, self.cost, self.tokens, self.error, self.result = resume, 0.0, 0, None, ""
        self.subagents = []
        self.stalled = self.cancelled = False
        self.proc = None
        self.last = time.time()

    def command(self):
        cmd = [config.CLAUDE_BIN, "-p", self.prompt, "--output-format", "stream-json", "--verbose",
                            "--max-turns", str(config.MAX_TURNS), "--permission-mode", "acceptEdits",
                            "--allowedTools", TOOLS, "--disallowedTools", NEVER,
                            "--append-system-prompt", self.system, "--model", self.ctx.model["id"]]
        if self.resume:
            cmd += ["--resume", self.resume]
        return cmd

    def extra_env(self):
        return {}

    def _price(self, u):
        if self.local:
            return 0.0
        pin, pout, pcache = config.price(self.ctx.model["id"])
        return (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout
                + u.get("cache_read_input_tokens", 0) * pcache + u.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1e6

    def _usage(self, final=False):
        self.panel.event(self.ctx.ref, type="usage", cost=round(self.ctx.base_cost + self.cost, 4), tokens=self.ctx.base_tokens + self.tokens,
                         session_id=self.session_id or "",
                         agents=[{"name": "session principale", "task": self.ctx.title, "model": self.ctx.model["id"],
                                  "tokens": self.ctx.base_tokens + self.tokens, "cost": round(self.ctx.base_cost + self.cost, 4), "running": not final}]
                         + [{"name": s["name"], "task": s["task"], "model": self.ctx.model["id"], "tokens": 0, "cost": 0, "running": not final}
                            for s in self.subagents])

    def _tool_line(self, name, inp):
        if name == "Bash":
            return (inp.get("command") or "")[:160]
        if name in ("Read", "Edit", "Write", "MultiEdit"):
            f = str(inp.get("file_path") or "")
            return os.path.relpath(f, self.ctx.repo_dir) if os.path.isabs(f) else f
        if name in ("Glob", "Grep"):
            return str(inp.get("pattern", ""))[:120]
        if name == "Task":
            self.subagents.append({"name": str(inp.get("subagent_type") or "sous-agent")[:40], "task": str(inp.get("description", ""))[:120]})
            return str(inp.get("description", ""))[:120]
        return ""

    def _infer_stage(self, tool):
        """Si le modèle n'annonce pas ses étapes (fab etape), le runner les déduit de son activité, en le disant."""
        st = self.ctx.stages
        # Seulement à la première modification de fichier : avant, le modèle a encore le temps d'annoncer lui-même ses étapes.
        if tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit") or "implementing" in st:
            return
        for s in [x for x in ("triage", "investigating") if x not in st] + ["implementing"]:
            st.add(s)
            self.panel.event(self.ctx.ref, type="stage", stage=s, summary="Étape déduite par le runner de l'activité de l'agent (non annoncée).")

    def _line(self, line):
        self.last = time.time()
        try:
            m = json.loads(line)
        except ValueError:
            return
        t = m.get("type")
        if t == "system" and m.get("session_id"):
            self.session_id = m["session_id"]
        elif t == "assistant":
            msg = m.get("message") or {}
            u = msg.get("usage") or {}
            self.tokens += (u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_read_input_tokens", 0)
                            + u.get("cache_creation_input_tokens", 0))
            self.cost += self._price(u)
            for blk in msg.get("content") or []:
                if blk.get("type") == "tool_use":
                    self._infer_stage(blk.get("name", ""))
                    self.panel.event(self.ctx.ref, type="log", kind=blk.get("name", "outil").lower(),
                                     message=self._tool_line(blk.get("name"), blk.get("input") or {}))
            if self.ctx.base_cost + self.cost > self.ctx.budget > 0:
                self._kill(f"budget dépassé ({self.ctx.budget:.2f} $)")
        elif t == "result":
            self.session_id = m.get("session_id") or self.session_id
            self.result = str(m.get("result") or "")[:4000]
            if not self.local and m.get("total_cost_usd") is not None:
                self.cost = float(m["total_cost_usd"])
            if m.get("is_error") and not self.error:
                # Le texte du résultat porte la vraie cause (« API Error: 500 … ») : c'est lui qu'on remonte.
                why = " ".join(str(m.get("result") or "").split())[:400] or str(m.get("subtype") or "inconnue")
                self.error = f"l'agent s'est arrêté sur une erreur : {why}"
        if time.time() - self._last_usage > 5:
            self._last_usage = time.time()
            self._usage()

    def run(self):
        ex = self.ctx.ex
        menv, self.local = model_env(self.ctx.model, ex.gateway_url(), self.ctx.fab_token)
        env = base_env(self.ctx.home, {"FAB_RELAY": ex.relay_url(), "FAB_TOKEN": self.ctx.fab_token,
                                       # Volume réseau (NFS) qui présente les fichiers sous un autre propriétaire que
                                       # l'utilisateur du job : git refuserait le dépôt (« dubious ownership »). Le Pod du job
                                       # n'a qu'un utilisateur, le dépôt est le sien : on le déclare sûr.
                                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
                                       "FAB_WORKSPACE": self.ctx.repo_dir, "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                                       "DISABLE_ERROR_REPORTING": "1", **menv, **self.extra_env()})
        self.ctx.run = self
        self._last_usage, self.done = 0.0, False
        killer = threading.Timer(config.JOB_TIMEOUT, self._kill, args=(f"durée maximale dépassée ({config.JOB_TIMEOUT // 60} min)",))
        killer.start()
        idle_max = config.WATCHDOG_SECONDS or max(60, int(self.ctx.watchdog_min) * 60)

        def watchdog():   # chien de garde : aucune ligne du flux depuis idle_max -> arrêt, le runner relance une fois
            while not self.done:
                time.sleep(1 if config.WATCHDOG_SECONDS else 5)
                if not self.done and time.time() - self.last > idle_max:
                    self.stalled = True
                    self._kill(f"aucune activité depuis {max(1, idle_max // 60)} min")
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            code, err = ex.stream(self.ctx, self.command(), env, self.ctx.repo_dir, self._line)
        finally:
            self.done = True
            killer.cancel()
        if code not in (0, None) and not self.error:
            self.error = f"l'agent a quitté avec le code {code} : {err.strip()}"
        self._usage(final=True)
        return self

    def cancel(self, who):
        """Arrêt demandé depuis le panneau."""
        self.cancelled = True
        self._kill(f"arrêté par {who}")

    def _kill(self, why):
        if not self.error:
            self.error = why
        self.ctx.ex.kill(self.ctx)


WRITES = re.compile(r"apply_patch|sed\s+-i|\bcat\s*>|\btee\b|(?<![<>2&])>\s*[\w./-]+\.\w+|\bmv\s|\bcp\s|\btouch\s|\bmkdir\s")


class CodexRun(Run):
    """Même passage d'agent, avec Codex CLI (codex exec, flux JSON). Le modèle est joint par la passerelle de clés du
    runner (fournisseur « fabrique », API Responses) avec le jeton du job ; la vraie clé reste dans le runner. Codex n'a pas
    de hook équivalent à garde.py : restent le conteneur jetable, le proxy de sortie et les barrières du runner."""

    def provider(self):
        from .gateway import uses_gateway
        m, ex = self.ctx.model, self.ctx.ex
        if uses_gateway(m):
            return ex.gateway_url().rstrip("/") + "/m/v1", self.ctx.fab_token
        base = (m.get("base_url") or "").rstrip("/")
        return (base if base.endswith("/v1") else base + "/v1"), "local"

    def command(self):
        base, _ = self.provider()
        # Le conteneur jetable du job EST le bac à sable (pas de secret, réseau filtré) : l'option prévue pour ce cas.
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-m", self.ctx.model["id"],
                 "-c", 'model_provider="fabrique"',
                 "-c", f'model_providers.fabrique={{name="Fabrique",base_url="{base}",env_key="FAB_MODEL_KEY",wire_api="responses"}}',
                 "-c", "analytics.enabled=false", "-c", "check_for_update_on_startup=false"]
        # Pas d'instructions système sûres par -c : les consignes de la Fabrique ouvrent le message, comme une donnée de cadrage.
        prompt = self.prompt if self.resume else f"<consignes_fabrique>\n{self.system}\n</consignes_fabrique>\n\n{self.prompt}"
        # « codex exec [OPTIONS] resume <ID> [PROMPT] » : les options d'exec avant la sous-commande
        tail = (["resume", self.resume] if self.resume else []) + [prompt]
        return ["sh", "-c", 'mkdir -p "$CODEX_HOME" && exec "$@"', "codex", config.CODEX_BIN, "exec"] + flags + tail

    def extra_env(self):
        _, key = self.provider()
        return {"CODEX_HOME": os.path.join(self.ctx.home, ".codex"), "FAB_MODEL_KEY": key, "RUST_LOG": "error"}

    def _price(self, u):
        if self.local:
            return 0.0
        pin, pout, pcache = config.price(self.ctx.model["id"])
        cached = u.get("cached_input_tokens", 0)
        return ((u.get("input_tokens", 0) - cached) * pin + cached * pcache + u.get("output_tokens", 0) * pout) / 1e6

    def _line(self, line):
        self.last = time.time()
        try:
            m = json.loads(line)
        except ValueError:
            return
        t = m.get("type")
        if t == "thread.started":
            self.session_id = m.get("thread_id") or self.session_id
        elif t in ("item.started", "item.completed"):
            it = m.get("item") or {}
            kind = it.get("type") or it.get("item_type") or ""
            if kind == "command_execution" and t == "item.started":
                cmd = str(it.get("command", ""))
                if WRITES.search(cmd):   # Codex modifie souvent les fichiers par le shell (sed -i, apply_patch, redirections)
                    self._infer_stage("Edit")
                self.panel.event(self.ctx.ref, type="log", kind="bash", message=cmd[:160])
            elif kind == "file_change" and t == "item.completed":
                self._infer_stage("Edit")
                files = ", ".join(str(c.get("path", "")) for c in (it.get("changes") or [])[:4])
                self.panel.event(self.ctx.ref, type="log", kind="edit", message=files[:160])
            elif kind == "agent_message" and t == "item.completed":
                self.result = str(it.get("text") or "")[:4000]
            elif kind == "error":
                self.panel.event(self.ctx.ref, type="log", kind="codex", message=str(it.get("message", ""))[:300])
        elif t == "turn.completed":
            u = m.get("usage") or {}
            self.tokens += u.get("input_tokens", 0) + u.get("output_tokens", 0)
            self.cost += self._price(u)
            if self.ctx.base_cost + self.cost > self.ctx.budget > 0:
                self._kill(f"budget dépassé ({self.ctx.budget:.2f} $)")
        elif t in ("turn.failed", "error"):
            msg = (m.get("error") or {}).get("message") if isinstance(m.get("error"), dict) else m.get("message")
            if not self.error:
                self.error = f"Codex s'est arrêté sur une erreur : {str(msg or t)[:300]}"
        if time.time() - self._last_usage > 5:
            self._last_usage = time.time()
            self._usage()


def runner_for(ctx):
    return CodexRun if (ctx.job or {}).get("harness") == "codex" else Run
