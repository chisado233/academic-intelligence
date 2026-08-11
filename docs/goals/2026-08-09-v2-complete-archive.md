# Goal: Academic Intelligence 推进至完成(3A v2 对齐)

- 状态: completed（2026-08-09 Codex 第 5 轮全面审查后重新验收；见 progress.md）
- 创建: 2026-08-07
- 主 Agent: kimi-code(本会话)
- 执行模型: opencode-go/deepseek-v4-flash(原生CLI单,ds-flash 执行/审核/测试;muse-spark-1.2-contributor 线路当日多次故障,B6 复核以 ds-flash 独立会话完成)

## objective

把 `projects/paper-research-crawler`(Academic Intelligence)从当前 v1 形态推进到
3A 技术设计 v2(2026-07-26-technical-design-v2.md)+ 3B 执行计划
(2026-07-26-execution-plan-3B.md)定义的完成状态,并完成全量测试与文档对齐。

## inScope

- core/models.py 升级到 v2 模型(AuthorRef、evidence_list、arxiv_id/pmid、关系字段、Author 多源 ID、disambiguation_status)
- Config 重写(图谱/消歧/增量/置信度参数)
- 存储层关系查询(引用边/合著边/证据关联)
- 去重升级(ID 交叉匹配、SequenceMatcher)+ 置信度评分
- 图谱层 graph/(KnowledgeGraph、traversal、cache)+ expand()/subgraph()/path() API
- CLI: expand / export / update / author-papers
- 作者消歧(disambiguator.py:ID 直连 + 启发式聚类)
- arxiv/pubmed/ieee 适配器接线进主入口
- 错误处理与降级(WP-4.3)
- 集成测试、覆盖率 ≥85%、全量回归
- 文档对齐(progress.md、README、SKILL.md、docs/)
- 抓取层:FetchGateway 抽象落地(httpx 默认实现;Scrapling 未安装,做成可选、import 检测)——v2 设计偏差,记录于 decisions

## outOfScope

- 安装 Scrapling 重型浏览器依赖链并全面替换抓取层
- PyPI 发布 / GitHub push / 部署
- 用户确认接口(3A §6.2 第三层,Phase 1 不实现)
- 参考/PaperExtraction 相关

## prohibitions

- Worker 禁止 git 操作、派发下级 Agent、修改共享文档与 Goal
- 禁止让测试适应实现或静默放宽冻结样例
- 不删除现有可用功能(演进式升级,不推倒重来)

## acceptanceCases

- AC-1 (required): 模型 v2 —— Paper.authors 为 List[AuthorRef],evidence_list 多源,含 arxiv_id/pmid/fields_of_study/reference_count/references/citations;Author 含 orcid/semantic_scholar_id/openalex_id/disambiguation_status;现有测试同步更新
- AC-2 (required): 去重升级 —— 同一论文 3 源(不同 ID 格式)正确合并为 1 条,evidence_list 含 3 条,置信度正确(base + 0.05×(n-1))
- AC-3 (required): 图谱层 —— KnowledgeGraph 构建/邻居查询/子图导出;expand() 先查 SQLite miss 拉源;占位节点 loaded=False;depth=3/max_nodes=50 截断生效
- AC-4 (required): 消歧 —— ORCID 相同自动合并;同名不同机构/方向不合并(<0.6);同名同机构合并(≥0.85);边界标 ambiguous
- AC-5 (required): 增量 —— 首次同步写 sync_state;hash 相同跳过;hash 变化触发更新
- AC-6 (required): CLI —— `ai expand <id> --relations ... --depth 2`、`ai export --center ... --radius 2`、`ai update --author ...` 可用
- AC-7 (required): 降级 —— mock 单源失败不阻塞整体;全源失败抛 SourceUnavailableError 附每源原因
- AC-8 (required): 集成 —— `ai paper "10.1038/nature14539"` 返回完整论文(标题/作者/年份/摘要/引用数);`ai author "Geoffrey Hinton"` 返回学者资料
- AC-9 (required): 覆盖率 ≥85%,全量测试通过,无 Critical/Important 遗留
- AC-10 (required): 文档与实现一致(progress.md/README/SKILL.md/docs)
- AC-11 (required): agent 测试任务集(docs/agent-eval/task-suite.md)真实派单执行——required 任务 T1/T2/T3/T4/T6 全部执行,PASS ≥80%、无 FAIL;至少 1 个任务双模型(ds-flash + muse-spark)交叉复核结果一致

## batches

- B1: 模型 v2 + Config + 兼容层(AC-1 前置)
- B2: 存储关系边 + 去重/评分升级(AC-2)
- B3: 图谱层 + expand API/CLI(AC-3, AC-6 部分)
- B4: 消歧 + 适配器接线 + 增量 + 降级(AC-4, AC-5, AC-7)
- B5: 集成测试 + 覆盖率 + 文档对齐(AC-6~AC-10)
- B6: agent 任务集执行(AC-11): T1/T2/T3/T4/T6 全部执行 PASS 5/5;T1 由两个独立会话(ds-flash)复核结果一致;muse-spark 线路持续故障(4 次失败)改由 ds-flash 独立会话复核,GLM-5.2/claude-sonnet-5 因订阅限制不可用(用户指示 ds 自审)

## closeConditions

- 全部 required AC 逐条 PASS(新鲜证据)
- 覆盖率 ≥85%
- 文档与实现一致
- 无未关闭 Critical/Important

## 2026-08-09 freshRevalidation

- 37 条新增 hardening 回归覆盖安全、取消语义、存储一致性、作者消歧、graph snapshot、CLI/导出与安装元数据。
- 最终全量测试 **854 passed**，覆盖率 **92%**，CLI 覆盖率 **82%**。
- Ruff、mypy（42 source files）、`git diff --check`、MkDocs strict 全部通过。
- wheel 在不继承系统包的全新虚拟环境安装并通过 `pip check` 与真实 CLI/API dogfood；安装路径确认来自 `site-packages`。
- 全面审查确认的 Critical/Important 项已关闭；JSON 跨进程写限制已显式文档化，属于设计边界而非未修缺陷。
