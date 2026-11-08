# 改善 mission 1 回ごとの要約を人間に見せる (improve-run-summary-for-humans) 設計 C2.2

**チケット:** `improve-run-summary-for-humans` (高、機能)。`improve-mission-output-not-persisted` (中、不具合) の output 側を含む
**状態:** C2.2 (2026-11-08)、sol 1 周目・opus 2 周目・sol 3 周目を反映、§3 は利用者が決定

## 0. 何が起きているか

改善 mission は週次や手動で回り、候補を作る。しかし提出床 (`settings.improve.gate`、既定 `min_pf=1.0`) に届かなかった試行は、承認待ちにも plugin にも現れない。利用者に見えるのは Discord の終了通知の数行 (結末の見出し、unprofitable なら落ちた段の失敗 pair の pf・avg_r・取引数、所要時間) と、backlog の `observation:not_submitted` だけ。「何を試して、どれだけ足りなかったか」が読めない。

調べて分かったこと (file:line は `tmp/review-runsum/explore.md`、レビューの原文は `tmp/review-runsum/c0-sol-out.md`・`c1-r2-out.md`):

- **agent の最終出力** (`IMPROVE_OUTPUT_SCHEMA`、`loops/summary.py:129`) は、仮説 `selected.idea`、`artifact` (plugin / report / observation)、`selection_rationale`、`discoveries` を持つ。
- **保存されるもの**は一部だけ。
  - `selected.idea` は `improvement_backlog.idea` に入る。
  - `selection_rationale` と `artifact.summary` は、提出したときの承認 payload にしか入らない。
  - observation の観察文は、`_select_observation` (`improve_loop.py:570-580`) がマスクの規則 (family_redirect / duplicate なら出さない) を通したものだけが、`improve_run_notices.observation_text` に入る。
  - 質検査で降格した経路の `artifact.reason` は、agent の文ではなく親が作った固定理由 (`improve_loop.py:3711`、`:4082`)。
- **`missions.output_json`** は改善 mission では常に NULL。
  - `finish_improve_mission` は `output=` を受け取れるが、`improve_loop.py` の実呼出し 13 箇所はどれも渡していない (未配線)。
  - commit 例外の補償 `compensate_commit_failure` は `commit()` の外で、supervisor から呼ばれる (`improve_supervisor.py:369/667/691`)。受け取るのは ctx と exc だけ。
  - 起動時の回収 `recover_interrupted` は `finish_improve_mission` を通らない。
- **今の Discord の終了通知**は、`_body_lines` (`afx_ops/gate/improve_render.py:299-329`) で次を出す。
  - 床未達の落ちた段の失敗 pair (公開済みの md レポートの逆パース `_floor_of`、`floor` の意味は「落ちた段の失敗 pair だけ」)
  - 観察文
  - 配備中の版との比較
- **10-25 の通知の裁定** (`docs/superpowers/specs/2026-10-25-improve-discord-notice-design.md:202`)。Discord に出す agent 由来の文字列は観察文 1 項目だけで、`not_submitted` では出さない。

## 1. 何を作るか (動作)

改善 mission が終わるたびに (起動時の回収を含む)、親 (決定論的コード) が **要約 1 件** を組み立てて保存する。ops API が要約を返し、Discord の終了通知が要約から本文を組む。web の画面は、別の設計「plugin と改善の画面群」で作り、この API を読む。

### 1.1 要約の項目

結末そのものは要約に重ねて持たない。結末は `improve_run_notices` (終端の正本) から読む。

| 項目 | 中身 | 出所 |
|---|---|---|
| 仮説 | agent が選んだ案 (`selected.idea`)、backlog の既存項目なら backlog id | agent の出力。**agent の申告**。API と web だけに出す (§3 の裁定 1) |
| agent の説明 | `selection_rationale`、plugin なら `artifact.summary` | agent の出力。**agent の申告**。API と web だけ |
| 観察文 | `choice.observation_text` (今の通知と同じもの。マスクの規則をそのまま守る) | 終端の choice |
| 親の判定理由 | 質検査の降格・見送りの理由 (固定語) | 親 |
| 試したもの | plugin 名 (loader の文法を通ったもの)、kind・新設か更新か・strategy なら使った indicator の名前 | 名前は親が確かめた時点から出す。kind と indicator は `candidate_meta` (plugin gate の通過後) から。それより前に落ちた経路では、名前だけ。agent が申告した kind を、親が確かめた kind として補わない |
| 床との差 | 段 (in_sample / holdout) × pair ごとの判定値 (pf・avg_r・取引数・max_drawdown・取引頻度・評価可能か)。その時の閾値 (`ImproveGateSettings.snapshot()`)。落ちた段と条件 (`_floor_fail_items` の結果) と該当 pair 数 | verdict の `candidate_metrics` / `holdout_metrics`。holdout を実施していなければ「未実施」。評価不能 (取引数不足) で verdict が成績を持たない経路は、`candidate_rows` (候補の計測行だけ。baseline を混ぜない) の値を「参考値」として出す |
| 配備中の版との比較 | 上回ったか / 上回らなかったか、比べた指標 | verdict の `comparison` |
| 申し送り | 終端の時点で、この候補の族が次の mission の「族別試行結果」でどう見えるか。表示 key、試行行数、公開の結末の内訳 | 下の「申し送りの数え方」 |

**申し送りの数え方:**
- 配備の集合は、mission 開始時の `ctx.inventory` に固定する (live を読み直さない。書込みロックの中で FS を走査しない)。
- 次の prompt と同じ投影を 1 件ずつ行う関数 (生の family key → 表示 key、outcome → 公開の結末) を `improve_context` から切り出して共有する。
- この候補の表示 key の行だけを、archive 行単位で数える。
- `family_key` が None、または settle が失敗して archive 行が無いときは、申し送りの欄を出さない。
- 次の prompt は 50 件・16 KiB で打ち切るので、この族が次の prompt に載るとは限らない。表示は「終端時点の集計」とし、後の prompt と一致することは約束しない。

手に入らない項目は欄ごと出さない。

### 1.2 材料の受け渡し

- `ImproveRunContext` (frozen) に、可変の材料入れ `summary_material` を 1 つ持たせる (mission ごとに `default_factory` で作り、ctx の間で共有しない)。commit の各段で、そこへ材料を書き足す。
  - finalize helper 13 箇所に引数を通さない。commit の外の補償 (`compensate_commit_failure`) も、ctx から材料を読める。
- staging を消す前 (`improve_loop.py:4794` より前) に、候補の材料を書いておく。

| 材料 | 書く時点 |
|---|---|
| agent の元の出力 | 出力を受け取った直後 (検証の前でも、`output_json` 用に保存する。表示には検証済みの項目だけ使う) |
| 仮説・説明 | `_select_and_bind` の後 |
| plugin 名 | 親が名前の文法を確かめた時点 |
| kind・indicator・新設か | `candidate_meta` が得られた後 |
| 床・比較 | strategy verdict が出た後 |
| 観察文・親の判定理由 | 降格・見送りを決めた時点 (choice) |

- `finish_improve_mission` に `summary=` を、既定値なしの必須 kw として足す (`notice=` と同じ扱い)。10-25 の設計の AST 網羅 test (AC-5(b)) を更新し、13 箇所すべてが渡すことを固定する。

終端経路ごとに手に入るもの:

| 終端経路 | 要約に入るもの |
|---|---|
| prepare の補償 (`:1052`)、Tx-2 の補償 (`:3127`) | なし (結末は notice) |
| commit 例外の補償 (`:1192`) | 出力が schema を満たせば仮説・説明、plugin 名まで進んでいれば名前 |
| 提出 (`:3536`) | 全項目 |
| report の公開 / 書込み失敗 (`:4240`、`:4159`) | 仮説・説明・観察文。降格なら試したもの・床・親の判定理由 |
| runner 失敗 (`:4406`) | 出力が schema を満たすときだけ仮説・説明 |
| sandbox 不能 (`:4447`) | 仮説・説明 (gate の結果は出さない) |
| output 不正 (`:4499`) | なし (出力そのものは `output_json` に残す) |
| 選択競合 (`:4544`、`:4567`) | 仮説 |
| gate 失敗 (`:4845`、`:4898`) | 仮説・説明、得られた範囲の試したもの・床、verdict を得ていれば比較 (`not_improved` を含む)、申し送り |
| 起動時の回収 | なし |

### 1.3 保存

- **新しい表 `improve_run_summaries`** に、mission ごとに 1 行を書く。
  - 列: `mission_id` (主キー、`missions` への FK)、`summary_version`、`state`、`summary_json`、`created_at`。
  - 書くのは `finish_improve_mission` と同じ tx。起動時の回収も、自分の tx の中で `state=empty` の行を `INSERT OR IGNORE` で書く。
  - UPDATE / DELETE を禁止する trigger を付け、`TABLE_NAMES` に登録する。既存の `CREATE TABLE IF NOT EXISTS` / `init_db` の作法に沿い、移行は再実行しても変わらない。
  - `improve_run_notices` には列を足さない。
- **組み立ての順序と失敗の扱い:**
  - tx の前: 申し送り以外の項目を正規化・検証しておく。
  - tx の中は、次の順に行う。
    1. archive の settle (今どおり)。
    2. SAVEPOINT の中で申し送りを集計する (先例 `_settle_candidate_in_tx`)。集計が失敗したら SAVEPOINT へ戻し、申し送りの欄を出さずに続ける。
    3. 最終の直列化と 16 KiB の上限処理。直列化が失敗したら `state=build_failed` (`summary_json` は `{}`) に倒し、終端は続ける。
    4. `finish_improve_mission` の CAS。要約の INSERT は CAS が成功した後に置く。
  - SAVEPOINT へ戻すこと自体の失敗と、INSERT の失敗は外へ伝える。終端 tx ごと rollback する (今の終端と同じ扱い)。CAS が成立しなければ、settle も要約も残らない。
- `state` は `ok` / `empty` (材料なし) / `build_failed`。
- **上限:** `summary_json` は数値・固定語・長さを切った引用文字列だけを持つ。
  - 仮説・説明は保存時 500 字。
  - indicator の名前は 20 件まで。
  - pair は設定の pair 数まで。
  - 全体は 16 KiB まで (超えたら説明 → 申し送り → pair の順に落とす)。
- **`missions.output_json` も配線する。** agent の元の出力を、tx の前に直列化して渡す。
  - 64 KiB を超えるときは、保存せず `{"truncated": true, "bytes": n}` を入れる。出力が無い経路は NULL。
  - 事後の調査用で、表示には使わない。
  - transcript は保存しない (今のまま `[]`)。秘密を含みうるため、別チケットで扱う。

### 1.4 agent の文章の扱い

- 保存の前に `safe_text` (`store/missions.py:176-178`、観察文の保存と同じ) を通し、`display_text` で 1 行に正規化して長さを切る。
- 表示先ごとに既存の escape を通し、長さは escape の後で数える。Discord は `escape_discord` + `cut_escaped`、web は HTML escape。
- 仮説・説明は「agent の申告」と分かる見出しの下に出す。
- 要約も、保存した `output_json` も、その自由文も、改善 mission の prompt に入れない。prompt に流れる経路 (backlog の `last_result`、族別試行結果) は今のままで、要約から新しい経路を作らない。

### 1.5 ops API

- **一覧 `GET /v1/improve/runs` (新設):** 新しい順。`?limit=` (既定 50、上限 100) と `has_more` を返す (既存の一覧の作法)。
  - 各行: mission id、日時、trigger、結末 (notice の固定語)、plugin 名、落ちた段と条件と該当 pair 数、承認 id。
  - 権限は `APPROVALS_LIST` (ops-web が持つ)。
  - 応答は 100 件で 64 KiB 以内に収める。
  - ops API の endpoint 数は 30 → 31 (`api_server.py:275` と test を更新)。
- **詳細 `GET /v1/improve/runs/{id}` (既存):** 応答に `summary` を足す。今の項目は、形も意味も変えずに残す (`floor` は今の md の逆パースのまま)。
- **`summary` の形:**
  - `{"version": 1, "state": "ok" | "empty" | "build_failed", "items": {...}}`。要約の行が無い mission は `"summary": null`。
  - 各項目の型 (数値 / null)、文字列の上限、配列の上限を決め、DTO の検査で固定する。

### 1.6 Discord の終了通知

- 有効な `summary` (version 1、`state=ok`、形が正しい) があるときは、本文の**内容行**を要約から組む。今の `_body_lines` の内容行 (床・比較・観察文) は使わない (同じ内容を 2 回出さない)。
  - 要約で置き換えるのは内容行だけ。次のものは、今どおり notice と既存の detail から組む: 承認番号 (`plugin <name> (<kind>) / 承認 #<id>`)、固定の案内文 (`not_improved` の見送り文、`not_submitted` の固定文など)、所要と tool 回数、繰り返しの行。
  - 要約から組んでも、今の本文が出していた情報は欠かさない: 落ちた段の失敗 pair と評価不能の表示、提出時の候補と配備中の版の比較値、`not_improved` の理由、通常の観察文。
- `summary` が無い・`empty`・`build_failed`・形が不正・未知の version のとき、または afx-ops が古い daemon と話して `summary` キーが無いときは、今の `_body_lines` のまま出す。
- Discord に出す agent 由来の文字列は、今どおり観察文 1 項目だけ (10-25 の裁定、§3 の裁定 1)。出す項目は描画の allowlist 1 か所で決める。
  - 仮説と agent の説明は Discord に出さない。
  - 要約から組む本文に新しく入るのは、親が作った項目 (試したもの・床との差・比較・申し送り・親の判定理由) だけ。

```
[改善] 床未達で見送り #812 (unprofitable)
試したもの: strategy london_pullback (新設)、indicator: atr_band, session_open
床との差: in-sample で不合格 (pf が床 1.00 未満 1/1 pair)
  USDJPY pf 0.91 avg_r -0.02 取引 64
申し送り (終端時点): 族 <表示 key> — 試行 4 (<公開の結末> 3、<公開の結末> 1)
所要 6分12秒 / tool 41 回
```

- 本文の上限 2000 字は今のまま。長さは escape の後で数える。
  - 足りなければ、申し送り → 床との差の pair 行 → indicator の名前の順に削る。
  - それでも超えたら、今と同じく見出しと「(本文を省略)」にする。

## 2. しないこと

- 改善 mission の出力 schema (`IMPROVE_OUTPUT_SCHEMA`) は変えない。申し送りは agent に書かせず、親が集計から作る。
- 要約を LLM に作らせない。
- transcript は保存しない。
- 既存の `floor` の意味と出所は変えない。
- web の画面は作らない (「plugin と改善の画面群」の設計で作る)。

## 3. 裁定 (2026-11-08 利用者が決定)

1. **仮説と agent の説明は API と web の詳細だけに出す。** Discord は 10-25 の裁定 (agent 由来は観察文 1 項目) を守る。ただし、後で Discord にも送れる拡張性を残す。
   - 要約には仮説・説明を保存しておく。
   - Discord に出す項目は、bot の描画の allowlist 1 か所で決める。仮説を足すときは、この 1 か所と 10-25 の裁定を更新する。その場合も、保存時と表示時の無害化を通し、200 字で切り、`not_submitted` ではマスクの規則に揃えて出さない。
2. **申し送りは親の集計**とする。終端時点で、次の mission に渡る族別の試行結果を数える。agent の出力 schema は変えない。
3. **`missions.output_json` の配線は同じ束で行う。** 上限は 64 KiB で、transcript は保存しない。

## 4. 受入

1. 終端の各経路 (§1.2 の表、起動時の回収を含む) で、要約の行がちょうど 1 行保存され、表のとおりの項目だけを持つ。二重に終端しようとしても行は増えない。`finish_improve_mission` の 13 箇所すべてが `summary=` を渡すことを AST test で固定する。
2. 床との差は verdict の判定値から出て、候補と baseline を取り違えない。閾値は終端時点の `snapshot()` で、設定を後で変えても保存した値は変わらない。holdout を実施していなければ「未実施」と出る。評価不能の経路は `candidate_rows` の「参考値」と出る。落ちた条件は `_floor_fail_items` と一致する。
3. strategy の候補で、使った indicator の名前が出る。新設の indicator 候補は「新設」と出る。plugin gate の前に落ちた経路では、名前だけが出る。
4. 申し送りは共有した投影関数で数える。同じ DB 状態・同じ inventory・同じ allowed_pairs を入力すれば、族別試行結果の同じ表示 key の行と同じ値になる。
   - 未配備 indicator 名だけが違う生の key が同じ表示 key に合流する場合と、内部の outcome が公開の語に畳み込まれる場合を含めて確かめる。
   - 自分の settle を含めて数える。同じ mission の複数 artifact も数えられる。
   - `family_key` が None なら欄が無い。
   - 材料入れは mission ごとで、別の mission の材料が混ざらない。
5. agent の文章は `safe_text` と正規化を通る。改行・制御文字・URL の秘密パラメータ・Markdown・mention を含む入力で、Discord に素のまま出ない。API の応答も同じ無害化を通った値になる。
6. 要約・`output_json`・その自由文が、次の改善 mission の prompt に入らない。
7. Discord の終了通知:
   - 有効な要約があるときは内容行を要約から組み、今の本文と同じ内容を重ねない。
   - 承認番号、落ちた段の失敗 pair と評価不能の表示、提出時の比較値、`not_improved` の理由、通常の観察文は保持される。
   - 要約が無い・不正・未知 version・`build_failed`・`summary` キーなしのときは、今の通知と同じになる。
   - escape の後の長さで 2000 字を守り、決めた順に削る。
   - 仮説と agent の説明は出ない。出す項目が allowlist 1 か所で決まることを test で固定する。
   - マスクの規則 (`not_submitted` で観察文を出さない) は変わらない。
8. `GET /v1/improve/runs` (`limit`・`has_more`) と、`summary` を足した `GET /v1/improve/runs/{id}` が、ops-web の権限で読める。既存の `floor` の形と意味は変わらない。100 件の一覧が 64 KiB に収まる。endpoint 数の test は 31。
9. `missions.output_json` に改善 mission の元の出力が入る。64 KiB を超えれば truncated の印。出力の無い経路は NULL。transcript は `[]` のまま。
10. 失敗の扱い:
    - 最終の直列化が失敗しても mission の終端は成功し、要約は `build_failed` になる。
    - 申し送りの集計が失敗しても、欄が無いだけで終端は成功する。
    - 自分の settle を含めた申し送りが保存される。
    - INSERT の失敗・SAVEPOINT へ戻すことの失敗・CAS 不成立では、settle も要約も残らない。
11. DB の移行: 既存の DB に表と trigger が足される。再実行しても変わらない。古い mission は `summary: null` になる。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-08 | C2.2 | §3 を利用者の裁定で確定 (仮説・説明は API と web だけ、Discord へは allowlist 1 か所で後から足せる / 申し送りは親の集計 / output_json は同じ束) | 利用者の裁定 | — |
| 2026-11-08 | C2.1 | tx 内の順序を settle → SAVEPOINT の申し送り → 最終直列化 → CAS 後の INSERT に。Discord は内容行だけを要約で置き換え、承認番号・固定の案内文は notice から (既存の情報を欠かさない受入)。gate 失敗でも verdict があれば比較を持つ。受入 4 を「同じ入力なら一致」に。材料入れは mission ごと | sol 3 周目 (Important 2 / Minor 1) | — |
| 2026-11-08 | C2 | 材料は ctx の可変の材料入れに置き、`summary=` を必須 kw にして AST test で 13 箇所を固定。Discord は有効な要約があれば要約から本文を組む (重ねない)。既存の `floor` は変えない。観察文は `choice.observation_text` (マスクの規則を守る)、降格の理由は親の固定語。仮説・説明は API と web だけ (10-25 の裁定を守る、裁定 1)。申し送りは `ctx.inventory` に固定して 1 件ずつ投影して数える。直列化は tx の前、申し送りは SAVEPOINT。回収は `INSERT OR IGNORE` で `empty`。一覧は `limit`・`has_more`、endpoint 数 31。`output_json` は 64 KiB 上限。結末は要約に重ねない。web に依存する受入を API の上限に直す | opus 2 周目 (Important 6 / Minor 11) | — |
| 2026-11-08 | C1 | 床の値は verdict から (候補と baseline を分ける、未実施・参考値)、材料の受け渡しと経路別の表、申し送りは次の prompt と同じ集計関数で「終端時点」、起動時の回収にも要約、`safe_text` と表示先ごとの escape、prompt に流さない受入、DTO の version と不正時の扱い、表の FK・trigger・`TABLE_NAMES`、組み立て失敗と書込み失敗の区別、Discord の削り方の最終手段。web の画面は「plugin と改善の画面群」の設計へ移す。呼出し数を 13 に訂正 | sol 1 周目 (Important 6 / Minor 3)、利用者の web 要件 | — |
| 2026-11-08 | C0 | 初版 | チケット improve-run-summary-for-humans の設計 | — |
