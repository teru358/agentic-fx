---
id: verify-backend-counts-preexisting-descendants
title: verify_backend が実行前から居た子孫プロセスも未回収と数えて失敗する
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-06
closed: null
related: [improve-admission-selftest-flake-needs-restart]
---
# [verify-backend-counts-preexisting-descendants] verify_backend が実行前から居た子孫プロセスも未回収と数えて失敗する

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

tests/loops/test_verify_backend.py::test_verify_backend_opencode_bypasses_llama_swap_verified_gate_with_warning_log が tests/datafeed と同じ pytest process で回すと落ち、単独では通る (2026-11-05〜06、束の判定で 3 回)。tests/datafeed/test_news_collector_enable_race.py:111 の multiprocessing.get_context("spawn") が起こす resource tracker の子プロセスが pytest に残り、verify_backend (src/agentic_fx/loops/verify_backend.py:651,686) が実行後の os.getpid() の子孫を実行前と比べずに数えて ok=False にする。main でも再現。CLI の単独起動では起きないが、子プロセスを持つ process から呼ぶと誤って失敗する。処置案: 実行前の子孫を基準として差分だけを未回収と数える、test 側は spawn の resource tracker を残さない

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-06: 起票。
- 2026-11-07: 優先: 中、関連: [improve-admission-selftest-flake-needs-restart] — 改善 admission の自己試験が improve 要求のたびに再評価されうるようになり (daemon 稼働中で他の子孫がいる)、誤った not_reaped は再評価できない失敗に倒れる。/code-review の指摘 (tmp/review-adm/cr/rulings.md #4) を受けて優先度を中に
