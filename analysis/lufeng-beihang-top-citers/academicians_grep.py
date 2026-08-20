"""Grep the citing-author pool against a watchlist of Chinese CV/imaging/multimedia
academicians (pinyin variants). Watchlist only aids discovery — every hit is later
verified against cae.cn / casad.cas.cn official pages (skill §10/§0.5.3)."""
import csv, json

# pinyin name variants of active CAE/CAS academicians in CS/CV/Electronics-adjacent fields
WATCH = {
    "Gao Wen": ["高文"], "Dai Qionghai": ["戴琼海"], "Tan Tieniu": ["谭铁牛"],
    "Zheng Nanning": ["郑南宁"], "Xu Zongben": ["徐宗本"], "Wu Feng": ["吴枫"],
    "Ma Huadong": ["马华东"], "Wang Yaowei": ["王耀威"], "Zhang Qinping": ["赵沁平"],  # typo guard below
    "Zhao Qinping": ["赵沁平"], "Zhang Ping": ["张平"], "Liu Yunhao": ["刘云浩"],
    "He You": ["何友"], "Wu Zhaohui": ["吴朝晖"], "Chen Jie": ["陈杰"],
    "Li Deyi": ["李德毅"], "Lu Jianhua": ["陆建华"], "You Zheng": ["尤政"],
    "Fang Baoxin?": [], "Jiang Changjun": ["蒋昌俊"], "Wang Chenghong?": [],
    "Ding Wenqi?": [], "Liu Ming?": [], "Huang Xiao?": [], "Song Jinping?": [],
    "Tian Qi?": [], "Shen Xubang": ["沈绪榜"], "Li Wei?": [], "Huang Liheng?": [],
    "Zhou Zhihua?": ["周志华"], "Hu Shimin?": ["胡事民"], "Ji Zhen?": [],
    "Zhang Bo": ["张钹"], "Wang Guoping?": [], "Chen Xilin?": ["陈熙霖"],
    "Shan Shiguang?": ["山世光"], "Huang Tiejun?": ["黄铁军"], "Tian Yonghong?": [],
    "Wang Liang?": ["王亮"], "Lin Zhaowen?": [], "Jin Hai?": ["金海"], "Feng Dan?": ["冯丹"],
    "Xia Yuanqing?": ["夏元清"], "Wang Guoren?": ["王国仁"], "Zhang Xinpeng?": ["张新鹏"],
    "Guan Xiaohong": ["管晓宏"], "Zhang Yanning?": ["张艳宁"],
}
WATCH = {k: v for k, v in WATCH.items() if "?" not in k}

pool = list(csv.DictReader(open("citing_authors_pool.csv", encoding="utf-8-sig")))
print(f"pool size: {len(pool)}")

def norm(s):
    return s.lower().replace("-", " ").replace(".", " ").strip()

def variants(py):
    parts = py.split()
    return {norm(py), norm(" ".join(reversed(parts)))}

hits = []
for r in pool:
    name = norm(r["name"])
    for py, cn in WATCH.items():
        if name in variants(py):
            hits.append((py, cn, r))
            break

print("\n=== watchlist hits ===")
for py, cn, r in hits:
    print(f"{py} {cn} | {r['n_citing_papers']} citing papers | {r['affiliations'][:70]} | ids={r['author_ids'][:40]}")

json.dump([{**r, "watch_cn": cn} for py, cn, r in hits],
          open("watchlist_hits.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
