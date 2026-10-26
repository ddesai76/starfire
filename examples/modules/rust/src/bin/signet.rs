// signet.rs:    SIGNET SIGNAL HELPER FOR RUST MODULES IN A CARGO PROJECT (standard library only)
// AUTHOR:       DANIEL DESAI
// UPDATED:      2026-10-25
// VERSION:      0.5.0
//
// Signet's own Rust helper, for modules Signet doesn't build itself (a Cargo project with dependencies).
// Put it in src/bin/ beside the module and say `#[path = "signet.rs"] mod signet;`. Cargo.toml has
// autobins = false, so this file isn't taken for a program. Inputs: signet::get_f64("voltage").
// Outputs: signet::put("power", &power, "float"). Anything wrong stops the module with exit code 2.

use std::collections::HashMap;
use std::sync::OnceLock;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Complex {
    pub re: f64,
    pub im: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Series<V> {
    pub domain: String,
    pub x: Vec<f64>,
    pub values: Vec<V>,
}

struct Record {
    kind: String,
    shape: String,
    tok: Vec<String>,
}

fn fail(msg: &str) -> ! {
    eprintln!("signet: {msg}");
    std::process::exit(2)
}

fn env(key: &str) -> Option<String> {
    std::env::var(format!("SIGNET_{key}")).or_else(|_| std::env::var(format!("STARFIRE_{key}"))).ok()
}

fn unescape(t: &str) -> String {
    if t == "\\e" {
        return String::new();
    }
    let mut s = String::new();
    let mut it = t.chars();
    while let Some(c) = it.next() {
        if c != '\\' {
            s.push(c);
            continue;
        }
        match it.next() {
            Some('s') => s.push(' '),
            Some('t') => s.push('\t'),
            Some('n') => s.push('\n'),
            Some('r') => s.push('\r'),
            Some(o) => s.push(o),
            None => s.push('\\'),
        }
    }
    s
}

fn inputs() -> &'static HashMap<String, Record> {
    static M: OnceLock<HashMap<String, Record>> = OnceLock::new();
    M.get_or_init(|| {
        let mut m = HashMap::new();
        let Some(path) = env("SIGNALS_TXT") else { return m };
        let text = std::fs::read_to_string(&path).unwrap_or_else(|e| fail(&format!("can't read {path}: {e}")));
        for line in text.lines() {
            let f: Vec<&str> = line.splitn(4, '\t').collect();
            if f.len() == 4 {
                let tok = f[3].split(' ').filter(|t| !t.is_empty()).map(String::from).collect();
                m.insert(f[0].to_string(), Record { kind: f[1].into(), shape: f[2].into(), tok });
            }
        }
        m
    })
}

fn rec(name: &str, shape: &str, want: &str) -> &'static Record {
    let r = inputs().get(name).unwrap_or_else(|| fail(&format!("no input {name} (see the point's Signet code)")));
    if r.shape != shape {
        fail(&format!("input {name} is {}, not {want}", r.kind));
    }
    r
}

fn num(t: &str) -> f64 {
    t.parse().unwrap_or(f64::NAN)
}

fn real(name: &str, r: &Record, want: &str) {
    if r.kind == "bool" || r.kind == "text" || r.kind == "vector::bool" || r.kind == "vector::text" {
        fail(&format!("input {name} is {}, not {want}", r.kind));
    }
}

pub fn has(name: &str) -> bool {
    inputs().contains_key(name)
}

/// A number input; null reads as NaN.
pub fn get_f64(name: &str) -> f64 {
    let r = rec(name, "s", "a number");
    real(name, r, "a number");
    num(&r.tok[0])
}

pub fn get_i64(name: &str) -> i64 {
    let r = rec(name, "s", "an integer");
    if r.kind != "int" && r.kind != "hex" {
        fail(&format!("input {name} is {}, not an integer", r.kind));
    }
    r.tok[0].parse().unwrap_or_else(|_| fail(&format!("input {name} is not a whole number")))
}

pub fn get_bool(name: &str) -> bool {
    let r = rec(name, "s", "a bool");
    if r.kind != "bool" {
        fail(&format!("input {name} is {}, not a bool", r.kind));
    }
    r.tok[0] == "true"
}

pub fn get_text(name: &str) -> String {
    let r = rec(name, "s", "text");
    if r.kind != "text" {
        fail(&format!("input {name} is {}, not text", r.kind));
    }
    r.tok.first().map(|t| unescape(t)).unwrap_or_default()
}

pub fn get_complex(name: &str) -> Complex {
    let r = rec(name, "c", "a complex number");
    Complex { re: num(&r.tok[0]), im: num(&r.tok[1]) }
}

pub fn get_vec_f64(name: &str) -> Vec<f64> {
    let r = rec(name, "v", "a vector of numbers");
    real(name, r, "a vector of numbers");
    r.tok.iter().map(|t| num(t)).collect()
}

pub fn get_vec_i64(name: &str) -> Vec<i64> {
    let r = rec(name, "v", "a vector of integers");
    if r.kind != "vector::int" && r.kind != "vector::hex" {
        fail(&format!("input {name} is {}, not a vector of integers", r.kind));
    }
    r.tok.iter().map(|t| t.parse().unwrap_or_else(|_| fail(&format!("input {name} has a value that is not whole")))).collect()
}

pub fn get_vec_text(name: &str) -> Vec<String> {
    let r = rec(name, "v", "a vector of text");
    if r.kind != "vector::text" {
        fail(&format!("input {name} is {}, not a vector of text", r.kind));
    }
    r.tok.iter().map(|t| unescape(t)).collect()
}

pub fn get_vec_complex(name: &str) -> Vec<Complex> {
    let r = rec(name, "vc", "a vector of complex numbers");
    r.tok.chunks(2).map(|p| Complex { re: num(&p[0]), im: num(&p[1]) }).collect()
}

pub fn get_series(name: &str) -> Series<f64> {
    let r = rec(name, "xs", "a real series");
    let n: usize = r.tok[1].parse().unwrap_or(0);
    Series {
        domain: r.tok[0].clone(),
        x: r.tok[2..2 + n].iter().map(|t| num(t)).collect(),
        values: r.tok[2 + n..2 + 2 * n].iter().map(|t| num(t)).collect(),
    }
}

pub fn get_complex_series(name: &str) -> Series<Complex> {
    let r = rec(name, "xc", "a complex series");
    let n: usize = r.tok[1].parse().unwrap_or(0);
    Series {
        domain: r.tok[0].clone(),
        x: r.tok[2..2 + n].iter().map(|t| num(t)).collect(),
        values: r.tok[2 + n..2 + 3 * n].chunks(2).map(|p| Complex { re: num(&p[0]), im: num(&p[1]) }).collect(),
    }
}

fn jnum(d: f64) -> String {
    if !d.is_finite() {
        return "null".into();
    }
    let s = format!("{d:?}");
    if s.contains(['.', 'e', 'E']) { s } else { format!("{s}.0") }
}

fn jstr(t: &str) -> String {
    let mut s = String::from("\"");
    for c in t.chars() {
        match c {
            '"' => s.push_str("\\\""),
            '\\' => s.push_str("\\\\"),
            '\n' => s.push_str("\\n"),
            '\r' => s.push_str("\\r"),
            '\t' => s.push_str("\\t"),
            c if (c as u32) < 0x20 => s.push_str(&format!("\\u{:04x}", c as u32)),
            c => s.push(c),
        }
    }
    s.push('"');
    s
}

fn jlist<T>(v: &[T], f: impl Fn(&T) -> String) -> String {
    format!("[{}]", v.iter().map(f).collect::<Vec<_>>().join(","))
}

fn jcomplexes(v: &[Complex]) -> String {
    format!("{{\"re\":{},\"im\":{}}}", jlist(v, |z| jnum(z.re)), jlist(v, |z| jnum(z.im)))
}

fn floaty(ty: &str) -> bool {
    ty.ends_with("float")
}

/// What `put` can write. The JSON text and the default type of the value.
pub trait Put {
    fn json(&self, ty: &str) -> (String, String);
}

macro_rules! put_float { ($($t:ty),*) => {$(
    impl Put for $t { fn json(&self, _: &str) -> (String, String) { (jnum(*self as f64), "float".into()) } }
    impl Put for Vec<$t> { fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jnum(*e as f64)), "vector::float".into()) } }
    impl Put for [$t] { fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jnum(*e as f64)), "vector::float".into()) } }
)*}}
macro_rules! put_int { ($($t:ty),*) => {$(
    impl Put for $t { fn json(&self, ty: &str) -> (String, String) {
        (if floaty(ty) { jnum(*self as f64) } else { self.to_string() }, "int".into()) } }
    impl Put for Vec<$t> { fn json(&self, ty: &str) -> (String, String) {
        (jlist(self, |e| if floaty(ty) { jnum(*e as f64) } else { e.to_string() }), "vector::int".into()) } }
    impl Put for [$t] { fn json(&self, ty: &str) -> (String, String) {
        (jlist(self, |e| if floaty(ty) { jnum(*e as f64) } else { e.to_string() }), "vector::int".into()) } }
)*}}
put_float!(f64, f32);
put_int!(i8, i16, i32, i64, u8, u16, u32, u64, usize, isize);

impl Put for bool {
    fn json(&self, _: &str) -> (String, String) { (self.to_string(), "bool".into()) }
}
impl Put for str {
    fn json(&self, _: &str) -> (String, String) { (jstr(self), "text".into()) }
}
impl Put for String {
    fn json(&self, _: &str) -> (String, String) { (jstr(self), "text".into()) }
}
impl Put for Complex {
    fn json(&self, _: &str) -> (String, String) { (format!("{{\"re\":{},\"im\":{}}}", jnum(self.re), jnum(self.im)), "complex".into()) }
}
impl Put for Vec<Complex> {
    fn json(&self, _: &str) -> (String, String) { (jcomplexes(self), "vector::complex".into()) }
}
impl Put for [Complex] {
    fn json(&self, _: &str) -> (String, String) { (jcomplexes(self), "vector::complex".into()) }
}
impl Put for Vec<String> {
    fn json(&self, _: &str) -> (String, String) { (jlist(self, |e| jstr(e)), "vector::text".into()) }
}
impl Put for Series<f64> {
    fn json(&self, _: &str) -> (String, String) {
        (format!("{{\"domain\":{},\"x\":{},\"values\":{}}}", jstr(&self.domain), jlist(&self.x, |e| jnum(*e)), jlist(&self.values, |e| jnum(*e))),
         format!("series::{}::float", self.domain))
    }
}
impl Put for Series<Complex> {
    fn json(&self, _: &str) -> (String, String) {
        (format!("{{\"domain\":{},\"x\":{},\"values\":{}}}", jstr(&self.domain), jlist(&self.x, |e| jnum(*e)), jcomplexes(&self.values)),
         format!("series::{}::complex", self.domain))
    }
}

/// Writes output `name` as <prefix>-<module>.<name>.sig, whole and then renamed so a reader never sees half a
/// file. `ty` is the Signet type ("float", "vector::float", ...), or "" for the value's own.
pub fn put<T: Put + ?Sized>(name: &str, value: &T, ty: &str) {
    let (json, own) = value.json(ty);
    let ty = if ty.is_empty() { own } else { ty.to_string() };
    let module = env("MODULE").unwrap_or_else(|| fail("SIGNET_MODULE is not set (run from Signet or Starfire)"));
    let prefix = env("PREFIX").unwrap_or_default();
    let path = if prefix.is_empty() { format!("{module}.{name}.sig") } else { format!("{prefix}-{module}.{name}.sig") };
    let tmp = format!("{path}.tmp");
    std::fs::write(&tmp, format!("{{\"value\":{json},\"type\":{}}}\n", jstr(&ty))).unwrap_or_else(|e| fail(&format!("can't write {tmp}: {e}")));
    std::fs::rename(&tmp, &path).unwrap_or_else(|e| fail(&format!("can't rename {tmp}: {e}")));
}