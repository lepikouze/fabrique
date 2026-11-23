"""Diff of a job, as sent by the runner (unified git diff): split by file for display. Read-only,
no git call: the dashboard never touches the code, it organizes text. Everything coming out of here is plain text,
escaped by templates."""
from .i18n import M
import re

MAX_TEXT = 500_000        # diff characters kept per job; beyond, cut and flagged as "truncated"
MAX_FILES = 500           # files summarized by diff
MAX_PATH = 300
OPEN_UNDER = 400          # total modified lines: below this, files are displayed expanded

_HEAD = re.compile(r"^diff --git (.*)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
STATUS = {"added": M("added"), "deleted": M("deleted"), "modified": M("modified"), "binary": M("binary")}


def _unquote(p):
    """Path as git writes it: in quotes, with escapes, when it contains special characters."""
    p = p.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1", "replace").decode("utf-8", "replace")
        except (UnicodeError, ValueError):
            p = p[1:-1]
    return p


def _side(line, prefix):
    """Path of a line "--- a/x" or "+++ b/x"; None for /dev/null."""
    p = _unquote(line[4:].split("\t", 1)[0])
    if p == "/dev/null":
        return None
    return p[2:] if p.startswith(prefix) else p


def parse(text):
    """[{path, status, add, rem, binary, hunks: [{header, lines: [(kind, old, new, text)]}]}] ; kind : ctx, add, rem, note.
    The "@@" header counters indicate where each block ends: a content line that looks like a header
    ("+++", "diff --git") remains part of the content."""
    files, cur, hunk, old, new, left_old, left_new = [], None, None, 0, 0, 0, 0
    for line in (text or "").split("\n"):
        inside = hunk is not None and (left_old > 0 or left_new > 0)
        if inside and line[:1] in ("+", "-", " ", ""):
            if line.startswith("+"):
                hunk["lines"].append(("add", None, new, line[1:])); new += 1; left_new -= 1; cur["add"] += 1
            elif line.startswith("-"):
                hunk["lines"].append(("rem", old, None, line[1:])); old += 1; left_old -= 1; cur["rem"] += 1
            else:
                hunk["lines"].append(("ctx", old, new, line[1:])); old += 1; new += 1; left_old -= 1; left_new -= 1
            continue
        if hunk is not None and line.startswith("\\"):
            hunk["lines"].append(("note", None, None, M("no newline at end of file")))
            continue
        m = _HEAD.match(line)
        if m:
            raw = m.group(1)
            # "a/path b/path" : without renaming (the runner disables it), both halves are identical.
            half = raw[len(raw) // 2 + 1:] if len(raw) % 2 and raw[len(raw) // 2] == " " else raw.rsplit(" b/", 1)[-1]
            path = _unquote(half)
            cur = {"path": (path[2:] if path.startswith("b/") else path)[:MAX_PATH], "status": "modified", "add": 0, "rem": 0,
                   "binary": False, "hunks": []}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        h = _HUNK.match(line)
        if h:
            old, new = int(h.group(1)), int(h.group(3))
            left_old, left_new = int(h.group(2) or 1), int(h.group(4) or 1)
            hunk = {"header": line[:200], "lines": []}
            cur["hunks"].append(hunk)
        elif hunk is None:   # file header
            if line.startswith("new file mode"):
                cur["status"] = "added"
            elif line.startswith("deleted file mode"):
                cur["status"] = "deleted"
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur["binary"], cur["status"] = True, "binary" if cur["status"] == "modified" else cur["status"]
            elif line.startswith("+++ "):
                p = _side(line, "b/")
                if p:
                    cur["path"] = p[:MAX_PATH]
            elif line.startswith("--- "):
                p = _side(line, "a/")
                if p and cur["status"] == "deleted":
                    cur["path"] = p[:MAX_PATH]
        elif line.strip():   # block cut by the cap ("[... truncated diff]" ) or unexpected line: shown as is
            hunk["lines"].append(("note", None, None, line[:400]))
    return files


def summary(text):
    """Summary kept in database upon receipt: [{path, add, rem, status}], to display the list without re-reading the text."""
    return [{"path": f["path"], "add": f["add"], "rem": f["rem"], "status": f["status"]} for f in parse(text)[:MAX_FILES]]


def clip(text):
    """(text kept, cut?) : cut at MAX_TEXT, at a line end."""
    text = str(text or "").replace("\r\n", "\n").replace("\x00", "")
    if len(text) <= MAX_TEXT:
        return text, False
    cut = text.rfind("\n", 0, MAX_TEXT)
    return text[:cut if cut > 0 else MAX_TEXT], True


def view(row, protected=()):
    """What the template displays: split files, totals, protected files flagged, expanded or not by default."""
    files = parse(row["text"])
    total = sum(f["add"] + f["rem"] for f in files)
    prot = set(protected)
    for f in files:
        f["label"] = STATUS[f["status"]]
        f["protected"] = f["path"] in prot
        f["open"] = total <= OPEN_UNDER and not f["binary"]
    return {"sha": row["sha"], "truncated": bool(row["truncated"]), "files": files, "add": sum(f["add"] for f in files),
            "rem": sum(f["rem"] for f in files), "at": row["at"]}
