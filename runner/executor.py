"""Where the repo code runs (installation, agent, checks, reviewers). Two modes, same interface:

- "local" (shared workshop): in the runner container, under the agent user (setpriv). Simple, but all
  jobs share the same container as the runner and its secrets are only protected by the Unix user.
- "docker" (one container per job): the runner asks the LAUNCHER to create a disposable container for the job, then
  execute commands in it. The runner has no access to Docker; the job container has no secrets, no Git, and no
  local network (internal network, egress via the allowlist proxy).

The initial clone and push remain in the runner (mirror, frozen configuration). Git operations on the agent
repo, after its agent run, pass through here: they run where the agent code is already running (see main.jgit)."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, os, signal, subprocess, threading
import httpx
from . import config

log = logging.getLogger("runner.executor")


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


SETPRIV = "/usr/bin/setpriv"   # absolute path: never resolved by a PATH that the agent could influence


def as_agent():
    """Prefix that runs a command under the agent user (without capabilities), in local mode."""
    if not config.AGENT_UID:
        return []
    u = config.AGENT_UID
    return [SETPRIV, f"--reuid={u}", f"--regid={u}", "--clear-groups", "--inh-caps=-all", "--"]


def kill_agent_leftovers():
    """Local mode: kills every remaining process of the agent's user."""
    if not config.AGENT_UID:
        return
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                if os.stat(f"/proc/{pid}").st_uid == int(config.AGENT_UID):
                    os.kill(int(pid), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass


class Local:
    name = "local"

    def __init__(self):
        self.procs = {}

    def relay_url(self):
        return f"http://127.0.0.1:{config.RELAY_PORT}"

    def gateway_url(self):
        return f"http://127.0.0.1:{config.GATEWAY_PORT}"

    def start(self, ctx):
        pass

    def stop(self, ctx):
        self.kill(ctx)

    def run(self, ctx, argv, env, cwd, timeout):
        try:
            p = subprocess.run(as_agent() + argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill(ctx)
            return Result(124, "", M("timeout exceeded ({timeout} s)", timeout=timeout))
        return Result(p.returncode, p.stdout, p.stderr)

    def stream(self, ctx, argv, env, cwd, on_line):
        """Runs the command and calls on_line for each line of output; returns (code, stderr end)."""
        proc = subprocess.Popen(as_agent() + argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, start_new_session=True)
        self.procs[ctx.ref] = proc
        err = []
        t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
        t.start()
        try:
            for line in proc.stdout:
                on_line(line)
            proc.wait()
        finally:
            self.procs.pop(ctx.ref, None)
        t.join(timeout=5)
        return proc.returncode, "".join(err)[-1500:]

    def kill(self, ctx):
        proc = self.procs.get(ctx.ref)
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        kill_agent_leftovers()


class Docker:
    """Launcher client (private network, token). Each job has its own `fabrique-job-<ref>` container."""
    name = "docker"

    def __init__(self, base=None, token=None, transport=None):
        self.h = httpx.Client(base_url=(base or config.LAUNCHER_URL).rstrip("/"), transport=transport,
                              headers={"Authorization": f"Bearer {token or config.LAUNCHER_TOKEN}"}, timeout=httpx.Timeout(30, read=None))

    def relay_url(self):
        return config.RELAY_PUBLIC_URL

    def gateway_url(self):
        return config.GATEWAY_PUBLIC_URL

    def _post(self, path, body=None):
        r = self.h.post(path, json=body or {})
        if r.status_code != 200:
            raise RuntimeError(M("launcher: {path} refused ({status_code}) {p}", path=path, status_code=r.status_code, p=r.text[:200]))
        return r.json()

    def start(self, ctx):
        self._post(f"/jobs/{ctx.ref}/start", {"image": getattr(ctx, "image", "") or "python"})

    def stop(self, ctx):
        try:
            self._post(f"/jobs/{ctx.ref}/stop")
        except Exception as e:
            log.warning("%s: cannot stop the container (%s)", ctx.ref, e)

    def kill(self, ctx):
        try:
            self._post(f"/jobs/{ctx.ref}/kill")
        except Exception as e:
            log.warning("%s: cannot stop the processes (%s)", ctx.ref, e)

    def net(self, ctx):
        """Network egress of the job (proxy log, read by the launcher), or None if the launcher does not know."""
        try:
            r = self._post(f"/jobs/{ctx.ref}/net")
        except Exception as e:   # old launcher (no "net" action), proxy not found...: no Network line
            log.info("%s: network egress unavailable (%s)", ctx.ref, e)
            return None
        return r if r.get("known") else None

    def _exec(self, ctx, argv, env, cwd, timeout, on_out):
        code, err = 125, []
        with self.h.stream("POST", f"/jobs/{ctx.ref}/exec", json={"argv": argv, "env": env, "workdir": cwd, "timeout": timeout}) as r:
            if r.status_code != 200:
                r.read()
                raise RuntimeError(M("launcher: execution refused ({status_code}) {p}", status_code=r.status_code, p=r.text[:200]))
            for line in r.iter_lines():
                if not line:
                    continue
                m = json.loads(line)
                if "o" in m:
                    on_out(m["o"])
                elif "e" in m:
                    err.append(m["e"])
                elif "exit" in m:
                    code = int(m["exit"])
        return code, "".join(err)

    def run(self, ctx, argv, env, cwd, timeout):
        out = []
        code, err = self._exec(ctx, argv, env, cwd, timeout, out.append)
        return Result(code, "".join(out), err)

    def stream(self, ctx, argv, env, cwd, on_line):
        buf = [""]

        def feed(chunk):
            buf[0] += chunk
            *lines, buf[0] = buf[0].split("\n")
            for ln in lines:
                on_line(ln + "\n")
        code, err = self._exec(ctx, argv, env, cwd, config.JOB_TIMEOUT + 60, feed)
        if buf[0]:
            on_line(buf[0])
        return code, err[-1500:]


def make():
    return Docker() if config.EXECUTOR == "docker" else Local()
