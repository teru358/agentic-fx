---
id: wire-list-branch-unreachable
status: 設計待ち
priority: 未設定
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [wire-list-branch-unreachable]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[wire-list-branch-unreachable] 起票 2026-09-18 (同 T-2): `worker._indicator_result_to_wire` の `list` 分岐は唯一の呼び出し元が `df_index` 非 None のため到達不能 (validator が必ず `pd.Series` へ畳む)。`0ab6ce6` で NaN 安全にはした。削除するか到達経路を作るか未決 (ローカル 3 本が独立に引っかかった = 読み手を惑わす)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

