//! ARO benchmark probe — SALT sequencer-style incremental state/trie aggregation.
//! Standalone equivalent: `new(...).cache_read()`; repeated `update` +
//! `StateRoot::update`; separate `canonicalize` update; then `finalize`.
//! `test-bucket-resize` makes canonicalization deterministically non-empty.
use salt::{EphemeralSaltState, MemStore, StateRoot, StateUpdates, TrieUpdates};
use std::{collections::BTreeMap, time::Instant};
type Batch = BTreeMap<Vec<u8>, Option<Vec<u8>>>;

fn key(global: u64) -> Vec<u8> {
    let mut key = vec![0u8; 20];
    key[0..8].copy_from_slice(&global.to_le_bytes());
    key[8..16].copy_from_slice(&global.wrapping_mul(0x9e37_79b9_7f4a_7c15).to_be_bytes());
    key[16..20].copy_from_slice(&(global as u32).rotate_left(13).to_le_bytes());
    key
}
fn value(batch: u64, global: u64) -> Vec<u8> {
    let mut value = vec![0u8; 40];
    for (i, byte) in value.iter_mut().enumerate() {
        *byte = global
            .wrapping_mul(0xd6e8_feb8_6659_fd93)
            .wrapping_add(batch.wrapping_mul(17))
            .rotate_left((i & 63) as u32) as u8;
    }
    value
}
fn workload() -> Vec<Batch> {
    let mut batches = Vec::new();
    for batch_id in 0..5u64 {
        let mut batch = Batch::new();
        for item in 0..96u64 {
            let global = batch_id * 96 + item;
            batch.insert(key(global), Some(value(batch_id, global)));
        }
        batches.push(batch);
    }
    let mut mutations = Batch::new();
    for global in 0..96u64 {
        let next = if global % 11 == 0 {
            None
        } else {
            Some(value(99, global))
        };
        mutations.insert(key(global), next);
    }
    batches.push(mutations);
    batches
}
struct Outcome {
    incremental: Vec<StateUpdates>,
    canonical: StateUpdates,
    merged: StateUpdates,
    root: [u8; 32],
    trie: TrieUpdates,
}
fn run_once(store: &MemStore, batches: &[Batch]) -> Outcome {
    let mut state = EphemeralSaltState::new(store).cache_read();
    let mut state_root = StateRoot::new(store);
    let mut incremental = Vec::with_capacity(batches.len());
    let mut merged = StateUpdates::default();
    for batch in batches {
        let updates = state
            .update(batch.iter())
            .expect("incremental state update");
        state_root
            .update(&updates)
            .expect("incremental trie update");
        merged.merge(updates.clone());
        incremental.push(updates);
    }
    let canonical = state.canonicalize().expect("canonicalize state");
    if !canonical.data.is_empty() {
        state_root
            .update(&canonical)
            .expect("canonical trie update");
    }
    merged.merge(canonical.clone());
    let (root, trie) = state_root.finalize().expect("finalize state root");
    Outcome {
        incremental,
        canonical,
        merged,
        root,
        trie,
    }
}
fn main() {
    let store = MemStore::new();
    let batches = workload();
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let start = Instant::now();
        while start.elapsed().as_secs_f64() < secs {
            let o = std::hint::black_box(run_once(&store, &batches));
            std::hint::black_box((o.incremental, o.canonical, o.merged, o.root, o.trie));
        }
        return;
    }
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let calls = 2 * scale;
    std::hint::black_box(run_once(&store, &batches));
    let start = Instant::now();
    for _ in 0..calls {
        std::hint::black_box(run_once(&store, &batches));
    }
    let ns = start.elapsed().as_nanos() as f64 / calls as f64;
    println!("BENCH {ns} ns_per_call iters={calls} scale={scale}");
}
