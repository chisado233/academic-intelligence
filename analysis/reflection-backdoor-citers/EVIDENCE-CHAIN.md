# 证据链：引用 Reflection Backdoor 的学者中的院士与杰青

> ⚠️ **勘误（2026-08-17，任务完成当日）**：本报告§任务背景中"Reflection Backdoor 是陆峰被引最多的论文"这一前提**有误**。后续核验证实 **MBLLEN**（BMVC 2018，无 DOI，陆峰通讯作者，[作者名单见 OpenAlex](https://api.openalex.org/works/W2893333553?select=display_name,authorships)、[本人 CV](https://phi-ai.buaa.edu.cn/members/CV_Beihang_Lufeng.pdf)）才是其被引最多的论文（GS 1121 / S2 804，均高于 Reflection Backdoor 的 ~961 / 621）。本报告当时**凭记忆错误排除了 MBLLEN**（违反 §8 漏检核验规则，该规则已因此收紧）。**本报告的引用者画像部分仍然有效**（针对 Reflection Backdoor 这一种子的结论自洽）；MBLLEN 引用者的院士/杰青名单见 `analysis/beihang-lufeng-top-citers/EVIDENCE-CHAIN.md`。

> **任务**：查 Reflection Backdoor: A Natural Backdoor Attack on Deep Neural Networks（ECCV 2020，北航陆峰团队）的引用者中，谁是杰青、谁是院士。
> **查询日期**：2026-08-17 · **方法**：paper-research-crawler skill §11（引用者画像）+ §10（学术资历核验方法论，`docs/titles-source-map.md`）
> **结论**：院士 **2** 人，杰青 **10** 人（含 1 人双料）。
>
> 本目录数据文件：`citing.csv`（468 篇引用论文）· `authors.csv`（1774 行署名作者）· `authors_cn.csv`（822 位中国机构唯一作者）· `all_profiles.json`（777 份 OpenAlex 画像）· `evidence_papers.json`（确认者的引用论文证据）

---

## 一、链路总览（每步可复现、可验证）

```text
⑴ 锁种子论文 ──► ⑵ 反查引用论文(468) ──► ⑶ 展平作者(1774/1614唯一)
     │                                            │
     │                                            ▼
     │                                   ⑷ 中国机构子集(822) + OpenAlex批量画像(777)
     │                                            │
     │                     ┌──────────────────────┴──────────────────────┐
     │                     ▼                                             ▼
     │             ⑸a 大牛观察名单 grep                            ⑸b h指数≥40 全量扫描
     │                     └──────────────────────┬──────────────────────┘
     │                                            ▼
     │                              ⑹ 身份指纹核验（引用论文署名机构 == 头衔持有人单位）
     │                                            ▼
     └────────────────────────────—— ⑺ 头衔核验（责任主体源：两院名录 / 单位官网）
                                                  ▼
                                    ⑻ 负结论与同名陷阱排除 ──► 本报告
```

### 步骤明细

| # | 操作 | 命令 / 手段 | 产出 | 一键验证 |
|---|------|------------|------|---------|
| 1 | 锁定种子论文 | `paper source openalex search "Reflection Backdoor"` | 种子 = [W3107337211](https://api.openalex.org/works/W3107337211?select=id,display_name,cited_by_count,publication_year) | [OpenAlex 实体](https://api.openalex.org/works/doi:10.1007/978-3-030-58607-2_11)（cited_by_count=469）· [Springer 正式版](https://link.springer.com/chapter/10.1007/978-3-030-58607-2_11) |
| 2 | 反查引用 | `paper trace-citing 10.1007/978-3-030-58607-2_11 --sources openalex --output citing.csv` | `citing.csv`：468 行 | 复现链接：[OpenAlex filter=cites:W3107337211](https://api.openalex.org/works?filter=cites:W3107337211&per-page=200&select=id,doi,title,publication_year) |
| 3 | 展平作者 | `paper trace-authors citing.csv --output authors.csv` | `authors.csv`：1774 行 / 1614 唯一姓名 / 127 人引用≥2次 | 本地文件 `authors.csv`（列：author_name / appears_in / affiliation / author_id） |
| 4a | 中国机构子集 | python 过滤（机构关键词表） | `authors_cn.csv`：822 唯一（777 带 OpenAlex A-id） | 本地文件 `authors_cn.csv` |
| 4b | 批量画像 | OpenAlex API 多线程逐 ID 拉取（**注**：CLI `trace-profiles` 有 bug——读到了项目 `tmp/` 下另一篇论文的陈旧 CSV，故改直连 API） | `all_profiles.json`：777 份（h 指数/总引用/机构） | 示例：[管晓宏画像](https://api.openalex.org/authors/A5075845093?select=display_name,summary_stats,cited_by_count) |
| 5a | 观察名单 | 预置 19 位两院院士 + 30 位杰青候选人拼音，对 1614 个姓名 grep | 命中 20+ 候选 | `authors.csv` 可复验 |
| 5b | h≥40 全扫描 | 按 `all_profiles.json` 排序，补出低频大牛（黄铁军/夏元清/张艳宁/查正军/王国仁/张新鹏等） | 补充候选 ~15 人 | `all_profiles.json` |
| 6 | 身份指纹 | 逐人核对**引用论文里的署名机构**（citing.csv 的 authors_detail JSON），与头衔持有人单位比对 ≥2 特征 | 全部吻合 12 人；**排除 1 个同名陷阱**（见 §四） | 每人证据卡（§三）第一条链接 |
| 7 | 头衔核验 | 院士→两院官方名录；杰青→单位官网名录页（2024 起基金委不再公布完整名单） | 院士 2 / 杰青 10 / 排除 11 | 每人证据卡（§三）第二条链接 |

---

## 二、结论表（名字即链接，指向权威头衔来源）

**院士（2 人）**

| 学者 | 单位 | 头衔证据 |
|---|---|---|
| [管晓宏](https://casad.cas.cn/ysxx2022/ysmd/xxjs/201711/t20171129_4625087.html) | 西安交通大学 | 中国科学院院士 2017（信息技术科学部）；兼杰青 1997 |
| [张艳宁](https://jsj.nwpu.edu.cn/info/1598/27065.htm) | 西北工业大学 | 中国科学院院士 2025（2025-11-21 公布） |

**杰青（10 人，管晓宏双料）**

| 学者 | 单位 | 头衔证据 |
|---|---|---|
| [管晓宏](https://casad.cas.cn/ysxx2022/ysmd/xxjs/201711/t20171129_4625087.html) | 西安交通大学 | [杰青 1997（清华自动化系介绍）](https://www.au.tsinghua.edu.cn/info/1076/3185.htm) |
| [冯丹](https://www.cae.cn/cae/html/main/col245/2025-08/20/20250820205538931177768_1.html) | 华中科技大学副校长 | 杰青 + 长江 + IEEE Fellow（CAE 2025 候选人官方公告履历） |
| [金海](https://ccf.org.cn/BigData2025/news_d_2339) | 华中科技大学 | [杰青 2001（CCF 官方介绍）](https://ccf.org.cn/BigData2025/news_d_2339) |
| [陈熙霖](https://www.ict.ac.cn/yjdw/gjjcqnkxjjhdz/) | 中科院计算所所长 | [杰青 2010（计算所官方杰青名单页）](https://www.ict.ac.cn/yjdw/gjjcqnkxjjhdz/) |
| [山世光](https://www.ict.ac.cn/sourcedb/cn/jssrck/200909/t20090917_2496706.html) | 中科院计算所 | [杰青 2022（计算所官方个人页）](https://www.ict.ac.cn/sourcedb/cn/jssrck/200909/t20090917_2496706.html) |
| [黄铁军](https://www.ai.pku.edu.cn/info/1139/1243.htm) | 北京大学 | 杰青 + 长江特聘（北大 AI 研究院官方页） |
| [查正军](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm) | 中国科学技术大学 | 杰青 + 优青 2016（USTC 官方主页） |
| [王国仁](https://cs.bit.edu.cn/szdw/jsml/bssds/1f3b4eb54a2545caa3b2cee7962737a2.htm) | 北京理工大学计算机学院院长 | [杰青 2010（北理工官方博导页）](https://cs.bit.edu.cn/szdw/jsml/bssds/1f3b4eb54a2545caa3b2cee7962737a2.htm) |
| [夏元清](https://renshichu.bit.edu.cn/2019gb1/mxms/jcrc/index.htm) | 北京理工/中原工学院校长 | [杰青 2012（北理工人事处杰出人才名单）](https://renshichu.bit.edu.cn/2019gb1/mxms/jcrc/index.htm) |
| [张新鹏](https://ai.fudan.edu.cn/zxp/list.htm) | 复旦大学/上海大学 | [杰青 2015（复旦官方教师页）](https://ai.fudan.edu.cn/zxp/list.htm) |

---

## 三、逐人证据卡（三层证据：引用论文 → OpenAlex 实体 → 头衔来源）

> 每张卡逻辑：**该学者署名于某篇引用论文（DOI 可点开验证署名机构）→ OpenAlex 作者实体（机构/h指数吻合）→ 责任主体源确认头衔**。三层齐全才计入结论。

### 院士

**1. 管晓宏（西安交通大学，中科院院士 2017 + 杰青 1997）**
- 📄 引用论文：[De²Trojan: Deployable Trojan Analysis Tool and Benchmark（IEEE TIFS 2025）](https://doi.org/10.1109/tifs.2025.3632218)，署名 School of Electronic and Information Engineering, Xi'an Jiaotong University（与其电子与信息学部主任身份吻合，合作者含西交沈超、武大王骞）
- 🆔 OpenAlex：[A5075845093](https://api.openalex.org/authors/A5075845093?select=display_name,summary_stats,last_known_institutions)
- 🏅 头衔：[中科院学部官方页](https://casad.cas.cn/ysxx2022/ysmd/xxjs/201711/t20171129_4625087.html)（院士）· [清华自动化系双聘介绍](https://www.au.tsinghua.edu.cn/info/1076/3185.htm)（杰青 1997）· 佐证：[西交新闻网](http://news.xjtu.edu.cn/info/1659/232481.htm)

**2. 张艳宁（西北工业大学，中科院院士 2025）**
- 📄 引用论文：[Contrastive Neuron Pruning for Backdoor Defense（IEEE TIP 2025）](https://doi.org/10.1109/tip.2025.3539466)，署名 Northwestern Polytechnical University（国家工程实验室）
- 🆔 OpenAlex：[A5028235866](https://api.openalex.org/authors/A5028235866?select=display_name,summary_stats,last_known_institutions)
- 🏅 头衔：[西工大计算机学院"张艳宁教授当选中国科学院院士"（2025-11-21）](https://jsj.nwpu.edu.cn/info/1598/27065.htm) · 佐证：[西工大官网校领导页](https://www.nwpu.edu.cn/info/6308/77328.htm)（常务副校长）
- ⚠️ 其杰青身份仅有[学院科研页](https://jsj.nwpu.edu.cn/snew/kxyj.htm)线索（2008），官方当选新闻未列 → 本报告仅确认院士，杰青标注 *未确认*

### 杰青

**3. 冯丹（华中科技大学副校长，杰青）**
- 📄 引用论文：[Physical Backdoor（CVPR 2024）](https://doi.org/10.1109/cvpr52733.2024.01210) + [Backdoor Attacks on Bimodal Salient Object Detection（ACM MM 2024）](https://doi.org/10.1145/3664647.3681096)，署名 Wuhan National Laboratory for Optoelectronics / HUST（其信息存储研究部）
- 🆔 OpenAlex：[A5102658992](https://api.openalex.org/authors/A5102658992?select=display_name,summary_stats)（注意实体有拆分：另有 A5057421680）
- 🏅 杰青：[CAE 2025 有效候选人官方公告](https://www.cae.cn/cae/html/main/col245/2025-08/20/20250820205538931177768_1.html)履历栏 · 佐证：[华中大信息存储实验室官网](https://storage.hust.edu.cn/sysgk/zrjj.htm)
- ❌ 非院士：[CAE 2025 当选名单（71 人）](https://www.cae.cn/cae/html/main/col245/2025-11/21/20251121085534729719452_1.html)信息与电子工程学部 9 人中无冯丹（已逐条核对）

**4. 金海（华中科技大学，杰青 2001）**
- 📄 引用论文：[BadHash（ACM MM 2022）](https://doi.org/10.1145/3503161.3548272)，署名 HUST
- 🆔 OpenAlex：[A5022262922](https://api.openalex.org/authors/A5022262922?select=display_name,summary_stats)
- 🏅 杰青：[CCF BigData2025 官方介绍](https://ccf.org.cn/BigData2025/news_d_2339) · 佐证：[百度百科（2001 年）](https://baike.baidu.com/item/%E9%87%91%E6%B5%B7/9243017)

**5. 陈熙霖（中科院计算所所长，杰青 2010）**
- 📄 引用论文：[T2IShield（ECCV 2024）](https://doi.org/10.1007/978-3-031-73013-9_7) + [Dynamic Attention Analysis（IEEE TPAMI 2025）](https://doi.org/10.1109/tpami.2025.3644016)，署名 Key Laboratory of AI Safety of CAS, ICT
- 🆔 OpenAlex：[A5083420537](https://api.openalex.org/authors/A5083420537?select=display_name,summary_stats)
- 🏅 杰青：[中科院计算所"国家杰出青年科学基金获得者"名单页](https://www.ict.ac.cn/yjdw/gjjcqnkxjjhdz/)（2010）· [计算所个人页](https://www.ict.ac.cn/sourcedb/cn/jssrck/200909/t20090917_2496595.html)

**6. 山世光（中科院计算所，杰青 2022）**
- 📄 引用论文：同陈熙霖两篇（T2IShield / Dynamic Attention Analysis），署名 ICT CAS
- 🆔 OpenAlex：[A5050297728](https://api.openalex.org/authors/A5050297728?select=display_name,summary_stats)
- 🏅 杰青：[计算所官方个人页](https://www.ict.ac.cn/sourcedb/cn/jssrck/200909/t20090917_2496706.html)（2022）· 佐证：[国科大主页](https://people.ucas.ac.cn/~sgshan)

**7. 黄铁军（北京大学，杰青 + 长江特聘）**
- 📄 引用论文：[Neuromorphic computing paradigms enhance robustness（Nature Communications 2025）](https://doi.org/10.1038/s41467-025-65197-x)，署名 State Key Laboratory of Multimedia Information Processing, PKU（其任主任的多媒体信息处理全国重点实验室）
- 🆔 OpenAlex：[A5058066577](https://api.openalex.org/authors/A5058066577?select=display_name,summary_stats)
- 🏅 杰青：[北大人工智能研究院官方页](https://www.ai.pku.edu.cn/info/1139/1243.htm)（"教授、长江特聘、国家杰青"）
- ❌ 非院士：2023 与 2025 两届 CAE 有效候选人（[2025 候选名单](https://www.cae.cn/cae/html/main/col245/2025-08/20/20250820205538931177768_1.html)）均未当选（[CAE 2025 当选名单](https://www.cae.cn/cae/html/main/col245/2025-11/21/20251121085534729719452_1.html)无此人）

**8. 查正军（中国科学技术大学，杰青）**
- 📄 引用论文：[Revisiting Single Image Reflection Removal in the Wild（CVPR 2024）](https://doi.org/10.1109/cvpr52733.2024.02406)，署名 USTC
- 🆔 OpenAlex：h=73 实体（见 `all_profiles.json`）
- 🏅 杰青：[USTC 官方中文主页](https://faculty.ustc.edu.cn/chazhengjun/zh_CN/index.htm)（"国家杰出青年科学基金获得者、国家优秀青年科学基金获得者"）

**9. 王国仁（北京理工大学计算机学院院长，杰青 2010）**
- 📄 引用论文：[Backdoor Attacks on Graph Classification（2025）](https://doi.org/10.1007/978-3-032-06066-2_16)，署名 Beijing Institute of Technology
- 🆔 OpenAlex：[A5054991337](https://api.openalex.org/authors/A5054991337?select=display_name,summary_stats)
- 🏅 杰青：[北理工计算机学院官方博导页](https://cs.bit.edu.cn/szdw/jsml/bssds/1f3b4eb54a2545caa3b2cee7962737a2.htm)（"2010 年获国家杰出青年科学基金"）

**10. 夏元清（北京理工大学/中原工学院校长，杰青 2012）**
- 📄 引用论文：[PerVK: A Robust Personalized Federated Framework（IEEE TII 2023）](https://doi.org/10.1109/tii.2023.3329688)，署名 School of Automation, BIT（其自动化学院学科带头人身份）
- 🆔 OpenAlex：[A5064231378](https://api.openalex.org/authors/A5064231378?select=display_name,summary_stats)
- 🏅 杰青：[北理工人事处"杰出人才"名单页](https://renshichu.bit.edu.cn/2019gb1/mxms/jcrc/index.htm)（2012）· 佐证：[北理工 Pure 主页](https://pure.bit.edu.cn/zh/persons/yuanqing-xia/)

**11. 张新鹏（复旦大学/上海大学，杰青 2015）**
- 📄 引用论文：[Physical Invisible Backdoor Based on Camera Imaging（ACM MM 2023）](https://doi.org/10.1145/3581783.3612476)，署名 Fudan University（2023 年已调入复旦）
- 🆔 OpenAlex：[A5101889358](https://api.openalex.org/authors/A5101889358?select=display_name,summary_stats)
- 🏅 杰青：[复旦计算与智能创新学院官方教师页](https://ai.fudan.edu.cn/zxp/list.htm)（"教授、国家杰出青年科学基金获得者"）· [项目页（2016-2020 杰青项目）](https://ai.fudan.edu.cn/3e/d1/c25921a278225/page.htm)

---

## 四、排除与陷阱记录（负结论同样可验证）

| 候选 | 排除原因 | 证据 |
|---|---|---|
| 王骞（武大网安） | 优青非杰青 | [武大网安学院页](http://www.lianpp.com/whu/smu_cse/info/3501/37731.htm)（"优秀青年科学基金"） |
| 沈超（西交） | 优青 2018 非杰青 | [西交官方教师页](http://www.xjtu.edu.cn/jsnr.jsp?wbtreeid=1632&wbwbxjtuteacherid=1790) |
| 姜育刚（复旦副校长） | 优青 2016 非杰青 | [百度百科](https://baike.baidu.com/item/%E5%A7%9C%E8%82%B2%E5%88%9A/22131913) |
| 纪守领（浙大） | 浙江省杰青（省级）非国家杰青 | [CNCC2018 官方介绍](https://cncc2018.ccf.org.cn/cms/news/100000/0000000001/2018/9/25/5161a0ad76744ca78c1d103c789984ff.shtml) |
| 张玉清（西电） | 官方页未列杰青 | [西电官方主页](https://web.xidian.edu.cn/yqzhang/) |
| 陈晓峰（西电） | 青年长江（2015）非杰青 | [西电官方主页](https://web.xidian.edu.cn/xfchen/) |
| 孙茂松（清华） | 杰青无权威来源（清华官方页未列；个别论坛简历提及，按方法论以官方页裁决） | [清华计算机系官方页](https://www.cs.tsinghua.edu.cn/info/1121/3554.htm) |
| 俞能海（中科大） | 官方页未列杰青 | [USTC 主页](http://staff.ustc.edu.cn/~ynh/) |
| 任奎（浙大） | ACM/IEEE Fellow、海外高层次人才，无国家杰青记录 | [浙大主页](https://person.zju.edu.cn/en/kuiren) |
| 陈松灿（南航） | 仅"参与"过杰青项目，非获得者 | [山大报告通知](https://www.sc.sdu.edu.cn/info/1032/1422.htm) |
| 孙栩（北大） | 国家级青年人才（非杰青） | [北大计算机学院页](https://cs.pku.edu.cn/info/1090/1785.htm) |
| **Dezhong Yao（同名陷阱）** | 引用论文 [DarkHash 2025](https://doi.org/10.1109/tifs.2025.3632218) 署名**华中科技大学**，与电子科大杰青**尧德中**（UESTC 生命科学学院）单位不符 → 判非同一人，整条排除 | [电子科大尧德中主页](https://faculty.uestc.edu.cn/yaodezhong/zh_CN/index.htm) vs DarkHash 署名机构 |
| 杨强（HKUST/微众银行） | 境外机构（IEEE/ACM/AAAI Fellow），不持国家杰青/两院院士 | [引用论文](https://doi.org/10.1109/tifs.2025.3632218) 署名 HKUST/WeBank |
| Song Guo（HKUST） | 境外机构 | 引用论文署名 HKUST（`citing.csv` Unleashing Continual Learning 2025 行） |

---

## 五、口径与局限（结论的适用边界）

1. **引用列表口径**：OpenAlex 468/469 篇；Semantic Scholar 口径 621 篇，其中约 150 篇 S2 收录而 OpenAlex 未收录的引用论文**未纳入扫描**；Google Scholar 口径（~961，版本聚类）未反查。理论上上述渠道中可能存在未发现的院士/杰青。
2. **杰青名单时效**：2024 年起国家自然科学基金委不再公布完整杰青名单，本报告杰青判断全部基于**单位官网/官方介绍页**（次级直接证据），未做基金委系统内部核验；置信度标注为 confirmed（有单位官方页直接陈述）。
3. **OpenAlex 实体质量**：作者实体的 last_known_institutions 存在同名合并污染（如 Qian Wang 实体挂到 Mahasarakham University），因此**身份判定一律以引用论文署名机构（authors_detail）为准**，不依赖实体机构字段。
4. **数据快照日**：2026-08-17。头衔/名单为该日快照，院士名单动态更新。
5. **工具偏差记录**：CLI `trace-profiles` 读取了项目 `tmp/` 下另一论文的陈旧 CSV（bug），本次步骤 4b 改用 OpenAlex API 直连（等价实现），产出 `all_profiles.json`。
