"""PDF study report: a small Markdown to reportlab (BSD) converter, without external programs.
Supported: titles, paragraphs, bold, italic, inline code, links, lists (bulleted, numbered, nested),
quotes, code blocks, tables, horizontal rules. The text comes from an agent: it is treated as data (escaped, links
limited to http, https and mailto); a passage that reportlab cannot compose is rendered as plain text, never as an error.
Embedded font: DejaVu (app/static/fonts), for accents, arrows and common symbols."""
import io, os, re
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, XPreformatted

FONTS = os.path.join(os.path.dirname(__file__), "static", "fonts")
SANS, BOLD, ITALIC, MONO = "FabSans", "FabSans-Bold", "FabSans-Oblique", "FabMono"
INK, MUTED, RULE, CODE_BG, LINK = colors.HexColor("#1a1a1a"), colors.HexColor("#5b5b63"), colors.HexColor("#d9d8d4"), colors.HexColor("#f3f2ef"), colors.HexColor("#1d5fd1")
MAX_CHARS = 200_000          # beyond this, the report is cut in the PDF (a study report is a few pages)
CODE_COLS = 88               # code block width, in characters: longer lines are wrapped
HEADING_MAX = 300            # characters in a title
CELL_MAX = 1500              # characters in a table cell (a table row does not break between two pages)

_ready = False
_glyphs = set()


def _fonts():
    global _ready
    if _ready:
        return
    for name, file in ((SANS, "dejavu-sans.ttf"), (BOLD, "dejavu-sans-bold.ttf"), (ITALIC, "dejavu-sans-oblique.ttf"), (MONO, "dejavu-sans-mono.ttf")):
        pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, file)))
    pdfmetrics.registerFontFamily(SANS, normal=SANS, bold=BOLD, italic=ITALIC, boldItalic=BOLD)
    pdfmetrics.registerFontFamily(MONO, normal=MONO, bold=MONO, italic=MONO, boldItalic=MONO)
    _glyphs.update(pdfmetrics.getFont(SANS).face.charToGlyph.keys())
    _ready = True


# Characters outside the font: close equivalent, otherwise a square (never a hole or an error).
SWAP = {"✅": "✓", "✔": "✓", "☑": "✓", "❌": "✗", "✖": "✗", "❎": "✗", "❓": "?", "❗": "!", "➜": "→", "➡": "→", "⬅": "←", "⬆": "↑",
        "⬇": "↓", "🔴": "●", "🟢": "●", "🟡": "●", "🟠": "●", "🔵": "●", "⭐": "★", "\t": "    ",
        # invisibles: soft hyphen, byte order mark, zero-width space, zero-width joiner, variation selector
        "\u00ad": "", "\ufeff": "", "\u200b": "", "\u200d": "", "\ufe0f": ""}


def clean(text):
    _fonts()
    out = []
    for ch in str(text or ""):
        ch = SWAP.get(ch, ch)
        for x in ch:
            out.append(x if x == "\n" or ord(x) in _glyphs else "□" if ord(x) >= 32 else "")
    return "".join(out)


_CODE = re.compile(r"(`[^`\n]+`)")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def inline(text):
    """One-line text in reportlab markup: everything is first escaped, then only our markers become tags."""
    out = []
    for part in _CODE.split(text):
        if len(part) > 2 and part.startswith("`") and part.endswith("`"):
            out.append(f'<font face="{MONO}" size="8.6" backColor="#f3f2ef">{escape(part[1:-1])}</font>')
            continue
        s = escape(part)

        def link(m):
            label, url = m.group(1), m.group(2)
            if not url.startswith(("https://", "http://", "mailto:")):
                return f"{label} ({url})"
            return f'<a href="{url.replace(chr(34), "%22")}" color="#1d5fd1">{label}</a>'
        s = _LINK.sub(link, s)
        s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\*\w])\*(?=[^\s\*])(.+?)(?<=[^\s\*])\*(?![\*\w])", r"<i>\1</i>", s)
        s = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<i>\1</i>", s)
        out.append(s)
    return "".join(out)


_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _cells(line):
    line = line.strip()
    line = line[1:] if line.startswith("|") else line
    line = line[:-1] if line.endswith("|") and not line.endswith("\\|") else line
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def blocks(md):
    """Splits Markdown into blocks: ("h", level, text), ("p", text), ("li", level, bullet, text), ("code", text),
    ("table", lines), ("quote", text), ("hr",)."""
    lines = clean(md)[:MAX_CHARS].replace("\r\n", "\n").split("\n")
    out, para, i = [], [], 0

    def flush():
        if para:
            out.append(("p", " ".join(x.strip() for x in para)))
            para.clear()
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            flush()
            mark, body = _FENCE.match(line).group(1), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(mark):
                body.append(lines[i]); i += 1
            out.append(("code", "\n".join(body)))
            i += 1
            continue
        if not line.strip():
            flush(); i += 1
            continue
        h = _HEADING.match(line)
        if h:
            flush(); out.append(("h", len(h.group(1)), h.group(2))); i += 1
            continue
        if "|" in line and i + 1 < len(lines) and _SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            rows = [_cells(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i])); i += 1
            out.append(("table", rows))
            continue
        if _RULE.match(line) and not para:
            out.append(("hr",)); i += 1
            continue
        m = _ITEM.match(line)
        if m:
            flush()
            level, mark, text = min(len(m.group(1).replace("\t", "    ")) // 2, 3), m.group(2), [m.group(3)]
            i += 1
            while i < len(lines) and lines[i].strip() and lines[i].startswith((" ", "\t")) and not _ITEM.match(lines[i]) and not _FENCE.match(lines[i]):
                text.append(lines[i].strip()); i += 1
            out.append(("li", level, mark if mark[0].isdigit() else "•", " ".join(text)))
            continue
        if line.lstrip().startswith(">"):
            flush()
            q = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                q.append(lines[i].lstrip()[1:].strip()); i += 1
            out.append(("quote", " ".join(q)))
            continue
        para.append(line); i += 1
    flush()
    return out


def _styles():
    base = dict(fontName=SANS, textColor=INK, alignment=TA_LEFT, wordWrap="LTR")
    body = ParagraphStyle("body", fontSize=10, leading=14.5, spaceAfter=7, **base)
    return {
        "body": body,
        "title": ParagraphStyle("title", fontName=BOLD, fontSize=19, leading=24, textColor=INK, spaceAfter=4),
        "meta": ParagraphStyle("meta", fontName=SANS, fontSize=9, leading=12, textColor=MUTED, spaceAfter=8),
        1: ParagraphStyle("h1", parent=body, fontName=BOLD, fontSize=15, leading=19, spaceBefore=12, spaceAfter=6, keepWithNext=1),
        2: ParagraphStyle("h2", parent=body, fontName=BOLD, fontSize=12.5, leading=16, spaceBefore=11, spaceAfter=5, keepWithNext=1),
        3: ParagraphStyle("h3", parent=body, fontName=BOLD, fontSize=11, leading=15, spaceBefore=9, spaceAfter=4, keepWithNext=1),
        4: ParagraphStyle("h4", parent=body, fontName=BOLD, fontSize=10, leading=14, spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "code": ParagraphStyle("code", fontName=MONO, fontSize=8.2, leading=11, textColor=INK, backColor=CODE_BG, borderPadding=(6, 7, 6, 7),
                               leftIndent=7, rightIndent=7, spaceBefore=6, spaceAfter=12),
        "quote": ParagraphStyle("quote", parent=body, textColor=MUTED, leftIndent=12, borderPadding=(0, 0, 0, 8)),
        "cell": ParagraphStyle("cell", parent=body, fontSize=8.8, leading=12, spaceAfter=0),
        "head": ParagraphStyle("head", parent=body, fontName=BOLD, fontSize=8.8, leading=12, spaceAfter=0),
    }


def _para(text, style, **kw):
    """Paragraph; if the resulting markup cannot be laid out (badly closed Markdown markers), the text is rendered as is."""
    try:
        return Paragraph(inline(text), style, **kw)
    except Exception:
        return Paragraph(escape(text), style, **kw)


def _wrap_code(text):
    out = []
    for line in text.split("\n"):
        while len(line) > CODE_COLS:
            out.append(line[:CODE_COLS]); line = "  " + line[CODE_COLS:]
        out.append(line)
    return "\n".join(out)


def _table(rows, st, width):
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    # Each column first receives the space of its longest word (no word is cut as long as the page allows), then the
    # remainder is distributed according to the content length.
    word = lambda c: max((pdfmetrics.stringWidth(w, BOLD, 8.8) for r in rows for w in re.sub(r"[*_`]", "", r[c]).split()), default=0)
    need = [min(word(c) + 12, width * 0.45) for c in range(n)]
    if sum(need) > width:
        need = [x * width / sum(need) for x in need]
    weight = [min(max(max(len(r[c]) for r in rows), 4), 60) for c in range(n)]
    spare = width - sum(need)
    widths = [need[c] + spare * weight[c] / sum(weight) for c in range(n)]
    data = [[_para(cell[:CELL_MAX], st["head"] if ri == 0 else st["cell"]) for cell in r] for ri, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, RULE), ("BACKGROUND", (0, 0), (-1, 0), CODE_BG), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def flowables(md, st, width):
    out = []
    for b in blocks(md):
        kind = b[0]
        if kind == "h":   # bounded title: it remains attached to the following block, so it must never exceed one page
            out.append(_para(b[2][:HEADING_MAX], st[min(b[1], 4)]))
        elif kind == "p":
            out.append(_para(b[1], st["body"]))
        elif kind == "li":
            indent = 14 + 14 * b[1]
            s = ParagraphStyle("li", parent=st["body"], leftIndent=indent, bulletIndent=indent - 12, spaceAfter=3, bulletFontName=SANS, bulletFontSize=10)
            out.append(_para(b[3], s, bulletText=b[2]))
        elif kind == "code":
            out.append(XPreformatted(escape(_wrap_code(b[1])) or " ", st["code"]))
        elif kind == "table":
            out += [Spacer(1, 3), _table(b[1], st, width), Spacer(1, 9)]
        elif kind == "quote":
            out.append(_para(b[1], st["quote"]))
        elif kind == "hr":
            out.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=6, spaceAfter=8))
    return out


class _Numbered(canvas.Canvas):
    """Footer "label · page n / total" : the total is only known at the end, so pages are kept and then written."""
    footer = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(SANS, 8)
            self.setFillColor(MUTED)
            self.drawString(20 * mm, 12 * mm, self.footer[:110])
            self.drawRightString(A4[0] - 20 * mm, 12 * mm, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


def report(title, meta, markdown, footer="Fabrique"):
    """The PDF (bytes) of a report: plain header (title, context line), Markdown body, numbered pages."""
    _fonts()
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
                            title=clean(title)[:200], author="Fabrique", subject=clean(meta)[:200])
    story = [Paragraph(escape(clean(title)[:HEADING_MAX]), st["title"]), Paragraph(escape(clean(meta)), st["meta"]),
             HRFlowable(width="100%", thickness=0.8, color=RULE, spaceBefore=2, spaceAfter=10)]
    head = list(story)
    maker = type("Numbered", (_Numbered,), {"footer": clean(footer)})
    try:
        doc.build(head + flowables(markdown, st, doc.width), canvasmaker=maker)
    except Exception:
        # Impossible layout (a block that fits on no page, for example): the report is still output, in
        # plain text, rather than an error. The Markdown remains downloadable alongside.
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
                                title=clean(title)[:200], author="Fabrique")
        plain = [Paragraph(escape(clean(title))[:HEADING_MAX], st["title"]), Paragraph(escape(clean(meta)), st["meta"])]
        plain += [XPreformatted(escape(_wrap_code(chunk)) or " ", ParagraphStyle("plain", parent=st["code"], backColor=None))
                  for chunk in clean(markdown)[:MAX_CHARS].split("\n\n")]
        doc.build(plain, canvasmaker=maker)
    return buf.getvalue()
