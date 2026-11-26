#!/usr/bin/env python3
"""Lightweight security check for Fabrique: hardcoded secrets (Betterleaks) and missing or just
published packages (model-invented name, or typo-squatting package exploiting these invented names). Two uses:
  security_check.py hook        Claude Code PostToolUse hook (Write, Edit, MultiEdit), installed via managed settings.
                          Checks the file the agent just wrote. If a problem is found: exit code 2 and a message to the
                          agent, which corrects it immediately. The file is already written: it is a report, not a refusal.
  security_check.py diff BASE   at the end of a job, launched by the runner in the job container: checks everything the
                          job changed (commits BASE..HEAD) and writes a report. Always exit code 0: for information,
                          does not block.
The value of a secret is never displayed. Network: only package registries, via the job's egress proxy.
An unreachable registry does not block anything: the package is flagged as "unverified"."""
try:
    from .texts import M
except ImportError:
    try:
        from texts import M
    except ImportError:
        def M(t, **p):
            return t.format(**p) if p else t
import calendar, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

ALERTS_LINE = "fabrique-alerts: "     # last line of the diff report, for the runner
YOUNG_DAYS = 30            # a package published more recently than this (in days) is flagged
MAX_PACKAGES = 15          # beyond that, the following packages are not checked (and this is stated)
TIMEOUT = 6                # seconds per registry request
# In tests, dummy passwords are the norm: only generic rules are ignored there. A real token
# recognized (GitHub, Stripe, AWS...) remains flagged everywhere.
TEST_PATH = re.compile(r"(^|/)(tests?|spec|specs|__tests__|testdata|fixtures?)(/|$)|(^|/)(test_[^/]*|[^/]*_test\.[a-z]+|[^/]*\.(test|spec)\.[a-z]+)$")
GENERIC = re.compile(r"^generic-")
# A generic rule fires on `password: "smtp.password"`: the value is the NAME of a setting, not a secret. Words joined by
# dots or underscores, no digit, nothing random: a line whose quoted values all look like that is not reported (seen:
# eight alerts on one settings module, each asking the agent to "fix" it).
KEY_NAME = re.compile(r"[A-Za-z]+(?:[._][A-Za-z]+)+")
QUOTED = re.compile(r"""(["'`])((?:(?!\1).){1,200})\1""")


def key_name_only(cwd, path, line):
    """True if the line's quoted values are all setting names (smtp.password, admin_password)."""
    try:
        with open(os.path.join(cwd, path), encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f, 1):
                if i == line:
                    values = [m.group(2) for m in QUOTED.finditer(text)]
                    return bool(values) and all(KEY_NAME.fullmatch(v) for v in values)
    except OSError:
        pass
    return False

# Recognized dependency files -> ecosystem
MANIFESTS = [(re.compile(r"(^|/)requirements[^/]*\.(txt|in)$"), "pypi"), (re.compile(r"(^|/)pyproject\.toml$"), "pypi"),
             (re.compile(r"(^|/)package\.json$"), "npm"), (re.compile(r"(^|/)go\.mod$"), "go")]
REGISTRY = {"pypi": "PyPI", "npm": "npm", "go": "the Go proxy"}


# --- Secrets -------------------------------------------------------------------------------------------------------------

def secrets(args, cwd):
    """Runs Betterleaks (secret validation disabled: nothing is sent to the concerned services); returns
    [(file, line, rule)], or None if the tool is missing."""
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
        line = int(f.get("StartLine") or 0)
        if GENERIC.match(rule) and (TEST_PATH.search(path) or key_name_only(cwd, path, line)):
            continue
        out.append((path, line, rule))
    return sorted(set(out))


# --- Packages ------------------------------------------------------------------------------------------------------------

def ecosystem(path):
    return next((e for pat, e in MANIFESTS if pat.search(path)), None)


def _pyname(spec):
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec or "")
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else None


def packages(path, text):
    """Names of packages declared in a dependency file (public registry only: paths, URLs, and Git repos
    are ignored)."""
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
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fabrique-security-check"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def lookup(eco, name, now=None, get=_get):
    """What the public registry says: ("absent", None), ("ok", age in days or None) or ("unknown", reason)."""
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
        return ("absent", None) if e.code in (404, 410) else ("unknown", M("response {code} from the registry", code=e.code))
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown", "registry unreachable"
    if not first:
        return "ok", None
    try:
        t = calendar.timegm(time.strptime(first[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return "ok", None
    return "ok", max(0, int((now - t) // 86400))


def check_packages(changes, get=_get):
    """changes: [(file, old text, new text)]. Returns (alerts, notes, number of packages reviewed)."""
    alerts, notes, n = [], [], 0
    for path, old, new in changes:
        eco = ecosystem(path)
        added = sorted(packages(path, new) - packages(path, old))
        for name in added:
            if n >= MAX_PACKAGES:
                notes.append(M("{path}: other added packages are not checked (limit of {max_packages}).", path=path, max_packages=MAX_PACKAGES))
                return alerts, notes, n
            n += 1
            state, info = lookup(eco, name, get=get)
            if state == "absent":
                alerts.append(M("{path}: package “{name}” cannot be found on {p}. The name is probably misspelled or made up: fix it with the exact name of the intended package, or remove it.", path=path, name=name, p=REGISTRY[eco]))
            elif state == "ok" and info is not None and info < YOUNG_DAYS:
                alerts.append(M("{path}: package “{name}” has existed on {p} for only {info} day(s). A package this recent may be a trap imitating a known name: check it is the intended one, otherwise replace it.", path=path, name=name, p=REGISTRY[eco], info=info))
            elif state == "unknown":
                notes.append(M("{path}: “{name}” not checked ({info}).", path=path, name=name, info=info))
    return alerts, notes, n


# --- The two uses -----------------------------------------------------------------------------------------------------

def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout if p.returncode == 0 else ""


def _say(message):
    """Line in the job activity (runner relay, job token). Without relay: nothing."""
    if not os.environ.get("FAB_TOKEN"):
        return
    req = urllib.request.Request(os.environ.get("FAB_RELAY", "http://127.0.0.1:7071") + "/fab",
                                 data=json.dumps({"cmd": "check", "message": message[:900]}).encode(),
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
    found = []   # (key, message): the key survives line moves, so an alert already given is not given again
    for path, line, rule in secrets(["dir", rel], root) or []:
        found.append((f"{path}|{rule}|{_line_hash(root, path, line)}",
                      M("{path} line {line}: hard-coded secret (rule {rule}). Remove it and read it from an environment variable or the configuration; never copy its value, here or in a message.", path=path, line=line, rule=rule)))
    if ecosystem(rel):
        try:
            new = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            new = ""
        alerts, _, _ = check_packages([(rel, _git(["show", f"HEAD:{rel}"], root), new)], get=get)
        found += [(f"{rel}|{a}", a) for a in alerts]
    problems = _new(found)
    if not problems:
        return 0
    _say(M("Check on {rel}: ", rel=rel) + " ".join(p.split(": ", 1)[1].split(".")[0] for p in problems)[:600] + M(". The agent is asked to fix it."))
    print(M("Fabrique check: fix before going on.\n- ") + "\n- ".join(problems), file=sys.stderr)
    return 2


def _line_hash(root, path, line):
    import hashlib
    try:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
            for i, text in enumerate(f, 1):
                if i == line:
                    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]
    except OSError:
        pass
    return str(line)


def _new(found):
    """Messages not given yet in this job: the agent is asked once per alert. If it chose to keep the line (a false
    alert), it is not stopped again at each edit of the file; the end-of-job report still lists every alert."""
    path = os.path.join(os.environ.get("HOME") or "/tmp", ".fabrique-alerts-seen")
    try:
        with open(path, encoding="utf-8") as f:
            seen = set(f.read().split("\n"))
    except OSError:
        seen = set()
    fresh = [(k, m) for k, m in found if k not in seen]
    if fresh:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("".join(k + "\n" for k, _ in fresh))
        except OSError:
            pass
    return [m for _, m in fresh]


def diff(base, cwd=".", get=_get):
    """Report on everything the job has changed. First line: the summary (read by the runner and the dashboard)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", base or ""):
        print(M("Security check: invalid base"))
        return 0
    lines, alerts = [], 0
    found = secrets(["git", f"--log-opts={base}..HEAD", "."], cwd)
    if found is None:
        lines.append(M("Secrets: not checked (Betterleaks missing from the job image)."))
    elif found:
        alerts += len(found)
        lines.append(M("Hard-coded secrets:"))
        lines += [M("  ⚠ {p} line {l} (rule {r})", p=p, l=l, r=r) for p, l, r in found]
    else:
        lines.append(M("Secrets: none in the job's commits."))
    changed = [f for f in _git(["diff", "--name-only", "--no-renames", f"{base}..HEAD"], cwd).splitlines() if ecosystem(f)]
    if changed:
        pa, notes, n = check_packages([(f, _git(["show", f"{base}:{f}"], cwd), _git(["show", f"HEAD:{f}"], cwd)) for f in changed], get=get)
        alerts += len(pa)
        lines.append(M("Added packages: {n} checked against their registry.", n=n) if n else M("Added packages: none."))
        lines += [f"  ⚠ {a}" for a in pa] + [f"  · {x}" for x in notes]
    else:
        lines.append(M("Packages: no dependency file changed."))
    head = M("Security check: {alerts} alert(s)", alerts=alerts) if alerts else M("Security check: nothing to report")
    print(head + "\n" + "\n".join(lines) + M("\n\nFor information: this check does not block the job."))
    print(f"{ALERTS_LINE}{alerts}")     # read by the runner: the number of alerts, never taken from the wording above
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Job repo created by another user (shared volume, NFS): Git must not refuse it as "suspicious".
    os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"})
    if argv[:1] == ["hook"]:
        return hook()
    if argv[:1] == ["diff"] and len(argv) == 2:
        return diff(argv[1])
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
