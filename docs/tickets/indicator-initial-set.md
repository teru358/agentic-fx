---
id: indicator-initial-set
status: 設計待ち
priority: 未設定
opened: 2026-09-15
closed: null
related: [indicator-consumption-wiring]
backfilled: true
source_section: 未完了
---
# [indicator-initial-set]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[indicator-initial-set]** (2026-09-15 ユーザー方針、[indicator-consumption-wiring] 完了直後に着手): 標準指標を人間提供で初期導入する — sma / ema (`value`)、rsi Wilder (`rsi`)、macd (`macd`/`signal`/`hist`)、bollinger (`upper`/`middle`/`lower`)、atr (`atr`)、adx (`adx`/`plus_di`/`minus_di`)、stochastic (`k`/`d`)、ichimoku (`tenkan`/`kijun`/`senkou_a`/`senkou_b`/`chikou`、先行スパンは未来へずらさず現在バー時点で確定する値として定義 = lookahead 禁止)。全部系列版 + `outputs` 宣言。fable がひな形、ユーザーが submit → approve。調整は U3 の params 上書き、改造は複製 → 別名 → 承認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

