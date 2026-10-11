# Starfire

**Starfire** is a test campaign and test card designer for aerospace, automotive, and other engineering test programs, with a **measure** button that sends SCPI lines to bench instruments and an **activate** button that runs your own scripts or programs, both built into the card. It is one local program (Linux, two small binaries) that handles the documentation side of testing (campaigns, procedures, requirements traceability, sign-off) and, optionally, talks to hardware.

It reads and writes native JSON (`.camp`, `.test`, `.vcrm`) and exports `.csv`, `.docx`, and print/PDF.

---

## Quick Start

```bash
python3 starfire.py [--port <n>] [--no-browser]   # -> http://localhost:5791
```

* `/` is the campaign designer. Each test in a campaign opens as a tab running the test card editor.
* `/test?load=<file>.test` is a single test card on its own.

### Build a standalone binary

```bash
./build.sh                              # -> dist/starfire, dist/starfire-hw, dist/jira_config.json
./build.sh --install-desktop            # also adds a menu entry (uses assets/icon.png)
```

### Dependencies
* **Runtime:** Python 3 standard library, plus `python-docx` for Word export.
* **SCPI interface:** `g++` to build `starfire-hw` from `starfire.cpp` (standard library and POSIX headers only).
* **Optional:**
  * `jira_config.json` in `dist/` next to the binary, or in the working directory, turns on Jira ticket creation (see `jira_config.example.json`).
  * `starfire-hw` (built by `build.sh`, or `g++ -O2 -std=c++17 -o starfire-hw starfire.cpp`) turns on the Measure panel's SEND button. `starfire` looks for it in the working directory, then next to itself.

---

## Features

### Campaign
* Metadata, a requirements table, a **Modules** table, and a numbered **Test Series** (plus ancillary tests). Open creates or opens the `.test` file in a tab.
* Free-form write-up sections.
* **VCRM:** cross-references campaign requirements against each card's `Requirement(s)` field and reports `Covered`, `Partial`, `Planned`, or `Not Covered`. Exports to `.vcrm`, `.docx`, and PDF.

### Test card
* Metadata, nomenclature (raw LaTeX symbols), references, notes, and warnings.
* Test points numbered 10, 20, 30... with action, expected result, **observed result**, reference, responsible party, date, time, initial, and a sign-off quality (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Locking:** save a card as Locked to freeze the baseline; sign-off fields and newly added points stay editable.
* **Flag** a point (amber), **capture** a frame from a webcam or UVC device (saved as `<timestamp>.jpg`).
* **Jira:** ticket button on `CAUTION`/`FAIL` points; task button on campaign test rows; auto-links to the campaign's epic key.

### Test point tools: Camera, Measure, Activate
Each test point has three tools in the button row. Each opens in its own column below the point, left to right: camera, measure, module.
 
| Button | Icon | What it does |
|:--|:--|:--|
| **Camera** | camera | Live preview and image capture (see above). |
| **Measure** | gauge | Opens the measure column: a **USB port** (e.g. `/dev/usbtmc0`) and a **Command** box with a **send** button next to it. A query (contains `?`, e.g. `MEAS:VOLT:DC?`) is sent and its reply is added as a timestamped row in the point's measurement table. A command (e.g. `OUTP ON`) is sent and the status line shows `Sent OUTP ON at 14:03:22`; nothing is logged. The **download** icon saves the point's readings as `<campaign>-<card>-<point>.csv` (e.g. `SC-001-TC-001-20.csv`) in the working folder. Without `starfire-hw` the panel still works for manual entry and says nothing was sent. |
| **Activate** | lightning bolt | Opens the module column. Type or pick a module name, press **run**, and watch the status: `RUNNING`, then `COMPLETE`, `FAILED` or `BROKEN` with the exit code, run time, and the module's output. **break** stops a running module (and anything it started): it is sent SIGTERM, then SIGKILL after 3 seconds. The **download** icon under the output saves it as `<campaign>-<card>-<point>.txt`. A card outside a campaign uses `XX-000` as the campaign. |
 
### Modules
A **module** is a script or program you wrote that Starfire runs for you: warm up a feed system, power a supply on, start a logger. Starfire doesn't know what it does; it starts it, waits, and reports how it ended.

1. **Put the file in `dist/`**, next to the `starfire` binary (when running `python3 starfire.py` from source, in a `dist/` folder next to `starfire.py`). How it is run depends on its name:

| File | How Starfire runs it | Needs |
|:--|:--|:--|
| `*.py` | `python3 <file>` | `python3` on `PATH`. No `chmod`, no shebang. |
| `*.sh` | `bash <file>` | `bash` on `PATH`. No `chmod`, no shebang. |
| `*.jl` | `julia <file>` | `julia` on `PATH`. No `chmod`, no shebang. |
| Anything else | `./<file>` directly | Execute permission (`chmod +x`). A compiled binary (`g++ -o dist/hello hello.cpp`), or a script of any other language with a shebang line such as `#!/usr/bin/env ruby`. |

The extension decides: a `.py` file runs under `python3` even if it has a different shebang. Source files such as `hello.cpp` aren't listed until you compile them. The same rule is what fills the Modules picker, so an unrunnable file never shows up.
2. **Register it** in the campaign's **Modules** table (below Requirements):

| Column | Meaning |
|:--|:--|
| **Module** | Picked from a list of the runnable files in `dist/`, which refreshes when you return to the page. The name's color shows its kind: **green** a binary (no extension), **amber** `.py`, **blue** `.jl`, **white** `.sh`, **red** anything else. |
| **Description** | Optional free text. |
| **Scope** | Where it may be run. Blank means any test point. `TC-001` means any point on card TC-001. `TC-001-20` means only point 20 on card TC-001. Identifiers are compared exactly, ignoring case. |

3. **Run it** from a test point's bolt button. The module name must be registered with a scope that includes that point, so a card opened on its own (outside a campaign) has nothing to run.

**What a module sees:**
* It runs with your project folder (where your `.test` and `.camp` files are) as its current directory, not `dist/`, so a relative path like `results.csv` lands next to your cards. To find a file next to the module itself, build the path from the module's own location (in Python, `os.path.dirname(__file__)`).

```
Starfire/                    <- launch ./dist/starfire from here; modules run here
├── TC-001.test
├── CAMP-01.camp
├── results.csv              <- written by example.py via a relative path
└── dist/
    ├── starfire
    ├── starfire-hw
    ├── jira_config.json
    └── example.py            <- example module 
```

* Environment: `STARFIRE_CARD` (card id), `STARFIRE_POINT` (point number), and `STARFIRE_HW` (path to `starfire-hw`, when found), so a shell script can drive an instrument with `"$STARFIRE_HW" send /dev/usbtmc0 "OUTP ON"`.
* The environment is your own, except that `LD_LIBRARY_PATH` is restored to its original value when running from the built binary, so a system `python3` or a compiled module isn't pointed at Starfire's bundled libraries.
* No standard input. Output (stdout and stderr together) is shown in the panel, last 4,000 characters.
* A module may run for up to an hour; after that it is killed along with anything it started.

**Safety.** Running a program on request from a web page needs care:
* Only a bare filename that exists in `dist/`, is runnable (executable, or `.py` / `.sh` / `.jl`), and isn't `starfire` or `starfire-hw` can run. Paths and `..` are rejected.
* `/run_module` accepts only JSON requests addressed to `localhost`/`127.0.0.1` on Starfire's own port. A form post from another website, or a DNS-rebinding trick, is refused.
* The scope rule is enforced by the page, not the server: the server knows which files are runnable, but not your campaign. Treat `dist/` as the trust boundary; anything executable you put there can be run from the UI.

---

## `starfire.cpp` (the SCPI interface)

`starfire.cpp` builds to `starfire-hw`, the only part of Starfire that touches a device node. The Python side never opens one itself. It has one mode, `send`, used by the Measure panel's SEND button and available to your modules (see `STARFIRE_HW` above).

```bash
starfire-hw send [--timeout <sec>] [--opc] <device> <scpi line>
#   "MEAS:VOLT:DC?"  query    -> written, then the reply is read
#   "OUTP ON"        command  -> written only
```

Every result is one JSON line on stdout, which `starfire.py` parses:

```json
{"t":"2026-10-11T14:03:22.418Z","mode":"send","dev":"/dev/usbtmc0","cmd":"MEAS:VOLT:DC?","kind":"query","ok":true,"reply":"+4.998E+00"}
{"t":"...","mode":"send","dev":"/dev/usbtmc0","cmd":"OUTP ON","kind":"command","ok":false,"error":"timed out after 5 s"}
```

Exit codes: `0` success, `1` runtime failure (the JSON line has `error`), `2` usage error (message on stderr).

### Behavior
* **`send`** decides query vs command from the `?` and says which in `kind`. A query returns the reply; a command is write-only.
* **`--opc`** (commands only; with a query it is a usage error): follows with `*OPC?` and waits for `1`, so a success means the instrument finished.
* **Timeouts:** a hard limit (default 5 s, max 60) covers open, write and read, so a wedged instrument can't hang a test. `starfire.py` keeps its 10 s limit as an outer ceiling.
* **Device and command checks:** the device must be a path under `/dev/` with no `..`, so the UI can't make this write a command into an ordinary file; a command must be one printable line.

---

## Export formats

| Format | Purpose |
|:--- | :--- |
| `.camp` | Native JSON campaign save file. |
| `.test` | Native JSON test card (supports locked/unlocked state). |
| `.vcrm` | VCRM snapshot JSON. |
| `.csv` | Flat export of card metadata and test points. |
| `.docx` | Word document with tables and styled callouts (campaign, VCRM, and single card). |
| **Print / PDF** | Browser-rendered campaign, VCRM, or test card. |

