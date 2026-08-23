"""版ストア (`plugins/.versions/<name>/<artifact_hash>/`) の作成・不変化・
`GC_ROOTS` 判定 (プラン 10 Task 11、設計書 §2.3・§5.1)。

- `content_hash_bytes` は既存 `plugin/loader.content_hash` の定義 (spec
  逐語、不変) を bytes 引数から計算する薄いラッパ。`loader.content_hash`
  はこの関数へ委譲するよう Task 5/本 task で更新する (二重実装しない)。
- `artifact_hash_bytes` は 3 本全体 (`plugin.py` + `config.yaml` +
  `test_plugin.py`) の新設ハッシュ — 版ストアのキー。
- 版ストアは不変 (0400/0500)。人間の編集場所ではない。
"""
from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
from pathlib import Path


def content_hash_bytes(plugin_py: bytes, config_yaml: bytes) -> str:
    """既存 content_hash の定義 (spec 逐語、不変)。"""
    return hashlib.sha256(
        b"plugin.py\0" + plugin_py + b"\0config.yaml\0" + config_yaml
    ).hexdigest()


def artifact_hash_bytes(plugin_py: bytes, config_yaml: bytes,
                        test_plugin: bytes) -> str:
    """版ストアのキー (3 本全体)。"""
    return hashlib.sha256(
        b"plugin.py\0" + plugin_py + b"\0config.yaml\0" + config_yaml
        + b"\0test_plugin.py\0" + test_plugin
    ).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_ro_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o400)


def create_version_dir(root: Path, name: str, artifact_hash: str, *,
                       plugin_py: bytes, config_yaml: bytes,
                       test_plugin: bytes, op_identity: str) -> Path:
    """`plugins/.versions/<name>/<artifact_hash>.tmp-<op_identity>/` へ書き
    fsync し、0400/0500 に落として rename で
    `plugins/.versions/<name>/<artifact_hash>/` へ。既に同 artifact_hash の
    版があれば作り直さず、tmp を作った場合はそれを削除して既存版を返す
    (冪等 — §5.1 手順 4)。
    """
    name_dir = root / ".versions" / name
    name_dir.mkdir(parents=True, exist_ok=True)
    final_dir = name_dir / artifact_hash
    if final_dir.is_dir():
        return final_dir

    tmp_dir = name_dir / f"{artifact_hash}.tmp-{op_identity}"
    if tmp_dir.exists():
        # 同一 op_identity での再試行 (再起動後の頭からの冪等再実行) —
        # 内容を作り直す (中途半端な内容の可能性があるため削除してやり直す)
        import shutil
        shutil.rmtree(tmp_dir)
    # precheck 2026-08-22 wave2: T11-M2
    tmp_dir.mkdir(mode=0o700)
    try:
        _write_ro_file(tmp_dir / "plugin.py", plugin_py)
        _write_ro_file(tmp_dir / "config.yaml", config_yaml)
        _write_ro_file(tmp_dir / "test_plugin.py", test_plugin)
        _fsync_dir(tmp_dir)
        os.chmod(tmp_dir, 0o500)
        try:
            os.rename(tmp_dir, final_dir)
        except OSError as e:
            # M-2 是正 (実測): 非空ディレクトリへの os.rename は
            # OSError errno 39 (ENOTEMPTY) であり FileExistsError (EEXIST)
            # ではない — final_dir は常に 3 ファイルを持つ非空ディレクトリ
            # なので、並行プロセスが先に同じ artifact_hash を作った場合は
            # 必ず ENOTEMPTY になる。EEXIST/ENOTEMPTY のどちらでも「並行
            # プロセスが先着した」ことの表明として冪等に扱い、それ以外の
            # errno は fail closed で再送出する。
            if e.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            # 並行プロセスが先に同じ artifact_hash を作った (冪等) —
            # 自分の tmp を掃除して既存版を採用する
            os.chmod(tmp_dir, 0o700)
            import shutil
            shutil.rmtree(tmp_dir)
            return final_dir
        _fsync_dir(name_dir)
        return final_dir
    except BaseException:
        # 失敗時は tmp を残す (reconcile が §5.1 手順 4 の「tmp-* の残骸は
        # reconcile が削除する」規則で掃除する) — ここでは削除しない
        # (M-3: 素の再送出だが、コメントが「delete しない」という意図を
        # 明示する役割を持つため保持する)
        raise


def gc_roots(conn: sqlite3.Connection, *, plugins_root: Path) -> frozenset[Path]:
    """§5.1 の唯一の定義 (実装は 11f で完成させる — ここでは骨格のみ書き、
    11f が journal/legacy_plain_present の集合を追加する)。他節はこの関数の
    結果だけを参照し、集合をその場で再展開しない (codex 14 周目 M1)。
    """
    raise NotImplementedError("11f で実装する — 4 集合の union")
