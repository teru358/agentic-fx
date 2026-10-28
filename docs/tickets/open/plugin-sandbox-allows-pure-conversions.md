---
id: plugin-sandbox-allows-pure-conversions
title: pd.to_numeric が plugin 検査で禁止され、改善 mission が gate で繰り返し落ちる
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-10-28
closed: null
related: [improve-loop-quality]
---
# [plugin-sandbox-allows-pure-conversions] pd.to_numeric が plugin 検査で禁止され、改善 mission が gate で繰り返し落ちる

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

plugin の AST 検査 (src/agentic_fx/plugin/sandbox.py の _is_denied_bare_name) は `to_` 前綴りを既定で禁止し、純粋な型変換の 4 つ (to_dict / to_list / to_numpy / to_pydatetime) だけを許す。I/O を伴わない `pd.to_numeric` も巻き込まれ、改善 mission の候補が `gate_failed: … attribute to_numeric is not allowed` で落ちる (activity で 17 回: 10/27 4、10/28 13。10/28 の 24 回繰り返しでは 7 回)。改善 prompt にこの禁止が書かれておらず、agent は理由を学べず同じ書き方を繰り返す。案 (ユーザー承認 2026-10-28): (1) I/O を伴わない純粋な変換 (`to_numeric`、要検討で `to_datetime` / `to_timedelta`) を許可リストに足す。(2) 改善 prompt に `read_` / `to_` 前綴りの禁止と許可リストを明記する。sandbox の防御に触れるので、許可する各関数が I/O・任意コード実行・ファイル経路を持たないことを確かめ (引数に path や callable を取る形がないか)、境界コードのレビュー手順で通す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-28: 起票。
