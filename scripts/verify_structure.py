"""Probe document structure and parent/child units on a throwaway database.

Run from the repository root: python scripts/verify_structure.py
The probe creates its own database, publishes a synthetic 标书, reads the tree
back through the same queries the API uses, then drops the database.
"""

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.shared import Pt
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.chunking import build_chunks
from kb.config import load_settings
from kb.database import Database
from kb.parsers import parse_document
from kb.service import tokenize
from kb.structure import build_units, from_rows, tree

PROBE_DATABASE = "kb_structure_probe"
DIMENSION = 4


def sample_document(path: Path) -> None:
    document = Document()
    contents = document.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
    document.add_paragraph("第一章 项目概况 1", style=contents)
    document.add_paragraph("第二章 技术方案 4", style=contents)

    document.add_heading("第一章 项目概况", 1)
    document.add_paragraph("本项目位于市区，主要建设内容包括前端设备与平台。")

    manual = document.add_paragraph()
    run = manual.add_run("1.1 项目背景")
    run.bold = True
    run.font.size = Pt(15)
    document.add_paragraph("项目背景说明：现有系统已运行五年，需要升级改造。")

    document.add_heading("第二章 技术方案", 1)
    document.add_paragraph("总体设计采用分层架构，前端接入、平台处理、应用展示。")

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "设备名称"
    table.cell(0, 1).text = "技术参数"
    table.cell(1, 0).text = "网络摄像机"
    table.cell(1, 1).text = "不低于400万像素，防护等级IP67"
    document.save(path)


def scratch_settings(settings):
    info = conninfo_to_dict(settings.database_url)
    with psycopg.connect(make_conninfo(**{**info, "dbname": "postgres"}),
                         autocommit=True) as connection:
        connection.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % PROBE_DATABASE)
        connection.execute('CREATE DATABASE "%s"' % PROBE_DATABASE)
    return replace(settings,
                   database_url=make_conninfo(**{**info, "dbname": PROBE_DATABASE}),
                   embedding_backend="http", embedding_base_url="http://127.0.0.1:9",
                   embedding_model="structure-probe", embedding_dimension=DIMENSION)


def drop_database(settings) -> None:
    info = conninfo_to_dict(settings.database_url)
    with psycopg.connect(make_conninfo(**{**info, "dbname": "postgres"}),
                         autocommit=True) as connection:
        connection.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % PROBE_DATABASE)


def chunks_for(blocks, block_units):
    """Publish with the same block packing the ingest worker uses."""
    chunks = []
    for chunk in build_chunks(blocks, [block.text for block in blocks], block_units):
        block = blocks[chunk["block_start"]]
        chunk["asset_name"] = None
        chunk["heading"] = " / ".join(block.heading_path)
        chunk["heading_level"] = block.heading_level or (len(block.heading_path) or None)
        chunk["embedding"] = [0.1, 0.2, 0.3, 0.4]
        chunk["tokens"] = " ".join(tokenize(chunk["text"]))
        chunks.append(chunk)
    return chunks


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="kb-structure-"))
    settings = load_settings()
    database = Database(scratch_settings(settings))
    try:
        database.initialize()
        source = root / "标书样例.docx"
        sample_document(source)
        blocks = parse_document(source, root / "assets")
        units, block_units = build_units(blocks)
        chunks = chunks_for(blocks, block_units)
        if not any(block.role == "heading" for block in blocks):
            print("解析结果中没有识别到任何标题", file=sys.stderr)
            return 1

        document_id, token = uuid4(), uuid4()
        with database.connect() as connection:
            connection.execute("""
                INSERT INTO kb002.documents
                    (id, filename, content_hash, source_path, status, claim_token, confirmed_at)
                VALUES (%s, %s, %s, %s, 'processing', %s, now())
            """, (document_id, source.name, "probe-" + document_id.hex,
                  "probe", token))
        database.publish(document_id, token, chunks, 0, units)

        rows = database.units(document_id)
        forest = tree(from_rows(rows))
        root_unit = forest[0]
        chapters = [node["title"] for node in root_unit["children"]]
        section = root_unit["children"][0]["children"][0]
        stored = sum(row["chunk_count"] for row in rows)
        hit = database.search([0.1, 0.2, 0.3, 0.4], "摄像机", 3)

        checks = {
            "标题识别": [block.text for block in blocks if block.role == "heading"],
            "一级章节": chapters,
            "二级章节": section["title"],
            "章-节父子关系": section["parent_key"] == root_unit["children"][0]["key"],
            "单元数": len(rows),
            "切片数": len(chunks),
            "单元切片合计": stored,
            "检索命中标题": [row["heading"] for row in hit[:2]],
        }
        assert chapters == ["第一章 项目概况", "第二章 技术方案"], chapters
        assert section["title"] == "1.1 项目背景", section
        assert section["parent_key"] == root_unit["children"][0]["key"]
        assert stored == len(chunks), (stored, len(chunks))
        assert hit and hit[0]["heading"], hit
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        print("结构树与父子单元校验通过")
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)
        drop_database(settings)


if __name__ == "__main__":
    raise SystemExit(main())
