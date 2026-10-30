#!/usr/bin/env julia

# sweep.jl:       DEMO MODULE - LINEAR CHIRP WAVEFORM
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-30
# VERSION:        4.0.0
#
# Writes a linear chirp to the "waveform" output port (<prefix>-sweep.waveform.sig): a series, or with
# --stream a stream written sample by sample at the sample rate, as a live sensor would.
# Arguments: --f0 Hz  --f1 Hz  --duration s  --rate Hz  --amplitude  --stream  (all optional)

using Signet: signals, series, stream
# signet: out waveform

function arg(name, default)
    i = findfirst(==("--$name"), ARGS)
    i === nothing ? default : parse(Float64, ARGS[i + 1])
end

f0, f1, dur, rate, amp = arg("f0", 5.0), arg("f1", 50.0), arg("duration", 1.0), arg("rate", 1000.0), arg("amplitude", 1.0)
live = "--stream" in ARGS
dt = 1 / rate
n = floor(Int, dur * rate)
k = (f1 - f0) / dur                                   # sweep rate, Hz/s
ts = (0:n-1) .* dt
x = [amp * sin(2π * (f0 * t + k * t^2 / 2)) for t in ts]

if live
    s = stream("waveform"; domain="time")             # keeps the last 1048 samples
    start = time()
    for (t, v) in zip(ts, x)
        write(s, t, v)
        lag = start + t - time()                      # keep to the sample rate
        lag > 0.001 && sleep(lag)
    end
else
    signals.waveform = series(x; domain="time", start=0.0, step=dt)
end
println("sweep: $f0-$f1 Hz, $n samples at $rate Hz, peak $(round(maximum(x), digits=3))", live ? ", streamed" : "")
