"""Extract searchable text and visual evidence into a per-document asset directory."""

import math
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile

import fitz as pymupdf
from docx import Document
from docx.exceptions import InvalidXmlError
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from markdown_it import MarkdownIt
from markdown_it.rules_inline.image import image as parse_inline_image
from PIL import Image, ImageOps, UnidentifiedImageError

from .headings import (HeadingDetector, body_font_size, running_line_indexes,
                       title_sizes, toc_line_indexes)
from .types import ParsedBlock


MAX_PAGES = 200
MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_PREVIEW_EDGE = 2048
MAX_VISUALS = 500
MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_DOCX_PARTS = 10_000
MAX_DOCX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_DOCX_XML_BYTES = 16 * 1024 * 1024
MAX_DOCX_TABLE_DEPTH = 20


def parse_document(path: Path, asset_dir: Path) -> list[ParsedBlock]:
    """asset_dir must be private to this import attempt; the caller owns its cleanup."""
    asset_dir = asset_dir.resolve()
    asset_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        return _parse_pdf(path, asset_dir)
    if path.suffix.lower() in {".md", ".markdown"}:
        return _parse_markdown(path, asset_dir)
    if path.suffix.lower() == ".docx":
        return _parse_docx(path, asset_dir)
    if path.suffix.lower() == ".doc":
        return _parse_doc(path, asset_dir)
    raise ValueError("仅支持 PDF、Markdown（.md、.markdown）和 Word（.doc、.docx）文件")


def _validate_docx_package(path: Path) -> None:
    """Bound package expansion before python-docx loads parts; never extract it."""
    try:
        with ZipFile(path) as package:
            parts = package.infolist()
            if len(parts) > MAX_DOCX_PARTS:
                raise ValueError("Word 文件内部部件数量超限，请拆分文件")
            total = 0
            names = set()
            for part in parts:
                name = part.filename
                relative = PurePosixPath(name)
                if (not name or "\\" in name or relative.is_absolute()
                        or ".." in relative.parts or PureWindowsPath(name).anchor
                        or any(":" in item for item in relative.parts) or name in names):
                    raise ValueError("Word 文件内部路径无效或重复")
                names.add(name)
                if part.flag_bits & 1:
                    raise ValueError("请提供未加密的 Word .docx 文件")
                total += part.file_size
                if total > MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise ValueError("Word 文件解压后总大小超限，请拆分文件")
                if name.lower().endswith((".xml", ".rels")):
                    if part.file_size > MAX_DOCX_XML_BYTES:
                        raise ValueError("Word XML 部件大小超限，请拆分文件")
                    if b"<!DOCTYPE" in package.read(part).upper():
                        raise ValueError("Word 文件不允许包含 XML 文档类型声明")
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                raise ValueError("请提供有效的 Word .docx 文件")
    except BadZipFile:
        raise ValueError("Word 文件损坏或并非有效的 .docx 文件") from None


_STYLE_HEADING = re.compile(r"^(?:heading|标题|h)\s*([1-9])$", re.IGNORECASE)
_TOC_STYLE = re.compile(r"^(?:toc|目录)\s*([1-9])?$", re.IGNORECASE)
_DEFAULT_WORD_SIZE = 10.5


def _effective_bold(value, style) -> bool:
    if value is not None:
        return bool(value)
    while style is not None:
        font = getattr(style, "font", None)
        if font is not None and font.bold is not None:
            return bool(font.bold)
        style = getattr(style, "base_style", None)
    return False


def _paragraph_font_size(paragraph) -> float:
    for run in paragraph.runs:
        if run.text.strip():
            size = run.font.size
            if size is not None:
                return float(size.pt)
    style = paragraph.style
    while style is not None:
        size = getattr(getattr(style, "font", None), "size", None)
        if size is not None:
            return float(size.pt)
        style = getattr(style, "base_style", None)
    return _DEFAULT_WORD_SIZE


def _paragraph_bold(paragraph) -> bool:
    runs = [run for run in paragraph.runs if run.text.strip()]
    if runs:
        return all(_effective_bold(run.font.bold, paragraph.style) for run in runs)
    return _effective_bold(None, paragraph.style)


def _heading_style_level(paragraph) -> int:
    """Built-in Word heading styles, including the Chinese localised names."""
    style = paragraph.style
    if style is None:
        return 0
    for candidate in (style.style_id or "", (style.name or "").strip()):
        matched = re.fullmatch(r"Heading([1-9])", candidate)
        if matched:
            return int(matched.group(1))
        matched = _STYLE_HEADING.match(candidate.replace(" ", ""))
        if matched:
            return int(matched.group(1))
    if (style.name or "").strip().lower() in {"title", "标题"}:
        return 1
    return 0


def _is_toc_paragraph(paragraph) -> bool:
    """Word marks contents entries by style, or by links to _Toc bookmarks."""
    style = paragraph.style
    if style is not None:
        for candidate in (style.style_id or "", (style.name or "").strip()):
            if _TOC_STYLE.match(candidate):
                return True
    anchors = paragraph._p.xpath(".//w:hyperlink/@w:anchor")
    if not any(str(anchor).startswith("_Toc") for anchor in anchors):
        return False
    text = paragraph.text.strip()
    # Cross references in the body also point at _Toc bookmarks; a contents
    # entry is short and ends with its page number.
    return bool(text) and len(text) <= 100 and text[-1].isdigit()


def _docx_text_metrics(document):
    """Character-weighted body font size plus all observed sizes."""
    weighted = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            weighted.append((_paragraph_font_size(paragraph), len(text)))
    body_size = body_font_size(weighted) or _DEFAULT_WORD_SIZE
    return body_size, [size for size, _ in weighted]


def _parse_doc(path: Path, asset_dir: Path) -> list[ParsedBlock]:
    """Convert a legacy binary .doc to .docx, then reuse the Word pipeline."""
    with tempfile.TemporaryDirectory(prefix="kb-doc-",
                                     ignore_cleanup_errors=True) as directory:
        converted = _convert_doc_to_docx(path, Path(directory))
        blocks = _parse_docx(converted, asset_dir, fmt="doc")
        if not blocks:
            raise ValueError("旧版 .doc 转换后没有可解析内容，可能已损坏或受密码保护")
        return blocks


def _convert_doc_to_docx(source: Path, target_dir: Path) -> Path:
    """Convert .doc with LibreOffice when present, otherwise with Word COM."""
    target = target_dir / (source.stem + ".docx")
    failures = []
    program = _find_soffice()
    if program:
        try:
            _convert_with_soffice(program, source, target_dir)
            if target.is_file() and target.stat().st_size > 0:
                return target
            failures.append("LibreOffice 未生成 .docx")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            failures.append(f"LibreOffice 转换失败：{error}")
    if os.name == "nt":
        try:
            if _convert_with_word(source, target):
                return target
            failures.append("未检测到可用的 Microsoft Word")
        except Exception as error:  # COM raises dynamic pywin32 errors
            failures.append(f"Microsoft Word 转换失败：{error}")
    detail = "；".join(failures) or "未找到 LibreOffice 或 Microsoft Word"
    raise ValueError(
        "无法解析旧版 .doc：" + detail
        + "。请安装 Microsoft Word / LibreOffice，或把文件另存为 .docx 后上传。"
    )


def _find_soffice() -> Path | None:
    candidates = []
    configured = os.environ.get("SOFFICE_PATH", "").strip()
    if configured:
        candidates.append(Path(configured))
    for name in ("soffice.exe", "soffice"):
        located = shutil.which(name)
        if located:
            candidates.append(Path(located))
    candidates.extend([
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ])
    return next((item for item in candidates if item.is_file()), None)


def _convert_with_soffice(program: Path, source: Path, target_dir: Path) -> None:
    # A private profile keeps conversions out of the desktop session and lets
    # several conversions run without waiting on the shared profile lock.
    profile = target_dir / "soffice-profile"
    profile.mkdir(parents=True, exist_ok=True)
    command = [str(program), "--headless", "--nologo", "--norestore",
               "--convert-to", "docx", "--outdir", str(target_dir),
               f"-env:UserInstallation={profile.as_uri()}", str(source)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(command, capture_output=True, timeout=180,
                            creationflags=flags)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise ValueError(message[:300] or f"退出码 {result.returncode}")
    # LibreOffice drops a read-only "~lock" beside the converted file and may
    # still hold it briefly; clear it so the temporary directory can go away.
    for _ in range(10):
        locks = list(target_dir.glob(".~lock.*"))
        if not locks:
            break
        for lock in locks:
            try:
                lock.unlink()
            except OSError:
                pass
        if list(target_dir.glob(".~lock.*")):
            time.sleep(0.2)


def _convert_with_word(source: Path, target: Path) -> bool:
    try:
        import pythoncom
        import win32com
    except ImportError:
        return False
    # pywin32 caches generated COM wrappers under %TEMP%; point it at a
    # writable workspace cache first so restricted service tokens still work.
    cache = Path(tempfile.gettempdir()) / "kb-word-com"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = target.parent / ".word-com-cache"
        cache.mkdir(parents=True, exist_ok=True)
    win32com.__gen_path__ = str(cache)
    from win32com import client
    pythoncom.CoInitialize()
    word = None
    try:
        word = client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3  # msoAutomationSecurityForceDisable
        except Exception:
            pass
        document = word.Documents.Open(str(source), False, True, False)
        try:
            saver = getattr(document, "SaveAs2", None) or document.SaveAs
            saver(str(target), 16)  # wdFormatDocumentDefault -> .docx
        finally:
            document.Close(0)
        return target.is_file() and target.stat().st_size > 0
    except Exception:
        raise
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _parse_docx(path: Path, asset_dir: Path, fmt: str = "docx") -> list[ParsedBlock]:
    # Reuses EduRAG edu_docloader.py's ordered paragraph/table traversal and
    # relationship-based embedded-image lookup. Image interpretation is left to
    # the existing preview/vision pipeline instead of starting a second OCR stack.
    _validate_docx_package(path)
    try:
        document = Document(path)
    except (BadZipFile, KeyError, InvalidXmlError, XMLSyntaxError, ValueError):
        raise ValueError("Word 文件结构无效，请另存为未加密的 .docx 后重试") from None
    if document.element.body.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo"):
        # python-docx omits revised text while image traversal can still see it.
        # Require one explicit source version rather than mixing revision states.
        raise ValueError("Word 正文含未处理修订，请先接受或拒绝全部修订，另存为 .docx 后再上传")
    body_size, sizes = _docx_text_metrics(document)
    detector = HeadingDetector(body_size, title_sizes(sizes, body_size))
    blocks = []
    headings = {}
    paragraph_number = 0
    table_number = 0
    image_number = 0
    assets = {}
    saved_bytes = 0

    def paragraph_blocks(paragraph, location):
        nonlocal paragraph_number, image_number, saved_bytes
        paragraph_number += 1
        text = paragraph.text.strip()
        in_table = "table" in location
        if not in_table and _is_toc_paragraph(paragraph):
            return
        level = 0
        if not in_table:
            # Style first, then numbering, then font size and weight: most
            # 标书 headings carry manual bold/large formatting only.
            level = detector.detect(text, style_level=_heading_style_level(paragraph),
                                    size=_paragraph_font_size(paragraph),
                                    bold=_paragraph_bold(paragraph))
        if level:
            for key in list(headings):
                if key >= level:
                    del headings[key]
            if text:
                headings[level] = text
        loc = {"format": fmt, **location, "paragraph": paragraph_number,
               "heading": " / ".join(headings.values())}
        if text:
            blocks.append(ParsedBlock(text, "text", loc, heading_path=tuple(headings.values()),
                                      heading_level=level,
                                      role="heading" if level else "body"))
        # Both modern DrawingML and legacy VML embedded pictures are supported.
        image_tags = {qn("a:blip"), "{urn:schemas-microsoft-com:vml}imagedata"}
        pictures = [element for element in paragraph._p.iter() if element.tag in image_tags]
        drawings = paragraph._p.xpath(".//w:drawing | .//w:pict")
        if (any(not any(element.tag in image_tags for element in drawing.iter()) for drawing in drawings)
                or paragraph._p.xpath(".//w:object | .//w:altChunk | .//w:txbxContent")):
            raise ValueError("Word 含暂不支持的图表、绘图或嵌入对象，请转为 PDF 后上传")
        for picture in pictures:
            image_number += 1
            if image_number > MAX_VISUALS:
                raise ValueError("文档图片数量超限，请拆分文件")
            if picture.get(qn("r:link")) or picture.get("src"):
                raise ValueError("Word 图片须嵌入文档，不能引用外部链接")
            relationship_id = picture.get(qn("r:embed")) or picture.get(qn("r:id"))
            relation = document.part.rels.get(relationship_id)
            if relation is None:
                raise ValueError("Word 图片引用缺失，请重新嵌入图片后上传")
            if relation.is_external:
                raise ValueError("Word 图片须嵌入文档，不能引用外部链接")
            part = relation.target_part
            if not part.content_type.startswith("image/"):
                raise ValueError("Word 图片引用未指向图片部件")
            blob = part.blob
            if len(blob) > MAX_IMAGE_BYTES:
                raise ValueError("Word 图片超过 16 MiB")
            digest = hashlib.sha256(blob).hexdigest()
            if digest not in assets:
                asset = asset_dir / f"word-image-{len(assets) + 1}.png"
                _save_word_image(blob, asset)
                saved_bytes += asset.stat().st_size
                if saved_bytes > MAX_ASSET_BYTES:
                    raise ValueError("Word 图片总大小超限，请拆分文件")
                assets[digest] = asset
            asset = assets[digest]
            context = "\n".join(dict.fromkeys(value for value in
                                             (loc["heading"][:300], text[:1000]) if value))
            blocks.append(ParsedBlock(context, "image", {**loc, "image": image_number,
                                                         "preview": asset.name}, str(asset),
                                      heading_path=tuple(headings.values())))

    def visit(parent, location, depth=0):
        nonlocal table_number
        element = parent._tc if isinstance(parent, _Cell) else parent.element.body
        for child in element.iterchildren():
            if child.tag == qn("w:p"):
                paragraph_blocks(Paragraph(child, parent), location)
            elif child.tag == qn("w:tbl"):
                if depth >= MAX_DOCX_TABLE_DEPTH:
                    raise ValueError("Word 表格嵌套层数超限")
                table_number += 1
                current_table = table_number
                table = Table(child, parent)
                seen_cells = set()
                for row_number, row in enumerate(table.rows, 1):
                    for cell_number, cell in enumerate(row.cells, 1):
                        # Merged cells may appear several times in row.cells.
                        if cell._tc in seen_cells:
                            continue
                        seen_cells.add(cell._tc)
                        visit(cell, {"table": current_table, "row": row_number,
                                     "cell": cell_number}, depth + 1)
            elif child.tag in {qn("w:sdt"), qn("w:customXml"), qn("w:altChunk")}:
                # Silently omitting a content control or imported document would
                # produce a misleading preview, so require a flattened source.
                raise ValueError("Word 含暂不支持的内容控件或导入对象，请转为普通正文或 PDF 后上传")

    visit(document, {})
    return blocks


def _save_word_image(blob: bytes, asset: Path) -> None:
    try:
        with Image.open(BytesIO(blob), formats=("PNG", "JPEG", "WEBP", "BMP", "TIFF", "GIF")) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Word 图片像素超限")
            if getattr(image, "is_animated", False):
                raise ValueError("请将 Word 动图转换为所需帧的静态图片")
            upright = ImageOps.exif_transpose(image)
            upright.thumbnail((MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
            rgba = upright.convert("RGBA")
            with Image.new("RGB", rgba.size, "white") as output:
                output.paste(rgba, mask=rgba.getchannel("A"))
                output.save(asset, "PNG")
    except Image.DecompressionBombError:
        raise ValueError("Word 图片像素超限") from None
    except (UnidentifiedImageError, OSError):
        raise ValueError("Word 图片损坏或格式暂不支持，请将图片转为 PNG/JPEG，或将文档转为 PDF") from None


def _pdf_bold(font_name) -> bool:
    name = (font_name or "").lower()
    return any(marker in name for marker in
               ("bold", "black", "heavy", "semibold", "demi", "medium"))


def _pdf_page_text(page, frame):
    """Text lines grouped by source block, keeping font size and weight."""
    data = page.get_text("dict", sort=True)
    groups = []
    flat = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = (pymupdf.Rect(line.get("bbox")) * page.rotation_matrix) & frame
            if bbox.is_empty:
                continue
            characters = sum(len(span.get("text", "")) for span in spans) or 1
            bold_characters = sum(len(span.get("text", "")) for span in spans
                                  if span.get("flags", 0) & 16
                                  or _pdf_bold(span.get("font", "")))
            sizes = [float(span.get("size") or 0) for span in spans]
            record = {"text": text, "bbox": list(bbox),
                      "size": max(sizes) if sizes else 0.0,
                      "bold": bold_characters * 2 >= characters,
                      "index": len(flat)}
            flat.append(record)
            lines.append(record)
        if lines:
            groups.append(lines)
    return {"height": frame.height, "lines": flat, "groups": groups}


def _flush_pdf_block(result, buffer):
    if not buffer:
        return
    text = "\n".join(line["text"] for line in buffer)
    result.append({
        "text": text,
        "bbox": [min(line["bbox"][0] for line in buffer),
                 min(line["bbox"][1] for line in buffer),
                 max(line["bbox"][2] for line in buffer),
                 max(line["bbox"][3] for line in buffer)],
        "size": max(line["size"] for line in buffer),
        "bold": all(line["bold"] for line in buffer),
    })
    buffer.clear()


def _pdf_page_blocks(groups, dropped, detector):
    """Split a source block wherever a title line starts a new section.

    PDF blocks often merge a heading with the paragraph below it; giving the
    title its own block keeps the heading out of the body chunk.
    """
    result = []
    for lines in groups:
        buffer = []
        for line in lines:
            if line["index"] in dropped:
                continue
            level = detector.detect(line["text"], size=line["size"], bold=line["bold"])
            if buffer and level:
                _flush_pdf_block(result, buffer)
            buffer.append(line)
            if level:
                _flush_pdf_block(result, buffer)
        _flush_pdf_block(result, buffer)
    return result


def _parse_pdf(path: Path, asset_dir: Path) -> list[ParsedBlock]:
    # Adapted from EduRAG's rag_qa/edu_document_loaders/edu_pdfloader.py:
    # enumerate pages, extract native text and inspect displayed image occurrences.
    blocks = []
    visual_count = 0
    saved_bytes = 0
    with pymupdf.open(path) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError("请提供可直接读取的未加密 PDF")
        if not 1 <= doc.page_count <= MAX_PAGES:
            raise ValueError(f"PDF 页数须在 1 到 {MAX_PAGES} 页之间")
        # One text pass up front: body font size, repeated page furniture and
        # contents lines all depend on the whole document, not a single page.
        pages = [_pdf_page_text(page, page.rect) for page in doc]
        running = running_line_indexes(pages)
        contents = toc_line_indexes(pages, max(5, doc.page_count // 5))
        sizes = [line["size"] for page in pages for line in page["lines"]]
        body_size = body_font_size([(line["size"], len(line["text"]))
                                    for page in pages for line in page["lines"]])
        detector = HeadingDetector(body_size, title_sizes(sizes, body_size))
        headings = {}
        for page_number, page in enumerate(doc, start=1):
            dropped = running[page_number - 1] | contents[page_number - 1]
            frame = page.rect
            if frame.is_empty or frame.is_infinite:
                raise ValueError(f"PDF 第 {page_number} 页的尺寸无效")
            images = page.get_image_info()
            if any(img["width"] * img["height"] > MAX_IMAGE_PIXELS for img in images):
                raise ValueError(f"PDF 第 {page_number} 页图片像素超限")
            preview = asset_dir / f"page-{page_number}.png"
            scale = min(2.5, MAX_PREVIEW_EDGE / max(frame.width, frame.height))
            display = page.get_displaylist()
            pixmap = display.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                        colorspace=pymupdf.csRGB, alpha=False)
            pixmap.save(str(preview))
            saved_bytes += preview.stat().st_size
            loc = {"page": page_number, "page_width": frame.width, "page_height": frame.height,
                   "preview": preview.name}
            page_text = []
            for block in _pdf_page_blocks(pages[page_number - 1]["groups"], dropped, detector):
                level = detector.detect(block["text"], size=block["size"], bold=block["bold"])
                if level:
                    for key in list(headings):
                        if key >= level:
                            del headings[key]
                    headings[level] = block["text"].strip()
                blocks.append(ParsedBlock(block["text"], "text", {**loc, "bbox": block["bbox"]},
                                          heading_path=tuple(headings.values()),
                                          heading_level=level,
                                          role="heading" if level else "body"))
                page_text.append(block["text"])
            context = "\n".join(page_text)[:1800]

            # All coordinates refer to the displayed page, including PDF rotation.
            # A dense diagram may have much more detail than its page preview.
            seen = set()
            # A page whose text was all page furniture or contents still is not a
            # scanned page: sending it to the vision model would waste money.
            page_visual = bool(page.get_drawings()) or (
                not pages[page_number - 1]["lines"] and not images)
            with Image.open(preview) as rendered:
                for img in images:
                    bbox = (pymupdf.Rect(img["bbox"]) * page.rotation_matrix) & frame
                    if bbox.is_empty:
                        continue
                    pixels = (
                        max(0, math.floor((bbox.x0 - frame.x0) * rendered.width / frame.width)),
                        max(0, math.floor((bbox.y0 - frame.y0) * rendered.height / frame.height)),
                        min(rendered.width, math.ceil((bbox.x1 - frame.x0) * rendered.width / frame.width)),
                        min(rendered.height, math.ceil((bbox.y1 - frame.y0) * rendered.height / frame.height)),
                    )
                    if pixels in seen:
                        continue
                    seen.add(pixels)
                    if bbox.get_area() >= frame.get_area() * 0.98:
                        page_visual = True
                        continue
                    visual_count += 1
                    if visual_count > MAX_VISUALS:
                        raise ValueError("文档图片数量超限，请拆分文件")
                    asset = asset_dir / f"page-{page_number}-image-{len(seen)}.png"
                    source_scale = min(max(img["width"], img["height"]), MAX_PREVIEW_EDGE) / max(bbox.width, bbox.height)
                    if source_scale > scale:
                        detail = display.get_pixmap(matrix=pymupdf.Matrix(source_scale, source_scale),
                                                    clip=bbox, colorspace=pymupdf.csRGB, alpha=False)
                        detail.save(str(asset))
                    else:
                        with rendered.crop(pixels) as cropped:
                            cropped.save(asset)
                    saved_bytes += asset.stat().st_size
                    if saved_bytes > MAX_ASSET_BYTES:
                        raise ValueError("PDF 预览图总大小超限，请拆分文件")
                    blocks.append(ParsedBlock(context, "image", {**loc, "bbox": list(bbox)}, str(asset)))
                if page_visual and (page_text or images or rendered.getextrema() != ((255, 255),) * 3):
                    visual_count += 1
                    if visual_count > MAX_VISUALS:
                        raise ValueError("文档图片数量超限，请拆分文件")
                    blocks.append(ParsedBlock(context, "image", {**loc, "bbox": list(frame)}, str(preview)))
            if saved_bytes > MAX_ASSET_BYTES:
                raise ValueError("PDF 预览图总大小超限，请拆分文件")
    return blocks


def _relative_image(root: Path, src: str) -> Path:
    url = urlsplit(unquote(src).replace("\\", "/"))
    rel = PureWindowsPath(url.path)
    if (url.scheme or url.netloc or url.query or url.fragment or not rel.parts
            or rel.anchor or ".." in rel.parts
            or any(":" in part or PureWindowsPath(part).is_reserved() for part in rel.parts)):
        raise ValueError("Markdown 图片须使用随文上传的相对路径")
    image = root.joinpath(*rel.parts).resolve()
    if not image.is_relative_to(root):
        raise ValueError("Markdown 图片超出文档目录")
    if not image.is_file():
        raise ValueError(f"缺少 Markdown 图片附件：{rel.as_posix()}")
    return image


def _save_markdown_image(source: Path, asset: Path) -> None:
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Markdown 图片超过 16 MiB")
    with Image.open(source, formats=("PNG", "JPEG", "WEBP")) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Markdown 图片像素超限")
        if getattr(image, "is_animated", False):
            raise ValueError("请将动态图转换为所需帧的静态图片")
        upright = ImageOps.exif_transpose(image)
        upright.thumbnail((MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
        rgba = upright.convert("RGBA")
        output = Image.new("RGB", rgba.size, "white")
        output.paste(rgba, mask=rgba.getchannel("A"))
        output.save(asset, "PNG")


def _image_with_offsets(state, silent):
    """The CommonMark image rule resolves references but omits the source position."""
    start = state.pos
    matched = parse_inline_image(state, silent)
    if matched and not silent:
        state.tokens[-1].meta["offsets"] = (start, state.pos)
    return matched


def _parse_markdown(path: Path, asset_dir: Path) -> list[ParsedBlock]:
    body = path.read_text(encoding="utf-8-sig")
    parser = MarkdownIt("commonmark", {"html": False})
    parser.inline.ruler.at("image", _image_with_offsets)
    tokens = parser.parse(body)
    root = path.parent.resolve()
    headings = {}
    assets = {}
    saved_bytes = 0
    blocks = []
    for index, token in enumerate(tokens):
        if token.type not in {"inline", "fence", "code_block"} or token.map is None:
            continue
        first, last = token.map
        location = {"line_start": first + 1, "line_end": last,
                    "heading": " / ".join(headings.values())}
        if token.type in {"fence", "code_block"}:
            if token.content.strip():
                content = token.content.removesuffix("\n")
                start_line = first + (2 if token.type == "fence" else 1)
                location.update(line_start=start_line, line_end=start_line + content.count("\n"),
                                language=token.info.strip())
                blocks.append(ParsedBlock(content, "text", location))
            continue

        parts = []
        figures = []
        current_line = first + 1
        previous_end = 0
        for child in token.children or []:
            if child.type == "image":
                src = str(child.attrGet("src") or "")
                source = _relative_image(root, src)
                if source not in assets:
                    if len(assets) >= MAX_VISUALS:
                        raise ValueError("文档图片数量超限，请拆分文件")
                    asset = asset_dir / f"md-image-{len(assets) + 1}.png"
                    _save_markdown_image(source, asset)
                    saved_bytes += asset.stat().st_size
                    if saved_bytes > MAX_ASSET_BYTES:
                        raise ValueError("Markdown 图片总大小超限，请拆分文件")
                    assets[source] = asset
                start, end = child.meta["offsets"]
                current_line += token.content.count("\n", previous_end, start)
                end_line = current_line + token.content.count("\n", start, end)
                figures.append((child.content.strip(), current_line, end_line, assets[source]))
                current_line = end_line
                previous_end = end
            elif child.type in {"softbreak", "hardbreak"}:
                parts.append("\n")
            elif child.type in {"text", "code_inline"}:
                parts.append(child.content)

        rendered = "".join(parts)
        # Markdown can fold a multiline code span or link into a single line.
        # Use the original inline source when folding would break line citations.
        if rendered.strip() and rendered.count("\n") != token.content.count("\n"):
            rendered = token.content
        text = rendered.strip()
        leading = rendered[: len(rendered) - len(rendered.lstrip())]
        location["line_start"] += leading.count("\n")
        location["line_end"] = location["line_start"] + text.count("\n")
        level = 0
        if index and tokens[index - 1].type == "heading_open":
            level = int(tokens[index - 1].tag[1:])
            headings = {k: v for k, v in headings.items() if k < level}
            if text:
                headings[level] = text
            location["heading"] = " / ".join(headings.values())
        if text:
            blocks.append(ParsedBlock(text, "text", location, heading_path=tuple(headings.values()),
                                      heading_level=level,
                                      role="heading" if level else "body"))
        for alt, line, end_line, asset in figures:
            context = "\n".join(dict.fromkeys(part for part in
                                (location["heading"][:300], alt[:400], text[:1000]) if part))
            image_location = {**location, "line_start": line, "line_end": end_line,
                              "preview": asset.name}
            blocks.append(ParsedBlock(context, "image", image_location, str(asset),
                                      heading_path=tuple(headings.values())))
    return blocks
