"""Example search on the static catalog without a STAC API: pointcloud tiles in a time
window that intersect an AOI and hold at least MIN_POINTS points. Campaigns whose
collection extent misses the query are skipped without opening their items.

usage: python search_static.py <catalog.json>
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import turnstac
import pystac
from shapely.geometry import shape, box

AOI = box(15.37, 48.20, 15.39, 48.21)  # lon/lat
T0 = datetime(2015, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2017, 12, 31, tzinfo=timezone.utc)
MIN_POINTS = 50000


def overlaps(coll):
    s, e = coll.extent.temporal.intervals[0]
    return s <= T1 and e >= T0 and box(*coll.extent.spatial.bboxes[0]).intersects(AOI)


root = pystac.read_file(sys.argv[1])
hits, opened = [], 0
for camp in root.get_children():
    if not overlaps(camp):
        continue
    for item in camp.get_items(recursive=True):
        opened += 1
        p = item.properties
        if ("pc:count" in p and p["pc:count"] >= MIN_POINTS
                and T0 <= (item.common_metadata.start_datetime or item.datetime) <= T1
                and shape(item.geometry).intersects(AOI)):
            hits.append((item.id, p["pc:count"], item.assets["pointcloud_copc"].href))
print(f"opened {opened} items")
for h in sorted(hits):
    print(*h)
