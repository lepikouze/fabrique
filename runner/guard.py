#!/usr/bin/env python3
"""Claude Code PreToolUse hook (installed as a managed setting, /etc/claude-code: the agent cannot remove it).
Refuses: any remote or configuration Git operation, the network outside package installation, reading the
environment and /proc, and any file outside the job's workspace. Exit code 2 = tool blocked, message to the agent.
It is only one layer: the agent also runs as a user with no secrets and no Git rights. The messages go to a model: they
are in English whatever the job language.

Bash commands are read the way a shell would read them: a forbidden word is refused when it is *run* (in any command
position: after ; && || |, inside $(...) or backticks, behind sh -c, eval, xargs, timeout, find -exec...), not when it
is only part of a file name or of a quoted text (`cat docker-compose.yml`, `git commit -m "add CSV export"`).
Whenever the line cannot be read with certainty, the former broad rule applies: the word anywhere refuses the line."""
import json, os, re, sys

WHY_GIT = "remote Git operations and Git configuration are reserved for the runner"
WHY_NET = "no direct network access from the agent"
WHY_ENV = "no reading of the environment or of /proc"
WHY_PRIV = "no privilege escalation and no access to containers"
WHY_SETTINGS = "the agent's settings cannot be changed"
WHY_DYNAMIC = ("the command name must be written literally (no variable, substitution, wildcard or escape sequence in "
               "command position)")
WHY_NESTED = "a command substitution inside ${...} or $((...)) is refused: write the command on its own line"
WHY_STDIN = ("a shell that reads its commands from a pipe, a here-document or /dev/stdin is refused: write the commands "
             "directly")
HINT = " (a file name or a quoted text that merely contains such a word is fine; running it is not)"

# The former rules, word anywhere in the line. Still used as they are on text that is executed but cannot be parsed
# (inline interpreter code, a shell fed from a pipe or a here-document, an ambiguous line).
BROAD = [
    (r"\bgit\s+(push|remote|config|credential|fetch|pull|clone|submodule)\b", WHY_GIT),
    (r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|socat)\b", WHY_NET),
    (r"\b(env|printenv|export)\b|/proc/|\$\{?(ANTHROPIC|CLAUDE_CODE|GIT_|FAB_TOKEN)", WHY_ENV),
    (r"\b(sudo|su|setpriv|chroot|nsenter|mount|docker|podman|kubectl)\b", WHY_PRIV),
]
# Refused wherever they appear, quoted or not: the sensitive variables, /proc, indirect expansion, the agent's settings.
ANYWHERE = [
    (r"/proc/|\$\{?(ANTHROPIC|CLAUDE_CODE|GIT_|FAB_TOKEN)|\$\{!", WHY_ENV),
    (r"(^|\s)(/etc/claude-code|~/.claude|\.claude/settings)", WHY_SETTINGS),
]
INLINE_EXTRA = r"environ|getenv|process\.env|%ENV|\$ENV|ENV\[|ENVIRON"   # reading the environment from inline code

NET = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "telnet", "socat"}
ENV = {"env", "printenv", "export", "compgen"}
PRIV = {"sudo", "su", "doas", "runuser", "setpriv", "chroot", "nsenter", "unshare", "mount", "umount", "docker", "podman", "kubectl"}
FAMILY = ("docker", "podman", "kubectl", "ssh")       # docker-compose, podman-compose, ssh-keyscan...: same refusal
GIT_SUB = {"push", "remote", "config", "credential", "fetch", "pull", "clone", "submodule"}
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env", "--super-prefix"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"}
INTERPRETERS = {"python": ("-c",), "python3": ("-c",), "pypy": ("-c",), "pypy3": ("-c",), "node": ("-e", "--eval", "-p", "--print"),
                "nodejs": ("-e", "--eval", "-p", "--print"), "deno": ("eval",), "bun": ("-e", "--eval"), "perl": ("-e", "-E"),
                "ruby": ("-e",), "php": ("-r",), "lua": ("-e",)}
AWKS = {"awk", "gawk", "mawk", "nawk"}
LAUNCHERS = {"xargs", "nohup", "time", "timeout", "nice", "ionice", "stdbuf", "watch", "command", "builtin", "exec", "busybox",
             "parallel", "setsid", "chrt", "taskset", "strace", "ltrace", "unbuffer", "flock", "script", "sem", "caffeinate"}
WRAPPERS = {"poetry": {"run"}, "pipenv": {"run"}, "uv": {"run"}, "pdm": {"run"}, "hatch": {"run"}, "conda": {"run"},
            "mamba": {"run"}, "pixi": {"run", "exec"}, "npm": {"exec", "x"}, "pnpm": {"exec", "dlx"}, "yarn": {"exec", "dlx"},
            "bundle": {"exec"}, "direnv": {"exec"}, "npx": None, "bunx": None, "dotenv": None, "pipx": {"run"}}
KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}", "time", "coproc"}
DATA_SINKS = {"cat", "tee"}                           # a here-document given to these is data, not code
MAX_DEPTH = 6


class Ambiguous(Exception):
    """The line cannot be read with certainty: the caller falls back on the broad rules."""


class Denied(Exception):
    pass


class Tok:
    __slots__ = ("value", "dynamic")

    def __init__(self, value, dynamic):
        self.value, self.dynamic = value, dynamic


def lex(cmd):
    """Split a command line the way a shell does. Returns (groups, subs): groups is a list of (commands, heredocs) per
    physical statement line, commands being lists of Tok; subs are the texts of $(...) and backticks, to be read in turn."""
    groups, subs = [], []
    cmds, cur, bodies, pending = [], [], [], []
    buf, dyn, started = [], False, False
    skip_word = here_next = False
    i, n = 0, len(cmd)

    def end_token():
        nonlocal buf, dyn, started, skip_word, here_next
        if started:
            value = "".join(buf)
            if here_next:
                pending.append(value)
                here_next = False
            elif skip_word:
                skip_word = False
            else:
                cur.append(Tok(value, dyn))
        buf, dyn, started = [], False, False

    def end_command():
        nonlocal cur
        end_token()
        if cur:
            cmds.append(cur)
        cur = []

    def substitution(start):
        """Text between the ( at `start` and its matching ). Quotes are honoured."""
        depth, j = 1, start + 1
        while j < n:
            c = cmd[j]
            if c == "\\":
                j += 2
                continue
            if c == "'":
                k = cmd.find("'", j + 1)
                if k < 0:
                    raise Ambiguous
                j = k + 1
                continue
            if c == '"':
                j += 1
                while j < n and cmd[j] != '"':
                    j += 2 if cmd[j] == "\\" else 1
                if j >= n:
                    raise Ambiguous
                j += 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return cmd[start + 1:j], j + 1
            j += 1
        raise Ambiguous

    def dollar(j, quoted):
        """Handle what follows a $ at position j. Returns the next position."""
        nonlocal dyn
        if cmd.startswith("$((", j):
            k = cmd.find("))", j)
            if k < 0:
                raise Ambiguous
            if "$(" in cmd[j + 3:k] or "`" in cmd[j + 3:k]:
                raise Denied(WHY_NESTED)
            dyn = True
            buf.append("\0")
            return k + 2
        if cmd.startswith("$(", j):
            text, k = substitution(j + 1)
            subs.append(text)
            dyn = True
            buf.append("\0")
            return k
        if cmd.startswith("${", j):
            k = cmd.find("}", j)
            if k < 0:
                raise Ambiguous
            if "$(" in cmd[j:k] or "`" in cmd[j:k]:
                raise Denied(WHY_NESTED)
            dyn = True
            buf.append(cmd[j:k + 1])
            return k + 1
        m = re.match(r"\$(\w+|[@*#?$!0-9-])", cmd[j:])
        if m:
            dyn = True
            buf.append(m.group(0))
            return j + len(m.group(0))
        if not quoted and cmd.startswith("$'", j):          # ANSI-C quoting: escapes may spell anything
            k = j + 2
            while k < n and cmd[k] != "'":
                k += 2 if cmd[k] == "\\" else 1
            if k >= n:
                raise Ambiguous
            body = cmd[j + 2:k]
            if "\\" in body:
                dyn = True
                buf.append("\0")
            else:
                buf.append(body)
            return k + 1
        if not quoted and cmd.startswith('$"', j):
            return j + 1
        buf.append("$")
        return j + 1

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 >= n:
                raise Ambiguous
            if cmd[i + 1] != "\n":
                buf.append(cmd[i + 1])
                started = True
            i += 2
            continue
        if c == "'":
            k = cmd.find("'", i + 1)
            if k < 0:
                raise Ambiguous
            buf.append(cmd[i + 1:k])
            started = True
            i = k + 1
            continue
        if c == '"':
            started = True
            i += 1
            while True:
                if i >= n:
                    raise Ambiguous
                d = cmd[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n:
                    buf.append(cmd[i + 1] if cmd[i + 1] in '"\\$`\n' else cmd[i:i + 2])
                    i += 2
                elif d == "$":
                    i = dollar(i, True)
                elif d == "`":
                    k = cmd.find("`", i + 1)
                    if k < 0:
                        raise Ambiguous
                    subs.append(cmd[i + 1:k])
                    dyn = True
                    buf.append("\0")
                    i = k + 1
                else:
                    buf.append(d)
                    i += 1
            continue
        if c == "`":
            k = cmd.find("`", i + 1)
            if k < 0:
                raise Ambiguous
            subs.append(cmd[i + 1:k])
            dyn = started = True
            buf.append("\0")
            i = k + 1
            continue
        if c == "$":
            started = True
            i = dollar(i, False)
            continue
        if c in " \t":
            end_token()
            i += 1
            continue
        if c == "#" and not started:
            k = cmd.find("\n", i)
            i = n if k < 0 else k
            continue
        if c == "\n":
            end_command()
            for delim in pending:                               # here-document bodies follow the statement line
                tabs = delim.startswith("\t-")
                word = delim[2:] if tabs else delim
                lines = []
                while True:
                    if i + 1 > n:
                        raise Ambiguous
                    k = cmd.find("\n", i + 1)
                    line = cmd[i + 1:] if k < 0 else cmd[i + 1:k]
                    i = n if k < 0 else k
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
                    if k < 0:
                        raise Ambiguous
                    lines.append(line)
                bodies.append("\n".join(lines))
            pending.clear()
            if cmds or bodies:
                groups.append((cmds, bodies))
            cmds, bodies = [], []
            i += 1
            continue
        if c in ";|&()":
            if c == "&" and i + 1 < n and cmd[i + 1] == ">":       # &> file
                end_token()
                skip_word = True
                i += 2
                if i < n and cmd[i] == ">":
                    i += 1
                continue
            end_command()
            i += 1
            continue
        if c in "<>":
            if started and all(x.isdigit() for x in buf) and not dyn:
                buf, started = [], False                           # 2> : the digit is a file descriptor
            end_token()
            j = i
            while j < n and cmd[j] in "<>":
                j += 1
            op = cmd[i:j]
            if j < n and cmd[j] == "(":                            # <( ... ) process substitution: read as a subshell
                i = j
                continue
            if op in ("<<", "<<-") or (op.startswith("<<") and len(op) == 2):
                dash = j < n and cmd[j] == "-"
                if dash:
                    j += 1
                here_next = True
                if dash:                                           # remember that leading tabs are stripped
                    buf, started = ["\t-"], False
                    i = j
                    while i < n and cmd[i] in " \t":
                        i += 1
                    # read the delimiter word now, so that the marker stays attached to it
                    k = i
                    word = []
                    while k < n and cmd[k] not in " \t\n;|&()<>":
                        if cmd[k] in "'\"":
                            q = cmd.find(cmd[k], k + 1)
                            if q < 0:
                                raise Ambiguous
                            word.append(cmd[k + 1:q])
                            k = q + 1
                        elif cmd[k] == "\\" and k + 1 < n:
                            word.append(cmd[k + 1])
                            k += 2
                        else:
                            word.append(cmd[k])
                            k += 1
                    if not word:
                        raise Ambiguous
                    pending.append("\t-" + "".join(word))
                    here_next = False
                    buf, started = [], False
                    i = k
                    continue
            elif op == "<<<":
                pass                                               # here-string: the next word is an ordinary argument
            else:
                if j < n and cmd[j] == "&":                        # 2>&1, >&2
                    j += 1
                    while j < n and (cmd[j].isdigit() or cmd[j] == "-"):
                        j += 1
                    i = j
                    continue
                skip_word = True
            i = j
            continue
        if c in "*?[{":
            dyn = True                                             # wildcard or brace expansion: value unknown
        buf.append(c)
        started = True
        i += 1
    if pending:
        raise Ambiguous                                           # a here-document that never ends
    end_command()
    if cmds or bodies:
        groups.append((cmds, bodies))
    return groups, subs


def broad(text):
    for pat, why in BROAD:
        if re.search(pat, text):
            raise Denied(why)


def literal_name(tok):
    """Name of the program a command token runs, or Denied when it is not written literally."""
    v = tok.value
    if v in ("[", "[[", "{", "}"):
        return v
    if tok.dynamic:
        m = re.fullmatch(r"(\$\w+|\$\{\w+\}|~)((/[\w.+-]+)+)", v)        # "$VENV/bin/python": the program itself is literal
        if not m:
            raise Denied(WHY_DYNAMIC)
        v = m.group(2)
    return v.rsplit("/", 1)[-1]


def forbidden(base):
    if base in NET or base.split("-", 1)[0].split(".", 1)[0] == "ssh":
        return WHY_NET
    if base in ENV:
        return WHY_ENV
    if base in PRIV or base.split("-", 1)[0].split(".", 1)[0] in FAMILY:
        return WHY_PRIV
    return None


def check_simple(toks, raw, depth):
    while toks and (re.match(r"^\w+\+?=", toks[0].value) or toks[0].value in KEYWORDS):
        timed = toks[0].value == "time"
        toks = toks[1:]
        while timed and toks and toks[0].value.startswith("-"):    # time -p CMD
            toks = toks[1:]
    if not toks:
        return
    base = literal_name(toks[0])
    args = toks[1:]
    vals = [t.value for t in args]
    why = forbidden(base)
    if why:
        raise Denied(why + HINT)
    if base == "set" and not args:
        raise Denied(WHY_ENV)
    if base in ("declare", "typeset") and (not args or any(re.fullmatch(r"-\w*[px]\w*", v) for v in vals)):
        raise Denied(WHY_ENV)                                      # lists or exports the variables
    if base == "git":
        k = 0
        while k < len(args):
            v = args[k].value
            if v in ("-c", "--config-env"):
                if k + 1 >= len(args) or args[k + 1].dynamic or re.match(r"(alias|core\.sshcommand|core\.fsmonitor|core\.pager|core\.editor|credential|url)\b",
                                                                        args[k + 1].value, re.I):
                    raise Denied(WHY_GIT)
            if v in GIT_VALUE_OPTS:
                k += 2
                continue
            if v.startswith("-"):
                if re.match(r"-c\S|--config-env=", v):
                    raise Denied(WHY_GIT)
                k += 1
                continue
            if args[k].dynamic or v in GIT_SUB:
                raise Denied(WHY_GIT)
            break
        return
    if base in SHELLS:
        k, code, script = 0, None, False
        while k < len(args):
            v = args[k].value
            if re.fullmatch(r"-\w*c\w*", v) and not v.startswith("--"):
                if k + 1 >= len(args):
                    raise Ambiguous
                code = args[k + 1]
                break
            if v in ("-o", "+o", "--rcfile", "--init-file"):
                k += 2
                continue
            if v.startswith(("-", "+")) and v != "-":
                if v == "-s":
                    break
                k += 1
                continue
            script = v != "-"
            break
        if code is not None:
            if code.dynamic:
                broad(raw)                                         # built from a variable: only the broad rule can tell
            check_text(code.value.replace("\0", " "), depth + 1)
        elif not script:
            raise Denied(WHY_STDIN)
        return
    if base in ("source", "."):
        if any(v.startswith("/dev/") or v == "-" for v in vals) or "<(" in raw:
            raise Denied(WHY_STDIN)
        return
    if base == "alias":                                            # alias x='COMMANDS'
        for t in args:
            if "=" in t.value:
                if t.dynamic:
                    broad(raw)
                check_text(t.value.split("=", 1)[1].replace("\0", " "), depth + 1)
        return
    if base == "trap" and args:                                    # trap 'COMMANDS' SIGNAL
        first = next((t for t in args if not t.value.startswith("-")), None)
        if first is not None:
            if first.dynamic:
                broad(raw)
            check_text(first.value.replace("\0", " "), depth + 1)
        return
    if base == "eval":
        text = " ".join(v.replace("\0", " ") for v in vals)
        if any(t.dynamic for t in args):
            broad(raw)
        check_text(text, depth + 1)
        return
    ibase = re.sub(r"[\d.]+$", "", base) or base                   # python3.12 -> python
    if base in INTERPRETERS or ibase in INTERPRETERS:
        base = base if base in INTERPRETERS else ibase
        flags, inline, has_script = INTERPRETERS[base], False, False
        for k, t in enumerate(args):
            if t.value in flags or (base == "deno" and k == 0 and t.value == "eval"):
                inline = True
                code = " ".join(v for v in vals[k + 1:])
                broad(code)
                if re.search(INLINE_EXTRA, code):
                    raise Denied(WHY_ENV)
                break
            if not t.value.startswith("-") or t.value == "-m":
                has_script = t.value != "-"
                break
        if not inline and not has_script:
            broad(raw)                                             # program read from a pipe or a here-document
            if re.search(INLINE_EXTRA, raw):
                raise Denied(WHY_ENV)
        return
    if base in AWKS:
        k = 0
        while k < len(args):
            v = vals[k]
            if v in ("-F", "-v", "-f", "--field-separator", "--assign", "--file"):
                if v in ("-f", "--file"):
                    return
                k += 2
                continue
            if v.startswith("-") and v != "-":
                k += 1
                continue
            broad(v)
            if re.search(INLINE_EXTRA + r"|\bsystem\s*\(|\|\s*getline|\|\s*\"", v):
                broad(raw)
                if re.search(INLINE_EXTRA, v):
                    raise Denied(WHY_ENV)
            break
        return
    if base == "sed":
        for v in vals:
            if not v.startswith("-") and re.search(r"(^|[;{]|[0-9$/,])\s*e(\s|$)|/[gipIM0-9]*e[gipIM0-9w]*\s*($|[;}])", v):
                broad(raw)                                         # GNU sed can execute commands (the `e` command and flag)
        return
    if base == "find":
        for k, t in enumerate(args):
            if t.value in ("-exec", "-execdir", "-ok", "-okdir"):
                rest = []
                for u in args[k + 1:]:
                    if u.value in (";", "+") and not u.dynamic:
                        break
                    rest.append(u)
                check_simple(rest, raw, depth + 1)
        return
    if base in WRAPPERS:                                           # poetry run CMD, npx CMD, uv run CMD...
        subs_ok, k = WRAPPERS[base], 0
        while k < len(args) and vals[k].startswith("-"):
            k += 1
        if subs_ok is not None:
            if k >= len(args) or vals[k] not in subs_ok:
                return
            k += 1
        while k < len(args) and (vals[k].startswith("-") or re.match(r"^\w+=", vals[k])):
            k += 1
        if args[k:]:
            check_simple(args[k:], raw, depth + 1)
        return
    if base in LAUNCHERS:
        k = 0
        while k < len(args):
            v = vals[k]
            if base == "xargs" and v in ("-I", "-i", "-d", "-E", "-L", "-n", "-P", "-s", "-a", "--replace", "--max-args", "--max-procs", "--delimiter"):
                k += 2
                continue
            if base in ("timeout", "nice", "ionice", "stdbuf", "chrt", "taskset", "flock", "strace", "ltrace") and v in (
                    "-s", "-k", "--signal", "--kill-after", "-n", "-c", "-p", "-o", "-e", "-i", "-w", "--adjustment"):
                k += 2
                continue
            if v.startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", v) or re.match(r"^\w+=", v):
                k += 1
                continue
            break
        rest = args[k:]
        if base in ("flock",) and rest:                            # flock FILE command...
            rest = rest[1:]
        if rest:
            for t in rest:                                         # any later word naming a forbidden program
                if not t.dynamic and forbidden(t.value.rsplit("/", 1)[-1]):
                    raise Denied(forbidden(t.value.rsplit("/", 1)[-1]) + HINT)
            check_simple(rest, raw, depth + 1)
            if len(rest) == 1 or base in ("watch", "parallel", "sem", "script"):
                check_text(" ".join(t.value.replace("\0", " ") for t in rest), depth + 1)   # a command given as one string
        return


def check_text(text, depth=0):
    if depth > MAX_DEPTH:
        raise Ambiguous
    groups, subs = lex(text)
    for cmds, bodies in groups:
        for toks in cmds:
            check_simple(toks, text, depth)
        if bodies:
            names = set()
            for toks in cmds:
                rest = [t for t in toks if not re.match(r"^\w+\+?=", t.value)]
                if rest:
                    names.add(rest[0].value.rsplit("/", 1)[-1] if not rest[0].dynamic else "?")
            for body in bodies:
                if "$(" in body or "`" in body:                    # expanded by the shell unless the delimiter is quoted
                    try:
                        _, inner = lex('"' + body.replace("\\", "\\\\").replace('"', '\\"') + '"')
                    except Ambiguous:
                        broad(body)
                        inner = []
                    for sub in inner:
                        check_text(sub, depth + 1)
                if not names <= DATA_SINKS:                        # fed to a program that may run it: broad rule
                    broad(body)
                    if re.search(INLINE_EXTRA, body):
                        raise Denied(WHY_ENV)
    for sub in subs:
        check_text(sub, depth + 1)


def bash_verdict(cmd):
    """None when the command may run, else the reason for the refusal."""
    for pat, why in ANYWHERE:
        if re.search(pat, cmd):
            return why
    try:
        check_text(cmd)
    except Denied as e:
        return str(e)
    except (Ambiguous, RecursionError, IndexError):
        for pat, why in BROAD:                                     # cannot be read with certainty: the former broad rule
            if re.search(pat, cmd):
                return why + " (this line could not be analysed precisely: simplify it)"
    return None


def deny(msg):
    print(f"Blocked by Fabrique: {msg}.", file=sys.stderr)
    sys.exit(2)


def main():
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        deny("unreadable request")
    tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
    root = os.path.realpath(os.environ.get("FAB_WORKSPACE") or ev.get("cwd") or ".")
    if tool == "Bash":
        why = bash_verdict(str(inp.get("command") or ""))
        if why:
            deny(why)
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"):
        p = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or root
        full = os.path.realpath(os.path.join(root, os.path.expanduser(str(p))))
        if full != root and not full.startswith(root + os.sep):
            deny("file outside the job's workspace")
        if tool not in ("Read", "Glob", "Grep") and (os.sep + ".git" + os.sep in full + os.sep):
            deny("the .git directory is not to be changed directly")
    elif tool in ("WebFetch", "WebSearch"):
        deny("no web access from the agent")
    sys.exit(0)


if __name__ == "__main__":
    main()
