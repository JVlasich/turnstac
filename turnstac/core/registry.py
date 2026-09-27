"""Default registry.

Per-campaign overrides ('campaign.yaml'): a 'patterns' entry replaces a whole
STEM_PATTERNS entry (omitted require/forbid/extensions default to []). A 'labels'
entry replaces a whole LABELS entry; omitted keys default to empty, which is only
useful for a 'category: ignore' label. Unknown keys in either block raise.
New patterns/labels can be defined entirely in overrides.

Also here: override merge, validation."""

import logging
from typing import Any

log = logging.getLogger(__name__)

# stem_patterns: label -> match rule. stem split("_") -> set -> matched against require/forbid
# {label: {"require": [], "forbid": [], "extensions": ""}}
STEM_PATTERNS: dict[str, dict[str, object]] = {
    # Pointcloud variants
    "pointcloud_copc": {
        "require": [],
        "forbid": [],
        "extensions": [".copc.laz"],
    },
    "pointcloud": {
        "require": [],
        "forbid": [],
        "extensions": [".laz"],
    },
    "pointcloud_las": {
        "require": [],
        "forbid": [],
        "extensions": [".las"],
    },

    # Ortho
    "orthophoto": {
        "require": ["transparent", "mosaic"],
        "forbid": [],
        "extensions": [".tif", ".tiff"],
    },

    # DTM variants
    "dtm": {
        "require": ["dtm"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },
    "dtm_filled": {
        "require": ["dtm", "filled"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },
    "dtm_masked": {
        "require": ["dtm", "masked"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },

    # DSM variants
    "dsm": {
        "require": ["dsm"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },
    "dsm_filled": {
        "require": ["dsm", "filled"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },
    "dsm_masked": {
        "require": ["dsm", "masked"],
        "forbid": ["shd"],
        "extensions": [".tif", ".tiff"],
    },

    # shd for filtering
    "shade": {
        "require": ["shd"],
        "forbid": [],
        "extensions": [".tif", ".tiff"],
    },
    "log": {
        "require": [],
        "forbid": [],
        "extensions": [".log"],
    },

}


# labels: label -> role definition
LABELS: dict[str, dict[str, Any]] = {
    "pointcloud_copc": {
        "category":   "pointcloud",          # drives item-grouping + collection placement
        "kind":       "pcl",           # reader dispatch (pcl | raster)
        "stac_roles": ["data"],               # STAC asset.roles array
        "media_type": "application/vnd.laszip+copc",
        "extensions": ["pointcloud", "projection", "file"],  # populators to run
        "thumbnail":  "pointcloud",           # renderer: rgb | hillshade | pointcloud; None = no thumbnail
    },
    "pointcloud": {
        "category":   "pointcloud",
        "kind":       "pcl",
        "stac_roles": ["data"],
        "media_type": "application/vnd.laszip",
        "extensions": ["pointcloud", "projection", "file"],
        "thumbnail":  None,
    },
    "pointcloud_las": {
        "category":   "pointcloud",
        "kind":       "pcl",
        "stac_roles": ["data"],
        "media_type": "application/vnd.las",
        "extensions": ["pointcloud", "projection", "file"],
        "thumbnail":  None,
    },

    # orthophoto: RGB orthomosaic, primary deliverable -> data + visual; eo comes off color interp
    "orthophoto": {
        "category":   "orthophoto",
        "kind":       "raster",
        "stac_roles": ["data", "visual"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file"],
        "thumbnail":  "rgb",
    },

    # DTM (terrain) variants -> category "dtm"
    "dtm": {
        "category":   "dtm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },
    "dtm_filled": {
        "category":   "dtm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },
    "dtm_masked": {
        "category":   "dtm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },

    # DSM (surface) variants -> category "dsm"
    "dsm": {
        "category":   "dsm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },
    "dsm_filled": {
        "category":   "dsm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },
    "dsm_masked": {
        "category":   "dsm",
        "kind":       "raster",
        "stac_roles": ["data"],
        "media_type": "image/tiff; application=geotiff",
        "extensions": ["bands", "projection", "file", "histogram"],
        "thumbnail":  "hillshade",
    },

    # category "ignore" = discover matches then drops silently (derived viz, not a product)
    "shade": {
        "category":   "ignore",
        "kind":       "raster",
        "stac_roles": [],
        "media_type": "image/tiff; application=geotiff",
        "extensions": [],
        "thumbnail":  None,
    },
    "log": {
        "category":   "ignore",
        "kind":       "",
        "stac_roles": [],
        "media_type": "",
        "extensions": [],
        "thumbnail":  None,
    },
}

SIDECAR_EXTENSIONS = {".prj", ".tfw", ".aux.xml"}  # recognized, never an asset, never "unknown"


# --- override merge ---

_PATTERN_KEYS = ("require", "forbid", "extensions")
# label key -> value backfilled when an override omits it
_LABEL_DEFAULTS: dict[str, Any] = {
    "category":   "",
    "kind":       "",
    "stac_roles": [],
    "media_type": "",
    "extensions": [],
    "thumbnail":  None,
}
_LABEL_KEYS = tuple(_LABEL_DEFAULTS)
_THUMB_KINDS = ("rgb", "hillshade", "pointcloud")  # render_thumbnail dispatch


def merge_overrides(patterns, labels):
    """Per-campaign overrides onto the defaults. Returns merged (stem_patterns,
    labels) copies; module globals stay unmutated."""
    sp = {k: dict(v) for k, v in STEM_PATTERNS.items()}
    # copy the caller's entries too: _validate normalizes/backfills them in place
    sp.update({k: dict(v) if isinstance(v, dict) else v for k, v in (patterns or {}).items()})

    lb = {k: dict(v) for k, v in LABELS.items()}
    lb.update({k: dict(v) if isinstance(v, dict) else v for k, v in (labels or {}).items()})

    _validate(sp, lb)
    return sp, lb


def _validate(stem_patterns, labels) -> None:
    for key, value in stem_patterns.items():
        if not isinstance(value, dict) or not value:
            raise ValueError(f"pattern {key!r}: set at least one key")
        unknown = [k for k in value if k not in _PATTERN_KEYS]
        if unknown:
            raise ValueError(f"pattern {key!r}: unknown keys {unknown}, known: {list(_PATTERN_KEYS)}")
        for k in _PATTERN_KEYS:
            if isinstance(value.get(k), str):  # a bare scalar would iterate character-wise
                log.warning(f"pattern {key!r}: {k} is the string {value[k]!r}, read as a one-element list")
                value[k] = [value[k]]
            value[k] = [str(t).lower() for t in value.get(k, [])]  # omitted -> []; matching is lowercased
    for key, value in labels.items():
        unknown = [k for k in value if k not in _LABEL_KEYS]
        if unknown:
            raise ValueError(f"label {key!r}: unknown keys {unknown}, known: {list(_LABEL_KEYS)}")
        thumb = value.get("thumbnail")
        if not (thumb is None or thumb in _THUMB_KINDS):
            raise ValueError(f"label {key!r}: thumbnail {thumb!r} is not a renderer kind, "
                             f"use one of {list(_THUMB_KINDS)} or null")
        missing = [k for k in _LABEL_KEYS if k not in value]
        if not missing:
            continue
        for k in missing:
            default = _LABEL_DEFAULTS[k]
            value[k] = list(default) if isinstance(default, list) else default
        # fine for an ignore label, a build failure for anything else
        report = log.debug if value["category"] == "ignore" else log.warning
        report(f"label {key!r}: missing keys {missing}, defaulted to empty")
