# llama-swap `timeout_sec` cold / warm 実測 (プラン 9 Task 18)

実施日時: 2026-08-15 20:40〜21:04 JST (2026-08-15T11:40〜12:04Z)
対象: `settings.llama_swap.timeout_sec` の採用値決定 (設計書 `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` D8)

## 環境

計測対象の設定値 (`config/settings.yaml`、gitignore の運用設定。読み取りのみ)。

```
$ uv run python -c "..."   # 実行内容は「再現手順」節
base_url= http://localhost:8080/v1
trade_model= qwen3.6-35b-a3b_Q4
improve_model= qwen3.6-35b-a3b_Q4
current_timeout_sec= 300.0
```

trade / improve は同一 alias である。したがって cold load は 1 組しか存在しない。

| 項目 | 値 | 取得元 |
|---|---|---|
| GPU / VRAM: | Intel Arc Pro B70 (OpenCL device `Intel(R) Graphics [0xe223]`)、global memory 30.3 GiB (32,530,182,144 bytes)、max compute units 256、driver 26.05.037020 | `clinfo`、`llama-server-sycl.sh` 冒頭コメント |
| 計測時点の空き VRAM | 取得手段なし (`nvidia-smi` / `xpu-smi` / `sycl-ls` いずれも不在。`/props` は叩かない方針) | — |
| quantization: | UD-Q4_K_M (`Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`、22,134,528,992 bytes) | llama-swap config `models.qwen3.6-35b-a3b_Q4.cmd` |
| simultaneous loaded model limit: | 1 (group `chats` が `swap: true` のため同グループ内は 1 モデルのみ常駐。別 group `embeds` は `swap: false` / `persistent: true` で `nomic-embed-text` が並行常駐しうる) | llama-swap config `groups:` |
| TTL setting: | 120 (秒) | llama-swap config `models.qwen3.6-35b-a3b_Q4.ttl` |
| context size | 65536 | 同 `--ctx-size` |
| llama-server 引数 | `--n-gpu-layers 99 --jinja --reasoning-format auto --parallel 2 --kv-unified --cache-ram 4096 --cache-reuse 256 --ctx-size 65536` | config macros `server` / `base` |
| llama-swap | v245 (30470a4), built 2026-07-31T04:56:21Z、`-listen 127.0.0.1:8080`、`healthCheckTimeout: 300` | `llama-swap --version` / systemd unit |
| SYCL 実行環境 | `ONEAPI_DEVICE_SELECTOR=level_zero:0`、oneAPI setvars、`bin/sycl/llama-server` | `/home/teru358/project/llm/bin/llama-server-sycl.sh` |
| service 状態 | `systemctl is-active llama-swap.service` → `active` (20:40:50 起動の pid 2094952) | systemd |

model エントリ全文 (llama-swap config):

```yaml
  qwen3.6-35b-a3b_Q4:
    cmd: |
      ${server}
      ${base}
      -m ${models-dir}/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
      --ctx-size 65536
    proxy: http://127.0.0.1:${PORT}
    ttl: 120
```

### cold 前提の確認方法

`/props?model=` はモデルをロードさせる副作用があるため使用しない。cold の前提は
`GET http://127.0.0.1:8080/running` を 10 秒間隔で polling し、対象 model が
`running` 配列に **3 回連続で存在しない**ことをもって確定した。強制 unload
(`/api/models/unload`) は使わず、TTL 120 秒の自然満了を待った。サーバ側ログにも
`Unloading model, TTL of 120s reached` が両セットで出ている (下記)。

### 計測中の他負荷 (実測環境の記録)

計測開始時、同一セッションの Task 17 検証レーン
(`t17v_run_table.py` → pytest → `python -m agentic_fx.mission_worker`、client PID 2139389)
が同じ alias に対して約 86 秒間隔で `/v1/chat/completions` を投げ続けており、
TTL 120 秒が満了しない状態が続いた。この負荷が止むまで待ってから計測した
(`quiet_unloaded` まで 620.3 秒待機)。この他負荷の観測そのものが判断材料になるため
下記「判断」に記録する。

## 計測結果

payload は 2 セット・全リクエストで同一:

```json
{"model": "qwen3.6-35b-a3b_Q4", "max_tokens": 1,
 "messages": [{"role": "user", "content": "ping"}]}
```

httpx 側は `timeout=None` (クライアント側で打ち切らない)。

### 生 JSON 出力 (全回)

```json
{"event": "start", "model": "qwen3.6-35b-a3b_Q4", "chat_url": "http://localhost:8080/v1/chat/completions", "running_url": "http://localhost:8080/running", "t17_pid": 1894112, "utc": "2026-08-15T11:50:27Z"}
{"event": "pid_gone", "pid": 1894112, "waited_sec": 0.0, "utc": "2026-08-15T11:50:27Z"}
{"event": "quiet_unloaded", "waited_sec": 620.3, "running": "{\"running\":[]}", "utc": "2026-08-15T12:00:48Z"}
{"event": "pre_cold_running", "set": 1, "attempt": 1, "running": "{\"running\":[]}", "meminfo_cached": "Cached:         22570420 kB", "utc": "2026-08-15T12:00:48Z"}
{"set": 1, "role": "trade", "phase": "cold", "idx": null, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 14.405, "status": 200, "utc": "2026-08-15T12:01:02Z"}
{"set": 1, "role": "trade", "phase": "warm", "idx": 1, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.071, "status": 200, "utc": "2026-08-15T12:01:02Z"}
{"set": 1, "role": "trade", "phase": "warm", "idx": 2, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.083, "status": 200, "utc": "2026-08-15T12:01:02Z"}
{"set": 1, "role": "trade", "phase": "warm", "idx": 3, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.073, "status": 200, "utc": "2026-08-15T12:01:02Z"}
{"event": "quiet_unloaded", "waited_sec": 150.1, "running": "{\"running\":[]}", "utc": "2026-08-15T12:03:32Z"}
{"event": "pre_cold_running", "set": 2, "attempt": 1, "running": "{\"running\":[]}", "meminfo_cached": "Cached:         23653740 kB", "utc": "2026-08-15T12:03:32Z"}
{"set": 2, "role": "trade", "phase": "cold", "idx": null, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 13.388, "status": 200, "utc": "2026-08-15T12:03:46Z"}
{"set": 2, "role": "trade", "phase": "warm", "idx": 1, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.079, "status": 200, "utc": "2026-08-15T12:03:46Z"}
{"set": 2, "role": "trade", "phase": "warm", "idx": 2, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.074, "status": 200, "utc": "2026-08-15T12:03:46Z"}
{"set": 2, "role": "trade", "phase": "warm", "idx": 3, "model": "qwen3.6-35b-a3b_Q4", "elapsed_sec": 0.079, "status": 200, "utc": "2026-08-15T12:03:46Z"}
{"event": "done", "utc": "2026-08-15T12:03:46Z"}
```

### 表

| role | model | phase | set 1 (秒) | set 2 (秒) | 備考 |
|---|---|---|---|---|---|
| trade | qwen3.6-35b-a3b_Q4 | cold after confirmed TTL unload | 14.405 | 13.388 | `/running` 3 連続不在 + サーバログの TTL unload で cold を確定 |
| trade | qwen3.6-35b-a3b_Q4 | immediate warm (1 回目) | 0.071 | 0.079 | cold 完了直後 |
| trade | qwen3.6-35b-a3b_Q4 | warm 2 回目 | 0.083 | 0.074 | |
| trade | qwen3.6-35b-a3b_Q4 | warm 3 回目 | 0.073 | 0.079 | |
| trade | qwen3.6-35b-a3b_Q4 | warm 中央値 | 0.073 | 0.079 | 3 回の中央値 |
| improve | qwen3.6-35b-a3b_Q4 | — | same as trade; no second cold load | same as trade; no second cold load | trade と同一 alias |

全リクエスト `status: 200`。失敗・打ち切りは 0 件。

### サーバ側ログによる裏取り

set 1:

```
 8月 15 21:00:26 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Unloading model, TTL of 120s reached
 8月 15 21:00:48 llama-swap[2094952]: http: proxy error: dial tcp 127.0.0.1:5815: connect: connection refused
 8月 15 21:01:02 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Health check passed on http://127.0.0.1:5815/health
 8月 15 21:01:02 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 682 "python-httpx/0.28.1" 14.400406606s
 8月 15 21:01:02 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 670 "python-httpx/0.28.1" 67.946557ms
 8月 15 21:01:02 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 670 "python-httpx/0.28.1" 79.780426ms
 8月 15 21:01:02 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 670 "python-httpx/0.28.1" 69.541084ms
```

set 2:

```
 8月 15 21:03:03 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Unloading model, TTL of 120s reached
 8月 15 21:03:33 llama-swap[2094952]: http: proxy error: dial tcp 127.0.0.1:5815: connect: connection refused
 8月 15 21:03:46 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Health check passed on http://127.0.0.1:5815/health
 8月 15 21:03:46 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 694 "python-httpx/0.28.1" 13.383458988s
 8月 15 21:03:46 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 670 "python-httpx/0.28.1" 75.820253ms
 8月 15 21:03:46 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 669 "python-httpx/0.28.1" 70.38319ms
 8月 15 21:03:46 llama-swap[2094952]: [INFO] Request 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 671 "python-httpx/0.28.1" 76.142054ms
```

クライアント計測 (14.405 / 13.388) とサーバ計測 (14.400 / 13.383) は 5 ミリ秒差で一致する。
うちモデルロード自体は spawn (21:00:48 / 21:03:33) から health check 通過
(21:01:02 / 21:03:46) までの約 14 秒 / 約 13 秒である。

## 判断

**selected timeout_sec: 300** (現行値を維持。`config/settings.yaml` と
`config/settings.yaml.example` はいずれも変更しない)

### 算術上の下限

Step 4 の式に生値を入れる。成功した cold/warm の最大値 `W = 14.405`
(set 1 cold)。安全余裕 `max(30, 14.405 * 0.25) = max(30, 3.601) = 30`。
`14.405 + 30 = 44.405` → 秒単位に切り上げて **45 秒**。これは
「この payload でこの環境なら最低これだけは要る」という下限であって、
運用値の推奨ではない。45 は 300 以下なので、規則どおり設定変更は行わない。

### 45 秒を採らず 300 秒を維持する根拠

1. **本計測の payload は `max_tokens: 1` の `ping` であり、実リクエストの下限しか測っていない。**
   計測しているのはモデルロード + 最初の 1 トークンまでであり、実 Mission の
   プロンプト長・生成トークン数・tool-calling の往復は一切含まれない。
   warm 0.07 秒台という値は「生成をほぼしていない」ことの帰結であって、
   実運用の warm レイテンシではない。
2. **既知の warm 31.16 秒 (旧記録) との差は、環境差ではなく payload 差で説明できる。**
   旧記録は実 Mission プロンプトに対する応答時間であり、本計測の 0.07 秒台とは
   測っている区間が違う。同じ warm 状態でも、生成量が増えれば 31 秒台に届く。
   既知の cold 参考値 13.86 秒 (2026-08-12、`/props` 経由) は本計測の
   13.388 / 14.405 秒とよく一致しており、cold ロード時間の再現性は確認できた。
3. **短い timeout が cold でも warm でも実際に失敗することを、同じ環境で観測している。**
   計測待機中、同一 alias に対する別レーンのクライアント (`python-httpx`、
   クライアント側 timeout 約 30 秒、実 Mission プロンプト) は全試行が
   キャンセルされて 502 になっていた:

```
 8月 15 20:42:19 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Health check passed on http://127.0.0.1:5815/health
 8月 15 20:42:34 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.716619018s
 8月 15 20:44:00 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 27.776153274s
 8月 15 20:45:27 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.406182659s
 8月 15 20:46:54 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 27.805170738s
 8月 15 20:48:21 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.579321328s
 8月 15 20:49:47 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 29.330414859s
 8月 15 20:52:03 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.761410382s
 8月 15 20:53:35 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.459276981s
 8月 15 20:55:06 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.740877444s
 8月 15 20:56:37 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 28.719909014s
 8月 15 20:58:25 llama-swap[2094952]: [INFO] Request "POST /v1/chat/completions" 502 0 "python-httpx/0.28.1" 29.071749049s
 8月 15 21:00:26 llama-swap[2094952]: [INFO] <qwen3.6-35b-a3b_Q4> Unloading model, TTL of 120s reached
```

   この区間の `Health check passed` は 20:42:19 の 1 回のみで、次のロードは
   21:00:48 (TTL 満了後) である。したがって**ロードを含むのは最初の 1 件
   (20:42:34) だけ**で、残る 10 件 (20:44:00〜20:58:25) はいずれも
   **すでにロード済み = warm のモデルに対する試行**である。
   つまりこのクライアントは、warm であってもなお 27.8〜29.3 秒を使い切って
   応答を得られていない。実 Mission 形状のリクエストは、この環境では
   warm でも 28 秒を超えるということであり、30 秒級の timeout は
   cold でも warm でも機能しないという直接の反証になる。
   これは既知の warm 31.16 秒 (旧記録) を、本計測と同じ環境の別データが
   独立に裏付けているということでもある。
4. **`healthCheckTimeout: 300` と揃う。** llama-swap 側がモデル起動を最大 300 秒
   待つ設定なので、クライアント側を 300 秒より短くすると、サーバがまだ待っている
   最中にクライアントだけ先に諦める区間ができる。300 は両者の整合が取れた値である。
5. **timeout は上限であって遅延ではない。** 正常時は warm 0.07 秒で返るので、
   300 を維持しても平常時のレイテンシは 1 ミリ秒も増えない。300 が効くのは
   異常時だけであり、そこで早く諦める利得は無い。

### 設定ファイルの現況 (変更なし)

```
config/settings.yaml           llama_swap.timeout_sec = 300.0
config/settings.yaml.example   llama_swap.timeout_sec = 300.0
```

文書の selected timeout_sec (300) と両ファイルの値 (300) は一致する。
本 task による設定ファイルの変更は無い。

## 再現手順

```bash
# 設定値
uv run python - <<'PY'
from pathlib import Path
from agentic_fx.config import load_settings
s = load_settings(Path('config/settings.yaml'))
print('base_url=', s.llama_swap.base_url)
print('trade_model=', s.runner.trade.model)
print('improve_model=', s.runner.improve.model)
print('current_timeout_sec=', s.llama_swap.timeout_sec)
PY

# 環境 (nvidia-smi は不在。Intel Arc + SYCL のため clinfo と config から採る)
clinfo | grep -iE 'Device Name|Global memory size|Max compute units|Driver Version'
rg -n -A6 '^  qwen3\.6-35b-a3b_Q4:' /home/teru358/project/llm/bin/.config/llama-swap/config.yaml
rg -n -A20 '^groups:' /home/teru358/project/llm/bin/.config/llama-swap/config.yaml
systemctl is-active llama-swap.service
curl -s http://127.0.0.1:8080/v1/models
curl -s http://127.0.0.1:8080/running

# cold / warm 実測 (2 セット)。スクリプト実体は tmp/t18/measure.py
#   1. 対象 model が /running に 3 回連続で不在になるまで 10 秒間隔で待つ
#   2. cold 1 回 → warm 3 回を同一 payload で計測
#   3. 再度 TTL 満了を待って 2 セット目
```

計測スクリプトは cold 直前に `/running` を再取得し、対象 model が現れていたら
その回を破棄して待機に戻す (contention 下の値を cold として記録しない)。

サーバ側の裏取り:

```bash
journalctl -u llama-swap.service --since '2026-08-15 20:58' --no-pager \
  | grep -E 'Unloading|Health check passed|chat/completions'
```
