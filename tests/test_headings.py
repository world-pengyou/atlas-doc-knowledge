import unittest

from kb.headings import (HeadingDetector, body_font_size, is_title_like, is_toc_line,
                         normalize_running_text, numbering_level, running_line_indexes,
                         title_sizes, toc_line_indexes)


class NumberingTests(unittest.TestCase):
    def test_strong_schemes_report_their_own_level(self):
        cases = {
            "第一章 项目概况": 1, "第二篇 技术方案": 1, "附件 企业资质": 1,
            "附件2 产品证明": 1, "第三节 施工组织": 2, "第五条 付款方式": 3,
            "2.2.1 摄像机": 3, "2.2.1.4 安装要求": 4, "（三）商务条款": 3,
            "一、项目背景": 2,
        }
        for text, level in cases.items():
            with self.subTest(text=text):
                self.assertEqual(numbering_level(text), (level, "strong"))

    def test_single_level_numbers_are_weak(self):
        for text in ("1、材料进场", "2. 施工准备"):
            with self.subTest(text=text):
                self.assertEqual(numbering_level(text), (2, "weak"))

    def test_plain_text_has_no_numbering(self):
        for text in ("本项目采用先进技术。", "总体设计", ""):
            with self.subTest(text=text):
                self.assertIsNone(numbering_level(text))


class TitleTests(unittest.TestCase):
    def test_short_phrases_are_titles(self):
        self.assertTrue(is_title_like("2.2 视频监控系统"))

    def test_sentences_and_long_lines_are_not_titles(self):
        for text in ("本项目共建设两千台摄像机。", "要求如下：", "甲" * 61):
            with self.subTest(text=text[:10]):
                self.assertFalse(is_title_like(text))

    def test_multiline_block_is_not_a_title(self):
        self.assertFalse(is_title_like("第一章 项目概况\n项目位于市区。"))


class DetectorTests(unittest.TestCase):
    def detector(self, body_size=10.5, sizes=(18.0, 15.0)):
        return HeadingDetector(body_size, title_sizes(sizes, body_size))

    def test_style_level_wins_over_visual_evidence(self):
        self.assertEqual(self.detector().detect("随便一段话", style_level=3), 3)

    def test_strong_numbering_needs_no_formatting(self):
        detector = self.detector()
        self.assertEqual(detector.detect("2.2.1 摄像机"), 3)
        self.assertEqual(detector.detect("第一章 项目概况"), 1)

    def test_weak_numbering_needs_format_evidence(self):
        detector = self.detector()
        self.assertEqual(detector.detect("1、材料进场"), 0)
        self.assertEqual(detector.detect("1、材料进场", bold=True), 2)
        self.assertEqual(detector.detect("1、材料进场", size=15.0), 2)

    def test_font_size_rank_defines_levels(self):
        detector = self.detector()
        self.assertEqual(detector.detect("总体设计", size=18.0), 1)
        self.assertEqual(detector.detect("视频监控系统", size=15.0), 2)
        self.assertEqual(detector.detect("正文段落", size=10.5), 0)

    def test_large_sentences_are_not_titles(self):
        self.assertEqual(self.detector().detect("本项目共建设摄像机两千台。", size=18.0), 0)

    def test_bold_body_text_stays_body(self):
        self.assertEqual(self.detector().detect("重点提示", size=10.5, bold=True), 0)

    def test_missing_style_information_still_uses_numbering(self):
        self.assertEqual(HeadingDetector(0, []).detect("2.3 综合布线系统"), 2)


class ContentsTests(unittest.TestCase):
    def test_entries_carry_their_page_number(self):
        for text in ("2.2 视频监控系统 …… 12", "第一章 项目概况.......1", "技术要求    15"):
            with self.subTest(text=text):
                self.assertTrue(is_toc_line(text))

    def test_body_lines_are_not_entries(self):
        for text in ("2.2 视频监控系统", "本项目从整体上分为三个部分。"):
            with self.subTest(text=text):
                self.assertFalse(is_toc_line(text))

    def test_two_entries_are_not_a_contents_page(self):
        pages = [{"height": 100, "lines": [
            {"text": "第一章 项目概况 .... 1", "bbox": [0, 10, 50, 20]},
            {"text": "第二章 技术方案 .... 2", "bbox": [0, 30, 50, 40]}]}]
        self.assertEqual(toc_line_indexes(pages, 5), [set()])

    def test_amount_columns_are_not_a_contents_page(self):
        pages = [{"height": 100, "lines": [
            {"text": "网络摄像机    1200", "bbox": [0, 10, 50, 20]},
            {"text": "存储设备    800", "bbox": [0, 30, 50, 40]},
            {"text": "交换机    300", "bbox": [0, 50, 50, 60]}]}]
        self.assertEqual(toc_line_indexes(pages, 5), [set()])

    def test_leading_contents_page_is_dropped(self):
        pages = [{"height": 100, "lines": [
            {"text": "第一章 项目概况 .... 1", "bbox": [0, 10, 50, 20]},
            {"text": "第二章 技术方案 .... 2", "bbox": [0, 30, 50, 40]},
            {"text": "第三章 实施方案 .... 3", "bbox": [0, 50, 50, 60]}]},
            {"height": 100, "lines": [{"text": "正文", "bbox": [0, 10, 50, 20]}]}]
        dropped = toc_line_indexes(pages, 5)
        self.assertEqual(dropped[0], {0, 1, 2})
        self.assertEqual(dropped[1], set())


class FurnitureTests(unittest.TestCase):
    def page(self, header, body_text):
        return {"height": 100, "lines": [{"text": header, "bbox": [10, 5, 90, 9]},
                                         {"text": body_text, "bbox": [10, 50, 90, 60]}]}

    def test_repeated_header_and_footer_are_dropped(self):
        pages = [self.page("投标文件 第 %d 页" % number, "正文 %d" % number)
                 for number in range(1, 5)]
        dropped = running_line_indexes(pages)
        for index in range(4):
            self.assertEqual(dropped[index], {0})

    def test_unique_margin_line_survives(self):
        pages = [self.page("投标文件 第 %d 页" % number, "正文 %d" % number)
                 for number in range(1, 5)]
        pages[2]["lines"][0]["text"] = "唯一出现的页眉"
        self.assertNotIn(0, running_line_indexes(pages)[2])

    def test_single_page_document_keeps_every_line(self):
        self.assertEqual(running_line_indexes([self.page("页眉", "正文")]), [set()])

    def test_digits_are_normalised(self):
        self.assertEqual(normalize_running_text("第 12 页 / 共 99 页"),
                         normalize_running_text("第 1 页 / 共 9 页"))


class FontSizeTests(unittest.TestCase):
    def test_character_weighted_body_size(self):
        self.assertEqual(body_font_size([(10.5, 1000), (18.0, 20)]), 10.5)

    def test_title_sizes_are_ranked_and_filtered(self):
        self.assertEqual(title_sizes([18.0, 15.0, 10.5, 9.0], 10.5), [18.0, 15.0])
        self.assertEqual(title_sizes([10.5, 10.6], 10.5), [])

    def test_empty_input(self):
        self.assertEqual(body_font_size([]), 0.0)
        self.assertEqual(title_sizes([], 0), [])


if __name__ == "__main__":
    unittest.main()
