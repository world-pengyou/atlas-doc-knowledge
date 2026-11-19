import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.errors import DocumentConflict, DocumentNotFound


class Database:
    def __init__(self, settings):
        self.settings = settings

    @contextmanager
    def connect(self):
        with psycopg.connect(self.settings.database_url, row_factory=dict_row,
                             connect_timeout=8) as connection:
            connection.execute("SET statement_timeout = '30s'")
            yield connection

    def initialize(self):
        dimension = self.settings.embedding_dimension
        with self.connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(2002001)")
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            connection.execute("CREATE SCHEMA IF NOT EXISTS kb002")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb002.settings (
                    id integer PRIMARY KEY CHECK (id = 1),
                    embedding_fingerprint text NOT NULL,
                    embedding_dimension integer NOT NULL
                )
            """)
            connection.execute("""
                INSERT INTO kb002.settings VALUES (1, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (self.settings.embedding_fingerprint, dimension))
            self._check_model(connection)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb002.documents (
                    id uuid PRIMARY KEY,
                    filename text NOT NULL,
                    content_hash text UNIQUE NOT NULL,
                    source_path text NOT NULL,
                    status text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
                    claim_token uuid,
                    error text,
                    chunk_count integer NOT NULL DEFAULT 0,
                    image_count integer NOT NULL DEFAULT 0,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            connection.execute("""
                ALTER TABLE kb002.documents
                    ADD COLUMN IF NOT EXISTS preview_path text,
                    ADD COLUMN IF NOT EXISTS preview_token uuid,
                    ADD COLUMN IF NOT EXISTS confirmed_at timestamptz
            """)
            connection.execute("ALTER TABLE kb002.documents DROP CONSTRAINT IF EXISTS documents_status_check")
            connection.execute("""
                ALTER TABLE kb002.documents ADD CONSTRAINT documents_status_check
                    CHECK (status IN ('pending', 'processing', 'preview', 'ready', 'failed'))
            """)
            # dimension is a validated integer. Values and identifiers from uploads
            # never enter SQL source text.
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS kb002.chunks (
                    id bigserial PRIMARY KEY,
                    document_id uuid NOT NULL REFERENCES kb002.documents(id),
                    ordinal integer NOT NULL,
                    text text NOT NULL,
                    kind text NOT NULL CHECK (kind IN ('text', 'image')),
                    location jsonb NOT NULL,
                    asset_name text,
                    embedding vector({dimension}) NOT NULL,
                    search_vector tsvector NOT NULL,
                    UNIQUE (document_id, ordinal)
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS kb002_chunks_document ON kb002.chunks(document_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS kb002_chunks_text ON kb002.chunks USING gin(search_vector)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS kb002.deletions (
                    document_id uuid PRIMARY KEY,
                    filename text NOT NULL,
                    source_path text NOT NULL,
                    removed_chunks integer NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
            """)

    def _check_model(self, connection):
        row = connection.execute("SELECT * FROM kb002.settings WHERE id = 1").fetchone()
        if not row or row["embedding_fingerprint"] != self.settings.embedding_fingerprint or row["embedding_dimension"] != self.settings.embedding_dimension:
            raise ValueError("当前 embedding 模型、端点或维度与知识库记录不一致，请先制定重建索引方案")

    def check(self):
        with self.connect() as connection:
            self._check_model(connection)
            connection.execute("SELECT id, preview_path, preview_token, confirmed_at FROM kb002.documents LIMIT 0")
            connection.execute("SELECT id FROM kb002.chunks LIMIT 0")
            connection.execute("SELECT document_id, source_path FROM kb002.deletions LIMIT 0")

    def list_documents(self):
        with self.connect() as connection:
            return connection.execute("""
                SELECT * FROM (
                    SELECT id, filename, status, error, created_at, chunk_count, image_count,
                        preview_token, confirmed_at FROM kb002.documents
                    UNION ALL
                    SELECT document_id, filename, 'cleanup_pending',
                        '检索内容已删除，本地文件清理待重试', created_at, 0, 0,
                        NULL::uuid, NULL::timestamptz FROM kb002.deletions
                ) listed ORDER BY created_at DESC LIMIT 200
            """).fetchall()

    def document(self, document_id):
        with self.connect() as connection:
            return connection.execute("SELECT * FROM kb002.documents WHERE id = %s", (document_id,)).fetchone()

    def add_document(self, document_id, filename, digest, source_path):
        with self.connect() as connection:
            row = connection.execute("""
                INSERT INTO kb002.documents (id, filename, content_hash, source_path)
                VALUES (%s, %s, %s, %s) ON CONFLICT (content_hash) DO NOTHING
                RETURNING id, filename, status, error, true AS created
            """, (document_id, filename, digest, source_path)).fetchone()
            if row:
                return row
            return connection.execute("""
                SELECT id, filename, status, error, false AS created FROM kb002.documents
                WHERE content_hash = %s
            """, (digest,)).fetchone()

    def retry(self, document_id):
        with self.connect() as connection:
            return connection.execute("""
                UPDATE kb002.documents SET status = 'pending', error = NULL, updated_at = now()
                WHERE id = %s AND status = 'failed' RETURNING id, status
            """, (document_id,)).fetchone()

    def delete_document(self, document_id, validate_source):
        """Commit index removal and a durable cleanup plan in one transaction."""
        with self.connect() as connection:
            document = connection.execute("""
                SELECT * FROM kb002.documents WHERE id = %s FOR UPDATE
            """, (document_id,)).fetchone()
            if document is None:
                job = connection.execute("""
                    SELECT * FROM kb002.deletions WHERE document_id = %s FOR UPDATE
                """, (document_id,)).fetchone()
                if job is None:
                    raise DocumentNotFound("文档不存在或已完成删除")
                return job
            if document["status"] != "ready":
                raise DocumentConflict("仅支持删除已入库文档，处理中或待确认文档不能删除")
            validate_source(document["source_path"])
            removed = connection.execute("DELETE FROM kb002.chunks WHERE document_id = %s",
                                         (document_id,)).rowcount
            job = connection.execute("""
                INSERT INTO kb002.deletions (document_id, filename, source_path, removed_chunks)
                VALUES (%s, %s, %s, %s) RETURNING *
            """, (document_id, document["filename"], document["source_path"], removed)).fetchone()
            connection.execute("DELETE FROM kb002.documents WHERE id = %s", (document_id,))
            return job

    def cleanup_deletion(self, document_id, cleanup_callback):
        """Serialize shared-batch cleanup; a failed callback keeps the job."""
        with self.connect() as connection:
            job = connection.execute("""
                SELECT * FROM kb002.deletions WHERE document_id = %s FOR UPDATE
            """, (document_id,)).fetchone()
            if job is None:
                return {"cleanup_pending": False, "shared_uploads_retained": False}
            prefix = "/".join(job["source_path"].replace("\\", "/").split("/")[:2]) + "/"
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 2002003))", (prefix,))
            shared = connection.execute("""
                SELECT 1 FROM kb002.documents
                WHERE starts_with(replace(source_path, chr(92), '/'), %s) LIMIT 1
            """, (prefix,)).fetchone() is not None
            complete = cleanup_callback(job, shared)
            if complete:
                connection.execute("DELETE FROM kb002.deletions WHERE document_id = %s", (document_id,))
            return {"cleanup_pending": not complete, "shared_uploads_retained": shared}

    def save_preview(self, document_id, claim_token, preview_path, chunk_count, image_count):
        with self.connect() as connection:
            row = connection.execute("""
                UPDATE kb002.documents SET status = 'preview', preview_path = %s,
                    preview_token = %s, chunk_count = %s, image_count = %s,
                    error = NULL, claim_token = NULL, updated_at = now()
                WHERE id = %s AND claim_token = %s AND status = 'processing'
                    AND confirmed_at IS NULL RETURNING id
            """, (preview_path, claim_token, chunk_count, image_count, document_id, claim_token)).fetchone()
            if row is None:
                raise InterruptedError("任务所有权已变化，切片预览停止发布")

    def confirm(self, document_id, preview_token):
        with self.connect() as connection:
            row = connection.execute("""
                SELECT id, status, preview_token, confirmed_at FROM kb002.documents
                WHERE id = %s FOR UPDATE
            """, (document_id,)).fetchone()
            if not row or row["preview_token"] != preview_token:
                raise ValueError("切片预览版本已变化，请刷新后确认")
            if row["confirmed_at"] is not None and row["status"] in {"pending", "processing", "ready"}:
                return row
            if row["status"] != "preview":
                raise ValueError("文档切片尚未准备完成，或当前状态不能确认入库")
            return connection.execute("""
                UPDATE kb002.documents SET status = 'pending', confirmed_at = now(),
                    error = NULL, updated_at = now() WHERE id = %s
                RETURNING id, status, preview_token, confirmed_at
            """, (document_id,)).fetchone()

    def claim(self):
        with self.connect() as connection:
            return connection.execute("""
                UPDATE kb002.documents SET status = 'processing', claim_token = %s, updated_at = now()
                WHERE id = (SELECT id FROM kb002.documents WHERE status = 'pending'
                    ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *
            """, (uuid4(),)).fetchone()

    def fail(self, document_id, claim_token, message):
        with self.connect() as connection:
            connection.execute("""
                UPDATE kb002.documents SET status = 'failed', error = %s,
                    claim_token = NULL, updated_at = now()
                WHERE id = %s AND status = 'processing' AND claim_token = %s
            """, (message[:500], document_id, claim_token))

    def publish(self, document_id, claim_token, chunks, image_count):
        with self.connect() as connection:
            owner = connection.execute("""
                SELECT id FROM kb002.documents WHERE id = %s AND status = 'processing'
                    AND claim_token = %s AND confirmed_at IS NOT NULL FOR UPDATE
            """, (document_id, claim_token)).fetchone()
            if not owner:
                raise InterruptedError("任务所有权已变化，当前处理结果停止发布")
            connection.execute("DELETE FROM kb002.chunks WHERE document_id = %s", (document_id,))
            with connection.cursor() as cursor:
                cursor.executemany("""
                    INSERT INTO kb002.chunks
                        (document_id, ordinal, text, kind, location, asset_name, embedding, search_vector)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::vector, to_tsvector('simple', %s))
                """, [(document_id, index, chunk["text"], chunk["kind"], Jsonb(chunk["location"]),
                        chunk["asset_name"], json.dumps(chunk["embedding"]), chunk["tokens"])
                       for index, chunk in enumerate(chunks)])
            connection.execute("""
                UPDATE kb002.documents SET status = 'ready', error = NULL,
                    chunk_count = %s, image_count = %s, claim_token = NULL,
                    updated_at = now() WHERE id = %s
            """, (len(chunks), image_count, document_id))

    def search(self, vector, tokens, candidate_count):
        # Exact cosine search is intentional for this small first version.
        # RRF merges ranks, whose numerical scales are comparable across channels.
        query_vector = json.dumps(vector)
        with self.connect() as connection:
            semantic = connection.execute("""
                SELECT c.id, c.ordinal, c.document_id, d.filename, c.text, c.kind, c.location, c.asset_name,
                    c.embedding <=> %s::vector AS distance
                FROM kb002.chunks c JOIN kb002.documents d ON d.id = c.document_id
                WHERE d.status = 'ready' ORDER BY distance, c.id LIMIT %s
            """, (query_vector, candidate_count)).fetchall()
            lexical = connection.execute("""
                SELECT c.id, c.ordinal, c.document_id, d.filename, c.text, c.kind, c.location, c.asset_name,
                    c.embedding <=> %s::vector AS distance,
                    ts_rank_cd(c.search_vector, q.query) AS lexical_score
                FROM kb002.chunks c JOIN kb002.documents d ON d.id = c.document_id
                    CROSS JOIN (SELECT to_tsquery('simple', %s) AS query) q
                WHERE d.status = 'ready' AND c.search_vector @@ q.query
                ORDER BY lexical_score DESC, c.id LIMIT %s
            """, (query_vector, tokens, candidate_count)).fetchall() if tokens else []
        merged = {}
        for channel, rows in (("semantic", semantic), ("lexical", lexical)):
            for rank, row in enumerate(rows, 1):
                item = merged.setdefault(row["id"], {**row, "score": 0.0, "channels": []})
                item["score"] += 1 / (60 + rank)
                item["channels"].append(channel)
        for item in merged.values():
            item["similarity"] = 1.0 - float(item["distance"])
        return sorted(merged.values(), key=lambda row: (-row["score"], row["id"]))
