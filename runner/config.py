"""Runner settings, read from the environment. Secrets (dashboard token, Git token, model key) are never
transmitted to the agent: it runs under a different user (AGENT_UID) with a minimal environment."""
import os, socket


def _env(k, d=""):
    return os.environ.get(k, d).strip()


PANEL_URL = _env("FABRIQUE_URL", "http://fabrique:8080").rstrip("/")
def _file(k):
    """Value read from the file named by the variable k (token placed on a shared volume); empty if absent."""
    p = _env(k)
    try:
        return open(p).read().strip() if p else ""
    except OSError:
        return ""


def runner_token():
    return _env("FABRIQUE_RUNNER_TOKEN") or _file("FABRIQUE_RUNNER_TOKEN_FILE")


RUNNER_TOKEN = runner_token()
GIT_BASE_URL = _env("GIT_BASE_URL").rstrip("/")
GIT_TOKEN = _env("GIT_TOKEN")                                      # Forgejo
GITLAB_BASE_URL = _env("GITLAB_BASE_URL", "").rstrip("/")
GITLAB_TOKEN = _env("GITLAB_TOKEN")                                # project or group token, Developer role
GITHUB_BASE_URL = _env("GITHUB_BASE_URL", "https://github.com").rstrip("/")
GITHUB_API_URL = _env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_TOKEN = _env("GITHUB_TOKEN")                                # fine-grained token limited to managed repos
GIT_AUTHOR = _env("GIT_AUTHOR", "Agent Fabrique <fabrique-bot@noreply.invalid>")
WORK_DIR = _env("WORK_DIR", "/work")
CAPACITY = int(_env("CAPACITY", "1"))
# Absolute paths: the agent or reviewer program is never searched in a PATH that the job can write to.
CLAUDE_BIN = _env("CLAUDE_BIN", "/usr/local/bin/claude")
RELAY_PORT = int(_env("RELAY_PORT", "7071"))
AGENT_UID = _env("AGENT_UID")
EXECUTOR = _env("EXECUTOR", "local")                     # local (shared workshop) | docker (one container per job, via the launcher)
LAUNCHER_URL = _env("LAUNCHER_URL", "http://fabrique-launcher:7080")
LAUNCHER_TOKEN = _env("LAUNCHER_TOKEN") or _file("LAUNCHER_TOKEN_FILE")
RELAY_HOST = _env("RELAY_HOST", "127.0.0.1")              # 0.0.0.0 in docker mode: job containers reach the relay
RELAY_PUBLIC_URL = _env("RELAY_PUBLIC_URL", "http://fabrique-runner:7071")
GATEWAY_PORT = int(_env("GATEWAY_PORT", "7072"))           # key gateway (models); same host as the relay
GATEWAY_PUBLIC_URL = _env("GATEWAY_PUBLIC_URL", "http://fabrique-runner:7072")
JOB_PROXY = _env("JOB_PROXY")                            # egress proxy given to jobs (docker mode)                  # empty: same user (development only)
JOB_TIMEOUT = int(_env("JOB_TIMEOUT", "3600"))  # seconds per agent run
# Model server saturated, loading or unreachable: the job waits then resumes its session. Seconds before each new attempt.
MODEL_WAITS = [int(x) for x in _env("MODEL_WAITS", "30,60,120,240,480").split(",") if x.strip()]
# The Git host refuses a merge for a moment (two merges at once): seconds before each new attempt.
MERGE_RETRY_WAITS = [int(x) for x in _env("MERGE_RETRY_WAITS", "5,15,30").split(",") if x.strip()]
CHECK_TIMEOUT = int(_env("CHECK_TIMEOUT", "900"))
REVIEW_TIMEOUT = int(_env("REVIEW_TIMEOUT", "600"))     # per reviewer; beyond: "undetermined", never "ok"
# direct: one request to the model per reviewer, diff provided (fast, no tools); agent: Claude Code in read-only mode.
# Direct mode applies to local models and Anthropic with API key; otherwise the runner reverts to agent mode.
REVIEW_MODE = _env("REVIEW_MODE", "direct")
REVIEW_MAX_DIFF = int(_env("REVIEW_MAX_DIFF", "60000"))  # diff characters sent to reviewers
DIFF_MAX = int(_env("FABRIQUE_DIFF_MAX", "400000"))    # diff characters sent to the dashboard for human reading; 0: no sending
STUDY_MAX = 40000   # maximum size of a study report (characters)
REVIEW_MAX_TOKENS = int(_env("REVIEW_MAX_TOKENS", "8000"))  # reviewer response, thinking included; cut off: second attempt without thinking
ANTHROPIC_API_URL = _env("ANTHROPIC_API_URL", "https://api.anthropic.com")
MAX_TURNS = int(_env("MAX_TURNS", "60"))
FIX_ATTEMPTS = int(_env("FIX_ATTEMPTS", "2"))
WATCHDOG_SECONDS = int(_env("WATCHDOG_SECONDS", "0"))   # 0: dashboard threshold (minutes); forced in seconds for tests   # new runs if checks fail
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
HOST = _env("RUNNER_HOST", socket.gethostname())
TAGS = [t.strip() for t in _env("RUNNER_TAGS").split(",") if t.strip()]   # e.g. "internal,docker": repos that require them
VERSION = _env("RUNNER_VERSION", "lot2")
# Indicative prices ($ per million tokens) for Anthropic models; a local model costs 0.
PRICE_IN, PRICE_OUT, PRICE_CACHE = float(_env("PRICE_IN", "5")), float(_env("PRICE_OUT", "25")), float(_env("PRICE_CACHE_READ", "0.5"))
# Indicative prices ($ per million tokens: input, output, cached input), per model prefix; the longest wins.
PRICES = {"claude-opus": (5, 25, 0.5), "claude-sonnet": (3, 15, 0.3), "claude-haiku": (1, 5, 0.1),
          "gpt-5-nano": (0.05, 0.4, 0.005), "gpt-5-mini": (0.25, 2, 0.025), "gpt-5": (1.25, 10, 0.125), "o": (1.1, 4.4, 0.275)}
CODEX_BIN = _env("CODEX_BIN", "/usr/local/bin/codex")


def price(model_id):
    best = max((k for k in PRICES if model_id.startswith(k)), key=len, default=None)
    return PRICES[best] if best else (PRICE_IN, PRICE_OUT, PRICE_CACHE)
