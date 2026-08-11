# 信息反向挖掘（引用者画像）实现计划

> **For agentic workers:** 本项目使用 agent-dispatch 原生CLI单派单模式；Worker 禁止 git（commit 由主 Agent 统一处理），按 TDD 实现（先写测试→跑失败→实现→跑过）。
> **Goal:** 实现 3 个挖掘原语（trace-citing / trace-authors / trace-profiles）+ SKILL 方法论，支持"论文→引用者→画像"反向挖掘。
> **Architecture:** CLI 固有工具（确定性批量操作）＋ SKILL 方法论（agent 执行手册）；分析产物 CSV 不落主库。
> **Tech Stack:** 复用项目现有 httpx/SQLAlchemy/typer/pydantic；OpenAlex `filter=cites:` API；OpenCitations COCI。

## Global Constraints（继承 spec，逐条执行）

- 消歧不做进 CLI（不自动合并同名；只给原始数据行）
- 头衔核验走 agent webfetch（本项目 CLI 不实现）
- 分析产物 = CSV/JSON 文件，不落主库（可选 `--cache` 拉取缓存）
- 合规红线不变：不碰付费墙、不爬 GS、不过盾、限速礼貌（1 rps）、凭证从 env 读
- 错误契约：单源失败 fail-soft；全失败 exit 2；429 退避 + 断点续传
- 作者姓名保留原始（拼音/中文不转换）；机构为原始署名机构字符串

---

### Task 1: `trace-citing` 原语（反向引用拉取）

**Files:**
- Create: `academic_intelligence/trace/citing.py`（核心逻辑，可测）
- Create: `tests/test_trace_citing.py`
- （CLI 注册在 Task 4 统一做，本任务只做库层）

**Interfaces:**
- 入口：`fetch_citing_papers(paper_id: str, *, sources: list[str] | None = None, limit: int | None = None, resume_from: str | None = None, http: HTTPClient | None = None) -> CitingResult`
- `CitingResult`: `papers: list[CitingPaper]` / `resume_cursor: str | None` / `source_stats: dict[str, int]` / `errors: list[SourceFailure]`
- `CitingPaper`: `citing_paper_id / doi / title / year / venue / authors_raw(list[str])`

**Steps:**
- [ ] 1. 写失败测试：mock OpenAlex 响应（`filter=cites:Wxxx` 分页两页）→ 断言 papers 合并、cursor 正确、`source_stats` 计数
- [ ] 2. 跑测试确认失败（模块不存在）
- [ ] 3. 实现 `academic_intelligence/trace/citing.py`：
  - OpenAlex：`GET https://api.openalex.org/works?filter=cites:{openalex_id}&per-page=200&cursor={cursor}`（**cursor 首页必须 `*`**）；需先把 paper_id 归一化为 OpenAlex W-id（调 openalex 适配器 get 或 `filter=doi:`/`filter=ids.arxiv:`）
  - OpenCitations：`GET https://opencitations.net/index/coci/api/v1/references/{doi}`（以 DOI 为键；双源交叉取并集，去重按 citing_paper_id）
  - 429 退避（复用 utils/HTTPClient retry）、断点续传（`resume_from` 为 OpenAlex cursor）、`--limit` 安全阈值
  - 输出 CSV 由 Task 4 的 CLI 层负责；本任务返回结构化对象
- [ ] 4. 跑测试确认通过
- [ ] 5. 补测试：OpenCitations 路径、双源去重、单源失败 fail-soft、限流重试

### Task 2: `trace-authors` 原语（作者展平）

**Files:**
- Create: `academic_intelligence/trace/authors.py`
- Create: `tests/test_trace_authors.py`

**Interfaces:**
- 入口：`flatten_authors(citing_papers: list[CitingPaper], *, affiliation_filter: str | None = None) -> list[AuthorRow]`
- `AuthorRow`: `author_name / appears_in(list[str] 论文id) / affiliation(str|None) / author_id(str|None)`
- 输入 `CitingPaper.authors_raw` 是原始作者字符串列表；本任务需要从源记录补机构/ID——设计：`CitingPaper` 增加可选 `authors_detail: list[dict]`（含 affiliation/author_id，OpenAlex authorships 结构），由 Task 1 填充（OpenAlex 响应自带）

**Steps:**
- [ ] 1. 写失败测试：给定含 `authors_detail` 的 CitingPaper 列表 → 断言展平行数 = Σ作者数、`appears_in` 聚合正确、`affiliation_filter` 过滤生效
- [ ] 2. 跑测试确认失败
- [ ] 3. 实现 `flatten_authors`：遍历 papers → 每作者一行（name/appears_in 聚合/affiliation 取该论文署名机构/author_id 如有）；`affiliation_filter` 为子串匹配（机械过滤，非消歧判断）；**不做任何同名合并**
- [ ] 4. 跑测试确认通过
- [ ] 5. 补测试：空输入、无 affiliation 的行、filter 无匹配

### Task 3: `trace-profiles` 原语（批量画像）

**Files:**
- Create: `academic_intelligence/trace/profiles.py`
- Create: `tests/test_trace_profiles.py`

**Interfaces:**
- 入口：`fetch_profiles(author_rows: list[AuthorRow], *, batch_size: int = 20, http: HTTPClient | None = None) -> list[AuthorProfile]`
- `AuthorProfile`: `author_name / author_id / institution / h_index / fields(list[str]) / works_count / top_works(list[dict]: title/venue/year/cited_by_count)`
- 有 `author_id` 的行直接拉 OpenAlex `authors/{id}`；无 ID 的行返回 `author_id=None` + 空字段（**不自动搜索匹配**——留给 agent 方法论）

**Steps:**
- [ ] 1. 写失败测试：mock OpenAlex 作者端点 + works（`filter=author.id:&sort=cited_by_count:desc&per-page=5`）→ 断言 profile 字段、top_works 排序、限速分批
- [ ] 2. 跑测试确认失败
- [ ] 3. 实现 `fetch_profiles`：有 ID 拉档案（institution/h_index/fields/works_count）+ top_works（按引用排序取 5）；无 ID 返回占位（author_id=None）；分批限速（1 rps）；单作者失败不阻塞批量（记 errors）
- [ ] 4. 跑测试确认通过
- [ ] 5. 补测试：无 ID 行、单作者失败容错、批处理进度

### Task 4: CLI 注册（3 命令 + CSV 输出 + 集成测试）

**Files:**
- Modify: `academic_intelligence/cli.py`（或新增 `cli_trace.py` 注册进命令树）
- Create: `tests/test_cli_trace.py`

**Interfaces:**
- 消费 Task 1-3 的库层函数
- 命令（对齐 spec §4）：
  - `paper trace-citing <paper-id|DOI> [--sources openalex,opencitations] [--limit N] [--resume-from CURSOR] [--output citing.csv]` → 写 CSV（列：citing_paper_id/doi/title/year/venue/authors_raw）
  - `paper trace-authors <citing.csv|paper-id> [--affiliation-filter KW] [--output authors.csv]` → 写 CSV（列：author_name/appears_in/affiliation/author_id）
  - `paper trace-profiles <authors.csv> [--batch-size N] [--output profiles.csv]` → 写 CSV（列：author_name/author_id/institution/h_index/fields/works_count/top_works）

**Steps:**
- [ ] 1. 写失败测试：CLI 命令存在性 + CSV 输出内容正确（用 mock 数据）
- [ ] 2. 跑测试确认失败
- [ ] 3. 实现：CSV 读写（UTF-8 + BOM 兼容 Excel，参照 export-papers 的 excel-safe）、命令挂载、错误映射（exit 2 全失败）
- [ ] 4. 跑测试确认通过
- [ ] 5. 补集成测试：三命令链式（mock）端到端

### Task 5: 方法论文档（SKILL.md 章节 + API 直连手册）

**Files:**
- Modify: `SKILL.md`（新增"信息反向挖掘"章节：6 步工作流 + 消歧方法论 + 头衔核验衔接）
- Create: `docs/api-direct.md`（官方 API 直连手册）

**Steps:**
- [ ] 1. 写 SKILL.md §"信息反向挖掘"：工作流（定位种子→trace-citing→trace-authors→trace-profiles→消歧方法论→头衔核验→输出画像）、消歧规则（ID 直连优先/机构+领域+代表作特征对比/不硬合并/标注置信度）、信息获取边界表（CLI 提供 vs agent 联网搜索）、衔接 titles-source-map
- [ ] 2. 写 `docs/api-direct.md`：OpenAlex（works/authors 端点、cites: 过滤、cursor 分页从 `*` 起、author.id 过滤、机构过滤 raw_affiliation_strings.search、限速礼貌池）、OpenCitations（citations/references，DOI 键）、Crossref（polite pool）、S2（429 说明）——每条带合规注释；**实测坑**：cursor=*、urllib 超时、GBK 编码、同名需机构+ID 双校验
- [ ] 3. 自检：与 spec §3 信息边界一致；无历史命令残留

### Task 6: 真实场景验收（dogfood，ds-flash 派单执行）

**Files:**
- Create: `tmp/agent-dispatch/<run>/result.md`（验收报告，派单时主 Agent 建档）

**Steps:**
- [ ] 1. 主 Agent 派 ds-flash 验收单：种子论文（陆峰/北航 的论文或用户指定）→ 执行 trace-citing → trace-authors（含机构过滤）→ trace-profiles → agent 方法论消歧 + 头衔核验（webfetch 官方源）→ 输出画像 CSV
- [ ] 2. 验收点：反向引用数量与 OpenAlex 一致；作者行含机构过滤能力；profiles 为消歧提供足够特征；画像 CSV 含头衔标注与来源链
- [ ] 3. 全量离线回归（既有 1177+ 测试不回归）

---

## Self-Review 记录

- Spec 覆盖：§4 三原语 → Task 1/2/3；§5 方法论 → Task 5；§9 测试 → Task 6；CLI → Task 4 ✅
- 占位符：无 TBD/TODO ✅
- 类型一致：CitingPaper/AuthorRow/AuthorProfile 三接口在 Task 1-3 定义、Task 4 消费，签名一致 ✅
- 并行性：Task 1/2/3/5 互不依赖可并行；Task 4 依赖 1-3；Task 6 依赖全部
