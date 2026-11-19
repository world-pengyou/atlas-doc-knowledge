"""Run as python -m scripts.verify_database from the project root."""
import json
import math

import psycopg
from psycopg.conninfo import conninfo_to_dict

from kb.config import load_settings


TABLES = ("chunks", "deletions", "documents", "settings")


def verify(checks):
    settings = load_settings()
    target = conninfo_to_dict(settings.database_url)
    if target.get("dbname") != "knowledge":
        checks["target_database"] = {"ok": False, "expected": "knowledge"}
        return
    if (target.get("host") not in {"127.0.0.1", "localhost", "::1"}
            or target.get("hostaddr") not in {None, "", "127.0.0.1", "::1"}):
        checks["connection_scope"] = {"ok": False, "expected": "loopback"}
        return
    with psycopg.connect(settings.database_url, connect_timeout=8) as connection:
        connection.read_only = True
        connection.execute("SET LOCAL statement_timeout = '10s'")
        database, server_version = connection.execute(
            "SELECT current_database(), current_setting('server_version_num')::integer"
        ).fetchone()
        checks["target_database"] = {"ok": database == "knowledge", "expected": "knowledge"}
        checks["postgresql_16"] = {"ok": server_version // 10000 == 16, "version_num": server_version}
        if not checks["target_database"]["ok"]:
            return

        extension = connection.execute(
            "SELECT extversion FROM pg_catalog.pg_extension WHERE extname = 'vector'"
        ).fetchone()
        checks["pgvector"] = {"ok": bool(extension), "version": extension[0] if extension else None}

        table_rows = connection.execute("""
            SELECT c.relname FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'kb002' AND c.relkind IN ('r', 'p')
              AND c.relname IN ('documents', 'settings', 'chunks', 'deletions')
            ORDER BY c.relname
        """).fetchall()
        tables = [row[0] for row in table_rows]
        checks["kb002_tables"] = {"ok": set(tables) == set(TABLES), "found": tables}

        column = connection.execute("""
            SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'kb002' AND c.relname = 'chunks'
              AND a.attname = 'embedding' AND a.attnum > 0 AND NOT a.attisdropped
        """).fetchone()
        column_type = column[0] if column else None
        checks["embedding_column"] = {
            "ok": column_type == "vector(1024)" and settings.embedding_dimension == 1024,
            "type": column_type, "configured_dimension": settings.embedding_dimension,
        }

        indexes = connection.execute("""
            SELECT idx.relname FROM pg_catalog.pg_index i
            JOIN pg_catalog.pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_catalog.pg_am am ON am.oid = idx.relam
            JOIN pg_catalog.pg_class t ON t.oid = i.indrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_catalog.pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
            WHERE n.nspname = 'kb002' AND t.relname = 'chunks'
              AND a.attname = 'search_vector' AND a.atttypid = 'pg_catalog.tsvector'::regtype
              AND am.amname = 'gin' AND i.indnkeyatts = 1
              AND i.indisvalid AND i.indisready AND i.indpred IS NULL
            ORDER BY idx.relname
        """).fetchall()
        checks["full_text_gin"] = {"ok": bool(indexes), "indexes": [row[0] for row in indexes]}

        if extension:
            identical, orthogonal = connection.execute("""
                SELECT '[1,0]'::vector <=> '[1,0]'::vector, '[1,0]'::vector <=> '[0,1]'::vector
            """).fetchone()
            checks["cosine_distance"] = {
                "ok": math.isclose(identical, 0, abs_tol=1e-6) and math.isclose(orthogonal, 1, abs_tol=1e-6),
                "identical": identical, "orthogonal": orthogonal,
            }
        else:
            checks["cosine_distance"] = {"ok": False, "reason": "vector extension missing"}

        match, unrelated = connection.execute("""
            SELECT v @@ to_tsquery('simple', '流程 & 审批'), v @@ to_tsquery('simple', '库存')
            FROM (SELECT to_tsvector('simple', '流程 审批 归档') AS v) fixture
        """).fetchone()
        checks["chinese_full_text"] = {
            "ok": match and not unrelated, "matching_terms": match, "unrelated_term": unrelated,
        }


def main():
    checks = {}
    try:
        verify(checks)
    except (psycopg.Error, OSError, ValueError) as error:
        # Driver messages may include connection details; report only safe diagnostics.
        checks["execution"] = {
            "ok": False, "error_type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None),
        }
    ok = bool(checks) and all(check["ok"] for check in checks.values())
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
