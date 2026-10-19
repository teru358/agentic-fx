"""承認の変更内容の表示に関わる上限と固定文の定義。

上限は 3 種類に分ける。値はここだけに置き、実装も test もこの定数を参照する。

- 入力読み取り上限: ディスクや API から読み込む量に掛ける byte の上限 (DoS 防止)。
- redaction 対象上限: redactor に渡す 1 値の byte の上限。超える値は切らずに全文を伏せる。
- 表示上限: 利用者に見せる完成した出力に最後に掛ける上限 (escape 後で数える)。
"""
from __future__ import annotations

KIB = 1024

# ---- 入力読み取り上限
MAX_FILE_BYTES = 256 * KIB
FLATTEN_MAX_DEPTH = 4
FLATTEN_MAX_ITEMS = 64
API_DIFF_BYTES = 512 * KIB
# 行単位の差分計算を掛ける 1 ファイルの行数の上限。超えるファイルは全置換の 1 hunk にする
DIFF_MAX_LINES = 20000
DIFF_CONTEXT = 3

# ---- redaction 対象上限
MAX_REDACT_VALUE_BYTES = 8 * KIB

# ---- 表示上限
PARAMS_DIFF_MAX = 64
PARAM_VALUE_CHARS = 80
SUMMARY_DISCORD_CHARS = 300
CLAIM_WEB_CHARS = 2000
DISCORD_BODY_CHARS = 2000
WEB_PAGE_BYTES = 64 * KIB
WEB_DIFF_BYTES = 48 * KIB
WEB_OTHER_BYTES = 15 * KIB
WEB_FILE_QUOTA_BYTES = 8 * KIB

# ---- redactor
REDACTED = "[redacted]"
KEY_PATTERN = r"[A-Za-z0-9_.]{1,64}"
TOKEN_MIN_LENGTH = 32
# 数字と英字が混じる列に掛ける閾値と、数字を含まない列 (大小文字混在のみ) に掛ける閾値
TOKEN_MIN_ENTROPY = 3.3
TOKEN_MIN_ENTROPY_NO_DIGIT = 4.0
ACCOUNT_MIN_DIGITS = 8

# ---- 分類 (API の machine enum)
CLASS_NEW = "new"
CLASS_UPDATED = "updated"
CLASS_SAME = "same"
CLASS_UNKNOWN = "unknown"
CLASSES = (CLASS_NEW, CLASS_UPDATED, CLASS_SAME, CLASS_UNKNOWN)
CLASS_LABELS = {CLASS_NEW: "新規", CLASS_UPDATED: "更新", CLASS_SAME: "同一版",
                CLASS_UNKNOWN: "不明"}

# ---- reason code と固定文 (表示面はこの文だけを出し、自由文を足さない)
UNAVAILABLE = "unavailable"
HASH_MISMATCH = "hash_mismatch"
TOO_LARGE = "too_large"
UNSAFE_SOURCE = "unsafe_source"
REDACTED_CODE = "redacted"
DETAIL_UNAVAILABLE = "detail_unavailable"
LEGACY_PAYLOAD = "legacy_payload"

REASON_TEXT = {
    UNAVAILABLE: "変更内容を取得できません",
    HASH_MISMATCH: "内容を検証できません",
    TOO_LARGE: "内容を検証できません",
    UNSAFE_SOURCE: "内容を検証できません",
    REDACTED_CODE: "機微情報の可能性があるためローカルで確認してください",
    DETAIL_UNAVAILABLE: "詳細を取得できません",
    LEGACY_PAYLOAD: "旧形式のため要約なし",
}

PLUGIN_FILES = ("plugin.py", "config.yaml", "test_plugin.py")
PLUGIN_KINDS = ("strategy", "indicator", "signal")
