---
id: killswitch-reset-implicit-generation-and-dd-preview
title: kill switch の解除で世代の入力を不要にし、確認画面に現在の DD と再ラッチの予告を出す
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-11-16
closed: null
related: [killswitch-reset-hwm-rebase, killswitch-latched-activity-repeats, ctl-guard-recovery-entry]
---
# [killswitch-reset-implicit-generation-and-dd-preview] kill switch の解除で世代の入力を不要にし、確認画面に現在の DD と再ラッチの予告を出す

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

`afx ctl killswitch reset <世代>` は利用者に世代を打たせるが、確認画面でも世代・発動時刻・equity を表示して reset の入力を求めており二重。解除は常に現在のラッチが対象なので、CLI が表示した世代を内部で照合 (CAS) に使えば足りる (応答喪失後の送り直しで新しいラッチを外さない保護は残る)。また解除後に DD が閾値以上のままだと次の新規判断で必ず再ラッチする (2026-11-13 の解除後、11-16 に gen=2 で再ラッチ) が、今の reset は成功後に固定文を出すだけで DD を示さない。確認の前に server 計算の現在 DD と閾値を出し、閾値以上なら「このまま解除すると次の新規判断で再ラッチ。先に rebase」を示す (解除は止めない)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-16: 起票。
- 2026-11-26: 関連: [killswitch-reset-hwm-rebase, killswitch-latched-activity-repeats, ctl-guard-recovery-entry] — 2026-11-26: 運用の全体整理の裁定 A で ctl-guard-recovery-entry に吸収する (世代入力の撤去と DD の予告はその一部)
