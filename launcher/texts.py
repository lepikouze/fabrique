"""Texts the launcher words itself (see runner/texts.py): the sentence with its template and values."""
import json

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
