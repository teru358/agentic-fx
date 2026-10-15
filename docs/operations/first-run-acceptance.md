# 初回起動の手順書 (clean clone から常駐まで) と実機受入

`git clone` した直後の状態から、対話ウィザード `afx init` で初期設定を済ませ、systemd の user サービスとして常駐させるまでの手順です。Claude の記憶や口伝に頼らず、この文書と画面の表示だけで最後まで通れることを目標にしています。足りない知識が出たら、それは案内の欠陥なので、この文書を直してください。

- 前半 (「前提」から「詰まったときに貼る情報」まで) は利用者向けです。
- 後半の「試験環境の都合」と「リセットと自動化の範囲」は、実機受入を行う試験者向けです。利用者向けの本文と README には入れません。

設計の正は [初回起動ウィザード設計](../superpowers/specs/2026-10-12-first-run-wizard-design.md) です。ウィザードの文面はこの文書に引用していますが、食い違ったら実際の画面 (`src/agentic_fx/setup/wizard.py`) が正です。

## 目次

1. 前提: OS と実行環境
2. 前提: CLI の導入
3. 手順
4. 各段で見える文面と期待
5. 主シナリオと追加シナリオ
6. 観測表
7. 詰まったときに貼る情報
8. 試験環境の都合 (試験者向け)
9. リセットと自動化の範囲 (試験者向け)

---

## 1. 前提: OS と実行環境

| 項目 | 内容 | 確認 |
|---|---|---|
| OS | Ubuntu 22.04 以降 (主)。WSL は対話の流れの確認用で、unit と隔離の判定には使いません | `. /etc/os-release && echo "$PRETTY_NAME"` |
| user systemd | `systemctl --user` が使える | `systemctl --user is-system-running` が `running` か `degraded` を返す |
| cgroup v2 | unified hierarchy | `stat -fc %T /sys/fs/cgroup` が `cgroup2fs` |
| linger | ログアウト後も afx を動かすなら有効が必要 (ウィザードの段 13 が確認つきで有効化できます) | `loginctl show-user "$USER" -p Linger` |
| `uv` | Python と依存の管理。Python 3.13 は `uv sync` が用意します (ネットワークが要ります) | `uv --version` |
| `git` | clone に使う | `git --version` |
| Landlock | 標準指標の導入と改善ループに必要 (x86_64 かつ ABI 3 以上)。無くてもペーパー運用だけは設定できます。ウィザードの段 0 が判定して表示します | 段 0 の表示で分かります |
| 改善ループの隔離 | Landlock ABI 8 以上 = Linux 6.18 以降。Ubuntu 22.04 / 24.04 の標準 kernel (6.8) は ABI 4 で不可、Ubuntu 26.04 LTS (7.0) は可 | `python3 -c 'import ctypes; print(ctypes.CDLL(None).syscall(444, None, 0, 1))'` (`landlock_create_ruleset(NULL,0,LANDLOCK_CREATE_RULESET_VERSION)` の戻り値)。`-1` は Landlock syscall 自体が使えず、隔離不可です。0 より大きい数が ABI です。 |

clone 先は、ホームの下の任意のディレクトリです (例 `~/agentic-fx`)。パスに空白・改行・`:` などの特殊文字を入れないでください。unit に書けないため、段 13 で止まります。

LLM と価格源の有無でシナリオが変わります (「5. 主シナリオと追加シナリオ」)。

- 主シナリオ: local LLM (llama-swap) が無い。取引判断は Claude (`claude -p`)、改善も Claude。価格は MT5 bridge が無ければ yfinance。
- 別ホストの llama-swap や MT5 bridge がある場合は、追加シナリオを参照してください。

## 2. 前提: CLI の導入

`afx init` の段 4 は、`claude` / `codex` の導入とログインを確認します。未導入・未ログインでもウィザードは最後まで通りますが (安全側に倒して「未確認」と記録します)、取引判断と改善は動きません。先に済ませておくと、最初から確認が通ります。

どの CLI も **サブスクリプションのログインで使います。従量課金の API キーは使いません。** 環境変数に `*API_KEY*` / `*TOKEN*` などの名前の秘密があると、CLI backend の起動は拒否されます (段 4 が該当する変数名だけ表示します。値は表示しません)。

### 2.1 Node.js

```
node --version
```

何か版が表示されれば足ります。表示されない場合は、Node.js の公式手順 (LTS) で導入してください。nvm で入れた場合、node の場所が unit の `PATH` に書かれます。node を更新したら `afx init` を再実行して unit を作り直してください。

### 2.2 Claude CLI

1. 導入: Claude Code の公式セットアップ手順に従います。最小の例は npm です。

   ```
   npm install -g @anthropic-ai/claude-code
   ```

2. ログイン: `claude` を一度起動し、画面の案内でサブスクリプションのアカウントにログインします。API キーの入力は求められても選ばないでください。
3. 版の確認:

   ```
   claude --version
   ```

4. 最小の往復の確認 (サブスクの枠を少し使います):

   ```
   claude -p "ok だけ答えて" --max-turns 1
   ```

   `ok` と返れば、導入とログインは通っています。

### 2.3 Codex CLI (改善を Codex にするときだけ)

1. 導入とログイン: Codex の公式手順に従います。最小の例は npm です。

   ```
   npm install -g @openai/codex
   codex login
   ```

   ChatGPT のアカウントでログインします (`runner.codex.provider` は `chatgpt` のみ対応)。

2. **実体 (ELF) の絶対パスを調べます。** `runner.codex.bin` には node のラッパーではなく、実行形式 (ELF) の絶対パスを書きます。afx は CLI を隔離した環境で起動するので、ラッパーが呼ぶ node や相対パス解決に頼れないためです。

   ```
   readlink -f "$(command -v codex)"
   file "$(readlink -f "$(command -v codex)")"
   ```

   `file` が `ELF` と答えればそのパスが実体です。`a node script` などと答えた場合は、npm の導入先 (`npm root -g`) の下にある `@openai/codex` の中から、ELF の `codex` を探してください。

   ```
   find "$(npm root -g)/@openai/codex" -type f -name codex -perm -u+x
   ```

   見つけた絶対パスを、段 4 で `codex の実体の絶対パス` を尋ねられたときに入力します (自動実行する台本では環境変数 `CODEX_ELF` に入れておきます)。

### 2.4 ウィザードによる再確認

ここまでが済んでいなくても、段 4 が `bin_not_found` (CLI が PATH に無い) / `credentials_missing` (ログインしていない) を表示し、この節への導線を出します。表示は次のとおりです。

```
claude が見つかりません (bin_not_found)。導入手順は docs/operations/first-run-acceptance.md の『前提: CLI の導入』 を見てください。
```

---

## 3. 手順

```
git clone <このリポジトリの URL> agentic-fx
cd agentic-fx
uv sync
uv run afx init
```

1. `uv run afx init` が対話ウィザードです。標準入力が端末でないと「対話が必要です」と言って終了します (rc=2)。段 9 で「書き込みますか」に `y` と答えるまでは、設定もデータも書きません (排他用の空の lock ファイル 2 つだけが `data/` に作られます)。
2. 段 8 で常駐サービスとして動かすかを尋ねます。選んだ場合は段 13 で unit の内容を確認して置き、その先の外から見る手段 (ops) の有効化、linger、ログイン時の自動起動、今の起動までを同じウィザードで進めます。
3. 起動後の確認は 3 段階で、ウィザードが続けて表示します。あとから自分で確かめるには次のコマンドを使います。

   ```
   systemctl --user is-active afx          # 1. サービス
   uv run afx ctl status                   # 2. 操作 API (認証つきの status)。repo の root で打つ
   systemctl --user is-active afx-ops@web  # 3. ops の feature (有効にしたものの名前に置き換える)
   uv run afx ops status web
   journalctl --user -u afx | grep 'improve isolation'   # 改善 backend の隔離適合 (admission) の行
   ```

4. 詰まったら、「7. 詰まったときに貼る情報」を集めてから、`scripts/first-run-reset.sh` で最初に戻せます。

   ```
   scripts/first-run-reset.sh --dry-run    # 何が消えるかを見るだけ
   scripts/first-run-reset.sh              # 全数を見せたうえで reset と打ち込みを求めます
   ```

   リセットは paper の取引履歴・注文・approval・改善 report・RAG・plugins を永久に消します。`.env` は既定で残します。詳しくは「9. リセットと自動化の範囲」。

ウィザードの操作の共通事項:

- 各問いで `?` を打つと説明が出る問いがあります。`q` を打つと中断します (段 10 の前なら何も書かずに終わります)。
- `[既定: …]` や `[…]` が付く問いは、空 Enter でその値になります。
- 再実行 (`afx init` をもう一度打つ) すると、各段の既定値が現在の `settings.yaml` の値になり、変わった項目だけを書きます。

---

## 4. 各段で見える文面と期待

以下は `src/agentic_fx/setup/wizard.py` の表示の引用です (環境で変わる部分は `<…>` と書いています)。入力する値は「5. 主シナリオ」の表を見てください。

### 段 0: 前提確認 (質問なし)

```
agentic-fx の初期設定を始めます。途中でやめても、段 10 で確定するまでは何も書きません。
初めての設定です。
Python 3.13 / linux
```

- 再実行のときは「初めての設定です。」の代わりに「再実行です。現在の値を既定として表示します。」。
- リポジトリの直下でないと「ここは agentic-fx のリポジトリ直下ではありません (config/settings.yaml.example が見つかりません)。clone したディレクトリで実行してください。」(rc=2、何も作りません)。
- 別の `afx init` が動いていると「別の `afx init` または `afx ops enable` が実行中です。終了してから再実行してください。設定・DB は変更していません。」、別の agentic-fx が動いていると「別の agentic-fx が動いています。`systemctl --user stop afx` で止めてから再実行してください …」(rc=2)。
- Landlock が使えない環境では「この環境では Landlock が使えません (x86_64 かつ ABI 3 以上が必要)。標準指標の導入と改善ループは使えません。取引判断のペーパー運用だけ設定できます。」に続けて `続けますか [y/N] `。
- Landlock ABI が 8 未満のときは「この kernel (Landlock ABI <ABI>) では改善ループの隔離が使えません (ABI 8 以上、Linux 6.18 以降が必要)。取引判断と標準指標は設定できます。改善は kernel を更新するまで止まります。」と表示して続行します。
- pytest が無いと「plugin の採用審査 (gate) が pytest を使います。`uv sync` (dev 依存を含む既定) で入ります。」(警告、続行します)。
- 前回の途中で止まった印があると「前回の初期設定が途中で止まっています。このまま続けて収束させます (サービスはこの印がある間、起動を拒否します)。」

### 段 1: はじめに (初回だけ)

```
[段 1 はじめに]
これは「使う人が育てる」システムです。すぐに取引できる、すぐに勝てるものではありません。
まずペーパー (実資金を動かさない) で動かします。発注・損切り・資金保護は LLM ではなく決められたコードが行い、drawdown kill switch は設定で切れません。
戦略や指標の採用は必ずあなたの明示確認を経ます。改善ループは LLM を使うので、CLI backend (claude / codex) を選ぶとサブスクの枠を使います。
読みました [Enter]: 
```

再実行のときは 1 行だけ `[段 1] 再実行です。各段の既定値は現在の settings.yaml の値です。`

### 段 2: 基本

```
[段 2 基本] 表示・口座・対象ペア・初期残高 (まだ何も書きません)
表示タイムゾーン (例 Asia/Tokyo) [<この端末のタイムゾーン>]: 
口座通貨 (後から変えると過去の損益と通貨が混ざります) [JPY]: 
対象ペア (選べるもの: <選べるペアの一覧>。複数はカンマ区切り) [USDJPY]: 
初期残高 (ペーパー。後から変えると過去の残高と HWM の基準が動きます) [1000000]: 
```

- タイムゾーンは `JST` のような略称では通りません (「'JST' のような略称は使えません。'Asia/Tokyo' のような地域名で入力してください。」)。
- 口座通貨は 3 文字の大文字、初期残高は正の数。不正だと理由を表示して聞き直します。
- 複数ペアを選ぶと「複数ペアを選びました。運用実績は USDJPY のみです。」

### 段 3: 価格源

```
[段 3 価格源] 取引判断と約定判定に使う価格をどこから取るかを決めます。
本線は MT5 bridge (MetaTrader 5 から価格を受け取る小さな HTTP サーバ) です。
  1. 使う。bridge は動いている
  2. 使う予定。まだ準備していない
  3. 使わない (yfinance などを使う)
価格源: 
```

初回は既定がありません (再実行では現在の設定が既定になります)。

- **3 (使わない)** を選ぶと次が出ます。

  ```
    1. yfinance (既定)
    2. Twelve Data (API キーが要ります)
  価格源 [既定: 1]: 
  ```

  `1` を選ぶと「yfinance は週明けの開場直後 (日曜 21:00〜23:00 UTC ごろの足が来ない) に、毎週しばらく取引判断を止めます (degraded)。既知の問題です。」と表示され、`yfinance に USDJPY の価格を 1 回問い合わせて確認します。実行しますか [Y/n] ` を尋ねます。`y` で成功なら `yfinance OK`、失敗なら「yfinance の確認に失敗しました (…)。後で再確認できます。」で、どちらでも先へ進みます。`n` で断ると未確認 (`user_declined_probe`) で記録します。`2` (Twelve Data) は `.env` と環境のどちらにも `TWELVEDATA_API_KEY` が無いと選び直しになります (値はここでは入力しません)。
- **1 (bridge は動いている)** を選ぶと `bridge の URL [http://localhost:8812]: `、`bridge 側で API キーを設定しましたか (1 はい / 2 いいえ / 3 分からない) [既定: 3]: ` の順に尋ね、成功なら `MT5 bridge OK: USDJPY (bridge の時刻 <時刻>)`。失敗すると理由を表示し、`[r] 再試行 / [2] 『使う予定・未準備』として進む / [u] URL を入れ直す: ` を尋ねます。
- **2 (使う予定)** は bridge の配置手順 (7 項目) を表示し、`bridge の URL …` を尋ねて、今接続を試すかを尋ねます (既定: 試さない)。

### 段 4: LLM

```
[段 4 LLM] 取引判断 (1 足ごと) と改善 (週次など) にそれぞれ LLM を割り当てます。
取引判断は local か claude、改善は local / opencode / claude / codex。claude と codex はサブスク (従量課金 API ではない) を使います。
llama-swap が応答しません。local は既定にしません。
  1. 取引判断に Claude を使う (`claude -p`、サブスクの枠を使う)
  2. 取引判断に Claude を使う (改善の既定は Codex)
  3. 別ホストの llama-swap の URL を入れる
  4. 後で設定する
LLM の構成: 
```

- llama-swap が応答するときは「llama-swap が応答しました (モデル N 件)。」と表示され、先頭に `  local. 取引判断に llama-swap を使う (Enter)` が加わり、それが既定になります。**応答しないとき (主シナリオ) は既定が出ません。** local は自動では選ばれません。
- `1` / `2` を選ぶと、Node.js の確認 (見つからなければ「Node.js が見つかりません (bin_not_found)。claude / codex の導入に必要です。手順は … を見てください。」)、claude の確認が続きます。成功は `claude: 導入とログインを確認しました。`。失敗は次のとおりで、選択肢が出ます。

  `claude` が PATH で見つかった場合は `claude の実体: <path>` と表示し、そのパスを settings に保存します。

  ```
  claude が見つかりません (bin_not_found)。導入手順は docs/operations/first-run-acceptance.md の『前提: CLI の導入』 を見てください。
  claude にログインしていません (credentials_missing)。端末で `claude` を起動してログインしてから再試行してください。ここでは入力しません。
  [r] 導入・ログイン後に再試行 / [c] 未確認のまま進む / [d] 後で設定する [既定: c]: 
  ```

- 続いて取引判断のモデルの一覧が出ます (claude は別名の固定表)。

  ```
    1. sonnet (標準)
    2. opus (高性能)
    3. fable (別系統)
    4. haiku (軽量)
  取引判断のモデル (番号 / m = 手入力) [1]: 
  ```

- claude の確認が成功したときだけ、取引判断の 1 往復試験を尋ねます。

  ```
  取引判断用の `claude -p` が 1 往復できるかを試験できます。サブスクの枠を少し使います。
  trade 用の `claude -p` を 1 往復で試験します。サブスクの枠を少し使います。送りますか [y/N] 
  ```

  `y` で `claude -p の試験: 成功` (失敗なら `失敗`)。`n` で断ると `unverified` / `user_declined_probe` として記録します。
- 次に改善の backend を選びます。`1` を選んだ場合の既定は 3 (claude)、`2` なら 4 (codex)。

  ```
  改善の backend: ローカル LLM では opencode が本線で、local は最低線です。
    1. local
    2. opencode
    3. claude
    4. codex
  改善の backend [既定: 3]: 
  ```

  claude を選ぶと、claude の確認と `改善のモデル (番号 / m = 手入力) [1]: `、確認が成功したときだけ `改善 backend の実往復 (`afx improve verify-backend`) を確定の後に試験しますか。サブスクの枠を少し使います [y/N] ` を尋ねます。
- 改善ループの隔離が使えない kernel では、この問いの代わりに「改善の実往復の試験は、この kernel では隔離が使えないため行いません。」と表示します。
- codex を選ぶと、確認 (ELF の実体が必要) の後、見つからなければ `codex の実体の絶対パス (Enter = 後で settings.yaml に自分で書く): ` を尋ねます (絶対パスのみ。`/` で始まらないと「絶対パス (/ で始まる) で入力してください。相対パスや ~ は使えません。」)。モデルは `codex app-server` の一覧から選び (取れなければ `codex のモデル一覧を取得できませんでした (model_list_unavailable)。モデル名を手入力します。`)、既定は名前に `terra` を含むものです。
- 取引判断のモデル名は `取引判断のモデル名`、改善のモデル名は `改善のモデル名` として尋ねます (手入力のとき。一覧から選ぶ場合は `<backend>のモデル名` / `<backend>のモデル (番号 / m = 手入力)` の形です)。
- `3` (別ホストの llama-swap) は `認証を要しない llama-swap だけ扱えます。` と `llama-swap の URL [<現在の URL>]: ` を尋ねます。接続できない・認証を要する・一覧が空のときは固定の理由 (`connect_error` / `auth_unsupported` / `model_not_found`) の文を表示し、`[r] 構成の選び直し / [m] モデル名を手入力して未確認のまま進む / [4] 後で設定する [既定: r]: ` を尋ねます。
- `4` (後で設定する) は「LLM は後で設定します。runner.* には触れません。取引判断 mission は LLM が使えるまで止まり、未完の手順に出し続けます。」で、`deferred_by_user` を記録します。

### 段 5: 判断足

```
[段 5 判断足] 判断を行う足です。
  15m / 1h (推奨)。1m と 5m は選べますが、判断の上限時間より短いため使えません。
判断足 [既定: 1h]: 
```

### 段 6: 履歴と backtest

```
[段 6 履歴と backtest] 戦略を採用するかは、過去の価格での試験 (backtest) の成績で判断します。
```

- MT5 bridge を選んだ場合は `backtest の基底足` として基底足 (`1m` / `5m` / `15m`、既定 5m) を尋ねます。
- 選ばなかった場合 (主シナリオ) は質問なしで、「MT5 bridge が無いと、戦略の採用審査に使う履歴を取り込めません。この状態では改善ループは戦略を作れても採用審査を通せません。指標の導入はできます (指標の審査は履歴を使いません)。」と「backtest.eval_source は none にします。Dukascopy は相手に負荷をかけ遮断されることがあるため勧めません。」、「holdout は 3 ヶ月です (変更は人の審査が要る項目なので尋ねません)。」を表示します。

### 段 7: 改善周期と最初の 1 本

```
[段 7 改善周期と最初の 1 本]
改善の周期 (weekly / daily) [既定: weekly]: 
改善の時刻 (表示タイムゾーン Asia/Tokyo、例 Sat 03:00) [Sat 03:00]: 
直近の予定は <日時> (期 <期>)、次回は <日時> です。
  1. 次の予定まで待つ (既定) — 起動直後には走らない。その間に方針文と最初の課題を入れられる
  2. サービス起動の直後に 1 本走らせる
最初の 1 本 [既定: 1]: 
```

- `1` を選ぶと「直近の予定の期 (<期>) は消費済みにします。」
- 改善が claude / codex のときは「1 回の改善 mission はサブスク枠を使います。」
- Landlock が使えない環境では「Landlock が使えないため、改善ループは動きません。ここでは何も尋ねません。」
- 改善ループの隔離だけが使えない kernel では周期と時刻は従来どおり尋ね、最後に「この kernel では改善は走りません (段 0 の表示)。」と表示します。

### 段 8: 常駐サービスと .env

```
[段 8 常駐サービスと .env]
常駐なら `afx ctl` と bot / web / notify で操作し、承認もそこから行います。
手動起動なら対話シェルで操作し、外からは見えません。
afx を常駐サービス (systemd user unit) として動かしますか [Y/n]
.env に必要なキーはありません。
.env は作りません (.gitignore で除外済みです)。Discord の token と通知の webhook URL は `afx ops enable` が feature 専用の secret file に置きます。
```

必要なキーがある構成 (Twelve Data、bridge の API キー) では `.env に必要なキー: TWELVEDATA_API_KEY (なし)` のように、あり / なしだけを表示します (値は表示しません)。

### 段 9: 要約と最終確認

```
[段 9 要約]
価格源: <源 / URL / 鍵の有無 / 検査結果>
取引判断と改善: <backend とモデル>
判断足: <足>。backtest の基底足: <足>、履歴源: <源>
改善: <周期と次回>。最初の 1 本: <方法>
常駐: <段 8 の答え>
作るもの: settings.yaml、DB、状態、確認記録。
このあと尋ねること: 標準指標、方針文と最初の課題、常駐化と外から見る手段。
この内容で書き込みますか (d = settings.yaml の差分を見る) [Y/n]
```

- 未確認のものは「bridge の接続は未確認」のように利用者向けの文で表示します。`d` は設定キーの差分を表示して、この問いに戻ります。
- 再実行で価格源・口座通貨・初期残高などを変えると「注意が要る項目を変えます:」が出て、`変えてよいときだけ ('yes' と打ち込んでください。やめるときは q): ` を求めます。
- ここで `n` (または Enter) だと「何も書かずに終了します。」で終わります。
- 書く前の設定検査で、質問への答えでは直せない値 (settings.yaml に書かれている値) が見つかると、`この値は質問への答えでは直せません (settings.yaml に書かれている値です)。` と出て `[a] 中止して settings.yaml を直す / [r] 段 2 からやり直す [既定: a]: ` を尋ねます。`a` は何も書かずに終了します。

### 段 10: 確定

```
[段 10 確定] 設定・DB・確認記録・状態を順に書きます。
設定を書き込みました。ここから先 (指標・方針文・常駐化) は 1 つずつ確定し、途中でやめても書いた所までが残ります。
```

途中で失敗すると「確定の途中で失敗しました: …」「書かれたもの: …」「書かれていないもの: …」「次にすること: `afx init` を再実行してください。印が残っている間、サービスは起動しません。」を表示します。再実行で同じ手順が収束します。

### 段 11: 標準指標

```
[段 11 標準指標]
指標だけでは取引しません。改善ループが戦略を作るときの材料になります。
1. sma: 単純移動平均 (終値の期間平均)
2. ema: 指数移動平均 (直近を重く見る平均)
   …(rsi / macd / bollinger / atr / adx / stochastic / ichimoku の 9 本)
外したい番号 (複数はカンマか空白、範囲は 2-4。配備済みの番号は採用に戻します。空 Enter = この一覧で進む) []: 
この 9 本を採用します。採用はあなたの確認として記録されます (decided_by=human_cli)。よろしいですか [y/N] [N]: 
```

- `y` で 1 本ずつ審査します (`1/9 sma: 審査中 (pytest、最長 300 秒)` → `sma: 採用しました (承認 #<番号>)。`)。9 本で 10 分以上かかることがあります。最後に「反映は次回のサービス起動時です。」
- 失敗した指標はその 1 本だけ理由を表示して先へ進みます (他は止まりません)。
- Landlock が使えない環境では「Landlock が使えないため、標準指標の導入は飛ばします。」

### 段 12: 方針文と最初の課題

```
[段 12 方針文と最初の課題]
既存の方針文: 0 文字 / 改善課題: 0 件
方針文は取引判断と改善の全 mission に注入される運用者の方針です。
例: 「ペーパー運用。実資金の発注は行わない。」
例: 「対象は USDJPY の 1 時間足。指標発表の前後 30 分は新規に入らない。」
例: 「選別して勝つを優先し、取引回数より負けを減らす提案を評価する。」
後から `afx ctl policy add` / `afx ctl improve add` で足せます。空 Enter で飛ばせます。e で例文を入れます。
方針文: 
方針文を <N> 行書きました。
課題は改善ループが最初に取り組む 1 件です。
例: 「標準指標だけで組める 1 時間足の押し目買い戦略候補を 1 本作り、直近 3 ヶ月の履歴で PF と最大ドローダウンを測る」
後から `afx ctl policy add` / `afx ctl improve add` で足せます。空 Enter で飛ばせます。e で例文を入れます。
最初の課題: 
最初の課題を <N> 件追加しました。
```

両方とも空 Enter だけで終えられます。課題が短い (4 文字未満) かプレースホルダに見えると「短い、またはプレースホルダのように見えます。」と `このまま追加しますか [y/N] ` を尋ねます。

### 段 13: 常駐化と外から見る手段

```
[段 13 常駐化と外から見る手段]
（段 8 で常駐を選んだ場合、unit の内容を表示して確認します。）
unit (afx.service) を設置しました。
外から見る / 操作する手段: bot (Discord bot) / web (読み取り専用 web) / notify (通知)。質問 (token・port・webhook URL など) は `afx ops enable` 側が端末から直接受けます。
外から見る手段を今設定しますか (既定: あとで) [y/N] 
```

- 段 8 で常駐を選ばないと、段 13 は unit 設置・linger・enable・起動を尋ねず「手動起動: `uv run afx`。常駐に切り替えるときは `afx init` を再実行してください。」で終えます。
- unit の設置前に、unit と同じ環境で `afx check` を走らせます。fatal があると unit を置きません (理由を表示)。warning だけ (LLM 未検証など) なら `afx check の warning (unit は置きます): <コード>` と表示して置きます。
- 設置済みの unit と内容が違うと差分を表示して `この内容で unit を置きますか [y/N] ` を尋ね、旧 unit は `afx.service.bak-<時刻>` に退避します。同じなら「unit は設置済みです (内容は同じです)。」
- systemd が使えず `XDG_RUNTIME_DIR` も無い環境では「systemd のユーザーセッションが見えません (XDG_RUNTIME_DIR が無い)。`su` ではなく <user> で直接ログインして `afx init` を再実行してください」で、unit は置かずに進みます。対象ユーザーで直接ログインして実行します (`su` は使いません)。
- `XDG_RUNTIME_DIR` があるのに systemd が使えない環境では「systemd のユーザーサービスが使えません。手で起動するには `uv run afx` を実行してください」で、unit は置かずに進みます。
- 外から見る手段に `y` を選ぶと `有効にする手段 (カンマ区切り。空 Enter = あとで): ` を尋ね、選んだものごとに `afx ops enable` の質問が続きます (token などは端末から直接入力し、この文書にも記録にも残りません)。
- linger が `no` のときだけ次を尋ねます。

  ```
  ログアウトすると afx も止まります。`loginctl enable-linger $USER` を実行しますか [y/N] 
  ```

  `y` で有効化し、所有を確認記録に残します (リセットが解除してよい根拠になるのはこの記録だけです)。すでに `yes` なら何も尋ねません。
- 起動の前に次を尋ねます。

  ```
  ログイン時に自動起動するよう enable しますか [Y/n] 
  今起動すると、価格の取り込みと (待つを選ばなかった場合) 改善 mission が始まります。
  起動しますか [y/N] 
  ```

- 起動すると、起動後の確認 (3 段階) を表示します。

  ```
  起動後の確認 (3 段階。それぞれ別に記録します):
    1. サービス: active
    2. 操作 API (認証つき status): ok
    3. ops <feature>: ready <ok|failed|unchecked>
  改善の隔離 (admission): <yes|no|unknown> (<理由>)
  ```

  - `3.` の行は ops を有効にした feature ごとにだけ出ます。
  - 改善 backend が CLI のとき、`改善の隔離 (admission)` の行が journal の今回の起動分から読まれます。行が出るまで数分かかることがあり、そのときは `unknown` (`admission_line_timeout`) で終わります (確認コマンド: `journalctl --user -u afx | grep 'improve isolation'`)。
  - 起動しなかった場合は「サービスは起動していません (<理由>)。起動するには `systemctl --user start afx` を実行してください。」
  - 操作 API が無効なら、「操作 API が無効です。`afx ctl` は使えず、未完の手順を `afx ctl status` に出せません。案内はこの表示と起動画面だけです。」
  - 操作 API に認証つきで繋がらなかったら、「サービスは起動しましたが、操作 API に認証つきで接続できませんでした。`journalctl --user -u afx -n 50` を見てください。」

### 段 14: 次の一歩

```
[段 14 次の一歩]
起動: unit なら `systemctl --user start afx` (状態は `systemctl --user status afx`)、手動なら `uv run afx`。
操作は `afx ctl`: `afx ctl status` (稼働状態と未完の手順)、`afx ctl approval list`、`afx ctl improve add <案>`、`afx ctl policy add <文>`。
改善候補の承認は二通り: Discord bot を有効にしたなら承認依頼で二段操作 (claim → confirm)、もう一つは `afx ctl --as approver approve <id>` (端末での確認あり)。
ログ: logs/agentic.log、logs/activity.log、unit なら `journalctl --user -u afx`。
設定を変えるときは `afx init` の再実行です (稼働中は先に `systemctl --user stop afx`)。

未完の手順:
  - <各項目>
```

`未完の手順:` は、未確認の LLM、MT5 bridge 未接続、標準指標の未導入、戦略なし、履歴源なし、linger、起動が未了、などが残っているときだけ出ます。0 件なら何も出ません。同じ内容は起動後も `afx ctl status` の末尾と起動画面に出続けます (操作 API が無効のときは起動画面と、この段の表示だけです)。

---

## 5. 主シナリオと追加シナリオ

### 5.1 主シナリオ: local LLM が無い Ubuntu 22

取引判断は `claude -p`、改善は Claude。「2. 前提: CLI の導入」の 2.1〜2.2 を済ませてから `uv run afx init` に入ります。価格は yfinance (bridge が無いため) を使います。:8080 (llama-swap) に何も居ないことが前提です (SSH トンネルなどで転送していると、段 4 の既定が `local` になって別の分岐に入ります。「8. 試験環境の都合」)。

| 段 | 問い (固定部分) | 入力 | 期待する表示 |
|---|---|---|---|
| 1 | `読みました [Enter]:` | Enter | 段 2 へ |
| 2 | `表示タイムゾーン` | `Asia/Tokyo` | |
| 2 | `口座通貨` | `JPY` | |
| 2 | `対象ペア` | `USDJPY` | |
| 2 | `初期残高` | `1000000` | |
| 3 | `価格源:` | `3` | 「yfinance (既定)」「Twelve Data」の 2 択 |
| 3 | `価格源 [既定: 1]:` | `1` | 週明けの注意文 |
| 3 | `yfinance に USDJPY の価格を 1 回問い合わせて確認します。実行しますか` | `y` | `yfinance OK` (失敗でも先へ進む) |
| 4 | `LLM の構成:` | `1` | 「llama-swap が応答しません。local は既定にしません。」が先に出ており、既定は無い。`claude: 導入とログインを確認しました。` |
| 4 | `取引判断のモデル (番号 / m = 手入力)` | Enter (sonnet) | |
| 4 | `trade 用の `claude -p` を 1 往復で試験します。…送りますか` | `y` | `claude -p の試験: 成功` |
| 4 | `改善の backend` | `3` | `claude: 導入とログインを確認しました。` |
| 4 | `改善のモデル (番号 / m = 手入力)` | Enter (sonnet) | |
| 4 | `改善 backend の実往復 (`afx improve verify-backend`) を確定の後に試験しますか` | `n` | |
| 5 | `判断足` | `1h` | |
| 7 | `改善の周期` | `weekly` | |
| 7 | `改善の時刻` | `Sat 03:00` | 直近の予定と次回の日時 |
| 7 | `最初の 1 本` | `1` | 「直近の予定の期 (…) は消費済みにします。」 |
| 8 | `操作 API を有効にしますか` | `y` | |
| 9 | `書き込みますか` | `y` | 段 10 の表示 |
| 11 | `外したい番号` | Enter | 9 本の一覧 |
| 11 | `よろしいですか` | `y` | 1 本ずつ審査 (長い) |
| 12 | `方針文:` | 任意の 1 行、続けて Enter | |
| 12 | `最初の課題:` | 任意の 1 件、続けて Enter | |
| 13 | `常駐させますか` | `y` | `unit (afx.service) を設置しました。` |
| 13 | `外から見る手段を今設定しますか` | `n` | |
| 13 | `enable-linger を実行しますか` (Linger が no のときだけ) | `y` | |
| 13 | `enable しますか` | `y` | |
| 13 | `起動しますか` | `y` | 起動後の確認 3 段階 |

完了後に確かめること:

1. `systemctl --user is-active afx` が `active`。
2. `uv run afx ctl status` が成功する (未完の手順が末尾に出ていてよい)。
3. `systemctl --user cat afx.service` に `Type=exec` / `ExecStart=… main.py --daemon` がある。
4. `data/state/setup.json` の `unit` が `installed`、`start.result` が `started`。
5. 観測表 (「6. 観測表」) の各行。

台本どおりに自動で通す場合は `tests/acceptance/scripts/main-claude.json` を使います (「9. リセットと自動化の範囲」)。

### 5.2 追加 2: 改善を Codex にする

主シナリオとの差分です。段 4 で `LLM の構成:` に `2` (取引判断は Claude、改善の既定は Codex) を選び、`改善の backend [既定: 4]:` で `4` を選びます。

- Codex CLI が導入・ログイン済みで、`runner.codex.bin` が実体 (ELF) を指していれば、`codex: 導入とログインを確認しました。` が出て、モデルの一覧 (`改善のモデル (番号 / m = 手入力)`、10 件を超えると先に `改善のモデルの絞り込み (部分一致。空 Enter で全件): `) が出ます。既定は名前に `terra` を含むものです。
- 実体が見つからない (例えば node のラッパーを指している) と、`codex が見つかりません (bin_not_found)。…` と `[r] / [c] / [d]` の選択肢、`c` の後に「`runner.codex.bin` には codex の実体 (node のラッパーではなく ELF) の絶対パスを入れます。」と `codex の実体の絶対パス (Enter = 後で settings.yaml に自分で書く): ` が出ます。「2.3 Codex CLI」で調べた絶対パスを入力します。
- 台本: `tests/acceptance/scripts/codex-improve.json`。実体のパスは環境変数 `CODEX_ELF` に入れて渡します。

確かめること: 完了後 `settings.yaml` の `runner.improve.backend` が `codex`、`runner.codex.bin` が入力した絶対パス。ELF でないパスを入れると `bin_not_found` に戻る。

### 5.3 追加 3: 別ホストの llama-swap

段 4 で `3` (別ホストの llama-swap の URL を入れる) を選び、`llama-swap の URL [...]:` に URL を入れます。

| 入れる URL | 期待する表示 |
|---|---|
| 動いている llama-swap | モデル一覧 (`llama-swap のモデル一覧 (取得だけで、モデルはロードしません):`)、取引判断のモデルの番号、改善の backend の選択 |
| 誤り (接続できない) | `接続できません (connect_error)。` と `[r] 構成の選び直し / [m] モデル名を手入力して未確認のまま進む / [4] 後で設定する [既定: r]:` |
| 認証を要する URL | `この URL は認証を要求しています (auth_unsupported)。ウィザードは認証つきの llama-swap に対応していません。認証不要の URL を使うか、後で設定してください。` |
| 空のモデル一覧 | `モデル一覧が空です (model_not_found)。` |

`m` を選ぶとモデル名を手入力し、「未確認」(`unverified`) で記録して進みます。

### 5.4 追加 4: CLI 未導入・未ログインで完了する

Claude CLI を PATH から外した状態 (`bin_not_found`)、またはログイン前の状態 (`credentials_missing`) で `afx init` を最後まで通します。

- 段 4 の `LLM の構成:` で `1` を選ぶと、`claude が見つかりません (bin_not_found)。…` または `claude にログインしていません (credentials_missing)。…` と、`[r] 導入・ログイン後に再試行 / [c] 未確認のまま進む / [d] 後で設定する [既定: c]:` が出ます。`c` で進みます。取引判断のモデルを選び、改善の backend (`3`) でもう一度同じ選択肢が出るので `c` を選びます。1 往復試験と実往復の問いは出ません (確認が通っていないため)。
- 完了後の期待:
  - `data/state/setup.json` に `llm_trade` / `llm_improve` の `reason_code` として `bin_not_found` または `credentials_missing` が残る。
  - 段 13 で `afx check の warning (unit は置きます): …` が出て、unit は置かれる (warning だけなら起動を妨げない)。
  - 起動後は取引判断が止まった degraded で、段 14 の `未完の手順:` に「取引判断の LLM の LLM は未確認です (<理由>)」のような行が出る。同じ内容が `afx ctl status` の末尾にも出る。
  - 未完の手順を解消するには、「2. 前提: CLI の導入」を済ませてから `uv run afx init` を再実行する。
- 台本: `tests/acceptance/scripts/cli-missing.json`。

### 5.5 追加 5: `local` を既定にしないことの確認

主シナリオでは llama-swap が無いので、段 4 に `local.` の行も既定も出ません。`LLM の構成:` に Enter だけを打っても通らず、`1 / 2 / 3 / 4 のどれかで答えてください。` と出ます。あえて local を選ぶ手段は、llama-swap が応答しているとき (トンネルなどで :8080 を見せているとき) の `local.` だけです。

- 応答しない環境で、別ホスト (3) の URL に `http://localhost:8080/v1` を入れて `m` (モデル名を手入力して未確認のまま進む) を選ぶと、モデル一覧が取れないまま local の構成で進みます。このとき記録は `unverified`、未完の手順に「LLM は未確認です」が出ます。

### 記録に残る固定語彙

`data/state/setup.json` の `reason_code` は次の固定語彙だけです (自由文・URL・例外文面は保存しません)。

| 語彙 | 意味 | 主に出る所 |
|---|---|---|
| `bin_not_found` | CLI が PATH に無い (codex は ELF の実体でない場合も) | 段 4 |
| `credentials_missing` | CLI にログインしていない | 段 4 |
| `auth_unsupported` | 別ホストの llama-swap が認証を要求する | 段 4 (3) |
| `model_list_unavailable` | codex / opencode のモデル一覧を取れなかった | 段 4 |
| `unverified` | (`result` の値) 確認していない | 段 3・段 4 |
| `deferred_by_user` | 「後で設定する」を選んだ | 段 4 (4) |
| `user_declined_probe` | 1 往復 / 問い合わせの試験を断った | 段 3・段 4 |

そのほか `connect_error` / `model_not_found` / `auth_required` / `server_time_unknown` などが出ることがあります。

---

## 6. 観測表

自動試験では確かめられないことを、実機で確かめます。各行の観測コマンドは貼ってそのまま使えます。ここでは `afx` サービスの名前を `afx.service` と書きます。なお、`scripts/collect-first-run-diagnostics.sh` が、この表の読み取りだけの観測を 1 つの tar.gz にまとめます (restart / stop / marker の操作を伴うものは手で行います)。

| 項目 | 期待 |
|---|---|
| 1. 実効の unit | 設置した本文と一致。`Type=exec`、`NotifyAccess=none`、`KillMode=control-group`、`DelegateControllers` に `pids` と `memory`、`MainPID` が `python main.py --daemon`、`TimeoutStopUSec=10min 30s`、`RestartPreventExitStatus=75 76` |
| 2. cgroup の形 | afx が unit root から `supervisor/` へ自己移動した位置にあり、mission ごとの cgroup が作られる |
| 3. linger とログアウト後の存続 | `Linger=yes` なら存続。`no` ならログアウトで止まり、ウィザードが案内を出していたこと |
| 4. 今回の起動の journal | 今回の起動の admission 行が読め、前回の起動の行と区別できる。`admitted=yes`。field が契約と違えば契約の field 名を直す |
| 5. 終了コード 75 / 76 の非再起動 | 終了コード 76 (enroll 中なら 75)、`NRestarts=0`、再起動されない |
| 6. 依存の実効値 | afx-ops の設計 (unit 契約の表) と一致 |
| 7. ops の ready と API 認証 | READY は操作 API の認証後。鍵なしは拒否 |
| 8. ops の restart 連動 | ops が追随して ready に戻る / 一緒に止まる |
| 9. リセットからのやり直し | clean clone と同じ。再実行も同じ流れで通る。別 instance の資材があれば触らず表示される |
| 10. 環境差 | Ubuntu 22 (主): 全項目が期待どおり。systemd 有効の WSL: 対話の流れを確かめる用 (`Delegate` が効かず admission が `cgroup_topology_unsupported` でも、それは環境差であって失敗ではない)。systemd 無効の WSL: `systemctl --user` が使えず、unit を置かず手動起動の案内で完了する |

### 6.1 実効の unit

```
systemctl --user cat afx.service
systemctl --user show afx.service -p Type -p NotifyAccess -p KillMode -p DelegateControllers -p MainPID -p TimeoutStopUSec -p SendSIGKILL -p FinalKillSignal -p TimeoutStopFailureMode -p RestartPreventExitStatus -p Restart -p RestartUSec
ps -o args= -p "$(systemctl --user show afx.service -p MainPID --value)"
```

### 6.2 cgroup の形

```
pid=$(systemctl --user show afx.service -p MainPID --value)
cat /proc/$pid/cgroup
group=$(systemctl --user show afx.service -p ControlGroup --value)
find /sys/fs/cgroup"$group" -maxdepth 2 -type d
```

`/proc/<pid>/cgroup` の末尾が `…/afx.service/supervisor` になっていること。mission を実行しているあいだ (改善を走らせたとき) は、`find` に mission ごとの dir が増えます。

### 6.3 linger とログアウト後の存続

```
loginctl show-user "$USER" -p Linger
```

`Linger=yes` のときは、いったんログアウトして再接続し、`systemctl --user is-active afx` が `active` のままであることを確かめます。`no` のときは止まります (止まること自体と、ウィザードが案内を出していたことが期待です)。

### 6.4 今回の起動の journal (観測契約の実機確認)

afx の起動前後で、今回の起動を区別できる手段 (InvocationID) が journal にあるかを確かめます。契約は設計書の「起動後確認」の節にあり、次の手順です。

```
# 起動の直前
systemctl --user show afx.service -p InvocationID --value      # 前回の InvocationID (空のことがある)
journalctl --user -u afx.service -n 0 --show-cursor            # 末尾の「-- cursor: …」を控える (fallback 用)
systemctl --user start afx
# 起動のあと (前回と違う、空でない値になるまで数秒かかることがあります)
systemctl --user show afx.service -p InvocationID --value
# journal の field の実在
journalctl --user -u afx.service -o json --no-pager | grep -c '_SYSTEMD_INVOCATION_ID'
journalctl --user -u afx.service -o json --no-pager | grep -c 'USER_INVOCATION_ID'
# 今回の admission 行 (<ID> は上で得た今回の InvocationID)
journalctl --user -u afx.service -o json --no-pager | grep '<ID>' | grep 'improve isolation'
# fallback: cursor より後ろだけ
journalctl --user -u afx.service --after-cursor='<控えた cursor>' | grep 'improve isolation'
```

確かめること:

1. 起動前後で `InvocationID` が変わる。
2. `_SYSTEMD_INVOCATION_ID` と `USER_INVOCATION_ID` のどちらが `-u afx.service` の行に付くか (どちらも 0 件なら cursor の fallback になる)。**Ubuntu 22 と systemd 有効の WSL の両方で**確かめ、結果を記録する。
3. 今回の admission 行 (`improve isolation: enabled; backend=… admitted=yes|no [reason=…]`) が読め、前回の起動の行と区別できる。
4. field が契約と違っていたら、契約の field 名だけを直す (設計書の起動後確認の節の手順 3)。

### 6.5 終了コード 75 / 76 の非再起動

afx が停止している状態で行います。

```
systemctl --user stop afx
mkdir -p data/state && : > data/state/setup_in_progress     # 試験用の印 (中身は見ません。在るだけで 76)
systemctl --user start afx
systemctl --user show afx.service -p ExecMainStatus -p NRestarts -p Result
rm -- data/state/setup_in_progress                           # 印を消す
systemctl --user reset-failed afx
```

期待: `ExecMainStatus=76`、`NRestarts=0`。再起動されない (`RestartPreventExitStatus=75 76`)。終了コード 75 は `afx ops enable` の途中で止まった状態 (`data/ops/enroll_in_progress` がある間) の起動拒否で、通常は再現しません。確かめるなら ops の有効化を途中で中断した状態が要ります。

### 6.6 依存の実効値

```
systemctl --user show afx.service -p Wants -p After
systemctl --user show afx-ops@web.service -p Wants -p Requisite -p PartOf -p After     # web を有効にした場合。feature 名は置き換える
```

期待 (ops の unit 契約): `afx-ops@<feature>.service` は `Requisite=afx.service`、`PartOf=afx.service`、`After=afx.service` を持つ。`afx.service` は `Wants=afx-ops@<feature>.service` を持つ (drop-in `afx.service.d/afx-ops.conf`)。

### 6.7 ops の ready と API 認証

```
systemctl --user is-active afx-ops@web
uv run afx ops status web
uv run afx ctl status
# 鍵が見えない環境の拒否 (HOME を空の dir に向ける。鍵は $HOME/.config に置かれる)
d=$(mktemp -d) && HOME=$d .venv/bin/afx ctl status; echo "rc=$?"; rmdir "$d"
```

期待: ops が `active` で、`afx ops status` が ready、`afx ctl status` が成功する。ops の READY は操作 API の server 認証が通った後にだけ送られる。鍵が見えない環境では `afx ctl status` が rc=3 で拒否する。

### 6.8 ops の restart 連動

```
systemctl --user restart afx
systemctl --user is-active afx afx-ops@web        # 数十秒待つ。ops が ready に戻るまで時間がかかります
systemctl --user stop afx
systemctl --user is-active afx-ops@web            # inactive (一緒に止まる)
systemctl --user start afx                        # 試験の後は起動し直す
```

期待: restart で ops も再起動して ready に戻る。stop で ops も一緒に止まる。

### 6.9 リセットからのやり直し

```
scripts/first-run-reset.sh --dry-run
scripts/first-run-reset.sh
git status --porcelain --ignored
uv run afx init
```

期待: リセット後の `git status` は clean clone と同じです。ただし afx 稼働で生成され、残ることを許可する差分は `.venv/`、`afx-ops/.venv/`、`.pytest_cache/`、`.ruff_cache/`、`**/__pycache__/`、`tmp/`、および既定で残す `.env` です。もう一度 `afx init` が同じ流れで通る。同じホームに別の instance (別の clone) の資材があれば、「触らない」と表示されて変更されない。

### 6.10 環境差

```
. /etc/os-release && echo "$PRETTY_NAME"
uname -r
systemctl --version | head -1
stat -fc %T /sys/fs/cgroup
grep -qi microsoft /proc/version && echo WSL || echo not-WSL
```

---

## 7. 詰まったときに貼る情報

次を集めて貼ってください。**秘密と URL は貼らないでください。** (API キー・token・webhook URL・llama-swap や bridge の URL・`.env`・`config/settings.yaml` の URL を含む行・ops の secret ファイルの中身・操作 API の鍵ファイルの中身は、どれも貼りません。)

1. ウィザードの画面の該当部分 (入力した値に URL や鍵があれば消してから)。
2. 「6. 観測表」の観測コマンドの出力。まとめて集めるには:

   ```
   scripts/collect-first-run-diagnostics.sh
   ```

   読み取りだけで、`tmp/acceptance/diag-<時刻>.tar.gz` ができます。URL と KEY / TOKEN / SECRET / PASSWORD に続く値は伏せてありますが、貼る前に中身を見て確かめてください。`.env` と `config/settings.yaml` と ops の secret・API の鍵は集めません。

3. サービスのログ:

   ```
   journalctl --user -u afx -n 100
   ```

4. `data/state/setup.json` の固定語彙だけ (結果と理由の語彙以外は抜かない):

   ```
   jq -r '.. | objects | to_entries[] | select(.key=="result" or .key=="reason_code" or .key=="state") | "\(.key)=\(.value)"' data/state/setup.json
   ```

   `jq` が無ければ python でも抜けます。

   ```
   python3 -I -c 'import json,sys
   def walk(n,p=""):
       if isinstance(n,dict):
           [print(p+k+"="+v) if k in ("result","reason_code","state") and isinstance(v,str) else walk(v,p+k+".") for k,v in n.items()]
       elif isinstance(n,list):
           [walk(v,p) for v in n]
   walk(json.load(open(sys.argv[1])))' data/state/setup.json
   ```

---

## 8. 試験環境の都合 (試験者向け)

この節は実機受入を行う人向けです。利用者向けの本文と README には入れません。

- **サブマシン (Ubuntu 22) には local LLM と MT5 が無い。** 本環境の bridge (:8812) と llama-swap (:8080) を SSH トンネルでサブマシンの localhost に見せます。製品側の既定 URL (`http://localhost:8812`、`http://localhost:8080/v1`) をそのまま使い、製品のコードと設定の既定はトンネルの存在を知りません。

  ```
  ssh -N -L 8812:127.0.0.1:8812 -L 8080:127.0.0.1:8080 <本環境のホスト>
  ```

  トンネルは user unit で常駐させます (`Restart=always`、`ServerAliveInterval` つき)。例 (`~/.config/systemd/user/afx-tunnel.service`):

  ```
  [Unit]
  Description=SSH tunnel to the test host (bridge and llama-swap)
  After=network-online.target
  Wants=network-online.target

  [Service]
  ExecStart=/usr/bin/ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 8812:127.0.0.1:8812 -L 8080:127.0.0.1:8080 <本環境のホスト>
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=default.target
  ```

  `systemctl --user daemon-reload && systemctl --user enable --now afx-tunnel` で起動します。
- **トンネルは主シナリオを歪めます。** :8080 を転送していると llama-swap が応答するため、段 4 の既定が `local` になり、「local 無し」の分岐を通りません。主シナリオ (5.1〜5.5) は :8080 の転送を止めて行います (`systemctl --user stop afx-tunnel`、または :8080 の `-L` を外した別の unit)。追加シナリオ 3 (別ホストの llama-swap) と、MT5 bridge を使う分岐 (段 3 の `1`) の確認は、転送を有効にして行います。
- **発注系の endpoint が届きます。** トンネルで本環境の bridge に繋ぐので、サブマシン側から本環境へ発注系の endpoint が届きます。受入は価格の読み取りだけを行い、`DRY_RUN` の設定を変えません。

## 9. リセットと自動化の範囲 (試験者向け)

- **1 回目の受入は人の手で、この手順書どおりに行います。** 案内の欠陥を見つけるのが目的なので、自動化しません。
- **2 回目以降は SSH 経由で自動化します。** 回帰の確認用で、案内の評価は 1 回目に行います。

  1. リセット:

     ```
     scripts/first-run-reset.sh --yes --stop-running --include-linger
     ```

     `--include-linger` は解除の許可であって、所有判定の代替ではありません。ウィザードの所有記録 (`data/state/setup.json` の `linger.state=owned_by_wizard`) が無ければ何も変えません。これを付けないと、1 回目に所有した linger が残り、2 回目のウィザードが `already_enabled` と記録して所有を失い、以後のリセットで解除できなくなります。
  2. 対話 driver で `afx init` を通します (台本の答えを送り、出力を記録します)。

     ```
     python3 -I tests/acceptance/first_run_driver.py --help
     python3 -I tests/acceptance/first_run_driver.py --dry-run --script tests/acceptance/scripts/main-claude.json   # 台本の検証だけ
     python3 -I tests/acceptance/first_run_driver.py --script tests/acceptance/scripts/main-claude.json
     CODEX_ELF=/path/to/codex-elf python3 -I tests/acceptance/first_run_driver.py --script tests/acceptance/scripts/codex-improve.json
     python3 -I tests/acceptance/first_run_driver.py --script tests/acceptance/scripts/cli-missing.json
     ```

     - 台本は JSON の配列 `[{"expect": "<問いの部分文字列>", "send": "<入力>"}, …]` です。`expect` は子が入力を待っている行に含まれる文字列で、合う問いにだけ答えます。合わない問いが来たら「予期しない問い」として記録し、何も送らずに止めます (勝手に Enter を送りません)。`optional` を付けた要素は、その問いが出なければ飛ばします。
     - 予期しない問いの判定は timeout より先です。出力が止まって最終行がどの `expect` にも合わない状態が `min(--unexpected-after, 有効な 1 問の上限)` 秒続くと、終了コード 3 で止めます。終了コード 4 は、出力が続いたまま上限を超えたときと、最終行が `expect` に合うのに子が入力を受け取らないときだけです。長い処理が静かに続く問いには、台本の `timeout` を `--unexpected-after` より長くし、`--unexpected-after` も大きくしてください (推奨: 既定の 30 秒のまま、静かに続く問いの前だけ `timeout` を伸ばす)。止まったら `scripts/first-run-reset.sh` で戻してから最初からやり直します。
   - 全体の上限は `--total-timeout`、1 問ごとの上限は `--step-timeout` (台本の要素の `timeout` が優先) で、超えると止めて終了コード 4 を返します。終了コードは 0 完走 / 1 台本が済んだあと子が非 0 / 2 使い方か台本の誤り / 3 予期しない問い / 4 timeout / 5 台本が残ったまま子が終了。
     - 記録は `tmp/acceptance/<UTC 時刻>/transcript.txt` (`.gitignore` 済み) に残ります。**秘密と URL は記録から除きます**: `http` / `key` / `token` / `secret` / `password` を含む行は丸ごと `***` になります。台本にも秘密や URL は書かないでください。環境変数で渡した値と `send` の値は、4 文字以上なら記録のどこに出ても `***` になります。**4 文字未満の値は伏せません**。秘密 (鍵・token・URL) を台本の値に書かないでください。番号や y/n は伏せる対象ではありません。
     - 段 11 の標準指標は 1 本ずつ審査するため長くかかります。台本はその直後の問いの上限を 3600 秒にしてあります。
  3. 診断の収集:

     ```
     scripts/collect-first-run-diagnostics.sh
     ```

     観測表の読み取りだけの観測コマンドを束ねて `tmp/acceptance/diag-<時刻>.tar.gz` にします。先頭に収集するものと収集しないもの (`.env`・`config/settings.yaml`・ops の secret・API の鍵) を表示します。unit が無い・journalctl が使えない場合は「取得できず」と記録して続け、終了コードは 0 です。
- 台本が参照する文面は、実装 (`src/agentic_fx/setup/wizard.py` など) の文字列です。ウィザードの文面を変えたら、`tests/acceptance/test_first_run_driver.py` が台本の `expect` と実装の文面を突き合わせて落ちるので、台本とこの文書を一緒に直します。
