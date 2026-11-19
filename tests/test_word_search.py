"""Word chunk continuity requires the same paragraph and table cell."""
import unittest
from copy import deepcopy
from uuid import uuid4

from kb.search_dedup import deduplicate


class WordSearchTests(unittest.TestCase):
    def rows(self):
        text = "售前产品技术方案功能说明。" * 80
        location = {"format": "docx", "paragraph": 3, "heading": "方案",
                    "table": 1, "row": 2, "cell": 1}
        identifier = uuid4()
        return [dict(document_id=identifier, kind="text", asset_name=None, ordinal=index,
                     location={**location, "char_start": start, "char_end": end},
                     text=text[start:end], score=0.03 - index * 0.001)
                for index, (start, end) in enumerate(((0, 700), (620, len(text))))], text

    def test_same_word_paragraph_overlap_merges_and_keeps_both_sources(self):
        rows, text = self.rows()
        groups = deduplicate(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].text, text)
        self.assertEqual(len(groups[0].members), 2)

    def test_different_or_missing_word_location_does_not_merge(self):
        original, _ = self.rows()
        for key, value in (("paragraph", 4), ("paragraph", None), ("table", 2),
                           ("row", 3), ("cell", 2), ("format", "markdown")):
            with self.subTest(key=key, value=value):
                rows = deepcopy(original)
                rows[1]["location"][key] = value
                self.assertEqual(len(deduplicate(rows)), 2)
