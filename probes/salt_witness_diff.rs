//! ARO differential probe — identical production-style Salt witness workload and path.
//! Fingerprints canonical witness data, proof points/levels, verification, reads, and replay outputs.
use salt::{
    hasher, EphemeralSaltState, MemStore, SaltValue, StateRoot, StateUpdates, TrieUpdates, Witness,
};
use std::collections::BTreeMap;

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

    fn u64(&mut self, value: u64) {
        self.raw(&value.to_le_bytes());
    }
}

fn mix_salt_value(fp: &mut Fingerprint, value: &Option<SaltValue>) {
    match value {
        None => fp.bytes(&[0]),
        Some(value) => {
            fp.bytes(&[1]);
            fp.bytes(&value.data[..value.data_len()]);
        }
    }
}

fn mix_updates(fp: &mut Fingerprint, updates: &StateUpdates) {
    fp.u64(updates.data.len() as u64);
    for (key, (old, new)) in &updates.data {
        fp.u64(key.0);
        mix_salt_value(fp, old);
        mix_salt_value(fp, new);
    }
}

fn main() {
    let outcome = run_once(&workload());
    let mut fp = Fingerprint::new();
    fp.bytes(b"salt-witness-v1");
    fp.bytes(&outcome.state_root);
    fp.bytes(&[u8::from(outcome.verified)]);

    let salt_witness = &outcome.witness.salt_witness;
    fp.bytes(b"kvs");
    fp.u64(salt_witness.kvs.len() as u64);
    for (key, value) in &salt_witness.kvs {
        fp.u64(key.0);
        mix_salt_value(&mut fp, value);
    }

    fp.bytes(b"parents");
    fp.u64(salt_witness.proof.parents_commitments.len() as u64);
    for (node_id, commitment) in &salt_witness.proof.parents_commitments {
        fp.u64(*node_id);
        fp.bytes(&commitment.as_bytes());
    }

    fp.bytes(b"levels");
    let mut levels = salt_witness.proof.levels.iter().collect::<Vec<_>>();
    levels.sort_unstable_by_key(|(bucket_id, _)| **bucket_id);
    fp.u64(levels.len() as u64);
    for (bucket_id, level) in levels {
        fp.u64(u64::from(*bucket_id));
        fp.bytes(&[*level]);
    }

    fp.bytes(b"multipoint-proof");
    fp.bytes(
        &salt_witness
            .proof
            .proof
            .0
            .to_bytes()
            .expect("canonical multipoint proof bytes"),
    );

    fp.bytes(b"lookup-values");
    fp.u64(outcome.lookup_values.len() as u64);
    for value in &outcome.lookup_values {
        match value {
            None => fp.bytes(&[0]),
            Some(value) => {
                fp.bytes(&[1]);
                fp.bytes(value);
            }
        }
    }

    fp.bytes(b"replay-updates");
    mix_updates(&mut fp, &outcome.replay_updates);
    fp.bytes(b"next-root");
    fp.bytes(&outcome.next_root);
    fp.bytes(b"next-trie");
    let mut next_trie = outcome.next_trie;
    next_trie.sort_unstable_by_key(|(node_id, _)| *node_id);
    fp.u64(next_trie.len() as u64);
    for (node_id, (old, new)) in next_trie {
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
