"""One-off fix: GDAL 3.1.2 counted transparent (alpha = 0) pixels as 0 in the band statistics.
Corrects mean and stddev of every band next to an alpha band with p = valid_percent / 100.
Run once only, a second run corrects again.

usage: python fix_ortho_stats.py <catalog root>
"""

import json
import math
import sys
from pathlib import Path

for path in Path(sys.argv[1]).rglob("*.json"):
    item = json.loads(path.read_text())
    bands = [b for a in item.get("assets", {}).values() if any(b.get("name") == "alpha" for b in a.get("bands", []))
             for b in a["bands"] if b.get("name") != "alpha"]
    for b in bands:
        st = b["statistics"]
        p, mean, std = st["valid_percent"] / 100, st["mean"], st["stddev"]
        st["mean"] = mean / p
        st["stddev"] = math.sqrt((std * std + mean * mean) / p - st["mean"] ** 2)
    if bands:
        path.write_text(json.dumps(item, indent=2))
        print(path)
