# Turnstac (Pielach STAC)

Automated, idempotent pipeline that turns the processed topo-bathymetric LiDAR
time series of the Pielach river into a standards-compliant static
[STAC 1.1](https://stacspec.org) catalog. Bachelor thesis project, TU Wien,
Department of Geodesy and Geoinformation.

## Deployment layout

The repo folder is meant to live inside the processed-datasets root.
Each campaign is one ISO-dated folder next to it,
holding the products plus a per-campaign `campaign.yaml` sidecar
(template: `sample_configs/sample_campaign.yaml`):

```
data-root/
├── 2023-02-08/              # campaign: <date>-named folder
│   ├── campaign.yaml        # per-campaign metadata + overrides (optional)
│   ├── *_dtm_*.tif ...      # products (COG / COPC preferred)
│   └── <name>_tiles/        # tiled products -> subcollection
├── 2024-10-09/
├── catalog/                 # generated STAC catalog (output)
└── <this repo>/
```

## Prerequisites

- **Not a pip package.** Clone or unzip the repo and run it in place, inside `opalsShell`
- Windows with an [OPALS](https://opals.geo.tuwien.ac.at/) installation
  (default path `C:\opals_nightly_2.6.0`, override with the `OPALS_ROOT`
  environment variable). GDAL, numpy, scipy and matplotlib included in OPALS'
  bundled Python.
- Dependencies (pystac, pyyaml, laspy, lazrs) are vendored in `libs\`, no pip
  install needed. `lazrs` ships as a compiled extension for Linux and Windows.
- Running outside `opalsShell`: set `PROJ_LIB=<opals>\addons\crs` and
  `GDAL_DATA=<opals>\addons\gdal`, otherwise CRS information drops silently.
  The `.bat` launchers handle this.

## Quick start

- **`update_catalog.bat`** — double-click to build/refresh the catalog into
  `<data root>\catalog`. Re-running is safe: an item is rebuilt only when its file
  changed, and a `campaign.yaml` `properties` edit is patched into the affected items
  without re-reading them. It is not free. The gate is content-true, so a file whose
  size is unchanged is still hashed (sha256), and `discover` opens
  every raster once with GDAL.
  Passes `"%REPO%\config.yaml"` if it exists
- **`view_catalog.bat`** — serves data root + bundled STAC Browser and opens
  `http://localhost:8111/browser/`.

## CLI

run inside repo-root:
```
python -m turnstac <data-root> [--config config.yaml] [options]
python -m turnstac --init <path>
```

Key options (CLI > YAML config > defaults):

| Option | Effect |
| --- | --- |
| `--force` | skip the idempotency gate, rebuild everything (use after registry/code changes) |
| `--dryRun` | discover + gate only, write nothing |
| `--only <glob>` | process only matching campaign dirs |
| `--stale warn\|remove\|raise` | items/collections whose files vanished from disk |
| `--unknownAssets warn\|skip\|raise` | files matching no registry pattern |
| `--nonCloudNative warn\|skip\|raise` | files without a cloud-native twin |
| `--invalidCog warn\|demote\|raise` | rasters failing the COG structure validator (demote = publish as plain GeoTIFF) |
| `--idCollisions warn\|raise` | duplicate ids across campaigns |
| `--assetHrefs relative\|absolute` | asset href style (thumbnails always relative) |
| `--thumbnails / --no-thumbnails` | PNG thumbnails for raster + COPC items |
| `--validate` | STAC-validate after saving (needs `pystac[validation]`) |

Each run writes a machine-readable report to `<out>/last_run.json`.

## Pre-processing tools

| Tool | Purpose |
| --- | --- |
| `python -m turnstac.pre.tac_pcl` | tile a LAZ with OPALS and convert tiles to COPC |
| `python -m turnstac.pre.tac_raster` | convert GeoTIFF to COG, optional tiling above a size threshold |
| `python -m turnstac.pre.c_copc` | convert LAZ to COPC without tiling |

The two tiling tools share the `--config` / `--init` pattern (see
`sample_configs/sample_config.yaml`); `c_copc` takes plain CLI flags.

## Tests

```
python -m pytest tests
```
requires `pytest`, run in the same interpreter as the pipeline (`opalsShell`).

## License

MIT License, see `LICENSE`.

The vendored [stac-browser](https://github.com/radiantearth/stac-browser) build in
`browser/` is third-party code under its own ISC license, see `browser/LICENSE`.
The dependencies vendored in `libs/` each keep their upstream license
in their `.dist-info` folder.

The `lascopcindex64` binaries in `turnstac/bin/` are
[LAStools](https://github.com/LAStools/LAStools) version 260326 by rapidlasso GmbH,
redistributed under the LGPL 2.1. See `turnstac/bin/NOTICE-LAStools.txt` and
`turnstac/bin/COPYING-LAStools.LGPL21.txt`. They are run as a separate process,
not linked.
