"""Asset discovery and matching.

Walk a campaign folder, classify files against the registry, probe cloud-native status,
emit one Product (= one future Item) per file, resolve cloud-native twins, mark tile
groups, attach sidecars. Unclassifiable files go through the unknown_assets policy.
"""

import functools
import logging
import re
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from ..core.registry import STEM_PATTERNS, LABELS, SIDECAR_EXTENSIONS
from .policy import RunPolicy
log = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _gdal():
    """(gdal, COG validator or None), imported on the first raster probe. Kept out of
    module scope so importing turnstac.catalog does not require GDAL."""
    from osgeo import gdal
    gdal.UseExceptions()
    try:  # optional, (pip install gdal-utils on GDAL < 3.2)
        from osgeo_utils.samples.validate_cloud_optimized_geotiff import validate
    except ImportError:
        validate = None
    return gdal, validate

COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


@dataclass
class Asset:
    path: Path
    label: str
    category: str
    kind: str          # pcl | raster  (dispatches extract.py @reader)
    stac_roles: list
    media_type: str
    extensions: list   # which @populators run
    cloud_native: bool
    thumbnail: str | None = None  # registry renderer kind: rgb | hillshade | pointcloud
    sidecars: list = field(default_factory=list)  # Paths matched by full basename
    file_meta: object = None  # extract.FileMeta, attached by manager's gate when it hashed


@dataclass
class Product:
    id: str            # one future Item
    category: str
    kind: str
    assets: list[Asset]       # always length 1 today; a list for the Item builder
    group: str | None = None  # tile-group name -> subcollection; None -> flat in the campaign
    item: object = None       # pystac.Item, attached by manager (build/reuse); untyped so discover stays pystac-free


# --- matching ---

def _match_ext(low_name: str, exts) -> str | None:
    """Longest of a pattern's extensions that low_name ends with, else None."""
    for e in sorted(exts, key=len, reverse=True):
        if low_name.endswith(e):
            return e
    return None


def _best_match(name: str, stem_patterns):
    """Most specific (pattern, matched_ext) for a filename, else None. Specificity = more
    require tokens, then longer extension (so dtm_masked > dtm, .copc.laz > .laz)."""
    low = name.lower()
    candidates = []
    for label, pat in stem_patterns.items():
        ext = _match_ext(low, pat["extensions"])
        if ext is None:
            continue
        tokens = set(name[: -len(ext)].lower().split("_"))
        if not set(pat["require"]) <= tokens:
            continue
        if set(pat["forbid"]) & tokens:
            continue
        candidates.append((label, pat, ext))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (len(c[1]["require"]), len(c[2])))


def match(filename, stem_patterns=STEM_PATTERNS) -> str | None:
    """Most specific registry label for a filename, else None."""
    bm = _best_match(Path(filename).name, stem_patterns)
    return bm[0] if bm else None


# --- cloud-native probe ---

def _probe_cloud_native(path: Path, kind: str, ext: str, invalid_cog: str = "demote") -> bool:
    """Checks if a file is cloud native

    Pointcloud: ext == .copc.laz.
    Raster: GDAL reports LAYOUT=COG for COG-structured files, filename ignored.
            Also validates COG and routes through policy on error
    """
    if kind == "pcl":
        return ext == ".copc.laz"
    if kind != "raster":
        return False
    gdal, validate_cog = _gdal()
    try:
        ds = gdal.Open(str(path))
    except RuntimeError as e:
        log.warning(f"gdal open failed ({path.name}): {e}")
        return False
    if ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE") != "COG":
        return False
    if validate_cog is not None:
        _, errors, _ = validate_cog(str(path))
        if errors:
            msg = f"invalid COG ({path.name}): {'; '.join(errors)}"
            if invalid_cog == "raise":
                raise ValueError(msg)
            if invalid_cog == "demote":
                log.warning(f"{msg}; demoted to plain GeoTIFF")
                return False
            log.warning(msg)
    return True


# --- ids / twins / tile groups ---

def _item_id(name: str, ext: str) -> str:
    """Deterministic id: filename tokens minus the cog marker, original order/case, so an
    item keeps its id when a plain raster is later converted to COG."""
    tokens = name[: -len(ext)].split("_")
    return "_".join(t for t in tokens if t.lower() != "cog")


def _twin_key(m: "_Match"):
    """Twins are the files that would produce the same item id: only the cog/copc marker differs
    Keyed on the id itself, so two names whose tokens are a permutation of each other stay separate."""
    return (m.path.parent, m.category, _item_id(m.path.name, m.ext).lower())


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def qualify_id(raw_id: str, prefix: str | None) -> str:
    """Campaign-qualified id. An ISO date token already makes a name campaign-unique, so
    only date-less ones take the prefix. Same rule for item and subcollection ids."""
    if prefix and not _ISO_DATE.search(raw_id):
        return f"{prefix}_{raw_id}"
    return raw_id


def _cog_named(m: "_Match") -> bool:
    return "cog" in m.path.name[: -len(m.ext)].lower().split("_")


# format preference among twins; unlisted (raster) extensions rank equal
_EXT_RANK = {".copc.laz": 2, ".laz": 1, ".las": 0}


def _resolve_twins(matches: list["_Match"], policy: str) -> list["_Match"]:
    """One deterministic winner per twin, pcl and raster at once. Precedence:
        1) cloud_native (pcl & raster)
        2) "cog" in name (raster)
        3) format rank .copc.laz > .laz > .las (pcl)

    args:
      matches   - as built in discover()
      policy    - warn | skip | raise ; for non-cn winners
    
    returns:
      list of kept matches
    """
    buckets: dict = {}
    for m in matches:
        buckets.setdefault(_twin_key(m), []).append(m)
    kept = []
    for members in buckets.values():
        non_copc = {m.ext for m in members} - {".copc.laz"}
        if len(non_copc) > 1:
            names = ", ".join(sorted(m.path.name for m in members))
            log.warning(f"extension mix in twins ({names}), keeping preferred format")
        winner = max(members, key=lambda m: (m.cloud_native, _cog_named(m), _EXT_RANK.get(m.ext, 0)))
        for m in members:
            if m is not winner:
                log.debug(f"superseded by twin {winner.path.name}: {m.path.name}")
        if winner.cloud_native:
            kept.append(winner)
            continue
        reason = "named cog, not a COG" if _cog_named(winner) else "non-cloud-native"
        if policy == "raise":
            raise ValueError(f"{reason}: {winner.path.name}")
        if policy == "warn":
            log.warning(f"{reason} ({winner.label}): {winner.path.name}")
            kept.append(winner)
        # skip: drop silently
    return kept


def _assign_tile_groups(products: list, root: Path) -> None:
    """Tiled = more than one product of a category sharing a subdir below the campaign
    root. Subdir names the subcollection; files at the root stay flat"""
    buckets: dict = {}
    for p in products:
        parent = p.assets[0].path.parent
        if parent != root:
            buckets.setdefault((parent, p.category), []).append(p)
    for (parent, _), members in buckets.items():
        if len(members) > 1:
            name = "_".join(parent.relative_to(root).parts)
            for m in members:
                m.group = name


# --- discovery ---

@dataclass
class _Match:
    path: Path
    label: str
    category: str
    ext: str
    info: dict
    cloud_native: bool


def _walk(folder: Path) -> list:
    return sorted(p for p in folder.rglob("*") if p.is_file())


def _sidecar_ext(name: str) -> str | None:
    low = name.lower()
    for e in sorted(SIDECAR_EXTENSIONS, key=len, reverse=True):
        if low.endswith(e):
            return e
    return None


def _handle_unknown(path: Path, reason: str, policy: str) -> None:
    if policy == "raise":
        raise ValueError(f"unknown asset {path.name}: {reason}")
    if policy == "warn":
        log.warning(f"skip ({reason}): {path.name}")
    # skip: silent


def discover(folder: str | Path, policy: RunPolicy = RunPolicy(), *,
             stem_patterns=None, labels=None, id_prefix: str | None = None,
             exclude: list[str] | None = None) -> list:
    """Products under a campaign folder: walk, apply policies, assign .group (pcl tiles).
    Pass merge_overrides() output for per-campaign overrides.

    args:
      stem_patterns - stem-patterns to look for
      folder    - the folder to walk
      policy    - RunPolicy for this run. unknown_assets & non_cloud_native
      labels    - the labels those patterns point to
      id_prefix - prefix for files without ISO token, keeps ids unique
      exclude   - sidecar globs vs each file name (case-insensitive); matches dropped

    returns:
      list of Products
    """
    sp = stem_patterns if stem_patterns is not None else STEM_PATTERNS
    lb = labels if labels is not None else LABELS
    folder = Path(folder)
    files = _walk(folder)
    sidecars = [f for f in files if _sidecar_ext(f.name)]
    # campaign.yaml is the per-campaign sidecar, never an asset
    candidates = [f for f in files if not _sidecar_ext(f.name) and f.name.lower() not in {"campaign.yaml", "campaign.yml"}]

    if exclude:
        kept = []
        for f in candidates:
            if any(fnmatch(f.name.lower(), p.lower()) for p in exclude):
                log.warning(f"excluded by sidecar: {f.name}")
            else:
                kept.append(f)
        candidates = kept

    matches = []
    for f in candidates:
        bm = _best_match(f.name, sp)
        if bm is None:
            _handle_unknown(f, "no registry match", policy.unknown_assets)
            continue
        label, pat, ext = bm
        if label not in lb:
            _handle_unknown(f, f"label {label!r} not in LABELS", policy.unknown_assets)
            continue
        info = lb[label]
        if info["category"] == "ignore":
            log.debug(f"ignored ({label}): {f.name}")
            continue
        cn = _probe_cloud_native(f, info["kind"], ext, policy.invalid_cog)
        matches.append(_Match(f, label, info["category"], ext, info, cn))

    matches = _resolve_twins(matches, policy.non_cloud_native)

    products = []
    seen_ids: dict = {}
    for m in sorted(matches, key=lambda m: m.path.name):
        item_id = qualify_id(_item_id(m.path.name, m.ext), id_prefix)
        # if item_id[:1].isupper():
        #     log.warning(f"id starts with an uppercase letter (kept as-is): {item_id}")
        if item_id in seen_ids:
            raise ValueError(f"id collision: {item_id!r} from {seen_ids[item_id]} and {m.path.name}")
        seen_ids[item_id] = m.path.name

        is_cog = m.info["kind"] == "raster" and m.cloud_native
        asset = Asset(
            path=m.path,
            label=m.label,
            category=m.category,
            kind=m.info["kind"],
            stac_roles=list(m.info["stac_roles"]),
            media_type=COG_MEDIA_TYPE if is_cog else m.info["media_type"],
            extensions=list(m.info["extensions"]),
            cloud_native=m.cloud_native,
            thumbnail=m.info["thumbnail"],
        )
        # same dir + stem form (x.prj) or full-name form (x.tif.aux.xml)
        base = m.path.name[: -len(m.ext)]
        asset.sidecars = [
            sc for sc in sidecars
            if sc.parent == m.path.parent
            and sc.name[: -len(_sidecar_ext(sc.name) or "")] in (base, m.path.name)
        ]
        products.append(Product(id=item_id, category=m.category, kind=m.info["kind"], assets=[asset]))

    _assign_tile_groups(products, folder)
    log.debug(f"{len(files)} files -> {len(products)} products in {folder}")
    
    return products


# --- self-check ---
def _report(products) -> None:
    lines = [f"products ({len(products)}):"]
    for p in products:
        grp = f"  group={p.group}" if p.group else ""
        lines.append(f"  {p.id}  [{p.category}/{p.kind}]{grp}")
        for a in p.assets:
            cn = "" if a.cloud_native else "  (non-cloud-native)"
            sc = f"\n\t\tsidecars={[s.name for s in a.sidecars]}" if a.sidecars else ""
            lines.append(f"      - {a.label}: {a.path.name}{cn}{sc}")
    log.info("\n".join(lines))


if __name__ == "__main__":
    from ..core.log import setup

    setup()

    args = sys.argv[1:]
    if args:
        _report(discover(Path(args[0])))
    else:
        log.info("usage: python -m turnstac.catalog.discover <folder>")
