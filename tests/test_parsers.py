import tempfile
import unittest
from pathlib import Path

import fitz as pymupdf
from PIL import Image

from kb.parsers import parse_document


class ParserTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.assets = self.root / "assets"

    def image(self, size=64):
        image_dir = self.root / "figures"
        image_dir.mkdir(exist_ok=True)
        path = image_dir / "diagram.png"
        with Image.new("RGB", (size, size), (200, 30, 50)) as image:
            image.save(path)
        return path

    def parse_md(self, body, suffix=".md"):
        path = self.root / f"notes{suffix}"
        path.write_text(body, encoding="utf-8-sig")
        return parse_document(path, self.assets)

    def test_rotated_pdf_embedded_image_matches_source_preview(self):
        image = self.image(size=320)
        pdf = self.root / "figure.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=180)
            page.insert_text((10, 20), "Flow diagram")
            page.insert_image(pymupdf.Rect(30, 40, 90, 100), filename=str(image))
            page.set_rotation(90)
            doc.save(pdf)
        blocks = parse_document(pdf, self.assets)
        self.assertTrue(any("Flow diagram" in b.text for b in blocks if b.kind == "text"))
        figure, = [b for b in blocks if b.kind == "image"]
        self.assertIn("Flow diagram", figure.text)
        self.assertEqual(figure.location["page"], 1)
        self.assertEqual(figure.location["page_width"], 180)
        self.assertEqual(figure.location["bbox"], [80, 30, 140, 90])
        with Image.open(figure.asset_path) as crop:
            self.assertGreaterEqual(crop.width, 320)
            self.assertEqual(crop.getpixel((crop.width // 2, crop.height // 2)), (200, 30, 50))
        self.assertTrue((self.assets / figure.location["preview"]).is_file())

    def test_vector_flowchart_without_embedded_images(self):
        pdf = self.root / "workflow.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=180)
            page.draw_rect(pymupdf.Rect(15, 20, 70, 45))
            page.draw_line((70, 32), (110, 32))
            page.draw_rect(pymupdf.Rect(110, 20, 180, 45))
            page.insert_text((20, 38), "Start")
            page.insert_text((115, 38), "Finish")
            doc.save(pdf)
        blocks = parse_document(pdf, self.assets)
        figure, = [b for b in blocks if b.kind == "image"]
        self.assertEqual(figure.location["bbox"], [0, 0, 240, 180])
        self.assertEqual(Path(figure.asset_path), self.assets / "page-1.png")
        self.assertIn("Start", figure.text)
        self.assertIn("Finish", figure.text)

    def test_scanned_page_produces_one_page_image(self):
        image = self.image()
        pdf = self.root / "scan.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page(width=100, height=100)
            page.insert_image(page.rect, filename=str(image))
            doc.save(pdf)
        block, = parse_document(pdf, self.assets)
        self.assertEqual(block.kind, "image")
        self.assertEqual(Path(block.asset_path).name, "page-1.png")

    def test_markdown_headings_relative_image_and_source_lines(self):
        self.image()
        body = "# 课程\n\n## 流程\n\n第一步\n![节点关系][fig]\n第二步\n\n[fig]: figures/diagram.png\n"
        blocks = self.parse_md(body, suffix=".markdown")
        figure, = [b for b in blocks if b.kind == "image"]
        self.assertEqual(figure.location["heading"], "课程 / 流程")
        self.assertEqual((figure.location["line_start"], figure.location["line_end"]), (6, 6))
        self.assertTrue(Path(figure.asset_path).is_file())
        for word in ["节点关系", "课程", "第二步"]:
            self.assertIn(word, figure.text)
        body_block, = [b for b in blocks if b.kind == "text" and "第一步" in b.text]
        self.assertEqual((body_block.location["line_start"], body_block.location["line_end"]), (5, 7))

    def test_mermaid_fence_content_lines_with_and_without_closing_fence(self):
        source = "# 流程图\n\n~~~mermaid\nflowchart LR\n  A --> B"
        for ending in ("\n~~~\n", ""):
            with self.subTest(ending=ending):
                blocks = self.parse_md(source + ending)
                fence, = [b for b in blocks if b.location.get("language") == "mermaid"]
                self.assertEqual(fence.kind, "text")
                self.assertEqual(fence.text, "flowchart LR\n  A --> B")
                self.assertEqual((fence.location["line_start"], fence.location["line_end"]), (4, 5))

    def test_markdown_missing_and_disallowed_image_paths_fail(self):
        for src in ("../outside.png", "%2E%2E/outside.png", "/outside.png",
                    "C:/outside.png", "https://example.invalid/photo.png", "//example.invalid/p.png"):
            with self.subTest(src=src):
                with self.assertRaisesRegex(ValueError, "相对路径"):
                    self.parse_md(f"![图]({src})")
        with self.assertRaisesRegex(ValueError, "缺少 Markdown 图片附件"):
            self.parse_md("![图](missing.png)")
