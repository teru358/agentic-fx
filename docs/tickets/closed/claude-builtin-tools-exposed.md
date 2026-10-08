---
id: claude-builtin-tools-exposed
title: claude 子プロセスに組み込みツールが露出する問題は是正済
status: 是正済
priority: 未設定
opened: 2026-09-12
closed: 2026-10-02
related: [outbound-request-budget-is-a-design-constraint]
backfilled: true
source_section: 未完了
---
# [claude-builtin-tools-exposed]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[claude-builtin-tools-exposed] (重要、同 観測 C)** claude backend の子プロセスは `--allowedTools mcp__afx__*` でも組み込み 27 本 (Bash/Write/Edit/WebFetch/WebSearch/Task/SendMessage/RemoteTrigger/Cron* …) が `system/init` に広告され、`ToolSearch` と `StructuredOutput` が実際に実行された (registry を通らず `max_tool_calls` に数えられない)。Bash/WebFetch 等が呼ばれたときに拒否されるかは**未観測**。検証案: `--disallowedTools` を足した版と足さない版で init の tools 配列を比較 (claude 1 ターン)。外向きリクエスト経路 (WebFetch/WebSearch/RemoteTrigger/SendMessage) は [[outbound-request-budget-is-a-design-constraint]] に照らして先に潰す **→ 是正済 `dfbe40f`→`244559c` で --tools StructuredOutput,ToolSearch、実機 (system/init) 未確認** **実機確認済 (A4 11 回目 claude #72、2026-09-12): system/init の tools が 13 本 (StructuredOutput/ToolSearch/mcp__afx__* 11) に縮小、組み込み 27 本は全滅 → クローズ**

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。--tools で絞り、実機で組み込み 27 本が消えたことを確認済み。 根拠: 是正 dfbe40f→244559c (--tools StructuredOutput,ToolSearch)、A4 11 回目 #72 で tools 13 本に縮小を確認、本文に『クローズ』。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: claude 子プロセスに組み込みツールが露出する問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。--tools で絞り、実機で組み込み 27 本が消えたことを確認済み。 根拠: 是正 dfbe40f→244559c (--tools StructuredOutput,ToolSearch)、A4 11 回目 #72 で tools 13 本に縮小を確認、本文に『クローズ』。
