"""0.8.9: a tolerant onboarding — what can be repaired is repaired, a long check is allowed or written as a list, and a refused
recipe goes back to the agent with the exact reason instead of failing the job."""
import json
import pytest
from runner import main as rmain, messages

REAL = {  # shaped like a real recipe refused by 0.8.8: a startup check of more than 500 characters
    "image": "python",
    "setup": ["python -m venv .venv", ".venv/bin/pip install -q -e \".[dev]\""],
    "checks": {
        "lint": ".venv/bin/ruff check src tests tools",
        "conformance": ".venv/bin/python tools/conformance.py",
        "tests": "APP_ENCRYPTION_KEY=test-only APP_SECRET=throwaway .venv/bin/python manage.py makemigrations --check --dry-run && .venv/bin/pytest -q",
        "startup": "export APP_ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789ab= APP_SECRET=throwaway-for-the-smoke-test APP_ENV=ci "
                   "APP_DEBUG=0 APP_HOSTS='*' APP_DATABASE=/tmp/smoke.sqlite3 && .venv/bin/python manage.py migrate --noinput && .venv/bin/python manage.py "
                   "collectstatic --noinput --clear && .venv/bin/python manage.py tick --once && PYTHONPATH=src .venv/bin/gunicorn config.wsgi:application "
                   "--bind 127.0.0.1:8000 --workers 1 --daemon --pid /tmp/smoke.pid && sleep 3 && .venv/bin/python tools/smoke.py http://127.0.0.1:8000"},
}


def test_a_long_real_recipe_now_passes():
    assert len(REAL["checks"]["startup"]) > 500
    rec, notes = rmain.normalize_recipe(REAL)
    assert rec["checks"]["startup"] == REAL["checks"]["startup"] and notes == []


def test_a_check_as_a_list_is_chained_in_one_shell():
    rec, _ = rmain.normalize_recipe({"checks": {"Startup!": ["export A=1", "python migrate.py", "python smoke.py"]}})
    assert rec["checks"] == {"startup": "{ export A=1; } && { python migrate.py; } && { python smoke.py; }"}


def test_names_are_repaired_without_collision():
    rec, notes = rmain.normalize_recipe({"checks": {"Tests": "a", "tests": "b", "2e passe": "c"}})
    assert list(rec["checks"]) == ["tests", "tests-2", "c-2e-passe"] and len(notes) == 3


def test_the_reason_names_the_check_and_the_limit():
    with pytest.raises(ValueError, match="check “startup”: a command of 2500 characters, 2000 at most"):
        rmain.normalize_recipe({"checks": {"startup": "x" * 2500}})


def test_a_refused_recipe_message_tells_the_agent_how_to_fix_it():
    m = messages.fix_recipe("check “startup”: a command of 2500 characters, 2000 at most")
    assert "2500 characters" in m and "list of commands" in m and "script that already exists" in m and "fab decision" in m


def test_the_onboarding_instructions_state_the_limits():
    text = open(rmain.__file__.replace("main.py", "onboarding.md"), encoding="utf-8").read()
    assert "2000 characters" in text and "Reuse what the repository already has" in text


def test_a_refused_creation_says_which_step_and_the_hosts_reason():
    import httpx
    from runner import forge
    gl = lambda h: forge.GitLab(base="https://gl.example", token="t", transport=httpx.MockTransport(h))
    def member_but_developer(req):
        if req.url.path == "/api/v4/namespaces/fabrique":
            return httpx.Response(200, json={"id": 7})
        return httpx.Response(403, json={"message": "403 Forbidden"})
    with pytest.raises(forge.CreateRefused, match=r"GitLab refuses \(403\).*Maintainer.*403 Forbidden"):
        gl(member_but_developer).create_repo("fabrique", "new-repo")
    with pytest.raises(forge.CreateRefused, match=r"cannot find the group “fabrique”.*\(404\)"):
        gl(lambda req: httpx.Response(404, json={"message": "404 Namespace Not Found"})).create_repo("fabrique", "x")
    fj = forge.Forgejo(base="https://fj.example", token="t", transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "token does not have scope"})))
    with pytest.raises(forge.CreateRefused, match="write:organization.*token does not have scope"):
        fj.create_repo("org", "x")
