import unittest

from kb.structure import build_units, tree, unit_key
from kb.types import ParsedBlock


def heading(text, level, page=None, path=()):
    location = {"page": page} if page else {}
    return ParsedBlock(text, "text", location, heading_path=tuple(path), heading_level=level,
                       role="heading")


def body(text, page=None):
    location = {"page": page} if page else {}
    return ParsedBlock(text, "text", location)


class StructureTreeTests(unittest.TestCase):
    def sample(self):
        return [
            body("前言说明"),
            heading("第一章 项目概况", 1, page=1),
            body("项目背景", page=1),
            heading("1.1 项目目标", 2, page=1),
            body("目标说明", page=2),
            heading("第二章 技术方案", 1, page=3),
            body("总体设计", page=3),
        ]

    def test_units_follow_the_heading_hierarchy(self):
        units, _ = build_units(self.sample())
        self.assertEqual([unit["title"] for unit in units],
                         ["", "第一章 项目概况", "1.1 项目目标", "第二章 技术方案"])
        self.assertEqual([unit["level"] for unit in units], [0, 1, 2, 1])
        self.assertEqual(units[1]["parent_key"], "root")
        self.assertEqual(units[2]["parent_key"], units[1]["key"])
        self.assertEqual(units[3]["parent_key"], "root")

    def test_every_block_maps_to_its_nearest_unit(self):
        units, keys = build_units(self.sample())
        self.assertEqual(keys, ["root", units[1]["key"], units[1]["key"],
                                units[2]["key"], units[2]["key"],
                                units[3]["key"], units[3]["key"]])

    def test_block_and_page_ranges_cover_owned_content(self):
        units, _ = build_units(self.sample())
        root, chapter, section, second = units
        self.assertEqual((root["block_start"], root["block_end"]), (0, 6))
        self.assertEqual((chapter["block_start"], chapter["block_end"]), (1, 4))
        self.assertEqual((section["block_start"], section["block_end"]), (3, 4))
        self.assertEqual((second["block_start"], second["block_end"]), (5, 6))
        self.assertEqual((chapter["page_start"], chapter["page_end"]), (1, 2))
        self.assertEqual((section["page_start"], section["page_end"]), (1, 2))
        self.assertEqual((second["page_start"], second["page_end"]), (3, 3))
        self.assertEqual((root["page_start"], root["page_end"]), (1, 3))

    def test_keys_are_stable_across_reparses_and_unique(self):
        first, _ = build_units(self.sample())
        second, _ = build_units(self.sample())
        self.assertEqual([unit["key"] for unit in first], [unit["key"] for unit in second])
        self.assertEqual(len({unit["key"] for unit in first}), len(first))
        self.assertNotEqual(unit_key(["A"], 1), unit_key(["A"], 2))

    def test_skipped_heading_levels_attach_to_the_nearest_open_unit(self):
        units, keys = build_units([
            heading("第一章 项目概况", 1),
            heading("1.1.2 建设范围", 3),
            body("范围说明"),
        ])
        self.assertEqual(len(units), 3)
        self.assertEqual(units[2]["parent_key"], units[1]["key"])
        self.assertEqual(keys[2], units[2]["key"])

    def test_a_document_without_headings_is_one_root_unit(self):
        units, keys = build_units([body("第一段"), body("第二段")])
        self.assertEqual(len(units), 1)
        self.assertEqual(keys, ["root", "root"])

    def test_toc_blocks_do_not_create_units(self):
        toc = ParsedBlock("目录", "text", {}, role="toc")
        units, keys = build_units([toc, heading("第一章 项目概况", 1), body("正文")])
        self.assertEqual([unit["title"] for unit in units], ["", "第一章 项目概况"])
        self.assertEqual(keys, [None, units[1]["key"], units[1]["key"]])

    def test_tree_nests_children_under_their_parent(self):
        units, _ = build_units(self.sample())
        roots = tree(units)
        self.assertEqual(len(roots), 1)
        chapter = roots[0]["children"]
        self.assertEqual([node["title"] for node in chapter],
                         ["第一章 项目概况", "第二章 技术方案"])
        self.assertEqual([node["title"] for node in chapter[0]["children"]], ["1.1 项目目标"])


if __name__ == "__main__":
    unittest.main()
