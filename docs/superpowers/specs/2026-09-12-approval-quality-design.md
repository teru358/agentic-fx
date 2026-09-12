# 承認品質・運用導線 設計書 v1 (2026-09-12)

- 日付: 2026-09-12 (初版)
- ステータス: **ユーザー承認済み (2026-09-12)**。根拠 = A4 12〜13 回目の実機観測 (`tmp/a4-run12-codex-20260912.md` / `tmp/a4-run13-ornith-20260912.md` / `tmp/a4-run13-muse-20260912.md` §6〜§7) と `.superpowers/sdd/plan10-plan/tickets.md` の該当チケット
- 対象: main `2801614`。本体設計書 phase2-10 (`2026-08-16-phase2-10-improve-loop-design.md`) と ledger 設計書 (`2026-09-10-ledger-preserve-design.md`) の上に立つ、承認品質・運用導線の 4 項目
- 本書の位置づけ: A4 10〜13 回目で ledger 保全束 (T1〜T4) の実機検証が進んだ結果、**保全そのものではなく「保全した結果をどう見せるか・重複候補をどう扱うか」の欠陥/観測**が 4 件出そろった。うち 3 件 (A/B/C) は同一 worktree で直列に、1 件 (D) はセキュリティ寄りで独立性が高いため別 worktree で並列に進める (実装プランは別文書)
- 保留: **行単位の採否 (`submitted` フラグ)** は [backtest-dedup-cache] と同時設計とし、本書では扱わない (§0.1)

## 0. 前提を疑う

| 通説 | 実測・再読 | 帰結 |
|---|---|---|
| 「gate を通った候補は承認価値がある」 | A4 12〜13 回目: codex(#74)/ornith(#75)/muse(#76) の 3 backend が**コードは別物 (content_hash 3 種) なのに in_sample/holdout の metrics が小数点以下まで完全一致** (pf 1.4981 / trades 194、holdout pf 0.8047 / trades 53)。backlog note (#43/#50/#52/#56) に書かれた具体パラメータ (fast=10/slow=30) へ 3 backend が収束し、同じ却下判断を 3 回求めている (`tickets.md:69`) | gate 通過だけでは承認提示の理由にならない。**既存 approval / example と成績が一致する候補は observation に落とす** (§A) |
| 「`plugins/_archive/INDEX.md` は改善ループの終端一覧」 | approval 終端 (#69, #74) は 3 run 連続で INDEX に載らない — `_write_archive_index_safe` の呼び出し元は `_settle_ledger_after_commit` (report/observation/failed/output_invalid/loser/gate_failed/report_failed 系) と commit 失敗補償だけで、**`_finalize_success` (approval 経路) からは呼ばれない** (`improve_loop.py:1802` 以降、`tickets.md:78`, run12 観測 B) | INDEX は「終端ログ」であって目録ではないと明記した上で、approval 終端も 1 行残す (§B) |
| 「archive は artifact_hash で引ける」 | run12 観測 C: `candidate_archives.artifact_hash` (backtest 時点、`00:47:09.7Z`) と approval payload の `artifact_hash` (提出時点、`00:47:38.97Z`) が不一致。原因は agent が self-test を書き直したこと (`test_plugin.py` は artifact_hash に含まれるが `content_hash` には含まれない)。両方の `content_hash` は一致する (`tickets.md:68`) | 人間向け導線・`afx> approval <id>` は `(mission_id, content_hash)` で `candidate_archives` を引く (§C) |
| 「mission prompt は argv に載せてよい」 | claude/codex/opencode の worker は Landlock で `/proc` を read-only に持つ (R10 受容、codex にも拡張済 = `b8004d9`)。CLI は prompt を argv で受けており (`claude -p <prompt>` / `codex exec <prompt>`)、run11 で「走行中の `/proc/<pid>/cmdline` でプロンプト全文が読めること」を実機で再確認済み (`a4-run11-claude-20260912.md` 末尾)。`improve.parallel ≥ 2` で同一 UID の他 mission から読める (`tickets.md:70`) | prompt を stdin (実体はワークディレクトリ配下の 0600 ファイル経由) に移し argv/cmdline から落とす (§D) |

## A. 成績一致候補の質検査 [candidates-converge-to-example-sma]

### 誰が何をどう扱うか
- gate 通過後・approval payload 組み立て前 (`improve_loop.py` の `_finalize_success` 手前、`_build_approval_payload` 呼び出し直前) に、**質検査**を 1 段挟む。
- 検査対象母集団: `backtest_runs` の **`scope='in_sample'` かつ `variant='candidate'`** の既存行のうち **`mission_outcome='approval'`** のもの。**この母集団には過去に承認された example (`sma_cross` 等) の行が自然に含まれる** — example 専用の variant を新設する必要はない (承認済みなら `mission_outcome='approval'` で既に絞り込める)。
- 候補の `(trades, pf, avg_r)` が母集団のいずれかの行と一致 (浮動小数は `store/db.py:409` の **`FLOAT_TOL` (1e-9)** で比較、既存の `abs(a-b) < FLOAT_TOL` パターンを流用) するとき、**approval にせず observation**へ倒す。
- observation の `last_result` は **`duplicate_metrics_of:<content_hash>`**（一致した既存行の `content_hash`）とし、**system note の文面から具体パラメータ (fast=10/slow=30 等) を落とす** — note 経由でパラメータが逆伝播し、次の mission が同じ局所解へ収束する経路 (run12/13 観測 C の原因) を断つ。
- `report`/`observation` 経路に合流するので、既存の INDEX・note 経路 (ledger 設計書 §L3) はそのまま使う。GC・archive publish は変更しない (candidate_archives 行・archive dir は既存どおり作る — 質検査対象という記録は残す)。

### 変更点表
| file | 変更 |
|---|---|
| `loops/improve_loop.py` | `_finalize_success` 手前に `_check_duplicate_metrics(conn, *, content_hash, pair, variant, source, base_interval, metrics)` を新設し呼び出す。一致時は `approval_payload = None` のまま `_finalize_report_or_observation` へ回し、`last_result="duplicate_metrics_of:<hash>"` を渡す |
| `store/backtest_runs.py` | `find_matching_approved_metrics(conn, *, pair, variant, source, base_interval, trades, pf, avg_r)` を新設 (`mission_outcome='approval'` 絞り込み + `FLOAT_TOL` 比較を SQL または Python 側で実施) |
| `store/db.py` | 既存 `FLOAT_TOL` を再利用 (新規定数不要) |
| system note 生成箇所 (`improve_loop.py` の note 文面組立) | 具体パラメータ (fast/slow 等) を note に書かない旅程を確認・削る (backlog note へは書けるが system note には出さない) |

### 検証
- pin: `(trades, pf, avg_r)` が既存 approval 行と一致 → observation + `duplicate_metrics_of` / 1 つでも異なれば approval を継続 / `avg_r` が `None` 同士の一致 (pf=None のケース) / `FLOAT_TOL` 境界 (ちょうど閾値上・閾値未満)
- 段 0 変異: 比較条件の `<` を `<=` に反転 / 3 値のうち 1 つを比較から外す / `mission_outcome='approval'` 絞りを外す (未承認 example も母集団に入る誤りが red になること)
- 実機 CP: A4 14 回目以降、backlog に fast=10/slow=30 系の note が残る状態で codex/ornith/muse いずれかを再実行し、observation `duplicate_metrics_of` が出ること・system note に具体パラメータが出ないことを確認

## B. INDEX.md に approval 終端も 1 行 [archive-index-naming]

### 誰が何をどう扱うか
- `_finalize_success` (approval 経路) の末尾、外側 commit 成功後に `_write_archive_index_safe(conn, ctx=ctx, status="approval", now=now)` を追加で呼ぶ (既存の `_settle_ledger_after_commit` 経路と同じヘルパを流用、二重書き防止のロックも既存のまま効く — `c0ddb15` のプロセス内 lock)。
- `plugins/_archive/INDEX.md` の見出し／ドキュメント文言を **「終端ログ」**と明記し直す: 「承認可否の目録ではない。GC はこのファイルを更新しない。承認済み候補の正 (authoritative source) は `candidate_archives` 表と `approval_requests` 表である」という 1 行をヘッダに足す。
- GC (ledger 設計書 §L4 `sweep_orphans` の archive branch) は変更しない — INDEX.md はログとして追記のみで、GC 対象にしない現行方針を維持する。

### 変更点表
| file | 変更 |
|---|---|
| `loops/improve_loop.py` | `_finalize_success` の末尾 (Tx-2 外側 commit 成功後) に `_write_archive_index_safe(..., status="approval", now=now)` を追加 |
| `plugins/_archive/INDEX.md` のヘッダ生成箇所 (`_write_archive_index_safe` 内、初回作成時のテンプレート) | 「終端ログ (GC 非対象)。正は `candidate_archives`」の 1 行を追記 |
| 設計書 (ledger 設計書 §L3 / 本体設計書該当箇所) | INDEX.md の性格を「終端ログ」に統一する旨を次版で反映 (本書は起票のみ、本文修正は実装後の次版) |

### 検証
- pin: approval 終端後に INDEX.md に 1 行追加される (mission_id / status=approval / 候補数 / best) / 二重終端 (commit 失敗補償からの再試行) で行が重複しない (既存 lock の対象に approval 経路も含まれることを確認) / report/observation/failed 系の既存行フォーマットと同じ列で揃う
- 段 0 変異: `status="approval"` を渡さない (既存呼び出しの分岐漏れ) / lock を外す (二重行が red になること)
- 実機 CP: A4 14 回目、approval に到達する run (run12/13 の局所解を A で弾いた後の再現、または baseline を変えた新規候補) で INDEX.md に approval 行が出ることを確認

## C. archive 引き当てキー [archive-artifact-hash-vs-submitted]

### 誰が何をどう扱うか
- `afx> approval <id>` (`commands.py::_approval_detail`) と、将来の人間導線・監査ツールは **archive dir の引き当てに approval payload の `artifact_hash` を使わない**。`_approval_detail` は表示している payload の `mission_id` + `content_hash` で `candidate_archives` を引き、一致した行の `archive_path` を表示行に追加する。
- `content_hash` は backtest 時点 (archive snapshot) と提出時点 (approval payload) の両方で不変 (plugin.py/config.yaml 相当のみをカバー) なので引き当てキーとして安定する。`artifact_hash` は `test_plugin.py` を含むため提出前の self-test 修正で動きうる (run12 観測 C) — 引き当てキーには使わない、という理由を docstring に明記する。

### 変更点表
| file | 変更 |
|---|---|
| `store/candidate_archives.py` | `find_by_mission_content(conn, *, mission_id, content_hash) -> CandidateArchiveRow | None` を新設 (`(mission_id, artifact_hash)` UNIQUE 制約はそのまま、検索は content_hash 経由に限定) |
| `commands.py::_approval_detail` | payload の `mission_id`/`content_hash` で `find_by_mission_content` を呼び、見つかれば `archive=<archive_path>` の行を出力に追加。見つからない場合は「archive 不明」と明示 (GC で消えている等) |
| docstring (`candidate_archives.py` / ledger 設計書 §L3) | 「引き当ては `(mission_id, content_hash)`。`artifact_hash` は self-test 修正でずれるため引き当てに使わない」を明記 |

### 検証
- pin: `content_hash` 一致・`artifact_hash` 不一致のケースで archive が引ける (run12 の実データ形をフィクスチャ化) / 該当 mission に archive が無い (GC 済み・失敗終端) 場合の表示 / 同一 mission に複数候補があるとき指定 `content_hash` の 1 件だけ返る
- 段 0 変異: 検索キーを `artifact_hash` に戻す (run12 実データで見つからなくなることが red になる)
- 実機 CP: A4 14 回目、`afx> approval <id>` を run12/13 の approval (#7〜#9) に対して実行し、archive path が表示されることを確認

## D. prompt を stdin 渡しに [mission-prompt-in-argv-readable-via-proc]

### 誰が何をどう扱うか
- `ClaudeRunner._build_argv` / `CodexRunner._build_argv` から **`mission.prompt` を argv 要素として渡すのをやめる** (`claude_runner.py:64` の `-p <prompt>` 位置引数、`codex_runner.py:51` の `exec <prompt>` 位置引数)。argv は `-p` (claude) / `exec` (codex) のフラグのみを残す。
- prompt の受け渡しは **workdir 配下のファイル経由の stdin** にする: mission 起動時に `workdir/prompt.txt` を **0600** で書き、`CliRunner._run_cli_process` が現在 `/dev/null` を開いている箇所 (`cli_runner.py:279`) と同じ形で、その代わりに `os.open(prompt_path, os.O_RDONLY)` を Popen の `stdin=` に渡す。パイプで書き込みながら流すのではなく**ファイル読み出し**にすることで、prompt サイズによる pipe バッファのデッドロックを避ける (現行の devnull 手法をそのまま流用できる形)。
- claude `-p` / codex `exec` の**両 CLI が引数無しの prompt を stdin から読めるかは実測が要る** (`tickets.md:70` に明記の未検証事項)。設計としては両立できる形 (workdir 内ファイル読み出し) を用意し、**実ターン各 1 回まで**で受理形式を確認する (§検証)。受理しない場合はその CLI だけ現行の argv 渡しに残し、起票 (別チケット) して不一致を明示する。
- `/proc/*/cmdline` にはもう prompt 文字列が現れない。ファイル自体は mission workdir (0700、Landlock で他 mission から不可視) の中にあるので、同一 UID の並行 mission 間の露出は無くなる (workdir 自体を割れば読めるが、それは argv 越しの `/proc` 走査より遥かに狭い攻撃面)。

### 変更点表
| file | 変更 |
|---|---|
| `runners/claude_runner.py` | `_build_argv` から `mission.prompt` を argv から除去 (`-p` のみ残す) |
| `runners/codex_runner.py` | `_build_argv` から `mission.prompt` を argv から除去 (`exec` のみ残す) |
| `runners/cli_runner.py` | `_run_cli_process` (または新設 `_write_prompt_file` ヘルパ) が `workdir/prompt.txt` (0600) を書き、Popen の `stdin=` にその読み出し fd を渡す (既存の `devnull_r` 開閉パターンを流用) |
| 既存 argv pin テスト | 「argv に prompt 文字列が含まれない」方向へ更新 (現行は含まれる前提のテストがあるはずなので反転) |
| 新規 pin | `/proc/<pid>/cmdline` (fake CLI 経由) に prompt 文字列が現れないこと |

### 検証
- pin (段 0): argv に mission.prompt の一部が含まれない / `workdir/prompt.txt` が 0600 で書かれる / stdin fd がそのファイルを指す (fake Popen で捕捉) / ファイル内容が mission.prompt と一致
- 逆変異: prompt を argv に戻す変異が red になること
- **実機確認 (要実測、各 CLI 1 ターンまで)**: claude `-p` (プロンプトを stdin 経由で渡し、正常に mission が完走するか) / codex `exec` (同様) — 両方成功すれば既定化、いずれか不成立ならその backend だけ現行 argv 渡しに残し起票
- 実機 CP: A4 14 回目、mission 走行中に `/proc/<pid>/cmdline` を読んで prompt が含まれないことを確認 (run11 で確認した「読めてしまう」の反証)

## 保留 E: 行単位の採否

`backtest_runs` の行が「この mission の終端」であって「この候補が承認された」ではない問題 (run13/ornith 観測 C: 途中で捨てた候補の行にも `mission_outcome='approval'` が付き live 絞りを通過しうる) は、**`submitted` フラグによる行単位の採否**を要する。これは [backtest-dedup-cache] (同一 (候補, config) の backtest 再実行で予算を消費しない) と設計上のキー (履歴内容の同一性、trial_count の数え方) を共有するため、**本書では設計せず、[backtest-dedup-cache] と同時設計とする**。

## 変更履歴

| 日付 | 版 | 変更 | 理由 (レビュー指摘 / 実機観測 / 裁定) | commit |
|---|---|---|---|---|
| 2026-09-12 | v1 | 起案。A (質検査) / B (INDEX approval 行) / C (archive 引き当てキー) / D (prompt stdin 化) の 4 項目、保留 E ([backtest-dedup-cache] と同時設計) を明記 | 根拠 = A4 12〜13 回目の観測 (`tmp/a4-run12-codex-20260912.md`, `tmp/a4-run13-ornith-20260912.md`, `tmp/a4-run13-muse-20260912.md`)、ユーザー承認 (2026-09-12) | - |
