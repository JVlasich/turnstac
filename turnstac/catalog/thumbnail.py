"""Render thumbnails"""

import logging
from dataclasses import dataclass
from pathlib import Path

from osgeo import gdal

gdal.UseExceptions()
log = logging.getLogger(__name__)

MAX_EDGE = 512  # longest thumbnail edge in px
HS_EDGE = 1024  # render hillshade at this res, then downscale to MAX_EDGE


@dataclass(frozen=True)
class ItemThumbJob:
    """One item thumbnail, queued during the campaign loop, rendered after normalize_hrefs."""
    item: object       # pystac.Item
    src_path: Path
    kind: str          # rgb | hillshade | pointcloud


@dataclass(frozen=True)
class CollThumbJob:
    """One aggregate subcollection thumbnail. changed = re-render, else only re-attach."""
    coll: object       # pystac.Collection
    src_paths: list    # list[Path], the flagged tiles
    changed: bool


def _data_window(band, sw: int, sh: int) -> list[int]:
    """[xoff, yoff, xsize, ysize] around the valid-data pixels, so a thumbnail shows the
    item footprint/bbox instead of the full grid (nodata margins). Full grid when the
    band is all-valid or the mask is unusable."""
    import numpy as np
    if band.GetMaskFlags() == gdal.GMF_ALL_VALID:
        return [0, 0, sw, sh]
    scale = max(1.0, max(sw, sh) / MAX_EDGE)
    bw, bh = max(1, round(sw / scale)), max(1, round(sh / scale))
    m = band.GetMaskBand().ReadAsArray(0, 0, sw, sh, buf_xsize=bw, buf_ysize=bh)
    if m is None:
        return [0, 0, sw, sh]
    valid = m > 0
    rows, cols = np.where(valid.any(axis=1))[0], np.where(valid.any(axis=0))[0]
    if not len(rows) or not len(cols):
        return [0, 0, sw, sh]
    fx, fy = sw / bw, sh / bh
    xoff, yoff = int(cols[0] * fx), int(rows[0] * fy)
    xsize = min(sw - xoff, int(round((cols[-1] + 1) * fx)) - xoff)
    ysize = min(sh - yoff, int(round((rows[-1] + 1) * fy)) - yoff)
    return [xoff, yoff, xsize, ysize]


def _thumb_srs(item, file_srs):
    """Source CRS for the warp
    tries the item's proj:wkt2, then proj:code, then the file's own CRS.

    returns:
      str | none ; None makes caller skip the warp"""
    props = getattr(item, "properties", None) or {}
    return props.get("proj:wkt2") or props.get("proj:code") or (
        file_srs.ExportToWkt() if file_srs is not None else None)


def _fit(cw: int, ch: int, edge: int) -> tuple[int, int]:
    """(w, h) scaled so the longest side is <= edge; never upscales."""
    s = min(1.0, edge / max(cw, ch))
    return max(1, round(cw * s)), max(1, round(ch * s))


def _write_png(ds, out: Path) -> None:
    """Writes a dataset to PNG, capping the longest edge at MAX_EDGE"""
    w, h = _fit(ds.RasterXSize, ds.RasterYSize, MAX_EDGE)
    if (w, h) != (ds.RasterXSize, ds.RasterYSize):
        ds = gdal.Translate("", ds, format="MEM", resampleAlg="average", width=w, height=h)
    gdal.Translate(str(out), ds, format="PNG")


def render_thumbnail(item, src_path, kind: str) -> str:
    """Write <item_dir>/<item_id>_thumbnail.png next to the item JSON, return its abs href.

    kind: "rgb" (ortho band downscale) | "hillshade" (DSM/DTM height render)
          | "pointcloud" (COPC/LAZ top-down elevation colormap)

    Raster thumbnails warp to EPSG:4326; native-CRS pixels would sit stretched/rotated
    Source CRS comes from the item's proj metadata (the file may carry none)."""
    src = str(src_path)
    out = Path(item.get_self_href()).parent / f"{item.id}_thumbnail.png"
    out.parent.mkdir(parents=True, exist_ok=True)  # save() has not created the item dir yet

    if kind == "pointcloud":
        return _render_pcl(src, out) 

    ds = gdal.Open(src)
    sw, sh, nbands = ds.RasterXSize, ds.RasterYSize, ds.RasterCount
    has_alpha = nbands >= 4 and ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand
    file_srs = ds.GetSpatialRef()  # usually None
    win = _data_window(ds.GetRasterBand(1), sw, sh)  # crop nodata margin so thumb matches bbox
    ds = None
    cw, ch = win[2], win[3]
    if kind == "hillshade":
        # without computeEdges drops every valid pixel touching a nodata edge
        w, h = _fit(cw, ch, HS_EDGE)
        small = gdal.Translate("", src, format="MEM", width=w, height=h, srcWin=win)
        # zFactor=1 default, can adjust later
        rendered = gdal.DEMProcessing("", small, "hillshade", format="MEM",
                                      computeEdges=True)  # 1-band, nodata=0
    else:
        # RGBA when the source carries an alpha band, keeps nodata edges transparent
        w, h = _fit(cw, ch, MAX_EDGE)
        bands = [1, 2, 3, 4] if has_alpha else ([1, 2, 3] if nbands >= 3 else [1])
        rendered = gdal.Translate("", src, format="MEM", width=w, height=h,
                                  bandList=bands, resampleAlg="average", srcWin=win)

    srs_in = _thumb_srs(item, file_srs)
    if srs_in and item.bbox:
        # warp to plate-carree and pin the extent to item.bbox
        warp = {"srcSRS": srs_in, "dstSRS": "EPSG:4326", "resampleAlg": "bilinear",
                "outputBounds": item.bbox, "outputBoundsSRS": "EPSG:4326"}
        if not has_alpha:
            warp["dstAlpha"] = True
        rendered = gdal.Warp("", rendered, format="MEM", **warp)
    else:
        log.warning(f"no CRS/bbox for thumbnail, map overlay may misalign: {item.id}")

    _write_png(rendered, out)
    return out.resolve().as_posix()


COARSE_N = int(4e5)  # decimation target for plain (non-COPC) laz/las


def _coarse_xyz(src: str, resolution: float | None = None):
    """(x, y, z) arrays, coarsely sampled.
    COPC reads shallow octree levels; plain LAZ/LAS itterates chunks (full read)
    
    returns:
      tuple of ndarrays (x,y,z)
    """
    import laspy
    import numpy as np
    if src.endswith(".copc.laz"):
        with laspy.CopcReader.open(src) as r:
            if resolution is None:
                h = r.header
                span = max(h.maxs[0] - h.mins[0], h.maxs[1] - h.mins[1])
                resolution = span / MAX_EDGE  # ~one octree node per thumbnail pixel
            pts = r.query(resolution=resolution)
        return np.asarray(pts.x), np.asarray(pts.y), np.asarray(pts.z)
    # selection is ignored for LAS < 1.4 / point format < 6.
    sel = laspy.DecompressionSelection.base().decompress_z() # base is xy only so explicit z
    with laspy.open(src, decompression_selection=sel) as f:
        step = max(1, f.header.point_count // COARSE_N)
        xs, ys, zs = [], [], []
        for pts in f.chunk_iterator(3_000_000):
            xs.append(np.asarray(pts.x)[::step])
            ys.append(np.asarray(pts.y)[::step])
            zs.append(np.asarray(pts.z)[::step])
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)


def _bin_and_save(x, y, z, out: Path, extent=None) -> str:
    """Top-down elevation colormap, max-Z per cell, longest edge MAX_EDGE, empty cells transparent.

    extent: (xmin, xmax, ymin, ymax) to bin over. None derives it from the points themselves.
    An aggregate must pass the union extent of all its sources, else the image edges follow
    whatever happened to be sampled instead of the collection footprint."""
    import matplotlib.image as mpimg
    import numpy as np
    from scipy import ndimage
    from scipy.stats import binned_statistic_2d

    if extent is None:
        ex, ey, rng = np.ptp(x), np.ptp(y), None
    else:
        ex, ey = extent[1] - extent[0], extent[3] - extent[2]
        rng = [[extent[0], extent[1]], [extent[2], extent[3]]]
    if ex >= ey:
        w, h = MAX_EDGE, (max(1, round(MAX_EDGE * ey / ex)) if ex else 1)
    else:
        w, h = (max(1, round(MAX_EDGE * ex / ey)) if ey else 1), MAX_EDGE
    grid, *_ = binned_statistic_2d(x, y, z, statistic="max", bins=[w, h], range=rng)  # nan = empty
    # nearest-fill small holes, keep real voids transparent
    nan = np.isnan(grid)
    dist, (ix, iy) = ndimage.distance_transform_edt(nan, return_indices=True)
    small = nan & (dist <= 2)  # fills gaps up to ~4 px wide; bump if speckle persists
    grid[small] = grid[ix[small], iy[small]]
    vmin, vmax = np.nanpercentile(grid, [10, 90])  # guard implausible high/low returns
    # grid is [x, y]; transpose to rows=y, origin lower keeps north up
    mpimg.imsave(str(out), grid.T, cmap="cividis", vmin=vmin, vmax=vmax, origin="lower")
    return out.resolve().as_posix()


def _render_pcl(src: str, out: Path) -> str:
    """One point cloud, binned over its own extent."""
    return _bin_and_save(*_coarse_xyz(src), out)


def render_collection_thumbnail(coll, src_paths) -> str:
    """Write <coll_dir>/<coll_id>_thumbnail.png, return its abs href.

    One image for a whole tiled point-cloud subcollection: every tile queried at the
    collection-wide resolution, binned into one grid over their union extent, so all tiles
    share one color ramp"""
    import laspy
    import numpy as np

    out = Path(coll.get_self_href()).parent / f"{coll.id}_thumbnail.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    srcs, mins, maxs = [], [], []
    for p in src_paths:
        s = str(p)
        try:
            with laspy.open(s) as f:  # header only
                mins.append(f.header.mins)
                maxs.append(f.header.maxs)
                srcs.append((s, f.header.point_count))
        except Exception as e:
            log.warning(f"header unreadable, tile dropped from {coll.id} thumbnail: {Path(s).name} ({e})")
    if not srcs:
        raise ValueError("no readable source")
    lo, hi = np.min(mins, axis=0), np.max(maxs, axis=0)
    extent = (lo[0], hi[0], lo[1], hi[1])
    res = max(extent[1] - extent[0], extent[3] - extent[2]) / MAX_EDGE

    xs, ys, zs = [], [], []
    for s, npts in srcs:
        if not s.endswith(".copc.laz"):
            # no COPC octree to query shallowly, and a chunk seek lands on one flight-line
            # segment rather than a spread sample: the whole file gets read
            log.warning(f"no COPC index, full {npts}-point read for {coll.id} thumbnail: {Path(s).name}")
        try:
            x, y, z = _coarse_xyz(s, resolution=res)
        except Exception as e:
            log.warning(f"read failed, tile dropped from {coll.id} thumbnail: {Path(s).name} ({e})")
            continue
        xs.append(x)
        ys.append(y)
        zs.append(z)
    if not xs:
        raise ValueError("every source failed to read")
    return _bin_and_save(np.concatenate(xs), np.concatenate(ys), np.concatenate(zs),
                         out, extent=extent)


if __name__ == "__main__":
    # self-check: python -m turnstac.catalog.thumbnail <src> [<src> ...] [<dest dir>]
    # several sources render one aggregate collection thumbnail instead
    import sys
    from types import SimpleNamespace
    import time

    args = [Path(a).resolve() for a in sys.argv[1:]]
    dst = args.pop() if len(args) > 1 and args[-1].is_dir() else Path.cwd()
    if len(args) > 1:
        coll = SimpleNamespace(id=dst.name, get_self_href=lambda: str(dst / "collection.json"))
        start = time.time()
        print(render_collection_thumbnail(coll, args), f" ({round(time.time()-start,1)}s)")
        sys.exit()
    src = args[0]
    if src.name.endswith((".las", ".laz")):
        kind = "pointcloud"
    else:
        ds = gdal.Open(str(src))
        kind = "rgb" if ds.RasterCount >= 3 else "hillshade"
        sw, sh = ds.RasterXSize, ds.RasterYSize
        xoff, yoff, xs, ys = _data_window(ds.GetRasterBand(1), sw, sh)
        assert 0 <= xoff and 0 <= yoff and xs > 0 and ys > 0 \
            and xoff + xs <= sw and yoff + ys <= sh, (xoff, yoff, xs, ys, sw, sh)
        ds = None
    item = SimpleNamespace(id=src.stem.removesuffix(".copc"),
                           get_self_href=lambda: str(dst / "item.json"))
    start = time.time()
    print(render_thumbnail(item, src, kind), f" ({round(time.time()-start,1)}s)")
