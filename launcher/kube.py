"""Fabrique Kubernetes launcher: same contract as the Docker launcher (start, stop, kill, exec), but each job runs
in a Pod created by the cluster API. It is the ONLY component that talks to the Kubernetes API, with a service
account limited to its namespace (create, read, delete Pods; execute a command in them). The job Pod policy is
fixed here, not negotiable by the caller:

  imposed image (allowlist) · user 10002 · no capabilities · no privilege escalation · read-only file
  system · limited memory and CPU · a single mount (the sub-folder of THIS job in the working volume)
  · no cluster API access · fabrique.io/job labels (network closed by NetworkPolicy)

It only acts on the Pods it has created (label)."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import logging, os, threading, time
from . import main as base
from .main import Refused, name_of

log = logging.getLogger("launcher")
NAMESPACE = os.environ.get("JOBS_NAMESPACE", "").strip()
WORK_PVC = os.environ.get("WORK_PVC", "").strip()                       # shared volume with the runner (/work)
PULL_SECRETS = [s.strip() for s in os.environ.get("JOB_IMAGE_PULL_SECRETS", "").split(",") if s.strip()]
PULL_POLICY = os.environ.get("JOB_IMAGE_PULL_POLICY", "IfNotPresent").strip()
START_TIMEOUT = int(os.environ.get("JOB_START_TIMEOUT", "300"))         # image pull included
MAX_AGE = int(os.environ.get("JOB_MAX_AGE", str(24 * 3600)))            # beyond that, a job Pod is considered orphaned
REAP_EVERY = 600
PROXY_SELECTOR = os.environ.get("PROXY_SELECTOR", "app.kubernetes.io/component=proxy").strip()   # the egress proxy's Pod
UID = 10002
LABEL = "fabrique.io/job"
ROLE = {"fabrique.io/role": "job"}


def memory(v):
    """"4g" (Docker notation) -> "4Gi"; a Kubernetes quantity is kept as is."""
    v = str(v).strip()
    return v[:-1] + {"g": "Gi", "m": "Mi", "k": "Ki"}[v[-1].lower()] if v and v[-1].lower() in "gmk" and v[:-1].isdigit() else v


def pod_spec(ref, image):
    """Job Pod manifest. Nothing here comes from the caller, except the reference (validated) and the image NAME."""
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
                "volumeMounts": [{"name": "work", "mountPath": ws, "subPath": ref}, {"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": WORK_PVC}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
        },
    }


def wrap(argv, env, workdir):
    """The Kubernetes execution API takes neither environment nor directory: they are set using env(1) and sh(1)."""
    return ["env", *[f"{k}={v}" for k, v in env.items()], "sh", "-c", 'cd "$0" && exec "$@"', workdir, *argv]


class KubeLauncher:
    def __init__(self, core, execer, namespace=None, sleep=time.sleep):
        self.core, self.execer, self.ns, self.sleep = core, execer, namespace or NAMESPACE, sleep

    def _mine(self, ref):
        """The job Pod, only if it carries our label (never another Pod in the namespace)."""
        try:
            p = self.core.read_namespaced_pod(name_of(ref), self.ns)
        except Exception as e:
            if getattr(e, "status", None) == 404:
                return None
            raise
        if (p.metadata.labels or {}).get(LABEL) != ref:
            raise Refused(M("this Pod was not created by the launcher"))
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
        why = "unknown"
        while time.time() < t:
            p = self._mine(ref)
            if p is None:
                raise Refused(M("the job's Pod disappeared while starting"))
            if p.status.phase == "Running":
                return p
            if p.status.phase in ("Failed", "Succeeded"):
                raise Refused(M("the job's Pod stopped at start-up ({phase})", phase=p.status.phase))
            for cs in (p.status.container_statuses or []):
                w = getattr(cs.state, "waiting", None)
                if w and w.reason:
                    why = f"{w.reason} : {(w.message or '')[:200]}"
                    if w.reason in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError", "CreateContainerError"):
                        raise Refused(M("the job's Pod does not start ({why})", why=why))
            self.sleep(1)
        raise Refused(M("the job's Pod did not start within {start_timeout} s ({why})", start_timeout=START_TIMEOUT, why=why))

    def start(self, ref, image="python"):
        image = image or "python"
        if image not in base.IMAGES:
            raise Refused(f"unknown job image: {image} (allowlist: {', '.join(sorted(base.IMAGES)) or 'empty'})")
        if not WORK_PVC:
            raise Refused("WORK_PVC is not set: the launcher does not know which volume to mount")
        p = self._mine(ref)
        if p is not None:
            if p.spec.containers[0].image == base.IMAGES[image] and p.status.phase in ("Running", "Pending"):
                self._wait_running(ref)
                return {"container": p.metadata.name, "reused": True, "image": image}
            self._delete(ref)             # other language (onboarding that changes image), or finished Pod: new Pod
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
            log.warning("%s: removal: %s", ref, e)
        return {"stopped": True}

    def net(self, ref):
        """Job network egress from its startup, read from the proxy Pod log (pods/log permission, read-only)."""
        p = self._mine(ref)
        if p is None:
            raise Refused(M("job Pod missing"))
        ip, started = getattr(p.status, "pod_ip", "") or "", getattr(p.status, "start_time", None)
        proxies = self.core.list_namespaced_pod(self.ns, label_selector=PROXY_SELECTOR).items
        if not ip or not proxies:
            return {"known": False}
        try:
            age = int(time.time() - started.timestamp()) + 60 if started else 24 * 3600
            text = self.core.read_namespaced_pod_log(proxies[0].metadata.name, self.ns, since_seconds=max(60, age))
        except Exception as e:
            log.warning("%s: proxy log unreadable (%s)", ref, type(e).__name__)
            return {"known": False}
        return base.egress(text, ip)

    def kill(self, ref):
        """Kills all agent processes in the Pod (except process 1 which keeps it alive)."""
        p = self._mine(ref)
        if p is not None and p.status.phase == "Running":
            try:
                self.execer(self.core, name_of(ref), self.ns, ["kill", "-9", "-1"], lambda o: None)
            except Exception as e:
                log.warning("%s: stopping the processes: %s", ref, e)
        return {"killed": True}

    def reap(self, now=None):
        """Deletes forgotten job Pods: finished, or older than MAX_AGE (runner stopped mid-job, for
        example). Only looks at Pods carrying the launcher label. Returns the deleted names."""
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
                    log.warning("%s: cleanup: %s", p.metadata.name, e)
        if gone:
            log.info("orphaned job Pods deleted: %s", ", ".join(gone))
        return gone

    def exec_stream(self, ref, body, write):
        argv, env = body.get("argv"), body.get("env") or {}
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv) or sum(map(len, argv)) > 400_000:
            raise Refused("invalid command")
        if not isinstance(env, dict) or len(env) > 64:
            raise Refused("invalid environment")
        for k, v in env.items():
            if not base.ENV_KEY.match(k) or k in base.FORBIDDEN_ENV or not isinstance(v, str) or len(v) > 20_000:
                raise Refused(M("variable refused: {k}", k=k))
        root = f"{base.WORK_IN}/{ref}"
        workdir = str(body.get("workdir") or root)
        if not (workdir == root or workdir.startswith(root + "/")) or ".." in workdir:
            raise Refused(M("working directory outside the job's space"))
        timeout = min(int(body.get("timeout") or 600), base.MAX_TIMEOUT)
        p = self._mine(ref)
        if p is None or p.status.phase != "Running":
            raise Refused(M("job Pod missing: start first"))
        timer = threading.Timer(timeout, self.kill, args=(ref,))
        timer.start()
        try:
            code = self.execer(self.core, name_of(ref), self.ns, wrap(argv, env, workdir), write)
        finally:
            timer.cancel()
        write({"exit": 124 if code is None else code})


def kube_exec(core, name, namespace, command, write):
    """Executes a command in the Pod and relays its output; returns its exit code."""
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
        raise SystemExit("LAUNCHER_TOKEN missing or too short")
    if not NAMESPACE or not WORK_PVC:
        raise SystemExit("JOBS_NAMESPACE and WORK_PVC are required")
    from kubernetes import client, config as kconfig
    kconfig.load_incluster_config()
    log.info("Kubernetes launcher: namespace %s, work volume %s", NAMESPACE, WORK_PVC)
    launcher = KubeLauncher(client.CoreV1Api(), kube_exec)

    def reaper():
        while True:
            try:
                launcher.reap()
            except Exception as e:
                log.warning("cleanup of orphaned Pods: %s", e)
            time.sleep(REAP_EVERY)
    threading.Thread(target=reaper, daemon=True).start()
    base.serve(launcher)


if __name__ == "__main__":
    main()
