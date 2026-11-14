"""kill switch の利用者向け固定文。shell と `afx ctl` の両方が同じ文を出す。"""
from __future__ import annotations

# reset は HWM を付け直さない。数値は出さない (API の field にも event の field にも載せない)。
KILL_SWITCH_RESET_WARNING = "DD が閾値以上のままなら、次の新規判断で kill switch が再びラッチします。"
