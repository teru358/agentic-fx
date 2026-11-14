# kill switch 解除後の HWM 付け直し設計 C2.1

- 対象: `docs/tickets/open/killswitch-reset-hwm-rebase.md`
- コード基準: `main` / `558cb9fd`（2026-11-14 に HEAD を再確認。`afa7819f..HEAD -- src tests afx-ops` の差分は空）
- 状態: **C2.1。2026-11-14 利用者承認、実装待ち (opsalert の T1b の後)**
- 改訂日: 2026-11-14 JST
- 入力: 設計 C1 と opus 2 周目の記録 (非公開、Critical 1 / Important 4 / Minor 12、食い違い 12)、terra の C2 閉鎖確認
- 関連設計の正本: `docs/superpowers/specs/2026-11-14-ops-alert-delivery-design.md`（opsalert C4、2026-11-14 利用者承認、実装中。branch opsalert にあり、merge で main に入る）。旧 C2/C3 を規範参照にしない
- 作業範囲: この C2 の作成のみ。コード、既存 docs、チケット、C1、レビュー原本は変更しない。金額・秘密情報を調査・転記しない

## 1. 動作目線: 誰が何をどう扱うか

### 1.1 目的と操作

ローカル approver が、確定したペーパー損失を受け入れ、現在 equity を今後の HWM の起点にする。新設する操作は次である。

```text
afx ctl --as approver killswitch rebase <世代>
```

rebase は HWM だけを下げ、ラッチ、generation、latched_at は変更しない。通常の `killswitch reset <世代>` は従来どおりラッチだけを解除し、HWM を変えない。ラッチ中なら rebase → reset の順で行う。既に reset 済みなら、現世代のまま rebase を行える。generation > 0 の既存世代では、再ラッチを起こすためだけの open intent は不要である。

rebase の前提は **PaperBroker、flat、新鮮な snapshot、DD ≥ 現在閾値、generation > 0**。ラッチ有無と同世代の過去の適用回数は問わない。成功直後は DD = 0 になり、再度必要になるには損失によって DD が閾値へ達する必要がある。含み益の消失を経た初回もあり得るが、flat 時点で損益は確定している。同世代でも、その後の取引による損失で条件を再び満たせば適用できる。

gen 0 は `generation_unavailable`（preview reason）として拒否する。正常な初期 state と、state 再作成で世代履歴を失った状態を本件では区別できず、既存の kill switch 世代への操作に限定するためである。gen 0・clear・flat・DD 超過では例外的に初回 open intent の DD 拒否によるラッチが必要になる。gen > 0 でも state 再作成を検知できる保証はなく、世代再利用の修復は対象外とする。

閉場中は不可とする。snapshot を新規採取する特別経路や、鮮度を broker balance だけで代用する経路は作らない。市場が開き、新鮮な MTM が記録されてから再実行する。他の gate（日次損失、総リスク、データ鮮度等）が解除される保証はない。

### 1.2 確定裁定: 累積に硬い上限を置かない

PaperBroker の rebase に、累積切下げ上限、HWM の保護下限、回数上限を置かない。protection anchor、上限率 config、anchor 再設定操作も作らない。ただし正の有限な equity/HWM という既存の口座健全性条件は守る。

理由は以下の三点である。

- 1 回の rebase に閾値分の実損が要る。flat と DD 条件を満たす必要があり、成功直後の連打で損失予算を増やすことはできない。
- 同じ本人が config で DD 閾値を上げられるので、硬い上限は守る相手に対する迂回不能な保護にはならない。
- 上限到達後は flat かつ DD ≥ 閾値から抜けられなくなる。flat の equity は取引なしでは回復せず、open は DD gate で拒否される。通常 reset で回復を待つ、という C0 の出口は成立しない。

これは連続した明示操作でも越えられない損失総額の保証を提供しない。代わりに ledger、累積表示、警告、毎回の確認で扱う。**実口座 broker を導入するチケットでは、rebase の硬い累積損失上限を必須要件とする。** その時点で上限到達後の正規の出口、入出金の正本、broker 側だけの exposure、通貨精度も設計・検証し、完了するまで実口座 rebase を有効にしてはならない。本仕様の paper 用警告をその代用にしてはならない。

### 1.3 preview と確認

CLI は mutation 前に専用 preview を読み、ローカル端末だけに次を表示する。本書に口座金額は記載しない。

- 世代、ラッチ状態、発動時刻、source snapshot の id / 時刻 / source
- 現在 HWM、balance、equity、付け直し後 HWM、現在 DD と閾値
- 今回の切下げ幅と率、過去の累積と回数、今回適用後の累積と回数
- 戻る追加損失余地、flat 確認結果、閉場中は不可であること
- 「rebase ではラッチを解除しない」「日次損失・総リスク等の gate は変わらない」

計算は全て server が行う。`T = drawdown_kill_pct` とすると、

```text
new_hwm = equity
cut = old_hwm - equity
cut_pct = 100 * cut / old_hwm
cumulative_cut_after = SUM(ledger.cut) + cut
cumulative_cut_pct_after = SUM(ledger.cut_pct) + cut_pct
count_after = COUNT(ledger) + 1
additional_headroom = equity * T / 100  # 適格条件 DD >= T の下で付け直し前の余地は 0
```

空 ledger の SUM は COALESCE で 0 とする。過去累積は同一読取 transaction の `SUM(cut)` / `SUM(cut_pct)` / `COUNT(*)` のみから作り、保存累積との照合はしない。preview の今回後累積はその SUM に今回分を加えた予測値であり、次回は全件 SUM を取り直す。浮動小数の丸めで予測値と後日の SUM が末尾で異なり得るが、これを拒否条件にしない。

累積幅は通貨額の単純和、累積率は**各回の直前 HWM に対する切下げ率の単純和（percentage points）**とする。累積率は当初資金に対する実損率でも複利 DD でもない。途中の利益・HWM 更新があるため両者は一致しない。reset、新 generation、再起動で集計を消さない。前回までの値、今回の増分、今回後の値を区別する。

技術判断で、`cumulative_cut_pct_after > 3 * T` のとき固定警告を付ける。ちょうど 3 倍は警告なし。三回の閾値相当の予算再付与を超える段階で反復を目立たせ、初回・二回目の復旧のたびに警告が出ることを避けるため、倍率は **3** に固定する。現在 T、比較対象、各回適用時の T を表示・記録し、閾値を変更すると警告判定も変わると明記する。これは運用上の注意喚起であり、拒否や実損総額の制限には使わない。

```text
警告: 累積切下げ率（各回率の合計）が現在の DD 閾値の3倍を超えます。
過去の損失を受け入れ、追加の損失余地を戻します。
```

確認直前の文には、今回と累積の切下げ幅・率、今回後の回数を必ず埋め込む。入力は `rebase <世代>` の完全一致。`y`、`--yes`、非 TTY、pipe、EOF、別文字列では POST を送らない。TTY は client の誤操作防止であり server の認証要件ではない。

成功時、latched なら「HWM を付け直した。新規停止は継続中」と既存 reset コマンドを案内する。clear なら「HWM を付け直した。現在の DD 条件は解消した」と表示する。他の gate の通過は約束しない。

### 1.4 入口と reset 通知の整合

preview / POST とも `local_guard`、approver、UDS 限定。既存の pidfd、同一 UID、service 子孫拒否、Bearer 認証を通す。autopilot 中は拒否。bot / web / notify / TCP、LLM / plugin / ask / improve からの配線は作らない。legacy shell `afx>` にも分岐を作らず、ShellCaller の認可素通りを利用させない。さらに preview / POST の service method の先頭で `caller is Principal.APPROVER` を検査し、それ以外は既存 `forbidden` で拒否する。route scope だけに依存しない。

server が保証するのは上の peer / token 境界である。適切な token と非子孫 peer を持つ人の直接 UDS 呼出しに TTY を強制する保証はない。既存 reset と同じ水準である。

opsalert C4 §4.7 の reset 成功後固定文を、それぞれの入口（shell / ctl）で一回、DD 条件判定で消さず表示する。

```text
DD が閾値以上のままなら、次の新規判断で kill switch が再びラッチします。
```

ctl はこの固定文の後に、毎回「HWM を付け直すには `afx ctl --as approver killswitch rebase <世代>`」という固定案内を出す（世代は reset 結果を使う）。client が DD を推測して出し分けない。shell は C4 の固定警告だけとし、rebase の入口を増やさない。reset に自動 rebase、`--rebase`、DD 数値の API/event field を追加しない。rebase 自体は latch/release event を出さず、通知設計の sweep の意味を変えない。C4 §4.3〜4.4 の kill switch event emitter は単一の private unlocked sweep のままとする。shell reset/reconcile は直接 StateStore mutation 後に公開 wrapper を呼ぶ現契約を維持する。rebase は state を変えないので独自の前後 sweep を追加せず、lane 保持中に公開 wrapper を呼ばない。C4 の instance/thread 所有検査・再入即時拒否にも従う。

## 2. 仕組み

### 2.1 コードで確認した前提

| 根拠（現 HEAD） | 事実と設計への制約 |
|---|---|
| `src/agentic_fx/core/accounting.py:12-41` | 通常 HWM は `max(prev_hwm + cashflow, equity)`。同 timestamp は許容、時刻逆行は拒否。通常関数で HWM は下げられない |
| `src/agentic_fx/store/snapshots.py:20-38` | latest は `ts DESC, id DESC`。日次用 first/last は ts のみ。時刻をずらして順序を作らない |
| `src/agentic_fx/core/accounting.py:50-92` | 日次基準と現在口座の鮮度評価。現在口座は 10 分超で欠損扱い |
| `src/agentic_fx/core/scheduler.py:245-271,990-1020` | 閉場中 MTM なし、source 既定は paper、cashflow は渡さず 0。含み損益は OPEN だけを評価するので、それだけを flat の根拠にできない |
| `src/agentic_fx/core/transitions.py:99-100` | `TERMINAL = {CLOSED, CANCELLED, REJECTED, EXPIRED, INVALIDATED}` が状態集合の正本 |
| `src/agentic_fx/core/paper_broker.py:42-47` | balance は starting_balance と orders の確定損益・費用の和。`equity()` は balance を二つ返す |
| `src/agentic_fx/service.py:1473,1907,2155-2195` | 現行 broker は PaperBroker。status は既に balance/equity を返すが、それは含み損益込み MTM と同じ意味ではない |
| `src/agentic_fx/config.py:65,806-815` | DD 閾値は `gt=0, le=100`。高い値は WARN で許される。無効化キーはないが、本人は保護を緩められる |
| `src/agentic_fx/core/risk_gate.py:192-198` | latched と DD を別々に判定する。reset のみでは現在 DD の拒否が残る |
| `src/agentic_fx/core/executor.py:637-670,848-927` | DD 拒否でラッチ。commit-core では口座 context を再読込する |
| `src/agentic_fx/store/state.py:245-253,271-273,320-343,389-442` | load は marker を latched に投影。reset は世代/発動時刻を保持。state 欠損なら gen 0。reconcile の confirm_latched も世代を進める |
| `src/agentic_fx/ops/service.py:702,754-769,807-902,1620-1649` | ops lane は独立 RLock / 接続。accepted → 副作用 → terminal、投影は best effort。現行 reset は state だけ変更 |
| `src/agentic_fx/service.py:1584,2131,2226-2232,2463-2502,2918` | core_lock は App 側にあり OpsService には未注入。tick の now は lock より前に採る。core 解放後に ops 通知を行う |
| `src/agentic_fx/ops/contracts.py:137-155`、`ops/api_server.py:144-168,293-305,624-646` | local_guard と peer 境界。ShellCaller は別扱いなので入口不在に加え rebase service の approver 直接検査が必要 |

### 2.2 flat / broker / source の確定

flat は「orders が 0 件、または全ての orders.status が `transitions.TERMINAL` の要素」で定義する。現 schema の status は NOT NULL（`store/db.py:558`）なので通常の NULL 検査は要らない。不明な文字列、将来増える非 terminal 状態も拒否し、旧/不正 fixture の NULL も許可扱いにしない。APPROVAL_PENDING、SUBMITTED、保護待ち、取消中、close 中、各 UNKNOWN を漏らさない。`orders.active`、executor の `_EXPOSURE`、OPEN 建玉だけの集計は使わない。

runtime の broker が正規の PaperBroker 配線であることを検査する。source 文字列だけの偽装では通らず、他 broker / 不明 adapter は preview reason `broker_not_supported`（POST は既存 `unavailable`）。最新 source は `paper` のみ許可する。`manual_ks_rebase` が最新なら、直後 DD = 0 なので preview reason `rebase_not_needed`（POST は `preview_changed`）、後日の操作には通常 paper MTM を待つ。replay の source は許可しない。

paper の通常経路では cashflow は構造上 0。実在しない「外部入出金の連続性証明」は要求しない。最新行の cashflow が非 0 なら契約外として拒否する。equity/balance の一致は flat の証明ではなく不整合検知用に保持する。finite / 正を検査し、丸めた表示値ではなく保存値で一致を検査する。さらに同じ settings / clock と ops 接続で作る `PaperBroker(conn_ops, settings, clock).equity()` を同一 DB transaction 内で呼び、返された balance / equity と source の保存値の一致を検査する（status provider `service.py:2155` と同じ構築形）。不一致は preview reason `account_snapshot_changed` として新しい MTM を要求する。SQL SUM を Python の逐次加算へ置換する等、式の再実装は禁止する。core 接続を ops thread へ流用しない。MTM 後の同 tick の決済で、orders は flat でも snapshot が古い balance の窓をこの検査で拾う。

`paper.starting_balance` の変更は今も cashflow に記録されない。過去にその変更がなかったとの証明はしない。累積は ledger の rebase だけを意味し、入出金調整済み総損失とは表示しない。この問題の解消は別 ticket 案 `paper-starting-balance-cashflow-history` とし（未起票）、本件で架空の cashflow 正本を作らない。

### 2.3 値 CAS と API

`GET /v1/killswitch/rebase-preview?expected_generation=<世代>` を追加。DTO は §1.3 の値、eligible、固定 reason enum、`preview_token` を持つ。不適格なら token は null とし、取得不能な金額等も null として捏造しない。reason と POST error は §3 で分ける。preview は DB/state を変更せず、mutation の accepted も作らない。

`POST /v1/killswitch/rebase` は **非 idempotent**、code は `killswitch_rebase`。body は `expected_generation` と `preview_token` だけとし、snapshot id の一致を要求しない。snapshot id/ts は確認・監査用である。

preview token は version 付き canonical JSON の SHA-256 digest。対象は generation、latched / latched_at、old_hwm、equity、balance、flat、現在 T、broker 種別、実効 starting_balance、ledger 最終 id / count / 累積幅 / 累積率。数値は保存値を round-trip 可能な一意表現で encode し、非有限を拒否、負の zero は正の zero に正規化する。表示用丸め値は digest に使わない。digest は認証 token ではなく確認した値との照合である。

commit 時に最新行で値を再構成して比較する。同値の MTM tick を何度挟んでも通る。id/ts/source の変更自体は CAS 不一致にせず、新鮮さと許可 source は毎回別に検査する。値・閾値・state・ledger が変わったら再 preview。鮮度は server の実行時刻に対して `0 <= age <= 10分` とし、未来 timestamp も拒否する。市場カレンダーは scheduler と同じ定義を使い、閉場は snapshot の鮮度より先に `market_closed` で返す。

同 generation の複数回を許し、`UNIQUE(generation)` は置かない。generation は現在値との CAS および監査値である。snapshot の値、ledger digest、DD 条件が二重適用を防ぐ。再送は既存成功の再応答にせず、再 preview で ledger と現在 HWM を照合する。

### 2.4 排他と実行境界（新設する保証）

core 注入案 (a) を維持する。OpsService に App と同じ core_lock と paper の口座評価用依存を明示注入する。既存機能とは扱わない。未注入/非対応は rebase だけ `unavailable` とし、daemon や SL/TP 監視の起動を止めない。新しい accounting 処理は ops 専用 module に置く方針で、共通 core accounting は変更しない。

POST のロック順は **ops_lock → core_lock → StateStore shared flock → BEGIN IMMEDIATE**。accepted は core 取得前に commit して DB write lock を持ち越さない。StateStore には共有 flock を保持して state と marker を読む context API を新設し、commit まで保持する。context 内で読んだ AppState.autopilot、generation、latched、latched_at と marker を再検査する。`autopilot_at_start()` は排他 flock を取るので共有保持中には呼ばない。公開 load の再呼出し、state writer / reset / reconcile もこの区間へ入れない。

core 取得待ちは要求 deadline の残りまで（現行 `request_deadline_seconds=5.0`）。取得後は共有 flock、SQLite busy、計算、commit に共通の最大 1 秒予算（要求残時間以下）を置く。待ち時間と保持時間を区別する。tick / prepare / reflection の実保持時間は未確認であり、長い既存保持で本要求が timeout になり得る。外部 I/O、通知、ユーザー入力を保持区間へ入れない。rollback 後は逆順で解放し、terminal / 投影は core と state lock の外。OS/ストレージ停止まで wall time の上限を保証するものではない。

POST は BEGIN IMMEDIATE 内で最新 snapshot、orders、ledger を再読込し、state guard 下で世代と digest を照合する。core 排他は旧 HWM を読んだ commit-core のラッチが rebase 後に割り込む窓を閉じる。SQLite の原子性自体は DB transaction で保証する。

preview は core_lock を取らない。ops lane 内で通常の `StateStore.load()` により state を読み（その shared flock は解放）、一つの DB 読取 transaction で snapshot / orders / ledger / broker を読む。state と DB の間の変化は許容し、POST の再読込/CASで拒否する。preview で書込 transaction や長寿命 state guard を作らない。

guard sweep は C4 の **ops_lock → state shared flock → DB**、core を取らない。scheduler thread は core を解放した tick の後に maintenance を呼ぶ（`service.py:2918`、通知関数内呼出しは `:2522`）。core 保持中に ops lane を取る辺を追加しない。受入 scan は core 取得全箇所からの OpsService / `_ops_lock` 呼出し、および state 排他保持から core を取得する逆辺に絞る。既存 executor の `state.update()` → `_exclusive` は core → state の順であり、全面禁止しない。rebase shared guard 内の排他再取得は禁止する。競合試験は commit-core と MTM 各一本に絞り、core を取らない decide lane / job terminal の barrier は要求しない。全経路の deadlock 不在の証明とはしない。

### 2.5 snapshot と ledger の原子更新

専用 accounting 操作で次を同じ SQLite transaction に入れる。汎用 `snapshots.add()` の途中 commit や過去行の UPDATE を使わない。

1. §2.2〜2.4、generation > 0、一致する現世代、DD ≥ T を再検査し、適用前の `daily_start_equity` を読む。日次基準が評価不能なら拒否する。
2. 最新 source 行の **DB 保存文字列の ts をそのまま複写**し（datetime を経由して再整形しない）、balance/equity も複写、`hwm=equity`、`cashflow=0`、`source=manual_ks_rebase` で snapshot を INSERT。
3. append-only `kill_switch_hwm_rebases` に一行 INSERT。
4. latest が新行で id は source より大、ts 同一、equity = hwm、DD = 0、日次基準が前後で同じであることを検査して commit。

source と同 ts で順序を id に任せるため、rebase 前に now を採った同 tick の MTM を未来時刻の INSERT で逆行させない。日次用の ts 同値行は equity が同じなので基準を変えない。次の通常 snapshot は新 HWM を引き継ぐ。どの事後条件失敗でも両 INSERT を rollback する。rebase は state.json を一切書かない。

ledger は DB migration で追加し、UPDATE/DELETE 禁止 trigger と snapshot / accepted audit id の一意参照を持つ。generation の一意制約は作らない。保持項目は rebase id、適用時刻（wall clock、snapshot.ts と別）、generation / latched_at、source/rebase snapshot id、accepted audit id、digest version/token、HWM before/after、equity/balance、DD/T、cut/cut_pct、追加損失余地。**累積幅/率/count は保存しない。** 全 ledger の `COALESCE(SUM(cut),0)` / `COALESCE(SUM(cut_pct),0)` / `COUNT(*)` だけを表示・警告・digest の正本にし、前行の保存累積との厳密照合やその不一致による拒否を作らない。append-only 内容を拒否条件にする規則を今後追加する場合も、正常な反復で行き止まりにならない根拠と正規の復旧出口を必ず記す。

新たな上限 config や AppState schema は追加しない。現 DB に schema version はない。既存 `_SCHEMA` / `_OPS_SCHEMA` の `CREATE TABLE IF NOT EXISTS`、必要な `_ensure_column` / `data_migrations` の方式で移行し、架空の版管理を足さない。`TABLE_NAMES` と完全一致 test `tests/store/test_db.py` / `tests/store/test_db_migrations.py` を更新する。旧 DB fixture、再実行安全性、既存 replay DB 初期化との互換を検証する。ledger のない旧 DB は空 ledger で開始し、過去の rebase を推測して挿入しない。

### 2.6 監査と commit 結果

正本の順序は ops accepted → snapshot + ledger commit → ops terminal。accepted audit id を ledger に一意記録する。activity は既存 `_project` の `SYSTEM / ops_killswitch_rebase`（audit id / result）だけとし、専用 `kill_switch_hwm_rebased` は追加しない。新 domain event `risk_budget_rebased` や専用の起動時投影照合 loop も作らない。

既存 `ops_request_terminal` の `endpoint_code=killswitch_rebase` を利用する。値の allowlist 登録は不要（`ops/events.py` の `_SAFE_VALUE` 形検査）。guard event は code `guard_state_changed`、ref `instance`、fields **guard_kind / state / generation の三つだけ**を維持する。HWM、DD、rebase 情報を足さない。本人の明示操作なので rebase は push しない（bot 既定では ops terminal を push しない）。監査は ops_requests と ledger で確認する。

COMMIT の送出前・成功確認後・結果不明を区別する。action は成功応答を COMMIT 前に準備し、COMMIT の正常 return 後は lock/context 解放等の通常 Exception を固定 code の best-effort 診断に隔離し、成立済み結果を failure に変えない。例外を握って lock を放置してよいという意味ではなく、finally による逆順解放を要求し、その境界も fault 注入する。activity は既存 terminal 投影に任せ、追加の commit 後 action は置かない。診断に例外文・金額を含めない。

COMMIT 呼出し自体が例外になった場合は、たとえ後で rollback を試しても不成立とは断定しない。新設内部例外 `RebaseOutcomeUnknown` に変換し、`_mutate` に `PolicyProjectionUnknown` と同じ `outcome_unknown` terminal / response への写像を追加する。新 ErrorCode は不要。accepted audit id の ledger 行（snapshot 参照を含む）があれば成立、正常な回復接続で無ければ不成立と調査できる。調査用に累積の一致を要求しない。

成功後の terminal 書込失敗/応答喪失や BaseException / process crash は、成功した本体を rollback しない。既存 `_recover_ops` により accepted のまま残った要求を outcome_unknown として調べる。ledger を根拠に調査し、自動再送や「毎回適用成功」の再応答はしない。activity/event 欠落の自動補完は保証しない。

status は既に金額を含む。本件は通常 status / bot / web / event に HWM や追加の金額 field を足さず、秘密情報、自由文例外、preview 全体を journal に出さない。preview DTO をそのまま audit terminal に保存せず、成功結果は rebase id / snapshot id / generation / latched に絞る。口座値は専用ローカル preview と DB の snapshot / ledger に留める。

## 3. 失敗時の扱い

preview の不適格理由は DTO の固定 reason enum とする。ここに列挙する reason を個別の ErrorCode に昇格させない。POST の新 ErrorCode は **preview_changed / market_closed の二つだけ**。生の例外文を返さない。認証/認可/peer、引数形式、autopilot 等は既存 code を維持する。

| 条件 | preview reason | POST の code / 処置 |
|---|---|---|
| 非対応 broker / 依存未注入 | `broker_not_supported` / `rebase_unavailable` | 既存 `unavailable`、rebase だけ拒否 |
| marker あり / state 不正 | `state_unavailable` | 既存 `invalid_state`、既存 reconcile 手順を確認 |
| 世代不一致 | `generation_mismatch` | 既存 `generation_mismatch` |
| gen 0 | `generation_unavailable` | `preview_changed`、§1.1 の例外を案内 |
| 閉場 | `market_closed` | `market_closed`、開場と新しい MTM を待つ |
| snapshot 欠損/古い/未来/非有限/非正/不明 source | `account_snapshot_unavailable` | `preview_changed`、再 preview |
| broker 再評価と snapshot 不一致 | `account_snapshot_changed` | `preview_changed`、新しい MTM 後に再 preview |
| 非 terminal 注文あり | `exposure_present` | `preview_changed`、下表の状態別案内 |
| DD < T | `rebase_not_needed` | `preview_changed`、再 preview |
| cashflow 非0 / 日次基準が評価不能 | `accounting_inconsistent` | `preview_changed`、状態/MTM の回復を確認。累積照合は含まない |
| 値・閾値・ledger・digest 変化、その他 commit 時の不適格 | `preview_changed` | `preview_changed`、再 preview |
| lock timeout / stop / DB busy | DTO を捏造しない | 既存 `request_timeout` / `unavailable` / `database_busy`、commit 前なら無変更 |
| INSERT/事後条件の失敗、COMMIT をまだ呼んでいない | — | rollback、既存の固定 code（写像不能は `internal`）。両行なし |
| COMMIT 呼出しの例外 | — | `outcome_unknown`、accepted audit id で ledger 調査 |
| commit 成功後の通常投影/解放例外 | — | 成立を保持、terminal は `ok`。投影を隔離 |
| commit 後 terminal/応答前 crash | — | accepted が残れば既存 recover → `outcome_unknown`、ledger 調査 |
| rebase 後 reset 失敗 | — | 新 HWM を保持、ラッチ継続。既存 reset/reconcile 手順 |

CLI は `preview_changed` を受けたら preview を取り直して reason を表示し、確認を取り直す。値が変わらず、commit 前 lock timeout 等で何も適用されなかったと確定する場合は同じ token の再送を許す。成功/不明の要求は自動再送しない。成功後は ledger count と HWM/DD が変わるため旧 token は拒否される。

| 非 terminal 状態 | 人が取れる次の行動（人用 close/cancel CLI は存在しない） |
|---|---|
| APPROVAL_PENDING | 既存 DECIDE の `reject` を使う |
| PENDING_FILL | 期限切れを待つ |
| *_UNKNOWN | scheduler の `_resolve_unknowns` を待つ |
| OPEN、保護途中、その他非 terminal | SL / TP または判断 loop の close 等の終端化を待つ。未解決なら状態別の運用手順を確認 |

state file 再作成での世代再利用や履歴削除を修復する保証はない。ledger の generation を一意鍵にせず、現在 state の CAS と audit id / snapshot id を使う。

## 4. test と受入条件

以下は実装時の要求であり、C2 作成時には実行していない。

### 4.1 利用目的を通す統合 test

- gen > 0、clear、flat、DD ≥ T の 11-13 相当状態 → preview → rebase → 実際の TradeLoop の cron / signal の commit-core（`loops/trade_loop.py:379,658`）で open。両経路で kill switch 理由がなく、generation 不変。他の gate は通る fixture にする。
- latched、flat、DD ≥ T → rebase だけでは open 拒否 → reset → open に kill switch 理由なし。
- rebase 後に新たな確定損失を作り、未ラッチの同世代で二回目が成功。直後の連打/同 token 再送は無変更で拒否。
- preview と POST の間に同値の MTM tick を一回/複数回挟んでも成功。balance、equity、HWM、T、generation、latched 状態、starting_balance、ledger のどれかが変われば拒否。gen 0 の例外拒否と gen>0 の同世代操作を別に確認。
- 閉場中は新鮮な snapshot が残っていても `market_closed`。開場後新鮮な MTM で実行可能。
- rebase 前に now を採った tick、同 ts MTM、日境界で `snapshot_out_of_order` が増えず、後続の unknown 解決・保護処理が走る。
- 日次損失超過状態で rebase → reset → open を実行し、日次損失理由で拒否。日次基準は不変。

### 4.2 accounting / 全状態 / race / fault

- OrderStatus 全値を parametrize し、TERMINAL だけ許可。NULL/未知状態も拒否。複数件中一件だけ非 terminal でも拒否。
- broker 非対応、source 偽装、欠損/未来/10分境界/陳腐化、非有限、非正、不一致、cashflow 非0、日次基準欠損の全拒否で書込みゼロ。
- row.ts は source と同一、id は大、cashflow 0、DD 0。次の通常 MTM へ新 HWM を継承。過去行は UPDATE/DELETE されない。
- commit 成功後の投影失敗/通常 lock 解放例外 → 本体成立・terminal ok、COMMIT 例外 → outcome_unknown・ledger で成立を判定する fault を固定。snapshot/ledger 各 INSERT、postcondition、commit 前後、terminal の fault injection。DB 正本は両行または両方なし。crash 復旧で state が勝手に解除されない。
- core / state / DB の各取得停止、stop_event、要求 deadline と取得後1秒予算を検証。同 token の無変更 timeout 後の再試行、shared guard 内の autopilot/marker 直接検査も確認する。barrier は rebase 対 commit-core と rebase 対 MTM の各一本。core 全取得箇所と state 排他からの逆辺を §2.4 の範囲で scan し、decide 全経路の試験へ広げない。
- 表現誤差のある合成値で閉じた注文3件以上・rebase 5回以上を通し、broker 式の再実装や累積照合による拒否がない。ledger の SUM/COUNT による累積幅/率/count、利益を挟むケース、同世代複数、restart/new generation、閾値変更、3倍ちょうど/超過を固定。累積 > 100 points でも上限を理由には拒否しない（他の健全性条件は必要）。
- migration 再実行、旧 DB、trigger、snapshot/audit 参照一意、generation 非一意、TABLE_NAMES と二本の完全一致 test を検証。新 config 必須化で起動を止めない。

### 4.3 CLI / 境界 / 表示

- approver + UDS + pidfd + 許可 peer のみ preview/POST。service 直接呼出しでも ShellCaller を forbidden にする試験を一本置く。operator/bot/web/web-write/notify、TCP、service 子孫は拒否。ShellCaller 入口不在を AST/source scan。
- 非 TTY、pipe、EOF、不一致は POST/accepted なし。`rebase <世代>` 完全一致だけ送る。server の TTY 保証と混同する test は作らない。
- preview と確認文は今回幅/率、過去累積、適用後累積/count、追加余地、3倍警告、他 gate 不変、閉場不可、reset 別操作を含む。CLI は金額を再計算しない。
- reset 成功後は opsalert C4 §4.7 の固定文を shell / ctl がそれぞれ一回出す。ctl は DD 判定なしで固定 rebase 案内も出し、その案内が C4 固定警告を置き換えない。
- 新しい金額 field が通常 status / bot / web / event に増えず、journal / audit body に preview 全体・金額・秘密が漏れない。既存 status の balance/equity は現行契約のまま。
- 資金保護の不変条件を回帰で固定する: (1) config に kill switch や rebase を無効化・自動化するキーが無く、既存の閾値検査 (`tests/test_config.py` の `drawdown_kill_pct` の範囲検査) が残る。(2) rebase の service method と CLI の呼出し元を source scan し、LLM の tool 表・plugin worker・ask・improve・scheduler・reset・reconcile・replay から到達しない。(3) reset・reconcile・自動解除・起動時の復旧を実行しても HWM と ledger が変わらない (rebase は人の明示操作だけ)。server が直接 UDS 呼出しに TTY を保証しない境界 (§1.4) は維持し、TTY 検査は CLI 側の test に限る。

### 4.4 replay の受入範囲と既存保護

本機の常駐 daemon や実データでは実験しない。`backtest/runner.py`、`_RecordingStateStore`、`replay_ks_rebase` は変更せず、共通 accounting 関数を作っても replay の自動解除をそこへ寄せない。replay は flat を要求せず、rollover / 市場 / 当該 tick MTM の条件で自動 release する別の評価規則である (`runner.py:236-305,320-357,433-448`)。manual endpoint / ledger は replay から到達不能にする。

受入は次の三点に限定する。

1. 実装着手時の基準 commit を記録し、`src/agentic_fx/backtest/` と `src/agentic_fx/core/{executor,risk_gate,scheduler,accounting}.py` の実装差分 0 を確認する。本版は ops 専用 module を選ぶ。将来共通 accounting 関数が不可避なら、その理由を記録して accounting.py だけを差分0対象から外し、新処理が replay から呼ばれないことを scan で示す。他の除外は広げない。
2. backtest package が manual rebase module、ledger 表 `kill_switch_hwm_rebases`、source `manual_ks_rebase` を import/参照しないことを source/import scan で固定する。共通 schema による空表の初期化は手動機能への到達ではない。
3. 既存 `tests/backtest/test_golden_1m.py`（canonical JSON の byte 比較）、`test_golden_diff_*.py`、`test_kill_switch_replay.py`、該当 improve gate tests が green。既存 golden を差分隠しのために更新したり、丸め/field 除外を加えたりしない。

独立した基準 HEAD 比較 harness や新 replay fixture は作らない。opsalert の旧 T1a は C4 付録 C-1 の別チケット案 `killswitch-latched-activity-repeats` に移っており、本件の依存/受入にしない。replay row/cashflow/日次基準/failure compensation、reset marker/reconcile crash、DD 閾値/不正 HWM fail-closed の既存 tests は維持する。本版で tests は実行していない。

## 5. 裁定済み / 技術で決定（C0 の裁定節を置換）

| 設計点 | 区分 | C2 の決定・理由 |
|---|---|---|
| R1 reset と分けるか | 技術で決定（既存 R4 と整合） | 独立 rebase。意思と監査を分け、DB/state の疑似原子性を作らない。reset への自動付随は不採用 |
| R2 reset 済みの同世代 | 技術で決定 | 許可。同世代一回制限も外す。DD/flat/値CAS で連打を防ぎ、不要な open を要求しない |
| R3 建玉中 | 技術で決定 | flat-only。TERMINAL 補集合がゼロ。含み損益の予算固定と承認待ち注文の持越しを避ける |
| R4 累積上限 | **2026-11-13 裁定済み** | 硬い上限なし、ledger/累積表示/警告。理由は §1.2。実口座導入 ticket の硬い上限は将来の必須要件 |
| R5 anchor 再設定 | 技術で決定（R4 に従属） | anchor ごと削除。上限到達後の架空の回復待ちも削除 |
| R6 web/bot | 技術で決定 | 発行不可、local_guard/UDS のみ。TTY は client UI の層 |
| 警告倍率 | 技術で決定 | 累積各回率の合計 > 現在 T の3倍。意味・境界・理由は §1.3 |
| CAS/時刻/排他 | 技術で決定 | 値 digest、source ts、core 注入と state shared guard。§2.3〜2.5 |

### 5.1 commit される文書へ残す義務（実装 T3）

本 spec は docs/ に収載済み (6915548b) だが、義務は全体設計書からも辿れるようにする。実装 commit で全体設計書 `docs/superpowers/specs/2026-07-25-agentic-fx-design.md` の broker 節へ次を追記する案とする。本 spec を `docs/superpowers/specs/` に正式収載する場合も同文を本文に含め、broker 節から参照する。新たな承認を要する案の放置ではなく、実装時の完了条件である。

> 実口座 broker の導入前提として、HWM rebase の硬い累積損失上限と上限到達時の正規の復旧出口、入出金の正本、broker 側 exposure、通貨精度の設計・検証を必須とする。完了するまで実口座の rebase は有効化しない。PaperBroker 限定の上限なし・累積警告は代用にならない。現行 paper.starting_balance の変更は cashflow 履歴に残らず、ledger は入出金調整済み総損失を証明しない。

同じ commit で `docs/tickets/open/killswitch-reset-hwm-rebase.md` の「処置案・裁定」に利用者裁定（硬い上限なし・累積警告・PaperBroker 限定・実口座導入時に硬い上限必須）を記録する。starting_balance 履歴問題は別 ticket 案として記録し、本件の前提にはしない。本改訂では既存 docs/ticket を編集・起票していない。

未確認: core lock の実保持時間、実装後の lock scan/競合/fault tests、実口座の口座/入出金/精度契約、11-13 実データの詳細、鍵を持つ同 UID 非子孫 process に対する鍵ファイル保護。`data/`、個人 config、秘密情報は調査していない。r2 の本番件数観測を本作業で再検証したとはしない。起動通知/配送/health は opsalert C4 の範囲。自動 close、日次損失 reset、DD ラッチ時点の tick 化、実口座対応は対象外。

## 6. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-13 | C0 | 独立 rebase、flat-only、snapshot CAS、一世代一回、累積上限/anchor を提案 | ticket と通知設計 R4 に対する初案 | —（tmp 文書、未確認） |
| 2026-11-14 | C1 | 上限/anchor/同世代一回を削除。PaperBroker 限定、値CAS、source ts、TERMINAL flat、core/state guard、累積3倍警告、実配線/replay byte 一致の受入を確定 | 2026-11-13 確定裁定と opus 1周目を絶対基準で再判定。当時の opsalert C2 reset 固定文と整合 | —（c1.md、未 commit） |
| 2026-11-14 | C2 | 累積保存/厳密照合を削除、broker.equity 再利用、commit 結果境界、error/試験縮小、live 配線、C4 整合、永続文書への追記案を明文化 | r2 の Critical 1 / Important 4 / Minor 12 と食い違い12を絶対基準で裁定。浮動小数による恒久拒否と成功の誤監査を防ぐ | 6915548b |
| 2026-11-14 | C2.1 | §4.3 に資金保護の不変条件の回帰 (config 無効化不可・LLM 等から到達しない・人の明示操作以外で rebase しない) を追加 | terra の C2 閉鎖確認 Important 1 (本文の禁止事項が受入 test として明示されていない) | 6915548b |

## 7. 短い task 表

| Task | 範囲 | 完了条件 |
|---|---|---|
| T1 DB/accounting | append-only ledger、原子 snapshot 更新、ops 接続 PaperBroker.equity、SUM/COUNT、既存 migration 方式 | §4.2。TABLE_NAMES と test_db/test_db_migrations の完全一致二本、旧DB/精度反復/fault/日次基準 |
| T2 排他/API/監査 | core 注入、shared context、approver 直接検査、DTO reason と新 error 二つ、RebaseOutcomeUnknown 写像、C4 lane 規約 | §2.4/2.6/4.2。commit-core/MTM barrier 各一本と限定 scan。api_server の「local_guard の3本」コメントを本数非依存の説明へ更新。依存なしでも daemon 起動 |
| T3 CLI/docs | ctl/help/cli_surface、確認と警告、ops-api の7群表、ticket 裁定欄、全体設計 broker 節/正式 spec | §4.3/5.1。C4 固定文維持、ctl の固定 rebase 案内。実口座上限義務と starting_balance 制約が commit される文書に入る |
| T4 非回帰 | live cron/signal、reset/保護、module 差分0 + scan + 既存 golden byte 比較 | §4.1/4.4。新 replay fixture/harness、専用 domain event/activity/照合 loop を作らない |

## 付録 A. opus 2 周目の指摘ごとの採否

C1 付録 A の r1 採否は C1 に履歴として残す。本版の規範は本文と下表。設計上の処置を定義したものであり、実装・試験・既存文書更新の完了を意味しない。

| ID | 採否 | 理由・反映先（却下/補正部分を含む） |
|---|---|---|
| C-1 | 採用（保存累積案は削除） | §1.3/2.2/2.5/4.2。累積列と厳密照合拒否をなくし SUM/COUNT だけを正本に。balance は ops 接続 PaperBroker.equity。監査用累積を残して警告する代案も不要で不採用。3注文以上・5回以上の誤差反復試験 |
| I-1 | 採用 | §2.6/3/4.2。COMMIT 例外を専用内部例外→outcome_unknown。正常 commit 後の通常例外を隔離し terminal ok。応答前 crash/terminal 保存失敗は既存 recover と ledger 調査 |
| I-2 | 採用 | §4.4/T4。module 差分0 + 到達不能 scan + 既存 golden byte 比較。新 fixture と独立比較 harness を削除。旧 T1a は killswitch-latched-activity-repeats |
| I-3 | 採用 | §2.3/3/T2。詳細理由は preview enum。POST 新 ErrorCode は preview_changed / market_closed の二つに限定し、他は既存を再利用 |
| I-4 | 採用（scan 条件を補正） | §2.4/4.2。新しい辺の逆辺 scan、commit-core/MTM 各一本へ縮小。core 保持中の StateStore._exclusive 全面禁止は却下。現行 executor が state.update でラッチし、その向き core→state は順序と整合するため。state exclusive→core と core→ops の逆順、shared→exclusive 再取得を調べる |
| m1 | 採用 | §4.1。Executor._open の backtest 経路を live と扱わず、TradeLoop cron/signal commit-core を統合試験にする |
| m2 | 採用 | §1.4/4.3/T2。service 先頭で caller is Principal.APPROVER を強制。ShellCaller の scope/autopilot 例外を迂回にしない |
| m3 | 採用 | §2.4。共有保持中は context の AppState.autopilot / marker を使う。排他を取る autopilot_at_start を呼ばず自己待ちを防ぐ |
| m4 | 採用 | §2.4/3。取得待ちは要求残時間、取得後は最大1秒。実保持時間は未確認。確定未適用 timeout は値が不変なら同 token 再試行可 |
| m5 | 採用 | §2.4。preview は通常 load + 一読取 transaction、core 不要。DB/state 同時性は POST 再検査に委ねる |
| m6 | 採用 | §1.3/2.5。適格時の前余地0を使い additional_headroom 一本に統合、冗長列を持たない |
| m7 | 採用（理由を明記、gen0許可案は不採用） | §1.1。既存 kill switch 世代に限定する。gen0 は正常初期/履歴喪失を区別不能。初回 intent が必要な例外を明記し、「不要」を gen>0 に限定。gen>0 の完全性までは保証しない |
| m8 | 採用（固定案内を選択） | §1.4/4.3。ctl は毎回固定文の後に rebase 案内。client に DD 判定をさせない |
| m9 | 採用（専用 activity を削除） | §2.6。既存 ops_killswitch_rebase のみ。二行投影と不要な commit 後 action を避ける |
| m10 | 採用 | §5.1/T3。全体設計 broker 節と正式 spec への具体的追記案、ticket 裁定欄更新を完了条件に。tmp だけでは永続化未完。starting_balance 履歴は別 ticket 案、未起票 |
| m11 | 採用 | §3。人用 close/cancel があるかのような案内をやめ、reject/期限切れ/unknown 解決/保護・loop 終端待ちへ状態別に分ける |
| m12 | 採用 | §2.6。本人の明示操作なので push しない。監査は ops_requests/ledger と明示 |

r2 §4 の source timestamp は保存文字列の完全複写とし、broker 再評価は MTM 後決済の窓を拾う要件として残した。§5 の関連版は C3 からさらに利用者承認済み C4 に更新。§6 の縮小/分割は上表へ統合し、不要になった累積照合・新 activity・新 fixture を復活させない。§8 の未確認は §5 に引き継ぎ、本番データを再調査したとはしない。全面却下の指摘は無いが、選ばなかった代案と I-4 の補正は上表の理由による。

## 付録 B. r2 §7 の食い違い 12 件と処置

| # | 食い違い | C2 の処置 |
|---:|---|---|
| 1 | C1 の HEAD が afa7819f | 冒頭を再確認済み 558cb9fd に更新。src/tests/afx-ops の比較差分が空なことも確認。C4 の基準570d7fe1とは混同しない |
| 2 | maintenance の根拠行が不適切 | §2.4 を scheduler thread service.py:2918 と関数内 :2522 に訂正。core 解放後という順を記載 |
| 3 | 架空の schema 版管理、TABLE_NAMES/test 漏れ | §2.5/T1 を既存 CREATE IF NOT EXISTS/_ensure_column/data_migrations 方式に訂正。TABLE_NAMES と完全一致二本を要求 |
| 4 | orders.status に NULL が通常あるような記述 | §2.2 で NOT NULL と明記。未知状態は拒否、旧/不正 fixture の NULL を許可しない防御だけ残す |
| 5 | 人用 close/cancel CLI を案内 | §3 に状態別の実行可能な案内を記載。m11 と同処置 |
| 6 | Executor._open を live と扱う | §4.1 を TradeLoop cron/signal commit-core に置換。m1 と同処置 |
| 7 | commit unknown 写像の仕組みがない | §2.6/T2 に専用内部例外と _mutate 写像の新設を明記。I-1 の fault 条件を追加 |
| 8 | terminal endpoint_code の値登録が必要との誤記 | §2.6 に形検査のみで登録不要と訂正。必要な route/contracts/DTO と新 error 二つの登録は T2 に残す |
| 9 | 古い関連設計版と存在しない fixture | 冒頭/§1.4/4.4 を承認済み opsalert C4 に統一。旧T1a 別 ticket に依存しない |
| 10 | OpsService 構築の根拠行が誤り | §2.1 を _build_ops_service :2131/:2226-2232 に訂正。core は未注入、新設が必要 |
| 11 | ticket の処置案・裁定が空 | §5.1/T3 で実装 commit 時の記入内容を具体化。本作業は C2 のみであり、ticket 自体は未更新 |
| 12 | api_server コメント「local_guard の3本」が古くなる | T2 で本数非依存の説明へ更新する。コメント修正自体は未実施 |

## 付録 C. opsalert C4 との契約照合

| 契約 | C2 の扱い |
|---|---|
| C4 §4.7 reset 固定文 | shell/ctl の成功時に一回。DD 数値なし、条件で省略なし。ctl の rebase 固定案内は別文 |
| C4 §4.3〜4.4 guard sweep | state だけを正本にして単一 helper が event 化。rebase は state 不変、独自 latch/release event や sweep を足さない |
| C4 再入禁止 | ops lane 内で公開 emit_pending_guard_notices を呼ばず、instance/thread 所有検査を回避しない |
| event 三 field | guard_kind/state/generation を維持。HWM/DD/rebase を増やさない |
| shell reset/reconcile | 直接 state mutation 後に wrapper の既存方針を維持。rebase は approver service 直接検査で shell を拒否 |
| lock 順 | sweep は ops→shared→DB、rebase POST は ops→core→shared→DB。tick の sweep は core 外。shared 保持中に autopilot 排他取得なし |
| 保証の限界 | C4 の clear+event無しの履歴欠落、shell mutation の世代間順序の残余競合を本件で解消済みとしない |
| replay 分割 | T1a は killswitch-latched-activity-repeats。新 fixture/基準比較 harness は本件に持ち込まない |

