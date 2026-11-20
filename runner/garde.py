#!/usr/bin/env python3
"""Hook PreToolUse de Claude Code (installé en réglages gérés, /etc/claude-code : l'agent ne peut pas le retirer).
Refuse : toute opération Git distante ou de configuration, le réseau hors installation de paquets, la lecture de
l'environnement et de /proc, et tout fichier hors de l'espace de travail du job. Code 2 = outil bloqué, message à l'agent.
Ce n'est qu'une couche : l'agent tourne aussi sous un utilisateur sans secrets ni droits Git."""
import json, os, re, sys

BASH_DENY = [
    (r"\bgit\s+(push|remote|config|credential|fetch|pull|clone|submodule)\b", "les opérations Git distantes et la configuration Git sont réservées au runner"),
    (r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|rsync|telnet|socat)\b", "pas d'accès réseau direct depuis l'agent"),
    (r"\b(env|printenv|export)\b|/proc/|\$\{?(ANTHROPIC|CLAUDE_CODE|GIT_|FAB_TOKEN)", "pas de lecture de l'environnement ni de /proc"),
    (r"\b(sudo|su|setpriv|chroot|nsenter|mount|docker|podman|kubectl)\b", "pas d'élévation de droits ni d'accès aux conteneurs"),
    (r"(^|\s)(/etc/claude-code|~/.claude|\.claude/settings)", "les réglages de l'agent ne se modifient pas"),
]


def deny(msg):
    print(f"Bloqué par la Fabrique : {msg}.", file=sys.stderr)
    sys.exit(2)


def main():
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        deny("requête illisible")
    tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
    root = os.path.realpath(os.environ.get("FAB_WORKSPACE") or ev.get("cwd") or ".")
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        for pat, why in BASH_DENY:
            if re.search(pat, cmd):
                deny(why)
    elif tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"):
        p = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or root
        full = os.path.realpath(os.path.join(root, os.path.expanduser(str(p))))
        if full != root and not full.startswith(root + os.sep):
            deny("fichier hors de l'espace de travail du job")
        if tool not in ("Read", "Glob", "Grep") and (os.sep + ".git" + os.sep in full + os.sep):
            deny("le dossier .git ne se modifie pas directement")
    elif tool in ("WebFetch", "WebSearch"):
        deny("pas d'accès web depuis l'agent")
    sys.exit(0)


if __name__ == "__main__":
    main()
