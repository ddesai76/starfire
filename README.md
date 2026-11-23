# Starfire

**Starfire 4.6.3 (Bellatrix)** is a design suite for engineering test plans (campaigns) and procedures (cards), that runs in a browser. Test cards comprise a series of test points, and each point can run a ***module*** (a program in any language) and record its result.

Starfire is designed to work with [Signet](https://github.com/ddesai76/signet), a coordination language for wiring modules written in different languages. Signet comes built into Starfire, and also runs on its own as the `signet` command (`pipx install signet-lang`). Each point's Signals box holds the Signet that connects its module to Signals from earlier points, and a whole card can be imported from or exported as a Signet wiring script (`.sg`). A test card's modules and the Signals connecting them make up a ***System***, which Starfire draws as a block diagram.

Together, Starfire and Signet enable a test evolution to be organized, visualized, and evaluated as a ***System of Signals***.

For further detail, see *The Signet Programming Language* (Daniel Desai, 2026), Chapters 11–15.

## Contents

| File | What it is |
|---|---|
| `starfire.py` | Server and application |
| `starfi.sh` | Builds/runs the Linux binary `starfire` (PyInstaller) |
| `starfire.ps1` | Builds/runs the Windows binary `starfire.exe` (PyInstaller) |
| `static/starfire.css` | Styles |
| `static/campaign.html` | Campaign page |
| `static/test.html` | Test card page |
| `static/system.html` | System visualizer |
| `tests/regress.py` | Regression suite over the HTTP API, from source or a built binary |
| `.github/workflows/regress.yml` | Runs on Linux and Windows |

## Requirements

Starfire runs on Linux and Windows. It needs:

- Python 3 (on Windows, from python.org or winget, not the Microsoft Store stub)
- `python-docx` (`.docx` export)
- NumPy 2 or later (the test point Calculator)
- Optionally, `signet.py` from the [Signet repo](https://github.com/ddesai76/signet), placed next to `starfire.py`. Without it, Starfire builds and runs as usual, but the Signals box, live list, System diagrams and stamping are off.
- Optionally, `jira_config.json` in the working directory, to turn on Jira tickets.
- To build the binary, PyInstaller.

```sh
python3 -m pip install python-docx numpy pyinstaller      # Windows: python -m pip install ...
```

## Quick start

From source:

```sh
python3 starfire.py [--port <n>] [--no-browser]     # → http://localhost:5791
python starfire.py [--port <n>] [--no-browser]      # Windows
```

Starfire prints its working folder (where cards, campaigns and run outputs go: the folder it was started in) and its modules folder. If Starfire is already running in the same folder, a second copy opens that one instead; if it is running in another folder, the second copy starts on the next free port.

Or build and run the standalone binary with `starfi.sh`:

```sh
bash starfi.sh                     # run dist/starfire (no build)
bash starfi.sh --build             # build only
bash starfi.sh --all               # build, then run
bash starfi.sh --clean --all       # delete dist/, then build and run
bash starfi.sh --clean             # just delete dist/
bash starfi.sh --all --new dist2   # build and run in dist2/
bash starfi.sh --build --install-desktop   # also add a menu entry
```

The build puts `starfire`, and `jira_config.json` if present, into `dist/`. If `signet.py` is next to `starfi.sh`, Signet is built in, so the binary needs nothing else.

- `--new` needs `--build` or `--all`, and `--clean` has no effect with it.
- `--clean` also deletes any modules already in `dist/`.
- `--install-desktop` uses `static/assets/icon.png` for the menu entry, if present.

On Windows, `starfire.ps1` does the same in PowerShell and builds `dist\starfire.exe`:

```powershell
.\starfire.ps1                      # run dist\starfire.exe (no build)
.\starfire.ps1 -Build               # build only
.\starfire.ps1 -All                 # build, then run
.\starfire.ps1 -Clean -All          # delete dist\, then build and run
.\starfire.ps1 -Clean               # just delete dist\
.\starfire.ps1 -All -New dist2      # build and run in dist2\
```

If scripts are blocked, run it as `powershell -ExecutionPolicy Bypass -File starfire.ps1 -All`. There is no `--install-desktop`: pin `starfire.exe` to Start instead.

Folder layout before a build:

```
├── starfi.sh          (Linux)
├── starfire.ps1       (Windows)
├── starfire.py
├── signet.py          (optional)
└── static
    ├── assets/icon.png    (optional)
    ├── campaign.html
    ├── starfire.css
    ├── system.html
    └── test.html
```

Modules go in `dist/` beside `starfire.py`, or beside the `starfire` binary. For your own work, keep a project folder holding the binary and its modules; a `dist/` folder beside the binary is used instead, if there is one. A `.starfireignore` file in the modules folder lists files that aren't modules (wiring scripts, helper tools), one file-name pattern per line, such as `*.sg` or `outline.py`.

Each push runs `tests/regress.py` on Linux and Windows (GitHub Actions), from source and against the built binary, and must stay at 0 failed. It brings its own small modules, so it doesn't depend on the examples in `modules/`. To run it yourself: `python3 tests/regress.py starfire.py` or `python3 tests/regress.py dist/starfire` (add `-k gain` for just the cases named gain).

## Campaigns

A **campaign** (`SC-001`) is a test plan: what requirements are verified, by which test cards, and with which modules.

| Component | Contents |
|---|---|
| Header | ID, title, revision, author, date, project, test lead and quality assurance. Copied to its cards and reports. |
| Requirements | A code, description and source for each, and its method: T (test), A (analysis), D (demonstration) or I (inspection). |
| Modules | The programs in `dist/` its points may run, each with a scope. |
| Test Series | The test cards (`TC-001` …) that verify requirements. The open button creates or opens a card in its own tab. |
| Ancillary Tests | Cards run alongside the series. These aren't included in the VCRM. |
| Sections | Headed free text. |

A card's **status** comes from its sign-offs. A point is signed off when it has initials and a quality.

- **TO DO:** no point is signed off yet.
- **IN PROGRESS:** at least one point is signed off.
- **COMPLETE:** every point is signed off, the last one with RFI.

The worst quality is shown alongside, for example `IN PROGRESS (CAUTION)`.

**GENERATE VCRM** builds a Verification Cross-Reference Matrix. It checks each requirement against every card whose Requirement(s) field lists it, with that card's status and report.

## Test cards

A test card has fields for metadata, nomenclature, references, notes and warnings. Its points are numbered 10, 20, 30 …. Each point holds:

- action, expected result, observed result and reference;
- responsible party, date and time;
- initials and a sign-off quality (RFI, PASS, CAUTION or FAIL).

Each point has six tools:

| Tool | Does |
|---|---|
| Flag | Marks the point amber, for reference only. |
| Camera | Live preview and image capture from a webcam or connected device. |
| Measure | A live, view-only look at the point's Signals. |
| Activate | Opens the module column: a module, with parameters, arguments and Signals code. RUN starts it and BREAK stops it. Shows the status (RUNNING, COMPLETE, FAILED or BROKEN), exit code, run time and output. |
| Calculator | Works on a point's `.csv` or `.txt`, for example `max @voltage`. |
| Add | Attaches a file to the point. |

### Running a module

1. Put the module's source file or binary in `dist/`.
2. Add it to the campaign's **Modules** table, with its scope:
   - blank: any point;
   - `TC-001`: any point on card `TC-001`;
   - `TC-001-20`: only point 20 of `TC-001`.

   Pinned modules show as icons on the points in their scope. Module names are colored by type: amber `.py`, blue `.jl`, white `.sh`, indigo `.a68` (ALGOL 68, run by `a68g`), purple `.sg`, green a binary (`.exe` on Windows), and magenta a source Signet builds (`.c`, `.cpp`, `.rs`, `.adb`, `.f90`, `.hs`).
3. Draft the points on a card opened from the Test Series table.
4. Press Activate, enter the Signals code, and press RUN.

A run is stopped after an hour; set `STARFIRE_MODULE_TIMEOUT` (seconds) to change that. BREAK asks the module and its child processes to stop (SIGTERM; Ctrl+Break on Windows) and forces them after 3 s. On Windows a `.py` module runs with the Python that runs Starfire (or, for `starfire.exe`, `python` on the `PATH`), and a `.sh` module needs `bash` (Git Bash) on the `PATH`.

A module declares its ports in a comment, as in Signet:

```julia
# signet: in float::voltage_in, out float::voltage_meas
using Signet: signals
v = signals.voltage_in
write(signals, "voltage_meas", v > 0 ? 0.63 : v, "float")
```

## Signals and Systems

The **Signals box** sits below Arguments in a point's module column. Its Signet code runs before each module run on the point. It is saved in the `.test` file and frozen when the card is locked. A point runs one module at a time, so lines for other modules are skipped. The box is disabled on a card outside a campaign, because there is no live list.

```
connect($voltage_in, diode.voltage_in)      # A parameter from the point's Parameters
connect(10:source.k1!, display.input)       # A Signal from point 10 on this card
connect(TC-002-10:watts!, report.watts)     # …or from point 10 on card TC-002
```

Parameters are `$name` on the current point and `<point>:$name` on another one. A module also sees them as `STARFIRE_PARAM_<NAME>` environment variables. A run writes and stamps Signals named after the campaign, card, point, module and port, for example `SC-001-TC-001-10-source.k1.sig`.

Signals can be numbers, text, vectors, series, complex numbers and quaternions (`hypercomplex` as a declared port type takes a real, complex or quaternion number). The System view plots a vector or series, with one line for each part of a complex (re, im) or quaternion (w, x, y, z) series.

**IMPORT WIRING (.SG)** in the campaign sidebar turns a Signet wiring script (`connect()`, `run()` and comments) into a new card. Each module gets a point: modules in series are numbered 10, 20, 30, and modules that don't feed each other share a step, 10, 11, 12. Each point's Signals box holds the `connect()`s into its module, and its action is the comment above them. The campaign gets each module pinned and scoped to its point, the card as a Test Series row, and every output a later point connects ticked live. **EXPORT WIRING (.SG)** in a card's sidebar does the reverse, one `run()` per point with a module. From a terminal, `python3 starfire.py --import-sg SCRIPT.sg [--id TC-001] [--camp SC-001.camp]` writes the card (and updates the campaign) without opening the browser.

**BUILD SYSTEM** in a card's sidebar opens an `SY-xxx` tab (`TC-001` opens `SY-001`). It shows the card's modules, sources and live files, with one line per source of every `connect()`. A box is outlined by its module's last run, and a line turns amber when its source has data. Stepped feedback loops are drawn dashed.

## Reports and export

- **BUILD TEST REPORT** exports a card as last saved; REFRESH reads it again. Checked files are embedded in the report:
  - `.csv` as text, with PLOT;
  - images inline;
  - `.txt` as output, up to 4,096 lines;
  - anything else by name.
- **BUILD CAMPAIGN REPORT** opens an `SR` tab, and **+ APPEND** in a Test Report or VCRM tab adds to it.

| Format | Description |
|---|---|
| `.camp` | JSON campaign save file |
| `.test` | JSON test card (locked or unlocked) |
| `.vcrm` | JSON VCRM |
| `.csv` | Card metadata and test points, flat |
| `.sys` | A card's System, as JSON |
| `.png` | A card's System diagram |
| `.docx` | Word document |
| `.pdf` | Browser-rendered |

## Look and feel

Starfire runs in dark mode by default. Its palette follows military night-vision (NVIS) lighting, to suit darkened test areas and cockpits, and its font is Airbus B612, designed for cockpit displays. **THEME** in the campaign sidebar cycles every page through four themes, and the choice is remembered:

| Theme | Look |
|---|---|
| Dark | The default, in the colors below |
| B/W | Black on white, for screenshots and black-and-white reports |
| Retro | Amber `#FFB300` on `#0B0A0C` |
| NVIS | NVIS green `#39FF14` on `#0B0A0C` |

B/W, Retro and NVIS each draw everything in one color (their `--ink`) on one background (`--paper`), set in `static/starfire.css`. Report previews stay black on white in all three, as printed.

| Color | Hex | Used for |
|---|---|---|
| Text | `#B8FFD0` | Body text |
| Blue | `#0077C8` | Point numbers, focus, Julia modules |
| Green | `#39FF14` | Complete runs, plots, built modules |
| Amber | `#FFB300` | Flags, data, Python modules |
| Red | `#FF2800` | Failed runs and errors |
| Magenta | `#FD3DB5` | Signals in code |
| Purple | `#B388FF` | Signet modules, commands in code |
| Indigo | `#6B7BFF` | ALGOL 68 modules |

## Security

The server listens on `localhost` only. POST requests must carry a JSON content type and Starfire's own `Host` header, which blocks cross-site requests and DNS rebinding. Modules are ordinary programs run with your permissions, so only put trusted code in `dist/`.

## License

MIT. The software is provided "as is", without warranty of any kind. See `LICENSE`.
