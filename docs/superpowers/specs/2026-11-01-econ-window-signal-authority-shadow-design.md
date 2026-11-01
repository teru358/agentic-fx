# 指標回避帯・シグナル採否契約・shadow 成績・反応統計 — 設計 C0

**版:** v0.9.2 C0（2026-11-01、設計レビュー 8 周を全件反映、J1〜J3 利用者承認済、実装待ち）
**対象:** `econ-event-hold-window-deterministic` / `llm-vs-strategy-signal-authority` / `econ-event-actuals-and-reaction-stats`。実装は束 1 と束 2 に分けるが、本書を共通契約とする。

## 0. 目的と非目標

**目的:** 保存済み経済イベントから対象ペアの回避帯を決定論的に作り、帯内の新規リスクだけを止める。執行可能な strategy signal は、signal 起動の専用 mission が 1 件ずつ claim し、帯外で LLM に二値の採否だけを任せ、最終判断を `signal_decisions` に一意に残す。cron mission は signal を claim せず、従来 schema で裁量判断を続ける。reject と accept の両方に同じ規則の `signal_shadow` を作り、選別の損得を同じ物差しで人間が検証できるようにする。保存済みイベントと退避した 1m 足から発表後 15m / 1h / 4h の反応統計も作り、人間と trade agent にだけ開示する。

**非目標:** actual の外部取得 / 過去カレンダーの追加取得 / backtest への回避帯導入 / LLM に signal の方向・価格・期限・SL・TP を書き換えさせること / 帯内の既存注文の自動取消 / close・cancel・SL・TP の抑止 / shadow 成績を agent の学習材料にすること / 床 20 や p75 を予測力・有意性の証明として扱うこと。

### 0.1 前提の再検討

| 当初の前提 | 現物と前提確認で分かったこと | C0 の結論 |
|---|---|---|
| claimed signal がある mission を専用 signal mission とみなせる | 現物は cron mission も signal を claim し、同じ schema と runner を共有する | cron は claim せず従来 schema のまま。1 signal = 1 signal 起動の専用採否 mission とする |
| signal があれば signal 起動の mission が走る | 現物の signal 起動は建玉か約定待ち注文があるときだけ due になる。cron 優先・容量 1・trade 後の reflection 連鎖で、15m signal の鮮度内に届かないこともある | 起動条件を「実行可能な signal が pending」に変え、待機上限を式で契約し、届かない signal は黙って残さず abandoned にする |
| `trade_intents` が signal 採否の台帳になる | 同表は signal 外部キーを持たず、偽の OPEN を残すと執行提案と採否が混ざる | 全 signal の最終採否は `signal_decisions` を正本にし、実際に構成した intent だけを結ぶ |
| 今後のイベント検索だけで帯を判定できる | 発表後 15 分も回避帯である | 現在時刻より前のイベントも拾う専用検索にし、境界を inclusive にする |
| 同名 event の時刻が変われば旧行を無効化すればよい | 取得元に安定 ID が無く、同名の定期指標は毎週・毎月の別インスタンスである | 未発表かつ 7 日以内の同名行だけを置換候補にし、既発表・過去の同名行は残す |
| calendar tool だけを絞ればよい | state summary も全通貨の 24h イベントを自動注入する | tool と state summary は、対象通貨・重要度・帯・鮮度の同じ表現を使う |
| OPEN 受付時の gate だけで新規リスクを止められる | 既存 limit は約定時に risk gate を再実行しない | 帯と足区間が交差する limit 約定だけを抑止し、既存建玉の保護は常に続ける |
| LLM が signal の数値を組み直せる | strategy 側は方向・entry・SL・TP を決め、day と limit 4h も決定論で補える | LLM は accept / reject だけを返し、intent は保存済み signal からシステムが組み立てる |
| reject だけ shadow にすれば選別を測れる | accept は実注文、reject は仮想約定となり、測定規則が違う | accept にも同じ仮想 track を作り、decision 別に並べる |
| 1m は必要時に元表から読める | cache は 30 日で消え、後日の再現結果が変わる | event / shadow 単位の 1m slice を確定時に退避し、以後は退避値を正本にする |
| scheduler には専用の write 接続がある | scheduler は executor・broker・trade loop・reflection・calendar・news と core 接続を共有し、signal maintenance は同じ tick 内で別の signal 接続から書く。フラグを持たない長い writer（improve 等）もいる | 既存接続の busy timeout 5000ms は変えない。新設の worker 接続だけを即 rollback の規律にし、scheduler の待ちは実測で判定する |
| slice は確定 transaction で一緒に書けばよい | 1 owner 最大 1,680 行あり、scheduler と同じ core lock と SQLite の書込み直列を長く占有する | lock の外で staging へ小分けに書き、lock 内は完備 marker と短い CAS だけにする |
| 足が無い分はすべて欠損である | rollover など tick が無い分と feed 障害が混在する | `datafeed_outage_gap` だけを障害欠損とし、それ以外は前値保持の flat 足にする |
| calendar が取れなければ帯も安全側に閉じられる | calendar 取得は fail-soft で、未知のイベントは列挙できない | 帯は保存済みイベントに対してのみ決定論的。未取得時は fail-open とし鮮度警告を出す |
| shadow と反応統計は改善材料にもできる | 評価と選別が循環し、agent 入力が汚染される | 数値は人間面だけ。反応統計 tool も trade mission 限定とする |
| managed-policy id=1 を置換すれば方針文が変わる | 現行の方針 1 行目は managed block の外の利用者行で、id=1 は DB に存在せず再採番もされない | 既定文の全文を本書に置いて fixture にし（J3 の承認を前提）、配備時に利用者が managed 行として登録し旧行を消す |

## 1. 現物の確認（file:line）

| 現物 | 確認結果 |
|---|---|
| `src/agentic_fx/loops/trade_loop.py:63,214-237` | `_CLAIMING_TRIGGERS` は `cron` と `signal`。cron mission も作成直後に最古の pending signal を claim する。DB 実測では claim 済み 82 件と abandoned 2 件はいずれも `trigger='cron'`、`signal%` mission は 0 件だった |
| `src/agentic_fx/core/scheduler.py:292-305` / `src/agentic_fx/service.py:1618-1625` | cron は supervisor が受理した時点で watermark を進める。cron mission が signal 採否へ置き換わると、その判断足の close / cancel / hold を再実行できない。signal 起動の受理拒否は記録されず、次 tick で再判定される |
| `src/agentic_fx/service.py:1720-1730` | `signal_due_fn` は `orders.list_by_status(open, pending_fill)` が空なら False を返し、その後に `signals.pending_exists` と `missions.signals_rate_ok` を見る。建玉も約定待ち注文も無い平常時は signal 起動が起きない |
| `src/agentic_fx/core/scheduler.py:353-421` | `_trade_mission_due` は outage 中なら何も返さず、cron due があれば `"cron"`、無いときだけ `signal_due_fn` を見て `"signal"` を返す。判定例外は起動しないに倒す |
| `src/agentic_fx/core/supervisor.py:78-79,99-115,242-249` | queue は maxsize 1、`try_submit` は phase が idle 以外なら拒否する。trade の dispatch は trade 実行の後に同じ dispatch で `_reflection_fn` を呼ぶ。signal 起動の mission も同じ連鎖に入る |
| `src/agentic_fx/core/mission_ceiling.py:21,53-70` / `src/agentic_fx/service.py:938-984` | dispatch 全体の上限 Cd は worker 1 回の上限 Cw × (1 + reflection 最大 3) + 10 秒。`config/settings.yaml.example` を読むと Cw 405 秒・Cd 1630 秒。Cw ≥ 判断足幅と lease ≤ Cw + 60 秒は起動拒否、Cd ≥ 判断足幅は起動を続けて `mission_ceiling_dispatch_warning` を書く |
| `src/agentic_fx/service.py:2596-2619` | scheduler thread は前 tick の開始から 60 秒経つと次の tick を始めるので、tick 所要が 60 秒を超えれば間隔も伸びる。tick 中は `scheduler_busy` を立てる。ただし writer transaction の取得待ちを登録するフラグは無く、次 tick の予定時刻も thread の局所変数で外から読めない。worker が busy 印の確認直後に書込みを始める TOCTOU を防げない |
| `src/agentic_fx/store/signals.py:163-180,211-237,351-366` | 鮮度の終点は `bar_ts + 足幅 × freshness_bars`。既定 2 なので 15m signal は足確定から 15 分だけ claim できる。`claim_oldest` と `pending_exists` は同じ `_claimable_where`（status・鮮度・deploy link）を共有し、kind と payload は見ない |
| `src/agentic_fx/store/missions.py:278-307` / `src/agentic_fx/config.py:528-535` | signal 起動の rate は最短間隔 10 分・日次 12 本。`trigger LIKE 'signal%'` の行を数え、claim 失敗で `skipped` になった mission も算入する |
| `src/agentic_fx/service.py:1145-1180,1290,1637-1645` | maintenance は lease 切れ回収 → 鮮度切れ pending の abandoned 化 → producer 評価の順。鮮度切れの abandoned は activity を書かない（未知 timeframe だけ書く）。maintenance は data state が `ready` の tick だけ走り、core 接続とは別の `conn_signal` で書く |
| `src/agentic_fx/loops/trade_loop.py:373-453,551-570` | claim 後の source lock timeout、snapshot stale、source inactive、data state 非 ready と、consume / requeue / abandoned の分岐が既にある。現在は intent 記録が consume より先に走る |
| `src/agentic_fx/store/signals.py:270-324` | claim は mission id 必須の CAS。status は `pending → claimed → consumed` または `abandoned`。consume は自動 commit、requeue 上限後は abandoned |
| `src/agentic_fx/loops/mission_finalize.py:18-51` / `src/agentic_fx/store/missions.py:26-48` | mission 終端の共有 helper は `missions.finish` を自動 commit で呼ぶ。`missions.finish` 自体は `commit=False` を受けられる |
| `src/agentic_fx/store/db.py:30-42,88-121,303-377,1399-1513,1571-1652` | `signals` は status CHECK と mission FK、`missions.status` は CHECK なし、`trade_intents.reject_category` は 5 値 CHECK。既存 DB の table rebuild は共通 DDL、事前 backup、行数照合、全 FK 検査、最後の commit を使う |
| `src/agentic_fx/store/db.py:557-562` / `src/agentic_fx/service.py:2596-2619` | 全接続は WAL・`busy_timeout=5000` で、synchronous は指定せず既定値。scheduler は tick 中の印を立てるだけで DB writer の優先権を予約しない。SQLite は core lock の外でも single-writer なので、worker が先に write transaction を取ると scheduler は現状最大 5 秒待ち得る |
| `src/agentic_fx/service.py:1288-1290,1336,1373-1386,1527-1534,1732` | scheduler 専用の write 接続は無い。`conn_core` を PriceProvider・EconCalendar・NewsCollector・PaperBroker・Executor・TradeLoop・ReflectionCycle・Scheduler が共有する |
| `src/agentic_fx/loops/improve_loop.py:3233-3260` / `src/agentic_fx/ops/audit.py:106` / `src/agentic_fx/datafeed/outage.py:270` / `src/agentic_fx/news_cli.py:229` | scheduler のフラグを持たない writer が `BEGIN IMMEDIATE` を使う。improve は transaction の中で候補の質検査（file I/O・AST parse）と archive の rename をする。`afx news` は別プロセス。現行の 5000ms がこれらとの競合を吸収している |
| `src/agentic_fx/loops/trade_loop.py:395-424` | commit-core で `gate_now > fresh_until` なら signal を abandoned にし、intent を `reject_category=signal` で拒否する。claim 後の decision 期限は現物でも鮮度終点である |
| `src/agentic_fx/store/econ_events.py:7-16` / `src/agentic_fx/datafeed/econ_calendar.py:178-220` | upsert は 1 行ごとに commit する。refresh は保存が途中で失敗しても保存済みの行を残し、その件数を返す |
| `src/agentic_fx/store/signals.py:85-100,240-260,287-312,314-324,327-348,388-403` / `src/agentic_fx/store/missions.py:161-215` | abandoned を書く経路は 6 つ: 鮮度切れ pending の一括終端、claimed の即時 abandoned（stale / source inactive）、requeue と lease 回収（共有の遷移式で上限超過か deploy に結ばれた行を abandoned）、deploy op の approve / revoke による pending の abandoned、起動時の中断回収（上限超過か deploy に結ばれた行、205-210）。どれも理由を残さない |
| `src/agentic_fx/store/db.py:999-1001,1031-1065,1399-1520` / `src/agentic_fx/store/db.py:253-258` | `_migrate_signals_fk` は「signals に参照元が無い」前提の RENAME 先行で、検査も `foreign_key_check(signals)` だけだが、現物では `plugin_deploy_op_signals.signal_id` が signals を参照するため新しい rebuild の前例にはできない。参照される表を新名 CREATE → copy → 旧表 DROP → rename し、DB 全体を FK 検査する `_migrate_trade_intents_observability` を前例にする |
| `src/agentic_fx/store/db.py:304-306,1544,1571-1652,1774-1790` | `data_migrations` は既存表で、`init_db` は migration を順に直接呼ぶ。循環を避ける lazy import と、name を記録して 2 回目を no-op にする既存流儀がある |
| `src/agentic_fx/loops/trade_loop.py:57-60,652-657,759-764` | `_TRADE_TOOLS` は `get_signals` を含み、trade mission と ask mission が同じ一覧を使う |
| `src/agentic_fx/datafeed/price_provider.py:106-123` / `src/agentic_fx/datafeed/health.py:70-84` | quote 取得は source chain を順に試し、鮮度・未来時刻・正の有限値・`bid ≤ ask` を検査する。`bid = ask` は通す |
| `src/agentic_fx/core/scheduler.py:1240-1252` | live の day close は `next_rollover(filled_at or created_at)` から buffer を引いた時刻 |
| `src/agentic_fx/store/db.py:345-349` / `src/agentic_fx/store/alert_state.py` | 通知の dedupe に使う key-value 表 `alert_state` がある（gate reject streak が使用） |
| `src/agentic_fx/loops/summary.py:55-90` | state summary は建玉・注文・calendar を出し、mission status は読まない |
| `src/agentic_fx/plugin/strategy_adapter.py:298-328` | horizon=day と limit expiry=4h の変換は backtest replay 経路であり live producer ではない |
| `src/agentic_fx/plugin/signal_producer.py:402-453` | live は strategy 結果をそのまま保存し、horizon / expiry を持たない。`kind="signal"` は方向だけの参考行で、執行可能 payload ではない |
| `src/agentic_fx/core/risk_gate.py:110-146` | risk gate は DB を読まない純関数。帯の結果は `GateContext` の値として渡す必要がある |
| `src/agentic_fx/core/scheduler.py:32-35,253-260,698-705,1284-1420` | calendar refresh は 6h。fresh bar は 5 分遅れまで許容する。limit fill と exit は分離済みで、SL / TP と processed-bar 更新は limit 抑止後も継続できる |
| `src/agentic_fx/core/scheduler.py:1405-1410` / `src/agentic_fx/core/paper_fills.py:7-50` / `src/agentic_fx/config.py:40` | live paper fill は `risk.pair_rules` の `assumed_spread_pips` を使う。limit 約定と exit は spread を引数で受ける純関数で、long は ask 側・short は bid 側へ half spread をずらす |
| `src/agentic_fx/core/contracts.py:73-78` / `src/agentic_fx/datafeed/sources.py:117-150` | `Quote` は bid / ask / ts / source を持つ。mt5 は実 bid / ask、yfinance は 1m close を bid と ask の両方に入れる |
| `src/agentic_fx/store/db.py:124-140,677-692` | `ohlcv_cache` に spread 列は無く、`ohlcv_history.spread` は nullable。数値一致は `FLOAT_TOL=1e-9` の `values_match` を共有する |
| `src/agentic_fx/service.py:2171-2251` | scheduler の遅延 callable も同じ scheduler thread で同期実行される。長い resolver を置くと次 tick の kill switch・SL / TP 判定を遅らせる |
| `src/agentic_fx/store/db.py:446-452` | `datafeed_outage_gap` が feed 障害区間の正本として存在する |
| `src/agentic_fx/datafeed/econ_calendar.py:17-18,41` / `src/agentic_fx/store/econ_events.py:7-16` / `src/agentic_fx/store/db.py:371-377` | 取得元は ForexFactory の今週分 JSON で、項目は title / country / date / impact / forecast / previous の 6 つ。安定 event ID は無い。`econ_events` は `(ts,country,name)` UNIQUE の upsert |
| DB 実測 `econ_events` | importance=3 は全通貨で 117 件（2026-08-26〜2026-10-30、約 53 件 / 月）、importance=2 は 121 件、importance=1 は 766 件 |
| `src/agentic_fx/loops/improve_context.py:121-162` / `src/agentic_fx/loops/trade_loop.py:79-93` | improve report は全 trade intent の hold_rate / reject_breakdown を数え、gate reject streak は全拒否を通知対象にする |
| `src/agentic_fx/ops/trade_event_sink.py:343` / `src/agentic_fx/commands.py:593-618` / `src/agentic_fx/status.py:258-259` | intent の mission trigger、直近 mission status を読む既存利用者がある |
| `src/agentic_fx/tools/signal_tools.py:28-40,73` | docstring は improve allowlist と禁止集合の非交差 assert を意図するが、現物にその assert は無い |
| `src/agentic_fx/loops/prompts/trade_mission.md:1` | prompt は「1 時間毎」と書くが、稼働中の判断足は 15m |
| `policy/directives.md:1-10` / `src/agentic_fx/policy_projection.py:13-16,48-49` / `src/agentic_fx/store/db.py:511-516` | 1 行目は managed marker の外にある利用者行で、「signal の方向と自分のテクニカル判断が一致するかを reasoning に書く」を求める。本機 DB の `ops_policies` は id 2〜4 だけで、AUTOINCREMENT の採番済み最大は 4。id=1 は存在せず再発行されない。方針文は `"- "` + 空白畳み込みの 1 行、上限 4000 字。`policy/` で追跡されるのは `.gitkeep` だけで、directives.md は利用者データ |
| `src/agentic_fx/cli_surface.py:260-271` | `afx ctl policy add / show / remove / reconcile` がある。show は managed 行と unmanaged 行を分けて出す |
| DB 実測 `[2026-10-16T00:00Z, 2026-10-31T00:00Z)` | `missions.started_at` 基準で hold 970 / open 3、計 973。`signals.claimed_by_mission_id` で結ぶ signal-linked は 57。944 はチケット・メモリ作成時の途中値 |
| `tmp/review-econ/sol-premise-check.md:17` | 現時点で 4h の完全な 1m 足を持つ importance=3 は USD 22/38、JPY 9/12。ただし元 cache の保持で変動する |

## 2. 利用者から見た動き

### 2.1 帯内に signal が出たとき

signal 起動の専用 mission が 1 件を claim する。帯内なら LLM runner を起動せず、`reject / system / econ_window`、reject shadow、consume、mission の `system_filtered` 終端を 1 transaction で確定する。cron mission はこの signal を claim せず、同じ判断足でも従来の open / close / cancel / hold schema で独立して走る。帯明けに同じ signal は再利用しない。

利用者は `signal_filtered_econ_window` と参照 ID を見る。status / web では current window、system reject、未解決 shadow、calendar freshness を確認し、親専有レポートでは strategy × decision × actor / code と shadow 成績を見る。

### 2.2 帯外で signal が出て LLM が採否するとき

専用 mission は建玉の有無にかかわらず起動し、保存済み signal の方向や価格を変えず、accept または固定理由付き reject だけを返す。accept は保存値から intent を作り、accept shadow を開始してから通常の資金 gate / executor へ渡す。reject は注文を作らず reject shadow を開始する。契約違反は採否・consume・shadow を確定せず mission failed とし、signal を requeue、上限後だけ abandoned にする。cron・reflection・runner が長引いて鮮度終点までに採否を確定できなかった signal は、claim の前後を問わず abandoned で終わる。

利用者は `signal_accepted`、`signal_rejected_by_llm`、`signal_decision_invalid` のいずれかを見る。status / web と親専有レポートは accept / reject / abandoned を分け、accept / reject shadow を同じ規則で並べる。最悪値から鮮度内採否を保証できない設定は status の `worst_case_guarantee_unavailable` で常時確認するが、それだけでは activity warning を出さない。実測 latency または freshness 系 abandoned が悪化したときだけ `signal_wait_budget_warning` を見る。同じ設定と運用日の組では 1 回だけ通知し、再起動しても増やさない。

### 2.3 cron の裁量判断

cron mission は signal の有無にかかわらず signal を claim・注入せず、signal 一覧の tool も持たない。従来 schema で open / close / cancel / hold を提案する。cron watermark も従来どおり supervisor の受理時に進む。帯内の裁量 OPEN は commit-core 冒頭で帯を判定して `trade_intents.reject_category=econ_window` で却下する。close / cancel / hold は帯の影響を受けない。裁量判断は `signal_decisions` / `signal_shadow` の母集団に入れない。

利用者は帯内 OPEN 却下を `open_rejected_econ_window` で見る。status / web / report は裁量 OPEN の帯内却下と signal reject を混ぜない。

### 2.4 帯内に既存 limit・既存建玉があるとき

既存 limit は、処理する 1m 足の区間が回避帯と交差すれば価格到達しても約定させない。cancel せず期限も延長しない。帯明け後の新しい足で再到達した場合だけ約定できる。close / cancel / day close / SL / TP は常に許可する。

利用者は `limit_fill_suppressed_econ_window` を order × 回避帯の連結成分につき 1 回だけ見る。再起動を挟んでも再通知しない。「同一回避帯」は重なりを推移的にまとめた帯の和集合で、その ID は最初に抑止した時点の成分の最小 event id に固定する。同じ order の次の抑止は、その成分の event と 1 つでも共通すれば同じ成分とし、後から event が合流・supersede しても ID を変えない。

## 3. 規則

### 3.1 回避帯

`econ.window` は settings schema v5 のまま追加する任意 section で、省略時は次の既定値を使う（既定値は §8 J2 の承認を前提）。

| key | 型・範囲 | 既定値 | 意味 |
|---|---:|---:|---|
| `importance_min` | 整数 1〜3 | 3 | この重要度以上だけを対象にする |
| `before_min` | 0 以上の整数 | 30 | 発表前の回避分数 |
| `after_min` | 0 以上の整数 | 15 | 発表後の回避分数 |

1. instrument 定義の base / quote currency と `country='All'` を対象にする。importance 条件は全行に同じく適用する。
2. event `ts` の帯は inclusive な `[ts-before_min, ts+after_min]`。重なる帯は推移的な和集合へまとめる。
3. 時刻 `now` で判定するとき（signal runner 前、OPEN commit）の候補は `[now-after_min, now+before_min]`、足 `[b,b+1m]` で判定するとき（limit fill）の候補は `[b-after_min, b+1m+before_min]` から取る。どちらも対象通貨・importance・`superseded_by IS NULL` を再確認する。次の対象 event は同条件で 7 日先まで検索する。
4. signal runner 前、OPEN commit、limit fill は同じ純粋判定を使う。signal accept は intent 記録前に lock 内で再判定し、帯内なら system reject + shadow へ切り替える。裁量 OPEN は lock 内で判定結果を `GateContext` に渡し、その後に intent を記録する。risk gate は純関数のままにする。
5. limit は処理足の閉区間 `[b,b+1m]`（b は足の開始時刻）と inclusive な帯の交差で抑止し、候補は 3 の足区間基準で取る。`now` は使わないので、5 分遅れの fresh bar で帯の後端を跨ぐ足も同じ判定になる。抑止足は既存 processed-bar 規則どおり再処理しない。抑止通知の dedupe 状態（order、固定した成分 ID、成分の event 集合）は DB の既存 key-value 表に保存する（§2.4）。
6. calendar freshness の正本は `fetch_attempts` の `ECON_KEY`。attempt、success、failure を残し、初回成功なしは `unknown`、直近失敗または最終成功から refresh 周期 6h × 2 = 12h 超は `stale`、それ以外は `fresh`。保存済み event は stale でも帯を作り、未知 event は仮定しない。
7. 取得元に安定 ID が無いので、時刻変更は refresh が取得結果の全 row を保存し終えた直後の pass（§3.5-5）で、取得結果全体を入力に判定する。保存が途中で失敗した refresh では判定しない。次の条件をすべて満たす旧 row だけを置換候補とする: refresh で新しく入った row と同じ `(country,name)`、`superseded_by IS NULL`、refresh 時点で未発表（`ts > now`）、新 row との時刻差が 7 日以内、今回の取得結果に含まれない。候補が複数なら時刻差最小（同差は id 最小）の 1 行だけに新 row の id を `superseded_by` として保存し、削除しない。既発表の同名 row、7 日を超えて離れた同名 row、同じ取得結果に並ぶ同名 row は別インスタンスとして残る。window / reaction は有効 row だけを読む。
8. agent 表現は pair、対象通貨、`in_window`、active events、7 日以内の次 event、freshness に限定し、無関係通貨・importance 未満を注入しない。

### 3.2 専用 signal mission の起動・採否契約と排他

**対象分類。** live producer は `kind="strategy"` の執行可能 payload に、既存の backtest 変換と同じ規則で horizon=day、limit expiry=4h を保存前に補い、signals の新しい列 `contract_version` に `executable_v1` を記録する（payload の key にはしない）。pair、direction、entry_type、limit price、expiry、horizon、SL、TP が正本である。全 query は同じ分類式を共有し、次の 3 規則を混ぜない。

1. **eligibility:** 対象側の条件は `kind='strategy'` かつ `contract_version='executable_v1'`。これに既存の status・鮮度・deploy link を足した全条件を、signal 起動判定と claim がそのまま共有する。人間面の decision / abandoned 集計は対象側の条件だけを共有する。全条件が true なら claim は同じ行を取れるので、claim できない専用 mission（`skipped`）は鮮度境界の競合でしか生じない。
2. **旧形式の移行:** `data_migrations` 表は既存である（§1）。T1a は signals v3 DDL と rebuild migration、name を引数に取る冪等 guard helper を置き、`init_db` の順序を「既存 `_migrate_signals_fk` → v3 rebuild → T1b の正規化 migration 呼出し位置」として空ける。T1b は live producer の保存規則を確定した後、同じ規則を使う配備 migration と key 名・記録行を持ち、循環を避ける lazy import の呼出し 1 行をその位置へ追加する。migration は pending / claimed の `kind="strategy"` で `contract_version` が NULL の行を処理し、pair・direction・entry_type・SL が揃い、limit なら価格もあれば正規化して status は変えない。揃わない行は `normalization_failed` で abandoned にし、その時刻を `abandoned_at` に書く。`kind="signal"` は変えない。
3. **除外対象の cleanup:** maintenance は stale な pending のうち、eligible 対象を requeue 歴が無ければ `freshness_expired`、あれば `requeued_expired`、`kind="signal"` または契約版の無い旧 payload を `ineligible_cleanup` で abandoned にする。後者は参考情報の掃除であり、decision 母集団、運用上の abandoned 合計・率、strategy 別集計から除外し、cleanup 件数としてだけ表示する。maintenance の更新対象、起動判定、claim、集計は同じ分類 query の各 branch を使う。

**起動条件。** 専用 signal 起動は「eligible な pending signal が鮮度内にある」かつ「signal 起動の rate 内」のときだけ due とする。建玉・約定待ち注文の有無は見ない。rate（最短間隔 R と日次上限）は LLM を呼んだ専用 mission と claim に失敗した `skipped` を現行どおり数え、帯内で `system_filtered` に終わった mission は数えない。

**待機上限の契約。** 記号は signal の足幅 w、`freshness_bars` k、足確定 c = bar_ts + w、鮮度終点 f = bar_ts + w·k、dispatch 全体の上限 Cd、worker 1 回の上限 Cw、tick 間隔 T = 60 秒、signal 起動の最短間隔 R、plugin name lock の待機上限 Pl とする。以下の上限は data state が ready で、各 tick の所要が T 未満の間だけ成り立つ。

1. scheduler は現行どおり cron due を先に選び、受理時に cron watermark を進める。cron は claim も signal 注入もしない。
2. capacity 1 の既存 mission supervisor が cron と signal mission を直列化する。signal 起動は cron が due でなく supervisor が idle の tick だけで、受理されなければ次 tick で再判定する。trade 後の reflection は従来どおり同じ dispatch に続く。
3. signal は c 以降の maintenance で作られるので、作成は最大 c + T（取込が遅れればその分も遅れる）。作成後に cron が再び due にならなければ、最初の起動機会までの待ちは最大 `Cd + T + R`（走行中 dispatch の残り + tick 間隔 + 最短間隔の残り）。cron が先に due になれば cron が先に走り、待ちはその dispatch 分だけ延びる。
4. f までに claim されなかった signal は claim されない（claim 条件に鮮度がある）。f 後の最初の maintenance（f + T 以内）で abandoned になる。したがって ready 中の eligible signal は「f までに claim」か「f + T までに abandoned」のどちらかに必ず入る。data state が ready でない間は maintenance も専用 mission も走らないので、pending は claim されないまま f + T を越えて残り、ready に戻った最初の maintenance で abandoned になる（claim されないので安全側）。tick が T を超えて伸びた分も同じだけ遅れる。
5. f は claim と decision の両方の期限である。15m・k=2 では c から f までの余地は 900 秒。専用 mission は core lock と name lock を取った後の commit-core で時刻が f を過ぎていれば、decision / shadow を書かず signal を abandoned にする（現物の stale 分岐、§3.3）。したがって q で claim した signal が decision に届くのは、q + runner の所要 + core lock 待ち（scheduler tick 1 本分、上の条件で T 未満）+ name lock 待ち（Pl 以下）が f 以内のときだけである。異常終了は既存の signal lease L で回収して requeue し、再 claim は f 以前だけ、f 後なら abandoned にする。example 設定は Cw=405 秒、L=900 秒、Pl=2 秒。
6. **静的保証と稼働 warning を分ける。** 承認済み strategy の最小足幅 w について `Cd + 3T + R + Cw + Pl ≥ w·(k−1)`、または `Cd + T ≥ 判断足幅` なら、起動は止めず status の `worst_case_guarantee_unavailable=true` を恒常表示する。これは最大値による静的な「保証不能」の表示であり、activity warning にはしない。承認済み strategy が 0 本なら静的 status は `n/a`、稼働 warning は inactive とする。example 設定（Cd 1630 秒、R 600 秒、Cw 405 秒、Pl 2 秒）は 15m・k=2 で 2,817 ≥ 900 となる。
7. **稼働 warning は実測で出す。** 直近 14 運用日の actor=`llm` の final decision について `eligible→claim = signals.claimed_at − signals.created_at` と `claim→decision commit = signal_decisions.decided_at − signals.claimed_at` を直接測る。100 件以上なら各 p95 の和が承認済み strategy の最小 `w·(k−1)` 以上、または同期間に `abandoned_at` が入る `freshness_expired` / `requeued_expired` が 1 件以上なら `signal_wait_budget_warning` を出す。100 件未満だけは、終了済み trade mission の `started_at` から、同じ dispatch に続く最後の reflection mission の `finished_at` まで（reflection が無ければ trade mission 自身の `finished_at` まで）を dispatch 所要として、その p95 + R + Cw を代用する。reflection は capacity 1 の直列性を使い、対象 trade mission の `finished_at` 以後かつ次の trade mission の `started_at` より前に開始した `loop='reflection'` を時間隣接で結ぶ。p95 の標本数、2 latency、freshness 系件数、直接観測か代用かを status / web / 親 report に出す。評価は final decision / freshness 系 abandoned の確定時と運用日境界に行う。dedupe key は「判定に使う設定値の canonical fingerprint、signal 日次上限と同じ境界の運用日」の組で、同じ key では 1 回だけ、fingerprint または運用日が変わったときだけ再通知する。直近 key と時刻は DB の既存 key-value 表に保存する。

**排他。**

1. 専用 mission は claim 後、既存の plugin name lock を commit-core だけ保持し、source identity と payload を再確認する。cron と同じ signal を奪い合う経路は無い。
2. 正常系は 1 signal = 1 専用 mission = 1 claim = 1 final decision。claim CAS と name lock のどちらかに失敗したら decision を書かず requeue 規則へ戻し、再試行 mission は失敗試行として decision 母集団へ入れない。

**spread の記録。** 専用 mission は commit-core の前に lock の外で、鮮度と `bid ≤ ask` を検査する既存の quote 取得経路（§1）から対象 pair の quote を 1 回取る。source 関数を直接呼ばない。bid・ask が有限で `ask > bid > 0` なら `(ask-bid)/pip_size` を `spread_source=quote` で、取れない・条件を満たさない（既存の検査が通す bid = ask を含む）なら `risk.pair_rules` の assumed spread を `spread_source=assumed` で shadow に保存する。system reject も同じにする。

Claude backend に渡す schema は最上位を object とし、全 property に型を持たせる。Anthropic API は `--json-schema` の最上位 `anyOf` / `allOf` / `oneOf` を 400（`input_schema does not support oneOf, allOf, or anyOf at the top level`）で拒否し、最上位 `type: object` を要求する。実 CLI で最上位の `if` / `then` / `else` は受理され、accept / reject 両 prompt で正しい構造化出力を得た（実測）。そのため相関は最上位の if/then/else で持つ。

```json
{
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "decision": {"type": "string", "enum": ["accept", "reject"]},
    "reject_code": {
      "type": ["string", "null"],
      "enum": [null, "news_shock", "opposite_position"]
    }
  },
  "required": ["decision", "reject_code"],
  "if": {"properties": {"decision": {"const": "accept"}}},
  "then": {"properties": {"reject_code": {"type": "null"}}},
  "else": {"properties": {"reject_code": {"type": "string"}}}
}
```

reject code を 2 語にするのは §8 J1 の承認を前提とする。accept ⇔ null、reject ⇔ 2 固定語の相関は Python parser と local jsonschema の条件節で二重強制する。条件節で `const` を使う場合は同じ node に `type` も置く。`news_shock` は重大ニュースが signal の前提を破壊した場合、`opposite_position` は同じ pair の逆方向 `OPEN` / `PENDING_FILL` / `PROTECTION_PENDING` が orders にある場合だけ許す。LLM が `opposite_position` を返したのに該当 order が無ければ契約違反とし、`signal_decision_invalid` に固定 detail を付け、mission failed → requeue → 上限で abandoned とする。

非準拠、parser 不一致、runner 失敗、commit 前の内部失敗では signal は未 consume、decision / shadow は未作成。`uncoded` や hold へ読み替えない。

確定 transaction は conn を受けて commit しない store 処理を組み合わせ、外側で 1 回だけ commit する。現在自動 commit する signal 消費と mission 終端もこの形へ直す。

- system reject / LLM reject: decision、reject shadow、consume、mission 終端を 1 transaction。
- accept: intent 記録、decision、accept shadow、consume、mission completed を 1 transaction。その成功後だけ executor を呼ぶ。consume は executor より前という既存規則を保つ。
- transaction 成功時だけ実行単位の finalized / consumed を両方 true にする。失敗時は両方 false のまま finally の requeue に任せる。
- accept commit 時に帯へ入っていれば intent を記録せず system reject へ切り替える。

### 3.3 永続化、status、既存分岐

`signal_decisions` は最終採否だけを持つ。

| 列 | 契約 |
|---|---|
| `id` | 主キー |
| `mission_id` | 専用採否 mission。NOT NULL、外部キー |
| `signal_id` | 対象 signal。NOT NULL、外部キー、UNIQUE |
| `trade_intent_id` | accept だけ NOT NULL。reject は NULL |
| `decision` | `accept` / `reject` |
| `actor` | `llm` / `system` |
| `reject_code` | accept は NULL、LLM は `news_shock` / `opposite_position`、system は `econ_window` |
| `decided_at` | UTC |

CHECK は全 nullable 列に `IS NULL` / `IS NOT NULL` を明示し、accept、LLM reject、system reject の 3 正規形だけを許す。`trade_intent_id` が同じ decision の intent を指すことは store と test で守る。

`signal_shadow` は accept / reject の両方に 1 行作る。

| 列 | 契約 |
|---|---|
| `id` / `decision_id` / `signal_id` | 主キー、decision への UNIQUE FK、signal への UNIQUE FK。`(decision_id, signal_id)` は `signal_decisions(id, signal_id)` への複合 FK で、別 signal の decision を指せない |
| `decision` | `accept` / `reject` snapshot |
| `actor` | decision と同じ値域 `llm` / `system` の snapshot。accept は常に `llm` |
| `reject_code` | accept は NULL、reject は decision と同じ固定語 |
| `state` | `pending_entry` / `filled` / `resolved` / `expired` |
| `entry_price` / `entry_at` | 仮想約定後だけ値を持つ |
| `result_code` | `sl` / `tp` / `day_close` / `data_insufficient` / `no_fill` |
| `exit_price` / `pnl_pips` / `resolved_at` | 数値終端だけ値を持つ。insufficient / no_fill の pnl は NULL |
| `pip_size` / `spread_pips` / `spread_source` / `rule_version` | decision 時の規則 snapshot。`spread_source` は `quote` / `assumed`。初版 rule_version=1 |
| `created_at` / `updated_at` | UTC。CAS に使う |

`state` と結果列は次の 4 正規形だけを CHECK で許す。`entry_price` / `entry_at` は常に両方 NULL または両方 NOT NULL とする。

| `state` | `result_code` | fill 情報 (`entry_price` / `entry_at`) | `exit_price` / `pnl_pips` / `resolved_at` |
|---|---|---|---|
| `pending_entry` | NULL | 両方 NULL | すべて NULL |
| `filled` | NULL | 両方 NOT NULL | すべて NULL |
| `resolved` | `sl` / `tp` / `day_close`、または `data_insufficient` | 数値終端は両方 NOT NULL。`data_insufficient` は不足が fill 前なら両方 NULL、fill 後なら両方 NOT NULL | 数値終端は 3 列とも NOT NULL。`data_insufficient` は 3 列とも NULL |
| `expired` | `no_fill` だけ | 両方 NULL | すべて NULL |

`ohlcv_slice_sets` は退避単位（owner）の台帳と完備 marker である。

| 列 | 契約 |
|---|---|
| `id` | 主キー |
| `event_id` / `shadow_id` | 片方だけ NOT NULL の FK。UNIQUE は `(event_id,pair)` と `shadow_id` |
| `pair` | settings pair |
| `range_start` / `range_end` | UTC の半開区間で `range_start < range_end`。event は `[ts-15m,ts+4h)`、shadow は §3.4 の区間 |
| `expected_rows` | 区間の分数。0 以上 |
| `state` | `awaiting` / `staging` / `complete` / `unrecoverable` / `cancelled`。`cancelled` は対象外になった event set の状態で、worker の候補にならない。設定変更で再び対象になれば、slice 行が 0 件であることを確認して `completed_at=NULL`、`generation=NULL`、`purged_at=NULL` として `awaiting` に戻す。発表前に記録済みの spread は保持する（§3.5-5） |
| `generation` | staging 中だけ値を持つ。staging 行を書いた worker 起動の世代で、`ohlcv_slice_generations.id` への FK |
| `spread_pips` / `spread_source` | event だけ値を持つ（§3.5）。shadow は NULL（shadow 行が持つ） |
| `created_at` / `completed_at` / `purged_at` / `rule_version` | `completed_at` は complete / unrecoverable / cancelled だけ、`purged_at` は保持期限後の行削除後だけ値を持つ |

`ohlcv_slice_generations` は worker 世代の永続台帳である。

| 列 | 契約 |
|---|---|
| `id` | AUTOINCREMENT 主キー。worker は起動ごとに 1 行を commit し、その id を現世代とする。再利用されないので単調増加 |
| `started_at` | UTC |

`ohlcv_slices` は退避した 1m 足の行である。reader は state=`complete` の set の行だけを読む。

| 列 | 契約 |
|---|---|
| `set_id` | `ohlcv_slice_sets` への FK。NOT NULL |
| `bar_time` | UTC 1 分境界 |
| `open` / `high` / `low` / `close` / `volume` | observed / no-tick flat では NOT NULL。outage marker では NULL |
| `source` | 代表 source（§3.5）。flat / outage も由来 source を保持 |
| `quality` | `observed` / `no_tick_flat` / `datafeed_outage_gap` |

UNIQUE は `(set_id,bar_time)`。quality と OHLC の NULL 対応を CHECK する。

`econ_reaction_keys`（束 2 が書く）は反応集計の一意キーと canonical pair の対応で、算定時点に 1 回だけ書いて変えない。

| 列 | 契約 |
|---|---|
| `event_id` / `currency` | UNIQUE。`All` は通貨ごとに 1 行 |
| `set_id` | 算定時点の canonical pair の event set への FK |
| `decided_at` | UTC |

`econ_reaction_values`（束 2）は event set ごとの窓別の値 snapshot で、slice 行が保持期限で消えても残る。

| 列 | 契約 |
|---|---|
| `set_id` / `window_min` | FK と 15 / 60 / 240。UNIQUE |
| `status` | `resolved` / `unresolvable` |
| `range_pips` / `close_change_pips` / `abs_close_change_pips` / `net_abs_close_change_pips` | resolved だけ値を持つ |
| `rule_version` / `computed_at` | 算定規則版と算定時刻 |

既存表の扱いは次で固定する。

| 表 | 変更・遷移 |
|---|---|
| `econ_events` | nullable FK `superseded_by` を追加。有効 row は NULL |
| `trade_intents` | `reject_category` に `econ_window` を追加し、NULL 三値を明示した CHECK へ rebuild |
| `missions` | 専用 mission の帯内終端に `system_filtered` を追加。既存 status は維持し、全 reader で terminal success/filter として扱う |
| `signals` | status CHECK の 4 値は変えず、`trade_intents` と同じ手順で table を rebuild する（既存行があると CHECK 付きの列追加は失敗するため）。nullable な `contract_version`（NULL か `executable_v1`）、`abandon_reason`、`abandoned_at` を加える。`abandon_reason` は status=abandoned のときだけ NOT NULL で、値は `freshness_expired` / `requeued_expired` / `retry_exhausted` / `source_inactive` / `normalization_failed` / `deploy_op` / `deploy_linked` / `ineligible_cleanup` / `legacy_unknown` のいずれか。他の status では NULL。`abandoned_at` は status が abandoned 以外なら NULL、abandoned なら NOT NULL。ただし既存 abandoned は copy 時に reason=`legacy_unknown`、`abandoned_at=NULL` を許す。確定採否は `claimed→consumed`、retry は `claimed→pending`、cron は遷移させない |

accept 前の既存分岐は母集団へ入れない。

| 分岐 | signal 終端 | abandoned reason | decision / shadow |
|---|---|---|---|
| source lock timeout / error | requeue、上限で abandoned | `retry_exhausted` | 書かない |
| snapshot stale | requeue、上限で abandoned | `retry_exhausted` | 書かない |
| signal stale（commit-core の時刻 > f。runner が f 後に戻った場合を含む） | abandoned | requeue 歴なし `freshness_expired`、あり `requeued_expired` | 書かない |
| source inactive | abandoned | `source_inactive` | 書かない |
| data state not ready | f 以内なら requeue、外なら abandoned | 外は stale と同じ | 書かない |
| 未 claim のまま鮮度切れ | abandoned | stale と同じ | 書かない |
| LLM または system が確定 | consumed | NULL | 1 decision + 1 shadow |

abandoned を書く既存の 6 経路（§1）は、すべて理由を同じ文で書く。

| 経路 | 対象 | reason |
|---|---|---|
| maintenance の鮮度切れ一括終端 | stale な pending | eligible は requeue 歴で `freshness_expired` / `requeued_expired`、除外対象は `ineligible_cleanup` |
| claimed の即時 abandoned | commit-core の stale / source inactive | 上の分岐表どおり |
| mission の requeue | 契約違反・lock timeout 等の失敗 | 上限超過は `retry_exhausted`、deploy op に結ばれた行は `deploy_linked` |
| lease 回収 | lease 切れの claimed | requeue と同じ |
| 起動時の中断回収 | 前回 running の mission が持つ claimed | requeue と同じ |
| deploy op の approve / revoke | 対象 plugin の pending | `deploy_op` |

deploy に結ばれ、かつ上限も超えた行は `deploy_linked` とする。旧 payload migration の abandoned は `normalization_failed`、rebuild 前からの abandoned は `legacy_unknown`。

abandoned は対象側の eligibility を満たす signal だけを `abandon_reason` 別に数える（`legacy_unknown` は契約版が無いので、移行前の件数として別に出す）。`ineligible_cleanup` は cleanup 件数へ分離する。期間への帰属は `abandoned_at` で決め、時刻の無い `legacy_unknown` は期間集計へ入れず累計だけへ出す。accept / reject 率の分母は decision 行だけとし、abandoned と cleanup は入れない。

### 3.4 shadow の状態機械と slice worker

1. decision 時刻を次の UTC 分境界へ切り上げた `t0` を観測開始とする。分境界ちょうどならその時刻。
2. market は `t0` の最初の完全な 1m open に half spread を適用する。limit は `[t0,t0+4h)` の最初の到達足で、live paper fill と同じ gap / spread 保守則を使う。spread は shadow 行の保存値を使う。
3. fill 足で SL と TP の両方が見えれば SL。TP だけなら entry と TP の順序不明なので、その足では TP を確定しない。fill 後も同時 SL / TP は SL、gap は open で不利に滑らせる。
4. day close は live と同じ規則で、起点の次の rollover から live と同じ buffer を引いた時刻と、その前の最後の完全足を使う。起点は market が `t0`、limit が fill 足の時刻である（rollover 後に約定した limit は翌 rollover まで続く）。filled は SL / TP が無くても day close で必ず終端する。
5. 判定に必要な最後の足の終端から backfill 猶予 1 日までは不足を確定しない。猶予内の backfill は採る。猶予後は `data_insufficient` / 除外を確定し、後から足が来ても再計算しない。
6. shadow の slice 区間は、market が `[t0, t0 起点の day close)`、limit filled が `[t0, fill 時刻起点の day close)`、limit no-fill が `[t0, expiry)`。limit の区間終端は fill の有無が決まってから決まる（fill 足が見えた時点、または expiry を過ぎて no-fill）。1 owner の行数上限は limit filled の 240 + 1,440 = 1,680 行。最終確定点は区間終端 + 1 日。早期に見えた SL / TP は候補として保持し、slice set の完備 CAS と同じ transaction で終端を commit する。
7. `pnl_pips` は long `(exit-entry)/pip_size`、short `(entry-exit)/pip_size`。手数料・swap・lot は含めない。

**slice worker。** shadow の解決と event slice の退避は、scheduler の遅延 hook ではない 1 本の専用 worker thread が受け持つ。worker は専用 read-only conn と専用 write conn を持つ。read transaction は owner ごとに閉じ、複数 owner にまたがって snapshot を持ち続けない（WAL checkpoint を止めない）。30 秒ごとの 1 cycle で最大 25 owner を扱う。250ms は 1 cycle の DB 作業（候補抽出・正規化の読取・staging 書込み・読戻し・完備 CAS と BUSY の再試行）の累計経過の上限で、達したら次の transaction を始めず cycle を終える。spread 用の quote 取得は DB 作業の外で行い、250ms に数えない。1 cycle は次の順に進む。

1. **候補抽出**（read-only、lock 外）: 最優先は `awaiting` かつ spread 未記録で現在時刻が `[ts-5m,ts)` の event set。その後に、最終確定点を過ぎて complete / unrecoverable でない shadow と、`ts+4h+1 日` を過ぎて complete / unrecoverable / cancelled でない event set を取る。25 owner 上限の外側で spread 候補を先に処理し、slice 解決の滞留で発表前 quote を取り逃さない。
2. **正規化**（read-only、lock 外）: §3.5 の 1m 規則で区間の行を作り、判定や値もここで計算する。
3. **staging 書込み**（write conn、core lock なし）: shadow は set 行を `staging` と現世代で作る。event は既存の `awaiting` set を現世代の `staging` へ CAS で移し、spread 列はそのまま残す。slice 行は小分けにする。worker は短い writer admission mutex の中で scheduler の書込み要求フラグを確認して `BEGIN IMMEDIATE` まで行い、立っていれば transaction を始めず yield する。確認と BEGIN の間へ scheduler の登録が割り込まないため、事前確認の TOCTOU を作らない。1 transaction は 240 行以下とし、`BEGIN IMMEDIATE` から COMMIT を発行する直前までの経過を 50ms 以下にする。経過は文の境界ごとに見て、超えていれば次の文を出さず rollback して batch を縮小する。1 文の途中では打ち切らない（1 文は最大 240 行の一括挿入）。COMMIT の所要（WAL の fsync）は 50ms に含めず、毎回計測して記録する。`SQLITE_BUSY` は待たず即 rollback し、同じ owner をその場で 10ms、20ms、40ms、以後 50ms の間隔で再試行する。再試行も 250ms の cycle 予算に含め、予算を使い切った owner は次 cycle に回す。
4. **読戻し**: staging 行だけを読み戻して判定と値を再計算し、2 の結果と一致しなければ set を捨てて次 cycle でやり直す。捨て方は 6 の回収と同じで、slice 行を消し、shadow は set 行も消し、event は spread を保ったまま `awaiting` に戻す。不一致の set は complete にならない。
5. **完備 CAS**（core lock 内）: scheduler の書込み要求フラグが無いときだけ core lock を non-blocking で試し、取れなければ次 cycle に回す。取れたら 3 と同じく writer admission mutex の中でフラグを再確認し、立っていなければ `BEGIN IMMEDIATE` まで行ってから mutex を放す。立っていれば core lock を解放して yield する。transaction では「set が staging・現世代・行数 = `expected_rows` なら complete」と「shadow が期待 state / `updated_at` なら終端値」を書く。O(1) 行で、core lock の保持と commit 前の経過はともに 50ms 上限、超えれば rollback する。commit の所要は計測して記録する。
6. **回収と cancelled 掃除**: worker は起動ごとに世代台帳へ 1 行を commit し、その id を現世代とする。世代は DB に永続し単調増加なので、旧世代の staging を現世代と誤認しない。起動時は旧世代の `staging` set を、毎 cycle は `cancelled` set に残る staging 行を、lock 外で 240 行ずつ消す。event set は spread 列を保持したまま `awaiting` に戻し（世代は NULL）、shadow set は set 行も消して候補として作り直す。記録済みの発表前 spread は再起動で失われない。`awaiting`・complete / unrecoverable は変えない。`cancelled` は slice 行だけを掃除し、状態は変えない。
7. **競合**: 2 worker が同じ owner を処理しても、世代と期待 state の CAS で成功は 1 回だけになる。

scheduler 専用の write 接続は無い。scheduler は executor・broker・trade loop・reflection・calendar・news と共有する core 接続で書き、signal maintenance は同じ tick の中で signal 用の接続から書く（§1）。scheduler は writer admission mutex の中で、DB write を含み得る tick の開始前に書込み要求フラグを立て、mutex を解放してから書く。フラグはその tick で 2 接続が開いた write transaction がすべて commit / rollback するまで維持し、最後に同 mutex 内で下ろす。登録は writer 取得試行より必ず先で、worker の確認と BEGIN に対して全順序になる。既存接続の busy timeout 5000ms は変えない。新設の worker write 接続だけを busy timeout 0 にし、競合は即 rollback する。フラグを持たない既存 writer（improve の長い `BEGIN IMMEDIATE`、ops audit、outage 観測、別プロセスの news 操作）と scheduler の競合は現行どおり 5000ms の中で吸収する。worker はそれらと競合すれば即 rollback するので、scheduler の待ちを増やさない。worker transaction 中に要求が来た場合だけ、scheduler は残りの commit 前 50ms 以下と worker の commit 所要を待ち得るが、新しい worker batch は入らない。scheduler の待ちは、tick 内の各 write transaction の最初の文を出してから戻るまでの時間として測る（§5-12）。kill switch・day close・SL / TP の scheduler thread は worker の完了通知を待たない。tick 中の印と次 tick 予定は負荷抑制の観測値として残すが、writer 排他の正しさには使わない。

### 3.5 1m 共通規則、退避、反応統計

1. 足は tz-aware UTC の `[bar_time,bar_time+1m)`。OHLC は有限正数で high / low の整合を満たす。
2. 同じ pair / minute の複数 source は、OHLC が既存の数値一致（許容誤差 1e-9）で一致すれば 1 本へ畳み、不一致なら障害欠損と同じ unresolvable にする。畳んだ行の代表 source は、live cache の保存 source、次に history の source 名の昇順で選ぶ。
3. 足が無い minute が `datafeed_outage_gap` に含まれる場合は障害欠損とし、shadow は `data_insufficient`、reaction window は `unresolvable` で除外する。それ以外は rollover など tick 無しとし、直前 close の flat OHLC、volume=0 で前値保持する。先頭 minute で直前 close も無ければ `unresolvable`。gap-open は次の observed open をそのまま使う。この分類は shadow / reaction で共有する。
4. 区間の先頭が cache / history の保持範囲（その pair の最古の 1m 足）より前なら、元足は保持で消えているので set を `unrecoverable` で終端し、shadow は `data_insufficient`、reaction の 3 窓は unresolvable に数える。
5. event の退避対象は、importance が `econ.window.importance_min` 以上、country が settings pair の通貨か `All`、`superseded_by IS NULL` の row で、対象 pair ごとに 1 set とする。set の作成は event 保存と同じ transaction を要求しない（event 保存は 1 行ごとに commit する、§1）。calendar refresh が取得結果の全 row を保存し終えた直後に、取得結果全体を入力とする冪等な 1 transaction の pass で、§3.1-7 の supersede、対象 event × pair の `awaiting` set の作成（既存 set は UNIQUE で作らない）、superseded になった event と importance 低下・pair 削除で対象外になった event の `awaiting` / `staging` set の `cancelled` 化を行う。保存が途中で失敗した refresh ではこの pass を行わず、保存済み row は次の refresh か worker 起動時の補完で拾う。worker 起動時は supersede を除く同じ pass（作成と cancelled 化）を保存済み row 全体に行い、設定変更・旧 event による不足 set を補う。`cancelled` の set の event が設定変更（importance_min の引き下げ、pair の再追加）で再び対象になれば、pass は §3.4-6 の毎 cycle 掃除後に slice 行が 0 件であることを確認し、0 件なら `completed_at` / `generation` / `purged_at` を NULL にして同じ set を `awaiting` に戻す（UNIQUE のため新しい set は作らない）。行が残れば `cancelled` のまま見送り、次の pass で再試行する。発表前に記録済みの `spread_pips` / `spread_source` は保持する。superseded 由来の `cancelled` は復帰させない。区間は `[ts-15m,ts+4h)` の 255 本、確定は `ts+4h+1 日`。元足が保持範囲に残る過去 event も同じ規則で退避する。
6. event の spread は、worker が `[ts-5m,ts)` に対象 pair の quote を §3.2 と同じ取得経路で取り、同じ条件で `quote` / `assumed` を決め、spread が NULL の `awaiting` set に 1 回だけ CAS 保存する。再起動時も NULL でまだ同区間なら再試行する。ts までに記録できなかった set は ts 以後の最初の cycle で assumed spread と `assumed` を CAS 保存し、後から quote へ置換しない。spread 候補は成熟済み slice の解決より常に先に処理する。
7. 反応値の式（pip は pair の pip_size、窓 H ∈ {15,60,240} 分）:
   - 基準 close B = `[ts-1m,ts)` 足の close
   - 対象足 = 開始時刻 `ts, ts+1m, …, ts+H-1m` の H 本
   - `range_pips` = (対象足の max(high) − min(low)) / pip
   - `close_change_pips` = (C_H − B) / pip。C_H は開始 `ts+H-1m` の足の close
   - `abs_close_change_pips` = |close_change_pips|
   - `net_abs_close_change_pips` = abs_close_change_pips − set の `spread_pips`（往復 1 回分の全幅）。負値もそのまま残す
   - 基準足か対象足に outage marker、source 不一致、前値の無い先頭 flat があれば、その窓は unresolvable
8. 3 窓は set が complete になった直後の worker cycle で `econ_reaction_values` に 1 回書き、以後変えない。集計は窓 × 束ごとに range / abs / net の median / p75 と、符号付き close_change の median を返す。
9. 集計の一意キーは `(event_id,currency)`。通常 event は country の 1 通貨、`All` は settings pairs に含まれる全通貨へ 1 回ずつ展開する。各通貨の価格系列は、反応値を書く時点の settings pairs の並びでその通貨を含む最初の pair を canonical pair とし、`(event_id,currency)` → set の対応を、その canonical pair の set の反応値と同じ transaction で 1 回だけ保存する（先に完備した別 pair の set には結ばない）。後から pair の並びや集合が変わっても保存済みの対応は変えず、集計は保存済みの対応だけを使う。同じ event の別 pair set は保存しても同じ通貨束へ重複加算しない。束は `(currency,importance)` と `(currency,name)`。superseded row は除外し、同時刻の別名は別 event。人間面は `n` / `unique_ts_n` を併記する。
10. trade agent の開示床は束 × 窓で n>=20。床未満は `status=insufficient` だけ、人間面は n と数値を出す。pair 入力には、その base / quote currency の束を返し、値は 9 の canonical pair から算出する。pair は `settings.pairs` の enum に限定し、watch symbols は受けない。cron trade mission にだけ見せ、ask / improve / 専用採否 mission へ登録しない。
11. **保持と容量。** LLM 呼出枠（`plugin.signal_min_interval_min` / `plugin.signal_daily_max`。`system_filtered` は不算入）と、全 signal / shadow の保存容量を分離する。最小保持日数 D = 400 日（set の `created_at` 起点）。producer は承認・admit 済みの executable strategy の `(name, content_hash)` ごと、対象 pair ごと、bar ごとに最大 1 signal を既存 UNIQUE 制約で保存する。content hash の更新直後は新 cursor が鮮度窓を再評価するため、更新 1 回につき最大 `k × pair 数` 本だけこの上限を超え得るが、80% 警告の範囲で扱う。日次上限 `S_producer` は `Σa (|a.pairs ∩ settings.pairs| × ceil(1,440 / timeframe_minutes(a)))`。任意設定 `econ.signal_capacity_daily_max` は config 読込時には 1 以上の整数だけを検査し、省略時は `S_producer`、指定時はその値を S とする。起動時に上書き値が `S_producer` 未満なら起動を拒否する。実行中の strategy 承認・pair・timeframe の変更で下回ったら S=`S_producer` に切り上げ、status に `capacity_override_below_producer` を出す。S の比較と再計算は T1c / T2a-2 が持つ。

    行容量 C は `ceil(D × (S × 1,680 + E × 12 / 365 × 255 × P))`。1,680 は shadow 1 owner の上限（§3.4-6）、P は settings pair 数である。E は importance_min 3 / 2 / 1 ごとの全保存行 117 / 238 / 1,004 を 66 日で割り、1 月 = 365/12 日として月換算して切り上げた 54 / 110 / 463 event / 月を使う。例として importance_min=3、15m strategy 1 本 × 1 pair、上書きなしなら S=96、C=64,693,085 行になる。この C は配備時の inventory ではなく、bench の測定・外挿に使う固定例である。

    byte 容量は `B = B0 + C × β`。B0 は production と同じ fresh DDL / index / WAL / synchronous 設定で、DDL commit 後に `wal_checkpoint(TRUNCATE)` し、slice 0 行の DB + WAL bytes を 3 回測った最大値。β は固定例 C の代表行数 `Ci = unique(ceil(C/1000), ceil(C/100), ceil(C/10))` ごとに `ceil((size(Ci) − B0) / Ci)` を求めた上側最大値で、`size(Ci)` は signal / event 比で Ci 行を 240 行以下の transaction に分け、最後の COMMIT 直後・明示 checkpoint 前に測る。各 run は fresh DB を作り、B0 測定後は production と同じ auto-checkpoint に任せる。この手順を各 Ci で 3 回行い、全点で `B0 + Ci × β ≥ size(Ci)` を満たす B0 / β だけをコードに置き、C 自体は `B0 + C × β` で外挿する。capacity job 全体は 4 時間以内、job が同時に占有する test 用 DB / WAL / 成果物は 4 GiB 以下とし、どちらかを超えたら fail とする。

    D 未満の set は削除しない。D を過ぎた complete / unrecoverable の set だけ、set 単位で slice 行を 240 行ずつ消して `purged_at` を付ける。set 行、shadow の終端、reaction 値 snapshot は残す。status と親 report は S の導出値 / 上書き値、`capacity_override_below_producer`、現在行数/C、B0 / β、DB bytes、WAL bytes、同じ filesystem の空き bytes を常時出す。byte 比較の「slice 関連」は新 7 表 `signal_decisions` / `signal_shadow` / `ohlcv_slice_sets` / `ohlcv_slices` / `ohlcv_slice_generations` / `econ_reaction_keys` / `econ_reaction_values` とそれらの index に固定し、bench と実行時の双方が同じ集合を `dbstat(aggregate=1)` で測って B0 に空 DDL からの増分だけを足し、transcript など無関係な表の増分は含めない。行数またはこの slice 関連 bytes のどちらかが 80% / 100% に達したら警告する。取引は止めず、D 未満は消さない。

### 3.6 agent 面と既存 reader

| 面 | 出すもの | 出さないもの |
|---|---|---|
| trade prompt / state summary | current window、対象 event、次 event、freshness | 採否率、shadow 成績、reaction 自動注入 |
| cron trade mission tool | 既存の読み取り tool から signal 一覧を除いた集合。束 2 で `settings.pairs` の reaction（床以上だけ数値）を足す | signal 一覧（pending signal は専用 mission の領分）、watch symbol、床未満 n、shadow、他 pair |
| 専用採否 mission tool | ニュース検索と建玉一覧の 2 つだけ | signal 一覧（正本値は prompt に注入）、価格・指標（方向不一致は reject 理由にならない）、calendar（帯判定は決定論）、reaction |
| ask | 現行と同じ集合（signal 一覧を含む）を ask 専用の一覧として分離する。ask は人間が起動する | reaction tool |
| signal prompt | signal 正本値、二値採否、2 reject code | shadow 過去成績、変更可能な数値 field |
| reflection / RAG / improve agent | 従来の実注文・改善入力 | decision / shadow / reaction / 親 report |
| activity | 固定語と参照 ID | shadow pnl・勝率・分位 |
| 人間面 | decision 母数、accept / reject shadow、abandoned、reaction 全束、slice 容量 | agent prompt への再注入 |

status / web / 親 report は abandoned を `freshness_expired` / `requeued_expired` / `retry_exhausted` / `source_inactive` / `normalization_failed` / `deploy_op` / `deploy_linked` の理由別に件数表示し、`ineligible_cleanup` と移行前の `legacy_unknown` は別枠にする。期間合計と直近運用日は `abandoned_at` で帰属させ、時刻の無い `legacy_unknown` は累計だけに出し、accept / reject と合算しない。`worst_case_guarantee_unavailable` は静的 status として常時表示する。`signal_wait_budget_warning` は §3.2-7 の実測値、標本数、代用有無、active / inactive、直近通知時刻を status / web に恒常表示し、親 report に日次 snapshot を残す。

既存 reader は次の意味へ更新する。

- improve performance report: signal mission を hold_rate の分母から外し、`econ_window` を reject_breakdown に入れない。
- gate reject streak: `econ_window` を streak に数えず Discord 通知もしない（trade loop 側の reader）。
- trade event sink: intent → mission trigger の既存 origin を保ち、専用 signal mission を signal origin として読む。
- mission status の既存 reader: `system_filtered` を既知の終端 filter として表示し、失敗や running に数えない。

activity 固定語は次の 9 個だけ追加する: `signal_filtered_econ_window`、`signal_accepted`、`signal_rejected_by_llm`、`signal_decision_invalid`、`open_rejected_econ_window`、`limit_fill_suppressed_econ_window`、`econ_calendar_stale`、`econ_calendar_fresh`、`signal_wait_budget_warning`。

親専有 report は既存 baseline monitor と同じ親プロセス側の owner-only 領域へ出す。T2a-2 が scheduler / service に「市場の当日 close 後に 1 回」と「起動時に当日分が無ければ 1 回」の hook API を置き、T5 はその hook に生成関数を差す。RAG / tool / worker へ保存先を渡さない。

## 4. 配線表

配線表は実在ファイルと新規ファイルだけを列挙する。関数名は実装時に各ファイルの既存流儀へ合わせる。「所有」列は §7 の task で、1 ファイルを持つ task は 1 つだけとする（例外は行内に書く）。

| 場所 | 配線 | 所有 |
|---|---|---|
| `src/agentic_fx/config.py` / `config/settings.yaml.example` / `tests/fixtures/settings_examples/v5.yaml` | 任意 `econ.window`、3 既定値、任意 `econ.signal_capacity_daily_max`。config 層は容量上書きを整数 1 以上とだけ検査し、`S_producer` との比較はしない。schema_version は 5 のまま、example と v5 fixture は byte 同期し、v4 → v5 migrate chain は `econ` を注入しない | 束1 T1a |
| `src/agentic_fx/store/db.py` | `signal_decisions`、`signal_shadow`、`ohlcv_slice_sets`、`ohlcv_slices`、`ohlcv_slice_generations`、`econ_reaction_keys`、`econ_reaction_values` の fresh DDL / index / CHECK / table list。`econ_events.superseded_by`。`trade_intents` の `econ_window` CHECK rebuild と、`signals` の rebuild（`contract_version`・`abandon_reason`・`abandoned_at` と CHECK、既存 abandoned は reason=`legacy_unknown` / `abandoned_at=NULL`）。`_SIGNALS_V2_DDL` は変えず `{ine}{name}` 型の v3 DDL を別に置き、fresh は v3、既存は `_migrate_signals_fk` の後に v3 rebuild とする。`signal_decisions` は `UNIQUE(id, signal_id)` を持ち、`signal_shadow` は `(decision_id, signal_id)` の複合 FK で結ぶ（decision / actor / reject_code の snapshot 一致は T1c の writer 契約）。`ohlcv_slice_sets` は `expected_rows >= 0` と `range_start < range_end` の CHECK を持つ。T1a は §3.3 の PK / UNIQUE だけを作り、候補抽出 index が必要なら T4c bench 前に ownership を戻して追加する。busy timeout は変えない。既存 `data_migrations` に対する name 引数の冪等 helper と `init_db` の呼出し順を T1a が置き、T1b が正規化 migration の lazy-import 呼出し 1 行だけを引き継ぐ | 束1 T1a → T1b（呼出し 1 行）→ T4c（追加 index が必要な場合だけ） |
| `src/agentic_fx/store/signals.py` | T1a は現存する abandoned 書込み（`expire_stale`、requeue / lease 回収の共有式、claimed 即時 abandoned、deploy op pending abandoned）へ reason と `abandoned_at` を付ける最小変更だけを行う。`abandon_claimed()` は store 側で理由を判別できないので、UTC 時刻と reason を引数で受ける形に変え、reason を知る呼出側 `loops/trade_loop.py` の 3 箇所（source inactive / stale の 2 箇所と maintenance の 1 箇所）の引数追加も同じ T1a が行う（`trade_loop.py` のこの 3 行だけ。T1a → T2b-1 に引き渡す）。T1b は eligibility / migration / cleanup の共通分類 query、tx 合成できる consume、集計、旧 payload の正規化 migration / `normalization_failed` を実装する | 束1 T1a → T1b |
| `src/agentic_fx/store/missions.py` | T1a は起動時の中断回収で abandoned にする既存経路へ reason と `abandoned_at` を付ける最小変更だけを行う。T1b は `system_filtered` 終端と commit を外側へ委ねる終端、signal rate から `system_filtered` を除外する | 束1 T1a → T1b |
| `src/agentic_fx/store/signal_decisions.py`（新規） | 3 正規形、signal 一意性、decision / shadow / consume / mission の tx | 束1 T1c |
| `src/agentic_fx/store/signal_shadow.py`（新規） | accept / reject shadow、spread snapshot、候補 query、CAS | 束1 T1c |
| `src/agentic_fx/store/ohlcv_slices.py`（新規） | set 台帳、refresh 後 pass と起動時補完の store 操作（awaiting 作成・cancelled 化・再有効化）、staging 行の `BEGIN IMMEDIATE` 小分け書込み、完備 CAS、世代台帳と回収、保持期限の purge、S / C / B0 / β による容量算定 | 束1 T1c |
| `src/agentic_fx/plugin/signal_producer.py` / `src/agentic_fx/plugin/strategy_adapter.py` | live の執行可能 payload を backtest と同じ規則で保存前に正規化し `contract_version` を記録。adapter の replay 挙動は変更しない | 束1 T1b |
| `src/agentic_fx/core/econ_window.py`（新規） | inclusive window、All、連結成分、bar 区間交差の純粋判定 | 束1 T2a-1 |
| `src/agentic_fx/store/econ_events.py` / `src/agentic_fx/store/fetch_attempts.py` / `src/agentic_fx/datafeed/econ_calendar.py` | 発表後を含む範囲、All、importance、superseded 除外、7 日 next event、fetch attempts の success / failure。全 row 保存に成功した refresh の直後に、supersede（未発表・7 日以内だけ）・awaiting 作成・cancelled 化・再有効化の pass を配線する | 束1 T2a-1 |
| `src/agentic_fx/core/scheduler.py` | cron 優先と signal 後続の維持、足区間基準の候補と bar 区間 limit 抑止、抑止通知の dedupe を `alert_state` に保存、resolver を同期 hook にしない。市場の当日 close 後 1 回だけ親 report callback を呼べる hook API を T2a-2 の出力にする | 束1 T2a-2 |
| `src/agentic_fx/service.py` | T2a-2: signal 起動判定を eligible + rate に変更、静的保証 status と実測 warning / dedupe、起動時 / 実行中の S と `S_producer` の比較、signal id と終端種別を受けて commit 後に core lock 外で呼ぶ warning 評価境界（maintenance 終端は service 内から呼ぶ）、scheduler の DB 書込み要求フラグ（`conn_core` と `conn_signal` の両方を覆う）・tick 中の印・次 tick 予定の公開、親 report の close 後 1 回 + 起動時 catch-up hook API。T4c: slice worker の起動・停止・catch-up、worker 専用の read-only / write 接続（write だけ busy timeout 0）、起動時の補完 passを追加し hook を保つ。T5: report 生成関数を hook に差す | 束1 T2a-2 → T4c → T5 |
| `src/agentic_fx/loops/signal_decision_contract.py`（新規） / `src/agentic_fx/loops/summary.py` | 専用採否 schema と parser、orders 照合。cron は従来 schema。calendar summary を pair view に変更（summary は mission status を読まないので `system_filtered` の変更は無い） | 束1 T3 |
| `src/agentic_fx/loops/trade_loop.py` / `src/agentic_fx/loops/mission_finalize.py` | T2b-1: claim する trigger から cron を外して cron の claim / injection を廃止、cron / ask / 専用採否 mission の tool 一覧を分離、専用採否の claim と tx flow。T2b-2: commit-core の f 判定、quote spread snapshot、commit 前 window 再判定、gate reject streak の `econ_window` 除外、固定 activity、decision / freshness 系 abandoned の commit 成功後に T2a-2 の warning 評価境界を core lock 外で呼ぶ。T2b-1 から T2b-2、束1完了後に束2 T3へ順次引き渡す | 束1 T2b-1 → T2b-2 → 束2 T3 |
| `src/agentic_fx/core/risk_gate.py` | `GateContext` の window 結果で裁量 OPEN だけを拒否 | 束1 T2b-2 |
| `src/agentic_fx/loops/prompts/trade_mission.md` | 15m 表記、帯内新規 entry 禁止、帯外では指標だけを理由に見送らない、cron と signal 契約の分離 | 束1 T2b-2 |
| `src/agentic_fx/core/econ_slice_worker.py`（新規） | §3.4 の writer 優先制御、worker cycle、shadow 終端、発表前 spread の優先 CAS、読戻し不一致の破棄、世代回収、cancelled の後始末、complete set の後処理差込口 | 束1 T4b |
| `src/agentic_fx/datafeed/econ_bars.py`（新規） | cache / history、outage / no-tick 分類、保持範囲判定、代表 source、slice 用 1m 正規化 | 束1 T4a |
| `src/agentic_fx/core/paper_fills.py` | live / shadow の limit / exit 保守則を共用 | 束1 T4a |
| `src/agentic_fx/loops/improve_context.py` / `src/agentic_fx/loops/prompts/improve_mission.md` | hold_rate / reject_breakdown の新しい母集団 | 束1 T5 |
| `src/agentic_fx/ops/trade_event_sink.py` / `src/agentic_fx/status.py` / `src/agentic_fx/commands.py` | trigger / `system_filtered` / abandoned / 容量警告 / 小集計の既存 reader 更新 | 束1 T5 |
| `src/agentic_fx/loops/econ_signal_report.py`（新規） / `src/agentic_fx/loops/baseline_monitor.py` / `data/improve_reports/econ_signal/` | owner-only の日次親専有 report 生成関数を作り、T2a-2 の close 後 1 回 + 起動時 catch-up hook に T5 が差す | 束1 T5（束2 T4 が reaction 節を追加） |
| `src/agentic_fx/ops/service.py` / `src/agentic_fx/ops/client.py` / `afx-ops/afx_ops/web/render.py` / `afx-ops/afx_ops/web/routes.py` | allowlist DTO から status / web の人間面を表示 | 束1 T5（束2 T4 が reaction 節を追加） |
| `src/agentic_fx/tools/signal_tools.py` | improve allowlist と禁止集合の非交差 assert | 束1 T5 |
| `src/agentic_fx/store/econ_reaction_values.py`（新規） / `src/agentic_fx/analytics/__init__.py`（新規） / `src/agentic_fx/analytics/econ_reactions.py`（新規） | complete event set から 3 窓の値 snapshot、3 窓 × 2 束、median / p75、床 projection | 束2 T2 |
| `src/agentic_fx/tools/market_tools.py` / `src/agentic_fx/tools/mission_registry.py` | reaction tool を settings pairs 限定で登録し、cron trade mission の一覧にだけ加える（一覧の分離は束1 T2b-1 済み）。ask / improve / 専用採否 mission には加えない | 束2 T3 |
| `policy/directives.md` | 追跡外の利用者データで、コードは読み書きしない。既定文は §8 J3 の全文（承認を前提）と fixture。配備時に利用者が `afx ctl policy add` で登録し、旧 1 行目を手で消す | 利用者 |
| `tests/fixtures/econ_signal_policy_default.txt` / `econ_signal_claude_accept.json` / `econ_signal_claude_reject.json`（新規） | §8 J3 の既定文、T3 が 1 回だけ抽出して追跡する §5-4 の実 CLI transcript 由来固定 JSON。merge 前 job は tmp 出力を照合するだけで追跡 fixture を書き換えない | 束1 T3 |
| `tests/fixtures/econ_window_history_20261016_1031.sqlite` / `tests/fixtures/build_econ_window_history_fixture.py` / `tests/audit/test_econ_history_regeneration.py`（新規） | §5-14 の列 allowlist 抽出スクリプト、read-only 履歴 snapshot、970 / 3 / 57 再生成。snapshot は missions=`id,loop,trigger,status,started_at`、trade_intents=`id,mission_id,action`、signals=`id,status,kind,claimed_by_mission_id` だけを保持し、payload / output / transcript と他表を含めない | 束1 T5 |
| `tests/store/test_econ_signal_schema.py`（新規） | schema / rebuild / migration / store の test。所有は T1a → T1b → T1c の順に引き渡す | 束1 T1a → T1b → T1c |
| `tests/core/test_econ_window.py`（新規） / `tests/core/test_signal_wait_budget.py`（新規） | 前者は T2a-1、後者は T2a-2 が所有し、それぞれの出力と一緒に後続へ渡す | 束1 T2a-1 / T2a-2 |
| `tests/loops/test_signal_decision_flow.py`（新規） | contract / realbackend case を T3、claim〜未確定 flow を T2b-1、commit-core と warning 評価呼出しを T2b-2 が順に追加し、file ownership も同順で渡す | 束1 T3 → T2b-1 → T2b-2 |
| `tests/core/test_econ_shadow_worker.py`（新規） | 1m / fill fixture は T4a、worker state は T4b、service / 実ディスク / 容量は T4c が順に追加し、file ownership も同順で渡す | 束1 T4a → T4b → T4c |
| `tests/analytics/test_econ_reactions.py`（新規） / `tests/test_econ_agent_isolation.py`（新規） | 前者は束2 T1 → T2、後者は束1 T5 → 束2 T4 の順に引き渡す。実ディスク・容量・履歴再生成は既存 `bench` marker、Claude 実往復は既存 `realbackend` marker で通常 suite から外す | 束2 T1 → T2 / 束1 T5 → 束2 T4 |

新規の `tests/audit/` と `tests/analytics/` にはそれぞれ `__init__.py` を置き、既存 tests の package 収集規約を保つ。

DB migration は `db.py` の既存流儀に従う。新 7 表 / index は fresh schema と `CREATE TABLE IF NOT EXISTS` を同じ DDL から作る。列追加は冪等に行う。`trade_intents` と `signals` の rebuild は共通 DDL、事前 backup、FK 無効化を transaction 外で行い、新表作成 → 全行 copy → 行数照合 → 旧表 drop → rename → signals を参照する子表 3 本（`plugin_deploy_op_signals` / `signal_decisions` / `signal_shadow`）の FK 検査 → commit、finally で FK を戻す。子表 3 本の違反は失敗、signals と無関係な表の既存違反は数えて技術ログに warning を出し、移行を止めない。CHECK 本文の冪等 guard は文字列リテラルの中を 1 文字も変えず、リテラル外だけ空白を落としてキーワードの大小を畳み、列レベルと表レベルの CHECK を区別せず集合で比べる。signals は RENAME 先行の `_migrate_signals_fk` ではなく、参照される表を扱う `_migrate_trade_intents_observability`（§1）を前例にする。signals の copy では既存 abandoned に reason=`legacy_unknown` / `abandoned_at=NULL`、それ以外に両方 NULL を入れ、`contract_version` は NULL で写す。CHECK 本文も冪等 guard に含め、2 回目初期化で rebuild しない。`missions.status` は現行に CHECK が無いため DDL rebuild をせず reader と test を更新する。`signals.status` の 4 値は変えない。T1a は v3 rebuild と既存 `data_migrations` 用 helper・呼出し順の枠までを作り、旧 payload の正規化は T1b が live 保存規則と一緒に実装して v3 rebuild の直後に走らせ、key と記録行を所有して 2 回目は何もしない。

## 5. 受入条件

同じ不変条件の fixture を重複して作らない。§5-10 / 30 の set 作成・cancelled・再有効化、§5-11 / 31 の canonical pair・反応値不変、§5-13 / 16 / 35 の agent 面遮断、§5-2 / 6 / 17 / 18 の signal 起動・期限・分類は、それぞれ 1 つの下位 test / fixture を複数の受入番号から参照する。受入番号ごとの期待値は残すが、同じ setup と判定を別 test に複製しない。

1. **既存境界表 test を最初に通す。** importance=3 の USD event `ts`、30 / 15、USDJPY を固定する。

   | 入力 | 期待 |
   |---|---|
   | `now=ts-30m` / `now=ts+15m` | 帯内 |
   | 端の 1µs 外 | 帯外 |
   | importance=3 CAD、USDJPY | 帯外 |
   | `country='All'` | 帯内 |
   | 発表後 15m 内 | query から落ちず帯内 |
   | 未発表の同名 row の時刻が 7 日以内で変わる | 旧 row は superseded、帯を作らない |
   | 既発表の同名 row / 7 日を超えて離れた同名 row / 同じ取得結果に並ぶ同名 row | superseded にならず別 event として残る |
   | bar が帯の前端を跨ぐ / 後端を跨ぐ | `now` が帯外でも足区間の交差で limit fill 抑止 |
   | 足 `[b,b+1m]` を `now=b+5m` で処理、event `ts=b−15m`（後端交差） | `now` 基準の候補には入らないが、足区間基準の候補で limit fill 抑止 |
   | 同じ帯の 3 tick（途中で再起動を挟む） | activity 1 回、pending のまま、期限延長なし |
   | close / cancel / day close / SL / TP | 従来経路を通る |

2. cron と専用採否 mission を同じ判断足で due にし、cron が先に 1 回受理され watermark が進み、通常 schema で裁量判断すること、cron が signal を claim しないこと、signal が pending のまま次の idle tick で専用 mission に 1 件だけ claim されることを test する。2 mission は同時に runner / commit-core へ入らない。cron mission の tool 一覧に signal 一覧が無く、ask の一覧には残り、専用採否 mission の一覧はニュース検索と建玉一覧だけであることも確かめる。
3. 専用 schema は accept+null / reject+2 語だけを通す。未知 key/code、相関不一致、hold、通常 intent は schema / Python の片方だけを迂回しても失敗する。逆建玉なしの `opposite_position` も `signal_decision_invalid`、failed、requeue になる。
4. **Claude 実往復（merge 前必須、`realbackend` profile）。** `runner.trade.backend=claude`、設定済み `runner.claude.bin`、サブスクリプション認証だけを使い、accept と reject を各 1 往復する。reject 入力は同じ pair に逆方向の `OPEN` order がある場面へ固定し、最大 3 往復で正しい reject が 1 回も得られなければ fail とする。両 case で実 CLI に `--json-schema` を渡し、最上位 type が object で anyOf / allOf / oneOf を持たないこと（実測では最上位 anyOf 等は 400）、property type、最上位 if/then/else による相関、parser の相関を検証する。sanitized transcript と CLI version / model / schema hash は `tmp/econ-window-acceptance/claude/` にだけ出す。追跡する固定 JSON は T3 が最初の確定時に 1 回だけ抽出して commit 対象へ置き、merge 前 job は tmp の応答をその形と照合するだけで `tests/fixtures/econ_signal_claude_accept.json` / `econ_signal_claude_reject.json` を書き換えない。通常 suite では既存 `realbackend` marker により除外してよいが、§7 の merge 前 job は `--fail-on-skip` で両 case の成功を必須にする。Anthropic API key は使わない。
5. accept は intent 記録前に window を再判定し、帯へ入っていれば intent 0、system reject、reject shadow、consume、`system_filtered`。帯外なら intent / decision / accept shadow / consume / completed が同じ transaction で確定後に executor が走る。通常の資金 gate 拒否でも decision=accept と accept shadow は残る。
6. lock timeout、snapshot stale、source inactive、signal stale、data state not ready、未 claim の鮮度切れの表を test し、decision / shadow が 0、signal が指定どおり requeue / abandoned になり、reason が §3.3 の表どおりになる。signal stale は commit-core の時刻 > f で判定し、runner が f 後に戻った場合も abandoned、requeue 歴の有無で `freshness_expired` / `requeued_expired` に分かれる。abandoned は decision 分母に入らない。
7. 任意の decision / shadow / consume / mission 書込み失敗で transaction 全体が rollback し、finalized / consumed は false。retry しても decision は signal ごとに最大 1 行。DB CHECK は NULL 三値を含む不正形を拒否する。
8. shadow fixture は accept / reject、market、limit 未到達、gap fill、同一足 SL+TP、TP のみ、day close、rollover 後に約定した limit（翌 rollover の day close まで、1,680 行以内）、long / short、spread `quote` / `assumed`（bid = ask の quote と鮮度切れ quote は assumed）を通す。accept shadow の actor は `llm`。2 worker の CAS 成功は 1 回。filled は day close + 猶予後に必ず終端する。
9. 欠損を 2 分類する。`datafeed_outage_gap` に重なる minute は shadow `data_insufficient` / reaction `unresolvable`、gap 外の no-tick minute は前値 flat で解決する。猶予前の backfill は正常解決、猶予後の backfill は確定値を変えない。区間先頭が保持範囲より前の owner は `unrecoverable`。
10. 前半（束1 T2a-1 の所有）: 全 row を保存した refresh の直後の pass で対象 event × pair の `awaiting` set ができ、2 回目の pass で増えない。superseded になった event と importance 低下で対象外になった event の未完 set は `cancelled` になり、worker の候補から外れる。保存が途中で失敗した refresh では pass が走らず、次の refresh で補われる。後半（束1 T4b の所有）: `[ts-5m,ts)` の spread 候補と成熟済み slice を同時に積み、spread が先に 1 回だけ CAS 記録されること、記録前の再起動は同区間なら再試行すること、ts までの取り逃しは `assumed` で固定されることを通す。その後 event `[ts-15m,ts+4h)` の 255 本と shadow の §3.4 区間を退避し、元 cache / history を消しても同じ結果を再現する。staging 途中の停止では未完行が reader から見えず、再起動後に記録済み spread が残ったまま set が `awaiting` へ戻り、同じ結果へ戻る。世代台帳の id は再起動ごとに増え、旧世代の staging 行を現世代として完備にしない。
11. reaction fixture は 15 / 60 / 240 本、基準 close、range = max(high) − min(low)、終端 close、net が負になる例、重複一致（代表 source の選択）/ 競合、同時刻別 event を固定する。同じ通貨を含む複数 pair では settings 順の canonical pair だけが `(event_id,currency)` に 1 回入り、`All` は全対象通貨へ各 1 回入る。pair 入力から base / quote の束へ写す fixture も固定する。退避 fixture で importance=3 の USD 22/38、JPY 9/12 を再現する。床 19 は数値非開示、20 は n / median / p75 開示。pair は settings pairs だけで、ask / improve / 専用採否 mission の tool list には存在しない。
12. **writer 優先の実ディスク実測（merge 前必須、`bench` profile）。** 本番の `data/` と同じ filesystem、同じ journal / synchronous 設定の実 SQLite file で測る。tmpfs では判定しない。通常 suite では既存 `bench` marker により除外してよい。§7 の merge 前 job は project 配下 `tmp/econ-window-acceptance/disk/basetemp` を `--basetemp` に指定し、試験自身が `data/` と `st_dev` の一致を検査する。不一致・必要条件不足・skip は fail とする。試験は自分で作ったファイルだけを終了時に消し、測定 JSON は `tmp/econ-window-acceptance/disk/` に残す。同じ job ではこの latency 測定を §5-20 の capacity 投入より先に完了し、両者の間で `sync` を実行する。drop_caches は使わない。最大量（25 owner × 1,680 行）の staging と scheduler writer を並行させ、次の 3 場面を測る。
    - worker と scheduler だけ: worker transaction 中に scheduler が書込み要求を登録する最悪 interleaving で、その後に新規 worker batch が始まらず、既存 batch の commit 前経過が 50ms 以内に終わって scheduler が取得する。scheduler の write 待ち p99 ≤ 50ms、最大 ≤ 100ms、待ち発生 ≤ 1 回 / tick。
    - フラグを持たない長い writer を 1 本加える（improve 型: `BEGIN IMMEDIATE` の中で file I/O をして 2 秒保持）: scheduler は既存の 5000ms の中で取得し `SQLITE_BUSY` 0。worker はその間 transaction を確立できず、即 rollback と規定 backoff だけを繰り返す。scheduler の待ちから長い writer の保持時間を引いた残りが、上の閾値を満たす。
    - signal 接続から maintenance が書く tick: フラグが tick の終わりまで立ち続け、その間に worker batch が入らない。
    共通: scheduler 側 `SQLITE_BUSY` 0、core lock 待ち p99 ≤ 10ms、tick 最大所要 ≤ worker 無し対照 + 100ms、既存接続の busy timeout は 5000ms のまま。worker は要求フラグ時に yield、各 transaction は `BEGIN IMMEDIATE`・240 行以下・commit 前 50ms 以下で、commit 所要が記録される。core lock 50ms 以下、1 cycle 25 owner / DB 作業 250ms 上限を満たす。
13. calendar は unknown、fresh、直近 failure、12h stale、回復を通す。固定 activity は §3.6 の 9 語だけ。`econ_window` は gate streak / Discord / improve reject_breakdown に入らず、signal mission は hold_rate 分母に入らない。`system_filtered` は status / web / ctl の mission 表示で既知 terminal として表示する。
14. **履歴 DB 再生成（merge 前必須、`bench` audit profile）。** 入力は本機 DB から追跡する抽出スクリプトで固定した read-only snapshot `tests/fixtures/econ_window_history_20261016_1031.sqlite` とし、test はこの fixture を変更しない。保持するのは missions の `id / loop / trigger / status / started_at`、trade_intents の `id / mission_id / action`、signals の `id / status / kind / claimed_by_mission_id` だけで、payload / output / transcript 列と他の全表は落とす。test は抽出スクリプトの列 allowlist と snapshot schema をこの一覧へ固定する。`[2026-10-16T00:00Z,2026-10-31T00:00Z)` では、trade_intents を missions に結び `missions.started_at` で切った intent が hold 970 / open 3 / 計 973、trade mission は 1,033 本、`signals.claimed_by_mission_id` 基準の signal-linked 57 になることを再生成し、query、fixture SHA-256、母集団別件数を `tmp/econ-window-acceptance/history/summary.json` に出す。signal 57 は全件を移行前 cron 由来 consumed の 1 区分とし、新分類の system reject / LLM accept / LLM reject / abandoned は各 0 と照合する。944 は期待値に使わない。通常 suite では `bench` marker により除外してよいが、§7 の merge 前 job は `--fail-on-skip` でこれらの一致を必須にする。
15. settings の `econ` 省略時は 3 既定値、範囲外は起動前拒否、`signal_capacity_daily_max` は config 層で整数 1 以上だけを検査し、example と model は同期し、schema_version は 5 のまま。fresh / existing DB の新表・列・CHECK・index が同形で、2 回目 init は行を変えない。signals rebuild 後の `sqlite_master` / FK metadata を検査し、子表 `plugin_deploy_op_signals` / `signal_decisions` / `signal_shadow` の参照先がいずれも `signals` で、`signals_v1` や新表の一時名を指さないことを確かめる。子表 3 本の FK 違反では rebuild が失敗し、無関係な表の既存違反では warning を出して完了する。
16. agent 面 canary は shadow pnl / 勝率 / 件数、strategy 別率、床未満 n、親 report 固有値を埋め、trade prompt / summary / reflection / RAG / improve / ask / activity を全探索して 0 件。status / web は同じ DTO、壊れた集計は該当節だけ unavailable で取引を止めない。
17. **起動条件、claim 期限、decision 期限。** 注入時計で次を通す。
    - flat book: 建玉・注文 0 件で eligible signal 1 件 → 次の idle tick に専用 mission が claim する。
    - cron 連続: 判断足ごとの cron が毎回 dispatch を長く占有する → signal は f までの idle tick で claim されるか、f + T 以内に abandoned になる。ready 中かつ tick 所要 < T なら、どちらでもない pending は f + T を越えて残らない。
    - outage: data state が ready でない間は pending が claim されず f + T を越えて残り、ready に戻った最初の maintenance で abandoned になる。
    - 長い reflection: trade 後の reflection 3 本で dispatch が f を越える → 専用 mission は起動されず abandoned、鮮度切れの claim は 0。
    - ready 中のどの条件でも claim 時刻 ≤ f、decision を書いた commit-core の時刻 ≤ f、abandoned 時刻 ≤ f + T、`skipped` の専用 mission 0。f − 60 秒に claim して runner が 127 秒かかると、decision 0・shadow 0・`freshness_expired` で abandoned になる。異常終了は lease L で回収されて requeue し、f 後の再 claim は 0。`Cd + T ≥ 判断足幅` の設定で cron が毎回 dispatch を使い切ると全 signal が abandoned になることも確かめる。
    - rate: 帯内で `system_filtered` に終わった専用 mission は最短間隔と日次上限に数えず、直後の eligible signal は R を待たずに起動する。LLM を呼んだ mission と `skipped` は数える。
    - 静的式が true の example 設定では `worst_case_guarantee_unavailable=true` だが、それだけで activity は増えない。承認済み strategy が 0 本なら静的 status は `n/a`、warning は inactive。actor=`llm` の final decision が 100 件以上なら直接観測した `eligible→claim` / `claim→decision commit` の p95、100 件未満なら §3.2-7 の時間隣接で組んだ trade dispatch p95 + R + Cw を使い、system decision は標本に入れない。p95 の和が最小 freshness 余地以上、または `abandoned_at` が直近 14 運用日に入る freshness 系 abandoned が 1 件以上なら warning が active になる。warning は同じ設定 fingerprint・運用日で 1 回だけで、再起動を挟んでも増えず、fingerprint または運用日が変われば 1 回増える。final decision と freshness 系 abandoned の各 commit 後に signal id / 終端種別を評価境界へ渡し、core lock 外で評価が走ること、maintenance 終端も同じ境界を通ることを確かめる。status は標本数、2 latency、freshness 系件数、直接観測 / 代用、状態、直近時刻を表示する。
18. **eligibility / migration / cleanup の共有。** `kind="signal"` と `contract_version` が NULL の行だけが pending のとき、起動判定は false、claim 0、`skipped` 0、rate 消費 0。eligible と混在すると eligible だけを claim する。stale 後は eligible が `freshness_expired`、除外対象が `ineligible_cleanup` で abandoned になり、後者は decision / abandoned 集計から外れる。起動・claim・maintenance・集計の分類 query の branch を片方だけ変える変異で test が落ちる。
19. **旧 payload の migration。** 旧形の fixture（正規化可の pending、必須項目欠落の pending、claimed、`kind="signal"`）が、正規化 / abandoned / 正規化 / 不変になる。正規化可の旧行は、同じ strategy 結果を live producer に通した保存 payload と migration 後 payload が byte 単位で一致する。`normalization_failed` は `abandoned_at` を持つ。2 回目の初期化は行を変えない。
20. **保持と容量。** 通常 suite と実ディスク測定を分ける。
    - **通常 suite:** D 未満の set は削除されない。D を過ぎた set は set 単位で行だけ消え、set 行・shadow 終端・reaction 値・`(event_id,currency)` 対応は残り、集計の n が変わらない。LLM rate を変えても C は変わらず、承認 strategy / pair / timeframe または `econ.signal_capacity_daily_max` を変えたときだけ S と C が変わる。起動時に上書き値 < `S_producer` なら起動拒否。実行中の inventory 変更で下回れば S=`S_producer`、status=`capacity_override_below_producer` となる。importance_min 3 / 2 / 1 で E=54 / 110 / 463 に切り替わる。15m strategy 1 本 × 1 pair の固定例では S=96、C=64,693,085 行になる。content hash 更新 1 回の追加は最大 `k × pair 数` 本で、警告比率へ反映される。
    - **実ディスク（merge 前必須、`bench` profile）:** §5-12 の測定完了と `sync` の後に、各 run の fresh DDL を commit → `wal_checkpoint(TRUNCATE)` → slice 0 行の DB + WAL を測り、その最大値を B0 とする。その後、固定例 C に対する `Ci = unique(ceil(C/1000), ceil(C/100), ceil(C/10))` を signal / event 比で、240 行以下の transaction に分けて投入し、最後の COMMIT 直後・明示 checkpoint 前の DB / WAL / 合計 bytes を測る。各 Ci を fresh DB で 3 回ずつ測り、`β = max ceil((size(Ci)−B0)/Ci)`、全点の `B0 + Ci×β ≥ size(Ci)`、C への外挿を検査する。job 全体 4 時間・同時占有 4 GiB の上限も検査する。checkpoint 状態、commit 数、B0、各 Ci の 3 測定、β、上包絡・外挿判定、所要時間、peak bytes を `tmp/econ-window-acceptance/disk/capacity.json` に残す。行数または slice 関連 bytes の 80% / 100% 警告、S の導出 / 上書き / 実効値、B0 / β、DB bytes、WAL bytes、slice 関連 bytes、filesystem 空き bytes が status と親 report に出る。
21. **方針既定文。** fixture の全文が方針文の描画規則で 1 行（4000 字以内）になり、「方向と自分のテクニカル判断の一致」を求める語を含まない。既定文を入れても入れなくても、専用採否の schema・parser・orders 照合の判定は変わらない。
22. **abandoned の理由と観測。** §3.3 の 6 経路を 1 件ずつ通し、`freshness_expired`、`requeued_expired`、`retry_exhausted`、`source_inactive`、`deploy_op`、`deploy_linked` が付くこと、deploy op の approve / revoke の transaction が CHECK で落ちないこと、deploy に結ばれ上限も超えた行が `deploy_linked` になることを確かめる。migration の `normalization_failed`、`ineligible_cleanup` も各 1 件作る。新しい abandoned は reason と `abandoned_at` の両方を持つ。abandoned で reason が NULL、abandoned 以外で reason または `abandoned_at` が非 NULL、reason が `legacy_unknown` 以外の abandoned で `abandoned_at` が NULL の行は DB が拒否する。既存 `legacy_unknown` だけは `abandoned_at=NULL` で copy できる。status / web / 親 report の理由別件数、cleanup と `legacy_unknown` の分離、`abandoned_at` 基準の期間合計と直近運用日を照合する。accept / reject の分母は変わらず、warning の恒常表示も agent 面 canary へ漏れない。
23. **連結成分。** 重なる 3 帯（A と B、B と C が重なり、A と C は重ならない）が 1 成分になり、ID は最小 event id。抑止中の order に新しい event が合流しても、supersede で最小 id の event が消えても、ID は最初の抑止時の値のまま通知 1 回。成分が切れて別の帯で抑止されれば 1 回増える。
24. **次 event の 7 日検索。** 次の対象 event が `now + 7 日` ちょうどなら返し、1µs 後なら返さない。対象外通貨・importance 未満・superseded は次 event にならない。
25. **裁量 OPEN の帯内拒否。** cron mission の OPEN が帯内なら、lock 内の判定結果が gate の入力に入り、intent は `reject_category=econ_window` で記録され、注文は 0、`open_rejected_econ_window` が 1 回。帯外の OPEN と、帯内の close / cancel / hold は従来どおり通る。gate 自体は DB を読まない。
26. **agent 表現の絞り込み。** CAD の importance 3、USD の importance 2、USD の importance 3 を保存し USDJPY で描画すると、state summary と calendar tool の両方に USD の importance 3 だけが出て、他 2 件の name は文字列としても出ない。
27. **live と backtest の正規化一致。** 同じ strategy 結果（market / limit、TP あり / なし）を live producer と backtest 変換に通すと、horizon・limit expiry・SL・TP・entry が一致し、live 側だけに `contract_version=executable_v1` が付く。
28. **t0 の切り上げ。** decision が分境界ちょうどなら t0 はその時刻、1µs 後なら次の分境界。
29. **読戻し不一致。** staging 後に 1 行を書き換えて読戻しを不一致にすると、その set は complete にならず、slice 行が消え、event set は spread を保ったまま `awaiting` に戻り、次 cycle で正しい値に収束する。
30. **起動時の補完。** pair の追加、`importance_min` の引き下げ、保持範囲内の旧 event がある状態で worker を起動すると、不足 set だけが作られ、既存 set は増えない。対象外になった未完 set は `cancelled` になる。cancelled set の staging 行は再起動を待たず毎 cycle 掃除され、行が残る pass では再有効化を見送って `cancelled` のまま、0 行になった次の pass で、設定由来なら同じ set が `completed_at=NULL` / `generation=NULL` / `purged_at=NULL` の `awaiting` に戻り、取得済み spread は同じ値のまま残る。superseded 由来は `cancelled` のまま戻らない。
31. **反応値は 1 回だけ。** 反応値を書いた後に backfill や再算定を走らせても値と `computed_at` は変わらない。settings pairs を並べ替えても既存の `(event_id,currency)` 対応と集計値は変わらない。
32. **既存 abandoned の移行。** 理由の無い abandoned を複数（kind・requeue 歴の違うもの）持つ旧 DB を初期化すると、全件が `legacy_unknown` になり、行数と他の列は一致し、2 回目の初期化は行を変えない。
33. **trade event sink の origin。** 専用採否 mission の accept から生じた intent・注文の event は signal origin、cron の OPEN は cron origin として出る。
34. **親専有 report の生成時点。** 市場の当日 close 後に 1 回だけ生成し、同じ日に 2 回目は作らない。起動時に当日分が無ければ 1 回 catch-up し、有れば作らない。
35. **improve allowlist の非交差。** improve の tool allowlist と禁止集合（signal・decision・shadow・reaction 系）の積は空で、禁止 tool を allowlist に 1 つ加える変異で import 時か test が落ちる。
36. **OHLC の入力制約。** observed / no-tick flat の OHLC は有限正数で `low ≤ open,close ≤ high` を満たす。NaN、無限、0 以下、high / low 逆転を入れると set は complete にならず unresolvable になる。
37. **decision と intent の対応。** accept decision の `trade_intent_id` は同じ signal 採否 transaction で作った intent だけを指す。別 mission / 別 signal の intent、存在しない intent、reject の非 NULL は store または DB test が拒否する。
38. **抑止足は再処理しない。** limit fill を回避帯で抑止した足は processed-bar 状態が進み、同じ足を次 tick や再起動後に再入力しても fill 判定・activity・期限を再実行しない。新しい足だけが次の fill 候補になる。

## 6. 残余

- actual は供給元に無く 1027 件すべて NULL。本束では取得・surprise 計算をしない。
- backtest 側に回避帯を入れない。数値補完は replay 経路と live producer で同じ規則にする。
- hold 944 はチケット / メモリの途中値。固定再現値は §1 / §5 の半開区間に対する hold 970 / open 3 / signal-linked 57。
- improve 非交差 assert は現物に無く、本束で実装し §5-35 で test する。
- trade prompt の「1 時間毎」は 15m 判断足と不一致。本束で直す。
- calendar の fail-open は未知 event を止められない。freshness は安全保証ではなく観測状態である。
- 1m の障害欠損は補間しない。no-tick flat は障害補間ではなく tick 不在の明示的規則である。
- accept / reject shadow の market entry は次の 1m open + half spread、live 注文は gate 時点 quote であり価格は一致しない。shadow は decision 時の quote spread、live paper fill は assumed spread を使うので物差しも違う。同じ shadow 同士の比較を選別指標とし、実注文との差を因果効果と解釈しない。
- cron / signal の順序は案 (a)「cron 優先維持 + 警告」に固定する。2026-10-16 以降の trade mission 1,033 件は平均 127 秒、最大 310 秒、600 秒超 0 件で、Cd=1630 秒は実態ではなく上限だった。15m・k=2 の claim 余地 900 秒は通常満たされ、close / cancel の資金保護を後退させない。一方で混雑時の abandoned 偏りは残るため、理由別件数と warning の恒常表示を必須とし、鮮度内採否そのものは保証しない。
- event の spread は発表直前 1 回の quote で、発表時の拡大は捉えない。記録できなかった event は assumed spread になる。
- 保持期限を過ぎた set は slice 行が消え、その owner の再計算はできない。値 snapshot と shadow 終端は残る。容量は producer が保証する `(name, content_hash)` × pair × bar の日次上限と importance_min 別の実測上側値 E（3 / 2 / 1 → 54 / 110 / 463 件 / 月）に依存し、content hash 更新時の鮮度窓再評価分を含む超過は警告で知らせる。LLM 呼出枠は容量根拠に使わない。
- 方針文は利用者データで、配備後に利用者が文面を変えられる。コードの契約は方針文に依存せず、test は既定文の fixture だけを使う。
- 複数 event の価格反応は各 name 束へ入る。`unique_ts_n` を示すが原因分離はしない。
- 床 20 / p75 は開示・記述規則であり、有意性・再現性・予測力を保証しない。
- example 設定では最悪値式が成り立つため `worst_case_guarantee_unavailable=true` になるが、これは稼働悪化 warning ではない。同じ足に複数の signal が出ると、最短間隔 R=600 秒の直列で 2 本目以降は f に間に合いにくく、claim 余地 900 秒では 3 本目以降がほぼ必ず abandoned になる。実測 p95 と freshness 系 abandoned で `signal_wait_budget_warning` を判定する。
- decision の期限も f なので、f の近くで claim された signal は runner の所要（平均 127 秒）で abandoned になり得る。
- data state が ready でない間は pending が f + T を越えて残る。claim されないので安全側で、復帰後に abandoned になる。
- フラグを持たない既存 writer（improve 等）が書込みを保持する間、scheduler は現行どおり最大 5 秒待ち得る。本束はこの待ちを増やさないが、減らしもしない。
- canonical pair は反応値の算定時点で固定し、遡って変えない。pair の並びを変えると、変更の前後で同じ通貨の canonical pair が違う event が混ざる。
- cron mission は signal 一覧を見なくなる。pending signal の採否は専用 mission だけが扱う。

## 7. 変更一覧と task 分割

束 1 を先に完了し、束 2 は束 1 の slice / 1m 規則と worker の後処理差込口に依存する。行数見積は実装 + test。同じファイルを 2 task が同時に持たず、表の順に exclusive ownership を引き渡す。担当は `codex terra` または `sonnet` に固定する。

### 束 1: 回避帯・採否契約・shadow

| task | 内容 | 見積 | 担当 | 所有 | 引渡し（入力 → 出力） | 依存 |
|---|---|---:|---|---|---|---|
| T1a schema / migration 枠 + 既存 abandoned 書込み | 任意 settings、fresh DDL、新 7 表、`trade_intents` / signals v3 rebuild、`abandoned_at`、既存 abandoned の `legacy_unknown`、既存 `data_migrations` 用の name 引数 guard helper と `init_db` の呼出し順の枠、`signals.py` / `missions.py` の既存 abandoned 書込みへ reason と時刻を付ける最小変更（`abandon_claimed()` の引数化と `trade_loop.py` の呼出側 3 箇所を含む） | 490 | codex terra | `config.py`、settings example、v5 example fixture、`store/db.py`、`store/signals.py`、`store/missions.py`、`loops/trade_loop.py`（abandon_claimed の呼出 3 行のみ、T2b-1 へ引き渡す）、schema test | v5 DB / settings → 冪等な fresh / existing DB 同形 schema、rebuild 後も子表 3 本が signals を参照する DB、CHECK 下で既存 suite が通る lifecycle、旧 payload 未移行 fixture。`db.py` / `signals.py` / `missions.py` と schema test を T1b、DDL / store test を T1c へ渡す | — |
| T1b signal lifecycle / producer / payload migration | eligibility / migration / cleanup 分類、tx 合成、abandoned 集計、LLM rate から `system_filtered` を除外、live payload 正規化、同じ保存規則による旧 payload migration / `normalization_failed`、producer の `(name, content_hash)` / bar 上限。`db.py` には v3 rebuild 直後の lazy-import 呼出し 1 行だけを足し、migration key と記録行を持つ | 390 | codex terra | T1a から受けた `store/db.py` / `store/signals.py` / `store/missions.py` / schema test、`plugin/signal_producer.py`、`strategy_adapter.py` | T1a の schema / guard helper / 呼出し位置 / 既存書込み → 分類 query、tx 合成可能な consume / mission 終端、live と byte 一致する migration 済み signal、`S_producer` の入力集合。T1c / T2a-2 へ渡す | T1a |
| T1c decision / shadow / slice store | decision / shadow の tx、set state、cancelled 再有効化、staging / 完備 CAS、世代台帳、purge、S / C / B0 / β の容量算定と起動時の S 比較 | 430 | codex terra | `store/signal_decisions.py`、`store/signal_shadow.py`、`store/ohlcv_slices.py` と T1b から受けた store test | T1a DDL + T1b 分類 / migration → decision / shadow / slice の store 操作一式と S / `S_producer` 比較。T2a-1 / T2b-1 / T4b へ渡す | T1b |
| T2a-1 window / calendar / refresh | 純粋 window、候補 query / freshness、supersede、refresh 後 pass、awaiting / cancelled / 設定由来の再有効化 | 350 | sonnet | `core/econ_window.py`、`store/econ_events.py`、`store/fetch_attempts.py`、`datafeed/econ_calendar.py` と window test | event / pair / 時刻・足区間 → window view、次 event、freshness、refresh 後 set 差分。T2a-2 / T2b-2 / T4b へ渡す | T1c |
| T2a-2 scheduler / service admission | cron 優先、signal 起動条件、足区間 limit 抑止、静的保証 status、実測 p95 warning、S の実行時再計算、writer admission、tick / next tick 観測、親 report の市場 close 後 1 回 + 起動時 catch-up hook API | 370 | sonnet | `core/scheduler.py`、`service.py`、wait-budget test | T1b signal 集合 + T2a-1 window → scheduler 起動判定、warning snapshot、signal id / 終端種別を受け commit 後に core lock 外で呼ぶ評価境界、writer 要求フラグ、report hook。評価境界を T2b-2、hook API を T5、`service.py` の ownership を T4c へ渡す | T2a-1 |
| T3 採否 contract | Claude schema、Python parser、orders 照合、summary の pair view、方針既定文 fixture、`realbackend` test | 400 | codex terra | `loops/signal_decision_contract.py`、`loops/summary.py`、policy fixture、contract test | LLM JSON + orders → accept / reject+code / invalid detail。T2b-1 へ contract を渡す | T2a-2 |
| T2b-1 mission 分離 / claim / tx flow | cron claim 廃止、cron / ask / 専用 mission の tool 集合分離、専用 mission の claim / 排他 / 既存分岐、tx 終端 | 460 | codex terra | `loops/trade_loop.py`、`loops/mission_finalize.py` の claim〜transaction 部分と flow test | T1c store + T3 contract → signal 1 件の未確定 flow。両ファイルの ownership と commit-core 差込点を T2b-2 へ渡す | T3 |
| T2b-2 commit-core / gate / prompt | f 判定と理由・`abandoned_at` 付き abandoned、quote spread、commit 前 window 再判定、`GateContext`、activity、gate streak 除外、commit 後 warning 評価、prompt | 390 | codex terra | T2b-1 から受けた 2 ファイル、T2a-2 の評価境界、`core/risk_gate.py`、trade prompt と flow test | 未確定 flow + T2a-1 window → decision 1 行 + shadow 1 行、または requeue / abandoned。commit 成功後に signal id / 終端種別を評価境界へ渡し、`trade_loop.py` の ownership を束2 T3 へ渡す | T2b-1 |
| T4a 1m 正規化 / fill 共用 | cache / history、outage / no-tick、保持範囲、source 選択、live / shadow の limit / exit 保守則共用 | 320 | codex terra | `datafeed/econ_bars.py`、`core/paper_fills.py` と純粋 fixture | 生 1m 足 + owner 範囲 → 正規化済み足 / unresolvable と fill 判定。T4b へ渡す | T2b-2 |
| T4b worker state / CAS / recovery | worker cycle、shadow 終端、event spread、読戻し、完備 CAS、世代回収、cancelled staging の毎 cycle 掃除、後処理差込口 | 500 | sonnet | `core/econ_slice_worker.py` と T4a から受けた worker state test | T1c store + T2a-1 set + T4a 1m 規則 → complete / unrecoverable set、掃除後の再有効化、shadow 終端。T4c へ worker lifecycle を渡す | T4a |
| T4c service / 実ディスク / purge・容量受入 | worker 起動停止 / catch-up / 接続、起動時補完、最悪 interleaving、purge、B0 / β 上包絡・外挿、4 時間 / 4 GiB 上限、実ディスク成果物 | 380 | sonnet | T2a-2 から受けた `service.py`、T4b から受けた shadow worker integration / `bench` test | T4b worker + T1c purge / 容量 store → service wiring と §5-12 / 20 の必須 job。T2a-2 の report hook を保ち、`service.py` と集計可能な store を T5 へ渡す | T4b |
| T5 人間面 / reader / 遮断 | status、親 report、web、improve / event sink / mission reader、abandoned、静的 status / 実測 warning、容量、列 allowlist の履歴 snapshot 抽出 / 再生成、非交差 assert、canary。T2a-2 の hook API に report 生成関数を差す | 820 | sonnet | T4c から受けた `service.py`、§4 の他の T5 所有ファイル、履歴抽出スクリプト / audit fixture / test、isolation test | store 集計 + close / catch-up hook → 1 日 1 回の DTO / report と 970 / 3 / 57 / 1,033 成果物。report / status 群の ownership を束2 T4 へ渡す | T4c |

束 1 は表の順に直列で渡す。小 task に分けても migration、signal lifecycle、worker、service を同じ受入単位へ戻さない。

### 束 2: 反応統計

| task | 内容 | 見積 | 担当 | 所有 | 引渡し（入力 → 出力） | 依存 |
|---|---|---:|---|---|---|---|
| 束2 T1 定義 fixture | 基準 close、3 窓、range、終値、spread、source、重複、欠損、束、床 | 300 | codex terra | reaction fixture | 式 → 固定期待値 | 束1 T4c |
| 束2 T2 集計器 / snapshot | complete event set の 3 窓値、`(event_id,currency)`、2 束、median / p75、n / unique_ts_n | 500 | codex terra | reaction store / analytics と test | complete set + fixture → 値 snapshot / 集計値 | 束2 T1 |
| 束2 T3 trade tool | settings pair、床 projection、cron trade だけへ登録 | 300 | sonnet | market tool / registry、T2b-2 から受けた `trade_loop.py` の tool 一覧 | 集計値 → cron tool 応答。ask / improve / 専用採否には非登録 | 束2 T2 |
| 束2 T4 人間面 / 再現 | report / status / web の reaction 節、canary、22/38・9/12 再現 | 400 | sonnet | T5 から受けた report / status / web ファイル | 集計値 → report / DTO | 束2 T3 |

束 2 も表の順に直列で渡す。shadow 状態機械を import せず、束 1 の slice / 1m 契約と後処理差込口だけを使う。

### 配備手順

1. **merge 前必須 job を 3 本とも成功させる。** skip / xfail / 成果物欠落は不合格。
   - Claude 実往復: `uv run pytest tests/loops/test_signal_decision_flow.py -m realbackend -p no:cacheprovider -rs --fail-on-skip --basetemp=tmp/econ-window-acceptance/claude/basetemp`
   - 履歴 DB 再生成: `uv run pytest tests/audit/test_econ_history_regeneration.py -m bench -p no:cacheprovider -rs --fail-on-skip --basetemp=tmp/econ-window-acceptance/history/basetemp`
   - 実ディスク writer / 容量: `uv run pytest tests/core/test_econ_shadow_worker.py -m bench -p no:cacheprovider -rs --fail-on-skip --basetemp=tmp/econ-window-acceptance/disk/basetemp`
2. 手順 1 の 3 job はそれぞれ、§5-4 の 2 応答と最大 3 往復、§5-14 の 970 / 3 / 57 / 1,033 と列 allowlist、§5-12 / 20 の device・latency・`sync` 後の B0 / β 上包絡・外挿・4 時間 / 4 GiB 上限を `tmp/econ-window-acceptance/` の対応成果物で照合する。3 本の成功を merge 条件とし、通常 suite で marker により除外された結果を代用しない。
3. merge 後は停止中に配備し、初回起動で T1a の schema / rebuild を通した後、T1b の live 保存規則と同じ旧 payload migration を通す。
4. 利用者が §8 J3 で承認した既定文を `afx ctl policy add` で登録し、方針ファイルの旧 1 行目（managed block の外の行）を手で消す。`afx ctl policy show` で旧行が unmanaged に残っていないことを確かめる。後日の文面変更も §8 J3 の 3 条件を保つ。

## 8. 裁定と利用者承認

### 裁定済（2026-11-01、ユーザー）

1. **役割分担は中間形。** LLM は signal の最終確認をするが、方向不一致は拒否理由にできず、固定 code だけを使う。strategy・理由別の拒否率を人間へ見せる。
2. **帯内 signal は決定論で捨てる。** `econ_window` として LLM に渡さず、帯明け後に再利用しない。次の足で strategy が再発火した新 signal だけを扱う。
3. **backtest との乖離は目的どおり。** 過去 calendar は取得せず、live 選別の良否は signal shadow で測る。
4. **shadow を本束に含める。** system / LLM の拒否理由を残し、accept も同じ規則で追跡し、decision・strategy 別に集計する。
5. **反応統計は人間 + LLM tool。** 通貨 × 重要度と通貨 × 種別、15m / 1h / 4h、床 20 未満は LLM へ数値を出さない。
6. **actual は対象外。** 供給元が持たないため DB 列だけ残し、本束では取得しない。

### 利用者承認済（2026-11-01、推奨どおり）

J1〜J3 は 2026-11-01 に利用者が推奨どおり承認した。本書の該当箇所はその前提で確定している。

**J1. reject code を `news_shock` / `opposite_position` の 2 語で始めるか。** 推奨: 2 語で始め、第 3 語を予約しない。理由: 裁定 1 は却下理由を固定語に限る。語を増やすほど LLM が「方向不一致」を別の語で言い換える余地が増え、却下率の意味が薄まる。`opposite_position` は orders 照合で決定論的に検証でき、`news_shock` は reject shadow の成績で事後に良否を測れる。足りない理由が観測（reject shadow、abandoned の理由別件数）で見えたら spec 改訂で足す。ブレスト骨子 B の「他に要るか」への答えにあたる。

**J2. 回避帯の既定を importance 3、発表前 30 分、発表後 15 分にするか。** 推奨: この既定で始め、自動調整しない。理由: 発表前 30 分は発表直前の spread 拡大と様子見を覆い、発表後 15 分は判断足 15m の 1 本分で初動を避ける。importance 3 は全通貨で約 53 件 / 月（§1）で、USDJPY の取引機会を大きくは削らない。束 2 の反応統計（15m / 1h / 4h の値幅）が貯まったら、それを根拠に利用者が設定で変える。

**J3. signal 採否の方針既定文を次の全文で承認するか、文面を変えるか。** 推奨: 次の全文で承認する。理由: 裁定 1 をそのまま文にしたもので、現行の方針 1 行目（signal の方向と自分のテクニカル判断の一致を reasoning に書く）は裁定 1 と矛盾する。システム契約は schema・parser・orders 照合で強制し方針文に依存しないので、文面を変えても安全性は変わらない。変える場合も「accept が既定」「reject は固定 code の場合だけ」「LLM 自身の方向不一致は理由にしない」の 3 条件を保つ。

> strategy signal の採否 mission では採用 (accept) を既定とする。却下 (reject) は news_shock (重大ニュースが signal の前提を壊した) か opposite_position (同じ pair に逆方向の建玉・約定待ち注文がある) に当たるときだけにし、自分のテクニカル判断と方向が合わないことを却下の理由にしない。

承認された文を §4 の既定文 fixture に置き、配備手順 2 で現行 1 行目と置き換える。

### 技術上の決定

J1〜J3 以外の境界、欠損、CAS、calendar fail-open、limit 非取消、agent 隔離、床未満 projection、待機上限、slice の lock 規約、保持容量は、ユーザー裁定 1〜6 と設計レビューの裁定に基づき本書の規則で決める。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-01 | v0.1 C0 | 初版。回避帯、signal 採否、reject shadow、反応統計、agent 面遮断 | ユーザー裁定 1〜6 と sol 前提確認を反映 | — |
| 2026-11-01 | v0.2 C0 | cron / signal mission 分離、accept shadow、slice、migration / reader / worker / test 契約を具体化 | 設計レビュー 1 周目 Critical 1 / Important 15 / Minor 11 の全件反映 | — |
| 2026-11-01 | v0.3 C0 | signal 起動条件と eligible 判定の共通化、待機上限の契約、旧 payload migration、slice の staging + 完備 CAS と lock 規約・測れる受入、supersede の条件、event slice の worker 所有、spread の出所と反応値の式、容量算定と値 snapshot、task の見積・所有・引渡し、方針既定文の固定 | 設計レビュー 2 周目 Critical 2 / Important 7 / Minor 2 の全件反映 | — |
| 2026-11-01 | v0.4 C0 | DB writer 優先制御、eligibility / cleanup 分離、発表前 spread、反応集計の一意性、実測容量、warning / abandoned 観測、cron 優先の実測根拠を確定 | 設計レビュー 3 周目 Critical 1 / Important 3 / Minor 3 を全件反映し、2 周目 C-1 / C-2 / I-2 / M-2 を閉鎖 | — |
| 2026-11-01 | v0.5 C0 | busy timeout 5000ms 維持と実測受入（フラグ外 writer・signal 接続を含む）、signals rebuild と 6 経路の abandon 理由、decision 期限 = f、limit 候補を足区間基準に、event set の段階回収と永続世代、refresh 後 pass と `cancelled`、50ms は commit 前・実ディスク試験、cron tool から signal 一覧を外す、Minor 18 件、pin 無し 13 規則を §5-23〜35 に、§8 に J1〜J3 を利用者承認待ちで復活 | 設計レビュー 4 周目 Critical 1 / Important 7 / Minor 18 / pin 無し 13 / §8 欠落を全件反映 | — |
| 2026-11-01 | v0.6 C0 | LLM rate と全 signal 容量の分離、静的保証 status と実測 warning、`B0 + C×β`、merge 前必須 3 job、cancelled 再有効化、重複 fixture 共用、T1a〜T4c の task 分割 | 設計レビュー 5 周目 Critical 1 / Important 5 / Minor 1 / §5 重複を全件反映 | — |
| 2026-11-01 | v0.7 C0 | payload migration を T1b へ移管、`abandoned_at`、dispatch 代用式、履歴 fixture 列 allowlist、warning 評価引渡し、容量上書きの実行時挙動、容量 bench の外挿・上限、Minor 10 件、§5 pin 3 件 | 設計レビュー 6 周目 Critical 1 / Important 7 / Minor 10 / pin 補強 3 を全件反映 | — |
| 2026-11-01 | v0.8 C0 | signals v3 rebuild と既存 abandoned 書込みの所有、参照元 FK を保つ移行前例、shadow state 正規形、payload migration / report hook の引渡し、容量算定とブリーフ注記を確定 | 設計レビュー 7 周目 Critical 2 / Important 5 / Minor 4 を全件反映 | — |
| 2026-11-01 | v0.8.1 C0 | `abandon_claimed()` に UTC 時刻と reason を引数で渡し、`trade_loop.py` の呼出側 3 箇所を T1a の所有に含めて T2b-1 へ引き渡す | 設計レビュー 8 周目 (閉鎖確認) の残り Critical 1 | — |
| 2026-11-01 | v0.9 C0 | J1〜J3 を利用者承認済（推奨どおり: reject code 2 語、帯 3 / 30 / 15、方針既定文）に | ユーザー承認、実装着手 | — |
| 2026-11-01 | v0.9.1 C0 | v3 rebuild の FK 検査を子表 3 本に訂正（他表の違反は warning）、CHECK 正規化の規則、`signal_shadow` の複合 FK、slice set の値域 CHECK を足す | T1a /code-review 2 周目の裁定 | — |
| 2026-11-01 | v0.9.2 C0 | §3.2 の Claude 向け schema を最上位 object + if/then/else の 1 本にし、§5-4 に最上位 anyOf / allOf / oneOf が 400 になる実測を足す | claude --json-schema の実測で最上位 anyOf/allOf/oneOf は 400、if/then/else は受理 | — |
