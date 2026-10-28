"""ReportLab building blocks shared by all PDF reports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

PAGE = landscape(A4)
MARGIN = 12 * mm
CONTENT_WIDTH = PAGE[0] - 2 * MARGIN

_base = getSampleStyleSheet()
STYLES = {
    "title": ParagraphStyle("title", parent=_base["Title"], fontSize=16, spaceAfter=4, alignment=TA_LEFT),
    "h2": ParagraphStyle("h2", parent=_base["Heading2"], fontSize=11.5, spaceBefore=8, spaceAfter=4),
    "body": ParagraphStyle("body", parent=_base["BodyText"], fontSize=8.5, leading=11),
    "small": ParagraphStyle("small", parent=_base["BodyText"], fontSize=7, leading=8.5),
    "cell": ParagraphStyle("cell", parent=_base["BodyText"], fontSize=7, leading=8.5),
    "note": ParagraphStyle("note", parent=_base["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#444444")),
}
HEADER_BG = colors.HexColor("#1f3a5f")
ZEBRA = colors.HexColor("#f2f5f9")
GAP_BG = colors.HexColor("#fff3cd")
SERIES_COLORS = [colors.HexColor(c) for c in ("#1f77b4", "#6c757d", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b")]


def esc(text) -> str:
    return ("" if text is None else str(text)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def numbered_canvas_factory(footer_left: str):
    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont("Helvetica", 7)
                self.setFillColor(colors.HexColor("#555555"))
                self.drawString(MARGIN, 7 * mm, footer_left)
                self.drawRightString(PAGE[0] - MARGIN, 7 * mm, f"Page {self._pageNumber} of {total}")
                super().showPage()
            super().save()

    return NumberedCanvas


def document(path: str, title: str) -> SimpleDocTemplate:
    return SimpleDocTemplate(path, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN,
                             bottomMargin=14 * mm, title=title, author="Temperature Monitoring Server")


def kv_table(pairs: Sequence[tuple[str, str]], col_widths=(55 * mm, None)) -> Table:
    widths = [col_widths[0], col_widths[1] or CONTENT_WIDTH - col_widths[0]]
    data = [[Paragraph(f"<b>{esc(k)}</b>", STYLES["cell"]), Paragraph(esc(v), STYLES["cell"])] for k, v in pairs]
    table = Table(data or [["", ""]], colWidths=widths, hAlign="LEFT")
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (0, -1), ZEBRA)]))
    return table


def data_tables(header: Sequence[str], rows: Sequence[Sequence], col_widths: Sequence[float] | None = None,
                chunk: int = 250, highlight_rows: set[int] | None = None) -> list[Table]:
    """Tables split in chunks (ReportLab is slow on huge single tables); every
    chunk and every page repeat the header row."""
    highlight_rows = highlight_rows or set()
    if col_widths is None:
        col_widths = [CONTENT_WIDTH / len(header)] * len(header)
    head = [Paragraph(f"<b>{esc(h)}</b>", ParagraphStyle("h", parent=STYLES["cell"], textColor=colors.white))
            for h in header]
    tables = []
    if not rows:
        rows = [["No records match the selected criteria."] + [""] * (len(header) - 1)]
    for offset in range(0, len(rows), chunk):
        part = rows[offset:offset + chunk]
        data = [head] + [[Paragraph(esc(c), STYLES["cell"]) for c in row] for row in part]
        table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        style = [("BACKGROUND", (0, 0), (-1, 0), HEADER_BG), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8c8c8")),
                 ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]
        for i in range(1, len(data)):
            if offset + i - 1 in highlight_rows:
                style.append(("BACKGROUND", (0, i), (-1, i), GAP_BG))
                style.append(("SPAN", (0, i), (-1, i)))
            elif i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
        table.setStyle(TableStyle(style))
        tables.append(table)
    return tables


def trend_chart(series: list[dict], title: str, zone, width: float = CONTENT_WIDTH, height: float = 62 * mm):
    """series: [{"label", "points": [(datetime, value|None)], "dashed": bool}] - None breaks the line."""
    drawing = Drawing(width, height + 14)
    drawing.add(String(0, height + 4, title, fontName="Helvetica-Bold", fontSize=9))
    plot = LinePlot()
    plot.x, plot.y, plot.width, plot.height = 36, 22, width - 50, height - 30
    data, styles = [], []
    all_x, all_y = [], []
    for index, s in enumerate(series):
        segment = []
        for t, v in s["points"]:
            if v is None:
                if len(segment) > 1:
                    data.append(segment)
                    styles.append((index, s.get("dashed", False)))
                segment = []
                continue
            x = t.replace(tzinfo=timezone.utc).timestamp()
            segment.append((x, v))
            all_x.append(x)
            all_y.append(v)
        if len(segment) > 1:
            data.append(segment)
            styles.append((index, s.get("dashed", False)))
    if not data:
        drawing.add(String(plot.x, plot.y + plot.height / 2, "No valid temperature values in this period.",
                           fontName="Helvetica", fontSize=8))
        return drawing
    plot.data = data
    for i, (series_index, dashed) in enumerate(styles):
        plot.lines[i].strokeColor = SERIES_COLORS[series_index % len(SERIES_COLORS)]
        plot.lines[i].strokeWidth = 0.8 if dashed else 0.9
        if dashed:
            plot.lines[i].strokeDashArray = [3, 2]
    lo, hi = min(all_y), max(all_y)
    pad = max((hi - lo) * 0.1, 0.5)
    plot.yValueAxis.valueMin, plot.yValueAxis.valueMax = lo - pad, hi + pad
    plot.yValueAxis.labels.fontSize = 6.5
    plot.yValueAxis.labels.fontName = "Helvetica"
    plot.xValueAxis.labels.fontName = "Helvetica"
    plot.yValueAxis.labelTextFormat = "%.1f"
    plot.xValueAxis.valueMin, plot.xValueAxis.valueMax = min(all_x), max(all_x)
    plot.xValueAxis.labels.fontSize = 6.5
    span = max(all_x) - min(all_x)
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M"
    plot.xValueAxis.labelTextFormat = lambda x: datetime.fromtimestamp(x, timezone.utc).astimezone(zone).strftime(fmt)
    drawing.add(plot)
    legend_x = plot.x
    for index, s in enumerate(series):
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        drawing.add(String(legend_x, 4, f"— {s['label']}", fontName="Helvetica", fontSize=7, fillColor=color))
        legend_x += 9 + 4.2 * len(s["label"])
    return drawing


def note(text: str) -> Paragraph:
    return Paragraph(esc(text), STYLES["note"])
