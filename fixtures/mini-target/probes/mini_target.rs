// ARO bench probe for the mini-target fixture. Prints one `BENCH s1 s2 ...` line
// (ns per checksum() call, 5 samples). Scale-aware: ARO_BENCH_SCALE multiplies the
// batch size so higher scales average more work per sample (a lower A/A floor).
use std::time::Instant;

fn main() {
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let n = 512usize;
    let xs: Vec<u64> = (0..n as u64)
        .map(|i| i.wrapping_mul(0x9E37_79B9_7F4A_7C15))
        .collect();
    let reps = 40 * scale;
    let mut samples: Vec<f64> = Vec::new();
    let mut sink = 0u64;
    for _ in 0..5 {
        let t = Instant::now();
        for _ in 0..reps {
            sink = sink.wrapping_add(mini_target::checksum(&xs));
        }
        samples.push(t.elapsed().as_nanos() as f64 / reps as f64);
    }
    if sink == 42 {
        eprintln!("."); // consume the sink so the timed loop cannot be optimized away
    }
    let line = samples
        .iter()
        .map(|s| format!("{:.1}", s))
        .collect::<Vec<_>>()
        .join(" ");
    println!("BENCH {}", line);
}
