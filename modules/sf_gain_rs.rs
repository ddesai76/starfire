// signet: in float::k1, in series::time::float::s1, out series::time::float::s2
use signet::{get_f64, get_series, put};

fn main() {
    let k1 = get_f64("k1");
    let mut s = get_series("s1");
    for a in s.values.iter_mut() {
        *a *= k1;
    }
    put("s2", &s, "");
}
