"""Rapport d'étude en PDF : un petit convertisseur Markdown vers reportlab (BSD), sans programme externe.
Pris en charge : titres, paragraphes, gras, italique, code en ligne, liens, listes (à puces, numérotées, imbriquées),
citations, blocs de code, tableaux, filets. Le texte vient d'un agent : il est traité comme une donnée (échappé, liens
limités à http, https et mailto) ; un passage que reportlab ne sait pas composer est rendu en texte brut, jamais en erreur.
Police embarquée : DejaVu (app/static/fonts), pour les accents, les flèches et les symboles courants."""
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
MAX_CHARS = 200_000          # au-delà, le rapport est coupé dans le PDF (un rapport d'étude fait quelques pages)
CODE_COLS = 88               # largeur d'un bloc de code, en caractères : les lignes plus longues sont repliées
HEADING_MAX = 300            # caractères d'un titre
CELL_MAX = 1500              # caractères d'une cellule de tableau (une ligne de tableau ne se coupe pas entre deux pages)

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


# Caractères hors de la police : équivalent proche, sinon un carré (jamais un trou ni une erreur).
SWAP = {"✅": "✓", "✔": "✓", "☑": "✓", "❌": "✗", "✖": "✗", "❎": "✗", "❓": "?", "❗": "!", "➜": "→", "➡": "→", "⬅": "←", "⬆": "↑",
        "⬇": "↓", "🔴": "●", "🟢": "●", "🟡": "●", "🟠": "●", "🔵": "●", "⭐": "★", "\t": "    ",
        # invisibles : trait d'union conditionnel, marque d'ordre des octets, espace sans chasse, liant, sélecteur de variante
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
    """Texte d'une ligne en balisage reportlab : tout est d'abord échappé, puis seuls nos repères deviennent des balises."""
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
    """Découpe le Markdown en blocs : ("h", niveau, texte), ("p", texte), ("li", niveau, puce, texte), ("code", texte),
    ("table", lignes), ("quote", texte), ("hr",)."""
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
    """Paragraphe ; si le balisage produit ne se compose pas (repères Markdown mal fermés), le texte est rendu tel quel."""
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
    # Chaque colonne reçoit d'abord la place de son mot le plus long (aucun mot coupé tant que la page le permet), puis le
    # reste est réparti selon la longueur du contenu.
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
        if kind == "h":   # titre borné : il reste solidaire du bloc suivant, donc il ne doit jamais dépasser une page
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
    """Pied de page « libellé · page n / total » : le total n'est connu qu'à la fin, donc les pages sont gardées puis écrites."""
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
    """Le PDF (octets) d'un rapport : en-tête sobre (titre, ligne de contexte), corps en Markdown, pages numérotées."""
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
        # Mise en page impossible (un bloc qui ne tient sur aucune page, par exemple) : le rapport sort quand même, en
        # texte brut, plutôt qu'une erreur. Le Markdown reste téléchargeable à côté.
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
                                title=clean(title)[:200], author="Fabrique")
        plain = [Paragraph(escape(clean(title))[:HEADING_MAX], st["title"]), Paragraph(escape(clean(meta)), st["meta"])]
        plain += [XPreformatted(escape(_wrap_code(chunk)) or " ", ParagraphStyle("plain", parent=st["code"], backColor=None))
                  for chunk in clean(markdown)[:MAX_CHARS].split("\n\n")]
        doc.build(plain, canvasmaker=maker)
    return buf.getvalue()
