# EVIDENCE-CHAIN：北航陆峰被引最多论文 & 引用者中的杰青/院士

- **任务**：① 查北京航空航天大学陆峰教授被引最多的论文；② 反查该论文的引用者中，谁是杰青、谁是院士。
- **查询日期**：2026-08-17（北京时间下午/晚间）
- **方法**：`academic-intelligence` skill（本仓库 CLI `paper` + Python 库形态）。流程遵循 SKILL.md §11（信息反向挖掘·引用者画像 6 步工作流）与 §10 / docs/titles-source-map.md §0.5（资历核验方法论）。**约束**：全程只使用本 skill；未读取任何历史探查记录（analysis/ 下旧目录一律未打开；本次使用独立数据库 `task.db` 从零采集）。

---

## 1. 结论

### 1.1 陆峰被引最多的论文

> **MBLLEN: Low-light Image/Video Enhancement Using CNNs**
> Feifan Lv, **Feng Lu\***（通讯）, Jianhua Wu, Chongsoon Lim · British Machine Vision Conference (BMVC) 2018 · 无 DOI/arXiv · S2 paperId `70cb4bdd05cccc1f99cf690582e66b7637b81da7`

| 论文 | 年份 | S2 citationCount | Crossref is-referenced-by-count |
|---|---|---|---|
| **MBLLEN（BMVC 2018）** | 2018 | **804** | —（无 DOI，Crossref 不收录）|
| Attention Guided Low-light Image Enhancement…（IJCV） | 2021 | 333 | 281 |
| Appearance-Based Gaze Estimation…Review and Benchmark（TPAMI） | 2024 | 264 | 153 |
| Appearance-Based Gaze Estimation via Evaluation-Guided Asymmetric Regression（ECCV） | 2018 | 169 | — |
| A Head Pose-free Approach for Appearance-based Gaze Estimation（BMVC） | 2011 | 102 | — |
| SymPS: BRDF Symmetry Guided Photometric Stereo（TPAMI） | 2018 | 45 | 41 |
| Uncalibrated Photometric Stereo under Natural Illumination（CVPR） | 2018 | — | 29 |

- 数字来源：S2（2026-08-17 经 skill S2 源实测）；Crossref（2026-08-17 经 skill crossref 源实测）。
- 判定依据：SKILL §11.1①b——无 DOI 会议论文以 S2 citationCount 为准；804 对第二名 333 为断层领先（>2.4×），全部有 DOI 作品的 Crossref 口径均 ≤281。两口径交叉已满足（S2 + Crossref 下界）。
- 归属裁决：SKILL §8 硬规则——以[陆峰实验室官方出版物页](https://phi-ai.buaa.edu.cn/publications/index.htm)逐条核对，MBLLEN 在列且陆峰标 `*`（通讯），一作吕飞帆（Feifan Lv）为其 2017-2020 硕士生（[members 页](https://phi-ai.buaa.edu.cn/members/index.htm)）。

### 1.2 引用者中的院士（两院口径，官方名录 confirmed）

| 姓名 | 单位 | 头衔 | 置信度 | 引用 MBLLEN 的论文 |
|---|---|---|---|---|
| **戴琼海** | 清华大学 | 中国工程院院士（**2017 年当选**，信息与电子工程学部；自动控制/智能科学与技术、成像与脑认知） | **confirmed** | ① DarkVision: A Benchmark for Low-light Image/Video Perception（2023, arXiv [2301.06269](https://arxiv.org/abs/2301.06269)）② Computational Imaging and Artificial Intelligence: The Next Revolution（Proc. IEEE 2021） |
| **吴枫** | 中国科学技术大学 | 中国工程院院士（**2025 年当选**，信息与电子工程学部；计算机应用技术/多媒体压缩与网络传输） | **confirmed** | Unsupervised Underexposed Image Enhancement via Self-Illumination Compensation（IEEE TMM 2022, DOI [10.1109/TMM.2022.3193059](https://doi.org/10.1109/TMM.2022.3193059)，末位作者，合作者含 USTC 傅学阳） |

**高概率但未能官方核验（indeterminate，渠道不可达，详见 §5）**：

| 姓名 | 单位 | 待核头衔 | 身份依据 | 引用论文 |
|---|---|---|---|---|
| 田奇（Qi Tian） | 华为→湖南大学 | 中国科学院院士（2023？） | 末位作者+USTC 周文罡/李厚强合著指纹 | Low-Light Video Enhancement with Synthetic Event Guidance（ACM MM 2022, arXiv [2208.11014](https://arxiv.org/abs/2208.11014)） |
| 杨健（Jian Yang） | 南京理工大学 | 中国科学院院士（2023？） | 钱建军/严志强/翁江维等南理工合著指纹 | MambaLLIE（IJCAI 2024, arXiv [2405.16105](https://arxiv.org/abs/2405.16105)）、Daytime-Mixed Non-Aligned Learning（2025） |
| 曹进德（Jinde Cao） | 东南大学 | 欧洲科学院院士等（宽口径"院士"，非两院） | LightingNet 合作群指纹 | LightingNet（2023，被引 116） |

### 1.3 引用者中的杰青（官方/官网 confirmed）

| 姓名 | 单位 | 头衔 | 置信度 | 引用 MBLLEN 的论文 |
|---|---|---|---|---|
| **程明明**（Ming-Ming Cheng） | 南开大学 | 国家杰出青年科学基金（**2023.01–2027.12**，面向开放环境的图像自适应感知）；另：优青、国家"万人计划"青年拔尖 | **confirmed**（[个人官网基金列表](https://mmcheng.net/)） | Low-Light Image and Video Enhancement Using Deep Learning: A Survey（IJCV 2021，S2 被引 585） |

**高概率杰青候选（身份指纹吻合，但当日核验渠道全部不可达 → indeterminate / not_found_after_search）**：
刘日升（大连理工，9 篇引用——外部引用者最高频之一）、刘家英（北京大学，5 篇，含 372 引 TPAMI 基准论文）、马华东（北京邮电大学，5 篇）、黄庆明（中国科学院大学，4 篇）、查正军（中科大，1 篇）、沈建冰（2 篇，含 172 引视网膜论文）、段凌宇（北大深圳，2 篇）、左旺孟（哈工大，2 篇）、熊志伟/刘东（中科大）、付莹（北理工）、薛向阳（复旦）。
**优青-track（≠杰青，仅备查）**：翟广涛、闵霄阔（SJTU）、周曼（合肥工大）、方玉明（江西财大）、刘金元（大连理工）、施柏鑫（北大）、任文琦（中科院信工所）等。

> 最高频引用者 Wenhan Yang（杨文瀚，北大，12 篇引用论文）与陆峰本人（7 篇自引）无杰青/院士主张记录。

---

## 2. 链路总览

| # | 步骤 | 命令/脚本 | 产出 | 一键验证 |
|---|---|---|---|---|
| 1 | 锁人 | `paper web crawl https://phi-ai.buaa.edu.cn/…` | lab_home.json / lab_members.json / lab_pubs.json | [官网](https://phi-ai.buaa.edu.cn/) |
| 2 | 种子校验 | `paper source semantic_scholar search/get`；`paper source crossref search` | s2_mbllen.json, s2_get_*.json, cr_tmp 等 | [S2 API](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7?fields=title,citationCount) |
| 3 | 反查引用（804 篇） | `analysis/.../fetch_citations.py`（skill S2 适配器库形态，CLI 的 50 条上限/无作者字段的已知限制，见 §5-D） | s2_citing_full.json → citing.csv | 同上（citations 端点） |
| 4 | 展平作者 | `paper trace-authors citing.csv` | authors.csv（2908 行） | 本目录 |
| 5 | 候选池 | 打分脚本（频次 × 引用论文影响力） | pool.json（252 人） | 本目录 |
| 6 | 短名单档案 | `fetch_author_profiles.py`（S2 /author） | shortlist_profiles.json | [S2 author API](https://api.semanticscholar.org/graph/v1/author/1491800101?fields=name,affiliations) |
| 7 | 头衔核验 | `paper web crawl`（两院名录/官网/arXiv） | cae_*.json, probe_*.json, titles.csv | 见 §3 证据卡 |
| 8 | 报告 | 本文件 + `python scripts/render_report.py … --open` | EVIDENCE-CHAIN.html | 本目录 |

## 3. 逐条证据卡（三层证据）

### 3.1 戴琼海 = 工程院院士（confirmed）
- 📄 原始证据：引用论文 [arXiv 2301.06269](https://arxiv.org/abs/2301.06269)（作者列 Bo Zhang, …, J. Suo, Qionghai Dai——清华计算成像组指纹）；Proc. IEEE 2021 综述（citing.csv 行 #（见 s2_citing_full.json））
- 🆔 结构化实体：S2 authorId [1491800101](https://api.semanticscholar.org/graph/v1/author/1491800101?fields=name)
- 🏅 责任主体源：[工程院院士名单（信息与电子工程学部）](https://www.cae.cn/cae/html/main/col48/column_48_1.html)；[院士个人页](https://www.cae.cn/cae/html/main/colys/02548488.html)："2017 年当选中国工程院院士"，自动控制学专家
- 时效：现任（截至 2026-08-17 名录在列）

### 3.2 吴枫 = 工程院院士（confirmed）
- 📄 原始证据：引用论文 IEEE TMM 2022（DOI [10.1109/TMM.2022.3193059](https://doi.org/10.1109/TMM.2022.3193059)，作者 Naishan Zheng, Jie Huang, Fengmei Zhao, Xueyang Fu（USTC）, Feng Wu）
- 🆔 结构化实体：S2 authorId [144864333](https://api.semanticscholar.org/graph/v1/author/144864333?fields=name,affiliations)（affiliations=University of Science and Technology of China）
- 🏅 责任主体源：[工程院院士名单](https://www.cae.cn/cae/html/main/col48/column_48_1.html)；[院士个人页](https://www.cae.cn/cae/html/main/colys/95871425.html)："2025 年当选中国工程院院士"，计算机应用技术专家（多媒体压缩与网络传输——与 USTC 吴枫履历一致）
- 交叉：院士页领域(多媒体压缩) × 引用论文领域(欠曝光图像增强，USTC 信息学院群体) × S2 机构——三处吻合

### 3.3 程明明 = 杰青（confirmed）
- 📄 原始证据：引用论文 IJCV 2021 低光照综述（S2 被引 585；S2 中作者名拼作 "Mingg-Ming Cheng"，源数据笔误）
- 🆔 结构化实体：S2 authorId [1557350184](https://api.semanticscholar.org/graph/v1/author/1557350184?fields=name)
- 🏅 责任主体源：[mmcheng.net 基金列表](https://mmcheng.net/)——"国家杰出青年科学基金｜面向开放环境的图像自适应感知｜2023.01–2027.12｜程明明"（个人官网自述+项目明细；雇主机构官网级页面，单源 confirmed 依据 §0.5.5"一级直接证据可单独确认"的边界，标注为官网自述型直接证据）

### 3.4 田奇 / 杨健（indeterminate——身份 likely，头衔未核验）
- 📄 田奇：arXiv [2208.11014](https://arxiv.org/abs/2208.11014)（末位作者；合著 Wen-gang Zhou、Houqiang Li 等 USTC 群体）。杨健：arXiv [2405.16105](https://arxiv.org/abs/2405.16105)（合著 钱建军/严志强/翁江维——南理工群体）
- 🆔 S2 authorId：田奇 2056268330；杨健 2273535040 / 2146236970
- 🏅 责任主体源：**未获取**。中科院院士馆（ys.casad.cas.cn）为 JS 应用不可爬；zh.wikipedia / web.archive.org 本网络不可达；Bing/百度被 robots 拒（skill 合规 fail-closed）；湖南大学/南理工门户 JS 或反爬（202/412）。已试 query/URL 见 §5-C。
- 结论状态：indeterminate（"未获取"≠"不存在"）

## 4. 排除与陷阱记录

| 事项 | 裁决 | 依据 |
|---|---|---|
| "Bo Zhang"（7 处引用署名）≠ 张钹院士 | 排除 | GDP2023 的 Bo Zhang S2 档案=上海 AI Lab；DarkVision 的 Bo Zhang 为戴琼海组学生型作者；领域/年龄/角色均与张钹（1935 生，AI 理论）不符 |
| 内窥镜修复论文（2020）的 "Jian Yang" | 与南理工杨健分开处理 | S2 id 13524601 与 MambaLLIE 的杨健（2273535040）不同实体；合著群（Alejandro Frangi 等）非南理工指纹 |
| "Ping Zhang"（2024 一篇） | 不采信为北邮张平院士 | 领域不符（低光照增强 vs 通信）；单一同名无机构证据——§11.2 不硬合并 |
| 陆峰自引 7 篇 | 计入 804 但单独披露 | S2 口径含自引；self-citations 为常态 |
| OpenAlex 全程 429 | 放弃当日第二口径 | 十次重试跨 ~25 分钟全部 HTTP 429（本机 IP 当日限额）；S2+Crossref 已满足双口径（§11.1①b 无 DOI 论文以 S2 为准） |

## 5. 口径与局限

- **A 引用口径**：S2 自建引用图（804，2026-08-17）；非 Google Scholar 数据。GS 精确值未获取（用户约束禁用其他工具；skill §11.4 的人工单查路径亦未触发）。参考：SKILL §8 教训记录中 MBLLEN 的 GS 口径曾达 1121（该数字为 skill 文档所载历史快照，本次未复核）。
- **B 覆盖范围**：804 篇引用论文 → 2908 个作者行（约 3136 个不同署名）。系统性筛查覆盖：高被引引用论文作者（前 45 篇）+ 频次×影响力池（252 人）+ 约 90 个知名学者姓名正/反向扫描 + 工程院信息学部名录拼音比对。**不承诺全量穷举**；未入围作者中仍可能有未发现的杰青/院士。
- **C 头衔核验可达性**（2026-08-17 本网络实测）：可达 ✅ cae.cn、casad.cas.cn 静态公告、phi-ai.buaa.edu.cn、mmcheng.net、arxiv.org、news.hnu.edu.cn、cs.dlut.edu.cn（目录 JS）；不可达 ❌ api.openalex.org（429 限额）、api.semanticscholar.org（间歇 429，已用退避完成关键取数）、Bing（robots）、zh.wikipedia/web.archive.org（网络不通）、多数高校师资门户（JS/202/412）、NSFC 无公开完整库（2024 起杰青不公布完整名单——docs/titles-source-map.md §1.2）。
- **D 工具边界**：`paper source semantic_scholar citations` CLI 适配器单次上限 50 条且无作者字段（源码 `sources/semantic_scholar.py::get_citations`），故 804 条全量+作者字段经 skill 的 Python 库形态（`SemanticScholarSource._get_json`，SKILL §4）分页获取，礼貌退避（429 睡 75s）。
- **置信度标注**：confirmed=责任主体直接证据；likely=单一/间接证据；indeterminate=渠道不可达；not_found_after_search=达终止条件无果（本次主要落在 indeterminate）。"未获取"≠"不存在"。

## 6. 归档文件清单（本目录）

`citing.csv`（804 行引用论文）· `authors.csv`（2908 行作者）· `pool.json`（252 人候选池）· `s2_citing_full.json`（全量原始）· `shortlist_profiles.json`（29 人 S2 档案）· `titles.csv`（头衔结论表）· `fetch_citations.py` / `fetch_author_profiles.py`（取数脚本）· `task.db`（任务独立数据库）· `lab_*.json` `cae_*.json` `probe_*.json`（网页证据）· 各 S2/Crossref 原始响应 JSON
