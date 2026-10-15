#!/usr/bin/env python3
# hotfire.py:     EXAMPLE TEST MODULE (ELECTRIC PROPULSION HOT FIRE)
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-10-13
# VERSION:        0.1.0
#
# Python version of hotfire.cpp. Prints voltage/current/accel as CSV (simulated values)
# and, if matplotlib is installed, saves <campaign>-<card>-<point>-plot.png to the pwd.


import os
import random
import signal
import sys
import time

FIRE = 0xFF                 # 0b11111111 fires all 8 thrusters
MIN_FUEL = 10.0
BURN_TIME = 300.0           # s
SAMPLE_TIME = 1.0           # s

stop = False
reg = 0                     # Stand-in for the thruster register
# reg = mmap.mmap(fd, 1, offset=0x40000000)   # on the device (import mmap; fd = open("/dev/mem"))


def on_term(signum, frame):
    global stop
    stop = True


def set_reg(v):
    global reg
    reg = v


def jitter():
    return random.uniform(-1.0, 1.0)


def save_plot(t, voltage, current, accel):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("# No matplotlib, plot skipped")
        return
    name = "-".join(os.environ.get(k, d) for k, d in
                    (("STARFIRE_CAMPAIGN", "XX-000"), ("STARFIRE_CARD", "TC"), ("STARFIRE_POINT", "0")))
    fig, ax = plt.subplots(3, 1, sharex=True, figsize=(7, 6))
    for a, y, label in zip(ax, (voltage, current, accel), ("voltage_V", "current_uA", "accel_um_s2")):
        a.plot(t, y)
        a.set_ylabel(label)
    ax[-1].set_xlabel("time_s")
    fig.tight_layout()
    fig.savefig(name + "-plot.png", dpi=150)
    print("# saved " + name + "-plot.png")


def main():
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    # send("HV ON")
    # fuel = query("FUEL?")
    fuel = 90.0
    if fuel < MIN_FUEL:
        # send("HV OFF")
        print("# STOPPED: fuel below MIN_FUEL")
        return 1

    set_reg(FIRE)
    n = bin(reg).count("1")                     # Thrusters energized
    samples = int(BURN_TIME / SAMPLE_TIME)
    t, voltage, current, accel = [], [], [], []

    print("time_s,voltage_V,current_uA,accel_um_s2")
    for k in range(samples):
        if stop:
            break
        # voltage.append(query("MEAS:VOLT?"))
        # current.append(query("MEAS:CURR?"))
        # accel.append(query("STAND:ACCEL?"))
        v = 1500.0 + 15.0 * jitter()            # random: ~1500 V, +/-15 V
        i = n * (50.0 + 1.0 * jitter())         # random: ~50 uA per thruster, +/-1 uA
        a = n * (20.0 + 0.5 * jitter())         # random: ~20 um/s^2 per thruster, +/-0.5
        t.append(k * SAMPLE_TIME)
        voltage.append(v)
        current.append(i)
        accel.append(a)
        print("%.1f,%.1f,%.2f,%.2f" % (t[k], voltage[k], current[k], accel[k]), flush=True)
        time.sleep(SAMPLE_TIME)

    set_reg(0)
    # send("HV OFF")
    if t:
        save_plot(t, voltage, current, accel)
    print("# STOPPED" if stop else "# DONE")
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())