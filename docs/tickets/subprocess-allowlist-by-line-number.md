---
id: subprocess-allowlist-by-line-number
status: 設計待ち
priority: 未設定
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [subprocess-allowlist-by-line-number]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[subprocess-allowlist-by-line-number] 起票 2026-09-18 (同 T-3): `tests/test_subprocess_stdin_policy.py` の allowlist が `(path, 行番号)` 固定で、同ファイルの無関係な編集で壊れる (今回初発)。関数名/マーカーコメント固定に変えるか運用として明記

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

