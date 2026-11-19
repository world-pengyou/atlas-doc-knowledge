"""Consolidate search evidence without discarding distinct text or citations."""
from dataclasses import dataclass


MAX_MERGED_TEXT = 1400


def _text_key(text):
    # Keep case, punctuation, numbers and internal whitespace meaningful.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass
class SearchGroup:
    representative: dict
    text: str
    members: list
    first: dict
    last: dict
    rank: int


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _adjacent_overlap(left, right):
    """Require both source continuity and a verified, literal text overlap."""
    a, b = left.last, right.first
    if (a["document_id"] != b["document_id"] or a["kind"] != b["kind"]
            or not _integer(a.get("ordinal")) or not _integer(b.get("ordinal"))
            or b["ordinal"] != a["ordinal"] + 1
            or a.get("asset_name") != b.get("asset_name")):
        return 0
    if a["kind"] == "image" and not a.get("asset_name"):
        return 0
    loc_a, loc_b = a["location"], b["location"]
    spans = [loc_a.get("char_start"), loc_a.get("char_end"),
             loc_b.get("char_start"), loc_b.get("char_end")]
    if not all(_integer(value) for value in spans):
        return 0
    start_a, end_a, start_b, end_b = spans
    if not 0 <= start_a < start_b < end_a < end_b:
        return 0
    if "page" in loc_a or "page" in loc_b:
        if (loc_a.get("page") != loc_b.get("page") or not loc_a.get("bbox")
                or loc_a["bbox"] != loc_b.get("bbox")
                or loc_a.get("preview") != loc_b.get("preview")):
            return 0
    elif loc_a.get("format") == "docx" or loc_b.get("format") == "docx":
        if (loc_a.get("format") != "docx" or loc_b.get("format") != "docx"
                or not _integer(loc_a.get("paragraph")) or loc_a["paragraph"] < 1
                or any(loc_a.get(key) != loc_b.get(key) for key in
                       ("paragraph", "table", "row", "cell", "image", "heading", "preview"))):
            return 0
    else:
        lines = [loc_a.get("line_start"), loc_a.get("line_end"),
                 loc_b.get("line_start"), loc_b.get("line_end")]
        if not all(_integer(value) for value in lines):
            return 0
        first_a, last_a, first_b, last_b = lines
        if (not 1 <= first_a <= first_b <= last_a + 1 or last_b < last_a
                or loc_a.get("heading") != loc_b.get("heading")
                or loc_a.get("language") != loc_b.get("language")):
            return 0
    # split_text strips outer whitespace, so offsets alone cannot trim text.
    trimmed_a = end_a - start_a - len(a["text"])
    trimmed_b = end_b - start_b - len(b["text"])
    if trimmed_a < 0 or trimmed_b < 0:
        return 0
    expected = end_a - start_b
    minimum = max(8, expected - trimmed_a - trimmed_b)
    maximum = min(expected, len(a["text"]), len(b["text"]))
    for size in range(maximum, minimum - 1, -1):
        if left.text[-size:] == right.text[:size]:
            return size
    return 0


def _combine(left, right, text):
    best = left if left.rank < right.rank else right
    return SearchGroup(best.representative, text, left.members + right.members,
                       left.first, right.last, best.rank)


def deduplicate(rows):
    """Input is RRF ordered; output keeps the best representative per group.

    Exact duplicates retain all sources. Adjacency uses the canonical source
    of each group; uncertain alternative-source adjacency remains separate.
    """
    exact = {}
    groups = []
    for rank, row in enumerate(rows):
        key = _text_key(row["text"])
        if key in exact:
            exact[key].members.append(row)
        else:
            group = SearchGroup(row, row["text"], [row], row, row, rank)
            exact[key] = group
            groups.append(group)
    # Source order allows a lower-ranked predecessor to be prepended correctly.
    groups.sort(key=lambda group: (str(group.first["document_id"]),
                                  group.first.get("ordinal") if _integer(group.first.get("ordinal")) else -1,
                                  group.rank))
    adjacent = []
    for group in groups:
        overlap = _adjacent_overlap(adjacent[-1], group) if adjacent else 0
        if overlap and len(adjacent[-1].text) + len(group.text) - overlap <= MAX_MERGED_TEXT:
            previous = adjacent.pop()
            adjacent.append(_combine(previous, group, previous.text + group.text[overlap:]))
        else:
            adjacent.append(group)
    # Different original slice boundaries can produce the same merged passage.
    exact = {}
    result = []
    for group in sorted(adjacent, key=lambda group: group.rank):
        key = _text_key(group.text)
        if key in exact:
            exact[key].members.extend(group.members)
        else:
            exact[key] = group
            result.append(group)
    return result
