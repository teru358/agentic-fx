# 履歴の修復のあとに、配備中の strategy を再評価する

`afx history repair-time` などで履歴が修復されると、それより前の履歴で評価・承認された配備中の strategy は、承認の根拠 (in_sample / holdout) が古いままになります。この手順は、今の履歴で評価し直し、配備を続けるか止めるかを人間が決める運用です。実行は daemon を動かしている repository root で行います。daemon は動かしたままでよく、評価の間も配備は変わりません。

再評価は改善の試行には数えません。評価の規則 (期間・収益性の床・gate) は変わらず、修復前の記録 (`backtest_runs` と承認済みの行) は書き換えません。indicator は対象外です。

## 1. 依頼を作る

```bash
afx plugin reevaluate --all-deployed
```

配備中の strategy を名前順に評価し、承認の依頼を 1 本ずつ作ります。1 本につき数分かかります。確認の問いに答えるか、`--yes` を付けます。1 本だけ評価するなら `afx plugin reevaluate <name>` です。

## 2. 結果表を読む

1 行は `名前  状態  #承認 id  (理由)  説明` です。

| 状態 | 意味 | することは |
|---|---|---|
| `created` | 承認の依頼を作った | 3. へ |
| `existing` | 同じ評価の依頼が既にある (2 回目の実行など) | 3. へ。評価はやり直していない |
| `unevaluable` | 評価できず、依頼は作っていない (理由は括弧の中) | 5. へ |
| `failed` | 評価が失敗した。他の strategy は止まらない | もう一度実行する。続くときは `afx ctl log` を読む |
| `changed_during_evaluation` | 評価中に配備・履歴が変わったため何も書いていない | もう一度実行する |
| `skipped` | 配備中の strategy でない (indicator、旧式の plugin、承認の無い entry など) | `--all-deployed` では何もしなくてよい |

終了コードは、`created` と `existing` 以外が 1 本でもあれば非 0 です。`--all-deployed` では、配備中の strategy でないことによる `skipped` (`not_strategy` `plain_plugin` `not_approved` `not_deployed`) は数えません。名前で指したときは数えます。

## 3. 承認の中身を見る

```bash
afx ctl approval list
afx ctl approval show <id>
```

見出しは `再評価 (前回の承認 #<id>)` です。次を確かめます。

- コード変更はありません (配備中の版そのものの評価です)。
- pair ごとの in_sample / holdout が、前回 → 今回で並びます (pf / trades / avg_r / max_drawdown)。前回の値には出所が付き、承認時の記録、`run #<id>`、不明のどれかです。不明は、前回の値を特定できなかったことを表します。
- 評価時点は、前回の承認日時と今回の評価時点が利用者の表示 tz で出ます。期間が違う比較なので、値の差はそのまま優劣ではありません。

web の承認ページと Discord の承認カードにも同じ内容が出ます。

## 4. 承認か却下か

- **承認** (`afx ctl approve <id>`): 承認の根拠だけを新しい行に差し替えます。配備 (symlink・版・journal) は変わりません。同名の別候補の承認待ちには影響しません。
- **却下** (`afx ctl reject <id>`): **その strategy の配備を停止します**。`afx plugin disable` と同じ処理で、新しい signal と OPEN が止まり、待機中の signal は取り下げられ、symlink が外れ、`plugin_disabled` が通知されます。shadow の観測と open position の保護 (SL・決済) は続き、強制決済はしません。確認の文にも同じことが出ます。

却下の結果は次のどれかで返ります。

| 結果の文言 | 意味 | することは |
|---|---|---|
| 却下し、配備を停止しました | 完了 | なし |
| 却下しました。取引は止まりましたが symlink を外せていません | 外す操作が失敗 | `afx plugin disable <name>` で完了する |
| 却下しました。取引は止まりましたが plugins/<name> が想定と違うため外していません | live が評価した版と違う | `plugins/<name>` の状態を確認する |
| 依頼が古いため無効にしました。配備は変えていません | 依頼のあとに切替・取消・停止などがあった | 再評価をやり直す。配備を止めるなら `afx plugin disable <name>` |
| 履歴が変わったため決定していません | 依頼のあとに履歴が修復された | 再評価をやり直す。配備を止めるなら `afx plugin disable <name>` |

未完の切替 journal や配備操作があると、却下は止まって pending のままです。その場合も、配備を止めるなら `afx plugin disable <name>` を使います。

## 5. `unevaluable` の行

評価できない版 (データセット未設定、参照履歴の不足、取引数の不足など) には依頼を作りません。古い根拠のまま配備が続くので、行に並ぶ `止めるには: afx plugin disable <name>` で止めるかどうかを人間が決めます。取引数の不足のように評価の行が残るものは、履歴が増えてからもう一度実行します。この扱いは、実際に評価不能が出たときに裁定します。

## 6. 再評価のやり直し

依頼は、履歴の epoch・最新の決定・配備世代が評価時と同じ間だけ有効です。変わった依頼は承認も却下も「古い」として無効になるので、もう一度 `afx plugin reevaluate --all-deployed` を実行します。変わっていない依頼は `existing` と表示され、評価はやり直しません。

## 7. 履歴の穴埋め (通常の import) のあとに再評価する

time repair だけでなく、評価用履歴が古い・欠けているときも、この手順で再評価します。新しい holdout 評価と採用は、評価用履歴の鮮度と充足を満たさないと止まります (改善の in-sample は続きます)。既存の配備と資金保護は、この検査で自動停止しません。

1. `afx status` の `evaluation history` で、主 pair の最新確定足・充足率・60 分を超える日別欠損を確かめます。取得も修復もしないので、stale / gap と出たときだけ、daemon を止めて開場中に `afx history import --source mt5` で補います (ready のときは案内は出ません)。閉場中に bridge が 503 を返したら、開場後に再試行するか、期間を分けて取り、取得後にもう一度 `afx status` で日別の充足を確かめます。最新足が更新されただけで完了にしません。
2. broker の休場で足が無い日だと確認できたときだけ、`config/history-no-bars.yaml` に 1 系列・1 日の項目を足します (例は `config/history-no-bars.yaml.example`。実ファイルは gitignore 対象で、`reason` `approved_by` `approved_at` は確認した人が書く)。履歴の行は消さず補間もしません。足したら `afx status` の `no-bars applied` で、検査の対象範囲に当たる項目と digest を確かめます。壊れた一覧は空として続けず、検査が判定不能になります。
3. 再評価は status の充足から推測しません。履歴を補ったあと、承認や gate が出した具体的な結果 (`history_not_ready` や下の表の `pending` / `invalidated` など) が再評価を求めているときに `afx plugin reevaluate --all-deployed --yes` を実行します。通常の import でも `history_epoch` は変わらないので、穴埋め後は新しい依頼が作られます。

### guard の規則版と `existing`

依頼の重複判定 (`existing`) は、次のすべてが成り立つときだけです。既存の鍵 (コード・artifact・epoch・基準決定・配備世代) が同じ / 現在の guard 規則版・暦版・閾値が同じ / 依頼の証跡に保存した dataset・pair・評価区間で見た現在の無足日一覧が同じ / 正しい `ready` の証跡がある。規則版は完全一致で、互換扱いはありません。条件を満たさない pending は再利用されず、新しい評価に成功すると旧依頼が `reevaluation_superseded` で無効になります (整理のための却下はしません。却下は配備停止です)。評価が履歴不足で作れなかったときは、旧依頼は pending のまま残ります。穴埋め後にもう一度実行します。

### 履歴証跡で止まった承認 (`history_not_ready`)

提出時の履歴証跡を承認時に照合します。止まった承認は、固定の reason と次の操作が shell・`afx ctl`・web・承認カードに出ます。

| 状況 | status | reason | 次の操作 |
|---|---|---|---|
| 通常の依頼の証跡が合わない | `invalidated` | `history_evidence_invalidated` | 候補が残っていれば `plugins/_human/<name>` に保存し (旧依頼の hash と照合し、別の候補で上書きしない)、`afx status` で履歴と無足日一覧を確かめ、`afx plugin submit <name> --from _human` で新しく評価して承認する。候補が消えていれば復元か再作成が要る。元の mission との結び付きは戻らない |
| 再評価の依頼の証跡が合わない | `pending` | `history_evidence_refresh_required` | 履歴条件を確かめ、`afx plugin reevaluate --all-deployed --yes` を再実行する。却下しない |
| 一覧・設定の読取失敗などで照合できない | `pending` | `history_check_unavailable` | `afx status` で読取エラーを確かめ、直してから承認を再試行する (通常の依頼もこの場合は失効しない) |

journal が始まっている承認の再開には、この照合を掛けません。再評価の却下にも掛けません。

### rollback

履歴証跡の無い strategy の版へは rollback できません (`history_not_ready` / `history_evidence_required`)。証跡は、戻り先の名前と完全長 artifact hash に一致する最新の承認済み payload のものだけを使います。人間が確認した上でこの拒否だけを迂回するときは、ローカルで次を実行します。完全長 hash と理由が要り、既定の戻り先や短縮 hash では強行できません。実行者・時刻・理由が activity に監査記録として残り、記録できなければ切替えません。hash・依存・live の状態など他の検証は迂回しません。

```bash
afx plugin rollback <name> --to <完全長の artifact hash> --force-history-guard --reason "<人間が記入する理由>"
```

強行は適正な証跡を作らず、監査完了にも数えません。

## 8. 移行 manifest (承認 15 件の追跡表)

`data/history-guard-migration/manifest-2026-11-11.json` (gitignore 対象、DB のバックアップとともに人間が保全する) に、旧承認 1 件 1 行で計 15 行を持ちます。書き手は運用担当者で、gate や worker は書きません。

- 作成: `agentic_fx.backtest.history_migration.initial_manifest(...)` に、旧承認 id ごとの完全長 hash と依存 pin (旧承認の payload から写す) を渡す。配備中 8 行は `1a / pending`、非配備 7 行は `1b / deferred`。
- 更新: 再評価の結果ごとに、同じ版の旧承認 2 行へ同じ新依頼 id (`evaluated`) か同じ固定の評価不能理由 (`unevaluable`) と、評価試行の時刻・根拠参照を書く。人間が決めたら `human_decision` と `decided_at` を書く。
- 検証: `validate_manifest(manifest)` が形と受入条件 (15 行の一致、配備中 8 行の結果、同じ版 2 行の一致、非配備 7 行が段 1b 未完了のまま) を、`verify_against_db(manifest, conn, no_bars_path=...)` が DB との一致 (新依頼がその版自身の再評価で、有効な証跡があること、決定の一致) を確かめます。
- `unevaluable` は再評価の成功ではありません。配備中 4 版の評価・人間の決定が残っていれば、その残作業を最終報告に書きます。

### 非配備 7 版の評価 (段 1b) と manifest 版 2

非配備 7 版は、配備せず現在の条件で各 1 回評価し、人間専用の report と manifest に記録します。承認・決定・配備・rollback の可否は変えません (新しい approval も作りません)。

- 実行: 運用担当者が DB と manifest を backup してから、版ごとに `agentic_fx.plugin.history_audit.audit_non_deployed_version` を直列に呼ぶ (改善 loop・agent・daemon のジョブにはしない)。入力は manifest の旧承認 id・name・完全長 hash・dependency_pins。実行前に、DB の旧承認行・`.versions` の実物・現在 live の依存 pin を照合する。
- 評価: 保存版を lock の下で symlink を追わずに検査して専用領域へ複製し、共有 gate を `floor_mode=warn` / 比較なしで 1 回通す。依存は現在の承認済み inventory で `pin_mode="require"` 解決し、保存 pin と完全一致しなければ評価不能 (`dependency_changed`)。
- report: `data/history-guard-migration/reports/<試行 id>.json` に 1 試行 1 ファイル。実行完了 / 床合否 / 履歴検証 / 未実行 scope の 4 軸と総合結果 (`pass` / `fail` / `unevaluable`) を持つ。warn の完走は合格ではない。成績は DB の成績表に書かない。
- manifest: 運用担当者が backup 後に `convert_to_v2` で版 2 へ変換し、7 行の `result_status` / `audit_ref` / `evaluated_at` / `evidence_ref` / (評価不能なら) `unevaluable_reason` を書く。版 1 は従来どおり受理し、厳格化は版 2 だけ。検証は `validate_manifest` と `verify_against_db(manifest, conn, no_bars_path=..., report_root=...)`。
- rollback: 失効済みの 4 版 (#21・#23・#24・#25) は、評価が pass でも記録のみで、`--force-history-guard` でも戻せません。残る 3 版 (#39・#71・#72) で必要なときは、report を確認したうえで上の強行操作を使う (理由に report 参照を書く)。通常 rollback を可能にする接続は需要が出るまで延期しています。
- `unevaluable` と `fail` は再評価成功ではありません。評価不能が残れば「15 件すべて再評価成功」と報告せず、対象 id と対処を残作業に書きます。

