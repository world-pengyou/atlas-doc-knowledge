"""Extract searchable text and visual evidence into a per-document asset directory."""

import math
import hashlib
import re
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
    raise ValueError("仅支持 PDF、Markdown（.md、.markdown）和 Word（.docx）文件；旧版 .doc 请另存为 .docx")


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


def _parse_docx(path: Path, asset_dir: Path) -> list[ParsedBlock]:
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
        style_id = paragraph.style.style_id if paragraph.style is not None else ""
        heading = re.fullmatch(r"Heading([1-9])", style_id or "")
        if heading and "table" not in location:
            level = int(heading.group(1))
            for key in list(headings):
                if key >= level:
                    del headings[key]
            if text:
                headings[level] = text
        loc = {"format": "docx", **location, "paragraph": paragraph_number,
               "heading": " / ".join(headings.values())}
        if text:
            blocks.append(ParsedBlock(text, "text", loc))
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
                                                         "preview": asset.name}, str(asset)))

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
        for page_number, page in enumerate(doc, start=1):
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
            for item in page.get_text("blocks", sort=True):
                if item[6] != 0 or not item[4].strip():
                    continue
                bbox = (pymupdf.Rect(item[:4]) * page.rotation_matrix) & frame
                if not bbox.is_empty:
                    blocks.append(ParsedBlock(item[4].strip(), "text", {**loc, "bbox": list(bbox)}))
                    page_text.append(item[4].strip())
            context = "\n".join(page_text)[:1800]

            # All coordinates refer to the displayed page, including PDF rotation.
            # A dense diagram may have much more detail than its page preview.
            seen = set()
            page_visual = bool(page.get_drawings()) or (not page_text and not images)
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
        if index and tokens[index - 1].type == "heading_open":
            level = int(tokens[index - 1].tag[1:])
            headings = {k: v for k, v in headings.items() if k < level}
            if text:
                headings[level] = text
            location["heading"] = " / ".join(headings.values())
        if text:
            blocks.append(ParsedBlock(text, "text", location))
        for alt, line, end_line, asset in figures:
            context = "\n".join(dict.fromkeys(part for part in
                                (location["heading"][:300], alt[:400], text[:1000]) if part))
            image_location = {**location, "line_start": line, "line_end": end_line,
                              "preview": asset.name}
            blocks.append(ParsedBlock(context, "image", image_location, str(asset)))
    return blocks
