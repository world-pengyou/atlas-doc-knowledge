import asyncio
import hashlib
import logging
import shutil
import ipaddress
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import PurePosixPath
from uuid import UUID, uuid4
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kb.config import ROOT, library_settings, load_settings
from kb.database import Database
from kb.models import ModelError
from kb.service import IngestWorker, KnowledgeService
from kb.mcp_server import create_server
from kb.errors import DocumentConflict, DocumentNotFound, StorageCheckError, StoragePathError


logger = logging.getLogger(__name__)
DOCUMENT_TYPES = {".pdf", ".md", ".markdown", ".docx"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 100 * 1024 * 1024


@asynccontextmanager
async def lifespan(app):
    settings = load_settings()
    app.state.services = {}
    app.state.workers = {}
    async with AsyncExitStack() as stack:
        public = urlsplit(settings.public_base_url)
        libraries = ["default"]
        if getattr(settings, "presales_database_url", ""):
            libraries.append("presales")
        for library in libraries:
            selected = library_settings(settings, library)
            database = Database(selected)
            await asyncio.to_thread(database.check)
            service = KnowledgeService(selected, database)
            if library == "default":
                service.models.validate()
                app.state.service = service
            else:
                service.models = app.state.service.models
            worker = IngestWorker(service)
            await asyncio.to_thread(worker.start)
            stack.push_async_callback(asyncio.to_thread, worker.stop)
            app.state.services[library] = service
            app.state.workers[library] = worker
            if library == "default":
                app.state.worker = worker
            prefix = "" if library == "default" else "/presales"
            mcp = create_server(service, preload_local_runtime=False,
                                streamable_http_path=prefix + "/mcp",
                                port=public.port or (443 if public.scheme == "https" else 80))
            setattr(app.state, "mcp_http" if library == "default" else "presales_mcp_http",
                    mcp.streamable_http_app())
            await stack.enter_async_context(mcp.session_manager.run())
        yield


app = FastAPI(title="图文知识库", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
router = APIRouter()


def _library(request):
    path = getattr(getattr(request, "url", None), "path", "/")
    return "presales" if path == "/presales" or path.startswith("/presales/") else "default"


def service_for_request(request):
    if _library(request) == "default":
        return request.app.state.service
    service = getattr(request.app.state, "services", {}).get("presales")
    if service is None:
        raise HTTPException(404, "售前知识库未启用")
    return service


def worker_for_request(request):
    service_for_request(request)
    if _library(request) == "default":
        return request.app.state.worker
    return request.app.state.workers["presales"]


class MCPHttpEndpoint:
    async def __call__(self, scope, receive, send):
        request = Request(scope)
        service_for_request(request)
        name = "presales_mcp_http" if _library(request) == "presales" else "mcp_http"
        await getattr(scope["app"].state, name)(scope, receive, send)


app.add_route("/mcp", MCPHttpEndpoint(), methods=["GET", "POST", "DELETE"])
app.add_route("/presales/mcp", MCPHttpEndpoint(), methods=["GET", "POST", "DELETE"])


@app.middleware("http")
async def local_origin(request: Request, call_next):
    settings = request.app.state.service.settings
    allowed_hosts = getattr(settings, "web_allowed_hosts", ("127.0.0.1", "localhost", "::1"))
    if request.url.hostname not in allowed_hosts:
        return JSONResponse({"detail": "请求主机不在允许访问的地址中"}, status_code=403)
    try:
        peer = ipaddress.ip_address(request.client.host) if request.client else None
        networks = getattr(settings, "web_allowed_networks", ("127.0.0.0/8", "::1/128"))
        if peer is None or not any(peer in ipaddress.ip_network(value) for value in networks):
            raise ValueError()
    except ValueError:
        return JSONResponse({"detail": "访问来源不在允许的局域网范围内"}, status_code=403)
    origins = request.headers.getlist("origin")
    if origins:
        try:
            if len(origins) != 1 or _origin(origins[0]) != _origin(str(request.base_url).rstrip("/")):
                raise ValueError()
        except ValueError:
            return JSONResponse({"detail": "请求来源与当前服务不一致"}, status_code=403)
    return await call_next(request)


def _origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("无效 Origin")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("无效 Origin 端口")
    return parsed.scheme, parsed.hostname, port


@app.exception_handler(ModelError)
async def model_error(request, error):
    return JSONResponse({"detail": str(error)}, status_code=502)


@app.exception_handler(psycopg.Error)
async def database_error(request, error):
    logger.error("Database operation failed (%s)", type(error).__name__)
    return JSONResponse({"detail": "数据库操作失败，请检查测试库服务及表结构"}, status_code=503)


@router.get("/")
def index(request: Request):
    service_for_request(request)
    return FileResponse(ROOT / "static" / "index.html")


@router.get("/preview")
def preview_page(request: Request):
    service_for_request(request)
    return FileResponse(ROOT / "static" / "preview.html")


@router.get("/health")
def health(request: Request):
    service_for_request(request).database.check()
    worker = worker_for_request(request)
    if worker.error or not worker.thread.is_alive():
        raise HTTPException(503, worker.error or "入库工作进程已停止")
    return {"status": "ok", "worker": "running"}


@router.get("/api/documents")
def list_documents(request: Request):
    return {"documents": service_for_request(request).database.list_documents()}


def safe_upload_name(filename):
    name = (filename or "").replace("\\", "/")
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or ":" in name
            or any(ord(character) < 32 for character in name)
            or len(name) > 240 or not path.name or path.parts[0] == "."):
        raise HTTPException(400, "上传文件名或相对路径无效")
    if path.suffix.lower() not in DOCUMENT_TYPES | IMAGE_TYPES:
        raise HTTPException(400, "支持 PDF、Word（.docx）、Markdown 及配套 PNG/JPEG/WebP 图片")
    if any(part.endswith((" ", ".")) or part.split(".")[0].upper() in
           {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))} for part in path.parts):
        raise HTTPException(400, "文件名包含系统保留名称")
    return path


@router.post("/api/documents", status_code=202)
async def upload_documents(request: Request, files: list[UploadFile] = File(...)):
    worker = worker_for_request(request)
    if worker.error:
        raise HTTPException(503, worker.error)
    if not 1 <= len(files) <= 30:
        raise HTTPException(400, "每批请上传 1 到 30 个文件")
    paths = [safe_upload_name(upload.filename) for upload in files]
    if len({str(path).casefold() for path in paths}) != len(paths):
        raise HTTPException(400, "同一批次存在重名文件")
    if not any(path.suffix.lower() in DOCUMENT_TYPES for path in paths):
        raise HTTPException(400, "请同时选择 PDF、Word（.docx）或 Markdown 文档")
    service = service_for_request(request)
    upload_root = service.settings.data_dir / "uploads"
    batch = upload_root / str(uuid4())
    batch.mkdir(parents=True)
    total_bytes = 0
    saved = []
    registered = False
    registration_finished = False
    marker = batch / ".uploading"
    marker.touch(exist_ok=False)
    try:
        for upload, relative in zip(files, paths):
            destination = batch.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            count = 0
            with destination.open("xb") as output:
                while data := await upload.read(1024 * 1024):
                    count += len(data)
                    total_bytes += len(data)
                    if count > MAX_FILE_BYTES or total_bytes > MAX_BATCH_BYTES:
                        raise HTTPException(413, "单文件限制 50 MB，每批限制 100 MB")
                    digest.update(data)
                    output.write(data)
            if count == 0:
                raise HTTPException(400, "文件内容为空")
            saved.append((relative, destination, digest.hexdigest()))
        images = sorted((str(name), digest) for name, _, digest in saved
                        if name.suffix.lower() in IMAGE_TYPES)
        documents = []
        for relative, destination, digest in saved:
            if relative.suffix.lower() not in DOCUMENT_TYPES:
                continue
            if relative.suffix.lower() in {".md", ".markdown"}:
                # Include uploaded dependencies so changed images yield a new document.
                digest = hashlib.sha256((digest + repr(images)).encode()).hexdigest()
            previous_registration = registered
            # Preserve the complete source batch if cancellation or connection loss
            # makes the commit outcome uncertain. A retry uses the content hash.
            registered = True
            row = await asyncio.to_thread(service.database.add_document, uuid4(), str(relative), digest,
                                          str(destination.relative_to(service.settings.data_dir)))
            registered = row.pop("created", True) or previous_registration
            documents.append(row)
        registration_finished = True
        return {"documents": jsonable_encoder(documents)}
    finally:
        # A cancelled to_thread await can leave a registration committing in
        # the background. Preserve the marker until its result is confirmed.
        if registration_finished or not registered:
            marker.unlink(missing_ok=True)
        for upload in files:
            await upload.close()
        if not registered and batch.resolve().is_relative_to(upload_root.resolve()):
            shutil.rmtree(batch)


@router.delete("/api/documents/{document_id}")
def delete_document(document_id: UUID, request: Request):
    try:
        return service_for_request(request).delete_document(document_id)
    except DocumentNotFound as error:
        raise HTTPException(404, str(error)) from None
    except DocumentConflict as error:
        raise HTTPException(409, str(error)) from None
    except StoragePathError as error:
        raise HTTPException(400, str(error)) from None
    except StorageCheckError as error:
        raise HTTPException(503, str(error)) from None


@router.post("/api/documents/{document_id}/retry")
def retry_document(document_id: UUID, request: Request):
    worker = worker_for_request(request)
    if worker.error:
        raise HTTPException(503, worker.error)
    row = service_for_request(request).database.retry(document_id)
    if not row:
        raise HTTPException(409, "仅处理失败的文档可以重试")
    return row


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int | None = Field(default=None, ge=1, le=20)


class ConfirmRequest(BaseModel):
    preview_token: UUID


@router.get("/api/documents/{document_id}/preview")
def get_preview(document_id: UUID, request: Request, offset: int = 0, limit: int = 20):
    try:
        return service_for_request(request).preview_document(document_id, offset, limit)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post("/api/documents/{document_id}/confirm")
def confirm_document(document_id: UUID, body: ConfirmRequest, request: Request):
    worker = worker_for_request(request)
    if worker.error:
        raise HTTPException(503, worker.error)
    try:
        return service_for_request(request).database.confirm(document_id, body.preview_token)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/api/search")
def search(body: SearchRequest, request: Request):
    try:
        return service_for_request(request).search(body.query, body.limit)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get("/api/documents/{document_id}")
def get_document(document_id: UUID, request: Request, offset: int = 0, limit: int = 20):
    try:
        return service_for_request(request).document_detail(document_id, offset, limit)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get("/api/documents/{document_id}/source")
def source(document_id: UUID, request: Request):
    service = service_for_request(request)
    document = service.database.document(document_id)
    if not document:
        raise HTTPException(404, "文档不存在")
    path = (service.settings.data_dir / document["source_path"]).resolve()
    if not path.is_relative_to(service.settings.data_dir) or not path.is_file():
        raise HTTPException(404, "原件不存在")
    media_type = {".pdf": "application/pdf",
                  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}.get(
                      path.suffix.lower(), "text/plain; charset=utf-8")
    return FileResponse(path, media_type=media_type,
                        filename=PurePosixPath(document["filename"]).name,
                        content_disposition_type="inline")


@router.get("/api/documents/{document_id}/assets/{asset_name:path}")
def asset(document_id: UUID, asset_name: str, request: Request):
    service = service_for_request(request)
    if not service.database.document(document_id):
        raise HTTPException(404, "文档不存在")
    relative = PurePosixPath(asset_name)
    if not asset_name.endswith(".png") or len(relative.parts) != 2 or ".." in relative.parts or "\\" in asset_name:
        raise HTTPException(404, "图片不存在")
    try:
        UUID(relative.parts[0])
    except ValueError:
        raise HTTPException(404, "图片不存在") from None
    asset_root = (service.settings.data_dir / "assets" / str(document_id)).resolve()
    path = (asset_root / asset_name).resolve()
    if not path.is_relative_to(asset_root) or not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path, media_type="image/png")


app.include_router(router)
app.include_router(router, prefix="/presales")
