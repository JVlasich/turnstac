import json
import shutil
from pathlib import Path

import pystac
import pytest

import turnstac

pytest.importorskip("osgeo.gdal")  # these tests need the geo stack

from turnstac.catalog.manager import update_catalog
from turnstac.catalog.policy import RunPolicy


def test_git_commit_is_none_without_git(monkeypatch):
    """A zip download has no .git and maybe no git: the report says None, it does not fail."""
    from turnstac.catalog import manager

    def boom(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(manager.subprocess, "run", boom)
    assert manager._git_commit() is None


def _counts(res: dict, camp: str) -> dict:
    """Campaign counts without the timing block."""
    return {k: v for k, v in res["ok"][camp].items() if k != "seconds"}


def _raw_href(out: Path, item_id: str, role: str) -> str:
    """The on-disk (un-resolved) href of an item's first asset with the given role."""
    item_json = next(out.rglob(f"{item_id}.json"))
    d = json.loads(item_json.read_text(encoding="utf-8"))
    a = next(a for a in d["assets"].values() if role in (a.get("roles") or []))
    return a["href"]


def test_failed_item_isolated(tmp_path, write_tif, write_tif_no_crs):
    out = tmp_path / "catalog"
    camp = tmp_path / "2020-01-01"
    camp.mkdir()
    write_tif(camp / "pielach_2020-01-01_dtm_etrs89.tif", 10)
    write_tif_no_crs(camp / "pielach_2020-01-01_dsm_etrs89.tif")
    (camp / "campaign.yaml").write_text("", encoding="utf-8")

    # no-CRS item fails alone, campaign still builds
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2020-01-01") == {"rebuilt": 1, "refreshed": 0, "reused": 0, "stale": 0, "failed": 1}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert {i.id for i in cat.get_items(recursive=True)} == {"pielach_2020-01-01_dtm_etrs89"}

    # sidecar crs fallback rescues it; the sidecar edit rebuilds the whole campaign
    (camp / "campaign.yaml").write_text('crs: "EPSG:31256"\n', encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2020-01-01") == {"rebuilt": 2, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    item = next(i for i in cat.get_items(recursive=True) if i.id.endswith("dsm_etrs89"))
    assert item.properties["proj:code"] == "EPSG:31256"

    # every item failing: campaign untouched, no collection created
    camp2 = tmp_path / "2021-02-02"
    camp2.mkdir()
    write_tif_no_crs(camp2 / "pielach_2021-02-02_dtm_etrs89.tif")
    (camp2 / "campaign.yaml").write_text("", encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2021-02-02") == {"rebuilt": 0, "refreshed": 0, "reused": 0, "stale": 0, "failed": 1}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert cat.get_child("catalog_2021-02-02") is None


def test_catalog_validates_against_stac_schemas(tmp_path, write_tif, write_rgb_tif):
    """The whole point of the pipeline: what it writes must pass the STAC 1.1 schemas,
    the declared extensions included (multi-band ortho pulls in eo + raster v2.0.0)."""
    pytest.importorskip("jsonschema")
    out = tmp_path / "catalog"
    camp = tmp_path / "2023-02-08"
    camp.mkdir()
    write_tif(camp / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    write_rgb_tif(camp / "pielach_2023-02-08_transparent_mosaic_cog.tif")
    (camp / "campaign.yaml").write_text("", encoding="utf-8")

    res = update_catalog(tmp_path, out, RunPolicy(validate=True))
    verdict = res["validation"]
    if any(m in verdict for m in ("urlopen", "getaddrinfo", "Max retries", "name resolution")):
        pytest.skip(f"STAC schemas unreachable: {verdict}")
    assert verdict == "ok", verdict


def test_subcollection_id_not_doubled_and_asset_href_modes(tmp_path, write_tif):
    out = tmp_path / "catalog"
    camp = tmp_path / "2024-10-09"
    tiles = camp / "pielach_2024-10-09_tiles"
    tiles.mkdir(parents=True)
    write_tif(camp / "pielach_2024-10-09_dsm_etrs89.tif", 10)
    write_tif(tiles / "pielach_2024-10-09_dtm_526000_534000.tif", 20)
    write_tif(tiles / "pielach_2024-10-09_dtm_527000_534000.tif", 30)
    (camp / "campaign.yaml").write_text("", encoding="utf-8")

    # default: subcollection id takes the subdir name as-is, no camp_id doubling
    update_catalog(tmp_path, out, RunPolicy())
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    coll = cat.get_child("catalog_2024-10-09")
    assert coll.get_child("pielach_2024-10-09_tiles") is not None
    assert coll.get_child("catalog_2024-10-09_pielach_2024-10-09_tiles") is None
    # absolute (default) data href keeps the build-time path; thumbnail stays relative
    assert Path(_raw_href(out, "pielach_2024-10-09_dsm_etrs89", "data")).is_absolute()
    assert (_raw_href(out, "pielach_2024-10-09_dsm_etrs89", "thumbnail")
            == "./pielach_2024-10-09_dsm_etrs89_thumbnail.png")

    # relative mode: data href climbs out of catalog/, thumbnail unchanged
    update_catalog(tmp_path, out, RunPolicy(force=True, asset_hrefs="relative"))
    assert _raw_href(out, "pielach_2024-10-09_dsm_etrs89", "data").startswith("..")
    assert (_raw_href(out, "pielach_2024-10-09_dsm_etrs89", "thumbnail")
            == "./pielach_2024-10-09_dsm_etrs89_thumbnail.png")


def test_dateless_subcollection_ids_qualified_per_campaign(tmp_path, write_tif):
    """A subdir without an ISO date token (a hand-made tiles/) is not campaign-unique on
    its own: two campaigns would publish two collections with one id."""
    out = tmp_path / "catalog"
    for date in ("2020-01-01", "2021-02-02"):
        tiles = tmp_path / date / "tiles"
        tiles.mkdir(parents=True)
        write_tif(tiles / f"pielach_{date}_dtm_526000_534000.tif", 10)
        write_tif(tiles / f"pielach_{date}_dtm_527000_534000.tif", 20)
        (tmp_path / date / "campaign.yaml").write_text("", encoding="utf-8")

    res = update_catalog(tmp_path, out, RunPolicy())
    assert not [w for w in res["warnings"] if "id collision" in w], res["warnings"]
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    for date in ("2020-01-01", "2021-02-02"):
        coll = cat.get_child(f"catalog_{date}")
        assert [c.id for c in coll.get_children()] == [f"catalog_{date}_tiles"]


def test_new_product_type_from_sidecar_only(tmp_path, write_tif):
    """A product the default registry does not know reaches the catalog through
    campaign.yaml alone: new pattern + new label, no code change."""
    out = tmp_path / "catalog"
    camp = tmp_path / "2024-10-09"
    camp.mkdir()
    write_tif(camp / "pielach_2024-10-09_bathy_depth.tif", 10)
    (camp / "campaign.yaml").write_text("", encoding="utf-8")

    # default registry: no pattern matches the file, so the campaign stays empty
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-10-09") == {"rebuilt": 0, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res
    assert not (out / "catalog.json").exists() or not list(
        pystac.Catalog.from_file(str(out / "catalog.json")).get_items(recursive=True))

    (camp / "campaign.yaml").write_text(
        "patterns:\n"
        "  waterdepth:\n"
        "    require: [bathy, depth]\n"
        "    extensions: [.tif, .tiff]\n"
        "labels:\n"
        "  waterdepth:\n"
        "    category: waterdepth\n"
        "    kind: raster\n"
        "    stac_roles: [data]\n"
        "    media_type: image/tiff; application=geotiff\n"
        "    extensions: [bands, projection, file]\n"
        "    thumbnail: null\n",
        encoding="utf-8")

    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-10-09") == {"rebuilt": 1, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    item = next(iter(cat.get_items(recursive=True)))
    assert item.id == "pielach_2024-10-09_bathy_depth"
    asset = item.assets["waterdepth"]       # asset key is the registry label
    assert asset.media_type == "image/tiff; application=geotiff"
    assert asset.roles == ["data"] and "proj:code" in item.properties


def test_tiny_pcl_tiles_dropped(tmp_path, monkeypatch):
    # pcl reads need opals/laspy; mock discover/build_item/pcl_point_count to exercise the filter
    from datetime import datetime, timezone

    import turnstac.catalog.manager as mgr
    from turnstac.catalog.discover import Asset, Product

    camp = tmp_path / "2024-10-09"
    (camp / "tiles").mkdir(parents=True)
    (camp / "campaign.yaml").write_text(
        "collection:\n"
        "  providers:\n"
        "    - name: TU Wien\n"
        "      roles: [processor]\n"
        "  keywords: [LiDAR, Pielach]\n", encoding="utf-8")

    def _prod(pid):
        a = Asset(path=camp / "tiles" / f"{pid}.copc.laz", label="pointcloud_copc",
                  category="pointcloud", kind="pcl", stac_roles=["data"],
                  media_type="application/vnd.laszip+copc", extensions=[], cloud_native=True)
        return Product(id=pid, category="pointcloud", kind="pcl", assets=[a], group="tiles")

    monkeypatch.setattr(mgr, "discover",
                        lambda folder, policy, **kw: [_prod("pielach_2024-10-09_big"),
                                                      _prod("pielach_2024-10-09_tiny")])
    # header point count drives the drop, read before build (tile files aren't real here)
    monkeypatch.setattr(mgr, "pcl_point_count",
                        lambda path: 5_000_000 if "big" in str(path) else 3)

    def _fake_item(product, campaign, **kw):
        return pystac.Item(id=product.id, geometry={"type": "Point", "coordinates": [15.4, 48.2]},
                           bbox=[15.4, 48.2, 15.4, 48.2],
                           datetime=datetime(2024, 10, 9, tzinfo=timezone.utc), properties={})

    monkeypatch.setattr(mgr, "build_item", _fake_item)

    root = pystac.Catalog(id="pielach", description="d")
    mgr.process_campaign(camp, root, RunPolicy())
    camp_coll = root.get_child("pielach_2024-10-09")
    ids = {i.id for i in camp_coll.get_items(recursive=True)}
    assert ids == {"pielach_2024-10-09_big"}, ids  # 3-point tile dropped, real tile kept

    # P2-1: the tiles subcollection inherits the campaign's providers/keywords
    # (group "tiles" carries no date, so the id is campaign-qualified)
    sub = camp_coll.get_child("pielach_2024-10-09_tiles")
    assert sub.providers and sub.providers[0].name == "TU Wien"
    assert "LiDAR" in (sub.keywords or [])


def test_root_promoted_to_collection(tmp_path, write_tif, monkeypatch):
    from turnstac.core import config
    orig = config.section
    base = orig("catalog")
    monkeypatch.setattr(config, "section", lambda ns: (
        {**base, "license": "CC-BY-4.0", "providers": [{"name": "TU Wien"}]}
        if ns == "catalog" else orig(ns)))
    out = tmp_path / "catalog"
    camp = tmp_path / "2024-10-09"
    camp.mkdir()
    write_tif(camp / "pielach_2024-10-09_dsm_etrs89.tif", 10)
    (camp / "campaign.yaml").write_text("", encoding="utf-8")

    update_catalog(tmp_path, out, RunPolicy())
    root = pystac.read_file(str(out / "catalog.json"))
    assert isinstance(root, pystac.Collection)
    assert root.license == "CC-BY-4.0" and root.providers
    assert root.extent.spatial.bboxes[0] and root.extent.temporal.intervals[0]


def test_update_catalog_staged_idempotency(tmp_path, write_tif, monkeypatch):
    """One sequential story: build -> reuse -> content change -> stale ->
    dry-run -> force -> subcollection stale -> duplicate id -> vanished campaign."""
    monkeypatch.chdir(tmp_path)   # the run report lands in cwd; keep it out of the repo
    out = tmp_path / "catalog"
    camp_dir = tmp_path / "2023-02-08_test"
    (camp_dir / "pielach_2023-02-08_tiles").mkdir(parents=True)
    write_tif(camp_dir / "pielach_2023-02-08_dtm_etrs89.tif", 10)
    write_tif(camp_dir / "pielach_2023-02-08_dsm_etrs89.tif", 20)
    write_tif(camp_dir / "pielach_2023-02-08_tiles" / "pielach_2023-02-08_dtm_1_1.tif", 30)
    write_tif(camp_dir / "pielach_2023-02-08_tiles" / "pielach_2023-02-08_dtm_1_2.tif", 40)
    write_tif(camp_dir / "pielach_2023-02-08_tiles" / "pielach_2023-02-08_dtm_1_3.tif", 45)
    (camp_dir / "campaign.yaml").write_text(
        "collection:\n"
        "  title: Test campaign\n"
        "  description: fixture campaign\n"
        "  license: CC-BY-4.0\n"
        "properties:\n"
        "  platform: riegl-test\n"
        "hierarchy:\n"
        "  groups:\n"
        "    pielach_2023-02-08_tiles:\n"
        "      title: DTM tiles\n",
        encoding="utf-8",
    )
    broken = tmp_path / "2023-05-05_broken"
    broken.mkdir()                                    # campaign with a malformed sidecar
    (broken / "campaign.yaml").write_text("- not a mapping\n", encoding="utf-8")
    (tmp_path / "notes").mkdir()                      # not a campaign, ignored

    # run 1: full build, broken campaign isolated
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 5, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res
    assert "2023-05-05_broken" in res["failed"]

    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    camp = cat.get_child("catalog_2023-02-08")
    assert camp is not None and camp.title == "Test campaign"
    sub = camp.get_child("pielach_2023-02-08_tiles")
    assert sub is not None and sub.title == "DTM tiles"
    assert len(list(sub.get_items())) == 3
    assert len(list(camp.get_items(recursive=True))) == 5

    # sidecar properties passthrough + provenance timestamps + curated summaries
    item = next(i for i in camp.get_items() if i.id == "pielach_2023-02-08_dtm_etrs89")
    assert item.properties["platform"] == "riegl-test"
    created0, updated0 = item.properties["created"], item.properties["updated"]
    s = camp.to_dict()["summaries"]
    assert s["proj:code"] == ["EPSG:31256"] and s["platform"] == ["riegl-test"]
    assert s["gsd"] == {"minimum": 25, "maximum": 25}

    # run report persisted
    report = json.loads((out / "last_run.json").read_text(encoding="utf-8"))
    assert report["ok"]["2023-02-08_test"]["rebuilt"] == 5 and report["failed"]
    assert report["version"] == turnstac.__version__ and "commit" in report

    # run 2: no-op, everything reused, timestamps untouched
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 0, "refreshed": 0, "reused": 5, "stale": 0, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    item = next(i for i in cat.get_items(recursive=True) if i.id == "pielach_2023-02-08_dtm_etrs89")
    assert (item.properties["created"], item.properties["updated"]) == (created0, updated0)

    # only-filter: broken campaign skipped, stale-collection sweep off
    res = update_catalog(tmp_path, out, RunPolicy(only="2023-02-08*"))
    assert res["ok"]["2023-02-08_test"]["reused"] == 5
    assert not res["failed"] and res["stale_collections"] == []

    # content change at constant size -> hash path rebuilds exactly that item
    write_tif(camp_dir / "pielach_2023-02-08_dtm_etrs89.tif", 99)
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 1, "refreshed": 0, "reused": 4, "stale": 0, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    item = next(i for i in cat.get_items(recursive=True) if i.id == "pielach_2023-02-08_dtm_etrs89")
    assert item.properties["created"] == created0, "created survives rebuilds"
    assert item.properties["updated"] > updated0, "updated bumps on rebuild"

    # deleted file: default warn keeps the item, remove drops it
    (camp_dir / "pielach_2023-02-08_dsm_etrs89.tif").unlink()
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 0, "refreshed": 0, "reused": 4, "stale": 1, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert len(list(cat.get_items(recursive=True))) == 5

    res = update_catalog(tmp_path, out, RunPolicy(stale="remove"))
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert len(list(cat.get_items(recursive=True))) == 4

    # dry run: reports, writes nothing but the run report
    write_tif(camp_dir / "pielach_2023-02-08_dtm_etrs89.tif", 50)
    before = (out / "catalog.json").stat().st_mtime
    res = update_catalog(tmp_path, out, RunPolicy(dry_run=True))
    assert res["ok"]["2023-02-08_test"]["rebuilt"] == 1
    assert (out / "catalog.json").stat().st_mtime == before
    report = json.loads((out / "last_run.json").read_text(encoding="utf-8"))
    assert report["dry_run"] is True

    # force skips the gate, everything rebuilds
    res = update_catalog(tmp_path, out, RunPolicy(force=True))
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 4, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res

    # kept-stale tile stays inside its subcollection (no flat drift)
    (camp_dir / "pielach_2023-02-08_tiles" / "pielach_2023-02-08_dtm_1_3.tif").unlink()
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2023-02-08_test") == {"rebuilt": 0, "refreshed": 0, "reused": 3, "stale": 1, "failed": 0}, res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    camp = cat.get_child("catalog_2023-02-08")
    assert {i.id for i in camp.get_items()} == {"pielach_2023-02-08_dtm_etrs89"}
    sub = camp.get_child("pielach_2023-02-08_tiles")
    assert len(list(sub.get_items())) == 3  # 2 live + 1 stale clone kept in place

    # duplicate campaign id fails isolated, first campaign untouched
    dup = tmp_path / "2023-06-06_dup"
    dup.mkdir()
    (dup / "campaign.yaml").write_text("collection:\n  id: catalog_2023-02-08\n",
                                       encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert "already used" in res["failed"]["2023-06-06_dup"], res
    assert "2023-02-08_test" in res["ok"]
    shutil.rmtree(dup)

    # vanished campaign dir: removal blocked while another campaign fails...
    shutil.rmtree(camp_dir)
    res = update_catalog(tmp_path, out, RunPolicy(stale="remove"))
    assert res["stale_collections"] == ["catalog_2023-02-08"] and res["failed"], res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert cat.get_child("catalog_2023-02-08") is not None

    # ...then removed once the run is clean
    shutil.rmtree(tmp_path / "2023-05-05_broken")
    res = update_catalog(tmp_path, out, RunPolicy(stale="remove"))
    assert not res["failed"] and res["stale_collections"] == ["catalog_2023-02-08"], res
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    assert cat.get_child("catalog_2023-02-08") is None


def test_optional_sidecar(tmp_path, write_tif, monkeypatch):
    """A campaign without campaign.yaml warns and builds from defaults. The digest of a
    missing sidecar equals the digest of an empty one, so adding the file changes nothing
    but the warning. The lookup is case-insensitive."""
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "catalog"
    camp = tmp_path / "2024-04-04"
    camp.mkdir()
    write_tif(camp / "pielach_2024-04-04_dtm_etrs89.tif", 10)

    # no sidecar: clean run, collection from defaults
    res = update_catalog(tmp_path, out, RunPolicy())
    assert not res["failed"], res
    assert _counts(res, "2024-04-04") == {"rebuilt": 1, "refreshed": 0, "reused": 0, "stale": 0, "failed": 0}, res
    assert any("no campaign.yaml" in w for w in res["warnings"]), res["warnings"]
    cat = pystac.Catalog.from_file(str(out / "catalog.json"))
    coll = cat.get_child("catalog_2024-04-04")
    assert coll is not None and coll.title is None and coll.license == "other"

    # second run reuses, a missing sidecar is not a rebuild trigger
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-04-04") == {"rebuilt": 0, "refreshed": 0, "reused": 1, "stale": 0, "failed": 0}, res

    # an empty sidecar carries the same digest: warning gone, still reused
    (camp / "campaign.yaml").write_text("", encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-04-04") == {"rebuilt": 0, "refreshed": 0, "reused": 1, "stale": 0, "failed": 0}, res
    assert not any("no campaign.yaml" in w for w in res["warnings"]), res["warnings"]

    # case-insensitive lookup finds it too
    (camp / "campaign.yaml").rename(camp / "Campaign.YAML")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-04-04") == {"rebuilt": 0, "refreshed": 0, "reused": 1, "stale": 0, "failed": 0}, res
    assert not any("no campaign.yaml" in w for w in res["warnings"]), res["warnings"]

    # both spellings present: warned, .yaml wins (the .yml crs would have forced a rebuild)
    (camp / "campaign.yml").write_text('crs: "EPSG:4326"\n', encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert _counts(res, "2024-04-04") == {"rebuilt": 0, "refreshed": 0, "reused": 1, "stale": 0, "failed": 0}, res
    assert any("both present" in w for w in res["warnings"]), res["warnings"]

    # a dangling sidecar symlink is a missing sidecar, not a failed campaign
    (camp / "campaign.yml").unlink()
    (camp / "Campaign.YAML").unlink()
    (camp / "campaign.yaml").symlink_to(camp / "gone.yaml")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert not res["failed"], res
    assert any("no campaign.yaml" in w for w in res["warnings"]), res["warnings"]


def test_sidecar_refresh(tmp_path, write_tif, monkeypatch):
    """A properties edit is reconciled against the item it produced: patched in place,
    no reader call and no rehash. labels and crs stay on the campaign-wide digest, and
    a registry LABELS change is caught by the asset shape check."""
    from turnstac.catalog import build
    from turnstac.core import registry

    out = tmp_path / "catalog"
    camp = tmp_path / "2025-03-03"
    camp.mkdir()
    write_tif(camp / "pielach_2025-03-03_dtm_etrs89.tif", 10)
    write_tif(camp / "pielach_2025-03-03_dsm_etrs89.tif", 20)
    sidecar = camp / "campaign.yaml"
    dtm_id, dsm_id = "pielach_2025-03-03_dtm_etrs89", "pielach_2025-03-03_dsm_etrs89"

    def _item(iid):
        cat = pystac.Catalog.from_file(str(out / "catalog.json"))
        return next(i for i in cat.get_items(recursive=True) if i.id == iid)

    def _run():
        return _counts(update_catalog(tmp_path, out, RunPolicy()), "2025-03-03")

    def _c(rebuilt=0, refreshed=0, reused=0):
        return {"rebuilt": rebuilt, "refreshed": refreshed, "reused": reused,
                "stale": 0, "failed": 0}

    # a reader call is what a refresh must never cost
    read = []
    real = build.readers["raster"]
    monkeypatch.setitem(build.readers, "raster",
                        lambda path, **kw: (read.append(path), real(path, **kw))[1])

    sidecar.write_text("properties:\n  platform: airborne\n", encoding="utf-8")
    assert _run() == _c(rebuilt=2)

    # byId edit on one item: only that item is touched, and no reader runs
    read.clear()
    sidecar.write_text(
        "properties:\n"
        "  platform: airborne\n"
        "  byId:\n"
        f"    {dtm_id}:\n"
        "      title: DTM, retitled\n", encoding="utf-8")
    assert _run() == _c(refreshed=1, reused=1)
    assert not read, "refresh read the data file"
    assert _item(dtm_id).properties["title"] == "DTM, retitled"

    # convergence: the refreshed item satisfies the gate next run
    assert _run() == _c(reused=2)

    # a byId entry for an item that had none
    sidecar.write_text(
        "properties:\n"
        "  platform: airborne\n"
        "  byId:\n"
        f"    {dtm_id}:\n"
        "      title: DTM, retitled\n"
        f"    {dsm_id}:\n"
        "      mission: second pass\n", encoding="utf-8")
    assert _run() == _c(refreshed=1, reused=1)
    assert _item(dsm_id).properties["mission"] == "second pass"

    # campaign-wide edit reaches every item, still without a reader
    read.clear()
    sidecar.write_text(
        "properties:\n"
        "  platform: helicopter\n"
        "  byId:\n"
        f"    {dtm_id}:\n"
        "      title: DTM, retitled\n"
        f"    {dsm_id}:\n"
        "      mission: second pass\n", encoding="utf-8")
    assert _run() == _c(refreshed=2)
    assert not read, "refresh read the data file"
    assert _item(dtm_id).properties["platform"] == "helicopter"
    assert _item(dsm_id).properties["platform"] == "helicopter"

    # dropping a title override falls back to the generated title (the one removal a
    # subset check can notice, because the builder always writes a title)
    sidecar.write_text(
        "properties:\n"
        "  platform: helicopter\n"
        "  byId:\n"
        f"    {dsm_id}:\n"
        "      mission: second pass\n", encoding="utf-8")
    assert _run() == _c(refreshed=1, reused=1)
    assert _item(dtm_id).properties["title"] == "dtm 2025-03-03"

    # labels and crs stay on the digest: either rebuilds the whole campaign
    sidecar.write_text(
        "properties:\n"
        "  platform: helicopter\n"
        "labels:\n"
        "  shade:\n"
        "    category: ignore\n", encoding="utf-8")
    assert _run() == _c(rebuilt=2)
    sidecar.write_text(
        "properties:\n"
        "  platform: helicopter\n"
        "labels:\n"
        "  shade:\n"
        "    category: ignore\n"
        "crs: EPSG:31256\n", encoding="utf-8")
    assert _run() == _c(rebuilt=2)
    assert _run() == _c(reused=2)

    # a LABELS default edit never reaches the digest; the asset shape check catches it
    monkeypatch.setitem(registry.LABELS["dtm"], "stac_roles", ["data", "reflectance"])
    assert _run() == _c(rebuilt=1, reused=1)
    assert _item(dtm_id).assets["dtm"].roles == ["data", "reflectance"]


def test_failed_campaign_queues_no_thumbnails(tmp_path, write_tif):
    """Thumbnail jobs are per-campaign locals: a campaign that raises after building items
    drops them. Those items never reach the tree, so rendering would only warn on a
    missing href and put that noise in the run report."""
    out = tmp_path / "catalog"
    camp = tmp_path / "2024-10-09"
    camp.mkdir()
    write_tif(camp / "pielach_2024-10-09_dtm_etrs89.tif", 10)
    write_tif(camp / "pielach_2024-10-09_dsm_etrs89.tif", 20)
    (camp / "campaign.yaml").write_text("", encoding="utf-8")
    update_catalog(tmp_path, out, RunPolicy())

    # one item rebuilds and queues its thumbnail, then the stale sweep raises on the other
    write_tif(camp / "pielach_2024-10-09_dtm_etrs89.tif", 99)
    (camp / "pielach_2024-10-09_dsm_etrs89.tif").unlink()
    res = update_catalog(tmp_path, out, RunPolicy(stale="raise"))
    assert "stale item" in res["failed"]["2024-10-09"], res
    assert not [w for w in res["warnings"] if "thumbnail failed" in w], res["warnings"]


def _coll_asset_href(out: Path, coll_id: str, key: str) -> str:
    """The on-disk href of a collection-level asset."""
    cj = next(p for p in out.rglob("collection.json") if p.parent.name == coll_id)
    return json.loads(cj.read_text(encoding="utf-8"))["assets"][key]["href"]


def test_tiled_pcl_subcollection_thumbnail(tmp_path, write_las, monkeypatch):
    """One aggregate thumbnail per tiled point-cloud subcollection: rendered on the first run,
    re-rendered only when a member changes, re-attached on every run."""
    from datetime import datetime, timezone

    import turnstac.catalog.manager as mgr
    from turnstac.catalog.discover import Asset, Product
    from turnstac.catalog.extract import file_meta

    camp = tmp_path / "2024-10-09"
    tiles = camp / "pielach_2024-10-09_tiles"
    tiles.mkdir(parents=True)
    (camp / "campaign.yaml").write_text("", encoding="utf-8")
    for i, (dx, dy) in enumerate([(0, 0), (100, 0), (100, 50)]):   # L-shaped, one empty cell
        write_las(tiles / f"pielach_2024-10-09_pcl_{i}.las", n=20_000, dx=dx, dy=dy)

    # pcl item builds need opals; mock discover/build_item/pcl_point_count, the thumbnail
    # path itself is real and reads the real tiles
    def _prod(path):
        a = Asset(path=path, label="pointcloud_copc", category="pointcloud", kind="pcl",
                  stac_roles=["data"], media_type="application/vnd.laszip+copc",
                  extensions=[], cloud_native=True, thumbnail="pointcloud")
        return Product(id=path.stem, category="pointcloud", kind="pcl", assets=[a],
                       group="pielach_2024-10-09_tiles")

    monkeypatch.setattr(mgr, "discover",
                        lambda folder, policy, **kw: [_prod(p) for p in sorted(tiles.glob("*.las"))])
    monkeypatch.setattr(mgr, "pcl_point_count", lambda path: 20_000)

    def _fake_item(product, campaign, **kw):
        a = product.assets[0]
        fm = file_meta(a.path)   # the idempotency gate reads file:size / file:checksum back off this
        item = pystac.Item(id=product.id, geometry={"type": "Point", "coordinates": [15.4, 48.2]},
                           bbox=[15.4, 48.2, 15.4, 48.2],
                           datetime=datetime(2024, 10, 9, tzinfo=timezone.utc), properties={})
        item.add_asset(a.label, pystac.Asset(
            href=str(a.path), media_type=a.media_type, roles=["data"],
            extra_fields={"file:size": fm.size, "file:checksum": "1220" + fm.sha256}))
        return item

    monkeypatch.setattr(mgr, "build_item", _fake_item)

    out = tmp_path / "catalog"
    thumb = "./pielach_2024-10-09_tiles_thumbnail.png"

    update_catalog(tmp_path, out, RunPolicy())
    png = next(out.rglob("pielach_2024-10-09_tiles_thumbnail.png"))
    assert _coll_asset_href(out, "pielach_2024-10-09_tiles", "thumbnail") == thumb
    stamp = png.stat().st_mtime_ns

    # nothing changed: no re-render, but the asset survives (collections are rebuilt from scratch)
    update_catalog(tmp_path, out, RunPolicy())
    assert png.stat().st_mtime_ns == stamp
    assert _coll_asset_href(out, "pielach_2024-10-09_tiles", "thumbnail") == thumb

    # a properties edit refreshes the members in place, so the aggregate is not re-rendered
    (camp / "campaign.yaml").write_text("properties:\n  platform: airborne\n", encoding="utf-8")
    res = update_catalog(tmp_path, out, RunPolicy())
    assert res["ok"]["2024-10-09"]["rebuilt"] == 0 and res["ok"]["2024-10-09"]["refreshed"] == 3
    assert png.stat().st_mtime_ns == stamp

    # a member's content changes -> re-render
    write_las(tiles / "pielach_2024-10-09_pcl_0.las", n=20_001)
    res = update_catalog(tmp_path, out, RunPolicy())
    assert res["ok"]["2024-10-09"]["rebuilt"] == 1
    assert png.stat().st_mtime_ns != stamp
    stamp = png.stat().st_mtime_ns

    # the member set moves -> re-render
    (tiles / "pielach_2024-10-09_pcl_2.las").unlink()
    update_catalog(tmp_path, out, RunPolicy(stale="remove"))
    assert png.stat().st_mtime_ns != stamp
    assert _coll_asset_href(out, "pielach_2024-10-09_tiles", "thumbnail") == thumb

    # relative href mode: the collection thumbnail still resolves next to its collection.json
    update_catalog(tmp_path, out, RunPolicy(stale="remove", asset_hrefs="relative"))
    assert _coll_asset_href(out, "pielach_2024-10-09_tiles", "thumbnail") == thumb
