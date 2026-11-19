"""Inspect or create only the database selected by the project's .env."""
import argparse
import json
from pathlib import Path
import sys
import subprocess
import shutil

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from urllib.parse import quote, unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
DOCKER = shutil.which('docker')


def use_container_credentials():
    """Read only the approved local container's DB credentials into memory."""
    if not DOCKER:
        raise ValueError('请先安装 Docker CLI 并将其加入 PATH')
    path = ROOT / '.env'
    configured = dotenv_values(path).get('DATABASE_URL', '')
    target = unquote(urlsplit(configured).path.lstrip('/'))
    if not target:
        raise ValueError('请先在 .env 指定目标数据库名')
    result = subprocess.run(
        [str(DOCKER), 'inspect', '--format', '{{json .Config.Env}}', 'postgres16'],
        capture_output=True, text=True, check=True,
    )
    variables = dict(item.split('=', 1) for item in json.loads(result.stdout) if '=' in item)
    username = variables.get('POSTGRES_USER', 'postgres')
    password = variables.get('POSTGRES_PASSWORD')
    if not password:
        raise ValueError('现有容器未提供可用的数据库密码配置')
    value = ('postgresql://' + quote(username, safe='') + ':' + quote(password, safe='')
             + '@127.0.0.1:5432/' + quote(target, safe=''))
    set_key(path, 'DATABASE_URL', value)
    print('已使用现有本机容器账号更新项目数据库连接，凭据未输出')


def normalized_connection(library="default"):
    path = ROOT / ".env"
    key = "PRESALES_DATABASE_URL" if library == "presales" else "DATABASE_URL"
    value = dotenv_values(path).get(key)
    if not value:
        raise ValueError(".env 缺少 " + key)
    if value.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(value)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("本次只操作本机 PostgreSQL")
        credentials = ""
        if parsed.username:
            credentials = quote(unquote(parsed.username), safe="")
            if parsed.password is not None:
                credentials += ":" + quote(unquote(parsed.password), safe="")
            credentials += "@"
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        normalized = urlunsplit((parsed.scheme, credentials + host, parsed.path, parsed.query, ""))
        if normalized != value:
            set_key(path, key, normalized)
            print("已规范化 .env 数据库 URL 中的特殊字符")
        value = normalized
    info = conninfo_to_dict(value)
    if info.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("本次只操作本机 PostgreSQL")
    if not info.get("dbname"):
        raise ValueError("DATABASE_URL 未指定目标数据库")
    return info


def run(create=False, library="default"):
    info = normalized_connection(library)
    target = info["dbname"]
    print(json.dumps({"host": info["host"], "port": info.get("port", "5432"),
                      "database": target, "user": info.get("user")}, ensure_ascii=False))
    admin = {**info, "dbname": "postgres", "connect_timeout": 8}
    with psycopg.connect(**admin, autocommit=True) as connection:
        version = connection.execute("SHOW server_version").fetchone()[0]
        extension = connection.execute("""
            SELECT default_version FROM pg_available_extensions WHERE name = 'vector'
        """).fetchone()
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        print(json.dumps({"server_version": version, "database_exists": bool(exists),
                          "pgvector_available": bool(extension),
                          "pgvector_version": extension[0] if extension else None}))
        if create and not exists:
            connection.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(target)))
            print("目标数据库已创建")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--use-container-credentials", action="store_true")
    parser.add_argument("--library", choices=("default", "presales"), default="default")
    arguments = parser.parse_args()
    try:
        if arguments.use_container_credentials:
            if arguments.library != "default":
                raise ValueError("售前库沿用本地配置的连接，请单独配置 PRESALES_DATABASE_URL")
            use_container_credentials()
        run(arguments.create, arguments.library)
    except Exception as error:
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print(json.dumps({"error_type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)}), file=sys.stderr)
        raise SystemExit(1) from None
