import unittest

from kb.chunking import build_chunks, split_text
from kb.types import ParsedBlock


def block(text, location=None, kind="text", role="body", heading_level=0):
    return ParsedBlock(text, kind, location or {"format": "docx", "paragraph": 1},
                       role=role, heading_level=heading_level)


def cells(values, table=1, row=1):
    return [block(value, {"format": "docx", "table": table, "row": row,
                          "cell": index + 1, "paragraph": index + 1})
            for index, value in enumerate(values)]


def build(blocks, keys=None):
    keys = keys or ["root"] * len(blocks)
    return build_chunks(blocks, [item.text for item in blocks], keys)


class RowTests(unittest.TestCase):
    def test_a_table_row_keeps_its_cells_together(self):
        blocks = (cells(["适用对象", "软件销售、售前、解决方案顾问"]) +
                  cells(["核心目标", "听得懂技术 → 讲得清方案"], row=2))
        chunks = build(blocks)
        self.assertEqual(chunks[0]["text"], "适用对象：软件销售、售前、解决方案顾问\n"
                                            "核心目标：听得懂技术 → 讲得清方案")
        self.assertEqual((chunks[0]["block_start"], chunks[0]["block_end"]), (0, 3))
        self.assertEqual(chunks[0]["location"]["table"], 1)

    def test_a_row_is_not_split_when_it_still_fits(self):
        blocks = cells(["设备名称", "网络摄像机", "防护等级 IP67"])
        chunks = build(blocks)
        self.assertEqual(len(chunks), 1)
        self.assertIn("设备名称：网络摄像机：防护等级 IP67", chunks[0]["text"])

    def test_a_row_longer_than_a_chunk_is_split(self):
        blocks = cells(["参数", "一" * 400, "二" * 400])
        chunks = build(blocks)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk["text"]) <= 700 for chunk in chunks))

    def test_packing_stops_at_a_table_boundary(self):
        blocks = cells(["适用对象", "软件销售"]) + [block("表后的正文段落")]
        chunks = build(blocks)
        self.assertEqual(chunks[0]["text"], "适用对象：软件销售")
        self.assertEqual(chunks[1]["text"], "表后的正文段落")


class PackingTests(unittest.TestCase):
    def test_short_paragraphs_are_packed_within_one_section(self):
        blocks = [block("第一段很短。"), block("第二段也很短。"), block("第三段。")]
        chunks = build(blocks)
        self.assertEqual(chunks[0]["text"], "第一段很短。\n第二段也很短。\n第三段。")
        self.assertEqual(len(chunks[0]["text"]), 19)

    def test_packing_does_not_cross_a_section_boundary(self):
        blocks = [block("第一章 项目概况", role="heading", heading_level=1), block("正文一。"),
                  block("第二章 技术方案", role="heading", heading_level=1), block("正文二。")]
        chunks = build(blocks, ["chapter-1", "chapter-1", "chapter-2", "chapter-2"])
        self.assertEqual([chunk["text"] for chunk in chunks],
                         ["第一章 项目概况\n正文一。", "第二章 技术方案\n正文二。"])

    def test_chunks_never_exceed_the_limit(self):
        blocks = [block("句子。" * 300)]
        chunks = build(blocks)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk["text"]) <= 700 for chunk in chunks))
        self.assertEqual(chunks[0]["location"]["char_start"], 0)
        self.assertEqual(chunks[-1]["location"]["char_end"], 900)

    def test_a_section_title_is_kept_with_the_table_it_introduces(self):
        blocks = ([block("1.1 概念二维图", role="heading", heading_level=2)] +
                  cells(["大模型", "开放 / 闭源"]))
        chunks = build(blocks, ["section", "section", "section"])
        self.assertTrue(chunks[0]["text"].startswith("1.1 概念二维图\n"))
        self.assertIn("大模型：开放 / 闭源", chunks[0]["text"])

    def test_markdown_line_ranges_follow_each_split(self):
        text = "\n".join("第%d行内容" % number for number in range(200))
        blocks = [block(text, {"line_start": 10, "line_end": 209, "heading": "过程"})]
        chunks = build(blocks)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["location"]["line_start"], 10)
        for chunk in chunks[1:]:
            self.assertGreater(chunk["location"]["line_start"], 10)
            self.assertGreaterEqual(chunk["location"]["line_end"], chunk["location"]["line_start"])

    def test_a_packed_chunk_records_the_paragraph_range(self):
        blocks = [block("第一段。", {"format": "docx", "paragraph": 7}),
                  block("第二段。", {"format": "docx", "paragraph": 8})]
        chunks = build(blocks)
        self.assertEqual(chunks[0]["location"]["paragraph"], 7)
        self.assertEqual(chunks[0]["location"]["paragraph_end"], 8)
        self.assertEqual((chunks[0]["location"]["char_start"],
                          chunks[0]["location"]["char_end"]), (0, 9))

    def test_packing_stops_at_a_page_boundary(self):
        blocks = [block("第一页正文。", {"page": 1, "page_width": 100, "page_height": 100,
                                        "bbox": [1, 2, 3, 4]}),
                  block("第二页正文。", {"page": 2, "page_width": 100, "page_height": 100,
                                        "bbox": [1, 2, 3, 4]})]
        chunks = build(blocks)
        self.assertEqual([chunk["location"]["page"] for chunk in chunks], [1, 2])

    def test_images_keep_their_own_chunks(self):
        blocks = [block("图片说明", {"format": "docx", "image": 1, "paragraph": 3}, "image"),
                  block("图片二说明", {"format": "docx", "image": 2, "paragraph": 4}, "image")]
        chunks = build(blocks)
        self.assertEqual([chunk["kind"] for chunk in chunks], ["image", "image"])
        self.assertEqual([chunk["text"] for chunk in chunks], ["图片说明", "图片二说明"])
        self.assertEqual(chunks[0]["location"]["image"], 1)


class WhitespaceTests(unittest.TestCase):
    def test_split_text_keeps_sentences_whole_when_possible(self):
        parts = [part for part, _, _ in split_text("句子。" * 300)]
        self.assertTrue(all(part.endswith("。") for part in parts))
        self.assertTrue(all(len(part) <= 700 for part in parts))


if __name__ == "__main__":
    unittest.main()
