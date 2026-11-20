# Development

## Layout

| Folder | Contents |
|---|---|
| `app/` | Dashboard: FastAPI, Jinja templates, SQLAlchemy Core, Alembic migrations, live refresh over SSE |
| `app/work.py` | Runners' order queue, events, gates |
| `app/jobs.py`, `app/auth.py` | Jobs and repos; OIDC sign-in and roles |
| `app/providers.py`, `app/notify.py` | Model providers (encrypted keys); notifications |
| `runner/main.py` | Runner loop and the steps of a job |
| `runner/agent.py`, `runner/review.py` | Starting the agents (Claude Code, Codex); reviewers |
| `runner/relay.py`, `runner/gateway.py` | `fab` relay; key gateway |
| `runner/forge.py` | Forgejo, GitLab and GitHub clients |
| `runner/playbook.md`, `runner/onboarding.md`, `runner/review.md` | Agent instructions |
| `runner/garde.py`, `runner/managed-settings.json` | Hook and settings imposed on Claude Code in the job |
| `runner/controle.py` | Security check: hard-coded secrets and missing packages (hook after each write, and end-of-job report) |
| `lanceur/` | Launcher of job containers |
| `proxy/` | Egress proxy (tinyproxy) |
| `tests/` | Tests; `tests/fake/` contains fake agents |
| `deploy/` | Example Compose files |

## Running and testing

```sh
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt
FABRIQUE_DATA=./data FABRIQUE_DEMO=1 FABRIQUE_OPEN=1 .venv/bin/uvicorn app.main:app --port 8080 --reload
.venv/bin/python -m pytest -q tests
```

The tests need no network, no Docker and no model: the Git host, the OIDC provider, the models and the SMTP server are
simulated, and a full cycle (request, question, checks, review, PR, merge) runs with a fake agent and a local Git
repo. `tests/test_audit.py` keeps one regression test per flaw fixed after an audit, and `tests/test_playbook.py`
checks that the agent instructions keep their essential rules.

## Database

SQLAlchemy Core, with no engine-specific SQL. Alembic migrations (`app/migrations`) are applied at start-up. Creating
a migration: `alembic revision --autogenerate -m "…"`. A test checks that the migrations and the schema match.

## Images and continuous integration

The `.forgejo/workflows/ci.yml` workflow runs the tests, then builds and pushes four families of images, tagged
`sha-<8 characters of the commit>`:

| Image | File | Used for |
|---|---|---|
| `fabrique` | `Dockerfile` | the dashboard |
| `fabrique-runner` | `Dockerfile.runner` | the runner and the launcher |
| `fabrique-job-{python,node,go,java}` | `Dockerfile.job` | job containers (agents included), tagged by content hash |
| `fabrique-proxy` | `Dockerfile.proxy` | the egress proxy |

## Conventions

- The code, the comments and the interface are in French for now; the documentation is in English.
- The runner never writes into a job's workspace itself, and never runs Git there with a token: see
  [security.md](security.md).
- Every fix to a security behaviour comes with a test that fails without it.
