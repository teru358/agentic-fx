# 取引判断の手数を最終回答に残す設計

版: C0（2026-11-21、下書き）。対象: `docs/tickets/open/signal-mission-max-turns-failures.md`。

取引判断の agent がニュース検索や建玉確認を続けても、最後の2ターンは調査を止め、手元の材料から判断の JSON を出す。signal は保存済み戦略の採否、cron は売買操作または hold を返す。利用者からは「判断なしで終了」の通知が減り、採否の記録が増える。判断不能を成功扱いに置き換えて件数だけ改善することは目標にしない。

利用者が2026-11-21に承認した、最終回のツール停止、同一呼出しの抑制、signal の検索3回上限、記事の取込み時刻、prompt の打切り規則を具体化する。以下の数え方・配置・失敗処理は設計上の推奨であり、未回答の資金保護方針は末尾に残す。この作業では設計書のみを追加し、コード・設定・本番状態を変更せず、テストも実行しない。

## 仕組み

### 現行の経路と制約

参照は調査時の行番号。`src/agentic_fx/` 配下のパスは省略せず記す。

| 対象 | 現在の動作と根拠 |
|---|---|
| local のターン | `src/agentic_fx/runners/local_runner.py:167` の request/response が1ターン。毎回 tools と `tool_choice: auto` を送る（同:175–180）。1応答の tool_calls は16件までで、順に実行する（同:218–249）。 |
| JSON と終了 | ツール呼出しを含む応答は content よりツールを優先して次へ進む（同:211、273）。parse 修復・schema 修復は別カウンタで各2回、いずれも残りターンを消費（同:288–323）。有効な出力は completed、ループ終了は max_turns（同:334–336）。timeout が優先（同:154–157）。 |
| parser | `src/agentic_fx/runners/response_parser.py:49–105` は think・フェンス・周辺文章を除き dict を抽出する。これはローカル解析であり LLM の追加ターンではない。schema 検証は runner 側。 |
| signal | `src/agentic_fx/loops/trade_loop.py:442–517` は oldest claim 後、専用 prompt・2ツール・DECISION_SCHEMA で同じ runner を呼ぶ。非 completed は判断を記録せず失敗通知（同:491–497）、finally で未消費 signal を再待機へ戻す（同:696–701）。 |
| cron | `src/agentic_fx/loops/trade_loop.py:211–249` は trade prompt で runner を呼ぶ。非 completed は終了・通知し、その判断を実行しない。schema と上限は同:820–824。 |
| 出力契約 | signal は accept/null または reject/許可コードの2キーのみ（`src/agentic_fx/loops/signal_decision_contract.py:21–32、54–78`）。cron の hold は action と reasoning が必要（`src/agentic_fx/loops/summary.py:19–36、48–57`）。 |
| 最終防衛 | signal の採否は再解析され、opposite_position は実注文と照合する（`src/agentic_fx/loops/signal_decision_contract.py:94–124`）。accept 確定後も executor の gate を通る（`src/agentic_fx/loops/trade_loop.py:646–660`）。news_shock の真偽はこの契約では決定論検証しない。 |
| 日次枠 | `src/agentic_fx/store/missions.py:346–358` は signal% の起動行を数え、system_filtered だけを除く。max_turns・skipped も含む。日境界は signal_day_start、直近起動との最短間隔も同じ集合で判定する。 |

チケットの集計は signal 102本中44本が max_turns、44本中30本が同じ query、14本が言換えの連続。cron でも49本が max_turns。これは既存調査の値であり今回の再集計ではない。

### ターンと最終回答

Mission に明示的な任意の判断予算 policy を追加する。種類は signal / cron、最終回答用ターン数は2。既定は無効とし、signal と cron の生成箇所だけで指定する。schema のキーや prompt の文字列から種類を推測しない。WorkerRunner の handshake は現在5項目を手書きで送っている（`src/agentic_fx/runners/worker_runner.py:626–629`）ため、policy の転送・検証・worker での復元も変更対象に含む。

1始まりの現在ターンを t、上限を M とすると、`M - t + 1 <= min(2, M)` で最終回答へ切り替える。M=16なら15・16回目、M=2なら1・2回目、M=1なら最初からツールなし。上限を増やさず、修復用の隠れた17回目も設けない。開始時に総上限と調査上限を知らせ、切替時に信頼できる user メッセージとして「ツールはもう使えない。既に得た結果で判断し、指定形式の JSON だけを出す」を追記する。

local の最終要求は tools と tool_choice の両フィールドを省略する方式を第一候補とする。履歴の assistant/tool の対応は保持する。`tools` を残して `tool_choice: none` とする方式も比較するが、採用は後述の環境互換性確認後に固定する。失敗を見て auto へ戻す再送は行わない。JSON schema を最終指示に明示し、API の grammar 強制は今回加えない。

最終段階で禁止を無視した tool_calls が来ても一切実行しない。各 tool_call_id に固定の停止結果を返して履歴を整え、そのターンを消費する。content に JSON が併記されていても通常の tool_calls 優先規則を維持し、採用しない。次の残りターンがあれば再びツールなしで回答させる。

| 残りターン内の応答 | 処理 |
|---|---|
| 正しい JSON・schema | completed。signal はさらに parse_decision、cron は TradeIntent と gate を通す。 |
| JSON 不正 / schema 違反 | 現行の種類別2回上限を維持。残りターンがあれば修復指示。3回目の同種失敗は failed。 |
| 最終回で未回答 / 不正 JSON | 修復上限未超過なら max_turns、output=None。修復超過なら failed。残り0で修復要求だけを transcript に追加しない。 |
| HTTP・応答形状・schema 定義不良 | 現行の failed。timeout・abort の確認を抑制結果の経路でも維持する。 |
| 締切到達 | 最終回答用のターンが残っていても timeout が優先。時間枠の延長はしない。 |

最終指示は種類別にする。cron は「材料が足りなければ `{"action":"hold","reasoning":"判断材料が不足"}`」。signal は「前提を壊す事実を確かめられず、実在する逆方向注文にも該当しなければ `{"decision":"accept","reject_code":null}`」。既に確認した重大事実があれば reject を維持する。情報取得エラーを『重大事実なし』と読み替えるようには指示しない。

比較した案は、prompt だけの回数目安（強制力がない）、runner で停止してモデルに回答させる案（推奨）、未回答をプログラムで既定 JSON にする案。最後の案は回答率を保証するが、signal の自動 accept は発注可能性を増やし、cron の自動 hold も必要な close/cancel を省き得る。C0 の推奨は有効なモデル回答だけを completed とし、未回答は従来の失敗に保つこと。利用者の判断事項は末尾の1件に集約する。

### 同一呼出しと検索予算

mission ごとに1つの状態を作り、終了・requeue 後の別 mission へは持ち越さない。runner の request 制御と、local/MCP が共に使うツール実行入口で同じ policy を参照する。単に `search_news` の関数内や TradeLoop の終了後に置いても、同じ mission の get_positions 反復や CLI を止められない。

比較キーは正式なツール名と、検証済み JSON 引数をキー順ソートした安定直列化。JSON の書式空白・オブジェクトのキー順は無視し、配列順、文字列内の空白・大小・Unicode、値の型は保つ。query の trim・casefold・類義語判定はしない。誤って別操作を同一視しないためである。不正 JSON・非 object・許可外ツールは従来のエラーで返し、正規化対象にしない。

同一キーの2回目以降は実ツールを実行せず、固定コード `duplicate_call` と「既に得た結果。判断して JSON を出す」を返す。先の結果を履歴で参照できる識別子を添えるが、大きな結果は再掲しない。先の実行がエラーなら『取得失敗済み』も明示し、成功を装わない。同じ応答に複数の同一 call がある場合も最初だけ実行する。mission のこの仕様では transient error の同一引数再試行も抑止する。必要な再試行は次の mission に委ねる。

適用順は形状・許可・引数検証、最終段階の停止、同一キー検査、ツール予算、実実行。同じ結果は同じ tool_call_id 対応の tool 応答で返す。CLI の `mcp__afx__` 名は adapter で registry の正式名へ対応させる。検証・予算判定と実行予約は原子的に行い、並行 MCP 呼出しでも二重実行しない。registry の既存 on_execute/on_result は実行・エラー監視に使われる（`src/agentic_fx/tools/registry.py:60–105`）ので、抑止を成功実行として通知せず別イベントで数える。

signal の search_news は実行を試みる3回まで。空結果・取得エラーも1回、同一呼出しの抑止は0回とする。4回目の異なる query には `search_budget_exhausted` と「検索の予算切れ。前提を壊す事実が無ければ accept」を返し、逆方向注文の例外も添える。1応答内に4検索があっても4件目を実行しない。予算切れだけで即終了せず、残りターンでは get_positions と回答を可能にする。言換え14/44本は同一キー抑制では防げず、この3回上限と最終2ターンで受ける。

cron には検索3回上限を置かない。複数 pair とニュース・テクニカルの両軸を見る契約（`src/agentic_fx/loops/prompts/trade_mission.md:5–14`）のため、signal と同じ数の根拠がない。cron は同一呼出し抑制と最終2ターンで止め、残る異なる検索の連続は配備後に測る。1ターン16件の既存上限は緩めない。

### backend と他 loop への影響

| backend | 現状の欠け / 今回の扱い |
|---|---|
| local | request を自前管理するため (a) を実装可能。(b)(c) は mission 状態を持つ実行 guard を registry の検証後・関数呼出し前に適用する。 |
| Claude | `src/agentic_fx/runners/claude_runner.py:74–83` は CLI に schema と max-turns を渡すだけ。(a) の途中切替も (b) の記憶もない。`--allowedTools` 空は拒否される（同:50–55）。MCP 側の (b)(c) は共通 guard で実現できるが、CLI 内の残り request 数はこの入口から分からない。 |
| Codex | `src/agentic_fx/runners/codex_runner.py:22、101–102` は max_turns を無視する。共通 CliRunner は終了後に抽出・schema 検証するだけ（`src/agentic_fx/runners/cli_runner.py:613–637`）。 |
| OpenCode | `src/agentic_fx/runners/opencode_runner.py:69、130` も max_turns を無視し、resume 回収は終了後の別機構（同:182–261）。最終2ターン予約ではない。 |

一貫する配置は「Mission の明示 policy + runner のターン制御 adapter + registry の実行 guard」。MCP は `src/agentic_fx/tools/mcp_shim.py:120–137` から同じ registry.execute を通る。factory だけの共通ラッパでは CLI 内部の各 request を数えられず、(a) は保証できない。

今回の有効化は現在の取引 backend=local（`config/settings.yaml:26–29`）に限定する。新 policy に必要な能力を factory/admission で検査し、非対応 backend を選んだ場合は起動前に明確な非対応エラーを出す。policy を黙って無視して取引を続けない。Claude の取引利用には、request 単位で停止を保証する adapter と同じ受入試験を別途必要とし、今回 CLI の `--max-turns` を互換と見なさない。Codex/OpenCode は既に取引設定で拒否される（`src/agentic_fx/config.py:129–135`）。

ask は TradeLoop と同じ runner を使う（`src/agentic_fx/loops/trade_loop.py:929–941`）。improve も共通 factory から local/CLI を構成する（`src/agentic_fx/mission_worker.py:856–869`）、ただし独自上限を使う（`src/agentic_fx/loops/improve_loop.py:1012–1015`）。無条件に (a)(b) を入れると ask の再照会や改善の書込み・検証の反復を変えてしまう。両者は今回 policy 無効を保つ。記事 added_at の追加だけは全利用先に見える additive な変更となる。

### 記事の時刻と判断本文

`src/agentic_fx/store/rag.py:155–159` が保持する added_at を検索結果（同:171–173）とツールの安全フィールド投影（`src/agentic_fx/tools/news_tools.py:12–14`）の両方に追加する。UTC offset 付き ISO 文字列、過去データで欠ける場合は null。既存メタデータの再投入・移行は不要。URL は公開しない。初回取込み時刻を保つ仕様を維持し、記事の発表時刻・介入実施時刻と混同しない。added_at だけで鮮度や事実の真偽を確定しない。

`signal_decision_mission.md:4–11` の判断本文に「検索は2〜3回まで」「前提を壊す事実を確かめられなければ accept」「介入の噂・警戒だけでは news_shock にしない」を追記する。逆方向注文の例外、実際の重大事実による reject、取込み時刻の意味も隣接して書く。これは一般原則の変更ではなく検索打切りの具体化。`trade_mission.md:12` の hold 原則は保ち、残り手数で不足する調査を無理に継続しない旨を加える。

## 判断を出せなかった場合と日次枠

推奨では max_turns を accept/hold に決定論変換しない。signal は判断なし・未消費のまま requeue、cron は当該判断足の失敗として通知する。失敗通知と status は維持し、最終段階で失敗したことは固定 reason で区別する。自動既定値案を採用するなら system による判断という provenance が必要で、既存の LLM accept として保存してはならない（現行の正規形は `src/agentic_fx/store/signal_decisions.py:51–64`）。これは今回の小変更には収まらない。

日次枠と requeue の仕様は変更しない。max_turns も1起動として数える。標準 requeue 上限2（`src/agentic_fx/config.py:589`）では初回と再試行2回で同じ signal が最大3枠を使う。鮮度切れならその前に abandoned（`src/agentic_fx/loops/trade_loop.py:877–892`）。失敗を枠から除くと無回答が続く日に起動回数の防波堤がなくなる。distinct signal 数に変えるには別の試行上限も必要なので、今回の回答率改善と混ぜない。

個人設定の36への引上げはチケットに記録された別裁定であり、今回反映・確認しない。枠の回帰試験は、失敗3起動が3枠、4回目は起動不可、system_filtered は0枠、skipped は1枠、最短間隔と日境界、鮮度切れで再起動しないことを確認する。仕様変更ではなく現行の保護である。

## テストと受入

本作業では実行しない。実装時は以下を満たす。

実 transcript を読み取り専用 DB から抽出し、形状を保って匿名化した fixture を追加する。今回 `sqlite3 -readonly data/agentic.db` で mission #4394（signal/max_turns）のキー・型・配列数だけを確認した。34メッセージ、user 1、assistant 16、tool 17。最初の assistant は tool_calls 2件（search_news と get_positions）、続く15回は search_news 1件ずつ。content・arguments・tool_call_id は文字列で、記事結果は title/body/source_name の配列。この形を手書きの簡略往復に置き換えない。

実装時にはこの由来と、同じ query が続く例・言換え例・cron の get_positions 反復例の出典 mission ID を fixture の来歴に残す。配列順、複数call、null と文字列の違い、arguments の JSON 文字列化、ID の参照関係を保持し、本文・query・状態サマリは安全な置換値にする。金額・口座情報を含む原文は持ち込まない。CLI用の既存 `tests/fixtures/signal_decision_claude.json:1–10` は採否2例のみで、この構造再現の代用にはならない。

fake LLM は既存 `httpx.MockTransport` の方式（`tests/runners/test_local_runner.py:33–45`）で要求 body を捕捉する。過去の assistant/tool の並びから provider message を再生し、終盤の応答だけを差し替える。fixture を単に保存して参照しないテストは不可。

| 再現・境界 | 合格条件 |
|---|---|
| 異なる query を続ける | 実検索は3回。4件目以降は固定結果。同一ターンの複数callでも超過しない。 |
| 同じ query / get_positions を反復 | 同一キーは1実行。ID・引数書式が異なっても抑止。文字列内容・大小の違いは別キー。別 mission は再実行可能。 |
| 最終回で JSON を出す | 15・16回目に新規ツール定義がなく、16回以内に signal accept/reject、cron hold の両 schema で completed。 |
| 15回目不正、16回目修復 / 未回答 | 修復成功なら completed、修復不能は max_turns/None。17回目を要求しない。既に種類別修復上限を超えた場合は failed。 |
| ツール禁止無視・混在応答 | tool 関数は呼ばれず、tool_call_id 対応が崩れない。JSON 併記を勝手に採用しない。 |
| 小さい上限・各種エラー | M=1/2、修復各2回の独立性、1ターン16/17 calls、timeout・abort・HTTP 不良・schema 不良の既存終端を守る。 |
| 共通入口・policy 転送 | local/MCP で同じ重複・予算判定、同時要求でも3回以下。handshake 往復で種類を保持、非対応 backend は実行前に拒否。ask/improve は従来動作。 |
| 判断の保護 | 噂だけは accept、確認済み重大事実は news_shock、実在する逆方向注文は opposite_position。JSON 不在・検索エラーを自動 accept にしない。LLM 採否後の gate は継続。 |
| 時刻 | Rag→tool の両段で added_at を保持、欠損 null、再取込みでも初回時刻、URL 非公開。 |

最終応答が無い fake が max_turns になることも正常な受入結果であり、全件 completed は要求しない。LLM が必ず従う保証と、無駄な調査を止める保証を分ける。

環境互換性は次の範囲まで確認済み。設定は `config/settings.yaml:48–51`（max_turns=16、timeout_sec=300）。配置済み llama-swap バイナリの `--version` は v272 / 802f1c4、`/home/teru358/project/llm/bin/current` は llama-b10205 を指す。ただし稼働プロセスの実行パス・モデル別起動引数は調べず、配置版と稼働版の同一性は未確認。

ローカルの llama.cpp ソース HEAD=1c2adc75f は `common/chat.cpp:323–333` で none を受理し、`tools/server/server-common.cpp:1422–1433` はツール省略を扱うが、jinja 無効では tool_choice=none を拒否する。履歴は同:1509–1513 で別途解析する。このソースが配置バイナリと同じとは証明できない。llama-swap の転送互換性・モデルテンプレートも含めた実リクエストの可否は未検証であり、「この環境で none が動く」とは断定しない。

配備前に本番とは別の確認環境で、配置対象版・同じテンプレートを用い、既存 tool 履歴を含む要求について (1) tools/tool_choice 省略、(2) tools あり/none、を比較する。HTTP 成功、履歴の受理、JSON 回答、tool 実行0を確認し、(1) を優先して採用方式を固定する。両方不可なら有効化を止め、auto に戻して配備しない。本作業では server への生成要求を送っていない。

### 配備後の確認

既存 status の DB 表示は最新 mission 1件のみ（`src/agentic_fx/status.py:326–330`）、既存 report の mission 集計は全 loop の status 合算（`src/agentic_fx/loops/econ_signal_report.py:310–322`）。両者は健全性の入口として使い、今回の効果は読み取り専用 SQL で trigger・backend・model・期間を揃えて見る。

以下は `sqlite3 -readonly data/agentic.db` 用。`:since` / `:until` を UTC の比較期間に束縛し、配備前後それぞれで実行する。system_filtered・skipped・running は LLM 試行率の分母から除く。失敗を completed に混ぜない。

```sql
SELECT CASE WHEN trigger LIKE 'signal%' THEN 'signal' ELSE 'cron' END kind,
       runner, model, count(*) attempts,
       sum(status='max_turns') max_turns,
       round(100.0*sum(status='max_turns')/count(*),1) max_turns_pct,
       sum(status='completed') completed,
       sum(status='failed') failed, sum(status='timeout') timeout
FROM missions
WHERE loop='trade' AND (trigger LIKE 'signal%' OR trigger='cron')
  AND status IN ('completed','failed','timeout','max_turns')
  AND datetime(started_at)>=datetime(:since)
  AND datetime(started_at)<datetime(:until)
GROUP BY kind, runner, model;

SELECT m.trigger, d.actor, d.decision, d.reject_code, count(*) n
FROM signal_decisions d JOIN missions m ON m.id=d.mission_id
WHERE datetime(m.started_at)>=datetime(:since)
  AND datetime(m.started_at)<datetime(:until)
GROUP BY m.trigger, d.actor, d.decision, d.reject_code;

SELECT m.id, m.trigger, m.status,
       sum(json_extract(j.value,'$.role')='assistant') turns
FROM missions m, json_each(m.transcript_json) j
WHERE m.loop='trade' AND m.runner='local'
  AND (m.trigger LIKE 'signal%' OR m.trigger='cron')
  AND datetime(m.started_at)>=datetime(:since)
  AND datetime(m.started_at)<datetime(:until)
GROUP BY m.id;
```

最後の集計から completed の最終回答までのターン数の中央値・p95を求める。全終端の消費ターン数も併記し、成功例だけ速くなる偏りを隠さない。追加する user 指示や tool 結果をターンとして数えない。assistant 数を使う方法は local のみ。最終要求直前の timeout 等、応答なしの request は transcript からは数えられないため、実装では policy 切替・request 開始数・重複抑止数・検索実行数・予算超過数を本文なしの構造化イベントとして既存 transcript sink に記録する。計測用イベントは role=assistant にしない。

配備前の同じ長さの期間と配備後7運用日を比較し、signal/cron 別に max_turns 率が下がり failed/timeout への付替えでないことを確認する。signal 100試行未満なら母数不足として継続観測。チケットの44/102は参考値で、現行モデル・設定を揃えた基準を優先する。3回上限、同一キー再実行0、最終段階の実ツール0は割合でなく全件で検証する。

LLM actor の accept/reject 比と news_shock・opposite_position の内訳を、plugin・時間帯別に比較する。system reject は別に数える。日次枠増加による母集団変化と requeue も併記する。news_shock 率が基準の半分以下、または事実が確認できる重大ニュース日に news_shock が0なら調査の目安とする（自動的な正誤判定ではない）。匿名化した該当検索結果と signal 前提を人が少数確認し、重大事実を取りこぼしていれば検索予算/prompt を見直す。噂由来の reject 減少だけを異常扱いしない。

## 作業の分割

各行は単独で merge できる成果物。依存先の有効化まで既定無効を保つ。ここでは commit しない。

| task | 成果物・検証 | 依存 |
|---|---|---|
| 1 | 実 transcript 由来の匿名 fixture と来歴、fake 再生ヘルパー。現行のメッセージ対応を検証。 | なし |
| 2 | Rag と news tool の added_at 追加・欠損/初回保持の試験。 | なし |
| 3 | 任意 Mission policy、handshake、mission 単位の重複・検索予算 guard。既定無効、local/MCP 共通試験。 | なし |
| 4 | local の最終2ターン制御、修復・timeout 境界、構造化計測。policy 無効では従来動作。 | 1、3 |
| 5 | signal/cron で policy 有効化、本文更新、backend 能力検査。環境互換性確認・日次枠/他loopの回帰試験を通してから有効化を merge。 | 2、3、4 |
| 6 | 読み取り専用の比較集計手順と受入結果の記録。母数・採否の偏り・news_shock を確認。 | 5（比較手順自体は先行可） |

## 利用者に問う事項

1. **最後まで有効な JSON が出ない mission を、決定論的な accept / hold に変えてよいか。** 推奨は変えず、max_turns/failed と既存 requeue を維持する。signal accept は取引へ進み、cron hold は既存注文の処置を見送るので、両者を単なる出力修復として扱えない。承認済みの「反証が無ければ accept」というモデルへの判断指示と、「判断が無いからシステムが accept」は別の資金保護方針である。既定値案を選ぶ場合は signal と cron を個別に裁定し、system provenance と記録契約の拡張を追加設計する。

## 付録: 文書とコードの食い違い

今回読んだ対象文書・コードコメントのうち、設計や実装を誤らせる差分を列挙する。修正はこの設計書以外には行わない。

| # | 文書側の記述 | 現行コードとの差分・扱い |
|---|---|---|
| 1 | チケットは記事時刻が news_tools の投影で欠けることを挙げる。 | `src/agentic_fx/store/rag.py:171–173` も added_at を落とす。news_tools だけの変更では直らず両層が必要。 |
| 2 | `src/agentic_fx/runners/base.py:25–27` は ClaudeRunner を SDK とし、request 単位に強制終了すると記す。 | 実体は CLI、max-turns の引渡しだけ（`src/agentic_fx/runners/claude_runner.py:74–83`）。共通定義どおりの計数・最終回切替が実装されているとは言えない。 |
| 3 | `src/agentic_fx/runners/base.py:100–105` は ClaudeRunner を未実装と記す。 | 現在は実装済み。CLI 共通の failed/timeout 理由・schema 検証もある（`src/agentic_fx/runners/cli_runner.py:598–633`）。 |
| 4 | `src/agentic_fx/store/missions.py:332–344` は signal% 全行が永続カウンタと説明する。 | 実 SQL は system_filtered を除く（同:346–358）。チケットの「失敗も1枠」は正しいが「すべての signal 起動」が枠を使うわけではない。 |
| 5 | `config/settings.yaml:27` の backend 注釈は codex の拒否だけを記す。 | validator は opencode も取引で拒否（`src/agentic_fx/config.py:129–135`）。改善 backend の選択肢と区別する。 |
| 6 | Mission の共通説明は request/response と種類別2回修復を runner 共通契約とする（`src/agentic_fx/runners/base.py:17–23`）。 | Codex/OpenCode は max_turns 無視、CLI 共通 schema 違反は即 failed（`src/agentic_fx/runners/codex_runner.py:101–102`、`src/agentic_fx/runners/opencode_runner.py:130`、`src/agentic_fx/runners/cli_runner.py:626–633`）。local の保証を全 backend の実装済み機能とみなさない。 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 承認済み5方針の具体化、最終2ターン・共通guard・現行枠維持・受入と計測を記述。未回答の既定判断を1件残す。 | 検索の反復で取引判断が失われる問題を、無回答の自動採用と混同せず改善するため。 | 未commit（指定による） |
