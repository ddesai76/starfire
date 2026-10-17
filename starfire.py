#!/usr/bin/env python3

# starfire.py:    TEST CAMPAIGN / TEST CARD DESIGNER
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-16
# VERSION:        0.2.0
#

"""
Usage
-----
    python3 starfire.py                    # Open GUI on http://localhost:5791
    python3 starfire.py --port 8080
    python3 starfire.py --no-browser

"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import errno
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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


# Test card data model
@dataclass
class TestPoint:
    seq: int
    action: str = ""
    expected: str = ""
    observed: str = ""
    reference: str = ""
    responsible_party: str = ""
    date: str = ""
    time: str = ""        # 24h "HH:MM" -- filled by the UI's "now" button
    initial: str = ""
    quality: str = ""
    locked: bool = False
    flagged: bool = False
    images: list = field(default_factory=list)
    files: list = field(default_factory=list)      # uploaded with the + button
    usb_port: str = ""
    measure_command: str = ""
    measurements: list = field(default_factory=list)


def _normalize_pair_list(items: list, width: int) -> list:
    """Shape normalization for nomenclature/references/
    notes/warnings: pads each row to `width` elements, and
    accepts a bare string in place of a single-element row."""
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
    article: str = ""
    requirements: str = ""
    points: list = field(default_factory=list)         # list[TestPoint]
    nomenclature: list = field(default_factory=list)
    references: list = field(default_factory=list)
    notes: list = field(default_factory=list)       # list[[text, locked]]
    warnings: list = field(default_factory=list)     # list[[text, locked]]
    locked: bool = False
    files_migrated: bool = False   # set once files already named for a point were attached

    @staticmethod
    def from_dict(d: dict) -> "TestCard":
        # Unknown keys are dropped, so files saved by an older version still open.
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


def sorted_nomenclature(card: TestCard) -> list:
    # card.nomenclature as a list of (symbol, definition, units, locked),alphabetized.
    rows = [(r[0], r[1], r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else False)
            for r in card.nomenclature]
    return sorted(rows, key=lambda row: row[0].lower())


# LaTeX Greek-letter commands -> Unicode, for the print/HTML view.
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
    become their Unicode letter; anything else passes unchanged."""
    return _GREEK_CMD_RE.sub(lambda m: GREEK_UNICODE.get(m.group(1), m.group(0)), sym or "")


# Export as CSV
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


# Export as print-ready HTML (browser print-to-PDF, no new dependency).
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


# Campaign data model
@dataclass
class CampaignTestRow:
    seq: int
    test_id: str = ""
    title: str = ""
    author: str = ""
    requirements: str = ""


@dataclass
class CampaignModule:
    # A script or binary in the program's dist/ folder that a test point's Activate button can run. 
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
    requirements: list = field(default_factory=list)   # [code, description, source, method]
    # Modules a test point's Activate button can run.
    modules: list = field(default_factory=list)         # list[CampaignModule]
    tests: list = field(default_factory=list)           # list[CampaignTestRow] -- "Test Series"
    ancillary_tests: list = field(default_factory=list)   # list[CampaignTestRow]
    sections: list = field(default_factory=list)         # list[(header, text)]
    jira_epic_key: str = ""
    reports: dict = field(default_factory=dict)
    # The campaign report (SR-xxx), once built: {"items": [section | vcrm | report, ...]}.
    campaign_report: dict = None

    @staticmethod
    def from_dict(d: dict) -> "Campaign":
        row_known = {f.name for f in fields(CampaignTestRow)}   # drops the retired "status" key
        tests = [CampaignTestRow(**{k: v for k, v in t.items() if k in row_known}) for t in d.get("tests", [])]
        ancillary_tests = [CampaignTestRow(**{k: v for k, v in t.items() if k in row_known})
                           for t in d.get("ancillary_tests", [])]
        known = {f.name for f in fields(CampaignModule)}
        modules = [CampaignModule(**{k: v for k, v in m.items() if k in known}) for m in d.get("modules", [])]
        base = {k: v for k, v in d.items() if k not in ("tests", "ancillary_tests", "modules", "system", "subsystem")}
        return Campaign(tests=tests, ancillary_tests=ancillary_tests, modules=modules, **base)

    def to_dict(self) -> dict:
        return asdict(self)


STATUS_LABELS = {"TO_DO": "TO DO", "IN_PROGRESS": "IN PROGRESS", "COMPLETE": "COMPLETE"}

# A point's sign-off quality, worst to best. RFI (ready for issue) is above PASS.
RESULTS = ["FAIL", "CAUTION", "PASS", "RFI"]


def _quality(p) -> str:
    return (p.quality or "").upper()


def _is_run(p) -> bool:
    return bool((p.initial or "").strip()) and _quality(p) in RESULTS


def card_state(card: TestCard) -> dict:
    """Progress and worst result of a card, from its points' initials and quality.
    COMPLETE needs every point run and the last point RFI."""
    pts = sorted(card.points, key=lambda p: p.seq)
    run = [p for p in pts if _is_run(p)]
    if not run:
        progress = "TO_DO"
    elif len(run) == len(pts) and _quality(pts[-1]) == "RFI":
        progress = "COMPLETE"
    else:
        progress = "IN_PROGRESS"
    return {"progress": progress, "result": min((_quality(p) for p in run), key=RESULTS.index, default="")}


def status_label(state: dict) -> str:
    label = STATUS_LABELS[state["progress"]]
    return f"{label} ({state['result']})" if state["result"] else label


def card_warnings(card: TestCard) -> list:
    """Sign-off problems on one card, as short phrases."""
    pts = sorted(card.points, key=lambda p: p.seq)
    out = []
    for p in pts:
        initialed, q = bool((p.initial or "").strip()), _quality(p)
        if q in RESULTS and not initialed:
            out.append(f"point {p.seq} is {q} but has no initials")
        elif initialed and q not in RESULTS:
            out.append(f"point {p.seq} is initialed but has no PASS, CAUTION, FAIL or RFI")
        elif q in ("CAUTION", "FAIL"):
            out.append(f"point {p.seq} is {q}")
    if pts and all(_is_run(p) for p in pts) and _quality(pts[-1]) != "RFI":
        out.append("all points are run but the last is not RFI")
    elif pts and _is_run(pts[-1]) and _quality(pts[-1]) == "RFI" and not all(_is_run(p) for p in pts):
        out.append("the last point is RFI but earlier points are not run")
    return out


def _read_card(test_id: str):
    """The TestCard in the working directory for a Test ID, or None if missing or unreadable."""
    try:
        with open(os.path.join(os.getcwd(), test_filename(test_id)), "r", encoding="utf-8") as f:
            return TestCard.from_dict(json.loads(f.read()))
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def campaign_statuses(camp: Campaign) -> dict:
    """Test ID -> computed status label for every row of both tests tables ("" if no .test file)."""
    out = {}
    for t in list(camp.tests) + list(camp.ancillary_tests):
        tid = (t.test_id or "").strip()
        if tid and tid not in out:
            card = _read_card(tid)
            out[tid] = status_label(card_state(card)) if card else ""
    return out


def next_test_id(camp: Campaign) -> str:
    """The next TC-nnn not used by a test row of the campaign or by a .test file here."""
    used = {(t.test_id or "").strip() for t in list(camp.tests) + list(camp.ancillary_tests)}
    n = 1
    while f"TC-{n:03d}" in used or os.path.exists(os.path.join(os.getcwd(), test_filename(f"TC-{n:03d}"))):
        n += 1
    return f"TC-{n:03d}"


def create_test(camp: Campaign, codes: list) -> dict:
    """Write an empty, untitled card that verifies the given requirements; returns its row fields."""
    known = {c for c, _, _, _ in requirement_rows(camp)}
    codes = [c for c in dict.fromkeys(str(c).strip() for c in codes) if c in known]
    if not codes:
        raise ValueError("no known requirement codes given")
    card = TestCard(id=next_test_id(camp), title="", author=camp.test_lead or camp.author, project=camp.project,
                    quality_assurance=camp.quality_assurance, requirements=", ".join(codes))
    with open(os.path.join(os.getcwd(), test_filename(card.id)), "w", encoding="utf-8") as f:
        json.dump(card.to_dict(), f, indent=2)
    return {"id": card.id, "title": "", "author": card.author, "requirements": card.requirements}


def test_filename(test_id: str) -> str:
    # The .test filename a campaign's Tests-table row links to, derived from Test ID rather than stored separately. 
    return _safe_filename(test_id, ".test")


def vcrm_filename(campaign_id: str) -> str:
    return _safe_filename(campaign_id, ".vcrm")


METHODS = ["T", "A", "D", "I"]   # Test, Analysis, Demonstration, Inspection


def requirement_rows(camp: Campaign) -> list:
    """(code, source, description, method) per requirement. Rows are stored as
    [code, description, source, method]; older files have only the first two."""
    out = []
    for r in camp.requirements:
        r = list(r) + [""] * (4 - len(r))
        out.append(((r[0] or "").strip(), r[2] or "", r[1] or "", r[3] or "T"))
    return out


def compute_vcrm(camp: Campaign) -> dict:
    """Verification Cross-Reference Matrix: requirement -> which Test
    Series tests verify it, with each test's computed status. One row per
    requirement and verifying test. Read-only and computed fresh each time
    from the campaign's Requirements table and each referenced test's own
    `requirements` field and point sign-offs. Ancillary Tests are excluded.
    """
    reqs = requirement_rows(camp)
    codes = [code for code, _, _, _ in reqs]
    cards: dict = {}        # test_id -> (card, state), or None if there is no readable .test file
    warnings = []
    for t in camp.tests:
        tid = (t.test_id or "").strip()
        if not tid or tid in cards:
            continue
        card = _read_card(tid)
        cards[tid] = (card, card_state(card)) if card else None
        if not card:
            warnings.append(f"{tid}: no {test_filename(tid)} file")
            continue
        tokens = [tok.strip() for tok in (card.requirements or "").split(",") if tok.strip()]
        if not tokens:
            warnings.append(f"{tid}: lists no requirements")
        warnings += [f"{tid}: {tok} is not in the Requirements table" for tok in tokens if tok not in codes]
        warnings += [f"{tid}: {g}" for g in card_warnings(card)]

    updated = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for req_code, source, req_desc, method in reqs:
        if not req_code:
            continue
        head = {"requirement": req_code, "source": source, "description": req_desc, "method": method}
        found = False
        for t in camp.tests:
            entry = cards.get((t.test_id or "").strip())
            if not entry:
                continue
            card, state = entry
            if req_code in [tok.strip() for tok in (card.requirements or "").split(",")]:
                found = True
                run = any(_is_run(p) for p in card.points)
                rows.append({**head, "event": f"{t.test_id} {card.title or t.title}".strip(),
                             "report": report_id(card.id) if run else "NONE", "status": status_label(state),
                             "updated": updated})
        if not found:
            rows.append({**head, "event": "NONE", "report": "NONE", "status": "--", "updated": updated})
            warnings.append(f"{req_code}: no test covers it")

    return {
        "campaign_id": camp.id, "campaign_title": camp.title,
        "generated": updated,
        "rows": rows, "warnings": warnings,
    }


# Export: print-ready HTML (same visual system as the test card's).
def export_campaign_print_html(camp: Campaign, host: str = "localhost") -> str:
    h = _h
    reqs = [r for r in requirement_rows(camp) if r[0] or r[2]]
    req_rows = "\n".join(f"<tr><td>{h(c)}</td><td>{h(src)}</td><td>{h(d)}</td><td>{h(m)}</td></tr>"
                         for c, src, d, m in reqs)
    req_section = "" if not reqs else f"""<h2>Requirements</h2>
<table><tr><th style="width:20%">Requirement</th><th style="width:16%">Source</th><th>Description</th><th style="width:8%">Method</th></tr>
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
    statuses = campaign_statuses(camp)
    test_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(statuses.get((t.test_id or '').strip(), ''))}</td></tr>"
        for t in camp.tests
    )
    anc_rows = "\n".join(
        f"<tr><td>{t.seq}</td><td>{h(t.test_id)}</td><td>{h(t.title)}</td><td>{h(t.author)}</td>"
        f"<td>{h(t.requirements)}</td><td>{h(statuses.get((t.test_id or '').strip(), ''))}</td></tr>"
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


VCRM_COLUMNS = [("Requirement", "requirement"), ("Source", "source"), ("Description", "description"),
                ("Method", "method"), ("Verification Event", "event"), ("Report", "report"), ("Status", "status"),
                ("Updated", "updated")]


def _vcrm_table_html(vcrm: dict) -> str:
    head = "".join(f"<th>{title}</th>" for title, _ in VCRM_COLUMNS)
    rows = "\n".join("<tr>" + "".join(f"<td>{_h(r.get(key, ''))}</td>" for _, key in VCRM_COLUMNS) + "</tr>"
                     for r in vcrm["rows"])
    return f"<table><tr>{head}</tr>\n{rows}\n</table>"


def export_vcrm_csv(vcrm: dict) -> str:
    # The VCRM as a Test Matrix: one row per requirement and verification event.
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([title for title, _ in VCRM_COLUMNS])
    for r in vcrm["rows"]:
        w.writerow([r.get(key, "") for _, key in VCRM_COLUMNS])
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
        f"ID: {camp.id}    Rev: {camp.revision}    Project: {camp.project}    Author: {camp.author}\n"
        f"Test Lead: {camp.test_lead}    Quality Assurance: {camp.quality_assurance}    "
        f"Date: {camp.date}"
    ).font.size = Pt(9.5)

    reqs = [r for r in requirement_rows(camp) if r[0] or r[2]]
    if reqs:
        doc.add_heading("Requirements", level=1)
        _add_table(doc, ["Requirement", "Source", "Description", "Method"], reqs)

    mods = [m for m in camp.modules if (m.module or "").strip()]
    if mods:
        doc.add_heading("Modules", level=1)
        _add_table(doc, ["Module", "Description", "Scope"],
                   [(m.module, m.description, m.scope or "any") for m in mods])

    statuses = campaign_statuses(camp)
    doc.add_heading("Test Series", level=1)
    _add_table(
        doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
        [(t.seq, t.test_id, t.title, t.author, t.requirements, statuses.get((t.test_id or "").strip(), ""))
         for t in camp.tests],
    )

    if camp.ancillary_tests:
        doc.add_heading("Ancillary Tests", level=1)
        _add_table(
            doc, ["#", "Test ID", "Title", "Author", "Requirements", "Status"],
            [(t.seq, t.test_id, t.title, t.author, t.requirements, statuses.get((t.test_id or "").strip(), ""))
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
    _add_table(doc, [title for title, _ in VCRM_COLUMNS],
               [[r.get(key, "") for _, key in VCRM_COLUMNS] for r in vcrm["rows"]])


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
        p["measurements"], p["images"], p["files"] = [], [], []
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
    # Yield (kind, payload) for each item that has something to show; a test with no saved .test file is skipped.
    items = (camp.campaign_report or {}).get("items", [])
    for it in sorted(items, key=lambda x: x.get("type") == "report"):   # Test Reports always follow the rest
        kind = it.get("type")
        if kind == "section":
            idx = it.get("idx")
            if idx is not None:   # Live reference to the campaign's Sections
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
        f"Campaign: {camp.id}    Project: {camp.project}\n"
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


def _own_file(campaign_id: str, card_id: str, name) -> bool:
    # A bare name of an existing file starting <campaign>-<card>-.
    return (isinstance(name, str) and name == os.path.basename(name)
            and name.startswith(_point_prefix(campaign_id, card_id) + "-") and os.path.isfile(os.path.join(os.getcwd(), name)))


def _point_file_pattern(campaign_id: str, card_id: str):
    return re.compile(r"^" + re.escape(_point_prefix(campaign_id, card_id)) + r"-(\d+)(?:-[^/]*)?\.[A-Za-z0-9]+$")


def list_point_files(campaign_id: str, card_id: str, attached: dict = None) -> dict:
    """{seq: [{"name", "kind"}]}. With `attached` ({seq: [names]}, a point's own list) it
    returns those that are files of this card. Without it, every file in the working folder
    named <campaign>-<card>-<point>[-anything].<ext>, to attach files that predate the list."""
    out = {}
    if attached is not None:
        for seq, names in attached.items():
            for n in names if isinstance(names, list) else []:
                if _own_file(campaign_id, card_id, n):
                    out.setdefault(str(seq), []).append({"name": n, "kind": _file_kind(n)})
        return out
    pat = _point_file_pattern(campaign_id, card_id)
    try:
        names = sorted(os.listdir(os.getcwd()))
    except OSError:
        return out
    for n in names:
        m = pat.match(n)
        if m and os.path.isfile(n):
            out.setdefault(m.group(1), []).append({"name": n, "kind": _file_kind(n)})
    return out


_CARD_FILE_MAX = 25_000_000   # bytes


def add_card_file(campaign_id: str, card_id: str, point, name: str, data: bytes) -> dict:
    """Save an uploaded file as <campaign>-<card>-<point>-<name> (a name already starting
    <campaign>-<card>- is kept); never replaces an existing file with different contents."""
    prefix = _point_prefix(campaign_id, card_id) + "-"
    seq = str(point)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(str(name or "")).strip()).lstrip(".")
    target = base if base.startswith(prefix) else f"{prefix}{seq}-{base}"
    if not seq.isdigit() or not base or len(target) > 200:
        return {"saved": False, "error": "bad file name"}
    if len(data) > _CARD_FILE_MAX:
        return {"saved": False, "error": f"file is over {_CARD_FILE_MAX // 1_000_000} MB"}
    path = os.path.join(os.getcwd(), target)
    try:
        with open(path, "xb") as f:
            f.write(data)
    except FileExistsError:
        # Picking a file already in the folder (same name, same bytes) just attaches it.
        with open(path, "rb") as f:
            if f.read(_CARD_FILE_MAX + 1) == data:
                return {"saved": True, "filename": target}
        return {"saved": False, "error": f"{target} already exists with different contents"}
    except OSError as e:
        return {"saved": False, "error": str(e)}
    return {"saved": True, "filename": target}


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
    # A .txt that is comma-separated with at least two plottable rows ("# ..." lines are ignored).
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
    # Ticked names for a point, kept only if the point still has them attached.
    own = next((p.files for p in card.points if str(p.seq) == str(seq)), [])
    return [n for n in (files or {}).get(str(seq), []) if n in own and _own_file(campaign_id, card.id, n)]


def _plotted(files: dict, seq) -> list:
    # Names the user chose to plot; stored under files["plot"] as {seq: [names]}.
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
    # One Test Report as an HTML fragment (header through test points).
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






# HTTP server

def _safe_filename(stem: str, ext: str) -> str:
    """Turn a card id into a filesystem-safe filename, confined to the
    current directory (no path separators from user-controlled input)."""
    stem = os.path.basename((stem or "card").strip()) or "card"
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return stem + ext




# Optional integrations, each checked once at startup: jira_config.json turns Jira tickets on;
# an executable `starfire-hw` (built from starfire.cpp) turns Sample / Activate on.


def _program_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _modules_dir() -> str:
    # The dist/ folder: next to the built binary, or ./dist beside this script when run from source (where starfi.sh puts its output).
    here = _program_dir()
    return here if getattr(sys, "frozen", False) else os.path.join(here, "dist")


# Jira tickets: on only if jira_config.json is in the working directory or next to the program.
JIRA_CONFIG_FILENAME = "jira_config.json"

JIRA_CONFIG_TEMPLATE = {
    "base_url": "https://yourteam.atlassian.net",
    "email": "you@example.com",
    "api_token": "",
    "project_key": "PROJ",
    # Bug for test point tickets, Task for campaign test rows.
    "issue_type": "Bug",
    "test_issue_type": "Task",
    # Set automatically after the first ticket with an epic key.
    "epic_link_field": None,
}


def _jira_config_path() -> str:
    # Working directory first, then beside the program (dist/ when run from source).
    for d in (os.getcwd(), _program_dir(), _modules_dir()):
        p = os.path.join(d, JIRA_CONFIG_FILENAME)
        if os.path.isfile(p):
            return p
    return os.path.join(os.getcwd(), JIRA_CONFIG_FILENAME)


def jira_load_config() -> dict | None:
    path = _jira_config_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    required = ("base_url", "email", "api_token", "project_key")
    if not all(cfg.get(k) for k in required):
        return None
    cfg["issue_type"] = cfg.get("issue_type") or "Bug"
    cfg["test_issue_type"] = cfg.get("test_issue_type") or "Task"
    cfg["epic_link_field"] = cfg.get("epic_link_field") or None
    return cfg


def _jira_save_epic_field(field_name: str) -> None:
    # Best-effort cache; a failed write just means re-detecting next time.
    path = _jira_config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["epic_link_field"] = field_name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def _jira_auth_header(cfg: dict) -> str:
    token = base64.b64encode(f"{cfg['email']}:{cfg['api_token']}".encode()).decode()
    return f"Basic {token}"


def _jira_api_request(cfg: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = cfg["base_url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _jira_auth_header(cfg))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"errorMessages": [raw.decode(errors="replace")]}
    except urllib.error.URLError as e:
        return 0, {"errorMessages": [str(e.reason)]}


def _jira_error_text(body: dict) -> str:
    parts = list(body.get("errorMessages") or [])
    for field, msg in (body.get("errors") or {}).items():
        parts.append(f"{field}: {msg}")
    return "; ".join(parts) if parts else "unknown Jira error"


def _jira_find_epic_field(cfg: dict) -> str | None:
    """Classic projects: find the custom field named 'Epic Link'."""
    status, body = _jira_api_request(
        cfg, "GET",
        f"/rest/api/2/issue/createmeta?projectKeys={cfg['project_key']}&expand=projects.issuetypes.fields",
    )
    if status != 200:
        return None
    for project in body.get("projects", []):
        for issuetype in project.get("issuetypes", []):
            for field_id, field_meta in issuetype.get("fields", {}).items():
                if (field_meta.get("name") or "").strip().lower() == "epic link":
                    return field_id
    return None


def jira_create_ticket(summary: str, description: str, epic_key: str = "",
                    issue_type: str | None = None) -> dict:
    """Returns {"success": True, "key", "url"} or {"success": False, "error"}; never raises.
    issue_type overrides the config's issue_type for this call."""
    cfg = jira_load_config()
    if not cfg:
        return {"success": False,
                 "error": f"No usable {JIRA_CONFIG_FILENAME} found "
                          f"(or it's missing base_url/email/api_token/project_key)."}

    issue = {
        "project": {"key": cfg["project_key"]},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type or cfg["issue_type"]},
    }

    epic_key = (epic_key or "").strip()
    if epic_key:
        cached = cfg.get("epic_link_field")
        if cached:
            issue[cached] = epic_key if cached != "parent" else {"key": epic_key}
        else:
            # Team-managed projects first; no metadata lookup needed.
            issue["parent"] = {"key": epic_key}

    status, body = _jira_api_request(cfg, "POST", "/rest/api/2/issue", {"fields": issue})

    if status == 201:
        if epic_key and not cfg.get("epic_link_field"):
            _jira_save_epic_field("parent")
        key = body.get("key", "")
        return {"success": True, "key": key, "url": f"{cfg['base_url'].rstrip('/')}/browse/{key}"}

    # "parent" failed: likely a classic project, so retry with the Epic Link field.
    if epic_key and not cfg.get("epic_link_field") and "parent" in issue:
        custom_field = _jira_find_epic_field(cfg)
        if custom_field:
            issue.pop("parent")
            issue[custom_field] = epic_key
            status2, body2 = _jira_api_request(cfg, "POST", "/rest/api/2/issue", {"fields": issue})
            if status2 == 201:
                _jira_save_epic_field(custom_field)
                key = body2.get("key", "")
                return {"success": True, "key": key,
                         "url": f"{cfg['base_url'].rstrip('/')}/browse/{key}"}
            return {"success": False, "error": _jira_error_text(body2)}

    return {"success": False, "error": _jira_error_text(body)}


JIRA_AVAILABLE = jira_load_config() is not None


def _static_dir() -> str:
    # Beside this script; in a PyInstaller build the bundled data is in sys._MEIPASS instead.
    base = sys._MEIPASS if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS") else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "static")


def _read_static(name: str) -> str:
    with open(os.path.join(_static_dir(), name), encoding="utf-8") as f:
        return f.read()


def _find_hardware_binary() -> str:
    # Working directory first, then beside the program itself (and dist/ when run from source).
    for d in (os.getcwd(), _program_dir(), _modules_dir()):
        path = os.path.join(d, "starfire-hw")
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return os.path.join(os.getcwd(), "starfire-hw")   # Not found


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


_MODULE_ARGS_MAX = 4096


def _parse_args(text) -> list:
    """Split a command-line string the way a POSIX shell would (quotes, backslashes),
    without running one. Raises ValueError on bad quoting, NUL or an oversized line."""
    text = "" if text is None else str(text)
    if len(text) > _MODULE_ARGS_MAX or "\0" in text:
        raise ValueError("arguments too long or contain a NUL byte")
    return shlex.split(text)


def _point_files_state(campaign_id: str, card_id: str, point_seq) -> dict:
    # {name: (mtime, size)} of the files named for this point, to see what a run wrote.
    pat = _point_file_pattern(campaign_id, card_id)
    state = {}
    try:
        for n in os.listdir(os.getcwd()):
            m = pat.match(n)
            if m and m.group(1) == str(point_seq) and os.path.isfile(n):
                st = os.stat(n)
                state[n] = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return state


def run_module(name: str, card_id: str = "", point_seq: str = "", campaign_id: str = "", args="") -> dict:
    """Run one module from dist/ and wait for it. The name must be a bare
    filename that list_modules() reports -- never a path, so the browser can't
    point this at anything else. The module runs in the working directory
    (where .test files live) with STARFIRE_CAMPAIGN, STARFIRE_CARD, STARFIRE_POINT
    and, if found, STARFIRE_HW (path to starfire-hw) in its environment.
    The result's "files" lists the files named for this point that the run created or changed.
    args is one command-line string, split into plain argv entries (no shell),
    so it reaches a module in any language the same way."""
    if name not in list_modules():
        return {"status": "failed", "error": f"{name!r} is not a runnable file in dist/"}
    try:
        extra = _parse_args(args)
    except ValueError as e:
        return {"status": "failed", "error": f"bad arguments: {e}"}
    argv = [os.path.join(_modules_dir(), name)]
    interpreter = _INTERPRETERS.get(os.path.splitext(name)[1])
    if interpreter:
        found = shutil.which(interpreter)
        if not found:
            return {"status": "failed", "error": f"{interpreter} not found on PATH (needed to run {name})"}
        argv.insert(0, found)
    argv += extra
    key = (str(card_id), str(point_seq))
    started = time.monotonic()
    before = _point_files_state(campaign_id, card_id, point_seq)
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
        "files": sorted(n for n, st in _point_files_state(campaign_id, card_id, point_seq).items() if before.get(n) != st),
    }
    if timed_out:
        result["error"] = f"timed out after {_MODULE_TIMEOUT_S} s"
    return result

def break_module(card_id, point_seq, grace: float = 3.0) -> dict:
    # SIGTERM the module running on this point; SIGKILL if it is still alive after `grace` s.
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

_TEST_HTML = _fill_flags(_read_static("test.html"))
_CAMPAIGN_HTML = _fill_flags(_read_static("campaign.html"))

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
            try:
                attached = json.loads(qs["attached"][0]) if "attached" in qs else None
            except json.JSONDecodeError:
                attached = {}
            self._send_json(200, {"files": list_point_files((qs.get("campaign") or [""])[0], (qs.get("card") or [""])[0],
                                                              attached if isinstance(attached, dict) or attached is None else {})})
        elif path == "/list_files":
            # Lists files in the working directory by extension.
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
        safe = os.path.basename(filename)
        file_path = os.path.join(_static_dir(), safe)
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
        # Payload is {"data": "data:image/jpeg;base64,..."}, not a card.
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
        # Millisecond resolution to deconflict a quick burst of captures.
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
            result = jira_create_ticket(
                payload.get("summary", ""), payload.get("description", ""),
                payload.get("epic_key", ""), payload.get("issue_type") or None,
            )
        except Exception as e:
            result = {"success": False, "error": f"Jira ticket creation raised: {e}"}
        self._send_json(200, result)

    def _handle_hardware(self, raw_body: bytes):
        # {"port": "/dev/usbtmc0", "command": "MEAS:VOLT:DC?"}. starfire-hw
        # answers with one JSON line. A missing binary is a "not available".
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
            # The binary enforces its own 5 s limit; this is an outer ceiling.
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
        """For routes that run or write: only accept a request a web page on
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
                                        payload.get("point", ""), payload.get("campaign", ""),
                                        payload.get("args", "")))

    def _handle_add_card_file(self, raw_body: bytes):
        # {"campaign", "card", "point", "name", "data": base64} -> <campaign>-<card>-<point>-<name>
        payload = self._local_json_payload(raw_body)
        if payload is None:
            return
        try:
            data = base64.b64decode(payload.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            self._send_json(400, {"saved": False, "error": "bad request"})
            return
        r = add_card_file(str(payload.get("campaign") or ""), str(payload.get("card") or ""),
                          payload.get("point"), payload.get("name"), data)
        self._send_json(200 if r["saved"] else 400, r)

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
        if path == "/add_card_file":
            return self._handle_add_card_file(raw)
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
        if path in ("/export/csv", "/export/print", "/export/test/docx", "/save"):
            card = TestCard.from_dict(data)
            if path == "/export/csv":
                self._send(200, "text/csv", export_csv(card).encode())
            elif path == "/export/print":
                self._send(200, "text/html", export_print_html(procedure_only(card), host).encode())
            elif path == "/export/test/docx":
                self._send(200, _DOCX_MIME, export_test_docx(procedure_only(card)))
            else:
                self._write_file(_safe_filename(card.id, ".test"), card.to_dict())
        # Campaign routes
        elif path in ("/save_campaign", "/export/campaign/print", "/export/campaign/docx", "/vcrm", "/campaign_status"):
            camp = Campaign.from_dict(data)
            if path == "/save_campaign":
                self._write_file(_safe_filename(camp.id, ".camp"), camp.to_dict())
            elif path == "/export/campaign/print":
                self._send(200, "text/html", export_campaign_print_html(camp, host).encode())
            elif path == "/export/campaign/docx":
                self._send(200, _DOCX_MIME, export_campaign_docx(camp))
            elif path == "/campaign_status":
                self._send_json(200, campaign_statuses(camp))
            else:
                self._send_json(200, compute_vcrm(camp))
        # Create test: {"campaign": <the .camp dict>, "requirements": [codes]}
        elif path == "/create_test":
            self._send_json(200, create_test(Campaign.from_dict(data["campaign"]), data["requirements"]))
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
        print("WARNING: Running as root. sudo is not needed to run this --")
        print("         only to clear out a root-owned dist/ from an earlier")
        print("         build, if that ever comes up. Run it as your normal")
        print("         user instead: root has no desktop session of its own,")
        print("         so the browser likely will fail to open correctly,")
        print("         and any files this writes would end up root-owned as well.\n")

    url = f"http://localhost:{args.port}"
    try:
        server = ThreadingHTTPServer(("localhost", args.port), _Handler)
    except OSError as e:
        print(f"ERROR: can't listen on port {args.port}: {e.strerror}.")
        if e.errno == errno.EADDRINUSE:
            print(f"       Starfire (or something else) is already running there: open {url},")
            print("       stop it first, or use --port to pick another.")
        return 1
    print(f"Serving GUI at {url}")
    print("Press Ctrl+C to stop\n")
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
