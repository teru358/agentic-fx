---
id: datafeed-outage-discord-notice
title: MT5 不通 (degraded) と復帰待ちを Discord に通知する
status: 設計待ち
priority: 高
kind: 機能
opened: 2026-10-31
closed: null
related: []
---
# [datafeed-outage-discord-notice] MT5 不通 (degraded) と復帰待ちを Discord に通知する

**状態**: 設計待ち / **優先**: 高 / **種別**: 機能

## 現象

OANDA の TOTP 必須化 (2026-10-31) で MT5 ターミナルの再ログインは人手でしか復旧しない。datafeed の停止判定 (data_outage_degraded の confirmed、datafeed_recovered_awaiting_resume、data_outage_unprocessed_bars) は activity にしか書かれず Discord には届かないため、取引停止が続いても人が気づけない。通知は固定語で数値なし、episode あたり 1 回 (confirmed 時) + 復帰待ち 1 回。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。
- 2026-11-21: 優先: 高 — 2026-11-21 再発で優先を高に: 11-19 22:40Z に USDJPY 1m が 1 分 stall → 建玉 1 つで auto resume にならず人の resume 待ち (pending_human_confirmation)。Discord に届かず、判断の cron mission が 11-20 の取引日のほぼ全部 (約 23 時間) 止まった。9 月 (epoch 1・2) も同じ形で 10〜12 時間止まっている。指揮者が再起動後に active と warning だけ見て data: を見落とした。11-21 08:24 JST に利用者が端末で data resume --acknowledge (開場後の最初の tick で ready)。通知は ops_events の固定語 event にして bot が拾う形 (既存の運用異常の通知と同じ型)、episode あたり confirmed と再開待ちの 2 回
- 2026-11-21: 状態: 設計待ち — 訂正: 11-21 08:24 JST の resume は要求を記録しただけで、開場の最初の tick で受理か拒否かが判定される (受理されるとは限らない。未観測)。設計 docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md をレビュー中
