---
id: iw-r3-unverified-residuals
status: 設計待ち
priority: 未設定
opened: 2026-09-19
closed: null
related: [fake-run-context-consolidation]
backfilled: true
source_section: 未完了
---
# [iw-r3-unverified-residuals]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[iw-r3-unverified-residuals] 起票 2026-09-19 (iw 3 周目で「未検証」のまま残った 3 点、`tmp/review-20260919-iw-r3/review-r3.md`): (1) **`signals` の UNIQUE キー `(plugin, content_hash, pair, timeframe, bar_ts)` × 再ロック** — 再ロック承認で strategy の content_hash が変わるので、同一バーに旧 hash と新 hash で 1 回ずつ signal が立ち得る (pin 不一致の旧版は live から外れる設計 F2 なので同時 live は無いはずだが、切替バー 1 本の重複は probe 未実施)。ペーパー到達前の実機観測で見る (2) `find_matching_approved_metrics(exclude_name=)` は承認 payload の `$.name` で除外する — payload 名と候補名が食い違う入力 (改名再提出) の挙動は未 probe (3) CR7 で 9 本見つかった「実物より緩い `SimpleNamespace` double」の残存を全数確認していない ([fake-run-context-consolidation] と同根)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

