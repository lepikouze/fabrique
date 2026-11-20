# Changelog

Notable changes to Fabrique, most recent first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/).

The interface is still in French: its labels are quoted here as they appear on screen.

## [0.7.4] - 2026-11-20

No change to the application itself: documentation and licence only. Chart 0.4.4.

### Changed
- **Licence: MIT with the Commons Clause.** Using, modifying and sharing Fabrique is free, including inside a company.
  Selling it is not allowed: no paid product, hosted service or paid support whose value comes mainly from Fabrique.
- **Documentation in English**: the README, the roadmap, the `docs/` pages and this changelog. A French README is kept
  as `README.fr.md`. The interface is still in French; English is next.
- **Files renamed**: `docs/fonctionnement.md` → `docs/how-it-works.md`, `docs/securite.md` → `docs/security.md`,
  `docs/developpement.md` → `docs/development.md`. Links to the old names need updating.
- **Diagrams and roadmap image in English.** The two screenshots will follow with the English interface.
- **Kubernetes guide**: Fabrique also runs on RKE2.

## [0.7.3] - 2026-11-20

### Added
- **Archives page** ("Archivés" button on the Jobs page): archived jobs, 50 per page, with a search that filters as you
  type (reference, title, request, requester, repo), filters by repo and by outcome, and a "Expression régulière"
  checkbox. Restore one job or a selection. Clicking a job opens it in the side panel, as on the Jobs page, with
  "Ouvrir en grand"; closing or collapsing the full view of an archived job returns to the Archives with the current
  search.
- **Permanent deletion of an archived job**, admins only: the button is enabled only after typing `repo/FAB-3` (or
  "supprimer N jobs" for a selection). From the list or from the job itself. The audit log keeps a record; nothing is
  touched on the Git host. Demo jobs carry a "démo" label and cannot be deleted by hand.
- **Automatic archiving** (Settings → Budgets et barrières, off by default): merged jobs and read studies, N days after
  they ended.
- **Abnormally long model response reported in the job log**: beyond 2 minutes or 1 MB, a "modèle" line gives the
  duration and the size. It is the usual sign of a local model looping until its output limit, so a slow job can be
  explained without reading the model server's logs.

### Changed
- **Agent commits**: the trailer added by Claude Code is replaced with `Co-Authored-By: Fabrique`.
- **More direct onboarding**: the agent reads first, writes the recipe, then runs each command once in its final form;
  it no longer looks for tools the repo does not declare, and no longer replays the whole recipe at the end (the runner
  does that in a fresh container).

### Fixed
- **A job's working directory was never removed from the runner**: the disk filled up slowly. The runner now removes the
  directory and the mirror of a job finished more than an hour ago, or deleted.
- **A job number can no longer be reused**: the counter never goes down (commits and the audit log refer to it).
- **Demo**: archived demo jobs piled up without limit; only the 30 most recent are kept.
- Two PRs merged in quick succession in a blank repo wrote "onboarding lancé par la Fabrique" twice on the same
  onboarding job: one line now.

## [0.7.2] - 2026-11-19

### Added
- **Review guide in the PR** ("Pour relire" section): review effort from 1 to 5, computed from the size of the change;
  the three files to start with; the request's criteria judged one by one by the code reviewer (met, not met, unclear);
  each reviewer's verdict. The effort is also shown in the job, next to the merge decision.
- **The security reviewer receives the results of the automatic check** (secrets, packages) as leads to verify: it
  discards false positives and takes up the real problems.
- **Network egress of each job** ("Réseau" line in the Evidence): the domains the job reached through the egress proxy,
  and those the allowlist refused. A refusal also appears under "À vérifier avant d'approuver": it is either a
  dependency to allow, or an agent that tried to reach a site it should not. Informative, never blocking. The launcher
  reads the proxy's log (on Kubernetes: read access to `pods/log`, added to the launcher's role).
- **`Fabrique-Job: FAB-12` trailer** in the job's commits and in the merge commit: from any commit you can find the job,
  its request and its evidence.

### Fixed
- **Sign-out with single sign-on**: you land on Fabrique's sign-in page ("Tu es déconnecté") instead of staying on the
  identity provider's page. Fabrique sends it the return address (`post_logout_redirect_uri`), which must be declared
  there: `PUBLIC_URL/auth/login?out=1` (shown in Settings → Connexion). Without a local account, the sign-in page no
  longer redirects to the provider right after a sign-out.

### Changed
- **Roadmap**: "Model per step" removed. Model profiles (one model per role) cover the need; the topic will come back if
  required.
- **Roadmap**: "Memory per repo" under study (need declared per repo, queue with a memory budget, peak measured for each
  job). "Private package registries" and "Diff inside Fabrique" added as ideas.

## [0.7.1] - 2026-11-18

### Changed
- **Full logo** in the sidebar and on the sign-in page: the workshop F replaces the first letter of "Fabrique". The
  letters follow the theme; the collapsed sidebar keeps the F alone. App icon and home-screen icon (180 px) in the new
  colours.
- **Demo**: 12 demo jobs at most, and it renews itself. A question, a spec or a PR waiting for 3 minutes gets a
  simulated answer; at the cap, the oldest finished job is archived to make room for a new request. The inbox and the
  job list no longer grow forever.
- **Simpler model profiles**: a profile is identified by its name alone (the identifier is derived from the name and
  does not change when it is renamed). No more "Effort" column: a local model's effort level is set once, on its
  provider.
- **README**: animated banner with the logo at the top of the page.

### Added
- **Several Git providers of the same type** (Settings → Providers Git): two GitLab instances, or the same GitLab
  declared twice with two bot accounts (confidential repos on one side, full access on the other). Each provider is
  identified by its unique name; the address may repeat, the token never. Each repo is attached to the provider chosen
  when it is added (or requested) and the runner receives **that provider's token**. The same path may exist on two
  providers. The interface shows the provider's name instead of its type.
- **Gitea** as a Git provider type, next to Forgejo, GitLab and GitHub (same API as Forgejo).
- Database migration (0014): each existing provider gets an identifier derived from its name and each repo is attached
  to the provider of its type; nothing changes for existing installations. A provider still used by repos can no longer
  be removed.

## [0.7.0] - 2026-11-18

### Added
- **Model profiles** (Settings → Agents et modèles → Profils de modèles): one model and one effort level per role,
  reusable from one repo to another. Roles: the agent (triage, investigation, implementation, fixes), the reviewers
  (code, security, architecture, infrastructure), studies and onboarding. Each role names its model, with a suitable
  example under each role. Example: the agent on a local model, the reviewers on Claude for a second opinion.
- **A single "Modèle ou profil" list** in the global settings, in a repo's settings and in the request (admins): one
  model for everything, or a profile, never both. The first level that made a choice wins: request, then repo, then
  global. **Without a profile, nothing changes**: one model for everything, as before.
- The selected models are frozen when the job is created: editing a profile does not change a running job. The job
  shows the profile and the reviewers' model; the request form announces them for each repo.
- **Helm chart**: `dashboard.demo` (true or false) turns on demo mode, without going through `extraEnv`.
- **Roadmap**: "Several models" shipped as profiles.

### Changed
- **New logo**: an "F" shaped like a workshop (sawtooth roof and its windows) sitting on a conveyor. Sidebar, sign-in
  pages and browser tab icon.
- **Key gateway**: it relays each request to the provider of the requested model, among the job's models (agent and
  reviewers), never another one. Reviewers can therefore use a different provider from the agent.

## [0.6.7] - 2026-11-18

### Added
- **Security check on write**, without an AI model: for every file written by the agent (Claude Code), a search for
  hard-coded secrets with Betterleaks 1.10.1 and a check of added packages against their public registry (PyPI, npm,
  Go proxy). A package that cannot be found (misspelled, or invented by the model) or published less than 30 days ago
  is flagged. The agent gets the message and fixes it at once; a "contrôle" line is added to the job's activity. About
  0.1 s per written file.
- **"Sécurité" line in the Evidence**: at the end of the job, the runner reruns this check on all the job's commits.
  Alerts also appear under "À vérifier avant d'approuver". For information only: the job is not blocked. Job images
  ship Betterleaks (SHA-256 checked at build time, about 10 MB compressed).
- **Roadmap**: `ROADMAP.md` and an overview at the end of the README, with the status of each topic (planned, under
  study, idea).
- **Study report downloadable from the inbox**: the "Télécharger le rapport (Markdown)" button is also on the study
  delivered in the inbox, not only in the job.

### Security
- **Dashboard dependencies updated**: starlette 0.50.0 → 1.10.0 (5 known vulnerabilities, two of them rated 7.5),
  python-multipart 0.0.20 → 0.0.34 (7 vulnerabilities, up to 8.6), fastapi 0.122.0 → 0.163.2 (required for
  starlette 1.x), pytest 8.4.2 → 9.2.0. Surveyed with OSV-Scanner on the versions actually installed.

### Fixed
- **Sign-in page**: the buttons overflowed their frame on the right and were not aligned with the logo and the name.
  Everything is centred.

## [0.6.6] - 2026-11-17

### Added
- **Effort level for local models** (Settings → Agents et modèles → edit a compatible provider): server default, low,
  medium, high or maximum. The gateway sends this level to the server instead of Claude Code's. A lower effort makes
  the agent answer faster. "Medium" by default. No effect on Anthropic and OpenAI providers.
- **Study report download** as Markdown, from the job.
- **Adding a repo**: the path is checked at once with the provider; a wrong path is refused with the reason.

### Changed
- **New logo**: an "F" whose top bar ends in a conveyor pulley with a finished module coming out. It replaces the orange
  square marked "F" in the sidebar, on the sign-in pages and in the browser tab. The name is set in Jost (a free font,
  served by the dashboard).
- **Sign-in page**: when single sign-on is configured, the SSO button is put forward and local sign-in unfolds below.
- **Requesting another role**: a dialog over the page (closed with the cross or a click outside) instead of a full
  page; the name no longer appears twice.
- Settings: the Démonstration tab only appears in demo mode (`FABRIQUE_DEMO=1`).
- Notifications: "Nom de notification" instead of "Nom du channel".

### Fixed
- **OIDC sign-in**: "Tester la découverte" reloaded the page and lost what had been typed, which looked saved. The test
  now runs without leaving the page.
- **Public address entered without a scheme** (`fabrique.example.org`): https is added (session cookie restricted to
  HTTPS, links in notifications, OIDC return address).
- "Modifier" and "Retirer" buttons aligned in the Settings tables.

## [0.6.5] - 2026-11-17

### Fixed
- **Helm chart, fresh work volume on NFS**: the runner could not write to it when the root of the volume belongs to
  root (common with NFS provisioners). The runner's group is now applied at mount time (`fsGroup`).

### Documentation
- Kubernetes: prefer fast storage for the dashboard's database; measure the cost of a query before choosing.

## [0.6.4] - 2026-11-17

### Fixed
- **Pages stuck loading behind an HTTP/1.1 intermediary** (load balancer, proxy): a browser opens only 6 connections
  per site there, and each tab kept one for live updates without releasing the previous page's at once. A single
  connection now serves all tabs; it is closed as soon as the page is left and opened only once the page has loaded.
- **NFS volume presenting files under another owner**: git refused the repository in the job's Pod ("dubious
  ownership") and the agent wasted time working around it. The job's repository is declared safe.

## [0.6.3] - 2026-11-17

### Fixed
- **Dashboard database on an NFS volume**: the interface froze at times, especially during a job. SQLite's WAL journal
  relies on shared memory that NFS does not provide. The dashboard now detects a network file system and switches to
  rollback mode, with a warning at startup. `FABRIQUE_SQLITE_JOURNAL=wal|delete` forces the choice.
- **NFS work volume on a multi-node cluster**: the runner failed with "paquet de commits illisible (No such file or
  directory)". A file written by the job's Pod on another node can stay invisible to the runner for a while (NFS client
  cache); the runner now waits for it, up to 90 seconds (`FABRIQUE_NFS_WAIT`).

## [0.6.2] - 2026-11-17

### Fixed
- **Local model served by vLLM**: every call from the agent failed with "Unexpected reasoning effort high". Claude Code
  adds parameters specific to Anthropic's API on its own (effort level, "adaptive" thinking, context management), which
  vLLM rejects. The runner's gateway strips them before relaying to a compatible server.
- **Agent failure message**: it shows the cause returned by the agent (for example "API Error: 500 …") instead of a
  bare "(success)".

## [0.6.1] - 2026-11-17

### Added
- **Corporate proxy**: when the network only reaches the Internet through a proxy, the jobs' egress proxy forwards the
  requests on its allowlist to it (`PROXY_UPSTREAM`, `PROXY_NO_UPSTREAM`). On Kubernetes, a single chart setting,
  `outboundProxy`, also configures the dashboard and the runner. The runner's git follows the runner's proxy.
- Chart: example annotations for cert-manager and for an nginx Ingress.

## [0.6.0] - 2026-11-17

Installation on Kubernetes. Nothing changes for a Docker installation.

### Added
- **Helm chart** (`deploy/helm/fabrique`): dashboard, Service, optional Ingress, volumes, a Secret generated once and
  kept, built-in runner.
- **One Pod per job** (`runner.executor: kubernetes`, the chart's default): a Kubernetes launcher, the only component
  talking to the cluster API, with a service account limited to its namespace, creates a disposable Pod per job. Fixed
  policy: allowlisted image, unprivileged user, no capabilities, read-only file system, no access to the API.
  NetworkPolicy: a job only reaches the egress proxy, the runner's relay and gateway, and DNS.
- **Compliant with the "restricted" Pod Security level**: in Kubernetes mode, the runner runs as the jobs' unprivileged
  user (10002), with no capabilities. The work volume works in `ReadWriteMany` on NFS, including with `root_squash`.
- **Cleanup of orphaned job Pods** by the launcher (finished, or forgotten for 24 hours).
- **Documentation**: [docs/kubernetes.md](docs/kubernetes.md).
- **Built-in runner bootstrapped with a supplied token** (`FABRIQUE_BOOTSTRAP_RUNNER_TOKEN`): on Kubernetes, the
  dashboard and the runner receive the same token from a Secret, instead of sharing a file.

## [0.5.6] - 2026-11-16

### Changed
- **README**: the quick start says where job workspaces live (`/opt/fabrique/travail`, created by Docker on first
  start) and how to move that directory (`FABRIQUE_WORK_DIR`).

## [0.5.5] - 2026-11-16

### Changed
- **GitHub continuous integration**: `actions/checkout` and `actions/setup-python` move to version 7 (version 4 ran on
  a deprecated Node).
- **README**: the first installation section is called "Quick start".

## [0.5.4] - 2026-11-16

### Added
- **Start without cloning the repo**: `deploy/compose.prod.yaml` (published port) and `deploy/compose.traefik.yaml`
  (behind Traefik; domain name, network and resolver as variables) pull the published images. The README explains how
  to start in three commands and how to generate the two secrets (`SESSION_SECRET`, `FABRIQUE_SECRET_KEY`).

## [0.5.3] - 2026-11-16

### Fixed
- **Settings on a phone**: the page no longer overflows sideways (it was up to three screens wide). Tabs scroll in a
  row, forms use one column, and a table that is too wide scrolls inside its frame.

## [0.5.2] - 2026-11-16

### Fixed
- **Phone**: the sidebar button now opens the menu over the page, with its labels (it did nothing on a small screen);
  a tap outside closes it. Archived jobs remain reachable, as an icon.
- **Demo mode**: it no longer started on an installation without a default model (regression from 0.4.1).

## [0.5.1] - 2026-11-16

### Fixed
- **Migration 0012**: repos initialized before 0.5.0 are marked "blank", and those their onboarding had blocked ("not
  ready", "failed") become blank and usable again on upgrade, with no intervention.

## [0.5.0] - 2026-11-16

### Changed
- **A repo initialized by Fabrique ("blank") stays usable whatever happens to its onboarding.** Until now, the
  onboarding restarted after the first merge put the repo back to "not ready": requests were refused until the recipe
  PR was merged, and a failed onboarding blocked the repo. The repo now stays "blank" and accepts requests during the
  onboarding, if the agent gives up (project with no tests or build: nothing to check), and while the recipe PR awaits
  review. It becomes "ready" when that PR is merged.
- The onboarding of a blank repo is retried after each merge, unless a recipe PR is already awaiting review.
- A blank repo's page says where its onboarding stands (running, PR proposed, last attempt and its reason).

## [0.4.6] - 2026-11-16

### Fixed
- **Failed onboarding on a repo that already has a recipe** (blank repo, or proposed recipe): the repo's page no longer
  offered the "Relancer l'onboarding" button and did not give the reason for the failure. Both are back.

## [0.4.5] - 2026-11-16

### Fixed
- **Recipe with no check** (project with no tests or build): the onboarding PR opened, but its approved merge failed
  with "rien à merger (pas de PR ou pas de sha vérifié)". The runner now records the verified commit even when the
  recipe declares no check.

## [0.4.4] - 2026-11-16

### Fixed
- **Agent's assumptions**: they were displayed letter by letter in the job and cut to ten characters. The runner now
  sends them as a list, one assumption per line.

## [0.4.3] - 2026-11-16

### Fixed
- **Setup**: the getting-started menu no longer comes back when an already initialized repo returns to onboarding
  (after its first merge). The "Un premier repo" step stays done as soon as one repo has a recipe.
- **Onboarding restarted after the first merge of a blank repo**: the onboarding job's log says why it starts and
  following which job.
- **Image builds**: no more "uid … is greater than SYS_UID_MAX" warning (container accounts are no longer created as
  system accounts; same uids).

## [0.4.2] - 2026-11-16

### Fixed
- **Repo created with only a README or a licence file**: the onboarding failed ("l'agent n'a pas écrit
  .fabrique/setup.json"), for lack of a project to analyse. Such a repo is now treated as an empty repo: its page
  offers "Initialiser le repo", which adds an empty recipe and keeps the existing files. The repo becomes blank and
  accepts a first request.

## [0.4.1] - 2026-11-16

Feedback from the first real-world installation.

### Added
- **Test a Git provider before saving it** (Settings → Providers Git, "Tester" button): the dashboard connects to the
  Git host with the token entered, says which account it belongs to and whether the organization for new repos is
  visible. The test saves nothing.
- **Edit a Git provider**: "Modifier" button on its row, pre-filled form; the token is unchanged if the field is left
  empty.
- **Remove a repo** from Fabrique (repo page), as long as no request has been filed on it. Nothing is deleted on the
  Git host.

### Changed
- **No model provider or default model at installation**: no more pre-created Anthropic provider without a key. The
  first model enabled becomes the default model; Setup only validates the "Un modèle par défaut" step once it is
  usable, and Settings → Agents et modèles says why no job can start.
- **Adding and requesting a repo**: the "Provider" list shows the configured providers, by name.

### Fixed
- **Restarting the onboarding** uses the agent and model currently in force (repo setting, otherwise global) instead of
  those recorded when the job was created: fixing the default model and restarting is enough.
- **Repo page**: the status and the Onboarding block of the panel follow live updates, like the list.

## [0.4.0] - 2026-11-15

### Added
- **Study mode**: a request of type "Étude" has the agent read the repo and deliver a report (short answer, findings
  with evidence, costed options, proposed next step, limits). The runner captures no commit, pushes nothing and opens
  no PR. New "Étude rendue" step and notification, and an "En faire une demande" button that pre-fills a code request
  with the conclusions.
- **Pause and resume on the daily budget**: when the daily budget is reached while an agent is working, the job is
  paused instead of failing. The workspace and the agent's session are kept; the job resumes by itself where it was,
  the next day or as soon as the budget is raised.
- **Revocable sessions**: signing out, changing your password or an admin's "Déconnecter" button closes all open
  sessions of an account, server side.
- **Personal job views** (user menu): board by step (default) or condensed into four columns; list by step (default)
  or flat, sorted by last activity, with a filter. The choice is stored on the account.
- **Runner token rotation**: "Renouveler le token" issues a new token without recreating the runner; the old one stops
  working at once.
- **Organization for new repos** set on each Git provider (Settings → Providers Git).

### Changed
- **Separate deployment examples** (`deploy/`) simplified: no shared network or init container, no secret to generate.
  The dashboard starts without any variable; a runner only needs the dashboard's address and its token.
- **New request**: labels and examples adapted to the chosen type (change or study).
- **My account** (user menu): a local account changes its password without going through the Settings.
- **Inbox**: one card per item (type, age, full question, repo, requester), the first one opened by default with its
  action; delivered studies stay there until marked as read; no more counter banner.
- **New request**: the repo is chosen first, with the agent, model, budget and merge policy that will apply; a refused
  request keeps everything that was typed.
- Job activity badge: "en file", "actif", "en attente", "terminé"; "en pause" is reserved for an unanswered question and
  for the budget pause.
- The daily budget is also checked during a job, at each runner heartbeat.
- A stored secret no longer follows a change of destination: changing the address of a model provider, a Git provider,
  a Jira or an SMTP server requires entering the key, token or password again.

### Security
Fixes from the second security audit (see `docs/security.md`).
- All-in-one: the token files of the built-in runner and of the launcher are no longer world-readable (`0640` with a
  group reserved for the runner, `0600`). The built-in runner's file follows a token renewal.
  **Upgrading an existing all-in-one installation**: take the new `compose.yaml` (the `runner` service gets
  `group_add: ["10001"]`, without which it can no longer read its token), then renew both tokens, which were readable
  by jobs: Runners → "Renouveler le token" then restart the runner; delete the file in the `jeton-lanceur` volume and
  restart launcher and runner. Separate deployments (tokens passed as variables) are not affected.
- The runner no longer runs git, in its own container, on the repository the agent can configure: these operations
  happen in the job's container, and the commit bundle is checked before import.
- Reviewers in agent mode: program started by absolute path, system `PATH`, fresh home directory, reading an export of
  the verified commit; the job's processes are stopped before the review.
- No runner credential is handed to a job: `CLAUDE_CODE_OAUTH_TOKEN` is no longer supported, an Anthropic model without
  an API key makes the job fail.
- Key gateway: exact paths, `POST` only; spending counted at any point of the job, reserved before the call, and
  estimated when the response has no usage counter.
- "Protected paths" gate: a list of more than 500 files, a path that is too long or contains a control character count
  as protected.
- The dashboard refuses to start without a usable session secret, and writes `secrets.json` atomically.
- An "ok" from the reviewers on a truncated diff no longer counts: the review is marked incomplete.
- Agent text in PRs: reference-style links, `www.` addresses and HTML entities are neutralized.

### Fixed
- Archiving or restoring a job from the List view returned to the Board view.
- Key gateway: a call's spending is counted before the end of the response, so that a very quick following call no
  longer gets past the job's budget check.

## [0.3.0]

First published version.

- Dashboard: requests, job board and list, inbox, timeline, costs, audit log, settings.
- Runner with no AI, launcher (the only Docker access), disposable container per job, allowlisted egress proxy, key
  gateway that keeps the model keys and meters spending.
- Claude Code and Codex agents; Anthropic, OpenAI or compatible-server models (llama.cpp, vLLM).
- Git providers: Forgejo, GitLab, GitHub. Repo onboarding through a recipe (`.fabrique/setup.json`), repo creation on
  human approval.
- Checks replayed by the runner, read-only review (code, security, architecture, infrastructure), gates and merge
  policies per repo, protected paths, budgets per job and per day, watchdog.
- OIDC sign-in and local accounts, requester / reviewer / admin roles, admin account created on first start.
- Notifications: signed webhook, Microsoft Teams, Jira, e-mail.
- All-in-one installation with `docker compose`, or dashboard and runners deployed separately.
