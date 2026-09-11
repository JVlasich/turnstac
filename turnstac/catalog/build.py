"""item + collection builders, id/datetime/geometry, extension wiring, thumbnails"""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

import pystac
from pystac import Collection, Extent, Provider, Summaries
from pystac.extensions.file import FileExtension
from pystac.extensions.pointcloud import PointcloudExtension, Schema, SchemaType, Statistic
from pystac.extensions.projection import ProjectionExtension

from ..core.registry import SIDECAR_EXTENSIONS
from .extract import file_meta, readers

import logging
log = logging.getLogger(__name__)

_GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
_WEEK = 604800  # seconds
_MAX_DEVIATION_DAYS = 14  # derived datetime (gps or filename) further than this from the campaign is rejected


def campaign_date(name: str) -> date:
    """ISO date token (YYYY-MM-DD) out of a campaign folder name (firm data-keeping demand)."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(name))
    if not m:
        raise ValueError(f"no ISO date token in {name!r}")
    return date.fromisoformat(m.group())


def resolve_pc_datetime(gps_min, gps_max, campaign: date) -> tuple[datetime, datetime] | None:
    """Raw GPSTime min/max -> (start, end) UTC.
    Above one week = adjusted standard GPS time (seconds since GPS epoch minus 1e9);
    else weekseconds, resolved against GPS week of the campaign date.
    
    returns:
      datetime tuple | None for absent or degenerate GPSTime -> caller falls back to campaign date.
    """
    # Leap seconds ignored, ~18 s error irrelevant for catalog datetimes.
    if gps_min is None or gps_max is None or gps_min == gps_max or gps_min < 0:
        return None
    if gps_max > _WEEK:  # adjusted standard
        start = _GPS_EPOCH + timedelta(seconds=gps_min + 1e9)
        end = _GPS_EPOCH + timedelta(seconds=gps_max + 1e9)
    else:  # weekseconds
        if gps_max < gps_min:
            log.warning(f"gps weekseconds wrap Sat->Sun ({gps_min} > {gps_max}), extending into next week")
            gps_max += _WEEK
        week_start = (datetime.combine(campaign, datetime.min.time(), tzinfo=timezone.utc)
                      - timedelta(days=(campaign.weekday() + 1) % 7))
        start = week_start + timedelta(seconds=gps_min)
        end = week_start + timedelta(seconds=gps_max)
    # a stray min OR max poisons the extent; warn on drift, reject gross outliers
    for edge, dt in (("start", start), ("end", end)):
        dev = abs((dt.date() - campaign).days)
        if dev > _MAX_DEVIATION_DAYS:
            log.warning(f"gps {edge} {dt.date()} >2wk from campaign date {campaign}, rejected "
                        f"(item falls back to campaign date)")
            return None
        if dev > 7:
            log.warning(f"gps {edge} {dt.date()} deviates >7d from campaign date {campaign}")
    return start, end

# opals column type int -> stac schema type
_STAC_SCHEMA_TYPE = {
    0: SchemaType.SIGNED,   2: SchemaType.SIGNED,   4: SchemaType.SIGNED,   9: SchemaType.SIGNED,   # int32/8/16/64
    1: SchemaType.UNSIGNED, 3: SchemaType.UNSIGNED, 5: SchemaType.UNSIGNED,                          # uint32/8/16
    6: SchemaType.FLOATING, 7: SchemaType.FLOATING,    # float32 / double
    11: SchemaType.UNSIGNED # bool is technically an uint
}

# eo:common_name values GDAL color interps can map to (alpha etc. get name only)
_EO_COMMON = {"red", "green", "blue", "nir"}


# ext  → fn(item, pystac_asset, meta, fm) -> None (no I/O)
_extensions: dict[str, Callable] = {}


def extension(name: str):
    """Register a populator under a registry extension key."""
    def deco(fn):
        _extensions[name] = fn
        return fn
    return deco


@extension("projection")
def _populate_projection(item, pa, meta, fm) -> None:
    proj = ProjectionExtension.ext(item, add_if_missing=True)
    if meta.proj_epsg:
        proj.code = f"EPSG:{meta.proj_epsg}"
    proj.wkt2 = meta.proj_wkt
    if meta.proj_shape:
        proj.shape = meta.proj_shape
    if meta.proj_transform:
        proj.transform = meta.proj_transform
    if meta.proj_bbox:
        proj.bbox = meta.proj_bbox


@extension("pointcloud")
def _populate_pointcloud(item, pa, meta, fm) -> None:
    schemas = []
    for s in meta.pc_schemas:
        t = _STAC_SCHEMA_TYPE.get(s["type"])
        if t is None:
            log.warning(f"unmapped opals column type {s['type']} for {s['name']}, dim dropped from pc:schemas")
            continue
        schemas.append(Schema({"name": s["name"], "size": s["size"], "type": t.value}))
    pc = PointcloudExtension.ext(item, add_if_missing=True)
    pc.apply(
        count=meta.pc_count,
        type=meta.pc_type,
        encoding=_pc_encoding(pa.href),
        schemas=schemas,
        density=meta.pc_density,
        statistics=[Statistic(dict(s)) for s in meta.pc_statistics] or None,
    )


def _pc_encoding(href: str) -> str:
    low = href.lower()
    if low.endswith(".copc.laz"):
        return "copc"
    return low.rsplit(".", 1)[-1]  # laz | las


# statistics.count stays out: extract derives it from the mask band's mean
_STAT_KEYS = ("minimum", "maximum", "mean", "stddev", "valid_percent")

# what a band is, never what it measures: these identify a band and are never attached
_BAND_IDENTITY = {"name", "eo:common_name"}

# pystac 1.14.3 doesnt fully support the new unified bands array
# and the extension versions needed -> hardcode the uri's
_EO_V2 = "https://stac-extensions.github.io/eo/v2.0.0/schema.json"
_RASTER_V2 = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"


@extension("bands")
def _populate_bands(item, pa, meta, fm) -> None:
    """STAC 1.1 unified bands on the asset.
    Values equal across all bands attached to the asset and the bands inherit them;
    everything but identity attaches, an asset with one band is that band."""
    multi = len(meta.raster_bands) > 1
    if meta.raster_bands:
        unknown = set(meta.raster_bands[0]["statistics"]) - set(_STAT_KEYS) - {"count"}
        if unknown:
            log.warning(f"statistics dropped, not in _STAT_KEYS: {sorted(unknown)}")
    bands = []
    for b in meta.raster_bands:
        stats = {k: b["statistics"][k] for k in _STAT_KEYS if b["statistics"].get(k) is not None}
        band = {}
        ci = b["color_interp"]
        # "undefined" is GDAL's word for no colour at all; "gray" on a lone band adds
        # nothing an asset with one band does not already state
        label = None if ci == "undefined" or (not multi and ci == "gray") else ci
        name = b["description"] or label or (f"band{b['index']}" if multi else None)
        if name:
            band["name"] = name
        if ci in _EO_COMMON:
            band["eo:common_name"] = ci
        band.update({
            "data_type": b["data_type"],
            "nodata": b["nodata"],
            "unit": b["unit"],
            "statistics": stats or None,
            "raster:scale": b["scale"] if b["scale"] != 1.0 else None,      # identity is a no-op
            "raster:offset": b["offset"] if b["offset"] != 0.0 else None,
            "raster:bits_per_sample": b["bits_per_sample"],
        })
        bands.append({k: v for k, v in band.items() if v is not None})

    # attached in declared band order, not set order: key order must not vary between runs
    shared = [k for k in (bands[0] if bands else ()) if k not in _BAND_IDENTITY
              and all(k in b and b[k] == bands[0][k] for b in bands[1:])]
    for key in shared:
        pa.extra_fields[key] = bands[0][key]
        for b in bands:
            del b[key]
    # dataset-level in AssetMeta, so never per-band to begin with
    for key, value in (("raster:sampling", meta.raster_sampling),
                       ("raster:spatial_resolution", meta.raster_spatial_resolution)):
        if value is not None:
            pa.extra_fields[key] = value
    if any(bands):  # all empty = single band, attached fully 
        pa.extra_fields["bands"] = bands

    written = set(pa.extra_fields) | {k for b in bands for k in b}
    if "eo:common_name" in written:
        _add_schema(item, _EO_V2)
    if any(k.startswith("raster:") for k in written):
        _add_schema(item, _RASTER_V2)


@extension("histogram")
def _populate_histogram(item, pa, meta, fm) -> None:
    """Height models only, single-banded, so _populate_bands has attached every field
    onto the asset and left no bands array to carry this.
    v2.0.0 allows the field on an asset for exactly that reading."""
    if len(meta.raster_bands) != 1:
        return
    hist = meta.raster_bands[0].get("histogram")
    if hist:
        pa.extra_fields["raster:histogram"] = hist
        _add_schema(item, _RASTER_V2)


@extension("file")
def _populate_file(item, pa, meta, fm) -> None:
    f = FileExtension.ext(pa, add_if_missing=True)
    # multihash: 0x12 = sha2-256, 0x20 = 32 byte digest
    f.apply(checksum="1220" + fm.sha256, size=fm.size)


def _add_schema(item, uri: str) -> None:
    if uri not in item.stac_extensions:
        item.stac_extensions.append(uri)


_SIDECAR_MEDIA = {".prj": "text/plain", ".tfw": "text/plain", ".aux.xml": "application/xml"}


def _round_coords(v):
    """WGS84 coords to 7 decimals (~1 cm), keeps footprint JSON small."""
    if isinstance(v, (int, float)):
        return round(v, 7)
    return [_round_coords(c) for c in v]


def _item_title(product, campaign: date) -> str:
    """Short human title for browse UIs.
    Tile coords when tiled, else asset label + date.
    The registry label separates variants the coarse category collapses
    (dtm_filled vs dtm_masked vs dtm). Sidecar properties (byId title) override this.
    """
    if product.group:  # tiled: id tail carries the tile coords (…_easting_northing)
        tail = product.id.split("_")[-2:]
        if len(tail) == 2 and all(t.isdigit() for t in tail):
            return f"{product.category} tile {tail[0]}_{tail[1]}"
    return f"{product.assets[0].label.replace('_', ' ')} {campaign.isoformat()}"


def merged_properties(properties: dict | None, product) -> dict:
    """Sidecar properties overlay for one product: campaign-wide base, then byLabel
    (registry label), then byId; a null value drops the key it landed on."""
    if not properties:
        return {}
    merged = {k: v for k, v in properties.items() if k not in ("byLabel", "byId")}
    merged.update((properties.get("byLabel") or {}).get(product.assets[0].label) or {})
    merged.update((properties.get("byId") or {}).get(product.id) or {})
    return {k: v for k, v in merged.items() if v is not None}


def build_item(product, campaign: date, *, created: datetime | None = None,
               properties: dict | None = None, crs: str | None = None) -> pystac.Item:
    """discover::Product -> populated pystac.Item.
        1) readers -> AssetMeta
        2) resolve datetime
        3) populators add extensions
        4) apply created (idempotency) + properties (sidecar)
    """
    extracted = []
    for a in product.assets:
        meta = readers[a.kind](a.path, crs=crs)
        fm = a.file_meta or file_meta(a.path)
        extracted.append((a, meta, fm))

    # baseline from the first asset (single-asset products today)
    _, m0, _ = extracted[0]
    span = resolve_pc_datetime(m0.pc_gps_time_min, m0.pc_gps_time_max, campaign)
    if span:
        start = span[0]
    else:
        # no GPS time: filename ISO token is honored, unless it drifts too far from the campaign
        try:
            token = campaign_date(product.assets[0].path.name)
            if abs((token - campaign).days) > _MAX_DEVIATION_DAYS:
                log.warning(f"filename date {token} >2wk from campaign date {campaign}, using campaign "
                            f"date (filename should encode image acquisition date, not processing "
                            f"time): {product.id}")
                token = campaign
            elif token != campaign:
                log.warning(f"filename date {token} deviates from campaign date {campaign}: {product.id}")
        except ValueError:
            token = campaign
        start = datetime.combine(token, datetime.min.time(), tzinfo=timezone.utc)

    geometry, bbox = m0.geometry_wgs84, m0.bbox_wgs84
    if geometry is not None:
        geometry = {**geometry, "coordinates": _round_coords(geometry["coordinates"])}
    if bbox is not None:
        bbox = [round(v, 7) for v in bbox]

    item = pystac.Item(
        id=product.id,
        geometry=geometry,
        bbox=bbox,
        datetime=start,
        properties={},
    )
    if span:
        item.common_metadata.start_datetime = span[0]
        item.common_metadata.end_datetime = span[1]
    item.properties["title"] = _item_title(product, campaign)  # sidecar byId title overrides below

    for a, meta, fm in extracted:
        pa = pystac.Asset(
            href=a.path.resolve().as_posix(),
            media_type=a.media_type,
            roles=list(a.stac_roles),
        )
        item.add_asset(a.label, pa)
        for ext in a.extensions:
            fn = _extensions.get(ext)
            if fn is None:
                log.warning(f"no populator for extension {ext!r} ({a.label})")
                continue
            fn(item, pa, meta, fm)
        for sc in a.sidecars:
            # key = matched sidecar type (prj | tfw | aux.xml), covers foo.tif.aux.xml too
            low = sc.name.lower()
            ext = next(e for e in sorted(SIDECAR_EXTENSIONS, key=len, reverse=True) if low.endswith(e))
            item.add_asset(ext.lstrip("."), pystac.Asset(href=sc.resolve().as_posix(),
                                                         media_type=_SIDECAR_MEDIA.get(ext),
                                                         roles=["metadata"]))

    if m0.raster_spatial_resolution is not None:
        item.common_metadata.gsd = m0.raster_spatial_resolution
    now = datetime.now(timezone.utc)
    item.common_metadata.created = created or now
    item.common_metadata.updated = now
    item.properties.update(merged_properties(properties, product))

    log.debug(f"built item {item.id} ({len(extracted)} asset(s))")
    return item


def refresh_item(prev, product, campaign: date, properties: dict | None = None) -> pystac.Item:
    """Carry a cataloged item over with the sidecar properties overlay reapplied.

    The file is unchanged, so nothing is read and nothing is hashed: only the shaped
    title and the overlay are restated, in build_item's order, and updated is stamped.
    A key deleted from the sidecar is not undone (no marker of origin on the item), so
    that case still needs --force.

    args:
      prev       - the item from the previous run
      product    - discover::Product the item was built from
      campaign   - campaign date, for the generated title
      properties - sidecar properties block
    returns:
      a new pystac.Item, the previous one untouched
    """
    item = prev.clone()
    item.properties["title"] = _item_title(product, campaign)  # a sidecar title overrides below
    item.properties.update(merged_properties(properties, product))
    item.common_metadata.updated = datetime.now(timezone.utc)
    return item


# curated collection summaries: sets for categorical, ranges for numeric.
# created/updated (run noise), wkt2 (bloat) and datetime (extent) excluded.
_SUMMARY_SETS = ("proj:code", "platform", "instruments", "pc:encoding")
_SUMMARY_RANGES = ("gsd", "pc:count", "pc:density")

# a collection summarizing extension fields must declare those extensions; gsd/platform/
# instruments are common metadata (no extension), so only proj/pc appear here
_SUMMARY_EXT_URI = {
    "proj:": ProjectionExtension.get_schema_uri(),
    "pc:": PointcloudExtension.get_schema_uri(),
}


def _declare_summary_extensions(coll: Collection) -> None:
    """Add the extension URL of every prefixed field in the collection's summaries."""
    s = coll.summaries
    keys = set(s.lists) | set(s.ranges) | set(s.other) | set(s.schemas)
    for prefix, uri in _SUMMARY_EXT_URI.items():
        if any(k.startswith(prefix) for k in keys) and uri not in coll.stac_extensions:
            coll.stac_extensions.append(uri)


def _summarize(items) -> Summaries | None:
    out = {}
    for f in _SUMMARY_SETS:
        vals = set()
        for i in items:
            v = i.properties.get(f)
            if isinstance(v, list):
                vals.update(v)
            elif v is not None:
                vals.add(v)
        if vals:
            out[f] = sorted(vals)
    for f in _SUMMARY_RANGES:
        nums = [i.properties[f] for i in items if isinstance(i.properties.get(f), (int, float))]
        if nums:
            out[f] = {"minimum": min(nums), "maximum": max(nums)}
    return Summaries(out) if out else None


# id consumed upstream in manager.process_campaign
_COLLECTION_META_KEYS = {"id", "title", "description", "license", "license_link", "providers", "keywords"}


def build_collection(cid: str, meta: dict, items: list, children: Sequence = ()) -> Collection:
    """Collection factory for campaign collections and tile subcollections.
    Extent + curated summaries from items + children's items. meta keys used: title,
    description, license, providers, keywords. providers takes the STAC list form or
    a name-keyed mapping."""
    all_items = list(items) + [i for c in children for i in c.get_items(recursive=True)]
    if not all_items:
        raise ValueError(f"collection {cid!r} would be empty")

    unknown = set(meta) - _COLLECTION_META_KEYS
    if unknown:
        log.warning(f"collection {cid}: ignored unknown sidecar keys: {sorted(unknown)}")

    providers = meta.get("providers") or []
    if isinstance(providers, dict):  # name-as-key convenience form
        providers = [{"name": name, **(spec or {})} for name, spec in providers.items()]

    coll = Collection(
        id=cid,
        title=meta.get("title"),
        description=meta.get("description") or meta.get("title") or cid,
        extent=Extent.from_items(all_items),
        license=meta.get("license") or "other",
        providers=[Provider.from_dict(p) for p in providers] or None,
        keywords=meta.get("keywords"),
        summaries=_summarize(all_items),
    )
    _declare_summary_extensions(coll)
    lic_link = meta.get("license_link")
    if lic_link:
        coll.add_link(pystac.Link(rel="license", target=lic_link, title=meta.get("license")))
    elif meta.get("license") == "other":
        log.warning(f"collection {cid}: license 'other' without a license_link (spec recommends one)")
    for c in children:
        coll.add_child(c)
    for i in items:
        coll.add_item(i)
    return coll


# --- self-check ---

if __name__ == "__main__":
    import sys

    from ..core.log import setup
    from .discover import discover

    setup()

    # build items from real files (raster default, pass a dir for others)
    args = sys.argv[1:]
    folder = Path(args[0]) if args else Path("data/sample_tif")
    products = discover(folder)
    for p in products:
        try:
            camp = campaign_date(str(p.assets[0].path))
        except ValueError:
            camp = date(2023, 2, 8)  # sample files without a date token
        item = build_item(p, camp)
        # STAC 1.1 band invariants (no jsonschema here, so --validate cannot check them)
        keys = set()
        for label, a in item.assets.items():
            where = f"{item.id}/{label}"
            assert "eo:bands" not in a.extra_fields, f"{where}: pre-1.1 eo:bands"
            assert "raster:bands" not in a.extra_fields, f"{where}: pre-1.1 raster:bands"
            bands = a.extra_fields.get("bands", [])
            assert all(bands), f"{where}: empty band object"
            hist = a.extra_fields.get("raster:histogram")
            assert not hist or not bands, f"{where}: asset histogram beside a bands array"
            assert not hist or len(hist["buckets"]) == hist["count"], f"{where}: bucket count"
            keys |= set(a.extra_fields) | {k for b in bands for k in b}
        # a declared v2.0.0 extension without one of its fields fails require_fields
        assert (_EO_V2 in item.stac_extensions) == ("eo:common_name" in keys), f"{item.id}: eo"
        assert (_RASTER_V2 in item.stac_extensions) == any(
            k.startswith("raster:") for k in keys), f"{item.id}: raster"
        log.info(f"item {item.id}: dt={item.datetime} ext={len(item.stac_extensions)} assets={list(item.assets)}")
