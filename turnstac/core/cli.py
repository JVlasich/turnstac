"""Catalog update entrypoint"""

import argparse
import logging
import sys
import textwrap
from pathlib import Path

from ..catalog import extract, manager
from ..catalog.policy import RunPolicy
from ..core import config
from ..core.log import setup

log = logging.getLogger(__name__)

NAMESPACE = "catalog"  # defaults registered in manager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build/update the static STAC catalog over a processed-datasets root (idempotent).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Config precedence: CLI args > YAML config > built-in defaults.
            Use --init to generate a template config file.
        """),
    )

    cat = parser.add_argument_group("Catalog options")
    cat.add_argument("root", type=str, nargs="?", default=None,
                     help="Processed-datasets root holding the campaign folders (positional)")

    cat.add_argument("--config", type=str, default=None,
                     help="Path to YAML configuration file")
    cat.add_argument("--init", type=str, nargs="?", const="config.yaml", default=None,
                     metavar="FILENAME",
                     help="Generate template config YAML and exit (default: config.yaml)")
    cat.add_argument("--loglevel", type=str, choices=["warning", "info", "debug", "none"],
                     default="info",
                     help="Console log level, opals modules derive from it (default: info)")
    cat.add_argument("--out", type=str, default=None,
                     help="Catalog output directory (default: <root>/catalog)")
    cat.add_argument("--only", type=str, default=None,
                     help="Process only campaign dirs matching this glob; skips the stale-collection sweep")
    cat.add_argument("--force", action=argparse.BooleanOptionalAction, default=None,
                     help="Skip the idempotency gate, rebuild every item (use after registry/code "
                          "changes, or to repair a dangling href from a hand-deleted thumbnail/sidecar)"
                          "strongly recommended to use in conjuntcure with --only")
    cat.add_argument("--assetHrefs", type=str, choices=["relative", "absolute"], default=None,
                     help="Asset href style: relative (self-contained) or absolute (keep build-time paths); "
                          "thumbnails are always relative (default: absolute)")
    cat.add_argument("--minPoints", type=int, default=None,
                     help="Drop point-cloud items below this pc:count, degenerate tiles (default: 1000)")
    cat.add_argument("--thumbnails", action=argparse.BooleanOptionalAction, default=None,
                     help="Render PNG thumbnails for raster items according to registry (default: on)")

    pol = parser.add_argument_group("Policy options")

    pol.add_argument("--stale", type=str, choices=["warn", "remove", "raise"], default=None,
                     help="Items/collections whose file/dir vanished from disk: keep with warning, remove, or abort (default: warn)")
    pol.add_argument("--unknownAssets", type=str, choices=["warn", "skip", "raise"], default=None,
                     help="Files matching no registry pattern (default: warn)")
    pol.add_argument("--nonCloudNative", type=str, choices=["warn", "skip", "raise"], default=None,
                     help="Files without a cloud-native twin: catalog with warning, drop, or abort (default: warn)")
    pol.add_argument("--invalidCog", type=str, choices=["warn", "demote", "raise"], default=None,
                     help="Rasters failing the COG structure validator: keep cloud-native with a "
                          "warning, demote to plain GeoTIFF, or abort (default: demote)")
    pol.add_argument("--idCollisions", type=str, choices=["warn", "raise"], default=None,
                     help="Duplicate item/subcollection ids across campaigns: warn and keep the first "
                          "owner, or fail the campaign. Collection ids and collisions inside one "
                          "campaign always fail (default: warn)")

    deb = parser.add_argument_group("Debug options")

    deb.add_argument("--dryRun", action=argparse.BooleanOptionalAction, default=None,
                     help="Discover + gate only, report counts, write nothing")
    deb.add_argument("--validate", action=argparse.BooleanOptionalAction, default=None,
                     help="STAC-validate the catalog after saving (needs pystac[validation])")

    inf = parser.add_argument_group("OpalsInfo options")

    inf.add_argument("--nbThreads", type=int, default=None,
                     help="Thread count for opals modules (default: opals default, all CPUs)")
    inf.add_argument("--exactComputation", action=argparse.BooleanOptionalAction, default=None,
                     help="Exact point statistics via full scan; --no-exactComputation reads headers only: "
                          "no pc:statistics, item datetime falls back to campaign date (default: on)")

    return parser


def main():
    parser = build_parser()
    cli_args = parser.parse_args()
    setup(cli_args.loglevel)

    # --init: generate template and exit
    if cli_args.init is not None:
        config.generate_template_config(NAMESPACE, Path(cli_args.init))
        sys.exit(0)

    if cli_args.config is not None:
        config_path = Path(cli_args.config)
        if not config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        config.load_config(config_path)

    # Merge: CLI > config > defaults
    config.merge_cli(NAMESPACE, cli_args)
    cfg = config.section(NAMESPACE)

    extract.OPALS_INFO["nbThreads"] = cfg["nbThreads"]
    extract.OPALS_INFO["exactComputation"] = cfg["exactComputation"]

    if cfg["root"] is None:
        parser.error("root is required (positional arg or config file)")
    root = Path(cfg["root"])
    if not root.is_dir():
        raise NotADirectoryError(f"Processed root not found: {root}")
    out = Path(cfg["out"]) if cfg["out"] else root / "catalog"

    res = manager.update_catalog(root, out, RunPolicy.from_config(cfg))
    ok, failed = res["ok"], res["failed"]

    # Summary
    failed_items = 0
    for name, c in ok.items():
        log.info(f"  {name}: {c['rebuilt']} rebuilt, {c['refreshed']} refreshed, "
                 f"{c['reused']} reused, {c['stale']} stale, {c['failed']} failed")
        failed_items += c["failed"]
    if res["stale_collections"]:
        log.info(f"  stale collections: {', '.join(res['stale_collections'])}")
    log.info(f"Done. {len(ok)} ok, {len(failed)} failed, {failed_items} failed item(s).")
    for name, msg in failed.items():
        log.error(f"  {name}: {msg}")
    if failed or failed_items or res["validation"] not in (None, "ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
