# 改善ループに渡す履歴の情報構造 設計書

**版:** v0.2
**日付:** 2026-10-24  
**対象チケット:** `improve-context-family-attempt-summary`、`improve-deployed-signal-lookup-gap`  
**状態:** 設計案

## 0. 目的、範囲、非目標

改善 agent が次の mission で仮説を選ぶとき、過去の候補を一件ずつ眺めて「未試行らしい族を順に消費する」のではなく、仮説族ごとの試行量と終わり方、配備との重なりを把握できるようにする。同時に、取引判断 loop 専用の `get_signals` を改善 mission でも探し続ける無駄と、それを申し送る note の反復を止める。

この変更は改善の**頻度や周回数を変えない**。変えるのは次周へ渡る情報だけである。

範囲は次のとおり。

- `candidate_archives` から、仮説族 key、試行回数、安全な結果区分ごとの件数、最終試行月だけを親が集計し、改善 prompt に渡す。
- 配備済み族を同じ表示へ統合し、同じ族を履歴表と配備済み一覧へ二重に出さない。
- `get_signals` が改善では利用できず、直近 signal の方向を改善材料にしないこと、過去の note にある signal 照合の要望が解消済みの制約であることを prompt に明記する。

非目標は次のとおり。

- 候補 archive の `metrics_json`、PF、`avg_r`、取引数、勝率、drawdown、期間、順位、score その他の成績を族別履歴へ出さない。
- `approval_requests.reason` その他の人間による reject 理由を出さない。
- `quality_flags`、候補名、hash、archive path、pair 単体、mission transcript を族別履歴へ出さない。配備されていない indicator 候補の名前は、族 key の一部であっても出さない (§2.3)。
- 改善側に `get_deployed_directions` などの新しい signal 読み取り tool を追加しない。既存 `get_signals` も許可しない。
- cadence、並列数、tool budget、backtest 回数、床、承認手順を変えない。
- backlog note の表示窓、並び、DB 上の note を変えない。note のあいまい一致による畳み込みは入れない (§6)。
- 現在の「直近の成績レポート」は実運用側の既存情報面であり、本 spec では変更しない。ただし、候補 archive や backtest の成績をこの節へ迂回して混ぜることも許さない。
- in-sample 成績による族別の順序尺度は扱わない。これは遮断境界を別途決める後続事項である (§9.4)。

## 1. 現状と守る境界

### 1.1 現物から確認できること

| 面 | 現状 | この設計での扱い |
|---|---|---|
| 改善 context | `build_improve_context` は成績レポート、直近 50 mission の改善履歴、現行構成、backlog、方針、参照を組み立てる。`candidate_archives` は読まない | 安全な明示 projection だけを新しい族別要約へ足す |
| `last_result` | `project_last_result` は既知値を固定語彙へ畳み、未知値は `unclassified` にする。`_MISSION_DIAGNOSTIC` は呼出回数だけを残し、理由・成績・archive 位置を捨てる | 同じ fail-closed 原則を `outcome_class` に適用する |
| 質検査の agent 面 | `_quality_rejection` は `duplicate_logic` / `duplicate_metrics` を agent 面の `duplicate` に、それ以外を `not_submitted` に畳む。比較不能 (`_quality_unavailable`、`history_source_unconfigured`、`backtest_data_unavailable`) は `outcome_class="gate_failed"` で記録し、agent 面には `not_submitted` を出す | 族別要約も同じ畳み方にする (§2.2)。archive 上の `gate_failed` は比較不能とコード gate 不合格を区別できないので `not_submitted` に畳む |
| backlog note | `_backlog_section` は全 note を新しい順にし、先頭 20 件を表示する。書込み側の `_upsert_backlog_idea` / `_select_and_bind` / `upsert_system_note` は `idea_norm` で重複を排除している | 変えない (§6) |
| prompt | 「改善履歴」「現行構成」「配備済みの仮説族」「バックログと既知の事実」を別々に表示する | 「配備済みの仮説族」の prompt 節を族別試行状況へ統合する |
| 仮説族 | `family_key` は、strategy なら依存 indicator 名の集合 + 足 + pair 集合、indicator なら名前 + 足。依存 indicator がない strategy は key を持たない。key を parse する関数はなく、生成だけがある | 生成規則を 1 本の純関数にし、文法と再生成による byte 一致で既知族を判定する (§2.3) |
| 候補結果 | `outcome_class` は 14 値の CHECK を持つ。`quality_flags` は `family_detected` / `legacy_unlinked` の検出属性 | 結果は安全な固定語彙へ投影する。flag は出さない |
| 同族判定 | `find_same_family` は配備済みの同 kind・別名・同 family key を `family_redirect` の対象にする。duplicate 判定は別のロジックである | `family_redirect` は該当する配備済み族と同じ要約行の `not_submitted` に数える |
| signal tool | `get_signals` は strategy 行に `in_sample_metrics` も添える。`IMPROVE_FORBIDDEN` は `get_signals` と `bless` を改善から除外する | 遮断を維持し、prompt で意図を説明する |
| 改善 RPC | `run_backtest` / `analyze_corr` の agent 応答は `_strip_forbidden` が期間端点などを再帰的に剥がす。改善 tooldef はこの 2 tool を含む | tool の追加も返却 schema の拡張もしない |
| 時刻 | context の `recent_runs` は `started_at` / `finished_at` を持つが、`_history_table` は描画時に落とす。rendered prompt に wall-clock の日付は出ていない | 族別要約の最終試行だけを月単位で出す (§2.3、§3.3) |

本番 DB の read-only 実測 (2026-10-24) は次のとおりである。

- `candidate_archives` は 46 行で、`family_key` を持つ行が 22 行、空の行が 24 行である。空の行は、依存 indicator を持たない strategy (現行規則で key が None) と、`archive_path` が NULL のため backfill の対象外だった旧行である。§2.3 の文法検査で既知族から外れる行が増えうるので、既知族へ帰属する行は 22 行以下になる。
- note は 78 件で、`idea_norm` も 78 種類ある。完全一致の重複は 0 件で、`get_signals` 系の言い換え (語順・句読点・語の違い) が 10 件ある。

依頼時に示された結果件数の内訳は合計が総行数と一致しなかったので使わない。本設計は件数を定数や AC の期待値にせず、集計規則自体は件数に依存しない。

### 1.2 不変条件

1. 親だけが DB を読み、worker へは完成済みの投影を prompt として渡す。worker に DB path や履歴読取 tool を与えない。
2. `candidate_archives` から読む列は `family_key`、`outcome_class`、`created_at` の 3 列だけである。`SELECT *` を使わず、metrics、名前、hash、path、pair、flags を取得しない。改善 context から呼べる archive 読取りは、この 3 列を返す関数 1 つだけである。
3. 人間の reject 理由を持つ表とは join しない。
4. identifier 以外の文字列は固定語彙だけにする。identifier は §2.3 の文法を満たし、文法値から再生成した値と byte 一致したものだけである。自由文をこの要約へ入れない。
5. 族別要約の結果は成績の良し悪しを意味しない。特に `rejected_by_human` は理由を含まず、`not_submitted` は非提出だった事実しか表さない。

## 2. 採用する形

親は archive の安全な 3 列を読み、配備時点の inventory と合わせ、`improvement_history.family_attempts` を作る。prompt では既存の「配備済みの仮説族」節を置き換え、「仮説族の試行状況」1 節として表示する。

### 2.1 公開型

Python の型としては次を契約とする。`CanonicalFamilyKey`、`CanonicalPluginName`、`CanonicalPair` は自由文ではなく、§2.3 の文法を通った bounded identifier である。

```python
PublicOutcome = Literal[
    "not_submitted", "duplicate", "pending", "approved",
    "rejected_by_human", "expired", "invalidated", "revoked",
    "unclassified",
]

FamilyLabel = (
    CanonicalFamilyKey               # 配備済み名だけで書ける key
    | UndeployedIndicatorLabel       # "indicator (未配備) @<tf>"
    | Literal["族不明"]
)

class FamilyAttemptSummary(TypedDict):
    family_key: FamilyLabel
    attempts: int                         # 0 以上
    outcomes: dict[PublicOutcome, int]    # 非ゼロだけ、表示順は固定
    last_attempt_month: YearMonth | None  # YYYY-MM、日・時刻なし
    deployed: Literal["yes", "no"]
    deployed_names: list[CanonicalPluginName]

class UnkeyedDeployedSummary(TypedDict):
    name: CanonicalPluginName
    kind: Literal["indicator", "signal", "strategy"]
    timeframe: CanonicalTimeframe | None
    pairs: list[CanonicalPair | Literal["不明"]]
    indicators: list[CanonicalPluginName]

class FamilyAttemptSection(TypedDict):
    rows: list[FamilyAttemptSummary]
    unkeyed_deployed: list[UnkeyedDeployedSummary]
    entries_omitted: int
```

外へ出る可変値は bounded identifier、非負整数、年月だけである。結果 label、kind、`deployed`、`未配備指標`、`不明` は固定語彙である。`None` は prompt で固定記号 `-` として表示する。`outcomes` が空の配備済み未試行族も `-` とする。`UnkeyedDeployedSummary` は現行の配備済み族表示がすでに持つ構成情報を縮退させずに移すための型であり、自由文や params、hash、成績は持たない。`pairs` の各要素は §2.3 の pair 文法を通ったものだけで、通らない要素は固定語 `不明` に置き換える。

context の `current_inventory.deployed_families` は残す。`build_improve_context` はこの名前集合から `deployed_names` を作り、`project_last_result` の redirect 名 allowlist に渡している。置き換えるのは prompt の「配備済みの仮説族」節だけである。

### 2.2 結果区分の投影

入力 `outcome_class` は次の表だけを通す。表にない値、文字列でない値、空文字は `unclassified` とする。入力をそのまま fallback にしない。公開語彙は既存の agent 面 (`_quality_rejection` と `project_last_result`) の畳み方に揃え、そこで出していない区別を族単位で復元させない。

| 保存値 | 公開値 | 理由 |
|---|---|---|
| `not_submitted` | `not_submitted` | 非提出の事実だけ |
| `unprofitable` | `not_submitted` | 成績による非提出との区別を渡さない |
| `insufficient_trades` | `not_submitted` | 取引数による非提出との区別を渡さない |
| `gate_failed` | `not_submitted` | 比較不能経路とコード gate 不合格が同じ値で記録されるので区別できない。既存の agent 面と同じく非提出に畳む (fail closed) |
| `noop_copy` | `not_submitted` | 質検査の非提出。`_quality_rejection` と同じ畳み方 |
| `family_redirect` | `not_submitted` | 質検査の非提出。誘導先は `deployed=yes` の同じ行として見える |
| `duplicate_logic` | `duplicate` | 既存の agent 面と同じく 1 語にする |
| `duplicate_metrics` | `duplicate` | 成績一致による区別を渡さない |
| `pending` | `pending` | 承認待ちの固定状態。既存の `last_result` 面の `approval_pending` と同じ情報 |
| `approved` | `approved` | 固定のライフサイクル状態 |
| `rejected_by_human` | `rejected_by_human` | 理由を伴わない固定状態 |
| `expired` | `expired` | 固定のライフサイクル状態。既存の `last_result` 面に同じ語がある |
| `invalidated` | `invalidated` | 固定のライフサイクル状態。既存の `last_result` 面に同じ語がある |
| `revoked` | `revoked` | 固定のライフサイクル状態 |
| その他 | `unclassified` | fail closed |

これにより、`unprofitable` / `insufficient_trades` / 比較不能 (`gate_failed` で記録) の 3 内部状態は、族行でも同じ `not_submitted` に見える。表示する `outcomes` は公開値の宣言順とし、ゼロ件の label は省略する。`attempts` は archive 行数であり、`sum(outcomes.values()) == attempts` を常に満たす。同じ mission 内で候補を修正して別 archive 行になったものは別試行である。mission 数や backlog の `attempts` とは混ぜない。

### 2.3 family key、候補名、日付の扱い

**生成規則の一本化。** `logic_identity.py` に `family_key_from_parts(kind, name, timeframe, pairs, indicator_names)` を置き、保存側の `_family_key(config)` も配備側の meta 経路もこの関数を通す。

- timeframe が None のときは `"1h"` とする。現行の `_family_key` は key が無い config を `"1h"` で補う一方、loader が受け付ける indicator の `timeframe: null` を `"@None"` にしていた。meta 経路では両者を区別できないので、None を `"1h"` に揃える。key の値が変わるのはこの明示 null の indicator だけである。過去の archive に残る `@None` の key は文法を満たさないので `族不明` に入り、書き換えはしない。
- pairs と indicator_names は `sorted(set(str(x)))` にする。meta の宣言順や重複は key に影響しない。
- 配備済み plugin ごとに、meta から作った key と `read_identity(plugin_dir)` が返す key が byte 一致することを pin する (AC-17)。

**文法。** 既知族として扱う key は次の文法を満たすものだけである。

- `indicator:<name>@<tf>`
- `strategy:<name>(,<name>)*@<tf>@(<pair>(,<pair>)*)?`
- `<name>` は `^[a-z][a-z0-9_]{0,63}$` を満たし、strategy の name 列は昇順で重複がない。
- `<tf>` は `PLUGIN_TIMEFRAMES` の要素である。
- `<pair>` は `settings.pairs` の要素で、pair 列は昇順で重複がない。strategy の pairs は承認時に `settings.pairs` との照合を通っているので、所属で判定する。設定から外した pair を含む過去行は `族不明` に入るが、これは許容する。空の pair 列は生成規則が出しうる形で、自由文を含まないので文法に含める。
- key 全体は UTF-8 で 512 bytes 以下である。

判定は「保存された key を文法で分解し、分解した値を `family_key_from_parts` に渡して再生成し、元の key と byte 一致したものだけを既知族とする」で行う。分解できない、再生成値と一致しない、上限を超える key は推測で補完せず、すべて `族不明` という**1 行**に集約する。改行、`|`、日本語、数値と自由文を含む pair はここで落ちる。

**候補名の投影。** 文法検査は保存値に対してだけ行い、その後で表示用の名前置換をする。手順は次の順に固定する。

1. 保存 key を文法で分解し、再生成して byte 一致を確かめる。外れたら `族不明`。
2. 配備済み indicator 名の集合 D (inventory の canonical 名) を作る。
3. indicator key は、name が D にあれば key をそのまま使う。D に無ければ名前を落として `indicator (未配備) @<tf>` にする。
4. strategy key は、依存 indicator 名のうち D に無いものをすべて固定語 `未配備指標` 1 個に置き換え、D にある名前の昇順の後ろに置く。足と pair 列はそのまま残す。
5. 置換後の表示 key が同じになった行を 1 行に merge する。`attempts` と結果件数は合計し、最終試行月は最大値をとる。

`indicator (未配備) @<tf>` と `未配備指標` は表示用の固定語で、name の文法を満たさない。文法は手順 1 の保存値にだけ当てる。この投影により、名前がそのまま出る indicator は配備済みのもの (現行構成の節にすでに名前が出ているもの) だけになる。indicator 候補は名前を変えるたびに別 key になり、非配備のものを名前ごとに並べると候補名の一覧になるので、足ごとの件数と結果内訳だけを残す。

**日付。** `created_at` は ISO datetime として解釈し、aware 値は UTC に直して `YYYY-MM` だけを出す。naive 値は既存保存規約に合わせ UTC とみなす。解釈できない値は生値を出さず、その行を回数・結果には数えたうえで月計算から外す。有効な値が一つもなければ `None` とする。

月単位にする根拠は次のとおりである。最終試行の時期を渡すことはユーザー裁定で決まっている。一方、holdout は直近 K ヶ月の固定分割で、エージェント向け API は期間も端点も返さない (遮断 7)。ハーネスの system prompt や web_search の記事日付ですでに現在日はおおよそ見えるので、月単位の最終試行が足す情報は小さい。日単位にすると族ごとの試行日を並べて mission 周期と照合でき、時刻アンカーとしての精度が上がる。裁定の「いつ最後に試したか」は月単位で足りるので、より粗い方を採る。`started_at` / `finished_at` など他の時刻アンカーは族別要約に出さない。

依頼時に示された空 key 24 行は、この規則により削除されず `族不明` 1 行として見える。古い行を別々の族へ推測分類しない。`族不明` の `deployed` は `no`、`deployed_names` は空とする。配備済み plugin の key 不在とは混ぜない。

### 2.4 配備済み族との統合

親は inventory の meta から `family_key_from_parts` で配備側の key を得る。context 専用に key 算出を再実装しない。配備側の key にも §2.3 の候補名投影を同じ規則で当てる (配備済み strategy の依存 indicator は通常すべて D にあるので、key はそのまま残る)。

- archive と inventory が同じ表示 key を持つ場合は 1 行に merge し、`deployed=yes` と配備中の canonical plugin 名を付ける。
- archive がなくても key を持つ配備済み族は `attempts=0` の 1 行を作る。
- `family_redirect` の archive は redirect 先と同じ key を持つので、その行の `not_submitted` に数える。redirect 専用の別行を作らない。
- 配備後に config を変えて key が変わった場合、変更前の key を持つ archive 行は非配備族の行に移る。これは key の定義どおりの帰結である。
- key を持たない配備済み plugin、特に依存 indicator のない strategy は `族不明` の archive 行へ混ぜない。「family key なしの配備済み plugin」という残余一覧へ、現行表示と同じ canonical な name / kind / timeframe / pairs / indicators だけを一度だけ出す。kind は `indicator` / `signal` / `strategy` をそのまま出す。params、hash、成績は出さない。
- prompt の「配備済みの仮説族」節は削除する。key を持つ plugin は統合行、key を持たない plugin は残余一覧のどちらか一方にだけ現れる。

`improvement_history.recent_runs` に残る `observation:family_redirect:<name>` は mission の時系列結果であり、族一覧の重複ではない。この既存履歴は変更しない。

### 2.5 列、順序、上限

prompt の表は次の 5 列に固定する。

| family_key | attempts | outcomes | last_attempt_month | deployed |
|---|---:|---|---|---|
| `strategy:rsi,sma@1h@USDJPY` | 4 | `not_submitted=3, rejected_by_human=1` | `2026-10` | `yes (rsi_sma_strategy)` |
| `indicator (未配備) @1h` | 6 | `not_submitted=5, duplicate=1` | `2026-10` | `no` |
| `族不明` | 24 | `not_submitted=20, unclassified=4` | `2026-09` | `no` |

例の数値は形式説明であり、DB 実値の主張ではない。

`deployed` 列は、`deployed=no` なら `no`、`deployed=yes` なら `yes (<名前>)` とする。名前は `deployed_names` の昇順で `, ` 区切り、最大 5 件とし、6 件目以降は `他 N` を付ける。名前は §2.3 の name 文法を満たす配備済み名だけである。最終列が `deployed` なので、行末は `| no |` か `| yes (...) |` になり、既存の履歴表・backlog 表の行末 (`| observation:not_submitted |`、`| not_submitted |`) と同じ形にならない。

並びは次で固定する。

1. key を持つ配備済み族を `family_key` 昇順。
2. key を持たない配備済み plugin を `kind, name` 昇順で残余一覧へ出す。
3. 非配備の既知族 (`indicator (未配備) @<tf>` を含む) を `attempts` 降順、`last_attempt_month` 降順、`family_key` 昇順。
4. `族不明` を最後に出す。

表示上限は、表と残余一覧を合わせて 50 entry、節全体で UTF-8 16 KiB とする。`族不明` がある場合はその 1 entry を先に予約する。残りは上の順序で、完成した 1 行ずつ byte 上限内に入るものだけを追加し、行の途中では切らない。省略した entry 数を固定文 `他 N 件を省略` で出す。配備済み entry を先に置くため、上限時も別名重複を避ける情報を優先する。

### 2.6 agent への読み方

prompt の節本文は次を基準文面とする。

> この表は候補の順位表でも、成績表でもありません。`attempts=0` の族を機械的に順番に消費せず、配備済みとの違い、すでに試した族の広がりと反復回数、今回の根拠と検証可能性を合わせて仮説を選んでください。1 回の非提出・却下を族全体の否定とみなさず、同じ族を再試行するなら前回と異なる仮説上の理由を説明してください。`indicator (未配備) @<足>` は配備されていない indicator 候補を足ごとにまとめた行で、`未配備指標` は配備されていない indicator への依存を表します。`族不明` は一つの族ではなく、族へ帰属できない候補 (依存 indicator を持たない strategy、記録の欠けた旧行、key の形が規則に合わない行) の件数です。`deployed=yes` の族は別名の新規候補にせず、表示された配備名の更新として扱ってください。

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

`improve_context.py` から `candidate_archives` で参照してよいのは、この 3 列を返す関数の名前 1 つだけである。`ImportFrom` で取り込む名前も、module alias を基点とする `ast.Attribute` の属性名も、その 1 つに限る。`list_by_mission` / `get` / `find_without_path` などの `SELECT *` 系 API は、import しても属性参照しても pin が red になる (AC-18)。

### 3.2 fail-closed と自由文の不在

- outcome は §2.2 の dict lookup で投影し、`.get(raw, "unclassified")` 相当とする。
- family key は §2.3 の文法で分解し、再生成値との byte 一致を確かめる。外れたら `族不明` にする。
- 非配備 indicator の名前は表示前に固定語へ置き換える。
- date は parse できた値の年月だけを出し、壊れた文字列をそのまま出さない。
- plugin 名と pair は §2.3 の文法を通った値だけを出す。
- `quality_flags`、`last_result`、reason、metrics の値を表示 row に受け取る欄そのものを作らない。

これは `project_last_result` が未知の自由文を `unclassified` に畳む方針と同じである。denylist で危険語だけを除くのではなく、公開 schema を allowlist とする。

### 3.3 既存 pin と変更点

| pin | 現在守っていること | 本 spec での扱い |
|---|---|---|
| `tests/loops/test_improve_context.py` | top-level section、直近 note 20 件、`project_last_result` の固定語彙、mission 診断から数値成績・path を落とすこと、redirect 名を配備済み名に限定すること | 既存の assert はすべて維持する。族別型・上限・並び・空 key・未知 outcome・文法・候補名投影・月単位の pin を追加する |
| `tests/loops/test_improve_ledger_columns_not_on_agent_surfaces.py` | 台帳の新列 4 つ (`logic_hash` / `family_key` / `outcome_class` / `quality_flags`) の canary が、agent の見る 8 面のどれにも出ないこと | 書き換える。緩めはしない。変更前後の assert は下表のとおり |
| `tests/loops/test_floor_leak_guard.py` | 人間 reject 理由が履歴・rendered prompt に出ず、人間向け台帳には残ること | そのまま維持し、族別要約を含む全文を観測する |
| `tests/loops/test_improve_agent_surface.py` | approval payload の holdout 値、archive metrics、hash、候補名、activity canary が context/prompt に出ないこと。改善 tool 名が exact allowlist であること。`ac24` は 3 内部状態が agent から見分けられないこと | canary pin と `ac24` は維持する。`candidate_archives` import 禁止と「候補履歴節そのものが無い」という旧 pin は置換する (下記) |
| `tests/loops/test_improve_forbidden.py` | `IMPROVE_FORBIDDEN == {get_signals, bless}` と改善系 Mission tools との非交差 | 一切緩めない |
| `tests/tools/test_signal_tools.py` | `get_signals` の lookback、pair、status、正確な返却 key 集合、strategy 行への `in_sample_metrics` 添付 | 一切変えない。むしろ既存 tool を改善へ流用できない根拠として維持する |
| `tests/tools/test_mission_registry.py` | improve registry に人間 log 読取経路がなく、許可 tool 集合が固定されること | exact allowlist に新 tool を足さず維持する |
| `tests/tools/test_improve_staging_tools.py` | 配備済み view に成績・期間・段の field 名がなく、自由な `params` 名前空間と区別されること | 一切緩めない |
| `tests/tools/test_improve_rpc_tools.py` | `_strip_forbidden` が期間・日時 key を再帰的に剥がし、agent 応答と private ledger を分離すること | 一切変えない。族別要約は RPC 経路を使わない |
| 遮断 7 (期間・端点を返さない) | エージェント向け API に期間も端点も返さない。rendered prompt に wall-clock の日付が無い | 族別節に出る時刻は `YYYY-MM` だけで、`YYYY-MM-DD`、時刻、`started_at` / `finished_at` の値が節にも prompt 全体にも出ないことを pin する (AC-19) |

`test_improve_ledger_columns_not_on_agent_surfaces.py` の変更前後は次のとおりである。この fixture の canary は `family_key="indicator:familycanary_c2e84b@1h"` (配備されていない indicator) と `outcome_class="duplicate_metrics"` なので、§2.2 と §2.3 の投影後は `indicator (未配備) @1h` 行の `duplicate` として出る。

| assert | 変更前 | 変更後 |
|---|---|---|
| 8 面 (`generated_prompt` / `improve_context` / `rendered_prompt` / `last_result_and_backlog_notes` / `improvement_runs` / `activity` / `archive_index` / `best_note_suffix`) が空でない | あり | 変えない |
| 8 面のどれにも 4 つの生 canary (`v1:logiccanary7f3a91d2`、`indicator:familycanary_c2e84b@1h`、`duplicate_metrics`、`legacy_unlinked`) が出ない | あり | 変えない |
| `best_note_suffix` と `archive_index` に `best=` がある | あり | 変えない |
| best / INDEX の読取関数 4 つが新列名を参照しない | あり | 変えない |
| `generated_prompt` と `rendered_prompt` の族別節に、植えた行の投影形 `indicator (未配備) @1h` の行があり、`attempts` と `duplicate=` の件数が植えた行数と一致する | なし | 追加 (族別節が台帳を実際に読んだことの positive control) |
| `improve_context` の `improvement_history.family_attempts.rows` に同じ投影行が構造化値として 1 行ある | なし | 追加 |
| 残り 5 面に投影形 (`indicator (未配備)`、族別節の見出し) が出ない | なし | 追加 (投影が DB・activity・INDEX へ書き戻されない) |
| `family_key` に自由文の pair を含む key (`strategy:...@1h@USDJPY,pf1.42 ...`) を植えると、8 面のどれにもその文字列が出ず、族別節では `族不明` に数えられる | なし | 追加 |

module docstring は「新列の生値は agent 面に流れず、族別節には §2 の投影形だけが出る」に改める。

既存 `test_ac13b_improve_context_does_not_import_candidate_archives` と `test_ac13b_prompt_has_no_candidate_history_section` は、新要件と直接矛盾するため削除だけにはしない。前者は「`improve_context.py` が `candidate_archives` から参照する名前は 3 列関数 1 つだけ」という AST pin (§3.1) に、後者は「store API が返す key 集合が 3 列と完全一致する」「rendered family row の key 集合が §2.1 と完全一致する」「archive に植えた metric / reason / hash / name canary が全文にない」という、より狭い読取面の pin に置換する。`namecanary` の候補名 canary は、§2.3 の投影で非配備の名前が出ないので、そのまま green を保つ。

## 4. `get_signals` 不在の扱い

### 4.1 比較

| 案 | 内容 | 利点 | リスク・費用 |
|---|---|---|---|
| (a) prompt で禁止と理由を明示 | `get_signals` は取引判断 loop 専用で改善では使えず、配備済み strategy の直近方向を改善材料にせず、不在を note にしないと書く | 現在の遮断を保つ。新しい DB / tool / payload 面がない。反復の原因である「設計上の不在が説明されていない」を直接直す | prompt 規律なので agent が無視する可能性はゼロではない。効いたかは配備後の改善周で観測する (AC-20) |
| (b) 方向専用 tool を追加 | `get_deployed_directions` 等で成績を除いた直近方向だけを返す | agent が求めている情報を直接与えられる | signals は direction だけでなく status、鮮度、足、payload、配備操作との link を持つ。安全な projection、件数・期間上限、stale/consumed の意味、pair 横断、壊れた payload の扱いが新たに必要になる。現在相場の方向を改善仮説へ混ぜる短期バイアスも生む。新しい情報経路が遮断面と pin を増やす |

### 4.2 採用

案 (a) を採用する。

族別履歴の目的は、過去にどの仮説族をどれだけ試したかを次周へ渡すことであり、現在の signal 方向はその問いに必要ない。既存 `get_signals` は strategy 行に in-sample 成績を添えるため、そのまま改善へ許可できない。方向だけの派生 tool を作ると、成績を除いても新しい時系列情報経路と状態解釈が増える。観測された失敗は tool 不足ではなく、意図的な不在が prompt に伝わっていないことなので、より小さく原因へ効く案 (a) とする。

note の窓には、`get_signals` 相当の tool を求める過去の note (言い換え 10 件) が残る。note を外す人間の操作は今は無く、§6 のとおり DB も表示窓も変えないので、基準文面でこれらを解消済みの制約として扱わせる。

prompt の規律へ次の基準文面を追加する。

> `get_signals` は取引判断 loop 専用で、この改善 mission では利用できません。配備済み strategy の直近 signal の方向・status・payload は、仮説の選択や候補評価の材料にしないでください。この不在は設計上の制約なので、課題・note・discovery として記録せず、候補自体は許可された `run_backtest` と self-test で検証してください。過去の note にある signal 照合や `get_signals` 相当の tool の要望は、この制約によって解消済みです。それに従って tool を探したり、同じ要望を再記録したりしないでください。

案 (b) と、既存 `get_signals` の改善 allowlist への追加は本 spec の非目標である。

**戻り条件。** 配備後の改善周で AC-20 の観測を行い、2 周続けて `get_signals` や signal 方向照合への言及が新しく残る場合は、案 (b) を別 spec で再検討する。

## 5. prompt 内の配置

「改善履歴」の直後、「現行構成」の前に「仮説族の試行状況」を置く。agent が選択前に履歴の時系列と族別の集約を続けて読めるためである。旧「配備済みの仮説族」節は削除し、内容は §2.4 の統合表示へ移す。

「直近の成績レポート」は現状の位置と内容を変えない。族別表をその節へ入れず、候補成績と実運用の成績を一つの表にしない。

`get_signals` の基準文面は「規律」に置く。tool 一覧を読む時点で禁止と代替検証手段を同時に見せる。

## 6. note の表示は変えない

backlog note の表示窓 (新しい順に 20 件) と DB 上の note は変えない。

本番 DB の実測では note 78 件の `idea_norm` が 78 種類で、完全一致の重複は 0 件だった。書込み側がすでに `idea_norm` で重複を排除しているので、表示側で `idea_norm` による newest-wins をかけても 1 件も畳まれない。観測された反復は言い換え (10 件) で、自由文の類似度判定は fail-closed の原則に反するので入れない。言い換え note による逆向きの申し送りは §4.2 の基準文面で扱う。note を退役させる人間の操作は別チケットで扱う (§9.4)。

## 7. 受入条件と変異

### 7.1 受入条件

| AC | 条件 | 観測点 |
|---|---|---|
| AC-1 | store の族別履歴用読取は `family_key` / `outcome_class` / `created_at` だけを返し、metrics、候補名、hash、path、pair、flags を process へ返さない | store 戻り値の exact key set、SQL trace または spy |
| AC-2 | 表示 key ごとに archive 行が集約され、`attempts == sum(outcomes.values())`。同 mission の別 archive 行は別試行として数える | context の構造化値 |
| AC-3 | NULL / 空 / 文法外 / 再生成値と不一致 / 長過ぎる key は削除も逐語表示もされず、`族不明` 1 行へ集約される。改行、`\|`、日本語、数値と自由文を含む pair の key、`settings.pairs` に無い pair の key、`@None` の key を fixture に含める。依頼時の 24 旧行に相当する fixture も 1 行になる | row 数、`族不明.attempts`、rendered prompt に原文が無いこと |
| AC-4 | `unprofitable` / `insufficient_trades` / `gate_failed` / `noop_copy` / `family_redirect` は `not_submitted` に、`duplicate_logic` / `duplicate_metrics` は `duplicate` に畳まれ、未知 outcome は必ず `unclassified` になる。公開 label の集合は §2.1 の 9 語と完全一致し、保存値の生文字列 (`unprofitable`、`insufficient_trades`、`gate_failed`、`noop_copy`、`family_redirect`、`duplicate_logic`、`duplicate_metrics`) は族別節にない | context と prompt の canary、公開 label 集合 |
| AC-5 | key を持つ配備済み族は archive と 1 行に merge され、未試行なら 0 回で出る。`family_redirect` はその行の `not_submitted` に数えられる。key なし配備済み plugin は構成を保った残余一覧に一度だけ出る。signal kind の plugin を 1 件含め、kind が `signal` と表示される | family/plugin 名の出現回数、構造化値、残余一覧の kind |
| AC-6 | 順序は §2.5 のとおりで、最大 50 entry、節全体 16 KiB 以下。超過時は完全な行だけを出し、省略件数が一致する | UTF-8 byte 数、entry 数、先頭/末尾 row、`entries_omitted` |
| AC-7 | 表は 5 列で、結果 label は固定順、月は `YYYY-MM`、不正日時の生文字列は出ない。`deployed` 列は §2.5 の描画規則 (昇順、`, ` 区切り、最大 5 件、`他 N`) に従う | rendered table 全文 |
| AC-8 | metric canary を `metrics_json` に植えても context / prompt に値も metric 列も現れない。PF、`avg_r`、取引数の各 canary を別値にする | positive control の DB 行、context JSON、rendered prompt 全文 |
| AC-9 | 人間 reject reason canary は `approval_requests.reason` に残るが、族別要約を含む context / prompt には現れない | DB reason と rendered prompt 全文 |
| AC-10 | `get_signals` の基準文面 (解消済みの要望を再記録しない一文を含む) が prompt にあり、改善 registry の exact tool set と `IMPROVE_FORBIDDEN` は変更前と同じである | rendered prompt、registry names、禁止集合 |
| AC-11 | backlog note の表示は変わらない。既存の `test_backlog_notes_are_newest_twenty_with_omission_count` が無変更で green である | 既存 test |
| AC-12 | 既存 `project_last_result`、`_MISSION_DIAGNOSTIC`、RPC strip、staging view、signal tool の遮断 pin と `ac24` が green のままである | §3.3 の既存 test 群 |
| AC-13 | 改善 mission の cadence、slot 数、tool budget に差分がない | 設定 diff と対象ファイル diff |
| AC-14 | `test_improve_ledger_columns_not_on_agent_surfaces.py` が §3.3 の変更後の assert で green である。生 canary 4 つの不在の assert は変更前と同じである | 8 面の本文と族別節 |
| AC-15 | 同じ族に `unprofitable` / `insufficient_trades` / `gate_failed` (比較不能経路で記録) の 3 行を置くと、族行は `not_submitted=3` の 1 区分だけになり、3 状態を区別する手掛かりが無い | 族行の構造化値と rendered row |
| AC-16 | 配備されていない indicator 候補の名前は族別節にも context の族要約にも出ない。非配備 indicator の行は足ごとの `indicator (未配備) @<tf>` 1 行になり、strategy key の非配備依存は `未配備指標` に置き換わる。名前の違う 2 つの非配備依存を持つ strategy 2 本は同じ表示 key の 1 行に merge される。名前がそのまま出る indicator は `deployed=yes` の行だけである | 名前 canary の不在、表示 key の集合、merge 後の件数 |
| AC-17 | 配備済み plugin ごとに、meta から `family_key_from_parts` で作った key と `read_identity(plugin_dir)` の key が byte 一致する。timeframe を省いた indicator、`timeframe: null` の indicator、pair が重複・未整列の strategy、依存 indicator が宣言順で未整列の strategy を含める | key の byte 比較 |
| AC-18 | `improve_context.py` が `candidate_archives` から参照する名前 (`ImportFrom` の名前と、module alias を基点とする `ast.Attribute` の属性名) は 3 列関数 1 つだけである | AST |
| AC-19 | 族別節の時刻は `YYYY-MM` だけで、rendered prompt 全体に `YYYY-MM-DD`、時刻、`started_at` / `finished_at` の値が出ない | rendered prompt の正規表現検査 |
| AC-20 (運用) | 配備後の次の改善周 (土曜の周) の transcript と、その周で作られた note・discovery を読み、`get_signals` や signal 方向照合を求める文と、存在しない tool の呼出・探索が 0 件である。2 周続けて言及が残れば §4.2 の戻り条件に従う | 改善周の transcript、improvement_backlog の新規行 |

### 7.2 殺すべき変異

| 変異 | 変異内容 | red にする pin |
|---|---|---|
| M-1 | family row に `pf`、`avg_r`、`trades`、`metrics`、score のどれか 1 列を混ぜる、または `metrics_json` を select する | AC-1 の exact key set + AC-8 の値 canary / exact公開 schema |
| M-2 | `approval_requests.reason` を join する、または reject reason を outcome 表示へ足す | AC-9 の reason canary |
| M-3 | 未知 outcome をそのまま返す、空にする、または `not_submitted` にする | AC-4 の `unclassified` exact assert |
| M-4 | `unprofitable` / `insufficient_trades` を独立 label で出す | AC-4 の公開 label 集合、AC-15 |
| M-5 | 空 key 行を集計外にする、行ごとに出す、または既知族へ推測分類する | AC-3 の件数と 1 行 assert |
| M-6 | archive 行数でなく distinct mission 数を `attempts` にする、または結果件数と試行回数を別集計する | AC-2 の同 mission 複数 archive fixture と総和 invariant |
| M-7 | 配備済み一覧を旧節にも残す、redirect を別行にする、key なし plugin を `族不明` archive と merge する | AC-5 の出現回数と行分類 |
| M-8 | entry 上限、byte 上限、固定順のいずれかを外す | AC-6 の上限境界 fixture |
| M-9 | 月の代わりに日や時刻を出す、壊れた原文を出す、`started_at` を描画する | AC-7、AC-19 |
| M-10 | `get_signals` を improve registry に足す、禁止集合から外す、または基準文面 (解消済みの一文を含む) を削る | AC-10 の exact allowlist / prompt assert |
| M-11 | note の表示窓・並び・件数を変える | AC-11 の既存 test |
| M-12 | `gate_failed` を独立 label として素通しにする | AC-4 の公開 label 集合、AC-15 |
| M-13 | `duplicate_metrics` を `duplicate` と別の label にする | AC-4 の公開 label 集合、AC-14 の生 canary 不在 |
| M-14 | 文法検査を外して長さ検査だけにする、または再生成との byte 比較を外す | AC-3 の自由文 pair・`settings.pairs` 外 pair・未整列 key の fixture |
| M-15 | 配備側の key を meta から別の式で作る (timeframe None を `"None"` にする、pairs を宣言順のままにする) | AC-17 の parity、AC-5 の merge |
| M-16 | 改善 context が `list_by_mission` などの `SELECT *` 系 API を呼ぶ | AC-18 の AST pin |
| M-17 | 非配備 indicator の名前を落とさずに出す、または strategy key の非配備依存を置換しない | AC-16 の名前 canary、`namecanary` pin |
| M-18 | `deployed_families` を context から消し、`deployed_names` を空にする | `test_improve_context.py` の redirect 名 pin |

M-1 と M-2 は必須の漏洩変異であり、単なる禁止語検索ではなく、DB に実在する別々の canary 値を positive control としてから agent 面の全文で不在を確認する。AC-20 は事後観測であり、変異では落とせない。

## 8. 確認事項

追加のユーザー裁定が必要な事項はない。次は本 spec 内で技術的に確定した。

- `get_signals` は案 (a) を採用し、新 tool は作らない。効果は配備後の改善周で観測し、戻り条件を持つ。
- 空 family key と文法外の key は集計外にせず `族不明` 1 行にする。
- 公開する結果区分は既存の agent 面の畳み方に揃える。
- 非配備の indicator 候補の名前は出さず、足ごとの件数に集約する。
- 最終試行は月単位で出す。
- note の表示は変えない。
- 表の上限と順序は遮断と既存データ構造から決めた。

## 9. 実装範囲、残余リスク、後続

### 9.1 task

T1 と T2 は `improve_mission.md`、`improve_loop.py` の render、`test_improve_context.py` を共通に触るので、同じ worktree で T1 → T2 の順に直列で進める。

| task | 内容 | 規模 | 性質 |
|---|---|---:|---|
| T1 | `family_key_from_parts` への一本化と parity pin、archive の 3 列 projection と AST pin、key の文法検査と再生成照合、outcome 投影、候補名投影、族別集計、配備済み族との merge、bounded render、月単位の日付、既存遮断 pin の書き換え、漏洩・変異 pin | 中 | 純 Python + prompt Markdown。schema migration なし |
| T2 | `get_signals` 不在と解消済み要望の基準文面、exact allowlist の観測 | 小 | prompt Markdown と test のみ。tool 追加なし |

### 9.2 変更ファイル

実装時に変更を見込むファイルは次のとおり。

- `src/agentic_fx/store/candidate_archives.py`: 3 列だけの read API。
- `src/agentic_fx/plugin/logic_identity.py`: `family_key_from_parts` の追加と、`_family_key` をそれに通す整理。key の値が変わるのは `timeframe: null` を明示した indicator だけである (§2.3)。key の文法による分解と再生成照合もここに置く。
- `src/agentic_fx/loops/improve_context.py`: outcome 投影、key の既知族判定、候補名投影、族別集計、配備との merge。`deployed_families` は残す。
- `src/agentic_fx/loops/improve_loop.py`: bounded family table の render と旧「配備済みの仮説族」節の除去。
- `src/agentic_fx/loops/prompts/improve_mission.md`: 節の置換、読み方、`get_signals` 不在の基準文面。
- `tests/loops/test_improve_context.py`: 構造、集計、順序、上限、日付、文法、候補名投影の pin。
- `tests/loops/test_improve_agent_surface.py`: archive canary と exact projection。新要件と矛盾する旧 negative pin の置換 (§3.3)。
- `tests/loops/test_improve_ledger_columns_not_on_agent_surfaces.py`: §3.3 の変更前後の表のとおり書き換える。
- `tests/loops/test_floor_leak_guard.py`: 族別要約を通した人間 reason 非漏洩。
- `tests/plugin/test_logic_identity.py`: `family_key_from_parts` の文法・再生成照合の pin と、meta / `read_identity` の parity pin。
- 必要なら `tests/loops/test_improve_prompt_objective.py`: prompt 基準文面の exact pin。

`src/agentic_fx/tools/signal_tools.py`、`src/agentic_fx/tools/improve_rpc_tools.py`、`src/agentic_fx/tools/mission_registry.py` と `tests/tools/` は変更対象ではない。既存 pin を実行して「緩めていない」ことを確認する対象である。

### 9.3 残余リスク

- family key はパラメータ値を含まないため、同じ族内の仮説差を表さない。これは同族を更新扱いにする現行設計と同じである。
- 現時点では archive 46 行のうち半数以上 (24 行、文法検査でさらに増えうる) が `族不明` になる。族表が伝えられる情報は今のデータでは半分程度で、新しい候補が増えるにつれて比率が下がる。本 spec は旧行の backfill を行わない。
- 非配備 indicator を足ごとに集約するため、indicator 候補の系統ごとの反復回数は見えない。名前を出さないこととの交換である。
- `rejected_by_human` の族別件数から、人間がどの族を好まないかを推定できる。これは族 key と結果区分を渡すというユーザー裁定の範囲に入る残余であり、理由の文は渡さない。
- entry / byte 上限を超えた古い非配備族は省略される。省略件数は見えるが詳細は見えない。
- prompt 指示は agent の反復を確率的に抑えるもので、書込み時に要望 note を拒否する制約ではない。既存の言い換え note は窓に残り、基準文面だけが逆向きの申し送りを打ち消す。効いたかは AC-20 で観測する。
- `timeframe: null` の indicator の key が `@None` から `@1h` に変わるので、そのような旧 archive 行は質検査の同族判定でも配備側と一致しなくなる。現行の配備済み・archive にこの形が無いかを着手時に read-only で確かめる。

### 9.4 後続

- in-sample 成績の族別順序尺度は、holdout・期間・標本数・人間判断の漏洩境界を別 spec で裁定する。
- note のあいまい一致による畳み込みは入れない。解消済みの note を退役させる人間の操作は別チケットで扱う。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-24 | v0.1 | 初版。族別試行要約、fail-closed 投影、配備済み族との統合、`get_signals` 不在の案 (a)、note 窓内 dedup、AC と変異を定義 | 2 チケットを同じ改善 feedback 境界として設計するため | — (未 commit) |
| 2026-10-24 | v0.2 | 既存の遮断 pin (台帳新列の 8 面検査) の変更前後を定義。公開結果区分を既存 agent 面の畳み方に揃える。key の文法と再生成照合、非配備 indicator 名の集約、`family_key_from_parts` への一本化と parity pin、`deployed_families` の維持、3 列関数だけの AST pin、最終試行を月単位にして遮断 7 を pin。note dedup を削除し、解消済み要望の基準文面と事後観測 AC と戻り条件を追加。DB 実測値を訂正し、T1 → T2 を直列に | 1 周目の指摘 | |
