---
id: approval-notice-shows-strategy-for-indicator
title: 指標の承認・配備の通知が「戦略」と表示される
status: 是正済
priority: 中
kind: 不具合
opened: 2026-10-28
closed: 2026-10-29
related: []
---
# [approval-notice-shows-strategy-for-indicator] 指標の承認・配備の通知が「戦略」と表示される

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

Discord の承認状態の通知が plugin の種別を見ずに「承認 #63 (戦略 downside_realized_variance_share) が承認・配備されました。」と出す。downside_realized_variance_share は指標 (indicator)。原因: afx-ops/afx_ops/gate/render.py の approval_state_changed の文面が「戦略」の固定語。承認通知の「承認 #7 / 戦略 sma_cross (新規)」系 (bot/adapter.py) も同じ疑い。是正: payload の plugin 種別 (strategy / indicator) を event か GET から受け、「戦略」「指標」を出し分ける。種別が読めなければ「plugin」。

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-29: afx-ops の承認状態通知が plugin 種別 (strategy / indicator、不明は plugin) を出し分ける。daemon は approval payload の kind を event の plugin_kind で運び、読み取り失敗は承認処理に影響させない。main d2c8bf76、配備 2026-10-29、本機の Discord で確認 (ユーザー)。

## 経緯

- 2026-10-28: 起票。
- 2026-10-29: 状態: 実装中 — 2026-10-29: main d2c8bf76 で merge・配備 (afx-ops 再起動 → daemon 再起動 19:34)。次に指標の承認・配備があったときに Discord の文面「(指標 …)」を確認して closed
- 2026-10-29: 是正内容 — afx-ops の承認状態通知が plugin 種別 (strategy / indicator、不明は plugin) を出し分ける。daemon は approval payload の kind を event の plugin_kind で運び、読み取り失敗は承認処理に影響させない。main d2c8bf76、配備 2026-10-29、本機の Discord で確認 (ユーザー)。
