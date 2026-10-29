# Signet

**Signet 26** is a lightweight programming language for building systems of signals: programs written in any language, called Modules, that pass results to one another as Signals. The user writes a few lines of Signet to wire modules together, and Signet checks it, passes inputs to each module, and exports results.

Signet grew out of Starfire, a test-campaign tool, where each test point can run a module. A vibration test produces an acceleration record; an analysis module finds its modes; a third module compares them against a requirement. Signet is the wiring between them. It also runs on its own, in any folder of modules.

**Who it is for.** Engineers who write modules in Python, Julia, C++, Rust, Ada, Fortran, shell or Signet itself (all with built-in support), or any other programming language, and want to connect them.

**Conventions.** Signet code and file names are in `code`. Output appears after a `→`. *Signal* with a capital S means a `.sig` file: one named result of one module run.

## Terminology

| Term | What it is |
|:--|:--|
| **Signal** | A `.sig` file: one named result of one module run, stamped with where, when and by which run it was made. |
| **Signet** | This language, and `signet.py`, the one file that reads it. A `.signet` file is Signet code saved on its own. |
| **Module** | Any program that reads its inputs and writes its outputs as Signals: Python, Julia, C++, Rust, Ada, Fortran, a shell script, or anything that reads and writes files. |
| **Stamp** | The record of origin and the hash that Signet adds to a Signal after a clean run. An edited Signal no longer matches its stamp. |
| **Parameter** | An external value, `$name`, given on the command line or in a test point's Parameters table. |
| **Series** | Values against an axis, such as amplitude against frequency, real or complex. It travels as one Signal. |
| **Sequence** | Signet code that runs test points itself, in order, waiting for conditions between them. |
| **Starfire** | The test-campaign tool Signet grew out of ([STARFIRE-README.md](STARFIRE-README.md)). Its test points each hold a Signals box; a Signal ticked **live** can be used by other points. |
| **System** | A set of modules joined by Signals: each module's outputs are wired to other modules' inputs, and Signet runs them and keeps the record. |

## Getting started

Signet is one file: `signet.py`, or an executable binary (see [Building a binary](#building-a-binary)). Put it in a folder with the files below and run it:

```
python3 signet.py hello.signet        # or: ./signet hello.signet
```

### Hello, Signal

Every language starts by saying hello. Signet says it with a Signal. `hello.signet` makes a value, uses `connect` to send it as a Signal to the input of a module called `messenger`, and uses `run` to start that module. The module answers back as a Signal, and `echo` shows it.

`hello.signet`:

```
let hello = 'Hello, world!'
connect(hello, messenger.input)
run(messenger)
echo(messenger.output.sig)
```

`messenger` is the module it runs. Signet looks for `messenger.py`, `messenger.jl`, `messenger.signet`, or any other module file in the folder, so write it in whichever language you like. Each version reads the input and writes it back as the output `output`. Its first line declares those ports, so Signet can check the wiring before anything runs (see [Direction safety](#direction-safety)).

`messenger.py`, in Python:

```python
# signet: in text::input, out text::output
from signet import signals

print(signals.input)
signals.output = signals.input    # written as messenger.output.sig
```

`messenger.jl`, in Julia (needs the `JSON3` package):

```julia
# signet: in text::input, out text::output
using Signet: signals

println(signals.input)
signals.output = signals.input    # written as messenger.output.sig
```

`messenger.signet`, in Signet itself. The module's inputs are variables, and `output` writes a Signal:

```
# signet: in text::input, out text::output
echo(input)
output(output, input)
```

Keep one of the three: Signet runs whichever `messenger` file is in the folder, and stops if there is more than one. Run it:

```
python3 signet.py hello.signet
```

```
→ Hello, world!
→ line 4: messenger.output.sig = "Hello, world!"
```

The first line is the module's own `print` (in the Signet version it is the module's `echo`, `line 2: input = "Hello, world!"`); the second is `hello.signet`'s `echo`, with the line it is on and what the Signal holds.

### Signet on its own

A module is only needed when something has to be worked out outside Signet. For this message nothing does, so Signet can say it by itself. `echo` prints what you wrote and what it worked out to, and no module is involved:

`hello_only.signet`:

```
let hello = 'Hello, world!'
echo(hello)
```

```
→ line 2: hello = "Hello, world!"
```

### The Signal between them

A Signal on disk is plain JSON. After the run above, `messenger.output.sig` holds:

```json
{"value": "Hello, world!", "type": "text", "origin": "hello:messenger.output", "generated": "2026-10-26T18:06:45Z", "hash": "f2bd6c21f55c525f7bf3977ab93b0dd21c284d02af060af585e068648e77e9e6"}
```

The module wrote only the `value`; Signet added the type, where it came from, when, and a hash of the rest after the module finished cleanly. If anyone edits the file, the hash stops matching and the Signal is refused. That is what makes a Signal trustworthy evidence rather than just a number in a file.

The module's input travelled the other way. `connect` wrote it to `messenger.inputs.json` (and a plain-text copy, `messenger.inputs.txt`, for languages without a JSON library), and the helper read it back as `signals.input`.

### Direction safety

Every connection runs in a single direction. Variables passed by a signal have a direction definition based upon case inflections of natural languages: ***elative*** out from a module run as a Signal (`messenger.output.sig`) or ***ablative*** off of an outside source (`$volts`, `sweep.csv`, `'Hello, world!'`), and always ***allative*** towards a module input (`messenger.input`). Signet checks the direction before anything runs, with `--check` and before every run:

```
connect(messenger.input, report.text)
```

```
→ line 1: messenger.input is an input of messenger, not a Signal: read messenger's output as messenger.<output>.sig (messenger writes output)
```

A module that declares its ports in a `signet:` comment (after `#`, `//`, `--` or `!`, so it works in every language) is also checked for port names and types, and its run is held to the declaration: it must be given every input it declares, and write exactly the outputs it declares. A module without one still runs, and is checked for direction only.

### Checking without running

`--check` reads the whole file and reports every line it can't understand, without running anything:

```
python3 signet.py hello.signet --check
```

```
→ hello.signet: ok
```

### In Starfire

The same code runs in the Signals box of a Starfire test point, where test points take the place of the folder: see [STARFIRE-README.md](STARFIRE-README.md).

Exit codes: `0` the file ran to the end, `1` an error, `2` a `break`.

## Building a binary

`build.sh` compiles `signet.py` into a Linux binary with Nuitka, so Signet runs without Python installed:

```sh
python3 -m pip install nuitka zstandard patchelf   # once; gcc is needed too
./build.sh                # one file:  dist/signet-<version>-linux-<arch>
./build.sh --standalone   # a folder:  dist/signet-<version>-linux-<arch>-standalone/signet
```

`--onefile` (the default) gives one file to copy anywhere; it unpacks itself once per version into `~/.cache/signet` and starts in about a quarter of a second. `--standalone` gives a folder that starts at once, for when Signet is called many times. `--python PY` builds with another interpreter.

The build stops if `VERSION` and `signet.py` disagree, and finishes with a smoke test: `--version`, `--check`, the hello and messenger modules, and a module written in Signet.

The binary runs on Linux with the same or a newer glibc than the machine that built it, so build on the oldest system you need to support. It contains Signet itself, but not the languages modules are written in: running a `.py` module still needs `python3` on the `PATH`, a `.jl` module `julia`, a `.sh` module `bash`, and a C++, Rust, Ada or Fortran module its compiler (`g++`, `rustc`, `gnatmake`, `gfortran`).

Starfire imports `signet.py` directly and is built on its own without recourse to this binary.
