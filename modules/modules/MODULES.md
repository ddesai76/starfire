# Starfire example modules

Starter modules: copy them into `dist/`, next to `starfire` or `starfire.py`. Every one is prefixed `sf_`, so none clashes with your own.

| Module | Does |
|---|---|
| `sf_source.sg` | Outputs a constant, `k1` = 1.234 |
| `sf_wave.py` | Outputs `wave`: one cycle of a sine, 100 samples, 0.01 s apart |
| `sf_gain_<lang>` | Outputs `s2 = k1 · s1`: series `s1` times scalar `k1`, one per language |
| `sf_terminal.cpp` | The smallest module: does nothing, exits 0 |

## Try it

On one test card, run `sf_source.sg` on point 10 and `sf_wave.py` on point 20. Then on point 30, run any gain with this in its Signals box (for example, `sf_gain_cpp`):

```
connect(10:sf_source.k1!, sf_gain_cpp.k1)
connect(20:sf_wave.wave!, sf_gain_cpp.s1)
```

Point 30 writes `…-30-sf_gain_cpp.s2.sig`: the sine scaled by 1.234. Every gain gives the same result, so you can swap languages without changing anything else.

## The gains

| File | Language | Needs on the `PATH` |
|---|---|---|
| `sf_gain_sg.sg` | Signet | nothing (runs inside Starfire) |
| `sf_gain_py.py` | Python | `python3` (Windows: `python`), with NumPy |
| `sf_gain_jl.jl` | Julia | `julia`, with the JSON3 package |
| `sf_gain_sh.sh` | shell | `bash` (Windows: Git Bash) |
| `sf_gain_a68.a68` | ALGOL 68 | `a68g` |
| `sf_gain_c.c` | C | `gcc` |
| `sf_gain_cpp.cpp` | C++ | `g++` |
| `sf_gain_rs.rs` | Rust | `rustc` |
| `sf_gain_adb.adb` | Ada | `gnatmake` |
| `sf_gain_f90.f90` | Fortran | `gfortran` |
| `sf_gain_hs.hs` | Haskell | `ghc` |

Signet builds the compiled ones on their first run and reuses the build until the source changes. Each module declares its ports in its first comment, `in float::k1, in series::time::float::s1, out series::time::float::s2`, and reads and writes them through Signet's helper for its language.
