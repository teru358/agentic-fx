---
id: auto-killswitch-latch-emits-no-event
title: drawdown による kill switch の自動作動が通知されない
status: 実装中
priority: 高
kind: 不具合
opened: 2026-11-13
closed: null
related: [bot-gateway-disconnect-is-silent]
---
# [auto-killswitch-latch-emits-no-event] drawdown による kill switch の自動作動が通知されない

**状態**: 実装中 / **優先**: 高 / **種別**: 不具合

## 現象

executor が drawdown の閾値で kill switch をラッチしても ops event (guard_state_changed latched) を出さないため、bot・web に作動が届かない。event を出すのは ops の reset (released) と reconcile (latched) だけ。2026-11-13 07:20 JST に 2.20% >= 2.0% でラッチしたが ops_events に記録がなく、利用者は取引停止に気づかなかった。設計 2026-10-20-trade-notices-design.md:488 で別チケットにするとしていたが未起票だった。直し方の案: ラッチの遷移 (false→true) を書いた側で guard_state_changed (latched, 世代) を best-effort で出す。DurabilityUncertain の経路も含め、遷移ごとに 1 回だけ出す。daemon 再起動時の取りこぼしは起動時に state と最終 event を照合して補う。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。
- 2026-11-14: 状態: 実装中 — 設計 C4 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-ops-alert-delivery-design.md)。直し方は producer best-effort ではなく ops lane 内の単一 guard sweep が state と既存 event を照合して欠けた latched / released を出す形に変えた。完了条件は「遷移ごとに 1 回」から縮め、解除済みで event が無い世代 (clear + event 無し) には後から通知しない (古い「作動しました」を出さないため)。activity の重複・世代の下限は別チケット。
