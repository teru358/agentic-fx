---
id: afx-on-path-and-root-resolution
title: afx を PATH に置き、root を cwd 以外からも解決する
status: 設計待ち
priority: 中
opened: 2026-10-25
closed: null
related: [cli-surface-principle-and-reorg]
---
# [afx-on-path-and-root-resolution] afx を PATH に置き、root を cwd 以外からも解決する

**状態**: 設計待ち / **優先**: 中

## 現象

`afx` の console script は `.venv/bin/afx` にしか無く (uv sync は PATH に入れない)、導入手順にも PATH への配置が無いので `uv run afx` か venv 直叩きになる。さらに entry.py は root を `Path.cwd()` 固定で解決するため、PATH を通してもリポジトリ直下以外から打つと settings が無いと扱われる (2026-10-25 ユーザー指摘)。案: root の解決順を `AFX_ROOT` 環境変数 → `--root <dir>` → cwd から上方へ `config/settings.yaml` と `data/` を探索 → cwd とし、`afx init` または受入手順書で `uv tool install --editable <repo>` (~/.local/bin/afx) を案内する。daemon unit は WorkingDirectory で root を与えているので影響なし。ユーザー裁定 (2026-10-25): 優先 中、CLI 整理 I1 の merge 後の小束。設計は cli-surface spec に追補 1 節で足りる見込み。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
