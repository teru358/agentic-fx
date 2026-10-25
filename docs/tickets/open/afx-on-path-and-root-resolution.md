---
id: afx-on-path-and-root-resolution
title: afx を PATH に置き、root 解決と bash 補完を足す
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
- 2026-10-25: 題名: afx を PATH に置き、root 解決と bash 補完を足す — 2026-10-25 ユーザー裁定: 範囲に bash 補完を追加 (案 2 = 静的スクリプト生成)。`afx completion bash` が cli_surface.py の record (正式名・別名・`ops <feature> <verb>` の 2 段・ctl の語順表。planned / 非公開は除外) から補完スクリプトを出力し、利用者は `.bashrc` に `eval "$(afx completion bash)"`。依存を増やさない (argcomplete 不採用: ctl 以下が argparse を通らず正本が分かれるため)。zsh / fish は対象外。test: record を 1 つ変えると出力が変わる pin、help golden と候補集合の一致。PATH 導入 (`uv tool install --editable`) と `.bashrc` の案内を受入手順書に同じ節で書く
