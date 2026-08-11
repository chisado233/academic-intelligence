# Academic Intelligence — Agent 测试任务集(Agent Eval Task Suite)

> 目的: 用**真实 agent 派单**验证模块"文档驱动可操作性"。每个任务把
> `SKILL.md` 路径交给一个真实 coding agent(如 kimi 的 opencode-go/deepseek-v4-flash),
> agent 只读用户面文档,必须**通过 academic_intelligence 的 API/CLI** 完成一项真实学术
> 调研,产出结构化结果。主 Agent 用**已知事实**核验 agent 返回的数据是否真实、路径是否走通。
>
> 这不是单元测试,是 dogfood 测试:文档写得好不好、模块能不能真正端到端工作,
> 由陌生 agent 照着文档操作来验证。

## 0. 通用前提与判定准则

- 环境: agent 工作目录 = `D:\agent_workspace\projects\paper-research-crawler`(已安装 `pip install -e .` 或项目可导入)。
- 输入给 agent 的核心资产:
  - `D:\agent_workspace\projects\paper-research-crawler\SKILL.md`(主契约)
  - `D:\agent_workspace\projects\paper-research-crawler\README.md`(备用)
- 硬约束(prompt 内必须写明):
  - **必须通过 `academic_intelligence` 模块的 API 或 CLI 完成任务**,禁止绕过模块直接 curl/爬取数据源页面;
  - 不得编造数据——拿不到就如实报告;
  - 报告必须附**实际执行命令**与关键输出片段(证据),不能只给结论;
  - 尊重源站 rate limit,单任务网络请求控制在合理数量。
- 判定级别:
  - `PASS` — 返回数据与已知事实一致(标题/作者/年份/DOI 等关键字段),且证据显示确实走了模块;
  - `PARTIAL` — 通路走通但数据有缺/有误(如某源限流导致字段缺失),主 Agent 判定可接受;
  - `FAIL` — 通路未走通,或返回与已知事实矛盾,或伪造数据。
- 网络前提: 任务依赖公网 API(OpenAlex / arXiv / Semantic Scholar),若执行时网络不可用,标记 `BLOCKED` 而非失败,可择期重跑。

## 1. 任务清单总览

| ID | 任务 | 核心能力 | 预期关键事实 | 优先级 |
|----|------|----------|--------------|--------|
| T1 | 按 DOI 调研论文 | get_paper / CLI paper | nature14539 = 《Deep learning》(LeCun/Bengio/Hinton, Nature, 2015) | required |
| T2 | 按标题调研论文 | search / get_paper | 《Attention Is All You Need》(Vaswani 等, NeurIPS 2017) | required |
| T3 | 作者调研 | collect_author_papers / CLI author | Geoffrey Hinton 资料(h-index、机构) | required |
| T4 | 多源交叉验证 + 去重 | 多源采集 → 去重/证据链/置信度 | 同一论文多源合并为 1 条,evidence_list ≥2 条 | required |
| T5 | 引用关系 | collect_citations / get_references | 某论文的引用列表非空且包含真实论文 | recommended |
| T6 | 存储 + 查询 | persist → query → stats | 入库后 query 能查回、stats 计数正确 | required |
| T7 | 增量更新 | update_author_papers | 二次 update 返回 unchanged 而非全量重拉 | recommended |
| T8 | 知识图谱 expand | expand / CLI expand(图谱层完成后) | 从论文展开 references,节点数、占位节点标记正确 | recommended |

## 2. 任务定义(可复制派单)

### T1 — 按 DOI 调研论文

- **输入 prompt**(派单时全文复制,替换 `<agent>` 为执行模型):

```
你是一名学术数据调研员。请使用本机 Python 模块 academic_intelligence 完成一次
真实调研任务。

## 必读文档
1. D:\agent_workspace\projects\paper-research-crawler\SKILL.md
2. 如文档不足,读 D:\agent_workspace\projects\paper-research-crawler\README.md

## 任务
按 DOI 调研论文 "10.1038/nature14539",输出:
- 论文标题(英文原题)
- 全部作者(按原顺序)
- 发表年份
- 发表载体(期刊/会议名)
- 摘要(前 200 字即可)
- 引用数(如模块能取到)
- 该论文在哪些数据源中有记录(evidence 来源列表)

## 硬约束
- 必须通过 academic_intelligence 的 API 或 CLI(如 `ai collect paper` /
  AcademicIntelligence.collect_paper)完成,禁止绕过模块直接抓数据源。
- 禁止编造: 任何字段拿不到就写 "N/A" 并说明。
- 报告必须包含: 你实际执行的命令或代码、关键输出片段、最终结果 JSON。
- 工作目录: D:\agent_workspace\projects\paper-research-crawler
- 尊重 rate limit,请求数量控制在合理范围。

## 输出格式
markdown 报告,最后附一个 JSON 块(字段: title, authors, year, venue,
abstract_excerpt, citation_count, sources, commands_used)。
```

- **已知事实(核验用)**: 标题 = "Deep learning";作者 = Yann LeCun, Yoshua Bengio, Geoffrey Hinton;年份 = 2015;载体 = Nature(521, 436–444);DOI = 10.1038/nature14539。
- **判定**: title 命中、含 Hinton 作者、year=2015、venue 含 "Nature"、sources 非空 → PASS;title 或 DOI 不符 → FAIL。

### T2 — 按标题调研论文

- **输入 prompt**: 同 T1 模板,任务改为:

```
## 任务
按标题搜索论文 "Attention Is All You Need",找到该论文(可优先按 arXiv ID
1706.03762 精确获取),输出:
- 标题
- 作者列表(前 6 位)
- 年份
- 载体
- 是否有 arXiv ID
- 引用数(如能取到)
- evidence 来源列表
```

- **已知事实**: 标题 = "Attention Is All You Need";作者含 Ashish Vaswani;年份 = 2017;arXiv ID = 1706.03762;载体 = NeurIPS 2017。
- **判定**: title 命中、含 Vaswani、year=2017 → PASS。

### T3 — 作者调研

- **输入 prompt**: 任务改为:

```
## 任务
调研学者 "Geoffrey Hinton":
1. 获取该作者资料(机构/affiliation、h-index、总引用数,如模块能取到)
2. 获取该作者的代表性论文列表(前 10 篇)
3. 输出: 作者姓名、机构、h-index、论文数、前 3 篇论文标题 + 年份
4. 注明数据来源
```

- **已知事实**: Hinton 长期任职 University of Toronto,与 Google(Google Brain);h-index 极高(80+);代表作含《Deep learning》(2015, Nature)、深度学习相关。**注意**: 各源返回的机构/h-index 可能不同,核验时以"OpenAlex 或 Semantic Scholar 返回的非空值 + 标题含代表作"为准,不要求精确等于某值。
- **判定**: 返回非空作者资料 + 论文列表非空 + 列表中包含或接近《Deep learning》→ PASS;完全空 → FAIL。

### T4 — 多源交叉验证 + 去重

- **输入 prompt**: 任务改为:

```
## 任务
对论文 "10.1038/nature14539" 用至少两个数据源采集(如 openalex + semantic_scholar,
或 openalex + arxiv),验证:
1. 两个源都返回后,最终合并为几条记录?(预期 1 条)
2. 合并后的 evidence 有几条来源?(预期 ≥2)
3. 合并后论文的 confidence 值是多少?
4. 列出每条 evidence 的 source 与 confidence。
```

- **已知事实**: 模块去重后应得 1 条唯一论文,evidence_list 含每个成功源的记录,confidence 按多源加成计算(> 单源值)。
- **判定**: 唯一记录 = 1 且 evidence ≥2 → PASS;>1 条(未合并)→ FAIL。

### T5 — 引用关系

- **输入 prompt**: 任务改为:

```
## 任务
对论文 "10.1038/nature14539":
1. 获取引用该论文的论文列表(被引列表,如模块支持)
2. 获取该论文的参考文献列表(如模块支持)
3. 输出各列表的前 5 项(标题 + 年份),注明来源与失败情况
```

- **已知事实**: 《Deep learning》(2015)被引数很高(数万),OpenAlex 支持被引列表;arXiv 无被引数据。
- **判定**: 至少一个列表非空且条目真实(标题/年份合理)→ PASS;两列表都空但错误信息完整 → PARTIAL。

### T6 — 存储 + 查询

- **输入 prompt**: 任务改为:

```
## 任务
1. 用模块采集论文 "10.1038/nature14539" 并持久化到临时 SQLite(建议用临时目录,
   不要污染项目根)
2. 用 query 接口按标题关键词或年份查询,确认能查回该论文
3. 用 stats 接口输出存储统计
4. 报告: 存储路径、查询结果条数、stats 内容、查询用的代码/命令
```

- **判定**: 查询回该论文 + stats 计数与入库数一致 → PASS。
- 注意: 提示 agent 用 `tempfile` 或 tmp 路径,避免在项目根留 db 文件。

### T7 — 增量更新

- **输入 prompt**: 任务改为:

```
## 任务
1. 采集学者 "Geoffrey Hinton" 的论文并持久化(如数据量大,限制到 openalex 单源
   且 limit 合理)
2. 立即再次调用增量更新接口(update_author_papers 或 CLI update)
3. 报告: 第一次入库条数、第二次的 new/updated/unchanged 统计
4. 说明: 第二次预期大多为 unchanged(数据未变化),如果全是 new 说明增量失效
```

- **判定**: 第二次 unchanged 数量 > 0 且 new 数量为 0 或极小 → PASS;第二次仍大量 new → FAIL(增量失效)。
- 网络波动风险: 若两次采集本身结果不一致(源端变化),按 PARTIAL 处理。

### T8 — 知识图谱 expand(图谱层完成后启用)

- **输入 prompt**: 任务改为:

```
## 任务
1. 获取论文 "10.1038/nature14539" 或 "1706.03762" 入本地库
2. 调用图谱展开接口(expand / CLI expand),按 references 展开一层
3. 输出: 展开得到的节点数、边数、占位节点(loaded=False)数量、截断情况
4. 说明: 引用节点中哪些是"占位节点"(只有 ID/标题,未拉全文)
```

- **已知事实**: 一层的 references 节点数应 ≥1;未预先拉全的引用论文应为占位节点(loaded=False)。
- **判定**: expand 返回节点数 > 0 且占位节点标记符合预期 → PASS。

## 3. 执行方式

- 派单类型: 原生CLI单(kimi / cmdc 均可),一条任务 = 一个独立 run,档案放
  `D:\agent_workspace\tmp\agent-dispatch\20260807-0840-ai-paper-complete\eval\<task-id>\`。
- 模型: 主力 `opencode-go/deepseek-v4-flash`;可用 `cmdc -m meta/muse-spark-1.2-contributor`
  跑交叉验证(不同模型家族复核同一任务,核对数据一致)。
- 至少执行: T1 T2 T3 T4 T6(required);T5 T7 T8 视模块完成度与网络条件执行。
- 每个任务完成后,主 Agent 按 §0 判定准则给 PASS/PARTIAL/FAIL 并记录证据。

## 4. 验收门槛

- 全部 executed 的 required 任务中,`PASS ≥ 80%`,无 `FAIL`(PARTIAL 可接受需注明原因)。
- 至少 1 个任务由两种不同模型(如 ds-flash 与 muse-spark)执行且结果一致。
