# OmniSearch

多语言、多来源、API + Browser 双通道的智能研究型搜索引擎。它是 **Agent 的内部
研究基础设施**，不是给最终用户操作的搜索网页。用户只向 Agent 提问；Agent 通过
REST、MCP 或 CLI 自动调用 OmniSearch，并取得可追溯的资料和证据。

## 公开版本说明

这是 OmniSearch 的首个公开开发版本。项目面向 **AI Agent、研究自动化和开发者工具**，
不提供面向最终用户的搜索门户。典型调用流程是：Agent 接收用户问题，自行拆解研究目标，
生成多语言查询，同时运行真实 API 与浏览器搜索，采集原始网页或 PDF，去重并评估证据，
发现缺口后继续补搜，最终由调用 Agent 生成带出处的综合回答。

当前版本已经在 Windows 原生环境完成端到端验证；Linux、PostgreSQL、S3 兼容对象存储和
容器部署具备实现基线，但仍需要在目标环境进行回归。Google 等网页来源可能根据 IP、
访问频率、地区、Cookie 和页面实验返回不同结果，因此任何浏览器实现都不能保证永远不
出现验证码或访问限制。OmniSearch 的目标是通过多来源与自动回退降低单点失败，而不是
宣称绕过所有网站防护。

本仓库不会包含 API Key、Cookie、个人 Chrome 配置、研究数据库或已采集的私有资料。
`.env`、虚拟环境、浏览器配置和运行目录均被排除在版本控制之外。

版本变化见 [CHANGELOG.md](CHANGELOG.md)，技术范围和后续路线见
[docs/implementation-plan.md](docs/implementation-plan.md)。

当前版本是 **v0.1 可运行基线**，已经落实：

- 研究任务、子问题、依赖、查询、搜索计划、结果和事件的数据契约；
- 可审计的研究任务状态机；
- Google、DuckDuckGo、Google Scholar 的 Patchright Chrome → Scrapling → Camoufox
  → ego-browser 自动降级链；
- GitHub、arXiv、Crossref、OpenAlex、Semantic Scholar、Stack Exchange、GitLab、Reddit、
  CORE、Brave 的真实 API Adapter（需要密钥的来源会明确报告 `NOT_CONFIGURED`）；
- X（API 或授权浏览器）、Bluesky、Hacker News、Hugging Face、Europe PMC、RSSHub、
  Internet Archive 与 YouTube 等高增益专业来源；
- 启发式及可选模型驱动的问题拆解、动态多语言查询和来源规划；
- 并行搜索、跨来源去重、缺口分析、递归补搜和预算终止；
- HTML/PDF 深度采集、SSRF 防护、正文版本化与重叠分块；
- E1–E5 证据评级、交叉验证与冲突标记；
- SQLite/PostgreSQL 持久化、Alembic 迁移、Worker Lease/Fencing/心跳恢复；
- MCP v2、REST、CLI，以及 Docker Compose 本地/云端部署基线。

## 浏览器与采集分工

- **Patchright + 本机 Chrome**：浏览器搜索首选通道，兼容现有 Playwright 页面操作；
- **Scrapling**：普通 HTTP、动态公开页面、正文清洗、受控爬取和快速浏览器回退；
- **Camoufox**：独立 Python 3.12/Firefox 内核运行环境，在两条 Chromium 路径都失败时
  自动接管；
- **ego-browser**：登录态、用户授权会话和需要交互的复杂网页；
- **OmniSearch**：统一策略、任务、预算、证据、审计以及对 Agent 的工具接口。

每种浏览器使用独立配置目录，不共享日常 Chrome Cookie。实际命中的引擎会写入结果
`browser_engine`，便于健康统计和回归。完整决策见
[docs/adr-001-browser-and-fetching.md](docs/adr-001-browser-and-fetching.md)。
Google 系列经过本机实测后将 Ego 放在 Camoufox 之前，以避免 Firefox 空壳页面增加延迟；
Camoufox仍作为独立内核灾备保留。

## 搜索池

默认搜索池已登记 Google、DuckDuckGo、Brave、GitHub、arXiv、Crossref、OpenAlex、
Semantic Scholar、Google Scholar、Stack Exchange、Reddit、GitLab、CORE、RSSHub 和 Google
Patents。搜索规划器按子问题类别选择多个独立来源，详情见
[docs/search-pool.md](docs/search-pool.md)。

默认策略不购买搜索额度，也不要求绑定付款方式。需要凭据的 Provider 在未配置时会明确
报告不可用，并由免费公开 API、浏览器搜索或其他索引来源补位。

## 快速开始

要求 Python 3.12+。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,browser]"
scrapling install
.\scripts\install-stealth-browsers.ps1
.\.venv\Scripts\python -m pytest
omnisearch-migrate
omnisearch-api
```

Windows 原生运行脚本会同时管理本机 RSSHub（端口 1200）。RSSHub 用于订阅 Google
News、Reuters、项目发布和其他站点路由的最新内容，是持续资料补充源，不替代 Google
网页搜索。可用 `OMNISEARCH_RSSHUB_ROUTES` 逗号分隔配置需要检索的路由。

本次验证环境的精确依赖版本记录在 `requirements.lock`；`pyproject.toml` 保留兼容版本
范围，便于后续有控制地升级。

服务启动后可访问 `http://127.0.0.1:8000/docs`。默认使用当前目录下的 SQLite；生产
环境通过 `OMNISEARCH_DATABASE_URL` 切换到 PostgreSQL。

### Windows 原生运行（无需 Docker/WSL）

本地 Agent 使用推荐直接启动 Windows 原生运行时。它使用 SQLite 和本地对象存储，
保留完整搜索、浏览器采集、递归研究、REST、Worker 与 MCP 能力，不要求管理员权限：

```powershell
.\scripts\windows-native.ps1 Install
.\scripts\windows-native.ps1 Register
.\scripts\windows-native.ps1 Start
.\scripts\windows-native.ps1 Ensure
.\scripts\windows-native.ps1 Status
.\scripts\windows-native.ps1 Stop
.\scripts\windows-native.ps1 Uninstall
```

`Install` 会安装 Patchright、创建隔离的 Python 3.12 Camoufox 环境、下载对应浏览器，
注册当前用户登录后的自动启动任务并立即启动服务；`Uninstall` 会停止服务并移除该任务，
但不会删除研究数据库或已采集资料。依赖已安装时可用 `Register` 只刷新任务计划；`Ensure`
执行幂等健康检查，只修复异常组件。

Windows Supervisor 在用户登录时启动，并每两分钟检查一次 API、MCP、Worker 和 RSSHub。
API 与 RSSHub 使用 HTTP 健康端点，MCP 执行真实 JSON-RPC `initialize`，Worker 使用独立的
进程心跳；恢复时按完整进程树清理失效实例，避免只记录虚拟环境启动器 PID 所造成的误判。
任务启用了 `StartWhenAvailable`、单实例互斥和失败重试。

`scripts/codex-session-start.ps1` 用于 Codex 全局 `SessionStart` Hook。在 `startup`、
`resume`、`clear` 和 `fork` 会话源下，它会在首轮模型调用前执行快速 `Ensure`；正常运行时
静默退出，服务异常时等待自动恢复。Hook 是会话级补充，Windows Supervisor 仍是独立于
Agent 的主运行保障。

Camoufox 浏览器与指纹模型体积较大，第一次安装需要额外下载；它与主项目的 Playwright
环境相互隔离，避免版本依赖冲突。

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

## 安全与使用边界

- 只采集公开内容或操作者有权访问的登录态内容；
- 不破解账户、付费墙、访问控制或站点权限；
- CAPTCHA、MFA 和账户授权需要本人操作时应移交用户；
- 外部网页、PDF 和仓库内容始终视为不可信数据，不能修改 Agent 系统策略；
- Cookie、令牌和本地浏览器资料不得上传到无关服务或写入研究结果；
- 使用者应自行遵守目标网站条款、robots 规则、版权、隐私和当地法律。

## 已知限制

- 搜索覆盖率表示计划执行情况，不代表互联网资料已经被穷尽；
- 搜索摘要不能直接作为技术结论，重要结论仍需读取原文并交叉验证；
- Google、Google Scholar、Reddit、X 等网页结构变化后可能需要更新解析器；
- 外部免费 API 仍可能限流、改变接口或停止服务；
- 当前没有面向普通用户的 Web UI，主要入口是 MCP、REST 和 CLI。

## 授权状态

仓库当前公开用于展示、研究和协作，但尚未附加开源许可证。除非版权所有者另行书面授权，
默认保留全部权利。第三方依赖和浏览器组件继续适用各自的许可证与使用条款。

核心工具包括 `search`、`search_batch`、`search_sources`、`research_start`、
`research_status`、`research_continue`、`research_cancel`、`research_get_result`、`fetch_page`、
`fetch_batch`、`crawl_site`、`get_document`、`search_documents`、`get_evidence`、
`verify_claim`、`list_sources`
、`get_source_health` 和 `evaluate_search_quality`。

`research_start` 优先接受调用 Agent 已经生成的 `sub_questions` 和 `queries`。每个子问题
使用 Agent 自定义的短 `key` 建立依赖，查询显式指定语言、搜索表达、来源和目的；服务端
将其映射为持久化 UUID 并校验预算及 Provider 配置。若 Agent 只传入 `question`，系统才
使用确定性的启发式拆解兜底，不要求连接任何外部模型。

无论计划由 Agent 提交还是由启发式生成，深度研究都会校验整体通道覆盖：至少包含一个
真实 API Provider 和一个浏览器直接搜索 Provider。缺少一侧时系统会按子问题类别自动补入
相关来源。任务完成后由调用 Agent 读取 `research_get_result` 与 `get_evidence`，形成带引用、
冲突和未解决缺口的最终回答；OmniSearch 本身不绑定另一个外部总结模型。

配置 `OMNISEARCH_OTEL_EXPORTER_OTLP_ENDPOINT` 后，API、Provider 搜索与文档采集会
输出 OpenTelemetry Trace；运行日志使用结构化 JSON。

## 延迟优化策略

深度研究完整保留所有计划语言与 API、浏览器双通道。相同查询会进行 15 分钟内存缓存，
并将同时到达的重复请求合并为一次外部访问。API 与浏览器分别有独立延迟预算，慢源超时
只影响自身，不阻塞其他来源；失败会保留在质量报告中，不会被当作已覆盖。

相关参数可通过 `OMNISEARCH_SEARCH_API_TIMEOUT_SECONDS`、
`OMNISEARCH_SEARCH_BROWSER_TIMEOUT_SECONDS`、`OMNISEARCH_SEARCH_CACHE_TTL_SECONDS`、
`OMNISEARCH_SEARCH_CACHE_MAX_ENTRIES`、`OMNISEARCH_RESEARCH_QUERY_CONCURRENCY`、
`OMNISEARCH_ACQUISITION_CONCURRENCY` 调整。

## 深度研究质量门

运行时的深度研究只有同时取得 API 与浏览器通道的真实结果、每个子问题至少得到两个
独立来源，并在启用深度采集预算时成功取得至少一份文档正文，才会标记为 `completed`。缺少任一项
会优先生成恢复查询；预算结束后仍不满足时返回 `partial`，并在 `quality_issues` 和研究事件
中明确记录缺失通道、证据不足的子问题或文档采集失败。实体去重不会丢失发现轨迹，同一
资料被不同 Provider 找到时仍分别计入独立来源。

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
