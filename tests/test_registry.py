import pytest

from turnstac.core.registry import LABELS, STEM_PATTERNS, merge_overrides

FULL_LABEL = {"category": "pointcloud", "kind": "pcl", "stac_roles": ["data"],
              "media_type": "application/vnd.laszip+copc",
              "extensions": ["pointcloud", "projection", "file"], "thumbnail": "pointcloud"}


def test_pattern_override_defaults_omitted_keys_leaves_siblings():
    sp, _ = merge_overrides({"pointcloud": {"extensions": [".laz", ".las"]}}, {})
    assert sp["pointcloud"]["extensions"] == [".laz", ".las"]
    assert sp["pointcloud"]["require"] == [] and sp["pointcloud"]["forbid"] == []
    assert sp["dtm"] == STEM_PATTERNS["dtm"] and sp["dsm"] == STEM_PATTERNS["dsm"]


def test_full_label_override_replaces_entry():
    _, lb = merge_overrides({}, {"pointcloud": FULL_LABEL})
    assert lb["pointcloud"] == FULL_LABEL


def test_partial_ignore_label_backfills_quietly(caplog):
    _, lb = merge_overrides({}, {"scratch": {"category": "ignore"}})
    assert lb["scratch"] == {"category": "ignore", "kind": "", "stac_roles": [],
                             "media_type": "", "extensions": [], "thumbnail": None}
    assert "missing keys" not in caplog.text   # debug for ignore labels, not a warning


def test_partial_product_label_backfills_with_warning(caplog):
    _, lb = merge_overrides({}, {"bathy": {"category": "dtm", "kind": "raster"}})
    assert lb["bathy"]["stac_roles"] == [] and lb["bathy"]["thumbnail"] is None
    assert "missing keys" in caplog.text       # empty kind/media_type would fail the item build


def test_unknown_thumbnail_kind_raises():
    with pytest.raises(ValueError, match="not a renderer kind"):
        merge_overrides({}, {"dtm": {**FULL_LABEL, "thumbnail": "hillshde"}})


def test_unknown_pattern_key_raises():
    with pytest.raises(ValueError, match="unknown keys"):
        merge_overrides({"dtm": {"require": ["dtm"], "extensios": [".tif"]}}, {})


def test_unknown_label_key_raises():
    with pytest.raises(ValueError, match="unknown keys"):
        merge_overrides({}, {"dtm": {**FULL_LABEL, "catgory": "dtm"}})


def test_override_dicts_are_copied_not_adopted():
    patterns = {"dtm": {"require": ["DTM"]}}
    labels = {"scratch": {"category": "ignore"}}
    merge_overrides(patterns, labels)
    assert patterns == {"dtm": {"require": ["DTM"]}}          # not lowercased in place
    assert labels == {"scratch": {"category": "ignore"}}      # not backfilled in place


def test_empty_pattern_raises():
    with pytest.raises(ValueError):
        merge_overrides({"x": {}}, {})


def test_override_tokens_and_extensions_are_lowercased():
    sp, _ = merge_overrides({"dtm": {"require": ["DTM", "Masked"], "forbid": ["SHD"],
                                     "extensions": [".TIF"]}}, {})
    assert sp["dtm"] == {"require": ["dtm", "masked"], "forbid": ["shd"], "extensions": [".tif"]}


@pytest.mark.parametrize("key", ["require", "forbid", "extensions"])
def test_scalar_pattern_key_wrapped_with_warning(key, caplog):
    sp, _ = merge_overrides({"dtm": {key: "DTM"}}, {})
    assert sp["dtm"][key] == ["dtm"]
    assert "one-element list" in caplog.text


def test_no_overrides_copies_equal_originals():
    sp, lb = merge_overrides(None, None)
    assert sp == STEM_PATTERNS and lb == LABELS
