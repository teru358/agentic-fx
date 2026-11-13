# backtest 履歴の鮮度と充足の守り — 段1a 設計 C2

対象: `agentic-fx` main `70ac595f`  
日付: 2026-11-11  
文書の予定配置先: `docs/superpowers/specs/2026-11-11-backtest-history-freshness-guard-design.md`  
状態: 設計改訂。コード・ファイルの変更、import、再評価、承認は実行していない。

主pairの評価用履歴が古い、または充足条件を満たさない場合、新しいholdout評価と採用を止める。改善runはin-sampleで続けられる。承認では、提出時に適正な履歴で評価した証跡を照合する。既存配備と資金保護処理は、このguardによって自動停止しない。

C0に対するopus 1周目のCritical 1件・Important 10件・Minor 10件の対応を維持する。C2では、opus閉鎖確認2周目の新Important 5件・Minor 6件と指揮者の裁定を反映する。非配備7版への証跡なしrollbackは段1aで拒否する。

task 1・2の実装が進行中のため、C1からの§2・§3の変更は、§3.3の日別欠損閾値を「60分ぶん超」にする変更だけに限定する。無足一覧の照合範囲と管理方法の補足は§5・§7に定める。

## 0. 確認した事実

パスは、特記しない限り `src/agentic_fx/` 配下。以下はC1の調査時点のコードの事実であり、後述する変更案とは区別する。

| 事実 | 根拠 |
|---|---|
| importは現在、実時計ではなく `cutoff=window_end, grace=0` で確定判定する。naive応答はUTCとみなす。 | `backtest/mt5_import.py:83`, `:198` |
| import本体はnaive・非UTCの入力境界を拒否する。一方、CLIは指定された時差を捨ててUTCを付ける。 | `backtest/mt5_import.py:235`、`backtest/cli.py:73` |
| importは全窓を取得・検証してから書く。不正応答なら書かず、衝突時は既存値を保持して部分commit後に例外を返す。 | `backtest/mt5_import.py:263`, `:296` |
| 共通確定式は `bar_start + width + grace <= cutoff`。graceの既定値は30秒。 | `datafeed/closed_bars.py`、`config.py` |
| holdout境界はハーネス所有。保存済みcoverageのexpectedは休場込みの全格子数で、presentは区間内の行数。 | `backtest/holdout.py:92`, `:145` |
| strategy gateはin-sample、取引数判定、in-sampleの床、holdout、baseline比較の順に進む。 | `plugin/strategy_gate.py:595`以降 |
| `_run_full_gate` は既知の判別子を個別に拒否するが、評価不能全般の既定拒否がない。 | `plugin/switch.py:1352`以降 |
| holdoutの標本不足は、`require_holdout_evaluable=false` なら床の失敗にならない。履歴不足をこの標本不足と混同してはならない。 | `plugin/strategy_gate.py:135` |
| 通常承認はgateを再実行しない。`history_epoch` はtime repairの世代であり、通常importでは変わらない。 | `plugin/switch.py:2067`、`core/history_epoch.py` |
| 再評価承認は根拠の更新、再評価却下は配備停止である。 | `plugin/switch.py:1995`以降 |
| 再評価の既存依頼判定にはguardの版が含まれず、評価前に `existing` を返す。 | `plugin/switch.py:2874`, `:3109` |
| `afx status` はローカルの読取専用接続を使用できる。 | `status.py:339`以降 |
| historyは同値を無変更、異値を非上書きとする。cacheのGCはhistoryを削除しない。 | `store/ohlcv.py` |
| rollbackには `RollbackRefusal(code, reason)` があり、明示対象と既定対象を解決する経路がある。 | `plugin/deploy_rollback.py` |
| 再評価payloadの `reevaluates_approval_id` は旧承認1件を指す。過去承認2件への関連付けは、それだけでは記録できない。 | `plugin/switch.py`、`tmp/design-livehist/r2-opus.md` |

C1ではC0とレビューを全文確認し、主要分岐をコードで確認した。本番DBは読取専用接続で承認15件のpayloadを照合し、対応する `.versions` のディレクトリの存在も確認した。strategyのpendingは0件で、pendingの #78・#115 はindicatorだった。この結果を引き継ぎ、C2で本番DBの再調査はしていない。

履歴の正常範囲と停止再現の数値は、`tmp/design-livehist/r1-opus.md` の実測を採用する。本改訂では履歴全体の再集計はしていない。

- USDJPYの475日で、暦上の有足日に0本の日はなく、日別の最大欠損は2本。
- USDJPYの3か月窓61個は、穴埋め後の最小充足率が0.9998。
- EURUSDも履歴始点を越えた窓では1.0000〜0.9999。
- 09-04の更新停止を再現すると、09-11時点で開場時間差120時間・充足率0.924、11-07時点で1080時間・0.314となる。

閉鎖確認2周目の1m実測も採用する。USDJPY mt5 1mの2026-06-01〜09-02、80開場日は全体充足率0.9984、日別最大欠損15本だった。12本固定では正常範囲の履歴を誤検知するため、日別閾値は足幅から60分ぶんへ換算する。

## 1. 今回実装する範囲

段1aは次の7 taskに限定する。

1. importの安全境界。
2. 共通の履歴検査関数。
3. 共有gateへの検査と既定拒否。
4. 承認証跡・承認結果の表示・再評価の鍵・照合不能pendingの出口。
5. ローカルの `afx status`。
6. 履歴補充と配備中4版の再評価、過去承認8件への関連付けの記録。
7. 新しいrollback操作の証跡照合と、人間専用の明示的な強行操作。

人が無足を認める日の一覧は、検査の例外を人間が管理できるよう、task 2と5に含める。非配備版の監査、ctl・webのstatus拡張、衝突の永続記録などは段1aの受入条件に含めない。

対象はstrategy。indicator等のfixture検証には適用しない。

## 2. importの安全境界

### 2.1 時計・終端・確定判定

`import_mt5` にclockとgraceを渡せるようにする。処理開始時にaware UTC時刻 `T` を一度だけ取得し、取得中に時計が進んでも変更しない。grace `g` はライブと同じ設定を使う。

足幅を `w` とすると、排他的実効終端 `E` は次とする。

```text
E = min(requested_end, floor_grid(T - g - w, w))

保存条件:
  requested_start <= t < E
  t + w + g <= T
```

ローカル・bridge・MT5の時計差に対して、graceとは別に1本分の余裕を取る。これは無制限な時計差への保証ではない。

- 未来の `--to` は `E` に切り詰める。要求終端・実効終端・1本分の余裕を取ったことを人間向け結果に表示する。
- 入力自体の `start < requested_end` と格子整列は従来どおり検証する。
- 切り詰め後に `start >= E` なら、HTTP取得・DB書込みを行わず「確定済み対象なし」、終了コード0を返す。
- 行数上限は要求終端ではなく `E` で計算する。
- 窓ごとの右端ちょうどの行は落とす。中間窓の右端は次窓で取り直し、最終終端はexclusiveを維持する。
- 確定判定には全窓で同じ `T` と `g` を使い、窓終端を実時計の代用にしない。
- 全応答の検証後に書く。不正応答で履歴を部分更新しない。

### 2.2 タイムゾーン

import本体の入力境界は、aware UTC必須という既存契約を維持する。

CLIの `_parse_date` は次に改め、docstring・helpも一致させる。

| 入力 | 扱い |
|---|---|
| 日付のみ、naive ISO日時 | 文書どおりUTCとして解釈 |
| `Z`、`+00:00` の日時 | aware UTCとして受理 |
| 非ゼロの時差付き日時 | 明示的に拒否し、UTC指定を案内 |

`2026-11-04T00:00+09:00` を黙って `00:00Z` に変える挙動を廃止する。

bridge応答のnaive日時は拒否する。aware応答はUTCへ正規化する。同梱bridgeはaware UTCを返すため、この変更と整合する。

### 2.3 衝突・閉場中の運用

異値衝突は上書きせず、既存の停止・部分commit契約を維持する。段1aでは新しい衝突テーブルを作らず、既存例外の衝突内容と部分結果を人間が確認する。

bridgeは閉場中、時刻規則を検証できず503を返す場合がある。これを「対象なし」や成功に変換しない。人間向け案内とrunbookに、開場後の再試行、期間を分けた取得、取得後の日別充足確認を書く。

## 3. 共通の履歴検査

### 3.1 所有者と入力

新設する `backtest/history_health.py` が判定を所有する。

読取部分と純粋な判定部分を分離し、入力はdataset、主pair集合、固定時刻 `T`、grace、暦、無足承認一覧とする。ネットワーク取得や履歴の変更は行わない。

gateでは **`T` をそのgateの `now` と同じ値にする**。検査内で別の壁時計を読まない。

```text
N = holdout._normalize_now(T, base_interval)
B = holdout.in_sample_until(T, holdout_months, base_interval=...)
```

候補・baseline再生も同じ `now`・datasetを使用する。

### 3.2 数え方

主pairごとに次を計算する。

- expectedは `[B,N)` の基底足格子のうち、実時刻で確定済み、FX暦で開場、無足承認の対象外である時刻集合。
- presentはexpected集合に存在する、異なる足時刻数。
- 別source、別interval、格子外、形成中、未来行は数えない。
- 最新確定足も同じdatasetの格子整列・確定条件を満たす行から選ぶ。単純な `MAX(bar_time)` を鮮度判定に使わない。
- 全主pairが合格することを要求する。pair間の平均では判定しない。

最新保存足は診断用に別途表示できるが、最新確定足の代用にはしない。

### 3.3 閾値

閾値は人間所有のcore定数とし、agentが変更できる設定や無効化スイッチを設けない。既存graceは引き続き既存設定を使用する。

| 条件 | 判定 |
|---|---|
| 鮮度 | 最新確定足の終端から `T` までの開場時間が24時間を超えたら `history_stale` |
| 区間充足率 | `market_present / market_expected >= 0.99` |
| 日単位の穴 | 完了済みUTC日のexpectedが正で、欠損本数 × 足幅が60分を超えたら `history_gap` |
| 判定不能 | expectedが0、暦・設定・DBの読取失敗等は `history_check_unavailable` |

日単位判定は `[B,N)` に含まれるexpectedをUTC日別に集計する。期間先頭の部分日もその対象部分を数え、現在進行中のUTC日は日単位判定から除く。進行中の日の不足も区間充足率には反映する。

24時間ちょうど、充足率0.99ちょうど、日別欠損60分ぶんちょうどは、それぞれの条件では合格する。日別の許容欠損本数は `floor(60分 / 足幅)` とし、それを超えたら穴とする。1mでは60本まで合格・61本で穴、5mでは12本まで合格・13本で穴、15mでは4本まで合格・5本で穴となる。比較は欠損本数と足幅を使って行い、表示用の丸め値は使わない。

空系列はstaleかつgapとする。人間面には両方を残し、代表理由は `unavailable → stale → gap` の順とする。

開場時間差は、既存の `market_hours.is_market_open` に基づき、週末・既知休場・承認済み無足日を除いて積算する。壁時計差も併記する。

0.95は採用しない。レビュー実測の正常下限0.9998に対し余裕を持たせつつ、数営業日分の欠損を許さない0.99を採用する。割合の比較は丸めた表示値ではなく正確な本数で行う。例えばexpectedが18,984本なら許容欠損は189本である。

### 3.4 人が無足を認める日の一覧

段1aで `config/history-no-bars.yaml` を追加する。coreが読み、agentの変更可能領域・workerへの入力には含めない。

各項目は次を持つ。

```text
source
base_interval
symbol
date_utc
reason
approved_by
approved_at
```

操作は、人間がbrokerの休場等を確認して項目を追加・削除し、`afx status` で適用結果を確認する手順とする。段1aでは専用編集CLIを追加しない。

- 指定は単一UTC日・単一系列に限り、無期限指定やワイルドカードを認めない。
- 当該日は期待格子と鮮度の開場時間から除外する。履歴行そのものを削除・補間しない。
- 適用した日、理由、承認者と一覧のdigestを人間面・内部証跡に残す。
- ファイル未配置は空一覧。不正な内容や読取失敗は `unavailable` とし、空一覧として黙って続けない。
- 一覧変更は判定方針の変更であり、古い承認証跡と既存再評価依頼の照合対象にする。

### 3.5 読取transaction

検査と再生を同じ長時間の読取transactionに固定する要件は削除する。通常historyは非上書きの追加であり、固定区間への追加は充足を下げない。削除を伴うtime repairは既存のepoch照合で扱う。

これは入力集合の完全一致や再現性を保証する設計ではない。長時間snapshotの導入は段1aに含めない。

## 4. gateと提出

### 4.1 配置

検査は共有 `evaluate_strategy_adoption_gate` の1か所に置く。

```text
既存の入力・依存・参照全無等の事前検証
→ in-sample
→ 取引数判定
→ in-sampleの床
→ 履歴検査
→ 候補holdout
→ holdoutの床
→ 配備baseline比較
```

- `floor_mode=enforce` の床割れは従来どおり先に返す。
- `bless` と配備中再評価の `floor_mode=warn` は床割れでも進むが、履歴検査を迂回できない。
- 改善開始時の重複検査は追加しない。worker起動、agent用in-sample backtest・分析に新しいholdout検査を掛けない。
- `history_health_fn=None` を注入口とする。Noneの場合は、呼出し時に `history_health` moduleの検査関数を解決する。関数を既定引数に束縛したり、別名importした関数を固定したりしない。運用CLIから差替えや無効化はできない。
- `run_holdout_gate` 内部には検査を追加しない。本番の呼出元は共有gateに集約されているため、そこで守る。任意期間の人間向け監査再生は引き続き可能だが、採用証跡には転用しない。

参照銘柄の部分的な穴は新guardの停止条件にしない。一方、系列が全くない場合の既存 `require_reference_history` は変更しない。欠損足は補間しない。

§3.3の空系列の診断は、statusと検査関数の直接呼出しに適用する。共有gateでは主pairが空なら先行するin-sampleの `NoHistoryError` で止まる。検査を前へ重複配置せず、既存の空履歴拒否と非提出を維持する。

### 4.2 結果と既定拒否

履歴不合格は、標本不足やholdout指標の `evaluable` と分けて返す。

```text
evaluable = false
unevaluable_kind =
  history_stale | history_gap | history_check_unavailable
observation_reason = 上記の固定語のみ
holdout_metrics = None
```

検査前に生成したin-sample行は保存する。再生していないholdout行や成功指標は作らない。approvalも配備も行わない。

変更は次を一体で扱う。

- `StrategyGateVerdict`、`GateOutcome`、notice側を含む関連する3か所のLiteral・判別子契約。
- `plugin/approval.py` の結果写像。
- `_run_full_gate` の拒否処理。
- `improve_notice_codes.py` の `_UNEVALUABLE_NOTICES`、`RESULT_CODES`、`validate_pair` の許可対。
- 改善終端、submit、bless、再評価の結果処理。

`_run_full_gate` は既知分岐の後に **`not outcome.evaluable` の既定拒否**を追加する。捕捉済み行を保存してから固定語の例外を返す。さらに未知の判別子は、`evaluable=True` でも成功扱いにしない。

継続可能な判別子は `ok` と、`floor_mode=warn` の場合の `floor` だけとする。いずれも `evaluable=True` が必要であり、それ以外は拒否する。

`floor_mode=warn` で許すのは収益性の床の警告だけであり、履歴不合格を警告に降格しない。

| 結果 | submit | bless | improve | reevaluate |
|---|---|---|---|---|
| 履歴3種の評価不能 | 依頼なし | 配備なし | 非提出終端 | 依頼なし |
| その他の既知の評価不能 | 既存拒否を維持 | 既存拒否を維持 | 既存終端 | 依頼なし |
| 未知の判別子 | 既定拒否 | 既定拒否 | 安全な失敗終端 | 依頼なし |
| 床の不合格 | enforceで拒否 | warnで継続可能 | 既存非提出 | 既存警告付き依頼 |
| 適正な評価結果 | 既存経路 | 既存経路 | 既存経路 | 既存経路 |

### 4.3 agent面と候補保全

agent向け分類は既存の `observation:not_submitted` に畳む。C0の `submission_deferred` と履歴専用の保留文言は追加しない。履歴不合格だけを見分けられる新しいラベル・文言を作らない。

tool応答、例外、次回prompt、backlog、mission要約、ledger、archive INDEX、agentが読める付属ファイルに、日時・端点・本数・比率・不足pair・穴位置・内部例外を流さない。新しい `observation_reason` は固定語だけとし、詳細結果は親の人間専用面に限定する。

人間向けnotice・reportでは「履歴のため評価不能・提出保留」を表示できる。improveの人間向け許可対に `("gate_failed", "history_not_ready")` を追加し、履歴3種をそこへ写像する。`RESULT_CODES` と `validate_pair` も同時に更新する。

Discordの人間向け文言は「履歴条件を満たさないため提出しませんでした。`afx status` で確認し、履歴補充後に候補を人間submitしてください」とする。このnotice対や文言はagentへ渡さず、他の非提出理由と同じ中立表現にする。

候補は既存archive経路に残す。ただしarchiveはGC対象であり、恒久保全は保証しない。

archiveから直接submitするAPIは現状存在しない。復旧後の人間手順は、残っている候補を `plugins/_human/<name>` に写し、`afx plugin submit --from _human` で新しく評価する。この操作では元missionとの自動的な結び付きが切れる。自動再提出は行わない。

## 5. 承認証跡とjournal

### 5.1 提出時の証跡

成功したstrategy gateの結果を、改善提出・人間submit・bless・配備中再評価のpayloadへ同じ形式で保存する。

```text
history_guard:
  version
  verdict = ready
  checked_at
  dataset
  pairs
  holdout_start / holdout_end
  latest_confirmed_bar: pair別
  market_present / market_expected: pair別
  missing_days / worst_day: pair別
  calendar_version
  threshold_snapshot
  no_bars_entries
  no_bars_digest
```

`history_guard_version` は証跡の `version` と一致させる。証跡はcoreが生成し、候補ファイルからの自己申告を受理しない。候補のhash・dataset・pair等は既存payloadの評価対象と一致させる。

`StrategyGateVerdict` と `GateOutcome` に `history_guard` 欄を追加する。検査へ未到達・不合格の場合はNoneとする。改善の `_build_approval_payload`、`submit_candidate`、bless、`_store_reevaluation` の4か所は、gateから受け取った同じ証跡を写すだけとし、それぞれで再計算・再生成しない。

既存coverageキーの意味は変えない。新証跡はpayload JSONに置き、新規テーブルは作らない。評価不能時は既存activityへ固定結果を残し、詳細は人間専用report・statusで確認する。activityをagent向け要約へ渡す際も同じ遮断を適用する。

### 5.2 承認時の照合と方針の範囲

承認時は現在のDBの鮮度・充足を再計算せず、提出時の証跡を照合する。

照合項目は、証跡の存在、構造、`verdict=ready`、guard規則版、暦・閾値の適合、対象範囲の無足一覧、payloadの評価対象との一致とする。既存のhash・依存pin・epoch等の照合は維持する。

guard規則版は完全一致とし、同じmajor等による暗黙の互換扱いは設けない。規則版・暦版・閾値を変更した場合は、旧方針の未決依頼を再評価する必要がある。表示やログだけの変更で規則版を上げない。

§3.4の「一覧のdigest」「一覧変更の照合」は、次の対象範囲に限定する。ファイル全体のdigestを承認条件に使わない。

- 証跡のdatasetに一致する `source`・`base_interval`。
- 証跡の主pair集合に含まれる `symbol`。
- そのUTC日が証跡の `[holdout_start, holdout_end)`、すなわち `[B,N)` と交差する項目。

coreは対象項目の全フィールドを正規化し、系列・日で安定ソートした配列を `no_bars_entries` に保存する。そのcanonical JSONのSHA-256を `no_bars_digest` とする。対象項目がない場合も空配列のdigestを持つ。

承認時は、現在の一覧から**証跡に保存したdataset・pairs・区間**で同じ抽出を行い、項目とdigestを照合する。現在時刻の新しい評価区間へ置き換えない。別dataset・別pair・区間外の日の追加・削除では照合を落とさない。対象項目の追加・削除・承認情報の変更は再評価を必要とする。

一覧の不正・読取失敗は、対象外の項目を含めて設定エラーとして扱い、適合とは判定しない。ただし一時的な読取失敗と、確定した証跡不一致は区別する。

証跡がない、旧版、不正、対象不一致、対象範囲の方針不一致の場合は、版作成・git・symlink切替・承認確定へ進まない。単に現在の履歴が埋まっただけでは、古い依頼の証跡なしを解除しない。依頼の出口は§5.5に定める。

提出時の根拠を守るには証跡照合が必要であり、承認直前のDB鮮度だけでは代用できない。通常historyの追加では評価済み入力の既存値は変わらず、閉場中はimportできない場合もあるため、承認直前のimportを必須にしない。

段1aでは承認証跡の新たな有効期限は導入しない。古いが適正な評価結果の扱いは残余として明示する。

### 5.3 検査位置と再開中journal

通常承認の新しい照合は、plugin lock内で、このapproval自身の既存journalの有無を確認してから行う。

| 状態 | 新しい証跡照合 |
|---|---|
| 自身のjournalがなく、新しい承認操作を始める | 必須 |
| 自身の既存journalをretry・reconcileで再開する | 行わない |
| 別approvalの未完journalがある | 既存の未解決journal処理を維持 |
| 再評価の承認 | `_confirm_reevaluation` 内、決定前に実施 |
| 再評価の却下 | 行わない |

自身のjournalを再開する操作は、既存の復旧分岐でjournalを作り直す場合も含め、開始済み操作の復旧として扱う。新照合を途中に挿入しない。

**I1への対応:** symlink切替済み・承認pendingで新しい照合を失敗させると、`approved_plugins` が新旧どちらも取引対象にできずstrategyが消える。その副作用を避けるため、再開中journalには検査を掛けない。既存のlive分類・hash再検証・rollback・復旧処理は維持する。導入前に開始したjournalも同じ扱いとする。

既存epoch照合が再開前にある問題は今回変更せず、残余として記録する。

再評価の新しい照合を、承認・却下共通の分岐前へ置いてはならない。却下、disable、期限切れ、journal rollbackには新guardを追加しない。既存のepoch等による拒否は従来どおり残る。

### 5.4 承認結果の全表示面

`history_not_ready` を `APPROVAL_OUTCOMES` に追加し、すべての承認結果表示を同時に対応させる。結果には必ず次のいずれかの固定 `reason` を設定する。

| 状況 | status | reason |
|---|---|---|
| 通常依頼の確定した証跡不一致 | `invalidated` | `history_evidence_invalidated` |
| 再評価依頼の確定した証跡不一致 | `pending` | `history_evidence_refresh_required` |
| 設定・一覧の読取失敗等で照合不能 | `pending` | `history_check_unavailable` |

対話shellの `_ops_decide` の `reason=` にもこの固定語を渡し、`reason=None` にしない。

全人間向け表示で、reasonに応じて次の文言を使う。

- `history_evidence_invalidated`: 「評価証跡が現在の方針に適合しないため、依頼を失効しました。候補を `plugins/_human/<name>` に保存し、履歴条件を確認して人間submitし直してください」
- `history_evidence_refresh_required`: 「再評価証跡の更新が必要です。依頼は保留中です。履歴条件を確認し、`afx plugin reevaluate --all-deployed --yes` を再実行してください。整理のために却下しないでください」
- `history_check_unavailable`: 「評価証跡を照合できません。依頼は保留中です。`afx status` で設定・読取エラーを確認し、修復後に承認を再試行してください」

| 面 | 表示・扱い |
|---|---|
| 対話shell | 固定reasonと対応する文言、次の操作を表示 |
| ctl client | outcome・status・reasonを保持し、同じ次の操作を表示 |
| web承認画面 | invalidatedとpendingを区別し、次の操作を表示 |
| Discord承認カード | invalidatedとpendingを区別し、次の操作を表示 |
| ops/serviceの結果変換 | 値を保持し、配備完了処理へ進めない |

これは承認結果の対応であり、後段に送るctl・webのstatus拡張とは別である。

### 5.5 照合が合わなくなったpendingの出口

通常の改善提出・人間submitの依頼は、承認試行時に確定した証跡不一致を検出したら、既存の `invalidated` 状態を使い、固定理由 `history_evidence_invalidated` で失効させる。判定と失効記録はplugin lock内で行う。自身のjournalがある依頼には適用しない。

rejectは使わず、人間の自由記述による履歴情報を改善promptへ運ばない。agent向けには既存の中立な非提出・失効表現だけを渡し、固定理由も履歴専用の分類として露出させない。失効処理は候補・archiveを削除しない。

人間の再提出手順は次とする。

1. 旧依頼とhashを照合し、残っている候補を `plugins/_human/<name>` に保存する。別候補で上書きしない。
2. `afx status` で履歴と無足一覧を確認し、必要なら補充・修復する。
3. `afx plugin submit --from _human` で現在の方針のgateを新しく通す。
4. 新依頼の証跡と成績を確認して承認する。

候補がGC等で残っていなければ、旧payloadの成績だけで再提出せず、候補の復元または再作成が必要である。元missionとの自動的な結び付きは復元しない。

再評価依頼は、既存配備の停止を避けるため、照合不一致だけでは失効・却下しない。§6の再実行で新依頼を作り、旧依頼を原子的にsupersededにする。新評価が履歴不合格なら旧依頼はpendingのまま残し、人間面に未完了と次の操作を表示する。

一覧の読取失敗等では、修復により元の証跡が再び照合できる可能性があるため、通常依頼も失効させずpendingを維持する。

## 6. 再評価の重複判定

`_Snapshot.key()` と `_reevaluation_key(payload)` の双方に `history_guard_version` を加える。方針識別情報には、暦版、閾値、§5.2の対象範囲の無足一覧digestを含める。ファイル全体のdigestは使わない。

`existing` を返せる条件は次のすべてとする。

- 既存のコード・artifact・epoch・基準決定・配備世代の鍵が一致する。
- 現在のguard規則版・暦版・閾値が一致する。
- 既存証跡のdataset・pairs・評価区間で抽出した現在の無足一覧が一致する。
- 正しい `ready` 証跡がある。

新評価前の `existing` 判定では、既存依頼の証跡に保存した区間を使って一覧を照合する。保存時の再照合でも、現在の一覧を既存依頼自身の区間で照合する (新評価の区間は使わない)。評価中に時計が進んだだけで、適正なpendingを無効にしないためである。

証跡なし・旧規則・対象範囲の方針不一致のpendingを、評価前にも保存時の再照合でも `existing` として再利用しない。新評価に成功したら、新依頼の作成と旧依頼の `invalidated / reevaluation_superseded` を同じtransactionで行う。旧pendingをrejectして整理してはならない。rejectは配備停止を伴うためである。

履歴不足で依頼を作らなかった場合は、穴埋め後に再実行して実評価する。通常importでepochを人為的に増やす運用は採らない。

同じ規則の適正な証跡付きpendingは、通常importの追加だけでは無効にしない。追加後の成績を改めて測り直す機能や不変入力snapshotは、別の要件として扱う。

## 7. `afx status`

段1aではローカルの `afx status` に `evaluation_history` を1ブロック追加する。daemon停止中も読取専用接続で計算し、既存ライブdata・referencesとは区別する。

表示は次に絞る。

- dataset、検査時刻、guard規則版、閾値、無足一覧の適用状況。
- 主pairごとの最新保存足・最新確定足、壁時計差・開場時間差。
- holdoutのpresent・expected・充足率、最悪の日の欠損本数と時間、欠損が60分ぶんを超える日。
- `ready / history_stale / history_gap / unavailable`。複数理由があれば併記。
- 参照銘柄について、主pairに足があるUTC日なのに0本だった日の一覧。
- 手動import、開場後の再試行、配備中版の再評価への案内。

無足一覧のファイルパスはrepository rootを基準とする `config/history-no-bars.yaml` に固定し、実際に解決したパスを人間面に表示する。実行時のカレントディレクトリによって別の一覧を読まない。

この実ファイルを `.gitignore` に追加する。追跡する `config/history-no-bars.yaml.example` には空一覧と記入例を置き、broker固有の実値は含めない。`reason`、`approved_by`、aware UTCの `approved_at` は、休場等を確認した人間が手書きする。

statusの無足一覧表示には、§5.2の対象範囲で抽出した項目とdigestを使う。§3.4の一覧照合についてもこの範囲規則を適用する。

参照の0本日は疑いの表示であり、休場と取得不足を断定しない。採用停止にも使わない。20有足日の中央値との比較は導入しない。

DB・設定・暦の読取失敗では、このブロックを `unavailable` とし、status全体の表示は維持する。statusは履歴取得や修復を実行しない。

## 8. 移行と15件の扱い

### 8.1 対象の訂正

09-04以降の承認15件は、**配備中4版に対応する承認8件と、非配備7版に対応する承認7件**である。合計11版を評価する。版はartifact hashで固定し、content hashもmanifestへ記録する。

| 配備 | 名前 | artifact hash先頭 | 対応する承認id |
|---|---|---|---|
| 配備中 | ichimoku_rsi_cloud_reentry | `3b35b61fa798` | #32・#92 |
| 配備中 | volume_close_pressure | `127825cc55df` | #65・#94 |
| 配備中 | asym_shock_breakout | `0b5ec2c155db` | #73・#89 |
| 配備中 | close_pressure_followthrough | `ee670b8630f6` | #74・#91 |
| 非配備 | sma_cross_10_30 | `395e2ce2d065` | #21 |
| 非配備 | bb_ema_breakout | `eb0bd5257957` | #23 |
| 非配備 | rsi_bb_reversion_200 | `dc59a62892ca` | #24 |
| 非配備 | rsi6_bb200_rr2 | `8ad4942e2d29` | #25 |
| 非配備 | asym_shock_breakout | `1483bc7e63ea` | #39 |
| 非配備 | close_pressure_followthrough | `12d14addcf7c` | #71 |
| 非配備 | close_pressure_followthrough | `a74732d7d316` | #72 |

表のhashは表示用の省略形であり、実運用の対象manifestでは完全長を用いる。

配備中4版の再評価承認 #89・#91・#92・#94 は、いずれも `eval_now=2026-11-07T07:05Z`。レビューの停止再現では、この時点の履歴は充足率約0.31である。

### 8.2 段1aの運用手順

1. 整合したDBバックアップ、承認id・完全長hash・依存pin・現在配備の対象manifestを保存する。
2. 新guardとrollback拒否を導入する。既存strategy pendingはC1調査時点で0件なので、一括再提出・無効化は行わない。将来の証跡不一致依頼は§5.5の手順で扱う。
3. 開場中に手動importで主pairのholdoutを補充し、参照も可能な範囲で補う。衝突は上書きせず確認する。
4. `afx status` で鮮度・区間充足・日別欠損を確認する。最新足の更新だけで完了にしない。
5. `afx plugin reevaluate --all-deployed --yes` を実行する。対象manifestの配備中4版と照合する。
6. 人間が新依頼の成績と証跡を読み、継続承認または却下による配備停止を決める。
7. §8.5のmanifestに、新依頼idまたは評価不能理由と人間の決定を記録し、対応する過去承認8件との関連付けを検証する。

新評価は版単位で1回行い、その結果を対応する過去承認2件へmanifestで関連付ける。同じ配備版を二重に再生しない。

評価不能なら、新依頼を作らず、理由と未完了状態を記録する。既存配備は自動停止せず、必要なら人間が `afx plugin disable <name>` を使う。

`docs/operations/reevaluate-deployed-after-history-repair.md` には、time repairだけでなく通常importによる穴埋め手順、guard規則版、`existing` の新条件、`history_not_ready` の固定reasonと次の操作、通常依頼の失効・候補保存・再submit、manifestの更新・検証手順を追記する。

### 8.3 「15件すべて再評価」と段1bの関係

利用者の既存裁定「15件すべて再評価」は維持する。段1a完了は、その全件完了を意味しない。

- 段1a: 配備中4版を再評価し、過去承認8件へ結果を関連付ける。
- **段1bの先頭task**: 非配備7版を配備せずに監査再生し、残りの承認7件へ結果を関連付ける。

段1bでは `.versions` の正確なコード版と依存pinから評価入力を復元する必要がある。現在のinventoryや同名最新版で代用しない。この監査再生は現行 `reevaluate_deployed` では実行できず、別チケットの実装対象である。

新run・証跡または復元不能等の根拠付き理由を、旧承認idと版へ関連付ける。再評価不能は成功に数えず、残作業として明示する。15件の追跡表を閉じるのは、この段1bの結果を記録した後とする。

### 8.4 裁定済みのrollback制限と人間専用の強行操作

**裁定（2026-11-11）: 適正な新証跡のない版へのrollbackは、段1bの監査結果が出るまで拒否する。段1aでこの拒否を導入する。**

新しいstrategy rollback操作では、`resolve_rollback_target` の明示 `--to` と既定対象の双方について、対象を解決した後に証跡を確認する。対象の名前と完全長artifact hashに一致する最新の承認済みpayloadを使い、§5.2の有効な `history_guard` と既存のhash・依存pin・epoch等を照合する。別版の証跡や、却下・pendingの新依頼を代用しない。

適正な証跡を確認できなければ、既存の型を使って次を返す。

```text
RollbackRefusal(
  code="history_not_ready",
  reason="history_evidence_required"
)
```

新しいdeploy操作の作成・symlink切替・runtime activationへ進まない。読取失敗も拒否する。対象がindicatorの場合は新guardの対象外とする。

拒否は新規操作開始時、plugin lock内で行う。既に開始したrollbackのretry・reconcileと、承認journalの補償rollbackには挿入せず、既存の復旧を維持する。

人間専用の強行操作は、ローカルの運用CLIに次の形で設ける。

```text
afx plugin rollback <name> --to <完全長artifact_hash> \
  --force-history-guard --reason "<人間が記入する理由>"
```

- 強行には完全長hashと空でない理由を必須とする。既定対象や短縮hashでの強行は受理しない。
- `--force-history-guard` は今回の履歴証跡拒否だけを迂回する。hash、依存、live状態、未解決操作等の既存検証は維持する。
- worker、agent tools、mission設定、ctl・web・Discordからこの指定を受け付ける経路は追加しない。
- 強行指定、対象hash、実行者、UTC時刻、理由を既存activityへ人間専用の監査記録として保存する。記録に失敗した場合は、opを作らず切替へ進まない。
- 開始の監査はactivityに書き、opとの関連付けはopと同じtransactionでDBに記録して、乱数のforce_tokenで結ぶ(DBは対象hash・実行者・UTC時刻・tokenだけを持ち、理由の本文は持たない)。再開時に監査の書込みも強行指定も要しない。理由: 完了経路ごとにactivityの読み返しで書込み済みを判定する方式は、部分行・log回転・経路の漏れで3周続けて欠陥が出たため。
- 自由記述理由と履歴詳細はagent向けactivity要約から除外する。
- 強行は適正証跡を生成せず、監査完了やmanifest上の再評価成功にも数えない。

段1aの通常運用と移行作業では強行を使わない。受入では、非配備7版への通常rollbackがすべて拒否されることと、明示的な人間の強行経路だけが履歴証跡拒否を迂回できることを分けて検証する。

段1bの監査で適正な証跡を得た版は、承認済み根拠へ結び付ける後続実装を経て通常rollbackを可能にする。manifestに監査結果を書いただけでは拒否を解除しない。

### 8.5 移行manifestの置き場・形式・書き手

記録先はrepository root基準の次のJSONファイルに固定する。

```text
data/history-guard-migration/manifest-2026-11-11.json
```

`data/history-guard-migration/` は明示的にgitignore対象とし、agentの入力・変更可能領域に含めない。DBバックアップとともに人間が保全する。

書き手はtask 6を実行する指揮者側の運用担当者とする。gateやworkerによる自動更新は実装範囲に含めない。task 6の実行時に、DBとコマンド結果を照合して更新する。

トップレベルには `schema_version=1`、基点commit、バックアップの識別子、作成・更新UTC時刻、記入者、`entries` を持つ。`entries` は旧承認1件につき1行、計15行とする。各行は次を持つ。

```text
old_approval_id
name
artifact_hash                 # 完全長
content_hash                  # 完全長
dependency_pins               # 旧承認・対象版から保存
deployed_at_inventory         # 初回棚卸し時点のbool
phase                         # 1a または 1b
result_status                 # pending / evaluated / unevaluable / deferred
new_reevaluation_approval_id   # evaluatedなら必須、それ以外はnull
unevaluable_reason            # unevaluableなら固定語、それ以外はnull
evaluated_at                  # 評価試行のaware UTC時刻
evidence_ref                  # 新payloadまたは人間専用reportの参照
human_decision                # pending / approved / rejected / not_applicable
decided_at
```

段1a開始時に15行を作成する。配備中8行は `phase=1a`、非配備7行は `phase=1b / result_status=deferred` とし、未評価であることを明示する。同じ配備版の旧承認2行には、同じ新再評価approval idまたは同じ評価不能結果を記録する。既存payloadの単一 `reevaluates_approval_id` は変更しない。

task 6の記録の受入は、次をすべて満たすこととする。

- 旧承認id15件が§8.1と完全一致し、重複・欠落がない。
- 配備中8行すべてに、新再評価approval idまたは固定の評価不能理由と評価試行時刻・根拠参照がある。
- 新idがある場合、DBの対象name・完全長hash・ready証跡と一致する。
- 同じ版の旧承認2行が同じ結果を参照し、4版を二重に再生していない。
- 人間が決定した場合、その決定とDB上の結果が一致する。
- 非配備7行は段1b未完了として残り、段1aの新配備を指していない。

8行に理由が入れば記録要件は満たすが、`unevaluable` は再評価成功ではない。配備中4版の評価・人間の決定が未完了なら、その残作業を最終報告に明示する。段1bも同じmanifestを更新し、15件の追跡を継続する。

## 9. 受入・検証

以下は実装時に満たす条件であり、本改訂でテスト済みという意味ではない。

| 対象 | 受入の要点 |
|---|---|
| importの時計境界 | 5m・grace30秒で `T=12:05:20Z` なら `E=11:55Z`、保存は11:50足まで。`T=12:05:30Z` なら `E=12:00Z`、保存は11:55足まで |
| importの入出力 | naive応答・不正窓・格子外を保存前に拒否。CLIの非ゼロ時差を拒否。UTC・naiveの文書どおりの入力を受理 |
| ページ・容量 | inclusive右端を次窓で取得。容量は実効終端で判定。対象なしは取得0・書込み0・rc0 |
| 不変保存 | 衝突時に旧値を保持し、既存の部分commit契約を維持 |
| 暦と充足 | DST・週末・祝日、24時間境界、比率0.99境界、1mの60／61本・5mの12／13本・15mの4／5本を検証 |
| 無足一覧 | 指定系列・日のみ除外。不正設定はunavailable。対象範囲の変更は再評価、別dataset・別pair・区間外の変更は照合を妨げない |
| 無足一覧の管理 | repository rootから解決。実ファイルはgitignore、exampleは追跡。人間記入の承認情報をstatus・証跡で確認できる |
| 主pair集合 | 1pairでも不合格なら候補・baselineのholdout呼出し0、approval・配備なし |
| gateの全数表 | 判別子全値×submit・bless・improve・reevaluate。継続はevaluableなokとwarn時のfloorのみ。標本不足の設定で履歴guardを迂回できない |
| 評価順序 | 取引数不足・enforce床割れでは履歴検査へ進まない。warnでも履歴不合格なら止まる。空系列は先行する既存拒否で止まる |
| 証跡の運搬 | `StrategyGateVerdict` → `GateOutcome` → payload生成4か所で同じ証跡を保持し、再生成しない |
| 承認 | 確定不一致の通常依頼は固定理由でinvalidated。再評価依頼と一時的照合不能はpending。適正証跡は時間経過だけで拒否しない |
| pendingの出口 | 通常依頼の候補保存・新submitと、再評価依頼の再実行・supersededを検証。整理目的のrejectを使わない |
| journal | switched済みjournalが旧証跡や現在履歴不足で止まらず、既存再検証を経て復旧する |
| 再評価決定 | 新guardによって却下・配備停止が阻害されない |
| 結果の表示 | `APPROVAL_OUTCOMES`全値×shell・ctl・web・Discord。固定reason、実際のstatus、次の操作を表示 |
| improve notice | 新許可対を `RESULT_CODES`・`validate_pair` が受理し、Discordは指定文言、agent面は既存not_submitted |
| 再評価の鍵 | epoch不変の穴埋め後でも旧guard依頼をexistingにせず、新依頼作成とsupersededを一体で記録 |
| 漏洩 | 日時・本数・比率・pairのcanaryと、人間の強行理由が全agent sinkに不在。分類は既存not_submitted |
| status | daemon停止中も計算。参照の11-04〜11-09の0本日を、該当観測のDB写しで表示。失敗はブロック単位のunavailable |
| rollback | 明示・既定対象とも証跡なし・不正・対象不一致を既存RollbackRefusalで拒否。新操作・切替なし。再開・補償rollbackは阻害しない |
| 人間専用の強行 | 完全長hash・理由が必須。agent経路から指定不可。監査記録失敗時は切替なし。既存の他の検証を迂回しない |
| 移行 | manifest15行の対象一致、配備中8行の新idまたは理由、同一版2行の対応を検証。非配備7版は通常経路で配備されない |

### 9.1 差し替え口とreadyにするtest群

gateの注入引数がNoneの場合は、module属性を呼出し時に参照する。既存の入口テストは、この同じmodule属性を差し替えられるようにする。

ready差し替えの対象は `tests/plugin`、`tests/loops`、`tests/backtest`、`tests/e2e`、`tests/ops` 配下の既存の合成fixtureを使うテストとする。共通conftestに対象パスを限定したfunction-scope autouse fixtureを1つ置き、各packageで重ねてpatchしない。

`@pytest.mark.real_history_guard` をpytest設定に登録し、このmarkerが付いたテストではautouse fixtureが検査関数を変更しない。新設する履歴検査の単体テスト群にもmodule単位でこのmarkerを付ける。

ready差し替えはMagicMockを使わず、本物と同じ結果型を返す関数とする。入力のdataset・pairs・時刻・区間に対応する構造的に正しいready証跡を返し、Noneや固定の別対象の証跡で承認テストを通さない。

差し替えの所有者は共通fixtureだけとする。テストごとの失敗注入には、共通fixtureが提供する応答設定を使う。autouse patchの上から同じ属性をmonkeypatchしない。`monkeypatch.undo()` による一括解除は禁止し、fixtureの通常のteardownで元の関数へ戻す。この規則を実装者向けブリーフにも記載する。

gate単体の明示的な `history_health_fn` 注入は使用できる。本物の検査を確認するテストでは、この引数も差し替えない。

### 9.2 本物の検査を通す結合テスト

improve・submit・bless・reevaluateの各入口に、`real_history_guard` marker付きの結合テストを置く。

共通helperは、固定UTC時刻、dataset、pairs、暦から3か月のholdoutと必要なin-sample履歴を生成する。5mのholdoutは約1.9万本×pairとなる。ネットワークと本番DBは使わない。

4入口それぞれで、少なくとも次を確認する。

- 暦どおりの充足した履歴で本物の検査を通り、ready証跡が結果・payloadへ届く。
- 同じ履歴から確定足を除いてstaleまたはgapにすると、候補・baselineのholdoutを呼ばず、依頼・配備を作らない。

承認証跡の対象範囲照合、通常依頼の失効、再評価のsuperseded、rollback拒否と強行は、実際の証跡検証関数を通す。ready差し替えによってこれらの受入を代用しない。

## 10. 段1b以降

以下は段1a本文の実装要件から外す。

| 段 | 対象 |
|---|---|
| 段1b・先頭 | 非配備7版の正確なコード・依存pinによる監査再生。manifestを更新し、承認15件の追跡を完了する |
| 段1b | 監査で得た証跡を承認済み根拠へ結び付け、適格な版の通常rollbackを可能にする |
| 段1b以降 | ctl・webのstatus、参照銘柄の20有足日中央値による欠損検知 |
| 段1b以降 | archive候補の専用再提出・保全操作 |
| 段1b以降・必要性を再検討 | 同じ読取transaction、snapshot/as-of、不変履歴による再現性 |
| 段2 | 衝突の永続記録、衝突後続行、人間による置換、`afx history sync` |
| 段3 | 開場中の定期同期、閉場時deferred、異常・悪化・回復通知 |
| 別チケット | cache/historyの二重保存解消・表統合 |

証跡のない版への新規rollback拒否と人間専用の強行操作は、段1aに含める。

新しい永続テーブルが必要になる段では、リポジトリ規約どおりschema version・migration・旧fixture・整合性テストを伴わせる。

## 11. 残余と限界

- 検査は鮮度と充足を守るもので、過去に混入した形成中足や価格値の正しさを保証しない。
- 1本分の余裕を超える時計差は保証対象外。必要なら後段でbridge時計との照合を追加する。
- 通常importによる追加後も、既存payloadの成績は自動再生しない。
- 適正な旧評価証跡の有効期限は導入しない。
- guard規則版・暦版・閾値の変更は、旧方針のpendingの再評価を必要とする。無関係な無足一覧項目の変更は必要としない。
- 既存epoch照合によるjournal再開・再評価却下の制約は残る。新guardによる制約は加えない。
- MT5が保持しない期間は埋められない。評価期間を自動短縮しない。
- 非配備7版の監査は段1bに残る。段1aでは通常rollbackを拒否し、人間の明示的な強行は監査完了に数えない。
- archive候補の恒久保全は保証しない。失効した依頼の候補が失われていれば、人間による復元・再作成が必要になる。
- manifestは運用担当者が記録・保全する。関連付けの記録は、過去承認時点の履歴が適正だったことを証明しない。
- guardは既存売買の自動停止、資金保護処理の停止、過去承認の正当化を担当しない。

## 12. レビュー対応

1周目のCritical C1、Important I1〜I10はすべて採用する。閉鎖確認で残ったI8のrollback導入時期と関連付けの記録先は、§8.4・§8.5で閉じる。

1周目のMinorも全件採用する。M1参照全無の既存拒否維持、M2固定reason、M3既存JSON・activityの利用と永続記録の後送り、M4core定数、M5ローカルstatusへの限定、M6開始時検査の削除、M7gateのnow共有、M8却下への非適用、M9runbook更新、M10対象なしrc0を維持する。

閉鎖確認2周目の新Important 5件は次のとおり採用する。

| 指摘 | C2での決定 |
|---|---|
| N1 無足一覧全体の照合とpendingの行き止まり | §5.2でdataset・pairs・区間に限定。規則版は完全一致。通常依頼は固定理由で失効して人間submit、再評価依頼は再実行でsuperseded。全表示面に次の操作を示す |
| N2 日別12本固定 | §3.3を60分ぶん超へ変更。足幅で換算し、境界を検証する |
| N3 rollbackの導入時期 | §8.4で段1aに既存RollbackRefusalによる拒否と人間専用の強行を定義。task 7を追加 |
| N4 承認8件の関連付け先 | §8.5で人間専用JSON manifest、運用担当者、15行の形と8行の受入を定義 |
| N5 test差し替え | §4.1・§9で呼出し時のmodule参照、対象test群、marker、単一patch所有者、実検査の4入口結合テストを定義 |

閉鎖確認2周目のMinor 6件も全件採用する。

| 指摘 | 反映先 |
|---|---|
| M-a 継続可能な判別子 | §4.2で `ok` とwarn時の `floor` だけと明記 |
| M-b noticeの許可対と文言 | §4.2・§4.3で `RESULT_CODES`・`validate_pair`・Discord文言を定義 |
| M-c 無足一覧の管理 | §7でrepository root基準、gitignore、example、人間による承認情報の手書きを定義 |
| M-d 空系列の到達経路 | §4.1でstatus・直接検査の診断と、gateの先行NoHistoryErrorを区別 |
| M-e 証跡を運ぶ型 | §5.1で両結果型の欄とpayload生成4か所への受け渡しを明記 |
| M-f shellのreason | §5.4で固定reasonを必須にし、Noneを防ぐ |

採らないMinorはない。task 1・2への変更範囲を守るため、M-cの補足と一覧digestの範囲規則は§3を変更せず§5・§7に置く。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-12 | C2.1 | §5 の保存時の再照合を実装に合わせて訂正 (現在の一覧を既存依頼自身の区間で照合) | 新評価の区間で照合すると、評価中に時計が進んだだけで適正な pending が無効になるため。入口ごとの実検査の結合 test を追加 | — |
| 2026-11-12 | C2.2 | 強行rollbackの監査を、activityの読み返しによるop記録から、開始の監査行(force_token入り)とopと同一transactionのDBの印の対へ組み直し。op記録のevent・完了経路での書込みを廃止。activityの追記は部分行を改行で閉じてから書く | 同じ部品でCriticalが3周続いたため。指揮者の指示 | — |
| 2026-11-11 | C2 | 日別欠損を60分ぶん超へ変更。無足一覧の照合を評価対象範囲に限定し、pendingの出口と次の操作を定義。rollback拒否・人間専用強行を段1aへ追加。移行manifest、test差し替え、実検査の結合テスト、Minor 6件を具体化 | opus閉鎖確認 新Important 5 / Minor 6。指揮者裁定を反映し、I8の残りを閉鎖。§2・§3の変更は日別閾値だけに限定 | — |
| 2026-11-11 | C1 | 既定拒否、証跡照合、journal 再開、閾値 (充足率 ≥ 0.99・日別欠損 12 本超・無足日一覧)、15 件の内訳と段 1a・1b の境界を訂正。非配備 7 版への rollback は監査まで拒否 (利用者裁定) | opus 1 周目 Critical 1 / Important 10 / Minor 10。利用者承認 (閉鎖確認と並行で実装) | — |
| 2026-11-11 | C0 | 段 1 初版。import、採用・承認前検査、status、15 件再評価を定義 | 評価用履歴が 09-04 で止まり holdout が空のまま承認された件 | — |

## 段1a task表

| 順 | task | 受入の要点 |
|---|---|---|
| 1 | importの安全境界 | 固定clock、1本分の余裕、実効終端で容量判定、naive応答・CLI非ゼロ時差拒否、対象なしrc0 |
| 2 | 共通の履歴検査 | 24開場時間・充足率≥0.99・日別欠損60分ぶん超・unavailable、無足一覧、暦と境界の検証 |
| 3 | 共有gate | in-sample床の後で1回検査、okとwarn時floorだけ継続、評価不能の既定拒否、notice許可対、全入口の非提出・漏洩防止、差し替えと実検査の結合テスト |
| 4 | 承認と再評価の鍵 | 両結果型からpayloadへ証跡を運搬、対象範囲の一覧照合、通常依頼の固定理由による失効と再submit、再評価のsuperseded、journal復旧維持、全表示面の固定reasonと次の操作 |
| 5 | `afx status` | 主pairの鮮度・充足・60分超の日別欠損、参照0本日、対象範囲の無足一覧、root基準のパス・gitignore・example、読取失敗時unavailable |
| 6 | 配備中4版の再評価 | 穴埋め後に新依頼または評価不能理由を記録。指定manifestの配備中8行すべてに新idまたは理由・時刻・根拠参照を入れ、同一版2行の関連を検証。人間が継続・停止を決定 |
| 7 | rollbackの証跡拒否と人間専用強行 | 明示・既定対象とも有効証跡なしは既存RollbackRefusalで拒否。完全長hash・理由を伴うローカル人間CLIだけが履歴拒否を迂回。監査記録必須、既存検証・開始済み操作の復旧を維持 |
