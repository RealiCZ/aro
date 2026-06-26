# ARO run → Lark card

Turn one ARO optimization run into a Lark (Feishu) interactive card. Self-contained:
follow this top-to-bottom and you produce a correct card without reading ARO's internals.
**Every number on the card must come from the run's files — never estimate or invent.**

Prerequisite contract: [`run-data.md`](run-data.md). The one rule that governs the card:
**`accepted` ≠ should-merge** — only a `mergeable:true` edit is safe to PR directly; the
rest are real wins that still need a human call. The card must show that distinction.

---

## 1. Get the structured data (local, no cargo / no LLM / free)

```sh
cd ~/workspace/aro
python3 -m aro manifest .aro-runs/<RUN>     # → manifest.json  (the accepted edits)
python3 -m aro tree     .aro-runs/<RUN>     # → tree.json       (run summary)
```
(Both files may already exist in the copied run dir — regenerating is just safer.)

Pull exactly these fields:

| card needs | from | field |
|---|---|---|
| spec name | `manifest.json` | `spec` |
| overall speedup (% faster, **already positive**) | `tree.json` | `summary.realized_pct` |
| #attempted · #accepted · #skipped · decision | `tree.json` | `summary.attempted` / `accepted` / `skipped` / `decision` |
| LLM spend | `tree.json` | `summary.tokens` · `summary.cost_usd` |
| the wins (one per row) | `manifest.json` | `accepted[]`: `fn`, `delta_pct`, `regime`, `critic_verdict`, **`mergeable`** |
| chart image | run dir | `perf-token.png` (see §4) |

Sign note: a win's `delta_pct` is the raw metric change — for a minimize metric
(`ns_per_call`) **negative = faster**. On the card show the magnitude with 「快」, e.g.
`delta_pct:-19.22` → `快 19.22%`. `summary.realized_pct` is already the positive % faster.

---

## 2. Hard rules

- **Numbers only from `manifest.json` / `tree.json`.** Missing field → write `—`, never guess.
- **Respect `mergeable`.** 🟢 = `mergeable:true` (byte-identical + critic pass) = PR-ready.
  🟡 = `mergeable:false` (relaxed regime or critic `pass-risk`) = needs a human. Never mark a
  🟡 win as ready-to-merge.
- **Header color** = `green` if any win is mergeable, else `yellow`.
- Chinese card; keep it scannable.

---

## 3. The card (JSON 2.0 skeleton)

Replace every `{{…}}`; keep the structure.

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": {
    "template": "{{header_color}}",
    "title":    { "tag": "plain_text", "content": "ARO 优化报告 · {{spec}}" },
    "subtitle": { "tag": "plain_text", "content": "realized 快 {{realized_pct}}% · {{accepted}} 个 accept" }
  },
  "body": { "elements": [
    { "tag": "markdown",
      "content": "**整体提速　快 {{realized_pct}}%**　compounded over {{accepted}} 个 accept\n{{attempted}} 个函数尝试 · {{accepted}} 个采纳 · 决定 {{decision}}" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**成果**　_(accepted ≠ should-merge:只有 🟢 能直接发 PR)_\n{{win_lines}}" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**成本**　{{tokens}} tokens · ${{cost_usd}}\n**结论**　🟢 {{n_mergeable}} 个可直接发 PR（{{mergeable_fns}}）· 🟡 {{n_review}} 个待人审" },
    { "tag": "hr" },
    { "tag": "img",
      "img_key": "{{img_key}}",
      "alt": { "tag": "plain_text", "content": "加速 vs 累计 token" } },
    { "tag": "action", "actions": [
      { "tag": "button", "type": "primary",
        "text": { "tag": "plain_text", "content": "查看完整报告" },
        "url": "{{report_url}}" } ] }
  ] }
}
```

**`{{win_lines}}`** — sort `manifest.accepted` by `|delta_pct|` **descending** (biggest win
first; NOT the file's `order`, which is acceptance order). One line each, joined by `\n`:

```
{{🟢 if mergeable else 🟡}} `{{fn}}` **快 {{abs(delta_pct)}}%** · {{regime}} · {{'可直接合' if mergeable else '待人审'}}
```

**`{{report_url}}`** — if `aro serve --port 8010` is running, `http://<host>:8010/`; if not,
drop the whole `action` block. **`{{img_key}}`** — see §4.

If your Lark stack uses card **v1**: move `body.elements` to top-level `elements`, and
replace each `{"tag":"markdown","content":X}` with `{"tag":"div","text":{"tag":"lark_md","content":X}}`.
The `img`, `hr`, `action`, `button` tags are the same.

---

## 4. The chart (`perf-token.png`)

The figure at the bottom of `decision-tree.html` — running-best speedup vs cumulative LLM
tokens. Lark cards **cannot embed an SVG or a URL image**: upload the PNG to get an `img_key`.

1. File: `.aro-runs/<RUN>/perf-token.png` (ships with the run dir; 1600×1600 PNG).
2. Upload it via the Lark image API (`POST /open-apis/im/v1/images`, `image_type=message`) →
   the returned `image_key` is `{{img_key}}`.
3. The `img` element above references it.

If this run was generated fresh on a box with no SVG→PNG converter, only `perf-token.svg`
exists — convert first (`rsvg-convert perf-token.svg -o perf-token.png` or
`cairosvg perf-token.svg -o perf-token.png`), then upload.

---

## 5. Gold-standard filled card (run `mega-evm-medium`, verified values)

Match this shape exactly.

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": {
    "template": "green",
    "title":    { "tag": "plain_text", "content": "ARO 优化报告 · mega-evm" },
    "subtitle": { "tag": "plain_text", "content": "realized 快 34.46% · 4 个 accept" }
  },
  "body": { "elements": [
    { "tag": "markdown",
      "content": "**整体提速　快 34.46%**　compounded over 4 个 accept\n8 个函数尝试 · 4 个采纳 · 决定 CONTINUE" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**成果**　_(accepted ≠ should-merge:只有 🟢 能直接发 PR)_\n🟡 `sstore` **快 19.22%** · relaxed · 待人审\n🟡 `inspect_storage` **快 8.61%** · relaxed · 待人审\n🟡 `inspect_storage` **快 7.06%** · relaxed · 待人审\n🟢 `sload` **快 4.48%** · byte-identical · 可直接合" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**成本**　1.63M tokens · $68.80\n**结论**　🟢 1 个可直接发 PR（`sload`）· 🟡 3 个待人审" },
    { "tag": "hr" },
    { "tag": "img", "img_key": "{{上传 perf-token.png 得到的 image_key}}",
      "alt": { "tag": "plain_text", "content": "加速 vs 累计 token" } },
    { "tag": "action", "actions": [
      { "tag": "button", "type": "primary",
        "text": { "tag": "plain_text", "content": "查看完整报告" },
        "url": "http://<host>:8010/" } ] }
  ] }
}
```

---

## 6. Self-check before sending

Re-read every number off `manifest.json` / `tree.json` once more:
- [ ] `realized_pct`, `tokens`, `cost_usd`, `accepted`/`attempted` match `tree.json.summary`.
- [ ] One win line per `manifest.accepted` entry, sorted by `|delta_pct|` desc, magnitude shown.
- [ ] 🟢/🟡 each match that entry's `mergeable`; `结论` counts add up; header color matches.
- [ ] `img_key` came from uploading **this run's** `perf-token.png`.

If anything can't be backed by a field, replace it with `—` and redo — never fill from memory.
