# signet: in series::data, in text::file, out text::written
# signal2csv.py:  A SERIES SIGNAL AS A CSV FILE: FIRST COLUMN THE AXIS, THEN THE VALUES
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-19
# VERSION:        26.b.0.1.7.5
#
# connect(sensor.data!, signal2csv.data), connect('out.csv', signal2csv.file), run(signal2csv).
# The header is the series' domain, then value (real), re, im (complex) or w, x, y, z (quaternion);
# NaN is an empty cell. signal2csv.written! names the
# file, so the run is stamped and --lineage shows which series made it. csv2signal reads it back.

import pandas as pd
from signet import Quaternion, signals

s = signals.data
cols = {s.domain: s.x}
if s.values and isinstance(s.values[0], Quaternion):
    cols.update({k: [getattr(q, k) for q in s.values] for k in ("w", "x", "y", "z")})
elif s.values and isinstance(s.values[0], complex):
    cols.update(re=[v.real for v in s.values], im=[v.imag for v in s.values])
else:
    cols["value"] = s.values
pd.DataFrame(cols).to_csv(signals.file, index=False)
signals.written = signals.file
