"""Contrôle de sécurité (runner/controle.py) : paquets introuvables ou tout juste publiés, secrets en dur, hook et rapport.
Les registres sont simulés ; Betterleaks est remplacé par un faux exécutable quand il faut un résultat précis."""
import io, json, os, subprocess, sys, time, urllib.error
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "runner"))
import controle  # noqa: E402

DAY = 86400
NOW = time.time()


def iso(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - days_ago * DAY))


def registry(known):
    """Faux registre : known = {nom: âge en jours}. Tout autre nom renvoie 404."""
    def get(url):
        for name, age in known.items():
            if url.rstrip("/").endswith("/" + name + "/json") or url.endswith("/" + name) or f"/{name}/@latest" in url:
                if "pypi.org" in url:
                    return {"releases": {"1.0": [{"upload_time_iso_8601": iso(age) + ".000Z"}]}}
                if "npmjs" in url:
                    return {"time": {"created": iso(age) + ".000Z"}}
                return {"Version": "v1.0.0"}
        raise urllib.error.HTTPError(url, 404, "absent", {}, None)
    return get


def test_packages_parsed_from_each_manifest():
    req = "requests>=2\n# commentaire\n-r autre.txt\nDjango_Rest.Framework[x]==3 ; python_version>'3'\ngit+https://x/y.git\n./local\n"
    assert controle.packages("requirements.txt", req) == {"requests", "django-rest-framework"}
    toml = '[project]\ndependencies = ["httpx>=0.28", "pkg @ https://x/y.whl"]\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    assert controle.packages("pyproject.toml", toml) == {"httpx", "pytest"}
    pkg = json.dumps({"dependencies": {"left-pad": "^1", "@scope/Lib": "2", "loc": "file:../loc"}, "devDependencies": {"vitest": "1"}})
    assert controle.packages("web/package.json", pkg) == {"left-pad", "@scope/lib", "vitest"}
    gomod = "module x\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n\tgolang.org/x/sync v0.1.0 // indirect\n)\nrequire example.com/a v1.2.3\n"
    assert controle.packages("go.mod", gomod) == {"github.com/pkg/errors", "golang.org/x/sync", "example.com/a"}
    assert controle.ecosystem("src/main.py") is None and controle.packages("package.json", "{pas du json") == set()


def test_only_added_packages_are_checked_and_invented_or_young_ones_flagged():
    get = registry({"requests": 4000, "flask": 5000, "fraichement-publie": 3})
    old = "requests==2\n"
    new = "requests==2.32\nrequessts\nflask\nfraichement-publie\n"
    alerts, notes, n = controle.check_packages([("requirements.txt", old, new)], get=get)
    assert n == 3                                                   # requests existait déjà : pas revérifié
    assert any("« requessts » est introuvable sur PyPI" in a for a in alerts)
    assert any("« fraichement-publie »" in a and "depuis 3 jour" in a for a in alerts)
    assert not any("flask" in a for a in alerts) and not notes


def test_unreachable_registry_never_blocks():
    def down(url):
        raise urllib.error.URLError("proxy")
    alerts, notes, n = controle.check_packages([("package.json", "{}", json.dumps({"dependencies": {"x": "1"}}))], get=down)
    assert alerts == [] and n == 1 and "non vérifié (registre injoignable)" in notes[0]


def test_package_limit(monkeypatch):
    monkeypatch.setattr(controle, "MAX_PACKAGES", 2)
    alerts, notes, n = controle.check_packages([("requirements.txt", "", "a\nb\nc\n")], get=registry({"a": 999, "b": 999, "c": 999}))
    assert n == 2 and "limite de 2" in notes[0]


@pytest.fixture
def fake_betterleaks(tmp_path, monkeypatch):
    """Faux Betterleaks : renvoie les détections écrites dans FAKE_BL (JSON), comme le vrai avec -f json -r -."""
    exe = tmp_path / "bin" / "betterleaks"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\ncat \"$FAKE_BL\"\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}:{os.environ['PATH']}")
    out = tmp_path / "bl.json"
    monkeypatch.setenv("FAKE_BL", str(out))
    return out


def test_generic_rules_ignored_in_tests_but_real_tokens_kept(fake_betterleaks, tmp_path):
    fake_betterleaks.write_text(json.dumps([
        {"File": "tests/test_auth.py", "StartLine": 3, "RuleID": "generic-password", "Secret": "NE-DOIT-PAS-SORTIR"},
        {"File": "tests/test_api.py", "StartLine": 9, "RuleID": "github-pat", "Secret": "NE-DOIT-PAS-SORTIR"},
        {"File": "app/config.py", "StartLine": 2, "RuleID": "generic-password", "Secret": "NE-DOIT-PAS-SORTIR"}]))
    assert controle.secrets(["dir", "."], str(tmp_path)) == [("app/config.py", 2, "generic-password"), ("tests/test_api.py", 9, "github-pat")]


def test_missing_betterleaks_is_reported_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert controle.secrets(["dir", "."], str(tmp_path)) is None


def _hook(ws, tool, path, get=None):
    ev = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(ws)}
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        code = controle.hook(io.StringIO(json.dumps(ev)), get=get or registry({}))
    finally:
        sys.stderr = old
    return code, err.getvalue()


def test_hook_tells_the_agent_what_to_fix_without_the_secret(fake_betterleaks, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "app.py").write_text('KEY = "x"\n')
    (ws / "requirements.txt").write_text("requessts\n")
    monkeypatch.setenv("FAB_WORKSPACE", str(ws))
    monkeypatch.delenv("FAB_TOKEN", raising=False)
    fake_betterleaks.write_text(json.dumps([{"File": "app.py", "StartLine": 1, "RuleID": "stripe-access-token", "Secret": "NE-DOIT-PAS-SORTIR"}]))
    code, msg = _hook(ws, "Write", "app.py")
    assert code == 2 and "app.py ligne 1 : secret écrit en dur (règle stripe-access-token)" in msg and "NE-DOIT-PAS-SORTIR" not in msg
    fake_betterleaks.write_text("[]")
    code, msg = _hook(ws, "Edit", str(ws / "requirements.txt"))
    assert code == 2 and "« requessts » est introuvable sur PyPI" in msg
    assert _hook(ws, "Read", "app.py")[0] == 0                       # lecture : rien à contrôler
    assert _hook(ws, "Write", "/etc/passwd")[0] == 0                  # hors de l'espace de travail : pas son rôle (garde.py refuse)
    code, _ = _hook(ws, "Write", "requirements.txt", get=registry({"requessts": 900}))
    assert code == 0


def test_diff_report_first_line_and_never_blocks(fake_betterleaks, tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    ws.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True).stdout
    g("init", "-q")
    (ws / "requirements.txt").write_text("requests\n")
    g("add", "-A"); g("commit", "-qm", "base")
    base = g("rev-parse", "HEAD").strip()
    (ws / "requirements.txt").write_text("requests\nrequessts\n")
    g("add", "-A"); g("commit", "-qm", "job")
    fake_betterleaks.write_text(json.dumps([{"File": "app.py", "StartLine": 4, "RuleID": "aws-access-token", "Secret": "NE-DOIT-PAS-SORTIR"}]))
    assert controle.diff(base, str(ws), get=registry({"requests": 3000})) == 0
    out = capsys.readouterr().out
    assert out.startswith("Contrôle de sécurité : 2 alerte(s)\n") and "app.py ligne 4 (règle aws-access-token)" in out
    assert "« requessts » est introuvable" in out and "NE-DOIT-PAS-SORTIR" not in out and "ne bloque pas" in out
    fake_betterleaks.write_text("[]")
    controle.diff(base, str(ws), get=registry({"requests": 3000, "requessts": 3000}))
    assert capsys.readouterr().out.startswith("Contrôle de sécurité : rien à signaler\n")
    controle.diff("; rm -rf /", str(ws))
    assert "base invalide" in capsys.readouterr().out


def test_hook_is_installed_as_managed_setting_and_in_the_job_image():
    ms = json.load(open(os.path.join(HERE, "..", "runner", "managed-settings.json")))
    post = ms["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "Write|Edit|MultiEdit" and post["hooks"][0]["command"] == "python3 /opt/fabrique/runner/controle.py hook"
    assert ms["attribution"] == {"commit": "Co-Authored-By: Fabrique <fabrique@fabrique.invalid>", "pr": ""}   # pas de mention de l'outil
    df = open(os.path.join(HERE, "..", "Dockerfile.job")).read()
    assert "runner/controle.py" in df and "sha256sum -c" in df and "--validation" not in df


def test_security_warning_shown_to_the_reviewer():
    from app import jobs
    warn = {"name": "Sécurité", "sha": "abc1234", "ok": True, "output": "Contrôle de sécurité : 1 alerte(s)\nSecrets écrits en dur :"}
    calm = dict(warn, output="Contrôle de sécurité : rien à signaler\n")
    j = {"verified_sha": "abc1234", "evidence": [warn], "decision": {"limits": []}}
    assert jobs.security_warn(warn) and not jobs.security_warn(calm)
    assert jobs.to_check(j)[0]["title"] == "Contrôle de sécurité : 1 alerte(s)"
    assert jobs.to_check(dict(j, verified_sha="def5678")) == []          # alerte d'un ancien commit : plus affichée
