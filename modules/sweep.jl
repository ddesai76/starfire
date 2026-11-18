#!/usr/bin/env julia

# sweep.jl:       DEMO MODULE - LOGARITHMIC SINE SWEEP
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-04
# VERSION:        4.3.0
#
# Writes a logarithmic sine sweep to the "waveform" output port (<prefix>-sweep.waveform.sig).
# Defaults: NASA GEVS protoflight low-level sine survey, 0.25 g, 20-2000 Hz at 4 octaves/minute.
# Arguments or Starfire parameters (all optional): f0 Hz, f1 Hz, octaves (per minute), rate (samples/s), amplitude (g)
# --stream (or a stream parameter) writes it as a stream at 5000 samples/s by default, in real time if the
# writer keeps up, otherwise as fast as it can.

using Signet: signals, series, stream
# signet: out waveform

function arg(name, default)
    i = findfirst(==("--$name"), ARGS)
    v = i === nothing ? get(ENV, "STARFIRE_PARAM_$(uppercase(name))", "") : ARGS[i + 1]
    isempty(v) ? default : parse(Float64, v)
end

streaming = "--stream" in ARGS || !isempty(get(ENV, "STARFIRE_PARAM_STREAM", ""))
f0, f1, oct, amp = arg("f0", 20.0), arg("f1", 2000.0), arg("octaves", 4.0), arg("amplitude", 0.25)
rate = arg("rate", streaming ? 5000.0 : 10000.0)
r = oct / 60                                          # octaves/s
dur = log2(f1 / f0) / r
dt = 1 / rate
n = floor(Int, dur * rate)
x(t) = amp * sin(2π * f0 * (2^(r * t) - 1) / (r * log(2)))
ts = (0:n-1) .* dt

if streaming
    s, t0 = stream("waveform"; domain="time"), time()
    for t in ts
        sleep(max(0, t0 + t - time()))                # real time, or flat out if behind
        write(s, t, x(t))
    end
else
    signals.waveform = series(x.(ts); domain="time", start=0.0, step=dt)
end
println("sweep: $f0-$f1 Hz at $oct oct/min, $(round(dur, digits=1)) s, $n samples at $rate Hz, $amp g peak")