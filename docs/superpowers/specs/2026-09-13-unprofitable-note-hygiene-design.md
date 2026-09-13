# [unprofitable-note-hygiene] 設計書 v2.0

束: 収益性フロア不合格 (`unprofitable`) で終わった改善 mission が起票した backlog 行 (note / task) に機械注記を付け、次 mission の同型再提出を抑止する。ユーザー承認 2026-09-13。親: [floor-path-skips-duplicate-metrics] 裁定 (c) 現状維持、遮断 8 例外の再評価 (`2026-09-12-profitability-floor-design.md` v1.5)。

## 1. 問題

A4 run19 (`tmp/a4-run19-codex-20260913.md` 観測 D): mission #83 が holdout 段フロアで `unprofitable` に落ちた直後、#84 が #83 とビット一致の候補を再提出して同じフロアに落ちた。原因は情報漏洩ではなく、#83 が起票した note #74「fast=10/slow=30 は提出条件を満たした」(agent 自筆、in_sample 語彙) が backlog #69 の `unprofitable` (機械由来 1 bit) より強く agent の判断に効いたこと。

事実 (`src/agentic_fx/loops/improve_loop.py`):
- agent の discoveries (note / task) は選択時 `_select_and_bind` (`:1219`) で INSERT される = **gate より前**。起票時点では unprofitable かどうか分からない。
- prompt の backlog 表 / note 表 (`_backlog_table` `:649`) は `id / idea / status / attempts / assigned` のみで `last_result` を表示しない。
- task 行 (#73) も再現の種になる。`idea_norm` の重複検出は文言違いで効かない。

## 2. 設計 (v2.0: 専用列方式。v1.x の `last_result` 相乗り方式は `/code-review high` で根本原因と判定され撤回)

**なぜ作り直すか**: v1.x は注記を `last_result` 列に載せたため、同列を書く 10 箇所 (終端・note 昇格・人間の `backlog note` / `reopen` / `reopened`) がそれぞれ CAS と保持分岐を覚える必要があり、実際に 2 経路で注記が消えた (同一 mission 内の note → task 自己昇格、人間コマンド)。注記は「誰がいつ起票したか」という**系譜**であり、`last_result` (最後の評価結果) とは別の事実。専用列に置けば他の書き手と干渉しない。

1. **起票時の系譜**: `improvement_backlog.origin_mission_id INTEGER NULL` を `_ensure_column` で追加。`_select_and_bind` が INSERT する行 (discoveries の note / task、選択された新規 idea) に `ctx.mission_id` を書く。既存行の再利用 (`existing` / `promoted`) は書き換えない (最初の起票者が系譜)。`_SelectionOutcome.inserted_ids` は不要になるので**削除**。
2. **終端時の結果**: `improvement_backlog.origin_outcome TEXT NULL` を追加。`_finalize_gate_failed` の `mission_outcome == "unprofitable"` 分岐で、既存 `BEGIN IMMEDIATE` tx 内に `UPDATE improvement_backlog SET origin_outcome='unprofitable' WHERE origin_mission_id=?` (この mission が起票した全行。CAS 不要 — この列を書くのはここだけ)。他の終端では書かない。当該 mission の選択行 (`backlog_id`) 自身が起票行なら同様に書かれるが、`origin` 列と `last_result` は別列なので衝突しない。
3. **表示**: `_backlog_table` (items 表・notes 表) に `origin` 列。`row["origin_outcome"] == "unprofitable"` なら `unprofitable`、それ以外は空。`improve_context` の SELECT に `origin_outcome` を足す。
4. **規律 5**: v1.x と同文 (`origin` 列の意味は不変)。
5. **不変**: 遮断 8 の語彙 (`origin_outcome` の値は固定文言 `unprofitable` のみ、holdout の数値・段名・pair を含まない)、質検査の母集団 (F4-8)、`idea_norm` 規律、`last_result` の全書き手 (v1.x の CAS / 昇格保持分岐 / `promoted_from_note` 特別扱いは**撤回**して元に戻す)、`max_new_backlog_per_mission`。DB は additive な 2 列のみ (既存の `_ensure_column` 移行慣行)。
6. **受容するトレードオフ (明記)**: 同 mission が起票した**無関係な** discovery にも `origin_outcome='unprofitable'` が付く (`discoveries` は提出候補との紐付けを持たない)。規律 5 は「同じ指標・同じパラメータの再提出を避ける」誘導で、無関係な課題の選択を禁じない (status は不変で選択可能) ため、害は誤帰属の bias に留まる。紐付けを持たせるのは discoveries schema の変更になるので本束では行わない。
7. **人間コマンド** (`backlog note` / `reopen` / `reject`) は `origin_*` を触らない (系譜は消えない)。
8. **遡及しない**: 過去行 (#70/#71/#73/#74) の `origin_*` は NULL のまま。#73 は人間が `afx> backlog reject 73`。

## 3. 変更面

| 面 | 変更 |
|---|---|
| `store/db.py` | `improvement_backlog` に `origin_mission_id INTEGER NULL` / `origin_outcome TEXT NULL` (`_ensure_column`) |
| `loops/improve_loop.py` | `_select_and_bind` の INSERT に `origin_mission_id`、`_SelectionOutcome.inserted_ids` と `commit()` の受け渡しを削除、`_finalize_gate_failed(inserted_ids=...)` 引数を削除し `origin_mission_id=?` の UPDATE に置換、`_upsert_backlog_idea` の昇格分岐を v1.1 以前 (無条件 `promoted_from_note`) に戻す、`_backlog_table` は `origin_outcome` を読む |
| `loops/improve_context.py` | items / notes の SELECT に `origin_outcome` |
| `loops/prompts/improve_mission.md` | 規律 5 (不変) |
| tests | §4 に合わせて書き直し (v1.x の N9 CAS / N10 昇格保持は撤回、代替 pin を追加) |

## 4. 受入 (pin)

| # | 基準 | 観測点 |
|---|---|---|
| N1 | フロア不合格 (in_sample 段 / holdout 段) で終わった mission が INSERT した note と task の `origin_outcome == 'unprofitable'`、`origin_mission_id == mission_id` | sqlite fixture |
| N2 | `gate_failed:*` / `insufficient_trades` / report / observation / approval / `report_failed` 分岐では `origin_outcome` が NULL (`origin_mission_id` は INSERT 時に入っている) (逆変異: 条件を外すと red) | 同上 |
| N3 | 既存行の再利用 (`existing` / `promoted`) では `origin_mission_id` が最初の起票者のまま | 同上 |
| N3' | 同一 `_select_and_bind` 内で note を INSERT → 同 idea の task で自己昇格 → フロア不合格 → その行の `origin_outcome == 'unprofitable'` (v1.x で消えた経路、code-review #1) | 同上 |
| N4 | 同 tx: 注記後に `finish_improve_mission` が失敗すると `origin_outcome` も rollback | 同上 |
| N5 | `idea` / `idea_norm` / `status` / `last_result` 不変 (注記は `last_result` を一切書かない) | 同上 |
| N6 | prompt の backlog 表・note 表に `origin` 列、注記行だけ `unprofitable`、他行は空 | レンダ済み prompt |
| N7 | 規律 5 が prompt に逐語で出る。遮断 pin (`holdout` 0 件) 緑 | 同上 |
| N8 | 当該行が後に選択されて終端しても `origin_outcome` は残る (`last_result` は終端値、`origin` 列は `unprofitable` のまま) | 同上 |
| N9 | 逆順並行: A が INSERT → B が選択・終端 → A が `unprofitable` 終端 → B の `last_result` は不変、`origin_outcome` は付く (別列なので両立) | 同上 |
| N10 | 人間コマンド `backlog note` / `reopen` / `reject` を注記行に打っても `origin_*` 不変 (code-review #2) | 同上 |
| N11 | 選択行自身が起票行のとき、`last_result == reason` かつ `origin_outcome == 'unprofitable'` (code-review #5 の順序依存が消えていること) | 同上 |
| N12 | 無関係 discovery も注記される (受容トレードオフの pin、逆変異で緩めても red にならない = 記録用) | 同上 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-13 | v1.0 | 初版 (bounded 設計、チャット提示 → ユーザー承認) | run19 観測 D、裁定 (c) の後継 | - |
| 2026-09-13 | v1.1 | 実装 `98a5d68` (sonnet) → 段 0 `e1595a5` (14 変異、生存 6 → pin S1〜S5) → ローカル 3 本 1 周目 `9088c86` (Y2/N14/dup6、pin L1/L2、本番欠陥 0) → codex 1 周目 (`tmp/review-20260913-nh/codex-r1.md`): Critical 0 / Important 1 (N2 の report/observation/approval 終端が未 pin) / Minor 5 (F5-5 説明改訂、N3 空振り、N5 note 不変、L1 docstring、N4 文言) → 全件是正。N4 の受入文言を「注記後の後段失敗で rollback」に訂正 (report 作成失敗は注記より前) | codex r1 | - |
| 2026-09-13 | v1.2 | codex 2 周目 (`tmp/review-20260913-nh/codex-r2.md`): Critical 0 / Important 2 (I1 並行時の後発 UPDATE が他 mission の終端値を潰す → `AND last_result IS NULL` CAS / I2 note 昇格で注記が消える → 保持分岐) / Minor 2 (文書) → 全件是正 `b658caf` (pin N9/N10 追加、602 passed)。§2-2 の SQL と昇格規則、§4 N9/N10 を追記 | codex r2 | b658caf |
| 2026-09-13 | v2.0 | `/code-review high` (sonnet、`a0fd1e4..3a40070`) 5 件: #3 根本原因 = 注記を `last_result` に相乗りさせたため書き手 10 箇所が CAS/保持分岐を要し、#1 同 tx 自己昇格 / #2 人間 `backlog note`/`reopen` で注記が消える、#5 順序依存が未 pin、#4 無関係 discovery の誤帰属が未記録。**ユーザー裁定: 専用列 (`origin_mission_id` / `origin_outcome`) に作り直す** → §2 全面改訂、v1.x の CAS・昇格保持・`inserted_ids` を撤回、#4 は受容トレードオフとして §2-6 に明記 | code-review high | - |
