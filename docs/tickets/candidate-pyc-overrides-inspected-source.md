---
id: candidate-pyc-overrides-inspected-source
title: 候補の偽 pyc が検査済み plugin.py の代わりに実行される
status: 実装待ち
priority: 高
opened: 2026-10-03
closed: null
related: []
---
# [candidate-pyc-overrides-inspected-source] 候補の偽 pyc が検査済み plugin.py の代わりに実行される

**状態**: 実装待ち / **優先**: 高

## 現象

候補 plugin のディレクトリに偽の __pycache__/*.pyc を置くと、hash と AST 検査 (check_source) を通った plugin.py ではなく pyc が import される (2026-10-03 実測、Landlock の有無に関係なし)。候補の backtest 成績が、承認時に人が読む source とは別のコードから作られ得る。live でも同様。

## 原因

## 処置案・裁定

worker が plugin.py を自分で読み、hash を照合してから compile() して実行する (pyc を使わない)。gate と同じ PYTHONPYCACHEPREFIX は site-packages まで再コンパイルするので不採用。plugin worker Landlock の束に含める (2026-10-03 裁定、tmp/design-plugin-landlock/C0.md)。

## 修正内容

## 経緯

- 2026-10-03: 起票。
- 2026-10-04: 状態: 実装待ち — 2026-10-04: 設計 spec v1.0 (docs/superpowers/specs/2026-10-04-plugin-worker-sandbox-design.md、設計レビュー 8 周 + 受入周) をユーザー承認。実装は T1 ∥ T2 → T3 → T4 → T5。
