# 人に見せる時刻を表示 tz で出せるようにする — 設計 v0.5.1

チケット: `display-timestamps-in-local-timezone` (機能 / 中)。

## 0. 目的と非目標

**原則 (利用者の認識、2026-11-06)。** 内部は UTC で保存・実行し、DST・NY・London などの時刻は都度換算する。利用者が目にする日時 (と利用者が入力する日時) は利用者の tz で扱う。週次改善の `improve_at` を表示 tz で入力・表示し内部で UTC に換算しているのはこの原則どおりで、本設計はこれを表示面全体に広げる。

**目的。** 人に見せうる時刻は、どの面でも利用者の表示 tz に換算して出せるようにする (利用者の要望 2026-11-06:「外部に表示する可能性のある値は tz 換算ができるように。今はログ中心、web を改修するときに換算後の値が増える」)。今回はその仕組み (時刻を換算できる形で運ぶ規則、換算の部品、時刻 field の宣言、自由文の時刻 token と本文 builder、これらを守らせる検査) を作り、今ある面を載せ替える。`mt5_bridge` の `bridge.log` を UTC に揃える。

**非目標。**
- 保存とファイルの時刻は変えない。DB は UTC ISO、`logs/agentic.log` は UTC (`... UTC` 表記)、`logs/activity.log` は UTC ISO (`+00:00`)。
- 機械向けの面は変えない。API の wire (UTC ISO)、webhook の `ts`、LLM の prompt、改善レポートのファイル名の日付。
- 市場境界 (NY 17:00) と週次改善の時刻規則は変えない。
- LLM が書いた文 (AGGREGATE の判断文、improve の observation、承認の claims) の中の時刻は換算しない (§6)。
- CLI の日時入力 (`afx history` の `--from` / `--to` は UTC として解釈し offset を捨てる、`backtest/cli.py:72-79`) は今回変えない。原則の「入力も利用者の tz」と食い違うので別チケットに起票する。

### 0.1 前提の再検討

チケットの処置案は「設定 `display.timezone` (既定はシステム tz) を新設」だった。現物には同じ目的の設定がある:

- `src/agentic_fx/config.py:596` `display_timezone: str = "UTC"` (コメント「ログ・status 表示に使う」)。`ZoneInfo` で読めるかを検証 (`:742-748`)。
- `config/settings.yaml.example:3` と v0〜v5 の settings fixture は `Asia/Tokyo`。初回設定 (`setup/wizard.py:787-798`) は既定をマシンの tz にして確かめる。
- これを読むのは週次改善の時刻 (`core/scheduler.py:1455-1530`) と wizard (`setup/wizard.py:585-587` の `_format_occurrence`、`:1768-1769`)。換算部品 `core/timeutil.py:15` `to_display` は呼出し元が無い。

**新しい設定は作らず `display_timezone` を使う。** チケットの「既定はシステム tz」は初回設定が満たしている。schema の既定 `UTC` は据え置く。表示 tz と週次改善の時刻は原則どおり同じ tz で、分けない。表示 tz を変えると次回の改善の時刻も同じ局所時刻のまま絶対時刻で数時間前後することを、example のコメント・wizard の問い・運用手順に書く (example は v5 fixture と byte 一致で更新、`tests/setup/test_settings_migrate.py:105-109`、schema の版は上げない)。

**tz の名前は IANA の地域名だけ。** 略称 (`JST` / `EST`) を設定に許す案は採らない (利用者、2026-11-06): 略称は夏冬で名前が変わり、地域で重なり、DST の規則を持たない。現行の検証は `ZoneInfo` が読める名前をすべて通すので、`EST` (年中 -05:00、NY の夏時間が効かない)・`EST5EDT`・`Japan`・`Etc/GMT-9` (符号が見た目と逆) も通る。規則 10 で扱う。

## 1. 現物の確認 (file:line)

### 1.1 載せ替える面

| 面 | 今 | 位置 |
|---|---|---|
| bridge のログ | プロセスの現地時刻、tz 表記なし。bridge.log には file formatter と通常 stream が書き、uvicorn の default / access は端末 (stderr / stdout、`propagate: False`) にだけ出る | `mt5_bridge/server.py:41-42,70-83,131,135-136,630-651` |
| `afx ctl log` / `afx ctl activity` | ファイルの行をそのまま | `ops/client.py:1177-1186`、`ops/service.py:741,752` |
| ctl の時刻欄 | wire の ISO をそのまま | `ops/client.py:649,655,733,816,862,868-872 (events の ts と fields、event_stream_health の first_failed_at / last_failed_at は ops/events.py:28),1355,1393,1463,1532,1563,1614-1618 (job の result の cell)` |
| ctl の期限 | `... UTC` に整形、naive は UTC とみなす | `ops/client.py:1714-1719,1298` |
| `afx status` (offline) | activity 末尾行・DB の ISO | `status.py:178-188,259-261,279-281,286,363` |
| 対話シェル (`Commands.dispatch`) | ISO をそのまま | `commands.py:132-135 (activity),255,585,588,601,603,633,644-649,836,882,1043-1050 (log)` |
| `afx plugin versions` | ISO をそのまま | `plugin/deploy_versions.py:153-156,166` |
| news CLI | `... UTC` に整形 | `news_cli.py:76-77` |
| `afx init` の bridge 時刻 | aware ISO | `setup/wizard.py:985` (`setup/checks.py:71-76,264`) |
| 承認カード (Discord) | `... UTC`、naive は分まで、不正は非表示 | `afx-ops/afx_ops/gate/render.py:110-123,297,325-326`、投稿は `bot/adapter.py:362,898` (try なし) |
| web | allowlist の ISO を切り詰め。承認詳細の「計算時刻」は手書きの行、log / activity のページは生の行 | `afx-ops/afx_ops/web/render.py:62-79,352,616-628`、`web/request.py:270-296` |
| status provider の DTO | `observed_at`、`gaps[].since` | `src/agentic_fx/service.py:1899,1990` |

### 1.2 時刻を書き込む既存の箇所 (activity 本文)

| 箇所 | 今 | 扱い (§3-6 / §3-7) |
|---|---|---|
| `core/scheduler.py:632` | `HH:MM UTC` | 時点 → builder (token) |
| `datafeed/news_collector.py:143` | `HH:MM UTC まで待つ` | 時点 → builder (token) |
| `datafeed/outage.py:377` (`BEGIN IMMEDIATE` の中で組み立てる) | `expected=… deadline=…` (`+00:00` ISO) | 時点 → builder (token)。送出しない builder にする (§3-7) |
| `core/time_cutover.py:309-311` | 保存した capture の JSON (`+00:00` ISO) | 時点 → capture の JSON を出す builder |
| `commands.py:293-296` `(SYSTEM, kill_switch_reset)` | `latched_at={…}` (保存済みの `+00:00` ISO) | 時点 → builder (token) |
| `core/scheduler.py:469,490,506,574,587,595` (`_fmt_bar`) | offset なしの UTC (`bar=2026-11-06T10:00`) | 識別子 (足のラベル) → `bar_label(dt)` で `2026-11-06T10:00 UTC`、換算しない。`_fmt_bar` は廃止 |
| `mt5_bridge/mt5_client.py:56,59` | NY のサーバ壁時計 (naive) を log に出す | server 時刻であることを語で添える (換算しない) |

`backtest/runner.py:282-285,465-467` の `_RecordingActivity` への `isoformat()` は backtest 内の記録で、人に見せる activity ではない (検査の対象外、§3-7)。

### 1.3 対象外 (UTC のまま)

- `afx history import` の conflict 出力の `bar_time` (DB 主キーの照合用、`backtest/cli.py:283-285`)。
- 改善 archive の `INDEX.md` 行 (`loops/improve_loop.py:2654`)、改善レポートのファイル名。
- LLM prompt (`loops/summary.py:65`、`loops/trade_loop.py:893-900`)。webhook payload の `ts` (`afx-ops/afx_ops/notify/payload.py:76-86`)。
- improve / trade / ops event の Discord 通知文には絶対時刻が無い (`afx-ops/afx_ops/gate/service.py:136-142`、`improve_render.py`、`trade_render.py`)。

afx-ops は独立 package で `agentic_fx` に依存しない (`afx-ops/pyproject.toml:8-11`)。main は pip の `tzdata` に依存する (`pyproject.toml:20`) が afx-ops は依存しない。afx-ops は Landlock で隔離され、読取許可に zoneinfo が無い (`afx-ops/afx_ops/isolation/startup.py:135,427-471`)。隔離後に未使用の zone を作ると `PermissionError` で落ちる (r2 の実測)。本機の system の zoneinfo には `US/` などの互換名が無く (tzdata-legacy 未導入)、main では pip の tzdata で読める `Japan` / `US/Eastern` が、afx-ops では読めない (r4 の実測)。

## 2. 利用者から見た動き

- `display_timezone: Asia/Tokyo` の利用者が `afx ctl activity` を打つと、各行の先頭の時刻が `2026-11-06 19:04:17 +09:00` の形で出る。システムが書いた本文の中の時刻 (データ障害の期限、news の待機期限など) も `… 19:30:00 +09:00` の形で出る。LLM が書いた判断文の中の時刻は書かれたまま。`afx ctl log` は行頭の時刻がミリ秒つきで換算される (`2026-11-06 19:14:23,090 +09:00`)。
- ctl / shell / offline status / plugin versions / news / init / 承認カード / web の時刻欄も `YYYY-MM-DD HH:MM[:SS] ±HH:MM` になる。秒の有無は面ごとに今と同じ。
- 夏時間のある zone は offset が変わる (America/New_York なら夏 `-04:00`・冬 `-05:00`)。
- どの地域の規則で換算しているかは、status の見出し (ctl status・shell status・offline status) と web の全ページの上部に出る `表示 tz: Asia/Tokyo (+09:00)` で確かめられる。個々の時刻には zone の名前を付けない。
- `display_timezone: UTC` なら `... +00:00`。今 `… UTC` と出している面 (news の待機期限、承認カード、ctl の期限) も `+00:00` になる。
- `logs/*.log` を直接読むと今どおり UTC (`agentic.log` は `UTC` 表記のまま)。`bridge.log` は `... UTC` 表記の UTC。
- 初回設定で表示 tz に `EST` や `Etc/GMT-9` を入れると「`America/New_York` のような地域名で」と問い直される。既存の settings にそれらが書かれていても起動は止まらず、status と起動ログに警告が出る。

## 3. 規則

1. **時刻は換算できる形のまま運ぶ。** 人に見せうる時刻は、保存・API の wire・DTO・自由文のどこでも、offset 付きの UTC ISO で持つ。整形済みの表示文字列で運ばない。新しい API の field・web の項目・通知文を足すときもこの規則に従う。
2. **換算の部品 (1 module、2 package へ byte 一致で写す)。**
   - stdlib だけで書いた 1 module (`agentic_fx/core/display_time.py`) に、時刻の換算・自由文 token の換算・ログ行頭の換算・時刻 field の集合・本文 builder の表・`bar_label` を置く。**正本は main package 側** (この module と `tests/fixtures/display_time_vectors.json`)。afx-ops 側の `afx_ops/display_time.py` と `afx-ops/tests/fixtures/display_time_vectors.json` は写しで、直接編集しない。変更の手順は「正本を編集 → 写しを更新 → byte 比較 → 両 suite」を同じ変更の中で行う。両 package の test が file 全体の byte 一致を検査する。
   - 時刻の換算: 入力は aware datetime か ISO 文字列、tz 名、精度 (分 / 秒 / ミリ秒)。並びは `YYYY-MM-DD HH:MM[:SS[,mmm]] ±HH:MM` (J1)、精度は面ごとに今と同じ (承認カードの期限・判定時刻と ctl の期限は分、activity と status は秒、agentic.log はミリ秒)。offset は zone に移した後の値の `utcoffset()` から作る (DST 境界で正しい値になる)。
   - **部品は送出しない**: offset が分で割り切れない zone の時刻 (19 世紀の地方平均時など) は「換算できない」。naive、読めない文字列、`OverflowError`、tz 名の型違い・不正 (`KeyError` / `ValueError` / `TypeError` / `OSError`、`ZoneInfoNotFoundError` を含む) はすべて「換算できない」を返す。
   - 換算できないときの扱いは各面が決める。今の契約を保つ: ctl の期限は naive を UTC とみなす (`ops/client.py:1716-1717`)、承認カードは naive を分まで・不正は出さない、ctl の `_safe_cell` の伏字。
   - 処理の順は「無害化 (伏字) → 換算 → 長さ制限」。
   - 正しさは共通の入出力表 (test vector) で pin する。vector の id は一意で、両 suite が消化した id の集合が vector の id の集合と一致することを確かめる。
3. **表示 tz の出どころ。**
   - daemon の API: `ops/api_server.py:636-639` の成功応答の 1 か所で `data["display_timezone"]` を足す (全 GET / POST の成功応答に付く)。
   - ctl client・web・bot は、描画する応答そのものの `data.display_timezone` を使う (status の追加取得や cache はしない)。field が無い (古い daemon) ときは UTC。`gate/reconcile.py:96` の detail なしの出し直しは時刻を出さない。
   - 見出しの `表示 tz:` に出すのは、実際に換算に使った tz とその時点の offset (fallback で UTC になったときは `UTC (+00:00)`)。
   - wire 由来の tz 名は形を検査し (`[A-Za-z0-9_+\-/]{1,64}`)、web は html escape してから出す。不正・換算できない tz の fallback のログは wire の値を含めない固定語。
   - daemon 内 (ctl の server 側、shell) は `settings.display_timezone`。
   - news CLI と offline `afx status` は共通の小さな読み手を使う: `config/settings.yaml` を `yaml.safe_load` で読み、`display_timezone` の 1 key だけを取り ZoneInfo で検証する。送出せず、読めなければ UTC。`load_env_file` / `load_settings` は呼ばない (news の list / enable / disable がそれらを呼ばない契約 `tests/test_news_cli.py:209-220` を保つ。`load_settings` は `.env` の鍵に依存するので、表示 tz だけのために呼ぶと正しい設定でも黙って UTC になる)。
   - `afx init` は段 2 の答え (`a.display_timezone`、`setup/wizard.py:832-838`) を使う。
4. **時刻 field の宣言。** 時刻を持つ field の名前を 1 つの集合で宣言し、両 package に置く (byte 一致の module に含める)。初期の集合: `ts`, `created_at`, `updated_at`, `started_at`, `latched_at`, `decided_at`, `computed_at`, `observed_at`, `effective_at`, `deadline_at`, `expires_at`, `first_failed_at`, `last_failed_at`, `last_attempt_at`, `expected_last_attempt_at`, `restricted_since`, `since`, `deadline`, `until`。T1a は DTO を作る箇所 (`ops/service.py`、`ops/events.py`、`ops/api_server.py`、`src/agentic_fx/service.py` の status provider) を grep して名前と代表 path (`status.kill_switch.latched_at`、`approvals[].created_at`、`events[].ts`、`events[].fields.first_failed_at`、`status.gaps[].since`、job の result の cell など) を表にし、集合に足りないものを足す。web は `_value` の全階層で、ctl は `format_event` / `_job_line` など汎用の k=v 描画で、この集合に入る field を一律に換算する。新しいページ・項目は集合に足せば換算される (§0 の要望の本体)。手書きの行 (web の承認詳細の「計算時刻」など) は部品を明示的に呼ぶ。
5. **ログ行頭の換算 (ctl と web の log / activity、shell の log / activity)。** activity の行は tab 区切り 5 field で、第 1 field が UTC ISO、第 2 field が既知の category のときだけ第 1 field を換算する。agentic.log の行は `YYYY-MM-DD HH:MM:SS,mmm UTC <LEVEL> <logger>:` の形が揃うときだけ時刻を換算する。形が合わない行 (traceback の続き、途中で切れた行、tab を含む疑似行) はそのまま。
6. **自由文の時刻 token。**
   - 形: 候補は `(?<![\w.:+-])\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?(Z|[+-]\d\d:\d\d)(?![\w+-]|:\S|\.\d)` で拾い、候補全体を `fromisoformat` で読み、offset がちょうど 0 のもの (`Z`、`+00:00`、`-00:00`) だけを換算する (`+00:00.5`、`Z+09:00`、offset 秒、7 桁以上の小数秒は換算しない。token の直後の `.` や `:` + 空白は句読点として許す。`:` の直後が空白でないもの (`+00:00:30` など) は拒否)。
   - 換算するのは「形の正しい activity 行の本文 field (第 4 field) で、`(category, event)` が builder の表の key に入るもの」だけ。LLM の書く本文、agentic.log の本文、形の崩れた行は換算しない。
   - 規則 5 (行頭) と規則 6 (本文) は同じ行に両方適用する。
7. **時刻を含む本文は builder だけが作る。**
   - 部品の module に builder の表を置く。各 builder は特定の `(category, event)` に束ねられ、`(category, event, body)` を返す (呼出側は `activity.write(*builder(...))` と書く)。表の key の集合が、規則 6 の換算対象の event の集合そのもの (1 つの表)。
   - builder は時刻を `utc_token(dt)` (秒まで、`Z` 終わり) で埋める。builder は送出しない (`activity.write` は資金保護の経路と `BEGIN IMMEDIATE` の中から呼ばれる、`activity.py:33-48`、`outage.py:220-222,270,292-377`): naive な datetime を受けたら token にせず `naive:` を前に付けた ISO で、範囲外の値は `invalid:` を前に付けて書く (換算されず、読む人に分かる)。builder の返す category は文字列 (部品は stdlib だけで `Category` を import しない) で、`ActivityLog.write` は文字列の category を `Category` に正規化して受ける (T1b)。
   - `utc_token(` は部品の module の中 (builder) でだけ使う。足のラベルは `bar_label(dt)` (`YYYY-MM-DDTHH:MM UTC`) で書く。
   - **検査**: (a) 表の各 event について「builder で書く → activity 行 → ctl / shell / web の表示」で換算後の時刻が出る。(b) 補助の AST 検査: daemon の `ActivityLog` に書く module (`src/agentic_fx/` の `backtest/` を除く) で、日時の format spec を持つ f-string (`{x:%H…}` など)、`.format(` の日時指定、`strftime(`、`isoformat()`、部品の module の外での `utc_token(`、`_fmt_bar(` が activity の本文に入る書き方を検出する (直接の引数と、同じ関数内で事前に代入した変数)。変異 (直接・format spec・`.format`・事前代入・別 helper・module 外の `utc_token`) を 1 つずつ足すと落ちる。属性を埋め込むだけの書き方 (`f"latched_at={state.latched_at}"`) は AST で取れないので、§1.2 の一覧を builder へ載せ替えて守る。
8. **bridge のログを UTC に。** `mt5_bridge/server.py` の file formatter、通常 stream の `_ColoredLevelFormatter`、uvicorn の default formatter、uvicorn の access formatter (動的に派生させるもの) のすべてで `converter = time.gmtime` を使い、各書式に `UTC` を入れる (端末に出る uvicorn の行も UTC になる)。server time の換算は触らない。
9. **afx-ops の tz データ。** afx-ops は main と同じ pin の pip `tzdata` に依存する。`isolation/startup.py` の `run` で、preload の前に `zoneinfo.reset_tzpath(to=[])` を呼んで system の zoneinfo を探さないようにし (`PYTHONTZPATH` も無効になる)、zone は pip の tzdata から読む。tzdata は purelib にあり、隔離の既存規則 (`startup.py:427-437` の RUNTIME) で読めるので、Landlock の規則は足さない。fallback の import が隔離後に起きないよう、`importlib.resources` と `tzdata` を preload に加える (`cli.py:42-45` の流儀)。zone が読めないときは部品が「換算できない」を返し、面は UTC で出して固定語のログを 1 回残す。
10. **tz の名前の検査。** 初回設定 (wizard) で表示 tz を入れるときは、`Area/Location` 形式 (`/` を含み、`Etc/` で始まらない) と `UTC` だけを受け付ける。`EST` / `EST5EDT` / `Japan` / `Etc/GMT-9` などには「`America/New_York` のような地域名で」と問い直す。settings の読込 (`config.py:742-748`) は今のまま `ZoneInfo` で読める名前を通し、上の形に合わない名前のときは status と起動ログに警告を出す (更新で起動しなくなるのを避ける)。

## 4. 配線表

| 場所 | 配線 | 担当 |
|---|---|---|
| `src/agentic_fx/core/display_time.py` (新規) / `core/timeutil.py` | 部品 (規則 2・5・6・7、時刻 field の集合、builder の表、`utc_token`、`bar_label`)。`to_display` はこの module から使うか置き換える | T1a |
| `tests/fixtures/display_time_vectors.json` (新規) / afx-ops 側の写し 2 file と byte 一致の検査 (両 package) | vector と最初の写し | T1a |
| `src/agentic_fx/ops/api_server.py` / `ops/client.py` / `commands.py` / `status.py` / `news_cli.py` / `plugin/deploy_versions.py` / `setup/wizard.py` / `service.py` (status provider) | 応答の `display_timezone`、ctl・shell・offline status・plugin versions・news・init の時刻、行頭・本文の換算、見出し、小さな読み手 | T1b |
| `core/scheduler.py` / `datafeed/news_collector.py` / `datafeed/outage.py` / `core/time_cutover.py` / `commands.py` | §1.2 の書き込みを builder / `bar_label` に、AST の検査 | T1b |
| `config.py` / `setup/wizard.py` / `config/settings.yaml.example` / `tests/fixtures/settings_examples/v5.yaml` / 運用手順 | 規則 10 の検査と警告、`display_timezone` の説明 (表示と週次改善の時刻、変えると次回の改善が数時間前後する、地域名で書く) | T1b |
| `afx-ops/afx_ops/web/render.py` / `gate/render.py` / `bot/adapter.py` | 応答の `display_timezone` で換算、時刻 field の宣言、手書きの行、log / activity のページ、全ページの見出し | T2 |
| `afx-ops/pyproject.toml` / `afx-ops/afx_ops/isolation/startup.py` / `cli.py` | `tzdata` の依存、`reset_tzpath(to=[])`、preload (規則 9) | T2 |
| `mt5_bridge/server.py` / `mt5_bridge/mt5_client.py` | ログの UTC 化 (規則 8)、server 時刻の語 | T3 |

## 5. 受入条件

1. **部品 (両 package、共通 vector)**: `Asia/Tokyo` で `2026-11-06T10:04:05Z → 2026-11-06 19:04:05 +09:00`。`America/New_York` で春 `2026-03-08T06:59:59Z → 01:59:59 -05:00`、`07:00:00Z → 03:00:00 -04:00`、秋 `2026-11-01T05:30:00Z → 01:30:00 -04:00`、`06:30:00Z → 01:30:00 -05:00`。`UTC` で `... +00:00`。`Japan` (互換名) で `+09:00`。`+09:00` の入力、小数秒、精度 (分 / 秒 / ミリ秒)。「換算できない」: naive、date だけの文字列、壊れた文字列、`9999-12-31T23:59:59Z` + `Asia/Tokyo` (`OverflowError`)、tz が `''` / `None` / `zone.tab` / `'A'*300`。どれも送出しない。token の正例: `…Z`、`…+00:00`、`…-00:00`、直後が `.` / `: ` / `。` / 行末。負例: `2026-11-06T10:00:00+00:00.5`、`…Z+09:00`、offset 秒 (`+00:00:30`)、7 桁の小数秒。2 つの module と 2 つの vector が byte 一致し、vector の id が一意で、両 suite が消化した id の集合が一致する。
2. `afx ctl activity` の行頭が換算され、本文は builder の表に入る event だけ換算される。ISO で始まる LLM の本文、traceback の続き、途中で切れた行、tab を含む疑似行はそのまま。`afx ctl log` はミリ秒を残して換算。web の log / activity のページ、shell の log / activity も同じ。
3. ctl / shell / offline status / plugin versions / news / init の時刻欄が表示 tz で出る。面ごとの秒の有無が今と同じ。表示の期待値は意図して更新し (例 `tests/test_commands.py:125-137`、`tests/test_news_cli.py:278,293,695` の `… UTC` は `… +00:00` に)、保存・wire の期待値は変えない。status の見出しに `表示 tz: <IANA 名> (±HH:MM)` (ctl / shell / offline)。init は答えの tz を使う。
4. route 表の全 GET / POST の成功応答 (200 / 202、POST 後の job の poll、events の follow を含む) の `data` が `display_timezone` を持つ。エラー応答の envelope は今のまま。HTTP 層で応答 data を完全一致で pin する既存 test (`tests/ops/test_policy_projection_matrix.py:118`) は意図して更新する。古い daemon (field なし) に対して ctl / web / bot は UTC で出す。これは具体的な表示期待値 (`… +00:00`、見出しの `表示 tz: UTC (+00:00)`) で pin する。
5. news CLI と offline status: settings が欠落・破損していても news の list / enable / disable が今どおり動き、`load_env_file` / `load_settings` を呼ばない (`tests/test_news_cli.py:193,209-220`)。表示 tz は小さな読み手で読め、読めなければ UTC。
6. afx-ops: 承認カードと web は応答の `display_timezone` で換算する。不正・欠落では UTC で、ページ表示と承認投稿は失敗しない (部品が送出しないこと)。承認カードの不正値の非表示・naive の分精度は保ち、UTC の表示は `… UTC` から `… +00:00` に意図して更新する (`afx-ops/tests/gate/test_approval_post.py:63,269,301,322,440-452`、`tests/ops/test_approval_post_e2e.py:109`)。時刻 field の集合に足した field は追加の配線なしで換算される。web の全ページの上部に `表示 tz:` が出る。
7. **時刻表示の網羅**: (a) 合成 DTO (規則 4 の代表 path を全部含み、深い dict / list と手書きの行を含む) を表示 tz ≠ UTC ですべての renderer (ctl・shell・web・承認カード) に通し、各時刻欄の**換算後の期待値が出力にある**こと、時刻欄が消えていないこと、LLM の欄を除いて生の `+00:00` / `Z` で終わる ISO が残らないことを確かめる。(b) 集合に新しい field 名を足すと、追加の配線なしでその field が換算されて出る。(c) LLM の本文 (claims・reason・AGGREGATE の判断文) と識別子 (足のラベル、`bar_time`) は変わらない。(d) 補助の lint: web の `*_FIELDS` (`afx-ops/afx_ops/web/render.py:62-79`)、`ops/events.py` の FIELDS、trade_events の FIELDS に載る名前のうち `_at$|^ts$|deadline|until|since` に合うものは集合に入っている。
8. **自由文**: §1.2 の時点の書き込みが builder で書かれ、表の各 event について書込み → ctl / shell / web の表示で換算後の時刻が出る。builder は naive を受けても送出しない (`naive:` 付きで書かれ、換算されない)。足のラベルは `bar_label` で `UTC` が明記される。offset 0 でない ISO・数字列・表に無い event の本文は換算されない。補助の AST 検査が 6 種の変異をそれぞれ検出し、`backtest/` は対象にしない。
9. **隔離下の tz**: 既存の canary の子 harness (Landlock + seccomp、`tests/isolation/afx_isolation_canary.py` の `startup.run` 経由) で、`zoneinfo.TZPATH == ()` であり、隔離後にまだ使っていない zone (例 `America/New_York`) と互換名 (`Japan`) を pip の tzdata から作って換算できる。`PYTHONTZPATH` を別の dir にしても結果が同じ。Landlock の規則一覧は変わらない (規則一覧を pin する既存 test `afx-ops/tests/isolation/test_isolation_pins.py:368`、`test_startup_plan.py:144-156` が無変更で緑)。zone が読めない場合を模擬すると「換算できない」になり、面は UTC で出て固定語のログが 1 回出る。
10. **bridge**: dictConfig を適用した後の実 formatter 4 種に固定の `record.created` を与えた出力が UTC と `UTC` 表記になる。
11. **tz の名前**: wizard は `EST` / `EST5EDT` / `Japan` / `Etc/GMT-9` を問い直し、`Asia/Tokyo` / `America/New_York` / `UTC` を受ける。settings にそれらの互換名が書かれていても起動し、status と起動ログに警告が出る。
12. ファイル・DB・API wire・webhook・LLM prompt の時刻は変わらない。

## 6. 残余

- LLM が書いた文の中の時刻 (`現在 HH:MM UTC`、offset なしの ISO など) は換算されない。システムの書いた時刻 (換算される) と同じ行に並ぶことがある。
- 既に保存された `HH:MM UTC` の文言 (`scheduler.py:632`・`news_collector.py:143` の旧い行) は UTC のまま出る。保存済みでも offset 0 の ISO で、表に入る event の本文は表示で換算される。
- 行頭換算は、本文が正規の行頭と同じ形の行を区別できない。
- main は system の zoneinfo を先に、afx-ops は pip の tzdata だけを読む。tz データの版が違うと、規則が変わった直後の zone で 2 つの面の換算がずれ得る (どちらも offset が出るので、別の瞬間を同じ表示にすることは無い)。
- 既存の `bridge.log` の行は書き換えない。切替の前後で表記が変わる。
- CLI の日時入力は UTC のまま (§0 の非目標、別チケット)。

## 7. task 分割

| task | 内容 | 担当 |
|---|---|---|
| T1a | 部品 (`display_time.py`)・vector・afx-ops への最初の写しと両側の byte 一致の検査・AST 検査の枠 | luna (純関数) |
| T1b | daemon / CLI の配線 (api_server・ctl・shell・status・plugin versions・news・init・status provider・書き込み箇所の builder 化・規則 10・説明文) | sonnet (ctl の test は UDS の偽 server を使い、codex sandbox では AF_UNIX の bind が通らない) |
| T2 | afx-ops (web・承認カード・bot・tzdata と `reset_tzpath`・preload) | sonnet (Landlock の子 process の test) |
| T3 | bridge のログの UTC 化と server 時刻の語、Wine 側 bridge の配備・再起動手順 (DST の T3 と同じ: bridge だけ restart、ターミナルは触らない) | luna |

T1a → T1b → T2 の順。T3 は独立。判定は指揮者が回す。

## 8. 裁定

### 利用者の裁定 (2026-11-06)
- **J1 表記**: 並びは `YYYY-MM-DD HH:MM:SS` (A 案、`YYYYMMDD-HH:MM:SS-TZ` 案は不採用)。全面で共通、精度は面ごとに今と同じ。ファイルは UTC のまま。
- **J1-b tz の表し方**: 略称でなく offset (`2026-11-06 19:04:17 +09:00`)。UTC も `+00:00`。zone の名前は status と web の見出しにだけ `表示 tz: Asia/Tokyo (+09:00)`。理由: 設定の地域名と略称 (`JST` / `EDT` / 数字の `-03`) が混ざるのを避ける。
- **設定に略称を許す案は採らない**: 夏冬で名前が変わる。DST の規則を持つのは IANA の地域名だけ。

### 技術上の決定
- 新しい設定は作らない、schema の既定 `UTC` は据え置く。表示 tz と週次改善の時刻は分けない (§0 の原則で決まる)。
- 換算は表示側、wire・保存は UTC ISO。tz は daemon の成功応答に 1 か所で付け、各面は描画する応答から読む。
- 部品は stdlib だけの 1 module を 2 package に byte 一致で置き (正本は main 側)、vector で正しさを pin する。部品と builder は送出しない。
- 時刻を含む activity 本文は event に束ねた builder だけが作り、換算はその event の本文に限る。
- afx-ops は pip の tzdata だけを読み、Landlock の規則は増やさない。
- 互換の tz 名は、wizard では問い直し、既存の settings では警告にとどめる (更新で起動しなくなるのを避ける)。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-06 | C0 | 初版 | チケット起票 (2026-11-02) | — |
| 2026-11-06 | v0.2 | 目的を「換算できる仕組み」に、DST、afx-ops の独立 package と隔離、表示面の追加、tz の経路、news、換算不能の扱い、行頭判定、bridge、対象外、J1 / J2 | 利用者の要望、sol r1 (I6 / M3) | — |
| 2026-11-06 | v0.3 | tz を成功応答 1 か所で付け cache を廃止、部品は送出しない、自由文 token の範囲、書き込み一覧と AST、表示面の追加、web の全階層、byte 一致の写し、小さな読み手、J1 = A、CLI 日時入力は非目標 | 利用者の裁定、opus r2 (I8 / M11) | — |
| 2026-11-06 | v0.4 | tz の dir を固定、登録 builder + 補助 AST、時刻 field の初期一覧と網羅の pin、正本と更新手順、全 GET / POST、token は候補全体を読む、J1-b の選択肢 | sol r3 (I3 / M5) | — |
| 2026-11-06 | v0.5.1 | token の lookahead を `(?![\w+-]|:\S|\.\d)` に (`…Z: next` を正例に通す)、範囲外は `invalid:`、分で割り切れない offset は換算できない、builder の category は文字列で `ActivityLog.write` が正規化 | T1a の実装で判明 | — |
| 2026-11-06 | v0.5 | tz は offset 表記 (J1-b)、UTC も `+00:00`、zone の名前は見出しだけ。afx-ops は pip の tzdata だけを読み Landlock の規則を足さない (`reset_tzpath(to=[])`、preload)。builder は event に束ね送出しない (naive は `naive:` 付き)、`utc_token` は builder の中だけ、`bar_label` で `_fmt_bar` を廃止、AST は daemon の ActivityLog に限る。書き込み一覧に `kill_switch_reset` と `_fmt_bar` の全箇所、時刻 field に `last_attempt_at` / `since`、status provider を grep 対象に、lint の allowlist を名指し、token の lookahead と `-00:00`、§5-4 の引用 test を訂正、最初の写しを T1a に、tz の名前の検査 (規則 10) | 利用者の裁定 (J1-b、略称の設定は却下)、opus r4 (I1 / M9) | — |
