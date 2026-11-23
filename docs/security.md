# Security

## Trust model

The model is that of CI runners: a **trusted manager** (the runner) and **untrusted executions** (the repos' code and
the agent). An agent reads content nobody controls — files in the repo, the text of requests — and can therefore be
hijacked. Fabrique assumes that **anything running in a job may be hostile**, and gives each part a single power.

| Part | What it holds | What it cannot do |
|---|---|---|
| **Dashboard** | the database, the decisions, the audit, a read-only copy of each job's diff | touch the code or the repos |
| **Runner** | the dashboard token, Git tokens, model keys for the duration of a job | talk to Docker; execute a repo's code |
| **Launcher** | the only access to Docker | anything other than create, exec into and stop **its own** job containers, with a fixed policy |
| **Job container** | this job's workspace | read a secret; reach the Git host, the dashboard, the launcher, the local network or the Internet outside the allowlist |

![Isolation](img/isolation.png)

## A job's container

The policy is imposed by the launcher and the runner cannot negotiate it: image chosen from an allowlist, unprivileged
user (uid 10002), no capability, `no-new-privileges`, read-only file system (except the work space and `/tmp`),
limited memory, CPU and number of processes, a single mount (**this** job's workspace), a single network. The launcher
refuses to act on a container it did not create.

The jobs' network is internal, with no gateway. Its only exits:

- the allowlisted **proxy**, for package registries;
- the runner's **`fab` relay**, with a token specific to the job, to talk to the dashboard;
- the runner's **key gateway**, with that same token, to talk to the model. It replaces the token with the real key
  and only opens the agents' endpoints: exact path, `POST` only (no sub-resource, no `..`). It measures the cost of
  every call, at any point in the job (agent, checks, reviewers), reports it to the dashboard and caps it at the job's
  budget: a call in progress is reserved before being relayed, and a paid response with no usage counter is estimated,
  never counted as zero. Local models go through it too: the model's machine is not reachable by jobs.

No credential of the runner enters a job: no model key, no Git token, no subscription token. An Anthropic model with
no API key (in the dashboard or on the runner) makes the job fail before the agent starts.

## The workspace is never trusted

The agent writes the whole workspace, `.git` folder included. So the runner never trusts it.

- **Git token.** It is only used in a Git mirror owned by the runner, which the agent can read but not write and
  which is never mounted in a job. The main branch is copied there from the Git host; the agent's commit is imported
  into it (objects verified), checked, then pushed from there. The agent clones from this mirror, with no token.
- **Git on the runner side.** In its own container, the runner only runs git on what it created itself: its mirror,
  and the initial clone of the workspace, before any agent has touched it (system binary and `PATH`, configuration
  imposed through the environment). Once the agent has been there, its repository is configured by it — a content
  filter or a signing program declared in `.git/config` would be run by git, and no list of frozen settings can name
  them all. Operations on that repository (status, add, commit, bundle of the commits) therefore run in the job's
  container, where the agent's code already runs. Nothing they print is taken as authoritative.
- **Importing the commit.** The commit bundle written on the job side is read only as data: opened without following
  links, refused if it is not a regular file, copied to a folder of the runner, then imported from that copy.
- **Recipe and diff base.** Read from the mirror, at the Git host's commit. The list of changed files and their size
  are computed in the mirror, on raw paths; the dashboard treats any doubtful path as protected.
- **Onboarding.** The commit must contain the recipe and nothing else; the recipe replayed is the one in the commit;
  if the replay changes the commit, the onboarding fails.
- **Agent's text.** Bounded and made inert before being published with the bot account (PR, comments) or sent in a
  notification: no mention, issue-closing keyword, image or link (direct, by reference, bare address, HTML entity).
- **List of files in a PR.** If the dashboard cannot faithfully compare a path with the protected paths — more than
  500 files, a path of 300 characters or more, a control character — the gate counts the file as protected: a human
  decides.
- **Reviewers.** The job's processes are stopped before the review. An "ok" returned on a truncated diff does not
  count: the review is marked incomplete. In agent mode, the reviewer is started by its absolute path, with the system
  `PATH`, a fresh home folder and an export of the verified commit taken from the runner's mirror: neither the working
  tree nor the home folder that the agent and the checks may have modified.

## Instructions are not gates

The instructions given to the agent, and the hook that filters its commands in Claude Code (`runner/guard.py`), reduce
mistakes; they are not security boundaries. The boundaries are the container, the network, the absence of secrets and
the checks done by the runner and the dashboard.

What the hook does with a Bash command: it reads the line the way a shell does and refuses a network tool, a container
or privilege tool, `env`/`printenv`/`export`, or a remote Git operation wherever it is *run* (any command position,
`$(...)`, `sh -c`, `eval`, `xargs`, `find -exec`, a path such as `/usr/bin/env`, a name spelled with quotes, a command
name held in a variable). A file name or a quoted text that merely contains such a word passes (`cat docker-compose.yml`,
`git commit -m "add CSV export"`). References to the sensitive variables and to `/proc` are refused anywhere. A line
that cannot be parsed falls back on the broad rule: the word anywhere refuses it.

Known limits of the hook, which it does not claim to cover:

- a script the agent writes and then runs (`python tool.py`, `sh build.sh`, a `Makefile` target, an `npm run` script)
  is not inspected: it can read its own environment or call any program present in the image;
- inline code is only matched against words (`environ`, `getenv`, the forbidden program names): code that builds these
  names at run time, or decodes itself, passes;
- wrappers the hook does not know (it knows the common ones: `xargs`, `timeout`, `nohup`, `poetry run`, `npx`…) hide
  the command they start.

This is acceptable because of what the job's environment holds. The container itself starts with `HOME` only. The agent's
process receives: `PATH`, `HOME`, the Git author name and e-mail, the egress proxy address, the model name, the address
of the runner's key gateway with **the job's own token** (valid for this job, within its budget, for its models only),
and the `fab` relay address with **the job's relay token** (it can only report on this job). It never holds a Git
token, a model provider key, or any credential of the runner; there is no Docker socket and no container tool in the
job image.

## Code kept by the dashboard

The dashboard never clones a repo and uses no Git token itself. Since 0.7.5 it does keep **text that comes
from the repos**, sent by the runner: the diff of each job's verified commit, so that a reviewer can read it in place.

- **Where**: the `diffs` table of the dashboard's database, one row per job (the latest diff replaces the previous
  one), capped at 500,000 characters. It follows the database: same volume, same backups.
- **Who can read it**: whoever can read the job. A requester sees the diffs of their own requests; reviewers and
  admins see all of them.
- **How long**: as long as the job exists. Deleting a job deletes its diff.
- **What it is used for**: display only. It is escaped like any other text from a repo or an agent (strict content
  security policy, no inline script), and no gate reads it.
- **Turning it off for a confidential repo**: untick “Show the diff in Fabrique” in the repo's settings
  (admins only, on by default). From then on the runner sends no diff for that repo, the dashboard refuses to store one
  even if a runner sends it, and the diffs already stored for that repo are deleted at once (the audit log says how
  many). The job page says the diff is not kept and links to the pull request: the review happens on the Git host.
- **Turning it off for a whole runner**: `FABRIQUE_DIFF_MAX=0` on that runner. Nothing is sent for its jobs, whatever
  the repo's setting.

Check outputs, the agent's messages and study reports were already kept in the same database; they too can quote the
repo.

## What goes to the translation model

Translating a job (the “Translate” button, never automatic) sends text from the dashboard to the model chosen as
translation model. A deployment that must keep everything on its own network chooses a local model, or none: without
a chosen model the button does not exist.

- **Sent**: the prose written by the job's agents and reviewers, and nothing else: interpretation, assumptions,
  verdict, notes, questions, study report, reviewers' summaries and points, step summaries.
- **Never sent**: the code, the diff, the output of checks and commands, the request typed by a person, secrets, the
  audit log.
- This prose can quote file names or short pieces of code the agent mentioned. A study report is prose about the
  repository: on a confidential repo, treat the translation model like the model that ran the study.
- The text is framed as data: the model is told never to follow an instruction it contains. A translation is only
  displayed, escaped, and never acted upon.
- The translation is stored in the dashboard's database, per job and language, and deleted with the job. The audit log
  records who asked, the language, the number of texts, the model and the cost, never the texts.

## What goes to the model when a request is filed

When a change request is sent from the form, Fabrique makes one short call to the model that would run the job, to
check the request against a fixed grid (what must change, where, how to know it is done).

- **Sent**: the title, the need and the “Done when” criterion typed by the requester. Nothing from the repo, no code,
  no other job.
- **To which model**: the one the job would use (request choice, then repo, then global setting). A confidential repo
  whose jobs run on a local model is therefore checked by that local model.
- **The request is data.** The model is told never to follow an instruction it contains, and its answer is read
  strictly: a JSON object saying “clear”, or one to three questions, each cut to 240 characters. Anything else is
  ignored. The questions are only ever shown, escaped, to the person who wrote the request; they change no gate, no
  setting and no right.
- **Never blocking**: a model that is missing, slow (8 seconds) or failing is ignored and the request is filed.
- **Audit**: one line per check, with the model, the outcome, the cost and the duration; never the request nor the
  questions.
- Studies and requests created through the API are not checked.

## Sign-in and secrets

- First start: creating the administrator account requires a setup code written to the container's logs. Strong
  passwords, hashed (scrypt), five attempts per quarter of an hour.
- OIDC with PKCE; identity token verified; signed sessions. An incomplete configuration closes the dashboard (503),
  except for the local fallback account. An OIDC sign-in cannot take over the name of a local account.
- Model keys, Git tokens, the OIDC secret and channel secrets are encrypted in the database (`FABRIQUE_SECRET_KEY`),
  write-only. Git tokens and model keys are handed to the runner for each job.
- **A secret does not follow a change of destination.** Changing the address of a model provider, a Git provider, a
  Jira or an SMTP server requires entering the key, token or password again: a stored secret is never sent to an
  address it was not entered for.
- Runner tokens are only kept as a hash. “Renew the token” issues a new one without recreating the runner (the
  old one stops working at once); “Revoke” cuts access immediately.
- **Revocable sessions.** Signing out, changing your password or an admin's “Sign out” button (Settings →
  Users) closes all the account's open sessions, server-side: a copied session cookie is worth nothing. Removing
  someone's role removes all their access from the next request.
- Requests that change state are refused if they come from another origin.
- **Generated secrets.** `SESSION_SECRET` and `FABRIQUE_SECRET_KEY`, if they do not come from the environment, are
  generated in `secrets.json` (atomic write). If that file is damaged or the folder not writable, the dashboard
  refuses to start: it never runs with an empty secret. A session secret that is too short also closes local accounts.
- **Token files (all-in-one).** The launcher's token is `0600`; the built-in runner's is `0640`, readable by a group
  that the runner receives (`group_add`) and that the agent's user does not have. The runner warns at start-up if a
  token file is world-readable.

## Accepted risks

- **The launcher has access to Docker.** If compromised, it would compromise the host. It is tiny, only listens on a
  private network shared with the runner alone, requires a token and refuses anything outside its policy. Possible
  hardening: launcher and jobs on a dedicated machine, rootless Docker.
- **Shared kernel.** A container is not a VM. For untrusted code, aim for a disposable VM per job (on the roadmap).
- **Leak through allowed exits.** An agent can in theory get code out through a request to the model or to a package
  registry. Reduce the allowlist, or use an internal package mirror.
- **OIDC identity.** Accounts are recognised by their user name (or their e-mail). If the provider lets users change
  these fields themselves, restrict that right on the provider side.
- **Generated secrets.** Without variables, the encryption key is kept with the data: provide `SESSION_SECRET` and
  `FABRIQUE_SECRET_KEY` through the environment to separate them.
- **Open mode.** `FABRIQUE_OPEN=1` removes sign-in: for development only.
- **`local` execution mode.** The agent runs in the runner's container, separated by a plain Unix user: for
  development and demonstration only.

## Audit

The code has been through two full security audits (code reading, search by area, each finding reproduced in a
sandbox with no network then verified independently), the second rechecking every fix from the first. Confirmed
findings were fixed, and each is covered by a regression test (`tests/test_audit.py`, `tests/test_audit3.py`). The
principles above — runner's mirror, agent's git run on the job side, capped gateway, reviewer independent of the job,
inert agent text — come directly from them.

What remains to be checked in each installation, because the code cannot decide it:

- **OIDC identity.** An account is tied to the user name (`preferred_username`), otherwise to the e-mail. The identity
  provider must not let a user choose or change these values, nor reassign a name.
- **Version tags.** The Forgejo workflow publishes images for any `v*` tag: creating these tags must be reserved for
  maintainers (protected tags), and images deployed by digest rather than by tag.
- **Exposure.** The all-in-one compose publishes the dashboard over HTTP on all interfaces: behind a TLS reverse
  proxy, restrict the published port to that proxy.

## Reporting a vulnerability

Write to the repo's maintainer, privately, with enough to reproduce the problem.
