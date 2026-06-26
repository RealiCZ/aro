# ARO — 运行操作手册(server)

把 ARO 放到一台机器上无人值守地跑性能优化。本手册覆盖当前**可运行版本**(per-function
sweep:profile 热点 → 逐个函数优化 → compound),不含尚未实现的"全项目 explore 模式"。

---

## 0. 平台前提

- **macOS 或 Linux 都行。** profiler 跨平台(`aro/profile.py` 的 `_raw_samples`):
  - **macOS** — 内置 `/usr/bin/sample`,免 sudo,开箱即用。
  - **Linux** — 用 **`perf`**:需装 perf(`linux-tools` / `perf` 包),且
    `kernel.perf_event_paranoid <= 1`(`sudo sysctl kernel.perf_event_paranoid=1`),或 root /
    CAP_PERFMON。采样失败(没装 / 没权限)→ 跑不出 frontier,见 §11。
- PNG(SVG→图)跨平台 best-effort:macOS `qlmanage`,Linux `rsvg-convert` / `cairosvg` / `inkscape`;
  都没有也不影响——`decision-tree.html` / `*.svg` 照出,只是少 `*.png`(HTML 里内嵌的是 SVG,不缺图)。
- Python **零 pip 依赖**(纯 stdlib,3.9+),不用建 venv、不用 `pip install`。
- profiler 已自动把探针跑在高 `ARO_BENCH_SCALE` 上,让它在采样窗口里一直处于热循环——所以
  不用担心"探针太快采不到"。

## 1. 依赖清单

| 需要 | 用途 | 检查 |
|---|---|---|
| macOS `/usr/bin/sample` **或** Linux `perf` | 采样热帧 | `ls /usr/bin/sample` 或 `perf --version` |
| Python 3.9+ | ARO 本体 | `python3 --version` |
| Rust + cargo | 编译 / test / bench 目标仓库 | `cargo --version` |
| git | worktree 隔离 | `git --version` |
| `claude` CLI(**已登录**) | 生成候选 + 语义评审 | `claude -p "ok" --output-format json` |
| `rustfilt`(可选) | 更准的符号解析,缺了有内置兜底 | `which rustfilt` |
| Linux 出 PNG(可选) | `rsvg-convert`/`cairosvg`/`inkscape` 任一 | `which rsvg-convert` |

`claude` 必须在这台机器上**完成认证**(`claude` 登录,或配好 `ANTHROPIC_API_KEY`)。验证:
```bash
claude -p "reply with: OK" --output-format json   # 应返回 JSON,result=OK
```

## 2. 一次性设置

```bash
# 1) 拿到 ARO 和目标仓库(各自独立的 git 仓库)
git clone <aro-repo>            ~/aro-py
git clone <target-repo>        ~/work/mega-evm     # 你要优化的 Rust 仓库

# 2) 目标仓库先能自己编译过(ARO 会在它的 worktree 里 build/test/bench)
cd ~/work/mega-evm && cargo build --release && cd -
```

## 3. 写 spec(target JSON)

一个 spec 描述"优化谁、怎么量、怎么验、改哪、跑多久"。看现成例子 `targets/mega-evm-r3.json`:

```jsonc
{
  "name": "mega-evm-r3",
  "target_repo":  { "path": "/绝对路径/mega-evm", "baseline_ref": "<commit-sha>" },
  "hot_path":     { "file": "crates/.../host.rs", "fn": "inspect_storage" },
  "metric":       "ns_per_call", "direction": "minimize",
  "benchmark_probe": { "pkg": "mega-evm", "probe": "probes/evm_r3.rs",
                       "example": "evm_r3", "sample_prefix": "BENCH",
                       "profile": { "spin_secs": 8, "sample_secs": 4 } },
  "correctness_oracle": {
    "build": ["cargo","build","--release","-p","mega-evm"],
    "test":  ["cargo","test","--release","-p","mega-evm","--lib"],
    "differential": { "pkg":"mega-evm", "probe":"probes/evm_r3_diff.rs",
                      "example":"evm_r3_diff", "prefix":"DIFF" }   // ← 字节相同的判官,强烈建议有
  },
  "constraints": { "editable": ["crates/.../host.rs"], "no_new_deps": true, "byte_identical": true },
  "run": { "generator": "agentic", "stop": {"max_rounds":1,"dry_rounds":1},
           "aa_runs": 2, "ab_pairs": 8, "timeout": 1800, "bench_scales": [1,8,64] }
}
```

关键点:
- `baseline_ref` 钉一个 **commit sha**(ARO 从它切出隔离 worktree,你主仓库随便动不影响)。
- `differential` 探针 = 字节相同的 oracle。**没有它,significance 判官会拒**(除非 `constraints.weak_oracle=true`,但那只剩测试套件、不是字节相同)。
- `bench_scales` 给 auto-tighten 用:噪声受限时自动放大 batch 重测。

## 4. 先做一次"只画图"的健全性检查(不改代码、不花钱)

```bash
cd ~/aro-py
python3 -m aro sweep targets/mega-evm-r3.json --min-pct 1.5
```
它 profile + 出一张 frontier 地图(哪些函数热、哪些是我方杠杆、哪些碰不得)。**先确认这张图有内容**(能 parse 出 profile),再开真跑。空的多半是 probe 不能 spin、或符号被 strip。

## 5. 无人值守真跑(会改代码、会花钱)

```bash
python3 -m aro sweep targets/mega-evm-r3.json --attempt --diverge --critic \
    --max-attempts 8 --rounds-per-fn 2 --fanout 2 --out-dir ./.aro-runs/megaevm-prod
```

常用旋钮:

| 旋钮 | 默认(--diverge) | 作用 |
|---|---|---|
| `--attempt` | — | 开 L3 无人值守(否则只出地图) |
| `--diverge` | — | 无限探索:走完前沿、refill 重试,不在 dry 处早停 |
| `--critic` | off | 开第二道 judge(语义评审,拦 reward-hack / 钻 bench);**建议开** |
| `--max-attempts N` | 10000 | **成本闸**:最多攻多少个 function-attempt。线性控成本/时间 |
| `--rounds-per-fn N` | 4 | 每个函数几轮 |
| `--fanout N` | 3 | 每轮并行出几个候选(>1 自动开 prescreen) |
| `--gen-concurrency N` | 8 | 并行 `claude` 生成上限(判官仍串行——这是 moat) |
| `--dry-rounds N` | 3 | 每函数几轮无 accept 算榨干 |
| `--out-dir DIR` | `.aro-runs/<name>-diverge` | 产物目录 |

**成本/时间**:token 重(读阶段 + 生成 + 评审都烧),mega-evm 这种 ~$8–10/小时量级。`--max-attempts`
是主闸:中等档(8 / 2 / 2)实测 ~6–7h / ~$69 / 4 个 accept。先小后大。

## 6. 让它在 server 上活过断连

跑是多小时的,别让 SSH 断了就死。三选一:

```bash
# tmux(推荐,能回看)
tmux new -s aro
python3 -m aro sweep targets/mega-evm-r3.json --attempt --diverge --critic --out-dir ./.aro-runs/prod
# Ctrl-b d 脱离;tmux attach -t aro 回来

# 或 nohup
nohup python3 -m aro sweep ... > ./.aro-runs/prod.log 2>&1 &

# 或 launchd plist(开机自起/守护,自己按需写)
```

## 7. 产物(全在 `--out-dir`)

| 文件 | 是什么 |
|---|---|
| **`events.jsonl`** | **真值**,逐条事件流。出了任何分歧以它为准 |
| `decision-tree.html` | 浅色三栏报告(火焰图 │ 候选 │ dossier)+ 底部"加速 vs 累计 token"图。跑完自动出 |
| `perf-token.svg` / `.png` | 那张轨迹图,独立文件 |
| `REPORT.md` | 文字报告(realized / headroom / floor / 判定),跑中实时刷新 |
| `trajectory.svg` / `.png` | realized vs headroom 折线 |
| `a<N>/records.jsonl`、`a<N>/patches/` | 每次 attempt 的候选记录 + patch |

看报告:把 `decision-tree.html` 拷回本地浏览器打开(自包含单文件,离线可看)。

任何**旧 run**想重出新版报告:`python3 -m aro tree <out-dir>`(只读 events.jsonl,不重新优化、不花钱)。

## 8. 盯一个在跑的 run

```bash
tail -f ./.aro-runs/prod/events.jsonl          # 逐事件
watch -n5 'tail -20 ./.aro-runs/prod/REPORT.md' # 报告实时刷新
# 关键信号:attempt_started/finished、baseline_advanced(accept)、
#          gate apply status=fail(漂移/sibling)、critic verdict=reject(拦下假优化)
python3 - <<'PY'
import json
e=[json.loads(l) for l in open(".aro-runs/prod/events.jsonl") if l.strip()]
af=[x for x in e if x.get("event")=="attempt_finished"]
print("accepts:", sum(1 for x in af if x.get("accepted")), "/", len(af),
      "| tok:", sum(x.get("tokens") or 0 for x in e),
      "| $:", round(sum(x.get("cost_usd") or 0 for x in e),2))
PY
```

## 9. 停止 + 清理

```bash
pkill -f "aro sweep.*<out-dir名>"     # 停 orchestrator
# ARO 的隔离 worktree / target-dir(中途被杀可能残留):
git -C <目标仓库> worktree list        # 看有没有 .aro-worktrees 下的残留
for w in $(git -C <目标仓库> worktree list --porcelain | awk '/^worktree/{print $2}' | grep .aro-worktrees); do
  git -C <目标仓库> worktree remove --force "$w"; done
git -C <目标仓库> worktree prune
rm -rf <目标仓库父目录>/.aro-worktrees/* <目标仓库父目录>/.aro-*-td   # target-dir 很占盘
```
> ⚠️ 别误删:`.aro-worktrees/*` 和 `.aro-<name>-td` 是 ARO 的临时物;`git worktree list` 里你
> 自己的 `cz/*` worktree 不要碰。

## 10. compounding / 续跑

`--out-dir` 指向同一个目录再跑 → 从**已 accept 的 advanced baseline 续**(resume),wins 跨次累积。
想从头来就换一个空 `--out-dir`(或 `--ignore-resume-failure` 故意从头)。

## 11. 排错

| 症状 | 多半原因 / 处理 |
|---|---|
| 地图空 / "no profile parsed" | **Linux**:多半是 `perf` 没装或 `perf_event_paranoid > 1`,跑 `sudo sysctl kernel.perf_event_paranoid=1`;**macOS**:`/usr/bin/sample` 该有。两者通用:release 别 strip 符号(ARO 已强制 `CARGO_PROFILE_RELEASE_DEBUG=2 / STRIP=false`),再查 probe 例子能否独立 `cargo run` |
| 候选全 `verify-failed: no differential oracle` | spec 缺 `differential` 探针。补上,或 `constraints.weak_oracle=true`(降级、判官会标注) |
| `apply failed: search text not found` | 漂移/同轮 sibling 冲突;benign(已修锚点 + 轮末折叠)。看是不是真新场景再深挖 |
| `claude` 卡住 / 报错 | 认证过期;`claude` 重新登录。读阶段有 600s 超时兜底 |
| 盘爆了 | 每个 worktree 一份 target-dir(为正确性必须独立编译)。清理 `.aro-*-td`,或减 `--gen-concurrency` |
| 占着不放的 cargo/claude | 中途被杀的残留;删对应 `.aro-worktrees` 子目录会让它们退出 |

## 12. 当前能力边界(诚实)

- 优化范围 = **profile 驱动的热前沿**(热 ≥ min_pct 且能在源码定位到 `fn` 的我方函数),**不扫全代码库**。
- 需要 spec 喂的 **bench + differential 探针**(没有 oracle 的代码现在做不了——那是未实现的"全项目 explore"档)。
- 判官是 moat:reward-hack guard + 字节相同 differential + A/A floor + 配对 A/B + bootstrap CI +
  auto-tighten,外加第二道语义评审(`--critic`)。`accepted` = 正确性+提速已证,**≠ 该合**;合不合人来定。
