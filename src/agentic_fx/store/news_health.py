"""Persistent, validated health state for individual news sources."""
from __future__ import annotations
import json, secrets, sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from agentic_fx.store import fetch_attempts

N = 3
PREFIX = "news_source_health."

@dataclass(frozen=True, slots=True)
class Health:
    consecutive_failures: int = 0; retry_after_streak: int = 0
    backoff_until: datetime | None = None; backoff_cause: str | None = None
    reset_at: datetime | None = None; reset_nonce: str | None = None
    last_outcome: str | None = None; last_at: datetime | None = None
    last_count: int | None = None; last_reason: str | None = None

@dataclass(frozen=True, slots=True)
class HealthRead:
    raw: str | None; record: Health | None; corrupt: bool

def _utc(v):
    if not isinstance(v, datetime) or v.tzinfo is None: raise ValueError("invalid datetime")
    return v.astimezone(timezone.utc)
def _name(name):
    if not isinstance(name, str) or any(ord(c) < 32 or ord(c) == 127 for c in name): raise ValueError("invalid name")
    return PREFIX + name
def stage(k: int) -> timedelta | None:
    if isinstance(k, bool) or not isinstance(k, int) or k < N: return None
    k = min(k, N + 10)
    return min(timedelta(minutes=30) * (2 ** (k-N+1)), timedelta(hours=24))
def _parse(raw):
    d=json.loads(raw)
    if not isinstance(d, dict) or d.get("v", 1) != 1: raise ValueError("invalid")
    def integer(k, nullable=False):
        x=d.get(k)
        if nullable and x is None:return None
        if isinstance(x,bool) or not isinstance(x,int) or x<0 or x>10**9: raise ValueError("invalid")
        return x
    def instant(k):
        x=d.get(k)
        if x is None:return None
        if not isinstance(x,str):raise ValueError("invalid")
        return _utc(datetime.fromisoformat(x.replace("Z","+00:00")))
    f=integer("consecutive_failures"); s=integer("retry_after_streak")
    cause=d.get("backoff_cause")
    if cause not in (None,"failures","refusals"):raise ValueError("invalid")
    outcome=d.get("last_outcome")
    if outcome not in (None,"ok","failed","rate_limited"):raise ValueError("invalid")
    for key in ("reset_nonce","last_reason"):
        if d.get(key) is not None and not isinstance(d[key],str):raise ValueError("invalid")
    return Health(f,s,instant("backoff_until"),cause,instant("reset_at"),d.get("reset_nonce"),outcome,instant("last_at"),integer("last_count",True),d.get("last_reason"))
def read(conn, name) -> HealthRead:
    """Read only; sqlite errors propagate, malformed JSON returns corrupt=True."""
    row=conn.execute("SELECT value FROM alert_state WHERE key=?",(_name(name),)).fetchone()
    if row is None:return HealthRead(None,None,False)
    raw=row["value"]
    try:return HealthRead(raw,_parse(raw),False)
    except (ValueError,TypeError,KeyError,json.JSONDecodeError,OverflowError):return HealthRead(raw,None,True)
def _raw(h):
    d=asdict(h); d={k:(v.astimezone(timezone.utc).isoformat() if isinstance(v,datetime) else v) for k,v in d.items()}; d["v"]=1
    return json.dumps(d,sort_keys=True,separators=(",",":"))
def next_record(current, outcome, now, *, count=None, reason=None, retry_after=None):
    """Build next record; ValueError is raised for invalid time/outcome."""
    now=_utc(now); current=current or Health()
    if outcome=="ok": f=s=0; until=cause=None
    elif outcome=="failure": f=current.consecutive_failures+1; s=0; until=stage(max(f,s)); cause="failures" if until else None
    elif outcome=="refusal": f=current.consecutive_failures; s=current.retry_after_streak+1; until=stage(max(f,s)); cause=("failures" if f>=s else "refusals") if until else None
    else: raise ValueError("invalid outcome")
    return Health(f,s,now+until if until else None,cause,current.reset_at,current.reset_nonce,{"ok":"ok","failure":"failed","refusal":"rate_limited"}[outcome],now,count,reason)
def effective_wait(health, attempt, now):
    now=_utc(now); cap=now+timedelta(hours=24); candidates=[]
    if health and health.backoff_until and health.backoff_until>now:candidates.append((min(health.backoff_until,cap),health.backoff_cause))
    if attempt:
        at,wait=attempt; until=min(at+wait,cap)
        if until>now:candidates.append((until,"directive"))
    return max(candidates,key=lambda x:x[0]) if candidates else None
def read_effective_wait(conn,name,now):
    """Read wait; sqlite errors propagate and corrupt fetch attempts mean no directive."""
    h=read(conn,name).record
    try:a=fetch_attempts.load(conn,fetch_attempts.news_source_key(name))
    except ValueError:a=None
    return effective_wait(h,a,now)
def write_if_unchanged(conn,name,expected_raw,record,now,*,commit=True):
    key=_name(name); val=_raw(record); now=_utc(now).isoformat()
    if expected_raw is None: cur=conn.execute("INSERT INTO alert_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",(key,val,now))
    else: cur=conn.execute("UPDATE alert_state SET value=?,updated_at=? WHERE key=? AND value=?",(val,now,key,expected_raw))
    if commit:conn.commit()
    return cur.rowcount==1
def initialize_for_enable(conn,name,now,*,nonce=None):
    """Initialize in caller transaction; sqlite errors propagate."""
    now=_utc(now); h=Health(reset_at=now,reset_nonce=nonce or secrets.token_hex())
    conn.execute("INSERT INTO alert_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(_name(name),_raw(h),now.isoformat()))
def delete(conn,name,*,commit=True):
    conn.execute("DELETE FROM alert_state WHERE key=?",(_name(name),))
    if commit:conn.commit()
