# signet: in float::k1, in series::time::float::s1, out series::time::float::s2
import numpy as np
from signet import signals, Series

k1, s1 = signals.k1, signals.s1
signals.s2 = Series(k1 * np.asarray(s1.values), s1.domain, x=s1.x)
