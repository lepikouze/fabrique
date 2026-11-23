"""The repository is written in English: code, comments, file names, documentation. French lives in the French catalogues
and in README.fr.md, and in test data written in French on purpose (what a French-speaking user types, what an agent
answers in French). This test fails when French comes back anywhere else."""
import io, os, re, subprocess, tokenize

ROOT = os.path.join(os.path.dirname(__file__), "..")
ACCENT = re.compile(r"[éèêàùçôîûœÉÈÀÇ«»]")
WORD = re.compile(r"[A-Za-z]{4,}")
# Function words: two different ones on a line make French (one alone can be English: "pour", "sans", "est", "dans").
COMMON = re.compile(r"\b(pour|avec|dans|les|des|une|est|pas|qui|que|cette|ces|leur|leurs|doit|peut|fait|sinon|donc|puis|aussi|"
                    r"jamais|toujours|seulement|chaque|tous|toute|toutes|mais|avoir|quand|alors|ainsi|entre|vers|chez|dont|soit|afin|lors|"
                    r"depuis|pendant|selon|sont|ont|nous|vous|ils|elle|elles|sur|sous|plus|moins|tout|rien|autre|autres|encore|voir|faire|"
                    r"dire|mettre|quoi|oui|non)\b", re.I)
# Words that are French and not English (the most frequent French words, minus English ones and the project's own terms).
FRENCH_ONLY = set(open(os.path.join(os.path.dirname(__file__), "data", "french_only_words.txt"), encoding="utf-8").read().split())
# French kept on purpose: the catalogues, the French README, binary assets, the word list above, and the lines listed below.
SKIP = ("app/locales/fr.json", "runner/locales/fr.json", "README.fr.md", "app/static/fonts/LICENCE-", "docs/img/", "tests/test_english.py",
        "tests/data/french_only_words.txt")
BINARY = (".png", ".gif", ".woff", ".woff2", ".ttf", ".ico", ".pdf")
ALLOWED = (
    '"fr": "Français"',                 # the name of the language, in the language
    "[Français](README.fr.md)",         # link to the French README
    "English, Français, or Auto",       # the picker's labels, quoted in the documentation
    "[A-Za-zÀ-ÿ]", "[À-ÿ ]",            # letter classes in the translation engine
    "ACCENT = re.compile(",             # the extractor's own French detector
    "| `",                              # the table of renamed things in the changelog names the former French names
)


def french(line):
    if any(a in line for a in ALLOWED):
        return False
    if ACCENT.search(line) or len({w.lower() for w in COMMON.findall(line)}) >= 2:
        return True
    return any(w.lower() in FRENCH_ONLY for w in WORD.findall(line))


def files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
        found = [f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        found = []
    if not found:      # outside a Git checkout (an image, an archive): walk the tree
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules", ".pytest_cache")]
            found += [os.path.relpath(os.path.join(base, n), ROOT) for n in names]
    return [f for f in found if not f.endswith(BINARY) and not any(s in f for s in SKIP)]


def prose_of_a_test(path, src):
    """In tests, only comments and docstrings must be English: the data may be French on purpose."""
    lines = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return [(i, l) for i, l in enumerate(src.split("\n"), 1) if l.lstrip().startswith("#")]
    prev = None
    for t in toks:
        if t.type == tokenize.COMMENT:
            lines.append((t.start[0], t.string))
        elif t.type == tokenize.STRING and t.string.lstrip("rRuUbB").startswith(('"""', "'''")) and (prev is None or prev.type in (
                tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.ENCODING, tokenize.COMMENT)):
            lines += [(t.start[0] + i, l) for i, l in enumerate(t.string.split("\n"))]
        if t.type not in (tokenize.NL, tokenize.COMMENT):
            prev = t
    return lines


def test_no_french_outside_the_french_catalogues():
    found = []
    for f in files():
        try:
            src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        lines = prose_of_a_test(f, src) if f.startswith("tests/") else list(enumerate(src.split("\n"), 1))
        found += [f"{f}:{n}: {l.strip()[:140]}" for n, l in lines if french(l)]
    assert not found, "French text outside the French catalogues:\n  " + "\n  ".join(found[:60])


def test_no_french_file_name():
    french_names = re.compile(r"(lanceur|garde|controle|textes|fonctionnement|securite|developpement|banniere|schema-|etude|jeton|travail)")
    assert not [f for f in files() if french_names.search(os.path.basename(f))]


def test_the_detector_sees_french_and_leaves_english_alone():
    assert french("# le runner relance les checks pour chaque job") and french("Sécurité")
    assert not french("# the runner replays the checks for each job") and not french('LANGS = {"fr": "Français", "en": "English"}')
