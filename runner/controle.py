#!/usr/bin/env python3
"""Contrôle de sécurité léger de la Fabrique : secrets écrits en dur (Betterleaks) et paquets introuvables ou tout juste
publiés (nom inventé par le modèle, ou paquet piège qui exploite ces noms inventés). Deux usages :
  controle.py hook        hook PostToolUse de Claude Code (Write, Edit, MultiEdit), installé en réglages gérés. Contrôle le
                          fichier que l'agent vient d'écrire. S'il trouve un problème : code 2 et message à l'agent, qui
                          corrige aussitôt. Le fichier est déjà écrit : c'est un signalement, pas un refus.
  controle.py diff BASE   en fin de job, lancé par le runner dans le conteneur du job : contrôle tout ce que le job a changé
                          (commits BASE..HEAD) et écrit un rapport. Toujours code 0 : à titre d'information, ne bloque pas.
La valeur d'un secret n'est jamais affichée. Réseau : seulement les registres de paquets, par le proxy de sortie du job.
Un registre injoignable ne bloque rien : le paquet est signalé « non vérifié »."""
import calendar, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

YOUNG_DAYS = 30            # un paquet publié depuis moins longtemps est signalé
MAX_PACKAGES = 15          # au-delà, les paquets suivants ne sont pas vérifiés (et c'est dit)
TIMEOUT = 6                # secondes par requête de registre
# Dans les tests, les mots de passe factices sont la norme : seules les règles génériques y sont ignorées. Un vrai jeton
# reconnu (GitHub, Stripe, AWS…) reste signalé partout.
TEST_PATH = re.compile(r"(^|/)(tests?|spec|specs|__tests__|testdata|fixtures?)(/|$)|(^|/)(test_[^/]*|[^/]*_test\.[a-z]+|[^/]*\.(test|spec)\.[a-z]+)$")
GENERIC = re.compile(r"^generic-")

# Fichiers de dépendances reconnus -> écosystème
MANIFESTS = [(re.compile(r"(^|/)requirements[^/]*\.(txt|in)$"), "pypi"), (re.compile(r"(^|/)pyproject\.toml$"), "pypi"),
             (re.compile(r"(^|/)package\.json$"), "npm"), (re.compile(r"(^|/)go\.mod$"), "go")]
REGISTRY = {"pypi": "PyPI", "npm": "npm", "go": "le proxy Go"}


# --- Secrets -------------------------------------------------------------------------------------------------------------

def secrets(args, cwd):
    """Lance Betterleaks (validation des secrets désactivée : rien ne part vers les services concernés) ; renvoie
    [(fichier, ligne, règle)], ou None si l'outil est absent."""
    exe = shutil.which("betterleaks")
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args, "--no-banner", "--no-color", "--redact=100", "--log-level", "error", "-f", "json", "-r", "-"],
                           cwd=cwd, capture_output=True, text=True, timeout=60)
        found = json.loads(p.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    out = []
    for f in found if isinstance(found, list) else []:
        path, rule = str(f.get("File") or ""), str(f.get("RuleID") or "secret")
        path = os.path.relpath(path, cwd) if os.path.isabs(path) else path
        if TEST_PATH.search(path) and GENERIC.match(rule):
            continue
        out.append((path, int(f.get("StartLine") or 0), rule))
    return sorted(set(out))


# --- Paquets -------------------------------------------------------------------------------------------------------------

def ecosystem(path):
    return next((e for pat, e in MANIFESTS if pat.search(path)), None)


def _pyname(spec):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec or "")
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


def packages(path, text):
    """Noms des paquets déclarés dans un fichier de dépendances (registre public seulement : chemins, URL et dépôts Git
    sont ignorés)."""
    eco, names = ecosystem(path), set()
    if not text:
        return names
    if eco == "pypi" and path.endswith(".toml"):
        try:
            import tomllib
            d = tomllib.loads(text)
        except Exception:
            return names
        proj = d.get("project") or {}
        specs = list(proj.get("dependencies") or [])
        for v in (proj.get("optional-dependencies") or {}).values():
            specs += list(v or [])
        for v in (d.get("dependency-groups") or {}).values():
            specs += [x for x in v or [] if isinstance(x, str)]
        poetry = (d.get("tool") or {}).get("poetry") or {}
        specs += [k for k, v in (poetry.get("dependencies") or {}).items() if k.lower() != "python" and not (isinstance(v, dict) and ({"path", "git", "url"} & set(v)))]
        for g in (poetry.get("group") or {}).values():
            specs += [k for k, v in (g.get("dependencies") or {}).items() if not (isinstance(v, dict) and ({"path", "git", "url"} & set(v)))]
        names = {_pyname(s) for s in specs if isinstance(s, str) and "@" not in s.split(";")[0]}
    elif eco == "pypi":
        for line in text.splitlines():
            line = line.split(" #")[0].strip()
            if not line or line.startswith(("#", "-", ".", "/")) or "://" in line or " @ " in line:
                continue
            names.add(_pyname(line))
    elif eco == "npm":
        try:
            d = json.loads(text)
        except ValueError:
            return names
        for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for n, v in (d.get(k) or {}).items():
                if isinstance(v, str) and not re.match(r"^(file:|link:|workspace:|git|https?:|npm:)|/", v):
                    names.add(n.lower())
    elif eco == "go":
        block = False
        for line in text.splitlines():
            s = line.split("//")[0].strip()
            if s.startswith("require ("):
                block = True
                continue
            if block and s == ")":
                block = False
                continue
            m = re.match(r"^(?:require\s+)?([A-Za-z0-9.\-_~/]+\.[A-Za-z0-9.\-_~/]+)\s+v\S+$", s) if (block or s.startswith("require ")) else None
            if m:
                names.add(m.group(1))
    return {n for n in names if n}


def _get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fabrique-controle"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def lookup(eco, name, now=None, get=_get):
    """Ce que dit le registre public : ("absent", None), ("ok", âge en jours ou None) ou ("inconnu", raison)."""
    now = now or time.time()
    try:
        if eco == "pypi":
            d = get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
            ups = [f.get("upload_time_iso_8601") or f.get("upload_time") for files in (d.get("releases") or {}).values() for f in files or []]
            first = min((u for u in ups if u), default=None)
        elif eco == "npm":
            d = get("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@"))
            first = (d.get("time") or {}).get("created")
        else:
            esc = re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), name)
            get(f"https://proxy.golang.org/{esc}/@latest")
            first = None
    except urllib.error.HTTPError as e:
        return ("absent", None) if e.code in (404, 410) else ("inconnu", f"réponse {e.code} du registre")
    except (urllib.error.URLError, OSError, ValueError):
        return "inconnu", "registre injoignable"
    if not first:
        return "ok", None
    try:
        t = calendar.timegm(time.strptime(first[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return "ok", None
    return "ok", max(0, int((now - t) // 86400))


def check_packages(changes, get=_get):
    """changes : [(fichier, ancien texte, nouveau texte)]. Renvoie (alertes, notes, nombre de paquets vérifiés)."""
    alerts, notes, n = [], [], 0
    for path, old, new in changes:
        eco = ecosystem(path)
        added = sorted(packages(path, new) - packages(path, old))
        for name in added:
            if n >= MAX_PACKAGES:
                notes.append(f"{path} : d'autres paquets ajoutés ne sont pas vérifiés (limite de {MAX_PACKAGES}).")
                return alerts, notes, n
            n += 1
            state, info = lookup(eco, name, get=get)
            if state == "absent":
                alerts.append(f"{path} : le paquet « {name} » est introuvable sur {REGISTRY[eco]}. Le nom est probablement mal "
                              "orthographié ou inventé : corrige-le avec le nom exact du paquet voulu, ou retire-le.")
            elif state == "ok" and info is not None and info < YOUNG_DAYS:
                alerts.append(f"{path} : le paquet « {name} » n'existe sur {REGISTRY[eco]} que depuis {info} jour(s). Un paquet aussi "
                              "récent peut être un piège qui imite un nom connu : vérifie que c'est bien celui voulu, sinon remplace-le.")
            elif state == "inconnu":
                notes.append(f"{path} : « {name} » non vérifié ({info}).")
    return alerts, notes, n


# --- Les deux usages -----------------------------------------------------------------------------------------------------

def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout if p.returncode == 0 else ""


def _say(message):
    """Ligne dans l'activité du job (relais du runner, jeton du job). Sans relais : rien."""
    if not os.environ.get("FAB_TOKEN"):
        return
    req = urllib.request.Request(os.environ.get("FAB_RELAY", "http://127.0.0.1:7071") + "/fab",
                                 data=json.dumps({"cmd": "controle", "message": message[:900]}).encode(),
                                 headers={"Content-Type": "application/json", "X-Fab-Token": os.environ["FAB_TOKEN"]})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except (urllib.error.URLError, OSError):
        pass


def hook(stdin=sys.stdin, get=_get):
    try:
        ev = json.load(stdin)
    except ValueError:
        return 0
    if ev.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        return 0
    root = os.path.realpath(os.environ.get("FAB_WORKSPACE") or ev.get("cwd") or ".")
    full = os.path.realpath(os.path.join(root, str((ev.get("tool_input") or {}).get("file_path") or "")))
    if not full.startswith(root + os.sep) or not os.path.isfile(full) or os.sep + ".git" + os.sep in full:
        return 0
    rel = os.path.relpath(full, root)
    problems = []
    for path, line, rule in secrets(["dir", rel], root) or []:
        problems.append(f"{path} ligne {line} : secret écrit en dur (règle {rule}). Retire-le et lis-le depuis une variable "
                        "d'environnement ou la configuration ; ne recopie jamais sa valeur, ni ici ni dans un message.")
    if ecosystem(rel):
        try:
            new = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            new = ""
        alerts, _, _ = check_packages([(rel, _git(["show", f"HEAD:{rel}"], root), new)], get=get)
        problems += alerts
    if not problems:
        return 0
    _say(f"Contrôle sur {rel} : " + " ".join(p.split(" : ", 1)[1].split(".")[0] for p in problems)[:600] + ". L'agent est invité à corriger.")
    print("Contrôle de la Fabrique : à corriger avant de continuer.\n- " + "\n- ".join(problems), file=sys.stderr)
    return 2


def diff(base, cwd=".", get=_get):
    """Rapport sur tout ce que le job a changé. Première ligne : le bilan (lu par le runner et le dashboard)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", base or ""):
        print("Contrôle de sécurité : base invalide")
        return 0
    lines, alerts = [], 0
    found = secrets(["git", f"--log-opts={base}..HEAD", "."], cwd)
    if found is None:
        lines.append("Secrets : non contrôlés (Betterleaks absent de l'image du job).")
    elif found:
        alerts += len(found)
        lines.append("Secrets écrits en dur :")
        lines += [f"  ⚠ {p} ligne {l} (règle {r})" for p, l, r in found]
    else:
        lines.append("Secrets : aucun dans les commits du job.")
    changed = [f for f in _git(["diff", "--name-only", "--no-renames", f"{base}..HEAD"], cwd).splitlines() if ecosystem(f)]
    if changed:
        pa, notes, n = check_packages([(f, _git(["show", f"{base}:{f}"], cwd), _git(["show", f"HEAD:{f}"], cwd)) for f in changed], get=get)
        alerts += len(pa)
        lines.append(f"Paquets ajoutés : {n} vérifié(s) sur leur registre." if n else "Paquets ajoutés : aucun.")
        lines += [f"  ⚠ {a}" for a in pa] + [f"  · {x}" for x in notes]
    else:
        lines.append("Paquets : aucun fichier de dépendances modifié.")
    head = f"Contrôle de sécurité : {alerts} alerte(s)" if alerts else "Contrôle de sécurité : rien à signaler"
    print(head + "\n" + "\n".join(lines) + "\n\nÀ titre d'information : ce contrôle ne bloque pas le job.")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Dépôt du job créé par un autre utilisateur (volume partagé, NFS) : Git ne doit pas le refuser comme « douteux ».
    os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"})
    if argv[:1] == ["hook"]:
        return hook()
    if argv[:1] == ["diff"] and len(argv) == 2:
        return diff(argv[1])
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
