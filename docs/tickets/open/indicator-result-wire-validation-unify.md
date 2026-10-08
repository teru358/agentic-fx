---
id: indicator-result-wire-validation-unify
title: 指標結果の検査が 2 箇所に重複している
status: 実装待ち
priority: 低
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [indicator-result-wire-validation-unify]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[indicator-result-wire-validation-unify] 起票 2026-09-18 (iw 2 周目 CR8 見送り): `plugin/sandbox._validate_indicator_result` の per-item bool/number/finite 検査が `core/plugin_contract._check_number` の手書きコピーで、scalar `None`↔NaN の bridging (`nan_keys`/`finally`) も要る。共通 validator に wire 意味論 (scalar null = NaN) を足して 1 箇所にできる。現行は probe で健全 (`finally` は例外経路でも復元)。`tmp/review-20260918-iw-r2/triage-r2.md` §8 T-1

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 指標結果の検査が 2 箇所に重複している — 仕分け (2026-10-02、現物で成立を確認): 現行は健全、共通 validator に統一できる (整理)。 根拠: plugin/sandbox.py:1082 _validate_indicator_result と core/plugin_contract.py:35 _check_number に検査が重複。
