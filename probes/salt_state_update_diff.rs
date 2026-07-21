//! ARO differential probe — same sequencer-style path and workload as timed probe.
//! Fingerprints incremental/canonical/merged state deltas, root, and NodeId-sorted
//! trie updates without relying on unstable Debug or HashMap-derived ordering.
use salt::{EphemeralSaltState, MemStore, SaltValue, StateRoot, StateUpdates, TrieUpdates};
use std::collections::BTreeMap;
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
struct Fingerprint([u64; 4]);
impl Fingerprint {
    fn new() -> Self {
        Self([
            0xcbf2_9ce4_8422_2325,
            0x8422_2325_cbf2_9ce4,
            0x6eed_0e9d_a4d9_4a4f,
            0x9e37_79b9_7f4a_7c15,
        ])
    }
    fn bytes(&mut self, bytes: &[u8]) {
        self.raw(&(bytes.len() as u64).to_le_bytes());
        const P: [u64; 4] = [
            0x1000_0000_01b3,
            0x1000_0000_01e7,
            0x1000_0000_023b,
            0x1000_0000_028d,
        ];
        for (position, byte) in bytes.iter().enumerate() {
            for (lane, prime) in self.0.iter_mut().zip(P) {
                *lane ^= u64::from(*byte).wrapping_add((position as u64).rotate_left(17));
                *lane = lane.wrapping_mul(prime).rotate_left(5);
            }
        }
    }
    fn u64(&mut self, value: u64) {
        self.raw(&value.to_le_bytes());
    }
    fn raw(&mut self, bytes: &[u8]) {
        const P: [u64; 4] = [
            0x1000_0000_01b3,
            0x1000_0000_01e7,
            0x1000_0000_023b,
            0x1000_0000_028d,
        ];
        for byte in bytes {
            for (lane, prime) in self.0.iter_mut().zip(P) {
                *lane = (*lane ^ u64::from(*byte)).wrapping_mul(prime);
            }
        }
    }
}
fn mix_value(fp: &mut Fingerprint, value: &Option<SaltValue>) {
    match value {
        None => fp.bytes(&[0]),
        Some(value) => {
            fp.bytes(&[1]);
            fp.bytes(&value.data[..value.data_len()]);
        }
    }
}
fn mix_updates(fp: &mut Fingerprint, label: &[u8], updates: &StateUpdates) {
    fp.bytes(label);
    fp.u64(updates.data.len() as u64);
    for (key, (old, new)) in &updates.data {
        fp.u64(key.0);
        mix_value(fp, old);
        mix_value(fp, new);
    }
}
fn main() {
    let outcome = run_once(&MemStore::new(), &workload());
    let mut fp = Fingerprint::new();
    fp.u64(outcome.incremental.len() as u64);
    for (index, updates) in outcome.incremental.iter().enumerate() {
        fp.u64(index as u64);
        mix_updates(&mut fp, b"incremental", updates);
    }
    mix_updates(&mut fp, b"canonical", &outcome.canonical);
    mix_updates(&mut fp, b"merged", &outcome.merged);
    fp.bytes(b"root");
    fp.bytes(&outcome.root);
    let mut trie = outcome.trie;
    trie.sort_unstable_by_key(|(node_id, _)| *node_id);
    fp.bytes(b"trie");
    fp.u64(trie.len() as u64);
    for (node_id, (old, new)) in trie {
        fp.u64(node_id);
        fp.bytes(&old);
        fp.bytes(&new);
    }
    print!("DIFF ");
    for lane in fp.0 {
        print!("{lane:016x}");
    }
    println!();
}
