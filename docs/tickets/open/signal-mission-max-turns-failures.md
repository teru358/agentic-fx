---
id: signal-mission-max-turns-failures
title: signal で起動する取引判断の 4 割が手数の上限で判断を出せずに終わる
status: 調査待ち
priority: 高
kind: 不具合
opened: 2026-11-21
closed: null
related: [weekly-improve-rebacktests-deployed-strategies, strategy-attribution-and-shadow-evaluation]
---
# [signal-mission-max-turns-failures] signal で起動する取引判断の 4 割が手数の上限で判断を出せずに終わる

**状態**: 調査待ち / **優先**: 高 / **種別**: 不具合

## 現象

2026-11-09 以降、signal で起動した trade mission 102 本のうち 44 本が `max_turns` (16) で終わり、判断を出していない (11-10 は 12 本中 10 本)。同じ期間の cron の mission は 858 本中 completed 742・max_turns 49・timeout 66 で、signal 起動の方が大きく悪い。runner は local (qwen3.8-27b-Q5-nothink)。

失敗した mission も `signal_daily_max` の 1 枠を消費し、requeue された古い signal が oldest-first で再び枠を食う (retry_exhausted / requeued_expired 9 件)。日次上限 12 では 1 日 10〜31 件の signal の大半が上限到達後に freshness_expired で捨てられていた (11-09 以降 166 件中 98 件)。判断に回るのはその日の早い時間の signal だけになり、配備中の戦略の機械採点が少数・時間帯の偏りを持つ。

## 原因

未調査。調べること: signal mission の prompt と使うツール (get_signals ほか) の往復の数、max_turns で終わった transcript のツール呼出しの並び、cron との差。

## 処置案・裁定

- 2026-11-21 利用者裁定: 当面 `signal_daily_max` を 36 に上げる (個人設定、次の再起動で反映)。失敗の原因は本チケットで調べる。成績の悪い戦略 (close_pressure_followthrough・volume_close_pressure) は止めず、判断に回る件数が増えて採点が進んでから改善の対象にする。

## 修正内容

## 経緯

- 2026-11-21: 起票。日次上限の使い切りと鮮度切れの関係を DB で確認 (signal% mission は 10 日中 8 日で 12 本ちょうど、鮮度切れ 98 件は全部が上限到達後、consumed 58 件は全部が到達前)。
