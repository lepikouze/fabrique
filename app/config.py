"""Settings lus dans l'environnement au démarrage. Les settings modifiables depuis le dashboard (OIDC non secret, rôles,
agents, budgets…) vivent en base (table settings) ; l'environnement fournit les secrets et les valeurs de départ.
Une valeur fixée par l'environnement est affichée verrouillée dans les settings."""
import os


def _env(k, d=""):
    return os.environ.get(k, d).strip()


def _list(k):
    return [x.strip().lower() for x in _env(k).replace(";", ",").split(",") if x.strip()]


DATA_DIR = _env("FABRIQUE_DATA", "/data")
GENERATED = set()   # secrets absents de l'environnement, générés dans le dossier des données (signalé dans les réglages)


def _secret(env):
    """Secret donné par l'environnement (recommandé : il reste séparé de la base). Sinon il est généré une fois et gardé
    dans DATA_DIR/secrets.json (lisible par l'application seule), pour qu'une installation démarre sans réglage."""
    import json, secrets
    v = _env(env)
    if v:
        return v
    path = os.path.join(DATA_DIR, "secrets.json")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = json.load(open(path)) if os.path.exists(path) and os.path.getsize(path) else {}
        if not isinstance(data, dict):
            raise ValueError("contenu inattendu")
        if not data.get(env):
            data[env] = secrets.token_urlsafe(48)
            # Écriture atomique : fichier temporaire puis remplacement. Une écriture interrompue (disque plein, arrêt)
            # ne laisse jamais un fichier vide ou à moitié écrit à la place de l'ancien.
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        GENERATED.add(env)
        return data[env]
    except (OSError, ValueError) as e:
        # Jamais de secret vide : sans secret de session, n'importe qui signerait sa propre session. Le dashboard refuse
        # de démarrer plutôt que de tourner sans protection.
        raise SystemExit(f"{env} : ni variable d'environnement, ni secret utilisable dans {path} ({e}). Fournis {env} par "
                         "l'environnement, ou rends ce dossier inscriptible (et supprime le fichier s'il est vide ou abîmé).")
# Adresse de la base : SQLite par défaut ; PostgreSQL plus tard (postgresql+psycopg://…) sans changer le code.
DATABASE_URL = _env("DATABASE_URL") or f"sqlite:///{os.path.join(DATA_DIR, 'fabrique.db')}"
DEMO = _env("FABRIQUE_DEMO", "0") == "1"            # données et simulateur de démonstration
DEMO_TICK = float(_env("FABRIQUE_DEMO_TICK", "6"))   # secondes entre deux pas du simulateur
PUBLIC_URL_RAW = _env("PUBLIC_URL", "http://localhost:8080").rstrip("/")
# Saisie sans protocole (« fabrique.exemple.org ») : https, sinon le cookie de session ne serait pas réservé au HTTPS et
# les liens des notifications comme l'adresse de retour OIDC seraient faux.
PUBLIC_URL = PUBLIC_URL_RAW if "://" in PUBLIC_URL_RAW else ("https://" + PUBLIC_URL_RAW.lstrip("/") if PUBLIC_URL_RAW else PUBLIC_URL_RAW)
JOB_PREFIX = _env("FABRIQUE_PREFIX", "FAB")
# API des runners refusée quand la requête est passée par un proxy inverse (cas du tout-en-un, où le runner est sur le
# réseau interne) ; 0 quand les runners sont sur d'autres machines et arrivent par le proxy inverse.
RUNNER_API_INTERNAL = _env("FABRIQUE_RUNNER_API_INTERNAL", "1") == "1"
APP_VERSION = "0.7.4"                                   # numéro de lot livré
BUILD = _env("FABRIQUE_VERSION", "dev")              # sha du commit, donné à la construction de l'image
VERSION = f"v{APP_VERSION} · {BUILD}"

# Connexion OIDC. Le secret client et le secret de session ne viennent que de l'environnement, jamais de la base.
OIDC_ISSUER = _env("OIDC_ISSUER")
OIDC_CLIENT_ID = _env("OIDC_CLIENT_ID")
OIDC_CLIENT_SECRET = _env("OIDC_CLIENT_SECRET")
OIDC_SCOPES = _env("OIDC_SCOPES")
OIDC_GROUPS_CLAIM = _env("OIDC_GROUPS_CLAIM")
OIDC_ADMIN_GROUPS = _list("OIDC_ADMIN_GROUPS")
OIDC_VERIFIER_GROUPS = _list("OIDC_VERIFIER_GROUPS")
OIDC_REQUESTER_GROUPS = _list("OIDC_REQUESTER_GROUPS")
# Comptes toujours admin (secours si la correspondance des groupes est mal réglée).
FABRIQUE_ADMINS = _list("FABRIQUE_ADMINS")
SESSION_SECRET = _secret("SESSION_SECRET")
SECRET_KEY = _secret("FABRIQUE_SECRET_KEY")           # chiffre les secrets gardés en base (clés des modèles, canaux…)
# Runner intégré (compose tout-en-un) : au démarrage, le panneau crée un runner et écrit son jeton dans ce fichier, sur un
# volume partagé avec le runner seul. Vide : les runners se déclarent à la main (Runners → Nouveau runner).
BOOTSTRAP_RUNNER_FILE = _env("FABRIQUE_BOOTSTRAP_RUNNER_FILE")
# Kubernetes : le dashboard et le runner ne partagent pas de fichier. Le token du runner intégré est fourni aux deux par un
# Secret ; le dashboard enregistre le runner avec ce token au premier démarrage.
BOOTSTRAP_RUNNER_TOKEN = _env("FABRIQUE_BOOTSTRAP_RUNNER_TOKEN")
# Mode ouvert (développement et démonstration locale SEULEMENT) : sans connexion, tout visiteur est admin.
OPEN = _env("FABRIQUE_OPEN", "0") == "1"
SETUP_CODE = _env("FABRIQUE_SETUP_CODE")              # code d'installation imposé ; sinon généré et écrit dans les journaux
OIDC_SESSION_HOURS = float(_env("OIDC_SESSION_HOURS", "12"))
