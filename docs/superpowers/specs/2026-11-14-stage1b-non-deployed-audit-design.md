# 設計 C2 — 段 1b: 非配備 7 版を現在の条件で再評価する (方式 D)

- 改訂: 2026-11-14。C2。2026-11-14 利用者承認 (J0 = 7 版すべて、J1 = 方式 D)。task 1.1〜1.3 を実装中、task 1.4 (7 版の実行) は実行前に利用者が改めて確認する。
- 調査基点: `a58dc4bbe5110453e806e875c351f0f4cecc0f36`。コードは変更していない。参照する段 1a 設計は `docs/superpowers/specs/2026-11-11-backtest-history-freshness-guard-design.md`。
- 以下の report 形式、manifest v2、gate の記録先切替は後続実装の仕様であり、現行機能ではない。設計対象は評価・記録だけ。秘密情報、口座値、個人設定値を本文にも出力仕様にも含めない。

## 1. 動作目線

### 1.1 目的と対象

履歴不足の期間に通った旧 strategy 承認 **15 件すべてを再評価する**裁定を維持する。段 1a は初回棚卸し時の配備中 4 版・旧承認 8 件を追跡済み。段 1b は残る非配備 7 版を、配備せず、各 1 回 `floor_mode=warn / compare_mode=off` で評価して人間専用 report と manifest に記録する。

これは**現在の条件での再評価**である。当時の core・履歴・設定による承認の再現ではない。strategy は当該 artifact の 3 ファイルをそのまま使い、依存は現在の承認済み inventory と保存 pin の完全一致を必須にする。期間・履歴・評価規則は現在の共有 gate に従う。

C1 調査で本番 DB に `mode=ro` で接続し、manifest 全 15 行の旧 id / name / 両 hash と段 1a の新 id・決定を照合した。現行 v1 の `validate_manifest` と `verify_against_db` は問題 0 件。段 1b の最新決定 (同 name + content)、保存版の存在と両 hash、現在 live への依存 content pin 一致も確認した。

| 旧承認 | 名前 | artifact 表示用先頭 | 最新決定 | rollback の既存承認検査 |
|---|---|---|---|---|
| #21 | sma_cross_10_30 | 395e2ce2d065 | rejected #26 (取消し) | 拒否。history force でも迂回不可 |
| #23 | bb_ema_breakout | eb0bd5257957 | rejected #90 (再評価却下) | 同上 |
| #24 | rsi_bb_reversion_200 | dc59a62892ca | rejected #93 (再評価却下) | 同上 |
| #25 | rsi6_bb200_rr2 | 8ad4942e2d29 | rejected #27 (取消し) | 同上 |
| #39 | asym_shock_breakout | 1483bc7e63ea | approved #39 | 承認検査は通る。履歴拒否あり |
| #71 | close_pressure_followthrough | 12d14addcf7c | approved #71 | 同上 |
| #72 | close_pressure_followthrough | a74732d7d316 | approved #72 | 同上 |

7 版の両 hash は旧承認と一致、依存は各 2 本・延べ 14 本が現在 live の content と一致した。承認済み inventory での依存承認の有効性は r1 の実測報告にもあるが、C1 では resolver の実行までは行っていない。実行直前に必ず再照合する。AST、保存 test の pytest、max_bars 契約、sandbox 実行、現在履歴での床合否と history_guard は未確認。

表の短縮 hash は表示専用。入力は manifest / DB の完全長 hash と旧 id に固定する。同じ名前の #71・#72 をまとめない。初回の `deployed_at_inventory` と phase は後日の配備停止で移し替えない。段 1a の #122〜#125 の承認と、後日の取消し #126・#127 は別の決定として保持する。

### 1.2 利用者が読む結果

結果には次の 4 軸を別々に示す。**warn の完走は合格ではない。**

| 軸 | 記録する内容 |
|---|---|
| 実行完了 | 未着手 / 実行中 / 完了 / 中断。到達段階も記録。正常に検査を終えた不合格と、例外中断を区別 |
| 床合否 | in_sample / holdout ごとに合格 / 不合格 / 判定不能 / 未実行。取得できた成績と共有判定理由を併記 |
| 履歴検証 | ready・証跡検証済み / 不合格 (固定理由) / 未実行 / 検証不能。epoch と証跡の有無を別記 |
| 未実行 scope | pair ごとの in_sample / holdout、および実行前検査のうち未実行のものと理由。欠測を 0 にしない |

`result_status=evaluated` は、検査で不合格が確定した場合を含む「評価結果を得た」の意味で、合格や承認ではない。report の総合結果は次のように定義する。

- `pass`: 事前検査合格、gate が evaluable、必要な全 pair / scope が実行済み、現在の床規則で両 scope が合格、ready 証跡と epoch の照合に成功した場合だけ。
- `fail`: 保存 test の正常実行による不合格、または評価可能な gate の床不合格。保存 test で止まれば床と履歴は未実行とする。
- `unevaluable`: 標本・履歴・依存・実行環境の不足、中断、整合性変化等で評価を確定できない場合。部分成績や既知の床不合格は残すが、必要 scope が欠ければ総合結果を fail/pass に押し上げない。

warn は in_sample 床不合格だけを理由に holdout を省略しない。履歴検査・標本不足等で止まる場合は holdout 未実行とする。床の総合理由だけで両 scope を推定せず、測定済みの各 scope に同じ共有床判定を適用して記録する。二度目の backtest は行わない。

最終報告は、旧承認 **15 行の追跡記録**と版単位の結果を分ける。段 1b の 7 試行の実行完了数、pass / fail / unevaluable 数、未実行 scope、未解決の旧 id を示す。段 1a の 8 行は 4 評価への参照なので 8 回の実行と数えない。評価不能は再評価成功に数えず、残れば「15 件すべて再評価成功」と報告しない。床不合格も「合格数」に含めない。

### 1.3 承認・rollback と確定裁定

**J0: 失効済み 4 版を含め 7 版すべて評価する。J1: 方式 D。** 成績は人間専用 report と manifest に残す。旧 approval の status / payload、同名現配備、live symlink、deploy op / journal を変えず、新 approval も作らない。評価結果で既存の失効決定を復活させない。

本件は rollback の可否を変えない。特に失効済み #21・#23・#24・#25 は評価結果が pass でも記録のみであり、既存 `--force-history-guard` でも戻せない。必要な再配備は通常の submit と承認の経路で扱う。

残る 3 版について rollback が必要なら、人間が report の対象版・条件・結果を確認し、既存のローカル CLI の `--to <完全長artifact hash> --force-history-guard --reason <理由>` を使う。理由に report 参照と判断理由を記入する。force は履歴拒否だけを迂回し、hash・依存・承認・kind 等の検査は維持される。activity の必須監査記録と DB の force_token 関連付けも既存のまま。report が force を自動承認することはなく、force 自体は評価成功や適正証跡の生成に数えない。本作業で rollback は実行しない。

新テーブル、basis、根拠接続 CLI、rollback 改修は作らない。段 1a 設計 §8.4 の「後続実装で通常 rollback を可能にする」は**需要が出るまで延期**する。該当行と周辺の「監査まで拒否」の表現を更新する task を §6 に置く。本 C1 作成では元文書を編集しない。

## 2. 仕組み

### 2.1 方式選択と再利用の境界

| 方式 | 評価と判断 |
|---|---|
| A: 非配備専用 approval | 新しい承認・決定経路が必要。既存 submit 由来の承認は配備を伴うため、そのまま流用できない。採らない |
| B: 独立監査 + rollback basis | 最大 3 版の force 省略のために DB / 接続 / rollback を増やす。需要が出るまで延期 |
| C: 一時配備 / 現版で再 submit | 配備しない要件や対象版の同一性を満たさない。採らない |
| **D: report + manifest + 必要時は既存 force** | **利用者裁定で採用**。15 件の監査を満たし、既存の承認検査を保つ |

現コードで使う部品は次のとおり。行番号より symbol を参照の正とする。

| 部品 | C1 での用途・制約 |
|---|---|
| `plugin/version_store.py` / `core/plugin_files.py` の hash 関数、`switch.hashes_of` | artifact=3 本、content=plugin.py + config.yaml の既存定義を再利用 |
| `plugin/gate_pytest.py::check_candidate_snapshot` | 3 通常ファイル、symlink / hardlink / 特殊ファイル / サイズ等の既存検査を再利用 |
| `plugin/loader.py::_discover_one` (基点 578 行) | 保存版 metadata を読む。C0 の 618 行表記を訂正 |
| `tools/plugin_loader.py::approved_plugins`、`plugin/resolve.py::resolve_indicator_deps` | 現在の inventory と `pin_mode="require"`。歴史的依存探索をしない |
| `plugin/switch.py::_run_full_gate` | snapshot → AST → loader / 契約 → 隔離 pytest → hash → 共通 gate をそのまま使う。配備部分の抽出は不要 |
| `plugin/approval.py::run_kind_gate` → `strategy_gate.evaluate_strategy_adoption_gate` | warn / compare off。比較 off は現在の既定だが、接続 test で明示保証する |
| `backtest/history_evidence.py` | ready 証跡の生成・検証、epoch は呼出し側で照合 |
| `backtest/history_migration.py` | v1 保持と最小 v2 分岐、report 照合を追加。現行 v1 は 1b=deferred 固定 |

### 2.2 実行前照合と入力

運用担当者は既存の DB / manifest バックアップの識別子を確認し、7 版について次の 3 点を照合してから直列実行する。

1. **承認行**: 旧 id / name / 両 hash / dependency_pins を DB と manifest で照合し、同 name + content の最新決定を別に記録する。失効済みというだけでは評価を省略しない。
2. **`.versions` の実物**: 正規の name と完全長 hash から対象を限定し、lock 下で正規版ディレクトリを確認。コピー前に、コピー元の必須 3 ファイルをそれぞれ symlink を追わずに検査し、通常ファイル、`st_nlink == 1`、既存のファイルサイズ上限内であることを必須にする。欠損・symlink・hardlink・特殊ファイル・サイズ超過はコピー前に拒否する。検査済みの元から専用一時ディレクトリへ安全に bytes コピーし、コピー先にも既存 `check_candidate_snapshot` を適用、`hashes_of` の両 hash を旧承認 / manifest の完全長 hash と照合する。hardlink は使わない。保存ファイル・設定・test を変更せず、`lock_config` で pin を付け直さない。
3. **現在 live の pin**: 現在の `approved_plugins` から resolver を require で通し、config と旧 payload / manifest の alias・plugin・content pin・解決 params / 参照指定を照合する。欠損や変化は評価不能。現在版で代用せず、過去 indicator artifact を探索しない。

コピー元の事前検査からコピー先検査・hash 照合までを既存 plugin lock 下で行う。パスの親も含め symlink を辿らず正規ディレクトリを dirfd で固定し、全 3 ファイルを `stat(..., follow_symlinks=False)` 相当で検査してから開く。dirfd 相対の `O_NOFOLLOW | O_NONBLOCK` 付き open と `fstat` で通常ファイル・リンク数・サイズ・事前検査との device / inode 一致を再確認し、検査した同じ fd から上限付きで読む。検査後にパスを通常 open し直す方式は使わず、特殊ファイルへの差替えでも読取り前に拒否する。コピー先は専用領域に排他的に新規作成し、元の読取後属性・読取量も照合する。途中の差替え・サイズ変化・不一致はコピーを破棄して評価不能にする。サイズ上限と hash 定義は既存部品を参照し、コピー先の既存検査を置き換えない。

コピー取得と終端照合だけ既存 plugin lock 規約を使い、評価中は持たない。取得済みコピーは試行終了まで保持する。GC root を増やさない。現行 `gc_roots` は status=approved の旧行自体を root にするため、後続 rejected 行があっても旧行を保持する本設計では対象版が root に残ることを test で確認する。実物が消えていれば評価不能であり、report は GC の新しい根拠にならない。

各試行の aware UTC `eval_now` を開始時に固定して gate へ渡し、開始・終了時刻、core commit、dataset 識別子、pair / timeframe / 区間、epoch、規則・暦版、評価条件の識別 digest を記録する。7 試行は本番履歴を段 1a と同じ共有 gate 契約で読む。7 版全体が同一 DB snapshot だったとは主張しない。履歴 DB のコピー作成、独自 inventory root、過去の core / broker 環境復元は行わない。

評価条件 digest は評価に必要な非秘密の契約項目から作り、全 settings の複製・hash 化をしない。report に個人設定値や口座値を保存しない。後日の完全な数値再現を保証する履歴 snapshot 製品化は範囲外とする。

### 2.3 副作用なしで共有 gate を使う

`_run_full_gate` 自体は approval・staging・archive・symlink を変更しない。一方、現行は**失敗時の gate 行を DB 保存する副作用**があり、enforce の床拒否では activity も書く。従って現関数を無変更で呼んで「副作用なし」とはしない。

後続実装では失敗時記録の小さな差込口を足す。外側の既存呼出し API では sink の省略を許し、省略時は従来の `_persist_human_gate_rows` を使う既定 DB sink に解決する。監査入口は DB 保存をしない収集 sink を明示し、`activity=None` とする。内部へ進む前に必ず sink を決定し、以後の全失敗・例外分岐ではその sink だけを使う。監査入口での sink 未指定や収集失敗を既定 DB sink へ fallback させない。成功は戻り値の `outcome.gate_rows`、失敗・想定外例外は例外前に収集した部分行を同じ report 出力側へ渡す。全失敗分岐に適用し、例外文字列の部分一致で結果を分類しない。構造化された判別子・停止段階を収集し、得られない検証情報は「未確認」とする。`GateOutcome` の既存戻り tuple は増やさず、必要な診断は任意の収集先に渡す。

**成績は backtest_runs に書かず report のみに持つ。** 新しい mission_outcome を設けず、`reevaluation` 行の追加も不要。in_sample_view と baseline_monitor の読取条件を変更しない。内部の記録処理は解決済み sink を全経路で必須とする。これは既存呼出し元への明示引数の必須化ではなく、監査経路からの暗黙の DB 保存を防ぐ境界である。report の run 識別子は試行内の local row id で、DB run id は null。DB run があるようには表示しない。

warn の床判定を採用可へ読み替えない。収集済み結果に既存の共有床判定を適用して scope 別の合否を得る。ready 証跡は既存生成・検証関数を使い、終了時の epoch・規則・暦・対象範囲の無足一覧照合が失敗したら総合 pass を公開しない。

### 2.4 report と運用

保存先は人間専用の `data/history-guard-migration/reports/<試行id>.json` とする (後続実装案)。同 directory は既存 migration directory と同様に gitignore 対象、agent 入力・agent の変更可能領域に入れない。専用の一回限りの人間用実行入口から起動し、改善 loop / agent mission / daemon のジョブにはしない。新たな恒久サービスは作らない。

report は 1 試行 1 ファイル、同 id を上書きしない。必要項目は report 形式版、試行 id、旧承認 id、name、完全長両 hash、依存 alias / plugin / content hash、入力照合結果、試行時点の最新決定、§2.2 の評価識別情報、§1.2 の 4 軸と総合結果・固定理由、local row id、許可された成績、ready 証跡または欠落理由である。

生の gate 行・例外・settings・payload を丸ごと dump しない。成績は PF・平均 R・件数等の判断に必要な項目と区間に限定し、初期残高・口座情報・個人設定・任意の自由記述は出力しない。証跡は既存の人間用構造を利用し、秘密等を含めない。guard 不合格で ready 証跡が生成されなければ、固定理由と未生成を記録する。

report は一時ファイルへの書出しと flush / fsync 後の公開で完成させ、manifest より先に確定する。完成済みファイルは再実行で変更しない。manifest は運用担当者が backup 後に更新し、gate / worker は書かない。再試行は新 id として旧 report を保持する。中断を自動回収する DB 状態機械や冪等鍵は設けない。

daemon は段 1a と同様に稼働したままでよい。7 版を直列、評価中の plugin lock / 長時間の書込 transaction はなし、lock 待ちは有限とする。既存 worker / pytest sandbox と資源制限を維持し、起動できなければ評価不能。終了・例外時は子を終了・回収し一時領域を片付ける。DB busy や実行環境異常で止め、無限再試行しない。専用 cgroup / quota / inode 下限 / 親 DataFrame 上限の新設を必須条件にしない。ホストの負荷安全性を測定済みとはしない。

終了時に旧行 identity、コピーの hash、依存の現在 live / 承認、最新決定、history_epoch と証跡の現方針を再照合する。開始から変化していれば `changed_during_evaluation` として部分結果を保持し、採用可能な pass としない。失効済みのまま変化していない版は通常どおり評価結果を記録できる。通常 import の追記だけを破壊的変更扱いせず、段 1a の epoch / 証跡検証と同程度にする。不変 snapshot の保証は追加しない。

### 2.5 manifest の最小 v2

DB schema は変更しない。**互換の範囲は、現在の manifest だけでなく従来受理された v1 全体とする。** `schema_version=1` は従来の `validate_manifest` と `verify_against_db` の判定を維持し、新たな必須欄・時刻形式・状態制約・report 検証を課さない。既存 v1 の受理範囲を暗黙に狭めると、保存済み記録の読取りや既存運用が版変更なしで失敗するためである。厳格化は v2 分岐に限定し、v1 に新条件を混ぜない。

明示的な backup・変換で v2 を作る。変換後は v2 の制約を満たす必要があり、v1 として受理されても必要情報が不足すれば変換を止めて不足を報告する。時刻・根拠を捏造したり、v1 を自動書換えしたりしない。トップレベル項目は維持し、**各 entry に `audit_ref` (相対 report path または null) だけを追加**する。成績・判定の詳細を二重管理する欄や basis id は追加しない。

| 対象 | v2 制約 |
|---|---|
| 全 15 行 | 旧 id 集合、name、両 hash、dependency_pins、初回配備事実、phase を維持。未知欄を拒否 |
| 段 1a の 8 行 | `audit_ref=null`。既存の結果、新 id、人間決定、時刻、参照をそのまま保持し、従来の同版 2 行の一致と DB 照合を継続 |
| 段 1b の 7 行 | 新 approval id は常に null。`human_decision=not_applicable / decided_at=null` (失効済み 4 版だけでなく全 7 版。方式 D は新承認判断を作らない) |
| deferred / pending | 未着手 / 試行中。`audit_ref / evidence_ref / evaluated_at / unevaluable_reason` は null。pending は再評価成功ではない |
| evaluated | report の総合結果 pass または fail。`audit_ref`、aware UTC 試行時刻、`evidence_ref` 必須、unevaluable_reason=null |
| unevaluable | report の総合結果 unevaluable。固定理由・試行時刻・audit_ref・evidence_ref 必須。部分成績があっても成功ではない |

`evidence_ref` は report 内の結果・根拠への参照とし、ready 証跡を意味するとは限らない。例えば report の `#result` を指し、pass ではそこから ready 証跡を検証できるようにする。v2 の形の検査で evaluated / unevaluable の時刻と参照を **phase によらず**要求する。v1 の時刻・参照の検査は従来どおりに保つ。現行 v1 は段 1b の evaluated / unevaluable 自体を受理せず、時刻は timezone 付きであれば非 UTC も受理するため、これらを一般的な v1 互換と混同しない。v2 の aware UTC、未着手欄の null、report 整合等は v2 の契約である。

`validate_manifest` / `verify_against_db` は版別に分岐し、v1 は従来の検査・DB 照合を維持する (report root 入力を既存 v1 呼出しに必須化しない)。v2 の `verify_against_db` は段 1b を新 approval のない行として素通りせず、旧 payload と pin、report の対象 id / hash / pin、時刻・結果・理由を照合する。report root の明示入力を追加し、audit_ref の root 外・symlink・別版参照・欠落・不正 JSON を拒否する。pass なら全 scope、床、ready 証跡・epoch も確認する。DB run id は要求しない。

後日 epoch / 方針 / 依存 / 最新決定が変わっても歴史 report は削除しない。検証は「実行時の記録整合」と「現在の条件で証跡が有効か」を分け、後者を再評価済みの事実や rollback 許可に混同しない。既存段 1a の証跡検証は緩めない。

## 3. 失敗時

| 事象 | report / manifest と次の動作 |
|---|---|
| 保存版欠損・hash 不一致・snapshot 不正 | `artifact_missing / hash_mismatch / invalid_snapshot`、unevaluable。別版 fallback をしない |
| 現 inventory の依存欠損・pin 不一致・承認変化 | `dependency_changed`、unevaluable。resolver の固定詳細理由も記録。過去依存探索はしない |
| 現 loader / AST / max_bars に不適合 | `unsupported_version`、unevaluable。監査中に版を直さない |
| 保存 test の正常な不合格 | evaluated / fail、停止段階 pytest、床・履歴・gate scope は未実行 |
| in_sample 床不合格 | warn で履歴検査・holdout へ進み、scope 別結果を残す。後段が評価不能なら総合 unevaluable |
| holdout 床不合格 | evaluable かつ必要 scope 完了なら evaluated / fail。新たな却下行は作らない |
| history stale / gap / unavailable、標本不足、参照不足、履歴源未設定 | 既存の固定理由で unevaluable。未実行 scope と部分成績を記録。穴を自動で休場認定しない |
| sandbox 起動不可、timeout、worker crash、資源不足、DB busy | `sandbox_unavailable / timeout / worker_failed / resource_unavailable / db_busy`。unevaluable、終了・回収後に次の試行を判断 |
| 評価中の identity / pin / 最新決定 / epoch / 方針変化 | `changed_during_evaluation`。部分結果を残すが pass を出さない |
| 想定外の失敗 | 固定理由 `failed` と段階、unevaluable。未確認の原因を断定しない |
| 中断 | 可能なら `interrupted` の report を確定。SIGKILL 等で書けなければ manifest は pending / deferred のまま。担当者が中断の記録を別 report で確定し unevaluable に更新。未実行範囲を記録 |
| report 出力失敗 | 完成 report がないため manifest を evaluated にしない。書出し失敗を実行完了とは別に報告。再試行は新 id |
| manifest 更新失敗・参照不一致 | 完成 report は保持し、担当者が修復・再検証する。評価を自動でやり直さず、検証完了まで追跡未完了 |

固定理由の enum は既存履歴理由に表の到達可能な理由を足すだけとする。`ambiguous_dependency`、`dependency_pin_missing` 等の歴史復元専用語は導入しない。未知の理由は成功扱いせず検証エラーにする。評価不能は残作業として対象 id と対処を最終報告する。


### 3.x 受け入れた残余: worker の状態の偽装

監査は、gate の worker が専用の pipe に書く「初期化の成功」と「pytest の終了コード」を使い、初期化が成功して pytest が test の失敗を報告したときだけ fail とする。この pipe は候補の test からも書けるので、候補は自分を評価不能から fail に変えることができる。偽装で変えられるのはこの向きだけで、pass には pytest の成功と床の判定の両方が要るので、pass への道は開かない。版が自分を不利にするだけで、承認・rollback の安全に影響しないため、送信元の分離はしない。pytest 中の改変は、終了コードと独立の hash_changed で評価不能を優先する。

## 4. Test と受入

以下は後続実装に求める検証であり、本 C2 で実施したという意味ではない。

| test | 必須の確認 |
|---|---|
| 入力同一性 | 同名 live が別版でも指定 artifact の保存 test を使う。hash / pin / 旧 payload / manifest の不一致は拒否 |
| コピー元の検査と安全コピー | 各必須ファイルを順に symlink (正しい bytes を指す場合も含む)・hardlink (同じ bytes)・FIFO / 特殊ファイル・上限超過・欠損へ置換した fixture を、lock 下かつコピー前に拒否。FIFO で block せず、gate を起動しない。検査後の差替え・サイズ変化も検出し、検査した fd 以外から読まない。正常な 3 通常ファイルは安全にコピーでき、コピー先の `check_candidate_snapshot` と両 hash 照合を必ず実行。コピー先破損・hash 不一致も拒否し、失敗時の領域を片付ける |
| 現在 inventory | require で成功する例、live 更新・依存失効で dependency_changed となる例。歴史探索や pin 自動更新は起きない |
| gate 接続 | 共有 `_run_full_gate` を warn / compare off で 1 回だけ実行。同条件の共有 gate と成績・床理由が一致。in_sample 床不合格でも holdout 成績を残す |
| 判定の分離 | warn 完走かつ床不合格を pass にしない。履歴不合格で holdout 未実行、標本不足・中断を unevaluable、pytest 不合格を fail と記録。両 scope の不合格をそれぞれ残す |
| 証跡 | pass は全 pair / scope と ready 証跡検証・epoch 一致が必須。不足・無足一覧 / 方針 / epoch 変化を検出し、未検証を verified と表示しない |
| 記録先と副作用 | 成功・各失敗・想定外例外で backtest_runs と approval 行を追加しない。live / staging / archive / deploy op / journal / signal を変更しない。既存 gate 呼出しは sink 省略時に従来どおり失敗行を DB 保存。監査入口は明示した非 DB sink に成功・部分行・全失敗・例外診断を集め、未指定・sink 失敗時にも DB fallback がないことを確認 |
| 読み手への非混入 | report 作成前後で in_sample_view と baseline_monitor の採用窓が不変。新 mission_outcome がないことも確認 |
| 並行運用・隔離 | fixture で daemon 相当の writer と共存、評価中 lock 不保持、有限 lock 待ち、依存/決定変化の終端検出。既存 worker / pytest sandbox と例外時の子回収を確認 |
| report と failure | 許可項目のみ出力し、口座値 / 個人設定値 / 生例外が出ない。部分実行、出力失敗、中断、manifest 更新失敗で半端な pass を公開しない。別 id の再試行を保持 |
| manifest v1 互換 | 従来受理 / 拒否の fixture 群で旧判定と新 v1 分岐の validate / verify 結果を比較。現 manifest だけでなく、非 UTC の aware 時刻、従来検査対象外の pending / deferred の時刻・根拠欄等も含め受理範囲を維持。従来の不正 id / hash / 状態 / DB 不一致は引き続き拒否。既存呼出しに report root 不要 |
| manifest v2 と変換 | 明示変換、段 1a 不変、15 id 固定。v1 では受理される非 UTC 時刻・未着手欄の非 null を v2 では拒否。v2 の evaluated / unevaluable は全 phase で時刻・根拠欠落を拒否。1b の new id=null / decision=not_applicable、report の別版・root 外・symlink・欠落・不正・状態不一致を検査。変換時の情報不足を報告し、v1 を変更せず時刻・根拠を補作しない |
| rollback / GC 回帰 | report の有無で通常 rollback は変化せず、4 失効版は history force でも拒否。他 3 版も force は履歴だけ迂回、監査書込失敗なら拒否。GC root は現行旧 approved 行で保持 |
| 運用結果 | 7 版それぞれ report + manifest 参照、旧 15 行を追跡できる。unevaluable・未実行 scope と残作業を明示し、段 2 / 3 を含むチケット全体の完了とはしない |

関連回帰: `tests/plugin/test_history_reevaluate_real.py`、`test_history_rollback.py`、`test_version_store.py`、`tests/backtest/test_history_migration.py`、`test_history_evidence.py`。隔離・読み手関連も対象とする。本番 DB を test の書込先にしない。

## 5. 変更履歴表

| 版 | 日付 | 変更 | 確認状況 |
|---|---|---|---|
| C0 | 2026-11-14 | 方式 B、歴史依存復元、新 storage / basis、enforce、独自 snapshot と資源要件の初稿 | ソース静的確認。DB 未照合 |
| C1 | 2026-11-14 | J0 / J1 確定、方式 D に縮小。失効 4 版を明記、現 inventory require、warn / compare off、report のみの成績、最小 manifest v2、通常 rollback 接続を延期 | DB 読取専用で 15 行照合、現 manifest validate / verify 問題 0、7 版の両 hash・live pin 14 本一致。評価実行・実装 test・負荷試験は未実施 |
| C2 | 2026-11-14 | terra 閉鎖確認の Important 2 / Minor 1 を採用。コピー元を lock 下で非追従検査して安全コピー、v1 の従来受理範囲を維持して厳格化は v2、既定 DB / 明示非 DB sink の境界を明確化。test 仕様・task・付録を同期 | 文書と関連コードを静的照合。実装・追加 test の実行・7 版の評価・負荷安全性は未確認。C1 の実測を再実施したものではない |

## 6. 短い task 表

| task | 内容 | 完了条件 |
|---|---|---|
| 1 | **非配備 7 版を配備せず現在の条件で各 1 回評価し、旧承認へ結果を記録する** | 下位 task 完了。unevaluable は成功に数えず残作業を列挙 |
| 1.1 | lock 下のコピー元非追従検査と安全な exact 版コピー、現在 inventory require、共有 gate の記録先切替、人間専用 report | 通常ファイル・リンク数・サイズの事前検査、検査済み fd コピー、コピー先既存検査・両 hash 照合の test。warn / compare off、全失敗収集、既存省略時 DB sink / 監査明示非 DB sink・fallback 禁止、DB 成績非保存と副作用・隔離 test |
| 1.2 | manifest 最小 v2、validate / verify の版別対応と report 照合 | 従来受理 v1 全体の互換回帰、明示変換と不足検出。15 行固定、段 1a 保持、v2 の全 phase の時刻・根拠必須と厳格化、report 整合 test |
| 1.3 | 段 1a 設計 §8.4 と §8.1、§8.5、運用手順・チケットを更新 | 「通常 rollback を可能にする後続実装」は需要まで延期、方式 D、失効 4 版は force 不可、manifest v2 の意味と v1 の従来受理維持 / 厳格化は v2 の版別境界を明記。過去裁定を削除せず今回裁定を追記 |
| 1.4 | backup 確認、承認行 status / 保存版 hash / 現 live pin 照合後、7 版を直列実行 | 7 report を確定して担当者が manifest を更新、validate / DB・report 照合、15 行追跡と 4 軸別報告。残る評価不能は未解決として報告 |

コード実装・実行はこの依頼の対象外。basis / rollback 改修を本 task の完了条件にしない。履歴同期 (段 2 / 3)、import 衝突修復、評価窓再設計、設定調整、indicator 自動配備、過去環境再現も範囲外。

## 付録 A. r1 の採否と理由

件数: Critical 1、Important 8、Minor 8 の全 17 指摘を裁定した。採用は指摘の問題認識への採否であり、提案の全機構を追加する意味ではない。

| id | 採否 | 理由・C1 への反映 |
|---|---|---|
| C1 | 採用 | 4 版の失効を DB で再確認。§1.1 / §1.3 に列記し、J0 により全 7 版評価。受益は最大 3 版という前提で D を採用。basis 自体を削除 |
| I1 | 採用 | live pin 14 本一致を確認。現在 inventory + require を使い、不一致なら評価不能。過去依存探索・artifact 選択・独自 inventory root を削除 (§2.2) |
| I2 | 問題認識を採用、記録案を簡素化 | 配備等は呼出し元の責務。既存の失敗 DB 保存まで無副作用とは言えないため、失敗収集の差込口と activity=None で再利用。成功行も report のみ、DB 保存を足さない (§2.3) |
| I3 | 採用 | 新 history_audit は reader に漏れる。提示された report のみの選択肢を採用し、reevaluation を含め新 backtest 行を作らない (§2.3) |
| I4 | 採用・J1 確定 | force の既存監査で足りる。新テーブル・basis・接続・rollback 改修を削除。§8.4 更新を task 1.3 に追加 |
| I5 | 採用 | 段 1a と同じ並行運用・直列・lock 外評価・有限待ち。独自履歴 snapshot と追加資源制度を必須から除外 (§2.4) |
| I6 | 採用 | warn / compare off を各 1 回。完了・床・履歴・未実行を分け、warn を pass としない (§1.2) |
| I7 | 採用 (C2 で互換境界を確定) | report + audit_ref だけの v2、v1 / v2 の validate / verify 分岐。v1 は従来受理範囲を維持、時刻・根拠等の厳格化は v2 に限定する task。全 7 行の decision を not_applicable に固定 (§2.5) |
| I8 | 採用、裁定取得の再要求は不要 | J0 / J1 は利用者が確定済み。J2 は status 保持・必要なら既存 revoke、J3 は段 1a 相当で技術的に決め、追加質問にしない |
| M1 | 採用 | `_discover_one` の参照を symbol と基点 578 行へ訂正 (§2.1) |
| M2 | 採用 | 名称と冒頭を「現在の条件での再評価」に変更。当時の承認再生とは呼ばない |
| M3 | 採用 | GC root 拡張を削除。旧 approved 行が残ることと既存 root を test で確認 (§2.2 / §4) |
| M4 | 採用 | 歴史復元特有の理由語を削除。現在依存の不成立は dependency_changed に統合し resolver の理由を保持 (§3) |
| M5 | 採用 (C2 でコピー元検査を補完) | コピーで元のリンク属性が失われるため、lock 下で元の非追従検査と検査済み fd コピーを追加。コピー先の snapshot 検査・hash 計算は既存部品を名指し再利用 (§2.2 / §4) |
| M6 | 採用 | C1 で DB 読取専用照合と現物 hash・pin 確認を実施。未実施の契約・実行検査は未確認と明記 (§1.1) |
| M7 | 採用 | A は既存 submit 承認が配備を伴うと明記。ただし B の推薦ではなく確定 D との比較に用いる (§2.1) |
| M8 | 採用 | 「保全物確認」を承認行 status・保存版 hash・現 live pin の 3 点と backup 確認へ具体化 (§2.2 / task 1.4) |

## 付録 B. 文書とコードの食い違い 5 件の処置

| r1 の番号 | 食い違い | 処置 |
|---|---|---|
| 1 | 段 1a spec §8.5 は evaluated / unevaluable の時刻・参照必須、実装は deployed のみ | C2 では v1 の従来受理範囲を優先して維持し、phase 非依存の時刻・参照必須は v2 に限定する task 1.2。task 1.3 で元 spec §8.5 にこの版別境界を追記する。現行 manifest の照合は C1 の記録であり、C2 では再確認・コード修正は未実施 |
| 2 | spec §8.1 / §8.4 と過去の「監査まで拒否」が、既に失効済みの 4 版を示していない | C1 に 4 版の決定を明記。task 1.3 で元 spec と運用記録へ今回裁定を追記。7 版評価は維持し、監査後も通常 rollback 拒否、4 版は force 不可と明記 |
| 3 | C0 の `_discover_one:618` は定義位置でない | C1 §2.1 で 578 に訂正。以降は symbol を正にする。コード変更不要 |
| 4 | C0 が `_run_full_gate` に staging / 承認 / archive 変更を帰属 | C1 §2.3 で訂正。残る失敗行 DB 保存を正確に区別し、report 収集へ切替える最小 task を置く |
| 5 | manifest 形式の参照 §8.4 と実際の §8.5 | C0 の訂正を維持。manifest は §8.5、force と後続接続延期の更新先は §8.4 と区別。見出し番号を動かす必要はない |

## 付録 C. terra の C1 閉鎖確認への裁定

対象は `c1-close-terra.md` の Important 2 件 / Minor 1 件。いずれも採用し、設計上の不足を補った。動作の閉鎖確認は後続実装と §4 の test 待ちであり、未確認。

| 指摘 | 裁定と理由 | C2 反映先 |
|---|---|---|
| Important 1: 保存版のコピー元検査 (r1 M5) | 採用。コピー後だけでは元の symlink / hardlink 属性を失い、特殊ファイルを先に読んでしまう。lock 下で元を非追従検査してから検査済み fd でコピーし、コピー先検査と hash 照合も維持 | §2.2、§4 コピー元 test、task 1.1、付録 A M5 |
| Important 2: v1 互換範囲 (r1 I7) | 推奨案を採用。従来受理された v1 全体を従来どおり受理し、版変更なしの互換破壊を避ける。厳格化は v2 に限定、変換時の不足を明示し補作しない | §2.5、§4 v1 / v2 test、task 1.2、付録 A I7 / B 1 |
| Minor 1: sink 必須と既定動作 | 採用。内部で常に sink を解決することと、既存外部呼出しで引数省略を許すことを区別。既存は既定 DB sink、監査入口は明示的非 DB sink とし DB fallback を禁止 | §2.3、§4 記録先 test、task 1.1 |

C1 の DB 照合記録・既閉鎖項目・方式 D の裁定を引き継ぐ。C2 は文書改訂のみで、コード、元の段 1a spec、DB、manifest、保存版は変更していない。保存 test・AST・max_bars・sandbox・現在履歴での評価結果・daemon 並行時の負荷安全性は引き続き未確認。
