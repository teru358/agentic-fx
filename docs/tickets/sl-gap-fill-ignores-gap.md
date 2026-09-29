---
id: sl-gap-fill-ignores-gap
status: 裁定待ち
priority: 未設定
opened: 2026-09-06
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [sl-gap-fill-ignores-gap]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[sl-gap-fill-ignores-gap]** (2026-09-06、ローカル 1 周目 C8b-1 の副産物、設計判断待ち): 訂正 (09-06 codex 2 周目): `paper_fills.check_exit` は**始値 gap は始値で滑らせる (実装済)**。楽観なのは「始値は SL 上、足中スパイクで SL を大きく突き抜ける」ケースのみ (SL 価格で約定、実測 147.795、`c8cdb68` で pin)。MT5 の逆指値は触れた瞬間の成行なので平常時は現実装に近く、指標時の足中スパイクだけ楽観。粗い基底ほどずれが広がる。実運用 (MT5) では gap 先の価格で約定する。判断: (a) 足中も low/high で約定 (悲観過多、非推奨) (b) 現状維持 + 設計書 v2 の「SL gap」を「始値 gap (実装済) / 足中スパイク (楽観)」に訂正 + base_interval 制約欄に追記 (**推奨**) (c) 足中スパイクに固定 slippage 設定。**裁定 2026-09-06: (b)** — 設計書 §5 ペーパー約定規則に SL 約定価格規則と楽観ずれを明記 (済)。実装変更なし。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

