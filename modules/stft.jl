#!/usr/bin/env julia

# stft.jl:        DEMO MODULE - SHORT-TIME FOURIER TRANSFORM
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-08
# VERSION:        4.2.2
#
# Reads a time series or a time stream on "signal_in"; writes the dominant frequency in each window
# ("ridge", time) and the mean magnitude spectrum ("spectrum", series::frequency). From a series, the ridge is
# a series; from a stream, it is a stream, one value per window as each fills, and the spectrum is written
# when the stream ends.
# Arguments or Starfire parameters (optional): window, hop (samples). Defaults suit the GEVS sweep from sweep.jl:
# 1024-sample windows every 512 samples, 9.8 Hz bins at 10,000 samples/s.

using Signet: signals, series, stream, StreamIn
# signet: in signal_in, out spectrum, out ridge

function arg(name, default)
    i = findfirst(==("--$name"), ARGS)
    v = i === nothing ? get(ENV, "STARFIRE_PARAM_$(uppercase(name))", "") : ARGS[i + 1]
    isempty(v) ? default : parse(Int, v)
end
fail(msg) = (println(stderr, "stft: ", msg); exit(1))

win, hop = arg("window", 1024), arg("hop", 512)
nb = win ÷ 2 + 1
w = [0.5 - 0.5 * cos(2π * m / (win - 1)) for m in 0:win-1]          # Hann
E = [cis(-2π * p * m / win) for p in 0:nb-1, m in 0:win-1]          # DFT rows, bins 0..win/2
frame(seg) = abs.(E * (seg .* w))

function from_series(s)
    s.domain == "time" || fail("signal_in must be a time series, got $(s.domain)")
    x = Float64.(s.values)
    dt = s.step === nothing ? s.x[2] - s.x[1] : s.step
    length(x) >= win || fail("$(length(x)) samples is shorter than the $win-sample window")
    starts = 0:hop:length(x)-win
    mag = permutedims(reduce(hcat, [frame(x[k+1:k+win]) for k in starts]))   # frames x bins
    f = collect(0:nb-1) ./ (win * dt)
    t = [(k + win / 2) * dt for k in starts]                             # window centers
    ridge = [f[argmax(mag[r, :])] for r in axes(mag, 1)]
    signals.ridge = series(ridge; domain="time", x=t)
    signals.spectrum = series(vec(sum(mag, dims=1)) ./ size(mag, 1); domain="frequency", x=f)
    println("stft: $(length(t)) windows of $win (hop $hop), $(f[2]) Hz bins, ridge $(ridge[1]) -> $(ridge[end]) Hz")
end

function from_stream(s)
    ridge = stream("ridge"; domain="time")
    buf, total, frames = Float64[], zeros(nb), 0
    t0, dt, f, first_r, last_r = NaN, NaN, Float64[], NaN, NaN
    for (t, v) in s                                   # each sample as it arrives, until the stream ends
        if isnan(t0)
            t0 = t
        elseif isnan(dt)
            dt = t - t0
            dt > 0 || fail("signal_in's samples must rise in time")
            f = collect(0:nb-1) ./ (win * dt)
        end
        push!(buf, v)
        if length(buf) >= win
            m = frame(buf[1:win])
            total .+= m
            last_r = f[argmax(m)]
            frames == 0 && (first_r = last_r)
            write(ridge, t0 + (frames * hop + win / 2) * dt, last_r)   # window center
            frames += 1
            deleteat!(buf, 1:hop)
        end
    end
    frames > 0 || fail("the stream ended before filling the $win-sample window")
    signals.spectrum = series(total ./ frames; domain="frequency", x=f)
    println("stft: $frames windows of $win (hop $hop) from a stream, $(f[2]) Hz bins, ridge $first_r -> $last_r Hz")
end

s = signals.signal_in
s isa StreamIn ? from_stream(s) : from_series(s)