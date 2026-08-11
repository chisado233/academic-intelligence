# 关键决策记录

> 记录项目中的关键选择：为什么选 A 不选 B、踩过的坑、重要的边界划分。
> 按时间倒序，新决策加在最上面。
> 编号约定：`D-YYYY-MM-DD-N`。早期（2026-07-21）的 5 条决策在编号约定前记录，未编号。

---

### 2026-08-10 — 精确标识符在 collector 分类并只路由 capable 来源
- **编号**：D-2026-08-10-1
- **日期**：2026-08-10
- **决策**：完整现代/旧式 arXiv ID（含可选版本、前缀、URL）在 collector 入口严格分类，仅调用支持 `get_paper_by_arxiv_id` 的来源；adapter 使用 `id_list` 并对返回记录做 canonical ID 匹配。
- **原因**：把裸 ID 当 `all:<id>` 搜索会混入“正文提及该 ID”的无关论文；在 collector 分类能保持 facade/CLI 语义一致，adapter 二次校验则防止上游异常响应污染结果。
- **边界**：包含 ID 的自然语言仍是全文搜索；不新增通用 identifier registry、异常类型或依赖。

### 2026-08-10 — 重试策略归 transport，终止元数据跨包装保留
- **编号**：D-2026-08-10-2
- **日期**：2026-08-10
- **决策**：`RetryHandler` 只在终止异常上记录实际已消耗 retry count；`SourceFailure` 用最多 16 个唯一异常节点的 cause/context 链恢复 retry/status，外层显式上下文优先。
- **原因**：来源 adapter 会把 `HTTPStatusError` 包装为领域异常，过去导致实际重试 2 次却报告 0 次/无 HTTP 状态。保留 transport 对策略的唯一所有权，避免 adapter 或 agent 叠加无界重试。
- **自动化契约**：默认无外层重试；显式恢复最多 1 次。部分可用结果为 `PARTIAL`，所有 capable 来源仍受外部条件阻塞为 `BLOCKED` 并停止。

### 2026-08-09 — Cursor 使用实体 ID，排序值由后端解析
- **编号**：D-2026-08-09-1
- **日期**：2026-08-09
- **决策**：`after`/`cursor` 始终传上一页最后实体 ID；后端读取其排序列值并按 `(sort_value, id)` 翻页。
- **原因**：比暴露自定义编码 cursor 更易用，同时用 ID tie-breaker 保证非唯一排序列无重复、无遗漏。

### 2026-08-09 — Graph snapshot 独立于 storage
- **编号**：D-2026-08-09-2
- **日期**：2026-08-09
- **决策**：graph 持久化采用版本化独立 JSON snapshot 和原子替换，不修改 SQLite/JSON storage schema。
- **原因**：保持 graph 会话语义和主数据持久化边界，最小化迁移风险，并直接解决 CLI 跨进程丢图。

### 2026-08-09 — Parquet 保持可选依赖
- **编号**：D-2026-08-09-3
- **日期**：2026-08-09
- **决策**：CSV 使用标准库、JSONL 逐行写；Parquet 懒导入 pyarrow，由 `academic-intelligence[export]` extra 安装。
- **原因**：不增加核心安装体积，同时保留分块 ParquetWriter 路径和明确的缺依赖错误。

### 2026-08-09 — JSON 后端单进程单写者，SQLite 承担并发

- **决策**：同一进程内，同一解析目录只允许一个已连接的 `JSONStorage` 写者；重复连接 fail-closed。JSON 不承诺跨进程锁，并发写入使用 SQLite。
- **原因**：避免两个内存快照在 close 时互相覆盖，同时保持 JSON 后端简单、可移植的定位。

### 2026-08-09 — 导出显式区分 raw CSV 与 Excel-safe CSV

- **决策**：默认 CSV 保持无 BOM 的原始 UTF-8；`excel_safe=True`/`--excel-safe` 才添加 BOM 并中和公式前缀。Parquet 的嵌套字段用确定性 JSON 字符串和固定 schema。
- **原因**：机器互换不应被静默改写，同时为 Excel 用户提供明确的编码与公式注入防护。

### 2026-08-07 — 抓取层保留 httpx 实现，Scrapling 不做强制迁移
- **编号**：D-2026-08-07-1
- **日期**：2026-08-07
- **决策**：抓取层保留 httpx 实现，Scrapling 不做强制迁移
- **原因**：3A v2 设计原文"抓取层全面采用 Scrapling 并移除 utils/"未落地——环境无 Scrapling 且其为重型浏览器依赖链；现有 httpx 实现 364 测试全绿
- **实现**：演进式——保留 `utils/`（http / proxy / rate_limiter / retry / cache），不引入 Scrapling 依赖；若未来需要 Google Scholar 无 API 反爬增强，再评估 `scrapling[fetchers]` 可选集成
- **影响**：v2 §2 / §3 / §14 中 Scrapling 相关内容以本文档为准

### 2026-08-07 — 作者消歧为独立阶段，不替换名字去重
- **编号**：D-2026-08-07-2
- **日期**：2026-08-07
- **决策**：作者消歧为独立阶段，不替换名字去重
- **原因**：`AuthorDisambiguator`（ID 直连 + 启发式聚类）作为 `Deduplicator` 去重后的独立阶段由调用方使用；"同名不同人"的管线级拆分依赖 §6.2 第三层 `confirm_split`（Phase 2 预留），当前不做

### 2026-08-07 — 全源失败保留 AllSourcesFailedError
- **编号**：D-2026-08-07-3
- **日期**：2026-08-07
- **决策**：全源失败保留 `AllSourcesFailedError`，不映射为 `SourceUnavailableError`
- **原因**：已扩展 `failures` 字段携带每源失败原因，满足 §11.2 的信息要求

### 2026-08-07 — 图谱层不引入 networkx
- **编号**：D-2026-08-07-4
- **日期**：2026-08-07
- **决策**：图谱层不引入 networkx
- **原因**：纯 Python 有向图 + `OrderedDict` LRU 实现 `KnowledgeGraph` / `GraphCache`，避免新增依赖

---

### 2026-07-21 — 项目定位决策
- **日期**：2026-07-21
- **决策**：将学术情报采集系统设计为纯 Python 库 + CLI，而非 Web 平台或 Agent 框架
- **原因**：
  - 参考系统（PaperExtraction）过度耦合 FastAPI + Vue + Agent，不可复用
  - 纯库形式可被任何项目导入使用，灵活性最高
  - CLI 提供独立使用能力，无需编写代码
- **替代方案**：
  - 方案 A：继续扩展参考系统的 Web 平台（否决：耦合度高，维护成本大）
  - 方案 B：设计为 Agent 工具（否决：用户明确要求不需要 Agent 能力）
  - 方案 C：纯库 + CLI（采纳：最灵活、最可复用）
- **后果**：
  - 优势：高度可复用、零依赖 Web 框架、易于测试
  - 劣势：无 Web 界面，需要用户编写代码或命令行操作

### 2026-07-21 — 数据源策略
- **日期**：2026-07-21
- **决策**：支持 6+ 数据源（Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE）
- **原因**：参考系统仅支持 Google Scholar，数据覆盖不足；多源可交叉验证
- **替代方案**：
  - 方案 A：仅支持 Google Scholar（否决：数据覆盖不足）
  - 方案 B：支持全部学术数据源（否决：维护成本过高）
  - 方案 C：6 个核心源 + 插件化扩展（采纳：平衡覆盖与维护）
- **后果**：
  - 优势：数据覆盖广、交叉验证、单源失败可 fallback
  - 劣势：需要维护多个源的解析逻辑

### 2026-07-21 — 存储策略
- **日期**：2026-07-21
- **决策**：默认 SQLite，可选 JSON 文件
- **原因**：零配置、易部署、足够支撑中小规模数据
- **替代方案**：
  - 方案 A：PostgreSQL（否决：需要额外配置，增加使用门槛）
  - 方案 B：纯 JSON 文件（否决：查询性能差，不适合关系数据）
  - 方案 C：SQLite 默认 + JSON 可选（采纳：平衡易用与性能）
- **后果**：
  - 优势：零配置、事务支持、查询能力强
  - 劣势：大规模数据（>10GB）性能下降

### 2026-07-21 — 反爬策略
- **日期**：2026-07-21
- **决策**：代理池 + 智能频率控制 + 多策略 fallback
- **原因**：参考系统反爬能力弱，频繁被 Google Scholar 拦截
- **替代方案**：
  - 方案 A：Selenium + 浏览器模拟（否决：资源消耗大，易被检测）
  - 方案 B：纯 requests（否决：无 JS 渲染，部分页面无法获取）
  - 方案 C：httpx + 代理 + 频率控制（采纳：轻量、异步、可控）
- **后果**：
  - 优势：资源消耗低、异步并发、可控性强
  - 劣势：需要维护代理池

### 2026-07-21 — 技术栈选择
- **日期**：2026-07-21
- **决策**：Python 3.11+ + httpx + Pydantic v2 + SQLAlchemy 2.0 + Typer
- **原因**：
  - Python 3.11：原生 asyncio 支持、类型提示完善
  - httpx：异步 HTTP，支持 HTTP/2
  - Pydantic v2：高性能数据验证
  - SQLAlchemy 2.0：异步 ORM
  - Typer：类型安全的 CLI
- **替代方案**：
  - aiohttp（否决：httpx 更现代，API 更友好）
  - dataclasses（否决：Pydantic 提供更强大的验证和序列化）
  - Click（否决：Typer 基于类型注解，更简洁）
- **后果**：
  - 优势：现代化、高性能、类型安全
  - 劣势：部分库版本较新，可能存在兼容性问题
