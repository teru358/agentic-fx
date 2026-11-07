---
id: improve-admission-selftest-flake-needs-restart
title: 改善 admission の自己試験が 1 回落ちると再起動まで改善が止まる
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-01
closed: 2026-11-07
related: [graceful-stop-waits-mission, verify-backend-counts-preexisting-descendants]
---
# [improve-admission-selftest-flake-needs-restart] 改善 admission の自己試験が 1 回落ちると再起動まで改善が止まる

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

2026-11-01 17:53 の afx 再起動時、改善 isolation の admission 自己試験が gate=incomplete (mcp tools=13, call=0, steps=1: codex が tool を呼ばずに応答) で failed になり、admitted=no のまま固定。17:56 の手動 improve #2722 は runner_failed (sandbox_unavailable: runtime_fingerprint_selftest_failed) で 0 秒終了。fingerprint は同日の admitted=yes の 3 回と同一で、LLM 側の一時的な振る舞い (tool 呼出なし) が原因。18:0x の再起動で admitted=yes に戻った。admission は起動時 1 回だけで、失敗理由が自己試験の不完走 (fingerprint 不一致ではない) でも再判定の経路が無い。処置案: 失敗理由が selftest 不完走のときは改善 request 時 (または一定間隔) に自己試験を再実行し、成功すれば admitted に切り替える。fingerprint 不一致は従来どおり固定。回数上限と activity 固定語

## 原因

## 処置案・裁定

2026-11-07 利用者承認 (設計 v0.2、tmp/review-adm/design-v0.1.md、sol と Fable の前提確認を反映)。
- 前提の訂正: 再判定の経路は既にある (retryable な判定は 60 秒後の次の admit で再評価、improve_admission.py:234-237)。問題は call=0 の不完走が permanent に分類されること。
- 分類: verify_backend 側の固定語彙の helper で「gate=incomplete・status=completed・echo 到達・MCP initialized・tools 列挙非空・calls==0・grace_expired でない」(隔離に依存する gate は全部通り、LLM が tool を 1 回呼ばなかっただけ) を retryable に。他の incomplete・nonce 不一致・grace_expired は permanent のまま。合格まで admitted=no (fail-closed)。
- 再評価は既存の lazy 経路 (次の improve 要求 / 週次、60 秒間隔)。費用は demand 駆動・60 秒 cache・ctl improve 繰り返しの連続失敗停止で有界なので回数上限は足さない。再評価中の prepare は slot と mission(running) を最長 約 11.5 分保持する (既存の timeout retryable と同じ)。
- no → yes の遷移を admit の cache 書込と同じ lock 区間で検出し、起動行と同じ書式 (共通関数) の admitted 行を technical log に出す。
- 対象外: 自己試験への停止の伝播 (graceful-stop-waits-mission へ)、plugin 側 runtime_fingerprint の admission、合格の永続化 (登録経路を持たない方針)、自己試験内の即時再試行・prompt / max_turns の変更。
- test の材料は 11-07 の失敗の実 transcript (agent_message だけで tool 呼出し無し)。

## 修正内容

main a69c0857 (branch adm-retry: 680ebd43 → r1 是正 8ea5464f → /code-review 是正 323f7f15)。
- `VerifyBackendResult.tool_not_called` (verify_backend.py): gate=incomplete・status=completed・CLI 子の観測と回収・echo 監視側の `tool_not_called()` (echo 到達・MCP initialized・tools 列挙が非空 list・calls が int の 0・grace_expired でない、lock の下)。
- 自己試験 (improve_canary.py): 上が真で cgroup gate (`host.mission_allowed()`) も通るときだけ transient (= 60 秒後の次の admit で再評価)。
- admission (improve_admission.py): backend ごとの直前の判定 (早期拒否・cache 命中も含む) と比べ、no → yes の遷移を起動行と同じ書式で 1 回 technical log に出す。不合格の行は理由を必須に。
- 実 transcript fixture `tests/fixtures/selftest_call0/`。レビュー: sol r1 (3 件全採用) → /code-review high (6 件、採用 4 / 却下 2) → sol 閉鎖確認 (全閉)。
- 2026-11-07 17:54 JST に配備 (afx 再起動、新 fingerprint f795bc60… は canary + 自己試験に合格し admitted=yes、登録を更新)。
- 2026-11-07: main a69c0857 を 11-07 に配備、tool 未呼出しの自己試験失敗は再評価される

## 経緯

- 2026-11-01: 起票。
- 2026-11-07: 再発 (3 回目)。07:05Z の再起動で同じ gate=incomplete (call=0) → admitted=no、16:27 / 16:33 JST の improve #3254・#3255 が sandbox_unavailable で 0 秒終了。fingerprint 15bd94e5… (codex 0.166.0) が未登録で毎起動に自己試験が走っていた → 登録を更新 (起動時の自己試験を減らす当面の策、本件の是正ではない)
- 2026-11-07: 優先: 高 — 利用者が優先タスクに指定 (11-07 の 3 回目の再発を受けて)。設計に着手
- 2026-11-07: 状態: 実装中、関連: [graceful-stop-waits-mission] — 設計 v0.2 を利用者が承認、実装着手
- 2026-11-07: 関連: [graceful-stop-waits-mission, verify-backend-counts-preexisting-descendants] — /code-review の指摘 #4 (他の子孫で reaped が偽) は verify-backend-counts-preexisting-descendants と同根として関連付け
- 2026-11-07: 是正内容 — main a69c0857 を 11-07 に配備、tool 未呼出しの自己試験失敗は再評価される
