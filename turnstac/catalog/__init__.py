# CLI / Config resolver   (cli.py + config.py)
#         │ config.yaml (namespaced) + campaign.yaml sidecars + CLI overrides
#         ▼
# Layer 1  Discovery + Extraction   (discover.py, registry.py, extract.py)
#    walk → matcher → (label, category, kind, roles, media_type, extensions)
#    twin resolve + tile groups; associate sidecars (same dir, stem or full-name form)
#    reader[kind] does I/O once → AssetMeta; @populator(ext) maps AssetMeta → pystac
#         ▼
# Layer 2  Item + Collection builder   (build.py, hierarchy.py)
#    one Item per Product; enable extensions; build ids/datetime/geometry
#    placement resolver: Product → flat node / group nodes
#         ▼
# Layer 3  Catalog manager   (manager.py, thumbnail.py)
#    load existing → gate by id (size+hash) → build/reuse → stale sweep → write
