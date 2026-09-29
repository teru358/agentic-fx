#!/usr/bin/env python3
"""docs/tickets/*.md の frontmatter から docs/tickets/INDEX.md を生成する。手で編集しない。"""
from __future__ import annotations
import pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
D = ROOT / "docs" / "tickets"
ORDER = ["実装中", "実装待ち", "裁定待ち", "設計待ち", "是正済", "見送り"]
PRI = {"高": 0, "中": 1, "低": 2, "未設定": 3}

def read_fm(p: pathlib.Path) -> dict:
    text = p.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    title = re.search(r"^## 現象.*?\n\n(.*?)\n", text, re.S | re.M)
    fm["summary"] = (title.group(1) if title else "").replace("|", "／")[:110]
    return fm

def main() -> int:
    rows = []
    for p in sorted(D.glob("*.md")):
        if p.name == "INDEX.md" or p.name.startswith("archive-"):
            continue
        fm = read_fm(p)
        if not fm.get("id"):
            print(f"frontmatter missing id: {p}", file=sys.stderr); return 1
        rows.append((fm, p.name))
    out = ["# チケット一覧 (自動生成: `python3 scripts/tickets_index.py`。手で編集しない)", "",
           f"件数: {len(rows)}。状態は `docs/tickets/<id>.md` の frontmatter が正。出来事の記録は `archive-*.md`。", ""]
    for st in ORDER:
        sub = [r for r in rows if r[0].get("status") == st]
        if not sub:
            continue
        if st == "是正済":
            sub.sort(key=lambda r: r[0].get("closed") or "", reverse=True)
        else:
            sub.sort(key=lambda r: (PRI.get(r[0].get("priority", "未設定"), 9), r[0].get("opened") or ""))
        out += [f"## {st} ({len(sub)})", "", "| id | 優先 | 起票 | 完了 | 概要 |", "|---|---|---|---|---|"]
        for fm, name in sub:
            closed = fm.get("closed", "")
            closed = "" if closed in ("", "null") else closed
            opened = fm.get("opened", "")
            opened = "" if opened in ("", "null") else opened
            out.append(f"| [{fm['id']}]({name}) | {fm.get('priority','未設定')} | {opened} | {closed} | {fm['summary']} |")
        out.append("")
    unknown = [r for r in rows if r[0].get("status") not in ORDER]
    if unknown:
        print("unknown status: " + ", ".join(r[0]["id"] for r in unknown), file=sys.stderr); return 1
    (D / "INDEX.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"INDEX.md: {len(rows)} tickets")
    return 0

if __name__ == "__main__":
    sys.exit(main())
