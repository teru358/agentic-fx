# 改善ループの質 設計書

**版:** v1.0 (ユーザー承認済み、実装可)
**対象:** 改善ループの質の 4 点 (A 自己書き戻しの遮断、B 重複の定義と質検査の強化、C 目標の定義と伝え方、D 床の戻し方) を 1 つの spec で扱う。チケット `improve-backlog-self-feedback-duplicates-work` / `candidates-converge-to-example-sma` / `improve-objective-filtered-signal-quality` を本 spec で閉じる (最後のものは目標の縮小版まで。測定基盤は §11)。
**位置づけ:** ユーザー承認済み (2026-10-19、§8.1 に 7 問の決定を記録)。実装に入ってよい。B の `family_redirect` 有効化は別 spec `2026-10-19-approval-content-design.md` (v1.0 実装可、着手済み) の §10.2 の T1 (API + CLI + web) と T2 (Discord 承認投稿) の完了を条件にする (§2.3)。この 2 つは本 spec の task と区別するため、以下 `AC-T1` / `AC-T2` と書く。本 spec の task は `IQ-T0`〜`IQ-T3` (§10)。

## 0. 目的、範囲、決定

週 1 回の改善 mission は、直近 6 回 (10/02 以降) のうち 3 回が重複 (1062 / 1063 / 1545)、agent 発の新規仮説で承認まで行ったのは #23 の 1 本だけだった (現状調査 §2.3)。原因は 3 つ重なっている。(a) agent 自身の成果文が backlog に task として戻る (#92 → 1062、#97 → 1063、#102 → 1545)。(b) 重複検知は in-sample 成績の完全一致だけで、SL/TP を変えれば通る。(c) 目標が prompt に書かれておらず、床も案 A 設定で実質ゼロ (`config/settings.yaml:83-84` の `min_pf: 0.0`、`require_positive_avg_r: false`)。この設計は (a)(b)(c) を直し、案 A の床を戻す。

**目的文 (prompt の目的節の本文、製品既定):** 「配備済みと異なる市場仮説を、根拠と再現手順が明確な候補として 1 本ずつ増やす。候補の優先順位は仮説の違いと検証可能性で決め、収益指標は提出可否の床としてのみ用いる。既存仮説の調整は新規名を作らず、同名の更新として扱う。」

| 項目 | 決定 |
|---|---|
| 範囲 | A 自己書き戻しの遮断、B 重複の定義と質検査の強化、C 目標の定義と伝え方、D 床の戻し方。範囲外は測定基盤 (戦略別帰属と影の台帳)、日次 / 週次の cadence 分離、指名実行、mission 出力の保存 (§11)。 |
| A 書き手 | 不変条件: **`discoveries` は open の行を作らない。agent が新規 task を作れる唯一の経路は、その mission で直ちに選択される `selected.idea` である。** backlog に task を足せるのは人間 (`afx ctl backlog add`)、親の自動記録、この `selected.idea` の 3 つだけ。agent は `discoveries` に note (`fact` / `idea`) しか書けない。`task` kind は廃止する。文字列判定は使わない。 |
| B 重複 | 最終分類の優先順位と実行位置を固定する (§2.3)。ロジック等価 (数値リテラルと非意味的文字列を落とした AST 構造 hash) は、別名・却下済み・降格済み・example との一致なら質検査で拒否する。配備済みの**同名旧版**との一致は「更新」として許す。仮説族 (indicator 構成 + 足 + pair が配備済み戦略と同じ) は、別名なら既存名での更新として出し直すよう降格する。ただしこの降格は AC-T1 と AC-T2 の完了後に有効化し、それまでは検出と記録 (`quality_flags` の `family_detected`) だけで降格しない。 |
| C 目標 | 上の目的文。「異なる市場仮説」と「検証可能性」が優先順位で、収益指標 (PF など) は提出可否の床としてだけ使う。holdout の条件と数値、「標本不足」「評価可能」は agent 面に一切出さない。 |
| D 床 | 案 A 設定を、本 spec の実装 merge と同時に戻す。3 値 (`min_pf` 1.0、`require_positive_avg_r` true、`require_holdout_evaluable` true) は一組で変更し、標本下限 30 は据え置く。daemon の再起動と検証を含む運用手順 (IQ-T3)。IQ-T1 と IQ-T2 の両方が済んでから行う。承認済み #21〜#25 は自動では再評価・解除しない。床を戻す前に、保存済み backtest を新基準で読み取り専用に再評価して結果を人間に提示し、#21 / #24 / #25 を個別に keep / disable と明示決定する (§2.5、§8.1 問 6)。戻す時期は IQ-T1 + IQ-T2 の完了と同じリリース窓 (§8.1 問 4)。 |
| 多様性 | 強制しない。親が「配備済みの仮説族の一覧」を prompt に出し、同族は更新扱いになることを伝える。軸の指定は利用者が directives.md に書く。 |
| 実装 | 1 つの spec、先行の IQ-T0 (schema + migration + 記録)、その後 IQ-T1 (agent 面)・IQ-T2 (質検査)、運用手順の IQ-T3 (§10)。 |

前提を疑った点。(1) 「重複を拒否すれば新規仮説が増える」は自明でない。拒否は無駄な試行を減らすだけで、新規を出す力は与えない。agent が新規を出せないのは目標と材料が無いためでもある (§2.4)。(2) 質検査の (b) は #25 を捕まえない (§2.3 の実測)。捕まえるのは (c) の仮説族。(3) 床を戻すと 4 本のうち 1 本 (#21) は今の基準で不合格になり、配備済みと新基準が食い違う (§2.5、§8.1 問 6)。(4) 「同名で出し直せば更新として通り、人間が差分を見て裁く」は、現行 main では成立しない。差分表示は未実装で、同名更新は `duplicate_logic` と衝突する (§2.3)。

## 1. 用語と非目標

| 用語 | 意味 |
|---|---|
| 自己書き戻し | agent が `discoveries` に書いた自由文が backlog の選択対象 (open) になり、次の mission で課題として選ばれること。 |
| note | 選択対象にならない backlog 行 (status=note)。prompt の表示にだけ出る。 |
| 候補台帳 | 親が mission 中に評価した全候補 (採否を問わず) を、名前・仮説族・ライフサイクルの分類 (`outcome_class`)・検出属性 (`quality_flags`) だけで記録した表。成績・holdout・人間の reason は台帳の新列に持たない (§2.2)。 |
| 最終 artifact の行 | 提出した、または判定した候補の `candidate_archives` 行。最終 artifact は「終端に至った時点の staging の bytes」で、その `artifact_hash` で引く。候補の identity は `(mission_id, artifact_hash)` で、pair は属性。承認 payload の `candidate_archive_id` が指す (§2.2)。 |
| ロジック hash | 候補の `plugin.py` を、docstring・数値リテラル・非意味的な文字列 (rationale 等) を落として AST 化し、`config.yaml` の構造 (キーと依存 indicator 名、値は落とす) を足して hash したもの。規則は §2.3。 |
| 仮説族 | kind ごとの key。strategy は `config.yaml` の依存 indicator plugin 名の集合、足、pair の集合の 3 つ組。indicator は plugin 名と足 (pair は持たない)。依存 indicator が空の strategy (example の SMA クロスなど) は仮説族を持たない (§2.3)。 |
| 更新 | 承認画面の分類 `updated` (承認 payload の `name` が配備済みと同名で版が違う)。別の承認 spec (`approval-content`) が定める分類で、**その実装完了 (AC-T1 と AC-T2) までは現行 main に存在しない将来仕様**。 |
| 質検査 | 候補を降格・拒否する検査の総称。現物では 2 か所で動く。plugin gate 内の完全コピー (`noop_copy`) と、`_finalize_success` の tx 内の成績完全一致 (`duplicate_metrics_of`)。本 spec は (b)(c) を後者の tx 内に足す (§2.3 の実行位置の表)。 |
| agent 面 | agent が読めるもの全部。prompt、候補履歴の節、activity の agent 投影、降格の通知文、backlog の note と `last_result`。 |

非目標: 発火時刻の重なりによる重複判定 (§11)、根拠タグの契約追加、戦略別の帰属と影の台帳、他 pair / 他足 / 参照系列を軸にすること、親が mission ごとに探索軸を割り当てること、drawdown や連敗を床に足すこと、gate の式の変更、承認画面の変更 (approval-content が担う)。

## 2. 材料と設計

### 2.1 A: 自己書き戻しの遮断

現状。agent の最終出力 `discoveries[]` (`kind` = `task` / `fact`) は `_select_and_bind` が `max_new_backlog_per_mission` (20) まで `_upsert_backlog_idea` で INSERT し、`task` は status=open、`fact` は note になる (`src/agentic_fx/loops/improve_loop.py:1583-1599`、`:1525-1560`)。重複判定は `idea_norm` の文字列完全一致だけ。さらに `selected.backlog_id` が null で `selected.idea` に新規文を書くと、同じ関数で task として INSERT され、そのまま選択される (`:1604-1625`)。成果報告を書ける欄が 2 つある。prompt は `task` を「実装できる変更」と説明するだけで、成果文を登録しない制約は無い (`loops/prompts/improve_mission.md:133-136`)。

変更。

| 欄 | 変更 |
|---|---|
| `discoveries[].kind` | `fact` (観察・制約の記録) と `idea` (未試行の仮説) の 2 種にする。どちらも note として保存し、選択対象にしない。`task` は prompt から消す。出力検査は `task` を拒否せず note に落とす (出力全体を捨てると mission の 4 分と枠が無駄になる)。 |
| 昇格 | `_upsert_backlog_idea` の「note + task → open」昇格 (`:1532-1540`) を agent 経路から外す。昇格は人間 (`afx ctl backlog reopen` 系) だけ。 |
| `selected.idea` (新規) | 残す。agent が新規 task を作れる唯一の経路であり、その回に実施する仮説の宣言として同じ mission で消費される。prompt に「仮説の記述として書く。実装済みの成果を書かない」と書く。row が観察に戻って再選択されるのは再挑戦の意図どおり。 |
| 上限 | `discoveries` の**入力件数**の上限を 20 から 5 に下げる (超過分は落として activity に残す)。note が 20 件の表示枠を食う問題 (現状調査 §3.1 の「get_signals が無い」旨の 8 件) を減らす。 |

親の自動記録 (候補台帳、§2.2)。agent は候補の評価結果を backlog に書かない。親が mission 終了時に、同じ tx 内で候補ごとに記録する。

### 2.2 候補台帳

置き場は `candidate_archives` (mission 中に評価した候補の来歴、`store/candidate_archives.py`、列は `mission_id` / `name` / `content_hash` / `artifact_hash` / `archive_path` / `pair` / `metrics_json`、`UNIQUE(mission_id, artifact_hash)`) に次の 4 列を足し、`pair` を NULL 許可に変える (下の「`pair` の NULL 許可」)。新しい表は作らない。

| 追加列 | 内容 |
|---|---|
| `logic_hash` | §1 のロジック hash。親が行を作るときに staging / snapshot の bytes から計算して保存する。 |
| `family_key` | §1 の仮説族を kind ごとに正規化した文字列 (strategy は indicator 集合 + 足 + pair、indicator は名前 + 足)。strategy で依存 indicator が空なら NULL。 |
| `outcome_class` | `TEXT NOT NULL DEFAULT 'not_submitted' CHECK (outcome_class IN (...14 値...))`。ライフサイクル専用 (下表)。検出属性は入れない。 |
| `quality_flags` | `TEXT NOT NULL DEFAULT '[]'` の JSON 配列。検出属性を持ち、人間の決定後も消えない。許容要素は `family_detected` (配備済みと同族の別名と検出された) と `legacy_unlinked` (§2.2 の migration で承認と結べなかった旧行) の 2 つ。書き込み API が集合外を拒否する。 |

**`pair` の NULL 許可。** 候補の identity は `(mission_id, artifact_hash)` であり、`pair` は属性である。indicator の `config.yaml` は `pairs` を持たない (`docs/examples/plugins/rsi_indicator/config.yaml` は `kind`、`outputs`、`params` だけ) ので、indicator 候補の行は `pair` NULL で作る。strategy 候補は config の先頭 pair を入れる。現行の列は `pair TEXT NOT NULL` (`store/db.py:385`) なので、IQ-T0 の migration で table rebuild (新表を作って写し、旧表を落として rename) により NULL 許可に変える。DB に版番号の列は無く、migration は `init_db()` 内の冪等な shape migration なので、`PRAGMA table_info(candidate_archives)` で `pair` の `notnull` を見て未変更なら rebuild する (settings の `schema_version` とは別物)。test は fresh と migrated の列定義一致、既存 strategy 行の値不変、再 migration の冪等性を見る。全数 grep の結果、`pair` を読む既存コードで NULL により壊れる箇所は無い (`duplicate_metrics` は `backtest_runs.pair` を比較し kind が strategy 以外を先に除外する、表示と GC は pair を読まない)。IQ-T0 で変える実体は 4 つ: `store/db.py` の列定義、`candidate_archives.insert()` の `pair: str | None`、`tests/store/test_candidate_archives.py` の NULL round-trip、`tests/store/test_db_migrations.py` の rebuild 検査。sentinel 文字列は使わない。

行の `id` を**不変の候補 id** (`candidate_archive_id`) として使う。承認 payload は作成時に `candidate_archive_id` を入れる (§2.2 末尾の「最終 artifact の行」)。payload の作成後に変えない契約は変わらない。

**outcome_class の意味と書き手。** 14 値。初期値は `not_submitted`。値ごとに、書き込める関数と元の値が決まっている。

| 値 | 意味 | 書き手 (元の値 → この値) |
|---|---|---|
| `not_submitted` | 評価した、または作ったが提出していない (初期値) | 行の作成時 (`insert`) |
| `pending` | 承認依頼を提出済みで人間の決定待ち | 終端 tx の `settle` (`not_submitted` → `pending`) |
| `approved` | 人間が承認 | `apply_decision` (`pending` → `approved`) |
| `rejected_by_human` | 人間が却下 | `apply_decision` (`pending` → この値) |
| `expired` | 期限切れ | `apply_decision` (`pending` → この値) |
| `invalidated` | 無効化 | `apply_decision` (`pending` → この値) |
| `revoked` | 承認後の撤回 | `append_revocation` (`approved` → `revoked`、§2.2 の「撤回」) |
| `unprofitable` | 収益性の床で不合格 (holdout 床の不合格を含む。原因は区別しない)。内部状態で、agent 面には出さない | 終端 tx の `settle` (`not_submitted` → この値) |
| `insufficient_trades` | 取引数不足で評価不能。内部状態 | 同上 |
| `gate_failed` | plugin gate / strategy gate の不合格、比較基盤の不能、質検査の parse 失敗、その他の提出不能 | 同上 |
| `noop_copy` | 完全コピーで不合格 | 同上 |
| `duplicate_logic` | (b) で拒否 | 同上 |
| `duplicate_metrics` | (a) の成績完全一致で降格 | 同上 |
| `family_redirect` | (c) で降格 (`family_redirect` の有効化後のみ) | 同上 |

**許される遷移は 13 本だけ**: `not_submitted` → {`pending`, `unprofitable`, `insufficient_trades`, `gate_failed`, `noop_copy`, `duplicate_logic`, `duplicate_metrics`, `family_redirect`} (8 本、終端 tx の `settle`)、`pending` → {`approved`, `rejected_by_human`, `expired`, `invalidated`} (4 本、`apply_decision`)、`approved` → `revoked` (1 本、`append_revocation`)。それ以外の組 (自己遷移を含む 196 組中の 183 組) は書き込み API が拒否する。`outcome_class` を UPDATE する箇所は、migration (DDL 時の旧行の埋め) を除き、この 3 関数 (`candidate_archives.settle`、`approvals.apply_decision`、`approvals.append_revocation`) だけに限る (AC-14 で全数 pin)。

**検出属性。** `family_detected` は outcome ではない。(c) が配備済みの別名の同族を検出したとき、`family_redirect` の有効化前は降格せず提出を続け、`quality_flags` に `family_detected` を足す。提出後に `pending` → `approved` と進んでもフラグは残る。有効化後に降格するときは、outcome が `family_redirect` になり、フラグは足さない (outcome がそのことを示す)。§3.2 の指標と AC-11 はこの列を見る。

**最終 artifact の定義と最終 hash の規則。** 最終 artifact は「終端に至った時点の staging の bytes」である。`_run_plugin_gate` は pytest の実行前に `hashes_of` を一度取り (`:1643`)、実行後に取り直す (`:1706`) が、pytest の失敗 return (`:1680`) は取り直しの前に抜ける。candidate 自身の self-test が staging を書き換えて失敗すると、`:1643` の値は最終 bytes を表さない。そこで次の規則にする。(1) `run_gate_pytest` を呼んだ後に到達する**全ての return** (RuntimeError、pytest 不合格 `:1680`、収集本数不足、hash 変化 `:1706` 以降、合格を含む) は、return の直前に staging を `hashes_of` で再 hash し、その値を `_PluginGateVerdict` の最終 hash として持たせる。(2) pytest の実行前に抜ける return (source 検査、loader、`noop_copy`、`self_test_missing`) は staging が変わっていないので `:1643` の値でよい。(3) 再 hash が取れない (ファイルが消された等) ときは最終行を作らず、E8 を「なし」として扱う。行を作らないことは `not_submitted` の行が増えないだけで、候補の判定 (`gate_failed`) は変わらない。

**最終 artifact の行 (承認との結び)。** `artifact_hash` は backtest 時点の値 (`improve_loop.py:1305`) で、提出時 (承認 payload は `_run_plugin_gate` が `hashes_of` で取った値、`:1643`、`:1706`) の値は、agent が提出前に self-test を書き換えると異なり得る (`candidate_archives.py:59` の記述どおり)。そこで次の規則にする。

1. 提出する候補 (`gate_verdict.artifact_hash`) の行を、承認依頼を作る**直前**に必ず作る。同じ `(mission_id, artifact_hash)` の行が既にあれば (台帳の `INSERT OR IGNORE` が返す既存 id) それを再利用し、無ければ新しい行を作る。新しい行の snapshot は run_backtest と同じ手順 (tmp に書いて rename) で staging から作る。snapshot が作れなければ `archive_path` NULL の行にして `archive_failed` を activity に残す (行そのものは必ず作る)。新しい行の `metrics_json` は `{}`、`pair` は strategy なら config の先頭 pair、indicator なら NULL (成績は gate の行が別に持つ。新列に成績を持たない原則は変えない)。
2. 承認 payload に、その行の `id` を `candidate_archive_id` として入れる (`_finalize_success` の tx 内、`approval_payload = dict(approval_payload)` に `analysis_run_ids` を足す箇所と同じ位置、`improve_loop.py:2443`)。作成時に確定し、以後変えない。
3. 提出に至らない終端でも、判定した候補 (§2.2 の全終端写像表で「最終行あり」の終端) は同じ規則で行を作り、outcome を `settle` する。snapshot は作らない (`archive_path` NULL)。
4. `apply_decision` と `append_revocation` は、payload の `candidate_archive_id` で行を引く。ID が無い旧 payload は、後述の旧行の照合規則 (`resolve`) で引く。

**更新の規則。**

- `apply_decision` (`store/approvals.py:132-215`) は、approval 行の CAS (`status='pending'`、`rowcount==1`) が成立した**同じ tx の中で**、行の `outcome_class` を `pending` → 新値に更新する (CAS)。CAS が rowcount 0 なら archive も更新しない。
- 対象の行が見つからない (`resolve` が NULL、行が消えている、`outcome_class` が `pending` でない) ときは、approval の決定は commit し、archive の更新だけを skip して `warning` を残す。決定を archive の都合で失敗させない。したがって保証は「行があれば同じ tx で更新される。行が無ければ決定だけが残る」で、「片方だけ残らない」とは言わない。
- 成績値は新列に持たせない。既存の `metrics_json` は質検査と gate の母集団として今まで通り使う。

**撤回 (`revoked`) の更新点。** 撤回は元の approval 行を `apply_decision` で更新する経路ではなく、`approvals.append_revocation` (`store/approvals.py:98`) が `rejected` の revocation 行を**追加**する経路である。呼び出し元は 2 つで、どちらも `BEGIN IMMEDIATE` の中から `commit=False` で呼ぶ。

| 経路 | 呼び出し | tx |
|---|---|---|
| 配備中の版の無効化 (`disable --revoke`、`rollback --revoke`) | `deploy_ops.record_intent` (`plugin/deploy_ops.py:301-327`、`append_revocation` は `:309`)。呼び出し元は `deploy_disable.disable_plugin` (`:198-206`)、`deploy_rollback` (`:526`) | `record_intent` の `BEGIN IMMEDIATE` (`:304`) |
| 履歴の版の撤回 (`revoke_version`) | `plugin/deploy_disable.py:263`。同じ tx で `plugin_deploy_ops.insert_done_revoke` (`store/plugin_deploy_ops.py:60`) が `done` の op を記録する | `deploy_disable.py:262` の `BEGIN IMMEDIATE` |

archive の更新は、2 経路の共通の入口である `append_revocation` の中で行う。revocation 行の INSERT と**同じ tx** で、`revokes_approval_id` が指す approval の payload から行を引き (`candidate_archive_id`、無ければ `resolve`)、`approved` → `revoked` に CAS で更新する。`append_revocation` が `None` を返す (最新の決定が既に `rejected`) ときは更新しない。同一の版 (name + content_hash) に承認が複数あるときは、撤回の対象に指された approval の行だけを `revoked` にし、より古い承認の行は決定時の事実のまま `approved` で残す。`retire` (`revoke=None`) は承認を撤回しないので outcome は変わらない。

**全終端の写像表。** 改善 loop の `commit()` (`improve_loop.py:2538`) は、1 mission を次のどれかで終える。行の紐付けは「その mission の `run_backtest` で作った全行は初期値 `not_submitted` のまま。最終行 (提出した、または判定した候補の行) だけが `settle` で値を持つ」。最終行の hash は、判定時に `_PluginGateVerdict` / `_QualityVerdict` が持つ最終 artifact の hash から引き、**staging の削除より前**に取る (`_finalize_gate_failed` は先頭で staging を消す、`:3436`)。`report_failed` など mission_outcome が何であっても、候補の outcome は mission_outcome とは独立に決まる。

| # | 分岐 (呼び出し点 → 終端関数) | 最終行 | 最終行の outcome | 備考 |
|---|---|---|---|---|
| E1 | `result.status != "completed"` (`:2582` → `_finalize_failed_mission` `:3093`) | なし | — | 候補を判定していない。全行 `not_submitted` |
| E2 | `_inspect_output` 不合格 (`:2596` → `_finalize_output_invalid` `:3223`) | なし | — | artifact が確定しない |
| E3 | 選択に負けた (`:2603` → `_finalize_loser` `:3270`) | なし | — | 候補を作っていない |
| E4 | `recovered` の report を observation に降格 (`:2638` → `_finalize_report_or_observation` `:2955`) | なし | — | |
| E5 | agent の `artifact` が observation / report (`:2845`、`:2881` → 同 `:2955`) | なし | — | 提出していない |
| E6 | report の書き込み失敗 (`:2845` → `_prepare_report_if_applicable` `:2894`、`report_failed`) | なし | — | |
| E7 | plugin gate が `noop_copy` で不合格 (`:2651`、`:2659` → `_finalize_gate_failed` `:3391`) | あり (pytest 実行前の return なので `:1643` の hash) | `noop_copy` | `_PluginGateVerdict` に失敗の種別を持たせる (理由文字列の前方一致で判定しない) |
| E8 | plugin gate のその他の不合格 (`:1637-1706` の各 return。snapshot 検査、source 検査、loader、構文、self-test 欠如、pytest、test 本数、hash 変化) | `:1643` 以降の return は あり (pytest 実行後の return は直前に再 hash した値、実行前の return は `:1643` の値、前掲の最終 hash の規則)。`:1636-1641` の snapshot 検査不合格と、再 hash が取れない場合は なし | `gate_failed` | 同上 |
| E9 | loader が None / `max_bars` / `kind` 非対応 / `outputs` 必須 / `indicator_unresolved` (`:2670`、`:2679`、`:2687`、`:2699`、`:2717`) | あり | `gate_failed` | |
| E10 | `NoHistoryError` (`:2750` → `:2753`)、`unevaluable_kind == "history_source_unconfigured"` (`plugin/strategy_gate.py:256-261`、`:2763`) | あり | `gate_failed` | 比較基盤の不能 |
| E11 | 取引数不足 `unevaluable_kind == "insufficient_trades"` (`strategy_gate.py:366-369`、`:2763`) | あり | `insufficient_trades` | |
| E12 | 収益性の床の不合格 (`strategy_verdict.floor_reason`、`:2773-2788` → `_finalize_gate_failed(mission_outcome="unprofitable")`) | あり | `unprofitable` | in-sample 段と holdout 段のどちらでも同じ |
| E13 | sandbox 不能 (`:2742` → `_finalize_gate_sandbox_unavailable` `:3181`) | なし | — | 環境側の失敗で候補は判定されない。backlog は `open` に戻り再挑戦できる |
| E14 | 質検査 (§2.3 の段 L、F、M) の不合格 (`_finalize_success` が `_QualityVerdict` を返し、`:2854` → `:2869` → `_finalize_report_or_observation`) | あり | `duplicate_logic` / `family_redirect` / `duplicate_metrics` / `gate_failed` (質検査自身の parse 失敗) | 内側 tx は rollback 済みなので何も永続化しておらず、この終端 tx が行を作って `settle` する |
| E15 | 提出 (`:2854` → `_finalize_success` `:2368`) | あり (提出直前に作る、上記規則) | `pending` | 質検査が `family_detected` を検出していれば `quality_flags` にも足す |
| E16 | `_finalize_success` が例外 (`:2524` → `_compensate_tx2_failure` `:2256`) | なし | — | approval の作成が rollback される。台帳は `commit_failed` で保存、全行 `not_submitted` |
| E17 | `commit()` が例外 (`compensate_commit_failure` `:669`) | なし | — | 同上 |
| E18a | prepare の失敗 (`_compensate_prepare_failure` `:584`) | なし | — | 台帳が無い (行を作らない) |
| E18b | 定期 slot の launch 失敗 (`ImproveSupervisor` の slot 経路で `runner.run()` が例外、`core/improve_supervisor.py:275` → `compensate_launch_failure` `:639`) | なし | — | 同上。`slot_key` を持つ補償 |
| E18c | 手動 mission の launch 失敗 (`submit_manual` の `runner.run()` が例外、`improve_supervisor.py:125`、`:135`) | なし | — | 現行は `runner.run()` が try の外で補償を呼ばず、mission / run が非終端のまま残り得る。本 spec で `compensate_launch_failure` の下に置く (例外は握らず re-raise)。`_compensate_prepare_failure` は `slot_key=None` を扱えるので手動でも mission / run だけを終端できる |

E1〜E6、E13、E16、E17、E18a〜E18c は最終行を持たず、その mission の全行が `not_submitted` のまま残る。`_persist_ledger_in_tx` が失敗して行が作られなかったとき (`ledger_persist_failed`、`:2219`) は、`settle` を skip して warning を残す (承認の提出は E15 の経路で `RuntimeError` になり E16 へ進む)。

**migration** (`store/db.py:1516-1524` の `origin_*` 列と同じ ALTER ADD COLUMN の形で 4 列を足し、`pair` の NULL 許可は table rebuild で行う。既存行の値は rebuild で変えない)。旧行の埋め方:

- `logic_hash` / `family_key`: archive の現物 (`plugins/_archive/<id>/<hash>/` の `plugin.py` と `config.yaml`) から migration で再計算する。現物が無い・parse できない行は NULL にし、**NULL の行は比較対象から外す** (同じ NULL 同士の一致とは見なさない)。再計算できた旧行は (b) の母集団に入る。
- `outcome_class` の照合 (`resolve`)。各 `approval_requests` (kind=`plugin`、`revokes_approval_id` を持つ revocation 行は除く) の payload から次の順で archive 行を探す。(1) `(mission_id, artifact_hash)` の完全一致。(2) 無ければ `(mission_id, content_hash)` で一致する行が**ちょうど 1 行**のときだけ採用 (`find_by_mission_content` の「最新を返す」規約は使わない。曖昧な行を誤って承認済みにしないため)。(3) それ以外は**結ばない**。結べた行は approval の status に応じて `pending` / `approved` / `rejected_by_human` / `expired` / `invalidated` を設定する (遷移表の外で、migration だけが直接設定してよい)。`revokes_approval_id` が指す approval の行は `revoked`。
- 結べなかった approval の mission の行は、`outcome_class` を `not_submitted` のまま `quality_flags` に `legacy_unlinked` を足し、§3.2 の指標の分母と分子から外す。approval に結ばれない行 (提出していない候補) は `not_submitted`。
- 新規の承認は `candidate_archive_id` を持つので (1) は常に成立する。旧 payload のまま migration 後に決定される pending は、`apply_decision` が同じ `resolve` を使って引く。

`candidate_archives` は `SELECT *` を decode する (`candidate_archives.py:10-13,39-42`) ので、追加列は既存の read model に即座に現れる。「旧 fixture と byte 一致 test」は、**既存列の値と payload bytes が一致し、schema の列集合の差が追加 4 列だけであり、`pair` の NOT NULL が外れる以外に既存列の定義が変わらない**ことを指す (DB ファイル全体の byte 一致ではない。ALTER と table rebuild の後には成立しない)。

**agent 面の投影 (固定)。** agent に出る分類は、内部の outcome とは別の固定の写像で決める。`unprofitable` は収益性の床で落ちた**内部状態**であり、agent 面には出さない。

| 内部の状態 | agent 面の表示 (履歴の `last_result`、`origin` 列、降格の通知) |
|---|---|
| `unprofitable` / `insufficient_trades` / 比較不能 (E10) / その他の非提出 | `not_submitted` |
| `duplicate_logic` / `duplicate_metrics` | `duplicate` (他候補の hash、pair、成績は出さない) |
| `family_redirect` | `family_redirect:<配備済みの戦略名>` (名前と構成だけ。優劣は含まない) |
| `noop_copy` / その他の gate 不合格 | 現行の `gate_failed:<固定理由>` (agent 自身の候補の機械的な理由) |

固定文は「理由非公開。同一候補を再提出しない」だけを添える。現行は `insufficient_trades:<n>` (`:2763`) が `last_result` にそのまま入り、`duplicate_metrics_of:<content_hash> pair=<pair>` (`:2877`) が降格の `last_result` に入る。どちらも上の写像に置き換え、細部は activity (運用者向け) の別イベント (`quality_demoted`) にだけ残す。`origin` 列 (`improve_loop.py:799`、`origin_outcome == 'unprofitable'` のとき `unprofitable` と表示) は `not_submitted` と表示し、`origin_outcome` の書き込み (`:3527`) は `unprofitable` と `insufficient_trades` のどちらでも `not_submitted` を書く。これで「収益性か、標本数か」の区別は agent に渡らない。

**candidate 単位の履歴 (本版では採らない、§8.2)。** agent 面には mission 単位の `last_result` だけを出す。後続で有効にする場合は上の写像の 5 分類だけを使う。並びは作成順のみで、成績・結果による sort / filter は禁止。成績、holdout、人間の reason、hash は出さない。既存の mission 単位 `last_result` との差は、1 件ごとに対応が取れること。この増幅は §7 の残余リスク。

### 2.3 B: 重複の定義と質検査

実測 (読み取りのみ)。

- #24 `rsi_bb_reversion_200` と #25 `rsi6_bb200_rr2`: `plugin.py` を数値リテラルを落として AST hash にすると**一致しない** (`8284a08a4f66` / `623b25583455`)。#25 は SL/TP の既定値に加えて、変数 `prev` の導入と `vals` リストへの要素追加がある。つまりロジック等価 (b) だけでは #25 型は捕まらない。config の依存 indicator 集合は両方 `{bollinger, rsi}`、足 1h、pair USDJPY で一致するので、仮説族 (c) が捕まえる。
- #21 と #22 (SMA クロス系): **両方とも `config.yaml` で `sma` 依存を宣言している** (`plugins/_archive/85/395e.../config.yaml:7-17`、`plugins/_archive/86/7323.../config.yaml:7-17`)。仮説族は両方 `{sma}, 1h, USDJPY` で一致し、**捕まえるのは (c)**。数値リテラルだけを落とした AST hash は、rationale 文字列が違うため**一致しない** (`c7246962080d...` / `d6d9e64ef65...`)。
- `docs/examples/plugins/sma_cross` は `indicators:` を持たず自前計算する (`config.yaml:1-10`)。この型は `family_key` が NULL で (c) は効かない。rationale の変更や無害な AST 変更で hash も避けられるため、**穴が残る** (後述)。

**ロジック hash の正規化規則。** `plugin/noop_gate.py:13` の `normalized_plugin_ast` (docstring を落とした AST) を拡張し、次を落とす。(1) 数値リテラル、(2) docstring、(3) 非意味的な文字列、すなわち rationale / reason に当たる keyword 引数・dict 値の文字列リテラル (f-string を含む)。列名など判定に効く文字列は残す。規則を適用した後の #21 / #22 の hash が一致するかは IQ-T2 で実測して fixture に固定する。一致しなくても #21 / #22 は (c) で捕まる。

**既知の未解決穴。** indicator を使わない自前計算の戦略 (`sma_cross` 型) は、(c) が効かず、(b) も文構造を変えれば避けられる。本 spec はこの穴を塞がない。§11 の後続条件 (発火時刻の Jaccard、または自前計算の戦略への構造的な近さの判定) に置き、prompt の「配備済みの仮説族の一覧」にも出さない (族が無いため)。

質検査は、現物では 2 つの別の場所で動く。一つの分類器ではないので、「最終分類の優先順位」と「実行位置」を別の表で定める。

**最終分類の優先順位** (上から順に当たったもので最終 outcome が決まる。各段は当たった時点で終端し、後ろの段は実行されない。したがって優先順位は実行位置の順に一致する)。

| 順 | 段 | 当たったとき |
|---|---|---|
| 0 | 候補自身の parse (`plugin.py` の AST、`config.yaml` の YAML、source 検査、loader) | `gate_failed` |
| 1 | `noop_copy`: code / config が配備済み・example と完全一致 | `noop_copy`。pin 再ロック (`is_relock_transition`) は現行の例外のまま |
| 2 | その他の plugin gate (self-test の欠如・不足、pytest、hash 変化) | `gate_failed` |
| 3 | strategy gate: 比較基盤の不能 | `gate_failed` |
| 4 | strategy gate: 取引数不足 | `insufficient_trades` |
| 5 | strategy gate: 収益性の床 | `unprofitable` |
| 6 | 段 L: (b) `logic_hash` 一致 | 下の 2 ケース |
| 7 | 段 F: (c) `family_key` 一致 | 下の 2 ケース |
| 8 | 段 M: (a) 成績完全一致 `duplicate_metrics_of` | `duplicate_metrics` |
| 9 | 全部通った | 提出 (`pending`) |

収益性・評価可能性 (順 3〜5) が重複 (順 6〜8) より優先する。床で落ちる候補は重複かどうかを問わず提出されず、agent 面では同じ `not_submitted` に畳まれる。§3.2 の重複降格率は「床を通った候補の中の重複率」である。

順 6 の 2 ケース: 一致先が**配備済みの同名旧版**なら「更新」として許し、次の順へ進む。一致先が別名の配備済み・却下済み・降格済み・example なら `duplicate_logic` で拒否する。母集団は `candidate_archives` と `plugins/` と example。**母集団に同じ mission の行は入らない** (行は終端 tx で作るので、質検査の時点では他の mission の行だけがある)。同じ mission の途中の反復と自分自身が一致して拒否される誤りが起きない。

順 7 の 2 ケース: 一致先が配備済みの**別名**の戦略なら、approval-content の AC-T1/T2 完了前は `quality_flags` に `family_detected` を足すだけで降格しない。完了後は `family_redirect` で降格し、「同族は既存名で更新として出し直す」と ledger に出す。**同名**なら更新として通る。母集団は**配備済みの同 kind の plugin だけ** (strategy は strategy と、indicator は indicator と比べる。却下や降格された族は含めない)。

この順序で、同名の param 更新 (config 値だけが違う) は、順 1 は一般更新では当たらず (完全一致でない)、順 6 は同名旧版との一致で許され、順 7 は同名で通り、順 8 の成績完全一致だけが止める。これは一般更新の経路であり、**pin 再ロック (`is_relock_transition`、`plugin/resolve.py:291-322`) とは別のケース**。再ロックは古い indicator pin を新しい pin に貼り直す専用例外で、順 1 と順 8 の例外としてのみ現行どおり働く。一般更新が再ロックの例外を流用することはない。

**実行位置と、行への紐付け。**

| 段 | 実行位置 | tx | 最終行 (`candidate_archive_id`) への書き込み |
|---|---|---|---|
| 0 (plugin gate 内の parse 系) | `_run_plugin_gate` (`improve_loop.py:1634-1656`)。呼び出しは `:2651` | tx の外 | `_PluginGateVerdict` が最終 hash を**全ての失敗 return に持たせる**。pytest 実行前の return は `:1643` の値、`run_gate_pytest` 後の return は直前に staging を再 hash した値 (§2.2 の最終 hash の規則)。終端 (`:2659` → `_finalize_gate_failed`) の tx が最終行を作り `gate_failed` を `settle`。`:1636-1641` (snapshot 検査) の失敗と再 hash 不能は hash が取れず行を作らない (E8) |
| 1 (`noop_copy`) | 同上、`find_noop_copy` の呼び出し `:1658`。早期に終端 | tx の外 | 同上で `noop_copy` |
| 2 (その他の plugin gate) | 同上 `:1669-1713` | tx の外 | 同上で `gate_failed` |
| 3〜5 (strategy gate) | `_run_strategy_gate` (`:1717`)。呼び出し `:2725`、判定 `:2763`、`:2773` | tx の外 | `gate_verdict.artifact_hash` で最終行を作り `settle` (E9〜E12) |
| 6〜8 の入口 | `_finalize_success` (`:2368`) の `BEGIN IMMEDIATE` (`:2402`) の中。新設の `_check_candidate_quality` を、現行の `_check_duplicate_metrics_for_approval` の呼び出し (`:2423`) の位置に置き、段 L → 段 F → 段 M の順で呼ぶ。`duplicate_metrics` の本体は `:2853-2854` の `_finalize_success` 呼び出しから入る既存の関数 | tx の内 (`BEGIN IMMEDIATE`) | 当たったら内側 tx を rollback し、`_QualityVerdict` (outcome、最終 hash) を返す。呼び出し元 (`commit()` `:2854-2880`) が `_finalize_report_or_observation` へ再ルートし、その終端 tx が最終行を作って `settle` (E14)。通過したときは `family_detected` を `flags` として持ち、提出の経路で最終行を作って `pending` を `settle` し、`quality_flags` を書く (E15) |
| 段 L・F の parse 失敗 (到達しないはずだが fail closed) | 同上 | tx の内 | `gate_failed` として E14 の経路に載せる (§6) |

段 L・F・M を `_finalize_success` の `BEGIN IMMEDIATE` の内側に置く理由は、現行の (a) と同じ。並行 slot (`settings.improve.parallel > 1`) の check-then-insert の競合を、書き込みロックの直列化で閉じる。

(c) の母集団を配備済みだけにする理由: 却下された候補の族は、却下の理由が param や出口にあるかもしれず、族ごと閉じると探索が狭まる。(b) の母集団に却下を含めるのは、リテラル以外が同一な別名の再実装は理由を問わず無駄だから。(c) は拒否でなく降格にし、同名の出し直しを案内する。

**依存する将来仕様。** 「同名で出し直すと承認画面の分類が `updated` となり、前版との params 差分と 3 ファイルの diff が出る」は、現行 main の `ops/service.py:576-604` の `approval_detail` にまだ無い (`change`・分類・params 差分・3 ファイル diff はいずれも未実装)。approval-content spec (`2026-10-19-approval-content-design.md` §10.2) の AC-T1 (API + CLI + web) と AC-T2 (Discord 承認投稿) が完了して初めて成立する。その前に `family_redirect` を有効化すると、人間は差分なしで更新を裁くことになるので、**有効化は AC-T1 と AC-T2 の両方の merge 後**とする (§10 の IQ-T2b)。成績差の並記は approval-content が見送っているので本 spec でも出さない (§11)。

### 2.4 C: 目標の定義と伝え方

現状、目標に当たる文言は prompt に無い (`improve_mission.md:3-4` は「改善」とだけ書く)。数値で見えるのは `{profitability_floor_rule}` の `pf >= 0.0` (`improve_loop.py:878-902`) だけで、これは今の設定ではほぼゼロ。さらに、次の文が param 違いの亜種 (#25) を作る動機になっている。

- 規律 2: 「必ず実質的な変更を含めること」(`improve_mission.md:71-72`)
- 規律 3: 「試したパラメータと得られた pf / avg_r を具体的に書いて次回への申し送りにしてください」(`:74-76`)。param と成績値を自由文で backlog に戻す規則であり、数値を最適化の目標にさせ、成績が backlog に再記録される。
- 規律 5: 「試すなら明確にパラメータを変え、その理由を `selection_rationale` に書いてください」(`:87-88`)。param 変更を直接促す文はここである (v0.1 は規律 3 と取り違えていた)。
- floor 文 (`improve_loop.py:894-902`): 「予算内でパラメータ・フィルタ・エントリ条件を変えて `run_backtest` をやり直し」。

「バックテストで損をしない戦略を増やす」だけでは、agent が観測できる唯一の評価軸が PF になり、より高い PF を探す動機になる。そこで目的を仮説の違いと検証可能性で肯定形に書き、収益指標を床に位置づける (§0 の目的文)。

| 層 | 置くもの |
|---|---|
| prompt の目的節 (新規、冒頭) | §0 の目的文。「全体収益」「PF 最大化」の語は使わない。「標本不足」「評価可能」「holdout」も書かない。 |
| 規律 2 の書き換え | 「完全コピーは不合格。変更は仮説の違いとして説明できること」。 |
| 規律 3 の書き換え | 観察には**試した仮説と確認できた事実**を書く。pf / avg_r を含む成績値は backlog の自由文に書かない。 |
| 規律 5 の書き換え | 「`not_submitted` の課題・note は、その mission の候補が提出されなかったことを示す。理由非公開。同一候補を再提出しない。再挑戦は別の仮説として扱う」。パラメータを変えて試す指示と、原因の推測を促す文は外す。`origin` 列の表示も `not_submitted` にする (§2.2 の投影)。 |
| `{profitability_floor_rule}` (floor 文) | 床の値 (`pf >= ...`) は従来どおり (D で値が戻る)。「パラメータ・フィルタ・エントリ条件を変えて再実行」の文を外し、「満たさない候補は提出しない。予算内で満たせなければ提出せず、試した仮説を `observation` に書く」に改める。holdout の条件は出さない (`audience="agent"`、`strategy_gate.floor_rule_text`)。現行の文中の「`unprofitable` として observation に落とします」も `not_submitted` の語に改める (§2.2 の投影)。 |
| 配備済みの仮説族の一覧 | 名前と構成 (indicator 集合・足・pair) だけ。成績は含まない。「param の調整は同名の更新として扱われる」と添える。 |
| directives.md | 利用者が書く欄として変更しない。目的節は製品既定、利用者の方針はその上に重なる。 |

目的の「異なる市場仮説」は、gate がまだ測らない (発火時刻の Jaccard は §11)。測らない目標を prompt に書くのは、agent を誘導するだけで裁かない。質検査では (b)(c) の構造的な近さだけで裁く。これが本 spec の限界であり、受容した上で目的文を採る (§8.1 問 3)。

### 2.5 D: 床の戻し方

`config/settings.yaml.example` は既に `min_pf: 1.0` / `require_positive_avg_r: true` / `require_holdout_evaluable: false` で、コード既定 (`config.py:327-329`) と一致する。案 A の一時設定は gitignore (`.gitignore:14`) の `config/settings.yaml` にだけあるので、**戻す操作は commit に載らず、運用手順になる**。daemon は起動時に settings を読む (`service.py:2425`、`:1187`) ので、反映には再起動が要る。IQ-T3 (§10) は次を一組の手順にする。手順 0 は床を戻す前に行う読み取り専用の確認で、手順 1〜6 の前提になる。

0. **保存済み backtest の再評価 (読み取り専用、床を戻す前)。** 承認済みの #21 / #24 / #25 の承認 payload に保存された backtest 結果 (in-sample と holdout の成績、取引数) を、新基準 (`min_pf` 1.0、`require_positive_avg_r` true、`require_holdout_evaluable` true、標本下限 30) に照らして判定し、承認ごとに 1 行の表を人間に提示する。出力の形は 承認 id / 戦略名と版 / in-sample の pf と avg_r / holdout の pf と取引数 / 判定 (合格、または失敗理由 = `PF 不足` / `holdout 評価不能` / その両方) とする。この表は運用者向けで、agent 面には出さない (§7)。実装は既存の床判定関数 (`_check_profitability_floor`) を保存済みの値に適用する読み取り専用の確認コマンドまたは手順書のコマンド列とし、DB・settings・配備のいずれにも書かない。新しい自動経路は作らない。
   提示を見て、人間が #21 / #24 / #25 のそれぞれを keep (何もしない) か disable かを**個別に明示決定**する。自動解除はしない。disable は既存の人間操作 `afx plugin disable <名前> --revoke` で行い、その決定の記録は既存の revocation 経路 (`append_revocation` の revocation 行と、同 tx での候補台帳の `revoked` 更新、§2.2) に残る。keep は操作を伴わないので、決定を運用手順書の実施記録に書く (新しい表は作らない)。再評価は情報の提示であり、結果が不合格でも自動では何も変わらない。
1. 3 値 (`min_pf` 1.0、`require_positive_avg_r` true、`require_holdout_evaluable` true) を**一組で**変更する。
2. config validation (`afx check` 系) を通す。
3. daemon を再起動する。
4. 起動後に、daemon が保持する settings の active snapshot で 3 値を確認する (確認の面は運用手順書で確定する)。
5. 次の承認 payload の `profitability_floor` に新値が入っていることを確認する。
6. 戻す (案 A に戻す) 場合も、同じ 3 値を一組で変更し、1〜5 を繰り返す。

`require_holdout_evaluable` の既定をコードで true にするかは別問題。既定を変えると他の利用者の挙動が変わる。本機の `settings.yaml` だけで true にする (決定、§8.1 問 5)。schema 変更はなく、example のコメント更新だけで足りる。

挙動の確認 (運用者向け、agent には見せない)。`require_holdout_evaluable` が true で holdout が評価不能だと、評価不能の判定が `_check_profitability_floor` の `unprofitable` 判定に入る (`plugin/strategy_gate.py:84-88`)。つまり #24 / #25 型 (holdout 4 trades) は、内部では `unprofitable` で observation になる。agent 面では、取引数不足・比較不能・その他の非提出と一緒に中立な `not_submitted` に畳む (§2.2 の投影)。`unprofitable` という語は収益性が悪いと読めるため、標本不足の候補に付けると別仮説への誘導を誤らせる。ラベルを分けると holdout の標本数という 1 bit を渡すので、分けずに 1 つの中立な語に畳む。また「標本不足も同じ扱い」のような説明も agent 面には書かない (条件の存在を知らせるため)。この説明は運用文書 (運用者向け) にだけ書く。

## 3. 観測と判定

### 3.1 固定事項

質検査と prompt が共通に守る 4 つ。

| # | 固定事項 | 反映先 |
|---|---|---|
| 1 | 新規候補と既存更新を区別する。同名 (配備済みの名前) は更新、別名は新規。仮説族が配備済みと同じ別名は新規と数えない | 質検査 (§2.3)、新規仮説率 |
| 2 | パラメータ探索を新規仮説と数えない。param だけが違う同族は新規仮説率に入れない | 新規仮説率、規律 5 |
| 3 | agent は PF / avg_r を backlog の自由文に再記録しない | 規律 3、出力検査の方針 (§2.4) |
| 4 | 提出されなかった理由 (収益性か、標本数か、比較不能か) を、agent に推測させる説明をしない。agent 面は `not_submitted` の 1 語に畳む | 規律 5、floor 文、`origin` 列、履歴の節、§7 |

### 3.2 指標

| 指標 | 定義 | 取得元 |
|---|---|---|
| 新規仮説率 | 直近 6 周のうち、提出された最終行 (`outcome_class` が `pending` / `approved` / `rejected_by_human` / `expired` / `invalidated` / `revoked`) があり、かつその行の `quality_flags` に `family_detected` が無い mission の割合 (分母は mission。`legacy_unlinked` の行は分母分子から外す) | `candidate_archives` の `outcome_class` と `quality_flags` |
| update 扱いの件数 | `outcome_class = family_redirect` の行と `quality_flags` に `family_detected` を持つ行の合計、および同名の出し直しで `updated` となった承認の件数 | 同上と `approval_requests` |
| 承認率 | 提出された承認依頼のうち approved の割合 | `approval_requests` |
| 重複降格率 | 直近 6 周のうち、最終行の `outcome_class` が `duplicate_logic` / `duplicate_metrics` / `family_redirect` / `noop_copy` である、または `quality_flags` に `family_detected` を持つ mission の割合 (基準: 現状 6 周中 3 回、分母は mission)。収益性・評価可能性で落ちた候補 (§2.3 の順 3〜5) は先に確定するので、この率は「床を通った候補の中の重複率」 | 同上 |

観測は統計的な合否にせず、次の 2 層で判定する (§8.1 問 7)。戻した床で候補が一件も出ない状況 (床が厳しすぎる) も、この指標で見る。

**必須の不変条件** (満たさなければ観測期間の判定に進まない):

1. 台帳の分類が全件正しい。観測期間中の全 mission について、`candidate_archives` の `outcome_class` と `quality_flags` が、§2.2 の終端写像と §2.3 の優先順位から期待される値と一致する (運用者が抜き取りでなく全件を確認する)。
2. 自己書き戻しの再発が 0。`discoveries` 経由で open になった backlog 行が 0 (AC-1、AC-3 の観測を運用でも確認する)。

**方向性指標** (週次の有効 mission 6 回を 1 期として数える。有効 mission = 定期 slot の mission のうち、E1、E13、E16、E17、E18a〜E18c で終わらず、候補が判定に至ったもの):

1. 同族の重複試行が 1 回以下。最終行の `outcome_class` が `duplicate_logic` / `duplicate_metrics` / `family_redirect` / `noop_copy` である、または `quality_flags` に `family_detected` を持つ mission の回数 (上の重複降格率の分子と同じ)。
2. **新規候補**が 2 本以上。「新規候補」は次の 3 条件をすべて満たす最終行に固定する。(i) **agent 発**: 定期 slot の mission で、agent 自身が `selected.idea` で宣言した仮説である (ユーザーが `afx ctl backlog add` で追加した task を選んだ mission と、手動 mission は数えない)。(ii) **新規仮説族**: `quality_flags` に `family_detected` が無く、`outcome_class` が `family_redirect` でもない。(iii) **提出到達**: `outcome_class` が `pending` / `approved` / `rejected_by_human` / `expired` / `invalidated` / `revoked` のいずれか。承認数は条件にしない。

**観測開始の条件。** mission 出力の保存修復 (チケット `improve-mission-output-not-persisted`) が完了していること。現状は `missions.output_json` が保存されておらず、起票者・仮説族・最終分類を後から再現できないため、この修復なしには方向性指標を判定できない。

## 4. 責務の分離

| 層 | 責務 |
|---|---|
| 親 (決定論) | 候補台帳の記録と、`apply_decision` と同じ tx での更新、`logic_hash` と `family_key` の計算、(b)(c) の判定、床の判定、prompt の組み立て (目的節、配備済みの族の一覧、任意の履歴の節)。 |
| agent | 仮説の宣言 (`selected.idea`。agent が新規 task を作れる唯一の経路)、候補の実装、未試行の idea / 観察した fact の note。`discoveries` は open を作らない。親が記録する欄には書けない。 |
| 人間 | backlog の task 追加と昇格、承認、directives.md、床の値。 |

## 5. 設定

新しい設定キーは作らない。`improve.max_new_backlog_per_mission` の既定は 20 のままにし、`discoveries` の入力上限は別定数 5 を使う (§2.1。設定化するかは §8.2)。`family_redirect` の有効化は設定キーでなくコード上の切替で、IQ-T2 と approval-content の AC-T1・AC-T2 の merge 後の task (IQ-T2b) で行う。`require_holdout_evaluable` はコード既定を変えない。

## 6. 例外と失敗

「検査不能」を 3 つに分け、新設した構造重複防御について fail-open にしない。

| 失敗 | 扱い |
|---|---|
| 候補自身の parse 失敗 (`plugin.py` の AST、`config.yaml` の YAML) | 候補を**拒否** (`gate_failed`)。(b)(c) を skip して通さない。plugin gate (§2.3 の段 0) で落ちるのが通常で、段 L・F の hash 計算で落ちる場合 (通常は到達しない) も内側 tx を rollback して E14 の終端に載せる |
| 比較対象 1 行の破損 (その行の `logic_hash` / 現物が使えない) | warning を activity に残し、**その行だけ skip** して他の行と比較する |
| 比較基盤が全部使えない (母集団を一件も組み立てられない、DB 読み取りの失敗) | 内側 tx を rollback し `gate_failed` にして人間に通知する (E14)。意図的な fail-open にはしない |

(a) の `duplicate_metrics_of` は従来どおり動く。台帳の記録失敗は mission の裁定を落とさず、`improve_commit_failed` 系の activity に残す (既存の `backlog_note_failed` と同じ扱い、`improve_loop.py:3168-3172`)。

## 7. 遮断と秘密

絶対制約。

- 改善 agent に holdout の数値・条件、人間の reason、承認画面の要約を渡す経路を新設しない。agent 面 (prompt、候補履歴、activity の agent 投影、降格の通知文) に、「標本不足」「評価可能」「30 trades」「holdout_not_evaluable」とその言い換えを出さない。収益性・取引数不足・比較不能の非提出は、中立な `not_submitted` の 1 語に畳み、`unprofitable` / `insufficient_trades` は agent 面に出さない (§2.2 の投影)。降格の通知は固定文言だけで、他候補の hash、pair、成績を含めない (現行の `duplicate_metrics_of:<content_hash> pair=<pair>` と `insufficient_trades:<n>` は置き換える)。
- 候補台帳の新列は成績を持たない。履歴の節 (任意) は名前・仮説族・§2.2 の投影の 5 分類だけで、`rejected_by_human` は既存の `last_result` の固定文言 (`store/backlog.py` の `_OUTCOME_TABLE`) と同じ 1 bit に限る。
- gate は決定論的。床は config で緩められるが、kill switch と同じく drawdown 系は config で無効化できない (本 spec は drawdown 系に触れない)。
- 生成コードは `plugins/` だけ。本体コードを変更する経路は作らない。
- 親が計算する `logic_hash` / `family_key` は staging の bytes から作るだけで、agent の出力を信用しない。

遮断 8 との整合の確認観点: (1) 非提出の理由の区別 (収益性・標本数・比較不能) を agent に渡さない。現行の `unprofitable` 1 bit と `insufficient_trades:<n>` も `not_submitted` に畳み、新しい bit を作る分類 (`holdout_not_evaluable` の独立ラベルなど) は採らない。(2) 「同族は既存名で更新」の案内は、配備済みの戦略名と構成が分かるだけで、成績や前版との優劣を含まない。(3) 目的節は holdout の条件を述べない。

**残余リスク (累積推定)。** 履歴を candidate 単位で出すと、名前・仮説族・分類を連続で対応付けられるため、agent は一要因ずつ変えた候補から次を推定できる: 人間が好む・嫌う仮説族や更新形、in-sample で合格が分かっている候補の `not_submitted` から「採用段のどこかで落ちた」こと、発火頻度を変えた反復から評価境界のおおよその位置。数値そのものは漏れないが、既存の 1 bit が検索しやすくなる。緩和は作成順のみの並び、sort / filter の禁止、原因を区別しない 1 語への畳み込み。完全には消えないので、candidate 単位の履歴は当面採らない (mission 単位の `last_result` で足りる。§8.2 で固定)。

## 8. 裁定の記録

### 8.1 ユーザー裁定 (2026-10-19)

7 問すべてをユーザーが裁定した。問 6 と問 7 は第二意見を受けて当初の推奨を改め、下表の決定になった。

| # | 問い | 決定 | 日付 | 根拠 |
|---|---|---|---|---|
| 1 | agent の task 供給権限 | (a) `discoveries` の task 起票は廃止し note のみ。新規 task の唯一の経路は `selected.idea` | 2026-10-19 | `selected.idea` はその場で選択・実行され、未選択 task を量産する `discoveries` とは故障モードが異なる。昇格は人間。 |
| 2 | 別名で仮説族が配備済みと同じ候補の扱い | 既存名の更新として再提出させる。有効化は IQ-T2 + AC-T1 + AC-T2 の後 (§8.2、IQ-T2b) | 2026-10-19 | 別名のまま承認すると亜種が並ぶ (#24 と #25)。更新なら承認画面が差分を見せ、人間は上書きに値するかを判断できる。 |
| 3 | §0 の目的文 (3 文) を prompt の目的節として製品既定にするか | はい。目的節に書く | 2026-10-19 | PF を主目的にすると PF 探索・param 微調整へ寄る。収益指標は床に置き、重なりの測定 (発火時刻の Jaccard) は後続 (§11)。 |
| 4 | 案 A の床を戻す時期 | IQ-T1 + IQ-T2 の完了と同じリリース窓で戻す (IQ-T3 は両方の後) | 2026-10-19 | 戻さないと床 PF 1.0 のチケット裁定と運用が食い違い続ける。旧 prompt のまま床だけが効く順序を作らない (§2.5)。 |
| 5 | `require_holdout_evaluable` | 本機だけ true。コード既定は据え置き | 2026-10-19 | #24 / #25 型 (holdout 4 trades) の承認を止める。評価不可の承認は選択バイアスの検証が効かない。条件は agent に説明しない。 |
| 6 | 既存配備 #21 / #24 / #25 の扱い | (c) 保存済み backtest を新基準で**読み取り専用に再評価**し、結果 (失敗理由 = PF 不足 / holdout 評価不能) を見て人間が個別に keep / disable を**明示決定**する。自動解除はしない。disable は既存の `afx plugin disable --revoke`。再評価は IQ-T3 の手順 0 として床を戻す前に実行する (§2.5) | 2026-10-19 | 当初の推奨は無期限の grandfather だったが、第二意見で改めた。#21 を無期限に残すと「PF ≥ 1.0 を床」の製品裁定と恒久的に食い違う。一方、解除は人間承認済みを覆すので、既存の明示的な revocation 経路で人間が決める。 |
| 7 | 観測期間と合否 | 統計的な合否にせず 2 層にする。必須の不変条件 = 台帳の分類が全件正しい、自己書き戻しの再発 0。方向性指標 = 週次の有効 mission 6 回で、同族の重複試行 1 回以下、agent 発・新規仮説族・提出到達の候補 2 本以上 (ユーザー案と手動 mission は数えない、承認数は条件にしない)。観測開始の条件 = mission 出力の保存修復 (`improve-mission-output-not-persisted`) の完了 (§3.2) | 2026-10-19 | 当初の推奨は「重複 1 回以下かつ新規 2 本以上」の合否だったが、件数が少なく統計にならないため、第二意見で不変条件と方向性指標に分けた。「新規候補」の定義を固定しないとユーザー案や redirect が成功数に混ざる。 |

### 8.2 既に決めた確認事項 (問いにしない)

次は設計で決めた事項。覆したいときだけ指摘する。

- 多様性は強制しない。軸の指定、他 pair / 他足、複数足は後続。利用者が directives.md に書ける。
- 失敗の学習は親が記録する事実のみ (名前・仮説族・分類)。成績値・holdout・人間の reason は台帳の新列にも agent 面にも出さない。
- agent 面に「標本不足」「評価可能」「30 trades」「holdout_not_evaluable」は出さない。収益性・取引数不足・比較不能の非提出は中立な `not_submitted` に畳み、「理由非公開。同一候補を再提出しない」とだけ書く。`unprofitable` は内部状態。
- drawdown と連敗は床に足さない (根拠データが足りない。max_drawdown の観測は 0.03〜0.13 の少数)。
- (b) の拒否は、別名・却下済み・降格済み・example との一致だけ。配備済みの同名旧版との一致は更新として許す。(c) の降格は配備済みの族だけを対象にする (§2.3)。
- 新しい設定キーを作らない。`discoveries` の上限は定数 (設定化は後続)。
- 候補台帳は `candidate_archives` に列を足す。新しい表を作らない。人間の決定と archive 更新は `apply_decision` の同じ tx、撤回は `append_revocation` の同じ tx。行が無いときは決定だけが残る (§2.2)。
- 親の台帳は mission 出力の保存 (`missions.output_json`) に依存しない。この保存は別チケットで先行して直す (小)。ただし §3.2 の方向性指標の観測は、この修復の完了を開始条件にする (§8.1 問 7)。
- 最終分類の優先順位 (§2.3: parse → noop_copy → 他の gate → 床 → `logic_hash` → 仮説族 → 成績完全一致) と実行位置は技術的な整合条件として固定する。配備済みの同名旧版との `logic_hash` 一致は更新として許す。
- `family_redirect` の有効化は IQ-T2 と approval-content の AC-T1・AC-T2 の merge 後 (IQ-T2b)。それまでは検出と `quality_flags` への記録だけで降格しない (安全条件)。
- 仮説族の定義は strategy で「indicator 集合 + 足 + pair」、indicator で「名前 + 足」(pair なし)。エントリ型 (cross / reclaim など) は実行しないと判別できないので含めない。粗い同族判定と、indicator を使わない自前計算の戦略 (`sma_cross` 型) の穴は受容し、§11 の後続条件に置く。
- `discoveries` の入力件数の上限は 5 (設計値)。4 / 5 / 6 件の境界 test と運用観測で調整する。
- candidate 単位の履歴 (「試した仮説」節) は採らない。agent 面には mission 単位の `last_result` だけを出す。必要になれば §2.2 の固定事項つきで、codex のレビューを受けて有効化する。
- 候補の identity は `(mission_id, artifact_hash)`、`pair` は属性で nullable (indicator は NULL)。最終 artifact は終端に至った時点の staging の bytes で、pytest 実行後の全 return の直前に再 hash する (§2.2)。手動 mission の launch 失敗も `compensate_launch_failure` の下に置く。
- 質検査の優先順位と実行位置は §2.3 の 2 つの表で確定済み (noop_copy は plugin gate 内、重複の段は `_finalize_success` の tx 内)。実装時の再確認事項は残さない。

## 9. 受入条件

境界値の AC は、上限 `L` について `L-1` / `L` / `L+1` を入れて観測する。「全数」「趣旨」など観測できない語は使わず、列挙した対象と固定の文字列で書く。

| AC | 内容 | 観測 |
|---|---|---|
| 1 | `discoveries` に `kind=task` を書いた出力は note として保存され、open の行が増えない。`discoveries` 経由の open 行は常にゼロ | DB の status |
| 2 | 同じ idea 文の note が既にある `task` は昇格しない (従来は open に昇格) | DB の status |
| 3 | backlog に `open` の行を作る・戻す writer を次の列挙に固定する。(i) `store/backlog.py::add` (人間のみ。呼び出し元は `commands.py:60` と `ops/service.py:713`)、(ii) `commands.py` の `set_status(..., "open")` (`:404`、人間の reopen)、(iii) `store/missions.py::finish_improve_mission` の `backlog_transition` (選択済みの行を `open` に戻す。`_finalize_gate_sandbox_unavailable` のみ)、(iv) `improve_loop._upsert_backlog_idea` の `INSERT` (`:1554`)。agent から届くのは (iv) だけで、`discoveries` の経路は `kind` を問わず note を挿入し、`selected.idea` の経路だけが open を挿入する。test は `src/` の `INSERT INTO improvement_backlog` と `UPDATE improvement_backlog SET status` の全記述を AST / 文字列走査で数え、列挙した (file, 関数) の集合と一致することを pin する。さらに `discoveries` に 6 種の入力を入れた出力で open 行が 0 であることを観測する | writer の allowlist pin (走査)、DB の status |
| 4 | `discoveries` の入力が 4 / 5 / 6 件のとき、6 件目は落ち activity に残る | 境界 (`L=5`) |
| 5 | #24 / #25 の実ファイルを入力にして、`logic_hash` は不一致、`family_key` は一致 (`{bollinger, rsi}`, 1h, USDJPY) | 実ファイル fixture |
| 6 | #21 / #22 の実ファイルを入力にして、`family_key` は両方 `{sma}, 1h, USDJPY` で一致し (c) で検出される。正規化後の `logic_hash` の一致 / 不一致は実測値を fixture に固定する | 実ファイル fixture |
| 7 | 数値リテラル・docstring・rationale 文字列だけが違う 2 候補は `logic_hash` が一致し、列名など判定に効く文字列が違うと不一致。`docs/examples/plugins/sma_cross` 型は `family_key` が NULL で (c) の対象外。この型の穴は既知の未解決として test の期待に明記する | SMA 系の fixture |
| 8 | 優先順位 (§2.3)。次の fixture を含む。(i) **noop と、logic / family の両方に一致する候補は最終 outcome が `noop_copy`** (段 L・F に到達しない。候補の最終行が `noop_copy` で `quality_flags` が空)、(ii) 完全コピーは `noop_copy`、(iii) 別名・却下済み・降格済み・example との `logic_hash` 一致は成績が違っても `duplicate_logic`、(iv) `duplicate_logic` と family の両方に一致する候補は `duplicate_logic`、(v) `unprofitable` かつ `duplicate_logic` の候補は `unprofitable`、(vi) 成績完全一致は `duplicate_metrics`。各 fixture は順序を入れ替えると結果が変わる | 優先順位の境界 |
| 9 | **一般更新**: 配備済みの同名旧版との `logic_hash` 一致 (config 値だけが違う同名候補) は `duplicate_logic` で拒否されず、仮説族でも降格されない。成績が前版と完全一致する場合だけ `duplicate_metrics` | 同名更新の 3 ケース |
| 10 | **pin 再ロック** (`is_relock_transition`): 一般更新とは別のケースとして、現行どおり `noop_copy` / `duplicate_metrics` の例外が働き、一般更新の例外を流用しない | 再ロックの fixture |
| 11 | 仮説族が配備済みと一致し名前が違う候補は、有効化前は降格せず提出され、最終行の `quality_flags` が `["family_detected"]` になり outcome は `pending`。その行が `approved` に進んだ後も `quality_flags` は `["family_detected"]` のまま。有効化後は `family_redirect` で降格し (フラグは足さない)、同名の候補は降格しない。却下済みの族は (c) で閉じない | 有効化前後 × 別名 / 同名 × 決定後 |
| 12 | 却下済み・降格済み候補と同じ `logic_hash` の候補は、成績が違っても拒否される。同じ mission の途中の反復の行とは一致を取らない (母集団に入らない) | 母集団の境界 |
| 13a | **canary、prompt**。内部状態 (DB) に、holdout の数値 (例 pf 0.7351)、holdout の条件語 (「標本不足」「評価可能」「30 trades」「holdout_not_evaluable」)、人間の reason (`approval_requests.reason` に固有文字列)、他候補の成績 (`metrics_json` に固有の数値)、各 hash (`artifact_hash` / `content_hash` / `logic_hash`) を、**面ごとに別の canary 値**で入れる。最初に、入れた値が DB から読み戻せることを assert する (入力側の positive control)。そのうえで `_render_improve_mission_prompt` の出力に、いずれの canary も含まれないことを assert する | 入力存在の assert + 不在の assert |
| 13b | **canary、履歴の節**。candidate 単位の履歴の節は採らないので、prompt に履歴の節の見出しが無いことと、`improve_context.py` が `candidate_archives` を import しないことを pin する。履歴の節を後続で採る変更は、この pin を赤にする | 不在 pin |
| 13c | **canary、activity の agent 投影**。improve の mission registry (`tools/mission_registry.py:80-107`) が登録する tool は research / staging / rpc の 3 群だけで、activity を読む tool が無い。この tool 名の集合を allowlist として pin し、improve 用の context (`improve_context.py`) が `activity` を import していないことを走査で pin する。agent 投影を新設する変更は、この pin を赤にする | registry の allowlist pin、import 走査 |
| 13d | **canary、降格の通知**。(b)(a) と、有効化後の (c) の降格を実際に 1 件ずつ発生させ、それぞれ `improvement_backlog.last_result` が 1 件書かれたことを assert する。その `last_result` が固定の許容集合 {`observation:duplicate`、`observation:family_redirect:<配備済みの戦略名>`、`observation:not_submitted`} のどれかと完全一致し、他候補の hash、pair、成績、canary を含まないことを assert する。activity の `quality_demoted` には詳細が残ることも assert する (詳細は運用者向け) | 通知の生成 + 完全一致 |
| 14 | `outcome_class` の遷移 (§2.2) を、14 × 14 = 196 組の全部で観測する。許される 13 組 (`not_submitted` → 8 値、`pending` → 4 値、`approved` → `revoked`) は書き込み API が成功し、残りの 183 組は拒否される (自己遷移を含む)。CHECK 句が 14 値を持ち、集合外の値の挿入が制約違反になる。`outcome_class` を UPDATE する箇所が (migration を除き) `settle` / `apply_decision` / `append_revocation` の 3 関数だけであることを `src/` の走査で pin する。`quality_flags` は許容 2 要素以外の書き込みが拒否される | 全組の parametrize、制約違反、書き手の走査 |
| 15 | `apply_decision` が決定と archive 更新を同じ tx で行う。(i) 提出した行 (`candidate_archive_id`) だけが更新され、同じ mission の他の行は変わらない、(ii) **self-test を書き換えて提出した候補** (run_backtest 時の `artifact_hash` と提出時の `artifact_hash` が異なる fixture) でも `candidate_archive_id` の行が更新される、(iii) CAS が失敗したときは更新しない、(iv) 行が無い・`pending` でないときは approval の決定は commit され、archive の更新だけが skip されて `warning` が残る、(v) 4 つの決定 (承認・却下・期限切れ・無効化) のそれぞれ、(vi) 途中の失敗で approval だけが更新された状態が残らない (例外注入)。旧 payload (`candidate_archive_id` なし) は `resolve` で引かれる | tx 境界、行の限定、例外注入 |
| 16 | prompt の文字列 pin。描画した prompt に次の文字列が**含まれない**: 「全体収益」「PF 最大化」「標本不足」「標本 (取引数)」「評価可能」「30 trades」「holdout」「holdout_not_evaluable」「必ず実質的な変更を含めること」「試したパラメータと得られた pf」「avg_r を具体的に」「明確にパラメータを変え」「パラメータ・フィルタ・エントリ条件を」「変えて `run_backtest` をやり直し」「試したパラメータ群」「`unprofitable`」。次の文字列が**含まれる**: §0 の目的文 (逐語)、「理由非公開。同一候補を再提出しない」、`not_submitted`。floor 文は床の値 (`pf >= ...`) を含み、holdout の条件は含まない | 固定文字列の pin |
| 17 | 床の snapshot (`profitability_floor`) が承認 payload に新値で入る | payload |
| 18 | 失敗の 3 分類 (§6): 候補自身の parse 失敗は拒否、比較対象 1 行の破損は warning + その行だけ skip、比較基盤が全部使えないときは `gate_failed` + 人間通知。(a) は従来どおり動く | 例外注入 |
| 19 | migration の旧 fixture で、既存列の値と payload bytes が変わらず、schema 列集合の差が追加 4 列だけ。旧行の `logic_hash` / `family_key` が現物から埋まり、計算不能な行は NULL で比較から外れる。旧行の `outcome_class` が §2.2 の照合順で埋まる。fixture は (1) 完全一致、(2) `artifact_hash` が違い `(mission_id, content_hash)` が 1 行、(3) `(mission_id, content_hash)` が 2 行で曖昧、(4) 行が無い、(5) 撤回済みの承認、(6) 提出に至らない行、を含む。(3)(4) は結ばれず `legacy_unlinked` が付き、他の行を `approved` にしない | migration test |
| 20 | 質検査の正負ペアの fixture (mutation tool に依存しない)。各対の一方が当たり、もう一方が通る。(i) 判定の反転: 同じ `logic_hash` の別名 → `duplicate_logic` / 構造が 1 文違う別名 → 通る、(ii) 母集団: 却下済みの別名との一致 → `duplicate_logic` / 同じ mission の途中の反復との一致 → 通る、(iii) 母集団 (c): 配備済みの族 → 検出 / 却下済みの族 → 検出しない、(iv) 同名例外: 同名旧版との一致 → 通る / 別名の配備済みとの一致 → `duplicate_logic`、(v) 優先順位: AC-8 の fixture。いずれかの判定を反転・除去・入れ替えると、どれかの対が赤になる | 通常 test の正負ペア |
| 21 | **撤回**。(i) `revoke_version` (`deploy_disable.py:263`) と `disable --revoke` / `rollback --revoke` (`deploy_ops.record_intent` `:309`) のそれぞれで、`append_revocation` の tx の中で元の approval の行が `approved` → `revoked` になる、(ii) 最新の決定が既に `rejected` で `append_revocation` が `None` を返すとき行は変わらない、(iii) `retire` (`revoke=None`) では行は変わらない、(iv) tx の失敗で revocation 行だけが残らない (例外注入)、(v) 同じ版に承認が 2 つあるとき、撤回の対象に指された approval の行だけが `revoked` になる | 2 経路 × tx 境界 |
| 22 | **最終 artifact の行**。(i) self-test を書き換えた候補の提出で、新しい `artifact_hash` の行が作られ、承認 payload の `candidate_archive_id` がその行を指す、(ii) 書き換えが無いときは既存の行が再利用され新しい行が増えない、(iii) 提出に至らない判定済みの候補 (E7〜E12) で `archive_path` NULL の行が作られ outcome が `settle` される、(iv) E1〜E6、E13、E16、E17、E18a〜E18c では行が `not_submitted` のまま増えない、(v) `_PluginGateVerdict` が `:1643` 以降の失敗 return すべてで hash を持つ。pytest 実行後の return (pytest 失敗 `:1680` を含む) では return 直前に再 hash した値を持つ。fixture は「self-test が `plugin.py` を書き換えてから pytest が失敗する候補」で、終端の最終行の `artifact_hash` が書き換え後の staging の bytes の hash と一致し、`:1643` の値とは異なること。再 hash が取れない (ファイル削除) ときは行が作られない、(vi) **indicator 候補**の提出 (`docs/examples/plugins/rsi_indicator/config.yaml` 型、`pairs` なし) で最終行が作られ、`pair` が NULL、承認 payload の `candidate_archive_id` がその行を指す、(vii) indicator の仮説族 key が名前 + 足 (pair なし) で、strategy の key と混ざらない | 行の作成 / 再利用 |
| 23 | **終端写像** (§2.2 の E1〜E18c)。各終端に 1 つの fixture を作り、最終行の有無と outcome が表と一致し、最終行以外の行が `not_submitted` のままであることを観測する。`report_failed` の終端 (E6 と、E7〜E12 の report 書き込み失敗の分岐) でも候補の outcome が mission_outcome に引きずられない。launch 失敗は定期 slot (E18b) と手動 mission (E18c) の**両 corridor**で、`runner.run()` に例外を注入し、mission / run (定期は slot も) が終端し、staging が消え、例外が re-raise され、台帳の行が増えないことを観測する。手動経路は補償を外すと mission が非終端のまま残って赤になる | 終端ごとの fixture |
| 24 | **agent 面の投影**。内部の `unprofitable` / `insufficient_trades` / 比較不能の 3 つが、履歴の `last_result` と `origin` 列でいずれも `not_submitted` と表示され、3 つの間で表示の差が無い。`origin_outcome` の書き込みが `unprofitable` と `insufficient_trades` の両方で `not_submitted` になる | 3 状態の表示比較 |
| 25 | **保存済み backtest の再評価 (IQ-T3 手順 0)**。#21 / #24 / #25 の実 payload を入力に、新基準での判定表が承認ごとの 1 行で出る。#21 は失敗理由に `PF 不足`、#24 / #25 は `holdout 評価不能` を含む (実測値を fixture に固定する)。実行の前後で DB (`approval_requests`、`candidate_archives`、`plugin_deploy_ops` を含む全表)、`config/settings.yaml`、`plugins/` の bytes が変わらない。出力の表を agent 面の文脈 (prompt、`improve_context.py`) が読む経路が無い | 読み取り専用 (前後の hash 一致)、実 payload fixture |
| 26 | **方向性指標の取得 (§3.2)**。「新規候補」の 3 条件 (agent 発、新規仮説族、提出到達) を、条件ごとに 1 つずつ外した fixture で観測する。ユーザー案を選んだ mission、手動 mission、`family_redirect` の行、`family_detected` を持つ行、`not_submitted` の行は数えない。有効 mission の除外 (E1、E13、E16、E17、E18a〜E18c) も同様。不変条件 1 は終端写像 (AC-23) の期待値と台帳の全行を突き合わせる確認、不変条件 2 は AC-3 の writer 走査と DB の status で観測する | 条件別の fixture、全行の突き合わせ |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル (予定)

| 領域 | ファイル | 変更 |
|---|---|---|
| 記録 | `src/agentic_fx/store/candidate_archives.py`、`store/db.py` | 4 列の追加、`pair` の NULL 許可 (`PRAGMA table_info` 判定の冪等 table rebuild)、CHECK / default、migration (旧行の再計算と照合)、`insert` / `settle` / `resolve` の API、遷移表の検査 |
| 決定と撤回 | `src/agentic_fx/store/approvals.py` | `apply_decision` と `append_revocation` の同じ tx で archive の `outcome_class` を更新 |
| 記録と遮断 | `src/agentic_fx/loops/improve_loop.py` | `_select_and_bind` の `discoveries` を note のみに、昇格を外す、最終 artifact の行の作成、`settle` の呼び出し (E1〜E18c)、`_PluginGateVerdict` の最終 hash (pytest 実行後の return の再 hash) と失敗種別、承認 payload の `candidate_archive_id`、`origin_outcome` の書き込み |
| 起動の補償 | `src/agentic_fx/core/improve_supervisor.py` | `submit_manual` の `runner.run()` を `compensate_launch_failure` の下に置く (E18c) |
| 出力検査 | `src/agentic_fx/loops/summary.py` (`IMPROVE_OUTPUT_SCHEMA`) | `kind` の許容値に `idea`、`task` は note に落とす |
| prompt | `src/agentic_fx/loops/prompts/improve_mission.md`、`loops/improve_context.py`、`improve_loop.py` の `_floor_rule_text` と `_backlog_table` | 目的節、規律 2・3・5 の書き換え、floor 文の書き換え、`origin` 列の投影、配備済みの族の一覧、`discoveries` の説明 (履歴の節は設けない) |
| 質検査 | `src/agentic_fx/loops/improve_loop.py` (`_check_candidate_quality`、`_QualityVerdict`)、`plugin/noop_gate.py` | `logic_hash` / `family_key`、段 L・F・M の呼び出し順、母集団の組み立て、失敗の 3 分類、降格の固定文言と `quality_demoted` の activity |
| activity の agent 投影 | なし | improve の registry に activity を読む tool が無いことを pin (AC-13c) |
| 運用 | 運用手順 (docs)、再評価の読み取り専用の確認 | 床を戻す手順 (§2.5 の手順 0〜6)。手順 0 の再評価は読み取り専用の確認コマンドまたは手順書のコマンド列で、新しい書き込み経路は作らない |
| test | `tests/loops`、`tests/store`、`tests/plugin` | 実ファイル fixture (#24 / #25 / #21 / #22 / `sma_cross`)、migration の旧 fixture、canary、終端ごとの fixture、遷移の全組 |

### 10.2 task 表

本 spec の task は `IQ-T0`〜`IQ-T3`。approval-content spec §10.2 の task は `AC-T1` (API + CLI + web) と `AC-T2` (Discord 承認投稿) と書き、混同しない。

`candidate_archives` を参照する既存 test は 9 ファイル 66 箇所、`candidate_archives.insert()` は 5 test ファイルの 10 呼び出しで、追加列は `SELECT *` の read model に即座に現れる。IQ-T0 の規模はここから見る。

| task | 依存 | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|---|
| IQ-T0 schema と記録 | なし | 4 列と CHECK / default、migration (旧行の再計算と照合)、`insert` / `settle` / `resolve`、遷移表、`apply_decision` と `append_revocation` の同 tx 更新、最終 artifact の行の作成、終端写像 E1〜E18c の `settle`、`_PluginGateVerdict` の最終 hash と種別、`pair` の NULL 許可、既存 test 66 箇所の追随 | AC-14、15、19、21、22、23。旧 fixture、既存列の値と payload bytes の一致、列集合の差分 (`pair` の NOT NULL 解除を含む)、全終端 fixture (手動 launch 失敗を含む)、indicator 候補の fixture | L |
| IQ-T1 agent 面 | IQ-T0 | `discoveries` の note 化、目的節と規律 2・3・5 と floor 文の書き換え、`origin` 列と `last_result` の `not_submitted` 投影、降格の固定文言、配備済みの族の一覧 | AC-1〜4、13a〜13d、16、17、24。実 DB 形の fixture、canary、文字列 pin、writer の走査 | M |
| IQ-T2 質検査 | IQ-T0 | `logic_hash` の正規化、`family_key`、段 L・F・M の呼び出し順、(b)(c)、`quality_flags` の `family_detected`、失敗の 3 分類 | AC-5〜12、18、20。#24 / #25 / #21 / #22 / `sma_cross` の実ファイル、正負ペアの fixture | M |
| IQ-T2b `family_redirect` の有効化 | IQ-T2 + AC-T1 + AC-T2 | 切替を `family_redirect` に変える | AC-11 の有効化後ケース | S |
| IQ-T3 運用手順 | IQ-T1 + IQ-T2 | §2.5 の手順 0〜6。手順 0 (床を戻す前の読み取り専用の再評価と、結果の人間への提示、#21 / #24 / #25 の個別の keep / disable の明示決定) を含む。実装は無く docs と運用 (merge と同時) | AC-17、25。手順の実行確認。承認 payload の新値。旧 prompt のまま床だけが有効になる順序を作らない。再評価が何も書かないこと | S |

IQ-T1 と IQ-T2 はどちらも IQ-T0 の API (`settle`、`resolve`、遷移表) にだけ依存し、互いの変更を前提にしない。merge の順は問わない。IQ-T3 は IQ-T1 と IQ-T2 の**両方**の後に行う。旧 prompt は「標本不足」「pf / avg_r を具体的に」「パラメータを変えて再実行」を明示しており (`improve_mission.md:16-20`、`:74-76`、`:87-88`)、これを直すのは IQ-T1 である。IQ-T2 だけが済んだ状態で `require_holdout_evaluable=true` を有効にすると、遮断と「PF は床のみ」の目的が成立しない。IQ-T2b は IQ-T2 のほか、AC-T1 と AC-T2 の**3 者**が merge 済みであることを必要とする (同名更新の差分を人間が API / CLI / web と Discord の両方で見られる前に降格を有効化しない)。IQ-T0〜IQ-T2 は純 Python (IQ-T0 は終端が多く規模が大きいので、codex に渡すときは終端写像を分割する)。

## 11. 後続と非目標

- 測定基盤: 戦略別の帰属と影の台帳 (`strategy-attribution-and-shadow-evaluation`、別の C0)。重複判定 (d) の発火時刻の Jaccard と、「選別後の期待値」の目標 (a) はこれに依存する。
- **indicator を使わない自前計算の戦略 (`sma_cross` 型) の重複判定 (既知の未解決穴)**: 仮説族が空で、AST hash も構造変更で避けられる。発火時刻の Jaccard、または自前計算の戦略への構造的な近さの判定が入るまで残る。
- 根拠タグの契約追加 (signal に決定論的なタグ)。plugin 契約・trade 側・backtest 側を貫くので別設計。
- 発火時刻の Jaccard による重複判定: backtest の entry bar を使う。#25 を堅牢に捕まえるが、本 spec は構造 (b)(c) までにする。
- 承認画面の成績差の並記: approval-content が見送っている。
- candidate 単位の履歴を agent に出すこと (§8.2 で当面採らないと固定、必要になれば後続)。
- 指名実行 (`improve-targeted-run`)、cadence 分離 (`improve-daily-params-weekly-algorithm`): 目標と独立。指名実行は重複課題の人間側の回避手段にはなる。
- mission 出力の保存 (`improve-mission-output-not-persisted`): 別チケットで先行 (小)。本 spec の台帳は依存しないが、§3.2 の方向性指標の観測はこの修復の完了を開始条件にする。
- 親による探索軸の割り当て、他 pair / 他足 / 参照系列: 利用者が directives.md に書く運用から始め、必要になったら設計する。
- drawdown / 最大連敗の床: 根拠データが溜まってから。

## 12. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-19 | v0.1 | C0 下書きを新規作成 | 現状調査 (`tmp/design-improve-quality/survey.md`) と 5 チケットの設計 | |
| 2026-10-19 | v0.2 | holdout 条件の agent 面からの全削除と canary AC、判定順序と同名例外、#21 / #22 の実測訂正と正規化規則、B の有効化条件 (approval-content 依存)、目的文と規律 2・3・5・floor 文の書き換え、台帳の enum 15 値と状態遷移表・migration・同 tx 更新、履歴の oracle 緩和と問 10 の書き換え、不変条件の統一、T0 の先行分離、床の戻しの運用手順、失敗の 3 分類、旧版の記述訂正 | codex sol 1 周目の 12 件 | |
| 2026-10-19 | v0.3 | 承認と archive 行の結び (`candidate_archive_id`、最終 artifact の行、旧行の照合規則、行が無いときの扱い)。`family_detected` を outcome から外し `quality_flags` 列へ (enum 14 値、遷移 13 本)。§2.3 を「最終分類の優先順位」と「実行位置」の 2 表に分け、現物の file:line を付与。全終端の写像表 (E1〜E18) と撤回の更新点 (`append_revocation` の tx)。task 名に `IQ-` 接頭辞、依存を `IQ-T3 ← IQ-T1 + IQ-T2`、`IQ-T2b ← IQ-T2 + AC-T1 + AC-T2` に固定。agent 面の非提出表示を中立な `not_submitted` に畳む (`unprofitable` は内部状態のみ)。AC-13 を 4 面に分割、AC-3、16、20 を観測できる形に、AC-21〜24 を追加。§8.2 の「実装時に確認」を削除 | codex sol 2 周目の 8 件 | |
| 2026-10-19 | v0.4 | 最終 artifact を「終端に至った時点の staging の bytes」と定義し、pytest 実行後の全 return の直前に再 hash する規則に (E8、実行位置表、AC-22(v))。`candidate_archives.pair` を nullable にし (IQ-T0 の table rebuild)、候補の identity を `(mission_id, artifact_hash)`、仮説族の key を kind ごとに (AC-22 に indicator fixture)。手動 mission の launch 失敗を `compensate_launch_failure` の下に置き、E18 を E18a〜E18c に分けて AC-23 に両 corridor。§8.1 を主要 4 問 + 3 問の 7 問に整理し、旧問 2(i)(ii)、8、9、10 を §8.2 の固定事項へ | codex sol 3 周目の High 3 件と問 4 | |
| 2026-10-19 | v0.5 | `pair` の NULL 許可の migration を現物 (`init_db()` の冪等 shape migration、DB に `schema_version` 無し) に合わせ、全数 grep の結果 (NULL で壊れる reader なし、変更は 4 箇所) を §2.2 に記録。問 3 を §0 の目的文と一意に対応させ、問 2 との依存を明記 | codex sol 4 周目 (閉鎖確認) の Low 2 件 | |
| 2026-10-19 | v1.0 | ユーザー裁定 7 問を記録し (§8 を「裁定の記録」に、§8.1 を決定の表に)、条件分岐の文を決定後の文に書き換え。問 6 = 保存済み backtest の読み取り専用の再評価と人間の個別の keep / disable の明示決定を IQ-T3 の手順 0 として §2.5 に追加 (AC-25)。問 7 = 統計的合否を必須の不変条件と方向性指標の 2 層に改め、「新規候補」の定義と観測開始条件を §3.2 に固定 (AC-26)。§0、§10、§11 の参照を同期 | ユーザー裁定 (2026-10-19) と第二意見 | |
