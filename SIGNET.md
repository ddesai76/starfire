# Signet

**Signet 26** (26.b.0.1.7.5) is a lightweight coordination language for building systems of programs written in any language. The programs, called ***modules***, pass results to one another as ***Signals***. Signet is the wiring between them, a typed directed multigraph where modules are nodes and Signals are arrows.

Signet was initially developed for [Starfire](https://github.com/ddesai76/starfire), a test design suite where each test point can run a module. It also runs on its own, as wiring code in any folder of modules.

For further details, see [*The Signet Programming Language*](https://a.co/d/0hsBrw7T) (Daniel Desai, 2026). 

## Contents

| File | What it is |
|---|---|
| `signet.py` | The reference implementation: the evaluator, CLI and embedded module helpers |
| `build.sh` | Builds the standalone `signet` binary from `signet.py` on Linux (`--onefile` for a single file) |
| `build.ps1` | The same on Windows: `signet.exe` (`-OneFile` for a single file) |
| `pyproject.toml` | The PyPI package, `signet-lang` |
| `.github/workflows/regress.yml` | Runs `signet-tests/regress.py` on Linux and Windows, from source, the pipx install and both binaries |
| `.github/workflows/release.yml` | Publishes a tagged release to PyPI once the regression passes |
| `visualizer.py` | Draws a system as SVG, in the style of Starfire's System view |
| `signet-tests/` | The regression suite: every example in the book, and every case added since |

## Install

From PyPI, as `signet-lang` (the command is `signet`). 

```sh
pipx install signet-lang
signet hello.sg
pipx upgrade signet-lang       # a newer release
```

On Linux, `#!/usr/bin/env signet` as a program's first line lets `chmod +x hello.sg && ./hello.sg` run it.

`signet.py` needs only Python 3.8 or later and its standard library, so it also runs without installing: put `signet.py`, or the standalone `signet` binary, in the working folder. To build the binary, run `./build.sh` on Linux, or `powershell -ExecutionPolicy Bypass -File build.ps1` on Windows (needs `python -m pip install nuitka`).

Windows support is experimental: `signet.py` runs, but parts of the regression suite still fail there. Fixes are planned for 26.b.0.1.8.

## Hello, Signal

`hello.sg`:

```
echo('Hello, world!')
```

```
$ signet hello.sg
→ line 1: 'Hello, world!' = "Hello, world!"
```

Without installing, run `python3 signet.py hello.sg` from source, or `./signet hello.sg` with the standalone binary. The rest of this README uses `signet`.

That's just saying hello, but to say it with a Signal, edit `hello.sg` so it's a module with an output port, write a second module `world.cpp` to receive the message, and wire the two together with `messenger.sg`:

`hello.sg`
```
# signet: out text::message
let hello = 'Hello, world!'
output(text::message, hello)
```

`world.cpp`
```cpp
// signet: in text::input
#include <signet.hpp>
#include <iostream>
int main() {
    auto m = signet::in<std::string>("input");
    std::cout << m << '\n';
    return 0;
}
```

`messenger.sg`
```
run(hello)
connect(hello.message!, world.input)
run(world)
```

```
$ signet messenger.sg
→ Hello, world!
```

`connect` transmits a Signal from one module's output port to another module's input port. Within a single pass the graph is acyclic, because a module can only read a Signal that already exists. Feedback loops are written as passes, each reading what the one before it wrote.

## Signals

A Signal is a `.sig` file: one named result of one module run, written as JSON. In code, `!` is shorthand for `.sig`, so `hello.message!` is the file `hello.message.sig`; `!` is the preferred form. After `messenger.sg` runs in a folder called `messenger`, `hello.message!` holds:

```json
{"value": "Hello, world!", "type": "text", "origin": "messenger:hello.message",
 "generated": "2026-11-19T17:08:22Z", "signet": "26.b.0.1.7.5", "inputs": {},
 "module": {"file": "hello.sg", "sha256": "1f3412ff…"}, "hash": "a7dd3f81…"}
```

`origin` is the folder, module and port; `signet` is the version that stamped it; `inputs` holds each input's hash and source (`hello.sg` has none); `module` is the module file and its SHA-256; `hash` is the value's.

When a module's run ends cleanly, Signet ***stamps*** each Signal it wrote. A stamp records the hash of every input value and where it came from: a Signal by its stamp, a file by its SHA-256, a parameter by its value. It also records the module file. If anyone edits the Signal afterwards, it no longer matches its stamp.

## The language at a glance

Signet borrows from Python, Julia, MATLAB, BASIC, Fortran 90, Rust, Haskell, C++ and the Unix shell. Typing is strong and dynamic, with optional `type::` annotations. Indexing is zero-based by default; `index(1)` changes that. Slices include both ends, and `last` names the last element.

Signet is deliberately not Turing-complete. Each loop is capped at 100,000 iterations, there is no unbounded `while`, and functions are non-recursive, so every script terminates. Anything that needs more complexity can be implemented in a module written in a different language.

```
# Comments start with #
let mass = $mass                      # A parameter, from --param mass=…
where gravity = 9.81                  # A constant
let weight = mass * gravity           # A variable
echo(weight)

for k = 0:2                           # Loops are bounded
  if k == 1 then
    echo(k)
  end if
next k

f(x) = x**2 + 1                       # Short form of function … end function
let v = map(f, [1, 2, 3])

connect(v, plot.y)                    # Give a module's input a value
run(plot)
lineage(plot.figure!)                 # Print the chain that made a Signal
```

**Types:** `int`, `float`, `real`, `num`, `bool`, `hex`, `bits::<n>`, `text`, `complex`, `quaternion`, `hypercomplex`, `vector::<type>`, `tuple`, `range`, `series::<domain>::<float|complex|quaternion>`, `stream::<domain>::<float|complex>`, `bus`, or a file extension such as `csv`.

**Routing:** `route(v, exit, [a, b])` fans a value out to several inputs, and `route(s, merge, [t, u])` appends Signals. Optional case markers, based on the directional cases of natural languages, specify a value as elative `ex` (`.sta`), ablative `ab` (`.lta`) or adessive `ad` (`.lle`).

**Waiting:** `when c within s by s … end when` waits for a condition, with deadlines. Streams (timestamped samples that arrive while their source is running) are read with `sample(s)`, `$sample(s)`, `@sample(s)` and `freeze(s)`.

Appendix A of the book has the full quick reference: 120 reserved words, the operators in order of precedence, and the built-in functions. Additions since publication follow.

### Quick reference: what's new in 26.b.0.1.7

| Syntax | Definition |
|---|---|
| `if c then … elseif d then … else … end if` | Further conditions, tested in turn; one `end if` closes the chain. `then` is optional. |
| `base(value, from, to)` | An integer's digits read in base `from` and written in base `to` (2 to 36): `base(255, 10, 16)` is `"FF"`, `base('FF', 16, 10)` is `255`. Base 10 gives an `int`, any other base `text`. A hex or bits value gives its own digits. |
| `(w, x, y, z)` | A quaternion, w + xi + yj + zk: `let quaternion::q = (1, 0, 0, 0)` |
| `quaternion` | A quaternion; also `vector::quaternion` and `series::<domain>::quaternion` |
| `hypercomplex` | A real, complex or quaternion number |
| `+ - * /` on quaternions | Hamilton product (`i * j` is `k`); a real or complex number promotes. `p / q` is `p` times the inverse of `q`. Only `==` and `/=` compare. |
| `abs(q)`, `\|q\|`, `conj(q)`, `q‾`, `arg(q)` | Norm, conjugate, and the angle from the real axis (0 to π) |
| `q ** n`, `power(q, n)`, `root(q, n)` | A real power or root (principal value) |
| `Re(q)`, `Im(q)` | The real part `w`, and the vector part `[x, y, z]` |
| `versor(q)` | `q` over its norm |
| `ang2quat(Z, Y, X)` | The unit quaternion of a rotation by Z, then Y, then X (yaw, pitch, roll), in radians |
| `quat2ang(q)` | `[Z, Y, X]` back, in radians; at a pitch of ±π/2 the roll is 0 and Z takes the rest |
| `sum`, `product`, `mean`, `norm`, `length` | Also work on a `vector::quaternion` (`product` multiplies in order) |

`num` is still a real or complex number, so existing `num` ports are unchanged. `quaternion::` doesn't turn a real into a quaternion (as `complex::` doesn't). 

Added: the built-in functions `versor`, `quat2ang`, `ang2quat` and `base`, the keyword `elseif`, and the type `hypercomplex` (a supertype of real, complex and quaternion).

## Modules

A module is any program that reads its inputs and writes its outputs as Signals. A module declares its ports in a comment, using its own language's comment marker (`#`, `//`, `--` or `!`). For example,

```python
# signet: in float::volts, in float::amps, out float::watts
from signet import signals
signals.watts = signals.volts * signals.amps   # Writes multiply.watts.sig
```

`run(multiply)` finds `multiply.<ext>` in the working folder. There can be only one module per name: if `multiply.py` and `multiply.sg` are both present, Signet reports `multiply matches multiply.py, multiply.sg`.

| Language | Extension | How it runs |
|---|---|---|
| Signet | `.sg` | Natively |
| Python | `.py` | Interpreted (`from signet import signals`) |
| Julia | `.jl` | Interpreted (`using Signet: signals`; needs JSON3) |
| ALGOL 68 | `.a68` | Interpreted |
| C, C++ | `.c`, `.cpp` | Compiled by Signet (C11, C++17) |
| Rust | `.rs` | Compiled by Signet with `rustc` (no Cargo) |
| Ada | `.adb` | Compiled by Signet with `gnatmake` |
| Fortran | `.f90` | Compiled by Signet with `gfortran` |
| Haskell | `.hs` | Compiled by Signet with `ghc` (`import Signet`) |

Signet compiles a module the first time it runs, and again only when its source or Signet changes. On Debian or Ubuntu, install the compilers with:

```sh
sudo apt install gcc g++ gnat gfortran rustc ghc
```

A Haskell module, `multiply.hs`:

```haskell
-- signet: in float::volts, in float::amps, out float::watts
import Signet

main :: IO ()
main = do
  v <- getFloat "volts"
  i <- getFloat "amps"
  put "watts" (v * i) ""
```

Quaternion ports work in every helper: `Quaternion` in Python, Quaternions.jl's `Quaternion` in Julia (loaded only when a quaternion is used, so other Julia modules still need only JSON3), `signet::Quaternion` in C++, `signet_quaternion` in C, `signet::Quaternion` in Rust, `Signet.Quaternion` in Ada, `type(signet_quaternion)` in Fortran, `SIGNETQUAT` in ALGOL 68 and `Quaternion` in Haskell. A quaternion input reads a real or complex number as a quaternion too, which is how a `hypercomplex` port is read in a compiled language.

### Helpers

Language helper files are embedded in Signet. To write them out, run:

```sh
signet --export-helpers include
```

| Language | File | Use |
|---|---|---|
| Ada | `signet.ads`, `signet.adb` | `with Signet;` |
| C | `signet.h` | `#include <signet.h>` |
| C++ | `signet.hpp` | `#include <signet.hpp>` |
| Fortran | `signet.f90` | `use signet` |
| Haskell | `Signet.hs` | `import Signet` |
| Rust | `signet.rs` | `use signet::get_f64;` |

The helpers only change when Signet does, so export them again after updating. To compile a module yourself and check it for errors before wiring it in:

```sh
gcc -O2 -std=c11 -I include module.c -o module -lm
g++ -O2 -std=c++17 -I include module.cpp -o module

# Rust: build the library once, then each module
rustc --edition 2021 -O --crate-type rlib --crate-name signet include/signet.rs -o include/libsignet.rlib
rustc --edition 2021 -O --extern signet=include/libsignet.rlib module.rs -o module

# Fortran: compile the helper once, then link each module
gfortran -O2 -c -J include include/signet.f90 -o include/signet.o
gfortran -O2 -I include module.f90 include/signet.o -o module

# Ada
mkdir -p obj && gnatmake -q -O2 -gnat2012 -Iinclude -D obj module.adb -o module

# Haskell
ghc -O1 -iinclude -outputdir obj module.hs -o module
```

Streams default to a window of 1024 samples. For example, in Python:

```python
v = signals.stream("volts", domain="time", window=1024)
v.write(t, x)
```

## Command line

```
signet [file] [--param NAME=VALUE] [--check] [--lineage SIG]
       [--export-helpers DIR] [--connections] [--graph] [--version]
```

| Flag | Does |
|---|---|
| `--param NAME=VALUE` | Sets `$NAME` (repeatable) |
| `--check` | Reads the file and reports every suspect line, without running it |
| `--lineage SIG` | Prints the chain that made a Signal in this folder (`name.sig` or `name!`) |
| `--export-helpers DIR` | Writes the module helpers into `DIR` |
| `--connections` | Prints the file's `connect()`s as JSON, without running it |
| `--graph` | Prints the system as a Graphviz graph, without running it |
| `--version` | Prints the version |

**Exit codes:** `0` means the file ran to the end, `1` means an error (the message names the line), and `2` means the code reached `break`.

Both `.sg` and the older `.signet` extension are accepted.

### Lineage

```
$ signet --lineage performance_test.report!
performance_test.report! = true  bool  made by chamber:performance_test.report at …, stamp ok
  module performance_test.rs (unchanged)
  temperature <-
    tvac.temperature! = {…}  stream::time::float  made by chamber:tvac.temperature at …, stamp ok
      module tvac.rs (unchanged)
      setpoint <- written in the code
```

Problems are reported in the chain as `edited after it was stamped`, `replaced by a later run`, `missing`, `module … (changed since)` or `file … (changed since)`.

## Visualizing a system

In the system's working folder:

```sh
signet wiring.sg --graph | dot -Tsvg -o system.svg
python3 visualizer.py wiring.sg -o diagram.svg --theme light --large --label car=10:speed
```

`--graph` prints the system as a Graphviz graph without running it, and Graphviz's `dot` draws it (`sudo apt install graphviz`). `visualizer.py` draws it in the style of Starfire's System view, using only the standard library: it uses `signet.py` from its own folder if present, or else the `signet` command. A line is solid where its source Signal exists and faint where there is no data yet. In both, stepped feedback lines are dashed.

## Starfire

In Starfire, Signet code goes in each test point's **Signals** box, and a point's Signal is written `10:module.port!`. If `signet.py` is next to `starfire.py`, Starfire imports it. See the [Starfire README](https://github.com/ddesai76/starfire).

## License

MIT. The software is provided "as is", without warranty of any kind. See `LICENSE`.
