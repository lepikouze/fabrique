"""Texts of Fabrique, for the language catalogues (app/locales, runner/locales). English is the source language: a text
shown to someone is written in English in the code, marked with M("…", name=value) (or W(ctx, "…") for what the runner
writes on the Git host), and translated from the catalogues. This tool lists those texts and says which ones have no
translation.

    python tools/i18n_extract.py            # what is missing in each language
    python tools/i18n_extract.py --all      # everything that is marked

Template texts are found separately, by app/i18n.py (template_texts)."""
import ast, glob, json, os, re, sys

ACCENT = re.compile(r"[éèàêçùôîâûÉÈÀ«»]")          # a French text left in the code


def marked(path):
    """[(line, function name, template)] for every M("…") and W(ctx, "…") call of a file."""
    out = []
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("M", "W"):
            arg = node.args[1 if node.func.id == "W" else 0] if len(node.args) > (1 if node.func.id == "W" else 0) else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((node.lineno, node.func.id, arg.value))
    return out


def files(root="."):
    return sorted(p for d in ("app", "runner", "launcher") for p in glob.glob(os.path.join(root, d, "*.py")))


def code_texts(root=".", kind="M"):
    out = {}
    for f in files(root):
        for line, fn, text in marked(f):
            if fn == kind:
                out.setdefault(text, f"{os.path.relpath(f, root)}:{line}")
    return out


def french_left(root="."):
    """String literals that still carry French (outside docstrings): [(file:line, text)]."""
    out = []
    for f in files(root):
        tree = ast.parse(open(f, encoding="utf-8").read())
        doc = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(getattr(node.body[0], "value", None), ast.Constant):
                doc.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc and ACCENT.search(node.value):
                out.append((f"{os.path.relpath(f, root)}:{node.lineno}", node.value))
    return out


if __name__ == "__main__":
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    texts = code_texts(root)
    if "--all" in sys.argv:
        for t, where in texts.items():
            print(where, "|", t.replace("\n", "⏎"))
        sys.exit(0)
    bad = 0
    for path in sorted(glob.glob(os.path.join(root, "app", "locales", "*.json"))):
        lang = os.path.basename(path)[:-5]
        if lang == "en":
            continue
        known = json.load(open(path, encoding="utf-8"))["messages"]
        for t, where in texts.items():
            if t not in known:
                bad += 1
                print(f"[{lang}] {where} | " + t.replace("\n", "⏎"))
    print(bad, "text(s) without a translation")
    sys.exit(1 if bad else 0)
