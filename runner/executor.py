"""Où s'exécute le code du dépôt (installation, agent, contrôles, relecteurs). Deux modes, même interface :

- « local » (atelier partagé) : dans le conteneur du runner, sous l'utilisateur de l'agent (setpriv). Simple, mais tous
  les jobs partagent le même conteneur que le runner et ses secrets ne sont protégés que par l'utilisateur Unix.
- « docker » (un conteneur par job) : le runner demande au LANCEUR de créer un conteneur jetable pour le job, puis d'y
  exécuter les commandes. Le runner n'a aucun accès à Docker ; le conteneur du job n'a ni secret, ni Git, ni réseau
  local (réseau interne, sortie par le proxy en liste blanche).

Le clone initial et la poussée restent dans le runner (miroir, configuration figée). Les opérations Git sur le dépôt de
l'agent, après son passage, passent par ici : elles s'exécutent là où le code de l'agent tourne déjà (voir main.jgit)."""
import json, logging, os, signal, subprocess, threading
import httpx
from . import config

log = logging.getLogger("runner.executor")


class Result:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


SETPRIV = "/usr/bin/setpriv"   # chemin absolu : jamais résolu par un PATH que l'agent pourrait influencer


def as_agent():
    """Préfixe qui fait tourner une commande sous l'utilisateur de l'agent (sans capacités), en mode local."""
    if not config.AGENT_UID:
        return []
    u = config.AGENT_UID
    return [SETPRIV, f"--reuid={u}", f"--regid={u}", "--clear-groups", "--inh-caps=-all", "--"]


def kill_agent_leftovers():
    """Mode local : tue tout processus restant de l'utilisateur de l'agent."""
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
            return Result(124, "", f"délai dépassé ({timeout} s)")
        return Result(p.returncode, p.stdout, p.stderr)

    def stream(self, ctx, argv, env, cwd, on_line):
        """Lance la commande et appelle on_line pour chaque ligne de sortie ; renvoie (code, fin de stderr)."""
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
    """Client du lanceur (réseau privé, jeton). Chaque job a son conteneur `fabrique-job-<ref>`."""
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
            raise RuntimeError(f"lanceur : {path} refusé ({r.status_code}) {r.text[:200]}")
        return r.json()

    def start(self, ctx):
        self._post(f"/jobs/{ctx.ref}/start", {"image": getattr(ctx, "image", "") or "python"})

    def stop(self, ctx):
        try:
            self._post(f"/jobs/{ctx.ref}/stop")
        except Exception as e:
            log.warning("%s : arrêt du conteneur impossible (%s)", ctx.ref, e)

    def kill(self, ctx):
        try:
            self._post(f"/jobs/{ctx.ref}/kill")
        except Exception as e:
            log.warning("%s : arrêt des processus impossible (%s)", ctx.ref, e)

    def net(self, ctx):
        """Sorties réseau du job (journal du proxy, lu par le lanceur), ou None si le lanceur ne sait pas le dire."""
        try:
            r = self._post(f"/jobs/{ctx.ref}/net")
        except Exception as e:   # ancien lanceur (pas d'action « net »), proxy introuvable… : pas de ligne Réseau
            log.info("%s : sorties réseau indisponibles (%s)", ctx.ref, e)
            return None
        return r if r.get("known") else None

    def _exec(self, ctx, argv, env, cwd, timeout, on_out):
        code, err = 125, []
        with self.h.stream("POST", f"/jobs/{ctx.ref}/exec", json={"argv": argv, "env": env, "workdir": cwd, "timeout": timeout}) as r:
            if r.status_code != 200:
                r.read()
                raise RuntimeError(f"lanceur : exécution refusée ({r.status_code}) {r.text[:200]}")
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
