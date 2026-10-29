// hotfire.rs:     EXAMPLE TEST MODULE (ELECTRIC PROPULSION HOT FIRE)
// AUTHOR:         DANIEL DESAI
// UPDATED:        2026-10-27
// VERSION:        0.2.1
//
// Rust version of hotfire.cpp with GUI. User-selected firing pattern (hex code),
// and burn time. Prints voltage/current/accel as CSV (simulated values).
//
// Works on its own or as a Signet module. Run from Signet/Starfire, when the burn ends the outputs
// `voltage`, `current` and `accel` are written as series over time (s), for other modules to take. It reads no signals. Run on its own, none are written.
//
// signet: out series::time::float::voltage, out series::time::float::current, out series::time::float::accel
//
// Usage:
// 1. Build with cargo build --release. Lives in src/bin/ with signet.rs (the signal helper);
//    Cargo.toml lists the programs.
// 2. Copy the binary to the Starfire/dist folder (e.g. mv target/release/hotfire ../../../dist/hotfire). 
//    If there are other binaries with the same name (e.g. generated from hotfire.cpp, save with a different name).
// 3. Add the hotfire module to the desired test point(s) in Starfire.
// 4. Press ACTIVATE (lightning bolt) on a test point, select hotfire, and press RUN.
// 5. Select thrusters to fire by entering hex code; FF = 11111111 (default) fires all 8.
// 6. Select burn time (default 300 s).
// 7. Press ARM.
// 8. Press FIRE.
// 9. Return to the Starfire test point, and press the download icon below the output.
// 10. Save the test card.
// 11. Press BUILD TEST REPORT. Select the .txt output for the test point and press PLOT to add it to the test point.
//


#[path = "signet.rs"]
#[allow(dead_code)] // the helper has more than this module uses
mod signet; // Signet's signal helper, in src/bin/ beside this file

// Run from Signet or Starfire (outputs are written only then)
fn under_signet() -> bool {
    std::env::var_os("SIGNET_MODULE").or_else(|| std::env::var_os("STARFIRE_MODULE")).is_some()
}

use eframe::egui::{self, Align, Color32, Pos2, RichText, Stroke, Vec2};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

const BURN_MAX: f64 = 3600.0; // s
const SAMPLE_TIME: f64 = 1.0; // s
const BG: Color32 = Color32::from_rgb(0x08, 0x09, 0x08);
const AMB: Color32 = Color32::from_rgb(0xFF, 0xB3, 0x00);
const GRN: Color32 = Color32::from_rgb(0x39, 0xFF, 0x14);
const RED: Color32 = Color32::from_rgb(0xFF, 0x28, 0x00);
const FG: Color32 = Color32::from_rgb(0xB8, 0xFF, 0xD0);
const DIM: Color32 = Color32::from_rgb(0x61, 0x8A, 0x6B);
const SIZE: f32 = 340.0;
const R_RING: f32 = 36.0;
const R_CLUSTER: f32 = 105.0;

static EXIT: AtomicI32 = AtomicI32::new(1);

static SIM_REG: AtomicU8 = AtomicU8::new(0); // Represents the thruster register

// Byte store to the register in Assembly (x86-64 and AArch64; plain store elsewhere).
fn set_reg(v: u8) {
    let reg = SIM_REG.as_ptr();
    // ASM
    #[cfg(target_arch = "x86_64")]
    unsafe {
        std::arch::asm!(
            "mov byte ptr [{reg}], {val}",
            "mfence",
            reg = in(reg) reg,
            val = in(reg_byte) v,
            options(nostack),
        );
    }
    #[cfg(target_arch = "aarch64")]
    unsafe {
        std::arch::asm!(
            "strb {val:w}, [{reg}]",
            "dsb sy",
            reg = in(reg) reg,
            val = in(reg) v,
            options(nostack),
        );
    }
    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    unsafe {
        reg.write_volatile(v)
    }
}

fn read_reg() -> u8 {
    SIM_REG.load(Ordering::SeqCst)
}

// xorshift32 jitter in [-1, 1]
struct Rng(u32);

impl Rng {
    fn new() -> Rng {
        Rng(2463534242)
    }

    fn jitter(&mut self) -> f32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 17;
        self.0 ^= self.0 << 5;
        self.0 as f32 / 2147483648.0 - 1.0
    }
}

fn parse_hex(text: &str) -> Option<u8> {
    let t = text.trim();
    let t = t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")).unwrap_or(t);
    u8::from_str_radix(t, 16).ok()
}

struct App {
    code: String,
    secs: String,
    sel: u8,
    applied: Option<(u8, f64)>,
    k: Option<usize>,
    next: Instant,
    close_at: Option<Instant>,
    over: bool,
    status: (String, Color32),
    term: Arc<AtomicBool>,
    rng: Rng,
    sim: bool,
    hold: Option<Instant>,
    log: Vec<[f64; 4]>, // time_s, voltage_V, current_uA, accel_um_s2
}

impl App {
    fn say(&mut self, text: &str, color: Color32) {
        self.status = (text.to_string(), color);
    }

    fn values(&self) -> Option<(u8, f64)> {
        let t = self.secs.trim().parse::<f64>().ok()?;
        let v = parse_hex(&self.code)?;
        (v != 0 && (1.0..=BURN_MAX).contains(&t)).then_some((v, t))
    }

    fn typed(&mut self) {
        if let Some(v) = parse_hex(&self.code) {
            self.sel = v;
        }
        self.say("", FG);
    }

    fn apply(&mut self) {
        match self.values() {
            None => self.say(&format!("Need a hex code 01-FF and 1-{} s", BURN_MAX as i32), RED),
            Some(vals) => {
                self.applied = Some(vals);
                self.say("ARMED", AMB);
            }
        }
    }

    fn tick(&mut self) {
        let (k, n) = (self.k.unwrap_or(0), read_reg().count_ones() as f32); // n = thrusters energized
        let samples = (self.applied.map_or(0.0, |a| a.1) / SAMPLE_TIME) as usize;
        // voltage.push(query("MEAS:VOLT?")) etc., with real instruments
        let v = 1500.0 + 15.0 * self.rng.jitter(); // random: ~1500 V, +/-15 V
        let i = n * (50.0 + 1.0 * self.rng.jitter()); // random: ~50 uA per thruster, +/-1 uA
        let a = n * (20.0 + 0.5 * self.rng.jitter()); // random: ~20 um/s^2 per thruster, +/-0.5
        println!("{:.1},{:.1},{:.2},{:.2}", k as f64 * SAMPLE_TIME, v, i, a);
        self.log.push([k as f64 * SAMPLE_TIME, v as f64, i as f64, a as f64]);
        self.say(&format!("FIRING {} / {} s", k + 1, samples), RED);
        self.k = Some(k + 1);
        if k + 1 >= samples {
            if self.sim {
                // --sim: the data is done; keep the thrusters lit for 1 s, then end.
                self.k = None;
                self.hold = Some(Instant::now() + Duration::from_secs(1));
            } else {
                self.end(false);
            }
        } else {
            self.next = Instant::now() + Duration::from_secs_f64(SAMPLE_TIME);
        }
    }

    fn fire(&mut self) {
        if self.k.is_some() || self.hold.is_some() {
            return;
        }
        let vals = match (self.values(), self.applied) {
            (Some(v), Some(a)) if v == a => v,
            _ => return self.say("ARM first", AMB),
        };
        set_reg(vals.0); // Device register write
        self.k = Some(0);
        self.log.clear();
        println!("# fire=0x{:02X} burn_time={:.0} s", vals.0, vals.1);
        println!("time_s,voltage_V,current_uA,accel_um_s2");
        self.tick();
        // --sim: run the rest of the burn now; the time column is still sim time.
        while self.sim && self.k.is_some() {
            self.tick();
        }
    }

    fn end(&mut self, aborted: bool) {
        if self.over {
            return;
        }
        self.over = true;
        set_reg(0);
        self.k = None;
        println!("{}", if aborted { "# STOPPED" } else { "# DONE" });
        if under_signet() && !self.log.is_empty() {
            let time: Vec<f64> = self.log.iter().map(|r| r[0]).collect();
            for (n, name) in ["voltage", "current", "accel"].iter().enumerate() {
                let values: Vec<f64> = self.log.iter().map(|r| r[n + 1]).collect();
                signet::put(name, &signet::Series { domain: "time".into(), x: time.clone(), values }, "");
            }
        }
        EXIT.store(if aborted { 1 } else { 0 }, Ordering::SeqCst);
        self.say(if aborted { "STOPPED" } else { "DONE" }, if aborted { RED } else { GRN });
        self.close_at = Some(Instant::now() + Duration::from_millis(1500));
    }

    fn stop(&mut self) {
        if self.k.is_some() {
            self.end(true);
        } else {
            self.say("NOT FIRING", DIM);
        }
    }
}

impl eframe::App for App {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if self.term.load(Ordering::Relaxed) {
            self.end(true); // BREAK from Starfire
        }
        if self.k.is_some() && Instant::now() >= self.next {
            self.tick();
        }
        if self.hold.is_some_and(|at| Instant::now() >= at) {
            self.hold = None;
            self.end(false);
        }
        if self.close_at.is_some_and(|at| Instant::now() >= at) {
            ctx.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        if ui.ctx().input(|i| i.viewport().close_requested()) && !self.over {
            ui.ctx().send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.end(true);
        }
        let font = |s: &str, c: Color32| RichText::new(s).monospace().size(16.0).color(c);
        egui::CentralPanel::default()
            .frame(egui::Frame::new().fill(BG).inner_margin(24.0))
            .show(ui, |ui| {
                ui.vertical_centered(|ui| {
                    ui.label(font("THRUSTERS", FG));
                    let (resp, painter) = ui.allocate_painter(Vec2::splat(SIZE), egui::Sense::hover());
                    let c = resp.rect.center();
                    let reg = read_reg();
                    for k in 0..8usize {
                        // Thruster 1 in the centre, 2-8 around it clockwise from the top.
                        let p = if k == 0 {
                            c
                        } else {
                            let a = (-90.0 + (k - 1) as f32 * 360.0 / 7.0).to_radians();
                            Pos2::new(c.x + R_CLUSTER * a.cos(), c.y + R_CLUSTER * a.sin())
                        };
                        let (on, hot) = ((self.sel >> k) & 1 == 1, (reg >> k) & 1 == 1);
                        let color = if on || hot { RED } else { DIM };
                        if hot {
                            painter.circle_filled(p, R_RING, RED);
                        }
                        painter.circle_stroke(p, R_RING, Stroke::new(4.0, color));
                        painter.text(p, egui::Align2::CENTER_CENTER, (k + 1).to_string(),
                                     egui::FontId::monospace(16.0), if hot { BG } else { color });
                    }
                    ui.add_space(12.0);
                    let pad = |ui: &egui::Ui, w: f32| ((ui.available_width() - w) / 2.0).max(0.0);
                    ui.horizontal(|ui| {
                        ui.add_space(pad(ui, 290.0));
                    egui::Grid::new("fields").num_columns(2).show(ui, |ui| {
                        ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                            ui.label(font("PATTERN (HEX CODE)", FG));
                        });
                        let color = if parse_hex(&self.code).is_some() { AMB } else { RED };
                        let edit = egui::TextEdit::singleline(&mut self.code)
                            .desired_width(100.0)
                            .horizontal_align(Align::RIGHT)
                            .font(egui::FontId::monospace(16.0))
                            .text_color(color);
                        if ui.add(edit).changed() {
                            self.typed();
                        }
                        ui.end_row();
                        ui.with_layout(egui::Layout::right_to_left(Align::Center), |ui| {
                            ui.label(font("BURN TIME (S)", FG));
                        });
                        ui.add(egui::TextEdit::singleline(&mut self.secs)
                            .desired_width(100.0)
                            .horizontal_align(Align::RIGHT)
                            .font(egui::FontId::monospace(16.0))
                            .text_color(AMB));
                        ui.end_row();
                    });
                    });
                    ui.add_space(8.0);
                    ui.horizontal(|ui| {
                        ui.add_space(pad(ui, 304.0));
                        for (text, color) in [("ARM", AMB), ("FIRE", RED), ("STOP", FG)] {
                            let b = egui::Button::new(font(text, color))
                                .fill(BG)
                                .stroke(Stroke::new(1.0, FG))
                                .min_size(Vec2::new(96.0, 30.0));
                            if ui.add(b).clicked() {
                                match text {
                                    "ARM" => self.apply(),
                                    "FIRE" => self.fire(),
                                    _ => self.stop(),
                                }
                            }
                        }
                    });
                    ui.add_space(10.0);
                    ui.label(font(&self.status.0, self.status.1));
                });
            });
        ui.ctx().request_repaint_after(Duration::from_millis(100));
    }
}

fn main() {
    let term = Arc::new(AtomicBool::new(false));
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, term.clone()).ok();
    }
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("HOTFIRE")
            .with_inner_size([460.0, 640.0])
            .with_resizable(false),
        ..Default::default()
    };
    let result = eframe::run_native("HOTFIRE", options, Box::new(move |cc| {
        let mut visuals = egui::Visuals::dark();
        visuals.panel_fill = BG;
        visuals.extreme_bg_color = BG;
        visuals.text_cursor.stroke = Stroke::new(2.0, AMB);
        visuals.widgets.inactive.bg_stroke = Stroke::new(1.0, FG);
        cc.egui_ctx.set_visuals(visuals);
        let sel: u8 = 0xFF;
        Ok(Box::new(App {
            code: format!("0x{sel:02X}"),
            secs: "300".into(),
            sel,
            applied: None,
            k: None,
            next: Instant::now(),
            close_at: None,
            over: false,
            status: (String::new(), FG),
            term,
            rng: Rng::new(),
            sim: std::env::args().any(|a| a == "--sim"),
            hold: None,
            log: Vec::new(),
        }))
    }));
    if let Err(e) = result {
        eprintln!("# panel could not open: {e}");
    }
    std::process::exit(EXIT.load(Ordering::SeqCst));
}
