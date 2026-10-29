---
id: plugin-sandbox-allows-pure-conversions
title: pd.to_numeric が plugin 検査で禁止され、改善 mission が gate で繰り返し落ちる
status: 是正済
priority: 高
kind: 不具合
opened: 2026-10-28
closed: 2026-10-29
related: [improve-loop-quality]
---
# [plugin-sandbox-allows-pure-conversions] pd.to_numeric が plugin 検査で禁止され、改善 mission が gate で繰り返し落ちる

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

plugin の AST 検査 (src/agentic_fx/plugin/sandbox.py の _is_denied_bare_name) は `to_` 前綴りを既定で禁止し、純粋な型変換の 4 つ (to_dict / to_list / to_numpy / to_pydatetime) だけを許す。I/O を伴わない `pd.to_numeric` も巻き込まれ、改善 mission の候補が `gate_failed: … attribute to_numeric is not allowed` で落ちる (activity で 17 回: 10/27 4、10/28 13。10/28 の 24 回繰り返しでは 7 回)。改善 prompt にこの禁止が書かれておらず、agent は理由を学べず同じ書き方を繰り返す。案 (ユーザー承認 2026-10-28): (1) I/O を伴わない純粋な変換 (`to_numeric`、要検討で `to_datetime` / `to_timedelta`) を許可リストに足す。(2) 改善 prompt に `read_` / `to_` 前綴りの禁止と許可リストを明記する。sandbox の防御に触れるので、許可する各関数が I/O・任意コード実行・ファイル経路を持たないことを確かめ (引数に path や callable を取る形がないか)、境界コードのレビュー手順で通す。

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-29: sandbox の _TO_ALLOWED に to_numeric / to_datetime / to_timedelta を追加 (pandas 3.0.5 の実装で path / callable / pickle / I/O 経路が無いことを sol が確認)、公開 accessor allowed_to_conversions() / denied_names()、改善 prompt の規律 9 に read_ / to_ 前綴りと固定の禁止名と適用範囲 (変数名・引数名・関数定義名・from-import) を定数から載せる、実 worker で 3 関数を呼ぶ結合試験 (pyarrow 不在は plugin エラー、SIGSYS なし)。名前単位の許可は AST 検査の設計どおり (主防御は Landlock + seccomp) で、codex の alias 限定要求は却下。古い設計書 2 本の「4 つのみ」も訂正。

## 経緯

- 2026-10-28: 起票。
- 2026-10-29: 状態: 実装中 — 2026-10-29: worktree tmp/wt/sandbox-conv で codex terra 実装
- 2026-10-29: 是正内容 — sandbox の _TO_ALLOWED に to_numeric / to_datetime / to_timedelta を追加 (pandas 3.0.5 の実装で path / callable / pickle / I/O 経路が無いことを sol が確認)、公開 accessor allowed_to_conversions() / denied_names()、改善 prompt の規律 9 に read_ / to_ 前綴りと固定の禁止名と適用範囲 (変数名・引数名・関数定義名・from-import) を定数から載せる、実 worker で 3 関数を呼ぶ結合試験 (pyarrow 不在は plugin エラー、SIGSYS なし)。名前単位の許可は AST 検査の設計どおり (主防御は Landlock + seccomp) で、codex の alias 限定要求は却下。古い設計書 2 本の「4 つのみ」も訂正。
