// tvac.rs:        THERMAL VACUUM PROFILE
// AUTHOR:         DANIEL DESAI
// UPDATED:        2026-10-17
// VERSION:        0.2.0
//
// Thermal profile visualization and editing tool. Time and temperature vectors, linearly
// interpolated to represent ramps/plateaus. An accelerated simulation mode (--sim) runs the
// profile in 30 s.
//
// Works on its own or as a Signet module. Run from Signet/Starfire, the optional inputs `time`
// and `temp` (vector::float, s and C), `baseline` (C), `vacuum` and `ambient` (torr) fill the
// fields (you still ARM and START), and when the run ends the outputs `time`, `temp` and
// `pressure` (the setpoint log, vector::float) are written as signals. Run on its own, no
// signals are read or written.
//
// Usage:
// 1. Build with cargo build --release. Lives in src/bin/ with signet.rs (the signal helper);
//    Cargo.toml lists the programs.
// 2. Copy the binary to the Starfire/dist folder (e.g. mv target/release/tvac ../../../dist/tvac).
// 3. Run as ./dist/tvac (realtime mode) or ./dist/tvac --sim (accelerated simulation mode),
//    or as a Starfire module.
// 3. Enter a thermal profile by either:
//    a. Type the vectors: time in seconds, temperature in degrees Celsius. 
//       Brackets, commas, semicolons and "..." are ignored,
//       so a vector such as [20 40 40 -40 -40] can be pasted directly.
//    b. Type a value in the box after + and press +; ++4H/++24H and --10C/++10C append the
//       last entry incremented by that value. An empty vector starts as t = 0 / T = baseline.
//    c. Enter a CSV path and press LOAD. The first two numeric columns are time and temperature;
//       header and # lines are skipped.
// 4. Set BASELINE (C), the temperature at t = 0 (default 20). A vector T value at index 0 overrides it.
// 5. Set VACUUM (default 1e-5 torr) and AMBIENT, the pressure above vacuum (default 760 torr).
// 6. Press ARM.
// 7. Press START. STOP, closing the window, or SIGTERM/SIGINT ends the run early.
//    The window stays open after DONE/STOPPED; close it to exit (code 0 = DONE, 1 = STOPPED).
// 8. EXPORT CSV writes the profile breakpoints; EXPORT PNG saves a light-mode plot.
//    Both go to the working directory as tvac_profile.csv / tvac_profile.png.
// 9. With --sim, EXPORT TEST writes a Starfire test card (TV-001.test): pump down to VACUUM, one point per
//    segment ("Ramp to <T> C" or "Hold plateau at <T> C for HH:MM:SS"), then vent to AMBIENT.
//    Note pump down/vent are not included in the thermal profile, CSV or PNG.



#[path = "signet.rs"]
#[allow(dead_code)] // the helper has more than this module uses
mod signet; // Signet's signal helper, in src/bin/ beside this file

// Run from Signet or Starfire (outputs are written only then)
fn under_signet() -> bool {
    std::env::var_os("SIGNET_MODULE").or_else(|| std::env::var_os("STARFIRE_MODULE")).is_some()
}

use eframe::egui::{self, Align, Color32, FontId, RichText, Stroke, Vec2};
use egui_plot::{HLine, HoverPosition, Line, Plot, PlotPoint, PlotPoints, Text, VLine};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const DURATION_MAX: f64 = 604800.0; // s (7 days)
const SAMPLE_TIME: f64 = 10.0; // s
const SIM_RUN: f64 = 30.0; // s, wall-clock length of a --sim run
const TEMP_MIN: f64 = -180.0; // C
const TEMP_MAX: f64 = 150.0; // C
const BASELINE: f64 = 20.0; // C, default T at t = 0
const TORR_PA: f64 = 101325.0 / 760.0; // Pa per torr
// NVIS colors: sRGB points nearest the MIL-STD-3009 Table II (u', v') centers.
// Green A and B lie just outside sRGB; these are the nearest gamut-edge points.
const BG: Color32 = Color32::from_rgb(0x08, 0x09, 0x08);
const AMB: Color32 = Color32::from_rgb(0xFF, 0xB0, 0x00); // NVIS Yellow
const GRN: Color32 = Color32::from_rgb(0x1F, 0xFF, 0x01); // NVIS Green B
const RED: Color32 = Color32::from_rgb(0xFF, 0x0B, 0x00); // NVIS Red
const FG: Color32 = Color32::from_rgb(0x01, 0xFF, 0x5B); // NVIS Green A
const DIM: Color32 = Color32::from_rgb(0x00, 0x7C, 0x28); // NVIS Green A, 20% luminance
const BLU: Color32 = Color32::from_rgb(0x1E, 0x90, 0xFF); // plot only; no NVIS blue
const MAG: Color32 = Color32::from_rgb(0x8F, 0x00, 0x8F); // plot only; no NVIS magenta
const WHT: Color32 = Color32::from_rgb(0xEA, 0xFF, 0xD7); // NVIS White: titles, labels, box outlines
const GRY: Color32 = Color32::from_rgb(0x8A, 0x8A, 0x8A); // plot grid, ticks, baseline
const CSV_OUT: &str = "tvac_profile.csv";
const PNG_OUT: &str = "tvac_profile.png";
const TEST_ID: &str = "TV-001";
const TEST_OUT: &str = "TV-001.test";
const PNG_SIZE: (u32, u32) = (1200, 540);
const PLOT_W: f32 = 600.0;
const PLOT_H: f32 = 240.0;

static EXIT: AtomicI32 = AtomicI32::new(1);

static SIM_SP: AtomicU64 = AtomicU64::new(0); // Represents the chamber controller setpoint (f64 bits)

// write("SOUR:TEMP {v}") etc., with a real controller
fn set_sp(v: f64) {
    SIM_SP.store(v.to_bits(), Ordering::SeqCst);
}

fn parse_vec(text: &str) -> Option<Vec<f64>> {
    let v: Option<Vec<f64>> = text
        .split(|c: char| c.is_whitespace() || ",;[]".contains(c))
        .filter(|s| !s.is_empty() && *s != "...")
        .map(|s| s.parse().ok())
        .collect();
    v.filter(|v| !v.is_empty())
}

fn is_num(text: &str) -> bool {
    text.trim().parse::<f64>().is_ok()
}

fn join(v: &[f64]) -> String {
    v.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" ")
}

fn abs(path: &str) -> String {
    std::path::absolute(path).map_or(path.into(), |p| p.display().to_string())
}

fn to_torr(v: f64, pa: bool) -> f64 {
    if pa { v / TORR_PA } else { v }
}

fn unit_name(pa: bool) -> &'static str {
    if pa { "Pa" } else { "torr" }
}

// Plain for 1 <= |v| < 1e6 (760, 101325), exponent otherwise (1e-5).
fn pres(v: f64) -> String {
    if (1.0..1e6).contains(&v.abs()) { format!("{v}") } else { format!("{v:e}") }
}

fn font(s: &str, c: Color32) -> RichText {
    RichText::new(s).monospace().size(16.0).color(c)
}

fn entry(text: &mut String, w: f32, ok: bool) -> impl egui::Widget + '_ {
    egui::TextEdit::singleline(text)
        .desired_width(w)
        .font(FontId::monospace(16.0))
        .text_color(if ok { AMB } else { RED })
}

fn btn(text: &str, color: Color32, w: f32) -> impl egui::Widget {
    egui::Button::new(font(text, color))
        .fill(BG)
        .stroke(Stroke::new(1.0, WHT))
        .min_size(Vec2::new(w, 30.0))
}

#[derive(Clone, PartialEq)]
struct Profile {
    t: Vec<f64>,
    temp: Vec<f64>,
    vac: f64,
    pa: bool,
    amb: f64,
    amb_pa: bool,
}

impl Profile {
    // Linear interpolation; holds the end values outside the profile.
    fn at(&self, x: f64) -> f64 {
        let i = self.t.partition_point(|&t| t <= x).clamp(1, self.t.len() - 1);
        let (t0, t1, y0, y1) = (self.t[i - 1], self.t[i], self.temp[i - 1], self.temp[i]);
        y0 + (y1 - y0) * ((x - t0) / (t1 - t0)).clamp(0.0, 1.0)
    }

    fn end(&self) -> f64 {
        self.t[self.t.len() - 1]
    }

    fn unit(&self) -> &'static str {
        unit_name(self.pa)
    }
}

struct App {
    times: String,
    temps: String,
    add_t: String,
    add_temp: String,
    csv: String,
    base: String,
    vac: String,
    pa: bool,
    amb: String,
    amb_pa: bool,
    applied: Option<Profile>,
    k: Option<usize>,
    samples: usize,
    period: f64,
    next: Instant,
    over: bool,
    status: (String, Color32),
    term: Arc<AtomicBool>,
    sim: bool,
    trace: Vec<[f64; 2]>,    // [time_s, temp_C] run so far
    vac_torr: f64,           // vacuum level of the armed run, for the pressure output
}

impl App {
    fn say(&mut self, text: &str, color: Color32) {
        self.status = (text.to_string(), color);
    }

    fn values(&self) -> Result<Profile, String> {
        let mut t = parse_vec(&self.times).ok_or("Bad time vector")?;
        let mut temp = parse_vec(&self.temps).ok_or("Bad temperature vector")?;
        let base: f64 = self.base.trim().parse().map_err(|_| "Bad baseline")?;
        let vac: f64 = self.vac.trim().parse().map_err(|_| "Bad vacuum level")?;
        let amb: f64 = self.amb.trim().parse().map_err(|_| "Bad ambient pressure")?;
        if t.len() != temp.len() {
            return Err("Vectors must be the same length".into());
        }
        if t[0] != 0.0 {
            t.insert(0, 0.0);
            temp.insert(0, base);
        }
        if t.len() < 2 {
            return Err("Need a point after t = 0".into());
        }
        if t.windows(2).any(|w| w[1] <= w[0]) {
            return Err("Time must be >= 0 and increase".into());
        }
        if t[t.len() - 1] > DURATION_MAX {
            return Err(format!("Profile longer than {} s", DURATION_MAX as i64));
        }
        if temp.iter().any(|y| !(TEMP_MIN..=TEMP_MAX).contains(y)) {
            return Err(format!("Temps must be {} to {} C", TEMP_MIN, TEMP_MAX));
        }
        if !(vac > 0.0 && to_torr(vac, self.pa) <= to_torr(amb, self.amb_pa)) {
            return Err("Vacuum must be > 0 and <= ambient".into());
        }
        Ok(Profile { t, temp, vac, pa: self.pa, amb, amb_pa: self.amb_pa })
    }

    // Append the typed value (inc = None) or last entry + inc. An empty vector starts at t = 0 / baseline.
    fn push(&mut self, time: bool, inc: Option<f64>) {
        let first = if time { 0.0 } else { self.base.trim().parse().unwrap_or(BASELINE) };
        let field = if time { &self.times } else { &self.temps };
        let blank = field.chars().all(|c| c.is_whitespace() || "[],;.".contains(c));
        let last = match parse_vec(field) {
            Some(v) => v[v.len() - 1],
            None if blank => first,
            None => return self.say("Fix the vector first", RED),
        };
        let v = match inc {
            Some(d) => last + d,
            None => match (if time { &self.add_t } else { &self.add_temp }).trim().parse::<f64>() {
                Ok(v) => v,
                Err(_) => return self.say("Enter a value to append", RED),
            },
        };
        let field = if time { &mut self.times } else { &mut self.temps };
        *field = if blank { format!("{first} {v}") } else { format!("{} {v}", field.trim()) };
        if inc.is_none() {
            if time { self.add_t.clear() } else { self.add_temp.clear() }
        }
        self.say("", FG);
    }

    // Profile breakpoints; LOAD reads this back.
    fn export_csv(&mut self) {
        let p = match self.values() {
            Ok(p) => p,
            Err(e) => return self.say(&e, RED),
        };
        let mut out = format!("# tvac profile vacuum={:.3e} {} ambient={:.3e} {}\ntime_s,temp_C\n",
                              p.vac, p.unit(), p.amb, unit_name(p.amb_pa));
        for (t, y) in p.t.iter().zip(&p.temp) {
            out += &format!("{t},{y}\n");
        }
        match std::fs::write(CSV_OUT, out) {
            Ok(()) => self.say(&format!("EXPORTED {}", abs(CSV_OUT)), FG),
            Err(e) => self.say(&format!("CSV: {e}"), RED),
        }
    }

    fn export_test(&mut self) {
        let p = match self.values() {
            Ok(p) => p,
            Err(e) => return self.say(&e, RED),
        };
        match std::fs::write(TEST_OUT, test_card(&p)) {
            Ok(()) => self.say(&format!("EXPORTED {}", abs(TEST_OUT)), FG),
            Err(e) => self.say(&format!("TEST: {e}"), RED),
        }
    }

    fn export_png(&mut self) {
        let Some(p) = self.shown() else {
            return self.say("INVALID PROFILE", RED);
        };
        match draw_png(&p, &self.trace) {
            Ok(()) => self.say(&format!("EXPORTED {}", abs(PNG_OUT)), FG),
            Err(e) => self.say(&format!("PNG: {e}"), RED),
        }
    }

    // Editable preview until running; the armed profile once running.
    fn shown(&self) -> Option<Profile> {
        if self.k.is_some() || self.over { self.applied.clone() } else { self.values().ok() }
    }

    fn load_csv(&mut self) {
        let text = match std::fs::read_to_string(self.csv.trim()) {
            Ok(text) => text,
            Err(e) => return self.say(&format!("CSV: {e}"), RED),
        };
        let (mut t, mut temp) = (Vec::new(), Vec::new());
        for line in text.lines().filter(|l| !l.trim_start().starts_with('#')) {
            let mut f = line.split(|c| ",;\t".contains(c)).map(|s| s.trim().parse::<f64>());
            if let (Some(Ok(a)), Some(Ok(b))) = (f.next(), f.next()) {
                t.push(a);
                temp.push(b);
            }
        }
        if t.is_empty() {
            return self.say("CSV: no numeric rows", RED);
        }
        self.times = join(&t);
        self.temps = join(&temp);
        self.say(&format!("LOADED {} POINTS", t.len()), FG);
    }

    // Switch the unit of the vacuum or ambient box, converting its value.
    fn set_unit(&mut self, vac: bool, pa: bool) {
        let (text, cur) = if vac { (&mut self.vac, &mut self.pa) } else { (&mut self.amb, &mut self.amb_pa) };
        if pa == *cur {
            return;
        }
        if let Ok(v) = text.trim().parse::<f64>() {
            let x = if pa { v * TORR_PA } else { v / TORR_PA };
            *text = format!("{:e}", format!("{x:.5e}").parse::<f64>().unwrap_or(x)); // 6 significant digits
        }
        *cur = pa;
    }

    fn apply(&mut self) {
        if self.k.is_some() {
            return;
        }
        match self.values() {
            Err(e) => self.say(&e, RED),
            Ok(p) => {
                self.applied = Some(p);
                self.say("ARMED", AMB);
            }
        }
    }

    fn tick(&mut self) {
        let k = self.k.unwrap_or(0);
        let (time, sp, end, vac) = match &self.applied {
            Some(p) => {
                let time = (k as f64 * SAMPLE_TIME).min(p.end());
                (time, p.at(time), p.end(), p.vac)
            }
            None => return,
        };
        set_sp(sp);
        println!("{:.1},{:.2},{:.3e}", time, sp, vac);
        self.trace.push([time, sp]);
        self.say(&format!("RUNNING {:.0} / {:.0} s  SP {:.1} C", time, end, sp), AMB);
        self.k = Some(k + 1);
        if k + 1 >= self.samples {
            self.end(false);
        } else {
            let dt = Duration::from_secs_f64(self.period);
            // --sim keeps a fixed cadence (catches up between frames); real runs wait a full sample.
            self.next = if self.sim { self.next + dt } else { Instant::now() + dt };
        }
    }

    fn start(&mut self) {
        if self.k.is_some() || self.over {
            return;
        }
        let p = match (self.values(), &self.applied) {
            (Ok(v), Some(a)) if v == *a => v,
            _ => return self.say("ARM first", AMB),
        };
        self.samples = (p.end() / SAMPLE_TIME).ceil() as usize + 1;
        self.period = if self.sim { SIM_RUN / self.samples as f64 } else { SAMPLE_TIME };
        self.trace.clear();
        self.next = Instant::now();
        self.k = Some(0);
        self.vac_torr = to_torr(p.vac, p.pa);
        println!("# profile t_s={:?} temp_C={:?} vacuum={:.3e} {} ambient={:.3e} {}",
                 p.t, p.temp, p.vac, p.unit(), p.amb, unit_name(p.amb_pa));
        println!("time_s,temp_C,pressure_{}", p.unit());
        self.tick();
    }

    fn end(&mut self, aborted: bool) {
        if self.over {
            return;
        }
        self.over = true;
        set_sp(self.applied.as_ref().map_or(BASELINE, |p| p.temp[0])); // back to baseline
        self.k = None;
        println!("{}", if aborted { "# STOPPED" } else { "# DONE" });
        if under_signet() && !self.trace.is_empty() {
            let time: Vec<f64> = self.trace.iter().map(|r| r[0]).collect();
            let temp: Vec<f64> = self.trace.iter().map(|r| r[1]).collect();
            let pressure = vec![self.vac_torr; time.len()];
            signet::put("time", &time, "vector::float");
            signet::put("temp", &temp, "vector::float");
            signet::put("pressure", &pressure, "vector::float");
        }
        EXIT.store(if aborted { 1 } else { 0 }, Ordering::SeqCst);
        self.say(if aborted { "STOPPED" } else { "DONE" }, if aborted { RED } else { GRN });
    }

    fn stop(&mut self) {
        if self.k.is_some() {
            self.end(true);
        } else {
            self.say("NOT RUNNING", DIM);
        }
    }

    fn plot(&self, ui: &mut egui::Ui) {
        let shown = self.shown();
        let axis = |s: &str| RichText::new(s).monospace().size(12.0).color(WHT);
        Plot::new("profile")
            .width(PLOT_W)
            .height(PLOT_H)
            .allow_drag(false)
            .allow_zoom(false)
            .allow_scroll(false)
            .allow_boxed_zoom(false)
            .allow_double_click_reset(false)
            .x_axis_label(axis("TIME (S)"))
            .y_axis_label(axis("TEMP (C)"))
            .label_formatter(|pos| match pos {
                HoverPosition::NearDataPoint { position: p, .. } => Some(format!("{:.0} s\n{:.1} C", p.x, p.y)),
                HoverPosition::Elsewhere { .. } => None,
            })
            .show(ui, |pu| {
                let Some(p) = shown else {
                    return pu.text(Text::new("", PlotPoint::new(0.0, 0.0), axis("INVALID PROFILE").color(RED)));
                };
                let base = p.temp[0];
                let cmd: PlotPoints = p.t.iter().zip(&p.temp).map(|(&t, &y)| [t, y]).collect();
                pu.hline(HLine::new("BASELINE", base).color(GRY).width(1.0));
                pu.line(Line::new("COMMAND", cmd).color(MAG).width(2.0));
                for (c, pts) in runs(&self.trace, base) {
                    pu.line(Line::new("ACTUAL", pts).color(c).width(2.0));
                }
                if let (Some(_), Some(&[t, _])) = (self.k, self.trace.last()) {
                    pu.vline(VLine::new("", t).color(FG).width(1.0));
                }
            });
    }
}

// Starfire test card (.test JSON): pump down, one test point per profile segment, vent.
// {:?} on these plain-ASCII strings is valid JSON.
fn test_card(p: &Profile) -> String {
    let vac = format!("{} {}", pres(p.vac), p.unit());
    let mut steps = vec![(format!("Initiate pump down sequence to {vac}"), format!("p = {vac}"))];
    for i in 1..p.t.len() {
        let (t, prev) = (p.temp[i], p.temp[i - 1]);
        let action = if t == prev {
            let d = (p.t[i] - p.t[i - 1]).round() as u64;
            format!("Hold plateau at {t} C for {:02}:{:02}:{:02}", d / 3600, d / 60 % 60, d % 60)
        } else {
            format!("Ramp to {t} C")
        };
        steps.push((action, format!("T = {t} C  p = {vac}")));
    }
    steps.push(("Initiate vent procedure".into(), format!("p = {} {}", pres(p.amb), unit_name(p.amb_pa))));
    let points: Vec<String> = steps
        .iter()
        .enumerate()
        .map(|(i, (action, expected))| {
            format!(
                r#"    {{
      "seq": {},
      "action": {action:?},
      "expected": {expected:?},
      "observed": "",
      "reference": "",
      "responsible_party": "",
      "date": "",
      "time": "",
      "initial": "",
      "quality": "",
      "locked": false,
      "flagged": false,
      "images": [],
      "files": [],
      "usb_port": "",
      "measure_command": "",
      "measurements": []
    }}"#,
                (i + 1) * 10
            )
        })
        .collect();
    format!(
        r#"{{
  "id": {TEST_ID:?},
  "title": "",
  "revision": "",
  "author": "",
  "date": "",
  "project": "",
  "test_conductor": "",
  "quality_assurance": "",
  "system": "",
  "subsystem": "",
  "test_type": "",
  "article": "",
  "requirements": "",
  "points": [
{}
  ],
  "nomenclature": [],
  "references": [],
  "notes": [],
  "warnings": [],
  "locked": false,
  "files_migrated": true
}}
"#,
        points.join(",\n")
    )
}

// Actual trace: blue below baseline, red above; runs split at the crossing.
fn runs(trace: &[[f64; 2]], base: f64) -> Vec<(Color32, Vec<[f64; 2]>)> {
    let side = |y: f64| if y < base { BLU } else { RED };
    let mut runs: Vec<(Color32, Vec<[f64; 2]>)> = Vec::new();
    let mut add = |a: [f64; 2], b: [f64; 2], c: Color32| match runs.last_mut() {
        Some((rc, pts)) if *rc == c => pts.push(b),
        _ => runs.push((c, vec![a, b])),
    };
    for w in trace.windows(2) {
        let (a, b) = (w[0], w[1]);
        if (a[1] - base) * (b[1] - base) < 0.0 {
            let x = [a[0] + (b[0] - a[0]) * (base - a[1]) / (b[1] - a[1]), base];
            add(a, x, side(a[1]));
            add(x, b, side(b[1]));
        } else {
            add(a, b, side((a[1] + b[1]) / 2.0));
        }
    }
    runs
}

// Drop interior points on the line through their neighbours; thick bitmap lines over thousands of
// tiny segments render jagged.
fn simplify(pts: &[[f64; 2]]) -> Vec<[f64; 2]> {
    let mut out = vec![pts[0]];
    for w in pts.windows(2).skip(1) {
        let (a, b, c) = (out[out.len() - 1], w[0], w[1]);
        let cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
        if cross.abs() > 1e-9 * (c[0] - a[0]).abs().max(1.0) * (1.0 + (c[1] - a[1]).abs()) {
            out.push(b);
        }
    }
    out.push(pts[pts.len() - 1]);
    out
}

// Light-mode plot for EXPORT PNG, drawn off-screen with plotters (the GUI stays dark).
fn draw_png(p: &Profile, trace: &[[f64; 2]]) -> Result<(), Box<dyn std::error::Error>> {
    use plotters::prelude::*;
    let rgb = |c: Color32| RGBColor(c.r(), c.g(), c.b());
    let grey = rgb(GRY);
    let (w, h) = PNG_SIZE;
    let mut buf = vec![0u8; (w * h * 3) as usize];
    {
        let root = BitMapBackend::with_buffer(&mut buf, (w, h)).into_drawing_area();
        root.fill(&WHITE)?;
        let ys = p.temp.iter().copied().chain(trace.iter().map(|q| q[1]));
        let (lo, hi) = ys.fold((f64::MAX, f64::MIN), |(a, b), y| (a.min(y), b.max(y)));
        let pad = ((hi - lo) * 0.05).max(1.0);
        let mut chart = ChartBuilder::on(&root)
            .caption("THERMAL PROFILE", ("sans-serif", 30).into_font().color(&BLACK))
            .margin(20)
            .x_label_area_size(56)
            .y_label_area_size(72)
            .build_cartesian_2d(0.0..p.end(), (lo - pad)..(hi + pad))?;
        chart
            .configure_mesh()
            .x_desc("TIME (S)")
            .y_desc("TEMP (C)")
            .axis_desc_style(("sans-serif", 22).into_font().color(&BLACK))
            .label_style(("sans-serif", 18).into_font().color(&grey))
            .x_label_formatter(&|x| format!("{x:.0}"))
            .y_label_formatter(&|y| format!("{y:.2}").trim_end_matches('0').trim_end_matches('.').to_string())
            .axis_style(grey)
            .bold_line_style(grey.mix(0.5))
            .light_line_style(grey.mix(0.15))
            .draw()?;
        let base = p.temp[0];
        chart.draw_series(LineSeries::new([(0.0, base), (p.end(), base)], grey.stroke_width(1)))?;
        let cmd = p.t.iter().copied().zip(p.temp.iter().copied());
        chart.draw_series(LineSeries::new(cmd, rgb(MAG).stroke_width(2)))?;
        for (c, pts) in runs(trace, base) {
            chart.draw_series(LineSeries::new(simplify(&pts).into_iter().map(|[t, y]| (t, y)), rgb(c).stroke_width(3)))?;
        }
        root.present()?;
    }
    image::save_buffer(PNG_OUT, &buf, w, h, image::ColorType::Rgb8)?;
    Ok(())
}

impl eframe::App for App {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.term.load(Ordering::Relaxed) {
            self.end(true); // SIGTERM/SIGINT (e.g. BREAK from Starfire)
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
        while self.k.is_some() && Instant::now() >= self.next {
            self.tick();
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        if ui.ctx().input(|i| i.viewport().close_requested()) {
            if self.k.is_none() && !self.over {
                EXIT.store(0, Ordering::SeqCst); // closed without a run: not an error
            } else {
                self.end(true); // no-op after DONE/STOPPED
            }
        }
        let dropped = ui.ctx().input(|i| i.raw.dropped_files.first().map(|f| f.path().to_path_buf()));
        if let Some(path) = dropped {
            self.csv = path.display().to_string();
            self.load_csv();
        }
        let mut act = "";
        let mut changed = false;
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BG).inner_margin(24.0))
            .show(ui, |ui| {
                ui.vertical_centered(|ui| {
                    ui.scope(|ui| {
                        // Grey plot grid/ticks/border.
                        let v = ui.visuals_mut();
                        v.widgets.noninteractive.fg_stroke.color = GRY;
                        v.widgets.noninteractive.bg_stroke = Stroke::new(1.0, GRY);
                        ui.label(font("THERMAL PROFILE", WHT));
                        ui.add_space(4.0);
                        self.plot(ui);
                    });
                    ui.add_space(12.0);
                    let pad = |ui: &egui::Ui, w: f32| ((ui.available_width() - w) / 2.0).max(0.0);
                    let label = |ui: &mut egui::Ui, s: &str| {
                        ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                            ui.label(font(s, WHT));
                        });
                    };
                    ui.horizontal(|ui| {
                        ui.add_space(pad(ui, 600.0));
                        egui::Grid::new("fields").num_columns(2).show(ui, |ui| {
                            for (name, vec, add, tag, incs) in [
                                ("TIME (S)", &mut self.times, &mut self.add_t, "+t", ["++4H", "++24H"]),
                                ("TEMP (C)", &mut self.temps, &mut self.add_temp, "+T", ["--10C", "++10C"]),
                            ] {
                                label(ui, name);
                                ui.horizontal(|ui| {
                                    let ok = parse_vec(vec).is_some();
                                    changed |= ui.add(entry(vec, 200.0, ok)).changed();
                                    if ui.add(btn("+", AMB, 30.0)).clicked() {
                                        act = tag;
                                    }
                                    let ok = add.trim().is_empty() || is_num(add);
                                    changed |= ui.add(entry(add, 70.0, ok)).changed();
                                    for inc in incs {
                                        if ui.add(btn(inc, AMB, 70.0)).clicked() {
                                            act = inc;
                                        }
                                    }
                                });
                                ui.end_row();
                            }
                            label(ui, "CSV");
                            ui.horizontal(|ui| {
                                changed |= ui.add(entry(&mut self.csv, 240.0, true)).changed();
                                if ui.add(btn("LOAD", WHT, 66.0)).clicked() {
                                    act = "csv";
                                }
                            });
                            ui.end_row();
                            ui.label("");
                            ui.end_row();
                            label(ui, "BASELINE (C)");
                            let ok = is_num(&self.base);
                            changed |= ui.add(entry(&mut self.base, 70.0, ok)).changed();
                            ui.end_row();
                            label(ui, "VACUUM");
                            ui.horizontal(|ui| {
                                let ok = is_num(&self.vac);
                                changed |= ui.add(entry(&mut self.vac, 120.0, ok)).changed();
                                let (mut pa, mut torr) = (self.pa, !self.pa);
                                if ui.checkbox(&mut pa, font("Pa", WHT)).clicked() {
                                    act = "vpa";
                                }
                                if ui.checkbox(&mut torr, font("torr", WHT)).clicked() {
                                    act = "vtorr";
                                }
                            });
                            ui.end_row();
                            label(ui, "AMBIENT");
                            ui.horizontal(|ui| {
                                let ok = is_num(&self.amb);
                                changed |= ui.add(entry(&mut self.amb, 120.0, ok)).changed();
                                let (mut pa, mut torr) = (self.amb_pa, !self.amb_pa);
                                if ui.checkbox(&mut pa, font("Pa", WHT)).clicked() {
                                    act = "apa";
                                }
                                if ui.checkbox(&mut torr, font("torr", WHT)).clicked() {
                                    act = "atorr";
                                }
                            });
                            ui.end_row();
                        });
                    });
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        ui.add_space(pad(ui, 304.0));
                        for (text, color) in [("ARM", AMB), ("START", GRN), ("STOP", WHT)] {
                            if ui.add(btn(text, color, 96.0)).clicked() {
                                act = text;
                            }
                        }
                    });
                    ui.add_space(4.0);
                    ui.horizontal(|ui| {
                        // EXPORT TEST (Starfire test card) is --sim only.
                        let exports: &[&str] = if self.sim { &["EXPORT CSV", "EXPORT PNG", "EXPORT TEST"] } else { &["EXPORT CSV", "EXPORT PNG"] };
                        ui.add_space(pad(ui, 138.0 * exports.len() as f32 - 8.0));
                        for &text in exports {
                            if ui.add(btn(text, WHT, 130.0)).clicked() {
                                act = text;
                            }
                        }
                    });
                    ui.add_space(10.0);
                    ui.label(font(&self.status.0, self.status.1));
                });
            });
        if changed && self.k.is_none() {
            self.say("", FG);
        }
        match act {
            "+t" => self.push(true, None),
            "+T" => self.push(false, None),
            "++4H" => self.push(true, Some(4.0 * 3600.0)),
            "++24H" => self.push(true, Some(24.0 * 3600.0)),
            "--10C" => self.push(false, Some(-10.0)),
            "++10C" => self.push(false, Some(10.0)),
            "EXPORT CSV" => self.export_csv(),
            "EXPORT PNG" => self.export_png(),
            "EXPORT TEST" => self.export_test(),
            "csv" => self.load_csv(),
            "vpa" => self.set_unit(true, true),
            "vtorr" => self.set_unit(true, false),
            "apa" => self.set_unit(false, true),
            "atorr" => self.set_unit(false, false),
            "ARM" => self.apply(),
            "START" => self.start(),
            "STOP" => self.stop(),
            _ => {}
        }
        let fast = self.sim && self.k.is_some();
        ui.ctx().request_repaint_after(Duration::from_millis(if fast { 16 } else { 100 }));
    }
}

fn main() {
    let term = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, term.clone()).ok();
    }
    // EXPORT PNG text in Ubuntu Light (egui's proportional font); plotters' ab_glyph text ignores
    // glyph bearings, which leaves visible gaps in a monospace font.
    plotters::style::register_font("sans-serif", plotters::style::FontStyle::Normal, epaint_default_fonts::UBUNTU_LIGHT).ok();
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("TVAC")
            .with_inner_size([680.0, 672.0])
            .with_resizable(false),
        ..Default::default()
    };
    let result = eframe::run_native("TVAC", options, Box::new(move |cc| {
        let mut visuals = egui::Visuals::dark();
        visuals.panel_fill = BG;
        visuals.extreme_bg_color = BG;
        visuals.text_cursor.stroke = Stroke::new(2.0, AMB);
        visuals.widgets.inactive.bg_stroke = Stroke::new(1.0, WHT);
        // Entry/button/checkbox outlines and checkmarks NVIS White; selection and dim text NVIS Green A.
        visuals.widgets.noninteractive.fg_stroke = Stroke::new(1.0, DIM);
        for w in [&mut visuals.widgets.inactive, &mut visuals.widgets.hovered, &mut visuals.widgets.active] {
            w.fg_stroke = Stroke::new(1.5, WHT);
        }
        visuals.selection.bg_fill = DIM;
        visuals.selection.stroke = Stroke::new(1.0, FG);
        visuals.window_fill = BG;
        visuals.window_stroke = Stroke::new(1.0, DIM);
        cc.egui_ctx.set_visuals(visuals);
        // Signet inputs, if connected; any not connected keep the defaults. A wrong type ends the module (exit 2).
        let num = |n: &str, d: &str| if signet::has(n) { format!("{:e}", signet::get_f64(n)) } else { d.to_string() };
        let (times, temps) = if signet::has("time") && signet::has("temp") {
            (join(&signet::get_vec_f64("time")), join(&signet::get_vec_f64("temp")))
        } else {
            ("0 6000 24400".into(), "20 40 40".into())
        };
        Ok(Box::new(App {
            times,
            temps,
            add_t: String::new(),
            add_temp: String::new(),
            csv: String::new(),
            base: if signet::has("baseline") { format!("{}", signet::get_f64("baseline")) } else { "20".into() },
            vac: num("vacuum", "1e-5"),
            pa: false,
            amb: num("ambient", "760"),
            amb_pa: false,
            applied: None,
            k: None,
            samples: 0,
            period: SAMPLE_TIME,
            next: Instant::now(),
            over: false,
            status: (String::new(), FG),
            term,
            sim: std::env::args().any(|a| a == "--sim"),
            trace: Vec::new(),
            vac_torr: 0.0,
        }))
    }));
    if let Err(e) = result {
        eprintln!("# panel could not open: {e}");
    }
    std::process::exit(EXIT.load(Ordering::SeqCst));
}
