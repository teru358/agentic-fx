---
id: improve-targeted-run
status: 設計待ち
priority: 低
opened: 2026-09-12
closed: null
related: [id, indicator-consumption-wiring, improve-mission-indicator-creation-path]
backfilled: true
source_section: 未完了
---
# [improve-targeted-run]

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-targeted-run] (中、ユーザー要望 2026-09-12「戦略作成の準備として CLI から indicator を準備させる経路」) 現状は `improve add <text>` + 引数なし `improve` で LLM が open から自分で選ぶため、特定課題 (例: MACD indicator) を指名して今すぐ作らせる経路が無い。案: `afx> improve run <backlog_id>` (その 1 件だけを assigned にした one-shot mission、`submit_manual(backlog_ids=[id])`) + `improve add --kind indicator|strategy` で課題に kind を持たせ、選択とプロンプト文面 (indicator は backtest 不要、strategy は backtest 必須) を kind で分ける。前提: [indicator-consumption-wiring] が無いと作った indicator を strategy が使えないので、順序は wiring → targeted-run

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-29: 優先: 低、関連: [id, indicator-consumption-wiring, improve-mission-indicator-creation-path] — 2026-10-29 棚卸し: ctl improve の -c 一時指示 (10/27 merge) が部分吸収の疑い。backlog 指名実行・kind 付与は残る。indicator 新設は improve-mission-indicator-creation-path が担当
