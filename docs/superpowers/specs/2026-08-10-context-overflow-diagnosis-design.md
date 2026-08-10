# 設計: コンテキスト超過の診断 (ローカル LLM)

**位置づけ**: プラン 9 の task として実装する。本書はその spec。
**日付**: 2026-08-10 (改訂 3 — codex レビュー全件反映 + §4.5 のユーザー裁定を確定)
**裁定**: ユーザー承認済み。案「実行時に正確に診断する + init で `n_ctx` を可視化する」を採用。
送信前の見積もりによる予防は**採らない** (理由は §3)。

レビュー記録: `.superpowers/sdd/2026-08-10-context-overflow-diagnosis/spec-review-codex.md`

---

## 1. 解く問題

`LocalRunner` はローカル LLM に `{model, messages, tools, tool_choice}` しか送らない。
`max_tokens` もコンテキスト系パラメータも無く、**agentic-fx 側にコンテキストの知識がゼロ**である。
`max_turns` (16) は往復回数、`worker.transcript_max_bytes` (1MB) は transcript の保存上限で、
どちらもコンテキスト予算ではない。

コンテキストを超えると llama.cpp は **HTTP 400 と機械可読な診断**を返す (実測):

```json
{"error":{"code":400,
  "message":"request (90010 tokens) exceeds the available context size (65536 tokens), try increasing it",
  "type":"exceed_context_size_error",
  "n_prompt_tokens":90010, "n_ctx":65536}}
```

ところが `LocalRunner` は `except httpx.HTTPError` で受けて `safe_error_text(e)` を記録するため、
**ログに残るのは `HTTPStatusError: HTTP 400` の一行だけ**である (実測で確認)。
運用者に届くのは `[agentic-fx] 判断 Mission 失敗: failed` のみで、コンテキスト超過なのか、
モデル不在なのか、リクエストが壊れているのかも区別できない。

**問題は設定の不足ではない。サーバーが只でくれる正確な診断を捨てていることである。**

## 2. 実測した根拠

| 測定 | 結果 | 効き方 |
|---|---|---|
| 超過時の応答 | HTTP 400 / `type=exceed_context_size_error` / `n_prompt_tokens` / `n_ctx` | 診断は**サーバー由来で正確**。推測が要らない |
| 現状のログ出力 | `HTTPStatusError: HTTP 400` | 上記が**全部捨てられている** |
| 超過 400 が返るまで | **14.46 秒** (527KB 送信) | 反応後手のコスト。300 秒 timeout よりは軽いが無料ではない |
| `GET /props?model=<id>` | `default_generation_settings.n_ctx` = 65536 | 実行時に取得できる |
| `/props` の副作用 | **モデルをロードする** (gemma3-4b: unloaded → loaded、4.37 秒) | init での順序設計が要る (§4.4) |
| Mission 失敗の既存出口 (trade) | `activity.write(AGGREGATE, "mission_failed", ...)` + `notifier.send(...)` | **出口は既にある。中身が無いだけ** |
| Mission 失敗の既存出口 (reflection) | **無い** (`return False` のみ) | §4.5 |

## 3. 採らなかった案とその理由

**送信前にプロンプト長を見積もって予防する案は採らない。**

- **見積もりは原理的に不正確**。`chars/4` は JSON の tool schema や日本語で大きく外れる。
  正確にやるなら `/tokenize` だが、ターンごとの往復が増え、モデルのロードも要る
- **保守的に見積もると、成功したはずの Mission を落とす**。これは
  `task20-gather-deadline-design.md` が codex に Critical を受けた「非損失性」の穴と同型である
- **`n_ctx` をキャッシュすれば llama-swap 側の `--ctx-size` 変更で陳腐化する**。
  config キーに持てば、`.bashrc` の `gen-ctx.awk` で手作業同期しているのと同じドリフトを
  agentic-fx 内にも作ることになる
- **予防を入れても本設計は不要にならない**。見積もりが楽観側に外れればサーバーはやはり拒否するので、
  実行時の診断はどちらにせよ床として要る

情報の質が違う — **本設計の数値はサーバーが只でくれる正解、見積もりはこちらが金を払って作る推測**である。

## 4. 設計

### 4.1 検知 — `LocalRunner`

**status code で分岐しない。** `HTTPStatusError` の**全 status** について、応答本文を
JSON error envelope として解析する。理由は codex I4:
llama.cpp の版差・llama-swap・リバースプロキシ・OpenAI 互換層が、同じ
`error.type=exceed_context_size_error` を **413 / 422 / upstream 500** で返しうる。
status code で判定すると、同じ機械可読な正解を再び捨てることになる。

**コンテキスト専用の文言は `error.type` で判定する** (status code では判定しない):

```
context exceeded: prompt 90010 tokens > n_ctx 65536 (model=qwen3.6-35b-a3b_Q4)
```

- `error.type` がそれ以外でも、`error.message` があれば拾って理由にする
- **本文の解析にはサイズ上限を設ける。** 上限超過・JSON でない・キー欠落のいずれでも
  **例外を出さず**、従来どおりの汎用文言 (`HTTPStatusError: HTTP <code>`) に退避する
- 接続エラー等 response を持たない `HTTPError` は従来文言のままとする

**安全化 (codex I5 — 改訂 1 の型が誤っていた)**:

- サーバー本文由来の文字列には **`safe_text(text: str)`** を使う。
  `safe_error_text` は `BaseException` 用であり、文字列を渡すのは契約違反である
  (`_safe_error.py:47` と `:58` で確認)
- `reason` は**単一行**に正規化する (改行・制御文字を除去)
- **文字数 (または UTF-8 バイト数) の上限を定める。** 無制限だと activity 一行・
  Discord 通知・worker の result frame を肥大化させる
- コンテキスト専用文言に使う値は **`str` / bool を除く正整数**に型を絞る。
  不正な型なら汎用の `error.message` に降格する
- **レスポンス本文そのものは transcript にもログにも保存しない**

**`status` は `"failed"` のまま**にする。新しい status 値は作らない。

**根拠 (codex M1 で表現を訂正)**: 技術的に不可能だからではない —
`missions.status` は `TEXT NOT NULL` で CHECK 制約が無く、`mission_protocol` にも
status の厳格スキーマは無いので、値の追加自体は通る。避ける理由は
**「runner 非依存の 4 終端契約・呼び出し側の意味論・集計互換性を不必要に広げるから」**であり、
かつ**「なぜ失敗したか」と「どの終端状態か」は別の軸**だからである。

### 4.2 運搬 — `MissionResult.reason`

```python
@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None        # 追加
```

既定 `None` なので既存の呼び出しは無変更である
(codex が全生成・消費点を洗って確認済み: production の `AgentRunner` 実装は
`LocalRunner` / `WorkerRunner` / `FakeRunner` の 3 つのみ)。

**worker 経由も通すこと。これは必須である** — `LocalRunner` は `WorkerRunner` 経由では
**子プロセスの中で走る**ため、プロトコルを通さないと親に届かない。

- `mission_worker` が `result` フレームに `reason` を載せる
- `WorkerRunner` が `payload.get("reason")` で読み、`MissionResult` に渡す
- `mission_protocol.py` は frame が dict であることと seq しか検証しないため、
  キーの追加は既存 reader に対して非破壊である (実コードで確認)

### 4.3 本 task の `reason` の適用範囲 (codex I6)

**本 task が保証するのは「`LocalRunner` が解釈できた HTTP failure の `reason`」に限定する。**

以下は**本 task の範囲外**とし、spec に明記する:

- `WorkerRunner` が親側で生成する失敗 (startup timeout / worker timeout / protocol error / EOF)。
  現状これらは `else: # eof / protocol_error / error — すべて failed に正規化` で
  **子の `error` フィールドごと捨てている** (`worker_runner.py:252` で確認)。
  昇格するなら安全化と上限のテストが別途要るため、本 task には混ぜない
- `ClaudeRunner` (未実装) 固有のエラー。**予測実装してはならない**

ただし将来の実装が無理由のまま入るのを防ぐため、以下を本 task に含める:

- `AgentRunner` の docstring に規範を書く:
  **「runner は `failed` / `timeout` / `max_turns` を返すとき、可能な限り安全化済みの
  `reason` を設定する。外部応答の本文を生で入れない」**
- **プラン 9 の ClaudeRunner task の blocking チェックリストに契約テストを追加する**

### 4.4 init での可視化 — `_check_llama_swap` (codex I1 / I2)

**現行は trade モデル 1 つしか見ていない** (`service.py:79` で `model = settings.runner.trade.model`)。
したがって改訂 1 の「smoke 済みだから追加コストはほぼゼロ」は improve モデルには成立しない。

**対象モデルは重複除去した順序付きリストとして定義する。**

| 条件 | 手順 |
|---|---|
| `trade.model == improve.model` | 存在確認 → cold-load smoke → `/props` を**各 1 回だけ** |
| 異なる | **improve を先に** `/props` (表示のみ)、**trade を最後に**存在確認 → smoke → `/props` |

**trade を最後に置くのは意図的である** — llama-swap の常駐数・VRAM・TTL 次第では
後発のロードが先発を unload しうるため、init 終了時に**取引判断で使うモデルが hot な状態**で
終わらせる。improve モデルが trade と異なる場合、**init に cold load 1 回分の時間が増える**
ことを spec の既知コストとして明記する。

表示:

```
llama-swap OK (model 'qwen3.6-35b-a3b_Q4' loaded, ctx 65536)
improve model 'X' ctx 65536
```

**例外境界を明示する (codex I2)**。`/props` 1 モデル分を小さな関数に分離し、
以下だけを「取得失敗」として限定捕捉する:

- `httpx.RequestError` / `httpx.HTTPStatusError`
- JSON decode の `ValueError`
- 形状不正の `TypeError` / `KeyError`

**無差別な `except Exception` は使わない** — `service.py` の
「実装バグまで警告に落とさない」既存方針を壊すため。取得した `n_ctx` は
**bool を除く正整数**であることを検証し、不正なら表示しない。

- **警告止まりの方針は維持する** — 取得に失敗しても init は成功させる
- 取得した `n_ctx` は**保存しない**。表示のみ (§3 のドリフト回避)

### 4.5 出口 — 既存の通知に載せる

#### trade loop

新しい出口は作らない。既存箇所に `reason` を足すだけ:

```python
activity.write(Category.AGGREGATE, "mission_failed",
               f"runner status={result.status}" + (f" — {result.reason}" if result.reason else ""),
               ref_id=str(mid))
notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}" + ...)
```

#### reflection loop — **失敗を一切記録していない** (ユーザー裁定 2026-08-10: **(a) 本 task に含める**)

> **裁定済み**。codex 推奨の (a) をユーザーが採用。activity 書込は本 task に含め、
> **再試行ポリシーの変更は含めない** (別途起票)。

`reflection_cycle.py:171` は失敗時に `return False` するだけで、activity も通知も無い。
したがって reflection Mission でコンテキスト超過が起きると**完全に不可視**である。

**本 task に `reflection_mission_failed` の activity 書込を新設する。**
記録するのは `order id` / `mission id` / `status` / 安全化済み `reason`。**通知は出さない**
(reflection の失敗は資金に直結しないため activity で足りる)。

##### 同時に判明した、より重い問題 (codex I3 — 本 task では**直さない**)

対象選択は `WHERE o.status='closed' AND r.order_id IS NULL ORDER BY o.id LIMIT ?`
(`reflection_cycle.py:76-78` で確認)。恒久的に失敗する order は `reflections` 行を作らないため:

1. **毎周期同じ order が選ばれ続ける** — 回数上限も backoff も失敗マーカーも無い
2. **試行ごとに新しい `missions` 行が `failed` で終端して増え続ける**
3. `ORDER BY o.id LIMIT 3` なので、**古い失敗 order が先頭を占有し後続の closed order が starvation する**

**再試行ポリシーの変更は本 task に混ぜない** (診断の課題とは別のポリシー問題である)。
本 task では**現挙動を spec に明記し、回帰テストで固定する**にとどめ、
**無制限再試行・starvation・DB 増大は独立の課題として起票する**。

### 4.6 永続化について (codex M3)

**`reason` は `missions` テーブルに保存しない。** 運用診断として activity / 通知に限定する。
`finalize_mission` は status を変換せず `missions.finish` へ渡すだけで、
`missions` に理由列は無い (`store/missions.py` / `store/db.py` で確認)。

したがって「notifier 停止中 + activity ローテート後に DB だけを見る」監査では
`reason` は復元できない。**永続監査が必要なら `failure_reason` 列を別途起票する。**
`MissionResult` 全体が保存されると読める記述を残さない。

### 4.7 非対象 (codex M4)

**本 task は non-streaming transport の HTTP error envelope のみを対象とする。**
現行 `LocalRunner` は `stream` を送らず応答全体を読んでから `raise_for_status` / `json` を
呼ぶため、SSE 途中で超過が現れる経路は存在しない。将来 `stream=true` を導入する task で、
status 200 後の SSE error event と途中切断を別設計とする。

## 5. 変えないもの

- `MissionResult.status` の 4 値
- 送信前の見積もり・トークナイザ・`n_ctx` のキャッシュ・config キーの追加 — **一切入れない**
- 決定論的コア (`risk_gate.py` / `paper_broker.py` / `transitions.py`)
- `_check_llama_swap` が警告止まりであること
- **reflection の再試行ポリシー** (§4.5 — 起票のみ)
- `WorkerRunner` 親側失敗・`ClaudeRunner` の理由付け (§4.3 — 範囲外)

## 6. 変更対象

| ファイル | 変更 |
|---|---|
| `runners/local_runner.py` | HTTP error envelope の解析と `reason` の組み立て・安全化 |
| `runners/base.py` | `MissionResult.reason` の追加 + `AgentRunner` docstring の規範 |
| `mission_worker.py` | `result` フレームに `reason` を載せる |
| `runners/worker_runner.py` | `reason` を読んで `MissionResult` に渡す |
| `loops/trade_loop.py` | `mission_failed` の activity と通知に `reason` を載せる |
| `loops/reflection_cycle.py` | `reflection_mission_failed` の activity 書込を新設 |
| `service.py` | `_check_llama_swap` にモデル重複除去・順序・`/props` 表示を追加 |
| `_safe_error.py` | 変更なし (`safe_text` を使う) |

## 7. テストと変異 (codex I7 — 検査点ごとに独立させる)

**1 つのテストに複数の assert を並べてはならない。** 先に落ちる assert が後続を短絡させ、
後続の検査点が無検証のまま残る (プラン 8 Task 20 で実測した穴。テスト内の `assert` は
production の早期 `return` とまったく同じように後続を短絡させる)。

| # | 独立テスト | 殺す変異 |
|---|---|---|
| 1 | `error.type` によるコンテキスト判定 | `type` 判定を無視して常に ctx 文言にする |
| 2 | `reason` に `n_prompt_tokens` が入る | prompt 数値の抽出を削除 |
| 3 | `reason` に `n_ctx` が入る | n_ctx の抽出を削除 |
| 4 | `reason` に model が入る | model の付与を削除 |
| 5 | `safe_text` による安全化・単一行化・長さ上限 | 安全化を外す / 上限を外す |
| 6 | 超過でも `status` は `"failed"` のまま | status を別値にする |
| 7 | **非 400 (413 / 422) の同一 envelope も解析される** | `status_code == 400` に限定する |
| 8 | 本文が JSON でない / キー欠落 → 例外を出さず汎用文言 | パースを無防備にする |
| 9 | 本文が上限超過 → 汎用文言に退避 | 上限判定を削除 |
| 10 | **子が `result` frame に `reason` を載せる** | frame から `reason` を落とす |
| 11 | **親が frame の `reason` を `MissionResult` に載せる** | 親の読み取りを削除 |
| 12 | 未知キーを含む `result` frame でも read/dispatch できる (codex M2) | reader を厳格化する |
| 13 | trade の `mission_failed` activity に `reason` が載る | activity 側で無視する |
| 14 | trade の通知に `reason` が載る | 通知側で無視する |
| 15 | `reflection_mission_failed` activity が書かれ `reason` が載る | 書込を削除 |
| 16 | **reflection の現挙動の固定**: 同一 order で 2 回 `run_pending` → `failed` の missions 行が 2 本・failure activity が 2 本・`reflections` 行は 0 | 再試行挙動を無言で変える |
| 17 | init: `trade.model == improve.model` なら `/props` は 1 回だけ | 重複除去を削除 |
| 18 | init: 異なるなら improve → trade の順で、trade が最後 | 順序を入れ替える |
| 19 | init: `/props` の HTTP 失敗でも init は成功 | 例外境界を広げる / 握らない |
| 20 | init: `/props` の形状不正 (`n_ctx` が文字列・欠落・bool) でも init は成功し表示しない | 型検証を削除 |
| 21 | init: 想定外例外は握らず init を落とす | `except Exception` にする |

**変異は 1 つずつ独立に当て、その変異を殺した固有のテスト名を記録した
mutation ledger を受入成果物とする。**「何本落ちたか」ではなく
「狙った検査点のテストが落ちたか」を見る。

## 8. 受入条件

1. **trade loop**: コンテキスト超過時に、運用者が **activity と通知だけで「超過だった」と分かる**
2. **reflection loop**: コンテキスト超過時に `reflection_mission_failed` の activity が残る
   (通知は出さない)
3. 表示される `n_prompt_tokens` / `n_ctx` が**サーバーの応答そのまま**である (再計算・推測をしない)
4. **非 400 の status でも同一 envelope なら診断が拾える**
5. `reason` は**単一行・上限内・安全化済み**であり、応答本文そのものはどこにも保存されない
6. worker profile (子プロセス実行) でも `reason` が親に届く
7. `init` が実際の `n_ctx` を表示し、**取得に失敗しても init は成功する**。
   想定外例外は握らない
8. 既存テストが 1 本も壊れない
9. reflection の**無制限再試行・starvation・`missions` 行の増大**が独立課題として起票されている
   (本 task では直さないが、現挙動が回帰テストで固定されている)
