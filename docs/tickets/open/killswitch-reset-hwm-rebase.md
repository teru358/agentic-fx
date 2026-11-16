---
id: killswitch-reset-hwm-rebase
title: kill switch の手動解除で HWM を付け直す明示操作が無い
status: 実装中
priority: 高
kind: 機能
opened: 2026-11-13
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [killswitch-reset-hwm-rebase] kill switch の手動解除で HWM を付け直す明示操作が無い

**状態**: 実装中 / **優先**: 高 / **種別**: 機能

## 現象

live の kill switch reset は latch を解くだけで HWM を付け直さない。HWM は含み益込みなので、実現損の後に建玉が無いと DD が閾値以上のまま残り、解除しても次の open intent の gate 評価で必ず再ラッチする (2026-11-13 の解除後がこの状態)。利用者裁定 (2026-11-13 R4): 通知の設計とは分け、損失予算を人が切り下げる明示操作 (例: 通常の reset と別の rebase 操作、確認文・監査つき) として本チケットで設計する。replay の自動解除の rebase との意味の違いも整理する。

## 原因

## 処置案・裁定

利用者裁定 (2026-11-13 R4、設計 C2.1 で 2026-11-14 承認。正本は docs/superpowers/specs/2026-11-14-killswitch-hwm-rebase-design.md の §1.2 と §5):

- 累積の切下げに硬い上限・保護下限・回数上限は置かない。1 回の付け直しに閾値分の実損が要り、同じ本人が config で DD 閾値を上げられるので、硬い上限は迂回不能な保護にならないため。代わりに台帳 (append-only)・累積の表示・毎回の確認で扱う。
- 累積警告: 各回の切下げ率の合計が現在の DD 閾値の 3 倍を超えたら固定の警告を出す (拒否はしない。倍率は 3 に固定)。
- 対象は PaperBroker 限定。flat、新鮮な snapshot、DD ≥ 閾値、世代 > 0 が条件。
- 実口座 broker を導入するチケットでは、HWM rebase の硬い累積損失上限を必須要件とする。上限到達時の正規の復旧出口、入出金の正本、broker 側だけの exposure、通貨精度も設計・検証し、完了するまで実口座の rebase を有効にしない。paper 用の警告をその代用にしない (全体設計書 §5 に追記済み)。
- paper.starting_balance の変更は cashflow 履歴に残らない。台帳は rebase の累積だけを意味し、入出金調整済みの総損失を証明しない。この問題は別チケット paper-starting-balance-cashflow-history で扱い、本件の前提にしない。

## 修正内容

## 経緯

- 2026-11-13: 起票。
- 2026-11-14: 状態: 実装待ち — 設計 C2.1 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-killswitch-hwm-rebase-design.md)。reset と別の操作 afx ctl --as approver killswitch rebase <世代> で HWM を今の equity に付け直す。PaperBroker・flat・新鮮な snapshot・DD≥閾値・世代>0 が条件。累積に硬い上限は置かず、各回の率の合計が閾値の 3 倍を超えたら警告。実口座 broker の導入時は硬い上限を必須とする。実装は運用異常の通知 (opsalert) の guard sweep の後。
- 2026-11-15: 状態: 実装中 — T1 (台帳・原子更新)・T2 (排他・API) は閉鎖済み。T3 で afx ctl killswitch rebase・固定文・文書を実装。T4 (非回帰) が残る
