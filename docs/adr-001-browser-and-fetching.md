# ADR-001：Agent 优先的浏览器与采集架构

状态：已接受
日期：2026-11-17

## 背景

OmniSearch 的用户不直接操作搜索界面。最终用户只向上层 Agent 描述问题，Agent 决定
是否启动快速搜索或深度研究，并自动选择搜索源、浏览器和采集方式。

搜索发现与内容采集是两种不同能力：搜索 Provider 负责发现 URL；采集器负责取得
URL 的正文、元数据和版本。Scrapling 是采集框架，不应被描述为新的综合搜索引擎。

## 决策

保留一个采集路由层，并提供三个相互隔离的执行路径。

| 路径 | 用途 | 会话边界 |
|---|---|---|
| Scrapling HTTP | 普通 HTML、文档入口、批量公开页面 | 独立请求或任务会话 |
| Scrapling Chrome | JavaScript 页面、专用 Chrome、授权 CDP | 专用配置目录或新建 CDP Context |
| ego-browser | 登录态、用户授权上下文、复杂交互 | 独立 TaskSpace |

### Chrome 模式

1. **Managed Chrome**：Scrapling 以 `real_chrome=True` 启动本机 Google Chrome，并使用
   OmniSearch 专用用户数据目录；不得使用日常 Chrome 默认目录。
2. **CDP Chrome**：用户明确启动调试实例后，通过 `AsyncRemoteSession` 连接，并默认
   `new_context=True`。不得把 Cookie 或个人配置同步到云端。

### ego-browser 模式

ego-browser 通过自身 CLI、TaskSpace 和 Page API 集成。它不是 Playwright，适配器不得
推测或调用未公开的 Playwright API。研究任务与 TaskSpace ID 建立关联；需要登录、
验证码或用户决定时可以移交控制，也可以由已注册的自动 challenge 处理能力继续执行。

### Scrapling 集成方式

OmniSearch 在核心服务内调用 Scrapling Python API，而不是把 Scrapling MCP 的所有工具
原样暴露给上层 Agent。这样可以保持统一的数据契约、URL 安全检查、预算、来源条款、
审计和访问策略。

使用的能力：

- `AsyncFetcher`：普通 HTTP；
- `StealthyFetcher.async_fetch(..., real_chrome=True)`：专用本地 Chrome；
- `AsyncRemoteSession`：授权的 CDP Chrome；
- `Response.markdown()`：移除脚本、样式和隐藏内容后生成 Markdown；
- `crawl_site`：已实现受域名、深度、页数、并发和 `robots.txt` 约束的站内采集。

完整保留的 Scrapling 能力：

- `solve_cloudflare` 和浏览器隐匿配置；
- TLS/浏览器指纹模拟与真实 Chrome；
- 代理与代理轮换；
- 持久会话、状态保存与恢复；
- 页面动作、截图、Shadow DOM 和后台 API 响应捕获；
- 受任务预算约束的 Spider/ContentSpider。

这些能力不是全局强制开启。调度器根据 Provider 健康、页面类型、失败原因和研究预算
选择，并记录启用原因、参数摘要和结果。密钥、Cookie 与代理凭据仅使用 Secret Store
引用。网页内容仍然作为不可信数据处理，不能修改 Agent 指令或系统策略。

## 路由原则

```text
公开静态页面       -> Scrapling HTTP
公开动态页面       -> Scrapling Managed Chrome
授权调试浏览器     -> Scrapling CDP Chrome
登录态/个人上下文  -> ego-browser
challenge 可自动处理 -> Scrapling Chrome
登录态/人工确认      -> ego-browser handoff
```

失败应回到调度器，由策略决定是否换通道；一个浏览器失败不能使 API 搜索任务整体失败。

## 影响

- 避免重复开发成熟的抓取、解析和站内爬取能力；
- Chrome 与 ego-browser 可以并存，但不会共享配置文件或 Cookie；
- OmniSearch 仍需自行实现问题拆解、多源发现、去重、证据评估和递归研究；
- Scrapling 作为可替换适配器，不进入核心领域模型。
