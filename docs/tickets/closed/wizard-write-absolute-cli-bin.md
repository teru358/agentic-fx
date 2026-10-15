---
id: wizard-write-absolute-cli-bin
title: ウィザードは claude の実体を which で見つけても runner.claude.bin を絶対パスに書かない
status: 是正済
priority: 高
opened: 2026-10-15
closed: 2026-10-15
related: [first-run-setup, wizard-stage4-menu-per-role]
---
# [wizard-write-absolute-cli-bin] ウィザードは claude の実体を which で見つけても runner.claude.bin を絶対パスに書かない

**状態**: 是正済 / **優先**: 高

## 現象

example の既定は runner.claude.bin: claude (裸の名前)。段 4 の check_cli_backend は cli_candidate (which) で見つけて成功するが、settings には書かないので bin: claude のまま確定する。mission worker (mission_worker.py:266 Path(claude_bin)、factory.py:51) は裸の名前を cwd 相対に解決し /tmp/afx-mission-*/claude を探して FileNotFoundError → verify-backend 失敗 (verify_failed)、取引・改善 mission も claude backend では起動不能。clean install + 主シナリオで必ず再現 (2026-10-15 stream-svr 受入 2 回目)。是正: (1) ウィザードは claude を選んだとき which で解決した絶対パスを runner.claude.bin に書く (codex と同じ扱い。symlink は辿らず which の結果をそのまま)、(2) 防御として factory / mission_worker は裸の名前を shutil.which で解決し、見つからなければ固定 reason で fail closed (cwd 相対には決して解決しない)、(3) test: example 既定のまま claude を選ぶ台本で settings に絶対パスが書かれる / 裸の名前が cwd 相対に解決されないことの pin。回避 (受入中): settings.yaml の bin を /usr/local/bin/claude に手で直す

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-15: ウィザードは claude を選ぶと which で見つけた絶対パス (~ は展開) を runner.claude.bin に書く。core/cli_bin.resolve_cli_bin に解決を一本化: / を含む相対は拒否、裸の名前は PATH 検索の結果が絶対のときだけ、cwd には解決しない、見つからなければ CliBinNotFound → 親が bin_not_found。mission worker は入口で 1 回解決し prepare と bootstrap に同じ絶対パスを渡す。fingerprint の source に cli_bin。spec v1.7。codex r1 → /code-review r2 → codex r3

## 経緯

- 2026-10-15: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix1 (2026-10-15 ユーザー承認の設計: ブリーフ tmp/review-20261011-sc/fr-fix1-ab.md / fr-fix1-c.md)
- 2026-10-15: 是正内容 — ウィザードは claude を選ぶと which で見つけた絶対パス (~ は展開) を runner.claude.bin に書く。core/cli_bin.resolve_cli_bin に解決を一本化: / を含む相対は拒否、裸の名前は PATH 検索の結果が絶対のときだけ、cwd には解決しない、見つからなければ CliBinNotFound → 親が bin_not_found。mission worker は入口で 1 回解決し prepare と bootstrap に同じ絶対パスを渡す。fingerprint の source に cli_bin。spec v1.7。codex r1 → /code-review r2 → codex r3
