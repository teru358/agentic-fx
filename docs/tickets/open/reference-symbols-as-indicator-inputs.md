---
id: reference-symbols-as-indicator-inputs
title: 戦略 plugin に株価指数など参照 symbol の値を指標として渡す
status: 実装中
priority: 中
opened: 2026-10-08
closed: null
related: [multi-timeframe-indicator-deps, executor-pair-membership-guard]
---
# [reference-symbols-as-indicator-inputs] 戦略 plugin に株価指数など参照 symbol の値を指標として渡す

**状態**: 実装中 / **優先**: 中 / **種別**: 機能

## 現象

plugin 契約は 1 pair × 1 足の df のみで、株価指数・ドル指数などの参照 symbol を判断材料にできない。datafeed.watch_symbols (取引不可・分析専用) は設定にあるが相関分析の候補にしか使われず、ingest も plugin も参照していない。非 FX symbol の取得経路も無い。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-08: 起票。
- 2026-10-08: 状態: 設計待ち — 設計案: plugin 契約 (df は取引 pair のまま) は変えず、indicators 宣言に参照 symbol (例 JP225 / US500 / DXY) を書けるようにし、watch_symbols を ingest に配線して MT5 bridge の指数 CFD を主源にする (yfinance は補助)。論点: 取引時間差の時刻整合 (直近確定値 + 鮮度、stale を明示して渡す、実時刻で pin)、backtest 側に同じ整合規則で供給、発注対象と参照対象の区別は watch_symbols 側で決定論的に守る。multi-timeframe-indicator-deps と同じ束で設計。着手はウィザード完了後 (2026-10-08 ユーザー方針)。
- 2026-10-08: 状態: 設計待ち — ユーザーの理想像 (2026-10-08): 株価指数・金利などの参照系列とドル円の相関を見られること、各参照系列に EMA 等の標準指標を計算できること、戦略 plugin に参照系列のデータ (スカラーだけでなく系列) を渡せること。上の「指標として渡す」案は最小形であり、実装時に設計を改めてユーザーと確認する (勝手に確定しない)。
- 2026-10-25: 状態: 設計待ち — 2026-10-25: C0 v0.1 を docs/superpowers/specs/2026-10-25-reference-series-design.md に清書 (ブローカー symbol 実測: 指数 CFD 10 本、金利・DXY なし。soft 取得、indicators 宣言に symbol / timeframe / max_age、passthrough indicator ohlcv、整列と鮮度の規則、backtest は fail closed、裁定 5 点)。multi-timeframe-indicator-deps を同束。ユーザー確認待ち
- 2026-10-25: 状態: 設計待ち、関連: [multi-timeframe-indicator-deps, executor-pair-membership-guard] — 2026-10-25: sol 設計レビュー 1 周目 (tmp/review-ref/c0-r1-out.md): Critical 5 / Important 11 / Minor 4、現 C0 は承認不可、C1 改稿は可。C1 の必要条件 10 (MT5 限定 source と raw symbol、hard-first ingest と soft backoff、判定時刻 d = s + tf 基準の asof、4h / 1d は 1h から導出、handshake と call payload の分離と入力 byte 上限、warmup / max_bars の明示契約、executor の pairs membership guard = executor-pair-membership-guard、live は strategy 単位の fail-closed skip、backtest の同一 source / base interval、改善 worker に履歴範囲を出さない)。task は T1 契約・安全境界 → T2 → T3 → T4 の直列。ユーザー裁定: 作業は一旦後回し
- 2026-10-28: 状態: 実装中 — 2026-10-28: 設計 v0.6 C1 (sol 5 周で閉鎖、R1/R2/R4 推奨どおり裁定、R5 は MT5 限定)、T1 着手
