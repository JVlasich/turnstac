"""Convert LAZ input(s) to COPC without tiling."""
import argparse
import logging
import subprocess
import sys
from pathlib import Path

from ..core.log import setup

from .common import resolve_inputs, _BIN, _COPCINDEX

log = logging.getLogger(__name__)


def to_copc(infile: Path, odir: Path) -> Path:
    """One LAZ -> COPC in odir. lascopcindex returns nonzero on benign CRS warnings, so
    success is judged by the output existing, not by the exit code."""
    out = odir / f"{infile.stem}.copc.laz"
    log.debug(f"indexing {infile.name} -> {out.name}")
    subprocess.run([str(_BIN / _COPCINDEX), "-i", str(infile), "-odir", str(odir), "-progress"],
                   check=False)
    if not out.exists():
        raise RuntimeError("lascopcindex produced no output")
    return out


def main():
    ap = argparse.ArgumentParser(description="Convert LAZ file(s) to COPC, no tiling.")
    ap.add_argument("--infile", nargs="+", required=True, help="LAZ file(s) and/or directories")
    ap.add_argument("--outdir", default=None,
                    help="Output dir (default: beside each input)")
    ap.add_argument("--loglevel", choices=["warning", "info", "debug", "none"], default="info",
                    help="Console log level (default: info)")
    args = ap.parse_args()
    setup(args.loglevel)

    inputs = resolve_inputs(args.infile, (".laz",), ".copc.laz")
    results = []
    for f in inputs:
        odir = Path(args.outdir).resolve() if args.outdir else f.parent
        odir.mkdir(parents=True, exist_ok=True)
        if (odir / f"{f.stem}.copc.laz").exists():   # idempotent: skip already-converted
            log.info(f"skip (exists): {f.stem}.copc.laz")
            results.append((f.name, "ok"))
            continue
        try:
            to_copc(f, odir)
            results.append((f.name, "ok"))
        except Exception as e:
            log.exception(f"FAILED: {f.name}", stack_info=True)
            results.append((f.name, str(e)))

    failed = [(n, m) for n, m in results if m != "ok"]
    log.info(f"Done. {len(results) - len(failed)} ok, {len(failed)} failed.")
    for name, msg in failed:
        log.error(f"  {name}: {msg}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
