# [switch-ops-hardening] 実装プラン v1.4b (設計書 = `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md` v1.8b 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (推奨) または superpowers:executing-plans で task ごとに実行すること。Step は
> チェックボックス (`- [ ]`) で追跡する。

**Goal:** plugin 承認・切替回廊の**収束の穴**と**人間から見える状態の穴**を塞ぐ。
(1) 切替が失敗して journal だけ `switched` になった行を `approval retry` すると
「approved なのに未配備」で終端する欠陥を、**巻き戻して新しい journal 行で手順を頭から
流す**形 (案 C) に直す。(2) 起動時 reconcile の巻き戻しを **lock + lock 内での再読**
の規律に載せる。(3) `approve_candidate` / `retry_approval` が **lock 内で確定した
outcome** を返し、シェルはそれを文言に写すだけにする。(4) CLI の例外の扱いを揃え、
(5) 対話シェルに `approval list` を足す。**新しい配備経路も自動化も作らない。**
(6) **シェルの `approve <id>` も同じ lock 内 outcome を文言に写す** (v1.2、設計書 §3.6)。
(7) **巻き戻しの commit が lock の内側で完了していることを別コネクションから pin する**
(v1.2、設計書 AC-9d。**本体コードの変更は無い**)。
(8) **再開時に `switch_required` が保存値と食い違ったら、その行を巻き戻して新しい `op_id` で
頭から流し直す** (v1.3 = **T11**、設計書 §3.2 / R11。2 周目 `/code-review` #1 を probe で再現)。
(9) **`afx plugin retire` の `UnresolvedJournalError` にも bless と同じ「次の 1 手」を添える**
(v1.3 = **T12**、設計書 §3.4 / R12)。
(10) **版 dir の hash 再照合を切替の要否にかかわらず通す** (v1.4 = **T13**、設計書 §3.7 / R14 / IV-7)。
(11) **2 周目ローカル LLM の確定 2 件 (テスト欠落) を pin する** (v1.4 = **T14**、テストのみ)。
**v1.4 では T11 の判定述語を 3 つ組 `(switch_required, old_kind, old_target)` に強め** (設計書 R13)、
**判定を 1 箇所に統合した** (plain 分岐専用の hunk は不要になった)。

**Architecture:** 変更は 3 ファイルに閉じる —
`src/agentic_fx/plugin/switch.py` (分類器 / 2 段ガード / 0d 案 C / reconcile の lock +
再読 / outcome)、`src/agentic_fx/commands.py` (retry の結果報告 / `approval list`)、
`src/agentic_fx/backtest/cli.py` (例外の捕捉)。**DB スキーマ変更なし・新 phase なし・
journal-first の順序は不変**。判定 (live がどこを指しているか) は `classify_live` の
1 関数に寄せ、**処置だけが actor で分かれる** (無人の reconcile は pending 留置、
人間の retry は配備まで完了)。

**Tech Stack:** Python 3.13 / uv / pytest / sqlite3 / fcntl.flock。

**Spec:** `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md` v1.6。
ユーザー裁定 R1〜R8 (2026-09-19) と **R9 / R10 (2026-09-20、段 0 が報告した設計判断 2 点)**、
設計レビュー r1 (C1/I4)・r2 (C0/I4/M1)・r3 (C0/I1)・r4 (C0/I3/M1)
は設計書 §0 / §8.1〜§8.4 に確定記録済みで、**裁定待ちは無い**。設計書に無い判断が
必要になったら**実装を止めて指揮者へ申告**すること。
**T1〜T8 は worktree `tmp/wt/soh` (HEAD `3f4f553`) で実装 + 段 0 まで完了済み。
v1.2 で追加する T9 / T10 はその上に積む。**

## Global Constraints

- **実 DB (`data/agentic.db`) を読み書きしないこと。** 全テストは `tmp_path` 上の sqlite
  を使う。`tests/conftest.py` の session ガードを無効化・迂回しない
  ([[tests-touching-real-repo-resources]])
- **実 `plugins/` を読み書きしないこと。** bless / retry の実測は `tmp_path` 配下に作った
  plugins dir に対してのみ行う
- **承認規律を破らない。** 人間の明示操作 (`approve` / `bless` / `approval retry`) が
  無ければ pending が approved にならない。無人の reconcile が前に進めるのは
  **live が既に新 target を指している行だけ** (設計書 IV-4)
- **遮断 8 を破らない。** 本束が触るのは CLI / 対話シェル / 起動時 reconcile の**人間向け
  経路のみ**。改善 agent のプロンプト生成には 1 行も触れない。**新しい文言を
  `approval_requests.reason` / `improvement_backlog.last_result` に書かない**
  (書くと `last_result` 経由で改善プロンプトへ漏れる — 既知の事故)
- **agent に届く文字列にレビュー由来の語を書かない。** activity ログは agent に届かない
  ので可。**tool 応答・prompt・`docs/examples/plugins/` は届く**ので、そこに
  「codex」「レビュー r2」等を書かない
- **`run_kind_gate` は raise しない**等の既存規約を変えない。gate の再実行も増やさない
- **既存テストの書き換えは §7.1 の 4 本のみ** (下記 File Structure)。**T9 / T10 / T11 / T12 / T13 / T14 は
  既存テストを 1 本も書き換えない** (T13 は `switch_required=1` の枝を 1 行も変えないので既存 pin
  `tests/plugin/test_switch_paths.py:569` / `:1401` はそのまま。T14 は**既存テストに assert を足す**が、
  既存の assert は 1 つも消さない) (T11 は「前提が崩れていない再開」の挙動を変えない = **AC-16c-3 が回帰防止として pin する**。
  T12 は CLI の `_plugin_retire` 失敗経路を踏む既存テストが 0 件 — 設計書 §8 で確認済)。
  T9 については `tests/test_commands.py::test_approve_plugin_kind_reports_actual_outcome_not_always_approved`
  が無改変で緑になる文言形を設計書 §3.6.3 が選んである。**red になったら黙って直さず、指揮者へ申告する**
  ([[plan-code-defects-not-implementer-defects]])
- **未コミットの差分の上で `git checkout` / `git restore` を使わない。** 逆変異の復元は
  `cp` 退避で行う ([[no-git-checkout-over-uncommitted-subagent-work]])
- **出力を `| grep` / `| head` に通して途中終了させない。** pytest の結果は最後まで読む
- **一時ファイルは `tmp/` か scratchpad。** `rm` を使ってよいのは自分が同セッションで
  作った一時領域だけ ([[rm-allowed-directories]])

## プラン規約

- **設計を変えない。** 設計書 v1.6 が正。設計書に無い判断が要るときは**実装を止めて申告**
- **テスト先行**: 各 task は「テストを置く → **red の逐語確認** → 実装 → **green** →
  **逆変異** → commit」の順。red の出力 (最終行) を Step のチェック時に貼る
- **逐語転写は機械 diff する** ([[transcription-must-be-machine-diffed]])。本文のコード
  ブロックをコピーしたら、`diff` で 0 を確認してから次へ進む。「目視で確認した」は不可
- **逆変異は適用可能な形で書いてある** (置換前の行 / 置換後の行 / 対象ファイル / red に
  なるべきテスト名)。説明文だけで済ませない ([[mutation-testing.md]] の規律)。
  **リストは下限であって上限ではない**
- **`except` ハンドラを足すときは故障源を共有していないか見る**
  ([[except-handler-shares-failure-source]])。本束で足す `except` は
  `_plugin_bless` の `UnresolvedJournalError` と `_plugin_materialize` の `ValueError`、
  **および T12 (v1.3) の `_plugin_retire`** の 3 つで、どれも**新しい握りつぶしを作らない**
  (rc=1 + メッセージ)。**T12 は既存の 1 節を 2 節に割るだけで、捕まえる例外の集合は 1 つも変わらない**
- **起草者 (このプラン) は未実測の箇所を明示申告している** — 末尾の「未実測の申告」。
  着手前検証はそこに集中させること

## File Structure

| ファイル | 変更 | task |
|---|---|---|
| `src/agentic_fx/plugin/switch.py` | 分類器 / `_revert_under_lock` / reconcile の載せ替え / 2 段ガード / 0d 案 C / outcome / **再開の前提 (3 つ組) が崩れたときの巻き戻し (T11)** / **版 dir の hash 再照合を常に通す (T13)** | T1〜T5・**T11**・**T13** |
| `src/agentic_fx/commands.py` | `approval retry` の結果報告 / `approval list` / `_HELP` / **`approve <id>` の結果報告 + `_retry_outcome_text` → `_approval_outcome_text` の改名** | T5・T7・**T9** |
| `src/agentic_fx/backtest/cli.py` | `_plugin_bless` / `_plugin_materialize` の except / **`_plugin_retire` の except を 2 節に割る (T12)** | T6・**T12** |
| `tests/plugin/test_switch_ops_hardening.py` | **新規** (AC-1〜AC-9c / **AC-9d** / AC-14d / AC-16a/b) + **追記 (AC-16c-1〜8 / AC-19a〜e)** | T1〜T5・**T10**・**T11**・**T13** |
| `tests/test_commands.py` | **追記** (AC-12 / AC-13 / AC-14a/b/d / **AC-17a〜d**) + **`:541` の spy を書き換え** | T5・T7・**T9** |
| `tests/backtest/test_cli.py` | **追記** (AC-11 / **AC-18**) | T6・**T12** |
| `tests/plugin/test_indicator_initial_set.py` | **2 本を書き換え** (retry の戻り文言 / CLI の traceback) | T3・T6 |
| `tests/plugin/test_reconcile.py` | **1 本を書き換え** (lock 取得列に巻き戻しの 1 本が増える) | T4 |
| `tests/plugin/test_switch_paths.py` | **既存 2 本に pin を 1 行ずつ「追加」+ 1 本を新規追加** (T2-01 / T2-02)。**既存の assert は 1 つも消さない・変えない**ので §7.1 の「書き換え 4 本」には数えない (設計書 AC-15 の注) | **T14** |
| `docs/operations/indicator-initial-set-deploy-2026-09-19.md` | runbook 改訂 (設計書 §7.2) | T8 |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` | §5.1-1 に 1 段落追記 | T8 |

**書き換える既存テストは 4 本** (設計書 §7.1 は 3 本と書いているが、**隔離環境で実際に
走らせたところ `tests/plugin/test_reconcile.py::test_reconcile_resolution_holds_the_
dependency_locks` も red になった** — 下記「spec と食い違った点」)。

## 受入条件 (設計書 §6) と task の対応

| AC | task | テスト |
|---|---|---|
| AC-5 / 分類器の適用範囲 | T1 | `test_ac5_classifier_compares_the_raw_readlink_string` / `test_classifier_refuses_rows_it_does_not_apply_to` |
| AC-16a / AC-16b / AC-6 の変異終点 | T2 | `test_ac16a_entry_guard_...` / `test_ac16a_finalize_guard_...` / `test_ac16b_switch_required_zero_reaches_decided` |
| AC-1 / AC-3 / AC-6 / AC-7 / AC-8 | T3 | `test_ac1_...` / `test_ac3_...` / `test_ac7_...` / `test_ac8_...` |
| AC-2 / AC-9a / AC-9b-i/ii/iii / AC-9c | T4 | `test_ac2_...` / `test_ac9a_...` / `test_ac9b_i/ii/iii_...` / `test_ac9c_...` |
| **AC-14d / AC-14a の switch 側** | **T3** (v1.1 で T5 から移動) | `test_ac14d_no_bare_return_in_approval_entrypoints` / `test_ac14d_outcomes_are_known_enum_values` / `test_ac14d_still_pending_on_missing_candidate` / `test_ac14a_already_decided_status_comes_from_the_row` |
| AC-14a / AC-14b / AC-14c (シェル側) | T5 | `tests/test_commands.py` の追記 (付録 D-1) |
| AC-10 / AC-11 | T6 | `test_runbook_cli_bless_after_post_gate_failure_raises_traceback` (書き換え) / `test_plugin_materialize_containment_error_is_rc1_message` |
| AC-12a/b/c / AC-13 | T7 | `tests/test_commands.py` の `approval list` 群 |
| AC-15 | T8 | フルスイート |
| **AC-9d** (巻き戻しの commit が lock 内) | **T10** | `test_ac9d_reconcile_revert_is_durable_from_another_connection` / `test_ac9d_revert_is_committed_before_the_lock_is_released` / `test_ac9d_retry_rollback_is_durable_before_pending_return` |
| **AC-16c-1〜AC-16c-8** (再開の前提 (3 つ組) が崩れたら巻き戻して新 `op_id`) | **T11** | `test_ac16c1_resume_with_stale_switch_required_rolls_back_and_redeploys` / `test_ac16c2_reverse_stored_zero_recomputed_true_rolls_back` / `test_ac16c3_resume_without_drift_reuses_the_same_op_id` / `test_ac16c4_precondition_change_leaves_a_dedicated_activity_event` / `test_ac16c5_resume_rollback_is_committed_inside_the_lock` / `test_ac16c6_plain_branch_closes_its_own_unfinished_journal` |
| **AC-18** (retire の「次の 1 手」) | **T12** | `test_plugin_retire_unresolved_journal_is_rc1_with_next_step` |
| **AC-19a〜AC-19e** (版 dir の照合を常に通す、IV-7。**-e は v1.8a 新規**、bless の残余が `approval retry` でも解除できること) | **T13** | `test_ac19a_switch_not_required_verifies_version_dir_before_deciding` / `test_ac19b_verification_runs_before_finalize` / `test_ac19c_same_candidate_reapproval_still_succeeds` / `test_ac19d_bless_raises_when_version_dir_is_tampered` / `test_ac19e_bless_residual_resolved_by_approval_retry` |
| (2 周目ローカル LLM T2-01 / T2-02、AC 番号なし) | **T14** | `tests/plugin/test_switch_paths.py` の 2 本に `assert outcome.status == "pending"` + `snapshot_invalid` 版の未完 journal クローズ 1 本 |
| **AC-17a / AC-17b / AC-17c / AC-17d** (approve の outcome 報告) | **T9** | `test_approve_plugin_reports_the_locked_outcome` / `test_approve_plugin_writes_activity_only_when_deployed` / `test_approve_plugin_message_unaffected_by_later_decision` / `test_approve_plugin_fails_loud_on_unknown_outcome` |

## task 依存図

```
T1 (分類器 + ApprovalOutcome の型を置く、挙動不変)
 │
 ├─► T2 (2 段 phase ガード)            ← T1 の _TERMINAL_PHASES を使う
 │    │
 │    └─► T3 (0d 案 C + outcome 戻り値)
 │         │
 │         └─► T4 (reconcile の lock + lock 内の再読)
 │              │
 │              ├─► T5 (commands.py — retry の文言 + spy 書き換え)
 │              │    │
 │              │    └─► T7 (approval list)
 │              │         │
 │              │         └─► T9 (**v1.2 新設** — approve の文言。commands.py なので T5/T7 と直列)
 │              │
 │              ├─► T10 (**v1.2 新設** — AC-9d の pin。**テストのみ**、T9 と並列可)
 │              │
 │              └─► T11 (**v1.3 新設 / v1.4 で述語を 3 つ組へ** — 再開の前提が崩れたら巻き戻して新 op_id)
 │                   │
 │                   └─► T13 (**v1.4 新設** — 版 dir の hash 再照合を常に通す。同じ switch.py なので直列)
 │
 ├─► T6 (CLI の except)        ← 独立、並列可
 │    │
 │    └─► T12 (**v1.3 新設** — retire の「次の 1 手」。`cli.py` なので T6 と同ファイル → 直列)
 │
 │
 ├─► T14 (**v1.4 新設** — テストのみ。`tests/plugin/test_switch_paths.py` だけなので **T13 の後**に置く
 │        = T11/T13 で対象の行番号が動くため、関数名・分岐名で特定する)
 │
T8 (runbook / 設計書追記 / フルスイート)   ← 全 task の後 (T9〜T14 を含む)
```

- **T1〜T5 は同じ `switch.py` の同じ領域を触るので 1 レーン直列。**
- **T6 は完全に独立** (`backtest/cli.py` のみ) — 別レーンで並列可。
- **T7 は `commands.py` を触る**ので T5 と同ファイル。worktree を分けないなら T5 → T7 の直列。
- **T9 と T10 は触るファイルが重ならない** (`tests/test_commands.py` + `commands.py` ↔
  `tests/plugin/test_switch_ops_hardening.py`) ので**並列可**。worktree を分けなくてよい。
- **T9 は T5 / T7 の後**。同じ `commands.py` の同じメソッド群を触るため。
- **T10 は T4 の後** (`_revert_under_lock` が要る) だが、実態としては **T1〜T8 完了後に積む**。
- **T11 は T3 (0d 案 C の `rolled_back_op_id`) と T5 (outcome の戻り値) の後**。実態としては
  **T1〜T10 完了後に積む**。**T9 / T10 とはファイルが重なる** (`tests/plugin/test_switch_ops_hardening.py` を
  T10 と共有) ので **T10 の後に直列**で置く。T12 とは重ならないので **T11 ∥ T12 は並列可**。
- **T12 は T6 の後**。`backtest/cli.py` の同じ領域を触る。
- **T13 は T11 の後**。同じ `_advance_to_decided` / `approve_candidate` を触るので 1 レーン直列。
- **T14 は T13 の後**。テストのみだが、**T11 / T13 で `switch.py` の行番号が動く**ので、
  対象を**関数名・分岐名・`reason` 値**で特定する書き方にすること (行番号で書かない)。

## T1: live の分類器 + reconcile の載せ替え (挙動不変)

**担当**: 設計書 §3.1。`switched` 行に対する「live は今どこを指しているか」の判定を
1 関数に寄せ、既存 reconcile をそれに載せ替える。**この task では挙動を変えない**
(既存の reconcile テストが 1 本も書き換えずに緑であることが、載せ替えの正しさの証拠)。

### Step 1-a: テストを置いて red を確認する

- [ ] `tests/plugin/test_switch_ops_hardening.py` を新規作成し、**T1 の 2 本だけ**を書く
      (ファイル全体は T3 の Step 3-a で完成させる。ここではヘッダ + 下の 2 本)。
      本文末尾の「新規テストファイル (完成形)」から
      `test_ac5_classifier_compares_the_raw_readlink_string` と
      `test_classifier_refuses_rows_it_does_not_apply_to` を転写する
- [ ] red を確認する (**着手前検証で実走、逐語**):
      `E       AttributeError: module 'agentic_fx.plugin.switch' has no attribute 'classify_live'`
      → `2 failed in 0.51s` (`test_ac5_...` / `test_classifier_refuses_...` の 2 本)

```
uv run pytest tests/plugin/test_switch_ops_hardening.py -q
```

### Step 1-b: 分類器を転写する

- [ ] `src/agentic_fx/plugin/switch.py` の `_PHASE_ORDER = [...]` の**直後**に次を挿入する
      (`APPROVAL_OUTCOMES` / `ApprovalOutcome` は T5 で使うが、**同じブロックとして
      ここで入れる** — T5 で import 順を触り直さないため)
- [ ] `from datetime import datetime` の行の**上**に `from dataclasses import dataclass` を足す

```python path=src/agentic_fx/plugin/switch.py anchor=after:_PHASE_ORDER
_TERMINAL_PHASES = ("decided", "reverted")

# [switch-ops-hardening] T5: lock の内側で確定した結果 (設計書 §3.5)。
APPROVAL_OUTCOMES = frozenset({
    "deployed", "deployed_after_rollback", "already_decided", "foreign_waiting",
    "still_pending", "legacy_plain_present", "invalidated",
})


@dataclass(frozen=True)
class ApprovalOutcome:
    """`approve_candidate` / `retry_approval` が **plugin flock の内側で確定**
    した結果 (設計書 §3.5)。呼び出し元はこの値を文言に写すだけで、
    lock の外で DB / FS を読み直さない (読み直すと、別プロセスの後続の
    正規配備を「契約違反」と誤報する — r2 Important 4)。"""
    outcome: str
    name: str
    status: str
    op_id: int | None = None
    rolled_back_op_id: int | None = None
    target: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in APPROVAL_OUTCOMES:
            raise ValueError(f"unknown approval outcome: {self.outcome!r}")


def classify_live(plugins_root: Path, row) -> str:
    """[switch-ops-hardening] T1 (設計書 §3.1): `switched` で止まった journal 行
    に対し、**live symlink が実際にどこを指しているか**を 3 値で返す。

    比較は `readlink` の生文字列で行う (`resolve()` は使わない) — journal の
    `new_target`/`old_target` は plugins_root 相対の文字列として書かれ、
    `switch_live` がそれをそのまま symlink の中身にするため。`resolve()` に
    すると版ディレクトリが消えた dangling symlink で比較が壊れる。

    **読み取り専用** (IV-5)。`phase='switched'` かつ `switch_required=1` の
    行にしか意味が無いので、それ以外で呼ばれたら fail closed。"""
    if row["phase"] != "switched" or not row["switch_required"]:
        raise ValueError(
            f"classify_live: op_id={row['op_id']} is phase={row['phase']!r} "
            f"switch_required={row['switch_required']} — 分類器は "
            "phase='switched' かつ switch_required=1 の行にのみ適用する")
    live = plugins_root / row["name"]
    live_target = live.readlink().as_posix() if live.is_symlink() else None
    if live_target == row["new_target"]:
        return "switched"
    if live_target == row["old_target"] or (
            row["old_kind"] == "absent" and live_target is None):
        return "not_switched"
    return "foreign"
```

### Step 1-c: green を確認する (reconcile はまだ触らない)

**T1 は分類器を「置くだけ」で、呼び出し元は 1 行も変えない。** reconcile への載せ替えは
**T4 でまとめて行う** — 同じ行 (`live_target == ...` の分岐) を T1 と T4 で 2 度書き換えると
task ごとの diff が重なり、逐語転写と機械 diff が成立しないため (起草時の構成を改めた)。

- [ ] green: `uv run pytest tests/plugin/test_switch_ops_hardening.py tests/plugin/test_reconcile.py tests/plugin/test_switch_journal.py -q`
      → 新規 2 本が緑、**既存 2 ファイルは無改変で緑** (分類器は誰からも呼ばれていないので当然)

### Step 1-d: 逆変異

| # | ファイル / 関数 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T1-M1 | `switch.py` / `classify_live` | `live_target = live.readlink().as_posix() if live.is_symlink() else None` | `live_target = (live.resolve().relative_to(plugins_root.resolve()).as_posix() if live.is_symlink() else None)` | `test_ac5_classifier_compares_the_raw_readlink_string` |
| T1-M2 | `switch.py` / `classify_live` | `if row["phase"] != "switched" or not row["switch_required"]:` | `if False:` | `test_classifier_refuses_rows_it_does_not_apply_to` |
| T1-M3 (**KILLED**、着手前検証で実走) | `switch.py` / `classify_live` | `        return "not_switched"` (`old_kind == "absent"` の枝を含む `if` の本体) | `        return "foreign"` | `tests/plugin/test_switch_journal.py::test_switched_recovery_absent_old_kind_with_no_live_reverts` (**T4 で reconcile が分類器を使うようになってから有効** — T1 時点では分類器が誰からも呼ばれないので無効変異。T4 の Step 4-d で回す) |

- [ ] T1-M1 / T1-M2 が red になることを確認し、**`cp` 退避から復元**する (`git checkout` 不可)
- [ ] T1-M3 は T4 へ繰り越す (T1 時点では無効変異)

### Step 1-e: 機械 diff と commit

- [ ] `diff` でプラン本文のブロックと実ファイルの当該範囲が一致することを確認する
- [ ] `git add -A && git commit` (メッセージ: `feat(switch-ops): live 分類器を切り出し reconcile を載せ替える (T1、設計書 §3.1)`)

---

## T2: 2 段 phase ガード

**担当**: 設計書 §3.2「ガードを置く位置」。`_advance_to_decided` の**入口**で終端行を弾き
(FS より前)、`_finalize_decision` で**期待 phase** を検査する (0d-2a はこちらしか通らない)。

### Step 2-a: テストを置いて red を確認する

- [ ] `test_ac16a_entry_guard_refuses_terminal_row_before_touching_fs` /
      `test_ac16a_finalize_guard_refuses_wrong_phase` /
      `test_ac16b_switch_required_zero_reaches_decided` を転写する
- [ ] red を確認する (**着手前検証で実走、逐語**): `E       Failed: DID NOT RAISE ValueError`
      → `2 failed, 3 passed in 5.42s`。**red は 3 本中 2 本だけ** —
      `test_ac16b_switch_required_zero_reaches_decided` は**ガード導入前から緑**
      (`switch_required=0` の正規経路を pin するテストなので当然。このテストは
      「ガードを常に `switched` 期待にする」fail open 変異 T2-M2 の killer として働く)

### Step 2-b: 実装を転写する

- [ ] 下の diff の **T2 部分** (`_finalize_decision` 冒頭の `guard_row` ブロックと、
      `_advance_to_decided` 冒頭の `entry_row` ブロック) を当てる
- [ ] **期待 phase は `switch_required` で決まる**。`phase in ("switched", "recorded")` と
      書くと `switch_required=1` の行が `recorded` のまま決定できて **fail open** になる

### Step 2-c: green + 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T2-M1 | `if entry_row is None or entry_row["phase"] in _TERMINAL_PHASES:` | `if False:` | `test_ac16a_entry_guard_refuses_terminal_row_before_touching_fs` |
| T2-M2 | `expected_phase = "switched" if guard_row["switch_required"] else "recorded"` | `expected_phase = "switched"` | `test_ac16b_switch_required_zero_reaches_decided` |
| T2-M3 | 同上 | `expected_phase = guard_row["phase"]` | `test_ac16a_finalize_guard_refuses_wrong_phase` |
| T2-M4 (**KILLED**、着手前検証で実走) | `_finalize_decision` の `guard_row` ブロック全体 | 削除 | `test_ac16a_finalize_guard_refuses_wrong_phase` |

- [ ] commit: `feat(switch-ops): _advance_to_decided 入口と _finalize_decision の 2 段 phase ガード (T2、設計書 §3.2)`

---

## T3: 0d の案 C + **lock 内 outcome の戻り値** (v1.1 で範囲を訂正)

**担当**: 設計書 §3.2 の 0d-1 / 0d-2a / 0d-2b / 0d-2c **と §3.5 の `switch.py` 側**
(`approve_candidate` / `retry_approval` が `ApprovalOutcome` を返す)。**本束の中心**。
案 C と「lock 内で確定した outcome を返す」は**同じ 1 つの挙動変更**なので 1 task にまとめる
(v1.0 は T3 / T5 に割っていたが、T3 の受入テストが戻り値を assert するため中間段が
green にならないことを着手前検証で実測した)。

### Step 3-a: テストファイルを完成させて red を確認する

- [ ] **v1.1 訂正**: 付録 C の完成形のうち、**T4 の 6 本を除いた 13 本**を置く
      (T1 の 2 本 + T2 の 3 本 + 本 task の `test_ac1` / `test_ac3` / `test_ac7` / `test_ac8` +
      **`test_ac14d_no_bare_return_in_approval_entrypoints` /
      `test_ac14d_outcomes_are_known_enum_values` /
      `test_ac14d_still_pending_on_missing_candidate` /
      `test_ac14a_already_decided_status_comes_from_the_row`**)。
      除くのは `test_ac2_*` / `test_ac9a_*` / `test_ac9b_i/ii/iii_*` / `test_ac9c_*` の 6 本で、
      これは **T4 の Step 4-a で足してファイルを完成形にする**。
      v1.0 は「ここで完成形どおりに仕上げる」と書いていたが、それだと T4 の 6 本が
      T3 で red のまま残り、Step 4-a の「完成形のファイルに既に含まれている」と矛盾する
      (着手前検証で実測)。**転写後は機械 diff で一致を確認する**
- [ ] red を確認する (**着手前検証で実走、訂正後の分割で測り直した値**):
      `E       AssertionError: assert False` / `E        +  where False = is_symlink()`
      (`test_ac1`)、`E           Failed: DID NOT RAISE OSError` (`test_ac7`)、
      `E       FileNotFoundError: [Errno 2] No such file or directory: '.../plugins/sma'` (`test_ac8`)、
      `E           AssertionError: approve_candidate: outcome を返さない return が [1631, 1605, ...] 行目にある` (`test_ac14d_no_bare_return_*`)、
      `E       AttributeError: 'NoneType' object has no attribute 'outcome'` (`test_ac14a_*` ほか)
      → **`8 failed, 5 passed in 14.12s`** (13 本中 8 本が red。緑の 5 本 = T1 の 2 本 + T2 の 3 本)。
      *参考: v1.0 の分割 (戻り値 8 hunk と 4 本が T5 だった形) では `4 failed, 5 passed in 10.37s`*

### Step 3-b: 実装を転写する

- [ ] 付録 A の **T3 部分**を当てる。**v1.1 訂正で 3 hunk → 11 hunk**:
      - 案 C 本体 = `@@ -1439,6` (`rolled_back_op_id` の導入) / `@@ -1447,13` (0d の
        `live_class` 分岐) / `@@ -1477,8` (`_close_own_unfinished_journal_if_any` のコメント)
      - **outcome 戻り値** (v1.0 では T5 だった 8 hunk) = `@@ -1366,7` / `@@ -1398,7` /
        `@@ -1414,7` / `@@ -1500,7` / `@@ -1517,12` / `@@ -1548,7` / `@@ -1568,6` / `@@ -1642,16`
      - 理由: `test_ac1` / `test_ac3` / `test_ac8` は `retry_approval` の**戻り値**を assert する。
        戻り値の配線が無いと `AttributeError: 'NoneType' object has no attribute 'outcome'` で
        red のまま残る (着手前検証で実測: `3 failed, 6 passed`)
- [ ] **`op_id = None` のリセットを落とさない** — 落とすと停止行の `reverted` が
      上書きされ、T2 の入口ガードで `ValueError` になる (設計書 AC-6)

### Step 3-c: green

```
uv run pytest tests/plugin/test_switch_ops_hardening.py -q
uv run pytest tests/plugin/test_switch_paths.py tests/plugin/test_switch_journal.py -q
```

- [ ] green の実測値 (訂正後の分割): **`13 passed in 14.28s`**。この時点の既存テスト群
      (`tests/plugin tests/test_commands.py tests/backtest/test_cli.py tests/test_service_app.py`) は
      **`925 passed, 1 deselected, 0 failed in 229.45s`** — **1 本も壊れない**

- [ ] **訂正 (v1.1、着手前検証で実測)**: `tests/plugin/test_indicator_initial_set.py::
      test_runbook_post_gate_failure_converges_via_approval_retry` は **T3 では red にならない**
      (この task は `commands.py` を触らないので、シェルの戻り文言 `approval #N を再試行しました` が
      そのまま成立する)。この書き換えは **T5 の材料**に移した (付録 F-1)。T3 時点の
      `tests/plugin` は**この 1 本も含めて無改変で緑**

### Step 3-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T3-M1 (8 月設計書の変異「`switched` の復旧を常に完遂にする」を 0d に注ぐ) | `live_class = classify_live(plugins_root, existing_journal)` | `live_class = "switched"` | `test_ac1_retry_of_switched_but_unswitched_deploys` |
| T3-M2 | `rolled_back_op_id = op_id` + 次行 `op_id = None` | `rolled_back_op_id = op_id` のみ (`op_id = None` を削る) | `test_ac1_retry_of_switched_but_unswitched_deploys` |
| T3-M3 | `if live_class == "foreign":` | `if False:` | `test_ac3_foreign_live_is_never_touched` |
| T3-M4 (**KILLED**、着手前検証で実走) | `_revert_one(conn, existing_journal, ...)` (0d-2c) | 削除 (巻き戻さずに新規経路へ) | `test_ac1_...` (旧行が `switched` のまま残り UNIQUE index に当たる) |
| T3-M5 (**KILLED**、着手前検証で実走) | `return "foreign"` (`classify_live`) | `return "not_switched"` | `test_ac3_foreign_live_is_never_touched` |

- [ ] commit: `feat(switch-ops): 0d の switched 行を分類して巻き戻し + 頭から再実行する (T3、設計書 §3.2 案 C)`

---

## T4: reconcile の巻き戻しに lock + lock 内の再読を入れる

**担当**: 設計書 §3.3。**巻き戻しは 3 箇所** (`force_revert` / pin 破れ / `not_switched`)。

### Step 4-a: テストを置いて red を確認する

- [ ] `test_ac2_...` / `test_ac9a_...` / `test_ac9b_i/ii/iii_...` / `test_ac9c_...` の **6 本を足して
      `tests/plugin/test_switch_ops_hardening.py` を付録 C の完成形にする** (v1.1 訂正 —
      v1.0 は「完成形のファイルに既に含まれている」と書いていたが、完成形を T3 で置くと
      この 6 本が T3 で red のまま残る)。**機械 diff で完成形との一致を確認する**
- [ ] red を確認する (**着手前検証で実走、逐語**):
      `E       AssertionError: 巻き戻しは lock の内側で行われていない` /
      `E       AssertionError: 第三者の symlink を消してはならない` /
      `E       AssertionError: stale 行で巻き戻され、配備が消えた (IV-3 の破れ)`
      → **`5 failed, 14 passed in 21.58s`** (訂正後の分割。red は T4 の 6 本のうち
      `test_ac2_*` を除く 5 本 — `test_ac2` は T3 の実装だけで緑になる)。
      *参考: v1.0 の分割では `8 failed, 26 passed in 18.12s`*

### Step 4-b: 実装を転写する

- [ ] `_revert_under_lock` を `reconcile_switch_journals` の**直前**に挿入する

```python path=src/agentic_fx/plugin/switch.py anchor=before:def reconcile_switch_journals
def _revert_under_lock(conn: sqlite3.Connection, row: dict, *, plugins_root: Path,
                       now: datetime, activity: "ActivityLog | None",
                       expect_class: str | None) -> bool:
    """[switch-ops-hardening] T4 (設計書 §3.3.2): 巻き戻しは必ず
    (1) name lock を取り (2) `op_id` で journal 行を取り直し (3) (分類を使う枝なら)
    live を読み直して分類をやり直し、(4) すべて一致したときだけ `_revert_one` する。

    lock を取るだけでは足りない — lock 待ちの間に別プロセスの `approval retry` が
    同じ行を畳んで配備を完了していると、手元の古い行で live を巻き戻して
    「approved だが未配備」を作ってしまう (r1 Critical 1、probe で実測)。

    `expect_class=None` は `force_revert_op_id` 経路 — 「phase に依らず巻き戻す
    割込」という既存の意味論を保つため、**行の再取得だけ**を行い分類は見ない。"""
    with _plugin_lock(plugins_root, row["name"]):
        fresh = journal_store.get(conn, row["op_id"])
        phase_now = fresh["phase"] if fresh is not None else None
        if fresh is None or phase_now in _TERMINAL_PHASES or phase_now != row["phase"]:
            if activity is not None:
                activity.write(
                    Category.APPROVAL, "switch_reconcile_skipped_stale_row",
                    f"name={row['name']} op_id={row['op_id']} "
                    f"phase_before={row['phase']} phase_now={phase_now}")
            return False
        if expect_class is not None:
            class_now = classify_live(plugins_root, fresh)
            if class_now != expect_class:
                if activity is not None:
                    activity.write(
                        Category.APPROVAL, "switch_reconcile_skipped_stale_row",
                        f"name={row['name']} op_id={row['op_id']} "
                        f"phase_before={row['phase']} phase_now={phase_now} "
                        f"class_before={expect_class} class_now={class_now}")
                return False
        _revert_one(conn, fresh, plugins_root=plugins_root, now=now, activity=activity)
        conn.commit()
        return True
```

- [ ] 付録 A の diff のうち `reconcile_switch_journals` のハンクを当てる。内容は 2 つ:
      (a) **分類器への載せ替え** (`live = plugins_root / row["name"]` 〜 `live_target` の直読みを
      `live_class = classify_live(plugins_root, row)` に、`elif live_target == old_norm ...` を
      `elif live_class == "not_switched"` に)、(b) **巻き戻し 3 箇所を `_revert_under_lock` へ**
- [ ] **`live == new_target` → `retry_approval` の枝は包まない** (flock 非再入 = 自己デッドロック)

### Step 4-c: green

- [ ] green の実測値 (訂正後の分割): **`19 passed in 21.76s`** (付録 C の 19 本が全て緑)
- [ ] `tests/plugin/test_reconcile.py::test_reconcile_resolution_holds_the_dependency_locks`
      が red になる (**想定内 — 書き換え対象 2 本目**。pin 破れの巻き戻しに lock が
      1 本増えるため)。付録 G の書き換え後の形へ直す → **`38 passed in 21.66s`**
- [ ] この時点の既存テスト群: **`931 passed, 1 deselected, 0 failed in 244.46s`**

### Step 4-d: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T4-M1 | `fresh = journal_store.get(conn, row["op_id"])` | `fresh = row` | `test_ac9b_iii_phase_changed_live_same` |
| T4-M2 | `if expect_class is not None:` | `if False:` | `test_ac9b_ii_live_changed_phase_same` |
| T4-M3 | `with _plugin_lock(plugins_root, row["name"]):` (`_revert_under_lock`) | `if True:` | `test_ac9a_reconcile_revert_holds_the_plugin_lock` |
| T4-M4 | `if fresh is None or phase_now in _TERMINAL_PHASES or phase_now != row["phase"]:` | `if fresh is None:` | `test_ac9b_iii_phase_changed_live_same` |
| T4-M5 (**KILLED**、着手前検証で実走) | force revert 分岐の `_revert_under_lock(..., expect_class=None)` | `_revert_one(conn, row, ...)` + `conn.commit()` (旧実装) | `test_ac9c_force_revert_takes_the_lock_and_refetches` |

- [ ] commit: `feat(switch-ops): reconcile の巻き戻し 3 箇所に lock + 行/分類の再読を入れる (T4、設計書 §3.3)`

---

## T5: シェルの結果報告 (**commands.py のみ**、v1.1 で範囲を訂正)

**担当**: 設計書 §3.5 のうち**シェル側**。`approve_candidate` / `retry_approval` が
**lock 内で確定した** `ApprovalOutcome` を返す部分 (`switch.py`) は **T3 に移った** ので、
この task はシェルがそれを文言に写すところだけを担当する。
**File Structure の `switch.py` 欄の「T1〜T5」は、訂正後は「T1〜T4」と読むこと。**

### Step 5-a: テストを置いて red を確認する

- [ ] **v1.1 訂正: switch 側の `test_ac14d_*` / `test_ac14a_*` は T3 へ移った。**
      この task で置くのは (a) **付録 D-1** (`tests/test_commands.py` の spy 書き換え +
      retry の結果報告 7 本) と (b) **付録 F-1** (`tests/plugin/test_indicator_initial_set.py::
      test_runbook_post_gate_failure_converges_via_approval_retry` の書き換え —
      **v1.0 は T3 と書いていたが、T3 では red にならないことを実測した**) の 2 つだけ
- [ ] red を確認する (**起草時 + 着手前検証の両方で実走、逐語**):
      `assert '再試行' in "エラー: AttributeError: 'NoneType' object has no attribute 'outcome'"`
      → **`11 failed, 70 passed in 45.92s`** (訂正後の分割。`tests/test_commands.py` の 10 本 +
      `tests/plugin/test_indicator_initial_set.py::test_runbook_post_gate_failure_converges_via_approval_retry`)。
      *参考: v1.0 の分割 (switch 側の 4 本も T5 に居た形) では `18 failed, 82 passed in 66.95s`*

### Step 5-b: 実装を転写する

- [ ] **v1.1 訂正: この task は `commands.py` しか触らない。** 当てるのは **付録 B-1**
      (`outcome` の受け取り + `_retry_outcome_text`) だけ。`switch.py` の
      `ApprovalOutcome` 戻り値は **T3 で入っている**
- [ ] **AST 検査は入れ子関数を除外する** — `_close_own_unfinished_journal_if_any` は
      内部 helper なので bare `return` を持ってよい (テスト側の `_walk_own_body` が担保。
      **この受入条件のテストは T3 にある**、設計書 AC-14d の但し書き)

### Step 5-c: green + 既存 spy の書き換え

- [ ] `tests/test_commands.py::test_approval_retry_dispatches_to_switch_retry_approval`
      が red になる (**想定内 — 書き換え対象 3 本目**。付録 D-1 に含まれる)。spy が `None` を返す
      「実物より緩い fake」なので、**実物と同じ `ApprovalOutcome` を返す**よう直す
      ([[test-fixtures-from-real-transcripts]])

### Step 5-d: 逆変異

| # | ファイル | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T5-M1 | `switch.py` | `return ApprovalOutcome(  # pending のまま (§8.1-29)` 以下 4 行 | `return  # pending のまま (§8.1-29)` | `test_ac14d_no_bare_return_in_approval_entrypoints` / `test_ac14d_still_pending_on_missing_candidate` |
| T5-M2 | `switch.py` | `outcome=("deployed_after_rollback" if rolled_back_op_id is not None else "deployed")` | `outcome="deployed"` | `test_ac1_retry_of_switched_but_unswitched_deploys` |
| T5-M3 | `commands.py` | `raise ValueError(f"unknown approval outcome: {kind!r}")` | `return "結果を判別できませんでした"` | `test_approval_retry_fails_loud_on_unknown_enum_value` |
| T5-M4 | `commands.py` | `f"{self._retry_outcome_text(outcome)}"` | `"再試行しました"` (固定文言に戻す) | `test_approval_retry_reports_the_locked_outcome` |
| T5-M5 (**KILLED**、着手前検証で実走) | `commands.py` | `outcome = plugin_switch.retry_approval(...)` の受け取り | 受け取りはするが**文言を lock の外で FS から作り直す** (v1.1 の案を適用可能な形に具体化 — `return (f"approval #{approval_id} を再試行しました: " f"{self._retry_outcome_text(outcome)}")` を `_live = self.plugins_root / outcome.name` → `_t = _live.readlink().as_posix() if _live.is_symlink() else None` → `return (f"approval #{approval_id} を再試行しました: " f"配備まで完了しました (plugins/{outcome.name} → {_t})")` に置換) | `test_approval_retry_message_unaffected_by_later_deployment` |
| T5-M6 | `switch.py` | `status=row["status"]` (`already_decided`) | `status="approved"` | `test_approval_retry_reports_the_locked_outcome[already_decided]` |

- [ ] green の実測値 (訂正後の分割): **`81 passed in 47.16s`**。この時点の既存テスト群は
      **`941 passed, 1 deselected, 0 failed in 234.32s`**
- [ ] commit: `feat(switch-ops): シェルが lock 内 outcome を文言に写す (T5、設計書 §3.5)`

---

## T6: CLI の例外の扱いを `_plugin_retire` に揃える

**担当**: 設計書 §3.4。`switch.py` を触らないので **T1〜T5 と並列可**。

### Step 6-a: テストを置いて red を確認する

- [ ] `tests/backtest/test_cli.py` に `test_plugin_materialize_containment_error_is_rc1_message`
      を追記する (末尾の「`tests/backtest/test_cli.py` への追記」)
- [ ] `tests/plugin/test_indicator_initial_set.py::test_runbook_cli_bless_after_post_gate_failure_raises_traceback`
      を**書き換え後の形**に直す (**書き換え対象 4 本目**)
- [ ] red を確認する (**着手前検証で実走、逐語**):
      `E       ValueError: materialize_plugin: live symlink target escapes plugins_root: /x` (materialize 側) /
      `E               agentic_fx.plugin.switch.UnresolvedJournalError: plugin 'sma': an unresolved switch journal (op_id=1, approval_id=1) blocks bless — resolve it first (reconcile or approval retry)` (bless 側の素通り)
      → `2 failed, 83 passed in 58.69s`

### Step 6-b: 実装を転写する

```diff path=src/agentic_fx/backtest/cli.py
--- src/agentic_fx/backtest/cli.py
+++ src/agentic_fx/backtest/cli.py
@@ -586,6 +586,17 @@
             conn, name=args.name, human_dir=human_dir, settings=settings,
             now=datetime.now(timezone.utc), decided_by="human_cli",
             on_floor_warning=_warn, activity=activity)
+    except plugin_switch.UnresolvedJournalError as e:
+        # [switch-ops-hardening] T6: `UnresolvedJournalError` は `Exception`
+        # 直系なので `(ValueError, SandboxError)` にも外側の包括 catch にも
+        # 掛からず traceback になっていた。`_plugin_retire` (同ファイル) と
+        # 同じ作法 (rc=1 + `エラー: `) に揃え、**次の 1 手**を添える。
+        # 元メッセージの `op_id=` / `approval_id=` は runbook と既存テストが
+        # 依存しているので必ず含める。
+        print(f"エラー: {e}\n"
+              "  収束手順: サービスの対話シェルで `approval list` → "
+              "`approval retry <approval_id>`", file=sys.stderr)
+        return 1
     except (ValueError, plugin_sandbox.SandboxError) as e:
         print(f"エラー: {e}", file=sys.stderr)
         return 1
@@ -597,7 +608,10 @@
     plugins_dir = root / "plugins"
     try:
         dest = plugin_switch.materialize_plugin(plugins_dir, args.name)
-    except (FileExistsError, FileNotFoundError, OSError) as e:
+    except (FileExistsError, FileNotFoundError, OSError, ValueError) as e:
+        # [switch-ops-hardening] T6: `materialize_plugin` は live symlink が
+        # plugins_root の外を指すとき `ValueError` を投げる (containment 検査)。
+        # 従来は外側の包括 catch に落ちてメッセージ形式だけが不揃いだった。
         print(f"エラー: {e}", file=sys.stderr)
         return 1
     print(f"materialize: {dest}")
```

### Step 6-c: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T6-M1 | `except plugin_switch.UnresolvedJournalError as e:` | (この except 節ごと削除) | `test_runbook_cli_bless_after_post_gate_failure_raises_traceback` |
| T6-M2 | `except (FileExistsError, FileNotFoundError, OSError, ValueError) as e:` | `except (FileExistsError, FileNotFoundError, OSError) as e:` | `test_plugin_materialize_containment_error_is_rc1_message` |
| T6-M3 | `print(f"エラー: {e}\n  収束手順: ...")` | `print(f"エラー: {e}")` (次の 1 手を落とす) | `test_runbook_cli_bless_...` (`assert "approval retry" in err2`) |
| T6-M4 (**KILLED**、着手前検証で実走) | `return 1` (bless の新 except) | `return 0` | 同上 (`assert rc2 == 1`) |

- [ ] commit: `fix(switch-ops): afx plugin bless / materialize の例外を retire と同じ作法に揃える (T6、設計書 §3.4)`

---

## T7: 対話シェルの `approval list`

**担当**: 設計書 §3.5。`commands.py` を触るので **T5 の後に直列**。

### Step 7-a: テストを置いて red を確認する

- [ ] `tests/test_commands.py` に `approval list` 群 (AC-12a/b/c、AC-13) を追記する
- [ ] red を確認する (**着手前検証で実走**): `approval list` が `_HELP` に落ちるため
      `assert out == "承認待ちはありません"` が `'コマンド一覧:\n  status ...'` と比較されて失敗
      → `5 failed, 63 passed in 2.79s`

### Step 7-b: 実装を転写する

- [ ] 下の diff の **T7 部分** (`_HELP` の 1 行、dispatch の分岐、`_approval_list` /
      `_open_journal_lines`) を当てる
- [ ] **`approval <id>` (`isdigit()` 分岐) と `approval retry <id>` を壊さない**

### Step 7-c: 逆変異

| # | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|
| T7-M1 | `and args[0] == "list" and len(args) <= 2` | `and args == ["list"]` | `test_approval_list_limit_and_cap` |
| T7-M2 | `if limit > self._APPROVAL_LIST_MAX:` ブロック | 削除 | `test_approval_list_limit_and_cap` |
| T7-M3 | `if limit <= 0:` / `except ValueError:` の `usage` | `limit = self._APPROVAL_LIST_DEFAULT` | `test_approval_list_rejects_bad_argument` |
| T7-M4 | `if journal_lines:` | `if True:` (0 件でも節を出す) | `test_approval_list_empty_message` |
| T7-M5 | `ORDER BY id ASC` | `ORDER BY id DESC` | `test_approval_list_shows_pending_ids_and_no_metrics` |

- [ ] commit: `feat(switch-ops): 対話シェルに approval list を足す (T7、設計書 §3.5)`

---

## T9: シェルの `approve <id>` も lock 内 outcome を写す (**commands.py のみ**、v1.2 新設)

**担当**: 設計書 §3.6 / AC-17a〜AC-17d。段 0 が報告した設計判断 1 (2026-09-20 ユーザー裁定 R9)。
**触るのは `commands.py` と `tests/test_commands.py` だけ。`switch.py` は 1 行も変えない。**

### Step 9-a: テストを置いて red を確認する

- [ ] `tests/test_commands.py` に 6 本を追記する (下記)。**既存テストは 1 本も書き換えない。**
      `_fake_outcome` ヘルパ (`:552-557`) は既存のものを再利用する。

| # | テスト名 | AC | 何を観測するか |
|---|---|---|---|
| 1 | `test_approve_plugin_reports_the_locked_outcome` (7 パラメタ) | AC-17a | `approve_candidate` の spy が 7 種の outcome を返すとき、戻り文字列が設計書 §3.6.3 の**逐語** (接頭辞込み、`==` で完全一致) |
| 2 | `test_approve_plugin_writes_activity_only_when_deployed` (7 パラメタ) | AC-17b | `activity.tail(10, Category.APPROVAL)` に `approved` を含む行が出るのは `deployed` / `deployed_after_rollback` のときだけ。**`already_decided` かつ `status="approved"` でも出ない** |
| 3 | `test_approve_plugin_message_unaffected_by_later_decision` | **AC-17c (主 killer)** | 下記の競合 fixture (競合者は**別コネクションで `approvals.apply_decision(..., "approved")`** を直接打つ — 期限切れ sweep / 別プロセスの CLI / もう一人の運用者を代表させる)。戻り文字列が**逐語一致**で `(status=pending): … (reason=candidate_missing)` のまま。activity にも `approved` が無い |
| 4 | `test_approve_plugin_fails_loud_on_unknown_outcome` | AC-17d | spy が `None` / 未知 enum 値を返すと `out.startswith("エラー: ")` かつ `"approval #" not in out.split("\n")[0]` (接頭辞が付かない) |
| 5 | `test_approve_plugin_outcome_text_is_shared_with_retry` | (共用の pin) | 同じ `ApprovalOutcome` を `approve` 経路と `approval retry` 経路に流し、**接頭辞を除いた残りが 1 文字も違わない**こと。文言生成の二重実装が入ると red |
| 6 | `test_approve_non_plugin_kind_is_unchanged` | §3.6.4 | `kind="tech_plugin"` の approve が `approval #<id> approved` (逐語) を返し、DB が `approved`、activity に `approved` が出る (= 非 plugin 枝が巻き添えにならない) |

- [ ] **AC-17c の fixture (逐語で書く)**: 競合者は**同一スレッド・別コネクション**でよい
      (`approve_candidate` が return した時点で `_plugin_locks` は解放済みなので、
      barrier もスレッドも要らない。既存 AC-14b のスレッド版より読みやすく、flock も競合しない)。

      ```python
      def test_approve_plugin_message_unaffected_by_later_decision(tmp_path, monkeypatch):
          """AC-17c: lock を抜けた後に別プロセスがこの approval を承認しても、
          シェルは **自分の呼び出しが返した outcome** を報告する。lock 外で
          `SELECT status` し直す実装 (v1.1 までの approve ハンドラ) では
          「自分が下していない決定」を自分の結果として報告してしまう。"""
          conn, _, activity, cmds = _commands(tmp_path)
          # ... plugins_root / settings を配線し、candidate_missing になる
          #     pending plugin approval を 1 本作る (既存
          #     test_approve_plugin_kind_reports_actual_outcome_not_always_approved
          #     の前半と同じ形) ...
          real = plugin_switch.approve_candidate

          def _then_competitor(*a, **kw):
              outcome = real(*a, **kw)          # lock はここで解放される
              # 競合者: 別コネクションで同じ approval を approved にする
              # `_commands(tmp_path)` の DB は **ファイル** `tmp_path/"t.db"`
              # (`tests/test_commands.py::_commands` が `connect(tmp_path / "t.db")`)
              # なので、第 2 コネクションが作れる (実測確認済、2026-09-20)。
              conn2 = connect(tmp_path / "t.db")
              approvals.apply_decision(conn2, approval_id, "approved",
                                       decided_by="competitor", now=NOW, commit=True)
              conn2.close()
              return outcome

          monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                              _then_competitor)

          out = cmds.dispatch(f"approve {approval_id}")

          # 逐語で見る。`"approved: " not in out` だけでは旧文言
          # `approval #N approved` (コロン無し) と区別できない。
          assert out == (
              f"approval #{approval_id} は今回の操作では承認されませんでした "
              f"(status=pending): approved になりませんでした (reason=candidate_missing)")
          assert not any("approved" in r for r in activity.tail(10, Category.APPROVAL))
      ```

      **確認済 (2026-09-20)**: `_commands(tmp_path)` は `connect(tmp_path / "t.db")` の
      **ファイル DB** なので第 2 コネクションが作れる。fixture の書き換えは不要。

- [ ] red を確認する (**逐語で貼る**)。予想される主な失敗:
      `AssertionError: assert 'approval #1 approved: 配備まで完了しました (...)' == 'approval #1 approved'`
      および `assert 'approved: ' not in 'approval #1 approved'`

### Step 9-b: 実装を転写する

- [ ] `commands.py:361` の **`_retry_outcome_text` を `_approval_outcome_text` に改名**する
      (中身は 1 行も変えない)。docstring の `T5` を `T5 / T9` に、
      「`ApprovalOutcome` を文言に写す」の前に「(`approve` / `approval retry` 共用)」を足す。
- [ ] `commands.py:161` の呼び出しを `self._approval_outcome_text(outcome)` に変える。
      **retry の接頭辞と文言は 1 文字も変えない。**
- [ ] `approve` ハンドラ (`commands.py:94-110`) の plugin 枝を差し替える。**置換前 (逐語)**:

      ```python
                      plugin_switch.approve_candidate(
                          self.conn, approval_id, decided_by="shell",
                          now=self.clock.now(), plugins_root=self.plugins_root,
                          settings=self.settings, activity=self.activity)
                      # 検収 m5 是正: `approve_candidate` は正常な主要経路として
                      # non-pending 以外にも pending 留置で return しうる
                      # (§5.1: legacy_plain_present / candidate_missing /
                      # hash 不一致 / 未完ジャーナル)。旧稿は結果を確認せず
                      # 無条件に「approved」と報告していた — 実際の到達状態を
                      # 読み直して報告する。
                      outcome = self.conn.execute(
                          "SELECT status, reason FROM approval_requests WHERE id=?",
                          (approval_id,)).fetchone()
                      if outcome is None or outcome["status"] != "approved":
                          status = outcome["status"] if outcome else "不明"
                          reason = (outcome["reason"] if outcome else None) or "-"
                          return (f"approval #{args[0]} は approved になりません"
                                 f"でした (status={status}, reason={reason})")
      ```

      **置換後 (逐語)**:

      ```python
                      # [switch-ops-hardening] T9 (設計書 §3.6、2026-09-20 裁定 R9):
                      # `approve_candidate` は **plugin flock の内側で確定した**
                      # `ApprovalOutcome` を返す。検収 m5 是正の「実際の到達状態を
                      # 読み直して報告する」形は、lock を抜けた後の DB を読むため
                      # **別プロセスの決定を自分の結果として報告しうる** (§3.6.1)。
                      # retry と同じく、写すだけにする。未知 / None は文言にせず
                      # 例外に落とす (fail loud)。
                      outcome = plugin_switch.approve_candidate(
                          self.conn, approval_id, decided_by="shell",
                          now=self.clock.now(), plugins_root=self.plugins_root,
                          settings=self.settings, activity=self.activity)
                      text = self._approval_outcome_text(outcome)
                      if outcome.outcome not in ("deployed", "deployed_after_rollback"):
                          # 「今回の操作では」= status が既に `approved` の
                          # 二重 approve (`already_decided`) でも文が矛盾しない
                          # ようにするため (§3.6.3 / §3.6.5 の 1)。
                          return (f"approval #{args[0]} は今回の操作では承認されません"
                                 f"でした (status={outcome.status}): {text}")
                      self.activity.write(Category.APPROVAL, "approved",
                                          f"#{args[0]} via shell", ref_id=args[0])
                      return f"approval #{args[0]} approved: {text}"
      ```

      **注意 (転写の落とし穴)**: 置換後の plugin 枝は **`return` で必ず抜ける**ので、
      以降の共通の `self.activity.write(...)` / `return f"approval #{args[0]} approved"`
      **には落ちない**。共通部分は**非 plugin 枝 (`else:`) 専用になる** — 削除も移動もせず、
      そのまま残すこと (§3.6.4 の 1 行目 = `test_approve` / `test_approve_non_plugin_kind_is_unchanged` が pin)。

- [ ] 機械 diff (`diff` で 0) を確認してから次へ。目視は不可 ([[transcription-must-be-machine-diffed]])。

### Step 9-c: green

- [ ] `uv run pytest tests/test_commands.py -q` が green。**書き換えた既存テストが 0 本**であることを、
      `git diff --stat tests/test_commands.py` が**追記のみ** (削除行 0) であることで確認する
      ([[spec-must-check-existing-guards]] — 検収は削除行から読む)
- [ ] `uv run pytest tests/ -q` (フルスイート) が green。**基準 (v1.2a 訂正) は T9 着手前 4278 passed
      → T9 後 4297 passed (+19。実測 `2f88df8` = 新規テスト関数 7 本、うち 2 本が 7 パラメタ化
      (7×2=14) + 非パラメタ化 5 本 = 収集項目 19 件)**

### Step 9-d: 逆変異 (**リストは下限**)

| # | ファイル | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T9-M1 | `commands.py` | `outcome = plugin_switch.approve_candidate(...)` 〜 `return f"approval #{args[0]} approved: {text}"` の全体 | **v1.1 の実装に戻す** (`approve_candidate(...)` の戻り値を捨て、`SELECT status, reason` で読み直す旧ブロック) | `test_approve_plugin_message_unaffected_by_later_decision` (**AC-17c = この task の存在理由**) |
| T9-M2 | `commands.py` | `if outcome.outcome not in ("deployed", "deployed_after_rollback"):` | `if False:` | `test_approve_plugin_reports_the_locked_outcome[still_pending]` / `test_approve_plugin_writes_activity_only_when_deployed[still_pending]` |
| T9-M3 | `commands.py` | `if outcome.outcome not in ("deployed", "deployed_after_rollback"):` | `if outcome.outcome != "deployed":` | `test_approve_plugin_reports_the_locked_outcome[deployed_after_rollback]` |
| T9-M4 | `commands.py` | 接頭辞全体 | `f"approval #{args[0]} は承認されませんでした: {text}"` (`(status=...)` と「今回の操作では」を削る) | `test_approve_plugin_reports_the_locked_outcome[*]` (逐語一致) + **既存の `test_approve_plugin_kind_reports_actual_outcome_not_always_approved` の `"pending" in out`** (二重の網) |
| T9-M4b | `commands.py` | `は今回の操作では承認されませんでした` | `は承認されませんでした` (限定句だけ削る) | `test_approve_plugin_reports_the_locked_outcome[already_decided]` (逐語一致。**`status=approved` なのに「承認されませんでした」と言う矛盾**を殺す) |
| T9-M5 | `commands.py` | `text = self._approval_outcome_text(outcome)` を使った戻り値 | `return f"approval #{args[0]} approved"` (固定文言に戻す) | `test_approve_plugin_reports_the_locked_outcome[deployed]` |
| T9-M6 | `commands.py` | `self.activity.write(Category.APPROVAL, "approved", ...)` を plugin 枝の **`return` の前**から**失敗枝の前**へ移す (= 全 outcome で書く) | (移動) | `test_approve_plugin_writes_activity_only_when_deployed[already_decided]` |
| T9-M7 | `commands.py` | `_approval_outcome_text` の `raise ValueError(...)` | `return "結果を判別できませんでした"` | `test_approve_plugin_fails_loud_on_unknown_outcome` (+ 既存の `test_approval_retry_fails_loud_on_unknown_outcome`) |
| T9-M8 | `commands.py` | approve 枝の `text = self._approval_outcome_text(outcome)` | approve 専用に文言を別実装する (例: `text = f"status={outcome.status}"`) | `test_approve_plugin_outcome_text_is_shared_with_retry` |

- [ ] commit: `feat(switch-ops): approve も lock 内 outcome を文言に写す (T9、設計書 §3.6)`

---

## T10: 巻き戻しの commit が lock 内で完了していることの pin (**テストのみ**、v1.2 新設)

**担当**: 設計書 §3.3.2 手順 4 / IV-6 / AC-9d。段 0 の未 pin **S0-54 / S0-55 / S0-75** を殺す。
**本体コードは 1 行も変えない** — 現実装は既に 5 箇所すべて lock 内 commit であることを
設計書 §3.3.2 の全数表が記録している。**もし実装が違っていたら、テストを緩めずに指揮者へ申告する。**

### Step 10-a: 観測点を決める (probe で先に確かめる)

- [ ] `tests/plugin/test_switch_ops_hardening.py` の DB は **`tmp_path/data/agentic.db` のファイル**
      (`_cli_env` が `db_store.connect(tmp_path / _DB)`) であり、**`db_store.connect` は WAL**
      (`store/db.py:353`)。よって**同一プロセスの第 2 コネクションから committed だけが見える** —
      これが観測装置になる。
- [ ] 第 2 コネクションのヘルパをファイル先頭のヘルパ群 (`_rows` / `_status` の隣) に足す:

      ```python
      def _phase_from_another_connection(root, op_id):
          """別コネクションから journal の phase を読む (AC-9d)。
          **commit されていない書込はここからは見えない** — これが
          「lock の内側で commit まで完了しているか」の観測装置になる。"""
          other = db_store.connect(root / _DB)
          try:
              row = journal_store.get(other, op_id)
              return row["phase"] if row is not None else None
          finally:
              other.close()
      ```

- [ ] **ActivityLog は DB を触らない** (`activity.py` はファイル追記) ことを確認する。
      触るなら `_revert_one` 内の `activity.write` が暗黙 commit を起こし、S0-54 が等価変異になる。
      **確認できなければ指揮者へ申告**して観測点を変える。

### Step 10-b: テストを置いて red を確認する (**変異を当ててから**)

**本体は変えないので、通常の red → green は取れない。** 代わりに
[[mutation-testing]] の流儀で「**変異を当てた状態で red、戻して green**」を確認する。

| # | テスト名 | 殺す変異 | 作り | 観測 |
|---|---|---|---|---|
| 1 | `test_ac9d_reconcile_revert_is_durable_from_another_connection` | **S0-54** (`_revert_under_lock` の `conn.commit()` を落とす) | `_stopped_at_switched` で `switched` / live 未切替を作り、`reconcile_switch_journals` を 1 回回す | `_phase_from_another_connection(root, op_id) == "reverted"`。同時に `_rows(conn) == [(op_id, "reverted")]` も見て「自分のコネクションからは見える」ことと区別する |
| 2 | `test_ac9d_revert_is_committed_before_the_lock_is_released` | **S0-55** (commit を `with _plugin_lock(...)` の外へ出す) | 既存の S0-87 pin (`test_ac9a_revert_runs_while_the_lock_is_still_held`、`:250`) と**同じ `_plugin_lock` spy の型**を使い、`finally` で**実 lock を解放する前に**別コネクションから phase を読む | 解放直前の観測が `"reverted"` (変異を当てると `"switched"`) |
| 3 | `test_ac9d_retry_rollback_is_durable_before_pending_return` | **S0-75** (0d-2c の `conn.commit()` を落とす) | `_stopped_at_switched` の後に**候補ディレクトリを消す** (`shutil.rmtree(root/"plugins"/"_human"/"sma")`) → `retry_approval` を 1 回。0d-2c が巻き戻して commit した直後に `CandidateMissingError` → `still_pending` で戻る (**その間に commit する箇所が無い**ことをコードで確認済: `_close_own_unfinished_journal_if_any` は `op_id is None` で即 return する) | 戻り値が `outcome="still_pending"` / `reason="candidate_missing"` / `rolled_back_op_id == op_id`。かつ `_phase_from_another_connection(root, op_id) == "reverted"` |

- [ ] **2 の spy の形 (逐語)** — 既存 `:250` の `_spy_lock` に観測を足すだけ:

      ```python
      @_ctx.contextmanager
      def _spy_lock(rt, name):
          with real_lock(rt, name):
              try:
                  yield
              finally:
                  # **実 lock を解放する前に**別コネクションから読む。
                  # commit が with の外に出ていると、ここではまだ見えない。
                  seen.append(_phase_from_another_connection(root, op_id))
      ```

      **例外を飲み込まないこと** (`finally` の中で例外を起こさない / `except` を書かない)。
      観測が失敗したら `seen` に `None` が入り、アサーションで落ちる形にする。

- [ ] 3 つとも、**まず正実装で green** を確認し、**次に §Step 10-c の変異を 1 つずつ当てて red**
      (逐語で貼る) → `cp` 退避から復元。`git checkout` / `git restore` は使わない。

### Step 10-c: 逆変異 (= この task の本体。3 件とも段 0 の SURVIVED)

| # | ファイル | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T10-M1 (S0-54) | `switch.py` `_revert_under_lock` | `        _revert_one(conn, fresh, plugins_root=plugins_root, now=now, activity=activity)\n        conn.commit()` | `        _revert_one(conn, fresh, plugins_root=plugins_root, now=now, activity=activity)` (commit を削る) | `test_ac9d_reconcile_revert_is_durable_from_another_connection` |
| T10-M2 (S0-55) | `switch.py` `_revert_under_lock` | 同上 (commit が `with` の内側) | commit を `with` ブロックの外に出し、`return True` の直前に置く | `test_ac9d_revert_is_committed_before_the_lock_is_released` |
| T10-M3 (S0-75) | `switch.py` 0d-2c | `                _revert_one(conn, existing_journal, plugins_root=plugins_root,\n                           now=now, activity=activity)\n                conn.commit()` | `conn.commit()` を削る | `test_ac9d_retry_rollback_is_durable_before_pending_return` |

- [ ] **副作用の確認**: T10-M1 / M3 を当てたとき、**他のどのテストが red になるか**も記録する。
      もし既存テストが既に殺しているなら段 0 の「未 pin」判定が誤っていたことになるので申告する
      (段 0 は「同一コネクションなので観測できない」と判定済み)。
- [ ] `uv run pytest tests/ -q` (フルスイート) が green。**基準 (v1.2a 訂正) は T9 後 4297 passed
      → T10 後 4300 passed (+3、新規 3 本)**。実測 `4300 passed, 17 deselected` (2026-09-20、
      HEAD `c1abf8d`)
- [ ] commit: `test(switch-ops): 巻き戻しの commit が lock 内で完了していることを別コネクションで pin (T10、AC-9d)`

---

## T11: 再開の前提が崩れていたら巻き戻して新しい `op_id` で流し直す (v1.3 新設、**v1.4 で述語を 3 つ組へ**)

**担当**: 設計書 §3.2「#### 再開の前提が崩れていたら…」/ §0 R11 + **R13** / AC-16c-1〜8。
**出所**: 2 周目 `/code-review high` の指摘 #1。**指揮者側の probe で再現済み**
(`tmp/review-20260920-soh/r2/cr1-probe.md`、probe 本体
`tmp/review-20260920-soh/r2/cr1-probe/test_probe_cr1.py` = `4 passed`)。

**この task で変える挙動は 1 つだけ**: 自分の未完 journal (`preparing` / `versioned` / `recorded`) を
再開するとき、**その回に再計算した `switch_required` が保存値と食い違ったら**、停止行を巻き戻して閉じ、
`op_id = None` / `rolled_back_op_id = <停止行>` にして新しい行で流し直す。
**食い違いが無いときの挙動は 1 バイトも変えない** (← これを守る網が `test_ac16c3_*`)。

**設計を変えない。** `_finalize_decision` の phase ガード (T2) は**緩めない**。
新しい outcome も新しい `reason` も作らない。`commands.py` は**触らない**。

### Step 11-a: probe を red の種として取り込む

- [ ] **既存ヘルパの現物 (起草時に `grep -n "^def _" tests/plugin/test_switch_ops_hardening.py` で確認済)**:
      `_cli_env` (`:36`) / `_fail_switch_live` (`:50`) / `_stopped_at_switched` (`:64`) /
      **`_rows` (`:80`)** / `_status` (`:85`) / `_phase_from_another_connection` (`:90`) /
      `_activity_text` (`:102`) / `_make_foreign` (`:193`) / `_activity` (`:233`) / `_walk_own_body` (`:582`)。
      **重複定義を作らず既存を使う。**
- [ ] **⚠ `_rows` のシグネチャが probe と違う** — 既存の `_rows` は **`(op_id, phase)` の 2 タプル**
      (`:80-82`)、probe の `_rows` は `(op_id, phase, switch_required)` の 3 タプル。
      **既存の 2 タプル版をそのまま使い、行数と phase だけを見る**。
      AC-16c-1 / 2 が要る `switch_required` / `old_kind` / `old_target` は
      **`journal_store.get(conn, outcome.op_id)` で新しい行を直接読んで assert する**。
      **既存 `_rows` を 3 タプルに書き換えない** (T1〜T10 の既存テストが全部 red になる)。
      黙って別名ヘルパを増やすのも不可 — この段落の形で書くこと
      ([[haiku-silently-adapts-report-deviations]])
- [ ] `tmp/review-20260920-soh/r2/cr1-probe/test_probe_cr1.py` から**ヘルパ 3 本を
      `tests/plugin/test_switch_ops_hardening.py` へ転写**する。新たに足すのは
      **`_fail_advance_at(monkeypatch, phase_to_fail)`** (指定 phase への `advance_switch_journal` を
      1 回だけ失敗させる) と **`_stopped_before(tmp_path, monkeypatch, fail_phase, expect_phase)`**
      (bless を途中で落として journal を `expect_phase` で止める) と
      **`_third_party_points_live_at(root, target)`** (live symlink を外から張り替える)。
      **転写は機械 diff する** ([[transcription-must-be-machine-diffed]]) — probe の当該関数だけを
      抜き出して `diff` で 0 を確認する
- [ ] **`fail_phase` → `expect_phase` の対応を取り違えないこと** (probe の parametrize を逐語で引き継ぐ):

      | `fail_phase` (advance を落とす phase) | 行が止まる `expect_phase` |
      |---|---|
      | `versioned` | `preparing` |
      | `recorded` | `versioned` |
      | `switched` | `recorded` |

- [ ] **⚠ この対応表が使えるのは `switch_required=1` の行だけ** (v1.4 の追記)。
      **`switch_required=0` の行は `advance(switched)` を一度も呼ばない**
      (`_advance_to_decided` の `if switch_required:` の内側にしかない) ので、
      **`_fail_advance_at("switched")` では止まらず最後まで完走してしまう**。
      `switch_required=0` の行を `recorded` で止めるには **`_finalize_decision` を落とす**
      (probe `test_probe_cr1.py:202` の `_boom` がその形)。
      → **テスト 1 / 3 / 6 (すべて `switch_required=1`) は上の対応表どおり、
      テスト 2 (`switch_required=0`) だけが別の止め方**になる。**取り違えると恒真テストになる**

      probe の実測では、停止 phase が `preparing` / `versioned` のときは **1 回目の retry が
      `recorded` まで前進してからそこで落ちる**ので、**3 通りとも同じ吸収状態**
      (`recorded` / `switch_required=1` / 非終端 1 本 / approval `pending`) に収束する
- [ ] **probe の assert は「欠陥が起きること」を pin している**。取り込むときは
      **目標状態へ反転する** (下表の「観測」列)。probe をそのまま置くと、
      実装後に **probe 側が red になって「直したのに赤い」混乱を生む**
- [ ] AC-16c-5 は **T10 が置いた `_phase_from_another_connection(root, op_id)` を再利用する**
      (新しい観測装置を作らない)

### Step 11-b: テストを置いて red を確認する

| # | テスト名 | AC | 作り | 観測 (= assert) |
|---|---|---|---|---|
| 1 | `test_ac16c1_resume_with_stale_switch_required_rolls_back_and_redeploys` (**3 parametrize**) | AC-16c-1 | `_stopped_before` で 3 通りに止める → `_third_party_points_live_at(root, new_target)` → `retry_approval` を **1 回** | `_status(conn, aid) == "approved"` / `_rows(conn)` が **2 行** (停止行 `reverted` + 新 `op_id` が `decided`) / 新行の `old_kind == "symlink"` かつ `old_target == new_target` かつ `switch_required == 0` / `live.readlink().as_posix() == new_target` (**不変**) / 戻り値 `outcome.outcome == "deployed_after_rollback"` かつ `outcome.rolled_back_op_id == 停止行の op_id` かつ `outcome.op_id == 新 op_id` |
| 2 | `test_ac16c2_reverse_stored_zero_recomputed_true_rolls_back` (**3 parametrize**、v1.4) | AC-16c-2 | **まず `switch_required=0` の行を作る** — 1 本目の候補を配備して live を `.versions/<name>/<hash>` へ向け、**同一候補を再 bless / 再 approve** する (これで `old_kind='symlink'` かつ `old_target == new_target` = `switch_required=0`)。**停止のさせ方は phase ごとに違う (下の注を読むこと)**: `preparing` ← `_fail_advance_at("versioned")` / `versioned` ← `_fail_advance_at("recorded")` / `recorded` ← **`_finalize_decision` を `_boom` で落とす** (probe `:202` の形)。そのあと live を外から**消す** → `retry_approval` | 新行の `switch_required == 1` / `live` が `new_target` を指す symlink になる / `_status == "approved"` / `_rows` が 2 行 |
| 3 | `test_ac16c3_resume_without_drift_reuses_the_same_op_id` (**v1.4: 3 parametrize。v1.8a で `switch_required` の両方 (0 と 1) に拡張 — codex sol r6 #3、計 6 parametrize**) | **AC-16c-3 (回帰防止)** | **`preparing` / `versioned` / `recorded` の 3 通り × `switch_required` の両方**で止め、**live を一切触らずに** `retry_approval`。`switch_required=1` 側は `_stopped_before` (テスト 1 と同じ止め方) / `switch_required=0` 側はテスト 2 と同じ作り (同一候補の再配備) で止め、**live は変更しない** (テスト 2 は live を消して drift させるのに対し、3 は live を触らない no-drift 版)。`switch_required=0` の停止は上の注のとおり `preparing`/`versioned` は `_fail_advance_at` で、`recorded` は `_finalize_decision` を `_boom` で落とす | いずれの組も: `_rows(conn)` が **1 行だけ**で、その `op_id` が**停止行と同じ**かつ `phase == "decided"` / `outcome.outcome == "deployed"` / `outcome.rolled_back_op_id is None` / activity に `switch_resume_precondition_changed` が **1 件も無い** |
| 4 | `test_ac16c4_precondition_change_leaves_a_dedicated_activity_event` | AC-16c-4 | 1 と同じ状態 (`fail_phase="switched"` の 1 通りでよい) | activity 行に `switch_resume_precondition_changed` が **1 本**あり、`name=` / `op_id=<停止行>` / `phase=recorded` / `stored_switch_required=1` / `recomputed=0` を含む。**その直後の行が `switch_reverted`** (= 2 行になる。「ちょうど 1 行」と書かない) / 停止行は最終的に `reverted` の**まま** |
| 5 | `test_ac16c5_resume_rollback_is_committed_inside_the_lock` | AC-16c-5 (IV-6) | 1 と同じ状態 + T10 の `_plugin_lock` spy (`finally` で**実 lock を解放する前に**別コネクションから読む)。**spy は `_stopped_*` の中の bless でも発火する** (そのとき停止行はまだ存在せず観測は `None`) ので、**`retry_approval` の呼び出し中に取れた観測だけを見る** (spy を仕掛けるのを retry の直前にするか、`seen` を retry 前にクリアする) | 解放直前の観測が `"reverted"` / かつ `grep` 相当の静的確認として `_revert_one(` の呼び出し元が **5 箇所のまま** (テストではなく Step 11-e のチェック項目で見る) |
| 6 | `test_ac16c6_plain_branch_closes_its_own_unfinished_journal` (**3 parametrize**、v1.4) | AC-16c-6 | **3 通り**で止め、live symlink を**削除して同名の plain ディレクトリを置く** → `retry_approval` | `outcome.outcome == "legacy_plain_present"` (**文言もフィールドも不変**) / `journal_store.list_non_terminal(conn)` が **空** / 続けて `bless_candidate` (または別 approval の `approve_candidate`) を呼んでも `UnresolvedJournalError` に**ならない** |
| 7 | `test_ac16c7_crash_between_rollback_commit_and_new_journal_converges` (**v1.4、codex r5 #4**) | AC-16c-7 | 1 と同じ状態 (`fail_phase="switched"` の 1 通りでよい) + **`begin_switch_journal` を 1 回だけ例外にする** monkeypatch → `retry_approval` (例外が上がる) → patch を外して **もう 1 回** `retry_approval` | 1 回目の直後: 停止行が **`reverted`**、`journal_store.list_non_terminal(conn)` が **空**、approval `pending`、live 不変。2 回目の後: `_rows(conn)` が **2 行** (旧 `reverted` + 新 `decided`)、approval `approved`、**`outcome.outcome == "deployed"` かつ `outcome.rolled_back_op_id is None`** (巻き戻したのは前回の呼び出しなので、今回は巻き戻していない) |
| 8 | `test_ac16c8_old_target_drift_is_detected` (**v1.4、R13 の主 killer**) | AC-16c-8 | `_stopped_before(..., "switched", "recorded")` で **`old_kind='symlink'` の行**を作り (= 先に別の版を配備してから 2 本目の候補を approve する)、第三者が live を **`old_target` でも `new_target` でもない第 3 の先 `Y`** へ張り替える → `retry_approval` | **保存も再計算も `switch_required=1` のまま**なのに巻き戻しが起きること: `_rows` が **2 行** (旧 `reverted` + 新 `decided`)、**新しい行の `old_target == Y`** (`journal_store.get` で読む)、`outcome.outcome == "deployed_after_rollback"`、live は `new_target`。**`switch_required` だけを比べる v1.7 の述語では巻き戻しが起きず 1 行のまま**なので、この 1 本が R13 の存在理由 |

- [ ] red の出力 (最終行) を逐語で貼る。**1 / 2 は `ValueError` で red になるはず** (probe と同じ文言:
      `op_id=N: _finalize_decision expects phase='switched' but found 'recorded'` /
      `op_id=N: switch_required=0 の行は 'switched' phase を経ない …`)。
      **3 は実装前から green** (回帰防止なので当然) — **green のまま置くことを Step のチェックで明示する**
- [ ] 6 は「非終端が残る」で red、4 は「イベントが無い」で red、5 は 1 が red なので巻き添えで red

### Step 11-c: 実装を転写する (`switch.py` の 3 hunk)

- [ ] **hunk 1** — `_close_own_unfinished_journal_if_any` (`:1632-1649`) の戻り値を
      `None` → `int | None` (閉じた `op_id`、閉じなければ `None`) にする。
      **既存の 3 つの呼び出し元 (`candidate_missing` / `snapshot_invalid` / `hash_mismatch`) は
      戻り値を使わないので 1 文字も変えない。** docstring の
      「**switched のうち `not_switched` は 0d-2c が既に閉じて…**」の段落に
      「**v1.7 / v1.8: 再開の前提 (3 つ組) が崩れた行もここで閉じる**」を足す
- [ ] **hunk 2 (判定、v1.8 で 1 箇所に統合)** — **v1.8a 訂正 (codex sol r6 D、現物照合)**:
      `new_target` は現物 (T11 着手前の HEAD) で**既に** `old_kind == "plain"` 分岐 (`:1702`)
      の直前 (`:1700`) にあり、**繰り上げ不要**。繰り上げるのは **`switch_required`** (`:1719`、
      plain 分岐の**後ろ**にある) の**1 行だけ** — `old_kind` / `old_target` を読んだ直後
      (`:1700` の直後、`new_target` の代入の後) へ移し、`if old_kind == "plain":` (`:1702`) の
      **直前**に判定を置く。
      **v1.7 が持っていた plain 分岐専用の hunk は不要**(`old_kind` の不一致として自動的に拾われる、設計書 §3.2):

      ```python
      if op_id is not None and (
              bool(existing_journal["switch_required"]),
              existing_journal["old_kind"],
              existing_journal["old_target"]) != (
                  switch_required, old_kind, old_target):
          # [switch-ops-hardening] T11 (設計書 §3.2 / R13): 再開の前提が
          # resume 時点で崩れている。journal の (switch_required, old_kind,
          # old_target) は**行の作成時**の live から決まった値で、store 層に
          # 更新 API は無い。同じ行を使い回すと (i)「切替の要否・実際の切替・
          # finalize の期待 phase」が別々の値を見て詰まり、(ii) old_target が
          # 古いままなので後の巻き戻しが第三者の変更を上書きして戻す。
          # 0d-2c と同形に、停止行を巻き戻して閉じ、新しい op_id で頭から
          # 流し直す。live が plain に化けた場合も old_kind の不一致として
          # ここで拾われる (journal の old_kind は absent/symlink のみ)。
          if activity is not None:
              activity.write(
                  Category.APPROVAL, "switch_resume_precondition_changed",
                  f"name={name} op_id={op_id} "
                  f"phase={existing_journal['phase']} "
                  f"stored=({int(bool(existing_journal['switch_required']))},"
                  f"{existing_journal['old_kind']},"
                  f"{existing_journal['old_target']}) "
                  f"now=({int(switch_required)},{old_kind},{old_target})")
          closed = _close_own_unfinished_journal_if_any()
          if closed is not None:
              rolled_back_op_id = closed
              op_id = None
      ```

- [ ] **繰り上げが挙動を変えないことを確認する** — `switch_required` は
      plain 分岐では**使われない**ので、計算位置を上げても観測される挙動は変わらない
      (`new_target` は元々 plain 分岐の前にあり移動しない)。**変わったら申告**
- [ ] **activity は helper の前に書く** (ログが因果の順に読める)。
      **`closed is None` の枝は足さない** — 到達不能で、万一そうなっても T2 の入口ガードが
      FS より前に `ValueError` で止める (fail closed、設計書 §3.2)
- [ ] **`existing_journal` がスコープに生きていることを確認する** — 0d で読んだ行
      (`:1564`) がそのまま使える。**再読しない** (lock 内で読んだ値だけを使う規律、§3.3.5)
- [ ] 転写後に**機械 diff で 0 を確認**する

### Step 11-d: green

```
uv run pytest tests/plugin/test_switch_ops_hardening.py -q
uv run pytest tests/plugin/test_switch_paths.py tests/plugin/test_switch_journal.py tests/plugin/test_materialize_retire.py -q
uv run pytest tests/test_commands.py tests/plugin/test_indicator_initial_set.py -q
```

- [ ] 新規 6 本が green。**既存テストが 1 本も red にならない** —
      特に `tests/plugin/test_switch_paths.py:677 / :1119 / :1160` (`recorded` からの retry、
      live を触らないので食い違いが生じない) と
      `tests/plugin/test_indicator_initial_set.py::test_runbook_post_gate_failure_converges_via_approval_retry`。
      **red になったら黙って直さず申告する** ([[plan-code-defects-not-implementer-defects]])

### Step 11-e: 逆変異 (**リストは下限**)

| # | ファイル / 箇所 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T11-M1 | `switch.py` hunk 2 | 判定の `if` 全体 | `if False:` (判定ごと落とす = 現行に戻す) | `test_ac16c1_*` (3 通りとも `ValueError`) / `test_ac16c2_*` / `test_ac16c6_*` |
| T11-M2 (**「常に巻き戻す」への退化を殺す**) | `switch.py` hunk 2 | 同上 | `if op_id is not None:` (食い違いを見ずに必ず巻き戻す) | **`test_ac16c3_resume_without_drift_reuses_the_same_op_id`**。**「これだけが殺す」は未実測** — `tests/plugin/test_switch_paths.py:677 / :1119 / :1160` も行数や `op_id` を pin していれば同時に red になりうる。**Step 11-e の副作用確認で全数を記録すること** |
| T11-M3 | `switch.py` hunk 2 | `op_id = None` | (この 1 行を削る) | `test_ac16c1_*` (停止行が `reverted` のまま再利用され、T2 の入口ガードが `ValueError`。journal は 1 行・`reverted`・live 不変で終わる) |
| T11-M4 | `switch.py` hunk 2 | `rolled_back_op_id = closed` | (この 1 行を削る) | `test_ac16c1_*` (`outcome.outcome` が `deployed` に化け、`rolled_back_op_id is None` になる) |
| T11-M5 | `switch.py` hunk 1 | `_close_own_unfinished_journal_if_any` の中の `_revert_one(...)` | 削除 (行を閉じずに `op_id` だけ返す) | `test_ac16c1_*` (停止行が非終端のまま残り、`begin_switch_journal` が部分 UNIQUE index に当たって `IntegrityError`) |
| T11-M6 | `switch.py` hunk 1 | `_close_own_unfinished_journal_if_any` の中の `conn.commit()` | 削除 | `test_ac16c5_resume_rollback_is_committed_inside_the_lock` (別コネクションから `reverted` が見えない) |
| T11-M7 (**v1.4 で意味が変わった**) | `switch.py` hunk 2 | 判定を `if old_kind == "plain":` の**後ろ**へ動かす (= v1.7 の配置に戻す) | 同上 | `test_ac16c6_*` (plain のケースだけ判定に届かず、非終端が 1 本残って続く `bless` が `UnresolvedJournalError`) |
| **T11-M10 (v1.4、R13)** | `switch.py` hunk 2 | 3 つ組の比較 | **`switch_required` だけ**比較する (v1.7 の述語に戻す) | `test_ac16c6_*` (plain は `switch_required` が両方 `True` になり得て判定をすり抜ける) + **`test_ac16c8_old_target_drift_is_detected`** (下記 8 本目) |
| **T11-M11 (v1.4、R13。v1.4b で実測に合わせて訂正)** | `switch.py` hunk 2 | 同上 | **`old_kind` だけ**比較する | **実測 (`tmp/wt/soh` HEAD `cd792f5`、`pytest tests/plugin/test_switch_ops_hardening.py -k ac16c`) の killer は `test_ac16c8_*` のみ** — `test_ac16c1_*` は本変異で green のまま残った (元記述の `test_ac16c1_*` は誤り) |
| **T11-M12 (v1.4、R13。v1.4b で実測に合わせて訂正)** | `switch.py` hunk 2 | 同上 | **`old_target` だけ**比較する | **実測の killer は `test_ac16c6_*` のみ** (3 parametrize とも red — `absent`↔`symlink`/`plain` の分岐で `old_kind` の差が拾われず判定をすり抜けるため)。元記述の `test_ac16c2_*` (`absent`↔`symlink` で `old_target` が両方 `None` になるケース) は本変異では green のまま残った — 誤り |
| T11-M8 | `switch.py` hunk 2 | `activity.write(..., "switch_resume_precondition_changed", ...)` のブロック | 削除 | `test_ac16c4_*` |
| T11-M9 | `switch.py` hunk 2 | `bool(existing_journal["switch_required"]) != switch_required` | `existing_journal["switch_required"] != switch_required` (`bool()` を落とす) | **等価変異 (実測で確認、v1.4b)**。`sqlite3` は `switch_required` を `int` (0/1) で返し、Python では `1 != True` も `0 != False` も `False` なので、**`bool()` の有無で判定は変わらない**。`pytest tests/plugin/test_switch_ops_hardening.py -k ac16c` は本変異で全件 green (19 passed)。**「殺せない変異」として記録し、`bool()` は可読性のために残す** ([[mutation-testing]] の規律 — 等価変異を無理に殺さない) |
| **T11-M6-scope (v1.4b、B の pin の動機)** | `switch.py` hunk 1 | `_close_own_unfinished_journal_if_any` の `conn.commit()` (`:1658`) | 削除 | **T11 の再開判定経路 (§Step 11-c) では等価** — 実測 (`-k "ac16c or ac9d"` 全件 green、`tests/plugin/test_switch_ops_hardening.py tests/plugin/test_switch_paths.py` フルでも全件 green) — 閉じた直後に処理が `op_id = None` のまま継続し、新しい journal 行を `begin_switch_journal(..., commit=True)` で作るため、この commit に吸収される。**ただし `candidate_missing` / `snapshot_invalid` / `hash_mismatch` の 3 経路は閉じた直後に `return` するため commit が続かず load-bearing** — 現状これを別コネクションから確かめる pin が無い (T14 Step 14-b の既存 pin 2 本も DB の状態変化は見るが「lock 内で commit まで終わっているか」は見ない)。**T14 Step 14-c 相当として B で 1 本追加する** (T10/AC-9d と同じ `_phase_from_another_connection` の流儀) |
| T11-M10 | `switch.py` `_finalize_decision` | `expected_phase = "switched" if guard_row["switch_required"] else "recorded"` | `expected_phase = guard_row["phase"]` (**ガードを緩める = 案 1 への退化**) | 既存 `test_ac16a_finalize_guard_refuses_wrong_phase` (T2)。**T11 でガードを緩めていないことの確認**として毎回回す |

- [ ] 各変異は `cp` 退避 → 適用 → 該当テストで red を逐語確認 → 復元。
      **`git checkout` / `git restore` は使わない** ([[no-git-checkout-over-uncommitted-subagent-work]])
- [ ] **副作用の確認 (T10 Step 10-c と同じ流儀)**: 各変異を当てたとき、**他のどのテストが
      red になるか**も記録する。特に **T11-M2** — 「`test_ac16c3_*` だけが殺す」という
      本プランの主張が正しいかを、`tests/plugin/` を丸ごと回して確かめる
- [ ] **静的確認**: `grep -n "_revert_one(" src/agentic_fx/plugin/switch.py` が **5 件のまま**
      (設計書 §3.3.2 の全数表と AC-9d が成立し続ける)。
      `grep -n "return" ` 相当は **AC-14d のテストが自動で見る** (`approve_candidate` に
      bare `return` / `return None` が増えていないこと) ので、
      **`test_ac14d_no_bare_return_in_approval_entrypoints` が green のままであることを確認する**
- [ ] `uv run pytest tests/ -q` (フルスイート) が green。
      **基準: T11 着手前 `4304 passed`。T11 後の値は実測で埋める** —
      テスト関数は 6 本だが `test_ac16c1_*` が 3 parametrize なので収集数はそれより多い。
      **予測値をここに書かない** (書くと実装者がそこに合わせてしまう)。
      **着手前の値が 4304 でなければ、先に指揮者へ申告する**
- [ ] commit: `fix(switch-ops): 再開時に switch_required が食い違ったら巻き戻して新しい op_id で流し直す (T11、設計書 §3.2 / AC-16c)`

---

## T12: `afx plugin retire` にも「次の 1 手」を添える (v1.3 新設、小)

**担当**: 設計書 §3.4 の retire 行 / §0 R12 / AC-18。
**出所**: 2 周目 `/code-review high` の指摘 #5 (同じ原因に同じ案内を出す)。

### Step 12-a: テストを置いて red を確認する

- [ ] `tests/backtest/test_cli.py` に 1 本追記する:
      `test_plugin_retire_unresolved_journal_is_rc1_with_next_step`。
      **既存の `tests/plugin/test_indicator_initial_set.py::test_runbook_cli_bless_after_post_gate_failure_raises_traceback`
      (T6 で書き換えた形。`tests/backtest/test_cli.py` ではなくこちらにある) と同じ作り**にする — `begin_switch_journal` で未終端行を作り、
      `main(["plugin", "retire", "<name>"])` 相当を呼び、`rc == 1` と stderr を見る
- [ ] 観測: `rc == 1` / `"op_id=" in err` / `"approval list" in err` / `"approval retry" in err` /
      **`"Traceback" not in err`**。**bless (AC-10) と同じ 2 行目の文字列**であることを
      **逐語一致**で assert する (文言が片方だけ変わる drift を殺す)
- [ ] red の最終行を逐語で貼る (現行は 2 行目が出ないので `assert "approval list" in err` で落ちる)
- [ ] **`(ValueError, OSError)` 側が不変であることの pin も同じテストか隣に置く** —
      retire が `ValueError` で失敗するケースは `rc == 1` + `エラー: ` のままで、
      **「次の 1 手」が付かない**こと (節を割っただけで捕捉集合が変わっていないことの観測)

### Step 12-b: 実装を転写する (`cli.py` の 1 hunk)

- [ ] `_plugin_retire` (`cli.py:621-632`) の `except` を 2 節に割る。
      **`UnresolvedJournalError` の節を先に置く** (`ValueError` / `OSError` とは継承関係が無いので
      順序で捕捉が変わることはないが、`_plugin_bless` と同じ並びに揃える):

      ```python
      except plugin_switch.UnresolvedJournalError as e:
          # [switch-ops-hardening] T12 (設計書 §3.4 / R12): 同じ原因
          # (未終端の切替ジャーナル) には `_plugin_bless` と同じ案内を出す。
          # `retire_plugin` の例外文言は `op_id=` のみで `approval_id=` を
          # 含まないので、**第一手が `approval list` であることに意味がある**。
          print(f"エラー: {e}\n"
                "  収束手順: サービスの対話シェルで `approval list` → "
                "`approval retry <approval_id>`", file=sys.stderr)
          return 1
      except (ValueError, OSError) as e:
          print(f"エラー: {e}", file=sys.stderr)
          return 1
      ```

- [ ] **2 行目は `_plugin_bless` (`cli.py:596-598`) からの逐語コピー**。
      **機械 diff で 2 箇所が一致することを確認する** (片方だけ直す drift を作らない)

### Step 12-c: green + 逆変異

| # | ファイル | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T12-M1 | `cli.py` | `except plugin_switch.UnresolvedJournalError as e:` (新設した節) | この節ごと削除 (`(ValueError, OSError)` を元の 3 タプルに戻す) | `test_plugin_retire_unresolved_journal_is_rc1_with_next_step` |
| T12-M2 | `cli.py` | `print(f"エラー: {e}\n  収束手順: ...")` (retire 側) | `print(f"エラー: {e}")` | 同上 (`assert "approval retry" in err`) |
| T12-M3 | `cli.py` | `return 1` (retire の新 except) | `return 0` | 同上 (`assert rc == 1`) |
| T12-M4 | `cli.py` | `except (ValueError, OSError) as e:` | `except (ValueError, OSError, plugin_switch.UnresolvedJournalError) as e:` の**単独節に戻す** (= 捕捉集合は同じだが案内が消える) | 同上。**「例外の集合は変えていない」という主張の裏取りでもある** |

- [ ] `uv run pytest tests/backtest/test_cli.py tests/plugin/test_materialize_retire.py -q` が green
- [ ] commit: `feat(switch-ops): plugin retire の未終端ジャーナル拒否にも次の 1 手を添える (T12、設計書 §3.4 / AC-18)`

---

## T13: 版 dir の hash 再照合を切替の要否にかかわらず通す (v1.4 新設、R14)

**担当**: 設計書 §3.7 / §0 R14 / IV-7 / AC-19a〜e。
**出所**: codex sol 設計レビュー r5 #3 (= 指揮者の問い 6 への回答) + 2026-09-20 ユーザー裁定。
**T11 と同じ `switch.py` の同じ関数群を触るので T11 → T13 の直列。**

**この task で変える挙動**: `_advance_to_decided` が `_finalize_decision` を呼ぶ**前**に、
**切替の要否にかかわらず** `new_target` の版 dir の中身を照合する。
**`switch_required=1` の枝の扱いは 1 行も変えない** (既存 pin `tests/plugin/test_switch_paths.py:569` / `:1401`)。

**設計を変えない。** **修復 (候補からの版の作り直し) はしない** — 裁定の文言は「pending 留置」。
新しい outcome も新しい `reason` も作らない (既存の `still_pending(reverify_failed)` に合流)。

### Step 13-a: テストを置いて red を確認する

| # | テスト名 | AC | 作り | 観測 |
|---|---|---|---|---|
| 1 | `test_ac19a_switch_not_required_verifies_version_dir_before_deciding` | AC-19a | **同一候補の再 approve で `switch_required=0` の状態**を作る (live が既に `.versions/<name>/<artifact_hash>` を指す) → **版 dir の `plugin.py` を in-place 編集** (`os.chmod` で書けるようにしてから書き換え、`create_version_dir` は 0500/0400 で作る) → `approve_candidate` | approval が **`pending`** / 戻り値が **`still_pending`** かつ `reason == "reverify_failed"` / **非終端 journal が 0 本** (名前が凍らない) / **live は不変** |
| 2 | `test_ac19b_verification_runs_before_finalize` | AC-19b | 1 と同じ状態 + `_finalize_decision` を spy | spy が **1 度も呼ばれない** |
| 4 | `test_ac19c_same_candidate_reapproval_still_succeeds` | AC-19c (**正常系の回帰**) | 版 dir を**触らずに**同一候補を再 approve / 再 bless | 従来どおり `approved` / outcome `deployed` / journal `switch_required=0` が `recorded` → `decided` |
| 5 | `test_ac19d_bless_raises_when_version_dir_is_tampered` | AC-19d | 1 と同じ改竄を作ってから `afx plugin bless` (CLI) | **rc=1** / stderr に `content_hash mismatch` / **traceback が出ない** / **未終端行が 1 本残る** (設計どおりの残余、§3.7.3) / そのあと `approval list` に出て `reject` で閉じられる |
| 6 | `test_ac19e_bless_residual_resolved_by_approval_retry` (**v1.8a 新規、codex sol r6 #1**) | AC-19e | 5 と同じ状態 (bless の `recorded` 残余行) を作ったあと、`reject` ではなく **`approval retry <approval_id>` を打つ** | `retry_approval` → `approve_candidate` の 0d (`existing_journal["approval_id"] == approval_id` は `kind` を見ない、`:1567`) が bless の行を拾う。版 dir を直してから打てば **`decided`/`approved`** まで完遂、直さなければ `_close_own_unfinished_journal_if_any()` (`:1632-1649`) が閉じて **`still_pending(reverify_failed)`**。**いずれも非終端 journal が 0 本になる** (`approval list` に残らない) |

**v1.4b 訂正 (実装時の実測、設計書 v1.8b §6 AC-19d/AC-19e と同じ訂正)**:
- **行 5**: CLI (`afx plugin bless`) 経由では `_plugin_bless` の except が `RuntimeError` を
  捕らないため **traceback が出る** (rc=1 にはならない)。実装 (`tests/plugin/test_switch_ops_hardening.py::
  test_ac19d_bless_raises_when_version_dir_is_tampered`) は `bless_candidate` を直接呼んで
  `RuntimeError` と残余行、`reject_candidate` による解除を確認する形にした。CLI の rc=1 化は
  ticket `[cli-bless-runtime-error-traceback]` へ切り出し (本束の範囲外)。
- **行 6**: 版 dir を直さずに `approval retry` を打つと `still_pending(reverify_failed)` には
  ならない。`_advance_to_decided` は switch_required の分岐に入る前に必ず
  `history_git.record_version` を呼び直すため (§3.7.1 の v1.8b 訂正)、直さないまま retry すると
  T13 の switch_required=0 分岐に到達する前に `record_version` 自身が `HistoryGitError`
  (`"index blob hash mismatch"`) を送出し、残余行は非終端のまま残る (`still_pending` への収束はしない)。
  実装は 2 本に分割した:
  `test_ac19e_bless_residual_resolved_by_approval_retry_when_repaired` (版 dir を直してから retry
  → `decided`/`approved`、非終端 0 本) と
  `test_ac19e_bless_residual_unrepaired_retry_raises_history_git_error` (直さずに retry →
  `HistoryGitError` が上へ抜け、非終端行が 1 本のまま残る)。

- [ ] red の最終行を逐語で貼る。**1 / 2 / 5 は現行では red** (照合を通らず `approved` になる)。
      **4 は実装前から green** (回帰防止) — green のまま置くことを明示する。
      **6 (AC-19e) は 5 (AC-19d) が作る残余行に依存する** — 現行 HEAD には T13 の
      hunk 1 が無く bless の照合自体が起きないため、5 と同様 **T13 実装前は red**
      (残余行が作れず `retry` の対象が無い)。**Step 13-c の green 確認は 5 → 6 の順で行う**
- [ ] **新しいテストは書かない項目が 1 つある** — `switch_required=1` 枝の `RuntimeError` は
      既存 `tests/plugin/test_switch_paths.py:569` / `:1401` が pin 済なので、
      **Step 13-c のチェック項目として「無改変で緑」を確認するだけ**にする (AC-19b の後半)

### Step 13-b: 実装を転写する (`switch.py` の 2 hunk)

- [ ] **hunk 1** — `_advance_to_decided` (`:1288-1360`):
      - `new_target = f".versions/{name}/{artifact_hash}"` (`:1337`) を **`if switch_required:` の外**へ出す
      - `if switch_required:` ブロックは**切替まで**を担当し、`_version_dir_hashes_ok` の照合 (`:1348-1358`) を
        ブロックの**外**、`_finalize_decision` (`:1359`) の**直前**へ移す
      - **`switch_required=1` の不一致は現行どおり `RuntimeError`**、
        **`switch_required=0` の不一致は `return False`**
      - 関数の戻り値を `-> None` → **`-> bool`** に変え、末尾を `_finalize_decision(...)` → `return True` にする

      ```python
      # (骨子。逐語は実装時に現物へ合わせ、機械 diff で確認する)
      new_target = f".versions/{name}/{artifact_hash}"
      if switch_required:
          if current_idx < _PHASE_ORDER.index("switched"):
              advance_switch_journal(conn, op_id, phase="switched", now=now, commit=True)
          switch_live(plugins_root, name, new_target=new_target, op_id=op_id)

      # [switch-ops-hardening] T13 (設計書 §3.7 / R14 / IV-7): 切替の要否に
      # かかわらず、decide の前に版 dir の中身を照合する。switch_required=0 の
      # 枝では版 dir が「この回に作ったもの」ではなく「既に在ったもの」
      # (create_version_dir は同 artifact_hash の dir があれば中身を作り直さ
      # ない — version_store.py:84-86) なので、この照合が無いと
      # record_version の照合 (毎回無条件で走る) が通った後〜decide の前に
      # 版 dir が書き換わる TOCTOU の窓を無検証で approved にしてしまう
      # (v1.4b 訂正: record_version 自体は switch_required に関係なく毎回
      # 版 dir を独立に再照合しているので「一切検証しない」わけではない)。
      resolved_after = (plugins_root / new_target).resolve()
      if not _version_dir_hashes_ok(
              resolved_after, content_hash=content_hash, artifact_hash=artifact_hash):
          if switch_required:
              # 行は switched。reconcile / 0d-2a の _reverify_switched_journal に
              # 自動収束経路があるので、現行どおり送出する (既存 pin)。
              raise RuntimeError(
                  f"plugin {name!r}: live content_hash mismatch after switch "
                  f"(expected content_hash={content_hash} "
                  f"artifact_hash={artifact_hash})")
          # 行は recorded。reconcile は非 switched 行を飛ばすので、ここで
          # 送出すると名前が凍る。呼び出し元に処置を委ねる (§3.7.2)。
          return False

      _finalize_decision(conn, approval_id, op_id=op_id, decided_by=decided_by, now=now)
      return True
      ```

- [ ] **RuntimeError の文言は 1 文字も変えない** — 既存 pin が
      `pytest.raises(RuntimeError, match="content_hash mismatch")` で部分一致している
- [ ] **hunk 2** — 呼び出し元 2 箇所:
      - `approve_candidate` (`:1728-1733`): `if not _advance_to_decided(...):` →
        `_close_own_unfinished_journal_if_any()` → `return ApprovalOutcome(outcome="still_pending",
        name=name, status="pending", op_id=op_id, rolled_back_op_id=rolled_back_op_id,
        reason="reverify_failed")`
      - `bless_candidate` (`:2080-2085`): `if not _advance_to_decided(...): raise RuntimeError(...)`
        (文言は上と同形。**行は閉じない** — 設計どおりの残余、§3.7.3)
- [ ] **AC-14d が緑のままであることを確認する** — 足した `return` は **outcome を返す return** なので
      bare `return` の検査には掛からない。掛かったら**黙って直さず申告**

### Step 13-c: green + 副作用の確認

```
uv run pytest tests/plugin/test_switch_ops_hardening.py -q
uv run pytest tests/plugin/test_switch_paths.py tests/plugin/test_switch_journal.py tests/plugin/test_history_git.py -q
uv run pytest tests/plugin/test_indicator_initial_set.py tests/test_commands.py -q
```

- [ ] **既存の `switch_required=1` の pin 2 本** (`tests/plugin/test_switch_paths.py:569` / `:1401`) が
      **無改変で緑**
- [ ] **`test_runbook_post_gate_failure_converges_via_approval_retry` の手順 5 (2 回目の bless)** が
      **無改変で緑** (= `switch_required=0` の正常系が照合を通る)
- [ ] `tests/plugin/test_switch_ops_hardening.py:513` (`_advance_to_decided` を直接呼ぶ AC-16a) が
      戻り値の変更で壊れないこと

### Step 13-d: 逆変異 (**リストは下限**)

| # | ファイル / 箇所 | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T13-M1 | `switch.py` hunk 1 | 照合ブロック (`if not _version_dir_hashes_ok(...)` 以下) | **`if switch_required:` の内側へ戻す** (= 現行) | `test_ac19a_*` / `test_ac19d_*` |
| T13-M2 | `switch.py` hunk 1 | `if switch_required: raise ... / return False` | **`return False` の枝を削る** (不一致でも素通り) | `test_ac19a_*` |
| T13-M3 | `switch.py` hunk 1 | 同上 | **両枝とも `raise RuntimeError`** (処置を揃えてしまう) | `test_ac19a_*` (`still_pending` にならず例外になる + **非終端行が 1 本残る**) |
| T13-M4 | `switch.py` hunk 1 | 照合の位置 | **`_finalize_decision` の後ろ**へ動かす | `test_ac19b_verification_runs_before_finalize` (`_finalize_decision` の spy が呼ばれてしまう) + `test_ac19a_*` (approval が `approved` になる) |
| T13-M5 | `switch.py` hunk 1 | `if not _version_dir_hashes_ok(resolved_after, ...)` | `if False:` | `test_ac19a_*` / `test_ac19d_*` / 既存 `tests/plugin/test_switch_paths.py:569` / `:1401` |
| T13-M6 | `switch.py` hunk 1 | `if not _version_dir_hashes_ok(...)` | `if True:` (常に不一致) | **`test_ac19c_same_candidate_reapproval_still_succeeds`** (正常系の回帰) + `AC-16b` + 既存の手順 5 — **三重の網** |
| T13-M7 | `switch.py` hunk 2 | `approve_candidate` の `_close_own_unfinished_journal_if_any()` | 削除 | `test_ac19a_*` (非終端が 1 本残る) |
| T13-M8 | `switch.py` hunk 2 | `reason="reverify_failed"` | `reason="hash_mismatch"` | `test_ac19a_*` (`reason` の逐語一致) |
| T13-M9 | `switch.py` hunk 1 | `return True` (末尾) | `return False` | 多数 (`deployed` 系がすべて `still_pending` に化ける) — **等価でないことの確認用** |

**AC-19e (v1.8a 新規) について**: `test_ac19e_*` が確認する「0d は `kind` を見ずに
`approval_id` だけで自分の未完行を拾う」経路 (`:1567` の `existing_journal["approval_id"]
== approval_id`) は **T13 の hunk ではなく既存コード** (T3/T5、確定-1) の契約なので、
T13 の変異表には専用の killer を持たない。T13-M7 (`_close_own_unfinished_journal_if_any()`
の削除) が当たれば `test_ac19e_*` の「非終端が 0 本」観測も**巻き添えで red** になる
(補助的な二重の網であって、AC-19e 専用の killer ではない)。

- [ ] 各変異は `cp` 退避 → 適用 → red の逐語確認 → 復元。**`git checkout` / `git restore` は使わない**
- [ ] **副作用の確認**: T13-M5 / M6 を当てたとき **他のどのテストが red になるか**も記録する
- [ ] フルスイートが green。**基準は T11 後の実測値 + T13 の新規分** (予測値を書かない)
- [ ] commit: `fix(switch-ops): 版 dir の hash 再照合を切替の要否にかかわらず通す (T13、設計書 §3.7 / IV-7 / AC-19)`

---

## T14: 2 周目ローカル LLM の確定 2 件を pin する (**テストのみ**、v1.4 新設)

**担当**: `tmp/review-20260920-soh/r2/local/triage.md` の **T2-01 / T2-02**。
**本体コードは 1 行も変えない** — どちらも「実装は正しいがテストが無い」。
**T11 / T13 で行番号が動くので、行番号ではなく関数名・分岐名・reason 値で特定すること。**

### Step 14-a: T2-01 — `still_pending` の `outcome.status` が 2 箇所だけ未 pin

- [ ] 対象: `approve_candidate` の **`snapshot_invalid` の枝**と **`hash_mismatch` の枝**が返す
      `ApprovalOutcome(..., status="pending", ...)`。
      `approve_candidate` が `status="pending"` を返す 6 箇所のうち、
      `foreign_waiting` / `reverify_failed` / `candidate_missing` / `legacy_plain` の 4 つは pin 済で、
      **この 2 つだけが未 pin** (トリアージで全数確認済)
- [ ] 生き残る変異: その 2 箇所の `status="pending"` を **`status="approved"`** に変える。
      DB は決定していないので `row["status"]` は `"pending"` のままで、**変異が DB 側に現れない**
- [ ] 是正 (各 1 行): `tests/plugin/test_switch_paths.py` の
      **`test_approve_candidate_snapshot_invalid_stays_pending_with_reason`** (`:433`) と
      **`test_approve_candidate_hash_mismatch_stays_pending_with_reason`** (`:461`) の末尾に
      `assert outcome.status == "pending"` を足す
- [ ] 影響の根拠 (テストの docstring に 1 行書く): `outcome.status` は
      `commands.py` の approve ハンドラが `(status={outcome.status})` として**人間に直接出す**ので、
      潰れると「承認されませんでした (status=approved)」という矛盾文言になる (**段 0 S0-78 と同じ形**)

### Step 14-b: T2-02 — `snapshot_invalid` の枝だけ `_close_own_unfinished_journal_if_any()` が未 pin

- [ ] 生き残る変異: **`snapshot_invalid` の枝の `_close_own_unfinished_journal_if_any()` 1 行だけ**を削る
      (`candidate_missing` / `hash_mismatch` の 2 箇所は残す)
- [ ] なぜ生き残るか: 既存 pin は
      `tests/plugin/test_switch_paths.py::test_stuck_preparing_journal_is_closed_when_candidate_no_longer_matches`
      (`:1191`、hash_mismatch 側) と `::test_stuck_preparing_journal_is_closed_when_candidate_goes_missing`
      (`:1232`、candidate_missing 側) の 2 本だけで、
      **`op_id` が `None` でない状態で `snapshot_invalid` に到達するテストが無い**
      (新規 submit → approve の経路は `op_id is None` で helper が早期 return する)
- [ ] 是正: `test_stuck_preparing_journal_is_closed_when_candidate_no_longer_matches` を**丸ごと流用**し、
      候補の改竄 (hash 不一致) の代わりに **`extra.txt` を足す** (= snapshot 検査を落とす) 1 本を隣に追加する。
      観測は同じ — `switch.get_open_by_name(conn, name) is None` と `outcome.reason == "snapshot_invalid"`
- [ ] これで **3 呼び出しすべてが pin される**

### Step 14-c: 逆変異 (= この task の本体)

| # | ファイル | 置換前 | 置換後 | red になるテスト |
|---|---|---|---|---|
| T14-M1 | `switch.py` | `snapshot_invalid` の枝の `status="pending"` | `status="approved"` | 14-a で足した assert |
| T14-M2 | `switch.py` | `hash_mismatch` の枝の `status="pending"` | `status="approved"` | 同上 |
| T14-M3 | `switch.py` | `snapshot_invalid` の枝の `_close_own_unfinished_journal_if_any()` | 削除 | 14-b で足したテスト |

- [ ] **3 件とも「まず正実装で green、次に変異で red、復元」**の順で逐語確認する (T10 と同じ流儀)
- [ ] **T2-03 / T2-04 は本束では扱わない** (トリアージの判断どおり — T2-03 は
      「どの例外を通すのが契約か」を先に決める必要があり、T2-04 は本束の diff 外)
- [ ] commit: `test(switch-ops): still_pending の status と snapshot_invalid の journal 閉じを pin (T14、2 周目ローカル LLM T2-01/T2-02)`

---

## T8: runbook / 設計書の追記 / フルスイート

- [ ] `docs/operations/indicator-initial-set-deploy-2026-09-19.md` を設計書 §7.2 の表のとおり改訂する
      (L56-57 / L108-127 の traceback 記述、L146-153 の phase 別表、L155-164 の既知不具合、
      **L167-169 の「いずれの phase でも再 bless 必須」**、approval id の入手節)
- [ ] `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` §5.1-1 に
      「retry の `not_switched` 処置」を 1 段落追記する (既存規則の具体化。規則自体は変えない)
- [ ] **v1.2**: 設計書 §3.6 / §3.3.2 手順 4 / IV-6 / AC-9d / AC-17a〜d が v1.6 に入っていることと、
      runbook の「approval id の入手」節が `approve` の新文言と矛盾しないことを確認する
      (runbook は `approval list` を第一手にしており、`approve` の戻り文言は引用していない — 要確認)
- [ ] **v1.3**: runbook に「**plugin 名が凍ったとき**」の節を足す (設計書 §7.2 の最終行) —
      `bless` / `retire` / 別 approval の `approve` が `UnresolvedJournalError` で拒否されたら
      **`approval list` → `approval retry <approval_id>`**、**起動時 reconcile では解けない**
      (非 `switched` 行は飛ばす)、**`foreign` のときだけ人間が判断する**
- [ ] **v1.3**: 設計書 §3.2 の T11 節 / §3.4 の retire 行 / AC-16c-1〜6 / AC-18 が v1.7 に
      入っていることを確認する
- [ ] **v1.4**: runbook に「**版 dir の中身が候補と合わないとき**」の節を足す (設計書 §7.2 の最終行) —
      `reason=reverify_failed` / bless の `content_hash mismatch` を見たら、
      **機械は自動で版を作り直さない**こと、人間が版 dir を確認し壊れていれば消して `approval retry`、
      bless 経路では未終端行が残るので先に `approval list` → `approval retry` / `reject`
- [ ] **v1.4**: 設計書 §3.7 / IV-7 / AC-16c-7 / AC-16c-8 / AC-19a〜d / §8.6 が v1.8 に
      入っていることを確認する
- [ ] **フルスイート**を回す: `uv run pytest -q`。**書き換えた 4 本以外が red になったら
      黙って直さず申告する**。**基準 (v1.3)**: T10 後 `4300 passed` →
      **2 周目までの pin 追加を含む現状 `4304 passed`** → T11 (+6 本、parametrize 込みの
      収集数は実測) → T12 (+1〜2 本)。**予測値を書かず、各 task の Step で実測値に置き換えること**
- [ ] commit: `docs(switch-ops): runbook と 8 月設計書を本束の挙動に合わせる (T8)`

---

## 付録 A: `switch.py` の完全な差分 (T1〜T5、適用可能な形)

```diff path=src/agentic_fx/plugin/switch.py
--- src/agentic_fx/plugin/switch.py
+++ src/agentic_fx/plugin/switch.py
@@ -10,6 +10,7 @@
 import shutil
 import sqlite3
 import stat
+from dataclasses import dataclass
 from datetime import datetime
 from pathlib import Path
 from typing import Callable, Literal
@@ -36,6 +37,59 @@
 
 _PHASE_ORDER = ["preparing", "versioned", "recorded", "switched", "decided", "reverted"]
 
+_TERMINAL_PHASES = ("decided", "reverted")
+
+# [switch-ops-hardening] T5: lock の内側で確定した結果 (設計書 §3.5)。
+APPROVAL_OUTCOMES = frozenset({
+    "deployed", "deployed_after_rollback", "already_decided", "foreign_waiting",
+    "still_pending", "legacy_plain_present", "invalidated",
+})
+
+
+@dataclass(frozen=True)
+class ApprovalOutcome:
+    """`approve_candidate` / `retry_approval` が **plugin flock の内側で確定**
+    した結果 (設計書 §3.5)。呼び出し元はこの値を文言に写すだけで、
+    lock の外で DB / FS を読み直さない (読み直すと、別プロセスの後続の
+    正規配備を「契約違反」と誤報する — r2 Important 4)。"""
+    outcome: str
+    name: str
+    status: str
+    op_id: int | None = None
+    rolled_back_op_id: int | None = None
+    target: str | None = None
+    reason: str | None = None
+
+    def __post_init__(self) -> None:
+        if self.outcome not in APPROVAL_OUTCOMES:
+            raise ValueError(f"unknown approval outcome: {self.outcome!r}")
+
+
+def classify_live(plugins_root: Path, row) -> str:
+    """[switch-ops-hardening] T1 (設計書 §3.1): `switched` で止まった journal 行
+    に対し、**live symlink が実際にどこを指しているか**を 3 値で返す。
+
+    比較は `readlink` の生文字列で行う (`resolve()` は使わない) — journal の
+    `new_target`/`old_target` は plugins_root 相対の文字列として書かれ、
+    `switch_live` がそれをそのまま symlink の中身にするため。`resolve()` に
+    すると版ディレクトリが消えた dangling symlink で比較が壊れる。
+
+    **読み取り専用** (IV-5)。`phase='switched'` かつ `switch_required=1` の
+    行にしか意味が無いので、それ以外で呼ばれたら fail closed。"""
+    if row["phase"] != "switched" or not row["switch_required"]:
+        raise ValueError(
+            f"classify_live: op_id={row['op_id']} is phase={row['phase']!r} "
+            f"switch_required={row['switch_required']} — 分類器は "
+            "phase='switched' かつ switch_required=1 の行にのみ適用する")
+    live = plugins_root / row["name"]
+    live_target = live.readlink().as_posix() if live.is_symlink() else None
+    if live_target == row["new_target"]:
+        return "switched"
+    if live_target == row["old_target"] or (
+            row["old_kind"] == "absent" and live_target is None):
+        return "not_switched"
+    return "foreign"
+
 
 # precheck 2026-08-22 wave2: T11-B2 / T11-B3 / T11-B1
 def begin_switch_journal(
@@ -141,6 +195,44 @@
                        f"name={row['name']} op_id={row['op_id']}")
 
 
+def _revert_under_lock(conn: sqlite3.Connection, row: dict, *, plugins_root: Path,
+                       now: datetime, activity: "ActivityLog | None",
+                       expect_class: str | None) -> bool:
+    """[switch-ops-hardening] T4 (設計書 §3.3.2): 巻き戻しは必ず
+    (1) name lock を取り (2) `op_id` で journal 行を取り直し (3) (分類を使う枝なら)
+    live を読み直して分類をやり直し、(4) すべて一致したときだけ `_revert_one` する。
+
+    lock を取るだけでは足りない — lock 待ちの間に別プロセスの `approval retry` が
+    同じ行を畳んで配備を完了していると、手元の古い行で live を巻き戻して
+    「approved だが未配備」を作ってしまう (r1 Critical 1、probe で実測)。
+
+    `expect_class=None` は `force_revert_op_id` 経路 — 「phase に依らず巻き戻す
+    割込」という既存の意味論を保つため、**行の再取得だけ**を行い分類は見ない。"""
+    with _plugin_lock(plugins_root, row["name"]):
+        fresh = journal_store.get(conn, row["op_id"])
+        phase_now = fresh["phase"] if fresh is not None else None
+        if fresh is None or phase_now in _TERMINAL_PHASES or phase_now != row["phase"]:
+            if activity is not None:
+                activity.write(
+                    Category.APPROVAL, "switch_reconcile_skipped_stale_row",
+                    f"name={row['name']} op_id={row['op_id']} "
+                    f"phase_before={row['phase']} phase_now={phase_now}")
+            return False
+        if expect_class is not None:
+            class_now = classify_live(plugins_root, fresh)
+            if class_now != expect_class:
+                if activity is not None:
+                    activity.write(
+                        Category.APPROVAL, "switch_reconcile_skipped_stale_row",
+                        f"name={row['name']} op_id={row['op_id']} "
+                        f"phase_before={row['phase']} phase_now={phase_now} "
+                        f"class_before={expect_class} class_now={class_now}")
+                return False
+        _revert_one(conn, fresh, plugins_root=plugins_root, now=now, activity=activity)
+        conn.commit()
+        return True
+
+
 def reconcile_switch_journals(conn: sqlite3.Connection, *,
                               plugins_root: Path, now: datetime,
                               settings,
@@ -171,8 +263,10 @@
                 # 割込操作の巻き戻しは収束規則より優先する (B-3)。phase が
                 # switched でなくても _revert_one は非 FS 操作 (phase="reverted"
                 # への書き換えのみ) に閉じるので安全。
-                _revert_one(conn, row, plugins_root=plugins_root, now=now, activity=activity)
-                conn.commit()
+                # [switch-ops-hardening] T4: lock + 行の再取得を通す
+                # (分類の一致は求めない — 割込の意味論を保つ、§3.3.3)。
+                _revert_under_lock(conn, row, plugins_root=plugins_root, now=now,
+                                  activity=activity, expect_class=None)
                 continue
             if row["phase"] != "switched":
                 # preparing/versioned/recorded: 「同じ操作の再試行」は 11d の
@@ -181,11 +275,9 @@
                 # まだ無いので触らずスキップ — 実際の完了は次回の approve/
                 # approval retry が担う (§5.1-1 (a))。
                 continue
-            live = plugins_root / row["name"]
-            live_target = live.readlink().as_posix() if live.is_symlink() else None
-            new_norm = row["new_target"]
-            old_norm = row["old_target"]
-            if live_target == new_norm:
+            # [switch-ops-hardening] T1: 分類は 1 つの関数に寄せる (§3.1)。
+            live_class = classify_live(plugins_root, row)
+            if live_class == "switched":
                 # [indicator-consumption-wiring] §2.4 (codex r5 I3):
                 # switched (symlink 切替済・DB decided 前) のまま停止し、
                 # 再起動までに indicator が更新されて pin が破れた場合、
@@ -200,9 +292,10 @@
                     conn, row, plugins_root=plugins_root, settings=settings)
                 if unresolved is not None:
                     alias, reason = unresolved
-                    _revert_one(conn, row, plugins_root=plugins_root, now=now,
-                               activity=activity)
-                    conn.commit()
+                    if not _revert_under_lock(
+                            conn, row, plugins_root=plugins_root, now=now,
+                            activity=activity, expect_class="switched"):
+                        continue  # stale — 次回の reconcile が再評価する
                     if activity is not None:
                         # Global Constraints の固定文言 (逐語):
                         # `switch_reverted reason=indicator_unresolved
@@ -221,9 +314,9 @@
                 retry_approval(conn, row["approval_id"], decided_by="system_reconcile",
                                 now=now, plugins_root=plugins_root, settings=settings,
                                 activity=activity)
-            elif live_target == old_norm or (row["old_kind"] == "absent" and live_target is None):
-                _revert_one(conn, row, plugins_root=plugins_root, now=now, activity=activity)
-                conn.commit()
+            elif live_class == "not_switched":
+                _revert_under_lock(conn, row, plugins_root=plugins_root, now=now,
+                                  activity=activity, expect_class="not_switched")
             else:
                 # 第三者に触られた — activity ERROR、人間待ち。触らない。
                 # B-2: journal_store 層に activity を書かせない (層違反) —
@@ -1165,7 +1258,21 @@
                        op_id: int, decided_by: str, now: datetime) -> None:
     """§4.3 手順 9: `apply_decision(approved)` + ジャーナル `decided` 化を
     1 tx で行う (`apply_decision` 自身はジャーナルに触らない — B-2/§申し
-    送り⑤どおり、Task 11 側がここで拡張する拡張点)。"""
+    送り⑤どおり、Task 11 側がここで拡張する拡張点)。
+
+    [switch-ops-hardening] T2 (設計書 §3.2): **単調性 API を迂回する唯一の
+    `set_phase` 呼び出し**なので、ここで期待 phase を検査する。期待値は
+    `switch_required` で決まる — `phase in ("switched", "recorded")` と
+    書くと `switch_required=1` の行が `recorded` のまま (= 切替をしていない
+    のに) 決定できてしまい IV-3 を破る fail open になる。"""
+    guard_row = journal_store.get(conn, op_id)
+    if guard_row is None:
+        raise ValueError(f"op_id={op_id}: _finalize_decision found no journal row")
+    expected_phase = "switched" if guard_row["switch_required"] else "recorded"
+    if guard_row["phase"] != expected_phase:
+        raise ValueError(
+            f"op_id={op_id}: _finalize_decision expects phase="
+            f"{expected_phase!r} but found {guard_row['phase']!r}")
     conn.execute("BEGIN IMMEDIATE")
     try:
         approvals_store.apply_decision(
@@ -1192,8 +1299,20 @@
     冪等)。`advance_switch_journal` は段 0 で単調性チェックを獲得したため、
     既に到達済みの phase へ**後方**の advance を投げると ValueError になる
     — ここで到達済み phase をスキップするガードを持ち、その後方 advance
-    自体を発生させない (§5.1-1 (a) の再試行冪等性を壊さない)。"""
-    current_idx = _PHASE_ORDER.index(journal_store.get(conn, op_id)["phase"])
+    自体を発生させない (§5.1-1 (a) の再試行冪等性を壊さない)。
+
+    [switch-ops-hardening] T2 (設計書 §3.2): **入口で終端行を弾く**。
+    終端行を渡されると全 advance が guard でスキップされる一方 `switch_live`
+    は走ってしまい、「approval は pending なのに live だけ新 target へ進み、
+    行は終端なので次回 reconcile も拾わない」という誰も直さない状態が残る
+    (probe 実測)。FS に触る前に落とすのが fail closed。"""
+    entry_row = journal_store.get(conn, op_id)
+    if entry_row is None or entry_row["phase"] in _TERMINAL_PHASES:
+        raise ValueError(
+            f"op_id={op_id}: _advance_to_decided called on a terminal/missing "
+            f"journal row (phase={entry_row['phase'] if entry_row else None}) "
+            "— 新しい操作は新しい journal 行に載せること (設計書 §3.2)")
+    current_idx = _PHASE_ORDER.index(entry_row["phase"])
 
     version_dir = version_store.create_version_dir(
         plugins_root, name, artifact_hash,
@@ -1366,7 +1485,7 @@
     conn: sqlite3.Connection, approval_id: int, *,
     decided_by: str, now: datetime, plugins_root: Path, settings,
     activity: "ActivityLog | None" = None,
-) -> None:
+) -> "ApprovalOutcome":
     """P2 (approve) の入口。plugins_root は FS 操作 (版・git・切替) の起点、
     settings は将来のゲート再検証・kind 別分岐のために渡す (現行 P2 手順は
     gate を再実行しないが、シグネチャで揃えておくことで retry_approval/
@@ -1398,7 +1517,9 @@
         row = conn.execute(
             "SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
         if row["status"] != "pending":
-            return
+            # [switch-ops-hardening] T5: status の出所は **read** (この行の SELECT)。
+            return ApprovalOutcome(outcome="already_decided", name=name,
+                                  status=row["status"])
         payload = json.loads(row["payload_json"])
 
         if _is_superseded(conn, row, payload):  # 0c
@@ -1414,7 +1535,10 @@
             # I1 是正: 終端決定 (invalidated) の tx 直後に staging 候補を
             # 削除する (§5.1 手順 3)。
             _drop_staging_candidate(plugins_root, payload, activity=activity)
-            return
+            # T5: status の出所は **decision 結果** (同 lock 内で成功した
+            # apply_decision に渡したリテラル — 再読しない)。
+            return ApprovalOutcome(outcome="invalidated", name=name,
+                                  status="invalidated")
 
         # [indicator-consumption-wiring] §2.3: P2 (決定時) に `require` で
         # **解決だけ**行う (gate は再実行しない)。submit → approve の間に
@@ -1439,6 +1563,7 @@
         # 経由せずここで直接処理する、11c の NameError landmine を踏まない)。
         existing_journal = journal_store.get_open_by_name(conn, name)
         op_id = None
+        rolled_back_op_id = None  # [switch-ops-hardening] T3 (§3.5)
         if existing_journal is not None and existing_journal["approval_id"] == approval_id:
             op_id = existing_journal["op_id"]
             if existing_journal["phase"] == "switched":
@@ -1447,13 +1572,42 @@
                 # (§5.1-1 (a))。再検証失敗時は `_reverify_switched_journal`
                 # が journal を reverted で閉じ activity ERROR を書く —
                 # ここでは pending のまま return するだけでよい。
-                if not _reverify_switched_journal(
-                        conn, existing_journal, plugins_root=plugins_root,
-                        payload=payload, now=now, activity=activity):
-                    return
-                _finalize_decision(conn, approval_id, op_id=op_id,
-                                   decided_by=decided_by, now=now)
-                return
+                # [switch-ops-hardening] T3 (設計書 §3.2、案 C): journal が
+                # switched でも **live が本当に切り替わっているとは限らない**
+                # (`advance(switched)` を commit してから `switch_live` を
+                # 呼ぶ journal-first の順序ゆえ)。分類してから枝を選ぶ。
+                live_class = classify_live(plugins_root, existing_journal)
+                if live_class == "foreign":
+                    # 0d-2b: 第三者に触られた — 触らず人間待ち (fail closed)。
+                    if activity is not None:
+                        activity.write(
+                            Category.APPROVAL,
+                            "switch_retry_unrecognized_live_target",
+                            f"name={name} op_id={op_id}")
+                    return ApprovalOutcome(outcome="foreign_waiting", name=name,
+                                          status="pending", op_id=op_id)
+                if live_class == "switched":
+                    # 0d-2a: 切替は完了している — 従来どおり再検証して決定。
+                    if not _reverify_switched_journal(
+                            conn, existing_journal, plugins_root=plugins_root,
+                            payload=payload, now=now, activity=activity):
+                        return ApprovalOutcome(
+                            outcome="still_pending", name=name, status="pending",
+                            op_id=op_id, reason="reverify_failed")
+                    _finalize_decision(conn, approval_id, op_id=op_id,
+                                       decided_by=decided_by, now=now)
+                    return ApprovalOutcome(
+                        outcome="deployed", name=name, status="approved",
+                        op_id=op_id, target=existing_journal["new_target"])
+                # 0d-2c (not_switched): 停止行を巻き戻して閉じ、**新しい
+                # journal 行で手順を頭から流す** (§5.3 契機③「手順を頭から
+                # 流す」+ §5.1-1 (b)「巻き戻してから本来の操作を適用する」)。
+                # `op_id` を None に戻さないと下流が終端行を使い回してしまう。
+                _revert_one(conn, existing_journal, plugins_root=plugins_root,
+                           now=now, activity=activity)
+                conn.commit()
+                rolled_back_op_id = op_id
+                op_id = None
         elif existing_journal is not None:
             # 別 approval_id の未完ジャーナルが同名に存在する場合、この
             # approve は進められない — `plugin_switch_journal` の部分
@@ -1477,8 +1631,10 @@
 
         def _close_own_unfinished_journal_if_any() -> None:
             # 確定-1 (Critical): この approval 自身の未完ジャーナル
-            # (preparing/versioned/recorded — switched はここに来ない、上の
-            # 0d 分岐で先に処理・return 済み) が残っていれば、候補が壊れて
+            # (preparing/versioned/recorded。**switched のうち
+            # `not_switched` は 0d-2c が既に閉じて op_id=None にしている**
+            # ので、ここに残る switched 行は無い — [switch-ops-hardening] T3
+            # で前提が変わった箇所) が残っていれば、候補が壊れて
             # pending 留置する前に `_revert_one` で閉じる。閉じないと name が
             # 永久に `UnresolvedJournalError` で封鎖される
             # (verified-local-round1.md 確定-1)。`_revert_one` は非 switched
@@ -1500,7 +1656,10 @@
                 candidate_path=candidate_path, name=name)
         except CandidateMissingError:
             _close_own_unfinished_journal_if_any()
-            return  # pending のまま (§8.1-29)
+            return ApprovalOutcome(  # pending のまま (§8.1-29)
+                outcome="still_pending", name=name, status="pending",
+                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
+                reason="candidate_missing")
 
         try:
             check_candidate_snapshot(candidate_dir)
@@ -1517,12 +1676,18 @@
             # `test_approve_candidate_missing_stays_pending` で red に
             # ならず survive してしまう)。
             _close_own_unfinished_journal_if_any()
-            return  # 候補が壊れている/検査失敗 → pending のまま
+            return ApprovalOutcome(  # 候補が壊れている/検査失敗 → pending のまま
+                outcome="still_pending", name=name, status="pending",
+                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
+                reason="snapshot_invalid")
         if (content_hash != payload["content_hash"]
                 or artifact_hash != payload["artifact_hash"]):
             # 確定-1: ⓐ 不一致でも同様に自分の未完ジャーナルを閉じる。
             _close_own_unfinished_journal_if_any()
-            return  # ⓐ 不一致 → pending のまま
+            return ApprovalOutcome(  # ⓐ 不一致 → pending のまま
+                outcome="still_pending", name=name, status="pending",
+                op_id=op_id, rolled_back_op_id=rolled_back_op_id,
+                reason="hash_mismatch")
 
         live = plugins_root / name
         if live.is_symlink():
@@ -1548,7 +1713,8 @@
                 version_dir=version_dir)
             approvals_store.set_reason(
                 conn, approval_id, "legacy_plain_present", commit=True)
-            return
+            return ApprovalOutcome(outcome="legacy_plain_present", name=name,
+                                  status="pending")
 
         switch_required = not (old_kind == "symlink" and old_target == new_target)
 
@@ -1568,6 +1734,14 @@
         if candidate_origin == "staging":
             shutil.rmtree(candidate_dir, ignore_errors=True)
 
+        # [switch-ops-hardening] T5: lock を抜ける前に結果を確定する (§3.5)。
+        return ApprovalOutcome(
+            outcome=("deployed_after_rollback" if rolled_back_op_id is not None
+                     else "deployed"),
+            name=name, status="approved", op_id=op_id,
+            rolled_back_op_id=rolled_back_op_id,
+            target=new_target)
+
 
 class UnresolvedJournalError(Exception):
     """retire は未完 switch ジャーナルがあれば拒否する (§5.1)。"""
@@ -1642,16 +1816,17 @@
 def retry_approval(conn: sqlite3.Connection, approval_id: int, *,
                    decided_by: str, now: datetime, plugins_root: Path,
                    settings: "Settings",
-                   activity: "ActivityLog | None" = None) -> None:
+                   activity: "ActivityLog | None" = None) -> "ApprovalOutcome":
     """§5.3 契機③: 手順を頭から流す (lock → plain 検出 → ⓓ → ⓐ → 版(冪等) →
     git(no-op) → 切替(no-op なら済み) → apply_decision)。approve_candidate と
     同じ実装を呼ぶだけ (retry は「approve をもう一度呼ぶ」と同義 — §5.3 本文)。
     B-1 是正で plugins_root/settings を追加した (approve_candidate へそのまま
     透過する)。B3 是正: `activity` も同様に透過する (0d の再検証失敗時の
-    ERROR 記録用)。"""
-    approve_candidate(conn, approval_id, decided_by=decided_by, now=now,
-                      plugins_root=plugins_root, settings=settings,
-                      activity=activity)
+    ERROR 記録用)。[switch-ops-hardening] T5: `approve_candidate` が lock 内で
+    確定した `ApprovalOutcome` を**そのまま透過する** (§3.5)。"""
+    return approve_candidate(conn, approval_id, decided_by=decided_by, now=now,
+                             plugins_root=plugins_root, settings=settings,
+                             activity=activity)
 
 
 def reject_candidate(conn: sqlite3.Connection, approval_id: int, *,
```

**hunk → task の割り当て (v1.1、着手前検証で 1 task ずつ実走して確定)**

| hunk (`@@` 行) | 内容 | task |
|---|---|---|
| `@@ -10,6` | `from dataclasses import dataclass` | T1 |
| `@@ -36,6` | `_TERMINAL_PHASES` / `APPROVAL_OUTCOMES` / `ApprovalOutcome` / `classify_live` | T1 |
| `@@ -1165,7` | `_finalize_decision` の `guard_row` ブロック | T2 |
| `@@ -1192,8` | `_advance_to_decided` の `entry_row` ブロック | T2 |
| `@@ -1439,6` | `rolled_back_op_id = None` | T3 |
| `@@ -1447,13` | 0d の `live_class` 分岐 (案 C 本体) | T3 |
| `@@ -1477,8` | `_close_own_unfinished_journal_if_any` のコメント更新 | T3 |
| `@@ -1366,7` / `@@ -1398,7` / `@@ -1414,7` / `@@ -1500,7` / `@@ -1517,12` / `@@ -1548,7` / `@@ -1568,6` / `@@ -1642,16` | `approve_candidate` / `retry_approval` の `ApprovalOutcome` 戻り値 (8 hunk) | **T3** (v1.1 で T5 から移動 — 下記) |
| `@@ -141,6` | `_revert_under_lock` の新設 | T4 |
| `@@ -171,8` / `@@ -181,11` / `@@ -200,9` / `@@ -221,9` | reconcile の載せ替えと巻き戻し 3 箇所 | T4 |

**v1.1 の訂正 — T3 / T4 の分割は v1.0 のままでは成立しない (実測)**

着手前検証で T1 → T7 を 1 task ずつ当てたところ、**T3 の green が取れなかった**
(`3 failed, 6 passed`)。原因は分割の欠陥で、設計の欠陥ではない:

- T3 の受入テスト `test_ac1` / `test_ac3` / `test_ac8` は
  `outcome = plugin_switch.retry_approval(...)` の**戻り値**を assert する。
  `retry_approval` が `ApprovalOutcome` を返すのは付録 A の `@@ -1642,16` (v1.0 では T5) なので、
  T3 単独では `AttributeError: 'NoneType' object has no attribute 'outcome'` になる。
  **構造的な assert (journal 2 行 / live symlink / approval approved) はすべて緑**であり、
  **T3 の実装自体は正しい** — 足りないのは戻り値の配線だけ。
- 同じ 3 本が T4 の green でも red のまま残る (`4 failed, 30 passed`)。

**訂正**: 付録 A の `approve_candidate` / `retry_approval` の戻り値 8 hunk
(`@@ -1366,7` / `@@ -1398,7` / `@@ -1414,7` / `@@ -1500,7` / `@@ -1517,12` / `@@ -1548,7` /
`@@ -1568,6` / `@@ -1642,16`) と、付録 C の **`test_ac14d_no_bare_return_in_approval_entrypoints` /
`test_ac14d_outcomes_are_known_enum_values` / `test_ac14d_still_pending_on_missing_candidate` /
`test_ac14a_already_decided_status_comes_from_the_row` の 4 本**を **T3 へ移す**。
これにより **T5 は `commands.py` だけの task** (付録 B-1 + 付録 D-1 + 付録 F-1) になる。
案 C と「lock 内で確定した outcome を返す」は**同じ 1 つの挙動変更**なので、この形が設計にも忠実。

**訂正後の実測** (worktree `soh-preflight` の `soh-t3split` ブランチ、T2 の commit から):

```
T3 (訂正後) の新規テスト                   : 9 passed in 10.65s
T3 (訂正後) + T4/T5 のテストも置いた場合   : 14 passed, 5 failed (T4 の lock 系 5 本のみ = 正しい red)
T3 (訂正後) の既存テスト群                 : 925 passed, 1 deselected, 0 failed in 229.45s
```



## 付録 B-1: `commands.py` の差分 — **T5 部分のみ** (retry の結果報告)

**v1.1 で分割**: v1.0 の付録 B は 1 本の完全差分で、`@@ -144,13` の中に T5 の `outcome =` 受け取りと
T7 の `approval list` dispatch が、`@@ -342,6` の中に `_retry_outcome_text` (T5) と
`_approval_list` / `_open_journal_lines` (T7) が**同じ hunk に同居**していた。そのままでは
**T5 を当てると T7 の実装まで入ってしまい T7 の red が取れない**ので、機械的に 2 本へ割った
(B-1 → B-2 の順に当てると v1.0 の付録 B と**バイト一致**することを確認済)。

```diff path=src/agentic_fx/commands.py
--- src/agentic_fx/commands.py
+++ src/agentic_fx/commands.py
@@ -144,13 +144,20 @@
                     return "approval retry backend (plugins_root/settings) が未配線です"
                 from agentic_fx.plugin import switch as plugin_switch
                 approval_id = int(args[1])
-                plugin_switch.retry_approval(
+                # [switch-ops-hardening] T5 (設計書 §3.5): `retry_approval` が
+                # **plugin flock の内側で確定した** outcome を返す。ここでは
+                # それを文言に写すだけで、lock の外で DB / FS を読み直さない
+                # (読み直すと、別プロセスの後続の正規配備を「契約違反」と
+                # 誤報する)。未知 / None は文言にせず例外に落とす (fail loud) —
+                # 本体の return 漏れを「普通の応答」に化けさせないため。
+                outcome = plugin_switch.retry_approval(
                     self.conn, approval_id, decided_by="shell", now=self.clock.now(),
                     plugins_root=self.plugins_root, settings=self.settings,
                     activity=self.activity)
                 self.activity.write(Category.APPROVAL, "retry",
                                     f"#{approval_id} via shell", ref_id=str(approval_id))
-                return f"approval #{approval_id} を再試行しました"
+                return (f"approval #{approval_id} を再試行しました: "
+                        f"{self._retry_outcome_text(outcome)}")
             if cmd == "approval" and len(args) == 1 and args[0].isdigit():
                 # [approval-payload-missing-gate-metrics] 是正 (A4 10 回目
                 # claude #69 観測 A、2026-09-11): approve/reject する前に
@@ -342,6 +349,30 @@
                    for pair, metrics in value.items()]
         return [cls._metrics_line(prefix, None)]
 
+    def _retry_outcome_text(self, outcome) -> str:
+        """[switch-ops-hardening] T5: `ApprovalOutcome` を文言に写す。
+        未知 / None はここで `ValueError` になり、`dispatch` の包括 `except`
+        が `エラー: ...` を返す (fail loud — 設計書 §3.5)。"""
+        kind = outcome.outcome  # None なら AttributeError (fail loud)
+        if kind in ("deployed", "deployed_after_rollback"):
+            tail = f"配備まで完了しました (plugins/{outcome.name} → {outcome.target})"
+            if kind == "deployed_after_rollback":
+                return (f"中断していた切替 (op_id={outcome.rolled_back_op_id}) を"
+                        f"巻き戻してから再実行し、{tail}")
+            return tail
+        if kind == "foreign_waiting":
+            return (f"live が第三者に触られているため自動収束しません "
+                    f"(op_id={outcome.op_id})。plugins/{outcome.name} の状態を"
+                    "確認してください")
+        if kind == "still_pending":
+            return f"approved になりませんでした (reason={outcome.reason})"
+        if kind == "legacy_plain_present":
+            return (f"plugins/{outcome.name} が旧式のディレクトリのままです "
+                    "(先に afx plugin retire が要ります)")
+        if kind in ("already_decided", "invalidated"):
+            return f"この承認は既に決着しています (status={outcome.status})"
+        raise ValueError(f"unknown approval outcome: {kind!r}")
+
     def _approval_detail(self, approval_id: int) -> str:
         row = self.conn.execute(
             "SELECT kind, status, payload_json, reason, decided_by, "
```

## 付録 B-2: `commands.py` の差分 — **T7 部分のみ** (`approval list`)

```diff path=src/agentic_fx/commands.py
--- src/agentic_fx/commands.py
+++ src/agentic_fx/commands.py
@@ -23,6 +23,7 @@
   ask <質問>                  臨時 Mission (回答専用 — 発注はしない)
   approve <id> / reject <id> [理由]   承認操作
   approval <id>               承認申請の詳細 (in_sample/holdout 成績を含む)
+  approval list [n]          承認待ちの一覧 (+ 未終端の切替ジャーナル)
   approval retry <id>        承認手順を頭から再試行 (§5.3 契機③)
   killswitch reset           kill switch ラッチの解除 (人間の明示操作)
   reflect retry <order_id>   abandon された reflection を再試行対象へ戻す
@@ -158,6 +159,11 @@
                                     f"#{approval_id} via shell", ref_id=str(approval_id))
                 return (f"approval #{approval_id} を再試行しました: "
                         f"{self._retry_outcome_text(outcome)}")
+            if cmd == "approval" and args and args[0] == "list" and len(args) <= 2:
+                # [switch-ops-hardening] T7 (設計書 §3.5): `approval retry <id>`
+                # の id を知るための一覧。**holdout / in_sample の数値は出さない**
+                # (詳細は `approval <id>`)。
+                return self._approval_list(args[1] if len(args) == 2 else None)
             if cmd == "approval" and len(args) == 1 and args[0].isdigit():
                 # [approval-payload-missing-gate-metrics] 是正 (A4 10 回目
                 # claude #69 観測 A、2026-09-11): approve/reject する前に
@@ -349,6 +355,9 @@
                    for pair, metrics in value.items()]
         return [cls._metrics_line(prefix, None)]
 
+    _APPROVAL_LIST_DEFAULT = 20
+    _APPROVAL_LIST_MAX = 200
+
     def _retry_outcome_text(self, outcome) -> str:
         """[switch-ops-hardening] T5: `ApprovalOutcome` を文言に写す。
         未知 / None はここで `ValueError` になり、`dispatch` の包括 `except`
@@ -373,6 +382,57 @@
             return f"この承認は既に決着しています (status={outcome.status})"
         raise ValueError(f"unknown approval outcome: {kind!r}")
 
+    def _approval_list(self, limit_arg: "str | None") -> str:
+        """承認待ちの一覧 + 未終端の切替ジャーナル (設計書 §3.5)。"""
+        limit = self._APPROVAL_LIST_DEFAULT
+        truncated = False
+        if limit_arg is not None:
+            try:
+                limit = int(limit_arg)
+            except ValueError:
+                return "usage: approval list [n]"
+            if limit <= 0:
+                return "usage: approval list [n]"
+            if limit > self._APPROVAL_LIST_MAX:
+                limit = self._APPROVAL_LIST_MAX
+                truncated = True
+        rows = self.conn.execute(
+            "SELECT id, kind, payload_json, created_at FROM approval_requests "
+            "WHERE status='pending' ORDER BY id ASC LIMIT ?", (limit,)).fetchall()
+        lines = []
+        for row in rows:
+            try:
+                payload = (json.loads(row["payload_json"])
+                          if row["payload_json"] else {})
+            except (TypeError, ValueError):
+                payload = {}
+            content_hash = payload.get("content_hash") or ""
+            lines.append(
+                f"#{row['id']} kind={row['kind']} "
+                f"name={payload.get('name', '-')} "
+                f"created_at={row['created_at']} "
+                f"content_hash={content_hash[:8] or '-'}")
+        if not lines:
+            lines.append("承認待ちはありません")
+        if truncated:
+            lines.append(f"(上限 {self._APPROVAL_LIST_MAX} 件で打ち切り)")
+        journal_lines = self._open_journal_lines()
+        if journal_lines:
+            lines.append("-- 未終端の切替ジャーナル --")
+            lines += journal_lines
+        return "\n".join(lines)
+
+    def _open_journal_lines(self) -> list:
+        """未終端 journal の行 (0 件なら空 list = 節ごと出さない)。
+        `plugins_root` 未配線でも DB だけで引けるが、fail-soft に揃える。"""
+        try:
+            from agentic_fx.store import plugin_switch_journal as journal_store
+            rows = journal_store.list_non_terminal(self.conn)
+        except Exception:  # noqa: BLE001 — 一覧表示は fail-soft
+            return []
+        return [f"op_id={r['op_id']} name={r['name']} phase={r['phase']} "
+                f"approval_id={r['approval_id']}" for r in rows]
+
     def _approval_detail(self, approval_id: int) -> str:
         row = self.conn.execute(
             "SELECT kind, status, payload_json, reason, decided_by, "
```

## 付録 C: 新規テストファイル (完成形)

```python path=tests/plugin/test_switch_ops_hardening.py
"""[switch-ops-hardening] 受入テスト (設計書 `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md`)。

**実 DB (`data/agentic.db`) と実 `plugins/` には一切触れない** — 全て `tmp_path` 配下
([[tests-touching-real-repo-resources]])。故障注入はモジュール属性の monkeypatch
(`tests/plugin/test_reconcile.py` と同じ流儀)。
"""
from __future__ import annotations

import ast
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.entry import main
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store.state import StateStore

_REPO = Path(__file__).resolve().parents[2]
EXAMPLES = _REPO / "docs" / "examples" / "plugins"
SETTINGS = load_settings(_REPO / "config" / "settings.yaml.example")
NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
_INJECTED = "injected: switch_live failed"
_DB = "data/agentic.db"


# ---------------------------------------------------------------- helpers

def _cli_env(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / "config" / "settings.yaml.example",
                tmp_path / "config" / "settings.yaml")
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "_human").mkdir(parents=True, exist_ok=True)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(initialized=True)
    conn = db_store.connect(tmp_path / _DB)
    db_store.init_db(conn)
    conn.close()
    return tmp_path


def _fail_switch_live(monkeypatch, *, times: int):
    real = plugin_switch.switch_live
    calls = {"n": 0}

    def _fail(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= times:
            raise OSError(_INJECTED)
        return real(*a, **kw)

    monkeypatch.setattr(plugin_switch, "switch_live", _fail)
    return real


def _stopped_at_switched(tmp_path, monkeypatch, *, times: int = 1):
    """journal=`switched` / live 未切替 / approval `pending` を作る。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    real = _fail_switch_live(monkeypatch, times=times)
    main(["plugin", "bless", "sma", "--from", "_human"])
    if times == 1:
        monkeypatch.setattr(plugin_switch, "switch_live", real)
    conn = db_store.connect(root / _DB)
    row = journal_store.get_open_by_name(conn, "sma")
    assert row is not None and row["phase"] == "switched", "前提: switched で停止"
    assert not (root / "plugins" / "sma").exists(), "前提: live は未切替"
    return root, conn, row


def _rows(conn):
    return [(r["op_id"], r["phase"]) for r in conn.execute(
        "SELECT op_id, phase FROM plugin_switch_journal ORDER BY op_id")]


def _status(conn, approval_id):
    return conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()["status"]


def _activity_text(root) -> str:
    p = root / "logs" / "activity.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------------------------------------------------------------- AC-1 / 6 / 7 / 8

@pytest.mark.slow
def test_ac1_retry_of_switched_but_unswitched_deploys(tmp_path, monkeypatch):
    """AC-1 / AC-6: 切替が失敗して journal だけ `switched` になった行を
    `approval retry` すると、**巻き戻して新しい行で配備まで完了する**。
    journal は `reverted` + `decided` の **2 行**、旧行は `reverted` のまま。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    live = plugins_root / "sma"
    assert live.is_symlink()
    assert live.readlink().as_posix().startswith(".versions/sma/")
    assert _status(conn, row["approval_id"]) == "approved"
    rows = _rows(conn)
    assert len(rows) == 2, f"停止行 + 新行の 2 行でなければならない: {rows}"
    assert rows[0] == (row["op_id"], "reverted")
    assert rows[1][1] == "decided" and rows[1][0] != row["op_id"]
    # outcome (T5)
    assert outcome.outcome == "deployed_after_rollback"
    assert outcome.rolled_back_op_id == row["op_id"]
    assert outcome.target == live.readlink().as_posix()
    assert outcome.status == "approved"


@pytest.mark.slow
def test_ac2_reconcile_on_same_row_reverts_and_keeps_pending(tmp_path, monkeypatch):
    """AC-2: 同じ状態に起動時 reconcile を掛けると `reverted` + `pending`
    (**現行と同じ = 無人経路は前に進めない**)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)

    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"
    assert not (plugins_root / "sma").exists()


@pytest.mark.slow
def test_ac7_permanent_switch_failure_keeps_one_open_row(tmp_path, monkeypatch):
    """AC-7: `switch_live` が失敗し続けても、retry 1 回につき
    「`reverted` 1 本 + 新しい `switched` 1 本」で **非終端行は常に 1 本**。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch, times=99)
    plugins_root = root / "plugins"

    for expected_rows in (2, 3):
        with pytest.raises(OSError, match="injected"):
            plugin_switch.retry_approval(
                conn, row["approval_id"], decided_by="human", now=NOW,
                plugins_root=plugins_root, settings=SETTINGS)
        rows = _rows(conn)
        assert len(rows) == expected_rows
        assert sum(1 for _op, ph in rows if ph not in ("decided", "reverted")) == 1
        assert _status(conn, row["approval_id"]) == "pending"
        assert not (plugins_root / "sma").exists()


@pytest.mark.slow
def test_ac8_retry_after_success_is_noop(tmp_path, monkeypatch):
    """AC-8: 成功後にもう一度 retry しても何も起きない (`already_decided`)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch.retry_approval(conn, row["approval_id"], decided_by="human",
                                 now=NOW, plugins_root=plugins_root, settings=SETTINGS)
    before_rows, before_link = _rows(conn), (plugins_root / "sma").readlink()

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "already_decided" and outcome.status == "approved"
    assert _rows(conn) == before_rows
    assert (plugins_root / "sma").readlink() == before_link


# ---------------------------------------------------------------- AC-3 (foreign)

def _make_foreign(root, row):
    plugins_root = root / "plugins"
    other = plugins_root / ".versions" / "sma" / ("f" * 64)
    other.mkdir(parents=True, exist_ok=True)
    tmp_link = plugins_root / ".sma.foreign"
    tmp_link.symlink_to(f".versions/sma/{'f' * 64}")
    os.rename(tmp_link, plugins_root / "sma")


@pytest.mark.slow
def test_ac3_foreign_live_is_never_touched(tmp_path, monkeypatch):
    """AC-3: live が new でも old でもない先を指しているとき、
    retry も reconcile も **live を 1 バイトも変えない** (fail closed)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    _make_foreign(root, row)
    before = (plugins_root / "sma").readlink()

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS,
        activity=_activity(root))
    assert outcome.outcome == "foreign_waiting" and outcome.op_id == row["op_id"]
    assert (plugins_root / "sma").readlink() == before
    assert _status(conn, row["approval_id"]) == "pending"
    assert _rows(conn) == [(row["op_id"], "switched")]
    assert "switch_retry_unrecognized_live_target" in _activity_text(root)

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))
    assert (plugins_root / "sma").readlink() == before
    assert _rows(conn) == [(row["op_id"], "switched")]
    assert "switch_reconcile_unrecognized_live_target" in _activity_text(root)


def _activity(root):
    from agentic_fx.activity import ActivityLog
    return ActivityLog(root / "logs" / "activity.log")


# ---------------------------------------------------------------- AC-9 (lock / stale row)

@pytest.mark.slow
def test_ac9a_reconcile_revert_holds_the_plugin_lock(tmp_path, monkeypatch):
    """AC-9a: reconcile の巻き戻しは `plugins/.locks/<name>.lock` の内側。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    held = []

    @_ctx.contextmanager
    def _spy(rt, name):
        with real(rt, name):
            held.append((name, plugin_switch.journal_store.get(conn, row["op_id"])["phase"]))
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)
    assert held == [("sma", "switched")], "巻き戻しは lock の内側で行われていない"


@pytest.mark.slow
def test_ac9b_i_stale_row_both_changed(tmp_path, monkeypatch):
    """AC-9b-i: lock 待ちの間に競合者が畳んで配備まで完了した場合、
    reconcile は **何もしない** (live は new のまま、approval は approved)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    done = threading.Event()

    def _competitor():
        c2 = db_store.connect(root / _DB)
        try:
            plugin_switch.retry_approval(
                c2, row["approval_id"], decided_by="competitor", now=NOW,
                plugins_root=plugins_root, settings=SETTINGS)
        finally:
            c2.close()
            done.set()

    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        # **lock を取る直前**に競合者を走らせ、終わる (= lock を解放する) まで待つ。
        if fired["n"] == 0:
            fired["n"] = 1
            th = threading.Thread(target=_competitor, daemon=True)
            th.start()
            assert done.wait(timeout=30), "競合者が 30 秒で終わらない (deadlock)"
            th.join(timeout=30)
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    live = plugins_root / "sma"
    assert live.is_symlink(), "stale 行で巻き戻され、配備が消えた (IV-3 の破れ)"
    assert _status(conn, row["approval_id"]) == "approved"
    rows = _rows(conn)
    assert rows[0] == (row["op_id"], "reverted") and rows[1][1] == "decided"
    assert "switch_reconcile_skipped_stale_row" in _activity_text(root)


@pytest.mark.slow
def test_ac9b_ii_live_changed_phase_same(tmp_path, monkeypatch):
    """AC-9b-ii: phase は `switched` のまま live だけ第三者が張り替えた場合
    (**分類の再確認**だけが検出できる)。live は触られない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        if fired["n"] == 0:
            fired["n"] = 1
            _make_foreign(root, row)  # phase は変えない
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    assert (plugins_root / "sma").is_symlink(), "第三者の symlink を消してはならない"
    assert (plugins_root / "sma").readlink().as_posix().endswith("f" * 64)
    assert _rows(conn) == [(row["op_id"], "switched")]
    text = _activity_text(root)
    assert "switch_reconcile_skipped_stale_row" in text
    assert "class_now=foreign" in text


@pytest.mark.slow
def test_ac9b_iii_phase_changed_live_same(tmp_path, monkeypatch):
    """AC-9b-iii: live の分類は同じまま phase だけ終端化した場合
    (**行の再取得**だけが検出できる)。終端行に二重の巻き戻しを記録しない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        if fired["n"] == 0:
            fired["n"] = 1
            c2 = db_store.connect(root / _DB)
            try:  # 競合者は巻き戻しだけして配備に進まない
                plugin_switch._revert_one(
                    c2, journal_store.get(c2, row["op_id"]),
                    plugins_root=plugins_root, now=NOW)
                c2.commit()
            finally:
                c2.close()
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    assert _rows(conn) == [(row["op_id"], "reverted")]
    text = _activity_text(root)
    assert "switch_reconcile_skipped_stale_row" in text
    # 競合者は activity を渡していないので、**正しい実装なら
    # `switch_reverted` は 1 行も出ない**。行の再取得を落とすと reconcile が
    # 終端行に対して `_revert_one` を再実行し、この行が 1 本出て red になる。
    assert "switch_reverted" not in text, "終端行への二重の巻き戻し記録"


@pytest.mark.slow
def test_ac9c_force_revert_takes_the_lock_and_refetches(tmp_path, monkeypatch):
    """AC-9c: `force_revert_op_id` も lock + 行の再取得を通る
    (**分類の一致は求めない** — phase 無視の割込という既存の意味論)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    held = []

    @_ctx.contextmanager
    def _spy(rt, name):
        held.append(name)
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        force_revert_op_id=row["op_id"])
    assert held == ["sma"]
    assert _rows(conn) == [(row["op_id"], "reverted")]


# ---------------------------------------------------------------- AC-16 (2 段ガード)

@pytest.mark.slow
def test_ac16a_entry_guard_refuses_terminal_row_before_touching_fs(tmp_path, monkeypatch):
    """AC-16a(1): `_advance_to_decided` は終端行を渡されると **FS を触る前**に
    `ValueError`。live も journal も変わらない。"""
    from agentic_fx.plugin.gate_pytest import hashes_of
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch._revert_one(conn, row, plugins_root=plugins_root, now=NOW)
    conn.commit()
    candidate_dir = plugins_root / "_human" / "sma"
    content_hash, artifact_hash = hashes_of(candidate_dir)

    with pytest.raises(ValueError, match="terminal/missing"):
        plugin_switch._advance_to_decided(
            conn, row["approval_id"], op_id=row["op_id"], name="sma",
            plugins_root=plugins_root, candidate_dir=candidate_dir,
            content_hash=content_hash, artifact_hash=artifact_hash,
            switch_required=True, decided_by="probe", now=NOW)

    assert not (plugins_root / "sma").exists(), "FS に触れてから落ちている"
    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"


@pytest.mark.slow
def test_ac16a_finalize_guard_refuses_wrong_phase(tmp_path, monkeypatch):
    """AC-16a(2): `_finalize_decision` は期待 phase 以外を `ValueError`。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugin_switch._revert_one(conn, row, plugins_root=root / "plugins", now=NOW)
    conn.commit()
    with pytest.raises(ValueError, match="expects phase='switched'"):
        plugin_switch._finalize_decision(
            conn, row["approval_id"], op_id=row["op_id"], decided_by="probe", now=NOW)
    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"


@pytest.mark.slow
def test_ac16b_switch_required_zero_reaches_decided(tmp_path, monkeypatch):
    """AC-16b: 同一候補の再 bless は `switch_required=0` の行を作り、
    `recorded` から `decided` へ進む (ガードを常に `switched` 期待にすると red)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    seen = []
    real = plugin_switch._finalize_decision

    def _spy(conn_, approval_id, *, op_id, decided_by, now):
        r = journal_store.get(conn_, op_id)
        seen.append((r["phase"], r["switch_required"]))
        return real(conn_, approval_id, op_id=op_id, decided_by=decided_by, now=now)

    monkeypatch.setattr(plugin_switch, "_finalize_decision", _spy)
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0

    assert seen == [("switched", 1), ("recorded", 0)]
    conn = db_store.connect(root / _DB)
    assert [ph for _op, ph in _rows(conn)] == ["decided", "decided"]
    conn.close()


# ---------------------------------------------------------------- AC-14d (網羅性)

def test_ac14d_no_bare_return_in_approval_entrypoints():
    """AC-14d(1): `approve_candidate` / `retry_approval` の**関数本体**に
    bare `return` / `return None` が無い (入れ子関数は対象外 — 内部 helper は
    値を返さなくてよい)。"""
    src = Path(plugin_switch.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn_name in ("approve_candidate", "retry_approval"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        bad = []
        for node in _walk_own_body(fn):
            if isinstance(node, ast.Return) and (
                    node.value is None
                    or (isinstance(node.value, ast.Constant) and node.value.value is None)):
                bad.append(node.lineno)
        assert not bad, f"{fn_name}: outcome を返さない return が {bad} 行目にある"


def _walk_own_body(fn):
    """`fn` の本体を走査する。**入れ子の関数定義の中には入らない**。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


@pytest.mark.slow
def test_ac14d_outcomes_are_known_enum_values(tmp_path, monkeypatch):
    """AC-14d(2): 主要な `return` 地点が既知 enum の outcome を返す。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    seen = set()

    # foreign_waiting
    _make_foreign(root, row)
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root)).outcome)
    # deployed_after_rollback (live を not_switched に戻してから)
    (plugins_root / "sma").unlink()
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS).outcome)
    # already_decided
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS).outcome)
    assert seen == {"foreign_waiting", "deployed_after_rollback", "already_decided"}
    assert seen <= plugin_switch.APPROVAL_OUTCOMES


@pytest.mark.slow
def test_ac14d_still_pending_on_missing_candidate(tmp_path, monkeypatch):
    """AC-14d(2) の続き: 候補が消えた行は `still_pending(candidate_missing)`。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    shutil.rmtree(plugins_root / "_human" / "sma")
    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)
    assert outcome.outcome == "still_pending"
    assert outcome.reason == "candidate_missing"
    assert outcome.status == "pending"


def test_ac5_classifier_compares_the_raw_readlink_string(tmp_path):
    """AC-5: 分類は `readlink` の**生文字列**で行う (`resolve()` ではない)。

    同じ実体を指すが綴りが違う symlink (**絶対パス**で張られたもの) は
    **`foreign`** — 正規の書き手 (`switch_live`) は必ず plugins_root 相対の
    `.versions/<name>/<hash>` を書くので、綴りが違えば第三者が触った印として
    fail closed に扱う。比較を `resolve()` にすると `switched` に化けて red。
    (`./` 付きの綴りでは試験にならない — `Path.as_posix()` が単一ドットを
    畳んでしまい、生文字列でも一致してしまう。実測して絶対パスに変えた。)"""
    plugins_root = tmp_path / "plugins"
    target = f".versions/sma/{'a' * 64}"
    (plugins_root / target).mkdir(parents=True)
    (plugins_root / "sma").symlink_to((plugins_root / target).resolve())
    row = {"op_id": 1, "name": "sma", "phase": "switched", "switch_required": 1,
           "new_target": target, "old_target": None, "old_kind": "absent"}

    assert plugin_switch.classify_live(plugins_root, row) == "foreign"

    # 正規の綴りなら `switched`
    (plugins_root / "sma").unlink()
    (plugins_root / "sma").symlink_to(target)
    assert plugin_switch.classify_live(plugins_root, row) == "switched"


def test_classifier_refuses_rows_it_does_not_apply_to(tmp_path):
    """分類器は `phase='switched'` かつ `switch_required=1` の行専用 (fail closed)。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    base = {"op_id": 1, "name": "sma", "new_target": ".versions/sma/x",
            "old_target": None, "old_kind": "absent"}
    for phase, required in (("recorded", 1), ("switched", 0)):
        with pytest.raises(ValueError, match="分類器は"):
            plugin_switch.classify_live(
                plugins_root, {**base, "phase": phase, "switch_required": required})


@pytest.mark.slow
def test_ac14a_already_decided_status_comes_from_the_row(tmp_path, monkeypatch):
    """AC-14a: `already_decided` の `status` は **lock 内で読んだ行の値**。
    リテラル (`"approved"` 等) に潰すと red — reject した approval を retry すると
    `status="rejected"` が返らなければならない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch.reject_candidate(
        conn, row["approval_id"], decided_by="human", reason="",
        now=NOW, plugins_root=plugins_root)

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "already_decided"
    assert outcome.status == "rejected", "status が行の値でなくリテラルになっている"
```

## 付録 D-1: `tests/test_commands.py` — **T5 分** (spy の書き換え + retry の結果報告 7 本)

**v1.1 で分割** (付録 B と同じ理由 — v1.0 の `@@ -939,3` に T5 の retry 系と T7 の
`approval list` 系が同居していた)。D-1 → D-2 の順で v1.0 の付録 D と**バイト一致**。

```diff path=tests/test_commands.py
--- tests/test_commands.py
+++ tests/test_commands.py
@@ -2,9 +2,12 @@
 from pathlib import Path
 from unittest.mock import MagicMock
 
+import os
+
 import pytest
 
 from agentic_fx.activity import ActivityLog, Category
+from agentic_fx.plugin import switch as plugin_switch
 from agentic_fx.commands import Commands
 from agentic_fx.config import load_settings
 from agentic_fx.core.contracts import FixedClock
@@ -553,7 +556,13 @@
 
     def _spy(conn_, approval_id, *, decided_by, now, plugins_root, settings,
             activity=None):
+        # [switch-ops-hardening] T5: 実物は **必ず `ApprovalOutcome` を返す**。
+        # 旧 spy は `None` を返す「実物より緩い fake」で、シェルが outcome を
+        # 文言に写す契約 (設計書 §3.5) を素通りさせていた。
         calls.append((approval_id, decided_by))
+        return plugin_switch.ApprovalOutcome(
+            outcome="deployed", name="sma", status="approved", op_id=7,
+            target=".versions/sma/" + "a" * 64)
 
     monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval", _spy)
 
@@ -561,6 +570,7 @@
 
     assert calls == [(42, "shell")]
     assert "再試行" in out
+    assert "配備まで完了しました" in out
     records = activity.tail(10, Category.APPROVAL)
     assert any("retry" in r for r in records)
 
@@ -939,3 +949,139 @@
     assert row["status"] == "pending"
     # lock ファイルも作られない (`.locks` の mkdir より前に落ちる)
     assert not (plugins_dir / ".locks").exists()
+
+
+# ============================================================
+# [switch-ops-hardening] T5 / T7 — `approval list` と retry の結果報告
+# ============================================================
+
+
+def _fake_outcome(**kw):
+    base = dict(outcome="deployed", name="sma", status="approved", op_id=3,
+                target=".versions/sma/" + "a" * 64)
+    base.update(kw)
+    return plugin_switch.ApprovalOutcome(**base)
+
+
+@pytest.mark.parametrize("outcome,expected", [
+    (_fake_outcome(), "配備まで完了しました (plugins/sma → .versions/sma/" + "a" * 64 + ")"),
+    (_fake_outcome(outcome="deployed_after_rollback", rolled_back_op_id=2),
+     "中断していた切替 (op_id=2) を巻き戻してから再実行し、配備まで完了しました"),
+    (_fake_outcome(outcome="foreign_waiting", status="pending", target=None),
+     "live が第三者に触られているため自動収束しません (op_id=3)"),
+    (_fake_outcome(outcome="still_pending", status="pending", target=None,
+                   reason="candidate_missing"),
+     "approved になりませんでした (reason=candidate_missing)"),
+    (_fake_outcome(outcome="legacy_plain_present", status="pending", target=None),
+     "plugins/sma が旧式のディレクトリのままです"),
+    (_fake_outcome(outcome="already_decided", status="rejected", target=None),
+     "この承認は既に決着しています (status=rejected)"),
+])
+def test_approval_retry_reports_the_locked_outcome(tmp_path, monkeypatch, outcome,
+                                                   expected):
+    """AC-14a: シェルは lock 内で確定した outcome を文言に写すだけ (全 6 行)。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    plugins_dir = tmp_path / "plugins"
+    (plugins_dir / ".locks").mkdir(parents=True)
+    cmds.plugins_root = plugins_dir
+    cmds.settings = SETTINGS
+    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
+                        lambda *a, **kw: outcome)
+
+    out = cmds.dispatch("approval retry 7")
+
+    assert out.startswith("approval #7 を再試行しました: ")
+    assert expected in out
+
+
+def test_approval_retry_fails_loud_on_unknown_outcome(tmp_path, monkeypatch):
+    """AC-14d(3): 未知 / None の outcome は**文言にしない** (fail loud)。
+    接頭辞も付かず `エラー: ...` になる。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    plugins_dir = tmp_path / "plugins"
+    (plugins_dir / ".locks").mkdir(parents=True)
+    cmds.plugins_root = plugins_dir
+    cmds.settings = SETTINGS
+    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
+                        lambda *a, **kw: None)
+
+    out = cmds.dispatch("approval retry 7")
+
+    assert out.startswith("エラー: ")
+    assert "を再試行しました" not in out
+
+
+def test_approval_retry_missing_id_is_value_error_with_help(tmp_path, monkeypatch):
+    """AC-14a: 存在しない approval への retry は `ValueError` 送出で、
+    先行する `except (ValueError, KeyError)` に捕まり `_HELP` が付く。"""
+    from agentic_fx.commands import _HELP
+    conn, _, _, cmds = _commands(tmp_path)
+    plugins_dir = tmp_path / "plugins"
+    (plugins_dir / ".locks").mkdir(parents=True)
+    cmds.plugins_root = plugins_dir
+    cmds.settings = SETTINGS
+
+    def _raise(*a, **kw):
+        raise ValueError("approval 42 not found")
+
+    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval", _raise)
+
+    out = cmds.dispatch("approval retry 42")
+
+    assert out == f"エラー: ValueError: approval 42 not found\n{_HELP}"
+
+
+def test_approval_retry_message_unaffected_by_later_deployment(tmp_path, monkeypatch):
+    """AC-14b: lock 解放後に別の正規配備が live を進めても、**この retry の
+    報告は自分の target のまま**。シェルが lock 外で読み直さないことの pin。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    plugins_dir = tmp_path / "plugins"
+    (plugins_dir / ".locks").mkdir(parents=True)
+    (plugins_dir / ".versions" / "sma" / ("a" * 64)).mkdir(parents=True)
+    (plugins_dir / ".versions" / "sma" / ("z" * 64)).mkdir(parents=True)
+    (plugins_dir / "sma").symlink_to(".versions/sma/" + "a" * 64)
+    cmds.plugins_root = plugins_dir
+    cmds.settings = SETTINGS
+    mine = _fake_outcome()
+
+    def _retry_then_competitor(*a, **kw):
+        # retry は自分の target まで配備した。その直後に別プロセスが
+        # **正規に**次の版を配備して live を進める (IV-3 は「approved に
+        # なった瞬間」の条件なので、これは契約違反ではない)。
+        tmp_link = plugins_dir / ".sma.next"
+        tmp_link.symlink_to(".versions/sma/" + "z" * 64)
+        os.rename(tmp_link, plugins_dir / "sma")
+        return mine
+
+    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
+                        _retry_then_competitor)
+
+    out = cmds.dispatch("approval retry 7")
+
+    assert "配備まで完了しました" in out
+    assert "a" * 64 in out, "後続配備の target を報告してはならない"
+    assert "z" * 64 not in out
+    assert "一致しません" not in out
+
+
+def test_approval_retry_fails_loud_on_unknown_enum_value(tmp_path, monkeypatch):
+    """AC-14d(3) の対: **未知の enum 値**も文言にしない (fail loud)。
+    `None` は属性参照で落ちるが、この経路は文字列の網羅漏れを狙う。"""
+    from types import SimpleNamespace
+    conn, _, _, cmds = _commands(tmp_path)
+    plugins_dir = tmp_path / "plugins"
+    (plugins_dir / ".locks").mkdir(parents=True)
+    cmds.plugins_root = plugins_dir
+    cmds.settings = SETTINGS
+    monkeypatch.setattr(
+        "agentic_fx.plugin.switch.retry_approval",
+        lambda *a, **kw: SimpleNamespace(outcome="brand_new_outcome", name="sma",
+                                         status="pending", op_id=1,
+                                         rolled_back_op_id=None, target=None,
+                                         reason=None))
+
+    out = cmds.dispatch("approval retry 7")
+
+    assert out.startswith("エラー: ")
+    assert "を再試行しました" not in out
+    assert "brand_new_outcome" in out
```

## 付録 D-2: `tests/test_commands.py` — **T7 分** (`approval list` 5 本)

```diff path=tests/test_commands.py
--- tests/test_commands.py
+++ tests/test_commands.py
@@ -955,6 +955,74 @@
 # [switch-ops-hardening] T5 / T7 — `approval list` と retry の結果報告
 # ============================================================
 
+def _pending_plugin_approval(conn, name="sma", chash="a" * 64):
+    return approvals.create(
+        conn, kind="plugin",
+        payload={"name": name, "content_hash": chash, "artifact_hash": "b" * 64,
+                 "candidate_origin": "staging",
+                 "candidate_path": f"plugins/_staging/1/{name}"},
+        now=NOW)
+
+
+def test_approval_list_shows_pending_ids_and_no_metrics(tmp_path):
+    """AC-12a / AC-13: pending を id 昇順で列挙し、holdout / in_sample の
+    数値は出さない (詳細は `approval <id>`)。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    a1 = _pending_plugin_approval(conn, "sma")
+    a2 = _pending_plugin_approval(conn, "ema", chash="c" * 64)
+
+    out = cmds.dispatch("approval list")
+
+    assert out.index(f"#{a1}") < out.index(f"#{a2}")
+    assert "name=sma" in out and "name=ema" in out
+    assert "content_hash=aaaaaaaa" in out
+    for banned in ("in_sample", "holdout", "pf=", "avg_r"):
+        assert banned not in out
+
+
+def test_approval_list_empty_message(tmp_path):
+    """AC-12a: 0 件の文言。未終端 journal が無ければ節ごと出ない。"""
+    _, _, _, cmds = _commands(tmp_path)
+    out = cmds.dispatch("approval list")
+    assert out == "承認待ちはありません"
+    assert "未終端" not in out
+
+
+def test_approval_list_includes_open_journal_section(tmp_path):
+    """AC-12a: 未終端 journal があれば末尾の節に op_id / name / phase / approval_id。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    aid = _pending_plugin_approval(conn, "sma")
+    op_id = plugin_switch.begin_switch_journal(
+        conn, kind="bless", approval_id=aid, name="sma", old_kind="absent",
+        old_target=None, new_target=".versions/sma/" + "b" * 64,
+        switch_required=True, actor="human_cli", now=NOW, commit=True)
+
+    out = cmds.dispatch("approval list")
+
+    assert "-- 未終端の切替ジャーナル --" in out
+    assert f"op_id={op_id} name=sma phase=preparing approval_id={aid}" in out
+
+
+def test_approval_list_limit_and_cap(tmp_path):
+    """AC-12b: `<n>` が効き、既定 20 / 上限 200 で打ち切る。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    for i in range(25):
+        _pending_plugin_approval(conn, "sma", chash=f"{i:064d}")
+    assert len(cmds.dispatch("approval list 5").splitlines()) == 5
+    assert len(cmds.dispatch("approval list").splitlines()) == 20
+    out = cmds.dispatch("approval list 999")
+    assert "(上限 200 件で打ち切り)" in out
+
+
+def test_approval_list_rejects_bad_argument(tmp_path):
+    """AC-12c: 0 / 負数 / 非数値は usage。既存の `approval <id>` を壊さない。"""
+    conn, _, _, cmds = _commands(tmp_path)
+    aid = _pending_plugin_approval(conn, "sma")
+    for bad in ("approval list 0", "approval list -1", "approval list abc"):
+        assert cmds.dispatch(bad) == "usage: approval list [n]"
+    assert f"approval #{aid}" in cmds.dispatch(f"approval {aid}")
+    assert "存在しません" in cmds.dispatch("approval 999")
+
 
 def _fake_outcome(**kw):
     base = dict(outcome="deployed", name="sma", status="approved", op_id=3,
```

## 付録 E: `tests/backtest/test_cli.py` の追記 (T6)

```diff path=tests/backtest/test_cli.py
--- tests/backtest/test_cli.py
+++ tests/backtest/test_cli.py
@@ -1609,3 +1609,26 @@
                     "rsi_pullback"]) == 1
     assert capsys.readouterr().err.strip().splitlines()[-1] == \
         "indicator_unresolved:rsi:not_found"
+
+
+def test_plugin_materialize_containment_error_is_rc1_message(tmp_path, monkeypatch, capsys):
+    """[switch-ops-hardening] AC-11: `materialize_plugin` の containment 拒否
+    (`ValueError`) は `_plugin_materialize` の中で処理され、rc=1 と
+    `エラー: ` の 1 行になる (外側の包括 catch に落ちない)。"""
+    import argparse as _argparse
+
+    from agentic_fx.backtest import cli as _cli
+    from agentic_fx.plugin import switch as _switch
+
+    def _boom(root, name):
+        raise ValueError(
+            "materialize_plugin: live symlink target escapes plugins_root: /x")
+
+    monkeypatch.setattr(_switch, "materialize_plugin", _boom)
+    rc = _cli._plugin_materialize(
+        None, None, _argparse.Namespace(name="sma"), tmp_path)
+
+    assert rc == 1
+    err = capsys.readouterr().err
+    assert err.startswith("エラー: ")
+    assert "escapes plugins_root" in err
```

## 付録 F: `tests/plugin/test_indicator_initial_set.py` の書き換え 2 本 (**T5**・T6)

**v1.1 の訂正**: 1 本目 (`test_runbook_post_gate_failure_converges_via_approval_retry`、
hunk `@@ -797,15` と `@@ -851,8`) は **T3 ではなく T5 の材料** — 書き換え後の assert が
`_retry_outcome_text` (T5) の出す文言 (`配備まで完了しました`) を見ているため。**T3 時点では
このテストは無改変で緑**であることを実測した。2 本目 (`@@ -996,16` と `@@ -1028,6`、
`test_runbook_cli_bless_after_post_gate_failure_raises_traceback`) は従来どおり T6。

```diff path=tests/plugin/test_indicator_initial_set.py
--- tests/plugin/test_indicator_initial_set.py
+++ tests/plugin/test_indicator_initial_set.py
@@ -797,15 +797,13 @@
     |---|---|---|
     | `preparing` (版作成で失敗) | journal 終端 + **配備完了** | 手順 5 の確認。同内容なので no-op |
     | `versioned` / `recorded` (history / 切替直前) | journal 終端 + **配備完了** | 同上 (本テストのケース) |
-    | `switched` (切替で失敗し live が新 target でない) | journal 終端・approval `approved` だが **配備されない** (`approve_candidate` の 0d は `switched` を `_reverify_switched_journal` → `_finalize_decision` で閉じるだけで `switch_live` を呼ばない、`switch.py:1440-1454`) | **必須** |
+    | `switched` (切替で失敗し live が新 target でない) | journal 終端 + **配備完了** (停止行を巻き戻して閉じ、新しい journal 行で手順を頭から流す — [switch-ops-hardening] 案 C) | 確認 (no-op) |
 
-    `switched` の行は **`[retry-switched-approves-without-deploy]` として指揮者へ
-    申告済みの観測**であり、本テストの対象ではない (起動時 reconcile なら
-    live target を見て `_revert_one` する — `test_switch_journal.py::
-    test_switched_recovery_absent_old_kind_with_no_live_reverts`)。
+    `switched` の行は [switch-ops-hardening] で解消済み。0d は live の指す先を
+    分類し、`not_switched` なら巻き戻してから新しい行で配備まで完了させる。
+    live が第三者に触られている (`foreign`) ときだけ、触らず人間待ちになる。
     §6.3 (B) の手順 5 (「もう一度 bless して `UnresolvedJournalError` が出ない
-    ことを確認する。そのまま成功すれば配備完了」) は **どちらの phase でも
-    正しく働く**ので、runbook は手順 5 を必ず実行する形のままでよい。
+    ことを確認する」) は **どの phase でも確認 (no-op)** として働く。
 
     状態遷移そのものは `tests/plugin/test_switch_journal.py` /
     `test_reconcile.py` が pin 済みなので再実装しない (設計書 §6.1)。
@@ -851,8 +849,13 @@
         activity=ActivityLog(root / "logs" / "activity.log"),
         log_dir=root / "logs", clock=clock, health_latch=HealthLatch(),
         plugins_root=plugins_dir, settings=SETTINGS)
-    assert cmds.dispatch(f"approval retry {approval_id}") == (
-        f"approval #{approval_id} を再試行しました")
+    # [switch-ops-hardening] T5: シェルは lock 内で確定した outcome を文言に
+    # 写す (設計書 §3.5)。`versioned` からの retry は新規経路を流し切るので
+    # `deployed`。
+    retry_out = cmds.dispatch(f"approval retry {approval_id}")
+    assert retry_out.startswith(f"approval #{approval_id} を再試行しました: ")
+    assert "配備まで完了しました" in retry_out
+    assert f"plugins/rsi → .versions/rsi/" in retry_out
 
     # `versioned` からの retry は手順を頭から冪等に流すので、journal の終端と
     # **配備の完了**が同時に起きる。
@@ -996,16 +999,12 @@
 def test_runbook_cli_bless_after_post_gate_failure_raises_traceback(
         tmp_path, monkeypatch, capsys):
     """I8 (CLI ゲート後失敗、設計書 §6.3 (B)): 未終端 journal が残った状態で
-    同名を再 bless すると、CLI から **`UnresolvedJournalError` が素通りして
-    traceback になる** ([[cli-bless-unresolved-journal]])。
-
-    **これは「現状をそのまま pin する」テストであり、望ましい姿ではない** —
-    `UnresolvedJournalError` は `Exception` 直下 (`switch.py:1572`) なので
-    `_plugin_bless` の `except (ValueError, SandboxError)` にも
-    `dispatch` の `except (ValueError, KeyError, OSError, sqlite3.Error, ...)`
-    にも掛からない。ticket [cli-bless-unresolved-journal] が直ったら
-    (rc=1 + 収束手順の案内を stderr に出す形になるはず)、**このテストは
-    その新しい振る舞いへ書き換える**こと。
+    同名を再 bless すると、CLI は **rc=1 と `エラー: ` の 1 行**で拒否する
+    ([switch-ops-hardening] T6 で是正。旧稿は `UnresolvedJournalError` が
+    `_plugin_bless` の `except (ValueError, SandboxError)` を素通りして
+    **traceback** になるのを pin していた — その旧挙動をこの版で置き換えた)。
+    メッセージには収束に必要な `op_id` と `approval_id`、および次の 1 手が
+    含まれる。
 
     1 回目の失敗注入 (`record_version` の `OSError`) は逆に
     `dispatch` の `except OSError` に**捕まる**ので rc=1 になる — 同じ
@@ -1028,6 +1027,12 @@
     finally:
         conn.close()
 
-    with pytest.raises(plugin_switch.UnresolvedJournalError) as excinfo:
-        main(["plugin", "bless", "sma", "--from", "_human"])
-    assert f"op_id={open_journal['op_id']}" in str(excinfo.value)
+    rc2 = main(["plugin", "bless", "sma", "--from", "_human"])
+    err2 = capsys.readouterr().err
+    assert rc2 == 1
+    assert "Traceback" not in err2
+    assert f"op_id={open_journal['op_id']}" in err2
+    assert f"approval_id={open_journal['approval_id']}" in err2
+    # **この行は T6 で足した案内文そのもの** — 元の例外文言にも
+    # "approval retry" は含まれるので、そちらでは判別力がない (変異で実測)。
+    assert "収束手順: サービスの対話シェルで `approval list`" in err2
```

## 付録 G: `tests/plugin/test_reconcile.py` の書き換え 1 本 (T4)

```diff path=tests/plugin/test_reconcile.py
--- tests/plugin/test_reconcile.py
+++ tests/plugin/test_reconcile.py
@@ -473,4 +473,8 @@
         conn, plugins_root=plugins_root, now=fx.NOW, settings=SETTINGS,
         activity=activity)
 
-    assert acquired == sorted({"rsi_pullback", "rsi"})
+    # [switch-ops-hardening] T4: pin 破れの巻き戻しも name lock の内側で
+    # 行うようになった (§3.3.3 の 3 箇所目) ため、解決時の 2 本に続いて
+    # **巻き戻しの 1 本**が増える。この pin の主旨 (解決が依存 lock の
+    # 内側であること) は不変で、増えた 1 本は巻き戻し専用。
+    assert acquired == [*sorted({"rsi_pullback", "rsi"}), "rsi_pullback"]
```

---

## 着手前検証の記録 (起草者による実測、2026-09-19)

**このプランのコードは「書いただけ」ではない。** scratchpad に repo の `src/agentic_fx` を
コピーした**隔離環境** (PYTHONPATH overlay。**実 repo の `src/` `tests/` は 1 行も変更して
いない**) を作り、T1〜T7 の実装を実際に当てて実測した。

### 1. 環境

```
overlay = <scratchpad>/overlay/agentic_fx      # src/agentic_fx のコピー (ここを編集)
tests   = <scratchpad>/tests                   # tests/ のコピー
実行     = cd <scratchpad> && PYTHONPATH=<scratchpad>/overlay .venv/bin/python -m pytest ...
```

overlay が本当に使われていることは、`agentic_fx.__file__` が overlay 側を指すことと、
番兵行を足して読めることで確認済み。

### 2. 新規テスト

| ファイル | 本数 |
|---|---|
| `tests/plugin/test_switch_ops_hardening.py` (新規) | **19** |
| `tests/test_commands.py` + `tests/backtest/test_cli.py` への追記 | **16** (119 → 135) |

合計 **35 本の新規テスト**。

### 3. 既存の関連テスト (overlay に対して)

```
tests/plugin/ tests/test_commands.py tests/backtest/test_cli.py
tests/fixtures/test_wiring_envs.py tests/test_service_app.py
tests/store/test_plugin_switch_journal.py
  → 974 passed, 1 failed in 244.60s
```

**唯一の failed = `tests/plugin/test_gate_pytest.py::test_gate_pytest_worker_argv_pins_cacheprovider_and_rootdir`
は本束と無関係の環境要因**。同テストは `<cwd>/src/agentic_fx/plugin/gate_pytest_worker.py`
をパスで読むため、cwd が repo でない隔離環境では必ず落ちる。**overlay を外して同じ cwd で
走らせても同一の `FileNotFoundError` で落ちる**ことを実測して切り分けた。
**repo 上で実装するときは発生しない** — ただし T8 のフルスイートで確認すること。

### 4. 逆変異スイープ (**実走 18 件、全 KILLED**。机上の 7 件は表で `★未実測`)

`cp` 退避 → 置換 → 対象テスト実行 → 復元 を機械的に回した (`git checkout` は使っていない)。

```
[KILLED] M1  switched の復旧を常に完遂にする (0d)          -> 1 failed
[KILLED] M2  分類の比較を resolve() 化                      -> 1 failed
[KILLED] M3  op_id = None のリセットを落とす                -> 1 failed
[KILLED] M4  lock 内の行の再取得を落とす                    -> 1 failed
[KILLED] M5  lock 内の分類の再確認を落とす                  -> 1 failed
[KILLED] M6  入口ガードを外す                               -> 1 failed
[KILLED] M7  _finalize_decision のガードを常に switched 期待 -> 1 failed
[KILLED] M8  outcome を返し忘れる return を 1 つ作る        -> 1 failed
[KILLED] M9  巻き戻しから lock を外す (reconcile)           -> 1 failed
[KILLED] M10 CLI の except から UnresolvedJournalError を外す -> 1 failed
[KILLED] M11 foreign を触ってしまう (0d)                    -> 1 failed
[KILLED] M12 シェルが未知 outcome を黙って文言にする        -> 1 failed
[KILLED] T2-M3 finalize ガードを現在 phase と同値にする      -> 1 failed
[KILLED] T4-M4 stale 判定を「行が消えた」だけにする          -> 1 failed
[KILLED] T5-M2 deployed_after_rollback を deployed に潰す    -> 1 failed
[KILLED] T5-M6 already_decided の status をリテラルにする     -> 1 failed
[KILLED] T5-M4 シェルが固定文言に戻る                         -> 1 failed
[KILLED] T6-M3 bless の案内文から次の 1 手を落とす            -> 1 failed
[KILLED] T7-M1 dispatch 条件を args == ['list'] に戻す        -> 1 failed
[KILLED] T7-M2 上限の丸めを落とす                             -> 1 failed
[KILLED] T7-M3 不正引数を既定値に丸める                       -> 1 failed
[KILLED] T7-M4 journal の節を 0 件でも出す                    -> 1 failed
[KILLED] T7-M5 並び順を降順にする                             -> 1 failed
ALL KILLED (18/18)
```

**途中で 4 件が SURVIVED し、テストを作り直した** (記録として残す — 変異は「テストの
判別力」を測る道具であって、通ったことの証明ではない):

- **M2 (`resolve()` 化)**: 最初は AC-1 で殺そうとしたが survive した (版ディレクトリが
  実在する通常ケースでは raw と resolve が一致する)。`./` 付きの綴りでも駄目だった —
  **`Path.as_posix()` が単一ドットを畳む**ので生文字列でも一致してしまう (実測)。
  **絶対パスで張った symlink** なら raw では `foreign`・resolve では `switched` と分かれる
  ので、その形の単体テストに作り直して KILLED。
- **M12 (未知 outcome を黙って文言にする)**: 最初のテストは `None` を渡していたため
  属性参照で先に落ち、`raise` の有無を判別できなかった。**未知の enum 文字列**を持つ
  stub を渡すテストを足して KILLED。
- **T5-M6 (`already_decided` の `status` をリテラルに潰す)**: 文言テストは**偽の outcome**
  を渡すので、`status` が**どこから来たか**を判別できなかった。実物を通す E2E
  (`test_ac14a_already_decided_status_comes_from_the_row` — reject 後に retry すると
  `status="rejected"` が返る) を足して KILLED。
- **T6-M3 (bless の案内文から次の 1 手を落とす)**: `assert "approval retry" in err2` は
  **元の例外文言にも同じ語が入っている**ので判別力ゼロだった (`resolve it first
  (reconcile or approval retry)`)。**T6 で足した案内文そのもの**
  (`収束手順: サービスの対話シェルで \`approval list\``) を assert する形に直して KILLED。

### 5. barrier 競合テストの安定性

**同一プロセスの別スレッド + 別 sqlite コネクションで競合を作れる** (flock は ofd 単位
なので、別 `open()` した fd どうしは同一プロセスでも本当に待ち合う)。subprocess は不要だった。
seam は **`_plugin_lock` の呼び出しを monkeypatch** し、**取得の直前**で競合者を走らせて
`threading.Event.wait(timeout=30)` で完了を待つ形。**timeout 付きなので deadlock しても
30 秒で fail する** (ハングしない)。

**`test_approval_retry_message_unaffected_by_later_deployment` (AC-14b) は barrier を
使っていない** — outcome は戻り値なので、**後続の配備が報告に影響し得ない**ことが構造的に
保証される。決定論的な fake (FS を進めてから outcome を返す) で v1.1 案 (lock 外の読み直し)
を殺す形にした。以下の 10 回連続実行はこの 1 本を**同じ束で回した**もの:

```
4 passed in 4.20s / 4.15 / 4.17 / 4.16 / 4.14 / 4.15 / 4.14 / 4.16 / 4.17 / 4.14
→ 10/10 green、所要時間のばらつきも 0.06 秒以内 (flaky ではない)
```

### 6. プラン本文からの機械抽出と再検証

プラン本文の ```` ```<lang> path=<相対パス> ```` ブロックを正規表現で抽出し、
**repo の原本から作り直した別の隔離環境**に再展開して同じテストを回した。

```
抽出したコードブロック: 9
[patched] src/agentic_fx/backtest/cli.py / plugin/switch.py / commands.py
[patched] tests/test_commands.py / backtest/test_cli.py /
          plugin/test_indicator_initial_set.py / plugin/test_reconcile.py
[verified-subset] src/agentic_fx/plugin/switch.py   (本文中の python ブロックが
                                                     diff 適用後の本文に逐語で含まれる)
[written] tests/plugin/test_switch_ops_hardening.py
304 passed in 86.37s
SAME  overlay/agentic_fx/plugin/switch.py / commands.py / backtest/cli.py
SAME  tests/plugin/test_switch_ops_hardening.py / tests/test_commands.py /
      tests/backtest/test_cli.py / tests/plugin/test_reconcile.py /
      tests/plugin/test_indicator_initial_set.py
→ 抽出 → 再展開 → 再検証: 差分ゼロ
```

## 未実測の申告 (着手前検証はここに集中させること)

### v1.3 (T11 / T12) の未実測 — **新しい着手前検証はここへ**

- **T11-a1**: **`test_ac16c1_*` の 3 parametrize を含めた収集数**。フルスイートの増分を予測で
  書いていない (Step 11-e)。**着手前に 4304 の再確認から始めること**。
- **T11-a2 (T11-M9)**: `bool()` を落とす変異が**等価か**。`sqlite3` が `switch_required` を
  `int` で返す以上 `1 != True` / `0 != False` はどちらも `False` なので等価と予想しているが、
  **実走していない**。等価なら「殺せない変異」として記録する。
- **T11-a3 (T11-M2)**: 「`test_ac16c3_*` だけが殺す」という主張。
  `tests/plugin/test_switch_paths.py:677 / :1119 / :1160` が行数や `op_id` を pin していれば
  同時に red になる。**`tests/plugin/` を丸ごと回して全数を記録すること** (Step 11-e の副作用確認)。
- **T11-a4 (AC-16c-5)**: `_plugin_locks` が `_plugin_lock` を**モジュール大域経由で呼んでいるか**
  (= T10 の spy 手法がそのまま効くか) を確認していない。効かなければ観測点を変える
  (**テストを緩めずに指揮者へ申告**)。また spy は `_stopped_*` の中の bless でも発火するので、
  **retry の呼び出し中に取れた観測だけを見る**組み立てにすること (Step 11-b の 5)。
- **T11-a5**: `_stopped_before` / `_fail_advance_at` を probe から転写したとき、
  既存ファイルの `_stopped_at_switched` (`:64`) と**役割が重なるかどうか**を見ていない。
  重なるなら既存に寄せる (重複ヘルパを増やさない)。
- **T11-a6 (AC-16c-6)**: 「live symlink を消して同名 plain dir を置く」構成で本当に
  `old_kind == "plain"` 分岐に落ちるか (`:1692-1700` の `live.exists()` 判定) は**未実走**。
- **T12-a1**: CLI の `plugin retire` を **`main([...])` から叩く既存テストが 1 本も無い**
  (`grep -rn "_plugin_retire\|\"retire\"" tests/` = 0 件) ので、
  **argparse の subcommand 名と引数の形を現物 (`cli.py:772` 付近) で確認してから書くこと**。

### v1.4 (T13 / T14 と T11 の変更分) の未実測

- **T13-a1**: **版 dir を in-place 編集する手順**が実際に効くか。`create_version_dir` は
  dir を `0500`、ファイルを `0400` で作る (`version_store.py`) ので、テストは
  `os.chmod` で書けるようにしてから編集する必要がある。**`_reverify_switched_journal` が
  同じことをしている** (`switch.py:1455-1461`) のでそこを写す。**未実走**。
- **T13-a2**: `switch_required=0` を作る最短手順 (同一候補の再 approve) で、
  **`_advance_to_decided` に本当に到達するか** (0d-2a や `already_decided` で早期 return しないか)。
  設計書 AC-16b が同じ状態を既に踏んでいるので流用できるはずだが**未確認**。
- **T13-a3**: `bless_candidate` の `False` 経路 (AC-19d) が、**`_run_full_gate` を通さずに**
  組み立てられるか。bless は gate を回すので、テストは既存の
  `tests/plugin/test_switch_paths.py` の bless fixture (gate を fake する形) を写すこと。**未実走**。
- **T13-a4**: T13-M9 (`return True` → `return False`) が「多数 red」で済むか、
  **収集エラーなど別の壊れ方をしないか**。
- **T13-a5 / T11-a9 (v1.4)**: **`switch_required=0` の行を 3 phase で止める手順**が実際に効くか。
  `preparing` / `versioned` は `_fail_advance_at`、`recorded` は `_finalize_decision` の `_boom` という
  **2 つの機構を使い分ける**必要がある (上記 Step 11-a の注)。**未実走** — ここを取り違えると
  テストが恒真になるので、**着手前検証で 3 通りとも「狙った phase で止まっている」ことを
  `_rows(conn)` で確認してから assert を書くこと**。
- **T14-a1**: T2-01 / T2-02 の変異が**本当に SURVIVED か** (トリアージはコード読解と grep による
  全数確認で、**変異は打っていない**)。**T14 の Step 14-c で 3 件とも実走して確認すること**。
  もし既存テストが既に殺しているなら、トリアージの「未 pin」判定が誤っていたことになるので**申告する**。
- **T11-a7 (v1.4)**: 3 つ組の述語で **`test_ac16c8_old_target_drift_is_detected` の状態
  (`old_kind='symlink'` の行 + 第三者が第 3 の先へ張り替え)** が組み立てられるか。
  既存の `_write_candidate_v` (`tests/plugin/test_switch_paths.py:490` 付近、内容を変えた 2 つ目の候補を作る)
  を使うと 2 本目の版を作れる。**未実走**。
- **T11-a8 (v1.4。v1.8a 訂正: 繰り上げるのは `switch_required` の 1 行だけ、`new_target` は
  既に plain 分岐の前にあり不動)**: `switch_required` を plain 分岐より前へ**繰り上げても
  挙動が変わらない**こと (設計上は plain 分岐で未使用なので不変のはず)。**フルスイートで確認**。

### v1.0〜v1.2 の申告 (既存)

1. **`docs/` の改訂 (T8) は一切実測していない。** runbook と 8 月設計書の文面は
   設計書 §7.2 の表に従うだけで、動く対象が無い。**文面の整合は人間が読む**こと。
2. ~~**フルスイート (`uv run pytest -q` 全体) は repo 上で回していない。**~~
   → **解消 (v1.1、着手前検証)**。repo の worktree でフルスイートを 2 回実測した
   (下記「指揮者側の着手前検証」§3)。起草者が見た「974 passed / **1 failed**」の
   `test_gate_pytest_worker_argv_pins_cacheprovider_and_rootdir` は **repo 上では再現しない**
   (baseline `4224 passed, 17 deselected` / 全適用後も同数)。T8 でのフルスイートは引き続き必須。
3. **`tests/test_service_app.py` の reconcile 配線テストは隔離環境で緑だったが、
   `service.py` 自体には 1 行も触っていない**ので、起動時の実挙動 (実 service の起動 →
   reconcile → approved_plugins) は未実測。
4. **複数プロセス (別 `python` プロセス) での競合は未実測。** barrier テストは同一
   プロセスの別スレッドで組んだ。
   → **判断済 (v1.1、着手前検証): 追加しない**。理由は下記「指揮者側の着手前検証」§5。
   **ただし忠実度の差は残る**ので、そのことを明記した上での見送り。
5. **`approval list` の表示を実際の端末で見ていない。** 桁揃え・折り返しは未評価
   (テストは文字列の包含だけを見ている)。
6. **性能は測っていない。** `_revert_under_lock` が増やす `journal_store.get` は
   非終端行 1 本あたり 1 回だが、起動時 reconcile の行数が多い環境での実測は無い。
7. ~~**task ごとの red は 1 つを除いて未実走。**~~
   → **解消 (v1.1、着手前検証)**。T1 → T7 を 1 task ずつ worktree に当て、全 task の red /
   green / その時点の既存テスト群を実測した (下記 §2)。`(予測)` は全て実測の逐語に置換済。
   **そのうえで、task 分割そのものに欠陥が 2 件見つかった** — (a) T3 / T4 の中間段が green に
   ならない (付録 A の戻り値 8 hunk と新規テスト 4 本を T3 へ移す訂正を入れた)、
   (b) Step 3-c の「`test_runbook_post_gate_failure_...` が T3 で red」は誤り (T5 の材料)。
8. ~~**逆変異は 25 件中 18 件が実走。**~~
   → **解消 (v1.1、着手前検証)**。残り 7 件 (T1-M3 / T2-M4 / T3-M4 / T3-M5 / T4-M5 /
   T5-M5 / T6-M4) を全適用後の worktree で実走し **7/7 KILLED**、かつ**予測どおりの理由で**
   死んだ (下記 §4)。T5-M5 は本文に置換前/置換後が無かったので、適用可能な形に具体化した。
9. ~~**`force_revert_op_id` の「分類の一致を求めない」判断は、実際にそれで困る場面を
   作って確かめていない**~~ → **解消 (v1.1、着手前検証)**。`tmp_path` の probe を 3 本作って
   実測した (下記 §6)。**結論: Critical ではない。設計変更は不要**。ただし「第三者が張り替えた
   live を force_revert が消す / 上書きする」ことは**実測した事実**なので、設計書に記録する
   価値はある (本束の非スコープ R4 = CLI / シェル入口が無いので、現状は到達経路が無い)。

## spec と食い違った点 (実装して分かったこと)

| # | 設計書の記述 | 実測 | 処置 |
|---|---|---|---|
| 1 | §7.1「書き換える既存テストは **3 本**」 | **4 本**。`tests/plugin/test_reconcile.py::test_reconcile_resolution_holds_the_dependency_locks` が `acquired == sorted({...})` で lock 取得列を完全一致 assert しており、pin 破れの巻き戻しに lock が 1 本増えると red になる | 本プランは **4 本**で進める。→ **設計書 v1.5 で反映済** (§1 / §3.5 / §7.1 / AC-15 を 4 本へ訂正し、§7.1 の「不変」表からも外した) |
| 2 | AC-14d「AST で bare `return` / `return None` が無いこと」 | `approve_candidate` には**入れ子関数** `_close_own_unfinished_journal_if_any` があり、その bare `return` まで拾ってしまう | **入れ子関数の中には入らない**走査 (`_walk_own_body`) にした。→ **設計書 v1.5 で反映済** (AC-14d に但し書きを追加) |
| 3 | AC-5「`resolve()` 化の変異が dangling のケースで red」 | dangling でも `Path.resolve()` は非 strict で同じ文字列を返すので **red にならない**。`./` 付きの綴りも `as_posix()` が畳む | **絶対パスで張った symlink** で分岐が割れることを実測し、その形の単体テストにした。→ **設計書 v1.5 で反映済** (AC-5 を書き換え、dangling で red にならない理由を明記) |

## 指揮者側の着手前検証の記録 (2026-09-19、worktree `tmp/wt/soh-preflight` / ブランチ `soh-preflight`)

起草者の「未実測の申告」の **2・4・7・8・9** を対象に、**repo の worktree** (隔離 overlay では
なく実ファイル) で実測した。本体 root の `src/` `tests/` は 1 行も触っていない。

### 1. 材料の作り方 (機械抽出のみ)

プラン本文の ```` ```<lang> path=<相対パス> ```` ブロックを正規表現で抽出し、
**付録 A は hunk index で、付録 C は AST で test 関数ごとに** task へ割った
(割り当ては付録 A / C の表を正とする)。付録 B / D は 1 hunk に T5 と T7 が同居していたので、
「完成形から T7 部分を落とした中間形」を作って `diff -u` で 2 本に割った
(**B-1→B-2 / D-1→D-2 の再合成が v1.0 の付録とバイト一致**することを確認済)。

### 2. task 分割の検証 (申告 7)

| task | (i) テストだけ置いた red | (ii) 実装後の green | (iii) その時点の既存テスト群 |
|---|---|---|---|
| T1 | `2 failed in 0.51s` (`AttributeError: ... has no attribute 'classify_live'`) | `2 passed in 0.47s` | `914 passed, 1 deselected` |
| T2 | `2 failed, 3 passed in 5.42s` (`Failed: DID NOT RAISE ValueError`) | `5 passed in 5.37s` | `917 passed, 1 deselected` |
| T3 | `4 failed, 5 passed in 10.37s` | **`3 failed, 6 passed`** ← **分割の欠陥** | **`3 failed, 918 passed`** |
| T4 | `8 failed, 26 passed in 18.12s` | `4 failed, 30 passed` / 付録 G 適用後 `3 failed, 31 passed` | **`3 failed, 924 passed`** |
| T5 | `18 failed, 82 passed in 66.95s` | `100 passed in 68.16s` | `941 passed, 1 deselected` |
| T6 | `2 failed, 83 passed in 58.69s` | `85 passed in 58.46s` | `942 passed, 1 deselected` |
| T7 | `5 failed, 63 passed in 2.79s` | `68 passed in 2.69s` | `947 passed, 1 deselected` |

(iii) の対象は `tests/plugin tests/test_commands.py tests/backtest/test_cli.py tests/test_service_app.py`
(フルスイートと同じ 2 件を `--deselect`)。**T3 / T4 の 3 failed は常に同じ 3 本**
(`test_ac1` / `test_ac3` / `test_ac8` の `outcome.*` の assert) で、**既存テストは 1 本も壊れていない**。

**分割の欠陥と訂正**は付録 A の節に書いた。**訂正後の分割でも T3 以降を実測し直した**
(ブランチ `soh-t3split`):

| task (訂正後) | red | green | その時点の既存テスト群 |
|---|---|---|---|
| T3 (案 C + outcome 戻り値、テスト 13 本) | `8 failed, 5 passed in 14.12s` | `13 passed in 14.28s` | **`925 passed, 0 failed`** |
| T4 (lock + 再読、テスト 19 本に完成) | `5 failed, 14 passed in 21.58s` | `19 passed` → 付録 G 適用後 `38 passed` | **`931 passed, 0 failed`** |
| T5 (`commands.py` のみ) | `11 failed, 70 passed in 45.92s` | `81 passed in 47.16s` | **`941 passed, 0 failed`** |

**訂正後は全中間段で「既存テストが 1 本も壊れない」かつ「その task の red が全て green になる」**
が成立する。T6 / T7 は v1.0 の分割のままで問題なし (上表で実測済)。

**機械 diff**: 全 task 適用後の worktree の `switch.py` / `commands.py` / `backtest/cli.py` と、
5 つのテストファイルが、起草者の隔離環境の成果物と**バイト一致** (DIFF-ZERO)。
= **hunk 単位の分割は情報を落としていない**。

### 3. フルスイート (申告 2)

```
baseline (984547e、未適用) : 4224 passed, 17 deselected, 482 warnings in 589.47s (0:09:49)
全 task 適用後              : 4259 passed, 17 deselected, 482 warnings in 612.06s (0:10:12)
```

**差は +35 = 新規テストの本数と一致** (`test_switch_ops_hardening.py` 19 + `test_commands.py` 15 +
`test_cli.py` 1)。**failed は baseline / 適用後とも 0 件**。

`--deselect tests/test_shell_interrupt.py --deselect tests/test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end` 付き。
**起草者が見た 1 failed (`test_gate_pytest_worker_argv_pins_cacheprovider_and_rootdir`) は
repo 上では再現しない** — baseline が `0 failed` なので、隔離環境の cwd 依存で確定。

### 4. ★未実測 7 件の逆変異 (申告 8)

全 task 適用後の commit の上に注入 → 対象テストのみ実行 → `git checkout -- <file>` で復元
(毎回 `git status --porcelain` が空であることと `__pycache__` の削除を確認)。

| # | 結果 | red の理由 (逐語) |
|---|---|---|
| T1-M3 | **KILLED** | `AssertionError: old_kind=absent かつ live 不在の switched 行が収束せず非終端のまま残った` / `assert 'switched' == 'reverted'` |
| T2-M4 | **KILLED** | `Failed: DID NOT RAISE ValueError` |
| T3-M4 | **KILLED** | `sqlite3.IntegrityError: UNIQUE constraint failed: plugin_switch_journal.name` (**プランの予測どおり**) |
| T3-M5 | **KILLED** | `assert ('deployed_after_rollback' == 'foreign_waiting')` |
| T4-M5 | **KILLED** | `assert [] == ['sma']` (lock を 1 本も取らない) |
| T5-M5 | **KILLED** | `AssertionError: 後続配備の target を報告してはならない` |
| T6-M4 | **KILLED** | `assert 0 == 1` |

**7/7 KILLED、かつ全件が予測どおりの理由で死んだ** (等価変異・判別力不足は 0 件)。
T5-M5 は本文が「受け取らず lock 外で読み直す (v1.1 の案)」という**説明だけ**で
適用可能な形になっていなかったので、置換前/置換後を具体化して表に書き戻した。

### 5. 実プロセス競合を足すか (申告 4)

**見送り。** 理由:

- **flock が別プロセス間でブロックすること**は既存の `tests/plugin/test_flock_multiprocess.py::
  test_reject_waits_for_approve_flock_and_then_gets_already_decided` が
  **同じ `_plugin_lock` に対して**実プロセスで pin 済み。`_revert_under_lock` はその
  `_plugin_lock` をそのまま使うので、この性質は継承される。
- **stale 行の再取得が別コネクションの commit を見ること**は、AC-9b-i が
  `db_store.connect` で**別の sqlite コネクション**を開いた競合者で既に実測している
  (プロセス境界ではなくコネクション境界が効く性質)。
- 足りないのは**両者の合成** (「実際に lock 待ちでブロックしている間に別プロセスが commit する」)
  だが、これを組むには親が lock を握ったまま子に reconcile を走らせ、握ったまま DB を進めて
  から解放する **3 点同期**が要り、`_flock_worker.py` に新しい role を足す必要がある。
  multiprocess テストは既知の flaky 源でもある。
- **忠実度の差は残る**ことを明記しておく: AC-9b の seam は `_plugin_lock` の**取得直前**で
  競合者を走らせて終了まで待つ形なので、**flock の待ち合わせ自体は起きていない**。

### 6. `force_revert_op_id` が「分類の一致を求めない」ことの実害 (申告 9)

`tmp_path` の probe 3 本 (実 DB / 実 `plugins/` には触っていない)。

| probe | 仕込み | 実測 |
|---|---|---|
| D-a | `old_kind='absent'` の `switched` 行 + live が**第三者の symlink** | force_revert が **`live.unlink()` して第三者の symlink を消す** (`plugins/sma` が消える)。行は `reverted` |
| D-b | `old_kind='symlink'` の `switched` 行 + live が**第三者の symlink** | force_revert が **`old_target` で上書き**する (第三者の指す先が失われる)。行は `reverted` |
| D-c | lock 待ちの間に競合者が同じ行を `decided` に畳んで live を `new_target` へ進めた | **巻き戻さない** (`switch_reconcile_skipped_stale_row`)。live は `new_target` のまま、行は `decided` のまま |

**結論: Critical ではない。設計変更は不要。**

- **危険な競合 (D-c) は `expect_class` ではなく「行の再取得」が止めている** — `force_revert` 経路でも
  この保護は効く。§3.3.2 が `expect_class=None` でも行の再取得だけは必ず行う設計にしているのが正しい。
- D-a / D-b は**本束が作った挙動ではない** — 旧実装も force_revert 分岐では分類を見ずに
  `_revert_one` を直接呼んでいた。T4 は保護を**足している**だけで、減らしていない。
- **到達経路が無い**: `force_revert_op_id` は設計書 §1 の非スコープ R4 で CLI / シェル入口を持たず、
  現状はプログラムからしか呼べない。
- とはいえ「phase に依らず巻き戻す割込」という意味論の**帰結**として
  「第三者が張り替えた live も巻き戻し対象になる」ことは実測事実なので、
  設計書 §3.3.2 の `expect_class=None` の説明に 1 行添えておく価値はある (指揮者判断)。

### 7. 変更後の再確認

- プラン本文からの機械抽出 → task 順に全適用 → worktree の最終状態と **DIFF-ZERO**
  (`switch.py` / `commands.py` / `backtest/cli.py` + テスト 5 ファイル)。
- 付録 B-1 → B-2 / D-1 → D-2 の再合成が v1.0 の付録 B / D と**バイト一致**。

### 8. 逸脱の全件申告

1. **T3 / T4 の中間段で green が取れなかった** — プランの Step どおりに進めると止まる。
   訂正 (戻り値 8 hunk + 新規テスト 4 本を T3 へ) をプランに書き、訂正版を別ブランチ
   `soh-t3split` で実測して green を確認した。**worktree の `soh-preflight` ブランチ自体は
   v1.0 の分割のまま**進めてある (最終状態は同じ = DIFF-ZERO)。
2. **付録 F の 1 本目を T5 の材料として扱った** (プランは T3 と書いていた)。T3 で red に
   ならないことを実測した結果の判断。プラン本文も訂正済。
3. **付録 B / D を task 別に割った** — v1.0 のままでは T5 / T7 の分離が物理的に不可能なため。
   再合成のバイト一致で担保した。
4. **`patch -p0` に対象ファイルを明示して当てた** — 付録 D / E / F / G の diff ヘッダが
   絶対パスなので、`patch` が「potentially dangerous file name」として拒否する。
   **実装者も同じ問題に当たる**ので、付録の見出しに対象パスがあることを前提に
   `patch -p0 <target> < <diff>` の形で当てること (プラン本文は変更していない)。
5. **`patch` が `src/agentic_fx/plugin/switch.py.orig` を残し、1 度 commit に混入した** —
   worktree 内で削除済。実装時は `--no-backup-if-mismatch` を付けること。
6. **(iii) の既存テスト群にフルスイートと同じ 2 件の `--deselect` を付けた** (baseline と
   件数を比較可能にするため)。
7. **probe 用の一時テストファイル** (`tests/plugin/test_probe_d.py`) を worktree に置いて
   実行し、削除した。commit には入っていない。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-20 | v1.4b | 設計書 v1.8b に追随する docs 是正 (T11 `4df5207` / T13 `cd792f5` 実装時の実測): Step 13-a 行 5/6 (AC-19d/AC-19e) に訂正注記を追加 — CLI 経由の bless は `RuntimeError` が `_plugin_bless` の except を素通りし **traceback が出る** (rc=1 化は ticket `[cli-bless-runtime-error-traceback]` へ切り出し) / 版 dir を直さない `approval retry` は `still_pending(reverify_failed)` にならず `record_version` 自身が `HistoryGitError` を送出して残余行が非終端のまま残る (実装は `test_ac19e_..._when_repaired` / `..._unrepaired_...` の 2 本に分割) / Step 11-e の T11-M11/M12 の killer を実測 (`test_ac16c8_*` のみ / `test_ac16c6_*` のみ) に訂正し、T11-M6 の等価判定を「T11 の再開判定経路に限る」形で明記 (T11-M6-scope 追加)、T11-M9 の等価判定を実測で確認 / hunk 1 のコメント例に「record_version は無条件で毎回照合する」旨を追記。**設計判断・実装は 1 つも変えない** (docs 訂正のみ) | 実装プラン T11/T13 の担当による着手前検証・実測報告 (worktree `tmp/wt/soh` HEAD `cd792f5` で全項目を現物照合、M11/M12/M6/M9 は `-k ac16c`/`-k "ac16c or ac9d"` を実走) | — |
| 2026-09-20 | v1.4a | 設計書 v1.8a (codex sol r6 C0/I3/M1) に追随する docs 是正: **#2 (Important)** Step 11-a の周辺・§3.7.2 相当の記述には変更なし (プラン側はそもそも自動収束の対称性を主張していなかったため直接の修正箇所は無い、設計書側のみ是正) / **#1 (Important、AC 網羅性)** Step 13-a のテスト一覧に **# 6 `test_ac19e_bless_residual_resolved_by_approval_retry` (AC-19e)** を追加し、bless の残余行が `approval retry` でも解除できることを明記。Step 13-d の変異表末尾に AC-19e が既存コード (T3/T5) の契約に依存し T13 専用の killer を持たないことの注記 / **#3 (Important、AC 網羅性)** Step 11-b のテスト 3 (`test_ac16c3_*`) を **`switch_required` の両方 (0 と 1) × 3 phase = 6 parametrize** に拡張し、`switch_required=0` 側の止め方 (`_finalize_decision` を `_boom`) を明記 / **#4 (Minor、file:line 誤citation)** Step 11-c hunk 2 と Step 13-b hunk 1、T11-a8 の `:1717`→`:1700`・`:1335`→`:1337` を訂正し、**繰り上げ対象は `switch_required` の 1 行だけ** (`new_target` は既に plain 分岐の前 `:1700` にあり不動、codex sol r6 D) に書き換え。タイトルを設計書 v1.8a 準拠に更新 | codex sol 設計レビュー r6 (`tmp/review-20260920-soh/r2/design-r6/codex-out.md`、一次トリアージ `codex-triage.md` = 確定 2 / 蒸し返し 1 (AC 網羅性は新規) / 要確認 1)。設計書 v1.8a の変更に追随 | — |
| 2026-09-20 | v1.4 | 設計書 v1.8 (**R13 / R14** と codex sol r5 の確定 4 件) に追随: **T11 を R13 に合わせて書き換え** (判定述語を 3 つ組 `(switch_required, old_kind, old_target)` へ / **hunk を 3 → 2 に統合** — `new_target` と `switch_required` の 2 行を live の読み取り直後へ繰り上げ、判定を plain 分岐の**前**に 1 箇所だけ置く / テストを 6 → 8 本へ (**AC-16c-2 / -3 / -6 を 3 phase parametrize**、**AC-16c-7 = rollback commit と新行作成の間のクラッシュ**、**AC-16c-8 = `old_target` drift = R13 の主 killer**) / 変異表に **3 つ組のうち 1 要素だけ比べる退化 ×3 (T11-M10/M11/M12)** を追加、T11-M7 を「判定を plain 分岐の後ろへ動かす」へ差し替え)。**T13 を新設** (R14 — 版 dir の hash 再照合を切替の要否にかかわらず `_finalize_decision` の直前で通す。`switch_required=1` は現行どおり `RuntimeError`、`=0` は `_advance_to_decided` が `False` → 呼び出し元が行を閉じて `still_pending(reverify_failed)`。テスト 5 本 + 逆変異 9 件)。**T14 を新設** (テストのみ。2 周目ローカル LLM の確定 2 件 = `still_pending` の `outcome.status` 2 箇所と `snapshot_invalid` 枝の journal クローズ。**行番号ではなく関数名・分岐名で特定**する書き方に)。**`switch_required=0` の行は `_fail_advance_at("switched")` では止まらない** (`advance(switched)` を呼ばないため) ので、AC-16c-2 だけ `_finalize_decision` の `_boom` で止める注を Step 11-a に明記。Goal (10)(11) / Global Constraints / File Structure / 受入条件表 / task 依存図 (T11 → T13 → T14 の直列) / T8 の確認項目と runbook 追記 / 「未実測の申告」に v1.4 の 8 件を同時に更新 | codex sol 設計レビュー r5 (`tmp/review-20260920-soh/r2/design/codex-out.md` / `codex-triage.md`、C0 / I5) の 確定 4 件 + 2026-09-20 ユーザー裁定 2 点、および 2 周目ローカル LLM (`tmp/review-20260920-soh/r2/local/triage.md` の T2-01 / T2-02) | — |
| 2026-09-20 | v1.3 | 設計書 v1.7 (ユーザー裁定 **R11 / R12**) に追随: **T11 (再開時に `switch_required` が保存値と食い違ったら巻き戻して新しい `op_id` で流し直す。`switch.py` の 3 hunk + 新規テスト 6 本 + 逆変異 10 件)** と **T12 (`afx plugin retire` の `UnresolvedJournalError` にも「次の 1 手」。`cli.py` の 1 hunk + テスト 1 本 + 逆変異 4 件)** を新設。Goal (8)(9) / Global Constraints (`except` は 3 つ・T11/T12 も既存テストを書き換えない) / File Structure / 受入条件表 (AC-16c-1〜6 / AC-18) / task 依存図 (T11 は T10 の後、T12 は T6 の後、T11 ∥ T12) / T8 の確認項目とフルスイート基準を同時に更新。**T11 の red の種は指揮者側の probe (`tmp/review-20260920-soh/r2/cr1-probe/test_probe_cr1.py`) のヘルパと 3 parametrize を転写し、assert を目標状態へ反転して作る** (probe をそのまま置かない — 実装後に probe 側が red になるため)。**T11-M2 (「常に巻き戻す」への退化) を殺すのは `test_ac16c3_*`** であることを変異表に明記 (「だけ」は未実測なので Step 11-e に副作用の全数確認を置いた)。**「未実測の申告」に T11/T12 の 7 件を追加**。**既存 `_rows` が `(op_id, phase)` の 2 タプルで probe の 3 タプル版と食い違う**ことを Step 11-a に明記し、`switch_required` / `old_target` は `journal_store.get` で読む形を指定した (既存 `_rows` を書き換えると T1〜T10 のテストが全滅する) | 2 周目 `/code-review high` (`tmp/review-20260920-soh/r2/code-review.md`) の #1 (probe で再現、Important) と #5 に対する 2026-09-20 ユーザー裁定 (採用 = 案 3、是正範囲 = #1 と #5 のみ) | — |
| 2026-09-20 | v1.2a | 1 周目 codex (terra/medium) の指摘を反映: **T9 Step 9-c と T10 のフルスイート基準値が 4274 / 4278 で混在していたのを訂正** — 正は **T9 着手前 4278 passed → T9 後 4297 (+19) → T10 後 4300 (+3)**、実測 `4300 passed, 17 deselected` (2026-09-20、HEAD `c1abf8d`)。段 0 完了時 4274 passed から T9/T10 着手前の 4278 passed への +4 は、未 pin 4 群を pin した `6e1d431` / `0e079ee` / `3952962` / `5cee9f5` (2026-09-20) による | 2026-09-20 [switch-ops-hardening] 1 周目 codex (terra/medium)、`tmp/review-20260920-soh/r1/codex-triage.md` | — |
| 2026-09-19 | v1.0 | 初版 (T1〜T8、逆変異 12 件 + task ごとの表、隔離環境での実測記録つき) | 設計書 v1.4 の承認を受けた実装プラン化 | — |
| 2026-09-20 | v1.2 | 設計書 v1.6 (ユーザー裁定 R9 / R10) に追随: **T9 (シェルの `approve <id>` も lock 内 outcome を文言に写す、`commands.py` のみ、逆変異 8 件)** と **T10 (巻き戻しの commit が lock 内で完了していることを別コネクションから pin、テストのみ、逆変異 3 件 = 段 0 の S0-54 / S0-55 / S0-75)** を新設。File Structure / Global Constraints (書き換え 0 本の宣言) / 受入条件表 / task 依存図 (T9 は T5・T7 と直列、T10 は T9 と並列) / T8 の確認項目を同時に更新。**T10 は本体コードを 1 行も変えない** — 現実装が既に lock 内 commit であることを設計書 §3.3.2 の全数表が記録している | `tmp/review-20260920-soh/stage0.md` §3 の未 pin 3 件と §6 の設計判断 2 点に対する 2026-09-20 ユーザー裁定 | — |
| 2026-09-19 | v1.1 | 指揮者側の着手前検証 (専用 worktree `soh-preflight` で T1→T7 を 1 task ずつ実走) を反映: **全 task の `(予測)` red を実測の逐語へ置換** / **★未実測 7 件の逆変異を実走し全 KILLED** / **T3・T4 の task 分割が成立しないことを実測**し、付録 A の hunk 10〜12・16〜20 と新規テスト 4 本を T3 へ移す訂正を追加 (訂正後の中間段は `925 passed, 0 failed` を実測) / Step 3-c の「`test_runbook_post_gate_failure_...` が T3 で red」を訂正 (T5 の材料へ) / **付録 B・D を task 別に分割** (B-1/B-2・D-1/D-2) / 付録 A の hunk → task 表を新設 / **訂正を Step 3-a・3-b・4-a・5-a・5-b・5-c と T3 / T5 の見出し・受入条件表・task 依存図に落とした** (Step だけを追う実装者が v1.0 の分割に戻らないように) / **付録 D・E・F・G の diff ヘッダを相対パスへ統一** (絶対パスだと `patch` が "potentially dangerous file name" として拒否する) / フルスイート実測 (baseline `4224 passed` / 全適用後も同数) / `force_revert_op_id` と実プロセス競合の判断を追記 | 起草者が「未実測の申告」に挙げた 2・4・7・8・9 の解消。設計書は v1.5 へ (食い違い 3 件を全件プラン側の実測で採用) | — |
