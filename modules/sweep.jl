#!/usr/bin/env julia

# sweep.jl:       DEMO MODULE - LINEAR CHIRP WAVEFORM
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-24
# VERSION:        3.9.0
#
# Writes a linear chirp to the "waveform" output port (<prefix>-sweep.waveform.sig).
# Arguments: --f0 Hz  --f1 Hz  --duration s  --rate Hz  --amplitude  (all optional)

using Signet: signals, series
# signet: out series::waveform

function arg(name, default)
    i = findfirst(==("--$name"), ARGS)
    i === nothing ? default : parse(Float64, ARGS[i + 1])
end

f0, f1, dur, rate, amp = arg("f0", 5.0), arg("f1", 50.0), arg("duration", 1.0), arg("rate", 1000.0), arg("amplitude", 1.0)
dt = 1 / rate
n = floor(Int, dur * rate)
k = (f1 - f0) / dur                                   # sweep rate, Hz/s
x = [amp * sin(2π * (f0 * t + k * t^2 / 2)) for t in (0:n-1) .* dt]

signals.waveform = series(x; domain="time", start=0.0, step=dt)
println("sweep: $f0-$f1 Hz, $n samples at $rate Hz, peak $(round(maximum(x), digits=3))")
