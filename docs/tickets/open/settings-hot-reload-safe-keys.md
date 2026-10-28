---
id: settings-hot-reload-safe-keys
title: 設定の一部を daemon 再起動なしで反映する (安全なキーに限定)
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-10-28
closed: null
related: [improve-gpt6-models-fail-isolated-selftest]
---
# [settings-hot-reload-safe-keys] 設定の一部を daemon 再起動なしで反映する (安全なキーに限定)

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

config/settings.yaml は daemon 起動時に 1 回だけ読まれ (service.py の load_settings)、improve / trade の runner の model 変更でも systemctl --user restart afx が要る。再起動は走行中の改善の繰り返し job を中断し、起動時の隔離 admission (canary + 自己試験) をやり直す (2026-10-27/28、gpt-6-luna の切替で 2 回ずつ再起動)。案: 明示操作 (例 `afx ctl config reload`、自動のファイル監視はしない) で、許可したキーの集合 (runner.improve / runner.trade の model・backend、通知の on/off 等) だけを検証して差し替え、集合外のキーの変更は「再起動が要る」と一覧で返して何も変えない。資金・risk gate・kill switch・mode・autopilot・pairs・datafeed・隔離の設定は対象外 (config では切替不可の規律を保つ)。improve の backend / model を変えたら admission を再判定し、自己試験の結果を返す。走行中の mission は旧設定のまま完走。決めること: 許可キーの集合、settings の版と migration との関係、trade loop の判断足途中での差し替え時点、activity と Discord への記録、afx-ops 側の設定 (ops.toml) を含めるか。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-28: 起票。
