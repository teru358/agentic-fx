---
id: sandbox-lifecycle-minor-followups
title: worker 観測境界の残り: ready 直後の死亡で code が割れる、細部の未 pin
status: 設計待ち
priority: 低
opened: 2026-09-30
closed: null
related: [worker-death-cause-observed-by-parent]
---
# [sandbox-lifecycle-minor-followups] worker 観測境界の残り: ready 直後の死亡で code が割れる、細部の未 pin

**状態**: 設計待ち / **優先**: 低

## 現象

ready 応答の直後に死んだ worker は、観測の順序によって最初の call の code が backtest_failed と crashed に割れる (稀、spec 違反ではない)。終端化時の error_code 先入れ・診断ログの出力時点・process group への kill 1 回ガード・pgid の記録は、壊してもテストが通る。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-09-30: 起票。
