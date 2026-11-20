"""Lanceur de la Fabrique : le SEUL composant qui parle à Docker. Il offre quatre actions au runner (jeton, réseau privé),
(plus « net » : lire, dans le journal du proxy, les sorties réseau d'un job),
et la politique de sécurité des conteneurs de jobs est figée ici, pas négociable par l'appelant :

  image imposée · utilisateur 10002 · aucune capacité · no-new-privileges · système de fichiers en lecture seule ·
  mémoire, processeur et nombre de processus limités · un seul montage (l'espace de travail de CE job) ·
  un seul réseau (interne, sans sortie : le proxy en liste blanche et le relais du runner) · étiquette fabrique.job

Il n'agit que sur les conteneurs qu'il a créés (étiquette). C'est l'équivalent, avec Docker, du compte de service
Kubernetes limité à un namespace et du contrôle d'admission « restricted »."""
import json, logging, os, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("lanceur")
def _token():
    """Jeton exigé pour toute requête : variable LAUNCHER_TOKEN, sinon fichier LAUNCHER_TOKEN_FILE (créé ici s'il manque,
    sur un volume partagé avec le runner seul, en mode 0600)."""
    t = os.environ.get("LAUNCHER_TOKEN", "").strip()
    path = os.environ.get("LAUNCHER_TOKEN_FILE", "").strip()
    if t or not path:
        return t
    try:
        if not (os.path.exists(path) and open(path).read().strip()):
            import secrets
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(48) + "\n")
        # Lisible par son seul propriétaire (le lanceur et le runner tournent sous le même utilisateur) : dans le conteneur
        # du runner, l'utilisateur de l'agent ne peut pas le lire. Corrige aussi un fichier créé par une ancienne version.
        os.chmod(path, 0o600)
        return open(path).read().strip()
    except OSError:
        return ""


TOKEN = _token()
IMAGE = os.environ.get("ATELIER_IMAGE", "").strip()          # image par défaut (python)
# Liste blanche des images de jobs, par langage : « python=registre/img:tag,node=…,go=…,java=… ». Le runner ne choisit
# qu'un NOM de cette liste ; jamais une image arbitraire.
IMAGES = {k.strip(): v.strip() for k, v in (x.split("=", 1) for x in os.environ.get("ATELIER_IMAGES", "").split(",") if "=" in x)}
if IMAGE:
    IMAGES.setdefault("python", IMAGE)
NETWORK = os.environ.get("JOBS_NETWORK", "fabrique-jobs")
PREFIX = os.environ.get("JOBS_PREFIX", "fabrique-job").strip() or "fabrique-job"   # nom des conteneurs de jobs (deux piles côte à côte)
WORK_HOST = os.environ.get("WORK_HOST", "/opt/volumes/fabrique-runner").rstrip("/")   # dossier des espaces de travail, sur l'hôte
WORK_IN = os.environ.get("WORK_DIR", "/work").rstrip("/")                            # même chemin que dans le runner
MEM = os.environ.get("JOB_MEMORY", "4g")
CPUS = float(os.environ.get("JOB_CPUS", "2"))
PIDS = int(os.environ.get("JOB_PIDS", "512"))
USER = "10002:10002"
LABEL = "fabrique.job"
REF = re.compile(r"^[A-Z][A-Z0-9]{0,9}-[0-9]{1,7}$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
FORBIDDEN_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONSTARTUP"}
MAX_TIMEOUT = 4 * 3600


class Refused(Exception):
    pass


PROXY_CONTAINER = os.environ.get("PROXY_CONTAINER", "").strip()   # nom du conteneur du proxy ; vide : celui du service « proxy » sur le réseau des jobs
_CONNECT = re.compile(r"Connect \(file descriptor (\d+)\): (\S+)")
_REQUEST = re.compile(r"Request \(file descriptor (\d+)\): (\S+) (\S+)")
_REFUSED = re.compile(r'Proxying refused on filtered domain "([^"]+)"')


def egress(text, ip):
    """Sorties réseau d'un job, lues dans le journal du proxy (tinyproxy) : ce que le client `ip` a demandé, et ce que la
    liste blanche a refusé. Une requête est autorisée sauf si le proxy écrit ensuite qu'il la refuse.
    Renvoie {"allowed": [[hôte, nombre]…], "denied": [[hôte, nombre]…]}, du plus fréquent au moins fréquent."""
    fd_ip, pending = {}, []                       # pending : [ip, hôte, refusée ?] dans l'ordre du journal
    for line in (text or "").splitlines():
        m = _CONNECT.search(line)
        if m:
            fd_ip[m.group(1)] = m.group(2)
            continue
        m = _REQUEST.search(line)
        if m:
            target = m.group(3)
            host = re.sub(r"^[a-z]+://", "", target, flags=re.I).split("/")[0].rsplit("@", 1)[-1]
            host = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
            pending.append([fd_ip.get(m.group(1), ""), host.lower()[:200], False])
            continue
        m = _REFUSED.search(line)
        if m:
            for p in reversed(pending):
                if p[1] == m.group(1).lower() and not p[2]:
                    p[2] = True
                    break
    ok, ko = {}, {}
    for pip, host, refused in pending:
        if pip == ip and host:
            d = ko if refused else ok
            d[host] = d.get(host, 0) + 1
    order = lambda d: [[h, n] for h, n in sorted(d.items(), key=lambda x: (-x[1], x[0]))][:50]
    return {"known": True, "allowed": order(ok), "denied": order(ko)}


def name_of(ref):
    if not REF.match(ref or ""):
        raise Refused("référence de job invalide")
    return f"{PREFIX}-{ref.lower()}"


class Launcher:
    def __init__(self, client):
        self.docker = client
        self.api = client.api

    def _mine(self, ref):
        """Le conteneur du job, seulement s'il porte notre étiquette (jamais un autre conteneur de l'hôte)."""
        try:
            c = self.docker.containers.get(name_of(ref))
        except Exception:
            return None
        if c.labels.get(LABEL) != ref:
            raise Refused("ce conteneur n'a pas été créé par le lanceur")
        return c

    def start(self, ref, image="python"):
        image = image or "python"
        if image not in IMAGES:
            raise Refused(f"image de job inconnue : {image} (liste blanche : {', '.join(sorted(IMAGES)) or 'vide'})")
        IMG = IMAGES[image]
        c = self._mine(ref)
        if c is not None:
            if (c.image.tags or [""])[0] == IMG or IMG in (c.image.tags or []):
                if c.status != "running":
                    c.start()
                return {"container": c.name, "reused": True, "image": image}
            c.remove(force=True)            # autre langage (onboarding qui change d'image) : conteneur neuf
        ws = f"{WORK_HOST}/{ref}"
        c = self.docker.containers.run(
            IMG, ["sleep", "infinity"], name=name_of(ref), detach=True, user=USER, labels={LABEL: ref},
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], read_only=True,
            tmpfs={"/tmp": "rw,nosuid,nodev,size=1g"}, mem_limit=MEM, nano_cpus=int(CPUS * 1e9), pids_limit=PIDS,
            network=NETWORK, volumes={ws: {"bind": f"{WORK_IN}/{ref}", "mode": "rw"}}, working_dir=f"{WORK_IN}/{ref}",
            environment={"HOME": f"{WORK_IN}/{ref}/home"}, auto_remove=False, privileged=False)
        return {"container": c.name, "reused": False, "image": image}

    def stop(self, ref):
        c = self._mine(ref)
        if c is not None:
            try:
                c.remove(force=True)
            except Exception as e:
                log.warning("%s : suppression : %s", ref, e)
        return {"stopped": True}

    def _proxy(self):
        """Conteneur du proxy de sortie : celui nommé par PROXY_CONTAINER, sinon le service « proxy » du réseau des jobs."""
        try:
            if PROXY_CONTAINER:
                return self.docker.containers.get(PROXY_CONTAINER)
            found = self.docker.containers.list(filters={"network": NETWORK, "label": "com.docker.compose.service=proxy"})
            return found[0] if found else None
        except Exception:
            return None

    def net(self, ref):
        """Sorties réseau du job depuis son démarrage. Seule LECTURE hors de ses propres conteneurs : le journal du proxy."""
        c = self._mine(ref)
        if c is None:
            raise Refused("conteneur du job absent")
        proxy = self._proxy()
        if proxy is None:
            return {"known": False}
        attrs = getattr(c, "attrs", None) or {}
        ip = (((attrs.get("NetworkSettings") or {}).get("Networks") or {}).get(NETWORK) or {}).get("IPAddress") or ""
        since = None
        try:
            import datetime as dt
            since = dt.datetime.fromisoformat(str((attrs.get("State") or {}).get("StartedAt") or "")[:19]).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
        if not ip:
            return {"known": False}
        text = proxy.logs(stdout=True, stderr=True, **({"since": since} if since else {"tail": 20000}))
        return egress(text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text), ip)

    def kill(self, ref):
        """Tue tous les processus de l'agent dans le conteneur (sauf le processus 1 qui le maintient en vie)."""
        c = self._mine(ref)
        if c is not None and c.status == "running":
            ex = self.api.exec_create(c.id, ["kill", "-9", "-1"], user=USER)
            self.api.exec_start(ex["Id"])
        return {"killed": True}

    def exec_stream(self, ref, body, write):
        argv, env = body.get("argv"), body.get("env") or {}
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or sum(map(len, argv)) > 400_000:
            raise Refused("commande invalide")
        if not isinstance(env, dict) or len(env) > 64:
            raise Refused("environnement invalide")
        for k, v in env.items():
            if not ENV_KEY.match(k) or k in FORBIDDEN_ENV or not isinstance(v, str) or len(v) > 20_000:
                raise Refused(f"variable refusée : {k}")
        workdir = str(body.get("workdir") or f"{WORK_IN}/{ref}")
        if not (workdir == f"{WORK_IN}/{ref}" or workdir.startswith(f"{WORK_IN}/{ref}/")) or ".." in workdir:
            raise Refused("dossier de travail hors de l'espace du job")
        timeout = min(int(body.get("timeout") or 600), MAX_TIMEOUT)
        c = self._mine(ref)
        if c is None or c.status != "running":
            raise Refused("conteneur du job absent : start d'abord")
        ex = self.api.exec_create(c.id, argv, user=USER, environment=env, workdir=workdir, stdout=True, stderr=True)
        timer = threading.Timer(timeout, self.kill, args=(ref,))
        timer.start()
        try:
            for out, err in self.api.exec_start(ex["Id"], stream=True, demux=True):
                if out:
                    write({"o": out.decode("utf-8", "replace")})
                if err:
                    write({"e": err.decode("utf-8", "replace")})
        finally:
            timer.cancel()
        code = self.api.exec_inspect(ex["Id"]).get("ExitCode")
        write({"exit": 124 if code is None else code})


def serve(launcher, host="0.0.0.0", port=7080):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"   # réponse en flux, fermée à la fin

        def _reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, {"ok": True})
            self._reply(404, {"error": "inconnu"})

        def do_POST(self):
            if not TOKEN or self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
                return self._reply(403, {"error": "jeton invalide"})
            m = re.match(r"^/jobs/([^/]+)/(start|stop|kill|exec|net)$", self.path)
            if not m:
                return self._reply(404, {"error": "inconnu"})
            ref, action = m.groups()
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = json.loads(self.rfile.read(n) or b"{}")
                name_of(ref)
                if action == "exec":
                    self.send_response(200); self.send_header("Content-Type", "application/x-ndjson"); self.end_headers()
                    w = self.wfile
                    try:
                        launcher.exec_stream(ref, body, lambda o: (w.write((json.dumps(o, ensure_ascii=False) + "\n").encode()), w.flush()))
                    except Refused as e:
                        w.write((json.dumps({"e": f"refusé : {e}"}) + "\n" + json.dumps({"exit": 126}) + "\n").encode())
                    return
                log.info("%s : %s", ref, action)
                if action == "start":
                    return self._reply(200, launcher.start(ref, str(body.get("image") or "python")))
                self._reply(200, getattr(launcher, action)(ref))
            except Refused as e:
                self._reply(400, {"error": str(e)})
            except Exception as e:
                log.exception("%s : %s", ref, action)
                self._reply(500, {"error": type(e).__name__})

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer((host, port), H)
    log.info("lanceur prêt sur %s:%s, images %s, réseau %s", host, port, ", ".join(f"{k}={v}" for k, v in sorted(IMAGES.items())), NETWORK)
    srv.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN or len(TOKEN) < 32:
        raise SystemExit("LAUNCHER_TOKEN manquant ou trop court")
    import docker
    serve(Launcher(docker.from_env()))


if __name__ == "__main__":
    main()
