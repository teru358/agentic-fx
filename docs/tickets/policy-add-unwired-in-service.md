---
id: policy-add-unwired-in-service
status: 是正済
priority: 中
opened: 2026-09-20
closed: 2026-10-05
related: [verify-integration-not-just-units]
backfilled: true
source_section: 未完了
---
# [policy-add-unwired-in-service]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[policy-add-unwired-in-service] 起票 2026-09-20 (重要度: 中、案 A 準備の実機で発覚)**: `service.py:1054` の `Commands(...)` に `policy_path` を渡していないので、本番サービスの `afx> policy add <text>` は常に「policy directives の path が未配線です」を返す (`commands.py:288-289`)。単体テストは `policy_path` を渡すので緑。[[verify-integration-not-just-units]] の実例。処置 = `policy_path=root / "policy" / "directives.md"` を配線 + `build_app` 経由の配線テスト。暫定 = ファイルへ直接 1 行追記 (`Policy.tail` は mission ごとに読み直すので再起動不要)

## 修正内容

- 2026-10-05: build_app は Commands へ policy_path=directives_path(root) を配線済み。build_app 経由の policy add が共有 directives ファイルへ届くことを tests/test_service_wiring_pins.py で pin

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-05: 是正内容 — build_app は Commands へ policy_path=directives_path(root) を配線済み。build_app 経由の policy add が共有 directives ファイルへ届くことを tests/test_service_wiring_pins.py で pin
