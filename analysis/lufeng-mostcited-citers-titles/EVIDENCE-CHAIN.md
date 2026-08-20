# 证据链报告：北航陆峰被引最多论文及其引用者中的杰青/院士

> ⚠️ **勘误（2026-08-17，任务完成当日复核）**：本报告§二"高置信先验"表**至少 7 条头衔与已核验事实相反**：黄庆明、马华东**均非院士**（分别是杰青 2010/2009 + IEEE Fellow）；刘家瑛、马佳义、周文罡、沈建冰、左旺孟**均非国家杰青**（无记录/湖北省杰青/优青）。另漏检吴枫 2025 工程院院士及 8 位已核验杰青（见 `analysis/lufeng-beihang-top-citers/EVIDENCE-CHAIN.md`，经独立全链路抽验）。种子结论（MBLLEN）与两条 confirmed（戴琼海院士、李厚强杰青）**有效**。教训已写入 SKILL.md §8（未核验头衔禁止写成具体断言）与 §10 要点 6/7（降级协议、勘误必读）。

1. **任务**：① 查明北京航空航天大学陆峰教授被引最多的论文；② 反查引用该论文的作者中，谁是国家杰青、谁是两院院士。
2. **查询日期**：2026-08-17（所有引用数为当日快照）
3. **方法**：本 skill（academic-intelligence）§11 信息反向挖掘六步工作流 + §10 资历查询方法论（docs/titles-source-map.md §0.5）+ §12 报告契约。全部数据采集自当日全新请求，独立任务库 `ai.db`，未读取任何历史探查记录。

---

## 一、链路总览

| 步骤 | 操作 | 命令/来源 | 产出 | 一键验证 |
|---|---|---|---|---|
| ① 锁人 | 官方主页确认身份 | `paper web crawl https://phi-ai.buaa.edu.cn/`（+members/publications 页） | `homepage.json` `lab_members.json` `lab_pubs.txt` | [实验室官网](https://phi-ai.buaa.edu.cn/) |
| ①b 种子校验 | 多源作品清单 + 双口径引用数 | S2 API（search/get）+ Crossref API（is-referenced-by-count） | `s2_mbllen.json` `s2_adv.json` `s2_agllnet.json` | [S2 MBLLEN](https://www.semanticscholar.org/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7) |
| ② 反向引用 | S2 citations 全量分页 | 库 API 脚本 `fetch_citers.py`（复用 `SemanticScholarSource._get_json`，429 有界退避） | `citing.csv`（804 篇） | [S2 citations API](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7/citations) |
| ③ 展平作者 | `paper trace-authors` | 分号规范 `authors_raw` + 嵌套 `authors_detail` | `authors.csv`（3152 行） | 本目录 `authors.csv` |
| ④ 画像 | S2 author/batch（≤1000 id/请求，共 4 请求） | `fetch_profiles_batch.py` | `profiles.csv`（3135 有档案） | 本目录 `profiles.csv` |
| ⑤ 消歧 | S2 实体拆分按名聚合 + 引用论文领域指纹 | 本地分析 | `evidence_citing_papers.json` | 本目录 |
| ⑥ 头衔核验 | 官方页 `paper web crawl`（尊重 robots） | 见下文证据卡 | `verif_*.json`（22 个页面快照） | 见证据卡链接 |
| ⑦ 报告 | 本文件 + HTML 渲染 | `scripts/render_report.py` | `EVIDENCE-CHAIN.html` | 本文件 |

---

## 二、结论表

### 结论 1：被引最多的论文 = **MBLLEN: Low-Light Image/Video Enhancement Using CNNs**（BMVC 2018）

作者：Feifan Lv, **Feng Lu\***（通讯）, Jianhua Wu, Chongsoon Lim。出处为[陆峰实验室官方出版列表](https://phi-ai.buaa.edu.cn/publications/index.htm) 2018 年条目（[SI] 标注）。

| 候选论文 | S2 引用数（2026-08-17） | Crossref is-referenced-by-count | 判定 |
|---|---|---|---|
| **MBLLEN（BMVC 2018，无 DOI）** | **804** | 不适用（无 DOI） | ✅ 被引最多 |
| Understanding Adversarial Attacks on DL-based Medical Image Analysis Systems（PR 2021） | 555 | 451 | 次席 |
| Attention Guided Low-light Image Enhancement（AGLLNet, IJCV 2021） | 333 | 281 | 第三 |

双口径交叉满足：竞争者 S2/Crossref 差异 <25%（量级一致）；MBLLEN 无 DOI，S2 为唯一可取精确口径（SKILL §11 已注明此情况），且对次席领先 45% 以上，排名稳健。GS 口径未获取（合规红线：不自动爬 Google Scholar）。

### 结论 2：引用者中的院士与杰青

804 篇引用论文 → 3152 作者行 → 3135 个 S2 作者档案（含 532 位多次引用者）。

**当日完成证据链核验（confirmed，三层证据齐全）：**

| 人名 | 头衔 | 引用 MBLLEN 的论文（示例） | 责任主体源（当日抓取） |
|---|---|---|---|
| **戴琼海**（清华大学） | **中国工程院院士**（成像与智能） | Computational Imaging and AI: The Next Revolution（Proc. IEEE 2021）；DarkVision 基准（arXiv 2301.06269, 2023） | [清华自动化系官网"两院院士"栏目](https://www.au.tsinghua.edu.cn/szdw/lyys.htm) |
| **李厚强**（中国科学技术大学） | **国家杰出青年科学基金获得者（2013）**；IEEE Fellow；长江学者特聘教授（2017） | Low-Light Video Enhancement with Synthetic Event Guidance（AAAI 2022, arXiv 2208.11014） | [USTC 官方个人主页](http://staff.ustc.edu.cn/~lihq/) |

**高置信先验 · 未能完成官方核验（当日权威源不可达，不作为事实断言，仅列核验线索）：**

| 人名 | 先验头衔 | 引用论文（示例 DOI） | 未核验原因与建议 |
|---|---|---|---|
| 黄庆明（国科大） | 中国科学院院士（2023，先验） | 10.1145/3735973 等 4 篇 | 中科院院士馆 JS 渲染、维基/百科网络不可达；建议人工查 [院士馆](https://ys.casad.cas.cn/) |
| 马华东（北邮） | 中国工程院院士（2023，先验） | 10.1109/TIP.2025.3616639 等 5 篇 | 北邮官网 DNS 失败；建议人工查 [工程院院士馆](https://www.cae.cn/) |
| 吴枫（USTC） | 国家杰青（先验）；2021 工程院增选有效候选人（[时代学者文章](https://www.shidaixuezhe.com/)线索，候选≠当选） | 10.1109/TMM.2022.3193059 | 官方页被同名者旧页劫持跳转（见陷阱 P2） |
| 刘家瑛（北大王选所） | 国家杰青（2023，先验） | 10.1145/3815421 等 5 篇 | 身份已由[王选所官网新闻](https://www.icst.pku.edu.cn/)（"王选所刘家瑛教授"）确认；杰青年份待 NSFC 源 |
| 马佳义（华中科大） | 国家杰青（先验） | 10.1109/TNNLS.2022.3190880 | 主页猜测失败 |
| 翟广涛（上海交大） | 国家杰青（先验） | arXiv 2407.12431 等 6 篇 | 主页猜测失败 |
| 查正军（USTC） | 国家杰青（先验） | 10.1145/3343031.3350994 | USTC staff 页路径未知 |
| 周文罡（USTC） | 国家杰青（先验） | arXiv 2208.11014 | 同上 |
| 沈建冰（北理工） | 国家杰青（先验） | 10.1109/TMI.2020.3043495 等 2 篇 | 主页域名失效 |
| 孟德宇（西安交大） | 国家杰青（先验） | 10.1016/j.neunet.2025.107700 等 3 篇 | gr.xjtu.edu.cn 页面不可达 |
| 左旺孟（哈工大） | 国家杰青（先验） | 10.1016/j.neunet.2025.107700 等 4 篇 | 主页路径未知 |
| 杨健（南京理工） | 国家杰青（先验） | 10.1109/TITS.2025.3553106 等 3 篇 | 学院页 JS |

> 2024 年起 NSFC 不再公布杰青完整名单（titles-source-map §1.2），上述"先验"为模型知识库线索，**置信度 likely，不构成事实断言**，每条均已给建议核验入口。

---

## 三、逐条证据卡（confirmed 项）

### 3.1 戴琼海 — 中国工程院院士

- 📄 **原始证据**（引用 MBLLEN 的论文）：
  - Computational Imaging and Artificial Intelligence: The Next Revolution. *Proceedings of the IEEE*, 2021（S2 记录在 `citing.csv`）
  - DarkVision: A Benchmark for Low-light Image/Video Perception, arXiv:2301.06269, 2023
- 🆔 **结构化实体**：S2 作者档案（见 `profiles.csv`，maxC=28231, h=92）
- 🏅 **责任主体源**：[清华大学自动化系 · 师资队伍 · 两院院士](https://www.au.tsinghua.edu.cn/szdw/lyys.htm)——"戴琼海 中国工程院院士 研究方向 成像与智能"（2026-08-17 抓取，快照 `verif_au_lyys.json`；工程院官网院士馆当日 JS 不可渲染，以雇主官网栏目为确认源，授予机构名录待补）

### 3.2 李厚强 — 国家杰青（2013）

- 📄 **原始证据**：Low-Light Video Enhancement with Synthetic Event Guidance. AAAI 2022（arXiv:2208.11014，共同作者含周文罡）
- 🆔 **结构化实体**：S2 作者档案（`profiles.csv`，maxC=23746, h=78；DBLP 实体见 [pid](https://dblp.org/search?q=Houqiang+Li)）
- 🏅 **责任主体源**：[USTC 官方个人主页](http://staff.ustc.edu.cn/~lihq/)——"国家'杰出青年基金'获得者（2013），长江学者特聘教授（2017），IEEE Fellow"（2026-08-17 抓取，快照 `verif_lihouqiang.json`；NSFC 系统需账号，以雇主官方页为准，标注 self-reported on official domain）

### 3.3 陆峰（种子作者）身份

- 📄 [PHI-AI Lab 官网](https://phi-ai.buaa.edu.cn/)：虚拟现实技术与系统全国重点实验室 · 计算机学院 · 北京航空航天大学
- 📄 [成员页](https://phi-ai.buaa.edu.cn/members/index.htm)："Feng Lu 陆峰 Professor … Beihang University"
- 📄 [出版页](https://phi-ai.buaa.edu.cn/publications/index.htm)：MBLLEN 列于 2018（Feng Lu* 通讯）
- 🆔 S2 论文实体：[70cb4bdd05cccc1f99cf690582e66b7637b81da7](https://www.semanticscholar.org/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7)（作者序 Feifan Lv, Feng Lu, Jianhua Wu, C. Lim）

---

## 四、排除与陷阱记录

| # | 事项 | 处理依据 |
|---|---|---|
| P1 | OpenAlex 当日对本机 IP 持续 HTTP 429（直连单请求亦拒） | fail-soft 降级为 S2+Crossref 双口径；未升级对抗手段（红线） |
| P2 | **吴枫同名陷阱**：staff.ustc.edu.cn/~wufeng02 跳转至 github.io 主页，简历含"Southampton ORCHID/Nick Jennings"——为同名 AI 学者（已赴安徽大学），**非**引用者吴枫 | 引用论文 TMM 2023（10.1109/TMM.2022.3193059）共同作者含傅学阳（USTC 多媒体组），锁定引用者为信院吴枫；其杰青身份因此**降级为待核验** |
| P3 | 严骏驰（上海交大）：实验室官网仅列 "Professor (tenured), Fellow of IAPR/IET/AAIA/NAAI"，无杰青/院士声明 | 不计入杰青/院士（无证据不列） |
| P4 | 任文琦（×7）、刘日升（×9）：先验为**优青**级（非杰青） | 不计入杰青，此处注记 |
| P5 | F. Nie（"Feiping Nie"）：唯一引用论文为 Expert Systems with Applications 2022，领域指纹不足以确认为 NWPU 聂飞平 | 存疑排除（同名风险） |
| P6 | 吴枫 2021 工程院增选"有效候选人"（时代学者转载工程院公告） | 候选 ≠ 当选，不作为院士证据 |
| P7 | S2 作者实体拆分（同一人多 id，h/citations 差一个量级，如 Jiaying Liu h=5 与 h=63 两个实体） | 按名聚合取最大实体 + 引用论文指纹消歧（§11.2 ID 直连优先） |
| P8 | 海外院士（先验，未核验）：S.K. Nayar（美国工程院）、B. Jalali（美国工程院）等在引用者中 | 非中国两院院士，仅附注 |
| P9 | 搜索引擎/维基/百度百科：robots 拒绝或网络不可达；S2 网站 robots 禁爬 /search | 全程未违反任何 robots/ToS；CLS 块状名录（CAE/CAS 院士馆、ucas/bupt 官网）当日不可达 → 对应结论降级 |

---

## 五、口径与局限

- **引用数口径**：S2 citationCount（自建引用图，非 GS 数据），查询日 2026-08-17；Crossref is-referenced-by-count 为下界口径；OpenAlex 口径当日缺失（IP 429）；GS 精确值未获取（红线：不自动化爬取 GS；亦未使用第三方代查）。经验规律 GS ≥ S2，MBLLEN 的 GS 口径预期高于 804。
- **引用者集合**：S2 引用图对预印本/期刊覆盖好，但对专利、书籍覆盖弱；804 条与 S2 总引用数一致（无分页遗漏）。
- **杰青名单**：2024 年起 NSFC 不再公布完整名单；本报告中"先验"标签的条目未获当日证据，置信 likely/not_found_after_search（≠不存在），每条附人工核验入口。
- **置信度标注（§0.5.5）**：戴琼海=confirmed（雇主官网栏目；授予机构名录待补注）；李厚强=confirmed（机构官网自述，NSFC 源不可及已注）；其余先验条目=likely；严骏驰/F.Nie=存疑/排除。
- **数据归档**：`citing.csv`（804 篇）、`authors.csv`（3152 行）、`profiles.csv`（3135 档案）、`evidence_citing_papers.json`、22 个 `verif_*.json` 页面快照、采集脚本 `fetch_citers.py` `fetch_profiles_batch.py` `render_crawl.py`，全部在本目录。
