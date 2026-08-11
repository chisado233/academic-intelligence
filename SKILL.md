---
name: academic-intelligence
description: Use when collecting, querying, or analyzing academic information such as papers, authors, citations, venues, publishers, or research trends. Use when integrating scholarly data from free sources (arXiv, Crossref, OpenAlex, Semantic Scholar, PubMed, Europe PMC, Unpaywall, OpenCitations, CORE, IEEE) or building academic data pipelines. Use when needing evidence-tracked, confidence-scored academic data with deduplication, author disambiguation, legal open-access full text, web crawling, and incremental updates.
---

# Academic Intelligence（论文爬虫）

A modular Python library + CLI (`paper`) for academic data collection, fusion, full-text acquisition, and knowledge-graph browsing.
**CLI 命令是 `paper`**（旧名 `ai` 保留为 shim，调用会打印 `Command 'ai' was renamed to 'paper'` 并 exit 2）。

本文件是技能的**详细使用方法**契约：所有命令、能力、示例均与当前实现一致。

---

## 1. 总览

```
元数据采集（11 源）→ 去重融合（evidence_list + 置信度）→ 存储(SQLite) → 查询/图谱/导出
        ↓
全文管线（合法 OA 定位 → 下载 → 解析 → 段落入库）
网页爬取（无 API 的公开页面）
作者身份解析（论文内作者 → 身份档案/消歧/确认）
预算管理（每源配额 + fail-soft）
```

- **免费优先**：Crossref/arXiv/Europe PMC/OpenCitations/Unpaywall/CORE/S2 免费；OpenAlex 限量（免费 key $1/天，超出 fail-soft）；Google Scholar 适配器**保留但默认不注册**（ToS 红线）
- **合规红线**：不碰付费墙正文、不集成 Sci-Hub/libgen、不自动化爬 Google Scholar、不破解验证码/过盾、尊重 robots.txt

---

## 2. 安装

```bash
cd projects/paper-research-crawler
pip install -e ".[dev]"        # 开发安装
paper --version                # 验证
# 可选依赖（增强能力，装齐后对应命令自动启用）：
pip install curl-cffi          # TLS 指纹抓取（网页爬取增强）
pip install scrapling          # 浏览器渲染/反检测（L1/L2 网页）
pip install crawl4ai           # LLM 结构化抽取（可选）
pip install docling            # PDF 深度结构化（可选）
```

---

## 3. 详细 CLI 使用方法

### 3.1 命令总览

```
paper [OPTIONS] COMMAND
├── source <源> <操作> <值>     # 单源直接操作（核心新界面）
├── sources / sources status   # 源注册表（能力矩阵）/ 健康额度
├── budget                     # 每源预算配额总览
├── collect paper|author|citations   # 多源聚合采集（去重融合入口）
├── paper|author|author-papers # 旧别名（collect 的快捷方式）
├── update --author <名>       # 增量刷新（stale-gate + 字段级 diff）
├── fulltext <id>              # 合法 OA 全文管线
├── web crawl <url>            # 网页爬取 + schema 抽取
├── pdf parse <file>           # 本地 PDF 解析为段落
├── author resolve|profile|search|confirm   # 作者身份解析
├── query papers               # 查询已存数据（仅 papers 实体）
├── stats                      # 存储统计
├── expand / export            # 知识图谱展开 / 子图导出
├── export-papers              # 流式导出 CSV/JSONL/Parquet
└── --version / --help
```

### 3.2 通用选项

| 选项 | 说明 |
|---|---|
| `--persist` | **必须显式**才写库。不加只打印/写 `--output` 文件 |
| `--sources/-s` | 源选择（多源聚合时）：`arxiv,openalex,crossref,...` 或 `all` |
| `--output/-o` | JSON 输出路径 |
| `--limit/-n` | 搜索结果条数 |
| `--storage-path` | 数据库路径（默认 `./academic_intelligence.db`）|
| `--fulltext` | `source get` 时同时走全文管线 |
| `--key` | 源专用 key（如 IEEE）|

### 3.3 单源操作（`paper source <源> <操作>`）

**11 个源的能力矩阵**（`paper sources` 可实时查看）：

| 源 | 别名 | search | get | citations | fulltext |
|---|---|---|---|---|---|
| arxiv | — | ✓ | ✓ | – | –* |
| openalex | oa | ✓ | ✓ | ✓ | – |
| semantic_scholar | ss, s2 | ✓ | ✓ | ✓ | – |
| pubmed | — | ✓ | ✓ | ✓ | – |
| ieee | — | ✓ | ✓ | – | – |
| crossref | — | ✓ | ✓ | – | – |
| unpaywall | — | – | ✓ | – | ✓ |
| europe_pmc | europe-pmc, epmc | ✓ | ✓ | – | ✓ |
| opencitations | coci | – | – | ✓ | – |
| core | — | ✓ | ✓ | – | ✓ |
| google_scholar | gs | （默认不注册）| | | |

\* arXiv 全文经 `paper fulltext` 定位器覆盖（源适配器层不声明 fulltext）。

**示例**：

```bash
# 元数据获取
paper source arxiv get 2501.12948 --persist            # 按 arXiv ID
paper source arxiv search "deepseek" --limit 20 --persist
paper source crossref get 10.1038/s41586-025-09422-z --persist   # DOI 权威 + publisher
paper source crossref search "deep learning" --limit 5
paper source openalex get 10.1038/nature14539          # OpenAlex（限量）
paper source ss search "graph neural network" --limit 5
paper source pubmed search "deepseek" --limit 5
paper source europe_pmc search "cancer" --limit 5      # 生物医学 + OA 全文
paper source core search "transformer" --limit 5       # 无 key 走免费 tier（限流频繁）

# 全文定位（Unpaywall 需要邮箱）
paper source unpaywall get 10.1038/s41586-025-09422-z --fulltext

# 引用关系（OpenCitations 以 DOI 为键！不接受 W-id）
paper source opencitations citations 10.1038/s41586-025-09422-z
paper citations 10.1038/s41586-025-09422-z --sources openalex,opencitations

# 不支持的操作 → 明确报错（exit 2，fail-closed）
paper source arxiv citations 2501.12948    # Error: arxiv 不支持 citations
```

### 3.4 全文管线（合法 OA 全文）

```bash
# 定位顺序：Unpaywall → CORE → arXiv → Europe PMC；仅合法 OA
paper fulltext 2501.12948 --sources arxiv --persist
paper fulltext 10.1038/s41586-025-09422-z --persist

# 无合法 OA → 明确拒绝（不绕过付费墙）：
#   "无合法 OA 全文" + 提示；exit 非 0
```

- ID 归一化：内部 id / arXiv ID / DOI 均可（查库/查源得 DOI）
- 解析默认 **pdfplumber**（MIT）；可选 `pymupdf`/`docling`
- 结果入 `full_text` 表（segments JSON：heading/text/page + license 标记）

### 3.5 网页爬取 / PDF 解析

```bash
# 网页爬取（公开页：会议主页/作者主页/机构仓库；尊重 robots）
paper web crawl "https://arxiv.org/abs/2501.12948" --output page.json
paper web crawl "<url>" --extract schema.json --persist   # 规则抽取（CSS/XPath）

# 本地 PDF 解析为段落
paper pdf parse ./paper.pdf --output segments.jsonl
```

- robots 拒绝 / 403 / 挑战页 → `blocked`（exit 2，**不升级对抗手段**）
- 默认礼貌模式：限速 1 req/s、UA `paper-research-crawler/0.2 (learning use)`、低并发
- 可选增强：`curl_cffi`（TLS 指纹）、`scrapling`（JS 渲染）、`crawl4ai`（LLM 抽取）——未装自动降级

### 3.6 作者身份解析（`paper author`）

```bash
# 论文内作者身份解析（Q2 场景）
paper author resolve 2403.05525 "Haoyu Lu"     # <paper.id> "<作者名>"
#   → 输出：机构 / h-index / 引用 / 论文数 / 主页 / 代表论文（按引用排序）+ 证据链
#   → 已确认身份跨论文复用（author_identity_global 表）

# 按 ID 拉完整档案（Q3 场景）
paper author profile A5110986785

# 名字搜候选 + 消歧排序（Q7 场景）
paper author search "Haoyu Lu" --disambiguate
#   → 候选对比表：机构/h-index/引用/合著者/综合分；≥0.85 auto、0.60-0.85 ambiguous

# 确认身份写回（跨论文生效）
paper author confirm <candidate-id> --for <paper-id> --name "<作者名>"
```

### 3.7 源注册表 / 预算

```bash
paper sources          # 11 源 × 4 操作能力矩阵
paper sources status   # 能力矩阵 + 预算配额（Semantics/Period/Used/Limit/Remaining）
paper budget           # 预算总览（precheck=req 类事前预检；metered=USD 类事后熔断）
```

- 超额 → **fail-soft**：该源跳过、其他源继续、状态上报；OpenAlex 额度耗尽自动降级
- 环境变量：`CROSSREF_MAILTO`（polite pool）、`UNPAYWALL_EMAIL`（必需）、`CORE_API_KEY`（可选提升）、`OPENALEX_EMAIL`、`IEEE_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY`

### 3.8 多源聚合 / 查询 / 图谱 / 导出 / 增量

```bash
# 聚合采集（去重融合入口：同一论文多源 → 1 条 + evidence_list 多源）
paper collect paper "10.1038/nature14539" --sources all --persist
paper collect author "Geoffrey Hinton" --sources openalex,ss --persist

# 查询（仅 papers 实体；keyword 走 FTS5）
paper query papers --author "Hinton" --year 2015-2024 --limit 10
paper query papers --keyword "deep learning" -o out.json

# 图谱
paper query papers --keyword "DeepSeek-R1" -o id.json     # 先拿内部 id
paper expand "<paper.id>" -r references,citations,authors -d 2 -o graph.json
paper export --snapshot graph.json -c "<paper.id>" -r 2 -o subgraph.json

# 导出
paper export-papers --format csv --output papers.csv --excel-safe
paper export-papers --format jsonl --output papers.jsonl

# 增量刷新（7 天 stale-gate；只写变化的字段）
paper update --author "Geoffrey Hinton" --sources openalex

# 统计
paper stats
```

---

## 4. Python API 快速参考

```python
import asyncio
from academic_intelligence import AcademicIntelligence, Config

async def main():
    config = Config(
        sources=["openalex", "crossref", "semantic_scholar"],
        storage_type="sqlite",
        storage_path="./academic_intelligence.db",
        unpaywall_email=None,          # 或 env UNPAYWALL_EMAIL
    )
    async with AcademicIntelligence(config) as ai:
        result = await ai.collect_paper("10.1038/s41586-025-09422-z", persist=True)
        paper = result.papers[0]
        print(paper.title, paper.year, paper.venue)
        print([a.name for a in paper.authors])
        for ev in paper.evidence_list:
            print(f"  {ev.source.value}: conf={ev.confidence:.2f}")

asyncio.run(main())
```

模块：`sources/`（11 适配器）、`collectors/`、`processors/`（去重/消歧/评分/增量）、
`fulltext/`（管线）、`webcrawler/`、`budget/`、`identity/`（作者解析）、`graph/`、`storage/`。

### 4.1 高级公共 API 与错误语义（契约）

- `ArxivSource.get_paper_by_arxiv_id(arxiv_id)`（async）→ `Paper | None`
- `AuthorDisambiguator.score_pair(a, b)` / `AuthorDisambiguator.cluster(authors)` / `AuthorDisambiguator.disambiguate(authors)`（特征阈值：≥0.85 auto / 0.60-0.85 ambiguous / <0.60 不同人）
- `KnowledgeGraph.add_node(...)` / `KnowledgeGraph.add_edge(...)` / `KnowledgeGraph.save_snapshot(path)` / `KnowledgeGraph.load_snapshot(path, *, cache_size=None)`；版本 1 快照含 `"node_count"`、`"edge_count"`、`version`/`directed`/`nodes`/`edges` 校验字段
- 错误语义：单源失败软性记录于 `CollectionResult.errors`；全部源失败抛 `AllSourcesFailedError`；自动化**不得创建无界**外层重试（默认无外层重试，或至多一次显式重试并尊重 `retry_after`）；报告约定：部分成功 `PARTIAL`、全部不可用/凭证缺失 `BLOCKED` 并停止

---

## 5. 配置与凭证

```python
config = Config(
    sources=["openalex", "semantic_scholar"],
    storage_type="sqlite",
    crossref_mailto=None,    # Crossref polite pool（env CROSSREF_MAILTO）
    unpaywall_email=None,    # Unpaywall（env UNPAYWALL_EMAIL，必需才能用）
    core_api_key=None,       # CORE v3（env CORE_API_KEY，可选）
    openalex_email=None,     # OpenAlex 礼貌池（env OPENALEX_EMAIL）
    ieee_api_key=None,       # IEEE（env IEEE_API_KEY）
)
```

预算：`budget` 段（每源配额，缺省内置：openalex 1.0 USD/day、s2 100 req/5min、crossref 3 req/s）。
爬虫：`crawler` 段（fetch_mode/ua/enable_browser/enable_robots）。

---

## 6. 数据模型（要点）

- **Paper**：`id/title/authors(List[AuthorRef])/year/venue/abstract/doi/arxiv_id/pmid/url/pdf_url/citations/references/fields_of_study/evidence_list`
- **AuthorRef**：`author_id/name/position/is_corresponding/affiliation`
- **Evidence**：`source/source_id/source_url/collected_at/confidence/raw_data`
- **FullText**：`paper_id/source/oa_license/segments(JSON)/paragraph_count`
- 去重：DOI/arXiv/PMID 精确 → arXiv↔DOI 交叉 → 标题 ≥0.92 → 加权模糊；union-find 闭包
- 置信度基线：arXiv 0.95 / PubMed 0.92 / Crossref 0.90 / OpenAlex 0.90 / Europe PMC 0.90 / S2 0.88 / CORE 0.85 / Unpaywall 0.85 / OpenCitations 0.85 / IEEE 0.85 / GS 0.75；多源 +0.05/源、DOI +0.05、封顶 1.0

---

## 7. 合规红线（不可逾越）

| 能做 | 不能做 |
|---|---|
| 免费 API 全用（限速礼貌）| 绕过付费墙拿正文 |
| 下载合法 OA 全文（arXiv/PMC/Unpaywall/CORE 定位）| Sci-Hub / libgen 类集成 |
| 下载作者主页/机构仓库自存档 | 自动化爬 Google Scholar |
| 爬公开网页（尊重 robots + 限速）| 破解验证码 / Cloudflare 过盾 |
| OpenAlex 免费快照（Phase 2）| 大规模并行打爆源站 |

---

## 8. 排错与已知限制

| 现象 | 原因 / 处理 |
|---|---|
| `Error: Unpaywall requires an email address` | 配置 `UNPAYWALL_EMAIL`（本人邮箱，服务端强制）|
| CORE 频繁 429 | 免费 tier 限流；注册免费 `CORE_API_KEY` 提升 |
| OpenAlex 报额度/计费错误 | 免费额度耗尽；降级到其他源（fail-soft 自动）|
| `arxiv 不支持 citations` | 该源无此能力（fail-closed 正确行为）|
| `opencitations ... W...` 报 invalid DOI | COCI 只认 DOI，不接受 OpenAlex W-id |
| `ai` 提示 renamed | 旧命令 shim，正式命令是 `paper` |
| `query` 只支持 papers 实体 | 当前限制（作者查询走 `paper author`）|
| OpenAlex 作者档案混入同名论文 | 外部源聚合噪声；身份判定以著作列表含目标论文为准 |
| 作者拼写变体（缩写）不自动合并 | 需各自 `paper author confirm` |

---

## 9. 使用场景速查

| 想做什么 | 命令 |
|---|---|
| 找一篇论文 | `paper source arxiv search "<关键词>" --persist` |
| 查出版社 | `paper source crossref get <DOI>`（publisher 字段）|
| 查作者是谁 | `paper author resolve <paper-id> "<名字>"` |
| 查作者代表作 | `paper author profile <author-id>` |
| 区分同名作者 | `paper author search "<名字>" --disambiguate` |
| 下载合法全文 | `paper fulltext <id> --persist` |
| 爬会议主页 | `paper web crawl "<url>" --extract schema.json` |
| 引用关系 | `paper source opencitations citations <DOI>` |
| 批量统计 | `paper source arxiv search "<关键词>" --limit 20 --persist` + `paper stats` |
| 更新作者论文 | `paper update --author "<名字>"` |

---

## 10. 学术头衔查询指引（杰青/院士/长江等）

**学术头衔（杰青、优青、长江学者、两院院士、万人计划等）不在本项目 11 个学术数据源内**——这些是中文人才头衔，国际学术库（OpenAlex/Crossref/S2）不标注。查询方向与交叉验证方法见：

**`docs/titles-source-map.md`**（学术头衔查询源地图）

使用要点：
1. **先锁人**：`paper collect author "<姓名>" --persist` + `paper author profile <author-id>` 确认机构/方向，避免同名误配
2. **再验头衔**：按地图到官方源（中科院院士馆/工程院院士馆/教育部/基金委系统等）按"姓名+单位"核验
3. **交叉验证**：至少两个独立来源一致（官方库 ↔ 高校官网/喜报 ↔ 论文致谢 NSFC 资助号），结论标注 确认/疑似/存疑/无法验证
4. **时效**：名单逐年动态；2024 年起杰青不再公布完整名单；以官方为准

---

## 11. 信息反向挖掘（引用者画像）

> 场景：拿到一篇**种子论文**，反查"谁引用了它"，再为这批引用者建立完整画像（作者/机构/领域/代表作/venue/时间线/头衔）。
> 定位：**CLI 给结构化原料（确定性原语），判断与融合由 agent 方法论完成**——CLI 不做自动消歧、不自动核验头衔、不落主库（分析产物即 CSV/JSON 文件）。

### 11.1 工作流（6 步）

```
① 定位种子论文   paper source arxiv|openalex get <id> --persist
② 反向引用       paper trace-citing <id> --sources openalex,opencitations --output citing.csv
③ 展平作者       paper trace-authors citing.csv --output authors.csv
④ 画像特征       paper trace-profiles authors.csv --output profiles.csv
⑤ 消歧           agent 方法论判断（见 11.2，不自动合并）
⑥ 头衔核验       agent web fetch 官方源（衔接 §10 / docs/titles-source-map.md）
→ 输出：最终画像 CSV（作者/机构/领域/代表作/venue/时间/头衔/来源链）
```

各原语输出列：

| 原语 | 输出文件 | 列 |
|---|---|---|
| `trace-citing` | citing.csv | citing_paper_id / doi / title / year / venue / authors_raw / authors_detail（compact JSON，含 author.id / display_name / institutions，链式保留 detail） |
| `trace-authors` | authors.csv | author_name / appears_in / affiliation / author_id |
| `trace-profiles` | profiles.csv | author_name / author_id / institution / h_index / fields / works_count / top_works |

说明：
- `trace-citing` 双源交叉（OpenAlex `filter=cites:` + OpenCitations `citations/{doi}`，后者以 DOI 为键；`references/{doi}` 返回的是论文自身引用了什么，方向相反，勿用），内置 429 退避与断点续传（`--resume-from`）；`--limit` 作用于合并后总结果，双源不会被静默跳过
- `trace-authors` **只展平不合并**——同名作者各占一行，保留原始署名机构；`--affiliation-filter <关键词>` 是机械子串过滤，非消歧判断
- `trace-profiles` 按 author_id（如有）拉 OpenAlex 档案与代表作（按引用排序）；无 ID 的行返回占位（author_id=None），**不自动搜索匹配**——留给 agent 方法论

### 11.2 消歧方法论（agent 判断，不自动合并）

- **ID 直连优先**：author_id（OpenAlex / ORCID / S2）相同 → 同一人（强证据）
- 无 ID：**机构 + 领域 + 代表作品 + 合著者**特征对比
- 多候选并列展示，标注置信度：**确认 / 疑似 / 存疑**
- 中文名：拼音 ↔ 中文名对照需联网搜索确认（API 只有拼音，如 "Lu Feng" ↔ "陆峰"）
- **不硬合并**：证据不足一律标"存疑"，待更多搜索或人工核实

### 11.3 信息获取边界表

| 信息 | 提供方 | 说明 |
|---|---|---|
| 论文 / 引用 / 机构 / 作品 / venue / 年份 / h-index / 领域 / 作者 ID | **CLI（API）** | 结构化、可批量 |
| 中文姓名对照（拼音 ↔ 中文） | **agent 联网搜索** | API 只有拼音 |
| 个人主页 / 最新动向 / 单位介绍 | **agent 联网搜索** | 非结构化信息 |
| 头衔（杰青 / 院士 / 长江等） | **agent 联网搜索**（官方源） | 见 §10 / `docs/titles-source-map.md` |
| 画像融合（CLI 数据 + 搜索数据合成） | **agent** | 最终画像 CSV |

**原则：API 没有的信息一律 agent 联网确认，CLI 不假装提供。**
