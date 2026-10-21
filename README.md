# Starfire

**Starfire** is a test campaign and test card designer for aerospace, automotive, and other engineering test programs. It runs on Linux, either as a Python 3 script (`python3 starfire.py`) or as a standalone binary, within a browser GUI, and provides two complementary workflows:
* Documentation: campaigns (test plans), procedures (test cards), requirements matrices, and test reports, which can be combined into an overall campaign report.
* Hardware integration: Each test card has two optional hardware-interface tools: **Measure** sends SCPI commands to bench instruments through the `starfire-hw` helper binary, and **Activate** runs _modules_, user-placed scripts or binaries.

It reads and writes native JSON (`.camp`, `.test`, `.vcrm`) and exports `.csv`, `.docx`, and print/PDF.

---

## Quick Start

```bash
python3 starfire.py [--port <n>] [--no-browser]   # -> http://localhost:5791
```

From source, keep the `static/` folder next to `starfire.py`; the pages and stylesheet load from it (the built binary bundles it).

### Build and run with `starfi.sh`

```bash
bash starfi.sh                          # run dist/starfire from dist/ (no build)
bash starfi.sh --build                  # build only -> dist/starfire (+ dist/starfire-hw if g++ is present, dist/jira_config.json if you have one)
bash starfi.sh --all                    # build, then run
bash starfi.sh --clean --all            # delete dist/ first, then build and run
bash starfi.sh --clean                  # just delete dist/
bash starfi.sh --all --new dist2        # build and run in a new dist folder, dist2/
bash starfi.sh --build --install-desktop   # also adds a menu entry (uses static/assets/icon.png if present)
```

`--new` needs `--build` or `--all` (alone it stops with "New dist requires build"), and `--clean` has no effect with it. `--clean` also deletes any modules already in the dist folder.

### Dependencies
* **Runtime:** Python 3 standard library, `numpy`, and `python-docx` for Word export.
* **SCPI interface (optional):** `g++` to build `starfire-hw` from `starfire.cpp` (standard library and POSIX headers only). `starfi.sh` skips it when `g++` or `starfire.cpp` is missing; everything but Measure's SEND button still works.
* **Optional:**
  * `jira_config.json` in `dist/` next to the binary, or in the working directory, turns on Jira ticket creation (see `jira_config.example.json`).
  * `starfire-hw` (built by `starfi.sh`, or `g++ -O2 -std=c++17 -o starfire-hw starfire.cpp`) turns on the Measure panel's SEND button. `starfire` looks for it in the working directory, then next to itself.

---

## Features

### Campaign
* Metadata, a requirements table, a **Modules** table, and a numbered **Test Series** (plus ancillary tests). Open creates or opens the `.test` file in a tab.
* Free-form write-up sections.
* Every tab (campaign, test card, VCRM, report) has the same layout: the STARFIRE title and that tab's import, export and generate buttons in a left sidebar, and the page on the right.
* **VCRM:** cross-references campaign requirements against each card's `Requirement(s)` field, one row per requirement and verifying test: **Requirement**, **Source**, **Description**, **Method** (`T`, `A`, `D` or `I`), **Verification Event** (card ID and title, `NONE` if no card covers it), **Report** (the card's `TR-` number once a point has been run, else `NONE`), **Status** (`TO DO`, `IN PROGRESS` or `COMPLETE`, computed from the card's sign-offs) and **Updated**. A **Warnings** list flags uncovered requirements, cards with no requirements or unknown codes, and missing `.test` files. The VCRM tab shows the campaign header and details above the matrix and exports to `.vcrm`, `.docx`, `.csv` and PDF.
* **Create test:** a requirement no card covers has a checkbox on its row. Once any box is checked, the icon button in the column header writes one new untitled card (next free `TC-nnn`, no points) whose `Requirement(s)` lists every checked requirement, adds it to the Test Series, saves the campaign and opens the card.

### Test card
* Metadata (such as **Article / S/N**), nomenclature (raw LaTeX symbols), references, notes, and warnings.
* Test points numbered 10, 20, 30... with action, expected result, **observed result**, reference, responsible party, date, time, initial, and a sign-off quality (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Duplicate** (copy icon, beside the arrows) inserts a copy of a point directly below it, with its action, expected result, reference, responsible party and measure settings, and none of its results, sign-off, photos or files. 
* **Locking:** save a card as Locked to freeze the baseline; sign-off fields and newly added points stay editable.
* **Flag** a point (amber), **capture** a frame from a webcam or UVC device (saved as `<timestamp>.jpg`).
* **Jira:** ticket button on `CAUTION`/`FAIL` points; task button on campaign test rows; auto-links to the campaign's epic key.

### Test Card and Test Report
There are two exports for a card:

* **Test Card**, from the card tab ("generate card" for print/PDF, "export .docx"): the procedure only, for reference and manual operation. Observed results, quality, sign-offs, measurements, images and the article are left blank.
* **Test Report**, from the **build test report** button in the card tab's sidebar. It opens a `TR-xxx` tab (`TC-001` gets `TR-001`) beside the campaign and card tabs, showing the card **as last saved** (save the card tab first; **refresh** re-reads it). The report always has the header (`TR-001 <test name> REV A`, then campaign, article, conductor, QA, date), the requirements covered, and each point's observed result, quality, measurements, captured images and sign-off. Exportable with **print / pdf** and **.docx**.

A point owns the files attached to it, shown as chips under the point on the card tab (**x** detaches one, and the file stays on disk). A file is attached when added with the **+** button beside the bolt button (25 MB max; saved as `<campaign>-<card>-<point>-<its name>` in the project folder), when readings or a module's output are downloaded, or when a module run creates or changes a file named for the point: `<campaign>-<card>-<point>[-anything].<ext>`, for example `SC-001-TC-001-20.csv` from the measure download, `SC-001-TC-001-20.txt` from the module output download, or `SC-001-TC-001-20-sweep.png` written by a module. Save the card, then click a point in the report to make it active; the left panel lists that point's attached files, and ticking one embeds it.

| File | In the report |
|:--|:--|
| `.csv` or a comma-separated `.txt` | Shown as raw text (capped at 4,096 lines). Press plot beside a ticked file to plot it instead, against the first numeric column (or the row number). The trace is the value column if there is one, else the next numeric column; when there are more columns, checkboxes under plot add up to four traces, in blue, red, green and amber. # lines are ignored. A `.docx` lists values in a table instead of a plot. |
| `.png` `.jpg` `.gif` | Embedded image. |
| `.txt` | Shown as text output (capped at 4,096 lines). |
| anything else | Listed by name. |

The ticks are saved in the campaign's `.camp` file.

### Campaign Report
**build campaign report** (last button in the campaign sidebar) opens an `SR-xxx` tab (`SC-001` gets `SR-001`). It starts with only the header (`SR-001 <title> REV A`, then project, lead, QA and date) and the campaign's Sections.

* **Sections** stay live: any non-empty section on the campaign tab appears in the report, and later edits, additions and deletions follow. Remove one from the report with its x and it stays out.
* **+ Append** adds the VCRM or a Test Report. The button shows on the VCRM and Test Report tabs only while the SR tab is open, and each part can be added once.
* **Order:** the VCRM and Sections always come first, then the Test Reports. The **Contents** list in the SR sidebar moves a part up or down within its group, or removes it (x).
* A test with no saved `.test` file is skipped.
* **print / pdf** starts the VCRM and each Test Report on a new page; **.docx** adds a "Page N of N" footer.
* The Contents list is saved in the `.camp` file.

### Test point tools: Camera, Measure, Activate
Each test point has three tools in the button row. Each opens in its own column below the point, left to right: camera, measure, module.

| Button | Icon | What it does |
|:--|:--|:--|
| **Camera** | camera | Live preview and image capture (see above). |
| **Measure** | gauge | Opens the measure column: a **USB port** (e.g. `/dev/usbtmc0`) and a **Command** box with a **send** button next to it. A query (contains `?`, e.g. `MEAS:VOLT:DC?`) is sent and its reply is added as a timestamped row in the point's measurement table. A command (e.g. `OUTP ON`) is sent and the status line shows `Sent OUTP ON at 14:03:22`; nothing is logged. The **download** icon saves the point's readings as `<campaign>-<card>-<point>.csv` (e.g. `SC-001-TC-001-20.csv`) in the working folder. Without `starfire-hw` the panel still works for manual entry and says nothing was sent. |
| **Activate** | lightning | Opens the module column. Enter a module name, press **run**, and watch the status: `RUNNING`, then `COMPLETE`, `FAILED` or `BROKEN` with the exit code, run time, and the module's output. **break** stops a running module (and anything it started): it is sent SIGTERM, then SIGKILL after 3 seconds.  ||

### Test point exports: Download, Branch, Extend
The **Download** icon under the output saves it as <campaign>-<card>-<point>.txt. If the run wrote a test card (a .test file in the project folder, or output that is a card’s JSON), the same icon also saves that card as <campaign>-<card>-<point>.test and attaches it to the point. A card outside a campaign uses XX-000 as the campaign.
**Branch** and **Extend** (lower right of each point) use the last .test file attached to the point:
* **Branch** makes a _new_ card (next free TC-nnn) identical up to and including the working point: metadata, sign-offs, photos and attached files, and adds the .test file’s points, numbered from the working point + 10. The new card is added to the Test Series and opened.
*  **Extend** adds the new points to the _same_ card. It works only from the last point.

### Modules
A **module** is a script or program placed in the `/dist` folder that Starfire runs. The module's own internal logic (UI, hardware interface, etc.) is transparent to Starfire.

1. **Put the file in `dist/`**, next to the `starfire` binary (when running `python3 starfire.py` from source, in a `dist/` folder next to `starfire.py`). How it is run depends on its name:

| File | How Starfire runs it | Needs |
|:--|:--|:--|
| `*.py` | `python3 <file>` | `python3` on `PATH`. No `chmod`, no shebang. |
| `*.sh` | `bash <file>` | `bash` on `PATH`. No `chmod`, no shebang. |
| `*.jl` | `julia <file>` | `julia` on `PATH`. No `chmod`, no shebang. |
| Anything else | `./<file>` directly | Execute permission (`chmod +x`). A compiled binary (`g++ -o dist/test test.cpp`), or a script of any other language with a shebang line `#!/usr/bin/env <LANGUAGE>`. |

The extension decides: a `.py` file runs under `python3` even if it has a different shebang. Source files such as `hello.cpp` aren't listed until compiled. The same rule applies to what populates the Modules picker, so an unrunnable file never shows up. 

2. **Register it** in the campaign's **Modules** table (below Requirements):

| Column | Meaning |
|:--|:--|
| **Module** | Picked from a list of the runnable files in `dist/`, which refreshes when you return to the page. The name's color shows its kind: **green** no extension, **amber** `.py`, **blue** `.jl`, **white** `.sh`, **red** anything else. |
| **Description** | Optional free text. |
| **Scope** | Where it may be run. Blank means any test point. `TC-001` means any point on card TC-001. `TC-001-20` means only point 20 on card TC-001. Ids are compared exactly (not as prefixes), ignoring case. |

3. **Run it** from a test point's bolt button. The module name must be registered with a scope that includes that point, so a card opened on its own (outside a campaign) has nothing to run.

**Module exposure:**
* A module runs within the project folder (where the `.test` and `.camp` files are) as its current directory, not `dist/`, so a relative path like `results.csv` lands next to the cards. To find a file next to the module itself, build the path from the module's own location (in Python, `os.path.dirname(__file__)`). For a module `test.py`:

```
Starfire/                    <- launch ./dist/starfire from here; modules run here
├── TC-001.test
├── CAMP-01.camp
├── results.csv              <- written by test.py via a relative path
└── dist/
    ├── starfire
    ├── starfire-hw
    ├── jira_config.json
    └── test.py            <- the module (runs as: python3 test.py)
```

* Environment: `STARFIRE_CAMPAIGN` (campaign id, `XX-000` outside a campaign), `STARFIRE_CARD` (card id), `STARFIRE_POINT` (point number), and `STARFIRE_HW` (path to `starfire-hw`, when found), so a shell script can drive an instrument with `"$STARFIRE_HW" send /dev/usbtmc0 "OUTP ON"` and name a file for its point with `"$STARFIRE_CAMPAIGN-$STARFIRE_CARD-$STARFIRE_POINT-plot.png"`.
* The environment is the user's own, except that `LD_LIBRARY_PATH` is restored to its original value when running from the built binary, so a system `python3` or a compiled module isn't pointed at Starfire's bundled libraries.
* Arguments: type them in the **Arguments** box beside the module name, e.g. `--gui --burn 30`. They are split like a shell would (quotes and backslashes), and are passed as plain argv entries after the program (after `python3`/`bash`/ etc.  for scripts), so a module in any language reads them the usual way. Not saved between sessions.
* No standard input. Output (stdout and stderr together) is shown in the panel and saved by the download icon. It is capped at 4,096 lines; a longer run keeps its first and last 2,048 with a marker between.
* A module may run for up to an hour; after that it is killed along with anything it started.

**Safety.** Running a program on request from a web page needs care:
* Only a bare filename that exists in `dist/`, is runnable (executable, or `.py` / `.sh` / `.jl`), and isn't `starfire` or `starfire-hw` can run. Paths and `..` are rejected.
* `/run_module` accepts only JSON requests addressed to `localhost`/`127.0.0.1` on Starfire's own port. A form post from another website, or a DNS-rebinding trick, is refused.
* The scope rule is enforced by the page, not the server: the server knows which files are runnable, but not your campaign. Treat `dist/` as the trust boundary; anything executable you put there can be run from the UI.

### Example Modules
Several example modules are included here as a demonstration of the range of capabilities the module architecture offers.
Source is in `examples/modules/`. Build or copy the result into `dist/`, then register it in the campaign's **Modules** table.

| Module | Language | What it does |
|---|---|---|
| `validate.py` | Python | Test point validation and metrics module. Compares output to user-entered limits (_e.g_ max @current < 450) and provides scalar or vector metrics (_e.g._ @voltage * @current). Writes `<campaign>-<card>-<point>-VAL.txt` |
| `obd2.cpp` | C++ | Automotive On-Board Diagnostics (OBD2) scanner |
| `hotfire.cpp` | C++ | Thruster hot fire demo in C++. Energizes HV, checks fuel, fires, and prints the simulated readings as CSV. Build: `g++ -O2 -std=c++17 -o dist/hotfire hotfire.cpp`. |
| `rust/src/bin/hotfire.rs` | Rust | The hot fire with an egui GUI. User-entered the firing pattern as a hex code (`FF` fires all 8 thrusters) and  burn time. Prints simulated CSV. `--sim` runs the burn quickly |
| `rust/src/bin/tvac.rs` | Rust | Thermal vacuum profile editor and runner. User-entered time and temperature vectors by typing, interpolated to ramps and plateaus. `--sim` runs the profile in 30 s. EXPORT CSV and PNG write the profile. With `--sim`, EXPORT TEST writes a Starfire test card (`TV-001.test`) that can be added to a card with BRANCH or EXTEND. |

---

### `starfire.cpp` (the SCPI interface)

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

### Export formats

| Format | Purpose |
|:--- | :--- |
| `.camp` | Native JSON campaign save file. |
| `.test` | Native JSON test card (supports locked/unlocked state). |
| `.vcrm` | VCRM snapshot JSON. |
| `.csv` | Flat export of card metadata and test points, or the VCRM as a test matrix. |
| `.docx` | Word document with tables and styled callouts (campaign, VCRM, Test Card, and Test Report). |
| **Print / PDF** | Browser-rendered campaign, VCRM, Test Card, or Test Report. |
