---
id: wizard-stage5-timeframe-menu
title: 段 5 の判断足は対応する足の番号選択にする
status: 設計待ち
priority: 低
opened: 2026-10-14
closed: null
related: [first-run-setup, wizard-stage4-menu-per-role]
---
# [wizard-stage5-timeframe-menu] 段 5 の判断足は対応する足の番号選択にする

**状態**: 設計待ち / **優先**: 低

## 現象

段 5 は「15m / 1h (推奨)」の自由入力で、10m のような未対応の値も打てる (打つと Settings 検証で弾かれて再質問)。製品が知る足は datafeed.sources.INTERVAL_MIN の 1m 5m 15m 30m 1h 4h 1d に固定なので、この集合から番号で選ばせる: 15m / 30m / 1h (推奨、既定) / 4h / 1d を選択肢に、1m / 5m は「判断の上限時間より短いため選べません」と表示だけ。再実行は現在値を既定に。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
