# Development

## Layout

| Folder | Contents |
|---|---|
| `app/` | Dashboard: FastAPI, Jinja templates, SQLAlchemy Core, Alembic migrations, live refresh over SSE |
| `app/work.py` | Runners' order queue, events, gates |
| `app/jobs.py`, `app/auth.py` | Jobs and repos; OIDC sign-in and roles |
| `app/diffview.py`, `app/pdf.py` | Splitting a job's diff by file for display; study report as PDF (a small Markdown converter on reportlab) |
| `app/providers.py`, `app/notify.py` | Model providers (encrypted keys); notifications |
| `runner/main.py` | Runner loop and the steps of a job |
| `runner/agent.py`, `runner/review.py` | Starting the agents (Claude Code, Codex); reviewers |
| `runner/relay.py`, `runner/gateway.py` | `fab` relay; key gateway |
| `runner/forge.py` | Forgejo, GitLab and GitHub clients |
| `runner/playbook.md`, `runner/onboarding.md`, `runner/review.md` | Agent instructions |
| `runner/guard.py`, `runner/managed-settings.json` | Hook and settings imposed on Claude Code in the job |
| `runner/security_check.py` | Security check: hard-coded secrets and missing packages (hook after each write, and end-of-job report) |
| `launcher/` | Launcher of job containers |
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

**Reads per page.** Inside a page request (`GET`), `db.cached(key, load)` keeps the result of a read for the duration
of that request: the list of jobs, the repos, the runners and the settings are read once, however many parts of the page
use them. Nothing is cached between requests, nor for requests that write. A test bounds the number of queries of the
main pages (`tests/test_diff_pdf_perf.py`): if it fails, a read is being repeated.

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

- Live updates: `tests/test_live.py` covers the server side and the rules of the page; `tools/live_check.py` measures
  them in a real browser (Playwright) against a running instance, to run after any change to the live code in `app.js`.
- Everything is written in English: code, comments, names, logs, documentation. French lives only in the French
  catalogues and in `README.fr.md` (see "Languages in the code" below); `tests/test_english.py` fails when French comes
  back anywhere else (test data may be French on purpose: what a user types, what an agent answers).
- No decision reads a displayed text: a gate has a `key`, an informative evidence line a `kind` and a `warn` flag.
- The runner never writes into a job's workspace itself, and never runs Git there with a token: see
  [security.md](security.md).
- Every fix to a security behaviour comes with a test that fails without it.

## Languages in the code

English is the source language: texts are written in English in the templates and in the code, and each reader sees
them in their own language. There is one catalogue per language, `app/locales/<lang>.json`, mapping each English text
to its translation; `en.json` only holds formats (dates, numbers) and the list of texts used by `app.js`.

- **Fixed text of a template** is found at compile time by a Jinja extension (`app/i18n.py`) and looked up in the
  catalogue. Nothing to mark: write the English text. Inline markup (`<b>`, `<code>`, a link) stays inside the unit,
  and so does a simple `{{ value }}` (a name, an attribute, a filter): `Follow-up to the study {{ pre.ref }}` is the
  unit `Follow-up to the study {ref}`, the value being named after its expression, so a language can order the words
  and the values its own way. A call (`{{ ic('plus') }}`) or a block (`{% if %}`) ends a unit. To build a sentence
  inside an expression, call the same function yourself: `_tf('runner {name}', 'page.html', runner.name)`; never glue
  pieces with `~`. A translation can be made specific to one template with the key `page.html::English text`.
- **Text worded by the code** (status, log line, error message, audit entry) is marked with
  `M("{ref} archived.", ref=ref)`: the English sentence with **named values**. `M` returns a `Msg`, a string that
  keeps its template and its values. Joining two of them (`M(…) + " " + M(…)`) keeps both; slicing, `join` or an
  f-string give a plain string. When the same English text needs two translations ("done" for a job, "done" for a
  study), name the context: `M("done", _ctx="job")` looks for `job::done` in the catalogue before `done`.
- **What Fabrique says of a job is stored as a fact, not as a sentence.** The text columns `jobs.status_text`,
  `jobs.summary`, `timeline.summary`, `events.message`, `audit.action`, `audit.detail`, `reviews.summary` (type
  `Worded` in `app/db.py`) and every JSON column store a `Msg` as its template plus its values, and give it back as a
  `Msg`. The runner sends its messages the same way (`runner/texts.py`, `pack_tree`), and the dashboard revives them
  on arrival. Nothing is frozen in a language: two readers see the same line each in their own. Command outputs,
  diffs and what a person or an agent wrote are data and are never translated here.
- A sentence that reached the database as plain text (cut, joined with `join`, or sent by something that does not know
  `Msg`) is still recognised when displayed: the catalogue matches it against its templates. Unknown text is shown as
  it is.
- **A count** uses `nb(n, "{n} job archived", "{n} jobs archived")` in a template; a catalogue value may give its
  forms by number: `{"one": "…", "other": "…"}`.
- **`app.js`** takes its texts from `T("…")`; the server gives it the translations of the current language.
- **Texts written outside Fabrique** (PR description, review guide, decision comment, notifications) use the job
  language, set for the application and overridable per repo, never the language of whoever is looking. The runner
  has its own small catalogue, `runner/locales/<lang>.json`, used through `W(ctx, "…")`.
- **Searching**: the archive search reads what people wrote (reference, title, request, requester, repo). A query on
  a stored fact must look for its English template (see the onboarding lookup in `app/work.py`).

`python tools/i18n_extract.py` lists the marked texts that have no translation; the test suite fails on the same
list, on any template text without a translation, on a translation whose values or markup differ from the source,
and on French left in a string literal (`tests/i18n_ignore.json` holds the few legitimate exceptions).
`tests/test_english.py` checks the whole repository, comments and documentation included, with a list of French-only
words (`tests/data/french_only_words.txt`).

To add a language: copy `app/locales/fr.json` and `runner/locales/fr.json` under the new code, translate the values,
and set `meta.name` (the language's own name, shown in the menu) and `meta.english_name` in the first one. The list on
offer is read from these files: no code to change, unless the language is missing from `LANGUAGES` in
`runner/messages.py` (its English name, as given to the agents).

The instructions given to the agents and reviewers (`runner/playbook.md`, `runner/onboarding.md`, `runner/study.md`,
`runner/review.md`, the messages in `runner/messages.py`, the guard hook and the relay's replies) are written in
English whatever the job language. `@LANGUAGE@` (or `{language}` in the review template) is replaced by the job
language: the model is told to write for people in that language. Changing these texts changes the models'
behaviour: try a change on real jobs.
