"""A small, dependency-free PDF 1.7 writer for tabular engineering reports.

Supports the 14 standard fonts (no embedding: Helvetica for headings,
Courier for tables, so column widths are exact), WinAnsi text with
transliteration of characters outside it, filled and stroked rectangles,
lines and polylines (vector plots), automatic pagination with running
headers and "page n of N" footers, compressed content streams, document
metadata, and custom metadata keys (used to embed the signed report payload).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4 in points
MARGIN = 48.0
COURIER_ADVANCE = 0.6  # every Courier glyph is 600/1000 em

FONTS = {"regular": "Helvetica", "bold": "Helvetica-Bold", "mono": "Courier", "mono-bold": "Courier-Bold"}
_FONT_IDS = {name: f"F{index}" for index, name in enumerate(FONTS, start=1)}

_TRANSLITERATE = {
    "⁺": "+", "⁻": "-", "₀": "0", "₁": "1", "₂": "2", "ω": "omega", "ε": "eps", "ν": "nu", "α": "alpha",
    "ρ": "rho", "μ": "mu", "Δ": "delta ", "σ": "sigma", "τ": "tau", "θ": "theta", "π": "pi", "→": "->",
    "←": "<-", "≈": "~", "≤": "<=", "≥": ">=", "✅": "[PASS]", "❌": "[FAIL]", "⚠": "[!]", "✓": "OK", "✗": "X",
    "−": "-", "√": "sqrt", "∂": "d", "∇": "grad", "·": "\xb7",
}


def pdf_text(value: str) -> bytes:
    text = "".join(_TRANSLITERATE.get(ch, ch) for ch in str(value))
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)").replace(b"\r", b"").replace(b"\n", b" ")


def _rgb(color: str) -> str:
    color = color.lstrip("#")
    return " ".join(f"{int(color[i:i + 2], 16) / 255:.3f}" for i in (0, 2, 4))


@dataclass
class Page:
    ops: list[str] = field(default_factory=list)


class PDFDocument:
    """Cursor-based layout: callers add blocks; pages break automatically"""

    def __init__(self, title: str, *, header: str = "", author: str = "Paramfabric", created: datetime | None = None) -> None:
        self.title = title
        self.created = created  # fixed timestamp -> byte-identical output for the same content
        self.header = header
        self.author = author
        self.pages: list[Page] = []
        self.metadata: dict[str, str] = {}
        self.y = 0.0
        self.new_page()

    # primitives
    @property
    def page(self) -> Page:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append(Page())
        self.y = PAGE_HEIGHT - MARGIN - 18

    def ensure(self, height: float) -> None:
        if self.y - height < MARGIN + 24:
            self.new_page()

    def text(self, x: float, y: float, value: str, size: float = 9, font: str = "regular", color: str = "#1d2733") -> None:
        self.page.ops.append(f"BT /{_FONT_IDS[font]} {size:.2f} Tf {_rgb(color)} rg {x:.2f} {y:.2f} Td (" + pdf_text(value).decode("latin-1") + ") Tj ET")

    def rect(self, x: float, y: float, width: float, height: float, fill: str | None = None, stroke: str | None = None, line: float = 0.5) -> None:
        ops = []
        if fill:
            ops.append(f"{_rgb(fill)} rg")
        if stroke:
            ops.append(f"{_rgb(stroke)} RG {line:.2f} w")
        paint = "B" if fill and stroke else ("f" if fill else "S")
        ops.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re {paint}")
        self.page.ops.append(" ".join(ops))

    def line(self, points: list[tuple[float, float]], color: str = "#1d2733", width: float = 0.75, dash: str = "") -> None:
        if len(points) < 2:
            return
        path = f"{points[0][0]:.2f} {points[0][1]:.2f} m " + " ".join(f"{x:.2f} {y:.2f} l" for x, y in points[1:])
        self.page.ops.append(f"q {_rgb(color)} RG {width:.2f} w 1 J 1 j {('[' + dash + '] 0 d ') if dash else ''}{path} S Q")

    # blocks
    @staticmethod
    def wrap(value: str, chars: int) -> list[str]:
        words, lines, current = str(value).split(), [], ""
        for word in words:
            while len(word) > chars:
                if current:
                    lines.append(current)
                    current=""
                lines.append(word[:chars])
                word = word[chars:]
            candidate = f"{current} {word}".strip()
            if len(candidate) > chars:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current or not lines:
            lines.append(current)
        return lines

    def heading(self, value: str, size: float = 14, space: float = 10) -> None:
        self.ensure(size + space + 30)
        self.y -= space
        self.text(MARGIN, self.y - size, value, size, "bold", "#0d2b52")
        self.y -= size + 4
        self.line([(MARGIN, self.y), (PAGE_WIDTH - MARGIN, self.y)], "#9ec5f4", 0.8)
        self.y -= 8

    def paragraph(self, value: str, size: float = 8.5, font: str = "mono", color: str = "#1d2733", indent: float = 0) -> None:
        width = PAGE_WIDTH - 2 * MARGIN - indent
        chars = max(10, int(width / (COURIER_ADVANCE * size)))
        for row in self.wrap(value, chars):
            self.ensure(size * 1.45)
            self.text(MARGIN + indent, self.y - size, row, size, font, color)
            self.y -= size * 1.45
        self.y -= 3

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int], size: float = 7.5,
              status_column: int | None = None) -> None:
        """Monospace table; `widths` are column widths in characters (wrapped, never truncated)"""
        advance = COURIER_ADVANCE * size
        total = sum(widths) * advance + 6 * len(widths)
        scale = min(1.0, (PAGE_WIDTH - 2 * MARGIN) / total)
        size *= scale
        advance = COURIER_ADVANCE * size
        line_height = size * 1.35
        x_positions = [MARGIN]
        for width in widths[:-1]:
            x_positions.append(x_positions[-1] + width * advance + 6 * scale)
        table_width = x_positions[-1] + widths[-1] * advance + 6 * scale - MARGIN

        def draw_header() -> None:
            self.ensure(line_height * 2 + 4)
            self.rect(MARGIN, self.y - line_height - 3, table_width, line_height + 3, fill="#e6eef8")
            for x, header, width in zip(x_positions, headers, widths):
                self.text(x + 2, self.y - line_height + 1, header[:width], size, "mono-bold", "#0d2b52")
            self.y -= line_height + 4

        draw_header()
        for index, row in enumerate(rows):
            cells = [self.wrap(str(cell), width) for cell, width in zip(row, widths)]
            height = max(len(c) for c in cells) * line_height + 2
            if self.y - height < MARGIN + 24:
                self.new_page()
                draw_header()
            if index % 2:
                self.rect(MARGIN, self.y - height, table_width, height, fill="#f5f8fc")
            for column, (x, lines) in enumerate(zip(x_positions, cells)):
                color = "#1d2733"
                if status_column is not None and column == status_column:
                    text=str(row[column]).upper()
                    color = "#0b7a0b" if text.startswith(("OK", "PASS", "VERIFIED", "YES")) else ("#b42318" if text.startswith(("FAIL", "ERROR", "NO", "INVALID")) else "#8a5a00")
                for line_index, value in enumerate(lines):
                    self.text(x + 2, self.y - (line_index + 1) * line_height + 1, value, size, "mono", color)
            self.y -= height
        self.line([(MARGIN, self.y), (MARGIN + table_width, self.y)], "#c5d3e3", 0.5)
        self.y -= 10

    def key_values(self, pairs: list[tuple[str, str]], size: float = 8.5) -> None:
        label = max((len(k) for k, _ in pairs), default=10) + 2
        self.table(["Item", "Value"], [[k, v] for k, v in pairs], [label, 92 - label], size)

    def plot(self, series: dict[str, list[tuple[float, float]]], *, title: str, colors: list[str], log_y: bool = True,
             height: float = 190, x_label: str = "Iteration", y_label: str = "Initial residual") -> None:
        import math

        self.ensure(height + 40)
        left, right = MARGIN + 46, PAGE_WIDTH - MARGIN - 8
        top, bottom = self.y - 16, self.y - height
        self.text(MARGIN, self.y - 9, title, 9.5, "bold", "#0d2b52")
        points = [p for values in series.values() for p in values if (p[1] > 0 or not log_y)]
        if not points:
            self.paragraph("No data.")
            return
        xs = [p[0] for p in points]
        ys = [math.log10(p[1]) if log_y else p[1] for p in points]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = math.floor(min(ys)), math.ceil(max(ys))
        if y1 == y0:
            y1 = y0 + 1
        sx = lambda x: left + (x - x0) / ((x1 - x0) or 1) * (right - left)  # noqa: E731
        sy = lambda y: bottom + (y - y0) / (y1 - y0) * (top - bottom)  # noqa: E731
        for decade in range(int(y0), int(y1) + 1):  # horizontal grid at decades
            yy = sy(decade)
            self.line([(left, yy), (right, yy)], "#dde5ee", 0.4)
            self.text(MARGIN + 4, yy - 3, f"1e{decade}" if log_y else f"{decade:g}", 6.5, "mono", "#5b6876")
        for k in range(6):
            xv = x0 + k * (x1 - x0) / 5
            self.text(sx(xv) - 8, bottom - 10, f"{xv:.0f}", 6.5, "mono", "#5b6876")
        self.line([(left, bottom), (right, bottom)], "#5b6876", 0.6)
        self.line([(left, bottom), (left, top)], "#5b6876", 0.6)
        legend_x = left
        for (name, values), color in zip(series.items(), colors):
            path = [(sx(x), sy(math.log10(v) if log_y else v)) for x, v in values if v > 0 or not log_y]
            self.line(path, color, 1.1)
            self.rect(legend_x, top + 4, 8, 3, fill=color)
            self.text(legend_x + 11, top + 2.5, name, 7, "mono", "#1d2733")
            legend_x += 11 + len(name) * 4.3 + 14
        self.text(left + (right - left) / 2 - 18, bottom - 20, x_label, 7, "regular", "#5b6876")
        self.text(MARGIN, top + 14, y_label, 7, "regular", "#5b6876")
        self.y = bottom - 30

    # output
    def _decorate(self) -> None:
        count = len(self.pages)
        for number, page in enumerate(self.pages, start=1):
            header = [
                f"BT /{_FONT_IDS['bold']} 8.00 Tf {_rgb('#0d2b52')} rg {MARGIN:.2f} {PAGE_HEIGHT - MARGIN + 6:.2f} Td (" + pdf_text(self.title).decode("latin-1") + ") Tj ET",
                f"BT /{_FONT_IDS['regular']} 7.00 Tf {_rgb('#5b6876')} rg {PAGE_WIDTH - MARGIN - 4.2 * len(self.header):.2f} {PAGE_HEIGHT - MARGIN + 6:.2f} Td (" + pdf_text(self.header).decode("latin-1") + ") Tj ET",
                f"{_rgb('#9ec5f4')} RG 0.6 w {MARGIN:.2f} {PAGE_HEIGHT - MARGIN + 2:.2f} m {PAGE_WIDTH - MARGIN:.2f} {PAGE_HEIGHT - MARGIN + 2:.2f} l S",
                f"BT /{_FONT_IDS['regular']} 7.00 Tf {_rgb('#5b6876')} rg {PAGE_WIDTH - MARGIN - 48:.2f} {MARGIN - 18:.2f} Td (" + pdf_text(f"Page {number} of {count}").decode("latin-1") + ") Tj ET",
                f"BT /{_FONT_IDS['regular']} 7.00 Tf {_rgb('#5b6876')} rg {MARGIN:.2f} {MARGIN - 18:.2f} Td (" + pdf_text("Signed (Ed25519): verify with paramfabric report-verify --public-key").decode("latin-1") + ") Tj ET",
            ]
            page.ops = header + page.ops

    def render(self) -> bytes:
        self._decorate()
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog_id = add(b"")  # placeholders filled below
        pages_id = add(b"")
        font_ids = {name: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode()) for name, base in FONTS.items()}
        fonts = " ".join(f"/{_FONT_IDS[name]} {obj} 0 R" for name, obj in font_ids.items())
        page_ids = []
        for page in self.pages:
            stream = zlib.compress("\n".join(page.ops).encode("latin-1"), 9)
            content_id = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
            page_ids.append(add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << {fonts} >> >> /Contents {content_id} 0 R >>".encode()
            ))
        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_ids)}] /Count {len(page_ids)} >>".encode()
        stamp=(self.created or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        info = [f"/Title ({pdf_text(self.title).decode('latin-1')})", f"/Author ({pdf_text(self.author).decode('latin-1')})",
                "/Producer (Paramfabric report engine)", f"/CreationDate ({stamp})"]
        info += [f"/{key} <{value.encode('utf-8').hex()}>" for key, value in self.metadata.items()]
        info_id = add(("<< " + " ".join(info) + " >>").encode("latin-1"))

        output=bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets=[]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(output))
            output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(output)
        output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
        output += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R /Info {info_id} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(output)
