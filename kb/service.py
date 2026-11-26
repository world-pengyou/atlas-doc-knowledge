import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote

import jieba
import psycopg

from kb.chunking import build_chunks, split_text
from kb.models import ModelClient, ModelError
from kb.parsers import parse_document
from kb.search_dedup import deduplicate
from kb.errors import StorageCheckError, StoragePathError
from kb.storage import clean_deletion_files, deletion_paths
from kb.structure import build_units, from_rows, tree as unit_tree


logger = logging.getLogger(__name__)


def tokenize(text):
    """Reuse EduRAG mysql_qa/utils/preprocess.py's lowercase + jieba rules."""
    return [word for word in jieba.lcut(text.lower())
            if re.fullmatch(r"[\w-]+", word, flags=re.UNICODE)]


def query_tokens(text):
    # Escape lexemes using the tsquery grammar, then OR terms for coarse recall.
    words = dict.fromkeys(tokenize(text))
    return " | ".join("'" + word.replace("'", "''").replace("\\", "\\\\") + "'" for word in words)


class KnowledgeService:
    def __init__(self, settings, database):
        self.settings = settings
        self.database = database
        self.models = ModelClient(settings)

    def result(self, row):
        document_id = str(row["document_id"])
        base = self.settings.public_base_url
        location = dict(row["location"])
        if location.get("preview"):
            location["preview_url"] = f"{base}/api/documents/{document_id}/assets/{quote(location['preview'])}"
        source_url = f"{base}/api/documents/{document_id}/source"
        if location.get("page"):
            source_url += f"#page={location['page']}"
        asset_name = row.get("asset_name")
        return {
            "document_id": document_id, "filename": row["filename"],
            "text": row["text"], "kind": row["kind"], "location": location,
            "asset_url": f"{base}/api/documents/{document_id}/assets/{quote(asset_name)}" if asset_name else None,
            "source_url": source_url, "score": row.get("score"),
            "similarity": row.get("similarity"),
            "channels": row.get("channels", []),
            "heading": row.get("heading") or "",
            "heading_level": row.get("heading_level"),
            "unit_id": str(row["unit_id"]) if row.get("unit_id") else None,
        }

    def search(self, query, limit=None):
        query = query.strip()
        if limit is None:
            limit = self.settings.search_top_k
        if not query or len(query) > 500 or not 1 <= limit <= 20:
            raise ValueError("查询需包含 1 到 500 个字符，limit 需在 1 到 20 之间")
        vector = self.models.embed([query])[0]
        tokens = query_tokens(query)
        candidate_limit = max(20, limit * 3)
        threshold = self.settings.search_similarity_threshold
        while True:
            rows = self.database.search(vector, tokens, candidate_limit)
            accepted = [row for row in rows if row["similarity"] >= threshold]
            groups = deduplicate(accepted)
            removed = len(accepted) - len(groups)
            if (len(groups) >= limit or not removed or len(rows) < candidate_limit
                    or candidate_limit >= 200):
                break
            candidate_limit = min(200, candidate_limit * 3)
        return {"query": query, "similarity_threshold": threshold, "limit": limit,
                "deduplicated_count": removed, "candidate_limit": candidate_limit,
                "results": [self._search_result(group) for group in groups[:limit]]}

    def _search_result(self, group):
        result = self.result(group.representative)
        result["text"] = group.text
        result["merged_chunk_count"] = len(group.members)
        result["sources"] = []
        for row in group.members:
            source = self.result(row)
            del source["text"]
            if "id" in row:
                source["chunk_id"] = row["id"]
            if "ordinal" in row:
                source["ordinal"] = row["ordinal"]
            result["sources"].append(source)
        return result

    REPORT_STYLES = {
        "report": "把资料整合成一份结构化报告：先给出结论摘要，再按主题分节展开，"
                  "最后列出风险、待确认事项与建议。",
        "summary": "把资料整合成要点摘要：给出 3 到 8 条要点，每条 1 到 2 句，按重要性排序。",
        "qa": "直接回答提出的问题：先给结论，再给依据与细节，最后说明资料尚未覆盖的部分。",
    }
    REPORT_SYSTEM_PROMPT = (
        "你是企业知识库的资料整合助手，只能依据给出的编号资料作答，不得引入资料之外的事实。\n"
        "1. 每个结论后面用 [编号] 标注依据，可以同时标注多个编号。\n"
        "2. 不同资料冲突时并列说明冲突，不要自行取舍。\n"
        "3. 资料没有覆盖的内容写“资料未覆盖”，不要编造数字、日期、人名或承诺。\n"
        "4. 使用简体中文输出 Markdown 正文，不要复述这些规则，也不要写“以下是”之类的开场白。"
    )
    MAX_REPORT_CONTEXT = 12_000
    MAX_REPORT_CHUNK = 1_500
    MAX_SOURCE_SNIPPET = 500

    def report(self, query, style="report", top_k=8):
        """Retrieve then integrate: a model report, or an extractive digest."""
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("查询需包含 1 到 500 个字符")
        if style not in self.REPORT_STYLES:
            raise ValueError("生成风格仅支持 report、summary 或 qa")
        if not 1 <= top_k <= 12:
            raise ValueError("top_k 需在 1 到 12 之间")
        results = self.search(query, limit=top_k)["results"]
        base = {"query": query, "style": style, "sources": [], "model": None,
                "usage": {}, "note": None}
        if not results:
            return {**base, "mode": "empty",
                    "report": "知识库中没有检索到与该主题相关的内容。可以更换说法，或先上传资料。"}
        sources = [self._report_source(number, result)
                   for number, result in enumerate(results, 1)]
        if self._chat_configured():
            try:
                text, usage = self.models.generate(
                    self.REPORT_SYSTEM_PROMPT, self._report_prompt(query, style, results))
                return {**base, "mode": "model", "sources": sources, "report": text,
                        "model": self.settings.chat_model, "usage": usage}
            except ModelError as error:
                logger.warning("Report generation failed (%s)", type(error).__name__)
                note = "生成模型调用失败（" + str(error) + "），已改为抽取式整合。"
        else:
            note = "未配置生成模型（CHAT_BASE_URL、CHAT_MODEL），当前为抽取式整合。"
        return {**base, "mode": "extractive", "sources": sources, "note": note,
                "report": self._extractive_report(results)}

    def _chat_configured(self):
        return bool(getattr(self.settings, "chat_base_url", "")
                    and getattr(self.settings, "chat_model", ""))

    def _report_source(self, number, result):
        text = " ".join(result["text"].split())
        if len(text) > self.MAX_SOURCE_SNIPPET:
            text = text[:self.MAX_SOURCE_SNIPPET] + "…"
        location = result.get("location") or {}
        return {"citation": number, "document_id": result["document_id"],
                "filename": result["filename"], "heading": result.get("heading") or "",
                "location": location, "location_label": self._location_label(location),
                "text": text, "source_url": result.get("source_url"),
                "similarity": result.get("similarity")}

    @staticmethod
    def _location_label(location):
        def span(first, last, single, ranged):
            if isinstance(first, int) and isinstance(last, int) and last != first:
                return ranged % (first, last)
            return single % first

        parts = []
        if location.get("format") in {"docx", "doc"}:
            table, row = location.get("table"), location.get("row")
            if isinstance(table, int) and isinstance(row, int):
                label = "表格 %d %s" % (
                    table, span(row, location.get("row_end"), "第 %d 行", "第 %d–%d 行"))
                cell = location.get("cell")
                if isinstance(cell, int):
                    label += " " + span(cell, location.get("cell_end"), "单元格 %d", "单元格 %d–%d")
                parts.append(label)
            elif isinstance(location.get("paragraph"), int):
                parts.append(span(location["paragraph"], location.get("paragraph_end"),
                                  "段落 %d", "段落 %d–%d"))
            if isinstance(location.get("image"), int):
                parts.append("图片 %d" % location["image"])
        page = location.get("page")
        if isinstance(page, int):
            parts.append(span(page, location.get("page_end"), "第 %d 页", "第 %d–%d 页"))
        first = location.get("line_start")
        if isinstance(first, int):
            parts.append(span(first, location.get("line_end"), "第 %d 行", "第 %d–%d 行"))
        return " · ".join(parts)

    def _report_prompt(self, query, style, results):
        blocks = []
        used = 0
        for number, result in enumerate(results, 1):
            text = " ".join(result["text"].split())
            if len(text) > self.MAX_REPORT_CHUNK:
                text = text[:self.MAX_REPORT_CHUNK] + "…"
            header = "[%d] 文档：%s" % (number, result["filename"])
            if result.get("heading"):
                header += "｜章节：" + result["heading"]
            label = self._location_label(result.get("location") or {})
            if label:
                header += "｜位置：" + label
            block = header + "\n" + text
            if used + len(block) > self.MAX_REPORT_CONTEXT:
                break
            blocks.append(block)
            used += len(block)
        return ("任务：" + self.REPORT_STYLES[style] + "\n主题或问题：" + query
                + "\n\n资料片段：\n" + "\n\n".join(blocks))

    def _extractive_report(self, results):
        lines = ["## 资料整合", "", "按检索结果摘录、归类原文要点，未做改写。", ""]
        grouped = {}
        for number, result in enumerate(results, 1):
            grouped.setdefault(result["document_id"], []).append((number, result))
        for items in grouped.values():
            lines.append("### 《" + items[0][1]["filename"] + "》")
            for number, result in items:
                heading = result.get("heading") or "正文"
                text = " ".join(result["text"].split())
                if len(text) > 240:
                    text = text[:240] + "…"
                lines.append("- **%s**：%s [%d]" % (heading, text, number))
            lines.append("")
        return "\n".join(lines).strip()

    def delete_document(self, document_id):
        def validate(source_path):
            try:
                deletion_paths(self.settings.data_dir, document_id, source_path)
            except OSError as error:
                raise StorageCheckError("文件检查失败，知识库记录尚未删除，请重试") from error

        job = self.database.delete_document(document_id, validate)
        result = {"id": str(document_id), "filename": job["filename"], "deleted": True,
                  "removed_chunks": job["removed_chunks"], "cleanup_pending": True,
                  "shared_uploads_retained": False}
        try:
            paths = deletion_paths(self.settings.data_dir, document_id, job["source_path"])
            # Read the marker BEFORE the database reads remaining references.
            # Its disappearance during that query must not permit stale-count
            # cleanup of a batch whose registration has just finished.
            uploading = (paths.batch / ".uploading").exists()

            def cleanup(plan, shared):
                complete = clean_deletion_files(self.settings.data_dir, document_id,
                                                plan["source_path"], shared or uploading)
                return complete and not uploading

            result.update(self.database.cleanup_deletion(
                document_id, cleanup))
        except (OSError, StoragePathError, psycopg.Error) as error:
            logger.warning("Document %s cleanup pending (%s)", document_id, type(error).__name__)
        return result

    def document_detail(self, document_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("分页范围无效")
        document = self.database.document(document_id)
        if not document:
            raise ValueError("文档不存在")
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT c.*, d.filename FROM kb002.chunks c
                JOIN kb002.documents d ON d.id = c.document_id
                WHERE d.id = %s AND d.status = 'ready'
                ORDER BY c.ordinal LIMIT %s OFFSET %s
            """, (document_id, limit, offset)).fetchall()
        return {"id": str(document["id"]), "filename": document["filename"],
                "status": document["status"], "error": document["error"],
                "chunk_count": document["chunk_count"], "offset": offset,
                "chunks": [self.result(row) for row in rows]}

    def ingest(self, document, stop):
        if document.get("confirmed_at") is not None:
            self._index_preview(document, stop)
        else:
            self._prepare_preview(document, stop)

    def preview_document(self, document_id, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("分页范围无效")
        document = self.database.document(document_id)
        if not document:
            raise ValueError("文档不存在")
        if document["status"] == "ready":
            result = self.document_detail(document_id, offset, limit)
            units = self.database.units(document_id)
        else:
            chunks, units = (self._read_preview(document)
                             if document.get("preview_path") else ([], []))
            rows = [{**chunk, "document_id": document_id, "filename": document["filename"]}
                    for chunk in chunks[offset:offset + limit]]
            result = {"id": str(document_id), "filename": document["filename"],
                      "status": document["status"], "error": document.get("error"),
                      "chunk_count": document["chunk_count"], "offset": offset,
                      "chunks": [self.result(row) for row in rows]}
        result.update({"image_count": document["image_count"],
                       "unit_count": len(units),
                       "preview_token": str(document["preview_token"]) if document.get("preview_token") else None,
                       "can_confirm": document["status"] == "preview" and document.get("confirmed_at") is None})
        return result

    def document_structure(self, document_id):
        document = self.database.document(document_id)
        if not document:
            raise ValueError("文档不存在")
        units = from_rows(self.database.units(document_id))
        return {"id": str(document_id), "filename": document["filename"],
                "status": document["status"], "unit_count": len(units),
                "units": unit_tree(units)}

    def _read_preview(self, document):
        preview_root = (self.settings.data_dir / "previews" / str(document["id"])).resolve()
        path = (self.settings.data_dir / (document.get("preview_path") or "")).resolve()
        token = str(document.get("preview_token"))
        if not path.is_relative_to(preview_root) or path.name != token + ".json":
            raise ValueError("切片预览路径无效")
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ValueError("切片预览文件无法读取") from None
        if (not isinstance(snapshot, dict) or snapshot.get("document_id") != str(document["id"])
                or snapshot.get("preview_token") != token):
            raise ValueError("切片预览版本与文档不一致")
        chunks = snapshot.get("chunks")
        if not isinstance(chunks, list) or len(chunks) != document["chunk_count"] or not chunks:
            raise ValueError("切片预览数量无效")
        for chunk in chunks:
            if (not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str)
                    or not chunk["text"].strip() or chunk.get("kind") not in {"text", "image"}
                    or not isinstance(chunk.get("location"), dict)
                    or "asset_name" not in chunk):
                raise ValueError("切片预览内容无效")
        units = snapshot.get("units") or []
        if not isinstance(units, list):
            raise ValueError("切片预览结构无效")
        keys = set()
        for unit in units:
            if (not isinstance(unit, dict) or not isinstance(unit.get("key"), str)
                    or not isinstance(unit.get("title"), str)
                    or not isinstance(unit.get("level"), int)
                    or not isinstance(unit.get("ordinal"), int)
                    or not isinstance(unit.get("block_start"), int)
                    or not isinstance(unit.get("block_end"), int)):
                raise ValueError("切片预览结构无效")
            keys.add(unit["key"])
        if any(chunk.get("unit_key") and chunk["unit_key"] not in keys for chunk in chunks):
            raise ValueError("切片预览结构单元缺失")
        return chunks, units

    def _prepare_preview(self, document, stop):
        document_id = str(document["id"])
        attempt_id = str(document["claim_token"])
        asset_dir = self.settings.data_dir / "assets" / document_id / attempt_id
        asset_dir.mkdir(parents=True, exist_ok=True)
        source = (self.settings.data_dir / document["source_path"]).resolve()
        if not source.is_relative_to(self.settings.data_dir):
            raise ValueError("文档来源路径无效")
        blocks = parse_document(source, asset_dir)
        units, block_units = build_units(blocks)
        descriptions = self._describe_images(blocks, asset_dir, stop)
        texts = []
        image_count = 0
        for index, block in enumerate(blocks):
            self._check_stop(stop)
            content = block.text
            if block.kind == "image":
                image_count += 1
                content = "\n".join(filter(None, [block.text, "图片模型解析\n" + descriptions[index]]))
            texts.append(content)
        chunks = []
        for chunk in build_chunks(blocks, texts, block_units):
            block = blocks[chunk["block_start"]]
            location = chunk["location"]
            if location.get("preview"):
                location["preview"] = attempt_id + "/" + location["preview"]
            asset_name = None
            if block.kind == "image":
                asset_name = attempt_id + "/" + Path(block.asset_path).name
            chunk["asset_name"] = asset_name
            chunk["heading"] = " / ".join(block.heading_path)
            chunk["heading_level"] = block.heading_level or (len(block.heading_path) or None)
            chunks.append(chunk)
        if not chunks:
            raise ValueError("文档没有可入库内容")
        preview_dir = self.settings.data_dir / "previews" / document_id
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_file = preview_dir / (attempt_id + ".json")
        temporary = preview_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"document_id": document_id, "preview_token": attempt_id,
                                         "chunks": chunks, "units": units},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, preview_file)
        self._check_stop(stop)
        self.database.save_preview(document_id, document["claim_token"],
                                   str(preview_file.relative_to(self.settings.data_dir)), len(chunks), image_count)

    def _describe_images(self, blocks, asset_dir, stop):
        descriptions = {}
        pending = []
        cache_dir = asset_dir.parent / "analysis-cache"
        for index, block in enumerate(blocks):
            if block.kind != "image":
                continue
            self._check_stop(stop)
            image = Path(block.asset_path)
            cache_key = hashlib.sha256(
                image.read_bytes() + self.settings.vision_base_url.encode()
                + self.settings.vision_model.encode() + block.text.encode() + b"prompt-batch-v2"
            ).hexdigest()
            cache_file = cache_dir / (cache_key + ".json")
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    value = cached.get("description") if isinstance(cached, dict) else None
                    if isinstance(value, str) and value.strip() and len(value) <= 24000:
                        descriptions[index] = value
                        continue
                except (ValueError, OSError):
                    pass
            pending.append((index, image, block.text[:1800], cache_file))
        for start in range(0, len(pending), ModelClient.VISION_BATCH_SIZE):
            self._check_stop(stop)
            batch = pending[start:start + ModelClient.VISION_BATCH_SIZE]
            values = self.models.describe_images([(image, context) for _, image, context, _ in batch])
            if len(values) != len(batch):
                raise ModelError("图片识别结果数量与本批输入不一致")
            cache_dir.mkdir(parents=True, exist_ok=True)
            for (index, _, _, cache_file), value in zip(batch, values):
                if not isinstance(value, str) or not value.strip():
                    raise ModelError("图片识别结果为空")
                descriptions[index] = value
                # Keep the scratch name short: without LongPathsEnabled, Windows
                # rejects paths of 260 characters and the cache key is already 64.
                temporary = cache_file.with_name(cache_file.name + "." + os.urandom(6).hex() + ".tmp")
                temporary.write_text(json.dumps({"description": value}, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, cache_file)
        return descriptions

    def _index_preview(self, document, stop):
        chunks, units = self._read_preview(document)
        for start in range(0, len(chunks), 16):
            self._check_stop(stop)
            batch = chunks[start:start + 16]
            vectors = self.models.embed([chunk["text"] for chunk in batch])
            for chunk, vector in zip(batch, vectors):
                chunk["embedding"] = vector
                chunk["tokens"] = " ".join(tokenize(chunk["text"]))
        self._check_stop(stop)
        self.database.publish(document["id"], document["claim_token"], chunks,
                              document["image_count"], units)

    @staticmethod
    def _check_stop(stop):
        if stop.is_set():
            raise InterruptedError("入库随服务停止，请点击重试继续")


class IngestWorker:
    """One owned worker per database, with persisted jobs and restart recovery."""

    def __init__(self, service):
        self.service = service
        self.stop_event = threading.Event()
        self.thread = None
        self.lock_connection = None
        self.error = None

    def start(self):
        self.lock_connection = psycopg.connect(self.service.settings.database_url,
                                              autocommit=True, connect_timeout=8)
        try:
            acquired = self.lock_connection.execute("SELECT pg_try_advisory_lock(2002002)").fetchone()[0]
            if not acquired:
                raise RuntimeError("本测试库已有入库进程，请使用单个 Web 服务实例")
            self.lock_connection.execute("""
                UPDATE kb002.documents SET status = 'pending', error = NULL,
                claim_token = NULL, updated_at = now() WHERE status = 'processing'
            """)
        except Exception:
            self.lock_connection.close()
            raise
        self.thread = threading.Thread(target=self._run, name="kb002-ingest", daemon=False)
        self.thread.start()

    def _run(self):
        try:
            while not self.stop_event.is_set():
                # Check ownership before each job. Lost connections stop this worker.
                self.lock_connection.execute("SELECT 1")
                document = self.service.database.claim()
                if document is None:
                    self.stop_event.wait(1)
                    continue
                try:
                    self.service.ingest(document, self.stop_event)
                except (ValueError, ModelError, InterruptedError) as error:
                    self.service.database.fail(document["id"], document["claim_token"], str(error))
                except Exception as error:
                    logger.error("Document %s failed (%s)", document["id"], type(error).__name__)
                    self.service.database.fail(document["id"], document["claim_token"], "解析或入库失败，错误类型 " + type(error).__name__)
        except Exception as error:
            self.error = "入库工作进程停止，错误类型 " + type(error).__name__
            logger.error(self.error)
        finally:
            self.lock_connection.close()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
