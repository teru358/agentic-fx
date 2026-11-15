# 同梱 web の配置の整理 (web-layout-tabs) 設計 C1

**チケット:** `web-layout-tabs` (中、機能)。関連: `web-plugin-improve-hub` (今の画面群を作った束)、`web-ui-charts-equity-indicators` (チャートは後)
**状態:** C1 (2026-11-15)。方針は利用者が決定 (§3)、sol 1 周目 (Important 9 / Minor 1) を反映

## 0. 何が起きているか

同梱 web の画面は、機能を足すたびに節を縦に積んできた仮置きの配置になっている。棚卸しは `tmp/review-weblayout/inventory.md` にある (A = afx-ops/afx_ops、S = src/agentic_fx)。

- **縦に長い**: 承認詳細は最大 12 節、improve 詳細は 10 節、Status は 5 節 (反応統計の表 6 枚を含む) が 1 ページに並ぶ。
- **予算を全節で分け合う**: 1 ページの上限は 64 KiB (`A/web/render.py:16`)。
  - 承認詳細は diff を 48 KiB 以下に、diff 以外 (骨格を含む) を 15 KiB 以下に収める (`render.py:866-903`)。
  - 15 KiB を超えるときは、params の差分 → 申告 → 親の記録の順に縮め、それでも収まらなければ末尾の追加節を先頭から省く。
  - 承認詳細は 64 KiB ちょうどで省略し、ほかのページは 64 KiB ちょうどまで許す (`afx-ops/tests/web/test_approval_page_budget.py:115-134`)。
- **並びがアルファベット順**: `_pairs` は key を `sorted()` で出す (`render.py:107-119`)。
  - Status の上部は `autopilot, balance, data, equity, health, jobs, kill_switch, …` の順で、kill_switch が 7 番目に来る。
  - 承認詳細の概要は、英語 key の dl と日本語 dt の dl を同じ節に続けて並べている (`render.py:645-650`)。
- **承認一覧が nav に無い**: `/approvals` へは `/plugins` の先頭の link からしか辿れない (`render.py:57`)。
- **表を並べ替えられない**:
  - plugin 一覧は行数に上限が無く、残りの byte に収まる行だけを出す (`A/web/plugin_pages.py:130-156`)。
  - improve 一覧は byte で行を省くと、「さらに古い run」の link を CLI の案内に置き換える (`A/web/improve_pages.py:230-232`)。
- **書込みの form と自動再読込が衝突する**:
  - form がある画面は再読込を外す (`A/web/request.py:473-475`、`A/web/write_pages.py:40-47`)。
  - `inject_forms` は、元のページに再読込が無いと form を足さない (`write_pages.py:40-47`)。
  - 書込みが使える間は、improve 一覧は自動では更新されない。
- **ページ内を切り替える仕組みが無い**:
  - query は route ごとに受ける key が固定で、ほかの key は 400 (`request.py:173-184`、`:286-322`)。`?tab=` はどの画面でも 400 になる。
  - `<details>`・`id=`・`#` アンカー・script はどれも使っていない。
  - CSP は script を禁じ、style は `render.STYLE` の hash だけを許す (`A/web/headers.py:6-10`)。

利用者の方針 (2026-11-15): web は「配置の整理 → CSS のデザイン → チャートの配置」の順で進める。この束は配置の整理で、行うのは次の 3 つ。

1. 各ページの大項目をタブにまとめる。
2. plugin と improve の表を並べ替えられるようにし、表の中でスクロールさせる。
3. 内容の削除は後回しにし、整理だけを行う。

## 1. 何を作るか (動作)

### 1.1 タブ

- **帯**: 大項目のある画面では、h1 の下にタブの帯を置く。
  - 帯はページ内の link の列 (`<nav class="tabs">`) で、今のタブに `aria-current="page"` を付ける。
  - 帯は中身が空でも全部出し、空のタブには「なし」と出す。並びが画面ごとに変わらないようにするため。
- **切替**:
  - タブを選ぶと、`?tab=<名前>` の付いた URL を読み直す。web はそのタブの節だけを描き、JS は使わない。
  - `tab` を省いた URL は、既定のタブ (§1.2 の表の左端) を出す。`?tab=<既定>` も受け付け、同じ本文を返す。
  - タブの名前は route ごとの固定の表で決める。query の検査は §1.7 による。
- **自動再読込** (`<meta http-equiv="refresh">`): 今のタブの URL をそのまま読み直すので、選んだタブは保たれる。外すのは §1.5 と §1.6 の場合だけ。
- **予算**: §1.8 による。
- **書込みが使えるとき**: 書込みが使えるとは、write context があり、かつ経路が loopback か TLS (`WriteContext.transport_ok`、`A/web/write.py:300-303`) であること。
  - 使えないときは、書込み専用のタブ (Improve の「実行」) を帯に出さない。
  - その名前を URL で指定したら 404 を返す (今、書込みが無効なときの書込み route と同じ扱い)。
- **error と HEAD**:
  - error のページ (400 / 404 / 502 / 500) にはタブの帯を出さない (今の `render_error` のまま)。
  - HEAD は、GET と同じ検査・同じ status・同じ Content-Length を返し、本文は空にする。確認 record は作らない (今と同じ)。

### 1.2 画面ごとのタブ

項目は消さない。今の節をタブに振り分け、タブの中では今の節の順を保つ。並びを変えるのは §1.3 の 2 か所だけ。

| 画面 | タブ (左が既定) | 中身 (今の節) |
|---|---|---|
| Status `/` | `overview` 概要 / `bot` Bot / `econ` 経済指標 / `reaction` 反応統計 | <ul><li>概要: 上部の dl (§1.3 で並べ直す)</li><li>Bot: Bot の節</li><li>経済指標: Economic calendar + Economic signal</li><li>反応統計: Economic reactions (表 6 枚と末尾の dl)</li></ul> |
| Plugins `/plugins` | `list` 一覧 / 承認待ち | <ul><li>一覧: 今の `/plugins` の中身 (承認待ちの一覧への link、sources の警告、unknown_submissions、invalid_pending、表)</li><li>承認待ち: タブは `/approvals` への link</li></ul> |
| 承認待ち `/approvals` | (Plugins と同じ帯) | 今の `/approvals` の中身。帯は Plugins と共有し、「承認待ち」を今のタブとして出す |
| Plugin 詳細 `/plugins/{name}` | `overview` 概要 / `approvals` 承認 / `deps` 依存 / `usefulness` 有用性 / `versions` 版の履歴 | <ul><li>承認: 承認待ち + 承認の履歴</li><li>依存: 使う indicator + 参照する strategy</li></ul> |
| Improve `/improve` | `runs` 履歴 / `run` 実行 | <ul><li>履歴: 説明の段落、表、件数の注記</li><li>実行: 改善を実行の form (書込みが使えるときだけ、§1.6)</li></ul> |
| Improve 詳細 `/improve/{id}` | `overview` 概要 / `agent` agent / `result` 成績 / `handoff` 申し送り | <ul><li>概要: 概要 + 要約</li><li>agent: 仮説、agent の説明、観察文、判定理由、試したもの</li><li>成績: 床との差 (レポートからの表を含む) と配備中の版との比較</li><li>申し送り: 次回への申し送り</li></ul> |
| 承認詳細 `/approvals/{id}` | `overview` 概要 / `result` 成績 / `change` 変更 / `claims` 申告 | <ul><li>概要: 概要 (§1.3 で並べ直す)、indicator gate の警告、再評価 (前回の承認)、土台、履歴証跡、決定 (§1.5)</li><li>成績: 成績 + 配備中の版 (同条件で再評価)</li><li>変更: params の差分 + 変更 diff</li><li>申告: 申告</li></ul> |
| Activity `/activity` | 全件 / NEWS / TECH / AGGREGATE / TRADE / IMPROVE / APPROVAL / SYSTEM | 今ある `?category=` をそのままタブにする (`tab` という key は足さない)。全件は category を省いた URL |
| Log・書込みの画面・job・error | タブなし | 変更なし |

- **承認詳細の概要に注意の節を集める理由**: 承認・却下の form の前に、今と同じく gate・再評価・土台・履歴証跡の注意が並ぶようにする。これらは評価時点の注意、却下による配備停止、土台の不一致、次の操作を示す (`render.py:493-531`)。
- **変更内容の無い承認詳細** (`_unchanged_page`、`render.py:534-540`):
  - 概要には、今の dl と「変更内容」の固定文を出す。
  - 追加節は、通常と同じタブに置く (配備中の版は成績、ほかは概要)。
  - 変更と申告のタブは「なし」と出す。
- **Improve 詳細の例外の表示**:
  - 終端の記録が無いとき (`improve_pages.py:437-439`): 概要のタブに今の固定文を出し、ほかのタブは「なし」と出す。
  - 要約が無いときの固定文: 今どおり概要に出す。
- **ページ内の nav の段落**: plugin 詳細と improve 詳細にある段落 (`plugin_pages.py:350`、`improve_pages.py:436`) は、タブの帯の上にそのまま残す。
- **Status の `jobs`・`pnl`・`performance`**: 今の daemon は値を入れない (棚卸し §A)。§1.3 の並びの表には残し、値が無ければ今どおり行ごと出ない。

### 1.3 タブの中の並べ直し (2 か所)

1. **Status の概要**: `_pairs` のアルファベット順をやめ、3 つの小見出し (h2) に分ける。
   - 安全: `kill_switch`, `health`, `autopilot`
   - 資金: `equity`, `balance`, `pnl`, `performance`
   - 稼働: `mode`, `mission`, `data`, `orders`, `jobs`
   - 強調 (`kill_switch` の danger、`autopilot` の warn) は今のまま。入れ子 (NESTED_FIELDS) の中はアルファベット順のまま。
2. **承認詳細の概要**: 2 つの dl を、次の 3 つの小見出し (h3) の順に並べ直す。dt の文言は今のまま変えない (英語 key は英語のまま)。
   - 承認: `id`, `kind`, `status`, `decided_by`, `decided_at`
   - 対象: `plugin`, `kind` (change.kind), `分類`, `候補`, `比較対象`, `計算時刻`、続いて今の「前版なし」と reason の固定文
   - 内部値: `payload_sha256`, `baseline`, `parent_facts`, `asserted_actor`

固定の順は、今の allowlist (`STATUS_FIELDS`, `OVERVIEW_FIELDS`) と同じ集合を並べる表で持つ。allowlist と並びの表の集合が一致することを test で固定する。どちらかだけに key を足すと赤になる。

### 1.4 plugin 一覧と improve 一覧の表

**並べ替え**

- 列の見出しを link にし、`?sort=<値>&dir=asc|desc` で並べ替える。並べ替えはサーバ側で、取得した行に対して行う。
- 今の列の見出しに ▲ / ▼ を付ける。今の列の見出しを押すと向きが逆になる。ほかの列の見出しを押すと、その列の初めの向き (下表) で並ぶ。
- **同じ値の行**: 既定の並び (daemon の返した順) を保つ。desc でも同じ値の行の並びは逆にしない (安定 sort に `reverse=True` を使う)。
- **値の無い行**: 値が無い、または型が合わない行は、向きにかかわらず末尾に置き、その中は既定の並びで出す。数値の 0 は値として扱う。
- **byte 予算との順序**: 並べ替えたあとに byte 予算を当てるので、予算で省かれるのは並べ替えた順の末尾になる。
- **plugin の「有用性」の列**: 今は 1 つの見出しとセルに提出数と配備成功数を併記している (`plugin_pages.py:177`, `:182-183`)。見出しの中に「提出」「配備成功」の 2 つの link を独立に置く。

| 表 | `sort` の値 | 比べる値 (DTO) | 比べ方 | 初めの向き |
|---|---|---|---|---|
| plugin 一覧 | `name` | `name` | 文字列 | asc |
| | `kind` | `kind` | 文字列 | asc |
| | `state` | `entity.state` (配備の状態の code) | 文字列 | asc |
| | `pending` | `pending_count` | 整数 | desc |
| | `submitted` | `usefulness.submitted` | 整数 (indicator だけ。ほかは値無し) | desc |
| | `deployed` | `usefulness.deployed` | 整数 (同上) | desc |
| improve 一覧 | `run` | `id` | 整数 | desc |
| | `started` | `started_at` | 時刻 (UTC に換算して比べる。換算できなければ値無し) | desc |
| | `finished` | `finished_at` | 時刻 (同上) | desc |
| | `trigger` | `trigger` | 文字列 | asc |
| | `result` | `result_code` (同じ値の中は `reason_code` の文字列) | 文字列 | asc |
| | `target` | 対象の plugin 名 | 文字列 | asc |

- 文字列は code 値をそのまま比べる (日本語の表示名の順ではない)。DTO の field 名は実装の前にコードで確かめ、違えば表を直す。
- 既定 (`sort` を省いたとき) は今の並び。見出しに印は付けない。

**improve 一覧の件数の注記**

- 説明の段落の「新しい順に並べています」(`improve_pages.py:224`) は、並べ替えているときは「取得した最新 L 件を <列> の <向き> で並べています」に変える。並べ替えていないときは今のまま。
- 件数の注記の文は「最新 N 件を表示」から「取得した最新 L 件のうち S 件を表示」に変える。L は取得した件数、S は描けた行数。
- byte で行を省いたときは、今どおり「さらに古い run」の link を CLI の案内に置き換える (`improve_pages.py:230-232`)。
- 「さらに古い run」の link は `sort`・`dir` を引き継ぐ (§1.7)。
- 確かめる組合せ: `limit` = 50 と 100 × `has_more` × byte で省いたか否か。

**表の中のスクロール**

- 表を枠 (`<div class="scroll">`) で包み、枠の中で縦横にスクロールさせる。CSS だけで作る。
- 枠の高さは画面の 70% を上限にし、見出しの行は枠の上端に固定する (`position: sticky`)。
- 対象は plugin 一覧と improve 一覧の 2 つの表に限る。ほかの表は今のまま (CSS の段で揃える)。

### 1.5 承認詳細の決定 (概要のタブ)

- **form の場所**: 承認・却下の form は、概要のタブの末尾 (注意の節の後) に出す。ほかのタブには出さない。
- **form を出す条件**: 今と同じ (書込みが使える、status = pending、kind = plugin、digest が 64 桁の hex) (`request.py:489-497`)。
- **自動再読込**: 今と同じく、form を足せたときだけ外す。
  - form を足すとタブが 64 KiB を超えるときは、form を足さずに再読込を残す (`write_pages.py:47`)。
  - form を出す条件を満たさないとき (pending でない、digest が不正など) は再読込を残す。
- **承認が何に結び付くか**:
  - form が承認するのは、概要のタブを描いたときの `payload_sha256` (今と同じ)。daemon は承認のときにこの digest を照合する (`S/ops/service.py:2442`)。
  - payload が作成後に書き換わるのは、承認を試みたときの indicator gate の再判定 (`S/plugin/switch.py:1700-1729`) だけ。変わるのは `gate_warnings`・`similar_to`・`scale_columns` で、候補のコードは変わらない。
- **比較は digest で保証されない**:
  - 比較対象は、取得したときの live の symlink から選ぶ (`S/ops/change.py:107-121`)。params の差分と diff も、その比較対象から計算し直す (`change.py:494-513`)。
  - そのため、別の承認で live が切り替わると、同じ承認 id・同じ digest でも、比較対象・params の差分・diff が変わり得る。今も 1 ページを読み直す間には同じ時間差がある。タブに分けると、変更を見る要求と決定する要求が必ず別になる。
  - 概要のタブには、form の直前に次の 3 つを出す。
    - 今の比較対象・分類・計算時刻 (概要の dl の対象欄)。比較を取れない理由 (`REASON_TEXT`) も含む。
    - 「差分は表示した時点の配備中の版との比較で、配備中の版が変わると変わる。承認が結び付くのは候補の内容 (payload_sha256 の先頭 12 文字) だけ」という 1 文。
    - 変更のタブへの link。
- **候補の hash**: 概要の「候補」と同じ `change.candidate_hash` の先頭 12 文字 (`render.py:645-648`、`S/ops/change.py:459-466`)。値が無いときは今の概要と同じ表示。
- **確認の画面・確認 record・Origin・二重 POST の扱い**: 今のまま変えない。

### 1.6 Improve の「実行」タブ

- **書込みが使えるとき**:
  - 「実行」タブは form だけを描き、一覧を取得しない (daemon を呼ばない)。
  - 再読込は付けない。
  - form と確認の手順は今のまま。
- **書込みが使えないとき**: 「実行」タブは帯に出さず、`?tab=run` は 404。
- **「履歴」タブ**:
  - 書込みが使えても、30 秒ごとに再読込する (今は書込みが使えると外れている)。
  - 一覧の取得に失敗したときは、ほかの読む画面と同じく 502 の error ページを返す。
  - 今の「一覧を取得できません + form」の 200 のページ (`request.py:468-471`) は廃止する。form は「実行」タブで一覧と無関係に出るので、一覧の障害が改善の実行を妨げない。

### 1.7 query と link

**route ごとに受ける key**

検査は今の route ごとの厳しさを保ち、route の分岐の中で行う。全 route に共通の key 集合は作らない。

| route | 受ける key | 組合せの規則 |
|---|---|---|
| `/` | `tab` | — |
| `/plugins` | `tab` (= `list`)、`sort`、`dir` | `dir` は `sort` と一緒のときだけ。`sort` だけなら、その列の初めの向き |
| `/approvals` | `limit` (今のまま) | — |
| `/plugins/{name}` | `tab` | — |
| `/improve` | `tab`、`limit`、`sort`、`dir` | `limit`・`sort`・`dir` は「履歴」タブでだけ受ける。`tab=run` と一緒なら 400。`dir` の規則は `/plugins` と同じ |
| `/improve/{id}` | `tab` | — |
| `/approvals/{id}` | `tab` | — |
| `/logs` | `n` (今のまま) | — |
| `/activity` | `n`、`category` (今のまま) | — |

- 表に無い key、表に無い値は 400。不正な `tab` と未知の key を同時に指定しても 400。
- 整数 (`limit`・`n`) は今の字句と上限の検査 (`request.py:173-184`) を通す。
- 重複した key、空の key、`%`・`#` を含む target を 400 にする今の規則 (`request.py:86-100`) は変えない。
- 400 のとき、daemon は呼ばない。受け取った値は本文に写さない。GET と HEAD の両方で test する。

**link の組み立て**

- タブ・並べ替え・「さらに古い run」の link は、route の path、固定の表から選び直した enum の値、検査を通した整数を数字として書き直した値、の 3 つだけから組み立てる。受け取った raw の query は使わない。
- 値は固定の表から来るが、それでも `html.escape` を通す。
- 引き継ぐ値:
  - Improve の並べ替えの link: `limit` を引き継ぐ。
  - 「さらに古い run」の link: `sort` と `dir` を引き継ぐ。
  - Improve のタブの link: ほかの key を引き継がない。
  - Activity のタブの link: `n` を引き継ぐ。
  - Plugins の並べ替えの link: ほかの key を持たない。

### 1.8 予算

- **承認詳細**:
  - 縮め方と省き方は今の計算 (`compose_approval_page`) をそのまま使う。全タブの節を集めた仮想の 1 ページについて 1 回計算し、各タブはその結果のうち自分の節だけを出す。
  - そのため、どの節がどこまで縮むか、どの追加節が省かれるかは、どのタブで見ても今と同じになる。
  - 64 KiB の判定 (承認詳細は 64 KiB ちょうどで省略) は、実際に返すタブのページに掛ける。
  - 追加節を省いたときの「省略: 一部の節は…」の注記は、省いた節が属するタブに出す。
- **承認詳細の取得**: どのタブでも、今と同じく承認の本体と変更内容の 2 回を取得する。仮想の 1 ページの計算に申告と diff の大きさが要るため。
- **ほかの画面**:
  - 節ごとの予算と行の省き方は、今の値と計算のまま変えない。
  - タブの帯は骨格に数える。plugin 一覧と improve 一覧の残りの byte の計算 (`plugin_pages.py:130-156`、`improve_pages.py:227-228`) は、帯の分だけ小さくなる。
  - ページの上限 (64 KiB ちょうどまで許す) は今のまま。
- 予算を広げるのは別の束で行う。この束では、タブに分けても省き方が今と同じことを保つ。

### 1.9 nav と CSS

- **nav**: 上部の nav は今の 5 つのまま (Status, Plugins, Improve, Log, Activity)。今の画面の項目に `aria-current="page"` を付ける。`/approvals` と `/approvals/{id}` では Plugins に付ける。
- **CSS**: この束で足すのは次の 3 つだけ。`render.STYLE` に足し、`headers.STYLE_HASH` を更新する。
  - タブの帯 (横並び、今のタブの下線)
  - 表の枠 (`max-height`、`overflow: auto`)
  - 見出し行の固定 (`position: sticky`)
- 色・余白・ダークモードは次の段で扱う。

## 2. 作りの要点

- **タブの表は 1 か所**: route ごとに `(タブ名, 表示名, 書込み専用か)` の列を 1 つの表で持つ。query の検査、タブの帯、renderer の振り分けはこの表を読む。表と renderer の振り分けがずれたら test が赤になるようにする。
- **daemon は変えない**: ops API・DTO・scope は今のまま。変わるのは afx-ops の web だけ。

## 3. 裁定 (2026-11-15 利用者が決定)

1. 進め方は「配置の整理 → CSS のデザイン → チャートの配置」。この束は配置の整理。
2. 各ページの大項目をタブにする。plugin と improve の表は並べ替えと表の中のスクロール。内容の削除は後回し。
3. タブはサーバ側で描く (`?tab=`)。並べ替えもサーバ側 (`?sort=&dir=`)。JS は入れない (指揮者の推奨を利用者が了承)。

## 4. 受入

1. **タブの振り分け**:
   - §1.2 の表の各 route × 各タブで、出る節の集合と順序が表どおりになる (route × タブの表で test)。
   - 今の節は、どれかのタブにちょうど 1 回だけ出る (取りこぼしも重複も無い)。
   - 変更内容の無い承認詳細、終端の記録の無い improve 詳細の場合も、同じ表で確かめる。
2. **query の検査**:
   - §1.7 の表に無い key・値・組合せは 400 になり、daemon を呼ばない (GET と HEAD の両方)。
   - 書込みが使えないときの `?tab=run` は 404。
   - `tab` を省いた URL と `?tab=<既定>` は同じ本文を返す。
3. **自動再読込**: 次の表のとおり (各行を test する)。

| 画面とタブ | 状態 | 再読込 |
|---|---|---|
| 読む画面の各タブ | — | あり |
| 承認詳細の概要 | form を足せた | なし |
| 承認詳細の概要 | pending でない / kind が plugin でない / digest が不正 / 書込みが使えない | あり |
| 承認詳細の概要 | form を足すと 64 KiB を超える | あり (form なし) |
| Improve の履歴 | 書込みが使えるか否かを問わない | あり |
| Improve の実行 | 書込みが使える | なし |
| error のページ | — | 今のまま |

4. **承認の form**:
   - form は承認詳細の概要のタブにだけ出る。
   - form を出す条件を 1 つずつ外すと、form は出ない (今の test を概要のタブへ移す)。
   - form の直前に、比較対象・分類・計算時刻・比較についての 1 文・変更のタブへの link・`change.candidate_hash` の先頭 12 文字が出る。
5. **Improve の実行**:
   - 「実行」タブは一覧を取得しない。
   - 「履歴」タブで一覧の取得に失敗すると 502 になる。
   - 書込みが無効なとき、および有効でも経路が許されないとき (`transport_ok` が偽) は、「実行」タブが帯に出ず、`?tab=run` は 404 になる。
6. **並べ替え**:
   - §1.4 の表の各値 × 各向きで、並びが決定的になる。同じ値の行は既定の並びのまま、値の無い行と型の合わない行は末尾、0 は値として扱う。
   - 並べ替えたあとで byte 予算が当たる (予算で省かれるのは末尾)。
   - link が §1.7 のとおりに値を引き継ぐ。
   - improve の件数の注記は、`limit` = 50 と 100 × `has_more` × byte で省いたか否か、の組合せで確かめる。
7. **予算**:
   - 承認詳細の縮め方と省き方は、どのタブで見ても今の計算と同じになる。
   - 今の境界の test と縮める順の test (`test_approval_page_budget.py:101-110`、`:115-134`、`:285-315`) は、仮想の 1 ページの計算に対する test と、各タブのページの 64 KiB の判定に対する test に分けて移す。
   - 省略の注記は、省いた節が属するタブに出る。
8. **並べ直し**: Status の概要と承認詳細の概要の並びが §1.3 どおりになる。並びの表と allowlist の集合が一致する。
9. **CSS**: `render.STYLE` の hash と `headers.STYLE_HASH` が一致する (今の test)。
10. **実ブラウザ**: 自機のみ、SSH の転送経由で次が動くことを確かめる (header の扱いはブラウザでしか確かめられない。メモリ `browser-behavior-needs-real-browser-check`)。
    - タブの切替
    - 並べ替え
    - 表の中のスクロールと見出し行の固定
    - 自動再読込でタブが保たれること
    - 承認の form の送信と、改善の実行の form の送信

### test の変更の範囲

- **節の test**: 1 ページに全節が出ることを前提にした今の web の test (`afx-ops/tests/web/` の承認詳細・plugin・improve・Status の各 file) は、確かめている節を出すタブの URL を描くように書き換える。確かめる中身 (文言・上限・省略の注記) は変えない。
- **予算の test**: §4.7 のとおり分けて移す。予算の値は変えない。
- **書込みの test** (`test_write_request.py` ほか): form を承認詳細の概要と Improve の「実行」タブで確かめる形に移す。
  - 経路が許されないときの保護の test (`test_write_request.py:208-219`) は残す。
  - 「一覧を取得できません + form」の test は、§1.6 の 502 と「実行」タブの test に置き換える。

## 5. 残余

- **取得の回数**: タブを切り替えるたびに daemon に問い合わせる。回数は今の 1 ページと同じで、承認詳細は 2 回、ほかは 1 回、Improve の「実行」タブは 0 回。
- **improve の並べ替えの範囲**: 取得した範囲の中だけ。全件を並べたいときは CLI (`afx ctl improve runs`) を使う。
- **スクロールの位置**: タブを切り替えると先頭に戻る。
- **比較の時間差**: 承認詳細の変更を見たあと、概要で決定するまでの間に配備中の版が変わると、見た差分と承認時の比較が異なり得る。web はこれを止めず、概要のタブに 1 文で示す (§1.5)。承認時の土台の照合は daemon 側の既存の規則による。
- **次の段に回すもの**: 色・余白・ダークモード・スマホ幅、ほかの表のスクロール、内容の削除と簡略化、予算の拡大。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-15 | C1 | <ul><li>承認詳細の「決定」と「検査」のタブをやめ、form と注意の節 (gate・再評価・土台・履歴証跡) を概要のタブに集める</li><li>比較は digest で保証されないことを明記し、概要に比較対象と 1 文を出す。候補の hash は `change.candidate_hash`</li><li>再読込は form を足せたときだけ外す (受入表)</li><li>予算は仮想の 1 ページで今の計算を 1 回行い、タブで切り出す。承認詳細はどのタブでも 2 回取得する</li><li>並べ替えの値・比べ方・初めの向き・欠損・安定性の表。有用性の見出しに link を 2 つ</li><li>improve の件数の注記を「取得した最新 L 件のうち S 件」に</li><li>route ごとの key と組合せの表、link の組み立ての規則</li><li>書込みが使える (write context と transport_ok) の区別。「実行」タブは一覧を取得せず、履歴の失敗は 502</li><li>例外の表示 (変更内容なし、終端の記録なし) の振り分け</li></ul> | sol 1 周目 (Important 9 / Minor 1) | — |
| 2026-11-15 | C0 | 初版 | 利用者の方針 (タブ化・表の並べ替えとスクロール・削除は後回し) | 36765325 |
