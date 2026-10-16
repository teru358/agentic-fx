---
id: deployed-plugin-has-no-disable-or-rollback
title: 配備済み plugin を止める・前の版に戻す操作が無い
status: 実装待ち
priority: 高
opened: 2026-10-04
closed: null
related: [retire-symlink-deployed-plugin]
---
# [deployed-plugin-has-no-disable-or-rollback] 配備済み plugin を止める・前の版に戻す操作が無い

**状態**: 実装待ち / **優先**: 高

## 現象

承認して配備した plugin を取り消す手段が無い。承認済みの行への reject は CAS (status='pending' 限定、approvals.py:77-86) で拒否され、afx plugin retire は plain directory 専用で配備済みの symlink を拒否する (switch.py:1856-1858)。鍵が漏れたとき・誤って承認したときの回復手段が無い (操作 API 設計レビュー r1 F4、2026-10-03)。

## 原因

切替ジャーナルに disable / rollback の遷移が無い。

## 処置案・裁定

切替ジャーナルの規律に乗る disable (配備を外す) と rollback (前の版へ戻す) を設計する。入ったら操作 API の approver 権限の endpoint と、鍵が漏れたときの手順に足す。着手はウィザード完了後 (2026-10-04 ユーザー裁定)。それまで運用文書に『回復手段は未整備』と明記する。

## 修正内容

## 経緯

- 2026-10-04: 起票。
- 2026-10-16: 状態: 設計待ち — 2026-10-16 ユーザー承認で設計着手 (C0 を sonnet、tmp/design-plugin-rollback/C0.md)。retire-symlink-deployed-plugin を統合
- 2026-10-16: 設計収束 (C0 v0.8、sol 8 周: r1 C2/H4 → r2 C1/H4 → r3 C1/M7 → r4 C1/M8 → r5 H1 → r6 H1 → r7 H1 → r8 C0/H0/M1/L2 = 着手可)。ユーザー裁定: U1 CLI のみ (API/bot に出さない) / U2 非対称反映を許容 / U3 版 dir は自動削除しない。spec 清書 → docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md v1.0 + switch-ops-hardening v1.9。task 表 T1 → T2 → (T3, T4a, T6) → T5 → T4b → T7 → T8。
- 2026-10-16: 状態: 実装待ち — 設計収束、spec 清書中
