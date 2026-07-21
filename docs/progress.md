# 项目进度

## 项目文件地图

```
├── README.md                          # 项目说明
├── SKILL.md                           # Skill 契约文档
├── pyproject.toml                     # 项目配置（依赖、脚本、工具）
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-07-21-academic-intelligence-skill-design.md  # 架构设计文档
├── academic_intelligence/             # 核心库
│   ├── __init__.py                    # 主入口（AcademicIntelligence 类）
│   ├── cli.py                         # CLI 入口
│   ├── core/                          # 核心模型与类型
│   │   ├── __init__.py
│   │   ├── models.py                  # Pydantic 数据模型（Author/Paper/Citation/Evidence）
│   │   ├── exceptions.py              # 异常层次结构（18 个异常类）
│   │   ├── constants.py               # 系统常量与配置默认值
│   │   └── types.py                   # 类型定义（SourceType, AntiCrawlStrategy）
│   ├── sources/                       # 数据源插件
│   │   ├── __init__.py
│   │   └── base.py                    # BaseSource 抽象基类
│   ├── collectors/                    # 采集器编排
│   │   ├── __init__.py
│   │   └── base.py                    # BaseCollector 抽象基类
│   ├── processors/                    # 后处理器
│   │   ├── __init__.py
│   │   ├── deduplicator.py            # 去重融合（SimilarityConfig, Deduplicator）
│   │   ├── enricher.py                # 信息增强（Enricher + 策略模式）
│   │   └── validator.py               # 数据校验（Validator + ValidationResult）
│   ├── storage/                         # 存储后端
│   │   ├── __init__.py
│   │   ├── base.py                    # BaseStorage 抽象基类
│   │   ├── sqlite_store.py            # SQLite 实现（SQLAlchemy ORM）
│   │   └── json_store.py              # JSON 文件实现
│   └── utils/                         # 工具模块
│       ├── __init__.py
│       ├── http.py                    # HTTP 客户端（含反爬）
│       ├── proxy.py                   # 代理池管理
│       ├── rate_limiter.py            # 频率控制（固定/令牌桶/自适应）
│       ├── retry.py                   # 重试策略（指数退避 + 抖动）
│       └── cache.py                   # 缓存管理（内存 + 持久化）
```

## 关键决策记录

### 2026-07-21 — 项目架构决策
- **决策**：将学术情报采集系统设计为纯 Python 库 + CLI，而非 Web 平台或 Agent 框架
- **原因**：
  - 参考系统（PaperExtraction）过度耦合 FastAPI + Vue + Agent，不可复用
  - 纯库形式可被任何项目导入使用，灵活性最高
  - CLI 提供独立使用能力，无需编写代码
- **替代方案**：
  - 方案 A：继续扩展参考系统的 Web 平台（否决：耦合度高，维护成本大）
  - 方案 B：设计为 Agent 工具（否决：用户明确要求不需要 Agent 能力）
  - 方案 C：纯库 + CLI（采纳：最灵活、最可复用）

### 2026-07-21 — 数据源策略
- **决策**：支持 6+ 数据源（Google Scholar, arXiv, Semantic Scholar, OpenAlex, PubMed, IEEE）
- **原因**：参考系统仅支持 Google Scholar，数据覆盖不足；多源可交叉验证
- **实现**：插件化架构，每个源实现 BaseSource 抽象基类

### 2026-07-21 — 存储策略
- **决策**：默认 SQLite，可选 JSON 文件
- **原因**：零配置、易部署、足够支撑中小规模数据
- **扩展**：BaseStorage 抽象基类允许未来接入 PostgreSQL/MySQL

### 2026-07-21 — 反爬策略
- **决策**：代理池 + 智能频率控制 + 多策略 fallback
- **原因**：参考系统反爬能力弱，频繁被 Google Scholar 拦截
- **实现**：HTTPClient 封装 httpx，集成 RateLimiter + ProxyPool + RetryHandler

## 当前进度

- **当前目标**：完成 Phase 1 核心骨架（MVP）
- **已完成**：
  - 参考系统深度分析（功能清单、核心问题）
  - 新系统架构设计（模块架构、数据模型、工作流）
  - 设计文档编写（`docs/superpowers/specs/2026-07-21-academic-intelligence-skill-design.md`）
  - Skill 契约文档（`SKILL.md`）
  - 项目骨架代码（24 个 Python 文件，全部通过导入验证）
  - 项目配置（`pyproject.toml`、依赖管理、代码质量工具）
- **进行中**：Phase 1 核心实现（数据模型、抽象基类、基础工具）
- **下一步**：
  - 实现核心数据模型（Pydantic 模型验证）
  - 实现 HTTPClient（httpx + 反爬）
  - 实现第一个数据源（Google Scholar）
  - 实现 SQLite 存储后端
  - 编写单元测试
- **阻塞/风险**：
  - 反爬策略需要实际测试验证（依赖代理可用性）
  - Google Scholar 无官方 API，需维护解析逻辑
- **测试结果**：
  - 所有 24 个 Python 文件导入验证通过
  - 骨架代码结构完整，可直接扩展实现
