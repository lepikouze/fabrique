"""Kubernetes launcher: one Pod per job, fixed policy, same contract as the Docker launcher. Simulated cluster."""
from types import SimpleNamespace as NS
import pytest
from launcher import kube, main as base


class NotFound(Exception):
    status = 404


class FakeCore:
    """Just what the launcher uses from CoreV1Api."""
    def __init__(self):
        self.pods, self.created, self.deleted = {}, [], []

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise NotFound()
        return self.pods[name]

    def create_namespaced_pod(self, ns, body):
        self.created.append((ns, body))
        self.pods[body["metadata"]["name"]] = NS(metadata=NS(name=body["metadata"]["name"], labels=body["metadata"]["labels"]),
                                                 spec=NS(containers=[NS(image=body["spec"]["containers"][0]["image"])]),
                                                 status=NS(phase="Running", container_statuses=[]))

    def list_namespaced_pod(self, ns, label_selector=""):
        return NS(items=[p for p in self.pods.values() if label_selector in (p.metadata.labels or {})])

    def delete_namespaced_pod(self, name, ns, grace_period_seconds=0):
        self.deleted.append(name)
        if self.pods.pop(name, None) is None:
            raise NotFound()


@pytest.fixture
def L(monkeypatch):
    monkeypatch.setattr(base, "IMAGES", {"python": "reg/job-python:1", "node": "reg/job-node:1"})
    monkeypatch.setattr(kube, "WORK_PVC", "fabrique-work")
    core, calls = FakeCore(), []

    def execer(c, name, ns, command, write):
        calls.append((name, ns, command))
        write({"o": "bonjour\n"})
        return 0
    launcher = kube.KubeLauncher(core, execer, namespace="fabrique", sleep=lambda s: None)
    launcher.calls = calls
    return launcher


def test_pod_policy_is_fixed_and_restrictive(L):
    assert L.start("FAB-12", "python") == {"container": "fabrique-job-fab-12", "reused": False, "image": "python"}
    ns, pod = L.core.created[0]
    spec, c = pod["spec"], pod["spec"]["containers"][0]
    assert ns == "fabrique" and pod["metadata"]["labels"]["fabrique.io/job"] == "FAB-12" and pod["metadata"]["labels"]["fabrique.io/role"] == "job"
    assert c["image"] == "reg/job-python:1" and c["command"] == ["sleep", "infinity"]
    assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False and spec["restartPolicy"] == "Never"
    assert spec["securityContext"]["runAsNonRoot"] and spec["securityContext"]["runAsUser"] == 10002
    sc = c["securityContext"]
    assert sc == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "privileged": False, "capabilities": {"drop": ["ALL"]}}
    assert c["resources"]["limits"]["memory"].endswith("i") and c["resources"]["limits"]["cpu"]
    # a single data mount: the sub-folder of THIS job in the workspace volume
    assert c["volumeMounts"][0] == {"name": "work", "mountPath": "/work/FAB-12", "subPath": "FAB-12"}
    assert spec["volumes"][0] == {"name": "work", "persistentVolumeClaim": {"claimName": "fabrique-work"}}
    assert "hostPath" not in str(pod) and "hostNetwork" not in str(pod) and "serviceAccountName" not in spec


def test_only_whitelisted_images_and_valid_references(L):
    with pytest.raises(base.Refused):
        L.start("FAB-1", "ubuntu:latest")                                  # never an arbitrary image
    with pytest.raises(base.Refused):
        L.start("../kube-system", "python")
    assert L.core.created == []


def test_start_reuses_the_pod_or_replaces_it_when_the_image_changes(L):
    L.start("FAB-1", "python")
    assert L.start("FAB-1", "python")["reused"] is True and len(L.core.created) == 1
    assert L.start("FAB-1", "node")["reused"] is False                     # onboarding qui change de langage : Pod neuf
    assert L.core.deleted == ["fabrique-job-fab-1"] and L.core.created[1][1]["spec"]["containers"][0]["image"] == "reg/job-node:1"


def test_never_touches_a_pod_it_did_not_create(L):
    L.core.pods["fabrique-job-fab-7"] = NS(metadata=NS(name="fabrique-job-fab-7", labels={"app": "autre"}), spec=NS(containers=[NS(image="x")]),
                                           status=NS(phase="Running", container_statuses=[]))
    for action in (lambda: L.start("FAB-7"), lambda: L.stop("FAB-7"), lambda: L.kill("FAB-7"),
                   lambda: L.exec_stream("FAB-7", {"argv": ["id"]}, lambda o: None)):
        with pytest.raises(base.Refused):
            action()
    assert L.core.deleted == [] and L.calls == []


def test_exec_sets_environment_and_workdir_and_stays_in_the_job_space(L):
    L.start("FAB-3")
    out = []
    L.exec_stream("FAB-3", {"argv": ["pytest", "-q"], "env": {"HOME": "/work/FAB-3/home", "A": "b c"}, "workdir": "/work/FAB-3/repo"}, out.append)
    name, ns, cmd = L.calls[-1]
    assert (name, ns) == ("fabrique-job-fab-3", "fabrique")
    assert cmd == ["env", "HOME=/work/FAB-3/home", "A=b c", "sh", "-c", 'cd "$0" && exec "$@"', "/work/FAB-3/repo", "pytest", "-q"]
    assert out == [{"o": "bonjour\n"}, {"exit": 0}]
    for bad in ({"argv": ["id"], "workdir": "/work/FAB-4"}, {"argv": ["id"], "workdir": "/work/FAB-3/../FAB-4"}, {"argv": ["id"], "workdir": "/etc"},
                {"argv": ["id"], "env": {"LD_PRELOAD": "x"}}, {"argv": ["id"], "env": {"A B": "x"}}, {"argv": "id"}, {"argv": []}):
        with pytest.raises(base.Refused):
            L.exec_stream("FAB-3", bad, out.append)
    with pytest.raises(base.Refused):
        L.exec_stream("FAB-9", {"argv": ["id"]}, out.append)               # pas de Pod : start d'abord


def test_stop_and_kill(L):
    L.start("FAB-5")
    assert L.kill("FAB-5") == {"killed": True} and L.calls[-1][2] == ["sh", "-c", "kill -9 -1"]
    assert L.stop("FAB-5") == {"stopped": True} and "fabrique-job-fab-5" not in L.core.pods
    assert L.stop("FAB-5") == {"stopped": True} and L.kill("FAB-5") == {"killed": True}    # already sent: without error


def test_a_pod_that_cannot_pull_its_image_fails_with_the_reason(L):
    def create(ns, body):
        L.core.pods[body["metadata"]["name"]] = NS(
            metadata=NS(name=body["metadata"]["name"], labels=body["metadata"]["labels"]), spec=NS(containers=[NS(image="reg/job-python:1")]),
            status=NS(phase="Pending", container_statuses=[NS(state=NS(waiting=NS(reason="ImagePullBackOff", message="pull access denied")))]))
    L.core.create_namespaced_pod = create
    with pytest.raises(base.Refused) as e:
        L.start("FAB-8")
    assert "ImagePullBackOff" in str(e.value) and "pull access denied" in str(e.value)


def test_memory_notation():
    assert kube.memory("4g") == "4Gi" and kube.memory("512m") == "512Mi" and kube.memory("2Gi") == "2Gi"


def test_orphan_pods_are_reaped_but_only_job_pods(L):
    import datetime as dt
    now = 1_800_000_000
    def pod(name, labels, phase, age):
        return NS(metadata=NS(name=name, labels=labels, creation_timestamp=dt.datetime.fromtimestamp(now - age, dt.timezone.utc)),
                  spec=NS(containers=[NS(image="x")]), status=NS(phase=phase, container_statuses=[]))
    job = lambda ref: {"fabrique.io/job": ref, "fabrique.io/role": "job"}
    L.core.pods = {"fabrique-job-fab-1": pod("fabrique-job-fab-1", job("FAB-1"), "Running", 600),          # job in progress: kept
                   "fabrique-job-fab-2": pod("fabrique-job-fab-2", job("FAB-2"), "Running", 25 * 3600),    # forgotten for a day
                   "fabrique-job-fab-3": pod("fabrique-job-fab-3", job("FAB-3"), "Failed", 60),            # finished
                   "autre": pod("autre", {"app": "autre"}, "Failed", 99 * 3600)}                           # not ours
    assert sorted(L.reap(now=now)) == ["fabrique-job-fab-2", "fabrique-job-fab-3"]
    assert sorted(L.core.pods) == ["autre", "fabrique-job-fab-1"]


def test_net_reads_the_proxy_pod_log_for_the_job_ip(L, monkeypatch):
    """On Kubernetes: Pod IP and start time of the job Pod, proxy Pod log (pods/log permission)."""
    import datetime as dt
    from launcher import kube, main as base
    L.start("FAB-7")
    pod = L.core.pods[base.name_of("FAB-7")]
    pod.status.pod_ip, pod.status.start_time = "10.42.0.9", dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
    assert L.net("FAB-7") == {"known": False}                                     # no proxy Pod found
    L.core.pods["fabrique-proxy-x"] = NS(metadata=NS(name="fabrique-proxy-x", labels={kube.PROXY_SELECTOR: "1"}), status=NS(phase="Running"))
    asked = {}

    def read_log(name, ns, since_seconds=0):
        asked.update(name=name, since=since_seconds)
        return ('CONNECT x [1]: Connect (file descriptor 4): 10.42.0.9\nCONNECT x [1]: Request (file descriptor 4): CONNECT registry.npmjs.org:443 HTTP/1.1\n'
                'CONNECT x [1]: Connect (file descriptor 5): 10.42.0.9\nCONNECT x [1]: Request (file descriptor 5): CONNECT evil.test:443 HTTP/1.1\n'
                'NOTICE x [1]: Proxying refused on filtered domain "evil.test"\n')
    L.core.read_namespaced_pod_log = read_log
    r = L.net("FAB-7")
    assert r["allowed"] == [["registry.npmjs.org", 1]] and r["denied"] == [["evil.test", 1]]
    assert asked["name"] == "fabrique-proxy-x" and 60 <= asked["since"] <= 200


def test_chart_gives_the_launcher_read_access_to_pod_logs():
    import os
    y = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "helm", "fabrique", "templates", "launcher.yaml")).read()
    assert "resources: [pods/log]" in y and "PROXY_SELECTOR" in y
