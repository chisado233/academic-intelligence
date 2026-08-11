# 升级用户测试文档（2026-08-10 Crawler Upgrade）

> 项目：Academic Intelligence（CLI `paper`）
> 文档状态：设计基线 v1.0（2026-08-10，待 kimi-k3-256k 审核）
> 验收方式：派单执行（kimi code `opencode-go/deepseek-v4-flash` 以真实用户身份，独立会话）
> 前置：功能设计 `functional-design.md` / 技术实现 `technical-design.md`

---

## 1. 测试执行规则

1. 每条任务由 ds-flash 独立会话执行，记录：命令 → 实际输出 → 证据位置（输出文件）→ `PASS / FAIL / BLOCKED / NOT-RUN`
2. 主观判定任务（Q2/Q3/Q6/Q7）**双会话交叉复核**：两个独立 ds-flash 会话结果一致才 PASS（D15 自身即双会话一致性机制，不重复列入复核清单，M6）
3. 代码生成者不能成为唯一测试者：实现 Worker 与测试 Worker 分离
4. 测试环境：本项目开发环境（Python 3.11+，`pip install -e ".[dev]"`）
5. 测试数据：真实公开数据（arXiv/Crossref 等免费源），遵循限速与 robots；网络不可稳定触发的场景（如 429）允许 cassette/mock 注入作为合法证据
6. 执行顺序：L1 → L2 → L3(Q) → D；单源阶段 → 全源 → 作者阶段 → 最终全样例
7. 任何 FAIL 先回执行修复，重跑失败项 + 受影响回归；修复后必须新鲜证据
8. **BLOCKED 兜底**：某条因外部源临时不可用 BLOCKED → 重试窗口 24h（3 次）；仍 BLOCKED 升级为 FAIL 走修复流程；连续 3 天无法通过 → 升级人工决策（§5 风险 4）
9. **可选依赖缺席**：curl_cffi/Scrapling/Crawl4AI/Docling 未安装时，对应测试 skip 并计数上报（不静默漏测，不视为 FAIL；安装齐后在最终轮补跑，§5 风险 6）
10. 测试固定 URL 清单（T5 用）：首选 OpenReview 固定论文页（如 `https://openreview.net/forum?id=<固定ID>`）；备选 arXiv abs 页（`https://arxiv.org/abs/2501.12948`）

---

## 2. L1 工具可用性（6 条，required）

| ID | 任务 | 操作 | PASS 判定 |
|---|---|---|---|
| T1 | 全新环境安装 | 新 venv：`pip install -e .` → `paper --version` → `paper --help`；再跑 `ai --version` | `paper` 命令可用；`ai` 存在且打印 "renamed to paper" 并 exit 2（I3 单一判定）|
| T2 | 源注册表 | `paper sources` → `paper sources status` | 列出全部源 + 能力矩阵；status 显示健康/额度 |
| T3 | 单源采集 | `paper source arxiv get 2501.12948 --persist` | 返回 DeepSeek-R1 正确元数据，落库（`paper query` 可查）|
| T4 | 全文管线 | `paper fulltext 2501.12948 --sources arxiv --persist` | PDF 下载成功 + 解析落库：`segments ≥ 20 段` 且 `总字符数 ≥ 10000`（M1 量纲明确），`full_text` 表可查 |
| T5 | 网页爬取 | `paper web crawl <固定URL> --extract paper-schema.json --persist`（固定 URL：OpenReview 固定页面或 CVPR 开放页，附备选，见 §1.8）| 抽取标题/作者/摘要成功，可查询 |
| T5b | 本地 PDF 解析 | `paper pdf parse <样例PDF> --output parsed.jsonl`（用 T4 下载的 PDF）| 输出 jsonl 段落数 ≥20，含页码/文本字段（I10 补验 WP4 交付物）|

---

## 3. L2 能力正确性（4 条，required）

| ID | 任务 | 操作 | PASS 判定 |
|---|---|---|---|
| T6 | 跨源去重融合（跨 ID 场景）| 同一篇论文走三个不同 ID 体系各采一次：`paper source arxiv get 2501.12948 --persist` + `paper source crossref get 10.1038/s41586-025-09422-z --persist` + `paper source openalex get 10.1038/s41586-025-09422-z --persist` | `paper query` 仅 1 条（跨 ID 合并：arXiv ID ↔ DOI 交叉匹配或标题 ≥0.92）；`evidence_list` 含 3 源；复合置信度 = max(各源基线) + 0.05×(n-1) 且 ≤1.0（base=最高源基线；DOI 精确匹配 +0.05 按 scorer 规则，封顶 1.0）|
| T7 | 预算 fail-soft | 配置 OpenAlex 预算为 0（模拟耗尽）→ 采集 | 不崩溃；自动转其他源成功；`paper sources status` 上报额度耗尽 |
| T8 | 作者身份解析 | `paper author resolve <DeepSeek-VL-paper-id> "Haoyu Lu"` | 输出身份档案（机构/方向/h-index 至少机构与方向）；判定 DeepSeek-AI 多模态研究员，附证据链 |
| T9 | 全文合规 | 对一篇无合法 OA 的付费论文跑 `paper fulltext` | 明确报"无合法 OA 全文" + 提示路径；**不绕过、不报假成功** |
| T10-m | 存量库迁移 | 用升级前的 `.db` 副本（如 `academic_intelligence.db.bak`）启动新版 `paper` → `paper stats` / `paper query papers -n 3` | 新表自动创建；旧数据可查；stats 不丢数（I10 迁移高危面）|

---

## 4. L3 论文入口问答（7 条，required，主场景）

| ID | 用户问题 | 命令链 | PASS 判定（权威对照）|
|---|---|---|---|
| Q1 | DeepSeek-R1 发在哪个出版社？ | `paper source crossref get 10.1038/s41586-025-09422-z` → venue → 出版社映射 | 答出 Nature / Springer Nature（DOI 前缀 10.1038 → Springer Nature）|
| Q2 | 这篇论文里的 Haoyu Lu 是谁？ | `paper author resolve <DeepSeek-VL-id> "Haoyu Lu"` | DeepSeek-AI 多模态研究员（OpenAlex/S2 档案对照）★双会话复核 |
| Q3 | DeepSeek-AI 团队的代表作？ | `paper author profile <author-id>` → 引用数排序 | 代表作含 DeepSeek-V3/R1/Math（引用数客观排序）★双会话复核 |
| Q4 | 把这篇论文全文下下来总结 | `paper fulltext <id> --persist` → 读 full_text 段落 | 总结覆盖核心贡献（客观锚点：必须命中 "reinforcement learning" 与 "reasoning" 两个关键词，M4）；全文落库 |
| Q5 | 这篇论文引用了谁/被谁引用？ | `paper citations <id> --sources openalex,opencitations` | 引用/被引列表正确，图谱节点可查询 |
| Q6 | 两篇 DeepSeek 论文里的 Daya Guo 是同一人吗？ | `paper author resolve` ×2 → 消歧比较 | 判同一人（ID 直连 or 特征 ≥0.85）★双会话复核 |
| Q7 | 帮我区分这些同名作者 | `paper author search "Haoyu Lu" --disambiguate` | 候选对比表（机构/方向/合著者）+ ambiguous 标注合理 ★双会话复核 |

---

## 5. D 差异化任务（required 9 条 + recommended 6 条）

### 5.1 数据质量与冲突（required）

| ID | 任务 | 差异化点 | PASS 判定 |
|---|---|---|---|
| D1 | 跨源冲突裁决：arXiv 与 Crossref 同篇论文年份/标题不一致 | 冲突处理 | 融合后取高置信源值；`evidence_list` 保留两源可追溯 |
| D2 | 缺失字段互补：某源无摘要，另一源有 | 字段补全 | 最终记录摘要非空 + 标记来源 |
| D3 | 极端重名："J. Wang" 多领域候选 | 消歧诚实边界 | 输出 `ambiguous` 候选表，**不硬合并** |
| D4 | 错误输入：不存在的 DOI / arXiv ID | 容错 | 清晰报"未找到"，不崩溃，退出码正确 |

### 5.2 失败与反爬恢复（required D5，recommended D6/D7）

| ID | 任务 | 差异化点 | PASS 判定 |
|---|---|---|---|
| D5 | S2 限流 429 → 自动退避/转源 | 限流恢复 | 采集成功（转源或退避），日志有重试记录；允许 cassette/mock 注入 429 作为合法证据路径（live 429 不可稳定触发）|
| D6 | `paper web crawl` 403 页面 | 反爬降级 | 明确报"反爬拦截 + 建议降级级别"（不假成功）；降级级别语义见 technical-design §1.2 |
| D7 | 付费论文 `paper fulltext`（与 T9 同场景，recommended）| 版权边界 | 同 T9：明确拒绝 + 合规提示，不绕过 |

### 5.3 关系与增量（required D8/D10/D11，recommended D9）

| ID | 任务 | 差异化点 | PASS 判定 |
|---|---|---|---|
| D8 | DeepSeek-R1 与 Hinton 某论文间有无引用/合著路径 | 图谱路径 | 给出路径或明确说无，节点/边证据正确 |
| D9 | DeepSeek-AI 核心作者合作网络（Top 合著者）| 作者网络 | 合著关系与真实数据一致 |
| D10 | `paper source arxiv search "deepseek" --limit 20` + 统计 | 批量+统计 | 内部一致性判定（M2）：返回条数 ≤ limit；stats 计数与当次返回/落库一致（live 数据无外部权威对照）|
| D11 | 首采 → 手动改库中一条 → `paper update --author <该论文作者>`（M12：明确用现有 author 路径）| 增量 diff 精度 | 只更新改动那条，其余 untouched |

### 5.4 角色与杂音（recommended）

| ID | 任务 | 差异化点 | PASS 判定 |
|---|---|---|---|
| D12 | 审稿人视角：输出"方法-数据-结论"结构化摘要 | 角色视角 | 三要素齐全、忠实原文 |
| D13 | 圈外人："想了解 Transformer" → 推荐 3 篇入门论文 | 推荐场景 | 推荐合理（引用数/难度）|
| D14 | `paper source arxiv search "深度学习"` | 中文查询 | 返回相关论文或明确中文策略 |
| D15 | 同一查询双 ds-flash 独立会话 | 结果一致性 | 关键事实（作者/年份/出版社）一致 ★双会话复核 |
| D16 | PDF 深度：Docling 抽取表格+参考文献 | 深度解析 | 表格内容正确、参考文献数正确（可选依赖装齐时；不引入 GROBID，M7）|

---

## 6. 测试阶段划分（执行顺序）

```
阶段 A：单源可用性（每个新源独立）
  WP2a-e 各源：T3 对应源版 + 该源 1 条搜索/获取 → 全部 PASS 才进入 B
阶段 B：全源集成
  T2/T6/T7/T9/T10-m + Q1/Q5（跨源）
阶段 C：作者方面（WP6）
  T8 + Q2/Q3/Q6/Q7 + D3/D9/D15
阶段 D：网页与全文深度
  T4/T5/T5b + D6/D16
阶段 E：最终全样例
  全部 required（T1-T5/T5b/T6-T9/T10-m/Q1-Q7/D1-D5/D8/D10/D11）逐条执行 + recommended 尽力
```

## 7. 关闭条件

- 全部 required 样例 PASS（最终新鲜证据）
- 无未关闭 Critical / Important（审核结论）
- 文档与实现一致（README/SKILL/changelog/迁移说明）
- 测试矩阵完整记录（每条：ID/需求/环境/命令/预期/实际/证据/PASS-FAIL）
- BLOCKED 兜底规则未触发人工升级（或已解决）
