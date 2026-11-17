# signet: out series::time::float::wave
import math
from signet import signals, Series

n = 100                                   # Samples per cycle
dt = 1 / n                                # Step, s
signals.wave = Series([math.sin(2 * math.pi * k / n) for k in range(n)], "time", start=0.0, step=dt)
