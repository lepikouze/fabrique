"""Runner de la Fabrique : programme sans IA. Il prend des commandes dans la file du dashboard,
prépare un workspace jetable, lance l'agent, applique les barrières, relance lui-même les checks, pousse la
branche, ouvre la PR et merge quand le dashboard l'a décidé (épinglé au sha vérifié).

Toutes les opérations dans le workspace (clone, installation, checks, git) tournent sous l'utilisateur de
l'agent : le code du repo n'est jamais exécuté avec les droits du runner.

Le workspace (y compris son .git) est écrit par l'agent : il n'est jamais cru. Le token Git ne sert QUE dans un
miroir nu propre au runner (WORK_DIR/.miroirs/<ref>.git), que l'agent peut lire mais pas écrire : la branche principale y
est épinglée depuis la forge (recipe, base des diffs), le commit de l'agent y est importé puis vérifié, et c'est de là
qu'il est poussé. Git y tourne avec une configuration figée (aucun fichier de configuration lu hors du miroir)."""
import json, logging, os, re, shutil, signal, subprocess, threading, time, stat
from . import config, executor, messages, review
from .agent import Run, base_env, runner_for
from .executor import as_agent, kill_agent_leftovers
from . import forge as forges
from .panel import Panel
from .relay import Relay
from .gateway import Gateway

log = logging.getLogger("runner")


class Ctx:
    def __init__(self, item):
        j = item["job"]
        self.intent, self.job, self.model, self.repo = item["intent"], j, item["model"], item["repo"]
        # Relecteurs : modèle du profil, sinon celui de l'agent (contexte neuf dans les deux cas : ils ne voient que le diff).
        self.review_model = item.get("review_model") or item["model"]
        self.forge = item.get("forge")   # fournisseur Git réglé dans le panneau (adresse et jeton), ou None
        self.ref, self.title, self.branch = j["ref"], j["title"], j["branch"]
        self.budget = float(j.get("budget") or 0)
        self.base_cost, self.base_tokens = float(j.get("cost") or 0), int(j.get("tokens") or 0)
        self.dir = os.path.join(config.WORK_DIR, self.ref)
        self.repo_dir, self.home = os.path.join(self.dir, "repo"), os.path.join(self.dir, "home")
        self.question = self.decision = self.gave_up = self.run = None
        self.review_fixed = False
        self.stages = set()
        self.watchdog_min = int((item.get("limits") or {}).get("watchdog_min") or 10)
        self.nudged = self.cancelled = False
        self.study = j.get("source") == "étude"   # étude : un rapport, ni branche ni PR
        self.in_agent = self.paused = False   # passage d'agent en cours ; mis en pause (budget du jour)
        self.attempt = 1
        self.fab_token = ""
        self.recipe = None
        self.image = "python"   # image de job (liste blanche du lanceur) : rubrique « image » de la recette
        self.onboarding = False
        self.system = None   # consignes de l'agent (playbook des jobs, ou consignes d'onboarding)
        self.mirror = os.path.join(config.WORK_DIR, ".miroirs", f"{self.ref}.git")
        self.base_sha = ""   # commit de la branche principale, épinglé depuis la forge dans le miroir
        self.gw_spent = self.gw_mark = 0.0   # dépense mesurée par la passerelle de clés (toute la commande / début du passage)


# Dossiers d'environnement jamais commités par le runner (installés par la recette, pas écrits par l'agent).
ENV_EXCLUDES = [f":(exclude,glob)**/{d}/**" for d in (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
                                                     ".mypy_cache", ".tox", ".gradle", "target")]


# Git : binaire et PATH système uniquement (jamais un dossier que l'agent peut écrire), et configuration imposée par
# l'environnement, qui l'emporte sur tout fichier de configuration : aucune clé capable d'exécuter une commande ou de
# détourner le jeton ne peut venir d'un .git/config écrit par l'agent.
SYS_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WORKSPACE = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")   # nom d'un dossier de travail : la référence du job (FAB-12)
GIT = shutil.which("git", path=SYS_PATH) or "/usr/bin/git"
SAFE_GIT = {"core.hooksPath": "/dev/null", "safe.directory": "*", "core.fsmonitor": "false", "core.sshCommand": "false",
            "core.gitProxy": "", "core.askPass": "", "credential.helper": "", "diff.external": "", "http.proxy": "",
            "http.sslVerify": "true", "protocol.allow": "never", "protocol.https.allow": "always", "protocol.http.allow": "always",
            "protocol.file.allow": "always", "push.followTags": "false", "core.quotePath": "false",
            "commit.gpgSign": "false", "tag.gpgSign": "false", "gpg.program": "false", "core.attributesFile": "/dev/null"}


def global_config():
    """Configuration « globale » écrite par le runner (lisible, pas inscriptible par l'agent). Elle sert aux sous-processus que
    git lance sur un repo local (upload-pack d'un clone depuis le miroir), qui ne reçoivent pas les GIT_CONFIG_* de
    l'environnement : sans elle, le clone du miroir (au runner) par l'utilisateur de l'agent est refusé (« dubious ownership »)."""
    return os.path.join(config.WORK_DIR, ".miroirs", "gitconfig")


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
    """Variables qui figent la configuration Git : SAFE_GIT (+ clés du provider, ex. en-tête du token), aucun fichier
    système, la configuration globale du runner, pas d'objets de remplacement."""
    cfg = {**SAFE_GIT, **(extra_cfg or {})}
    env = {"GIT_CONFIG_COUNT": str(len(cfg)), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": global_config(),
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for i, (k, v) in enumerate(cfg.items()):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env


JOB_GIT = ("core.hooksPath", "core.fsmonitor", "commit.gpgSign", "tag.gpgSign", "gpg.program", "safe.directory")


def job_git_env():
    """Configuration Git passée aux commandes git lancées dans le conteneur du job : le strict utile (pas de hook, pas de
    signature), en peu de variables — le lanceur borne la taille de l'environnement d'une commande."""
    env = {"GIT_CONFIG_COUNT": str(len(JOB_GIT)), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for i, k in enumerate(JOB_GIT):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, SAFE_GIT[k]
    return env


def provider_cfg(secret_env):
    """Clés de configuration passées par le provider (GIT_CONFIG_*) -> dictionnaire."""
    secret_env = dict(secret_env or {})
    return {secret_env[f"GIT_CONFIG_KEY_{i}"]: secret_env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(secret_env.get("GIT_CONFIG_COUNT", 0) or 0))}


class Failed(Exception):
    pass


class Waiting(Exception):
    """Onboarding : repo pas encore prêt (accès, repo vide, branche absente). Le job attend, il n'échoue pas."""


class Cancelled(Exception):
    pass


class Paused(Exception):
    """Budget du jour atteint pendant le passage de l'agent : le job attend (espace de travail et session gardés)."""


def sh(ctx, cmd, timeout=600, check=True, cwd=None):
    """Fichiers du workspace et git AVANT tout passage d'agent (clone depuis le miroir, dépôt créé par le runner), DANS
    le runner, sous l'utilisateur de l'agent, SANS token, PATH système et configuration Git figée. Dès que l'agent est
    passé, son dépôt n'est plus manipulé ici : voir jgit()."""
    cmd = cmd if isinstance(cmd, list) else ["sh", "-c", cmd]
    if cmd[0] == "git":
        cmd = [GIT] + cmd[1:]
    env = base_env(ctx.home, git_env())
    env["PATH"] = SYS_PATH
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        env.pop(k, None)
    p = subprocess.run(as_agent() + cmd, cwd=cwd or ctx.repo_dir, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise Failed(f"{cmd if isinstance(cmd, str) else ' '.join(cmd[:3])} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def jgit(ctx, args, check=True, timeout=600):
    """Git sur le dépôt de l'AGENT, après son passage : exécuté par l'exécuteur, donc dans le conteneur jetable du job.
    Ce dépôt est configuré par l'agent (.git/config, attributs) : un filtre de contenu ou un programme de signature qu'il
    y déclare est lancé par git, et une liste de clés gelées ne peut pas tous les nommer. Ils tournent donc là où le code
    de l'agent tourne déjà, jamais dans le conteneur du runner. Rien de ce que ces commandes affichent ne fait foi : le
    commit est ensuite importé dans le miroir du runner et vérifié là. En atelier local (développement, pas de conteneur
    de job), on retombe sur sh() et sa configuration gelée."""
    if getattr(ctx.ex, "name", "local") != "docker":
        return sh(ctx, ["git", *args], timeout=timeout, check=check)
    env = base_env(ctx.home, job_git_env())
    p = ctx.ex.run(ctx, ["git", *args], env, ctx.repo_dir, timeout)
    if check and p.returncode:
        raise Failed(f"git {' '.join(args[:2])} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


BUNDLE_MAX = 2 * 1024 ** 3   # taille maximale du paquet de commits lu depuis l'espace de travail


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


def outbound_proxy_env():
    """Proxy de sortie du runner lui-même (proxy d'entreprise), repris pour le git du miroir : sans lui, le runner ne
    joindrait pas une forge située hors du réseau. Jamais transmis aux jobs, qui ont leur propre proxy en liste blanche."""
    return {k: os.environ[k] for k in PROXY_VARS if os.environ.get(k)}


NFS_WAIT = float(os.environ.get("FABRIQUE_NFS_WAIT", "90"))   # secondes, au plus, pour voir un fichier écrit ailleurs


def appeared(path, wait=None, sleep=time.sleep):
    """Volume de travail partagé (NFS) : un fichier que le Pod du job vient d'écrire, depuis un autre nœud, peut rester
    invisible un moment pour le runner (cache des noms du client NFS). On relit les dossiers parents, ce qui force le
    client à les revalider, et on réessaie. Ne coûte rien quand le fichier est déjà visible."""
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
    """Git dans le miroir du runner (jamais dans l'espace de l'agent), seul endroit où le token est donné à git.
    errors="replace" : pour une sortie qui peut contenir autre chose que de l'UTF-8 (le contenu d'un fichier du repo)."""
    env = {"PATH": SYS_PATH, "HOME": ctx.mirror, "LANG": "C.UTF-8", **outbound_proxy_env(), **git_env(provider_cfg(secret_env))}
    p = subprocess.run([GIT, "--git-dir", ctx.mirror, *args], env=env, capture_output=True, text=True, errors=errors, timeout=timeout, cwd=ctx.mirror)
    if check and p.returncode:
        raise Failed(f"git {args[0]} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def job(ctx, cmd, timeout=600, check=True):
    """Code du repo (installation, checks) : toujours par l'exécuteur, jamais dans le runner."""
    p = ctx.ex.run(ctx, ["sh", "-c", cmd], base_env(ctx.home), ctx.repo_dir, timeout)
    if check and p.returncode:
        raise Failed(f"{cmd} : {(p.stderr or p.stdout).strip()[-400:]}")
    return p


NETWORK_CHECK = "Réseau"       # ligne des sorties réseau du job dans les preuves (lue aussi par le dashboard)
SECURITY_CHECK = "Sécurité"   # nom de la ligne du contrôle de sécurité dans les preuves (lu aussi par le dashboard)

# Fichiers de présentation : un repo qui ne contient qu'eux n'a pas encore de projet à analyser.
BARE_OK = re.compile(r"^(readme|licen[sc]e|copying|notice|changelog|authors|contributing|code_of_conduct|security)(\.[a-z0-9]+)?$"
                     r"|^\.git(ignore|attributes|keep)$|^\.editorconfig$", re.I)


def bare(ctx):
    """Rien à analyser : la branche principale (lue dans le miroir du runner) ne porte que des fichiers de présentation
    (README, licence, .gitignore…). Un tel repo se traite comme un repo vide : on l'initialise, on n'invente pas de recipe."""
    p = mgit(ctx, ["ls-tree", "-r", "--name-only", ctx.base_sha], check=False)
    files = [f for f in p.stdout.splitlines() if f.strip()]
    return p.returncode == 0 and len(files) <= 20 and all("/" not in f and BARE_OK.match(f) for f in files)


class Runner:
    def __init__(self, panel=None, forge=None, ex=None):
        self.panel, self._forge, self._forges = panel or Panel(), forge, {}
        self.ex = ex or executor.make()
        self.relay = Relay(self.panel, config.RELAY_PORT, config.RELAY_HOST)
        self.gateway = Gateway(self.relay.ctx_for, config.GATEWAY_PORT, config.RELAY_HOST,
                               on_slow=lambda ctx, msg: self.panel.event(ctx.ref, type="log", kind="modèle", message=msg))
        self.active, self.lock, self.stop = 0, threading.Lock(), threading.Event()
        self.running = {}   # ref -> Ctx, pour les arrêts demandés depuis le panneau

    # ---------- boucle ----------
    def harnesses(self):
        try:
            v = subprocess.run([config.CLAUDE_BIN, "--version"], capture_output=True, text=True, timeout=20).stdout.split()[0]
        except Exception:
            v = "?"
        out = [{"name": "Claude Code", "version": v}]
        if config.EXECUTOR == "docker" or shutil.which(config.CODEX_BIN):   # Codex est dans les images de jobs
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
        """Ordres reçus avec le battement de cœur : arrêts demandés, et jobs à mettre en pause (budget du jour atteint)."""
        for ref in res.get("cancel") or []:
            self.cancel(ref)
        for ref in res.get("purge") or []:
            self.purge(ref)
        for ref in res.get("over_budget") or []:
            ctx = self.running.get(ref)
            if ctx and ctx.in_agent and ctx.run and not ctx.paused and not ctx.run.error:
                log.info("%s : budget du jour atteint, agent mis en pause", ref)
                ctx.paused = True
                ctx.run._kill("budget du jour atteint")

    def workspaces(self):
        """Dossiers de travail présents sur ce runner (un par job), pour que le dashboard dise lesquels ne servent plus."""
        try:
            return sorted(n for n in os.listdir(config.WORK_DIR) if WORKSPACE.fullmatch(n) and n not in self.running)[:500]
        except OSError:
            return []

    def purge(self, ref):
        """Supprime le dossier de travail et le miroir d'un job terminé ou supprimé. Jamais un job en cours, jamais un chemin
        hors du dossier de travail : le nom doit avoir la forme d'une référence de job."""
        if not WORKSPACE.fullmatch(str(ref)) or ref in self.running:
            return
        paths = [os.path.join(config.WORK_DIR, ref), os.path.join(config.WORK_DIR, ".miroirs", f"{ref}.git")]
        try:
            subprocess.run(as_agent() + ["sh", "-c", 'chmod -R u+rwx -- "$1" "$2" 2>/dev/null; rm -rf -- "$1" "$2"', "purge", *paths],
                           cwd=config.WORK_DIR, env={"PATH": SYS_PATH}, capture_output=True, timeout=300)
            for p in paths:   # reste éventuel créé par le runner lui-même (pas par l'utilisateur de l'agent)
                shutil.rmtree(p, ignore_errors=True)
            log.info("%s : dossier de travail supprimé (job terminé ou supprimé)", ref)
        except Exception as e:
            log.warning("%s : ménage du dossier de travail impossible (%s)", ref, e)

    def beat(self):
        h = self.harnesses()
        last_list = 0.0
        while not self.stop.is_set():
            try:
                info = {"host": config.HOST, "version": config.VERSION, "capacity": config.CAPACITY,
                        "in_flight": self.active, "harnesses": h, "metrics": self.metrics(), "tags": config.TAGS}
                if time.time() - last_list > 3600:   # une fois par heure : ménage des dossiers de travail
                    last_list = time.time()
                    info["workspaces"] = self.workspaces()
                res = self.panel.heartbeat(info)
                self.orders(res)
            except Exception as e:
                log.warning("battement de cœur impossible : %s", e)
            self.stop.wait(20)

    def loop(self):
        os.makedirs(config.WORK_DIR, exist_ok=True)
        write_global_config()
        self.relay.serve()
        self.gateway.serve()
        threading.Thread(target=self.beat, name="beat", daemon=True).start()
        log.info("runner prêt (%s), dashboard %s", config.HOST, config.PANEL_URL)
        while not self.stop.is_set():
            if self.active < config.CAPACITY:
                try:
                    item = self.panel.claim()
                except Exception as e:
                    log.warning("file indisponible : %s", e)
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
            log.info("%s : arrêt demandé depuis le dashboard", ref)
            ctx.cancelled = True
            if ctx.run:
                ctx.run.cancel("le dashboard")

    def execute(self, item):
        ctx = Ctx(item)
        ctx.ex = self.ex
        kind = ctx.intent["kind"]
        self.running[ctx.ref] = ctx
        try:
            {"start": self.start, "resume": self.resume, "merge": self.merge, "review": self.rereview, "onboard": self.onboard,
             "init": self.init_repo, "create": self.create_repo}[kind](ctx)
        except Waiting as w:
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="onboard_wait", reason=str(w))
            self.panel.event(ctx.ref, type="intent_done", note=f"en attente : {w}")
        except Paused:
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="intent_done", note="en pause : budget du jour atteint")
        except Cancelled:
            self.ex.kill(ctx)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="cancelled")
            self.panel.event(ctx.ref, type="intent_done", note="arrêté depuis le dashboard")
        except Failed as e:
            log.warning("%s : échec : %s", ctx.ref, e)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="failed", reason=str(e)[:400], attempts=ctx.attempt)
            self.panel.event(ctx.ref, type="intent_done", note=str(e)[:400])
        except Exception as e:
            log.exception("%s : erreur inattendue", ctx.ref)
            self.close(ctx, kind)
            self.panel.event(ctx.ref, type="failed", reason=f"erreur du runner : {type(e).__name__}", attempts=ctx.attempt)
            self.panel.event(ctx.ref, type="intent_done", note=repr(e)[:400])
        finally:
            self.close(ctx, kind)
            self.running.pop(ctx.ref, None)
            if ctx.fab_token:
                self.relay.forget(ctx.fab_token)
            with self.lock:
                self.active -= 1

    # ---------- étapes ----------
    def ensure_mirror(self, ctx):
        """Miroir nu du runner : créé par le runner, lisible mais pas inscriptible par l'agent, jamais monté dans un job."""
        os.makedirs(os.path.dirname(ctx.mirror), mode=0o755, exist_ok=True)
        write_global_config()
        if not os.path.isfile(os.path.join(ctx.mirror, "HEAD")):
            subprocess.run([GIT, "init", "-q", "--bare", ctx.mirror], env={"PATH": SYS_PATH, **git_env()}, check=True,
                           capture_output=True)
        os.chmod(ctx.mirror, 0o755)

    def fetch_mirror(self, ctx, branch=False):
        """Branche principale (et branche du job si demandé) récupérées DE LA FORGE dans le miroir, avec le token ; la base
        des diffs et la recipe sont lues là, sur le sha épinglé, jamais dans l'espace de l'agent."""
        g, br = self.git(ctx), ctx.repo["default_branch"]
        self.ensure_mirror(ctx)
        p = mgit(ctx, ["fetch", "-q", "--no-tags", "--force", g.clone_url(ctx.repo["slug"]), f"+refs/heads/{br}:refs/heads/{br}"],
                 secret_env=g.git_env(), check=False)
        if p.returncode:
            raise Failed(f"clonage de {ctx.repo['slug']} impossible : le token {ctx.repo.get('provider') or 'Git'} du runner n'a "
                         f"probablement pas accès à ce repo (ou la branche {br} n'existe pas). "
                         "Donne-lui l'accès, puis relance. Détail : " + ((p.stderr or p.stdout).strip()[-200:] or f"code de sortie {p.returncode}"))
        mgit(ctx, ["symbolic-ref", "HEAD", f"refs/heads/{br}"])
        ctx.base_sha = mgit(ctx, ["rev-parse", "--verify", f"refs/heads/{br}^{{commit}}"]).stdout.strip()
        if branch:
            p = mgit(ctx, ["fetch", "-q", "--no-tags", "--force", g.clone_url(ctx.repo["slug"]), f"+refs/heads/{ctx.branch}:refs/heads/{ctx.branch}"],
                     secret_env=g.git_env(), check=False)
            return p.returncode == 0
        return False

    def capture_head(self, ctx, trusted=False):
        """Importe le commit de tête du workspace dans le miroir ; renvoie son sha. Le paquet (bundle) des commits depuis
        la base est écrit côté job (jgit) ; le runner ne le lit que comme une donnée : il l'ouvre sans suivre de lien,
        exige un fichier ordinaire, le copie dans son propre dossier, et c'est cette copie que son git importe (objets
        vérifiés à la réception). « trusted » : dépôt que le runner vient de créer lui-même, sans passage d'agent."""
        bundle = os.path.join(ctx.dir, ".tete.bundle")
        sh(ctx, ["rm", "-rf", bundle], cwd=ctx.dir)
        args = ["bundle", "create", "-q", bundle, "HEAD"] + ([f"^{ctx.base_sha}"] if ctx.base_sha else [])
        p = sh(ctx, ["git", *args], check=False) if trusted else jgit(ctx, args, check=False)
        if p.returncode:
            if "empty bundle" in (p.stderr + p.stdout):   # aucun commit depuis la base
                return ctx.base_sha
            raise Failed(f"lecture du commit de l'agent impossible : {(p.stderr or p.stdout).strip()[-300:]}")
        safe = ctx.mirror + ".bundle"   # dossier des miroirs : au runner, jamais inscriptible par l'agent
        appeared(bundle)               # écrit par le Pod du job, éventuellement sur un autre nœud
        try:
            fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            raise Failed(f"paquet de commits illisible ({e.strerror})")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > BUNDLE_MAX:
                raise Failed("paquet de commits refusé : ce n'est pas un fichier ordinaire, ou il est trop gros")
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
        """Vide l'espace de travail d'un job avant de le recréer. S'il en reste quelque chose (un dossier que l'agent a
        rendu non inscriptible, par exemple), on s'arrête : un reste de l'agent ne doit jamais se retrouver dans un dépôt
        que le runner s'apprête à créer et à pousser."""
        sh(ctx, ["sh", "-c", 'chmod -R u+rwx -- "$1" "$2" 2>/dev/null; rm -rf -- "$1" "$2"', "wipe", ctx.repo_dir, ctx.home],
           cwd=config.WORK_DIR, check=False)
        if os.path.lexists(ctx.repo_dir) or os.path.lexists(ctx.home):
            raise Failed("espace de travail du job impossible à vider : supprime-le sur le runner avant de relancer")

    def prepare(self, ctx, fresh, onboarding=False, branch=False):
        # Sans clé relayable, inutile de cloner, de démarrer un conteneur et de rejouer l'installation.
        from .gateway import uses_gateway
        for m in (ctx.model, ctx.review_model):
            if m.get("provider") == "anthropic" and not uses_gateway(m) and not (m is ctx.review_model and review.mode(m) == "direct"):
                raise Failed(f"aucune clé d'API pour le modèle Anthropic {m.get('id')} : saisis-la dans Settings → Agents et modèles "
                             "(ou ANTHROPIC_API_KEY sur le runner)")
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
                # Mode atelier partagé seulement, et seulement si aucun autre job ne tourne : sinon on tuerait ses processus
                # (vu sur FAB-266 : le clonage d'un job tué par la préparation d'un autre). En mode docker, chaque job a son conteneur.
                kill_agent_leftovers()
            # Clone depuis le miroir (sans jeton) ; origin pointe ensuite vers la forge, pour l'agent (qui ne peut pas pousser).
            sh(ctx, ["git", "clone", "--quiet", "--no-hardlinks", "--branch", ctx.repo["default_branch"], ctx.mirror, ctx.repo_dir], cwd=ctx.dir, timeout=300)
            sh(ctx, ["git", "remote", "set-url", "origin", self.git(ctx).clone_url(ctx.repo["slug"])])
            if has_branch:   # espace de travail perdu, ou relecture : reprise depuis la branche poussée
                sh(ctx, ["git", "checkout", "-q", "-B", ctx.branch, f"origin/{ctx.branch}"])
            else:
                sh(ctx, ["git", "checkout", "-q", "-B", ctx.branch])
        # La recette est lue dans le miroir, au sha épinglé de la branche principale ; jamais dans l'espace de l'agent.
        p = mgit(ctx, ["show", f"{ctx.base_sha}:.fabrique/setup.json"], check=False)
        if onboarding:
            ctx.onboarding = True
            ctx.recipe = {"existing": p.stdout if p.returncode == 0 else ""}
            ctx.image = detect_image(ctx.repo_dir)
        elif p.returncode:
            raise Failed("pas de recipe .fabrique/setup.json sur la branche principale : onboarding à faire")
        else:
            try:
                ctx.recipe = validate_recipe(json.loads(p.stdout))
            except ValueError as e:
                raise Failed(f"recipe .fabrique/setup.json de la branche principale invalide : {e}")
            ctx.image = ctx.recipe.get("image") or "python"
        self.ex.start(ctx)
        self.panel.event(ctx.ref, type="log", kind="exécution", message="conteneur jetable du job" if self.ex.name == "docker" else "atelier partagé du runner")
        if cloned:
            for cmd in ctx.recipe.get("setup") or []:
                self.panel.event(ctx.ref, type="log", kind="installation", message=cmd[:200])
                job(ctx, cmd, timeout=900)
        ctx.fab_token = self.relay.register(ctx)

    def reachable(self, ctx):
        """Onboarding : le repo est-il joignable par le token du runner, et a-t-il sa branche principale ? Sinon le job
        attend (le dashboard réessaie tout seul) au lieu d'échouer : l'humain est peut-être en train de finir la configuration."""
        g = self.git(ctx)
        p = mgit(ctx, ["ls-remote", "--heads", g.clone_url(ctx.repo["slug"])], secret_env=g.git_env(), timeout=120, check=False)
        why = ""
        if p.returncode:
            try:
                who = g.account()
            except Exception:
                who = ""
            why = "acces:" + who
        elif f"refs/heads/{ctx.repo['default_branch']}" not in p.stdout:
            why = "vide" if not p.stdout.strip() else "branche"
        if why:
            raise Waiting(why)

    def agent(self, ctx, prompt, resume=None, system=None):
        """Un passage de l'agent. False s'il a posé une question (le job attend). Relance une fois s'il reste muet."""
        if ctx.cancelled:
            raise Cancelled
        self.settle(ctx)   # dépense faite hors passage (checks, processus laissé par un check) : comptée, jamais effacée
        ctx.in_agent = True
        try:
            run = runner_for(ctx)(ctx, self.panel, prompt, resume, system=system or ctx.system).run()
        finally:
            ctx.in_agent = False
        self.ex.kill(ctx)
        # Coût : le plus grand de ce que l'agent déclare et de ce que la passerelle de clés a mesuré pendant le passage (les
        # appels faits directement à la passerelle par le code du job comptent aussi).
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
            # Budget du jour : rien n'est perdu (commits et fichiers restent dans l'espace de travail, la session est notée) ;
            # le dashboard remet une reprise en file, servie quand le budget le permet.
            if ctx.onboarding:
                raise Failed("budget du jour atteint pendant l'onboarding : relance-le depuis le repo")
            self.report_usage(ctx)
            self.panel.event(ctx.ref, type="paused", session_id=ctx.job.get("session_id") or "")
            raise Paused
        if ctx.gave_up:
            raise Failed("l'agent abandonne : " + ctx.gave_up)
        if ctx.question:
            self.panel.event(ctx.ref, type="question", question=ctx.question, session_id=run.session_id or "")
            self.panel.event(ctx.ref, type="intent_done", note="question posée")
            return False
        if run.stalled:
            if ctx.nudged:
                raise Failed(f"aucune progression après une relance du watchdog ({ctx.watchdog_min} min)")
            ctx.nudged = True
            self.panel.event(ctx.ref, type="log", kind="watchdog", message=f"agent muet depuis {ctx.watchdog_min} min : relance")
            return self.agent(ctx, messages.nudge(ctx.job, ctx.watchdog_min), resume=ctx.job.get("session_id"), system=system)
        if run.error:
            raise Failed(run.error)
        return True

    def close(self, ctx, kind):
        """Fin d'une commande, quelle qu'en soit l'issue : le conteneur du job est détruit (plus aucun appel possible avec
        son jeton), puis la dépense encore non comptée est remontée — AVANT l'événement qui clôt la commande, après quoi le
        dashboard n'accepte plus rien de ce runner sur ce job. Sans effet la seconde fois."""
        if getattr(ctx, "closed", False):
            return
        ctx.closed = True
        if kind != "merge":
            self.ex.stop(ctx)   # l'espace de travail reste, pour une reprise
        try:
            self.settle(ctx)
        except Exception:
            pass

    def settle(self, ctx):
        """Dépense mesurée par la passerelle de clés et pas encore comptée dans le coût du job : ajoutée et remontée au
        dashboard (budget du job et budget du jour). Appelée avant chaque passage d'agent, après les checks et en fin de
        commande : un appel payant fait avec le jeton du job compte toujours, quel que soit le moment."""
        with self.gateway.lock:
            extra = ctx.gw_spent - ctx.gw_mark
            ctx.gw_mark = ctx.gw_spent
            ctx.base_cost += max(0.0, extra)
        if extra > 0:
            try:
                self.report_usage(ctx)
            except Exception as e:   # la commande est peut-être déjà close côté dashboard : le coût reste dans le runner
                log.warning("%s : dépense hors passage non remontée (%s)", ctx.ref, type(e).__name__)

    def report_usage(self, ctx):
        self.panel.event(ctx.ref, type="usage", cost=round(ctx.base_cost, 4), tokens=ctx.base_tokens, session_id=ctx.job.get("session_id") or "")

    def open_prs(self, ctx):
        """PR déjà ouvertes (hors celle de ce job) : l'agent vérifie au tri qu'aucune ne couvre déjà la demande."""
        try:
            return [p for p in self.git(ctx).open_prs(ctx.repo["slug"]) if p["branch"] != ctx.branch]
        except Exception as e:
            log.warning("%s : liste des PR ouvertes indisponible (%s)", ctx.ref, type(e).__name__)
            return []

    def start(self, ctx):
        if ctx.study:
            return self.study(ctx)
        self.prepare(ctx, fresh=True)
        if self.agent(ctx, messages.launch(ctx.job, ctx.repo, self.open_prs(ctx))):
            self.finish(ctx)

    def study(self, ctx, resumed=None):
        """Étude : l'agent lit le repo et rend un rapport. Le runner ne capture aucun commit, ne pousse rien et n'ouvre pas
        de PR : ce que l'agent écrit dans l'espace de travail y reste, seul le rapport remonte au dashboard."""
        from .agent import STUDY
        ctx.system = STUDY
        self.prepare(ctx, fresh=resumed is None)
        sid = ctx.job.get("session_id") if resumed else None
        prompt = resumed if sid else messages.study(ctx.job, ctx.repo) + ("\n\n" + resumed if resumed else "")
        if not self.agent(ctx, prompt, resume=sid):
            return
        p = job(ctx, f"head -c {config.STUDY_MAX} RAPPORT_ETUDE.md", timeout=60, check=False)
        report = (p.stdout if p.returncode == 0 else "").strip() or (ctx.run.result or "").strip()
        if not report:
            raise Failed("l'agent n'a rendu aucun rapport d'étude")
        self.report_usage(ctx)
        self.panel.event(ctx.ref, type="study", report=report)
        self.panel.event(ctx.ref, type="intent_done", note="étude rendue")

    def onboard(self, ctx):
        """Onboarding : l'agent écrit la recipe ; le runner vérifie qu'il n'a touché qu'elle, la valide, la rejoue dans le
        conteneur du job, puis ouvre la PR. Un humain le merge (protected path .fabrique/**)."""
        from .agent import ONBOARDING
        ctx.system = ONBOARDING
        self.prepare(ctx, fresh=True, onboarding=True)
        if ctx.recipe.get("existing"):
            try:
                recipe = validate_recipe(json.loads(ctx.recipe["existing"]))
            except ValueError as e:
                raise Failed(f"la recipe déjà présente sur la branche principale est invalide : {e}")
            if recipe["checks"] or recipe["setup"]:
                self.panel.event(ctx.ref, type="onboarded", recipe=recipe)
                self.panel.event(ctx.ref, type="intent_done", note="recipe déjà présente")
                return
            # recette vide (dépôt initialisé par la Fabrique) : l'agent écrit la vraie recette du projet construit depuis
        elif bare(ctx):
            raise Waiting("vide")   # un README, une licence : pas de projet à analyser. Même suite que pour un repo vide.
        if not self.agent(ctx, messages.onboard(ctx.job, ctx.repo)):
            return
        base = ctx.base_sha
        # Seule la recette part en PR, quoi que l'agent ait commité ou laissé (environnement .venv, fichiers d'essai) :
        # on repart de la branche principale avec ce seul fichier ; le reste est écarté et signalé.
        touched = set(jgit(ctx, ["diff", "--name-only", base]).stdout.split())
        touched |= {l[3:] for l in jgit(ctx, ["status", "--porcelain", "--untracked-files=normal"]).stdout.splitlines() if len(l) > 3}
        if not appeared(os.path.join(ctx.repo_dir, ".fabrique", "setup.json")):
            raise Failed("l'agent n'a pas écrit .fabrique/setup.json")
        jgit(ctx, ["reset", "-q", "--soft", base])
        jgit(ctx, ["reset", "-q"])
        jgit(ctx, ["add", "--", ".fabrique/setup.json"])
        if not jgit(ctx, ["diff", "--cached", "--name-only"]).stdout.strip():
            raise Failed("la recipe proposée est identique à celle de la branche principale")
        jgit(ctx, ["commit", "-q", "-m", "Recipe de la Fabrique"])
        dropped = sorted(f for f in touched if f and f != ".fabrique/setup.json" and not f.startswith(".fabrique/setup.json"))
        if dropped:
            self.panel.event(ctx.ref, type="log", kind="onboarding",
                             message=f"écartés (seule la recipe part en PR) : {', '.join(dropped[:8])}{' …' if len(dropped) > 8 else ''}")
        # Le commit d'onboarding est importé dans le miroir et vérifié là : un seul parent (la base épinglée), un seul fichier
        # (la recette). La recette validée et rejouée est celle du commit (blob), pas le fichier de l'espace de travail.
        sanitized = self.capture_head(ctx)
        parents = mgit(ctx, ["rev-list", "--parents", "-n", "1", sanitized]).stdout.split()[1:]
        names = [f for f in mgit(ctx, ["diff", "--name-only", "-z", "--no-renames", base, sanitized]).stdout.split("\0") if f]
        if parents != [base] or names != [".fabrique/setup.json"]:
            raise Failed("le commit d'onboarding ne contient pas que la recipe")
        try:
            ctx.recipe = validate_recipe(json.loads(mgit(ctx, ["show", f"{sanitized}:.fabrique/setup.json"]).stdout))
        except ValueError as e:
            raise Failed(f"recipe invalide : {e}")
        # Preuve indépendante : installation et contrôles rejoués par le runner dans un conteneur neuf (image de la recette).
        ctx.image = ctx.recipe.get("image") or ctx.image
        self.ex.stop(ctx)
        self.ex.start(ctx)
        jgit(ctx, ["clean", "-qfdx"])
        self.panel.event(ctx.ref, type="stage", stage="verifying", summary="Le runner rejoue la recipe proposée dans un conteneur neuf.",
                         status="Recipe en cours de vérification")
        for cmd in ctx.recipe["setup"]:
            self.panel.event(ctx.ref, type="log", kind="installation", message=cmd[:200])
            job(ctx, cmd, timeout=900, check=False)
        sha, checks = self.checks(ctx)
        if sha != sanitized:
            raise Failed("la recipe rejouée a modifié le commit d'onboarding")
        # Toujours envoyée, même sans check (projet sans tests) : c'est elle qui fixe le sha vérifié, sans lequel la PR de
        # recipe ne pourrait jamais être mergée par la Fabrique après l'accord d'un humain.
        self.panel.event(ctx.ref, type="evidence", sha=sha, checks=checks)
        self.publish(ctx, sha, checks, base, ctx.attempt)

    def create_repo(self, ctx):
        """Création validée par un humain : repo privé et vide dans le groupe dédié, puis initialisation (repo vierge)."""
        ns, _, name = ctx.repo["slug"].rpartition("/")
        try:
            res = self.git(ctx).create_repo(ns, name, ctx.repo.get("description") or "")
        except Exception as e:
            raise Failed(f"création de {ctx.repo['slug']} refusée par le provider ({type(e).__name__}) : le token du runner "
                         f"doit pouvoir créer des repos dans « {ns} ».")
        self.panel.event(ctx.ref, type="log", kind="création", message=f"{ctx.repo['slug']} : {res}")
        self.init_repo(ctx)

    def init_repo(self, ctx):
        """Repo vide, initialisation validée par un humain : premier commit (README + recipe vide) poussé sur la branche
        principale. C'est la seule poussée de la Fabrique sur une branche principale."""
        g = self.git(ctx)
        os.makedirs(ctx.dir, exist_ok=True)
        self.ensure_mirror(ctx)
        try:
            self.reachable(ctx)
        except Waiting as w:
            if str(w) != "vide":
                raise
            existing = False
        else:
            self.fetch_mirror(ctx, branch=False)
            if not bare(ctx) or mgit(ctx, ["show", f"{ctx.base_sha}:.fabrique/setup.json"], check=False).returncode == 0:
                self.panel.event(ctx.ref, type="log", kind="onboarding", message="le repo n'est plus vide : onboarding normal")
                return self.onboard(ctx)
            existing = True     # un README ou une licence seulement : on les garde, et on ajoute la recipe vide par-dessus
        br = ctx.repo["default_branch"]
        if os.path.isdir(ctx.dir):
            self.wipe(ctx)
        os.makedirs(ctx.dir, exist_ok=True)
        os.chmod(ctx.dir, 0o777)
        # Tout est créé par l'utilisateur de l'agent (comme un clone) : le runner n'écrit rien lui-même dans l'espace de travail.
        sh(ctx, ["mkdir", "-p", ctx.home], cwd=ctx.dir)
        if existing:
            sh(ctx, ["git", "clone", "--quiet", "--no-hardlinks", "--branch", br, ctx.mirror, ctx.repo_dir], cwd=ctx.dir, timeout=300)
        else:
            sh(ctx, ["git", "init", "-q", "-b", br, ctx.repo_dir], cwd=ctx.dir)
        name = ctx.repo["slug"].split("/")[-1]
        readme = (f"# {name}\n\nDépôt initialisé par la Fabrique ({time.strftime('%d/%m/%Y')}). Le projet sera construit à partir des "
                  "demandes déposées dans la Fabrique ; sa recipe `.fabrique/setup.json` (installation et checks) sera écrite "
                  "par l'onboarding après la première merge.\n")
        recipe = json.dumps({"setup": [], "checks": {}}, indent=2) + "\n"
        # Un README déjà présent (repo créé avec son fichier de présentation) est gardé tel quel.
        sh(ctx, ["sh", "-c", 'mkdir -p .fabrique && { [ -n "$3" ] || printf "%s" "$1" > README.md; } && printf "%s" "$2" > .fabrique/setup.json',
                 "init", readme, recipe, "1" if existing else ""])
        sh(ctx, ["git", "add", "-A"])
        sh(ctx, ["git", "commit", "-q", "-m", "Initialisation par la Fabrique"])
        sha = self.capture_head(ctx, trusted=True)   # dépôt créé à l'instant par le runner : aucun agent n'y est passé
        mgit(ctx, ["push", "-q", "--no-follow-tags", g.clone_url(ctx.repo["slug"]), f"{sha}:refs/heads/{br}"], secret_env=g.git_env())
        self.panel.event(ctx.ref, type="initialized", sha=sha)
        self.panel.event(ctx.ref, type="intent_done", note=f"repo initialisé ({sha[:12]})")

    def resume(self, ctx):
        p = ctx.intent["payload"]
        if p.get("pause"):
            prompt = messages.resume_paused(ctx.job)
        else:
            prompt = messages.resume(ctx.job, p.get("question", "(question non transmise)"), p.get("answer", ""))
        if ctx.study:
            return self.study(ctx, resumed=prompt)
        self.prepare(ctx, fresh=False)
        sid = ctx.job.get("session_id")
        if not sid:
            prompt = messages.launch(ctx.job, ctx.repo) + "\n\n" + prompt
        if self.agent(ctx, prompt, resume=sid):
            self.finish(ctx)

    def checks(self, ctx):
        """Checks de la recipe sur le commit de tête ; le sha est celui importé dans le miroir, et il ne doit pas changer
        pendant les checks (sinon la preuve ne vaudrait pas pour le commit poussé)."""
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
            raise Failed("le commit de tête a changé pendant les checks")
        self.settle(ctx)   # un check peut appeler le modèle avec le jeton du job : cette dépense compte
        return sha, out

    def security(self, ctx):
        """Contrôle de sécurité de tout ce que le job a changé (secrets en dur, paquets introuvables ou tout juste publiés),
        dans le conteneur du job. À titre d'information : toujours « ok », avec warn quand il y a des alertes. Une image de
        job sans le script (ancienne version) : pas de ligne."""
        if not re.fullmatch(r"[0-9a-f]{7,40}", ctx.base_sha or ""):
            return None
        t = time.time()
        p = job(ctx, f"python3 /opt/fabrique/runner/controle.py diff {ctx.base_sha}", timeout=180, check=False)
        txt = (p.stdout or "").strip()
        if p.returncode or not txt.startswith("Contrôle de sécurité"):
            return None
        ctx.security_report = txt    # transmis au relecteur sécurité comme indices à vérifier (review.prompts)
        return {"name": SECURITY_CHECK, "command": "controle.py diff (information, ne bloque pas)", "ok": True,
                "warn": "alerte" in txt.splitlines()[0], "duration_ms": int((time.time() - t) * 1000), "output": txt[-6000:]}

    def network(self, ctx):
        """Sorties réseau du job depuis son démarrage (journal du proxy de sortie) : ce qui est passé, ce que la liste blanche
        a refusé. À titre d'information, comme le contrôle de sécurité ; warn s'il y a eu des refus."""
        r = self.ex.net(ctx) if hasattr(self.ex, "net") else None
        if not r:
            return None
        a, d = r.get("allowed") or [], r.get("denied") or []
        na, nd = sum(n for _, n in a), sum(n for _, n in d)
        head = (f"Sorties réseau : {nd} refusée(s), {na} autorisée(s)" if nd else
                f"Sorties réseau : {na} autorisée(s), aucun refus" if na else "Sorties réseau : aucune")
        lines = [head]
        if d:
            lines += ["Refusées (hors de la liste blanche du proxy) :"] + [f"  ⚠ {h} ({n} fois)" for h, n in d]
        if a:
            lines += ["Autorisées :"] + [f"  · {h} ({n} fois)" for h, n in a]
        lines.append("\nUn refus : une dépendance à ajouter à la liste blanche du proxy, ou un agent qui tente de joindre un "
                     "site qu'il ne devrait pas. À titre d'information : ce contrôle ne bloque pas le job.")
        return {"name": NETWORK_CHECK, "command": "journal du proxy de sortie (information, ne bloque pas)", "ok": True,
                "warn": nd > 0, "duration_ms": 0, "output": "\n".join(lines)[-6000:]}

    def reviews(self, ctx, sha, base):
        """Vérificateurs en lecture seule sur le diff vérifié ; renvoie les verdicts (et les remonte au dashboard)."""
        files = [f for f in mgit(ctx, diff_args(base) + ["--name-only", "-z", "--no-renames", f"{base}...{sha}"]).stdout.split("\0") if f]
        diff = mgit(ctx, diff_args(base) + [f"{base}...{sha}"]).stdout
        truncated = len(diff) > config.REVIEW_MAX_DIFF
        if truncated:
            diff = diff[:config.REVIEW_MAX_DIFF] + f"\n[… diff tronqué à {config.REVIEW_MAX_DIFF} caractères]"
        roles = review.roles_for(files)
        self.panel.event(ctx.ref, type="stage", stage="review", summary="Vérification en lecture seule : " + ", ".join(review.ROLES[r][0] for r in roles) + ".",
                         status="Vérification en cours")
        # Plus aucun processus du job (un check a pu en laisser un) pendant la vérification. En mode agent, le vérificateur
        # lit un export du commit vérifié, tiré du miroir du runner, dans un dossier neuf au nom imprévisible, avec un
        # dossier personnel neuf : ni l'arbre de travail, ni le HOME que l'agent et les checks ont pu modifier.
        self.ex.kill(ctx)
        ctx.review_dir = ctx.review_home = None
        try:
            if review.mode(ctx.review_model) == "agent":
                tag = os.urandom(8).hex()
                ctx.review_dir, ctx.review_home = os.path.join(ctx.dir, f".verif-{tag}"), os.path.join(ctx.dir, f".verif-home-{tag}")
                # Export par un index temporaire (pas « git archive », qui obéirait aux attributs export-ignore du commit
                # jugé). Les réglages de projet de l'agent de code (.claude, .mcp.json, CLAUDE.md) sont retirés de l'export :
                # le vérificateur les voit dans le diff, il ne les charge pas.
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
        """Une relecture ; son coût est le plus grand de ce qu'elle déclare et de ce que la passerelle a mesuré pendant."""
        self.settle(ctx)
        r = review.run(ctx, role, diff)
        with self.gateway.lock:
            gw = ctx.gw_spent - ctx.gw_mark
            ctx.gw_mark = ctx.gw_spent
            r["cost"] = max(float(r.get("cost") or 0), gw)
            ctx.base_cost += r["cost"]   # compté ici, sous le verrou : la passerelle voit ce coût dès la relecture finie
        return r

    def _reviews(self, ctx, sha, roles, diff, truncated):
        out = []
        for role in roles:
            if ctx.cancelled:
                raise Cancelled
            r = self._review_once(ctx, role, diff)
            if r["verdict"] == "indetermine":   # une seconde chance avant de remonter à un humain
                self.panel.event(ctx.ref, type="log", kind="vérification", message=f"{r['label']} : {r['cause']} ; nouvelle tentative")
                r = self._review_once(ctx, role, diff)
            r = review.confirm(r, lambda cmd: self.replay(ctx, cmd))
            if truncated and r["verdict"] == "ok":
                # Le vérificateur n'a vu qu'une partie du changement : son « ok » ne peut pas valoir pour le tout. Un
                # humain relit (et aucune fusion automatique ne part).
                r["verdict"] = "indetermine"
                r["summary"] = (f"Diff trop long ({config.REVIEW_MAX_DIFF} caractères lus) : vérification incomplète, à relire par un "
                                f"humain. Avis sur la partie lue : {r['summary']}")[:2000]
            self.panel.event(ctx.ref, type="review", sha=sha, role=role, verdict=r["verdict"], summary=r["summary"], points=r["points"],
                             model=ctx.review_model["id"])
            self.report_usage(ctx)   # le coût des relecteurs compte dans le coût du job et le budget du jour
            out.append(r)
            if ctx.budget > 0 and ctx.base_cost > ctx.budget:
                self.panel.event(ctx.ref, type="log", kind="vérification", message="budget du job atteint : vérifications suivantes arrêtées")
                break
        return out

    def git(self, ctx):
        """Client du provider Git du repo (Forgejo, GitLab…), créé une fois par provider."""
        if self._forge is not None:
            return self._forge
        p = ctx.repo.get("provider") or "forgejo"
        f = getattr(ctx, "forge", None) or {}
        key = (p, f.get("base_url") or "", hash(f.get("token") or ""))   # réglé dans le panneau, sinon variables du runner
        if key not in self._forges:
            g = forges.make(p, f.get("base_url"), f.get("token"))
            if not g.base or not g.token:
                raise Failed(f"provider {p} : adresse ou token manquant. Renseigne-le dans Settings → Providers Git "
                             "(ou dans les variables du runner).")
            self._forges[key] = g
        return self._forges[key]

    def replay(self, ctx, cmd):
        """Rejoue la preuve d'un vérificateur dans l'espace du job (même isolement que l'agent)."""
        p = job(ctx, cmd, timeout=120, check=False)
        self.ex.kill(ctx)   # la preuve tourne sur le code jugé : aucun de ses processus ne doit voir le vérificateur suivant
        return p.returncode, (p.stdout + p.stderr)[-1000:]

    def finish(self, ctx):
        base = ctx.base_sha
        fixes = 0
        while True:
            if ctx.cancelled:
                raise Cancelled
            if jgit(ctx, ["status", "--porcelain"]).stdout.strip():
                jgit(ctx, ["add", "-A", "--", ".", *ENV_EXCLUDES])
                if jgit(ctx, ["diff", "--cached", "--name-only"]).stdout.strip():
                    jgit(ctx, ["commit", "-q", "-m", f"{ctx.ref} : modifications restantes de l'agent\n\nFabrique-Job: {ctx.ref}"])
            if not mgit(ctx, ["rev-list", f"{base}..{self.capture_head(ctx)}"]).stdout.strip():
                raise Failed("l'agent n'a produit aucun changement")
            self.panel.event(ctx.ref, type="stage", stage="verifying", summary="Le runner relance la recipe du repo sur le commit de tête.",
                             status="Checks en cours")
            sha, checks = self.checks(ctx)
            self.panel.event(ctx.ref, type="evidence", sha=sha, checks=checks)
            if not all(c["ok"] for c in checks):
                if fixes >= config.FIX_ATTEMPTS:
                    raise Failed(f"checks en échec après {ctx.attempt} essais")
                fixes += 1
                ctx.attempt += 1
                self.panel.event(ctx.ref, type="stage", stage="implementing", summary=f"Checks en échec : nouvel essai {ctx.attempt}.",
                                 status="Corrige les checks")
                if not self.agent(ctx, messages.fix_checks([c for c in checks if not c["ok"]]), resume=ctx.job.get("session_id")):
                    return
                continue
            verdicts = self.reviews(ctx, sha, base)
            blocking = [r for r in verdicts if r["verdict"] == "bloquant"]
            if blocking and not ctx.review_fixed:
                ctx.review_fixed = True
                ctx.attempt += 1
                self.panel.event(ctx.ref, type="stage", stage="implementing", summary="Vérification bloquante : une ronde de correction.",
                                 status="Corrige les points de vérification")
                if not self.agent(ctx, messages.fix_review(blocking), resume=ctx.job.get("session_id")):
                    return
                continue
            break
        self.publish(ctx, sha, checks, base, ctx.attempt, verdicts)

    def publish(self, ctx, sha, checks, base, attempt, verdicts=None):
        # Pousser et ouvrir la PR : conteneur du job détruit (ou processus de l'agent tués) ; hooks Git désactivés ; le jeton
        # n'existe que le temps de ces commandes.
        self.ex.stop(ctx)
        self.settle(ctx)   # plus de conteneur : la dépense du job est définitive, remontée avant l'événement de PR
        if ctx.cancelled:
            raise Cancelled
        g = self.git(ctx)
        mgit(ctx, ["push", "-q", "--force", "--no-follow-tags", g.clone_url(ctx.repo["slug"]), f"{sha}:refs/heads/{ctx.branch}"], secret_env=g.git_env())
        files, add, rem = numstat(ctx, base, sha)
        self.send_diff(ctx, base, sha)
        d = ctx.decision or {}
        # Tout texte venu de l'agent ou du demandeur est rendu inerte (pas de mention, de fermeture de ticket, d'image ni de
        # lien) : la PR est écrite et fusionnée avec le compte du runner.
        q, title = messages.quote, messages.one_line(ctx.title)
        body = (f"Demande **{ctx.ref}** : {title}\n\n{q(ctx.job['request'], 8000)}\n\n**Terminé quand** :\n{q(ctx.job.get('done_when'), 2000)}\n\n"
                f"### Avis de l'agent\n{q(d.get('verdict'), 1000)}\n\n**Interprétation** :\n{q(d.get('interpretation'), 4000)}\n\n"
                + review_guide(ctx, base, sha, files, add, rem, verdicts)
                + 
                f"### Preuves (relancées par le runner au sha {sha[:12]})\n"
                + "\n".join(f"- {'⚠️' if c.get('warn') else '✅' if c['ok'] else '❌'} {messages.inert(c['name'], 40)} `{messages.inert(c['command'], 500)}`" for c in checks)
                + "\n\nOuverte par la Fabrique. La décision de merge appartient au dashboard.")
        if ctx.cancelled:
            raise Cancelled
        n = self.git(ctx).open_pr(ctx.repo["slug"], ctx.branch, ctx.repo["default_branch"], f"[{ctx.ref}] {title}", body)
        res = self.panel.event(ctx.ref, type="pr", number=n, url=self.git(ctx).pr_url(ctx.repo["slug"], n), files=files, additions=add,
                               deletions=rem, attempts=attempt, **({"recipe": ctx.recipe} if ctx.onboarding else {})) or {}
        self.comment(ctx, n, res)
        self.panel.event(ctx.ref, type="intent_done", note=f"PR #{n}", attempts=attempt)

    def send_diff(self, ctx, base, sha):
        """Envoie au dashboard le diff du commit vérifié, pour qu'un humain le lise sans quitter la Fabrique. Lu dans le
        miroir du runner (attributs de la base, aucun programme externe), plafonné, et seulement informatif : un échec
        d'envoi n'arrête pas le job, la PR sur la forge fait foi."""
        # Aucun diff ne quitte le runner si son réglage le coupe (FABRIQUE_DIFF_MAX=0) ou si le repo est déclaré confidentiel
        # dans le dashboard (« Afficher le diff dans la Fabrique » décoché) : le code n'est alors copié nulle part.
        if config.DIFF_MAX <= 0 or ctx.repo.get("diff") is False:
            return
        try:
            text = mgit(ctx, ["-c", "core.quotepath=off"] + diff_args(base) + ["--no-renames", "--no-color", f"{base}...{sha}"],
                        errors="replace").stdout
            cut = len(text) > config.DIFF_MAX
            if cut:
                text = text[:text.rfind("\n", 0, config.DIFF_MAX) + 1 or config.DIFF_MAX]
            self.panel.event(ctx.ref, type="diff", sha=sha, text=text, truncated=cut)
        except Exception as e:   # jamais bloquant
            log.warning("%s : diff non envoyé au dashboard (%s)", ctx.ref, e)

    def comment(self, ctx, n, res):
        dec = (res or {}).get("decision")
        if dec:
            try:
                self.git(ctx).upsert_comment(ctx.repo["slug"], n, decision_comment(ctx, dec))
            except Exception as e:
                log.warning("%s : commentaire de décision impossible (%s)", ctx.ref, e)

    def rereview(self, ctx):
        """Vérification relancée par un reviewer, au sha vérifié, sans relancer l'agent ; puis barrières réévaluées."""
        want, n = ctx.intent["payload"].get("sha") or ctx.job.get("verified_sha") or "", ctx.job.get("pr_number")
        if not want or not n:
            raise Failed("rien à relire (pas de PR ou pas de sha vérifié)")
        self.prepare(ctx, fresh=True, branch=True)   # espace neuf, depuis le miroir : rien de l'ancien espace n'est cru
        head = mgit(ctx, ["rev-parse", "--verify", f"refs/heads/{ctx.branch}^{{commit}}"], check=False).stdout.strip()
        if not head.startswith(want):
            raise Failed(f"la branche a changé depuis la vérification ({head[:12]} au lieu de {want}) : nouvella vérification nécessaire")
        base = ctx.base_sha
        self.reviews(ctx, head, base)
        files, add, rem = numstat(ctx, base, head)
        self.send_diff(ctx, base, head)
        res = self.panel.event(ctx.ref, type="pr", number=n, url=ctx.job.get("pr_url") or self.git(ctx).pr_url(ctx.repo["slug"], n),
                               files=files, additions=add, deletions=rem, attempts=ctx.job.get("attempts") or 1,
                               summary=f"Vérification relancée au sha {want} ; barrières réévaluées.") or {}
        self.comment(ctx, n, res)
        self.panel.event(ctx.ref, type="intent_done", note="vérification relancée")

    def merge(self, ctx):
        n, want = ctx.job.get("pr_number"), (ctx.intent["payload"].get("sha") or ctx.job.get("verified_sha") or "")
        if not n or not want:
            raise Failed("rien à merger (pas de PR ou pas de sha vérifié)")
        if ctx.cancelled:
            raise Cancelled
        pr = self.git(ctx).pr(ctx.repo["slug"], n)
        head = pr["head"]["sha"]
        if not head.startswith(want):
            raise Failed(f"la PR a changé depuis la vérification ({head[:12]} au lieu de {want}) : nouvella vérification nécessaire")
        if ctx.cancelled:
            raise Cancelled
        merged = self.git(ctx).merge(ctx.repo["slug"], n, head, f"[{ctx.ref}] {messages.one_line(ctx.title)} (#{n})",
                                     message=f"Fabrique-Job: {ctx.ref}")   # le commit de fusion renvoie à son job
        self.panel.event(ctx.ref, type="merged", sha=merged, branch=ctx.repo["default_branch"])
        self.panel.event(ctx.ref, type="intent_done", note=f"mergé {merged[:12]}")
        sh(ctx, ["rm", "-rf", ctx.repo_dir, ctx.home], cwd=config.WORK_DIR, check=False)
        shutil.rmtree(ctx.mirror, ignore_errors=True)


JOB_IMAGES = ("python", "node", "go", "java")


def detect_image(repo_dir):
    """Langage principal d'un repo, pour choisir l'image du conteneur d'onboarding (l'agent peut corriger dans la recipe)."""
    has = lambda *names: any(os.path.exists(os.path.join(repo_dir, n)) for n in names)
    if has("go.mod"):
        return "go"
    if has("pom.xml", "build.gradle", "build.gradle.kts", "gradlew"):
        return "java"
    if has("package.json") and not has("pyproject.toml", "requirements.txt", "setup.py"):
        return "node"
    return "python"


def validate_recipe(r):
    """Recipe de la Fabrique : image (langage), setup (commandes), checks (nom -> commande), env (noms et usages, sans valeur)."""
    if not isinstance(r, dict) or set(r) - {"image", "setup", "checks", "env"}:
        raise ValueError("rubriques autorisées : image, setup, checks, env")
    if "image" in r and r["image"] not in JOB_IMAGES:
        raise ValueError("image : une de " + ", ".join(JOB_IMAGES))
    setup, checks, env = r.get("setup") or [], r.get("checks") or {}, r.get("env") or []
    if not isinstance(setup, list) or len(setup) > 20 or not all(isinstance(c, str) and 0 < len(c) <= 500 for c in setup):
        raise ValueError("setup : une liste d'au plus 20 commandes")
    if not isinstance(checks, dict) or len(checks) > 8 or not all(
            isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", k) and isinstance(v, str) and 0 < len(v) <= 500 for k, v in checks.items()):
        raise ValueError("checks : au plus 8 checks nom -> commande")
    if not isinstance(env, list) or len(env) > 30 or not all(
            isinstance(e, dict) and set(e) <= {"name", "purpose", "required"} and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(e.get("name", "")))
            for e in env):
        raise ValueError("env : des noms et des usages, sans valeur")
    out = {"setup": setup, "checks": checks, "env": env}
    if r.get("image"):
        out["image"] = r["image"]
    return out


def diff_args(base):
    """Diff dans le miroir : attributs lus sur la base épinglée (jamais ceux de la branche de l'agent), sans programme externe."""
    return ["-c", f"attr.tree={base}", "diff", "--no-ext-diff", "--no-textconv"]


def review_guide(ctx, base, sha, files, add, rem, verdicts):
    """Section « Pour relire » de la PR : effort estimé, par où commencer, critères de la demande, verdicts des relecteurs.
    Tout texte venu d'un modèle est rendu inerte."""
    per = []
    for rec in mgit(ctx, diff_args(base) + ["--numstat", "-z", "--no-renames", f"{base}...{sha}"]).stdout.split("\0"):
        a, d, f = (rec.split("\t", 2) + ["", "", ""])[:3]
        if f:
            per.append((int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0, f))
    from fnmatch import fnmatch
    prot = any(fnmatch(f, pat) for f in files for pat in (ctx.repo.get("protected_paths") or []))
    n = review.effort(add + rem, len(files), prot)
    out = [f"### Pour relire\n**Effort de relecture : {n}/5 ({review.EFFORT[n]})** · {len(files)} fichier(s), +{add} −{rem}"
           + (" · touche un chemin protégé" if prot else "")]
    top = sorted(per, key=lambda x: -(x[0] + x[1]))[:3]
    if top:
        out.append("**Par où commencer** : " + ", ".join(f"`{messages.inert(f, 200)}` (+{a} −{d})" for a, d, f in top))
    code = next((v for v in verdicts or [] if v.get("role") == "code"), None)
    crit = review.criteria(code.get("points")) if code else []
    if crit:
        icon = {"ok": "✅ traité", "non": "❌ non traité", "flou": "❓ pas clair"}
        out.append("**Critères de la demande** (selon le relecteur code) :\n" + "\n".join(f"- {icon[k]} : {messages.inert(t, 400)}" for k, t in crit[:10]))
    if verdicts:
        mark = {"ok": "✅", "a_revoir": "⚠️ à revoir", "bloquant": "❌ bloquant", "indetermine": "❓ sans verdict"}
        out.append("**Vérifications** : " + " · ".join(f"{messages.inert(v.get('label'), 40)} {mark.get(v.get('verdict'), '')}" for v in verdicts))
    return "\n\n".join(out) + "\n\n"


def numstat(ctx, base, sha):
    """Fichiers et lignes du diff, chemins bruts (-z, sans détection de renommage) : le dashboard les compare aux protected paths."""
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
    i = messages.inert   # les détails citent des chemins de fichiers choisis par l'agent
    lim = "\n".join(f"- {'✅' if l['ok'] else '❌'} **{i(l['name'], 60)}** : {i(l['detail'], 600)}" for l in dec.get("limits") or [])
    return (f"### Décision de la Fabrique · {ctx.ref}\n\n**Politique du repo** : {i(dec.get('policy'), 60)}  \n**Statut** : {i(dec.get('status'), 200)}\n\n"
            f"{lim}\n\n_Barrières évaluées par le dashboard, pas par l'agent. Ce commentaire est mis à jour à chaque nouvella vérification._")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # pas une ligne par sondage de la file
    # Compose tout-en-un : le jeton du runner et celui du lanceur arrivent par fichier, quelques secondes après le démarrage.
    for _ in range(90):
        if config.runner_token() and (config.EXECUTOR != "docker" or config._env("LAUNCHER_TOKEN") or config._file("LAUNCHER_TOKEN_FILE")):
            break
        time.sleep(2)
    config.RUNNER_TOKEN = config.runner_token()
    config.LAUNCHER_TOKEN = config._env("LAUNCHER_TOKEN") or config._file("LAUNCHER_TOKEN_FILE")
    for var in ("FABRIQUE_RUNNER_TOKEN_FILE", "LAUNCHER_TOKEN_FILE"):   # un token lisible par tous l'est aussi par l'agent
        path = config._env(var)
        try:
            if path and os.stat(path).st_mode & 0o004:
                log.warning("%s (%s) est lisible par tous les utilisateurs du conteneur : resserre ses droits (0640 ou 0600) "
                            "et renouvelle ce token", var, path)
        except OSError:
            pass
    if not config.RUNNER_TOKEN:
        path = config._env("FABRIQUE_RUNNER_TOKEN_FILE")
        if path and os.path.exists(path) and not os.access(path, os.R_OK):
            raise SystemExit(f"token du runner illisible ({path}) : le fichier n'est plus lisible par tous. Donne au runner le "
                             "groupe du dashboard (compose : group_add: [\"10001\"] sur le service runner), puis relance.")
        raise SystemExit("token du runner manquant (FABRIQUE_RUNNER_TOKEN ou FABRIQUE_RUNNER_TOKEN_FILE)")
    if config.EXECUTOR != "docker":
        log.warning("EXECUTOR=%s : les jobs tournent DANS le conteneur du runner, séparés par un simple utilisateur Unix. "
                    "Mode de développement : ce n'est pas une frontière de sécurité (utiliser EXECUTOR=docker).", config.EXECUTOR)
    r = Runner()
    signal.signal(signal.SIGTERM, lambda *a: r.stop.set())
    r.loop()


if __name__ == "__main__":
    main()
