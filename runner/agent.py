"""Launching the agent (Claude Code in non-interactive mode) and reading its event stream.
The agent runs via the executor (local workshop under AGENT_UID, or disposable job container) with a minimal environment:
no Git token, no dashboard token. For a local model (llama.cpp / vLLM with Messages API), Claude Code is directed
to this server and the cost is 0."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, os, re, threading, time
from urllib.parse import urlsplit
from . import config, messages
from .executor import as_agent  # noqa: F401  (re-exported for legacy calls)

log = logging.getLogger("runner.agent")
HERE = os.path.dirname(__file__)
PLAYBOOK = open(os.path.join(HERE, "playbook.md"), encoding="utf-8").read()
ONBOARDING = open(os.path.join(HERE, "onboarding.md"), encoding="utf-8").read()
STUDY = open(os.path.join(HERE, "study.md"), encoding="utf-8").read()
TOOLS = "Read Edit Write MultiEdit Glob Grep Bash TodoWrite Task"
NEVER = "WebFetch WebSearch AskUserQuestion ExitPlanMode EnterPlanMode"   # no web, no questions in the conversation


def base_env(home, extra=None):
    name, email = (config.GIT_AUTHOR.split("<", 1) + [""])[:2]
    # Paths of the tools for each job image (Go, JDK) and user installations, in addition to the system.
    path = f"{home}/go/bin:{home}/.local/bin:/usr/local/go/bin:/go/bin:/opt/java/openjdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    env = {"PATH": path, "HOME": home, "LANG": "C.UTF-8", "TZ": "Europe/Paris",
           "GIT_AUTHOR_NAME": name.strip(), "GIT_AUTHOR_EMAIL": email.strip(" >"), "GIT_COMMITTER_NAME": name.strip(),
           "GIT_COMMITTER_EMAIL": email.strip(" >"), "GIT_TERMINAL_PROMPT": "0", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if config.JOB_PROXY:   # job container: all outbound traffic goes through the allowlisted egress proxy, except the fab relay
        relay = urlsplit(config.RELAY_PUBLIC_URL).hostname or ""
        env.update({"HTTP_PROXY": config.JOB_PROXY, "HTTPS_PROXY": config.JOB_PROXY, "http_proxy": config.JOB_PROXY,
                    "https_proxy": config.JOB_PROXY, "NO_PROXY": relay, "no_proxy": relay})
        u = urlsplit(config.JOB_PROXY)   # Maven and Gradle ignore HTTP(S)_PROXY: equivalent Java options
        if u.hostname:
            jp = " ".join(f"-D{s}.proxyHost={u.hostname} -D{s}.proxyPort={u.port or 3128}" for s in ("http", "https"))
            env.update({"MAVEN_OPTS": jp, "GRADLE_OPTS": jp})
    env.update(extra or {})
    return env


def model_env(model, gateway=None, token=None):
    """Variables that select the model. The agent passes through the runner's key gateway with its job token:
    the real key never enters the container, and no other credential of the runner either."""
    from .gateway import uses_gateway
    local = model.get("provider") != "anthropic"
    if gateway and token and uses_gateway(model):
        env = {"ANTHROPIC_BASE_URL": gateway.rstrip("/") + "/m", "ANTHROPIC_API_KEY": token, "ANTHROPIC_MODEL": model["id"]}
        if local:
            env.update({"ANTHROPIC_SMALL_FAST_MODEL": model["id"], "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "API_TIMEOUT_MS": "600000"})
        return env, local
    if not local:
        # No key the gateway can relay: no credential of the runner enters the job (neither a key nor a
        # subscription token). The runner refuses this case before launching the agent (Runner.prepare).
        return {"ANTHROPIC_MODEL": model["id"]}, False
    base = (model.get("base_url") or "").rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    return {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": "local", "ANTHROPIC_MODEL": model["id"],
            "ANTHROPIC_SMALL_FAST_MODEL": model["id"], "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "600000"}, True


class Run:
    """An agent run. Reports the tools used and consumption; stops if budget or duration is exceeded."""

    def __init__(self, ctx, panel, prompt, resume=None, system=None):
        self.ctx, self.panel, self.prompt, self.resume = ctx, panel, prompt, resume
        # The instructions are in English; they tell the model in which language to write for people (the job language).
        self.system = (system or PLAYBOOK).replace("@LANGUAGE@", messages.language((getattr(ctx, "repo", None) or {}).get("lang")))
        self.session_id, self.cost, self.tokens, self.error, self.result = resume, 0.0, 0, None, ""
        self._by_msg = {}   # model response id -> its usage (deduplicated count)
        self._shown = 0.0   # cost last shown: the live figure never goes down
        self.subagents = []
        self.stalled = self.cancelled = False
        self.proc = None
        self.last = time.time()

    def context_env(self):
        """Claude Code does not know the context window of a local model: it then never summarizes the conversation,
        which grows until the server refuses or cuts the responses (FAB-22: 125,000 tokens out of 131,072). The window
        read from the model server is given to it, so that it summarizes in time."""
        if not self.local or getattr(self.ctx.model, "get", None) is None:
            return {}
        from .gateway import context_size
        n = context_size(self.ctx.model)
        return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(n)} if n else {}

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
        pin, pout, pcache = config.price(self.ctx.model["id"], self.ctx.model)
        return (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout
                + u.get("cache_read_input_tokens", 0) * pcache + u.get("cache_creation_input_tokens", 0) * pin * 1.25) / 1e6

    def _usage(self, final=False):
        # Live, the cost is an estimate that the provider's own total corrects at the end of the run: the figure shown never
        # goes down during the run (the final figure is set by the runner once the run is over).
        self._shown = max(self._shown, round(self.ctx.base_cost + self.cost, 4))
        self.panel.event(self.ctx.ref, type="usage", cost=self._shown, tokens=self.ctx.base_tokens + self.tokens,
                         session_id=self.session_id or "",
                         agents=[{"name": M("main session"), "task": self.ctx.title, "model": self.ctx.model["id"],
                                  "tokens": self.ctx.base_tokens + self.tokens, "cost": self._shown, "running": not final}]
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
            self.subagents.append({"name": str(inp.get("subagent_type") or "sub-agent")[:40], "task": str(inp.get("description", ""))[:120]})
            return str(inp.get("description", ""))[:120]
        return ""

    def _infer_stage(self, tool):
        """If the model does not announce its steps (fab step), the runner infers them from its activity, while stating so."""
        st = self.ctx.stages
        # Only on the first file modification: before, the model still has time to announce its steps itself.
        if tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit") or "implementing" in st:
            return
        for s in [x for x in ("triage", "investigating") if x not in st] + ["implementing"]:
            st.add(s)
            self.panel.event(self.ctx.ref, type="stage", stage=s, summary=M("Step inferred by the runner from the agent's activity (not announced)."))

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
            # Claude Code sends one line per content block (thinking, text, tool call), each repeating the usage of the
            # same model response: counted once per response (its latest usage), never added up line by line.
            self._by_msg[msg.get("id") or f"line-{len(self._by_msg)}"] = u
            self.tokens = sum(x.get("input_tokens", 0) + x.get("output_tokens", 0) + x.get("cache_read_input_tokens", 0)
                              + x.get("cache_creation_input_tokens", 0) for x in self._by_msg.values())
            self.cost = sum(self._price(x) for x in self._by_msg.values())
            for blk in msg.get("content") or []:
                if blk.get("type") == "tool_use":
                    self._infer_stage(blk.get("name", ""))
                    self.panel.event(self.ctx.ref, type="log", kind=blk.get("name", "tool").lower(),
                                     message=self._tool_line(blk.get("name"), blk.get("input") or {}))
            if self.ctx.base_cost + self.cost > self.ctx.budget > 0:
                self._kill(M("budget exceeded ({budget} $)", budget=f"{self.ctx.budget:.2f}"))
        elif t == "result":
            self.session_id = m.get("session_id") or self.session_id
            self.result = str(m.get("result") or "")[:4000]
            if not self.local and m.get("total_cost_usd") is not None:
                self.cost = float(m["total_cost_usd"])
            if m.get("is_error") and not self.error:
                # The result text contains the real cause ("API Error: 500 ..."): this is what is reported.
                why = " ".join(str(m.get("result") or "").split())[:400] or str(m.get("subtype") or "unknown")
                self.error = M("the agent stopped on an error: {why}", why=why)
        if time.time() - self._last_usage > 5:
            self._last_usage = time.time()
            self._usage()

    def run(self):
        ex = self.ctx.ex
        menv, self.local = model_env(self.ctx.model, ex.gateway_url(), self.ctx.fab_token)
        menv.update(self.context_env())
        env = base_env(self.ctx.home, {"FAB_RELAY": ex.relay_url(), "FAB_TOKEN": self.ctx.fab_token,
                                       # Network volume (NFS) that presents files under a different owner than
                                       # the job user: git would refuse the repo ("dubious ownership"). The job Pod
                                       # has only one user, the repo is theirs: we declare it safe.
                                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
                                       "FAB_WORKSPACE": self.ctx.repo_dir, "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1",
                                       "DISABLE_ERROR_REPORTING": "1", **menv, **self.extra_env()})
        self.ctx.run = self
        self._last_usage, self.done = 0.0, False
        killer = threading.Timer(config.JOB_TIMEOUT, self._kill, args=(M("maximum duration exceeded ({p} min)", p=config.JOB_TIMEOUT // 60),))
        killer.start()
        idle_max = config.WATCHDOG_SECONDS or max(60, int(self.ctx.watchdog_min) * 60)

        def watchdog():   # watchdog: no line from the stream for idle_max seconds -> stop, the runner restarts it once
            while not self.done:
                time.sleep(1 if config.WATCHDOG_SECONDS else 5)
                # A model still answering (a request in progress at the key gateway) is not a silent agent: a local model
                # can take more than the threshold for one long response. Up to three thresholds without a byte, though.
                busy = getattr(self.ctx, "gw_busy", 0) > 0
                last = max(self.last, getattr(self.ctx, "gw_seen", 0) or 0)
                if not self.done and time.time() - last > (idle_max * 3 if busy else idle_max):
                    self.stalled = True
                    self._kill(M("no activity for {max} min", max=max(1, idle_max // 60)))
                    return
        threading.Thread(target=watchdog, daemon=True).start()
        try:
            code, err = ex.stream(self.ctx, self.command(), env, self.ctx.repo_dir, self._line)
        finally:
            self.done = True
            killer.cancel()
        if code not in (0, None) and not self.error:
            self.error = M("the agent exited with code {code}: {strip}", code=code, strip=err.strip())
        self._usage(final=True)
        return self

    def cancel(self, who):
        """Stop requested from the dashboard."""
        self.cancelled = True
        self._kill(M("stopped by {who}", who=who))

    def _kill(self, why):
        if not self.error:
            self.error = why
        self.ctx.ex.kill(self.ctx)


WRITES = re.compile(r"apply_patch|sed\s+-i|\bcat\s*>|\btee\b|(?<![<>2&])>\s*[\w./-]+\.\w+|\bmv\s|\bcp\s|\btouch\s|\bmkdir\s")


class CodexRun(Run):
    """Same agent run, with Codex CLI (codex exec, JSON stream). The model is reached through the runner's key gateway
    (provider "fabrique", Responses API) with the job token; the real key remains in the runner. Codex does not have
    an equivalent hook to guard.py: the disposable container, the egress proxy, and the runner's gates remain."""

    def provider(self):
        from .gateway import uses_gateway
        m, ex = self.ctx.model, self.ctx.ex
        if uses_gateway(m):
            return ex.gateway_url().rstrip("/") + "/m/v1", self.ctx.fab_token
        base = (m.get("base_url") or "").rstrip("/")
        return (base if base.endswith("/v1") else base + "/v1"), "local"

    def command(self):
        base, _ = self.provider()
        # The job's disposable container IS the sandbox (no secrets, filtered network): the option intended for this case.
        flags = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "-m", self.ctx.model["id"],
                 "-c", 'model_provider="fabrique"',
                 "-c", f'model_providers.fabrique={{name="Fabrique",base_url="{base}",env_key="FAB_MODEL_KEY",wire_api="responses"}}',
                 "-c", "analytics.enabled=false", "-c", "check_for_update_on_startup=false"]
        # No reliable way to pass system instructions with -c: Fabrique's instructions open the message, as framing data.
        prompt = self.prompt if self.resume else f"<fabrique_instructions>\n{self.system}\n</fabrique_instructions>\n\n{self.prompt}"
        # "codex exec [OPTIONS] resume <ID> [PROMPT]" : exec options before the subcommand
        tail = (["resume", self.resume] if self.resume else []) + [prompt]
        return ["sh", "-c", 'mkdir -p "$CODEX_HOME" && exec "$@"', "codex", config.CODEX_BIN, "exec"] + flags + tail

    def extra_env(self):
        _, key = self.provider()
        return {"CODEX_HOME": os.path.join(self.ctx.home, ".codex"), "FAB_MODEL_KEY": key, "RUST_LOG": "error"}

    def _price(self, u):
        if self.local:
            return 0.0
        pin, pout, pcache = config.price(self.ctx.model["id"], self.ctx.model)
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
                if WRITES.search(cmd):   # Codex often modifies files via the shell (sed -i, apply_patch, redirections)
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
                self._kill(M("budget exceeded ({budget} $)", budget=f"{self.ctx.budget:.2f}"))
        elif t in ("turn.failed", "error"):
            msg = (m.get("error") or {}).get("message") if isinstance(m.get("error"), dict) else m.get("message")
            if not self.error:
                self.error = M("Codex stopped on an error: {p}", p=str(msg or t)[:300])
        if time.time() - self._last_usage > 5:
            self._last_usage = time.time()
            self._usage()


def runner_for(ctx):
    return CodexRun if (ctx.job or {}).get("harness") == "codex" else Run
