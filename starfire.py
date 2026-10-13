#!/usr/bin/env python3

# starfire.py:    TEST CAMPAIGN + TEST CARD DESIGNER
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-11
# VERSION:        0.2.0
#
# Single stdlib file (plus python-docx); client-side editor state, the
# server handles exports, saved files, and the optional `starfire-hw` binary.

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
`starfire-hw` binary (Sample / Activate buttons; built from starfire.cpp) and
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
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field, fields, asdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
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
    # Serial number / article under test; run data, so editable on a locked card.
    article: str = ""
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
        # Unknown keys are dropped, so files saved by an older version (e.g.
        # with the retired activate_command) still open.
        known = {f.name for f in fields(TestPoint)}
        pts = [TestPoint(**{k: v for k, v in p.items() if k in known}) for p in d.get("points", [])]
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
class CampaignModule:
    # A script or binary in the program's dist/ folder that a test point's
    # Activate (bolt) button may run. See list_modules / run_module.
    module: str = ""
    description: str = ""
    # "" = any test point; a card id (TC-001) = any point on that card;
    # card id + "-" + point number (TC-001-20) = that one point.
    scope: str = ""


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
    # Modules a test point's Activate button may run.
    modules: list = field(default_factory=list)         # list[CampaignModule]
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
    # Files ticked into each card's Test Report: {card_id: {point_seq: [filename, ...]}}.
    reports: dict = field(default_factory=dict)
    # The campaign report (SR-xxx), once built: {"items": [section | vcrm | report, ...]}.
    campaign_report: dict = None

    @staticmethod
    def from_dict(d: dict) -> "Campaign":
        tests = [CampaignTestRow(**t) for t in d.get("tests", [])]
        ancillary_tests = [CampaignTestRow(**t) for t in d.get("ancillary_tests", [])]
        known = {f.name for f in fields(CampaignModule)}
        modules = [CampaignModule(**{k: v for k, v in m.items() if k in known}) for m in d.get("modules", [])]
        base = {k: v for k, v in d.items() if k not in ("tests", "ancillary_tests", "modules")}
        return Campaign(tests=tests, ancillary_tests=ancillary_tests, modules=modules, **base)

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
    mods = [m for m in camp.modules if (m.module or "").strip()]
    mod_rows = "\n".join(
        f"<tr><td>{h(m.module)}</td><td>{h(m.description)}</td><td>{h(m.scope or 'any')}</td></tr>" for m in mods)
    mod_section = "" if not mods else f"""<h2>Modules</h2>
<table><tr><th style="width:25%">Module</th><th>Description</th><th style="width:15%">Scope</th></tr>
{mod_rows}
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
{req_section}{mod_section}<h2>Test Series</h2>
<table>
<tr><th style="width:6%">#</th><th style="width:14%">Test ID</th><th>Title</th><th style="width:12%">Author</th><th style="width:16%">Requirements</th><th style="width:12%">Status</th></tr>
{test_rows}
</table>
{anc_section}{sections_html}
<script>window.onload = () => window.print();</script>
</body></html>"""


def _covering_str(row: dict) -> str:
    if not row["covering_tests"]:
        return "--"
    return ", ".join(f"{c['test_id']} ({STATUS_LABELS.get(c['status'], c['status'] or '[None]')})"
                     for c in row["covering_tests"])


def _vcrm_table_html(vcrm: dict) -> str:
    rows = "\n".join(
        f"<tr><td>{_h(r['requirement'])}</td><td>{_h(r['description'])}</td>"
        f"<td>{_h(_covering_str(r))}</td><td>{_h(r['coverage'])}</td><td>{_h(vcrm['generated'])}</td></tr>"
        for r in vcrm["rows"]
    )
    return ('<table><tr><th style="width:14%">Requirement</th><th>Description</th><th style="width:24%">Covering Tests</th>'
            '<th style="width:10%">Coverage</th><th style="width:12%">Updated</th></tr>\n' + rows + "\n</table>")


def export_vcrm_csv(vcrm: dict) -> str:
    """The VCRM as a Test Matrix: one row per requirement."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Requirement", "Description", "Covering Tests", "Status", "Updated"])
    for r in vcrm["rows"]:
        w.writerow([r["requirement"], r["description"], _covering_str(r), r["coverage"], vcrm["generated"]])
    return buf.getvalue()


def export_vcrm_print_html(vcrm: dict, host: str = "localhost") -> str:
    h = _h

    table = _vcrm_table_html(vcrm)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{h(vcrm['campaign_id'])} VCRM -- {h(vcrm['campaign_title'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starfire.css"></head>
<body class="print-doc">
<h1>Verification Cross-Reference Matrix</h1>
{table}
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

    mods = [m for m in camp.modules if (m.module or "").strip()]
    if mods:
        doc.add_heading("Modules", level=1)
        _add_table(doc, ["Module", "Description", "Scope"],
                   [(m.module, m.description, m.scope or "any") for m in mods])

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


def _vcrm_docx_table(doc, vcrm: dict) -> None:
    _add_table(
        doc, ["Requirement", "Description", "Covering Tests", "Coverage", "Updated"],
        [(r["requirement"], r["description"], _covering_str(r), r["coverage"], vcrm["generated"])
         for r in vcrm["rows"]],
    )


def export_vcrm_docx(vcrm: dict) -> bytes:
    doc = Document()
    doc.add_heading("Verification Cross-Reference Matrix", level=0)
    _vcrm_docx_table(doc, vcrm)

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


# Test Card (procedure only) vs Test Report (as-run record).

def procedure_only(card: TestCard) -> TestCard:
    """A copy with every run result cleared, so the card prints as a blank procedure."""
    d = card.to_dict()
    d["article"] = ""
    for p in d["points"]:
        for k in ("observed", "responsible_party", "date", "time", "initial", "quality"):
            p[k] = ""
        p["measurements"], p["images"] = [], []
    return TestCard.from_dict(d)


def report_id(card_id: str) -> str:
    cid = (card_id or "").strip()
    return "TR-" + cid[3:] if cid.upper().startswith("TC-") else "TR-" + (cid or "000")



def report_head(card: TestCard) -> str:
    rev = (card.revision or "").strip()
    return " ".join(x for x in (report_id(card.id), (card.title or "").strip(), f"REV {rev.upper()}" if rev else "") if x)


def sr_id(campaign_id: str) -> str:
    cid = (campaign_id or "").strip()
    return "SR-" + cid[3:] if cid.upper().startswith("SC-") else "SR-" + (cid or "000")


def sr_head(camp: Campaign) -> str:
    rev = (camp.revision or "").strip()
    return " ".join(x for x in (sr_id(camp.id), (camp.title or "").strip(), f"REV {rev.upper()}" if rev else "") if x)


def _sr_items(camp: Campaign):
    """Yield (kind, payload) for each item that has something to show; a test with no saved .test file is skipped."""
    items = (camp.campaign_report or {}).get("items", [])
    for it in sorted(items, key=lambda x: x.get("type") == "report"):   # Test Reports always follow the rest
        kind = it.get("type")
        if kind == "section":
            idx = it.get("idx")
            if idx is not None:   # a live reference to the campaign's Sections
                secs = camp.sections or []
                it = {"header": secs[idx][0], "text": secs[idx][1]} if isinstance(idx, int) and 0 <= idx < len(secs) else {}
            if (it.get("header") or "").strip() or (it.get("text") or "").strip():
                yield kind, it
        elif kind == "vcrm":
            yield kind, compute_vcrm(camp)
        elif kind == "report":
            path = os.path.join(os.getcwd(), test_filename(str(it.get("card") or "")))
            try:
                with open(path, encoding="utf-8") as f:
                    yield kind, TestCard.from_dict(json.load(f))
            except (OSError, ValueError):
                continue


def export_campaign_report_html(camp: Campaign, host: str = "localhost", preview: bool = False) -> str:
    head = sr_head(camp)
    parts = []
    for kind, v in _sr_items(camp):
        if kind == "section":
            parts.append(f'<div class="sr-part"><h2>{_h(v.get("header"))}</h2><div class="section-text">{_h(v.get("text"))}</div></div>')
        elif kind == "vcrm":
            parts.append(f'<div class="sr-part sr-new"><h2>Verification Cross-Reference Matrix</h2>{_vcrm_table_html(v)}</div>')
        else:
            parts.append(f'<div class="sr-part sr-new">{report_body(v, camp.id, (camp.reports or {}).get(v.id, {}))}</div>')
    body = f"""<h1>{_h(head)}</h1>
<div class="meta">
  <span><b>Campaign:</b> {_h(camp.id)}</span>
  <span><b>Project:</b> {_h(camp.project)}</span>
  <span><b>System/Subsystem:</b> {_h(camp.system)} / {_h(camp.subsystem)}</span>
</div>
<div class="meta">
  <span><b>Test Lead:</b> {_h(camp.test_lead)}</span>
  <span><b>Quality Assurance:</b> {_h(camp.quality_assurance)}</span>
  <span><b>Date:</b> {_h(camp.date)}</span>
</div>
{"".join(parts)}"""
    return _report_page(head, body, host, preview)


def _add_field(paragraph, instr: str) -> None:
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, instr), ("separate", None), (None, "1"), ("end", None)):
        if kind:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        elif text == instr:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = f" {instr} "
        else:
            el = OxmlElement("w:t")
            el.text = text
        run._r.append(el)


def export_campaign_report_docx(camp: Campaign) -> bytes:
    doc = Document()
    foot = doc.sections[0].footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    foot.add_run("Page ")
    _add_field(foot, "PAGE")
    foot.add_run(" of ")
    _add_field(foot, "NUMPAGES")
    doc.add_heading(sr_head(camp), level=0)
    doc.add_paragraph().add_run(
        f"Campaign: {camp.id}    Project: {camp.project}    System/Subsystem: {camp.system} / {camp.subsystem}\n"
        f"Test Lead: {camp.test_lead}    Quality Assurance: {camp.quality_assurance}    Date: {camp.date}"
    ).font.size = Pt(9.5)
    for kind, v in _sr_items(camp):
        if kind == "section":
            doc.add_heading(v.get("header") or "", level=1)
            for line in (v.get("text") or "").split("\n"):
                doc.add_paragraph(line)
            continue
        doc.add_page_break()
        if kind == "vcrm":
            doc.add_heading("Verification Cross-Reference Matrix", level=1)
            _vcrm_docx_table(doc, v)
        else:
            _report_docx_body(doc, v, camp.id, (camp.reports or {}).get(v.id, {}))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".txt":
        return "csv" if _looks_csv(_cwd_file(name)) else "txt"
    return "csv" if ext == ".csv" else "image" if ext in _IMAGE_TYPES else "other"


def _point_prefix(campaign_id: str, card_id: str) -> str:
    part = lambda s, d: re.sub(r"[^A-Za-z0-9._-]", "_", str(s or "").strip()) or d
    return part(campaign_id, "XX-000") + "-" + part(card_id, "TC")


def list_point_files(campaign_id: str, card_id: str) -> dict:
    """{seq: [{"name", "kind"}]} for files in the working folder named
    <campaign>-<card>-<point>[-anything].<ext>."""
    pat = re.compile(r"^" + re.escape(_point_prefix(campaign_id, card_id)) + r"-(\d+)(?:-[^/]*)?\.[A-Za-z0-9]+$")
    out = {}
    try:
        names = sorted(os.listdir(os.getcwd()))
    except OSError:
        return out
    for n in names:
        m = pat.match(n)
        if m and os.path.isfile(n):
            out.setdefault(m.group(1), []).append({"name": n, "kind": _file_kind(n)})
    return out


def _num(s):
    m = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", str(s))
    return float(m.group(1)) if m else None


def csv_series(path: str, limit: int = 5000):
    """(x_label, y_label, xs, ys) from a csv: y is the "value" column if there is
    one, else the last numeric column; x is the first column if numeric, else the row number."""
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r) and not r[0].lstrip().startswith("#")][:limit + 1]
    if not rows:
        return None
    header = rows[0] if any(_num(c) is None for c in rows[0]) else []
    body = rows[1:] if header else rows
    width = max(len(r) for r in body) if body else 0
    numeric = [i for i in range(width) if any(i < len(r) and _num(r[i]) is not None for r in body)]
    if not numeric:
        return None
    names = [c.strip().lower() for c in header]
    y = names.index("value") if "value" in names and names.index("value") in numeric else numeric[-1]
    x = numeric[0] if numeric[0] != y else None
    pts = []
    for i, r in enumerate(body):
        yv = _num(r[y]) if y < len(r) else None
        xv = (_num(r[x]) if x < len(r) else None) if x is not None else i + 1
        if yv is not None and xv is not None:
            pts.append((xv, yv))
    if not pts:
        return None
    label = lambda i, d: header[i] if header and i < len(header) and header[i].strip() else d
    return (label(x, "x") if x is not None else "sample", label(y, "y"), [p[0] for p in pts], [p[1] for p in pts])


def _looks_csv(path) -> bool:
    """A .txt that is comma-separated with at least two plottable rows ("# ..." lines are ignored)."""
    try:
        s = csv_series(path, 50) if path else None
        first = next(l for l in _read_text(path, 4096).splitlines() if l.strip() and not l.lstrip().startswith("#"))
    except (OSError, StopIteration):
        return False
    return bool(s) and len(s[2]) >= 2 and "," in first


def csv_plot_svg(path: str) -> str:
    s = csv_series(path)
    if not s:
        return ""
    xl, yl, xs, ys = s
    x0, x1 = min(xs), max(xs)
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.1 or 1.0
    y0, y1 = lo - pad, hi + pad
    sx = lambda v: 56 + (v - x0) / ((x1 - x0) or 1) * 484
    sy = lambda v: 176 - (v - y0) / (y1 - y0) * 156
    line = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(xs, ys))
    dots = "".join(f'<circle cx="{sx(a):.1f}" cy="{sy(b):.1f}" r="3"/>' for a, b in zip(xs, ys)) if len(xs) <= 60 else ""
    g = lambda v: f"{v:.4g}"
    return (f'<svg class="rp-plot" viewBox="0 0 560 210" role="img" aria-label="{_h(yl)} vs {_h(xl)}">'
            f'<line x1="56" y1="176" x2="540" y2="176"/><line x1="56" y1="20" x2="56" y2="176"/>'
            f'<text x="50" y="24" text-anchor="end">{g(y1)}</text><text x="50" y="176" text-anchor="end">{g(y0)}</text>'
            f'<text x="56" y="194">{g(x0)}</text><text x="540" y="194" text-anchor="end">{g(x1)}</text>'
            f'<text x="298" y="206" text-anchor="middle">{_h(xl)}</text>'
            f'<text x="12" y="98" transform="rotate(-90 12 98)" text-anchor="middle">{_h(yl)}</text>'
            f'<polyline points="{line}"/><g>{dots}</g></svg>')


def _meas_rows(p: TestPoint) -> list:
    # Saved rows are objects from the UI; older files may hold [command, value, timestamp, auto].
    out = []
    for m in p.measurements:
        if isinstance(m, dict):
            out.append((m.get("command", "") if m.get("auto") else "--", m.get("value", ""), m.get("timestamp", "")))
        elif isinstance(m, (list, tuple)) and len(m) >= 3:
            out.append((m[0] if (len(m) < 4 or m[3]) else "--", m[1], m[2]))
    return [r for r in out if str(r[1]).strip()]


def _cwd_file(name: str):
    path = os.path.join(os.getcwd(), os.path.basename(name or ""))
    return path if name and os.path.isfile(path) else None


def _img_tag(name: str) -> str:
    path = _cwd_file(name)
    mime = _IMAGE_TYPES.get(os.path.splitext(name)[1].lower())
    if not path or not mime:
        return f'<div class="rp-missing">{_h(name)} (not found)</div>'
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f'<figure class="rp-img"><img src="data:{mime};base64,{data}" alt="{_h(name)}"><figcaption>{_h(name)}</figcaption></figure>'


def _read_text(path: str, limit: int = 200_000) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def _ticked(files: dict, card: TestCard, campaign_id: str, seq) -> list:
    """Ticked names for a point, kept only if they really belong to it."""
    pat = re.compile(r"^" + re.escape(_point_prefix(campaign_id, card.id)) + "-" + str(seq) + r"(?:-[^/]*)?\.[A-Za-z0-9]+$")
    return [n for n in (files or {}).get(str(seq), []) if isinstance(n, str) and pat.match(n) and _cwd_file(n)]


def _plotted(files: dict, seq) -> list:
    """Names the user chose to plot; stored under files["plot"] as {seq: [names]}."""
    p = (files or {}).get("plot")
    return p.get(str(seq), []) if isinstance(p, dict) else []


def _point_file_html(name: str, plot: bool = False) -> str:
    kind, path = _file_kind(name), _cwd_file(name)
    if kind == "csv" and not plot:
        return f'<div class="rp-file"><div class="rp-cap">{_h(name)}</div><pre class="rp-out">{_h(_cap_lines(_read_text(path)))}</pre></div>'
    if kind == "csv":
        svg = csv_plot_svg(path)
        return f'<div class="rp-file"><div class="rp-cap">{_h(name)}</div>{svg or "<div class=rp-missing>no numeric data to plot</div>"}</div>'
    if kind == "image":
        return _img_tag(name)
    if kind == "txt":
        return f'<div class="rp-file"><div class="rp-cap">{_h(name)}</div><pre class="rp-out">{_h(_cap_lines(_read_text(path)))}</pre></div>'
    return f'<div class="rp-file rp-cap">Attached: {_h(name)}</div>'


def report_body(card: TestCard, campaign_id: str = "", files: dict = None) -> str:
    """One Test Report as an HTML fragment (header through test points)."""
    pts = []
    for p in card.points:
        signed = _h(p.initial)
        when = _h(f"{p.date} {p.time}".strip())
        under = "".join(f'<span class="rp-when">{x}</span>' for x in (when, signed) if x)
        meas = _meas_rows(p)
        meas_tbl = "" if not meas else (
            '<table class="rp-meas"><tr><th>Command</th><th>Value</th><th>Timestamp</th></tr>'
            + "".join(f"<tr><td>{_h(str(c))}</td><td>{_h(str(v))}</td><td>{_h(str(t))}</td></tr>" for c, v, t in meas)
            + "</table>")
        imgs = "".join(_img_tag(n) for n in p.images)
        extra = "".join(_point_file_html(n, n in _plotted(files, p.seq)) for n in _ticked(files, card, campaign_id, p.seq))
        q = (p.quality or "").upper()
        pts.append(f'''<section class="rp-point rp-b-{_h(q.lower()) or "none"}" data-seq="{p.seq}">
<div class="rp-head"><span>{p.seq} &nbsp; {_h(p.action)}</span><span class="rp-right"><span class="rp-q rp-q-{_h(q.lower())}">{_h(q) or "&mdash;"}</span>{under}</span></div>
<div class="rp-line"><b>Expected:</b> {_h(p.expected)}</div>
<div class="rp-line"><b>Observed:</b> {_h(p.observed) or "&mdash;"}</div>
{meas_tbl}{imgs and f'<div class="rp-imgs">{imgs}</div>'}{extra}
</section>''')
    head = report_head(card)
    nom = sorted_nomenclature(card)
    nom_html = "" if not nom else "<h2>Nomenclature</h2><table><tr><th>Symbol</th><th>Definition</th><th>Units</th></tr>" + "".join(
        f"<tr><td>{_h(symbol_display(a))}</td><td>{_h(b)}</td><td>{_h(u)}</td></tr>" for a, b, u, _l in nom) + "</table>"
    refs = [(a, b) for a, b, _l in card.references if (a or "").strip() or (b or "").strip()]
    ref_html = "" if not refs else "<h2>References</h2><table><tr><th>Reference</th><th>Description</th></tr>" + "".join(
        f"<tr><td>{_h(a)}</td><td>{_h(b)}</td></tr>" for a, b in refs) + "</table>"
    return f"""<h1>{_h(head)}</h1>
<div class="meta">
  <span><b>Campaign:</b> {_h(campaign_id or "XX-000")}</span>
  <span><b>Article:</b> {_h(card.article) or "&mdash;"}</span>
  <span><b>System/Subsystem:</b> {_h(card.system)} / {_h(card.subsystem)}</span>
</div>
<div class="meta">
  <span><b>Test Conductor:</b> {_h(card.test_conductor)}</span>
  <span><b>Quality Assurance:</b> {_h(card.quality_assurance)}</span>
  <span><b>Date:</b> {_h(card.date)}</span>
</div>
<h2>Requirements covered</h2>
<p>{_h(card.requirements) or "&mdash;"}</p>
{nom_html}{ref_html}<h2>Test points</h2>
{"".join(pts)}"""


def _report_page(title: str, body: str, host: str, preview: bool) -> str:
    script = "" if preview else "<script>window.onload = () => window.print();</script>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_h(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612:wght@400;700&family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="http://{host}/static/starfire.css"></head>
<body class="print-doc report-doc{' preview' if preview else ''}">
{body}
{script}
</body></html>"""


def export_report_html(card: TestCard, campaign_id: str = "", files: dict = None,
                       host: str = "localhost", preview: bool = False) -> str:
    return _report_page(report_head(card), report_body(card, campaign_id, files), host, preview)


def _report_docx_body(doc, card: TestCard, campaign_id: str = "", files: dict = None) -> None:
    doc.add_heading(report_head(card), level=0)
    doc.add_paragraph().add_run(
        f"Campaign: {campaign_id or 'XX-000'}    "
        f"Article: {card.article}    System/Subsystem: {card.system} / {card.subsystem}\nTest Conductor: {card.test_conductor}    "
        f"Quality Assurance: {card.quality_assurance}    Date: {card.date}"
    ).font.size = Pt(9.5)
    doc.add_heading("Requirements covered", level=1)
    doc.add_paragraph(card.requirements or "--")
    nom = sorted_nomenclature(card)
    if nom:
        doc.add_heading("Nomenclature", level=1)
        _add_table(doc, ["Symbol", "Definition", "Units"], [(symbol_display(a), b, u) for a, b, u, _l in nom])
    refs = [(a, b) for a, b, _l in card.references if (a or "").strip() or (b or "").strip()]
    if refs:
        doc.add_heading("References", level=1)
        _add_table(doc, ["Reference", "Description"], refs)
    doc.add_heading("Test points", level=1)
    for p in card.points:
        doc.add_heading(f"{p.seq}  {p.action}    [{(p.quality or '--').upper()}]", level=2)
        doc.add_paragraph(f"Expected: {p.expected}\nObserved: {p.observed or '--'}")
        meas = _meas_rows(p)
        if meas:
            _add_table(doc, ["Command", "Value", "Timestamp"], meas)
        for n in list(p.images) + _ticked(files, card, campaign_id, p.seq):
            path, kind = _cwd_file(n), _file_kind(n)
            if kind == "image" and path:
                doc.add_picture(path, width=Inches(3.5))
                doc.add_paragraph(n).runs[0].font.size = Pt(8)
            elif kind == "csv" and path and n not in _plotted(files, p.seq):
                doc.add_paragraph(n).runs[0].font.size = Pt(8)
                run = doc.add_paragraph().add_run(_cap_lines(_read_text(path), 200))
                run.font.name, run.font.size = "Courier New", Pt(8)
            elif kind == "csv" and path:
                s = csv_series(path)
                doc.add_paragraph(f"{n} (plot in the PDF export)").runs[0].font.size = Pt(8)
                if s:
                    _add_table(doc, [s[0], s[1]], [(f"{a:g}", f"{b:g}") for a, b in list(zip(s[2], s[3]))[:50]])
            elif kind == "txt" and path:
                doc.add_paragraph(n).runs[0].font.size = Pt(8)
                run = doc.add_paragraph().add_run(_cap_lines(_read_text(path), 200))
                run.font.name, run.font.size = "Courier New", Pt(8)
            else:
                doc.add_paragraph(f"Attached: {n}" if path else f"{n} (not found)")
        sign = [x for x in (f"{p.date} {p.time}".strip(), p.initial) if x]
        if sign:
            doc.add_paragraph("    ".join(sign)).alignment = WD_ALIGN_PARAGRAPH.RIGHT


def export_report_docx(card: TestCard, campaign_id: str = "", files: dict = None) -> bytes:
    doc = Document()
    _report_docx_body(doc, card, campaign_id, files)
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
<div id="panels"><div class="panel active split">
<div class="report-side">
<div class="side-title"><h1>STARFIRE</h1><div class="sub">Test Campaign Designer</div></div>
<div class="side-btns" id="exportRow">
  <button class="btn" id="loadJsonBtn">import test file</button>
  <input type="file" id="loadJsonFile" accept=".test,.json,application/json" style="display:none">
  <button class="btn" id="saveUnlockedBtn">export unlocked test file (.test)</button>
  <button class="btn" id="saveLockedBtn">export locked test file (.test)</button>
  <button class="btn" id="exportCsvBtn">export .csv</button>
  <button class="btn" id="exportPrintBtn">generate card</button>
  <button class="btn" id="exportDocxBtn">export .docx</button>
  <button class="btn" id="buildReportBtn" style="display:none">build test report</button>
</div>
<div id="saveStatus" class="hint"></div>
</div>
<div class="side-main"><div class="side-inner">

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
  <div class="field"><label>Article / S/N</label><input id="m-article"></div>
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

</div></div>
</div></div>

<script>
// True only if jira_config.json was found in the working directory at
// startup -- see jira_ticket.py's own docstring.
const JIRA_AVAILABLE = __JIRA_AVAILABLE__;

// True only if the starfire-hw binary was found -- the Measure
// panel opens either way, this just controls whether Sample/Activate send anything.
const HARDWARE_AVAILABLE = __HARDWARE_AVAILABLE__;

// Set by the campaign page right after this iframe loads (openTestTab());
// `var` (not `let`) so that external write reaches this binding.
var starfireJiraEpicKey = "";

let card = {
  id: "TC-001", title: "Untitled Test Card", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_conductor: "",
  quality_assurance: "", system: "", subsystem: "", test_type: "", article: "", requirements: "",
  points: [], nomenclature: [], references: [], notes: [], warnings: [], locked: false
};

const metaFields = ["id","title","revision","author","date","project","test_conductor","quality_assurance","test_type","article","requirements"];
const ALWAYS_EDITABLE_META = new Set(["test_conductor", "quality_assurance", "date", "article"]);
function bindMeta() {
  metaFields.forEach(f => {
    const el = document.getElementById("m-" + f);
    el.value = card[f] ?? "";
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
const ICON_DOWNLOAD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M480-320 280-520l56-58 104 104v-326h80v326l104-104 56 58-200 200ZM240-160q-33 0-56.5-23.5T160-240v-120h80v120h480v-120h80v120q0 33-23.5 56.5T720-160H240Z"/></svg>';
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

// The gauge button opens/closes the panel (a point that already has readings keeps
// its panel showing, so closing only hides an empty one).
function toggleMeasure(i) {
  if (!measureOpenIndices.delete(i)) {
    measureOpenIndices.add(i);
    // First open of a point with no readings -- seed one blank row so
    // there's something to type into right away.
    if (card.points[i].measurements.length === 0) {
      card.points[i].measurements.push({command: "", value: "", timestamp: "", auto: false});
    }
  }
  renderPoints();
}

// SEND: the line in the Command box goes to the instrument. starfire-hw says
// whether it was a query or a command: a query's reply is logged as a reading,
// a command is just confirmed here.
async function sendMeasurement(i) {
  const p = card.points[i];
  const msgEl = document.getElementById("pt-measure-msg-" + i);
  const say = (cls, text) => { if (msgEl) { msgEl.className = "module-status " + cls; msgEl.textContent = text; } };
  if (!HARDWARE_AVAILABLE) return say("failed", "starfire-hw not found -- nothing was sent.");
  if (!p.usb_port || !p.measure_command) return say("failed", "Enter a USB port and a command first.");
  say("running", "Sending...");
  try {
    const res = await fetch("/measure", {
      method: "POST",
      body: JSON.stringify({port: p.usb_port, command: p.measure_command}),
    });
    const result = await res.json();
    if (!result.success) return say("failed", result.error || "Send failed");
    if (result.kind !== "query") {
      return say("complete", `Sent ${p.measure_command} at ${new Date().toLocaleTimeString("en-GB")}`);
    }
    if (p.measurements.length >= 8) {
      return say("failed", `Reply: ${result.value} (not logged: maximum 8 readings, delete a row first)`);
    }
    const ts = new Date().toISOString().slice(0, 16).replace("T", " ");
    p.measurements.push({command: p.measure_command, value: result.value, timestamp: ts, auto: true});
    renderPoints();
  } catch (err) {
    say("failed", `Send failed: ${err}`);
  }
}

// Modules (Activate / bolt): scripts or binaries in dist/ that the campaign's
// Modules table registers. The registry is read live from the campaign page
// we're embedded in; a standalone card has none, so nothing can be run.
// Panel-open and run state are transient (never saved) and keyed by the point
// object, so they follow a point when it's moved up or down.
const moduleState = new WeakMap();

function campaignModules() {
  try {
    return (window.parent !== window && window.parent.getCampaignModules) ? window.parent.getCampaignModules() : [];
  } catch (e) { return []; }
}

// Scope "" = any point; "TC-001" = any point on that card; "TC-001-20" = that point only.
function moduleInScope(m, cardId, seq) {
  const s = (m.scope || "").trim().toLowerCase();
  if (!s) return true;
  const id = (cardId || "").trim().toLowerCase();
  return s === id || s === id + "-" + seq;
}

function modulesFor(p) {
  return campaignModules().filter(m => (m.module || "").trim() && moduleInScope(m, card.id, p.seq)).map(m => m.module);
}

function modulePanelHtml(i, p) {
  const st = moduleState.get(p) || {};
  const names = modulesFor(p);
  const none = names.length === 0
    ? `<div class="pt-cam-msg">No modules registered for this point. Add one in the campaign's Modules table.</div>` : "";
  return `
    <label>Module</label>
    <div class="send-row">
      <input list="pt-modlist-${i}" data-modname="${i}" value="${esc(st.name || "")}" placeholder="module name">
      <datalist id="pt-modlist-${i}">${names.map(n => `<option value="${esc(n)}">`).join("")}</datalist>
      <button class="btn mini" data-modrun="${i}"${st.status === "running" ? " disabled" : ""}>run</button>
      <button class="btn mini break-btn" data-modbreak="${i}"${st.status === "running" ? "" : " disabled"}>break</button>
    </div>
    ${none}
    <div class="module-status ${esc(st.status || "")}">${esc(st.text || "")}</div>
    ${st.output ? `<pre class="module-output">${esc(st.output)}</pre>
    <div class="panel-actions">
      <button class="btn mini btn-icon btn-download" data-modsave="${i}" title="save output as ${esc(pointFileName(p, "txt"))}" aria-label="save module output">${ICON_DOWNLOAD}</button>
      <span class="module-status complete">${esc(st.saved || "")}</span>
    </div>` : ""}`;
}

function bindModulePanel(i) {
  const root = document.getElementById("pt-module-" + i);
  if (!root) return;
  const p = card.points[i];
  const inp = root.querySelector("input[data-modname]");
  if (inp) inp.addEventListener("input", () => {
    const st = moduleState.get(p) || {};
    st.name = inp.value;
    moduleState.set(p, st);
  });
  const btn = root.querySelector("button[data-modrun]");
  if (btn) btn.addEventListener("click", () => runModule(p));
  const brk = root.querySelector("button[data-modbreak]");
  if (brk) brk.addEventListener("click", () => breakModule(p));
  const sv = root.querySelector("button[data-modsave]");
  if (sv) sv.addEventListener("click", async () => {
    const st = moduleState.get(p);
    st.saved = await savePointFile(p, "txt", st.output);
    refreshModulePanel(p);
  });
}

// Redraws just this point's module panel (not the whole list) so a run
// finishing doesn't steal focus from whatever the person is typing elsewhere.
function moduleIsOpen(p) { return !!(moduleState.get(p) || {}).open; }

function refreshModulePanel(p) {
  const i = card.points.indexOf(p);
  const root = document.getElementById("pt-module-" + i);
  if (!root || !moduleIsOpen(p)) return;
  root.innerHTML = modulePanelHtml(i, p);
  bindModulePanel(i);
}

async function runModule(p) {
  const st = moduleState.get(p) || {};
  moduleState.set(p, st);
  const name = (st.name || "").trim();
  if (!name) {
    Object.assign(st, {status: "failed", text: "Enter a module name.", output: ""});
    return refreshModulePanel(p);
  }
  if (!modulesFor(p).includes(name)) {
    Object.assign(st, {status: "failed", output: "",
      text: `${name} isn't registered for this point. Add it to the campaign's Modules table, with a scope that includes it.`});
    return refreshModulePanel(p);
  }
  Object.assign(st, {status: "running", text: "Running...", output: "", saved: ""});
  refreshModulePanel(p);
  try {
    const res = await fetch("/run_module", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({module: name, card: card.id, point: p.seq, campaign: pointCampaign() || "XX-000"}),
    });
    const r = await res.json();
    const detail = [r.exit_code != null ? `exit ${r.exit_code}` : "", r.seconds != null ? `${r.seconds} s` : ""].filter(Boolean).join(", ");
    st.status = ["complete", "broken"].includes(r.status) ? r.status : "failed";
    st.text = `${st.status.toUpperCase()}${detail ? " (" + detail + ")" : ""}${r.error ? ": " + r.error : ""}`;
    st.output = r.output || "";
  } catch (err) {
    Object.assign(st, {status: "failed", text: `FAILED: ${err}`, output: ""});
  }
  refreshModulePanel(p);
}

async function breakModule(p) {
  const st = moduleState.get(p) || {};
  try {
    const res = await fetch("/break_module", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({card: card.id, point: p.seq}),
    });
    const r = await res.json();
    if (!r.success) { st.text = r.error || "Break failed"; refreshModulePanel(p); }
  } catch (err) {
    st.text = `Break failed: ${err}`;
    refreshModulePanel(p);
  }
}

function pointCampaign() {
  try { return (window.parent !== window && window.parent.getCampaignId) ? window.parent.getCampaignId() : ""; } catch (e) { return ""; }
}

// <campaign>-<card>-<point>.<ext>, as the server will name it.
function pointFileName(p, ext) {
  const part = s => String(s || "").trim().replace(/[^A-Za-z0-9._-]/g, "_");
  return `${part(pointCampaign()) || "XX-000"}-${part(card.id) || "TC"}-${p.seq}.${ext}`;
}

// Saves text next to the .test files; returns the line to show.
async function savePointFile(p, ext, content) {
  try {
    const res = await fetch("/save_point_file", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({campaign: pointCampaign() || "XX-000", card: card.id, point: p.seq, ext, content}),
    });
    const r = await res.json();
    return r.saved ? `Saved ${r.filename}` : `Save failed: ${r.error || "unknown error"}`;
  } catch (err) {
    return `Save failed: ${err}`;
  }
}

function measureCsv(p) {
  const q = v => /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  const rows = p.measurements.filter(m => m.value).map(m => [m.auto ? m.command : "", m.value, m.timestamp].map(q).join(","));
  return ["command,value,timestamp", ...rows].join("\\n") + "\\n";
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
    </div>
    <label>Command</label>
    <div class="send-row">
      <input data-i="${i}" data-f="measure_command" value="${esc(p.measure_command)}" placeholder="MEAS:VOLT:DC?  or  OUTP ON">
      <button class="btn mini" data-msend="${i}">send</button>
    </div>
    <div class="module-status" id="pt-measure-msg-${i}"></div>` : "";
  return `
    ${fields}
    <table class="measure-table">
      <colgroup><col class="mt-cmd"><col class="mt-val"><col class="mt-ts"><col class="mt-del"></colgroup>
      <tr><th>Command</th><th>Value</th><th>Timestamp</th><th class="blank-th"></th></tr>
      ${rows}
    </table>
    <div class="panel-actions">
      ${open && canAddRow ? `<button class="btn mini btn-plus" data-madd="${i}" title="add row">+</button>` : ""}
      ${p.measurements.some(m => m.value) ? `<button class="btn mini btn-icon btn-download" data-msave="${i}" title="save readings as ${esc(pointFileName(p, "csv"))}" aria-label="save readings">${ICON_DOWNLOAD}</button>` : ""}
      <span class="module-status complete" id="pt-measure-save-${i}"></span>
    </div>`;
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
          <button class="btn mini btn-icon measure-toggle" data-act="measure" data-i="${i}" title="measure">${ICON_SPEED}</button>
          <button class="btn mini btn-icon module-toggle" data-act="module" data-i="${i}" title="activate (run a module)">${ICON_BOLT}</button>
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
        <div class="pt-module" id="pt-module-${i}">${moduleIsOpen(p) ? modulePanelHtml(i, p) : ""}</div>
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
      if (act === "measure") { toggleMeasure(i); return; }
      if (act === "module") {
        const st = moduleState.get(card.points[i]) || {};
        st.open = !st.open;
        moduleState.set(card.points[i], st);
      }
      renderPoints();
    });
  });
  list.querySelectorAll("button[data-msave]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const i = +btn.dataset.msave;
      const msg = await savePointFile(card.points[i], "csv", measureCsv(card.points[i]));
      const el = document.getElementById("pt-measure-save-" + i);
      if (el) el.textContent = msg;
    });
  });
  list.querySelectorAll("button[data-msend]").forEach(btn => {
    btn.addEventListener("click", () => sendMeasurement(+btn.dataset.msend));
  });
  card.points.forEach((_, i) => bindModulePanel(i));
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
                     usb_port: "", measure_command: "", measurements: []});
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
                usb_port:"", measure_command:"", measurements:[]}];
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

// Only inside a campaign tab: ask the campaign page to open this card's Test Report tab.
if (window.self !== window.top && window.parent.openReportTab) {
  const buildBtn = document.getElementById("buildReportBtn");
  buildBtn.style.display = "";
  buildBtn.addEventListener("click", () => {
    const file = new URLSearchParams(location.search).get("load") || "";
    window.parent.openReportTab(file.replace(/\\.test$/, "") || card.id);
  });
}
</script>
</body>
</html>
"""



_CAMPAIGN_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>\U0001F680 \u2728 STARFIRE</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=B612+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/starfire.css">
</head>
<body class="editor-doc">
<div id="tabbar"></div>
<div id="panels">
  <div class="panel active split" id="panel-campaign">
    <div class="report-side">
      <div class="side-title"><h1>STARFIRE</h1><div class="sub">Test Campaign Designer</div></div>
      <div class="side-btns">
        <button class="btn" id="loadCampBtn">import campaign file</button>
        <input type="file" id="loadCampFile" accept=".camp,application/json" style="display:none">
        <button class="btn" id="saveCampBtn">export campaign file (.camp)</button>
        <button class="btn" id="printCampBtn">generate .pdf</button>
        <button class="btn" id="docxCampBtn">export .docx</button>
        <button class="btn" id="genVcrmBtn">generate VCRM</button>
        <button class="btn" id="buildSrBtn">build campaign report</button>
      </div>
      <div id="saveStatus" class="hint"></div>
    </div>
    <div class="side-main"><div class="side-inner">

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

    <h2 class="no-rule">Modules</h2>
    <table class="datatable" id="modTable">
      <colgroup><col style="width:28%"><col><col style="width:16%"><col style="width:60px"></colgroup>
      <tr><th>Module</th><th>Description</th><th>Scope</th><th class="blank-th"></th></tr>
    </table>
    <div class="row"><button class="btn btn-plus" id="addModBtn" title="add module">+</button></div>
    <div id="modHint" class="hint"></div>

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

    </div></div>
  </div>
</div>

<script>
let camp = {
  id: "SC-001", title: "Untitled Campaign", revision: "A", author: "",
  date: new Date().toISOString().slice(0,10), project: "", test_lead: "",
  quality_assurance: "", system: "", subsystem: "", jira_epic_key: "",
  requirements: [], modules: [], tests: [], ancillary_tests: [], sections: [], reports: {}
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

// Modules: runnable files in dist/ (see /list_modules) that a test point's
// Activate button may run. Scope blank = any point; TC-001 = any point on
// that card; TC-001-20 = that point only.
let moduleFiles = [];
// Kind by name, mirroring how the server runs it (case-sensitive, like os.path.splitext):
// no extension = a binary, .py/.jl/.sh = that language, anything else = "other".
function moduleKind(name) {
  if (!name) return "";
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot) : "";
  return ext === "" ? "bin" : ({".py": "py", ".jl": "jl", ".sh": "sh"})[ext] || "other";
}
function renderModules() {
  const t = document.getElementById("modTable");
  t.querySelectorAll("tr.datarow").forEach(r => r.remove());
  camp.modules.forEach((m, i) => {
    // A saved module that's no longer in dist/ stays selectable, flagged, so it isn't silently dropped.
    const names = moduleFiles.includes(m.module) || !m.module ? moduleFiles : [m.module, ...moduleFiles];
    const opts = [`<option value="">[choose module]</option>`].concat(names.map(n =>
      `<option class="mod-${moduleKind(n)}" value="${esc(n)}"${n === m.module ? " selected" : ""}>${esc(n)}${moduleFiles.includes(n) ? "" : " (missing)"}</option>`));
    const tr = document.createElement("tr");
    tr.className = "datarow";
    tr.innerHTML = `
      <td><select class="mod-${moduleKind(m.module)}" data-i="${i}" data-f="module">${opts.join("")}</select></td>
      <td><input value="${esc(m.description)}" data-i="${i}" data-f="description" placeholder="optional"></td>
      <td><input value="${esc(m.scope)}" data-i="${i}" data-f="scope" placeholder="any test point"></td>
      <td><button class="btn mini btn-x" data-del="${i}">x</button></td>`;
    t.appendChild(tr);
  });
  t.querySelectorAll("input[data-i], select[data-i]").forEach(el => {
    el.addEventListener("input", () => {
      camp.modules[+el.dataset.i][el.dataset.f] = el.value;
      if (el.dataset.f === "module") el.className = "mod-" + moduleKind(el.value);
    });
  });
  t.querySelectorAll("button[data-del]").forEach(btn => {
    btn.addEventListener("click", () => { camp.modules.splice(+btn.dataset.del, 1); renderModules(); });
  });
  document.getElementById("modHint").textContent = moduleFiles.length
    ? "" : "No runnable files found. Put programs (chmod +x) or .py / .sh / .jl scripts in the dist/ folder next to starfire.";
}
async function loadModuleFiles() {
  try {
    const res = await fetch("/list_modules");
    const next = (await res.json()).modules || [];
    // Redrawing while a picker is open would close it, so only when the list actually changed.
    if (JSON.stringify(next) !== JSON.stringify(moduleFiles)) { moduleFiles = next; renderModules(); }
  } catch (err) { /* leave the list as it was */ }
}
document.getElementById("addModBtn").addEventListener("click", () => {
  camp.modules.push({module: "", description: "", scope: ""});
  renderModules();
  loadModuleFiles();
});
window.addEventListener("focus", loadModuleFiles);
// Test tabs (iframes) read the live list from here when they offer or run a module.
window.getCampaignModules = () => camp.modules;
window.getCampaignId = () => camp.id;

// Google Material Symbols, embedded inline (not a file/CDN) so they work
// fully offline; fill="currentColor" picks up each button's own hover color.
const ICON_ARROW_UPWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-160v-487L216-423l-56-57 320-320 320 320-56 57-224-224v487h-80Z"/></svg>';
const ICON_ARROW_DOWNWARD = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960" fill="currentColor"><path d="M440-800v487L216-537l-56 57 320 320 320-320-56-57-224 224v-487h-80Z"/></svg>';
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
    btn.addEventListener("click", () => { srSectionDeleted(+btn.dataset.del); camp.sections.splice(+btn.dataset.del, 1); renderSections(); });
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
    if (!camp.modules) camp.modules = [];
    if (!camp.reports) camp.reports = {};
    if (!camp.tests) camp.tests = [];
    if (!camp.ancillary_tests) camp.ancillary_tests = [];
    if (!camp.sections) camp.sections = [];
    if (!camp.jira_epic_key) camp.jira_epic_key = "";
    bindMeta(); renderRequirements(); renderModules(); renderTests(); renderAncillaryTests(); renderSections();
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
  updateAppendButtons();
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

function updateAppendButtons() {
  const on = openTabs.some(t => t.key === SR_KEY);
  document.querySelectorAll("[data-rappend], #appendVcrmBtn").forEach(b => { b.style.display = on ? "" : "none"; });
}

function switchTab(key) {
  activeTab = key;
  if (key === SR_KEY) { srSync(); renderSrSide(); renderSrPreview(); }
  if (key.startsWith("report-")) loadReport(key, key.slice(7));
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

// ---- Campaign report (SR) tab: header, sections, and appended VCRM / Test Reports ----
const SR_KEY = "__sr__";
function srIdFor(id) {
  const s = String(id || "").trim();
  return /^sc-/i.test(s) ? "SR-" + s.slice(3) : "SR-" + (s || "000");
}
function srHead() {
  return [srIdFor(camp.id), camp.title, camp.revision ? "REV " + camp.revision.toUpperCase() : ""].filter(Boolean).join(" ");
}
function srItemLabel(it) {
  if (it.type === "vcrm") return "VCRM";
  if (it.type === "report") return reportIdFor(it.card);
  const sec = it.idx !== undefined ? camp.sections[it.idx] : null;
  return (sec ? sec[0] : it.header) || "(untitled section)";
}

// The VCRM and Sections always come first; Test Reports follow (each group keeps its own order).
const srIsReport = it => it.type === "report";
function srOrder() {
  camp.campaign_report.items.sort((a, b) => srIsReport(a) - srIsReport(b));
}

// Sections stay live: every non-empty one the user hasn't removed from the report is in it.
function srSync() {
  const rep = camp.campaign_report;
  if (!rep) return;
  rep.dismissed = rep.dismissed || [];
  rep.items = rep.items.filter(it => it.type !== "section" || it.idx === undefined || camp.sections[it.idx]);
  camp.sections.forEach(([h, t], i) => {
    const has = rep.items.some(it => it.type === "section" && it.idx === i);
    if (!has && !rep.dismissed.includes(i) && ((h || "").trim() || (t || "").trim())) rep.items.push({type: "section", idx: i});
  });
  srOrder();
}

// A section deleted on the campaign tab shifts the ones after it.
function srSectionDeleted(i) {
  const rep = camp.campaign_report;
  if (!rep) return;
  const shift = n => n > i ? n - 1 : n;
  rep.items = rep.items.filter(it => !(it.type === "section" && it.idx === i));
  rep.items.forEach(it => { if (it.type === "section" && it.idx !== undefined) it.idx = shift(it.idx); });
  rep.dismissed = (rep.dismissed || []).filter(n => n !== i).map(shift);
}
const SR_KINDS = {section: "section", vcrm: "matrix", report: "test report"};

function srAppend(item) {
  if (!camp.campaign_report) return "No campaign report yet.";
  const items = camp.campaign_report.items;
  if (items.some(x => x.type === item.type && x.card === item.card)) return "Already in " + srIdFor(camp.id);
  items.push(item);
  srOrder();
  renderSrSide();
  renderSrPreview();
  return "Appended to " + srIdFor(camp.id);
}

function openSrTab() {
  if (!camp.campaign_report) camp.campaign_report = {items: [], dismissed: []};
  if (!openTabs.some(t => t.key === SR_KEY)) {
    openTabs.push({key: SR_KEY, label: srIdFor(camp.id)});
    const panel = document.createElement("div");
    panel.className = "test-panel report-panel";
    panel.id = "panel-" + SR_KEY;
    panel.innerHTML = `
      <div class="report-side">
        <div class="side-title"><h1>STARFIRE</h1><div class="sub">Test Campaign Designer</div></div>
        <div class="report-box"><div class="report-label">Campaign report for</div><div class="report-for sr-for"></div></div>
        <div class="report-box">
          <div class="report-label">Contents</div>
          <div class="sr-items"></div>
          <div class="report-hint">Open the VCRM or a Test Report and press + append to add it here.</div>
        </div>
        <div class="report-btns">
          <button class="btn" data-srrefresh>refresh</button>
          <button class="btn" data-srprint>print / pdf</button>
          <button class="btn" data-srdocx>.docx</button>
        </div>
        <div class="report-hint sr-status"></div>
      </div>
      <iframe class="report-frame" title="campaign report preview"></iframe>`;
    document.getElementById("panels").appendChild(panel);
    panel.querySelector("[data-srrefresh]").addEventListener("click", () => { srSync(); renderSrSide(); renderSrPreview(); });
    panel.querySelector("[data-srprint]").addEventListener("click", printSr);
    panel.querySelector("[data-srdocx]").addEventListener("click", docxSr);
  }
  switchTab(SR_KEY);
}

function renderSrSide() {
  const panel = document.getElementById("panel-" + SR_KEY);
  if (!panel || !camp.campaign_report) return;
  panel.querySelector(".sr-for").textContent = srHead();
  const items = camp.campaign_report.items, list = panel.querySelector(".sr-items");
  list.innerHTML = items.length ? items.map((it, i) => `
    <div class="sr-item">
      <span class="sr-label"><span class="report-fname">${esc(srItemLabel(it))}</span><span class="report-kind">${SR_KINDS[it.type] || ""}</span></span>
      <button class="btn mini btn-icon" data-srup="${i}" title="move up"${i === 0 || srIsReport(items[i - 1]) !== srIsReport(it) ? " disabled" : ""}>${ICON_ARROW_UPWARD}</button>
      <button class="btn mini btn-icon" data-srdown="${i}" title="move down"${i === items.length - 1 || srIsReport(items[i + 1]) !== srIsReport(it) ? " disabled" : ""}>${ICON_ARROW_DOWNWARD}</button>
      <button class="btn mini btn-icon btn-x" data-srdel="${i}" title="remove">${ICON_CLOSE}</button>
    </div>`).join("") : `<div class="report-hint">Nothing added yet.</div>`;
  const edit = (name, fn) => list.querySelectorAll(`button[data-${name}]`).forEach(b => b.addEventListener("click", () => {
    fn(+b.dataset[name]);
    renderSrSide();
    renderSrPreview();
  }));
  edit("srup", i => { [items[i - 1], items[i]] = [items[i], items[i - 1]]; });
  edit("srdown", i => { [items[i + 1], items[i]] = [items[i], items[i + 1]]; });
  edit("srdel", i => {
    const it = items[i];
    if (it.type === "section" && it.idx !== undefined) camp.campaign_report.dismissed.push(it.idx);
    items.splice(i, 1);
  });
}

async function renderSrPreview() {
  const panel = document.getElementById("panel-" + SR_KEY);
  if (!panel) return;
  const frame = panel.querySelector(".report-frame");
  const res = await fetch("/export/campaign_report/print", {method: "POST", body: JSON.stringify({campaign: camp, preview: true})});
  const scroll = frame.contentWindow ? frame.contentWindow.scrollY : 0;
  frame.onload = () => frame.contentWindow.scrollTo(0, scroll);
  frame.srcdoc = await res.text();
}

async function printSr() {
  const w = window.open("", "_blank");
  if (w) w.document.write("Generating...");
  try {
    const res = await fetch("/export/campaign_report/print", {method: "POST", body: JSON.stringify({campaign: camp, preview: false})});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (w) { w.document.open(); w.document.write(text); w.document.close(); }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
  }
}

async function docxSr() {
  const status = document.querySelector("#panel-" + SR_KEY + " .sr-status");
  try {
    const res = await fetch("/export/campaign_report/docx", {method: "POST", body: JSON.stringify({campaign: camp})});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    download(srIdFor(camp.id) + ".docx", await res.blob());
  } catch (err) {
    status.textContent = `Docx export failed: ${err}`;
  }
}
document.getElementById("buildSrBtn").addEventListener("click", openSrTab);

// ---- Test Report (TR) tabs: one card's saved .test file, rendered by the server ----
function reportIdFor(testId) {
  const id = String(testId || "").trim();
  return /^tc-/i.test(id) ? "TR-" + id.slice(3) : "TR-" + (id || "000");
}
const REPORT_KINDS = {csv: "csv", image: "image", txt: "text output", other: "listed by name"};
const reportState = {};   // tab key -> {card, files, active}

function openReportTab(testId) {
  const key = "report-" + testId.replace(/[^A-Za-z0-9._-]/g, "_") + ".test";
  if (!openTabs.some(t => t.key === key)) {
    openTabs.push({key, label: reportIdFor(testId)});
    const panel = document.createElement("div");
    panel.className = "test-panel report-panel";
    panel.id = "panel-" + key;
    panel.innerHTML = `
      <div class="report-side">
        <div class="side-title"><h1>STARFIRE</h1><div class="sub">Test Campaign Designer</div></div>
        <div class="report-box"><div class="report-label">Report for</div><div class="report-for"></div></div>
        <div class="report-box">
          <div class="report-label report-files-title">Files for point</div>
          <div class="report-files"></div>
          <div class="report-hint">Files named <span class="report-prefix"></span>-anything.ext appear here. Tick the ones to add to the report. Click a point in the report to switch.</div>
        </div>
        <div class="report-btns">
          <button class="btn" data-rrefresh>refresh</button>
          <button class="btn" data-rprint>print / pdf</button>
          <button class="btn" data-rdocx>.docx</button>
          <button class="btn" data-rappend style="display:none">+ append</button>
        </div>
        <div class="report-hint report-status"></div>
      </div>
      <iframe class="report-frame" title="test report preview"></iframe>`;
    document.getElementById("panels").appendChild(panel);
    panel.querySelector("[data-rrefresh]").addEventListener("click", () => loadReport(key, key.slice(7)));
    panel.querySelector("[data-rprint]").addEventListener("click", () => printReport(key));
    panel.querySelector("[data-rdocx]").addEventListener("click", () => docxReport(key));
    panel.querySelector("[data-rappend]").addEventListener("click", () => {
      const st = reportState[key];
      if (st && st.card) panel.querySelector(".report-status").textContent = srAppend({type: "report", card: st.card.id});
    });
  }
  switchTab(key);
}

function reportTicks(card) {
  camp.reports = camp.reports || {};
  return camp.reports[card.id] = camp.reports[card.id] || {};
}

function reportPlots(card) {
  const t = reportTicks(card);
  return t.plot = t.plot || {};
}

function reportPayload(key, preview) {
  const st = reportState[key];
  return {card: st.card, campaign: camp.id || "", files: reportTicks(st.card), preview};
}

async function loadReport(key, filename) {
  const panel = document.getElementById("panel-" + key);
  if (!panel) return;
  const st = reportState[key] || (reportState[key] = {active: null});
  const status = panel.querySelector(".report-status");
  try {
    const res = await fetch("/load?file=" + encodeURIComponent(filename));
    if (!res.ok) throw new Error(`${filename} not found. Save the card first.`);
    st.card = await res.json();
    const fr = await fetch(`/point_files?campaign=${encodeURIComponent(camp.id || "")}&card=${encodeURIComponent(st.card.id || "")}`);
    st.files = (await fr.json()).files || {};
    const seqs = (st.card.points || []).map(p => String(p.seq));
    if (!seqs.includes(st.active)) st.active = seqs[0] || null;
    status.textContent = "Shows the card as last saved.";
  } catch (err) {
    status.textContent = String(err.message || err);
    return;
  }
  renderReportSide(key);
  await renderReportPreview(key);
}

function renderReportSide(key) {
  const st = reportState[key], panel = document.getElementById("panel-" + key);
  if (!st || !st.card || !panel) return;
  panel.querySelector(".report-for").textContent = [reportIdFor(st.card.id), st.card.title, st.card.revision ? "REV " + st.card.revision.toUpperCase() : ""].filter(Boolean).join(" ");
  panel.querySelector(".report-files-title").textContent = "Files for point " + (st.active ?? "");
  panel.querySelector(".report-prefix").textContent = `${camp.id || "XX-000"}-${st.card.id}-${st.active}`;
  const files = st.files[st.active] || [];
  const ticked = reportTicks(st.card)[st.active] || [], plotted = reportPlots(st.card)[st.active] || [];
  const list = panel.querySelector(".report-files");
  list.innerHTML = files.length ? files.map((f, j) => `
    <label class="report-file"><input type="checkbox" data-rfile="${j}"${ticked.includes(f.name) ? " checked" : ""}>
      <span><span class="report-fname">${esc(f.name)}</span><span class="report-kind kind-${esc(f.kind)}">${REPORT_KINDS[f.kind] || ""}</span></span>${f.kind === "csv" && ticked.includes(f.name) ? `<button class="btn btn-plot${plotted.includes(f.name) ? " on" : ""}" data-rplot="${j}">plot</button>` : ""}</label>`).join("")
    : `<div class="report-hint">No files for this point yet.</div>`;
  list.querySelectorAll("input[data-rfile]").forEach(cb => cb.addEventListener("change", () => {
    const all = reportTicks(st.card), name = files[+cb.dataset.rfile].name;
    all[st.active] = (all[st.active] || []).filter(n => n !== name).concat(cb.checked ? [name] : []);
    if (!cb.checked) { const pl = reportPlots(st.card); pl[st.active] = (pl[st.active] || []).filter(n => n !== name); }
    renderReportSide(key);
    renderReportPreview(key);
  }));
  list.querySelectorAll("button[data-rplot]").forEach(b => b.addEventListener("click", e => {
    e.preventDefault();
    const pl = reportPlots(st.card), name = files[+b.dataset.rplot].name, cur = pl[st.active] || [];
    pl[st.active] = cur.includes(name) ? cur.filter(n => n !== name) : cur.concat([name]);
    renderReportSide(key);
    renderReportPreview(key);
  }));
}

async function renderReportPreview(key) {
  const st = reportState[key], panel = document.getElementById("panel-" + key);
  const frame = panel.querySelector(".report-frame");
  const res = await fetch("/export/report/print", {method: "POST", body: JSON.stringify(reportPayload(key, true))});
  const scroll = frame.contentWindow ? frame.contentWindow.scrollY : 0;
  frame.onload = () => {
    const doc = frame.contentDocument;
    frame.contentWindow.scrollTo(0, scroll);
    doc.querySelectorAll(".rp-point").forEach(sec => {
      sec.classList.toggle("rp-active", sec.dataset.seq === String(st.active));
      sec.addEventListener("click", () => {
        st.active = sec.dataset.seq;
        doc.querySelectorAll(".rp-point").forEach(x => x.classList.toggle("rp-active", x === sec));
        renderReportSide(key);
      });
    });
  };
  frame.srcdoc = await res.text();
}

async function printReport(key) {
  if (!reportState[key] || !reportState[key].card) return;
  // Opened before the await, so a popup blocker doesn't kill it.
  const w = window.open("", "_blank");
  if (w) w.document.write("Generating...");
  try {
    const res = await fetch("/export/report/print", {method: "POST", body: JSON.stringify(reportPayload(key, false))});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    const text = await res.text();
    if (w) { w.document.open(); w.document.write(text); w.document.close(); }
  } catch (err) {
    if (w) { w.document.open(); w.document.write("Failed to generate: " + err); w.document.close(); }
  }
}

async function docxReport(key) {
  const st = reportState[key];
  if (!st || !st.card) return;
  const status = document.getElementById("panel-" + key).querySelector(".report-status");
  try {
    const res = await fetch("/export/report/docx", {method: "POST", body: JSON.stringify(reportPayload(key, false))});
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    download(reportIdFor(st.card.id) + ".docx", await res.blob());
  } catch (err) {
    status.textContent = `Docx export failed: ${err}`;
  }
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
    panel.className = "panel split";
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
    <div class="report-side">
      <div class="side-title"><h1>STARFIRE</h1><div class="sub">Test Campaign Designer</div></div>
      <div class="report-box"><div class="report-label">Verification Cross-Reference Matrix for Test Campaign</div>
        <div class="report-for">${esc([camp.id, camp.title, camp.revision ? "REV " + camp.revision.toUpperCase() : ""].filter(Boolean).join(" "))}</div></div>
      <div class="side-btns">
        <button class="btn" id="saveVcrmBtn">export .vcrm file</button>
        <button class="btn" id="printVcrmBtn">generate .pdf</button>
        <button class="btn" id="docxVcrmBtn">export .docx</button>
        <button class="btn" id="csvVcrmBtn">export .csv</button>
        <button class="btn" id="appendVcrmBtn" style="display:none">+ append</button>
      </div>
      <div id="vcrmStatus" class="hint"></div>
    </div>
    <div class="side-main"><div class="side-inner">
    <div class="side-head">${esc([camp.id, camp.title, camp.revision ? "REV " + camp.revision.toUpperCase() : ""].filter(Boolean).join(" "))}</div>
    <div class="side-meta">
      <span><b>Campaign:</b> ${esc(camp.id)}</span>
      <span><b>Project:</b> ${esc(camp.project)}</span>
      <span><b>System/Subsystem:</b> ${esc(camp.system)} / ${esc(camp.subsystem)}</span>
    </div>
    <div class="side-meta">
      <span><b>Test Lead:</b> ${esc(camp.test_lead)}</span>
      <span><b>Quality Assurance:</b> ${esc(camp.quality_assurance)}</span>
      <span><b>Date:</b> ${esc(camp.date)}</span>
    </div>
    <h2 class="no-rule">Verification Cross-Reference Matrix</h2>
    <table class="datatable">
      <tr><th>Requirement</th><th>Description</th><th>Covering Tests</th><th>Status</th><th>Updated</th></tr>
      ${rows || '<tr><td colspan="5">No requirements entered on this campaign yet.</td></tr>'}
    </table>
    </div></div>`;

  const vcrmStatus = panel.querySelector("#vcrmStatus");
  panel.querySelector("#appendVcrmBtn").addEventListener("click", () => { vcrmStatus.textContent = srAppend({type: "vcrm"}); });
  updateAppendButtons();
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
  panel.querySelector("#csvVcrmBtn").addEventListener("click", async () => {
    try {
      const res = await fetch("/export/vcrm/csv", {method: "POST", body: JSON.stringify(currentVcrm)});
      if (!res.ok) throw new Error(`server returned ${res.status}`);
      download((currentVcrm.campaign_id || "vcrm") + "-test-matrix.csv", await res.text(), "text/csv");
    } catch (err) {
      vcrmStatus.textContent = `CSV export failed: ${err}`;
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
renderModules();
loadModuleFiles();
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
# turns Jira tickets on; an executable `starfire-hw` (built from starfire.cpp)
# turns Sample / Activate on -- without it they open the panel for manual
# entry and say nothing was sent.
JIRA_AVAILABLE = jira_ticket.load_config() is not None


def _program_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _modules_dir() -> str:
    # The dist/ folder: next to the built binary, or ./dist beside this
    # script when run from source (where build.sh puts its output).
    here = _program_dir()
    return here if getattr(sys, "frozen", False) else os.path.join(here, "dist")


def _find_hardware_binary() -> str:
    # Working directory first, then beside the program itself (and dist/ when
    # run from source), so a binary run from anywhere still finds the one built next to it.
    for d in (os.getcwd(), _program_dir(), _modules_dir()):
        path = os.path.join(d, "starfire-hw")
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return os.path.join(os.getcwd(), "starfire-hw")   # not found; HARDWARE_AVAILABLE says so


_HW_PATH = _find_hardware_binary()
HARDWARE_AVAILABLE = os.path.isfile(_HW_PATH) and os.access(_HW_PATH, os.X_OK)


# Modules: scripts/binaries the user drops into dist/ and registers in a
# campaign's Modules table; a test point's Activate button runs one.

# Starfire's own files live in dist/ too; none of them is a module.
_RESERVED_MODULES = {"starfire", "starfire-hw"}
# A file with one of these extensions is run by its interpreter, so it needs
# no execute bit or shebang. Anything else must be an executable file (a
# compiled binary, or a script with a shebang and chmod +x) and is run directly.
_INTERPRETERS = {".py": "python3", ".sh": "bash", ".jl": "julia"}
_MODULE_TIMEOUT_S = 3600      # a module may legitimately run a whole test
_MODULE_OUTPUT_LINES = 4096   # output lines kept; a longer run keeps its first and last half
_RUNS: dict = {}              # (card, point) -> running Popen, so BREAK can find it
_BROKEN: set = set()          # runs a BREAK was sent to
_RUNS_LOCK = threading.Lock()


def _cap_lines(text: str, limit: int = None) -> str:
    limit = limit or _MODULE_OUTPUT_LINES
    lines = text.splitlines(keepends=True)
    if len(lines) <= limit:
        return text
    half = limit // 2
    omitted = len(lines) - 2 * half
    return "".join(lines[:half]) + f"... {omitted} lines omitted ...\n" + "".join(lines[-half:])


def _is_module(d: str, name: str) -> bool:
    path = os.path.join(d, name)
    if name in _RESERVED_MODULES or name.startswith(".") or not os.path.isfile(path):
        return False
    return os.path.splitext(name)[1] in _INTERPRETERS or os.access(path, os.X_OK)


def list_modules() -> list:
    """Names of runnable files in dist/: regular, not hidden, not Starfire's
    own, and either an interpreted type (.py, .sh, .jl) or executable."""
    d = _modules_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted(n for n in names if _is_module(d, n))


def _module_env(card_id, point_seq, campaign_id="") -> dict:
    env = dict(os.environ, STARFIRE_CAMPAIGN=str(campaign_id or "XX-000"),
               STARFIRE_CARD=str(card_id), STARFIRE_POINT=str(point_seq))
    if HARDWARE_AVAILABLE:
        env["STARFIRE_HW"] = _HW_PATH
    if getattr(sys, "frozen", False):
        # The one-file bundle points LD_LIBRARY_PATH at its own libraries;
        # a system python3 or a compiled module inheriting that can crash on
        # a library mismatch. PyInstaller keeps the original in *_ORIG.
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def run_module(name: str, card_id: str = "", point_seq: str = "", campaign_id: str = "") -> dict:
    """Run one module from dist/ and wait for it. The name must be a bare
    filename that list_modules() reports -- never a path, so the browser can't
    point this at anything else. The module runs in the working directory
    (where .test files live) with STARFIRE_CAMPAIGN, STARFIRE_CARD, STARFIRE_POINT
    and, if found, STARFIRE_HW (path to starfire-hw) in its environment."""
    if name not in list_modules():
        return {"status": "failed", "error": f"{name!r} is not a runnable file in dist/"}
    argv = [os.path.join(_modules_dir(), name)]
    interpreter = _INTERPRETERS.get(os.path.splitext(name)[1])
    if interpreter:
        found = shutil.which(interpreter)
        if not found:
            return {"status": "failed", "error": f"{interpreter} not found on PATH (needed to run {name})"}
        argv.insert(0, found)
    key = (str(card_id), str(point_seq))
    started = time.monotonic()
    with _RUNS_LOCK:
        if key in _RUNS:
            return {"status": "failed", "error": "a module is already running on this point"}
        try:
            # Own session, so a timeout or BREAK can take down the module's children too.
            proc = subprocess.Popen(argv, cwd=os.getcwd(), env=_module_env(card_id, point_seq, campaign_id),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return {"status": "failed", "error": f"could not start {name}: {e}"}
        _RUNS[key] = proc
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=_MODULE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    finally:
        with _RUNS_LOCK:
            _RUNS.pop(key, None)
            broken = key in _BROKEN
            _BROKEN.discard(key)
    if proc.returncode == 0 and not timed_out:
        status = "complete"
    else:
        status = "broken" if broken else "failed"
    result = {
        "status": status,
        "exit_code": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "output": _cap_lines(out.decode("utf-8", "replace")),
    }
    if timed_out:
        result["error"] = f"timed out after {_MODULE_TIMEOUT_S} s"
    return result

def break_module(card_id, point_seq, grace: float = 3.0) -> dict:
    """SIGTERM the module running on this point (and its children); SIGKILL if it is still alive after `grace` s."""
    key = (str(card_id), str(point_seq))
    with _RUNS_LOCK:
        proc = _RUNS.get(key)
        if proc is None:
            return {"success": False, "error": "no module is running on this point"}
        _BROKEN.add(key)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"success": True}

    def _kill():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    threading.Timer(grace, _kill).start()
    return {"success": True}


def _fill_flags(html: str) -> str:
    return (html.replace("__JIRA_AVAILABLE__", "true" if JIRA_AVAILABLE else "false")
                .replace("__HARDWARE_AVAILABLE__", "true" if HARDWARE_AVAILABLE else "false"))

_TEST_HTML = _fill_flags(_TEST_HTML)
_CAMPAIGN_HTML = _fill_flags(_CAMPAIGN_HTML)

_POINT_FILE_EXTS = ("csv", "txt")
_POINT_FILE_MAX = 5_000_000   # characters

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
        elif path == "/list_modules":
            self._send_json(200, {"modules": list_modules()})
        elif path == "/point_files":
            qs = parse_qs(parsed.query)
            self._send_json(200, {"files": list_point_files((qs.get("campaign") or [""])[0], (qs.get("card") or [""])[0])})
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
        # {"port": "/dev/usbtmc0", "command": "MEAS:VOLT:DC?"}. starfire-hw
        # answers with one JSON line. A missing binary is a clean "not available".
        if not HARDWARE_AVAILABLE:
            self._send_json(404, {"success": False, "error": "starfire-hw not available"})
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
            # The binary enforces its own 5 s limit; this is the outer ceiling.
            proc = subprocess.run([_HW_PATH, "send", port, command],
                                   capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self._send_json(200, {"success": False, "error": "starfire-hw timed out"})
            return
        except Exception as e:
            self._send_json(200, {"success": False, "error": f"starfire-hw raised: {e}"})
            return
        try:
            line = json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            # Usage errors (exit 2) and crashes have no JSON line, only stderr.
            self._send_json(200, {"success": False, "error": proc.stderr.strip() or "unknown error"})
            return
        if line.get("ok"):
            self._send_json(200, {"success": True, "value": line.get("reply", ""), "kind": line.get("kind", "")})
        else:
            self._send_json(200, {"success": False, "error": line.get("error", "unknown error")})

    def _local_json_payload(self, raw_body: bytes):
        """For routes that run or write things: only accept a request a web page on
        another site couldn't have sent, a JSON content type (which forces a CORS
        preflight this server never answers) and our own Host header (which stops
        DNS rebinding). Returns the payload dict, or None after sending the error."""
        port = self.server.server_address[1]
        if (self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json"
                or self.headers.get("Host", "") not in (f"localhost:{port}", f"127.0.0.1:{port}")):
            self._send_json(403, {"status": "failed", "error": "forbidden"})
            return None
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(400, {"status": "failed", "error": f"bad request: {e}"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"status": "failed", "error": "bad request: expected a JSON object"})
            return None
        return payload

    def _handle_run_module(self, raw_body: bytes):
        payload = self._local_json_payload(raw_body)
        if payload is None:
            return
        self._send_json(200, run_module(str(payload.get("module") or ""), payload.get("card", ""),
                                        payload.get("point", ""), payload.get("campaign", "")))

    def _handle_break_module(self, raw_body: bytes):
        payload = self._local_json_payload(raw_body)
        if payload is None:
            return
        self._send_json(200, break_module(payload.get("card", ""), payload.get("point", "")))

    def _handle_save_point_file(self, raw_body: bytes):
        # {"campaign", "card", "point", "ext": "csv"|"txt", "content"} -> <campaign>-<card>-<point>.<ext>
        payload = self._local_json_payload(raw_body)
        if payload is None:
            return
        ext = payload.get("ext")
        content = payload.get("content")
        if ext not in _POINT_FILE_EXTS or not isinstance(content, str) or len(content) > _POINT_FILE_MAX:
            self._send_json(400, {"saved": False, "error": "bad request"})
            return
        stem = "-".join(re.sub(r"[^A-Za-z0-9._-]", "_", str(payload.get(k) or d))
                        for k, d in (("campaign", "XX-000"), ("card", "TC"), ("point", "0")))
        filename = _safe_filename(stem, "." + ext)
        try:
            with open(os.path.join(os.getcwd(), filename), "w", encoding="utf-8", newline="") as f:
                f.write(content)
        except OSError as e:
            self._send_json(500, {"saved": False, "error": str(e)})
            return
        self._send_json(200, {"saved": True, "filename": filename})

    def do_POST(self):
        path = urlparse(self.path).path
        raw = self._body()
        if path == "/capture_image":
            return self._handle_capture_image(raw)
        if path == "/create_jira_ticket":
            return self._handle_create_jira_ticket(raw)
        if path == "/measure":
            return self._handle_hardware(raw)
        if path == "/run_module":
            return self._handle_run_module(raw)
        if path == "/break_module":
            return self._handle_break_module(raw)
        if path == "/save_point_file":
            return self._handle_save_point_file(raw)
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
                self._send(200, "text/html", export_print_html(procedure_only(card), host).encode())
            elif path == "/export/tex":
                self._send(200, "text/x-tex", export_tex(card).encode())
            elif path == "/export/test/docx":
                self._send(200, _DOCX_MIME, export_test_docx(procedure_only(card)))
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
        # Test Report routes: {"card", "campaign", "files": {seq: [names]}, "preview"}
        elif path in ("/export/report/print", "/export/report/docx"):
            card = TestCard.from_dict(data["card"])
            camp_id, files = str(data.get("campaign") or ""), data.get("files") or {}
            if path == "/export/report/print":
                html = export_report_html(card, camp_id, files, host, preview=bool(data.get("preview")))
                self._send(200, "text/html", html.encode())
            else:
                self._send(200, _DOCX_MIME, export_report_docx(card, camp_id, files))
        # Campaign report (SR) routes: {"campaign": <the .camp dict>, "preview"}
        elif path in ("/export/campaign_report/print", "/export/campaign_report/docx"):
            camp = Campaign.from_dict(data["campaign"])
            if path.endswith("/print"):
                self._send(200, "text/html", export_campaign_report_html(camp, host, bool(data.get("preview"))).encode())
            else:
                self._send(200, _DOCX_MIME, export_campaign_report_docx(camp))
        # VCRM routes (the VCRM is plain JSON, not a dataclass)
        elif path == "/save_vcrm":
            self._write_file(vcrm_filename(data.get("campaign_id", "VCRM")), data)
        elif path == "/export/vcrm/print":
            self._send(200, "text/html", export_vcrm_print_html(data, host).encode())
        elif path == "/export/vcrm/csv":
            self._send(200, "text/csv", export_vcrm_csv(data).encode())
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
