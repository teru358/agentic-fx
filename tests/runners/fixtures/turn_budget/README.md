# turn_budget fixtures

取引判断 mission の最終段階 (最後の 2 ターン) の要求・応答を試験するための fixture。

## 来歴

| file | 由来 |
|---|---|
| `signal_repeat_queries.json` | 稼働 DB の signal 起動 mission (max_turns 終了、同じ query を繰り返した 1 本) の message の形を保ち、本文を匿名化した。34 メッセージ (user 1、assistant 16、tool 17)。 |
| `signal_distinct_queries.json` | 同種で、16 回の query がすべて異なる (言い換えの連続) 1 本。 |
| `cron_get_positions_loop.json` | cron 起動 mission (max_turns 終了、get_positions の反復を含む) 1 本。38 メッセージ。 |
| `final_stage_responses.json` の `measured` | 稼働中の llama-swap へ合成要求を送って得た応答 (下記)。 |
| `final_stage_responses.json` の `synthetic` | 自然には発生しなかった形を手で作った応答。実測ではない。 |

匿名化の規則: 時刻・金額・口座・戦略名・記事本文・query は置換した。id は 32 桁英数の別値 (呼出しと結果の参照関係は保つ)。
assistant の content は実物どおり空文字、arguments は `json.dumps` 形式の文字列。
query は「出現順の番号つきの合成語」に置換し、同一 query の繰り返しパターンは保つ。
他の tool 結果は key 構成を保ち、値を一貫した合成値 (数値は 0) に置換した。配列は 5 件まで。
初期 user message は `(anonymized initial prompt)` に置換した。実 prompt が要る試験は自前で組み立てる。

`synthetic` の根拠: 構造化の呼出しは OpenAI 互換の `tool_calls` の形 (runner が組み立てる形と同じ)。
文字列の呼出しのうち `text_tool_call_function_form_response` と `..._after_prose_response` は、稼働モデルの chat template が
指示する形 (`<tool_call>` の中に `<function=名前>` と `<parameter=名前>` を入れ、template は「呼出しの前に自然文を置いてよい」と述べる)
をその template の文面から写した。モデルが実際にこの形を出した観測は無い (最終段階の要求は tools を省略するため)。
`text_tool_call_json_response` は JSON を包む別形 (他の Qwen 系 template に見られる一般形) の合成。
`text_tool_call_with_decision_inside` は呼出し包みの中に判断 JSON が入る形を作ったもの。

## 切替要求の互換性確認

稼働中の llama-swap に、最終段階へ入る時点の要求 (上限 16 ターンの 15 回目と 16 回目) を合成して送った。
送った回数は 7 回、1 回ずつ順に。判定は「HTTP・履歴の受理」と「schema に合う JSON の応答」を別に見た。

履歴: 初期 user (signal は判断 prompt と合成の signal 節、cron は判断 prompt と合成の state 節。末尾に上限文) +
assistant 14 応答 + tool 結果 15 件 (30 メッセージ)。signal は検索 5 回まで実結果、6 回目以降は予算切れの固定結果。
cron は同一呼出しの 2 回目以降が重複抑止の固定結果。その末尾に切替文 (user) と出力 schema を付けた。

| 要求 | tools / tool_choice | HTTP・履歴の受理 | schema に合う JSON |
|---|---|---|---|
| signal 15 回目 (切替文 + schema) | 両方省略 | 受理、choices/message 読める | 適合 (tool 呼出しなし) |
| cron 15 回目 | 両方省略 | 受理 | 適合 |
| signal 16 回目 (構造化呼出しへの停止結果 + 再掲文) | 両方省略 | 受理 | 適合 |
| signal 16 回目 (文字列呼出し + 修復文と再掲の統合) | 両方省略 | 受理 | 適合 |
| signal 16 回目 (JSON 不在 + 修復文と再掲の統合) | 両方省略 | 受理 | 適合 |
| cron 16 回目 (構造化呼出しへの停止結果) | 両方省略 | 受理 | 適合 |
| cron 16 回目 (文字列呼出し + 統合) | 両方省略 | 受理 | 適合 |

決定: 最終段階の要求は tools と tool_choice の両方を省略する (none 方式・auto 維持は試さなかった。省略方式が合格したため)。
切替文は「ツールは使えない。JSON だけを出す。」に出力 schema を続ける形で足りた (強化文は不要)。
16 回目の構造化・文字列の呼出し枝は自然発生しなかったため、履歴側を合成して要求の受理だけを確かめた。
応答は全件が tool 呼出しなしの JSON だった (`measured`)。この結果は 1 つのモデル・1 回ずつの観測で、
モデルが常に従う保証ではない。
