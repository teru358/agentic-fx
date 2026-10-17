---
id: bot-event-notices-are-raw-log-lines
title: bot の通知が event の k=v 行そのままで人が読めない
status: 設計待ち
priority: 中
opened: 2026-10-17
closed: null
related: [approval-notice-lacks-change-content, ops-bot-display-residuals]
---
# [bot-event-notices-are-raw-log-lines] bot の通知が event の k=v 行そのままで人が読めない

**状態**: 設計待ち / **優先**: 中

## 現象

Discord への通知が『approval_state_changed: decision_code=approve, state=approved』『ops_request_terminal: authenticated_principal=ops-bot, endpoint_code=approve, result_code=ok』『job_state_changed: job_kind=approve, result_code=ok, state=done』のように event code と fields の k=v をそのまま 1 行ずつ投稿している。1 回の承認で 3 通届き、どれも人の言葉になっていない。承認が終わった投稿も『Approval is no longer active.』の 1 行だけで、何が承認され何が起動したかが残らない (2026-10-17 ユーザー報告)。

## 原因

afx-ops gate/render.py render_event が『allowlist 検証を通った code と fields を写す』だけで、ops-api spec §3.2 の『event 種別ごとの文面』が未実装。event 自体は設計どおり code・識別子・数のみ (ops/events.py _SAFE_VALUE、自由文を載せない) なので、人間向けの言い換えは bot 側の種別ごとの template で行うしかない。ops_request_terminal と job_state_changed は運用監査向けの event で、承認者には approval_state_changed 1 通で足りる。

## 処置案・裁定

設計事項 (C0 → レビュー → ユーザー承認、approval-notice-lacks-change-content と同じ束で): (1) event code × 主要 fields ごとの日本語 template (例: approval_state_changed/approved → 『承認 #25 (plugin <name>) を <decided_by> が承認しました。配備ジョブを開始します』)。name は event に無いので approvals.detail を引いて補う (bot は detail 権限を持つ)。(2) 既定で投稿する event 種別を絞る: approval_state_changed / guard_state_changed / service_health_changed は投稿、ops_request_terminal / job_state_changed は結果が失敗のときだけ、または承認投稿のスレッド返信に畳む。設定で全件に戻せる。(3) 終端時の edit 文面を『承認済み (<decided_by>、<時刻>) → 配備完了 / 失敗 <reason>』のように結果を残す形にする。(4) notify webhook は固定 schema のまま (backup 経路、変更しない)。文面は display_text の allowlist を通し、event から自由文を復元しない原則は維持する。

## 修正内容

## 経緯

- 2026-10-17: 起票。
- 2026-10-17: ユーザー裁定 — 改善通知は週 1 発火なので急がない (優先 中のまま)。plugin deploy ops (T2〜T8) の後に 2 件同じ束で設計 (C0 → sol → 承認)。
