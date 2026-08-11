# Academic Intelligence — 技术设计文档 v2.0

> 本文档是 3A 技术设计基线，取代 v1.0（2026-07-21）。
> 核心变更：抓取层全面采用 Scrapling 框架；新增图谱层支持递归知识浏览；新增作者消歧模块。

---

## 1. 系统定位

Academic Intelligence 是一个纯 Python 库 + CLI，用于多源学术数据采集、融合、存储和递归图谱浏览。它不是 Web 平台，不是 Agent 框架，不内置 LLM。

核心能力：

- 从 6+ 学术数据源稳定获取论文、作者、引用数据
- 多源去重融合，每条数据带证据链和置信度
- 作者身份消歧（ID 直连 + 启发式聚类）
- 递归知识图谱：从任意节点出发逐层展开关联实体
- 增量更新：只拉变化，不全量重爬
- 本地 SQLite 持久化，零外部依赖

产品愿景：用户输入一篇论文或一个作者，系统自动构建以该实体为中心的知识子图，支持逐层递归浏览——点作者看论文，点论文看引用，点引用再展开下一层。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                         对外接口层                                    │
│              Python API (AcademicIntelligence)  +  CLI (Typer)        │
├─────────────────────────────────────────────────────────────────────┤
│                         图谱层 (Graph)                                │
│         NetworkX 会话图 + 递归遍历 + 懒加载 + 子图缓存               │
├─────────────────────────────────────────────────────────────────────┤
│                         处理层 (Processors)                           │
│     去重融合 │ 作者消歧 │ 信息增强 │ 置信度评分 │ 数据校验            │
├─────────────────────────────────────────────────────────────────────┤
│                         源适配层 (Source Adapters)                    │
│   arXiv │ OpenAlex │ Semantic Scholar │ PubMed │ IEEE │ Google Scholar│
├─────────────────────────────────────────────────────────────────────┤
│                         抓取层 (Scrapling)                            │
│     Fetcher/AsyncFetcher │ DynamicFetcher │ StealthyFetcher │ Spider  │
├─────────────────────────────────────────────────────────────────────┤
│                         存储层 (Storage)                              │
│              SQLite (SQLAlchemy 2.0)  │  JSON (可选)                  │
└─────────────────────────────────────────────────────────────────────┘
```

与 v1.0 的关键差异：

- 抓取层：v1.0 自建 httpx + proxy + rate_limiter + retry + cache → v2.0 全部由 Scrapling 承担
- 新增图谱层：v1.0 无图遍历能力 → v2.0 NetworkX 会话图 + expand() API
- 新增作者消歧：v1.0 仅按名字匹配 → v2.0 ID 直连 + 启发式聚类
- utils/ 层：v1.0 的 http.py、proxy.py、rate_limiter.py、retry.py、cache.py 全部移除，由 Scrapling 内置能力替代

---

## 3. 抓取层：Scrapling 集成

### 3.1 为什么用 Scrapling

自建 HTTP 客户端 + 反爬层的问题：Google Scholar 无官方 API，反爬策略频繁变化，自维护解析逻辑和反爬对抗的成本极高且不稳定。Scrapling（v0.4.11，71.3k stars，BSD-3）提供：

- 三层递进抓取（纯 HTTP → 浏览器渲染 → 隐身反检测）
- 内置代理轮换、频率控制、重试、checkpoint 暂停/恢复
- 自适应元素追踪（页面改版后自动重新定位）
- Spider API 支持大规模并发爬取
- lxml 解析性能（比 BeautifulSoup 快 700+ 倍）

### 3.2 各源对应的 Scrapling 层

| 数据源 | Scrapling 层 | 原因 |
|--------|-------------|------|
| arXiv | AsyncFetcher | 有正式 REST API，返回 Atom XML |
| OpenAlex | AsyncFetcher | 有正式 REST API，返回 JSON，无需认证 |
| Semantic Scholar | AsyncFetcher | 有 REST API，有 rate limit（100 req/5min） |
| PubMed | AsyncFetcher | E-utilities API，返回 XML/JSON |
| IEEE Xplore | AsyncFetcher | 有 API（需申请 key），返回 JSON |
| Google Scholar | StealthyFetcher / Spider | 无 API，Cloudflare 反爬，需浏览器渲染+指纹伪造 |

### 3.3 调用契约

源适配层不直接 import scrapling 顶层，而是通过一个薄封装 `FetchGateway` 统一调用：

```python
class FetchGateway:
    """对 Scrapling 的统一封装，隔离源适配层与 Scrapling 版本变化"""

    async def get_json(self, url: str, **kwargs) -> dict:
        """API 源：AsyncFetcher.get → 解析 JSON"""

    async def get_xml(self, url: str, **kwargs) -> etree._Element:
        """API 源：AsyncFetcher.get → 解析 XML"""

    async def get_html_stealth(self, url: str, **kwargs) -> Selector:
        """反爬源：StealthyFetcher.fetch → 返回 Scrapling Selector"""

    async def crawl(self, spider_cls: type, **kwargs) -> CrawlResult:
        """批量爬取：Spider API，带 checkpoint"""
```

### 3.4 频率控制与容错

不再自建 RateLimiter / RetryHandler / ProxyPool。配置通过 Scrapling 原生机制：

- API 源频率：Spider 的 `download_delay` + `concurrent_requests_per_domain`
- 代理：Scrapling 内置 `ProxyRotator`，配置传入 FetchGateway
- 重试：Spider 的 `max_blocked_retries` + `is_blocked()` 钩子
- 断点续爬：Spider 的 `crawldir` checkpoint 机制
- Google Scholar 自适应：Scrapling 的 `adaptive=True` 元素追踪

### 3.5 依赖策略

```toml
[project]
dependencies = [
    "scrapling>=0.4.11",       # 基础包：lxml + curl_cffi + 解析器
    "pydantic>=2.0",
    "sqlalchemy>=2.0",
    "networkx>=3.0",
    "typer>=0.9",
]

[project.optional-dependencies]
stealth = ["scrapling[fetchers]"]   # 需要浏览器渲染/反爬时安装
all = ["scrapling[all]"]
```

仅使用 API 源时不需要安装 playwright/patchright。Google Scholar 适配器在 import 时检测 StealthyFetcher 是否可用，不可用则跳过该源并警告。

---

## 4. 数据模型

### 4.1 核心实体

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum


class SourceType(str, Enum):
    ARXIV = "arxiv"
    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    PUBMED = "pubmed"
    IEEE = "ieee"
    GOOGLE_SCHOLAR = "google_scholar"


class Evidence(BaseModel):
    """证据链：每条数据的来源追溯"""
    source: SourceType
    source_id: str                    # 该源内的原始 ID（arXiv ID / DOI / PMID 等）
    source_url: str                   # 原始 URL
    collected_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    raw_data: Optional[Dict[str, Any]] = None


class Paper(BaseModel):
    """论文实体"""
    id: str                           # 内部主键（UUID 或规范化 DOI）
    title: str
    authors: List["AuthorRef"] = Field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    venue_type: Optional[str] = None  # journal / conference / preprint
    abstract: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pmid: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    citation_count: Optional[int] = None
    reference_count: Optional[int] = None
    keywords: List[str] = Field(default_factory=list)
    fields_of_study: List[str] = Field(default_factory=list)
    evidence_list: List[Evidence] = Field(default_factory=list)
    # 图谱关系（懒加载，不随主对象序列化）
    references: Optional[List[str]] = None    # 引用的论文 ID 列表
    citations: Optional[List[str]] = None     # 被引论文 ID 列表


class AuthorRef(BaseModel):
    """论文中的作者引用（轻量，指向 Author 实体）"""
    author_id: Optional[str] = None   # 已消歧时指向 Author.id
    name: str                         # 原始署名
    position: int                     # 作者顺序（1-based）
    is_corresponding: bool = False
    affiliation: Optional[str] = None


class Author(BaseModel):
    """学者实体"""
    id: str                           # 内部主键
    canonical_name: str               # 规范化姓名
    aliases: List[str] = Field(default_factory=list)
    orcid: Optional[str] = None
    semantic_scholar_id: Optional[str] = None
    openalex_id: Optional[str] = None
    affiliation: Optional[str] = None
    affiliation_history: List[Dict[str, Any]] = Field(default_factory=list)
    h_index: Optional[int] = None
    citation_count: Optional[int] = None
    paper_count: Optional[int] = None
    interests: List[str] = Field(default_factory=list)
    homepage: Optional[str] = None
    profile_urls: Dict[str, str] = Field(default_factory=dict)  # source → url
    disambiguation_status: str = "auto"  # auto / confirmed / ambiguous
    evidence_list: List[Evidence] = Field(default_factory=list)


class CitationEdge(BaseModel):
    """引用关系边"""
    citing_paper_id: str
    cited_paper_id: str
    context: Optional[str] = None     # 引用上下文句子（如有）
    evidence: Evidence


class CoauthorEdge(BaseModel):
    """合作关系边"""
    author_a_id: str
    author_b_id: str
    paper_ids: List[str] = Field(default_factory=list)  # 合作论文
    first_collaboration: Optional[int] = None  # 首次合作年份
    collaboration_count: int = 0
```

### 4.2 与 v1.0 模型的差异

- Paper.authors 从 `List[str]` 改为 `List[AuthorRef]`，保留作者顺序和对应关系
- Paper 新增 `arxiv_id`、`pmid`、`fields_of_study`、`reference_count`
- Paper 新增 `references` / `citations` 关系字段（图谱遍历用）
- Author 新增多源 ID（orcid、semantic_scholar_id、openalex_id）用于消歧
- Author 新增 `disambiguation_status` 标记消歧置信度
- Evidence 从单条改为 `evidence_list`（同一实体可被多源确认）
- 新增 CitationEdge 和 CoauthorEdge 作为图的边类型

---

## 5. 源适配层

### 5.1 抽象基类

```python
class BaseSourceAdapter(ABC):
    """数据源适配器抽象基类"""

    source_type: SourceType
    requires_stealth: bool = False    # 是否需要 StealthyFetcher
    rate_limit_per_minute: int = 60   # 该源的频率上限

    @abstractmethod
    async def search_papers(self, query: str, limit: int = 20) -> List[Paper]:
        """按关键词搜索论文"""

    @abstractmethod
    async def get_paper_by_id(self, source_id: str) -> Optional[Paper]:
        """按源内 ID 获取单篇论文（DOI / arXiv ID / PMID 等）"""

    @abstractmethod
    async def get_paper_references(self, paper: Paper) -> List[Paper]:
        """获取一篇论文的参考文献列表"""

    @abstractmethod
    async def get_paper_citations(self, paper: Paper) -> List[Paper]:
        """获取引用某篇论文的论文列表"""

    @abstractmethod
    async def get_author_papers(self, author: Author, limit: int = 100) -> List[Paper]:
        """获取某学者的论文列表"""

    @abstractmethod
    async def get_author_profile(self, author_id: str) -> Optional[Author]:
        """获取学者详细资料"""

    def supports(self, capability: str) -> bool:
        """声明该源支持哪些能力（并非所有源都能做所有事）"""
```

### 5.2 各源能力矩阵

| 能力 | arXiv | OpenAlex | Semantic Scholar | PubMed | IEEE | Google Scholar |
|------|-------|----------|-----------------|--------|------|---------------|
| 搜索论文 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 按 ID 取论文 | ✅ (arXiv ID) | ✅ (DOI/OpenAlex ID) | ✅ (DOI/S2 ID) | ✅ (PMID) | ✅ (DOI) | ❌ |
| 参考文献列表 | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ |
| 被引列表 | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ |
| 学者论文列表 | ❌ (按作者搜) | ✅ | ✅ | ✅ | ❌ | ✅ |
| 学者资料 | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ |
| 引用数 | ❌ | ✅ | ✅ | ❌ | ✅ | ✅ |
| PDF 链接 | ✅ | ✅ (部分) | ✅ (部分) | ✅ (PMC) | ❌ (付费) | ✅ (部分) |
| 需要反爬 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

### 5.3 各源适配要点

**arXiv**
- API：`http://export.arxiv.org/api/query`，Atom XML 格式
- 限制：无 rate limit 官方声明，但建议 3 秒间隔
- 特殊：只有预印本，无引用数据，无被引列表
- Scrapling 层：`AsyncFetcher.get()` → lxml 解析 XML

**OpenAlex**
- API：`https://api.openalex.org/works`、`/authors`，JSON 格式
- 限制：无需 key，100k requests/day，建议 polite pool（加 mailto 参数）
- 特殊：数据最全（2.5 亿+ 作品），有 author disambiguation ID，有 concepts/topics
- Scrapling 层：`AsyncFetcher.get()` → JSON 解析

**Semantic Scholar**
- API：`https://api.semanticscholar.org/graph/v1/paper`、`/author`，JSON
- 限制：无 key 100 req/5min；有 key 可提升
- 特殊：有 S2 author ID（已做消歧），有 citation context，有 TLDR 摘要
- Scrapling 层：`AsyncFetcher.get()` + download_delay=3

**PubMed**
- API：E-utilities（esearch + efetch），XML/JSON
- 限制：无 key 3 req/s，有 key 10 req/s
- 特殊：只覆盖生物医学，有 MeSH 术语，PMC 有免费全文
- Scrapling 层：`AsyncFetcher.get()` → XML 解析

**IEEE Xplore**
- API：`https://ieeexploreapi.ieee.org/api/v1/search/articles`，JSON
- 限制：需申请 API key，200 calls/day（免费层）
- 特殊：工程领域权威，但免费层数据有限
- Scrapling 层：`AsyncFetcher.get()` + API key header

**Google Scholar**
- 无 API，HTML 解析
- 反爬：Cloudflare + IP 封禁 + CAPTCHA
- 特殊：覆盖最全（含中文学术），有被引列表，有学者主页
- Scrapling 层：`StealthyFetcher` 或 Spider 模式（带 checkpoint + proxy）
- 自适应：启用 `adaptive=True`，用 SQLite 存元素指纹应对页面改版
- 降级：StealthyFetcher 不可用时跳过该源，不阻塞其他源

---

## 6. 处理层

### 6.1 去重融合 (Deduplicator)

输入：多源返回的 Paper/Author 列表（可能有重复）。

去重策略（按优先级）：

1. **精确匹配**：DOI 相同 → 同一论文；ORCID 相同 → 同一作者
2. **ID 交叉**：arXiv ID 与 DOI 的已知映射（arXiv 论文发表后会有 DOI）
3. **标题相似度**：归一化后（去标点、小写、去停用词）的 Jaccard / SequenceMatcher ≥ 0.92
4. **作者+年份+标题前缀**：标题前 50 字符相同 + 年份相同 + 至少一个作者姓氏匹配

融合策略：

- 字段级合并：每个字段取置信度最高的源的值
- 置信度计算：多源确认的字段 confidence = min(1.0, base + 0.15 × (确认源数 - 1))
- 冲突处理：同一字段多源值不同时，保留所有值到 evidence_list，取最高置信度为展示值
- 合并后 evidence_list 包含所有源的证据

### 6.2 作者消歧 (AuthorDisambiguator)

**第一层：ID 直连**

如果源数据提供了权威唯一 ID，直接使用：
- ORCID（最权威，跨源通用）
- Semantic Scholar Author ID
- OpenAlex Author ID

匹配规则：任意两个记录共享同一个权威 ID → 合并为同一 Author。

**第二层：启发式聚类**

无权威 ID 时，用以下特征判断两个同名记录是否为同一人：

```python
class DisambiguationFeatures:
    name_similarity: float        # 姓名变体相似度（Wei Zhang / W. Zhang / Zhang Wei）
    affiliation_overlap: float    # 机构重叠度
    coauthor_overlap: float       # 合作者网络重叠度
    topic_similarity: float       # 研究主题余弦相似度（基于 keywords/fields_of_study）
    year_range_overlap: float     # 活跃年份重叠度
    venue_overlap: float          # 发表期刊/会议重叠度
```

判定规则：
- 综合得分 ≥ 0.85 → 自动合并，`disambiguation_status = "auto"`
- 0.6 ≤ 得分 < 0.85 → 标记为 `ambiguous`，暂不合并，等待用户确认
- 得分 < 0.6 → 视为不同人

**第三层（接口预留，Phase 1 不实现）**：
- 用户确认接口：`ai.confirm_merge(author_a_id, author_b_id)` / `ai.confirm_split(author_id, paper_ids)`
- 确认后更新 `disambiguation_status = "confirmed"`

### 6.3 置信度评分

单源置信度基线：

| 源 | 基线 confidence | 原因 |
|----|----------------|------|
| OpenAlex | 0.90 | 数据量大、有消歧、更新频繁 |
| Semantic Scholar | 0.88 | AI 增强、有 citation context |
| arXiv | 0.95 | 一手来源（预印本本身） |
| PubMed | 0.92 | 权威医学数据库 |
| IEEE | 0.85 | 数据有限但准确 |
| Google Scholar | 0.75 | 覆盖广但解析不稳定 |

多源加成：每多一个源确认同一事实，+0.05，上限 1.0。

字段级调整：
- DOI 精确匹配：该论文所有字段 +0.05
- 有 PDF 链接可验证：+0.03
- 数据超过 2 年未更新：-0.10

### 6.4 信息增强 (Enricher)

在去重融合后，对缺失字段尝试从其他源补充：

- 缺 abstract → 尝试 OpenAlex / Semantic Scholar
- 缺 PDF → 尝试 arXiv（如有 arxiv_id）/ PubMed Central
- 缺 citation_count → 取 OpenAlex 和 Semantic Scholar 的最大值
- 缺 venue_type → 根据 venue 名称查 OpenAlex 的 source 分类
- 缺 fields_of_study → 取 OpenAlex concepts / Semantic Scholar fieldsOfStudy

### 6.5 数据校验 (Validator)

必填校验：Paper 必须有 title + 至少一个 author name。Author 必须有 canonical_name。

格式校验：DOI 格式（`10.\d{4,}/`）、年份范围（1900-当前年+1）、confidence 范围。

一致性校验：reference_count 与实际 references 列表长度一致（如都已获取）。

校验失败处理：标记 `validation_status`，不丢弃数据，降低 confidence。

---

## 7. 图谱层

### 7.1 设计原则

- SQLite 是持久化真相源（source of truth）
- NetworkX 是会话级工作图（session graph），加速遍历和图算法
- 用户当前浏览的子图加载到内存，不活跃的不加载
- 每次 expand() 先查本地 SQLite，miss 时才去源上拉

### 7.2 图结构

```python
import networkx as nx

class KnowledgeGraph:
    """会话级知识图谱"""

    def __init__(self, storage: BaseStorage):
        self._storage = storage
        self._graph = nx.DiGraph()

    # 节点类型：paper / author
    # 边类型：cites / authored_by / coauthor_with / referenced_by
```

节点属性：

```python
# Paper 节点
graph.add_node(paper_id, type="paper", title=..., year=..., citation_count=..., loaded=True)

# Author 节点
graph.add_node(author_id, type="author", name=..., affiliation=..., h_index=..., loaded=True)
```

边属性：

```python
# 引用边
graph.add_edge(citing_id, cited_id, relation="cites", evidence_source=..., collected_at=...)

# 作者边
graph.add_edge(paper_id, author_id, relation="authored_by", position=1)

# 合作边（无向，用两条有向边表示）
graph.add_edge(author_a, author_b, relation="coauthor_with", paper_count=5)
```

### 7.3 递归遍历 API

```python
class AcademicIntelligence:
    """主入口"""

    async def expand(
        self,
        entity_id: str,
        relations: List[str] = None,  # ["references", "citations", "authors", "papers", "coauthors"]
        depth: int = 1,               # 展开层数
        fetch_missing: bool = True,   # 本地没有的是否去源上拉
        sources: List[str] = None,    # 限定数据源
    ) -> ExpandResult:
        """
        从任意实体出发，展开指定关系。

        返回 ExpandResult 包含：
        - nodes: 新发现的实体列表
        - edges: 新发现的关系列表
        - graph: 当前会话图的子图视图
        - stats: 本次展开的统计（命中缓存数、新拉取数、失败数）
        """

    async def get_paper(self, identifier: str) -> Optional[Paper]:
        """按 DOI / arXiv ID / 标题获取论文（先查本地，miss 则多源拉取）"""

    async def get_author(self, identifier: str) -> Optional[Author]:
        """按 ORCID / 姓名 / 源 ID 获取学者"""

    async def subgraph(self, center_id: str, radius: int = 2) -> nx.DiGraph:
        """获取以某实体为中心、指定半径的子图"""

    async def path(self, source_id: str, target_id: str) -> List[str]:
        """两个实体间的最短关联路径"""
```

### 7.4 懒加载与缓存策略

```
expand(paper_id, relations=["references"])
    │
    ├─ 查 SQLite: 该论文的 references 是否已存储？
    │   ├─ 是 → 直接构建边，标记 loaded=True
    │   └─ 否 → 调源适配层 get_paper_references()
    │           → 拉回来的论文入 SQLite
    │           → 构建边
    │
    ├─ 对每个 reference 论文：
    │   ├─ 本地有完整数据 → 标记 loaded=True
    │   └─ 本地只有 ID/标题 → 标记 loaded=False（占位节点）
    │       └─ 用户再次 expand 该节点时才拉完整数据
    │
    └─ 返回 ExpandResult
```

占位节点（stub）：只有 ID 和标题（或连标题都没有），在图中标记 `loaded=False`。用户点到它时才触发完整拉取。这避免了深度爆炸——一篇论文引用 30 篇，不需要立刻把 30 篇的完整数据全拉回来。

### 7.5 深度控制

- 默认 depth=1（展开一层）
- 最大允许 depth=3（防止指数爆炸）
- 每层最大节点数限制：默认 50（可配置）
- 超过限制时截断并在 stats 中报告 `truncated: true`

---

## 8. 存储层

### 8.1 SQLite Schema

```sql
-- 论文表
CREATE TABLE papers (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    venue TEXT,
    venue_type TEXT,
    abstract TEXT,
    doi TEXT UNIQUE,
    arxiv_id TEXT,
    pmid TEXT,
    url TEXT,
    pdf_url TEXT,
    citation_count INTEGER,
    reference_count INTEGER,
    keywords TEXT,          -- JSON array
    fields_of_study TEXT,   -- JSON array
    validation_status TEXT DEFAULT 'valid',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 作者表
CREATE TABLE authors (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    aliases TEXT,           -- JSON array
    orcid TEXT UNIQUE,
    semantic_scholar_id TEXT,
    openalex_id TEXT,
    affiliation TEXT,
    h_index INTEGER,
    citation_count INTEGER,
    paper_count INTEGER,
    interests TEXT,         -- JSON array
    homepage TEXT,
    profile_urls TEXT,      -- JSON object
    disambiguation_status TEXT DEFAULT 'auto',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 论文-作者关系表
CREATE TABLE authorships (
    paper_id TEXT REFERENCES papers(id),
    author_id TEXT REFERENCES authors(id),
    position INTEGER,
    is_corresponding BOOLEAN DEFAULT 0,
    raw_name TEXT,          -- 原始署名（消歧前）
    affiliation TEXT,       -- 该论文中的机构
    PRIMARY KEY (paper_id, author_id)
);

-- 引用关系表
CREATE TABLE citations (
    citing_paper_id TEXT REFERENCES papers(id),
    cited_paper_id TEXT REFERENCES papers(id),
    context TEXT,
    source TEXT,
    collected_at TEXT,
    PRIMARY KEY (citing_paper_id, cited_paper_id)
);

-- 合作关系表
CREATE TABLE coauthorships (
    author_a_id TEXT REFERENCES authors(id),
    author_b_id TEXT REFERENCES authors(id),
    paper_count INTEGER DEFAULT 0,
    first_year INTEGER,
    last_year INTEGER,
    PRIMARY KEY (author_a_id, author_b_id)
);

-- 证据表（每个实体的每条证据）
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,   -- 'paper' / 'author'
    entity_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT,
    collected_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    raw_data TEXT               -- JSON，原始响应快照
);

-- 增量更新追踪表
CREATE TABLE sync_state (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    last_hash TEXT,             -- 数据指纹，用于变更检测
    PRIMARY KEY (entity_type, entity_id, source)
);

-- 索引
CREATE INDEX idx_papers_doi ON papers(doi);
CREATE INDEX idx_papers_year ON papers(year);
CREATE INDEX idx_papers_title ON papers(title);
CREATE INDEX idx_authors_name ON authors(canonical_name);
CREATE INDEX idx_authors_orcid ON authors(orcid);
CREATE INDEX idx_citations_cited ON citations(cited_paper_id);
CREATE INDEX idx_authorships_author ON authorships(author_id);
CREATE INDEX idx_evidence_entity ON evidence(entity_type, entity_id);
```

### 8.2 ORM 层

使用 SQLAlchemy 2.0 async 模式。Repository 模式封装查询：

```python
class PaperRepository:
    async def get_by_id(self, paper_id: str) -> Optional[Paper]
    async def get_by_doi(self, doi: str) -> Optional[Paper]
    async def get_by_arxiv_id(self, arxiv_id: str) -> Optional[Paper]
    async def search(self, query: str, year_range: tuple = None, limit: int = 20) -> List[Paper]
    async def save(self, paper: Paper) -> None
    async def update(self, paper: Paper) -> None
    async def get_references(self, paper_id: str) -> List[str]
    async def get_citations(self, paper_id: str) -> List[str]

class AuthorRepository:
    async def get_by_id(self, author_id: str) -> Optional[Author]
    async def get_by_orcid(self, orcid: str) -> Optional[Author]
    async def search_by_name(self, name: str) -> List[Author]
    async def save(self, author: Author) -> None
    async def merge(self, keep_id: str, merge_id: str) -> None
    async def get_papers(self, author_id: str) -> List[str]
    async def get_coauthors(self, author_id: str) -> List[str]
```

---

## 9. 增量更新

### 9.1 变更检测

每次同步时，对比 `sync_state.last_hash` 与当前数据指纹：

```python
def compute_hash(entity: Union[Paper, Author]) -> str:
    """对关键字段计算 SHA-256，用于变更检测"""
    # Paper: title + year + citation_count + authors 排序拼接
    # Author: name + affiliation + h_index + paper_count
```

hash 相同 → 跳过；hash 不同 → 更新并记录变更历史。

### 9.2 更新策略

- 引用数：每次 expand 时顺带刷新（citation_count 变化最频繁）
- 论文列表：按学者维度，记录 last_synced_at，超过 7 天重新拉取
- 作者资料：h_index / affiliation 变化慢，30 天刷新一次
- 新论文检测：对关注的学者，定期查 OpenAlex 的 `from_created_date` 过滤

### 9.3 增量拉取

```python
async def update_author_papers(self, author_id: str) -> IncrementalResult:
    """增量更新某学者的论文列表"""
    last_sync = await self.sync_state.get("author", author_id, "openalex")
    new_papers = await self.openalex.get_author_papers_since(author_id, last_sync)
    # 只拉 last_sync 之后新发表的论文
    # 已有论文只更新 citation_count
    return IncrementalResult(new=..., updated=..., unchanged=...)
```

---

## 10. 对外接口

### 10.1 Python API

```python
import asyncio
from academic_intelligence import AcademicIntelligence, Config

async def main():
    config = Config(
        sources=["openalex", "semantic_scholar", "arxiv"],
        storage_path="./academic.db",
        enable_google_scholar=False,  # 需要 scrapling[fetchers]
        max_expand_depth=3,
        max_expand_nodes=50,
    )
    ai = AcademicIntelligence(config)

    # 获取一篇论文
    paper = await ai.get_paper("10.1038/nature14539")

    # 递归展开
    result = await ai.expand(paper.id, relations=["references", "authors"])
    print(f"发现 {len(result.nodes)} 个新实体")

    # 继续展开某个作者
    author = result.nodes[0]
    author_result = await ai.expand(author.id, relations=["papers", "coauthors"])

    # 查看子图
    subgraph = await ai.subgraph(paper.id, radius=2)
    print(f"子图包含 {subgraph.number_of_nodes()} 个节点")

asyncio.run(main())
```

### 10.2 CLI

```bash
# 搜索论文
ai search "deep learning" --year 2020-2024 --sources openalex,ss --limit 10

# 获取论文详情
ai paper "10.1038/nature14539"

# 展开引用图
ai expand "10.1038/nature14539" --relations references,citations --depth 2

# 获取学者信息
ai author "Geoffrey Hinton" --source openalex

# 学者的论文列表
ai author-papers "Geoffrey Hinton" --year 2015-2024

# 增量更新
ai update --author "Geoffrey Hinton"
ai update --all --stale 7d

# 导出子图
ai export --center "10.1038/nature14539" --radius 2 --format json --output graph.json

# 统计
ai stats
```

---

## 11. 错误处理与降级

### 11.1 异常层次

```python
class AcademicIntelligenceError(Exception): ...

class SourceError(AcademicIntelligenceError):
    """数据源相关错误"""
    source: SourceType

class SourceUnavailableError(SourceError):
    """源不可达（网络/服务宕机）"""

class RateLimitError(SourceError):
    """触发频率限制"""
    retry_after: Optional[int]

class SourceBlockedError(SourceError):
    """被反爬拦截（Google Scholar CAPTCHA 等）"""

class DataError(AcademicIntelligenceError):
    """数据相关错误"""

class DisambiguationError(DataError):
    """消歧失败"""

class StorageError(AcademicIntelligenceError):
    """存储层错误"""
```

### 11.2 降级策略

- 单源失败不阻塞整体：多源并行采集，某源超时/报错时记录到 errors，其余源结果正常融合
- Google Scholar 不可用时：静默跳过，不影响 API 源的结果
- 所有源都失败时：抛 SourceUnavailableError，附带每源的失败原因
- 存储层只读降级：SQLite 锁定时允许从内存缓存读取（不写入）

---

## 12. 配置

```python
class Config(BaseModel):
    # 数据源
    sources: List[str] = ["openalex", "semantic_scholar", "arxiv"]
    source_priority: List[str] = ["openalex", "semantic_scholar", "arxiv", "pubmed", "ieee", "google_scholar"]

    # 存储
    storage_path: str = "./academic_intelligence.db"
    storage_type: str = "sqlite"  # sqlite / json

    # 抓取
    enable_google_scholar: bool = False
    proxy: Optional[str] = None
    proxy_list: Optional[List[str]] = None
    download_delay: float = 1.0
    max_concurrent_requests: int = 4

    # 图谱
    max_expand_depth: int = 3
    max_expand_nodes: int = 50
    graph_cache_size: int = 5000  # NetworkX 最大节点数

    # 消歧
    auto_merge_threshold: float = 0.85
    ambiguous_threshold: float = 0.60

    # 增量
    paper_refresh_days: int = 7
    author_refresh_days: int = 30

    # 置信度
    min_confidence: float = 0.3  # 低于此值的数据不入库
```

---

## 13. 模块结构（更新后）

```
academic_intelligence/
├── __init__.py                    # 主入口（AcademicIntelligence 类）
├── cli.py                         # CLI（Typer）
├── config.py                      # 配置模型
├── core/
│   ├── __init__.py
│   ├── models.py                  # Pydantic 数据模型（v2）
│   ├── exceptions.py              # 异常层次
│   ├── constants.py               # 常量
│   └── types.py                   # 类型定义
├── sources/                       # 源适配层
│   ├── __init__.py
│   ├── base.py                    # BaseSourceAdapter
│   ├── gateway.py                 # FetchGateway（Scrapling 封装）
│   ├── arxiv.py
│   ├── openalex.py
│   ├── semantic_scholar.py
│   ├── pubmed.py
│   ├── ieee.py
│   └── google_scholar.py
├── processors/                    # 处理层
│   ├── __init__.py
│   ├── deduplicator.py            # 去重融合
│   ├── disambiguator.py           # 作者消歧（新增）
│   ├── enricher.py                # 信息增强
│   ├── scorer.py                  # 置信度评分（新增）
│   └── validator.py               # 数据校验
├── graph/                         # 图谱层（新增）
│   ├── __init__.py
│   ├── knowledge_graph.py         # KnowledgeGraph 类
│   ├── traversal.py               # 递归遍历逻辑
│   └── cache.py                   # 图缓存管理
├── storage/                       # 存储层
│   ├── __init__.py
│   ├── base.py                    # BaseStorage / Repository 接口
│   ├── sqlite_store.py            # SQLite 实现
│   ├── models.py                  # SQLAlchemy ORM 模型
│   └── json_store.py              # JSON 实现（可选）
└── collectors/                    # 采集编排
    ├── __init__.py
    ├── base.py                    # BaseCollector
    ├── paper_collector.py         # 论文采集编排
    ├── author_collector.py        # 学者采集编排
    └── incremental.py             # 增量更新逻辑（新增）
```

与 v1.0 的差异：
- 移除 `utils/`（http.py、proxy.py、rate_limiter.py、retry.py、cache.py）→ Scrapling 内置
- 新增 `graph/`（knowledge_graph.py、traversal.py、cache.py）
- 新增 `sources/gateway.py`（FetchGateway）
- 新增 `processors/disambiguator.py`、`processors/scorer.py`
- 新增 `collectors/incremental.py`
- 新增 `config.py` 独立配置模块

---

## 14. 技术栈（更新后）

| 层级 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | asyncio、类型提示 |
| 抓取 | Scrapling ≥ 0.4.11 | 替代 httpx + 自建反爬 |
| 数据验证 | Pydantic v2 | 模型定义与校验 |
| 数据库 | SQLite + SQLAlchemy 2.0 | 异步 ORM |
| 图计算 | NetworkX ≥ 3.0 | 会话级图遍历 |
| CLI | Typer | 类型安全 CLI |
| 测试 | pytest + pytest-asyncio | 异步测试 |
| 文档 | MkDocs | 文档站点 |

---

## 15. 实施阶段（修订）

### Phase 1：核心通路（MVP）

目标：从 OpenAlex 拉一篇论文的完整数据，存入 SQLite，通过 API 可查。

- 数据模型 v2 实现（models.py）
- FetchGateway + OpenAlex 适配器
- SQLite 存储（papers + authors + authorships + evidence）
- 基础去重（DOI 精确匹配）
- 基础置信度评分
- Python API：get_paper() / get_author()
- CLI：ai paper / ai author / ai search
- 单元测试

### Phase 2：多源 + 图谱

目标：多源采集 + 递归展开可用。

- arXiv + Semantic Scholar 适配器
- 多源去重融合（标题相似度 + ID 交叉）
- KnowledgeGraph + expand() API
- 懒加载 + 占位节点
- CLI：ai expand
- 集成测试

### Phase 3：消歧 + 增量

目标：作者消歧可用，增量更新可用。

- 作者消歧（ID 直连 + 启发式聚类）
- 增量更新机制（sync_state + hash 变更检测）
- PubMed + IEEE 适配器
- 信息增强
- CLI：ai update

### Phase 4：反爬 + 完善

目标：Google Scholar 可用，系统健壮。

- Google Scholar 适配器（StealthyFetcher + Spider）
- 代理轮换配置
- 自适应元素追踪
- 完整错误处理和降级
- 性能优化（批量采集、并发控制）
- 完整文档 + 示例

---

## 16. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Google Scholar 反爬升级 | 该源不可用 | 降级跳过；Scrapling 社区持续更新对抗 |
| OpenAlex / S2 API 变更 | 适配器失效 | 适配器隔离；版本化响应解析 |
| 作者消歧误合并 | 数据污染 | 保守阈值（0.85）；ambiguous 标记；用户确认接口预留 |
| 图深度爆炸 | 内存/时间 | max_depth=3 + max_nodes=50 + 懒加载 |
| SQLite 并发写入 | 锁等待 | WAL 模式；写操作串行化 |
| Scrapling 大版本升级 | 接口不兼容 | FetchGateway 隔离；锁定最低版本 |

---

## 17. 验收条件

Phase 1 完成时，以下必须为真：

1. `ai paper "10.1038/nature14539"` 返回完整论文信息（标题、作者、年份、摘要、引用数）
2. 同一篇论文从 OpenAlex 和 Semantic Scholar 两个源获取后自动去重为一条记录
3. evidence 表包含两条记录（每源一条），confidence 正确计算
4. `ai author "Geoffrey Hinton"` 返回学者资料（机构、h-index、论文数）
5. SQLite 数据库文件可独立打开查询，schema 与本文档一致
6. 所有单元测试通过，覆盖率 ≥ 80%

---

*文档版本: v2.0*
*日期: 2026-07-26*
*状态: 待审批（3A 技术设计基线）*
