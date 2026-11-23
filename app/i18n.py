"""Languages. English is the source language: texts are written in English in the templates and in the code, and
worded for each reader from a catalogue per language (app/locales/<lang>.json).

- Fixed text of a template: found at compile time (Jinja extension below) and passed through `_t`.
- Text worded by the code (status, log line, error message): built with M("{ref} archived.", ref=ref), a Msg that
  keeps its template and its values. It is stored and sent as a fact (pack / revive) and worded when displayed (`_tv`).
- A plain sentence (one that lost its values on the way) is still matched against the templates of the catalogue.
- A text the catalogue does not know is shown as it is: nothing breaks, and a test lists what is missing.
"""
import contextvars, datetime as dt, json, os, re, time
from jinja2.ext import Extension
from jinja2.lexer import Token
from markupsafe import Markup, escape

DEFAULT = "en"          # source language: texts are written in English in the templates and the code
COOKIE = "fabrique_lang"
HERE = os.path.dirname(__file__)


def _discover():
    """Languages on offer: one catalogue file per language in app/locales, named by its `meta.name` (the language's own
    name) and `meta.english_name`. Adding a language is adding its catalogue (and runner/locales/<code>.json for what
    the runner writes on the Git host)."""
    names, english = {}, {}
    folder = os.path.join(HERE, "locales")
    for f in sorted(os.listdir(folder)):
        if not f.endswith(".json"):
            continue
        code = f[:-5]
        try:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                meta = json.load(fh).get("meta") or {}
        except (OSError, ValueError):
            continue
        names[code], english[code] = str(meta.get("name") or code), str(meta.get("english_name") or code)
    return dict(sorted(names.items(), key=lambda kv: kv[1].lower())), english


LANGS, ENGLISH_NAMES = _discover()
_lang = contextvars.ContextVar("fabrique_lang", default=DEFAULT)
_choice = contextvars.ContextVar("fabrique_lang_choice", default="")    # what the person explicitly chose ("" : follow the browser)
_cats = {}
_SPACES = re.compile(r"\s+")
_HOLE = re.compile(r"\{\{|\}\}|\{(\w+)\}")
MIN_LITERAL = 5          # a wildcard pattern must contain at least as many fixed letters, so as not to catch any text
MAX_PATTERN_TEXT = 1200  # beyond that, we do not search for a pattern (free text: report, diff, raw log)

MARK = "\x1e"            # first character of a text stored as a fact: MARK + JSON {"m": template, "p": values}
CONCAT = "\x1e+"         # template of two texts put end to end


class Msg(str):
    """A text Fabrique words itself: the English sentence, plus its template and values, so that it can be worded
    again in the reader's language, and stored or sent as a fact rather than as a sentence."""
    __slots__ = ("tid", "params")

    def __new__(cls, template, params=None):
        params = dict(params or {})
        try:
            if template == CONCAT:
                text = str(params.get("a", "")) + str(params.get("b", ""))
            else:
                text = template.format(**params) if params else template
        except (KeyError, IndexError, ValueError):
            text = template
        self = super().__new__(cls, text)
        self.tid, self.params = template, params
        return self

    def __add__(self, other):
        return Msg(CONCAT, {"a": self, "b": other}) if isinstance(other, str) else NotImplemented

    def __radd__(self, other):
        return Msg(CONCAT, {"a": other, "b": self}) if isinstance(other, str) else NotImplemented

    def __reduce__(self):
        return (Msg, (self.tid, self.params))


def M(template, **params):
    """A text worded by the code. `_ctx` names the context when the same English text needs two translations:
    M("done", _ctx="job") looks for "job::done" in the catalogue before "done"."""
    return Msg(template, params)


def _plain(v):
    if isinstance(v, Msg):
        return pack(v)
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def pack(text):
    """A Msg as a string that keeps its template and values (for the database and for the runner's messages)."""
    if not isinstance(text, Msg):
        return text
    return MARK + json.dumps({"m": text.tid, "p": {k: _plain(v) for k, v in text.params.items()}}, ensure_ascii=False)


def revive(text):
    """The Msg behind a packed string; any other value is returned as it is."""
    if not (isinstance(text, str) and text[:1] == MARK and not isinstance(text, Msg)):
        return text
    try:
        raw = json.loads(text[1:])
        return Msg(raw["m"], {k: revive(v) for k, v in (raw.get("p") or {}).items()})
    except (ValueError, KeyError, TypeError, AttributeError):
        return text[1:]


def pack_tree(o):
    if isinstance(o, Msg):
        return pack(o)
    if isinstance(o, dict):
        return {k: pack_tree(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [pack_tree(v) for v in o]
    return o


def revive_tree(o):
    if isinstance(o, str):
        return revive(o)
    if isinstance(o, dict):
        return {k: revive_tree(v) for k, v in o.items()}
    if isinstance(o, list):
        return [revive_tree(v) for v in o]
    return o


def _form(value, n=None):
    """A catalogue value is a sentence, or its forms by number: {"one": …, "other": …}."""
    if isinstance(value, dict):
        try:
            one = n is not None and abs(float(str(n).replace(",", "."))) < 2
        except ValueError:
            one = False
        return value.get("one" if one else "other") or next(iter(value.values()))
    return value


def norm(text):
    return _SPACES.sub(" ", text).strip()


class Catalogue:
    def __init__(self, lang):
        path = os.path.join(HERE, "locales", f"{lang}.json")
        raw = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        self.meta = raw.get("meta", {})
        self.exact, self.by_word, self.loose, self.cache, self.vague = {}, {}, [], {}, []
        for src, dst in raw.get("messages", {}).items():
            self.exact[src] = dst
            if any(m.group(1) for m in _HOLE.finditer(src)) and "::" not in src:
                self._pattern(src, dst)
        for group in list(self.by_word.values()) + [self.loose]:      # the most precise pattern first
            group.sort(key=lambda e: -len(e[0].pattern))

    def _pattern(self, src, dst):
        literals, holes, pos = [""], [], 0                # literal, hole name, literal, hole name, …
        for m in _HOLE.finditer(src):
            literals[-1] += src[pos:m.start()]
            if m.group(1):
                holes.append(m.group(1)); literals.append("")
            else:
                literals[-1] += m.group(0)[0]
            pos = m.end()
        literals[-1] += src[pos:]
        if sum(ch.isalpha() for lit in literals for ch in lit) < MIN_LITERAL:
            self.vague.append(src)                 # too little fixed text: would catch anything, we are discarding it
            return
        rx = "".join(re.escape(lit) + ("(.*?)" if i < len(holes) else "") for i, lit in enumerate(literals))
        entry = (re.compile(rx, re.S), holes, dst)
        head = literals[0].split(" ")[0] if literals[0].strip() else ""
        (self.by_word.setdefault(head, []) if head else self.loose).append(entry)

    def lookup(self, text):
        hit = self.exact.get(text)
        if hit is not None:
            return _form(hit)
        if text in self.cache:
            return self.cache[text]
        out = None
        if len(text) <= MAX_PATTERN_TEXT:
            for rx, holes, dst in list(self.by_word.get(text.split(" ")[0], ())) + self.loose:
                m = rx.fullmatch(text)
                if m:
                    args = {}
                    for n, g in zip(holes, m.groups()):
                        args[n] = _form(self.exact.get(g, g))    # a hole may carry a label the catalogue translates too
                    try:
                        out = _form(dst, args.get("n")).format(**args)
                    except (IndexError, KeyError, ValueError):
                        out = None
                    break
        if len(self.cache) > 4000:
            self.cache.clear()
        self.cache[text] = out
        return out


def catalogue(lang):
    if lang not in _cats:
        _cats[lang] = Catalogue(lang)
    return _cats[lang]


def reload():
    _cats.clear()


def current():
    return _lang.get()


def use(lang, choice=None):
    """Sets the language of the current request. Returns the token to go back (tests)."""
    if choice is not None:
        _choice.set(choice if choice in LANGS else "")
    return _lang.set(lang if lang in LANGS else DEFAULT)


def choice():
    return _choice.get()


# Machine translations of a job's texts, for the page being rendered: {original text: translation}. Set by
# translate.attach when the reader's language is not the job's; cleared at each request.
_texts = contextvars.ContextVar("fabrique_job_texts", default=None)
_original = contextvars.ContextVar("fabrique_original", default=False)


def show_translations(mapping):
    _texts.set(dict(mapping) if mapping else None)


def original(value=None):
    """True when the reader asked to see the original texts (?orig=1)."""
    if value is not None:
        _original.set(bool(value))
        _texts.set(None)
    return _original.get()


def known(text):
    """True if Fabrique words this text itself: the catalogues already translate it for each reader."""
    return any(catalogue(lang).lookup(text) is not None or catalogue(lang).lookup(text.strip()) is not None for lang in LANGS if lang != DEFAULT)


FALLBACK = "en"           # language shown when neither the person nor the browser decides


def default_lang():
    """Interface language of last resort: English, unless FABRIQUE_LANG says otherwise."""
    env = os.environ.get("FABRIQUE_LANG", "")
    return env if env in LANGS else FALLBACK


def job_lang(c, repo=None):
    """Language of a job: what Fabrique and its agents write for people (pull request, review guide, comments,
    notifications, an agent's interpretation or report). The repo's choice if it made one, otherwise the application's.
    It never depends on who is looking."""
    from . import db
    own = ((repo or {}).get("settings") or {}).get("lang")
    if own in LANGS:
        return own
    app = (db.get_setting(c, "languages")[0] or {}).get("jobs")
    return app if app in LANGS else FALLBACK


def pick(cookie="", accept_language="", fallback=None):
    """Language of a request: the person's choice (cookie set from their account), otherwise the browser's,
    otherwise the instance's."""
    if cookie in LANGS:
        return cookie
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return fallback if fallback in LANGS else FALLBACK


def tr(text, lang=None):
    """Translates French text produced by the code. Unchanged if the language is French or if the catalog does
    not know it."""
    lang = lang or _lang.get()
    if lang == DEFAULT or not text or not isinstance(text, str) or isinstance(text, Markup):
        return text
    cat = catalogue(lang)
    if isinstance(text, Msg) and text.tid == CONCAT:
        return "".join(tr(p, lang) if isinstance(p, str) else str(p) for p in (text.params.get("a", ""), text.params.get("b", "")))
    if isinstance(text, Msg):
        # A context tells two uses of the same English word apart ("done" for a job, "done" for a study): the
        # translation filed under "job::done" wins over the one under "done".
        ctx = text.params.get("_ctx")
        key = f"{ctx}::{text.tid}" if ctx and f"{ctx}::{text.tid}" in cat.exact else text.tid
    if isinstance(text, Msg) and key in cat.exact:
        params = {k: (tr(v, lang) if isinstance(v, str) else v) for k, v in text.params.items()}
        try:
            form = _form(cat.exact[key], params.get("n", params.get("count")))
            return form.format(**params) if params else form
        except (KeyError, IndexError, ValueError):
            pass
    hit = cat.lookup(text)
    if hit is not None:
        return hit
    core = text.strip()
    if core != text and core:
        hit = cat.lookup(core)
        if hit is not None:
            i = text.index(core)
            return text[:i] + hit + text[i + len(core):]
    if "\n" in core:                                          # text of several lines: line by line
        lines = [tr(line, lang) for line in text.split("\n")]
        return "\n".join(lines)
    return text


def _t(text, template=""):
    """Fixed text of a template (trusted HTML). A translation specific to a template ("page.html::text")
    takes precedence over general translation: the same phrase fragment is not translated the same way everywhere."""
    lang = _lang.get()
    if lang == DEFAULT:
        return Markup(text)
    exact, key = catalogue(lang).exact, norm(text)
    hit = exact.get(template + "::" + key)
    if hit is None:
        hit = exact.get(key)
    return Markup(_form(hit) if hit is not None else text)


def plural(n, one, many, is_plural=None):
    """"1 job archived", "3 jobs archived" : chooses the form, translates it, then places the number ({n}) in it."""
    form = many if (n != 1 if is_plural is None else is_plural) else one
    lang = _lang.get()
    if lang != DEFAULT:
        hit = catalogue(lang).exact.get(form)
        if hit is not None:
            form = _form(hit, n if is_plural is None else (2 if is_plural else 1))
    return form.replace("{n}", str(n))


def _tv(value):
    """Value displayed by a template: translated if it is text that the catalog knows. Text written by an
    agent, if it has been translated at the reader's request, is replaced by its translation (marked as automatic)."""
    if isinstance(value, str) and not isinstance(value, Markup):
        texts = _texts.get()
        if texts and value in texts:
            return (Markup('<span class="mt" data-mt>') + texts[value] + Markup('</span><span class="mo" data-mo hidden>')
                    + value + Markup("</span>"))
        if _lang.get() != DEFAULT:
            return tr(value)
    return value


def meta(key, lang=None):
    cat = catalogue(lang or _lang.get())
    return cat.meta[key] if key in cat.meta else catalogue(DEFAULT).meta[key]


def js_catalogue(lang=None):
    """Texts that app.js needs for the current language (French: nothing to send)."""
    lang = lang or _lang.get()
    if lang == DEFAULT:
        return {}
    cat = catalogue(lang)
    return {k: _form(cat.exact.get(k, k)) for k in catalogue(DEFAULT).meta.get("js", [])}


# --- dates, durations, numbers -------------------------------------------------------------------------------------------

def month(d):
    return meta("months")[d.month - 1]


def short_date(d):
    return meta("date_short").format(d=d.day, month=month(d), y=d.year)


def long_date(d):
    return meta("date_long").format(d=d.day, month=month(d), y=d.year, hm=f"{d:%H:%M}")


def decimal(x, digits=2):
    s = f"{x:.{digits}f}"
    return s.replace(".", meta("decimal")) if meta("decimal") != "." else s


def money(x):
    return meta("money").format(n=decimal(x or 0))


def tokens(n):
    n = n or 0
    if n >= 1e6:
        return meta("millions").format(n=decimal(n / 1e6, 1))
    if n >= 1e3:
        return meta("thousands").format(n=f"{n / 1e3:.0f}")
    return str(n)


def since(ts, key="since"):
    """"since 12 min" (key=since) or "12 min ago" (key=ago)."""
    s = max(0, time.time() - (ts or 0))
    forms = meta(key)
    if s < (90 if key == "since" else 60):
        return forms["now"]
    if s < 3600:
        return forms["min"].format(n=int(s // 60))
    if s < 86400:
        return forms["h"].format(n=int(s // 3600))
    return forms["d"].format(n=int(s // 86400))


# --- Jinja extension: identifies fixed text and displayed values ------------------------------------------------

INLINE = {"b", "i", "em", "strong", "code", "kbd", "span", "br", "u", "small", "sup", "sub", "a", "abbr", "wbr"}
VOID = {"br", "wbr"}
TEXT_ATTRS = {"title", "placeholder", "label", "data-ph-change", "data-ph-study", "label", "data-ph-change", "data-ph-study", "aria-label", "alt", "data-confirm", "data-tip", "data-label", "data-empty", "aria-description"}
RAW = {"script", "style"}
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.S)
_ATTR = re.compile(r"([\w:-]+)\s*=\s*(\"([^\"]*)\"|'([^']*)')", re.S)
_OPEN_ATTR = re.compile(r"([\w:-]+)\s*=\s*([\"'])([^\"']*)$", re.S)
_HAS_LETTER = re.compile(r"[A-Za-zÀ-ÿ]{2}")


def _worth(text):
    return bool(_HAS_LETTER.search(re.sub(r"&\w+;|<[^>]*>", " ", text)))


class Units:
    """Splits the raw text of a template (between two Jinja constructions) into chunks: ('data', s) copied as
    is, ('text', s) to be translated. Maintains the state of a chunk for the next one: inside a tag, in an attribute value."""

    def __init__(self):
        self.in_tag = False       # we are between < and >
        self.quote = None         # open quote of an attribute value
        self.attr = None          # attribute whose value is open
        self.raw = None           # in <script> or <style>

    def context(self):
        """Where a {{ ... }} expression placed here falls: 'text', 'attr' (text attribute) or None (do not translate)."""
        if self.raw:
            return None
        if not self.in_tag:
            return "text"
        return "attr" if self.quote and self.attr in TEXT_ATTRS else None

    def split(self, data):
        out, pos = [], 0
        while pos < len(data):
            if self.raw:
                end = data.lower().find("</" + self.raw, pos)
                if end < 0:
                    out.append(("data", data[pos:]))
                    return out
                out.append(("data", data[pos:end]))
                pos, self.raw = end, None
                continue
            if self.in_tag:
                pos = self._tag_tail(data, pos, out)
                continue
            lt = data.find("<", pos)
            if lt < 0:
                self._texts(data[pos:], out)
                return out
            if data.startswith("<!", lt) and not data.startswith("<!--", lt):      # <!doctype …>
                end = data.find(">", lt)
                end = len(data) if end < 0 else end + 1
                self._texts(data[pos:lt], out)
                out.append(("data", data[lt:end]))
                pos = end
                continue
            if data.startswith("<!--", lt):
                end = data.find("-->", lt)
                end = len(data) if end < 0 else end + 3
                self._texts(data[pos:lt], out)
                out.append(("data", data[lt:end]))
                pos = end
                continue
            m = _TAG.match(data, lt)
            if m:
                self._texts(data[pos:m.end()], out)      # the complete tag is treated with the text (inline tags)
                pos = m.end()
                if not m.group(1) and m.group(2).lower() in RAW:
                    self.raw = m.group(2).lower()
                continue
            self._texts(data[pos:lt], out)
            if lt + 1 < len(data) and (data[lt + 1].isalpha() or data[lt + 1] == "/"):
                self.in_tag, self.quote, self.attr = True, None, None     # tag cut by an expression
                pos = self._tag_tail(data, lt, out)
            else:
                out.append(("data", "<"))
                pos = lt + 1
        return out

    def _tag_tail(self, data, pos, out):
        """In a cut tag: advance to its end, or to the end of the chunk."""
        i, start = pos, pos
        while i < len(data):
            ch = data[i]
            if self.quote:
                if ch == self.quote:
                    value = data[start:i]
                    if self.attr in TEXT_ATTRS and _worth(value):
                        lead = len(value) - len(value.lstrip())
                        trail = len(value.rstrip())
                        out += [("data", value[:lead]), ("text", value[lead:trail]), ("data", value[trail:])]
                    else:
                        out.append(("data", value))
                    start, self.quote, self.attr = i, None, None
                i += 1
                continue
            if ch in "\"'":
                m = _OPEN_ATTR.search(data[start:i + 1])
                self.attr = m.group(1).lower() if m else None
                self.quote = ch
                out.append(("data", data[start:i + 1]))
                start = i + 1
            elif ch == ">":
                out.append(("data", data[start:i + 1]))
                self.in_tag = False
                return i + 1
            i += 1
        rest = data[start:]
        if self.quote and self.attr in TEXT_ATTRS and _worth(rest):
            lead = len(rest) - len(rest.lstrip())
            trail = len(rest.rstrip())
            out += [("data", rest[:lead]), ("text", rest[lead:trail]), ("data", rest[trail:])]
        else:
            out.append(("data", rest))
        return len(data)

    def _texts(self, chunk, out):
        """Text outside a cut tag: complete texts and tags. Inline tags (<b>, <code>...) remain
        within the sentence; others break it, and their text attributes are translated separately."""
        pieces, pos = [], 0
        for m in _TAG.finditer(chunk):
            if m.start() > pos:
                pieces.append(("t", chunk[pos:m.start()]))
            name = m.group(2).lower()
            pieces.append(("i" if name in INLINE else "b", m.group(0)))
            pos = m.end()
        if pos < len(chunk):
            pieces.append(("t", chunk[pos:]))
        run = []
        for kind, s in pieces + [("b", "")]:
            if kind != "b":
                run.append((kind, s))
                continue
            self._run(run, out)
            run = []
            if s:
                self._attrs(s, out)

    def _run(self, run, out):
        while run and not (run[0][0] == "t" and _worth(run[0][1])):       # what precedes the first real text
            self._plain(run.pop(0), out)
        tail = []
        while run and not (run[-1][0] == "t" and _worth(run[-1][1])):
            tail.insert(0, run.pop())
        if run:
            if len(run) > 1 and not self._balanced(run):                    # unpaired inline tags: text by text
                for kind, s in run:
                    if kind == "t" and _worth(s):
                        self._one(s, out)
                    else:
                        self._plain((kind, s), out)
            else:
                self._one("".join(s for _, s in run), out)
        for piece in tail:
            self._plain(piece, out)

    def _plain(self, piece, out):
        kind, s = piece
        if kind == "i":
            self._attrs(s, out)
        else:
            out.append(("data", s))

    @staticmethod
    def _balanced(run):
        stack = []
        for kind, s in run:
            if kind != "i":
                continue
            m = _TAG.match(s)
            name = m.group(2).lower()
            if name in VOID or s.endswith("/>"):
                continue
            if m.group(1):
                if not stack or stack.pop() != name:
                    return False
            else:
                stack.append(name)
        return not stack

    @staticmethod
    def _one(s, out):
        lead = len(s) - len(s.lstrip())
        trail = len(s.rstrip())
        out += [("data", s[:lead]), ("text", s[lead:trail]), ("data", s[trail:])]

    @staticmethod
    def _attrs(tag, out):
        pos = 0
        for m in _ATTR.finditer(tag):
            value = m.group(3) if m.group(3) is not None else m.group(4)
            if m.group(1).lower() in TEXT_ATTRS and value and _worth(value):
                start = m.start(2) + 1
                out += [("data", tag[pos:start]), ("text", value)]
                pos = start + len(value)
        out.append(("data", tag[pos:]))


_SLOT = re.compile("\ue000(\\d+)\ue001")       # place of a {{ value }} kept inside its sentence while the template is cut into units
_NAMED = re.compile(r"\{(\w+)\}")


def _tf(text, template, *values):
    """Fixed text of a template with its values inside the sentence: "Follow-up to the study {ref}". The whole sentence is
    one unit of translation, so a language can move the values around. Values are escaped and worded like any other."""
    out = text
    if _lang.get() != DEFAULT:
        exact, key = catalogue(_lang.get()).exact, norm(text)
        hit = exact.get(template + "::" + key)
        if hit is None:
            hit = exact.get(key)
        if hit is not None:
            out = _form(hit)
    given = {}
    for name, value in zip(_NAMED.findall(text), values):
        given.setdefault(name, str(escape(_tv(value))))
    return Markup(_NAMED.sub(lambda m: given.get(m.group(1), m.group(0)), out))


def _slot_name(tokens, taken):
    """Name of a value in its sentence, from its expression: `pre.ref` gives ref, `a.at|when` gives at; else p, p2…"""
    name = "p"
    for t in tokens:
        if t.type == "pipe":
            break
        if t.type == "name":
            name = t.value
    name = re.sub(r"\W", "", name) or "p"
    base, i = name, 2
    while name in taken:
        name, i = f"{base}{i}", i + 1
    taken.add(name)
    return name


class Texts(Extension):
    """Passes through `_t` every fixed text of a template, and through `_tv` every value displayed in text or in
    a text attribute. A simple value in the middle of a sentence stays inside it (`_tf`): the sentence is translated
    whole. Other values (addresses, classes, form values) are never touched."""

    def filter_stream(self, stream):
        name = stream.name or ""
        tokens = list(stream)
        # First pass: where does each {{ value }} fall (text, text attribute, elsewhere)?
        probe, where, i = Units(), {}, 0
        while i < len(tokens):
            t = tokens[i]
            if t.type == "data":
                probe.split(t.value)
            elif t.type == "variable_begin":
                where[i] = probe.context()
            i += 1
        # Second pass: data and simple values are gathered, cut into units, then written out.
        units, buf, slots, line = Units(), [], [], 1

        def var(expr, ctx, lineno):
            yield Token(lineno, "variable_begin", "{{")
            if ctx is not None:
                yield Token(lineno, "lparen", "(")
            yield from expr
            if ctx is not None:
                yield Token(lineno, "rparen", ")")
                yield Token(lineno, "pipe", "|")
                yield Token(lineno, "name", "_tv")
            yield Token(lineno, "variable_end", "}}")

        def call(fn, text, exprs, lineno):
            yield Token(lineno, "variable_begin", "{{")
            yield Token(lineno, "name", fn)
            yield Token(lineno, "lparen", "(")
            yield Token(lineno, "string", text)
            yield Token(lineno, "comma", ",")
            yield Token(lineno, "string", name)
            for e in exprs:
                yield Token(lineno, "comma", ",")
                yield Token(lineno, "lparen", "(")
                yield from e
                yield Token(lineno, "rparen", ")")
            yield Token(lineno, "rparen", ")")
            yield Token(lineno, "variable_end", "}}")

        def loose(piece, lineno):
            """A piece with values but no sentence to keep them in: texts and values one by one, as before."""
            pos = 0
            for m in _SLOT.finditer(piece):
                yield from plain(piece[pos:m.start()], lineno)
                expr, ctx = slots[int(m.group(1))]
                yield from var(expr, ctx, lineno)
                pos = m.end()
            yield from plain(piece[pos:], lineno)

        def plain(s, lineno):
            if not s:
                return
            if _worth(s):
                lead, trail = len(s) - len(s.lstrip()), len(s.rstrip())
                if s[:lead]:
                    yield Token(lineno, "data", s[:lead])
                yield from call("_t", s[lead:trail], [], lineno)
                if s[trail:]:
                    yield Token(lineno, "data", s[trail:])
            else:
                yield Token(lineno, "data", s)

        def flush(lineno):
            if not buf:
                return
            data = "".join(buf)
            del buf[:]
            for kind, s in _merge(units.split(data)):
                found = _SLOT.findall(s)
                if kind == "data":
                    if not found:
                        if s:
                            yield Token(lineno, "data", s)
                        continue
                    pos = 0
                    for m in _SLOT.finditer(s):
                        if s[pos:m.start()]:
                            yield Token(lineno, "data", s[pos:m.start()])
                        expr, ctx = slots[int(m.group(1))]
                        yield from var(expr, ctx, lineno)
                        pos = m.end()
                    if s[pos:]:
                        yield Token(lineno, "data", s[pos:])
                elif not found:
                    yield from call("_t", s, [], lineno)
                elif "{" in s or "}" in s:
                    yield from loose(s, lineno)           # literal braces in the text: no named values
                else:
                    taken, exprs = set(), []

                    def named(m):
                        expr, _ = slots[int(m.group(1))]
                        exprs.append(expr)
                        return "{" + _slot_name(expr, taken) + "}"
                    yield from call("_tf", _SLOT.sub(named, s), exprs, lineno)

        i = 0
        while i < len(tokens):
            t = tokens[i]
            line = t.lineno
            if t.type == "data":
                buf.append(t.value)
                i += 1
            elif t.type == "variable_begin":
                j = i + 1
                while tokens[j].type != "variable_end":
                    j += 1
                expr, ctx = tokens[i + 1:j], where.get(i)
                if ctx is not None and expr and not any(x.type in ("lparen", "lbracket", "lbrace") for x in expr):
                    buf.append(f"\ue000{len(slots)}\ue001")           # a simple value: it stays in its sentence
                    slots.append((expr, ctx))
                else:
                    yield from flush(line)
                    yield from var(expr, ctx, line)
                i = j + 1
            else:
                yield from flush(line)
                yield t
                i += 1
        yield from flush(line)


def _merge(pieces):
    out = []
    for kind, s in pieces:
        if out and kind == "data" and out[-1][0] == "data":
            out[-1] = ("data", out[-1][1] + s)
        else:
            out.append((kind, s))
    return out


def install(env):
    env.add_extension(Texts)
    env.globals["_t"] = _t
    env.globals["_tf"] = _tf
    env.globals["nb"] = plural
    env.filters["_tv"] = _tv
    env.globals.update(LANG=current, LANG_CHOICE=choice, LANGS=LANGS, COLON=lambda: meta("colon"), I18N_JS=lambda: Markup(json.dumps(js_catalogue(), ensure_ascii=False).replace("</", "<\\/")))


def template_texts(env, names=None):
    """All fixed texts of templates (normalized) and strings written in their expressions: what the language catalog
    must cover. The texts are read from what the engine really emits (`_t` and `_tf` calls), so a sentence that keeps
    its values is listed whole. Used for extraction and completeness testing."""
    fixed, strings = {}, {}
    for name in names or env.list_templates():
        if not name.endswith(".html"):
            continue
        source = env.loader.get_source(env, name)[0]
        for _, kind, value in env.lexer.tokeniter(source, name):
            if kind == "string":
                s = value[1:-1]
                if _worth(s) and re.search(r"[À-ÿ ]", s):
                    strings.setdefault(s.replace("\\'", "'").replace('\\"', '"'), name)
        toks = list(Texts(env).filter_stream(env.lexer.tokenize(source, name)))      # the units the engine emits
        for fn, par, text in zip(toks, toks[1:], toks[2:]):
            if fn.type == "name" and fn.value in ("_t", "_tf") and par.type == "lparen" and text.type == "string":
                fixed.setdefault(norm(text.value), name)
    return fixed, strings
