# Starfire

**Starfire** is a test campaign and test card designer for aerospace, automotive, and other engineering test programs. It runs on Linux, either as a Python 3 script (`python3 starfire.py`) or as a standalone binary, within a browser UI, and provides three complementary workflows:
* Documentation: campaigns (test plans), procedures (test cards), requirements matrices, and test reports, which can be combined into an overall campaign report.
* Hardware integration: **Activate** runs ***modules***, user-placed scripts or binaries in any language, and **Measure** shows the ***signals*** they write or read on a test point.
* Integration with the **Signet** programming language, which wires the modules on different test points together (see [Signals and Signet](#signals-and-signet)).

It reads and writes native JSON (`.camp`, `.test`, `.vcrm`) and exports `.csv`, `.docx`, and print/PDF.

---

## Quick Start

```bash
python3 starfire.py [--port <n>] [--no-browser]   # -> http://localhost:5791
```

### Build and run with `starfi.sh`

```bash
bash starfi.sh                          # run dist/starfire from dist/ (no build)
bash starfi.sh --build                  # build only -> dist/starfire, dist/jira_config.json
bash starfi.sh --all                    # build, then run
bash starfi.sh --clean --all            # delete dist/ first, then build and run
bash starfi.sh --clean                  # just delete dist/
bash starfi.sh --all --new dist2        # build and run in a new dist folder, dist2/
bash starfi.sh --build --install-desktop   # also adds a menu entry (uses static/assets/icon.png if present)
```

`--new` needs `--build` or `--all` (alone it stops with "New dist requires build"), and `--clean` has no effect with it. `--clean` also deletes any modules already in the dist folder.

### Dependencies
* **Runtime:** Python 3 standard library, plus `python-docx` for Word export.
* **Signet:** `signet.py`, next to `starfire.py`, defines Signet and the `.sig` files ([Signals and Signet](#signals-and-signet)). It is its own project, imported by `starfire.py` and built into the `starfire` binary, so a built `starfire` needs nothing else. It carries the helpers modules use for signals (Python, Julia, shell, C++, Rust, Ada, Fortran); `python3 signet.py --export-helpers DIR` writes them out. It is optional: without it, Starfire builds and runs as usual, with the Signals box, live list and System Reports turned off (`starfi.sh` says so when it builds).
* **Optional:**
  * `jira_config.json` in `dist/` next to the binary, or in the working directory, turns on Jira ticket creation (see `jira_config.example.json`).

---

## Features

### Campaign
* Metadata, a requirements table, a **Modules** table, and a numbered **Test Series** (plus ancillary tests). Open creates or opens the `.test` file in a tab.
* Free-form write-up sections.
* Every tab (campaign, test card, verification cross-reference matrix or VCRM, report) has the same layout: the STARFIRE title and that tab's import, export and generate buttons in a left sidebar, and the page on the right.
* **VCRM:** cross-references campaign requirements against each card's `Requirement(s)` field and reports `Covered`, `Partial`, `Planned`, or `Not Covered`. The VCRM tab shows the campaign header and details above the matrix and exports to `.vcrm`, `.docx`, and PDF.

### Test card
* Metadata (such as **Article / S/N**), nomenclature (raw LaTeX symbols), references, notes, and warnings.
* Test points numbered 10, 20, 30... with action, expected result, **observed result**, reference, responsible party, date, time, initial, and a sign-off quality (`RFI`, `PASS`, `CAUTION`, `FAIL`).
* **Locking:** save a card as Locked to freeze the baseline; sign-off fields and newly added points stay editable.
* **Flag** a point (amber), **capture** a frame from a webcam or UVC device (saved as `<timestamp>.jpg`).
* **Jira:** ticket button on `CAUTION`/`FAIL` points; task button on campaign test rows; auto-links to the campaign's epic key.

### Test Card and Test Report
There are two exports for a card:

* **Test Card**, from the card tab ("generate card" for print/PDF, "export .docx"): the procedure only, for reference and manual operation. Observed results, quality, sign-offs, images and the article are left blank.
* **Test Report**, from the **build test report** button in the card tab's sidebar. It opens a `TR-xxx` tab (`TC-001` gets `TR-001`) beside the campaign and card tabs, showing the card **as last saved** (save the card tab first; **refresh** re-reads it). The report always has the header (`TR-001 <test name> REV A`, then campaign, article, conductor, QA, date), the requirements covered, and each point's observed result, its Parameters on one line (`Parameters: voltage = 5, current = 1`, wrapping to 2 lines at most, then `…`), quality, captured images and sign-off. Exportable with **print / pdf** and **.docx**.

Files are associated with a test point by name: `<campaign>-<card>-<point>[-anything].<ext>`, for example `SC-001-TC-001-20.csv` written by a module, `SC-001-TC-001-20-validate.txt` from a module run, or `SC-001-TC-001-20-sweep.png` written by a module. Click a point in the report to make it active; the left panel lists that point's files, and ticking one embeds it:

| File | In the report |
|:--|:--|
| `.csv`, or a comma-separated `.txt` | Shown as raw text (capped at 4,096 lines). Press **plot** beside a ticked csv to plot it instead (the `value` column if there is one, else the last numeric column, against the first numeric column or the row number; `#` lines are ignored). The `.docx` lists the values in a table instead of a plot. |
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

### Test point tools: Camera, Measure, Activate, Calculator
Each test point has four tools in the button row. Camera shows a preview; the others open in their own column below the point: measure, module, calculator.

| Button | Icon | What it does |
|:--|:--|:--|
| **Camera** | camera | Live preview and image capture (see above). |
| **Measure** | gauge | A live view of the point's signals: its modules' outputs, then the signals its Signals box connects in (`stft.signal_in ← TC-001-10:sweep.waveform.sig`), and nothing else in the folder. A scalar shows in large type; a vector or series shows its first 5 entries and how many there are. It refreshes when a run on the point ends, or with **refresh**. View only: nothing is saved or reported. Needs Signet. |
| **Activate** | lightning bolt | Opens the module column. Enter a module name, press **run**, and watch the status: `RUNNING`, then `COMPLETE`, `FAILED` or `BROKEN` with the exit code, run time, and the module's output. **break** stops a running module (and anything it started): it is sent SIGTERM, then SIGKILL after 3 seconds. Output shows as the run goes, and is saved as it goes in `<campaign>-<card>-<point>-<module>.txt`, which is attached to the point. A run carries on if the page is reloaded or closed, and the card picks it up again when it reopens. Python modules run unbuffered; in C++, flush (`std::endl`) to show output as it is printed. If the output holds a test card, it is saved as `<campaign>-<card>-<point>.test` and attached to the point, ready for Branch or Extend. **Pinned modules** show as icons at the bottom left of the column, up to 4: each shows the first three letters of the module name (`validate.py` shows `Val`) and lights in the module's kind color when hovered or active. Clicking one runs it with the Arguments box; it won't run while the Module name box has text. **break** stops it as usual. A card outside a campaign uses `XX-000` as the campaign. |
| **Calculator** | calculator | Math and metrics on one of the point's `.csv` or `.txt` files, picked from a list. Click a column chip to insert `@column`, then type an expression such as `max @voltage` or `@voltage * @current`. Functions: `max`, `min`, `mean`, `median`, `sd`, `rms`, `range`, `abs`. A scalar shows 6 significant figures; an array shows its size and first values. **AC** clears. No limits (use `validate.py`). Needs numpy. |

### Modules
A **module** is a script or program placed in the `/dist` folder that Starfire runs. The module's own internal logic (UI, hardware interface, etc.) is transparent to Starfire.

1. **Put the file in `dist/`**, next to the `starfire` binary (when running `python3 starfire.py` from source, in a `dist/` folder next to `starfire.py`). How it is run depends on its name:

| File | How Starfire runs it | Needs |
|:--|:--|:--|
| `*.py` | `python3 <file>` | `python3` on `PATH`. No `chmod`, no shebang. |
| `*.sh` | `bash <file>` | `bash` on `PATH`. No `chmod`, no shebang. |
| `*.jl` | `julia <file>` | `julia` on `PATH`. No `chmod`, no shebang. |
| `*.signet` | Run inside Starfire | Signet only. See [SIGNET.md](SIGNET.md) and the [Signet spec](Signet_Language_Spec.pdf). |
| `*.cpp`, `*.rs`, `*.adb`, `*.f90` | Built by Signet on first run and whenever the source changes (cached), then run | `g++`, `rustc`, `gnatmake` or `gfortran` on `PATH`, and Signet. One-file Rust and Fortran. Building one yourself, and Rust with crates, are in the [Signet spec](Signet_Language_Spec.pdf). |
| Anything else | `./<file>` directly | Execute permission (`chmod +x`). A compiled binary (`g++ -o dist/test test.cpp`), or a script of any other language with a shebang line `#!/usr/bin/env <LANGUAGE>`. |

The extension decides: a `.py` file runs under `python3` even if it has a different shebang. Other source files, such as `hello.c`, aren't listed until compiled. The same rule applies to what populates the Modules picker, so an unrunnable file never shows up. 
2. **Register it** in the campaign's **Modules** table (below Requirements):

| Column | Meaning |
|:--|:--|
| **Module** | Picked from a list of the runnable files in `dist/`, which refreshes when you return to the page. The name's color shows its kind: **amber** `.py`, **blue** `.jl`, **white** `.sh`, **purple** `.signet`, **green** a binary (no extension), **magenta** a source Signet builds (`.cpp`, `.rs`, `.adb`, `.f90`), **red** anything else. |
| **Description** | Optional free text. |
| **Scope** | Where it may be run. Blank means any test point. `TC-001` means any point on card TC-001. `TC-001-20` means only point 20 on card TC-001. Ids are compared exactly (not as prefixes), ignoring case. |
| **Pin** | The pin button in the row's last column (amber when on) shows the module as an icon on the points in its scope. A point shows the first 4 pinned modules in its scope; the table warns when more than 4 are pinned. |

3. **Run it** from a test point's bolt button. The module name must be registered with a scope that includes that point, so a card opened on its own (outside a campaign) has nothing to run.

**Demo:** `sweep.jl` writes a linear chirp to its `waveform` output (`--f0`, `--f1`, `--duration`, `--rate`, `--amplitude`), and `stft.jl` reads it on `signal_in` and writes `ridge` (the dominant frequency in each window) and `spectrum` (`--window`, `--hop`). Run `sweep.jl` on one point and tick its `waveform.sig` live, put `connect(sweep.waveform.sig, stft.signal_in)` in another point's Signals box and run `stft.jl` there, then open Measure or **build system**. Both need only Julia and JSON3.

**What a module sees:**
* It runs within the project folder (where the `.test` and `.camp` files are) as its current directory, not `dist/`, so a relative path like `results.csv` lands next to the cards. To find a file next to the module itself, build the path from the module's own location (in Python, `os.path.dirname(__file__)`). For a module `test.py`:

```
Starfire/                    <- launch ./dist/starfire from here; modules run here
├── TC-001.test
├── CAMP-01.camp
├── results.csv              <- written by test.py via a relative path
└── dist/
    ├── starfire
    ├── jira_config.json
    └── test.py            <- the module (runs as: python3 test.py)
```

* Environment: `STARFIRE_CAMPAIGN` (campaign id, `XX-000` outside a campaign), `STARFIRE_CARD` (card id), `STARFIRE_POINT` (point number), `STARFIRE_PREFIX` (the point's file-name prefix, e.g. `SC-001-TC-001-20`, with unusual characters in the ids replaced by `_`), `STARFIRE_MODULE` (the module's name without its extension; both also set as `SIGNET_PREFIX` and `SIGNET_MODULE`), so a shell script can name a file for its point with `"$STARFIRE_PREFIX-plot.png"`. The `SIGNET_*` variables a module uses for its inputs and outputs are listed in the [Signet spec](Signet_Language_Spec.pdf).
* The environment is the user's own, except that `LD_LIBRARY_PATH` is restored to its original value when running from the built binary, so a system `python3` or a compiled module isn't pointed at Starfire's bundled libraries.
* Arguments: type them in the **Arguments** box below the module name, e.g. `--gui --burn 30`, on one line or several; a line starting with `#` is skipped and a `\` at the end of a line continues it. The **↓** button copies the point's Parameters in as `--name value` lines. They are split like a shell would (quotes and backslashes), and are passed as plain argv entries after the program (after `python3`/`bash`/ etc.  for scripts), so a module in any language reads them the usual way. The module name and Arguments are saved with the point in the `.test` file, and frozen when the card is Locked.
* Parameters: each point has a **Parameters** table of names and values (e.g. `voltage` = `5.0`) between the module name and the Arguments box, saved in the `.test` file and frozen when the card is Locked. Names are `[A-Za-z_][A-Za-z0-9_]*`, unique on the point ignoring case. A module gets them three ways: as arguments, only when **↓** copies them into the Arguments box as `--<name> <value>` lines (an existing `--<name>` line is replaced in place, values quoted where needed); always as `STARFIRE_PARAM_<NAME>` environment variables (`STARFIRE_PARAM_VOLTAGE=5.0`); and as `$<name>` in Signet code (see [Signals and Signet](#signals-and-signet)).
* No standard input. Output (stdout and stderr together) is shown in the panel and saved in the point's output file. It is capped at 4,096 lines; a longer run keeps its first and last 2,048 with a marker between.
* A module may run for up to an hour; after that it is killed along with anything it started.

**Safety.** Running a program on request from a web page needs care:
* Only a bare filename that exists in `dist/`, is runnable (executable, `.py` / `.sh` / `.jl` / `.signet`, or a source Signet builds), and isn't `starfire` can run. Paths and `..` are rejected.
* `/run_module` accepts only JSON requests addressed to `localhost`/`127.0.0.1` on Starfire's own port. A form post from another website, or a DNS-rebinding trick, is refused.
* A ***sequence*** never starts a module itself: `run(n)` waits for a person to press **run** on point *n*.
* The scope rule is enforced by the page, not the server: the server knows which files are runnable, but not your campaign. Treat `dist/` as the trust boundary; anything executable you put there can be run from the UI.

---

### Export formats

| Format | Purpose |
|:--- | :--- |
| `.camp` | Native JSON campaign save file. |
| `.test` | Native JSON test card (supports locked/unlocked state). |
| `.vcrm` | VCRM snapshot JSON. |
| `.csv` | Flat export of card metadata and test points. |
| `.sys` | A card's System (signal diagram) as JSON, without signal values; see [Signals and Signet](#signals-and-signet). |
| `.png` | A card's System diagram. |
| `.docx` | Word document with tables and styled callouts (campaign, VCRM, Test Card, and Test Report). |
| **Print / PDF** | Browser-rendered campaign, VCRM, Test Card, Test Report, or System. |

---

## Signals and Signet

Signet is the language Starfire uses to wire modules together: a test point's **Signals** box says which signals feed which module inputs and, in a sequence, when the next point runs. Signet also runs on its own; Starfire is built on it, as Simulink is on MATLAB or Dyad on Julia. Two references cover it:

* **[SIGNET.md](SIGNET.md)**: a short introduction, with a first system to try.
* **[The Signet Programming Language](Signet_Language_Spec.pdf)**: the full language, with a part on Signet in Starfire.

### How it works

1. A module run writes outputs, one `.sig` per output, named for the module and output (`chirp.sweep.sig`).
2. If the run ends `COMPLETE`, Starfire stamps each `.sig`, and deletes that module's older `.sig` files on the point that it didn't rewrite.
3. In the point's file list, tick the `.sig` **live**. Only live, stamped files can be connected, campaign-wide; the list is saved in the `.camp` file.
4. On another point, Signet code says which live signals feed which inputs: `connect(chirp.sweep.sig, stft.signal_in)`.
5. When you run that module, Starfire runs the Signet code, writes the inputs to `.inputs.json`, and starts the module.

```
connect(psu.volts.sig, multiply.volts)    # Signals box
connect(shunt.amps.sig, multiply.amps)
```

```python
# signet: in float::volts, in float::amps, out float::watts
from signet import signals                # multiply.py
watts = signals.volts * signals.amps      # read two inputs
signals.watts = watts                     # write one output -> ...-multiply.watts.sig
```

With a built-in helper, the same module can also be written in Julia, C++, Rust, Ada, Fortran, a shell script or Signet itself; C++, Rust, Ada and Fortran sources are built on their first run. Those are only the languages Signet has helpers for: any language that can read its inputs file and write a JSON file, MATLAB for example, works as a module too. The first line declares the module's ports, so Starfire can check the wiring before the run.

### The Signals box

The Signals box (beside Arguments) runs before every module run on the point, decides each input, and can stop the run with `break`, for interlocks. It is saved in the `.test` file and frozen when the card is Locked; a card outside a campaign has no live list, so its box is disabled. The box colours the code as you type: comments dim, commands purple, types indigo, names blue, signals magenta, files garnet, numbers and `true`/`false` amber, text green.

* **Naming the point.** A short name must match exactly one live signal in the campaign. Otherwise name the point: `10:chirp.sweep.sig` (this card) or `TC-001-10:chirp.sweep.sig`. A parameter is `$name`, or another point's: `20:$temperature`.
* **One module per point.** A point runs one module at a time; connects for other modules in its box are skipped.
* **Declared ports.** When a module declares its ports, an input it doesn't have, an output wired as an input, or a type that doesn't fit stops the run with the line number, and the run must write exactly the outputs it declares or nothing is stamped.
* **Messages** name the line and the cause: `line 4: sweep.sig matches 2 live signals: …` (name the point), `volts.sig has no valid stamp` (edited or unstamped), `this point has no parameter voltage`.

### Connecting points

A point's Signals box can feed another point's module as well as its own: `connect(10:psu.volts.sig, 20:volts)`, or `route(10:psu.volts.sig, exit, [20:volts, 30:vals])` to several points at once. When a module declares its ports, its module panel lists them under **Ports**, and ticking an output against a later point's input writes the `connect` or `route` line for you. Port colors: white unconnected, green connected, amber an output nothing takes, red a connection the declared ports don't allow.

### Systems

**build system** in the card tab's sidebar (card in a campaign) opens an `SY-xxx` tab drawing the card as it is now, edits included:

* **Boxes** are modules named in the Signet code or with a current `.sig`, labelled `<point>:<module>`, inputs left and outputs right; a source on another card is a small dim box. **Lines** are one per `connect()`; click one for the source's current value (text, or a plot for a vector). Parameter or number inputs have no line.
* **Colors:** dim blue box, module hasn't run; blue, last run `COMPLETE`; red, `FAILED` or `BROKEN`; amber line, the signal has data; dim amber, no data yet.
* **Exports:** **print / pdf** (diagram, a connection table of source, input, type and value, then a **Signals** section with each vector or series plotted), **.png**, and **.sys** (the diagram as JSON, no values). **+ system** in a Test Report, or **+ append** on the SY tab while the SR tab is open, adds it to reports (a `.docx` gets a heading and a note pointing to the PDF).
* **Import:** **import system file (.sys)** on the campaign page opens a saved `.sys` in an SY tab, redrawn as exported. It is a snapshot: no values, no refresh, and it can't be appended to a report.

The System diagram doesn't show arithmetic: name a worked-out value with `let` rather than writing an expression inside `connect()`.

### Sequences

A sequence is Signet code containing `when`, `run`, `delay`, `escape` or `finish`, in one point's Signals box. **run** on that point starts its module as usual; the sequence then carries on in the background, waiting for conditions and calling for the next points of the card in order (example: `tvac_hotfire.signet`, in point 10's box).

* **Waiting for RUN.** `run(n)` never starts a point by itself: the point shows `Waiting for RUN` until you press **run** on it.
* **Failure and break.** A run the sequence started that fails or breaks breaks the sequence. **break** on the sequence's point, `break` in the code, or closing Starfire stops it and every run it started.
* **Questions.** `input(...)` in a sequence shows its prompt and an **answer** box on the point's page.
* **As-run log.** `<campaign>-<card>-<point>-sequence.log` records the code and every event with its time, and is attached to the point when the sequence ends.
