---
id: tests-leak-tmp-dirs-outside-basetemp
title: 改善隔離 test helper が /tmp に afx-root-* を残す (basetemp 外)
status: 設計待ち
priority: 中
opened: 2026-10-17
closed: null
related: [backtest-tmp-state-leaks-and-tmp-quota]
kind: 不具合
---
# [tests-leak-tmp-dirs-outside-basetemp] 改善隔離 test helper が /tmp に afx-root-* を残す (basetemp 外)

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

/tmp に afx-root-* 1841 個、afx-transcripts-* 382 個 (2026-10-13〜17 の走行分、計 12MB) が残っている。tests/core/_improve_isolation_service_helper.py:358 / 362 / 579 が tempfile.mkdtemp(prefix=...) で /tmp 直下に作り、削除していない。容量は小さいが /tmp のエントリ数を押し上げ、tmpfs の掃除で見分けにくい。

## 原因

helper が pytest の tmp_path / basetemp を使わず tempfile.mkdtemp を直接使い、teardown で rmtree していない。実隔離 (Landlock) の都合で /tmp 直下が要るなら、その理由をコメントに残しつつ finally で消すか、basetemp 配下の path を渡す形にする。

## 処置案・裁定

helper の 3 か所を tmp_path_factory 由来の path に置き換える (隔離の allowlist に /tmp を入れない規律はメモリ pytest-under-landlock-pitfalls を確認)。不可能なら try/finally で rmtree。残骸 /tmp/afx-root-* / afx-transcripts-* の削除はユーザー操作 (rm 規律)。

## 修正内容

## 経緯

- 2026-10-17: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-10-29: 優先: 中、関連: [backtest-tmp-state-leaks-and-tmp-quota] — 2026-10-29 棚卸し: /tmp に afx-root-* 1841 個。/tmp 枯渇で本番 mission が落ちた前歴 (10/25)。引き上げ提案
