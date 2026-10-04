---
id: deployed-plugin-has-no-disable-or-rollback
title: 配備済み plugin を止める・前の版に戻す操作が無い
status: 設計待ち
priority: 高
opened: 2026-10-04
closed: null
related: [retire-symlink-deployed-plugin]
---
# [deployed-plugin-has-no-disable-or-rollback] 配備済み plugin を止める・前の版に戻す操作が無い

**状態**: 設計待ち / **優先**: 高

## 現象

承認して配備した plugin を取り消す手段が無い。承認済みの行への reject は CAS (status='pending' 限定、approvals.py:77-86) で拒否され、afx plugin retire は plain directory 専用で配備済みの symlink を拒否する (switch.py:1856-1858)。鍵が漏れたとき・誤って承認したときの回復手段が無い (操作 API 設計レビュー r1 F4、2026-10-03)。

## 原因

切替ジャーナルに disable / rollback の遷移が無い。

## 処置案・裁定

切替ジャーナルの規律に乗る disable (配備を外す) と rollback (前の版へ戻す) を設計する。入ったら操作 API の approver 権限の endpoint と、鍵が漏れたときの手順に足す。着手はウィザード完了後 (2026-10-04 ユーザー裁定)。それまで運用文書に『回復手段は未整備』と明記する。

## 修正内容

## 経緯

- 2026-10-04: 起票。
