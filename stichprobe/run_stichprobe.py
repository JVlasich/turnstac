"""Runs stichprobe.py per campaign on the first tile of each tile subcollection, the DSM and the orthophoto.

usage: python run_stichprobe.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

CATALOG = r"P:\Studies\13_Pielach\12_PROCESSED_DATASETS\catalog"
STICHPROBE = Path(__file__).resolve().parent / "stichprobe.py"


def dirs(path): return sorted(p for p in path.iterdir() if p.is_dir())


def item_json(d): return d / f"{d.name}.json"


def assets(d): return json.loads(item_json(d).read_text())["assets"]


def main():
    os.chdir(CATALOG)
    for campaign in dirs(Path(".")):
        children = dirs(campaign)
        subcollections = [d for d in children if (d / "collection.json").exists()]
        items = [d for d in children if d not in subcollections]
        tiles = [dirs(d)[0] for d in subcollections]
        dsm = [d for d in items if "dsm" in assets(d)]  # dsm_filled and dsm_masked have their own key
        ortho = [d for d in items if "orthophoto" in assets(d)]
        for d in tiles + dsm + ortho:
            print(f"\n=== {d} ===", flush=True)
            subprocess.run([sys.executable, str(STICHPROBE), str(item_json(d))])


if __name__ == "__main__":
    main()
