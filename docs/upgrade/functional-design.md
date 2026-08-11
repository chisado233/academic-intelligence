# 升级功能设计文档（2026-08-10 Crawler Upgrade）

> 项目：Academic Intelligence（`projects/paper-research-crawler`，CLI 从 `ai` 更名为 `paper`）
> 文档状态：设计基线 v1.0（2026-08-10，待 kimi-k3-256k 审核）
> 定位：本文件定义"做什么"，技术实现见 `technical-design.md`，验收见 `user-test-plan.md`

---

## 1. 背景与目标

### 1.1 现状

- 六源 API 适配器（arXiv/OpenAlex/Semantic Scholar/PubMed/IEEE/Google Scholar-SerpAPI）
- 元数据采集 + 去重融合 + 作者消歧 + 知识图谱 + SQLite 存储
- 已知痛点：无全文能力、外部 API 付费/限流脆弱（OpenAlex 2026-02 转按量计费、S2 429）、无网页爬取能力、CLI 名为 `ai` 语义不明

### 1.2 升级目标

1. **免费优先的信息源策略**：以免费 API 为主力，网页爬虫兜底，OpenAlex 降级为"预算受限源 + 免费快照"，不依赖 Google Scholar/SerpAPI
2. **元数据 + 全文双深度采集**：新增 PDF 全文管线（合法 OA 定位 → 下载 → 解析 → 段落入库）
3. **网页爬取能力**：覆盖无 API 的公开网页源（会议主页/作者主页/机构仓库/出版社公开页）
4. **CLI 重构**：`ai` → `paper`，新增 `paper source <源> <操作>` 子命令树
5. **作者身份解析**：给定论文 + 作者名 → 解析作者身份（机构/方向/h-index/主页），支持人工确认写回
6. **预算管理**：每源配额 + 超额 fail-soft + 上报，不被单一源额度拖垮整体

### 1.3 非目标（明确不做）

- 重写去重/消歧算法（复用现有管道，仅标定新源置信度基线）
- Google Scholar 落地（ToS 红线，仅保留评估记录）
- 付费墙正文获取（版权红线，任何用途不做）
- Sci-Hub/libgen 类集成（红线）
- PyPI 发布 / GitHub push

### 1.4 使用定位

个人学习用途工具。默认"礼貌爬取"模式：限速、标识 UA（`paper-research-crawler/0.2 (learning use)`）、尊重 robots.txt、低频。

---

## 2. 信息源矩阵（新增 + 保留）

### 2.1 保留源（改造）

| 源 | 类型 | 操作 | 备注 |
|---|---|---|---|
| arXiv | API | search/get/fulltext | 已有；全文 PDF 合法来源之一 |
| OpenAlex | API | search/get/citations | **降级**：免费 key 每日 $1 额度；本期策略 = 限量实时 + fail-soft；免费快照（本地索引）移入 **Phase 2**（本期不实现导入管线，仅预留 meta 表） |
| Semantic Scholar (ss) | API | search/get/citations | 已有；申请免费 key 降低 429 |
| PubMed | API | search/get | 已有 |
| IEEE | API | search/get | 已有；key 仍付费，作为低优先级源 |
| Google Scholar (gs) | 网页/商业 | — | **停用适配器调用**，仅保留替代源评估记录 |

### 2.2 新增 API 源（全部免费）

| 源 | 类型 | 操作 | 内容 | 限流 |
|---|---|---|---|---|
| Crossref | API | get（DOI 权威）/search | 150M+ DOI 元数据（标题/作者/年份/期刊/引用） | polite pool（mailto）：3-10 req/s |
| Unpaywall | API | get（DOI → 合法 OA 全文链接） | 全文定位器（无元数据） | 100k/天（需邮箱） |
| Europe PMC | API | search/get/fulltext | 生物医学元数据 + 合法 OA 全文 | 免费 |
| OpenCitations (COCI) | API | citations（以 **DOI** 为键，内部分 citing/cited 两方向参数化；不接受 OpenAlex W-id，M8/I4）| 纯引用关系 | 免费无鉴权 |
| CORE | API | search/get/fulltext | 4.5 亿+ 元数据 + 700 万+ 合法全文 | 免费 key |

### 2.3 新增网页源（爬虫驱动）

| 源 | 类型 | 操作 | 内容 |
|---|---|---|---|
| 通用网页 (web) | 网页 | crawl（URL → schema 抽取） | 会议主页（CVPR/NeurIPS/OpenReview）、作者主页、机构仓库、出版社公开页 |
| PDF 管线 | 本地 | fulltext/parse | 合法 OA PDF 下载 → 文本解析 → 段落入库 |

### 2.4 各源"爬什么"（内容矩阵）

统一产出模型：`Paper`（元数据）+ `FullText`（全文）。

| 源 | 标题/作者/摘要 | 机构 | 引用数 | 引用关系 | 全文 | OA 链接 |
|---|---|---|---|---|---|---|
| arXiv | ✅ | ❌ | ❌ | ❌ | ✅ 预印本 PDF | ✅ |
| Crossref | ✅ | ✅(部分) | ✅ | ✅ | ❌ | ✅ |
| Unpaywall | ❌ | ❌ | ❌ | ❌ | ✅ 定位链接 | ✅ |
| Europe PMC | ✅ | ✅ | ✅ | ✅ | ✅ OA 全文 | ✅ |
| OpenCitations | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| CORE | ✅ | ✅(部分) | ✅ | ❌ | ✅ OA 全文 | ✅ |
| OpenAlex | ✅ | ✅ | ✅ | ✅ | ⚠️ 计费 | ✅ |
| web crawl | ✅(抽取) | 视页面 | ❌ | ❌ | 页面 PDF 链接 | 视页面 |

---

## 3. 功能模块划分

### 3.1 模块清单

| 模块 | 职责 | 关键接口 |
|---|---|---|
| `sources/` 扩展 | 新增 5 个 API 适配器，全部走 `BaseSource` 契约 | `search/get/citations/fulltext` 能力声明 |
| `webcrawler/`（新增） | 网页爬取：curl_cffi 默认 fetcher + Scrapling 可选 + 抽取器（规则/LLM 可选） | `crawl(url, schema) → WebDocument` |
| `fulltext/`（新增） | 全文管线：定位（Unpaywall/CORE/arXiv）→ 下载 → 解析（PyMuPDF 默认）→ 段落入库 | `fetch(paper) → FullText` |
| `budget/`（新增） | 预算/配额管理：每源配额、剩余量、fail-soft 决策、上报 | `budget.check(source) / consume(source, cost)` |
| `identity/`（新增） | 作者身份解析：resolve（论文内定位 → ID 直连/候选消歧）→ profile → confirm 写回 | `resolve(paper_id, name) → AuthorIdentity` |
| `cli/` 重构 | `paper` 命令树 + `source` 子包 | 见 §4 |
| `storage/` 扩展 | 新表：full_text / budget_usage / author_identity / crawl_cache | 见 technical-design |

### 3.2 采集流（多源聚合与单源并行）

```
单源：paper source <源> <操作> <query> → 适配器 → Paper/FullText → 去重融合 → 存储
聚合：paper collect <id> --sources all → 多适配器并行 → 融合（已有逻辑）→ 存储
全文：paper fulltext <id> → 定位器(Unpaywall→CORE→arXiv) → 第一个合法命中 → 下载 → 解析 → 入库
```

---

## 4. CLI 命令规范

### 4.1 命名与层级

```
paper [OPTIONS] COMMAND [ARGS]
├── source <源> <操作> <query>     # 单源操作（源子包）
│    源：arxiv / openalex / ss / pubmed / ieee / crossref / unpaywall /
│         europe-pmc / opencitations / core / web
│    操作：search / get / citations / fulltext（各源按能力声明支持哪些）
├── collect paper|author|citations   # 顶层多源聚合（保留，兼容旧用法）
├── citations <id>                   # 顶层别名 = collect citations（Q5 用）
├── fulltext <id>                    # 跨源全文定位+下载
├── pdf parse <file>                 # 本地 PDF 解析
├── author resolve|profile|search|confirm   # 作者身份解析（WP6）
├── web crawl <url>                  # 网页爬取（= source web crawl）
├── sources / sources status         # 源注册表 / 健康额度
├── budget                           # 预算总览
├── query / stats / export / export-papers / update   # 保留现有
└── --version / --help
```

### 4.2 通用选项

- `--persist`：写入数据库（保持旧语义，必须显式）
- `--output/-o`：JSON 输出
- `--sources`：多源聚合时选源
- `--fulltext`：get 时同时走全文管线
- `--limit/-n`：search 条数
- `--key`：源专用 key（ieee 等）
- 迁移：`ai` 保留为 shim 一个版本周期（调用打印 "renamed to paper" 并 exit 2）；正式入口 `paper`（见 technical-design §8）

### 4.3 命令示例（验收基线，对应 user-test-plan）

```bash
paper source arxiv get 2501.12948 --persist
paper source crossref get 10.1038/s41586-025-09422-z --persist
paper source unpaywall get 10.1038/s41586-025-09422-z --fulltext --persist
paper source europe-pmc search "deepseek" --persist
paper source opencitations citing 10.1038/s41586-025-09422-z --persist   # COCI 以 DOI 为键
paper source core search "graph neural network" --persist
paper web crawl <url> --extract paper-schema.json --persist
paper fulltext 2501.12948 --sources unpaywall,core,arxiv --persist
paper author resolve 2403.05525 "Haoyu Lu"
paper author profile <author-id>
paper sources status
```

---

## 5. 用户场景（论文入口问答，验收主场景）

| 场景 | 用户问题 | 命令链 | 权威答案对照 |
|---|---|---|---|
| Q1 出版社 | DeepSeek-R1 发在哪个出版社？ | get → venue → 出版社映射（DOI 前缀→出版社） | Nature → Springer Nature |
| Q2 作者身份 | 这篇论文里的 Haoyu Lu 是谁？ | author resolve | DeepSeek-AI 多模态研究员（OpenAlex/S2 档案对照）|
| Q3 代表作 | DeepSeek-AI 团队代表作？ | author profile + 引用数排序 | V3/R1/Math 等 |
| Q4 全文总结 | 把这篇论文全文下下来总结 | fulltext + 段落读取 | 核心贡献正确 |
| Q5 引用 | 这篇论文引用了谁/被谁引用？ | citations | 引用列表正确 |
| Q6 同一人 | 两篇论文里的 Daya Guo 是同一人吗？ | author resolve ×2 比较 | ID 直连或特征≥0.85 → 同一人 |
| Q7 重名辨析 | 帮我区分这些同名作者 | author search --disambiguate | 候选对比表 + ambiguous 标注 |

---

## 6. 合规边界（能做/不能做）

### 6.1 能做

- 免费 API 全量使用（Crossref/arXiv/S2/Europe PMC/OpenCitations/Unpaywall/CORE）
- 下载合法 OA 全文（arXiv 预印本、PMC、Unpaywall/CORE 定位的 OA 副本）
- 下载作者自存档（作者主页/机构仓库/ResearchGate 上作者本人发布的 PDF）
- 爬公开网页元数据（会议主页/作者主页/机构仓库/出版社公开目录页）
- OpenAlex 免费快照本地建索引
- 本地解析已拥有的 PDF（段落抽取、建索引、全文检索）

### 6.2 不能做（红线，任何用途）

- 绕过付费墙获取正文（Elsevier/ScienceDirect 付费论文等）
- Sci-Hub/libgen 类工具集成
- 自动化查询 Google Scholar（ToS 禁止）
- 破解验证码/Cloudflare 类对抗性规避（含 Scrapling StealthyFetcher 自动过盾；详见 technical-design §1.2 反检测边界——仅限"允许抓取的公开页需 JS 渲染"场景，遇挑战页/403 一律 blocked 停止）
- 无视 robots.txt 爬取
- 大规模并行抓取打爆对方服务器

### 6.3 默认礼貌模式（学习用途）

- 全局限速（默认 1 req/s，每源可配）
- UA 标识 `paper-research-crawler/0.2 (learning use)`
- 尊重 robots.txt（预检，拒绝则 fail-soft）
- 低并发（默认 4 连接）
- 缓存优先（同一 URL 短时间不重抓）

### 6.4 作者自存档边界（明确可判定来源）

全文下载来源限"**作者主页 / 机构仓库**"两类可判定来源（页面域名/路径可核验）；
ResearchGate/Academia.edu 等社交平台的"作者本人发布"无法机器判定，**不作为自动下载来源**（人工确认后除外）。

---

## 7. 验收概览

验收方式：派单（kimi code `opencode-go/deepseek-v4-flash`）以真实用户身份执行任务集。
任务集分四组（详见 `user-test-plan.md`）：

- **L1 工具可用性**（T1-T5）：安装/源注册表/单源采集/全文/网页爬取
- **L2 能力正确性**（T6-T9）：跨源去重/预算 fail-soft/作者解析/全文合规
- **L3 论文问答**（Q1-Q7）：主场景
- **D 差异化**（D1-D15）：数据质量/失败恢复/关系网络/批量增量/用户角色/中文/交叉复核

关闭条件：全部 required 样例 PASS（新鲜证据）、无未关闭 Critical/Important、文档与实现一致。
