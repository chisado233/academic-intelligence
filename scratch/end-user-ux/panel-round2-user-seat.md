# 方案轮·用户席：Academic Intelligence 硬化方案的用户视角提案

- 轮次：详细规划 / 第二轮方案会（panel）
- 席位：end-user（真实安装用户 / 自动化脚本作者 / 中文·Windows·Excel 使用者）
- 依据材料：`docs/plans/2026-08-09-academic-intelligence-hardening-design.md`（设计 §9 全部 required ID）、`README.md`、上一轮实测 `scratch/end-user-ux/ux-report.md`
- 状态：完成。逐 ID 给出用户可见行为、错误文案、命令形状、兼容风险；附 dogfood 脚本与未知项。
- 实际加载技能：无（派单要求"不主动加载 skill"，最低必加载为无）

---

## 一、用户流程总图（验收的故事线）

一个真实用户的验收路径必须是这一条直线，任何一环断裂都算方案失败：

1. **装**：干净 venv → `pip install academic-intelligence`（或本地 wheel）→ `ai --version` 有输出。
2. **发现**：`ai --help` 看到的命令集 = README 讲到的命令集，help 文本里没有内部记号。
3. **首跑**：照 README Quick Start 跑通采集 → `--persist` → `ai stats` 数字 > 0。
4. **查**：`ai query papers ...` 中文关键词、大小写、带空格/通配符的输入行为一致可预期。
5. **图**：照 README 拿到一个可 expand 的 id → `ai expand ... --output graph.json` → 新窗口 `ai export --snapshot graph.json ...` 成功。
6. **表**：`ai export-papers --format csv` 出的文件，用 Excel 双击打开中文不乱码、不以公式执行单元格；同时保留 raw CSV 给脚本。
7. **坏路径**：断网、错误 id、缺依赖、损坏的 graph.json，全都是"一句人话 + 非零退出码"，永不喷堆栈。
8. **自动化**：脚本靠 `$?` 判断成败；部分失败有明确标注且退出码语义写进 `--help`/README。

方案的所有逐 ID 条目都挂在上述某一环上。

---

## 二、逐 required ID 用户方案

### SEC-01 — HTTP 密钥不泄漏

- **用户可见行为**：API key 配错或源返回 401/403 时，终端和日志里出现的是"哪个源、什么状态码、怎么办"，永不出现 key 本身——包括 URL 的 query 部分（`?api_key=...`）。
- **错误文案（建议）**：
  `Error: openalex returned HTTP 401 (authentication failed). Check your OPENALEX_EMAIL / API key configuration. Key values are redacted from this message.`
- **命令形状**：无新命令；`ai collect ...` 失败路径的输出形态变化。
- **兼容风险**：低。唯一风险是用户此前习惯"把报错截图发群里/debug"，脱敏后排查要靠 host+status 而非完整 URL——README 应注明"日志已脱敏，排查请提供源名与状态码"。上一轮实测断网报错（ux-report 时间线 11）已经不含密钥，本项是把 401/403 路径拉到同一水准。

### DATA-01 — 合作者计数随论文变更收敛

- **用户可见行为**：同一篇论文改作者、删论文、重复导入，之后 `ai expand <作者id> --relations coauthors` 和统计里的合作者数量始终是"当前事实"，不会越跑越多、也不会删了论文还留着幽灵合作关系。
- **错误文案**：正常路径无文案；若底层写入失败，必须整笔回滚并报：
  `Error: failed to update paper <id>; no partial changes were saved.`
- **命令形状**：无变化；体现在 `ai collect --persist` 重复执行、`ai expand` 的 coauthors 数字。
- **兼容风险**：中。升级后**老数据库里已存在的虚高计数**怎么办？用户方案：connect 时检测到不一致则一次性后台重建并在 CLI 打一句 `Rebuilt coauthorship index: N pairs corrected.`——只打一次，不要每次启动都刷。

### DATA-02 — 引用关系 ID 稳定

- **用户可见行为**：同一对引用重复导入，stats 里引用数不膨胀；用快照导出再导入，关系不会翻倍。
- **错误文案**：正常无文案。
- **命令形状**：无变化。
- **兼容风险**：低-中。设计明确"migration 不得改写已有关系 ID"，用户侧只关心：升级前后 `ai stats` 的 citations 数字不跳变。dogfood 里加一步升级前后 stats 对比即可。

### DATA-03 — 同名不同人不被合并

- **用户可见行为**：库里有两个同名"李华"（不同单位/方向）时，采集结果和 `ai expand` 的作者节点保持两个人，各自标 `disambiguation_status`；共享 ORCID 的才合并。用户应能通过某个入口看到"这条作者记录是 auto/ambiguous/confirmed"。
- **错误文案**：不算错误，但合并/不合并应可见：
  `Author "Hua Li" kept separate from existing record (different affiliation/topic); status=ambiguous.`（CLI verbose 或 collect 输出的 author 表带 status 列）
- **命令形状**：建议在 `ai collect author`/`ai author` 的输出表中加 `status` 列；不新增命令。
- **兼容风险**：中。老库可能已经被旧逻辑"错误合并"过。用户方案：文档声明"本次修复不追溯拆分历史合并"，并提供一条一次性检查手段（哪怕只是 `ai query` 出作者列表让用户自查），不要静默变更用户已有数据。

### QUERY-01 — 关键词查询两后端一致

- **用户可见行为**：`ai query papers --keyword "深度学习"`、`--keyword "attention*"`（用户手滑带星号）、`--keyword "C++"`，在 SQLite 和 JSON 后端下得到相同的命中集合；纯关键词命中（不在标题/摘要里）也能查到。
- **错误文案**：关键词为空或全空白时：
  `Invalid value: --keyword must not be empty.`（exit 2，与现有风格一致）
- **命令形状**：`ai query papers --keyword <text>` 不变；help 里加一句 `Searches title, abstract, and keywords; matched literally (no wildcards).`
- **兼容风险**：低。但要防止"修了关键词进 FTS 之后，老库查不到关键词"——老库 connect 时的 backfill 必须覆盖 keywords 列，否则升级用户会出现"同样的词升级前查得到、升级后查不到"的回退感。dogfood：升级前建库 → 升级 → 同关键词结果集不变（或变多是允许的，变少不行，需在文案中说明）。

### QUERY-02 — Unicode 兴趣词等价查询

- **用户可见行为**：用户用拼音输入法/macOS 复制来的带调字符（NFC/NFD 形态不同）查作者兴趣，两种形态命中同一批作者；大小写不敏感。
- **错误文案**：正常无文案。
- **命令形状**：无变化。
- **兼容风险**：低。持久化的展示值不改（设计已明确），只改比较逻辑，对用户无感——这正是用户想要的"无感修复"。

### ASYNC-01 — 并发连接与取消安全（用户侧：不重复建库、不留半成品）

- **用户可见行为**：用户在脚本里并发调 `connect()`，目录里不会出现两个 db 文件/两个缓存文件；Ctrl+C 中断一次采集，不会把共享的抓取也搞挂，下一次运行正常；断线/超时被当作"可重试的临时故障"，CLI 重试后给出源级原因。
- **错误文案**（断网路径，沿用并改善上轮实测）：
  `Error: All sources failed for get_paper_by_doi (openalex: connection failed — check your network or proxy).`（exit 2）
  重试日志把 `_do` 换成操作名：`Retry 1/3 for openalex.get_paper_by_doi after 1.0s ...`
- **命令形状**：无变化。
- **兼容风险**：低。用户侧只观察到"日志更好懂、Ctrl+C 不留烂摊子"。注意 Windows 上 Ctrl+C 的行为要在 dogfood 里真按一次，不要只在 Unix 上验证。

### STORAGE-01 — JSON 后端双写保护与 close 后拒绝写入

- **用户可见行为**：用户（或其脚本）不小心对同一目录开了第二个 JSON 存储，立刻得到明确报错而不是两个文件互相覆盖后数据丢失。
- **错误文案**：
  `Error: storage directory '<path>' is already open for writing in this process. JSON storage is single-writer; use storage_type="sqlite" if you need concurrent access.`
  close 后再写：
  `Error: storage is closed; create a new AcademicIntelligence/Storage instance to continue.`
- **命令形状**：无 CLI 变化（JSON 后端主要被库用户用）；README 的 `storage_type="json"` 说明旁加一句"单进程单写者，多进程请用 sqlite"。
- **兼容风险**：低-中。有用户脚本可能恰好依赖了"开两个实例"的未定义行为且碰巧没坏——现在会变硬错误。这是好事，但发版说明里要有一行"行为变更"。

### GRAPH-01 — 快照完整性校验

- **用户可见行为**：`ai export --snapshot graph.json` 读到一个手改坏的/被别的工具截断的/版本不认识的文件时，拒绝并说明哪里坏，而不是默默展开半个图。
- **错误文案**（按故障类型，保持同一模板 `Invalid snapshot <file>: <原因> (<补救>)`）：
  - `Invalid snapshot graph.json: node_count says 12 but file contains 9 nodes. Re-export with 'ai expand ... --output graph.json'.`
  - `Invalid snapshot graph.json: edge 'e7' points to missing node 'W999'.`
  - `Invalid snapshot graph.json: snapshot version 99 is not supported (supported: 1). Regenerate with this version of academic-intelligence.`
  exit 一律为 2（与设计 §6 一致）。
- **命令形状**：`ai export --snapshot <file>` 不变。
- **兼容风险**：中。老版本生成的快照若本身就不满足新校验（例如历史 bug 写出的 count 不一致），升级后用户的存量 graph.json 会突然"打不开"。用户方案：错误文案里必须给补救命令（如上），README/升级说明里明确"旧快照需重新导出"。

### CLI-01 — 退出码语义与承诺命令落地

- **用户可见行为**（自动化脚本作者是本项的核心用户）：
  - 退出码契约写进 `ai --help` 顶部 epilog 和 README：`0 = 成功（或已明确标注的部分成功）；1 = 操作整体失败；2 = 输入/用法错误`。全产品统一，任何命令不例外。
  - `ai expand <不存在的id>`：输出保留部分结果但打 `Partial result: 1 relation(s) failed` 且 **exit 非 0**（修复上轮实测"失败返回 0"）。
  - 新命令全部可用且是薄封装：
    - `ai paper <doi-or-title> [--sources ...] [--persist] [--output file]` —— 采集并打印单篇。
    - `ai author <name> [--persist]` —— 打印作者画像（含 disambiguation status）。
    - `ai author-papers <name> [--persist] [--limit N]` —— 打印该作者论文列表。
    - `ai update --author <name>` —— 增量更新并打印结构化统计：`updated=3 new=1 unchanged=12 skipped(stale)=0`。
- **错误文案**：
  - 实体找不到：`Error: could not resolve entity 'nature14539'. Expand requires a stored paper id (e.g. 'W2626778328'). Find one with 'ai query papers --keyword ...' or collect first with --persist.`（exit 2；同时修 README 示例）
  - 部分成功：`Warning: partial result — 2 of 3 relations succeeded; failed: citations (openalex: rate limited). Exit code is 0 because useful output was produced.`——"部分成功 exit 0"必须同时满足"有可用输出"和"显式标注"两个条件，这是契约。
- **命令形状**：如上四条新命令 + 退出码表。`ai collect ...` 全系列保留不动。
- **兼容风险**：中。已有用户脚本可能在靠"expand 失败也 exit 0"跑通流水线——改成非零后这些脚本会"变红"。这正是修复目的，但发版说明必须单独点名这一条行为变更，否则用户会以为产品退步了。

### EXPORT-01 — 可选依赖降级、Parquet 稳定 schema、Excel-safe CSV

- **用户可见行为**：
  - 未装/损坏的 `pyarrow`：`ai export-papers --format parquet` 给一句人话，exit 2，无 traceback（修复上轮实测整屏 NumPy 堆栈）。
  - CSV 分两种，help 里讲清区别：
    - `ai export-papers --format csv`（raw）：标准 UTF-8、逐字导出，给脚本/pandas 用，一个字节都不改。
    - `ai export-papers --format csv-excel`（或 `--excel-safe` 开关，二选一，建议独立 format 更显式）：UTF-8 BOM + 以 `=`/`+`/`-`/`@` 开头的文本字段前置 `'`，Excel 双击打开中文不乱码、不触发公式执行。
  - Parquet 空表/首批全空值/多批导出，schema 一致，pandas 直接 `read_parquet` 不炸。
- **错误文案**：
  `Error: Parquet export requires the optional dependency 'pyarrow'. Install it with: pip install "academic-intelligence[export]"`
  （ABI 损坏场景同文案，尾部加 `If pyarrow is already installed, reinstall it to match your NumPy version.`）
- **命令形状**：
  `ai export-papers --format {csv|csv-excel|jsonl|parquet} --output <file>`；help 注释：`csv = raw lossless (UTF-8); csv-excel = BOM + formula-safe for Microsoft Excel`。
- **兼容风险**：低。新增 format 是增量；raw csv 语义必须**逐字节保持现状**，已有管道零影响。风险点只在"默认 format 不要偷改成 excel-safe"。

### ENG-01 — 工程门（用户侧翻译：装到的 wheel 是干净的）

- **用户可见行为**：用户不关心 ruff/mypy，关心的是"我 `pip install` 到的这个 wheel 在干净机器上 `ai --help` 能用、中文路径能用"。用户方案：ENG-01 的验收物里必须有**面向用户的 smoke 证据**而不只是 CI 绿：
  - 干净 venv 装 wheel → `ai --version`、`ai --help`、四条新命令 `--help` 全部通过。
  - **不使用源码目录**的 Unicode 路径 dogfood（见第四节脚本）。
- **错误文案**：n/a。
- **命令形状**：n/a。
- **兼容风险**：低。唯一要求：dogfood 证据写进 docs（DOC-01 联动），用户能在文档里看到"哪一版在哪天通过了什么检查"。

### DOC-01 — 文档与真实行为一致

- **用户可见行为**（本项是用户席最重的关切，上一轮一半卡点源于此）：
  - README expand 示例改为真实可用流程：`ai query papers` 找到存储 id → `ai expand "<存储id>" ...`；删除 `nature14539` 当实体 id 的写法。
  - README 安装节：若未发 PyPI，改成 `pip install <wheel>` 或明确标注"尚未发布 PyPI，请从源码/wheel 安装"——不要留一句无法验证的 `pip install academic-intelligence`。
  - 加一张 **CLI / Python API 能力对照表**：query 只有 papers、作者查询/增量更新走哪条命令，一表讲清。
  - `--persist` 警告、快照跨进程流程、citation 50 条上限这些已有的好文档保留原样。
  - help 文本清除 `FIX-T F2 / T3`、`_do` 等内部记号（grep 级验收）。
  - 补一节"数据文件与卸载"：db 在哪、`.cache/http.json` 是什么、卸载前如何备份。
- **错误文案**：n/a。
- **命令形状**：n/a。
- **兼容风险**：低。文档改动无行为风险；注意 README 当前为 CRLF 行尾，编辑时保持原行尾风格，避免整文件 diff 噪声。

### HYGIENE-01 — 非破坏性卫生

- **用户可见行为**：用户 `git status`（若从源码安装）不再被 `.pyc`/coverage/site 噪声淹没；已跟踪的历史产物一个不动。
- **错误文案**：n/a。
- **命令形状**：n/a。
- **兼容风险**：低。用户方案唯一要求：`.gitignore` 的添加本身不得顺手删/取消跟踪任何已存在文件——这在设计里已冻结，用户席只强调验收时 `git status` 前后对比"既有条目数不减少"。

---

## 三、风险 / 反例汇总（用户视角，按杀伤力排序）

1. **README expand 示例即反例**（上轮实测反例 #1）：DOC-01 若不改示例，CLI-01 修得再好用户第一步就卡死。两项必须绑定验收。
2. **exit 0 假成功**（反例 #3）：CLI-01 是自动化用户的底线项；同时"部分成功 exit 0"的规则若文档不写清，会变成新的脚本陷阱。
3. **parquet 堆栈**（反例 #2）：EXPORT-01 的文案必须在 numpy2+旧 pyarrow 的真实组合下验证，不能只在"未安装 pyarrow"下验证——后者太干净，前者才是用户环境。
4. **老库/老快照升级回退感**：DATA-01 计数重建、QUERY-01 backfill、GRAPH-01 旧快照拒绝，三处都会让存量用户"升级后看到新行为"。每一处都要配一次性提示文案或补救命令，否则用户会把修复当回归。
5. **同名作者不追溯拆分**（DATA-03）：要防用户在 issue 里问"为什么我的老库里两个李华还是一个人"——文档提前声明。
6. **Windows 特有面**：中文路径 + 空格路径、Ctrl+C、Excel 双击打开 BOM，这三件必须在 Windows 上真机验证（本工作区即 Windows，dogfood 无借口跳过）。

---

## 四、替代方案（若主方案被砍）

- **csv-excel 独立 format vs `--excel-safe` 开关**：主方案选独立 format（help 里一眼可见、脚本里自描述）。备选开关方案优点是格式矩阵不膨胀；缺点是用户得先知道有这个坑才会去找开关——而不知道坑的用户正是受害者。坚持独立 format；若实现成本被质疑，退而求其次：raw csv 保持现状 + README 显式教 `pandas.read_csv(..., encoding="utf-8-sig")` 不行（那是导出不是导入）——正确退路是在 help 的 csv 选项描述里直接写"Excel 用户请用 csv-excel"。
- **老库 coauthorship 自动重建 vs 提供 `ai rebuild-index` 手动命令**：主方案选首次 connect 自动重建+一次性提示（零用户动作）。备选手动命令更保守，但真实用户不会知道自己需要跑它。坚持自动，但重建超过 N 秒时要打进度行。
- **`ai paper` 等四命令 vs 只修文档教人用 `collect`**：备选"不加命令只改文档"实现最便宜，但 Goal 已承诺四条命令，DOC-01"文档不得含不存在的命令/虚假承诺"反向要求命令存在。不接受纯文档方案。

---

## 五、Dogfood 脚本（从安装好的 wheel 跑，不依赖源码树）

```bash
#!/usr/bin/env bash
# dogfood-user.sh — 用户席验收脚本（Windows Git Bash / 干净 venv 中运行）
set -u
FAIL=0
check() { # check <name> <expected_code> <actual_code>
  if [ "$2" = "$3" ]; then echo "PASS  $1 (exit $3)";
  else echo "FAIL  $1 (want $2, got $3)"; FAIL=1; fi
}

# 0) 安装与发现
ai --version >/dev/null 2>&1;                       check "version"          0 $?
ai --help 2>&1 | grep -Eiq 'FIX-|_do' && { echo "FAIL  help-internal-tokens"; FAIL=1; } || echo "PASS  help-clean"
ai paper --help >/dev/null 2>&1;                    check "paper-help"       0 $?
ai author --help >/dev/null 2>&1;                   check "author-help"      0 $?
ai author-papers --help >/dev/null 2>&1;            check "authorpapers-help" 0 $?
ai update --help >/dev/null 2>&1;                   check "update-help"      0 $?

# 1) Unicode + 空格路径（不触碰源码目录）
WORK="/d/agent_workspace/tmp/dogfood 中文 目录"
mkdir -p "$WORK" && cd "$WORK"
ai stats --db "$WORK/数据 库.db" >/dev/null 2>&1 || ai stats >/dev/null 2>&1
check "unicode-path-stats" 0 $?

# 2) 退出码契约（断网 / 坏实体 / 坏快照 / 缺依赖）
HTTPS_PROXY=http://127.0.0.1:9 ai collect paper "10.1038/nature14539" --sources openalex 2>err1.txt
check "offline-collect" 2 $?; grep -qi 'Traceback' err1.txt && { echo "FAIL  offline-traceback"; FAIL=1; } || echo "PASS  offline-no-traceback"
ai expand "nature14539" --no-fetch-missing >/dev/null 2>&1
[ $? -ne 0 ] && echo "PASS  expand-bad-id-nonzero" || { echo "FAIL  expand-bad-id-zero"; FAIL=1; }
echo '{"version":1,"node_count":5,"nodes":[],"edges":[]}' > bad.json
ai export --snapshot bad.json --center X --output out.json 2>err2.txt
check "bad-snapshot" 2 $?; grep -qi 'node_count' err2.txt && echo "PASS  snapshot-reason" || { echo "FAIL  snapshot-reason"; FAIL=1; }
ai export-papers --format parquet --output p.parquet 2>err3.txt
check "parquet-missing" 2 $?; grep -qi 'Traceback' err3.txt && { echo "FAIL  parquet-traceback"; FAIL=1; } || echo "PASS  parquet-friendly"
grep -qi 'academic-intelligence\[export\]' err3.txt && echo "PASS  parquet-install-hint" || { echo "FAIL  parquet-install-hint"; FAIL=1; }

# 3) CSV 双模式（需要库里有含中文标题和以 = 开头字段的种子数据）
ai export-papers --format csv       --output raw.csv   >/dev/null 2>&1; check "raw-csv"   0 $?
ai export-papers --format csv-excel --output excel.csv >/dev/null 2>&1; check "excel-csv" 0 $?
head -c3 excel.csv | grep -q $'\xef\xbb\xbf' && echo "PASS  excel-bom" || { echo "FAIL  excel-bom"; FAIL=1; }
head -c3 raw.csv   | grep -q $'\xef\xbb\xbf' && { echo "FAIL  raw-has-bom"; FAIL=1; } || echo "PASS  raw-no-bom"
grep -q "^'=" excel.csv || grep -q ",'=" excel.csv && echo "PASS  formula-neutralized" || echo "WARN  formula-seed-missing"
# Excel 双击验证（人工步）：excel.csv 中文不乱码；raw.csv 用 pandas 读 round-trip 逐字一致

echo; [ $FAIL -eq 0 ] && echo "ALL PASS" || echo "HAS FAILURES"
exit $FAIL
```

人工补验项（脚本覆盖不了的）：Windows 资源管理器双击 excel.csv；采集过程中按一次 Ctrl+C 后重跑；老库升级后首次启动是否出现一次性重建提示。

---

## 六、未知项（需要其他席位/后续轮次回答）

1. PyPI 是否已发布 `academic-intelligence`——决定 README 安装节写法和 AC-01 是否可验。
2. 退出码用 `1` 还是其他值表示"操作整体失败"（设计只说 non-zero）；用户侧只要求：**选一个值、全部命令统一、写进 help**。
3. 老库 coauthorship 重建的耗时量级（万级论文上是否秒级）——决定要不要进度行。
4. `csv-excel` 的公式中和规则对合法以 `-` 开头的年份/编号文本的误伤面（建议只中和以 `=`/`+`/`@` 开头及 `-` 后跟非数字的情形，需数据席位确认）。
5. `--sources gs` 在未配 SerpAPI key 时的目标行为（静默跳过还是显式提示"GS 未启用"）——上轮已标记文档口径张力，设计 §9 未覆盖，建议补进 CLI-01 或 DOC-01。

## 七、用户席结论（一句话）

方案在纸面上覆盖了上一轮实测的全部阻塞/严重卡点（README 示例、parquet 堆栈、假成功退出码）；真正决定"真实用户能否独立走通"的是 DOC-01 与 CLI-01 是否绑定验收、以及 dogfood 是否真在 Windows + 中文路径 + 缺依赖的真实组合下跑——脚本已在上方给出，照跑即可证伪。
