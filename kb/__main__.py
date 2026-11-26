import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="本地文档知识库（售后/售前）")
    parser.add_argument("command", choices=["init-db", "serve", "check"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--host", help="监听 IP，默认从 WEB_HOST 读取")
    parser.add_argument("--library", choices=("default", "presales"), default="default",
                        help="init-db/check 的目标库；Web 同时提供所有已配置库")
    arguments = parser.parse_args()
    if arguments.command == "serve":
        import uvicorn
        from urllib.parse import urlsplit
        from dataclasses import replace
        from kb.config import load_settings, validate_web_access

        try:
            settings = load_settings()
            if arguments.host is not None:
                settings = replace(settings, web_host=arguments.host)
            validate_web_access(settings)
        except ValueError as error:
            parser.error(str(error))
        port = arguments.port
        public = urlsplit(settings.public_base_url)
        public_port = public.port or (443 if public.scheme == "https" else 80)
        if port is None:
            port = public_port
        if not 1 <= port <= 65535:
            parser.error("--port 必须在 1 到 65535 之间")
        if port != public_port:
            parser.error("--port 必须与 PUBLIC_BASE_URL 的端口一致，请同步更新配置以保证 MCP 和来源链接可用")
        uvicorn.run("kb.app:app", host=settings.web_host, port=port, workers=1, proxy_headers=False)
        return
    from kb.config import library_settings, load_settings
    from kb.database import Database
    from kb.models import ModelClient
    try:
        settings = library_settings(load_settings(), arguments.library)
        database = Database(settings)
        if arguments.command == "init-db":
            database.initialize()
            print(arguments.library + " 库 kb002 表结构、向量列及全文 GIN 索引已初始化")
        else:
            database.check()
            ModelClient(settings).embed(["知识库连接检查"])
            print("数据库与 embedding 实际调用通过，视觉模型随首次图片入库验证")
    except Exception as error:
        # Connection exceptions may contain credential-bearing connection strings.
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print("操作失败，错误类型 " + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
