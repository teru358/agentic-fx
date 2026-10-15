---
id: wizard-check-improve-landlock-abi-upfront
title: 段 0 で改善 worker の Landlock ABI 8 要件を判定し、満たさない kernel では先に言う
status: 是正済
priority: 高
opened: 2026-10-15
closed: 2026-10-15
related: [first-run-setup, wizard-write-absolute-cli-bin]
---
# [wizard-check-improve-landlock-abi-upfront] 段 0 で改善 worker の Landlock ABI 8 要件を判定し、満たさない kernel では先に言う

**状態**: 是正済 / **優先**: 高

## 現象

段 0 の Landlock 判定は core.landlock.is_available (ABI ≥ 3、plugin worker の水準) だけで、改善 worker の隔離が要求する IMPROVE_MIN_ABI = 8 (scope ABI 6、TSYNC ABI 8。fallback は作らない裁定) を見ない。Landlock ABI 4 の kernel (Ubuntu 22.04 HWE 6.8、stream-svr) では段 0〜9 が通り、段 10 の verify-backend で「Landlock restriction failed … landlock_abi_too_old: kernel ABI 4 < 8」と失敗、起動後も improve が admission で止まる。利用者は段 0 で知るべき。是正: (1) 段 0 に改善隔離の ABI 判定を足し、満たさなければ「この kernel (ABI N) では改善ループの隔離が使えません (ABI 8 以上が必要)。標準指標の導入と取引判断は設定できます。改善は kernel を更新するまで止まります」と表示して続行可 (記録に landlock_abi_too_old)、段 4 の改善 backend は「後で」を既定にし verify-backend の問いを出さない、段 7 は説明だけ、(2) 手順書 §1 前提に kernel 要件 (Landlock ABI 8 以上。確認コマンドと ABI → kernel 版の対応) を書く、(3) README の前提にも kernel 版を 1 行。ABI 8 を提供する kernel 版は実測で確定して文書に書く (本機 7.0 は可、6.8 は不可)。2026-10-15 stream-svr 受入 2 回目で発見

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-15: 段 0 で landlock_abi() を MIN_LANDLOCK_ABI (8) と比べ、足りなければ表示して続行 (記録 landlock に reason landlock_abi_too_old)。未完手順 improve_isolation_unavailable は現在の ABI で判定し status API にも載る (ops-api v1.18 に列挙)。段 4 は claude/codex の実往復の問いを省略文に、段 7 に 1 行。README と手順書に kernel 要件 (Linux 6.18 以降 = ABI 8、確認コマンドと -1 の読み方)。spec v1.7

## 経緯

- 2026-10-15: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix1 (2026-10-15 ユーザー承認の設計: ブリーフ tmp/review-20261011-sc/fr-fix1-ab.md / fr-fix1-c.md)
- 2026-10-15: 是正内容 — 段 0 で landlock_abi() を MIN_LANDLOCK_ABI (8) と比べ、足りなければ表示して続行 (記録 landlock に reason landlock_abi_too_old)。未完手順 improve_isolation_unavailable は現在の ABI で判定し status API にも載る (ops-api v1.18 に列挙)。段 4 は claude/codex の実往復の問いを省略文に、段 7 に 1 行。README と手順書に kernel 要件 (Linux 6.18 以降 = ABI 8、確認コマンドと -1 の読み方)。spec v1.7
