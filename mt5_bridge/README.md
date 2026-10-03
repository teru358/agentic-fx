# MT5 Bridge

MetaTrader5 端末へ HTTP でアクセスするための薄い FastAPI サービス。agentic-fx (afx) は価格 (quote・OHLCV) をこの bridge から取得する。

`MetaTrader5` Python パッケージは Windows 専用なので、bridge は **MT5 端末が動いている Windows (または Wine) 環境**で動かす。afx は別マシンでも同じマシンでもよい。

この文書は「bridge を配置して afx とつなげるまで」を扱う。Wine や MT5 端末そのものの導入は範囲外。

## 提供する endpoint

価格・口座の読み取り (API キー未設定でも使える):

| endpoint | 内容 |
|---|---|
| `GET /health` | 生存確認 (認証なし) |
| `GET /account` `GET /positions` `GET /symbols` | 口座・建玉・シンボル |
| `GET /quote/{symbol}` `GET /ohlcv/{symbol}` | 価格 |
| `GET /server-time` | サーバ時刻オフセットの確認 |
| `GET /admin/status` | halt・DRY_RUN の状態 |

発注系 (**API キーが必須**。キー未設定だと `DRY_RUN` に関係なく 403 `発注系は API キーが必要`):

| endpoint | 内容 |
|---|---|
| `POST /order` | 新規発注 |
| `POST /positions/{ticket}/modify` | SL/TP 変更 |
| `POST /positions/{ticket}/close` | クローズ |
| `POST /admin/halt` `POST /admin/resume` | 発注の停止・再開 |

afx の価格取得だけが目的なら、API キーなしでも動く (発注系が拒否されるだけ)。

## DRY_RUN

`.env` の `DRY_RUN` (既定 `true`) が発注の実体を決める。

- `true`: 発注系は約定のシミュレーションを返すだけで、MT5 へは発注しない。
- `false`: 実発注する。API キーの設定を必ず確認すること。

`/admin/halt` の `mode=hard` はフラグファイルを作り、以降 `DRY_RUN=true` 相当で起動する。再開はフラグファイルを削除し `.env` の `DRY_RUN` を戻す。

## 待受と API キー

- `BRIDGE_HOST` の既定は `127.0.0.1` (自機内のみ)。afx が同じマシンならこのままでよい。
- afx が別マシンのときだけ、届く最小の範囲のアドレスに変える (`0.0.0.0` は同じネットワークの全員に届く)。
- `BRIDGE_HOST=localhost` は `127.0.0.1` に読み替えて待ち受ける (hosts の設定次第で `localhost` が LAN の IP を指す環境があるため)。`::1` で待ちたい場合は `::1` と明示する。IP リテラル以外のホスト名はループバック扱いにならず、`BRIDGE_API_KEY` が必須。
- `127.0.0.1` / `::1` 以外で待ち受けるには `BRIDGE_API_KEY` が必須。空のままだと bridge は理由を示して起動を拒否する。
- `BRIDGE_API_KEY` が空のあいだは、待受先の指定方法 (`uvicorn server:app --host 0.0.0.0` のような別経路の起動を含む) に関係なく、接続元が自機 (ループバックの IP) でない要求は `/health` を含む全 endpoint で 403 になる。afx が `http://localhost:8812` で接続する構成はそのまま通る。
- 起動は `uv run python server.py` を使う。
- API キーを設定すると、全 endpoint (`/health` 以外。キーを設定済みなら `/health` は接続元を問わず開いている) が `X-Bridge-Api-Key` ヘッダを要求する。
- この bridge には認可や rate limit の仕組みは無い。インターネットには公開しないこと。

## セットアップ

前提: MT5 端末が起動しログイン済み (デモ/本番どちらでもよい)、Python 3.11+、[uv](https://docs.astral.sh/uv/)。

1. このリポジトリの `mt5_bridge/` ディレクトリを MT5 端末のある環境に置く。
2. `.env.example` を `.env` にコピーし、`MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` を書く。afx が別マシンなら `BRIDGE_HOST` と `BRIDGE_API_KEY` も設定する。同じマシンなら既定のままでよい (API キーは任意だが、勧める)。
3. 依存を入れて起動する。

   ```
   uv sync
   uv run python server.py
   ```

   成功すると `DRY_RUN=True | api_key=... | host=...` と `MT5 connection established` のログが出る。
4. afx を動かすマシンから `curl <URL>/health` で `"mt5_connected": true` を確認する (既定の URL は `http://localhost:8812`)。

   ```json
   {"status":"ok","mt5_connected":true,"dry_run":true,"server":"...","login":12345678}
   ```
5. bridge に API キーを設定したなら、afx 側のリポジトリ直下の `.env` に `MT5_BRIDGE_API_KEY=<同じ値>` を書く。
6. afx の `config/settings.yaml` で価格源を MT5 にする。bridge の URL は `datafeed` 配下の `mt5.bridge_url`:

   ```yaml
   datafeed:
     mt5: {enabled: true, bridge_url: "http://localhost:8812"}
   ```

   他の価格源 (`yfinance`、`twelvedata`) は `enabled: false` にする。
7. `afx init` を再実行して接続試験を記録する。
8. 戦略の採用審査に使う履歴を取り込む。

   ```
   uv run afx history import --source mt5 --symbol USDJPY --from <開始日> --to <終了日> --interval 5m
   ```

   MT5 の 1m 足は約 3 か月分しか遡れないので、基底足は `5m` を勧める。

## サーバ時刻オフセット

MT5 が返す時刻 (`tick.time` / `position.time` / `deal.time` / `rates["time"]`) はブローカーの**サーバ時間帯**のエポック秒で、UTC ではない (例: UTC+3 のブローカーもある)。`copy_rates_range` の `date_from` / `date_to` も同じ時間帯で解釈される。

bridge は `symbol_info_tick(symbol).time` と UTC 時計の差からオフセットを実測し、送受信の両方向で補正する。

- 検出のガード: `|raw| > 12h` は棄却 / 30 分単位に丸める / 丸め残差 > 120s は棄却 (市場が閉まっている間は tick が古いため)。
- 再計算は最短 300 秒間隔。棄却されたら直前の良い値を使う。
- 良い値は `SERVER_OFFSET_PATH` (既定 `logs/server_offset.json`) に永続化され、週末をまたぐ再起動でも残る。
- **良い値が 1 つも無ければ fail closed**。read 系 endpoint は **503** を返す (ずれた時刻で動くより止まる方が安全)。
- 夏時間の切り替わりは定期再計算で追随する。

`GET /server-time` で確認できる:

```json
{
  "server_offset_sec": 10800,
  "server_time": "2026-07-28T14:12:04+00:00",
  "utc_time": "2026-07-28T11:12:04+00:00",
  "offset_source": "live"
}
```

`offset_source` は `"live"` (直近の検出が成功) か `"cached"` (直近は棄却され前回の値を使用中)。

## 常駐化 (任意)

手動起動のままでもよい。常駐させるなら、実行環境の流儀 (Windows のタスクスケジューラ・NSSM、Linux の systemd user unit など) で `uv run python server.py` を `mt5_bridge/` を作業ディレクトリにして起動する。

## ログ

`logs/bridge.log` にローテーション付きで出力される (5MB × 6 世代)。ファイルに入るのは bridge 自身のログ (MT5 接続・切断・起動メッセージ等)。HTTP アクセスログは標準出力にだけ出る。

## トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| 起動時に `BRIDGE_API_KEY が必要です` | `BRIDGE_HOST` が自機以外から届く値になっている。キーを設定するか `127.0.0.1` に戻す |
| 全 endpoint が 403 `API キー未設定の bridge は自機内からの接続のみ受け付ける` | キー未設定のまま別マシンから接続している。`BRIDGE_API_KEY` を設定するか、afx を同じマシンで動かす |
| 発注系が 403 `発注系は API キーが必要` | `BRIDGE_API_KEY` が空。設定し、リクエストに `X-Bridge-Api-Key` を付ける |
| `ImportError: MetaTrader5 package is Windows-only` | Linux/Mac のネイティブ Python で動かしている。MT5 端末のある Windows (Wine 含む) の Python で実行する |
| `MT5 initialize() failed: (-10004, ...)` | MT5 端末が起動していない、または別ユーザーで起動している |
| `MT5 login failed: (-6, ...)` | 認証情報の誤り。MT5 端末で手動ログインが通ることを先に確認する |
| `mt5_connected: false` が続く | MT5 端末の自動売買ボタンと AlgoTrading の許可を確認する |
| 別マシンから接続できない | `BRIDGE_HOST` (と API キー) の設定、ファイアウォールで TCP 8812 の許可を確認する |
| quote・OHLCV が 503 | サーバ時刻オフセットが未確定 (市場が閉まっている間の初回起動など)。開場後に再試行する |

## セキュリティ

- `.env` は git で無視される。認証情報をコミットしない。
- 既定の待受は 127.0.0.1。広げるなら API キーを設定し、届く範囲を最小にする。
- 実発注 (`DRY_RUN=false`) にする前に API キーが設定されていることを確認する。
