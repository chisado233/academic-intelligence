# 接口设计

## 对内接口（模块间）

| 接口 | 提供方 | 消费方 | 说明 |
|------|--------|--------|------|
| `BaseSource.supports(operation)` / `.capabilities` | `sources/base.py` | collector / facade | 声明真实支持能力；collector 不调用声明为 false 的 stub |
| `ArxivSource.get_paper_by_arxiv_id(arxiv_id)` | `sources/arxiv.py` | collector / API 调用方 | 完整 arXiv ID 的 `id_list` 精确查询；按去版本 canonical ID 过滤响应 |
| `SourceFailure` | `core/exceptions.py` | collector / CLI / API 调用方 | 结构化来源失败，同时保持字符串展示/比较兼容 |
| `KnowledgeGraph.save_snapshot/load_snapshot` | `graph/knowledge_graph.py` | CLI / facade | 版本 1 JSON、原子写、加载校验 |
| `BaseStorage.query_papers/query_authors` cursor 参数 | storage backends | facade / exporter | `order_by` + 上页最后实体 ID 的 `after`/`cursor` |
| `export_papers(..., excel_safe=False)` | `exporters.py` | CLI | 分批读取并逐行/分块写 CSV、JSONL、固定 schema Parquet；可选 Excel-safe CSV |

## 对外 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `AcademicIntelligence.source_capabilities()` | Python API | 无连接查询配置来源能力表 |
| `AcademicIntelligence.save_graph_snapshot/load_graph_snapshot(path)` | Python API | 保存/恢复当前 session graph |
| `AuthorDisambiguator.score_pair(a, b)` | Python API | 返回 `DisambiguationScore` |
| `AuthorDisambiguator.cluster(authors)` | Python API | 只聚类，不合并，返回 `list[list[Author]]` |
| `AuthorDisambiguator.disambiguate(authors)` | Python API | 合并强匹配并标记歧义，返回 `list[Author]` |
| `KnowledgeGraph.add_node/add_edge` | Python API | 写入/刷新 session graph 节点与有向边 |
| `KnowledgeGraph.load_snapshot(path, *, cache_size=None)` | Python API | 校验快照并返回新的 `KnowledgeGraph` |
| `ai expand <id> --output <snapshot>` | CLI | expand 后原子写 graph snapshot |
| `ai paper` / `ai author` / `ai author-papers` / `ai update --author` | CLI | 现有公共 API 的薄封装 |
| `ai export --snapshot <snapshot> --center <id>` | CLI | 跨进程加载 snapshot 并导出子图 |
| `ai export-papers --format csv\|jsonl\|parquet --output <path>` | CLI | 从 storage 流式导出论文；Parquet 为可选依赖 |

## 数据结构约定
<!-- 模块间共享的数据结构、枚举、常量 -->

- Cursor 是上一页最后一条实体的 `id`；后端用该实体的排序值构造 `(sort_value, id)` keyset。
- `SourceFailure` 字段为 `source/operation/error_type/message/retry_count/http_status/transient/permanent`；adapter 包装异常后仍从有界 cause/context 链恢复 retry/status。
- Graph snapshot 顶层必含 `version=1`、`directed`、`nodes`、`edges`、`node_count`、`edge_count`；未知版本、计数不符、重复实体、悬空边或空 relation 拒绝加载。

## 精确标识符与重试边界

- `collect_paper` 仅把“完整”现代/旧式 arXiv ID、可选版本、`arXiv:` 前缀或 arXiv URL 判为精确标识符，并只调用声明 `get_paper_by_arxiv_id` 能力的来源；包含 ID 的自然语言仍走普通搜索。
- HTTP 层拥有 `anti_crawl.max_retries` 内部预算。外层自动化默认不重试；若任务显式要求恢复，最多额外 1 次并遵守 `retry_after`，不得创建无界重试循环。
- 有可用记录但部分来源失败为 `PARTIAL`；所有 capable 来源在有限预算后仍不可用/限流，或缺凭据，为 `BLOCKED` 并停止。
