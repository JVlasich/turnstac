"""Runs stichprobe.py on a random sample with a fixed seed. Per campaign: one tile,
the orthophoto if there is one and one other raster product (dsm or dtm).
Same seed and same catalog give the same sample.

usage: python run_stichprobe.py
"""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

CATALOG = r"P:\Studies\13_Pielach\12_PROCESSED_DATASETS\catalog"
STICHPROBE = Path(__file__).resolve().parent / "stichprobe.py"
SEED = 42


def dirs(path): return sorted(p for p in path.iterdir() if p.is_dir())


def item_json(d): return d / f"{d.name}.json"


def data_asset(d):
    """(key, asset) of the item's data asset."""
    assets = json.loads(item_json(d).read_text())["assets"]
    return next((k, a) for k, a in assets.items() if "data" in a.get("roles", []))


def sample(campaign, rng):
    """One random tile, orthophoto and other raster product of a campaign, groups that are empty are skipped."""
    children = dirs(campaign)
    subcollections = [d for d in children if (d / "collection.json").exists()]
    tiles = [t for s in subcollections for t in dirs(s)]
    rasters = [d for d in children if d not in subcollections and "image/tiff" in data_asset(d)[1].get("type", "")]
    ortho = [d for d in rasters if data_asset(d)[0] == "orthophoto"]
    others = [d for d in rasters if d not in ortho]
    return [rng.choice(group) for group in (tiles, ortho, others) if group]


def main():
    os.chdir(CATALOG)
    rng = random.Random(SEED)
    print(f"seed {SEED}")
    for campaign in dirs(Path(".")):
        for d in sample(campaign, rng):
            print(f"\n=== {d} ===", flush=True)
            subprocess.run([sys.executable, str(STICHPROBE), str(item_json(d))])


if __name__ == "__main__":
    main()
