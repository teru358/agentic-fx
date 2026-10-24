---
id: runner-fakeproc-self-pid-kills-pytest
title: tests/runners の FakeProc の自 pid で pytest が killpg される
status: 是正済
priority: 高
opened: 2026-10-24
closed: 2026-10-24
related: []
---
# [runner-fakeproc-self-pid-kills-pytest] tests/runners の FakeProc の自 pid で pytest が killpg される

**状態**: 是正済 / **優先**: 高

## 現象

test_worker_runner_cli_started_non_int_pgid_is_ignored の FakeProc が pid=os.getpid() で poll() が常に None のため、WorkerRunner._ensure_dead の killpg(proc.pid, SIGKILL) が pytest 自身の process group に届く。pytest が group leader のとき (.venv/bin/python -m pytest の直接起動、setsid) に exit 137 で死に、uv run 経由では uv が leader なので ESRCH で隠れていた。2026-10-24 の web-lan T1 判定で 2 回再現

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-24: main f3537cd: tests/runners/conftest.py を新設 (autouse の killpg ガード = pytest 自身の group / 0 への呼び出しは実行せず teardown で fail、別 session の犠牲用の子 pid を返す fixture sacrificial_pid)。当該 test の FakeProc.pid を犠牲用の子に。setsid 直接起動で tests/runners 398 green を指揮者が確認

## 経緯

- 2026-10-24: 起票。
- 2026-10-24: 是正内容 — main f3537cd: tests/runners/conftest.py を新設 (autouse の killpg ガード = pytest 自身の group / 0 への呼び出しは実行せず teardown で fail、別 session の犠牲用の子 pid を返す fixture sacrificial_pid)。当該 test の FakeProc.pid を犠牲用の子に。setsid 直接起動で tests/runners 398 green を指揮者が確認
