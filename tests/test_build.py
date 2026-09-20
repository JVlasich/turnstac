import logging
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("osgeo.gdal")  # these tests need the geo stack

from turnstac.catalog.build import (_EO_V2, _GPS_EPOCH, _RASTER_V2, build_collection,
                                build_item, campaign_date, resolve_pc_datetime)
from turnstac.catalog.discover import discover

CAMP = date(2023, 2, 8)


def _adjusted_gps(dt: datetime) -> float:
    """Adjusted-standard GPS seconds for a UTC datetime (matches resolve_pc_datetime)."""
    return (dt - _GPS_EPOCH).total_seconds() - 1_000_000_000


def test_campaign_date_iso_token():
    assert campaign_date("pielach_2023-02-08_processed") == CAMP
    with pytest.raises(ValueError):
        campaign_date("no_date_here")


def test_pc_datetime_weekseconds():
    # 2023-02-08 is a Wednesday, GPS week starts Sunday 2023-02-05
    start, end = resolve_pc_datetime(100.0, 200.0, CAMP)
    assert start == datetime(2023, 2, 5, 0, 1, 40, tzinfo=timezone.utc), start
    assert (end - start).total_seconds() == 100


def test_pc_datetime_adjusted_standard_round_trip():
    known = datetime(2023, 2, 8, 12, tzinfo=timezone.utc)
    secs = (known - _GPS_EPOCH).total_seconds() - 1_000_000_000
    start, end = resolve_pc_datetime(secs, secs + 3600, CAMP)
    assert start == known, start
    assert end == known + timedelta(hours=1)


def test_pc_datetime_weekseconds_wrap_sat_to_sun():
    start, end = resolve_pc_datetime(604000.0, 100.0, CAMP)
    assert end > start and (end - start).total_seconds() == 900


def test_pc_datetime_degenerate_is_none():
    assert resolve_pc_datetime(None, None, CAMP) is None
    assert resolve_pc_datetime(5.0, 5.0, CAMP) is None
    assert resolve_pc_datetime(-1.0, 50.0, CAMP) is None


def test_build_item_and_collection(tmp_path, write_tif):
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    write_tif(tmp_path / "pielach_2023-02-08_dsm_etrs89.tif", 20)
    products = discover(tmp_path)
    assert len(products) == 2

    items = [build_item(p, CAMP) for p in products]
    for p, item in zip(products, items):
        assert item.id == p.id
        assert item.bbox and item.geometry
        data_asset = item.assets[p.assets[0].label]
        assert data_asset.extra_fields["file:size"] > 0
        assert data_asset.extra_fields["file:checksum"].startswith("1220")
        props = item.properties
        assert props.get("proj:code") or props.get("proj:wkt2"), "no projection populated"
        # single band: everything hoisted to the asset, no bands array (STAC 1.1)
        assert data_asset.extra_fields["data_type"], "no band data_type"
        assert "bands" not in data_asset.extra_fields
        assert item.datetime == datetime.combine(CAMP, datetime.min.time(), tzinfo=timezone.utc)
        assert props["gsd"] == 25
        assert props["created"] and props["updated"]

    coll = build_collection("pielach_test", {"title": "t", "description": "d"}, items)
    assert coll.extent.spatial.bboxes and coll.extent.temporal.intervals
    assert [i.id for i in coll.get_items()] == [i.id for i in items]
    s = coll.to_dict()["summaries"]
    assert s["proj:code"] == ["EPSG:31256"]
    assert s["gsd"] == {"minimum": 25, "maximum": 25}
    assert "created" not in s and "updated" not in s

    with pytest.raises(ValueError):
        build_collection("empty", {}, [])


def test_build_item_filename_token_rules(tmp_path, write_tif, caplog):
    # token far from the campaign (>2wk): falls back to the campaign date, nudges the namer
    far = tmp_path / "far"
    far.mkdir()
    write_tif(far / "pielach_2014-10-16_dtm_etrs89.tif", 10)
    with caplog.at_level(logging.WARNING):
        item = build_item(discover(far)[0], CAMP)
    assert item.datetime == datetime.combine(CAMP, datetime.min.time(), tzinfo=timezone.utc)
    assert any("acquisition date" in r.getMessage() for r in caplog.records)

    # token within 2wk of the campaign: honored as-is
    near_camp = date(2023, 2, 8)
    near = tmp_path / "near"
    near.mkdir()
    write_tif(near / "pielach_2023-02-10_dtm_etrs89.tif", 10)  # 2 days off
    item = build_item(discover(near)[0], near_camp)
    assert item.datetime == datetime(2023, 2, 10, tzinfo=timezone.utc)

    # no token: campaign date fallback
    plain = tmp_path / "plain"
    plain.mkdir()
    write_tif(plain / "some_dtm.tif", 10)
    item = build_item(discover(plain)[0], CAMP)
    assert item.datetime == datetime.combine(CAMP, datetime.min.time(), tzinfo=timezone.utc)


def test_build_item_coords_rounded(tmp_path, write_tif):
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    item = build_item(discover(tmp_path)[0], CAMP)

    def leaves(v):
        if isinstance(v, (int, float)):
            yield v
        else:
            for c in v:
                yield from leaves(c)

    geom = list(leaves(item.geometry["coordinates"]))
    assert geom and all(v == round(v, 7) for v in geom + list(item.bbox))
    # ...rounded to 7, not coarser: the 7th decimal (~1 cm) still carries signal.
    # geometry and bbox are rounded at separate call sites, so check both
    assert any(v != round(v, 6) for v in geom)
    assert any(v != round(v, 6) for v in item.bbox)


def test_pc_datetime_end_outlier_rejected(caplog):
    # start on-campaign, a stray max GPS time ~5 months later (the 2024->2025 poisoning):
    # >2wk from campaign -> the whole interval is rejected (item falls back to campaign date)
    camp = date(2024, 10, 9)
    good = datetime(2024, 10, 9, 8, tzinfo=timezone.utc)
    stray = datetime(2025, 3, 12, 8, tzinfo=timezone.utc)
    with caplog.at_level(logging.WARNING):
        span = resolve_pc_datetime(_adjusted_gps(good), _adjusted_gps(stray), camp)
    assert span is None, "gross outlier edge rejected, not reported"
    assert any("end" in m and "rejected" in m for m in (r.getMessage() for r in caplog.records))


def test_pc_datetime_within_two_weeks_kept(caplog):
    # legit few-day drift (2015/2017 flew days off the folder date) stays honest, no reject
    camp = date(2024, 10, 9)
    start_dt = datetime(2024, 10, 9, 8, tzinfo=timezone.utc)
    end_dt = datetime(2024, 10, 19, 8, tzinfo=timezone.utc)  # 10 days: >7d warns, but <2wk kept
    with caplog.at_level(logging.WARNING):
        span = resolve_pc_datetime(_adjusted_gps(start_dt), _adjusted_gps(end_dt), camp)
    assert span is not None and span[1].date() == date(2024, 10, 19), "within 2wk kept as-is"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("deviates >7d" in m for m in msgs) and not any("rejected" in m for m in msgs)


def test_build_collection_license_link(tmp_path, write_tif):
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    items = [build_item(p, CAMP) for p in discover(tmp_path)]

    coll = build_collection("c", {"title": "t", "license": "CC-BY-4.0",
                                  "license_link": "https://example.org/lic"}, items)
    lic = [l for l in coll.links if l.rel == "license"]
    assert len(lic) == 1 and lic[0].target == "https://example.org/lic"

    # license "other" without a link: no link emitted (build warns, spec recommends one)
    other = build_collection("c2", {"title": "t", "license": "other"}, items)
    assert not [l for l in other.links if l.rel == "license"]


def test_build_item_provenance(tmp_path, write_tif):
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    p = discover(tmp_path)[0]
    fixed = datetime(2020, 1, 1, tzinfo=timezone.utc)
    item = build_item(p, CAMP, created=fixed, properties={"platform": "riegl-test", "gsd": 99})
    assert item.common_metadata.created == fixed
    assert item.common_metadata.updated > fixed
    assert item.properties["platform"] == "riegl-test"
    assert item.properties["gsd"] == 99, "sidecar properties win over derived"


def test_collection_declares_summary_extensions(tmp_path, write_tif):
    from pystac.extensions.projection import ProjectionExtension
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    items = [build_item(p, CAMP) for p in discover(tmp_path)]
    coll = build_collection("c", {"title": "t"}, items)
    # proj:code appears in the summaries -> the projection extension must be declared
    assert ProjectionExtension.get_schema_uri() in coll.stac_extensions, coll.stac_extensions


def test_item_gets_title(tmp_path, write_tif):
    write_tif(tmp_path / "pielach_2023-02-08_dsm_etrs89.tif", 10)
    item = build_item(discover(tmp_path)[0], CAMP)
    assert item.properties["title"] == "dsm 2023-02-08"
    # sidecar byId title overrides the derived default
    item2 = build_item(discover(tmp_path)[0], CAMP,
                       properties={"byId": {"pielach_2023-02-08_dsm_etrs89": {"title": "Custom"}}})
    assert item2.properties["title"] == "Custom"


def test_item_title_uses_variant_label(tmp_path, write_tif):
    # variant tokens (filled/masked) survive into the title instead of collapsing to the category
    write_tif(tmp_path / "pielach_2023-02-08_dtm_filled.tif", 10)
    item = build_item(discover(tmp_path)[0], CAMP)
    assert item.properties["title"] == "dtm filled 2023-02-08"


def test_pc_datetime_deviation_window_is_two_weeks(caplog):
    # the window is two weeks, not "somewhere far away": day 14 is kept, day 15 rejected.
    # The days are spelled out on purpose - deriving them from _MAX_DEVIATION_DAYS would
    # make the test move with the constant instead of pinning it
    midnight = datetime.combine(CAMP, datetime.min.time(), tzinfo=timezone.utc)
    start = _adjusted_gps(midnight)

    inside = midnight + timedelta(days=14)
    span = resolve_pc_datetime(start, _adjusted_gps(inside), CAMP)
    assert span is not None and span[1] == inside, "day 14 must be kept"

    with caplog.at_level(logging.WARNING):
        assert resolve_pc_datetime(start, _adjusted_gps(midnight + timedelta(days=15)),
                                   CAMP) is None
    assert any("rejected" in r.getMessage() for r in caplog.records)


def test_build_item_filename_token_window_is_two_weeks(tmp_path, write_tif):
    # same window on the filename-token path (a raster carries no GPS time)
    for days, expected in ((14, CAMP + timedelta(days=14)), (15, CAMP)):
        token = CAMP + timedelta(days=days)
        d = tmp_path / token.isoformat()
        d.mkdir()
        write_tif(d / f"pielach_{token.isoformat()}_dtm_etrs89.tif", 10)
        item = build_item(discover(d)[0], CAMP)
        assert item.datetime.date() == expected, f"{days} days off"


def test_build_item_multiband_hoists_only_shared_values(tmp_path, write_rgb_tif):
    """Per-band values must stay per band; only values equal across every band hoist to the
    asset. Identity (name / eo:common_name) never hoists."""
    write_rgb_tif(tmp_path / "pielach_2023-02-08_transparent_mosaic_cog.tif")
    item = build_item(discover(tmp_path)[0], CAMP)
    asset = item.assets["orthophoto"]
    bands = asset.extra_fields["bands"]

    assert len(bands) == 3
    assert [b["name"] for b in bands] == ["red", "green", "blue"]
    assert [b["eo:common_name"] for b in bands] == ["red", "green", "blue"]
    # statistics differ per band (fills are 40 / 80 / 120) -> stay per band, never folded
    assert [b["statistics"]["mean"] for b in bands] == [40.0, 80.0, 120.0]
    assert "statistics" not in asset.extra_fields
    # data_type and nodata are equal across the bands -> hoisted once, dropped from every band
    assert asset.extra_fields["data_type"] == "uint8"
    assert asset.extra_fields["nodata"] == 0.0
    assert all("data_type" not in b and "nodata" not in b for b in bands)
    # the prefixed keys actually written decide the declarations
    assert _EO_V2 in item.stac_extensions and _RASTER_V2 in item.stac_extensions
    assert "eo:bands" not in asset.extra_fields and "raster:bands" not in asset.extra_fields


def test_collection_summary_range_spans_both_gsds(tmp_path, write_tif):
    # two resolutions in one campaign: the range must span them, not collapse to one value
    write_tif(tmp_path / "pielach_2023-02-08_dtm_etrs89.tif", 10, px=25)
    write_tif(tmp_path / "pielach_2023-02-08_dsm_etrs89.tif", 20, px=50)
    items = [build_item(p, CAMP) for p in discover(tmp_path)]
    coll = build_collection("c", {"title": "t"}, items)
    assert coll.to_dict()["summaries"]["gsd"] == {"minimum": 25, "maximum": 50}


def test_build_item_pointcloud(tmp_path, write_las):
    """Full pcl path through opals: pc:* fields, projection off the sidecar CRS, and the
    GPS-derived acquisition window on the item."""
    pytest.importorskip("opals")  # the pcl reader calls opalsInfo
    # weekseconds: 3 days into the GPS week that starts Sun 2023-02-05 -> the campaign day
    write_las(tmp_path / "pielach_2023-02-08_ground.las", gps=(3 * 86400, 3 * 86400 + 3600))
    product = discover(tmp_path)[0]
    item = build_item(product, CAMP, crs="EPSG:31256")

    assert item.datetime == datetime(2023, 2, 8, tzinfo=timezone.utc)
    assert item.common_metadata.start_datetime == datetime(2023, 2, 8, tzinfo=timezone.utc)
    assert item.common_metadata.end_datetime == datetime(2023, 2, 8, 1, tzinfo=timezone.utc)

    props = item.properties
    assert props["pc:count"] == 804 and props["pc:type"] == "lidar"
    assert props["pc:encoding"] == "las"
    assert props["proj:code"] == "EPSG:31256"
    gps_schema = next(s for s in props["pc:schemas"] if s["name"] == "GPSTime")
    assert (gps_schema["type"], gps_schema["size"]) == ("floating", 8)
    # constant dimensions carry no signal and stay out of the statistics
    assert [s["name"] for s in props["pc:statistics"]] == ["GPSTime"]

    asset = item.assets["pointcloud_las"]
    assert asset.media_type == "application/vnd.las"
    assert asset.extra_fields["file:checksum"].startswith("1220")


def test_build_item_pointcloud_copc_encoding(tmp_path, write_las):
    pytest.importorskip("opals")  # the pcl reader calls opalsInfo
    # laspy has no COPC writer, so index a real .laz tile with the shipped tool
    binary = Path(__file__).resolve().parents[1] / "turnstac" / "bin" / "lascopcindex64"
    if not binary.exists():
        pytest.skip("lascopcindex64 not shipped for this platform")
    src = tmp_path / "pielach_2023-02-08_ground.laz"
    write_las(src, gps=(3 * 86400, 3 * 86400 + 3600))
    copc = tmp_path / "copc"
    copc.mkdir()
    subprocess.run([str(binary), "-i", str(src), "-odir", str(copc)], check=True)

    item = build_item(discover(copc)[0], CAMP, crs="EPSG:31256")
    assert item.properties["pc:encoding"] == "copc"
    assert item.assets["pointcloud_copc"].media_type == "application/vnd.laszip+copc"


def test_build_item_histogram_lands_on_the_asset(tmp_path, write_gradient_tif):
    """A height model has one band, so _populate_bands hoists it away entirely and the asset is
    the band. The histogram is a sibling of statistics, never a member of it."""
    write_gradient_tif(tmp_path / "pielach_2023-02-08_dsm_etrs89.tif")
    item = build_item(discover(tmp_path)[0], CAMP)
    asset = item.assets["dsm"]
    hist = asset.extra_fields["raster:histogram"]

    assert hist["count"] == 256 == len(hist["buckets"])
    assert "bands" not in asset.extra_fields
    assert "histogram" not in asset.extra_fields["statistics"]
    assert _RASTER_V2 in item.stac_extensions


def test_build_item_histogram_skips_orthophoto(tmp_path, write_rgb_tif):
    # orthophoto carries no "histogram" key in its registry extensions: a distribution over an
    # alpha or colour band describes the mask or the rendering, not a measurement
    write_rgb_tif(tmp_path / "pielach_2023-02-08_transparent_mosaic_cog.tif")
    item = build_item(discover(tmp_path)[0], CAMP)
    asset = item.assets["orthophoto"]

    assert "raster:histogram" not in asset.extra_fields
    assert all("raster:histogram" not in b for b in asset.extra_fields["bands"])
