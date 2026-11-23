"""Guard hook, Bash part (runner/guard.py): a forbidden word is refused when it is run, in any command position, and
accepted when it is only part of a file name or of a quoted text. Each bypass attempt listed here must stay refused."""
import os, sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))
import guard  # noqa: E402

ALLOWED = [
    # the five commands that were refused by mistake
    "cat docker-compose.yml",
    "python carnet/export.py",
    "cat .env.example",
    'git commit -m "add CSV export"',
    'fab step --summary "fix the mount point"',
    # infrastructure repos: file and folder names
    "cat Dockerfile docker/entrypoint.sh",
    "ls docker env mount",
    "cd env && ls",
    "mkdir -p deploy/docker deploy/mount && touch deploy/docker/.env",
    "grep -rn 'docker run' docs/",
    "grep -n export app/static/app.js | head -5",
    "sed -n '1,40p' docker-compose.override.yml",
    "sed -i 's/docker/podman/' README.md",
    "diff -u .env.example .env.sample",
    "git add docker-compose.yml .env.example mount/",
    "git diff -- docs/ssh.md scripts/curl.md",
    "git commit -m 'docs: explain sudo, docker and kubectl usage; mention env and export'",
    'git commit -am "fix: git push section of the guide"',
    "git log --oneline -5 && git status",
    "git -C sub status",
    "git -c user.name=agent commit -m x",
    'fab step --summary "docker image builds; ssh config documented; curl example fixed" --confident yes',
    'fab fail --reason "the env file is missing"',
    "wc -l scripts/export.sh scripts/rsync-notes.txt",
    "python -m pytest -q tests/test_export.py",
    ".venv/bin/python -m pytest -q",
    "python3 -m venv .venv",
    'python3 -c "print(1 + 1)"',
    "bash scripts/build.sh",
    "sh -c 'ls -la && cat docker-compose.yml'",
    "find . -name 'docker-compose*.yml' -not -path './.git/*'",
    "find . -name docker -exec cat {} \;",
    "timeout 60 python -m pytest -q",
    "xargs -n1 cat < files.txt",
    "echo \"export PATH\" >> notes.md",
    "cat > notes.md <<EOF\nprice: $(date +%Y) and `basename x`\nEOF",
    "source .venv/bin/activate && python -m pytest -q",
    "trap 'rm -f tmp.txt' EXIT; ls",
    "time -p python -m pytest -q",
    "cat > docker-compose.yml <<'EOF'\nservices:\n  app:\n    image: docker.io/library/nginx\n    environment:\n      - env=prod\nEOF",
    "npm run export",
    "npm test -- --env=jsdom",
    "for f in docker env; do ls $f; done",
    "[ -f .env ] && echo present",
    "\"$HOME/.local/bin/ruff\" check .",
    "make docker-build 2>&1 | tail -5",
    "echo done > env.log 2>&1",
    "set -e; ls",
]

REFUSED = [
    # every command position
    "env", "printenv", "export FOO=1", "sudo ls", "docker ps", "curl http://example.org", "mount /dev/sda /mnt",
    "ls; env", "ls && printenv", "ls || docker ps", "ls | env", "ls & env", "true\nenv",
    "echo $(env)", "echo `printenv`", 'git commit -m "$(env)"', 'echo "`docker ps`"',
    "(env)", "{ env; }", "if true; then env; fi", "while true; do docker ps; done", "! env",
    "A=b env", "A=b B=c docker ps", "FOO=1 /usr/bin/env",
    # launchers and wrappers
    'sh -c "env"', "bash -c 'docker ps'", "bash -lc env", "zsh -c 'curl x'", "dash -c printenv",
    "eval env", 'eval "docker ps"', "ls | xargs env", "xargs -I{} docker rm {}", "nohup docker ps",
    "time env", "timeout 5 curl x", "timeout -s KILL 5 env", "nice -n 5 docker ps", "stdbuf -o0 env",
    "watch docker ps", 'watch "docker ps"', "find . -exec env {} \;", "find . -name x -execdir docker ps \;",
    "parallel env ::: a", "command env", "command -v docker", "builtin export A=1", "exec env", "busybox wget x",
    "setsid docker ps", "poetry run env", "uv run curl x", "npx env", "bundle exec docker ps",
    # paths to the binaries
    "/usr/bin/env", "/bin/su", "./docker ps", "../bin/kubectl get pods", "/usr/bin/curl x",
    # family names
    "docker-compose up", "podman-compose up", "ssh-keyscan host", "ssh host", "scp a b:", "rsync -a a b:",
    # here-documents and pipes into a shell or an interpreter
    "bash <<EOF\nenv\nEOF", "sh <<'X'\ndocker ps\nX", "cat <<EOF | sh\nprintenv\nEOF", "echo env | sh", "echo 'docker ps' | bash -s",
    "python3 - <<EOF\nimport os\nprint(os.environ)\nEOF",
    # inline interpreter code (at least the former behaviour)
    'python3 -c "import subprocess; subprocess.run([\'env\'])"', 'python3 -c "import os; print(os.environ)"',
    "node -e \"console.log(process.env)\"", "perl -e 'print %ENV'", "ruby -e 'puts ENV[\"X\"]'",
    "awk 'BEGIN{print ENVIRON[\"HOME\"]}'", "awk 'BEGIN{system(\"env\")}'", "sed '1e env' x",
    # simple encodings and splits
    "e''nv", 'e""nv', "\\env", "$'env'", "$'\\x65nv'", "'env'", '"env"', "X=env; $X", "X=docker; $X ps", "${X}",
    "$(echo env)", "`echo env`", "en?", "e{n,}v", "$'docker' ps",
    # listing variables another way
    "set", "set | grep TOKEN", "declare -p", "declare -x", "typeset -p", "compgen -v",
    # anywhere, quoted or not
    "echo $ANTHROPIC_API_KEY", 'echo "${FAB_TOKEN}"', "cat /proc/1/environ", 'git commit -m "see /proc/self/environ"',
    "x=FAB_TOKEN; echo ${!x}", "cat ~/.claude/settings.json", "ls /etc/claude-code",
    # git
    "git push origin main", "git remote -v", "git config user.email x", "git -C sub push", "git  fetch", "git pull",
    "git clone x", "git submodule add x", "git -c alias.p=push p", "git $SUB", "timeout 5 git push", "sh -c 'git push'",
    # substitutions hidden where a naive reading would miss them
    "echo ${X:-$(env)}", "echo $(( $(env) ))", "echo \"${X:-`env`}\"", "cat <<EOF\n$(env)\nEOF", "cat > out.txt <<EOF\n`docker ps`\nEOF",
    "cat <<< \"$(env)\"", "ls > $(env)", "diff <(env) <(ls)", "f() { env; }; f",
    # other ways of running a text
    "time -p env", "trap env EXIT", "trap 'docker ps' EXIT", ". /dev/stdin <<< env", "source <(echo env)", "echo ZW52 | base64 -d | sh",
    "echo ls | bash", "exec -a x env", "alias x=env; x", "alias d='docker ps'",
    # cannot be read with certainty: the broad rule applies
    "echo 'env", 'echo "docker', "cat <<EOF\ndocker\n",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    assert guard.bash_verdict(cmd) is None, cmd


@pytest.mark.parametrize("cmd", REFUSED)
def test_refused(cmd):
    assert guard.bash_verdict(cmd), cmd


def test_unreadable_line_without_a_forbidden_word_passes():
    assert guard.bash_verdict("echo 'unfinished") is None


def test_the_refusal_tells_the_agent_what_is_fine():
    assert "file name" in guard.bash_verdict("docker ps")
    assert "literally" in guard.bash_verdict("X=ls; $X")
