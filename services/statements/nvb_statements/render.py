"""PDF rendering with ReportLab (pure Python, outside the core).

Safety: every data value is XML-escaped before it reaches a Paragraph (ReportLab parses a
mini-markup that could otherwise load images/links). No HTML engine, no URL or local-file
fetching, no shell commands. Money is formatted from integer minor units only.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BRAND = "Nova Valley Bank (demo)"
WATERMARK = "SYNTHETIC DEMO DATA - NOT A REAL BANK STATEMENT"
ROWS_PER_TABLE = 400  # keeps ReportLab table splitting fast for >10k rows


def fmt_minor(minor: int, currency: str = "") -> str:
    if not isinstance(minor, int):
        raise TypeError("money must be integer minor units")
    sign = "-" if minor < 0 else ""
    v = abs(minor)
    s = f"{sign}{v // 100:,}.{v % 100:02d}"
    return f"{s} {currency}".strip()


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(str(text)), style)


def render_statement(*, manifest: dict, entries: list[dict], period_label: str, timezone: str,
                     request_id: str, generated_at: datetime | None = None) -> bytes:
    generated_at = generated_at or datetime.now(UTC)
    tz = ZoneInfo(timezone)
    cur = manifest["currency"]
    acct = manifest["account"]
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=7.5, leading=9)
    buf = io.BytesIO()

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(colors.red)
        canvas.drawString(15 * mm, 287 * mm, WATERMARK)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 8 * mm, f"{BRAND} - request {request_id} - page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=18 * mm,
                            bottomMargin=15 * mm, title=f"NVB statement {period_label}", author=BRAND,
                            subject=WATERMARK)
    start_local = datetime.fromisoformat(manifest["period_start"]).astimezone(tz)
    end_local = datetime.fromisoformat(manifest["period_end"]).astimezone(tz)
    story = [
        _p(f"{BRAND} - Account statement", styles["Title"]),
        _p(WATERMARK, styles["Heading4"]),
        Spacer(1, 4 * mm),
        Table([
            [_p("Account holder", small), _p(acct.get("holder_name") or "-", small)],
            [_p("Account", small), _p(f"{acct['masked_number']} ({acct['type']}, {acct['nickname']})", small)],
            [_p("Currency", small), _p(cur, small)],
            [_p("Period", small), _p(f"{period_label} ({timezone}): {start_local:%Y-%m-%d %H:%M} to "
                                     f"{end_local:%Y-%m-%d %H:%M} local, end exclusive", small)],
            [_p("Period (UTC)", small), _p(f"{manifest['period_start']} to {manifest['period_end']} [start,end)",
                                           small)],
            [_p("Opening balance", small), _p(fmt_minor(manifest["opening_balance_minor"], cur), small)],
            [_p("Total credits", small), _p(fmt_minor(manifest["credit_total_minor"], cur), small)],
            [_p("Total debits", small), _p(fmt_minor(manifest["debit_total_minor"], cur), small)],
            [_p("Closing balance", small), _p(fmt_minor(manifest["closing_balance_minor"], cur), small)],
            [_p("Transactions", small), _p(str(manifest["entry_count"]), small)],
            [_p("Generated", small), _p(f"{generated_at.astimezone(UTC).isoformat()} (dataset "
                                        f"{manifest['dataset_id'][:16]})", small)],
        ], colWidths=[40 * mm, 140 * mm], style=TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")])),
        Spacer(1, 5 * mm),
    ]
    if acct and manifest.get("account_opened_after_period"):
        story.append(_p("The account was opened after this period.", small))

    header = [_p(h, small) for h in ("Date (local)", "Reference", "Description", "Debit", "Credit", "Balance")]
    style = TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b57")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (3, 1), (5, -1), "RIGHT")])
    running = manifest["opening_balance_minor"]
    rows: list[list] = []
    widths = [24 * mm, 30 * mm, 62 * mm, 22 * mm, 22 * mm, 22 * mm]
    if not entries:
        story.append(_p("No transactions in this period.", small))
    for e in entries:
        running += e["amount_minor"] if e["direction"] == "credit" else -e["amount_minor"]
        local = datetime.fromisoformat(e["posted_at"]).astimezone(tz)
        desc = e["description"] + (f" - {e['counterparty']}" if e["counterparty"] else "")
        rows.append([_p(f"{local:%Y-%m-%d %H:%M}", small), _p(e["reference"], small), _p(desc, small),
                     _p(fmt_minor(e["amount_minor"]) if e["direction"] == "debit" else "", small),
                     _p(fmt_minor(e["amount_minor"]) if e["direction"] == "credit" else "", small),
                     _p(fmt_minor(running), small)])
        if len(rows) == ROWS_PER_TABLE:
            story.append(Table([header] + rows, colWidths=widths, repeatRows=1, style=style))
            rows = []
    if rows:
        story.append(Table([header] + rows, colWidths=widths, repeatRows=1, style=style))
    if running != manifest["closing_balance_minor"]:
        raise ValueError("running balance does not reach the closing balance")
    story += [Spacer(1, 4 * mm), _p("Balances are computed from the core ledger for the stated period. "
                                    "Running balance follows the listed order (posted time, then entry id).", small),
              _p(WATERMARK, small)]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
