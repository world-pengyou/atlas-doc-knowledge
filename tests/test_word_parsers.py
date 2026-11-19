import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE
from PIL import Image

from kb.parsers import parse_document


class WordParserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"

    def save(self, document, suffix=".docx"):
        path = self.root / ("售前方案" + suffix)
        document.save(path)
        return path

    def picture(self):
        path = self.root / "diagram.png"
        with Image.new("RGBA", (64, 32), (200, 30, 50, 255)) as image:
            image.save(path)
        return path

    def rewrite_package(self, path, changes=None, extra=None):
        with ZipFile(path) as archive:
            parts = {info.filename: archive.read(info) for info in archive.infolist()}
        parts.update(changes or {})
        parts.update(extra or {})
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, body in parts.items():
                archive.writestr(name, body)

    def test_body_order_heading_tables_and_real_locations(self):
        document = Document()
        document.add_heading("售前方案", 1)
        document.add_paragraph("产品介绍")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "产品"
        table.cell(0, 1).text = "价格"
        table.cell(1, 0).text = "基础版"
        table.cell(1, 1).text = "100元"
        document.add_heading("交付", 2)
        document.add_paragraph("交付周期为两周")
        blocks = parse_document(self.save(document), self.assets)
        self.assertEqual([block.text for block in blocks],
                         ["售前方案", "产品介绍", "产品", "价格", "基础版", "100元", "交付", "交付周期为两周"])
        self.assertEqual(blocks[4].location, {"format": "docx", "table": 1, "row": 2,
                                            "cell": 1, "paragraph": 5, "heading": "售前方案"})
        self.assertEqual(blocks[-1].location["heading"], "售前方案 / 交付")
        self.assertTrue(all("page" not in block.location for block in blocks))

    def test_nested_and_merged_cells_are_not_duplicated(self):
        document = Document()
        table = document.add_table(rows=1, cols=2)
        cell = table.cell(0, 0).merge(table.cell(0, 1))
        cell.text = "合并单元格"
        nested = cell.add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "嵌套条款"
        blocks = parse_document(self.save(document), self.assets)
        self.assertEqual([block.text for block in blocks], ["合并单元格", "嵌套条款"])
        self.assertEqual([block.location["table"] for block in blocks], [1, 2])

    def test_embedded_images_in_body_and_table_keep_occurrence_locations(self):
        image = self.picture()
        document = Document()
        paragraph = document.add_paragraph("流程图说明")
        paragraph.add_run().add_picture(str(image))
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "表格中的流程"
        cell.paragraphs[0].add_run().add_picture(str(image))
        blocks = parse_document(self.save(document), self.assets)
        figures = [block for block in blocks if block.kind == "image"]
        self.assertEqual(len(figures), 2)
        self.assertEqual([block.location["image"] for block in figures], [1, 2])
        self.assertEqual(figures[1].location["table"], 1)
        self.assertEqual(figures[1].location["cell"], 1)
        self.assertEqual(figures[0].asset_path, figures[1].asset_path)
        self.assertIn("流程图说明", figures[0].text)
        with Image.open(figures[0].asset_path) as normalized:
            self.assertEqual(normalized.format, "PNG")
            self.assertEqual(normalized.mode, "RGB")
            self.assertEqual(normalized.getpixel((32, 16)), (200, 30, 50))
        self.assertEqual(len(list(self.assets.glob("*.png"))), 1)

    def test_hyperlink_text_is_preserved_without_fetching(self):
        document = Document()
        paragraph = document.add_paragraph("方案：")
        relationship = document.part.relate_to("https://example.invalid/reference",
                                               RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), relationship)
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "产品手册"
        run.append(text)
        hyperlink.append(run)
        paragraph._p.append(hyperlink)
        blocks = parse_document(self.save(document), self.assets)
        self.assertEqual([block.text for block in blocks], ["方案：产品手册"])

    def test_linked_image_is_rejected(self):
        document = Document()
        picture = document.add_picture(str(self.picture()))
        blip = picture._inline.xpath(".//a:blip")[0]
        blip.attrib.pop(qn("r:embed"))
        blip.set(qn("r:link"), document.part.relate_to("https://example.invalid/image.png",
                                                    RELATIONSHIP_TYPE.IMAGE, is_external=True))
        with self.assertRaisesRegex(ValueError, "不能引用外部链接"):
            parse_document(self.save(document), self.assets)
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_external_relationship_disguised_as_embedded_is_rejected(self):
        document = Document()
        picture = document.add_picture(str(self.picture()))
        picture._inline.xpath(".//a:blip")[0].set(
            qn("r:embed"), document.part.relate_to("https://example.invalid/image.png",
                                                 RELATIONSHIP_TYPE.IMAGE, is_external=True))
        with self.assertRaisesRegex(ValueError, "不能引用外部链接"):
            parse_document(self.save(document), self.assets)

    def test_missing_image_reference_fails_clearly(self):
        document = Document()
        picture = document.add_picture(str(self.picture()))
        picture._inline.xpath(".//a:blip")[0].set(qn("r:embed"), "rIdMissing")
        with self.assertRaisesRegex(ValueError, "图片引用缺失"):
            parse_document(self.save(document), self.assets)

    def test_legacy_doc_and_invalid_package_fail_clearly(self):
        legacy = self.root / "legacy.doc"
        legacy.write_bytes(b"not a docx")
        with self.assertRaisesRegex(ValueError, "旧版 .doc"):
            parse_document(legacy, self.assets)
        invalid = self.root / "invalid.docx"
        invalid.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ValueError, "并非有效的 .docx"):
            parse_document(invalid, self.assets)

    def test_package_traversal_and_expansion_are_rejected_before_document_load(self):
        path = self.save(Document())
        self.rewrite_package(path, extra={"../escape.xml": b"<x/>"})
        with self.assertRaisesRegex(ValueError, "内部路径"):
            parse_document(path, self.assets)
        self.assertFalse((self.root / "escape.xml").exists())
        path = self.save(Document())
        with patch("kb.parsers.MAX_DOCX_UNCOMPRESSED_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "解压后总大小超限"):
                parse_document(path, self.assets)

    def test_doctype_and_oversized_xml_are_rejected(self):
        path = self.save(Document())
        self.rewrite_package(path, extra={"word/custom.xml": b"<!DOCTYPE x><x/>"})
        with self.assertRaisesRegex(ValueError, "文档类型声明"):
            parse_document(path, self.assets)
        path = self.save(Document())
        with patch("kb.parsers.MAX_DOCX_XML_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "XML 部件大小超限"):
                parse_document(path, self.assets)

    def test_image_pixel_and_occurrence_limits(self):
        document = Document()
        document.add_picture(str(self.picture()))
        path = self.save(document)
        with patch("kb.parsers.MAX_IMAGE_PIXELS", 10):
            with self.assertRaisesRegex(ValueError, "Word 图片像素超限"):
                parse_document(path, self.assets)
        with patch("kb.parsers.MAX_VISUALS", 0):
            with self.assertRaisesRegex(ValueError, "图片数量超限"):
                parse_document(path, self.assets)

    def test_unrenderable_drawing_requires_pdf(self):
        document = Document()
        document.add_paragraph()._p.append(OxmlElement("w:drawing"))
        with self.assertRaisesRegex(ValueError, "请转为 PDF"):
            parse_document(self.save(document), self.assets)

    def test_unresolved_text_revisions_are_rejected_before_image_extraction(self):
        for revision_type in ("ins", "del", "moveFrom", "moveTo"):
            with self.subTest(revision_type=revision_type):
                document = Document()
                document.add_picture(str(self.picture()))
                paragraph = document.add_paragraph("原条款")
                revision = OxmlElement("w:" + revision_type)
                run = OxmlElement("w:r")
                text = OxmlElement("w:delText" if revision_type == "del" else "w:t")
                text.text = "未确认修订条款"
                run.append(text)
                revision.append(run)
                paragraph._p.append(revision)
                with self.assertRaisesRegex(ValueError, "先接受或拒绝全部修订"):
                    parse_document(self.save(document), self.assets)
                self.assertEqual(list(self.assets.iterdir()), [])

    def test_unresolved_revision_in_table_is_also_rejected(self):
        document = Document()
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "原报价"
        insertion = OxmlElement("w:ins")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "未确认新报价"
        run.append(text)
        insertion.append(run)
        cell.paragraphs[0]._p.append(insertion)
        with self.assertRaisesRegex(ValueError, "Word 正文含未处理修订"):
            parse_document(self.save(document), self.assets)


if __name__ == "__main__":
    unittest.main()
