# 数据库说明（Atlas Doc Knowledge）

> 文档版本：2026-11-20 v1.0
> 适用：`D:\AI-work\009-RAG` 本机部署实例
> 维护：数据库结构变更、库名或口令调整后必须同步更新本文件

---

## 1. 总览

本机运行 **一个 PostgreSQL 实例、两个互相独立的数据库**，分别承载售后知识库与售前知识库。两者共用同一套表结构（schema `kb002`），但数据、文件目录、检索入口完全隔离。

| 逻辑库 | 数据库名 | Web 入口 | MCP 入口 | 文件目录 |
|---|---|---|---|---|
| 售后知识库 | `knowledge` | `/` | `/mcp` | `data/` |
| 售前知识库 | `knowledge_presales` | `/presales/` | `/presales/mcp` | `data/presales/` |

隔离边界：

- **只隔离存储**，不提供用户、角色或权限隔离——两个库使用同一个数据库账号，所有能访问 Web 的人都可读写两个库。
- 售前库必须使用与售后库**不同的数据库名**，配置相同会被 `kb.config.library_settings` 拒绝。
- `PRESALES_DATABASE_URL` 留空时售前入口不存在，且**不会回退**到售后库。

---

## 2. 连接信息

| 项 | 值 |
|---|---|
| 主机 | `127.0.0.1`（仅回环监听，不对外） |
| 端口 | `5432` |
| 账号 | `postgres`（超级用户，唯一业务账号） |
| 口令 | `rag_local_pg` |
| 售后库连接 | `postgresql://postgres:rag_local_pg@127.0.0.1:5432/knowledge` |
| 售前库连接 | `postgresql://postgres:rag_local_pg@127.0.0.1:5432/knowledge_presales` |
| 认证方式 | `scram-sha-256`（`pg_hba.conf` 中对 local / 127.0.0.1 / ::1 全部要求口令） |
| 应用侧配置 | `atlas-doc-knowledge/.env` 的 `DATABASE_URL`、`PRESALES_DATABASE_URL` |

> 口令含特殊字符时，写进 URL 必须百分号编码（`@` → `%40`，`:` → `%3A`）。当前口令只含字母、数字、下划线，可原样写入。

命令行连接：

```powershell
$env:PGPASSWORD = 'rag_local_pg'
D:\AI-work\009-RAG\pgsql\app\bin\psql.exe -h 127.0.0.1 -U postgres -d knowledge
D:\AI-work\009-RAG\pgsql\app\bin\psql.exe -h 127.0.0.1 -U postgres -d knowledge_presales
```

---

## 3. 实例信息

| 项 | 值 |
|---|---|
| 版本 | PostgreSQL 16.2（x86_64-pc-mingw64） |
| 发行形式 | 便携版，来自 PyPI `pgserver` 轮子解包，非安装程序 |
| 程序目录 | `D:\AI-work\009-RAG\pgsql\app` |
| 数据目录 | `D:\AI-work\009-RAG\pgsql\data` |
| 日志 | `D:\AI-work\009-RAG\pgsql\postgres.log` |
| 字符集 | `UTF8` |
| 排序规则 | `C`（`initdb --locale=C`） |
| 时区 | `GMT` |
| `max_connections` | 50 |
| `shared_buffers` | 512 MB |
| `work_mem` | 32 MB |
| `maintenance_work_mem` | 256 MB |
| `wal_level` | replica（`fsync` 开启） |
| 已装扩展 | `plpgsql 1.0`、`vector 0.6.2`（pgvector） |

`pgvector` 由 `init-db` 中的 `CREATE EXTENSION IF NOT EXISTS vector` 建立；扩展二进制位于 `pgsql\app\lib\postgresql\vector.dll`，控制文件在 `pgsql\app\share\postgresql\extension\vector.control`。

---

## 4. Schema 与数据表

两个库结构完全相同，全部业务对象位于 schema `kb002`。

### 4.1 表清单

| 表 | 用途 | 约行数（售后库实测） |
|---|---|---|
| `kb002.settings` | 单行元数据：embedding 指纹与维度 | 1 |
| `kb002.documents` | 文档台账：状态、计数、预览与确认信息 | 1 |
| `kb002.chunks` | 切片：正文/图片描述、向量、全文索引 | 7 |
| `kb002.units` | 章节结构树兼父子知识单元，切片挂在所属单元上 | 0（新上传后按章节生成） |
| `kb002.deletions` | 删除待清理队列（索引已删、文件未清） | 0 |

### 4.2 kb002.settings

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 主键，`CHECK (id = 1)`，永远只有一行 |
| `embedding_fingerprint` | text | `sha256(backend\|endpoint\|model\|dimension)`，换了编码器/端点/维度就与库不一致，`init-db`/`check` 会报错 |
| `embedding_dimension` | integer | 向量维度，当前 1024 |

当前值：维度 `1024`，指纹 `1d1cffe06a0371e9af55f80bb083a20b2de396d53ca19e530b07661c2d849486`（两库一致，说明用的是同一套 BGE-M3 本地配置）。

### 4.3 kb002.documents

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | uuid | 主键，应用生成 |
| `filename` | text | 上传时的相对文件名（可含子目录） |
| `content_hash` | text | 内容 SHA-256，唯一；同内容重复上传复用同一行 |
| `source_path` | text | 相对 `DATA_DIR` 的原件路径，如 `uploads/<批次uuid>/xxx.md` |
| `status` | text | 文档类别，见 §5.1 |
| `claim_token` | uuid | 处理所有权令牌，防止并发 worker 互相覆盖 |
| `error` | text | 失败原因，截断至 500 字符 |
| `chunk_count` | integer | 切片数 |
| `image_count` | integer | 图片数 |
| `created_at` / `updated_at` | timestamptz | 默认 `now()` |
| `preview_path` | text | 预览 JSON 落盘路径 |
| `preview_token` | uuid | 预览版本令牌，确认时校验 |
| `confirmed_at` | timestamptz | 用户确认入库时间 |

### 4.4 kb002.chunks

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | bigint | 主键，自增 |
| `document_id` | uuid | 外键 → `documents(id)` |
| `ordinal` | integer | 文档内切片序号，`UNIQUE(document_id, ordinal)` |
| `text` | text | 切片正文（图片切片存视觉模型生成的描述） |
| `kind` | text | 切片类别，见 §5.2 |
| `location` | jsonb | 来源定位，见 §5.3 |
| `asset_name` | text | 图片切片对应的资产文件名 |
| `embedding` | `vector(1024)` | BGE-M3 句向量，**非空** |
| `search_vector` | tsvector | `to_tsvector('simple', text)`，中文由 jieba 预分词后写入 |
| `heading` | text | 切片所属章节路径，如 `第二章 技术方案 / 2.2 视频监控系统`，默认空串 |
| `heading_level` | integer | 该切片所在章节层级；标题切片为标题自身层级 |
| `unit_id` | bigint | 外键 → `units(id)`，所属知识单元；删除单元时置空 |

`text` 的组成方式：解析先得到段落、表格行等语义块，同一单元内相邻块按 700 字符上限贪心拼接，表格行的单元格合并为 `列名：值`；只有单块超过 700 字符才按句末切开，此时相邻片重叠 80 字符。`location` 记录起始位置，跨块拼接时补充 `paragraph_end` / `page_end` / `line_end` 等结束字段。

### 4.5 kb002.units

一节一行：既是文档结构树，也是父子知识单元。章、节、附件都是单元，`parent_id` 表达层级，切片通过 `chunks.unit_id` 挂到直接包含它的单元上；检索命中子级切片后，沿 `parent_id` 向上取父单元即得到完整上下文。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | bigint | 主键，自增 |
| `document_id` | uuid | 外键 → `documents(id)`，`ON DELETE CASCADE` |
| `parent_id` | bigint | 父单元，外键 → `units(id)`，根单元为 NULL |
| `unit_key` | text | 业务稳定键：`sha1(标题路径 + 同级序号)` 前 16 位；重新解析同一份文件得到相同键 |
| `title` | text | 章节标题，根单元为空串 |
| `level` | integer | 层级，根为 0；由标题样式、编号（第X章/2.2.1/一、）和字号推算 |
| `ordinal` | integer | 文档内顺序号，0 为根 |
| `block_start` / `block_end` | integer | 覆盖的解析块区间，**父单元覆盖整棵子树** |
| `page_start` / `page_end` | integer | PDF 页码区间；Word/Markdown 无页码，为 NULL |
| `chunk_count` | integer | 直接挂在本单元的切片数，发布时按 `chunks.unit_id` 汇总 |
| `created_at` | timestamptz | 默认 `now()` |

约束与索引：`UNIQUE (document_id, unit_key)`、`kb002_units_document` btree(document_id)、`units_parent_id_fkey` 自引用级联。

### 4.6 kb002.deletions

| 列 | 类型 | 说明 |
|---|---|---|
| `document_id` | uuid | 主键（沿用原文档 id） |
| `filename` | text | 原文件名 |
| `source_path` | text | 原原件路径 |
| `removed_chunks` | integer | 本次删除的切片数 |
| `created_at` | timestamptz | 入队时间 |

语义：删除文档时，索引（`documents` + `chunks`）与清理计划在同一事务提交；文件删除失败则本行保留，界面上显示为 `cleanup_pending`，可重试。

---

## 5. 类别与枚举

### 5.1 文档状态 `documents.status`

| 值 | 含义 | 出口 |
|---|---|---|
| `pending` | 已登记或已确认，等待 worker 处理 | → `processing` |
| `processing` | worker 持有 `claim_token`，正在解析 | → `preview` / `failed` |
| `preview` | 切片已生成，等待用户确认 | → `pending`（确认）/ `failed` |
| `ready` | 已入库，参与检索 | 终态（可删除） |
| `failed` | 处理失败，`error` 有原因 | → `pending`（重试） |

数据库约束只允许以上 5 个值；列表接口额外**合成**一个虚拟状态 `cleanup_pending`（来自 `kb002.deletions`），不落库。

状态流转：

```
上传 → pending → processing → preview → (确认) pending → processing → ready
                     ↓                                  ↓
                   failed ←────────────────────────────  (异常)
```

### 5.2 切片类别 `chunks.kind`

| 值 | 含义 |
|---|---|
| `text` | 正文段落 / 表格单元格 / 标题块 |
| `image` | 图片切片，`text` 为视觉模型描述，`asset_name` 指向资产文件 |

数据库约束：`CHECK (kind IN ('text','image'))`。

### 5.3 来源定位 `chunks.location`（jsonb，按文件类型不同）

| 来源类型 | 字段 |
|---|---|
| PDF | `page`（页码，1 起）、`page_width`、`page_height`、`bbox` = [x0, y0, x1, y1]；标题块同样带 bbox |
| Word `.docx` | `format` = "docx"、`paragraph`（段落序号）、`table` / `row` / `cell`（表格内定位时出现） |
| Markdown | `heading`、`line_start`、`line_end`、`char_start`、`char_end` |

### 5.4 其他固定取值

| 位置 | 取值 |
|---|---|
| `settings.id` | 恒为 1 |
| 知识库标识（CLI） | `--library default \| presales` |
| 文档类型（可上传） | `.pdf`、`.docx`、`.md`、`.markdown` |
| 图片类型（作为附件） | `.png`、`.jpg`、`.jpeg`、`.webp` |
| 单文件上限 | 50 MB（每批 100 MB，最多 30 个文件） |

---

## 6. 索引与约束

| 对象 | 定义 | 用途 |
|---|---|---|
| `documents_pkey` | PK (id) | 主键 |
| `documents_content_hash_key` | UNIQUE (content_hash) | 内容去重，同内容复用记录 |
| `documents_status_check` | CHECK status ∈ 5 值 | 状态机约束 |
| `chunks_pkey` | PK (id) | 主键 |
| `chunks_document_id_ordinal_key` | UNIQUE (document_id, ordinal) | 切片序号唯一 |
| `chunks_document_id_fkey` | FK → documents(id) | 级联语义由应用控制（先删 chunks） |
| `chunks_kind_check` | CHECK kind ∈ ('text','image') | 类别约束 |
| `chunks_unit_id_fkey` | FK → units(id) ON DELETE SET NULL | 单元删除时切片保留但脱离层级 |
| `kb002_chunks_document` | btree (document_id) | 按文档取切片 |
| `kb002_chunks_unit` | btree (unit_id) | 按单元取切片/统计 |
| `kb002_chunks_text` | **GIN (search_vector)** | 中文全文检索 |
| `units_pkey` | PK (id) | 单元主键 |
| `units_document_id_unit_key_key` | UNIQUE (document_id, unit_key) | 同文档内业务键唯一 |
| `units_document_id_fkey` | FK → documents(id) ON DELETE CASCADE | 删文档连带删单元 |
| `units_parent_id_fkey` | FK → units(id) ON DELETE CASCADE | 树结构自引用 |
| `kb002_units_document` | btree (document_id) | 按文档取结构树 |
| `deletions_pkey` | PK (document_id) | 清理队列 |
| `settings_pkey` / `settings_id_check` | PK (id) / CHECK id = 1 | 单行表 |

**注意：向量列没有 ANN 索引（无 HNSW / IVFFlat）**。检索用的是精确余弦距离 `embedding <=> query` 全表排序，代码注释明确说明这是当前版本的刻意选择（数据量小、结果可复现）。数据量上万切片后需要补 `hnsw (embedding vector_cosine_ops)` 并调整候选数。

---

## 7. 向量与检索口径

| 项 | 值 |
|---|---|
| 模型 | BGE-M3（本地 CPU 编码，`sentence-transformers` 加载 `models/bge-m3`） |
| 维度 | 1024（`EMBEDDING_DIMENSION`，须与 `vector(1024)` 一致） |
| 距离 | 余弦（`<=>`），展示为 `similarity = 1 - distance` |
| 全文检索 | `to_tsvector('simple', ...)` + jieba 预分词，`to_tsquery('simple', ...)` |
| 融合 | RRF（Reciprocal Rank Fusion）合并语义与词法两路排名 |
| 阈值 | `SEARCH_SIMILARITY_THRESHOLD = 0.40`（低于阈值不返回） |
| 默认条数 | `SEARCH_TOP_K = 5`（API 的 `limit` 可覆盖，1–20） |
| 候选数 | 内部分别取 20 条候选后融合去重 |

改阈值/条数需重启 Web 进程，**不需要**重新入库。

---

## 8. 文件存储布局

数据库只存路径，正文与图片落在 `DATA_DIR`（默认 `atlas-doc-knowledge/data`）：

| 目录 | 内容 |
|---|---|
| `data/uploads/<批次uuid>/...` | 原始上传文件（按批次隔离，共享批次保留至最后引用删除） |
| `data/previews/<docId>/<token>.json` | 切片预览，确认前不参与检索 |
| `data/assets/<docId>/<token>/...` | 抽取出的图片资产 |
| `data/presales/...` | 售前库的 uploads / previews / assets |

`documents.source_path` 存的是相对 `DATA_DIR` 的路径，因此**挪动 `data` 目录必须同步更新配置**，否则原件与图片来源链接失效。

---

## 9. 运维命令

### 9.1 初始化与自检

```powershell
cd D:\AI-work\009-RAG\atlas-doc-knowledge
.\.venv\Scripts\python.exe -m kb init-db                    # 售后库
.\.venv\Scripts\python.exe -m kb init-db --library presales  # 售前库
.\.venv\Scripts\python.exe -m kb check                       # 实连 + 真调 embedding
.\.venv\Scripts\python.exe -m kb check --library presales
```

两个命令都幂等；`check` 会实际编码一次，视觉服务在首次处理图片时才验证。

### 9.2 备份与恢复

```powershell
$env:PGPASSWORD='rag_local_pg'
$bin='D:\AI-work\009-RAG\pgsql\app\bin'
& "$bin\pg_dump.exe" -h 127.0.0.1 -U postgres -Fc -f backup_knowledge.dump knowledge
& "$bin\pg_dump.exe" -h 127.0.0.1 -U postgres -Fc -f backup_presales.dump knowledge_presales
# 恢复（目标库需先建好并装 vector 扩展）
& "$bin\pg_restore.exe" -h 127.0.0.1 -U postgres -d knowledge --clean --if-exists backup_knowledge.dump
```

`DATA_DIR` 下的原件需单独备份（`Compress-Archive`），数据库备份不含文件。

### 9.3 常用巡检 SQL

```sql
-- 各库规模
SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE NOT datistemplate;
-- 文档状态分布
SELECT status, count(*) FROM kb002.documents GROUP BY 1 ORDER BY 2 DESC;
-- 切片与图片
SELECT kind, count(*) FROM kb002.chunks GROUP BY 1;
-- 待清理队列
SELECT * FROM kb002.deletions;
-- 每库向量维度
SELECT embedding_dimension, embedding_fingerprint FROM kb002.settings;
```

### 9.4 重置

单库清空：`TRUNCATE kb002.chunks, kb002.deletions, kb002.units, kb002.documents;`（保留 settings 与扩展）。
彻底重建：`dropdb knowledge && createdb knowledge` 后重跑 `init-db`，注意同时清理 `data/` 下对应目录，否则原件成为孤儿文件。

---

## 10. 已知限制与注意事项

1. **单账号超级用户**：`postgres` 既是实例管理员又是业务账号，没有最小权限分离；口令写在 `.env` 与本文档中，注意不要提交到公开仓库（`.gitignore` 已忽略 `.env` 与 `/docs/` 下的非白名单文件）。
2. **无行级隔离**：两个库靠"不同数据库名 + 不同文件目录"隔离，同一 Web 进程用同一账号访问两库。
3. **精确检索无 ANN 索引**：切片数超过约 5 万后语义检索会明显变慢，需补 HNSW。
4. **embedding 指纹绑定**：换模型、换端点、改维度都会让 `check` 报"与知识库记录不一致"，需按重建索引方案处理，不能直接改配置了事。
5. **原件路径为相对路径**：`DATA_DIR` 迁移或改名会使来源链接 404。
6. **时区为 GMT**：`timestamptz` 存 UTC，展示层按浏览器本地时区换算；直接查表看到的 `created_at` 是 UTC。
7. **`cleanup_pending` 是虚拟状态**：只出现在列表接口，库里查不到该值。

---

## 11. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-20 | 建立本机实例：PostgreSQL 16.2 + pgvector 0.6.2，建 `knowledge` 库并初始化 `kb002` |
| 2026-11-20 | 新增售前独立库 `knowledge_presales`，启用 `/presales/` 入口，两库结构一致、互不共享数据 |
| 2026-11-20 | 新增 `kb002.units` 结构树/父子单元表；`chunks` 增加 `unit_id`、`heading`、`heading_level` 三列与 `kb002_chunks_unit` 索引，两库均已迁移 |
