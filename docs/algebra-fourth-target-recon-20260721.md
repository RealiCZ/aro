# Algebra fourth-target reconnaissance — 2026-07-21

## Decision summary

Proceed with ARO onboarding against the exact Salt consumer pin `80ca69c37f79d5d00750edc1602af81b5f456695`, using an algebra-only local onboarding baseline that adds the missing Salt measurement profile/toolchain metadata but changes no arithmetic source. Build two aligned lanes:

1. `algebra-bandersnatch-field`: production-shaped batches of Bandersnatch base-field Montgomery multiply/add/inverse.
2. `algebra-bandersnatch-msm`: Salt-scale Bandersnatch variable-base MSM/normalization.

No `aro/*.py` change is needed. `ship_conformance` runs arbitrary shell commands in the algebra candidate worktree (`shell=True`), so a checked-in algebra-side script can apply Cargo CLI path patches to a clean temporary Salt worktree and run the Salt test matrix. This satisfies the cross-repo requirement without editing tracked Salt files.

Upstream optimization candidates are report-only human decisions. None is cherry-picked in this B-class campaign.

## Fork ancestry and MegaETH delta

- Salt pin / MegaETH head: `80ca69c37f79d5d00750edc1602af81b5f456695`.
- Exact direct parent and merge-base with `arkworks-rs/algebra` master: `da450f98b9b4bf1b4c8eec8f96b4501f9705c517` (`make serial_batch_inversion_and_mul public`, #971).
- Upstream snapshot is post-Release-0.5 and pre-0.6 while manifests still say `0.5.0`; it is not the exact `v0.5.0` tag.
- Divergence at reconnaissance time: upstream ahead 55 commits, MegaETH ahead 1 commit.
- MegaETH-only change: 9 crate roots, 18 added lines, all adding `coverage_nightly` / `coverage(off)` crate attributes. There is no arithmetic, data-layout, feature, or runtime algorithm delta from the upstream base.

## Upstream candidates — human decision, no action

| Commit | Scope | Decision boundary |
|---|---|---|
| `a6ee3a9b` | removes `i % N` where `i < N` in `MontConfig::into_bigint` | clean low-risk port, but not core `mul_assign`; human may approve a focused experiment |
| `65f9aa25` | fixes GLV leading-zero skip bug | source applies cleanly; value depends on production GLV use, so human confirmation required |
| `2c4a6950` | extended Jacobian buckets for short-Weierstrass MSM | large, cleanly applicable architecture change; Bandersnatch is twisted Edwards, so direct benefit to the measured TE workload is uncertain |
| `da611a3c` + `104444d9` | small-scalar MSM specializations and cleanup | not independently clean on the pin; requires the broader MSM stack and a human-owned experiment branch |
| `8de9a9d9` | removes dead ff-asm spill-buffer generator path | maintenance cleanup, not a demonstrated runtime Montgomery improvement |

No upstream post-base algorithm optimization was found for the current generic Montgomery `mul_assign`, `add_assign`, or inverse implementation. `call_mut` is a compiler-generated closure trampoline, not an arkworks function name.

## Salt feature/profile/toolchain findings

Salt explicitly enables `ark-ff/asm` through `banderwagon default → parallel → ark-ff/asm`; this is not accidental dependency-default activation. All six 2026-07-20 lane build fingerprints contain `asm`.

The x86_64 build does not enable ADX/BMI2 (`target-cpu=native` / `+adx,+bmi2` are absent). Therefore:

- x86_64 `_addcarry_u64` / `_subborrow_u64` paths enabled by `asm` are active;
- ADX/BMI2-specific generated Montgomery multiplication is not active and falls back to Rust;
- enabling ADX/BMI2 would be a Salt/build-policy change and is outside this algebra-only B-class task; report only.

Salt production profile/toolchain to mirror:

- `opt-level = 3`
- `lto = "thin"`
- `codegen-units = 1`
- `panic = "abort"`
- `nightly-2026-03-20` (`rustc 1.96.0-nightly`, LLVM 22.1.0)

Algebra pin already has opt3/thin-LTO/panic-abort but omits release `codegen-units = 1` and has no toolchain file. The onboarding baseline will add only those measurement-fidelity settings in algebra; arithmetic source remains byte-identical to the Salt pin.

## Hot-symbol attribution

The historical “97% external” value is specifically the old `salt-ipa` perf self-sample floor: `76.43% mul_assign + 16.64% add_assign + 3.75% call_mut = 96.82%`. It is not a Callgrind-Ir percentage and should not be generalized to every lane. The 2026-07-20 repeat is 95.24% total retained floor and 89.11% crypto-labelled frames.

Proven exact monomorphization from the surviving binary:

- `mul_assign`: `MontBackend<ark_bls12_381::fields::fr::FrConfig, 4>` — Bandersnatch base field `Fq` aliases BLS12-381 `Fr`.
- `add_assign`: `ark_ec::twisted_edwards::Projective<BandersnatchConfig>`.

High-confidence lane attribution:

| Lane | External arithmetic share / shape | Source ownership |
|---|---|---|
| `salt-ipa` | mul 70.31%, add 12.87%, inverse 1.80%, closure 4.13% | Bandersnatch base-field arithmetic, TE projective addition, ark-ec MSM/normalize |
| `salt-witness` | mul 50.88%, add 7.13%, inverse 3.65%, closure 3.41% | shared Bandersnatch proof/MSM path; `inverse` short name remains ambiguous with `StateUpdates::inverse` |
| `salt-multiproof-prove` | mul 60.38%, add 9.06%, inverse 2.07%, closure 3.96% | shared Bandersnatch proving MSM and scalar/base-field inversion |
| `salt-multiproof-verify` | mul 73.78%, add 10.80%, closure 2.63% | shared Bandersnatch verifier MSM |
| `salt-msm` | outer `add_affine_point` 94.68%, `mul_index` 4.43% | Salt-owned hand-written committer path; retain as Salt lane |
| `salt-state-update` | `add_affine_point` 24.35%, `inverse` 7.65% | mixed Salt state + commitment path; not a pure algebra lane |

Raw perf/callgrind files were deleted by the runner. Exact `inverse` and `call_mut` monomorphizations cannot be reconstructed for every lane; they remain explicitly marked as high-confidence call-chain attribution rather than proof.

## Lane design decision

### `algebra-bandersnatch-field`

Probe/differential use the same deterministic arrays of both relevant 4-limb fields:

- Bandersnatch base `Fq = ark_bls12_381::FrConfig` (dominant production field);
- Bandersnatch scalar `FrConfig` (IPA transcript/proof arithmetic).

Timed mix reproduces production-style batched Montgomery multiplication/addition plus inversion/batch inversion. Editable/probe coverage is restricted to the concrete field configs, `ff` Montgomery backend/common batch inversion, and the `ff-macros` generated kernels actually exercised by both probes.

### `algebra-bandersnatch-msm`

Probe/differential construct deterministic Bandersnatch projective bases and Salt-scale scalar vectors, then run variable-base MSM and normalization. Editable/probe coverage is restricted to ark-ec variable-base MSM, twisted-Edwards group operations, and Bandersnatch curve glue reached by both probes. Field-kernel files stay in the field lane rather than making this lane's editable region indiscriminately broad.

Both lanes use arkworks crate tests in `correctness_oracle`, plus deterministic semantic differential probes. Salt cross-repo validation is a mandatory ship-conformance check.

## Stop conditions

- If the Cargo CLI path patch cannot run the complete Salt test matrix from `ship_conformance`, stop as Class A; do not modify `aro/*.py`.
- If a candidate reaches package/ship, stop before opening or pushing a PR and report to the user.
- Upstream cherry-pick choices remain human decisions and are not attempted here.
