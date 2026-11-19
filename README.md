# Atlas Doc Knowledge

支持 PDF、Word `.docx` 和 Markdown 的图文知识库，提供上传预览、确认入库、混合检索、来源追溯和只读 MCP。可配置独立的售前知识库，分别保存数据库记录和文件。

## 功能与边界

- PDF 提取正文、嵌图和页面图像，保留页码、坐标及原图来源；Markdown 保留标题、行号和本地引用图片。
- DOCX 按正文段落、表格及单元格定位，不生成页码，不读取页眉页脚。旧 `.doc`、未处理修订和不支持的复杂绘图需先转换或整理；复杂图表可转为 PDF。
- 解析后生成带版本标识的切片预览；用户确认相同 `preview_token` 后才向量化并发布。预览片段不参与检索。
- 本地 BGE-M3 在 CPU 上编码，pgvector 余弦检索与 jieba 中文全文检索通过 RRF 融合，再执行相似度过滤、精确重复及有来源证据的相邻重叠去重。
- 检索结果包含相似度、正文、原件、原图和全部合并来源；Web 支持删除已入库文档及重试文件清理，MCP 保持只读。
- 图片识别使用配置的 OpenAI 兼容视觉接口，每次请求最多 10 张图片，超出后分批处理。配置外部接口时，相关图片会发送到该服务；本地向量编码不代表整个图文流程离线。

## 环境与配置

依赖锁文件面向 **Windows / Python 3.10**，包含本地向量模型的依赖闭包。请先自行选定并配置解释器，以下 `python` 均指该解释器；已有项目应遵循自己的环境约定。锁文件记录已有环境版本，独立全新环境的安装兼容性尚未单独验收。

准备 PostgreSQL 16 和与该版本兼容的 pgvector 扩展，并在数据库中提供 `vector` 扩展的安装权限。模型权重、数据库和用户文档均不随仓库提供。请从可信来源单独准备 BGE-M3，并遵循模型许可证。

在仓库根目录使用已选定的解释器安装锁定依赖；安装前评估下文列出的历史依赖风险与现有环境兼容性：

```powershell
python -m pip install -r requirements.lock
Copy-Item .env.example .env
```

编辑本地 `.env`，不要提交或共享实际凭据。关键配置如下：

| 配置 | 用途 |
|---|---|
| `DATABASE_URL` | 原知识库 PostgreSQL 连接；URL 密码中的特殊字符须编码 |
| `PRESALES_DATABASE_URL` | 可选售前库连接；留空禁用，数据库名必须与原库不同 |
| `DATA_DIR` | 原件、预览、图片及解析缓存目录 |
| `EMBEDDING_BACKEND` | `local` 使用本地模型，`http` 使用兼容向量接口 |
| `EMBEDDING_MODEL_PATH` | 本地 BGE-M3 目录；不会自动下载权重 |
| `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` | HTTP 向量接口配置；启用后文本会发送到配置的服务 |
| `EMBEDDING_DIMENSION` | 向量维度，BGE-M3 使用 `1024`，须与数据库一致 |
| `VISION_BASE_URL` / `VISION_API_KEY` / `VISION_MODEL` | 图片识别配置；接口地址应包含服务要求的 `/v1` 前缀 |
| `PUBLIC_BASE_URL` | 原件、图片和 MCP 来源链接的公共基址，模板为回环地址 |
| `SEARCH_SIMILARITY_THRESHOLD` / `SEARCH_TOP_K` | 余弦阈值与默认结果数量，模板为 `0.40` / `5` |

使用相对路径时，请从仓库根目录运行。纯文本资料可以不配置视觉服务；含图资料需要视觉识别成功才能生成完整预览并发布。

## 数据库初始化与启动

先由具备权限的数据库管理员创建目标数据库，例如原库 `knowledge`；启用售前时再创建 `knowledge_presales`。`init-db` 负责目标库内的扩展、`kb002` 表结构和索引，不会创建数据库本身。

```powershell
python -m kb init-db
python -m kb check
python -m kb serve
```

`check` 会实际检查数据库并调用向量编码，视觉服务在首次处理图片时验证。按照模板配置，浏览器入口为 [http://127.0.0.1:8100](http://127.0.0.1:8100)，API 说明为 [/docs](http://127.0.0.1:8100/docs)。使用单个 Web 实例；在前台终端按 Ctrl+C 停止。

售前数据库已创建并配置连接后，单独初始化：

```powershell
python -m kb init-db --library presales
python -m kb check --library presales
```

Web 同时提供所有已配置库。原库入口 `/`、API `/api/...`、MCP `/mcp`；售前入口 `/presales/`、API `/presales/api/...`、MCP `/presales/mcp`。售前文件存入 `DATA_DIR/presales`。未配置售前连接时不会回退到原库；双库只隔离存储，**不提供用户或角色权限隔离**。

## 上传、检索与删除

1. 选择 PDF、DOCX、Markdown 及其引用图片，点击“上传并预览”。Markdown 图片必须位于文档同目录及子目录，外部图片 URL 和越界引用会被拒绝。
2. 在预览页核对正文、图片分析与来源位置，确认后点击“确认上传并入库”。确认请求使用当前预览版本，重复确认幂等。
3. 输入问题检索，展开来源查看原图、原件及位置。`limit` 可覆盖默认数量，低于相似度阈值的结果不会返回。
4. 已入库文档可在页面确认删除。索引删除与清理任务同事务提交；文件清理失败会显示待清理状态并允许重试。共享上传文件保留至最后引用删除，原上传目录之外的用户文件不受操作。

修改检索配置后需重启相应 Web 或独立 MCP 进程，无须重新入库。`SEARCH_SIMILARITY_THRESHOLD` 范围为 `-1` 到 `1`，`SEARCH_TOP_K` 范围为 `1` 到 `20`；默认阈值尚未针对具体业务标注集校准。

## API 与 MCP

| 方法与原库路径 | 用途 |
|---|---|
| `POST /api/documents` | 上传文档及附件，排队生成预览 |
| `GET /api/documents` | 列出文档与状态 |
| `GET /api/documents/{id}/preview` | 分页读取切片预览 |
| `POST /api/documents/{id}/confirm` | 使用当前 `preview_token` 确认入库 |
| `POST /api/documents/{id}/retry` | 重试失败任务 |
| `POST /api/search` | 混合检索，参数为 `query` 和可选 `limit` |
| `GET /api/documents/{id}` | 分页读取已发布片段 |
| `GET /api/documents/{id}/source` | 读取原件 |
| `DELETE /api/documents/{id}` | 删除已入库文档，或重试待执行的文件清理 |
| `GET /health` | 检查数据库结构与工作进程状态 |

MCP 提供 `search_knowledge`、`list_documents`、`get_document` 三个只读工具。可连接已运行 Web 的 `/mcp`，也可按需启动 stdio 或独立 HTTP 服务：

```powershell
python -m kb.mcp_server
python -m kb.mcp_server --transport streamable-http --port 8101
python -m kb.mcp_server --library presales
```

独立 HTTP MCP 绑定回环地址，协议路径为 `/mcp`；Web 共享 MCP 使用 Web 的访问配置。MCP 返回的来源链接需要对应 Web 服务可访问。

## 网络访问

模板默认仅允许本机回环访问。启用局域网前，需明确配置 `WEB_HOST`、`WEB_ALLOWED_HOSTS`、`WEB_ALLOWED_NETWORKS` 和 `PUBLIC_BASE_URL`，精确填写允许的地址与网段，再重启服务。`--host` 覆盖也会重新校验；`--port` 必须与公共基址有效端口一致。

应用包含 Host、Origin 和客户端网段校验，直连模式不信任代理头。**当前没有身份认证**，允许网段内可连接服务的设备能查看、上传、检索和删除资料。仓库不提供公网部署方案；不要把局域网免密配置直接用于公网。

## 验证与依赖说明

```powershell
python -m unittest discover -s tests -v
python -m pip check
```

单元测试使用合成资料及模拟数据库、模型；它们不能替代真实 PostgreSQL、向量模型与视觉接口的集成验收。`scripts/` 包含真实联调探针，部分会创建样本、上传入库、调用配置的模型或删除探针样本；运行前阅读脚本并选择获准的测试目标，不应默认对业务库执行。

`requirements.txt` 为直接依赖，`requirements.lock` 为已记录的解析版本，`docs/dependencies.json` 为依赖与许可证清单。`docs/existing-dependency-advisories.json` 是历史漏洞快照，记录了 Starlette `0.46.2`、Pillow `9.5.0`、sentence-transformers `3.0.1` 等现有依赖的已报告问题，不代表持续更新的安全扫描；升级需兼容性验证，当前仓库没有宣称这些问题已修复。

PyMuPDF 采用 AGPL / 商业双许可，分发和商业集成前应确认对应义务。本仓库未添加额外的项目许可证声明，依赖与模型各自许可证仍适用。

## 公开内容范围

公开源码、前端、合成单元测试、验证脚本、依赖锁文件、安全配置模板及依赖清单。`.env` 及其备份、`AGENTS.md`、`.codex/`、`data/`、`models/`、本机运行或验收报告、截图、日志及临时文件留在本地，不随仓库上传。新增文件时仍需检查内容，忽略规则不能替代凭据和业务数据审查。
