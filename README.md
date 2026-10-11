# Starfire

**Starfire** is a test campaign and test card designer for aerospace test programs, with hardware **sample** and **activate** buttons built into the card. It is one local program (Linux, single binary) that handles the documentation side of testing (campaigns, procedures, requirements traceability, sign-off) and, optionally, talks to bench instruments.

It reads and writes native JSON (`.camp`, `.test`, `.vcrm`) and exports `.csv`, `.docx`, `.tex`, and print/PDF.

> **Status:** `starfire.py` (everything under *Implemented*) works today. `starfire.cpp` and the *Roadmap* items are scoped here but not built. Sample and Activate have only been exercised against a stand-in script, not real instruments.

---

## Quick Start

```bash
python3 starfire.py [--port <n>] [--no-browser]   # -> http://localhost:5791
```

* `/` is the campaign designer. Each test in a campaign opens as a tab running the test card editor.
* `/test?load=<file>.test` is a single test card on its own.

### Build a standalone binary

```bash
./build.sh                              # -> dist/starfire
./build.sh --install-desktop            # also adds a menu entry (uses assets/icon.png)
```

### Dependencies
* **Runtime:** Python 3 standard library, plus `python-docx` for Word export.
* **Tests:** `pytest`, then run `pytest` from the project root.
* **Optional, dropped next to the binary (or in your working directory):**
  * `jira_config.json` turns on Jira ticket creation (see `jira_config.example.json`).
  * `measure` (compiled from `measure.cpp`) turns on the Sample and Activate buttons.

---

## Implemented

### Campaign
* Metadata, a requirements table, and a numbered **Test Series** (plus ancillary tests). Open creates or opens the `.test` file in a tab.
* Free-form write-up sections.
* **VCRM:** cross-references campaign requirements against each card's `Requirement(s)` field and reports `Covered`, `Partial`, `Planned`, or `Not Covered`. Exports to `.vcrm`, `.docx`, and PDF.

### Test card
* Metadata, nomenclature (raw LaTeX symbols), references, notes, and warnings.
* Test points numbered 10, 20, 30... with action, expected result, **observed result**, reference, responsible party, date, time, initial, and a sign-off quality (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Locking:** save a card as Locked to freeze the baseline; sign-off fields and newly added points stay editable.
* **Flag** a point (amber), **capture** a frame from a webcam or UVC device (saved as `<timestamp>.jpg`).
* **Jira:** ticket button on `CAUTION`/`FAIL` points; task button on campaign test rows; auto-links to the campaign's epic key.

### Hardware: Sample and Activate
Each test point has a hardware panel with a **USB port** (for example `/dev/usbtmc0`) and two SCPI commands:

| Button | Icon | Command field | What it does |
|:--|:--|:--|:--|
| **Sample** | gauge | Sample Command (`MEAS:VOLT:DC?`) | Sends the query, then records the response as a timestamped row in the point's measurement table. Reads, never changes instrument state. |
| **Activate** | lightning bolt | Activate Command (`OUTP ON`) | Sends the command to **change** instrument state. Asks for confirmation first, then shows `Sent <command> at HH:MM:SS`. Nothing is added to the measurement table. |

Both call the same `measure` binary; the server only forwards the port and command. Without `measure`, both buttons still open the panel for manual entry and say that `measure` wasn't found and nothing was sent.

---

## Scope: `starfire.cpp`

Today `measure.cpp` is a one-shot tool: open the device, write one SCPI line, read one reply if the command contains `?`, exit. `starfire.cpp` is the planned replacement that combines **measuring** and **controlling** in one program, so a test card can run a sequence rather than one command at a time.

**Goal:** a small, dependency-free C++ program (POSIX only, `g++ -O2`) that Starfire drives for everything that touches hardware, so the Python side never opens a device itself.

### Operations
| Mode | Behavior |
|:--|:--|
| `starfire sample <dev> <query>` | Today's measure: write, read, print. |
| `starfire activate <dev> <command>` | Write only; optionally follow with `*OPC?` to confirm the instrument finished. |
| `starfire run <script.json>` | Execute a sequence: set, wait, sample N times at an interval, set safe state. Emits one JSON line per event with a timestamp. |
| `starfire list` | Enumerate `/dev/usbtmc*` and send `*IDN?` to each, so the UI can offer a port picker instead of a typed path. |

### Behavior to get right (not true of `measure.cpp` today)
* **Timeouts** on every read and write, with a hard exit, so a wedged instrument can't hang a test.
* **Safe state on exit:** a script declares a safe command (for example `OUTP OFF`). It runs on error, on timeout, and on SIGINT/SIGTERM.
* **Structured output:** JSON lines (`{"t": ..., "dev": ..., "cmd": ..., "reply": ..., "ok": ...}`) instead of bare text, so results drop straight into a point's measurement table and, later, an as-run record.
* **Transport-agnostic core:** USBTMC first; serial and raw TCP SCPI (port 5025) behind the same interface, since most bench supplies and scopes speak one of the three.
* **Allow-list:** the UI cannot send arbitrary text to an arbitrary node. Devices and command prefixes come from an allow-list file next to the binary.
* **Exit codes** as today: `0` success, `1` runtime failure, `2` usage error.

### How `measure` becomes `starfire`

`measure.cpp` is not extended in place. `starfire.cpp` is built beside it, takes over its job, and then `measure` is deleted. The browser UI doesn't change at any step.

**Command mapping.** The old call is the new `sample` mode with one extra argument, and Activate gets its own mode instead of reusing the same call.

| Today | After |
|:--|:--|
| `measure /dev/usbtmc0 "MEAS:VOLT:DC?"` | `starfire sample /dev/usbtmc0 "MEAS:VOLT:DC?"` |
| `measure /dev/usbtmc0 "OUTP ON"` (Activate reuses it) | `starfire activate /dev/usbtmc0 "OUTP ON"` |
| *(no equivalent)* | `starfire run seq.json`, `starfire list` |

**What changes in `starfire.py`** (everything else stays as is):

| Today | After |
|:--|:--|
| `_MEASURE_PATH` = `./measure` | `./starfire-hw` (the binary is renamed so it doesn't collide with the `starfire` Python app/binary) |
| `MEASURE_AVAILABLE` | `HARDWARE_AVAILABLE`, same check: executable present in the working directory |
| `POST /measure` and `POST /activate` both go to `_handle_hardware` | Same two routes, but `_handle_hardware` picks the `sample` or `activate` mode from the route instead of passing the raw command through. Parses the JSON-line reply and returns the same `{"success", "value"}` shape the UI already reads. |
| 10 s subprocess timeout | Kept as an outer ceiling; the binary's own timeout fires first and reports why. |
| Missing-binary behavior (panel opens, message says nothing was sent) | Unchanged. |

**Stages.** Each stage ships on its own, and the app works after every one.

1. **Drop-in.** `starfire.cpp` implements `sample` and `activate` with timeouts and JSON-line output, and `starfire.py` switches to it. No new UI.
2. **Port picker.** `list` fills a dropdown in the hardware panel, replacing the typed `/dev/usbtmc0`.
3. **Sequences.** `run` is added, with a streaming route so the browser shows steps as they execute and writes each reading to the point's measurement table.
4. **Retire `measure`.** Once stage 1 passes against a real instrument, delete `measure.cpp`, drop the old binary name from `build.sh` and the README, and keep one release where both names are accepted.

**Old files still work until stage 4.** If `./measure` is present and `./starfire-hw` isn't, `starfire.py` falls back to the old call, so nobody's bench setup breaks mid-migration.

### Verification
Unit tests against a pty-backed fake instrument first; then at least one real USBTMC device before anything is called supported.

---

## Roadmap: toward a Revel + Epsilon3-style workflow

Starfire's lane is **test documentation, traceability, and evidence**: the paper trail an acceptance test has to produce, tied to the hardware that produced the data. It is not a live control room. The two products it learns from:

* **Epsilon3** (procedures and operations): authored procedures, step-by-step execution, sign-offs, as-run records, and revision history.
* **Revel** (test and control for hardware): data acquisition, test automation, and results tied to requirements.

What Starfire already has covers the *authoring* half of Epsilon3 (cards, locking, sign-off, VCRM) and a thin slice of the *acquisition* half of Revel (Sample, Activate, timestamped readings). What is missing, in build order:

1. **Better evidence export.** PDF and `.docx` test cards that include captured images and the measurement tables.
2. **As-run record.** Executing a locked card produces an immutable run: who, when, which step, observed value, sign-off, plus an audit trail of edits after the fact.
3. **Live traceability view.** Requirement to test to run to result, updated as steps complete, with gaps highlighted.
4. **Data capture and plots.** Time series from `starfire.cpp run`, CSV import from other loggers, plots embedded in the record, limits checked against expected values.
5. **Showpiece package.** A synthetic propellant-feed-system acceptance campaign, end to end, with a recorded walkthrough.

Not planned: multi-user, hosted/cloud, Windows, closed-loop real-time control.

---

## Export formats

| Format | Purpose |
|:--- | :--- |
| `.camp` | Native JSON campaign save file. |
| `.test` | Native JSON test card (supports locked/unlocked state). |
| `.vcrm` | VCRM snapshot JSON. |
| `.csv` | Flat export of card metadata and test points. |
| `.docx` | Word document with tables and styled callouts (campaign, VCRM, and single card). |
| `.tex` | LaTeX test card. |
| **Print / PDF** | Browser-rendered campaign, VCRM, or test card. |

---

## Codebase

```
├── starfire.py              # server, data model, exports, both pages
├── jira_ticket.py
├── static/
│   └── starfire.css
├── tests/
│   ├── test_card.py
│   ├── test_campaign.py
│   ├── test_server.py       # real HTTP server + fake `measure`
│   └── test_jira_ticket.py
├── assets/icon.png, icon.svg
├── build.sh
├── pytest.ini
├── measure.cpp              # today's hardware helper; starfire.cpp will replace it
├── jira_config.example.json
└── README.md
```

`starfire.py`, `jira_ticket.py` and `static/` are everything the app needs at runtime; `build.sh` bundles them into `dist/starfire`.
