# 取引判断の手数を最終回答に残す設計

版: C0.2（2026-11-21、下書き）。対象: `docs/tickets/open/signal-mission-max-turns-failures.md`。

取引判断の agent がニュース検索や建玉確認を続けても、最後の2ターンは調査を止め、手元の材料から判断の JSON を出す。signal は保存済み戦略の採否、cron は売買操作または hold を返す。利用者からは「判断なしで終了」の通知が減り、採否の記録が増える。判断不能を成功扱いに置き換えて件数だけ改善することは目標にしない。

承認の方針はチケットの「処置案・裁定」（2026-11-21）に記録されている。本書では (a) 最終2ターンのツール停止、(b) 同じツール・同じ引数の再実行抑制、(c) signal の検索予算と固定の返答、(d) 記事の取込み時刻の提示、と定義する。prompt に検索の目安と打切りの判断規則も加える。(a)(b)(c) は local だけに適用する。以下で数え方・配置・失敗処理を具体化し、未回答時の既定判断は末尾の問いに残す。この作業では設計書のみを改訂し、コード・設定・本番状態を変更せず、テストも実行しない。

## 仕組み

### 現行の経路と制約

参照は調査時の行番号。

| 対象 | 現在の動作と根拠 |
|---|---|
| local のターン | `src/agentic_fx/runners/local_runner.py:167–180` の request/response が1ターン。毎回 tools と tool_choice=auto を送る。1応答の tool_calls は16件までで、順に実行する（同:218–249）。 |
| JSON と終了 | ツール呼出しを含む応答は content よりツールを優先（同:211–273）。parse 修復・schema 修復は別カウンタで各2回、残りターンを消費（同:288–323）。有効な出力は completed、上限終了は max_turns（同:334–336）。timeout が優先（同:154–157）。 |
| parser | `src/agentic_fx/runners/response_parser.py:49–105` は think・フェンス・周辺文章を除き dict を抽出する。ローカル解析であり追加の LLM ターンではない。schema 検証は runner 側。 |
| signal | `src/agentic_fx/loops/trade_loop.py:442–517` は oldest claim 後、専用 prompt・DECISION_SCHEMA で runner を呼ぶ。local は2ツールに制限。非 completed は判断を記録せず失敗通知（同:491–497）、未消費 signal は finally で再待機へ戻す（同:696–701）。 |
| cron | `src/agentic_fx/loops/trade_loop.py:236–250` は非 completed なら終了・通知し、操作を実行しない。schema と上限は同:820–824。 |
| 出力契約 | signal は accept/null または reject/許可コードの2キーのみ（`src/agentic_fx/loops/signal_decision_contract.py:21–32、54–78`）。cron の hold は action と reasoning が必要（`src/agentic_fx/loops/summary.py:19–36`）。 |
| 最終防衛 | opposite_position は実注文と照合する（`src/agentic_fx/loops/signal_decision_contract.py:94–124`）。accept 後も executor の gate を通る（`src/agentic_fx/loops/trade_loop.py:646–660`）。news_shock の真偽は決定論検証しない。 |
| 日次枠 | `src/agentic_fx/store/missions.py:346–358` は signal% の起動行を数え、system_filtered だけを除く。max_turns・skipped も含む。日境界と最短間隔もこの集合で判定する。 |

2026-11-09以降の signal は102本中44本が max_turns。44本中30本は同一 query の重複を1回以上含むという意味であり、全検索が同じではない。完全一致の重複は704呼出し中248件、異なる query が4種以上ある mission は40本。同一呼出しの抑制は signal では空費を減らすが、上限到達の解消は主に検索予算と最終2ターンに期待する。cron の max_turns は49本で、ツール1038呼出し中、mission・名前・引数の異なる組は500。get_positions は440件で引数はすべて `{}` のため、(b) の主な対象となる。

検索予算は5回とする。読み取り専用 DB で確認した同期間の signal の LLM news_shock 6件の分布は以下。4回の例を覆う一方、12回の例は覆えないため、配備後に重大事実の見落としを確認する。回数だけでは決め手の記事の取得順を証明しない。

| 検索呼出し回数 | news_shock 件数 |
|---|---|
| 1 | 1 |
| 2 | 2 |
| 3 | 1 |
| 4 | 1 |
| 12 | 1 |

集計は `sqlite3 -readonly data/agentic.db` で、trade・signal%・開始日を限定し、transcript_json の tool_calls 中の search_news を mission 別に数え、signal_decisions の actor=llm・reject_code=news_shock と結合した。本文・query・金額は抽出しない。その他の件数は既存調査の集計値。

### ターンと最終回答

Mission に任意の判断予算 policy（signal / cron）を追加する。既定は None とし、種類を schema や prompt から推測しない。WorkerRunner の handshake は5項目を手書きで送る（`src/agentic_fx/runners/worker_runner.py:626–629`）ため、policy の転送・検証・worker での復元も変更する。

policy が有効な local は `max_turns >= 3` を要求し、満たさなければ実行前に設定エラーとする。既存設定は1以上を許す（`src/agentic_fx/config.py:151`）ため、policy 無効の小さい上限は変えない。1始まりの現在ターンを t、上限を M とし、t=M−1 と M を最終段階とする。M=16なら15・16回目。上限を増やさず、修復用の17回目も設けない。

開始時は runner が mission.prompt の末尾に固定文「総上限は M ターン。調査は M−2 ターンまで。最後の2ターンはツールを使わず JSON を出す」を実数で補う。signal には「検索の実行予算は5回」も加える。切替文は t=M−1 で1回だけ、信頼できる user メッセージとして「ツールは使えない。JSON だけを出す」と出力 schema を渡す。文言は後述の互換性確認で必要なら強化し、実装前に固定する。t=M は同じ文と schema を再掲する。前の応答への修復文・停止文と重なる場合は1つの user メッセージにまとめ、連続する user 行を作らない。残り0では指示を追加しない。

local の最終要求は tools と tool_choice の両フィールドを省略する。履歴の assistant/tool の対応は保持する。後述の task 0 で省略方式を先に試し、HTTP・履歴の受理が不合格の場合だけ tools あり・tool_choice=none を試す。両方が受理されなければ tools と auto を維持し、runner が最終段階の実行を拒否する方式を採る。方式は実装前に固定し、mission 中の失敗を受けて auto に戻す再送はしない。API の grammar 強制は加えない。

最終段階では構造化 tool_calls も、content の文字列によるツール呼出しも実行しない。JSON 併記でもツール呼出しを優先し、包み内の object を判断 JSON として採用しない。parser は周辺文中の object も抽出するため（`src/agentic_fx/runners/response_parser.py:77–86`）、この識別を先に行う。識別用 fixture は後述の互換性確認で実測と合成を区別して用意する。

形状検証は現行の即時失敗（`src/agentic_fx/runners/local_runner.py:211–257`）、許可・引数検証は現行の error 結果（`src/agentic_fx/tools/registry.py:69–81`）を維持する。最終段階の返答と修復加算は次のとおり。複数call の返答は各 ID に対応させ、parse 修復への加算は call 数によらず1応答につき1回、schema 修復には加算しない。

| ツール呼出しの状態 | 返答・終端 | parse 修復の加算 |
|---|---|---|
| 構造化呼出しの形状違反（配列・ID・name・arguments の型違反、17件以上） | 固定停止結果を返さず、現行どおり failed。再要求しない。arguments=null は従来どおり `{}` として扱う。 | 0（即時終了） |
| 形状は正しいが許可・引数違反（不正 JSON、非 object、schema 違反を含む） | 各 tool_call_id に現行の検証 error を返す。残りターンがあれば下記の固定文で判断 JSON を要求し、引数の修正は求めない。 | 1 |
| 形状・許可・引数が有効 | 各 tool_call_id に `{"error":"final_answer_only","message":"最終段階のため実行しない。判断の JSON だけを出す"}` を返す。 | 1 |
| 識別できる文字列のツール呼出し | tool ID を捏造せず、残りターンがあれば下記の固定文を user メッセージで返す。 | 1 |

固定の停止・修復文は「最終段階のため実行しない。判断の JSON だけを出す」とする。次回の切替文・schema と1つの user メッセージへ統合する。parse 修復は通常の parse 不良と共通で2回まで、3回目は failed。残り0では user メッセージ・要求を追加せず、修復上限未超過なら max_turns。即時失敗に至らないツール呼出し形だけを parse 修復に数え、schema 修復側とは分ける。

| 残りターン内の応答 | 処理 |
|---|---|
| 正しい JSON・schema | completed。signal はさらに parse_decision、cron は TradeIntent と gate を通す。local signal の検索成功0による保護は後述の終端優先表に従う。 |
| JSON 不正 / schema 違反 | 現行の種類別2回上限を維持。残りターンがあれば修復指示。3回目の同種失敗は failed。 |
| 最終回で未回答 / 不正 JSON | 修復上限未超過なら max_turns、output=None。超過なら failed。残り0で修復要求を追加しない。 |
| HTTP・応答形状・schema 定義不良 | 現行の failed。timeout・abort の確認は抑止結果の経路でも維持する。 |
| 締切到達 | 最終回答用ターンが残っていても timeout が優先。時間枠は延長しない。 |

最終段階の指示は採否を誘導しない。accept の既定や cron の hold 原則は prompt の判断規則にだけ置く。有効なモデル回答だけを completed とし、未回答は従来の失敗に保つ。signal の自動 accept は発注可能性を増やす。cron の自動 hold と失敗はともに操作を実行しないため資金への効果は同じだが、自動 hold は失敗の記録と通知を隠す。

### 同一呼出しと検索予算

状態は LocalRunner.run(mission) の開始時に作り、終了時に捨てる。registry インスタンスには保持せず、必要な状態を execute へ引数で渡す。別 mission・requeue 後の mission に持ち越さない。local は順次実行（`src/agentic_fx/runners/local_runner.py:223–249`）なので、MCP 向けの guard・名前の対応づけ・並行時の予約は加えない。

比較キーは正式なツール名と、検証済み JSON 引数をキー順ソートした安定直列化。書式空白・オブジェクトのキー順は無視し、配列順、文字列内の空白・大小・Unicode、値の型は保つ。query の trim・casefold・類義語判定はしない。不正 JSON・非 object・許可外ツールは従来のエラーとし、キーに登録しない。

成功した同一キーの2回目以降は実行せず、固定コード duplicate_call と「既に得た結果。判断して JSON を出す」を返す。先の結果は履歴にあるため識別子・本文の再掲は不要。空配列も成功として登録する。エラーは成功キーに登録せず、同じ引数で1回だけ再試行を許す。再試行も失敗したキーはそれ以上実行せず、duplicate_call と取得失敗済みであることを返す。これにより get_positions の一時エラーを回復可能にする。

検索の試行記録は search_news の名前を識別した時点で、引数の解析・検証や抑止より前に行う。検査順は形状・mission.tools の許可・引数、最終段階の停止、同一キーと再試行回数、検索予算、実行。最終段階の返答・修復加算は上表に従う。registry の検証後・関数呼出し前に、渡された状態で判定する（`src/agentic_fx/tools/registry.py:69–83`）。既存 on_execute は検証より前に呼ばれる（同:60–65）ため、policy 有効時は抑止判定を実行通知より先に置く。抑止を on_execute/on_result の成功実行として数えず、終端要約に集計する。policy 無効時の通知順は保つ。

signal の search_news は実行を試みる5回まで。空結果・取得エラー・同一引数の再試行も1回、重複抑止は0回。6回目以降は search_budget_exhausted と固定文「検索の予算に達した。これまでの材料で判断して JSON を出す」を返す。accept の文言は入れない。同一応答内の複数callでも5回を超えない。予算切れで即終了せず、残りターンの get_positions と回答は可能にする。

local signal は検索の試行有無、実行数、成功数、検証エラー数、実行エラー数を分けて保持する。検索を試み、成功が0回のときは accept を採用せず `failed / output=None / reason=search_unavailable` とし、既存の失敗通知・requeue に戻す（`src/agentic_fx/loops/trade_loop.py:491–497、696–701`）。引数違反だけ、最終段階での停止だけなど、実行数0でも試行があれば対象とする。検索を1回も呼ばずに判断した mission は対象外。正常な空配列は成功1回であり、取得エラーとは区別する。

引数検証は関数呼出しより前に error を返す（`src/agentic_fx/tools/registry.py:74–83`）ため、実行数だけでこの保護を判定しない。検証エラーと抑止は検索の実行予算を消費しない。RPC 失敗は例外（`src/agentic_fx/mission_worker.py:522–523`）、registry は例外・直列化失敗・error 結果を失敗として扱う（`src/agentic_fx/tools/registry.py:84–104`）。成功数はこの成否から加算し、重複・予算切れ・最終段階の停止結果では増やさない。新たな reject_code は増やさない（`src/agentic_fx/loops/signal_decision_contract.py:15–32`）。終端は上から順に判定する。

| 条件 | status / output / reason |
|---|---|
| timeout・abort・HTTP・応答形状不良・schema 定義不良など既存の終端原因あり | 既存の status/reason を優先。検索成功0の事実は要約に残す。 |
| 有効な reject の回答あり | 通常の completed。parse_decision と既存の採否検証を維持する。 |
| 検索を試みて成功0で、accept または有効な回答なし | failed / None / search_unavailable。 |
| 上記以外で有効な JSON・schema の回答あり | 通常の completed。検索未試行や正常な空結果だけを理由に失敗させない。 |
| 上記以外で最終段階の未回答・修復超過 | 上限未超過は max_turns / None、修復超過は failed / None。reason は final_answer_missing。 |

この決定論的保護は policy 有効の local signal だけの保証とする。Claude には追加せず、共通 prompt の判断規則だけが効くため、検索成功0での accept を防ぐ保証はない。

cron に検索5回上限は置かない。ニュースとテクニカルの両軸・複数 pair を調べる契約（`src/agentic_fx/loops/prompts/trade_mission.md:5–14`）で同じ数の根拠がないため、同一呼出し抑制と最終2ターンで止める。残る異なる検索の連続は配備後に測る。

### backend と他 loop への影響

| backend | 今回の扱い |
|---|---|
| local | (a)(b)(c) を実装する。Mission の明示 policy、runner のターン制御、mission 状態を渡す実行 guard を使う。 |
| Claude | (a)(b)(c) と検索成功0の決定論的保護は適用せず従来動作。共通 prompt の規則のみで、全検索エラー後の accept を防ぐ保証はない。CLI には schema と max-turns を渡す（`src/agentic_fx/runners/claude_runner.py:74–83`）。今回の policy を理由に起動を拒否しない。CLI 共通処理は schema 適合だけで completed にする（`src/agentic_fx/runners/cli_runner.py:626–637`）。 |
| Codex / OpenCode | 取引設定で既に拒否される（`src/agentic_fx/config.py:129–135`）。max_turns を無視する実装（`src/agentic_fx/runners/codex_runner.py:101–102`、`src/agentic_fx/runners/opencode_runner.py:130`）を変更しない。 |

local 以外の許可済み取引 backend では、起動時に1回だけ activity に「手数の制御と検索成功0の保護は local のみ」と記録する。取引 backend の選択肢は `config/settings.yaml.example:27` の local / claude を維持する。(d) と判断本文の更新は共通の検索結果・prompt に反映する。

policy は次の生成箇所でのみ設定し、それ以外は既定 None を保つ。

| Mission の生成箇所 | policy |
|---|---|
| signal: `src/agentic_fx/loops/trade_loop.py:473–477` | local のとき signal |
| cron: `src/agentic_fx/loops/trade_loop.py:820–824` | local のとき cron |
| ask: `src/agentic_fx/loops/trade_loop.py:929–933` | None |
| reflection: `src/agentic_fx/loops/reflection_cycle.py:176–179` | None（tools=[]、max_turns=2 を維持） |
| improve: `src/agentic_fx/loops/improve_loop.py:1012–1015` | None |
| verify_backend: `src/agentic_fx/loops/verify_backend.py:649–664` | None |
| canary: `src/agentic_fx/core/improve_canary.py:506–508` | None（max_turns=1 を維持） |

### 記事の時刻と判断本文

`src/agentic_fx/store/rag.py:155–159` が保持する added_at を検索結果（同:171–173）へ追加し、ツールの安全フィールド投影（`src/agentic_fx/tools/news_tools.py:12–14`）では ingested_at として提示する。UTC offset 付き ISO 文字列で、Rag の内部名は変えない。書込みは必ず added_at を保存し、cleanup も必須として読む（`src/agentic_fx/store/rag.py:186`）ため、欠損専用の分岐・試験は追加せず投影は `.get` で足りる。再投入・移行は不要。URL は公開しない。初回取込み時刻を保ち、記事の発表時刻・介入実施時刻と混同しない。時刻だけで鮮度や事実の真偽を確定しない。

`src/agentic_fx/loops/prompts/signal_decision_mission.md:4–11` の判断規則を更新し、検索は5回までを目安とする。「成功した検索材料に前提を壊す事実がなく、実在する逆方向注文にも該当しなければ accept」「介入の噂・警戒だけでは news_shock にしない」を判断規則にだけ置く。確認済み重大事実による reject、逆方向注文の例外、取込み時刻の意味を併記する。共通本文には「検索エラーは判断材料ではなく、重大事実がないことの確認にも使わない。保存済み signal、成功した検索結果と実在する注文に基づき、出力は指定 schema の accept/reject に限る」と書く。runner の failed 処理をモデルに約束させず、schema にない failed を出力させない（`src/agentic_fx/loops/signal_decision_contract.py:21–32`）。local の保護と Claude の保証範囲は前節に従う。承認の根拠はチケットの裁定に記録されている。`src/agentic_fx/loops/prompts/trade_mission.md:12` の hold 原則は保ち、残り手数で不足する調査を無理に継続しない旨を加える。

## 判断を出せなかった場合と日次枠

max_turns を accept/hold に決定論変換しないことを推奨する。signal は判断なし・未消費のまま requeue、cron は当該判断足の失敗として通知する。失敗通知と status を維持し、最終段階の失敗は固定 reason で区別する。local signal の検索成功0による失敗と他の終端との優先順位は「同一呼出しと検索予算」の表に統一する。検索成功0の保護に該当しない最終段階の未回答・修復超過は final_answer_missing、既存の別原因があればその reason を保つ。Claude に検索成功0の保護はない。最終段階の印は要約に記録する。

日次枠と requeue は変更しない。max_turns も1起動として数える。標準 requeue 上限2（`src/agentic_fx/config.py:589`）では初回と再試行2回で同じ signal が最大3枠を使う。鮮度切れならその前に abandoned（`src/agentic_fx/loops/trade_loop.py:877–892`）。失敗を枠から除くと無回答が続く日の起動回数の防波堤がなくなる。distinct signal 数への変更は別の試行上限も必要で、今回混ぜない。

`signal_daily_max` の36への引上げはチケットに記録された別裁定であり、今回反映・確認しない。配備前の基準期間は36化の反映後1週間とする。実装時の回帰試験は、失敗3起動が3枠、4回目は起動不可、system_filtered は0枠、skipped は1枠、最短間隔と日境界、鮮度切れで再起動しないことを確認する。

## テストと受入

本作業では実行しない。実装時は以下を満たす。

実 transcript を読み取り専用 DB から抽出し、形状を保って匿名化した fixture を追加する。既存調査の signal/max_turns は34メッセージ、user 1、assistant 16、tool 17。最初の assistant は tool_calls 2件（search_news と get_positions）、続く15回は search_news 1件ずつ。content・arguments・tool_call_id は文字列で、記事結果は title/body/source_name の配列。この形を手書きの簡略往復に置き換えない。

同じ query が続く例・言換え例・cron の get_positions 反復例の出典を fixture の来歴に残す。配列順、複数call、null と文字列の違い、arguments の JSON 文字列化、ID の参照関係を保持し、本文・query・状態サマリは安全な置換値にする。金額・口座情報を含む原文は持ち込まない。最終段階の応答形は互換性確認の実応答を優先し、自然発生しない形は実テンプレート・プロトコルを根拠に合成して補う。実測由来／合成を来歴で区別し、合成を実測と記さない。

fake LLM は既存 httpx.MockTransport の方式（`tests/runners/test_local_runner.py:33–45`）で要求 body を捕捉する。過去の assistant/tool の並びから応答を再生し、終盤だけ差し替える。fixture を保存するだけでなく再生に用いる。

| 再現・境界 | 合格条件 |
|---|---|
| 異なる query を続ける | 実検索は5回。6件目以降は中立の固定文。同一応答の複数callでも超過しない。 |
| 同じ query / get_positions の反復 | 成功キーは1実行。引数書式が異なっても抑止し、文字列内容の違いは別キー。別 mission は再実行可能。 |
| 一時エラーと全検索エラー（local signal） | 同じ引数は1回再試行でき、実行エラーは予算に数える。試行あり・成功0なら accept を保存せず failed/search_unavailable、未消費 signal を既存 requeue へ。 |
| 検証エラーだけの検索（local signal） | `search_news({"query":123})`、不正 JSON、非 object の引数だけを繰り返してから accept しても failed/search_unavailable。実行数・予算消費0、試行あり・成功0として記録する。 |
| 検索なし／正常な空結果／最終段階だけの検索（local signal） | 検索未試行の accept、正常な空配列の後の accept は通常の completed。最終段階で初めて検索を呼び停止された後の accept は試行あり・成功0で failed/search_unavailable。 |
| 検索失敗と終端の優先（local signal） | 成功0でも有効な reject は既存検証へ。timeout・abort・HTTP・形状不良は既存原因を優先。成功0と有効回答なしだけなら search_unavailable。 |
| 最終回の JSON | task 0 で決めた要求方式を15・16回目に使い、実ツールは0回。16回以内の signal accept/reject、cron hold は schema と検索成功0の終端優先を満たせば completed。 |
| 不正 JSON・文字列/構造化ツール呼出し | 実行せず修復各2回を維持し、形状違反は即時 failed、それ以外のツール形は1応答につき parse 修復1回。実測・合成 fixture で上表の検証エラーと固定停止結果、修復文を再現。ID 対応を維持し、併記 JSON を採用しない。検索成功0の終端優先を満たした上で修復成功は completed、残り0は max_turns/None、修復超過は failed。17回目を要求しない。 |
| 境界・エラー | policy 有効の M<3 を拒否し、無効時は従来動作。1ターン16/17 calls、timeout・abort・HTTP 不良・schema 定義不良の既存終端を守る。 |
| 転送・他 backend / loop | handshake で policy を保持し、要約も往復する。状態を mission 間で共有しない。Claude は従来動作と起動時の告知を確認し、全検索エラー後の schema 適合 accept も completed になり得ることを受入条件に明記する。ask/reflection/improve/verify_backend/canary は None のまま。 |
| 判断の保護 | 成功検索の噂だけは accept、確認済み重大事実は news_shock、実在する逆方向注文は opposite_position。JSON 不在を自動 accept にしない。検索成功0の決定論的保護は local signal のみ。採否後の gate を維持。 |
| 告知・計測 | 開始時の上限、切替1回、最終回の再掲、修復との user 行統合を確認。終端要約は1件で、要求 body の messages に計測情報がない。 |
| 時刻 | Rag の added_at を tool の ingested_at に投影。再取込みでも初回時刻、URL 非公開。 |

最終応答が無い fake が max_turns になることも正常で、全件 completed は要求しない。LLM が必ず従う保証と、無駄な調査を止める保証を分ける。

### 環境互換性の確認（task 0）

この作業では実行しない。設定の参照は `config/settings.yaml.example:50–51`（max_turns=16、timeout_sec=300）。reflection は tools=[] で local を使い（`src/agentic_fx/loops/reflection_cycle.py:176–179`）、既存集計に completed 24本があるが、履歴ありの tools 省略を保証するものではない。配置済み llama.cpp の current リンクや配置ソースの対応を推測せず、稼働中の llama-swap へ合成要求を送り確認する。

1. 実装で送る切替時点の要求を再現する。M=16 では既存34メッセージの匿名履歴を14応答終了時まで切り出す（初期 user 1、assistant 14、tool 15の30メッセージ）。初期 prompt に上限文を加え、tool 結果は予算・重複抑止を適用したものに置き換える。t=15 の切替文と実際の出力 schema を末尾の user に入れ、修復指示があれば統合する。signal/cron の両 schema を対象とし、assistant/tool の ID 対応、content の型、arguments の文字列化を保つ。要求の履歴と tools の組立て根拠は `src/agentic_fx/runners/local_runner.py:145–180、201–209、258–259`。実口座・実注文・金額・原文を使わず、ツールは接続・実行しない。
2. t=15 の要求では tools/tool_choice を省略する。その応答を履歴へ組み込み、未回答なら t=16 の要求も送る。構造化呼出しには上表の ID 対応の停止・検証結果を追加し、文字列呼出しや不正 JSON・schema には修復文を加える。t=16 の再掲文と schema は修復文と1つの user に統合する。t=15 で有効回答が得られた枝はそこで終了し、次回要求の互換性は別の合成した未回答枝で確かめる。構造化・文字列の両枝を用意する。
3. 合格条件を分けて記録する。**HTTP・履歴の受理**は両要求が HTTP 成功となり、choices/message の応答形状が読め、assistant/tool・切替文・schema・停止結果の履歴が拒否されないこと。これが不合格の場合だけ同じ要求群で tools あり・none を試し、両方不合格なら tools/auto と runner の実行拒否に固定する。モデルが JSON を返さないことだけを理由に方式を切り替えない。
4. **モデルの出力**は各 schema で t=15 または16に適合 JSON が返ることを別に確認する。落ちた場合は切替文を「調査は終了した。ツール呼出しや説明文を出さず、次の schema に合う JSON オブジェクトを1つだけ出す」と強化して同じ要求群を再確認する。parse/schema 修復は各2回、最終2ターン内かつ総上限16回を維持し、追加ターンは作らない。なお不合格なら未達を記録し、policy の有効化は保留する。HTTP 受理だけで判断出力の受入済みとしない。
5. 要求方式、切替文、各判定、応答形、使用した履歴・schema の来歴を残す。自然発生した応答は匿名 fixture に保存する。発生しなかった文字列・構造化呼出しは、稼働モデルの実テンプレートや API の呼出し形式を根拠に合成し、参照先・版・合成箇所を明記する。構造化の基本形は `src/agentic_fx/runners/local_runner.py:231–247`、文字列の包みは確認したテンプレート等を根拠にし、未観測の形を実測扱いしない。停止結果を含む次回要求もこれらの fixture から作って受理を確かめる。

### 配備後の確認

trigger・backend・model・期間を揃え、読み取り専用 SQL と activity を使う。以下は `sqlite3 -readonly data/agentic.db` 用。`:since` / `:until` を UTC の比較期間に束縛し、同じ runner/model に絞って配備前後それぞれで実行する。system_filtered・skipped・running は LLM 試行率の分母から除く。失敗を completed に混ぜない。

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
  AND runner=:runner AND model=:model
  AND datetime(started_at)>=datetime(:since)
  AND datetime(started_at)<datetime(:until)
GROUP BY kind, runner, model;

SELECT m.trigger, d.actor, d.decision, d.reject_code, count(*) n
FROM signal_decisions d JOIN missions m ON m.id=d.mission_id
WHERE m.loop='trade' AND m.trigger LIKE 'signal%'
  AND m.runner=:runner AND m.model=:model
  AND datetime(m.started_at)>=datetime(:since)
  AND datetime(m.started_at)<datetime(:until)
GROUP BY m.trigger, d.actor, d.decision, d.reject_code;
```

ターン数は transcript の assistant 数から数えない。WorkerRunner は transcript をサイズ上限で切り詰める（`src/agentic_fx/runners/worker_runner.py:399–409`）。終端要約の request 開始数から、completed の中央値・p95と全終端の消費回数を出す。応答なしの request も含める。

mission 終端で本文なしの要約イベントを1件、activity に mission 参照つきで記録する。要約は request 開始数、最終段階へ入ったか、最終段階で判断したか、予算切れの返答後に判断したか、検索の試行有無と実行・成功・検証エラー・実行エラー・重複抑止・予算超過回数、全ツールの重複抑止数と最終段階の実行数、runner の終端・固定 reason を持つ。query・記事・引数本文は含めない。種類ごとにイベントを増やさない。

要約は MissionResult の任意フィールドと worker の result frame で親へ渡し、要求 body の messages や `_sink` を通さない。`_sink` は messages に追加し、その配列が次の要求になる（`src/agentic_fx/runners/local_runner.py:127–142、178–179`）。既存の result 復元（`src/agentic_fx/runners/worker_runner.py:748–787`）を拡張し、親の終端処理から activity.write（`src/agentic_fx/activity.py:51–76`）へ送る。worker 異常終了で要約が無い場合は計測欠損として数え、0回や非最終段階とみなさない。

36化を反映した後、手数制御の配備前1週間を基準期間に置き、配備後7運用日と比較する。signal/cron 別に max_turns 率を確認し、failed/timeout への付替えがないことを見る。failed は activity の mission_failed の reason、signal_decision_invalid と照合する（`src/agentic_fx/loops/trade_loop.py:242–249、499–509、738–752`）。回答後の鮮度切れ・source_inactive・invalid_reject_code（同:590–627）は runner の無回答と分け、未分類も明示する。signal 100試行未満は継続観測とし、小さい news_shock 件数の率だけで安全と判定しない。検索5回以下、成功キーの再実行0、最終段階の実ツール0は全件確認する。

終端要約を mission に結び、最終段階で決まった判断／それ以前の判断、予算切れ後の判断／予算切れ前の判断に分ける。signal は LLM actor の accept 率と reject_code、cron は open/close/cancel/hold の内訳を出す。基準期間には印が無いため全体を基準にし、配備後の各群と全体を示す。plugin・時間帯・backend・model を揃え、requeue と日次枠による母集団変化も併記する。system reject は別集計とする。

最終段階や予算切れ後の accept/open 増加、重大ニュース日の news_shock 欠落は人が確認する。少数事例の検索結果と signal 前提を匿名化して照合し、事実を取りこぼしていれば予算・prompt を見直す。噂だけによる reject 減少と、事実による reject の消失を分けて扱う。

## 作業の分割

依存先の成果物が揃うまで policy は既定無効を保つ。ここでは commit しない。

| task | 成果物・検証 | 依存 |
|---|---|---|
| 0 | 稼働中 llama-swap で t=M−1 と M の実要求形を再現。HTTP・履歴受理と schema 適合出力を別判定し、受理不可時のみ省略→none→auto と実行拒否の順で方式を固定。切替文と各2回の修復契約、実測／合成 fixture の来歴を残す。 | なし |
| 1 | 実 transcript の形と来歴を持つ匿名 fixture、fake 再生ヘルパー、文字列/構造化ツール呼出しの応答例。 | 0 |
| 2 | Rag の added_at と tool の ingested_at 投影、初回時刻保持の試験。 | なし |
| 3 | 任意 Mission policy と handshake、local の最終2ターン・同一呼出し・検索予算、検索試行・成功数による local signal の保護、最終段階の返答表、終端要約の転送と activity 記録。既定無効。 | 0、1 |
| 4 | signal/cron の local で policy を設定、判断本文更新、非 local の起動時告知。互換性確認の両合格条件と日次枠・他 loop の回帰試験を満たした後に有効化。 | 2、3、36化後の基準期間1週間 |
| 5 | 読み取り専用の配備後集計と受入結果。最終段階・予算切れ後の判断、採否と操作の偏り、news_shock を確認。 | 4（比較手順は先行可） |

## 利用者に問う事項

1. **最後まで有効な JSON が出ない mission を、決定論的な accept / hold に変えてよいか。** 推奨は既定値にせず、max_turns/failed と既存 requeue を維持する。signal の自動 accept は発注を増やし得る。cron の失敗と hold はともに操作を実行せず、資金への効果は同じだが、自動 hold は失敗の記録・通知を成功へ置き換える。signal では再待機の扱いも変わる。承認済みのモデルへの判断規則と、無回答をシステムが採用する処理は分ける。DB の CHECK は accept に actor='llm' を要求し（`src/agentic_fx/store/db.py:175–191`）、保存前の検証も同じ（`src/agentic_fx/store/signal_decisions.py:51–64`）。既定値案には signal/cron 個別の裁定、system provenance、DB schema と記録契約の拡張が必要となる。未回答は既存 requeue で再試行でき、チケットには26本中17本が後に completed になった実績がある。

## 付録: 文書とコードの食い違い

設計や実装を誤らせる差分を列挙する。この設計書以外は変更しない。

| 項目 | 文書側の記述 | 現行コードとの差分・扱い |
|---|---|---|
| 記事時刻 | チケットは news_tools の投影で欠けることを挙げる。 | `src/agentic_fx/store/rag.py:171–173` も added_at を落とす。両層の変更が必要。 |
| Claude の制御 | `src/agentic_fx/runners/base.py:25–27` は SDK で request 単位に停止すると記す。 | 実体は CLI、max-turns の引渡し（`src/agentic_fx/runners/claude_runner.py:74–83`）。今回の最終段階の制御とは異なる。 |
| Claude の実装 | `src/agentic_fx/runners/base.py:100–105` は未実装と記す。 | 実装済みで CLI 共通の schema 検証もある（`src/agentic_fx/runners/cli_runner.py:626–633`）。 |
| 日次枠 | `src/agentic_fx/store/missions.py:332–344` は signal% 全行がカウンタと説明する。 | SQL は system_filtered を除く（同:346–358）。 |
| ツールの許可 | signal は2ツールに限定するという説明。 | local は mission.tools を使うが、MCP は registry 全体を許可（`src/agentic_fx/mission_worker.py:708`）、Claude の allowed_tools も全 afx ツール（`src/agentic_fx/runners/factory.py:57`）。今回この差は変更しない。 |
| 共通の上限 | Mission は request/response と種類別2回修復を共通契約とする（`src/agentic_fx/runners/base.py:17–23`）。 | Codex/OpenCode は max_turns 無視、CLI 共通 schema 違反は即 failed（`src/agentic_fx/runners/codex_runner.py:101–102`、`src/agentic_fx/runners/opencode_runner.py:130`、`src/agentic_fx/runners/cli_runner.py:626–633`）。local の保証を全 backend に当てはめない。 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 承認済み5方針の具体化、最終2ターン・共通guard・現行枠維持・受入と計測を記述。未回答の既定判断を1件残す。 | 検索の反復で取引判断が失われる問題を、無回答の自動採用と混同せず改善するため。 | 4f77d3e4 |
| 2026-11-21 | C0.1 | 検索予算5回、中立の停止文、全検索エラーの失敗処理、local 限定、互換性確認の先行、終端要約と比較基準、問いの理由を明確化。 | 設計レビュー 1 周目の指摘を反映 | 0c0d589c |
| 2026-11-21 | C0.2 | 検索試行と成功数による保護、backend ごとの保証範囲、切替要求の再現と合格条件、最終段階の返答・修復契約を明確化。 | 設計レビュー 2 周目の指摘を反映 | |
