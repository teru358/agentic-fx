# コードから読み取る現状の影響地図 — 設計 C2.1

- 日付: 2026-11-14 / 状態: C2.1。2026-11-14 利用者承認。T0 (頁の雛形と手順) に着手、T1 以降は opsalert の merge 後。
- チケット: `docs/tickets/open/code-impact-map.md`
- 文書照合 HEAD: `4562697450257a142d9337a3f428d574e92ce514`。C1/R2 のコード観測基準は `558cb9fd` / `6915548b`。§3 の F/D はその **C4 前の履歴観測** を修正したものであり、現在の並行実装の完成を主張しない。
- 正式試行の基準 revision は **opsalert C4 の merge 後**。merge SHA と着地後コードで正解表を凍結する。現在は未確定。それまでは頁の雛形と手順だけを用意する。
- 利用者裁定は確定: docs/ に公開 commit、契約・責務・影響関係が変わるチケットだけ該当頁を同じ commit で更新、kill switch / guard state で試行。基本設計とは独立した現状の正本とする。
- 本書は設計書。docs/impact-map/・scripts/・tests/ の成果物は task 表に従って別 commit で作る。

## 1. 動作目線 — 誰が何をどう使うか

初回で目的に効く最小は、**試行頁一枚 + 独立正解表 A/B + extract/evaluate + test 約六本**である。literal 索引の候補を人が書く入口・呼出し元の辺で補い、「自動ラッチが event を出さない」「shell が lane を迂回する」という起票動機を頁から発見できるかを測る。索引だけの recall を目的の達成に読み替えない。初回は hash、鮮度、自動 affected/check、各種管理 JSON、速度の反復測定を持たない。

指揮者は設計 C0 時に領域頁と source を読み、触る予定の symbol から手で逆引きする。入口×副作用、別 package の consumer、F/K/D を照合し、実装 brief へ守る K と禁止経路を渡す。まだ差分のない設計段で affected は実行しない。頁の差分作成・索引実行・閉鎖裁定は指揮者が持つ。

初読レビューは §4.1 の隔離 checkout でコード・要求・test を対象とし、地図を除いた差分を渡す。独立所見を保存した後、同じ commit の頁差分も別段でレビューする。同 commit の裁定を崩さず、独立初読と頁の整合レビューの両方を行う。local merge と閉鎖記録を主経路にする。

### 1.1 頁の読み方

冒頭約20〜30行に責務、入口、主要 resource、主要な逸脱と未確認を置く。詳細を一画面に押し込めず、必要なら隣接頁へ分ける。

| 層 | 内容 | 根拠 |
|---|---|---|
| F: 観測事実 | 現コードの呼出し・永続化・投影・障害動作 | revision と `path::qualified_symbol`。行番号は観測時点の参考だけ |
| K: 守る契約 | 裁定・承認済み設計による責務と許容する欠損窓 | 公開 spec の節と test。承認済みでも未実装ならそう書く |
| D: 既知の逸脱 | F と K の差、文書とコードの差、残る構造的問題 | F/K ID、公開チケット、解消条件または受容境界 |

### 1.2 本試行の境界と正本

製品コード、実 DB、daemon、bot、個人設定へ介入しない。公開可能な source と合成 fixture だけを使う。

契約の正本は次の公開配置とする。

- `docs/superpowers/specs/2026-11-14-ops-alert-delivery-design.md`（opsalert C4、承認済み、実装中）。§10 R1〜R5、§2、§4、§9 を参照する。
- `docs/superpowers/specs/2026-11-14-killswitch-hwm-rebase-design.md`（HWM C2.1、承認済み、opsalert T1b 後に実装待ち）。§1〜2、§4〜5 を参照する。

今回 C4 正本の内容確認に用いたのは、利用者指定の opsalert 作業 checkout にある同名 docs/ spec である。主 checkout の上記 C4 path は現時点で未収載。正式頁を公開する際は C4 spec が公開履歴から読めることを指揮者が確認し、未収載なら試行を開始しない（C4 merge 待ち）。正式頁の必須 link を私的な作業 checkout やレビュー記録へ向けない。利用者裁定を memory から推定しない。

## 2. 仕組み — 小さい索引から始める

### 2.1 採用方式と置き場

「人が保守する領域頁 + 小さい literal 索引 → 独立正解表で測定 → 必要な種類だけ抽出規則を追加」の順とする。人手文書だけでは別入口を見落としやすく、最初から全 call graph・SQL parser・型推論を作ると試行の費用が大きい。生成 graph を公開正本にはしない。

初回の配置は次だけとする。

```text
docs/impact-map/README.md                 読み方・公開規則・手動閉鎖手順
docs/impact-map/domains/kill-switch.md     宣言と F/K/D、手で保守する辺
docs/impact-map/oracle/kill-switch-a.json  独立表 A（五項目）
docs/impact-map/oracle/kill-switch-b.json  独立表 B（五項目）
docs/impact-map/evaluations/kill-switch.md 不一致裁定・凍結母数・三分類・初回結果
scripts/impact_map.py                     extract / evaluate のみ
tests/scripts/                           最小六本程度の合成 test
tmp/impact-map/<run-id>/                  index / metrics 等の生成物、非 commit
```

JSON は初回の正解表と抽出・評価出力に限る。registry / verification / deviations / extraction-rules 等を空の雛形で先行導入しない。resource / symbol / 手動依存宣言は頁の front matter と ID 付き F/K/D だけを正本にし、生成 registry は後続でも `tmp/impact-map/` に置く。標準ライブラリの単一 CLI とし、source を import しない。

README の公開規則には「**spec・チケットを含む他資料から、本番の時刻・event 行や件数・金額・閾値等の運用観測値を頁へ写さない**」と明記する。契約上の固定 code/仕様定数と運用の観測値を区別し、fixture は合成にする。鍵・ID・URL・口座・個人設定値・例外自由文を保存しない。公開頁の必須 link を `tmp/`、私的 checkout、ホームの memory へ向けない。将来 check でも検査するが、初回は指揮者が確認する。

### 2.2 頁の雛形と識別子

front matter は JSON object としても有効な制限 YAML とし、初版は標準ライブラリの JSON で読む。任意 YAML の実装を初期費用に含めない。

```markdown
---
{"id":"kill-switch", "owner_role":"conductor",
 "resources":["state:app_state.kill_switch_latched", "event:guard_state_changed", "db:account_snapshots", "db:kill_switch_hwm_rebases",
              "source:manual_ks_rebase"],
 "anchors":["src/agentic_fx/store/state.py::StateStore.reset_kill_switch"],
 "related":["auto-killswitch-latch-emits-no-event", "killswitch-reset-hwm-rebase"],
 "tests":["tests/ops/test_ac51_killswitch_api.py"]}
---
# 責務 / 入口 / resource / 主な逸脱
## F（anchor、観測 revision、参考行、test と検証状態）
## K（ID、入口×遷移、前提、副作用、障害時、正本、F/test）
## D（F/K、公開 ticket、解消条件・残余リスク）
## 影響先と検証記録
```

related は ticket ID とし、open/closed 両ディレクトリで解決する。閉鎖移動で根拠消失にしない。

resource は state field / file / marker / event / activity / db / config / endpoint / metric を別 namespace にする。`guard_state_changed` と activity は混同しない。主 anchor は repo 相対 path と qualified symbol。module 定数は `path::<module>.NAME`、解析不能な非 Python 入力だけ明示的な file hash を使う。相対 link を併記できるが行番号は一致判定に使わない。

### 2.3 最小索引の保証と限界

明示 resource の識別用 literal（event code、table 名、state field、marker suffix、config key、endpoint code、metric key）を Python AST の文字列・attribute・keyword と照合し、包む symbol と package を記録する。コメント候補の tokenize 層は後続へ送り、初回は扱わない。任意文字列の全文や設定値は出力しない。標準ライブラリの走査を必須とし、rg は人の補助に限る。

最小レコードは `resource, path, symbol, package, match_kind, reference_line`。候補 ID は resource/path/symbol/match_kind の組で、symbol 内の重複は採点上まとめる。初版は reader/writer や callback chain を断定しない。「literal がある候補」として人が分類する。dict の `state.update` を StateStore の writer と誤認せず、同名異型も候補止まりとする。

初回 header は schema_version、固定した source revision、入力 roots と入力数、diagnostics/unsupported に限る。tree/script/rules digest は後続へ送る。key/配列の順序を固定し、日時・絶対 path・自由文を canonical JSON に入れない。AST 解析失敗や未走査を成功に埋めない。

入力は git 管理の `src/agentic_fx` と `afx-ops/afx_ops`、明示した公開 schema/example。test は runtime と別層。変更中の tracked source と明示指定された新規 source を読む。未指定の新規 runtime file は未走査として表示する。個人設定、環境ファイル、data/logs/plugins/tmp/venv/worktree/.git を内容走査せず、symlink を辿って入力 root 外を読まない。自由文・SQL parameter 値・鍵・個人設定値は保存しない。

**最小構成の限界:** literal を持たない caller、引数経由 config、callback、alias、動的 SQL、文字列プロトコルの意味結合は分からない。literal 不在を関係不在に変換しない。たとえば data resume の callback 注入は別途手読みが要り、`risk.drawdown_kill_pct` の引数伝播は literal 索引だけでは証明できない。source の部分一致条件を見つけてもその文言互換性を保証しない。

### 2.4 規則追加の条件

二件の通常チケットの運用結果と FN の実例を見て、次のうち必要な種類だけ追加する。追加のたびに oracle、未使用 fixture、種類×領域の採点を用意する。初版の必須実装ではない。

| 種類 | 追加候補 | 未解決を残す境界 |
|---|---|---|
| SQL | 定数 SQL の read/write/schema、CTE、INSERT SELECT、有限な table 変数 | 動的識別子/dialect 未対応を明記。SQL 値を保存しない |
| event / wrapper | emit→wrapper→caller、poll transport と code dispatch、schema の区別 | 汎用 poll を個別 event consumer に昇格させない |
| state / 入口 | receiver 型の限定解決、marker 投影、ctl/router/action/lane chain | dict update、callback 不明、動的 dispatch は未解決 |
| config | Settings→引数→型付き reader の有限 chain | 同名 attribute だけで型を断定しない |
| package 境界 | envelope/flatten/投影の明示結線と schema 比較 | AST 同形だけで同じ責務にしない |
| 文字列プロトコル | producer と部分一致 consumer の人手 K 関係 | 自動抽出を初期保証に含めない。専用挙動 test で守る |

追加版の relation は reads/writes/produces/consumes/mutates/invokes/exposes/schema/duplicates/protocol を区別する。direct は当該 site だけで確定、inferred は規則と根拠 chain、unresolved は symbol/site・reason・候補 resource（不明も可）を残す。最大 hop 等は実際に wrapper 規則を足す時に fixture と固定し、未実装の高機能を約束しない。

### 2.5 実行と時間予算

初回の指揮者向け CLI 案（未実装）:

```bash
.venv/bin/python scripts/impact_map.py extract --domain kill-switch --out tmp/impact-map/current
.venv/bin/python scripts/impact_map.py evaluate --domain kill-switch --index tmp/impact-map/current/index.json --out tmp/impact-map/current
```

evaluate は A/B の裁定済み正解表と、ID 付き頁の被覆宣言を読む。全文の意味を自動採点するのではなく、頁が宣言する symbol/辺と根拠行を出し、人が source と照合して K/D の正しさを裁く。凍結表の入力指定と出力形式は実装計画で固定する。

既存 interpreter を指揮者が選ぶ。sandbox に uv・rg・commit 能力を要求しない。無ければ実行不能と記録する。両 runtime root を走査し、初回は入力数/byte と **一回の実測**だけを記録する。200 行程度・1〜2 日は R2 の推定であって予算達成の約束ではない。cold/warm 各五回、CPU/OS 記録、cache、extract 10秒/全root 30秒/affected 5秒という性能目標の評価は後続 task とする。daemon・外部通信・LLM・本番 DB を起動しない。

## 3. 試行領域 — F / K / D

### 3.1 境界

口座 snapshot/HWM → DD gate → executor の live/replay 入口 → StateStore/marker → shell/ctl/API reset・reconcile → 監査/event → daemon Notifier、bot、notify、status/activity 表示まで。起動回復、afx status の独立 marker 投影、replay の解除・補償と採用比較 metric も含む。data_feed は共通 guard producer と shell resume の迂回、停止通知の不在を含む。autopilot 全般や bot health 全実装は別領域だが接続点を残す。

以下は C4 前の比較用雛形。正式頁は C4 merge 後に source/test で再観測し、F06/F07/F09 は特に置換する。以下の根拠はすべて repo 相対。表の行番号は基準コードでの参考値。test 名は根拠候補を含み、今回未実行で、assert を全件再検証したという意味ではない。

### 3.2 観測事実（独立 oracle の代用品ではない）

| ID | 観測と主 anchor | test / 限界 |
|---|---|---|
| F01 | `src/agentic_fx/core/risk_gate.py::evaluate` は latch と現在DDを別々に拒否し、引数 risk の drawdown_kill_pct を読む。理由文字列を `core/executor.py::Executor._evaluate_and_execute_open` が部分一致で DD と認識する | `tests/core/test_risk_gate.py`、`tests/core/test_executor.py`、`tests/test_e2e_worker_isolation.py` の既存文字列 pin（参考200）。文言がラッチ成立に影響する |
| F02 | live は `loops/trade_loop.py::TradeLoop._run_once_impl` / `_run_signal_mission` → `Executor.open_from_snapshot`。replay は `backtest/runner.py::run_replay` → `Executor.handle_intent` → `_open`。両経路は `_evaluate_and_execute_open` へ合流。DD拒否で update(true)、通常は activity を繰り返す。DurabilityUncertain では load 確認後 `Executor._best_effort_record` → `_notify`、通常 latch ではこの直送無し | executor test。event/bot/notify と daemon Notifier の直送を分ける |
| F03 | `store/state.py::StateStore.__init__` は realpath 化し、`StateStore.load` は marker を latched に投影、`file_value` は raw。`StateStore.update` は marker を見ず raw を読む。単独 true は raw true なら no-op、false→true は世代と時刻を進め、update(false) は発動時刻を消す | `tests/store/test_state.py` |
| F04 | `store/state.py::StateStore.reset_kill_switch` は世代一致/latched/marker無しを検査、marker永続化→state保存→marker削除。世代/発動時刻を保持。`confirm_latched` は marker無しでno-op、marker中 raw false なら次世代true、raw trueなら維持 | state test、`tests/ops/test_ac51_killswitch_api.py` の store crash grid |
| F05 | `ops/client.py::parse` → `Runner._op_killswitch_reset` / `_op_killswitch_reconcile` → `ops/api_server.py::_call` → `ops/service.py::OpsService.reset_kill_switch` / `reconcile_kill_switch`。reset client は status の reconcile_required を先に確認。`cli_surface.py` の宣言は local_guard/approver。API reset は autopilot 中拒否、reconcile は可。`OpsService._restricted` は ShellCaller を除外 | AC51。shell/API 非対称は別々に記録 |
| F06 | **C4着地で置換**。`ops/service.py::OpsService._mutate`（参考868、action884）→ `_ops_lane` 内 action。現状 reset/reconcile は `_guard_event` から released/latched を直 emit。`_terminal` は別の `ops_request_terminal` も発行 | AC51、`tests/ops/test_ac10_45_state_and_deadlines.py` |
| F07 | **C4着地で置換**。`commands.py::Commands.dispatch` の reset と `Commands._killswitch_reconcile` は StateStore 直 mutation + activity。`Commands._data_resume` も outage.request_resume 直呼び。一方 ops の data resume callback 後は guard(data_feed/resume_requested) を出す。停止そのものはこの guard event を出さない | `tests/test_commands.py`。shell reset activity builder は `src/agentic_fx/core/display_time.py::kill_switch_reset` と `afx-ops/afx_ops/display_time.py::kill_switch_reset` の正本と写し（`tests/core/test_display_time.py::test_copy_in_afx_ops_is_byte_identical_to_canonical_module` が byte 一致を保護） |
| F08 | `service.py::build_app` は marker 確認要求 activity を出す。現 sweep は improve 用。`service.py::_recover_ops` → `ops/service.py::OpsService.recover_after_journal` は accepted-only を outcome_unknown + ops_request_terminal で終端し、guard を補わない | ops 経由 reset 適用後/event前 crash の統合 test の有無は未確認 |
| F09 | **C4着地で置換**。`ops/events.py::EventStore.emit` / `poll` は ops_events writer/reader。`emit_best_effort` は validate_fields を try 前に実行するため schema 違反は伝播。INSERT 障害は rollback/失敗計数で隔離し、`event_stream_health` を別 event として出す（GS-CONSUME への接続） | `tests/ops/test_ac36_38_audit_and_events.py`。best effort を全例外無害と書かない |
| F10 | `afx-ops/afx_ops/gate/render.py` の OPS_EVENT_SPECS と整形、`bot/adapter.py` の poll/send/cursor が guard と ops_request_terminal を扱う。terminal は既定投稿False。send失敗では cursor を進めず後で再試行 | `afx-ops/tests/bot/test_ops_notices.py`。runtime の非零終了説明とは範囲差 |
| F11 | `afx-ops/afx_ops/api_client.py::NotifyClient.events`（参考610、487はBotClient）→ `notify/runtime.py` poll/send/cursor、`notify/payload.py` の検査/flatten。初回は high まで skip。guard と terminal を扱う | `afx-ops/tests/notify/`。bot と別 cursor |
| F12 | `ops/service.py::OpsService.status` / activity API → `afx-ops/afx_ops/web/request.py::_invoke` → status と `web/render.py::render_activity`。web は event consumer ではない。`web/render.py` の ACTIVITY_FIELDS は描画の使用根拠にならない。ctl `ops/client.py::format_status` / `_kill_switch`、`commands.py::Commands._status`、`service.py::build_splash` も status consumer | `afx-ops/tests/web/`、`tests/ops/`。status と配送成功は独立 |
| F13 | `entry.py` の afx status → `status.py::read_state` は StateStore と別に raw+marker を読む。lstat OSError は marker有り扱い。StateStore は realpath後、statusはその場でrealpathしない | 親 dir 不在は learning/clear、lock file 不在は lock 無し読取、全例外は unavailable。StateStore._shared / AppState 既定値との差は未確認。全分岐を symlink と併せ合成 fixture で StateStore.load と比較する |
| F14 | live writer は `core/accounting.py::record_snapshot`（自前 INSERT）。`store/snapshots.py::add` は src 内 caller 無しで live writer に数えない。reader は accounting.current_account / daily_start_equity、core/scheduler.py の equity 非正/欠損時の予約取消、loops/summary.py、snapshots.latest の各 caller。HWM の manual_ks_rebase 行は latest の全 reader に見える | accounting/snapshot test。HWM変更がDD入力を変える |
| F15 | `backtest/runner.py::_RecordingStateStore.update` / `_kill_switch_event` / `_auto_release_kill_switch` / `run_replay` はラッチ記録、activityから理由復元、HWM rebaseと解除、commit失敗時補償。公開 artifact は kill_switch_events / kill_switch_latches | replay test。live helper と別責務 |
| F16 | `backtest/metrics.py` が kill_switch_latches を生成する。これは `plugin/strategy_gate.py`、`loops/improve_loop.py`、`ops/change.py` の比較 key。`src/agentic_fx/metric_words.py` と `afx-ops/afx_ops/metric_words.py` は正本と写し（`tests/test_metric_words.py::test_afx_ops_words_match_the_daemon_words` で保護）、web baseline と commands も表示 | replay変更は採用判定へ波及。literalから比較処理を再確認する |

上表の `core/`、`store/`、`ops/` 等の省略 path は `src/agentic_fx/` 相対（明示した afx-ops は別）。正式頁では anchor を完全な repo 相対 path に展開する。大きな module の定数や内側関数はその symbol だけを anchor にし、file全体を安易に依存指定しない。

### 3.3 守る契約（確定と未実装を分ける）

| K-ID | 入口×遷移・前提 | 必要な動作と障害時の境界 | 正本 / F / test |
|---|---|---|---|
| KS-LATCH | live 二 caller / replay、clear→latched、DD拒否 | 判定はopen intentのgate時。tick判定へ移さない。文字列producer/部分一致consumerの互換を維持、成立済みstateを通知失敗で戻さない | C4 §10 R1/§2、F01–03、risk/executor test |
| KS-REPEAT | latched→latched | 同世代単独trueで世代を増やさない。拒否と現 activity の繰返しは維持。一件化はC4対象外 | C4 §4.1–2、F02–03 |
| KS-NOTICE | 自動ラッチ/ops/shell/起動/tick | **確定・未実装**。C4の単一lane内sweepがloadの投影stateを既存eventと照合。generation>0/発動時刻でincarnationを限定。latchedなら補完、clearなら既存latchedに対するreleasedだけ補完。released既存なら閉鎖、clear+event無しから作動履歴を捏造しない。三field guard_kind/state/generationとref instanceを維持。時刻下限clamp、通知例外は元mutation/監査結果を変えない | C4 §2/4/10、F06–09、D01。守る test は C4 §9.1 の exact-set/再入/障害受入（§7.2）。二本目は作らない |
| KS-RESET-OPS | approver/local_guard、世代一致、marker無し、latched。APIはautopilot拒否 | marker→保存→削除、世代/発動時刻保持、HWM不変。C4前後sweepは独立の通知try。現状の解除後event前crashではguard欠落/監査outcome_unknown。C4後も前後sweep失敗のままclearとなる履歴欠落は受容 | C4 §4.3–4.7、F04–06/08–09、AC51。全履歴保証へ強めない |
| KS-RESET-SHELL | shell reset、store世代前提 | **直接mutationを維持**。成功/ResetNotApplied後にC4公開wrapperを呼び、通知失敗は隔離。ops未注入testはskip。OpsService mutationへ移さずterminal投稿を増やさない。shellのautopilot非対称を隠さない | C4 §4.4、F07、D02。commands test |
| KS-RECONCILE | ops/shell confirm、markerあり | raw falseなら次世代true、raw trueなら世代維持、保存後marker削除。marker待機中DD update(true)がraw falseを次世代trueにした後のconfirmは世代維持。解除へ倒さない。通知はC4の入口別sweep | F03–07、state/AC51/commands test。rawとloadを混同しない |
| KS-START | 起動時marker/accepted-only | 安全側投影・確認要求、自動解除無し。監査回復はoutcome_unknown/terminalでありguard回復の代用にしない。C4ではlistener/supervisor前のsweepとtick再試行を追加 | C4 §4.4、F08。未実装D01 |
| KS-RESET-UI | reset成功のshell/ctl | DD数値無しのC4 §4.7固定警告を各一回必ず表示。ctlはHWM C2.1のrebase固定案内も表示。API/event field増加無し | C4 §10 R2/R4、HWM §1.4。未実装部分D10 |
| KS-REBASE | 独立した人の明示操作、approver/UDS/local_guardのみ | **確定・未実装**。PaperBroker、TERMINAL補集合ゼロ、新鮮snapshot、DD条件、gen>0、autopilot拒否。閉場は market_closed、新鮮 snapshot だけでは許可しない。preview GET / POST の service method 先頭で Principal.APPROVER を直接検査。HWMだけ変更しlatch/世代/時刻不変。値CAS、ops→core→state shared→DB、ledger+snapshot原子化。硬い累積上限無し/累積警告、実口座対応前に硬い上限等が必須。通常resetに混ぜず、LLM/plugin/bot/web/shellへ入口追加無し、guard/専用push/独自sweep追加無し。既存 ops_request_terminal（endpoint_code=killswitch_rebase）は bot/notify へ流れるが bot は既定で投稿しない | HWM C2.1 §1–2/4–5、F14、D09。configで資金保護を無効化する入口も作らない |
| KS-REPLAY | replay自動解除・補償 | artifact/理由activity/採用metric互換。C4 は StateStore/executor/risk gate/replay/backtest の code 差分無し。HWM は backtest/ と core/{executor,risk_gate,scheduler,accounting}.py の差分0（将来 accounting 除外は HWM §4.4 の条件限定）。HWM の StateStore shared context API 新設と store/db.py migration は許容。backtest → manual rebase module/ledger/source の参照禁止 | C4 §4.2/9.1、HWM §2.4/2.5/4.4、F15–16。live→backtest 禁止を承認済み契約として追加しない |
| GS-CONSUME | bot/notify event配送 | envelope/flatten契約、各cursor、成功後保存、at-least-once。送信後cursor保存前crashの重複を許容。daemon直送Notifierは別経路として保持。C4 後は先頭 event の10分無進捗で rc79、429以外の恒久4xxは rc78、cursor 不変（message 消失等の既存回復例外は C4 §5.2 に従う）。event_stream_health と HWM terminal も consumer の接続点。notify任意、未設定でもliveを妨げず固定警告 | C4 §2/6/10 R5、F02/10–11。afx-ops testは現CI対象外 |
| GS-VIEW | status/activity/afx status | webはstateとactivity表示、event成功を前提にしない。marker中は安全側表示。StateStoreとstatusのmarker投影意味を同じfixtureで確認（symlink差は未確認のまま残す）。bot healthのstatus→webはC4確定/未実装。guard_notice_pending/対象generation を短命 read-only connection で計算し ctl/shell/web へ。alternate_push=none / configured を afx status / afx ops bot status / web bot_health に表示（配送成功・障害独立性の証明ではない） | C4 §10 R3、F12–13、D06/11 |

KS-NOTICE の event 化時点はラッチ後最初に終わるtick末尾（通常およそ60秒+tick処理）、配送完了の時間保証ではない。shell mutationはlane外なので世代間全順序を保証しない。event retentionやstate履歴喪失/時刻大幅後退への完全復元も保証しない。これらはC4の受容境界であり、本地図が保証を拡張しない。

### 3.4 既知の逸脱・文書との差

| D-ID | 差と処置 | 解消条件 / 関連ticket |
|---|---|---|
| D01 | 自動ラッチ・起動のguard回収が無い。KS-NOTICEは提案ではなく確定未達。reset後crashの欠損窓も含める | auto-killswitch-latch-emits-no-event。C4着地/試験でF更新、受容欠損窓は残す |
| D02 | shell reset/confirmがlane外、現状通知wrapperも無し | 同ticket。C4でwrapper不足だけ解消。lane外mutationは承認済み残余リスクとして残し、例外をゼロにするための移管を要求しない |
| D03 | state no-opとactivity繰返しが非対称 | killswitch-latched-activity-repeats（承認済みspecの分割先名。現主checkoutの公開ticketは未収載）。C4で一件化しない。独立oracleに現挙動を記録 |
| D04 | trade-notices設計のguardはresetだけという説明が古い | 現F06–07を正本にし、元docs修正は該当ticketで扱う |
| D05 | bot/runtimeの非零終了説明とsend再試行の範囲差、health不足 | bot-gateway-disconnect-is-silent、C4で是正予定。watchdog実装済みとしない |
| D06 | webをguard consumerとする説明は誤り。ACTIVITY_FIELDS定義は描画契約の証拠でない。既存ticketの「reset/reconcileだけ」もdata resumeを落とす | F07/12と公開specに基づき正す。ticketそのものは本作業で変更しない |
| D07 | Commands._data_resumeがlane/guard eventを迂回 | 是正 ticket は未起票。datafeed-outage-discord-notice を代わりの解消 ticket にしない。限定例外で新規迂回を止める |
| D08 | data_feed停止そのもののguard event無し、resume_requestedだけ | 公開 ticket datafeed-outage-discord-notice（設計待ち）の既知欠陥に結ぶ。D07 の迂回是正とは別件 |
| D09 | 承認済みlive rebaseの入口/ledger/排他/CASが未実装 | killswitch-reset-hwm-rebase、HWM C2.1着地で更新 |
| D10 | reset固定警告/ctl rebase案内が現コードに無い | C4とHWMの担当commitで更新、適用順を明記 |
| D11 | afx statusとStateStoreのmarker path解決が異なる | 親dir/lock無し/例外/既定値とsymlinkの合成fixtureで比較。運用上顕在化は未確認、勝手に統一済みとしない |
| D12 | DD理由文字列が暗黙protocol、定数/enum連携無し | KS-LATCH挙動testと既存pinで保護。型付き理由への是正は別ticket（未起票） |
| D13 | service.run_initのmodeコメントにあるmodeコマンドが現入口に無い | 実行時mode/autopilot切替予定は未確認。架空の入口を地図に足さない |

### 3.5 手で保守する辺と C4 追加契約

G_reach は索引に発見を期待せず、頁で人が持つ。最低限 F02 の live 二 caller→open_from_snapshot、replay→handle_intent→_open、Commands._data_resume→outage.request_resume（lane/event 迂回）、build_app の data_resume callback 注入→OpsService.resume_data→guard helper、reset/reconcile→C4 sweep を ID 付きの辺にする。始点/終点、resource、F/K/D 根拠を持たせる。literal がある caller でも当該 resource への辺が索引で結べなければ、この手動辺で補う。一般 call graph を生成する約束ではない。

| K-ID | 確定契約・状態（C4 前の観測） | 守る test / 接続 |
|---|---|---|
| GS-LANE | 全 OpsService lane 経路で同一 instance/thread 再入は取得前に guard_lane_reentry を即時送出、所有解除と通常競合を区別。通知境界は元 mutation/監査を保つ。確定・未実装 | C4 §4.3/9.1。ops 領域へ接続、D14 |
| KS-NOTICE-CODES | guard_notice_deferred / guard_notice_backfill_skipped は固定 code。EventStore.emit の not_before で時刻下限を clamp。確定・未実装 | C4 §4.3–4.4/9.1、F09、D15 |
| GS-PENDING | guard_notice_pending/対象generation は短命 read-only connection で算出。status は観測で取引判断に使わない。確定・未実装 | C4 §4.6/9.1、F12、D16 |
| GS-ALTERNATE | alternate_push の二値と三表示面、未設定でもlive継続。確定・未実装 | C4 §6/9.3、F12–13、D17 |

D14=共通lane再入拒否不足、D15=固定code/時刻下限不足、D16=pending投影不足、D17=alternate表示不足として、C4前の差を記録する。いずれも C4 の担当 ticket と受入に結び、merge 後は実コードと test 結果で F/D を置換する。「確定・未実装」を恒久ラベルにしない。

## 4. 正確さの測定 — 機械セルと頁セル

### 4.1 独立正解表と隔離

1. C4 merge 後の同一 revision、境界、種類、採点粒度を先に固定する。A/B に渡すのは source/test と境界・五項目形式のみ。B へ C1/C2/R2、A の表、抽出結果、影響先リストを渡さない。
2. B は repo 外の scratch（例: `/tmp/impact-map-oracle-b-<run>`）にその revision の detached worktree を作る。`tmp/` と `docs/impact-map/` を持たないことを開始前に列挙検査する。頁収載済み revision なら sparse checkout で当該 path を除外し、除外状態・revision を記録する。sparse の除外が初期化時から効くようにし、初読前に不要な working file が無いことを確認する。git object には存在し得るため意図的な探索へのセキュリティ境界ではないが、通常の初読材料から物理的に除く。B の結果は外部 scratch に保存し、A の表を見せる前に確定する。親セッション履歴も渡さない。
3. A/B が各々、入口から順方向と resource 逆引きで表を作る。行は `symbol（path込み）, relation, resource, critical, rationale` の五項目。非literal辺の終点は relation に、証拠は rationale に記す。header に役割・revision・領域を持つ。G_lit/G_reach は裁定時に source から分類する。単独再読は独立二者と数えず暫定にする。本作業は表の独立作成を実施していない。
4. 指揮者が両表の不一致を source/test で裁き、五項目の表と裁定結果・digest を凍結して公開 commit する。出力を見て oracle を変える場合は理由/旧新digest/役割/旧scoreを残して再測定する。既知レビューの転記を B と呼ばない。
5. 製品レビューも同様に map/tmp 無しの隔離 checkout で初読する。第一段は地図を除く code/test 差分と要求、第二段は保存済み独立所見と頁を照合し、同 commit の頁差分もレビューする。headless command の pathspec 対応を前提にせず、指揮者が除外済み patch を用意しそれを読めるレビュアーで初読する。通常の全差分レビューは第二段。地図を見た所見を独立初読と報告しない。

存在しない event は positive にしない。negative assertion（入口に event 無し）、K 未達、禁止経路は別欄。ただし実在する迂回 edge は positive に含める。

### 4.2 母数・採点単位

- **G_lit**: 宣言 literal を持つ site の `(resource, path::symbol)` 集合。重複siteはまとめる。機械セル literal-resource-symbol はこれを母数にする。
- **G_reach**: 当該 literal を持たない入口/caller/注入元とその辺。literal が別resourceで一致しても、対象resourceへの非literal辺は手動採点に残す（§3.5）。
- **機械セル**は G_lit と抽出集合 E を比較。**頁セル**は G_lit ∪ G_reach と人が書いた F/K/D の被覆を比較する。頁は symbol の名があるだけでは TP にせず、入口→副作用/不足・契約・根拠が該当行に正しく表現されているか人が裁く。

critical は資金保護 writer/marker、live/replay 全入口、guard producer、bot/notify/schema、口座入力・比較 metric を含み、A/B 作成前に範囲を固定する。各セルで母数/TP/FP/FN/未解決U/未走査と critical 内訳を記す。U はsite+reasonでまとめ、oracle positive なら FN から除外しない。空の母数は unsupported、fixture 合格を実領域合格にしない。P/R は補助値のみ。

期待項目ごとに **索引で発見 / 頁の手動辺で補完 / 両方で漏れ** の三分類を記す。両方にある項目は索引発見として頁根拠も併記する。機械で発見しても頁が欠ければ頁 FN は残る。semantic reader/writer・型推論の自動精度に読み替えない。

### 4.3 初回の合否と次段の合否

初回 T1 は機械セルの G_lit critical FN=0/U=0、頁セルの G_lit ∪ G_reach critical FN=0/U=0、誤った direct 断定=0、未裁定U=0 を要件にする。非critical FN も全件列挙・理由/手読みによる処置を確定する。**FP は critical/非critical とも件数と仕分け時間だけを記録し、ゼロを合格条件にしない。** state field と activity code の `kill_switch_latched` は名前空間が異なり、literal 衝突の候補両出しを誤って不合格にしない。match_kind 制限は必要と判明してから追加し、曖昧なものは resource 候補集合として残す。

加えて、C4 前の `558cb9fd6108a5829f05854e6a701691b21f5efc` を比較用に source だけ読み、(a)自動ラッチの event 欠落、(b)shell の lane 迂回（reset/reconcile/data resume）、(c)docstring と実動作の範囲差を、頁+索引から表に出せることを初回受入にする。a/b/c はすべて必須で (どれか一つでも出せなければ初回は不合格)、それぞれ入力→mutation→event/consumer の有無、(c) は docstring の主張と実動作の根拠行を示し、C4 後の解消部分と残余リスクを区別する。これは **履歴再現 fixture** であり、正式 oracle の基準を C4 前へ戻さない。本番の時刻/event 行/金額は使わない。

通常チケット二件は初回 T1 の完了条件に混ぜない。C4 merge 差分を通常チケット一件目として merge 後に遡及確認し、HWM 実装を二件目として運用確認する（§6.4）。後続の鮮度/禁止検査と二件運用を揃えてから領域拡張する。全セルは現在未測定。新領域・新抽出種類は独立 oracle/未使用fixture/holdout が揃うまで unsupported。

## 5. 鮮度・affected・check（初回外、T2 で閉じる要件）

### 5.1 記録・粒度・更新者

T2 は `docs/impact-map/verification/<domain>.json` を領域別に持ち、単一 verification.json の merge 衝突を避ける。anchor、正規化 AST hash、hash方式version、照合日、根拠test/結果、確認時source SHA（来歴）、必要な依存digestを記録する。literal 値は hash 入力に使うが保存は digest のみ。hash方式version は interpreter minor、`ast.dump` の明示引数（位置情報除外等）、正規化規則を含め、方式不一致は fresh にしない。異版で偶然 hash が一致したことを互換保証にしない。

主粒度は qualified symbol。module 定数/schema/helper は別 anchor で依存宣言し、関数 hash で外部依存を隠さない。300 行超の粗い symbol は、callee 名+宣言 literal の組で一意に特定できる statement の部分木を副 anchor にし、その hash を使う。曖昧なら固定式等を追加し、なお特定不能なら unresolved として人が読む。粗い全関数 hash の変化を無条件に全辺の変更としない。非 Python / 意味のあるコメントは明示 block/file の別方式に限定する。

記録操作を CLI に設ける（未実装）:

```bash
.venv/bin/python scripts/impact_map.py verify --page kill-switch --anchor <anchor>
.venv/bin/python scripts/impact_map.py verify --page kill-switch --anchor <anchor> --record --expected-hash <displayed-hash> --reason <public-reason> --evidence <test-or-ticket-id>
```

前者は現在 hash・記録 hash・差だけを表示し無変更。後者は **指揮者が source/test と照合後**に理由と根拠を渡した時だけ記録し、再計算値が expected-hash と違えば中断する。自動 fresh 化・手で digest を転記する運用にしない。理由は公開可能な固定説明に限る。

「契約が変わる時だけ頁を更新」の裁定に従い、関係不変なら頁本文/宣言を触らない。指揮者が閉鎖手順で不変の理由と検証結果を確認し、**照合記録だけを更新する案を採用**する。内容を再確認した記録は契約の改訂ではなく、本文まで毎回編集すると更新裁定を崩し機械的な差分を増やすため。実装者へ追加解析や記録更新を課さない。関係が変わる時は code と頁を同じ commit に含め、自己 SHA は埋めず完成 source の hash を記録する。

### 5.2 affected/check と終了コード

`affected --base <base> --head <head> --worktree` は base/current の symbol 差、literal候補差、旧新宣言依存を合わせて候補頁を列挙する。schema/helper/rule変更は依存頁へ伝播。新resource・未所属runtime file・依存削除は unmapped とし指揮者が裁く。path 一致は探索候補であり確定 stale ではない。設計段は予定 symbol で手動逆引き、affected は閉鎖時に使う。

check は正常 exit0、**stale（記録 hash 差）・根拠消失・必須違反は exit1**、解析失敗/宣言不正/方式不整合/履歴取得不能等は exit2 とする。base 不在で影響無しにせず、鮮度不明/閉鎖未完とする。stale は pytest に入れず指揮者の閉鎖必須項目にする。鮮度関連で pytest の赤にするのは根拠消失だけ（hash mismatch を test failure にしない）。schema/link/公開規則・決定性・禁止 scanner 等の別検査は root tests に置ける。古い index を今回の成功として流用しない。

行移動/同 file の別 symbol 変更は stale にせず、symbol 改名/削除は根拠消失として検出する。G_reach の手動辺の始点/終点も anchor 検査に含める。T2 の完了には上記の粒度・消失・依存変更・新resource・終了コード・記録競合を fixture で検証する。製品の起動/取引に地図や index の存在を要求しない。

### 5.3 local merge と CI

主経路は指揮者の閉鎖判定。root pytest と必要な afx-ops test、T2 後は check/affected 裁定を判定束に含める。私的 judge wrapper は pytest 用であり索引を自動実行する保証はない。README に interpreter 指定の同等コマンドを記し、wrapper を公開手順の必須依存にしない。sandbox の uv/rg/履歴/commit 能力を前提にしない。

C1/R2 観測の CI は root tests のみで **afx-ops/tests は未収載**。ただし display_time の byte 一致と metric_words の語一致は root tests で保護される例外。C4/HWM 着地後に構成を再確認する。spec が所有する受入を重ねて実装しない。CI artifact/summary の閲覧や PR を local merge の完了条件にしない。将来 CI で差分を使うなら checkout depth/base 取得を検証する。

## 6. 工程へ組み込む文言案

### 6.1 設計 C0 — 指揮者

頁、literal 逆引き、触る予定の symbol から手で全入口×副作用を照合する。まだ head 差分は無いので affected は使わない。未対応種類/critical 未解決は source を読み裁定する。F/守るK/変えるKを分け、頁改訂の要否と理由を台帳へ残す。

### 6.2 実装 brief

二行を基本にする（指揮者の c1-brief 方向づけによる）。

```text
守る契約: K-ID と該当行の内容・合成test要件
禁止経路: 該当するcallee/入口/例外境界と所有specのtest
```

実装者は brief の K を守り、与えられた test の結果/未検証を報告する。頁ID/未解決の台帳、頁差分の作成、抽出、照合、記録、commit は指揮者が担当する。実装者に頁差分を作らせるための第三行/全頁の再読を追加しない。

### 6.3 review-process 差込み案

1. 設計段で予定 symbol の逆引き、閉鎖段で差分候補（T2 後は affected）を全件裁き、brief へ K/禁止経路を入れたか。
2. 変わる契約の頁差分が code と同じ commit にあるか。関係不変なら本文改訂無しの理由、T2 後は指揮者の照合記録があるか。
3. map/tmp を除く隔離 checkout / patch で独立初読を保存し、その後に頁差分を含めて整合レビューしたか（§4.1）。

既存レビュー周回/担当配置を変えない。今回は工程ファイル/memory 自体を編集しない。

### 6.4 閉鎖と二件運用

初回 T1 の閉鎖は C4 merge 後の凍結 A/B、二セルの採点、三分類、起票動機の履歴再現、六本程度の test、速度一回の記録で行う。hash/check/affected があるふりをせず、根拠・全候補・公開入力規則は手読みで確認する。

T2 以降の通常閉鎖では指揮者が、(a)新 tree の extract/check と **stale 全件の source 再照合・verify 記録**、(b)affected/unmapped 全件裁定、(c)root/必要 afx-ops test、(d)F/K/D と例外、(e)契約変更時の code/頁同 commit を確認する。stale の赤を実装者へ差し戻すだけで終えない。

運用確認一件目は **C4 merge の親→merge 結果の差分**。oracle は merge 後で凍結し、merge 前から作り直さない。後から読み直して変更前の F06/F07/F09/D と変更後の頁を照合し、必要な頁更新・候補裁定・費用を記録する。初回頁が merge 後に導入されるため同 commit 更新の実地試験とは数えず、**遡及運用確認**と表示する。基準revisionと一件目を混同しない。

二件目は HWM 実装の実差分。関係を変える code/頁が実際に同 commit で更新され、reader/consumer への影響と T2 の閉鎖束が実行されたことを確認する。未着地/未実行なら未完。二件の欠陥発見数・見落とし・仕分け/更新費用を残してから拡張を判断する。extractor/oracle/rules を変えた時は独立表・未使用fixture/holdout を再測定し、通常の製品変更で全域再採点を強制しない。

### 6.5 CLAUDE.md 追加案（二行）

```text
- 指揮者は設計C0時に docs/impact-map/ の頁・予定symbolの逆引きとsourceを照合し、briefへ該当K行と禁止経路を渡す。
- 契約・責務・影響関係が変わるticketだけ該当頁をcodeと同じcommitで更新し、閉鎖時にREADMEの判定束と差分候補裁定を確認する。
```

## 7. test と禁止経路の所有

### 7.1 既存慣行

地図固有 scanner は既存 AST 配線 test の qualified function 識別、限定 ALLOWED、不要例外検知、合成変異に合わせる。source 部分文字列の pin だけで挙動を保証しない。資金保護・排他・crash の動作 test は所属 spec が所有する。

### 7.2 禁止経路と守る test

| 検査/契約 | 持ち主の test / 地図の処置 |
|---|---|
| Commands の新規 mutator 直呼び（reset/confirm/guard update/save、outage.request_resume） | 地図固有 D02/D07 の一本だけを T2 で追加。shell reset/confirm の既存直mutationは C4 が維持するので許容。wrapper 不足は C4 test で守る。dict update/init を一括禁止しない |
| kill-switch INSERT の sweep 一元化・別接続禁止・lane再入拒否 | **C4 §9.1 所有**。着地後の test 名へ link。作業中候補 `tests/ops/test_guard_sweep.py::test_only_the_sweep_helper_inserts_kill_switch_events` / `test_scan_flags_other_connections_and_improve_supervisor_but_not_data_feed` は merge 後に所在を確定。地図には二本目を作らない |
| guard 三field/ref、producer/bot/notify exact-set、通知例外、not_before、pending | **C4 §9.1 所有**。根拠 test 名は merge 後に確認。未確定名を存在済み扱いしない |
| backtest → manual rebase module/ledger/source の参照禁止、手動入口の到達制限、差分0集合 | **HWM §4.3/4.4 所有**。backtest から手動機能への向き。live→backtest という別の禁止を地図で足さない。StateStore API/migration を妨げない |
| KS-LATCH 理由文字列 protocol | 既存 risk/executor 挙動 test と e2e pin を参照。地図所有の文言変異/負例追加は T2。DD拒否で実ラッチ、latched理由だけで新世代無し。C4/HWM の scan を複製しない |
| G_reach 手動辺の anchor 消失 | 地図 T2 の根拠検査。始点/終点の存在確認であり call graph の意味証明ではない |

地図固有例外は T2 で初めて `deviations.json` に置く。key は `(path, qualified_symbol, callee_method)`、同じkeyに複数siteがある時だけ固定式を追加。D-ID/理由/解消ticket（未起票ならそう明記）/承認境界を持ち、個数だけの許容・ディレクトリ丸ごと除外は禁止。不要/消失例外を fail にする。spec 側例外台帳をコピーしない。

### 7.3 初回の六本程度と後続

初回は以下の六群を test にする（細かな parametrize は可）。

1. source を import/実行せず、string/attribute/keyword を包む symbol へ採る。
2. 同一入力の sorted JSON が決定的。
3. root 外/symlink/非公開入力を読まない。
4. 同名異型・state/activity 同 literal を断定せず候補として出し FP を数える。
5. 母数ゼロを unsupported とし合格にしない。
6. critical FN/U と頁の手動補完/両方の漏れを正しく計上する。

初回の頁意味採点と起票動機再現は §4 の受入記録で行い、六本で全文意味が自動検証できるとはしない。コメント層、移動/改名/削除/定数依存/新resource、hash方式版/verify記録/stale終了コード、scanner変異、速度反復は T2 以降。marker/raw/confirm、status独立分岐、mutation/event/audit crash の既存受入は参照し、不足する領域 test は所有 ticket に分けて補う。今回 test は実行していない。

## 8. 拡張順と追加測定の場所

試行の合格はセル単位であり、次領域を読む資格であってその領域の未測定規則の承認ではない。

| 順 | 領域と測定する種類 | 次へ進む条件 |
|---|---|---|
| 0 | kill switch/guard縦断。literal-resource-symbol、入口/state/event/境界の人手K、文字列protocolを専用testで測る | §4 の機械/頁二セル合格・起票動機再現、T2 の検査、C4/HWM 二件運用。semantic 抽出は実装/測定分だけ |
| 1 | 注文 lifecycle/risk gate/trade event。理由文字列protocol、引数経由risk config、注文/fill SQL | protocolのproducer/consumer oracleと挙動変異、config型連鎖の独立oracle。試行の一keyだけで型chain合格にしない |
| 2 | ops認可/監査/plugin deploy・approval・journal復旧。action callback注入とlane、event wrapper | callbackの注入元→呼出先→副作用、未使用alias/循環fixture、復旧の実source oracle |
| 3 | ingest/economic guard/admission/account snapshot。動的SQL/CTE/識別子引数、Settings型連鎖 | 当該構文が実在するsiteをsourceで選定（所在一覧は未確認）、SQL/config各セルを個別採点。母数0ならunsupported継続 |
| 4 | improve/backtest/sandbox/plugin互換。callback多段・artifact・文字列metric protocol | 各chain/protocolのoracleとholdout。既存規則の再利用だけで新領域合格にしない |
| 5 | setup/migration/systemd/残りCLI/web/report。file/config/process境界 | 非Python入力の別手法を明示し測る。危険なmigration等は順を待たず専用頁を作れるが保証を流用しない |

動的SQL・callback・config型連鎖を試行に存在するふりで測らない。実sourceが少ない場合は合成fixtureの合格だけ記録し、領域実測はunsupportedのままにする。共通schemaは一つの定義頁へ集約する。

## 9. 決定事項・未確認

新しい利用者裁定は求めない。小さい初回、C4 後の基準、頁と機械の別採点、FP の記録限定、頁差分は指揮者が作成、隔離初読→頁レビュー、領域別照合記録は本版で手順として決めた。承認時に `code-impact-map` ticket の「処置案・裁定」へ、初版は writer/reader の機械分類をしないこと、最終検証commitではなく後続taskの内容hashが鮮度基準であることを記録する（本文との範囲差の記録。今回は未更新）。

未確認: C4 merge SHA/最終実装/test 名と結果、HWM 実装時期、独立 A/B、精度/速度/仕分け費用、初回規模の推定、status と StateStore の全分岐差、ops reset crash 統合testの網、mode/autopilot切替予定、全域動的構文、sandbox の履歴読取、CI構成の着地後再確認。Y-9/Y-10 の文書不整合は 45626974 で修正済みだが C4 実装 merge 完了の証拠ではない。実通知・実daemon・本番データ・個人設定を調査していない。未測定を合格に置換しない。

## 10. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-14 | C0 | 人手頁・大型抽出案・oracle・工程を提示 | 現状の独立正本を設ける初案 | 未commit、観測558cb9fd |
| 2026-11-14 | C1 | 小索引先行、種類×領域/件数判定、symbol内容鮮度、local閉鎖、試行F/K/D補完、承認済みC4/HWM反映 | r1の漏れ・工程負担・保証の過大一般化を是正。確定裁定を維持 | 未commit、HEAD6915548b。製品コード差分無し確認 |
| 2026-11-14 | C2 | 初回縮小、機械/頁二セル、C4後基準と履歴再現、spec所有testへ一本化、隔離oracle/review、鮮度の後送と閉鎖手順 | R2 Critical1/Important8/Minor16・Y-1〜15を裁定し、literal索引の構造的限界と実装負担を是正 | 未commit（本書のみ）。文書照合HEAD45626974 |
| 2026-11-14 | C2.1 | §4.3 の履歴再現を a/b/c すべて必須に。付録 A I-7 を履歴表現と明示 | terra の C2 閉鎖確認 Critical 1 (三型の再現が必須でない) / Minor 1 | 未 commit |


## 11. 短い task 表（将来の実装）

| ID | 成果物・順序 | 完了条件 |
|---|---|---|
| T0 | C4 merge までは頁雛形と手順だけ準備 | 公開規則/独立作成/所有specの照合。oracleを凍結しない |
| T1 初回 | C4 merge後、試行頁 + 正解表A/B + extract/evaluate + 約六本test | §4 の二セル・三分類・履歴再現、一回速度計測。管理JSON/hash/affected/checkは作らない |
| T2 | T1後、鮮度/verify/affected/check、領域別記録、地図固有検査 | §5/7 の記録操作・終了コード・粗いsymbol・依存/消失/例外と閉鎖束を fixture で確認。性能反復はここで実測 |
| T3 | C4差分の遡及確認 + HWMの実運用確認 | §6.4。二件を区別して記録、HWMのcode/頁同commit、見落としと費用を裁く。必要規則だけ独立oracle/holdout付きで追加 |
| T4 | 領域拡張 | §8 の種類×領域を個別測定。未測定はunsupported |

## 付録 A. r1 採否の継承と補正

本付録は C1 時点の採否履歴。参照節と方式は C2 本文・付録 C/D が優先する。特に二セル、task 分離、KS-REPLAY、FP、地図固有検査の所有、レビュー隔離は C2 が置換する。採否は設計への処置であり、製品実装・測定の完了を意味しない。レビューの前提を承認済みspecで補正した箇所も理由を明記する。

| ID | 採否 | 理由・反映先 |
|---|---|---|
| C-1 | 採用 | §2/4/8。種類×領域、未測定unsupported、件数閾値、文字列protocol専用K/test。試行合格を全域性能へ一般化しない |
| I-1 | 採用 | §2/11。小literal索引→測定→必要規則だけ追加。callback/型/文字列結合の限界を明記 |
| I-2 | 採用 | §4.1。Bへの材料を限定、五項目oracle。単独再読は独立二者に数えない |
| I-3 | 採用 | F02/05–06、KS-LATCH/RESET。ctl/API/router、open二入口、autopilotとshell非対称 |
| I-4 | 採用（保証を限定） | F13/GS-VIEW/D11。独立marker投影と比較fixture。全条件で一致済みというKにはせずsymlink差は未確認 |
| I-5 | 採用（C4で補正） | F06/08、KS-RESET/START/NOTICE。terminal consumerとcrash窓を追加。C4後も全履歴保証へ強めない |
| I-6 | 採用 | F02/10–12/16。daemon直送、activity表示、ctl/shell/splash、採用metricのconsumerを補完 |
| I-7 | 採用（承認済み設計へ更新） | F14、KS-REBASE/REPLAY、related。C1 時点の旧表現「live rebase を許可し backtest helper 流用だけ禁止」は履歴。規範は本文 KS-REPLAY (承認済み HWM と同じ backtest → 手動 rebase の参照禁止) |
| I-8 | 採用 | F07/D07、§7。data resume直呼びも限定例外とし新規迂回を検査 |
| I-9 | 採用（briefを二行へ限定） | §2.5/6。指揮者が抽出/照合/commit。指揮者の c1-brief 方向づけに従い頁ID要確認は指揮者台帳に置き、実装者にはKと禁止経路だけ |
| I-10 | 採用 | §6.3を三項目に分割。独立初読後に指揮者が地図と照合 |
| I-11 | 採用 | §5。symbol hashが鮮度基準、SHAは来歴のみ。追いcommit不要、本文不変の照合記録は別管理 |
| I-12 | 採用 | §5.3/6.4。local閉鎖を主経路にしroot testsへhard fail。afx-ops/testsのCI未収載を明記 |
| I-13 | 採用 | §1/2/3/5。path::symbol主key、lineは参考 |
| I-14 | 採用（根拠を補正） | §1.2/3。memoryではなく承認済みC4 §10/HWMを正本にKS-NOTICE確定。ticket転記待ちを契約成立の条件にしない |
| M-1 | 採用 | F06/11、§7.1。NotifyClient.events/610、_mutate/868・action/884、変異test/303に補正しsymbolへ移行 |
| M-2 | 採用 | F09。INSERT障害隔離と事前validate伝播を分離 |
| M-3 | 採用 | F03、KS-RECONCILE、§7.3。marker中update→confirmのraw/世代動作を追加 |
| M-4 | 採用 | F12/D06。web ACTIVITY_FIELDSは描画証拠にならないとしsourceを根拠にする |
| M-5 | 採用 | F07。reset activity builderのpackage二重実装を追加 |
| M-6 | 採用 | 正式頁は公開spec/ticketのみ参照。C4の公開収載をT1条件とし私的レビュー/作業pathを正本の必須依存にしない。役割A/Bで記録 |
| M-7 | 対象外（指揮者が別途処理） | 既存ticketの運用値の除去は本書の編集範囲外。値は転記しない。新規頁の公開規則は§2.1 |
| M-8 | 採用（C4境界で補正） | §7。既存AST配線test/限定ALLOWED/不要例外検知に合わせ、tests/scriptsへ配置。shell例外をC4で必ずゼロにする案は採らない |
| M-9 | 採用 | §2/5。front matter宣言のみ正本、registry生成物、照合hashはverificationに分離 |
| M-10 | 採用 | F07/D08。停止guard無し/resume_requestedのみ。契約違反かは未確認 |


## 付録 B. C1 までの食い違い X-1〜X-13（履歴、C2 の規範は本文と付録D）

| ID | 判定とC1の処置 |
|---|---|
| X-1 | 採用。guardはresetだけでなくreconcile/data resumeにも存在。F06–07/D04 |
| X-2 | 採用。runtimeコメントとsend再試行を別範囲としてF10/D05へ。C4是正は未着地 |
| X-3 | 採用。webはstatus/activity、data resumeもguard発行。F07/12/D06。元ticketは変更しない |
| X-4 | 採用。notify transportはNotifyClient.events（610）。F11 |
| X-5 | 採用。_mutateとactionの誤った行を868/884に補正、symbolを主にする。F06 |
| X-6 | 採用。変異test参考行303に訂正。§7.1 |
| X-7 | 採用。root testpathsだけでafx-ops/testsはCI対象外。§5.3の閉鎖判定とroot配置へ |
| X-8 | 採用。過重な例外keyをpath/qualname/calleeへ縮小、必要時だけsite識別。§7.2 |
| X-9 | 採用（未確認を維持）。modeコマンドを仮定せず、古いコメントと実入口不在をD13へ |
| X-10 | 採用。ACTIVITY_FIELDSの存在をweb event消費の証拠にしない。F12/D06 |
| X-11 | 採用。validateはtry前、INSERTのbest effortとは別。F09 |
| X-12 | 採用（根拠更新）。KS-NOTICEは承認済みC4正本による確定契約・未実装。§1.2/3.3 |
| X-13 | 採用。rg必須を廃止、標準ライブラリでliteral照合。§2.3/2.5 |

全面却下はない。I-4の無条件一致、I-5の現窓の一般化、I-7の未承認扱い、I-9のbrief第三行、I-14のmemory由来、shell移管/例外ゼロ化は、上表の理由と確定済み利用者指示・C4/HWMの契約に従って補正した。

## 付録 C. R2 指摘ごとの採否と理由

採用は設計への反映であり、実装・独立採点・運用確認の完了ではない。全面却下はない。選ばなかった代案・延期範囲も以下に記す。

| ID | 採否 | 理由・反映先（却下/補正/延期部分を含む） |
|---|---|---|
| R2-C-1 | 採用 | §1/3.5/4。G_lit/G_reach、機械/頁二セル、三分類、動機の履歴再現。literal無しを無関係とせず頁で人が持つ |
| R2-I-1 | 採用（時機を補正） | §1/4/6.4/T0–3。利用者方向づけに従い C4 merge後の正解表を凍結し、C4差分を一件目として遡及確認。旧revisionの正式oracle案は二重作成になるため不採用。HWMを二件目。遡及確認を同commit更新の実績に水増ししない |
| R2-I-2 | 採用 | KS-REPLAY/§7.2。C4/HWM別々の差分0集合、store API/migration許容、backtest→手動rebase禁止。根拠のないlive→backtest禁止は削除し、別の未承認Kとしても残さない |
| R2-I-3 | 採用 | §3.3/3.5、D14–17。pending/generation/短命read-only、alternate二値と三表示面、共通lane再入拒否、固定code/not_before。C4前の未実装として記載しmerge後再観測 |
| R2-I-4 | 採用 | §7.2。C4/HWM所有scan・schema testを参照のみとし複製しない。地図所有はD02/D07一本、文字列protocolの不足、手動辺anchor消失。着地前の例外切替実装は不要 |
| R2-I-5 | 採用（レビュー案a） | §4.1/6.3。repo外detached worktreeにmap/tmpを持たせず独立Bを確定。reviewはmap除外patchの初読→頁差分込みの第二段。pathspec未確認のheadlessに依存しない。案bの独立性緩和は採らない |
| R2-I-6 | 採用（T2へ延期） | §5。verify表示/明示記録とexpected-hash、stale=check exit1でpytest対象外、根拠消失は赤、300行超の副anchor、領域別記録。関係不変の記録は指揮者が閉鎖時更新し本文は触らない。裁定を再確認する質問は不要 |
| R2-I-7 | 採用 | §1/2/7.3/11。初回を頁+独立A/B+extract/evaluate+六群testへ縮小。hash/affected/check/管理JSON/tokenize/digest/反復性能/変異はT2以降。行数/日数は推定と表示 |
| R2-I-8 | 採用 | §4.3/7.3。state/activity衝突のFPは件数/仕分け時間のみ。match_kind制限は必要な時だけ後送。FP0という達成不能な閾値を削除 |
| R2-M-1 | 採用 | F02/KS-LATCH/§3.5。live二caller→open_from_snapshot、replay→handle_intent→_open→共有末尾を分離 |
| R2-M-2 | 採用 | F07/F16/§5.3。正本+写しとrootのbyte/語一致testを記載、afx-ops未保護の例外を明示 |
| R2-M-3 | 採用 | F14/§2.2。addをlive writerに数えず、accounting/scheduler/summary等のreaderとmanual_ks_rebaseの可視性、ledger/source resourceを追加 |
| R2-M-4 | 採用 | F16。backtest/metrics.pyのproducerを追加して比較/表示consumerへ接続 |
| R2-M-5 | 採用 | D08をdatafeed-outage-discord-noticeへ結ぶ。D07は別件・未起票とし混同しない |
| R2-M-6 | 採用 | F13/D11/§7.3。親dir/lock/例外と既定値を合成fixtureの比較対象へ。StateStoreとの差は未確認 |
| R2-M-7 | 採用 | §2.1。生成registryをdocsからtmp/impact-mapへ移し初回は不要。非commitという宣言だけに頼らない |
| R2-M-8 | 採用 | §2.2。relatedはticket ID、open/closed双方で解決。移動を根拠消失扱いしない |
| R2-M-9 | 採用 | §1/6.1。設計段は予定symbol逆引き、affectedは閉鎖時だけ |
| R2-M-10 | 採用 | §6.2/付録A。二行briefの出典を指揮者c1-briefに訂正しr1 M-7行を補完 |
| R2-M-11 | 採用（T2へ延期） | §5.1。hash方式versionにinterpreter minor/ast.dump引数/規則を含める。観測した異版一致を一般化しない |
| R2-M-12 | 採用 | F09/GS-CONSUME。emit_best_effortの失敗集計→event_stream_healthを別event経路として追加 |
| R2-M-13 | 採用 | KS-REBASE/GS-CONSUME。market_closed、preview GET/POSTのPrincipal.APPROVER直接検査、terminal endpoint_code=killswitch_rebaseのbot/notify流通と既定投稿無し |
| R2-M-14 | 採用 | GS-CONSUME/§7.2。先頭event10分無進捗rc79・恒久4xx rc78・cursor不変、細部はC4 §5.2の回復例外を維持 |
| R2-M-15 | 採用 | §9。承認時にticket裁定欄へwriter/reader分類と最終検証commitの範囲縮小を記録する。今回はticketを編集しない |
| R2-M-16 | 採用 | §2.1 README規則。既存spec/ticketの本番時刻/event行/金額/閾値等の運用観測を転記せず合成fixtureを使う |

R2 §7 の brief/頁差分の未定義は §6.2 で「指揮者が頁差分を作る」と決めた。R2 §8 の私的link禁止は §2.1 の初回手動確認とT2 checkに入れた。R2 §10 の未確認は §9 に継承し、本作業で確認できなかった実装・測定を補完済みとしない。

## 付録 D. 食い違い Y-1〜Y-15 の裁定

| ID | 採否・理由 | C2 の処置/未完部分 |
|---|---|---|
| Y-1 | 採用。live/replayの入口混同 | F02/KS-LATCH/§3.5を修正 |
| Y-2 | 採用。写しを独立二重実装と呼ぶのは不正確 | F07/F16に一致testを明記 |
| Y-3 | 採用。addにsrc caller無し | F14のlive writerから除外しreaderを追加 |
| Y-4 | 採用。store変更はHWM承認範囲 | KS-REPLAYでspec別差分0集合と許容API/migrationを分離 |
| Y-5 | 採用。禁止の向きが逆 | §7.2をbacktest→手動rebaseへ修正。live→backtest禁止のK化は却下（承認根拠無し） |
| Y-6 | 採用。既知欠陥の公開ticketあり | D08へ結ぶ。D07と分離 |
| Y-7 | 採用。docsの生成物はgitignore保証無し | 初回registry無し、後続もtmpへ |
| Y-8 | 採用。二行briefは指揮者の方向づけ | §6.2/付録Aの出典訂正 |
| Y-9 | 採用・別途修正済み | 45626974のspec変更を確認。main未収載のC4を収載済みと書かない旨へ修正済み。正式頁/oracleはC4 merge後。specを本作業で再編集しない |
| Y-10 | 採用・別途修正済み | 同commitでHWM表題/収載状況/commit欄を実態へ修正済み。C2.1を正本とし新たな修正は不要 |
| Y-11 | 採用。emit/emit_best_effortは全例外を無害化しない | F09とD18（下記）でdocstringとの差を保持。コード修正は今回範囲外 |
| Y-12 | 採用。ticketの処置案と初版範囲が相違 | §9に承認時のticket裁定欄更新内容。今回は未更新 |
| Y-13 | 採用・継続記録 | D04–06/D13と付録BにX-1/2/3/9/10を残す。既存docs/comment/ticketの修正完了とはしない |
| Y-14 | 採用。ticket状態はbranch差 | C4前のmainは設計待ち、opsalertは実装中という観測差として扱う。merge後に実状態を再確認し正式頁へ。今回はticket状態を変更しない |
| Y-15 | 採用。literalだけで全入口を採点するのは矛盾 | §4でG_lit/G_reachと機械/頁セルを分離。頁の手動補完と動機再現を初回合否へ |

D18: `src/agentic_fx/ops/events.py` の module docstring は INSERT 失敗時の隔離を広く表現するが、`emit` は例外を伝え、`emit_best_effort` も事前 schema 違反は伝える。F09 を根拠とし、コメント修正の所有 ticket は未確定。C4 merge 後に残存を確認する。F/K/D 正式頁ではD18を§3.4へ配置する。
