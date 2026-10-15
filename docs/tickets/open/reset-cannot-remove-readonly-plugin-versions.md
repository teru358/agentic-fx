---
id: reset-cannot-remove-readonly-plugin-versions
title: リセットが読み取り専用の plugin 版 dir を消せず repo_plugins が失敗
status: 設計待ち
priority: 高
opened: 2026-10-15
closed: null
related: [first-run-setup]
---
# [reset-cannot-remove-readonly-plugin-versions] リセットが読み取り専用の plugin 版 dir を消せず repo_plugins が失敗

**状態**: 設計待ち / **優先**: 高

## 現象

plugin/version_store は採用した版を dir 0o500 / file 0o400 の不変スナップショットとして plugins/.versions/<name>/<hash>/ に置く。scripts/first-run-reset.sh の rm -rf は dir に書込権限が無いため「許可がありません」で失敗し、repo_plugins が失敗 → 規則どおり repo_data も残して rc=1。標準指標を採用した環境 (主シナリオ) では AC-36 (clean clone に戻る) が必ず不成立 (2026-10-15 stream-svr 受入 2 回目、指標 9 本採用後のリセットで実測。unit / ops / settings / logs は消えた)。是正: (1) repo 内の削除は計画配列の path ごとに `chmod -R u+rwX -- <path>` (symlink は辿らない。version_store.chmod_tree_writable と同じ意味) を rm -rf の前に行う、(2) test: fake でなく実際に 0o500 / 0o400 の木を作って消えることを pin、(3) `git status --porcelain --ignored` の比較で `**/__pycache__/` と `tmp/` が差として残る (afx 稼働で生成) → AC-36a の許可リストに加えるか reset の固定表で消すかを決めて手順書 (6.9) と test の allowed を揃える

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-15: 起票。
