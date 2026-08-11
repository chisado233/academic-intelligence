# Goal: Crawler Upgrade 2026-08（爬虫升级：多源采集 + 全文 + CLI 重构）

- 状态: **completed**（2026-08-11 最终全样例验收 26/26 PASS + 双会话复核 3/3 PASS 后关闭）
- 创建: 2026-08-10
- 主 Agent: kimi-code(本会话)
- 执行模型: opencode-go/deepseek-v4-flash（原生CLI单，实现/审核/测试）
- 文档审核模型: kimi-code/k3-256k

## objective

把 Academic Intelligence（CLI 更名 `paper`）升级为"免费优先的多源采集 + 元数据/全文双深度 + 网页爬取 + 作者身份解析"工具（详见 2026-08-09-v2-complete-archive.md 之前的演进）。

## 验收记录（2026-08-11 最终）

- AC-1 (required) L1 可用性 T1-T5b：**6/6 PASS**
- AC-2 (required) L2 正确性 T6-T9/T10-m：**5/5 PASS**（T6/T8 带注：跨 ID 融合在多源 collect 入口、OpenAlex 聚合噪声备注）
- AC-3 (required) L3 论文问答 Q1-Q7：**7/7 PASS**（Q2/Q6/Q7 双会话复核 3/3 关键事实一致）
- AC-4 (required) D 差异化 required：**8/8 PASS**
- AC-5 (required) 全量测试：**1177 passed**、覆盖率 90%（≥85 门槛）、ruff/mypy/mkdocs 通过
- AC-6 (required) 文档一致：README/changelog/user-guide/SKILL 已对齐（`ai`→`paper` + 新命令）；upgrade 三文档经 k3 审核
- AC-7 (required) k3 文档审核：28 条问题（2C/10I/16M）全部修订关闭后进入实现

## batches（全部完成）

- B0: 三文档 + Goal → k3 审核 → 修订 ✅
- B1: WP1 CLI 骨架（ai→paper + source 子命令树）✅
- B2: WP2a-e 五源 + WP3 网页爬虫 + WP4 全文管线 + WP5 预算（并行 9 单 + ds 审核 + 修复单 A/B）✅
- B3: 单源功能测试 7 单（4 全 PASS；E4/C4/E1 缺陷修复后重验）✅
- B4: WP6 作者身份解析 ✅
- B5: 最终全样例用户测试（26/26）+ 双会话复核（3/3）+ 文档对齐 ✅

## closeConditions（2026-08-11 核对）

- [x] 全部 required 验收样例 PASS（新鲜证据，最终轮）
- [x] 无未关闭 Critical / Important（k3 + ds 两轮审核关闭）
- [x] 文档与实现一致（changelog 含 Breaking Change + 迁移说明）
- [x] 红线全程未触碰（付费墙/盗版/GS/过盾；合规专项 PASS）

## 已知限制（不阻塞关闭，见 progress.md）

- Unpaywall 需 `UNPAYWALL_EMAIL`、CORE 建议免费 key（公共 tier 限流）
- OpenAlex 同名聚合噪声属外部源数据质量
- `docs/api.md` 等历史文档的 `ai` 引用待后续对齐
- OpenAlex 免费快照本地索引为 Phase 2（本期仅限量实时 + fail-soft）
