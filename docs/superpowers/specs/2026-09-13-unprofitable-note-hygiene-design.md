# [unprofitable-note-hygiene] 設計書 v1.1

束: 収益性フロア不合格 (`unprofitable`) で終わった改善 mission が起票した backlog 行 (note / task) に機械注記を付け、次 mission の同型再提出を抑止する。ユーザー承認 2026-09-13。親: [floor-path-skips-duplicate-metrics] 裁定 (c) 現状維持、遮断 8 例外の再評価 (`2026-09-12-profitability-floor-design.md` v1.5)。

## 1. 問題

A4 run19 (`tmp/a4-run19-codex-20260913.md` 観測 D): mission #83 が holdout 段フロアで `unprofitable` に落ちた直後、#84 が #83 とビット一致の候補を再提出して同じフロアに落ちた。原因は情報漏洩ではなく、#83 が起票した note #74「fast=10/slow=30 は提出条件を満たした」(agent 自筆、in_sample 語彙) が backlog #69 の `unprofitable` (機械由来 1 bit) より強く agent の判断に効いたこと。

事実 (`src/agentic_fx/loops/improve_loop.py`):
- agent の discoveries (note / task) は選択時 `_select_and_bind` (`:1219`) で INSERT される = **gate より前**。起票時点では unprofitable かどうか分からない。
- prompt の backlog 表 / note 表 (`_backlog_table` `:649`) は `id / idea / status / attempts / assigned` のみで `last_result` を表示しない。
- task 行 (#73) も再現の種になる。`idea_norm` の重複検出は文言違いで効かない。

## 2. 設計

1. **起票行の追跡**: `_SelectionOutcome` (`:186`) に `inserted_ids: tuple[int, ...]` を追加。`_select_and_bind` が INSERT した行 id (note / task とも、`_upsert_backlog_idea` が `"inserted"` を返したもの) を格納。既存行の再利用 (`existing` / `promoted`) は含めない。
2. **機械注記**: `commit()` は `selection.inserted_ids` を `_finalize_gate_failed(..., inserted_ids=...)` に渡す。`_finalize_gate_failed` は `mission_outcome == "unprofitable"` のときだけ、既存の `BEGIN IMMEDIATE` tx 内で `UPDATE improvement_backlog SET last_result='origin:unprofitable', updated_at=? WHERE id IN (...)` を実行する。`idea` / `idea_norm` / `status` は触らない。他の終端 (`gate_failed` 系 / report / observation / approval / `report_failed` 分岐) では書かない。当該行が後に選択されて終端すれば `last_result` は既存規律で上書きされる。
3. **表示**: `_backlog_table` に `origin` 列を追加 (items 表・notes 表の両方)。`row["last_result"] == "origin:unprofitable"` のときだけ `unprofitable`、それ以外は空文字。`improve_context.build_improve_context` は既に `last_result` を items / notes に載せている (`improve_context.py:102,109`) ので配線変更なし。
4. **規律文**: `src/agentic_fx/loops/prompts/improve_mission.md` の「規律 (必ず守ること)」に 1 項追加 (逐語):
   > 5. **`origin` 列が `unprofitable` の課題・note は、その mission の候補が収益性フロアで落ちたときに書かれたものです。** 同じ指標・同じパラメータの候補を再提出しないでください。試すなら明確にパラメータを変え、その理由を `selection_rationale` に書いてください。
5. **不変**: 遮断 8 の語彙 (`origin:unprofitable` は改善ループ内部の `last_result` 面であり、holdout の数値・段名・pair を含まない)、質検査の母集団 (F4-8)、DB スキーマ、`idea_norm` 規律、`max_new_backlog_per_mission`。
6. **遡及しない**: 実 DB の過去行 (#70/#71/#73/#74) は更新しない。#73 (open) は人間が `afx> backlog reject 73` で落とす (運用操作)。

## 3. 変更面

| 面 | 変更 |
|---|---|
| `loops/improve_loop.py` | `_SelectionOutcome.inserted_ids` / `_select_and_bind` の収集 / `commit()` の受け渡し / `_finalize_gate_failed(inserted_ids=())` の UPDATE / `_backlog_table` の `origin` 列 |
| `loops/prompts/improve_mission.md` | 規律 5 |
| tests | §4 |

## 4. 受入 (pin)

| # | 基準 | 観測点 |
|---|---|---|
| N1 | フロア不合格 (in_sample 段 / holdout 段の両方) で終わった mission が INSERT した note と task の `last_result` が `origin:unprofitable` (バイト一致) | 実 DB 相当の sqlite fixture |
| N2 | `gate_failed:*` / `insufficient_trades` / report / observation / approval / `report_failed` 分岐では `last_result` が NULL のまま (逆変異: 条件を外すと red) | 同上 |
| N3 | 既存行の再利用 (`existing` / `promoted`) は注記されない | 同上 |
| N4 | 同 tx: 正常分岐 tx で注記を書いた後に後段 DB 処理 (`finish_improve_mission`) が失敗すると注記も rollback で消える (report part 作成失敗は注記 tx より前なので注記を書かない = N2 側の非書込み基準) | 同上 |
| N5 | `idea` / `idea_norm` / `status` 不変 | 同上 |
| N6 | prompt の backlog 表・note 表に `origin` 列があり、注記行だけ `unprofitable`、他行は空 | レンダ済み prompt 文字列 |
| N7 | 規律 5 が prompt に逐語で出る。既存遮断 pin (prompt に `holdout` 0 件、F5-2) は緑のまま | 同上 |
| N8 | 当該行が後に選択されて終端すると `last_result` は終端の値で上書きされる | 同上 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-13 | v1.0 | 初版 (bounded 設計、チャット提示 → ユーザー承認) | run19 観測 D、裁定 (c) の後継 | - |
| 2026-09-13 | v1.1 | 実装 `98a5d68` (sonnet) → 段 0 `e1595a5` (14 変異、生存 6 → pin S1〜S5) → ローカル 3 本 1 周目 `9088c86` (Y2/N14/dup6、pin L1/L2、本番欠陥 0) → codex 1 周目 (`tmp/review-20260913-nh/codex-r1.md`): Critical 0 / Important 1 (N2 の report/observation/approval 終端が未 pin) / Minor 5 (F5-5 説明改訂、N3 空振り、N5 note 不変、L1 docstring、N4 文言) → 全件是正。N4 の受入文言を「注記後の後段失敗で rollback」に訂正 (report 作成失敗は注記より前) | codex r1 | - |
