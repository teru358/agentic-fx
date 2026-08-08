# Task 7 変異テスト結果 (指揮者実施, 2026-08-08)

ベースライン: 1459 → **1477 passed** (+18: `test_mission_protocol.py` 6 本 + `test_mission_worker_protocol.py` 12 本)。
実施規律: 毎回**正典 (プラン逐語コピー)** から復元 → 注入 → **diff を表示して目視確認** → `__pycache__` 削除 → pytest → 復元。

> 注: 初回実行では注入内容の表示が全件空だった (新規ファイルは untracked のため `git diff` に出ない)。
> 正典との difflib 比較に直して**生存 16 件を再実行し、全件で注入内容を目視確認済み**。

## 結果一覧

### プラン Step 11 記載の変異 (9 件 → 6a/9 は分割して 11 ケース)

| # | 変異 | 結果 | red になったテスト |
|---|---|---|---|
| 1 | `SeqTracker.check` の条件を `if False:` に | **red** | rejects_duplicate / _gap / _regression / rejects_seq_gap / wrong_seq / in_seq_continues (6 件) |
| 2 | `_set_pdeathsig` 後の ppid 照合を削除 | **生存** | — (プランに「Task 7 単体では観測不能・Task 10 E2E で拾う」と明記済み) |
| 3 | `_call` の `ok` チェックを削除 | **red** | propagates_error |
| 4 | `main()` が `_RagRpcProxy` に渡す `out_seq` を独立インスタンスに | **生存** | — ← **プラン記載の期待と食い違い** |
| 5a | `_call` の type チェックを削除 | **red** | rejects_wrong_frame_type |
| 5b | `_call` の `in_seq.check` を削除 | **red** | rejects_seq_gap / in_seq_continues_after_handshake |
| 6a | `main()` の handshake type チェックを削除 | **生存** | — ← **プラン記載の期待と食い違い** |
| 6b | `main()` の handshake `in_seq.check` を削除 | **red** | wrong_seq (指揮者が着手前に追加したテスト) |
| 7 | `_build_clock` を `FixedClock` 固定に | **red** | build_clock_default_rejects_stale_signal |
| 8 | `_make_on_message` の try/except + `os._exit` を削除 | **red** | on_message_exits_process_on_write_failure |
| 9 | `main()` が `_RagRpcProxy` に渡す `in_seq` を独立インスタンスに | **生存** | — ← **指揮者が着手前に追加した変異。同じく食い違い** |

### 自主追加変異 (17 件 — 「リストは下限であって天井ではない」)

| # | 変異 | 結果 |
|---|---|---|
| A | `SeqTracker.check` の int 型検証を削除 | **生存** |
| B | `SeqTracker.check` のカウンタ前進を削除 | red (4 件) |
| C | `write_frame` の `flush()` を削除 | **生存** |
| D | `read_frame` の EOF 判定を反転 | (アンカー不一致 — 未実施) |
| E | `_call` の `rpc_id` 照合を削除 | **生存** |
| F | `_call` の EOF (`response is None`) 判定を削除 | **生存** |
| G | `_RagRpcProxy._seq_next` のカウンタ前進を削除 (seq 固定) | **生存** |
| H | `_make_on_message` の `os._exit(1)` → `os._exit(0)` | red |
| I | `_set_pdeathsig` の戻り値チェックを無効化 | red |
| J | `_set_pdeathsig` の prctl 呼び出しごと削除 | red (2 件) |
| K | `main()` の `worker_profile != "trade"` チェックを削除 | **生存** |
| L | `main()` の `backend != "local"` (ClaudeRunner fail-closed) を削除 | **生存** |
| M | `main()` の `build_mission_registry(..., readonly=True)` を外す | **生存** |
| N | `_set_resource_limits` の `RLIMIT_AS` 設定を削除 | **生存** |
| O | `_protect_protocol_stdout` の `dup2` を削除 | **生存** |
| P | `main()` の `ready` 送出を `ok=False` に反転 | **生存** |
| Q | `runner.run` 例外時の `result` 送出 (fail closed) を削除 | **生存** |

## 生存 16 件の根本原因 (1 つ)

**`main()` の本体に一切テストが無い。** プラン Step 5 のテスト 12 本の内訳:

- `_RagRpcProxy` 単体 6 本 / `_make_on_message` 1 本 / `_set_pdeathsig` 2 本 / `_build_clock` 1 本
- `main()` を呼ぶのは 2 本だけ (`wrong_type` / `wrong_seq`) で、**どちらも最初の数行で例外を投げて外側 `except` に落ちる経路**しか通らない

結果、`main()` の bootstrap 本体 (ppid 照合・rlimit・profile 検証・**backend fail-closed**・`readonly=True`・`out_seq`/`in_seq` の共有配線・`ready` 送出・`result` 送出) は**全て無防備**。
`_set_resource_limits` と `_protect_protocol_stdout` も同様に未テスト。

これは既知の教訓 3 つの複合:
- 「変異リストは天井でなく下限」— リスト外の防御 11 件が無防備
- 「配線そのものを検証する」— 単体は全部緑だが `main()` の配線が誰にも検証されていない
- 「1 site の red で防御全体を守られていると誤判断しない」— `_RagRpcProxy` 単体の seq 共有テストは**契約**を pin するが `main()` の**配線**を pin しない (CR-3 の再発を防げない)

### 特に重大

- **追加L** — `runner.trade.backend != "local"` の fail closed は **Global Constraints (Anthropic API 従量課金は使用不可)** の強制点。削除しても緑
- **変異4 / 変異9** — CR-3 (レビューで実際に見つかった不具合) の回帰が、`main()` 側では**再発しても検出されない**
- **追加M** — CR-4 (`readonly=True`) も同様
- **追加N** — rlimit (§12 申し送り②の確定値) が実際に設定されるかを誰も確かめていない

## プラン記述側の欠陥 (レビューへの申し送り)

1. **Step 11 変異 4 は無効** — `main()` の call site を変異させても、`test_rag_rpc_proxy_shares_out_seq_...` は `_RagRpcProxy` を直接構築するため red にならない。指揮者が着手前に追加した変異 9 も同じ誤り (同じ穴を踏んだ)
2. **Step 11 変異 6a も無効** — type チェックを削除しても `test_main_rejects_handshake_with_wrong_type` は緑。理由: 後続の `handshake["expected_parent_pid"]` が `KeyError` を投げ、外側 `except` が同じ `ready: ok=False` を返すため。テストが**エラー内容を検証していない**

---

# 対応: `main()` の配線テストを追加 (指揮者, 2026-08-08)

生存 16 件の根本原因が 1 つ (`main()` 未テスト) だったため、レビュー前に塞いだ。
`tests/test_mission_worker_protocol.py` に **12 本追加 + 既存 1 本にアサート追加**:

| 追加テスト | 塞いだ変異 |
|---|---|
| `test_main_happy_path_emits_ready_event_result_in_one_seq_sequence` | 追加P (ready ok 反転) |
| `test_main_shares_out_seq_and_in_seq_with_rag_rpc_proxy` | **変異4 / 変異9** (CR-3・I2 の配線) |
| `test_main_builds_registry_readonly_and_applies_resource_limits` | **追加M** (CR-4 readonly) |
| `test_main_reports_result_failed_when_runner_raises` | 追加Q |
| `test_main_exits_without_ready_when_reparented` | **変異2** (プランは「Task 7 単体では観測不能」としていたが pin 可能だった) |
| `test_main_rejects_unsupported_worker_profile` | 追加K |
| `test_main_fails_closed_when_runner_backend_is_claude` | **追加L (Global Constraints 強制点)** |
| `test_set_resource_limits_sets_all_four_limits` | 追加N |
| `test_seq_tracker_rejects_bool_as_seq` | 追加A |
| `test_rag_rpc_proxy_rejects_rpc_id_mismatch` | 追加E |
| `test_rag_rpc_proxy_raises_when_parent_closes_pipe` | 追加F |
| `test_rag_rpc_proxy_advances_out_seq_across_successive_calls` | 追加G |
| `test_main_rejects_handshake_with_wrong_type` に `assert "handshake" in sent["error"]` を追加 | **変異6a** |

## 再検証結果

**生存 16 件 → 2 件**。全 16 件を再注入し、上表のテストが red になることを目視確認込みで実測した。

## 残存 (accepted-unpinned — 意図的に pin しない)

| # | 変異 | 判断 |
|---|---|---|
| 追加C | `write_frame` の `flush()` 削除 | pin には実 fd とバッファリング挙動の観測が要る。子→親は行志向 JSON で親が `readline` するため、flush 欠落は**実 subprocess でのみ**症状が出る。**Task 10/20 の実 spawn テストで拾う** |
| 追加O | `_protect_protocol_stdout` の `dup2` 削除 | 同上。fd 1 の付け替えは実プロセスでしか観測できない (インプロセスでは `_protect_protocol_stdout` 自体を monkeypatch しているため)。**Task 20 の E2E で拾う** |

いずれも「テストが無いことに気づいていない」のではなく、**Task 7 の観測範囲外**と判断して次 task へ送る。レビュアーはこの判断の妥当性を見てほしい。

## 最終状態

- `uv run pytest -q` = **1489 passed, 1 deselected** (ベースライン 1459 + 30)
- `mission_protocol.py` / `mission_worker.py` はプラン逐語コードと **byte 一致** (変異残留なし・機械照合済み)
