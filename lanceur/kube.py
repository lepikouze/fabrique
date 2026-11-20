"""Lanceur Kubernetes de la Fabrique : même contrat que le lanceur Docker (start, stop, kill, exec), mais chaque job tourne
dans un Pod créé par l'API du cluster. C'est le SEUL composant qui parle à l'API Kubernetes, avec un compte de service
limité à son namespace (créer, lire, supprimer des Pods ; y exécuter une commande). La politique des Pods de jobs est
figée ici, pas négociable par l'appelant :

  image imposée (liste blanche) · utilisateur 10002 · aucune capacité · pas d'élévation de privilèges · système de
  fichiers en lecture seule · mémoire et processeur limités · un seul montage (le sous-dossier de CE job dans le volume
  de travail) · aucun accès à l'API du cluster · étiquettes fabrique.io/job (réseau fermé par NetworkPolicy)

Il n'agit que sur les Pods qu'il a créés (étiquette)."""
import logging, os, threading, time
from . import main as base
from .main import Refused, name_of

log = logging.getLogger("lanceur")
NAMESPACE = os.environ.get("JOBS_NAMESPACE", "").strip()
WORK_PVC = os.environ.get("WORK_PVC", "").strip()                       # volume partagé avec le runner (/work)
PULL_SECRETS = [s.strip() for s in os.environ.get("JOB_IMAGE_PULL_SECRETS", "").split(",") if s.strip()]
PULL_POLICY = os.environ.get("JOB_IMAGE_PULL_POLICY", "IfNotPresent").strip()
START_TIMEOUT = int(os.environ.get("JOB_START_TIMEOUT", "300"))         # tirage de l'image compris
MAX_AGE = int(os.environ.get("JOB_MAX_AGE", str(24 * 3600)))            # au-delà, un Pod de job est tenu pour orphelin
REAP_EVERY = 600
PROXY_SELECTOR = os.environ.get("PROXY_SELECTOR", "app.kubernetes.io/component=proxy").strip()   # Pod du proxy de sortie
UID = 10002
LABEL = "fabrique.io/job"
ROLE = {"fabrique.io/role": "job"}


def memory(v):
    """« 4g » (notation Docker) -> « 4Gi » ; une quantité Kubernetes est gardée telle quelle."""
    v = str(v).strip()
    return v[:-1] + {"g": "Gi", "m": "Mi", "k": "Ki"}[v[-1].lower()] if v and v[-1].lower() in "gmk" and v[:-1].isdigit() else v


def pod_spec(ref, image):
    """Manifeste du Pod d'un job. Rien ici ne vient de l'appelant, sauf la référence (validée) et le NOM de l'image."""
    ws = f"{base.WORK_IN}/{ref}"
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name_of(ref), "labels": {LABEL: ref, **ROLE, "app.kubernetes.io/name": "fabrique",
                                                      "app.kubernetes.io/component": "job"}},
        "spec": {
            "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
            "terminationGracePeriodSeconds": 5,
            "imagePullSecrets": [{"name": s} for s in PULL_SECRETS],
            "securityContext": {"runAsNonRoot": True, "runAsUser": UID, "runAsGroup": UID, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "job", "image": base.IMAGES[image], "imagePullPolicy": PULL_POLICY, "command": ["sleep", "infinity"],
                "workingDir": ws, "env": [{"name": "HOME", "value": f"{ws}/home"}],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "privileged": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"limits": {"memory": memory(base.MEM), "cpu": str(base.CPUS)},
                              "requests": {"memory": "256Mi", "cpu": "100m"}},
                "volumeMounts": [{"name": "travail", "mountPath": ws, "subPath": ref}, {"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "travail", "persistentVolumeClaim": {"claimName": WORK_PVC}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
        },
    }


def wrap(argv, env, workdir):
    """L'API d'exécution de Kubernetes ne prend ni environnement ni dossier : on les pose avec env(1) et sh(1)."""
    return ["env", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", 'cd "$0" && exec "$@"', workdir, *argv]


class KubeLauncher:
    def __init__(self, core, execer, namespace=None, sleep=time.sleep):
        self.core, self.execer, self.ns, self.sleep = core, execer, namespace or NAMESPACE, sleep

    def _mine(self, ref):
        """Le Pod du job, seulement s'il porte notre étiquette (jamais un autre Pod du namespace)."""
        try:
            p = self.core.read_namespaced_pod(name_of(ref), self.ns)
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return None
            raise
        if (p.metadata.labels or {}).get(LABEL) != ref:
            raise Refused("ce Pod n'a pas été créé par le lanceur")
        return p

    def _delete(self, ref, wait=True):
        try:
            self.core.delete_namespaced_pod(name_of(ref), self.ns, grace_period_seconds=0)
        except Exception as e:
            if getattr(e, "status", None) != 404:
                raise
        for _ in range(120 if wait else 0):
            if self._mine(ref) is None:
                return
            self.sleep(0.5)

    def _wait_running(self, ref):
        t = time.time() + START_TIMEOUT
        why = "inconnu"
        while time.time() < t:
            p = self._mine(ref)
            if p is None:
                raise Refused("le Pod du job a disparu pendant son démarrage")
            if p.status.phase == "Running":
                return p
            if p.status.phase in ("Failed", "Succeeded"):
                raise Refused(f"le Pod du job s'est arrêté au démarrage ({p.status.phase})")
            for cs in (p.status.container_statuses or []):
                w = getattr(cs.state, "waiting", None)
                if w and w.reason:
                    why = f"{w.reason} : {(w.message or '')[:200]}"
                    if w.reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError"):
                        raise Refused(f"le Pod du job ne démarre pas ({why})")
            self.sleep(1)
        raise Refused(f"le Pod du job n'a pas démarré en {START_TIMEOUT} s ({why})")

    def start(self, ref, image="python"):
        image = image or "python"
        if image not in base.IMAGES:
            raise Refused(f"image de job inconnue : {image} (liste blanche : {', '.join(sorted(base.IMAGES)) or 'vide'})")
        if not WORK_PVC:
            raise Refused("WORK_PVC n'est pas défini : le lanceur ne sait pas quel volume monter")
        p = self._mine(ref)
        if p is not None:
            if p.spec.containers[0].image == base.IMAGES[image] and p.status.phase in ("Running", "Pending"):
                self._wait_running(ref)
                return {"container": p.metadata.name, "reused": True, "image": image}
            self._delete(ref)             # autre langage (onboarding qui change d'image), ou Pod terminé : Pod neuf
        self.core.create_namespaced_pod(self.ns, pod_spec(ref, image))
        self._wait_running(ref)
        return {"container": name_of(ref), "reused": False, "image": image}

    def stop(self, ref):
        try:
            if self._mine(ref) is not None:
                self._delete(ref, wait=False)
        except Refused:
            raise
        except Exception as e:
            log.warning("%s : suppression : %s", ref, e)
        return {"stopped": True}

    def net(self, ref):
        """Sorties réseau du job depuis son démarrage, lues dans le journal du Pod du proxy (droit pods/log, lecture seule)."""
        p = self._mine(ref)
        if p is None:
            raise Refused("Pod du job absent")
        ip, started = getattr(p.status, "pod_ip", "") or "", getattr(p.status, "start_time", None)
        proxies = self.core.list_namespaced_pod(self.ns, label_selector=PROXY_SELECTOR).items
        if not ip or not proxies:
            return {"known": False}
        try:
            age = int(time.time() - started.timestamp()) + 60 if started else 24 * 3600
            text = self.core.read_namespaced_pod_log(proxies[0].metadata.name, self.ns, since_seconds=max(60, age))
        except Exception as e:
            log.warning("%s : journal du proxy illisible (%s)", ref, type(e).__name__)
            return {"known": False}
        return base.egress(text, ip)

    def kill(self, ref):
        """Tue tous les processus de l'agent dans le Pod (sauf le processus 1 qui le maintient en vie)."""
        p = self._mine(ref)
        if p is not None and p.status.phase == "Running":
            try:
                self.execer(self.core, name_of(ref), self.ns, ["kill", "-9", "-1"], lambda o: None)
            except Exception as e:
                log.warning("%s : arrêt des processus : %s", ref, e)
        return {"killed": True}

    def reap(self, now=None):
        """Supprime les Pods de jobs oubliés : terminés, ou plus vieux que MAX_AGE (runner arrêté en plein job, par
        exemple). Ne regarde que les Pods portant l'étiquette du lanceur. Renvoie les noms supprimés."""
        now, gone = now or time.time(), []
        for p in self.core.list_namespaced_pod(self.ns, label_selector=LABEL).items:
            if not (p.metadata.labels or {}).get(LABEL):
                continue
            born = p.metadata.creation_timestamp
            age = now - born.timestamp() if born else 0
            if p.status.phase in ("Succeeded", "Failed") or age > MAX_AGE:
                try:
                    self.core.delete_namespaced_pod(p.metadata.name, self.ns, grace_period_seconds=0)
                    gone.append(p.metadata.name)
                except Exception as e:
                    log.warning("%s : nettoyage : %s", p.metadata.name, e)
        if gone:
            log.info("Pods de jobs orphelins supprimés : %s", ", ".join(gone))
        return gone

    def exec_stream(self, ref, body, write):
        argv, env = body.get("argv"), body.get("env") or {}
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or sum(map(len, argv)) > 400_000:
            raise Refused("commande invalide")
        if not isinstance(env, dict) or len(env) > 64:
            raise Refused("environnement invalide")
        for k, v in env.items():
            if not base.ENV_KEY.match(k) or k in base.FORBIDDEN_ENV or not isinstance(v, str) or len(v) > 20_000:
                raise Refused(f"variable refusée : {k}")
        root = f"{base.WORK_IN}/{ref}"
        workdir = str(body.get("workdir") or root)
        if not (workdir == root or workdir.startswith(root + "/")) or ".." in workdir:
            raise Refused("dossier de travail hors de l'espace du job")
        timeout = min(int(body.get("timeout") or 600), base.MAX_TIMEOUT)
        p = self._mine(ref)
        if p is None or p.status.phase != "Running":
            raise Refused("Pod du job absent : start d'abord")
        timer = threading.Timer(timeout, self.kill, args=(ref,))
        timer.start()
        try:
            code = self.execer(self.core, name_of(ref), self.ns, wrap(argv, env, workdir), write)
        finally:
            timer.cancel()
        write({"exit": 124 if code is None else code})


def kube_exec(core, name, namespace, command, write):
    """Exécute une commande dans le Pod et relaie sa sortie ; renvoie son code de sortie."""
    from kubernetes.stream import stream
    resp = stream(core.connect_get_namespaced_pod_exec, name, namespace, command=command, container="job",
                  stderr=True, stdin=False, stdout=True, tty=False, _preload_content=False)
    try:
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                write({"o": resp.read_stdout()})
            if resp.peek_stderr():
                write({"e": resp.read_stderr()})
        return resp.returncode
    finally:
        resp.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not base.TOKEN or len(base.TOKEN) < 32:
        raise SystemExit("LAUNCHER_TOKEN manquant ou trop court")
    if not NAMESPACE or not WORK_PVC:
        raise SystemExit("JOBS_NAMESPACE et WORK_PVC sont obligatoires")
    from kubernetes import client, config as kconfig
    kconfig.load_incluster_config()
    log.info("lanceur Kubernetes : namespace %s, volume de travail %s", NAMESPACE, WORK_PVC)
    launcher = KubeLauncher(client.CoreV1Api(), kube_exec)

    def reaper():
        while True:
            try:
                launcher.reap()
            except Exception as e:
                log.warning("nettoyage des Pods orphelins : %s", e)
            time.sleep(REAP_EVERY)
    threading.Thread(target=reaper, daemon=True).start()
    base.serve(launcher)


if __name__ == "__main__":
    main()
