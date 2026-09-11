"""Placement resolver.

Products -> placement Nodes: one flat node (campaign collection body) plus one node
per subcollection group. Starts from discover's auto tile groups (product.group),
then applies the sidecar hierarchy block:
  placement: {product_id: group_name | ~}   pin into a group / force flat
  groups:    {group_name: {title, description}}   subcollection metadata
no pystac import.
"""

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch

log = logging.getLogger(__name__)

WILDCARDS = "*?["


@dataclass
class Node:
    name: str | None            # None = flat in the campaign collection
    title: str | None = None
    description: str | None = None
    products: list = field(default_factory=list)

    def __str__(self) -> str:
        target = self.name if self.name else "<flat>"
        return f"Node {target}  ({len(self.products)} products)"


def _is_pattern(key: str) -> bool:
    return any(c in key for c in WILDCARDS)


def _pattern_hits(pid: str, patterns: list) -> list:
    return [k for k in patterns if fnmatch(pid.lower(), k.lower())]


def resolve_hierarchy(products, hier: dict | None = None) -> list[Node]:
    """Products + sidecar hierarchy block -> [flat Node, *group Nodes].
    Flat node always first. placement wins over product.group; a group named only
    in 'groups', with no products, warns and is dropped."""
    hier = hier or {}
    placement = hier.get("placement") or {}
    groups_meta = hier.get("groups") or {}
    patterns = [k for k in placement if _is_pattern(k)]

    buckets: dict = {}
    used: set = set()
    warned: set = set()
    for p in products:
        # hits are collected even when an exact key wins: a shadowed pattern is not a typo
        hits = _pattern_hits(p.id, patterns)
        used.update(hits)
        if p.id in placement:
            used.add(p.id)
            g = placement[p.id]
        elif hits:
            if len(hits) > 1 and tuple(hits) not in warned:  # once per overlap, not per product
                warned.add(tuple(hits))
                log.warning(f"placement patterns {hits} overlap, first wins: {hits[0]!r} "
                            f"(first hit: {p.id})")
            g = placement[hits[0]]
            log.debug(f"placement pattern {hits[0]!r} puts {p.id} into {g or '<flat>'}")
        else:
            g = p.group
        buckets.setdefault(g, []).append(p)

    for key in sorted(set(placement) - used):
        if _is_pattern(key):
            log.warning(f"hierarchy placement pattern matched no products: {key}")
        else:
            log.warning(f"hierarchy placement for unknown product id: {key}")

    nodes = [Node(None, products=buckets.pop(None, []))]
    for name in sorted(buckets):
        meta = groups_meta.get(name) or {}
        nodes.append(Node(name, meta.get("title"), meta.get("description"), buckets[name]))

    for name in sorted(set(groups_meta) - {n.name for n in nodes}):
        log.warning(f"hierarchy group {name!r} has no products, dropped")
    return nodes
