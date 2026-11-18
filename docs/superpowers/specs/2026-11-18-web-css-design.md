# 同梱 web の CSS の段 (web-css-design) 設計 C0.1

**チケット:** `web-css-design` (中、機能)。関連: `web-layout-tabs` (配置の段、完了)、`web-ui-charts-equity-indicators` (チャートは次の段)
**状態:** C0.1 (2026-11-18)。簡略化の候補 (§6) と問い (§7) は未裁定

パスの略記: A = `afx-ops/afx_ops`、T = `afx-ops/tests`、S = `src/agentic_fx`。

## 0. 何が起きているか

- **CSS は 1 本、白地だけ**: `A/web/render.py:33-60` の `STYLE` (27 行、1,472 byte)。色は 10 個の直書き (`#fff`・`#1f2328` など) で、暗い配色は無い。
- **状態は読んで判断する**: Status の概要は dl の入れ子 (`latched False`、`failures 0`) で、正常か否かを色でも語でも示さない。強調は `kill_switch` が作動中 (`.danger`)、`autopilot` が真 (`.warn`) の 2 つだけ (`render.py:76`、`:130-132`)。
- **表は大きく、縞が無い**: `th, td` の余白は 0.25rem × 0.5rem、行の区別は罫線だけ (`render.py:54-55`)。数字は等幅でない。
- **前段が残したもの** (`docs/superpowers/specs/2026-11-15-web-layout-tabs-design.md` §5): 色・余白・ダークモード・スマホ幅、ほかの表のスクロール、内容の削除と簡略化、予算の拡大。

守る制約 (コードで確かめた):

| 制約 | 根拠 |
|---|---|
| CSS は `STYLE` を `<style>` に inline で出す 1 本だけ。CSP の `style-src` はその SHA-256 だけを許す | `render.py:159-167`、`A/web/headers.py:7-11` |
| hash の pin は 2 か所 (`headers.py:7`、`T/web/test_render.py:74`)。全ページの `<style>` が `STYLE` と一致することを test が見る | `test_render.py:114-124` |
| `style=""` 属性は CSP で効かない。test も `" style="` が無いことを見る | `test_render.py:124` |
| script・外部の CSS・font・画像は無い (`default-src 'none'`) | `headers.py:9-10` |
| 1 ページ 64 KiB。超えると本文は「Output clipped」 | `render.py:17`、`:170-175` |
| 承認詳細は diff 48 KiB、diff 以外 15 KiB。**15 KiB は骨格 (`STYLE` を含む) を数える** | `render.py:22-23`、`:1091-1094`、`:1158-1165` |
| 15 KiB と 48 KiB は daemon 側に同じ値の定義があり、test が突き合わせる | `S/ops/change_limits.py:35-37`、`T/web/test_approval_change.py:448-451` |
| plugin 一覧と improve 一覧の表の予算は、骨格を引いた残り | `A/web/plugin_pages.py:313-315`、`A/web/improve_pages.py:292-295` |
| 時刻は内部 UTC。表示は応答の表示 tz (`display_timezone`) に換算し、表記は `YYYY-MM-DD HH:MM:SS ±HH:MM` (本番は `+09:00`)。tz が欠けている・使えないときは UTC。tz を渡す読む画面は h1 (と帯) の直後に tz の行を出し、tz を渡さない画面 (error・書込み・Improve の実行) には行が無い | `A/display_zone.py:25-33`、`render.py:112-115`、`:152-156`、`T/web/test_display_zone.py:134` |
| HTML を出すのは `render.py`・`plugin_pages.py`・`improve_pages.py`・`write_pages.py`・`tabs.py`・`common.py` だけ。`request.py` は `render_error` を呼ぶだけ (`request.py:416`)、`runtime.py`・`auth.py` は HTML を持たない | grep |

## 1. 何が変わるか (動作)

利用者 (運用する本人) が画面で見るものを、次のように変える。JS は使わない。URL・タブ・並べ替え・form の手順は変えない。

1. **暗い配色**: OS の設定が暗いと、全画面が暗い配色になる。切替ボタンは無い。入力欄・ボタン・スクロールバーも OS の暗い見た目になる。
2. **要点のカード**: 概要のタブの先頭 (tz の行の下) に、要点を小さなカードの列で出す。カードは見出しと値 1 つ。対象は Status の 3 タブ (概要・Bot・経済指標)、Plugin 詳細・Improve 詳細・承認詳細の概要。下の dl と表は今のまま残る (消すかは §6 で選ぶ)。
3. **状態の印**: 状態は「記号 + 語」の印で出す。正常は「○」緑、注意は「△」黄、危険は「✕」赤、どれでもないものは色なし。色を見分けられなくても、記号と語で読める。
   - 例: Status の概要のカードに `kill switch ○ 解除`、`health △ 異常の記録 2 件`、`data ○ ready`。
   - 一覧の表では、plugin 一覧の「配備」列と improve 一覧の「結果」列に印が付く。
   - どの値がどの状態かは固定の表 (§2.3) で決め、表に無い値は必ず「? 不明」(色なし) になる。正常には倒さない。
4. **表**: 文字を小さく、余白を詰め、1 行おきに薄い地色を付ける。見出し行に地色。数字は等幅 (桁の幅が揃う)。幅が足りない表は、その節の中で横にスクロールする。
5. **節**: 各節 (`<section>`) を枠で囲み、節の区切りが見えるようにする。
6. **書込みの form**: 承認と却下の form の間を空ける。ボタンには状態の色を付けない (§2.6)。
7. **変わらないもの**: 表示時刻の規則、`.danger`・`.warn` の意味 (危険・注意)、項目の並び、タブ、文言、daemon の API。

## 2. 仕組み

### 2.1 配色の token

色は `:root` の custom property にだけ書き、ほかの規則は `var()` で読む。暗い配色は `@media (prefers-color-scheme: dark)` で token を差し替えるだけ。`:root` に `color-scheme: light dark` を宣言し、form の部品とスクロールバーを OS の配色に合わせる。

| token | 用途 | 明 | 暗 |
|---|---|---|---|
| `--bg` | 地 | `#fff` | `#0d1117` |
| `--fg` | 本文 | `#1f2328` | `#e6edf3` |
| `--mut` | dt・カードの見出し | `#57606a` | `#9198a1` |
| `--line` | 罫線・枠 | `#d0d7de` | `#3d444d` |
| `--soft` | 縞・th・pre・カードの地 | `#f6f8fa` | `#161b22` |
| `--link` | link・今のタブの下線 | `#0969da` | `#4493f8` |
| `--ok` / `--ok-bg` | 正常の文字 / 地 | `#116329` / `#dafbe1` | `#3fb950` / `#12261e` |
| `--warn` / `--warn-bg` | 注意の文字 / 地 | `#7d4e00` / `#fff8c5` | `#d29922` / `#2a2111` |
| `--bad` / `--bad-bg` | 危険の文字 / 地 | `#cf222e` / `#ffebe9` | `#ff7b72` / `#2d1316` |

- 実際に重なる文字と地の組は、明・暗のどちらも 4.5:1 以上 (付録 A。最小は明の危険の文字 × 危険の地の 4.67)。
- 注意の文字は今の `#9a6700` から `#7d4e00` に変える。今の値は注意の地の上で 4.52、縞の上で 4.57 と余裕が無い。意味 (注意) は変えない。
- 罫線 (`--line`) は装飾で、状態を伝えないので 4.5:1 の対象にしない。
- `.scroll th` の `background: #fff` (`render.py:59`) は `th { background: var(--soft) }` に置き換える。直書きを残すと暗い配色で見出し行だけ白くなる。
- **系列色 (チャート用) の token はこの段で出荷しない**。値だけ付録 B に決めておく。理由: 使わない token も `STYLE` の byte を食い (§2.5)、チャートの段は SVG 用の規則を足すので、どのみち `STYLE` と hash を変える。

### 2.2 class と selector

**行ごと・cell ごとの属性は足さない。** 表と dl の見た目は要素の selector と入れ物の class で付ける。cell に class を付けると、反応統計のタブ (最大 1,440 cell、実測) や一覧の行数に効くため。

| 対象 | selector | 効果 |
|---|---|---|
| 数字 | `body { font-variant-numeric: tabular-nums }` | 全画面の数字が等幅 |
| 表 | `table { font-size: 0.85rem }`、`th, td { padding: 0.15rem 0.45rem }` | 詰める |
| 縞 | `tbody tr:nth-child(even) { background: var(--soft) }` | 1 行おきの地色 |
| 見出し行 | `th { background: var(--soft) }`、`.scroll th { position: sticky; top: 0 }` | 今の固定はそのまま |
| 節 | `section { border: 1px solid var(--line); border-radius: 6px; padding …; overflow-x: auto }` | 枠で囲む。幅が足りない表は節の中で横にスクロール |
| 状態の印 | `.st` (丸い枠) + `.ok` / `.warn` / `.danger` | 印 |
| カード | `.kpi` (横並び、折り返す)、`.kpi div` (枠と地)、`.kpi b` (見出し) | 要点 |
| form | `form + form { margin-top: 1.5rem }`、`button, input { font: inherit; padding … }` | 間隔 |

- **新しい class は `.st`・`.ok`・`.kpi` の 3 つ**。`.danger`・`.warn` は今の意味のまま印にも使う (危険・注意)。今の `dt.danger`・`dd.danger`・`dd.warn` の見た目 (太字と地色) は保つ。
- **ほかの表のスクロール**: 横だけ、`section` の `overflow-x: auto` で扱う。枠の `div` は足さない (`<div class="scroll">` が一覧の 2 表だけにあることを見る test `T/web/test_plugin_tabs.py:336-344` はそのまま)。縦の枠と見出し行の固定は足さない。ほかの表は行数に上限がある (反応統計は 1 表 24 行、`render.py:373`)。
- **桁揃え**: この段は等幅までにする。小数の桁数を揃える・右に寄せる、は入れない (§7 の問い 5)。多くの cell は `a / b` や `trades=… PF=…` の複合で (`render.py:379-380`、`:818-820`)、等幅だけでは桁は揃わない。
- 下書きの `STYLE` は付録 D (全文)。2,957 byte で、今より +1,485 byte。上の表に無い規則 (見出しの大きさ、本文 14px、nav の今の項目の色) も付録 D にある。

### 2.3 状態の割当表

印は `_mark(level, text)` の 1 関数で作り、`<span class="st ok">○ 解除</span>` の形にする。記号は HTML の文字として出す (CSS の `::before` にしない。hash が合わず CSS が外れても記号が残る)。level は `ok`・`warn`・`danger`・中立 (class は `st` だけ) の 4 つ。記号は level ごとに 1 つ: 正常「○」、注意「△」、危険「✕」、中立「–」。表に無い値だけ「?」。1 つの項目で複数の列に当たるときは、危険 > 注意 > 正常 の順に先のものを採る。

**分類と描画を分ける。** `_mark` は渡された level と文字を描くだけで、値が既知かどうかは知らない。level を決めるのは項目ごとの分類関数 (`_classify_<項目>(生の値) -> (level, 語)`) で、責務は次のとおり。

- **生の値を、厳密な型で見る**。daemon の応答から取り出した値そのものを割当表に当てる。翻訳 (`plugin_text.label`)・代替表示 (`_word` の「-」、Bot の `unknown` への置き換え) は分類の後に行い、置き換えた後の値を分類に使わない。
- **未知と既知の `unknown` を分ける**。Bot の状態が表に無い語のとき、今の表示は `unknown` に置き換える (`render.py:225-227`) が、印は「? 不明」(中立) にする。daemon が実際に `unknown` を返したときだけ、表の `unknown` の列 (注意) になる。calendar も同じで、今は未知の値を `unknown` と出す (`render.py:280-281`) が、印は未知なら「? 不明」、既知の `unknown` なら「– unknown」。
- **真偽値は `is True`・`is False` だけ**。欠けている・`None`・整数 (`0`・`1`)・文字列 (`"false"`) は偽に丸めず「? 不明」。件数 (`failures`) は `type(v) is int` かつ 0 以上だけを値とし、bool・負・欠損は「? 不明」。
- 複数の field を見る項目 (kill switch・health) は、どれか 1 つでも不明なら、危険・注意が確定している場合を除いて「? 不明」。例: `latched` が真なら `reconcile_required` が欠けていても「✕ 作動中」。`latched` が偽で `reconcile_required` が欠けていれば「? 不明」(「○ 解除」にしない)。

| 画面 | 項目 | 正常 ○ | 注意 △ | 危険 ✕ | 中立 (色なし) |
|---|---|---|---|---|---|
| Status 概要 | kill switch | `latched` が偽 (照合待ちでない) →「解除」 | `latched` が偽で `reconcile_required` が真 →「照合待ち」 | `latched` が真 →「作動中」 | — |
| | health | `latched` が偽で `failures` が 0 →「正常」 | `latched` が真、または `failures` ≥ 1 →「異常の記録 N 件」 | — | — |
| | autopilot | — | 真 →「ON」(今の `.warn` と同じ) | — | 偽 →「– OFF」 |
| | data | `ready` | `degraded`・`restricted` | — | — |
| Status Bot | gateway health | `healthy` | `stale`・`unknown` | `unhealthy`・`stalled`・`permanent-failure` | `not_configured` |
| Status 経済指標 | calendar | `fresh` | `stale` | — | `unknown` |
| | worker | `alive` | — | `stopped`・`start_failed` | `not_started`・`unknown` |
| | slice の容量 | `ok` | `warn` | `full` | — |
| plugin (一覧の配備列、詳細のカード) | 配備 | `symlink`・`plain` | — | `unreadable` | `absent` |
| plugin 詳細 | 承認と読込 | `loaded` | `excluded`・`unapproved` | — | `none`・`unknown` |
| improve (一覧の結果列、詳細のカード) | 結果 | `submitted`・`report` | `gate_failed` | `failed` | `observation` |
| 承認詳細 | status | `approved` | `pending` (対応待ち) | — | `rejected`・`expired`・`invalidated` |

- **表に無い値・型が違う値は「? 不明」(中立)**。`plugin_text.label` と同じ規律で、未知の語を正常に倒さない (`A/plugin_text.py:59-65`)。
- 語の表示は今の表 (`plugin_text.ENTITY`・`ADMISSION`、`improve_pages.RESULT`、`render.BOT_HEALTH_STATES` など) をそのまま使う。新しい訳語は上の表の「」内だけ。
- 割当表はコードで 1 か所に持つ。**語彙の突き合わせは二段で test する**。
  1. daemon の語彙 → afx-ops の語彙の写し: リポジトリ直下の `tests/` (両 package を import できる側) で、daemon の定義と afx-ops の写しの集合が一致することを見る。今あるのは Bot (`T/web/test_bot_health_page.py:46` の golden)・plugin の語 (`T/web/test_plugin_pages.py:284` 付近)・経済指標の理由。足りない項目 (`data` の state、calendar・worker・容量の level、improve の `result_code`、承認の status) は T3・T4 で足す。daemon 側に 1 か所の定義が無い語彙 (`data` の state は `S/datafeed/outage.py` の文字列) は、その旨を test に書き、定義を 1 か所にまとめるかは実装で判断する。
  2. afx-ops の語彙の写し → 割当表: 割当表の語の集合が写しと一致することを見る。
  - 二段のどちらが欠けても「daemon が語を足したのに印の割当が無い」を検出できない。一段目が無い項目は、割当を「未検査」と test 名に残す。
- **入力例を test に固定する** (どれも「? 不明」・中立になる): 語が表に無い (`"paused"`)、型が違う (`1`・`None`・`[]`)、真偽値の欠損・`None`・`0`・`1`・`"false"`、`failures` が `True`・`-1`・欠損。
- `data` の語彙は `ready`・`degraded`・`restricted` の 3 つ (`S/datafeed/outage.py:153-160`、`:341-382`)。afx-ops に語彙の表が無いので、T3 で写しを置く。
- health の `latched` は「process の再起動まで異常を覚えている」印で、`failures` はその件数 (`S/core/health_latch.py:7-20`)。停止を意味するかは確かめていないので、危険にはしない (§7 の問い 2)。
- **承認詳細には、成績の判定 (配備中の版との比較) のカードを出さない。** 承認・却下の form と同じタブに緑や黄の合図を置くと、ボタンに色を付けない理由 (§2.6) と同じ誘導になる。判定は今どおり成績のタブにだけ出す。

### 2.4 HTML の構造変更

変えるのは次の関数だけ。dl・表・見出し・form の今の markup には属性を足さない。

| 関数 | 変更 |
|---|---|
| `render._document` (`render.py:159-167`) | `<html lang="en">` を `lang="ja"` に。viewport の meta を足すかは §7 の問い 4 |
| `render` に新設 `_mark`・`_cards` | 印とカードの列 (`<div class="kpi"><div><b>見出し</b>値</div>…</div>`) |
| `render._status_overview` (`:186-193`) | 先頭にカード: kill switch・health・autopilot・mode・equity・balance・data (mode・equity・balance は値だけで印なし) |
| `render.STATUS_TAB_SECTIONS` の `bot`・`econ` (`:197-202`) | 先頭にカード: Bot は gateway health、経済指標は calendar・worker・slice の容量 |
| `render._overview_section`・`_unchanged_virtual` (`:747-764`、`:1112-1120`) | 概要の節の先頭にカード: status・plugin・分類。仮想ページの概要に数える |
| `plugin_pages._overview` (`plugin_pages.py:329-341`) | 節の先頭にカード: 配備・承認と読込・承認待ちの件数 |
| `plugin_pages._list_row` (`:162-180`) | 「配備」の cell の語を印で包む |
| `improve_pages._overview` (`improve_pages.py:321-339`) | 節の先頭にカード: 結果・起動・経過 (秒) |
| `improve_pages._list_row` (`:241-248`) | 「結果」の cell の語を印で包む (理由の括弧は印の外) |

- カードの値は、その画面が今も出している値だけ。新しい field を allowlist に足さない。値が無い項目のカードは出さず、全部無ければ列ごと出さない。
- カードは tz の行の後ろ (本文の先頭) に置く。tz の行が無い画面にはカードも無い。h1 → 帯 → tz の行の並び (`test_display_zone.py:134`) は変えない。
- 一覧 (plugin・improve・承認待ち)、Log、Activity、書込みの画面、job、error にはカードを足さない。
- `lang="ja"` の理由: 今は英語と宣言していて、OS によっては漢字に別の言語の字形が選ばれる。

### 2.5 予算

`STYLE` と骨格は全ページが払う。増分に上限を置く。

- **上限: `STYLE` は 4,096 byte 以下** (今より +2,624 byte まで)。`len(STYLE.encode()) <= 4096` を test で固定する。下書き (付録 D) は 2,957 byte。
- **カードの列は 1 画面 512 byte 以下** を目安にし、最大の Status 概要で test する。
- **承認詳細が最もきつい**。骨格は今 2,080 byte で、15 KiB (15,360 byte) の中に数える。
  - **縮むかどうかは件数でなく byte で決まる**。申告は 1 項目 2,000 文字まで (`CLAIM_CHARS`、escape 後の文字数、`render.py:676-688`) で、ASCII なら 2 項目で約 4,000 byte、日本語 (1 文字 3 byte) なら約 12,000 byte。params は 1 件あたり key 80 文字 + 値 80 文字 × 2 まで (`VALUE_CHARS`) で、日本語なら最大約 720 byte。
  - 実測 (`compose_approval_page` を scratchpad から呼び、`STYLE` を伸ばして骨格の増分を模した)。入力は、params の key が `params.period_N`・値が 2 桁の整数、成績と配備中の版の比較は pair ごとに全項目あり、gate の行は 1 行 39 byte、親の記録は 2 項目。「今」は増分 0、「下書き」は +1,950 byte (STYLE +1,485、viewport 67、カード約 400)、「上限」は +3,200 byte (STYLE +2,624、viewport 67、カード 512)。cell は「params の表示件数 / 申告 1 項目の表示文字数」。

| 入力 | 今 | 下書き | 上限 |
|---|---|---|---|
| params 10 件・pair 2・gate 2 行、申告 ASCII 2,000 文字 × 2 | 10 / 2,000 (9,737 byte) | 10 / 2,000 (11,687) | 10 / 2,000 (12,937) |
| 同、申告 日本語 600 文字 × 2 | 10 / 600 (9,337) | 10 / 600 (11,287) | 10 / 600 (12,537) |
| 同、申告 日本語 1,000 文字 × 2 | 10 / 1,000 (11,737) | 10 / 1,000 (13,687) | 10 / 1,000 (14,937) |
| 同、申告 日本語 2,000 文字 × 2 (最大長) | **0 / 1,000** | 0 / 1,000 | 0 / 1,000 |
| params 30 件・pair 4・gate 8 行、申告 日本語 2,000 文字 × 2 | 0 / 1,000 | 0 / 1,000 (15,202) | **0 / 500** |
| params 64 件・pair 4・gate 16 行、申告 日本語 2,000 文字 × 2 | 0 / 1,000 | **0 / 500** | 0 / 500 |

  - 読み取れること: (1) 日本語で最大長の申告は、**今でも** params の差分を全部省き、申告を 1,000 文字に切っている。CSS の増分で新しく起きることではない。(2) ASCII の申告、または日本語で 1,000 文字までの申告なら、params 10 件・pair 2 の承認は上限いっぱいでも縮まない (残り約 420 byte)。(3) 増分が効くのは境界の近くで、申告の切り詰めが 1 段 (1,000 → 500 文字) 早まる。この 6 例では親の記録と追加節 (配備中の版の比較・gate の警告) は省かれなかった。
  - 上の 6 例は保証ではない。pair が多い・params の値が長い・gate が 16 行で長文、の組合せでは、親の記録と追加節まで省かれる (受入 §4 の 7 で例を作って確かめる)。
  - 超えたときの縮め方は今のまま: params の差分 → 申告 → 親の記録 → 末尾の追加節 (配備中の版の比較が先、gate の警告が次) の順 (`render.py:1154-1165`、`:956-966`)。
  - 15 KiB を広げるかは §7 の問い 1。この設計の既定は「広げない」。
- **一覧**: 表の予算は骨格を引いた残りなので、`STYLE` が増えた分だけ自動で減る。plugin 一覧は 1 行が平均 206 byte (fixture `T/fixtures/plugins_list.json` で実測)。64 KiB に達するのは約 300 行で、`STYLE` +1.5 KiB で約 7 行、印 (1 行 +約 30 byte) で約 40 行減る。improve 一覧は最大 100 行 (`improve_pages.py:48`) で、行の大きさは測っていない。
- **反応統計のタブ**: 最大の入力 (`T/web/test_reaction_status.py:109-120` と同じ) で今 29,399 byte。cell に属性を足さないので増えるのは骨格の分だけ。
- **Plugin 詳細・Improve 詳細**: 節ごとの上限 (`plugin_pages.py:35-37`、`improve_pages.py:54-55`) はタブに分かれたあと合計しないので、余裕がある (最大のタブで 26 KiB + 骨格)。
- `PAGE_LIMIT_BYTES`・`DIFF_BUDGET_BYTES`・`OTHER_BUDGET_BYTES`・節ごとの上限は変えない。

### 2.6 書込みの form

対象は `A/web/write_pages.py` の 5 つの form: 承認・却下 (`:22-32`)、改善の実行 (`:45-54`)、確認の「実行する」(`:74-81`)、「結果を確かめる」(`:108-110`)、job の「取消へ進む」(`:157-158`)。

- **markup は変えない**。見た目は CSS だけで変える。hidden の値・action・ボタンの文言・順序はそのまま。
- **承認と却下を離す**: 2 つの form は今、縦に続けて並ぶ。`form + form` に上の余白を足し、押し間違いを減らす。左右には並べない (並べるとスマホ幅で順序が変わる)。
- **ボタンに状態の色を付けない**。理由: どちらが危険かは承認の種類で変わる。再評価の依頼では、却下が配備の停止になる (`render.py:548-551` の `reevaluation_text.reject_text`)。承認を緑・却下を赤にすると、この場合に逆の合図になる。誤操作は今の 2 段階 (確認の画面) で止める。
- 暗い配色では、入力欄とボタンは `color-scheme` で OS の部品の色になる。文字が読めることを実ブラウザで確かめる (§4)。
- 決定のブロック (`render.py:1021-1044`) の中身と、form を足す条件・64 KiB の判定 (`write_pages.py:35-42`) は変えない。

## 3. 失敗時・運用

- **hash のずれ**: `STYLE` と `STYLE_HASH` がずれると、ブラウザは CSS を全部捨てる。画面は素の HTML になるが、印は記号と語なので読める。ずれは test (`test_render.py:114-124`) が止める。`STYLE` と hash 2 か所は必ず同じ commit で変える。
- **未知の状態語**: daemon が新しい語を返すと、印は「? 不明」(色なし)、語は今どおり「不明 (<語>)」。正常の色にはならない。
- **古いブラウザ**: custom property か `prefers-color-scheme` を解さないブラウザでは、色が付かないか明の配色のまま。内容は読める。対応はしない。
- **反映**: web は起動時に `STYLE` を読むので、web の process を再起動して反映する。開いたままの画面は次の読み直し (30 秒) で新しい CSS になる。daemon の再起動は要らない。
- **戻し方**: task ごとに 1 commit を revert する。daemon・DB・設定に変更は無い。
- **印の色と実際の危険のずれ**: 割当表は表示だけで、発注・停止の判断には使わない (決定論のコアは daemon 側)。

## 4. test と受入

### 今の test への影響

web の test は 646 件 (`T/web/`)。markup を見る箇所を grep で数えた (行数 / file 数): `<dl|<dt|<dd` 54 / 11、`<table|<th|<td|<tr` 31 / 8、`<h1〜4` 61 / 11、`class=` 30 / 8、`<nav|aria-current` 40 / 6、`<form|<button|<input|<label` 26 / 4、`<p` 21 / 7、`<pre` 8 / 2、`<section` 8 / 2、`STYLE` 10 / 3。リポジトリ直下の `tests/` では `tests/ops/test_change_web_e2e.py` に 9 行 (h2・h3・dt・dd・pre)。

dl・表・見出し・form の markup に属性を足さないので、書き換えが要るのは次だけ。

| 変更 | 書き換える test | 中身 |
|---|---|---|
| `STYLE` | `headers.py:7`、`test_render.py:74` | hash の値 |
| `.scroll th` の背景を token に | `T/web/test_tabs.py:202-203` | `background: #fff` を見る正規表現 |
| Status のカード | `test_render.py:154-161` | 「偽のとき本文に `class=` が無い」を「dl の中に `class=` が無い」に |
| plugin 一覧の印 | `T/web/test_plugin_pages.py:110`、`:118` | 「配備」の cell の文字列の一致 |
| improve 一覧の印 | `T/web/test_improve_pages.py:113` ほか、結果の語を cell で見る箇所 | 語の一致 (T4 で grep し直す) |

変わらないことを確かめる pin (そのまま通る想定): `test_display_zone.py:134` (h1・帯・tz の行)、`test_econ_status.py:52`・`test_bot_health_page.py`・`test_reaction_status.py:44-45` (属性の無い dt・dd・td)、`test_plugin_tabs.py:298-299` (th)、`test_plugin_tabs.py:336-344` (スクロールの枠)、`test_approval_tabs.py:108-119` (概要の dt の並び。カードは dt を使わない)、`tests/ops/test_change_web_e2e.py`。

### 足す test

1. **配色**: `STYLE` の中で色の直書き (`#` で始まる値) が `:root` の 2 つの block にだけある。
2. **コントラスト**: 付録 A の組を `STYLE` の token から計算し、明・暗とも 4.5 以上。
3. **大きさ**: `STYLE` が 4,096 byte 以下。Status 概要のカードの列が 512 byte 以下。
4. **割当表**: §2.3 の各値 → level と記号。表に無い値・型違いは「? 不明」で中立。割当表の語の集合が語彙の表と一致。
5. **色だけに頼らない**: 印は必ず記号と語を持つ (`_mark` の出力に文字がある)。
6. **カード**: 各画面でカードが tz の行の後ろ・節の前 (または節の先頭) にあり、値が無ければ出ない。カードの値は escape 済み。
7. **予算**: 承認詳細の縮む順が今と同じ (今の境界の test は骨格を `_document` から測るので、値を直さずに通る想定。`test_approval_page_budget.py:101-136`)。加えて、新しい骨格で次の例を作り、省略が画面で分かることを見る。
   - **params だけ縮む例**: 変更のタブに「…(N 件省略)」が出る (`render.py:799-801`)。決定のブロックの規模は省く前の件数を出す。
   - **申告まで縮む例** (日本語の最大長): 申告のタブの各項目の末尾に「…」が付き、0 文字まで縮んだときは「…(省略)」になる (`render.py:825-826`)。決定のブロックは「申告: あり」のまま。
   - **親の記録まで省く例**: 概要の内部値から `parent_facts` の行が消える。
   - **追加節まで省く例** (配備中の版の比較、gate の警告): 省いた節が属するタブに「省略: 一部の節は表示容量の都合で省きました」が、残った節と form より前に出る (`render.py:948`、`:1074-1077`)。
   - **確認の経路**: どの例でも、省かれた中身を読める経路が画面から分かること。今ある案内は params の省略印と追加節の省略の注記だけで、申告と親の記録には CLI の案内が無い。足すかは T4 で文言と byte を見て決め、足さないなら残余 (§8) に書く。
   - 各例で、form が付くか (64 KiB の判定)・再読込の有無が今の規則どおり。
8. **form**: 書込みの画面の markup が変わらない (今の test)。

判定は `tmp/tools/judge.sh` 経由。`T/web/` に加えて `tests/ops/test_change_web_e2e.py` と `tests/ops_setup/` (web の起動) を含める。

### 実ブラウザの受入 (利用者)

配色・`color-scheme`・CSP は test で見られないので、利用者が実ブラウザで確かめる。

- **経路**: 自機 (SSH の転送) で全項目。LAN 越し (Basic + TLS) は、viewport を入れる (§7 の問い 4) ときだけ、スマホで Status の概要と承認詳細の概要の 2 画面を見る。配色と CSP は経路で変わらない (`headers.py:22-38` は経路を見ない) ので、LAN で全項目を繰り返さない。
- **明・暗の両方** (OS の設定を切り替える) で次を見る:
  1. Status の 4 タブ: カード、印の色と記号、縞、反応統計の表の横スクロール。
  2. plugin 一覧・improve 一覧: 印、並べ替え、表の中のスクロールで見出し行が透けない。
  3. 承認詳細の 4 タブ: カード、diff と申告の `<pre>` が読める、決定のブロック。
  4. 書込み: 承認の form → 確認 → 結果、改善の実行の form。入力欄の文字とボタンが暗い配色で読める。承認と却下が離れている。
  5. error のページ (存在しない path)。
  6. ブラウザの console に CSP の違反が出ない。
- kill switch が作動中・Bot が unhealthy などの異常の見た目は、本番で起こさない。fixture から描いた HTML を file で開いて見る (CSP は効かないが配色は同じ)。

## 5. task 表

`STYLE` を触る task は hash の 2 行を書き換えるので、直列に進める。各 task は単独で merge できる。

| task | 内容 | 触る file | markup |
|---|---|---|---|
| T1 | token 化と暗い配色。`color-scheme`、`lang="ja"`、(問い 4 が可なら viewport)。足す test 1〜3。見た目は明でほぼ同じ | `render.py`、`headers.py`、`test_render.py`、`test_tabs.py` | 骨格だけ |
| T2 | 運用画面風の CSS: 表を詰める・縞・等幅・節の枠・横スクロール・form の間隔 | `render.py`、`headers.py`、`test_render.py` | 不変 |
| T3 | 印とカードの仕組み (`_mark`・`_cards`・割当表・`.st`・`.ok`・`.kpi`) と Status の 3 タブ。`data` の語彙の写しと、daemon 側との突き合わせの test | `render.py`、`headers.py`、`test_render.py` ほか | Status |
| T4 | Plugin 詳細・Improve 詳細・承認詳細のカード、一覧 2 表の印。`STYLE` は変えない | `render.py`、`plugin_pages.py`、`improve_pages.py`、test | 3 画面 + 一覧 |
| T5 | 簡略化。§6 で利用者が選んだ項目だけ。選ばれなければ行わない | 選ばれた項目による | 選ばれた項目 |
| T6 | 実ブラウザの受入 (§4)。fixture から異常の画面を描く手順を添える | — | — |

- T1・T2 は markup を変えないので、test の書き換えは hash と `test_tabs.py:202-203` だけ。
- T5 は T3・T4 の後。§6 の K1〜K4 はカードが出ていることが前提。

## 6. 簡略化の候補 (未裁定)

各画面を読んで出した候補。**どれも未裁定で、利用者が選んだものだけを T5 に入れる。** 選ばれなかった項目は今のまま。消しても daemon の API (`GET /v1/status` など) には値が残る。

**異常・失敗・古い値・未測定・警告を示す値は、消す候補にしない。** 省く案は「0 のときだけ省き、非 0 と不明は必ず出す」の形にした。

「たたむ」は `<details>` を使う。ただし 30 秒の自動再読込のある画面では、開いても次の読み直しで閉じる (§7 の問い 3)。表の「再読込」の列がその印。

| # | 画面 | 項目 | 案 (★ = 推奨) | 理由 | 失う情報 | 再読込 |
|---|---|---|---|---|---|---|
| K1 | Status 概要 | `kill_switch` の dl の行 (解除で照合待ちでもないとき) | ★消す (作動中・照合待ちのときは出す) / 残す | カードが「○ 解除」を出す。残るのは `generation`・`latched False`・`reconcile_required False` | 平常時の `generation` | あり |
| K2 | Status 概要 | `health` の dl の行 (`latched` 偽・`failures` 0 のとき) | ★消す (異常のときは出す) / 残す | カードと同じ内容 | なし (カードにある) | あり |
| K3 | Status 概要 | `autopilot`・`mode` の dl の行 | ★消す / 残す | カードと同じ値 | なし。`autopilot` の `dt.warn`・`dd.warn` の強調は印に置き換わる | あり |
| K4 | Status 概要 | 資金の群 (`equity`・`balance`) | ★消す / 残す | カードと同じ値。群に残る行が無くなり見出しも消える | なし | あり |
| K5 | Status 概要 | allowlist の `jobs`・`pnl`・`performance` | ★消す (allowlist と並びの表から外す) / 残す | daemon は値を入れない (`S/service.py:2220-2224`)。画面には今も出ていない | 将来 daemon が入れたとき、自動では出ない | — |
| K6 | Status 経済指標 | `abandoned <理由> (latest day / total)` の 9 行 | ★値が 0 だけの行を消す / たたむ / 残す | 平常時は 0 が並び、0 でない行が埋もれる | 「0 である」ことの明示。行の数が日によって変わる | あり |
| K7 | Status 経済指標 | `slice <key>` の 21 行 | ★計算の内訳 4 行 (`base_bytes`・`bytes_per_row`・`db_bytes`・`wal_bytes`) だけ消す / 残す | 4 行は `used_bytes` の内訳で、異常の合図にならない。**残す 17 行**: 3 つの level、`rows`・`row_capacity`、`used_bytes`・`byte_capacity`、`free_bytes` (空き容量)、`slice_bytes`・`slice_bytes_measured_at`・`slice_bytes_stale` (未測定・古い値)、`measure_outcome`・`measure_elapsed_sec` (測定の失敗)、`signal_daily`・`signal_producer`・`signal_override` と `capacity_override_below_producer` (設定の警告)、`slice capacity warning` | DB と WAL の大きさの内訳 | あり |
| K8 | Status 経済指標 | `shadow <組>` の行 (最大 50) | ★残す / たたむ | 見送った判断の仮想損益で、判断の材料 | (たたむなら) 初めの表示で見えない | あり |
| K9 | Status 反応統計 | 6 表の列 `unique_ts_n`・`未算定` | ★残す / 表の全行で `未算定` が 0 かつ `unique_ts_n` が `n` と同じときだけ列を省く (1 行でも非 0・差あり・不明があれば出す) | 欠損や停滞で未算定が増えると、残った標本の中央値だけが見える。今の「印」は `n<床` しか見ない (`render.py:383-385`) ので、この 2 列が統計の信頼性の材料 | (省く案でも) 正常時に「0 である」ことの明示 | あり |
| K10 | Status 反応統計 | 末尾の dl (`reaction sets_without_values`・`events_without_currency_key`) | ★残す / 値が 0 の行だけ省く (非 0 と不明「-」は必ず出す) | 集計の滞留を示す値で、非 0 は統計が遅れている合図 | (省く案でも) 正常時に「0 である」ことの明示 | あり |
| K11 | 承認待ち一覧 | 1 件ごとの節と dl (`content_hash`・`created_at`・`id`・`kind`・`name`) | ★1 枚の表にまとめ、`content_hash` は先頭 12 文字 / 残す | 件数が増えると縦に長い。ほかの画面は hash を 12 文字で出す | `content_hash` の 13 文字目以降 | あり |
| K12 | 承認詳細 概要 | 内部値の群 (`payload_sha256` の全桁・`parent_facts`) | たたむ / ★残す | 照合用の値。決定のブロックに先頭 12 文字がある | (たたむなら) 初めの表示で見えない | form があるときは無し、ほかはあり |
| K13 | Plugins 一覧 | 先頭の link「承認待ちの一覧 (Pending approvals)」(`plugin_pages.py:290`) | ★消す / 残す | 帯の「承認待ち」と同じ先 | なし | — |
| K14 | Plugins 一覧 | 列「配備中の版」(hash 12 文字) | 消す / ★残す | 詳細の概要にある。10 列で横に長い | 一覧で版を見比べること | — |
| K15 | Plugin 詳細・Improve 詳細 | 帯の上の link の段落 (`plugin_pages.py:465`、`improve_pages.py:518`) | ★消す / 残す | 上部の nav (Plugins・Improve) と Plugins の帯 (承認待ち) に同じ先がある | 詳細から承認待ちの一覧へ 1 回で行く link (Plugins 経由で 2 回になる) | — |
| K16 | Improve 一覧 | 並べ替えていないときの説明の段落 (`improve_pages.py:272`) | ★消す / 残す | 見出しの印と link で分かる | 「run の番号から要約を開ける」の案内 | — |
| K17 | Improve 詳細 成績 | 「床 (閾値)」の dl 5 行 (`improve_pages.py:399-408`) | たたむ / ★残す | 設定値で、run ごとにほぼ同じ | (たたむなら) 初めの表示で見えない | あり |

候補にしなかったもの: tz の行 (時刻の規則)、決定のブロックの再掲 (前段の裁定)、承認詳細の注意の節 (gate・再評価・土台・履歴証跡)、Bot の注記の段落、英語の dt の和訳 (簡略化でなく文言の変更)。

## 7. 利用者に問う事項

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | 承認詳細の 15 KiB をどうするか。(a) 変えず、`STYLE` を 4,096 byte 以下に抑える。(b) diff を 46 KiB、diff 以外を 17 KiB に振り替える | (a) | 縮むかは件数でなく byte で決まり、日本語で最大長の申告は今でも params を全部省いて申告を 1,000 文字に切る (§2.5 の実測)。(a) の増分は、この境界を最大 3,200 byte 手前にずらし、申告の切り詰めが 1 段早まる例がある。ASCII か 1,000 文字までの申告で params 10 件・pair 2 なら縮まない。(b) は増分を打ち消すが、daemon 側の定義 (`S/ops/change_limits.py:36-37`) と 2 package の test を動かし、長い diff の見える量が 2 KiB 減る。日本語の長い申告が縮む問題は (b) の 2 KiB では解けない (不足は約 8 KiB) ので、予算の見直しとして別チケットにする |
| 2 | 状態の割当表 (§2.3) はこれでよいか。迷うのは 4 つ: `autopilot` が真 = 注意、improve の `gate_failed` (見送り) = 注意、承認の `pending` = 注意、health の異常の記録 = 注意 (危険にしない) | 表のとおり | `autopilot` は今の `.warn` の意味を保つ (変えるなら意味の変更になる)。見送りは失敗ではないが、床を割った合図。承認待ちは人の対応が要る。health の記録は再起動まで残るので、赤にすると解消後も赤いままになる。危険 (赤) は停止・故障だけに絞り、慣れで見落とさないようにする |
| 3 | 「たたむ」を使うか | 使わない (消すか残すかで選ぶ) | 再読込のある画面では 30 秒で閉じ、読んでいる途中で消える。開閉を保つには query を足す必要があり、タブの表と検査が増える。§6 の推奨はこれに合わせてある |
| 4 | スマホ幅: viewport の meta (`<meta name="viewport" content="width=device-width,initial-scale=1">`、67 byte) を入れるか | 入れる。専用の配置は作らない | 今はスマホで PC 幅の縮小表示になり、字が読めない。入れれば字の大きさが合い、広い表は節の中で横にスクロールする (§2.2)。カードは折り返す。入れるなら受入に LAN 越しの 2 画面を足す (§4) |
| 5 | 桁揃えをどこまでやるか。(a) 等幅だけ。(b) 小数の桁数を揃え、数値の列を右に寄せる | (a) | (b) は値の書式の変更 (`_num` は `str(float)`、`render.py:695-698`) で、複合の cell (`a / b`) を列に割る必要がある。cell に class が要り、予算にも効く。チャートの段で数値の見せ方を決めるときに一緒に扱う |

## 8. 残余・やらないこと

- **チャート**: 次の段 (`web-ui-charts-equity-indicators`)。系列色の値だけ付録 B に置く。
- **diff の色分け**: 入れない。行ごとに要素が要り、diff の 48 KiB を食う。
- **全画面に kill switch の状態を出す帯**: 入れない。Status 以外の画面で daemon への問い合わせが 1 回増える。
- **英語の dt の和訳、項目の並べ替え**: 入れない (文言と配置はこの段で変えない)。
- **スマホ専用の配置、印刷、`forced-colors`**: 入れない。
- **予算の拡大**: §7 の問い 1 の (a) なら行わない。
- **カードと dl の重複**: §6 の K1〜K4 を選ばなければ、同じ値がカードと dl の 2 か所に出る。
- **`section` の横スクロールと節の枠**: 節の外にある表 (plugin 一覧・improve 一覧は `.scroll` の枠) は今のまま。

## 付録 A. コントラスト比

WCAG の式で計算した (scratchpad の script)。4.5 以上が目安。

| 文字 × 地 | 明 | 暗 |
|---|---|---|
| 本文 × 地 / 縞 | 15.80 / 14.84 | 16.02 / 14.64 |
| 本文 × 正常の地 / 注意の地 / 危険の地 | 14.19 / 14.66 / 13.78 | 13.45 / 13.42 / 14.61 |
| dt × 地 / 縞 | 6.39 / 6.00 | 6.50 / 5.94 |
| link × 地 / 縞 | 5.19 / 4.88 | 6.11 / 5.58 |
| 正常 × 地 / 縞 / 正常の地 | 7.39 / 6.94 / 6.64 | 7.45 / 6.81 / 6.25 |
| 注意 × 地 / 縞 / 注意の地 | 7.09 / 6.66 / 6.58 | 7.50 / 6.85 / 6.28 |
| 危険 × 地 / 縞 / 危険の地 | 5.36 / 5.03 / 4.67 | 7.51 / 6.86 / 6.85 |

今の値の参考: 注意 `#9a6700` × 注意の地 4.52、× 縞 4.57。危険 `#cf222e` × 危険の地 4.67 (変えない)。

## 付録 B. 系列色 (チャートの段のための予約。この段では出荷しない)

図形は 3:1 以上が目安 (WCAG 1.4.11)。下の値は地と縞の両方で 4.7 以上。

| 系列 | 明 | 地 / 縞 | 暗 | 地 / 縞 |
|---|---|---|---|---|
| 1 | `#0969da` | 5.19 / 4.88 | `#4493f8` | 6.11 / 5.58 |
| 2 | `#bc4c00` | 5.03 / 4.73 | `#f0883e` | 7.48 / 6.83 |
| 3 | `#1a7f37` | 5.08 / 4.77 | `#3fb950` | 7.45 / 6.81 |
| 4 | `#8250df` | 5.05 / 4.74 | `#ab7df8` | 6.33 / 5.78 |

系列 3 は正常の緑に近い。チャートでは系列も色だけで区別しない (線種か凡例の記号を併記する) ことを、次の段の設計で決める。

## 付録 C. 文書とコードの食い違い

コードの書き方に従った。見つけたものは次の 7 件。

1. チケット `web-css-design` の「STYLE (約 25 行)」。コードは 27 行・1,472 byte (`render.py:33-60`)。
2. `2026-11-15-web-layout-tabs-design.md` は題名が「C2.2」、状態の行が「C2.1」。変更履歴の最新は C2.2。
3. `2026-10-19-approval-content-design.md:152` は、15 KiB を超えるとき「params 差分 → 申告 の順で省略」。コードは params の差分 → 申告 → 親の記録 → 追加節 (`render.py:1154-1165`)。
4. 同 `:202` は変更内容を「同ページの下部の節として出す」、`:391` (AC-13) は「CSS と STYLE_HASH が不変」。コードはタブに分かれ (`render.py:1079-1088`)、`STYLE` は前段で変わっている。
5. チケット `web-ui-charts-equity-indicators` の「読み取り専用」。コードには書込みの画面 (承認・却下・改善の実行・job の取消) がある (`write_pages.py`)。
6. `plugin_pages.py:34` と `improve_pages.py:53` のコメント「合計と枠・nav・style が 64 KiB に収まる値」。節は今タブに分かれ、1 ページに合計して出ない (`plugin_pages.py:444-453`)。上限は合計でも収まる保守的な値として今も正しく、不具合ではない。コメントがタブに分かれたことを説明していないだけ。
7. この設計の下書きの依頼は書込みの画面を「承認・改善の実行」とするが、`write_pages.py` にはほかに job の取消 (`:157-158`) と「結果を確かめる」(`:108-110`) の form がある。§2.6 は 5 つとも対象にした。

## 付録 D. `STYLE` の下書き (2,957 byte)

先頭と末尾の改行を含めて測った。実装で値を動かしてよいが、上限 (§2.5) と付録 A の比は test が守る。

```css
:root { color-scheme: light dark; --bg: #fff; --fg: #1f2328; --mut: #57606a; --line: #d0d7de;
  --soft: #f6f8fa; --link: #0969da; --ok: #116329; --ok-bg: #dafbe1; --warn: #7d4e00;
  --warn-bg: #fff8c5; --bad: #cf222e; --bad-bg: #ffebe9; }
@media (prefers-color-scheme: dark) { :root { --bg: #0d1117; --fg: #e6edf3; --mut: #9198a1;
  --line: #3d444d; --soft: #161b22; --link: #4493f8; --ok: #3fb950; --ok-bg: #12261e;
  --warn: #d29922; --warn-bg: #2a2111; --bad: #ff7b72; --bad-bg: #2d1316; } }
body { margin: 0; padding: 0 1rem 2rem; color: var(--fg); background: var(--bg);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
  font-variant-numeric: tabular-nums; }
a { color: var(--link); }
nav { display: flex; flex-wrap: wrap; gap: 1.25rem; padding: 0.6rem 0;
  border-bottom: 1px solid var(--line); margin-bottom: 0.75rem; }
nav a { text-decoration: none; font-weight: 600; }
nav a:hover { text-decoration: underline; }
nav a[aria-current="page"] { color: var(--fg); }
h1 { font-size: 1.25rem; margin: 0.5rem 0 0.75rem; }
h2 { font-size: 1rem; margin: 0 0 0.4rem; }
h3, h4 { font-size: 0.9rem; margin: 0.6rem 0 0.3rem; }
section { border: 1px solid var(--line); border-radius: 6px; padding: 0.6rem 0.75rem;
  margin: 0 0 0.75rem; overflow-x: auto; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.15rem 1.25rem; margin: 0; }
dt { font-weight: 600; color: var(--mut); }
dd { margin: 0; overflow-wrap: anywhere; }
dd dl { padding-left: 0.5rem; border-left: 2px solid var(--line); }
ul { margin: 0; padding-left: 1.25rem; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--soft); padding: 0.6rem;
  border-radius: 6px; font-size: 0.85rem; }
.danger { color: var(--bad); }
dd.danger, .st.danger { font-weight: 700; background: var(--bad-bg); }
.warn { color: var(--warn); }
dd.warn, .st.warn { font-weight: 700; background: var(--warn-bg); }
.ok { color: var(--ok); background: var(--ok-bg); }
.st { display: inline-block; padding: 0 0.4rem; border: 1px solid; border-radius: 1rem;
  font-size: 0.85rem; white-space: nowrap; }
.kpi { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0 0 0.75rem; }
.kpi div { border: 1px solid var(--line); border-radius: 6px; padding: 0.4rem 0.75rem;
  background: var(--soft); }
.kpi b { display: block; font-size: 0.8rem; color: var(--mut); }
table { border-collapse: collapse; font-size: 0.85rem; }
th, td { border: 1px solid var(--line); padding: 0.15rem 0.45rem; text-align: left; }
th { background: var(--soft); }
tbody tr:nth-child(even) { background: var(--soft); }
.tabs a { padding-bottom: 0.25rem; border-bottom: 2px solid transparent; }
.tabs a[aria-current="page"] { border-bottom-color: var(--link); }
.scroll { max-height: 70vh; overflow: auto; }
.scroll th { position: sticky; top: 0; }
form { margin: 0.5rem 0; }
form + form { margin-top: 1.5rem; }
button, input { font: inherit; padding: 0.25rem 0.6rem; }
```

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-18 | C0.1 | <ul><li>K7 は計算の内訳 4 行だけを消す候補にし、測定結果・測定時刻・空き容量・override の警告は残す</li><li>K9・K10 の推奨を「残す」にし、省く案は 0 のときだけ</li><li>語彙の突き合わせを daemon → afx-ops の写し → 割当表の二段で test。真偽値の欠損・None・整数は不明とする入力例を固定</li><li>15 KiB の縮みを byte (ASCII・日本語・最大長) で測り直し、受入に申告・親の記録・追加節まで縮む例と省略表示・確認経路を足す。問い 1 の理由も直す</li><li>分類は生の値と厳密な型で行い、翻訳・代替表示はその後。分類関数の責務 (Bot と calendar の未知と既知の unknown の区別) を明記</li><li>時刻の制約を、応答の表示 tz に従う・欠損は UTC・tz を渡さない画面は行なし、に直す。付録 C の 6 はコメントの説明不足</li></ul> | 設計レビュー 1 周目 (Important 5 / Minor 1、全件採用) | — |
| 2026-11-18 | C0 | 初版 (下書き) | 利用者の方針 (OS に従う暗い配色、運用画面風、簡略化は候補から選ぶ) | bc275183 |
