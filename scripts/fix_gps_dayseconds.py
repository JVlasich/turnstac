"""One-off fix: turnstac 1.0.0 read GPSTime below one day (seconds of day) as week seconds and
dated those point clouds to the Sunday of the campaign week (2015-03-20, 2017-11-15).
Moves datetime, start_datetime and end_datetime of such items to the campaign date, keeps
the time of day, then recomputes the temporal extent of every collection.
Safe to run twice.

usage: python fix_gps_dayseconds.py <catalog root>
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

_DAY = 86400
_KEYS = ("datetime", "start_datetime", "end_datetime")


def to_dt(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_str(dt): return dt.isoformat().replace("+00:00", "Z")


def gps_max(item): return next((s["maximum"] for s in item["properties"].get("pc:statistics", []) if s["name"] == "GPSTime"), None)


root = Path(sys.argv[1])
raw = {p: p.read_bytes() for p in root.rglob("*.json") if p.name != "last_run.json"}
docs = {p: json.loads(b) for p, b in raw.items()}
changed = set()

for path, item in docs.items():
    gmax = gps_max(item) if item.get("type") == "Feature" else None
    if gmax is None or gmax >= _DAY:
        continue
    camp = date.fromisoformat(re.search(r"\d{4}-\d{2}-\d{2}", path.relative_to(root).parts[0]).group())
    for k in _KEYS:
        if item["properties"].get(k):
            new = to_str(datetime.combine(camp, to_dt(item["properties"][k]).timetz()))
            if new != item["properties"][k]:
                item["properties"][k] = new
                changed.add(path)

# same rule as pystac Extent.from_items: min over datetime + start, max over datetime + end
for path, coll in docs.items():
    if coll.get("type") != "Collection":
        continue
    props = [d["properties"] for p, d in docs.items() if d.get("type") == "Feature" and path.parent in p.parents]
    starts = [to_dt(v) for pr in props for v in (pr.get("datetime"), pr.get("start_datetime")) if v]
    ends = [to_dt(v) for pr in props for v in (pr.get("datetime"), pr.get("end_datetime")) if v]
    interval = [[to_str(min(starts)), to_str(max(ends))]]
    if coll["extent"]["temporal"]["interval"] != interval:
        coll["extent"]["temporal"]["interval"] = interval
        changed.add(path)

for path in sorted(changed):  # keep the line endings the catalog was written with
    path.write_text(json.dumps(docs[path], indent=2), newline="\r\n" if b"\r\n" in raw[path] else "\n")
    print(path)
