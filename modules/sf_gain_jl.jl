# signet: in float::k1, in series::time::float::s1, out series::time::float::s2
using Signet

k1, s1 = signals.k1, signals.s1
signals.s2 = series(k1 .* s1.values; domain=s1.domain, x=s1.x)
