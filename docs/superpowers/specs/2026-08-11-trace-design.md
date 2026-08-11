# 信息反向挖掘（引用者画像）设计

> 日期：2026-08-11　状态：设计基线 v1.0（待用户复审）
> 项目：Academic Intelligence（`projects/paper-research-crawler`）
> 定位：本设计的**产品初衷**是信息反向挖掘：一篇论文 → 被哪些论文引用 → 引用者是谁 → 完整画像（身份/作品/期刊会议/时间线/头衔）。

## 1. 目标

输入一篇种子论文，输出一份**引用者画像**：

- 谁引用了它（反向引用）
- 这些引用者中，哪些是特定身份（杰青/院士/长江学者等头衔）
- 每人完整画像：身份信息、全部论文、发表期刊/会议、时间线、头衔

## 2. 架构总则（三块分工）

| 块 | 职责 | 形态 |
|---|---|---|
| **CLI 固有工具** | 确定性、可批量、API 能给出的结构化操作 | 3 个挖掘原语 |
| **SKILL.md 方法论** | agent 执行手册：工作流 + 消歧方法论 + API 直连手册 + 头衔核验 | 文档 |
| **agent 编排** | 用 CLI 拿数据 → 按方法论判断/搜索 → 融合输出 | agent 能力 |

**关键决策**：
- **消歧不做进 CLI**——同名判断是语义判断，交给大模型方法论（机构/ID/代表作/合著者特征对比），CLI 只给特征原料
- **头衔核验走 agent web fetch**（衔接 `docs/titles-source-map.md`）
- **分析产物不落主库**（CSV/JSON 文件即产物）；数据库降级为可选缓存

## 3. 信息获取边界（明确约束）

| 信息 | 来源 | 说明 |
|---|---|---|
| 论文/引用/机构/作品/venue/年份/引用数/h-index/领域/作者 ID | **CLI（API）** | 结构化、可批量 |
| 中文姓名对照（拼音→中文名）| **agent 联网搜索** | API 只有拼音 |
| 个人主页/最新动向/单位介绍 | **agent 联网搜索** | 非结构化信息 |
| 头衔（杰青/院士/长江）| **agent 联网搜索**（官方源）| titles-source-map |
| 消歧判断（同名是否同一人）| **agent 方法论** | CLI 只给特征 |
| 画像融合（多方信息合成）| **agent** | CLI 数据 + 搜索数据合并 |

**原则**：CLI 提供"API 能确定性给出的部分"；API 没有的（中文名、头衔、主页、最新动态）一律 agent 联网搜索确认；CLI 不假装提供拿不到的信息。

## 4. CLI 挖掘原语（3 个固有工具）

### 4.1 `paper trace-citing <paper-id|DOI>`

反向引用拉取（谁引用了种子论文）。

```bash
paper trace-citing 2501.12948 --sources openalex,opencitations --output citing.csv
```

- OpenAlex `filter=cites:Wxxx` 分页（cursor 从 `*` 开始，per-page 200）
- OpenCitations `citations/{doi}` 双源交叉（COCI 以 DOI 为键，`citations/{doi}` 是"谁引用了它"的被引侧端点；`references/{doi}` 返回的是该论文自身引用了什么，方向相反）
- 输出列：citing_paper_id / doi / title / year / venue / authors_raw / authors_detail（compact JSON，OpenAlex authorships 原样保留 author.id / display_name / institutions，供链式下游使用）
- 内置：429 退避、断点续传（`--resume-from <last-id>`）、进度输出
- 单次上限可配（`--limit`，默认无上限但有安全阈值提示）

### 4.2 `paper trace-authors <citing.csv|paper-id>`

作者展平——把引用论文的作者全部摊开，**保留原始，不做合并**。

```bash
paper trace-authors citing.csv --output authors.csv
```

- 输出列：author_name / appears_in(papers) / affiliation(署名机构) / author_id(如有)
- **不做同名合并、不做消歧判断**——只给原始数据行
- 可选 `--affiliation-filter <关键词>`：按署名机构粗筛（工具性过滤，非判断）

### 4.3 `paper trace-profiles <authors.csv> --output profiles.csv`

批量画像——为 agent 消歧/筛选提供特征原料。

```bash
paper trace-profiles authors.csv --output profiles.csv
```

- 按 author_id（如有）或名字拉 OpenAlex 作者档案：机构/h-index/领域/作品数
- 代表论文（按引用排序，含 title+venue+年份）
- 输出列：author_name / author_id / institution / h_index / fields / works_count / top_works(JSON)
- 限速礼貌（1 rps）；大批量支持分批（`--batch-size`）

**设计约束**：三个原语都是"确定性工具"——不做判断、不自动消歧、不自动核验头衔；输出 CSV/JSON（分析产物，不落主库）。

## 5. SKILL.md 方法论（agent 执行手册，新增章节）

### 5.1 挖掘工作流（6 步）

```
① 定位种子论文（paper source arxiv|openalex get <id>）
② 反向引用（paper trace-citing → citing.csv）
③ 展平作者（paper trace-authors → authors.csv）
④ 画像特征（paper trace-profiles → profiles.csv）
⑤ 消歧（方法论判断：见 5.2）
⑥ 头衔核验（agent web fetch 官方源：见 5.3）
→ 输出最终画像 CSV（作者/机构/领域/代表作/venue/时间/头衔/来源链）
```

### 5.2 消歧方法论（agent 判断规则，不自动合并）

- **ID 直连优先**：author_id（ORCID/S2/OpenAlex）相同 → 同一人（强证据）
- 无 ID：**机构 + 领域 + 代表作品 + 合著者**特征对比
- 多候选并列展示，标注置信度（确认/疑似/存疑）
- 中文名：拼音 ↔ 中文名对照需联网搜索确认
- 规则：不硬合并；证据不足标"存疑"待人工/更多搜索

### 5.3 头衔核验（衔接 titles-source-map）

- agent 对候选集（机器粗筛后缩小到几十人）web fetch 官方源
- 交叉验证（官方库 × 高校官网 × 论文资助号），标注确认/疑似/存疑/无法验证

### 5.4 API 直连手册

- 各源端点/参数/分页/限速/认证
- 高级查询示例：`cites:Wxxx`、`author.id` + 机构过滤、批量分页
- **实测坑沉淀**：cursor 从 `*` 开始、urllib 超时、GBK 编码、同名匹配需机构+ID 双校验、429 退避

## 6. 输出与持久化

- 分析产物 = CSV/JSON 文件（citing.csv / authors.csv / profiles.csv / 最终画像.csv）
- **不落主库**（结合"数据库作用不大"判断：分析型工作流产物即输出）
- 可选 `--cache`：拉取结果缓存（避免重复 API 调用），独立于主查询流

## 7. 错误处理

- CLI 内置：分页断点续传、429 退避、限速礼貌（1 rps）、凭证从 env 读
- 单源失败 → fail-soft（另一源继续）；全失败 → 明确报错 exit 2
- 大批量：进度输出 + 可中断续跑

## 8. 合规

- 延续既有红线：不碰付费墙、不爬 GS、不过盾、尊重 robots/限速
- API 直连手册每条示例带合规注释；凭证不硬编码

## 9. 测试

- 单元：分页（cursor 正确性）、展平（原始行保留、无自动合并）、画像映射（字段正确）、断点续传
- 集成（真实场景验收）：**陆峰/北航案例**——种子论文 → trace-citing → trace-authors → trace-profiles → agent 消歧 → 头衔核验 → 画像 CSV
  - 验收点：反向引用数量与 OpenAlex 一致；作者行含机构过滤能力；profiles 为 agent 判断提供足够特征（机构/领域/代表作）
- 全量离线回归（既有 1177+ 测试不回归）

## 10. 范围外（明确不做）

- 自动消歧/自动合并（交给 agent 方法论）
- 自动头衔核验（agent web fetch）
- 主库持久化（分析产物即文件）
- 递归多层引用挖掘（本期只一层：种子论文的直接引用者；`--depth 2` 列为后续）
- 网络可视化（本期 CSV/报告即可）

## 11. 交付物清单

1. `trace.py`（或 cli 扩展）：3 个原语 + 测试
2. SKILL.md 方法论章节（工作流/消歧/API 手册）
3. `docs/api-direct.md`（API 直连手册，含实测坑）
4. 真实场景验收证据（陆峰案例画像 CSV）
