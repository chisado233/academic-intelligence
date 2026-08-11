# 架构设计

## 模块划分

```text
academic_intelligence/
├── sources/       # 来源适配、精确 ID 查询
├── collectors/    # 能力筛选、并发采集、融合管线
├── processors/    # 去重、消歧、评分、增量更新
├── graph/         # session graph、遍历、snapshot
├── storage/       # SQLite / JSON 持久化
└── utils/         # HTTP、限速、有限重试、缓存
```

## 模块边界

> 每个模块必须写清：职责、不负责、输入依赖、输出/被依赖

### 模块：来源错误与能力契约
- **职责**：`BaseSource` 声明操作能力；collector 生成 `SourceFailure` 并区分不支持与真实空结果；终止异常携带已消耗 retry count，包装后通过有界异常链恢复 retry/status。
- **不负责**：不改变各来源的网络实现或重试策略。
- **输入依赖**：来源适配器声明、异常上下文。
- **输出/被依赖**：`CollectionResult`、`AllSourcesFailedError`、facade 能力查询。

### 模块：精确标识符路由
- **职责**：collector 严格识别完整 arXiv ID；只派给声明精确查询能力的 adapter；arXiv adapter 使用 `id_list` 并对响应做 canonical ID 过滤。
- **不负责**：不从任意自然语言抽取嵌入 ID，不建立通用标识符注册表，不让其他来源把 arXiv ID 当 mention search。
- **输入依赖**：完整现代/旧式 arXiv ID、可选版本/前缀/URL，`BaseSource.supports`。
- **输出/被依赖**：`collect_paper` 精确单篇语义、CLI/Skill 自动化。

### 模块：Graph snapshot
- **职责**：保存/校验/加载完整 session graph，供 CLI 跨进程继续导出。
- **不负责**：不把 graph 合并进主 storage schema，不改变遍历算法。
- **输入依赖**：`KnowledgeGraph` nodes/edges。
- **输出/被依赖**：版本化 JSON snapshot、`ai expand`、`ai export`。

### 模块：分页查询与论文导出
- **职责**：storage 提供稳定 `(sort_key, id)` keyset 查询；exporter 分批写 CSV/JSONL/Parquet。
- **不负责**：不新增 pandas；pyarrow 不成为核心依赖；不导出 graph。
- **输入依赖**：SQLite/JSON storage 的 `query_papers`。
- **输出/被依赖**：Python query API 与 `ai export-papers`。

## 数据流
<!-- 核心数据从哪来、经过哪些模块、最终到哪去 -->

```text
complete arXiv ID -> collector classifier -> capable arXiv exact lookup -> canonical match
source HTTP -> finite internal retry -> wrapped exception chain -> SourceFailure metadata
source call -> SourceFailure -> CollectionResult / AllSourcesFailedError
expand -> KnowledgeGraph -> atomic snapshot -> later export --snapshot -> subgraph JSON
storage -> keyset page -> exporter row/chunk writer -> CSV / JSONL / optional Parquet
```

## 部署拓扑
<!-- 这个项目跑在哪些机器/服务上，它们之间怎么通信 -->

```

```
