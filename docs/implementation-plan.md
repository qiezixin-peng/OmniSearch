# 开发实施基线

## 当前交付状态

阶段 0–6 的端到端基线均已实现，并通过自动化和实网烟雾测试。后续工作从“补齐
主链路”转为质量增强、更多专业 Provider、分布式压力测试和生产可观测性。

验收条件：

1. 可以持久化创建研究任务与带依赖的子问题；
2. 非法状态跳转被明确拒绝；
3. 每次状态变化均生成不可变审计事件；
4. Provider 必须声明来源、通道、能力、语言和限流信息；
5. SQLite 测试通过，模型按 PostgreSQL 能力设计；
6. REST API 可完成阶段 0 的任务管理闭环。

## 产品边界

- 最终用户只向上层 Agent 描述问题，不直接操作 OmniSearch；
- 第一阶段不开发面向用户的搜索页面；
- MCP 是 Agent 的首选接口，REST 用作控制面和跨节点通信；
- Agent 提交研究目标，OmniSearch 返回任务 ID、进度、证据和最终资料包；
- 浏览器、搜索源与采集器由调度器自动选择，默认无需用户理解技术细节。

## 已完成里程碑

- v0.2：Brave、GitHub、arXiv API；Google/DuckDuckGo 搜索 Provider；Scrapling
  HTTP/Chrome 采集；ego-browser 交互后端；
- v0.3：问题拆解、多语言原生查询与搜索计划；
- v0.4：HTML/PDF 深度采集、实体归并与证据绑定；
- v0.5：覆盖矩阵、缺口分析、递归轮次与预算终止；
- v0.6：Worker Lease、Fencing、心跳和本地/云端混合部署；
- v1.0 基线：MCP、REST、CLI、迁移、Worker、容器部署和安全边界。

阶段 0–6 之后已经补齐：本地/S3 对象存储、OpenTelemetry/Jaeger、Google Patents、
Windows 原生运行时、查询级恢复检查点、来源熔断、安全逐跳重定向、站内递归采集、
正文证据检索、Agent 取消和运行时间预算。

尚需持续增强：大规模人工标注质量基准集、大型 PostgreSQL 集群压力测试，以及必须
由外部账号提供授权的付费/私有搜索 API。Windows 本机默认使用已经过实网验证的
SQLite 与本地对象存储；Linux 云端保留 PostgreSQL、MinIO 与 Jaeger Compose 部署。

## 关键边界

- 网页内容始终是不可信数据，不能成为工具指令；
- Scrapling 的指纹伪装、代理、Cloudflare challenge 处理、持久会话和网络捕获能力
  完整注册，由 Agent 按任务策略选择并写入审计事件；
- 登录凭据、Cookie 和代理密钥通过 Secret Store 引用，不写入任务正文或日志；
- 原始 Provider 响应与标准化实体分离保存；
- 查询与结果是多对多关系，同一资料不因多语言命中而复制；
- Chrome 日常配置、Cookie 和本地个人数据不上传云端。
