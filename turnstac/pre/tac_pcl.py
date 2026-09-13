"""
Tile and convert pointcloud:
Tiles a LAZ file with OPALS, converts the tiles to COPC.

Usage:
    python tile_and_convert.py --infile file.laz [--config config.yaml] [--outdir dir]
    python tile_and_convert.py --init [config.yaml]

Requires: opals
"""

import argparse
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

from ..core.log import setup, opals_log
from .common import resolve_inputs, beep, _BIN, _COPCINDEX

#import opals
from opals import Import, pyDM
from opals.workflows import preTiling, preCutting # concidering _import

log = logging.getLogger(__name__)

DEFAULTS = {
    "infile": None,
    "outdir": None,
    "nbThreads": None,
    "distribute": None,
    "tileSize_odm": 20.0,
    "tmp_path": "./tmp",
    "pointOrigin": None,#"529000;5340000",
    "tileSize": 500,
    "keepodm": True,
    "buffer": 0,
    "keeptmp": False,
    "mergeBelow": 100.0,
}


def import_laz_file(infile: Path, tmp_path: Path, nbThreads: int, tileSize_odm: float):
    odm_path = tmp_path / infile.with_suffix(".odm").name
    if odm_path.exists():
        log.debug(f"ODM exists, skipping import: {odm_path.name}")
        return None

    imp = Import.Import()
    opals_log(imp)
    imp.inFile = str(infile)  # type: ignore
    if nbThreads:
        imp.commons.nbThreads = nbThreads
    imp.outFile = str(odm_path)
    imp.tileSize = tileSize_odm
    imp.run()
    return imp


def pretile(header, tmp_path: Path, nbThreads: int, pointOrigin: str, tileSize: int):
    pret = preTiling.preTiling()
    box = header.getLimit()
    pret.bbox = [box.xmin, box.ymin, box.xmax, box.ymax]  # type: ignore
    pret.skipIfExists = True  # type: ignore
    pret.tileSize = tileSize
    pret.pointOrigin = pointOrigin
    if nbThreads:
        pret.nbThreads = nbThreads  # type: ignore
    opals_log(pret)
    pret.export = str(tmp_path)  # type: ignore
    pret.run() # type: ignore
    return pret


def precut(infile: Path, buffer: int, export_dir: Path, nbThreads: int,
           distribute: int, tmp_path: Path):
    prec = preCutting.preCutting()
    prec.shapefile = str(tmp_path / "Tiles.shp")  # type: ignore
    prec.vector = str(tmp_path / infile.with_suffix(".odm").name)  # type: ignore
    prec.buffer = buffer
    prec.export = str(export_dir / infile.name)  # type: ignore
    prec.skipIfExists = True  # type: ignore
    prec.oformat = "<l v='4' p='6'/>"
    if nbThreads:
        prec.nbThreads = nbThreads  # type: ignore
    if distribute:
        prec.distribute = distribute  # type: ignore
    opals_log(prec)
    prec.run() # type: ignore
    return prec


def _copc_name(laz: Path) -> str:
    return laz.stem + ".copc.laz"


def read_las_bbox(path: Path) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) from the LAS public header (same offsets in LAS 1.2-1.4, LAZ too)."""
    with open(path, "rb") as f:
        f.seek(179)
        maxx, minx, maxy, miny = struct.unpack("<4d", f.read(32))
    return minx, miny, maxx, maxy


def group_tiles(tiles: list, threshold: float) -> list:
    """Merge tiles below threshold (bytes) into edge-adjacent neighbors.

    tiles: (name, size, (col, row)) per tile. Returns groups as name lists, largest
    member first (it names the merged output). Deterministic: smallest runt merges
    first, into its smallest adjacent group; name breaks ties.
    """
    groups = [{"members": [(name, size)], "cells": {cell}, "size": size}
              for name, size, cell in sorted(tiles)]

    def adjacent(a, b):
        return any(abs(ax - bx) + abs(ay - by) == 1
                   for ax, ay in a["cells"] for bx, by in b["cells"])

    while True:
        order = sorted(groups, key=lambda g: (g["size"], g["members"][0][0]))
        runt = next((g for g in order if g["size"] < threshold
                     and any(adjacent(g, o) for o in groups if o is not g)), None)
        if runt is None:
            break
        target = min((g for g in groups if g is not runt and adjacent(runt, g)),
                     key=lambda g: (g["size"], g["members"][0][0]))
        target["members"] += runt["members"]
        target["cells"] |= runt["cells"]
        target["size"] += runt["size"]
        groups.remove(runt)

    return [[n for n, s in sorted(g["members"], key=lambda m: (-m[1], m[0]))]
            for g in sorted(groups, key=lambda g: g["members"][0][0])]


def plan_tile_groups(tile_tmp: Path, point_origin: str, tile_size: float, merge_below: float) -> list:
    """Scan cut tiles, group runts (< mergeBelow MB) with their grid neighbors.
    Returns groups as LAZ path lists, largest first."""
    ox, oy = (float(v) for v in point_origin.split(";"))
    tiles = []
    for laz in sorted(tile_tmp.glob("*.laz")):
        if laz.name.endswith(".copc.laz"):
            continue
        xmin, ymin, xmax, ymax = read_las_bbox(laz)
        cell = (math.floor(((xmin + xmax) / 2 - ox) / tile_size),
                math.floor(((ymin + ymax) / 2 - oy) / tile_size))
        tiles.append((laz.name, laz.stat().st_size, cell))

    threshold = (merge_below or 0) * 1e6
    groups = group_tiles(tiles, threshold)

    sizes = {name: size for name, size, _ in tiles}
    for g in groups:
        if len(g) > 1:
            log.info(f"Merging {len(g)} tiles into {Path(g[0]).stem}: {', '.join(g[1:])}")
        if sum(sizes[n] for n in g) < threshold:
            log.warning(f"Tile below mergeBelow but no adjacent neighbor, kept as-is: {g[0]}")
    return [[tile_tmp / n for n in g] for g in groups]


def warn_stale(groups: list, outdir: Path):
    """Flag leftover COPCs from earlier runs that no current group produces."""
    receiver = {_copc_name(m): _copc_name(g[0]) for g in groups for m in g}
    expected = {_copc_name(g[0]) for g in groups}
    stale = sorted(p.name for p in outdir.glob("*.copc.laz") if p.name not in expected)
    if not stale:
        return
    receivers = sorted({receiver[n] for n in stale if n in receiver})
    msg = f"Stale tiles from a previous run in {outdir}: {', '.join(stale)}."
    if receivers:
        msg += (f" Their points now belong in: {', '.join(receivers)}."
                " Delete the stale files AND those receivers, then re-run.")
    log.warning(msg)


def convert_groups(groups: list, tile_tmp: Path, odir: Path):
    """Tile groups -> COPC in odir. Singletons batch through one -lof call, merged
    groups get one -merged call each. Existing outputs skipped."""
    singles = [g[0] for g in groups
               if len(g) == 1 and not (odir / _copc_name(g[0])).exists()]
    merged = [g for g in groups
              if len(g) > 1 and not (odir / _copc_name(g[0])).exists()]
    if not singles and not merged:
        log.info("All tiles already have COPC. Skipping conversion.")
        return

    if singles:
        convert_to_copc(singles, tile_tmp, odir)

    for g in merged:
        out = odir / _copc_name(g[0])
        log.info(f"Merging {len(g)} tile(s) into {out.name}...")
        lof_path = tile_tmp / "_merge_list.txt"
        lof_path.write_text("\n".join(str(f) for f in g), encoding="utf-8")
        # same convention as convert_to_copc
        subprocess.run([
            str(_BIN / _COPCINDEX),
            "-merged",
            "-lof", str(lof_path),
            "-o", str(out),
            "-progress",
        ],
            check=False
        )
        if not out.exists():
            raise RuntimeError(f"lascopcindex produced no merged output for {out.name}")
        lof_path.unlink(missing_ok=True)


def convert_to_copc(laz_files: list, tile_tmp: Path, odir: Path):
    if not laz_files:
        return

    log.info(f"Converting {len(laz_files)} tile(s) to COPC...")

    lof_path = tile_tmp / "_convert_list.txt"
    lof_path.write_text(
        "\n".join(str(f) for f in laz_files),
        encoding="utf-8"
    )

    # lascopcindex exits nonzero on benign CRS warnings while still writing the COPC,
    # so success is judged by the outputs existing, not the exit code (like c_copc)
    subprocess.run([
        str(_BIN / _COPCINDEX),
        "-lof", str(lof_path),
        "-odir", str(odir),
        "-progress",
    ],
        check=False
    )
    missing = [f.name for f in laz_files if not (odir / (f.stem + ".copc.laz")).exists()]
    if missing:
        raise RuntimeError(f"lascopcindex produced no output for: {', '.join(missing)}")

    lof_path.unlink(missing_ok=True)


def clean_dir(directory: str, keep: list):
    """Delete everything in [directory] except items whose names are in [keep]."""
    keep = set(keep)
    for item in Path(directory).iterdir():
        if item.name in keep:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tile a LAZ file and convert tiles to COPC format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Config precedence: CLI args > YAML config > built-in defaults.
            Use --init to generate a template config file.
        """)
    )

    tac = parser.add_argument_group("Tile and Convert options")

    tac.add_argument("--config", type=str, default=None,
                        help="Path to YAML configuration file")
    tac.add_argument("--init", type=str, nargs="?", const="config.yaml", default=None, # "?" means 0 or 1 args
                        metavar="FILENAME",
                        help="Generate template config YAML and exit (default: config.yaml)")
    tac.add_argument("--loglevel", type=str, choices=["warning", "info", "debug", "none"],
                        default="info",
                        help="Console log level, opals modules derive from it (default: info)")

    tac.add_argument("--infile", type=str, nargs="+", default=None,
                        help="Input LAZ file(s) and/or directories")
    tac.add_argument("--outdir", type=str, default=None,
                        help="Output directory for COPC tiles. Single input: exact dir. "
                             "Multiple inputs: parent root, each input goes to <outdir>/<stem>_tiles. "
                             "(default: <infile_stem>_tiles beside each input)")

    tac.add_argument("--pointOrigin", type=str, default=None,
                        help=f"Tile origin coordinates (default: {DEFAULTS['pointOrigin']})")
    tac.add_argument("--tileSize", type=float, default=None,
                        help=f"Tile size in map units (default: {DEFAULTS['tileSize']})")
    tac.add_argument("--buffer", type=float, default=None,
                        help="Buffer around tiles in map units; keep 0 when mergeBelow is active, "
                             f"merged tiles would duplicate overlap points (default: {DEFAULTS['buffer']})")
    tac.add_argument("--mergeBelow", type=float, default=None,
                        help="Merge tiles smaller than this (MB) into an adjacent tile, "
                             f"0 disables (default: {DEFAULTS['mergeBelow']})")
    tac.add_argument("--tileSize_odm", type=float, default=None,
                        help=f"Tile size for opalsImport (default: {DEFAULTS['tileSize_odm']})")
    tac.add_argument("--keepodm", action=argparse.BooleanOptionalAction, default=None, # note to self: NO store_true
                        help=f"Keep intermediate ODM files (default: {DEFAULTS['keepodm']})")

    tac.add_argument("--nbThreads", type=int, default=None,
                        help=f"Number of threads (default: {DEFAULTS['nbThreads']})")
    tac.add_argument("--distribute", type=int, default=None,
                        help=f"Distribution factor (default: {DEFAULTS['distribute']})")
    tac.add_argument("--tmp_path", type=str, default=None,
                        help=f"Temporary directory path (default: {DEFAULTS['tmp_path']})")
    tac.add_argument("--keeptmp", action=argparse.BooleanOptionalAction, default=None,
                        help=f"Keep temporary files (default: {DEFAULTS['keeptmp']})")

    return parser


def process_one(infile: Path | str, cfg: dict, outdir: Path | str, tmp_root: Path | str) -> Path:
    """Full tile+convert pipeline for one input. Returns the produced ODM path."""
    infile, outdir, tmp_root = Path(infile), Path(outdir), Path(tmp_root)
    work = tmp_root / infile.stem          # per-input work dir isolates odm, grid and tiles
    tile_tmp = work / "tiles"
    outdir.mkdir(parents=True, exist_ok=True)
    tile_tmp.mkdir(parents=True, exist_ok=True)

    if not infile.is_file():
        raise FileNotFoundError(f"Input file not found: {infile}")

    # OPALS Logger always writes XML logs to CWD — redirect to work dir
    original_cwd = Path.cwd()
    os.chdir(str(work))
    try:
        # Import to ODM
        log.info(f"Importing {infile.name} to ODM...")
        import_laz_file(infile, work, cfg["nbThreads"], cfg["tileSize_odm"])
        odm_path = work / infile.with_suffix(".odm").name
        header = pyDM.Datamanager.getHeaderODM(str(odm_path))

        # Create tile grid
        log.info("Creating tile grid...")
        # infer pointorigin from bbox if not provided
        # shift by half LAS resolution so quantized coords never sit exactly on tile edges (dupes)
        box = header.getLimit()
        origin = cfg["pointOrigin"] if cfg["pointOrigin"] else f"{box.xmin - 0.0005};{box.ymin - 0.0005}"
        pretile(header, work, cfg["nbThreads"], origin, cfg["tileSize"])

        # Cut tiles — LAZ goes to tile_tmp, not outdir
        log.info("Cutting tiles...")
        precut(infile, cfg["buffer"], tile_tmp, cfg["nbThreads"], cfg["distribute"], work)
    finally:
        os.chdir(str(original_cwd))

    # Group runt tiles with neighbors, then convert — skip outputs already in outdir
    groups = plan_tile_groups(tile_tmp, origin, cfg["tileSize"], cfg["mergeBelow"])
    warn_stale(groups, outdir)
    convert_groups(groups, tile_tmp, outdir)
    return odm_path


def main():
    namespace = "tile_and_convert_pcl"
    from ..core import config
    config.register_defaults(namespace, DEFAULTS)

    parser = build_parser()
    cli_args = parser.parse_args()
    setup(cli_args.loglevel)

    # --init: generate template and exit
    if cli_args.init is not None:
        config.generate_template_config(namespace, Path(cli_args.init))
        sys.exit(0)

    # Load config file
    if cli_args.config is not None:
        config_path = Path(cli_args.config)
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        config.load_config(config_path)

    # Merge: CLI > config > defaults
    config.merge_cli(namespace, cli_args)
    cfg = config.section(namespace)

    if cfg["infile"] is None:
        raise Exception("--infile is required (via CLI or config file)")

    inputs = resolve_inputs(cfg["infile"], (".laz",), ".copc.laz")
    tmp_path = Path(cfg["tmp_path"]).resolve()
    tmp_path.mkdir(parents=True, exist_ok=True)

    explicit_outdir = Path(cfg["outdir"]).resolve() if cfg["outdir"] else None

    def outdir_for(infile: Path) -> Path:
        if explicit_outdir is None:                 # default: beside each input
            return Path(str(infile.with_suffix("")) + "_tiles")
        if len(inputs) == 1:                        # single input: exact dir (back-compat)
            return explicit_outdir
        return explicit_outdir / (infile.stem + "_tiles")  # multi: parent root

    results = []          # (name, "ok" | error message)
    produced_odms = []

    log.debug(f'nbThreads={cfg["nbThreads"]}\t distribute={cfg["distribute"]}')

    for idx, infile in enumerate(inputs, 1):
        log.info(f"\033[96m=== {infile.name} ({idx}/{len(inputs)}) ===\033[00m")
        try:
            odm_path = process_one(infile, cfg, outdir_for(infile), tmp_path)
            produced_odms.append(odm_path)
            results.append((infile.name, "ok"))
        except Exception as e:
            log.exception(f"FAILED: {infile.name}", stack_info=True)
            results.append((infile.name, str(e)))

    # Cleanup once at batch end
    if cfg.get("keeptmp"):
        log.info("Keeping all temporary files.")
    elif cfg.get("keepodm"):
        log.info("Cleaning temporary files but keeping ODM.")
        for odm in produced_odms:
            clean_dir(str(odm.parent), [odm.name])
    else:
        log.info("Cleaning all temporary files.")
        clean_dir(str(tmp_path), [])

    beep()

    # Summary
    failed = [(n, m) for n, m in results if m != "ok"]
    log.info(f"Done. {len(results) - len(failed)} ok, {len(failed)} failed.")
    for name, msg in failed:
        log.error(f"  {name}: {msg}")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
