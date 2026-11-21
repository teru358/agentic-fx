---
id: signal-mission-max-turns-failures
title: signal で起動する取引判断の 4 割が手数の上限で判断を出せずに終わる
status: 実装中
priority: 高
kind: 不具合
opened: 2026-11-21
closed: null
related: [weekly-improve-rebacktests-deployed-strategies, strategy-attribution-and-shadow-evaluation]
---
# [signal-mission-max-turns-failures] signal で起動する取引判断の 4 割が手数の上限で判断を出せずに終わる

**状態**: 実装中 / **優先**: 高 / **種別**: 不具合

## 現象

2026-11-09 以降、signal で起動した trade mission 102 本のうち 44 本が `max_turns` (16) で終わり、判断を出していない (11-10 は 12 本中 10 本)。同じ期間の cron の mission は 858 本中 completed 742・max_turns 49・timeout 66 で、signal 起動の方が大きく悪い。runner は local (qwen3.8-27b-Q5-nothink)。

失敗した mission も `signal_daily_max` の 1 枠を消費し、requeue された古い signal が oldest-first で再び枠を食う (retry_exhausted / requeued_expired 9 件)。日次上限 12 では 1 日 10〜31 件の signal の大半が上限到達後に freshness_expired で捨てられていた (11-09 以降 166 件中 98 件)。判断に回るのはその日の早い時間の signal だけになり、配備中の戦略の機械採点が少数・時間帯の偏りを持つ。

## 原因

max_turns の 44 本は全件、16 ターンとも `search_news` を呼び続け、最終 JSON を一度も出していない (最終回答の試行・修復・ツールエラーは 0)。30 本は同じ query の繰り返し、14 本は言い換えの連続 (為替介入が「実施されたか」の確認や日付の語に寄る)。意味検索の上位 5 件はほぼ同じ記事が返る。同じ signal の再実行で結果が分かれる (max_turns の 26 本中 17 本が requeue 後に completed) ので、モデルのばらつきが主因。決定論的な欠けとして:
- runner は毎ターン `tool_choice: "auto"` で、残り手数を伝えず、最終ターンでもツールを使える。同じ呼出しの繰り返しも抑えない (`src/agentic_fx/runners/local_runner.py:167-179`)。cron の max_turns 49 本も最終回答の試行 0 (`get_positions` の連打)。
- signal の prompt (`src/agentic_fx/loops/prompts/signal_decision_mission.md`) に検索回数の目安・打ち切りの条件が無い。
- `search_news` の結果に記事の時刻が無い (`src/agentic_fx/tools/news_tools.py:12-14`、`added_at` は `src/agentic_fx/store/rag.py:155-159` で書き込まれる)。
ツール結果の大きさ・prompt の長さ・timeout は原因ではない (失敗の中央値 74 秒、timeout 300 秒)。

## 処置案・裁定

- 2026-11-21 利用者裁定: 当面 `signal_daily_max` を 36 に上げる (個人設定、次の再起動で反映)。失敗の原因は本チケットで調べる。成績の悪い戦略 (close_pressure_followthrough・volume_close_pressure) は止めず、判断に回る件数が増えて採点が進んでから改善の対象にする。
- 2026-11-21 利用者承認の方針 (設計 docs/superpowers/specs/2026-11-21-trade-mission-turn-budget-design.md の元): (a) 最後の 1〜2 ターンはツールを渡さず JSON だけを出させる (b) 同じツール・同じ引数の呼出しは実行せず既出と返す (c) signal の判断の `search_news` に回数の予算を置き、予算切れは固定の返答にする (d) `search_news` の結果に記事の取込み時刻を含める。prompt に検索回数の目安・「前提を壊す事実を確かめられなければ accept」・「介入の噂や警戒だけでは news_shock にしない」を書く。

## 修正内容

## 経緯

- 2026-11-21: 起票。
- 2026-11-21: transcript 102 本を集計し原因を記録。
- 2026-11-21: 設計 docs/superpowers/specs/2026-11-21-trade-mission-turn-budget-design.md C1 を利用者が承認 (問い 1 は推奨どおり)。日次上限の使い切りと鮮度切れの関係を DB で確認 (signal% mission は 10 日中 8 日で 12 本ちょうど、鮮度切れ 98 件は全部が上限到達後、consumed 58 件は全部が到達前)。
