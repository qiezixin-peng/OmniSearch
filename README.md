# OmniSearch

多语言、多来源、API + Browser 双通道的智能研究型搜索引擎。它是 **Agent 的内部
研究基础设施**，不是给最终用户操作的搜索网页。用户只向 Agent 提问；Agent 通过
REST、MCP 或 CLI 自动调用 OmniSearch，并取得可追溯的资料和证据。

当前版本是 **v0.1 可运行基线**，已经落实：

- 研究任务、子问题、依赖、查询、搜索计划、结果和事件的数据契约；
- 可审计的研究任务状态机；
- Google、DuckDuckGo、Google Scholar 真实 Chrome 搜索及 ego-browser 降级；
- GitHub、arXiv、Crossref、OpenAlex、Semantic Scholar、Stack Exchange、GitLab、Reddit、
  CORE、Brave 的真实 API Adapter（需要密钥的来源会明确报告 `NOT_CONFIGURED`）；
- 启发式及可选模型驱动的问题拆解、动态多语言查询和来源规划；
- 并行搜索、跨来源去重、缺口分析、递归补搜和预算终止；
- HTML/PDF 深度采集、SSRF 防护、正文版本化与重叠分块；
- E1–E5 证据评级、交叉验证与冲突标记；
- SQLite/PostgreSQL 持久化、Alembic 迁移、Worker Lease/Fencing/心跳恢复；
- MCP v2、REST、CLI，以及 Docker Compose 本地/云端部署基线。

## 浏览器与采集分工

- **Scrapling**：普通 HTTP、动态公开页面、真实 Google Chrome/CDP、正文清洗和受控爬取；
- **ego-browser**：登录态、用户授权会话和需要交互的复杂网页；
- **OmniSearch**：统一策略、任务、预算、证据、审计以及对 Agent 的工具接口。

Scrapling 的指纹伪装、代理、Cloudflare challenge 处理、持久会话和网络响应捕获均作为
可调度能力保留，并在每次任务中记录实际启用的策略。完整决策见
[docs/adr-001-browser-and-fetching.md](docs/adr-001-browser-and-fetching.md)。

## 搜索池

默认搜索池已登记 Google、DuckDuckGo、Brave、GitHub、arXiv、Crossref、OpenAlex、
Semantic Scholar、Google Scholar、Stack Exchange、Reddit、GitLab、CORE 和 Google
Patents。搜索规划器按子问题类别选择多个独立来源，详情见
[docs/search-pool.md](docs/search-pool.md)。

## 快速开始

要求 Python 3.12+。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,browser]"
scrapling install
.\.venv\Scripts\python -m pytest
omnisearch-migrate
omnisearch-api
```

本次验证环境的精确依赖版本记录在 `requirements.lock`；`pyproject.toml` 保留兼容版本
范围，便于后续有控制地升级。

服务启动后可访问 `http://127.0.0.1:8000/docs`。默认使用当前目录下的 SQLite；生产
环境通过 `OMNISEARCH_DATABASE_URL` 切换到 PostgreSQL。

### Windows 原生运行（无需 Docker/WSL）

本地 Agent 使用推荐直接启动 Windows 原生运行时。它使用 SQLite 和本地对象存储，
保留完整搜索、浏览器采集、递归研究、REST、Worker 与 MCP 能力，不要求管理员权限：

```powershell
.\scripts\windows-native.ps1 Install
.\scripts\windows-native.ps1 Start
.\scripts\windows-native.ps1 Status
.\scripts\windows-native.ps1 Stop
.\scripts\windows-native.ps1 Uninstall
```

`Install` 会注册当前用户登录后的自动启动任务并立即启动服务；`Uninstall` 会停止服务并
移除该任务，但不会删除研究数据库或已采集资料。

API 文档位于 `http://127.0.0.1:8000/docs`，Streamable HTTP MCP 位于
`http://127.0.0.1:8010/mcp`。运行日志和 PID 文件统一写入
`D:\三喜\临时文件\OmniSearch运行`。

## Agent 接口

本地 STDIO MCP：

```powershell
omnisearch-mcp
```

远程 Streamable HTTP MCP：

```powershell
omnisearch-mcp --transport streamable-http --host 127.0.0.1 --port 8010
```

核心工具包括 `search`、`search_batch`、`search_sources`、`research_start`、
`research_status`、`research_continue`、`research_cancel`、`research_get_result`、`fetch_page`、
`fetch_batch`、`crawl_site`、`get_document`、`search_documents`、`get_evidence`、
`verify_claim`、`list_sources`
、`get_source_health` 和 `evaluate_search_quality`。

`research_start` 优先接受调用 Agent 已经生成的 `sub_questions` 和 `queries`。每个子问题
使用 Agent 自定义的短 `key` 建立依赖，查询显式指定语言、搜索表达、来源和目的；服务端
将其映射为持久化 UUID 并校验预算及 Provider 配置。若 Agent 只传入 `question`，系统才
使用确定性的启发式拆解兜底，不要求连接任何外部模型。

配置 `OMNISEARCH_OTEL_EXPORTER_OTLP_ENDPOINT` 后，API、Provider 搜索与文档采集会
输出 OpenTelemetry Trace；运行日志使用结构化 JSON。

## REST 流程

```text
POST /v1/research             创建任务
GET  /v1/research/{id}        获取任务和子问题
POST /v1/research/{id}/execute 自动规划、搜索、采集并完成研究
GET  /v1/research/{id}/result 获取结构化结果资料包
GET  /v1/research/{id}/events 查看审计事件
POST /v1/search               快速多源搜索
POST /v1/fetch-page           深度采集网页或 PDF
POST /v1/search-documents     检索已采集正文中的精确证据段落
```

## 云端或集群部署

```powershell
docker compose up --build
```

Compose 会启动 PostgreSQL、数据库迁移、REST API、持久化 Worker 和 Streamable HTTP
MCP，并提供 MinIO 对象存储与 Jaeger Trace 查看界面。私有凭据通过 `.env` 中的
`OMNISEARCH_*` 变量注入，不进入代码或日志。

Compose 默认只把 REST、MCP 和 Jaeger 映射到 `127.0.0.1`。需要跨机器访问时，应通过
HTTPS 反向代理或私有网络发布，并设置 `OMNISEARCH_SERVICE_API_KEY` 保护 REST；远程
MCP 应在反向代理层配置认证，不要仅把 `OMNISEARCH_BIND_ADDRESS` 改成 `0.0.0.0` 后
直接暴露到公网。

Windows Home 上的 Docker Desktop 只能以 WSL 2 作为 Linux 容器后端。因此 Windows
本地环境使用上面的原生运行方式；Compose 用于 Linux 云服务器或明确启用 WSL 2 的机器。

详细范围和后续路线见 [docs/implementation-plan.md](docs/implementation-plan.md)。
