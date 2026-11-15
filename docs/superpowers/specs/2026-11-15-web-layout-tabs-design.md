# 同梱 web の配置の整理 (web-layout-tabs) 設計 C0

**チケット:** `web-layout-tabs` (中、機能)。関連: `web-plugin-improve-hub` (今の画面群を作った束)、`web-ui-charts-equity-indicators` (チャートは後)
**状態:** C0 (2026-11-15)、方針は利用者が決定 (§3)

## 0. 何が起きているか

同梱 web の画面は、機能を足すたびに節を縦に積んできた仮置きの配置になっている (棚卸しは `tmp/review-weblayout/inventory.md`、A = afx-ops/afx_ops)。

- **縦に長い**: 承認詳細は最大 12 節、improve 詳細は 10 節、Status は節 5 つ (反応統計の表 6 枚を含む) が 1 ページに並ぶ。
- **1 ページ 64 KiB の予算を全節で分け合う** (`A/web/render.py:16`)。承認詳細は diff を 48 KiB に抑え、末尾の追加節は 15 KiB を超えると先頭から省く (`render.py:21-23`, `:840-853`)。
- **並びがアルファベット順**: `_pairs` は key を `sorted()` で出す (`render.py:107-119`)。Status の上部は `autopilot, balance, data, equity, health, jobs, kill_switch, …` の順で、kill_switch が 7 番目に来る。承認詳細の概要は英語 key の dl と日本語 dt の dl を同じ節に続けて並べている (`render.py:645-650`)。
- **承認一覧が nav に無い**: `/approvals` には `/plugins` の先頭の link からしか辿れない (`render.py:57`)。
- **表が長い**: plugin 一覧は行数に上限が無く、残りの byte に収まる行だけを出す (`A/web/plugin_pages.py:130-156`)。improve 一覧は 10 列近く、並べ替えはできない。
- **書込みの form と自動再読込が衝突する**: form がある画面は再読込を外す (`A/web/request.py:473-475`, `A/web/write_pages.py:40-47`)。書込みが有効な間、improve 一覧は自動では更新されない。
- **ページ内を切り替える仕組みは無い**: query は route ごとに受ける key が固定で、ほかの key は 400 になる (`request.py:174`, `:286-322`)。`?tab=` はどの画面でも 400。`<details>`・`id=`・`#` アンカー・script はどれも使っていない。CSP は script を禁じ、style は `render.STYLE` の hash だけを許す (`A/web/headers.py:6-10`)。

利用者の方針 (2026-11-15): web は「配置の整理 → CSS のデザイン → チャートの配置」の順で進める。この束は配置の整理で、次の 3 つを行う。

1. 各ページの大項目をタブにまとめる。
2. plugin と improve の表を並べ替えられるようにし、表の中でスクロールさせる。
3. 内容の削除は後回しにし、整理だけを行う。

## 1. 何を作るか (動作)

### 1.1 タブ

- 大項目のある画面の上部 (h1 の下) に、タブの帯を置く。帯はページ内の link の列 (`<nav class="tabs">`) で、今のタブに `aria-current="page"` を付ける。
- タブを選ぶと、`?tab=<名前>` の付いた URL を読み直す。web はそのタブの中身だけを描く。JS は使わない。
- `tab` を省いた URL は既定のタブ (各表の左端) を出す。`?tab=<既定のタブ>` も受け付ける。
- タブの名前は route ごとの固定の表で決める。表に無い名前は 400 にする (今の query の厳しさと揃える)。
- 自動再読込 (`<meta http-equiv="refresh">`) は今のタブの URL をそのまま読み直すので、選んだタブは保たれる。
- 1 ページ 64 KiB の上限はタブごとに掛かる。各節の byte 予算は今の値のまま変えない (広げるのは別の束)。予算に収まらないときに節を省く仕組み (`render.py:840-853`) は、タブの中だけで働く。
- タブの帯は中身が空でも全部出す。空のタブには「なし」と出す。並びが画面ごとに変わらないようにするため。
- 書込みが無効なときは、書込み専用のタブ (Improve の「実行」、承認詳細の「決定」) を帯に出さない。その名前を URL で指定したら 404 を返す (今、書込みが無効なときの書込み route と同じ扱い)。

### 1.2 画面ごとのタブ

項目は消さない。今の節をタブに振り分け、タブの中では今の節の順を保つ。並びを変えるのは §1.3 の 2 か所だけ。

| 画面 | タブ (左が既定) | 中身 (今の節) |
|---|---|---|
| Status `/` | `overview` 概要 / `bot` Bot / `econ` 経済指標 / `reaction` 反応統計 | 概要 = 上部の dl (§1.3 で並べ直す)、Bot = Bot、経済指標 = Economic calendar + Economic signal、反応統計 = Economic reactions (表 6 枚) |
| Plugins `/plugins` | `list` 一覧 / 承認待ち | 一覧 = 今の `/plugins` (警告の段落と表)。承認待ちのタブは `/approvals` への link (下記) |
| 承認待ち `/approvals` | (Plugins と同じ帯) | 今の `/approvals` の中身。帯は Plugins と共有し、「承認待ち」を今のタブとして出す |
| Plugin 詳細 `/plugins/{name}` | `overview` 概要 / `approvals` 承認 / `deps` 依存 / `usefulness` 有用性 / `versions` 版の履歴 | 承認 = 承認待ち + 承認の履歴、依存 = 使う indicator + 参照する strategy |
| Improve `/improve` | `runs` 履歴 / `run` 実行 | 履歴 = 説明の段落、表、「さらに古い run」の link。実行 = 改善を実行の form (書込みが有効なときだけ) |
| Improve 詳細 `/improve/{id}` | `overview` 概要 / `agent` agent / `result` 成績 / `handoff` 申し送り | 概要 = 概要 + 要約。agent = 仮説・agent の説明・観察文・判定理由・試したもの。成績 = 床との差 (レポートからの表を含む)・配備中の版との比較。申し送り = 次回への申し送り |
| 承認詳細 `/approvals/{id}` | `overview` 概要 / `result` 成績 / `change` 変更 / `claims` 申告 / `checks` 検査 / `decide` 決定 | 概要 = 概要 (§1.3 で並べ直す)。成績 = 成績 + 配備中の版 (同条件で再評価) + 再評価 (前回の承認)。変更 = params の差分 + 変更 diff。申告 = 申告。検査 = indicator gate の警告 + 土台 + 履歴証跡。決定 = 承認・却下の form (§1.5) |
| Activity `/activity` | 全件 / NEWS / TECH / AGGREGATE / TRADE / IMPROVE / APPROVAL / SYSTEM | 今ある `?category=` をそのままタブにする (`tab` という key は足さない)。全件 = category を省いた URL |
| Log・書込み画面・job・error | タブなし | 変更なし |

- 変更内容の無い承認詳細 (`_unchanged_page`、`render.py:534-540`) は、`overview` に今の dl と「変更内容」の固定文を出し、`checks` に追加節を出す。ほかのタブは「なし」。
- plugin 詳細と improve 詳細のページ内の nav の段落 (`plugin_pages.py:350`, `improve_pages.py:436`) は、タブの帯の上にそのまま残す。
- Status の `jobs`・`pnl`・`performance` は、今の daemon は値を入れない (棚卸し §A)。§1.3 の並びの表には残し、値が無ければ今どおり行ごと出ない。

### 1.3 タブの中の並べ直し (2 か所)

1. **Status の概要**: `_pairs` のアルファベット順をやめ、3 つの小見出し (h2) に分ける。
   - 安全: `kill_switch`, `health`, `autopilot`
   - 資金: `equity`, `balance`, `pnl`, `performance`
   - 稼働: `mode`, `mission`, `data`, `orders`, `jobs`
   - 強調 (`kill_switch` の danger、`autopilot` の warn) は今のまま。入れ子 (NESTED_FIELDS) の中はアルファベット順のまま。
2. **承認詳細の概要**: 2 つの dl を、次の 3 つの小見出し (h3) の順に並べ直す。dt の文言は今のまま変えない (英語 key は英語のまま)。
   - 承認: `id`, `kind`, `status`, `decided_by`, `decided_at`
   - 対象: `plugin`, `kind` (change.kind), `分類`, `候補`, `比較対象`, `計算時刻`、続いて今の「前版なし」・reason の固定文
   - 内部値: `payload_sha256`, `baseline`, `parent_facts`, `asserted_actor`

固定の順は、今の allowlist (`STATUS_FIELDS`, `OVERVIEW_FIELDS`) と同じ集合を並べる表で持つ。allowlist の集合と並びの表の集合が一致することを test で固定する (どちらかだけに key を足すと赤になる)。

### 1.4 plugin 一覧と improve 一覧の表

- **並べ替え**: 列の見出しを link にして、`?sort=<列>&dir=asc|desc` で並べ替える。並べ替えはサーバ側で、取得した行に対して行う。
  - 今の列の見出しに ▲ / ▼ を付ける。同じ列の見出しをもう一度押すと向きが逆になる。
  - 同じ値の行は既定の並び (下表) で並べ、順序を決定的にする。
  - 値が無い行は、向きにかかわらず末尾に置く。
  - 並べ替えたあとで byte 予算を当てる。予算に収まらずに省かれるのは、並べ替えた順の末尾になる。
  - `sort` と `dir` は列ごとの固定の表で受け、表に無い値は 400。
  - plugin 一覧の `sort` は `/plugins` の一覧タブでだけ受ける。improve 一覧では `limit` (今の key)、`tab`、`sort`、`dir` を併せて受ける。「さらに古い run を表示」の link は `sort` と `dir` を引き継ぐ。

| 表 | 並べ替えられる列 (`sort` の値) | 既定 |
|---|---|---|
| plugin 一覧 | 名前 `name` / kind `kind` / 配備 `state` / 承認待ち `pending` / 有用性の提出 `submitted` / 有用性の配備成功 `deployed` | 今の並び (daemon の返した順) |
| improve 一覧 | run `run` / 開始 `started` / 終了 `finished` / 起動 `trigger` / 結果 `result` / 対象 `target` | 今の並び (daemon の返した順) |

- improve 一覧の並べ替えは、取得した最新 `limit` 件 (既定 50、上限 100) の中だけで行う。これは表の上の説明の段落に 1 文で書く。
- **表の中のスクロール**: 表を枠 (`<div class="scroll">`) で包み、枠の中で縦横にスクロールさせる。高さは画面の 70% を上限にし、見出しの行は枠の上端に固定する (`position: sticky`)。CSS だけで作る。
- 表の中のスクロールは plugin 一覧と improve 一覧の 2 つに限る。ほかの表は今のまま (CSS の段で揃える)。

### 1.5 承認詳細の「決定」タブ

- 承認・却下の form は「決定」タブにだけ出す。form を出す条件は今と同じ (書込みが有効、status = pending、kind = plugin、digest が 64 桁の hex) (`request.py:489-497`)。
- form が承認するのは、「決定」タブを描いたときの `payload_sha256` (今と同じ)。daemon は承認のときにこの digest を照合する (`S/ops/service.py:2442`)。
- **中身が変わる範囲**: 承認の payload が作成後に書き換わるのは、承認を試みたときの indicator gate の再判定だけで、変わるのは `gate_warnings`・`similar_to`・`scale_columns` (`S/plugin/switch.py:1700-1729`)。コード・params・diff は同じ承認 id の間は変わらない。
- そのため「決定」タブには form の前に、承認の判断に要る最小限を出す。
  - 対象: `plugin`, `kind`, `候補` (content_hash の先頭 12 文字)
  - その時点の indicator gate の警告 (検査タブと同じ ul)
  - 「承認すると、このタブを開いた時点の内容 (payload_sha256 の先頭 12 文字) を承認する」の 1 文
- 「決定」タブだけは自動再読込を外す (今の form のある画面と同じ)。ほかのタブは書込みが有効でも再読込する。
- form を足すとそのタブが 64 KiB を超えるときの扱い (form を足さず、再読込を残す) は今のまま (`write_pages.py:47`)。

### 1.6 Improve の「実行」タブ

- 改善を実行の form は「実行」タブにだけ出す。form と確認の手順は今のまま。
- 「実行」タブは再読込を外す。「履歴」タブは書込みが有効でも 30 秒ごとに再読込する (今は書込みが有効だと外れている)。
- 一覧の取得に失敗したときの「一覧を取得できません + form」の画面 (`request.py:468-471`) は、「実行」タブでは form だけを出し、「履歴」タブでは「一覧を取得できません」を出す。

### 1.7 nav

- 上部の nav は今の 5 つのまま (Status, Plugins, Improve, Log, Activity)。今の画面の項目に `aria-current="page"` を付ける。`/approvals` と `/approvals/{id}` では Plugins に付ける。
- 見た目の強調は最小限の CSS (下線) にとどめ、作り込みは CSS の段で行う。

### 1.8 CSS

この束で足すのは、タブの帯 (横並び・今のタブの下線)、表の枠 (`max-height`・`overflow: auto`)、見出し行の固定 (`position: sticky`) だけ。`render.STYLE` に足し、`headers.STYLE_HASH` を更新する。色・余白・ダークモードは次の段で扱う。

## 2. 作りの要点

- **タブの表は 1 か所**: route ごとに `(タブ名, 表示名, 書込み専用か)` の列を 1 つの表で持つ。request の query 検査、タブの帯、renderer の振り分けは、この表だけを読む。表と renderer の振り分けがずれたら test で赤になるようにする。
- **取得はタブに合わせる**: 承認詳細では `approval_change` (変更内容) を、それを使うタブ (`overview` の対象欄、`result`、`change`、`claims`、`decide`) でだけ取る。ほかの route は今と同じ 1 回の取得で、描く節だけを選ぶ。
- **query の検査**: 今の `_positive` は名指しした key 以外を 400 にする。route ごとに受ける key の集合 (`tab`, `sort`, `dir`, `limit`, `n`, `category`) を表で持ち、検査を 1 関数にまとめる。重複した key、空の key、`%`・`#` を含む target を 400 にする今の規則 (`request.py:86-100`) は変えない。
- **link の生成**: タブと並べ替えの link は、route の path と固定の値だけから組み立てる (受け取った query を写さない)。値はどれも固定の表から来るので、escape の必要が無い形にとどめ、それでも `html.escape` を通す。
- **daemon は変えない**: ops API・DTO・scope は今のまま。変更は afx-ops の web だけ。
- **HEAD**: GET と同じ検査を通す (今と同じ)。

## 3. 裁定 (2026-11-15 利用者が決定)

1. 進め方は「配置の整理 → CSS のデザイン → チャートの配置」。この束は配置の整理。
2. 各ページの大項目をタブにする。plugin と improve の表は並べ替えと表の中のスクロール。内容の削除は後回し。
3. タブはサーバ側で描く (`?tab=`)。並べ替えもサーバ側 (`?sort=&dir=`)。JS は入れない (指揮者の推奨を利用者が了承)。

## 4. 受入

1. §1.2 の表の各 route × 各タブで、出る節の集合と順序が表どおり (route × タブの表で test)。今の節がどれかのタブに必ず 1 回だけ出る (節の取りこぼしと重複が無い) ことも、同じ表で確かめる。
2. 表に無い `tab`・`sort`・`dir`、受けない key は 400。書込みが無効なときの書込み専用タブは 404。`tab` を省いた URL と `?tab=<既定>` は同じ本文。
3. 選んだタブの URL で `<meta http-equiv="refresh">` が付く。「決定」「実行」では付かない。書込みが有効でも「履歴」タブには付く。
4. 承認の form は「決定」タブにだけ出る。form の条件 (§1.5) を 1 つずつ外すと form が出ない (今の test を「決定」タブへ移す)。「決定」タブに対象・gate の警告・digest の先頭 12 文字が出る。
5. plugin 一覧と improve 一覧で、各列 × 各向きの並びが決定的 (同じ値は既定の並び、値の無い行は末尾)。並べ替えのあとで byte 予算が当たる (予算で省かれるのは末尾)。「さらに古い run」の link が `sort`・`dir` を引き継ぐ。
6. Status の概要と承認詳細の概要の並びが §1.3 どおり。並びの表と allowlist の集合が一致する。
7. `render.STYLE` の hash と `headers.STYLE_HASH` が一致する (今の test)。
8. 実ブラウザで、タブの切替・並べ替え・表の中のスクロール・見出し行の固定・自動再読込でのタブの保持・承認の form の送信が動く (自機のみ、SSH の転送経由)。header の扱いはブラウザでしか確かめられない (メモリ `browser-behavior-needs-real-browser-check`)。

### test の変更の範囲

- 1 ページに全節が出ることを前提にした今の web の test (`afx-ops/tests/web/` の承認詳細・plugin・improve・Status の各 file) は、確かめている節を出すタブの URL を描くように書き換える。確かめる中身 (文言・上限・省略の注記) は変えない。
- 予算の test (`test_approval_page_budget.py` ほか) は、節の予算をタブの中で確かめる形に移す。予算の値は変えない。
- 書込みの test (`test_write_request.py` ほか) は、form を「決定」「実行」タブで確かめる形に移す。

## 5. 残余

- タブを切り替えるたびに daemon へ 1 回問い合わせる。loopback か LAN の 1 往復で、今の自動再読込と同じ程度の負荷。
- improve 一覧の並べ替えは取得した範囲の中だけ。全件を並べたいときは CLI (`afx ctl improve runs`)。
- タブの切替でスクロールの位置は先頭に戻る。
- 色・余白・ダークモード・スマホ幅、ほかの表のスクロール、内容の削除と簡略化は次の段。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-15 | C0 | 初版 | 利用者の方針 (タブ化・表の並べ替えとスクロール・削除は後回し) | — |
