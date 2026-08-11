# Academic Intelligence — 3B 派发执行计划

> 本文档是 3B 派发执行计划，依赖 3A 技术设计基线（2026-07-26-technical-design-v2.md）。
> 3A 未获用户批准前，不得启动正式实现。

---

## 0. 执行约束

### 可用 Agent

| Agent | 模型 | 主责 |
|-------|------|------|
| Codex | gpt-5.6-sol（原生登录，代理 127.0.0.1:7890） | 后端核心逻辑、架构、复杂算法、数据模型 |
| qoderclicn | Qwen3.8-Max-Preview → Qwen3.7-Max | 适配器实现、存储层、CLI、测试、文档 |

### 审核规则

- Codex 实现 → qoderclicn 审核
- qoderclicn 实现 → Codex 审核
- 两家结论冲突 → 主 Agent 检查代码和测试后裁决
- Critical / Important 未关闭不放行
- 实现者不能成为唯一审核者或唯一测试者

### 阶段性审查

- 3A 技术设计：已提交，待用户批准
- 3B 派发计划（本文档）：待用户批准
- 执行期：每个 Batch 完成后提交阶段包，用户批准后进入下一 Batch
- 测试验证：全部 Batch 完成后独立验证
- 交付关闭：用户最终接受

### 通用规则

- Worker 不得派发下级 Agent、执行 Git、修改共享文档
- 主 Agent 独占 Git、共享文档、批次放行和冲突裁决
- 每个工作包使用唯一档案目录：`D:\agent_workspace\tmp\agent-dispatch\<timestamp>-<slug>\`
- Worker 最低必加载技能是下限，Worker 可自主加载更多
- 正式项目 cwd：`D:\agent_workspace\projects\paper-research-crawler`

---

## 1. 依赖总图

```
Batch 1 (Phase 1: MVP)
├── WP-1.1 数据模型 v2 ──────────────────────────────────────┐
├── WP-1.2 FetchGateway + Scrapling 封装 ────────────────────┤
├── WP-1.3 OpenAlex 适配器 ─────────── 依赖 1.1 + 1.2 ──────┤
├── WP-1.4 SQLite 存储层 ──────────── 依赖 1.1 ─────────────┤
├── WP-1.5 基础去重 + 置信度 ──────── 依赖 1.1 + 1.4 ───────┤
├── WP-1.6 Python API 主入口 ──────── 依赖 1.3 + 1.4 + 1.5 ─┤
├── WP-1.7 CLI ────────────────────── 依赖 1.6 ─────────────┤
└── WP-1.8 Phase 1 集成测试 ───────── 依赖 1.3~1.7 ─────────┘

Batch 2 (Phase 2: 多源 + 图谱)
├── WP-2.1 arXiv 适配器 ──────────── 依赖 1.2 ──────────────┐
├── WP-2.2 Semantic Scholar 适配器 ── 依赖 1.2 ─────────────┤
├── WP-2.3 多源去重融合升级 ───────── 依赖 1.5 + 2.1 + 2.2 ─┤
├── WP-2.4 KnowledgeGraph 核心 ────── 依赖 1.4 ─────────────┤
├── WP-2.5 expand() 遍历 + 懒加载 ── 依赖 2.4 + 1.6 ───────┤
├── WP-2.6 CLI expand 命令 ────────── 依赖 2.5 ─────────────┤
└── WP-2.7 Phase 2 集成测试 ───────── 依赖 2.1~2.6 ─────────┘

Batch 3 (Phase 3: 消歧 + 增量)
├── WP-3.1 作者消歧模块 ──────────── 依赖 1.4 + 1.5 ────────┐
├── WP-3.2 增量更新机制 ──────────── 依赖 1.4 + 1.6 ────────┤
├── WP-3.3 PubMed 适配器 ─────────── 依赖 1.2 ──────────────┤
├── WP-3.4 IEEE 适配器 ───────────── 依赖 1.2 ──────────────┤
├── WP-3.5 信息增强升级 ──────────── 依赖 2.3 + 3.1 ────────┤
├── WP-3.6 CLI update 命令 ────────── 依赖 3.2 ─────────────┤
└── WP-3.7 Phase 3 集成测试 ───────── 依赖 3.1~3.6 ─────────┘

Batch 4 (Phase 4: 反爬 + 完善)
├── WP-4.1 Google Scholar 适配器 ──── 依赖 1.2 + scrapling[fetchers] ┐
├── WP-4.2 代理轮换 + 自适应追踪 ─── 依赖 4.1 ─────────────────────┤
├── WP-4.3 完整错误处理 + 降级 ────── 依赖 全部适配器 ──────────────┤
├── WP-4.4 性能优化 ──────────────── 依赖 4.3 ─────────────────────┤
├── WP-4.5 文档 + 示例 ───────────── 依赖 4.3 ─────────────────────┤
└── WP-4.6 Phase 4 集成测试 ───────── 依赖 4.1~4.5 ────────────────┘
```

---

## 2. Batch 1：Phase 1 — 核心通路（MVP）

**目标**：从 OpenAlex 拉一篇论文的完整数据，存入 SQLite，通过 API 和 CLI 可查。

**批次验收条件**：
1. `ai paper "10.1038/nature14539"` 返回完整论文信息
2. `ai author "Geoffrey Hinton"` 返回学者资料
3. evidence 表包含正确记录，confidence 计算正确
4. SQLite 文件可独立查询，schema 与 3A 一致
5. 单元测试覆盖率 ≥ 80%

---

### WP-1.1 数据模型 v2

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §4 数据模型 |
| 对应模块 | `core/models.py`, `core/types.py`, `config.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`academic_intelligence/core/models.py`, `core/types.py`, `config.py`；读：3A 文档 |
| 不负责 | 不改 sources/、storage/、processors/ |
| 输入 | 3A §4 模型定义、§12 配置定义 |
| 输出 | 完整的 Pydantic v2 模型（Paper, Author, AuthorRef, Evidence, CitationEdge, CoauthorEdge, Config）+ 类型枚举 |
| 依赖 | 无（第一个工作包） |
| 验收 | 所有模型可实例化、序列化/反序列化正确、字段校验生效（DOI 格式、年份范围、confidence 范围）；单元测试覆盖每个模型的正常和异常路径 |
| 回退 | 模型定义错误 → 本包内修复 |

**实现要点**：
- Paper.authors 改为 `List[AuthorRef]`（含 position、is_corresponding、affiliation）
- evidence 从单条改为 `evidence_list: List[Evidence]`
- 新增 `arxiv_id`, `pmid`, `fields_of_study`, `reference_count`, `references`, `citations`
- Author 新增 `orcid`, `semantic_scholar_id`, `openalex_id`, `aliases`, `disambiguation_status`
- 删除 `AntiCrawlStrategy`，Config 按 3A §12 重写
- 保留现有 `ChangeType`, `ChangeDetection`, `IncrementalUpdateResult`（增量更新用）

---

### WP-1.2 FetchGateway + Scrapling 封装

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §3 抓取层 |
| 对应模块 | `sources/gateway.py` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`academic_intelligence/sources/gateway.py`；读：Scrapling 源码（`D:\agent_workspace\projects\Scrapling\scrapling\`） |
| 不负责 | 不实现具体源适配器 |
| 输入 | 3A §3.3 调用契约、Scrapling API |
| 输出 | FetchGateway 类：get_json / get_xml / get_html_stealth / crawl 四个方法 + 错误映射 |
| 依赖 | 无（与 1.1 并行） |
| 验收 | 单元测试 mock Scrapling 响应，验证 JSON/XML 解析正确、超时处理、错误类型映射（Scrapling 异常 → 项目异常） |
| 回退 | 接口设计不合理 → 本包内修复 |

**实现要点**：
- `get_json`: AsyncFetcher.get → response.json() → dict
- `get_xml`: AsyncFetcher.get → lxml.etree.fromstring(response.text)
- `get_html_stealth`: StealthyFetcher.fetch → 返回 Scrapling Selector（import 时检测可用性）
- `crawl`: 接收 Spider 子类，配置 crawldir checkpoint
- 统一异常映射：网络超时 → SourceUnavailableError，429 → RateLimitError，403/CAPTCHA → SourceBlockedError
- 可配置 proxy、download_delay、max_concurrent_requests（从 Config 读取）

---

### WP-1.3 OpenAlex 适配器

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §5.3 OpenAlex |
| 对应模块 | `sources/openalex.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`academic_intelligence/sources/openalex.py`；读：`sources/base.py`, `sources/gateway.py`, OpenAlex API 文档 |
| 不负责 | 不改 gateway、不改 storage |
| 输入 | WP-1.1 模型、WP-1.2 FetchGateway、OpenAlex API（`api.openalex.org`） |
| 输出 | OpenAlexAdapter(BaseSourceAdapter) 完整实现 |
| 依赖 | WP-1.1, WP-1.2 |
| 验收 | 集成测试：真实调用 OpenAlex API 获取 DOI=10.1038/nature14539 的论文，验证标题/作者/年份/引用数正确；获取 Geoffrey Hinton 的作者资料；获取该论文的 references 列表（≥10 条） |
| 回退 | API 响应格式变化 → 修复解析逻辑 |

**实现要点**：
- 搜索：`GET /works?search={query}&per_page={limit}`
- 按 DOI：`GET /works/doi:{doi}`
- 参考文献：`GET /works/doi:{doi}` → `referenced_works` 字段（返回 OpenAlex ID 列表，需二次查询）
- 被引：`GET /works?filter=cites:{openalex_id}`
- 学者论文：`GET /works?filter=author.id:{id}`
- 学者资料：`GET /authors/{id}`
- 礼貌池：所有请求加 `mailto` 参数（从 Config.openalex_email 读取）
- 响应映射：OpenAlex JSON → 内部 Paper/Author 模型，构建 Evidence 对象
- `supports()` 声明：search ✅, get_by_id ✅, references ✅, citations ✅, author_papers ✅, author_profile ✅

---

### WP-1.4 SQLite 存储层

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §8 存储层 |
| 对应模块 | `storage/sqlite_store.py`, `storage/models.py`, `storage/base.py`（更新） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`academic_intelligence/storage/`；读：3A §8, WP-1.1 模型 |
| 不负责 | 不改 sources/、processors/ |
| 输入 | 3A §8.1 Schema、§8.2 Repository 接口 |
| 输出 | SQLAlchemy 2.0 ORM 模型 + PaperRepository + AuthorRepository + EvidenceRepository + 建表迁移 |
| 依赖 | WP-1.1 |
| 验收 | 单元测试：CRUD 全路径、批量写入、查询过滤（按年份/作者/关键词）、evidence 关联查询、sync_state 读写；使用临时 SQLite 文件，测试后清理 |
| 回退 | Schema 设计问题 → 本包内修复 |

**实现要点**：
- 使用 SQLAlchemy 2.0 async（aiosqlite 驱动）
- WAL 模式启用（并发读优化）
- Repository 模式：PaperRepository / AuthorRepository / EvidenceRepository / SyncStateRepository
- 建表：papers, authors, authorships, citations, coauthorships, evidence, sync_state（按 3A §8.1）
- JSON 字段（keywords, fields_of_study, aliases 等）用 SQLAlchemy JSON 类型
- 保留现有 BaseStorage 抽象基类接口，SQLite 实现继承它
- 保留 json_store.py 作为可选后端（本包不改动）

---

### WP-1.5 基础去重 + 置信度评分

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §6.1 去重、§6.3 置信度 |
| 对应模块 | `processors/deduplicator.py`（升级）, `processors/scorer.py`（新建） |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`processors/deduplicator.py`, `processors/scorer.py`；读：3A §6, 现有 deduplicator.py |
| 不负责 | 不改 sources/、storage/ |
| 输入 | WP-1.1 模型（evidence_list）、3A §6.1 去重策略、§6.3 置信度算法 |
| 输出 | 升级版 Deduplicator（支持 evidence_list 合并）+ 新 ConfidenceScorer |
| 依赖 | WP-1.1, WP-1.4（需要存储接口定义） |
| 验收 | 单元测试：DOI 精确匹配去重、标题相似度去重（≥0.92 合并）、多源 evidence 合并后 confidence 正确计算（base + 0.05×(n-1)）、字段级冲突取最高置信度 |
| 回退 | 算法逻辑错误 → 本包内修复 |

**实现要点**：
- 保留现有 `_normalize_title`, `_jaccard`, `_fuzzy_match` 逻辑
- 升级 `_merge_papers`：合并 evidence_list 而非单条 evidence
- 新增精确匹配：DOI 相同 / arXiv ID 相同 / PMID 相同
- ConfidenceScorer：按 3A §6.3 的源基线表 + 多源加成 + 字段级调整
- 去重后自动调用 scorer 重算 confidence

---

### WP-1.6 Python API 主入口

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §10.1 Python API |
| 对应模块 | `__init__.py`（AcademicIntelligence 类重写） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 读写范围 | 写：`academic_intelligence/__init__.py`, `collectors/paper_collector.py`, `collectors/author_collector.py`；读：全部已完成模块 |
| 不负责 | 不实现图谱层（Phase 2） |
| 输入 | WP-1.3 OpenAlex 适配器、WP-1.4 存储、WP-1.5 去重 |
| 输出 | AcademicIntelligence 类：get_paper() / get_author() / search() / collect_author_papers() |
| 依赖 | WP-1.3, WP-1.4, WP-1.5 |
| 验收 | 集成测试：`ai = AcademicIntelligence(config)` → `await ai.get_paper("10.1038/nature14539")` 返回完整 Paper 对象（含 evidence_list）→ 数据已持久化到 SQLite |
| 回退 | 编排逻辑问题 → 本包内修复 |

**实现要点**：
- 初始化：读 Config → 实例化 FetchGateway → 注册启用的源适配器 → 连接 SQLite
- get_paper：先查 SQLite（by DOI / arXiv ID / title）→ miss 则多源并行拉取 → 去重 → 评分 → 入库 → 返回
- get_author：先查 SQLite（by ORCID / name）→ miss 则多源拉取 → 去重 → 入库
- search：多源并行搜索 → 去重 → 返回（不入库，除非用户显式 save）
- 生命周期：async context manager（`async with AcademicIntelligence(config) as ai:`）

---

### WP-1.7 CLI

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §10.2 CLI |
| 对应模块 | `cli.py` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 读写范围 | 写：`academic_intelligence/cli.py`；读：WP-1.6 API |
| 不负责 | 不改 API 层 |
| 输入 | WP-1.6 AcademicIntelligence API |
| 输出 | Typer CLI：`ai paper`, `ai author`, `ai search`, `ai stats` |
| 依赖 | WP-1.6 |
| 验收 | 端到端：`python -m academic_intelligence paper "10.1038/nature14539"` 输出格式化论文信息；`ai search "deep learning" --limit 5` 返回结果列表 |
| 回退 | CLI 参数设计问题 → 本包内修复 |

---

### WP-1.8 Phase 1 集成测试

| 项目 | 内容 |
|------|------|
| 对应需求 | 3A §17 验收条件 |
| 对应模块 | `tests/` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | verification-before-completion |
| 读写范围 | 写：`tests/`；读：全部 Phase 1 代码 |
| 不负责 | 不改产品代码（发现 bug 报告给主 Agent） |
| 输入 | 3A §17 验收条件 1-6 |
| 输出 | 集成测试套件 + 覆盖率报告 |
| 依赖 | WP-1.3 ~ WP-1.7 全部完成 |
| 验收 | 3A §17 六条验收条件逐条 PASS；覆盖率 ≥ 80%；无 Critical/Important 遗留 |
| 回退 | 发现产品缺陷 → 主 Agent 开修复单给对应 WP 的 Agent |

---

## 3. Batch 2：Phase 2 — 多源 + 图谱

**目标**：多源采集可用 + 递归展开 expand() 可用。

**批次验收条件**：
1. 同一篇论文从 OpenAlex + Semantic Scholar + arXiv 三源获取后正确去重为一条
2. `ai.expand(paper_id, relations=["references", "authors"])` 返回正确的邻居节点
3. 占位节点（stub）机制工作：未展开的节点标记 loaded=False
4. `ai expand "10.1038/nature14539" --depth 2` CLI 可用
5. 子图导出 JSON 格式正确

---

### WP-2.1 arXiv 适配器

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/arxiv.py` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.2（FetchGateway） |
| 输出 | ArxivAdapter：search_papers / get_paper_by_id / get_paper_references / get_author_papers |
| 验收 | 集成测试：搜索 "attention is all you need" 返回正确论文；按 arXiv ID 1706.03762 获取完整元数据；获取该论文的 references（从 API 的 `arxiv:comment` 或关联数据） |
| 特殊 | arXiv API 返回 Atom XML，需 lxml 解析；无被引数据（supports citations=False）；建议 3 秒间隔 |

---

### WP-2.2 Semantic Scholar 适配器

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/semantic_scholar.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.2（FetchGateway） |
| 输出 | SemanticScholarAdapter：全部 6 个方法 |
| 验收 | 集成测试：按 DOI 获取论文（含 TLDR）；获取 references 和 citations 列表；获取作者资料（含 S2 authorId）；rate limit 处理（429 → 等待 retry_after） |
| 特殊 | 无 key 100 req/5min；有 S2 author ID（消歧用）；有 citation context；download_delay=3 |

---

### WP-2.3 多源去重融合升级

| 项目 | 内容 |
|------|------|
| 对应模块 | `processors/deduplicator.py`（升级） |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.5, WP-2.1, WP-2.2 |
| 输出 | 支持 ID 交叉匹配（arXiv ID ↔ DOI 映射）、标题相似度升级到 SequenceMatcher、多源 evidence_list 完整合并 |
| 验收 | 单元测试：同一论文从 3 个源返回（不同 ID 格式）→ 正确合并为 1 条，evidence_list 含 3 条记录，confidence 正确 |

---

### WP-2.4 KnowledgeGraph 核心

| 项目 | 内容 |
|------|------|
| 对应模块 | `graph/knowledge_graph.py`, `graph/__init__.py` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.4（存储层） |
| 输出 | KnowledgeGraph 类：add_paper / add_author / add_citation_edge / add_authorship_edge / get_neighbors / to_subgraph / export_json |
| 验收 | 单元测试：构建 5 节点小图 → 查询邻居正确 → 导出 JSON 格式正确 → 从 SQLite 加载子图到 NetworkX 正确 |

---

### WP-2.5 expand() 遍历 + 懒加载

| 项目 | 内容 |
|------|------|
| 对应模块 | `graph/traversal.py`, `graph/cache.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-2.4, WP-1.6（API 主入口） |
| 输出 | expand() 实现：先查 SQLite → miss 调源适配层 → 新节点入图入库 → 占位节点标记 → 深度/节点数限制 |
| 验收 | 集成测试：expand(paper_id, ["references"]) 返回 ≥10 个节点；二次 expand 命中缓存不重复请求；depth=3 + max_nodes=50 截断生效 |

---

### WP-2.6 CLI expand 命令

| 项目 | 内容 |
|------|------|
| 对应模块 | `cli.py`（扩展） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-2.5 |
| 输出 | `ai expand <id> --relations references,citations,authors --depth 2` + `ai export --center <id> --radius 2 --format json` |
| 验收 | 端到端命令行可用，输出格式化 |

---

### WP-2.7 Phase 2 集成测试

| 项目 | 内容 |
|------|------|
| 对应模块 | `tests/integration/` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | verification-before-completion |
| 依赖 | WP-2.1 ~ WP-2.6 |
| 验收 | Batch 2 五条验收条件逐条 PASS；多源去重无误合并；图谱遍历深度正确 |

---

## 4. Batch 3：Phase 3 — 消歧 + 增量

**目标**：作者消歧可用 + 增量更新可用 + PubMed/IEEE 接入。

**批次验收条件**：
1. 两个 "Wei Zhang" 记录（不同机构/方向）不被误合并
2. 同一人不同源记录（有 ORCID）正确合并
3. `ai update --author "Geoffrey Hinton"` 只拉增量（新论文 + 引用数更新）
4. PubMed 搜索 "CRISPR" 返回正确结果
5. sync_state 表正确记录同步状态

---

### WP-3.1 作者消歧模块

| 项目 | 内容 |
|------|------|
| 对应模块 | `processors/disambiguator.py`（新建） |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.4, WP-1.5 |
| 输出 | AuthorDisambiguator：ID 直连合并 + 启发式聚类（6 维特征）+ ambiguous 标记 |
| 验收 | 单元测试：ORCID 相同 → 自动合并；同名不同机构/方向 → 不合并（得分 < 0.6）；同名同机构 → 合并（得分 ≥ 0.85）；边界 case 标记 ambiguous |
| 特殊 | 这是导师重点关注的模块，测试必须覆盖误合并和漏合并两个方向 |

---

### WP-3.2 增量更新机制

| 项目 | 内容 |
|------|------|
| 对应模块 | `collectors/incremental.py`（新建） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.4（sync_state 表）, WP-1.6 |
| 输出 | IncrementalUpdater：compute_hash / check_stale / update_author_papers / update_citation_counts |
| 验收 | 单元测试：首次同步写入 sync_state → 二次同步检测 hash 相同跳过 → 修改数据后 hash 变化触发更新 → 只更新变化字段 |

---

### WP-3.3 PubMed 适配器

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/pubmed.py` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.2 |
| 输出 | PubmedAdapter：search / get_by_pmid / get_author_papers |
| 验收 | 集成测试：搜索 "CRISPR gene editing" 返回结果；按 PMID 获取完整元数据（含 MeSH） |
| 特殊 | E-utilities API（esearch + efetch）；XML 解析；无 key 3 req/s；supports citations=False, references=False |

---

### WP-3.4 IEEE 适配器

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/ieee.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.2 |
| 输出 | IeeeAdapter：search / get_by_doi |
| 验收 | 集成测试（需 API key）：搜索 "transformer" 返回结果；无 key 时优雅降级（跳过 + 警告） |
| 特殊 | 需申请 API key（Config.ieee_api_key）；200 calls/day 免费层；supports 有限 |

---

### WP-3.5 信息增强升级

| 项目 | 内容 |
|------|------|
| 对应模块 | `processors/enricher.py`（升级） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-2.3, WP-3.1 |
| 输出 | 跨源补充缺失字段（abstract / PDF / citation_count / venue_type / fields_of_study） |
| 验收 | 单元测试：Paper 缺 abstract → enricher 从 S2 补充 → evidence_list 新增一条 |

---

### WP-3.6 CLI update 命令

| 项目 | 内容 |
|------|------|
| 对应模块 | `cli.py`（扩展） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-3.2 |
| 输出 | `ai update --author <name>` / `ai update --all --stale 7d` |
| 验收 | 端到端：首次 update 全量同步 → 二次 update 只拉增量 → 输出变更统计 |

---

### WP-3.7 Phase 3 集成测试

| 项目 | 内容 |
|------|------|
| 对应模块 | `tests/integration/` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | verification-before-completion |
| 依赖 | WP-3.1 ~ WP-3.6 |
| 验收 | Batch 3 五条验收条件逐条 PASS；消歧无误合并；增量不重复拉取 |

---

## 5. Batch 4：Phase 4 — 反爬 + 完善

**目标**：Google Scholar 可用 + 系统健壮 + 文档完整。

**批次验收条件**：
1. Google Scholar 搜索 "Geoffrey Hinton" 返回结果（需 scrapling[fetchers] 已安装）
2. 单源失败不阻塞整体（模拟 OpenAlex 超时，其余源正常返回）
3. 所有源都失败时抛 SourceUnavailableError 附带每源原因
4. 批量采集 100 篇论文无内存泄漏、无 rate limit 触发
5. 文档站点可构建，示例可运行

---

### WP-4.1 Google Scholar 适配器

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/google_scholar.py` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | test-driven-development |
| 依赖 | WP-1.2 + scrapling[fetchers] 已安装 |
| 输出 | GoogleScholarAdapter：search / get_author_papers / get_paper_citations / get_author_profile |
| 验收 | 集成测试（需网络）：搜索返回 ≥5 条结果；学者主页解析正确；被引列表可用 |
| 特殊 | StealthyFetcher + adaptive=True；Spider 模式带 checkpoint；import 时检测可用性，不可用则注册为 disabled |

---

### WP-4.2 代理轮换 + 自适应追踪

| 项目 | 内容 |
|------|------|
| 对应模块 | `sources/gateway.py`（扩展）, `config.py`（扩展） |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-4.1 |
| 输出 | ProxyRotator 配置集成 + Scrapling adaptive 元素存储路径配置 |
| 验收 | 单元测试：代理列表轮换逻辑正确；adaptive 存储路径可配置 |

---

### WP-4.3 完整错误处理 + 降级

| 项目 | 内容 |
|------|------|
| 对应模块 | 全局（collectors/, sources/, __init__.py） |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | systematic-debugging |
| 依赖 | 全部适配器完成 |
| 输出 | 统一错误处理：单源失败降级、全源失败异常、SQLite 锁降级、rate limit 自动等待 |
| 验收 | 单元测试：mock 各源失败场景 → 验证降级行为正确 → 错误信息包含每源失败原因 |

---

### WP-4.4 性能优化

| 项目 | 内容 |
|------|------|
| 对应模块 | collectors/, graph/ |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-4.3 |
| 输出 | 批量采集并发优化、NetworkX 图缓存 LRU 淘汰、SQLite 连接池 |
| 验收 | 性能测试：100 篇论文批量采集 < 60s；图缓存超 5000 节点时 LRU 淘汰生效 |

---

### WP-4.5 文档 + 示例

| 项目 | 内容 |
|------|------|
| 对应模块 | `docs/`, `examples/`, `README.md`, `SKILL.md` |
| 执行 Agent | **qoderclicn** |
| 审核 Agent | Codex |
| 最低技能 | 无额外 |
| 依赖 | WP-4.3 |
| 输出 | MkDocs 文档站点更新、3 个可运行示例（基础查询 / 递归浏览 / 增量更新）、README 更新、SKILL.md 更新 |
| 验收 | `mkdocs build` 成功；示例脚本可直接运行；README 快速开始 5 分钟内跑通 |

---

### WP-4.6 Phase 4 集成测试

| 项目 | 内容 |
|------|------|
| 对应模块 | `tests/` |
| 执行 Agent | **Codex** |
| 审核 Agent | qoderclicn |
| 最低技能 | verification-before-completion |
| 依赖 | WP-4.1 ~ WP-4.5 |
| 验收 | Batch 4 五条验收条件逐条 PASS；全量回归测试通过；覆盖率 ≥ 85% |

---

## 6. 执行节奏与 Checkpoint

### 批次流转

```
3A 批准 → 3B 批准 → Batch 1 执行 → Batch 1 阶段包 → 用户批准
→ Batch 2 执行 → Batch 2 阶段包 → 用户批准
→ Batch 3 执行 → Batch 3 阶段包 → 用户批准
→ Batch 4 执行 → Batch 4 阶段包 → 用户批准
→ 独立测试验证 → 交付关闭 → 用户最终接受
```

### 批次内 Checkpoint

每个 Batch 内，主 Agent 在以下节点检查实际文件和测试：

1. 每个 WP 完成后：检查 diff、运行该 WP 的单元测试
2. 依赖汇合点：如 WP-1.6 依赖 1.3+1.4+1.5，三者都完成后做一次集成 smoke
3. 批次末尾：完整集成测试 + 覆盖率 + 阶段包编写

### 阶段包内容（每批次提交给用户）

- 当前 Batch 和版本
- 已完成工作包列表及状态
- 可验证产物（可运行的命令）
- 实际测试证据（输出截图/日志）
- 与 3A 基线的偏差（如有）
- 风险和未解决项
- 推荐决定（进入下一 Batch / 需要修复）
- 批准后的下一步

---

## 7. 并行策略

### 可并行的工作包

| 并行组 | 工作包 | 条件 |
|--------|--------|------|
| Batch 1 第一波 | WP-1.1 + WP-1.2 | 无依赖，文件互斥 |
| Batch 1 第二波 | WP-1.3 + WP-1.4 | 都只依赖 1.1/1.2，文件互斥 |
| Batch 2 第一波 | WP-2.1 + WP-2.2 + WP-2.4 | 文件互斥，接口冻结 |
| Batch 3 第一波 | WP-3.1 + WP-3.2 + WP-3.3 + WP-3.4 | 文件互斥 |
| Batch 4 | WP-4.4 + WP-4.5 | 文件互斥 |

### 不可并行

- WP-1.5 依赖 WP-1.4 的接口定义
- WP-1.6 依赖 WP-1.3 + 1.4 + 1.5 全部完成
- WP-2.5 依赖 WP-2.4 的图结构
- 同一文件的修改不并行

### 并行约束

- 并行工作包的测试资源不冲突（各自使用独立临时 SQLite 文件）
- 接口冻结后才可并行（如 BaseSourceAdapter 接口在 WP-1.1 完成后冻结）
- 主 Agent 在并行 WP 都完成后做一次合并检查

---

## 8. 回退策略

| 问题类型 | 回退到 |
|----------|--------|
| 单个 WP 实现缺陷 | 该 WP 内修复，续跑原会话 |
| 模型/接口设计错误 | WP-1.1（数据模型）或对应基线 WP |
| 源 API 变更导致适配器失效 | 对应适配器 WP |
| 去重/消歧算法逻辑错误 | WP-1.5 / WP-3.1 |
| 集成问题（多 WP 交互） | 主 Agent 诊断后开修复单 |
| 需求/范围变更 | 退回 3A 明确需求 |

修复后重跑受影响的测试和审核。旧审核结论不覆盖新代码。

---

## 9. 成本估算

| Batch | 工作包数 | 预估 Codex 调用 | 预估 qoderclicn 调用 | 审核轮次 |
|-------|---------|----------------|---------------------|---------|
| 1 | 8 | 4（实现）+ 4（审核） | 4（实现）+ 4（审核） | 8 |
| 2 | 7 | 3 + 4 | 4 + 3 | 7 |
| 3 | 7 | 3 + 4 | 4 + 3 | 7 |
| 4 | 6 | 3 + 3 | 3 + 3 | 6 |
| **合计** | **28** | **~28** | **~28** | **28** |

每个工作包预估 1-2 轮实现 + 1 轮审核 + 0-1 轮修复。

---

*文档版本: v1.0*
*日期: 2026-07-26*
*状态: 待用户批准（3B 派发执行计划）*
