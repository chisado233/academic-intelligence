# 升级技术实现文档（2026-08-10 Crawler Upgrade）

> 项目：Academic Intelligence（CLI 更名 `paper`）
> 文档状态：设计基线 v1.0（2026-08-10，待 kimi-k3-256k 审核）
> 前置：功能设计见 `functional-design.md`；验收见 `user-test-plan.md`
> 定位：本文件定义"怎么做"——架构、数据流、Schema、接口、错误契约、迁移、工作包

---

## 1. 架构分层（现状 + 扩展）

```
现有：
  sources/（BaseSource 抽象 + 6 适配器）
  collectors/（MultiSourceCollector 编排）
  processors/（dedup/disambiguate/scorer/incremental）
  graph/（KnowledgeGraph/traversal/cache）
  storage/（sqlite_store / json_store）
  utils/（HTTPClient: httpx + rate limiter + retry + proxy + cache）
  cli.py（typer）

升级新增：
  sources/crossref.py、unpaywall.py、europe_pmc.py、opencitations.py、core_.py
  webcrawler/（fetchers + extractors + robots 预检）
  fulltext/（locator + downloader + parser + segmenter）
  budget/（BudgetManager + per-source 配额 + fail-soft）
  identity/（resolver + disambiguator 复用 + confirm 写回）
  utils/curl_fetcher.py（可选 curl_cffi 封装，import 检测）
```

### 1.1 抓取层抽象（FetchGateway 升级）

```
FetchGateway（已有抽象，升级实现）：
├── HTTPFetcher（默认）：httpx，走 utils/HTTPClient（限速/重试/缓存）
├── CurlFetcher（可选）：curl_cffi impersonate="chrome"，import 检测（curl_cffi 未装则降级 HTTPFetcher）
└── BrowserFetcher（可选）：Scrapling DynamicFetcher/StealthyFetcher，import 检测（重型依赖，装才启用）
```

所有 fetcher 暴露同一接口：`fetch(url, *, headers, timeout) → HttpResponse`。
行为差异（超时/重试/代理语义）由 fetcher 内保证一致，测试覆盖两种传输层回归。

### 1.1.1 BaseSource 契约适配方案（C1 修订）

现状：`BaseSource` 有 5 个 `@abstractmethod`（`search_papers`/`get_paper_by_doi`/`get_author_papers`/`get_author_profile`/`get_citations`），子类必须全部实现才能实例化；Unpaywall/OpenCitations/Crossref 等新源不支持部分操作。

**决策（本基线写死）**：

1. 将作者类方法（`get_author_papers`/`get_author_profile`）与 `get_citations` 从 `@abstractmethod` **降级为默认抛 `NotSupportedError` 的具名方法**，同时 `capabilities` ClassVar 声明对应键为 `False`（对齐 arXiv/IEEE 既有 `get_citations=False` 显式声明惯例）。元数据类方法（`search_papers`/`get_paper_by_doi`）保持 abstractmethod（所有源都必须支持元数据搜索/获取）。
2. `capabilities` 增加 `fulltext` 键（True/False），`supports(operation)` 以 capabilities 为准，不再依赖 `callable(getattr(...))` 的 fallback。
3. CLI 操作 → 适配器方法映射表（`cli_source.py` 注册依据）：

| CLI 操作 | 适配器方法 | 能力键 |
|---|---|---|
| `search` | `search_papers(query, **kw)` | `search` |
| `get` | `get_paper_by_doi(doi)` / `get_paper_by_arxiv_id(id)` / 源专用 get | `get` |
| `citations` | `get_citations(paper_id)` | `citations` |
| `fulltext` | `get_fulltext(paper)`（新方法，默认 None）| `fulltext` |

CLI 只对 `capabilities[op] == True` 的源生成子命令；否则 `paper source <源> <op>` 明确报"<源> 不支持 <op>"。

### 1.2 网页爬取器（webcrawler/）

```
WebCrawler.crawl(url, schema) →
  1. robots 预检（robots.txt 拒绝 → 返回 blocked 状态，不抓）
  2. 选 fetcher（静态→CurlFetcher/HTTPFetcher；检测到 JS 依赖→BrowserFetcher 可选）
  3. 解析（Trafilatura 轻量正文 / BeautifulSoup 选择器）
  4. schema 抽取（规则模式：CSS/XPath；可选 LLM 模式：Crawl4AI import 检测）
  5. 返回 WebDocument（title/url/content/links/metadata）+ evidence

**反检测边界（I1 修订）**：Scrapling `DynamicFetcher`/`StealthyFetcher` 仅用于"目标站允许抓取、但页面需要 JS 渲染或基本反爬"的公开页。一旦目标站返回**挑战页/验证码/403 反爬拦截**（Cloudflare 盾类），一律按 `blocked` 处理并停止，**禁止升级对抗手段**（不配置自动过盾、不破解验证码）。与 functional-design §6.2 红线一致，与 user-test-plan D6 语义对齐。
```

### 1.3 全文管线（fulltext/）

```
FulltextPipeline.fetch(paper, sources=["unpaywall","core","arxiv"]) →
  0. ID 归一化：输入 <id>（内部 id/arXiv ID/DOI）→ 查库/查源得 DOI
     （arXiv ID → 查库映射；无 DOI 则仅走 arXiv 路径）
  1. Locator：按优先级找合法 OA 链接
       unpaywall.get(doi) → oa_locations[]
       core.search(doi) → fulltext 链接
       arxiv（论文有 arxiv_id → pdf_url）
  2. 第一个合法命中 → Downloader（复用 HTTPClient 限速/缓存）
  3. Parser：PyMuPDF（默认）段落级抽取；Docling 可选（import 检测）深度结构化
  4. Segmenter：段落切分（标题/正文/参考文献边界启发式）
  5. 存储 full_text 表 + 版权标记（oa_source / license）
  6. 失败路径：所有定位源无合法全文 → 明确报"无合法 OA 全文"（不绕过付费墙）
```

### 1.4 预算管理（budget/）

```
BudgetManager（每源一个 Budget 对象）：
  budget = {source, limit, period, used, unit}
  示例：openalex: 1.0 USD/day（免费 key 额度）；s2: 100 req/5min；crossref: polite 3 req/s
  分级语义（I6 修订）：
    - req/rps 类（s2/crossref/arxiv）：事前预检 check(used < limit) 再发请求
    - USD/credit 类（openalex）：事后计量 + 阈值熔断（响应错误信号/计费响应头触发熔断，
      本地估算累计 used；不依赖事前成本预检，因为单次请求事前成本不可知）
  period 滚动：UTC 日界（USD 类按日）；req/5min 类按自然窗口；滚动在第一次使用新周期时初始化
  check(source) → 是否可用；consume(source, cost) → 记一笔
  超额 → fail-soft：跳过该源，collector 转其他源，report 到 budget log + sources status
与 utils/RateLimiter 叠加：全局速率层 + 源级配额层（两层独立）。
配额语义统一抽象（credit / req / rps → 统一 cost 单位，源适配器上报；语义分级见上）。
网页源（web）不设配额，其礼貌约束（限速/robots/缓存）由 webcrawler 内部执行并计入 crawl_cache 状态。
```

### 1.5 作者身份解析（identity/）

```
Resolver.resolve(paper_id, name) →
  1. 从存储取论文 → authors 里定位 AuthorRef（name 匹配）
  2. 分支 A：AuthorRef 有 author_id（orcid/s2_id/openalex_id）→
       SourceFetcher 拉完整档案（OpenAlex/S2）：机构/h-index/主页/代表论文/研究方向
  3. 分支 B：无 author_id → AuthorDisambiguator（现有）对本地库+源搜索候选打分
       输出候选对比表（机构/方向/合著者/年份/venue + 综合分）
       ≥0.85 → 判同；0.60-0.85 → ambiguous（列候选等确认）；<0.60 → 不同人
  4. confirm(candidate_id, paper_id, name) → 写 author_identity 表（disambiguation_status=confirmed）
  5. 再次 resolve → 直接读已确认身份
```

---

## 2. 存储 Schema 扩展（SQLite）

```sql
-- 全文段落
CREATE TABLE IF NOT EXISTS full_text (
  paper_id      TEXT PRIMARY KEY,        -- 关联 papers.id
  source        TEXT,                    -- 全文来源（arxiv/pmc/unpaywall/core）
  oa_license    TEXT,                    -- 版权许可（CC-BY 等）
  file_path     TEXT,                    -- 本地 PDF 缓存路径
  paragraph_count INTEGER,
  segments      TEXT,                    -- JSON：[{heading, text, page}]
  collected_at  TEXT
);

-- 预算用量
CREATE TABLE IF NOT EXISTS budget_usage (
  source    TEXT,
  period    TEXT,                        -- 周期标识（date or datetime bucket）
  used      REAL,
  unit      TEXT,
  PRIMARY KEY (source, period)
);

-- 作者身份确认（人工/自动确认写回）
-- 全局表：同一作者跨论文复用（I8 修订）
CREATE TABLE IF NOT EXISTS author_identity_global (
  author_name   TEXT,
  author_id     TEXT,                    -- 被确认的外部 author id
  source        TEXT,                    -- openalex / s2 / orcid
  status        TEXT,                    -- auto / ambiguous / confirmed
  confidence    REAL,
  confirmed_by  TEXT,
  PRIMARY KEY (author_name, author_id, source)
);
-- 论文级证据链接：某篇论文里某作者名 → 全局身份（可追溯）
CREATE TABLE IF NOT EXISTS author_identity (
  paper_id      TEXT,
  author_name   TEXT,
  author_id     TEXT,
  source        TEXT,
  PRIMARY KEY (paper_id, author_name),
  FOREIGN KEY (author_id, author_name, source) REFERENCES author_identity_global
);

-- 网页抓取缓存（M10 划界：HTTP 缓存管传输层原始响应；本表只存抽取后的 WebDocument + 状态）
CREATE TABLE IF NOT EXISTS crawl_cache (
  url       TEXT PRIMARY KEY,
  fetched_at TEXT,
  status    TEXT,                        -- ok / blocked / failed
  etag      TEXT,
  body_hash TEXT,
  web_doc   TEXT                         -- JSON WebDocument
);

-- OpenAlex 免费快照（Phase 2，I7 修订：本期仅预留 meta 表 + 设计说明，不实现导入管线；
-- 本期 OpenAlex 降级 = 限量实时 + fail-soft）
CREATE TABLE IF NOT EXISTS openalex_snapshot_meta (
  snapshot_date TEXT PRIMARY KEY,
  rows          INTEGER
);
```

保留现有 papers/authors/evidence 表结构不动（向后兼容）。

---

## 3. 置信度基线标定（processors/scorer.py 扩展）

| 源 | 基线置信度 | 依据 |
|---|---|---|
| arXiv | 0.95 | 已有 |
| PubMed | 0.92 | 已有 |
| OpenAlex | 0.90 | 已有 |
| Crossref | 0.90 | 新增：DOI 注册权威 |
| Semantic Scholar | 0.88 | 已有 |
| Europe PMC | 0.90 | 新增：官方 OA 库 |
| IEEE | 0.85 | 已有 |
| CORE | 0.85 | 新增：聚合源 |
| Unpaywall | 0.85 | 新增：仅定位链接 |
| OpenCitations | 0.85 | 新增：纯引用图 |
| Google Scholar | 0.75 | 停用适配器，保留数值 |

新源接入去重融合管道零代码改动；仅注册基线 + 测试验证融合正确。

### 3.1 出版社映射（I5 修订，归属 WP2a）

Q1"论文 → 出版社"实现路径（两级）：
1. **首选**：Crossref 元数据自带 `publisher` 字段（如 `10.1038/...` → "Springer Nature"），`paper source crossref get` 直接取，零维护
2. **兜底**：静态 DOI 前缀 → 出版社映射表（`academic_intelligence/utils/publisher_map.py`，约 30 个常见前缀：10.1038→Springer Nature、10.1109→IEEE、10.1145→ACM、10.1016→Elsevier 等），仅当源无 publisher 字段时启用
实现挂 WP2a（Crossref 适配器一并交付）。

---

## 4. CLI 重构（cli.py / 命令树）

```
项目结构：academic_intelligence/
  cli.py                 # paper 入口（typer），原 ai → paper
  cli_source.py          # paper source <源> <操作>（按源注册）
  cli_author.py          # paper author resolve/profile/search/confirm
  cli_web.py             # paper web crawl
  cli_budget.py          # paper budget / sources status
```

命令注册模式：源适配器通过 `register_source(app, source)` 挂载到 `source` 子命令树；
每个源声明支持的操作（search/get/citations/fulltext），CLI 按声明生成子命令。
未声明操作 → 明确报"<源> 不支持 <操作>"（对齐 arXiv citations 的既有行为）。

`pyproject.toml`：`[project.scripts] ai = "academic_intelligence.cli:main"` → `paper = ...`

---

## 5. 错误契约

- 单源失败：软失败（CollectionResult.errors 记录），不阻塞整体（已有）
- 全源失败：`AllSourcesFailedError` 附每源原因（已有）
- 配额耗尽：fail-soft（跳过该源 + 转其他源 + budget log 上报），不抛致命错误
- 网页 blocked（robots 拒绝/403）：返回 blocked 状态 + 诊断信息（建议 L1/L2），不假成功
- 无合法全文：明确报"无合法 OA 全文" + Unpaywall 提示，不绕过
- 错误输入（不存在的 DOI/ID）：清晰报"未找到"，退出码规范（0=成功含 partial、2=全失败，对齐现状）
- 幂等：collect/fulltext 可重复执行（upsert）；增量 update 保持 stale-gate + 字段级 diff

---

## 6. 并发与并行

- 多源采集并行：`collect --sources all` 各源异步并发（已有 max_concurrent_requests 控制）
- **实现阶段并行**（3B 工作包）：源适配器之间写入范围互斥（每源一个文件组 + 各自测试），可并行派单
- 测试资源：pytest 全离线（VCR cassette 回放）；新源测试采用同模式（cassette 录制 → 回放）

---

## 7. 可观察性

- `paper sources status`：每源健康（探针/最近结果）、额度余量、限流状态
- `paper budget`：全部源配额总览
- 采集日志：source/operation/status/duration/evidence（结构化为 events.jsonl 风格）
- 网页抓取：crawl_cache 表可查（status: ok/blocked/failed）

---

## 8. 迁移与兼容

- CLI 改名：**保留 `ai` shim 入口一个版本周期**（I3 修订）：`ai` 仍然安装（`[project.scripts] ai = "academic_intelligence.cli:ai_legacy_shim"`），任何 `ai` 调用打印 "Command 'ai' was renamed to 'paper'" 并 `exit 2`；正式命令为 `paper`。T1 判定单一化：`paper` 可用 + `ai` 打印改名提示且 exit 2。
- 数据库：现有 academic_intelligence.db 表结构不动，新表增量创建（轻量迁移脚本 + 迁移测试用例，见 user-test-plan §3 T10-migration）
- 配置：Config 新增 budget 段、crawler 段（fetch_mode/ua/robots 开关）、源配置项（M9）：`crossref_mailto` / `unpaywall_email` / `core_api_key`（及对应 env `CROSSREF_MAILTO` / `UNPAYWALL_EMAIL` / `CORE_API_KEY`）；旧配置兼容（缺省用默认值）
- Google Scholar（M14）：`gs` 适配器**保留代码但默认不注册**（延续 `enable_google_scholar=False` 语义），历史 gs 证据数据不动；README 移除 `gs` 为默认源说明
- 旧 `ai` 测试/文档同步更新；changelog 记录 Breaking Change
- 回滚：新表可独立删除；CLI 改名可回退（shim 即回退通道）

---

## 9. 工作包划分（3B 并行派单依据）

| 工作包 | 内容 | 写入范围 | 依赖 |
|---|---|---|---|
| WP1 | CLI 骨架重构（ai→paper + source 子命令树 + 源注册机制） | cli.py/cli_*.py/pyproject.toml | 无 |
| WP2a | Crossref 适配器 + 出版社映射表（§3.1）+ 测试 | sources/crossref.py + utils/publisher_map.py + tests/ | WP1 |
| WP2b | Unpaywall 适配器 + 测试 | sources/unpaywall.py + tests/ | WP1 |
| WP2c | Europe PMC 适配器 + 测试 | sources/europe_pmc.py + tests/ | WP1 |
| WP2d | OpenCitations 适配器 + 测试 | sources/opencitations.py + tests/ | WP1 |
| WP2e | CORE 适配器 + 测试 | sources/core_.py + tests/ | WP1 |
| WP3 | 网页爬虫层（webcrawler/ + curl_cffi 可选 + web crawl CLI） | webcrawler/ + utils/curl_fetcher.py + cli_web.py | WP1 |
| WP4 | 全文管线（fulltext/ + storage full_text 表 + pdf parse CLI） | fulltext/ + storage 迁移 + cli | WP1 |
| WP5 | 预算管理（budget/ + budget_usage 表 + sources status） | budget/ + storage 迁移 | WP1 |
| WP6 | 作者身份解析（identity/ + author_identity 表 + paper author CLI） | identity/ + storage 迁移 + cli_author.py | WP1 + WP2 全通过 |
| WP7 | 测试套件（VCR cassette 录制/回放 + 集成 + 回归 + 迁移用例） | tests/ | WP2-6 |
| WP8 | 文档对齐（README/SKILL/changelog/迁移说明 + user-guide） | 文档 | 全部 |

**并行策略**：WP2a-e 相互独立可并行；WP3/WP4/WP5 与 WP2 并行（写入范围互斥）；
WP6 依赖全部源通过后启动；WP7/WP8 收尾。

---

## 10. 派单执行约束（Worker 硬性禁止项）

每个实现/测试派单的 `prompt.md` **必须**包含以下禁止项（转述原文）：

1. **禁止调用 mycli 的任何工具**（含 `mycli agent-cli` / `project-manager` / `skill-library` / `dwm` 等全部子命令）
2. **禁止加载/调用任何 skill**（本单指定的除外；默认不加载任何 skill）
3. 禁止任何 git 操作（clone/commit/push/checkout 等）
4. 禁止创建/派发下级 Agent
5. 禁止修改共享文档（progress.md/decisions.md/architecture.md 等）与 Goal
6. 禁止扩大读写授权范围（只写本单指定文件）
7. 禁止执行未经授权的外部/不可逆动作

**爬虫框架强制选型**（必须使用调研选定框架，不得另选替代实现）：

| 能力 | 强制框架 | 引入方式 |
|---|---|---|
| TLS 指纹抓取 | **curl_cffi** | 可选依赖 + import 检测（默认 httpx 兜底）|
| 浏览器渲染/反检测 | **Scrapling**（DynamicFetcher/StealthyFetcher）| 可选重型依赖 + import 检测 |
| 轻量正文抽取 | **Trafilatura** | 依赖 |
| LLM 结构化抽取（可选） | **Crawl4AI** | 可选 + import 检测 |
| PDF 段落解析 | **pdfplumber（MIT）为默认**（I2 决策：项目 MIT 许可，避免 AGPL 传染；PyMuPDF 降为可选 extra，仅个人本地使用且接受 AGPL 时启用）| 依赖（pdfplumber）|
| PDF 深度结构化（可选）| **Docling** | 可选 + import 检测（GROBID 本期不引入，见 M7）|
| 限流/配额 | **复用现有 utils/RateLimiter**（M16：不新增核心依赖；确需 limits/aiolimiter 时降为可选并说明）| 依赖（现有）|

约束：新依赖一律走"可选 + import 检测 + 特性开关"（延续 Scrapling 历史决策 D-2026-08-07-1），不污染核心安装。

---

## 11. 风险与回退

| 风险 | 缓解 |
|---|---|
| 新源 API 字段质量差异 | 置信度基线 + 融合测试（T6）|
| curl_cffi/Scrapling 行为差异 | 可选 import + 双传输层回归测试 |
| OpenAlex 额度耗尽 | 快照 + 限量 + fail-soft（T7）|
| 网页改版导致选择器失效 | 规则 + LLM 抽取双保险 + blocked 诊断 |
| 全文版权误采 | 仅 OA 源（Unpaywall/CORE/arXiv/PMC），存 license 标记（T9）|
| CLI 改名破坏旧用法 | 迁移提示 + changelog + 文档更新 |

回退规则（agent-dispatch）：普通缺陷→执行；设计/接口错误→本文档修订（3A）；工作包边界错误→3B 调整。
