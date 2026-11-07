---
id: wizard-codex-model-list-fetch-failed
title: afx init: codex のモデル一覧取得に失敗する
status: 実装待ち
priority: 低
kind: 不具合
opened: 2026-10-31
closed: null
related: []
---
# [wizard-codex-model-list-fetch-failed] afx init: codex のモデル一覧取得に失敗する

**状態**: 実装待ち / **優先**: 低 / **種別**: 不具合

## 現象

afx init 再実行 (2026-10-31) の「改善の backend」段で「codex: 導入とログインを確認しました」の直後に「codex のモデル一覧を取得できませんでした (…)」が出る。選択自体は続けられる。2026-10-27 に codex のモデル名が 6.x 系 (gpt-6.1-sol / gpt-6.1-terra / gpt-6-luna) に変わっており、一覧取得の方法か既定の候補表が追随していない可能性。ウィザード調整 (道筋の最後の束) で扱う。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-31: 起票。
- 2026-11-07: 状態: 実装待ち — 原因を特定 (2026-11-07): setup/checks.py:list_codex_models は subprocess.run(input=...) で handshake と model/list を書いた直後に stdin を閉じる。codex 0.166 (2026-10-30 導入) の app-server は stdin の EOF で終わり、非同期の model/list の応答 (id 2) を出す前に終了する (rc 0、stdout は id 1 の応答だけ)。stdin を開いたまま待つと model/list は応答し一覧が返る (initialized 通知は不要)。init の改善段と、status の「improve の LLM は未確認 (model_list_unavailable)」(init 時に保存した記録) は同じ原因。処置案: Popen で stdin を開いたまま id 2 か timeout まで読み、process を止める。直した後に afx init の改善段を通し直すと status の表示が消える。モデル名の 6.x 系への変更は原因ではない
