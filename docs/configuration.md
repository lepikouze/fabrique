# Configuration

Secrets and starting values come from the environment. Everything else (roles, models, budgets, notification
channels…) is set in the dashboard and lives in the database. A value fixed by the environment appears locked in the
settings.

## Dashboard

| Variable | Default | Role |
|---|---|---|
| `FABRIQUE_DATA` | `/data` | Data folder (SQLite database) |
| `DATABASE_URL` | SQLite in `FABRIQUE_DATA` | Database address |
| `PUBLIC_URL` | `http://localhost:8080` | Public address; used for the OIDC redirect and for links in notifications |
| `FABRIQUE_PREFIX` | `FAB` | Prefix of job references (`FAB-12`) |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID` | — | OIDC provider; otherwise configurable in the dashboard |
| `OIDC_SCOPES`, `OIDC_GROUPS_CLAIM` | `openid profile email`, `groups` | Scopes and name of the groups claim |
| `OIDC_ADMIN_GROUPS`, `OIDC_REVIEWER_GROUPS`, `OIDC_REQUESTER_GROUPS` | — | Groups granting each role (comma-separated lists) |
| `OIDC_SESSION_HOURS` | `12` | Length of a session |
| `FABRIQUE_ADMINS` | — | Accounts that are always admin (fallback) |
| `FABRIQUE_BOOTSTRAP_RUNNER_FILE` | — | All-in-one: file where the dashboard drops the built-in runner's token, which it creates at start-up |
| `FABRIQUE_SETUP_CODE` | generated | Setup code required to create the first admin account; otherwise written to the logs |
| `FABRIQUE_OPEN` | `0` | `1`: open mode, no sign-in, every visitor is admin. Development only |
| `FABRIQUE_RUNNER_API_INTERNAL` | `1` | Refuses the runners' API when the request came through a reverse proxy; `0` for a remote runner |
| `FABRIQUE_DEMO`, `FABRIQUE_DEMO_TICK` | `0`, `6` | Demonstration mode and pace of the simulator (seconds) |
| `FABRIQUE_LANG` | `en` | Interface language of last resort (`en` or `fr`), when neither the person nor the browser decides |
| `FABRIQUE_NOTIFY` | `1` | `0` turns off notification delivery |
| `FABRIQUE_PRICES_URL` | LiteLLM's public list | Address of the model price list fetched every week (prices per token, LiteLLM format). Empty: nothing is fetched; the built-in table and the prices entered by hand on the models remain |
| `FABRIQUE_PRICES` | `1` | `0` turns off the weekly fetch of the price list |
| **`OIDC_CLIENT_SECRET`** | — | OIDC client secret; otherwise entered in Settings → Sign-in (encrypted) |
| **`SESSION_SECRET`** | generated | Signs sessions, at least 32 characters |
| **`FABRIQUE_SECRET_KEY`** | generated | Encrypts model keys and channel secrets |

The last two are generated in the data folder if they are missing; providing them through the environment keeps them
separate from the database. With neither single sign-on nor a local admin account, the dashboard only serves the
first-start page. If an OIDC issuer is set but incomplete and no local account exists, it answers 503 everywhere
rather than opening up.

## Runner

| Variable | Default | Role |
|---|---|---|
| `FABRIQUE_URL` | `http://fabrique:8080` | Dashboard address |
| **`FABRIQUE_RUNNER_TOKEN`** | — | Runner token, given once by the dashboard; or `FABRIQUE_RUNNER_TOKEN_FILE` (file) |
| `GIT_BASE_URL`, **`GIT_TOKEN`** | — | Forgejo — optional if set in Settings → Git providers |
| `GITLAB_BASE_URL`, **`GITLAB_TOKEN`** | — | GitLab — same |
| `GITHUB_BASE_URL`, `GITHUB_API_URL`, **`GITHUB_TOKEN`** | github.com | GitHub — same |
| `GIT_AUTHOR` | — | Author of the runner's commits, `Name <address>` |
| `RUNNER_HOST`, `RUNNER_TAGS` | machine name, — | Display name and tags (a repo can require some) |
| `CAPACITY` | `1` | Jobs in parallel |
| `EXECUTOR` | `local` | `docker`: one container per job, through the launcher; `local`: shared workshop (development) |
| `LAUNCHER_URL`, **`LAUNCHER_TOKEN`** | `http://fabrique-launcher:7080`, — | Launcher and its token; or `LAUNCHER_TOKEN_FILE` (file) |
| `WORK_DIR` | `/work` | The runner's workspaces and Git mirrors |
| `RELAY_HOST`, `RELAY_PORT`, `RELAY_PUBLIC_URL` | `127.0.0.1`, `7071`, — | `fab` relay (agent to dashboard); `0.0.0.0` in docker mode |
| `GATEWAY_PORT`, `GATEWAY_PUBLIC_URL` | `7072`, — | Key gateway (agent to model) |
| `JOB_PROXY` | — | Egress proxy given to jobs |
| `AGENT_UID` | `10002` in the image | User the agent and the operations in the workspace run as |
| `JOB_TIMEOUT`, `CHECK_TIMEOUT`, `REVIEW_TIMEOUT` | `3600`, `900`, `600` | Maximum durations, in seconds |
| `MODEL_WAITS` | `30,60,120,240,480` | Seconds waited before each new attempt when the model server is saturated, loading or unreachable; the job fails after the last one |
| `MERGE_RETRY_WAITS` | `5,15,30` | Seconds waited before each new attempt when the Git host refuses a merge for a moment |
| `MAX_TURNS`, `FIX_ATTEMPTS` | `60`, `2` | Agent turns; new attempts if the checks fail |
| `MAX_TURN_RESUMES` | `2` | Resumes of the agent's session after its turn limit (work kept) before the job fails |
| `REVIEW_MODE` | `direct` | `direct`: one request to the model per reviewer; `agent`: read-only agent, on an export of the verified commit |
| `CLAUDE_BIN`, `CODEX_BIN` | `/usr/local/bin/claude`, `/usr/local/bin/codex` | Agent programs, by absolute path (never looked up in a job's `PATH`) |
| `REVIEW_MAX_DIFF`, `REVIEW_MAX_TOKENS` | `60000`, `8000` | Size of the diff sent and of the reviewers' answer |
| `THINKING_BUDGET` | `8000` | Local model (compatible server without key): thinking tokens allowed per agent response, sent as the Anthropic `thinking.budget_tokens` (applied by llama.cpp; a server that does not take it, vLLM for example, gets the request without it after a 400 or 422). Without it, a small model can think until its output limit and never answer. `0`: no budget |
| `RUNAWAY_STREAK` | `3` | Agent responses cut at the output limit in a row before the run is stopped with that cause (the model loops, or its context is full). `0`: never |
| `FABRIQUE_DIFF_MAX` | `400000` | Characters of diff sent to the dashboard for human reading, with the pull request. `0`: nothing is sent (the reviewer reads the diff on the Git host) |
| `ANTHROPIC_API_URL`, **`ANTHROPIC_API_KEY`** | api.anthropic.com, — | Optional: key on the runner rather than in the dashboard. An API key is required: a subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) is not supported, since no credential of the runner is handed to a job |
| `PRICE_IN`, `PRICE_OUT`, `PRICE_CACHE_READ` | `5`, `25`, `0.5` | Default prices ($ per million tokens) for a model with no known pricing |

Behind a corporate proxy, the runner follows `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` to reach the dashboard, the
Git host and the model providers (put the dashboard and internal addresses in `NO_PROXY`). These variables are never
passed on to jobs: jobs go out through the allowlisted proxy, to which `PROXY_UPSTREAM=http://host:port` gives the
corporate proxy and `PROXY_NO_UPSTREAM` the internal domains to reach without it.

Model keys are best declared in the dashboard: the runner receives them for each job and never gives them to the
agent.

## Launcher

| Variable | Default | Role |
|---|---|---|
| **`LAUNCHER_TOKEN`** | — | Token required for every request, at least 32 characters; or `LAUNCHER_TOKEN_FILE`, created if missing |
| `JOBS_PREFIX` | `fabrique-job` | Prefix of job container names |
| `JOB_IMAGES` | — | Allowlist of job images: `python=image:tag,node=…,go=…,java=…` |
| `JOBS_NETWORK` | `fabrique-jobs` | (Internal) network of the job containers |
| `WORK_HOST` | — | Workspaces folder **as seen from the host** |
| `WORK_DIR` | `/work` | Same path as in the runner |
| `JOB_MEMORY`, `JOB_CPUS`, `JOB_PIDS` | `4g`, `2`, `512` | Limits of a job container |

## Proxy

| Variable | Role |
|---|---|
| `PROXY_ALLOW` | Allowed host names, as anchored regular expressions (`^pypi\.org$`), comma-separated. Everything else is refused. |
