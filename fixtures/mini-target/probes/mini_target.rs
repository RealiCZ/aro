// ARO bench probe for the mini-target fixture, and the template every new probe
// should copy. Two modes (the harness-protocol contract):
//   no args           bench: one `BENCH s1 s2 ...` line, 5 samples of ns per
//                     checksum() call; ARO_BENCH_SCALE multiplies the batch so
//                     higher scales average more work per sample (lower A/A floor)
//   <secs> (argv[1])  spin: run the SAME workload continuously until the deadline
//                     and print `SPUN <n>` — the profiler samples the running
//                     process to build the frontier map
use std::time::{Duration, Instant};

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
    let mut sink = 0u64;

    if let Some(secs) = std::env::args().nth(1).and_then(|s| s.parse::<u64>().ok()) {
        // Spin mode: keep the hot loop running so the sampler can attach.
        let deadline = Instant::now() + Duration::from_secs(secs);
        let mut spins = 0u64;
        while Instant::now() < deadline {
            for _ in 0..reps {
                sink = sink.wrapping_add(mini_target::checksum(&xs));
            }
            spins += 1;
        }
        if sink == 42 {
            eprintln!(".");
        }
        println!("SPUN {}", spins);
        return;
    }

    let mut samples: Vec<f64> = Vec::new();
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
