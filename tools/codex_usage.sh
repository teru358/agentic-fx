#!/usr/bin/env bash
# codex の残使用量 (5 時間枠 / 7 日枠) を app-server の JSON-RPC で読む。
# 参考: https://qiita.com/tatsuya582/items/5ca0c12a8495530f7d09
# 使い方: tools/codex_usage.sh        → 1 行要約
#         tools/codex_usage.sh --json → 生の rateLimits
set -u
raw=$( { printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"agentic-fx-usage","version":"1.0"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}'
    sleep 6
  } | codex app-server 2>/dev/null | grep '"id":2' | head -1 )
[ -z "$raw" ] && { echo "codex app-server から応答なし"; exit 1; }
if [ "${1:-}" = "--json" ]; then echo "$raw" | python3 -c 'import sys,json;print(json.dumps(json.load(sys.stdin).get("result",{}).get("rateLimits"),indent=1))'; exit; fi
python3 - "$raw" <<'PY'
import sys,json,datetime as dt
r=json.loads(sys.argv[1]).get("result",{}).get("rateLimits") or {}
def fmt(k,label):
    w=r.get(k) or {}
    if not w: return f"{label}: n/a"
    rs=w.get("resetsAt"); t=dt.datetime.fromtimestamp(rs,dt.timezone(dt.timedelta(hours=9))).strftime("%m/%d %H:%M JST") if rs else "?"
    return f"{label}: {w.get('usedPercent')}% used (reset {t})"
print(fmt("primary","5h"),"|",fmt("secondary","7d"),"|","plan:",r.get("planType"))
PY
