---
id: bot-event-notices-are-raw-log-lines
title: bot の通知が event の k=v 行そのままで人が読めない
status: 是正済
priority: 中
opened: 2026-10-17
closed: 2026-10-23
related: [approval-notice-lacks-change-content, ops-bot-display-residuals]
---
# [bot-event-notices-are-raw-log-lines] bot の通知が event の k=v 行そのままで人が読めない

**状態**: 是正済 / **優先**: 中

## 現象

Discord への通知が『approval_state_changed: decision_code=approve, state=approved』『ops_request_terminal: authenticated_principal=ops-bot, endpoint_code=approve, result_code=ok』『job_state_changed: job_kind=approve, result_code=ok, state=done』のように event code と fields の k=v をそのまま 1 行ずつ投稿している。1 回の承認で 3 通届き、どれも人の言葉になっていない。承認が終わった投稿も『Approval is no longer active.』の 1 行だけで、何が承認され何が起動したかが残らない (2026-10-17 ユーザー報告)。

## 原因

afx-ops gate/render.py render_event が『allowlist 検証を通った code と fields を写す』だけで、ops-api spec §3.2 の『event 種別ごとの文面』が未実装。event 自体は設計どおり code・識別子・数のみ (ops/events.py _SAFE_VALUE、自由文を載せない) なので、人間向けの言い換えは bot 側の種別ごとの template で行うしかない。ops_request_terminal と job_state_changed は運用監査向けの event で、承認者には approval_state_changed 1 通で足りる。

## 処置案・裁定

設計事項 (C0 → レビュー → ユーザー承認、approval-notice-lacks-change-content と同じ束で): (1) event code × 主要 fields ごとの日本語 template (例: approval_state_changed/approved → 『承認 #25 (plugin <name>) を <decided_by> が承認しました。配備ジョブを開始します』)。name は event に無いので approvals.detail を引いて補う (bot は detail 権限を持つ)。(2) 既定で投稿する event 種別を絞る: approval_state_changed / guard_state_changed / service_health_changed は投稿、ops_request_terminal / job_state_changed は結果が失敗のときだけ、または承認投稿のスレッド返信に畳む。設定で全件に戻せる。(3) 終端時の edit 文面を『承認済み (<decided_by>、<時刻>) → 配備完了 / 失敗 <reason>』のように結果を残す形にする。(4) notify webhook は固定 schema のまま (backup 経路、変更しない)。文面は display_text の allowlist を通し、event から自由文を復元しない原則は維持する。

## 修正内容

- 2026-10-23: approval-content spec §6 / AC-17〜20 (T3) を afx-ops で実装 (merge 2026-10-23)。gate/render.py の OPS_EVENT_SPECS が code ごとの既定の可否と固定 template を 1 表で持ち、承認 (承認・配備 / 却下 / 失効)、job 失敗・取消・停止、kill switch、データ取得再開、健全性を日本語 1 行にする。k=v 行、decided_by、authenticated_principal は出さない。plugin 名は bot が承認詳細から補完 (終端 state のみ、3 秒期限、throttle 先送り中は省略、失敗は名前なしで投稿継続)。bot の ops.toml に ops_notices (default | all、既定 default = job 成功・操作要求終了・event stream health を出さない。skip した既知 event も cursor は進む) を追加、本体側の ops_setup writer も同 key を保持・検査。整数 field は 18 桁超で省略、本文は 2000 字以下に固定、mention 込みで超えれば mention を落とす。daemon の event schema、notify、bot の state schema は無変更。文書 docs/operations/ops-notices.md。レビュー: codex r1 4 件 + /code-review high 9 件を是正、terra 閉鎖確認で全閉鎖、段 0 81 本 (pin 34、容認 4)

## 経緯

- 2026-10-17: 起票。
- 2026-10-17: ユーザー裁定 — 改善通知は週 1 発火なので急がない (優先 中のまま)。plugin deploy ops (T2〜T8) の後に 2 件同じ束で設計 (C0 → sol → 承認)。
- 2026-10-17 22:49: ユーザー裁定 — 道筋 2 番目 (pdo 完走の直後)。Discord は要約、diff 全文は既存の読み取り専用 web に 1 ページ足す形で、2 チケット同じ束で C0。
- 2026-10-19: 状態: 実装待ち — 設計 docs/superpowers/specs/2026-10-19-approval-content-design.md v1.0 の T3 (event 通知、名前なし) として実装する。T1/T2 と独立
- 2026-10-23: 是正内容 — approval-content spec §6 / AC-17〜20 (T3) を afx-ops で実装 (merge 2026-10-23)。gate/render.py の OPS_EVENT_SPECS が code ごとの既定の可否と固定 template を 1 表で持ち、承認 (承認・配備 / 却下 / 失効)、job 失敗・取消・停止、kill switch、データ取得再開、健全性を日本語 1 行にする。k=v 行、decided_by、authenticated_principal は出さない。plugin 名は bot が承認詳細から補完 (終端 state のみ、3 秒期限、throttle 先送り中は省略、失敗は名前なしで投稿継続)。bot の ops.toml に ops_notices (default | all、既定 default = job 成功・操作要求終了・event stream health を出さない。skip した既知 event も cursor は進む) を追加、本体側の ops_setup writer も同 key を保持・検査。整数 field は 18 桁超で省略、本文は 2000 字以下に固定、mention 込みで超えれば mention を落とす。daemon の event schema、notify、bot の state schema は無変更。文書 docs/operations/ops-notices.md。レビュー: codex r1 4 件 + /code-review high 9 件を是正、terra 閉鎖確認で全閉鎖、段 0 81 本 (pin 34、容認 4)
