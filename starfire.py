#!/usr/bin/env python3

# starfire.py:    TEST CAMPAIGN + TEST CARD DESIGNER
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-11
# VERSION:        0.2.0
#
# Single stdlib file (plus python-docx); client-side editor state, the
# server handles exports, saved files, and the optional `measure` binary.

"""
Usage
-----
    python3 starfire.py                    # open GUI on http://localhost:5791
    python3 starfire.py --port 8080
    python3 starfire.py --no-browser

Pages: `/` is the campaign designer; `/test?load=<file>.test` is a single
test card (the campaign page embeds these as tabs).

Tests: `pytest` from the project root.

Dependencies: python-docx for the .docx exports. Optional: a compiled
`measure` binary in the working directory (Sample / Activate buttons) and
jira_config.json (Jira tickets).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from docx import Document
from docx.shared import Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# Local module, bundled into the binary; what's optional is jira_config.json.
import jira_ticket


# Test card: data model

@dataclass
class TestPoint:
    seq: int
    action: str = ""
    expected: str = ""
    # What was actually observed when the point was run -- user-entered,
    # stays editable even on a locked point since this is execution data.
    observed: str = ""
    reference: str = ""
    responsible_party: str = ""
    date: str = ""
    time: str = ""        # 24h "HH:MM" -- filled by the UI's "now" button
    initial: str = ""
    quality: str = ""
    # True if this point existed at the moment the card was last exported
    # locked -- see the "Locking" note on TestCard below.
    locked: bool = False
    # Independent of locked -- always toggleable, recolors the point
    # number and whatever fields are currently editable.
    flagged: bool = False
    # Filenames only (see /capture_image); files live in the working
    # directory alongside .test/.camp files, not the image data itself.
    images: list = field(default_factory=list)
    # USB device node and SCPI command a "read" sends -- e.g.
    # "/dev/usbtmc0" and "MEAS:VOLT:DC?". Manual, like everything else
    # this app doesn't try to auto-discover.
    usb_port: str = ""
    measure_command: str = ""
    # Write-only SCPI command the Activate (bolt) button sends to the same
    # port -- e.g. "OUTP ON". Never a query; see _handle_hardware.
    activate_command: str = ""
    # Each row: [command, value, timestamp, auto]. auto=True rows came
    # from measure and are read-only (delete-only); auto=False
    # rows were typed in by hand and stay fully editable.
    measurements: list = field(default_factory=list)


def _normalize_pair_list(items: list, width: int) -> list:
    """Backward-compat + shape normalization for nomenclature/references/
    notes/warnings: pads each row to `width` elements (the last of which is
    always the `locked` flag), and accepts a bare string in place of a
    single-element row (old files stored notes/warnings as plain strings
    before locking existed)."""
    out = []
    for item in items:
        row = [item] if isinstance(item, str) else list(item)
        while len(row) < width - 1:
            row.append("")
        if len(row) < width:
            row.append(False)
        out.append(row[:width])
    return out


@dataclass
class TestCard:
    id: str = "TC-001"
    title: str = "Untitled Test Card"
    revision: str = "A"
    author: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())
    project: str = ""
    test_conductor: str = ""
    quality_assurance: str = ""
    system: str = ""
    subsystem: str = ""
    test_type: str = ""
    # Free-text requirement code(s) this test verifies. Deliberately
    # unstructured -- tracing coverage is a manual, unenforced responsibility.
    requirements: str = ""
    points: list = field(default_factory=list)         # list[TestPoint]
    # [symbol, definition, units, locked]. Symbol may be raw LaTeX (e.g.
    # "\alpha") -- see GREEK_UNICODE / _latex_symbol for how it renders.
    nomenclature: list = field(default_factory=list)
    # Reference documents cited by test points: list of [ref id, description, locked].
    references: list = field(default_factory=list)
    # Card-level callouts, Epsilon3-style: free-text notes and warnings,
    # each rendered as its own table on the printed card.
    notes: list = field(default_factory=list)       # list[[text, locked]]
    warnings: list = field(default_factory=list)     # list[[text, locked]]
    # A workflow marker, not security (plain JSON, hand-editable). Exporting
    # locked freezes every current row and restricts further edits to a few fields.
    locked: bool = False

    @staticmethod
    def from_dict(d: dict) -> "TestCard":
        pts = [TestPoint(**p) for p in d.get("points", [])]
        nom = [tuple(x) for x in _normalize_pair_list(d.get("nomenclature", []), 4)]
        refs = _normalize_pair_list(d.get("references", []), 3)
        notes = _normalize_pair_list(d.get("notes", []), 2)
        warnings = _normalize_pair_list(d.get("warnings", []), 2)
        base = {k: v for k, v in d.items()
                if k not in ("points", "nomenclature", "references", "notes", "warnings")}
        return TestCard(points=pts, nomenclature=nom, references=refs,
                         notes=notes, warnings=warnings, **base)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d



# Nomenclature -- manual entries only (an autoscan approach didn't scan well in practice).

def sorted_nomenclature(card: TestCard) -> list:
    """card.nomenclature as a list of (symbol, definition, units, locked),
    alphabetized. Tolerates old rows missing units and/or the locked flag."""
    rows = [(r[0], r[1], r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else False)
            for r in card.nomenclature]
    return sorted(rows, key=lambda row: row[0].lower())


# LaTeX Greek-letter commands -> Unicode, for the print/HTML view (the
# .tex export uses the LaTeX command itself, so it doesn't need this).
GREEK_UNICODE = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7", "theta": "\u03b8",
    "iota": "\u03b9", "kappa": "\u03ba", "lambda": "\u03bb", "mu": "\u03bc",
    "nu": "\u03bd", "xi": "\u03be", "pi": "\u03c0", "rho": "\u03c1",
    "sigma": "\u03c3", "tau": "\u03c4", "upsilon": "\u03c5", "phi": "\u03c6",
    "chi": "\u03c7", "psi": "\u03c8", "omega": "\u03c9",
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
}
_GREEK_CMD_RE = re.compile(r"\\([A-Za-z]+)")


def symbol_display(sym: str) -> str:
    """Render a nomenclature symbol for CSV/HTML: LaTeX Greek commands
    (\\alpha, \\Delta_max) become their Unicode letter; anything else
    (V_max, CG) passes through unchanged."""
    return _GREEK_CMD_RE.sub(lambda m: GREEK_UNICODE.get(m.group(1), m.group(0)), sym or "")


# Export: CSV

def export_csv(card: TestCard) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Revision", "Project", "Test Conductor",
                "Quality Assurance", "Author", "Date", "System", "Subsystem", "Test Type",
                "Requirement(s)"])
    w.writerow([card.id, card.title, card.revision, card.project, card.test_conductor,
                card.quality_assurance, card.author, card.date, card.system,
                card.subsystem, card.test_type, card.requirements])
    w.writerow([])
    w.writerow(["Seq", "Action", "Expected Result", "Observed Result", "Reference",
                "Responsible Party", "Date", "Time", "Initial", "Quality"])
    for p in card.points:
        w.writerow([p.seq, p.action, p.expected, p.observed, p.reference,
                    p.responsible_party, p.date, p.time, p.initial, p.quality])
    return buf.getvalue()


# Export: print-ready HTML (browser print-to-PDF, no new dependency).

def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def export_print_html(card: TestCard, host: str = "localhost") -> str:
    rows = "\n".join(
        f"<tr><td>{p.seq}</td><td>{_h(p.action)}</td><td>{_h(p.expected)}</td>"
        f"<td>{_h(p.observed)}</td><td>{_h(p.reference)}</td>"
        f"<td>{_h(p.responsible_party)}</td><td>{_h(p.date)}</td><td>{_h(p.time)}</td>"
        f"<td>{_h(p.initial)}</td><td>{_h(p.quality)}</td></tr>"
        for p in card.points
    )
    nom = sorted_nomenclature(card)
    nom_rows = "\n".join(
        f"<tr><td>{_h(symbol_display(sym))}</td><td>{_h(defn)}</td><td>{_h(units)}</td></tr>"
        for sym, defn, units, _locked in nom
    )
    nom_section = "" if not nom else f"""<h2>Nomenclature</h2>
<table><tr><th style="width:15%">Symbol</th><th>Definition</th><th style="width:15%">Units</th></tr>
{nom_rows}
</table>
"""
    refs = [(r, d) for r, d, _locked in card.references if (r or "").strip() or (d or "").strip()]
    ref_rows = "\n".join(f"<tr><td>{_h(r)}</td><td>{_h(d)}</td></tr>" for r, d in refs)
    ref_section = "" if not refs else f"""<h2>References</h2>
<table><tr><th style="width:20%">Reference</th><th>Description</th></tr>
{ref_rows}
</table>
"""
    warnings = [w for w, _locked in card.warnings if w.strip()]
    warn_bars = "\n".join(f'<div class="warn-bar">\u26a0 {_h(w)}</div>' for w in warnings)
    warn_section = "" if not warnings else f"""<h2>Warnings</h2>
{warn_bars}
"""
    notes = [n for n, _locked in card.notes if n.strip()]
    note_bars = "\n".join(f'<div class="note-bar">\U0001f4dd {_h(n)}</div>' for n in notes)
    note_section = "" if not notes else f"""<h2>Notes</h2>
{note_bars}
"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_h(card.id)} -- {_h(card.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starfire.css"></head>
<body class="print-doc">
<h1>{_h(card.title)}</h1>
<div class="meta">
  <span><b>ID:</b> {_h(card.id)}</span>
  <span><b>Rev:</b> {_h(card.revision)}</span>
  <span><b>Project:</b> {_h(card.project)}</span>
  <span><b>System/Subsystem:</b> {_h(card.system)} / {_h(card.subsystem)}</span>
  <span><b>Test Type:</b> {_h(card.test_type)}</span>
  <span><b>Requirement(s):</b> {_h(card.requirements)}</span>
  <span><b>Author:</b> {_h(card.author)}</span>
</div>
<div class="meta">
  <span><b>Test Conductor:</b> {_h(card.test_conductor)}</span>
  <span><b>Quality Assurance:</b> {_h(card.quality_assurance)}</span>
  <span><b>Date:</b> {_h(card.date)}</span>
</div>
{warn_section}{note_section}{nom_section}{ref_section}<h2>Test Points</h2>
<table class="test-points">
<colgroup>
<col style="width:4%"><col style="width:16%"><col style="width:16%"><col style="width:16%">
<col style="width:10%"><col style="width:8%"><col style="width:8%"><col style="width:6%">
<col style="width:6%"><col style="width:10%">
</colgroup>
<tr><th>#</th><th>Action</th><th>Expected Result</th><th>Observed Result</th><th>Reference</th>
<th>RP</th><th>Date</th><th>Time</th><th>Initial</th><th>Quality</th></tr>
{rows}
</table>
<script>window.onload = () => window.print();</script>
</body></html>"""


# Export: AIAA-format LaTeX -- suppressed from the UI for now, route left in place.

_LATEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "\\": r"\textbackslash{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))


def latex_escape(s: str) -> str:
    return _LATEX_RE.sub(lambda m: _LATEX_SPECIAL[m.group(0)], s or "")


def _latex_symbol(sym: str) -> str:
    # Renders as real LaTeX math for a raw command (\alpha) or a plain
    # subscripted var (V_max); anything else (CG, PID) stays literal text.
    m = re.match(r"^(\\[A-Za-z]+)(?:_([A-Za-z0-9]+))?$", sym or "")
    if m:
        base, sub = m.group(1), m.group(2)
        return f"${base}_{{{sub}}}$" if sub else f"${base}$"
    if "_" in sym and re.match(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+$", sym):
        base, sub = sym.split("_", 1)
        return f"${base}_{{{sub}}}$"
    return latex_escape(sym)


def export_tex(card: TestCard) -> str:
    nom = sorted_nomenclature(card)
    nom_lines = "\n".join(
        f"{_latex_symbol(sym)} \\> = \\> {latex_escape(defn) or 'TBD'}"
        f"{' [' + latex_escape(units) + ']' if units else ''} \\\\"
        for sym, defn, units, _locked in nom
    )
    row_lines = "\n".join(
        f"{p.seq} & {latex_escape(p.action)} & {latex_escape(p.expected)} & "
        f"{latex_escape(p.reference)} \\\\"
        for p in card.points
    )
    # Plain article class + AIAA-style structure; swap in \documentclass{aiaa}
    # below if that LaTeX class is installed.
    return f"""% {card.id} -- {card.title}, generated by starfire.py
% AIAA-style test plan document. Intended to be imported into
% LibreOffice (LaTeX extension) and extended after the test is run.
\\documentclass[11pt]{{article}}
\\usepackage[margin=1in]{{geometry}}
\\usepackage{{booktabs}}
\\usepackage{{array}}
\\usepackage{{amsmath,amssymb}}

\\title{{{latex_escape(card.title)} --- Test Plan}}
\\author{{{latex_escape(card.author)}}}
\\date{{{latex_escape(card.date)}}}

\\begin{{document}}
\\maketitle

\\noindent
\\begin{{tabular}}{{@{{}}ll@{{}}}}
\\textbf{{Project:}} & {latex_escape(card.project)} \\\\
\\textbf{{Test Conductor:}} & {latex_escape(card.test_conductor)} \\\\
\\textbf{{System / Subsystem:}} & {latex_escape(card.system)} / {latex_escape(card.subsystem)} \\\\
\\textbf{{Test Type:}} & {latex_escape(card.test_type)} \\\\
\\textbf{{Card ID / Rev:}} & {latex_escape(card.id)} / {latex_escape(card.revision)} \\\\
\\end{{tabular}}
\\vspace{{1em}}

\\section*{{Nomenclature}}
\\begin{{tabbing}}
XXXXXXXXXXXXXXXX \\= \\kill
{nom_lines if nom_lines else '(none) \\\\'}
\\end{{tabbing}}

\\section{{Test Points}}

\\begin{{table}}[htbp]
\\centering
\\caption{{Test points --- {latex_escape(card.title)} (Rev {latex_escape(card.revision)})}}
\\label{{tab:test_points}}
\\begin{{tabular}}{{@{{}}p{{0.05\\linewidth}}p{{0.30\\linewidth}}p{{0.35\\linewidth}}p{{0.20\\linewidth}}@{{}}}}
\\toprule
\\# & Action & Expected Result & Reference \\\\
\\midrule
{row_lines if row_lines else ' & & & \\\\'}
\\bottomrule
\\end{{tabular}}
\\end{{table}}

\\end{{document}}
"""



# Campaign: data model

@dataclass
class CampaignTestRow:
    seq: int
    test_id: str = ""
    title: str = ""
    status: str = ""   # "", COMPLETE, IN_PROGRESS, TO_DO, BLOCKED
    # Pulled from the linked .test file's own Author metadata, not typed
    # here -- kept in sync opportunistically, no error if it doesn't match yet.
    author: str = ""
    # Same pattern as Author, but not Author itself: a Task may get
    # reassigned in Jira, so baking in the test's original author wouldn't stay meaningful.
    requirements: str = ""


@dataclass
class Campaign:
    id: str = "SC-001"
    title: str = "Untitled Campaign"
    revision: str = "A"
    author: str = ""
    date: str = field(default_factory=lambda: date.today().isoformat())
    project: str = ""
    test_lead: str = ""
    quality_assurance: str = ""
    system: str = ""
    subsystem: str = ""
    # Requirement code + description, same shape as a TestCard's References.
    requirements: list = field(default_factory=list)   # list[(code, description)]
    tests: list = field(default_factory=list)           # list[CampaignTestRow] -- "Test Series"
    # Same shape as tests, for tests that support the series but aren't
    # part of its numbered sequence (one-off checkouts, vendor acceptance).
    ancillary_tests: list = field(default_factory=list)   # list[CampaignTestRow]
    # Free-form writeup blocks: each a (header, text) pair, rendered after
    # the Tests table; text is preserved as-is wherever it's exported.
    sections: list = field(default_factory=list)         # list[(header, text)]
    # Optional Jira Epic every ticket from this campaign links to. Passed
    # down into each open test tab once its iframe loads -- see openTestTab() below.
    jira_epic_key: str = ""

    @staticmethod
    def from_dict(d: dict) -> "Campaign":
        tests = [CampaignTestRow(**t) for t in d.get("tests", [])]
        ancillary_tests = [CampaignTestRow(**t) for t in d.get("ancillary_tests", [])]
        base = {k: v for k, v in d.items() if k not in ("tests", "ancillary_tests")}
        return Campaign(tests=tests, ancillary_tests=ancillary_tests, **base)

    def to_dict(self) -> dict:
        return asdict(self)


STATUS_LABELS = {
    "COMPLETE": "COMPLETE", "IN_PROGRESS": "IN PROGRESS",
    "TO_DO": "TO DO", "BLOCKED": "BLOCKED",
}


def test_filename(test_id: str) -> str:
    """The .test filename a campaign's Tests-table row links to. Derived
    from Test ID rather than stored separately -- one less field to keep
    in sync."""
    return _safe_filename(test_id, ".test")


def vcrm_filename(campaign_id: str) -> str:
    return _safe_filename(campaign_id, ".vcrm")


def compute_vcrm(camp: Campaign) -> dict:
    """Verification Cross-Reference Matrix: requirement -> which Test
    Series tests verify it -> rolled-up coverage. Read-only and computed
    fresh each time from data that already exists elsewhere (the
    campaign's Requirements table, and each referenced test's own
    `requirements` field) -- nothing new to keep in sync, same "manual,
    not enforced" traceability philosophy as the rest of the app: a typo
    in either place just means no link shows up, silently, no validation.

    Ancillary Tests are deliberately excluded -- they're invoked FROM a
    Test Series test (a performance check, etc.), not an independent
    verification of a requirement in their own right.
    """
    test_cache: dict = {}

    def get_test(test_id: str):
        if test_id not in test_cache:
            path = os.path.join(os.getcwd(), test_filename(test_id))
            card = None
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        card = TestCard.from_dict(json.loads(f.read()))
                except (OSError, json.JSONDecodeError, TypeError):
                    card = None
            test_cache[test_id] = card
        return test_cache[test_id]

    rows = []
    for req_code, req_desc in camp.requirements:
        req_code = (req_code or "").strip()
        if not req_code:
            continue
        covering = []
        for t in camp.tests:
            card = get_test(t.test_id)
            if not card:
                continue
            tokens = [tok.strip() for tok in (card.requirements or "").split(",")]
            if req_code in tokens:
                covering.append({"test_id": t.test_id, "title": t.title, "status": t.status})
        if not covering:
            coverage = "Not Covered"
        elif all(c["status"] == "COMPLETE" for c in covering):
            coverage = "Covered"
        elif any(c["status"] == "COMPLETE" for c in covering):
            coverage = "Partial"
        else:
            coverage = "Planned"
        rows.append({"requirement": req_code, "description": req_desc,
                      "covering_tests": covering, "coverage": coverage})

    return {
        "campaign_id": camp.id, "campaign_title": camp.title,
        # No 'T' separator, no seconds -- this is shown to a human as
        # "when was this generated", not parsed back programmatically.
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rows": rows,
    }


# Export: print-ready HTML (same visual system as the test card's).

def export_campaign_print_html(camp: Campaign, host: str = "localhost") -> str:
    h = _h
    reqs = [(c, d) for c, d in camp.requirements if (c or "").strip() or (d or "").strip()]
    req_rows = "\n".join(f"<tr><td>{h(c)}</td><td>{h(d)}</td></tr>" for c, d in reqs)
    req_section = "" if not reqs else f"""<h2>Requirements</h2>
<table><tr><th style="width:20%">Requirement</th><th>Description</th></tr>
{req_rows}
</table>
"""
    test_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.tests
    )
    anc_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(STATUS_LABELS.get(t.status, t.status))}</td></tr>"
        for t in camp.ancillary_tests
    )
    anc_section = "" if not camp.ancillary_tests else f"""<h2>Ancillary Tests</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:14%">Test ID</th><th>Title</th><th style="width:12%">Author</th><th style="width:16%">Requirements</th><th style="width:12%">Status</th></tr>
{anc_rows}
</table>
"""
    sections_html = "\n".join(
        f'<h2>{h(header)}</h2>\n<div class="section-text">{h(text)}</div>'
        for header, text in camp.sections
        if (header or "").strip() or (text or "").strip()
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{h(camp.id)} -- {h(camp.title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starfire.css"></head>
<body class="print-doc">
<h1>{h(camp.title)}</h1>
<div class="meta">
  <span><b>ID:</b> {h(camp.id)}</span>
  <span><b>Rev:</b> {h(camp.revision)}</span>
  <span><b>Project:</b> {h(camp.project)}</span>
  <span><b>System/Subsystem:</b> {h(camp.system)} / {h(camp.subsystem)}</span>
  <span><b>Author:</b> {h(camp.author)}</span>
</div>
<div class="meta">
  <span><b>Test Lead:</b> {h(camp.test_lead)}</span>
  <span><b>Quality Assurance:</b> {h(camp.quality_assurance)}</span>
  <span><b>Date:</b> {h(camp.date)}</span>
</div>
{req_section}<h2>Test Series</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:14%">Test ID</th><th>Title</th><th style="width:12%">Author</th><th style="width:16%">Requirements</th><th style="width:12%">Status</th></tr>
{test_rows}
</table>
{anc_section}{sections_html}
<script>window.onload = () => window.print();</script>
</body></html>"""


def export_vcrm_print_html(vcrm: dict, host: str = "localhost") -> str:
    h = _h

    def covering_str(row):
        if not row["covering_tests"]:
            return "--"
        parts = [f"{c['test_id']} ({STATUS_LABELS.get(c['status'], c['status'] or '[None]')})"
                  for c in row["covering_tests"]]
        return ", ".join(parts)

    rows_html = "\n".join(
        f"<tr><td>{h(r['requirement'])}</td><td>{h(r['description'])}</td>"
        f"<td>{h(covering_str(r))}</td><td>{h(r['coverage'])}</td><td>{h(vcrm['generated'])}</td></tr>"
        for r in vcrm["rows"]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{h(vcrm['campaign_id'])} VCRM -- {h(vcrm['campaign_title'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starfire.css"></head>
<body class="print-doc">
<h1>Verification Cross-Reference Matrix</h1>
<table>
<tr><th style="width:14%">Requirement</th><th>Description</th><th style="width:24%">Covering Tests</th><th style="width:10%">Coverage</th><th style="width:12%">Updated</th></tr>
{rows_html}
</table>
<script>window.onload = () => window.print();</script>
</body></html>"""


# Export: .docx (python-docx).

_HEADER_FILL = "E7EEF5"
_WARN_FILL = "F8D7D7"
_NOTE_FILL = "E7EEF5"


def _shade_cell(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _add_table(doc: Document, headers: list, rows: list, widths=None):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for i, htext in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.text = htext
        cell.paragraphs[0].runs[0].bold = True
        _shade_cell(cell, _HEADER_FILL)
    for row in rows:
        cells = table.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = str(val)
    return table


def _add_callout(doc: Document, text: str, hex_fill: str) -> None:
    table = doc.add_table(rows=1, cols=1)
    table.autofit = True
    cell = table.rows[0].cells[0]
    cell.text = text
    _shade_cell(cell, hex_fill)
    # No visible grid for callouts -- clear the borders python-docx's
    # default table style would otherwise draw.
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "nil")
        borders.append(el)
    tblPr.append(borders)


def export_campaign_docx(camp: Campaign) -> bytes:
    doc = Document()
    doc.add_heading(camp.title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"ID: {camp.id}    Rev: {camp.revision}    Project: {camp.project}    "
        f"System/Subsystem: {camp.system} / {camp.subsystem}    Author: {camp.author}\n"
        f"Test Lead: {camp.test_lead}    Quality Assurance: {camp.quality_assurance}    "
        f"Date: {camp.date}"
    ).font.size = Pt(9.5)

    reqs = [(c, d) for c, d in camp.requirements if (c or "").strip() or (d or "").strip()]
    if reqs:
        doc.add_heading("Requirements", level=1)
        _add_table(doc, ["Requirement", "Description"], reqs)

    doc.add_heading("Test Series", level=1)
    _add_table(
        doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
        [(t.seq, t.test_id, t.title, t.author, t.requirements, STATUS_LABELS.get(t.status, t.status))
         for t in camp.tests],
    )

    if camp.ancillary_tests:
        doc.add_heading("Ancillary Tests", level=1)
        _add_table(
            doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
            [(t.seq, t.test_id, t.title, t.author, t.requirements, STATUS_LABELS.get(t.status, t.status))
             for t in camp.ancillary_tests],
        )

    for header, text in camp.sections:
        if not (header or "").strip() and not (text or "").strip():
            continue
        doc.add_heading(header, level=1)
        for line in (text or "").split("\n"):
            doc.add_paragraph(line)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_vcrm_docx(vcrm: dict) -> bytes:
    doc = Document()
    doc.add_heading("Verification Cross-Reference Matrix", level=0)

    def covering_str(row):
        if not row["covering_tests"]:
            return "--"
        parts = [f"{c['test_id']} ({STATUS_LABELS.get(c['status'], c['status'] or '[None]')})"
                  for c in row["covering_tests"]]
        return ", ".join(parts)

    _add_table(
        doc, ["Requirement", "Description", "Covering Tests", "Coverage", "Updated"],
        [(r["requirement"], r["description"], covering_str(r), r["coverage"], vcrm["generated"])
         for r in vcrm["rows"]],
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def export_test_docx(card: "TestCard") -> bytes:
    doc = Document()
    doc.add_heading(card.title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"ID: {card.id}    Rev: {card.revision}    Project: {card.project}    "
        f"System/Subsystem: {card.system} / {card.subsystem}    Test Type: {card.test_type}    "
        f"Author: {card.author}\n"
        f"Test Conductor: {card.test_conductor}    Quality Assurance: {card.quality_assurance}    "
        f"Date: {card.date}    Requirement(s): {card.requirements}"
    ).font.size = Pt(9.5)

    warnings = [w for w, _locked in card.warnings if w.strip()]
    if warnings:
        doc.add_heading("Warnings", level=1)
        for w in warnings:
            _add_callout(doc, "\u26a0 " + w, _WARN_FILL)

    notes = [n for n, _locked in card.notes if n.strip()]
    if notes:
        doc.add_heading("Notes", level=1)
        for n in notes:
            _add_callout(doc, "\U0001f4dd " + n, _NOTE_FILL)

    nom = sorted_nomenclature(card)
    if nom:
        doc.add_heading("Nomenclature", level=1)
        _add_table(
            doc, ["Symbol", "Definition", "Units"],
            [(symbol_display(s), d, u) for s, d, u, _locked in nom],
        )

    refs = [(r, d) for r, d, _locked in card.references if (r or "").strip() or (d or "").strip()]
    if refs:
        doc.add_heading("References", level=1)
        _add_table(doc, ["Reference", "Description"], refs)

    doc.add_heading("Test Points", level=1)
    _add_table(
        doc,
        ["#", "Action", "Expected Result", "Observed Result", "Reference", "Responsible Party",
         "Date", "Time", "Initial", "Quality"],
        [(p.seq, p.action, p.expected, p.observed, p.reference, p.responsible_party,
          p.date, p.time, p.initial, p.quality) for p in card.points],
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()



# Browser UI

_TEST_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Starfire -- test card</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/starfire.css">
</head>
<body class="editor-doc">
<div id="panels"><div class="panel active">
<h1>STARFIRE</h1>
<div class="sub">test card / test point creator</div>

<h2>Card metadata</h2>
<div class="meta">
  <div class="field"><label>ID</label><input id="m-id"></div>
  <div class="field"><label>Title</label><input id="m-title"></div>
  <div class="field"><label>Revision</label><input id="m-revision"></div>
  <div class="field"><label>Project</label><input id="m-project"></div>
  <div class="field"><label>Author</label><input id="m-author"></div>
  <div class="field"><label>Test Conductor</label><input id="m-test_conductor"></div>
  <div class="field"><label>Quality Assurance</label><input id="m-quality_assurance"></div>
  <div class="field"><label>Date</label><input id="m-date"></div>
  <div class="field"><label>System / Subsystem</label><input id="m-system"></div>
  <div class="field"><label>Test type</label><input id="m-test_type"></div>
  <div class="field"><label>Requirement(s)</label><input id="m-requirements"></div>
</div>

<h2 class="no-rule">Test points</h2>
<div id="pointsList"></div>
<div class="row"><button class="btn btn-plus" id="addPointBtn" title="add point">+</button></div>

<h2 class="no-rule">Nomenclature</h2>
<table class="nomtable" id="nomTable">
  <tr><th>Symbol</th><th>Definition</th><th>Units</th><th class="blank-th" style="width:60px"></th></tr>
</table>
<div class="row"><button class="btn btn-plus" id="addNomBtn" title="add symbol">+</button></div>

<h2 class="no-rule">References</h2>
<table class="nomtable" id="refTable">
  <tr><th>Reference</th><th>Description</th><th class="blank-th" style="width:60px"></th></tr>
</table>
<div class="row"><button class="btn btn-plus" id="addRefBtn" title="add reference">+</button></div>


<h2 class="no-rule">Warnings</h2>
<div id="warningsList"></div>
<div class="row"><button class="btn btn-plus" id="addWarningBtn" title="add warning">+</button></div>

<h2 class="no-rule">Notes</h2>
<div id="notesList"></div>
<div class="row"><button class="btn btn-plus" id="addNoteBtn" title="add note">+</button></div>


<h2>Save / load / export</h2>
<div class="row" id="exportRow">
  <button class="btn" id="loadJsonBtn">import test file</button>
  <input type="file" id="loadJsonFile" accept=".test,.json,application/json" style="display:none">
  <button class="btn" id="saveUnlockedBtn">export unlocked test file (.test)</button>
  <button class="btn" id="saveLockedBtn">export locked test file (.test)</button>
  <button class="btn" id="exportCsvBtn">export .csv</button>
  <button class="btn" id="exportPrintBtn">generate card</button>
  <button class="btn" id="exportDocxBtn">export .docx</button>
</div>
<div id="saveStatus" class="hint"></div>
</div></div>

<script>
// Hide the app-title branding when embedded (a campaign tab's iframe),
// since the campaign page has its own header.
if (window.self !== window.top) {
  document.querySelectorAll("h1, .sub").forEach(el => el.style.visibility = "hidden");
}

// True only if jira_config.json was found in the working directory at
// startup -- see jira_ticket.py's own docstring.
const JIRA_AVAILABLE = __JIRA_AVAILABLE__;

// True only if a compiled measure binary was found -- the Measure
// panel opens either way, this just controls whether Sample/Activate send anything.
const MEASURE_AVAILABLE = __MEASURE_AVAILABLE__;

// Set by the campaign page right after this iframe loads (openTestTab());
// `var` (not `let`) so that external write reaches this binding.
var starfireJiraEpicKey = "";

let card = {
  id: "TC-001", title: "Untitled Test Card", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_conductor: "",
  quality_assurance: "", system: "", subsystem: "", test_type: "", requirements: "",
  points: [], nomenclature: [], references: [], notes: [], warnings: [], locked: false
};

const metaFields = ["id","title","revision","author","date","project","test_conductor","quality_assurance","test_type","requirements"];
const ALWAYS_EDITABLE_META = new Set(["test_conductor", "quality_assurance", "date"]);
function bindMeta() {
  metaFields.forEach(f => {
    const el = document.getElementById("m-" + f);
    el.value = card[f];
    el.disabled = !!card.locked && !ALWAYS_EDITABLE_META.has(f);
    el.addEventListener("input", () => { card[f] = el.value; });
  });
  const sysEl = document.getElementById("m-system");
  sysEl.value = [card.system, card.subsystem].filter(Boolean).join(" / ");
  sysEl.disabled = !!card.locked;
  sysEl.addEventListener("input", () => {
    const parts = sysEl.value.split("/").map(s => s.trim());
    card.system = parts[0] || ""; card.subsystem = parts[1] || "";
  });
}

function nextSeq() {
  const max = card.points.reduce((m, p) => Math.max(m, +p.seq || 0), 0);
  return Math.floor(max / 10) * 10 + 10;
}

// Google Material Symbols, embedded inline (not a file/CDN) so they work
// fully offline; fill="currentColor" picks up each button's own hover color.
const ICON_FLAG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-680h360l16 80h224v400H520l-16-80H280v280h-80Zm300-440Zm86 160h134v-240H510l-16-80H280v240h290l16 80Z"/></svg>';
const ICON_PHOTO_CAMERA = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M480-260q75 0 127.5-52.5T660-440q0-75-52.5-127.5T480-620q-75 0-127.5 52.5T300-440q0 75 52.5 127.5T480-260Zm0-80q-42 0-71-29t-29-71q0-42 29-71t71-29q42 0 71 29t29 71q0 42-29 71t-71 29ZM160-120q-33 0-56.5-23.5T80-200v-480q0-33 23.5-56.5T160-760h126l74-80h240l74 80h126q33 0 56.5 23.5T880-680v480q0 33-23.5 56.5T800-120H160Zm0-80h640v-480H638l-73-80H395l-73 80H160v480Zm320-240Z"/></svg>';
const ICON_ADD_A_PHOTO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-440ZM120-120q-33 0-56.5-23.5T40-200v-480q0-33 23.5-56.5T120-760h126l74-80h240v80H355l-73 80H120v480h640v-360h80v360q0 33-23.5 56.5T760-120H120Zm640-560v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80ZM440-260q75 0 127.5-52.5T620-440q0-75-52.5-127.5T440-620q-75 0-127.5 52.5T260-440q0 75 52.5 127.5T440-260Zm0-80q-42 0-71-29t-29-71q0-42 29-71t71-29q42 0 71 29t29 71q0 42-29 71t-71 29Z"/></svg>';
const ICON_SPEED = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M480-316.5q38-.5 56-27.5l224-336-336 224q-27 18-28.5 55t22.5 61q24 24 62 23.5Zm0-483.5q59 0 113.5 16.5T696-734l-76 48q-33-17-68.5-25.5T480-720q-133 0-226.5 93.5T160-400q0 42 11.5 83t32.5 77h552q23-38 33.5-79t10.5-85q0-36-8.5-70T766-540l48-76q30 47 47.5 100T880-406q1 57-13 109t-41 99q-11 18-30 28t-40 10H204q-21 0-40-10t-30-28q-26-45-40-95.5T80-400q0-83 31.5-155.5t86-127Q252-737 325-768.5T480-800Zm7 313Z"/></svg>';
const ICON_BOLT = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="m422-232 207-248H469l29-227-185 267h139l-30 208ZM320-80l40-280H160l360-520h80l-40 320h240L400-80h-80Zm151-390Z"/></svg>';
// Crosshair: "sample this reading". Children set fill="none" explicitly, since
// .btn-icon svg's CSS fill would otherwise paint them solid.
const ICON_BOOKMARK_ADD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-640q0-33 23.5-56.5T280-840h240v80H280v518l200-86 200 86v-278h80v400L480-240 200-120Zm80-640h240-240Zm400 160v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80Z"/></svg>';
const ICON_ARROW_UPWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z"/></svg>';
const ICON_ARROW_DOWNWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-800v487L216-537l-56 57 320 320 320-320-56-57-224 224v-487h-80Z"/></svg>';
const ICON_CLOSE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z"/></svg>';
// A thumbnail chip's own label icon, distinct from the point-button-cluster icons above.
const ICON_IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h560q33 0 56.5 23.5T840-760v560q0 33-23.5 56.5T760-120H200Zm0-80h560v-560H200v560Zm40-80h480L570-480 450-320l-90-120-120 160Zm-40 80v-560 560Z"/></svg>';

const QUALITY_OPTIONS = [
  ["", "[None]"],
  ["RFI", "\U0001F535 RFI"],
  ["PASS", "\U0001F7E2 PASS"],
  ["CAUTION", "\U0001F7E0 CAUTION"],
  ["FAIL", "\U0001F534 FAIL"],
];
function qualitySelect(i, current) {
  const opts = QUALITY_OPTIONS.map(([v, label]) =>
    `<option value="${v}"${v === current ? " selected" : ""}>${label}</option>`).join("");
  return `<select data-i="${i}" data-f="quality">${opts}</select>`;
}

// One camera stream shared across all points, acquired once via
// getUserMedia() -- reaches any UVC-class video device the OS exposes.
let cameraStream = null;
let cameraVideo = null;
let cameraCanvas = null;
let previewOpenIndex = null;

function ensureCameraElements() {
  if (!cameraVideo) {
    cameraVideo = document.createElement("video");
    cameraVideo.autoplay = true;
    cameraVideo.muted = true;
    cameraVideo.playsInline = true;
    cameraVideo.className = "cam-preview";
  }
  if (!cameraCanvas) cameraCanvas = document.createElement("canvas");
}

async function ensureCameraStream() {
  if (cameraStream) return cameraStream;
  ensureCameraElements();
  cameraStream = await navigator.mediaDevices.getUserMedia({video: true});
  cameraVideo.srcObject = cameraStream;
  return cameraStream;
}

function reattachPreviewIfOpen() {
  if (previewOpenIndex === null) return;
  const slot = document.getElementById("pt-camera-" + previewOpenIndex);
  if (slot && cameraVideo) slot.appendChild(cameraVideo);
}

function showCamMessage(i, text) {
  const el = document.getElementById("pt-cam-msg-" + i);
  if (!el) return;
  el.textContent = text;
  setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
}

async function capturePointImage(i) {
  try {
    await ensureCameraStream();
  } catch (err) {
    showCamMessage(i, "No camera available");
    return;
  }
  const w = cameraVideo.videoWidth, h = cameraVideo.videoHeight;
  if (!w || !h) { showCamMessage(i, "Camera not ready yet -- try again"); return; }
  cameraCanvas.width = w; cameraCanvas.height = h;
  cameraCanvas.getContext("2d").drawImage(cameraVideo, 0, 0, w, h);
  const dataUrl = cameraCanvas.toDataURL("image/jpeg", 0.9);
  try {
    const res = await fetch("/capture_image", {method: "POST", body: JSON.stringify({data: dataUrl})});
    const result = await res.json();
    if (!result.saved) throw new Error(result.error || "unknown error");
    card.points[i].images = card.points[i].images || [];
    card.points[i].images.push(result.filename);
    renderPoints();
    showCamMessage(i, "Captured " + result.filename);
  } catch (err) {
    showCamMessage(i, "Capture failed: " + err);
  }
}

async function createJiraTicket(i) {
  const p = card.points[i];
  const msgEl = document.getElementById("pt-jira-msg-" + i);
  if (msgEl) msgEl.innerHTML = "Creating ticket...";
  const summary = `${card.id} pt ${p.seq}: ${(p.action || "(no action text)").slice(0, 120)}`;
  const description = [
    `Test: ${card.id} -- ${card.title}`,
    `Point ${p.seq}`,
    `Action: ${p.action || ""}`,
    `Expected: ${p.expected || ""}`,
    `Observed: ${p.observed || ""}`,
    `Reference: ${p.reference || ""}`,
    `Quality: ${p.quality || ""}`,
  ].join("\\n");
  try {
    const res = await fetch("/create_jira_ticket", {
      method: "POST",
      body: JSON.stringify({summary, description, epic_key: starfireJiraEpicKey || ""}),
    });
    const result = await res.json();
    if (!msgEl) return;
    if (result.success) {
      msgEl.innerHTML = `Created <a href="${esc(result.url)}" target="_blank" rel="noopener">${esc(result.key)}</a>`;
    } else {
      msgEl.textContent = `Ticket creation failed: ${result.error || "unknown error"}`;
    }
  } catch (err) {
    if (msgEl) msgEl.textContent = `Ticket creation failed: ${err}`;
  }
}

function togglePreview(i) {
  if (previewOpenIndex === i) {
    if (cameraVideo && cameraVideo.parentElement) cameraVideo.parentElement.removeChild(cameraVideo);
    previewOpenIndex = null;
    return;
  }
  ensureCameraStream().then(() => {
    const slot = document.getElementById("pt-camera-" + i);
    if (cameraVideo.parentElement) cameraVideo.parentElement.removeChild(cameraVideo);
    slot.appendChild(cameraVideo);
    previewOpenIndex = i;
  }).catch(() => {
    showCamMessage(i, "No camera available");
  });
}

// Unlike the camera (one shared hardware stream, so only one point's
// preview can be open at a time), Measure is just form fields -- any
// number of points can have it open simultaneously.
let measureOpenIndices = new Set();

function openMeasure(i) {
  if (measureOpenIndices.has(i)) return;
  measureOpenIndices.add(i);
  // First open of a point with no readings -- seed one blank row so
  // there's something to type into right away.
  if (card.points[i].measurements.length === 0) {
    card.points[i].measurements.push({command: "", value: "", timestamp: "", auto: false});
  }
  renderPoints();
}

async function readMeasurement(i) {
  const p = card.points[i];
  // If the panel isn't already open (e.g. Read was clicked straight from
  // the main button cluster), open it now so there's somewhere to show
  // the result -- otherwise the message/table the read produces would
  // have no visible home at all.
  openMeasure(i);
  const msgEl = document.getElementById("pt-measure-msg-" + i);
  if (!MEASURE_AVAILABLE) {
    if (msgEl) msgEl.textContent = "measure binary not found -- enter readings by hand.";
    return;
  }
  if (p.measurements.length >= 8) {
    if (msgEl) msgEl.textContent = "Maximum 8 readings -- delete a row first.";
    return;
  }
  if (msgEl) msgEl.textContent = "Reading...";
  try {
    const res = await fetch("/measure", {
      method: "POST",
      body: JSON.stringify({port: p.usb_port, command: p.measure_command}),
    });
    const result = await res.json();
    if (result.success) {
      const now = new Date();
      const ts = now.toISOString().slice(0, 16).replace("T", " ");
      p.measurements.push({command: p.measure_command, value: result.value, timestamp: ts, auto: true});
      if (msgEl) msgEl.textContent = "";
      renderPoints();
    } else {
      if (msgEl) msgEl.textContent = result.error || "Instrument not found";
    }
  } catch (err) {
    if (msgEl) msgEl.textContent = `Read failed: ${err}`;
  }
}

async function activateHardware(i) {
  const p = card.points[i];
  openMeasure(i);
  const msgEl = document.getElementById("pt-measure-msg-" + i);
  if (!MEASURE_AVAILABLE) {
    if (msgEl) msgEl.textContent = "measure binary not found -- nothing was sent.";
    return;
  }
  if (!p.usb_port || !p.activate_command) {
    if (msgEl) msgEl.textContent = "Enter a USB port and an activate command first.";
    return;
  }
  if (!confirm(`Send "${p.activate_command}" to ${p.usb_port}?`)) return;
  if (msgEl) msgEl.textContent = "Activating...";
  try {
    const res = await fetch("/activate", {
      method: "POST",
      body: JSON.stringify({port: p.usb_port, command: p.activate_command}),
    });
    const result = await res.json();
    const when = new Date().toLocaleTimeString("en-GB");
    if (msgEl) msgEl.textContent = result.success ? `Sent ${p.activate_command} at ${when}` : (result.error || "Activate failed");
  } catch (err) {
    if (msgEl) msgEl.textContent = `Activate failed: ${err}`;
  }
}

function measurePanelHtml(i, p) {
  const open = measureOpenIndices.has(i);
  const rows = p.measurements.map((m, j) => {
    if (m.auto) {
      return `<tr>
        <td>${esc(m.command)}</td><td>${esc(m.value)}</td><td>${esc(m.timestamp)}</td>
        <td class="actions-cell"><button class="btn mini btn-icon btn-x" data-mdel="${j}" data-i="${i}" title="delete row">${ICON_CLOSE}</button></td>
      </tr>`;
    }
    // Manual row: Command never applies (no instrument query was sent for
    // a by-eye reading), Value is the one genuinely manual field,
    // Timestamp is set via "now", never typed.
    return `<tr>
      <td>--</td>
      <td><input data-mi="${i}" data-mj="${j}" data-mf="value" value="${esc(m.value)}"></td>
      <td class="measure-ts-cell">
        <span>${esc(m.timestamp)}</span>
        <button class="btn mini" data-mnow="${j}" data-i="${i}" title="now">now</button>
      </td>
      <td class="actions-cell"><button class="btn mini btn-icon btn-x" data-mdel="${j}" data-i="${i}" title="delete row">${ICON_CLOSE}</button></td>
    </tr>`;
  }).join("");
  const canAddRow = p.measurements.length < 8;
  const fields = open ? `
    <div class="measure-fields">
      <div><label>USB Port</label><input data-i="${i}" data-f="usb_port" value="${esc(p.usb_port)}" placeholder="/dev/usbtmc0"></div>
      <div><label>Sample Command</label><input data-i="${i}" data-f="measure_command" value="${esc(p.measure_command)}" placeholder="MEAS:VOLT:DC?"></div>
      <div class="wide"><label>Activate Command</label><input data-i="${i}" data-f="activate_command" value="${esc(p.activate_command)}" placeholder="OUTP ON"></div>
    </div>
    <div class="pt-cam-msg" id="pt-measure-msg-${i}"></div>` : "";
  return `
    ${fields}
    <table class="measure-table">
      <colgroup><col class="mt-cmd"><col class="mt-val"><col class="mt-ts"><col class="mt-del"></colgroup>
      <tr><th>Command</th><th>Value</th><th>Timestamp</th><th class="blank-th"></th></tr>
      ${rows}
    </table>
    ${open && canAddRow ? `<button class="btn mini btn-plus" data-madd="${i}" title="add row">+</button>` : ""}`;
}

function renderPoints() {
  const list = document.getElementById("pointsList");
  list.innerHTML = "";
  card.points.forEach((p, i) => {
    const locked = !!p.locked;
    const flagged = !!p.flagged;
    const dis = locked ? "disabled" : "";
    const div = document.createElement("div");
    div.className = "pt-card" + (locked ? " locked" : "") + (flagged ? " flagged" : "");
    const imgChips = (p.images || []).map(fn =>
      `<span class="thumb-chip">${ICON_IMAGE} ${esc(fn)} <button class="btn mini btn-x" data-rm-img="${esc(fn)}" data-i="${i}">x</button></span>`
    ).join("");
    // Ticket is offered on CAUTION/FAIL (a verdict), not on flagged,
    // which is just a "pay attention here" highlight independent of pass/fail.
    const showJiraBtn = JIRA_AVAILABLE && (p.quality === "CAUTION" || p.quality === "FAIL");
    div.innerHTML = `
      <div class="pt-head">
        <input class="seq" data-i="${i}" data-f="seq" value="${esc(p.seq)}">
        ${locked ? '<span class="lock-tag">\U0001F512 locked</span>' : ""}
        <div class="pt-btns">
          <button class="btn mini btn-icon flag-btn${flagged ? " flagged" : ""}" data-act="flag" data-i="${i}" title="flag">${ICON_FLAG}</button>
          <button class="btn mini btn-icon" data-act="preview" data-i="${i}" title="camera preview">${ICON_PHOTO_CAMERA}</button>
          <button class="btn mini btn-icon" data-act="capture" data-i="${i}" title="capture image">${ICON_ADD_A_PHOTO}</button>
          <button class="btn mini btn-icon measure-read" data-mread="${i}" title="sample">${ICON_SPEED}</button>
          <button class="btn mini btn-icon measure-activate" data-mactivate="${i}" title="activate">${ICON_BOLT}</button>
          ${showJiraBtn ? `<button class="btn mini btn-icon" data-act="jira" data-i="${i}" title="create Jira ticket">${ICON_BOOKMARK_ADD}</button>` : ""}
          <span class="pt-btn-gap"></span>
          <button class="btn mini btn-icon" data-act="up" data-i="${i}" title="move up">${ICON_ARROW_UPWARD}</button>
          <button class="btn mini btn-icon" data-act="down" data-i="${i}" title="move down">${ICON_ARROW_DOWNWARD}</button>
          <button class="btn mini btn-icon btn-x" data-act="del" data-i="${i}" ${dis} title="delete point">${ICON_CLOSE}</button>
        </div>
      </div>
      <div class="pt-main">
        <div><label>Action</label><textarea data-i="${i}" data-f="action" ${dis}>${esc(p.action)}</textarea></div>
        <div><label>Expected Result</label><textarea data-i="${i}" data-f="expected" ${dis}>${esc(p.expected)}</textarea></div>
        <div><label>Observed Result</label><textarea data-i="${i}" data-f="observed">${esc(p.observed)}</textarea></div>
        <div><label>Reference</label><textarea data-i="${i}" data-f="reference" ${dis}>${esc(p.reference)}</textarea></div>
      </div>
      <div class="pt-sign">
        <div><label>Responsible Party</label><input data-i="${i}" data-f="responsible_party" value="${esc(p.responsible_party)}" ${dis}></div>
        <div><label>Date</label><input data-i="${i}" data-f="date" value="${esc(p.date)}"></div>
        <div><label>Time (24h)</label>
          <div class="time-row">
            <input data-i="${i}" data-f="time" value="${esc(p.time)}">
            <button class="btn mini" data-now="${i}">now</button>
          </div>
        </div>
        <div><label>Initial</label><input data-i="${i}" data-f="initial" value="${esc(p.initial)}"></div>
        <div><label>Quality</label>${qualitySelect(i, p.quality)}</div>
      </div>
      <div class="pt-expand-row">
        <div class="pt-camera" id="pt-camera-${i}"></div>
        <div class="pt-measure" id="pt-measure-${i}">${(measureOpenIndices.has(i) || p.measurements.length > 0) ? measurePanelHtml(i, p) : ""}</div>
      </div>
      <div class="pt-images" id="pt-images-${i}">${imgChips}</div>
      <div class="pt-cam-msg" id="pt-cam-msg-${i}"></div>
      <div class="pt-cam-msg" id="pt-jira-msg-${i}"></div>`;
    list.appendChild(div);
  });
  reattachPreviewIfOpen();
  list.querySelectorAll("input[data-f], textarea[data-f], select[data-f]").forEach(el => {
    el.addEventListener("input", () => {
      card.points[+el.dataset.i][el.dataset.f] = el.value;
    });
  });
  list.querySelectorAll("select[data-f]").forEach(el => {
    el.addEventListener("change", () => {
      card.points[+el.dataset.i][el.dataset.f] = el.value;
      // Quality change needs a re-render: the Jira ticket button's
      // visibility is derived from it, not just displayed.
      if (el.dataset.f === "quality") renderPoints();
    });
  });
  list.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i, act = btn.dataset.act;
      if (act === "del") {
        if (card.points[i].locked) return;   // locked points can't be removed
        card.points.splice(i, 1);
      }
      if (act === "up" && i > 0) [card.points[i-1], card.points[i]] = [card.points[i], card.points[i-1]];
      if (act === "down" && i < card.points.length - 1) [card.points[i+1], card.points[i]] = [card.points[i], card.points[i+1]];
      if (act === "flag") {
        card.points[i].flagged = !card.points[i].flagged;   // works regardless of locked
      }
      if (act === "capture") { capturePointImage(i); return; }
      if (act === "preview") { togglePreview(i); return; }
      if (act === "jira") { createJiraTicket(i); return; }
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-mread]").forEach(btn => {
    btn.addEventListener("click", () => readMeasurement(+btn.dataset.mread));
  });
  list.querySelectorAll("button[data-mactivate]").forEach(btn => {
    btn.addEventListener("click", () => activateHardware(+btn.dataset.mactivate));
  });
  list.querySelectorAll("button[data-madd]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.madd;
      if (card.points[i].measurements.length < 8) {
        card.points[i].measurements.push({command: "", value: "", timestamp: "", auto: false});
        renderPoints();
      }
    });
  });
  list.querySelectorAll("button[data-mdel]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i;
      card.points[i].measurements.splice(+btn.dataset.mdel, 1);
      renderPoints();
    });
  });
  list.querySelectorAll("input[data-mi]").forEach(el => {
    el.addEventListener("input", () => {
      card.points[+el.dataset.mi].measurements[+el.dataset.mj][el.dataset.mf] = el.value;
    });
  });
  list.querySelectorAll("button[data-mnow]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i, j = +btn.dataset.mnow;
      const now = new Date();
      card.points[i].measurements[j].timestamp = now.toISOString().slice(0, 16).replace("T", " ");
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-rm-img]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.i, fn = btn.dataset.rmImg;
      // Detaches the filename from this point only -- the .jpg file
      // itself is left on disk, not deleted.
      card.points[i].images = (card.points[i].images || []).filter(x => x !== fn);
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-now]").forEach(btn => {
    btn.addEventListener("click", () => {
      const i = +btn.dataset.now, d = new Date(), pad = n => String(n).padStart(2, "0");
      card.points[i].date = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
      card.points[i].time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;   // 24h
      renderPoints();
    });
  });
}

function esc(s) { return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

document.getElementById("addPointBtn").addEventListener("click", () => {
  card.points.push({seq: nextSeq(), action:"", expected:"", observed:"", reference:"",
                     responsible_party:"", date:"", time:"", initial:"", quality:"",
                     locked: false, flagged: false, images: [],
                     usb_port: "", measure_command: "", activate_command: "", measurements: []});
  renderPoints();
});

function renderNomenclature() {
  const t = document.getElementById("nomTable");
  t.querySelectorAll("tr.nomrow").forEach(r => r.remove());
  card.nomenclature.forEach(([sym, defn, units, locked], i) => {
    const dis = locked ? "disabled" : "";
    const tr = document.createElement("tr");
    tr.className = "nomrow";
    tr.innerHTML = `
      <td><input value="${esc(sym)}" data-i="${i}" data-f="0" placeholder="\\\\alpha" ${dis}></td>
      <td><input value="${esc(defn)}" data-i="${i}" data-f="1" placeholder="definition" ${dis}></td>
      <td><input value="${esc(units||"")}" data-i="${i}" data-f="2" placeholder="units" ${dis}></td>
      <td><button class="btn mini btn-x" data-del="${i}" ${dis}>x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => {
      card.nomenclature[+inp.dataset.i][+inp.dataset.f] = inp.value;
    });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card.nomenclature[+btn.dataset.del][3]) return;   // locked row
      card.nomenclature.splice(+btn.dataset.del, 1);
      renderNomenclature();
    });
  });
}
document.getElementById("addNomBtn").addEventListener("click", () => {
  card.nomenclature.push(["", "", "", false]);
  renderNomenclature();
});

function renderReferences() {
  const t = document.getElementById("refTable");
  t.querySelectorAll("tr.refrow").forEach(r => r.remove());
  card.references.forEach(([ref, desc, locked], i) => {
    const dis = locked ? "disabled" : "";
    const tr = document.createElement("tr");
    tr.className = "refrow";
    tr.innerHTML = `
      <td><input value="${esc(ref)}" data-i="${i}" data-f="0" placeholder="REQ-1234" ${dis}></td>
      <td><input value="${esc(desc)}" data-i="${i}" data-f="1" placeholder="description" ${dis}></td>
      <td><button class="btn mini btn-x" data-del="${i}" ${dis}>x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => {
      card.references[+inp.dataset.i][+inp.dataset.f] = inp.value;
    });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card.references[+btn.dataset.del][2]) return;   // locked row
      card.references.splice(+btn.dataset.del, 1);
      renderReferences();
    });
  });
}
document.getElementById("addRefBtn").addEventListener("click", () => {
  card.references.push(["", "", false]);
  renderReferences();
});

function renderTextList(listId, key) {
  const el = document.getElementById(listId);
  el.innerHTML = "";
  card[key].forEach(([val, locked], i) => {
    const dis = locked ? "disabled" : "";
    const row = document.createElement("div");
    row.className = "list-row";
    row.innerHTML = `<textarea data-key="${key}" data-i="${i}" ${dis}>${esc(val)}</textarea>
      <button class="btn mini btn-x" data-key="${key}" data-del="${i}" ${dis}>x</button>`;
    el.appendChild(row);
  });
  el.querySelectorAll("textarea[data-key]").forEach(ta => {
    ta.addEventListener("input", () => { card[ta.dataset.key][+ta.dataset.i][0] = ta.value; });
  });
  el.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => {
      if (card[btn.dataset.key][+btn.dataset.del][1]) return;   // locked row
      card[btn.dataset.key].splice(+btn.dataset.del, 1);
      renderTextList(listId, btn.dataset.key);
    });
  });
}
document.getElementById("addNoteBtn").addEventListener("click", () => {
  card.notes.push(["", false]); renderTextList("notesList", "notes");
});
document.getElementById("addWarningBtn").addEventListener("click", () => {
  card.warnings.push(["", false]); renderTextList("warningsList", "warnings");
});

function download(filename, blobOrText, mime) {
  const blob = blobOrText instanceof Blob ? blobOrText : new Blob([blobOrText], {type: mime || "text/plain"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}

function freezeCurrentCard() {
  card.locked = true;
  card.points.forEach(p => { p.locked = true; });
  card.nomenclature.forEach(row => { row[3] = true; });
  card.references.forEach(row => { row[2] = true; });
  card.notes.forEach(row => { row[1] = true; });
  card.warnings.forEach(row => { row[1] = true; });
}
function unfreezeCard() {
  card.locked = false;
  card.points.forEach(p => { p.locked = false; });
  card.nomenclature.forEach(row => { row[3] = false; });
  card.references.forEach(row => { row[2] = false; });
  card.notes.forEach(row => { row[1] = false; });
  card.warnings.forEach(row => { row[1] = false; });
}
function rerenderAll() {
  bindMeta(); renderPoints(); renderNomenclature(); renderReferences();
  renderTextList("notesList", "notes"); renderTextList("warningsList", "warnings");
}
async function saveCard(statusEl) {
  statusEl.textContent = "saving...";
  try {
    const res = await fetch("/save", {method: "POST", body: JSON.stringify(card)});
    const data = await res.json();
    statusEl.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
  } catch (err) {
    statusEl.textContent = `Save failed: ${err}`;
  }
}
document.getElementById("saveLockedBtn").addEventListener("click", () => {
  // Locking is a workflow marker, not security. Freezing happens
  // client-side so the editor reflects it immediately, not just the file.
  freezeCurrentCard();
  rerenderAll();
  saveCard(document.getElementById("saveStatus"));
});
document.getElementById("saveUnlockedBtn").addEventListener("click", () => {
  unfreezeCard();
  rerenderAll();
  saveCard(document.getElementById("saveStatus"));
});
document.getElementById("loadJsonBtn").addEventListener("click", () => {
  document.getElementById("loadJsonFile").click();
});
function normalizePairList(items, width) {
  // Pads each row to `width` (the last element the locked flag); accepts
  // a bare string for a single-element row (pre-locking notes/warnings).
  return (items || []).map(item => {
    let row = typeof item === "string" ? [item] : item.slice();
    while (row.length < width - 1) row.push("");
    if (row.length < width) row.push(false);
    return row.slice(0, width);
  });
}
function normalizeCard(c) {
  c.nomenclature = normalizePairList(c.nomenclature, 4);
  c.references = normalizePairList(c.references, 3);
  c.notes = normalizePairList(c.notes, 2);
  c.warnings = normalizePairList(c.warnings, 2);
  if (!c.quality_assurance) c.quality_assurance = "";
  if (!c.requirements) c.requirements = "";
  if (!c.locked) c.locked = false;
  (c.points || []).forEach(p => {
    if (!("locked" in p)) p.locked = false;
    if (!("flagged" in p)) p.flagged = false;
    if (!p.images) p.images = [];
    if (!("observed" in p)) p.observed = "";
    if (!("usb_port" in p)) p.usb_port = "";
    if (!("measure_command" in p)) p.measure_command = "";
    if (!("activate_command" in p)) p.activate_command = "";
    if (!p.measurements) p.measurements = [];
  });
  return c;
}
function loadCardIntoUI(c) {
  card = normalizeCard(c);
  bindMeta(); renderPoints(); renderNomenclature(); renderReferences();
  renderTextList("notesList", "notes"); renderTextList("warningsList", "warnings");
}
document.getElementById("loadJsonFile").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(txt => loadCardIntoUI(JSON.parse(txt)));
});

async function postExport(path, filename, mime) {
  // Open the print window synchronously, before the await below -- many
  // popup blockers kill a window.open() called after a fetch() resolves.
  let w = null;
  if (path === "/export/print") {
    w = window.open("", "_blank");
    if (w) w.document.write("Generating...");
  }
  try {
    const res = await fetch(path, {method: "POST", body: JSON.stringify(card)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (path === "/export/print") {
      if (w) { w.document.open(); w.document.write(text); w.document.close(); }
    } else {
      download(filename, text, mime);
    }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
    document.getElementById("saveStatus").textContent = `Export failed: ${err}`;
  }
}
document.getElementById("exportCsvBtn").addEventListener("click", () =>
  postExport("/export/csv", (card.id || "card") + ".csv", "text/csv"));
document.getElementById("exportPrintBtn").addEventListener("click", () =>
  postExport("/export/print"));
document.getElementById("exportDocxBtn").addEventListener("click", async () => {
  try {
    const res = await fetch("/export/test/docx", {method: "POST", body: JSON.stringify(card)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    download((card.id || "test") + ".docx", await res.blob());
  } catch (err) {
    document.getElementById("saveStatus").textContent = `Docx export failed: ${err}`;
  }
});

bindMeta();
card.points = [{seq:10, action:"", expected:"", observed:"", reference:"",
                responsible_party:"", date:"", time:"", initial:"", quality:"",
                locked:false, flagged:false, images:[],
                usb_port:"", measure_command:"", activate_command:"", measurements:[]}];
renderPoints();
renderNomenclature();
renderReferences();
renderTextList("notesList", "notes");
renderTextList("warningsList", "warnings");

// If opened as ?load=<filename>, fetch that .test file and populate the
// editor with it -- how the campaign page opens a specific test in a tab.
const _loadFile = new URLSearchParams(window.location.search).get("load");
if (_loadFile) {
  fetch("/load?file=" + encodeURIComponent(_loadFile))
    .then(r => { if (!r.ok) throw new Error("not found"); return r.json(); })
    .then(loadCardIntoUI)
    .catch(() => { document.getElementById("m-id").value = _loadFile.replace(/\\.test$/, ""); });
}

// Lets an embedding page (the campaign tab bar) read the live editor
// state, since `card` is a module-scope `let` otherwise unreachable from an iframe.
window.getCard = () => card;
</script>
</body>
</html>
"""



_CAMPAIGN_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>\U0001F52D STARFIRE</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/starfire.css">
</head>
<body class="editor-doc">
<div id="tabbar"></div>
<div id="panels">
  <div class="panel active" id="panel-campaign">
    <h1>STARFIRE</h1>
    <div class="sub">Test Campaign Designer</div>

    <h2>Campaign metadata</h2>
    <div class="meta">
      <div class="field"><label>ID</label><input id="c-id"></div>
      <div class="field"><label>Title</label><input id="c-title"></div>
      <div class="field"><label>Revision</label><input id="c-revision"></div>
      <div class="field"><label>Project</label><input id="c-project"></div>
      <div class="field"><label>Author</label><input id="c-author"></div>
      <div class="field"><label>Test Lead</label><input id="c-test_lead"></div>
      <div class="field"><label>Quality Assurance</label><input id="c-quality_assurance"></div>
      <div class="field"><label>Date</label><input id="c-date"></div>
      <div class="field"><label>System / Subsystem</label><input id="c-system"></div>
      <div class="field" id="jira-epic-field" style="display:none"><label>Jira Epic Key</label><input id="c-jira_epic_key" placeholder="PROJ-142"></div>
    </div>

    <h2 class="no-rule">Requirements</h2>
    <table class="datatable" id="reqTable">
      <tr><th>Requirement</th><th>Description</th><th class="blank-th" style="width:60px"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addReqBtn" title="add requirement">+</button></div>

    <h2 class="no-rule">Test Series</h2>
    <table class="datatable tests-format" id="testsTable">
      <colgroup>
      <col style="width:4%"><col style="width:10%"><col style="width:33%">
      <col style="width:12%"><col style="width:12%"><col style="width:9%"><col style="width:20%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Author</th><th>Requirements</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addTestBtn" title="add test">+</button></div>
    <div class="row">
      <select id="browseTestFiles"><option value="">Browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedTestBtn">select</button>
    </div>
    <div id="browseStatus" class="hint"></div>

    <h2 class="no-rule">Ancillary Tests</h2>
    <table class="datatable tests-format" id="ancTestsTable">
      <colgroup>
      <col style="width:4%"><col style="width:10%"><col style="width:33%">
      <col style="width:12%"><col style="width:12%"><col style="width:9%"><col style="width:20%">
      </colgroup>
      <tr><th>#</th><th>Test ID</th><th>Title</th><th>Author</th><th>Requirements</th><th>Status</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addAncTestBtn" title="add test">+</button></div>
    <div class="row">
      <select id="browseAncTestFiles"><option value="">Browse existing .test files...</option></select>
      <button class="btn" id="addBrowsedAncTestBtn">select</button>
    </div>
    <div id="ancBrowseStatus" class="hint"></div>

    <h2 class="no-rule">Sections</h2>
    <div id="sectionsList"></div>
    <div class="row"><button class="btn btn-plus" id="addSectionBtn" title="add section">+</button></div>

    <h2>Save / load / export</h2>
    <div class="row">
      <button class="btn" id="loadCampBtn">import campaign file</button>
      <input type="file" id="loadCampFile" accept=".camp,application/json" style="display:none">
      <button class="btn" id="saveCampBtn">export campaign file (.camp)</button>
      <button class="btn" id="printCampBtn">generate .pdf</button>
      <button class="btn" id="docxCampBtn">export .docx</button>
      <button class="btn" id="genVcrmBtn">generate VCRM</button>
    </div>
    <div id="saveStatus"></div>
  </div>
</div>

<script>
let camp = {
  id: "SC-001", title: "Untitled Campaign", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_lead: "",
  quality_assurance: "", system: "", subsystem: "", jira_epic_key: "",
  requirements: [], tests: [], ancillary_tests: [], sections: []
};

// True only if jira_config.json was found in the working directory at startup.
const JIRA_AVAILABLE = __JIRA_AVAILABLE__;
if (JIRA_AVAILABLE) document.getElementById("jira-epic-field").style.display = "";

function esc(s) { return (s??"").toString().replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

const metaFields = ["id","title","revision","author","date","project","test_lead","quality_assurance","jira_epic_key"];
function bindMeta() {
  metaFields.forEach(f => {
    const el = document.getElementById("c-" + f);
    el.value = camp[f];
    el.addEventListener("input", () => {
      camp[f] = el.value;
      if (f === "id" || f === "title") renderTabBar();
    });
  });
  const sysEl = document.getElementById("c-system");
  sysEl.value = [camp.system, camp.subsystem].filter(Boolean).join(" / ");
  sysEl.addEventListener("input", () => {
    const parts = sysEl.value.split("/").map(s => s.trim());
    camp.system = parts[0] || ""; camp.subsystem = parts[1] || "";
  });
}

function renderRequirements() {
  const t = document.getElementById("reqTable");
  t.querySelectorAll("tr.datarow").forEach(r => r.remove());
  camp.requirements.forEach(([code, desc], i) => {
    const tr = document.createElement("tr");
    tr.className = "datarow";
    tr.innerHTML = `
      <td><input value="${esc(code)}" data-i="${i}" data-f="0" placeholder="REQ-1234"></td>
      <td><input value="${esc(desc)}" data-i="${i}" data-f="1" placeholder="description"></td>
      <td><button class="btn mini btn-x" data-del="${i}">x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i]").forEach(inp => {
    inp.addEventListener("input", () => { camp.requirements[+inp.dataset.i][+inp.dataset.f] = inp.value; });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { camp.requirements.splice(+btn.dataset.del, 1); renderRequirements(); });
  });
}
document.getElementById("addReqBtn").addEventListener("click", () => {
  camp.requirements.push(["", ""]);
  renderRequirements();
});

// Google Material Symbols, embedded inline (not a file/CDN) so they work
// fully offline; fill="currentColor" picks up each button's own hover color.
const ICON_OPEN_IN_NEW = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h280v80H200v560h560v-280h80v280q0 33-23.5 56.5T760-120H200Zm188-212-56-56 372-372H560v-80h280v280h-80v-144L388-332Z"/></svg>';
const ICON_CLOSE = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z"/></svg>';
const ICON_BOOKMARK_ADD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M200-120v-640q0-33 23.5-56.5T280-840h240v80H280v518l200-86 200 86v-278h80v400L480-240 200-120Zm80-640h240-240Zm400 160v-80h-80v-80h80v-80h80v80h80v80h-80v80h-80Z"/></svg>';

const STATUS_OPTIONS = [
  ["", "[None]"],
  ["COMPLETE", "\U0001F535 COMPLETE"],
  ["IN_PROGRESS", "\U0001F7E2 IN PROGRESS"],
  // No native colored-circle emoji is genuinely magenta -- purple is the
  // closest a real Unicode circle gets.
  ["TO_DO", "\U0001F7E3 TO DO"],
  ["BLOCKED", "\U0001F534 BLOCKED"],
];
// Plain-text labels (no colored dot -- that's UI-only, never shown in the
// VCRM or any export), matching STATUS_LABELS in starfire.py.
const STATUS_LABELS = {
  COMPLETE: "COMPLETE", IN_PROGRESS: "IN PROGRESS", TO_DO: "TO DO", BLOCKED: "BLOCKED",
};
function statusSelect(i, current) {
  const opts = STATUS_OPTIONS.map(([v, label]) =>
    `<option value="${v}"${v === current ? " selected" : ""}>${label}</option>`).join("");
  return `<select data-i="${i}" data-f="status">${opts}</select>`;
}

function nextSeqFor(list) {
  const max = list.reduce((m, t) => Math.max(m, +t.seq || 0), 0);
  return max + 1;   // campaigns number tests 1, 2, 3... not by 10s
}

function renderTestsTable(list, tableId) {
  const t = document.getElementById(tableId);
  t.querySelectorAll("tr.datarow").forEach(r => r.remove());
  list.forEach((row, i) => {
    const tr = document.createElement("tr");
    tr.className = "datarow";
    tr.innerHTML = `
      <td><input value="${esc(row.seq)}" data-i="${i}" data-f="seq"></td>
      <td><input value="${esc(row.test_id)}" data-i="${i}" data-f="test_id" placeholder="TC-001"></td>
      <td class="meta-cell">${esc(row.title || "")}</td>
      <td class="meta-cell">${esc(row.author || "")}</td>
      <td class="meta-cell">${esc(row.requirements || "")}</td>
      <td>${statusSelect(i, row.status)}</td>
      <td class="actions-cell">
        <button class="btn btn-icon" data-open="${i}" title="open">${ICON_OPEN_IN_NEW}</button>
        <button class="btn btn-icon" data-del="${i}" title="remove">${ICON_CLOSE}</button>
        ${JIRA_AVAILABLE ? `<button class="btn btn-icon" data-jira="${i}" title="create Jira ticket">${ICON_BOOKMARK_ADD}</button>` : ""}
      </td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-f], select[data-f]").forEach(el => {
    const ev = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => {
      list[+el.dataset.i][el.dataset.f] = el.value;
    });
  });
  // Title/Author/Requirements are pulled from the linked .test file --
  // refresh on blur (not every keystroke) so focus isn't yanked mid-type.
  t.querySelectorAll('input[data-f="test_id"]').forEach(el => {
    el.addEventListener("blur", () => backfillMetadataForRow(list, tableId, +el.dataset.i));
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { list.splice(+btn.dataset.del, 1); renderTestsTable(list, tableId); });
  });
  t.querySelectorAll("button[data-open]").forEach(btn => {
    btn.addEventListener("click", () => {
      const row = list[+btn.dataset.open];
      if ((row.test_id || "").trim()) openTestTab(row.test_id, row.title);
    });
  });
  t.querySelectorAll("button[data-jira]").forEach(btn => {
    btn.addEventListener("click", () => createTestJiraTicket(list, tableId, +btn.dataset.jira));
  });
}
function renderTests() { renderTestsTable(camp.tests, "testsTable"); }
function renderAncillaryTests() { renderTestsTable(camp.ancillary_tests, "ancTestsTable"); }

// Reuses the browse-picker's own status line for ticket-click results,
// rather than adding a third message area -- the two actions never overlap.
const TICKET_STATUS_EL_FOR_TABLE = { testsTable: "browseStatus", ancTestsTable: "ancBrowseStatus" };

async function createTestJiraTicket(list, tableId, i) {
  const row = list[i];
  const statusEl = document.getElementById(TICKET_STATUS_EL_FOR_TABLE[tableId]);
  const summary = `${row.test_id || "(no ID)"}: ${row.title || "(no title)"}`;
  const description = [
    `Test: ${row.test_id || ""} -- ${row.title || ""}`,
    `Status: ${STATUS_LABELS[row.status] || row.status || "[None]"}`,
    `Requirement(s): ${row.requirements || ""}`,
  ].join("\\n");
  statusEl.textContent = "Creating ticket...";
  try {
    const res = await fetch("/create_jira_ticket", {
      method: "POST",
      body: JSON.stringify({
        summary, description, issue_type: "Task",
        epic_key: camp.jira_epic_key || "",
      }),
    });
    const result = await res.json();
    if (result.success) {
      statusEl.innerHTML = `Created <a href="${esc(result.url)}" target="_blank" rel="noopener">${esc(result.key)}</a>`;
    } else {
      statusEl.textContent = `Ticket creation failed: ${result.error || "unknown error"}`;
    }
  } catch (err) {
    statusEl.textContent = `Ticket creation failed: ${err}`;
  }
}

async function backfillMetadataForRow(list, tableId, i) {
  const row = list[i];
  const testId = (row.test_id || "").trim();
  if (!testId) return;
  const filename = testId.replace(/[^A-Za-z0-9._-]/g, "_") + ".test";
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (!res.ok) return;   // no matching file yet -- leave title/author/requirements as-is, silently
    const data = await res.json();
    row.title = data.title || "";
    row.author = data.author || "";
    row.requirements = data.requirements || "";
    renderTestsTable(list, tableId);
  } catch (err) {
    // transient error -- same "silent, no validation" spirit as the rest
    // of this traceability; nothing here is load-bearing.
  }
}

document.getElementById("addTestBtn").addEventListener("click", () => {
  camp.tests.push({seq: nextSeqFor(camp.tests), test_id: "", title: "", author: "", requirements: "", status: ""});
  renderTests();
});
document.getElementById("addAncTestBtn").addEventListener("click", () => {
  camp.ancillary_tests.push({seq: nextSeqFor(camp.ancillary_tests), test_id: "", title: "", author: "", requirements: "", status: ""});
  renderAncillaryTests();
});

// Browse .test files already on disk, rather than requiring the exact
// Test ID up front -- for a file created earlier by this person or a teammate.
async function refreshFileList(selectId) {
  const sel = document.getElementById(selectId);
  const current = sel.value;
  try {
    const res = await fetch("/list_files?ext=.test");
    const data = await res.json();
    sel.innerHTML = '<option value="">Browse existing .test files...</option>' +
      (data.files || []).map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
    if ((data.files || []).includes(current)) sel.value = current;
  } catch (err) {
    document.getElementById(selectId === "browseTestFiles" ? "browseStatus" : "ancBrowseStatus")
      .textContent = `Couldn't list files: ${err}`;
  }
}
async function addBrowsedFile(selectId, list, tableId, statusId) {
  const sel = document.getElementById(selectId);
  const filename = sel.value;
  const status = document.getElementById(statusId);
  if (!filename) return;
  const testId = filename.replace(/\\.test$/, "");
  if (list.some(t => t.test_id === testId)) {
    status.textContent = `${testId} is already in this table.`;
    return;
  }
  // Grab title/author/requirements too, if we can -- not fatal if this
  // fails, the row just gets added with those left blank to fill in by hand.
  let title = "", author = "", requirements = "";
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (res.ok) {
      const data = await res.json();
      title = data.title || ""; author = data.author || ""; requirements = data.requirements || "";
    }
  } catch (err) { /* leave title/author/requirements blank */ }
  list.push({seq: nextSeqFor(list), test_id: testId, title: title, author: author,
             requirements: requirements, status: ""});
  renderTestsTable(list, tableId);
}
document.getElementById("browseTestFiles").addEventListener("focus", () => refreshFileList("browseTestFiles"));
document.getElementById("addBrowsedTestBtn").addEventListener("click", () =>
  addBrowsedFile("browseTestFiles", camp.tests, "testsTable", "browseStatus"));
document.getElementById("browseAncTestFiles").addEventListener("focus", () => refreshFileList("browseAncTestFiles"));
document.getElementById("addBrowsedAncTestBtn").addEventListener("click", () =>
  addBrowsedFile("browseAncTestFiles", camp.ancillary_tests, "ancTestsTable", "ancBrowseStatus"));
refreshFileList("browseTestFiles");
refreshFileList("browseAncTestFiles");

function renderSections() {
  const list = document.getElementById("sectionsList");
  list.innerHTML = "";
  camp.sections.forEach(([header, text], i) => {
    const div = document.createElement("div");
    div.className = "sec-card";
    div.innerHTML = `
      <div class="sec-head">
        <input value="${esc(header)}" data-i="${i}" data-f="0" placeholder="Section header">
        <button class="btn mini btn-x" data-del="${i}">x</button>
      </div>
      <textarea data-i="${i}" data-f="1" placeholder="Write or paste anything here">${esc(text)}</textarea>`;
    list.appendChild(div);
  });
  list.querySelectorAll("input[data-i], textarea[data-i]").forEach(el => {
    el.addEventListener("input", () => { camp.sections[+el.dataset.i][+el.dataset.f] = el.value; });
  });
  list.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { camp.sections.splice(+btn.dataset.del, 1); renderSections(); });
  });
}
document.getElementById("addSectionBtn").addEventListener("click", () => {
  camp.sections.push(["", ""]);
  renderSections();
});

function download(filename, blobOrText, mime) {
  const blob = blobOrText instanceof Blob ? blobOrText : new Blob([blobOrText], {type: mime || "text/plain"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}

document.getElementById("saveCampBtn").addEventListener("click", async () => {
  const status = document.getElementById("saveStatus");
  status.textContent = "saving...";
  try {
    const res = await fetch("/save_campaign", {method: "POST", body: JSON.stringify(camp)});
    const data = await res.json();
    status.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
  } catch (err) {
    status.textContent = `Save failed: ${err}`;
  }
});
document.getElementById("loadCampBtn").addEventListener("click", () => {
  document.getElementById("loadCampFile").click();
});
document.getElementById("loadCampFile").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(txt => {
    camp = JSON.parse(txt);
    if (!camp.requirements) camp.requirements = [];
    if (!camp.tests) camp.tests = [];
    if (!camp.ancillary_tests) camp.ancillary_tests = [];
    if (!camp.sections) camp.sections = [];
    if (!camp.jira_epic_key) camp.jira_epic_key = "";
    bindMeta(); renderRequirements(); renderTests(); renderAncillaryTests(); renderSections();
  });
});
document.getElementById("printCampBtn").addEventListener("click", async () => {
  // Open synchronously, before the await below -- popup blockers can kill
  // a window.open() called after a fetch() resolves.
  const w = window.open("", "_blank");
  if (w) w.document.write("Generating...");
  try {
    const res = await fetch("/export/campaign/print", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (w) { w.document.open(); w.document.write(text); w.document.close(); }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
    document.getElementById("saveStatus").textContent = `Print failed: ${err}`;
  }
});
document.getElementById("docxCampBtn").addEventListener("click", async () => {
  const status = document.getElementById("saveStatus");
  try {
    const res = await fetch("/export/campaign/docx", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const blob = await res.blob();
    download((camp.id || "campaign") + ".docx", blob);
  } catch (err) {
    status.textContent = `Docx export failed: ${err}`;
  }
});

// ---- Tab bar ----
let openTabs = [];   // [{key, label}], key = filename for test tabs
let activeTab = "campaign";

function renderTabBar() {
  const bar = document.getElementById("tabbar");
  bar.innerHTML = "";
  const campBtn = document.createElement("button");
  campBtn.className = "tab-btn" + (activeTab === "campaign" ? " active" : "");
  campBtn.textContent = camp.id || "Campaign";
  campBtn.addEventListener("click", () => switchTab("campaign"));
  bar.appendChild(campBtn);
  openTabs.forEach(tab => {
    const btn = document.createElement("button");
    btn.className = "tab-btn" + (activeTab === tab.key ? " active" : "");
    btn.innerHTML = `<span>${esc(tab.label || tab.key)}</span><span class="close-x" data-close="${esc(tab.key)}">x</span>`;
    btn.addEventListener("click", (e) => {
      if (e.target.dataset.close) { closeTestTab(e.target.dataset.close); return; }
      switchTab(tab.key);
    });
    bar.appendChild(btn);
  });
}

function switchTab(key) {
  activeTab = key;
  document.getElementById("panel-campaign").classList.toggle("active", key === "campaign");
  openTabs.forEach(tab => {
    const panel = document.getElementById("panel-" + tab.key);
    if (panel) panel.classList.toggle("active", key === tab.key);
  });
  renderTabBar();
}

function openTestTab(testId, title) {
  const filename = testId.replace(/[^A-Za-z0-9._-]/g, "_") + ".test";
  if (!openTabs.some(t => t.key === filename)) {
    openTabs.push({key: filename, label: testId});
    const panel = document.createElement("div");
    panel.className = "test-panel";
    panel.id = "panel-" + filename;
    panel.innerHTML = `<iframe src="/test?load=${encodeURIComponent(filename)}"></iframe>`;
    document.getElementById("panels").appendChild(panel);
    const iframe = panel.querySelector("iframe");
    iframe.addEventListener("load", () => {
      // The editor's own ?load= fetch is async and typically resolves
      // after this "load" event fires, so retry briefly rather than assume it's ready.
      pollForLockIcon(iframe, filename, 10);
      // The epic key is a plain variable the editor sets synchronously at
      // script top, so it's already there by the time "load" fires -- no retry needed.
      if (iframe.contentWindow) {
        iframe.contentWindow.starfireJiraEpicKey = camp.jira_epic_key || "";
      }
    });
  }
  switchTab(filename);
}

function pollForLockIcon(iframe, filename, attemptsLeft) {
  if (attemptsLeft <= 0) return;
  const iCard = iframe.contentWindow.getCard ? iframe.contentWindow.getCard() : null;
  if (iCard && iCard.locked) {
    const tab = openTabs.find(t => t.key === filename);
    const lockIcon = "\U0001F512";
    if (tab && !tab.label.startsWith(lockIcon)) {
      tab.label = lockIcon + " " + tab.label;
      renderTabBar();
    }
    return;
  }
  setTimeout(() => pollForLockIcon(iframe, filename, attemptsLeft - 1), 200);
}

function closeTestTab(key) {
  openTabs = openTabs.filter(t => t.key !== key);
  const panel = document.getElementById("panel-" + key);
  if (panel) panel.remove();
  if (activeTab === key) switchTab("campaign");
  else renderTabBar();
}

// A single reserved tab, regenerated in place rather than duplicated on
// repeat clicks. Read-only: shows a computed snapshot, not edited in the UI.
const VCRM_KEY = "__vcrm__";
let currentVcrm = null;

function vcrmCoveringStr(row) {
  if (!row.covering_tests.length) return "--";
  return row.covering_tests.map(c => `${c.test_id} (${STATUS_LABELS[c.status] || c.status || "[None]"})`).join(", ");
}

async function generateVcrm() {
  const status = document.getElementById("saveStatus");
  try {
    const res = await fetch("/vcrm", {method: "POST", body: JSON.stringify(camp)});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    currentVcrm = await res.json();
  } catch (err) {
    status.textContent = `VCRM generation failed: ${err}`;
    return;
  }

  if (!openTabs.some(t => t.key === VCRM_KEY)) {
    openTabs.push({key: VCRM_KEY, label: "VCRM"});
  }
  let panel = document.getElementById("panel-" + VCRM_KEY);
  if (!panel) {
    panel = document.createElement("div");
    panel.className = "panel";
    panel.id = "panel-" + VCRM_KEY;
    document.getElementById("panels").appendChild(panel);
  }
  const rows = currentVcrm.rows.map(r => `
    <tr>
      <td>${esc(r.requirement)}</td>
      <td>${esc(r.description)}</td>
      <td>${esc(vcrmCoveringStr(r))}</td>
      <td>${esc(r.coverage)}</td>
      <td>${esc(currentVcrm.generated)}</td>
    </tr>`).join("");
  panel.innerHTML = `
    <h1 style="visibility:hidden">STARFIRE</h1>
    <div class="sub" style="visibility:hidden">Test Campaign Designer</div>
    <h2 class="no-rule">Verification Cross-Reference Matrix</h2>
    <table class="datatable">
      <tr><th>Requirement</th><th>Description</th><th>Covering Tests</th><th>Status</th><th>Updated</th></tr>
      ${rows || '<tr><td colspan="5">No requirements entered on this campaign yet.</td></tr>'}
    </table>
    <div class="row">
      <button class="btn" id="saveVcrmBtn">export .vcrm file</button>
      <button class="btn" id="printVcrmBtn">generate .pdf</button>
      <button class="btn" id="docxVcrmBtn">export .docx</button>
    </div>
    <div id="vcrmStatus" class="hint"></div>`;

  const vcrmStatus = panel.querySelector("#vcrmStatus");
  panel.querySelector("#saveVcrmBtn").addEventListener("click", async () => {
    try {
      const res = await fetch("/save_vcrm", {method: "POST", body: JSON.stringify(currentVcrm)});
      const data = await res.json();
      vcrmStatus.textContent = data.saved ? `Saved to ${data.path}` : `Save failed: ${data.error || "unknown error"}`;
    } catch (err) {
      vcrmStatus.textContent = `Save failed: ${err}`;
    }
  });
  panel.querySelector("#printVcrmBtn").addEventListener("click", async () => {
    // Open synchronously, before the await below -- see the same note on
    // printCampBtn above for why.
    const w = window.open("", "_blank");
    if (w) w.document.write("Generating...");
    try {
      const res = await fetch("/export/vcrm/print", {method: "POST", body: JSON.stringify(currentVcrm)});
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const text = await res.text();
      if (w) { w.document.open(); w.document.write(text); w.document.close(); }
    } catch (err) {
      if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
      vcrmStatus.textContent = `Print failed: ${err}`;
    }
  });
  panel.querySelector("#docxVcrmBtn").addEventListener("click", async () => {
    try {
      const res = await fetch("/export/vcrm/docx", {method: "POST", body: JSON.stringify(currentVcrm)});
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      const blob = await res.blob();
      download((currentVcrm.campaign_id || "vcrm") + ".docx", blob);
    } catch (err) {
      vcrmStatus.textContent = `Docx export failed: ${err}`;
    }
  });

  switchTab(VCRM_KEY);
}
document.getElementById("genVcrmBtn").addEventListener("click", generateVcrm);

bindMeta();
camp.tests = [{seq: 1, test_id: "", title: "", author: "", requirements: "", status: ""}];
renderRequirements();
renderTests();
renderAncillaryTests();
renderSections();
renderTabBar();
</script>
</body>
</html>
"""


# HTTP server

def _safe_filename(stem: str, ext: str) -> str:
    """Turn a card id into a filesystem-safe filename, confined to the
    current directory (no path separators from user-controlled input)."""
    stem = os.path.basename((stem or "card").strip()) or "card"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return stem + ext




# Optional integrations, each checked once at startup: jira_config.json
# turns Jira tickets on; an executable `measure` in the working directory
# turns Sample / Activate on (without it they open the panel for manual entry
# and say nothing was sent).
JIRA_AVAILABLE = jira_ticket.load_config() is not None
_MEASURE_PATH = os.path.join(os.getcwd(), "measure")
MEASURE_AVAILABLE = os.path.isfile(_MEASURE_PATH) and os.access(_MEASURE_PATH, os.X_OK)

def _fill_flags(html: str) -> str:
    return (html.replace("__JIRA_AVAILABLE__", "true" if JIRA_AVAILABLE else "false")
                .replace("__MEASURE_AVAILABLE__", "true" if MEASURE_AVAILABLE else "false"))

_TEST_HTML = _fill_flags(_TEST_HTML)
_CAMPAIGN_HTML = _fill_flags(_CAMPAIGN_HTML)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html", _CAMPAIGN_HTML.encode())
        elif path == "/test":
            # ?load=<file> opens a specific .test file (read client-side via /load).
            self._send(200, "text/html", _TEST_HTML.encode())
        elif path == "/load":
            # Read a file by name from the working directory -- how a
            # test tab or the campaign page opens a saved file.
            qs = parse_qs(parsed.query)
            safe = os.path.basename((qs.get("file") or [""])[0])
            file_path = os.path.join(os.getcwd(), safe)
            if not safe or not os.path.isfile(file_path):
                self._send_json(404, {"error": f"not found: {safe!r}"})
                return
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self._send(200, "application/json", f.read().encode())
            except OSError as e:
                self._send_json(500, {"error": str(e)})
        elif path == "/list_files":
            # Lists files in the working directory by extension -- lets a
            # picker browse what's already on disk.
            qs = parse_qs(parsed.query)
            ext = (qs.get("ext") or [""])[0]
            if not re.match(r"^\.[A-Za-z0-9]+$", ext or ""):
                self._send_json(400, {"error": "ext must look like '.test'"})
                return
            try:
                names = sorted(
                    f for f in os.listdir(os.getcwd())
                    if f.endswith(ext) and os.path.isfile(f)
                )
            except OSError as e:
                self._send_json(500, {"error": str(e)})
                return
            self._send_json(200, {"files": names})
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        else:
            self._send(404, "text/plain", b"not found")

    def _serve_static(self, filename: str) -> None:
        # CSS assets live next to the script itself, not cwd (reserved for
        # user data). Under a PyInstaller build __file__ isn't reliable for
        # bundled data, so sys._MEIPASS (the extraction dir) is used instead.
        safe = os.path.basename(filename)
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            base_dir = sys._MEIPASS
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "static", safe)
        if not safe or not os.path.isfile(file_path):
            self._send(404, "text/plain", b"not found")
            return
        content_type = "text/css" if safe.endswith(".css") else "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                self._send(200, content_type, f.read())
        except OSError:
            self._send(500, "text/plain", b"error reading static file")

    def _write_file(self, filename: str, obj) -> None:
        # Saved to the server's working directory, meant to be re-opened by this program.
        out_path = os.path.join(os.getcwd(), filename)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=2)
        except OSError as e:
            self._send_json(500, {"saved": False, "error": str(e)})
            return
        self._send_json(200, {"saved": True, "filename": filename, "path": out_path})

    def _handle_capture_image(self, raw_body: bytes):
        # Payload is just {"data": "data:image/jpeg;base64,..."}, not a card.
        try:
            data_url = json.loads(raw_body)["data"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self._send_json(400, {"error": f"bad request: {e}"})
            return
        try:
            _header, b64data = data_url.split(",", 1)
            raw = base64.b64decode(b64data)
        except (ValueError, binascii.Error) as e:
            self._send_json(400, {"error": f"bad image data: {e}"})
            return
        # Millisecond resolution so a quick burst of captures doesn't collide.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{ts}.jpg"
        out_path = os.path.join(os.getcwd(), filename)
        try:
            with open(out_path, "wb") as f:
                f.write(raw)
        except OSError as e:
            self._send_json(500, {"saved": False, "error": str(e)})
            return
        self._send_json(200, {"saved": True, "filename": filename, "path": out_path})

    def _handle_create_jira_ticket(self, raw_body: bytes):
        # {"summary", "description", "epic_key", "issue_type"}; issue_type
        # optional -- falls back to cfg["issue_type"] if omitted.
        if not JIRA_AVAILABLE:
            self._send_json(404, {"success": False, "error": "Jira integration not available"})
            return
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"success": False, "error": f"bad request: {e}"})
            return
        try:
            result = jira_ticket.create_ticket(
                payload.get("summary", ""), payload.get("description", ""),
                payload.get("epic_key", ""), payload.get("issue_type") or None,
            )
        except Exception as e:
            # A Jira-side failure shouldn't take the whole server down.
            result = {"success": False, "error": f"jira_ticket.py raised: {e}"}
        self._send_json(200, result)

    def _handle_hardware(self, raw_body: bytes):
        # {"port": "/dev/usbtmc0", "command": "MEAS:VOLT:DC?"}. Sample and
        # Activate both run `measure`; they differ only in which command
        # the UI sends. A missing binary is a clean "not available".
        if not MEASURE_AVAILABLE:
            self._send_json(404, {"success": False, "error": "measure not available"})
            return
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"success": False, "error": f"bad request: {e}"})
            return
        port = (payload.get("port") or "").strip()
        command = (payload.get("command") or "").strip()
        if not port or not command:
            self._send_json(400, {"success": False, "error": "port and command are both required"})
            return
        try:
            proc = subprocess.run([_MEASURE_PATH, port, command],
                                   capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self._send_json(200, {"success": False, "error": "measure timed out"})
            return
        except Exception as e:
            self._send_json(200, {"success": False, "error": f"measure raised: {e}"})
            return
        if proc.returncode != 0:
            self._send_json(200, {"success": False, "error": proc.stderr.strip() or "unknown error"})
            return
        self._send_json(200, {"success": True, "value": proc.stdout.strip()})

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._body()
        if path == "/capture_image":
            return self._handle_capture_image(raw)
        if path == "/create_jira_ticket":
            return self._handle_create_jira_ticket(raw)
        if path in ("/measure", "/activate"):
            return self._handle_hardware(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": str(e)})
            return
        try:
            self._route_json(path, data)
        except (TypeError, KeyError, AttributeError, ValueError) as e:
            self._send_json(400, {"error": str(e)})

    def _route_json(self, path: str, data) -> None:
        host = self.headers.get("Host", "localhost")
        # Test-card routes
        if path in ("/export/csv", "/export/print", "/export/tex", "/export/test/docx", "/save"):
            card = TestCard.from_dict(data)
            if path == "/export/csv":
                self._send(200, "text/csv", export_csv(card).encode())
            elif path == "/export/print":
                self._send(200, "text/html", export_print_html(card, host).encode())
            elif path == "/export/tex":
                self._send(200, "text/x-tex", export_tex(card).encode())
            elif path == "/export/test/docx":
                self._send(200, _DOCX_MIME, export_test_docx(card))
            else:
                self._write_file(_safe_filename(card.id, ".test"), card.to_dict())
        # Campaign routes
        elif path in ("/save_campaign", "/export/campaign/print", "/export/campaign/docx", "/vcrm"):
            camp = Campaign.from_dict(data)
            if path == "/save_campaign":
                self._write_file(_safe_filename(camp.id, ".camp"), camp.to_dict())
            elif path == "/export/campaign/print":
                self._send(200, "text/html", export_campaign_print_html(camp, host).encode())
            elif path == "/export/campaign/docx":
                self._send(200, _DOCX_MIME, export_campaign_docx(camp))
            else:
                self._send_json(200, compute_vcrm(camp))
        # VCRM routes (the VCRM is plain JSON, not a dataclass)
        elif path == "/save_vcrm":
            self._write_file(vcrm_filename(data.get("campaign_id", "VCRM")), data)
        elif path == "/export/vcrm/print":
            self._send(200, "text/html", export_vcrm_print_html(data, host).encode())
        elif path == "/export/vcrm/docx":
            self._send(200, _DOCX_MIME, export_vcrm_docx(data))
        else:
            self._send(404, "text/plain", b"not found")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=5791)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("WARNING: running as root. sudo is never needed to run this --")
        print("         only to clear out a root-owned dist/ from an earlier")
        print("         build, if that ever comes up. Run it as your normal")
        print("         user instead: root has no desktop session of its own,")
        print("         so the browser almost certainly won't open correctly,")
        print("         and any files this writes would end up root-owned too.\n")

    url = f"http://localhost:{args.port}"
    print(f"Serving GUI at {url}")
    print("Press Ctrl+C to stop\n")

    server = ThreadingHTTPServer(("localhost", args.port), _Handler)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())