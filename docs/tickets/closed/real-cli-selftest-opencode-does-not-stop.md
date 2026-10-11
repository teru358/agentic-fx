---
id: real-cli-selftest-opencode-does-not-stop
title: 実 CLI selftest の opencode が echo の後に止まらず tool を呼び続ける
status: 是正済
priority: 低
opened: 2026-10-11
closed: 2026-10-11
related: []
---
# [real-cli-selftest-opencode-does-not-stop] 実 CLI selftest の opencode が echo の後に止まらず tool を呼び続ける

**状態**: 是正済 / **優先**: 低

## 現象

seccomp T6 の実 CLI selftest (-m realbackend) で、opencode は最初の step で期待の {"echo": ...} を返した後も止まらず afx_web_search / afx_fetch_article を呼び続け、mission の上限まで走る (prompt は「tool を呼ぶな」だが MCP 経由で afx_* tool が見える)。判定基準 (echo の後に止まること、tool が見えること) を見直すか、selftest 用に tool を隠す。合わせて失敗理由が cleanup_deadline_received に化けて CLI の実際の結果 (schema 不一致) が見えない観測性の問題も扱う。

## 原因

自己試験は CLI が自分で終わるまで待つ作りで、opencode は最初の step で echo を返した後も MCP 経由で見える afx_* tool を呼び続けた。mission_timeout (既定 3600) が unit の RuntimeMaxSec (900) より長く、unit に切られて失敗理由が cleanup_deadline_received に化けた。

## 処置案・裁定

指揮者裁定: tool は隠さない (MCP 接続が自己試験の対象のため)。最初の応答で echo が一致した時点を成功とし、selftest 側が通常の停止経路で mission を終える。mission_timeout は 600 に固定。

## 修正内容

- `verify_backend(stop_on_echo=True)`: WorkerRunner に追加した `on_event` で CLI の stdout の event を観測し、全体が `{"echo": nonce}` の文字列が最初に現れた時点で `stop_event` を立てる。mission は既存の停止経路 (cleanup 系列) で終わり、子孫の観測と回収の gate は従来どおり。echo の後の tool 呼び出しは成功を覆さない。3 backend 共通
- 失敗の detail に CLI の結果 (status / reason) に加えて `echo_reached=no steps=N events=M` を残す
- `run_selftest` は mission_timeout を 600 秒以下に固定 (`SELFTEST_MISSION_TIMEOUT_SEC`)
- 実機 (`-m realbackend`): opencode 5.98 s / claude 4.29 s / codex 6.34 s で ok
- 2026-10-11: verify_backend の stop_on_echo で最初の echo 一致を成功として停止経路で終える。mission_timeout は 600 に固定
- 成功条件を「assistant 本文の echo 一致 かつ MCP の tools/list が 1 件以上の tool を返した」に強めた。echo は backend ごとの assistant 本文の経路だけを見る (opencode の text part、claude の assistant text block と最終 result、codex の agent_message)。tool 結果・thinking・recovery marker (stderr 末尾) の nonce は数えない。code fence は完全な 1 つだけ許す
- MCP の観測: `McpShimDispatcher.on_observe` が成功した initialize / tools/list (返した名前) を通知し、worker が 1 重の包みの system event (`_mcp_observed`) で親へ送る。CLI の stdout の event は 2 重の包みなので、CLI が同じ文字列を出しても観測にならない。echo と tools/list の両方が揃った時点で停止し、揃わなければ mission_timeout 600 まで待って失敗。失敗の detail に `mcp=none|init_only|tools=N` を残す
- 実機 (`-m realbackend`): 3 backend とも ok、tools/list は 13 件 (server 側の名前は `afx_` 無し。`afx_` は CLI 側の呼び名)
- `WorkerRunner.on_event` が transcript の切り詰めより前に全 event で呼ばれることを pin

## 経緯

- 2026-10-11: 起票。
- 2026-10-11: 修正。実機 3 backend で ok。
- 2026-10-11: 是正内容 — verify_backend の stop_on_echo で最初の echo 一致を成功として停止経路で終える。mission_timeout は 600 に固定
