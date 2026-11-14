---
id: bot-gateway-disconnect-is-silent
title: bot が Discord から切断されたまま黙って生き続ける
status: 実装中
priority: 高
kind: 不具合
opened: 2026-11-13
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [bot-gateway-disconnect-is-silent] bot が Discord から切断されたまま黙って生き続ける

**状態**: 実装中 / **優先**: 高 / **種別**: 不具合

## 現象

afx-ops@bot の process は active のまま Discord gateway への接続が無く、bot が offline になっていた (2026-11-13 に発見、再起動で復旧)。discord.py の再接続 (connect(reconnect=True)) に任せており、discord の logger は NullHandler のため切断・再接続失敗が journal に何も残らない。process は dumpable でなく外から stack も fd も見えないので、原因の時刻も特定できなかった。直し方の案: discord の logger の WARNING 以上を journal へ出す。gateway が一定時間 (例: 10 分) READY/RESUMED でなければ BotFailure で落とし systemd に再起動させる。afx status / web に bot の gateway 状態を出す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-13: 起票。
- 2026-11-14: 状態: 実装中 — 設計 C4 を 2026-11-14 に利用者承認 (同上の spec)。直し方は discord の WARNING を出す案ではなく、gateway・送信・poll・event loop・停止処理の進捗監視と exit 分類 (一時 79 / loop 停止 80 / 恒久 78) + bot 専用 systemd drop-in の backoff 再起動、health を /v1/status と web に出す形に変えた。discord.py の log template 棚卸しは別チケット。
- 2026-11-14: 状態: 実装中 — 直し方の案の処置: discord の WARNING を journal へ出す案は採らず、gateway・配送・poll・event loop・停止処理を進捗監視し、10 分止まれば固定語で終了する (一時 79 / loop 停止 80 / 恒久 78)。bot 専用 systemd drop-in (30 秒から最大 10 分の backoff、start-limit 無し、恒久コードは再起動しない) で再起動し、systemd 254 未満では bot の enable を拒否する。health は gateway.snapshot から /v1/status・afx status・afx ops bot status・web に出し、notify 未設定は alternate_push=none の固定警告で示す。
- 2026-11-14: 関連: [auto-killswitch-latch-emits-no-event] — 2026-11-14: main への merge を統合 branch で判定中。kill switch の遷移は ops lane 内の単一 guard sweep が state と既存 event を照合して event にする (reset・reconcile・shell の前後、起動時、tick)。reset の成功時に再ラッチの固定文。bot は gateway・配送・poll・event loop・停止を監視し、10 分止まれば一時 (79 / 80)、直らない拒否は恒久 (78) で exit、bot 専用の systemd drop-in で backoff と恒久停止。health を /v1/status・afx status・web に出し、notify が無ければ alternate_push=none を警告。運用は docs/operations/bot-and-killswitch.md。
