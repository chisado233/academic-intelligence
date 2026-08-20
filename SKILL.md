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
任务收尾（EVIDENCE-CHAIN 证据链报告 → HTML 浏览器可开，§12 强制）
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

> **别名分两层**：`--sources` 选择器（如 `collect paper --sources ss,oa`）接受 `ss`/`s2`/`oa`/`gs`/`epmc`/`coci` 等短名；`paper source <源>` 子命令只额外挂载 `europe-pmc`/`epmc`/`coci` 三个别名，其余源必须用完整名（`semantic_scholar`、`openalex`、`arxiv`……），用 `ss` 会报 `No such command`。

**示例**：

```bash
# 元数据获取
paper source arxiv get 2501.12948 --persist            # 按 arXiv ID
paper source arxiv search "deepseek" --limit 20 --persist
paper source crossref get 10.1038/s41586-025-09422-z --persist   # DOI 权威 + publisher
paper source crossref search "deep learning" --limit 5
paper source openalex get 10.1038/nature14539          # OpenAlex（限量）
paper source semantic_scholar search "graph neural network" --limit 5
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
- `AuthorDisambiguator.score_pair(a, b)` / `AuthorDisambiguator.cluster(authors)` / `AuthorDisambiguator.disambiguate(authors)`（特征阈值：≥0.85 auto / 0.60-0.85 ambiguous / <0.60 不同人；**仅作候选评分展示，采集管道不自动调用**——2026-08-12 决策：作者消歧不做进任何采集路径，判断交 agent/人工）
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
| **OpenAlex 作者实体归属错误/拆分** | 同名实体可能挂载他人作品，或本人作品被挂到同名他人实体（如署名机构与实体主机构冲突）。**锁人后必须检查 `paper author profile` 输出的"疑似归属错误/漏检作品"区块**；有多个 ORCID/机构时以论文署名机构为准，勿仅凭单一实体下结论 |
| **疑似漏检/归属冲突作品凭记忆排除（禁止）** | entity_flags 命中的**每一篇**疑似归属/漏检作品，必须先用**官方 CV / 出版商页 / 论文原文**核实作者名单，才允许纳入或排除——"我记得这篇不是他的"不构成证据。2026-08-17 实例：MBLLEN（BMVC 2018，无 DOI，挂到浙师大同名实体）被凭记忆误判为他人作品，导致"陆峰被引最多论文"结论错误（正确答案 MBLLEN 本身：GS 1121 / S2 804，本人通讯作者）；正确裁决方式是[本人官方 CV](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf) 逐条核对 |
| **未核验头衔写成具体断言（禁止）** | 官方源不可达时，待核验条目只能列**候选名单**（姓名 + 单位 + 建议核验入口），**禁止写出具体头衔/年份**（如"某某=中科院院士(2023)"）——错误的具体断言比诚实的空白危害大，读者记住的是表格不是脚注。2026-08-17 实例：网络故障日 run4 先验表 7 条头衔全错（黄庆明/马华东非院士，刘家瑛/马佳义/周文罡/沈建冰/左旺孟非国家杰青），虽标 likely 仍构成误导 |
| **Google Scholar 引用数不可自动获取** | CLI 不自动爬 GS（ToS 红线），也无官方 API。需要 GS 口径引用数时：① 用 OpenAlex `cited_by_count` + S2 `citationCount`（**S2 引用图为自建口径，非 GS 数据**）双源交叉，作"GS 的独立近似源"；② 确需 GS 精确数字时，用 WebSearch 检索 GS 结果页快照（带查询日期）或人工浏览器单次查询并注明来源 |
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
| 任务收尾报告 | `python scripts/render_report.py analysis/<task>/EVIDENCE-CHAIN.md --open`（§12 强制）|
| 任务方法论总索引 | 接到任务先查 §13（任务类型 → 方法链 → 特有坑 + 横切原则）|

---

## 10. 学术资历查询指引（头衔 / 职称 / Fellow / 编委 / 会议主席 / 海外经历等）

**学术资历（杰青、院士、长江、IEEE Fellow、期刊编委、会议主席、海外经历等）不在本项目 11 个学术数据源内**——这些是学者属性，国际学术库（OpenAlex/Crossref/S2）只标注论文/引用/机构，不标注资历。查询方向与交叉验证方法见：

**`docs/titles-source-map.md`**（学术资历查询源地图，§0.5 通用方法论 + §1/§3 实例表）

使用要点：
1. **先锁人**：`paper collect author "<姓名>" --persist` + `paper author profile <author-id>` 确认机构/方向，避免同名误配
2. **判断声明类型 → 找责任发布主体**（titles-source-map §0.5.3）：谁对该事实负责谁就是主来源——院士/Fellow→授予组织名录；职称→雇佣机构；编委→期刊/出版方；会议主席→当年会议官网；海外经历→授予机构/CV。**源不确定是常态，按声明换源、不写死名单**
3. **构造 query**（§0.5.6）：身份变体 × 机构变体 × 角色词 × 年份 × 站点限定（站点限定用域名清单 `site:edu.cn OR site:cas.cn OR site:ac.cn`，勿裸用 edu.cn）；每个角色词单独查询；**设终止条件**（≥3 变体 × ≥2 后端 或 ≥8 次搜索仍无权威源 → 判 not_found 并记录已试 query）
4. **证据与置信度**（§0.5.5）：区分独立源与转载源（CV 与 LinkedIn 同源自述，不构成交叉）；自述与权威源矛盾时回权威源裁决；结论用四值 **confirmed / likely / not_found_after_search / indeterminate**，"未获取"≠"不存在"
5. **时效**：每条结论带时间边界（现任/曾任/当届 + 查询日期）；名单逐年动态；2024 年起杰青不再公布完整名单；历史态用 Wayback Machine
6. **降级协议（权威源大面积不可达时）**：当 ≥2 类责任主体源（两院官网 / 单位官网）当日不可达且无法交叉时，把该核验任务标 **BLOCKED 择期重跑**，输出"待核验候选名单（无头衔断言，见 §8 禁令）"。降级缩减的是**产出规模**，不是对先验的警惕——绝不以模型记忆填充头衔。2026-08-17 实例：run4 当日 CAE/CAS/北邮/USTC 全挂，产出 2 人名单 + 7 条错误先验，正确动作应是 BLOCKED
7. **勘误必读（历史归档是资产，不是污染）**：任务开始时先 grep `analysis/*/EVIDENCE-CHAIN.md` 顶部的"⚠️ 勘误"块，既有勘误作为待复核约束进入本次任务。**独立性 = 重新采集与核验，不是无视勘误**——同日 run4 因声明"不读任何历史记录"，重复了 run3 已书面纠正过的错误（马华东非院士）
8. **头衔断言必须内嵌原文（2026-08-17 假阳性教训）**：confirmed 级头衔结论须引用责任主体页**原文片段**（≥1 句原话，如"2009 年获得国家杰出青年科学基金"、NSFC 批准号 61125305），仅给链接不引原文最多算 likely——run3 曾断言孟德宇为杰青（实际为长江特聘 + 青年拔尖），Round2 复核时原页面无此内容，转述失真在无原文引用时无从发现；验收方同样应对**沿用项**（非新增）抽样复验
9. **搜索不可用时的 WebFetch 直取策略（2026-08-18 定稿，R7 补丁）**：搜索配额耗尽 ≠ BLOCKED——直取不依赖搜索配额，按 **URL 可猜性**排序推进：
   - **取页通道优先级**：agent 的 WebFetch/webReader 配额耗尽时，**改用 `paper web crawl <url>`**（skill 自带合规爬虫，独立于 agent 工具配额——2026-08-18 R7 实证：webReader 全挂当日用它抓到工程院院士页原文）
   - **搜索通道独立配额（ZCode 环境特有，2026-08-19 R14/R15 双轮实证）**：主 WebSearch/webReader 配额耗尽（1310 周/月限额）时，改用 `mmx search query --q "<关键词>" --output json --non-interactive --no-color`（本机 mmx CLI，独立配额池，≤10 条/次、无分页、换 --q 多试）——两轮在主搜索全挂下靠它完成六维度头衔核验；**搜索摘要必须以直取来源页原文复核**（实测摘要出现过 venue 张冠李戴）
   - **URL 发现走列表页导航，不靠猜**：学院/单位"师资队伍"列表页 URL 相对稳定且服务器端渲染（如 USTC `faculty.ustc.edu.cn` 站内导航、各校计算机学院 `jsdw/szdw` 栏目）——先取列表页，再从页面链接定位个人主页，等价于搜索引擎的发现功能
   - 直猜个人页时执行**机械变体枚举（2026-08-18 R11 教训：变体不穷举 = 白干）**：动手前先生成**完整变体清单**——全拼连写 × 每个多音字读音 × 姓/名分隔形式（连写 / `姓.名` / `姓-名`）× 数字后缀（同名区分），**全部试完才能记"未命中"**；禁止在单一拼写上穷举 URL 形状。R11 实例：外部引擎对查正军把 `zhazhengjun` 的 5 种 URL 形状全试完失败，却从未试正确的 `chazhengjun`（skill 已写明 cha|zha 变体仍未执行）；翟广涛拼成 `zhiguangtao`（应为 zhai 姓）
   - 直取成功的页面照常进证据链（原文引用，confirmed 标准不变）；列表页与多变体均未命中即列入待核验名单，**不得继续盲猜打站**
   - 模型记忆在任何层都不构成证据（§8 禁令不变）；**BLOCKED 只在直取（含爬虫通道）与搜索双通道均尽后判定**

---

## 11. 信息反向挖掘（引用者画像）

> 场景：拿到一篇**种子论文**，反查"谁引用了它"，再为这批引用者建立完整画像（作者/机构/领域/代表作/venue/时间线/头衔）。
> 定位：**CLI 给结构化原料（确定性原语），判断与融合由 agent 方法论完成**——CLI 不做自动消歧、不自动核验头衔、不落主库（分析产物即 CSV/JSON 文件）。**2026-08-12 起，作者自动消歧从全部采集路径移除**（阈值合并中文场景实测不可靠），采集只返回原始作者 + evidence。

### 11.1 工作流（6 步）

```
① 锁人 + 作品清单多源合并（必做，见下方"多源作品清单"）  paper author profile <id>（看 entity_flags）
①b 种子校验（必做） 确认所选种子论文在"多源合并后的完整作品集"中引用数仍为最高；
   用 ≥2 种引用口径（OpenAlex + Semantic Scholar，`paper source semantic_scholar get <DOI>`）交叉确认；
   无 DOI 的会议论文以 S2 citationCount 为准。不满足 → 更换种子论文
② 反向引用       paper trace-citing <id> --sources openalex,opencitations --output citing.csv
③ 展平作者       paper trace-authors citing.csv --output authors.csv
④ 画像特征       paper trace-profiles authors.csv --output profiles.csv
⑤ 消歧           agent 方法论判断（见 11.2，不自动合并）
⑥ 头衔核验       agent web fetch 官方源（衔接 §10 / docs/titles-source-map.md）
⑦ 收尾报告       按 §12 产出 EVIDENCE-CHAIN.md + HTML，与画像 CSV 同目录归档
→ 输出：最终画像 CSV（作者/机构/领域/代表作/venue/时间/头衔/来源链）+ 证据链报告
```

> **多源作品清单（强制，不可只信单一数据源）**：任何单一数据源的作品清单都可能是**不完整**的——OpenAlex 有实体归属错误/拆分（§8，`profile` 的 entity_flags 可提示；**entity_flags 命中的疑似漏检作品必须逐篇核实后才能取舍，禁止凭记忆排除——§8 硬规则，2026-08-17 MBLLEN 教训**）、Semantic Scholar 作者实体也会拆分（同一人多个 author_id，需按姓名+机构+代表作互认后合并）、Crossref 只收录有 DOI 的期刊/会议论文（**无 DOI 的会议论文如 BMVC 2018 MBLLEN——实为陆峰本人通讯论文、GS 1121 引——完全不在其中，OpenAlex 对其引用追踪也偏弱，勿以单口径判"被引最多"**）。因此锁人后**必须**用 ≥2 个独立来源构建作品清单并取并集：
> 1. OpenAlex：`paper author profile <id>`（274 篇量级）+ 检查 entity_flags 命中的疑似漏检作品；
> 2. Semantic Scholar：作者作品接口（`citationCount` 排序，**能覆盖无 DOI 会议论文**）；
> 3. Crossref 按 ORCID 枚举（`filter=orcid:<id>`，只含 DOI 作品）；
> 4. DBLP 作者页（出版记录全，辅助交叉）。
> 任一来源都不得作为唯一清单；以并集为准，逐源标注作品归属。

> **多口径引用交叉（强制）**：结论"某论文为某作者被引最多"前，必须用 ≥2 种引用口径验证（OpenAlex `cited_by_count` + S2 `citationCount`；注意 **S2 引用图是自建口径，并非 GS 数据**，只能作 GS 的独立近似源，且通常 ≤ OpenAlex ≤ GS；**无 DOI 论文 S2 常为唯一可取口径**）。两口径差异 >50% 时视作疑似归属/收录问题，先排查同名实体与归属错误（§8），再下结论。GS 精确引用数获取技巧见 **§11.4**。

> **种子竞品覆盖（强制，2026-08-17 复发教训）**：种子校验的对比表必须覆盖作品清单按**可获取口径**排序的**前 ≥3 名竞品**，逐篇列出引用数，并**按最强口径正确标注次席**——run4 与 Round1 均漏列真正的次席（Reflection Backdoor，S2 621，高于所列"第二名"PR 论文 555）；Round3 表格已覆盖但摘要仍把第三名误标为"次席"；Round4 竞品集被**主题预过滤**（只测了低照度方向 340/333/264，漏掉安全方向的 RB 621 与医学对抗 555）。**竞品集必须由全量作品清单（DBLP / S2 author profile / 官方出版页并集）按可获取口径降序产生，禁止按研究方向预过滤**；限流下宁可少测，也必须先测排序最靠前的。对比不完整或标注错误即校验不完整。

各原语输出列：

> **头衔候选生成（强制，2026-08-17 Round1 教训）**：为引用者核验头衔（杰青/院士/Fellow 等）时，候选短名单**必须由全池画像驱动**，三条硬要求：
> 1. **全池 h 指数必取**：OpenAlex 批量（`authors?filter=ids.openalex:A|B|…`，≤50/请求）或 S2 `author/batch`（≤1000 id/请求）。**OpenAlex 不可用时 S2 兜底是硬要求——换用自写采集脚本时，CLI 原语的兜底契约必须随行，不得因采集方式改变而丢失画像步骤**（Round1 自写 S2 驱动后画像整个缺失，短名单退化为纯频率排序，漏检 8/10 位真杰青：黄庆明 h=84 / 查正军 h=79 / 李厚强 h=78 / 孟德宇 h=78 / 卢孝强 / 於志文 / 杨健 / 吴枫，多为引用频率仅 1-3 次的资深作者）。
> 2. **短名单构成** = h≥40 ∪ 引用频率≥2 ∪ 封闭名录命中；**仅凭频率的短名单不合格**。
> 3. **核验顺序与配额管理（Round3 教训）**：搜索配额有限时按 **名录命中 > 高频≥3 > h≥50 > h40-50 段** 的优先级逐人核验（每人 1-2 个 query，教师页 URL 可从 DBLP/单位域名直取以省搜索配额）；配额耗尽按 §10 要点 6 停止，但**未核验名单必须整段显式列入报告（含 h 值与频率），禁止静默丢弃**——Round3 因配额耗尽跳过 h30-50 段 30 人，漏检 於志文（h≈51）与两位杨健（h≈35）；若该段当时被列出，验收方可立即发现缺口。
> 3. **封闭名录头衔优先做闭集交叉**：两院院士（工程院 + 中科院全名录）、IEEE/ACM Fellow 等有官方名录的，**全名录 ↔ 引用池做交集**（名录当日直取官网固定页，无搜索依赖，见 §10 要点 9），记忆观察名单只作补充不作主干——2026-08-17 实例：工程院 1071 + 中科院 886 名录交叉发现了所有预置观察名单都漏掉的桂卫华院士（中南大学，2013，流程工业方向，不在 CV/安全圈的记忆名单里）。
> 4. **子 agent 并发提速（2026-08-18 外部引擎 4.5h vs 常规 1h 的教训）**：独立可并行的阶段**必须并发**——若执行环境支持 task/subagent 工具，派 **5-6 个或更多**子 agent 并行：① 种子竞品逐篇引用数查询（每篇独立）；② 头衔候选**逐人/逐批核验**（每个子 agent 负责若干人，产出独立证据文件，主 agent 汇总）；③ 名录交叉后的同名排除核验；④ 引用池分页采集。**并发纪律**：不同候选/不同域名可任意并行；**同一限流端点（S2/OpenAlex 等）禁止并发**（= 加速封禁），端点内改用批量 API（如 `author/batch` ≤1000 id/请求）；对外部网站礼貌并发（同域名串行、限速 1 req/s 红线不变）。不支持子 agent 的环境退化为批量 API + 顺序执行，并在报告注明
> 5. **短名单门槛前必须姓名级聚合（2026-08-18 R13 教训）**：S2 实体拆分会让同一学者散成多个低 h / 低 freq 实体——套用 h≥40 ∪ freq≥2 门槛**之前**，必须先按姓名（+机构互认）聚合：**h 取各实体最大值、freq 求和**，再筛。R13 实例：马华东（真杰青，S2 主实体 h≈53）因池内记录落在 freq=1 的拆分实体上，与郭斌、杨健（两位真杰青）一同被短名单漏筛；名单聚合后 35 分钟并发轮反而漏掉 4 位串行轮（R12）能拿到的人

> **多维度头衔画像（职称/人才 title/Fellow/编委/会议主席，2026-08-19 R14/R15 双轮教训）**：任务含多维度头衔时三条规则：
> 1. **每维度先保底再加深**：按 院士 > 杰青 > 国家级人才 > Fellow > 职称/编委/主席 推进，但每个维度先完成短名单前列核验再进入下一维度——预算随机倾斜会造成覆盖波动（双轮各维度产出互补，杰青单轮 4-8 人、并集 9 人；查正军 R14 confirmed 而 R15 漏列）
> 2. **学会 Fellow 优先闭集交叉**：IAPR 官方名录可直取（R14 一次交叉挖出 13 位，含南理工杨健 2016 与北理工杨健分别计）；ACM 名录 JS 渲染不可机读（两轮实证）→ 降级为 IEEE 年度名单多源交叉（引"入选理由"原文）
> 3. **会议主席/编委验证降级链**：主会官网不可达（SPA/反爬）时**降级到分会官网**（ACM MMAsia / ICMR / ICME 等）与往届会议页，再降级到出版方编委页——R15 用此链完成主席维度（吴枫 TCSVT 主编、刘家瑛 MMAsia-2023 等会主席、李厚强 VCIP 2010 PC Chair），R14 因只盯 ACM MM 主会而整维度 pending；每条结论必须带届次/年份时间边界（§0.5.4），角色用受控词表不向上推断（Area Chair ≠ Program Chair）

| 原语 | 输出文件 | 列 |
|---|---|---|
| `trace-citing` | citing.csv | citing_paper_id / doi / title / year / venue / authors_raw / authors_detail（compact JSON，含 author.id / display_name / institutions，链式保留 detail） |
| `trace-authors` | authors.csv | author_name / appears_in / affiliation / author_id |
| `trace-profiles` | profiles.csv | author_name / author_id / institution / h_index / fields / works_count / top_works |

说明：
- `trace-citing` 双源交叉（OpenAlex `filter=cites:` + OpenCitations `citations/{doi}`，后者以 DOI 为键；`references/{doi}` 返回的是论文自身引用了什么，方向相反，勿用），内置 429 退避与断点续传（`--resume-from`）；`--limit` 作用于合并后总结果，双源不会被静默跳过
- `trace-authors` **只展平不合并**——同名作者各占一行，保留原始署名机构；`--affiliation-filter <关键词>` 是机械子串过滤，非消歧判断
- `trace-profiles` 按 author_id（如有）拉 OpenAlex 档案与代表作（按引用排序）；无 ID 的行返回占位（author_id=None），**不自动搜索匹配**——留给 agent 方法论

### 11.1.1 降级与快照（OpenAlex 额度不足时的路径）

- **S2 自动兜底**：OpenAlex 429/超时/5xx 时，`trace-citing` 与 `trace-profiles` **自动转 Semantic Scholar**（免费 100 req/5min）——`profiles` 的 S2 结果标 `source="s2"`（同名风险，需 agent 消歧确认）；也可 `--sources openalex,opencitations,semantic_scholar` 显式指定
- **无 DOI/arXiv 种子的 S2 paperId 直驱（配方，Round4 BLOCKED 教训）**：CLI `trace-citing` 对无 DOI 种子解析失败报 permanent 时，**不得据此判 BLOCKED**——两步 API 直驱：① `paper source semantic_scholar search "<精确题名>"`（或 S2 `/paper/search?query=`）拿 paperId；② `GET /graph/v1/paper/{paperId}/citations?fields=title,authors,externalIds&limit=1000&offset=0,1000,…` 全量分页（429 时 60–90s 有界退避，804 条约 9 页）。S2 **网站**引用页（50 条/页、仅题名无作者）不是替代品。2026-08-17 同日 R2/R3 用此配方全量成功，R4 误判"S2 兜底需 DOI/arXiv 结构性失效"而整段 BLOCKED
- **机械降级触发器（2026-08-18 外部引擎教训）**：对同一端点**连续 ≥3 次 429**（或单条目耗时 >2 分钟无进展）即判定该端点当日结构性不可用，**立即切换兜底通道**并在报告记降级——禁止在同端点对后续条目继续逐条重试。原则性表述（"OpenAlex 耗尽时转 S2"）对轻量模型不够硬：外部引擎轮（deepseek-v4-flash）曾在 OpenAlex 429 上逐作者死磕 45 分钟仅推进 1 人，需人工注入指令才转弯
- **OpenAlex 免费快照（可选下载，彻底零额度）**：

```bash
paper snapshot status          # 本地快照状态（日期/works 数/引用边数/路由开关）
paper snapshot download        # 下载季度快照（提示大小后用户主动确认；断点续传）
paper snapshot build           # 解压建 SQLite 索引（works + 引用倒排）
paper snapshot enable          # 路由开关：trace-citing 默认先查本地
paper trace-citing <id> --use-snapshot   # 查本地引用索引（未命中回退 API）
```

- 规模提示：全量 works 快照解压后约 20-60 GB，个人使用请评估磁盘；下载是**用户主动选择**（轻量使用可只靠 S2 兜底 + 免费额度）
- `profile.source` 语义：`openalex`（主源成功）/ `s2`（兜底成功，占位质量）/ 双失败无数据（CLI 计 failed）

### 11.2 消歧方法论（agent 判断，不自动合并）

- **ID 直连优先**：author_id（OpenAlex / ORCID / S2）相同 → 同一人（强证据）
- 无 ID：**机构 + 领域 + 代表作品 + 合著者**特征对比
- 多候选并列展示，标注置信度：**确认 / 疑似 / 存疑**
- **同名多人均可为头衔持有者（2026-08-17 Round2 教训）**：同一姓名在引用池中出现 ≥2 个独立身份指纹（不同合著网络/署名单位）时，**必须逐身份分别核验头衔，同一姓名可以对应多位头衔持有人**——杨健实例：池中 3 个 Jian Yang 实体 = 南理工杨健（杰青 2011，MambaLLIE/TITS 团队指纹）+ 北理工杨健（杰青，内窥镜论文合作者宋红指纹），两人都引了 MBLLEN、都计入；把同名收敛到"一个人"会漏掉另一位
- **领域直觉排除（禁止，2026-08-18 R13 教训）**：名录/候选命中不得以"研究方向不相关、不像是会引这篇论文的人"直觉排除——**跨领域引用是常态**（流程工业的炉膛成像引用低照度增强）。排除依据只能是**身份指纹**（署名单位/合著网络/h 量级/年龄在职状态），领域相关性只能作补充线索且不得单独构成排除理由。R13 实例：桂卫华（工程院院士 2013，控制方向）被 CAE 子 agent 以"方向无关 + 疑似低 h 青年"误杀，而其真实引用论文《高炉料面光影重建》正是工业场景使用 MBLLEN——该引用边经 Crossref 参考文献验证为真（前 6 轮均正确命中）
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

### 11.4 Google Scholar 引用数获取技巧（合规版）

> 红线：GS 无官方 API，ToS 禁止自动化访问。**绝不**自动化爬 GS、绕过验证码、IP 轮换/代理池、用第三方"GS 镜像/代查 API"（含 SerpAPI 类）。以下全部为合规手段。

**口径认知（先记住）**：
- 引用数排序规律（经验，非逐篇保证）：**Crossref ≤ S2 ≈ OpenAlex ≤ GS**。GS 计入会议论文/学位论文/书籍/报告/预印本/课程讲义，通常最高；S2 引用图为**自建口径**（非 GS 数据），不收录专利、图书覆盖有限；Crossref 只统计成员提交参考文献且匹配到 DOI 的记录，通常最低。
- **禁止**经验换算（如"S2 × 1.2 = GS"）与跨论文混用口径（A 用 GS、B 用 OpenAlex 直接比较）。

**推荐（默认路径，覆盖 90% 场景）**：
1. 用 OpenAlex `cited_by_count` 为主口径，S2 `citationCount` 互验（CLI：`paper source semantic_scholar get <DOI>`）；两源差异 <25% 时即可下"量级结论"（如"OpenAlex 540 / S2 554，同量级，GS 口径未获取"）。
2. 报告必须写**每个数字的来源与查询日期**（如"OpenAlex 540（2026-08-13）"）；GS 数字一律标"未获取"或给方向性说明（"GS 通常更高"），不编造。
3. 需要 GS 精确值时，WebSearch 检索 `site:scholar.google.com "论文完整标题"`、`"完整标题" "Cited by"`，从**明确来自 scholar.google.com 域名**的片段取 `Cited by N`——这是带日期的**快照值**（可能滞后数周~数月），写"GS 搜索快照（YYYY-MM-DD 查询）"。

**可用（补充手段）**：
4. 检索词技巧：标题常见时加第一作者姓氏/年份/DOI 限定（`"标题" Smith 2021 "Cited by"`）；精确标题无结果时改用标题中连续 8–12 个辨识词，或试预印本/正式发表两个标题变体。
5. `site:` 无结果 ≠ 零引用（搜索引擎可能不索引 GS 结果页）——只能标"WebSearch 未发现"，不能记 0。
6. Crossref `is-referenced-by-count` 作"下界锚点"；OpenCitations（COCI）作可复算的审计口径（注意其开放源覆盖偏倚）。

**兜底（最后手段）**：
7. 仅当报告核心论文需精确 GS 口径裁决时，真人浏览器单次低频查询 scholar.google.com，记录：数字、查询日期、版本聚类（"Cited by N，合并 N 个版本"）、匹配标题/作者/年份。
8. 遇 GS 反爬信号（HTTP 000、空页、"unusual traffic"验证码）**立即停止**，当天不再重试（重试加深封禁），降级到方案 1–2。

**常见坑**：GS 索引更新滞后（收录约 6–9 个月）；GS 可能把预印本与正式版聚类合并而 S2/OpenAlex 不合并，比较前核对版本；"作者页 h-index / citations"与"单篇 Cited by"是不同指标不可互换；S2 `influentialCitationCount` ≠ `citationCount`，取数时别拿错字段。

---

## 12. 任务收尾产物：证据链报告（每次任务强制）

**每次用户任务完成时，必须产出一份可浏览器打开的 markdown 证据链报告**：聊天回复给结论，报告给"结论怎么来的、如何逐条验证"。2026-08-17 决策：源于真实失败——任务做完只留在聊天记录与临时文件里，用户无法回溯验证（基线：用户两次追问才补出报告形态）。

### 12.1 产物契约

| 项 | 要求 |
|---|---|
| 位置 | **固定为 `<工作目录>/analysis/<task-slug>/EVIDENCE-CHAIN.md`** + 同名 `.html`（task-slug 用英文短横线，如 `reflection-backdoor-citers`；不得放在工作目录根下——Round5 偏差）|
| 数据归档 | 任务中间产物（CSV/JSON 等）全部归档到该目录；**禁用系统临时目录**（重启即丢）|
| 渲染打开 | `python scripts/render_report.py analysis/<task-slug>/EVIDENCE-CHAIN.md --open`（python-markdown + 内嵌样式；生成后双击 HTML 亦可打开）|
| 链接 | 每条结论、每个数字挂可点击验证链接（绝对 URL 或同目录相对路径）；数字必须带来源 + 查询日期（§11.4 约定）|
| 完成判定 | 报告 md + html + 归档数据三者齐备，且聊天最终回复指明报告路径，任务才算闭环 |
| 勘误块 | 结论被后续核验推翻时，在旧报告**顶部加"⚠️ 勘误"块（不删原文、写明正确结论与出处）**；新任务开始时必读既有勘误块作约束（§10 要点 7）|

### 12.2 报告固定骨架（按序填充；简单任务可收缩，不可缺头尾）

1. **头部元信息**：任务一句话 / 查询日期 / 所用方法（引用本 skill 章节）
2. **链路总览**：步骤流程图 + 步骤表（每步 = 操作 / 命令 / 产出文件 / 一键验证链接，如 OpenAlex API URL）
3. **结论表**：实体名链接到权威来源（官方名录 / DOI / 单位官网）
4. **逐条证据卡**：每条结论三层证据，齐全才计入——
   - 📄 原始证据：论文 DOI / 页面 URL（点开可核对署名机构等）
   - 🆔 结构化实体：OpenAlex/S2 ID 的 API 链接
   - 🏅 责任主体源：头衔/事实的官方发布方（§0.5.3 声明—主体匹配）
5. **排除与陷阱记录**：负结论同样给证据链接；同名排除写明比对依据（机构指纹）
6. **口径与局限**：快照日期 / 数据口径差异（OpenAlex vs S2 vs GS）/ 未覆盖范围 / 置信度四值标注（§0.5.5）

单命令即可回答的简单任务：报告收缩为"结论 + 一键验证链接 + 日期"三行骨架——但文件必须存在，节可省标题不可省。

### 12.3 渲染工具与参考实现

- 工具：`scripts/render_report.py`（md → 独立 HTML，`--open` 调默认浏览器；依赖 `pip install markdown`）
- 参考实现：`analysis/reflection-backdoor-citers/EVIDENCE-CHAIN.md`（首届实例，2026-08-17）——468 篇引用反查 → 777 份画像 → 院士/杰青逐人三层证据卡 + 14 条排除记录 + 口径局限声明

---

## 13. 任务类型方法论速查（2026-08-19，15+ 轮实战迭代沉淀）

### 13.0 横切原则（适用于一切任务）

| 原则 | 一句话 | 规则出处 |
|---|---|---|
| 多源交叉 | 任何"最/第一"类结论 ≥2 独立口径 | §11.1 |
| 闭集优先 | 有官方名录的头衔做全量交集，记忆名单只作补充 | §11.1 候选生成 3 |
| 责任主体 | 头衔/事实找发布方页面，自述不作最高证据 | titles-source-map §0.5.3 |
| 原文内嵌 | confirmed 级结论必须引用页面原话 | §10 要点 8 |
| 聚合先于筛选 | 同名实体先合并（h 取 max/freq 求和）再套门槛 | §11.1 候选生成 5 |
| 机械降级 | 同端点 3×429 即换通道，不死磕 | §11.1.1 |
| 显式留白 | 没验完的整段列名单，禁止静默丢弃或编造 | §10 要点 6 / §8 |
| 并发分片 | 独立任务派子 agent，限流端点主线程独占 | §11.1 候选生成 4 |
| 时间边界 | 每条头衔/职务带年份或"截至查询日" | §0.5.4 |
| 机构全称 | 机构结论写「大学全称+学院」，禁止只写城市名——同城多校易误读 | 2026-08-19 R18c：任文琦"深圳（网络空间安全学院）"实为**中山大学**（深圳校区）网络空间安全学院，只写城市会被读成深圳大学 |
| 前提核验 | 任务/用户给定的归属与事实也要验证，未证实就显式修正前提，不因"题面这么说"而采信 | 2026-08-19 R17d：任务称马佳义属"国家多媒体软件工程技术研究中心"，官网无其条目、领导名单无此人，全部权威源一致为**电子信息学院**——显式修正并保留 not_found 判定 |
| 证据自证 | 引用任何 PDF/文件作一手证据前，先核对其自身身份（首页题名+DOI 与目标论文一致）；repo/网页提供的下载物可能张冠李戴 | 2026-08-20 R26：SGM repo 提供的"SGR 论文"PDF 实为 Yang 等另一篇 TIP 2021（Band Representation），据此差点下"SGR 无 LOL-v2"错误负结论——核对题名后作废，误下载文件留档作陷阱记录 |

### 13.1 任务速查表（任务 → 方法链 → 特有坑）

| 任务类型 | 方法链（入口 → 产出） | 特有坑与已验证技巧 |
|---|---|---|
| **学者画像 / 代表作** | 锁人（官方主页/CV/DBLP 实体号）→ 多源作品清单（DBLP ∪ S2 ∪ Crossref-ORCID ∪ 官方页并集）→ 双口径引用排序 | OpenAlex 实体污染/拆分（§8）；官方页漏列的作品用 ORCID+合著网络裁决（R11：PR 2020 不在官方页）；同名实体号先验真（DBLP Feng Lu 0003≠0005） |
| **被引最多 / 影响力排序** | 同上 + 种子竞品覆盖（全清单按可获取口径降序前 ≥3，禁主题预过滤） | 无 DOI 会议论文 S2 为唯一强口径、OpenAlex 结构性低估（MBLLEN 316 vs 804）；同名高引作品先核归属再计入 |
| **引用者画像 / 头衔核验** | §11 六步 + §10 六维度（院士>杰青>人才>Fellow>职称/编委主席，每维先保底） | 详见 §11/§10；编委/主席带届次年份，受控词表不向上推断 |
| **同名消歧** | 身份指纹 = 署名单位 + 合著网络 + 代表作（≥2 项吻合才合并） | 同名可对应多位头衔持有人（杨健×2）；领域直觉不得单独排除（桂卫华：控制方向引低照度）；S2 多实体先聚合 |
| **领域调研** | 检索（`paper query papers --keyword` FTS + 逐源 `source <源> search`，中英文+同义变体多轮）→ 聚合去重 → 论文层（高引排序）/学者层（h 聚合）/团队层（机构+合著聚类）→ 综述甄别（title 含 survey/review + 期刊口径） | 检索词变体决定召回（low-light/underexposed/dark）；S2 搜索限流走逐源；结论按论文/学者/团队三层分开给，不混口径。**R16 首验实例**：团队层从 Top 论文反推（高引论文的机构+带头人聚类）比先列团队再找论文稳；挑战赛报告（NTIRE 类）按综述甄别单列不入榜；预印本/正式版拆分取 max；双口径排名冲突处（S2 与 Crossref 序不同）如实并列注明；头衔核验复用 §10（团队带头人张艳宁 = 双独立源 confirmed） |
| **学者对比** | 各自画像（第一行）→ **同口径对齐**（同一查询日、同一引用源、同名先聚合）→ 维度表（h/总引/代表作/头衔/活跃年段/方向） | 禁止跨口径比较（A 用 GS、B 用 S2，§11.4）；头衔按各自责任主体源核验后并列，不做主观加权；任务给定的单位归属要核验后修正（§13.0 前提核验）。**R17d 首验实例**：①OpenAlex 实体污染双向失真要量化修正（陆峰实体混入外领域 701 引、MBLLEN 被拆到另一实体漏 316 引）；②出版方编委页 403 时按要点 9 换 webReader 通道可取到原文（ScienceDirect 反爬对 WebFetch/爬虫 403、webReader 成功）；③bio 相邻归属不可裁决时记 indeterminate 不硬挂（BDCLOUD 2019 GC 案）；④头衔具体化要有授予方/学会原文支撑，单位页只写"国家级青年人才项目"未具名时保持 likely（陆峰青年千人：仿真学会原文"次年获评青年千人"+中组部名录不公开） |
| **后续工作追踪** | 种子 → 引用分页（无 DOI 用 S2 paperId 直驱配方）→ 年份/关键词过滤 → 版本去重（预印本 ↔ 正式版）→ 技术路线多标签聚类 → 团队榜（姓名级聚合 + 机构网络核验）→ 直接后续（原作团队官方页 + 作者作品接口双源） | 版本聚类差异（GS 合并、S2/OpenAlex 不合并）；引用图有时滞，"近 N 年"标注快照日；中文标题去重键不能只用 `[^a-z0-9]` 归一（纯中文压成空串会误并，改用 paperId）；挑战赛报告（NTIRE 类 70+ 作者）单列不入团队榜；团队归属以作者名单逐篇核对（Retinexformer 曾误归 Wenqi Ren，实为清华 Cai 簇）；S2 author/batch 机构字段覆盖率极低（实测 ~1%），机构结论必须联网核验；原作谱系以实验室官方出版物页原文为准。**R18c 首验实例**：804 全量单页枚举与 citationCount 一致性校验；2023+ 窗口 551 篇按多标签路线聚类（Retinex 18.5%/经典 CNN 20.9%/融合 16.7%/夜间下游 15.8%），趋势结论按路线份额逐年演变给出且注明样本量 |
| **最新动向** | DBLP 近作 + S2 author 作品分页 + 官方主页/机构新闻（mmx search 独立配额） | 动态页标注抓取时点；新闻转载与官方公告区分；自述动向需权威源裁决。**会议职务检索范围 = 官方自述清单 ∪ 近两年有论文发表的会议（尤其同届）∪ 领域 CCF-A 全集抽查**——不得只照自述清单检索（2026-08-19 R19 教训：教师主页自述只列 CVPR/ICCV/ECCV/NeurIPS/ACMMM，agent 照单检索漏掉 AAAI 2026 PC——官方页在列且其 AAAI 2026 有 3 篇论文，"有论文发表的会议必查其 committees"）。**R19 首验实例**：近作清单=官方出版物页∪DBLP 并集+Crossref/arXiv 逐条核验（29 篇）；DBLP 展平文本解析相邻记录会错位，用 DOI 锚定重解析；奖项类以单位官方新闻为责任主体源（CHI 2025 Best Paper 北航学院新闻直证，ACM DL 403 时降级此路径）；委员会名单页多无单位标注，身份由官方自述佐证时封顶 likely |
| **全文获取** | §3.4 合法 OA 管线 | 付费墙红线（§7） |
| **会议/期刊背景** | 官网 committees/编委页直取（当届）+ Wayback（历史届）+ CCF 分级（ccf.org.cn 为准） | SPA 官网降级链：主会 → 分会（MMAsia/ICMR/ICME）→ 往届 → 出版方编委页（§11.1 多维度 3）。**R20 首验实例**：①主编更替年份用 **Wayback 快照夹逼法**（编委页/首页多时点快照二分区间，独立时点旁证如新投稿论文作者简介可收窄窗口）；②CCF 收录状态必须 ccf.org.cn 类目页**逐页闭集枚举**，搜索摘要给出的"CCF B"类标签有假信息（R20 实例：首轮摘要称 Inf Fusion 为 CCF-B，逐页证伪实为未收录——与中科院 1 区 Top/IF 17.4 形成反差，此类反差结论要显式写出）；③指标三口径各自注明年份版本（JCR 影响因子注明 JCR 年+发布月、中科院分区注明升级版年份、发文量 OpenAlex 与 Crossref 分列不混用）；④创刊信息以 Vol.1 实体记录（DBLP 卷期页+创刊社论）为准，不用第三方转述 |
| **方法扩散/点名率测量** | 种子 → S2 citations 全量枚举（title+abstract 机械点名规则：方法名变体枚举+全称展开+大小写/连字符，规则写附录可复算）→ 对比方法同池同规则 → 分层结论 | 点名率必须**三层口径分列**：标题层（≈0 是常态）/摘要层（下界——摘要通常不列 baseline，且 S2 摘要覆盖仅 ~70%）/**引用句层**（用 S2 citations 的 `contexts` 字段，覆盖约 64% 边，最接近真实 baseline 使用；R21 实测：MBLLEN 摘要点名仅 0.73% 但引用句点名 62.5% 且逐年下滑 70.9%→44.7%——"高被引≠高摘要点名"）；对比份额要**剔方法原文自引**（原论文自身摘要含方法名，R21：Retinexformer 原文即池内命中）；headline 数字应并入变体展开口径或显式说明只计 primary |
| **合著网络桥接** | 两端锁人（DBLP 实体号）→ 各自一级合著者集合（原始 XML 全量抽取）→ 集合求交得 2 跳桥 → 无交则展二级 → 每跳论文证据（题名/venue/年份/链接）+ 中间人身份指纹（同 pid 或机构+代表作）| **DBLP XML 必须用 XPath 逐记录 schema 抽取**（trafilatura 等正文抽取器会丢作者行，R22 实测丢 Qinping Zhao；用"每记录必含本人"不变量校验）；**DBLP publ API 的 author: 组合查询有假阴性，否证类结论（无合著/无更短路径）只能靠原始 XML 全枚举**；最短性证明=一级合著者集合求交（R22 实例：陆峰 L1=213 ∩ 马佳义 M1=682 仅 Yu Li 0003 一人=唯一 2 跳桥，边论文 AGLLNet IJCV'21 + MGIF MM'17/TPAMI'20）；基名碰撞（Xin Liu 0012≠0091）按指纹排除，禁止仅凭姓名连边 |
| **论文完整性审计（撤稿/更正）** | 论文池（DOI/标题清单）→ Crossref 双向核查（正向记录 + 反向通知检索）→ arXiv withdrawal 标注 → 出版方页面横幅 → Retraction Watch 交叉 → 正结论五层证据 | **通道金规律（R23 首验）**：①Crossref 反向撤稿通知检索只有 `filter=relation.object:<DOI>` 有效（`relation.object-id` 与 `relation.type:is-update-of` 均无效，金标准=Surgisphere 案例）；②**Hindawi/Springer 撤稿以标题前缀存款（`[Retracted]`/`RETRACTED ARTICLE:`）而非 relation——标题前缀通道必须保留**；③S2 元数据不携带撤稿状态（撤稿论文 S2 标题无标记），不能以 S2 干净为由下负结论；④arXiv 系 DOI（10.48550）重路由到 arXiv 通道查 withdrawal（v2 说明），可把覆盖率从 96% 提到 99.75%；⑤arXiv 作者撤回分两类：替换式撤回（"to be replaced by new version"）非不端，单列不计入撤稿数；⑥正结论宁可少报不可错报（撤稿假阳性代价高），负结论声明通道盲区（RW 数据库 JS 不可机读、出版方不存款的勘误）。R23 实例：804 引用池挖出 2 篇撤稿（均与被引对象无关的原因）+1 篇替换式撤回 |
| **奖项后续追踪（award aftermath）** | 官方 awards 页锁定获奖名单（禁凭记忆）→ 身份卡 → 双口径引用 + **同届全量分位数** → 开源仓库（gh api star，注抓取日）→ 衍生采用举例 → 一作去向（官方主页为准）→ 完整性顺检 | 获奖名单常有多篇并列（CVPR 2023/2024 均为双最佳论文——任务说"每届 1 篇"要按 §13.0 前提核验顶回）；同届分位数用 **Crossref 全量同届分布**（filter container-title+年份，n≈2400-2900，与官方底册差 <1%）而非抽样；"获奖声誉 vs 短期引用"可能出现大反差（R24：CVPR 2024 GID 最佳论文仅 84.6 分位且无官方仓库，三通道证伪），反差本身是可报告发现；一作去向以本人主页/机构页为准，S2 实体污染不能直证时标 likely 留痕 |
| **数据集谱系与采用率** | 领域内数据集全枚举（变体正则表写附录）→ 每个数据集一手核验谱系（提出论文 DOI/repo 原文/论文 PDF 引文，置信 C/L 分级）→ 谱系边逐条原文内嵌 → 采用率=种子引用池上 title\|abstract 点名并集（三层口径）→ 同名碰撞限定词规则+人工裁决 → 路线×数据集交叉表（n 小格子不解读） | **点名扫描的变体表要与实现一致并用已知命中反测**（R26 实测：附录声称大小写不敏感，"Exdark" 小写 d 变体仍漏检 2 篇——用几篇已知点名论文反测扫描器可提前暴露；无分隔符写法 LOLv1/LOLv2 必须进变体表，`\bLOL\b` 词边界正则会漏）；数据集提出论文归属矛盾时读**使用者论文的参考文献表**定归属（R26：LOL-v2 归属 TIP 2021 SGR 而非 CVPR 2022，SNR-aware 原文 ref 表闭合）；经典非配对小集的规模数字多为社区口径（标 L 留白），官方源只证存在与提出论文；谱系边的演进声明（"Following the SID dataset"/"We instead propose"）是最强边证据 |
| **期刊审稿/出版时滞** | Crossref ISSN 全量抽样框 → 字段覆盖率审计（先证可得性再测量）→ 多通道日期采集（Wayback 出版方页快照/Xplore 快照原始 HTML/OA PDF/PubMed/arXiv）→ created 语义多源校准 → **口径分列统计（真值/created 在线/arXiv v1 上界三口径禁混算）** | **通道金规律（R25 首验）**：①多数刊**不向 Crossref 存 received/accepted**（InfFusion/TIP 各 300 篇审计 0%）——审稿周期禁从 Crossref 取，字段审计先行；②**绿色 OA 的 submittedVersion PDF 不含出版方日期**（占位符 "Manuscript received XXX"），OA PDF 通道对时滞测量基本死路（取到的 PDF 仍要按证据自证核对题名）；③ScienceDirect live 页全通道 403，webReader 可取正文但**剥离 Article history 区块**——真值唯一来源=Wayback 200 快照的**原始 HTML（`id_` 形式）**，日期藏在内嵌 JSON（markdown 转换会丢）；④IEEE 侧：Xplore 快照原始 HTML 含 insertDate（=Crossref created=在线日），无审稿日期（仅付费版 PDF 脚注）；⑤**arXiv API 编码陷阱：`quote()` 把 `+AND+` 编成 `%2B` → 假零命中**，且 `ti:` 全词 AND 对连字符词失配（用 `all:` 字段+最长 5 词）；⑥结构性不可测要作为**第一发现**报告（R25：TIP 0/20、InfFusion 3/20——"元数据存款习惯差异"本身是可靠对比结论），逐篇留白禁推算冒充 |
| **学术族谱（genealogy）** | 锁人（DBLP 实体号+代表作指纹）→ 向上导师线（本人 CV PDF/官方教师页原文，逐学位列年份与导师名）→ 向下学生线（实验室成员页在读/Alumni 区 + CV 内"学生一作+本人通讯\*"论文行 + 学生本人主页）→ 横向持续合著（导师-学生毕业多年仍合作=师承连续性强旁证）→ 每条边一手来源+去向（实时页面为准） | **边的证据优先级**：本人官方页/CV PDF > 论文原文（署名位/致谢）> 合著模式（密集合著只是旁证）；**无师生边一手证据的高产合著者显式不计为学生**（R27：合著 21 篇的 Haofei Wang 因实验室页/CV 无记载不列）；否证"某导师"要负结论三层：CV/教师页/实验室页零记载 + DBLP 全量 0 合著（R27：王田苗三层全零）；**webReader 转述会失真，去向类关键句要用 curl 原档复核**（R27 两起勘误）；同名实体用 pid+代表作指纹区分（Lu Feng 0005≠Feng Lu 0005=97/6483-5 共 166 条含 MBLLEN）；去向以**实时页面**裁决，过期快照（mmx 缓存的旧职称）要注明已过时（R27：程义华 Birmingham SRF→北理工 Tenure-Track）；硕士导师线单一 CV 源时标注置信（R27：Guihua Er 仅 CV 记载） |
| **学者迁徙画像（migration）** | 同名锁实体（S2 authorId+代表作指纹）→ 官方时间线（教师页学历+履历逐段，写大学全称+学院+校区）→ **论文单位追踪法**交叉验证（跨年份代表作的单位脚注序列，标出过渡带）→ 人才帽子分层（国家级/省级/校级）→ 双口径不一致显式对照 | **双口径纪律（R28 首验）**：官方履历 vs 论文署名序列只要求**过渡带一致**（带内数月滞后正常，R28：TJU→IIE 带 2017.08→2017.12 对应 2017.07 入职），逐点不一致才升级为矛盾；**异常署名站（官方履历无此站）标 indeterminate 不并入履历**（R28：2019 UIEB 论文署名香港城市大学——访学/客座性质不明，禁推算）；学位授予单位讹传机制要解释（毕业后雇主的持续署名易被误读为母校，R28："博士毕业于信工所"讹传由 2017.12-2021.06 信工所署名解释，真值=天大+UC Merced 联合培养，三责任主体源定案）；单位脚注通道用 **ar5iv**（LaTeX 全渲染保留脚注单位行）；帽子分层标注授予层级（校级"百人计划"≠省级"青年拔尖"≠国家级"优青"）；奖项/帽子年份无记载时用带日期的旧简介**时间窗夹逼**（R28：优青 2022-11 无/2023-11 有 → likely 2023） |
| **方法兴衰曲线（rise/decay）** | 方法集分代枚举（经典/中期/新世代）→ 每方法代表作**分年被引曲线**（OpenAlex `counts_by_year` 主实体；S2 citations 分年枚举为备份）→ 趋势分类（判据显式：完整三年斜率+峰值年）→ 份额池看相对结构 → 领域级结论与单方法分层 | **通道金规律（R29 首验）**：①**流传 DOI 可能是错的**（LIME 常见流传 DOI 10.1109/TIP.2016.2636392 无命中——DOI 直查失败先转题名检索再下"不存在"结论）；②**无 DOI 会议论文的 OA 单实体曲线不可单独下衰减结论**（实体拆分伪影，R29：RetinexNet OA 100→9 断崖 vs GS 聚类 414→1066 上升——OA 缺正式版实体）；③**GS 年被引可从搜索摘要的直方图数字串解析**（合规、不爬 GS：连写串拆分须精确重构原串且年值和与快照总量差 <1%，双解以总量裁决；快照缓存时点未知只作形态/量级旁证）；④当前不完整年一律剔除趋势统计（年化值若低于上年=索引滞后非回落，误读会得出"全领域崩塌"谬论）；⑤**版本家族实体不并曲线**（正式版/预印本/扩展版并存，合并会双计）；⑥`paper web crawl` 对 JSON API 返回 200 但 content=0（trafilatura 抽不出 JSON）——用 `paper source openalex search` 的 raw_data；⑦单通道结论标 likely，≥2 独立通道同形态才 confirmed；⑧领域级叙事（"取代"）必须曲线证据：R29 证伪"LIME 衰减被取代"（年引单调上升 225→572），合理内核只是份额稀释——经典 baseline 作为对比对象引用长青，被挤出的是中期世代 |
| **期刊编委互联（board interlock）** | 各刊官方编委页直取当届名单（**角色分层记录**：EIC/共同主编/SAE/AE/Area Editor 不混）→ 跨刊同名匹配 → 逐人身份指纹（双官方页机构一致/IEEE CASS 个人页/本人主页/单位官方新闻）→ 互联矩阵（人×刊×角色）+ 刊对重叠度 → 未能取得名单的刊显式留白 | **通道金规律（R30 首验）**：①IEEE 双体系——TIP 编委页在 **SPS**（signalprocessingsociety.org）、TCSVT 在 **CASS 出版页+逐人 contact 页**，别在 ieeexplore 死磕；②Elsevier 编委页 live 403 → webReader 可取但**长名单会被截断**（R30：截断致归档草稿超出实际内容，自查删除留痕）——归档后必须与源核对完整性，或走 **Wayback 稳定快照**（InfFusion 20260803 快照自 202311 起稳定）；③同名禁连（R30：Jun Liu 同名三人按机构指纹全部排除）；④互联强度分档："双刊 SAE"（Zhenzhong Chen）> "一刊主编级+一刊 AE"（马佳义 TIP AE×InfFusion co-EIC）> "AE×AE"——角色层级不同不可等价计数；⑤"无重叠"类前提极易击破（R30：16 名 confirmed 互联，TIP×TCSVT 12 人/TIP×InfFusion 4 人，跨刊任职公开合法且常见）；⑥名单不可得的刊对写"未知"绝不写"0 重叠"（R30：PR 四通道全败留白）；⑦主编更替精确日期用**单位官方新闻**补（R30：武大新闻 2026-01-18 证实马佳义 2026-01 起任 InfFusion co-EIC——补全 R20 的 2025 末夹逼窗口） |
| **期刊自引率审计（self-citation）** | Crossref ISSN 抽样框 → 每刊系统抽样 30-40 篇近年论文 → `reference` 字段逐篇统计（总 refs/带 DOI refs/DOI 前缀命中本刊数）→ **双口径+论文级 bootstrap CI+置换检验** → 与官方抑制名单对照定传言真伪 | **口径金规律（R31 首验）**：①**出站口径（references 存款）≠ JCR 入站口径（JIF 分子）不可直接对比**——Clarivate 实际执行自引抑制的案例入站占比 62-100%，社区流传的"15-20% 观察区"无官方成文依据（R31 查证）；②**双口径必报且排序可能翻转**：主口径（self/带 DOI refs）会被无 DOI 占比差的刊系统性抬高（R31：InfFusion 无 DOI 43.6% vs IEEE 刊 15-19%，两口径下 TCSVT/PR 排序互换）——无 DOI 占比必须披露；③自引按论文聚集，点估计要配论文级 bootstrap CI，两刊比较用置换检验且**两口径显著性不一致时如实分列**（R31：TIP>InfFusion 副口径 p=0.0097 显著、主口径 p=0.20 不显著）；④"被抑制/on-hold"传言只能官方源裁决：JCR 抑制名单（现行页+历年 xlsx）、MJL 月度更新、官方博客 on-hold 机制说明——搜索摘要不可定案，且注意同名刊混淆（R31：唯一 "fusion" 命中是 Journal of Fusion Energy；传言源头疑似真实被 on-hold 的 IEEE T-Intelligent Vehicles，标假设不作事实）；⑤抽样框要剔刊前事务页（editorial/board note 无参考文献，R31 排除 9/140 并披露）；⑥前 2 篇先验 reference 字段完整性再全量跑 |

### 13.2 使用方式

接到任务先在本表定位类型 → 按方法链执行 → 每步对照 §13.0 横切原则自查；复合任务（如"领域调研+头衔"）拆成单类型串行/并分片，各自保底。新任务类型跑完后，把踩到的坑按"实例入规"惯例补进本表。
