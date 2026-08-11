# 项目进度

> 更新于 2026-08-10（黑盒评测缺陷修复升级与 clean-wheel/live dogfood 完成）。本文档反映当前真实进度，与实现一致。

## 2026-08-10 — Kimi DeepSeek 50 项修复后复测（完成）

- **目标**：原样复跑上一轮 K01–K50 黑盒题集，验证修复后的产品与 Skill 在 `kimi` + `opencode-go/deepseek-v4-flash` 下的真实改善幅度。
- **可比性**：沿用上一轮 `generate_prompts.py` 中的 50 项任务定义、required observations、隐藏 oracle、每题独立 cwd/会话及禁止读取源码/tests/README/其他 docs/旧答案的隔离合同；不修改题目以迎合实现。
- **执行路线**：agent-scheduler Kimi 恒等单，显式锁定 `--model opencode-go/deepseek-v4-flash`，heavy / Auto / 不启用 Goal；已关闭 Job `job-202608091820-54c0`，共 50 个 run，49 succeeded、K44 在固定 1200 秒边界 canceled；50/50 实际路由正确，fallback 0、模型额度错误 0。
- **被测版本**：clean-wheel `academic_intelligence-0.1.0-py3-none-any.whl`，SHA-256 `a83f5d045d92d35ad4c3c1d70272ca99c84f002a78dc9c4789fd651f31b39be1`；Worker 使用隔离 venv 的 Python/CLI，并读取当前 `SKILL.md`。
- **新证据目录**：`D:\agent_workspace\tmp\agent-dispatch\20260810-kimi-ds-paper-skill-eval50-rerun1`；旧评测目录保持只读。
- **冻结结果**：**30 PASS / 6 PARTIAL / 5 FAIL / 9 BLOCKED**；严格通过率 60%，PASS+PARTIAL 72%，排除 BLOCKED 后能力通过率 73.17%。50/50 有有效 `result.json`，49/50 有 `report.md`，声明产物零缺失。离线严格 24 PASS / 4 FAIL、业务语义 28/28 成功；联网 6 PASS / 6 PARTIAL / 1 FAIL / 9 BLOCKED。
- **修复收益**：arXiv 精确 ID 切片从上一轮 2/5 提升为 **5/5 PASS**；K09 BERT 仅返回 `1810.04805v2`、K10 ResNet 仅返回 `1512.03385v1`、K12 GPT-4 仅返回 `2303.08774v6`，宽泛 mention-search 缺陷已真实消失。
- **严格分数噪声**：K05/K24/K34/K39 的产品行为均成功，但 Worker 选择的嵌套字段名/类型与隐藏 oracle 不同；不修改冻结 oracle，官方仍记 4 FAIL。仅作诊断的语义归一化结果为 34 PASS / 6 PARTIAL / 1 FAIL / 9 BLOCKED。
- **剩余问题**：17 项出现上游学术 API 限流；K06/K50 均因 OpenAlex 429 BLOCKED。K44 发现 CLI `--sources all` 实际回落到 Config 默认三源、未包含 PubMed；定向 PubMed 可成功持久化，但随后 expand 未在 1200 秒内完成。Skill 的“一次外层恢复”仍被 K06/K17 等任务不一致执行，需代码级预算约束。
- **评测器观察**：读取 `run-index.json` 与 Windows 原子替换发生一次竞态，导致本地 K44 索引 `PermissionError(13)`；scheduler run 不受影响，原始记录与调度真相已固化到 `dispatch-anomaly.json`，最终索引按 scheduler reconciled。
- **证据**：完整冻结判定、路由/额度/产物/时长审计、基线逐项对比与修复建议见复测目录下 `final-report.md`、`evaluation.json`、`route-audit.json`、`comparison.json`。

## 2026-08-10 — 黑盒评测缺陷修复升级（完成）

- **目标**：修复裸 arXiv ID 被降级为全文搜索、实际 HTTP retry 元数据未进入 `SourceFailure`，并补齐 Skill 对外层重试、公开 API 与 graph snapshot 的执行合同。
- **根因**：Collector 只识别 DOI/OpenAlex W-id，完整 arXiv ID 落入 `search_papers(all:<id>)`；arXiv exact adapter 未严格校验输入/响应且旧式 ID 丢 archive 前缀；RetryHandler 知道终止 attempt 但未附着元数据，adapter 包装后 `SourceFailure` 又只查看最外层异常；旧 Skill 未规定有限外层重试与 PARTIAL/BLOCKED 停止条件。
- **批准设计**：采用 Collector 精确路由 + arXiv 返回值校验 + RetryHandler 终态标注 + SourceFailure 有界 cause-chain 补缺，不引入通用路由框架或新依赖。设计见 `docs/superpowers/specs/2026-08-10-arxiv-routing-retry-observability-design.md`。
- **实施结果**：完整现代/旧式 arXiv ID、可选版本、`arXiv:` 前缀和 `/abs/`/`/pdf/` URL 只路由 capable exact adapter；包含 ID 的自然语言仍走全文搜索；adapter 用 `id_list` 且只接受 canonical ID 匹配；实际 3 次 HTTP 调用现在报告 `retry_count=2`、`http_status=429` 并保留 `retry_after`。Skill/README/API/architecture/decisions 已同步公开签名、snapshot schema 与有限重试合同。
- **TDD 证据**：arXiv 回归先得到 8 failed / 1 passed，最小实现后 9/9；重试链先复现 `retry_count=0`，修复后 2/2；Skill 合同先缺 12 个 token，补齐后通过。最终 `tests/test_fix_ag.py` 12/12。
- **全量门禁**：`python -m pytest -q` → **866 passed in 266.09s**，覆盖率 **92%**；Ruff 0；mypy 0（42 source files）；`mkdocs build --strict` 成功。相关异常/重试回归 119/119。
- **clean-wheel**：`pip wheel --no-deps` 隔离构建 `academic_intelligence-0.1.0-py3-none-any.whl`（198,693 bytes，SHA-256 `a83f5d045d92d35ad4c3c1d70272ca99c84f002a78dc9c4789fd651f31b39be1`）；全新 venv 安装依赖后 `pip check`、`ai --version`、`ai --help` 与仓库外离线探针通过。证据目录：`D:\agent_workspace\tmp\projects\paper-research-crawler\fix-ag-wheel-20260810`。
- **live dogfood**：无外层重试、单次 25s 上限下，`1810.04805` → BERT `v2`、`1512.03385` → ResNet `v1`、`2303.08774` → GPT-4 `v6`，3/3 精确返回且无无关 mention-search 记录。
- **工作区**：用户批准在当前普通 `master` 脏工作树原地实施；新 worktree 会丢失旧 HEAD 之后尚未提交的当前实现。只定点编辑目标文件，不覆盖无关改动，不执行 Git commit。
- **下一步**：本轮无阻塞项；后续可用同一 50 任务集复测代理行为改善幅度。

## 2026-08-10 — Kimi DeepSeek Skill 50 项黑盒评测（完成）

- **目标**：仅向 `kimi` + `opencode-go/deepseek-v4-flash` 提供 `SKILL.md` 和干净 wheel 入口，以 50 个独立 run 验证文档驱动的真实 API/CLI 可操作性。
- **隔离**：一题一会话/一 cwd；禁止 Worker 读源码、tests、README、其他 docs 或旧答案，禁止直接 HTTP/搜索、Git、mycli 和下级 Agent；不允许模型回退。
- **设计**：`docs/superpowers/specs/2026-08-10-kimi-ds-skill-blackbox-eval-design.md`。
- **计划**：`docs/superpowers/plans/2026-08-10-kimi-ds-skill-blackbox-eval.md`。
- **执行路线**：最终 50 项全部到达调度终态；因 K02 格式续跑与 K27/K45 无效夹具纠偏，共 53 个 run。53/53 个 run、最终选中的 50/50 个 run 均为 `kimi` + `opencode-go/deepseek-v4-flash`，无 fallback、无模型额度错误；调度 Job `job-202608091611-cb08` 已关闭。
- **最终结果**：**31 PASS / 6 PARTIAL / 3 FAIL / 10 BLOCKED**。严格通过率 62%；PASS+PARTIAL 可用结果率 74%；排除外部阻塞后的能力通过率 77.5%。28 个离线任务 28/28 PASS。
- **真实缺陷**：裸 arXiv ID 精确查询会混入宽泛搜索结果。K09 BERT 返回 4 条、K10 ResNet 返回 4 条、K12 GPT-4 报告返回 2 条；三题首条目标记录正确，但存在无关结果，独立 oracle 均判 FAIL。
- **外部限制**：OpenAlex 持续 HTTP 429，Semantic Scholar 部分 429，造成 6 项 PARTIAL、10 项 BLOCKED；其中 9 项在 Worker 长时间重试后超时，K23 主动形成 BLOCKED 结果。K06/K50 均无正式结果，重复一致性不可判定。
- **Skill 改进点**：补充模型/消歧器/图 API 的精确 import 与签名、snapshot 严格 schema；明确联网任务最大重试预算和 PARTIAL/BLOCKED 停止条件；进一步核对 `SourceFailure.retry_count` 的可观测性。
- **质量控制**：合同回归 4/4 通过；评测脚本编译通过；50 个任务目录、50 个选定 run、41 份正式结果和全部声明产物已独立审计。完整证据位于 `D:\agent_workspace\tmp\agent-dispatch\20260810-kimi-ds-paper-skill-eval50`。
- **源码影响**：本轮没有修改产品源码；只新增/修正评测工具、评测证据和本文档；未执行 Git commit。

## 2026-08-09 — Codex 第 5 轮全面审查、修复与隔离 dogfood（完成）

- **审查范围**：安全错误卫生、异步生命周期与取消语义、双存储一致性、作者身份融合、图快照可信度、CLI 自动化契约、CSV/Parquet 导出、安装包依赖和项目卫生。
- **关键修复**：HTTP 密钥全链路脱敏；并发 `connect()`/cache single-flight 取消安全；JSON 同目录单进程写者排他；SQLite 结构化关键词、Unicode 兴趣、稳定 citation ID 与可撤销合著边；作者消歧接入多源主流水线并对冲突 authority ID fail-closed；graph snapshot 严格计数/重复边校验；直达 `paper`/`author`/`author-papers`/`update --author` 命令和可靠退出码；稳定 Parquet schema、Excel-safe CSV、可选依赖错误降噪。
- **打包 dogfood**：从 wheel 在不继承系统包的全新虚拟环境安装；由此发现并修复 CLI 直接导入 `click` 却未声明直接依赖的问题；`pip check`、CLI 入口/帮助、SQLite/JSON、graph snapshot、CSV/JSONL 和 Parquet 缺依赖降级均通过。
- **新增回归**：5 个 hardening 测试文件共 **37 tests**，覆盖上述错误路径和安装元数据契约。
- **最终门禁**：`python -m pytest -q` → **854 passed in 253.30s**，总覆盖率 **92%**（CLI **82%**）；Ruff 0；mypy 0（42 source files）；`git diff --check` 无错误；MkDocs strict 通过。
- **边界**：JSON 后端只保证同一 Python 进程内单写者；跨进程并发写必须使用 SQLite。未删除或反跟踪历史生成物，未执行 Git commit。

## 2026-08-09 — Codex 升级轮第 4 轮（完成）

- **当前目标**：实施审查报告 Top 8~10：结构化来源失败与能力表、可持久化 graph snapshot、稳定 cursor 查询与 CSV/JSONL/可选 Parquet 流式导出。
- **约束**：全程 TDD；保持既有 API/语义兼容；不执行 Git；最终运行全量 pytest、Ruff、mypy。
- **已完成**：`SourceFailure` 与来源能力契约；graph version-1 原子 snapshot 与跨进程 CLI；SQLite/JSON 稳定 `(sort_key,id)` cursor；CSV/JSONL 分批逐行导出、Parquet 懒导入；README/SKILL/API/CLI/架构/决策文档同步。
- **测试结果**：新增 `tests/test_upgrade_top8_10.py` 17 条测试；聚焦 `17 passed in 22.39s`；兼容修复后聚焦 `23 passed in 18.73s`；全量 `817 passed, 1 warning in 726.41s`，覆盖率 92%；Ruff 0；mypy 0（42 source files）；`mkdocs build --strict` 成功。
- **下一步**：无阻塞性工作；按需安装 `academic-intelligence[export]` 使用 Parquet。
- **阻塞/风险**：全量测试有 1 条既有/偶发 `aiosqlite` worker 在 event loop 关闭后回调的 pytest warning（来自 `test_fix_aa.py::test_aa2_get_stats_excludes_absolute_paths`），不影响 817 条通过，未在本轮扩展范围处理。

## 项目文件地图

```
├── README.md                          # 项目说明（v2 对齐）
├── SKILL.md                           # Skill 契约文档（v2 对齐，B6 执行契约）
├── pyproject.toml                     # 项目配置（依赖、脚本、工具）
├── mkdocs.yml                         # MkDocs 文档站点配置
├── docs/
│   ├── superpowers/specs/
│   │   └── 2026-07-26-technical-design-v2.md  # 3A 技术设计基线（§17 验收条件）
│   ├── progress.md                    # 本文档
│   ├── decisions.md                   # 关键决策记录（含 B5 追加的 4 条）
│   ├── index.md / getting-started/* / user-guide/* / api/* / development/*
│   └── (api.md / architecture.md / features.md 等为模板占位页，mkdocs exclude)
├── academic_intelligence/             # 核心库
│   ├── __init__.py                    # 主入口（AcademicIntelligence 门面）
│   ├── cli.py                         # CLI（含 expand/export snapshot、export-papers）
│   ├── exporters.py                   # CSV/JSONL/可选 Parquet 分批导出
│   ├── core/
│   │   ├── models.py                  # Pydantic 数据模型 v2（Evidence/AuthorRef/Paper/Author/Citation/…）
│   │   ├── exceptions.py              # 异常层次（含 AllSourcesFailedError.failures）
│   │   ├── constants.py               # 常量
│   │   └── types.py                   # SourceType / Config / AntiCrawlStrategy
│   ├── sources/                       # 6 源适配器（全部接线）
│   │   ├── base.py                    # BaseSource 抽象基类
│   │   ├── arxiv.py / openalex.py / semantic_scholar.py / pubmed.py / ieee.py / google_scholar.py
│   ├── collectors/
│   │   └── base.py                    # BaseCollector / MultiSourceCollector
│   ├── processors/                    # 处理层
│   │   ├── deduplicator.py            # 去重融合（并查集传递闭包 + ID 交叉 + SequenceMatcher）
│   │   ├── disambiguator.py           # 作者消歧（ID 直连 + 启发式特征聚类）
│   │   ├── scorer.py                  # 置信度评分（基线表 + 多源加成 + 字段调整）
│   │   ├── enricher.py                # 信息增强
│   │   ├── validator.py               # 数据校验
│   │   └── incremental.py             # 增量变更检测与合并
│   ├── graph/                         # 图谱层（纯 Python，无 networkx）
│   │   ├── knowledge_graph.py         # KnowledgeGraph（节点/边 + LRU + versioned snapshot）
│   │   ├── traversal.py               # expand_from_graph（懒加载 BFS + 占位节点 + 截断）
│   │   └── cache.py                   # GraphCache（OrderedDict LRU）
│   ├── storage/
│   │   ├── base.py                    # BaseStorage 接口
│   │   ├── sqlite_store.py            # SQLite（SQLAlchemy 2.0 async + aiosqlite；9 表 + 3 索引表；v2 列自动迁移）
│   │   └── json_store.py              # JSON 目录后端（可选）
│   └── utils/                         # 工具层（保留 httpx 实现，见 decisions.md D-2026-08-07-1）
│       ├── http.py / proxy.py / rate_limiter.py / retry.py / cache.py
└── tests/
    ├── test_upgrade_top8_10.py        # 第 4 轮 17 条 TDD/CLI/分页/导出测试
    ├── unit/…（test_models_v2.py、test_b2_*.py、test_b4_*.py、test_graph_*.py、test_disambiguator.py 等）
    ├── integration/
    │   ├── test_end_to_end.py         # 端到端（cassette 重放）
    │   ├── test_sources.py            # 各源集成（cassette 重放）
    │   └── test_acceptance_v2.py      # §17 六条验收集成测试（B5 新增）
    ├── cassettes/                     # VCR 式 JSON cassette（离线重放）
    └── cassette_replay.py             # cassette 匹配/重放助手
```

## 关键决策记录

已迁移至 `docs/decisions.md`（按时间倒序）。B5 追加的 4 条决策：

- **D-2026-08-07-1**：抓取层保留 httpx 实现，Scrapling 不做强制迁移
- **D-2026-08-07-2**：作者消歧为独立阶段，不替换名字去重
- **D-2026-08-07-3**：全源失败保留 AllSourcesFailedError
- **D-2026-08-07-4**：图谱层不引入 networkx

## 当前进度

- **阶段**：B1~B6 已完成；此后经 **34 轮 dogfood + 33 批修复（FIX-A~AF）**、多轮 Codex 全面审查/升级，以及本轮 FIX-AG 精确路由与重试可观测性修复，至 **866 passed**。详见 `docs/agent-eval/capability-boundaries.md` 与本文顶部最新记录。
- **测试**：**866 passed**（基线 124 → 866），覆盖率 **92%**，完整离线套件可跑（cassette 重放 / mock，不依赖公网）。
- **覆盖率阈值**：§17.6 要求 ≥ 80%，当前 92%（验收标准要求 ≥ 88%）。
- **静态门禁**：ruff 0 errors / mypy 0 errors（42 files），Codex 轮清零。

## 模块完成清单

| 模块 | 状态 | 说明 |
|------|------|------|
| core/models.py | ✅ | 模型 v2 完整：AuthorRef / evidence_list / arxiv_id / pmid / fields_of_study / references / citations_list / Author 消歧字段 |
| core/types.py | ✅ | Config v2 字段（图谱/消歧/增量/置信度） |
| core/exceptions.py | ✅ | 异常层次 + AllSourcesFailedError.failures |
| sources/（6 源） | ✅ | arXiv / OpenAlex / Semantic Scholar / PubMed / IEEE / Google Scholar 全部接线 |
| collectors/ | ✅ | 多源并发编排：fetch → cross_validate → dedup → enrich → validate |
| processors/deduplicator.py | ✅ | 并查集传递闭包 + ID 交叉 + SequenceMatcher 标题 + 加权模糊 |
| processors/disambiguator.py | ✅ | ID 直连 + 启发式聚类（auto/ambiguous/confirmed） |
| processors/scorer.py | ✅ | 基线表 + 多源加成 + DOI/PDF/陈旧调整 |
| processors/incremental.py | ✅ | 增量 stale 门控 + 字段级变更检测 |
| processors/enricher.py / validator.py | ✅ | 信息增强 / 校验 |
| graph/ | ✅ | KnowledgeGraph / expand / subgraph / path / snapshot(跨进程 CLI) / CLI expand/export |
| storage/sqlite_store.py | ✅ | papers/authors/citations/authorships/coauthorships/evidence/paper_hashes/source_updates/entity_sync 9 表 + paper_author_tokens/paper_author_names_fts/paper_text_fts 3 索引表 + v2 列迁移 |
| storage/json_store.py | ✅ | JSON 单进程小数据后端；原子 `store.json` 快照 + 兼容镜像；citation/coauthorship 幂等 |
| utils/ | ✅ | http / proxy / rate_limiter / retry / cache（保留，见 D-2026-08-07-1） |
| cli.py | ✅ | `ai collect author/paper/citations` / `ai query` / `ai stats` / `ai expand` / `ai export` / `ai export-papers`(csv/jsonl/parquet) |

## 验收条件（3A v2 §17）与集成测试映射

| §17 验收 | 集成测试 | 结果 |
|---------|---------|------|
| 1. `ai paper "10.1038/nature14539"` 返回完整论文信息 | `test_acceptance_01_paper_lookup_returns_full_info` | ✅ |
| 2. OpenAlex + S2 去重为一条记录 | `test_acceptance_02_multi_source_dedup_single_record` | ✅ |
| 3. evidence 表两条记录 + confidence 正确 | `test_acceptance_03_evidence_two_rows_and_confidence` | ✅ |
| 4. `ai author "Geoffrey Hinton"` 返回机构/h-index/论文数 | `test_acceptance_04_author_profile` | ✅ |
| 5. SQLite 可独立打开，schema 与设计一致 | `test_acceptance_05_sqlite_schema_and_independent_open` | ✅ |
| 6. 全量测试通过，覆盖率 ≥ 80% | `test_acceptance_06_coverage_requirement` + 覆盖率报告 | ✅ 91% |

## 剩余工作

- 无阻塞性剩余工作。可选后续: 作者列表内姓名变体归并（T1/T4 实测 "Geoffrey Hinton" 与 "Geoffrey E. Hinton" 未归并，属 AuthorRef 列表级融合增强）、`Author.paper_count` 字段（设计 v2 §4.1，当前由收集列表推导）、`ai update` CLI（当前仅 API 层）、`ai query authors` CLI、适配器 evidence 置信度与 scorer 基线表统一（观察点，非阻塞）。
- 已知项: muse-spark-1.2-contributor 线路当日多次 API 故障；claude-sonnet-5 需 Pro 订阅；GLM-5.2 按用户指示未使用。B6 复核以 ds-flash 独立会话完成。

## 测试结果

- **Codex 第 2 轮升级后**：`python -m pytest -q` → **795 passed in 648.02s**，覆盖率 **92%**（6719 statements / 534 missed）；`python -m pytest -q --no-cov --durations=20` → **794 passed, 1 skipped in 466.72s**（无 coverage 时覆盖率验收用例按设计跳过）。`ruff check academic_intelligence` 全绿；`mypy academic_intelligence` → `Success: no issues found in 41 source files`。
- **新增契约**：增量匹配保留本地 ID；JSON citation pair upsert；coauthorship 由当前 authorships 重算；`rate_limit` / `max_concurrent_requests` / `author_refresh_days` / `enable_google_scholar` 全部接线；JSON/Cache 原子、非阻塞持久化；Cache per-key single-flight；门面 close best-effort。
- **测试分层**：10k/100k 规模测试标记 `performance` + `slow`；开发快跑命令为 `pytest -m "not slow and not boundary and not performance"`。性能断言仅在 coverage tracer 下使用 2× 仪器开销预算，无 coverage 时仍保留原 5s 门禁（两条聚焦测试 `2 passed in 5.66s`）。

- **B5 前**：`364 passed`，覆盖率 90%（line-rate 0.8958）。
- **B5 后**：`370 passed`（新增 6 条 §17 验收集成测试），覆盖率 90%。
- 全部离线：网络标记测试使用 `tests/cassettes/` JSON 重放，不依赖公网。

## Agent 测试任务集执行结果（B6）

`docs/agent-eval/task-suite.md` 真实派单（原生CLI单，agent 仅凭 SKILL.md/README 通过模块 API/CLI 完成真实调研）:

| 任务 | 内容 | 判定 | 核验要点 |
|------|------|------|---------|
| T1 | DOI 调研 10.1038/nature14539 | **PASS** | Deep learning / LeCun·Bengio·Hinton / 2015 / Nature / 引用 82963 / evidence 2 条（pubmed+openalex） |
| T1 复核 | 独立会话重做 T1 | **PASS** | 结果与首次一致（含作者融合瑕疵复现） |
| T2 | 标题调研 Attention Is All You Need | **PASS** | Vaswani 等 / 2017 / arXiv 1706.03762 通路验证；S2 429 软失败不阻塞 |
| T3 | Geoffrey Hinton 作者调研 | **PASS** | h-index 138 / 引用 452327 / 48 篇论文 / 代表作含《Deep learning》 |
| T4 | 多源交叉验证 + 去重 | **PASS** | 3 源→1 条，evidence 2 条，confidence 1.0（max 基线 0.92 + 0.05 加成 + DOI 0.05 封顶） |
| T6 | 存储 + 查询 + stats | **PASS** | persist→query（keyword/year）查回 1 条，stats total_papers=1 与入库一致 |

- required 任务 5/5 PASS（100%），无 FAIL，满足 AC-11（PASS ≥80%、无 FAIL）。
- T1 双独立执行（两个 ds-flash 会话）结果一致，满足"至少 1 个任务交叉复核"。
- 双模型复核说明: muse-spark 线路当日持续故障（4 次失败 1 次成功）、claude-sonnet-5 需 Pro 订阅、GLM-5.2 按用户指示不使用；最终复核由 ds-flash 独立会话完成（用户指示）。

---

## Crawler Upgrade 2026-08（2026-08-11 完成）

### 交付内容

- **CLI 重构**：`ai` → `paper`（`ai` 保留 shim，打印 "renamed to paper" exit 2）；新增 `paper source <源> <操作>` 子命令树 + 11 源注册机制
- **5 个新免费源**：Crossref（含出版社映射 publisher_map）、Unpaywall（合法 OA 定位）、Europe PMC（元数据+OA 全文）、OpenCitations/COCI（引用图，DOI 键）、CORE（元数据+OA 全文）
- **网页爬取层**：webcrawler/（curl_cffi 可选 TLS 指纹 + Trafilatura 抽取 + Scrapling 可选 + robots 预检 + 反检测红线 blocked）
- **全文管线**：fulltext/（Unpaywall→CORE→arXiv→Europe PMC 合法 OA 定位 → pdfplumber 解析 → 段落入库 full_text 表）
- **预算管理**：budget/（precheck/metered 分级语义 + fail-soft + budget_usage 表 + `paper budget`/`sources status`）
- **作者身份解析**：identity/（resolve/profile/search/confirm + author_identity_global 跨论文复用表 + `paper author` CLI）
- **OpenAlex 降级**：限量实时 + fail-soft（免费快照 Phase 2）

### 验证结果

- 全量离线测试：**1177 passed**（升级前 854 → +323），覆盖率 90%
- 三份设计文档经 kimi-k3-256k 审核（2 Critical/10 Important/16 Minor 全部修订关闭）
- ds 审核实现批次：合规红线专项 **全部 PASS**（付费墙/盗版/过盾/自存档边界/robots）
- 单源功能测试 7 单：4 全 PASS + 3 处缺陷修复后重验（E4 定位器加 europe_pmc、C4 未命中 exit 2、E1 CLI 别名）
- **最终全样例用户验收（ds-flash 真实用户）**：required **26/26 PASS**，recommended 6 PASS + D15 双会话复核 3/3 PASS（Q2/Q6/Q7 关键事实一致）
- 差异记录（均不构成 FAIL）：T5 web crawl 无 --persist（输出 JSON 承担）、T6 跨 ID 融合在多源 collect 入口、Q5 用 source citations 等价链、T7 预算经 Config 注入

### 已知限制/风险

- Unpaywall 需配置 `UNPAYWALL_EMAIL`（本人邮箱，服务端强制）才能完整使用；CORE 建议注册免费 `CORE_API_KEY`（公共 tier 限流频繁）
- OpenAlex 同名聚合噪声（如 A5110986785 混入非 DeepSeek 论文）属外部源数据质量，身份判定以著作列表含目标论文为准
- Google Scholar 适配器保留但默认不注册（ToS 红线）；`docs/api.md` 等历史文档仍有 `ai` 引用待后续对齐（不在本次范围）
- 付费墙正文/Sci-Hub/GS 自动化全程未触碰（红线）

---

## 信息反向挖掘（Trace）2026-08-11 完成

### 交付（spec: docs/superpowers/specs/2026-08-11-trace-design.md）

- **3 个 CLI 挖掘原语**（固有工具，不做判断）：
  - `paper trace-citing <seed>` — 反向引用（OpenAlex cites: + OpenCitations 双源、分页/断点/429 退避、--limit）
  - `paper trace-authors` — 作者机械展平（保留原始、不做合并；--affiliation-filter 子串过滤）
  - `paper trace-profiles` — 批量画像（OpenAlex 作者档案 + top_works；无 ID 返回占位不自动匹配）
- **SKILL.md §11 信息反向挖掘方法论**（6 步工作流 + 消歧规则 + 信息获取边界）
- **docs/api-direct.md**（官方 API 直连手册：端点/分页/限速/实测坑）
- 消歧与头衔核验**不做进 CLI**（agent 方法论 + webfetch 官方源）

### 验证

- 全量离线测试：**1259 passed**（+82），零回归
- ds 审核：4/5（无 Critical）；4 Important + 4 Minor 全部修复（I-1 链式工作流信息丢失、I-2 limit 静默跳源、I-3 跨源去重键、I-4 文档端点笔误）
- **dogfood PASS**（ds-flash 真实用户）：40 引用（可续拉 776）→ 441 作者 92% 带 ID → 过滤 10 人 → 90% 画像特征 → 4 组同名消歧 + 2 人头衔官方核验 → 10 行画像 CSV（作者/机构/领域/代表作/venue/时间/头衔/来源链）

### 已知改进点（非阻塞，dogfood 建议）

- trace-authors 无 ID 作者占位行可附缺失原因；--affiliation-filter 机械子串过滤易误解（文档已提示）；同人分 ID 需方法论层消化；OpenAlex 档案机构噪声
