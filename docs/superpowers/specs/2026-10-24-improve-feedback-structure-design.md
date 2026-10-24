# 改善ループに渡す履歴の情報構造 設計書

**版:** v0.1  
**日付:** 2026-10-24  
**対象チケット:** `improve-context-family-attempt-summary`、`improve-deployed-signal-lookup-gap`  
**状態:** 設計案

## 0. 目的、範囲、非目標

改善 agent が次の mission で仮説を選ぶとき、過去の候補を一件ずつ眺めて「未試行らしい族を順に消費する」のではなく、仮説族ごとの試行量と終わり方、配備との重なりを把握できるようにする。同時に、取引判断 loop 専用の `get_signals` を改善 mission でも探し続ける無駄と、それを伝える同文 note の反復を止める。

この変更は改善の**頻度や周回数を変えない**。変えるのは次周へ渡る情報だけである。

範囲は次のとおり。

- `candidate_archives` から、仮説族 key、試行回数、安全な結果区分ごとの件数、最終試行日だけを親が集計し、改善 prompt に渡す。
- 配備済み族を同じ表示へ統合し、同じ族を履歴表と配備済み一覧へ二重に出さない。
- `get_signals` が改善では利用できず、直近 signal の方向を改善材料にしないことを prompt に明記する。
- backlog note の現在の「新しい順 20 件」という窓の中で、同文を newest-wins で重複排除する。

非目標は次のとおり。

- 候補 archive の `metrics_json`、PF、`avg_r`、取引数、勝率、drawdown、期間、順位、score その他の成績を族別履歴へ出さない。
- `approval_requests.reason` その他の人間による reject 理由を出さない。
- `quality_flags`、候補名、hash、archive path、pair 単体、mission transcript を族別履歴へ出さない。
- 改善側に `get_deployed_directions` などの新しい signal 読み取り tool を追加しない。既存 `get_signals` も許可しない。
- cadence、並列数、tool budget、backtest 回数、床、承認手順を変えない。
- 現在の「直近の成績レポート」は実運用側の既存情報面であり、本 spec では変更しない。ただし、候補 archive や backtest の成績をこの節へ迂回して混ぜることも許さない。
- in-sample 成績による族別の順序尺度は扱わない。これは遮断境界を別途決める後続事項である (§9.4)。

## 1. 現状と守る境界

### 1.1 現物から確認できること

| 面 | 現状 | この設計での扱い |
|---|---|---|
| 改善 context | `build_improve_context` は成績レポート、直近 50 mission の改善履歴、現行構成、backlog、方針、参照を組み立てる。`candidate_archives` は読まない | 安全な明示 projection だけを新しい族別要約へ足す |
| `last_result` | `project_last_result` は既知値を固定語彙へ畳み、未知値は `unclassified` にする。`_MISSION_DIAGNOSTIC` は呼出回数だけを残し、理由・成績・archive 位置を捨てる | 同じ fail-closed 原則を `outcome_class` に適用する |
| backlog note | `_backlog_section` は全 note を新しい順にし、先頭 20 件を表示する。重複排除はない | 20 件の窓は維持し、その窓内だけを重複排除する |
| prompt | 「改善履歴」「現行構成」「配備済みの仮説族」「バックログと既知の事実」を別々に表示する | 「配備済みの仮説族」を族別試行状況へ統合する |
| 仮説族 | `family_key` は、strategy なら依存 indicator 名の集合 + 足 + pair 集合、indicator なら名前 + 足。依存 indicator がない strategy は key を持たない | 現行の key 算出規則を共有し、別の規則を作らない |
| 候補結果 | `outcome_class` は 14 値の CHECK を持つ。`quality_flags` は `family_detected` / `legacy_unlinked` の検出属性 | 結果は安全な固定語彙へ投影する。flag は出さない |
| 同族判定 | `find_same_family` は配備済みの同 kind・別名・同 family key を `family_redirect` の対象にする。duplicate 判定は別のロジックである | `family_redirect` は該当する配備済み族と同じ要約行で数える |
| signal tool | `get_signals` は strategy 行に `in_sample_metrics` も添える。`IMPROVE_FORBIDDEN` は `get_signals` と `bless` を改善から除外する | 遮断を維持し、prompt で意図を説明する |
| 改善 RPC | `run_backtest` / `analyze_corr` の agent 応答は `_strip_forbidden` が期間端点などを再帰的に剥がす。改善 tooldef はこの 2 tool を含む | tool の追加も返却 schema の拡張もしない |

依頼時の DB スナップショットは「46 行・24 族、空 `family_key` 24 行、結果件数 `not_submitted=39 / rejected_by_human=7 / approved=2 / revoked=2`」である。ただし列挙された結果件数の合計は 50 で総行数 46 と一致しない。本設計はこの数を定数や AC の期待値にせず、実装着手時の read-only 集計でスナップショットを再確認する。集計規則自体は件数に依存しない。

### 1.2 不変条件

1. 親だけが DB を読み、worker へは完成済みの投影を prompt として渡す。worker に DB path や履歴読取 tool を与えない。
2. `candidate_archives` から読む列は `family_key`、`outcome_class`、`created_at` の 3 列だけである。`SELECT *` を使わず、metrics、名前、hash、path、pair、flags を取得しない。
3. 人間の reject 理由を持つ表とは join しない。
4. identifier 以外の文字列は固定語彙だけにする。自由文をこの要約へ入れない。
5. 族別要約の結果は成績の良し悪しを意味しない。特に `rejected_by_human` は理由を含まず、`not_submitted` は非提出だった事実しか表さない。

## 2. 採用する形

親は archive の安全な 3 列を読み、配備時点の inventory と合わせ、`improvement_history.family_attempts` を作る。prompt では既存の「配備済みの仮説族」を置き換え、「仮説族の試行状況」1 節として表示する。

### 2.1 公開型

Python の型としては次を契約とする。ここで `CanonicalFamilyKey` と `CanonicalPluginName` は自由文ではなく、現行の family key 生成規則と plugin 名規則を通った bounded identifier である。

```python
PublicOutcome = Literal[
    "not_submitted", "pending", "approved", "rejected_by_human",
    "expired", "invalidated", "revoked", "gate_failed", "noop_copy",
    "duplicate_logic", "duplicate_metrics", "family_redirect",
    "unclassified",
]

class FamilyAttemptSummary(TypedDict):
    family_key: CanonicalFamilyKey | Literal["族不明"]
    attempts: int                         # 0 以上
    outcomes: dict[PublicOutcome, int]    # 非ゼロだけ、表示順は固定
    last_attempt_on: ISODate | None       # YYYY-MM-DD、時刻なし
    deployed: Literal["yes", "no"]
    deployed_names: list[CanonicalPluginName]

class UnkeyedDeployedSummary(TypedDict):
    name: CanonicalPluginName
    kind: Literal["indicator", "signal", "strategy"]
    timeframe: CanonicalTimeframe | None
    pairs: list[CanonicalPair]
    indicators: list[CanonicalPluginName]

class FamilyAttemptSection(TypedDict):
    rows: list[FamilyAttemptSummary]
    unkeyed_deployed: list[UnkeyedDeployedSummary]
    entries_omitted: int
```

外へ出る可変値は bounded identifier、非負整数、ISO 日付だけである。結果 label、kind、`deployed` は固定語彙である。`None` は prompt で固定記号 `-` として表示する。`outcomes` が空の配備済み未試行族も `-` とする。`UnkeyedDeployedSummary` は現行の配備済み族表示がすでに持つ構成情報を縮退させずに移すための型であり、自由文や params、hash、成績は持たない。

### 2.2 結果区分の投影

入力 `outcome_class` は次の表だけを通す。表にない値、文字列でない値、空文字は `unclassified` とする。入力をそのまま fallback にしない。

| 保存値 | 公開値 | 理由 |
|---|---|---|
| `not_submitted` | `not_submitted` | 非提出の事実だけ |
| `unprofitable` | `not_submitted` | 成績による非提出との区別を渡さない |
| `insufficient_trades` | `not_submitted` | 取引数による非提出との区別を渡さない |
| `pending` | `pending` | 固定のライフサイクル状態 |
| `approved` | `approved` | 固定のライフサイクル状態 |
| `rejected_by_human` | `rejected_by_human` | 理由を伴わない固定状態 |
| `expired` | `expired` | 固定のライフサイクル状態 |
| `invalidated` | `invalidated` | 固定のライフサイクル状態 |
| `revoked` | `revoked` | 固定のライフサイクル状態 |
| `gate_failed` | `gate_failed` | gate 名・数値・理由を伴わない固定状態 |
| `noop_copy` | `noop_copy` | 固定の質検査結果 |
| `duplicate_logic` | `duplicate_logic` | 固定の質検査結果 |
| `duplicate_metrics` | `duplicate_metrics` | 比較先・metrics を伴わない固定の質検査結果 |
| `family_redirect` | `family_redirect` | 固定の同族判定結果 |
| その他 | `unclassified` | fail closed |

表示する `outcomes` は上表の公開値順とし、ゼロ件の label は省略する。`attempts` は archive 行数であり、`sum(outcomes.values()) == attempts` を常に満たす。同じ mission 内で候補を修正して別 archive 行になったものは別試行である。mission 数や backlog の `attempts` とは混ぜない。

### 2.3 family key と日付の扱い

- 現行の family key 生成規則で再解釈でき、UTF-8 で 512 bytes 以下の key だけを既知の族として扱う。Markdown へ出すときは cell を escape する。
- 空、NULL、壊れた形式、上限超過の key は推測で補完しない。すべてを `族不明` という**1 行**に集約する。この行は一つの巨大な族を意味せず、「過去行を族へ帰属できない」という coverage 表示である。
- `族不明` の `attempts`、結果件数、最終試行日は集約するが、`deployed` は `no`、`deployed_names` は空とする。配備済み plugin の key 不在とは混ぜない。
- `created_at` は ISO datetime として解釈し、aware 値は UTC に直して `YYYY-MM-DD` だけを出す。naive 値は既存保存規約に合わせ UTC とみなす。解釈できない値は生値を出さず、その行を回数・結果には数えたうえで日付計算から外す。有効日が一つもなければ `None` とする。

依頼時に示された空 key 24 行は、この規則により削除されず `族不明` 1 行として見える。古い行を別々の族へ推測分類しない。

### 2.4 配備済み族との統合

親は inventory の plugin 構成から、質検査と同じ共有関数で family key を得る。context 専用に key 算出を再実装しない。

- archive と inventory が同じ key を持つ場合は 1 行に merge し、`deployed=yes` と配備中の canonical plugin 名を付ける。
- archive がなくても key を持つ配備済み族は `attempts=0` の 1 行を作る。
- `family_redirect` の archive は redirect 先と同じ key の結果件数へ足す。redirect 専用の別行を作らない。
- key を持たない配備済み plugin、特に依存 indicator のない strategy は `族不明` の archive 行へ混ぜない。「family key なしの配備済み plugin」という残余一覧へ、現行表示と同じ canonical な name / kind / timeframe / pairs / indicators だけを一度だけ出す。params、hash、成績は出さない。
- 既存の全件版「配備済みの仮説族」節は削除する。key を持つ plugin は統合行、key を持たない plugin は残余一覧のどちらか一方にだけ現れる。

`improvement_history.recent_runs` に残る `observation:family_redirect:<name>` は mission の時系列結果であり、族一覧の重複ではない。この既存履歴は変更しない。

### 2.5 列、順序、上限

prompt の表は次の 5 列に固定する。

| family_key | attempts | outcomes | last_attempt_on | deployed |
|---|---:|---|---|---|
| `strategy:rsi,sma@1h@USDJPY` | 4 | `not_submitted=3, rejected_by_human=1` | `2026-10-18` | `yes (rsi_sma_strategy)` |
| `族不明` | 24 | `not_submitted=20, unclassified=4` | `2026-09-30` | `no` |

例の数値は形式説明であり、DB 実値の主張ではない。

並びは次で固定する。

1. key を持つ配備済み族を `family_key` 昇順。
2. key を持たない配備済み plugin を `kind, name` 昇順で残余一覧へ出す。
3. 非配備の既知族を `attempts` 降順、`last_attempt_on` 降順、`family_key` 昇順。
4. `族不明` を最後に出す。

表示上限は、表と残余一覧を合わせて 50 entry、節全体で UTF-8 16 KiB とする。`族不明` がある場合はその 1 entry を先に予約する。残りは上の順序で、完成した 1 行ずつ byte 上限内に入るものだけを追加し、行の途中では切らない。省略した entry 数を固定文 `他 N 件を省略` で出す。配備済み entry を先に置くため、上限時も別名重複を避ける情報を優先する。

### 2.6 agent への読み方

prompt の節本文は次を基準文面とする。

> この表は候補の順位表でも、成績表でもありません。`attempts=0` の族を機械的に順番に消費せず、配備済みとの違い、すでに試した族の広がりと反復回数、今回の根拠と検証可能性を合わせて仮説を選んでください。1 回の非提出・却下を族全体の否定とみなさず、同じ族を再試行するなら前回と異なる仮説上の理由を説明してください。`族不明` は旧行を一族にまとめた意味ではなく、帰属不能件数です。`deployed=yes` の族は別名の新規候補にせず、表示された配備名の更新として扱ってください。

この文面により、意思決定を「未試行の族を順に消費」から「消費済み族の多さと偏りを踏まえた仮説選択」へ変える。結果件数だけで良否や優先順位を計算するようには指示しない。

## 3. 遮断との整合

### 3.1 読み取り面を狭くする

族別要約用の store 読み取りは、次の意味の SQL に限定する。

```sql
SELECT family_key, outcome_class, created_at
FROM candidate_archives
ORDER BY id
```

SQL で `metrics_json` を集計してから捨てる方法も採らない。不要列を Python process へ持ち込まないことで、renderer の変更だけで漏洩する余地を減らす。`approval_requests`、`backtest_runs`、activity、人間向け log は参照しない。

### 3.2 fail-closed と自由文の不在

- outcome は §2.2 の dict lookup で投影し、`.get(raw, "unclassified")` 相当とする。
- family key は正準形を再検証し、解釈できなければ `族不明` にする。
- date は parse できた日付だけを出し、壊れた文字列をそのまま出さない。
- plugin 名は既存の canonical name 検証を通った inventory 値だけを出す。
- `quality_flags`、`last_result`、reason、metrics の値を表示 row に受け取る欄そのものを作らない。

これは `project_last_result` が未知の自由文を `unclassified` に畳む方針と同じである。denylist で危険語だけを除くのではなく、公開 schema を allowlist とする。

### 3.3 既存 pin と変更点

| pin | 現在守っていること | 本 spec での扱い |
|---|---|---|
| `tests/loops/test_improve_context.py` | top-level section、直近 note 20 件、`project_last_result` の固定語彙、mission 診断から数値成績・path を落とすこと、redirect 名を配備済み名に限定すること | 維持して族別型・上限・並び・空 key・未知 outcome の pin を追加する。note の「最新 20 id 全件」期待値だけは §6 の dedup 契約へ更新する |
| `tests/loops/test_floor_leak_guard.py` | 人間 reject 理由が履歴・rendered prompt に出ず、人間向け台帳には残ること | そのまま維持し、族別要約を含む全文を観測する |
| `tests/loops/test_improve_agent_surface.py` | approval payload の holdout 値、archive metrics、hash、候補名、activity canary が context/prompt に出ないこと。改善 tool 名が exact allowlist であること | canary pin は維持する。`candidate_archives` import 禁止と「候補履歴節そのものが無い」という旧 pin は、安全な 3 列 projection と exact 公開 schema の pin に置き換える |
| `tests/loops/test_improve_forbidden.py` | `IMPROVE_FORBIDDEN == {get_signals, bless}` と改善系 Mission tools との非交差 | 一切緩めない |
| `tests/tools/test_signal_tools.py` | `get_signals` の lookback、pair、status、正確な返却 key 集合、strategy 行への `in_sample_metrics` 添付 | 一切変えない。むしろ既存 tool を改善へ流用できない根拠として維持する |
| `tests/tools/test_mission_registry.py` | improve registry に人間 log 読取経路がなく、許可 tool 集合が固定されること | exact allowlist に新 tool を足さず維持する |
| `tests/tools/test_improve_staging_tools.py` | 配備済み view に成績・期間・段の field 名がなく、自由な `params` 名前空間と区別されること | 一切緩めない |
| `tests/tools/test_improve_rpc_tools.py` | `_strip_forbidden` が期間・日時 key を再帰的に剥がし、agent 応答と private ledger を分離すること | 一切変えない。族別要約は RPC 経路を使わない |

既存 `test_ac13b_improve_context_does_not_import_candidate_archives` と `test_ac13b_prompt_has_no_candidate_history_section` は、新要件と直接矛盾するため削除だけにはしない。「store API が返す key 集合が 3 列と完全一致する」「rendered family row の key 集合が §2.1 と完全一致する」「archive に植えた metric/reason/hash/name canary が全文にない」という、より狭い読取面の pin に置換する。

## 4. `get_signals` 不在の扱い

### 4.1 比較

| 案 | 内容 | 利点 | リスク・費用 |
|---|---|---|---|
| (a) prompt で禁止と理由を明示 | `get_signals` は取引判断 loop 専用で改善では使えず、配備済み strategy の直近方向を改善材料にせず、不在を note にしないと書く | 現在の遮断を保つ。新しい DB / tool / payload 面がない。反復の原因である「設計上の不在が説明されていない」を直接直す | prompt 規律なので agent が無視する可能性はゼロではない。AC で文面と tool allowlist の両方を見る必要がある |
| (b) 方向専用 tool を追加 | `get_deployed_directions` 等で成績を除いた直近方向だけを返す | agent が求めている情報を直接与えられる | signals は direction だけでなく status、鮮度、足、payload、配備操作との link を持つ。安全な projection、件数・期間上限、stale/consumed の意味、pair 横断、壊れた payload の扱いが新たに必要になる。現在相場の方向を改善仮説へ混ぜる短期バイアスも生む。新しい情報経路が遮断面と pin を増やす |

### 4.2 採用

案 (a) を採用する。

族別履歴の目的は、過去にどの仮説族をどれだけ試したかを次周へ渡すことであり、現在の signal 方向はその問いに必要ない。既存 `get_signals` は strategy 行に in-sample 成績を添えるため、そのまま改善へ許可できない。方向だけの派生 tool を作ると、成績を除いても新しい時系列情報経路と状態解釈が増える。観測された失敗は tool 不足ではなく、意図的な不在が prompt に伝わっていないことなので、より小さく原因へ効く案 (a) とする。

prompt の規律へ次の基準文面を追加する。

> `get_signals` は取引判断 loop 専用で、この改善 mission では利用できません。配備済み strategy の直近 signal の方向・status・payload は、仮説の選択や候補評価の材料にしないでください。この不在は設計上の制約なので、課題・note・discovery として記録せず、候補自体は許可された `run_backtest` と self-test で検証してください。

案 (b) と、既存 `get_signals` の改善 allowlist への追加は本 spec の非目標である。

## 5. prompt 内の配置

「改善履歴」の直後、「現行構成」の前に「仮説族の試行状況」を置く。agent が選択前に履歴の時系列と族別の集約を続けて読めるためである。旧「配備済みの仮説族」節は削除し、内容は §2.4 の統合表示へ移す。

「直近の成績レポート」は現状の位置と内容を変えない。族別表をその節へ入れず、候補成績と実運用の成績を一つの表にしない。

`get_signals` の基準文面は「規律」に置く。tool 一覧を読む時点で禁止と代替検証手段を同時に見せる。

## 6. note の重複

同文 note が現在の表示枠を占有しているため、これは本 spec の範囲に含める。ただし DB 行の削除・更新や書込み時の統合はせず、context 表示だけを最小変更する。

1. note 全件を従来どおり id の新しい順にする。
2. 先頭 20 件を raw window とする。21 件目以降を補充候補にしない。
3. raw window 内を `idea_norm` で newest-wins にする。`idea_norm` が欠けた旧行だけ `idea.strip().lower()` を fallback に使う。
4. 同じ key の 2 件目以降を表示しない。異なる本文はまとめない。
5. `notes_omitted` は従来どおり raw window の外にある件数、`notes_deduplicated` は raw window 内で畳んだ件数とする。prompt は必要なときだけ固定文 `同文 note N 件を省略` を出す。

この規則は既存の「直近 20 件」という時間窓を広げない。古い note を掘り起こして context 量を増やさず、現在の重複だけを除く。今後の同文生成は §4.2 の prompt 文面で止め、表示側は既存行と逸脱時の防波堤になる。

## 7. 受入条件と変異

### 7.1 受入条件

| AC | 条件 | 観測点 |
|---|---|---|
| AC-1 | store の族別履歴用読取は `family_key` / `outcome_class` / `created_at` だけを返し、metrics、候補名、hash、path、pair、flags を process へ返さない | store 戻り値の exact key set、SQL trace または spy |
| AC-2 | 正準 key ごとに archive 行が集約され、`attempts == sum(outcomes.values())`。同 mission の別 archive 行は別試行として数える | context の構造化値 |
| AC-3 | NULL / 空 / 不正 / 長過ぎる key は削除も逐語表示もされず、`族不明` 1 行へ集約される。依頼時の 24 旧行に相当する fixture も 1 行になる | row 数、`族不明.attempts`、rendered prompt |
| AC-4 | `unprofitable` と `insufficient_trades` は `not_submitted` に畳まれ、未知 outcome は必ず `unclassified` になる。生値は prompt にない | context と prompt の canary |
| AC-5 | key を持つ配備済み族は archive と 1 行に merge され、未試行なら 0 回で出る。`family_redirect` はその行に数えられる。key なし配備済み plugin は構成を保った残余一覧に一度だけ出る | family/plugin 名の出現回数と構造化値 |
| AC-6 | 順序は §2.5 のとおりで、最大 50 entry、節全体 16 KiB 以下。超過時は完全な行だけを出し、省略件数が一致する | UTF-8 byte 数、entry 数、先頭/末尾 row、`entries_omitted` |
| AC-7 | 表は 5 列で、結果 label は固定順、日付は `YYYY-MM-DD`、不正日時の生文字列は出ない | rendered table 全文 |
| AC-8 | metric canary を `metrics_json` に植えても context / prompt に値も metric 列も現れない。PF、`avg_r`、取引数の各 canary を別値にする | positive control の DB 行、context JSON、rendered prompt 全文 |
| AC-9 | 人間 reject reason canary は `approval_requests.reason` に残るが、族別要約を含む context / prompt には現れない | DB reason と rendered prompt 全文 |
| AC-10 | `get_signals` の基準文面が prompt にあり、改善 registry の exact tool set と `IMPROVE_FORBIDDEN` は変更前と同じである | rendered prompt、registry names、禁止集合 |
| AC-11 | raw 最新 20 note の中で同じ `idea_norm` は newest 1 件だけ表示され、窓外から補充しない。`notes_omitted` と `notes_deduplicated` がそれぞれ一致する | 表示 id 列と 2 count |
| AC-12 | 既存 `project_last_result`、`_MISSION_DIAGNOSTIC`、RPC strip、staging view、signal tool の遮断 pin が green のままである | §3.3 の既存 test 群 |
| AC-13 | 改善 mission の cadence、slot 数、tool budget に差分がない | 設定 diff と対象ファイル diff |

### 7.2 殺すべき変異

| 変異 | 変異内容 | red にする pin |
|---|---|---|
| M-1 | family row に `pf`、`avg_r`、`trades`、`metrics`、score のどれか 1 列を混ぜる、または `metrics_json` を select する | AC-1 の exact key set + AC-8 の値 canary / exact公開 schema |
| M-2 | `approval_requests.reason` を join する、または reject reason を outcome 表示へ足す | AC-9 の reason canary |
| M-3 | 未知 outcome をそのまま返す、空にする、または `not_submitted` にする | AC-4 の `unclassified` exact assert |
| M-4 | `unprofitable` / `insufficient_trades` を独立 label で出す | AC-4 の公開 label 集合 |
| M-5 | 空 key 行を集計外にする、行ごとに出す、または既知族へ推測分類する | AC-3 の件数と 1 行 assert |
| M-6 | archive 行数でなく distinct mission 数を `attempts` にする、または結果件数と試行回数を別集計する | AC-2 の同 mission 複数 archive fixture と総和 invariant |
| M-7 | 配備済み一覧を旧節にも残す、redirect を別行にする、key なし plugin を `族不明` archive と merge する | AC-5 の出現回数と行分類 |
| M-8 | entry 上限、byte 上限、固定順のいずれかを外す | AC-6 の上限境界 fixture |
| M-9 | date の時刻や壊れた原文を出す | AC-7 の型・canary |
| M-10 | `get_signals` を improve registry に足す、禁止集合から外す、または基準文面を削る | AC-10 の exact allowlist / prompt assert |
| M-11 | note dedup を外す、20 件を unique になるまで過去へ広げる、oldest-wins にする | AC-11 の id 列と count |

M-1 と M-2 は必須の漏洩変異であり、単なる禁止語検索ではなく、DB に実在する別々の canary 値を positive control としてから agent 面の全文で不在を確認する。

## 8. 確認事項

追加のユーザー裁定が必要な事項はない。次は本 spec 内で技術的に確定した。

- `get_signals` は案 (a) を採用し、新 tool は作らない。
- 空 family key は集計外にせず `族不明` 1 行にする。
- note は DB を変更せず、最新 20 件の窓内だけを重複排除する。
- 表の上限、順序、outcome の安全な畳み方は遮断と既存データ構造から決めた。

## 9. 実装範囲、残余リスク、後続

### 9.1 task

| task | 内容 | 規模 | 性質 |
|---|---|---:|---|
| T1 | archive の 3 列 projection、既存規則を共有した family key 照合、族別集計、配備済み族との merge、bounded render、漏洩・変異 pin | 中 | 純 Python + prompt Markdown。schema migration なし |
| T2 | `get_signals` 不在の基準文面、最新 20 note 内の newest-wins dedup、exact allowlist と note 観測点 | 小 | 純 Python + prompt Markdown。tool 追加なし |

### 9.2 変更ファイル

実装時に変更を見込むファイルは次のとおり。

- `src/agentic_fx/store/candidate_archives.py`: 3 列だけの read API。
- `src/agentic_fx/plugin/logic_identity.py`: 保存候補と inventory が同じ family key 規則を共有するための純関数整理。key の意味は変えない。
- `src/agentic_fx/loops/improve_context.py`: outcome 投影、族別集計、配備との merge、note dedup。
- `src/agentic_fx/loops/improve_loop.py`: bounded family table と dedup 件数の render。
- `src/agentic_fx/loops/prompts/improve_mission.md`: 節の置換、読み方、`get_signals` 不在の基準文面。
- `tests/loops/test_improve_context.py`: 構造、集計、順序、上限、日付、note の pin。
- `tests/loops/test_improve_agent_surface.py`: archive canary と exact projection。新要件と矛盾する旧 negative pin の置換。
- `tests/loops/test_floor_leak_guard.py`: 族別要約を通した人間 reason 非漏洩。
- 必要なら `tests/loops/test_improve_prompt_objective.py`: prompt 基準文面の exact pin。

`src/agentic_fx/tools/signal_tools.py`、`src/agentic_fx/tools/improve_rpc_tools.py`、`src/agentic_fx/tools/mission_registry.py` と `tests/tools/` は変更対象ではない。既存 pin を実行して「緩めていない」ことを確認する対象である。

### 9.3 残余リスク

- family key はパラメータ値を含まないため、同じ族内の仮説差を表さない。これは同族を更新扱いにする現行設計と同じである。
- 旧行の `族不明` は coverage を見せるだけで、過去の 24 行を真の族へ復元しない。元 archive が残っていても、本 spec は backfill を行わない。
- entry / byte 上限を超えた古い非配備族は省略される。省略件数は見えるが詳細は見えない。
- prompt 指示は agent の反復を確率的に抑えるもので、書込み時に同文 note を拒否する制約ではない。表示 dedup が逸脱時の影響を限定する。
- 依頼時の DB 総行数と outcome 内訳には 4 件の不一致がある。実装の正しさはその固定値ではなく、投影前後の総和 invariant と read-only 実測で判断する。

### 9.4 後続

in-sample 成績の族別順序尺度は、holdout・期間・標本数・人間判断の漏洩境界を別 spec で裁定する。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-24 | v0.1 | 初版。族別試行要約、fail-closed 投影、配備済み族との統合、`get_signals` 不在の案 (a)、note 窓内 dedup、AC と変異を定義 | 2 チケットを同じ改善 feedback 境界として設計するため | — (未 commit) |
