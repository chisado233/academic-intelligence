"""Pull full citing-paper list for MBLLEN from S2 citations API.
limit=100 (safe for unauthenticated), plain author fields, incremental save."""
import json, time, urllib.request, urllib.error

UA = {"User-Agent": "paper-research-crawler/0.2 (academic-intelligence skill)"}
BASE = "https://api.semanticscholar.org/graph/v1"
PID = "70cb4bdd05cccc1f99cf690582e66b7637b81da7"
FIELDS = "title,year,venue,externalIds,authors"

def fetch(url, tries=10):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                wait = 30 + i * 20
                print(f"  {e.code}, backoff {wait}s", flush=True)
                time.sleep(wait)
            elif e.code == 400:
                print("  400:", e.read().decode("utf-8", "replace")[:150], flush=True)
                return {"error": 400}
            elif e.code == 404:
                return {"error": 404}
            else:
                print(f"  HTTP {e.code}", flush=True)
                time.sleep(10)
        except Exception as e:
            print(f"  ERR {e}", flush=True)
            time.sleep(15)
    return None

all_citing = []
offset = 0
fail_streak = 0
while True:
    url = f"{BASE}/paper/{PID}/citations?fields={FIELDS}&limit=100&offset={offset}"
    d = fetch(url)
    if d is None or "error" in d:
        fail_streak += 1
        print(f"offset {offset} failed ({d})", flush=True)
        if fail_streak >= 3:
            print("3 consecutive failures, stopping", flush=True)
            break
        time.sleep(60)
        continue
    fail_streak = 0
    data = d.get("data", [])
    for row in data:
        all_citing.append(row.get("citingPaper", {}))
    nxt = d.get("next")
    print(f"offset {offset}: got {len(data)}, total {len(all_citing)}, next={nxt}", flush=True)
    json.dump(all_citing, open("s2_citing_full.json", "w", encoding="utf-8"), ensure_ascii=False)  # incremental save
    if nxt is None or not data:
        break
    offset = nxt
    time.sleep(35)

print(f"TOTAL citing papers saved: {len(all_citing)}", flush=True)
