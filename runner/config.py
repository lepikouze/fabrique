"""Réglages du runner, lus dans l'environnement. Les secrets (jeton du panneau, jeton Git, clé du modèle) ne sont jamais
transmis à l'agent : il tourne sous un autre utilisateur (AGENT_UID) avec un environnement minimal."""
import os, socket


def _env(k, d=""):
    return os.environ.get(k, d).strip()


PANEL_URL = _env("FABRIQUE_URL", "http://fabrique:8080").rstrip("/")
def _file(k):
    """Valeur lue dans le fichier nommé par la variable k (jeton déposé sur un volume partagé) ; vide si absent."""
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
GITLAB_TOKEN = _env("GITLAB_TOKEN")                                # jeton de projet ou de groupe, rôle Developer
GITHUB_BASE_URL = _env("GITHUB_BASE_URL", "https://github.com").rstrip("/")
GITHUB_API_URL = _env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_TOKEN = _env("GITHUB_TOKEN")                                # jeton fin (fine-grained) limité aux dépôts gérés
GIT_AUTHOR = _env("GIT_AUTHOR", "Agent Fabrique <fabrique-bot@noreply.invalid>")
WORK_DIR = _env("WORK_DIR", "/work")
CAPACITY = int(_env("CAPACITY", "1"))
# Chemins absolus : le programme de l'agent ou du vérificateur n'est jamais cherché dans un PATH que le job peut écrire.
CLAUDE_BIN = _env("CLAUDE_BIN", "/usr/local/bin/claude")
RELAY_PORT = int(_env("RELAY_PORT", "7071"))
AGENT_UID = _env("AGENT_UID")
EXECUTOR = _env("EXECUTOR", "local")                     # local (atelier partagé) | docker (un conteneur par job, via le lanceur)
LAUNCHER_URL = _env("LAUNCHER_URL", "http://fabrique-lanceur:7080")
LAUNCHER_TOKEN = _env("LAUNCHER_TOKEN") or _file("LAUNCHER_TOKEN_FILE")
RELAY_HOST = _env("RELAY_HOST", "127.0.0.1")              # 0.0.0.0 en mode docker : les conteneurs des jobs joignent le relais
RELAY_PUBLIC_URL = _env("RELAY_PUBLIC_URL", "http://fabrique-runner:7071")
GATEWAY_PORT = int(_env("GATEWAY_PORT", "7072"))           # passerelle de clés (modèles) ; même hôte que le relais
GATEWAY_PUBLIC_URL = _env("GATEWAY_PUBLIC_URL", "http://fabrique-runner:7072")
JOB_PROXY = _env("JOB_PROXY")                            # proxy de sortie donné aux jobs (mode docker)                  # vide : même utilisateur (développement seulement)
JOB_TIMEOUT = int(_env("JOB_TIMEOUT", "3600"))  # secondes par passage de l'agent
CHECK_TIMEOUT = int(_env("CHECK_TIMEOUT", "900"))
REVIEW_TIMEOUT = int(_env("REVIEW_TIMEOUT", "600"))     # par relecteur ; au-delà : « indéterminé », jamais « ok »
# direct : une seule requête au modèle par relecteur, diff fourni (rapide, sans outils) ; agent : Claude Code en lecture seule.
# Le mode direct vaut pour les modèles locaux et pour Anthropic avec clé d'API ; sinon le runner repasse en mode agent.
REVIEW_MODE = _env("REVIEW_MODE", "direct")
REVIEW_MAX_DIFF = int(_env("REVIEW_MAX_DIFF", "60000"))  # caractères de diff envoyés aux relecteurs
STUDY_MAX = 40000   # taille maximale d'un rapport d'étude (caractères)
REVIEW_MAX_TOKENS = int(_env("REVIEW_MAX_TOKENS", "8000"))  # réponse du relecteur, réflexion comprise ; coupée : 2e essai sans réflexion
ANTHROPIC_API_URL = _env("ANTHROPIC_API_URL", "https://api.anthropic.com")
MAX_TURNS = int(_env("MAX_TURNS", "60"))
FIX_ATTEMPTS = int(_env("FIX_ATTEMPTS", "2"))
WATCHDOG_SECONDS = int(_env("WATCHDOG_SECONDS", "0"))   # 0 : seuil du panneau (minutes) ; forcé en secondes pour les tests   # nouveaux passages si les contrôles échouent
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
HOST = _env("RUNNER_HOST", socket.gethostname())
TAGS = [t.strip() for t in _env("RUNNER_TAGS").split(",") if t.strip()]   # ex. « interne,docker » : dépôts qui les exigent
VERSION = _env("RUNNER_VERSION", "lot2")
# Prix indicatifs ($ par million de jetons) pour les modèles Anthropic ; un modèle local coûte 0.
PRICE_IN, PRICE_OUT, PRICE_CACHE = float(_env("PRICE_IN", "5")), float(_env("PRICE_OUT", "25")), float(_env("PRICE_CACHE_READ", "0.5"))
# Prix indicatifs ($ par million de jetons : entrée, sortie, entrée en cache), par préfixe de modèle ; le plus long gagne.
PRICES = {"claude-opus": (5, 25, 0.5), "claude-sonnet": (3, 15, 0.3), "claude-haiku": (1, 5, 0.1),
          "gpt-5-nano": (0.05, 0.4, 0.005), "gpt-5-mini": (0.25, 2, 0.025), "gpt-5": (1.25, 10, 0.125), "o": (1.1, 4.4, 0.275)}
CODEX_BIN = _env("CODEX_BIN", "/usr/local/bin/codex")


def price(model_id):
    best = max((k for k in PRICES if model_id.startswith(k)), key=len, default=None)
    return PRICES[best] if best else (PRICE_IN, PRICE_OUT, PRICE_CACHE)
