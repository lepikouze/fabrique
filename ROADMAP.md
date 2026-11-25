# Roadmap

What comes next in Fabrique. No date and no promised version: only a status.

- **Planned**: decided, coming in a future version.
- **Under study**: likely, the shape is still open.
- **Idea**: up for discussion, may never happen.

What ships leaves this page and moves to the [changelog](CHANGELOG.md).

![Roadmap](docs/img/roadmap.png)

## Planned

### More languages
The interface exists in English and in French, following the browser or chosen by each person; a job has its own
language, in which the agents write. Spanish is next; other languages later, as needed. A new language is a catalogue
to translate: the code and the instructions given to the agents are in English.

`Spanish` `Catalogue per language`

## Under study

### Live view of the model
See what the model is writing while it writes: its thinking, its answer and the action it is preparing, live on the job
page. Everything the model sends already goes through the runner's key gateway, so it works the same with llama.cpp,
vLLM, Claude and Codex. Shown only to people who can read the job, limited to the end of the current response, never
stored, and can be turned off in the settings. A model that loops shows it in the first minute instead of after the
response.

`Live` `Thinking` `Key gateway` `Any model server`

### Project mode
Build a whole application from one request. A request of type "Project" has the agent write a specification, a
technical plan and an ordered list of tasks into the repo (`specs/`), in the spirit of spec-driven development. A human
approves that pull request; Fabrique then creates one request per task and runs them in order, each job reading the
spec and the plan first. A project page shows progress, and a budget caps the whole project.

`Specification` `Plan` `Task list` `Chained jobs` `Human approval`

### Security audit of a repo
A request of type "Security audit": read-only, like a study, nothing pushed. The agents follow a structured audit
method: reconnaissance of the code and its trust boundaries, hunting waves by area, each candidate finding checked again
by a fresh verifier, then a report with confirmed findings, findings to validate and rejected ones, each with the file,
the proof and the smallest fix. Runs in the job's disposable container, without network. Two depths: quick (one agent,
the main risks) and full (several agents, capped by a budget). From the report, a human can file a fix request per
finding in one click.

`Security` `Read-only` `Verified findings` `Report` `Fix requests`

### Requests from an issue
A `fabrique` label on a GitHub, GitLab or Forgejo issue creates an incoming request in the inbox. Nothing starts
without a reviewer's approval. Fabrique then comments on the issue: job created, PR opened, merge.

`GitHub` `GitLab` `Forgejo` `Label` `Human approval`

### Requests from Jira
Same principle, starting from a labelled Jira ticket. The repo is chosen at approval time.

`Jira Cloud` `Jira Data Center` `JQL`

### PostgreSQL
PostgreSQL in addition to SQLite, for larger installations or several dashboard replicas.

`PostgreSQL` `SQLite` `Migrations`

### Preview
See a change running before deciding on the merge.

`Ephemeral environment` `Before the merge`

### House rules
A rules file specific to each repo (conventions, things not to do, architecture choices), read by the agents and by
the reviewers.

`Conventions` `Reviews` `Per repo`

### Confidential mode
A request marked confidential only goes through a local model: nothing leaves for a remote provider.

`Local model` `Sensitive data`

### Memory per repo and queue
Today every job has the same ceiling (4 GB) and the only safeguard is the number of jobs running in parallel. A repo
with end-to-end tests in a browser does not have the needs of a small service.

- **Requirement declared per repo** in its recipe (for example 8 GB for E2E tests, 1 GB for a small service), capped
  by a maximum set by the admin.
- **Queue with a memory budget**: a job only starts if the sum of the requirements of the running jobs fits in the
  runner's memory, minus a margin; otherwise it waits instead of bringing the others down.
- **Peak measured for every job**, shown in the job, to tune the repo's requirement to reality. A job killed for lack
  of memory is reported as such, and can be restarted once with a higher ceiling if the budget allows.

`Memory` `Admission` `Queue` `OOM` `E2E tests`

## Ideas

### Mistral
Mistral's API (Mistral Large, Devstral, Codestral) as a provider. It speaks the Chat Completions API: declared as a
compatible provider with its key, it can already serve the reviewers and the check of a request when it is filed. The
agent is the missing part, since Claude Code needs the Messages API and Codex the Responses API. Two possible ways:
translate Messages to Chat Completions in the key gateway, or add Mistral's own coding agent as a third agent. Devstral
served locally (llama.cpp, vLLM) is already usable today.

`Mistral` `Devstral` `Chat Completions` `Key gateway` `Agent`

### Stronger isolation
Each job in a disposable virtual machine, on top of the container or the Pod.

`Disposable VM` `Isolation`

### Other sources of requests
A small API with a token so that other tools can file requests, which go through the same approval.

`API` `Token` `Human approval`

### Private package registries
A repo that depends on internal packages (PyPI, npm, Maven or company Go modules) cannot be installed in a job
without a credential, and no secret enters the container. As with model keys, a relay in the runner would add the
credential and forward to the registry: the job would never see the secret.

`Private PyPI` `Private npm` `Maven` `Credentials outside the job`

### Production signals
A monitoring alert that proposes a fix request, to be approved like any other.

`Monitoring` `Alerts` `Human approval`
