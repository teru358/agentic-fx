---
id: ctl-guard-recovery-entry
title: 保護の回復を afx ctl の 1 か所から番号で選べるようにする
status: 設計待ち
priority: 高
kind: 機能
opened: 2026-11-26
closed: null
related: [killswitch-reset-implicit-generation-and-dd-preview, killswitch-reset-hwm-rebase, datafeed-pending-auto-release-when-flat]
---
# [ctl-guard-recovery-entry] 保護の回復を afx ctl の 1 か所から番号で選べるようにする

**状態**: 設計待ち / **優先**: 高 / **種別**: 機能

## 現象

kill switch の解除・HWM の付け直し・データ再開は、通知を読んだ運用者が `afx ctl --as approver killswitch rebase <世代>` や `data resume --acknowledge` を自分で組み立てて端末で打つ必要がある。通知には次の操作が無いか長いコマンド文字列だけがあり、reset は現在の DD を見ずに通って次の新規判断で再ラッチし得る。2026-11-24〜26 の kill switch 2 回・データ再開待ち 1 回で、利用者が手順を組み立てる負担が表面化した。裁定 (2026-11-26、運用の全体整理): `afx ctl` (引数なし、端末) が対応の要る項目を番号で出し、選ぶと今の確認画面から実行する。--as・世代・フラグは打たない。approver 鍵の使用、rebase と reset の別要求・別確認、損失受容の確認、rebase の前提は変えない。通知と web は何が起きたかと「端末で afx ctl」まで (実行は UDS のまま)。通常の承認は Discord ボタンのまま。kill switch の項目では現在の DD と閾値を出し、閾値以上なら rebase の確認を経て reset する流れを選べる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-26: 起票。
