# New box checklist + frontier diagnostics

Bringing ARO up on a fresh machine, and diagnosing a collapsed frontier map. Every
item here was hit for real when moving from macOS to a Linux server (2026-07); the
code-level causes are fixed, the environment-level ones recur on every new box.

## Part 1: preflight on a fresh machine

Run these BEFORE the first campaign. Total cost: minutes, no LLM spend.

| Check | Command | Fix when it fails |
|---|---|---|
| `claude` CLI authed | `claude -p "say ok"` | log in on THIS machine; or `ARO_CLAUDE_BIN=/path/to/claude` |
| profiler present | Linux: `perf --version` · macOS: `ls /usr/bin/sample` | Linux: `apt install linux-tools-common linux-tools-$(uname -r)`; macOS needs nothing |
| perf allowed for users | `cat /proc/sys/kernel/perf_event_paranoid` (want <= 2) | root: `echo 'kernel.perf_event_paranoid = 2' > /etc/sysctl.d/99-perf.conf && sysctl --system`. Level 2 = sample own processes only, which is all ARO needs |
| a real Rust demangler | `which rustfilt c++filt` (either one) | `c++filt` ships with binutils (same package family as perf/addr2line); `cargo install rustfilt` also works. Without either, a heuristic fallback runs and can mislabel monomorphized hot frames |
| toolchain | `cargo --version && git --version` | install rustup/git |
| target repo submodules | `git -C <target-repo> submodule status` (no `-` prefix lines) | `git submodule update --init --recursive`. A LOCAL `git clone` does not carry submodules, and ARO's per-candidate worktrees fill them OFFLINE from the main clone's object store, so the main clone must have them first |
| target-specific tools | whatever the target's build.rs needs (mega-evm: `forge --version`) | install per target; a build.rs failure shows up as every candidate failing to build |
| probes compile at the pinned baseline | copy the spec's bench + diff probe into `<pkg>/examples/` and `cargo build --release -p <pkg> --example <name>`, then run them | API drift since the probe was written; fix the probe imports. The diff probe is NOT built by the L1 preflight, so check it by hand or the first candidate wastes a full LLM round before failing |
| disk headroom | worktrees + per-candidate `CARGO_TARGET_DIR`s land in the target repo's PARENT dir (`.aro-worktrees/`, `.aro-<name>-td/`), several GB each on big crates | point the clone at a big volume; clean after campaigns |
| quiet machine | nothing heavy scheduled alongside the run | A/A floors are measured, so a noisy box does not lie, it just proves fewer wins |
| ARO self-checks | `python3 selftest.py` (seconds) then `python3 tests/e2e_fixture.py` (minutes, needs cargo) | fix before spending LLM money |
| L1 observation arm | `python3 -m aro sweep targets/<spec>.json --min-pct 1.5` | must print a NON-EMPTY in-crate frontier; if not, see Part 2 |

## Part 2: the frontier-collapse diagnostic ladder

Symptom family: the L1 map is empty, or one giant bogus function owns most of the
self-time, or everything lands in `not_ours`, or `--attempt` skips top functions with
`source not located`. These are THREE DIFFERENT layers wearing the same costume.
Walk them in order; each layer's fix only exposes the next if it is also broken.

**Layer 1, sampling: does perf see symbols at all?**

```sh
perf record -o /tmp/d.data -F 997 -p <spinning-probe-pid> -- sleep 4
perf report -i /tmp/d.data --stdio --no-children --percent-limit 1 | head -20
```

Hex addresses or one anonymous blob instead of symbol names means the binary has no
symbol table. ARO already injects `CARGO_PROFILE_RELEASE_DEBUG=2` +
`CARGO_PROFILE_RELEASE_STRIP=none` into every target build, which overrides the
common `[profile.release] strip = "symbols"`. The one thing it cannot override is
`RUSTFLAGS = -C strip=symbols` in the target's `.cargo/config.toml`; remove that for
profiling. macOS is immune to stripping (Mach-O function starts survive), which is
why a probe can look healthy on a Mac and collapse on Linux.

**Layer 2, naming: are symbols demangled correctly?**

Signature: symbols exist, but the map's top "function" is really a MODULE from some
type's generic arguments (the historical case: dozens of distinct functions all
named `empty_db`, from `push1::<...CacheDB<EmptyDBTyped<...>>...>`), and the name is
un-locatable in the target source. Cause: no real demangler installed, heuristic
fallback in charge. Check `which rustfilt c++filt` and install one. Verify a real
symbol round-trips: `echo '_R...' | c++filt` should print a readable `crate::path`.

**Layer 3, locating: the name is right but `attempt_skipped: source not located`.**

Two honest sub-cases:
- The function is MACRO-GENERATED (`wrap_op!(push1, ...)` style): there is no literal
  `fn push1` in the source. The locator falls back to authoring sites (macro leading
  arg, `::name` dispatch wiring) and targets the macro's file, which is the better
  lever anyway: one edit improves every wrapped instance. If a macro-generated fn
  still comes back unlocated, check that the name appears at least twice in its
  authoring file (invocation + wiring).
- The function is genuinely EXTERNAL (a dependency's fn that ownership classification
  credited to us via generics, e.g. revm interpreter internals). Staying unlocated is
  correct; that weight belongs to the untouchable floor.

**The one-shot deep diagnostic** (when the layers are entangled and you want ground
truth on where cycles actually sit, inline frames included):

```sh
./target/release/examples/<probe> 30 &  SPIN=$!
sleep 2
perf record -o /tmp/d.data -F 997 --call-graph dwarf,4096 -p $SPIN -- sleep 4
kill $SPIN
perf script -i /tmp/d.data --inline | head -60   # per-sample inline stacks, innermost first
perf report -i /tmp/d.data --stdio --no-children --percent-limit 1 | head -20
```

Read the flat report for what the real machine functions are, and the inline stacks
for which source-level functions live inside them. If the flat report looks sane but
the ARO map does not, the bug is in ARO's parsing side (layers 2-3), not in perf.
