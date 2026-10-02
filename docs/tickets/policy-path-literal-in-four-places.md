---
id: policy-path-literal-in-four-places
title: directives.md のパスが 4 箇所に散在する
status: 実装待ち
priority: 低
opened: 2026-09-21
closed: null
related: [ops-first-contact-fixes]
backfilled: true
source_section: 完了ログ
---
# [policy-path-literal-in-four-places]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[policy-path-literal-in-four-places] (Minor、2026-09-21 [ops-first-contact-fixes] 1 周目 codex): `root / "policy" / "directives.md"` が `service.py` 3 箇所 (trade/improve の注入 + `Commands(policy_path=)`) と `loops/improve_context.py` の計 4 箇所で独立に組み立てられている。現状は全部一致 (`policy add` の追記先 = Mission 注入元)。1 箇所だけ変えると追記先と注入元がずれる。共有定数か `Policy` インスタンスの受け渡しに。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: directives.md のパスが 4 箇所に散在する — 仕分け (2026-10-02、現物で成立を確認): 現状は全て一致、共有定数か Policy 受け渡しに寄せる (整理)。 根拠: service.py:1046,1306,1629 と loops/improve_context.py:138 で policy/directives.md を独立に組み立て。
