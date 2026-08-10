# 設計: コンテキスト超過の診断 (ローカル LLM)

**位置づけ**: プラン 9 の task として実装する。本書はその spec。
**日付**: 2026-08-10
**裁定**: ユーザー承認済み (2026-08-10)。案「実行時に正確に診断する + init で `n_ctx` を可視化する」を採用。
送信前の見積もりによる予防は**採らない** (理由は §3)。

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
| `/props` の副作用 | **モデルをロードする** (gemma3-4b: unloaded → loaded、4.37 秒) | init の cold-load smoke の**後**なら追加コストはほぼゼロ |
| Mission 失敗の既存出口 | `activity.write(AGGREGATE, "mission_failed", ...)` + `notifier.send(...)` | **出口は既にある。中身が無いだけ** |

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

400 応答の本文 JSON を読み、理由文字列を組み立てる。

- `error.type == "exceed_context_size_error"` のとき:

  ```
  context exceeded: prompt 90010 tokens > n_ctx 65536 (model=qwen3.6-35b-a3b_Q4)
  ```

- それ以外の HTTP エラーでも、本文に `error.message` があれば拾う
  (現状は種別を問わず `HTTPStatusError: HTTP 400` の一行になっている)
- **`safe_error_text` は通す**。本文に秘密が混ざる可能性を残さない
- **本文が JSON でない / キーが欠けている場合も落ちない**。従来どおりの汎用文言に退避する

**`status` は `"failed"` のまま**にする。新しい status 値は作らない:

- `MissionResult.status` は `Literal["completed","failed","timeout","max_turns"]` であり、
  値の追加は `finalize_mission` / worker protocol / missions テーブルに波及する
- 「なぜ失敗したか」と「どの終端状態か」は別の軸である。前者を後者に混ぜない

### 4.2 運搬 — `MissionResult.reason`

```python
@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None        # 追加
```

既定 `None` なので既存の呼び出しは無変更。

**worker 経由も通すこと。これは必須である** — `LocalRunner` は `WorkerRunner` 経由では
**子プロセスの中で走る**ため、プロトコルを通さないと親に届かない。

- `mission_worker` が `result` フレームに `reason` を載せる
- `WorkerRunner` が `payload.get("reason")` で読み、`MissionResult` に渡す
- `mission_protocol.py` に厳格スキーマは無いことを実コードで確認済みなので、
  キーの追加は破壊的変更にならない

### 4.3 出口 — 既存の通知に載せる

新しい出口は作らない。`trade_loop` の既存箇所に `reason` を足すだけ:

```python
activity.write(Category.AGGREGATE, "mission_failed",
               f"runner status={result.status}" + (f" — {result.reason}" if result.reason else ""),
               ref_id=str(mid))
notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}" + ...)
```

### 4.3.1 `ReflectionCycle` は失敗を**一切記録していない** (実コードで確認)

`reflection_cycle.py:171` は失敗時に `return False` するだけで、activity も通知も無い:

```python
if result.status != "completed" or not finalize_ok:
    return False
```

したがって **reflection Mission でコンテキスト超過が起きると完全に不可視**である。
`run_pending` が次周期に同じ order を再試行するので、**永久に静かに失敗し続ける**。
`§8 受入条件 1` は reflection loop については現状**達成できない**。

**この節はユーザー裁定待ち** — 以下のいずれかを選ぶ:

- **(a) 本 task の範囲に含める**: `reflection_mission_failed` の activity 書込を新設し、
  `reason` を載せる。通知を出すかは別途決める (reflection の失敗は資金に直結しないため、
  activity のみでも筋は通る)
- **(b) 範囲外にして別途起票する**: 本 task は trade loop のみを対象にし、
  reflection loop の失敗が無記録である件は独立した可観測性の課題として記録する

**(a) を推奨する。** 超過の診断を運搬する配線を作りながら、2 つある Mission 経路の片方で
それが着地しないのは中途半端であり、`reason` を運ぶ意味が半減する。追加は activity 書込
1 箇所とそのピン 1 本で済む。

### 4.4 init での可視化 — `_check_llama_swap`

cold-load smoke の**後**に `GET /props?model=<id>` を引き、`n_ctx` を表示する。

```
llama-swap OK (model 'qwen3.6-35b-a3b_Q4' loaded, ctx 65536)
```

- smoke で既にロード済みなので追加コストはほぼゼロ
- **警告止まりの方針は維持する** — 取得に失敗しても init は成功させる
  (オフライン初期化を通すという既存の設計方針)
- `runner.trade.model` と `runner.improve.model` が異なる場合は**両方**表示する
- 取得した `n_ctx` は**保存しない**。表示のみ (§3 のドリフト回避)

## 5. 変えないもの

- `MissionResult.status` の 4 値
- 送信前の見積もり・トークナイザ・`n_ctx` のキャッシュ・config キーの追加 — **一切入れない**
- 決定論的コア (`risk_gate.py` / `paper_broker.py` / `transitions.py`)
- `_check_llama_swap` が警告止まりであること

## 6. 変更対象

| ファイル | 変更 |
|---|---|
| `runners/local_runner.py` | 400 本文の解析と `reason` の組み立て |
| `runners/base.py` | `MissionResult.reason` の追加 |
| `mission_worker.py` | `result` フレームに `reason` を載せる |
| `runners/worker_runner.py` | `reason` を読んで `MissionResult` に渡す |
| `loops/trade_loop.py` | `mission_failed` の activity と通知に `reason` を載せる |
| `loops/reflection_cycle.py` | **§4.3.1 の裁定次第** — (a) なら失敗の activity 書込を新設 / (b) なら変更なし |
| `service.py` | `_check_llama_swap` に `n_ctx` 表示を追加 |

## 7. テストと変異

| # | テスト | 殺す変異 |
|---|---|---|
| 1 | 超過 400 の本文 → `reason` に prompt / n_ctx / model が入り、`status` は `"failed"` | `reason` の抽出を削除 |
| 2 | 別 `type` の 400 → `reason` は入るが ctx 用の文言にはならない | `type` 判定を無視して常に ctx 文言にする |
| 3 | 本文が JSON でない 400 → 例外を出さず従来どおりの文言 | パースを無防備にする |
| 4 | **`reason` が worker の `result` フレームを通って親に届く** | フレームから `reason` を落とす |
| 5 | `mission_failed` の activity と通知に `reason` が載る | `trade_loop` が `reason` を無視する |
| 6 | init が `n_ctx` を表示する / `/props` 失敗でも init は成功する | `/props` 呼び出しを削除する |

**4 は配線そのものの検証である。** 1〜3 が緑でも「誰からも運ばれない」は残るため必ず 1 本置く
(プラン 8 Task 7 で確定した規律 — 単体テストは契約を pin するが配線を pin しない)。

**変異は 1 つずつ独立に当てること。** 検査点をまとめて 1 本のテストで見ると、
先に落ちる assert が後続を短絡させて後続の検査点が無検証のまま残る
(プラン 8 Task 20 で実測した穴)。

## 8. 受入条件

1. コンテキスト超過時に、運用者が **activity と通知だけで「超過だった」と分かる** — ログを掘らない
   (reflection loop については §4.3.1 の裁定に従う。(b) を選んだ場合、本条件は trade loop のみに適用)
2. 表示される `n_prompt_tokens` / `n_ctx` が**サーバーの応答そのまま**である (再計算・推測をしない)
3. worker profile (子プロセス実行) でも `reason` が親に届く
4. `init` が実際の `n_ctx` を表示し、**取得に失敗しても init は成功する**
5. 既存テストが 1 本も壊れない
