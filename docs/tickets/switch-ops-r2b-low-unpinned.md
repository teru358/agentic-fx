---
id: switch-ops-r2b-low-unpinned
status: 実装待ち
priority: 未設定
opened: 2026-09-20
closed: null
related: [switch-ops-hardening]
backfilled: true
source_section: 未完了
---
# [switch-ops-r2b-low-unpinned]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[switch-ops-r2b-low-unpinned] 起票 2026-09-20 ([switch-ops-hardening] 2 周目やり直しローカル LLM、束の diff 外の既存コード、変異は未実測 = 要 probe、手順 `tmp/review-20260920-soh/r2b/local/triage.md` §9): `_version_dir_hashes_ok` の `and version_dir.name == artifact_hash` を落とす変異 (是正案 = `.versions/<name>/<hash>` を別 dir への symlink に差し替える 1 本) / 同関数の `except OSError: return False` → `True` (`is_dir()` が先に弾くので except を踏むテストが無い、版 dir を `chmod(0o000)`) / `retire_plugin` の `not live.is_dir()` を落とす変異 (live 不在で retire を打つ 1 本)。あわせて Minor: `test_ac16c6_plain_branch_closes_its_own_unfinished_journal` の関数名だけ v1.7 の名残

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

