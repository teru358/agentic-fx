# [approval-parent-facts] 設計書 v1.0

作成日: 2026-10-02。対象は、改善 mission が作る plugin 承認依頼に含まれる agent の説明を、検証済みの承認材料と取り違えずに表示する仕組みである。既存の approval <id> の表示面を拡張し、承認可否の計算又は approve / reject の実行手順は変更しない。

## 要点

1. approval <id> は、親プロセスが受理・計測・状態遷移で記録した構造化値を先に表示し、agent の自由文を「agent の自己申告 (未検証)」欄へ隔離する。
2. parent_facts には agent 自由文を入れない。agent 起票の課題文を残す必要があるときも、自己申告欄の固定接頭辞付き引用だけに置く。
3. 同じ課題の過去の完了 run を示す。observation で終わった result=null は正規の履歴であり、「観測のみ (承認依頼なし)」と表示する。
4. 表示は保存形式が壊れていても fail-soft とする。payload root、親欄、metrics を独立に検査し、読めない欄だけを不正形式表示にして残りを続ける。
5. approval <id> が表示する全ての文字列は、Unicode の Cc / Cf を除去し、1 行・長さ上限へ正規化する。自然文の機械照合・警告・ブロックは追加しない。

## 背景と実例

agent の explanation は、人が承認判断の背景を理解する助けになる。しかし、agent が申告する試行回数、過去の実行、成績、又は選択理由を親がすべて検証できるとは限らない。

次の二つは合成例である。

|事実|agent の文|親が確認できる記録|表示上の扱い|
|---|---|---|---|
|課題が今回初めて選ばれた|「以前にも数回試した」|backlog の attempts は今回を含め 1|agent の自己申告として残す。回数の警告は出さない。|
|候補が承認依頼になった|「検証後に再実行し、良い成績だった」|親は受理済み backtest の構造化 ledger を持つが、文中の手順全体は持たない|ledger は親欄、説明文は自己申告欄に分ける。|

従来の approval <id> には、この区別を表示する面がなかった。agent の rationale / summary は payload に保存される一方、親が受理した試行、選定回数、過去 run を同じ画面で読めなかった。

## 守るべきものと現行

|守るもの|現行の手段|本設計で維持・追加するもの|
|---|---|---|
|承認可否|既存の in_sample / holdout / floor|計算・gate・approve / reject の実行経路を変更しない。|
|説明可能性|agent の rationale / summary|未検証の補助情報として残す。|
|監査可能な事実|ledger、backlog の状態、improvement run|親が受理・計測・状態遷移で記録した構造化値だけを parent_facts に保存する。|
|端末表示の安全性|一部の payload 値を直接表示|全表示文字列の無害化、型検査、field-level fail-soft を追加する。|
|後方互換|旧 payload に facts がない|旧形式であることと自己申告が未検証であることを 1 行で示す。|

親の記録とは「親が保存した文字列」ではない。親が受理、計測、又は状態遷移で記録した構造化値である。agent が同じ backtest を再実行するよう依頼でき、どの課題を選ぶかも決められるため、回数や選択は事実であっても agent から完全に独立した証拠ではない。

## 候補比較と選定

|候補|未検証文の扱い|説明を残す|結論|
|---|---|---|---|
|自然文を機械照合する|表現外の主張を取り逃がし、警告なしを正しさと誤読し得る|残る|不採用|
|親の記録と自己申告を分離する|親が扱える構造化値だけを根拠として先に示す|残る|**採用**|
|rationale を全面的に構造化する|自由文が残るか、schema の変更が広い|一部残る|不採用|
|rationale を廃止する|隔離できる|失う|不採用|

採用する分離案は、未検証文を正しいとも誤りとも判定しない。人は親欄を根拠にし、自己申告を補助情報として読む。個別の自然文を照合する動的な警告は追加しない。

## 動作仕様

### payload の追加項目

既存 payload のキーを維持し、次を追加する。

~~~
{
  "facts_version": 1,
  "parent_facts": {
    "backlog": {"id": 101, "attempts": 1, "status": "selected"},
    "prior_runs": [{"run_id": 8, "result": null, "approval_id": null}],
    "trials": [{"n": 1, "content_hash8": "1a2b3c4d",
                "same_hash_as_submitted": true, "trades": 100,
                "pf": 1.2, "avg_r": 0.1, "max_drawdown": 0.05}],
    "trials_omitted": 0,
    "analysis_call_count": 0
  },
  "agent_claims": {
    "selection_rationale": "選択・変更の意図",
    "summary": "候補の概要",
    "selected_backlog_idea": "agent 起票の課題文"
  }
}
~~~

この例は合成値である。parent_facts のキー集合は厳密に backlog、prior_runs、trials、trials_omitted、analysis_call_count とする。backlog は null 又は id、attempts、status だけを持つ。trial は n、content_hash8、same_hash_as_submitted、trades、pf、avg_r、max_drawdown だけを持つ。prior_runs の各要素は run_id、result、approval_id だけを持つ。

result は approval、report、null のみを許す。approval は正整数の approval_id を要し、report と null は approval_id=null を要する。数値は bool を拒否し、float は有限値だけを許す。status は親が書く許可語彙だけを許す。

### 収集の時点

親プロセスは approval を作る Tx-2 の同一 transaction で、frozen ledger、選択済み backlog 行、同じ backlog の完了済み run を読み、parent_facts を payload と同時に保存する。

過去 run は現在の run を除外し、finished_at がある行だけを新しい順に最大 10 件取る。result で絞り込まない。accepted run_backtest ledger entry は受理順に最大 20 件を trials へ入れ、超過数を trials_omitted に入れる。timeout などで親が受理しなかった RPC は trial に含めない。

agent / research 起票の selected 行だけ、agent_claims.selected_backlog_idea に課題文を置ける。source 自体は保存・表示しない。user / system 起票行は引用を持たない。

selection_rationale、plugin summary、selected / discoveries の文、observation reason、artifact summary、backlog の idea / source / last_result は parent_facts のどの深さにも入れない。

### 表示の文面例

approval <id> は既存 header と metrics の直後に親欄、次に自己申告欄を出す。次は実際に画面へ出る合成例である。

~~~
approval #<id> kind=plugin status=pending
name=<sanitized> content_hash=<sanitized> eval_timeframe=<sanitized>
in_sample: ...
holdout: ...
--- 親が受理・記録した内容 ---
backlog #<id> (今回を含め attempts=1) status=selected
過去の完了 run:
  run #8: 観測のみ (承認依頼なし)
mission 内の backtest 1 件 (in_sample の受理分) / 分析 0 件:
  1. hash=1a2b3c4d (提出と同一) trades=100 pf=1.2 avg_r=0.1 mdd=0.05
注記: backtest の回数と同じ内容の再実行は agent が依頼したもので、試行の独立性を示しません。どの課題を選ぶかは agent が決めています。
--- agent の自己申告 (未検証。親は内容の真偽を確認していない) ---
  自己申告: selection_rationale: <sanitized value>
  自己申告: summary: <sanitized value>
  引用: 選んだ課題の文面 (agent 起票): <sanitized value>
~~~

result=approval は「承認依頼 #<approval_id>」、result=report は「報告として完了 (承認依頼なし)」、result=null は「観測のみ (承認依頼なし)」と固定語で表示する。parent_facts に agent の文字列を入れず、固定注記は常に親欄の末尾に置く。

### 無害化

表示する全ての文字列を一つの display sanitizer に通す。name、content_hash、eval_timeframe、metrics の pair label、reason、decided_by、floor 関連文字列、依存 plugin 名、archive path、親欄、自己申告欄が対象である。

sanitizer は Unicode category Cc と Cf を除去する。C0/C1、ESC、U+202A–U+202E、U+2066–U+2069、その他の format control を含む。次に CR / LF / TAB を空白へ正規化し、連続空白を畳み、空値は - とする。自由文は保存時 2,000 文字、表示時 600 文字に制限し、その他の単値欄も 1 行上限を持つ。切捨てには固定接尾辞を付ける。全角の紛らわしい文字は除去しない。非文字列を str() で表示しない。

自己申告は固定接頭辞の後ろにだけ表示する。これにより agent が改行、区切り線、見出し風の文字列を送っても、親欄を偽装できない。

### fail-soft

表示 parser は JSON decode、payload root、親欄、metrics を独立に型検査する。payload root が dict でない（list、文字列、null、数値）場合も例外にせず、DB 行由来の header を表示した上で次を出す。

~~~
payload: (保存形式が不正で表示できません)
~~~

in_sample / holdout の root、per-pair metrics、metrics 値は個別に検査する。不正なら当該行だけを次に置換し、他の表示を続ける。

~~~
in_sample: (保存形式が不正で表示できません)
~~~

facts_version が厳密な整数 1 で parent_facts が不正なら、親欄だけを次の 1 行にする。読める自己申告、reason、archive は続ける。

~~~
--- 親が受理・記録した内容 --- (保存形式が不正で表示できません)
~~~

facts_version が無い、又は厳密な整数 1 以外なら、現在の DB から facts を推測・補完しない。次の 1 行を出して旧キー由来の自己申告を続ける。

~~~
--- 親が受理・記録した内容 --- 旧形式の承認依頼です。親の事実表は保存されていません。以下の自己申告は未検証です。
~~~

approve / reject は表示用 parser を通らない。表示の破損は決定可否を変えない。

### prompt の是正

agent 出力 schema は変えない。prompt は observation と承認に転載される自由文を明確に分ける。

~~~
observation の reason には、試したパラメータと得られた pf / avg_r を具体的に書く。
ただし承認画面に転載される selection_rationale と plugin の summary には、
試行回数・過去回数・pf / avg_r / trades 等の成績を書かない。そこには選択・変更の意図だけを書く。
~~~

observation の記録要求は維持し、rationale / summary の例から回数・成績を除く。holdout と approval payload を agent prompt / context に渡す経路は作らない。

## 不変条件

|ID|不変条件|
|---|---|
|IV-1|in_sample / holdout / floor の承認可否計算は変更しない。|
|IV-2|parent_facts は親が受理・計測・状態遷移で記録した構造化値だけであり、agent 自由文を再帰的に含まない。|
|IV-3|prior_runs は同 backlog、今回の run 除外、完了済みだけであり、approval / report / null の result を保存・表示する。|
|IV-4|親欄には試行の独立性と選択主体を限定する固定注記を置く。動的な自然文照合・警告・ブロックは追加しない。|
|IV-5|表示関数を通る全ての文字列は同じ sanitizer と 1 行・長さ制限を通る。全角類似文字は除去しない。|
|IV-6|新形式、旧形式、root / nested / metrics の不正形式のいずれでも approval <id> は例外にしない。読めない欄だけを不正形式表示にする。|
|IV-7|approve / reject は表示 parser を呼ばず、表示不能によって決定を妨げない。|
|IV-8|agent 起票課題文を残すなら agent_claims にだけ置き、固定の引用接頭辞を付ける。|

## 受入条件

|ID|条件|検証|
|---|---|---|
|AC-1|accepted run_backtest の採番と上限|3 件を受理順に表示し、error / timeout を含めない。20 件超は先頭 20 件と trials_omitted に分ける。|
|AC-2|キー集合の固定|parent_facts、backlog、trial、prior_runs 要素のキー集合が仕様と完全一致し、許可外キーを拒否する。|
|AC-3|過去 run の null|同一 backlog の approval、report、result=null の完了済み別 run だけを表示する。今回、未完了、別 backlog は除外し、null は「観測のみ (承認依頼なし)」になる。|
|AC-4|prior_runs validator|approval は正整数の approval_id、report / null は null approval_id を要求する。未知 result、bool、文字列、矛盾する id は親欄だけを不正形式表示にする。|
|AC-5|agent 文の混入防止|selection_rationale、plugin summary、selected 文、discoveries の idea / evidence / source、observation reason、artifact summary、backlog の idea / source / last_result に相異なる目印を入れる。parent_facts を再帰走査して全目印が無いことを assert する。|
|AC-6|agent 起票引用|agent / research 起票だけが selected_backlog_idea を持ち、固定接頭辞付きで表示される。user / system 起票には引用がない。|
|AC-7|payload root の fail-soft|list、文字列、null、数値の payload root で approval <id> が例外にならず、payload の不正形式行を出す。|
|AC-8|parent_facts の fail-soft|root 型、必須キー、list、要素キー、有限数、bool 数値、列挙値を壊し、親欄だけが不正形式表示になって自己申告・reason・archive が続くことを確認する。|
|AC-9|metrics の fail-soft|in_sample / holdout の root 非 dict、per-pair value 非 dict、metrics の型不正ごとに、該当行だけが不正形式表示になり、他欄を表示する。|
|AC-10|旧形式|facts_version 無し、0、文字列の 1 は旧形式 1 行を出し、facts を後付けしない。|
|AC-11|端末無害化|agent claim、name、content_hash、eval_timeframe、pair label、reason、依存 plugin 名、archive path に Cc、U+202A–U+202E、U+2066–U+2069、別の Cf、CR/LF/TAB、偽の見出しを入れる。出力に制御文字が無く、1 行・上限内で固定見出しが増えず、全角類似文字は残ることを assert する。|
|AC-12|決定経路の独立|approval <id> parser を例外化又は spy 化しても approve / reject がそれを呼ばずに既存の決定経路を実行する。壊れた表示用 payload でも同じことを確認する。|
|AC-13|prompt|observation の parameter / pf / avg_r 指示と、rationale / summary だけの回数・成績禁止が共存し、例には成績がない。|
|AC-14|回帰|improve loop、approval payload、commands の既存 test suite を実行する。|

## 範囲外と関連チケット

- rationale / summary の自然文の機械照合、attempts 限定警告、承認ブロック。
- output schema の変更、rationale / summary の廃止、self-test / lock 実行の新たな台帳化。
- 別 UI、通知、approval list、既存 decided approval のバックフィル、reject reason の保存規約変更。
- 関連チケット: selection-rationale-unverified。

## task 分割と影響ファイル

|Task|単独で入る成果|主な影響ファイル|
|---|---|---|
|T1: facts 保存|Tx-2 で backlog / prior runs / ledger から facts と claims を組み、厳格 validator を置く。|improve loop、improvement runs store、新規 facts helper、tests|
|T2: 表示安全化|approval <id> の field-level fail-soft、全表示文字列 sanitizer、親欄、自己申告欄、旧形式を実装する。|commands、tests|
|T3: prompt 是正|限定文と安全な例へ更新する。|improve mission prompt、tests|

T1 と T3 は旧形式 fallback により独立して導入できる。T2 は approve / reject の実行経路を変更しない。

## 残るリスク

- 人間が親欄を読まず自己申告だけを読むことは防げない。表示順、未検証見出し、固定引用接頭辞、固定注記で緩和する。
- 親が記録しない再実行手順などの説明は検証不能のままである。
- timeout で親が受理しなかった RPC は trial 表に出ない。
- parent facts は事実であっても、agent が backtest 回数と課題選択へ影響できる。固定注記は独立性を保証しない。
- sanitizer は表示安全策であり、DB 内の agent 文を親の事実へ昇格させない。

## 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-10-02|v1.0|親の記録と agent の自己申告の分離、過去 run の null result、field-level fail-soft、全表示文字列の無害化、固定注記、受入条件を確定した。|未検証の自然文を承認根拠と混同せず、既存の承認画面で構造化事実を読めるようにするため。||
