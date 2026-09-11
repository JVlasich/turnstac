from types import SimpleNamespace

from turnstac.catalog.hierarchy import resolve_hierarchy


def _p(pid, group=None):
    return SimpleNamespace(id=pid, group=group)


def _products():
    return [
        _p("flat_a"), _p("flat_b"),
        _p("tile_1", "tiles"), _p("tile_2", "tiles"),
        _p("stray"),
    ]


HIER = {
    "placement": {"stray": "tiles", "tile_2": None, "ghost": "tiles"},
    "groups": {"tiles": {"title": "Tile group"}, "unused": {"title": "x"}},
}


def test_flat_node_first_holds_flats_and_force_flattened():
    nodes = resolve_hierarchy(_products(), HIER)
    assert nodes[0].name is None
    assert {p.id for p in nodes[0].products} == {"flat_a", "flat_b", "tile_2"}


def test_pinned_stray_joins_group_with_metadata():
    nodes = resolve_hierarchy(_products(), HIER)
    tiles = {n.name: n for n in nodes}["tiles"]
    assert {p.id for p in tiles.products} == {"tile_1", "stray"}
    assert tiles.title == "Tile group"


def test_unused_group_dropped_unknown_placement_only_warned():
    nodes = resolve_hierarchy(_products(), HIER)
    assert {n.name for n in nodes} == {None, "tiles"}


def test_no_hierarchy_block_auto_groups_pass_through():
    nodes = resolve_hierarchy(_products())
    assert {p.id for p in nodes[1].products} == {"tile_1", "tile_2"}


PATTERN_HIER = {
    "placement": {"flat_a": "tiles", "*_1": "tiles", "tile_*": None, "*_ghost": "tiles"},
    "groups": {"tiles": {}},
}


def test_pattern_groups_products_exact_key_still_wins():
    nodes = resolve_hierarchy(_products(), PATTERN_HIER)
    by_name = {n.name: n for n in nodes}
    # tile_1 matches both patterns, "*_1" comes first; tile_2 only matches "tile_*" -> flat
    assert {p.id for p in by_name["tiles"].products} == {"flat_a", "tile_1"}
    assert {p.id for p in by_name[None].products} == {"flat_b", "tile_2", "stray"}


def test_pattern_beats_auto_group(caplog):
    with caplog.at_level("WARNING"):
        resolve_hierarchy(_products(), PATTERN_HIER)
    assert "placement patterns" in caplog.text          # tile_1 overlap, warned once
    assert caplog.text.count("placement patterns") == 1
    assert "matched no products: *_ghost" in caplog.text


def test_pattern_shadowed_by_exact_key_is_not_a_typo(caplog):
    hier = {"placement": {"tile_1": "tiles", "tile_*": "tiles"}}
    with caplog.at_level("WARNING"):
        nodes = resolve_hierarchy([_p("tile_1")], hier)
    assert {n.name for n in nodes} == {None, "tiles"}
    assert "matched no products" not in caplog.text


def test_pattern_key_matches_case_insensitively():
    nodes = resolve_hierarchy([_p("Tile_A")], {"placement": {"tile_*": "tiles"}})
    assert {p.id for p in nodes[1].products} == {"Tile_A"}
