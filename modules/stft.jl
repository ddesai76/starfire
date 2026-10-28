#!/usr/bin/env julia

# stft.jl:        DEMO MODULE - SHORT-TIME FOURIER TRANSFORM
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-24
# VERSION:        4.0.0
#
# Reads a time series on "signal_in"; writes the dominant frequency in each window
# ("ridge", series::time) and the mean magnitude spectrum ("spectrum", series::frequency).
# Arguments: --window samples  --hop samples  (optional)

using Signet: signals, series
# signet: in series::signal_in, out series::spectrum, out series::ridge

function arg(name, default)
    i = findfirst(==("--$name"), ARGS)
    i === nothing ? default : parse(Int, ARGS[i + 1])
end
fail(msg) = (println(stderr, "stft: ", msg); exit(1))

win, hop = arg("window", 128), arg("hop", 32)
s = signals.signal_in
s.domain == "time" || fail("signal_in must be a time series, got $(s.domain)")
x = Float64.(s.values)
dt = s.step === nothing ? s.x[2] - s.x[1] : s.step
length(x) >= win || fail("$(length(x)) samples is shorter than the $win-sample window")

nb = win ÷ 2 + 1
w = [0.5 - 0.5 * cos(2π * j / (win - 1)) for j in 0:win-1]          # Hann
E = [cis(-2π * k * j / win) for k in 0:nb-1, j in 0:win-1]          # DFT rows, bins 0..win/2
starts = 0:hop:length(x)-win
mag = permutedims(reduce(hcat, [abs.(E * (x[i+1:i+win] .* w)) for i in starts]))   # frames x bins
f = collect(0:nb-1) ./ (win * dt)
t = [(i + win / 2) * dt for i in starts]                             # window centres
ridge = [f[argmax(mag[r, :])] for r in axes(mag, 1)]

signals.ridge = series(ridge; domain="time", x=t)
signals.spectrum = series(vec(sum(mag, dims=1)) ./ size(mag, 1); domain="frequency", x=f)
println("stft: $(length(t)) windows of $win (hop $hop), $(f[2]) Hz bins, ridge $(ridge[1]) -> $(ridge[end]) Hz")
