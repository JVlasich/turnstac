"""Independent cross-check of one STAC item against its data asset.
Point clouds are read with PDAL, rasters with tifffile + numpy.

usage: python stichprobe.py <item.json>
"""

import json
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
WEEK = timedelta(weeks=1)

# opals attribute name in the item -> pdal dimension name, factor from pdal to opals units
PDAL_DIMS = {
    "Amplitude":           ("Intensity", 1),
    "EchoNumber":          ("ReturnNumber", 1),
    "NrOfEchos":           ("NumberOfReturns", 1),
    "ClassificationFlags": ("ClassFlags", 1),
    "ScanDirection":       ("ScanDirectionFlag", 1),
    "EdgeOfFlightLine":    ("EdgeOfFlightLine", 1),
    "Classification":      ("Classification", 1),
    "ScanAngle":           ("ScanAngleRank", math.pi / 180),  # pdal degrees, opals radians
    "UserData":            ("UserData", 1),
    "PointSourceId":       ("PointSourceId", 1),
    "GPSTime":             ("GpsTime", 1),
    "ChannelDesc":         ("ScanChannel", 1),
}
# las classification flag bits 0-3, pdal splits them into separate dimensions
FLAGS = ("Synthetic", "KeyPoint", "Withheld", "Overlap")

results = []


def check(name, a, b, ok):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FAIL'}  {name}: {a} | {b}")


def exact(name, a, b): check(name, a, b, a == b)


def close(name, a, b): check(name, a, b, math.isclose(a, b, rel_tol=0.01))


def gps_to_utc(t, adjusted, near):
    """GPS time -> UTC ISO string, leap seconds ignored. Week seconds count from the GPS week of near."""
    week = GPS_EPOCH + (near - GPS_EPOCH) // WEEK * WEEK
    dt = GPS_EPOCH + timedelta(seconds=t + 1e9) if adjusted else week + timedelta(seconds=t)
    return dt.isoformat().replace("+00:00", "Z")


def pointcloud(props, path):
    """Compares CRS, bbox, count, time span, statistics and schemas with PDAL.

    args:
      props - item properties
      path  - point cloud file
    """
    import pdal

    p = pdal.Reader(str(path)).pipeline()
    p.execute()
    pts = p.arrays[0]
    hdr = p.metadata["metadata"]["readers.copc" if props["pc:encoding"] == "copc" else "readers.las"]

    header_bbox = [hdr["minx"], hdr["miny"], hdr["maxx"], hdr["maxy"]]
    point_bbox = [float(f(pts[d])) for f, d in ((np.min, "X"), (np.min, "Y"), (np.max, "X"), (np.max, "Y"))]
    epsg = hdr["srs"]["json"].get("id", {}).get("code")  # None when the WKT carries no authority
    exact("CRS-Code", props.get("proj:code"), f"EPSG:{epsg}" if epsg else None)
    exact("Bbox nativ", props["proj:bbox"], header_bbox)
    exact("Bbox Header vs. Punkte", header_bbox, point_bbox)
    exact("Punktzahl", props["pc:count"], hdr["count"])
    exact("Punktzahl Header vs. Punkte", hdr["count"], len(pts))

    # LAS global encoding bit 0: adjusted standard GPS time, else week seconds
    adjusted = hdr["global_encoding"] & 1
    near = datetime.fromisoformat(props["datetime"].replace("Z", "+00:00"))  # python < 3.11 rejects "Z"
    exact("start_datetime", props.get("start_datetime"), gps_to_utc(pts["GpsTime"].min(), adjusted, near))
    exact("end_datetime", props.get("end_datetime"), gps_to_utc(pts["GpsTime"].max(), adjusted, near))

    for s in props.get("pc:statistics", []):
        dim, factor = PDAL_DIMS[s["name"]]
        v = sum(pts[f] << i for i, f in enumerate(FLAGS)) if dim == "ClassFlags" else pts[dim] * factor
        for key, ref in (("minimum", v.min()), ("maximum", v.max()), ("average", v.mean()), ("stddev", v.std())):
            close(f"{s['name']} {key}", s[key], float(ref))

    schema = {d["name"]: (d["size"], d["type"]) for d in p.schema["schema"]["dimensions"] if d["name"] not in FLAGS}
    schema["ClassFlags"] = (1, "unsigned")  # the four flag bits share one byte in the file
    exact("Dimensionen", len(props["pc:schemas"]), len(schema) - 3)  # X, Y, Z are not in pc:schemas
    for s in props["pc:schemas"]:
        exact(f"Schema {s['name']}", (s["size"], s["type"]), schema.get(PDAL_DIMS[s["name"]][0]))


def raster(props, asset, path):
    """Compares CRS, bbox, band count, data type, statistics and valid pixels with tifffile.
    Statistics are streamed tile by tile, so large orthophotos fit in memory.

    args:
      props - item properties
      asset - item data asset
      path  - GeoTIFF file
    """
    import tifffile

    logging.getLogger("tifffile").setLevel(logging.ERROR)  # it cannot parse GDAL_NODATA = float32 max
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        geo = tif.geotiff_metadata
        h, w, n = page.shape[0], page.shape[1], page.samplesperpixel
        assert page.planarconfig == 1, "only pixel interleaved tiffs"

        exact("CRS-Code", props["proj:code"], f"EPSG:{int(geo['ProjectedCSTypeGeoKey'])}")

        sx, sy = geo["ModelPixelScale"][:2]
        i, j, _, x, y, _ = geo["ModelTiepoint"][:6]
        left, top = x - i * sx, y + j * sy
        if geo["GTRasterTypeGeoKey"] == 2:  # PixelIsPoint: tiepoint sits on the pixel centre
            left, top = left - sx / 2, top + sy / 2
        exact("Bbox nativ", props["proj:bbox"], [left, top - h * sy, left + w * sx, top])

        bands = asset.get("bands") or [{}]  # a single band is hoisted onto the asset
        dtype = {1: "uint", 2: "int", 3: "float"}[page.sampleformat] + str(page.bitspersample)
        exact("Bänderzahl", len(bands), n)
        for b, band in enumerate(bands):
            exact(f"Band {b + 1} Datentyp", band.get("data_type", asset.get("data_type")), dtype)

        nodata = page.tags.get("GDAL_NODATA")
        nodata = float(nodata.value) if nodata else None
        alpha = bool(page.extrasamples) and page.extrasamples[-1] in (1, 2)  # last sample is alpha

        # per band sums over the valid pixels
        count, total, squares = np.zeros(n), np.zeros(n), np.zeros(n)
        lo, hi = np.full(n, np.inf), np.full(n, -np.inf)
        for seg, (_, _, ty, tx, _), _ in page.segments():
            if seg is None:  # sparse tile, all nodata
                continue
            seg = seg[0, :h - ty, :w - tx].reshape(-1, n)  # crop edge padding -> pixels x bands
            for b in range(n):
                v = seg[:, b]
                if alpha and b < n - 1:
                    v = v[seg[:, -1] > 0]
                elif nodata is not None:
                    v = v[v != nodata]
                if v.size:
                    v = v.astype(np.float64)
                    count[b] += v.size
                    total[b] += v.sum()
                    squares[b] += (v * v).sum()
                    lo[b], hi[b] = min(lo[b], v.min()), max(hi[b], v.max())

    for b, band in enumerate(bands):
        st = band.get("statistics") or asset["statistics"]
        mean = total[b] / count[b]
        close(f"Band {b + 1} minimum", st["minimum"], lo[b])
        close(f"Band {b + 1} maximum", st["maximum"], hi[b])
        close(f"Band {b + 1} mean", st["mean"], mean)
        close(f"Band {b + 1} stddev", st["stddev"], math.sqrt(squares[b] / count[b] - mean * mean))
        exact(f"Band {b + 1} valid_percent", st["valid_percent"], round(count[b] / (h * w) * 100, 4))


def main():
    item_path = Path(sys.argv[1])
    item = json.loads(item_path.read_text())
    asset = next(a for a in item["assets"].values() if "data" in a.get("roles", []))
    path = item_path.parent / asset["href"]  # absolute hrefs stay absolute
    print(f"{item['id']} -> {path}")
    if "pc:count" in item["properties"]:
        pointcloud(item["properties"], path)
    else:
        raster(item["properties"], asset, path)
    print(f"{sum(results)}/{len(results)} OK")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
