#!/usr/bin/env python3
# validate.py:    TEST POINT VERIFICATION AND METRICS MODULE
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-18
# VERSION:        0.1.0
#
# Checks a test point's csv output against typed limits (@voltage > 1450) and computes metrics
# (max @voltage, @voltage * @current). Needs numpy and tkinter.
#
# Usage:
# 1. Run on a test point that has a csv (or csv-like .txt) attached, or pass a file: validate.py data.csv
# 2. Pick the file if the point has more than one, then tick the columns to use.
# 3. Type one expression per row (+ adds a row). A comparison is a limit (PASS/FAIL); anything else
#    is a metric (VALUE). @name is a ticked column, matched case-insensitively by its full header
#    (voltage_V) or the part before the first underscore (voltage).
#    Operators: OR, AND, NOT, < <= > >= == != (chains allowed), + - * /, parentheses.
#    Functions (prefix, with or without parentheses): max min mean median sd rms range abs any all.
#    sd is the population standard deviation. any(...) and all(...) take a comparison in parentheses.
#    A limit on an array passes only if every sample does.
# 4. DONE prints the results and writes <campaign>-<card>-<point>-validate.txt.
#    Exit code 0 = no limit failed, 1 = a limit failed or the window was closed, 2 = no usable input.


import os
import re
import sys

try:
    import numpy as np
except ImportError:
    np = None

MARKER = "# validate"
FUNCS = ("max", "min", "mean", "median", "sd", "rms", "range", "abs", "any", "all")
RELOPS = ("<", "<=", ">", ">=", "==", "!=")
BG, PANEL, FG, DIM, STROKE = "#080908", "#0c0e0c", "#b8ffd0", "#618a6b", "#243520"  # starfire.css :root
GRN, AMB, RED, BLU = "#39ff14", "#FFB300", "#ff2800", "#00A3E0"
MONO = ("DejaVu Sans Mono", 10)

_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)"
                    r"|@([A-Za-z_]\w*)|(<=|>=|==|!=|[<>()+\-*/])|([A-Za-z_]\w*))")


class LimitError(Exception):
    pass


def resolve(name, headers):
    # @name -> column header: full header first, else a unique part before the first underscore.
    n = name.lower()
    full = [h for h in headers if h.lower() == n]
    short = [h for h in headers if h.lower().split("_")[0] == n]
    hits = full or short
    if len(hits) == 1:
        return hits[0]
    raise LimitError(f"@{name} matches {', '.join(hits)}" if hits else f"no column @{name}")


def _tokens(expr):
    toks, pos = [], 0
    while expr[pos:].strip():
        m = _TOKEN.match(expr, pos)
        if not m:
            raise LimitError(f"unexpected '{expr[pos:].strip()[0]}' at {pos + 1}")
        num, ref, op, word = m.groups()
        at = m.start() + len(m.group(0)) - len(m.group(0).lstrip()) + 1
        if word and word.lower() not in FUNCS + ("and", "or", "not"):
            raise LimitError(f"unknown word '{word}' at {at}")
        toks.append(("num", float(num), at) if num else ("ref", ref, at) if ref
                    else ("op", op, at) if op else ("word", word.lower(), at))
        pos = m.end()
    return toks


class _Parser:
    # Recursive descent: or > and > not > comparison > + - > * / > unary minus, prefix function > atom.
    def __init__(self, expr, headers):
        self.toks, self.i, self.headers = _tokens(expr), 0, headers

    def peek(self, *vals):
        t = self.toks[self.i] if self.i < len(self.toks) else None
        return t if t and (not vals or t[1] in vals) and t[0] in ("op", "word") else None

    def take(self, *vals):
        t = self.peek(*vals)
        if t:
            self.i += 1
        return t

    def parse(self):
        if not self.toks:
            raise LimitError("empty")
        node = self.or_()
        if self.i < len(self.toks):
            t = self.toks[self.i]
            raise LimitError(f"unexpected '{t[1] if t[0] != 'num' else '%g' % t[1]}' at {t[2]}")
        return node

    def or_(self):
        node = self.and_()
        while self.take("or"):
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.take("and"):
            node = ("and", node, self.not_())
        return node

    def not_(self):
        return ("not", self.not_()) if self.take("not") else self.cmp()

    def cmp(self):
        terms, ops = [self.add()], []
        while (t := self.take(*RELOPS)):
            ops.append(t[1])
            terms.append(self.add())
        return ("cmp", terms, ops) if ops else terms[0]

    def add(self):
        node = self.mul()
        while (t := self.take("+", "-")):
            node = ("bin", t[1], node, self.mul())
        return node

    def mul(self):
        node = self.unary()
        while (t := self.take("*", "/")):
            node = ("bin", t[1], node, self.unary())
        return node

    def unary(self):
        if self.take("-"):
            return ("neg", self.unary())
        t = self.take(*FUNCS)
        return ("fn", t[1], self.unary()) if t else self.atom()

    def atom(self):
        if self.i >= len(self.toks):
            raise LimitError("expression ends early")
        t = self.toks[self.i]
        self.i += 1
        if t[0] == "num":
            return ("num", t[1])
        if t[0] == "ref":
            return ("ref", resolve(t[1], self.headers))
        if t[1] == "(":
            node = self.or_()
            if not self.take(")"):
                raise LimitError(f"missing ')' for '(' at {t[2]}")
            return node
        raise LimitError(f"unexpected '{t[1]}' at {t[2]}")


def _isbool(v):
    return v.dtype == bool


def _num(v, what):
    if _isbool(v):
        raise LimitError(f"{what} needs numbers, not a comparison")
    return v


def _bool(v, what):
    if not _isbool(v):
        raise LimitError(f"{what} needs comparisons, not numbers")
    return v


_STATS = {"max": np.max, "min": np.min, "mean": np.mean, "median": np.median, "sd": np.std,
          "rms": lambda x: np.sqrt(np.mean(np.square(x))), "range": np.ptp, "abs": np.abs} if np else {}


def _eval(node, arrays):
    kind = node[0]
    if kind == "num":
        return np.asarray(node[1])
    if kind == "ref":
        if node[1] not in arrays:
            raise LimitError(f"tick {node[1]}")
        return arrays[node[1]]
    if kind == "neg":
        return -_num(_eval(node[1], arrays), "minus")
    if kind == "fn":
        v = _eval(node[2], arrays)
        if node[1] in ("any", "all"):
            return np.asarray(getattr(np, node[1])(_bool(v, node[1])))
        return np.asarray(_STATS[node[1]](_num(v, node[1])))
    if kind == "bin":
        a, b = _num(_eval(node[2], arrays), node[1]), _num(_eval(node[3], arrays), node[1])
        if node[1] == "/" and np.any(b == 0):
            raise LimitError("division by zero")
        return {"+": np.add, "-": np.subtract, "*": np.multiply, "/": np.divide}[node[1]](a, b)
    if kind == "cmp":
        vals = [_num(_eval(t, arrays), "comparison") for t in node[1]]
        fns = {"<": np.less, "<=": np.less_equal, ">": np.greater, ">=": np.greater_equal,
               "==": np.equal, "!=": np.not_equal}
        out = np.asarray(True)
        for op, a, b in zip(node[2], vals, vals[1:]):
            out = np.logical_and(out, fns[op](a, b))
        return out
    if kind == "not":
        return np.logical_not(_bool(_eval(node[1], arrays), "NOT"))
    a, b = _bool(_eval(node[1], arrays), kind.upper()), _bool(_eval(node[2], arrays), kind.upper())
    return (np.logical_and if kind == "and" else np.logical_or)(a, b)


def _g(v):
    return f"{float(v):.6g}"


def evaluate(expr, arrays, headers, time=None):
    """(verdict, detail): PASS / FAIL for a comparison (an array passes only if every sample does),
    VALUE for a metric."""
    node = _Parser(expr, headers).parse()
    with np.errstate(all="ignore"):
        v = _eval(node, arrays)
    if not _isbool(v):
        if v.ndim == 0:
            return "VALUE", _g(v)
        return "VALUE", f"{v.size} samples  min {_g(v.min())}  mean {_g(v.mean())}  max {_g(v.max())}"
    if v.ndim:
        bad = np.flatnonzero(~v)
        if not bad.size:
            return "PASS", f"all {v.size} samples"
        at = f"; first at {time[0]}={_g(time[1][bad[0]])}" if time else ""
        return "FAIL", f"{bad.size} of {v.size} samples violate{at}"
    detail = ""
    if node[0] == "cmp":
        with np.errstate(all="ignore"):
            sides = [_eval(t, arrays) for t in node[1]]
        if all(s.ndim == 0 for s in sides):
            detail = " ".join([_g(sides[0])] + [f"{op} {_g(s)}" for op, s in zip(node[2], sides[1:])])
    return ("PASS" if bool(v) else "FAIL"), detail


def _isnum(s):
    try:
        return np.isfinite(float(s))
    except ValueError:
        return False


def load_table(path):
    # (headers, {header: array} of the all-numeric columns, time column header or None)
    with open(path, encoding="utf-8", errors="replace") as f:
        rows = [[c.strip() for c in ln.rstrip("\r\n").split(",")] for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")]
    if not rows:
        return [], {}, None
    head = rows[0] if not all(_isnum(c) for c in rows[0]) else None
    data = rows[1:] if head else rows
    width = max(len(r) for r in rows)
    headers = [(head[i] if head and i < len(head) and head[i] else f"col{i + 1}") for i in range(width)]
    arrays = {}
    for i, h in enumerate(headers):
        cells = [r[i] if i < len(r) else "" for r in data]
        if cells and all(_isnum(c) for c in cells):
            arrays[h] = np.array([float(c) for c in cells])
    time = next((h for h in headers if h in arrays), None)
    return headers, arrays, time


def _csv_like(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            first = next((ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")), "")
        _, arrays, time = load_table(path)
    except OSError:
        return False
    return "," in first and time is not None and arrays[time].size >= 2


def point_prefix():
    return "-".join(os.environ.get(k, d) for k, d in
                    (("STARFIRE_CAMPAIGN", "XX-000"), ("STARFIRE_CARD", "TC"), ("STARFIRE_POINT", "0")))


def candidates(prefix):
    out = []
    for n in sorted(os.listdir(".")):
        stem, ext = os.path.splitext(n)
        if (ext.lower() in (".csv", ".txt") and (stem == prefix or stem.startswith(prefix + "-"))
                and not n.endswith("-validate.txt") and os.path.isfile(n)
                and (ext.lower() == ".csv" or _csv_like(n))):
            out.append(n)
    return out


def report(file, results):
    # results: [(expr, verdict, detail)]. No commas outside "#" lines, so Starfire shows it as text.
    lines = [MARKER, f"# file: {file}"]
    lines += [f"{v}  {e.strip()}" + (f"  {d}" if d else "") for e, v, d in results]
    limits = [v for _, v, _ in results if v != "VALUE"]
    n = len(results) - len(limits)
    lines.append(f"# {limits.count('FAIL')} of {len(limits)} limits failed; {n} metric{'s' * (n != 1)}")
    return "\n".join(lines) + "\n"


def main():
    if np is None:
        print("validate: numpy is required (pip install numpy)", file=sys.stderr)
        return 2
    try:
        import tkinter as tk
    except ImportError:
        print("validate: tkinter is required (on Debian/Ubuntu: apt install python3-tk)", file=sys.stderr)
        return 2
    prefix = point_prefix()
    if len(sys.argv) > 1:
        if not os.path.isfile(sys.argv[1]):
            print(f"validate: no file {sys.argv[1]}", file=sys.stderr)
            return 2
        files = [sys.argv[1]]
    else:
        files = candidates(prefix)
    if not files:
        print(f"validate: no csv data for {prefix} in {os.getcwd()}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("VALIDATE " + prefix)
    root.configure(bg=BG, padx=12, pady=12)
    st = {"file": None, "headers": [], "arrays": {}, "time": None, "ticks": {}, "rows": [], "code": 1}

    def label(parent, text, fg=FG, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=fg, font=MONO, anchor="w", **kw)

    def button(parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd, bg=PANEL, fg=FG, activebackground=STROKE,
                         activeforeground=FG, disabledforeground=DIM, highlightbackground=STROKE,
                         relief="flat", font=MONO, padx=10)

    top = tk.Frame(root, bg=BG)
    top.pack(fill="x")
    colbox = tk.Frame(root, bg=BG)
    colbox.pack(fill="x", pady=(8, 8))
    label(root, "LIMITS AND METRICS", DIM).pack(fill="x")
    rowbox = tk.Frame(root, bg=BG)
    rowbox.pack(fill="x")
    bottom = tk.Frame(root, bg=BG)
    bottom.pack(fill="x", pady=(8, 0))

    def ticked():
        return [h for h, var in st["ticks"].items() if var.get()]

    def results():
        arrays = {h: st["arrays"][h] for h in ticked()}
        time = (st["time"], st["arrays"][st["time"]]) if st["time"] else None
        out = []
        for entry, _, _ in st["rows"]:
            expr = entry.get()
            if not expr.strip():
                out.append(None)
                continue
            try:
                out.append((expr,) + evaluate(expr, arrays, list(st["arrays"]), time))
            except LimitError as e:
                out.append((expr, "ERROR", str(e)))
        return out

    def refresh(*_):
        res = results()
        for (_, status, _), r in zip(st["rows"], res):
            if r is None:
                status.config(text="")
            else:
                color = {"PASS": GRN, "FAIL": AMB, "VALUE": BLU}.get(r[1], RED)
                status.config(text=(r[1] + "  " + r[2]) if r[1] != "ERROR" else r[2], fg=color)
        live = [r for r in res if r]
        done.config(state="normal" if live and all(r[1] != "ERROR" for r in live) and st["file"] else "disabled")

    def add_row():
        f = tk.Frame(rowbox, bg=BG)
        f.pack(fill="x", pady=2)
        text = tk.StringVar()
        entry = tk.Entry(f, textvariable=text, width=44, bg=PANEL, fg=FG, insertbackground=FG, font=MONO,
                         relief="flat", highlightthickness=1, highlightbackground=STROKE, highlightcolor=BLU)
        entry.pack(side="left")
        status = label(f, "")
        status.pack(side="left", padx=(10, 0))
        text.trace_add("write", refresh)    # typing, pasting or any other edit
        st["rows"].append((entry, status, text))    # keep text referenced, or its trace is lost
        entry.focus_set()
        refresh()

    def load(name):
        st["file"] = name
        st["headers"], st["arrays"], st["time"] = load_table(name)
        for w in colbox.winfo_children():
            w.destroy()
        st["ticks"] = {}
        for i, h in enumerate(st["headers"]):
            if h in st["arrays"]:
                var = tk.BooleanVar(value=False)
                st["ticks"][h] = var
                w = tk.Checkbutton(colbox, text=h, variable=var, command=refresh, bg=BG, fg=FG,
                                   selectcolor=PANEL, activebackground=BG, activeforeground=FG, font=MONO,
                                   highlightthickness=0)
            else:
                w = label(colbox, f"{h} (non-numeric)", DIM)
            w.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 14))
        refresh()

    def finish():
        res = [r for r in results() if r]
        if not res or any(r[1] == "ERROR" for r in res):
            return refresh()
        text = report(st["file"], res)
        print(text, end="")
        with open(prefix + "-validate.txt", "w", encoding="utf-8") as f:
            f.write(text)
        print("# saved " + prefix + "-validate.txt")
        st["code"] = 1 if any(r[1] == "FAIL" for r in res) else 0
        root.destroy()

    def cancel():
        print("# CANCELLED")
        root.destroy()

    if len(files) == 1:
        label(top, files[0]).pack(side="left")
    else:
        choice = tk.StringVar(value="SELECT FILE")
        menu = tk.OptionMenu(top, choice, *files, command=load)
        menu.config(bg=PANEL, fg=FG, activebackground=STROKE, activeforeground=FG, font=MONO,
                    highlightthickness=0, relief="flat")
        menu["menu"].config(bg=PANEL, fg=FG, font=MONO)
        menu.pack(side="left")
    button(bottom, "+", add_row).pack(side="left")
    done = button(bottom, "DONE", finish)
    done.pack(side="right")
    root.protocol("WM_DELETE_WINDOW", cancel)
    add_row()
    if len(files) == 1:
        load(files[0])
    root.mainloop()
    return st["code"]


if __name__ == "__main__":
    sys.exit(main())