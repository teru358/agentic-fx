---
id: wizard-stage13-linger-prompt-duplicate
title: 段 13 の linger の案内が問いの直後に同文で繰り返される
status: 是正済
priority: 低
opened: 2026-10-15
closed: 2026-10-16
related: [first-run-setup]
---
# [wizard-stage13-linger-prompt-duplicate] 段 13 の linger の案内が問いの直後に同文で繰り返される

**状態**: 是正済 / **優先**: 低

## 現象

段 13 で「ログアウトすると afx も止まります。loginctl enable-linger $USER を実行しますか [y/N]」に n と答えた直後、「ログアウトすると afx も止まります。止めたくなければ … 実行してください。」が続けて出る (同じ文の繰り返し)。n のときは 1 行目を出さず、段 14 の未完手順に 1 回だけ出す。2026-10-15 stream-svr 受入 2 回目

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-16: linger の問いに n のとき同文を繰り返さない (未完手順に 1 回)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)

## 経緯

- 2026-10-15: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)
- 2026-10-16: 是正内容 — linger の問いに n のとき同文を繰り返さない (未完手順に 1 回)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)
