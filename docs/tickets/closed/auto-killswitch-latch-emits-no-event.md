---
id: auto-killswitch-latch-emits-no-event
title: drawdown による kill switch の自動作動が通知されない
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-13
closed: 2026-11-14
related: [bot-gateway-disconnect-is-silent, killswitch-reset-hwm-rebase]
---
# [auto-killswitch-latch-emits-no-event] drawdown による kill switch の自動作動が通知されない

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

executor が drawdown の閾値で kill switch をラッチしても ops event (guard_state_changed latched) を出さないため、bot・web に作動が届かない。event を出すのは ops の reset (released) と reconcile (latched) だけ。2026-11-13 に drawdown が閾値を超えてラッチしたが ops_events に記録がなく、利用者は取引停止に気づかなかった。設計 2026-10-20-trade-notices-design.md:488 で別チケットにするとしていたが未起票だった。直し方の案: ラッチの遷移 (false→true) を書いた側で guard_state_changed (latched, 世代) を best-effort で出す。DurabilityUncertain の経路も含め、遷移ごとに 1 回だけ出す。daemon 再起動時の取りこぼしは起動時に state と最終 event を照合して補う。

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-14: 2026-11-14 に配備 (main a8fdc0e8、afx 再起動)。ops lane 内の単一 guard sweep が state と既存 event を照合して latched / released を出す (起動時・tick・ops と shell の reset / reconcile の前後)。起動時の sweep は 11-13 の released だけの世代を閉じた世代として飛ばした (guard_notice_backfill_skipped)。reset 成功時に再ラッチの固定文。tests/ 全体と afx-ops は統合 branch で緑 (既存の opencode の赤 1 を除く)。activity の重複・世代の下限は別チケット。

## 経緯

- 2026-11-13: 起票。
- 2026-11-14: 状態: 実装中 — 設計 C4 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-ops-alert-delivery-design.md)。直し方は producer best-effort ではなく ops lane 内の単一 guard sweep が state と既存 event を照合して欠けた latched / released を出す形に変えた。完了条件は「遷移ごとに 1 回」から縮め、解除済みで event が無い世代 (clear + event 無し) には後から通知しない (古い「作動しました」を出さないため)。activity の重複・世代の下限は別チケット。
- 2026-11-14: 状態: 実装中 — 直し方の案の処置: 「書いた側で best-effort に 1 回出す」は採らず、ops lane 内の単一 guard sweep (OpsService.emit_pending_guard_notices) だけが guard_state_changed を出す。daemon 起動時・scheduler tick 後・ops と shell の reset / reconcile の前後で state と既存 event を照合し、欠けた latched / released を補う。通常の遅れは次の tick まで。reset 成功時は shell と afx ctl の両方が固定警告 (DD が閾値以上のままなら再ラッチする旨、数値なし) を 1 回出す。HWM の付け直しは別チケット。
- 2026-11-14: 関連: [bot-gateway-disconnect-is-silent, killswitch-reset-hwm-rebase] — 2026-11-14: main への merge を統合 branch で判定中。kill switch の遷移は ops lane 内の単一 guard sweep が state と既存 event を照合して event にする (reset・reconcile・shell の前後、起動時、tick)。reset の成功時に再ラッチの固定文。bot は gateway・配送・poll・event loop・停止を監視し、10 分止まれば一時 (79 / 80)、直らない拒否は恒久 (78) で exit、bot 専用の systemd drop-in で backoff と恒久停止。health を /v1/status・afx status・web に出し、notify が無ければ alternate_push=none を警告。運用は docs/operations/bot-and-killswitch.md。
- 2026-11-14: 是正内容 — 2026-11-14 に配備 (main a8fdc0e8、afx 再起動)。ops lane 内の単一 guard sweep が state と既存 event を照合して latched / released を出す (起動時・tick・ops と shell の reset / reconcile の前後)。起動時の sweep は 11-13 の released だけの世代を閉じた世代として飛ばした (guard_notice_backfill_skipped)。reset 成功時に再ラッチの固定文。tests/ 全体と afx-ops は統合 branch で緑 (既存の opencode の赤 1 を除く)。activity の重複・世代の下限は別チケット。
