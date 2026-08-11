# 用户体验测试报告

- 被测对象：Academic Intelligence（`academic-intelligence` 包 + `ai` CLI）
- 版本：0.1.0（`ai --version` / pyproject.toml 一致）
- 环境：Windows 10/11 + Git Bash；Python 3.12.4（Anaconda）；pip 24.0；本地 editable 安装（traceback 显示代码从 `D:\agent_workspace\projects\paper-research-crawler\academic_intelligence\cli.py` 加载）
- 场景：第一次安装和使用——从 README/SKILL.md 出发，完成帮助发现、首次采集、persist/query/stats、expand/export、错误恢复、边界与清理
- 约束声明：本轮换派单禁止访问真实外部学术 API，故"真实在线采集"以断网模拟（`HTTP(S)_PROXY=http://127.0.0.1:9`）；persist 后链路用公开 API 手工种子数据（`seed.py`，仅写 scratch 目录的 db）

## 时间线

1. 读 README.md → 期望：照 `pip install academic-intelligence` 装好就能用 → 实际：环境里已是本地安装，`ai --version` 报 0.1.0 → 感受：PyPI 是否真发布了这个包，文档没有证据，心里没底。
2. `ai --help` → 期望：看到所有命令 → 实际：6 个命令（query/stats/expand/export/export-papers/collect）列出 → 感受：正常；但命令顺序和 README 的叙述顺序不一致（collect 在 README 是主角，help 里排最后），小事。
3. `ai collect --help` / `ai collect citations --help` → 期望：看懂用法 → 实际：用法能看懂，但描述里写着 "(FIX-T F2 / T3)" → 感受：这是什么东西？像开发内部的缺陷单号漏到了用户界面，瞬间怀疑自己是不是装了个内部版。
4. 空目录 `ai stats` → 期望：告诉我什么都没有 → 实际：表格清晰显示 0/0/0，退出码 0，并自动创建了 192KB 的 `academic_intelligence.db` → 感受：好；但"只是看一眼统计就生成了一个数据库文件"略意外，文档没提。
5. `ai query papers`（空库）→ 空表格，退出码 0 → 正常。
6. `ai query authors` → 期望：查作者 → 实际：`Invalid value: Only 'papers' entity is currently supported`，退出码 2 → 感受：README 大篇幅讲 Author 模型和作者消歧，CLI 却不能查作者，落差明显。错误提示本身是清楚的。
7. `ai export-papers --format csv/jsonl`（空库）→ "Exported 0 paper(s)"，CSV 有表头，JSONL 0 字节，退出码 0 → 正常。
8. `ai export-papers --format parquet` → 期望：按 README 说的"可选：pip install ...[export]"，没装就给句人话 → 实际：喷出整屏 NumPy 1.x/2.x 不兼容警告 + 完整 Python traceback + `ImportError`，退出码 2 → 感受：完全看不懂，像程序崩了。真实用户到这一步会认为自己把环境搞坏了。
9. `ai expand "nature14539" --no-fetch-missing`（照 README 示例的实体 id）→ 期望：展开图谱 → 实际：`could not resolve entity type for nature14539`，0 节点，**退出码 0** → 感受：README 的例子直接跑不通；而且明明失败了却返回成功码，写脚本的人会被坑。
10. `ai export --center nature14539`（无 snapshot）→ `Error: graph is empty; run 'ai expand <id> --output graph.json' and retry with ...`，退出码 2 → 感受：这是全产品最好的错误提示，照做就行。
11. 断网模拟 `ai collect paper "10.1038/nature14539" --sources openalex --persist` → 期望：告诉我网络不行 → 实际：重试 3 次后 `Error: All sources failed for get_paper_by_doi (source failures: openalex: ... All connection attempts failed)`，退出码 2 → 感受：结论清楚；但重试日志写 "Retry 1/3 for _do"——`_do` 是内部函数名吧，用户看不懂。
12. `ai collect paper "test" --sources bogus` → `unknown data source(s): bogus; valid sources: arxiv, google_scholar, gs, ieee, oa, openalex, pubmed, s2, semantic_scholar, ss`，退出码 2 → 感受：非常好的错误，把所有合法值列出来了。
13. 种子 2 篇论文 + 1 作者（公开 API `save_batch`）→ `ai stats` 显示 2/1/0 → `ai query papers --author "Hinton"`、`--year 2015-2024`、`--keyword "attention"` 全部过滤正确 → 感受：核心查询链路顺手。
14. `ai expand W2 --relations authors --no-fetch-missing --output graph.json` → 1 节点 1 边，快照写入成功；`ai export --snapshot graph.json --center W2 --radius 2 --output sub.json` → 成功 → 感受：跨进程流程和文档描述一致。但展开结果里作者是 `~Geoffrey Hinton`、`loaded=false` 的占位节点——我明明已经把 Geoffrey Hinton 存进库了，为什么显示"未解析"？（可能是我种子数据没带 author_id，但文档没说清这个关联条件。）
15. 重复跑种子脚本 → stats 仍是 2 篇 1 作者，不重复 → 与文档"upsert 幂等"承诺一致。
16. 边界参数：`--year abc`、`--limit -1`、`--format xml`、`--storage-type xml` → 全部是友好错误 + 退出码 2 → 好。
17. `ai expand W2 --depth 99` → 静默按钳制后的深度跑完，无提示 → 文档（SKILL）说会 clamp，行为一致，但 CLI 用户不会知道自己输入被改了。
18. 清理：检查工作目录残留 → 只有 db 和我自己的输出文件，未见 `.cache/`（未联网所以没触发 HTTP 缓存）；文档提到默认 `cache_path=./.cache/http.json`，意味着真实使用后 cwd 会多一个缓存文件，README 的"清理/卸载"只字未提 → 卸载后残留情况未知（为避免破坏测试环境，未实际执行 pip uninstall）。

## 卡点清单

按严重度排序：

1. **[阻塞] README 的 expand 示例跑不通**：`ai expand "nature14539" --relations references,citations,authors --depth 2 --output graph.json`。证据：实测 `ai expand "nature14539" --no-fetch-missing` 输出 `could not resolve entity type for nature14539`、`nodes: []`；改用完整 DOI `ai expand "10.1038/nature14539"` 同样失败。expand 只接受内部实体 id（如 W 开头的 OpenAlex id），而 README 用了 DOI 片段当 id，且全文没有任何地方告诉用户"expand 的 id 从哪来"。
2. **[严重] parquet 导出直接喷原始堆栈**：`ai export-papers --format parquet --output papers.parquet` → 整屏 `A module that was compiled using NumPy 1.x cannot be run in NumPy 2.2.6` + 完整 traceback + `ImportError: numpy.core.multiarray failed to import`，退出码 2。README 承诺的"可选依赖"体验应该是"一句人话提示你去装"，不是崩溃现场。（证据见时间线 8；与本地 numpy 2.2.6 + 旧 pyarrow 组合有关，但这正是真实用户环境。）
3. **[严重] expand 失败但退出码为 0**：实体不存在时输出 `failed 1 relation(s)` 却 exit 0（证据：时间线 9，`expand-exit:0`）。用户在脚本/管道里无法靠 `$?` 判断失败，与 collect/export 失败时 exit 2 的行为不一致。
4. **[轻微] 内部缺陷单号泄漏到用户帮助**：`ai collect citations --help` 描述含 "(FIX-T F2 / T3)"。
5. **[轻微] 重试日志含内部函数名**：`Retry 1/3 for _do after 1.05s due to: ...`（时间线 11）。
6. **[轻微] `--depth 99` 静默钳制**：无任何提示（时间线 17）。
7. **[轻微] `ai stats` 首次运行静默创建 192KB db 文件**，文档未提（时间线 4）。

## 困惑点

- README 详细介绍 Author 模型、作者消歧、`update_author_papers`，但 CLI 既不能 `query authors` 也没有任何 update 入口——"CLI 能干什么、只能 Python API 干什么"没有一张对照表。
- `--sources` 的 help 写 "(gs,ss,openalex) or 'all'"，但 SKILL.md 说 Google Scholar 默认不注册（需 `enable_google_scholar=True`，且 CLI 没有这个开关）。用户照 help 写 `--sources gs` 会发生什么？未验证（需联网/配置），文档之间口径有张力。
- expand 结果里已入库作者显示为 `~` 占位 + `loaded=false`，关联条件（AuthorRef 需要 author_id 才能挂上已存作者）文档没说透。
- `ai expand --sources` 的默认值是什么？help 没写 default，README 也没说 expand 默认用哪些源。
- CSV 导出的编码：SKILL.md 教用户自己用 pandas 写 `utf-8-sig` 防 Excel 乱码，那 `ai export-papers --format csv` 本身是什么编码？中文标题在 Excel 里会不会乱码？未验证。

## 放弃点

- **真实在线采集（首次 collect 成功路径）**：派单禁止访问真实学术 API，无法验证"README Quick Start 端到端跑一次"这一最核心的新用户体验。用断网模拟替代，只验证了失败路径。
- **pip uninstall 清理验证**：环境是项目本身的 editable 安装，卸载会破坏被测环境，放弃。残留文件（db、`.cache/http.json`）的清理指引缺失这一点仅作记录。

## 一句话总结

**勉强**：本地链路（stats/query/export-papers/expand/export/错误提示）大体顺手且边界校验干净，但 README 的招牌示例（expand "nature14539"）直接跑不通、parquet 导出喷堆栈、expand 失败返回成功码——一个不看源码的真实用户会在图谱这一步卡住且无处求助。

---

# 验收样例（dogfood 用，12 例）

> 每例：ID / 优先级 / 前置 / 数据 / 步骤 / 预期 / 失败判据 / 验证方法。

## AC-01 干净安装与版本自查（P0）
- 前置：干净 venv，Python ≥ 3.11
- 数据：无
- 步骤：`pip install academic-intelligence` → `ai --version` → `python -c "import academic_intelligence"`
- 预期：安装成功；版本号与 pyproject 一致（0.1.0）；导入无警告
- 失败判据：PyPI 查无此包 / 版本不一致 / 导入报错
- 验证：命令输出与退出码。**当前状态：未验证（PyPI 发布状态未知；本地为 editable 安装）**

## AC-02 帮助发现（P1）
- 前置：安装完成
- 步骤：`ai --help`，再对 `collect/query/expand/export/export-papers/stats` 各跑 `--help`
- 预期：每个命令有看得懂的说明、参数有默认值标注
- 失败判据：帮助中出现内部记号（实测 `collect citations` 含 "FIX-T F2 / T3"——**当前失败**）
- 验证：grep help 输出无 `FIX-`/`P\d+`/`_do` 类内部字符串

## AC-03 首次采集 + persist（P0）
- 前置：可联网（或 cassette 回放）；空目录
- 数据：DOI `10.1038/nature14539`
- 步骤：`ai collect paper "10.1038/nature14539" --sources openalex --output paper.json --persist` → `ai stats`
- 预期：paper.json 含去重后记录与 evidence_list；stats total_papers ≥ 1
- 失败判据：stats 为 0 / 无 evidence / 报错无 actionable 信息
- 验证：stats 表格 + `python -m json.tool paper.json`

## AC-04 不带 --persist 不落库且有提示（P1）
- 前置：同 AC-03
- 步骤：`ai collect paper "10.1038/nature14539" --sources openalex`（不带 --persist、不带 -o）→ `ai stats`
- 预期：stats 仍为 0；且 CLI 输出中有一句"未持久化"提示（README 有警告块，CLI 是否提示未验证）
- 失败判据：静默丢弃且无任何提示
- 验证：stats 输出 + collect stdout 文本检查。**当前状态：未验证**

## AC-05 查询过滤语义（P1）
- 前置：库内有 ≥2 篇不同作者/年份论文
- 数据：种子（Hinton/2015、Vaswani/2017）
- 步骤：`ai query papers --author Hinton`、`--year 2015-2024`、`--keyword attention`、`--year abc`、`--limit -1`
- 预期：前三个过滤正确；后两个友好报错 exit 2
- 失败判据：过滤结果错误 / 非法输入喷堆栈
- 验证：表格行数与内容。**当前状态：已通过（本轮实测）**

## AC-06 stats 准确性与幂等（P1）
- 前置：同 AC-05
- 步骤：`ai stats` 记录数值 → 重复 persist 同一批数据 → 再 `ai stats`
- 预期：数值不变（upsert 幂等）
- 失败判据：重复行 / 计数膨胀
- 验证：两次 stats 对比。**当前状态：已通过（本轮实测）**

## AC-07 expand → snapshot → 跨进程 export（P0）
- 前置：库内有已 persist 的论文及其作者
- 步骤：`ai expand <存储id> --relations authors --output graph.json` → 新进程 `ai export --snapshot graph.json --center <id> --radius 2 --output sub.json`
- 预期：graph.json 含 `version`/`nodes`/`edges`；sub.json 含 `center`/`radius`/`node_count`
- 失败判据：快照缺版本字段 / export 读不了 / 节点丢失
- 验证：JSON 字段检查。**当前状态：已通过（本轮实测）；注意 expand 的 id 必须是内部存储 id，DOI 不行（见 AC-12）**

## AC-08 export 空图错误引导（P1）
- 前置：新进程、无 snapshot
- 步骤：`ai export --center xxx --radius 2`
- 预期：报"graph is empty"并给出下一步命令提示，exit ≠ 0
- 失败判据：喷堆栈 / exit 0
- 验证：stderr 文本 + 退出码。**当前状态：已通过（本轮实测，exit 2，提示 actionable）**

## AC-09 export-papers 三格式（P1）
- 前置：库内有含中文标题的论文
- 步骤：分别导出 csv / jsonl / parquet
- 预期：csv/jsonl 用标准库成功；parquet 未装依赖时给一句"请 pip install academic-intelligence[export]"式提示；中文在 Excel 打开不乱码（或文档声明编码）
- 失败判据：parquet 路径喷原始 ImportError 堆栈——**当前失败（本轮实测）**
- 验证：输出文件可解析 + 错误文本不含 "Traceback"

## AC-10 全源失败 / 断网恢复（P0）
- 前置：断网（或代理指向 127.0.0.1:9）
- 步骤：`ai collect paper "10.1038/nature14539" --sources openalex --persist`
- 预期：明确 `All sources failed` 错误 + exit ≠ 0 + 不写脏数据
- 失败判据：挂死 / exit 0 / stats 出现半成品记录
- 验证：stderr + 退出码 + 事后 stats。**当前状态：基本通过（exit 2、信息清楚）；瑕疵：重试日志含内部名 `_do`**

## AC-11 无效输入校验（P2）
- 前置：安装完成
- 步骤：`--sources bogus` / `--year abc` / `--limit -1` / `--format xml` / `--storage-type xml`
- 预期：全部友好报错并列出合法值，exit 2
- 失败判据：任何一例喷堆栈或静默接受
- 验证：逐条跑。**当前状态：全部通过（本轮实测）**

## AC-12 expand 标识符与失败退出码（P0）
- 前置：库内有 DOI 为 `10.1038/nature14539` 的论文
- 步骤：`ai expand "nature14539"`、`ai expand "10.1038/nature14539"`、`ai expand <真实存储id>`；检查各自退出码
- 预期：文档示例所用 id 形式可用，或文档改为真实存储 id；实体不存在时 exit ≠ 0
- 失败判据：README 示例 id 无法 resolve——**当前失败**；失败时 exit 0——**当前失败**
- 验证：stdout + `$?`

## AC-13 卸载与清理边界（P2）
- 前置：用过一段时间（产生 db、`.cache/http.json`、输出文件）
- 步骤：`pip uninstall academic-intelligence` → 检查 cwd 与 `~/.cache` 残留
- 预期：文档有"哪些文件是数据、如何备份/删除"的说明；卸载不删用户数据
- 失败判据：无任何清理文档 / 卸载误删数据
- 验证：文档检查 + 目录对比。**当前状态：未验证（本轮未执行卸载；README 无清理指引——文档缺口已成立）**

---

# 用户会误解的承诺（文档 vs 实际）

1. **README `ai expand "nature14539" ...`**：用户会以为 DOI 片段是合法实体 id。实测 DOI 片段和完整 DOI 都 `could not resolve entity type`。反例成立（证据见 AC-12）。
2. **README "From PyPI: pip install academic-intelligence"**：PyPI 是否发布未验证；若未发布，这是第一大坑。未知项。
3. **README "Parquet is optional"**：暗示"装了就能用/没装给提示"。实测在 numpy 2.x 环境直接堆栈。反例成立（环境相关，但真实用户环境如此）。
4. **README 大篇幅的 Author/消歧/增量更新**：CLI 无 query authors、无 update 命令，用户会找半天。文档未划清 CLI/API 边界。
5. **`--sources` help "(gs,ss,openalex)" vs SKILL "GS 默认不注册"**：用户照 help 用 gs 可能得到"静默没有 GS 结果"。未验证（需联网）。
6. **`ai expand --depth` "max 3"**：超界静默钳制，用户以为按自己输入跑了。

# 反例汇总（实测证据）

| # | 承诺 | 实测 | 证据 |
|---|------|------|------|
| 1 | README expand 示例 | `could not resolve entity type for nature14539`，exit 0 | 时间线 9 |
| 2 | parquet 可选导出 | NumPy 兼容警告 + 完整 traceback + ImportError，exit 2 | 时间线 8 |
| 3 | 失败可脚本检测 | expand 实体不存在 exit 0（collect/export 失败均 exit 2，不一致） | 时间线 9 |
| 4 | 用户界面无内部术语 | help 含 "FIX-T F2 / T3"；日志含 "_do" | 时间线 3、11 |

# 未知项

- PyPI 是否已发布 `academic-intelligence`（AC-01 前提）
- 真实在线采集端到端（受派单限制未测）：collect 成功路径的输出质量、`--persist` 缺省时 CLI 有无提示（AC-04）
- `--sources gs`/`all` 在未配 SerpAPI key 时的 CLI 行为
- CSV 导出编码与 Excel 中文表现
- pip uninstall 后残留与清理路径
- expand 结果中 `~` 占位作者与已入库作者的关联条件（是否仅因种子数据缺 author_id）
