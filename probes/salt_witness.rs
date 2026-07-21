//! ARO benchmark probe — production-style Salt witness creation, verification, and replay.
use salt::{hasher, EphemeralSaltState, MemStore, StateRoot, StateUpdates, TrieUpdates, Witness};
use std::{collections::BTreeMap, time::Instant};

type PlainBatch = BTreeMap<Vec<u8>, Option<Vec<u8>>>;

fn key(index: u64) -> Vec<u8> {
    let mut key = b"evm:account:".to_vec();
    key.extend_from_slice(&index.to_be_bytes());
    key.extend_from_slice(b":storage:");
    key.extend_from_slice(&index.wrapping_mul(0x9e37_79b9_7f4a_7c15).to_le_bytes());
    key
}

fn value(epoch: u64, index: u64) -> Vec<u8> {
    let mut value = vec![0u8; 48];
    for (position, byte) in value.iter_mut().enumerate() {
        *byte = index
            .wrapping_mul(0xd6e8_feb8_6659_fd93)
            .wrapping_add(epoch.wrapping_mul(0xa5a5_a5a5_a5a5_a5a5))
            .rotate_left((position & 63) as u32) as u8;
    }
    value
}

struct Workload {
    store: MemStore,
    lookups: Vec<Vec<u8>>,
    updates: PlainBatch,
    bucket_ids: Vec<u32>,
}

fn workload() -> Workload {
    let store = MemStore::new();
    let initial: PlainBatch = (0..96u64)
        .map(|index| (key(index), Some(value(0, index))))
        .collect();
    let initial_updates = EphemeralSaltState::new(&store)
        .cache_read()
        .update_fin(initial.iter())
        .expect("build initial Salt state");
    store.update_state(initial_updates.clone());
    let mut initial_trie = StateRoot::new(&store);
    let (_, trie_updates) = initial_trie
        .update_fin(&initial_updates)
        .expect("build initial Salt trie");
    store.update_trie(trie_updates);

    let lookups = (0..16u64)
        .map(key)
        .chain((256..264u64).map(key))
        .collect::<Vec<_>>();
    let mut updates = PlainBatch::new();
    for index in 0..12u64 {
        updates.insert(key(index), Some(value(1, index)));
    }
    for index in 32..38u64 {
        updates.insert(key(index), None);
    }
    for index in 96..104u64 {
        updates.insert(key(index), Some(value(1, index)));
    }
    let mut bucket_ids = updates
        .keys()
        .chain(lookups.iter())
        .map(|plain_key| hasher::bucket_id(plain_key))
        .collect::<Vec<_>>();
    bucket_ids.sort_unstable();
    bucket_ids.dedup();

    Workload {
        store,
        lookups,
        updates,
        bucket_ids,
    }
}

struct Outcome {
    witness: Witness,
    state_root: [u8; 32],
    verified: bool,
    lookup_values: Vec<Option<Vec<u8>>>,
    replay_updates: StateUpdates,
    next_root: [u8; 32],
    next_trie: TrieUpdates,
}

fn run_once(workload: &Workload) -> Outcome {
    let witness = Witness::create(
        workload.bucket_ids.iter().copied(),
        &workload.lookups,
        &workload.updates,
        &workload.store,
    )
    .expect("create production-style witness");
    let state_root = witness.state_root().expect("witness state root");
    let verified = witness.verify().is_ok();
    assert!(verified, "verify witness");

    let mut reader = EphemeralSaltState::new(&witness);
    let lookup_values = workload
        .lookups
        .iter()
        .map(|plain_key| reader.plain_value(plain_key).expect("witness lookup"))
        .collect();
    let replay_updates = EphemeralSaltState::new(&witness)
        .cache_read()
        .update_fin(workload.updates.iter())
        .expect("replay state transition from witness");
    let mut next_trie_builder = StateRoot::new(&witness);
    let (next_root, next_trie) = next_trie_builder
        .update_fin(&replay_updates)
        .expect("replay trie transition from witness");

    Outcome {
        witness,
        state_root,
        verified,
        lookup_values,
        replay_updates,
        next_root,
        next_trie,
    }
}

fn consume(outcome: Outcome) {
    std::hint::black_box((
        outcome.witness,
        outcome.state_root,
        outcome.verified,
        outcome.lookup_values,
        outcome.replay_updates,
        outcome.next_root,
        outcome.next_trie,
    ));
}

fn main() {
    let workload = workload();
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let secs: f64 = args[1].parse().unwrap_or(8.0);
        let start = Instant::now();
        while start.elapsed().as_secs_f64() < secs {
            consume(run_once(std::hint::black_box(&workload)));
        }
        return;
    }
    let scale: u64 = std::env::var("ARO_BENCH_SCALE")
        .ok()
        .and_then(|scale| scale.parse().ok())
        .unwrap_or(1);
    let calls = scale;
    consume(run_once(&workload));
    let start = Instant::now();
    for _ in 0..calls {
        consume(run_once(std::hint::black_box(&workload)));
    }
    let ns = start.elapsed().as_nanos() as f64 / calls as f64;
    println!("BENCH {ns} ns_per_call iters={calls} scale={scale}");
}
