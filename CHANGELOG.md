# Changelog

本项目的公开版本变更记录保存在此文件。

## Unreleased

### Changed

- Windows 原生运行脚本升级为逐组件 Supervisor，使用真实 API、MCP 协议、Worker 心跳和
  RSSHub 健康检查恢复异常进程；
- 进程停止与重启改为追踪完整进程树，避免虚拟环境启动器 PID 与实际 Python 进程不一致；
- 任务计划启用 `StartWhenAvailable`、互斥执行、登录启动、周期检查和失败重试；
- 增加 Codex `SessionStart` Hook 入口，为新建、恢复、清空和 Fork 会话提供启动前保障。
- 增加静默成功、异常显式汇报的 `PostToolUse` Hook，并在深度研究结果中返回来源与采集
  失败明细，确保调用 Agent 向用户说明实际失效路径、回退和覆盖影响。
- 修复 `fetch_page` 的 robots.txt 参数校验故障，并约束 OpenAlex 重试预算，使 429 能保留为
  `RATE_LIMITED` 而不是被外层截止时间误报为超时。
- 增加跨窗口 Provider 尝试日志、诊断 MCP/REST 接口和进程日志归档；同时加入 DuckDuckGo
  HTML 回退、双 DoH 校验以及 Semantic Scholar 无密钥限速与长熔断，降低浏览器解析失败、
  Fake-IP DNS 误伤和重复限流。

## 0.1.0 — 2026-11-17

首个公开开发版本。

### 已实现

- 面向 Agent 的 MCP、REST 和 CLI 接口；
- 复杂问题拆解、多语言原生查询、来源规划和研究预算；
- API 与浏览器双通道并行搜索；
- Google、DuckDuckGo、Google Scholar、GitHub、arXiv、Crossref、OpenAlex、
  Semantic Scholar、RSSHub 等多来源搜索池；
- Patchright、Scrapling、Ego Browser 与隔离 Camoufox 的浏览器回退链；
- HTML、PDF 深度采集，正文版本化、分块和文档检索；
- 跨来源去重、E1–E5 证据评级、冲突标记、缺口分析和递归补搜；
- SQLite/PostgreSQL、任务事件、Worker Lease/Fencing、断点恢复；
- Windows 原生自动启动与恢复，以及 Linux/Docker Compose 部署基线；
- Provider 连接复用、查询缓存、并发合并、独立超时和质量门；
- SSRF、重定向地址、内网地址和云元数据访问防护。

### 当前限制

- 没有面向普通用户的 Web UI；
- 部分来源需要用户自行配置官方 API 凭据；
- 浏览器来源的可用性会受到页面结构、IP 信誉、地区和访问频率影响；
- 公开版本仍处于开发阶段，生产部署前应完成目标环境的安全与负载测试。
