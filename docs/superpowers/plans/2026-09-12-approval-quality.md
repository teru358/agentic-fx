# 承認品質・運用導線 実装プラン v1 (設計書 = `2026-09-12-approval-quality-design.md` 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) または superpowers:executing-plans で task ごとに実行すること。

**Goal:** 設計書 v1 の A (成績一致候補の質検査) / B (INDEX.md に approval 終端も 1 行) / C (archive 引き当てキー) / D (mission prompt の stdin 渡し) を実装する。**設計を変えない** — 曖昧な箇所は各 task 末尾の「執筆時の未決事項」に列挙する。

**Architecture:** 4 task を 2 束に分ける。**T-A/T-B/T-C は 1 worktree で直列** (いずれも `loops/improve_loop.py` の `_finalize_success` 近傍・`store/backtest_runs.py` / `store/candidate_archives.py` を触るため、同一ファイルへの衝突を避ける)。**T-D は別 worktree で並列可** (`runners/claude_runner.py` / `runners/codex_runner.py` / `runners/cli_runner.py` のみを触り、A〜C と重ならない)。決定論的コアの判定ロジック (Risk Gate・approval 決定) は全 task で不変。

**Tech Stack:** Python 3.13 / uv / pytest / sqlite3 (既存 schema への追加のみ、破壊的 migration なし)

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ
- 発注・SL 変更・クローズ・資金保護は本プランの対象外 (改善ループの経路に閉じる)
- T-D の変更は **argv からプロンプト文字列を除くだけ**で、Landlock の allowlist・遮断 8 項目には触れない
- `plugins/`・`config/`・`data/` はコミット対象外のまま (本プランは `src/` と `docs/` のみ変更)

## §0. 先行する実機 run — 失敗終端の実証

ledger 設計書 (`2026-09-10-ledger-preserve-design.md`) §3 の全終端表は `failed` / `timeout` / `output_invalid` 終端でも台帳・archive・INDEX が保全される設計だが、**A4 10〜13 回目では未観測**(`2026-09-10-ledger-preserve-design.md` 変更履歴の最終行、「失敗終端の永続化は未観測」)。T-B (INDEX への approval 行追加) と併せて INDEX.md の全終端網羅を検収するには、失敗終端を実機で 1 回は起こす必要がある。

**実施内容 (T-A〜T-D 着手前、または並行して実施可)**:
1. `config/settings.yaml` の `improve.tool_budget.max_tool_calls` を一時的に **6** に絞り、backtest 1 回に満たない予算で mission を強制的に abort させる ([mission-abort-on-tool-budget] の Tier F と同じ機構)
2. improve mission を 1 回発火し、`failed` (`tool_budget_abort:*`) 終端で `backtest_runs (mission_outcome='failed')` / `candidate_archives` (backtest が 1 回でも成功していれば) / `plugins/_archive/INDEX.md` の行が揃うことを確認
3. 終了後は `max_tool_calls` を既定値に戻す (設定は個人設定 `config/settings.yaml` のため、変更・復元は運用操作でありコミット対象外)
4. 結果は `tmp/a4-run14-failed-*.md` に記録 (書式は run10〜13 の CP 実値表を踏襲)

この run は T-A〜T-D の実装成否を左右しないため、**着手前に必須ではないが、T-B の検収 (INDEX.md が全終端を網羅すること) の材料として実装完了までに 1 回は実施する**。

## T-A: 成績一致候補の質検査 [candidates-converge-to-example-sma]

**対応**: 設計書 §A

**変更**:
- `store/backtest_runs.py`: `find_matching_approved_metrics(conn, *, pair, variant, source, base_interval, trades, pf, avg_r)` を新設。`scope='in_sample' AND variant='candidate' AND mission_outcome='approval'` に絞り、`FLOAT_TOL` (`store/db.py:409`) で `(trades, pf, avg_r)` を比較
- `loops/improve_loop.py`: `_finalize_success` 手前 (gate_metrics 確定後・approval_payload 組み立て前) に質検査を挟み、一致時は `approval_payload = None` のまま `_finalize_report_or_observation` へ回す。`last_result="duplicate_metrics_of:<content_hash>"`
- system note 文面組立箇所: 具体パラメータ (fast/slow 等) を note に出さない

**完了条件**:
- [ ] pin: 一致 → observation + `duplicate_metrics_of` / 不一致 (3 値のうち 1 つでも異なる) → approval 継続 / `avg_r=None` 同士の一致 / `FLOAT_TOL` 境界値
- [ ] 逆変異 red: 比較演算子反転 / 3 値のうち 1 つを比較から除外 / `mission_outcome='approval'` 絞りの除去
- [ ] フルスイート green

## T-B: INDEX.md に approval 終端も 1 行 [archive-index-naming]

**対応**: 設計書 §B。**T-A の後**に着手 (T-A が observation に倒した候補は approval 経路を通らないため、INDEX 行が増えるのは approval に残った候補のみ — 順序が逆だと「質検査で弾かれた候補」も approval 行として INDEX に載る事故になる)

**変更**:
- `loops/improve_loop.py::_finalize_success`: 外側 commit 成功後に `_write_archive_index_safe(conn, ctx=ctx, status="approval", now=now)` を追加
- `plugins/_archive/INDEX.md` のヘッダテンプレート: 「終端ログ (GC 非対象)。正は `candidate_archives`」を追記

**完了条件**:
- [ ] pin: approval 終端後に INDEX.md へ 1 行追加 (mission_id/status=approval/候補数/best) / 二重終端 (commit 失敗補償の再試行) で行が重複しない (既存プロセス内 lock の対象に approval 経路が含まれることを確認) / 既存の report/observation/failed 系の行フォーマットと列が揃う
- [ ] 逆変異 red: `status="approval"` を渡さない分岐 / lock 除去
- [ ] フルスイート green

## T-C: archive 引き当てキー [archive-artifact-hash-vs-submitted]

**対応**: 設計書 §C。T-A/T-B と独立性が高いが同一ファイル (`improve_loop.py`) を触るため同 worktree の直列で最後に置く

**変更**:
- `store/candidate_archives.py`: `find_by_mission_content(conn, *, mission_id, content_hash) -> CandidateArchiveRow | None` を新設
- `commands.py::_approval_detail`: payload の `mission_id`/`content_hash` で `find_by_mission_content` を呼び、`archive=<path>` 行を出力に追加。無ければ「archive 不明」

**完了条件**:
- [ ] pin: `content_hash` 一致・`artifact_hash` 不一致 (run12 実データ形のフィクスチャ) で archive が引ける / archive 無し (GC 済み・失敗終端) の表示 / 同一 mission 複数候補で指定 `content_hash` の 1 件だけ返る
- [ ] 逆変異 red: 検索キーを `artifact_hash` に戻す
- [ ] フルスイート green

## T-D: prompt を stdin 渡しに [mission-prompt-in-argv-readable-via-proc]

**対応**: 設計書 §D。**別 worktree で T-A〜T-C と並列可**

**変更**:
- `runners/claude_runner.py::_build_argv`: `mission.prompt` を argv から除去 (`-p` のみ残す)
- `runners/codex_runner.py::_build_argv`: `mission.prompt` を argv から除去 (`exec` のみ残す)
- `runners/cli_runner.py`: `workdir/prompt.txt` (0600) を書き、`_run_cli_process` の `stdin=` を現行の `devnull_r` パターンから `os.open(prompt_path, os.O_RDONLY)` に変更
- 既存の argv pin テスト: 「argv に prompt 文字列を含む」前提を「含まない」へ反転

**完了条件**:
- [ ] pin: argv に prompt の一部が含まれない / `prompt.txt` が 0600 で書かれ内容が一致 / stdin fd がそのファイルを指す (fake Popen)
- [ ] 逆変異 red: prompt を argv に戻す
- [ ] **実機確認 (要実測、各 CLI 実ターン 1 回まで)**: claude `-p` が stdin からの prompt を受理して mission 完走 / codex `exec` が stdin からの prompt を受理して mission 完走。**いずれか不成立ならその backend だけ現行 argv 渡しに残し、起票して報告する** (設計を強行しない)
- [ ] フルスイート green

## レビュー段

1. **段 0 (指揮者の変異スイープ)** — レビュー前に必ず回す。T-A/T-B/T-C 束と T-D 束それぞれで実施
2. **1 周目**: codex + ローカル LLM 3 本 (枠ゼロ、並列可)
3. **2 周目**: `/code-review high` + codex + ローカル 3 本
4. `/code-review high` は指揮者から起動できないため**ユーザーに打ってもらう**。有償 2 本 (`/code-review` と sonnet) は並列にしない
5. **3 周目 (must-fix のみ)**: ブリーフ付き sonnet。1〜2 周目で must-fix (Critical/Important) が出た場合のみ実施。Critical/Important 0 なら 2 周で収束とする

## 実機確認 (A4 14 回目)

- **codex 1 ターン**で T-A/T-B/T-C を検収: 質検査が発火する状況を作る (backlog に fast=10/slow=30 系の note が残る環境、または run12/13 の再現) → observation `duplicate_metrics_of` を確認 / それとは別に承認に至る候補があれば INDEX.md の approval 行と `afx> approval <id>` の archive 表示を確認
- **claude 1 ターン**で T-D を検収: mission 走行中に `/proc/<pid>/cmdline` を読み、prompt が含まれないことを確認 (run11 で確認した「読めてしまう」の反証)
- 実施前提: §0 の失敗終端 run (`tmp/a4-run14-failed-*.md`) を完了していること

## 進捗表

| task | 状態 | commit | 備考 |
|---|---|---|---|
| §0 失敗終端 run | 未着手 | - | `tmp/a4-run14-failed-*.md` |
| T-A 質検査 | 完了 | 7837fe9 | 逆変異 5/5 RED。多 pair は 1 pair 一致で降格、降格時 gate 行なし、`observation:` prefix |
| T-B INDEX approval 行 | 完了 | 7837fe9 | 逆変異 2/2 RED |
| T-C archive 引き当て | 完了 | 7837fe9 | 逆変異 3/3 RED |
| T-D prompt stdin | 完了 (実機未確認: claude/codex 各 1 ターンで A4 15 回目) | 7837fe9 | 逆変異 2/2 RED。pgid 回収テストは trade profile + python fixture に |
| レビュー 1 周目 | 未着手 | - | |
| レビュー 2 周目 (`/code-review high`) | 未着手 | - | ユーザー起動 |
| レビュー 3 周目 | 未着手 (must-fix 時のみ) | - | |
| A4 14 回目 (実機確認) | 未着手 | - | |

## 変更履歴

| 日付 | 版 | 変更 | 理由 (レビュー指摘 / 実機観測 / 裁定) | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。T-A〜T-D の task 分割 (A〜C 直列 1 worktree、D 並列別 worktree)、§0 に失敗終端実証 run を追加、レビュー段・実機確認・進捗表を規定 | 設計書 `2026-09-12-approval-quality-design.md` v1 のユーザー承認 (2026-09-12) を実装プランへ写す | - |
| 2026-09-12 | v1.1 | §0 失敗終端実証 run 完了 (codex #77、`max_tool_calls` は 6 でなく 8 — 6/7 では run_backtest (8 番目) 前に abort し観測不能。窓は 1 呼び出し分) → F1〜F7 充足、新規欠陥 0 | 実機観測 | fb6aed8 以降 |
| 2026-09-12 | v1.2 | T-A〜T-D 実装完了 (2 worktree 並列、段 0 逆変異 12/12 red、3736 passed)。進捗表更新。次 = codex + ローカル 1 周目 → /code-review high → A4 15 回目 | 実装完了 | 7837fe9 |
| 2026-09-12 | v1.3 | 1 周目完了: codex Important 4 (I1〜I3 是正 `d2c103e`、I4 = 実機で) / ローカル 3 本 本番欠陥 0、pin 8。次 = A4 15 回目 (claude + codex 各 1 ターン: D の `/proc/<pid>/cmdline` に prompt 無し、A の `duplicate_metrics_of` 降格、B の INDEX approval 行、C の `approval <id>` archive 行) → `/code-review high` | レビュー | (this) |
| 2026-09-12 | v1.4 | A4 15 回目 (claude #78 observation 10.7 分 tool 14 / codex #79 report 7.3 分 tool 16、`tmp/a4-run15-{claude,codex}-20260912.md`): **D 両 backend で成立** (argv に prompt 本文ゼロ、claude argc 245→20 / codex `exec -`、prompt.txt 0600、両 CLI が stdin 受理) / **C 既存 #6〜#9 で archive= 解決** (artifact_hash 不一致でも content_hash で引けた) / B は observation・report 行の追記を確認、approval 行は条件未発生 / A は条件未発生 (関数は生存、配線は未実証)。新規欠陥 0。観測: INDEX ヘッダ注記は既存ファイルに後付けされない (軽微、据え置き = 文書側に「終端ログ」と明記済) | 実機観測 | 22479f8 |
| 2026-09-12 | v1.5 | 2 周目 `/code-review high` 8 件 (Confirmed 1 = 質検査の check-then-insert レース) → 全採用 `9acc651` (逆変異 5/5 red、improve profile の pgid 実 subprocess テストを C fixture で復元)。3757 passed。次 = codex + ローカル 2 周目 (是正 diff 限定) → must-fix が出た束なので 3 周目要否をユーザーへ | レビュー | 9acc651 |
| 2026-09-12 | v1.6 | 2 周目完了: codex (是正 diff 限定) Important 2 + Minor 1 → I2 argv ガード部分一致 / Minor コンパイル失敗は fail を `4d42dd4`、I1 (復元テストが必ず失敗) は偽 (WorkerRunner が source_snapshot_dir を workdir へ copytree) / ローカル 3 本 本番欠陥 0、pin 3 `aa0fb8f` (CR1 の tx 内質検査は並行性まで一次証拠で閉じている)。3764 passed。**must-fix (CR1) が 2 周目で出た束 → 3 周目要否はユーザー判断** | レビュー | aa0fb8f |
| 2026-09-12 | v1.7 | 3 周目 (sonnet ブリーフ + 除外リスト、`tmp/review-20260912-aq/sonnet-r3.md`): Critical 0 / Important 0 / Minor 0、§A〜§D 一致、実測 3 件 (質検査の SELECT は approval_requests 5000 × backtest_runs 20000 で 37 ms / values_match の frozen tol / prompt.txt の O_EXCL と resume 経路)。**レビューサイクル終了。** approval #6〜#9 は全 reject (`tmp/approval-20260912.md`、staging 即時 drop、archive 残置、symlink 不変、backlog 4 件が observation)。次束候補: テスト衛生 / [backtest-dedup-cache] + 保留 E / A4 16 回目で §A の配線実証 | レビュー・運用 | 886e0d5 |
| 2026-09-12 | v1.8 | A4 16 回目 (codex #80、`tmp/a4-run16-codex-20260912.md`): §A は条件未発生 (新規戦略 ema_rsi_pullback、母集団 8 行は rejected 込みで生存)、**§B approval 行の INDEX 追記 (6→7 行) と §C 新規 id の archive= 解決を初めて実機確認**、§D 維持。新規欠陥 0。approval #10 (in_sample pf 0.71) は要 reject | 実機観測 | 29309ce |

