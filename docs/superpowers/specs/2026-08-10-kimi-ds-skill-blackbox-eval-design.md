# Kimi DeepSeek Skill 黑盒评测设计

## 目标

使用精确执行目标 `kimi` + `opencode-go/deepseek-v4-flash`，只向执行者提供
`SKILL.md` 和已安装 wheel 的 Python/CLI 入口，通过 50 个相互独立的真实任务，验证
Academic Intelligence 是否能被陌生 Agent 按用户面契约正确操作。

本轮评测测的是“Skill 文档驱动的真实可操作性”，不是让 Agent 阅读源码后复述功能，
也不是让它修改项目。

## 调度选择

- 派单类型：岗位单恒等调用。
- 执行目标：`mycli agent-cli run kimi --model opencode-go/deepseek-v4-flash`。
- 强度：medium。
- 交互：Auto。
- Goal：启用，50 项全部执行。
- 模型回退：禁用；精确模型不可用时记 `BLOCKED`，不得换模型伪装成功。
- 隔离：一题一个全新 run、独立 cwd、独立输出目录和独立会话。

## 黑盒边界

执行 Agent 只允许：

1. 读取 `D:\agent_workspace\projects\paper-research-crawler\SKILL.md`。
2. 调用干净 wheel 环境中的：
   - `D:\agent_workspace\tmp\paper-research-crawler-installed-20260809-final2\venv\Scripts\python.exe`
   - `D:\agent_workspace\tmp\paper-research-crawler-installed-20260809-final2\venv\Scripts\ai.exe`
3. 在自己的 task 目录写临时代码、数据库、导出文件、`report.md` 和 `result.json`。
4. 通过 Academic Intelligence 自身访问公开学术数据源。

执行 Agent 禁止读取项目源码、README、tests、其他 docs、旧评测档案或本轮隐藏判定；
禁止使用 curl、浏览器、搜索引擎、requests/httpx 直连数据源；禁止 Git、mycli、下级 Agent
和项目文件修改。拿不到数据必须报告，不得用模型记忆补字段。

## 标准输出契约

每题必须写 `result.json`：

```json
{
  "task_id": "K01",
  "worker_status": "PASS|PARTIAL|FAIL|BLOCKED",
  "summary": "简述实际结果",
  "commands": [
    {"command": "实际命令", "exit_code": 0, "stdout_excerpt": "关键原始输出"}
  ],
  "observations": {},
  "artifacts": ["绝对路径"],
  "errors": []
}
```

同时写 `report.md`，说明步骤、原始证据、局限和未取得字段。Worker 的
`worker_status` 只是自报，最终状态由主 Agent 独立核验。

## 50 项冻结任务

### A. 文档发现与基础契约（K01-K05，本地）

| ID | 任务 | 必须观察 |
|---|---|---|
| K01 | CLI 版本和根帮助发现 | 版本、`paper`、`author`、`author-papers`、`update`、`export-papers` |
| K02 | 未连接时查询来源能力表 | 六来源、arXiv/IEEE citation unsupported、OpenAlex citation supported |
| K03 | 构造安全的临时 SQLite Config | sources、storage_type/path、并发和展开上限被正确读取 |
| K04 | 空库 async lifecycle | connect/context/close 成功，stats 为零且不泄露绝对数据库路径 |
| K05 | 结果模型序列化往返 | Paper、Author、CollectionResult、ExpandResult 往返保持关键字段 |

### B. 真实论文采集与诚实降级（K06-K15，公网）

| ID | 任务 | 输入 |
|---|---|---|
| K06 | 单源 DOI 精确采集 | `10.1038/nature14539`，OpenAlex |
| K07 | DOI 多源融合 | `10.1038/nature14539`，OpenAlex + PubMed |
| K08 | arXiv 精确采集 | `1706.03762` |
| K09 | BERT arXiv 精确采集 | `1810.04805` |
| K10 | ResNet arXiv 精确采集 | `1512.03385` |
| K11 | VGG arXiv 精确采集 | `1409.1556` |
| K12 | GPT-4 报告 arXiv 精确采集 | `2303.08774` |
| K13 | 不存在 DOI 的诚实空结果 | `10.9999/definitely-not-a-real-paper-20260810` |
| K14 | AlphaFold DOI 采集 | `10.1038/s41586-021-03819-2` |
| K15 | CRISPR DOI 采集 | `10.1126/science.1225829` |

### C. 作者、引用与增量（K16-K25，公网 + 本地）

| ID | 任务 | 输入/能力 |
|---|---|---|
| K16 | Geoffrey Hinton 作者调研 | OpenAlex，资料和代表论文 |
| K17 | Yann LeCun 作者调研 | OpenAlex，资料和代表论文 |
| K18 | Yoshua Bengio 作者调研 | OpenAlex，资料和代表论文 |
| K19 | Michael I. Jordan 同名候选调研 | OpenAlex，必须披露歧义和所选候选证据 |
| K20 | Nature Deep Learning 被引论文 | 先解析 OpenAlex ID，再用模块收集 citations |
| K21 | Attention 一层 references 展开 | persist 后 expand，报告 stub/loaded 数量 |
| K22 | Hinton 作者立即二次增量更新 | 首次 persist，随后 `update_author_papers` |
| K23 | Nature Deep Learning 立即二次论文更新 | 首次 persist，随后 `update_paper` |
| K24 | 同名但相同 authority 类型 ID 冲突 | 本地 AuthorDisambiguator，必须保持两人 |
| K25 | 精确姓名和机构、不同 authority 系统 | 本地 AuthorDisambiguator，验证可合并并保留证据 |

### D. 存储与查询一致性（K26-K35，本地）

| ID | 任务 | 必须观察 |
|---|---|---|
| K26 | SQLite `save_batch` 幂等重放 | 三篇论文写两次，total_papers 仍为 3 |
| K27 | JSON `save_batch` 幂等重放 | 三篇论文写两次，total_papers 仍为 3 |
| K28 | SQLite 结构化 keywords 查询 | 关键词仅在 `keywords` 数组中也能命中 |
| K29 | Unicode NFC/NFD 兴趣查询 | 两种规范形式在 SQLite/JSON 都能命中同一作者 |
| K30 | SQLite 多过滤器 AND + 年份区间 | 只返回同时满足全部条件的记录 |
| K31 | Paper cursor 稳定分页 | 重复 year 下无重无漏，cursor 传上一页实体 ID |
| K32 | Author cursor 稳定分页 | 重复 name 下无重无漏 |
| K33 | SQLite Citation pair upsert | 单条和 batch 返回同一持久 ID，存储只有一条边 |
| K34 | SQLite coauthorship 可撤销 | 作者列表更新/论文删除后旧 pair 消失 |
| K35 | JSON writer 生命周期 | 同目录第二写者和 close 后写入均明确失败 |

### E. 图谱、导出与 CLI 失败语义（K36-K45，本地 + 公网）

| ID | 任务 | 必须观察 |
|---|---|---|
| K36 | 图快照 roundtrip | 节点、边、属性、版本完整保持 |
| K37 | 图快照错误 node/edge count | load 明确拒绝 |
| K38 | 图快照重复节点/有向边 | load 明确拒绝 |
| K39 | 本地图 path + subgraph | 方向、半径、node_count/edge_count 正确 |
| K40 | raw CSV | UTF-8 无 BOM，公式前缀保持原样 |
| K41 | Excel-safe CSV | UTF-8 BOM，`= + - @ TAB CR` 前缀中和 |
| K42 | JSONL 分批导出 | 一记录一行，嵌套字段仍为合法 JSON |
| K43 | 未装 pyarrow 的 Parquet | exit 2、保留 `[export]` 提示、无 traceback |
| K44 | CLI 跨进程图工作流 | 真实论文 persist → expand snapshot → 新进程 export subgraph |
| K45 | 无法展开实体的退出码 | 全失败非零；有可用局部结果时为零 |

### F. 安装、并发与综合场景（K46-K50）

| ID | 任务 | 必须观察 |
|---|---|---|
| K46 | 干净 wheel 运行时依赖 | `pip check` 通过，导入来自 site-packages，Requires 含 click |
| K47 | SQLite 多实例并发写 | 独立实例并发写后记录完整、无静默丢失 |
| K48 | 端到端研究工作流 | collect → persist → query → expand → snapshot → JSONL export |
| K49 | 隐藏答案论文调研 | arXiv `2203.02155`，只凭模块证据形成结构化报告 |
| K50 | K06 全新会话重复 | 与 K06 的标题、年份、DOI、核心作者一致 |

## 批次与并发

- Pilot：K01 单独运行，验证精确模型、写权限和输出契约。
- Local-1：K02-K05、K24-K32，最多 10 并发。
- Live-1：K06-K15，最多 4 并发，减少源站限流。
- Live-2：K16-K23，最多 3 并发。
- Local-2：K33-K43、K45-K47，最多 10 并发。
- Integrated：K44、K48、K49，最多 2 并发。
- Repeat：K50 最后单独运行，随后与 K06 比较。

## 判定

- `PASS`：任务通过模块完成；命令、日志和实际产物一致；事实/契约正确。
- `PARTIAL`：模块路径走通，但公开源限流或缺字段；不得有错误事实。
- `FAIL`：伪造、绕过模块、关键事实错误、产物与报告矛盾、产品/Skill 契约未实现。
- `BLOCKED`：模型线路、网络或外部服务阻塞，且有原始错误证据。

套件给出两种分数：

1. 执行完成率：产生可审计 `result.json` 的任务数 / 50。
2. 能力通过率：`PASS / (PASS + PARTIAL + FAIL)`；BLOCKED 单列。

任何伪造或绕过模块都构成整套诚信红线。K01、K06、K07、K16、K26、K36、K44、
K48 是核心门槛；它们出现产品/Skill `FAIL` 时不能宣称整体通过。

## 产物

- 调度档案：`D:\agent_workspace\tmp\agent-dispatch\<runId>\`。
- 套件根：`D:\agent_workspace\tmp\agent-dispatch\20260810-kimi-ds-paper-skill-eval50\`。
- 每题：prompt、task cwd、`report.md`、`result.json`、数据库/导出物。
- 总结：`evaluation.json`、`evaluation.md`、run/job 索引。

## 安全与项目边界

- 不修改源码，不执行 Git，不提交，不发布。
- 不记录 API key、token、Cookie 或代理凭据。
- 公网请求只通过 Academic Intelligence，尊重速率限制。
- 本轮发现的产品缺陷只记录，不由 Worker 直接修复。
