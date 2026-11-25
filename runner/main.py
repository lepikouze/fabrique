"""Fabrique runner: non-AI program. It takes orders from the dashboard queue,
prepares a disposable workspace, launches the agent, applies gates, reruns checks itself, pushes the
branch, opens the PR and merges when the dashboard decides (pinned to the verified sha).

All operations in the workspace (clone, installation, checks, git) run under the agent's
user: the repo code is never executed with the runner's privileges.

The workspace (including its .git) is written by the agent: it is never trusted. The Git token is ONLY used in a
bare mirror owned by the runner (WORK_DIR/.mirrors/<ref>.git), which the agent can read but not write: the main branch is
pinned there from the Git host (recipe, base of diffs), the agent's commit is imported and then verified there, and it is
pushed from there. Git runs there with a fixed configuration (no configuration files read outside the mirror)."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import json, logging, os, re, shutil, signal, subprocess, threading, time, stat
from . import config, executor, messages, review
from .texts import W
from .agent import Run, base_env, runner_for
from .executor import as_agent, kill_agent_leftovers
from . import forge as forges
from .panel import Panel
from .relay import Relay, record_decision
from .gateway import Gateway

log = logging.getLogger("runner")


class Ctx:
    def __init__(self, item):
        j = item["job"]
        self.intent, self.job, self.model, self.repo = item["intent"], j, item["model"], item["repo"]
        # Reviewers: profile model, otherwise the agent's model (new context in both cases: they only see the diff).
        self.review_model = item.get("review_model") or item["model"]
        # Profile model per reviewer role (code, security…), when it differs from the reviewers' model.
        self.review_models = {k: v for k, v in (item.get("review_models") or {}).items() if isinstance(v, dict) and v.get("id")}
        self.forge = item.get("forge")   # Git provider set in the dashboard (address and token), or None
        self.ref, self.title, self.branch = j["ref"], j["title"], j["branch"]
        self.budget = float(j.get("budget") or 0)
        self.base_cost, self.base_tokens = float(j.get("cost") or 0), int(j.get("tokens") or 0)
        self.dir = os.path.join(config.WORK_DIR, self.ref)
        self.repo_dir, self.home = os.path.join(self.dir, "repo"), os.path.join(self.dir, "home")
        self.question = self.decision = self.gave_up = self.run = None
        self.review_fixed = False
        self.asked_opinion = False
        self.stages = set()
        self.watchdog_min = int((item.get("limits") or {}).get("watchdog_min") or 10)
        self.nudged = self.cancelled = False
        self.study = j.get("source") == "study"   # study: a report, neither branch nor PR
        self.model_waits = 0   # attempts already made while the model server was saturated
        self.turn_resumes = 0   # resumes after the agent's turn limit
        self.tests_changed = []   # existing test files the change modifies or deletes
        self.in_agent = self.paused = False   # an agent run is in progress; paused (daily budget)
        self.attempt = 1
        self.fab_token = ""
        self.recipe = None
        self.image = "python"   # job image (launcher allowlist): "image" section of the recipe
        self.onboarding = False
        self.system = None   # agent instructions (job playbook, or onboarding instructions)
        self.mirror = os.path.join(config.WORK_DIR, ".mirrors", f"{self.ref}.git")
        self.base_sha = ""   # main branch commit, pinned from the Git host into the mirror
        self.fork_sha = ""   # commit the workspace was cloned from: the main branch may have moved since (job resumed later)
        self.gw_spent = self.gw_mark = 0.0   # expense measured by the key gateway (entire command / start of agent run)


# Environment directories never committed by the runner (installed by the recipe, not written by the agent).
ENV_EXCLUDES = [f":(exclude,glob)**/{d}/**" for d in (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
                                                     ".mypy_cache", ".tox", ".gradle", "target")]


# Git: binary and system PATH only (never a directory the agent can write), and configuration imposed by
# the environment, which overrides any configuration file: no key able to run a command or to divert the token
# can come from a .git/config written by the agent.
SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WORKSPACE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")   # name of a working directory: the job reference (FAB-12)
GIT = shutil.which("git", path=SYS_PATH) or "/usr/bin/git"
SAFE_GIT = {"core.hooksPath": "/dev/null", "safe.directory": "*", "core.fsmonitor": "false", "core.sshCommand": "false",
            "core.gitProxy": "", "core.askPass": "", "credential.helper": "", "diff.external": "", "http.proxy": "",
            "http.sslVerify": "true", "protocol.allow": "never", "protocol.https.allow": "always", "protocol.http.allow": "always",
            "protocol.file.allow": "always", "push.followTags": "false", "core.quotePath": "false",
            "commit.gpgSign": "false", "tag.gpgSign": "false", "gpg.program": "false", "core.attributesFile": "/dev/null"}


def global_config():
    """"Global" configuration written by the runner (readable, not writable by the agent). It is used for sub-processes
    that git launches on a local repo (upload-pack from a clone from the mirror), which do not receive the GIT_CONFIG_*
    from the environment: without it, the clone of the mirror (owned by the runner) by the agent user is refused ("dubious ownership")."""
    return os.path.join(config.WORK_DIR, ".mirrors", "gitconfig")


def write_global_config():
    path = global_config()
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    body = "[safe]\n\tdirectory = *\n[core]\n\thooksPath = /dev/null\n\tfsmonitor = false\n[protocol]\n\tallow = never\n" \
           "[protocol \"file\"]\n\tallow = always\n[protocol \"https\"]\n\tallow = always\n[protocol \"http\"]\n\tallow = always\n"
    if not os.path.isfile(path) or open(path).read() != body:
        with open(path, "w") as f:
            f.write(body)
    os.chmod(path, 0o644)


def git_env(extra_cfg=None):
    """Variables that freeze the Git configuration: SAFE_GIT (+ provider keys, e.g., token header), no system
    files, the runner's global configuration, no replacement objects."""
    cfg = {**SAFE_GIT, **(extra_cfg or {})}
    env = {"GIT_CONFIG_COUNT": str(len(cfg)), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": global_config(),
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for i, (k, v) in enumerate(cfg.items()):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env


JOB_GIT = ("core.hooksPath", "core.fsmonitor", "commit.gpgSign", "tag.gpgSign", "gpg.program", "safe.directory")


def job_git_env():
    """Git configuration passed to git commands launched in the job container: the bare minimum (no hooks, no
    signature), in few variables — the launcher limits the size of a command's environment."""
    env = {"GIT_CONFIG_COUNT": str(len(JOB_GIT)), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for i, k in enumerate(JOB_GIT):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, SAFE_GIT[k]
    return env


def provider_cfg(secret_env):
    """Configuration keys passed by the provider (GIT_CONFIG_*) -> dictionary."""
    secret_env = dict(secret_env or {})
    return {secret_env[f"GIT_CONFIG_KEY_{i}"]: secret_env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(secret_env.get("GIT_CONFIG_COUNT", 0) or 0))}


class Failed(Exception):
    pass


class Closed(Exception):
    """The agent concluded, with its opinion, that there is nothing to change: the job closes cleanly, it did not fail."""


class Waiting(Exception):
    """Onboarding: repo not yet ready (access, empty repo, branch missing). The job waits, it does not fail."""


class Cancelled(Exception):
    pass


class Paused(Exception):
    """Daily budget reached during the agent run: the job waits (workspace and session kept)."""


def sh(ctx, cmd, timeout=600, check=True, cwd=None, extra=None):
    """Workspace and git files BEFORE any agent run (clone from the mirror, repo created by the runner), IN
    the runner, under the agent user, WITHOUT token, system PATH and fixed Git configuration. As soon as the agent
    has run, its repo is no longer manipulated here: see jgit()."""
    cmd = cmd if isinstance(cmd, list) else ["sh", "-c", cmd]
    if cmd[0] == "git":
        cmd = [GIT] + cmd[1:]
    env = base_env(ctx.home, git_env())
    env.update(extra or {})
    env["PATH"] = SYS_PATH
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        env.pop(k, None)
    p = subprocess.run(as_agent() + cmd, cwd=cwd or ctx.repo_dir, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise Failed(f"{cmd if isinstance(cmd, str) else ' '.join(cmd[:3])} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def jgit(ctx, args, check=True, timeout=600, extra=None):
    """Git on the AGENT repo, after its run: executed by the executor, therefore in the job's disposable container.
    This repo is configured by the agent (.git/config, attributes): a content filter or a signature program it
    declares there is launched by git, and a list of frozen keys cannot name them all. They therefore run where the agent's
    code already runs, never in the runner container. Nothing these commands display is authoritative: the
    commit is then imported into the runner mirror and verified there. In a local workshop (development, no job
    container), it falls back to sh() and its frozen configuration."""
    if getattr(ctx.ex, "name", "local") != "docker":
        return sh(ctx, ["git", *args], timeout=timeout, check=check, extra=extra)
    env = base_env(ctx.home, job_git_env())
    env.update(extra or {})
    p = ctx.ex.run(ctx, ["git", *args], env, ctx.repo_dir, timeout)
    if check and p.returncode:
        raise Failed(f"git {' '.join(args[:2])} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


BUNDLE_MAX = 2 * 1024 ** 3   # maximum size of the commit pack read from the workspace


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def outbound_proxy_env():
    """Egress proxy for the runner itself (enterprise proxy), reused for the mirror git: without it, the runner
    would not reach a Git host located outside the network. Never passed to jobs, which have their own allowlist proxy."""
    return {k: os.environ[k] for k in PROXY_VARS if os.environ.get(k)}


NFS_WAIT = float(os.environ.get("FABRIQUE_NFS_WAIT", "90"))   # seconds, at most, to see a file written elsewhere


def appeared(path, wait=None, sleep=time.sleep):
    """Shared working volume (NFS): a file that the job Pod just wrote, from another node, may remain
    invisible for a moment to the runner (NFS client name caching). We re-read the parent directories, which forces
    the client to revalidate them, and we retry. Costs nothing when the file is already visible."""
    end = time.time() + (NFS_WAIT if wait is None else wait)
    while True:
        if os.path.lexists(path):
            return True
        if time.time() >= end:
            return False
        for d in (os.path.dirname(path), os.path.dirname(os.path.dirname(path))):
            try:
                os.listdir(d)
            except OSError:
                pass
        sleep(0.5)


def mgit(ctx, args, secret_env=None, check=True, timeout=300, errors=None):
    """Git in the runner mirror (never in the agent workspace), the only place where the token is given to git.
    errors="replace" : for output that may contain something other than UTF-8 (the content of a repo file)."""
    env = {"PATH": SYS_PATH, "HOME": ctx.mirror, "LANG": "C.UTF-8", **outbound_proxy_env(), **git_env(provider_cfg(secret_env))}
    p = subprocess.run([GIT, "--git-dir", ctx.mirror, *args], env=env, capture_output=True, text=True, errors=errors, timeout=timeout, cwd=ctx.mirror)
    if check and p.returncode:
        raise Failed(f"git {args[0]} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def job(ctx, cmd, timeout=600, check=True):
    """Repo code (installation, checks): always by the executor, never in the runner."""
    p = ctx.ex.run(ctx, ["sh", "-c", cmd], base_env(ctx.home), ctx.repo_dir, timeout)
    if check and p.returncode:
        raise Failed(f"{cmd} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


NETWORK_CHECK = M("Network")       # job network egress line in the evidence (the dashboard tells the lines apart by their kind, not by this name)
SECURITY_CHECK = M("Security")   # security check line name in the evidence (the dashboard tells the lines apart by their kind, not by this name)

# Presentation files: a repo containing only these does not yet have a project to analyze.
BARE_OK = re.compile(r"^(readme|licen[sc]e|copying|notice|changelog|authors|contributing|code_of_conduct|security)(\.[a-z0-9]+)?$"
                     r"|^\.git(ignore|attributes|keep)$|^\.editorconfig$", re.I)


def bare(ctx):
    """Nothing to analyze: the main branch (read in the runner mirror) only contains presentation files
    (README, license, .gitignore...). Such a repo is treated as an empty repo: we initialize it, we do not invent a recipe."""
    p = mgit(ctx, ["ls-tree", "-r", "--name-only", ctx.base_sha], check=False)
    files = [f for f in p.stdout.splitlines() if f.strip()]
    return p.returncode == 0 and len(files) <= 20 and all("/" not in f and BARE_OK.match(f) for f in files)


class Runner:
    def __init__(self, panel=None, forge=None, ex=None):
        self.panel, self._forge, self._forges = panel or Panel(), forge, {}
        self.ex = ex or executor.make()
        self.relay = Relay(self.panel, config.RELAY_PORT, config.RELAY_HOST)
        self.gateway = Gateway(self.relay.ctx_for, config.GATEWAY_PORT, config.RELAY_HOST,
                               on_slow=lambda ctx, msg: self.panel.event(ctx.ref, type="log", kind=M("model"), message=msg),
                               on_runaway=self.runaway)
        self.active, self.lock, self.stop = 0, threading.Lock(), threading.Event()
        self.running = {}   # ref -> Ctx, for stops requested from the dashboard

    # ---------- loop ----------
    def harnesses(self):
        try:
            v = subprocess.run([config.CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=20).stdout.split()[0]
        except Exception:
            v = "?"
        out = [{"name": "Claude Code", "version": v}]
        if config.EXECUTOR == "docker" or shutil.which(config.CODEX_BIN):   # Codex is in the job images
            out.append({"name": "Codex", "version": "0.167.2" if config.EXECUTOR == "docker" else "local"})
        return out

    def metrics(self):
        try:
            load = os.getloadavg()[0] / (os.cpu_count() or 1) * 100
            st = shutil.disk_usage(config.WORK_DIR)
            mem = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines() if ":" in l)
            tot, av = int(mem["MemTotal"].split()[0]) / 1e6, int(mem["MemAvailable"].split()[0]) / 1e6
            return {"cpu": round(load), "mem": [round(tot - av, 1), round(tot, 1)], "disk": [round(st.free / 1e9), round(st.total / 1e9)]}
        except Exception:
            return {}

    def orders(self, res):
        """Orders received with the heartbeat: stops requested, and jobs to be paused (daily budget reached)."""
        for ref in res.get("cancel") or []:
            self.cancel(ref)
        for ref in res.get("purge") or []:
            self.purge(ref)
        for ref in res.get("over_budget") or []:
            ctx = self.running.get(ref)
            if ctx and ctx.in_agent and ctx.run and not ctx.paused and not ctx.run.error:
                log.info("%s: daily budget reached, agent paused", ref)
                ctx.paused = True
                ctx.run._kill(M("daily budget reached"))

    def workspaces(self):
        """Working directories present on this runner (one per job), so the dashboard can say which ones are no longer used."""
        try:
            return sorted(n for n in os.listdir(config.WORK_DIR) if WORKSPACE.fullmatch(n) and n not in self.running)[:500]
        except OSError:
            return []

    def purge(self, ref):
        """Deletes the working directory and the mirror of a finished or deleted job. Never a running job, never a path
        outside the working directory: the name must follow the job reference format."""
        if not WORKSPACE.fullmatch(str(ref)) or ref in self.running:
            return
        paths = [os.path.join(config.WORK_DIR, ref), os.path.join(config.WORK_DIR, ".mirrors", f"{ref}.git")]
        try:
            subprocess.run(as_agent() + ["sh", "-c", 'chmod -R u+rwx -- "$1" "$2" 2>/dev/null; rm -rf -- "$1" "$2"', "purge", *paths],
                           cwd=config.WORK_DIR, env={"PATH": SYS_PATH}, capture_output=True, timeout=300)
            for p in paths:   # possible leftover created by the runner itself (not by the agent user)
                shutil.rmtree(p, ignore_errors=True)
            try:
                os.remove(os.path.join(config.WORK_DIR, ".mirrors", f"{ref}.setup.json"))
            except OSError:
                pass
            log.info("%s: working directory removed (job finished or deleted)", ref)
        except Exception as e:
            log.warning("%s: cannot clean the working directory (%s)", ref, e)

    def beat(self):
        h = self.harnesses()
        last_list = 0.0
        while not self.stop.is_set():
            try:
                info = {"host": config.HOST, "version": config.VERSION, "capacity": config.CAPACITY,
                        "in_flight": self.active, "harnesses": h, "metrics": self.metrics(), "tags": config.TAGS}
                if time.time() - last_list > 3600:   # once per hour: cleaning of working directories
                    last_list = time.time()
                    info["workspaces"] = self.workspaces()
                res = self.panel.heartbeat(info)
                self.orders(res)
            except Exception as e:
                log.warning("heartbeat failed: %s", e)
            self.stop.wait(20)

    def loop(self):
        os.makedirs(config.WORK_DIR, exist_ok=True)
        write_global_config()
        self.relay.serve()
        self.gateway.serve()
        threading.Thread(target=self.beat, name="beat", daemon=True).start()
        log.info("runner ready (%s), dashboard %s", config.HOST, config.PANEL_URL)
        while not self.stop.is_set():
            if self.active < config.CAPACITY:
                try:
                    item = self.panel.claim()
                except Exception as e:
                    log.warning("queue unavailable: %s", e)
                    item = None
                if item:
                    with self.lock:
                        self.active += 1
                    threading.Thread(target=self.execute, args=(item,), name=item["job"]["ref"], daemon=True).start()
                    continue
            self.stop.wait(5)

    def cancel(self, ref):
        ctx = self.running.get(ref)
        if ctx and not ctx.cancelled:
            log.info("%s: stop requested from the dashboard", ref)
            ctx.cancelled = True
            if ctx.run:
                ctx.run.cancel("the dashboard")

    def execute(self, item):
        ctx = Ctx(item)
        ctx.ex = self.ex
        kind = ctx.intent["kind"]
        self.running[ctx.ref] = ctx
        try:
            {"start": self.start, "resume": self.resume, "merge": self.merge, "review": self.rereview, "onboard": self.onboard,
             "init": self.init_repo, "create": self.create_repo, "fix": self.fix, "close": self.close_pr}[kind](ctx)
        except Waiting as w:
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="onboard_wait", reason=str(w))
            self.panel.event(ctx.ref, type="intent_done", note=f"waiting: {w}")
        except Paused:
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="intent_done", note=M("paused: daily budget reached"))
        except Cancelled:
            self.ex.kill(ctx)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="cancelled")
            self.panel.event(ctx.ref, type="intent_done", note=M("stopped from the dashboard"))
        except Closed as e:
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="closed", reason=str(e)[:400])
            self.panel.event(ctx.ref, type="intent_done", note=M("closed without change"))
        except Failed as e:
            log.warning("%s: failed: %s", ctx.ref, e)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="failed", reason=str(e)[:400], attempts=ctx.attempt)
            self.panel.event(ctx.ref, type="intent_done", note=str(e)[:400])
        except Exception as e:
            log.exception("%s: unexpected error", ctx.ref)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="failed", reason=M("runner error: {name}", name=type(e).__name__), attempts=ctx.attempt)
            self.panel.event(ctx.ref, type="intent_done", note=repr(e)[:400])
        finally:
            self.close(ctx, kind)
            self.running.pop(ctx.ref, None)
            if ctx.fab_token:
                self.relay.forget(ctx.fab_token)
            with self.lock:
                self.active -= 1

    # ---------- steps ----------
    def ensure_mirror(self, ctx):
        """Bare mirror of the runner: created by the runner, readable but not writable by the agent, never mounted in a job."""
        os.makedirs(os.path.dirname(ctx.mirror), mode=0o755, exist_ok=True)
        write_global_config()
        if not os.path.isfile(os.path.join(ctx.mirror, "HEAD")):
            subprocess.run([GIT, "init", "-q", "--bare", ctx.mirror], env={"PATH": SYS_PATH, **git_env()}, check=True,
                           capture_output=True)
        os.chmod(ctx.mirror, 0o755)

    def fetch_mirror(self, ctx, branch=False):
        """Main branch (and job branch if requested) fetched FROM THE GIT HOST into the mirror, with the token; the base
        of the diffs and the recipe are read there, on the pinned sha, never in the agent's workspace."""
        g, br = self.git(ctx), ctx.repo["default_branch"]
        self.ensure_mirror(ctx)
        p = mgit(ctx, ["fetch", "-q", "--no-tags", "--force", g.clone_url(ctx.repo["slug"]), f"+refs/heads/{br}:refs/heads/{br}"],
                 secret_env=g.git_env(), check=False)
        if p.returncode:
            raise Failed(M("cannot clone {slug}: the runner's {p} token probably has no access to this repo (or branch {br} does not exist). Give it access, then restart. Detail: ", slug=ctx.repo['slug'], p=ctx.repo.get('provider') or 'Git', br=br) + ((p.stderr or p.stdout).strip()[-200:] or f"code de sortie {p.returncode}"))
        mgit(ctx, ["symbolic-ref", "HEAD", f"refs/heads/{br}"])
        ctx.base_sha = mgit(ctx, ["rev-parse", "--verify", f"refs/heads/{br}^{{commit}}"]).stdout.strip()
        if branch:
            p = mgit(ctx, ["fetch", "-q", "--no-tags", "--force", g.clone_url(ctx.repo["slug"]), f"+refs/heads/{ctx.branch}:refs/heads/{ctx.branch}"],
                     secret_env=g.git_env(), check=False)
            return p.returncode == 0
        return False

    def fork(self, ctx):
        """Commit the job's workspace started from. On a job resumed after a question, the main branch may have moved in
        the meantime (other pull requests merged): its new head does not exist in the workspace, and every command run there
        against it failed ("bad object"). The starting point is written once, next to the mirror (owned by the runner)."""
        return ctx.fork_sha or ctx.base_sha

    def remember_fork(self, ctx, cloned, has_branch):
        path = os.path.join(ctx.mirror, "fabrique-fork")
        if cloned:
            sha = ctx.base_sha
            if has_branch:   # workspace rebuilt from the pushed branch: its starting point is where the branch left main
                p = mgit(ctx, ["merge-base", ctx.base_sha, f"refs/heads/{ctx.branch}"], check=False)
                sha = p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else ctx.base_sha
            try:
                with open(path, "w") as f:
                    f.write(sha)
            except OSError as e:
                log.warning("%s: cannot record the starting commit (%s)", ctx.ref, e)
            ctx.fork_sha = sha
            return
        try:
            sha = open(path).read().strip()
        except OSError:
            sha = ""
        if re.fullmatch(r"[0-9a-f]{40,64}", sha) and mgit(ctx, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0:
            ctx.fork_sha = sha
        else:
            # Workspace from a version that did not record it: the newest commit of main the job's branch already contains
            # is unknown here; the old main head is still what the workspace was cloned from only if main did not move.
            ctx.fork_sha = ctx.base_sha

    def capture_head(self, ctx, trusted=False):
        """Imports the head commit of the workspace into the mirror; returns its sha. The bundle of commits since
        the base is written on the job side (jgit); the runner only reads it as data: it opens it without following
        a link, requires an ordinary file, copies it into its own directory, and it is this copy that its git imports
        (objects verified upon receipt). "trusted": repo that the runner just created itself, without an agent run."""
        bundle = os.path.join(ctx.dir, ".tete.bundle")
        sh(ctx, ["rm", "-rf", bundle], cwd=ctx.dir)
        fork = self.fork(ctx)
        args = ["bundle", "create", "-q", bundle, "HEAD"] + ([f"^{fork}"] if fork else [])
        p = sh(ctx, ["git", *args], check=False) if trusted else jgit(ctx, args, check=False)
        if p.returncode:
            if "empty bundle" in (p.stderr + p.stdout):   # no commits since the starting point
                return fork
            raise Failed(M("cannot read the agent's commit: {p}", p=(p.stderr or p.stdout).strip()[-300:]))
        safe = ctx.mirror + ".bundle"   # mirror directory: owned by the runner, never writable by the agent
        appeared(bundle)               # written by the job Pod, possibly on another node
        try:
            fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            raise Failed(M("commit bundle unreadable ({why})", why=e.strerror))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > BUNDLE_MAX:
                raise Failed(M("commit bundle refused: it is not a regular file, or it is too large"))
            try:
                with os.fdopen(os.dup(fd), "rb") as src, open(safe, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                mgit(ctx, ["fetch", "-q", "--no-tags", "--force", safe, "+HEAD:refs/fabrique/head"])
            finally:
                try:
                    os.remove(safe)
                except OSError:
                    pass
        finally:
            os.close(fd)
        sh(ctx, ["rm", "-f", bundle], cwd=ctx.dir, check=False)
        return mgit(ctx, ["rev-parse", "--verify", "refs/fabrique/head^{commit}"]).stdout.strip()

    def wipe(self, ctx):
        """Clears the job workspace before recreating it. If anything remains (a directory that the agent
        made non-writable, for example), we stop: a remnant of the agent must never end up in a repo
        that the runner is about to create and push."""
        sh(ctx, ["sh", "-c", 'chmod -R u+rwx -- "$1" "$2" 2>/dev/null; rm -rf -- "$1" "$2"', "wipe", ctx.repo_dir, ctx.home],
           cwd=config.WORK_DIR, check=False)
        if os.path.lexists(ctx.repo_dir) or os.path.lexists(ctx.home):
            raise Failed(M("the job's workspace cannot be emptied: delete it on the runner before restarting"))

    def prepare(self, ctx, fresh, onboarding=False, branch=False):
        # Without a relayable key, no point in cloning, starting a container, and replaying the installation.
        from .gateway import uses_gateway
        reviewers = [ctx.review_model, *ctx.review_models.values()]
        for m in (ctx.model, *reviewers):
            if m.get("provider") == "anthropic" and not uses_gateway(m) and not (m is not ctx.model and review.mode(m) == "direct"):
                raise Failed(M("no API key for the Anthropic model {id}: enter it in Settings → Agents and models (or ANTHROPIC_API_KEY on the runner)", id=m.get('id')))
        if fresh and os.path.isdir(ctx.dir):
            self.wipe(ctx)
        os.makedirs(ctx.dir, exist_ok=True)
        os.chmod(ctx.dir, 0o777)
        sh(ctx, ["mkdir", "-p", ctx.home], cwd=ctx.dir)
        cloned = fresh or not os.path.isdir(os.path.join(ctx.repo_dir, ".git"))
        if onboarding:
            self.ensure_mirror(ctx)
            self.reachable(ctx)
        has_branch = self.fetch_mirror(ctx, branch=branch or (cloned and not fresh))
        if cloned:
            if self.ex.name == "local" and self.active <= 1:
                # Shared workshop mode only, and only if no other job is running: otherwise its processes would be killed
                # (seen in FAB-266: cloning of a job killed by the preparation of another). In docker mode, each job has its own container.
                kill_agent_leftovers()
            # Clone from the mirror (without token); origin then points to the Git host, for the agent (which cannot push).
            sh(ctx, ["git", "clone", "--quiet", "--no-hardlinks", "--branch", ctx.repo["default_branch"], ctx.mirror, ctx.repo_dir], cwd=ctx.dir, timeout=300)
            sh(ctx, ["git", "remote", "set-url", "origin", self.git(ctx).clone_url(ctx.repo["slug"])])
            if has_branch:   # workspace lost, or review: resume from the pushed branch
                sh(ctx, ["git", "checkout", "-q", "-B", ctx.branch, f"origin/{ctx.branch}"])
            else:
                sh(ctx, ["git", "checkout", "-q", "-B", ctx.branch])
        self.remember_fork(ctx, cloned, has_branch)
        # The recipe is read from the mirror, at the pinned sha of the main branch; never in the agent's space.
        p = mgit(ctx, ["show", f"{ctx.base_sha}:.fabrique/setup.json"], check=False)
        if onboarding:
            ctx.onboarding = True
            ctx.recipe = {"existing": p.stdout if p.returncode == 0 else ""}
            ctx.image = detect_image(ctx.repo_dir)
        elif p.returncode:
            raise Failed(M("no .fabrique/setup.json recipe on the main branch: onboarding needed"))
        else:
            try:
                ctx.recipe = validate_recipe(json.loads(p.stdout))
            except ValueError as e:
                raise Failed(M("invalid .fabrique/setup.json recipe on the main branch: {e}", e=e))
            ctx.image = ctx.recipe.get("image") or "python"
        self.ex.start(ctx)
        self.panel.event(ctx.ref, type="log", kind=M("execution"), message=M("the job's disposable container") if self.ex.name == "docker" else M("the runner's shared workshop"))
        if cloned:
            for cmd in ctx.recipe.get("setup") or []:
                self.panel.event(ctx.ref, type="log", kind="installation", message=cmd[:200])
                job(ctx, cmd, timeout=900)
            if not onboarding:
                self.snapshot_setup(ctx)
        ctx.fab_token = self.relay.register(ctx)

    def setup_state_path(self, ctx):
        """Where the state of the workspace after installation is kept: next to the runner's mirror, out of the agent's reach."""
        return os.path.join(config.WORK_DIR, ".mirrors", f"{ctx.ref}.setup.json")

    def snapshot_setup(self, ctx):
        """What the recipe's installation created or changed in the repo (a package-lock.json written by npm install, for
        example), with the content of each file: such a file is never committed by the runner unless the agent changed it
        (FAB-15: a 1,431-line lock file nobody asked for made the PR too large)."""
        try:
            files, dirs = workspace_changes(ctx)
            hashes = file_hashes(ctx, files)
            with open(self.setup_state_path(ctx), "w") as fh:
                json.dump({"files": hashes, "dirs": dirs}, fh)
        except Exception as e:   # never blocking: at worst, the runner commits as before
            log.warning("%s: state after installation not recorded (%s)", ctx.ref, e)

    def setup_excludes(self, ctx):
        """Pathspecs excluding, from the runner's own commit, what the installation produced and the agent left unchanged."""
        try:
            with open(self.setup_state_path(ctx)) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return []
        files, dirs = workspace_changes(ctx)
        now = file_hashes(ctx, [f for f in files if f in st.get("files", {})])
        keep_out = [f for f, h in now.items() if st["files"].get(f) == h]
        keep_out += [d for d in dirs if d in st.get("dirs", [])]
        keep_out += [f for f in files if any(f.startswith(d) for d in st.get("dirs", []))]
        return [f":(exclude,literal){f.rstrip('/')}" for f in sorted(set(keep_out))]

    def reachable(self, ctx):
        """Onboarding: is the repo reachable by the runner token, and does it have its main branch? If not, the job
        waits (the dashboard retries automatically) instead of failing: a human might be finishing the configuration."""
        g = self.git(ctx)
        p = mgit(ctx, ["ls-remote", "--heads", g.clone_url(ctx.repo["slug"])], secret_env=g.git_env(), timeout=120, check=False)
        why = ""
        if p.returncode:
            try:
                who = g.account()
            except Exception:
                who = ""
            why = "access:" + who
        elif f"refs/heads/{ctx.repo['default_branch']}" not in p.stdout:
            why = "empty" if not p.stdout.strip() else "branch"
        if why:
            raise Waiting(why)

    def agent(self, ctx, prompt, resume=None, system=None):
        """An agent run. False if it asked a question (the job is waiting). Restarts once if it remains silent."""
        if ctx.cancelled:
            raise Cancelled
        self.settle(ctx)   # expense made outside of a run (checks, process left by a check): counted, never erased
        ctx.in_agent = True
        try:
            run = runner_for(ctx)(ctx, self.panel, prompt, resume, system=system or ctx.system).run()
        finally:
            ctx.in_agent = False
        self.ex.kill(ctx)
        # Cost: the greater of what the agent declares and what the key gateway measured during the run (the
        # calls made directly to the gateway by the job's code also count).
        with self.gateway.lock:
            gw = ctx.gw_spent - ctx.gw_mark
            ctx.gw_mark = ctx.gw_spent
        ctx.base_cost += max(run.cost, gw)
        if gw > run.cost:
            self.report_usage(ctx)
        ctx.base_tokens += run.tokens
        ctx.job["session_id"] = run.session_id or ctx.job.get("session_id")
        if ctx.cancelled or run.cancelled:
            raise Cancelled
        if ctx.paused:
            # Daily budget: nothing is lost (commits and files remain in the workspace, the session is recorded);
            # the dashboard queues a resume, served when the budget allows.
            if ctx.onboarding:
                raise Failed(M("daily budget reached during onboarding: restart it from the repo"))
            self.report_usage(ctx)
            self.panel.event(ctx.ref, type="paused", session_id=ctx.job.get("session_id") or "")
            raise Paused
        if not (ctx.question or ctx.gave_up or ctx.decision) and not run.stalled and not run.error:
            # A model sometimes WRITES `fab question "…"` in its answer instead of running it: the job would then go on
            # without the question (or fail for lack of change). The runner reads the last command written.
            w = messages.written_command(run.result)
            if w and w[0] == "question":
                ctx.question = w[1][:4000]
                self.panel.event(ctx.ref, type="log", kind="runner",
                                 message=M("the agent wrote its question in its answer instead of running the command: read by the runner"))
            elif w and w[0] == "fail":
                ctx.gave_up = w[1][:1000]
        if ctx.gave_up:
            raise Failed(M("the agent gives up: {why}", why=ctx.gave_up))
        if ctx.question:
            self.panel.event(ctx.ref, type="question", question=ctx.question, session_id=run.session_id or "")
            self.panel.event(ctx.ref, type="intent_done", note=M("question asked"))
            return False
        if run.stalled:
            if ctx.nudged:
                raise Failed(M("no progress after a watchdog restart ({watchdog_min} min)", watchdog_min=ctx.watchdog_min))
            ctx.nudged = True
            self.panel.event(ctx.ref, type="log", kind="watchdog", message=M("agent silent for {n} min: restarted", n=ctx.watchdog_min))
            return self.agent(ctx, messages.nudge(ctx.job, ctx.watchdog_min), resume=ctx.job.get("session_id"), system=system)
        if run.error:
            trouble = messages.model_trouble(run.error)
            if trouble == "too_large":
                raise Failed(M("this conversation no longer fits in the model's context, waiting would not help: raise the "
                               "context on the model server, or split the request into smaller ones ({why})", why=str(run.error)[:200]))
            if "error_max_turns" in str(run.error) and not ctx.onboarding:
                # Turn limit of the agent run: the work done so far is kept (files, commits, session), the agent picks up where
                # it stopped. A failure only after config.MAX_TURN_RESUMES resumes: a model that keeps turning without
                # finishing is not given an endless run (FAB-25: a whole module stopped at turn 60, its tests nearly green).
                if ctx.turn_resumes < config.MAX_TURN_RESUMES and ctx.job.get("session_id"):
                    ctx.turn_resumes += 1
                    self.report_usage(ctx)
                    self.panel.event(ctx.ref, type="log", kind="runner",
                                     message=M("turn limit of the agent reached ({turns} turns): the agent resumes where it stopped "
                                               "({n}/{max})", turns=config.MAX_TURNS, n=ctx.turn_resumes, max=config.MAX_TURN_RESUMES))
                    return self.agent(ctx, messages.more_turns(ctx.job), resume=ctx.job.get("session_id"), system=system)
                raise Failed(M("the agent did not finish after {n} runs of {turns} turns: split the request into smaller ones, or "
                               "give this repo a stronger model", n=ctx.turn_resumes + 1, turns=config.MAX_TURNS))
            if trouble == "cut":
                raise Failed(M("the model stopped at its output limit without finishing: it loops in its thinking or its "
                               "context is full. Give this repo a stronger model (Settings → Agents and models) or split "
                               "the request into smaller ones ({why})", why=str(run.error)[:200]))
            if trouble == "busy" and not ctx.onboarding:
                waits = config.MODEL_WAITS
                if ctx.model_waits >= len(waits):
                    raise Failed(M("the model server stayed saturated or unreachable after {n} attempts: limit the simultaneous "
                                   "jobs on this model (Settings → Agents and models), or raise its context if this "
                                   "conversation is too large for it ({why})", n=len(waits), why=str(run.error)[:200]))
                seconds = waits[ctx.model_waits]
                ctx.model_waits += 1
                self.report_usage(ctx)
                self.panel.event(ctx.ref, type="model_wait", attempt=ctx.model_waits, attempts=len(waits), seconds=seconds,
                                 why=str(run.error)[:300], session_id=ctx.job.get("session_id") or "")
                for _ in range(seconds):
                    if ctx.cancelled or self.stop.is_set():
                        raise Cancelled
                    time.sleep(1)
                self.panel.event(ctx.ref, type="model_back")
                again = messages.resume_busy(ctx.job) if ctx.job.get("session_id") else prompt
                return self.agent(ctx, again, resume=ctx.job.get("session_id"), system=system)
            raise Failed(run.error)
        ctx.model_waits = 0   # a run that ended normally: the next saturation starts its attempts again
        return True

    def runaway(self, ctx, streak):
        """Agent responses keep stopping at the output limit: the model loops in its thinking, or the conversation fills
        its context. The run is stopped with the cause rather than left asking again for an hour."""
        run = getattr(ctx, "run", None)
        if run is None or getattr(run, "done", True):
            return
        run._kill(M("the model stopped {n} times in a row at its output limit without finishing: it loops in its thinking "
                    "or its context is full. Asking again would not help: give this repo a stronger model (Settings → "
                    "Agents and models) or split the request into smaller ones", n=streak))

    def close(self, ctx, kind):
        """End of an order, regardless of the outcome: the job container is destroyed (no more calls possible with
        its token), then the still uncounted expense is reported — BEFORE the event that closes the order, after
        which the dashboard no longer accepts anything from this runner for this job. No effect the second time."""
        if getattr(ctx, "closed", False):
            return
        ctx.closed = True
        if kind != "merge":
            self.ex.stop(ctx)   # the workspace remains, for a resume
        try:
            self.settle(ctx)
        except Exception:
            pass

    def settle(self, ctx):
        """Expense measured by the key gateway and not yet counted in the job cost: added and reported to the
        dashboard (job budget and daily budget). Called before each agent run, after the checks and at the end of
        the order: a paid call made with the job token always counts, regardless of the timing."""
        with self.gateway.lock:
            extra = ctx.gw_spent - ctx.gw_mark
            ctx.gw_mark = ctx.gw_spent
            ctx.base_cost += max(0.0, extra)
        if extra > 0:
            try:
                self.report_usage(ctx)
            except Exception as e:   # the order is perhaps already closed on the dashboard side: the cost remains in the runner
                log.warning("%s: spending outside a run not reported (%s)", ctx.ref, type(e).__name__)

    def report_usage(self, ctx):
        self.panel.event(ctx.ref, type="usage", cost=round(ctx.base_cost, 4), tokens=ctx.base_tokens, session_id=ctx.job.get("session_id") or "")

    def open_prs(self, ctx):
        """PRs already open (excluding that of this job): the agent verifies during triage that none already cover the request."""
        try:
            return [p for p in self.git(ctx).open_prs(ctx.repo["slug"]) if p["branch"] != ctx.branch]
        except Exception as e:
            log.warning("%s: list of open PRs unavailable (%s)", ctx.ref, type(e).__name__)
            return []

    def start(self, ctx):
        if ctx.study:
            return self.study(ctx)
        self.prepare(ctx, fresh=True)
        if self.agent(ctx, messages.launch(ctx.job, ctx.repo, self.open_prs(ctx))):
            self.finish(ctx)

    def study(self, ctx, resumed=None):
        """Study: the agent reads the repo and produces a report. The runner captures no commits, pushes nothing and does
        not open any PR: what the agent writes in the workspace remains there, only the report is reported to the
        dashboard."""
        from .agent import STUDY
        ctx.system = STUDY
        self.prepare(ctx, fresh=resumed is None)
        sid = ctx.job.get("session_id") if resumed else None
        prompt = resumed if sid else messages.study(ctx.job, ctx.repo) + ("\n\n" + resumed if resumed else "")
        if not self.agent(ctx, prompt, resume=sid):
            return
        p = job(ctx, f"head -c {config.STUDY_MAX} STUDY_REPORT.md", timeout=60, check=False)
        report = (p.stdout if p.returncode == 0 else "").strip() or (ctx.run.result or "").strip()
        if not report:
            raise Failed(M("the agent delivered no study report"))
        self.report_usage(ctx)
        self.panel.event(ctx.ref, type="study", report=report)
        self.panel.event(ctx.ref, type="intent_done", note=M("study delivered"))

    def onboard(self, ctx):
        """Onboarding: the agent writes the recipe; the runner verifies that it only touched it, validates it, reruns it in
        the job container, then opens the PR. A human merges it (protected path .fabrique/**)."""
        from .agent import ONBOARDING
        ctx.system = ONBOARDING
        self.prepare(ctx, fresh=True, onboarding=True)
        if ctx.recipe.get("existing"):
            try:
                recipe = validate_recipe(json.loads(ctx.recipe["existing"]))
            except ValueError as e:
                raise Failed(M("the recipe already on the main branch is invalid: {e}", e=e))
            if recipe["checks"] or recipe["setup"]:
                self.panel.event(ctx.ref, type="onboarded", recipe=recipe)
                self.panel.event(ctx.ref, type="intent_done", note=M("recipe already present"))
                return
            # empty recipe (repo initialized by Fabrique): the agent writes the real recipe of the project built since then
        elif bare(ctx):
            raise Waiting("empty")   # a README, a license: no project to analyze. Same process as for an empty repo.
        if not self.agent(ctx, messages.onboard(ctx.job, ctx.repo)):
            return
        base = ctx.base_sha
        # Only the recipe is sent in the PR, regardless of what the agent committed or left (environment .venv, test files):
        # we start from the main branch with this single file; the rest is discarded and reported.
        touched = set(jgit(ctx, ["diff", "--name-only", self.fork(ctx)]).stdout.split())
        touched |= {l[3:] for l in jgit(ctx, ["status", "--porcelain", "--untracked-files=normal"]).stdout.splitlines() if len(l) > 3}
        if not appeared(os.path.join(ctx.repo_dir, ".fabrique", "setup.json")):
            raise Failed(M("the agent did not write .fabrique/setup.json"))
        jgit(ctx, ["reset", "-q", "--soft", base])
        jgit(ctx, ["reset", "-q"])
        jgit(ctx, ["add", "--", ".fabrique/setup.json"])
        if not jgit(ctx, ["diff", "--cached", "--name-only"]).stdout.strip():
            raise Failed(M("the proposed recipe is identical to the one on the main branch"))
        jgit(ctx, ["commit", "-q", "-m", W(ctx, "Fabrique recipe")])
        dropped = sorted(f for f in touched if f and f != ".fabrique/setup.json" and not f.startswith(".fabrique/setup.json"))
        if dropped:
            self.panel.event(ctx.ref, type="log", kind="onboarding",
                             message=M("left out (only the recipe goes into the PR): {join}{p}", join=', '.join(dropped[:8]), p=' …' if len(dropped) > 8 else ''))
        # The onboarding commit is imported into the mirror and verified there: a single parent (the pinned base), a single file
        # (the recipe). The validated and replayed recipe is the one from the commit (blob), not the file from the workspace.
        sanitized = self.capture_head(ctx)
        parents = mgit(ctx, ["rev-list", "--parents", "-n", "1", sanitized]).stdout.split()[1:]
        names = [f for f in mgit(ctx, ["diff", "--name-only", "-z", "--no-renames", base, sanitized]).stdout.split("\0") if f]
        if parents != [base] or names != [".fabrique/setup.json"]:
            raise Failed(M("the onboarding commit contains more than the recipe"))
        try:
            ctx.recipe = validate_recipe(json.loads(mgit(ctx, ["show", f"{sanitized}:.fabrique/setup.json"]).stdout))
        except ValueError as e:
            raise Failed(M("invalid recipe: {why}", why=str(e)))
        # Independent evidence: installation and checks replayed by the runner in a new container (recipe image).
        ctx.image = ctx.recipe.get("image") or ctx.image
        self.ex.stop(ctx)
        self.ex.start(ctx)
        jgit(ctx, ["clean", "-qfdx"])
        self.panel.event(ctx.ref, type="stage", stage="verifying", summary=M("The runner replays the proposed recipe in a fresh container."),
                         status=M("Recipe being verified"))
        for cmd in ctx.recipe["setup"]:
            self.panel.event(ctx.ref, type="log", kind="installation", message=cmd[:200])
            job(ctx, cmd, timeout=900, check=False)
        sha, checks = self.checks(ctx)
        if sha != sanitized:
            raise Failed(M("replaying the recipe changed the onboarding commit"))
        # Always sent, even without a check (project without tests): it is what sets the verified sha, without which the recipe
        # PR could never be merged by Fabrique after human approval.
        self.panel.event(ctx.ref, type="evidence", sha=sha, checks=checks)
        self.publish(ctx, sha, checks, base, ctx.attempt)

    def create_repo(self, ctx):
        """Validated creation by a human: private and empty repo in the dedicated group, then initialization (blank repo)."""
        ns, _, name = ctx.repo["slug"].rpartition("/")
        try:
            res = self.git(ctx).create_repo(ns, name, ctx.repo.get("description") or "")
        except Exception as e:
            raise Failed(M("creation of {slug} refused by the provider ({name}): the runner's token must be allowed to create repos in “{ns}”.", slug=ctx.repo['slug'], name=type(e).__name__, ns=ns))
        self.panel.event(ctx.ref, type="log", kind=M("creation"), message=f"{ctx.repo['slug']} : {res}")
        self.init_repo(ctx)

    def init_repo(self, ctx):
        """Empty repo, initialization validated by a human: first commit (README + empty recipe) pushed to the main
        branch. This is the only push from Fabrique to a main branch."""
        g = self.git(ctx)
        os.makedirs(ctx.dir, exist_ok=True)
        self.ensure_mirror(ctx)
        try:
            self.reachable(ctx)
        except Waiting as w:
            if str(w) != "empty":
                raise
            existing = False
        else:
            self.fetch_mirror(ctx, branch=False)
            if not bare(ctx) or mgit(ctx, ["show", f"{ctx.base_sha}:.fabrique/setup.json"], check=False).returncode == 0:
                self.panel.event(ctx.ref, type="log", kind="onboarding", message=M("the repo is no longer empty: normal onboarding"))
                return self.onboard(ctx)
            existing = True     # a README or a license only: we keep them, and we add the empty recipe on top
        br = ctx.repo["default_branch"]
        if os.path.isdir(ctx.dir):
            self.wipe(ctx)
        os.makedirs(ctx.dir, exist_ok=True)
        os.chmod(ctx.dir, 0o777)
        # Everything is created by the agent's user (like a clone): the runner does not write anything itself in the workspace.
        sh(ctx, ["mkdir", "-p", ctx.home], cwd=ctx.dir)
        if existing:
            sh(ctx, ["git", "clone", "--quiet", "--no-hardlinks", "--branch", br, ctx.mirror, ctx.repo_dir], cwd=ctx.dir, timeout=300)
        else:
            sh(ctx, ["git", "init", "-q", "-b", br, ctx.repo_dir], cwd=ctx.dir)
        name = ctx.repo["slug"].split("/")[-1]
        readme = W(ctx, "# {name}\n\nRepository initialized by Fabrique ({date}). The project will be built from the requests filed in Fabrique; its recipe `.fabrique/setup.json` (installation and checks) will be written by onboarding after the first merge.\n").format(name=name, date=time.strftime(W(ctx, "%Y-%m-%d")))
        recipe = json.dumps({"setup": [], "checks": {}}, indent=2) + "\n"
        # An existing README (repo created with its presentation file) is kept as is.
        sh(ctx, ["sh", "-c", 'mkdir -p .fabrique && { [ -n "$3" ] || printf "%s" "$1" > README.md; } && printf "%s" "$2" > .fabrique/setup.json',
                 "init", readme, recipe, "1" if existing else ""])
        sh(ctx, ["git", "add", "-A"])
        sh(ctx, ["git", "commit", "-q", "-m", W(ctx, "Initialized by Fabrique")])
        sha = self.capture_head(ctx, trusted=True)   # repo created just now by the runner: no agent has passed through it
        mgit(ctx, ["push", "-q", "--no-follow-tags", g.clone_url(ctx.repo["slug"]), f"{sha}:refs/heads/{br}"], secret_env=g.git_env())
        self.panel.event(ctx.ref, type="initialized", sha=sha)
        self.panel.event(ctx.ref, type="intent_done", note=M("repo initialized ({p})", p=sha[:12]))

    def resume(self, ctx):
        p = ctx.intent["payload"]
        if p.get("pause"):
            prompt = messages.resume_paused(ctx.job)
        else:
            prompt = messages.resume(ctx.job, p.get("question", "(question not passed on)"), p.get("answer", ""))
        if ctx.study:
            return self.study(ctx, resumed=prompt)
        self.prepare(ctx, fresh=False)
        sid = ctx.job.get("session_id")
        if not sid:
            prompt = messages.launch(ctx.job, ctx.repo) + "\n\n" + prompt
        if self.agent(ctx, prompt, resume=sid):
            self.finish(ctx)

    def fix(self, ctx):
        """Changes requested by a reviewer on a PR ready for review: the agent resumes on the same branch (its session if it
        still exists), with the reviews and the reviewer's note; then checks, reviews and the same PR, updated."""
        p = ctx.intent["payload"] or {}
        prompt = messages.fix_requested(p.get("reviews") or [], p.get("note") or "", p.get("by") or "")
        ctx.attempt = int(ctx.job.get("attempts") or 1) + 1
        self.prepare(ctx, fresh=False, branch=True)
        sid = ctx.job.get("session_id")
        if not sid or not os.path.isdir(ctx.home):
            sid, prompt = None, messages.launch(ctx.job, ctx.repo) + "\n\n" + prompt
        self.panel.event(ctx.ref, type="stage", stage="implementing", summary=M("Changes requested: the agent resumes on the same branch."),
                         status=M("Changes requested"))
        if self.agent(ctx, prompt, resume=sid):
            self.finish(ctx)

    def close_pr(self, ctx):
        """Merge refused in the dashboard: the PR is closed on the Git host, with the reason; nothing is merged."""
        n = ctx.job.get("pr_number")
        if n:
            note = (ctx.intent["payload"] or {}).get("note") or ""
            text = W(ctx, "Closed without merging: the merge was refused in Fabrique.") + (
                "\n\n" + W(ctx, "Reason") + ": " + messages.inert(note, 1000) if note else "")
            self.git(ctx).close_pr(ctx.repo["slug"], n, text)
        self.panel.event(ctx.ref, type="intent_done", note=M("PR closed on the Git host") if n else M("no PR to close"))

    def checks(self, ctx):
        """Recipe checks on the head commit; the sha is the one imported into the mirror, and it must not change
        during the checks (otherwise the evidence would not be valid for the pushed commit)."""
        sha = self.capture_head(ctx)
        out = []
        for name, cmd in (ctx.recipe.get("checks") or {}).items():
            t = time.time()
            p = job(ctx, cmd, timeout=config.CHECK_TIMEOUT, check=False)
            ok, txt = p.returncode == 0, (p.stdout + p.stderr)[-6000:]
            out.append({"name": name.capitalize(), "command": cmd, "ok": ok, "duration_ms": int((time.time() - t) * 1000), "output": f"$ {cmd}\n{txt}"})
        sec = self.security(ctx)
        if sec:
            out.append(sec)
        net = self.network(ctx)
        if net:
            out.append(net)
        if self.capture_head(ctx) != sha:
            raise Failed(M("the head commit changed during the checks"))
        self.settle(ctx)   # a check can call the model with the job token: this expense counts
        return sha, out

    def security(self, ctx):
        """Security check of everything the job has changed (hardcoded secrets, packages that are missing or just published),
        inside the job container. For information: always "ok", with warn when there are alerts. A job
        image without the script (old version): no line."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.fork(ctx) or ""):
            return None
        t = time.time()
        p = job(ctx, f"python3 /opt/fabrique/runner/security_check.py diff {self.fork(ctx)}", timeout=180, check=False)
        txt = (p.stdout or "").strip()
        mark = re.search(r"\nfabrique-alerts: (\d+)\s*\Z", txt)     # the script counts its alerts itself: nothing is read from its wording
        if p.returncode or not mark:
            return None
        txt, alerts = txt[:mark.start()].rstrip(), int(mark.group(1))
        ctx.security_report = txt    # transmitted to the security reviewer as clues to check (review.prompts)
        return {"name": SECURITY_CHECK, "kind": "security", "command": M("security_check.py diff (information, does not block)"), "ok": True,
                "warn": alerts > 0, "duration_ms": int((time.time() - t) * 1000), "output": txt[-6000:]}

    def network(self, ctx):
        """Network egress of the job since its start (egress proxy log): what passed, what the allowlist
        refused. For information, like the security check; warn if there were refusals."""
        r = self.ex.net(ctx) if hasattr(self.ex, "net") else None
        if not r:
            return None
        a, d = r.get("allowed") or [], r.get("denied") or []
        na, nd = sum(n for _, n in a), sum(n for _, n in d)
        head = (M("Network egress: {nd} refused, {na} allowed", nd=nd, na=na) if nd else
                M("Network egress: {na} allowed, none refused", na=na) if na else M("Network egress: none"))
        lines = [head]
        if d:
            lines += [M("Refused (outside the proxy's allowlist):")] + [f"  ⚠ {h} ({n}×)" for h, n in d]
        if a:
            lines += [M("Allowed:")] + [f"  · {h} ({n}×)" for h, n in a]
        lines.append(M("\nA refusal: a dependency to add to the proxy's allowlist, or an agent trying to reach a site it should not. For information: this check does not block the job."))
        return {"name": NETWORK_CHECK, "command": M("egress proxy log (information, does not block)"), "ok": True,
                "kind": "network", "warn": nd > 0, "duration_ms": 0, "output": "\n".join(lines)[-6000:]}

    def reviews(self, ctx, sha, base):
        """Read-only reviewers on the verified diff; returns verdicts (and reports them to the dashboard)."""
        files = [f for f in mgit(ctx, diff_args(base) + ["--name-only", "-z", "--no-renames", f"{base}...{sha}"]).stdout.split("\0") if f]
        diff = mgit(ctx, diff_args(base) + [f"{base}...{sha}"]).stdout
        truncated = len(diff) > config.REVIEW_MAX_DIFF
        if truncated:
            diff = diff[:config.REVIEW_MAX_DIFF] + M("\n[… diff truncated at {review_max_diff} characters]", review_max_diff=config.REVIEW_MAX_DIFF)
        roles = review.roles_for(files)
        ctx.tests_changed = tests_changed(ctx, base, sha)
        self.panel.event(ctx.ref, type="stage", stage="review", summary=M("Read-only review: ") + ", ".join(review.ROLES[r][0] for r in roles) + ".",
                         status=M("Review in progress"))
        # No more job processes (a check may have left one) during the review. In agent mode, the reviewer
        # reads an export of the verified commit, pulled from the runner mirror, into a new folder with an unpredictable name, with a
        # new personal folder: neither the workspace nor the HOME that the agent and the checks may have modified.
        self.ex.kill(ctx)
        ctx.review_dir = ctx.review_home = None
        try:
            if any(review.mode(review.reviewer(ctx, r)) == "agent" for r in roles):
                tag = os.urandom(8).hex()
                ctx.review_dir, ctx.review_home = os.path.join(ctx.dir, f".review-{tag}"), os.path.join(ctx.dir, f".review-home-{tag}")
                # Export via a temporary index (not "git archive", which would obey the export-ignore attributes of the
                # commit being judged). The coding agent's project settings (.claude, .mcp.json, CLAUDE.md) are removed from the export:
                # the reviewer sees them in the diff, it does not load them.
                sh(ctx, ["sh", "-c", 'mkdir "$1" "$2" && export GIT_INDEX_FILE="$2/.index" && git --git-dir "$3" read-tree "$4" '
                                     '&& cd "$1" && git --git-dir "$3" --work-tree . checkout-index -a -f && rm -f "$2/.index" '
                                     '&& rm -rf .claude .mcp.json CLAUDE.md CLAUDE.local.md', "export",
                         ctx.review_dir, ctx.review_home, ctx.mirror, sha], cwd=ctx.dir)
            return self._reviews(ctx, sha, roles, diff, truncated)
        finally:
            if ctx.review_dir:
                sh(ctx, ["rm", "-rf", ctx.review_dir, ctx.review_home], cwd=ctx.dir, check=False)
                ctx.review_dir = ctx.review_home = None

    def _review_once(self, ctx, role, diff):
        """A review; its cost is the largest of what it declares and what the key gateway measured during it."""
        self.settle(ctx)
        r = review.run(ctx, role, diff)
        with self.gateway.lock:
            gw = ctx.gw_spent - ctx.gw_mark
            ctx.gw_mark = ctx.gw_spent
            r["cost"] = max(float(r.get("cost") or 0), gw)
            ctx.base_cost += r["cost"]   # counted here, under the lock: the gateway sees this cost as soon as the review is finished
        return r

    def _reviews(self, ctx, sha, roles, diff, truncated):
        out = []
        for role in roles:
            if ctx.cancelled:
                raise Cancelled
            r = self._review_once(ctx, role, diff)
            if r["verdict"] == "undetermined":   # a second chance before escalating to a human
                self.panel.event(ctx.ref, type="log", kind=M("review"), message=M("{label}: {cause}; retrying", label=r['label'], cause=r['cause']))
                r = self._review_once(ctx, role, diff)
            r = review.confirm(r, lambda cmd: self.replay(ctx, cmd))
            if truncated and r["verdict"] == "ok":
                # The reviewer only saw part of the change: its "ok" cannot apply to the whole. A
                # human reviews (and no automatic merge is sent).
                r["verdict"] = "undetermined"
                r["summary"] = (M("Diff too long ({review_max_diff} characters read): review incomplete, a human must review it. Opinion on the part read: {summary}", review_max_diff=config.REVIEW_MAX_DIFF, summary=r['summary']))[:2000]
            self.panel.event(ctx.ref, type="review", sha=sha, role=role, verdict=r["verdict"], summary=r["summary"], points=r["points"],
                             model=review.reviewer(ctx, role)["id"])
            self.report_usage(ctx)   # the cost of reviewers counts towards the job cost and the daily budget
            out.append(r)
            if ctx.budget > 0 and ctx.base_cost > ctx.budget:
                self.panel.event(ctx.ref, type="log", kind=M("review"), message=M("job budget reached: remaining reviews stopped"))
                break
        return out

    def git(self, ctx):
        """Client of the repo Git provider (Forgejo, GitLab…), created once per provider."""
        if self._forge is not None:
            return self._forge
        p = ctx.repo.get("provider") or "forgejo"
        f = getattr(ctx, "forge", None) or {}
        key = (p, f.get("base_url") or "", hash(f.get("token") or ""))   # set in the dashboard, otherwise runner variables
        if key not in self._forges:
            g = forges.make(p, f.get("base_url"), f.get("token"))
            if not g.base or not g.token:
                raise Failed(M("provider {p}: address or token missing. Fill it in under Settings → Git providers (or in the runner's variables).", p=p))
            self._forges[key] = g
        return self._forges[key]

    def replay(self, ctx, cmd):
        """Reruns the evidence of a reviewer in the job workspace (same isolation as the agent)."""
        p = job(ctx, cmd, timeout=120, check=False)
        self.ex.kill(ctx)   # the evidence runs on the judged code: none of its processes may see the next reviewer
        return p.returncode, (p.stdout + p.stderr)[-1000:]

    def ensure_opinion(self, ctx):
        """The agent's merge opinion does not depend on the model's goodwill. A model sometimes writes `fab decision …` in
        its answer instead of running it, or stops on a malformed tool call: the runner asks once, precisely; failing
        that, it reads the command the agent wrote. False if the agent asked a question meanwhile (the job waits)."""
        if ctx.decision or ctx.study or ctx.onboarding:
            return True
        written = messages.opinion_from_text(getattr(ctx.run, "result", ""))
        if written:   # already written in the answer: no need to ask
            record_decision(ctx, self.panel, ctx.ref, written)
            self.panel.event(ctx.ref, type="log", kind="runner",
                             message=M("the agent wrote its opinion in its answer instead of running the command: read by the runner"))
            return True
        if not ctx.asked_opinion:
            ctx.asked_opinion = True
            self.panel.event(ctx.ref, type="log", kind="runner", message=M("the agent finished without giving its opinion: asked once"))
            if not self.agent(ctx, messages.ask_opinion(ctx.job), resume=ctx.job.get("session_id")):
                return False
        if not ctx.decision:
            written = messages.opinion_from_text(getattr(ctx.run, "result", ""))
            if written:
                record_decision(ctx, self.panel, ctx.ref, written)
                self.panel.event(ctx.ref, type="log", kind="runner",
                                 message=M("the agent wrote its opinion in its answer instead of running the command: read by the runner"))
            else:
                self.panel.event(ctx.ref, type="log", kind="runner", message=M("the agent gave no opinion, even when asked"))
        return True

    def ensure_trailer(self, ctx, base):
        """Every commit of the job carries `Fabrique-Job: <ref>`, whether or not the agent wrote it. Done in the job
        container, on commits that are not pushed yet (the branch is only pushed at publication, from the verified
        commit): no published history is rewritten. A failure is logged and changes nothing else."""
        try:
            p = jgit(ctx, ["log", "--format=%B%x01", f"{base}..HEAD"], check=False)
            bodies = [b for b in (p.stdout or "").split("\x01") if b.strip()]
            if p.returncode or not bodies or all(f"Fabrique-Job: {ctx.ref}" in b for b in bodies):
                return
            filt = f'git interpret-trailers --if-exists doNothing --trailer "Fabrique-Job: {ctx.ref}"'
            # jgit, not job(): in the job container, or (local workshop) in the runner with the system PATH and the fixed
            # Git configuration — never a `git` found on a path the agent controls.
            q = jgit(ctx, ["filter-branch", "-f", "--msg-filter", filt, f"{base}..HEAD"], check=False, timeout=300,
                     extra={"FILTER_BRANCH_SQUELCH_WARNING": "1"})
            if q.returncode:
                log.warning("%s: cannot add the Fabrique-Job trailer (%s)", ctx.ref, (q.stderr or q.stdout).strip()[-300:])
        except Exception as e:   # never blocks a job
            log.warning("%s: cannot add the Fabrique-Job trailer (%s)", ctx.ref, e)

    def finish(self, ctx):
        base = ctx.base_sha
        fixes = 0
        if not self.ensure_opinion(ctx):
            return
        while True:
            if ctx.cancelled:
                raise Cancelled
            if jgit(ctx, ["status", "--porcelain"]).stdout.strip():
                jgit(ctx, ["add", "-A", "--", ".", *ENV_EXCLUDES, *self.setup_excludes(ctx)])
                if jgit(ctx, ["diff", "--cached", "--name-only"]).stdout.strip():
                    jgit(ctx, ["commit", "-q", "-m", f"{ctx.ref}: the agent's remaining changes\n\nFabrique-Job: {ctx.ref}"])
            self.ensure_trailer(ctx, self.fork(ctx))
            if not mgit(ctx, ["rev-list", f"{base}..{self.capture_head(ctx)}"]).stdout.strip():
                if ctx.decision:   # it said why (already covered, dropped at the requester's request…): a clean end, not a failure
                    raise Closed(ctx.decision.get("verdict") or ctx.decision.get("interpretation") or "")
                raise Failed(M("the agent produced no change"))
            self.panel.event(ctx.ref, type="stage", stage="verifying", summary=M("The runner reruns the repo's recipe on the head commit."),
                             status=M("Checks running"))
            sha, checks = self.checks(ctx)
            self.panel.event(ctx.ref, type="evidence", sha=sha, checks=checks)
            if not all(c["ok"] for c in checks):
                if fixes >= config.FIX_ATTEMPTS:
                    raise Failed(M("checks failed after {attempt} attempts", attempt=ctx.attempt))
                fixes += 1
                ctx.attempt += 1
                self.panel.event(ctx.ref, type="stage", stage="implementing", summary=M("Checks failed: new attempt {attempt}.", attempt=ctx.attempt),
                                 status=M("Fix the checks"))
                if not self.agent(ctx, messages.fix_checks([c for c in checks if not c["ok"]]), resume=ctx.job.get("session_id")):
                    return
                continue
            verdicts = self.reviews(ctx, sha, base)
            blocking = [r for r in verdicts if r["verdict"] == "blocking"]
            if blocking and not ctx.review_fixed:
                ctx.review_fixed = True
                ctx.attempt += 1
                self.panel.event(ctx.ref, type="stage", stage="implementing", summary=M("Blocking review: one round of fixes."),
                                 status=M("Fix the review points"))
                if not self.agent(ctx, messages.fix_review(blocking), resume=ctx.job.get("session_id")):
                    return
                continue
            break
        self.publish(ctx, sha, checks, base, ctx.attempt, verdicts)

    def publish(self, ctx, sha, checks, base, attempt, verdicts=None):
        # Push and open the PR: job container destroyed (or agent processes killed); Git hooks disabled; the token
        # only exists for the duration of these commands.
        self.ex.stop(ctx)
        self.settle(ctx)   # no more container: the job expense is final, reported before the PR event
        if ctx.cancelled:
            raise Cancelled
        g = self.git(ctx)
        mgit(ctx, ["push", "-q", "--force", "--no-follow-tags", g.clone_url(ctx.repo["slug"]), f"{sha}:refs/heads/{ctx.branch}"], secret_env=g.git_env())
        files, add, rem = numstat(ctx, base, sha)
        self.send_diff(ctx, base, sha)
        d = ctx.decision or {}
        # Any text coming from the agent or the requester is rendered inert (no mention, ticket closing, image or
        # link): the PR is written and merged with the runner account.
        q, title = messages.quote, messages.one_line(ctx.title)
        body = (W(ctx, "Request **{ref}**: {title}\n\n{request}\n\n**Done when**:\n{done}\n\n### Agent's opinion\n{verdict}\n\n**Interpretation**:\n{interpretation}\n\n").format(
                    ref=ctx.ref, title=title, request=q(ctx.job['request'], 8000), done=q(ctx.job.get('done_when'), 2000) + (W(ctx, "\n\n_Default criterion: none was given with the request._") if ctx.job.get('done_when_default') else ""),
                    verdict=q(d.get('verdict'), 1000), interpretation=q(d.get('interpretation'), 4000))
                + review_guide(ctx, base, sha, files, add, rem, verdicts)
                + 
                W(ctx, "### Evidence (rerun by the runner on sha {sha})\n").format(sha=sha[:12])
                + "\n".join(f"- {'⚠️' if c.get('warn') else '✅' if c['ok'] else '❌'} {messages.inert(c['name'], 40)} `{messages.inert(c['command'], 500)}`" for c in checks)
                + W(ctx, "\n\nOpened by Fabrique. The merge decision belongs to the dashboard."))
        if ctx.cancelled:
            raise Cancelled
        n = self.git(ctx).open_pr(ctx.repo["slug"], ctx.branch, ctx.repo["default_branch"], f"[{ctx.ref}] {title}", body)
        res = self.panel.event(ctx.ref, type="pr", number=n, url=self.git(ctx).pr_url(ctx.repo["slug"], n), files=files, additions=add,
                               deletions=rem, attempts=attempt, tests_changed=tests_changed(ctx, base, sha),
                               **({"recipe": ctx.recipe} if ctx.onboarding else {})) or {}
        self.comment(ctx, n, res)
        self.panel.event(ctx.ref, type="intent_done", note=f"PR #{n}", attempts=attempt)

    def send_diff(self, ctx, base, sha):
        """Sends the diff of the verified commit to the dashboard, so a human can read it without leaving Fabrique. Read from the
        runner mirror (attributes of the base commit, no external program), capped in size, and for information only: a failed
        send does not stop the job, the PR on the Git host is the source of truth."""
        # No diff leaves the runner if its setting disables it (FABRIQUE_DIFF_MAX=0) or if the repo is declared confidential
        # in the dashboard ("Show diff in Fabrique" unchecked): the code is then copied nowhere.
        if config.DIFF_MAX <= 0 or ctx.repo.get("diff") is False:
            return
        try:
            text = mgit(ctx, ["-c", "core.quotepath=off"] + diff_args(base) + ["--no-renames", "--no-color", f"{base}...{sha}"],
                        errors="replace").stdout
            cut = len(text) > config.DIFF_MAX
            if cut:
                text = text[:text.rfind("\n", 0, config.DIFF_MAX) + 1 or config.DIFF_MAX]
            self.panel.event(ctx.ref, type="diff", sha=sha, text=text, truncated=cut)
        except Exception as e:   # never blocking
            log.warning("%s: diff not sent to the dashboard (%s)", ctx.ref, e)

    def comment(self, ctx, n, res):
        dec = (res or {}).get("decision")
        if dec:
            try:
                self.git(ctx).upsert_comment(ctx.repo["slug"], n, decision_comment(ctx, dec))
            except Exception as e:
                log.warning("%s: cannot post the decision comment (%s)", ctx.ref, e)

    def rereview(self, ctx):
        """Review restarted by a reviewer, at the verified sha, without restarting the agent; then gates re-evaluated."""
        want, n = ctx.intent["payload"].get("sha") or ctx.job.get("verified_sha") or "", ctx.job.get("pr_number")
        if not want or not n:
            raise Failed(M("nothing to review (no PR or no verified sha)"))
        self.prepare(ctx, fresh=True, branch=True)   # fresh space, from the mirror: nothing from the old space is trusted
        head = mgit(ctx, ["rev-parse", "--verify", f"refs/heads/{ctx.branch}^{{commit}}"], check=False).stdout.strip()
        if not head.startswith(want):
            raise Failed(M("the branch changed since the review ({p} instead of {want}): a new review is needed", p=head[:12], want=want))
        base = ctx.base_sha
        self.reviews(ctx, head, base)
        files, add, rem = numstat(ctx, base, head)
        self.send_diff(ctx, base, head)
        res = self.panel.event(ctx.ref, type="pr", number=n, url=ctx.job.get("pr_url") or self.git(ctx).pr_url(ctx.repo["slug"], n),
                               files=files, additions=add, deletions=rem, attempts=ctx.job.get("attempts") or 1,
                               tests_changed=tests_changed(ctx, base, head),
                               summary=M("Review rerun on sha {want}; gates re-evaluated.", want=want)) or {}
        self.comment(ctx, n, res)
        self.panel.event(ctx.ref, type="intent_done", note=M("review rerun"))

    def merge(self, ctx):
        n, want = ctx.job.get("pr_number"), (ctx.intent["payload"].get("sha") or ctx.job.get("verified_sha") or "")
        if not n or not want:
            raise Failed(M("nothing to merge (no PR or no verified sha)"))
        if ctx.cancelled:
            raise Cancelled
        pr = self.git(ctx).pr(ctx.repo["slug"], n)
        head = pr["head"]["sha"]
        if not head.startswith(want):
            raise Failed(M("the PR changed since the review ({p} instead of {want}): a new review is needed", p=head[:12], want=want))
        if ctx.cancelled:
            raise Cancelled
        # A Git host sometimes refuses a merge for a moment (two pull requests merged at the same time: "PushRejected",
        # 409): the merge is tried again a few times before giving up, and a refusal is a clean failure with the host's
        # reason, not an unexpected runner error.
        merged, waits = None, list(config.MERGE_RETRY_WAITS)
        while merged is None:
            try:
                merged = self.git(ctx).merge(ctx.repo["slug"], n, head, f"[{ctx.ref}] {messages.one_line(ctx.title)} (#{n})",
                                             message=f"Fabrique-Job: {ctx.ref}")   # the merge commit refers back to its job
            except RuntimeError as e:
                if not waits:
                    raise Failed(M("the Git host refused the merge after {n} attempts: {why}. The pull request is unchanged: "
                                   "approve the merge again to retry", n=len(config.MERGE_RETRY_WAITS) + 1, why=str(e)[:240]))
                wait = waits.pop(0)
                self.panel.event(ctx.ref, type="log", kind="runner",
                                 message=M("the Git host refused the merge for now: new attempt in {wait} s ({why})", wait=wait, why=str(e)[:200]))
                for _ in range(wait):
                    if ctx.cancelled or self.stop.is_set():
                        raise Cancelled
                    time.sleep(1)
        self.panel.event(ctx.ref, type="merged", sha=merged, branch=ctx.repo["default_branch"])
        self.panel.event(ctx.ref, type="intent_done", note=M("merged {p}", p=merged[:12]))
        sh(ctx, ["rm", "-rf", ctx.repo_dir, ctx.home], cwd=config.WORK_DIR, check=False)
        shutil.rmtree(ctx.mirror, ignore_errors=True)
        try:
            os.remove(self.setup_state_path(ctx))
        except OSError:
            pass


JOB_IMAGES = ("python", "node", "go", "java")


def detect_image(repo_dir):
    """Main language of a repo, to choose the onboarding container image (the agent can correct in the recipe)."""
    has = lambda *names: any(os.path.exists(os.path.join(repo_dir, n)) for n in names)
    if has("go.mod"):
        return "go"
    if has("pom.xml", "build.gradle", "build.gradle.kts", "gradlew"):
        return "java"
    if has("package.json") and not has("pyproject.toml", "requirements.txt", "setup.py"):
        return "node"
    return "python"


def validate_recipe(r):
    """Fabrique recipe: image (language), setup (commands), checks (name -> command), env (names and usages, without value)."""
    if not isinstance(r, dict) or set(r) - {"image", "setup", "checks", "env"}:
        raise ValueError(M("allowed sections: image, setup, checks, env"))
    if "image" in r and r["image"] not in JOB_IMAGES:
        raise ValueError(M("image: one of ") + ", ".join(JOB_IMAGES))
    setup, checks, env = r.get("setup") or [], r.get("checks") or {}, r.get("env") or []
    if not isinstance(setup, list) or len(setup) > 20 or not all(isinstance(c, str) and 0 < len(c) <= 500 for c in setup):
        raise ValueError(M("setup: a list of at most 20 commands"))
    if not isinstance(checks, dict) or len(checks) > 8 or not all(
            isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", k) and isinstance(v, str) and 0 < len(v) <= 500 for k, v in checks.items()):
        raise ValueError(M("checks: at most 8 checks name -> command"))
    if not isinstance(env, list) or len(env) > 30 or not all(
            isinstance(e, dict) and set(e) <= {"name", "purpose", "required"} and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(e.get("name", "")))
            for e in env):
        raise ValueError(M("env: names and purposes, no value"))
    out = {"setup": setup, "checks": checks, "env": env}
    if r.get("image"):
        out["image"] = r["image"]
    return out


def diff_args(base):
    """Diff in the mirror: attributes read from the pinned base (never those from the agent branch), without external program."""
    return ["-c", f"attr.tree={base}", "diff", "--no-ext-diff", "--no-textconv"]


def review_guide(ctx, base, sha, files, add, rem, verdicts):
    """PR "To review" section: estimated effort, where to start, request criteria, reviewer verdicts.
    Any text coming from a model is rendered inert."""
    per = []
    for rec in mgit(ctx, diff_args(base) + ["--numstat", "-z", "--no-renames", f"{base}...{sha}"]).stdout.split("\0"):
        a, d, f = (rec.split("\t", 2) + ["", "", ""])[:3]
        if f:
            per.append((int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0, f))
    from fnmatch import fnmatch
    prot = any(fnmatch(f, pat) for f in files for pat in (ctx.repo.get("protected_paths") or []))
    n = review.effort(add + rem, len(files), prot)
    out = [W(ctx, "### For the reviewer\n**Review effort: {n}/5 ({label})** · {files} file(s), +{add} −{rem}").format(
               n=n, label=W(ctx, review.EFFORT[n]), files=len(files), add=add, rem=rem)
           + (W(ctx, " · touches a protected path") if prot else "")]
    top = sorted(per, key=lambda x: -(x[0] + x[1]))[:3]
    if top:
        out.append(W(ctx, "**Where to start**: ") + ", ".join(f"`{messages.inert(f, 200)}` (+{a} −{d})" for a, d, f in top))
    code = next((v for v in verdicts or [] if v.get("role") == "code"), None)
    crit = review.criteria(code.get("points")) if code else []
    if crit:
        icon = {"ok": W(ctx, "✅ met"), "no": W(ctx, "❌ not met"), "unclear": W(ctx, "❓ unclear")}
        out.append(W(ctx, "**Request criteria** (according to the code reviewer):\n") + "\n".join(f"- {icon[k]}{W(ctx, ': ')}{messages.inert(t, 400)}" for k, t in crit[:10]))
    if verdicts:
        mark = {"ok": "✅", "needs_changes": W(ctx, "⚠️ to review again"), "blocking": W(ctx, "❌ blocking"), "undetermined": W(ctx, "❓ no verdict")}
        out.append(W(ctx, "**Reviews**: ") + " · ".join(f"{messages.inert(v.get('label'), 40)} {mark.get(v.get('verdict'), '')}" for v in verdicts))
    return "\n\n".join(out) + "\n\n"


def workspace_changes(ctx):
    """(files, directories) the repo's status reports as new or changed, raw paths (a whole new directory ends with "/")."""
    out = jgit(ctx, ["status", "--porcelain", "-z", "--no-renames", "--untracked-files=normal"]).stdout
    files, dirs = [], []
    for entry in [e for e in out.split("\0") if len(e) > 3]:
        path = entry[3:]
        (dirs if path.endswith("/") else files).append(path)
    return files, dirs


def file_hashes(ctx, files):
    """{path: git object id} of files that exist; a deleted file has no hash."""
    present = [f for f in files if os.path.isfile(os.path.join(ctx.repo_dir, f))]
    if not present:
        return {}
    ids = jgit(ctx, ["hash-object", "--no-filters", "--", *present]).stdout.split()
    return dict(zip(present, ids))


TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)(test_[^/]+|[^/]+_test\.[a-z0-9]+|[^/]+\.(test|spec)\.[a-z0-9]+|"
                       r"[^/]+Tests?\.(java|kt|cs))$", re.I)


def tests_changed(ctx, base, sha):
    """Test files that ALREADY existed on the main branch and that the change modifies, renames or deletes. Weakening a test
    is the easiest way to make the checks pass: a human and the reviewers are told (FAB-28 rewrote four test files)."""
    out = mgit(ctx, diff_args(base) + ["--name-status", "-z", "--no-renames", f"{base}...{sha}"], check=False).stdout.split("\0")
    pairs = list(zip(out[0::2], out[1::2]))
    return sorted({f for st, f in pairs if st[:1] in ("M", "D") and TEST_FILE.search(f)})[:50]


def numstat(ctx, base, sha):
    """Diff files and lines, raw paths (-z, without rename detection): the dashboard compares them to the protected paths."""
    files, add, rem = [], 0, 0
    for rec in mgit(ctx, diff_args(base) + ["--numstat", "-z", "--no-renames", f"{base}...{sha}"]).stdout.split("\0"):
        if not rec:
            continue
        a, d, f = (rec.split("\t", 2) + ["", "", ""])[:3]
        files.append(f)
        add += int(a) if a.isdigit() else 0
        rem += int(d) if d.isdigit() else 0
    return files, add, rem


def decision_comment(ctx, dec):
    i = messages.inert   # the details cite file paths chosen by the agent
    lim = "\n".join(f"- {'✅' if l['ok'] else '❌'} **{i(l['name'], 60)}**{W(ctx, ': ')}{i(l['detail'], 600)}" for l in dec.get("limits") or [])
    return W(ctx, "### Fabrique's decision · {ref}\n\n**Repo policy**: {policy}  \n**Status**: {status}\n\n{limits}\n\n_Gates evaluated by the dashboard, not by the agent. This comment is updated at each new review._").format(
        ref=ctx.ref, policy=i(dec.get('policy'), 60), status=i(dec.get('status'), 200), limits=lim)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # not one line per queue poll
    # All-in-one compose file: the runner token and the launcher token arrive via file, a few seconds after startup.
    for _ in range(90):
        if config.runner_token() and (config.EXECUTOR != "docker" or config._env("LAUNCHER_TOKEN") or config._file("LAUNCHER_TOKEN_FILE")):
            break
        time.sleep(2)
    config.RUNNER_TOKEN = config.runner_token()
    config.LAUNCHER_TOKEN = config._env("LAUNCHER_TOKEN") or config._file("LAUNCHER_TOKEN_FILE")
    for var in ("FABRIQUE_RUNNER_TOKEN_FILE", "LAUNCHER_TOKEN_FILE"):   # a token readable by all is also readable by the agent
        path = config._env(var)
        try:
            if path and os.stat(path).st_mode & 0o004:
                log.warning("%s (%s) is readable by every user of the container: tighten its rights (0640 or 0600) "
                            "and renew this token", var, path)
        except OSError:
            pass
    if not config.RUNNER_TOKEN:
        path = config._env("FABRIQUE_RUNNER_TOKEN_FILE")
        if path and os.path.exists(path) and not os.access(path, os.R_OK):
            raise SystemExit(f"runner token unreadable ({path}): the file is no longer world-readable. Give the runner the "
                             "dashboard's group (compose: group_add: [\"10001\"] on the runner service), then restart.")
        raise SystemExit("runner token missing (FABRIQUE_RUNNER_TOKEN or FABRIQUE_RUNNER_TOKEN_FILE)")
    if config.EXECUTOR != "docker":
        log.warning("EXECUTOR=%s: jobs run INSIDE the runner's container, separated by a plain Unix user. Development mode: this is not a security boundary (use EXECUTOR=docker).", config.EXECUTOR)
    r = Runner()
    signal.signal(signal.SIGTERM, lambda *a: r.stop.set())
    r.loop()


if __name__ == "__main__":
    main()
