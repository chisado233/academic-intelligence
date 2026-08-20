"""Batch-fetch S2 author profiles (hIndex, paperCount, citationCount, affiliations)
for unique author IDs in the citing pool -> author_profiles.json."""
import json, time, urllib.request, urllib.error

UA = {"User-Agent": "paper-research-crawler/0.2 (academic-intelligence skill)"}
BASE = "https://api.semanticscholar.org/graph/v1"

citing = json.load(open("s2_citing_full.json", encoding="utf-8"))
ids = set()
for p in citing:
    for a in p.get("authors", []):
        if a.get("authorId"):
            ids.add(a["authorId"])
ids = sorted(ids)
print(f"unique author ids: {len(ids)}")

FIELDS = "name,hIndex,paperCount,citationCount,affiliations"

def post_batch(chunk):
    url = BASE + "/author/batch?fields=" + FIELDS
    body = json.dumps({"ids": chunk}).encode()
    for i in range(10):
        try:
            req = urllib.request.Request(url, data=body, headers={**UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = 40 + i * 25
                print(f"  {e.code}, backoff {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code}: {e.read().decode('utf-8','replace')[:100]}", flush=True)
                time.sleep(15)
        except Exception as e:
            print(f"  ERR {e}", flush=True)
            time.sleep(20)
    return None

profiles = {}
CH = 500
for i in range(0, len(ids), CH):
    chunk = ids[i:i + CH]
    d = post_batch(chunk)
    if d and isinstance(d, list):
        for a in d:
            if a and a.get("authorId"):
                profiles[a["authorId"]] = {
                    "name": a.get("name"),
                    "h": a.get("hIndex"),
                    "papers": a.get("paperCount"),
                    "cites": a.get("citationCount"),
                    "affs": a.get("affiliations") or [],
                }
        json.dump(profiles, open("author_profiles.json", "w", encoding="utf-8"), ensure_ascii=False)
        print(f"batch {i//CH + 1}: total {len(profiles)} profiles", flush=True)
    else:
        print(f"batch {i//CH + 1} FAILED", flush=True)
    time.sleep(45)

json.dump(profiles, open("author_profiles.json", "w", encoding="utf-8"), ensure_ascii=False)
print(f"DONE: {len(profiles)} profiles")

# quick view: top h-index
top = sorted(profiles.values(), key=lambda x: -(x.get("h") or 0))
for a in top[:30]:
    print(f"  h={a['h']:3d} | {a['name']} | {'|'.join(a['affs'][:1])} | cites={a['cites']}")
