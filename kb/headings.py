"""Deterministic heading, numbering and page-furniture rules.

Both parsers use these helpers instead of inventing local heuristics: the same
rules decide whether a line is a title, which level it opens, and whether it is
table-of-contents or running header/footer noise.
"""

from __future__ import annotations

import math
import re
from collections import Counter

MAX_TITLE_CHARS = 60
MAX_BOLD_TITLE_CHARS = 30
MAX_TOC_CHARS = 100
MIN_TOC_LINES = 3
RUNNING_LINE_RATIO = 0.6
MARGIN_BAND = 0.07
MAX_LEVEL = 6

_SENTENCE_MARKS = "。！？；，、：:;,!?…"

_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百千万零〇两\d]{1,6}\s*[章篇]\s*")
_SECTION = re.compile(r"^第\s*[一二三四五六七八九十百千万零〇两\d]{1,6}\s*节\s*")
_ARTICLE = re.compile(r"^第\s*[一二三四五六七八九十百千万零〇两\d]{1,6}\s*条\s*")
_ATTACHMENT = re.compile(r"^附\s*[件录表]\s*[一二三四五六七八九十\d]{0,4}\s*")
_CN_PAREN = re.compile(r"^[（(]\s*[一二三四五六七八九十百]{1,3}\s*[）)]\s*")
_CN_DOT = re.compile(r"^[一二三四五六七八九十百]{1,3}\s*、\s*")
_NUMBERED = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2}){1,5})(?=[\s、．]|[^\d.．])\s*")
_SINGLE = re.compile(r"^(\d{1,2})\s*[、.．]\s*(?=\S)")

# Real PDFs often space the leader dots and even the page digits apart.
_TOC_LEADER = re.compile(
    r"^(.{1,80}?)[\s\u3000]*(?:[.…·][\s\u3000]*){2,}(\d(?:[\s\u3000]?\d){0,3})$")
_TOC_GAP = re.compile(r"^(.{1,80}?)[\s\u3000]{2,}(\d(?:[\s\u3000]?\d){0,3})$")


def numbering_level(text: str) -> tuple[int, str] | None:
    """Return (level, strength) for the numbering scheme a line starts with.

    strength is "strong" for schemes that only appear on titles
    (第X章、2.2.1、附件) and "weak" for single-level numbers (1、) which
    ordinary list paragraphs also use.
    """
    text = (text or "").strip()
    if not text:
        return None
    if _CHAPTER.match(text):
        return 1, "strong"
    if _ATTACHMENT.match(text):
        return 1, "strong"
    if _SECTION.match(text):
        return 2, "strong"
    if _CN_DOT.match(text):
        return 2, "strong"
    if _CN_PAREN.match(text):
        return 3, "strong"
    if _ARTICLE.match(text):
        return 3, "strong"
    matched = _NUMBERED.match(text)
    if matched:
        numbering = matched.group(1)
        depth = numbering.count(".") + numbering.count("．") + 1
        return min(MAX_LEVEL, depth), "strong"
    if _SINGLE.match(text):
        return 2, "weak"
    return None


def is_title_like(text: str, limit: int = MAX_TITLE_CHARS) -> bool:
    """A short, single-line phrase without sentence punctuation."""
    text = (text or "").strip()
    if not text or len(text) > limit or "\n" in text:
        return False
    if text[-1] in _SENTENCE_MARKS:
        return False
    return "。" not in text


def is_toc_line(text: str) -> bool:
    """A table-of-contents entry with a page number at the end."""
    text = (text or "").strip()
    if not text or len(text) > MAX_TOC_CHARS or "\n" in text:
        return False
    return bool(_TOC_LEADER.match(text) or _TOC_GAP.match(text))


def title_sizes(sizes, body_size: float, minimum_ratio: float = 1.08) -> list[float]:
    """Distinct font sizes visibly larger than body text, largest first."""
    if not body_size or body_size <= 0:
        return []
    unique = sorted({round(float(size), 1) for size in sizes
                     if size and size >= body_size * minimum_ratio}, reverse=True)
    return unique[:5]


def body_font_size(weighted_sizes) -> float:
    """Character-weighted most common font size; 0.0 when nothing is usable."""
    counts = Counter()
    for size, characters in weighted_sizes:
        if size and characters > 0:
            counts[round(float(size), 1)] += characters
    if not counts:
        return 0.0
    return counts.most_common(1)[0][0]


class HeadingDetector:
    """Turn style, numbering, font size and weight into one heading level.

    Zero means "not a heading". Style evidence wins over visual evidence so a
    correctly styled document keeps its declared hierarchy.
    """

    def __init__(self, body_size: float = 0.0, size_levels=None, bold_level: int = 2):
        self.body_size = body_size or 0.0
        self.size_levels = list(size_levels or [])
        self.bold_level = bold_level

    def _size_level(self, size) -> int:
        if not size or not self.size_levels:
            return 0
        for index, threshold in enumerate(self.size_levels, start=1):
            if float(size) >= threshold - 0.25:
                return min(MAX_LEVEL, index)
        return 0

    def _larger_than_body(self, size) -> bool:
        return bool(size and self.body_size and float(size) >= self.body_size * 1.08)

    def detect(self, text, style_level: int = 0, size=None, bold: bool = False) -> int:
        text = (text or "").strip()
        if not text:
            return 0
        if style_level:
            return min(MAX_LEVEL, int(style_level))
        if not is_title_like(text):
            return 0
        numbered = numbering_level(text)
        size_level = self._size_level(size)
        if numbered:
            level, strength = numbered
            if strength == "strong":
                return min(MAX_LEVEL, level)
            if size_level or bold or self._larger_than_body(size):
                return min(MAX_LEVEL, level)
            return 0
        if size_level:
            return size_level
        # Bold at body size is used for emphasis as often as for titles, so it
        # only counts when the line is also visibly larger than body text.
        if bold and len(text) <= MAX_BOLD_TITLE_CHARS and self._larger_than_body(size):
            return min(MAX_LEVEL, self.bold_level)
        return 0


def normalize_running_text(text: str) -> str:
    """Digit-insensitive key so page numbers compare equal across pages."""
    stripped = (text or "").strip()
    return re.sub(r"\d+", "#", stripped).replace(" ", "").replace("\u3000", "")


def _in_margin(line, page_height: float) -> bool:
    bbox = line.get("bbox") or []
    if len(bbox) != 4 or not page_height:
        return False
    center = (bbox[1] + bbox[3]) / 2
    return center <= page_height * MARGIN_BAND or center >= page_height * (1 - MARGIN_BAND)


def running_line_indexes(pages) -> list[set[int]]:
    """Indexes of repeated header/footer lines, per page.

    Each page is a mapping with height and a lines list of text/bbox items.
    A line in the top or bottom band that repeats on most pages is furniture.
    """
    dropped = [set() for _ in pages]
    if len(pages) < 3:
        return dropped
    counts = Counter()
    for page in pages:
        seen = set()
        for line in page["lines"]:
            if not _in_margin(line, page.get("height", 0)):
                continue
            key = normalize_running_text(line.get("text", ""))
            if key and len(key) <= 80:
                seen.add(key)
        counts.update(seen)
    limit = max(MIN_TOC_LINES, math.ceil(len(pages) * RUNNING_LINE_RATIO))
    furniture = {key for key, count in counts.items() if count >= limit}
    if not furniture:
        return dropped
    for index, page in enumerate(pages):
        for position, line in enumerate(page["lines"]):
            if not _in_margin(line, page.get("height", 0)):
                continue
            if normalize_running_text(line.get("text", "")) in furniture:
                dropped[index].add(position)
    return dropped


_LEADER_CHARS = re.compile(r"[.…·](?:[\s\u3000]*[.…·]){1,}")


def _is_contents_page(lines, candidates) -> bool:
    """A contents page is dominated by entries that are clearly navigational.

    Price and parameter tables also end in numbers, so a page only counts when
    most of its lines are entries and the entries carry dot leaders or section
    numbering. This keeps a quote table from being thrown away as a contents
    page just because its rows end in amounts.
    """
    if len(candidates) < MIN_TOC_LINES:
        return False
    if len(candidates) * 10 < len(lines) * 6:
        return False
    entries = [lines[position].get("text", "") for position in sorted(candidates)]
    if any(_LEADER_CHARS.search(entry) for entry in entries):
        return True
    numbered = sum(1 for entry in entries if numbering_level(entry) is not None)
    return numbered * 2 >= len(entries)


def toc_line_indexes(pages, search_pages: int) -> list[set[int]]:
    """Indexes of table-of-contents lines inside the leading pages."""
    dropped = [set() for _ in pages]
    if not pages:
        return dropped
    window = max(1, min(len(pages), search_pages))
    for index in range(window):
        page = pages[index]
        candidates = {position for position, line in enumerate(page["lines"])
                      if is_toc_line(line.get("text", ""))}
        if _is_contents_page(page["lines"], candidates):
            dropped[index].update(candidates)
    return dropped
