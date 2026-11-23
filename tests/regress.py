#!/usr/bin/env python3

# regress.py:     STARFIRE REGRESSION SUITE (LINUX AND WINDOWS, SOURCE OR BINARY)
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-23
# VERSION:        4.6.3 (BELLATRIX)
#

"""
Starts Starfire in a scratch folder and drives it through its HTTP API. Must stay at 0 failed,
from source and on the built binary. Its modules are its own (below), not the examples in modules/.
Standard library only.

    python tests/regress.py starfire.py            # from source (copies starfire.py, signet.py, static/)
    python tests/regress.py dist/starfire[.exe]    # a built binary
    python tests/regress.py starfire.py -k gain    # only cases whose name contains "gain"
"""

import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

CAMP, CARD = "SC-900", "TC-001"
TIMEOUT_S = 6                                        # STARFIRE_MODULE_TIMEOUT for this run
K1 = 1.234
WAVE = [math.sin(2 * math.pi * k / 100) for k in range(100)]

MODULES = {
    "emit.py": """# signet: out float::value, out series::time::quaternion::q
from signet import signals, Series, Quaternion
print("emit: héllo")
signals.value = 21.0
signals.q = Series([Quaternion(1, 0, 0, 0), Quaternion(0.5, 0.5, 0.5, 0.5)], "time", start=0.0, step=0.1)
""",
    "double.sg": """# signet: in float::x, out float::y
output(y, x * 2)
""",
    # An optional input: unconnected, it gets its default
    "scale.py": """# signet: in float::x, in float::g = 3.0, out float::y
from signet import signals
signals.y = signals.x * signals.g
""",
    # A child process that stamps a heartbeat file until the whole tree is killed
    "sleeper.py": """import os, subprocess, sys, time
beat = os.environ["STARFIRE_PREFIX"] + "-beat.txt"
child = "import sys, time\\nwhile True:\\n    open(sys.argv[1], 'w').write(str(time.time()))\\n    time.sleep(0.2)\\n"
subprocess.Popen([sys.executable, "-c", child, beat])
print("started", flush=True)
time.sleep(600)
""",
    # A chain for the wiring cases: a scalar and a series into a gain
    "src.sg": """# signet: out float::k1
output(float::k1, 1.234)
""",
    "wave.py": """# signet: out series::time::float::wave
import math
from signet import signals, Series
signals.wave = Series([math.sin(2 * math.pi * k / 100) for k in range(100)], "time", start=0.0, step=0.01)
""",
    "gain.py": """# signet: in float::k1, in series::time::float::s1, out series::time::float::s2
from signet import signals, Series
signals.s2 = Series([signals.k1 * a for a in signals.s1.values], signals.s1.domain, x=signals.s1.x)
""",
    "notes.txt": "not a module\n",
    "wiring.sg": "# a wiring script, kept out of the module list by .starfireignore\nrun(emit)\n",
    ".starfireignore": "# not modules\nwiring.sg\n",
}

results = []


class Skip(Exception):
    pass


def case(fn):
    results.append(fn)
    return fn


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Starfire:
    """One running copy, in folder cwd."""

    def __init__(self, cmd, cwd, port=None):
        self.cmd, self.cwd, self.port = cmd, cwd, port or free_port()
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["STARFIRE_MODULE_TIMEOUT"] = str(TIMEOUT_S)
        self.env = env
        self.log = open(os.path.join(cwd, "server.log"), "w+", encoding="utf-8")
        self.proc = subprocess.Popen(cmd + ["--no-browser", "--port", str(self.port)], cwd=cwd, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                self.get("/info")
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.5)
        raise RuntimeError(f"Starfire didn't start:\n{self.read_log()}")

    def read_log(self):
        self.log.seek(0)
        return self.log.read()

    def req(self, path, payload=None, raw=False):
        r = urllib.request.Request(f"http://localhost:{self.port}{path}")
        if payload is not None:
            r.data = json.dumps(payload).encode()
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=300) as res:
                body = res.read()
                return res.status, body if raw else (json.loads(body) if res.headers.get_content_type() == "application/json" else body)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, body

    def get(self, path, raw=False):
        return self.req(path, raw=raw)[1]

    def post(self, path, payload, raw=False):
        return self.req(path, payload, raw)[1]

    def run(self, module, point, signals="", live=(), card=CARD, wait=True, points=None):
        r = self.post("/run_module", {"module": module, "card": card, "point": str(point), "campaign": CAMP,
                                      "signals": signals, "live": list(live), "points": points or []})
        if r.get("status") != "running" or not wait:
            return r
        return self.finish(point, card)

    def finish(self, point, card=CARD, limit=300):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            r = self.get(f"/run_status?card={card}&point={point}")
            if r.get("status") not in ("running", "none"):
                return r.get("result", r)
            time.sleep(0.25)
        return {"status": "stuck"}

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def expect(cond, why):
    if not cond:
        raise AssertionError(why)


def sig(sf, name):
    v = sf.get(f"/signal_value?file={name}")
    expect("error" not in v, f"{name}: {v.get('error')}")
    return v


def live_of(r, point, card=CARD):
    return [{"card": card, "point": str(point), "file": f} for f in r.get("files", []) if f.endswith(".sig")]


# ---- Cases. Each takes the running Starfire. ----

@case
def pages(sf):
    for page in ("/", "/test", "/system", "/static/starfire.css"):
        st, body = sf.req(page, raw=True)
        expect(st == 200 and len(body) > 100, f"GET {page}: {st}")
    st, body = sf.req("/test", raw=True)
    expect(b"applyTheme" in body and b"isn't running" in body, "theme and server-gone script missing from /test")


@case
def info(sf):
    i = sf.get("/info")
    same = lambda a, b: os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    expect(same(i["folder"], sf.cwd), f"working folder {i['folder']}")
    expect(same(i["modules"], sf.mods), f"modules folder {i['modules']}, want {sf.mods}")


@case
def module_list(sf):
    lm = sf.get("/list_modules")
    names = set(lm["modules"])
    expect({"emit.py", "double.sg", "sleeper.py", "src.sg", "wave.py", "gain.py"} <= names, f"listed: {sorted(names)}")
    expect("notes.txt" not in names, "a plain file is listed")
    expect("wiring.sg" not in names, ".starfireignore not applied")
    expect(not any(n.startswith("starfire") for n in names), "Starfire lists itself")
    expect(lm.get("windows") == (os.name == "nt"), "wrong OS reported")


@case
def signal_between_points(sf):
    r = sf.run("emit.py", 10)
    expect(r.get("status") == "complete", f"emit.py: {r.get('status')} {r.get('error', '')}")
    expect("emit: héllo" in r.get("output", ""), "module output not read as UTF-8")
    sf.emit = live_of(r, 10)
    r = sf.run("double.sg", 20, "connect(10:emit.value!, double.x)", sf.emit)
    expect(r.get("status") == "complete", f"double.sg: {r.get('status')} {r.get('error', '')}")
    expect(sig(sf, f"{CAMP}-{CARD}-20-double.y.sig").get("text") in ("42", "42.0"), "42 not passed")


@case
def quaternion_view(sf):
    if not getattr(sf, "emit", None):
        raise Skip("needs signal_between_points")
    v = sig(sf, f"{CAMP}-{CARD}-10-emit.q.sig")
    expect("svg" in v and v.get("head", [""])[-1].endswith("+0.5i+0.5j+0.5k"), f"quaternion series: {v.get('head')}")


@case
def input_default(sf):
    if not getattr(sf, "emit", None):
        raise Skip("needs signal_between_points")
    r = sf.run("scale.py", 30, "connect(10:emit.value!, scale.x)", sf.emit)
    expect(r.get("status") == "complete", f"scale.py: {r.get('status')} {r.get('error', '')}")
    expect(sig(sf, f"{CAMP}-{CARD}-30-scale.y.sig").get("text") in ("63", "63.0"), "optional input didn't get its default")


@case
def ports_not_found(sf):
    m = sf.post("/port_map", {"points": [{"seq": 10, "module": "nosuch.py"}, {"seq": 20, "module": "emit.py"}]})
    rows = {r["seq"]: r for r in m["points"]}
    expect(rows["10"]["ports"] is None and rows["20"]["ports"], f"ports: {rows}")
    expect(m.get("modules_dir"), "no modules_dir for the not-found message")


@case
def break_module(sf):
    sf.run("sleeper.py", 400, wait=False)
    for _ in range(80):
        if "started" in sf.get(f"/run_status?card={CARD}&point=400").get("output", ""):
            break
        time.sleep(0.25)
    t0 = time.monotonic()
    b = sf.post("/break_module", {"card": CARD, "point": "400"})
    r = sf.finish(400)
    expect(b.get("success") and r.get("status") == "broken", f"BREAK: {r.get('status')}")
    expect(time.monotonic() - t0 < 10, f"BREAK took {time.monotonic() - t0:.1f} s")
    expect(_heartbeat_stopped(os.path.join(sf.cwd, f"{CAMP}-{CARD}-400-beat.txt")), "BREAK left the child running")


@case
def timeout(sf):
    r = sf.run("sleeper.py", 410)
    expect(r.get("status") == "failed" and f"timed out after {TIMEOUT_S} s" in r.get("error", ""), f"timeout: {r}")
    expect(_heartbeat_stopped(os.path.join(sf.cwd, f"{CAMP}-{CARD}-410-beat.txt")), "timeout left the child running")


def _heartbeat_stopped(path):
    time.sleep(1.5)
    a = os.path.getmtime(path) if os.path.exists(path) else 0
    time.sleep(1.5)
    return a == (os.path.getmtime(path) if os.path.exists(path) else 0)


@case
def card_files(sf):
    card = {"id": "TC-050", "title": "Regress — ünïcode", "points": [{"seq": 10, "action": "Line 1\nLine 2\nLine 3"}]}
    expect(sf.post("/save", card).get("saved"), "card not saved")
    st, body = sf.req("/load?file=TC-050.test", raw=True)
    back = json.loads(body)
    expect(back["title"] == card["title"] and back["points"][0]["action"] == card["points"][0]["action"], "card round trip")
    st, body = sf.req("/export/test/docx", card, raw=True)
    expect(st == 200 and "ünïcode" in zipfile.ZipFile(_bytes_file(sf, body)).read("word/document.xml").decode(), ".docx")
    st, body = sf.req("/export/csv", card, raw=True)
    expect(st == 200 and "Line 2" in body.decode("utf-8"), ".csv")
    st, body = sf.req("/export/print", card, raw=True)
    expect(st == 200 and b"<html" in body.lower(), "print")


def _bytes_file(sf, body):
    p = os.path.join(sf.cwd, "out.docx")
    with open(p, "wb") as f:
        f.write(body)
    return p


@case
def campaign_files(sf):
    camp = {"id": "SC-050", "title": "Regress", "tests": [{"seq": 1, "test_id": "TC-050", "title": ""}]}
    expect(sf.post("/save_campaign", camp).get("saved"), "campaign not saved")
    back = json.loads(sf.req("/load?file=SC-050.camp", raw=True)[1])
    expect(back["id"] == "SC-050" and back["tests"][0]["test_id"] == "TC-050", "campaign round trip")
    st, body = sf.req("/export/campaign/docx", camp, raw=True)
    expect(st == 200 and body[:2] == b"PK", "campaign .docx")


WIRING = """# Scalar gain
run(src)

# One cycle of a sine
run(wave)

# The gain: scale the wave
connect(src.k1!, gain.k1)
connect(wave.wave!, gain.s1)
run(gain)
"""


@case
def wiring_import(sf):
    camp = {"id": "SC-060", "title": "Wiring", "modules": [], "tests": [], "live": []}
    r = sf.post("/import_wiring", {"script": WIRING, "campaign": camp})
    expect("error" not in r, f"import: {r.get('error')}")
    expect(r["points"] == [[10, "src.sg"], [11, "wave.py"], [20, "gain.py"]], f"points {r['points']}")
    c = r["campaign"]
    expect([(m["module"], m["scope"], m["pinned"]) for m in c["modules"]] ==
           [("src.sg", f"{r['id']}-10", True), ("wave.py", f"{r['id']}-11", True), ("gain.py", f"{r['id']}-20", True)],
           f"modules {c['modules']}")
    expect([t["test_id"] for t in c["tests"]] == [r["id"]], f"tests {c['tests']}")
    expect(sorted(x["file"] for x in c["live"]) == [f"SC-060-{r['id']}-10-src.k1.sig", f"SC-060-{r['id']}-11-wave.wave.sig"],
           f"live {c['live']}")
    card = json.loads(sf.req(f"/load?file={r['id']}.test", raw=True)[1])
    expect(card["points"][2]["action"] == "The gain", f"action {card['points'][2]['action']!r}")
    for p in card["points"]:
        res = sf.post("/run_module", {"module": p["module"], "card": r["id"], "point": str(p["seq"]), "campaign": "SC-060",
                                      "signals": p["signals"], "live": c["live"], "points": card["points"]})
        res = sf.finish(p["seq"], r["id"]) if res.get("status") == "running" else res
        expect(res.get("status") == "complete", f"point {p['seq']}: {res.get('error', '')}")
    v = sig(sf, f"SC-060-{r['id']}-20-gain.s2.sig")
    expect(abs(float(v["head"][1].split(": ")[1]) - K1 * WAVE[1]) < 1e-5, f"s2 {v['head']}")
    sf.wired = card


@case
def wiring_export(sf):
    if not getattr(sf, "wired", None):
        raise Skip("needs wiring_import")
    card = dict(sf.wired)
    card["points"] = [dict(p) for p in card["points"]]
    card["points"][2]["signals"] = card["points"][2]["signals"].replace("src.k1!", "10:src.k1!")
    st, body = sf.req("/export/wiring", card, raw=True)
    text = body.decode("utf-8")
    expect(st == 200 and "connect(src.k1!, gain.k1)" in text and text.rstrip().endswith("run(gain)"), text)
    again = sf.post("/import_wiring", {"script": text, "campaign": {"id": "SC-061"}})
    expect(again.get("points") == [[10, "src.sg"], [11, "wave.py"], [20, "gain.py"]], f"round trip {again}")
    card["points"][1]["module"] = "src.sg"
    st, err = sf.req("/export/wiring", card)
    expect(st == 400 and "more than one point" in err.get("error", ""), f"duplicate module: {st} {err}")


@case
def wiring_errors(sf):
    for script, want in (("connect(a.x!, b.y)\nconnect(b.y!, a.x)\nrun(a)\nrun(b)", "feed each other"),
                         ("run(nosuch)", "nosuch: expected one module file"),
                         ("x = 1", "only connect(), run()")):
        st, r = sf.req("/import_wiring", {"script": script, "campaign": {}})
        expect(st == 400 and want in r.get("error", ""), f"{script!r}: {st} {r}")


@case
def busy_port(sf):
    same = subprocess.run(sf.cmd + ["--no-browser", "--port", str(sf.port)], cwd=sf.cwd, env=sf.env,
                          capture_output=True, text=True, timeout=60)
    expect(same.returncode == 0 and "already running in this folder" in same.stdout, f"same folder: {same.returncode} {same.stdout}")
    other = os.path.join(os.path.dirname(sf.cwd), "other")
    os.makedirs(other, exist_ok=True)
    p = subprocess.Popen(sf.cmd + ["--no-browser", "--port", str(sf.port)], cwd=other, env=sf.env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        folder = None
        for _ in range(60):          # the copy in the other folder should be on the next port
            try:
                with urllib.request.urlopen(f"http://localhost:{sf.port + 1}/info", timeout=2) as r:
                    folder = json.load(r)["folder"]
                break
            except OSError:
                time.sleep(0.5)
        expect(folder and os.path.normcase(os.path.realpath(folder)) == os.path.normcase(os.path.realpath(other)),
               f"other folder: port {sf.port + 1} serves {folder}")
    finally:
        p.terminate()
        p.wait(10)


@case
def import_sg_cli(sf):
    folder = os.path.join(os.path.dirname(sf.cwd), "cli")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "gain.sg"), "w", encoding="utf-8") as f:
        f.write(WIRING)
    r = subprocess.run(sf.cmd + ["--import-sg", "gain.sg", "--camp", "SC-070.camp"], cwd=folder, env=sf.env,
                       capture_output=True, text=True, timeout=120)
    expect(r.returncode == 0 and "wrote TC-001.test" in r.stdout, f"--import-sg: {r.returncode} {r.stdout} {r.stderr}")
    with open(os.path.join(folder, "SC-070.camp"), encoding="utf-8") as f:
        camp = json.load(f)
    expect(camp["id"] == "SC-070" and len(camp["live"]) == 2 and len(camp["modules"]) == 3, f"campaign {camp}")


@case
def modules_beside_binary(sf):
    if not sf.frozen:
        raise Skip("binary only")
    proj = os.path.join(os.path.dirname(sf.cwd), "project")
    os.makedirs(os.path.join(proj, "dist"), exist_ok=True)
    exe = os.path.join(proj, os.path.basename(sf.cmd[0]))
    shutil.copy(sf.cmd[0], exe)
    shutil.copy(os.path.join(sf.mods, "emit.py"), os.path.join(proj, "dist"))
    two = Starfire([exe], proj)
    try:
        i = two.get("/info")
        expect(os.path.normcase(os.path.realpath(i["modules"])) == os.path.normcase(os.path.realpath(os.path.join(proj, "dist"))),
               f"binary beside dist/ reads {i['modules']}")
        expect(two.get("/list_modules")["modules"] == ["emit.py"], "modules beside the binary")
    finally:
        two.stop()


def main():
    sys.stdout.reconfigure(errors="backslashreplace")
    args = sys.argv[1:]
    only = args[args.index("-k") + 1] if "-k" in args else ""
    target = os.path.abspath(args[0])
    work = tempfile.mkdtemp(prefix="starfire-regress-")
    frozen = not target.endswith(".py")
    if frozen:
        prog = os.path.join(work, "app")        # the binary in a project folder, modules beside it
        os.makedirs(prog)
        shutil.copy(target, prog)
        mods, cmd = prog, [os.path.join(prog, os.path.basename(target))]
    else:
        src, prog = os.path.dirname(target), os.path.join(work, "app")
        shutil.copytree(os.path.join(src, "static"), os.path.join(prog, "static"))
        for f in ("starfire.py", "signet.py"):
            shutil.copy(os.path.join(src, f), prog)
        mods, cmd = os.path.join(prog, "dist"), [sys.executable, os.path.join(prog, "starfire.py")]
        os.makedirs(mods)
    for name, text in MODULES.items():
        with open(os.path.join(mods, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    cwd = os.path.join(work, "cwd")
    os.makedirs(cwd)
    print(f"target {target}\nwork   {work}", flush=True)
    sf = Starfire(cmd, cwd)
    sf.mods, sf.frozen = mods, frozen
    passed = failed = skipped = 0
    try:
        for fn in results:
            if only and only not in fn.__name__:
                continue
            t0 = time.monotonic()
            try:
                fn(sf)
                passed += 1
                print(f"PASS {fn.__name__} ({time.monotonic() - t0:.1f} s)", flush=True)
            except Skip as e:
                skipped += 1
                print(f"SKIP {fn.__name__}: {e}", flush=True)
            except Exception as e:
                failed += 1
                print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}", flush=True)
    finally:
        sf.stop()
        if failed:
            with open(os.path.join(cwd, "server.log"), encoding="utf-8") as f:
                print("---- server log (last 60 lines) ----\n" + "".join(f.readlines()[-60:]))
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
