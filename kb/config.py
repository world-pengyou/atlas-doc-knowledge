import hashlib
import math
import os
import ipaddress
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    public_base_url: str
    vision_base_url: str
    vision_api_key: str
    vision_model: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimension: int
    embedding_backend: str = "local"
    embedding_model_path: str = ""
    model_timeout: float = 120
    search_similarity_threshold: float = 0.40
    search_top_k: int = 5
    chat_base_url: str = ""
    chat_api_key: str = ""
    chat_model: str = ""
    chat_max_tokens: int = 2_000
    web_host: str = "127.0.0.1"
    web_allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    web_allowed_networks: tuple[str, ...] = ("127.0.0.0/8", "::1/128")
    presales_database_url: str = ""

    @property
    def embedding_fingerprint(self) -> str:
        endpoint = self.embedding_model_path if self.embedding_backend == "local" else self.embedding_base_url
        value = f"{self.embedding_backend}|{endpoint}|{self.embedding_model}|{self.embedding_dimension}"
        return hashlib.sha256(value.encode()).hexdigest()


def library_settings(settings, library="default"):
    """Derive a fixed library target; never accept a connection from a request."""
    if library == "default":
        return settings
    if library != "presales":
        raise ValueError("未知知识库")
    value = getattr(settings, "presales_database_url", "")
    if not value:
        raise ValueError("售前知识库未配置 PRESALES_DATABASE_URL")
    try:
        original = conninfo_to_dict(settings.database_url)
        presales = conninfo_to_dict(value)
    except (ProgrammingError, ValueError):
        raise ValueError("知识库数据库连接配置无效") from None
    port = presales.get("port", "5432")
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("售前知识库数据库端口必须为 1 到 65535 之间的整数")
    # A different spelling of localhost or a different user must not disguise
    # reusing the original database. Separate database names are mandatory.
    if not presales.get("dbname") or presales["dbname"] == original.get("dbname"):
        raise ValueError("售前知识库必须使用独立数据库名，不能与售后知识库共库")
    return replace(settings, database_url=value, data_dir=settings.data_dir / "presales",
                   public_base_url=settings.public_base_url + "/presales")


def _web_hosts(value):
    hosts = tuple(dict.fromkeys(part.strip().lower() for part in value.split(",")))
    for host in hosts:
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        if (not host or len(host) > 253
                or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                           for part in host.split("."))):
            raise ValueError("WEB_ALLOWED_HOSTS 必须为逗号分隔的精确主机名或 IP，不含端口或通配符")
    return hosts


def _web_networks(value):
    try:
        networks = tuple(dict.fromkeys(str(ipaddress.ip_network(part.strip(), strict=False))
                                       for part in value.split(",")))
    except ValueError:
        raise ValueError("WEB_ALLOWED_NETWORKS 必须为逗号分隔的局域网或回环 CIDR") from None
    for value in networks:
        network = ipaddress.ip_network(value)
        if (network.prefixlen == 0 or network.network_address.is_unspecified
                or not network.network_address.is_private or not network.broadcast_address.is_private):
            raise ValueError("WEB_ALLOWED_NETWORKS 仅允许局域网或回环网段，不能开放全部来源")
    return networks


def validate_web_access(settings):
    host = settings.web_host
    try:
        address = ipaddress.ip_address(host) if host != "localhost" else ipaddress.ip_address("127.0.0.1")
    except ValueError:
        raise ValueError("WEB_HOST 必须为 IP 监听地址或 localhost") from None
    if not address.is_unspecified and not address.is_private:
        raise ValueError("WEB_HOST 仅允许回环或局域网监听地址")
    allowed_hosts = _web_hosts(",".join(settings.web_allowed_hosts))
    networks = tuple(ipaddress.ip_network(value) for value in _web_networks(",".join(settings.web_allowed_networks)))
    try:
        public = urlsplit(settings.public_base_url)
        port = public.port
        if (public.scheme not in {"http", "https"} or not public.hostname
                or public.username is not None or public.password is not None
                or public.query or public.fragment or public.path not in {"", "/"}
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
    except ValueError:
        raise ValueError("PUBLIC_BASE_URL 必须为有效的 HTTP(S) 根地址，不含认证、查询或片段") from None
    if public.hostname not in allowed_hosts:
        raise ValueError("PUBLIC_BASE_URL 主机必须包含在 WEB_ALLOWED_HOSTS 中")
    if not address.is_loopback:
        try:
            public_ip = ipaddress.ip_address(public.hostname)
        except ValueError:
            raise ValueError("局域网模式的 PUBLIC_BASE_URL 必须使用本机局域网 IP") from None
        if (public_ip.is_loopback or public_ip.is_unspecified or not public_ip.is_private
                or not any(public_ip in network for network in networks)):
            raise ValueError("局域网模式的 PUBLIC_BASE_URL IP 必须属于允许的局域网网段")
        if not address.is_unspecified and not any(address in network for network in networks):
            raise ValueError("WEB_HOST 必须属于 WEB_ALLOWED_NETWORKS")


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    backend = os.getenv("EMBEDDING_BACKEND", "local")
    if backend not in {"local", "http"}:
        raise ValueError("EMBEDDING_BACKEND 必须为 local 或 http")
    required = ["DATABASE_URL"]
    required += ["EMBEDDING_MODEL_PATH"] if backend == "local" else ["EMBEDDING_BASE_URL"]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("缺少配置项 " + ", ".join(missing))
    dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
    if not 1 <= dimension <= 16000:
        raise ValueError("EMBEDDING_DIMENSION 必须在 1 到 16000 之间")
    try:
        search_similarity_threshold = float(os.getenv("SEARCH_SIMILARITY_THRESHOLD", "0.40"))
    except ValueError:
        raise ValueError("SEARCH_SIMILARITY_THRESHOLD 必须为 -1 到 1 之间的有限数值") from None
    if not math.isfinite(search_similarity_threshold) or not -1 <= search_similarity_threshold <= 1:
        raise ValueError("SEARCH_SIMILARITY_THRESHOLD 必须为 -1 到 1 之间的有限数值")
    try:
        search_top_k = int(os.getenv("SEARCH_TOP_K", "5"))
    except ValueError:
        raise ValueError("SEARCH_TOP_K 必须为 1 到 20 之间的整数") from None
    if not 1 <= search_top_k <= 20:
        raise ValueError("SEARCH_TOP_K 必须为 1 到 20 之间的整数")
    try:
        chat_max_tokens = int(os.getenv("CHAT_MAX_TOKENS", "2000"))
    except ValueError:
        raise ValueError("CHAT_MAX_TOKENS 必须为 256 到 16000 之间的整数") from None
    if not 256 <= chat_max_tokens <= 16000:
        raise ValueError("CHAT_MAX_TOKENS 必须为 256 到 16000 之间的整数")
    public_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    for name, value in (("VISION_BASE_URL", os.getenv("VISION_BASE_URL", "")),
                        ("CHAT_BASE_URL", os.getenv("CHAT_BASE_URL", "")),
                        ("EMBEDDING_BASE_URL", os.getenv("EMBEDDING_BASE_URL", "")),
                        ("PUBLIC_BASE_URL", public_url)):
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment
                    or (port is not None and not 1 <= port <= 65535)
                    or (name == "PUBLIC_BASE_URL" and parsed.path not in {"", "/"})):
                raise ValueError()
        except ValueError:
            raise ValueError(f"{name} 必须为有效 HTTP(S) 服务地址，认证信息使用独立配置项") from None
    public_url = public_url.rstrip("/")
    settings = Settings(
        database_url=os.environ["DATABASE_URL"],
        data_dir=Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve(),
        public_base_url=public_url,
        vision_base_url=os.getenv("VISION_BASE_URL", "").rstrip("/"),
        vision_api_key=os.getenv("VISION_API_KEY", ""),
        vision_model=os.getenv("VISION_MODEL", ""),
        chat_base_url=os.getenv("CHAT_BASE_URL", "").rstrip("/"),
        chat_api_key=os.getenv("CHAT_API_KEY", ""),
        chat_model=os.getenv("CHAT_MODEL", ""),
        chat_max_tokens=chat_max_tokens,
        embedding_base_url=os.getenv("EMBEDDING_BASE_URL", "").rstrip("/"),
        embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
        embedding_model=os.getenv("EMBEDDING_MODEL", "bge-m3"),
        embedding_dimension=dimension,
        embedding_backend=backend,
        embedding_model_path=os.getenv("EMBEDDING_MODEL_PATH", ""),
        search_similarity_threshold=search_similarity_threshold,
        search_top_k=search_top_k,
        web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
        web_allowed_hosts=_web_hosts(os.getenv("WEB_ALLOWED_HOSTS", "127.0.0.1,localhost,::1")),
        web_allowed_networks=_web_networks(os.getenv("WEB_ALLOWED_NETWORKS", "127.0.0.0/8,::1/128")),
        presales_database_url=os.getenv("PRESALES_DATABASE_URL", "").strip(),
    )
    validate_web_access(settings)
    if settings.presales_database_url:
        library_settings(settings, "presales")
    return settings
