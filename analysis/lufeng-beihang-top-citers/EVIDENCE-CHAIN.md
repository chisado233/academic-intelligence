# 证据链：北航陆峰被引最多的论文 & 引用者中的院士与杰青（独立重做版）

> **⚠️ 勘误（2026-08-19 复核新增）**：§四-11 同名陷阱条目中 MambaLLIE 的 arXiv 链接有误——原文写 `arXiv 2410.08063`，正确为 [arXiv 2405.16105](https://arxiv.org/abs/2405.16105)（DOI 10.48550/arXiv.2405.16105，NeurIPS 2024，DBLP `conf/nips/WengYTQYL24`，S2 paperId `f35573be…`）。池成员资格与"南理工杨健组"作者归属结论不变（2026-08-17 与 2026-08-19 两轮独立采集的引用池该条目 paperId 一致）。复核来源：round18c 验收交叉比对。

> **任务**：① 查北京航空航天大学陆峰被引最多的论文；② 反查引用该论文的学者中谁是院士、谁是杰青。
> **查询日期**：2026-08-17（所有引用数与头衔均为该日快照）
> **方法**：academic-intelligence skill §11（信息反向挖掘·引用者画像六步工作流）+ §10 / `docs/titles-source-map.md` §0.5（头衔责任主体匹配 + 四值置信度）+ §11.4（多口径引用交叉）+ §12（本报告契约）
> **独立性声明**：本报告为 2026-08-17 下午会话的**独立重做版**——不沿用本仓库 `analysis/` 下任何先前任务报告的结论，所有数字、名单、头衔链接均由本次会话重新采集与核验；中间产物见本目录。

---

## 一、结论摘要

1. **北航陆峰被引最多的论文 = MBLLEN: Low-light Image/Video Enhancement Using CNNs**（BMVC 2018；作者 Feifan Lv, **Feng Lu\***（通讯）, Jianhua Wu, Chongsoon Lim）。
   多口径（2026-08-17）：**Semantic Scholar 804 / Google Scholar 1122**，均为陆峰全部作品的第一名（第二名 Reflection Backdoor：S2 口径之外 GS 961、Crossref 372）。
2. **引用者中的院士：2 人**（均中国工程院）：**戴琼海**（清华大学，2017）、**吴枫**（中国科学技术大学，2025）。
3. **引用者中的杰青：9 人 confirmed**（吴枫杰青身份为 likely，另计）：**马华东**（北邮）、**黄庆明**（国科大）、**李厚强**（中科大）、**查正军**（中科大）、**孟德宇**（西安交大）、**翟广涛**（上海交大）、**卢孝强**（中科院西安光机所）、**於志文**（哈尔滨工程大学）、**杨健**（北京理工大学）。

---

## 二、链路总览

```
① 锁人：Phi-Ai Lab 官方 CV PDF（75 篇代表作）+ GS 主页 + 北航教师主页
        ↓  （OpenAlex 当日免费额度耗尽 429，全流程改用 S2 + Crossref + GS 快照）
② 作品清单与引用数：CV 75 篇 Crossref 批量（is-referenced-by-count）
   + GS 主页全量作品按引用排序（1122/961/...） + S2 逐篇核验（804/555）
        ↓
③ 种子裁决：MBLLEN 双口径第一 → 定为种子
        ↓
④ 反查引用：S2 citations API 全量 804 篇（与 citationCount 严格一致）
        ↓
⑤ 展平 2908 个唯一作者名 / 3135 个 S2 authorId
        ↓
⑥ S2 author/batch 画像（h-index/引用/机构）→ h≥40 全名候选 + 院士观察名单 grep
        ↓
⑦ 头衔核验：两院官网/高校官网逐人核验（四值置信度）
        ↓
⑧ 排除同名陷阱 ──► 本报告 + 数据归档
```

### 步骤明细

| # | 操作 | 工具/命令 | 产出 | 一键验证 |
|---|---|---|---|---|
| 1 | 锁人 | 下载解析[官方 CV PDF](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf) | `cv_segments.jsonl`（75 篇 + 履历：东大博士/清华本硕） | [Phi-Ai Lab 主页](https://phi-ai.buaa.edu.cn/) |
| 2 | GS 主页 | WebSearch + webReader 单次读取 | 陆峰 [GS 主页](https://scholar.google.com/citations?user=9ggbm0QAAAAJ&hl=en)（总引 10,032，按引用排序全表） | 同左（GS 口径快照 2026-08-17） |
| 3 | Crossref 批量 | `cv_works_extract.py`（75 篇 query.bibliographic） | `cv_works_crossref.json`（Top：PR 451 / RB 372 / IJCV 281…） | [Crossref API](https://api.crossref.org/works?query.bibliographic=Reflection+Backdoor) |
| 4 | S2 双口径核验 | `s2_fetch.py` / `s2_seed_crosscheck.json` | MBLLEN **804**、PR **555**、陆峰 authorId=1388864077 | [S2 MBLLEN](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7?fields=title,citationCount) |
| 5 | 反查引用 | `s2_citations_pull.py`（citations API 分页 9 页） | `s2_citing_full.json`（**804 篇**，与 citationCount 一致） | [S2 citations](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7/citations?fields=title&limit=10) |
| 6 | 展平/画像 | `flatten_authors.py` + `author_batch_profiles.py` | `citing_authors_pool.csv`（2908 名）、`author_profiles.json`（3135 id） | 本地归档 |
| 7 | 候选发现 | `academicians_grep.py` + h≥40 全名扫描 + 高风险姓氏防漏复查（Yang Jian 等） | `watchlist_hits.json`、`confirmed_citing_papers.json` | 本地归档 |
| 8 | 头衔核验 | WebSearch（官方域名优先）× 20 次 | 本报告 §四 | 每卡 🏅 层链接 |
| 9 | 收尾 | `python scripts/render_report.py` | 本文件 + HTML | 同目录 `EVIDENCE-CHAIN.html` |

---

## 三、结论一：被引最多的论文 = MBLLEN（多口径裁决）

**MBLLEN: Low-light Image/Video Enhancement Using CNNs**，BMVC 2018，作者 Feifan Lv, Feng Lu\*（通讯）, Jianhua Wu, Chongsoon Lim。
📄 [BMVC 2018 原文 PDF](http://bmvc2018.org/contents/papers/0700.pdf) · [项目主页（陆峰实验室）](https://phi-ai.buaa.edu.cn/project/MBLLEN/) · [CV 条目 #53](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf)

### 多口径引用数（均为 2026-08-17 查询）

| 论文 | Crossref | Semantic Scholar | Google Scholar |
|---|---|---|---|
| **MBLLEN**（BMVC 2018，**无 DOI**） | —（Crossref 不收录无 DOI 会议论文，skill §8 已知限制） | **804** [API](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7?fields=citationCount) | **1122**（[陆峰 GS 主页](https://scholar.google.com/citations?user=9ggbm0QAAAAJ&hl=en) 快照） |
| Reflection Backdoor（ECCV 2020） | 372 [DOI](https://doi.org/10.1007/978-3-030-58607-2_11) | （限流未取；GS/Crossref 已定序） | 961（同上 GS 快照） |
| Understanding Adversarial Attacks on DL Medical Image（PR 2021） | **451** [DOI](https://doi.org/10.1016/j.patcog.2020.107332) | **555** [API](https://api.semanticscholar.org/graph/v1/paper/DOI:10.1016/j.patcog.2020.107332?fields=citationCount) | — |
| Gaze survey（TPAMI 2024，CV 外补充项） | 153 [DOI](https://doi.org/10.1109/TPAMI.2024.3393571) | — | 394（[程一华 GS 主页](https://scholar.google.com/citations?user=cGn8lUAAAAJ&hl=zh-CN) 快照） |
| Attention Guided Low-light（IJCV 2021） | 281 | — | — |
| Gaze Estimation using Transformer（ICPR 2022） | 156 | — | 257 |
| Coarse-to-Fine Gaze（AAAI 2020） | 165 | — | 239 |

**裁决**：可得口径中 **S2 与 GS 两口径 MBLLEN 均第一**（804 vs 555 领先 45%；1122 vs 961 领先 17%），满足 §11.1b 双口径交叉要求；两口径自身差异 40%（<50% 阈值，量级一致）。**MBLLEN 为北航陆峰被引最多的论文**。GS 口径为搜索快照，未逐篇反查 GS 引用列表（局限见 §六）。

**种子校验补充**：陆峰 GS 主页（总引 10,032）按引用降序的前若干名均已列入上表交叉；CV 之外的高引补充项（gaze survey TPAMI 2024）亦已单独核验（394/153，不构成竞争）。

---

## 四、结论二：引用者中的院士与杰青

**院士 2 人**（均中国工程院，官网直接证据 confirmed）：

| 姓名 | 单位 | 头衔 | 引用证据 |
|---|---|---|---|
| 戴琼海 | 清华大学 | 中国工程院院士（2017，信息与电子工程学部） | 2 篇（见证据卡 1） |
| 吴枫 | 中国科学技术大学 | 中国工程院院士（2025）；杰青（likely，见证据卡 2） | 1 篇 |

**杰青 9 人 confirmed**（单位官网/官方新闻直接陈述；吴枫 likely 另计）：

| 姓名 | 单位 | 杰青证据（责任主体源） | 引用 |
|---|---|---|---|
| 马华东 | 北京邮电大学 | [北邮教师主页](https://teacher.bupt.edu.cn/mahuadong)："国家杰出青年科学基金获得者"、IEEE Fellow | 5 篇 |
| 黄庆明 | 中国科学院大学 | [国科大主页](https://people.ucas.ac.cn/~qmhuang)：杰青、IEEE Fellow | 4 篇 |
| 李厚强 | 中国科学技术大学 | [USTC 主页](http://staff.ustc.edu.cn/~lihq/)：杰青 2013、长江 2017、IEEE Fellow 2021 | 1 篇 |
| 查正军 | 中国科学技术大学 | [USTC 主页](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm)：优青 2016 + 杰青 | 1 篇 |
| 孟德宇 | 西安交通大学 | [西交主页](https://gr.xjtu.edu.cn/mengdeyu/zh_CN/index.htm) + [上交会议官方介绍](https://ins.sjtu.edu.cn/conferences/2735)：杰青、长江特聘 | 3 篇 |
| 翟广涛 | 上海交通大学 | [SJTU 计算机学院官网](https://cs.sjtu.edu.cn/jzhspjs/1360.html)：杰青、IEEE Fellow | 6 篇 |
| 卢孝强 | 中科院西安光学精密机械研究所 | [光机所官网新闻 2019-12](http://www.xab.ac.cn/ttxw/201912/t20191202_5446648.html)：获国家杰青资助（2019） | 1 篇 |
| 於志文 | 哈尔滨工程大学（原西北工业大学） | [哈工程教师主页](https://faculty.hrbeu.edu.cn/yuzhiwen/zh_CN/index.htm)：杰青 2017（城市大数据三元空间协同计算）、优青 2012 | 2 篇 |
| 杨健 | 北京理工大学（光电学院） | [北理工光电学院教师页](https://opt.bit.edu.cn/jsdw/gdrc/218ea1c9a7284901ab464121d8a83bde.htm) + [INAVI 实验室主页](https://www.inavilab.com/teachers/yj.html)：国家杰青、国家级领军人才 | 1 篇 |

### 逐条证据卡（📄 论文 / 🆔 实体 / 🏅 责任主体源 三层）

**1. 戴琼海（清华大学，工程院院士 2017）— confirmed**
- 📄 引用论文：[DarkVision: A Benchmark for Low-light Image/Video Perception（arXiv 2023）](https://doi.org/10.48550/arXiv.2301.06269)、[Computational Imaging and Artificial Intelligence（Proc. IEEE）](https://doi.org/10.1109/JPROC.2023.3338272)——低照度感知/计算成像，与其"自动控制/智能科学与成像"方向及清华署名网络吻合
- 🆔 池内 S2 实体：authorId 1491800101 / 144954808（h=92）
- 🏅 [中国工程院院士主页（WebFetch 核验：2017 年当选，自动控制学专家）](https://www.cae.cn/cae/html/main/colys/02548488.html)

**2. 吴枫（中国科学技术大学，工程院院士 2025 + 杰青 likely）**
- 📄 引用论文：[Unsupervised Underexposed Image Enhancement via Self-Illuminated and Perceptual Guidance（IEEE TMM 2023）](https://doi.org/10.1109/TMM.2022.3193059)——多媒体/视频编码方向引用 MBLLEN 合理
- 🆔 池内 S2 实体：authorId 144864333（h=63）
- 🏅 院士：[中国工程院院士名单页（"2025 年当选中国工程院院士"，计算机应用技术专家）](https://www.cae.cn/cae/html/main/colys/95871425.html) + [中国科大官方喜报](https://news.ustc.edu.cn/info/1001/93281.htm)；杰青：中科大校友基金会报道其为国家杰青获得者（**USTC 官方主页未见直接陈述 → likely，年份未获取**）

**3. 马华东（北京邮电大学，杰青；非院士）**
- 📄 引用论文（5 篇）：[Rethinking the Low-Light Video Enhancement（TIP 2025）](https://doi.org/10.1109/TIP.2025.3616639)、[Dancing in the Dark（ICCV 2023）](https://doi.org/10.1109/ICCV51070.2023.01183)等——多媒体/视频方向与其一致
- 🆔 池内 S2 实体：authorId 2248034542 / 2263653397 / 144258295（h=53）
- 🏅 杰青：[北邮教师主页](https://teacher.bupt.edu.cn/mahuadong)（"国家杰出青年科学基金获得者"）；⚠️ 修正记录：他**不是院士**——仅列 [2017 年工程院增选有效候选人名单](https://www.cae.cn/cae/html/main/col280/2017-04/21/20170421160331519677867_1.html)，未当选（观察名单初判有误，官方源裁决修正）

**4. 黄庆明（中国科学院大学，杰青）**
- 📄 引用论文（4 篇）：[Unsupervised Low-Light Video Enhancement With Spatial-Temporal Co-Attention Transformer（TIP 2023）](https://doi.org/10.1109/TIP.2023.3301332)、[Adaptive Multi-Exposure Image Correction（ACM TOMM 2025）](https://doi.org/10.1145/3735973)
- 🆔 池内 S2 实体：authorId 对应 h=84（池内中国学者最高之一）
- 🏅 杰青：[国科大官方主页](https://people.ucas.ac.cn/~qmhuang)（"国家杰出青年科学基金获得者"、IEEE Fellow；当选年份未获取）

**5. 李厚强（中国科学技术大学，杰青 2013）**
- 📄 引用论文：[Low-Light Video Enhancement with Synthetic Event Guidance（AAAI 2022）](https://doi.org/10.48550/arXiv.2208.11014)
- 🆔 池内 S2 实体：h=78
- 🏅 杰青：[USTC 官方主页](http://staff.ustc.edu.cn/~lihq/)："国家'杰出青年基金'获得者（2013）"、长江特聘 2017、IEEE Fellow

**6. 查正军（中国科学技术大学，优青 2016 + 杰青）**
- 📄 引用论文：[Illumination-Invariant Person Re-Identification（ACM MM 2019）](https://doi.org/10.1145/3343031.3350994)
- 🆔 池内 S2 实体：h=79
- 🏅 杰青：[USTC 教师主页](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm) + [先研院页面](https://iatyz.ustc.edu.cn/teacher/profile/name/%E6%9F%A5%E6%AD%A3%E5%86%9B)（优青 2016 + 杰青已获资助；杰青年份未获取）

**7. 孟德宇（西安交通大学，杰青）**
- 📄 引用论文（3 篇）：[Low-Light Image Enhancement by Retinex-Based Algorithm Unrolling（IEEE TNNLS）](https://doi.org/10.1109/TNNLS.2023.3289626)、[Degradation-Guided cross-consistent deep unfolding（Neural Networks 2025）](https://doi.org/10.1016/j.neunet.2025.107700)——低照度+算法展开与其机器学习理论方向一致
- 🆔 池内 S2 实体：h=78
- 🏅 杰青：[西交大教师主页](https://gr.xjtu.edu.cn/mengdeyu/zh_CN/index.htm) + [上海交大会议官方介绍](https://ins.sjtu.edu.cn/conferences/2735)（杰青 + 长江特聘 + 青年拔尖；当选年份未获取）

**8. 翟广涛（上海交通大学，杰青）**
- 📄 引用论文（6 篇）：[GLARE（ECCV 2024）](https://doi.org/10.48550/arXiv.2407.12431)、[Light-VQA（ACM MM 2023）](https://doi.org/10.1145/3581783.3611923)、[Perceptual Quality Assessment of Low-light Image Enhancement（ACM TOMM 2021）](https://doi.org/10.1145/3457905)——低照度质量评价系列
- 🆔 池内 S2 实体：h=74
- 🏅 杰青：[SJTU 计算机学院官网](https://cs.sjtu.edu.cn/jzhspjs/1360.html)（"国家自然科学基金杰出青年基金获得者"、IEEE Fellow；年份未获取）

**9. 卢孝强（中科院西安光机所，杰青 2019）**
- 📄 引用论文：[Attention-Based Multi-Branch Network for Low-Light Image Enhancement（ICBAIE 2021）](https://doi.org/10.1109/ICBAIE52039.2021.9389960)——智能光学/机器视觉方向一致
- 🆔 池内 S2 实体：h=63
- 🏅 杰青：[西安光机所官网新闻（2019-12-02）](http://www.xab.ac.cn/ttxw/201912/t20191202_5446648.html)"获国家杰出青年科学基金资助"；注意中文为"卢**孝**强"

**10. 於志文（哈尔滨工程大学，杰青 2017）**
- 📄 引用论文：[AdaEnlight（ACM IMWUT 2022）](https://doi.org/10.1145/3569464)、[MoEnlight（ACM TURC 2023）](https://doi.org/10.1145/3603165.3607375)——移动/普适计算场景的低照度视频节能，与其"普适计算/社会感知"方向一致（IMWUT 为普适计算旗舰刊）
- 🆔 池内 S2 实体：h=51
- 🏅 杰青：[哈工程教师主页](https://faculty.hrbeu.edu.cn/yuzhiwen/zh_CN/index.htm)（2017 国家杰青"面向城市大数据的三元空间协同计算"；2012 首批优青）；注意姓氏正确写法"**於**志文"

**11. 杨健（北京理工大学光电学院，杰青）**
- 📄 引用论文：[An automatic framework for endoscopic image restoration and enhancement（Applied Intelligence 2020）](https://doi.org/10.1007/s10489-020-01923-w)——合作者含 **Hong Song（宋红，北理工计算机学院）** 与 **Alejandro F Frangi（曼彻斯特，医学影像）**，北理工医学图像团队指纹唯一匹配
- 🆔 池内 S2 实体：h=35（S2 实体碎片化致偏低；北理工官方介绍为二级/特聘教授）
- 🏅 杰青：[北理工光电学院教师页](https://opt.bit.edu.cn/jsdw/gdrc/218ea1c9a7284901ab464121d8a83bde.htm) + [INAVI 实验室主页](https://www.inavilab.com/teachers/yj.html)："国家杰出青年科学基金获得者"、国家级领军人才（年份未获取）
- ⚠️ 同名陷阱：池中另有 2 个 Jian Yang——[MambaLLIE（2024）](https://doi.org/10.48550/arXiv.2410.08063)作者（南理工低照度 Mamba 团队）与一篇 2025 夜间去雨论文作者（年轻同名），均非北理工杨健，头衔结论只适用于北理工杨健

---

## 五、排除与陷阱记录（负结论同样给证据）

| 候选 | 排除原因 | 证据 |
|---|---|---|
| Zhang Bo（池中 7 处） | 拼音匹配张钹（清华/中科院院士）初筛命中，但 7 篇引用分布在 6 个不同 S2 authorId、均为应用型低照度论文，无清华署名/方向指纹 → **同名排除** | `confirmed_citing_papers.json` 对照 + `author_profiles.json` |
| Zhang Ping（1 处） | 与北邮张平院士（移动通信）指纹不符（低层级会议 ICDSCA 2024 GAN 论文）→ 同名排除 | DOI [10.1109/ICDSCA63855.2024.10859773](https://doi.org/10.1109/ICDSCA63855.2024.10859773) |
| 马佳义（武汉大学，h=89） | 仅"**湖北省杰青** + 万人计划青年拔尖"，非国家杰青 | [中原工学院讲座介绍](https://www.zut.edu.cn/info/1044/26713.htm)、[百度百科](https://baike.baidu.com/item/%E9%A9%AC%E4%BD%B3%E4%B9%89/49763906) |
| 刘家瑛（北京大学，h=63） | 官方头衔 IEEE Fellow + 长江特聘（含青年长江），无国家杰青/优青记录 | [北大 AI 研究院主页](https://www.ai.pku.edu.cn/info/1312/1683.htm)、[王选所主页](https://www.wict.pku.edu.cn/xstd/xstd_01/1201844icst1222618.htm) |
| 沈建冰（澳门大学，h=88） | IEEE Fellow 2024、教育部新世纪优秀人才；无国家杰青/优青记录 | [澳门大学 IOTSC 师资页](https://skliotsc.um.edu.mo/people/academic-staff/?lang=zh-hant)、[浙大 IEEE Fellow 校友新闻](http://www.cs.zju.edu.cn/csen/2023/1130/c38564a2832371/page.htm) |
| 严骏驰（上海交大，h=73） | **优青**（非杰青）、IAPR Fellow | [上交计算机系主页](https://cs.sjtu.edu.cn/cse/PeopleDetail.aspx?id=400) |
| 周文罡（中科大，h=71） | **优青 2018**（非杰青） | [USTC 主页](https://faculty.ustc.edu.cn/~qQJJFn/zh_CN/index.htm) |
| 方玉明（江西财经大学，h=53） | **优青 2018** + 江西省级杰青类资助；无国家杰青 | [江西财大官网](http://cta.jxufe.edu.cn/pubcontent/index?newId=279&fid=&typeid=286) |
| 刘日升（大连理工，h=52，池中 8 篇） | **优青**（非杰青） | [百度百科](https://baike.baidu.com/item/%E5%88%98%E6%97%A5%E5%8D%87/61951236)、[大工未来技术学院页](https://futureschool.dlut.edu.cn/info/1037/3542.htm) |
| 赵德斌（哈工大，h=55） | 两轮 4+ 变体检索无国家杰青官方陈述 → not_found_after_search（不确认，非否定） | [哈工大主页](https://homepage.hit.edu.cn/zhaodebin) |
| 段凌宇（北京大学，h=53） | 两轮检索无杰青/优青官方陈述 → not_found_after_search | [北大计算机学院主页](https://cs.pku.edu.cn/info/1089/1654.htm) |
| 苗夺谦（同济大学，h=49） | 官方主页仅列 IRSS/CAAI Fellow；"杰青"仅见会议介绍歧义表述 → not_found_after_search | [同济计算机学院主页](https://cs.tongji.edu.cn/info/1061/2805.htm) |
| 齐国君（西湖大学，h=59） | IEEE Fellow/ACM 杰出科学家；无国家杰青记录 | [西湖大学工学院](https://engineering.westlake.edu.cn/Recuitment/202509/t20250926_59771.shtml)、[中科大校友会](https://aga.ustc.edu.cn/info/1197/35700.htm) |
| 江俊君（哈工大/原合工大，h=57） | 广称优青类国家级青年人才；无杰青证据 | [ResearchGate](https://www.researchgate.net/profile/Junjun-Jiang) |
| Ke Chen（h=87） | 唯一引用论文为低层级会议 ICPECA 2023，机构指纹不足以对应中科大陈柯（计算摄像）→ 存疑排除 | DOI [10.1109/ICPECA56706.2023.10075845](https://doi.org/10.1109/ICPECA56706.2023.10075845) |
| Hao Wang / Fan Wang / Ying Chen（h=72/56/73） | 高频同名且机构字段缺失，指纹不足 → 不作头衔结论 | `author_profiles.json` |
| Luc Van Gool（ETH，h=195） | 外籍院士：中科院外籍院士名录检索未命中 → not_found_after_search（≠不存在） | [中科院学部外籍院士页](https://casad.cas.cn/yszx/) |
| 马爱龙（武汉大学，2024 杰青） | 不在 MBLLEN 引用作者池中（本地 grep 无命中） | `citing_authors_pool.csv` |
| Feng Lu / Feifan Lv（池中"引用者"） | 陆峰及一作自引（9 篇），不计入"引用者" | `s2_citing_full.json` |
| 观察名单其余未命中 | 高文/谭铁牛/郑南宁/徐宗本/王耀威/赵沁平/张平（真身）/何友/吴朝晖/李德毅/陆建华/尤政/蒋昌俊/沈绪榜/张钹（真身）等均不在池（或仅同名命中，见上） | `watchlist_hits.json` |

---

## 六、口径与局限

1. **引用列表口径**：结论覆盖 **S2 全量 804 篇**（与 citationCount 严格一致）；GS 口径 1122 未逐篇反查——GS 独有的引用者（学位论文/预印本作者等）可能遗漏。OpenAlex 当日免费额度耗尽（429，UTC 午夜重置），其 `cites:` 口径未参与；OpenAlex 对无 DOI 会议论文引用追踪偏弱（skill §11.4）。
2. **头衔判定边界**：国家杰青 2024 年起基金委不再公布完整名单，2024–2025 新当选者只能靠单位官网发现，**可能漏检**（尤其 h<55 的年轻组：任文琦、施柏鑫、熊志伟、付学阳、闵雄阔、侯君辉、李重阳等未逐一核验，多数为优青量级）。院士为两院封闭名录，漏检风险低；外籍院士按名录检索未命中记录在案。
3. **四值置信度**：院士 2 人均 **confirmed**（工程院官网 + 单位官方新闻）；杰青 9 人 **confirmed**（7 人单位官网直接陈述 + 2 人双独立官方介绍），另吴枫杰青 **likely**（校友基金会报道、官方主页未直接列）。赵德斌/段凌宇/苗夺谦为 **not_found_after_search**（不构成否定）。
4. **S2 实体碎片化**：同一人多 authorId（如马华东 3 个），h 指数取各实体最大值近似；author/batch 不支持 lastKnownInstitution 字段（HTTP 400 实测），机构画像以引用论文署名 + WebSearch 交叉替代。
5. **网络环境**：S2 免费共享池当日持续 429，长退避串行完成；OpenAlex 429 全程不可用——均已在方法链中如实降级并记录。
6. **数据快照日**：2026-08-17。
