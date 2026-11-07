---
id: approval-retry-button-always-shown
title: Discord の承認カードに Retry が常に出て、未承認の依頼では承認と同じになる
status: 設計待ち
priority: 低
kind: 機能
opened: 2026-11-07
closed: null
related: [approval-base-identity-cas]
---
# [approval-retry-button-always-shown] Discord の承認カードに Retry が常に出て、未承認の依頼では承認と同じになる

**状態**: 設計待ち / **優先**: 低 / **種別**: 機能

## 現象

afx-ops の承認カード (afx-ops/afx_ops/gate/render.py:378-384 primary_view) は Approve・Reject・Retry を常に並べる。Retry は承認の手順を頭から流し直す (plugin/switch.py:2464-2490 retry_approval は approve_candidate を呼ぶだけ) ので、一度も承認していない依頼で押すと承認になる。本来の用途は前回の承認が途中で止まって pending に残った依頼 (legacy_plain_present の retire 後、lock 待ち・中断、切替途中) のやり直し。案: Retry は前回の承認の試行が pending のまま残った依頼 (reason 付き、または自分の未完 journal あり) にだけ出す。再評価の依頼には出さない (retry は拒否される)。2026-11-07 利用者の質問から起票

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-07: 起票。
