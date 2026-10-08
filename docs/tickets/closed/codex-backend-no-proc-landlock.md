---
id: codex-backend-no-proc-landlock
status: 是正済
priority: 未設定
opened: 2026-09-11
closed: 2026-09-11
related: [measure-capability-not-startup]
backfilled: true
source_section: 未完了
---
# [codex-backend-no-proc-landlock]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[codex-backend-no-proc-landlock] (Critical、A4 10 回目 codex #71 観測 A、2026-09-11、裁定待ち)** `mission_worker.py:161` の Landlock read_only allowlist に `/proc` を足すのは `backend in ("claude","opencode")` だけで codex が無い。codex CLI 0.150.1 は MCP ツールを `codex-code-mode-host` (V8/Node 同梱) 経由で呼び、これが `/proc/self/maps` を読めず SIGTRAP で 6 回即死 (coredumpctl + transcript `_stderr_tail` の 2 系統で一致、`tmp/a4-run10-codex/proc_access.py` で allowlist 再現)。codex 本体は生き残るので mission は 44 秒で `observation` 終端 = **codex backend は改善ループで 1 つもツールを呼べない**のに `*_failed` がどこにも出ない。是正案 (a) 条件に `"codex"` を足す (R10 のリスク受容を 3 本目に延ばす — **セキュリティ境界の判断なのでユーザー裁定**) / (b) `codex_runner` に `--disable code_mode` (MCP の露出形が未実測、実ターン 1 回要)。verify_backend は「起動できる」までしか見ていない ([[measure-capability-not-startup]])。報告 `tmp/a4-run10-codex-20260911.md` §7 **→ 是正済 `838b09d` で /proc を codex にも付与 (ユーザー承認 2026-09-11)、実機未確認** **A4 11 回目 codex #73 で `/proc` 付与後も SIGTRAP 6 件が再現 = 推定原因は反証**。仮説 (a) `--disable code_mode` / (b) RLIMIT_AS 4096MB (V8 cage)。切り分け中 (`tmp/codex-host-probe/findings.md`) **→ 真因確定 2026-09-12: RLIMIT_AS 4096MB (V8 sandbox 予約失敗 → int3)。是正 = `as_mb` 引き上げを codex にも (opencode と同じ 256GB)。`/proc` は引き金でない (戻すか裁定)** **→ 是正 `b8004d9` (as_mb 256GB を codex にも、/proc 付与は戻す)、A4 12 回目 codex #74 で実機 OK (tool 10 件、SIGTRAP 0、approval #7、RLIMIT_AS 256GB が 4 段継承) → クローズ**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

