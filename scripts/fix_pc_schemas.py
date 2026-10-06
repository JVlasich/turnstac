"""One-off fix: turnstac 1.0.0 published the OPALS reader types in pc:schemas, Amplitude and
ScanAngle as floating 4, and the ScanAngle statistics in radians. Sets the LAS storage types, read
from the point format of each item's file (Amplitude uint16, ScanAngle int8 up to point format 5,
int16 from 6), and converts the ScanAngle statistics to degrees. The statistics were rounded to
4 decimals as radians, so they carry an error of up to 0.003 deg.
Items whose ScanAngle schema is no longer floating are skipped, an item whose file cannot be read
is printed and left untouched. Safe to run twice.

usage: python fix_pc_schemas.py <catalog root>
"""

import json
import math
import sys
from pathlib import Path

_STATS = ("minimum", "maximum", "average", "stddev")


def data_path(item_path, item): return item_path.parent / next(a for a in item["assets"].values() if "data" in a.get("roles", []))["href"]  # absolute hrefs stay absolute


def point_format(las):
    with open(las, "rb") as f:
        f.seek(104)  # LAS header: point data record format, bits 6 and 7 flag LAZ compression
        return f.read(1)[0] & 0x3F


for path in sorted(Path(sys.argv[1]).rglob("*.json")):
    raw = path.read_bytes()
    item = json.loads(raw)
    if item.get("type") != "Feature":
        continue
    schemas = {s["name"]: s for s in item["properties"].get("pc:schemas", [])}
    if schemas.get("ScanAngle", {}).get("type") != "floating":
        continue
    las = data_path(path, item)
    try:
        pf = point_format(las)
    except (OSError, IndexError):
        print(f"unreadable, item untouched: {las}")
        continue
    stored = {"Amplitude": (2, "unsigned"), "ScanAngle": (2 if pf >= 6 else 1, "signed")}
    for name, (size, kind) in stored.items():
        if name in schemas:
            schemas[name].update(size=size, type=kind)
    for s in item["properties"].get("pc:statistics", []):
        if s["name"] == "ScanAngle":
            s.update({k: round(math.degrees(s[k]), 4) for k in _STATS if k in s})
    path.write_text(json.dumps(item, indent=2), newline="\r\n" if b"\r\n" in raw else "\n")  # keep the line endings the catalog was written with
    print(path)
