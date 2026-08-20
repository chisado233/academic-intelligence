"""S2 API fetcher with 429 backoff (shared free pool, ~100 req/5min)."""
import json, sys, time, urllib.request, urllib.error

BASE = "https://api.semanticscholar.org/graph/v1"

def fetch(path, params=None, max_retry=6):
    url = BASE + path
    if params:
        url += "?" + params
    for i in range(max_retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-research-crawler/0.2 verify"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 20 + i * 15
                print(f"  429, backoff {wait}s ({url[:80]})", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", "replace")[:200]
            print(f"  HTTP {e.code}: {body} ({url[:80]})", file=sys.stderr)
            if e.code == 404:
                return {"error": 404}
            time.sleep(5)
        except Exception as e:
            print(f"  ERR {e}", file=sys.stderr)
            time.sleep(5)
    return {"error": "exhausted"}

if __name__ == "__main__":
    path, out, params = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
    data = fetch(path, params)
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"saved {out}: {json.dumps(data, ensure_ascii=False)[:200]}")
