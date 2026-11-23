# How it works

Interface labels are quoted in French, as they appear on a French screen; the English interface shows their translation.

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
6. **Pull request.** The runner pushes the branch and opens the PR, with the evidence and the agent's opinion. It also
   sends the dashboard the diff of the verified commit, so that the reviewer can read it without leaving Fabrique.
7. **Decision.** The dashboard evaluates the gates and applies the repo's policy.
8. **Merge.** Pinned to the verified commit: if the PR has moved since, the merge is refused.

## Filing a request

A request that is too vague costs a job: the agent guesses, or asks a question long after its author has left. Fabrique
therefore challenges a change request when it is filed.

- **While typing**, with no model: a hint when the need is very short, and a reminder that the default criterion
  applies when “Done when” is empty.
- **On sending**, one short call to the model that would run the job reads the request against a fixed grid: what must
  change, where, and how to know it is done. If a point is missing, the form comes back with one to three questions.
  Answer them (the answers are added to the request under “Clarifications given when filing”) or send the request as
  it is. A clear request, a study, a request created through the API, or a model that does not answer within a few
  seconds: the request is filed at once.

The agent keeps its own right to ask a question while it works.

## The `fab` command

It is the agent's only link to the dashboard, through the runner's relay and with a token specific to the job:
`fab step`, `fab question`, `fab decision`, `fab note`, `fab fail`.

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
| **Disabled** (the default) | every PR waits for a human decision |
| **Trial** | Fabrique evaluates and shows "would have merged", without merging |
| **Merge if confident** | automatic merge if **all** gates hold; otherwise, human decision |

A job stopped by a human, or an onboarding, is never merged automatically.

## Reading the diff in Fabrique

The job page (side panel, full view and inbox) has a “Changes” section: the list of files with their added and
removed lines, and “Read the diff”, which shows the unified diff file by file. Files fold and unfold; a file on a
protected path carries a “protected” label; a binary file is named but not shown.

- The dashboard never reads the repo. The **runner** computes the diff in its own mirror, between the pinned base and
  the verified commit, and sends it with the pull request (and again when a review is rerun).
- The diff shown is always the one of the **verified commit**: after a new commit, the previous diff is hidden until
  the runner sends the new one.
- It is **capped**: 400,000 characters sent by the runner (`FABRIQUE_DIFF_MAX`), 500,000 kept by the dashboard. Beyond
  that the diff is cut and marked “truncated”; the rest is read on the Git host. The gates never rely on this text:
  they use the list of files and the line counts, as before.
- It is read with the **same rights as the job**, and it is deleted with the job.
- A **confidential repo** can opt out: untick “Show the diff in Fabrique” in the repo's settings. Its code is
  then never copied into Fabrique's database: the runner sends nothing, the dashboard stores nothing, and what was
  already stored for that repo is deleted. The job page links to the pull request instead.
- `FABRIQUE_DIFF_MAX=0` on a runner turns the feature off for all the jobs of that runner: no diff leaves it. The links
  to the pull request stay.

## Studies

In **New request**, the “Study” type asks for an analysis instead of a change: understanding a
behaviour, estimating a piece of work, comparing options. The agent works in the same disposable container as an
ordinary job and can run the repo's tests, but the runner captures no commit, pushes nothing and opens no PR: whatever
the agent writes in its workspace stays there. Its only deliverable is a report (`RAPPORT_ETUDE.md`), which the runner
reads and the dashboard shows as plain text in the job, at the “Study delivered” step. The report can be downloaded
as **PDF** (Markdown rendered: headings, lists, tables, code blocks; pages numbered) or as the original Markdown, from
the job and from the inbox. Either download can be turned off in Settings → Budgets and gates (“Study reports”): the button
disappears and the address answers 404; the report stays readable in Fabrique.

A study follows the same rules as a job: budget, watchdog, question to the requester, stop. There are no checks, no
review and no gates, since there is nothing to merge. The “Turn it into a request” button opens the form prefilled with
the original request and the report's conclusions, to be reread before sending.

## Archives and deletion

A finished job (merged, failed, PR refused, study delivered, closed without change) is archived from its page: it leaves the Jobs page and
keeps all its content. The **Archives** page (“Archived” button on the Jobs page) lists archived jobs, 50 per page,
with:

- a **search** that filters as you type, in the reference, the title, the text of the request, the requester and the
  repo. Every word typed must appear; the “Regular expression” box accepts a pattern (`^FAB-1[0-9]$` targets the
  reference, `export|import` looks for either);
- **filters** by repo and by outcome;
- **Restore**, for one job or for a selection.

**Automatic archiving** (Settings → Budgets and gates, off by default): merged jobs and studies already read are
archived N days after they end. Failures and refused PRs stay visible.

**Permanent deletion**, for admins only, from the archives only: you have to type `repo/FAB-3` (or “delete N jobs” for a selection) to enable the button. The job and all its content disappear; the audit keeps a record of the
deletion and the job's earlier lines; nothing is touched on the Git host. A deleted job's number is never reused.

The runner removes by itself the working folder of a job that finished more than an hour ago, or was deleted.

## Network egress of a job

Each job only goes out through the allowlisted proxy. At the end of the job, the launcher reads that proxy's log and
extracts what **this job** requested since it started: the domains reached, and those the allowlist refused. The result
appears in the evidence (“Network” line under “Evidence”) and, if anything was refused, under “To check before approving”.

A refusal can be read two ways: a legitimate dependency to add to the allowlist (`PROXY_ALLOW`), or an agent that
tried to reach a site it should not, for example after reading booby-trapped content. This check **does not block**: it
leaves a trace and informs the reviewer. Only domain names are kept, never full addresses.

## Model profiles

**Settings → Agents and models → Model profiles.** A profile, identified by its name alone, gives a model to each
role:

| Role | What it covers |
|---|---|
| Agent | triage, investigation, implementation and fixes, in a single session |
| Reviewers | the code, security, architecture and infrastructure reviews |
| Studies | study reports |
| Onboarding | reading a repo and writing its recipe |

Each role names its model. The effort level of a local model (vLLM, llama.cpp) is set once, on its provider: it
applies to every role that uses that model.

A single list, **“Model or profile”**, is used in the global settings, in a repo's settings and in the request
(admins): one model for everything, or a profile, never both. The first level that made a choice wins: the request,
then the repo, then the global setting. Without a profile, a single model is used for everything, as before. What is
chosen is frozen when the job is created.

**No loss of context**: the agent keeps a single model for its whole session. The reviewers always start from a fresh
context (the diff and the request): another model gives them a second pair of eyes. The key gateway relays each
request to the provider of the model asked for, among the job's models only.

## When the model is saturated

A local model server shared by several jobs, a provider that rate-limits, a model still loading: the agent's run ends
on an error that has nothing to do with the work. The runner then keeps the workspace and the session, waits (30 s,
1, 2, 4 then 8 minutes), and resumes the agent where it was. The job shows “Waiting for the model”, and its log says
why. After the last attempt the job fails with the reason and what to do. If the conversation itself is larger than
the model's context, waiting cannot help: the job fails at once and says so.

To keep a small local server from being saturated in the first place, give its model a number of **simultaneous
jobs** (Settings → Agents and models, in the list of the provider's models): the jobs beyond that number wait in the
queue, in order of arrival, while the runner serves the jobs of the other models. The count is on the job's agent
model; the reviewers of a profile are not counted. The size of a model's context is not a Fabrique setting: it is set
on the model server.

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

Two lightweight checks, with no AI model, run by `runner/security_check.py` in the job's container:

- **Hard-coded secrets**, with [Betterleaks](https://github.com/betterleaks/betterleaks). Validating secrets against
  the services concerned stays disabled: nothing leaves the container. In test folders and files, generic rules (dummy
  passwords) are ignored; a recognised real token is still reported everywhere.
- **Packages that do not exist or were only just published**: every package **added** to `requirements*.txt`,
  `pyproject.toml`, `package.json` or `go.mod` is looked up on its public registry (PyPI, npm, Go proxy), through the
  job's egress proxy. Not found: a misspelt name, or one made up by the model. Published less than 30 days ago:
  possibly a trap package imitating a well-known name. An unreachable registry blocks nothing: the package is noted
  as "not verified".

**On write** (Claude Code): a hook run after each file written by the agent (about 0.1 s). If it finds a problem, the
agent receives the message and fixes it straight away; a “check” line is added to the job's activity. Codex has
no equivalent hook: for it, only the end-of-job check applies.

**At the end of the job**: the runner reruns the check on all the job's commits. The result appears in the evidence
(“Security” line) and, if there are alerts, under “To check before approving”. This check **does not block**: it
informs the reviewer.

## Languages

Fabrique keeps two languages apart: the language of the **interface**, which belongs to each reader, and the language
of the **jobs**, which belongs to the application or to a repo.

### Interface language

The interface exists in English and in French. It follows the browser's language; anyone can change it from the menu at
the bottom left (English, Français, or Auto to follow the browser again), in every mode, including without an account.
The choice is stored on the account when there is one, so it follows the person from one computer to another;
otherwise it is kept in a cookie. When nothing decides, the interface is in English (`FABRIQUE_LANG` changes that
fallback).

Everything Fabrique says itself about a job follows each reader's language: steps, statuses, gates, evidence, audit
log, the lines of the job's log. Two people can look at the same job in two languages.

### Job language

The job language is set in Settings → Job language, and a repo can choose its own in its settings. There is no
choice per request. A new installation defaults to English. It decides:

- the language in which agents and reviewers write for people: understanding of the request, assumptions, notes,
  opinion, questions, study report, review points;
- the language of what Fabrique writes on the Git host: pull request description, "For the reviewer" guide, decision
  comment, initialization commits;
- the language of notifications (webhook, Teams, Jira, e-mail): the job's language, not the recipient's.

Each job records the language it was filed in. The instructions given to agents and reviewers are written in English
whatever the job language; they tell the model in which language to write for people.

### Translating a job

When a reader's language is not the job's, a “Translate” button appears on the job's page (side panel,
full view and inbox). It translates what the agents and reviewers wrote into the reader's language, with the model
chosen in Settings → Agents and models → Translation model. Without a chosen model, there is no button.

- Nothing is translated when a job is opened: only when someone asks.
- The result is stored (per job, language and text) and reused; a text that changes is translated again.
- Translated texts are marked "machine translation", and one click shows the originals again.
- Only prose is sent to the model: never code, a diff or the output of a command (see [security.md](security.md)).
- A long job is translated within limits (60,000 characters and 150 seconds per click); the button then offers to
  translate the rest. What a billed model costs is shown on the job and written to the audit log.

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
10 min then 30 min. “Send a test” checks a channel; “Latest deliveries” shows the state of the queue. Text coming
from the agent is made inert for each tool (no link, mention or image).
