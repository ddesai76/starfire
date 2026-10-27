# Starfire

**Starfire** is a test campaign and test card designer for aerospace, automotive, and other engineering test programs. It runs on Linux, either as a Python 3 script (`python3 starfire.py`) or as a standalone binary, within a browser GUI, and provides three complementary workflows:
* Documentation: campaigns (test plans), procedures (test cards), requirements matrices, and test reports, which can be combined into an overall campaign report.
* Hardware integration: **Activate** runs _modules_, user-placed scripts or binaries in any language, and **Measure** shows the signals they write or read on a test point.
* Integration with the custom **Signet** programming language (see [Signals and Signet](#signals-and-signet)).

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
* Every tab (campaign, test card, VCRM, report) has the same layout: the STARFIRE title and that tab's import, export and generate buttons in a left sidebar, and the page on the right.
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
| `*.signet` | Run inside Starfire | Signet only. See [a module written in Signet](#a-module-written-in-signet). |
| `*.cpp`, `*.rs`, `*.adb`, `*.f90` | Built by Signet on first run and whenever the source changes (cached), then run | `g++`, `rustc`, `gnatmake` or `gfortran` on `PATH`, and Signet. One-file Rust and Fortran. See [Signals and Signet](#signals-and-signet) for building one yourself, and Rust with crates. |
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

* Environment: `STARFIRE_CAMPAIGN` (campaign id, `XX-000` outside a campaign), `STARFIRE_CARD` (card id), `STARFIRE_POINT` (point number), `STARFIRE_PREFIX` (the point's file-name prefix, e.g. `SC-001-TC-001-20`, with unusual characters in the ids replaced by `_`), `STARFIRE_MODULE` (the module's name without its extension; both also set as `SIGNET_PREFIX` and `SIGNET_MODULE`), so a shell script can name a file for its point with `"$STARFIRE_PREFIX-plot.png"`.
* The environment is the user's own, except that `LD_LIBRARY_PATH` is restored to its original value when running from the built binary, so a system `python3` or a compiled module isn't pointed at Starfire's bundled libraries.
* Arguments: type them in the **Arguments** box below the module name, e.g. `--gui --burn 30`, on one line or several; a line starting with `#` is skipped and a `\` at the end of a line continues it. The **↓** button copies the point's Parameters in as `--name value` lines. They are split like a shell would (quotes and backslashes), and are passed as plain argv entries after the program (after `python3`/`bash`/ etc.  for scripts), so a module in any language reads them the usual way. The module name and Arguments are saved with the point in the `.test` file, and frozen when the card is Locked.
* Parameters: each point has a **Parameters** table of names and values (e.g. `voltage` = `5.0`) between the module name and the Arguments box, saved in the `.test` file and frozen when the card is Locked. Names are `[A-Za-z_][A-Za-z0-9_]*`, unique on the point ignoring case. A module gets them three ways: as arguments, only when **↓** copies them into the Arguments box as `--<name> <value>` lines (an existing `--<name>` line is replaced in place, values quoted where needed); always as `STARFIRE_PARAM_<NAME>` environment variables (`STARFIRE_PARAM_VOLTAGE=5.0`); and as `$<name>` in Signet code (see [Signals and Signet](#signals-and-signet)).
* No standard input. Output (stdout and stderr together) is shown in the panel and saved in the point's output file. It is capped at 4,096 lines; a longer run keeps its first and last 2,048 with a marker between.
* A module may run for up to an hour; after that it is killed along with anything it started.

**Safety.** Running a program on request from a web page needs care:
* Only a bare filename that exists in `dist/`, is runnable (executable, `.py` / `.sh` / `.jl` / `.signet`, or a source Signet builds), and isn't `starfire` can run. Paths and `..` are rejected.
* `/run_module` accepts only JSON requests addressed to `localhost`/`127.0.0.1` on Starfire's own port. A form post from another website, or a DNS-rebinding trick, is refused.
* A sequence never starts a module itself: `run(n)` waits for a person to press **run** on point *n*.
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

A **signal** is a value one module run writes to a file (`.sig`) so a later run can read it, which is how one test point's result feeds another's analysis. **Signet** is the small language in a point's **Signals** box that decides which signals go into which module inputs and, in a *sequence*, when to run the next point. It reads a signal's value when a line runs, like reading a file; nothing re-runs when a signal later changes. Modules never call each other: they read inputs and write outputs, in any language. Signals need `signet.py` next to `starfire.py`; without it Starfire works as before, minus the Signals box, live list, Systems and stamping.

| Term | File | What it is |
|:--|:--|:--|
| **Signal** | `.sig` | One named output of one module run, saved as a file and stamped with where, when and by which run it was made. |
| **Signet** | `.signet` | The language, defined in `signet.py` (its own project; `python3 signet.py runner.signet` also runs it in a plain folder). A `.signet` file is code to paste into a Signals box. |
| **Series** | | A value type: points against an axis (amplitude against frequency), real or complex. It can be written to a signal. |
| **System** | `.sys` | A card's signals drawn as a diagram. |
| **Sequence** | | Signet code that steps through other points of the card in order, waiting for conditions. |

### How it works

1. A module run writes outputs, one `.sig` per output, named for the module and output (`chirp.sweep.sig`).
2. If the run ends `COMPLETE`, Starfire stamps each `.sig`.
3. In the point's file list, tick the `.sig` **live**. Only live files can be connected, campaign-wide.
4. On another point, Signet code says which live signals feed which inputs: `connect(chirp.sweep.sig, stft.signal_in)`.
5. When you run that module, Starfire runs the Signet code, writes the inputs to `.inputs.json`, and starts the module.

```
connect(psu.volts.sig, multiply.volts)    # Signals box
connect(shunt.amps.sig, multiply.amps)
```

```python
from signet import signals                # multiply.py
watts = signals.volts * signals.amps      # read two inputs
signals.watts = watts                     # write one output -> ...-multiply.watts.sig
```

```julia
using Signet: signals                     # multiply.jl
watts = signals.volts * signals.amps      # read two inputs
signals.watts = watts                     # write one output -> ...-multiply.watts.sig
```

C++, Rust, Ada and Fortran have built-in helpers too, so there is nothing to copy: put the source in `dist/`, and Signet builds it the first time it runs and again whenever it changes, keeping the program in a cache. A build error is the run's error.

```cpp
#include <signet.hpp>                     // multiply.cpp

int main() {
    double volts = signet::in<double>("volts");   // read two inputs
    double amps = signet::in<double>("amps");
    signet::out("watts", volts * amps);           // write one output -> ...-multiply.watts.sig
}
```

```rust
use signet::{get_f64, put};               // multiply.rs

fn main() {
    let watts = get_f64("volts") * get_f64("amps");   // read two inputs
    put("watts", &watts, "");                          // write one output -> ...-multiply.watts.sig
}
```

```ada
with Signet;                              --  multiply.adb

procedure Multiply is
   Watts : constant Long_Float := Signet.Get_Float ("volts") * Signet.Get_Float ("amps");   --  read two inputs
begin
   Signet.Put ("watts", Watts);           --  write one output -> ...-multiply.watts.sig
end Multiply;
```

```fortran
program multiply                          ! multiply.f90
  use signet
  implicit none
  real(8) :: volts, amps
  call signet_get("volts", volts)         ! read two inputs
  call signet_get("amps", amps)
  call signet_put("watts", volts * amps)  ! write one output -> ...-multiply.watts.sig
end program multiply
```

**Example, step by step: `hello.cpp`.** The source goes in `dist/`, next to the `starfire` binary (or beside `starfire.py` when running from source), like any other module.

1. Save this as `dist/hello.cpp` (it needs `g++` installed):

   ```cpp
   #include <signet.hpp>                     // hello.cpp

   int main() {
       signet::out("greeting", std::string("Hello, world!"));   // -> ...-hello.greeting.sig
   }
   ```

2. Open a test point and pick `hello.cpp` in the **Module** list (reload the page if it isn't there yet).
3. Press **run**. The first run builds it; later runs reuse the build until the source changes.
4. `hello.greeting.sig` appears in the point's Signals; tick it **live** to use it in other points.

A build error, or a missing compiler, is shown as the run's error.

To build one yourself, write the helper sources out with `python3 signet.py --export-helpers signet_helpers`, build from outside `dist/` (or the source shows up as a module too), and put the program in `dist/`:

| Language | Build |
|:--|:--|
| C++ | `g++ -O2 -std=c++17 -I signet_helpers multiply.cpp -o dist/multiply` |
| Ada | `gnatmake -O2 -gnat2012 -Isignet_helpers multiply.adb -o dist/multiply` |
| Fortran | `gfortran -O2 signet_helpers/signet.f90 multiply.f90 -o dist/multiply` |
| Rust | `cargo build --release`, then copy `target/release/multiply` to `dist/` |

**Rust is the special case.** Signet builds a single `.rs` file with `rustc` and no crates. A module that needs crates (`hotfire` and `tvac` need eframe) is a Cargo project you build yourself: the module goes in `src/bin/` with the helper `signet.rs` beside it, `Cargo.toml` lists it as a `[[bin]]` (with `autobins = false`, so `signet.rs` isn't taken for a program), and the module starts with `#[path = "signet.rs"] mod signet;`, after which `use signet::{get_f64, put};` works as above. `cargo build` won't work without both files.

```
# multiply.signet                         # Signet: an input is a variable of its name
let float::watts = volts * amps
output(float::watts, watts)               # write one output -> ...-multiply.watts.sig
```

### Signet code

The Signals box (beside Arguments) runs before every module run on the point, decides each input, and can stop the run. It is saved in the `.test` file and frozen when the card is Locked. A card outside a campaign has no live list, so its box is disabled. `#` starts a comment; keywords match in any case. The box colours the code as you type: comments dim, commands purple, types indigo, names blue, signals magenta, files garnet, numbers and `true`/`false` amber, text green, the rest plain.

| Statement | Does |
|:--|:--|
| `connect(<value>, <module>.<input>)` | Gives a module input a value (once per run). |
| `let <name> = <value>`, `<name> = <value>`, `where <name> = <value>` | Declare a variable, change it, declare a constant. Not inside `if`, `for` or `when`. |
| `if <cond> [then]` … `else` … `end if` | Conditional; the condition must be a bool. |
| `for k = <start>[:<step>]:<stop>` … `next k` | Loop, inclusive, at most 100,000 turns. |
| `route(<value>, exit, [<input>, …])` | One value to several inputs. |
| `route(<signal>, merge, [<value>, …])` | Join signals into one value (series with series, vectors, scalars of one type) for the rest of the code. |
| `index(<n>)` | First index of vectors and series from here on (default 0; `[end]` counts from the last). |
| `function <name>(<args>)` … `end function`, or `<name>(<args>) = <value>` | Define a function (see **Functions, map and warp**). |
| `subprocess <name>(<args>)` … `end subprocess` | Define steps to run by calling `<name>(<values>)`; `return <value>` gives a function's value. |
| `break` | Stop the run: it ends `BROKEN`, the module never starts, nothing is written. Use it for interlocks. |
| `echo(<value>, …)` | Prints `line N: … = value` above the module's output. |
| `input([<type>::,] [<prompt>])` | Asks for a value, read like a parameter: `let v = input(float::, "Supply voltage?")`. Only a sequence asks: its point's page shows the prompt and an **answer** box, and the answer goes in the as-run log. Anywhere else it is an error. |

| Value | Written as |
|:--|:--|
| Live signal | `[module.]port.sig`, such as `sweep.sig` or `chirp.sweep.sig` |
| Live file | its file name, passed as a path: `sweep.csv` |
| Parameter | `$name`, or another point's: `20:$temperature` (read as JSON; anything else is text) |
| Number, hex, complex, bool, text, vector | `9.81`, `hex::FF`, `1 + 2i`, `true`, `'text'`, `[10, 20, 30]` |
| Variable | its name |
| Bundle | `multiplex(a.sig, b.sig)` bundles signals into one `bus`; `demultiplex(bus.a.sig)` takes one back |

* **Naming the point.** A short name must match exactly one live signal in the campaign. Otherwise name the point: `10:chirp.sweep.sig` (this card) or `TC-001-10:chirp.sweep.sig`.
* **Inputs** are `module.port`, the module's file name without extension. One box may connect several modules; connects for other modules are skipped.
* **Operators:** `+ - * /` on numbers (two ints give an int, as in Fortran), `**` power and `\\` root (`9\\2` is `3.0`), `< <= > >= == /=`, `and or not`, parentheses. Complex numbers take the arithmetic and `==`/`/=`, not `<`. A hex with any other number counts as its plain value: `hex::FF + 1` is `256`.
* **Built-in functions:** `Re(z)`, `Im(z)`, `abs(x)` or `|x|`, `arg(z)` or `>z<`, `conj(z)` or `z‾`, `power(x, p)`, `root(x, n)`. Each takes a vector too.
* **Vectors** work element by element, and must be the same length. `+` and `-` take two vectors, `*` and `/` a vector and a number (`v * 2`). The dot forms `.+ .- .* ./` also take two vectors or a number against every value: `[1, 2, 3] .* [10, 20, 30]` is `[10, 40, 90]`, `2 .* v`, `10 .- v`. Write `2.0 * x`, not `2.* x`. Series, text and a vector `*` a vector are errors.
* **Types** go in front, joined by `::`, and a mismatch stops the run before the module starts: `connect(float::$voltage, voltmeter.v_in)`, `series::time::float::stft.signal_in`.
* **A run** reads the whole box (every unreadable line is listed, run `FAILED`), runs it top to bottom (the first error stops it, `FAILED`), then writes the inputs and starts the module. A point runs one module at a time, so write the box for one module.
* **Functions, map and warp.** A function is pure and defined once, above its use: `volume(x) = x * 0.5`, or `function volume(x)` … `return x * 0.5` … `end function`. It sees only its arguments, its own `let`s and the constants and variables declared before it (a snapshot); it can't read a signal, file or parameter (pass it in), has no effects (`connect`, `run`, `echo`, `break`), and can't call itself. Arguments can be typed: `function scale(float::k, vector::float::v)`. A **subprocess** is the same for steps, and can do anything a sequence can; define it, then call it on its own line, `fire(20, 300)`. A sequence's points are found by following calls, so the arguments must be numbers, `let`s of numbers or loop values. `map(f, v)` applies `f` to every value of a vector (`map(add, a, b)` takes pairs). `warp(fx, fy, s)` applies `fx` to every point on a series' axis and `fy` to every value; the axis must stay strictly rising or falling. `unity` leaves one alone: `warp(unity, volume, amplitudes)`. `warp` takes real series; on a vector, `warp(unity, f, v)` is `map(f, v)`. Built-in names (`map`, `warp`, `unity`, `input`, `Re`, `Im`, `abs`, `arg`, `conj`, `power`, `root`) can't be variables.
* **Style.** The System diagram doesn't show arithmetic: name a worked-out value with `let` (or vectorize with `route ... merge`) rather than writing an expression inside `connect()`.

Typical messages: `line 4: sweep.sig matches 2 live signals: …` (name the point), `nothing live matches volts.sig`, `volts.sig has no valid stamp` (edited or unstamped), `this point has no parameter voltage`, `stft.signal_in is connected twice (lines 4 and 9)`, `expected series::frequency::float, got series::time::float`.

### A module written in Signet

A `.signet` file in `dist/` is a module like any other: pick it by name, run it, tick its outputs live. Starfire runs it itself, so it needs no interpreter, and it works wherever Signet does.

* Each input is a variable of the port's name, with its type: `volts`, `amps`.
* `output([<type>::]<name>, <value>)` writes an output. Each name once per run. Nothing is written if the run stops early: an error, or `break` (the run ends `FAILED` with the reason in its output).
* The usual statements work: `let`, `if`, `for`, `echo`, operators, vectors, `index`, functions, `map` and `warp`. `connect`, `route` and the sequence statements don't (they belong in a Signals box).
* No live signals, point parameters or Arguments: everything comes in through the inputs. It isn't for hardware or long jobs; use Python, Julia or a binary for those.

```
# limit.signet: watts, held at a limit
let float::limit = 100.0
let float::watts = volts * amps
if watts > limit then
  echo(watts)
  watts = limit
end if
output(float::watts, watts)
output(bool::limited, watts == limit)
```

### Types

`int`, `float` (or `null` for NaN), `real` (an int or float), `num` (any number, complex too), `bool`, `hex` (0 to `FFFFFFFF`, stored as a whole number with type `hex`), `text`, `complex` (`{"re": a, "im": b}`), `vector::<type>` (nest for 2-D), `series::<domain>::<type>`, `bus`, and `<ext>` for a file by extension (`csv::`).

A **series** is one signal carrying an axis and values. The axis is `start` and `step` (even) or `x` (any array), with a `domain`: `time` is seconds, `frequency` hertz, any other name has no unit. A series has one array of values; write one series per channel.

```json
{"value": {"domain": "time", "start": 0.0, "step": 0.1, "values": [0.0, 0.5, 0.9]}, "type": "series::time::float"}
{"value": {"domain": "frequency", "x": [5, 10, 20, 40], "values": [0.5, 0.5, 1.0, 1.0]}, "type": "series::frequency::float"}
```

In a type, `series::frequency::float::` is a real frequency series; either part can be left out (`series::`, `series::complex::`). A complex vector given to a helper with no axis becomes a series the way MATLAB's `plot(z)` reads it (real parts the axis, imaginary parts the values).

### Writing a module

A module can use signals if its file name without extension is letters, digits, `_` and `-`, and no other registered module has the same name. The Python (`signet`) and Julia (`Signet`, needs JSON3) helpers come with Signet; nothing to copy.

| | Python | Julia |
|:--|:--|:--|
| Import | `from signet import signals` | `using Signet: signals, series` |
| Read input `volts` | `v = signals.volts` | `v = signals.volts` |
| Write output `watts` | `signals.watts = w` | `signals.watts = w` |
| Write, with a type | `signals.write("watts", w, "float")` | `write(signals, "watts", w, "float")` |
| Build a series | `signals.series(v, domain="time", start=0.0, step=0.1)` or `x=f` | `series(v; domain="time", start=0.0, step=0.1)` or `x=f` |

`signals.my_output = signals.series(amps, domain="frequency", x=freqs)` writes a series; `my_output` is any name you choose. Without a type the helper picks the narrowest that fits; with one it checks the value and stops the module on a mismatch, so no bad `.sig` is stamped. Reading gives native values: `nan` for `null`, a `list`/`Vector` for a vector, a path string for a file, a `dict` for a bus, and for a series an object with `domain`, `x`, `values`, `start` and `step` (`x` is always full; `start` and `step` are `None`/`nothing` if it was stored with `x`).

**xarray and DimensionalData.** A series maps onto an xarray `DataArray` or a DimensionalData.jl `DimArray`: `domain` is the dimension's name, `x` its coordinate (a range for `start`/`step`), `values` the data. A `.sig` keeps only domain, axis, values and type, so names and units live in your array. To write one back, pass its values and axis to `series`; in Julia use `Dim{:time}` or `Dim{:frequency}`, not `Ti` or `X`.

```python
import xarray as xr
from signet import signals

s = signals.my_input                    # my_input is the input's name
da = xr.DataArray(s.values, dims=[s.domain], coords={s.domain: s.x})
```

```julia
using DimensionalData, Signet
using Signet: signals

s = signals.my_input
x = s.start === nothing ? s.x : range(s.start; step=s.step, length=length(s.values))
da = DimArray(s.values, (Dim{Symbol(s.domain)}(x),))
```

The **C++, Rust, Ada and Fortran** helpers (examples above) read vectors, 2-D arrays, series and complex values as well as numbers, bools and text. **Shell** modules `source "$SIGNET_HELPERS/signet.sh"` and use `signet_get`, `signet_put`, `signet_put_vector` and friends.

**Other languages** read the file named by `SIGNET_SIGNALS_IN` and write each output as a `.sig`:

| Variable | Value |
|:--|:--|
| `SIGNET_PREFIX` | Start of every file name for the point: `<campaign>-<card>-<point>`, such as `SC-001-TC-001-10` |
| `SIGNET_MODULE` | The module's name without extension |
| `SIGNET_SIGNALS_IN` | Path of the run's `.inputs.json` (unset with no connections) |
| `SIGNET_SIGNALS_TXT` | The same inputs as plain text, for the shell and compiled helpers |
| `SIGNET_HELPERS` | The helpers folder (also on `CPLUS_INCLUDE_PATH`) |
| `STARFIRE_PARAM_<NAME>` | Each of the point's parameters |
| `PYTHONPATH`, `JULIA_LOAD_PATH` | Signet's helper folder, in front |

An output `watts` is written to `$SIGNET_PREFIX-$SIGNET_MODULE.watts.sig`. `.inputs.json` has one entry per input, `{"value": …, "type": …, "source": …}` (a file's value is its path, a worked-out value's source is its line). JSON has no NaN: write `null`; a bare `NaN` is replaced when stamping.

### Signal files

A `.sig` is one JSON object, at most 10 MB: `{"value": [12.1, 12.4], "type": "vector::float"}`. It, `.inputs.json` and `.inputs.txt` are point files.

* **Stamping.** A `COMPLETE` run adds `origin`, `generated` and a SHA-256 `hash` of every other key. Only Starfire computes or checks it. A `.sig` from a `FAILED` or `BROKEN` run is unstamped, and one that is unstamped or edited can't be ticked live or connected.
* **Live.** Ticking checks the stamp. Detaching a file or deleting a superseded `.sig` takes it off the list, which is saved in the `.camp` file.
* **Superseded.** A `COMPLETE` run deletes that module's older `.sig` files on the point that it didn't rewrite.
* **Bulk data** (a long record, an image) belongs in a `.csv` or other point file, connected as a file.

### Systems

**build system** in the card tab's sidebar (card in a campaign) opens an `SY-xxx` tab drawing the card as it is now, edits included:

* **Boxes** are modules named in the Signet code or with a current `.sig`, labelled `<point>:<module>`, inputs left and outputs right; a source on another card is a small dim box. **Lines** are one per `connect()`; click one for the source's current value (text, or a plot for a vector). Parameter or number inputs have no line.
* **Colors:** dim blue box, module hasn't run; blue, last run `COMPLETE`; red, `FAILED` or `BROKEN`; amber line, the signal has data; dim amber, no data yet.
* **Exports:** **print / pdf** (diagram, a connection table of source, input, type and value, then a **Signals** section with each vector or series plotted), **.png**, and **.sys** (the diagram as JSON, no values). **+ system** in a Test Report, or **+ append** on the SY tab while the SR tab is open, adds it to reports (a `.docx` gets a heading and a note pointing to the PDF).
* **Import:** **import system file (.sys)** on the campaign page opens a saved `.sys` in an SY tab, redrawn as exported. It is a snapshot: no values, no refresh, and it can't be appended to a report.

### Sequences

A **sequence** is Signet code containing `when`, `run`, `delay`, `escape` or `finish`. **run** on its point starts that point's module as usual; the sequence then carries on in the background, waiting for conditions and calling for the next points of the card in order. It goes in one point's Signals box (example: `tvac_hotfire.signet`, in point 10's):

```
where burn = int::300
      pattern = hex::FF
let plateau_1 = 20:$temperature                   # point 20's parameter
let temperature = column(10:tvac.txt, temp_C)     # tvac's output, read while it runs

when temperature[end] >= plateau_1 by 7200        # clock seconds from run on point 10
    connect(burn, 20:burn_time)
    connect(pattern, 20:pattern)
    run(20)
end when

when complete(20) within 3900                     # from reaching this when
    connect(20:hotfire.txt, 40:hotfire_in)
    run(40)
end when

escape(40, 600)                                   # finish when 40 completes, else break
```

| Word | Means |
|:--|:--|
| `when <cond> [within s] [by s]` … `end when` | Wait until the condition holds (checked every second and when a run ends), then run the block. `within` counts from reaching the `when`, `by` from the start; a missed deadline breaks the sequence. |
| `run(n)` | Hold point *n* for **run**; pressing it starts the point's module with the `connect(…, n:input)` lines held for it, merged with its own box (the same input from both is an error). Goes on once the run starts. |
| `complete(n)` `failed(n)` `broken(n)` `running(n)` | State of point *n*'s latest run since the sequence started. |
| `finish`, `escape(n[, s])`, `delay(s)` | End normally; wait for *n* to end then finish (or break if it failed or ran past `s`); wait `s` seconds. |
| `column(<file>, <name>)`, `s[end]` | A csv point file column as a series (`time_s` makes a `time` domain); a series' last value. |

* **Waiting for RUN.** `run(n)` never starts a point by itself: the point shows `Waiting for RUN` until you press **run** on it, and the sequence waits.
* **Failure and break.** A run the sequence started that fails or breaks breaks the sequence, own point included. **break** on the sequence's point, `break` in the code, or closing Starfire stops it and every run it started. **break** on a point it started breaks the sequence on its next step.
* **Points** come from numbers, `let`s of numbers and `for` loops over numbers, fixed before the sequence starts. Two sequences on a card can't use the same point, and a point a sequence uses can't be run by hand until it ends (unless waiting for RUN).
* **On the page** the sequence's point shows `line N: waiting for …`, then COMPLETE or BROKEN. A started point's output is `<prefix>-<point>-<module>.txt`, written as it runs.
* **As-run log.** `<campaign>-<card>-<point>-sequence.log`: the code as `#` lines, then one line per event with wall-clock time and seconds since **run**. It is attached to the point when the sequence ends and shown under its output while it runs.
* A sequence doesn't survive Starfire closing. In a plain folder a sequence runs one point at a time.

Not yet built: units other than seconds and hertz, several-channel series, series operators, a `for` over a vector's items, and `interchange`.
