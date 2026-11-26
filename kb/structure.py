"""Build a document structure tree and parent/child units from parsed blocks.

A parser only reports ordered blocks plus heading evidence. This module turns
that into the two things retrieval needs: a stable section tree and the unit
each block belongs to, so every chunk can be traced back to its chapter and
expanded through its parent when more context is required.
"""

from __future__ import annotations

import hashlib

ROOT_KEY = "root"


def unit_key(path, ordinal: int) -> str:
    """Stable business key: same heading path and position, same key."""
    digest = hashlib.sha1()
    digest.update("\x1f".join(path).encode("utf-8"))
    digest.update(("#%d" % ordinal).encode("utf-8"))
    return digest.hexdigest()[:16]


def _page_of(block):
    value = (block.location or {}).get("page")
    return value if isinstance(value, int) and value > 0 else None


def _extend(unit: dict, index: int, page) -> None:
    unit["block_end"] = index
    if page is None:
        return
    start, end = unit.get("page_start"), unit.get("page_end")
    unit["page_start"] = page if start is None else min(start, page)
    unit["page_end"] = page if end is None else max(end, page)


def build_units(blocks) -> tuple[list[dict], list[str | None]]:
    """Return (units, block_unit_keys).

    units[0] is the document root; the rest follow document order. Every block
    maps to the unit that directly contains it, so a chunk's parent is found by
    walking unit parent links.
    """
    root = {"key": ROOT_KEY, "title": "", "level": 0, "parent_key": None,
            "ordinal": 0, "block_start": 0, "block_end": 0,
            "page_start": None, "page_end": None, "path": []}
    units = [root]
    keys = [None] * len(blocks)
    stack = [0]
    titles = {0: []}

    for index, block in enumerate(blocks):
        if getattr(block, "role", "body") == "toc":
            continue
        level = int(getattr(block, "heading_level", 0) or 0)
        if getattr(block, "role", "body") == "heading" and level > 0:
            while len(stack) > 1 and units[stack[-1]]["level"] >= level:
                stack.pop()
            parent_index = stack[-1]
            path = titles[parent_index] + [block.text.strip()]
            unit = {"key": unit_key(path, len(units)), "title": block.text.strip(),
                    "level": level, "parent_key": units[parent_index]["key"],
                    "ordinal": len(units), "block_start": index, "block_end": index,
                    "page_start": None, "page_end": None, "path": path}
            units.append(unit)
            titles[len(units) - 1] = path
            stack.append(len(units) - 1)
        # A parent covers its whole subtree: that range is the context a
        # parent-child lookup returns when a descendant chunk is retrieved.
        page = _page_of(block)
        for unit_index in stack:
            units[unit_index]["block_start"] = min(units[unit_index]["block_start"], index)
            _extend(units[unit_index], index, page)
        keys[index] = units[stack[-1]]["key"]
    return units, keys


def from_rows(rows) -> list[dict]:
    """Convert database rows (unit_key/parent_id) into tree-ready units."""
    keys = {row["id"]: row["unit_key"] for row in rows}
    return [{"key": row["unit_key"], "title": row["title"], "level": row["level"],
             "ordinal": row["ordinal"], "parent_key": keys.get(row["parent_id"]),
             "block_start": row["block_start"], "block_end": row["block_end"],
             "page_start": row["page_start"], "page_end": row["page_end"],
             "chunk_count": row["chunk_count"]} for row in rows]


def tree(units) -> list[dict]:
    """Nest units for display; input order is document order."""
    by_key = {unit["key"]: {**unit, "children": []} for unit in units}
    roots = []
    for unit in units:
        node = by_key[unit["key"]]
        parent = by_key.get(unit.get("parent_key") or "")
        if parent is None or unit["key"] == ROOT_KEY:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots
