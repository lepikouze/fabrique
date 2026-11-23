"""Security check (runner/security_check.py): missing or just published packages, hardcoded secrets, hook and report.
Registries are simulated; Betterleaks is replaced by a fake executable when a precise result is needed."""
import io, json, os, subprocess, sys, time, urllib.error
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "runner"))
import security_check  # noqa: E402

DAY = 86400
NOW = time.time()


def iso(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - days_ago * DAY))


def registry(known):
    """Fake registry: known = {name: age in days}. Any other name returns 404."""
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
    assert security_check.packages("requirements.txt", req) == {"requests", "django-rest-framework"}
    toml = '[project]\ndependencies = ["httpx>=0.28", "pkg @ https://x/y.whl"]\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    assert security_check.packages("pyproject.toml", toml) == {"httpx", "pytest"}
    pkg = json.dumps({"dependencies": {"left-pad": "^1", "@scope/Lib": "2", "loc": "file:../loc"}, "devDependencies": {"vitest": "1"}})
    assert security_check.packages("web/package.json", pkg) == {"left-pad", "@scope/lib", "vitest"}
    gomod = "module x\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n\tgolang.org/x/sync v0.1.0 // indirect\n)\nrequire example.com/a v1.2.3\n"
    assert security_check.packages("go.mod", gomod) == {"github.com/pkg/errors", "golang.org/x/sync", "example.com/a"}
    assert security_check.ecosystem("src/main.py") is None and security_check.packages("package.json", "{pas du json") == set()


def test_only_added_packages_are_checked_and_invented_or_young_ones_flagged():
    get = registry({"requests": 4000, "flask": 5000, "fraichement-publie": 3})
    old = "requests==2\n"
    new = "requests==2.32\nrequessts\nflask\nfraichement-publie\n"
    alerts, notes, n = security_check.check_packages([("requirements.txt", old, new)], get=get)
    assert n == 3                                                   # requests already existed: not re-reviewed
    assert any("“requessts” cannot be found on PyPI" in a for a in alerts)
    assert any("“fraichement-publie”" in a and "for only 3 day" in a for a in alerts)
    assert not any("flask" in a for a in alerts) and not notes


def test_unreachable_registry_never_blocks():
    def down(url):
        raise urllib.error.URLError("proxy")
    alerts, notes, n = security_check.check_packages([("package.json", "{}", json.dumps({"dependencies": {"x": "1"}}))], get=down)
    assert alerts == [] and n == 1 and "not checked (registry unreachable)" in notes[0]


def test_package_limit(monkeypatch):
    monkeypatch.setattr(security_check, "MAX_PACKAGES", 2)
    alerts, notes, n = security_check.check_packages([("requirements.txt", "", "a\nb\nc\n")], get=registry({"a": 999, "b": 999, "c": 999}))
    assert n == 2 and "limit of 2" in notes[0]


@pytest.fixture
def fake_betterleaks(tmp_path, monkeypatch):
    """Fake Betterleaks: returns the detections written in FAKE_BL (JSON), like the real one with -f json -r -."""
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
    assert security_check.secrets(["dir", "."], str(tmp_path)) == [("app/config.py", 2, "generic-password"), ("tests/test_api.py", 9, "github-pat")]


def test_missing_betterleaks_is_reported_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert security_check.secrets(["dir", "."], str(tmp_path)) is None


def _hook(ws, tool, path, get=None):
    ev = {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(ws)}
    err = io.StringIO()
    old, sys.stderr = sys.stderr, err
    try:
        code = security_check.hook(io.StringIO(json.dumps(ev)), get=get or registry({}))
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
    assert code == 2 and "app.py line 1: hard-coded secret (rule stripe-access-token)" in msg and "NE-DOIT-PAS-SORTIR" not in msg
    fake_betterleaks.write_text("[]")
    code, msg = _hook(ws, "Edit", str(ws / "requirements.txt"))
    assert code == 2 and "“requessts” cannot be found on PyPI" in msg
    assert _hook(ws, "Read", "app.py")[0] == 0                       # reading: nothing to check
    assert _hook(ws, "Write", "/etc/passwd")[0] == 0                  # outside the workspace: not its role (guard.py refuses)
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
    assert security_check.diff(base, str(ws), get=registry({"requests": 3000})) == 0
    out = capsys.readouterr().out
    assert out.startswith("Security check: 2 alert(s)\n") and out.rstrip().endswith("\nfabrique-alerts: 2") and "app.py line 4 (rule aws-access-token)" in out
    assert "“requessts” cannot be found" in out and "NE-DOIT-PAS-SORTIR" not in out and "does not block" in out
    fake_betterleaks.write_text("[]")
    security_check.diff(base, str(ws), get=registry({"requests": 3000, "requessts": 3000}))
    out = capsys.readouterr().out
    assert out.startswith("Security check: nothing to report\n") and out.rstrip().endswith("\nfabrique-alerts: 0")
    security_check.diff("; rm -rf /", str(ws))
    assert "invalid base" in capsys.readouterr().out


def test_hook_is_installed_as_managed_setting_and_in_the_job_image():
    ms = json.load(open(os.path.join(HERE, "..", "runner", "managed-settings.json")))
    post = ms["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "Write|Edit|MultiEdit" and post["hooks"][0]["command"] == "python3 /opt/fabrique/runner/security_check.py hook"
    assert ms["attribution"] == {"commit": "Co-Authored-By: Fabrique <fabrique@fabrique.invalid>", "pr": ""}   # no mention of the tool
    df = open(os.path.join(HERE, "..", "Dockerfile.job")).read()
    assert "runner/security_check.py" in df and "sha256sum -c" in df and "--validation" not in df


def test_security_warning_shown_to_the_reviewer():
    from app import jobs
    warn = {"name": "Security", "kind": "security", "warn": True, "sha": "abc1234", "ok": True, "output": "Security check: 1 alert(s)\nHard-coded secrets:"}
    calm = dict(warn, warn=False, output="Security check: nothing to report\n")
    worded = dict(warn, warn=False)       # the wording alone decides nothing: only what the runner reported counts
    j = {"verified_sha": "abc1234", "evidence": [warn], "decision": {"limits": []}}
    assert jobs.security_warn(warn) and not jobs.security_warn(calm) and not jobs.security_warn(worded)
    assert not jobs.security_warn(dict(warn, kind="check"))             # a recipe check is never an informative alert
    assert jobs.to_check(j)[0]["title"] == "Security check: 1 alert(s)"
    assert jobs.to_check(dict(j, verified_sha="def5678")) == []          # alert from an old commit: no longer displayed
