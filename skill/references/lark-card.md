# ARO run → Lark card

Turn one ARO optimization run into a Lark (Feishu) interactive card. Self-contained:
follow this top-to-bottom and you produce a correct card without reading ARO's internals.
**Every number on the card must come from the run's files: never estimate or invent.**

Prerequisite contract: [`run-data.md`](run-data.md). The one rule that governs the card:
**`accepted` ≠ should-merge**: only a `mergeable:true` edit is safe to PR directly; the
rest are real wins that still need a human call. The card must show that distinction.

---

## 1. Get the structured data (local, no cargo / no LLM / free)

```sh
cd ~/workspace/aro
python3 -m aro manifest .aro-runs/<RUN>     # → manifest.json  (the accepted edits)
python3 -m aro tree     .aro-runs/<RUN>     # → tree.json       (run summary)
```
(Both files may already exist in the copied run dir, regenerating is just safer.)

Pull exactly these fields:

| card needs | from | field |
|---|---|---|
| spec name | `manifest.json` | `spec` |
| overall speedup (% faster, **already positive**) | `tree.json` | `summary.realized_pct` |
| #attempted · #accepted · #skipped · decision | `tree.json` | `summary.attempted` / `accepted` / `skipped` / `decision` |
| LLM spend | `tree.json` | `summary.tokens` · `summary.cost_usd` |
| the wins (one per row) | `manifest.json` | `accepted[]`: `fn`, `delta_pct`, `regime`, `critic_verdict`, **`mergeable`** |
| chart image | run dir | `perf-token.png` (see §4) |

Sign note: a win's `delta_pct` is the raw metric change. For a minimize metric
(`ns_per_call`) **negative = faster**. On the card show the magnitude as `X% faster`, e.g.
`delta_pct:-19.22` → `19.22% faster`. `summary.realized_pct` is already the positive % faster.

---

## 2. Hard rules

- **Numbers only from `manifest.json` / `tree.json`.** Missing field → write an em dash (U+2014), never guess.
- **Respect `mergeable`.** 🟢 = `mergeable:true` (byte-identical + critic pass) = PR-ready.
  🟡 = `mergeable:false` (relaxed regime or critic `pass-risk`) = needs a human. Never mark a
  🟡 win as ready-to-merge.
- **Header color** = `green` if any win is mergeable, else `yellow`.
- English card; keep it scannable.

---

## 3. The card (JSON 2.0 skeleton)

Replace every `{{…}}`; keep the structure.

```json
{
  "schema": "2.0",
  "config": { "wide_screen_mode": true },
  "header": {
    "template": "{{header_color}}",
    "title":    { "tag": "plain_text", "content": "ARO optimization report · {{spec}}" },
    "subtitle": { "tag": "plain_text", "content": "{{realized_pct}}% faster · {{accepted}} accepted" }
  },
  "body": { "elements": [
    { "tag": "markdown",
      "content": "**Overall {{realized_pct}}% faster**, compounded over {{accepted}} accepts\n{{attempted}} functions tried · {{accepted}} accepted · decision {{decision}}" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**Wins** _(accepted ≠ should-merge: only 🟢 is PR-ready)_\n{{win_lines}}" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**Cost** {{tokens}} tokens · ${{cost_usd}}\n**Verdict** 🟢 {{n_mergeable}} PR-ready ({{mergeable_fns}}) · 🟡 {{n_review}} need human review" },
    { "tag": "hr" },
    { "tag": "img",
      "img_key": "{{img_key}}",
      "alt": { "tag": "plain_text", "content": "speedup vs cumulative tokens" } },
    { "tag": "action", "actions": [
      { "tag": "button", "type": "primary",
        "text": { "tag": "plain_text", "content": "View full report" },
        "url": "{{report_url}}" } ] }
  ] }
}
```

**`{{win_lines}}`**: sort `manifest.accepted` by `|delta_pct|` **descending** (biggest win
first; NOT the file's `order`, which is acceptance order). One line each, joined by `\n`:

```
{{🟢 if mergeable else 🟡}} `{{fn}}` **{{abs(delta_pct)}}% faster** · {{regime}} · {{'PR-ready' if mergeable else 'needs review'}}
```

**`{{report_url}}`**: if `aro serve --port 8010` is running, `http://<host>:8010/`; if not,
drop the whole `action` block. **`{{img_key}}`**: see §4.

If your Lark stack uses card **v1**: move `body.elements` to top-level `elements`, and
replace each `{"tag":"markdown","content":X}` with `{"tag":"div","text":{"tag":"lark_md","content":X}}`.
The `img`, `hr`, `action`, `button` tags are the same.

---

## 4. The chart (`perf-token.png`)

The figure at the bottom of `decision-tree.html`: running-best speedup vs cumulative LLM
tokens. Lark cards **cannot embed an SVG or a URL image**: upload the PNG to get an `img_key`.

1. File: `.aro-runs/<RUN>/perf-token.png` (ships with the run dir; 1600x1600 PNG).
2. Upload it via the Lark image API (`POST /open-apis/im/v1/images`, `image_type=message`) →
   the returned `image_key` is `{{img_key}}`.
3. The `img` element above references it.

If this run was generated fresh on a box with no SVG→PNG converter, only `perf-token.svg`
exists: convert first (`rsvg-convert perf-token.svg -o perf-token.png` or
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
    "title":    { "tag": "plain_text", "content": "ARO optimization report · mega-evm" },
    "subtitle": { "tag": "plain_text", "content": "34.46% faster · 4 accepted" }
  },
  "body": { "elements": [
    { "tag": "markdown",
      "content": "**Overall 34.46% faster**, compounded over 4 accepts\n8 functions tried · 4 accepted · decision CONTINUE" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**Wins** _(accepted ≠ should-merge: only 🟢 is PR-ready)_\n🟡 `sstore` **19.22% faster** · relaxed · needs review\n🟡 `inspect_storage` **8.61% faster** · relaxed · needs review\n🟡 `inspect_storage` **7.06% faster** · relaxed · needs review\n🟢 `sload` **4.48% faster** · byte-identical · PR-ready" },
    { "tag": "hr" },
    { "tag": "markdown",
      "content": "**Cost** 1.63M tokens · $68.80\n**Verdict** 🟢 1 PR-ready (`sload`) · 🟡 3 need human review" },
    { "tag": "hr" },
    { "tag": "img", "img_key": "{{image_key from uploading perf-token.png}}",
      "alt": { "tag": "plain_text", "content": "speedup vs cumulative tokens" } },
    { "tag": "action", "actions": [
      { "tag": "button", "type": "primary",
        "text": { "tag": "plain_text", "content": "View full report" },
        "url": "http://<host>:8010/" } ] }
  ] }
}
```

---

## 6. Self-check before sending

Re-read every number off `manifest.json` / `tree.json` once more:
- [ ] `realized_pct`, `tokens`, `cost_usd`, `accepted`/`attempted` match `tree.json.summary`.
- [ ] One win line per `manifest.accepted` entry, sorted by `|delta_pct|` desc, magnitude shown.
- [ ] 🟢/🟡 each match that entry's `mergeable`; the Verdict counts add up; header color matches.
- [ ] `img_key` came from uploading **this run's** `perf-token.png`.

If anything can't be backed by a field, replace it with an em dash (U+2014) and redo: never fill from memory.
