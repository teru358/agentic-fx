---
id: subprocess-allowlist-by-line-number
title: subprocess の stdin 方針テストが行番号固定で壊れやすい
status: 実装待ち
priority: 低
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [subprocess-allowlist-by-line-number]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[subprocess-allowlist-by-line-number] 起票 2026-09-18 (同 T-3): `tests/test_subprocess_stdin_policy.py` の allowlist が `(path, 行番号)` 固定で、同ファイルの無関係な編集で壊れる (今回初発)。関数名/マーカーコメント固定に変えるか運用として明記

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: subprocess の stdin 方針テストが行番号固定で壊れやすい — 仕分け (2026-10-02、現物で成立を確認): 関数名かマーカーコメント固定へ変えるか、運用として明記する。 根拠: tests/test_subprocess_stdin_policy.py:17 の allowlist が (path, 行番号 351) 固定のまま。
