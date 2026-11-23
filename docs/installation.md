# Installation

Interface labels are quoted in French, as they appear on a French screen; the English interface shows their translation.

Two ways to deploy Fabrique:

- **All-in-one** (the simplest): the [`compose.yaml`](../compose.yaml) file at the root starts the dashboard, a
  built-in runner, the launcher and the proxy on a single host. Nothing to configure before starting.
- **Split**: the dashboard on one machine, one or more runners on others, with the two examples in
  [`deploy/`](../deploy/). The runner reaches the dashboard by its address, like a browser.

Every variable is described in [configuration.md](configuration.md).

## All-in-one

```sh
docker compose --profile images build      # or: FABRIQUE_IMAGES=registry/namespace docker compose pull
docker compose up -d
docker compose logs dashboard | grep "setup code"
```

1. Open the dashboard (port 8080 by default), enter the setup code,
   create the administrator account.
2. Follow the **Setup** page: the built-in runner registers itself; what remains is to add a model
   (Settings → Agents and models), a Git provider with the token of a bot account (Settings → Git providers) and a
   first repo.
3. In production: put the dashboard behind a TLS reverse proxy, set `PUBLIC_URL`, and provide `SESSION_SECRET` and
   `FABRIQUE_SECRET_KEY` through the environment (`.env` file, see [`.env.example`](../.env.example)).

The runner and the launcher share a token created at start-up, and the dashboard drops the built-in runner's token on
a volume that only the runner reads: there is no token to copy. To run two instances of Fabrique on the same host,
change `COMPOSE_PROJECT_NAME` (and `FABRIQUE_PORT`, `FABRIQUE_WORK_DIR`).

## Split: the dashboard, and runners on other machines

Both examples start with no preparation: no network to create, no secret to generate. They use the same images and
the same variables as the all-in-one setup (`FABRIQUE_IMAGES`, `FABRIQUE_TAG`).

### 1. The dashboard

```sh
docker compose -f deploy/dashboard.compose.example.yaml up -d
docker compose -f deploy/dashboard.compose.example.yaml logs dashboard | grep "setup code"
```

Open the dashboard (port 8080), enter the setup code, create the administrator account. No variable is required; in
production, set `PUBLIC_URL` to the public address and put the dashboard behind a TLS reverse proxy.

In this example, `FABRIQUE_RUNNER_API_INTERNAL` is `0`: the runners' API accepts requests arriving through the reverse
proxy, since the runners are elsewhere. It remains protected by each runner's token.

### 2. A runner

1. In the dashboard: **Runners → Nouveau runner**. The token is shown **only once**.
2. On the runner's machine, give two values and start:

   ```sh
   FABRIQUE_URL=https://fabrique.example.org FABRIQUE_RUNNER_TOKEN=fab_… \
     docker compose -f deploy/runner.compose.example.yaml up -d
   ```

   `FABRIQUE_URL` is the dashboard's address as this machine reaches it. Both values can also go in a `.env` file next
   to the Compose file.
3. The runner shows as “online” in the dashboard at its first heartbeat (every 20 s).

Nothing else to configure on the runner side: the token shared with the launcher is created at start-up, and Git
tokens and model keys are entered in the dashboard, which hands them to the runner for each job. For a second runner
on the same machine, change `COMPOSE_PROJECT_NAME`, `RUNNER_HOST` and `FABRIQUE_WORK_DIR`.

### The bot account on the Git host

Create an account dedicated to Fabrique on the Git host, and enter its token in **Settings → Git providers**:

| Git host | Rights needed |
|---|---|
| Forgejo, Gitea | write access to repos and issues; write access to the organisation to create repos |
| GitLab | project or group token, Developer role, `api` and `write_repository` scopes |
| GitHub | fine-grained token limited to the managed repos: contents, pull requests, issues |

Protect the main branch of the repos: the bot can merge a PR, not push directly.

**Several providers of the same type** are possible: two GitLab instances, or the same GitLab declared twice with two
bot accounts (for example one limited to confidential repos, the other with full access). Each provider has its own
unique name; two providers can share an address, never a token. Each repo is attached to the provider chosen when it
was added: the runner receives that provider's token, and the same repo path can exist on two providers. A provider
still used by repos cannot be removed.

On **Forgejo or Gitea**, step by step:

1. Create the account (for example `fabrique-bot`) and sign in with it.
2. Avatar → **Settings → Applications → Generate new token**, access to all repos, with the permissions `repository`
   (read and write), `issue` (read and write), `user` (read) and, to let Fabrique create repos, `organization` (read
   and write).
3. Give the account write access to the managed repos (collaborator, or organisation team).
4. In **Settings → Git providers**, enter the Git host's address and the token, then **Tester**: the dashboard says
   which account the token belongs to, without saving anything. Then save.

### What makes a runner secure

Three points, already in place in the example, to keep if you adapt it:

- the jobs' network is `internal: true`;
- only the launcher mounts the Docker socket;
- `WORK_HOST` (launcher) points, on the host side, to the same folder as the runner's `/work` volume.

## Single sign-on, backup, rotation

Single sign-on (OIDC) is configured in **Settings → Sign-in**: create a confidential client at the provider with the
redirect address `PUBLIC_URL/auth/callback`, and as post-logout redirect address `PUBLIC_URL/auth/login?out=1`
(Keycloak: "Valid post logout redirect URIs"; Authentik: add it to the provider's redirect URIs). Without it, signing
out stops on the provider's page. Roles are given in **Settings → Users and roles**, or through OIDC groups. The
local account created at installation remains the fallback account.

`SESSION_SECRET` and `FABRIQUE_SECRET_KEY` are generated and kept with the data if they are missing, so that the stack
starts with no configuration. This is less safe: a backup of the data volume then contains the database **and** the key
that decrypts it. The settings page says so for as long as this is the case; in production, provide them through the
environment.

The dashboard exposes `/health` for monitoring. Its database is a SQLite file in the data volume: backing it up is
enough.

To change a runner's token (regular rotation, suspected leak): **Runners → Renew the token**. The runner keeps its
name, its tags and its history; the old token stops working at once, and the new one, shown only once, has to be put
in `FABRIQUE_RUNNER_TOKEN` before restarting the runner.

The proxy's allowlist (`PROXY_ALLOW`) only contains package registries. Models are reached through the runner's
gateway: there is no need to open them to jobs.

## 3. Models

In **Settings → Agents and models**: add a provider (Anthropic, OpenAI or a compatible server), enter its key
(write-only, encrypted), then tick the models to offer. The test button checks that a model can call tools with each
agent. Claude Code accepts Anthropic models and compatible servers; Codex is reserved for OpenAI models.

## 4. A first repo

1. **Repos → Add**: provider and repo path (`group/project`). Give the bot account write access.
2. **Onboarding** starts by itself: an agent writes the repo's recipe and the runner replays it. Merge the resulting
   PR.
3. The repo then accepts requests. Set its merge policy on the repo's page; start with “Disabled” or “Trial”.

An empty repo can be initialised by Fabrique (“Initialize the repo” button), and a reviewer can ask for a new repo
to be created, which a second person approves. To allow this, fill in “Organization for new repos” on the
provider (Settings → Git providers): it is the organisation (or group) where Fabrique creates repos, and the bot
account must be allowed to create there. Empty field: no repo creation for that provider.

## Updating

Images are tagged by commit (`sha-<8 characters>`). Updating means changing the tag in both Compose files and
restarting. Database migrations are applied when the dashboard starts. Update the runner when no job is running: an
interrupted job is taken again or restarted by the dashboard when its lease expires.
