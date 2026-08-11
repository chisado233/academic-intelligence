# arXiv 精确路由、重试可观测性与 Skill 收口设计

## 背景

2026-08-10 的 Kimi + `opencode-go/deepseek-v4-flash` 50 项黑盒评测得到 31 PASS / 6 PARTIAL / 3 FAIL / 10 BLOCKED。28 个离线任务全部通过，三个确定性 FAIL 均来自裸 arXiv ID 查询；联网任务还暴露了实际 HTTP 重试次数没有进入 `SourceFailure`，以及 Agent 在来源持续 HTTP 429 时进行无界外层重试的问题。

本轮只修复这些已由评测和离线最小复现确认的边界缺陷，不重构来源、存储、图谱或模型体系。

## 已确认根因

### 裸 arXiv ID 被当作全文搜索

`AcademicIntelligence.collect_paper()` 将请求交给 `MultiSourceCollector.collect_paper()`。Collector 只识别 DOI 和 OpenAlex Work ID，裸 arXiv ID 会进入 `search_papers()`；`ArxivSource.search_papers()` 再把它编码为 `all:<id>`，因此 arXiv 会同时返回目标论文和摘要中提到该 ID 的论文。

`ArxivSource.get_paper_by_arxiv_id()` 已使用精确的 `id_list`，但 facade/collector 没有路由到它；该方法还使用宽松的正则搜索并直接返回第一条解析记录，没有对返回 ID 做标准化后的相等校验。

### 重试次数与 HTTP 状态在异常边界丢失

`RetryHandler.execute()` 的局部变量知道已经发生多少次 retry，但终态重抛时没有把次数附着到异常。来源适配器随后以 `raise RateLimitError(...) from exc` 包装 HTTP 异常，新异常的 `context` 为空。`SourceFailure.from_exception()` 只读取最外层异常，不遍历 `__cause__` / `__context__`，所以真实三次 HTTP 请求最终显示 `retry_count=0`、`http_status=None`。

### Agent 外层重试没有停止合同

产品 HTTP 层默认已经执行首次请求加 `max_retries` 次内部重试。`SKILL.md` 说明可以 back off，但没有禁止 Agent 在同一任务中再次创建无界 retry loop，也没有定义 PARTIAL/BLOCKED 的收口条件。外部来源持续 429 时，执行器因而重复运行完整工作流。

## 设计选择

采用外科式边界修复，而不是建立新的通用 identifier registry 或新的 retry result 类型：

1. 在 Collector 入口严格识别完整 arXiv ID，并只把能够执行 `get_paper_by_arxiv_id` 的来源放入精确路径。
2. 在 arXiv adapter 内统一解析可接受的 ID 形式，并对 API 返回记录做 canonical ID 比对。
3. 在 `RetryHandler` 终态异常上附加 `retry_count`；不改变原有异常类型和抛出语义。
4. `SourceFailure.from_exception()` 使用有环保护、深度受限的异常链遍历，按“外层显式 context 优先、底层元数据补缺”的规则恢复 `retry_count` 和 `http_status`。
5. `SKILL.md` 明确产品内部重试与 Agent 外层重试边界，并补齐评测中暴露的 API/snapshot 文档缺口。

## 行为契约

### arXiv 标识符

下列完整输入是精确 arXiv ID：

- `1810.04805`
- `1810.04805v2`
- `arXiv:1810.04805`
- `https://arxiv.org/abs/1810.04805v2`
- 旧式 ID，例如 `hep-th/9901001v2`

包含额外自然语言的字符串（例如 `paper 1810.04805`）不是精确 ID，继续走普通搜索。`limit=0` 仍返回空结果且不发起精确来源调用。

精确查询按去版本后的 canonical ID 比较返回值；API 即使返回多条或错误排序，也只接受 canonical ID 相等的记录。没有相等记录时返回空结果，不以第一条冒充目标。

如果用户只选择不支持 `get_paper_by_arxiv_id` 的来源，Collector 通过既有 capability/失败机制明确报告不支持，不把 ID 降级为可能污染结果的全文搜索。`SKILL.md` 继续要求裸 arXiv ID 使用 `--sources arxiv`。

### 重试元数据

`retry_count` 表示首次请求之后已经实际发生的 retry 次数：

- 首次请求即失败且不重试：0
- 首次失败、重试两次后失败：2
- 第一次重试成功：1，但成功路径不生成 `SourceFailure`

终态 HTTP 429 经来源包装后生成的 `SourceFailure` 必须同时包含实际 `retry_count` 和 `http_status=429`。显式提供在外层异常 `context` 中的值具有最高优先级；异常链只用于补齐缺失字段。遍历必须防止异常链循环。

### Agent 停止协议

`SKILL.md` 明确：

- 库已经执行内部 retry；Agent 不得再创建无界、后台或长时间外层 retry loop。
- 一个来源的内部 retry 耗尽后，同一任务默认不再重复该来源。
- 其他来源产生有效数据时返回 PARTIAL，并逐字保留 `SourceFailure`。
- 所有可用来源失败时返回 BLOCKED/失败结果，不伪造数据。
- 只有用户明确要求等待，或 `retry_after` 很短且任务预算允许时，才允许一次有界外层重试；次数和等待时间必须写入报告。

## 文件边界

- `academic_intelligence/collectors/base.py`：查询分类和精确来源编排。
- `academic_intelligence/sources/arxiv.py`：严格 ID 解析、`id_list` 调用和返回值校验。
- `academic_intelligence/utils/retry.py`：终态异常的 retry 次数标注。
- `academic_intelligence/core/exceptions.py`：异常链元数据恢复。
- `tests/test_fix_ag.py`：本轮所有行为回归。
- `SKILL.md`：Agent 执行合同和精确公开 API。
- `docs/decisions.md`：记录 identifier 路由和 retry metadata 决策。
- `docs/progress.md`：记录实际实施与验收。

不修改六来源的公共方法签名，不新增核心依赖，不改变默认重试次数，不修改存储 schema，不扩大为通用 identifier framework。

## 测试策略

严格执行 TDD：

1. 先写 Collector 精确路由、arXiv 返回值过滤和非 ID 搜索保持不变的失败测试。
2. 观察测试因当前调用 `search_papers` / 返回错误首条而失败。
3. 实施最小 arXiv 修复并跑绿。
4. 再写真实 `HTTPClient` + `httpx.MockTransport` + `ArxivSource` + `SourceFailure` 的重试元数据失败测试。
5. 观察三次 HTTP 调用但 `retry_count=0` / `http_status=None`。
6. 实施 retry metadata 修复并跑绿。
7. 增加 Skill 文档合同检查并同步项目文档。

最终门禁：聚焦测试、完整 pytest、Ruff、mypy、MkDocs strict、wheel 构建、clean venv 安装、`pip check`、CLI/API 离线 dogfood。公网可用时对 BERT、ResNet、GPT-4 三个 ID 各做一次有界真实复验；公网失败只作为外部限制记录，不替代离线确定性验收。

## 工作区策略

项目当前位于普通 `master` 工作树，现有实现包含大量尚未提交的前序成果。新 worktree 只能基于旧 HEAD，无法安全包含当前产品状态，因此经用户批准在当前工作树原地实施。所有编辑使用精确文件目标，保留无关脏文件；本轮不执行 Git commit。

## 验收标准

- K09/K10/K12 对应的精确 ID 路径在确定性测试中各只返回目标记录。
- 支持裸 ID、版本 ID、`arXiv:` 前缀、abs URL 和旧式 ID；自然语言包含 ID 不误判。
- 精确来源返回错误首条时不会冒充目标。
- 真实 HTTP retry 经 source 包装后仍在 `SourceFailure` 中保留次数与状态。
- 既有 DOI、OpenAlex Work ID、标题搜索、异常类型和重试策略保持兼容。
- Skill 明确精确 API、snapshot schema 和有限重试停止协议。
- 全部质量门禁通过，实际命令和风险写入 `docs/progress.md`。
