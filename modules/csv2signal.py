# signet: in csv::table, out series::data
# csv2signal.py:  A CSV FILE AS A SERIES SIGNAL: FIRST COLUMN THE AXIS, THEN THE VALUES
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-19
# VERSION:        26.b.0.1.7.5
#
# connect(sweep.csv, csv2signal.table), run(csv2signal), then csv2signal.data! is the series.
# The first row is the header; the domain is the first column's name, as Signet's column() names it
# ("time_s" is time). Lines starting # are skipped, and an empty or non-numeric value is NaN.
# The second column is a real series, unless the headers after the first are re, im (complex) or
# w, x, y, z (quaternion); a prefix is allowed (z_re, z_im). signal2csv writes these headers.

import re

import pandas as pd
from signet import Quaternion, signals

TYPE_WORDS = {"int", "float", "real", "num", "bool", "text", "complex", "vector", "series", "bus", "stream", "tuple",
              "bits", "range", "hex", "fixed", "quaternion", "units", "hypercomplex"}


def domain_of(header):
    d = re.sub(r"_[A-Za-z0-9%]+\Z", "", header.strip()) if "_" in header else header.strip()
    d = re.sub(r"[^A-Za-z0-9_]", "_", d)
    if not d or d[0].isdigit():
        d = "x" + d
    return d + "_" if d.lower() in TYPE_WORDS else d.lower()


df = pd.read_csv(signals.table, comment="#", skipinitialspace=True)
if df.shape[1] < 2:
    raise SystemExit(f"{signals.table} needs two columns: the axis, then the values")
x = pd.to_numeric(df.iloc[:, 0], errors="coerce")
if x.isna().any():
    raise SystemExit(f"{signals.table}: the first column ({df.columns[0]}) must be numbers on every row")
kinds = [str(c).strip().lower().rsplit("_", 1)[-1] for c in df.columns[1:]]
n = 4 if kinds[:4] == ["w", "x", "y", "z"] else 2 if kinds[:2] == ["re", "im"] else 1
cols = [pd.to_numeric(df.iloc[:, k], errors="coerce").astype(float) for k in range(1, n + 1)]
rows = zip(*cols)
values = [Quaternion(*r) for r in rows] if n == 4 else [complex(*r) for r in rows] if n == 2 else \
    [None if pd.isna(v) else v for v in cols[0]]
signals.data = signals.series(values, domain=domain_of(str(df.columns[0])), x=[float(v) for v in x])
