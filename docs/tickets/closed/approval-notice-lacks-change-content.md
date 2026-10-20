---
id: approval-notice-lacks-change-content
title: 改善承認の通知に変更内容が無い (何がどう変わるか読めない)
status: 是正済
priority: 中
opened: 2026-10-17
closed: 2026-10-20
related: [ops-ui, bot-readonly-slash-commands]
---
# [approval-notice-lacks-change-content] 改善承認の通知に変更内容が無い (何がどう変わるか読めない)

**状態**: 是正済 / **優先**: 中

## 現象

Discord の承認投稿 (afx-ops gate/render.py render_approval) は approval id / kind / facts / claims=present|absent / digest / expires / decision / claimed の 8 行だけで、plugin 名も、新規か既存版の更新かも、コードや config.yaml の何が変わるかも出ない。agent_claims (summary / selection_rationale) は本文があるのに present の 1 語に潰れている。承認者は変化点を知らずに Approve / Reject を押すことになる (2026-10-17 ユーザー報告)。

## 原因

ops-api spec §3.2 は『自由文を event から復元せず API が許す承認詳細だけを出す』を設計方針にしており、approvals.detail の返却 (ops/service.py approval_detail) は parent_facts / agent_claims / facts_lines / in_sample / holdout / payload_sha256 に限られる。変更内容 (版 git の前版との diff、config.yaml の params、新規 / 更新の区別) は API の投影に無く、bot も web も出せない。

## 処置案・裁定

設計事項 (C0 → レビュー → ユーザー承認): (1) approvals.detail に『変更要約』の構造化投影を足す: plugin 名、kind (indicator / strategy)、新規 / 更新 (更新なら前版 artifact_hash と history_git の diff 統計 = 変更ファイルと行数)、config.yaml の params の差分 (鍵と値、display_text で bound)、agent_claims の summary / selection_rationale の本文 (上限付き)、親の metrics と前版の metrics の並記。(2) 全文 diff は Discord の 2000 字に収まらないので web (loopback read-only、/approvals/<id>/diff) と CLI (afx ctl approval show <id> --diff) に置き、Discord には要約 + 参照だけ。(3) render_approval の文面を人間向けに書き直す (k=v 行を日本語の見出し付きに、bot-readonly-slash-commands の文面方針と揃える)。注意: reject reason の漏洩規律 (human-reject-reason-leaks-to-improve-prompt) と payload_sha256 の digest 契約 (payload は変えずに detail の投影だけ足す) を保つ。

## 修正内容

- 2026-10-20: spec 2026-10-19-approval-content-design.md v1.6 の T1 (API + CLI + web、merge 85d4776) と T2 (Discord 承認投稿) で是正。承認投稿に plugin 名・新規/更新・params 差分・成績 (in-sample)・agent の申告 (未検証、300 字)・期限と web への案内が載り、web の承認ページで 3 ファイルの diff を読める。T3 (event 通知) は bot-event-notices-are-raw-log-lines で別途

## 経緯

- 2026-10-17: 起票。
- 2026-10-17: ユーザー裁定 — 改善通知は週 1 発火なので急がない (優先 中のまま)。plugin deploy ops (T2〜T8) の後に 2 件同じ束で設計 (C0 → sol → 承認)。
- 2026-10-17 22:49: ユーザー裁定 — 道筋 2 番目 (pdo 完走の直後)。Discord は要約、diff 全文は既存の読み取り専用 web に 1 ページ足す形で、2 チケット同じ束で C0。
- 2026-10-19: 状態: 実装中 — 設計 docs/superpowers/specs/2026-10-19-approval-content-design.md v1.0 をユーザー承認 (2026-10-19)。T1 (API + CLI + web) 着手
- 2026-10-20: 是正内容 — spec 2026-10-19-approval-content-design.md v1.6 の T1 (API + CLI + web、merge 85d4776) と T2 (Discord 承認投稿) で是正。承認投稿に plugin 名・新規/更新・params 差分・成績 (in-sample)・agent の申告 (未検証、300 字)・期限と web への案内が載り、web の承認ページで 3 ファイルの diff を読める。T3 (event 通知) は bot-event-notices-are-raw-log-lines で別途
