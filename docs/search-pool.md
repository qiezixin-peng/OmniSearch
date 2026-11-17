# 搜索池与权威来源策略

搜索池负责发现候选资料，采集器负责取得候选 URL 的正文。两者不能混为一层。

## 默认来源

| 优先级 | 来源 | 类别 | 首选通道 | 备用通道 |
|---|---|---|---|---|
| P0 | Brave Search | 综合网页 | API | — |
| P0 | Google Search | 综合网页 | Scrapling Chrome | ego-browser |
| P0 | DuckDuckGo | 综合网页 | Scrapling Chrome | ego-browser |
| P0 | GitHub | 代码 | REST API | ego-browser |
| P0 | arXiv | 学术 | API | — |
| P0 | Crossref | 学术元数据 | API | — |
| P0 | OpenAlex | 学术与引用 | API | — |
| P1 | Semantic Scholar | 学术与引用 | API | — |
| P1 | Google Scholar | 学术发现 | Scrapling Chrome | ego-browser |
| P1 | Stack Exchange | 技术社区 | API | — |
| P1 | Reddit | 社区线索 | API | ego-browser |
| P1 | X | 实时声明、事件与舆情线索 | API（有令牌时） | Chrome / ego-browser |
| P1 | Hacker News | 技术发布与开发者讨论 | Algolia API | — |
| P1 | Hugging Face Hub | 模型、模型卡与实现 | Hub API | — |
| P1 | Europe PMC | 生物医学论文与全文线索 | API | — |
| P1 | GDELT | 全球新闻与事件覆盖 | DOC API | — |
| P1 | Internet Archive | 历史资料与存档媒体 | Advanced Search API | — |
| P1 | YouTube | 官方频道、演讲与演示 | Data API（需密钥） | — |
| P2 | GitLab | 代码 | API | — |
| P2 | CORE | 论文与全文 | API | — |
| P2 | Bluesky | 开放社交网络的一手帖子 | Public AT Protocol API | — |
| P2 | Google Patents | 专利 | Scrapling Chrome | ego-browser |

P0/P1/P2 是接入和调度优先级，不等于证据等级。Google、DuckDuckGo 和 Brave 是发现
入口，不因排名靠前就成为权威证据。论文、官方文档、原始代码、标准和专利需要在
实体归并后单独评级。

## 调度原则

1. 每个子问题先判断来源类别：网页、学术、代码、社区或专利；
2. 优先执行稳定 API，同时并行执行至少一个综合网页来源；
3. Chrome 采集失败或需要登录态时，可切换 ego-browser；
4. 搜索结果只作为候选，不根据摘要直接形成技术结论；
5. X、Bluesky、Reddit、Hacker News 等社区来源只用于发现实时声明、线索和反例，
   不能单独支撑高置信技术结论；
6. GDELT 用于扩大跨国家新闻覆盖，Internet Archive 用于补历史版本，二者不替代原始来源；
7. 官方域名通过搜索计划的 domain filter 定向查找，不建立一份永远不变的官方站点表；
8. Provider 的健康、限流、凭据和历史成功率参与实际选择；
9. 查询、命中记录和标准化资料分开持久化，允许一个结果被多个来源发现。

Agent 可以通过 `GET /v1/sources` 或 MCP `list_sources` 查看当前搜索池，通过类别和通道筛选；MCP
`list_sources` 工具将复用同一个注册中心。
