"""
Convert and tile raster:
GeoTIFF -> COG. Inputs above minTileSize (GB) become COG tiles instead of one COG.
Each input produces its own output.

Usage:
    python tac_raster.py --infile file.tif [--config config.yaml] [--outdir dir]
    python tac_raster.py --init [config.yaml]

Requires: gdal
"""

import argparse
import logging
import sys
import textwrap
from pathlib import Path

from osgeo import gdal

from ..core import config
from ..core.log import setup
from .common import resolve_inputs, beep

gdal.UseExceptions()

log = logging.getLogger(__name__)

DEFAULTS = {
    "infile": None,
    "outdir": None,
    "minTileSize": None,   # GB on disk; None = COG-only for all inputs
    "tileSize": 16384,     # pixels, tiling path only
    "skipIfExists": True,
    "compress": "DEFLATE",
    "blockSize": 512,
    "overviews": "AUTO",
    "numThreads": "ALL_CPUS",
    "bigTiff": "IF_SAFER",
}


def cog_creation_options(cfg: dict) -> list:
    return [
        f"COMPRESS={cfg['compress']}",
        f"BLOCKSIZE={cfg['blockSize']}",
        f"OVERVIEWS={cfg['overviews']}",
        f"NUM_THREADS={cfg['numThreads']}",
        f"BIGTIFF={cfg['bigTiff']}",
    ]


def convert_to_cog(infile: Path, out_path: Path, cfg: dict) -> bool:
    """Whole raster -> one COG. False when skipped."""
    if cfg["skipIfExists"] and out_path.exists():
        log.info(f"COG exists, skipping: {out_path.name}")
        return False
    gdal.Translate(
        str(out_path), str(infile),
        format="COG",
        creationOptions=cog_creation_options(cfg),
    )
    log.info(f"Wrote COG: {out_path.name}")
    return True


def tile_to_cog(infile: Path, tiles_dir: Path, cfg: dict) -> tuple:
    """Window a raster into COG tiles, empty (all-nodata) tiles skipped.
    Returns (written, skipped)."""
    tile_size = cfg["tileSize"]
    creation_options = cog_creation_options(cfg)

    ds = gdal.Open(str(infile))
    width = ds.RasterXSize
    height = ds.RasterYSize
    # mask band of band 1 reflects alpha / nodata / internal mask transparently
    mask_band = ds.GetRasterBand(1).GetMaskBand()

    written, skipped = 0, 0
    for xoff in range(0, width, tile_size):
        for yoff in range(0, height, tile_size):
            xsize = min(tile_size, width - xoff)
            ysize = min(tile_size, height - yoff)

            out_path = tiles_dir / f"{infile.stem}_{xoff}_{yoff}.tif"
            
            # empty tiles are never written, so the mask is re-read every run to recheck them
            # possible fix: write a .empty marker for the skipped-empty tiles
            if cfg["skipIfExists"] and out_path.exists():
                log.debug(f"Tile exists, skipping: {out_path.name}")
                skipped += 1
                continue

            mask = mask_band.ReadAsArray(xoff, yoff, xsize, ysize)
            if mask is None or mask.max() == 0:
                log.debug(f"Empty tile, skipping: {out_path.name}")
                skipped += 1
                continue  # entirely nodata / transparent, dont write

            gdal.Translate(
                str(out_path), ds,
                srcWin=[xoff, yoff, xsize, ysize],
                format="COG",
                creationOptions=creation_options,
            )
            written += 1
            log.info(f"Wrote {out_path.name}")

    ds = None
    return written, skipped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert GeoTIFFs to COG, tiling rasters above a size threshold.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Config precedence: CLI args > YAML config > built-in defaults.
            Use --init to generate a template config file.
        """)
    )

    tac = parser.add_argument_group("Convert and tile options")

    tac.add_argument("--config", type=str, default=None,
                        help="Path to YAML configuration file")
    tac.add_argument("--init", type=str, nargs="?", const="config.yaml", default=None,
                        metavar="FILENAME",
                        help="Generate template config YAML and exit (default: config.yaml)")
    tac.add_argument("--loglevel", type=str, choices=["warning", "info", "debug", "none"],
                        default="info",
                        help="Console log level (default: info)")

    tac.add_argument("--infile", type=str, nargs="+", default=None,
                        help="Input GeoTIFF file(s) and/or directories")
    tac.add_argument("--outdir", type=str, default=None,
                        help="Output directory. COG-only inputs: file placed inside. "
                             "Single tiled input: exact dir. "
                             "Multiple inputs: parent root (<outdir>/<stem>_tiles or <stem>_cog.tif). "
                             "(default: beside each input)")

    tac.add_argument("--minTileSize", type=float, default=None,
                        help=f"Min file size in GB to trigger tiling. None = COG-only for all (default: {DEFAULTS['minTileSize']})")
    tac.add_argument("--tileSize", type=int, default=None,
                        help=f"Tile size in pixels (default: {DEFAULTS['tileSize']})")
    tac.add_argument("--skipIfExists", action=argparse.BooleanOptionalAction, default=None,
                        help=f"Skip outputs that already exist (default: {DEFAULTS['skipIfExists']})")

    tac.add_argument("--compress", type=str, default=None,
                        help=f"COG compression (default: {DEFAULTS['compress']})")
    tac.add_argument("--blockSize", type=int, default=None,
                        help=f"COG block size in pixels (default: {DEFAULTS['blockSize']})")
    tac.add_argument("--overviews", type=str, default=None,
                        help=f"COG overviews mode (default: {DEFAULTS['overviews']})")
    tac.add_argument("--numThreads", type=str, default=None,
                        help=f"GDAL NUM_THREADS (default: {DEFAULTS['numThreads']})")
    tac.add_argument("--bigTiff", type=str, default=None,
                        help=f"BIGTIFF mode (default: {DEFAULTS['bigTiff']})")

    return parser


def process_one(infile: Path | str, cfg: dict, inputs_count: int) -> None:
    """One input: tiles when above minTileSize, else a single COG."""
    infile = Path(infile)
    if not infile.is_file():
        raise FileNotFoundError(f"Input file not found: {infile}")

    st_size = infile.stat().st_size
    min_tile_size = cfg["minTileSize"]
    tiled = min_tile_size is not None and st_size >= min_tile_size * 1e9

    explicit = Path(cfg["outdir"]).resolve() if cfg["outdir"] else None

    if tiled:
        if explicit is None:                            # default: beside input
            tiles_dir = Path(str(infile.with_suffix("")) + "_tiles")
        elif inputs_count == 1:                         # single input: exact dir
            tiles_dir = explicit
        else:                                           # multi: parent root
            tiles_dir = explicit / (infile.stem + "_tiles")
        tiles_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Tiling ({st_size / 1e9:.2f} GB) -> {tiles_dir}")
        written, skipped = tile_to_cog(infile, tiles_dir, cfg)
        log.info(f"{written} tiles written, {skipped} skipped.")
    else:
        base = explicit if explicit else infile.parent
        base.mkdir(parents=True, exist_ok=True)
        out_path = base / (infile.stem + "_cog.tif")
        log.info(f"Converting ({st_size / 1e9:.2f} GB) -> {out_path}")
        convert_to_cog(infile, out_path, cfg)


def main():
    namespace = "tac_raster"
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

    inputs = resolve_inputs(cfg["infile"], (".tif", ".tiff"), "_cog.tif")

    results = []          # (name, "ok" | error message)
    for idx, infile in enumerate(inputs, 1):
        log.info(f"\033[96m=== {infile.name} ({idx}/{len(inputs)}) ===\033[00m")
        try:
            process_one(infile, cfg, len(inputs))
            results.append((infile.name, "ok"))
        except Exception as e:
            log.exception(f"FAILED: {infile.name}", stack_info=True)
            results.append((infile.name, str(e)))

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
