# 官方 API 直连手册（API Direct Manual）

> 用途：给 agent 提供绕过 CLI、**直接调用学术源官方 API** 的参考手册（信息反向挖掘场景的进阶工具，与 SKILL.md §11 配套）。
> 定位：CLI（`paper`）是首选入口；本手册用于 CLI 未覆盖的高级查询、以及需要手工验证/补数据的场景。
> 原则：**每条示例带合规注释；凭证从环境变量读取，不硬编码；限速礼貌（默认 1 rps）；API 没有的信息不假装提供。**
> 更新：2026-08-11

---

## 0. 通用坑（跨源，必读）

| 坑 | 现象 | 处理 |
|---|---|---|
| **urllib 无超时会挂起** | 用 `urllib.request` 拉数据，网络抖动时进程永久阻塞 | 用 `httpx`（`timeout=...`），或给 urllib 设 `socket.setdefaulttimeout`；本项目统一用 `httpx` |
| **GBK 编码**（Windows 中文输出） | 中文标题/机构名在 Windows 控制台输出乱码 | 脚本开头 `sys.stdout.reconfigure(encoding="utf-8")`；CSV 输出加 UTF-8 BOM 兼容 Excel |
| **批量拉取要分页 + 限速** | 一次性拉全量 → 超时 / 429 / 被打爆 | cursor 分页 + 每请求间限速（1 rps）；大批量加断点续传 |
| 429 无处理 | 直接抛异常中断整批 | 尊重 `Retry-After` 头做指数退避；单源失败 fail-soft 到其他源 |
| 凭证硬编码 | 泄露风险 + 无法轮换 | 一律从环境变量读（`OPENALEX_EMAIL` / `CROSSREF_MAILTO` / `SEMANTIC_SCHOLAR_API_KEY`） |

---

## 1. OpenAlex（重点：信息反向挖掘的主源）

官方文档：<https://docs.openalex.org/>

### 1.1 端点与参数

| 项 | 内容 |
|---|---|
| Base URL | `https://api.openalex.org` |
| **works 端点** | `GET /works`（作品搜索/过滤）；`GET /works/{id}`（按 W-id）；`GET /works/https://doi.org/{doi}`（按 DOI） |
| **authors 端点** | `GET /authors`（作者搜索）；`GET /authors/{id}`（作者档案） |
| 认证 | **无 key**（免费公开）；礼貌池：加 `mailto=<邮箱>` 参数（env `OPENALEX_EMAIL`），官方据此联系异常用量 |
| 分页 | **cursor 分页**：`cursor` 参数，**首页必须 `cursor=*`**；`per-page` 最大 200（推荐 200） |
| 限速 | 无 key 约 10 req/s、10 万 req/天；带 mailto 礼貌池日额度大幅放宽（仍勿并行打爆） |
| 关键 filter | `cites:Wxxx`（反向引用）、`author.id:Axxx`（作者作品）、`raw_affiliation_strings.search:`（机构过滤）、`doi:` / `ids.arxiv:` / `publication_year:` / `type:` |
| 排序 | `sort=cited_by_count:desc` 等 |
| 注意 | 摘要为 **inverted index**（`abstract_inverted_index`），需按位置重建；引用数是 `cited_by_count` |

### 1.2 高级查询示例（含合规注释）

```bash
# ① 反向引用：谁引用了 W2257979135（分页，cursor 从 * 开始）
#    合规：免费公开 API；限速礼貌；mailto 入礼貌池
curl -G "https://api.openalex.org/works" \
  --data-urlencode "filter=cites:W2257979135" \
  --data-urlencode "per-page=200" \
  --data-urlencode "cursor=*" \
  --data-urlencode "mailto=${OPENALEX_EMAIL}"

# ② 作者作品：A5110986785 的全部论文（按被引数降序）
#    合规：同上；author.id 精确过滤避免同名噪声
curl -G "https://api.openalex.org/works" \
  --data-urlencode "filter=author.id:A5110986785" \
  --data-urlencode "sort=cited_by_count:desc" \
  --data-urlencode "per-page=50" \
  --data-urlencode "cursor=*" \
  --data-urlencode "mailto=${OPENALEX_EMAIL}"

# ③ 机构过滤：署名机构含北京航空航天大学的论文
#    合规：raw_affiliation_strings.search 是字符串检索，结果需人工复核（见坑 2）
curl -G "https://api.openalex.org/works" \
  --data-urlencode "filter=raw_affiliation_strings.search:Beijing University of Aeronautics" \
  --data-urlencode "per-page=50" \
  --data-urlencode "cursor=*"

# ④ 作者档案（机构/h-index/作品数/被引）
#    合规：单次单条，礼貌请求
curl -G "https://api.openalex.org/authors/A5110986785" \
  --data-urlencode "mailto=${OPENALEX_EMAIL}"
```

### 1.3 分页循环（cursor 模式，伪代码）

```python
cursor = "*"
while cursor:
    r = httpx.get("https://api.openalex.org/works",
                  params={"filter": "cites:W2257979135",
                          "per-page": 200, "cursor": cursor,
                          "mailto": os.environ["OPENALEX_EMAIL"]},
                  timeout=30)
    if r.status_code == 429:
        time.sleep(int(r.headers.get("Retry-After", "60")))
        continue
    data = r.json()
    for item in data["results"]:
        ...   # 处理
    cursor = data["meta"].get("next_cursor")   # 最后一页为 None，循环结束
    time.sleep(1)                              # 礼貌限速
```

### 1.4 实测坑

1. **`cursor=*` 必须**：不传 cursor 只返回第一页且无法翻页；传错起始值（如空串）会重复/丢页。首页与续页都用 cursor 响应里的 `next_cursor`。
2. **作者同名噪声**：OpenAlex 的 `display_name` 同名者极多（尤其常见中文名/英文名），单靠名字匹配必错。**必须机构 + ID 双校验**：先用 `author.id:` 精确过滤，再对 `last_known_institution`/`raw_affiliation_strings` 与目标机构比对，才能下"同一人"结论。
3. 摘要 inverted index 需按 position 重建，直接取字段会得到字典而非文本。
4. 429 会返回 `Retry-After` 头，必须尊重；无 mailto 时高并发更容易触发。

---

## 2. OpenCitations（COCI）

官方文档：<https://opencitations.net/>（COCI = Crossref Open Citation Index）

### 2.1 端点与参数

| 项 | 内容 |
|---|---|
| Base URL | `https://opencitations.net/index/coci/api/v1` |
| **citations 端点** | `GET /citations/{doi}`——谁引用了该 DOI（返回边的数组，DOI 处于被引侧） |
| **references 端点** | `GET /references/{doi}`——该 DOI 引用了什么（DOI 处于引用侧） |
| 认证 | 无（完全免费，无需 key） |
| 分页 | 无分页参数；一次返回完整 JSON 数组（大数据量 DOI 数组可能很大） |
| 限速 | 无正式硬限，礼貌 1 rps |
| 返回结构 | JSON 数组，每条边 `{"oci","citing","cited","creation","timespan","journal_sc","author_sc"}` |

### 2.2 示例（含合规注释）

```bash
# 谁引用了 10.1038/s41586-025-09422-z
#    合规：免费 API；限速礼貌
curl "https://opencitations.net/index/coci/api/v1/citations/10.1038/s41586-025-09422-z"

# 该 DOI 引用了什么（反向引用挖掘时与 OpenAlex cites: 交叉验证）
#    合规：同上
curl "https://opencitations.net/index/coci/api/v1/references/10.1038/s41586-025-09422-z"
```

### 2.3 实测坑

1. **以 DOI 为键，不接受 W-id**：传 OpenAlex 的 `W2257979135` 会报 invalid DOI（COCI 只认 DOI，无元数据能力）。
2. 404 = 该 DOI 无边（不是错误），返回空数组即可。
3. 部分边的 `citing`/`cited` 可能为空串（不完整记录），解析时须跳过，不能当成合法引用对。
4. 只有引用边，**没有**论文元数据（标题/作者名/机构）——元数据要回 OpenAlex/Crossref 补。

---

## 3. Crossref

官方文档：<https://api.crossref.org/>（REST API）

### 3.1 端点与参数

| 项 | 内容 |
|---|---|
| Base URL | `https://api.crossref.org` |
| **works 端点** | `GET /works`（查询，`query.bibliographic=` 模糊检索）；`GET /works/{doi}`（按 DOI 精确取，权威 + publisher） |
| 认证 | 无 key；**polite pool**：加 `mailto=<邮箱>`（env `CROSSREF_MAILTO`），官方据此识别并给稳定配额 |
| 分页 | `rows`（每页条数，最大 1000）+ `offset`（偏移，≤10000） |
| 限速 | 公共池约 50 req/s；礼貌池更稳；本项目预算默认 3 req/s（SKILL.md §5） |
| 返回结构 | 外层 `message.items[]`（列表）或 `message`（单条）；日期在 `published-print/published-online/issued/created` 的 `date-parts` |
| 能力边界 | 只有元数据（含 publisher、引用计数 `is-referenced-by-count`）；**无引用图、无作者档案** |

### 3.2 示例（含合规注释）

```bash
# 按 DOI 精确取（出版社权威字段）
#    合规：polite pool（mailto）；免费公开
curl -G "https://api.crossref.org/works/10.1038/s41586-025-09422-z" \
  --data-urlencode "mailto=${CROSSREF_MAILTO}"

# 模糊检索 + 分页
#    合规：同上；rows/offset 分页，避免一次拉爆
curl -G "https://api.crossref.org/works" \
  --data-urlencode "query.bibliographic=deep learning" \
  --data-urlencode "rows=50" \
  --data-urlencode "offset=0" \
  --data-urlencode "mailto=${CROSSREF_MAILTO}"
```

### 3.3 实测坑

1. `query.bibliographic` 是模糊匹配，精确命中请直接用 DOI 或 `query.title=`/`query.author=` 组合。
2. 作者机构（`affiliation`）覆盖极不全，别指望它做机构核验——机构主源是 OpenAlex。
3. 429 时尊重 `Retry-After`；无 mailto 高并发容易被临时封 IP。
4. 日期字段四种（print/online/issued/created），取年份按"最早可用"解析。

---

## 4. Semantic Scholar（S2）

官方文档：<https://api.semanticscholar.org/api-docs/graph>

### 4.1 端点与参数

| 项 | 内容 |
|---|---|
| Base URL | `https://api.semanticscholar.org/graph/v1` |
| 常用端点 | `GET /paper/search`（搜索）；`GET /paper/{id}`（id 支持 `DOI:xxx` / `ArXiv:xxx`）；`GET /paper/{id}/citations`；`GET /author/search`；`GET /author/{id}/papers` |
| 认证 | 可选 `x-api-key` 头（env `SEMANTIC_SCHOLAR_API_KEY`，免费注册）；无 key 也可用 |
| 参数 | **`fields` 必填**（如 `title,abstract,year,venue,citationCount,externalIds,authors,fieldsOfStudy`），否则返回极少字段；`limit` 搜索端 max 100 |
| 限速 | 无 key：共享池约 100 req/5min（**429 频发**）；有 key：约 1 req/s、10 万 req/天 |
| 能力 | 元数据 + 引用图 + 作者档案（hIndex/citationCount/paperCount/affiliations） |

### 4.2 示例（含合规注释）

```bash
# 按 DOI 取论文（带 fields）
#    合规：免费 API；限速礼貌；有 key 加 x-api-key 头
curl -G "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/s41586-025-09422-z" \
  --data-urlencode "fields=title,abstract,year,venue,citationCount,externalIds,authors"

# 论文的引用者（反向引用另一数据源）
#    合规：同上；citations 数据量大时用 limit + offset 分页
curl -G "https://api.semanticscholar.org/graph/v1/paper/{paperId}/citations" \
  --data-urlencode "fields=citingPaper.paperId,citingPaper.title,citingPaper.authors" \
  --data-urlencode "limit=100"

# 作者搜索（拿到 authorId 后拉档案/作品）
#    合规：同上；同名噪声大，须再核机构
curl -G "https://api.semanticscholar.org/graph/v1/author/search" \
  --data-urlencode "query=Lu Feng" \
  --data-urlencode "fields=authorId,name,affiliations,hIndex"
```

### 4.3 实测坑

1. **429 非常频繁**（尤其无 key 时段），必须指数退避 + 重试，别硬刚；有 429 先降速。
2. `fields` 不传会得到几乎空的结果——每次请求都显式列全所需字段。
3. 搜索端 `limit` 上限 100；作者作品/引用端翻页用 `limit` + `offset`（或新 `page` token）。
4. 部分论文无 `abstract`（`null`），解析时容忍空值。
5. `DOI:` 前缀的 id 大小写敏感；找不到返回 404。

---

## 5. 合规总则（所有源通用）

- **免费 API 全用，限速礼貌**（默认 1 rps）；不搞大规模并行打爆源站
- 不碰付费墙正文；不集成 Sci-Hub/libgen；不自动化爬 Google Scholar；不破解验证码/过盾
- 凭证一律从环境变量读，**不硬编码、不落盘、不提交**
- 尊重各源文档的费率与 `Retry-After`；单源失败 fail-soft 到其他源，全失败才报错
- API 拿不到的信息（中文姓名、头衔、个人主页、最新动向）**一律走 agent web fetch 官方源确认**，参考 `docs/titles-source-map.md`

---

## 6. Semantic Scholar 兜底端点（OpenAlex 429 时）

- 定位论文：`GET https://api.semanticscholar.org/graph/v1/paper/{paperId}?fields=title`（paperId 支持 `DOI:10.xxx` / `ARXIV:1706.03762`）
- 反向引用：`GET https://api.semanticscholar.org/graph/v1/paper/{paperId}/citations?fields=title,authors,year,venue,externalIds&limit=100`（分页 `offset` + 响应 `next`）
- 作者画像：`GET https://api.semanticscholar.org/graph/v1/author/{s2id}?fields=name,affiliations,paperCount,hIndex,citationCount`；s2id 用 `author/search?query={name}` 获取（**同名风险：结果仅作占位，需 agent 消歧确认**）
- 限流：公开 API 100 req/5min（共享 IP），429 时退避，不无限重试
- CLI：`trace-citing`/`trace-profiles` 在 OpenAlex 429/5xx 时自动转 S2（`profile.source="s2"` 标注）

## 7. OpenAlex 免费快照（可选，零额度）

- 来源：`https://openalex.s3.amazonaws.com/data/work/{snapshot_date}/part_000.gz ... part_009.gz`（works 10 分区，季度更新）
- 每行 JSONL（gz）：work 含 `id`/`title`/`publication_year`/`doi`/`cited_by_count`/`referenced_works`/`authorships`
- 引用倒排：`referenced_works` 是"它引用了谁"，建索引时倒转为 `cited_id → citing_id`（`snapshot_citations` 表）
- CLI：`paper snapshot download/build/status/enable/disable`；`trace-citing --use-snapshot` 查本地
- 规模：解压后约 20-60 GB，用户主动选择下载
