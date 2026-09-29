---
id: mission-prompt-in-argv-readable-via-proc
status: 裁定待ち
priority: 未設定
opened: 2026-09-11
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [mission-prompt-in-argv-readable-via-proc]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[mission-prompt-in-argv-readable-via-proc] (重要、設計判断待ち、codex 1 周目 backend-fix I1、2026-09-11)** claude/codex/opencode の worker は Landlock で `/proc` 全体を read-only に持つ (R10 受容、codex にも拡張済)。CLI は mission prompt を argv で受けるため、同一 UID の並行 mission (improve.parallel ≥ 2) や argv に秘密を載せた他プロセスの `/proc/*/cmdline` を候補コードが読める。現状 `parallel: 1` で実害なし。是正案: prompt を stdin / ファイルで渡す (claude `-p` は stdin 可、codex exec も stdin 可) か、`/proc` を自 PID 配下に縮小 (hidepid / PID namespace)。`Read` 組み込みを `--tools` から外した (同 I3) ので Claude 経由の直接読取は塞いだ (2026-09-12 裁定: codex への /proc 付与は戻した。claude/opencode の R10 受容は継続、prompt の argv 渡しは設計判断のまま) **→ 是正 `7837fe9` (prompt を stdin 渡し)、A4 15 回目で claude/codex とも argv から prompt 消失を実機確認 → クローズ (opencode は未対応のまま = 別起票不要、local LLM は単一ユーザー運用)**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

