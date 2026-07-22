# Salt `test-bucket-resize` 高并发 livelock 调查（2026-07-22）

## 结论

`NUM_DATA_BUCKETS=2` + `BUCKET_RESIZE_LOAD_FACTOR_PCT=1` + `test-bucket-resize` 在 32 逻辑 CPU 主机的默认 libtest 并发下存在调度敏感的初始化 livelock。它不是测试负载过重，也不是 `a6ee3a9b` backport 引入的回归。

决定性路径是 Salt 的进程级全局：

```text
salt::trie::trie::SHARED_COMMITTER: spin::Lazy<Arc<Committer>>
```

大量并发测试首次访问它时，等待线程持续在 `SHARED_COMMITTER + 8` 的 atomic acquire 状态字上忙等；初始化路径进入 `Committer::new`，而 `Committer::new` 又使用 Rayon 并行生成预计算表。三次间隔约 30 秒的栈采样中，atomic 自旋、Rayon latch 和 futex 等待结构保持不变，没有 forward progress。

## 范围与基线

- 主机：`dev-tko-node-1`
- 逻辑 CPU：32
- Rust：`1.96.0-nightly (bcf3d36c9 2026-03-19)`
- Salt：`19419f4d13e6c615b7a94cf3d2bf53d1052f723c`
- Algebra 干净基线：`01b20e377460e7af9da069b0c96f2d1158a7b974`
- Algebra 隔离候选：`0fe47338d31c73e7d72b4a60b75951088485ca1a`
- 上游候选：`a6ee3a9b88058af37905dc462ce91ed2074a241c`
- GDB：15.1

CI 对应配置来自 Salt `.github/workflows/rust.yml`：

```text
cargo test --features test-bucket-resize
NUM_DATA_BUCKETS=2
BUCKET_RESIZE_LOAD_FACTOR_PCT=1
```

用户独立对照已确认：该命令在 Salt CI 为绿；同配置在 15 核 aarch64 主机约 30 秒全绿，被阻塞的测试本身为毫秒级。

## GDB 证据

### 采样方法

为满足本机 `kernel.yama.ptrace_scope=1`，测试二进制作为 GDB 子进程启动。确认 20 秒未终止后，对同一 inferior 采集三次：

```text
info threads
thread apply all bt 12
continue
# wait approximately 30 seconds
```

另保留一份早期完整 `thread apply all bt`。该完整回溯在输出超长 Rust 泛型符号时触发 GDB 15.1 的 `Recursive internal problem`，但在崩溃前已将 atomic 地址符号化为 `salt::trie::trie::SHARED_COMMITTER+8`。后续三份通过关闭 demangle、隐藏帧参数并限制 12 层稳定采集。

### 三次稳定样本

下表各路径列均表示“包含该路径的线程数”，不是字符串出现次数。

| 样本 | UTC | 线程回溯 | `atomic_load` | Rayon `LockLatch` | `futex_wait` |
|---|---|---:|---:|---:|---:|
| 1 | 08:42:59 | 65 | 42 | 14 | 15 |
| 2 | 08:43:29 | 65 | 42 | 14 | 15 |
| 3 | 08:44:00 | 65 | 47 | 14 | 15 |

三个样本的关键结构相同：

1. 多个 `proof::prover::*` / witness 测试线程停在 `core::sync::atomic::atomic_load(..., Ordering::Acquire)`；
2. 完整符号样本显示读取目标为 `SHARED_COMMITTER + 8`；
3. 部分线程直接停在 `_mm_pause`，证明等待路径是 CPU busy-spin；
4. 固定数量线程停在 Rayon `LockLatch::wait_and_reset` / futex；
5. 30 秒间隔内没有测试完成或等待结构变化。

相关源码：

- `salt/src/trie/trie.rs:47-52`：`SHARED_COMMITTER` 的 `spin::Lazy` 定义；
- `salt/src/trie/trie.rs:173`：`StateRoot::new` 克隆全局 committer；
- `banderwagon/src/salt_committer.rs:98-124`：`Committer::new` 使用 `bases.par_iter()` 构建表；
- `banderwagon/src/salt_committer.rs:122`：每项执行 `EdwardsProjective::normalize_batch`。

## 线程敏感性实验

仅改变 libtest `--test-threads`；Salt、Algebra、feature、环境变量和测试二进制均不变。

| libtest 线程 | 退出码 | 墙钟 | 结果 |
|---:|---:|---:|---|
| 4 | 0 | 7 s | 188 passed, 0 failed, 2 ignored；libtest 6.80 s |
| 16 | 0 | 7 s | 188 passed, 0 failed, 2 ignored；libtest 6.47 s |
| 默认（32 CPU 主机） | 非确定 | 6.74 s 或 livelock | 同一构建既出现全绿，也出现 20 秒以上无进展 |

这证明问题由线程调度/并发首次初始化触发，而不是固定计算量。`--test-threads=4` 与用户给出的 CI 有效并发等价，可作为门变体候选；是否替换 mandatory gate 由用户裁决。

## 候选独立性

使用干净 Algebra 基线 `01b20e37...` 重建同一 Salt 测试二进制，并在默认线程下执行短窗口复现：

- 第 1 次即达到 20 秒 timeout（退出码 124）；
- 同样没有完成；
- 候选一行变更不在 Salt `SHARED_COMMITTER`、Banderwagon `Committer::new` 或 Rayon 初始化路径中。

因此 livelock 与 `a6ee3a9b` 候选无关。

## Root cause 判断

直接根因是：**并发测试首次访问由 `spin::Lazy` 保护的昂贵全局 `SHARED_COMMITTER`，等待者在 Lazy 状态字上忙等；初始化者同时进入 Rayon 并行预计算，形成线程调度敏感且无 forward progress 的初始化 convoy/livelock。**

这个判断由以下闭环支持：

- 锁/状态字已符号化到 `SHARED_COMMITTER+8`；
- 三次栈结构重复；
- 有明确 `_mm_pause` busy-spin；
- 同时存在稳定 Rayon latch/futex 等待；
- 降低 libtest 并发后 4/16 均约 7 秒终止；
- 干净 baseline 同样挂起，排除 backport。

## 门裁决与停点

- 默认 mandatory Salt conformance：在本机不可靠，不能据此判定候选 correctness 失败；
- 有界门变体：`--test-threads=4` 与 `--test-threads=16` 均实证通过；
- 推荐供用户裁决：采用 `--test-threads=4` 作为 CI 等价门变体，或先在 Salt 修复初始化路径后恢复默认并发；
- 性能测量继续冻结；
- 未集成候选、未 package、未 ship、未开 PR；
- Salt bug 仅形成草稿，未提报。

## 原始证据

归档目录：

`docs/data/a6ee3a9b-salt-livelock-investigation-20260722/`

包含：环境指纹、三次 GDB 栈、首份完整栈、线程实验、干净 baseline 短窗口结果、运行脚本与 SHA-256 校验。