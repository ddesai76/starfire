#!/usr/bin/env python3

# signet.py:      SIGNET, A COORDINATION LANGUAGE FOR WIRING MODULES IN ANY LANGUAGE VIA SIGNALS
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-29
# VERSION:        26.b.0.1.3
#

"""
Signet: the language of Starfire's Signals boxes, and the .sig files modules pass between them.
Standard library only. See Starfire's README.md, Signals and Signet.

    python3 signet.py runner.signet [--param NAME=VALUE ...] [--check]

Code runs against a host object (API 2): resolve, param and optionally echo; a Sequence also uses
point_param, autorun, run, state and break_runs. FolderHost is a complete example.

Module helpers for Python, Julia, shell, C++, Rust, Ada and Fortran are inside this file; a module in C++, Rust, Ada or Fortran
is built by Signet itself (build_module), and --export-helpers DIR writes the helpers out.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import cmath
import decimal
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone

VERSION = "26.b.0.1.3"
API = 2                       # the interface Starfire uses; raised when it changes

_PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


_SIG_MAX = 10_000_000         # bytes, one .sig
_STEM = re.compile(r"[A-Za-z0-9_-]+\Z")
_OUTNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_KEYWORDS = {"let", "where", "connect", "if", "then", "else", "end", "and", "or", "not",
             "break", "multiplex", "demultiplex", "true", "false", "echo", "run",
             "when", "within", "by", "finish", "delay", "escape", "complete", "failed", "broken",
             "running", "column", "autorun", "for", "next", "index", "route", "exit", "merge", "interchange", "output",
             "function", "subprocess", "return", "in", "xor", "lineage"}
_MAX_NEST = 32                  # deepest vector of vectors that can be written
_TYPE_WORDS = {"int", "float", "real", "num", "bool", "text", "complex", "vector", "series", "bus", "hex", "stream", "tuple",
               "range", "bits"}
_RESERVED_PORTS = {"write", "series", "stream"}
_SIG_TOKEN = re.compile(r"""
   (?P<ws>\s+)
 | (?P<string>'[^'\n]*'|"[^"\n]*")
 | (?P<comment>\#.*)
 | (?P<type>[A-Za-z_][A-Za-z0-9_]*::(?:[A-Za-z0-9_]+::)*)
 | (?P<pparam>[A-Za-z0-9_-]+:\$[A-Za-z_][A-Za-z0-9_]*)
 | (?P<ref>[A-Za-z0-9_-]+:[A-Za-z0-9_.-]+)
 | (?P<hexval>(?<=hex::)(?:0[xX])?[0-9A-Fa-f]+(?![A-Za-z0-9_]))
 | (?P<bitsval>0[bB][01]+(?![A-Za-z0-9_.]))
 | (?P<imag>(?:\d+(?:\.(?![*/+\-])\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:im|i)(?![A-Za-z0-9_]))
 | (?P<num>(?:\d+(?:\.(?![*/+\-])\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![A-Za-z0-9_]|\.(?![*/+\-])))
 | (?P<path>[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*)
 | (?P<param>\$[A-Za-z_][A-Za-z0-9_]*)
 | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
 | (?P<op>\.[*/+\-]|\*\*|\\\\|‾|==|/=|<=|>=|<<|>>|[-+*/()<>=,\[\]:|@{};&~])
""", re.X)


class SignalError(Exception):
    def __init__(self, line, msg):
        super().__init__(f"line {line}: {msg}")
        self.line, self.msg = line, msg


class _Pending(SignalError):
    """A value that isn't there yet: a `when` keeps waiting on it; anywhere else it is an error."""


class Break(Exception):
    def __init__(self, line):
        super().__init__(f"break at line {line} of the Signet code")
        self.line = line


class Hex(int):
    """An unsigned 32-bit whole number written in hexadecimal: an int, shown in base 16. Two hex values give a hex; with anything else it counts as the plain int."""
    __slots__ = ()


_HEX_MAX = 0xFFFFFFFF


class Bits(int):
    """A fixed-width field of bits, 1 to 64 wide, as an unsigned int with its width. Bit 0 is the lowest."""

    def __new__(cls, value, width):
        o = int.__new__(cls, value)
        o.width = width
        return o


_BITS_MAX = 64


def _bits_type(width) -> str:
    return f"bits::{width}"


def _bits_from(t: str, v):
    """A stored int as Bits when its type says bits::<n>."""
    m = re.fullmatch(r"bits::(\d+)", t)
    if m and type(v) is int and 1 <= int(m.group(1)) <= _BITS_MAX and 0 <= v < 1 << int(m.group(1)):
        return Bits(v, int(m.group(1)))
    return v


@dataclass
class _SV:
    """A value in Signet code: its data, its type string, and where it came from."""
    value: object
    type: str = ""
    source: str = ""
    file: bool = False
    path: str = ""                # the file it was read from
    stamp: str = ""               # that Signal's stamp hash
    trace: dict = None            # the Signals, files and parameters it was worked out from: {name: hash or value}


def _code_part(text: str) -> str:
    """A line without its comment; a # inside a quoted text isn't one."""
    out, i = [], 0
    while i < len(text):
        m = _SIG_TOKEN.match(text, i)
        if not m or m.lastgroup == "comment":
            break
        out.append(m.group())
        i = m.end()
    return "".join(out)


def _sig_tokens(text: str, line: int) -> list:
    out, i = [], 0
    while i < len(text):
        m = _SIG_TOKEN.match(text, i)
        if not m:
            raise SignalError(line, f"can't read this line (at {text[i:i + 12]!r})")
        i = m.end()
        if m.lastgroup not in ("ws", "comment"):
            out.append((m.lastgroup, m.group()))
    return out


class _LineParser:
    """Recursive descent over one line's tokens. Expressions are tuples:
    (num v) (bool v) (ref text) (param name) (name n) (bin op a b) (not a) (neg a)
    (typed words a) (mux [(field, a)]) (demux bus field)."""

    def __init__(self, toks, line, text=""):
        self.t, self.i, self.line, self.text = toks, 0, line, text

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def kw(self, word, k=0):
        kind, text = self.peek(k)
        return kind == "name" and text.lower() == word

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def err(self, msg="can't read this line"):
        return SignalError(self.line, msg)

    def expect_op(self, op):
        if self.peek() != ("op", op):
            raise self.err(f"expected {op}")
        self.i += 1

    def done(self):
        if self.i != len(self.t):
            raise self.err(f"unexpected {self.peek()[1]!r}")

    def ident(self, what="a name"):
        kind, text = self.take()
        if kind != "name" or text.lower() in _KEYWORDS or text in _TYPE_WORDS:
            raise self.err(f"expected {what}, got {text!r}" if text else f"expected {what}")
        return text

    def point(self):
        """A point: a number or name as written, or ("pexpr", expr) such as test_points[k]."""
        kind, text = self.peek()
        if kind is None:
            raise self.err("expected a point")
        if ((kind == "num" and text.isdigit()) or (kind == "name" and text.lower() not in _KEYWORDS and text not in _TYPE_WORDS)) \
                and self.peek(1) in (("op", ")"), ("op", ",")):
            self.take()
            return text
        return ("pexpr", self.expr())

    def params(self):
        """(type:: name, ...) of a function or subprocess: [(type words, name)]."""
        self.expect_op("(")
        out = []
        while self.peek() != ("op", ")"):
            words = self.types()
            name = self.ident("an argument name")
            if any(n == name for _, n in out):
                raise self.err(f"{name} is used twice")
            out.append((words, name))
            if self.peek() != ("op", ","):
                break
            self.take()
        self.expect_op(")")
        return out

    def args(self):
        self.expect_op("(")
        out = []
        while self.peek() != ("op", ")"):
            out.append(self.expr())
            if self.peek() != ("op", ","):
                break
            self.take()
        self.expect_op(")")
        return out

    def callable_name(self):
        kind, text = self.peek()
        return kind == "name" and text.lower() not in _KEYWORDS and text not in _TYPE_WORDS and self.peek(1) == ("op", "(")

    def target(self):
        """An input to connect or route to: (words, module, port, point)."""
        words = self.types()
        kind, text = self.peek()
        if kind == "ref" and re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z_][A-Za-z0-9_]*", text):
            self.take()
            point, port = text.split(":")
            mod = None
        elif kind == "path":
            self.take()
            point, parts = None, text.split(".")
            if len(parts) != 2 or not _STEM.match(parts[0]) or not _PARAM_NAME.match(parts[1]):
                raise self.err(f"expected an input, module.port or point:port, got {text!r}")
            mod, port = parts
        elif (kind == "name" and self.peek(1) == ("op", "[") and text.lower() not in _KEYWORDS) or (kind, text) == ("op", "("):
            a = self.atom()
            while self.peek() == ("op", "["):
                self.take()
                a = ("index", a, self.index())
                self.expect_op("]")
            self.expect_op(":")
            port = self.ident("an input name")
            point, mod = ("pexpr", a), None
        else:
            raise self.err(f"expected an input, module.port or point:port, got {text!r}")
        if port in _RESERVED_PORTS:
            raise self.err(f"{port} can't be a port name")
        return words, mod, port, point

    def is_where_item(self):
        i = 1 if self.peek()[0] == "type" else 0
        return self.peek(i)[0] == "name" and self.peek(i + 1) == ("op", "=")

    def where_item(self):
        words = self.types()
        name = self.ident()
        self.expect_op("=")
        node = ("let", self.line, words, name, self.expr(), True)
        self.done()
        return node

    def types(self):
        if self.peek()[0] == "type":
            words = [w for w in self.take()[1].split("::") if w]
            if words[-1] == "bits" and self.peek()[0] == "num" and self.peek()[1].isdigit():   # bits::8
                words.append(self.take()[1])
            return words
        return []

    def statement(self):
        if self.kw("let") or self.kw("where"):
            const = self.take()[1].lower() == "where"
            words = self.types()
            name = self.ident()
            self.expect_op("=")
            node = ("let", self.line, words, name, self.expr(), const)
        elif self.kw("connect"):
            self.take()
            self.expect_op("(")
            src = self.expr()
            self.expect_op(",")
            words, mod, port, point = self.target()
            self.expect_op(")")
            node = ("connect", self.line, src, words, mod, port, point)
        elif self.kw("if"):
            self.take()
            cond = self.expr()
            if self.kw("then"):
                self.take()
            node = ("if", self.line, cond)
        elif self.kw("when"):
            self.take()
            cond, within, by = self.expr(), None, None
            while self.kw("within") or self.kw("by"):
                word = self.take()[1].lower()
                if (within if word == "within" else by) is not None:
                    raise self.err(f"{word} given twice")
                value = self.expr()
                within, by = (value, by) if word == "within" else (within, value)
            if self.kw("then"):
                self.take()
            node = ("when", self.line, cond, within, by, _code_part(self.text).strip())
        elif self.kw("for"):
            self.take()
            name = self.ident("a loop variable")
            self.expect_op("=")
            node = ("for", self.line, name) + self.loop_range()
            self.i = len(self.t)
        elif self.kw("next"):
            self.take()
            node = ("next", self.line, self.ident("the loop variable"))
        elif self.kw("index"):
            self.take()
            self.expect_op("(")
            n = self.expr()
            self.expect_op(")")
            node = ("setindex", self.line, n)
        elif self.kw("route"):
            self.take()
            self.expect_op("(")
            src = self.expr()
            self.expect_op(",")
            kind, word = self.take()
            mode = word.lower() if kind == "name" else ""
            if mode == "interchange":
                raise self.err("interchange isn't built yet: use exit or merge")
            if mode not in ("exit", "merge"):
                raise self.err("expected exit or merge")
            self.expect_op(",")
            if self.kw("exit") or self.kw("merge"):
                raise self.err("exit and merge can't be used in the same route")
            self.expect_op("[")
            items = []
            while True:
                items.append(self.target() if mode == "exit" else self.expr())
                if self.peek() != ("op", ","):
                    break
                self.take()
            self.expect_op("]")
            self.expect_op(")")
            if mode == "merge" and not (src[0] == "ref" and src[1].endswith(".sig")):
                raise self.err("merge needs a live signal first, such as route(20:voltage.sig, merge, [30:voltage.sig])")
            node = ("route", self.line, mode, src, items)
        elif self.kw("output"):
            self.take()
            self.expect_op("(")
            words = self.types()
            if self.kw("output") and self.peek(1) == ("op", ","):
                name = self.take()[1]
            else:
                name = self.ident("an output name")
            self.expect_op(",")
            value = self.expr()
            self.expect_op(")")
            node = ("output", self.line, words, name, value)
        elif self.kw("function") or self.kw("subprocess"):
            kind = self.take()[1].lower()
            name = self.ident("a name")
            node = (kind, self.line, name, self.params())
        elif self.kw("return"):
            self.take()
            node = ("return", self.line, None if self.i >= len(self.t) else self.expr())
        elif self.kw("lineage") and self.peek(1) == ("op", "("):
            self.take()
            self.expect_op("(")
            kind, text = self.take()
            if kind not in ("ref", "path") or not text.endswith(".sig"):
                raise self.err("lineage takes a Signal, such as lineage(report.pass.sig)")
            self.expect_op(")")
            node = ("lineage", self.line, text)
        elif self.callable_name():
            name, depth, j = self.take()[1], 0, self.i
            while j < len(self.t):
                depth += (self.t[j] == ("op", "(")) - (self.t[j] == ("op", ")"))
                if depth == 0:
                    break
                j += 1
            if j + 1 < len(self.t) and self.t[j + 1] == ("op", "="):
                params = self.params()
                self.expect_op("=")
                node = ("fshort", self.line, name, params, self.expr())
            else:
                node = ("callstmt", self.line, name, self.args())
        elif self.kw("finish"):
            self.take()
            node = ("finish", self.line)
        elif self.kw("delay"):
            self.take()
            self.expect_op("(")
            seconds = self.expr()
            self.expect_op(")")
            node = ("delay", self.line, seconds)
        elif self.kw("escape"):
            self.take()
            self.expect_op("(")
            point, seconds = self.point(), None
            if self.peek() == ("op", ","):
                self.take()
                seconds = self.expr()
            self.expect_op(")")
            node = ("escape", self.line, point, seconds)
        elif self.kw("else"):
            self.take()
            node = ("else", self.line)
        elif self.kw("end"):
            self.take()
            which = self.peek()[1].lower() if self.peek()[0] == "name" else ""
            if which not in ("if", "when", "function", "subprocess", ""):
                raise self.err("expected end if, end when, end function or end subprocess")
            if which:
                self.take()
            node = ("end", self.line, which)
        elif self.kw("break"):
            self.take()
            node = ("break", self.line)
        elif self.kw("echo"):
            self.take()
            self.expect_op("(")
            args = [self.expr()]
            while self.peek() == ("op", ","):
                self.take()
                args.append(self.expr())
            self.expect_op(")")
            code = _code_part(self.text)
            node = ("echo", self.line, args, code[code.index("(") + 1:code.rindex(")")].strip())
        elif self.kw("run"):
            self.take()
            self.expect_op("(")
            target = self.point()
            self.expect_op(")")
            node = ("run", self.line, target)
        elif self.peek()[0] == "name" and self.peek(1) == ("op", "="):
            name = self.ident()
            self.take()
            node = ("set", self.line, name, self.expr())
        else:
            raise self.err("can't read this line")
        self.done()
        return node

    def loop_range(self):
        # start:stop or start:step:stop. The colons are found in the text, since 1:100 reads as one token.
        text, parts, depth, cur, quote = _code_part(self.text), [], 0, "", ""
        text = text[text.index("=") + 1:]
        for ch in text:
            if quote:
                quote = "" if ch == quote else quote
            elif ch in "'\"":
                quote = ch
            elif ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == ":" and depth == 0:
                parts.append(cur)
                cur = ""
                continue
            cur += ch
        parts.append(cur)
        if len(parts) not in (2, 3):
            raise self.err("expected for k = start:stop or start:step:stop")
        exprs = []
        for seg in parts:
            p = _LineParser(_sig_tokens(seg, self.line), self.line, seg)
            if not p.t:
                raise self.err("expected for k = start:stop or start:step:stop")
            exprs.append(p.expr())
            p.done()
        return (exprs[0], exprs[1] if len(exprs) == 3 else None, exprs[-1])

    def expr(self):
        a = self.and_expr()
        while self.kw("or"):
            self.take()
            a = ("bin", "or", a, self.and_expr())
        return a

    def and_expr(self):
        a = self.not_expr()
        while self.kw("and"):
            self.take()
            a = ("bin", "and", a, self.not_expr())
        return a

    def not_expr(self):
        if self.kw("not"):
            self.take()
            return ("not", self.not_expr())
        return self.compare()

    def compare(self):
        a = self.bit_or()
        if self.kw("in"):
            self.take()
            a = ("bin", "in", a, self.bit_or())
        elif self.peek()[0] == "op" and self.peek()[1] in ("<", "<=", ">", ">=", "==", "/="):
            a = ("bin", self.take()[1], a, self.bit_or())
        return a

    def bit_or(self):
        a = self.bit_xor()
        while self.peek() == ("op", "|"):
            self.take()
            a = ("bin", "|", a, self.bit_xor())
        return a

    def bit_xor(self):
        a = self.bit_and()
        while self.kw("xor"):
            self.take()
            a = ("bin", "xor", a, self.bit_and())
        return a

    def bit_and(self):
        a = self.shift()
        while self.peek() == ("op", "&"):
            self.take()
            a = ("bin", "&", a, self.shift())
        return a

    def shift(self):
        a = self.sum()
        while self.peek() in (("op", "<<"), ("op", ">>")):
            a = ("bin", self.take()[1], a, self.sum())
        return a

    def sum(self):
        a = self.product()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-", ".+", ".-"):
            a = ("bin", self.take()[1], a, self.product())
        return a

    def product(self):
        a = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/", ".*", "./"):
            a = ("bin", self.take()[1], a, self.unary())
        return a

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return ("neg", self.unary())
        if self.peek() == ("op", "~"):
            self.take()
            return ("bnot", self.unary())
        words = self.types()
        if words == ["hex"] and self.peek()[0] == "hexval":
            return ("hexraw", self.take()[1])
        if words and self.peek()[0] == "type":          # int::hex::FF, bits::12 hex::FF
            return ("typed", words, self.unary())
        if self.peek() == ("op", "|"):                # |z|: the absolute value
            self.take()
            inner = self.sum()
            self.expect_op("|")
            a = ("call", "abs", inner)
        elif self.peek() == ("op", ">"):              # >z<: the argument (angle)
            self.take()
            inner = self.sum()
            self.expect_op("<")
            a = ("call", "arg", inner)
        else:
            a = self.atom()
        while self.peek() == ("op", "[") or (self.peek() == ("op", ":") and self.peek(1)[0] in ("param", "path")):
            if self.take() == ("op", "["):
                a = ("index", a, self.index())
                self.expect_op("]")
            else:                      # test_points[k]:$name, test_points[k]:hotfire.txt
                kind, text = self.take()
                if kind not in ("param", "path"):
                    raise self.err("expected $name or a file after the point")
                a = ("pref", a, text)
        while self.peek() == ("op", "‾"):             # z‾: the conjugate
            self.take()
            a = ("call", "conj", a)
        if self.peek() in (("op", "**"), ("op", "\\\\")):     # x**p and x\\n, right to left
            op = self.take()[1]
            a = ("call", "power" if op == "**" else "root", a, self.unary())
        return ("typed", words, a) if words else a

    def index(self):
        depth, j = 0, self.i
        while j < len(self.t):         # v[0:3] and v[k:end] read 0:3 and k:end as one token: split them
            kind, text = self.t[j]
            if (kind, text) in (("op", "["), ("op", "(")):
                depth += 1
            elif (kind, text) in (("op", "]"), ("op", ")")):
                if depth == 0:
                    break
                depth -= 1
            elif kind == "ref" and depth == 0 and re.fullmatch(r"[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", text):
                parts = _sig_tokens(text.replace(":", " : "), self.line)
                self.t[j:j + 1] = parts
                j += len(parts) - 1
            j += 1
        specs = [self.index_one()]
        while self.peek() == ("op", ","):          # A[i, j]: row, column
            self.take()
            specs.append(self.index_one())
        return specs[0] if len(specs) == 1 else ("multi",) + tuple(specs)

    def index_one(self):
        if self.peek() == ("op", ":") and self.peek(1) in (("op", ","), ("op", "]")):
            self.take()
            return ("all",)
        lo = self.index_end()
        if self.peek() == ("op", ":"):
            self.take()
            return ("slice", lo, self.index_end())
        return lo

    def index_end(self):
        if self.kw("end"):
            self.take()
            off = None
            if self.peek() == ("op", "-"):
                self.take()
                off = self.product()
            return ("end", off)
        return self.expr()

    def atom(self):
        kind, text = self.peek()
        if (kind, text) in (("name", "sample"), ("param", "$sample")) and self.peek(1) == ("op", "("):
            self.take()
            return ("sample", "$" if kind == "param" else "") + tuple(self.args())
        if (kind, text) == ("op", "@"):
            self.take()
            if self.peek() != ("name", "sample") or self.peek(1) != ("op", "("):
                raise self.err("@ goes before sample: @sample(stream)")
            self.take()
            return ("sample", "@") + tuple(self.args())
        if kind == "name" and text == "input" and self.peek(1) == ("op", "("):
            self.take()
            self.take()
            words = self.types()
            prompt = None
            if words and self.peek() == ("op", ","):
                self.take()
            if self.peek() != ("op", ")"):
                prompt = self.expr()
            self.expect_op(")")
            return ("input", words, prompt)
        if self.callable_name():
            name = self.take()[1]
            return ("call", name) + tuple(self.args())
        if kind == "string":
            self.take()
            return ("str", text[1:-1])
        if kind == "num":
            self.take()
            return ("num", float(text) if re.search(r"[.eE]", text) else int(text))
        if kind == "imag":
            self.take()
            return ("imag", float(text[:-2] if text.endswith("im") else text[:-1]))
        if kind == "bitsval":
            self.take()
            if len(text) - 2 > _BITS_MAX:
                raise self.err(f"bits can be {_BITS_MAX} wide at most, got {len(text) - 2}")
            return ("bits", text[2:])
        if (kind, text) == ("op", "{"):
            self.take()
            lo = self.expr()
            self.expect_op(",")
            hi = self.expr()
            self.expect_op("}")
            return ("range", lo, hi)
        if kind == "name" and text == "im":          # the imaginary unit, as in Julia
            self.take()
            return ("imag", 1.0)
        if kind in ("ref", "path"):
            self.take()
            return ("ref", text)
        if kind == "param":
            self.take()
            return ("param", text[1:])
        if kind == "pparam":
            self.take()
            point, name = text.split(":$")
            return ("pparam", point, name)
        if self.kw("autorun"):
            self.take()
            return ("autorun",)
        if kind == "name" and text.lower() in ("complete", "failed", "broken", "running") and self.peek(1) == ("op", "("):
            self.take()
            self.take()
            point = self.point()
            self.expect_op(")")
            return ("state", text.lower(), point)
        if self.kw("column"):
            self.take()
            self.expect_op("(")
            f = self.expr()
            self.expect_op(",")
            col = self.take()
            if col[0] != "name":
                raise self.err("expected column(file, name)")
            self.expect_op(")")
            return ("column", f, col[1])
        if (kind, text) == ("op", "["):
            self.take()
            self.depth = getattr(self, "depth", 0) + 1
            if self.depth > _MAX_NEST:
                raise self.err(f"vectors can nest {_MAX_NEST} deep at most")
            rows = [[]]                # [1, 2; 3, 4]: rows of a 2-D array
            while self.peek() != ("op", "]"):
                rows[-1].append(self.expr())
                if self.peek() == ("op", ";"):
                    self.take()
                    rows.append([])
                    continue
                if self.peek() != ("op", ","):
                    break
                self.take()
            self.expect_op("]")
            self.depth -= 1
            if len(rows) == 1:
                return ("vec",) + tuple(rows[0])
            if len(rows) > 1 and not rows[-1]:     # [1, 2, 3;] is a matrix of one row
                rows.pop()
            if any(not r for r in rows):
                raise self.err("a row of a matrix is empty")
            if len({len(r) for r in rows}) > 1:
                raise self.err("the rows of a matrix must all be the same length, got " + ", ".join(str(len(r)) for r in rows))
            # written row by row, kept column by column, as Julia and Fortran
            return ("vec",) + tuple(("vec",) + tuple(r[j] for r in rows) for j in range(len(rows[0])))
        if (kind, text) == ("op", "("):
            self.take()
            a = self.expr()
            self.expect_op(")")
            return a
        if self.kw("true") or self.kw("false"):
            return ("bool", self.take()[1].lower() == "true")
        if self.kw("multiplex"):
            self.take()
            self.expect_op("(")
            fields = []
            while True:
                k, t = self.peek()
                a = self.atom()
                if a[0] == "ref" and t.endswith(".sig"):
                    fields.append((t.split(":", 1)[-1], a))
                elif a[0] == "name":
                    fields.append((t, a))
                else:
                    raise self.err("multiplex takes live signals and variables")
                if self.peek() != ("op", ","):
                    break
                self.take()
            self.expect_op(")")
            return ("mux", fields)
        if self.kw("demultiplex"):
            self.take()
            self.expect_op("(")
            k, t = self.take()
            bus, _, fld = (t or "").partition(".")
            if k != "path" or not _PARAM_NAME.match(bus) or not fld:
                raise self.err("expected demultiplex(bus.field)")
            self.expect_op(")")
            return ("demux", bus, fld)
        if kind == "name" and text.lower() not in _KEYWORDS:
            self.take()
            return ("name", text)
        raise self.err(f"expected a value, got {text!r}" if text else "expected a value")


def parse_signals(code: str, inputs=()) -> list:
    """Signet code as a list of statements, blocks nested as ("if", line, cond, then, else) and
    ("when", line, cond, within, by, text, body). `inputs` are names already declared (a module's input ports).
    Raises ValueError listing every bad line."""
    errors, flat = [], []
    in_where = False
    for n, text in enumerate(str(code or "").splitlines(), 1):
        try:
            toks = _sig_tokens(text, n)
            if not toks:
                in_where = in_where and bool(text.strip())    # a blank line ends a where block
                continue
            parser = _LineParser(toks, n, text)
            if in_where and text[:1] in (" ", "\t") and parser.is_where_item():
                node = parser.where_item()
            else:
                node = parser.statement()
                in_where = node[0] == "let" and node[5]
            flat.append(node)
        except SignalError as e:
            errors.append(str(e))
            in_where = False
        except RecursionError:
            errors.append(f"line {n}: too deeply nested to read")
            in_where = False
    root = []
    stack = [(None, root)]
    inside = {"if": "an if", "when": "a when", "for": "a loop", "function": "a function", "subprocess": "a subprocess"}
    for node in flat:
        kind, line = node[0], node[1]
        if kind == "if":
            block = ["if", line, node[2], [], None]
            stack[-1][1].append(block)
            stack.append((block, block[3]))
        elif kind == "when":
            block = ["when", line, node[2], node[3], node[4], node[5], []]
            stack[-1][1].append(block)
            stack.append((block, block[6]))
        elif kind == "for":
            block = ["for", line, node[2], node[3], node[4], node[5], []]
            stack[-1][1].append(block)
            stack.append((block, block[6]))
        elif kind in ("function", "subprocess", "fshort"):
            if len(stack) > 1:
                errors.append(f"line {line}: a {kind.replace('fshort', 'function')} can only be defined at the top, not inside {inside[stack[-1][0][0]]}")
            if kind == "fshort":
                stack[-1][1].append(["function", line, node[2], node[3], None, None, [("return", line, node[4])]])
            else:
                block = [kind, line, node[2], node[3], None, None, []]
                stack[-1][1].append(block)
                stack.append((block, block[6]))
        elif kind == "next":
            block = stack[-1][0]
            if block is None or block[0] != "for":
                errors.append(f"line {line}: next without for")
                continue
            if block[2] != node[2]:
                errors.append(f"line {line}: next {node[2]} doesn't match for {block[2]} on line {block[1]}")
            stack.pop()
        elif kind == "else":
            block = stack[-1][0]
            if block is None or block[0] != "if" or block[4] is not None:
                errors.append(f"line {line}: else without if")
                continue
            block[4] = []
            stack[-1] = (block, block[4])
        elif kind == "end":
            block = stack[-1][0]
            if node[2] == "":
                if block is None or block[0] not in ("function", "subprocess"):
                    errors.append(f"line {line}: end what? write end if, end when, end function or end subprocess")
                    continue
            elif block is None or block[0] != node[2]:
                errors.append(f"line {line}: end {node[2]} without {node[2]}")
                continue
            stack.pop()
        else:
            if kind == "let" and len(stack) > 1 and stack[-1][0][0] in ("if", "when", "for"):
                errors.append(f"line {line}: let and where can't be inside " + inside[stack[-1][0][0]])
            stack[-1][1].append(node)
    for block, _ in stack[1:]:
        errors.append(f"line {block[1]}: {block[0]} without {'next' if block[0] == 'for' else 'end ' + block[0]}")
    names = {n: False for n in inputs}
    funcs = {}                         # name -> (kind, argument count), in order of definition
    scope = {"kind": None, "locals": None, "name": None}
    banned = {"connect": "connect", "route": "route", "run": "run", "when": "when", "finish": "finish", "delay": "delay",
              "escape": "escape", "echo": "echo", "break": "break", "setindex": "index", "output": "output", "lineage": "lineage",
              "callstmt": "a subprocess call"}
    unpure = {"ref": "signals and files", "pref": "signals and files", "param": "parameters", "pparam": "parameters",
              "column": "files", "state": "run states", "autorun": "AUTORUN", "input": "answers"}
    def define(nd):
        name, params, body = nd[2], nd[3], nd[6]
        if name in _BUILTINS:
            errors.append(f"line {nd[1]}: {name} is built in and can't be redefined")
        if name in names:
            errors.append(f"line {nd[1]}: {name} is already declared")
        backup, old = dict(names), dict(scope)
        scope["kind"], scope["locals"], scope["name"] = nd[0], set(), name
        for _, p in params:
            names[p] = False
            scope["locals"].add(p)
        walk(body)
        if nd[0] == "function" and not any(x[0] == "return" for x in _walk(body)):
            errors.append(f"line {nd[1]}: function {name} has no return")
        names.clear()
        names.update(backup)
        scope.update(old)
        funcs[name] = (nd[0], len(params))
        names[name] = "function"
    def walk(nodes):
        for nd in nodes:
            if scope["kind"] == "function" and nd[0] in banned:
                errors.append(f"line {nd[1]}: {banned[nd[0]]} isn't allowed in a function; use a subprocess")
                continue
            if nd[0] in ("function", "subprocess"):
                define(nd)
            elif nd[0] == "return":
                if scope["kind"] is None:
                    errors.append(f"line {nd[1]}: return is only for functions and subprocesses")
                elif scope["kind"] == "function" and nd[2] is None:
                    errors.append(f"line {nd[1]}: return needs a value")
                elif scope["kind"] == "subprocess" and nd[2] is not None:
                    errors.append(f"line {nd[1]}: a subprocess returns nothing")
                if nd[2] is not None:
                    check(nd[2], nd[1])
            elif nd[0] == "callstmt":
                f = funcs.get(nd[2])
                if f is None and nd[2] == scope["name"]:
                    errors.append(f"line {nd[1]}: {nd[2]} can't call itself")
                elif f is None:
                    errors.append(f"line {nd[1]}: unknown subprocess {nd[2]}" if nd[2] not in names else f"line {nd[1]}: {nd[2]} isn't a subprocess")
                elif f[0] != "subprocess":
                    errors.append(f"line {nd[1]}: {nd[2]} is a function and returns a value: use it in an expression")
                elif f[1] != len(nd[3]):
                    errors.append(f"line {nd[1]}: {nd[2]} takes {f[1]} value{'s' * (f[1] != 1)}, got {len(nd[3])}")
                for a in nd[3]:
                    check(a, nd[1])
            elif nd[0] == "output":
                check(nd[4], nd[1])
            elif nd[0] == "let":
                check(nd[4], nd[1])
                if nd[3] in _BUILTINS:
                    errors.append(f"line {nd[1]}: {nd[3]} is built in and can't be a variable")
                if scope["locals"] is not None:
                    if nd[5]:
                        errors.append(f"line {nd[1]}: where can't be inside a {scope['kind']}; use let")
                    if nd[3] in scope["locals"] or names.get(nd[3]) == "function":
                        errors.append(f"line {nd[1]}: {nd[3]} is already declared")
                    scope["locals"].add(nd[3])
                elif nd[3] in names:
                    errors.append(f"line {nd[1]}: {nd[3]} is already declared")
                names[nd[3]] = nd[5]
            elif nd[0] == "set":
                check(nd[3], nd[1])
                if nd[2] not in names:
                    errors.append(f"line {nd[1]}: unknown name {nd[2]}")
                elif names[nd[2]] == "loop":
                    errors.append(f"line {nd[1]}: {nd[2]} is a loop variable and can't be changed")
                elif names[nd[2]] == "function":
                    errors.append(f"line {nd[1]}: {nd[2]} is a function")
                elif names[nd[2]]:
                    errors.append(f"line {nd[1]}: {nd[2]} is a constant")
                elif scope["locals"] is not None and nd[2] not in scope["locals"]:
                    errors.append(f"line {nd[1]}: a {scope['kind']} can't change {nd[2]}, which is declared outside it")
            elif nd[0] == "connect":
                check(nd[2], nd[1])
                if isinstance(nd[6], tuple):
                    check(nd[6], nd[1])
            elif nd[0] == "route":
                check(nd[3], nd[1])
                for it in nd[4]:
                    if nd[2] == "merge":
                        check(it, nd[1])
                    elif isinstance(it[3], tuple):
                        check(it[3], nd[1])
            elif nd[0] == "setindex":
                check(nd[2], nd[1])
            elif nd[0] in ("run", "escape") and isinstance(nd[2], tuple):
                check(nd[2], nd[1])
                if nd[0] == "escape" and nd[3] is not None:
                    check(nd[3], nd[1])
            elif nd[0] == "for":
                for e in (nd[3], nd[4], nd[5]):
                    if e is not None:
                        check(e, nd[1])
                clash = nd[2] in (scope["locals"] if scope["locals"] is not None else names)
                if clash:
                    errors.append(f"line {nd[1]}: {nd[2]} is already declared")
                names[nd[2]] = "loop"
                if scope["locals"] is not None:
                    scope["locals"].add(nd[2])
                walk(nd[6])
                names.pop(nd[2], None)
                if scope["locals"] is not None:
                    scope["locals"].discard(nd[2])
            elif nd[0] == "echo":
                for a in nd[2]:
                    check(a, nd[1])
            elif nd[0] == "delay":
                check(nd[2], nd[1])
            elif nd[0] == "if":
                check(nd[2], nd[1])
                walk(nd[3])
                walk(nd[4] or [])
            elif nd[0] == "when":
                for e in nd[2:5]:
                    if e is not None:
                        check(e, nd[1])
                walk(nd[6])
    def check_builtin(e, line):
        who, nf, args = e[1], _FNARGS[e[1]], e[2:]
        if who == "warp" and len(args) != 3:
            errors.append(f"line {line}: warp takes two functions and a series: warp(fx, fy, series)")
            args = args[:0]
        elif who == "map" and len(args) < 2:
            errors.append(f"line {line}: map takes a function and a vector: map(f, vector)")
            args = args[:0]
        for a in args[:nf]:
            n = a[1] if a[0] == "name" else None
            f = funcs.get(n)
            if n == "unity":
                if who == "map" and len(args) != 2:
                    errors.append(f"line {line}: unity takes 1 value, map gives it {len(args) - 1}")
            elif n is None:
                errors.append(f"line {line}: {who} needs a function name where a function goes")
            elif f is None and n == scope["name"]:
                errors.append(f"line {line}: {n} can't call itself")
            elif f is None:
                errors.append(f"line {line}: unknown function {n}" if n not in names else f"line {line}: {n} isn't a function")
            elif f[0] != "function":
                errors.append(f"line {line}: {n} is a subprocess and returns nothing: {who} needs a function")
            else:
                want = len(args) - 1 if who == "map" else 1
                if f[1] != want:
                    errors.append(f"line {line}: {n} takes {f[1]} value{'s' * (f[1] != 1)}, {who} gives it {want}")
        for a in args[nf:]:
            check(a, line)
    def check(e, line):
        if e[0] == "hexraw":
            return
        if e[0] == "pexpr":
            return check(e[1], line)
        if e[0] == "sample" and len(e) != 3:
            errors.append(f"line {line}: {e[1]}sample takes one stream, got {len(e) - 2}")
        if e[0] == "call" and e[1] == "freeze":
            if len(e) != 3:
                errors.append(f"line {line}: freeze takes one stream, got {len(e) - 2}")
            if scope["kind"] == "function":
                errors.append(f"line {line}: {_NO_STREAMS}")
            for a in e[2:]:
                check(a, line)
            return
        if e[0] == "call" and e[1] in _FNARGS:
            return check_builtin(e, line)
        if e[0] == "call" and e[1] in _MATH:
            if len(e) - 2 != _MATH[e[1]]:
                n = _MATH[e[1]]
                errors.append(f"line {line}: {e[1]} takes {n} value{'s' * (n != 1)}, got {len(e) - 2}")
            for a in e[2:]:
                check(a, line)
            return
        if scope["kind"] == "function" and e[0] == "sample":
            errors.append(f"line {line}: {_NO_STREAMS}")
        elif scope["kind"] == "function" and e[0] in unpure:
            errors.append(f"line {line}: a function can't read {unpure[e[0]]}; pass them in as arguments")
        if e[0] == "name" and e[1] not in names:
            errors.append(f"line {line}: unknown name {e[1]}")
        elif e[0] == "name" and names[e[1]] == "function":
            errors.append(f"line {line}: {e[1]} is a function: call it as {e[1]}(...)")
        elif e[0] == "call":
            f = funcs.get(e[1])
            if f is None and e[1] == scope["name"]:
                errors.append(f"line {line}: {e[1]} can't call itself")
            elif f is None:
                errors.append(f"line {line}: unknown function {e[1]}" if e[1] not in names else f"line {line}: {e[1]} isn't a function")
            elif f[0] != "function":
                errors.append(f"line {line}: {e[1]} is a subprocess and returns nothing: call it on a line of its own")
            elif f[1] != len(e) - 2:
                errors.append(f"line {line}: {e[1]} takes {f[1]} value{'s' * (f[1] != 1)}, got {len(e) - 2}")
        elif e[0] == "demux" and e[1] not in names:
            errors.append(f"line {line}: unknown name {e[1]}")
        elif e[0] == "mux":
            for _, a in e[1]:
                check(a, line)
        for sub in e[1:]:
            if isinstance(sub, tuple):
                check(sub, line)
    walk(root)
    if errors:
        raise ValueError("\n".join(sorted(set(errors), key=lambda s: int(s.split()[1].rstrip(":")))))
    return root


# Types
def _is_num(v) -> bool:
    return type(v) in (int, float)


def _real_array(a, depth: int):
    """Shape of a nested list of numbers/null `depth` deep, or None."""
    if depth == 0:
        return () if (a is None or _is_num(a)) else None
    if not isinstance(a, list):
        return None
    shapes = {_real_array(e, depth - 1) for e in a}
    if None in shapes or len(shapes) > 1:
        return None
    return (len(a),) + (shapes.pop() if shapes else ())


def _complex_depth(v):
    # Depth of a {"re", "im"} value, or None.
    if not (isinstance(v, dict) and "re" in v and "im" in v):
        return None
    d, a = 0, v["re"]
    while isinstance(a, list):
        d += 1
        a = a[0] if a else None
    sr, si = _real_array(v["re"], d), _real_array(v["im"], d)
    return d if sr is not None and sr == si else None


def _series_parts(v):
    """(domain, kind) of a valid series, kind "float" or "complex", or None."""
    if not isinstance(v, dict) or not isinstance(v.get("domain"), str) or "values" not in v:
        return None
    dom, vals = v["domain"], v["values"]
    if not _PARAM_NAME.match(dom) or dom.lower() in _TYPE_WORDS:
        return None
    if _real_array(vals, 1) is not None:
        kind, n = "float", len(vals)
    elif _complex_depth(vals) == 1:
        kind, n = "complex", len(vals["re"])
    else:
        return None
    has_x, has_step = "x" in v, ("start" in v or "step" in v)
    if has_x == has_step:
        return None
    if has_x:
        x = v["x"]
        if not (isinstance(x, list) and len(x) == n and all(_is_num(e) and math.isfinite(e) for e in x)):
            return None
    elif not (_is_num(v.get("start")) and _is_num(v.get("step")) and v["step"] != 0):
        return None
    return dom.lower(), kind


def _is_bus(v) -> bool:
    return isinstance(v, dict) and bool(v) and all(isinstance(e, dict) and "value" in e for e in v.values())


def infer_type(v) -> str:
    """The narrowest type string for a JSON value ("" if none fits)."""
    if isinstance(v, Hex):
        return "hex"
    if isinstance(v, Bits):
        return _bits_type(v.width)
    if type(v) is bool:
        return "bool"
    if type(v) is int:
        return "int"
    if v is None or type(v) is float:
        return "float"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        if _is_range(v):
            return _range_type(v)
        d = _complex_depth(v)
        if d is not None:
            return "vector::" * d + "complex"
        s = _series_parts(v)
        if s:
            return f"series::{s[0]}::{s[1]}"
        if _stream_paths(v) is not None:
            return "stream"
        return "bus" if _is_bus(v) else ""
    if isinstance(v, list):
        inner = {infer_type(e) for e in v}
        heads = {t.rpartition("::")[0] for t in inner}
        if inner and len(heads) == 1 and {t.rpartition("::")[2] for t in inner} <= {"int", "float"}:
            head = heads.pop()                 # ints and floats together are floats, at any depth
            inner = {(head + "::" if head else "") + ("float" if any(t.endswith("float") for t in inner) else "int")}
        return "vector::" + inner.pop() if len(inner) == 1 and "" not in inner else "vector"
    return ""


def type_ok(words, sv: _SV) -> bool:
    """Whether a value matches a type, given as its :: words."""
    if not words:
        return True
    w, rest, v = words[0], words[1:], sv.value
    if w == "text":
        return True
    if sv.file:
        return len(words) == 1 and w not in _TYPE_WORDS and str(v).lower().endswith("." + w.lower())
    if w in ("int", "float", "real", "num", "bool", "complex", "bus", "hex", "tuple") and rest:
        return False
    if w == "tuple":
        return isinstance(v, list) and sv.type in ("", "tuple")
    if w == "bits":
        return isinstance(v, Bits) and (not rest or rest == [str(v.width)])
    if w == "range":
        return _is_range(v) and (not rest or rest in (["float"], ["real"], ["num"]) or rest == ["int"] and _range_type(v) == "range::int")
    if w == "stream":
        if _stream_paths(v) is None:
            return False
        have = sv.type.split("::")[1:]          # empty while the stream has no header yet
        return not have or all(r == "num" or (r if r != "real" else "float") == have[1] if r in ("float", "real", "complex", "num")
                               else r.lower() == have[0] for r in rest)
    if w == "hex":
        return isinstance(v, Hex)
    if w == "int":
        return type(v) is int or isinstance(v, Hex)
    if w in ("float", "real"):
        return v is None or _is_num(v) or isinstance(v, Hex)
    if w == "num":                     # a real or a complex number
        return v is None or _is_num(v) or isinstance(v, Hex) or _complex_depth(v) == 0
    if w == "bool":
        return type(v) is bool
    if w == "complex":
        return _complex_depth(v) == 0
    if w == "bus":
        return _is_bus(v)
    if w == "vector":
        k = 0
        while k < len(words) and words[k] == "vector":
            k += 1
        if words[k:] == ["complex"]:
            return _complex_depth(v) == k
        if words[k:] == ["num"] and isinstance(v, dict):
            return _complex_depth(v) == k
        return isinstance(v, list) and all(type_ok(rest, _SV(e)) for e in v)
    if w == "series":
        s = _series_parts(v)
        if not s:
            return False
        for r in rest:
            if r in ("float", "real", "complex", "num"):
                if r == "num":
                    continue
                if (r if r != "real" else "float") != s[1]:
                    return False
            elif r.lower() != s[0]:
                return False
        return True
    return False                       # a file extension, but not a file


# Live signals and files
def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sig_hash(obj: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in obj.items() if k != "hash"}).encode()).hexdigest()


def _denan(v):
    # NaN and infinity -> None, at any depth, so a .sig is strict JSON.
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, list):
        return [_denan(e) for e in v]
    if isinstance(v, dict):
        return {k: _denan(e) for k, e in v.items()}
    return v


def read_sig(path: str):
    """A stamped .sig as a dict, or raises ValueError saying why not."""
    try:
        if os.path.getsize(path) > _SIG_MAX:
            raise ValueError("is over 10 MB")
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("can't be read")
    if not isinstance(obj, dict) or "value" not in obj or not obj.get("hash"):
        raise ValueError("has no valid stamp")
    if obj["hash"] != _sig_hash(obj):
        raise ValueError("has no valid stamp")
    return obj


# Running Signet code
def _show_type(sv: _SV) -> str:
    return sv.type or infer_type(sv.value) or "a value"


def _typed(words, sv: _SV, line: int) -> _SV:
    if sv.type == "text" and sv.source.startswith("$") and words == ["bool"] and sv.value in ("True", "False"):
        sv = _SV(sv.value == "True", "bool", sv.source)
    if words == ["int"] and isinstance(sv.value, (Hex, Bits)):
        return _SV(int(sv.value), "int", sv.source)
    if words[:1] == ["bits"] and not sv.file and (type(sv.value) is int or isinstance(sv.value, (Hex, Bits))):
        return _to_bits(words, sv, line)
    if words == ["hex"] and isinstance(sv.value, Bits):
        if sv.value.width > 32:
            raise SignalError(line, f"bits::{sv.value.width} is too wide for hex (32 bits)")
        return _SV(Hex(int(sv.value)), "hex", sv.source)
    if words == ["range", "float"] and _is_range(sv.value) and not sv.file:
        return _SV({"lo": float(sv.value["lo"]), "hi": float(sv.value["hi"])}, "range::float", sv.source)
    if words == ["hex"] and type(sv.value) is int and not sv.file and 0 <= sv.value <= _HEX_MAX:
        return _SV(Hex(sv.value), "hex", sv.source)
    if words == ["tuple"] and isinstance(sv.value, list) and not sv.file and sv.type.count("::") <= 1:
        return _SV(sv.value, "tuple", sv.source)
    if not type_ok(words, sv):
        raise SignalError(line, f"expected {'::'.join(words)}, got {_show_type(sv)}")
    return sv if sv.type else _SV(sv.value, "::".join(words), sv.source, sv.file)


def _to_bits(words, sv: _SV, line: int) -> _SV:
    """bits::8 x: an int, hex or bits as a bit field; a hex is 32 wide unless a width is given."""
    v = sv.value
    if len(words) > 2 or (len(words) == 2 and not words[1].isdigit()):
        raise SignalError(line, f"expected bits::<width>, such as bits::8, got {'::'.join(words)}")
    w = int(words[1]) if len(words) == 2 else None
    if w is not None and not 1 <= w <= _BITS_MAX:
        raise SignalError(line, f"bits are 1 to {_BITS_MAX} wide, got bits::{w}")
    if isinstance(v, Bits):
        if w is None or w >= v.width:             # a wider field fills with zeros
            return _SV(Bits(int(v), w or v.width), _bits_type(w or v.width), sv.source)
        raise SignalError(line, f"bits::{v.width} doesn't fit in bits::{w}")
    width = w or (32 if isinstance(v, Hex) else None)
    if width is None:
        raise SignalError(line, "give bits a width, such as bits::8")
    if not 0 <= int(v) < 1 << width:
        raise SignalError(line, f"{int(v)} doesn't fit in bits::{width}")
    return _SV(Bits(int(v), width), _bits_type(width), sv.source)


def _unhex(sv: _SV) -> _SV:
    """A hex as the plain int it is, for working with anything that isn't a hex."""
    return _SV(int(sv.value), "int", sv.source) if isinstance(sv.value, Hex) else sv


def _hex_arith(op, a: _SV, b: _SV, line: int) -> _SV:
    x, y = int(a.value), int(b.value)
    if op == "/" and y == 0:
        raise SignalError(line, "division by zero")
    r = x + y if op == "+" else x - y if op == "-" else x * y if op == "*" else x // y
    if not 0 <= r <= _HEX_MAX:
        raise SignalError(line, f"hex result {r} is out of range (0 to FFFFFFFF)")
    return _SV(Hex(r), "hex")


def _complex_arith(op, a: _SV, b: _SV, line: int) -> _SV:
    def num(x):
        v = x.value
        if x.type == "complex" and isinstance(v, dict):
            return complex(math.nan if v["re"] is None else v["re"], math.nan if v["im"] is None else v["im"])
        if x.file or not _is_num(v) and not isinstance(v, Hex):
            raise SignalError(line, f"{op} needs numbers, got {_show_type(x)}")
        return complex(int(v) if isinstance(v, Hex) else v)
    x, y = num(a), num(b)
    if op == "/" and y == 0:
        raise SignalError(line, "division by zero")
    r = x + y if op == "+" else x - y if op == "-" else x * y if op == "*" else x / y
    part = lambda f: None if math.isnan(f) else f
    return _SV({"re": part(r.real), "im": part(r.imag)}, "complex")


def _is_vec(sv: _SV) -> bool:
    return isinstance(sv.value, list) or (sv.type == "vector::complex" and isinstance(sv.value, dict))


def _is_array(sv: _SV) -> bool:
    """A vector or array, real or complex."""
    return _is_vec(sv) or (sv.type.startswith("vector::") and sv.type.endswith("complex") and isinstance(sv.value, dict))


def _items(sv: _SV) -> list:
    v = sv.value
    return [{"re": r, "im": i} for r, i in zip(v["re"], v["im"])] if isinstance(v, dict) else v


def _wrap(x) -> _SV:
    if isinstance(x, dict):
        return _SV(x, "complex")
    if isinstance(x, list):
        return _SV(x, infer_type(x) or "vector")
    return _SV(x, infer_type(x))


def _broadcast(op, shown, a: _SV, b: _SV, line: int) -> _SV:
    """op (+ - * /) elementwise over vectors, or a scalar against each element. `shown` is what was written."""
    va, vb = _is_vec(a), _is_vec(b)
    if not va and not vb:
        return _arith(op, a, b, line)
    if va and vb:
        la, lb = _items(a), _items(b)
        if len(la) != len(lb):
            raise SignalError(line, f"{shown} needs vectors of the same length, got {len(la)} and {len(lb)} values")
        out = [_broadcast(op, shown, _wrap(x), _wrap(y), line) for x, y in zip(la, lb)]
    elif va:
        out = [_broadcast(op, shown, _wrap(x), b, line) for x in _items(a)]
    else:
        out = [_broadcast(op, shown, a, _wrap(y), line) for y in _items(b)]
    return _gather(out, shown, line)


def _gather(out: list, shown: str, line: int) -> _SV:
    """A vector from a list of scalar results."""
    vals = [o.value for o in out]
    if vals and all(o.type == "complex" for o in out):
        return _SV({"re": [v["re"] for v in vals], "im": [v["im"] for v in vals]}, "vector::complex")
    if any(o.type == "complex" for o in out):
        raise SignalError(line, f"{shown} gives a mix of complex and real values")
    t = infer_type(vals) or "vector"
    if t == "vector::float":
        vals = [float(v) if type(v) is int else v for v in vals]
    return _SV(vals, t)


def _re_im(which: str, a: _SV, line: int) -> _SV:
    """Re(z) or Im(z): the real or imaginary part of a complex number, vector or array. A real value has Im 0."""
    v = a.value
    a = _unhex(a)
    v = a.value
    if a.file or a.type == "bool" or isinstance(v, str):
        raise SignalError(line, f"{which} needs a complex or real number, got {_show_type(a)}")
    if isinstance(v, dict) and a.type.endswith("complex"):
        part = v["re" if which == "Re" else "im"]
        return _SV(part, "vector::" * _complex_depth(v) + "float")
    def conv(x):
        if isinstance(x, list):
            return [conv(e) for e in x]
        if x is not None and not _is_num(x):
            raise SignalError(line, f"{which} needs a complex or real number, got {_show_type(a)}")
        return None if x is None else float(x) if which == "Re" else 0.0
    out = conv(v)
    return _SV(out, a.type.replace("int", "float") if a.type.startswith("vector") else "float")


def _real_or_complex(name, sv, line):
    """A Python int, float or complex (NaN for a missing value) from a scalar, or an error."""
    sv = _unhex(sv)
    v = sv.value
    if sv.type == "complex" and isinstance(v, dict) and not isinstance(v["re"], list):
        return complex(math.nan if v["re"] is None else v["re"], math.nan if v["im"] is None else v["im"])
    if sv.file or sv.type == "bool" or not (v is None or _is_num(v)):
        raise SignalError(line, f"{name} needs a real or complex number, got {_show_type(sv)}")
    return math.nan if v is None else v


def _cres(z) -> _SV:
    part = lambda f: None if math.isnan(f) else f
    return _SV({"re": part(z.real), "im": part(z.imag)}, "complex")


def _fres(x) -> _SV:
    return _SV(None if isinstance(x, float) and math.isnan(x) else x, "int" if type(x) is int else "float")


def _isnan(x) -> bool:
    return cmath.isnan(x) if isinstance(x, complex) else isinstance(x, float) and math.isnan(x)


def _maths_scalar(name, args, line) -> _SV:
    x = _real_or_complex(name, args[0], line)
    if name in ("abs", "arg", "conj"):
        if name == "abs":
            return _fres(abs(x))
        if name == "arg":
            return _fres(math.atan2(x.imag, x.real) if isinstance(x, complex) else math.atan2(0.0, x))
        return _cres(x.conjugate()) if isinstance(x, complex) else args[0]
    p = _real_or_complex(name, args[1], line)
    if _isnan(x) or _isnan(p):
        return _SV(None, "float")
    try:
        return _power(x, p) if name == "power" else _root(x, p, line)
    except OverflowError:
        raise SignalError(line, f"{name} gives a result too large to hold")
    except ZeroDivisionError:
        raise SignalError(line, "division by zero")


def _power(x, p) -> _SV:
    if type(x) is int and type(p) is int and p >= 0:
        r = x ** p if p < 4096 else float(x) ** p
        return _SV(r, "int") if type(r) is float or abs(r) <= 2 ** 63 - 1 else _fres(float(r))
    if isinstance(x, complex) or isinstance(p, complex) or (x < 0 and p != int(p)):
        if x == 0:
            if complex(p).real > 0:
                return _cres(0j)
            raise ZeroDivisionError
        return _cres(complex(x) ** p)
    return _fres(float(x) ** p)


def _root(x, n, line) -> _SV:
    if isinstance(n, complex):
        raise SignalError(line, "root needs a real root, such as 2 or 3")
    if n == 0:
        raise SignalError(line, "root 0 isn't defined")
    if n == 2 and (isinstance(x, complex) or x < 0):
        return _cres(cmath.sqrt(x))
    if isinstance(x, complex):
        return _cres(x ** (1 / n))
    if x < 0:
        if n == int(n) and int(n) % 2:
            return _fres(-((-x) ** (1 / n)))
        return _cres(complex(x) ** (1 / n))
    r = float(x) ** (1 / n)
    if n == int(n) and n > 0 and round(r) ** int(n) == x:     # 64 \\ 3 is 4.0, not 3.9999999999999996
        r = float(round(r))
    return _fres(r)


def _maths(name, args, line) -> _SV:
    """Re, Im, abs, arg, conj, power and root, on a real or complex number; on every value of a vector."""
    if name in ("Re", "Im"):
        return _re_im(name, args[0], line)
    if len(args) > 1 and _is_vec(args[1]):
        raise SignalError(line, f"{name} takes one number for the {'exponent' if name == 'power' else 'root'}, not a vector")
    if not _is_vec(args[0]):
        return _maths_scalar(name, args, line)
    if isinstance(args[0].value, dict) and any(isinstance(r, list) for r in args[0].value["re"]):
        raise SignalError(line, f"{name} takes a vector, not a complex array")
    out = [_maths(name, [_wrap(item)] + args[1:], line) for item in _items(args[0])]
    if out and all(o.type == "complex" for o in out):
        return _SV({"re": [o.value["re"] for o in out], "im": [o.value["im"] for o in out]}, "vector::complex", "vector")
    if any(o.type == "complex" for o in out):
        raise SignalError(line, f"{name} gives complex and real values for one vector; make them all complex first")
    vals = [o.value for o in out]
    return _SV(vals, infer_type(vals) or "vector::float")


# Ranges, bits, signal shaping, vector functions and matrices
def _is_range(v) -> bool:
    return isinstance(v, dict) and set(v) == {"lo", "hi"} and all(_is_num(v[k]) for k in ("lo", "hi"))


def _range_type(v) -> str:
    return "range::int" if type(v["lo"]) is int and type(v["hi"]) is int else "range::float"


def _make_range(lo: _SV, hi: _SV, line: int) -> _SV:
    ends = []
    for x in (_unhex(lo), _unhex(hi)):
        if x.file or not _is_num(x.value):
            raise SignalError(line, f"a range's ends must be numbers, got {_show_type(x)}")
        ends.append(x.value)
    if ends[0] > ends[1]:
        raise SignalError(line, f"a range's low end is above its high end: {{{ends[0]}, {ends[1]}}}")
    v = {"lo": ends[0], "hi": ends[1]}
    return _SV(v, _range_type(v))


def _range_arg(name: str, sv: _SV, line: int):
    if sv.file or not _is_range(sv.value):
        raise SignalError(line, f"{name} needs a range such as {{0, 5}}, got {_show_type(sv)}")
    return sv.value["lo"], sv.value["hi"]


def _in_range(a: _SV, r: _SV, line: int) -> _SV:
    """x in {lo, hi}: true when x is within the range, ends included; a vector or series only if every value is."""
    lo, hi = _range_arg("in", r, line)
    v = _unhex(a).value
    if isinstance(v, dict) and _series_parts(v):
        if _series_parts(v)[1] != "float":
            raise SignalError(line, "in needs real values, not complex ones")
        v = v["values"]
    def ok(x):
        if isinstance(x, list):
            return all(ok(e) for e in x)
        if isinstance(x, Hex):
            x = int(x)
        if x is not None and not _is_num(x):
            raise SignalError(line, f"in needs numbers, got {_show_type(a)}")
        return x is not None and lo <= x <= hi
    if a.file or type(v) is bool or isinstance(v, dict):
        raise SignalError(line, f"in needs numbers, got {_show_type(a)}")
    return _SV(ok(v), "bool")


def _each_real(name: str, sv: _SV, line: int, f) -> _SV:
    """f on a real number, on every value of a vector or array, or on the values of a real series."""
    sv = _unhex(sv)
    v = sv.value
    if isinstance(v, dict) and _series_parts(v):
        dom, kind = _series_parts(v)
        if kind != "float":
            raise SignalError(line, f"{name} works on real values, not complex ones")
        new = dict(v)
        new["values"] = [None if y is None else float(f(y)) for y in v["values"]]
        return _SV(new, f"series::{dom}::float", sv.source)
    def go(x):
        if isinstance(x, list):
            return [go(e) for e in x]
        if x is None:
            return None
        if not _is_num(x):
            raise SignalError(line, f"{name} needs real numbers, got {_show_type(sv)}")
        return f(x)
    if sv.file or type(v) is bool or isinstance(v, dict) or isinstance(v, str):
        raise SignalError(line, f"{name} needs real numbers, got {_show_type(sv)}")
    out = go(v)
    if not isinstance(out, list):
        return _fres(out)
    t = infer_type(out)
    if t.endswith("float"):
        out = _floats(out)
    return _SV(out, t or "vector::float")


def _floats(x):
    return [_floats(e) for e in x] if isinstance(x, list) else float(x) if type(x) is int else x


def _saturate(name, args, line, ctx=None) -> _SV:
    lo, hi = _range_arg(name, args[1], line)
    ints = type(lo) is int and type(hi) is int
    def f(x):
        r = lo if x < lo else hi if x > hi else x
        return r if ints and type(x) is int else float(r)
    return _each_real(name, args[0], line, f)


def _excess(name, args, line, ctx=None) -> _SV:
    lo, hi = _range_arg(name, args[1], line)
    ints = type(lo) is int and type(hi) is int
    def f(x):
        r = x - lo if x < lo else x - hi if x > hi else 0
        return r if ints and type(x) is int else float(r)
    return _each_real(name, args[0], line, f)


def _quantize(name, args, line, ctx=None) -> _SV:
    q = _unhex(args[1])
    if q.file or not _is_num(q.value) or q.value <= 0:
        raise SignalError(line, f"quantize needs a step above 0, got {_show_value(q) if _is_num(q.value) else _show_type(q)}")
    q = q.value
    step = decimal.Decimal(repr(q))
    def f(x):
        k = math.floor(abs(x) / q + 0.5) * (1 if x >= 0 else -1)      # halfway goes away from zero
        if type(x) is int and type(q) is int:
            return k * q
        return float(step * k)
    return _each_real(name, args[0], line, f)


def _popcount(name, args, line, ctx=None) -> _SV:
    v = args[0].value
    if not isinstance(v, Bits):
        raise SignalError(line, f"popcount needs bits, got {_show_type(args[0])}")
    return _SV(bin(v).count("1"), "int")


def _vec_values(name: str, sv: _SV, line: int, complex_ok=True):
    """(values, complex?) of a vector or a series' values; NaN is None."""
    v = sv.value
    if isinstance(v, dict) and _series_parts(v):
        vals = v["values"]
    elif isinstance(v, list) or (isinstance(v, dict) and sv.type.endswith("complex") and isinstance(v.get("re"), list)):
        vals = v
    else:
        raise SignalError(line, f"{name} needs a vector or series, got {_show_type(sv)}")
    if isinstance(vals, dict):
        if any(isinstance(r, list) for r in vals["re"]):
            raise SignalError(line, f"{name} takes a vector, not an array")
        if not complex_ok:
            raise SignalError(line, f"{name} can't order complex numbers; use abs, Re or Im first")
        return [None if r is None or i is None else complex(r, i) for r, i in zip(vals["re"], vals["im"])], True
    out = []
    for x in vals:
        if isinstance(x, list):
            raise SignalError(line, f"{name} takes a vector, not an array")
        if isinstance(x, Hex):
            x = int(x)
        if x is not None and not _is_num(x):
            raise SignalError(line, f"{name} needs numbers, got {_show_type(sv)}")
        out.append(x)
    return out, False


def _num_sv(x) -> _SV:
    if isinstance(x, complex):
        return _cres(x)
    return _fres(x)


def _reduce(name, args, line, ctx=None) -> _SV:
    vals, _ = _vec_values(name, args[0], line)
    if name == "sum":
        return _SV(None, "float") if None in vals else _num_sv(sum(vals, 0))
    if name == "prod":
        if None in vals:
            return _SV(None, "float")
        r = 1
        for x in vals:
            r *= x
        return _num_sv(r)
    if not vals:
        raise SignalError(line, f"{name} needs at least one value")
    if None in vals:
        return _SV(None, "float")
    n = len(vals)
    if name == "mean":
        m = sum(vals, 0) / n
        return _num_sv(m if isinstance(m, complex) else float(m))
    if name == "rms":
        return _fres(math.sqrt(sum(abs(x) ** 2 for x in vals) / n))
    if name == "norm":
        return _fres(math.sqrt(sum(abs(x) ** 2 for x in vals)))
    if n < 2:
        raise SignalError(line, "std needs at least two values")
    m = sum(vals, 0) / n
    return _fres(math.sqrt(sum(abs(x - m) ** 2 for x in vals) / (n - 1)))      # n - 1, as MATLAB and Julia


def _extreme(name, args, line, ctx=None) -> _SV:
    vals, _ = _vec_values(name, args[0], line, complex_ok=False)
    if not vals:
        raise SignalError(line, f"{name} needs at least one value")
    known = [(x, i) for i, x in enumerate(vals) if x is not None]       # NaN is skipped, as in MATLAB
    if not known:
        return _SV(None, "float")
    x, i = (min if name in ("min", "argmin") else max)(known, key=lambda p: p[0])
    if name.startswith("arg"):
        return _SV(i + (ctx or {}).get("first", 0), "int")
    return _fres(x)


def _sort(name, args, line, ctx=None) -> _SV:
    vals, _ = _vec_values(name, args[0], line, complex_ok=False)
    out = sorted(x for x in vals if x is not None) + [x for x in vals if x is None]      # NaN last
    return _SV(out, infer_type(out) or "vector::float")


def _length(name, args, line, ctx=None) -> _SV:
    sv = args[0]
    v = sv.value
    if isinstance(v, Bits):
        return _SV(v.width, "int")
    if isinstance(v, dict) and _series_parts(v):
        vals = v["values"]
        return _SV(len(vals["re"]) if isinstance(vals, dict) else len(vals), "int")
    if (isinstance(v, list) or (isinstance(v, dict) and sv.type.endswith("complex") and isinstance(v.get("re"), list))) \
            and not sv.file:
        dims = _dims(_nest(sv)[0])
        if name == "size":
            return _SV(dims, "vector::int")
        return _SV(math.prod(dims), "int")              # every value, as Julia's length
    raise SignalError(line, f"{name} needs a vector, array, series or bits, got {_show_type(sv)}")


def _nest(sv: _SV):
    """(nested list, complex?) of a vector or array: a complex one with {"re", "im"} leaves."""
    v = sv.value
    if isinstance(v, dict) and isinstance(v.get("re"), list):
        def z(r, i):
            return [z(a, b) for a, b in zip(r, i)] if isinstance(r, list) else {"re": r, "im": i}
        return z(v["re"], v["im"]), True
    return v, False


def _dims(n) -> list:
    """An array's size in mathematical order, rows first. It is stored column by column, so the outer list is
    the last dimension."""
    out = []
    while isinstance(n, list):
        out.append(len(n))
        n = n[0] if n else None
    return out[::-1]


def _flat(n) -> list:
    return [x for e in n for x in _flat(e)] if isinstance(n, list) else [n]


def _unnest(n, cplx: bool, leaf_type: str, src: str) -> _SV:
    if not isinstance(n, list):
        return _SV(n, "complex" if cplx else (leaf_type or infer_type(n)), src)
    if cplx:
        def side(x, k):
            return [side(e, k) for e in x] if isinstance(x, list) else x[k]
        v = {"re": side(n, "re"), "im": side(n, "im")}
        return _SV(v, "vector::" * len(_dims(n)) + "complex", src)
    return _SV(n, infer_type(n) or "vector", src)


def _dot(name, args, line, ctx=None) -> _SV:
    a, ca = _vec_values(name, args[0], line)
    b, cb = _vec_values(name, args[1], line)
    if len(a) != len(b):
        raise SignalError(line, f"{name} needs vectors of the same length, got {len(a)} and {len(b)} values")
    if name == "cross":
        if len(a) != 3 or ca or cb:
            raise SignalError(line, "cross needs two real vectors of 3 values")
        if None in a or None in b:
            return _SV([None] * 3, "vector::float")
        out = [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
        return _SV(out, infer_type(out))
    if None in a or None in b:
        return _SV(None, "float")
    return _num_sv(sum(((x.conjugate() if isinstance(x, complex) else x) * y for x, y in zip(a, b)), 0))   # conj(a), as Julia


def _matrix(sv: _SV, line: int, what: str):
    """(rows, 2-D?) of a real or complex vector or 2-D array, as Python numbers (NaN as nan)."""
    v = sv.value
    def num(x):
        if isinstance(x, Hex):
            x = int(x)
        if x is None:
            return math.nan
        if not _is_num(x):
            raise SignalError(line, f"{what} needs numbers, got {_show_type(sv)}")
        return x
    if isinstance(v, dict) and sv.type.endswith("complex") and isinstance(v.get("re"), list):
        re_, im_ = v["re"], v["im"]
        if re_ and isinstance(re_[0], list):
            if any(isinstance(e, list) for r in re_ for e in r):
                raise SignalError(line, f"{what} works on vectors and 2-D arrays")
            cols = [[complex(num(a), num(b)) for a, b in zip(r, i)] for r, i in zip(re_, im_)]
            return [list(r) for r in zip(*cols)], True
        return [complex(num(a), num(b)) for a, b in zip(re_, im_)], False
    if not isinstance(v, list) or sv.file:
        raise SignalError(line, f"{what} needs a vector or 2-D array, got {_show_type(sv)}")
    if v and isinstance(v[0], list):
        if any(isinstance(e, list) for r in v for e in r):
            raise SignalError(line, f"{what} works on vectors and 2-D arrays")
        return [list(r) for r in zip(*[[num(e) for e in c] for c in v])], True     # stored by column
    return [num(e) for e in v], False


def _from_matrix(rows) -> _SV:
    """A vector, or a 2-D array given as rows and stored by column."""
    flat = [x for r in rows for x in (r if isinstance(r, list) else [r])]
    if rows and isinstance(rows[0], list):
        rows = [list(c) for c in zip(*rows)]
    if any(isinstance(x, complex) for x in flat):
        part = lambda f: None if math.isnan(f) else f
        def side(x, which):
            if isinstance(x, list):
                return [side(e, which) for e in x]
            z = complex(x)
            return part(z.real if which else z.imag)
        v = {"re": side(rows, True), "im": side(rows, False)}
        return _SV(v, "vector::" * _complex_depth(v) + "complex")
    def clean(x):
        if isinstance(x, list):
            return [clean(e) for e in x]
        return None if isinstance(x, float) and math.isnan(x) else x
    out = clean(rows)
    t = infer_type(out)
    if t.endswith("float"):
        out = _floats(out)
    return _SV(out, t or "vector::float")


def _matmul(a: _SV, b: _SV, line: int) -> _SV:
    """A * B: the matrix product of 2-D arrays, or of an array and a vector."""
    A, a2 = _matrix(a, line, "*")
    B, b2 = _matrix(b, line, "*")
    if not a2 and not b2:
        raise SignalError(line, "* of two vectors isn't defined; use dot(a, b), or .* for each pair of values")
    Am = A if a2 else [[x] for x in A]          # a vector is a column, as in Julia
    Bm = B if b2 else [[x] for x in B]
    n = len(Am[0]) if Am else 0
    if n != len(Bm):
        shape = lambda m: f"{len(m)}x{len(m[0]) if m else 0}"
        hint = "; a vector is a column: transpose(v) * A multiplies by a row" if not a2 else ""
        raise SignalError(line, f"* needs the columns of the first to match the rows of the second, got "
                                f"{shape(Am)} and {shape(Bm)}{hint}")
    out = [[sum((Am[i][k] * Bm[k][j] for k in range(n)), 0) for j in range(len(Bm[0]))] for i in range(len(Am))]
    if not b2:
        out = [r[0] for r in out]
    return _from_matrix(out)


def _transpose(name, args, line, ctx=None) -> _SV:
    sv = args[0]
    v = sv.value
    if isinstance(v, dict) and sv.type.endswith("complex") and isinstance(v.get("re"), list):
        t = lambda m: [list(r) for r in zip(*m)] if m and isinstance(m[0], list) else [[e] for e in m]
        if any(isinstance(e, list) for r in v["re"] if isinstance(r, list) for e in r):
            raise SignalError(line, "transpose works on vectors and 2-D arrays")
        out = {"re": t(v["re"]), "im": t(v["im"])}
        return _SV(out, "vector::vector::complex")
    if not isinstance(v, list) or sv.file:
        raise SignalError(line, f"transpose needs a vector or 2-D array, got {_show_type(sv)}")
    if v and isinstance(v[0], list):
        if any(isinstance(e, list) for r in v for e in r):
            raise SignalError(line, "transpose works on vectors and 2-D arrays")
        out = [list(r) for r in zip(*v)]
    else:
        out = [[e] for e in v]
    return _SV(out, infer_type(out) or "vector")


def _bits_not(a: _SV, line: int) -> _SV:
    v = a.value
    if not isinstance(v, Bits):
        raise SignalError(line, f"~ needs bits, got {_show_type(a)}")
    return _SV(Bits(~int(v) & ((1 << v.width) - 1), v.width), _bits_type(v.width))


def _bits_op(op: str, a: _SV, b: _SV, line: int) -> _SV:
    x, y = a.value, b.value
    if not isinstance(x, Bits):
        raise SignalError(line, f"{op} needs bits, got {_show_type(a)}")
    w = x.width
    if op in ("<<", ">>"):
        if type(y) is not int or y < 0:
            raise SignalError(line, f"{op} shifts by a whole number from 0 up, got {_show_value(b) if type(y) is int else _show_type(b)}")
        r = (int(x) << y) & ((1 << w) - 1) if op == "<<" else int(x) >> y
        return _SV(Bits(r, w), _bits_type(w))
    if not isinstance(y, Bits):
        raise SignalError(line, f"{op} needs bits, got {_show_type(b)}")
    if y.width != w:
        raise SignalError(line, f"{op} needs bits of the same width, got bits::{w} and bits::{y.width}")
    r = int(x) & int(y) if op == "&" else int(x) | int(y) if op == "|" else int(x) ^ int(y)
    return _SV(Bits(r, w), _bits_type(w))


_MORE = {"saturate": _saturate, "excess": _excess, "quantize": _quantize, "popcount": _popcount,
         "min": _extreme, "max": _extreme, "argmin": _extreme, "argmax": _extreme,
         "sum": _reduce, "prod": _reduce, "mean": _reduce, "std": _reduce, "rms": _reduce, "norm": _reduce,
         "sort": _sort, "length": _length, "size": _length, "dot": _dot, "cross": _dot, "transpose": _transpose}

_MATH = {"Re": 1, "Im": 1, "abs": 1, "arg": 1, "conj": 1, "power": 2, "root": 2}     # built-in maths, and their argument counts
_MATH.update({n: (2 if n in ("saturate", "excess", "quantize", "dot", "cross") else 1) for n in _MORE})
_NO_STREAMS = "a function can't read streams: sample or freeze the stream first and pass in the value"
_FNARGS = {"map": 1, "warp": 2}      # built-in calls and how many function arguments they start with
_BUILTINS = ("map", "warp", "unity", "Re", "Im", "im", "input", "abs", "arg", "conj", "power", "root", "sample", "freeze") \
    + tuple(_MORE)


def _fn_name(node) -> str:
    """The function a map/warp argument names; "unity" leaves values as they are."""
    return node[1]


def _apply(name: str, args: list, line: int, ctx) -> _SV:
    return args[0] if name == "unity" else _call_function(name, args, line, ctx)


def _scalar(r: _SV, who: str, line: int) -> _SV:
    if _is_vec(r) or r.file or r.type.startswith(("series", "vector", "bus")):
        raise SignalError(line, f"{who} must give one value for each, got {_show_type(r)}")
    return r


def _map(name: str, vecs: list, line: int, ctx) -> _SV:
    lists = []
    for v in vecs:
        if isinstance(v.value, dict) and _series_parts(v.value):
            raise SignalError(line, "map works on vectors; for a series use warp(unity, f, series) to change its values")
        if not _is_vec(v):
            raise SignalError(line, f"map needs vectors, got {_show_type(v)}")
        lists.append(_items(v))
    if len({len(x) for x in lists}) > 1:
        raise SignalError(line, "map needs vectors of the same length, got " + " and ".join(str(len(x)) for x in lists) + " values")
    out = [_scalar(_apply(name, [_wrap(x) for x in row], line, ctx), f"the function given to map ({name})", line)
           for row in zip(*lists)]
    return _gather(out, "map", line)


def _warp(fx: str, fy: str, s: _SV, line: int, ctx) -> _SV:
    if isinstance(s.value, dict) and _series_parts(s.value):
        parts = _series_parts(s.value)
    elif _is_vec(s) and fx == "unity":      # a plain vector has no axis: warp is map of the values
        return s if fy == "unity" else _map(fy, [s], line, ctx)
    elif _is_vec(s):
        raise SignalError(line, "a vector has no axis: give unity as the first argument of warp, or use map")
    else:
        raise SignalError(line, f"warp needs a series, got {_show_type(s)}")
    if parts[1] != "float":
        raise SignalError(line, "warp works on real series, not complex ones")
    if fx == fy == "unity":
        return s
    v = s.value
    def number(r, what):
        r = _scalar(r, f"the {what} function given to warp", line)
        if r.type not in ("int", "float") and r.value is not None or isinstance(r.value, bool):
            raise SignalError(line, f"the {what} function given to warp must give numbers, got {_show_type(r)}")
        if r.value is not None and not math.isfinite(r.value):
            raise SignalError(line, f"the {what} function given to warp gave a value that is not finite")
        return r.value
    new = dict(v)
    if fx != "unity":
        xs = [number(_apply(fx, [_SV(float(x), "float")], line, ctx), "axis") for x in _series_x(v)]
        if any(x is None for x in xs):
            raise SignalError(line, "the axis function given to warp must give numbers")
        for i in range(1, len(xs)):
            if xs[i] == xs[i - 1] or (i > 1 and (xs[i] > xs[i - 1]) != (xs[1] > xs[0])):
                raise SignalError(line, f"the axis must keep rising or keep falling after warp, but value {i + 1} "
                                        f"({xs[i]:g}) doesn't follow value {i} ({xs[i - 1]:g})")
        for k in ("start", "step"):
            new.pop(k, None)
        new["x"] = [float(x) for x in xs]
    if fy != "unity":
        ys = [number(_apply(fy, [_SV(None if y is None else float(y), "float")], line, ctx), "value") for y in v["values"]]
        new["values"] = [None if y is None else float(y) for y in ys]
    return _SV(new, f"series::{parts[0]}::float", f"line {line}: warp()")


def _arith(op, a: _SV, b: _SV, line: int) -> _SV:
    if a.type == "complex" or b.type == "complex":
        return _complex_arith(op, a, b, line)
    if isinstance(a.value, Hex) and isinstance(b.value, Hex):
        return _hex_arith(op, a, b, line)
    a, b = _unhex(a), _unhex(b)
    for x in (a, b):
        if x.file or not (x.value is None or _is_num(x.value)):
            raise SignalError(line, f"{op} needs numbers, got {_show_type(x)}")
    x, y = a.value, b.value
    if op == "/" and y == 0:
        raise SignalError(line, "division by zero")
    if x is None or y is None:
        return _SV(None, "float")
    if op == "+":
        r = x + y
    elif op == "-":
        r = x - y
    elif op == "*":
        r = x * y
    elif type(x) is int and type(y) is int:
        q = abs(x) // abs(y)          # as in Fortran: the fraction is dropped
        r = q if (x >= 0) == (y >= 0) else -q
    else:
        r = x / y
    return _SV(r, "int" if type(r) is int else "float")


class _Return(Exception):
    def __init__(self, value):
        super().__init__("return")
        self.value = value


class _Blocked:
    """A variable a function can't read, such as one bound to a live signal."""


class _Scope(dict):
    """A subprocess's own variables, with the caller's globals behind them to read."""

    def __init__(self, parent):
        super().__init__()
        self.parent = parent

    def __missing__(self, key):
        return self.parent[key]

    def __contains__(self, key):
        return dict.__contains__(self, key) or key in self.parent


class _Live:
    """A let bound to something that changes while a sequence waits: worked out again at every use."""

    def __init__(self, expr, line, words):
        self.expr, self.line, self.words = expr, line, words


def _is_live(e, ctx) -> bool:
    if e[0] in ("ref", "column", "state"):
        return True
    if e[0] == "sample":                     # a sample is taken once, when its line runs
        return False
    if e[0] == "name":
        return isinstance(ctx["vars"].get(e[1], (None,))[0], _Live)
    if e[0] == "mux":
        return any(_is_live(a, ctx) for _, a in e[1])
    return any(_is_live(sub, ctx) for sub in e[1:] if isinstance(sub, tuple))


def _param_value(text, source) -> _SV:
    t = str(text).strip()
    if re.fullmatch(r"0[bB][01]{1,64}", t):
        return _SV(Bits(int(t[2:], 2), len(t) - 2), _bits_type(len(t) - 2), source)
    m = re.fullmatch(r"\{\s*([^,{}]+?)\s*,\s*([^,{}]+?)\s*\}", t)
    if m:
        try:
            lo, hi = json.loads(m.group(1)), json.loads(m.group(2))
        except ValueError:
            lo = hi = None
        if _is_num(lo) and _is_num(hi) and lo <= hi:
            v = {"lo": lo, "hi": hi}
            return _SV(v, _range_type(v), source)
    try:
        v = json.loads(text)
    except ValueError:
        v = text
    v = _denan(v)
    return _SV(v, infer_type(v), source)


def _domain_of(header: str) -> str:
    # "time_s" -> time: the name before a trailing _unit, made safe to use as a series domain.
    d = re.sub(r"_[A-Za-z0-9%]+\Z", "", header.strip()) if "_" in header else header.strip()
    d = re.sub(r"[^A-Za-z0-9_]", "_", d)
    if not d or d[0].isdigit():
        d = "x" + d
    return d + "_" if d.lower() in _TYPE_WORDS else d.lower()


def _num_or_none(text):
    try:
        v = float(text)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _read_column(path: str, name: str, line: int) -> dict:
    """One column of a CSV file as a series against its first column. The file may still be written:
    a half-written last line is ignored, and anything before the header (such as echo lines) is skipped."""
    shown = os.path.basename(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        raise _Pending(line, f"{shown} can't be read yet")
    rows = raw.split("\n")
    if not raw.endswith("\n"):
        rows = rows[:-1]
    header, x, values = None, [], []
    for row in rows:
        s = row.strip()
        if not s or s.startswith("#"):
            continue
        cells = [c.strip() for c in s.split(",")]
        if header is None:
            if name in cells:
                header, col = cells, cells.index(name)
            continue
        if len(cells) < len(header):
            continue
        xv = _num_or_none(cells[0])
        if xv is not None:
            x.append(xv)
            values.append(_num_or_none(cells[col]))
    if header is None:
        raise _Pending(line, f"{shown} has no column {name} yet")
    return {"domain": _domain_of(header[0]), "x": x, "values": values}


# Streams: a stream is a Signal still being written, <module>.<port>.sig with type stream::<domain>::<kind>.
# Its value is {"domain", "x", "values", "window", "count", "ended"}: the last `window` samples (values as in a
# series), how many were ever written, and whether its module has ended. The module rewrites the whole file at
# each sample; Signet marks it ended when the module stops, and stamps it if the run went well. A stream's value
# in Signet code is {"stream": [path, ...]}, read again at each use: more than one path after a merge.
STREAM_WINDOW = 1048


def _stream_paths(v):
    if isinstance(v, dict) and len(v) == 1 and isinstance(v.get("stream"), list) and v["stream"] \
            and all(isinstance(p, str) for p in v["stream"]):
        return v["stream"]
    return None


def stream_sig(path: str):
    """A stream's .sig as a dict, or None if it isn't one: stamped, or unstamped while its module still writes it."""
    try:
        if os.path.getsize(path) > _SIG_MAX:
            return None
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not (isinstance(obj, dict) and str(obj.get("type", "")).split("::")[0] == "stream" and isinstance(obj.get("value"), dict)):
        return None
    if obj.get("hash"):
        return obj if obj["hash"] == _sig_hash(obj) else None
    return None if obj["value"].get("ended") else obj


def _stream_type(paths, line=None) -> str:
    """stream::<domain>::<kind> of one stream or a merge of them; just "stream" before any sample."""
    kinds = {o["type"] for o in map(stream_sig, paths) if o and o["type"] != "stream"}
    if len(kinds) > 1:
        raise SignalError(line, "merged streams differ: " + ", ".join(sorted(kinds)))
    return kinds.pop() if kinds else "stream"


def _stream_rows(obj):
    """[(t, value)] of a stream's window; a complex value is {"re", "im"}."""
    v = obj["value"]
    x, vals = v.get("x") or [], v.get("values") or []
    if isinstance(vals, dict):
        vals = [{"re": r, "im": i} for r, i in zip(vals.get("re") or [], vals.get("im") or [])]
    return [(t, y) for t, y in zip(x, vals) if _is_num(t)]


def _stream_arg(fn: str, arg, line: int, ctx):
    s = _eval(arg, line, ctx)
    paths = _stream_paths(s.value)
    if paths is None:
        raise SignalError(line, f"{fn} needs a stream, got {_show_type(s)}")
    kind = _stream_type(paths, line)
    rows = sorted((r for o in map(stream_sig, paths) if o for r in _stream_rows(o)), key=lambda r: r[0])
    return kind, rows


def _sample(mode: str, arg, line: int, ctx) -> _SV:
    """sample(s): the latest [t, value] as a tuple; $sample(s) the value; @sample(s) its t."""
    kind, rows = _stream_arg(f"{mode}sample", arg, line, ctx)
    if not rows:
        raise _Pending(line, f"{arg[1] if arg[0] in ('name', 'ref') else 'the stream'} has no samples yet")
    t, v = rows[-1]
    if kind.endswith("::complex"):
        vv = _SV(v if isinstance(v, dict) else {"re": None, "im": None}, "complex")
    else:
        vv = _SV(float(v) if _is_num(v) else None, "float")
    src = f"line {line}: {mode}sample"
    if mode == "$":
        return _SV(vv.value, vv.type, src)
    if mode == "@":
        return _SV(t, "int" if type(t) is int else "float", src)
    return _SV([t, vv.value], "tuple", src)


def _freeze(arg, line: int, ctx) -> _SV:
    """freeze(s): the stream's window as it is now, a series; the stream goes on."""
    kind, rows = _stream_arg("freeze", arg, line, ctx)
    if kind == "stream":
        raise _Pending(line, f"{arg[1] if arg[0] in ('name', 'ref') else 'the stream'} has no samples yet")
    _, domain, k = kind.split("::")
    x = [r[0] for r in rows]
    if k == "complex":
        vals = {"re": [r[1].get("re") if isinstance(r[1], dict) else None for r in rows],
                "im": [r[1].get("im") if isinstance(r[1], dict) else None for r in rows]}
    else:
        vals = [float(r[1]) if _is_num(r[1]) else None for r in rows]
    return _SV({"domain": domain, "x": x, "values": vals}, f"series::{domain}::{k}", f"line {line}: freeze")


_END_LOCK = threading.Lock()


def end_stream(path: str) -> None:
    """Mark an unstamped stream ended: its module has stopped. A stream with no samples is removed."""
    with _END_LOCK:
        _end_stream(path)


def _end_stream(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(obj, dict) or obj.get("hash") or not isinstance(obj.get("value"), dict) \
            or str(obj.get("type", "")).split("::")[0] != "stream":
        return
    if "domain" not in obj["value"]:
        os.remove(path)
        return
    obj["value"]["ended"] = True
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    os.replace(path + ".tmp", path)


def _note(ctx, sv: _SV) -> _SV:
    """Adds what a variable was worked out from to the expression being traced."""
    if sv.trace and ctx.get("touched") is not None:
        ctx["touched"].update(sv.trace)
    return sv


def _traced(e, line: int, ctx) -> _SV:
    """_eval, keeping on the result the Signals, files and parameters it was worked out from (for lineage)."""
    outer, ctx["touched"] = ctx.get("touched"), {}
    try:
        sv = _eval(e, line, ctx)
        got = ctx["touched"]
    finally:
        ctx["touched"] = outer
    if outer is not None:
        outer.update(got)
    if got:
        sv = dataclasses.replace(sv, trace={**(sv.trace or {}), **got})
    return sv


_FILE_HASHES = {}


def _file_hash(path: str) -> str:
    """"sha256:<hex>" of a file's bytes, kept while the file is unchanged; "" if it can't be read."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
        if key not in _FILE_HASHES:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            _FILE_HASHES[key] = "sha256:" + h.hexdigest()
        return _FILE_HASHES[key]
    except OSError:
        return ""


def _cmp(op, x, y) -> bool:
    return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y, "==": x == y, "/=": x != y}[op]


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _bind(params, args, name, line, ctx) -> dict:
    if len(args) != len(params):
        raise SignalError(line, f"{name} takes {len(params)} value{'s' * (len(params) != 1)}, got {len(args)}")
    out = {}
    for (words, p), sv in zip(params, args):
        try:
            out[p] = (_typed(words, sv, line) if words else sv, words)
        except SignalError as e:
            raise SignalError(e.line, f"{e.msg} (argument {p} of {name})")
    return out


def _define_fn(nd, ctx) -> None:
    """Make a function or subprocess known from here on. A function keeps a read-only snapshot of the
    variables declared before it; a subprocess reads the caller's globals when it is called."""
    snap = None
    if nd[0] == "function":
        snap = {k: ((_Blocked(), w) if isinstance(v, _Live) else (v, w)) for k, (v, w) in ctx["vars"].items()}
    funcs = ctx.setdefault("funcs", {})
    funcs[nd[2]] = {"kind": nd[0], "name": nd[2], "params": nd[3], "body": nd[6], "snap": snap, "funcs": dict(funcs)}


def _call_function(name, args, line, ctx) -> _SV:
    fn = ctx.get("funcs", {}).get(name)
    if fn is None or fn["kind"] != "function":
        raise SignalError(line, f"unknown function {name}")
    frame = dict(fn["snap"])
    frame.update(_bind(fn["params"], args, name, line, ctx))
    ex = _Exec(ctx["host"], None)
    ex.ctx.update({"vars": frame, "root": frame, "funcs": fn["funcs"], "first": ctx.get("first", 0)})
    try:
        for _ in ex.block(fn["body"]):
            pass
    except _Return as r:
        out = r.value
    except SignalError as e:
        raise SignalError(e.line, f"{e.msg} (in {name}, called at line {line})")
    else:
        raise SignalError(line, f"{name} ended without return")
    if not out.source:
        out.source = f"line {line}: {name}()"
    return out


def _point_str(p, line: int, ctx) -> str:
    """A point as text: a number or module name as written, a variable's value, or an expression's."""
    if isinstance(p, tuple):
        v = _eval(p[1], line, ctx)
    elif _IDENT.match(p) and p in ctx["vars"]:
        v = _eval(("name", p), line, ctx)
    else:
        return p
    if type(v.value) is not int:
        raise SignalError(line, f"a point needs a whole number, got {_show_type(v)}")
    return str(v.value)


def _ref_text(text: str, line: int, ctx) -> str:
    head, colon, rest = text.partition(":")
    return _point_str(head, line, ctx) + colon + rest if colon and _IDENT.match(head) and head in ctx["vars"] else text


def _eval(e, line: int, ctx) -> _SV:
    kind = e[0]
    host = ctx["host"]
    if kind == "num":
        return _SV(e[1], "int" if type(e[1]) is int else "float")
    if kind == "imag":
        return _SV({"re": 0.0, "im": e[1]}, "complex")
    if kind == "bool":
        return _SV(e[1], "bool")
    if kind == "str":
        return _SV(e[1], "text")
    if kind == "hexraw":
        raw = e[1]
        if raw in ctx["vars"]:          # a variable named like hex digits: convert it
            return _typed(["hex"], _eval(("name", raw), line, ctx), line)
        n = int(raw[2:] if raw[:2] in ("0x", "0X") else raw, 16)
        if n > _HEX_MAX:
            raise SignalError(line, f"hex::{raw} is over hex::FFFFFFFF")
        return _SV(Hex(n), "hex")
    if kind == "call" and e[1] in _MORE:
        return _MORE[e[1]](e[1], [_eval(a, line, ctx) for a in e[2:]], line, ctx)
    if kind == "bits":
        return _SV(Bits(int(e[1], 2), len(e[1])), _bits_type(len(e[1])))
    if kind == "range":
        return _make_range(_eval(e[1], line, ctx), _eval(e[2], line, ctx), line)
    if kind == "bnot":
        return _bits_not(_eval(e[1], line, ctx), line)
    if kind == "call" and e[1] in _MATH:
        return _maths(e[1], [_eval(a, line, ctx) for a in e[2:]], line)
    if kind == "call" and e[1] == "map":
        return _map(_fn_name(e[2]), [_eval(a, line, ctx) for a in e[3:]], line, ctx)
    if kind == "call" and e[1] == "freeze":
        return _freeze(e[2], line, ctx)
    if kind == "call" and e[1] == "warp":
        return _warp(_fn_name(e[2]), _fn_name(e[3]), _eval(e[4], line, ctx), line, ctx)
    if kind == "call":
        return _call_function(e[1], [_eval(a, line, ctx) for a in e[2:]], line, ctx)
    if kind == "vec":
        items = [_eval(a, line, ctx) for a in e[1:]]
        for it in items:
            if it.file or it.type in ("bus",) or isinstance(it.value, (dict, type(None))) and not it.type.endswith("complex"):
                raise SignalError(line, f"a vector holds numbers, bools, text, hex or complex numbers, got {_show_type(it)}")
        vals = [it.value for it in items]
        if items and all(it.type == "complex" for it in items):
            return _SV({"re": [v["re"] for v in vals], "im": [v["im"] for v in vals]}, "vector::complex", "vector")
        if items and len({it.type for it in items}) == 1 and items[0].type.startswith("vector::") and items[0].type.endswith("complex") \
                and all(isinstance(v, dict) for v in vals):
            packed = {"re": [v["re"] for v in vals], "im": [v["im"] for v in vals]}
            if _complex_depth(packed) is None:
                raise SignalError(line, "the columns of an array must all be the same length")
            return _SV(packed, "vector::" + items[0].type, "vector")
        t = infer_type(vals)
        if items and t in ("vector", ""):
            raise SignalError(line, "a vector's values must all be of one type, got " + ", ".join(sorted({_show_type(i) for i in items})))
        if t.endswith("float") and t.count("::") > 1:
            vals = _floats(vals)
        return _SV(vals, t or "vector", "vector")
    if kind == "pref":
        point = _point_str(("pexpr", e[1]), line, ctx)
        if e[2].startswith("$"):
            return _eval(("pparam", point, e[2][1:]), line, ctx)
        return _eval(("ref", f"{point}:{e[2]}"), line, ctx)
    if kind == "ref":
        text = _ref_text(e[1], line, ctx)
        if text in ctx.get("merged", {}):
            return _note(ctx, ctx["merged"][text])
        try:
            sv = host.resolve(text)
        except LookupError as err:
            raise _Pending(line, err.args[0] if err.args else f"nothing live matches {text}")
        if ctx.get("touched") is not None:
            key = getattr(sv, "path", "") or text
            ctx["touched"][key] = _file_hash(key) if sv.file else (getattr(sv, "stamp", "") or None)
        return sv
    if kind == "param":
        try:
            text = host.param(e[1])
        except LookupError as err:
            raise SignalError(line, err.args[0] if err.args else f"no parameter {e[1]}")
        if ctx.get("touched") is not None:
            ctx["touched"]["$" + e[1]] = str(text)
        return _param_value(text, "$" + e[1])
    if kind == "pparam":
        if not hasattr(host, "point_param"):
            raise SignalError(line, f"{e[1]}:${e[2]} needs a host with points")
        point = _point_str(e[1], line, ctx)
        try:
            text = host.point_param(point, e[2])
        except LookupError as err:
            raise SignalError(line, err.args[0] if err.args else f"point {point} has no parameter {e[2]}")
        if ctx.get("touched") is not None:
            ctx["touched"][f"{point}:${e[2]}"] = str(text)
        return _param_value(text, f"{point}:${e[2]}")
    if kind == "autorun":
        if not hasattr(host, "autorun"):
            raise SignalError(line, "AUTORUN needs a sequence")
        return _SV(bool(host.autorun()), "bool")
    if kind == "state":
        if not hasattr(host, "state"):
            raise SignalError(line, f"{e[1]}(...) needs a host that tracks runs")
        return _SV(host.state(_point_str(e[2], line, ctx)) == e[1], "bool")
    if kind == "column":
        f = _eval(e[1], line, ctx)
        if not f.file:
            raise SignalError(line, f"column needs a file, got {_show_type(f)}")
        s = _read_column(f.value, e[2], line)
        return _SV(s, f"series::{s['domain']}::float", f"column({f.source}, {e[2]})")
    if kind == "index":
        return _index(e, line, ctx)
    if kind == "sample":
        return _sample(e[1], e[2], line, ctx)
    if kind == "name":
        sv, words = ctx["vars"][e[1]]
        if isinstance(sv, _Blocked):
            raise SignalError(line, f"{e[1]} is bound to a live signal, so a function can't read it: pass it in as an argument")
        if isinstance(sv, _Live):
            sv = _eval(sv.expr, sv.line, ctx)
            if words:
                sv = _typed(words, sv, line)
        return _note(ctx, sv)
    if kind == "typed":
        return _typed(e[1], _eval(e[2], line, ctx), line)
    if kind == "input":
        if not hasattr(host, "ask"):
            raise SignalError(line, "input needs a person to ask, and this host has none")
        prompt = _eval(e[2], line, ctx).value if e[2] else "Value?"
        if not isinstance(prompt, str):
            raise SignalError(line, "the prompt of input must be text")
        try:
            text = host.ask(prompt, "::".join(e[1]))
        except LookupError as err:
            raise SignalError(line, err.args[0] if err.args else "no answer was given")
        sv = _SV(text, "text", "input") if e[1] == ["text"] else _param_value(text, "input")
        return _typed(e[1], sv, line) if e[1] else sv
    if kind == "neg":
        a = _eval(e[1], line, ctx)
        return _arith("-", _SV(0, "int"), _unhex(a), line)
    if kind == "not":
        a = _eval(e[1], line, ctx)
        if type(a.value) is not bool:
            raise SignalError(line, f"not needs a bool, got {_show_type(a)}")
        return _SV(not a.value, "bool")
    if kind == "mux":
        out = {}
        for fld, a in e[1]:
            if fld in out:
                raise SignalError(line, f"multiplex has two fields named {fld}")
            sv = _eval(a, line, ctx)
            out[fld] = {"value": sv.value, "type": sv.type or infer_type(sv.value)}
        return _SV(out, "bus")
    if kind == "demux":
        bus = _eval(("name", e[1]), line, ctx)
        if bus.type != "bus" or not isinstance(bus.value, dict):
            raise SignalError(line, f"{e[1]} isn't a bundle")
        if e[2] not in bus.value:
            raise SignalError(line, f"{e[1]} has no field {e[2]}")
        f = bus.value[e[2]]
        return _SV(f.get("value"), str(f.get("type") or ""), f"{e[1]}.{e[2]}")
    op, a, b = e[1], _eval(e[2], line, ctx), _eval(e[3], line, ctx)
    if op == "*" and _is_array(a) and _is_array(b):
        return _matmul(a, b, line)
    if op in ("+", "-", "*", "/"):
        if _is_vec(a) or _is_vec(b):
            both = _is_vec(a) and _is_vec(b)
            if op in ("+", "-") and not both:
                raise SignalError(line, f"{op} needs two vectors; use .{op} to apply a number to every value")
            if op == "*" and both:
                return _matmul(a, b, line)
            if op == "/" and (both or _is_vec(b)):
                raise SignalError(line, "/ divides a vector by a number; use ./ for elementwise division")
            return _broadcast(op, op, a, b, line)
        return _arith(op, a, b, line)
    if op in (".+", ".-", ".*", "./"):
        return _broadcast(op[1], op, a, b, line)
    if op in ("&", "|", "xor", "<<", ">>"):
        return _bits_op(op, a, b, line)
    if op == "in":
        return _in_range(a, b, line)
    if op in ("and", "or"):
        for x in (a, b):
            if type(x.value) is not bool:
                raise SignalError(line, f"{op} needs bools, got {_show_type(x)}")
        return _SV(a.value and b.value if op == "and" else a.value or b.value, "bool")
    x, y = a.value, b.value
    if isinstance(x, Bits) or isinstance(y, Bits):
        if op not in ("==", "/=") or not (isinstance(x, Bits) and isinstance(y, Bits)):
            raise SignalError(line, f"{op} can't compare {_show_type(a)} with {_show_type(b)}" if op in ("==", "/=")
                              else f"{op} can't order bits; compare them with == or /=")
        return _SV((int(x) == int(y) and x.width == y.width) == (op == "=="), "bool")
    if isinstance(x, Hex) != isinstance(y, Hex):
        a, b = _unhex(a), _unhex(b)
        x, y = a.value, b.value
    if isinstance(x, Hex):
        return _SV(_cmp(op, int(x), int(y)), "bool")
    if isinstance(x, str) and isinstance(y, str) and not (a.file or b.file):
        if op not in ("==", "/="):
            raise SignalError(line, f"{op} needs numbers, got text")
        return _SV((x == y) == (op == "=="), "bool")
    if op in ("==", "/=") and type(x) is bool and type(y) is bool:
        return _SV((x == y) == (op == "=="), "bool")
    if a.type == "complex" and b.type in ("complex", "int", "float") or b.type == "complex" and a.type in ("int", "float"):
        if op not in ("==", "/="):
            raise SignalError(line, f"{op} can't order complex numbers; compare Re(z) or Im(z)")
        u, w = _complex_arith("+", a, _SV(0, "int"), line).value, _complex_arith("+", b, _SV(0, "int"), line).value
        same = None not in (u["re"], u["im"], w["re"], w["im"]) and (u["re"], u["im"]) == (w["re"], w["im"])
        return _SV(same == (op == "=="), "bool")
    for s in (a, b):
        if s.file or not (s.value is None or _is_num(s.value)):
            raise SignalError(line, f"{op} needs numbers, got {_show_type(s)}")
    if x is None or y is None:
        return _SV(op == "/=", "bool")   # NaN equals nothing
    return _SV(_cmp(op, x, y), "bool")


def _position(spec, n: int, label: str, line: int, ctx) -> int:
    """The 0-based place an index names: a number (counted from index(), 0 by default) or end, end-k."""
    if spec[0] == "end":
        back = 0
        if spec[1] is not None:
            off = _eval(spec[1], line, ctx)
            if type(off.value) is not int:
                raise SignalError(line, f"an index needs a whole number, got {_show_type(off)}")
            back = off.value
        i = n - 1 - back
        shown = i
    else:
        iv = _eval(spec, line, ctx)
        if type(iv.value) is not int:
            raise SignalError(line, f"an index needs a whole number, got {_show_type(iv)}")
        i = iv.value - ctx.get("first", 0)
        shown = iv.value
    if not 0 <= i < n:
        raise SignalError(line, f"index {shown} is outside {label} ({n} values)")
    return i


def _index(e, line: int, ctx) -> _SV:
    base = _eval(e[1], line, ctx)
    label = e[1][1] if e[1][0] == "name" else "the value"
    v = base.value
    spec = e[2]
    if isinstance(v, Bits):                    # b[0] is the lowest bit
        if spec[0] == "slice":
            lo, hi = (_position(s, v.width, label, line, ctx) for s in spec[1:])
            if lo > hi:
                raise SignalError(line, f"a slice runs from low to high, got [{lo}:{hi}]")
            w = hi - lo + 1
            return _SV(Bits((int(v) >> lo) & ((1 << w) - 1), w), _bits_type(w), f"{base.source or label}[{lo}:{hi}]")
        i = _position(spec, v.width, label, line, ctx)
        return _SV(bool(int(v) >> i & 1), "bool", f"{base.source or label}[{i}]")
    if _is_range(v) and not base.file:
        i = _position(spec, 2, label, line, ctx) if spec[0] != "slice" else None
        if i is None:
            raise SignalError(line, "a range can't be sliced; use r[0] and r[1]")
        x = v["lo" if i == 0 else "hi"]
        return _SV(x, infer_type(x), f"{base.source or label}[{i}]")
    if (isinstance(v, list) and v and isinstance(v[0], list)) or spec[0] == "multi" \
            or (isinstance(v, dict) and isinstance(v.get("re"), list) and v["re"] and isinstance(v["re"][0], list)):
        return _index_array(base, spec, label, line, ctx)
    if spec[0] == "all" and (isinstance(v, list) or (isinstance(v, dict) and isinstance(v.get("re"), list))):
        return base
    if isinstance(v, dict) and _series_parts(v):
        if spec[0] == "slice":
            raise SignalError(line, "a series can't be sliced")
        vals = v["values"]
        n = len(vals["re"]) if isinstance(vals, dict) else len(vals)
    elif isinstance(v, list):
        vals, n = v, len(v)
    elif isinstance(v, dict) and base.type.endswith("complex") and isinstance(v.get("re"), list):
        vals, n = v, len(v["re"])
    else:
        raise SignalError(line, f"can't index {_show_type(base)}")
    if n == 0:
        raise _Pending(line, f"{label} has no values yet")
    if spec[0] == "slice":                     # v[1:3]: both ends included
        lo, hi = (_position(s, n, label, line, ctx) for s in spec[1:])
        if lo > hi:
            raise SignalError(line, f"a slice runs from low to high, got [{lo}:{hi}]")
        src = f"{base.source or label}[{lo}:{hi}]"
        if isinstance(vals, dict):
            return _SV({"re": vals["re"][lo:hi + 1], "im": vals["im"][lo:hi + 1]}, base.type, src)
        return _SV(vals[lo:hi + 1], base.type or infer_type(vals), src)
    i = _position(spec, n, label, line, ctx)
    src = f"{base.source or label}[{i}]"
    if isinstance(vals, dict):
        t = "complex" if base.type.startswith("series") else base.type[len("vector::"):]
        return _SV({"re": vals["re"][i], "im": vals["im"][i]}, t, src)
    el = vals[i]
    t = base.type[len("vector::"):] if base.type.startswith("vector::") else infer_type(el)
    if isinstance(v, dict):
        t = "float"
    return _SV(el, t, src)


def _index_array(base: _SV, spec, label: str, line: int, ctx) -> _SV:
    """A[k], A[a:b] and A[:] count down the columns; A[i, j] is row i, column j, and A[:, j] a column."""
    if isinstance(base.value, dict) and _series_parts(base.value) or not (isinstance(base.value, list)
                                                                         or isinstance(base.value, dict) and isinstance(base.value.get("re"), list)):
        raise SignalError(line, f"{_show_type(base)} takes one index")
    n, cplx = _nest(base)
    leaf = re.sub(r"^(vector::)+", "", base.type or "")
    dims = _dims(n)
    src = f"{base.source or label}[...]"
    if spec[0] != "multi":
        flat = _flat(n)
        if not flat:
            raise _Pending(line, f"{label} has no values yet")
        if spec[0] == "all":
            return _unnest(flat, cplx, leaf, src)
        if spec[0] == "slice":
            lo, hi = (_position(x, len(flat), label, line, ctx) for x in spec[1:])
            if lo > hi:
                raise SignalError(line, f"a slice runs from low to high, got [{lo}:{hi}]")
            return _unnest(flat[lo:hi + 1], cplx, leaf, src)
        return _unnest(flat[_position(spec, len(flat), label, line, ctx)], cplx, leaf, src)
    specs = spec[1:]
    if len(specs) != len(dims):
        raise SignalError(line, f"{label} has {len(dims)} dimension{'s' * (len(dims) != 1)}, "
                                f"so it takes 1 or {len(dims)} indexes, got {len(specs)}")
    def pick(node, rest):              # the outer list is the last dimension
        if not rest:
            return node
        sp, more = rest[-1], rest[:-1]
        if sp[0] == "all":
            return [pick(e, more) for e in node]
        if sp[0] == "slice":
            lo, hi = (_position(x, len(node), label, line, ctx) for x in sp[1:])
            if lo > hi:
                raise SignalError(line, f"a slice runs from low to high, got [{lo}:{hi}]")
            return [pick(e, more) for e in node[lo:hi + 1]]
        return pick(node[_position(sp, len(node), label, line, ctx)], more)
    return _unnest(pick(n, list(specs)), cplx, leaf, src)


def _elements(sv: _SV, line: int):
    """(element type, [elements]) of a vector, or (type, [value]) of a scalar, or None if neither."""
    t, v = sv.type or infer_type(sv.value), sv.value
    if sv.file or t in ("bool", "bus", "") or t.startswith("series"):
        return None
    if t == "vector::complex":
        return "complex", [{"re": r, "im": i} for r, i in zip(v["re"], v["im"])], True
    if t.startswith("vector"):
        return (t[len("vector::"):] or "", list(v), True) if isinstance(v, list) else None
    if t in ("int", "float", "hex", "text", "complex"):
        return t, [v], False
    return None


def _series_x(v: dict) -> list:
    if "x" in v:
        return list(v["x"])
    n = len(v["values"]["re"]) if isinstance(v["values"], dict) else len(v["values"])
    return [v["start"] + v["step"] * i for i in range(n)]


def _merge2(a: _SV, b: _SV, line: int) -> _SV:
    ta, tb = a.type or infer_type(a.value), b.type or infer_type(b.value)
    bad = SignalError(line, f"can't merge {ta or 'this'} with {tb or 'this'}")
    if ta.startswith("stream") or tb.startswith("stream"):
        pa, pb = _stream_paths(a.value), _stream_paths(b.value)
        if pa is None or pb is None:
            raise bad
        paths = pa + [p for p in pb if p not in pa]
        return _SV({"stream": paths}, _stream_type(paths, line), "route merge")
    if ta.startswith("series") or tb.startswith("series"):
        if ta != tb or not (_series_parts(a.value) and _series_parts(b.value)):
            raise bad
        va, vb = a.value, b.value
        if isinstance(va["values"], dict):
            vals = {"re": va["values"]["re"] + vb["values"]["re"], "im": va["values"]["im"] + vb["values"]["im"]}
        else:
            vals = list(va["values"]) + list(vb["values"])
        return _SV({"domain": va["domain"], "x": _series_x(va) + _series_x(vb), "values": vals}, ta, "route merge")
    ea, eb = _elements(a, line), _elements(b, line)
    if ea is None or eb is None or ea[0] != eb[0] or not ea[0]:
        raise bad
    et, items = ea[0], ea[1] + eb[1]
    if et == "complex":
        return _SV({"re": [c["re"] for c in items], "im": [c["im"] for c in items]}, "vector::complex", "route merge")
    if et.startswith("series"):
        raise bad
    return _SV(items, "vector::" + et, "route merge")


def _merge(first: _SV, rest: list, line: int) -> _SV:
    out = first
    for sv in rest:
        out = _merge2(out, sv, line)
    return out


def _expr_sources(e, names: dict, seen=()) -> tuple:
    """(refs, constants, buses) an expression draws on, following names to what they were assigned:
    refs are ref texts, constants are "$param" or literal texts, buses are [(field, refs)] for multiplex."""
    refs, consts, buses = [], [], []
    kind = e[0]
    if kind == "ref":
        refs.append(e[1])
    elif kind == "param":
        consts.append("$" + e[1])
    elif kind == "pparam":
        consts.append(f"{e[1]}:${e[2]}")
    elif kind == "str":
        consts.append(json.dumps(e[1], ensure_ascii=False))
    elif kind == "vec":
        for sub in e[1:]:
            r, c, b = _expr_sources(sub, names, seen)
            refs += r; consts += c; buses += b
    elif kind in ("num", "bool"):
        consts.append(str(e[1]).lower() if kind == "bool" else str(e[1]))
    elif kind == "bits":
        consts.append("0b" + e[1])
    elif kind == "name" and e[1] not in seen:
        for sub in names.get(e[1], []):
            r, c, b = _expr_sources(sub, names, seen + (e[1],))
            refs += r; consts += c; buses += b
    elif kind == "mux":
        fields = []
        for fld, a in e[1]:
            r, _, _ = _expr_sources(a, names, seen)
            fields.append((fld, r))
        buses.append(fields)
    elif kind == "demux" and e[1] not in seen:
        for sub in names.get(e[1], []):
            for fields in _expr_sources(sub, names, seen + (e[1],))[2]:
                refs += [r for fld, rs in fields if fld == e[2] for r in rs]
    else:
        for sub in e[1:]:
            if isinstance(sub, tuple):
                r, c, b = _expr_sources(sub, names, seen)
                refs += r; consts += c; buses += b
    return refs, consts, buses

def source_value(path: str, label: str, ref: str) -> _SV:
    """A host's answer for a reference: a stamped .sig's value, or any other file as its path."""
    if not path.endswith(".sig"):
        return _SV(path, os.path.splitext(path)[1][1:].lower(), label, file=True, path=path)
    live = stream_sig(path)
    if live is not None:
        return _SV({"stream": [path]}, _stream_type([path]), label, path=path, stamp=live.get("hash", ""))
    try:
        obj = read_sig(path)
    except ValueError as e:
        raise LookupError(f"{ref} {e}")
    v = obj["value"]
    if obj.get("type") == "hex" and type(v) is int and 0 <= v <= _HEX_MAX:
        v = Hex(v)
    v = _bits_from(str(obj.get("type") or ""), v)
    return _SV(v, str(obj.get("type") or ""), label, path=path, stamp=obj["hash"])


def _cshow(re_, im_) -> str:
    r = "nan" if re_ is None else repr(float(re_))
    if im_ is None:
        return f"{r} + nani"
    im_ = float(im_)
    return f"{r} {'-' if im_ < 0 or (im_ == 0 and math.copysign(1, im_) < 0) else '+'} {abs(im_)!r}i"


def _show_value(sv: _SV) -> str:
    if sv.type == "tuple" and isinstance(sv.value, list):
        return "[" + ", ".join(_show_value(_SV(x, infer_type(x))) for x in sv.value) + "]"
    if _stream_paths(sv.value) is not None:
        return "stream " + ", ".join(sv.value["stream"])
    if isinstance(sv.value, Hex):
        return f"hex::{sv.value:X}"
    if isinstance(sv.value, Bits):
        return "0b" + format(int(sv.value), f"0{sv.value.width}b")
    if _is_range(sv.value):
        return "{" + json.dumps(sv.value["lo"]) + ", " + json.dumps(sv.value["hi"]) + "}"
    if sv.type == "complex" and isinstance(sv.value, dict) and not isinstance(sv.value["re"], list):
        return _cshow(sv.value["re"], sv.value["im"])
    if sv.type == "vector::complex" and isinstance(sv.value, dict) and not any(isinstance(x, list) for x in sv.value["re"]):
        return "[" + ", ".join(_cshow(r, i) for r, i in zip(sv.value["re"], sv.value["im"])) + "]"
    if isinstance(sv.value, (list, dict)) and sv.type.startswith("vector::vector::") and sv.type.count("vector::") == 2:
        n, cplx = _nest(sv)
        if n and all(isinstance(c, list) and len(c) == len(n[0]) and not any(isinstance(x, list) for x in c) for c in n):
            one = (lambda z: _cshow(z["re"], z["im"])) if cplx else (lambda x: _show_value(_SV(x, infer_type(x))))
            rows = [", ".join(one(x) for x in r) for r in zip(*n)]
            return "[" + "; ".join(rows) + (";" if len(rows) == 1 else "") + "]"      # written as it is typed in
    text = json.dumps(sv.value, ensure_ascii=False)
    return text if len(text) <= 200 else text[:200] + " ..."


_SEQ_KINDS = {"when", "run", "finish", "delay", "escape"}
_LOOP_MAX = 100_000


def _walk(nodes):
    for nd in nodes:
        yield nd
        if nd[0] == "if":
            yield from _walk(nd[3])
            yield from _walk(nd[4] or [])
        elif nd[0] in ("when", "for", "function", "subprocess"):
            yield from _walk(nd[6])


def is_sequence(code: str) -> bool:
    """Whether the code is a sequence: it has when, run, finish, delay or escape."""
    try:
        return any(nd[0] in _SEQ_KINDS for nd in _walk(parse_signals(code)))
    except ValueError:      # bad code: judged by its first words, so the right run reports the bad lines
        return bool(re.search(r"^\s*(when|run|finish|delay|escape)\b", str(code or ""), re.I | re.M))


def _assigned(nodes) -> set:
    return {nd[2] for nd in _walk(nodes) if nd[0] == "set"}


def _uses(prog, point) -> list:
    """The points a program uses, found without running it: constants, lets of constants and for loops
    over numbers are followed. A point that depends on anything else is an error."""
    out = set() if point is None else {str(point)}
    ctx = {"host": object(), "vars": {}, "lazy": False}
    ctx["root"] = ctx["vars"]
    declared, subs = set(), {}

    def pt(p, line, what):
        if isinstance(p, str) and p in declared and p not in ctx["vars"]:
            p = ("pexpr", ("name", p))
        try:
            return _point_str(p, line, ctx)
        except Exception:
            raise ValueError(f"line {line}: can't tell which point {what} is before the sequence runs; "
                             "use numbers, lets of numbers and for loops over numbers")

    def walk(nodes):
        for nd in nodes:
            kind, line = nd[0], nd[1]
            if kind in ("let", "set"):
                name, expr, words = (nd[3], nd[4], nd[2]) if kind == "let" else (nd[2], nd[3], [])
                declared.add(name)
                try:
                    sv = _eval(expr, line, ctx)
                    ctx["vars"][name] = (_typed(words, sv, line) if words else sv, words)
                except Exception:
                    ctx["vars"].pop(name, None)
            elif kind in ("function", "subprocess"):
                _define_fn(nd, ctx)
                if kind == "subprocess":
                    subs[nd[2]] = ctx["funcs"][nd[2]]
            elif kind == "callstmt":
                fn = subs[nd[2]]
                frame, outer = _Scope(ctx["root"]), ctx["vars"]
                for (words, p), a in zip(fn["params"], nd[3]):
                    declared.add(p)
                    try:
                        sv = _eval(a, line, ctx)
                        frame[p] = (_typed(words, sv, line) if words else sv, words)
                    except Exception:
                        frame[p] = (_Blocked(), [])
                ctx["vars"], saved_funcs = frame, ctx.get("funcs")
                ctx["funcs"] = fn["funcs"]
                try:
                    walk(fn["body"])
                finally:
                    ctx["vars"], ctx["funcs"] = outer, saved_funcs
            elif kind == "setindex":
                try:
                    ctx["first"] = _eval(nd[2], line, ctx).value
                except Exception:
                    ctx["first"] = 0
            elif kind == "connect" and nd[6] is not None:
                out.add(pt(nd[6], line, "this connect goes to"))
            elif kind == "route" and nd[2] == "exit":
                out.update(pt(it[3], line, "this route goes to") for it in nd[4] if it[3] is not None)
            elif kind == "run":
                out.add(pt(nd[2], line, "run(...) starts"))
            elif kind == "if":
                walk(nd[3])
                walk(nd[4] or [])
                for name in _assigned(nd[3]) | _assigned(nd[4] or []):
                    ctx["vars"].pop(name, None)
            elif kind == "when":
                walk(nd[6])
            elif kind == "for":
                declared.add(nd[2])
                try:
                    vals = [_eval(e, line, ctx).value if e is not None else 1 for e in (nd[3], nd[4], nd[5])]
                    start, step, stop = vals
                    span = (stop - start) / step
                    count = 0 if span < -1e-9 else int(math.floor(span + 1e-9)) + 1
                    if not 0 <= count <= 10_000 or not all(_is_num(v) for v in vals):
                        raise ValueError
                except Exception:
                    count, start, step = None, 0, 1
                if count is None:
                    ctx["vars"].pop(nd[2], None)
                    walk(nd[6])
                else:
                    for i in range(count):
                        ctx["vars"][nd[2]] = (_SV(start + i * step, "int" if type(start + i * step) is int else "float"), [])
                        walk(nd[6])
                ctx["vars"].pop(nd[2], None)

    walk(prog)
    return sorted(out, key=lambda t: (not t.isdigit(), int(t) if t.isdigit() else 0, t))


def uses(code: str, point=None) -> list:
    """The points a sequence uses, as strings: its own `point`, every run(n), and every connect(x, n:input)
    target. Reading another point's parameter or file does not count. Raises ValueError like parse()."""
    return _uses(parse_signals(code), point)


def _short(v) -> str:
    return _show_value(_SV(v))


class _Stop(Exception):
    def __init__(self, kind, reason, line=None):
        super().__init__(reason)
        self.kind, self.reason, self.line = kind, reason, line


class _Done(Exception):
    def __init__(self, kind, line=None):
        super().__init__(kind)
        self.kind, self.line = kind, line


class _Exec:
    """Runs parsed Signet code a statement at a time. block() is a generator, so a Sequence can suspend
    it at a wait. With seq None (a point's own code) the sequence statements are skipped."""

    def __init__(self, host, module, seq=None, lazy=False):
        self.module, self.seq = module, seq
        self.ctx = {"host": host, "vars": {}, "lazy": lazy}
        self.ctx["root"] = self.ctx["vars"]
        self.pending, self.lines = {}, {}      # held connections by module or point; the line of each

    def block(self, nodes):
        ctx, seq = self.ctx, self.seq
        for nd in nodes:
            kind, line = nd[0], nd[1]
            if kind == "let":
                if ctx["lazy"] and _is_live(nd[4], ctx):
                    ctx["vars"][nd[3]] = (_Live(nd[4], line, nd[2]), nd[2])
                else:
                    sv = _traced(nd[4], line, ctx)
                    if nd[2]:
                        sv = _typed(nd[2], sv, line)
                    if not sv.source:
                        sv.source = f"line {line}: {nd[3]}"
                    ctx["vars"][nd[3]] = (sv, nd[2])
            elif kind == "set":
                words = ctx["vars"][nd[2]][1]
                sv = _traced(nd[3], line, ctx)
                if words:
                    sv = _typed(words, sv, line)
                if not sv.source:
                    sv.source = f"line {line}: {nd[2]}"
                ctx["vars"][nd[2]] = (sv, words)
            elif kind == "connect":
                if self._applies(nd[4]):
                    sv = _traced(nd[2], line, ctx)
                    self._hold(line, sv, nd[3], nd[4], nd[5], nd[6])
            elif kind == "route":
                self._route(nd)
            elif kind in ("function", "subprocess"):
                _define_fn(nd, ctx)
            elif kind == "return":
                raise _Return(_eval(nd[2], line, ctx) if nd[2] is not None else None)
            elif kind == "callstmt":
                yield from self._call(nd)
            elif kind == "setindex":
                v = _eval(nd[2], line, ctx)
                if type(v.value) is not int or not -255 <= v.value <= 255:
                    raise SignalError(line, f"index needs a whole number from -255 to 255, got {_show_value(v)}")
                ctx["first"] = v.value
            elif kind == "for":
                yield from self._for(nd)
            elif kind == "lineage":
                text = _ref_text(nd[2], line, ctx)
                sv = _eval(("ref", nd[2]), line, ctx)
                path = sv.path or text
                if sv.file or not path.endswith(".sig"):
                    raise SignalError(line, f"lineage takes a Signal, got {_show_type(sv)}")
                folder = getattr(ctx["host"], "folder", ".")
                say = seq.say if seq is not None else lambda t, ln: (ctx["host"].echo if hasattr(ctx["host"], "echo") else print)(t)
                say(f"line {line}: lineage of {text}", line)
                for ln in lineage(os.path.basename(path), os.path.dirname(path) or folder, getattr(ctx["host"], "module_dirs", ())):
                    say("  " + ln, line)
            elif kind == "echo":
                text = f"line {line}: {nd[3]} = " + ", ".join(_show_value(_eval(a, line, ctx)) for a in nd[2])
                if seq is not None:
                    seq.say(text, line)
                else:
                    (ctx["host"].echo if hasattr(ctx["host"], "echo") else print)(text)
            elif kind == "if":
                c = _eval(nd[2], line, ctx)
                if type(c.value) is not bool:
                    raise SignalError(line, f"the condition is {_show_type(c)}, not a bool")
                yield from self.block(nd[3] if c.value else (nd[4] or []))
            elif kind == "output":
                if ctx.get("out") is None:
                    raise SignalError(line, "output is for a module written in Signet (a .signet file), not the Signals box")
                sv = _eval(nd[4], line, ctx)
                if nd[2]:
                    sv = _typed(nd[2], sv, line)
                ctx["out"](nd[3], sv, line)
            elif kind == "break":
                raise Break(line)
            elif seq is None:
                continue
            elif kind == "when":
                yield from self._when(nd)
            elif kind == "run":
                yield from self._run(nd)
            elif kind == "finish":
                raise _Done("finish", line)
            elif kind == "delay":
                yield from self._delay(nd)
            elif kind == "escape":
                yield from self._escape(nd)

    def _call(self, nd):
        _, line, name, exprs = nd
        ctx = self.ctx
        fn = ctx.get("funcs", {}).get(name)
        if fn is None or fn["kind"] != "subprocess":
            raise SignalError(line, f"unknown subprocess {name}")
        local = _Scope(ctx["root"])
        local.update(_bind(fn["params"], [_traced(a, line, ctx) for a in exprs], name, line, ctx))
        inner = {"host": ctx["host"], "vars": local, "root": ctx["root"], "lazy": ctx["lazy"], "funcs": fn["funcs"],
                 "merged": ctx.setdefault("merged", {}), "first": ctx.get("first", 0)}
        if "out" in ctx:
            inner["out"] = ctx["out"]
        self.ctx = inner
        try:
            yield from self.block(fn["body"])
        except _Return:
            pass
        finally:
            self.ctx = ctx
            ctx["first"] = inner["first"]

    def _applies(self, mod):
        # In a point's own code only its module's connections count; in a sequence, only the sequence's own;
        # for deliveries(), only those to other points.
        if getattr(self, "points_only", False):
            return mod is None
        if self.seq is None:
            return mod is not None and mod == self.module
        return not (mod is not None and self.module is not None)

    def _hold(self, line, sv, words, mod, port, point):
        ctx = self.ctx
        if point is not None:
            point = _point_str(point, line, ctx)
        target = point if mod is None else mod
        name = f"{mod}.{port}" if mod else f"{point}:{port}"
        if (target, port) in self.lines:
            raise SignalError(line, f"{name} is connected twice (lines {self.lines[target, port]} and {line})")
        trace = sv.trace or {}
        if words:
            sv = _typed(words, sv, line)
        entry = {"value": sv.value, "type": sv.type or infer_type(sv.value), "source": sv.source or f"line {line}"}
        if trace:
            entry["sources"] = dict(trace)
        self.pending.setdefault(target, {})[port] = entry
        self.lines[target, port] = line

    def _route(self, nd):
        _, line, mode, src, items = nd
        ctx = self.ctx
        if mode == "merge":
            text = _ref_text(src[1], line, ctx)
            outer, ctx["touched"] = ctx.get("touched"), {}
            try:
                first = _eval(src, line, ctx)
                merged = _merge(first, [_eval(i, line, ctx) for i in items], line)
                merged.trace = ctx["touched"]
            finally:
                ctx["touched"] = outer
            merged.source = f"line {line}: merge of {text}"
            ctx.setdefault("merged", {})[text] = merged
            return
        sv = None
        for words, mod, port, point in items:
            if self._applies(mod):
                sv = sv or _traced(src, line, ctx)
                self._hold(line, sv, words, mod, port, point)

    def _for(self, nd):
        _, line, name, a, st, b, body = nd
        ctx = self.ctx
        nums = []
        for e in (a, st, b):
            v = _eval(e, line, ctx) if e is not None else _SV(1, "int")
            if not _is_num(v.value):
                raise SignalError(line, f"a loop range needs numbers, got {_show_type(v)}")
            nums.append(v.value)
        start, step, stop = nums
        if step == 0:
            raise SignalError(line, "a loop's step can't be 0")
        if name in ctx["vars"]:
            raise SignalError(line, f"{name} is already a variable")
        span = (stop - start) / step
        count = 0 if span < -1e-9 else int(math.floor(span + 1e-9)) + 1
        if count > _LOOP_MAX:
            raise SignalError(line, f"the loop would run {count} times; the most is {_LOOP_MAX}")
        kind = "int" if all(type(n) is int for n in nums) else "float"
        try:
            for i in range(count):
                ctx["vars"][name] = (_SV(start + i * step, kind, f"line {line}: {name}"), [])
                yield from self.block(body)
        finally:
            ctx["vars"].pop(name, None)

    def _seconds(self, expr, line, word, floor):
        v = _eval(expr, line, self.ctx)
        if not _is_num(v.value) or v.value < floor or (floor and v.value == floor):
            raise SignalError(line, f"{word} needs a number of seconds{' above 0' if floor else ''}, got {_show_value(v)}")
        return v.value

    def _when(self, nd):
        _, line, cond, within, by, text, body = nd
        seq, ctx = self.seq, self.ctx
        limits = []
        if by is not None:
            s = self._seconds(by, line, "by", 0)
            limits.append((seq.t0 + s, f"by {s:g} s from the start"))
        if within is not None:
            s = self._seconds(within, line, "within", 0)
            limits.append((seq.now + s, f"within {s:g} s"))
        seq.event("when", line, text=text)
        while True:
            seq.check_runs()
            note = ""
            try:
                c = _eval(cond, line, ctx)
            except _Pending as e:
                c, note = None, str(e).split(": ", 1)[-1]
            if c is not None:
                if type(c.value) is not bool:
                    raise SignalError(line, f"the condition is {_show_type(c)}, not a bool")
                if c.value:
                    break
            late = [why for t, why in limits if seq.now > t]
            if late:
                raise _Stop("deadline", f"line {line}: the condition wasn't true {late[0]}" + (f" ({note})" if note else ""), line)
            seq.waiting("when", line, text.removeprefix("when ").strip(), note)
            yield
        seq.event("when_true", line)
        yield from self.block(body)

    def _run(self, nd):
        _, line, point = nd
        seq, host = self.seq, self.ctx["host"]
        point = _point_str(point, line, self.ctx)
        inputs = self.pending.pop(point, {})
        for k in [k for k in self.lines if k[0] == point]:
            del self.lines[k]
        if not hasattr(host, "run"):
            raise SignalError(line, "this host can't start runs")
        try:
            host.run(point, inputs)
        except (LookupError, RuntimeError) as err:
            raise SignalError(line, str(err.args[0] if err.args else err))
        seq.started(point, line, inputs)
        state = getattr(host, "state", None)
        while state is not None:
            seq.check_runs()
            if state(point) in ("running", "complete", "failed", "broken"):
                break
            seq.waiting("run", line, f"RUN on point {point}", "")
            yield
        seq.event("run_started", line, point=point)

    def _delay(self, nd):
        _, line, seconds = nd
        seq = self.seq
        s = self._seconds(seconds, line, "delay", -1)
        until = seq.now + s
        seq.event("delay", line, seconds=s)
        while seq.now < until:
            seq.check_runs()
            seq.waiting("delay", line, f"delay({s:g})", "")
            yield

    def _escape(self, nd):
        _, line, point, seconds = nd
        seq, host = self.seq, self.ctx["host"]
        point = _point_str(point, line, self.ctx)
        limit = None if seconds is None else self._seconds(seconds, line, "escape", 0)
        until = None if limit is None else seq.now + limit
        while True:
            seq.check_runs()
            st = host.state(point) if hasattr(host, "state") else "none"
            if st == "complete":
                raise _Done("escape", line)
            if st in ("failed", "broken"):
                raise _Stop("run_failed", f"line {line}: point {point} {st}", line)
            if until is not None and seq.now > until:
                raise _Stop("deadline", f"line {line}: point {point} hadn't finished {limit:g} s after escape", line)
            seq.waiting("escape", line, f"point {point} to end", "")
            yield


Status = namedtuple("Status", "state reason kind line events")
Status.__doc__ = """After step(): state waiting/finished/broken; kind is what it waits on or how it ended."""


class Sequence:
    """Signet code that runs other points in order, waiting for conditions. The caller owns threads, processes
    and the clock: call step(now) about every second and when a run ends, until its state isn't "waiting".
    Events (seq.log, Status.events) are {"t": seconds since start, "event", "line", ...}."""

    def __init__(self, code, point, host, module=None, now=None):
        self.prog = parse_signals(code)
        self.point = None if point is None else str(point)
        self.host, self.module = host, module
        self.points = _uses(self.prog, self.point)
        self.t0 = self.now = now
        self.log, self._new = [], []
        self.status = Status("waiting", "", "", None, [])
        self._watch = {} if self.point is None else {self.point: None}   # point -> line of its run()
        self._wait = ("", None, "", "")
        self._lock = threading.RLock()
        self._exec = _Exec(host, module, self, lazy=True)
        self._gen = self._exec.block(self.prog)

    def event(self, name, line=None, **fields):
        t = 0.0 if self.t0 is None or self.now is None else round(self.now - self.t0, 3)
        ev = {"t": t, "event": name, "line": line, **fields}
        self.log.append(ev)
        self._new.append(ev)

    def say(self, text, line):
        self.event("echo", line, text=text)
        if hasattr(self.host, "echo"):
            self.host.echo(text)

    def waiting(self, kind, line, text, note):
        self._wait = (kind, line, text, note)

    def started(self, point, line, inputs):
        self._watch[point] = line
        self.event("run", line, point=point, inputs={k: {"type": v["type"], "source": v["source"], "value": _short(v["value"])}
                                                     for k, v in inputs.items()})

    def check_runs(self):
        # A run the sequence started that fails or breaks breaks the sequence.
        state = getattr(self.host, "state", None)
        for point, line in list(self._watch.items()) if state else ():
            st = state(point)
            if st in ("failed", "broken"):
                raise _Stop("run_failed", (f"line {line}: " if line else "") + f"point {point} {st}", line)

    def step(self, now) -> Status:
        with self._lock:
            if self.status.state != "waiting":
                return self.status._replace(events=[])
            self.now = now
            if self.t0 is None:
                self.t0 = now
            self._new = []
            if not self.log:
                self.event("start", None, point=self.point)
            try:
                self.check_runs()
                next(self._gen)
            except StopIteration:
                self._end("finished", "end")
            except _Done as d:
                self._end("finished", d.kind, "", d.line)
            except Break as b:
                self._end("broken", "break", str(b), b.line)
            except _Stop as s:
                self._end("broken", s.kind, s.reason, s.line)
            except SignalError as e:
                self._end("broken", "error", str(e), e.line)
            except Exception as e:      # a host fault must not leave runs going with no sequence
                self._end("broken", "error", f"internal error: {e}")
            else:
                kind, line, text, note = self._wait
                reason = f"line {line}: waiting for {text}" + (f" ({note})" if note else "")
                self.status = Status("waiting", reason, kind, line, list(self._new))
            return self.status

    def break_(self, now=None, reason="broken from outside") -> Status:
        with self._lock:
            if self.status.state != "waiting":
                return self.status._replace(events=[])
            if now is not None:
                self.now = now
                if self.t0 is None:
                    self.t0 = now
            self._new = []
            self._end("broken", "break", reason)
            return self.status

    def _end(self, state, kind, reason="", line=None):
        if state == "broken":
            if hasattr(self.host, "break_runs"):
                try:
                    self.host.break_runs()
                except Exception as e:
                    self.event("error", None, message=f"break_runs failed: {e}")
        else:
            for target in self._exec.pending:
                self.say(f"connections to {target} were never used: no run({target}) after them", None)
        self.event("finish" if state == "finished" else "break", line, kind=kind, reason=reason)
        self.status = Status(state, reason, kind, line, list(self._new))
        self._gen.close()


def evaluate(code: str, module, host, modules=None) -> dict:
    """A point's own code run against a host: the inputs of `module`, {port: {value, type, source}}.
    Other modules' connections and sequence statements are skipped. Raises ValueError or Break."""
    if module is None:
        raise ValueError("evaluate needs the module being run; a plain folder runs code with Sequence")
    prog = parse_signals(code)
    connects = [(nd[1], nd[4]) for nd in _walk(prog) if nd[0] == "connect" and nd[4] is not None]
    connects += [(nd[1], it[1]) for nd in _walk(prog) if nd[0] == "route" and nd[2] == "exit" for it in nd[4] if it[1] is not None]
    if modules is not None:
        stems = [os.path.splitext(m)[0] for m in modules]
        bad = [f"line {ln}: {m} isn't registered for this point" for ln, m in connects if m not in stems]
        if bad:
            raise ValueError("\n".join(bad))
        if any(m == module for _, m in connects) and stems.count(module) > 1:
            raise ValueError(f"two registered modules are named {module}; register only one")
    if any(m == module for _, m in connects) and not _STEM.match(module):
        raise ValueError(f"{module} can't use signals: its name must be letters, digits, _ and -")
    ex = _Exec(host, module, lazy=any(nd[0] in _SEQ_KINDS for nd in _walk(prog)))
    try:
        for _ in ex.block(prog):
            pass
    except SignalError as e:
        raise ValueError(str(e))
    return ex.pending.get(module, {})


def deliveries(code: str, point, host) -> dict:
    """What a point's code gives another point's module: connect(x, 20:volts) and route(x, exit, [20:volts]),
    as {port: {value, type, source}} for `point`. Raises ValueError or Break."""
    prog = parse_signals(code)
    ex = _Exec(host, None, lazy=False)
    ex.points_only = True
    try:
        for _ in ex.block(prog):
            pass
    except SignalError as e:
        raise ValueError(str(e))
    return ex.pending.get(str(point), {})


class _ModuleHost:
    """What a module written in Signet sees: no live signals, no other points; echo goes to its output."""

    def __init__(self, echo):
        self.echo = echo

    def resolve(self, text):
        raise LookupError("a module has no live signals: read its inputs by name")

    def point_param(self, point, name):
        raise LookupError("a module has no point parameters: read its inputs by name")

    def param(self, name):
        raise LookupError("a module has no parameters: read its inputs by name")


def run_module_code(code: str, inputs: dict, head: str, echo=print) -> list:
    """Run a module written in Signet. `inputs` is {port: {value, type, source}}; each is a variable of the
    same name. `output(<name>, <value>)` writes <head><name>.sig (head is "<prefix>-<module>."), and
    echo(...) goes to `echo`. Returns the output names written. Raises ValueError (listing bad lines, or
    the first error) or Break; no output is kept if the code stops."""
    prog = parse_signals(code, inputs)
    bad = [f"line {nd[1]}: {nd[0]} isn't used in a module" for nd in _walk(prog)
           if nd[0] == "connect" or nd[0] == "route" or nd[0] in _SEQ_KINDS]
    if bad:
        raise ValueError("\n".join(bad))
    ex = _Exec(_ModuleHost(echo), None)
    for port, e in inputs.items():
        v = e["value"]
        if e.get("type") == "hex" and type(v) is int:
            v = Hex(v)
        elif v is None and e.get("type") == "float":
            v = math.nan
        ex.ctx["vars"][port] = (_SV(v, e.get("type") or infer_type(v), e.get("source", "")), [])
    done = {}

    def out(name, sv, line):
        if not _OUTNAME.match(name) or name in _RESERVED_PORTS:
            raise SignalError(line, f"{name!r} can't be an output name")
        if name in done:
            raise SignalError(line, f"output {name} is written twice (lines {done[name]} and {line})")
        if sv.file:
            raise SignalError(line, f"output {name}: a file can't be an output value")
        done[name] = line
        sig = {"value": _denan(sv.value)}
        if sv.type or infer_type(sv.value):
            sig["type"] = sv.type or infer_type(sv.value)
        pending[name] = sig

    pending = {}
    ex.ctx["out"] = out
    try:
        for _ in ex.block(prog):
            pass
    except SignalError as e:
        raise ValueError(str(e))
    for name, sig in pending.items():       # written only when the whole run went well
        path = f"{head}{name}.sig"
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(json.dumps(sig, ensure_ascii=False, allow_nan=False) + "\n")
        os.replace(path + ".tmp", path)
    return list(pending)


def _pshow(p):
    return "expr" if isinstance(p, tuple) else p


def connections(code: str, points: bool = False) -> list:
    """Every connect() in the code, without running it: [{line, module, port, types, refs, constants, buses,
    conditional, point}]. Cross-point connects (module "") only with points=True. Raises ValueError."""
    prog = parse_signals(code)
    names, out, subs = {}, [], {}
    def walk(nodes, cond):
        for nd in nodes:
            if nd[0] == "subprocess":
                subs[nd[2]] = nd
            elif nd[0] == "callstmt":
                d = subs[nd[2]]
                for (_, p), a in zip(d[3], nd[3]):
                    names.setdefault(p, []).append(a)
                walk(d[6], True)
            elif nd[0] == "let":
                names.setdefault(nd[3], []).append(nd[4])
            elif nd[0] == "set":
                names.setdefault(nd[2], []).append(nd[3])
            elif nd[0] == "if":
                walk(nd[3], True)
                walk(nd[4] or [], True)
            elif nd[0] in ("when", "for"):
                walk(nd[6], True)
            elif nd[0] == "connect" and (nd[4] is not None or points):
                refs, consts, buses = _expr_sources(nd[2], names)
                out.append({"line": nd[1], "module": nd[4] or "", "port": nd[5], "types": nd[3], "refs": refs,
                            "constants": consts, "buses": buses, "conditional": cond, "point": _pshow(nd[6])})
            elif nd[0] == "route" and nd[2] == "exit":
                refs, consts, buses = _expr_sources(nd[3], names)
                for words, mod, port, point in nd[4]:
                    if mod is not None or points:
                        out.append({"line": nd[1], "module": mod or "", "port": port, "types": words, "refs": refs,
                                    "constants": consts, "buses": buses, "conditional": cond, "point": _pshow(point)})
    walk(prog, False)
    return out


_PROVENANCE = ("origin", "generated", "hash", "inputs", "module", "signet")


def provenance(inputs=None, module_file=None) -> dict:
    """What a stamp records about the run that made a Signal: each input's value hash and what it was
    worked out from, the module file's hash, and the Signet version."""
    out = {"signet": VERSION}
    if inputs is not None:
        out["inputs"] = {port: {"value": hashlib.sha256(_canonical({"value": _denan(e.get("value")), "type": e.get("type")}).encode()).hexdigest(),
                                "sources": dict(e.get("sources") or {})} for port, e in inputs.items()}
    if module_file:
        out["module"] = {"file": os.path.basename(module_file), "sha256": _file_hash(module_file)[len("sha256:"):]}
    return out


def stamp(prefix: str, module: str, written, origin: str, inputs=None, module_file=None) -> dict:
    """After a run that ended well: stamp the .sig files the module wrote (named <prefix>-<module>.<port>.sig,
    or <module>.<port>.sig with no prefix) and delete its older ones that it didn't rewrite.
    `origin` is put before "<module>.<port>" in each stamp; `inputs` (the run's inputs) and `module_file`
    are recorded for lineage. {"stamped", "deleted", "errors"}."""
    prov = provenance(inputs, module_file)
    head = f"{prefix}-{module}." if prefix else f"{module}."
    pat = re.compile(r"^" + re.escape(head) + r"([A-Za-z_][A-Za-z0-9_]*)\.sig$")
    out = {"stamped": [], "deleted": [], "errors": []}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mine = [n for n in written if pat.match(n)]
    for n in mine:
        try:
            if os.path.getsize(n) > _SIG_MAX:
                raise ValueError("is over 10 MB")
            with open(n, encoding="utf-8") as f:
                obj = json.load(f)       # Python's json reads a bare NaN
            if not isinstance(obj, dict) or "value" not in obj:
                raise ValueError("isn't a JSON object with a value")
        except (UnicodeDecodeError, json.JSONDecodeError):
            out["errors"].append(f"{n}: is not valid JSON")
            continue
        except (OSError, ValueError) as e:
            out["errors"].append(f"{n}: {e.strerror if isinstance(e, OSError) else e}")
            continue
        obj = {k: _denan(v) for k, v in obj.items() if k not in _PROVENANCE}
        if obj.get("type") is not None and not isinstance(obj["type"], str):
            obj["type"] = str(obj["type"])
        obj["origin"] = f"{origin}{module}.{pat.match(n).group(1)}"
        obj["generated"] = now
        obj.update(prov)
        obj["hash"] = _sig_hash(obj)
        tmp = n + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, n)
        out["stamped"].append(n)
    try:
        names = os.listdir(os.getcwd())
    except OSError:
        names = []
    for n in sorted(names):
        if pat.match(n) and n not in mine:
            try:
                os.remove(n)
                out["deleted"].append(n)
            except OSError as e:
                out["errors"].append(f"{n}: can't delete ({e.strerror})")
    return out


def _sig_index(folder: str) -> dict:
    """{stamp hash: file name} of the stamped Signals in a folder."""
    out = {}
    for n in sorted(os.listdir(folder)):
        if n.endswith(".sig"):
            try:
                out[read_sig(os.path.join(folder, n))["hash"]] = n
            except ValueError:
                pass
    return out


def _edited(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return isinstance(obj, dict) and bool(obj.get("hash")) and obj["hash"] != _sig_hash(obj)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def lineage(path: str, folder: str = ".", module_dirs=()) -> list:
    """The chain that made a Signal, as lines of text: each Signal checked against its stamp, the module that
    wrote it checked against the hash it had then, and each input followed back to the Signals, files and
    parameters it came from."""
    index = _sig_index(folder)
    lines, seen = [], set()

    def walk(name, depth, want=None):
        pad = "  " * depth
        full = os.path.join(folder, name)
        if depth > 32:
            lines.append(pad + f"{name}: too deep to follow")
            return
        try:
            obj = read_sig(full)
        except ValueError as e:
            if want and want in index:
                name, full = index[want], os.path.join(folder, index[want])
                obj = read_sig(full)
            else:
                lines.append(pad + f"{name}: " + ("missing" if not os.path.isfile(full) else
                                                  "edited after it was stamped" if _edited(full) else str(e)))
                return
        if want and obj["hash"] != want:
            if want in index:          # the one that was used is still here under another name
                return walk(index[want], depth, want)
            lines.append(pad + f"{name}: replaced by a later run; the one used ({want[:12]}) is gone")
            return
        shown = _show_value(_SV(_bits_from(str(obj.get("type") or ""), obj["value"]), str(obj.get("type") or "")))
        if len(shown) > 60:
            shown = shown[:57] + "..."
        lines.append(pad + f"{name} = {shown}  {obj.get('type') or ''}  made by {obj.get('origin', '?')} at {obj.get('generated', '?')}, stamp ok")
        if obj["hash"] in seen:
            lines.append(pad + "  (shown above)")
            return
        seen.add(obj["hash"])
        mod = obj.get("module")
        if isinstance(mod, dict) and mod.get("file"):
            now = next((h for h in (_file_hash(os.path.join(d, mod["file"])) for d in (folder, *module_dirs)) if h), "")
            state = "unchanged" if now == "sha256:" + str(mod.get("sha256")) else "missing" if not now else "changed since"
            lines.append(pad + f"  module {mod['file']} ({state})")
        if "inputs" not in obj:
            lines.append(pad + "  (stamped without a record of its inputs)")
            return
        if not obj["inputs"]:
            lines.append(pad + "  no inputs")
        for port, entry in obj["inputs"].items():
            srcs = (entry or {}).get("sources") or {}
            if not srcs:
                lines.append(pad + f"  {port} <- written in the code")
            for ref, h in srcs.items():
                if "$" in ref:
                    lines.append(pad + f"  {port} <- {ref} = {h}")
                elif h is None:
                    lines.append(pad + f"  {port} <- {ref} (a stream still being written: not checked)")
                elif str(h).startswith("sha256:"):
                    now = _file_hash(os.path.join(folder, ref))
                    lines.append(pad + f"  {port} <- file {ref} ({'unchanged' if now == h else 'missing' if not now else 'changed since'})")
                elif not h:
                    lines.append(pad + f"  {port} <- {ref} (not stamped)")
                else:
                    lines.append(pad + f"  {port} <-")
                    walk(ref, depth + 2, h)

    walk(path, 0)
    return lines


def _flat_tok(s: str) -> str:
    """A text as one space-free token (backslash, space, tab, newline and return escaped; empty is \\e)."""
    if s == "":
        return "\\e"
    return s.replace("\\", "\\\\").replace(" ", "\\s").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _flat_item(v, as_float=False) -> str:
    if v is None:
        return "nan"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _flat_tok(v)
    return repr(float(v)) if as_float or isinstance(v, float) else str(int(v))


def _flat_shape(t: str, v):
    """(shape, tokens) for one input in the plain-text form the C++, Rust, Ada and Fortran helpers read."""
    w = t.split("::")
    if w and w[-1] == "num" and isinstance(v, dict):
        w = w[:-1] + ["complex"]
    scalars = ("int", "float", "real", "num", "hex", "bool", "text")
    if w[0] == "range" and isinstance(v, dict):         # read as a pair: lo, hi
        return "v", [_flat_item(v["lo"], w[1:] != ["int"]), _flat_item(v["hi"], w[1:] != ["int"])]
    if w[0] == "stream" and isinstance(v, dict) and isinstance(v.get("stream"), list):
        return "st", [_flat_tok(str(p)) for p in v["stream"]]
    if w[0] == "bits" and isinstance(v, int) and type(v) is not bool:
        return "s", [str(v)]
    if len(w) == 1 and w[0] in scalars and not isinstance(v, (list, dict)):
        return "s", [_flat_item(v, w[0] == "float")]
    if w == ["complex"] and isinstance(v, dict) and not isinstance(v["re"], list):
        return "c", [_flat_item(v["re"], True), _flat_item(v["im"], True)]
    if w[0] == "vector" and w[1:] == ["complex"] and isinstance(v, dict) and isinstance(v["re"], list) \
            and not any(isinstance(e, list) for e in v["re"]):
        return "vc", [_flat_item(e, True) for p in zip(v["re"], v["im"]) for e in p]
    if w[0] == "vector" and len(w) == 2 and w[1] in scalars and isinstance(v, list) and not any(isinstance(e, list) for e in v):
        return "v", [_flat_item(e, w[1] == "float") for e in v]
    if w[0] == "series" and len(w) == 3 and w[2] in ("float", "complex") and isinstance(v, dict) and series_parts(v):
        x = _series_x(v)
        vals = v["values"]
        if w[2] == "float":
            return "xs", [w[1], str(len(x))] + [_flat_item(e, True) for e in x] + [_flat_item(e, True) for e in vals]
        return "xc", [w[1], str(len(x))] + [_flat_item(e, True) for e in x] + \
            [_flat_item(e, True) for p in zip(vals["re"], vals["im"]) for e in p]
    return "json", [_flat_tok(json.dumps(v, ensure_ascii=False, separators=(",", ":")))]


def flat_inputs(inputs: dict) -> str:
    """The run's inputs as plain text, one line per input: name, type, shape, data (tab separated).
    Shapes: s scalar, c complex, v vector, vc complex vector, xs real series, xc complex series (domain, n,
    x..., values...), st stream (the .sig files it is read from), json (anything else: the JSON text).
    Data tokens are separated by spaces; null is nan."""
    lines = []
    for name, obj in inputs.items():
        v = obj.get("value")
        t = str(obj.get("type") or infer_type(v) or "")
        shape, toks = _flat_shape(t, v)
        if t.startswith("bits::"):            # the compiled languages read bits as an int, a range as two values
            t = "int"
        elif t.startswith("range::"):
            t = "vector::int" if t == "range::int" else "vector::float"
        lines.append("\t".join((name, t or "json", shape, " ".join(toks))))
    return "\n".join(lines) + "\n" if lines else ""


def _flat_path(path: str) -> str:
    return (path[:-5] if path.endswith(".json") else path) + ".txt"


def write_inputs(path: str, inputs: dict) -> None:
    """The run's .inputs.json, and beside it .inputs.txt for the compiled languages; with no inputs,
    stale ones are removed."""
    flat = _flat_path(path)
    if inputs:
        for p, text in ((path, json.dumps(inputs, ensure_ascii=False, indent=1) + "\n"), (flat, flat_inputs(inputs))):
            with open(p + ".tmp", "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(p + ".tmp", p)
    else:
        for p in (path, flat):
            if os.path.isfile(p):
                os.remove(p)


def module_env(env: dict, prefix: str, module: str, inputs_path: str = "") -> dict:
    """A module's environment: SIGNET_PREFIX, SIGNET_MODULE, SIGNET_SIGNALS_IN (and the STARFIRE_ names
    for older modules), with the helpers in front of PYTHONPATH and JULIA_LOAD_PATH."""
    env = dict(env)
    for p in ("SIGNET_", "STARFIRE_"):
        env[p + "PREFIX"], env[p + "MODULE"] = prefix, module
        if inputs_path:
            env[p + "SIGNALS_IN"] = inputs_path
            env[p + "SIGNALS_TXT"] = _flat_path(inputs_path)
        else:
            env.pop(p + "SIGNALS_IN", None)
            env.pop(p + "SIGNALS_TXT", None)
    helpers = helper_dir()
    if helpers:
        env["SIGNET_HELPERS"] = helpers
        for var in ("CPLUS_INCLUDE_PATH", "C_INCLUDE_PATH"):    # #include <signet.hpp> in a build of your own
            env[var] = helpers + (os.pathsep + env[var] if env.get(var) else "")
        for var, sep in (("PYTHONPATH", os.pathsep), ("JULIA_LOAD_PATH", ":")):
            old = env.get(var)
            # JULIA_LOAD_PATH keeps a trailing ":" so Julia's default paths still apply.
            env[var] = helpers + sep + old if old else helpers + (":" if var == "JULIA_LOAD_PATH" else "")
    return env


# Module helpers: `from signet import signals` (Python) and `using Signet: signals` (Julia).
_PY_HELPER = r'''# signet:  SIGNALS FOR MODULES; put on PYTHONPATH by Signet or Starfire
import collections, json, math, os, re, time

__all__ = ["signals", "Series", "Bus", "Stream"]
_PORT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_WORDS = {"int", "float", "num", "bool", "text", "complex", "vector", "series", "bus", "stream", "tuple", "bits", "range"}


def _env(key):
    for name in ("SIGNET_" + key, "STARFIRE_" + key):
        if name in os.environ:
            return os.environ[name]
    raise RuntimeError(f"SIGNET_{key} is not set (run from Signet or Starfire)")


class Series:
    """Values against an axis. Read: x is always the full axis; start/step are None for an explicit one."""

    def __init__(self, values, domain, x=None, start=None, step=None):
        if not isinstance(domain, str) or not _PORT.match(domain) or domain.lower() in _WORDS:
            raise ValueError(f"bad series domain {domain!r}")
        values = _plain(values)
        if (x is None) == (start is None and step is None) and not (x is None and start is None and step is None):
            raise ValueError("give a series x, or start and step, not both")
        if x is None and start is None and step is None:
            if not values or not all(isinstance(v, complex) for v in values):
                raise ValueError("a series needs x, or start and step (or a complex vector z = x + iy)")
            x, values = [v.real for v in values], [v.imag for v in values]
        if x is not None:
            x = [float(e) for e in _plain(x)]
            if len(x) != len(values):
                raise ValueError("series x and values differ in length")
        elif step == 0 or start is None or step is None:
            raise ValueError("a series needs start and a non-zero step")
        self.domain, self.values, self.start, self.step = domain.lower(), values, start, step
        self.x = x if x is not None else [start + i * step for i in range(len(values))]
        self._explicit = x is not None

    def _json(self):
        d = {"domain": self.domain, "values": _out(self.values)}
        if self._explicit:
            d["x"] = self.x
        else:
            d["start"], d["step"] = self.start, self.step
        return d

    def __repr__(self):
        return f"Series(domain={self.domain!r}, n={len(self.values)})"


class Stream:
    """A stream input. sample() is the latest (t, value), or None before the first; samples() is the window
    (the last samples kept); iterating gives each (t, value) as it arrives, and ends when the module writing it
    has ended. A reader slower than the window misses the samples that fell out of it."""

    def __init__(self, paths):
        self.paths = list(paths)

    @staticmethod
    def _read(path):
        # (window rows, samples ever written, ended)
        try:
            with open(path, encoding="utf-8") as f:
                v = json.load(f)["value"]
            x, vals = v.get("x") or [], v.get("values") or []
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return [], 0, False
        vals = _cplx(vals["re"], vals["im"]) if isinstance(vals, dict) else _nan(vals)
        return list(zip(x, vals)), v.get("count", len(x)), bool(v.get("ended"))

    def samples(self):
        return sorted((r for p in self.paths for r in self._read(p)[0]), key=lambda r: r[0])

    def sample(self):
        s = self.samples()
        return s[-1] if s else None

    @property
    def ended(self):
        return all(self._read(p)[2] for p in self.paths)

    def __iter__(self):
        seen, done = {p: 0 for p in self.paths}, set()
        while len(done) < len(self.paths):
            new = []
            for p in self.paths:
                rows, count, ended = self._read(p)
                k = min(count - seen[p], len(rows))
                if k > 0:
                    new += rows[-k:]
                seen[p] = max(seen[p], count)
                if ended:
                    done.add(p)
            yield from sorted(new, key=lambda r: r[0])
            if not new and len(done) < len(self.paths):
                time.sleep(0.02)

    def __repr__(self):
        return f"Stream({', '.join(self.paths)})"


class _StreamOut:
    """An output stream: write(t, value) adds a sample, which readers see at once. Its .sig keeps the last
    `window` samples, rewritten whole at each write."""

    def __init__(self, path, domain, window):
        self.path, self.domain, self.window = path, domain, window
        self.kind, self.count = None, 0
        self._x, self._v = collections.deque(maxlen=window), collections.deque(maxlen=window)

    def write(self, t, value):
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t):
            raise ValueError(f"a stream's t must be a finite number, got {t!r}")
        kind = "complex" if isinstance(value, complex) else "float"
        if self.kind is None:
            self.kind = kind
        elif kind == "complex" and self.kind == "float":
            raise TypeError(f"stream {self.path}: expected float, got complex")
        self._x.append(t)
        self._v.append(complex(value) if self.kind == "complex" else None if value is None else float(value))
        self.count += 1
        if self.kind == "complex":
            vals = {"re": [_fin(z.real) for z in self._v], "im": [_fin(z.imag) for z in self._v]}
        else:
            vals = [_fin(e) for e in self._v]
        sig = {"value": {"domain": self.domain, "x": list(self._x), "values": vals, "window": self.window,
                         "count": self.count, "ended": False}, "type": f"stream::{self.domain}::{self.kind}"}
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            f.write(json.dumps(sig, allow_nan=False) + "\n")
        os.replace(self.path + ".tmp", self.path)


def _fin(e):
    return None if e is None or not math.isfinite(e) else e


class Bus(dict):
    """A bundle from multiplex: bus["speed.sig"], or bus.speed by port."""

    def __getattr__(self, port):
        for k, v in self.items():
            if k == port or k.rsplit(".sig", 1)[0].split(".")[-1] == port:
                return v
        raise AttributeError(f"no field {port}")


def _plain(v):
    if getattr(v, "ndim", 0) >= 2:    # a numpy array is kept column by column, as Signet keeps arrays
        v = v.transpose()
    if hasattr(v, "tolist"):          # numpy arrays and scalars
        v = v.tolist()
    if isinstance(v, tuple):
        v = list(v)
    return [_plain(e) for e in v] if isinstance(v, list) else v


def _has_complex(v):
    return isinstance(v, complex) or (isinstance(v, list) and any(_has_complex(e) for e in v))


def _part(v, f):
    if isinstance(v, list):
        return [_part(e, f) for e in v]
    if v is None:
        return None
    r = f(complex(v))
    return None if math.isnan(r) else r


def _out(v):
    # A Python value as JSON: NaN/None -> null, complex -> {"re", "im"}.
    if isinstance(v, Series):
        return v._json()
    v = _plain(v)
    if _has_complex(v):
        return {"re": _part(v, lambda c: c.real), "im": _part(v, lambda c: c.imag)}
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, list):
        return [_out(e) for e in v]
    if isinstance(v, dict):
        return {str(k): _out(e) for k, e in v.items()}
    return v


def _is_num(v):
    return type(v) in (int, float)


def _real(a, depth):
    if depth == 0:
        return () if a is None or _is_num(a) else None
    if not isinstance(a, list):
        return None
    s = {_real(e, depth - 1) for e in a}
    return None if None in s or len(s) > 1 else (len(a),) + (s.pop() if s else ())


def _cdepth(v):
    if not (isinstance(v, dict) and "re" in v and "im" in v):
        return None
    d, a = 0, v["re"]
    while isinstance(a, list):
        d, a = d + 1, (a[0] if a else None)
    r = _real(v["re"], d)
    return d if r is not None and r == _real(v["im"], d) else None


def _series(v):
    if not isinstance(v, dict) or not isinstance(v.get("domain"), str) or "values" not in v:
        return None
    kind = "float" if _real(v["values"], 1) is not None else "complex" if _cdepth(v["values"]) == 1 else None
    return (v["domain"].lower(), kind) if kind else None


def _infer(v):
    if type(v) is bool:
        return "bool"
    if type(v) is int:
        return "int"
    if v is None or type(v) is float:
        return "float"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        d, s = _cdepth(v), _series(v)
        return "vector::" * d + "complex" if d is not None else f"series::{s[0]}::{s[1]}" if s else ""
    if isinstance(v, list):
        inner = {_infer(e) for e in v}
        if inner and inner <= {"int", "float"}:
            inner = {"float"} if "float" in inner else {"int"}
        return "vector::" + inner.pop() if len(inner) == 1 and "" not in inner else "vector"
    return ""


def _ok(words, v):
    if not words or words[0] == "text":
        return True
    w, rest = words[0], words[1:]
    if w in ("int", "float", "real", "num", "bool", "complex") and rest:
        return False
    if w == "int":
        return type(v) is int
    if w == "tuple":
        return isinstance(v, list)
    if w == "bits":
        width = int(rest[0]) if rest and rest[0].isdigit() else 64
        return type(v) is int and 0 <= v < 1 << width and len(rest) <= 1
    if w == "range":
        return isinstance(v, dict) and set(v) == {"lo", "hi"} and all(_is_num(v[k]) for k in v) and v["lo"] <= v["hi"]
    if w in ("float", "real"):
        return v is None or _is_num(v)
    if w == "num":
        return v is None or _is_num(v) or _cdepth(v) == 0
    if w == "bool":
        return type(v) is bool
    if w == "complex":
        return _cdepth(v) == 0
    if w == "vector":
        k = 0
        while k < len(words) and words[k] == "vector":
            k += 1
        if words[k:] == ["complex"]:
            return _cdepth(v) == k
        if words[k:] == ["num"] and isinstance(v, dict):
            return _cdepth(v) == k
        return isinstance(v, list) and all(_ok(rest, e) for e in v)
    if w == "series":
        s = _series(v)
        return bool(s) and all(r == "num" or ((r if r != "real" else "float") == s[1]) if r in ("float", "real", "complex", "num") else r.lower() == s[0] for r in rest)
    return False


def _nan(v):
    return float("nan") if v is None else [_nan(e) for e in v] if isinstance(v, list) else v


def _cplx(re_, im):
    if isinstance(re_, list):
        return [_cplx(a, b) for a, b in zip(re_, im)]
    return complex(float("nan") if re_ is None else re_, float("nan") if im is None else im)


def _in(entry):
    # One .inputs.json entry as a Python value.
    v, t = entry.get("value"), str(entry.get("type") or "")
    words = [w for w in t.split("::") if w]
    if words[:1] == ["bus"] and isinstance(v, dict):
        return Bus({k: _in(e) for k, e in v.items()})
    if words[:1] == ["stream"] and isinstance(v, dict):
        return Stream(v["stream"])
    if words[:1] == ["tuple"] and isinstance(v, list):
        return tuple(v)
    if words[:1] == ["range"] and isinstance(v, dict):
        return (v["lo"], v["hi"])
    if words[:1] == ["series"] and isinstance(v, dict):
        vals = v["values"]
        vals = _cplx(vals["re"], vals["im"]) if isinstance(vals, dict) else _nan(vals)
        if "x" in v:
            return Series(vals, v["domain"], x=v["x"])
        s = Series(vals, v["domain"], start=v["start"], step=v["step"])
        return s
    if "complex" in words and isinstance(v, dict):
        return _cplx(v["re"], v["im"])
    if "float" in words or "real" in words or "num" in words:
        return _nan(v)
    return v


class _Signals:
    """signals.port reads an input; signals.port = value writes an output."""

    def __init__(self):
        object.__setattr__(self, "_inputs", None)

    def __getattr__(self, port):
        if port.startswith("_"):
            raise AttributeError(port)
        if self._inputs is None:
            path, data = os.environ.get("SIGNET_SIGNALS_IN") or os.environ.get("STARFIRE_SIGNALS_IN"), {}
            if path:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            object.__setattr__(self, "_inputs", data)
        if port not in self._inputs:
            raise AttributeError(f"no input {port} (see the point's Signet code)")
        return _in(self._inputs[port])

    def __setattr__(self, port, value):
        self.write(port, value)

    def write(self, port, value, type=None):
        """Write output `port` as <prefix>-<module>.<port>.sig, checked against `type` if given."""
        if not _PORT.match(port) or port in ("write", "series", "stream"):
            raise ValueError(f"{port!r} can't be a port name")
        js = _out(value)
        if type:
            words = [w for w in str(type).split("::") if w]
            if words[0] == "range" and isinstance(js, list) and len(js) == 2:
                js = {"lo": js[0], "hi": js[1]}
            depth = len(words) - 1
            if words[-1] == "complex" and all(w == "vector" for w in words[:-1]) \
                    and _cdepth(js) is None and _real(js, depth) is not None:
                js = {"re": js, "im": _part(js, lambda c: 0.0)}   # a real number as complex
            if not _ok(words, js):
                raise TypeError(f"output {port}: expected {type}, got {_infer(js) or 'a value'}")
        else:
            type = _infer(js)
        sig = {"value": js}
        if type:
            sig["type"] = type
        prefix = _env("PREFIX")
        path = f"{prefix}-{_env('MODULE')}.{port}.sig" if prefix else f"{_env('MODULE')}.{port}.sig"
        tmp = path + ".tmp"          # written whole, then renamed: a reader never sees half a file
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(sig, ensure_ascii=False, allow_nan=False) + "\n")
        os.replace(tmp, path)

    def series(self, values, domain, x=None, start=None, step=None):
        """A series to write: signals.out = signals.series(v, domain="time", start=0.0, step=0.1)."""
        return Series(values, domain, x=x, start=start, step=step)

    def stream(self, port, domain="time", window=1048):
        """An output stream, <prefix>-<module>.<port>.sig: out = signals.stream("volts"); out.write(t, v).
        It keeps the last `window` samples; Signet stamps it when the module ends."""
        if not _PORT.match(port) or port in ("write", "series", "stream"):
            raise ValueError(f"{port!r} can't be a port name")
        if not isinstance(domain, str) or not _PORT.match(domain) or domain.lower() in _WORDS:
            raise ValueError(f"bad stream domain {domain!r}")
        if isinstance(window, bool) or not isinstance(window, int) or window < 1:
            raise ValueError(f"a stream's window must be a whole number above 0, got {window!r}")
        prefix = _env("PREFIX")
        return _StreamOut(f"{prefix}-{_env('MODULE')}.{port}.sig" if prefix else f"{_env('MODULE')}.{port}.sig",
                          domain.lower(), window)


signals = _Signals()
'''

_JL_HELPER = r'''# Signet.jl:  SIGNALS FOR MODULES; needs JSON3, put on JULIA_LOAD_PATH by Signet or Starfire
module Signet

using JSON3
export signals, series, Series, stream, Stream, StreamIn, sample, samples, ended

const PORT = r"^[A-Za-z_][A-Za-z0-9_]*$"

envvar(k) = haskey(ENV, "SIGNET_" * k) ? ENV["SIGNET_" * k] :
            haskey(ENV, "STARFIRE_" * k) ? ENV["STARFIRE_" * k] : error("SIGNET_$k is not set (run from Signet or Starfire)")
inputs_path() = get(ENV, "SIGNET_SIGNALS_IN", get(ENV, "STARFIRE_SIGNALS_IN", ""))

# Values against an axis. x is always the full axis; start/step are nothing for an explicit one.
struct Series
    domain::String
    x::Vector{Float64}
    values::Vector
    start::Union{Float64,Nothing}
    step::Union{Float64,Nothing}
end

function series(values::AbstractVector; domain::AbstractString, x=nothing, start=nothing, step=nothing)
    occursin(PORT, domain) || error("bad series domain $domain")
    if x === nothing && start === nothing && step === nothing
        eltype(values) <: Complex || error("a series needs x, or start and step (or a complex vector z = x + iy)")
        return Series(lowercase(domain), Float64.(real.(values)), collect(imag.(values)), nothing, nothing)
    end
    if x !== nothing
        (start === nothing && step === nothing) || error("give a series x, or start and step, not both")
        length(x) == length(values) || error("series x and values differ in length")
        return Series(lowercase(domain), Float64.(collect(x)), collect(values), nothing, nothing)
    end
    (start !== nothing && step !== nothing && step != 0) || error("a series needs start and a non-zero step")
    xs = [Float64(start) + (i - 1) * Float64(step) for i in 1:length(values)]
    Series(lowercase(domain), xs, collect(values), Float64(start), Float64(step))
end

struct Signals end
const signals = Signals()
const INPUTS = Ref{Any}(nothing)

# JSON3 values as plain Julia ones; arrays narrowed where they can be.
native(v::JSON3.Object) = Dict{String,Any}(String(k) => native(e) for (k, e) in v)
function native(v::JSON3.Array)
    a = Any[native(e) for e in v]
    isempty(a) && return a
    all(e -> e isa Bool, a) && return Bool.(a)
    all(e -> e isa Integer && !(e isa Bool), a) && return Int.(a)
    all(e -> (e isa Real && !(e isa Bool)) || e === nothing, a) && return [e === nothing ? NaN : Float64(e) for e in a]
    a
end
native(v) = v

nan(v) = v === nothing ? NaN : v isa AbstractVector ? map(nan, v) : v
cplx(re, im) = re isa AbstractVector ? [cplx(a, b) for (a, b) in zip(re, im)] :
               complex(re === nothing ? NaN : Float64(re), im === nothing ? NaN : Float64(im))

# One input as a Julia value, from its plain value and type string.
function invalue(v, t::AbstractString)
    words = filter(!isempty, split(t, "::"))
    if !isempty(words) && words[1] == "bus" && v isa Dict
        return Dict(k => invalue(f["value"], string(get(f, "type", ""))) for (k, f) in v)
    elseif !isempty(words) && words[1] == "series" && v isa Dict
        vals = v["values"] isa Dict ? cplx(v["values"]["re"], v["values"]["im"]) : nan(v["values"])
        haskey(v, "x") && return series(vals; domain=v["domain"], x=v["x"])
        return series(vals; domain=v["domain"], start=v["start"], step=v["step"])
    elseif !isempty(words) && words[1] == "range" && v isa Dict
        return (v["lo"], v["hi"])
    elseif !isempty(words) && words[1] == "stream" && v isa Dict
        return StreamIn(String.(v["stream"]))
    end
    r = "complex" in words && v isa Dict ? cplx(v["re"], v["im"]) : ("float" in words || "num" in words) ? nan(v) : v
    # a 2-D array arrives column by column: a Matrix
    if length(words) == 3 && words[1] == words[2] == "vector" && r isa AbstractVector && !isempty(r) &&
       all(e -> e isa AbstractVector && length(e) == length(r[1]), r)
        return reduce(hcat, r)
    end
    r
end

# A stream input. sample(s) is the newest (t, v), or nothing before the first; samples(s) the samples kept;
# ended(s) whether its module has ended; for (t, v) in s follows it sample by sample until its module ends.
struct StreamIn
    paths::Vector{String}
end

function readstream(path)
    v = try
        native(JSON3.read(read(path, String)))["value"]
    catch
        return (Float64[], Any[], 0, false)
    end
    x = Float64.(get(v, "x", Float64[]))
    vals = get(v, "values", Any[])
    vs = vals isa AbstractDict ? cplx(vals["re"], vals["im"]) : nan(vals)
    (x, vs, Int(get(v, "count", length(x))), get(v, "ended", false) === true)
end

function samples(s::StreamIn)
    rows = Tuple{Float64,Any}[]
    for p in s.paths
        x, v, _, _ = readstream(p)
        length(x) == length(v) && append!(rows, collect(zip(x, v)))
    end
    sort!(rows; by=first)
end
sample(s::StreamIn) = (r = samples(s); isempty(r) ? nothing : r[end])
ended(s::StreamIn) = all(p -> readstream(p)[4], s.paths)

Base.IteratorSize(::Type{StreamIn}) = Base.SizeUnknown()
function Base.iterate(s::StreamIn, state=(zeros(Int, length(s.paths)), Tuple{Float64,Any}[]))
    seen, queue = state
    while isempty(queue)
        done = true
        got = Tuple{Float64,Any}[]
        for (k, p) in enumerate(s.paths)
            x, v, count, en = readstream(p)
            if length(x) != length(v)        # caught between writes: try again
                done = false
                continue
            end
            n = min(count - seen[k], length(x))
            n > 0 && append!(got, collect(zip(x[end-n+1:end], v[end-n+1:end])))
            seen[k] = max(seen[k], count)
            done = done && en
        end
        sort!(got; by=first)
        append!(queue, got)
        if isempty(queue)
            done && return nothing
            sleep(0.02)
        end
    end
    (popfirst!(queue), (seen, queue))
end

function Base.getproperty(::Signals, port::Symbol)
    if INPUTS[] === nothing
        INPUTS[] = isempty(inputs_path()) ? JSON3.read("{}") : JSON3.read(read(inputs_path(), String))
    end
    haskey(INPUTS[], port) || error("no input $port (see the point's Signet code)")
    e = INPUTS[][port]
    invalue(native(e[:value]), string(get(e, :type, "")))
end

Base.setproperty!(s::Signals, port::Symbol, v) = write(s, String(port), v)

# A Julia value as JSON: NaN -> null, complex -> {"re", "im"}.
jsonable(v::Series) = begin
    d = Dict{String,Any}("domain" => v.domain, "values" => jsonable(v.values))
    if v.start === nothing
        d["x"] = v.x
    else
        d["start"], d["step"] = v.start, v.step
    end
    d
end
jsonable(v::AbstractFloat) = isfinite(v) ? v : nothing
jsonable(v::Complex) = Dict("re" => jsonable(real(v)), "im" => jsonable(imag(v)))
jsonable(v::AbstractVector{<:Complex}) = Dict("re" => jsonable(real.(v)), "im" => jsonable(imag.(v)))
jsonable(v::AbstractMatrix) = Any[jsonable(collect(c)) for c in eachcol(v)]        # column by column, as Signet keeps arrays
jsonable(v::AbstractMatrix{<:Complex}) = Dict("re" => jsonable(real.(v)), "im" => jsonable(imag.(v)))
jsonable(v::AbstractVector) = Any[jsonable(e) for e in v]
jsonable(v::AbstractDict) = Dict(string(k) => jsonable(e) for (k, e) in v)
jsonable(v) = v

isnum(v) = (v isa Real && !(v isa Bool)) || v === nothing
cdepth(v) = v isa AbstractDict && haskey(v, "re") && haskey(v, "im") ?
            (v["re"] isa AbstractVector ? (isempty(v["re"]) ? 1 : 1 + depth_of(v["re"][1])) : 0) : -1
depth_of(a) = a isa AbstractVector ? (isempty(a) ? 1 : 1 + depth_of(a[1])) : 0

function infer(v)
    v isa Bool && return "bool"
    v isa Integer && return "int"
    (v isa AbstractFloat || v === nothing) && return "float"
    v isa AbstractString && return "text"
    if v isa AbstractDict
        d = cdepth(v)
        d >= 0 && return "vector::"^d * "complex"
        haskey(v, "domain") && return "series::$(v["domain"])::" * (v["values"] isa AbstractDict ? "complex" : "float")
        return ""
    end
    if v isa AbstractVector
        inner = unique(map(infer, v))
        issubset(inner, ["int", "float"]) && !isempty(inner) && (inner = "float" in inner ? ["float"] : ["int"])
        return length(inner) == 1 && inner[1] != "" ? "vector::" * inner[1] : "vector"
    end
    ""
end

function ok(words, v)
    (isempty(words) || words[1] == "text") && return true
    w, rest = words[1], words[2:end]
    w in ("int", "float", "num", "bool", "complex") && !isempty(rest) && return false
    w == "int" && return v isa Integer && !(v isa Bool)
    w in ("float", "num") && return isnum(v)
    w == "bool" && return v isa Bool
    w == "complex" && return cdepth(v) == 0
    if w == "vector"
        k = findfirst(!=("vector"), words)
        k !== nothing && words[k:end] == ["complex"] && return cdepth(v) == k - 1
        return v isa AbstractVector && all(e -> ok(rest, e), v)
    end
    if w == "series"
        v isa AbstractDict && haskey(v, "domain") || return false
        kind = v["values"] isa AbstractDict ? "complex" : "float"
        return all(r -> r in ("float", "complex") ? r == kind : lowercase(r) == v["domain"], rest)
    end
    false
end

# Write output `port` as <prefix>-<module>.<port>.sig, checked against `type` if given.
function Base.write(::Signals, port::AbstractString, v, type::AbstractString="")
    (occursin(PORT, port) && !(port in ("write", "series"))) || error("$port can't be a port name")
    js = jsonable(v)
    if !isempty(type)
        words = filter(!isempty, split(type, "::"))
        if words[end] == "complex" && all(==("vector"), words[1:end-1]) && cdepth(js) < 0
            js = jsonable(v isa AbstractArray ? complex.(v) : complex(v))   # a real number as complex
        end
        ok(words, js) || error("output $port: expected $type, got $(infer(js))")
    else
        type = infer(js)
    end
    emit(port, js, type)
end

function emit(port, js, type)
    sig = Dict{String,Any}("value" => js)
    isempty(type) || (sig["type"] = type)
    prefix = envvar("PREFIX")
    path = isempty(prefix) ? "$(envvar("MODULE")).$port.sig" : "$prefix-$(envvar("MODULE")).$port.sig"
    tmp = path * ".tmp"    # written whole, then renamed: a reader never sees half a file
    open(tmp, "w") do f
        println(f, JSON3.write(sig))
    end
    mv(tmp, path; force=true)
    nothing
end

# An output stream: s = stream("volts"); write(s, t, v). Its .sig keeps the last `window` samples, rewritten
# whole at each write.
mutable struct Stream
    port::String
    domain::String
    window::Int
    count::Int
    kind::String
    x::Vector{Float64}
    values::Vector{ComplexF64}
end

function stream(port::AbstractString; domain::AbstractString="time", window::Integer=1048)
    (occursin(PORT, port) && !(port in ("write", "series", "stream"))) || error("$port can't be a port name")
    occursin(PORT, domain) || error("bad stream domain $domain")
    window > 0 || error("a stream's window must be above 0")
    Stream(port, lowercase(domain), window, 0, "", Float64[], ComplexF64[])
end

function Base.write(s::Stream, t::Real, v::Number)
    isfinite(t) || error("a stream's t must be a finite number")
    kind = v isa Complex ? "complex" : "float"
    isempty(s.kind) && (s.kind = kind)
    kind == "complex" && s.kind == "float" && error("stream $(s.port): expected float, got complex")
    push!(s.x, Float64(t))
    push!(s.values, ComplexF64(v))
    if length(s.x) > s.window
        popfirst!(s.x)
        popfirst!(s.values)
    end
    s.count += 1
    vals = s.kind == "complex" ? jsonable(s.values) : jsonable(real.(s.values))
    emit(s.port, Dict{String,Any}("domain" => s.domain, "x" => s.x, "values" => vals, "window" => s.window,
                                  "count" => s.count, "ended" => false), "stream::$(s.domain)::$(s.kind)")
end

end # module
'''
_CPP_HELPER = r'''// signet.hpp:  SIGNALS FOR C++ MODULES (C++17, standard library only)
// AUTHOR:      DANIEL DESAI
// UPDATED:     2026-10-29
// VERSION:     0.6.0
//
// Built into signet.py: Signet and Starfire put this on the include path, so
// a module just says #include <signet.hpp>. Inputs: signet::in<double>("voltage").
// Outputs: signet::out("power", power, "float"). The type is optional.

#pragma once
#include <algorithm>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <exception>
#include <fstream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>

namespace signet {

template <class V> struct SeriesOf {
    std::string domain;
    std::vector<double> x;
    std::vector<V> values;
};
using Series = SeriesOf<double>;
using ComplexSeries = SeriesOf<std::complex<double>>;
using Complex = std::complex<double>;

namespace detail {

inline std::string env(const std::string& key) {
    for (const char* p : {"SIGNET_", "STARFIRE_"})
        if (const char* v = std::getenv((p + key).c_str())) return v;
    throw std::runtime_error("SIGNET_" + key + " is not set (run from Signet or Starfire)");
}

struct Record {
    std::string type, shape;
    std::vector<std::string> tok;
};

inline std::string unescape(const std::string& t) {
    if (t == "\\e") return "";
    std::string s;
    for (size_t i = 0; i < t.size(); i++) {
        if (t[i] != '\\' || i + 1 == t.size()) { s += t[i]; continue; }
        char c = t[++i];
        s += c == 's' ? ' ' : c == 't' ? '\t' : c == 'n' ? '\n' : c == 'r' ? '\r' : c;
    }
    return s;
}

inline const std::map<std::string, Record>& inputs() {
    static const std::map<std::string, Record> m = [] {
        std::map<std::string, Record> out;
        const char* path = std::getenv("SIGNET_SIGNALS_TXT");
        if (!path) path = std::getenv("STARFIRE_SIGNALS_TXT");
        if (!path) return out;                       // a module with no inputs
        std::ifstream f(path);
        if (!f) throw std::runtime_error(std::string("can't read ") + path);
        std::string line;
        while (std::getline(f, line)) {
            std::vector<std::string> field;
            size_t a = 0, b;
            while ((b = line.find('\t', a)) != std::string::npos && field.size() < 3) { field.push_back(line.substr(a, b - a)); a = b + 1; }
            field.push_back(line.substr(a));
            if (field.size() != 4) continue;
            Record r{field[1], field[2], {}};
            std::istringstream words(field[3]);
            for (std::string w; words >> w;) r.tok.push_back(w);
            out[field[0]] = r;
        }
        return out;
    }();
    return m;
}

inline const Record& get(const std::string& name) {
    auto it = inputs().find(name);
    if (it == inputs().end()) throw std::runtime_error("no input " + name + " (see the point's Signet code)");
    return it->second;
}

inline double num(const std::string& t) { return std::strtod(t.c_str(), nullptr); }   // "nan" reads as NaN

[[noreturn]] inline void wrong(const std::string& name, const Record& r, const char* want) {
    throw std::runtime_error("input " + name + " is " + r.type + ", not " + want);
}

inline void need(const std::string& name, const Record& r, const char* shape, const char* want) {
    if (r.shape != shape) wrong(name, r, want);
}

template <class T, class = void> struct Reader;

template <class T> struct Reader<T, std::enable_if_t<std::is_floating_point_v<T>>> {
    static T read(const std::string& n, const Record& r) {
        need(n, r, "s", "a number");
        if (r.type == "bool" || r.type == "text") wrong(n, r, "a number");
        return (T)num(r.tok.at(0));
    }
};
template <class T> struct Reader<T, std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool>>> {
    static T read(const std::string& n, const Record& r) {
        need(n, r, "s", "an integer");
        if (r.type != "int" && r.type != "hex") wrong(n, r, "an integer");
        return (T)std::strtoll(r.tok.at(0).c_str(), nullptr, 10);
    }
};
template <> struct Reader<bool> {
    static bool read(const std::string& n, const Record& r) {
        need(n, r, "s", "a bool");
        if (r.type != "bool") wrong(n, r, "a bool");
        return r.tok.at(0) == "true";
    }
};
template <> struct Reader<std::string> {
    static std::string read(const std::string& n, const Record& r) {
        need(n, r, "s", "text");
        if (r.type != "text") wrong(n, r, "text");
        return r.tok.empty() ? "" : unescape(r.tok[0]);
    }
};
template <> struct Reader<Complex> {
    static Complex read(const std::string& n, const Record& r) {
        need(n, r, "c", "a complex number");
        return {num(r.tok.at(0)), num(r.tok.at(1))};
    }
};
template <class T> struct Reader<std::vector<T>, std::enable_if_t<std::is_arithmetic_v<T> && !std::is_same_v<T, bool>>> {
    static std::vector<T> read(const std::string& n, const Record& r) {
        need(n, r, "v", "a vector of numbers");
        if (r.type == "vector::text" || r.type == "vector::bool") wrong(n, r, "a vector of numbers");
        std::vector<T> v;
        for (const auto& t : r.tok) v.push_back(std::is_floating_point_v<T> ? (T)num(t) : (T)std::strtoll(t.c_str(), nullptr, 10));
        return v;
    }
};
template <> struct Reader<std::vector<std::string>> {
    static std::vector<std::string> read(const std::string& n, const Record& r) {
        need(n, r, "v", "a vector of text");
        if (r.type != "vector::text") wrong(n, r, "a vector of text");
        std::vector<std::string> v;
        for (const auto& t : r.tok) v.push_back(unescape(t));
        return v;
    }
};
template <> struct Reader<std::vector<Complex>> {
    static std::vector<Complex> read(const std::string& n, const Record& r) {
        need(n, r, "vc", "a vector of complex numbers");
        std::vector<Complex> v;
        for (size_t i = 0; i + 1 < r.tok.size(); i += 2) v.emplace_back(num(r.tok[i]), num(r.tok[i + 1]));
        return v;
    }
};
template <> struct Reader<Series> {
    static Series read(const std::string& n, const Record& r) {
        need(n, r, "xs", "a real series");
        size_t k = std::strtoul(r.tok.at(1).c_str(), nullptr, 10);
        Series s{r.tok[0], {}, {}};
        for (size_t i = 0; i < k; i++) { s.x.push_back(num(r.tok.at(2 + i))); s.values.push_back(num(r.tok.at(2 + k + i))); }
        return s;
    }
};
template <> struct Reader<ComplexSeries> {
    static ComplexSeries read(const std::string& n, const Record& r) {
        need(n, r, "xc", "a complex series");
        size_t k = std::strtoul(r.tok.at(1).c_str(), nullptr, 10);
        ComplexSeries s{r.tok[0], {}, {}};
        for (size_t i = 0; i < k; i++) {
            s.x.push_back(num(r.tok.at(2 + i)));
            s.values.emplace_back(num(r.tok.at(2 + k + 2 * i)), num(r.tok.at(3 + k + 2 * i)));
        }
        return s;
    }
};

// JSON text for the values written to a .sig; NaN and infinity become null, a float always shows a "."
inline std::string jnum(double d) {
    if (!std::isfinite(d)) return "null";
    char b[40];
    for (int p = 15; p <= 17; p++) {
        std::snprintf(b, sizeof b, "%.*g", p, d);
        if (std::strtod(b, nullptr) == d) break;
    }
    std::string s = b;
    if (s.find_first_of(".eE") == std::string::npos) s += ".0";
    return s;
}
inline std::string jstr(const std::string& t) {
    std::string s = "\"";
    for (unsigned char c : t) {
        if (c == '"' || c == '\\') { s += '\\'; s += (char)c; }
        else if (c == '\n') s += "\\n";
        else if (c == '\r') s += "\\r";
        else if (c == '\t') s += "\\t";
        else if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); s += b; }
        else s += (char)c;
    }
    return s + "\"";
}
inline bool floaty(const std::string& type) { return type.size() >= 5 && type.compare(type.size() - 5, 5, "float") == 0; }
inline std::string jint(long long v, const std::string& type) { return floaty(type) ? jnum((double)v) : std::to_string(v); }
inline std::string jlist(const std::vector<std::string>& items) {
    std::string s = "[";
    for (size_t i = 0; i < items.size(); i++) s += (i ? "," : "") + items[i];
    return s + "]";
}
template <class F, class C> std::vector<std::string> map(const C& c, F f) {
    std::vector<std::string> o;
    for (const auto& e : c) o.push_back(f(e));
    return o;
}
inline std::string jcomplex(Complex z) { return "{\"re\":" + jnum(z.real()) + ",\"im\":" + jnum(z.imag()) + "}"; }
inline std::string jcomplexes(const std::vector<Complex>& v) {
    return "{\"re\":" + jlist(map(v, [](Complex z) { return jnum(z.real()); })) + ",\"im\":" +
           jlist(map(v, [](Complex z) { return jnum(z.imag()); })) + "}";
}

// <prefix>-<module>.<name>.sig, written whole and then renamed: a reader never sees half a file
inline void emit(const std::string& name, const std::string& value, const std::string& type) {
    std::string prefix;
    try { prefix = env("PREFIX"); } catch (const std::runtime_error&) {}
    std::string path = (prefix.empty() ? "" : prefix + "-") + env("MODULE") + "." + name + ".sig", tmp = path + ".tmp";
    {
        std::ofstream f(tmp);
        f << "{\"value\":" << value << ",\"type\":" << jstr(type) << "}\n";
        if (!f) throw std::runtime_error("can't write " + tmp);
    }
    if (std::rename(tmp.c_str(), path.c_str()) != 0) throw std::runtime_error("can't rename " + tmp);
}

// An error nobody catches ends the module with "signet: message" and exit code 2, not an abort
inline const bool terminate_installed = [] {
    std::set_terminate([] {
        try {
            if (auto e = std::current_exception()) std::rethrow_exception(e);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "signet: %s\n", e.what());
            std::exit(2);
        } catch (...) {
        }
        std::abort();
    });
    return true;
}();

}  // namespace detail

inline bool has(const std::string& name) { return detail::inputs().count(name) > 0; }

// An input by name: in<double>("v"), in<long long>("n"), in<bool>, in<std::string>, in<std::complex<double>>,
// in<std::vector<double>> (or int, std::string, complex), in<signet::Series>, in<signet::ComplexSeries>
template <class T> T in(const std::string& name) { return detail::Reader<T>::read(name, detail::get(name)); }

// Outputs. The type is optional ("float", "vector::float", ...); a float type turns whole numbers into floats.
template <class T, class = std::enable_if_t<std::is_floating_point_v<T>>>
void out(const std::string& name, T v, const std::string& type = "float") { detail::emit(name, detail::jnum(v), type); }
template <class T, class = std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool>>, class = void>
void out(const std::string& name, T v, const std::string& type = "int") { detail::emit(name, detail::jint((long long)v, type), type); }
inline void out(const std::string& name, bool v, const std::string& type = "bool") { detail::emit(name, v ? "true" : "false", type); }
inline void out(const std::string& name, const std::string& v, const std::string& type = "text") { detail::emit(name, detail::jstr(v), type); }
inline void out(const std::string& name, const char* v, const std::string& type = "text") { detail::emit(name, detail::jstr(v), type); }
inline void out(const std::string& name, Complex v, const std::string& type = "complex") { detail::emit(name, detail::jcomplex(v), type); }
template <class T, class = std::enable_if_t<std::is_floating_point_v<T>>>
void out(const std::string& name, const std::vector<T>& v, const std::string& type = "vector::float") {
    detail::emit(name, detail::jlist(detail::map(v, [](T e) { return detail::jnum(e); })), type);
}
template <class T, class = std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool>>, class = void>
void out(const std::string& name, const std::vector<T>& v, const std::string& type = "vector::int") {
    detail::emit(name, detail::jlist(detail::map(v, [&](T e) { return detail::jint((long long)e, type); })), type);
}
inline void out(const std::string& name, const std::vector<std::string>& v, const std::string& type = "vector::text") {
    detail::emit(name, detail::jlist(detail::map(v, detail::jstr)), type);
}
inline void out(const std::string& name, const std::vector<Complex>& v, const std::string& type = "vector::complex") {
    detail::emit(name, detail::jcomplexes(v), type);
}
inline void out(const std::string& name, const Series& s, const std::string& type = "") {
    detail::emit(name, "{\"domain\":" + detail::jstr(s.domain) + ",\"x\":" + detail::jlist(detail::map(s.x, detail::jnum)) +
                           ",\"values\":" + detail::jlist(detail::map(s.values, detail::jnum)) + "}",
                 type.empty() ? "series::" + s.domain + "::float" : type);
}
inline void out(const std::string& name, const ComplexSeries& s, const std::string& type = "") {
    detail::emit(name, "{\"domain\":" + detail::jstr(s.domain) + ",\"x\":" + detail::jlist(detail::map(s.x, detail::jnum)) +
                           ",\"values\":" + detail::jcomplexes(s.values) + "}",
                 type.empty() ? "series::" + s.domain + "::complex" : type);
}

// An output stream: signet::Stream volts("volts"); volts.write(t, v). Its .sig keeps the last `window` samples
// (1048 unless given), rewritten whole at each write. signet::ComplexStream takes complex values.
template <class V> class StreamOf {
  public:
    explicit StreamOf(std::string name, std::string domain = "time", size_t window = 1048)
        : name_(std::move(name)), domain_(std::move(domain)), window_(window) {
        if (window_ == 0) throw std::invalid_argument("a stream's window must be above 0");
    }
    void write(double t, V v) {
        if (!std::isfinite(t)) throw std::invalid_argument("a stream's t must be a finite number");
        x_.push_back(t);
        v_.push_back(v);
        if (x_.size() > window_) { x_.pop_front(); v_.pop_front(); }
        count_++;
        std::string vals;
        if constexpr (std::is_same_v<V, Complex>) vals = detail::jcomplexes(std::vector<Complex>(v_.begin(), v_.end()));
        else vals = detail::jlist(detail::map(v_, detail::jnum));
        detail::emit(name_, "{\"domain\":" + detail::jstr(domain_) + ",\"x\":" + detail::jlist(detail::map(x_, detail::jnum)) +
                                ",\"values\":" + vals + ",\"window\":" + std::to_string(window_) + ",\"count\":" +
                                std::to_string(count_) + ",\"ended\":false}",
                     "stream::" + domain_ + (std::is_same_v<V, Complex> ? "::complex" : "::float"));
    }

  private:
    std::string name_, domain_;
    size_t window_, count_ = 0;
    std::deque<double> x_;
    std::deque<V> v_;
};
using Stream = StreamOf<double>;
using ComplexStream = StreamOf<Complex>;

namespace detail {
// A stream's .sig, read without a JSON library: the window's x and values, count and ended.
inline size_t key(const std::string& s, const char* k, size_t from = 0) {
    std::string q = std::string("\"") + k + "\"";
    for (size_t p = s.find(q, from); p != std::string::npos; p = s.find(q, p + 1)) {
        size_t i = p + q.size();
        while (i < s.size() && s[i] == ' ') i++;
        if (i < s.size() && s[i] == ':') {
            i++;
            while (i < s.size() && s[i] == ' ') i++;
            return i;
        }
    }
    return std::string::npos;
}
inline std::vector<double> numbers(const std::string& s, size_t i) {
    std::vector<double> out;
    if (i == std::string::npos || s[i] != '[') return out;
    size_t end = s.find(']', i);
    std::string body = s.substr(i + 1, end - i - 1);
    std::stringstream ss(body);
    for (std::string t; std::getline(ss, t, ',');) {
        size_t a = t.find_first_not_of(' ');
        if (a == std::string::npos) continue;
        out.push_back(t.compare(a, 4, "null") == 0 ? NAN : std::strtod(t.c_str() + a, nullptr));
    }
    return out;
}
struct StreamRead {
    std::vector<double> x, v;
    long long count = 0;
    bool ended = false;
};
inline StreamRead read_stream(const std::string& path) {
    StreamRead r;
    std::ifstream f(path);
    if (!f) return r;
    std::stringstream ss;
    ss << f.rdbuf();
    std::string s = ss.str();
    size_t vi = key(s, "values");
    if (vi != std::string::npos && s[vi] == '{') throw std::runtime_error(path + " is a complex stream: read it in Python or Julia");
    r.x = numbers(s, key(s, "x"));
    r.v = numbers(s, vi);
    if (r.v.size() != r.x.size()) return StreamRead{};      // caught between writes: try again
    size_t c = key(s, "count");
    r.count = c == std::string::npos ? (long long)r.x.size() : std::strtoll(s.c_str() + c, nullptr, 10);
    size_t e = key(s, "ended");
    r.ended = e != std::string::npos && s[e] == 't';
    return r;
}
}  // namespace detail

// A real stream input: auto s = signet::in<signet::StreamIn>("volts").
// while (s.next(t, v)) follows it sample by sample until its module ends; s.latest(t, v) is the newest sample
// (false before the first); s.window(x, v) the samples kept; s.ended() whether its module has ended.
class StreamIn {
  public:
    explicit StreamIn(std::vector<std::string> paths) : paths_(std::move(paths)), seen_(paths_.size(), 0) {}
    bool latest(double& t, double& v) const {
        std::vector<double> x, y;
        window(x, y);
        if (x.empty()) return false;
        t = x.back();
        v = y.back();
        return true;
    }
    void window(std::vector<double>& x, std::vector<double>& v) const {
        std::vector<std::pair<double, double>> rows;
        for (const auto& p : paths_) {
            auto r = detail::read_stream(p);
            for (size_t i = 0; i < r.x.size(); i++) rows.emplace_back(r.x[i], r.v[i]);
        }
        std::stable_sort(rows.begin(), rows.end(), [](auto& a, auto& b) { return a.first < b.first; });
        x.clear();
        v.clear();
        for (auto& r : rows) { x.push_back(r.first); v.push_back(r.second); }
    }
    bool ended() const {
        for (const auto& p : paths_) if (!detail::read_stream(p).ended) return false;
        return true;
    }
    bool next(double& t, double& v) {
        while (queue_.empty()) {
            bool done = true;
            std::vector<std::pair<double, double>> got;
            for (size_t k = 0; k < paths_.size(); k++) {
                auto r = detail::read_stream(paths_[k]);
                long long n = std::min<long long>(r.count - seen_[k], (long long)r.x.size());
                for (long long i = (long long)r.x.size() - n; n > 0 && i < (long long)r.x.size(); i++) got.emplace_back(r.x[i], r.v[i]);
                seen_[k] = std::max(seen_[k], r.count);
                done = done && r.ended;
            }
            std::stable_sort(got.begin(), got.end(), [](auto& a, auto& b) { return a.first < b.first; });
            queue_.insert(queue_.end(), got.begin(), got.end());
            if (queue_.empty()) {
                if (done) return false;
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
        }
        t = queue_.front().first;
        v = queue_.front().second;
        queue_.pop_front();
        return true;
    }

  private:
    std::vector<std::string> paths_;
    std::vector<long long> seen_;
    std::deque<std::pair<double, double>> queue_;
};

namespace detail {
template <> struct Reader<StreamIn> {
    static StreamIn read(const std::string& n, const Record& r) {
        need(n, r, "st", "a stream");
        std::vector<std::string> paths;
        for (const auto& t : r.tok) paths.push_back(unescape(t));
        return StreamIn(paths);
    }
};
}  // namespace detail

}  // namespace signet

namespace sf = signet;   // for modules written against the older starfire_signals.hpp
'''
_RS_HELPER = r'''// signet.rs:  SIGNALS FOR RUST MODULES (standard library only)
// AUTHOR:     DANIEL DESAI
// UPDATED:    2026-10-29
// VERSION:    0.6.0
//
// Built into signet.py: Signet and Starfire compile a module against this as the crate `signet`, so a
// module just says `use signet::{get_f64, put};`. Inputs: get_f64("voltage"). Outputs: put("power", &power, "float").
// The type is optional (pass ""). Anything wrong stops the module with a message and exit code 2.

use std::collections::{HashMap, VecDeque};
use std::sync::OnceLock;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Complex {
    pub re: f64,
    pub im: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Series<V> {
    pub domain: String,
    pub x: Vec<f64>,
    pub values: Vec<V>,
}

struct Record {
    kind: String,
    shape: String,
    tok: Vec<String>,
}

fn fail(msg: &str) -> ! {
    eprintln!("signet: {msg}");
    std::process::exit(2)
}

fn env(key: &str) -> Option<String> {
    std::env::var(format!("SIGNET_{key}")).or_else(|_| std::env::var(format!("STARFIRE_{key}"))).ok()
}

fn unescape(t: &str) -> String {
    if t == "\\e" {
        return String::new();
    }
    let mut s = String::new();
    let mut it = t.chars();
    while let Some(c) = it.next() {
        if c != '\\' {
            s.push(c);
            continue;
        }
        match it.next() {
            Some('s') => s.push(' '),
            Some('t') => s.push('\t'),
            Some('n') => s.push('\n'),
            Some('r') => s.push('\r'),
            Some(o) => s.push(o),
            None => s.push('\\'),
        }
    }
    s
}

fn inputs() -> &'static HashMap<String, Record> {
    static M: OnceLock<HashMap<String, Record>> = OnceLock::new();
    M.get_or_init(|| {
        let mut m = HashMap::new();
        let Some(path) = env("SIGNALS_TXT") else { return m };
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| fail(&format!("can't read {path}: {e}")));
        for line in text.lines() {
            let f: Vec<&str> = line.splitn(4, '\t').collect();
            if f.len() == 4 {
                let tok = f[3].split(' ').filter(|t| !t.is_empty()).map(String::from).collect();
                m.insert(f[0].to_string(), Record { kind: f[1].into(), shape: f[2].into(), tok });
            }
        }
        m
    })
}

fn rec(name: &str, shape: &str, want: &str) -> &'static Record {
    let r = inputs().get(name).unwrap_or_else(|| fail(&format!("no input {name} (see the point's Signet code)")));
    if r.shape != shape {
        fail(&format!("input {name} is {}, not {want}", r.kind));
    }
    r
}

fn num(t: &str) -> f64 {
    t.parse().unwrap_or(f64::NAN)
}

fn real(name: &str, r: &Record, want: &str) {
    if r.kind == "bool" || r.kind == "text" || r.kind == "vector::bool" || r.kind == "vector::text" {
        fail(&format!("input {name} is {}, not {want}", r.kind));
    }
}

pub fn has(name: &str) -> bool {
    inputs().contains_key(name)
}

/// A number input; null reads as NaN.
pub fn get_f64(name: &str) -> f64 {
    let r = rec(name, "s", "a number");
    real(name, r, "a number");
    num(&r.tok[0])
}

pub fn get_i64(name: &str) -> i64 {
    let r = rec(name, "s", "an integer");
    if r.kind != "int" && r.kind != "hex" {
        fail(&format!("input {name} is {}, not an integer", r.kind));
    }
    r.tok[0].parse().unwrap_or_else(|_| fail(&format!("input {name} is not a whole number")))
}

pub fn get_bool(name: &str) -> bool {
    let r = rec(name, "s", "a bool");
    if r.kind != "bool" {
        fail(&format!("input {name} is {}, not a bool", r.kind));
    }
    r.tok[0] == "true"
}

pub fn get_text(name: &str) -> String {
    let r = rec(name, "s", "text");
    if r.kind != "text" {
        fail(&format!("input {name} is {}, not text", r.kind));
    }
    r.tok.first().map(|t| unescape(t)).unwrap_or_default()
}

pub fn get_complex(name: &str) -> Complex {
    let r = rec(name, "c", "a complex number");
    Complex { re: num(&r.tok[0]), im: num(&r.tok[1]) }
}

pub fn get_vec_f64(name: &str) -> Vec<f64> {
    let r = rec(name, "v", "a vector of numbers");
    real(name, r, "a vector of numbers");
    r.tok.iter().map(|t| num(t)).collect()
}

pub fn get_vec_i64(name: &str) -> Vec<i64> {
    let r = rec(name, "v", "a vector of integers");
    if r.kind != "vector::int" && r.kind != "vector::hex" {
        fail(&format!("input {name} is {}, not a vector of integers", r.kind));
    }
    r.tok.iter().map(|t| t.parse().unwrap_or_else(|_| fail(&format!("input {name} has a value that is not whole")))).collect()
}

pub fn get_vec_text(name: &str) -> Vec<String> {
    let r = rec(name, "v", "a vector of text");
    if r.kind != "vector::text" {
        fail(&format!("input {name} is {}, not a vector of text", r.kind));
    }
    r.tok.iter().map(|t| unescape(t)).collect()
}

pub fn get_vec_complex(name: &str) -> Vec<Complex> {
    let r = rec(name, "vc", "a vector of complex numbers");
    r.tok.chunks(2).map(|p| Complex { re: num(&p[0]), im: num(&p[1]) }).collect()
}

pub fn get_series(name: &str) -> Series<f64> {
    let r = rec(name, "xs", "a real series");
    let n: usize = r.tok[1].parse().unwrap_or(0);
    Series {
        domain: r.tok[0].clone(),
        x: r.tok[2..2 + n].iter().map(|t| num(t)).collect(),
        values: r.tok[2 + n..2 + 2 * n].iter().map(|t| num(t)).collect(),
    }
}

pub fn get_complex_series(name: &str) -> Series<Complex> {
    let r = rec(name, "xc", "a complex series");
    let n: usize = r.tok[1].parse().unwrap_or(0);
    Series {
        domain: r.tok[0].clone(),
        x: r.tok[2..2 + n].iter().map(|t| num(t)).collect(),
        values: r.tok[2 + n..2 + 3 * n].chunks(2).map(|p| Complex { re: num(&p[0]), im: num(&p[1]) }).collect(),
    }
}

fn jnum(d: f64) -> String {
    if !d.is_finite() {
        return "null".into();
    }
    let s = format!("{d:?}");
    if s.contains(['.', 'e', 'E']) { s } else { format!("{s}.0") }
}

fn jstr(t: &str) -> String {
    let mut s = String::from("\"");
    for c in t.chars() {
        match c {
            '"' => s.push_str("\\\""),
            '\\' => s.push_str("\\\\"),
            '\n' => s.push_str("\\n"),
            '\r' => s.push_str("\\r"),
            '\t' => s.push_str("\\t"),
            c if (c as u32) < 0x20 => s.push_str(&format!("\\u{:04x}", c as u32)),
            c => s.push(c),
        }
    }
    s.push('"');
    s
}

fn jlist<T>(v: &[T], f: impl Fn(&T) -> String) -> String {
    format!("[{}]", v.iter().map(f).collect::<Vec<_>>().join(","))
}

fn jcomplexes(v: &[Complex]) -> String {
    format!("{{\"re\":{},\"im\":{}}}", jlist(v, |z| jnum(z.re)), jlist(v, |z| jnum(z.im)))
}

fn floaty(ty: &str) -> bool {
    ty.ends_with("float")
}

/// What `put` can write. The JSON text and the default type of the value.
pub trait Put {
    fn json(&self, ty: &str) -> (String, String);
}

macro_rules! put_float { ($($t:ty),*) => {$(
    impl Put for $t { fn json(&self, _: &str) -> (String, String) { (jnum(*self as f64), "float".into()) } }
    impl Put for Vec<$t> { fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jnum(*e as f64)), "vector::float".into()) } }
    impl Put for [$t] { fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jnum(*e as f64)), "vector::float".into()) } }
)*}}
macro_rules! put_int { ($($t:ty),*) => {$(
    impl Put for $t { fn json(&self, ty: &str) -> (String, String) {
        (if floaty(ty) { jnum(*self as f64) } else { self.to_string() }, "int".into()) } }
    impl Put for Vec<$t> { fn json(&self, ty: &str) -> (String, String) {
        (jlist(self, |e| if floaty(ty) { jnum(*e as f64) } else { e.to_string() }), "vector::int".into()) } }
    impl Put for [$t] { fn json(&self, ty: &str) -> (String, String) {
        (jlist(self, |e| if floaty(ty) { jnum(*e as f64) } else { e.to_string() }), "vector::int".into()) } }
)*}}
put_float!(f64, f32);
put_int!(i8, i16, i32, i64, u8, u16, u32, u64, usize, isize);

impl Put for bool {
    fn json(&self, _: &str) -> (String, String) { (self.to_string(), "bool".into()) }
}
impl Put for str {
    fn json(&self, _: &str) -> (String, String) { (jstr(self), "text".into()) }
}
impl Put for String {
    fn json(&self, _: &str) -> (String, String) { (jstr(self), "text".into()) }
}
impl Put for Complex {
    fn json(&self, _: &str) -> (String, String) { (format!("{{\"re\":{},\"im\":{}}}", jnum(self.re), jnum(self.im)), "complex".into()) }
}
impl Put for Vec<Complex> {
    fn json(&self, _: &str) -> (String, String) { (jcomplexes(self), "vector::complex".into()) }
}
impl Put for [Complex] {
    fn json(&self, _: &str) -> (String, String) { (jcomplexes(self), "vector::complex".into()) }
}
impl Put for Vec<String> {
    fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jstr(e)), "vector::text".into()) }
}
impl Put for Series<f64> {
    fn json(&self, _: &str) -> (String, String) {
        (format!("{{\"domain\":{},\"x\":{},\"values\":{}}}", jstr(&self.domain), jlist(&self.x, |e| jnum(*e)), jlist(&self.values, |e| jnum(*e))),
         format!("series::{}::float", self.domain))
    }
}
impl Put for Series<Complex> {
    fn json(&self, _: &str) -> (String, String) {
        (format!("{{\"domain\":{},\"x\":{},\"values\":{}}}", jstr(&self.domain), jlist(&self.x, |e| jnum(*e)), jcomplexes(&self.values)),
         format!("series::{}::complex", self.domain))
    }
}

/// Writes output `name` as <prefix>-<module>.<name>.sig, whole and then renamed so a reader never sees half a
/// file. `ty` is the Signet type ("float", "vector::float", ...), or "" for the value's own.
pub fn put<T: Put + ?Sized>(name: &str, value: &T, ty: &str) {
    let (json, own) = value.json(ty);
    emit(name, &json, &if ty.is_empty() { own } else { ty.to_string() });
}

fn emit(name: &str, json: &str, ty: &str) {
    let module = env("MODULE").unwrap_or_else(|| fail("SIGNET_MODULE is not set (run from Signet or Starfire)"));
    let prefix = env("PREFIX").unwrap_or_default();
    let path = if prefix.is_empty() { format!("{module}.{name}.sig") } else { format!("{prefix}-{module}.{name}.sig") };
    let tmp = format!("{path}.tmp");
    std::fs::write(&tmp, format!("{{\"value\":{json},\"type\":{}}}\n", jstr(&ty))).unwrap_or_else(|e| fail(&format!("can't write {tmp}: {e}")));
    std::fs::rename(&tmp, &path).unwrap_or_else(|e| fail(&format!("can't rename {tmp}: {e}")));
}

/// What a stream holds: f64 or Complex.
pub trait StreamValue: Copy {
    const KIND: &'static str;
    fn jvalues(v: &[Self]) -> String;
}
impl StreamValue for f64 {
    const KIND: &'static str = "float";
    fn jvalues(v: &[f64]) -> String { jlist(v, |e| jnum(*e)) }
}
impl StreamValue for Complex {
    const KIND: &'static str = "complex";
    fn jvalues(v: &[Complex]) -> String { jcomplexes(v) }
}

/// An output stream: `let mut s = Stream::<f64>::new("volts", "time", 1048); s.write(t, v);`. Its .sig keeps the
/// last `window` samples, rewritten whole at each write.
pub struct Stream<V: StreamValue> {
    name: String,
    domain: String,
    window: usize,
    count: u64,
    x: VecDeque<f64>,
    values: VecDeque<V>,
}

impl<V: StreamValue> Stream<V> {
    pub fn new(name: &str, domain: &str, window: usize) -> Self {
        if window == 0 {
            fail("a stream's window must be above 0");
        }
        Stream { name: name.into(), domain: domain.into(), window, count: 0, x: VecDeque::new(), values: VecDeque::new() }
    }

    pub fn write(&mut self, t: f64, v: V) {
        if !t.is_finite() {
            fail("a stream's t must be a finite number");
        }
        self.x.push_back(t);
        self.values.push_back(v);
        if self.x.len() > self.window {
            self.x.pop_front();
            self.values.pop_front();
        }
        self.count += 1;
        let x: Vec<f64> = self.x.iter().copied().collect();
        let vals: Vec<V> = self.values.iter().copied().collect();
        emit(&self.name,
             &format!("{{\"domain\":{},\"x\":{},\"values\":{},\"window\":{},\"count\":{},\"ended\":false}}",
                      jstr(&self.domain), jlist(&x, |e| jnum(*e)), V::jvalues(&vals), self.window, self.count),
             &format!("stream::{}::{}", self.domain, V::KIND));
    }
}

// A stream's .sig, read without a JSON library: (x, values, count, ended) of the window.
fn key(s: &str, k: &str) -> Option<usize> {
    let q = format!("\"{k}\"");
    let mut from = 0;
    while let Some(p) = s[from..].find(&q) {
        let mut i = from + p + q.len();
        let b = s.as_bytes();
        while i < b.len() && b[i] == b' ' { i += 1; }
        if i < b.len() && b[i] == b':' {
            i += 1;
            while i < b.len() && b[i] == b' ' { i += 1; }
            return Some(i);
        }
        from += p + 1;
    }
    None
}

fn numbers(s: &str, at: Option<usize>) -> Vec<f64> {
    let Some(i) = at else { return Vec::new() };
    if !s[i..].starts_with('[') { return Vec::new(); }
    let end = s[i..].find(']').map(|e| i + e).unwrap_or(s.len());
    s[i + 1..end].split(',').map(str::trim).filter(|t| !t.is_empty()).map(|t| if t == "null" { f64::NAN } else { num(t) }).collect()
}

fn read_stream(path: &str) -> (Vec<f64>, Vec<f64>, u64, bool) {
    let Ok(s) = std::fs::read_to_string(path) else { return (Vec::new(), Vec::new(), 0, false) };
    let vi = key(&s, "values");
    if vi.map_or(false, |i| s[i..].starts_with('{')) {
        fail(&format!("{path} is a complex stream: read it in Python or Julia"));
    }
    let (x, v) = (numbers(&s, key(&s, "x")), numbers(&s, vi));
    if x.len() != v.len() { return (Vec::new(), Vec::new(), 0, false); }     // caught between writes
    let count = key(&s, "count").map(|i| s[i..].chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse().unwrap_or(0))
        .unwrap_or(x.len() as u64);
    let ended = key(&s, "ended").map_or(false, |i| s[i..].starts_with('t'));
    (x, v, count, ended)
}

/// A real stream input: `let mut s = get_stream("volts");`. `for (t, v) in &mut s` (or `s.next()`) follows it
/// sample by sample until its module ends; `s.latest()` is the newest sample; `s.window()` the samples kept.
pub struct StreamIn {
    paths: Vec<String>,
    seen: Vec<u64>,
    queue: VecDeque<(f64, f64)>,
}

pub fn get_stream(name: &str) -> StreamIn {
    let r = rec(name, "st", "a stream");
    let paths: Vec<String> = r.tok.iter().map(|t| unescape(t)).collect();
    StreamIn { seen: vec![0; paths.len()], paths, queue: VecDeque::new() }
}

impl StreamIn {
    pub fn window(&self) -> Vec<(f64, f64)> {
        let mut rows: Vec<(f64, f64)> = Vec::new();
        for p in &self.paths {
            let (x, v, _, _) = read_stream(p);
            rows.extend(x.into_iter().zip(v));
        }
        rows.sort_by(|a, b| a.0.total_cmp(&b.0));
        rows
    }
    pub fn latest(&self) -> Option<(f64, f64)> {
        self.window().last().copied()
    }
    pub fn ended(&self) -> bool {
        self.paths.iter().all(|p| read_stream(p).3)
    }
}

impl Iterator for StreamIn {
    type Item = (f64, f64);
    fn next(&mut self) -> Option<(f64, f64)> {
        while self.queue.is_empty() {
            let mut done = true;
            let mut got: Vec<(f64, f64)> = Vec::new();
            for (k, p) in self.paths.iter().enumerate() {
                let (x, v, count, ended) = read_stream(p);
                let n = (count.saturating_sub(self.seen[k]) as usize).min(x.len());
                got.extend(x[x.len() - n..].iter().copied().zip(v[v.len() - n..].iter().copied()));
                self.seen[k] = self.seen[k].max(count);
                done &= ended;
            }
            got.sort_by(|a, b| a.0.total_cmp(&b.0));
            self.queue.extend(got);
            if self.queue.is_empty() {
                if done { return None; }
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
        self.queue.pop_front()
    }
}
'''
_ADS_HELPER = r'''--  signet.ads:  SIGNALS FOR ADA MODULES (Ada 2012, standard library only)
--  AUTHOR:      DANIEL DESAI
--  UPDATED:     2026-10-29
--  VERSION:     0.6.0
--
--  Built into signet.py: Signet and Starfire put this on the compiler's search path, so a module
--  just says "with Signet;". Inputs: Signet.Get_Float ("voltage"). Outputs: Signet.Put ("power", P, "float").
--  The type is optional. A bad input raises Signet.Error. A vector aggregate needs a qualifier:
--  Signet.Put ("v", Signet.Float_Vector'(1.0, 2.0)).

with Ada.Numerics.Long_Complex_Types;
with Ada.Strings.Unbounded;

package Signet is

   Error : exception;

   subtype Complex is Ada.Numerics.Long_Complex_Types.Complex;
   subtype Text is Ada.Strings.Unbounded.Unbounded_String;

   type Float_Vector   is array (Positive range <>) of Long_Float;
   type Integer_Vector is array (Positive range <>) of Long_Long_Integer;
   type Complex_Vector is array (Positive range <>) of Complex;
   type Text_Vector    is array (Positive range <>) of Text;

   type Series (N : Natural) is record
      Domain : Text;
      X      : Float_Vector (1 .. N);
      Values : Float_Vector (1 .. N);
   end record;

   type Complex_Series (N : Natural) is record
      Domain : Text;
      X      : Float_Vector (1 .. N);
      Values : Complex_Vector (1 .. N);
   end record;

   function Has (Name : String) return Boolean;

   --  Inputs; null reads as NaN
   function Get_Float          (Name : String) return Long_Float;
   function Get_Integer        (Name : String) return Long_Long_Integer;
   function Get_Boolean        (Name : String) return Boolean;
   function Get_Text           (Name : String) return String;
   function Get_Complex        (Name : String) return Complex;
   function Get_Floats         (Name : String) return Float_Vector;
   function Get_Integers       (Name : String) return Integer_Vector;
   function Get_Texts          (Name : String) return Text_Vector;
   function Get_Complexes      (Name : String) return Complex_Vector;
   function Get_Series         (Name : String) return Series;
   function Get_Complex_Series (Name : String) return Complex_Series;

   --  Outputs: <prefix>-<module>.<name>.sig. Kind is the Signet type ("float", "vector::float", ...),
   --  or "" for the value's own; a float Kind turns whole numbers into floats.
   procedure Put (Name : String; Value : Long_Float;         Kind : String := "");
   procedure Put (Name : String; Value : Long_Long_Integer;  Kind : String := "");
   procedure Put (Name : String; Value : Boolean;            Kind : String := "");
   procedure Put (Name : String; Value : String;             Kind : String := "");
   procedure Put (Name : String; Value : Complex;            Kind : String := "");
   procedure Put (Name : String; Value : Float_Vector;       Kind : String := "");
   procedure Put (Name : String; Value : Integer_Vector;     Kind : String := "");
   procedure Put (Name : String; Value : Text_Vector;        Kind : String := "");
   procedure Put (Name : String; Value : Complex_Vector;     Kind : String := "");
   procedure Put (Name : String; Value : Series;             Kind : String := "");
   procedure Put (Name : String; Value : Complex_Series;     Kind : String := "");

   --  Output streams: S : Signet.Stream := Signet.Open ("volts"); then Signet.Write (S, T, V). Its .sig keeps
   --  the last Window samples, rewritten whole at each write.
   type Stream (Window : Positive) is record
      Name, Domain : Text;
      Count        : Natural := 0;
      X, Values    : Float_Vector (1 .. Window);
   end record;

   type Complex_Stream (Window : Positive) is record
      Name, Domain : Text;
      Count        : Natural := 0;
      X            : Float_Vector (1 .. Window);
      Values       : Complex_Vector (1 .. Window);
   end record;

   function Open (Name : String; Domain : String := "time"; Window : Positive := 1048) return Stream;
   function Open_Complex (Name : String; Domain : String := "time"; Window : Positive := 1048) return Complex_Stream;
   procedure Write (S : in out Stream; T : Long_Float; Value : Long_Float);
   procedure Write (S : in out Complex_Stream; T : Long_Float; Value : Complex);

   --  A real stream input: S : Signet.Stream_In := Signet.Get_Stream ("volts"); then
   --  while Signet.Next (S, T, V) loop ... end loop follows it sample by sample until its module ends.
   --  Signet.Latest (S, T, V) is False before the first sample; Signet.Ended (S) once its module has ended.
   type Stream_In is record
      Path : Text;
      Seen : Long_Long_Integer := 0;
   end record;

   function Get_Stream (Name : String) return Stream_In;
   function Next   (S : in out Stream_In; T, Value : out Long_Float) return Boolean;
   function Latest (S : Stream_In; T, Value : out Long_Float) return Boolean;
   function Ended  (S : Stream_In) return Boolean;

end Signet;
'''
_ADB_HELPER = r'''--  signet.adb:  SIGNALS FOR ADA MODULES (body)
--  AUTHOR:      DANIEL DESAI
--  UPDATED:     2026-10-29
--  VERSION:     0.6.0

with Ada.Containers.Vectors;
with Ada.Environment_Variables;
with Ada.Strings.Fixed;
with Ada.Text_IO;
with Interfaces.C;
with System;

package body Signet is

   use Ada.Strings.Unbounded;
   use type Interfaces.C.int;

   function C_Strtod (S : Interfaces.C.char_array; End_Ptr : System.Address) return Interfaces.C.double
     with Import, Convention => C, External_Name => "strtod";
   function C_Rename (Old_Name, New_Name : Interfaces.C.char_array) return Interfaces.C.int
     with Import, Convention => C, External_Name => "rename";

   package LF_IO is new Ada.Text_IO.Float_IO (Long_Float);

   type Item is record
      Name, Kind, Shape, Data : Unbounded_String;
   end record;
   package Item_Vectors is new Ada.Containers.Vectors (Positive, Item);

   Items  : Item_Vectors.Vector;
   Loaded : Boolean := False;

   function Env (Key : String) return String is
   begin
      if Ada.Environment_Variables.Exists ("SIGNET_" & Key) then
         return Ada.Environment_Variables.Value ("SIGNET_" & Key);
      elsif Ada.Environment_Variables.Exists ("STARFIRE_" & Key) then
         return Ada.Environment_Variables.Value ("STARFIRE_" & Key);
      end if;
      return "";
   end Env;

   function Num (T : String) return Long_Float is
   begin
      return Long_Float (C_Strtod (Interfaces.C.To_C (T), System.Null_Address));
   end Num;

   function Unescape (T : String) return String is
      R : Unbounded_String;
      I : Positive := T'First;
   begin
      if T = "\e" then
         return "";
      end if;
      while I <= T'Last loop
         if T (I) = '\' and then I < T'Last then
            I := I + 1;
            case T (I) is
               when 's'    => Append (R, ' ');
               when 't'    => Append (R, Character'Val (9));
               when 'n'    => Append (R, Character'Val (10));
               when 'r'    => Append (R, Character'Val (13));
               when others => Append (R, T (I));
            end case;
         else
            Append (R, T (I));
         end if;
         I := I + 1;
      end loop;
      return To_String (R);
   end Unescape;

   --  The space-separated tokens of S
   function Tokens (S : String) return Text_Vector is
      Count : Natural := 0;
      In_Tok : Boolean := False;
   begin
      for C of S loop
         if C = ' ' then
            In_Tok := False;
         elsif not In_Tok then
            In_Tok := True;
            Count := Count + 1;
         end if;
      end loop;
      declare
         Result : Text_Vector (1 .. Count);
         N      : Natural := 0;
         Start  : Natural := 0;
      begin
         for I in S'Range loop
            if S (I) = ' ' then
               if Start /= 0 then
                  N := N + 1;
                  Result (N) := To_Unbounded_String (S (Start .. I - 1));
                  Start := 0;
               end if;
            elsif Start = 0 then
               Start := I;
            end if;
         end loop;
         if Start /= 0 then
            N := N + 1;
            Result (N) := To_Unbounded_String (S (Start .. S'Last));
         end if;
         return Result;
      end;
   end Tokens;

   procedure Load is
      Path : constant String := Env ("SIGNALS_TXT");
      F    : Ada.Text_IO.File_Type;
   begin
      Loaded := True;
      if Path = "" then
         return;
      end if;
      begin
         Ada.Text_IO.Open (F, Ada.Text_IO.In_File, Path);
      exception
         when others => raise Error with "can't read " & Path;
      end;
      while not Ada.Text_IO.End_Of_File (F) loop
         declare
            Line : constant String := Ada.Text_IO.Get_Line (F);
            Tabs : array (1 .. 3) of Natural := (others => 0);
            K    : Natural := 0;
         begin
            for I in Line'Range loop
               if Line (I) = Character'Val (9) and then K < 3 then
                  K := K + 1;
                  Tabs (K) := I;
               end if;
            end loop;
            if K = 3 then
               Items.Append
                 ((Name  => To_Unbounded_String (Line (Line'First .. Tabs (1) - 1)),
                   Kind  => To_Unbounded_String (Line (Tabs (1) + 1 .. Tabs (2) - 1)),
                   Shape => To_Unbounded_String (Line (Tabs (2) + 1 .. Tabs (3) - 1)),
                   Data  => To_Unbounded_String (Line (Tabs (3) + 1 .. Line'Last))));
            end if;
         end;
      end loop;
      Ada.Text_IO.Close (F);
   end Load;

   function Find (Name : String) return Item is
   begin
      if not Loaded then
         Load;
      end if;
      for It of Items loop
         if To_String (It.Name) = Name then
            return It;
         end if;
      end loop;
      raise Error with "no input " & Name & " (see the point's Signet code)";
   end Find;

   function Get (Name, Shape, Want : String) return Item is
      It : constant Item := Find (Name);
   begin
      if To_String (It.Shape) /= Shape then
         raise Error with "input " & Name & " is " & To_String (It.Kind) & ", not " & Want;
      end if;
      return It;
   end Get;

   function Has (Name : String) return Boolean is
   begin
      if not Loaded then
         Load;
      end if;
      for It of Items loop
         if To_String (It.Name) = Name then
            return True;
         end if;
      end loop;
      return False;
   end Has;

   procedure Need_Number (Name : String; It : Item; Want : String) is
      K : constant String := To_String (It.Kind);
   begin
      if K = "bool" or K = "text" or K = "vector::bool" or K = "vector::text" then
         raise Error with "input " & Name & " is " & K & ", not " & Want;
      end if;
   end Need_Number;

   function Get_Float (Name : String) return Long_Float is
      It : constant Item := Get (Name, "s", "a number");
   begin
      Need_Number (Name, It, "a number");
      return Num (To_String (Tokens (To_String (It.Data)) (1)));
   end Get_Float;

   function Get_Integer (Name : String) return Long_Long_Integer is
      It : constant Item := Get (Name, "s", "an integer");
      K  : constant String := To_String (It.Kind);
   begin
      if K /= "int" and K /= "hex" then
         raise Error with "input " & Name & " is " & K & ", not an integer";
      end if;
      return Long_Long_Integer'Value (To_String (Tokens (To_String (It.Data)) (1)));
   end Get_Integer;

   function Get_Boolean (Name : String) return Boolean is
      It : constant Item := Get (Name, "s", "a bool");
   begin
      if To_String (It.Kind) /= "bool" then
         raise Error with "input " & Name & " is " & To_String (It.Kind) & ", not a bool";
      end if;
      return To_String (It.Data) = "true";
   end Get_Boolean;

   function Get_Text (Name : String) return String is
      It : constant Item := Get (Name, "s", "text");
   begin
      if To_String (It.Kind) /= "text" then
         raise Error with "input " & Name & " is " & To_String (It.Kind) & ", not text";
      end if;
      return Unescape (To_String (It.Data));
   end Get_Text;

   function Get_Complex (Name : String) return Complex is
      It : constant Item := Get (Name, "c", "a complex number");
      T  : constant Text_Vector := Tokens (To_String (It.Data));
   begin
      return (Re => Num (To_String (T (1))), Im => Num (To_String (T (2))));
   end Get_Complex;

   function Get_Floats (Name : String) return Float_Vector is
      It : constant Item := Get (Name, "v", "a vector of numbers");
   begin
      Need_Number (Name, It, "a vector of numbers");
      declare
         T : constant Text_Vector := Tokens (To_String (It.Data));
         R : Float_Vector (1 .. T'Length);
      begin
         for I in R'Range loop
            R (I) := Num (To_String (T (T'First + I - 1)));
         end loop;
         return R;
      end;
   end Get_Floats;

   function Get_Integers (Name : String) return Integer_Vector is
      It : constant Item := Get (Name, "v", "a vector of integers");
      K  : constant String := To_String (It.Kind);
   begin
      if K /= "vector::int" and K /= "vector::hex" then
         raise Error with "input " & Name & " is " & K & ", not a vector of integers";
      end if;
      declare
         T : constant Text_Vector := Tokens (To_String (It.Data));
         R : Integer_Vector (1 .. T'Length);
      begin
         for I in R'Range loop
            R (I) := Long_Long_Integer'Value (To_String (T (T'First + I - 1)));
         end loop;
         return R;
      end;
   end Get_Integers;

   function Get_Texts (Name : String) return Text_Vector is
      It : constant Item := Get (Name, "v", "a vector of text");
   begin
      if To_String (It.Kind) /= "vector::text" then
         raise Error with "input " & Name & " is " & To_String (It.Kind) & ", not a vector of text";
      end if;
      declare
         T : constant Text_Vector := Tokens (To_String (It.Data));
         R : Text_Vector (1 .. T'Length);
      begin
         for I in R'Range loop
            R (I) := To_Unbounded_String (Unescape (To_String (T (T'First + I - 1))));
         end loop;
         return R;
      end;
   end Get_Texts;

   function Get_Complexes (Name : String) return Complex_Vector is
      It : constant Item := Get (Name, "vc", "a vector of complex numbers");
      T  : constant Text_Vector := Tokens (To_String (It.Data));
      R  : Complex_Vector (1 .. T'Length / 2);
   begin
      for I in R'Range loop
         R (I) := (Re => Num (To_String (T (T'First + 2 * I - 2))), Im => Num (To_String (T (T'First + 2 * I - 1))));
      end loop;
      return R;
   end Get_Complexes;

   function Get_Series (Name : String) return Series is
      It : constant Item := Get (Name, "xs", "a real series");
      T  : constant Text_Vector := Tokens (To_String (It.Data));
      N  : constant Natural := Natural'Value (To_String (T (T'First + 1)));
      R  : Series (N);
   begin
      R.Domain := T (T'First);
      for I in 1 .. N loop
         R.X (I)      := Num (To_String (T (T'First + 1 + I)));
         R.Values (I) := Num (To_String (T (T'First + 1 + N + I)));
      end loop;
      return R;
   end Get_Series;

   function Get_Complex_Series (Name : String) return Complex_Series is
      It : constant Item := Get (Name, "xc", "a complex series");
      T  : constant Text_Vector := Tokens (To_String (It.Data));
      N  : constant Natural := Natural'Value (To_String (T (T'First + 1)));
      R  : Complex_Series (N);
   begin
      R.Domain := T (T'First);
      for I in 1 .. N loop
         R.X (I) := Num (To_String (T (T'First + 1 + I)));
         R.Values (I) := (Re => Num (To_String (T (T'First + 2 * I + N))), Im => Num (To_String (T (T'First + 2 * I + N + 1))));
      end loop;
      return R;
   end Get_Complex_Series;

   --  JSON text. NaN and infinity are null, and a float always shows a "."
   function Trim (S : String) return String is
      A : Natural := S'First;
      B : Natural := S'Last;
   begin
      while A <= B and then S (A) = ' ' loop A := A + 1; end loop;
      while B >= A and then S (B) = ' ' loop B := B - 1; end loop;
      return S (A .. B);
   end Trim;

   function JNum (V : Long_Float) return String is
      Buf  : String (1 .. 64);
      Mag  : constant Long_Float := abs V;
      Plain : constant Boolean := Mag = 0.0 or else (Mag >= 1.0E-4 and then Mag < 1.0E+15);
   begin
      if V /= V or else V > Long_Float'Last or else V < -Long_Float'Last then
         return "null";
      end if;
      for Aft in 1 .. (if Plain then 24 else 16) loop
         if Plain then
            LF_IO.Put (Buf, V, Aft => Aft, Exp => 0);
         else
            begin
               LF_IO.Put (Buf, V, Aft => Aft, Exp => 2);
            exception
               when Ada.Text_IO.Layout_Error => LF_IO.Put (Buf, V, Aft => Aft, Exp => 3);
            end;
         end if;
         if Num (Trim (Buf)) = V or else Aft = (if Plain then 24 else 16) then
            return Trim (Buf);
         end if;
      end loop;
      return Trim (Buf);
   end JNum;

   function JStr (T : String) return String is
      R : Unbounded_String := To_Unbounded_String ("""");
      Hex : constant String := "0123456789abcdef";
   begin
      for C of T loop
         case C is
            when '"'  => Append (R, "\""");
            when '\'  => Append (R, "\\");
            when Character'Val (10) => Append (R, "\n");
            when Character'Val (13) => Append (R, "\r");
            when Character'Val (9)  => Append (R, "\t");
            when others =>
               if Character'Pos (C) < 32 then
                  Append (R, "\u00" & Hex (Character'Pos (C) / 16 + 1) & Hex (Character'Pos (C) mod 16 + 1));
               else
                  Append (R, C);
               end if;
         end case;
      end loop;
      return To_String (R) & """";
   end JStr;

   function Floaty (Kind : String) return Boolean is
   begin
      return Kind'Length >= 5 and then Kind (Kind'Last - 4 .. Kind'Last) = "float";
   end Floaty;

   function JInt (V : Long_Long_Integer; Kind : String) return String is
   begin
      if Floaty (Kind) then
         return JNum (Long_Float (V));
      end if;
      return Trim (Long_Long_Integer'Image (V));
   end JInt;

   function JFloats (V : Float_Vector) return String is
      R : Unbounded_String := To_Unbounded_String ("[");
   begin
      for I in V'Range loop
         Append (R, (if I > V'First then "," else "") & JNum (V (I)));
      end loop;
      return To_String (R) & "]";
   end JFloats;

   function JComplex (V : Complex) return String is
   begin
      return "{""re"":" & JNum (V.Re) & ",""im"":" & JNum (V.Im) & "}";
   end JComplex;

   function JComplexes (V : Complex_Vector) return String is
      Re, Im : Unbounded_String;
   begin
      for I in V'Range loop
         Append (Re, (if I > V'First then "," else "") & JNum (V (I).Re));
         Append (Im, (if I > V'First then "," else "") & JNum (V (I).Im));
      end loop;
      return "{""re"":[" & To_String (Re) & "],""im"":[" & To_String (Im) & "]}";
   end JComplexes;

   --  <prefix>-<module>.<name>.sig, written whole and then renamed: a reader never sees half a file
   procedure Emit (Name, Value, Kind : String) is
      Module : constant String := Env ("MODULE");
      Prefix : constant String := Env ("PREFIX");
      Path   : constant String := (if Prefix = "" then "" else Prefix & "-") & Module & "." & Name & ".sig";
      Tmp    : constant String := Path & ".tmp";
      F      : Ada.Text_IO.File_Type;
   begin
      if Module = "" then
         raise Error with "SIGNET_MODULE is not set (run from Signet or Starfire)";
      end if;
      Ada.Text_IO.Create (F, Ada.Text_IO.Out_File, Tmp);
      Ada.Text_IO.Put_Line (F, "{""value"":" & Value & ",""type"":" & JStr (Kind) & "}");
      Ada.Text_IO.Close (F);
      if C_Rename (Interfaces.C.To_C (Tmp), Interfaces.C.To_C (Path)) /= 0 then
         raise Error with "can't rename " & Tmp;
      end if;
   end Emit;

   function Or_Own (Kind, Own : String) return String is (if Kind = "" then Own else Kind);

   procedure Put (Name : String; Value : Long_Float; Kind : String := "") is
   begin
      Emit (Name, JNum (Value), Or_Own (Kind, "float"));
   end Put;

   procedure Put (Name : String; Value : Long_Long_Integer; Kind : String := "") is
   begin
      Emit (Name, JInt (Value, Kind), Or_Own (Kind, "int"));
   end Put;

   procedure Put (Name : String; Value : Boolean; Kind : String := "") is
   begin
      Emit (Name, (if Value then "true" else "false"), Or_Own (Kind, "bool"));
   end Put;

   procedure Put (Name : String; Value : String; Kind : String := "") is
   begin
      Emit (Name, JStr (Value), Or_Own (Kind, "text"));
   end Put;

   procedure Put (Name : String; Value : Complex; Kind : String := "") is
   begin
      Emit (Name, JComplex (Value), Or_Own (Kind, "complex"));
   end Put;

   procedure Put (Name : String; Value : Float_Vector; Kind : String := "") is
   begin
      Emit (Name, JFloats (Value), Or_Own (Kind, "vector::float"));
   end Put;

   procedure Put (Name : String; Value : Integer_Vector; Kind : String := "") is
      R : Unbounded_String := To_Unbounded_String ("[");
   begin
      for I in Value'Range loop
         Append (R, (if I > Value'First then "," else "") & JInt (Value (I), Kind));
      end loop;
      Emit (Name, To_String (R) & "]", Or_Own (Kind, "vector::int"));
   end Put;

   procedure Put (Name : String; Value : Text_Vector; Kind : String := "") is
      R : Unbounded_String := To_Unbounded_String ("[");
   begin
      for I in Value'Range loop
         Append (R, (if I > Value'First then "," else "") & JStr (To_String (Value (I))));
      end loop;
      Emit (Name, To_String (R) & "]", Or_Own (Kind, "vector::text"));
   end Put;

   procedure Put (Name : String; Value : Complex_Vector; Kind : String := "") is
   begin
      Emit (Name, JComplexes (Value), Or_Own (Kind, "vector::complex"));
   end Put;

   procedure Put (Name : String; Value : Series; Kind : String := "") is
      D : constant String := To_String (Value.Domain);
   begin
      Emit (Name, "{""domain"":" & JStr (D) & ",""x"":" & JFloats (Value.X) & ",""values"":" & JFloats (Value.Values) & "}",
            Or_Own (Kind, "series::" & D & "::float"));
   end Put;

   procedure Put (Name : String; Value : Complex_Series; Kind : String := "") is
      D : constant String := To_String (Value.Domain);
   begin
      Emit (Name, "{""domain"":" & JStr (D) & ",""x"":" & JFloats (Value.X) & ",""values"":" & JComplexes (Value.Values) & "}",
            Or_Own (Kind, "series::" & D & "::complex"));
   end Put;

   function Open (Name : String; Domain : String := "time"; Window : Positive := 1048) return Stream is
   begin
      return (Window => Window, Name => To_Unbounded_String (Name), Domain => To_Unbounded_String (Domain),
              Count => 0, X => (others => 0.0), Values => (others => 0.0));
   end Open;

   function Open_Complex (Name : String; Domain : String := "time"; Window : Positive := 1048) return Complex_Stream is
   begin
      return (Window => Window, Name => To_Unbounded_String (Name), Domain => To_Unbounded_String (Domain),
              Count => 0, X => (others => 0.0), Values => (others => (0.0, 0.0)));
   end Open_Complex;

   function Stream_Json (Domain : String; X, Values : String; Window, Count : Natural) return String is
     ("{""domain"":" & JStr (Domain) & ",""x"":" & X & ",""values"":" & Values & ",""window"":" &
      Ada.Strings.Fixed.Trim (Natural'Image (Window), Ada.Strings.Left) & ",""count"":" &
      Ada.Strings.Fixed.Trim (Natural'Image (Count), Ada.Strings.Left) & ",""ended"":false}");

   procedure Write (S : in out Stream; T : Long_Float; Value : Long_Float) is
      N : Positive;
   begin
      if not T'Valid or else abs T > Long_Float'Last then
         raise Error with "a stream's t must be a finite number";
      end if;
      S.Count := S.Count + 1;
      if S.Count > S.Window then                --  the oldest sample falls out of the window
         S.X (1 .. S.Window - 1) := S.X (2 .. S.Window);
         S.Values (1 .. S.Window - 1) := S.Values (2 .. S.Window);
      end if;
      N := Natural'Min (S.Count, S.Window);
      S.X (N) := T;
      S.Values (N) := Value;
      Emit (To_String (S.Name), Stream_Json (To_String (S.Domain), JFloats (S.X (1 .. N)), JFloats (S.Values (1 .. N)), S.Window, S.Count),
            "stream::" & To_String (S.Domain) & "::float");
   end Write;

   procedure Write (S : in out Complex_Stream; T : Long_Float; Value : Complex) is
      N : Positive;
   begin
      if not T'Valid or else abs T > Long_Float'Last then
         raise Error with "a stream's t must be a finite number";
      end if;
      S.Count := S.Count + 1;
      if S.Count > S.Window then
         S.X (1 .. S.Window - 1) := S.X (2 .. S.Window);
         S.Values (1 .. S.Window - 1) := S.Values (2 .. S.Window);
      end if;
      N := Natural'Min (S.Count, S.Window);
      S.X (N) := T;
      S.Values (N) := Value;
      Emit (To_String (S.Name), Stream_Json (To_String (S.Domain), JFloats (S.X (1 .. N)), JComplexes (S.Values (1 .. N)), S.Window, S.Count),
            "stream::" & To_String (S.Domain) & "::complex");
   end Write;

   --  A stream's .sig, read without a JSON library
   type Stream_Read (N : Natural) is record
      X, V  : Float_Vector (1 .. N);
      Count : Long_Long_Integer;
      Done  : Boolean;
   end record;

   function Read_File (Path : String) return String is
      F : Ada.Text_IO.File_Type;
      R : Unbounded_String;
   begin
      begin
         Ada.Text_IO.Open (F, Ada.Text_IO.In_File, Path);
      exception
         when others => return "";
      end;
      while not Ada.Text_IO.End_Of_File (F) loop
         Append (R, Ada.Text_IO.Get_Line (F));
      end loop;
      Ada.Text_IO.Close (F);
      return To_String (R);
   end Read_File;

   function Key_At (S, K : String) return Natural is
      Q : constant String := '"' & K & '"';
      P : Natural := Ada.Strings.Fixed.Index (S, Q);
      I : Natural;
   begin
      while P /= 0 loop
         I := P + Q'Length;
         while I <= S'Last and then S (I) = ' ' loop
            I := I + 1;
         end loop;
         if I <= S'Last and then S (I) = ':' then
            I := I + 1;
            while I <= S'Last and then S (I) = ' ' loop
               I := I + 1;
            end loop;
            return I;
         end if;
         P := Ada.Strings.Fixed.Index (S, Q, P + 1);
      end loop;
      return 0;
   end Key_At;

   function Numbers (S : String; I : Natural) return Float_Vector is
      Stop : Natural;
   begin
      if I = 0 or else I > S'Last or else S (I) /= '[' then
         return (1 .. 0 => 0.0);
      end if;
      Stop := Ada.Strings.Fixed.Index (S, "]", I);
      declare
         List : constant String := S (I + 1 .. Stop - 1);
      begin
         if Ada.Strings.Fixed.Trim (List, Ada.Strings.Both)'Length = 0 then
            return (1 .. 0 => 0.0);
         end if;
         declare
            R     : Float_Vector (1 .. Ada.Strings.Fixed.Count (List, ",") + 1);
            Start : Positive := List'First;
            K     : Natural := 0;
         begin
            for J in List'First .. List'Last + 1 loop
               if J > List'Last or else List (J) = ',' then
                  K := K + 1;
                  declare
                     T : constant String := Ada.Strings.Fixed.Trim (List (Start .. J - 1), Ada.Strings.Both);
                  begin
                     R (K) := (if T = "null" then Num ("nan") else Num (T));
                  end;
                  Start := J + 1;
               end if;
            end loop;
            return R;
         end;
      end;
   end Numbers;

   function Read_Stream (Path : String) return Stream_Read is
      S  : constant String := Read_File (Path);
      VI : constant Natural := Key_At (S, "values");
   begin
      if VI /= 0 and then S (VI) = '{' then
         raise Error with Path & " is a complex stream: read it in Python or Julia";
      end if;
      declare
         X   : constant Float_Vector := Numbers (S, Key_At (S, "x"));
         V   : constant Float_Vector := Numbers (S, VI);
         CI  : constant Natural := Key_At (S, "count");
         EI  : constant Natural := Key_At (S, "ended");
         Cnt : Long_Long_Integer := X'Length;
         Last : Natural;
      begin
         if X'Length /= V'Length then        --  caught between writes: try again
            return (N => 0, X => (others => 0.0), V => (others => 0.0), Count => 0, Done => False);
         end if;
         if CI /= 0 then
            Last := CI;
            while Last <= S'Last and then S (Last) in '0' .. '9' loop
               Last := Last + 1;
            end loop;
            Cnt := Long_Long_Integer'Value (S (CI .. Last - 1));
         end if;
         return (N => X'Length, X => X, V => V, Count => Cnt, Done => EI /= 0 and then S (EI) = 't');
      end;
   end Read_Stream;

   function Get_Stream (Name : String) return Stream_In is
      It : constant Item := Get (Name, "st", "a stream");
      T  : constant Text_Vector := Tokens (To_String (It.Data));
   begin
      if T'Length /= 1 then
         raise Error with "input " & Name & " merges streams: read it in Python, Julia, C++ or Rust";
      end if;
      return (Path => To_Unbounded_String (Unescape (To_String (T (T'First)))), Seen => 0);
   end Get_Stream;

   function Next (S : in out Stream_In; T, Value : out Long_Float) return Boolean is
   begin
      T := 0.0;
      Value := 0.0;
      loop
         declare
            R : constant Stream_Read := Read_Stream (To_String (S.Path));
         begin
            if R.Count > S.Seen and then R.N > 0 then
               declare
                  Behind : constant Long_Long_Integer := Long_Long_Integer'Min (R.Count - S.Seen, Long_Long_Integer (R.N));
                  I      : constant Positive := R.N - Natural (Behind) + 1;
               begin
                  T := R.X (I);
                  Value := R.V (I);
                  S.Seen := R.Count - Behind + 1;
                  return True;
               end;
            elsif R.Done then
               return False;
            end if;
         end;
         delay 0.02;
      end loop;
   end Next;

   function Latest (S : Stream_In; T, Value : out Long_Float) return Boolean is
      R : constant Stream_Read := Read_Stream (To_String (S.Path));
   begin
      T := 0.0;
      Value := 0.0;
      if R.N = 0 then
         return False;
      end if;
      T := R.X (R.N);
      Value := R.V (R.N);
      return True;
   end Latest;

   function Ended (S : Stream_In) return Boolean is
   begin
      return Read_Stream (To_String (S.Path)).Done;
   end Ended;

end Signet;
'''
_F90_HELPER = r'''! signet.f90:  SIGNALS FOR FORTRAN MODULES (Fortran 2008, standard library only)
! AUTHOR:      DANIEL DESAI
! UPDATED:     2026-10-29
! VERSION:     0.6.0
!
! Built into signet.py: Signet and Starfire compile a module with this on the search path, so a module
! just says "use signet". Inputs: call signet_get("voltage", v). Outputs: call signet_put("power", p, "float").
! The type is optional. A bad input stops the module with a message and a non-zero exit code.

module signet
  use, intrinsic :: iso_c_binding, only: c_char, c_int, c_null_char
  use, intrinsic :: iso_fortran_env, only: iostat_eor, error_unit
  implicit none
  private
  public :: signet_has, signet_get, signet_put, signet_text, signet_series, signet_cseries
  public :: signet_stream, signet_stream_open, signet_stream_write
  public :: signet_stream_in, signet_get_stream, signet_stream_next, signet_stream_latest, signet_stream_ended

  type :: signet_text
    character(:), allocatable :: s
  end type
  type :: signet_series
    character(:), allocatable :: domain
    real(8), allocatable :: x(:), values(:)
  end type
  type :: signet_cseries
    character(:), allocatable :: domain
    real(8), allocatable :: x(:)
    complex(8), allocatable :: values(:)
  end type
  ! An output stream: call signet_stream_open(s, 'volts'), then call signet_stream_write(s, t, v) with a real(8)
  ! or complex(8) v. Its .sig keeps the last window samples (1048 unless given), rewritten whole at each write.
  type :: signet_stream
    character(:), allocatable :: name, domain
    integer :: window = 1048, count = 0
    real(8), allocatable :: x(:), values(:)
    complex(8), allocatable :: cvalues(:)
  end type
  ! A real stream input: call signet_get_stream('volts', s), then do while (signet_stream_next(s, t, v)) ... end do
  ! follows it sample by sample until its module ends. signet_stream_latest(s, t, v) is .false. before the first
  ! sample; signet_stream_ended(s) is .true. once its module has ended.
  type :: signet_stream_in
    character(:), allocatable :: path
    integer(8) :: seen = 0
  end type
  type :: rec
    character(:), allocatable :: name, kind, shape, data
  end type

  type(rec), allocatable, save :: recs(:)
  logical, save :: loaded = .false.

  interface signet_get
    module procedure get_real, get_int4, get_int8, get_logical, get_text, get_complex
    module procedure get_reals, get_ints4, get_ints8, get_texts, get_complexes, get_series, get_cseries
  end interface
  interface signet_put
    module procedure put_real, put_real4, put_int4, put_int8, put_logical, put_text, put_complex
    module procedure put_reals, put_ints4, put_ints8, put_texts, put_complexes, put_series, put_cseries
  end interface

  interface signet_stream_write
    module procedure stream_write_real, stream_write_complex
  end interface

  interface
    function c_rename(old, new) bind(C, name="rename")
      import :: c_char, c_int
      character(kind=c_char), intent(in) :: old(*), new(*)
      integer(c_int) :: c_rename
    end function
    function c_usleep(us) bind(C, name="usleep")
      import :: c_int
      integer(c_int), value :: us
      integer(c_int) :: c_usleep
    end function
  end interface

contains

  subroutine fail(msg)
    character(*), intent(in) :: msg
    write (error_unit, '(A)') 'signet: ' // msg
    stop 2
  end subroutine

  function env(key) result(v)
    character(*), intent(in) :: key
    character(:), allocatable :: v
    integer :: n, st
    v = ''
    call get_environment_variable('SIGNET_' // key, length=n, status=st)
    if (st == 0) then
      v = repeat(" ", n)
      call get_environment_variable('SIGNET_' // key, v)
      return
    end if
    call get_environment_variable('STARFIRE_' // key, length=n, status=st)
    if (st == 0) then
      v = repeat(" ", n)
      call get_environment_variable('STARFIRE_' // key, v)
    end if
  end function

  subroutine read_line(u, line, ios)
    integer, intent(in) :: u
    character(:), allocatable, intent(out) :: line
    integer, intent(out) :: ios
    character(4096) :: chunk
    integer :: n
    line = ''
    do
      n = 0
      read (u, '(A)', advance='no', iostat=ios, size=n) chunk
      line = line // chunk(1:n)
      if (ios == iostat_eor) then
        ios = 0
        return
      end if
      if (ios /= 0) return
    end do
  end subroutine

  function tokens(s) result(t)
    character(*), intent(in) :: s
    type(signet_text), allocatable :: t(:)
    integer :: i, n, start
    logical :: inside
    n = 0
    inside = .false.
    do i = 1, len(s)
      if (s(i:i) == ' ') then
        inside = .false.
      else if (.not. inside) then
        inside = .true.
        n = n + 1
      end if
    end do
    allocate (t(n))
    n = 0
    start = 0
    do i = 1, len(s) + 1
      if (i > len(s)) then
        if (start /= 0) then
          n = n + 1
          t(n)%s = s(start:len(s))
        end if
      else if (s(i:i) == ' ') then
        if (start /= 0) then
          n = n + 1
          t(n)%s = s(start:i - 1)
          start = 0
        end if
      else if (start == 0) then
        start = i
      end if
    end do
  end function

  function unescape(t) result(r)
    character(*), intent(in) :: t
    character(:), allocatable :: r
    integer :: i
    r = ''
    if (t == '\e') return
    i = 1
    do while (i <= len(t))
      if (t(i:i) == '\' .and. i < len(t)) then
        i = i + 1
        select case (t(i:i))
        case ('s'); r = r // ' '
        case ('t'); r = r // achar(9)
        case ('n'); r = r // achar(10)
        case ('r'); r = r // achar(13)
        case default; r = r // t(i:i)
        end select
      else
        r = r // t(i:i)
      end if
      i = i + 1
    end do
  end function

  real(8) function num(t)
    character(*), intent(in) :: t
    integer :: ios
    read (t, *, iostat=ios) num
    if (ios /= 0) num = transfer(-2251799813685248_8, num)   ! NaN
  end function

  integer(8) function whole(t)
    character(*), intent(in) :: t
    integer :: ios
    read (t, *, iostat=ios) whole
    if (ios /= 0) call fail('"' // t // '" is not a whole number')
  end function

  subroutine load()
    character(:), allocatable :: path, line
    integer :: u, ios, t1, t2, t3, n
    type(rec) :: r
    loaded = .true.
    allocate (recs(0))
    path = env('SIGNALS_TXT')
    if (len(path) == 0) return
    open (newunit=u, file=path, status='old', action='read', iostat=ios)
    if (ios /= 0) call fail("can't read " // path)
    do
      call read_line(u, line, ios)
      if (ios /= 0) exit
      t1 = index(line, achar(9))
      if (t1 == 0) cycle
      t2 = t1 + index(line(t1 + 1:), achar(9))
      if (t2 == t1) cycle
      t3 = t2 + index(line(t2 + 1:), achar(9))
      if (t3 == t2) cycle
      r%name = line(1:t1 - 1)
      r%kind = line(t1 + 1:t2 - 1)
      r%shape = line(t2 + 1:t3 - 1)
      r%data = line(t3 + 1:)
      n = size(recs)
      recs = [recs, r]
    end do
    close (u)
  end subroutine

  function find(name, shape, want) result(r)
    character(*), intent(in) :: name, shape, want
    type(rec) :: r
    integer :: i
    if (.not. loaded) call load()
    do i = 1, size(recs)
      if (recs(i)%name == name) then
        if (recs(i)%shape /= shape) call fail('input ' // name // ' is ' // recs(i)%kind // ', not ' // want)
        r = recs(i)
        return
      end if
    end do
    call fail('no input ' // name // " (see the point's Signet code)")
  end function

  logical function signet_has(name)
    character(*), intent(in) :: name
    integer :: i
    if (.not. loaded) call load()
    signet_has = .false.
    do i = 1, size(recs)
      if (recs(i)%name == name) signet_has = .true.
    end do
  end function

  subroutine need_number(name, r, want)
    character(*), intent(in) :: name, want
    type(rec), intent(in) :: r
    if (r%kind == 'bool' .or. r%kind == 'text' .or. r%kind == 'vector::bool' .or. r%kind == 'vector::text') &
      call fail('input ' // name // ' is ' // r%kind // ', not ' // want)
  end subroutine

  subroutine need_whole(name, r, want)
    character(*), intent(in) :: name, want
    type(rec), intent(in) :: r
    if (r%kind /= 'int' .and. r%kind /= 'hex' .and. r%kind /= 'vector::int' .and. r%kind /= 'vector::hex') &
      call fail('input ' // name // ' is ' // r%kind // ', not ' // want)
  end subroutine

  ! ---- inputs ----

  subroutine get_real(name, v)
    character(*), intent(in) :: name
    real(8), intent(out) :: v
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    r = find(name, 's', 'a number')
    call need_number(name, r, 'a number')
    t = tokens(r%data)
    v = num(t(1)%s)
  end subroutine

  subroutine get_int8(name, v)
    character(*), intent(in) :: name
    integer(8), intent(out) :: v
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    r = find(name, 's', 'an integer')
    call need_whole(name, r, 'an integer')
    t = tokens(r%data)
    v = whole(t(1)%s)
  end subroutine

  subroutine get_int4(name, v)
    character(*), intent(in) :: name
    integer(4), intent(out) :: v
    integer(8) :: w
    call get_int8(name, w)
    v = int(w, 4)
  end subroutine

  subroutine get_logical(name, v)
    character(*), intent(in) :: name
    logical, intent(out) :: v
    type(rec) :: r
    r = find(name, 's', 'a bool')
    if (r%kind /= 'bool') call fail('input ' // name // ' is ' // r%kind // ', not a bool')
    v = (r%data == 'true')
  end subroutine

  subroutine get_text(name, v)
    character(*), intent(in) :: name
    character(:), allocatable, intent(out) :: v
    type(rec) :: r
    r = find(name, 's', 'text')
    if (r%kind /= 'text') call fail('input ' // name // ' is ' // r%kind // ', not text')
    v = unescape(r%data)
  end subroutine

  subroutine get_complex(name, v)
    character(*), intent(in) :: name
    complex(8), intent(out) :: v
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    r = find(name, 'c', 'a complex number')
    t = tokens(r%data)
    v = cmplx(num(t(1)%s), num(t(2)%s), 8)
  end subroutine

  subroutine get_reals(name, v)
    character(*), intent(in) :: name
    real(8), allocatable, intent(out) :: v(:)
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i
    r = find(name, 'v', 'a vector of numbers')
    call need_number(name, r, 'a vector of numbers')
    t = tokens(r%data)
    allocate (v(size(t)))
    do i = 1, size(t)
      v(i) = num(t(i)%s)
    end do
  end subroutine

  subroutine get_ints8(name, v)
    character(*), intent(in) :: name
    integer(8), allocatable, intent(out) :: v(:)
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i
    r = find(name, 'v', 'a vector of integers')
    call need_whole(name, r, 'a vector of integers')
    t = tokens(r%data)
    allocate (v(size(t)))
    do i = 1, size(t)
      v(i) = whole(t(i)%s)
    end do
  end subroutine

  subroutine get_ints4(name, v)
    character(*), intent(in) :: name
    integer(4), allocatable, intent(out) :: v(:)
    integer(8), allocatable :: w(:)
    call get_ints8(name, w)
    v = int(w, 4)
  end subroutine

  subroutine get_texts(name, v)
    character(*), intent(in) :: name
    type(signet_text), allocatable, intent(out) :: v(:)
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i
    r = find(name, 'v', 'a vector of text')
    if (r%kind /= 'vector::text') call fail('input ' // name // ' is ' // r%kind // ', not a vector of text')
    t = tokens(r%data)
    allocate (v(size(t)))
    do i = 1, size(t)
      v(i)%s = unescape(t(i)%s)
    end do
  end subroutine

  subroutine get_complexes(name, v)
    character(*), intent(in) :: name
    complex(8), allocatable, intent(out) :: v(:)
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i
    r = find(name, 'vc', 'a vector of complex numbers')
    t = tokens(r%data)
    allocate (v(size(t) / 2))
    do i = 1, size(v)
      v(i) = cmplx(num(t(2 * i - 1)%s), num(t(2 * i)%s), 8)
    end do
  end subroutine

  subroutine get_series(name, s)
    character(*), intent(in) :: name
    type(signet_series), intent(out) :: s
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i, n
    r = find(name, 'xs', 'a real series')
    t = tokens(r%data)
    n = int(whole(t(2)%s))
    s%domain = t(1)%s
    allocate (s%x(n), s%values(n))
    do i = 1, n
      s%x(i) = num(t(2 + i)%s)
      s%values(i) = num(t(2 + n + i)%s)
    end do
  end subroutine

  subroutine get_cseries(name, s)
    character(*), intent(in) :: name
    type(signet_cseries), intent(out) :: s
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    integer :: i, n
    r = find(name, 'xc', 'a complex series')
    t = tokens(r%data)
    n = int(whole(t(2)%s))
    s%domain = t(1)%s
    allocate (s%x(n), s%values(n))
    do i = 1, n
      s%x(i) = num(t(2 + i)%s)
      s%values(i) = cmplx(num(t(2 + n + 2 * i - 1)%s), num(t(2 + n + 2 * i)%s), 8)
    end do
  end subroutine

  ! ---- outputs: JSON text ----

  function jnum(x) result(s)
    real(8), intent(in) :: x
    character(:), allocatable :: s
    character(64) :: buf
    character(24) :: fmt
    real(8) :: y
    integer :: p, last
    logical :: plain
    if (x /= x .or. abs(x) > huge(x)) then
      s = 'null'
      return
    end if
    plain = x == 0 .or. (abs(x) >= 1.0d-4 .and. abs(x) < 1.0d15)
    last = merge(24, 16, plain)
    do p = 1, last
      if (plain) then
        write (fmt, '("(F40.",I0,")")') p
      else
        write (fmt, '("(ES40.",I0,"E3)")') p
      end if
      write (buf, fmt) x
      read (buf, *) y
      if (y == x) exit
    end do
    s = trim(adjustl(buf))
  end function

  function jstr(t) result(s)
    character(*), intent(in) :: t
    character(:), allocatable :: s
    character(6) :: u
    integer :: i, c
    s = '"'
    do i = 1, len(t)
      c = iachar(t(i:i))
      select case (c)
      case (34); s = s // '\"'
      case (92); s = s // '\\'
      case (10); s = s // '\n'
      case (13); s = s // '\r'
      case (9); s = s // '\t'
      case default
        if (c < 32) then
          write (u, '("\u00",Z2.2)') c
          s = s // u
        else
          s = s // t(i:i)
        end if
      end select
    end do
    s = s // '"'
  end function

  logical function floaty(vtype)
    character(*), intent(in) :: vtype
    integer :: n
    n = len(vtype)
    floaty = n >= 5
    if (floaty) floaty = vtype(n - 4:n) == 'float'
  end function

  function jint(v, vtype) result(s)
    integer(8), intent(in) :: v
    character(*), intent(in) :: vtype
    character(:), allocatable :: s
    character(24) :: buf
    if (floaty(vtype)) then
      s = jnum(real(v, 8))
    else
      write (buf, '(I0)') v
      s = trim(buf)
    end if
  end function

  function jreals(v) result(s)
    real(8), intent(in) :: v(:)
    character(:), allocatable :: s
    integer :: i
    s = '['
    do i = 1, size(v)
      if (i > 1) s = s // ','
      s = s // jnum(v(i))
    end do
    s = s // ']'
  end function

  function jcomplexes(v) result(s)
    complex(8), intent(in) :: v(:)
    character(:), allocatable :: s
    s = '{"re":' // jreals(real(v, 8)) // ',"im":' // jreals(aimag(v)) // '}'
  end function

  function own(vtype, dflt) result(s)
    character(*), intent(in) :: vtype, dflt
    character(:), allocatable :: s
    if (len(vtype) == 0) then
      s = dflt
    else
      s = vtype
    end if
  end function

  ! <prefix>-<module>.<name>.sig, written whole and then renamed: a reader never sees half a file
  subroutine emit(name, value, vtype)
    character(*), intent(in) :: name, value, vtype
    character(:), allocatable :: module, prefix, path, tmp
    integer :: u, ios
    module = env('MODULE')
    prefix = env('PREFIX')
    if (len(module) == 0) call fail('SIGNET_MODULE is not set (run from Signet or Starfire)')
    path = module // '.' // name // '.sig'
    if (len(prefix) > 0) path = prefix // '-' // path
    tmp = path // '.tmp'
    open (newunit=u, file=tmp, status='replace', action='write', iostat=ios)
    if (ios /= 0) call fail("can't write " // tmp)
    write (u, '(A)') '{"value":' // value // ',"type":' // jstr(vtype) // '}'
    close (u)
    if (c_rename(tmp // c_null_char, path // c_null_char) /= 0) call fail("can't rename " // tmp)
  end subroutine

  subroutine put_real(name, v, vtype)
    character(*), intent(in) :: name
    real(8), intent(in) :: v
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'float'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, jnum(v), t)
  end subroutine

  subroutine put_real4(name, v, vtype)
    character(*), intent(in) :: name
    real(4), intent(in) :: v
    character(*), intent(in), optional :: vtype
    if (present(vtype)) then
      call put_real(name, real(v, 8), vtype)
    else
      call put_real(name, real(v, 8))
    end if
  end subroutine

  subroutine put_int8(name, v, vtype)
    character(*), intent(in) :: name
    integer(8), intent(in) :: v
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'int'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, jint(v, t), t)
  end subroutine

  subroutine put_int4(name, v, vtype)
    character(*), intent(in) :: name
    integer(4), intent(in) :: v
    character(*), intent(in), optional :: vtype
    if (present(vtype)) then
      call put_int8(name, int(v, 8), vtype)
    else
      call put_int8(name, int(v, 8))
    end if
  end subroutine

  subroutine put_logical(name, v, vtype)
    character(*), intent(in) :: name
    logical, intent(in) :: v
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'bool'
    if (present(vtype)) t = own(vtype, t)
    if (v) then
      call emit(name, 'true', t)
    else
      call emit(name, 'false', t)
    end if
  end subroutine

  subroutine put_text(name, v, vtype)
    character(*), intent(in) :: name, v
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'text'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, jstr(v), t)
  end subroutine

  subroutine put_complex(name, v, vtype)
    character(*), intent(in) :: name
    complex(8), intent(in) :: v
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'complex'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, '{"re":' // jnum(real(v, 8)) // ',"im":' // jnum(aimag(v)) // '}', t)
  end subroutine

  subroutine put_reals(name, v, vtype)
    character(*), intent(in) :: name
    real(8), intent(in) :: v(:)
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'vector::float'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, jreals(v), t)
  end subroutine

  subroutine put_ints8(name, v, vtype)
    character(*), intent(in) :: name
    integer(8), intent(in) :: v(:)
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t, s
    integer :: i
    t = 'vector::int'
    if (present(vtype)) t = own(vtype, t)
    s = '['
    do i = 1, size(v)
      if (i > 1) s = s // ','
      s = s // jint(v(i), t)
    end do
    call emit(name, s // ']', t)
  end subroutine

  subroutine put_ints4(name, v, vtype)
    character(*), intent(in) :: name
    integer(4), intent(in) :: v(:)
    character(*), intent(in), optional :: vtype
    if (present(vtype)) then
      call put_ints8(name, int(v, 8), vtype)
    else
      call put_ints8(name, int(v, 8))
    end if
  end subroutine

  subroutine put_texts(name, v, vtype)
    character(*), intent(in) :: name
    type(signet_text), intent(in) :: v(:)
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t, s
    integer :: i
    t = 'vector::text'
    if (present(vtype)) t = own(vtype, t)
    s = '['
    do i = 1, size(v)
      if (i > 1) s = s // ','
      s = s // jstr(v(i)%s)
    end do
    call emit(name, s // ']', t)
  end subroutine

  subroutine put_complexes(name, v, vtype)
    character(*), intent(in) :: name
    complex(8), intent(in) :: v(:)
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'vector::complex'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, jcomplexes(v), t)
  end subroutine

  subroutine put_series(name, s, vtype)
    character(*), intent(in) :: name
    type(signet_series), intent(in) :: s
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'series::' // s%domain // '::float'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, '{"domain":' // jstr(s%domain) // ',"x":' // jreals(s%x) // ',"values":' // jreals(s%values) // '}', t)
  end subroutine

  subroutine put_cseries(name, s, vtype)
    character(*), intent(in) :: name
    type(signet_cseries), intent(in) :: s
    character(*), intent(in), optional :: vtype
    character(:), allocatable :: t
    t = 'series::' // s%domain // '::complex'
    if (present(vtype)) t = own(vtype, t)
    call emit(name, '{"domain":' // jstr(s%domain) // ',"x":' // jreals(s%x) // ',"values":' // jcomplexes(s%values) // '}', t)
  end subroutine

  subroutine signet_stream_open(s, name, domain, window)
    type(signet_stream), intent(out) :: s
    character(*), intent(in) :: name
    character(*), intent(in), optional :: domain
    integer, intent(in), optional :: window
    s%name = name
    s%domain = 'time'
    if (present(domain)) s%domain = domain
    if (present(window)) s%window = window
    if (s%window < 1) call fail("a stream's window must be above 0")
    allocate (s%x(s%window), s%values(s%window), s%cvalues(s%window))
  end subroutine

  function inum(n) result(s)
    integer, intent(in) :: n
    character(:), allocatable :: s
    character(20) :: b
    write (b, '(I0)') n
    s = trim(b)
  end function

  integer function stream_slot(s, t) result(n)   ! makes room for one more sample; its index
    type(signet_stream), intent(inout) :: s
    real(8), intent(in) :: t
    if (.not. allocated(s%x)) call fail('open a stream with signet_stream_open before writing it')
    if (t /= t .or. abs(t) > huge(t)) call fail("a stream's t must be a finite number")
    s%count = s%count + 1
    if (s%count > s%window) then            ! the oldest sample falls out of the window
      s%x(1:s%window - 1) = s%x(2:s%window)
      s%values(1:s%window - 1) = s%values(2:s%window)
      s%cvalues(1:s%window - 1) = s%cvalues(2:s%window)
    end if
    n = min(s%count, s%window)
    s%x(n) = t
  end function

  function stream_json(s, n, values) result(js)
    type(signet_stream), intent(in) :: s
    integer, intent(in) :: n
    character(*), intent(in) :: values
    character(:), allocatable :: js
    js = '{"domain":' // jstr(s%domain) // ',"x":' // jreals(s%x(1:n)) // ',"values":' // values // &
         ',"window":' // inum(s%window) // ',"count":' // inum(s%count) // ',"ended":false}'
  end function

  subroutine stream_write_real(s, t, v)
    type(signet_stream), intent(inout) :: s
    real(8), intent(in) :: t, v
    integer :: n
    n = stream_slot(s, t)
    s%values(n) = v
    call emit(s%name, stream_json(s, n, jreals(s%values(1:n))), 'stream::' // s%domain // '::float')
  end subroutine

  subroutine stream_write_complex(s, t, v)
    type(signet_stream), intent(inout) :: s
    real(8), intent(in) :: t
    complex(8), intent(in) :: v
    integer :: n
    n = stream_slot(s, t)
    s%cvalues(n) = v
    call emit(s%name, stream_json(s, n, jcomplexes(s%cvalues(1:n))), 'stream::' // s%domain // '::complex')
  end subroutine

  ! A stream's .sig, read without a JSON library
  integer function key_at(s, k) result(at)
    character(*), intent(in) :: s, k
    integer :: p, from, i
    at = 0
    from = 1
    do
      p = index(s(from:), '"' // k // '"')
      if (p == 0) return
      i = from + p - 1 + len(k) + 2
      do while (i <= len(s))
        if (s(i:i) /= ' ') exit
        i = i + 1
      end do
      if (i <= len(s)) then
        if (s(i:i) == ':') then
          i = i + 1
          do while (i <= len(s))
            if (s(i:i) /= ' ') exit
            i = i + 1
          end do
          at = i
          return
        end if
      end if
      from = from + p
    end do
  end function

  subroutine numbers(s, at, out)
    character(*), intent(in) :: s
    integer, intent(in) :: at
    real(8), allocatable, intent(out) :: out(:)
    integer :: stop, k, start, j, n
    character(:), allocatable :: tok
    allocate (out(0))
    if (at == 0 .or. at > len(s)) return
    if (s(at:at) /= '[') return
    stop = at + index(s(at:), ']') - 1
    if (len_trim(adjustl(s(at + 1:stop - 1))) == 0) return
    n = 1
    do j = at + 1, stop - 1
      if (s(j:j) == ',') n = n + 1
    end do
    deallocate (out)
    allocate (out(n))
    k = 0
    start = at + 1
    do j = at + 1, stop
      if (j == stop .or. s(j:j) == ',') then
        k = k + 1
        tok = trim(adjustl(s(start:j - 1)))
        if (tok == 'null') then
          out(k) = transfer(-2251799813685248_8, out(k))
        else
          out(k) = num(tok)
        end if
        start = j + 1
      end if
    end do
  end subroutine

  subroutine read_stream(path, x, v, count, ended)
    character(*), intent(in) :: path
    real(8), allocatable, intent(out) :: x(:), v(:)
    integer(8), intent(out) :: count
    logical, intent(out) :: ended
    character(:), allocatable :: s
    integer :: u, ios, vi, ci, ei, last
    s = ''
    open (newunit=u, file=path, status='old', action='read', iostat=ios)
    if (ios == 0) then
      call read_line(u, s, ios)
      close (u)
    end if
    vi = key_at(s, 'values')
    if (vi > 0) then
      if (s(vi:vi) == '{') call fail(path // ' is a complex stream: read it in Python or Julia')
    end if
    call numbers(s, key_at(s, 'x'), x)
    call numbers(s, vi, v)
    count = size(x)
    ended = .false.
    if (size(x) /= size(v)) then               ! caught between writes: try again
      deallocate (x, v)
      allocate (x(0), v(0))
      count = 0
      return
    end if
    ci = key_at(s, 'count')
    if (ci > 0) then
      last = ci
      do while (last <= len(s))
        if (index('0123456789', s(last:last)) == 0) exit
        last = last + 1
      end do
      if (last > ci) read (s(ci:last - 1), *) count
    end if
    ei = key_at(s, 'ended')
    if (ei > 0) ended = s(ei:ei) == 't'
  end subroutine

  subroutine signet_get_stream(name, s)
    character(*), intent(in) :: name
    type(signet_stream_in), intent(out) :: s
    type(rec) :: r
    type(signet_text), allocatable :: t(:)
    r = find(name, 'st', 'a stream')
    t = tokens(r%data)
    if (size(t) /= 1) call fail('input ' // name // ' merges streams: read it in Python, Julia, C++ or Rust')
    s%path = unescape(t(1)%s)
    s%seen = 0
  end subroutine

  logical function signet_stream_next(s, t, v) result(got)
    type(signet_stream_in), intent(inout) :: s
    real(8), intent(out) :: t, v
    real(8), allocatable :: x(:), y(:)
    integer(8) :: count, behind
    logical :: ended
    integer :: i, rc
    t = 0
    v = 0
    do
      call read_stream(s%path, x, y, count, ended)
      if (count > s%seen .and. size(x) > 0) then
        behind = min(count - s%seen, int(size(x), 8))
        i = size(x) - int(behind) + 1
        t = x(i)
        v = y(i)
        s%seen = count - behind + 1
        got = .true.
        return
      end if
      if (ended) then
        got = .false.
        return
      end if
      rc = c_usleep(20000)
    end do
  end function

  logical function signet_stream_latest(s, t, v) result(got)
    type(signet_stream_in), intent(in) :: s
    real(8), intent(out) :: t, v
    real(8), allocatable :: x(:), y(:)
    integer(8) :: count
    logical :: ended
    t = 0
    v = 0
    call read_stream(s%path, x, y, count, ended)
    got = size(x) > 0
    if (got) then
      t = x(size(x))
      v = y(size(y))
    end if
  end function

  logical function signet_stream_ended(s)
    type(signet_stream_in), intent(in) :: s
    real(8), allocatable :: x(:), y(:)
    integer(8) :: count
    call read_stream(s%path, x, y, count, signet_stream_ended)
  end function

end module signet
'''

_SH_HELPER = r'''# signet.sh:  SIGNALS FOR SHELL MODULES; source "$SIGNET_HELPERS/signet.sh"
# Reads inputs from $SIGNET_SIGNALS_TXT and writes outputs as <prefix>-<module>.<port>.sig.

_signet_unescape='function u(s,  o, i, c, d) { o = ""; for (i = 1; i <= length(s); i++) { c = substr(s, i, 1);
  if (c == "\\") { d = substr(s, ++i, 1); o = o (d == "s" ? " " : d == "t" ? "\t" : d == "n" ? "\n" : d == "r" ? "\r" : d == "e" ? "" : d) } else o = o c } return o }'

_signet_row() {          # _signet_row NAME: the input'"'"'s tab-separated line, or an error
  if [ -z "${SIGNET_SIGNALS_TXT:-}" ] || [ ! -f "$SIGNET_SIGNALS_TXT" ]; then
    echo "signet: this run has no inputs" >&2; return 2
  fi
  LC_ALL=C awk -F'\t' -v n="$1" '$1 == n { print; found = 1; exit } END { exit !found }' "$SIGNET_SIGNALS_TXT" \
    || { echo "signet: no input $1" >&2; return 2; }
}

signet_has() {           # signet_has NAME: succeeds if the input exists
  [ -n "${SIGNET_SIGNALS_TXT:-}" ] && [ -f "$SIGNET_SIGNALS_TXT" ] \
    && LC_ALL=C awk -F'\t' -v n="$1" '$1 == n { f = 1 } END { exit !f }' "$SIGNET_SIGNALS_TXT"
}

signet_type() {          # signet_type NAME: its type, such as float or vector::int
  local row; row=$(_signet_row "$1") || return 2
  printf '%s\n' "$row" | LC_ALL=C awk -F'\t' '{ print $2 }'
}

signet_domain() {        # signet_domain NAME: the axis name of a series
  local row; row=$(_signet_row "$1") || return 2
  printf '%s\n' "$row" | LC_ALL=C awk -F'\t' '{ split($4, t, " "); print t[1] }'
}

signet_get() {           # signet_get NAME: one value per line
  local row; row=$(_signet_row "$1") || return 2
  printf '%s\n' "$row" | LC_ALL=C awk -F'\t' "$_signet_unescape"'
    { n = split($4, t, " ")
      if ($3 == "s" || $3 == "v") for (i = 1; i <= n; i++) print u(t[i])
      else if ($3 == "c" || $3 == "vc") for (i = 1; i < n; i += 2) print t[i], t[i + 1]
      else if ($3 == "xs") { k = t[2]; for (i = 1; i <= k; i++) print t[2 + i], t[2 + k + i] }
      else if ($3 == "xc") { k = t[2]; for (i = 1; i <= k; i++) print t[2 + i], t[2 + k + 2 * i - 1], t[2 + k + 2 * i] }
      else print u($4) }'
}

_signet_path() {         # <prefix>-<module>.<port>.sig, or <module>.<port>.sig without a prefix
  [ -n "${SIGNET_MODULE:-}" ] || { echo "signet: SIGNET_MODULE is not set (run from Signet or Starfire)" >&2; return 2; }
  case "$1" in ''|[0-9]*|*[!A-Za-z0-9_]*|write|series|stream) echo "signet: '$1' can't be a port name" >&2; return 2 ;; esac
  if [ -n "${SIGNET_PREFIX:-}" ]; then echo "${SIGNET_PREFIX}-${SIGNET_MODULE}.$1.sig"; else echo "${SIGNET_MODULE}.$1.sig"; fi
}

_signet_json_text() {    # a JSON string from its arguments joined as given
  LC_ALL=C awk 'BEGIN { s = ARGV[1]; o = "\"";
    for (i = 1; i <= length(s); i++) { c = substr(s, i, 1)
      if (c == "\\") o = o "\\\\"; else if (c == "\"") o = o "\\\""; else if (c == "\n") o = o "\\n"
      else if (c == "\t") o = o "\\t"; else if (c == "\r") o = o "\\r"; else o = o c }
    printf "%s\"", o }' "$1"
}

_signet_json_num() {     # _signet_json_num VALUE TYPE: a JSON number, null for nan; int or float
  case "$1" in
    nan|NaN|NAN|null) echo null; return ;;
  esac
  if ! printf '%s' "$1" | LC_ALL=C grep -Eq '^-?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$'; then
    echo "signet: '$1' isn't a number" >&2; return 2
  fi
  LC_ALL=C awk -v v="$1" -v t="$2" 'BEGIN {
    if (t == "int") { if (v !~ /^-?[0-9]+$/) { print "signet: " v " isn'"'"'t a whole number" > "/dev/stderr"; exit 2 } printf "%s", v }
    else { x = v + 0; s = sprintf("%.17g", x); if (sprintf("%.15g", x) + 0 == x) s = sprintf("%.15g", x)
           if (s !~ /[.eE]/) s = s ".0"; printf "%s", s } }'
}

_signet_scalar() {       # _signet_scalar VALUE TYPE: one JSON value of a scalar type
  case "$2" in
    int|float|num|real) _signet_json_num "$1" "$2" ;;
    bool) case "$1" in true|false) printf '%s' "$1" ;; *) echo "signet: '$1' isn't true or false" >&2; return 2 ;; esac ;;
    text) _signet_json_text "$1" ;;
    *) echo "signet: can't write type $2 from the shell" >&2; return 2 ;;
  esac
}

_signet_write() {        # _signet_write NAME JSON TYPE
  local path; path=$(_signet_path "$1") || return 2
  printf '{"value": %s, "type": "%s"}\n' "$2" "$3" > "$path.tmp" && mv "$path.tmp" "$path"
}

signet_put() {           # signet_put NAME VALUE [TYPE]: a bool, int, float or text output
  local name=$1 value=$2 type=${3:-} js
  if [ -z "$type" ]; then
    if [ "$value" = true ] || [ "$value" = false ]; then type=bool
    elif printf '%s' "$value" | LC_ALL=C grep -Eq '^-?[0-9]+$'; then type=int
    elif printf '%s' "$value" | LC_ALL=C grep -Eq '^-?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$'; then type=float
    else type=text; fi
  fi
  js=$(_signet_scalar "$value" "$type") || return 2
  _signet_write "$name" "$js" "$type"
}

signet_put_vector() {    # signet_put_vector NAME TYPE VALUE...: a vector of int, float, bool or text
  local name=$1 type=$2 js="" v item; shift 2
  for v in "$@"; do
    item=$(_signet_scalar "$v" "$type") || return 2
    js="${js:+$js, }$item"
  done
  _signet_write "$name" "[$js]" "vector::$type"
}

signet_put_complex() {   # signet_put_complex NAME RE IM
  local re im; re=$(_signet_json_num "$2" float) && im=$(_signet_json_num "$3" float) || return 2
  _signet_write "$1" "{\"re\": $re, \"im\": $im}" complex
}

signet_put_series() {    # signet_put_series NAME DOMAIN: reads "x value" lines on standard input
  local name=$1 domain=$2 xs="" vs="" x v xi vi
  case "$domain" in ''|[0-9]*|*[!A-Za-z0-9_]*) echo "signet: '$domain' can't be a domain" >&2; return 2 ;; esac
  while read -r x v; do
    [ -n "$x" ] || continue
    xi=$(_signet_json_num "$x" float) && vi=$(_signet_json_num "$v" float) || return 2
    xs="${xs:+$xs, }$xi"; vs="${vs:+$vs, }$vi"
  done
  _signet_write "$name" "{\"domain\": \"$domain\", \"x\": [$xs], \"values\": [$vs]}" "series::$domain::float"
}

signet_stream() {        # signet_stream NAME [DOMAIN] [WINDOW]: reads "t value" lines on standard input and writes
                         # the stream after each, keeping the last WINDOW samples (1048)
  local name=$1 domain=${2:-time} window=${3:-1048} count=0 t v ti vi xs vs
  local -a X=() V=()
  case "$domain" in ''|[0-9]*|*[!A-Za-z0-9_]*) echo "signet: '$domain' can't be a domain" >&2; return 2 ;; esac
  case "$window" in ''|*[!0-9]*|0) echo "signet: a stream's window must be a whole number above 0" >&2; return 2 ;; esac
  _signet_path "$name" >/dev/null || return 2
  while read -r t v; do
    [ -n "$t" ] || continue
    ti=$(_signet_json_num "$t" float) && vi=$(_signet_json_num "$v" float) || return 2
    [ "$ti" != null ] || { echo "signet: a stream's t must be a number" >&2; return 2; }
    X+=("$ti"); V+=("$vi"); count=$((count + 1))
    if [ ${#X[@]} -gt "$window" ]; then X=("${X[@]:1}"); V=("${V[@]:1}"); fi
    xs=$(IFS=,; printf '%s' "${X[*]}"); vs=$(IFS=,; printf '%s' "${V[*]}")
    _signet_write "$name" "{\"domain\": \"$domain\", \"x\": [$xs], \"values\": [$vs], \"window\": $window, \"count\": $count, \"ended\": false}" \
      "stream::$domain::float" || return 2
  done
}

# A stream's .sig, read without a JSON library: "count ended" on the first line, then "t value" for each sample kept
_signet_stream_awk='function key(s, k,   p, r) {
    while ((p = index(s, "\"" k "\"")) > 0) {
      r = substr(s, p + length(k) + 2); sub(/^ */, "", r)
      if (substr(r, 1, 1) == ":") { r = substr(r, 2); sub(/^ */, "", r); return r }
      s = substr(s, p + 1) }
    return "" }
  function list(r, out,   e, body) {
    if (substr(r, 1, 1) != "[") return 0
    e = index(r, "]"); body = substr(r, 2, e - 2)
    return body ~ /^ *$/ ? 0 : split(body, out, ",") }
  { s = s $0 }
  END {
    vr = key(s, "values"); if (substr(vr, 1, 1) == "{") exit 3
    nx = list(key(s, "x"), X); nv = list(vr, V)
    if (nx != nv) { print "0 0"; exit }
    c = key(s, "count"); e = key(s, "ended")
    print (c == "" ? nx : c + 0), (substr(e, 1, 1) == "t" ? 1 : 0)
    for (i = 1; i <= nx; i++) { gsub(/ /, "", X[i]); gsub(/ /, "", V[i]); print X[i], (V[i] == "null" ? "nan" : V[i]) } }'

_signet_stream_path() {  # the .sig a stream input is read from
  local row path; row=$(_signet_row "$1") || return 2
  path=$(printf '%s\n' "$row" | LC_ALL=C awk -F'\t' "$_signet_unescape"'
    $3 == "st" { if (split($4, t, " ") != 1) exit 3; print u(t[1]); exit } { exit 4 }')
  case $? in
    0) printf '%s\n' "$path" ;;
    3) echo "signet: input $1 merges streams: read it in Python, Julia, C++ or Rust" >&2; return 2 ;;
    *) echo "signet: input $1 isn't a stream" >&2; return 2 ;;
  esac
}

_signet_stream_read() {
  [ -f "$1" ] || { echo "0 0"; return 0; }
  LC_ALL=C awk "$_signet_stream_awk" "$1" || { echo "signet: $1 is a complex stream: read it in Python or Julia" >&2; return 2; }
}

signet_stream_latest() { # signet_stream_latest NAME: "t value" of the newest sample, nothing before the first
  local p out; p=$(_signet_stream_path "$1") || return 2
  out=$(_signet_stream_read "$p") || return 2
  printf '%s\n' "$out" | tail -n +2 | tail -n 1
}

signet_stream_ended() {  # signet_stream_ended NAME: succeeds once the module writing it has ended
  local p out; p=$(_signet_stream_path "$1") || return 2
  out=$(_signet_stream_read "$p") || return 2
  [ "$(printf '%s\n' "$out" | head -n 1 | cut -d' ' -f2)" = 1 ]
}

signet_stream_follow() { # signet_stream_follow NAME: prints "t value" for each sample as it arrives, until its module ends
  local p out head count ended rows n seen=0
  p=$(_signet_stream_path "$1") || return 2
  while :; do
    out=$(_signet_stream_read "$p") || return 2
    head=$(printf '%s\n' "$out" | head -n 1)
    count=${head% *}; ended=${head#* }
    rows=$(printf '%s\n' "$out" | tail -n +2)
    if [ "$count" -gt "$seen" ] && [ -n "$rows" ]; then
      n=$((count - seen))
      printf '%s\n' "$rows" | tail -n "$n"
      seen=$count
    elif [ "$ended" = 1 ]; then
      return 0
    else
      sleep 0.02
    fi
  done
}
'''

_HELPER_FILES = lambda: {os.path.join("signet", "__init__.py"): _PY_HELPER, "Signet.jl": _JL_HELPER, "signet.hpp": _CPP_HELPER,
                         "signet.rs": _RS_HELPER, "signet.ads": _ADS_HELPER, "signet.adb": _ADB_HELPER, "signet.f90": _F90_HELPER,
                         "signet.sh": _SH_HELPER}
_HELPER_DIR = None


def _cache_base() -> str:
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "signet")


def helper_dir() -> str:
    """The folder holding the helpers for every language, unpacked once per helper version under the
    user's cache; "" if it can't be written."""
    global _HELPER_DIR
    if _HELPER_DIR is not None:
        return _HELPER_DIR
    files = _HELPER_FILES()
    tag = hashlib.sha256("".join(files.values()).encode()).hexdigest()[:12]
    d = os.path.join(_cache_base(), f"helpers-{tag}")
    try:
        if not os.path.isfile(os.path.join(d, "signet.f90")):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            tmp = f"{d}.{os.getpid()}.tmp"
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                    f.write(text)
            try:
                os.rename(tmp, d)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)   # another run unpacked it first
        _HELPER_DIR = d if os.path.isfile(os.path.join(d, "signet.f90")) else ""
    except OSError:
        _HELPER_DIR = ""
    return _HELPER_DIR


def export_helpers(dest: str) -> list:
    """Write every helper into `dest` (to read them, or to build a module without Signet); returns the paths."""
    out = []
    for rel, text in _HELPER_FILES().items():
        p = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        out.append(p)
    return out


# Modules written in compiled languages: Signet builds <module>.cpp, .rs, .adb or .f90 itself, with the
# helper above on the compiler's search path, and keeps the binary in the cache until the source changes.
SOURCE_TYPES = {".cpp": "cpp", ".cc": "cpp", ".rs": "rust", ".adb": "ada",
                ".f90": "fortran", ".f95": "fortran", ".f03": "fortran", ".f08": "fortran"}
_COMPILERS = {"cpp": ("g++", "c++", "clang++"), "rust": ("rustc",), "ada": ("gnatmake",), "fortran": ("gfortran",)}
_BUILD_TIMEOUT = 600          # seconds for one compile
_NEIGHBOURS = (".h", ".hpp", ".hh", ".ads", ".adb", ".rs", ".inc") + tuple(SOURCE_TYPES)
_TOOL_IDS = {}


def _tool_id(cc: str) -> str:
    """A compiler's own version text, so a new compiler rebuilds what the old one built."""
    if cc not in _TOOL_IDS:
        try:
            p = subprocess.run([cc, "-vV" if os.path.basename(cc) == "rustc" else "--version"], capture_output=True,
                               text=True, timeout=30)
            _TOOL_IDS[cc] = cc + " " + p.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _TOOL_IDS[cc] = cc
    return _TOOL_IDS[cc]


def _build(cmd: list, cwd: str, what: str) -> None:
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=_BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out after {_BUILD_TIMEOUT} s")
    except OSError as e:
        raise RuntimeError(f"{what} failed: {e}")
    if p.returncode != 0:
        raise RuntimeError(f"{what} failed:\n" + "\n".join(p.stdout.strip().splitlines()[-25:]))


def _support_lib(lang: str, cc: str, helpers: str) -> str:
    """The helper compiled once for Rust (an rlib) or Fortran (a module and object); returns its folder."""
    tag = hashlib.sha256((os.path.basename(helpers) + _tool_id(cc)).encode()).hexdigest()[:12]
    d = os.path.join(_cache_base(), f"lib-{lang}-{tag}")
    done = os.path.join(d, "libsignet.rlib" if lang == "rust" else "signet.o")
    if os.path.isfile(done):
        return d
    os.makedirs(_cache_base(), exist_ok=True)
    tmp = tempfile.mkdtemp(dir=_cache_base(), prefix="build-")
    try:
        if lang == "rust":
            _build([cc, "--edition", "2021", "-O", "--crate-type", "rlib", "--crate-name", "signet",
                    os.path.join(helpers, "signet.rs"), "-o", os.path.join(tmp, "libsignet.rlib")], tmp, "building the Rust helper")
        else:
            _build([cc, "-O2", "-c", "-J", tmp, os.path.join(helpers, "signet.f90"), "-o", os.path.join(tmp, "signet.o")],
                   tmp, "building the Fortran helper")
        try:
            os.rename(tmp, d)
        except OSError:
            pass                  # another run built it first
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return d


def build_module(path: str, log=None) -> list:
    """argv that runs a module given as a source file (see SOURCE_TYPES), building it first if needed.
    Raises LookupError if the compiler isn't installed and RuntimeError with the compiler's messages if the
    build fails. `log(text)` is told when a build starts."""
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    lang = SOURCE_TYPES.get(ext.lower())
    if lang is None:
        raise LookupError(f"{name} is not a source file Signet builds ({', '.join(SOURCE_TYPES)})")
    cc = next((w for w in map(shutil.which, _COMPILERS[lang]) if w), None)
    if not cc:
        raise LookupError(f"{_COMPILERS[lang][0]} not found on PATH (needed to build {name})")
    helpers = helper_dir()
    if not helpers:
        raise RuntimeError(f"can't unpack the Signet helpers: {_cache_base()} is not writable")
    path = os.path.abspath(path)
    srcdir = os.path.dirname(path)
    h = hashlib.sha256()
    for part in (lang, os.path.basename(helpers), _tool_id(cc)):
        h.update(part.encode() + b"\0")
    with open(path, "rb") as f:
        h.update(f.read())
    for n in sorted(os.listdir(srcdir)):          # files built from the same folder count too
        if n != name and n.lower().endswith(_NEIGHBOURS) and os.path.isfile(os.path.join(srcdir, n)):
            st = os.stat(os.path.join(srcdir, n))
            h.update(f"{n} {st.st_size} {st.st_mtime_ns}".encode())
    base = _cache_base()
    out = os.path.join(base, "bin", f"{stem}-{h.hexdigest()[:16]}")
    if os.access(out, os.X_OK):
        return [out]
    os.makedirs(os.path.join(base, "bin"), exist_ok=True)
    if log:
        log(f"building {name}")
    lib = _support_lib(lang, cc, helpers) if lang in ("rust", "fortran") else ""
    tmp = tempfile.mkdtemp(dir=base, prefix="build-")
    try:
        exe = os.path.join(tmp, "module")
        if lang == "cpp":
            cmd = [cc, "-O2", "-std=c++17", "-I", helpers, "-I", srcdir, path, "-o", exe]
        elif lang == "rust":
            crate = re.sub(r"\W", "_", stem)
            cmd = [cc, "--edition", "2021", "-O", "--crate-name", crate if not crate[:1].isdigit() else "m_" + crate,
                   "--extern", "signet=" + os.path.join(lib, "libsignet.rlib"), path, "-o", exe]
        elif lang == "ada":
            cmd = [cc, "-q", "-O2", "-gnat2012", "-I" + helpers, "-I" + srcdir, path, "-o", exe]
        else:
            cmd = [cc, "-O2", "-ffree-line-length-none", "-I", lib, "-J", tmp, path, os.path.join(lib, "signet.o"), "-o", exe]
        _build(cmd, tmp, f"building {name}")
        os.replace(exe, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for n in os.listdir(os.path.dirname(out)):    # the older builds of this module
        if re.fullmatch(re.escape(stem) + r"-[0-9a-f]{16}", n) and n != os.path.basename(out):
            try:
                os.remove(os.path.join(os.path.dirname(out), n))
            except OSError:
                pass
    return [out]


# The standalone runner: python3 signet.py runner.signet
_INTERPRETERS = {".py": "python3", ".sh": "bash", ".jl": "julia"}


# Ports: what a module declares it reads and writes, and the wiring checked against it
_PORT_LINE = re.compile(r"^[ \t]*(?:#|//|--|!)[ \t]*signet:[ \t]*(.*)$", re.M | re.I)
_PORT_ITEM = re.compile(r"(in|out)\s+((?:[A-Za-z0-9_]+::)*)([A-Za-z_][A-Za-z0-9_]*)$", re.I)


def read_ports(text: str):
    """A module's declared ports from its `signet: in float::volts, out float::watts` comment lines
    (after #, //, -- or !): {"in": {name: words}, "out": {name: words}}, or None if it declares none.
    Raises ValueError for a bad declaration."""
    lines = _PORT_LINE.findall(text)
    if not lines:
        return None
    ports = {"in": {}, "out": {}}
    for line in lines:
        for item in (s.strip() for s in line.split(",")):
            m = _PORT_ITEM.match(item)
            if not m:
                raise ValueError(f"bad port declaration {item!r}: expected in <name> or out <name>, with an optional type")
            way, words, name = m.group(1).lower(), [w for w in m.group(2).split("::") if w], m.group(3)
            if name in ports["in"] or name in ports["out"]:
                raise ValueError(f"port {name} is declared twice")
            if name in _RESERVED_PORTS:
                raise ValueError(f"{name} can't be a port name")
            ports[way][name] = words
    return ports


_FITS = {"hex": {"hex"}, "int": {"int", "hex"}, "float": {"float", "real", "int", "hex"},
         "real": {"float", "real", "int", "hex"}, "num": {"float", "real", "int", "hex", "num", "complex"},
         "bool": {"bool"}, "complex": {"complex"}, "bus": {"bus"}, "stream": {"stream"}, "tuple": {"tuple"}, "series": {"series"},
         "bits": {"bits"}, "range": {"range"}}


def _fits(src, dst) -> bool:
    """Whether a value declared as `src` can go where `dst` is declared; unknown pairs pass."""
    if not src or not dst or dst == ["text"]:
        return True
    if src[0] == dst[0] == "vector":
        return _fits(src[1:], dst[1:])
    if dst[0] in _FITS and len(src) == 1 and (src[0] in _TYPE_WORDS or src[0] == "vector"):
        return src[0] in _FITS[dst[0]]
    if dst[0] in _TYPE_WORDS or src[0] in _TYPE_WORDS:
        return src[0] == dst[0] or dst[0] not in _FITS
    return src[0].lower() == dst[0].lower()          # file types


def _literal_words(e):
    if e[0] == "num":
        return ["int"] if type(e[1]) is int else ["float"]
    if e[0] == "bits":
        return ["bits", str(len(e[1]))]
    return {"imag": ["complex"], "str": ["text"], "bool": ["bool"]}.get(e[0])


def check_wiring(code, ports=lambda m: None, is_file=lambda t: False) -> list:
    """Direction and port errors in Signet code, as "line N: ..." strings. `ports(module)` gives None for
    no such module, else {"in": {...}, "out": {...}} or {} when the module declares nothing;
    `is_file(name)` says a name is a plain file."""
    root = parse_signals(code) if isinstance(code, str) else code
    nodes = list(_walk(root))
    targets = {}
    for nd in nodes:
        if nd[0] == "connect" and nd[4]:
            targets.setdefault((nd[4], nd[5]), nd[1])
        elif nd[0] == "route" and nd[2] == "exit":
            for _, mod, port, _ in nd[4]:
                if mod:
                    targets.setdefault((mod, port), nd[1])
    known = {m for m, _ in targets} | {nd[2] for nd in nodes if nd[0] == "run" and isinstance(nd[2], str) and not nd[2].isdigit()}
    cache, errors, here = {}, [], [0]
    def decl(m):
        if m not in cache:
            try:
                cache[m] = ports(m)
            except ValueError as e:
                cache[m] = {}
                errors.append(f"line {here[0]}: {e}")
        return cache[m]
    def outs(m):
        d = decl(m) or {}
        return f" ({m} writes {', '.join(sorted(d['out']))})" if d.get("out") else ""
    def source(e, line):
        """The declared type of a source, or None; reports a source that is really an input."""
        if e[0] != "ref" or ":" in e[1]:
            return _literal_words(e)
        text = e[1]
        parts = text[:-4].split(".") if text.endswith(".sig") else text.split(".")
        if not text.endswith(".sig"):
            if len(parts) == 2 and (parts[0] in known or decl(parts[0]) is not None) and not is_file(text):
                m, p = parts
                d = decl(m) or {}
                if p in d.get("out", {}):
                    errors.append(f"line {line}: {text} isn't a Signal; did you mean {text}.sig?")
                elif (m, p) in targets or p in d.get("in", {}):
                    errors.append(f"line {line}: {text} is an input of {m}, not a Signal: read {m}'s output as {m}.<output>.sig{outs(m)}")
                else:
                    errors.append(f"line {line}: {text} isn't a Signal: write {text}.sig for {m}'s output {p}")
            return None
        if len(parts) != 2:
            return None
        m, p = parts
        d = decl(m)
        if not d:
            return None
        if p in d["in"]:
            errors.append(f"line {line}: {m}.{p} is an input of {m}, so {text} is never written{outs(m)}")
        elif p not in d["out"]:
            errors.append(f"line {line}: {m} has no output {p}{outs(m)}")
        else:
            return d["out"][p]
        return None
    def target(words, mod, port, line, src_words):
        d = decl(mod) if mod else None
        if not d:
            return
        if port in d["out"]:
            errors.append(f"line {line}: {mod}.{port} is an output of {mod}, not an input")
        elif port not in d["in"]:
            ins = ", ".join(sorted(d["in"])) or "none"
            errors.append(f"line {line}: {mod} has no input {port} (its inputs: {ins})")
        else:
            want = d["in"][port]
            got = words or src_words
            if want and got and not _fits(got, want):
                errors.append(f"line {line}: {mod}.{port} takes {'::'.join(want)}, got {'::'.join(got)}")
    def reads(e, line):
        if isinstance(e, tuple):
            if e and e[0] == "ref":
                source(e, line)
            else:
                for sub in e[1:]:
                    reads(sub, line)
        elif isinstance(e, list):
            for sub in e:
                reads(sub, line)
    for nd in nodes:
        line = here[0] = nd[1]
        if nd[0] == "connect":
            src = nd[2]
            w = source(src, line) if src[0] in ("ref", "num", "imag", "str", "bool") else reads(src, line)
            target(nd[3], nd[4], nd[5], line, w)
        elif nd[0] == "route":
            w = source(nd[3], line) if nd[3][0] == "ref" else reads(nd[3], line)
            for item in nd[4]:
                if nd[2] == "exit":
                    target(item[0], item[1], item[2], line, w)
                else:
                    reads(item, line)
        elif nd[0] == "run" and isinstance(nd[2], str):
            decl(nd[2])                # reports a bad declaration
        elif nd[0] in ("if", "when", "for", "function", "subprocess"):
            for sub in nd[2:6]:
                if not isinstance(sub, list):
                    reads(sub, line)
        else:
            for sub in nd[2:]:
                reads(sub, line)
    return sorted(set(errors), key=lambda s: int(s.split()[1].rstrip(":")))


def contract_errors(module: str, ports, inputs=None, written=None) -> list:
    """Run-time checks against a module's declared ports: before a run (inputs), after it (written .sig names)."""
    if not ports:
        return []
    errs = []
    if inputs is not None:
        missing = sorted(set(ports["in"]) - set(inputs))
        extra = sorted(set(inputs) - set(ports["in"]))
        if missing:
            errs.append(f"{module} needs input{'s' * (len(missing) > 1)} {', '.join(missing)}: nothing is connected")
        if extra:
            errs.append(f"{module} has no input{'s' * (len(extra) > 1)} {', '.join(extra)}")
    if written is not None:
        pat = re.compile(r"^" + re.escape(module) + r"\.([A-Za-z_][A-Za-z0-9_]*)\.sig$")
        got = {pat.match(n).group(1) for n in written if pat.match(n)}
        extra = sorted(got - set(ports["out"]))
        missing = sorted(set(ports["out"]) - got)
        if extra:
            errs.append(f"{module} wrote {', '.join(extra)}, which it doesn't declare")
        if missing:
            errs.append(f"{module} didn't write {', '.join(missing)}")
    return errs


class FolderHost:
    """A plain folder as Signet's world: every stamped .sig in it is live, any other file by name;
    parameters from --param; run(module) starts <module>.py/.jl/.sh or an executable <module> here."""

    def __init__(self, params=None, timeout=3600):
        self.params = {k.lower(): v for k, v in (params or {}).items()}
        self.timeout = timeout
        self.states = {}
        self.going = {}          # module -> a run started in the background because it writes a stream
        self.errors = {}

    def point_param(self, point, name):
        raise LookupError(f"{point}:${name}: points need Starfire; outside it, use $name")

    def autorun(self):
        return True             # a folder has no RUN buttons

    def state(self, module):
        if module in self.going:
            self._poll(module)
        return self.states.get(module, "none")

    def break_runs(self):
        for module, r in list(self.going.items()):
            r["proc"].kill()
            r["proc"].wait()
            for path in r["streams"]:
                end_stream(path)
            del self.going[module]
            self.states[module] = "broken"

    def wait_all(self) -> list:
        """Waits for runs still going (modules writing streams); the errors of any that failed."""
        errors = []
        for module in list(self.going):
            while module in self.going:
                self._poll(module)
                time.sleep(0.05)
            if self.states[module] == "failed":
                errors.append(self.errors.get(module, f"{module} failed"))
        return errors

    def resolve(self, text):
        if ":" in text:
            raise LookupError(f"{text}: points need Starfire; outside it, name the signal alone")
        if not text.endswith(".sig"):
            if os.path.isfile(text):
                return source_value(text, text, text)
            raise LookupError(f"nothing in this folder matches {text}")
        want = text[:-4].split(".")
        found = sorted(n for n in os.listdir(".") if re.match(r"^[A-Za-z0-9_-]+\.[A-Za-z_][A-Za-z0-9_]*\.sig$", n)
                       and n[:-4].split(".")[-len(want):] == want)
        stamped = []
        for n in found:
            try:
                stream_sig(n) or read_sig(n)        # a stream still being written has no stamp yet
                stamped.append(n)
            except ValueError:
                pass
        if not stamped:
            raise LookupError(f"{text} has no valid stamp" if found else f"nothing in this folder matches {text}")
        if len(stamped) > 1:
            raise LookupError(f"{text} matches {len(stamped)} signals: {', '.join(stamped)}")
        return source_value(stamped[0], stamped[0], text)

    def param(self, name):
        if name.lower() not in self.params:
            raise LookupError(f"no parameter {name}: give --param {name}=value")
        return self.params[name.lower()]

    def echo(self, text):
        print(text, flush=True)

    def ask(self, prompt, type_name):
        """input(): asks on the terminal; answers can also be piped in."""
        try:
            return input(f"{prompt} " + (f"[{type_name}] " if type_name else ""))
        except EOFError:
            raise LookupError(f"input has no answer for: {prompt}")

    def run(self, module, inputs):
        """Runs the module to its end (a sequence in a folder waits for each run), except a module that declares
        a stream output: that one runs in the background, so others can read the stream while it is written."""
        if module in self.going:
            raise RuntimeError(f"{module} is still running")
        self.states[module] = "running"
        try:
            self._run(module, inputs)
        except BaseException:
            self.states[module] = "failed"
            raise
        if module not in self.going:       # one writing a stream is complete only when its process ends
            self.states[module] = "complete"

    def _run_signet(self, module, name, inputs, ports=None):
        """A module written in Signet: its inputs are variables, output(name, value) writes the Signals."""
        before = {n: os.stat(n).st_mtime_ns for n in os.listdir(".") if n.endswith(".sig") and os.path.isfile(n)}
        try:
            with open(name, encoding="utf-8") as f:
                code = f.read()
            run_module_code(code, inputs or {}, f"{module}.", self.echo)
        except Break as b:
            raise RuntimeError(f"{module} stopped at its break on line {b.line}")
        except ValueError as e:
            raise RuntimeError(f"{name}: {e}")
        written = [n for n, t in {n: os.stat(n).st_mtime_ns for n in os.listdir(".") if n.endswith(".sig") and os.path.isfile(n)}.items()
                   if before.get(n) != t]
        self._contract(module, ports, written=written)
        res = stamp("", module, written, os.path.basename(os.getcwd()) + ":", inputs or {}, name)
        for e in res["errors"]:
            print(f"{module}: {e}", file=sys.stderr)

    def _hits(self, module):
        return [n for n in sorted(os.listdir("."))
                if n != "signet.py" and os.path.isfile(n) and (n in (module + e for e in (*_INTERPRETERS, *SOURCE_TYPES, ".signet"))
                                                               or (n == module and os.access(n, os.X_OK)))]

    @staticmethod
    def _file_ports(name):
        """The ports a module file declares, {} if none (or it isn't text). Raises ValueError for a bad declaration."""
        try:
            if os.path.getsize(name) > 1_000_000:
                return {}
            with open(name, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            return read_ports(text) or {}
        except ValueError as e:
            raise ValueError(f"{name}: {e}")

    def module_ports(self, module):
        """None if there is no module of this name here, else its declared ports ({} if it declares none)."""
        hits = self._hits(module)
        return None if not hits else self._file_ports(hits[0]) if len(hits) == 1 else {}

    def _contract(self, module, ports, inputs=None, written=None):
        errs = contract_errors(module, ports, inputs, written)
        if errs:
            raise RuntimeError("; ".join(errs))

    def _run(self, module, inputs):
        hits = self._hits(module)
        if len(hits) != 1:
            raise LookupError(f"no module {module} here" if not hits else f"{module} matches {', '.join(hits)}; keep one")
        name = hits[0]
        try:
            ports = self._file_ports(name)
        except ValueError as e:
            raise RuntimeError(str(e))
        self._contract(module, ports, inputs=inputs or {})
        if name.endswith(".signet"):
            return self._run_signet(module, name, inputs, ports)
        argv = [os.path.join(".", name)]
        interp = _INTERPRETERS.get(os.path.splitext(name)[1])
        if os.path.splitext(name)[1].lower() in SOURCE_TYPES:
            argv = build_module(name, lambda t: print("signet: " + t, file=sys.stderr, flush=True))
        elif interp:
            found = shutil.which(interp)
            if not found:
                raise LookupError(f"{interp} not found on PATH (needed to run {name})")
            argv.insert(0, found)
        inputs_path = f"{module}.inputs.json"
        write_inputs(inputs_path, inputs)
        before = self._files()
        env = module_env(os.environ, "", module, os.path.abspath(inputs_path) if inputs else "")
        streams = [f"{module}.{p}.sig" for p, w in (ports or {}).get("out", {}).items() if w[:1] == ["stream"]]
        if streams:
            for path in streams:            # there from the start, so a reader can connect at once
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"value": {"x": [], "values": [], "window": STREAM_WINDOW, "count": 0, "ended": False},
                                        "type": "stream"}) + "\n")
            proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL)
            def ender():               # readers following the stream learn of its end at once
                proc.wait()
                for path in streams:
                    end_stream(path)
            threading.Thread(target=ender, daemon=True).start()
            self.going[module] = {"proc": proc, "ports": ports, "before": before, "streams": streams,
                                  "until": time.monotonic() + self.timeout, "inputs": inputs or {}, "file": name}
            return
        try:
            code = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, timeout=self.timeout).returncode
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{module} timed out after {self.timeout} s")
        self._ended(module, code, ports, before, (), inputs or {}, name)

    @staticmethod
    def _files():
        return {n: os.stat(n).st_mtime_ns for n in os.listdir(".") if n.endswith(".sig") and os.path.isfile(n)}

    def _poll(self, module):
        r = self.going[module]
        code = r["proc"].poll()
        if code is None and time.monotonic() > r["until"]:
            r["proc"].kill()
            code = r["proc"].wait()
            self.errors[module] = f"{module} timed out after {self.timeout} s"
        if code is None:
            return
        del self.going[module]
        try:
            self._ended(module, code, r["ports"], r["before"], r["streams"], r["inputs"], r["file"])
            self.states[module] = "complete"
        except (RuntimeError, LookupError) as e:
            self.errors.setdefault(module, str(e))
            print(self.errors[module], file=sys.stderr)
            self.states[module] = "failed"

    def _ended(self, module, code, ports, before, streams=(), inputs=None, file=None):
        """After a module's process ends: mark its streams ended, then check and stamp what it wrote."""
        changed = [n for n, t in self._files().items() if before.get(n) != t]
        for path in sorted(set(streams) | {n for n in changed if n.startswith(module + ".")}):
            end_stream(path)
        if code != 0:
            raise RuntimeError(f"{module} failed (exit {code})")
        written = [n for n, t in self._files().items() if before.get(n) != t]
        self._contract(module, ports, written=written)
        res = stamp("", module, written, os.path.basename(os.getcwd()) + ":", inputs, file)
        for e in res["errors"]:
            print(f"{module}: {e}", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="signet", description="Run a .signet file in the current folder.")
    ap.add_argument("file", nargs="?", help="the .signet file")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="a parameter for $NAME")
    ap.add_argument("--check", action="store_true", help="only read the file and report bad lines")
    ap.add_argument("--lineage", metavar="SIG", help="show the chain that made a Signal in this folder, and stop")
    ap.add_argument("--export-helpers", metavar="DIR", help="write the module helpers (Python, Julia, shell, C++, Rust, Ada, Fortran) into DIR and stop")
    ap.add_argument("--version", action="version", version=f"signet {VERSION} (API {API})")
    a = ap.parse_args(argv)
    if a.export_helpers:
        for p in export_helpers(a.export_helpers):
            print(p)
        return 0
    if a.lineage:
        if not os.path.isfile(a.lineage):
            print(f"no Signal {a.lineage} here", file=sys.stderr)
            return 1
        lines = lineage(os.path.basename(a.lineage), os.path.dirname(a.lineage) or ".")
        print("\n".join(lines))
        return 0 if lines and ", stamp ok" in lines[0] else 1
    if not a.file:
        ap.error("give a .signet file")
    try:
        with open(a.file, encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        print(f"can't read {a.file}: {e.strerror}", file=sys.stderr)
        return 1
    params = {}
    for p in a.param:
        name, eq, value = p.partition("=")
        if not eq or not _PARAM_NAME.match(name.strip()):
            print(f"bad --param {p!r}: expected NAME=VALUE", file=sys.stderr)
            return 1
        params[name.strip()] = value
    try:
        host = FolderHost(params)
        wiring = check_wiring(parse_signals(code), host.module_ports, os.path.isfile)
        if wiring:
            raise ValueError("\n".join(wiring))
        if a.check:
            print(f"{a.file}: ok")
            return 0
        seq = Sequence(code, None, host, now=time.monotonic())
        while True:
            st = seq.step(time.monotonic())
            if st.state != "waiting":
                break
            time.sleep(0.2)      # a wait on a condition, delay(), or a module writing a stream gets here
        if st.state != "broken" and host.wait_all():
            return 1             # wait_all has said why
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if st.state == "broken":
        print(st.reason, file=sys.stderr)
        return 2 if st.kind == "break" else 1
    return 0


_HELPER_MODULE = None


def __getattr__(name):
    # A module in the same folder as signet.py imports this file rather than the helper package,
    # so `from signet import signals` (and Series, Bus) is answered with the same helper.
    global _HELPER_MODULE
    if name in ("signals", "Series", "Bus", "Stream"):
        if _HELPER_MODULE is None:
            import types
            _HELPER_MODULE = types.ModuleType("signet_helper")
            exec(compile(_PY_HELPER, "signet_helper", "exec"), _HELPER_MODULE.__dict__)
        return getattr(_HELPER_MODULE, name)
    raise AttributeError(f"module 'signet' has no attribute {name!r}")


parse = parse_signals
sig_hash = _sig_hash
series_parts = _series_parts
is_num = _is_num
Value = _SV
STEM = _STEM


if __name__ == "__main__":
    sys.exit(main())
