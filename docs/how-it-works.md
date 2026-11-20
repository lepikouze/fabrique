# How it works

Interface labels are quoted in French, as they appear on screen.

## Roles

| Role | Can |
|---|---|
| **Requester** | file a request, follow **their own** requests and answer their questions |
| **Reviewer** | see everything, answer, approve or refuse a merge, stop and archive a job, ask for a repo to be created |
| **Admin** | configure Fabrique: repos, runners, models, sign-in, budgets, notifications, roles |

The role comes from a choice made in the dashboard, otherwise from OIDC groups, otherwise from the default role for
new accounts.

## Life cycle of a job

1. **Request.** A title, a need, a "done when" criterion, a repo.
2. **Pick-up.** A runner takes the order from the dashboard's queue (with a lease: if the runner disappears, the order
   is taken again). It prepares a fresh workspace and a disposable container.
3. **Triage, investigation, implementation.** The agent follows its instructions (`runner/playbook.md`) and announces
   each step with the `fab` command. If it lacks a piece of information it cannot verify by itself, it asks **one
   question**: the job waits, the requester answers in the inbox, the agent resumes.
4. **Checks.** The runner — not the agent — reruns the repo's recipe on the head commit. On failure, the agent gets a
   limited number of new attempts.
5. **Review.** Read-only reviewers read the diff: code and security always, architecture and infrastructure depending
   on the files touched. A blocking verdict must come with an evidence command, which the runner replays; without
   evidence that holds, it is downgraded to "to be looked at". A proven blocker earns one round of fixes.
6. **Pull request.** The runner pushes the branch and opens the PR, with the evidence and the agent's opinion.
7. **Decision.** The dashboard evaluates the gates and applies the repo's policy.
8. **Merge.** Pinned to the verified commit: if the PR has moved since, the merge is refused.

## The `fab` command

It is the agent's only link to the dashboard, through the runner's relay and with a token specific to the job:
`fab etape`, `fab question`, `fab decision`, `fab note`, `fab echec`.

## A repo's recipe

The `.fabrique/setup.json` file, on the main branch, says how to install the repo and run its checks:

```json
{
  "image": "python",
  "setup": ["python -m venv .venv", ".venv/bin/pip install -q -r requirements.txt"],
  "checks": { "tests": ".venv/bin/python -m pytest -q", "lint": ".venv/bin/ruff check ." },
  "env": [{ "name": "DATABASE_URL", "purpose": "integration test database", "required": true }]
}
```

- `image`: `python`, `node`, `go` or `java` — a name from the launcher's allowlist, never an arbitrary image.
- `setup` and `checks`: commands run in the job's container.
- `env`: names and purposes, never a value.

The recipe is always read from the main branch, never from the agent's branch, and `.fabrique/**` is a protected path:
changing it goes through a human.

## Onboarding

Adding a repo starts an onboarding job: an agent reads the repo (README, CI, manifests), writes the recipe and proves
it. The runner keeps only the recipe, replays it in a fresh container, then opens a PR that **only a human** can merge.
The repo accepts requests only after that. If the bot account has no access to the repo, or if the repo is empty, the
job waits and retries by itself.

## Gates and merge policy

| Gate | Holds if |
|---|---|
| Green checks | every check in the recipe passes, rerun by the runner on the PR's commit |
| Review | code and security have answered, with no blocking finding |
| First attempt | no new attempt was needed |
| Protected paths | no protected file is touched (migrations, authentication, CI, `Dockerfile`, `.env*`, recipe…) |
| Size | the diff stays under the repo's limit (300 lines by default) |
| Agent's opinion | the agent says it is confident |

| Repo policy | Effect |
|---|---|
| **Désactivée** (off, the default) | every PR waits for a human decision |
| **Essai** (trial) | Fabrique evaluates and shows "would have merged", without merging |
| **Merge si confiant** (merge if confident) | automatic merge if **all** gates hold; otherwise, human decision |

A job stopped by a human, or an onboarding, is never merged automatically.

## Studies

In **Nouvelle demande** (new request), the « Étude » type asks for an analysis instead of a change: understanding a
behaviour, estimating a piece of work, comparing options. The agent works in the same disposable container as an
ordinary job and can run the repo's tests, but the runner captures no commit, pushes nothing and opens no PR: whatever
the agent writes in its workspace stays there. Its only deliverable is a report (`RAPPORT_ETUDE.md`), which the runner
reads and the dashboard shows as plain text in the job, at the « Étude rendue » step.

A study follows the same rules as a job: budget, watchdog, question to the requester, stop. There are no checks, no
review and no gates, since there is nothing to merge. The « En faire une demande » button opens the form prefilled with
the original request and the report's conclusions, to be reread before sending.

## Archives and deletion

A finished job (merged, failed, PR refused, study delivered) is archived from its page: it leaves the Jobs page and
keeps all its content. The **Archives** page (« Archivés » button on the Jobs page) lists archived jobs, 50 per page,
with:

- a **search** that filters as you type, in the reference, the title, the text of the request, the requester and the
  repo. Every word typed must appear; the « Expression régulière » box accepts a pattern (`^FAB-1[0-9]$` targets the
  reference, `export|import` looks for either);
- **filters** by repo and by outcome;
- **Restaurer** (restore), for one job or for a selection.

**Automatic archiving** (Settings → Budgets et barrières, off by default): merged jobs and studies already read are
archived N days after they end. Failures and refused PRs stay visible.

**Permanent deletion**, for admins only, from the archives only: you have to type `repo/FAB-3` (or « supprimer N
jobs » for a selection) to enable the button. The job and all its content disappear; the audit keeps a record of the
deletion and the job's earlier lines; nothing is touched on the Git host. A deleted job's number is never reused.

The runner removes by itself the working folder of a job that finished more than an hour ago, or was deleted.

## Network egress of a job

Each job only goes out through the allowlisted proxy. At the end of the job, the launcher reads that proxy's log and
extracts what **this job** requested since it started: the domains reached, and those the allowlist refused. The result
appears in the evidence (« Réseau » line under « Preuves ») and, if anything was refused, under « À vérifier avant
d'approuver ».

A refusal can be read two ways: a legitimate dependency to add to the allowlist (`PROXY_ALLOW`), or an agent that
tried to reach a site it should not, for example after reading booby-trapped content. This check **does not block**: it
leaves a trace and informs the reviewer. Only domain names are kept, never full addresses.

## Model profiles

**Settings → Agents et modèles → Profils de modèles.** A profile, identified by its name alone, gives a model to each
role:

| Role | What it covers |
|---|---|
| Agent | triage, investigation, implementation and fixes, in a single session |
| Reviewers | the code, security, architecture and infrastructure reviews |
| Studies | study reports |
| Onboarding | reading a repo and writing its recipe |

Each role names its model. The effort level of a local model (vLLM, llama.cpp) is set once, on its provider: it
applies to every role that uses that model.

A single list, **« Modèle ou profil »**, is used in the global settings, in a repo's settings and in the request
(admins): one model for everything, or a profile, never both. The first level that made a choice wins: the request,
then the repo, then the global setting. Without a profile, a single model is used for everything, as before. What is
chosen is frozen when the job is created.

**No loss of context**: the agent keeps a single model for its whole session. The reviewers always start from a fresh
context (the diff and the request): another model gives them a second pair of eyes. The key gateway relays each
request to the provider of the model asked for, among the job's models only.

## Budgets and safeguards

- **Budget per job**: the agent is stopped when it is reached. Spending is measured by the key gateway, on the
  provider's responses; the cost of the reviewers counts too.
- **Budget per day**: beyond it, new jobs wait; merges already decided go through. It is also checked while a job is
  running, at each runner heartbeat (20 s): the agent at work is **paused**. Nothing is lost — the workspace (files,
  commits) and the agent's session are kept — and the job resumes by itself where it left off, the next day or as soon
  as the budget is raised. An onboarding interrupted this way fails and is restarted from the repo.
- **Watchdog**: an agent silent for too long is restarted once, then the job fails.
- **Stop**: a reviewer can stop a job at any time.

## Security check

Two lightweight checks, with no AI model, run by `runner/controle.py` in the job's container:

- **Hard-coded secrets**, with [Betterleaks](https://github.com/betterleaks/betterleaks). Validating secrets against
  the services concerned stays disabled: nothing leaves the container. In test folders and files, generic rules (dummy
  passwords) are ignored; a recognised real token is still reported everywhere.
- **Packages that do not exist or were only just published**: every package **added** to `requirements*.txt`,
  `pyproject.toml`, `package.json` or `go.mod` is looked up on its public registry (PyPI, npm, Go proxy), through the
  job's egress proxy. Not found: a misspelt name, or one made up by the model. Published less than 30 days ago:
  possibly a trap package imitating a well-known name. An unreachable registry blocks nothing: the package is noted
  as "not verified".

**On write** (Claude Code): a hook run after each file written by the agent (about 0.1 s). If it finds a problem, the
agent receives the message and fixes it straight away; a « contrôle » line is added to the job's activity. Codex has
no equivalent hook: for it, only the end-of-job check applies.

**At the end of the job**: the runner reruns the check on all the job's commits. The result appears in the evidence
(« Sécurité » line) and, if there are alerts, under « À vérifier avant d'approuver ». This check **does not block**: it
informs the reviewer.

## Notifications

**Settings → Notifications.** A channel has a type, events, and write-only secrets (encrypted, never shown again).
Changing a channel's Jira address or SMTP server requires entering its secret again. Events: question asked, PR ready
for review, job failed, merge done, study delivered, repo to create, repo waiting.

| Type | Delivery |
|---|---|
| Webhook | JSON `POST`; with a signing secret, header `X-Fabrique-Signature: sha256=<HMAC-SHA256 of the body>` |
| Microsoft Teams | adaptive card to a Workflows webhook |
| Jira | comment on the ticket named in the request's **title** (`PROJ-12`); otherwise a ticket is created if the option is ticked |
| E-mail | SMTP with STARTTLS (587), SSL/TLS (465) or unencrypted (25) |

Deliveries go through a queue: a channel that is down never blocks a job, and delivery is retried after 30 s, 2 min,
10 min then 30 min. « Envoyer un test » checks a channel; « Derniers envois » shows the state of the queue. Text coming
from the agent is made inert for each tool (no link, mention or image).
