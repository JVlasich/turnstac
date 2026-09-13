"""RunPolicy: run-wide knobs, built once from config, passed down unchanged.

One default per field, declared here. `CATALOG_DEFAULTS` derives the config template from
`config_defaults()`; `cli.main` builds the instance with `from_config()` and hands the same
object to update_catalog -> process_campaign -> discover. Root metadata (id/title/license/
providers) and the OPALS options stay out: not per-run policy.
"""

from dataclasses import dataclass, fields
from typing import Literal, get_args, get_origin

# config key (camelCase, as written in YAML / argparse) -> field name.
# Declaration order also fixes the key order of the generated config template.
_FIELD_MAP = {
    "stale":          "stale",
    "dryRun":         "dry_run",
    "force":          "force",
    "validate":       "validate",
    "unknownAssets":  "unknown_assets",
    "nonCloudNative": "non_cloud_native",
    "invalidCog":     "invalid_cog",
    "only":           "only",
    "idCollisions":   "id_collisions",
    "assetHrefs":     "asset_hrefs",
    "minPoints":      "min_points",
    "thumbnails":     "thumbnails",
}


@dataclass(frozen=True)
class RunPolicy:
    """Immutable knob set for one catalog run."""

    stale: Literal["warn", "remove", "raise"] = "warn"   # items and collections gone from disk
    dry_run: bool = False
    force: bool = False                                  # skip checks, rebuild every item
    validate: bool = False                               # STAC-validate after save (needs pystac[validation])
    unknown_assets: Literal["warn", "skip", "raise"] = "warn"    # unclassifiable files
    non_cloud_native: Literal["warn", "skip", "raise"] = "warn"  # files without a CN twin
    # a COG failing the structure validator. media_type is the catalog's only cloud-native
    # indicator, so demote publishes it as a plain GeoTIFF rather than let the type lie
    invalid_cog: Literal["warn", "demote", "raise"] = "demote"
    only: str | None = None                              # glob over campaign dir names; skips the stale-collection sweep
    id_collisions: Literal["warn", "raise"] = "warn"     # duplicate item/subcollection ids across campaigns
    asset_hrefs: Literal["absolute", "relative"] = "absolute"  # relative (self-contained) | absolute (build-time paths)
    min_points: int = 1000                               # drop point-cloud items below this pc:count (degenerate tiles)
    thumbnails: bool = True                              # render PNG thumbnails for raster items (ortho/DSM/DTM)

    def __post_init__(self):
        """Every Literal field must hold one of its declared values."""
        for f in fields(self):
            if get_origin(f.type) is not Literal:
                continue
            allowed = get_args(f.type)
            value = getattr(self, f.name)
            if value not in allowed:
                raise ValueError(f"{f.name}={value!r} not in {allowed}")

    @classmethod
    def from_config(cls, cfg: dict) -> "RunPolicy":
        """From a resolved config section (defaults < file < CLI).
        Keys absent from cfg keep the field default."""
        return cls(**{attr: cfg[key] for key, attr in _FIELD_MAP.items() if key in cfg})

    @classmethod
    def config_defaults(cls) -> dict:
        """{camelCase config key: default} for CATALOG_DEFAULTS."""
        defaults = {f.name: f.default for f in fields(cls)}
        return {key: defaults[attr] for key, attr in _FIELD_MAP.items()}
