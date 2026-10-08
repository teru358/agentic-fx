---
id: improve-targeted-run
status: 設計待ち
priority: 中
opened: 2026-09-12
closed: null
related: [id, indicator-consumption-wiring]
backfilled: true
source_section: 未完了
---
# [improve-targeted-run]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-targeted-run] (中、ユーザー要望 2026-09-12「戦略作成の準備として CLI から indicator を準備させる経路」) 現状は `improve add <text>` + 引数なし `improve` で LLM が open から自分で選ぶため、特定課題 (例: MACD indicator) を指名して今すぐ作らせる経路が無い。案: `afx> improve run <backlog_id>` (その 1 件だけを assigned にした one-shot mission、`submit_manual(backlog_ids=[id])`) + `improve add --kind indicator|strategy` で課題に kind を持たせ、選択とプロンプト文面 (indicator は backtest 不要、strategy は backtest 必須) を kind で分ける。前提: [indicator-consumption-wiring] が無いと作った indicator を strategy が使えないので、順序は wiring → targeted-run

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

