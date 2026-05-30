#!/usr/bin/env python3
"""
Fountain → WGA-formatted PDF generator.
Usage: python fountain_to_pdf.py <script.fountain> [output.pdf]
"""

import sys
import re
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── Page geometry (WGA standard) ──
PW, PH = letter          # 8.5 x 11 inches
MARGIN_LEFT   = 1.5 * inch
MARGIN_RIGHT  = 1.0 * inch
MARGIN_TOP    = 1.0 * inch
MARGIN_BOTTOM = 1.0 * inch
TEXT_WIDTH    = PW - MARGIN_LEFT - MARGIN_RIGHT   # ~6 inches

# ── WGA indent positions (from left margin) ──
CHAR_INDENT   = 2.2 * inch   # character name
DIAL_INDENT   = 1.0 * inch   # dialogue left
DIAL_RIGHT    = 1.5 * inch   # dialogue right margin offset
PAREN_INDENT  = 1.6 * inch   # parenthetical left
PAREN_RIGHT   = 2.0 * inch   # parenthetical right margin offset

FONT = "Courier"
FONT_SIZE = 12
LINE_H = FONT_SIZE * 1.2      # points — tight Courier spacing
CHARS_PER_LINE = 60           # approx chars for action lines


# ── Fountain parser ──

def parse_fountain(text):
    """Returns list of (type, content) tuples."""
    idx = text.find("\n===\n")
    title_raw = text[:idx] if idx != -1 else ""
    body = text[idx + 5:] if idx != -1 else text

    # Parse title page
    title_fields = {}
    for line in title_raw.splitlines():
        m = re.match(r'^(\w[\w ]*?):\s*(.+)$', line)
        if m:
            title_fields[m.group(1).lower()] = m.group(2).strip()

    elements = [("title_page", title_fields)]
    lines = body.split("\n")
    prev_blank = True
    in_dlg = False

    i = 0
    while i < len(lines):
        raw = lines[i]
        t = raw.strip()

        if not t:
            if elements and elements[-1][0] != "blank":
                elements.append(("blank", ""))
            prev_blank = True
            in_dlg = False
            i += 1
            continue

        if t in ("===", "---"):
            elements.append(("pagebreak", ""))
            prev_blank = True
            in_dlg = False
            i += 1
            continue

        if re.match(r'^(FADE IN:|FADE OUT|FADE TO BLACK|CUT TO:|SMASH CUT|MATCH CUT|TITLE CARD:)', t, re.I):
            elements.append(("transition", t))
            prev_blank = False
            in_dlg = False
            i += 1
            continue

        if re.match(r'^(INT\b|EXT\b|INT\.?\/EXT\.|I\/E\.)', t, re.I):
            elements.append(("heading", t.upper()))
            prev_blank = False
            in_dlg = False
            i += 1
            continue

        if re.match(r'^\(.*\)$', t):
            elements.append(("paren", t))
            prev_blank = False
            i += 1
            continue

        # Character cue detection
        if (prev_blank and t == t.upper() and re.search(r'[A-Z]', t)
                and len(t) < 52 and not re.search(r'[.!?,]$', t)
                and not re.match(r'^(INT\b|EXT\b)', t)):
            nxt = ""
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    nxt = lines[j].strip()
                    break
            if nxt and not re.match(r'^(INT\b|EXT\b)', nxt, re.I) and not re.match(r'^(FADE|CUT TO)', nxt, re.I):
                elements.append(("char", t))
                prev_blank = False
                in_dlg = True
                i += 1
                continue

        if in_dlg and not re.match(r'^(INT\b|EXT\b)', t, re.I):
            elements.append(("dialogue", t))
            prev_blank = False
            i += 1
            continue

        elements.append(("action", t))
        prev_blank = False
        in_dlg = False
        i += 1

    return elements


# ── PDF renderer ──

def wrap_text(text, max_chars):
    """Word-wrap text to max_chars per line."""
    # Strip inline markup for PDF (no italic support in basic Courier)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        if cur:
            test = cur + " " + w
        else:
            test = w
        if len(test) <= max_chars:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines if lines else [""]


def render_title_page(c, fields):
    title = fields.get("title", "Untitled")
    author = fields.get("author", "")
    draft = fields.get("draft date", "")

    c.setFont(FONT, FONT_SIZE)
    cy = PH / 2 + 1.5 * inch

    c.setFont(FONT + "-Bold", 14)
    title_w = c.stringWidth(title, FONT + "-Bold", 14)
    c.drawString((PW - title_w) / 2, cy, title)

    c.setFont(FONT, FONT_SIZE)
    if author:
        cy -= LINE_H * 3
        by_line = f"Written by {author}"
        bw = c.stringWidth(by_line, FONT, FONT_SIZE)
        c.drawString((PW - bw) / 2, cy, by_line)

    if draft:
        cy -= LINE_H * 2
        dw = c.stringWidth(draft, FONT, FONT_SIZE)
        c.drawString((PW - dw) / 2, cy, draft)

    c.showPage()


def render_pdf(elements, out_path, title="Untitled", author=""):
    c = canvas.Canvas(str(out_path), pagesize=letter)
    c.setTitle(title)
    c.setAuthor(author)

    page_num = 0
    cy = PH - MARGIN_TOP  # current y position

    def new_page():
        nonlocal cy, page_num
        c.showPage()
        page_num += 1
        cy = PH - MARGIN_TOP
        c.setFont(FONT, FONT_SIZE)
        # Page number top right
        pn_text = f"{page_num}."
        c.drawRightString(PW - MARGIN_RIGHT, PH - 0.6 * inch, pn_text)

    def need_space(lines_needed):
        space_needed = lines_needed * LINE_H
        if cy - space_needed < MARGIN_BOTTOM:
            new_page()

    def draw_line(text, x, bold=False):
        nonlocal cy
        font = FONT + "-Bold" if bold else FONT
        c.setFont(font, FONT_SIZE)
        c.drawString(x, cy, text)
        cy -= LINE_H

    # Title page
    title_fields = {}
    start_idx = 0
    if elements and elements[0][0] == "title_page":
        title_fields = elements[0][1]
        start_idx = 1

    render_title_page(c, title_fields)
    page_num = 1
    c.setFont(FONT, FONT_SIZE)
    # Page number for first script page
    c.drawRightString(PW - MARGIN_RIGHT, PH - 0.6 * inch, "1.")

    for etype, econtent in elements[start_idx:]:

        if etype == "blank":
            cy -= LINE_H
            if cy < MARGIN_BOTTOM:
                new_page()

        elif etype == "pagebreak":
            new_page()

        elif etype == "heading":
            need_space(3)
            cy -= LINE_H  # extra space before heading
            draw_line(econtent, MARGIN_LEFT, bold=True)

        elif etype == "action":
            wrapped = wrap_text(econtent, CHARS_PER_LINE)
            need_space(len(wrapped) + 1)
            for line in wrapped:
                draw_line(line, MARGIN_LEFT)

        elif etype == "char":
            need_space(3)
            cy -= LINE_H * 0.5
            draw_line(econtent, MARGIN_LEFT + CHAR_INDENT)

        elif etype == "dialogue":
            dial_width_chars = int((TEXT_WIDTH - DIAL_INDENT - DIAL_RIGHT) / (FONT_SIZE * 0.6))
            wrapped = wrap_text(econtent, max(30, dial_width_chars))
            need_space(len(wrapped))
            for line in wrapped:
                draw_line(line, MARGIN_LEFT + DIAL_INDENT)

        elif etype == "paren":
            need_space(2)
            draw_line(econtent, MARGIN_LEFT + PAREN_INDENT)

        elif etype == "transition":
            need_space(2)
            cy -= LINE_H * 0.5
            tw = c.stringWidth(econtent, FONT, FONT_SIZE)
            c.setFont(FONT, FONT_SIZE)
            c.drawString(PW - MARGIN_RIGHT - tw, cy, econtent)
            cy -= LINE_H

    c.save()
    return out_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python fountain_to_pdf.py <script.fountain> [output.pdf]")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"File not found: {src}")
        sys.exit(1)

    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".pdf")

    text = src.read_text(encoding="utf-8")
    elements = parse_fountain(text)

    # Extract title/author from title page
    title = "Untitled"
    author = ""
    if elements and elements[0][0] == "title_page":
        title = elements[0][1].get("title", "Untitled")
        author = elements[0][1].get("author", "")

    render_pdf(elements, out, title=title, author=author)
    print(f"PDF written to: {out}")


if __name__ == "__main__":
    main()
