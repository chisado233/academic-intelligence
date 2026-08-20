# 证据链：北航陆峰被引最多的论文 & 引用者中的院士与杰青

> **任务**：① 查北航（北京航空航天大学）陆峰被引最多的论文是哪一篇；② 反查引用该论文的学者中，谁是杰青、谁是院士。
> **查询日期**：2026-08-17（全部数据与头衔均为该日快照）
> **方法**：academic-intelligence skill §11（信息反向挖掘·引用者画像）+ §10 / `docs/titles-source-map.md` §0.5（学术资历查询方法论：身份指纹 → 声明-责任主体匹配 → 四值置信度）+ §12（本报告契约）
> **结论摘要**：被引最多论文 = **MBLLEN（BMVC 2018，陆峰通讯作者）**（S2 804 / GS 1121 双口径居首；OpenAlex 口径因无 DOI 会议论文收录偏少列第三）。引用者中：**院士 2 人**（戴琼海、吴枫——吴枫兼杰青），**杰青 8 人**（吴枫、李厚强、黄庆明、孟德宇、翟广涛、查正军、卢孝强、杨健）。

---

## 一、链路总览

```
① 锁人（陆峰 = Feng Lu，北航计算机学院）
   ├─ paper author search "Lu Feng" --disambiguate → OpenAlex A5050527785（Beihang University）
   ├─ 官方 CV PDF（phi-ai.buaa.edu.cn）解析：75 篇代表作清单 + 履历（东大博士/清华本硕）
   └─ ORCID 0000-0001-9064-7964；GS 总引用 8500+ ≈ OpenAlex 7842 自洽
        ▼
② 多源作品清单（OpenAlex 274 篇 + CV 75 篇 + S2 交叉；发现并修正 2 处实体污染）
        ▼
③ 被引最多论文判定（OpenAlex / S2 / GS-snapshot 三口径交叉，§11.4）
        ▼
④ 反查引用：paper trace-citing W2893333553（OpenAlex 315 篇）
   + S2 citations API 直连补全（804 篇全量，CLI 因无 DOI 无法自动路由 S2 → PARTIAL 上报）
        ▼
⑤ 展平作者 + 画像：trace-authors（1203 行）；S2 batch 3135 位作者（h 指数/机构）
        ▼
⑥ 候选筛选：预置观察名单（16 院士 + 36 杰青拼音）grep 全池 + h≥55 全扫描
        ▼
⑦ 头衔核验（责任主体源：工程院名录 / 高校官网；四值置信度）
        ▼
⑻ 负结论与同名陷阱排除 ──► 本报告
```

### 步骤明细

| # | 操作 | 命令 / 手段 | 产出 | 一键验证 |
|---|------|------------|------|---------|
| 1 | 锁人 | `paper author search "Lu Feng" --disambiguate` | 候选 `A5050527785`（Beihang） | [OpenAlex 作者实体](https://api.openalex.org/authors/A5050527785) |
| 2 | 官方履历 | 下载并解析 [CV PDF](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf) | `cv_segments.jsonl`（75 篇 + 任职史） | 北航教师主页：[shi.buaa.edu.cn/lufeng](https://shi.buaa.edu.cn/lufeng/en/index.htm) |
| 3 | 作品全集 | OpenAlex works API（cursor 翻页 274 篇） | `openalex_works_A5050527785.json` | [works?filter=author.id:A5050527785](https://api.openalex.org/works?filter=author.id:A5050527785&sort=cited_by_count:desc&per-page=25) |
| 4 | 引用数交叉 | `paper source semantic_scholar get <DOI>` + S2 API | `s2_top_candidates.json` | 见 §二 多口径表逐条链接 |
| 5 | 反查引用 | `paper trace-citing W2893333553 --sources openalex,semantic_scholar` | `citing.csv`（315 行） | [OpenAlex filter=cites:W2893333553](https://api.openalex.org/works?filter=cites:W2893333553&per-page=25) |
| 6 | S2 口径补全 | S2 `/paper/{id}/citations` + `/paper/batch` + `/author/batch` 直连 | `s2_citing_mbllen_enriched.json`（804 篇）、`s2_author_details.json`（3135 人） | [S2 论文页](https://www.semanticscholar.org/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7) |
| 7 | 展平/画像 | `paper trace-authors citing.csv` | `authors.csv`（1203 行） | 本地归档 |
| 8 | 观察名单 grep | 16 位院士 + 36 位杰青候选拼音 × 3241 个姓名池 | `suspect_hits.json`、`hit_citing_papers.json` | 本地归档（可复跑脚本逻辑见报告仓库） |
| 9 | 头衔核验 | WebSearch（官方域名优先）+ 上一任务已验证链接复用 | 本报告 §三 证据卡 | 每卡 🏅 层链接 |
| 10 | 收尾 | `python scripts/render_report.py` | 本文件 + HTML | 同目录 `EVIDENCE-CHAIN.html` |

---

## 二、结论一：被引最多的论文 = MBLLEN（多口径裁决）

**MBLLEN: Low-light Image/Video Enhancement Using CNNs**（BMVC 2018，作者 Feifan Lv, **Feng Lu\***（通讯）, Jianhua Wu, Chongsoon Lim）
📄 [BMVC 2018 原文 PDF](http://bmvc2018.org/contents/papers/0700.pdf) · [项目主页](https://phi-ai.buaa.edu.cn/project/MBLLEN/) · [CV 条目 #53（本人官方履历）](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf)

### 多口径引用数（2026-08-17 查询）

| 论文 | OpenAlex | Semantic Scholar | Google Scholar |
|---|---|---|---|
| **MBLLEN**（BMVC 2018，无 DOI） | 316 | **804**（[API](https://api.semanticscholar.org/graph/v1/paper/70cb4bdd05cccc1f99cf690582e66b7637b81da7?fields=title,citationCount)） | **1121**（[作者 GS 档案快照](https://scholar.google.com/citations?user=arEREYsAAAAJ&hl=en)） |
| Understanding Adversarial Attacks on DL Based Medical Image Analysis Systems（PR 2021） | **544**（[API](https://api.openalex.org/works/W3021182036)） | 555（[API](https://api.semanticscholar.org/graph/v1/paper/DOI:10.1016/j.patcog.2020.107332?fields=citationCount)） | 未获取（GS 快照检索未命中；GS ≥ S2，方向性说明） |
| Reflection Backdoor（ECCV 2020） | 469 | 621 | 未获取（同上） |

**裁决**：三口径中两票（S2、GS）MBLLEN 居首且幅度大（804 vs 555；1121 vs ≈700 量级），**MBLLEN 为陆峰被引最多的论文**。OpenAlex 口径单独看 PR 对抗论文居首，原因是 OpenAlex 对无 DOI 会议论文的引用追踪偏弱（§11.4 经验规律对 BMVC 类论文失效场景）——非归属错误。

**口径差异 >50% 排查记录（§11.4 强制项）**：
1. **归属问题（已修正）**：OpenAlex 把 MBLLEN 的 "Feng Lu" 挂到同名错误实体 `A5101480749`（浙江师范大学/地理所），本人主实体 `A5050527785` 漏收该篇——以[本人 CV #53](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf)（通讯作者）裁决归属。
2. **主实体污染（已剔除）**：`A5050527785` 混入化学论文（Chem. Mater. 2014 "Bipolar Phenanthroimidazole…"，236 引）——材料学同名者作品，按研究方向 + CV 缺失剔除，不计入作品集。
3. 剔除后各口径第一名不变（污染论文引用数 236 < 两侧第一名）。

---

## 三、结论二：引用者中的院士与杰青

**院士 2 人**（均中国工程院）：

| 姓名 | 单位 | 头衔 |
|---|---|---|
| [戴琼海](https://www.cae.cn/cae/html/main/colys/02548488.html) | 清华大学 | 中国工程院院士（2017，信息与电子工程学部） |
| [吴枫](https://www.cae.cn/cae/html/main/colys/95871425.html) | 中国科学技术大学 | 中国工程院院士（2025）＋国家杰青＋IEEE Fellow |

**杰青 8 人**（吴枫双料计入）：

| 姓名 | 单位 | 杰青证据（责任主体源） |
|---|---|---|
| 吴枫 | 中国科学技术大学 | [USTC 官方主页](https://faculty.ustc.edu.cn/wufeng1/zh_CN/index.htm)（院士见上） |
| [李厚强](http://staff.ustc.edu.cn/~lihq/) | 中国科学技术大学 | [USTC 官方主页](http://staff.ustc.edu.cn/~lihq/)："杰出青年基金获得者（2013）"，兼 IEEE Fellow、长江特聘（2017） |
| [黄庆明](https://people.ucas.ac.cn/~qmhuang) | 中国科学院大学 | [国科大官方主页](https://people.ucas.ac.cn/~qmhuang)：杰青（2010）、IEEE Fellow |
| 孟德宇 | 西安交通大学 | [西交大教师主页](https://gr.xjtu.edu.cn/mengdeyu)；[上海交大会议官方介绍](https://ins.sjtu.edu.cn)："国家杰青、长江特聘"（当选年份未获取） |
| [翟广涛](https://cs.sjtu.edu.cn/jzhspjs/1360.html) | 上海交通大学 | [SJTU 官方招聘页](https://gift.sjtu.edu.cn/post/673)："主持承担国家杰出青年科学基金、优秀青年科学基金项目"，兼 IEEE Fellow（年份未获取） |
| [查正军](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm) | 中国科学技术大学 | [USTC 官方主页](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm)：杰青 + 优青 2016 |
| 卢孝强 | 中科院西安光学精密机械研究所 | [西安光机所官网新闻（2019-12）](http://www.xab.ac.cn/ttxw/201912/t20191202_5446648.html)："获国家杰出青年科学基金资助" |
| [杨健](https://opt.bit.edu.cn/jsdw/gdrc/218ea1c9a7284901ab464121d8a83bde.htm) | 北京理工大学（光电学院） | [北理工官方教师主页](https://opt.bit.edu.cn/jsdw/gdrc/218ea1c9a7284901ab464121d8a83bde.htm)：国家杰青、国家级领军人才 |

### 逐条证据卡（三层齐全才计入）

**1. 戴琼海（清华大学，中国工程院院士 2017）**
- 📄 引用论文：[DarkVision: A Benchmark for Low-light Image/Video Perception（arXiv 2023）](https://doi.org/10.48550/arXiv.2301.06269)（共同作者含 J. Suo 索津莉——其清华计算成像团队长期合作者）；[Computational Imaging and AI（Proc. IEEE）](https://doi.org/10.1109/JPROC.2023.3338272)（共同作者 D. Brady，Duke 计算成像）
- 🆔 池内 S2 作者实体：[author/1491800101](https://www.semanticscholar.org/author/1491800101)；OpenAlex：[A5080722708](https://api.openalex.org/authors/A5080722708?select=display_name,summary_stats)
- 🏅 院士：[中国工程院官网戴琼海院士页](https://www.cae.cn/cae/html/main/colys/02548488.html) · [清华大学信息学院当选新闻（2017-11-27）](https://www.sist.tsinghua.edu.cn/info/1091/2300.htm) · [工程院 2017 增选结果](https://www.cae.cn/cae/html/main/col1/2017-11/27/20171127085546389185716_1.html)
- 身份指纹：计算成像/低照度感知方向 + 清华署名背景 + 上述合作者网络 → 匹配度高（confirmed）

**2. 吴枫（中国科学技术大学，中国工程院院士 2025 ＋ 杰青）**
- 📄 引用论文：[Unsupervised Underexposed Image Enhancement via Self-Illuminated and Perceptual Guidance（IEEE TMM 2023）](https://doi.org/10.1109/TMM.2022.3193059)
- 🆔 池内 S2 作者实体：[author/144864333](https://www.semanticscholar.org/author/144864333)；USTC 邮箱域 fengwu@ustc.edu.cn（[先研院介绍页](https://iat.ustc.edu.cn/iat/xnds535/20211207/5362.html)）
- 🏅 院士：[中国工程院官网吴枫院士页](https://www.cae.cn/cae/html/main/colys/95871425.html)（"2025 年当选中国工程院院士"）· [中国科大官方喜报](https://news.ustc.edu.cn/info/1001/93281.htm)；杰青：[USTC 教师主页](https://faculty.ustc.edu.cn/wufeng1/zh_CN/index.htm)（年份未获取，多年源一致 → confirmed）
- ⚠️ 陷阱记录：百度百科称"2023 年当选院士"为**错误年份**，以工程院官网（2025）为准

**3. 李厚强（中国科学技术大学，杰青 2013）**
- 📄 引用论文：[Low-Light Video Enhancement with Synthetic Event Guidance（AAAI 2022）](https://doi.org/10.48550/arXiv.2208.11014)
- 🆔 池内 S2 作者实体：[author/2108508109](https://www.semanticscholar.org/author/2108508109)；OpenAlex：[A5078141810](https://api.openalex.org/authors/A5078141810?select=display_name,summary_stats)
- 🏅 杰青：[USTC 官方主页](http://staff.ustc.edu.cn/~lihq/)："国家'杰出青年基金'获得者（2013）"，兼 IEEE Fellow、长江特聘（2017）

**4. 黄庆明（中国科学院大学，杰青 2010）**
- 📄 引用论文（4 篇，例）：[Unsupervised Low-Light Video Enhancement With Spatial-Temporal Co-Attention Transformer（IEEE TIP 2023）](https://doi.org/10.1109/TIP.2023.3301332)、[Adaptive Multi-Exposure Image Correction（ACM TOMM 2025）](https://doi.org/10.1145/3735973)
- 🆔 池内 S2 作者实体：[author/2237597856](https://www.semanticscholar.org/author/2237597856)
- 🏅 杰青：[国科大官方主页](https://people.ucas.ac.cn/~qmhuang)："国家杰出青年科学基金获得者"（2010），兼 IEEE Fellow

**5. 孟德宇（西安交通大学，杰青）**
- 📄 引用论文（3 篇，例）：[Low-Light Image Enhancement by Retinex-Based Algorithm Unrolling（IEEE TNNLS 2023）](https://doi.org/10.1109/TNNLS.2023.3289626)、[Degradation-Guided cross-consistent deep unfolding network（Neural Networks 2025）](https://doi.org/10.1016/j.neunet.2025.107700)
- 🆔 池内 S2 作者实体：[author/2324996221](https://www.semanticscholar.org/author/2324996221)
- 🏅 杰青：[西交大教师主页](https://gr.xjtu.edu.cn/mengdeyu) + 上海交大会议官方介绍"国家杰青、长江特聘"（**当选年份未获取**；两条独立介绍一致 → confirmed，年份缺口已标注）

**6. 翟广涛（上海交通大学，杰青）**
- 📄 引用论文（6 篇，例）：[GLARE: Low Light Image Enhancement via Generative Latent Feature based Codebook Retrieval（ECCV 2024）](https://doi.org/10.48550/arXiv.2407.12431)、[Light-VQA+（2024）](https://doi.org/10.48550/arXiv.2405.03333)
- 🆔 池内 S2 作者实体：[author/2266393212](https://www.semanticscholar.org/author/2266393212)
- 🏅 杰青：[SJTU 官方招聘页](https://gift.sjtu.edu.cn/post/673)"主持承担国家杰出青年科学基金、优秀青年科学基金项目"（**年份未获取**）；佐证 [SJTU 电院新闻](https://www.seiee.sjtu.edu.cn/index_news/1281.html)（2021 高被引，官方域名）

**7. 查正军（中国科学技术大学，杰青）**
- 📄 引用论文：[Illumination-Invariant Person Re-Identification（ACM MM 2019）](https://doi.org/10.1145/3343031.3350994)
- 🆔 池内 S2 作者实体：[author/143962510](https://www.semanticscholar.org/author/143962510)
- 🏅 杰青：[USTC 官方中文主页](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm)："国家杰出青年科学基金获得者、国家优秀青年科学基金获得者（2016）"（该链接经上一任务 Reflection Backdoor 引用者画像核验复用）

**8. 卢孝强（中科院西安光机所，杰青 2019）**
- 📄 引用论文：[Attention-Based Multi-Branch Network for Low-Light Image Enhancement（ICBAIE 2021）](https://doi.org/10.1109/ICBAIE52039.2021.9389960)（OpenAlex 署名带 [ORCID 0000-0002-7037-5188](https://orcid.org/0000-0002-7037-5188)；共同作者郑向涛——西安光机所智能光学团队长期合作者）
- 🆔 池内 S2 作者实体：[author/7828998](https://www.semanticscholar.org/author/7828998)
- 🏅 杰青：[西安光机所官网新闻（2019-12-02）](http://www.xab.ac.cn/ttxw/201912/t20191202_5446648.html)"卢孝强研究员获国家杰出青年科学基金资助"
- ⚠️ 名字陷阱：中文为"卢**孝**强"（Xiaoqiang Lu），非"晓强"

**9. 杨健（北京理工大学光电学院，杰青）**
- 📄 引用论文：[An automatic framework for endoscopic image restoration and enhancement（Applied Intelligence 2020）](https://doi.org/10.1007/s10489-020-01735-8)（共同作者宋红 Hong Song——北理工同事；医学影像方向与其"医学图像处理/手术导航"方向一致）
- 🆔 池内 S2 作者实体：[author/2146236970](https://www.semanticscholar.org/author/2146236970)
- 🏅 杰青：[北理工光电学院官方教师主页](https://opt.bit.edu.cn/jsdw/gdrc/218ea1c9a7284901ab464121d8a83bde.htm)"国家杰出青年科学基金获得者、国家级领军人才"
- ⚠️ 同名陷阱：池中另有**南京理工大学杨健**（[MambaLLIE, NeurIPS 2024](https://doi.org/10.48550/arXiv.2410.08063) 等论文，合作者钱建军等南理工 PCA 团队）——**非同一人，头衔结论只适用于北理工杨健**

---

## 四、排除与陷阱记录（负结论同样给证据）

| 候选 | 排除原因 | 证据 |
|---|---|---|
| 马佳义（武汉大学） | 人才称号为"万人计划青年拔尖 + **湖北省杰青（省级）**"，非国家杰青 | [中原工学院讲座介绍（官方）](https://www.zut.edu.cn/info/1044/26713.htm)；OA 机构字段 Wuhan University 一致 |
| 贾佳亚（港科大/思谋科技） | 多轮检索无国家杰青记录；持 IEEE Fellow（境外任职期为主） | [中关村论坛官方简介](https://www.zgcforum.com.cn/review/guest/t7901/318669)；科学网历年杰青名单未见 |
| 沈建冰（澳门大学，原北理工） | 无国家杰青记录；IEEE Fellow（2024）。名字正确写法"沈建**冰**" | [IEEE 中国 2024 Fellow 公告](https://cn.ieee.org/2023/11/28/ieee-2024%25E6%2596%25B0%25E6%2599%258Bfellow%25E5%2590%258D%25E5%258D%2595%25E6%25AD%25A3%25E5%25BC%258F%25E5%2585%25AC%25E5%25B8%2583/) |
| 乔宇（上海 AI 实验室） | 所获"**王选**杰出青年学者奖"≠国家自然科学基金杰青；无 NSFC 杰青记录 | [深圳先进院官方页](https://szs.siat.ac.cn/siat/2025-02/21/article_2025022803253088013.html)；[王选奖专访（北大）](https://www.icst.pku.edu.cn/xwgg/xwdt/2024/1201844icst1377595.htm) |
| 周文罡（中科大） | **优青 2018**，非杰青 | [USTC 官方主页](https://faculty.ustc.edu.cn/~qQJJFn/zh_CN/index.htm)（"2018 当选：国家优秀青年基金获得者"） |
| 严骏驰（上海交大） | **优青**，非杰青；IAPR Fellow 2024 | [上交计算机系官方页](https://cs.sjtu.edu.cn/cse/PeopleDetail.aspx?id=400)（"国家基金委优青"） |
| 左旺孟（哈工大） | **优青**，非杰青 | [哈工大 AI 研究院页](https://ai.hit.edu.cn/2021/0410/c13102a252718/pagem.htm)（"主持国自然优青…"） |
| 聂飞平（西北工业大学） | 官方称号"国家级领军人才/青年人才"，无杰青记录 | [西工大 iOPEN 官方页](https://iopen.nwpu.edu.cn/info/1015/1212.htm) |
| 刘家瑛（北京大学） | 2018 年所获为**石青云女科学家奖-青年奖**；杰青无记录，优青本次未获权威确认 | [北大王选所获奖页](https://www.icst.pku.edu.cn/kxyj/kycg/jlry/1201844icst1222372.htm) |
| 施柏鑫（北京大学） | "博雅青年学者"研究员；杰青/优青均未见官方记录 | [北大计算机学院官方页](https://cs.pku.edu.cn/info/1078/1674.htm) |
| Ke Chen（同名存疑） | 引用论文为低层级 ICPECA 2023 会议，机构指纹不足以匹配中科大陈柯（计算摄像）→ 整条存疑排除 | 引用论文 DOI 10.1109/ICPECA56706.2023.10075845 |
| 南京理工杨健 | 与北理工杰青杨健同名不同人（见证据卡 9 陷阱记录） | MambaLLIE 作者网络（钱建军等） |
| Feng Lu（池中"引用者"） | 陆峰本人在后续论文中自引 MBLLEN（自引不计入"引用者"） | `s2_author_details.json` |
| Luc Van Gool（ETH/INSAIT） | 外籍院士排查：两院外籍名录检索未命中（not_found_after_search，≠ 不存在） | [中科院外籍院士名录](https://casad.cas.cn/yszx/) · [工程院外籍院士名单](https://www.cae.cn/cae/html/main/col50/column_50_1.html) |
| 预置院士名单 16 人未命中 | 高文/谭铁牛/郑南宁/徐宗本/鄂维南/怀进鹏/赵沁平/丁汉/李学龙/管晓宏/张艳宁/王耀威/房建成/姜会林/郑海荣/骆清铭均不在本池（前二人组在 Reflection Backdoor 引用池，社区不同） | `suspect_hits.json`（grep 全池可复验） |
| 预置杰青名单未命中 | 陈熙霖/山世光/黄铁军/王国仁/夏元清/张新鹏/冯丹/金海/林宙辰/卢策吾/王亮/田永鸿/胡事民/周志华/沈定刚/田捷等不在本池 | 同上 |

---

## 五、口径与局限

1. **引用列表口径**：结论覆盖 **S2 全量 804 篇引用**（与 citationCount 严格一致）∪ OpenAlex 315 篇；GS 口径（1121）未逐篇反查——GS 口径中可能存在 S2/OpenAlex 均未收录的引用者。引用者姓名池 3241 个（S2 3135 + OA 展平）。
2. **头衔判定边界**：杰青 2024 年起基金委不再公布完整名单，2024-2025 年新当选者只能靠单位官网披露发现——**可能存在漏检**（尤其年轻杰青）。院士名单为封闭名录（两院官网可枚举），漏检风险低。
3. **四值置信度**：院士 2 人均 **confirmed**（工程院官网直接证据 + 单位官方佐证）；杰青 8 人中 6 人 **confirmed**（单位官网直接陈述），孟德宇、翟广涛为 **confirmed（年份缺口标注）**；吴枫杰青为 **likely→confirmed**（USTC 官方域页面 + 百科一致，当选年份未获取）。
4. **工具偏差记录**：`paper trace-citing` 对无 DOI 论文无法路由 S2（CLI 正确 PARTIAL 上报），S2 口径由 API 直连补全（等价数据，已归档）；S2 作者实体碎片化（同一人多 authorId），画像 h 指数取全池排序而非单实体；`trace-profiles` 后台运行耗时较长，本报告画像层以 S2 `/author/batch`（3135 人全量）等价实现。
5. **数据快照日**：2026-08-17。头衔/名单/引用数均为该日快照。
