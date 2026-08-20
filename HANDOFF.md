# HANDOFF — Academic Intelligence 项目交接文档

> 给接手的 agent：本文档让你零上下文快速接手。先读本文，再按需深入 README / SKILL.md / docs/。

## 1. 这是什么

**academic-intelligence**（CLI 命令 `paper`）——多源学术数据采集/融合/知识图谱 Python 库 + CLI。
从 11 个免费学术源（arXiv/OpenAlex/Semantic Scholar/Crossref/PubMed/IEEE/Unpaywall/Europe PMC/OpenCitations/CORE）采集论文、作者、引用，带证据链、去重融合、置信度评分、增量更新、全文 OA 管线、作者身份解析、反向引用挖掘（trace）、OpenAlex 快照等。

- **本地**：`D:\agent_workspace\projects\paper-research-crawler`
- **GitHub（公开）**：`https://github.com/chisado233/academic-intelligence`
- **Skill 契约**：项目根 `SKILL.md`（这是 agent 使用本工具的唯一入口文档，含方法论）
- **技术栈**：Python ≥3.11，pydantic v2 / SQLAlchemy async / httpx / typer / rich；纯库无 web server

## 2. 快速上手（已独立验证可跑通）

```bash
cd <克隆或本地路径>
python -m venv .venv && .venv/Scripts/activate   # 隔离环境
pip install -e ".[dev]"                           # 开发安装（hatchling editable）
pip check                                         # 依赖无冲突
paper --version                                   # CLI 可用
python -m pytest -q --no-cov -m "not slow and not performance"   # 快跑测试（离线）
```

**门禁标准**（改动后必跑）：
```bash
python -m pytest -q                       # 全量：1357 passed / 覆盖率 90%（约 12 分钟）
ruff check academic_intelligence          # 0 errors
mypy academic_intelligence                # 0 errors
```
> 注意：系统 `py` 启动器可能指向损坏的 Python 3.11，用 `python` 3.12 即可。

## 3. 当前状态（HEAD `1a565e5`）

**最近已完成的工作链（2026-08-13/14）**：
1. **跨实体归属检测**：`paper author profile` 会扫描 OpenAlex 同名实体，检出"署名机构与实体主机构冲突"的疑似漏检作品（`entity_flags` 警告区块）——修复了 MBLLEN 被挂错实体的真实缺陷
2. **SKILL.md 改进**：多源作品清单强制规则（§11.1）、GS 引用数合规获取技巧（§11.4）、种子校验
3. **学术资历查询方法论**：`docs/titles-source-map.md §0.5`（职称/Fellow/编委/会议主席/海外经历等非 API 信息的查询方法论，经 kimi-k3-256k + codex 双模型评审后重写）
4. **验证报告**：`docs/agent-eval/verify-20260813.md`（七轮 A/B 实验完整记录）

**验证结论**：skill 改进后，陌生 agent 用 SKILL.md 完成任务从 0/3 全错收敛到连续 4 轮 3/3 全对；资历查询 12 run 全对且产出可审计。

## 4. 关键文件地图

| 路径 | 用途 |
|---|---|
| `SKILL.md` | **agent 使用契约**：所有命令/方法论/红线（接手第一优先读）|
| `docs/titles-source-map.md` | 学术资历查询方法论（§0.5）+ 头衔源地图（§1/§3）|
| `docs/agent-eval/verify-20260813.md` | 七轮 A/B 验证报告（含结论与证据路径）|
| `docs/decisions.md` / `docs/changelog.md` | 决策记录 / 变更日志 |
| `docs/superpowers/specs/` | 各功能设计规格 |
| `academic_intelligence/identity/` | 作者身份（锁人/消歧/跨实体检测，最近改动区）|
| `tests/test_fix_entity_flags.py` | 跨实体检测测试（13 用例，离线）|

## 5. 核心约定（不可违反）

- **合规红线**：不碰付费墙正文、不集成 Sci-Hub/libgen、**不自动化爬 Google Scholar**、不破解验证码/过盾、尊重 robots.txt
- **作者消歧**：2026-08-12 起采集管道**不做自动消歧合并**（中文场景实测不可靠），只返回原始作者 + evidence，判断交 agent/人工（`paper author confirm` 是人工确认口）
- **不信任单一数据源**：作品清单必须多源取并集（OpenAlex + S2 + Crossref ORCID + DBLP）；"最高被引"结论 ≥2 引用口径交叉
- **GS 引用数**：S2 `citationCount` 是**自建口径**（非 GS 数据）；GS 精确值靠 WebSearch 快照（带日期）或人工，绝不爬
- **git**：commit 权归主 agent；push 到 GitHub 需用户明确同意（仓库已公开）
- **评测方法**：A/B 派单用 agent-scheduler（`mycli agent-cli run kimi --model opencode-go/deepseek-v4-flash` 或 `OvO/deepseek-v4-flash`），对照组禁 skill / 实验组用 SKILL.md，每组 3 并发，90 分钟超时

## 6. 已知遗留（不阻塞，接手可查）

1. `test_acceptance_04_author_profile` 在多次改动前即失败（S2 身份融合 cassette 问题，与最近改动无关，未修）
2. 对照组无 skill 的 agent 常陷入无边界穷举导致 90 分钟超时（非 skill 缺陷）
3. OvO 渠道比 opencode-go 慢（含任务难度因素，未做严格同任务对照）
4. 2024 年起杰青完整名单不公开，头衔核验依赖官方个人页/高校官网（外部限制，方法论已注明）

## 7. 常用命令速查

```bash
paper author profile <id>              # 作者档案（含跨实体检测）
paper author search "<名>" --disambiguate   # 同名候选
paper collect author "<名>" --sources openalex,ss --persist
paper trace-citing <doi> --sources openalex,opencitations -o citing.csv
paper source semantic_scholar get <doi>       # S2 引用数交叉
paper sources status / paper budget    # 源能力/预算
paper fulltext <id> --persist          # 合法 OA 全文
```

## 8. 接手建议

- 新任务先读 `SKILL.md`（agent 使用方法论全在里面）+ `docs/titles-source-map.md §0.5`（涉及资历类查询时）
- 改代码走 TDD（项目测试风格：fake fetcher/mock，离线可跑）；改完跑第 2 节门禁
- 改 skill 方法论 → 可复用第七轮 A/B 流程验证效果（`tmp/agent-dispatch/20260814-credentials-ab-r7/` 有完整示例）
