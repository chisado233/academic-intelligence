# Academic Intelligence Skill — 设计文档

> **目标：** 设计一个远强于参考系统的学术情报采集 skill 模块，作为可复用的 Python 库/CLI 工具，而非 Agent 框架或 Web 平台。

---

## 1. 参考系统分析

### 1.1 参考系统功能清单

| 模块 | 功能 | 技术 | 局限 |
|------|------|------|------|
| 数据采集 | Google Scholar 学者主页、论文列表、施引论文 | Scrapy + Selenium | 仅 GS 单一源；反爬弱；无增量更新策略 |
| 权威名录 | Fellow/院士/期刊会议名单爬取 | urllib/Selenium | 各源独立脚本，无统一抽象；维护成本高 |
| 论文分类 | Venue 级别分类（CCF/SCI/Nature） | 规则 + BM25 + LLM | 准确率依赖规则维护；LLM 为本地 Ollama，不可扩展 |
| 作者分类 | Fellow/院士身份识别 | 规则 + LLM+RAG | 同名消歧弱；LLM 调用成本高 |
| 数据管理 | 作者/论文/引用关系存储 | SQLAlchemy + PostgreSQL | 新旧 DAO 并存；迁移未完成 |
| 实验评估 | 算法评测（TP/FP/FN/TN） | JSON 文件存储 | 与生产数据分离；无版本控制 |
| 订单调度 | 端到端采集任务流水线 | asyncio.Queue | 状态机简单；无失败重试策略 |
| 前端界面 | Vue Element Admin | Vue.js | 与后端耦合；无独立 API 文档 |

### 1.2 参考系统核心问题

1. **数据源单一**：仅 Google Scholar，无 arXiv、PubMed、IEEE Xplore、Semantic Scholar 等补充
2. **反爬能力弱**：Selenium 容易被检测；无代理轮换、无请求频率智能控制
3. **LLM 依赖本地**：Ollama 性能受限；无多模型 fallback
4. **架构迁移未完成**：新旧 DAO 并存；MySQL/PostgreSQL 混合；字段不一致
5. **无证据链**：采集结果无来源追溯；无可信度评分
6. **不可复用**：与 FastAPI/Web 平台深度耦合；无法作为独立库使用
7. **无增量更新**：全量重爬；无变更检测
8. **无质量评估**：无数据完整性校验；无采集成功率统计

---

## 2. 新系统设计理念

### 2.1 核心原则

- **数据源优先**：多源采集 + 去重融合，而非单源依赖
- **可复用性**：纯 Python 库 + CLI，无 Web 框架依赖
- **证据链**：每条数据记录来源、时间、可信度
- **增量更新**：变更检测 + 智能调度，避免全量重爬
- **质量保障**：采集成功率监控 + 数据完整性校验
- **可扩展性**：插件化架构，新源接入成本低

### 2.2 与参考系统的关键差异

| 维度 | 参考系统 | 新系统 |
|------|---------|--------|
| 定位 | Web 平台 + Agent | Python 库/CLI 工具 |
| 数据源 | Google Scholar 单源 | 多源（GS/arXiv/PubMed/IEEE/SS/OpenAlex） |
| 反爬 | Selenium + 简单重试 | 代理池 + 智能频率控制 + 多策略 fallback |
| 存储 | PostgreSQL + JSON 文件 | SQLite（默认）+ 可扩展 |
| LLM | 本地 Ollama | 多模型 API（可配置） |
| 增量更新 | 无 | 变更检测 + 增量采集 |
| 证据链 | 无 | 每条记录来源/时间/可信度 |
| 可复用性 | 低（与 Web 耦合） | 高（纯库 + CLI） |

---

## 3. 架构设计

### 3.1 模块架构

```
academic-intelligence/
├── academic_intelligence/          # 核心库
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py              # 数据模型（Pydantic）
│   │   ├── exceptions.py          # 异常定义
│   │   ├── constants.py           # 常量枚举
│   │   └── types.py               # 类型定义
│   ├── sources/                   # 数据源插件
│   │   ├── __init__.py
│   │   ├── base.py                # 抽象基类
│   │   ├── google_scholar.py      # Google Scholar
│   │   ├── arxiv.py               # arXiv
│   │   ├── pubmed.py              # PubMed
│   │   ├── ieee.py                # IEEE Xplore
│   │   ├── semantic_scholar.py    # Semantic Scholar
│   │   └── openalex.py            # OpenAlex
│   ├── collectors/                # 采集器
│   │   ├── __init__.py
│   │   ├── base.py                # 抽象基类
│   │   ├── author_collector.py    # 学者信息采集
│   │   ├── paper_collector.py     # 论文信息采集
│   │   └── citation_collector.py  # 引用关系采集
│   ├── processors/                # 处理器
│   │   ├── __init__.py
│   │   ├── deduplicator.py        # 去重融合
│   │   ├── enricher.py            # 信息增强
│   │   └── validator.py           # 数据校验
│   ├── storage/                   # 存储层
│   │   ├── __init__.py
│   │   ├── base.py                # 抽象基类
│   │   ├── sqlite_store.py        # SQLite 实现
│   │   └── json_store.py          # JSON 文件实现
│   ├── utils/                     # 工具
│   │   ├── __init__.py
│   │   ├── http.py                # HTTP 客户端（含反爬）
│   │   ├── proxy.py               # 代理管理
│   │   ├── rate_limiter.py        # 频率控制
│   │   ├── retry.py               # 重试策略
│   │   └── cache.py               # 缓存管理
│   └── cli.py                     # CLI 入口
├── tests/                         # 测试
├── docs/                          # 文档
├── examples/                      # 示例
├── pyproject.toml                 # 项目配置
├── README.md                      # 说明文档
└── SKILL.md                       # Skill 契约
```

### 3.2 数据模型

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

class SourceType(str, Enum):
    GOOGLE_SCHOLAR = "google_scholar"
    ARXIV = "arxiv"
    PUBMED = "pubmed"
    IEEE = "ieee"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"

class Evidence(BaseModel):
    """证据链：每条数据的来源追溯"""
    source: SourceType
    source_url: str
    collected_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    raw_data: Optional[Dict[str, Any]] = None

class Author(BaseModel):
    """学者模型"""
    id: Optional[str] = None
    name: str
    affiliation: Optional[str] = None
    email: Optional[str] = None
    homepage: Optional[str] = None
    h_index: Optional[int] = None
    citations: Optional[int] = None
    interests: List[str] = Field(default_factory=list)
    profile_url: Optional[str] = None
    evidence: Evidence
    
class Paper(BaseModel):
    """论文模型"""
    id: Optional[str] = None
    title: str
    authors: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    pdf_url: Optional[str] = None
    citations: Optional[int] = None
    keywords: List[str] = Field(default_factory=list)
    evidence: Evidence

class Citation(BaseModel):
    """引用关系模型"""
    citing_paper_id: str
    cited_paper_id: str
    evidence: Evidence

class CollectionResult(BaseModel):
    """采集结果"""
    authors: List[Author] = Field(default_factory=list)
    papers: List[Paper] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    stats: Dict[str, Any] = Field(default_factory=dict)
```

### 3.3 核心工作流

```
┌─────────────────────────────────────────────────────────────────┐
│                        采集工作流                                │
├─────────────────────────────────────────────────────────────────
│                                                                 │
│  输入：学者姓名 / 论文标题 / DOI / 机构                          │
│       │                                                         │
│       ▼                                                         │
│  ┌─────────────────┐                                             │
│  │   查询解析器     │  → 提取关键词、标准化查询                    │
│  └────────┬────────┘                                             │
│           │                                                       │
│           ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              多源并行采集（Source Plugins）               │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │     │
│  │  │ Google   │ │ arXiv    │ │ Semantic │ │ OpenAlex │   │     │
│  │  │ Scholar  │ │          │ │ Scholar  │ │          │   │     │
│  │  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘   │     │
│  └───────┼────────────┼────────────────────────┼─────────┘     │
│           │            │            │            │                 │
│           ▼            ▼            ▼            ▼                 │
│  ┌─────────────────────────────────────────────────────────     │
│  │              去重融合（Deduplicator）                     │     │
│  │  - 基于标题/DOI/作者+年份的相似度匹配                      │     │
│  │  - 冲突解决策略（置信度优先 / 最新优先）                  │     │
│  └────────────────────────┬──────────────────────────────────┘     │
│                           │                                       │
│                           ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              信息增强（Enricher）                       │     │
│  │  - 补充缺失字段（venue级别、引用数、PDF链接）            │     │
│  │  - 交叉验证（多源数据一致性检查）                        │     │
│  └────────────────────────┬──────────────────────────────────┘     │
│                           │                                       │
│                           ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              质量校验（Validator）                       │     │
│  │  - 必填字段完整性                                        │     │
│  │  - 数据格式合法性                                        │     │
│  │  - 可信度评分                                            │     │
│  └──────────────────────────────────────────────────────────┘     │
│                           │                                       │
│                           ▼                                       │
│  ─────────────────────────────────────────────────────────┐     │
│  │              存储（Storage）                             │     │
│  │  - SQLite（默认）                                        │     │
│  │  - JSON 文件                                             │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.4 反爬与容错设计

```python
# 核心策略
class AntiCrawlStrategy:
    """反爬策略配置"""
    
    # 1. 代理池管理
    proxy_pool: List[str] = Field(default_factory=list)
    proxy_rotation_interval: int = 10  # 每 N 次请求轮换
    
    # 2. 智能频率控制
    base_delay: float = 1.0            # 基础延迟（秒）
    adaptive_delay: bool = True        # 自适应延迟（根据响应时间调整）
    jitter: bool = True                # 随机抖动
    
    # 3. 多策略 fallback
    fallback_sources: bool = True      # 源 A 失败时自动切换到源 B
    fallback_strategies: bool = True   # 策略 A 失败时切换到策略 B
    
    # 4. 重试策略
    max_retries: int = 3
    retry_backoff: float = 2.0         # 指数退避
    retry_on_status: List[int] = [429, 503, 504]
```

---

## 4. 核心能力对比

### 4.1 能力矩阵

| 能力 | 参考系统 | 新系统 | 提升点 |
|------|---------|--------|--------|
| 数据源 | 1 个 | 6+ 个 | 多源交叉验证 |
| 反爬 | 弱 | 强 | 代理池 + 智能频率 + 多策略 |
| 增量更新 | 无 | 有 | 变更检测 + 智能调度 |
| 证据链 | 无 | 有 | 来源追溯 + 可信度评分 |
| 去重融合 | 无 | 有 | 多源数据融合 |
| 质量校验 | 无 | 有 | 完整性 + 合法性 + 可信度 |
| 可复用性 | 低 | 高 | 纯库 + CLI |
| 扩展性 | 低 | 高 | 插件化架构 |

### 4.2 性能目标

| 指标 | 目标 |
|------|------|
| 单作者论文采集 | < 30 秒（含多源） |
| 单论文元数据 | < 5 秒 |
| 采集成功率 | > 95% |
| 数据去重准确率 | > 99% |
| 增量更新检测率 | > 90% |

---

## 5. 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3.11+ | 类型提示、asyncio |
| HTTP | httpx | 异步 HTTP，支持 HTTP/2 |
| 数据验证 | Pydantic v2 | 模型定义与校验 |
| 数据库 | SQLite（默认） | 零配置，可扩展 |
| ORM | SQLAlchemy 2.0 | 异步支持 |
| CLI | Typer | 类型安全的 CLI |
| 测试 | pytest + pytest-asyncio | 异步测试 |
| 文档 | MkDocs | 文档站点 |

---

## 6. 实施计划

### Phase 1: 核心骨架（MVP）
- [ ] 项目初始化（pyproject.toml、目录结构）
- [ ] 数据模型定义（Author/Paper/Citation/Evidence）
- [ ] 抽象基类（Source/Collector/Storage）
- [ ] Google Scholar 源实现
- [ ] SQLite 存储实现
- [ ] CLI 基础命令
- [ ] 基础测试覆盖

### Phase 2: 多源扩展
- [ ] arXiv 源实现
- [ ] Semantic Scholar 源实现
- [ ] OpenAlex 源实现
- [ ] 去重融合模块
- [ ] 信息增强模块

### Phase 3: 高级特性
- [ ] 增量更新机制
- [ ] 代理池管理
- [ ] 智能频率控制
- [ ] 质量校验模块
- [ ] 批量采集优化

### Phase 4: 完善与文档
- [ ] 完整测试覆盖
- [ ] 文档站点
- [ ] 示例代码
- [ ] Skill 契约文档

---

## 7. 风险评估

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 反爬策略失效 | 高 | 多源 fallback + 代理池 + 自适应频率 |
| 数据源 API 变更 | 中 | 抽象基类隔离 + 快速适配 |
| 数据质量不稳定 | 中 | 多源交叉验证 + 可信度评分 |
| 性能瓶颈 | 低 | 异步采集 + 缓存 + 增量更新 |

---

*设计文档版本: v1.0*
*日期: 2026-07-21*
