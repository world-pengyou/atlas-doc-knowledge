"""Turn parsed blocks into retrieval chunks.

Parsing yields semantic blocks, but their sizes vary by two orders of
magnitude: a paragraph can be one line while a "key/value card" table yields a
four-character cell. Indexing every block on its own produces hundreds of tiny
chunks that carry no context. This module keeps the semantic boundaries and
greedily packs neighbour blocks that belong to the same section into chunks of
about SIZE characters:

* a table row is never split between chunks, and its cells are joined as
  "column：value" so a card keeps its meaning;
* packing never crosses a section (unit) boundary, a table, or a page, so the
  location of every chunk stays exact;
* only blocks that are longer than SIZE are split, using sentence boundaries
  and an OVERLAP overlap like before.
"""

SIZE = 700
OVERLAP = 80
CELL_SEPARATOR = "："
_SENTENCE_ENDS = ("\n", "。", "！", "？", ";", "；")


def split_text(text, size=SIZE, overlap=OVERLAP):
    """Keep Chinese sentence boundaries, with a hard limit for unpunctuated text.

    Adapted from EduRAG's ChineseRecursiveTextSplitter separator strategy.
    This standalone form avoids importing the legacy application's configuration.
    """
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            candidates = [text.rfind(mark, start + size // 2, end)
                          for mark in _SENTENCE_ENDS]
            boundary = max(candidates)
            if boundary >= 0:
                end = boundary + 1
        value = text[start:end].strip()
        if value:
            yield value, start, end
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def _row_of(location):
    table, row = location.get("table"), location.get("row")
    if isinstance(table, int) and isinstance(row, int):
        return table, row
    return None


def _region(block):
    location = block.location
    if "table" in location:
        return "table", location.get("table")
    if "page" in location:
        return "page", location.get("page")
    return "body", None


def _row_text(blocks, texts, first, last):
    """Join the cells of one table row as "column：value" on a single line."""
    cells = []
    for index in range(first, last + 1):
        cell = blocks[index].location.get("cell")
        if cells and cells[-1][0] == cell:
            cells[-1][1].append(texts[index])
        else:
            cells.append((cell, [texts[index]]))
    values = [" ".join(part.strip() for part in parts if part.strip())
              for _, parts in cells]
    return CELL_SEPARATOR.join(value for value in values if value)


def _pieces(blocks, texts):
    """Yield (text, first block, last block, kind) with table rows merged."""
    index = 0
    while index < len(blocks):
        block = blocks[index]
        row = _row_of(block.location) if block.kind == "text" else None
        if row is None:
            yield texts[index], index, index, block.kind
            index += 1
            continue
        last = index
        while (last + 1 < len(blocks) and blocks[last + 1].kind == "text"
               and _row_of(blocks[last + 1].location) == row):
            last += 1
        yield _row_text(blocks, texts, index, last), index, last, "text"
        index = last + 1


def _extend_location(location, tail):
    """Record where a packed chunk ends so the source link can show a range."""
    for start_key, end_key in (("paragraph", "paragraph_end"), ("page", "page_end"),
                               ("row", "row_end"), ("cell", "cell_end")):
        start_value, end_value = location.get(start_key), tail.get(start_key)
        if isinstance(start_value, int) and isinstance(end_value, int) and end_value != start_value:
            location[end_key] = end_value
    if isinstance(location.get("line_start"), int) and isinstance(tail.get("line_end"), int):
        location["line_end"] = max(location.get("line_end") or 0, tail["line_end"])


def _make_chunk(text, blocks, first, last, unit_key, offset, span):
    location = dict(blocks[first].location)
    tail = blocks[last].location
    if span is None:
        location["char_start"], location["char_end"] = 0, len(text)
    else:
        location["char_start"], location["char_end"] = span
    if isinstance(location.get("line_start"), int):
        location["line_start"] += offset
        location["line_end"] = location["line_start"] + text.count("\n")
    if last != first:
        _extend_location(location, tail)
    return {"text": text, "kind": blocks[first].kind, "unit_key": unit_key,
            "block_start": first, "block_end": last, "location": location}


def build_chunks(blocks, texts, unit_keys, size=SIZE, overlap=OVERLAP):
    """Pack blocks into retrieval chunks, never crossing a section boundary."""
    chunks = []
    current = None
    current_key = None
    current_region = None

    def flush():
        nonlocal current, current_key, current_region
        if current is not None:
            chunks.append(current)
            current = current_key = current_region = None

    for text, first, last, kind in _pieces(blocks, texts):
        unit_key = unit_keys[first]
        region = _region(blocks[first])
        if kind == "image":
            # One picture keeps its own chunks: asset links and boxes point at it.
            flush()
            chunks.extend(_split(text, blocks, first, last, unit_key, size, overlap))
            continue
        if current is not None and (current_key != unit_key or current_region != region):
            # A section title that only opens a table or a figure would become a
            # chunk of its own; keep it as the lead-in of the first content chunk.
            lone_heading = (current_key == unit_key and current["block_start"] == current["block_end"]
                            and blocks[current["block_start"]].role == "heading"
                            and len(current["text"]) + 1 + len(text) <= size)
            if lone_heading:
                current_region = region
            else:
                flush()
        if len(text) <= size:
            if current is not None and len(current["text"]) + 1 + len(text) <= size:
                current["text"] += "\n" + text
                current["block_end"] = last
                current["location"]["char_end"] = len(current["text"])
                _extend_location(current["location"], blocks[last].location)
                continue
            flush()
            current = _make_chunk(text, blocks, first, last, unit_key, 0, None)
            current_key, current_region = unit_key, region
            continue
        # A single block longer than one chunk: split it and keep the pieces
        # alone, so their character offsets stay meaningful.
        flush()
        chunks.extend(_split(text, blocks, first, last, unit_key, size, overlap))
    flush()
    return chunks


def _split(text, blocks, first, last, unit_key, size, overlap):
    pieces = list(split_text(text, size, overlap))
    if len(pieces) == 1:
        return [_make_chunk(text, blocks, first, last, unit_key, 0, None)]
    return [_make_chunk(value, blocks, first, last, unit_key,
                        text[:start].count("\n"), (start, end))
            for value, start, end in pieces]
