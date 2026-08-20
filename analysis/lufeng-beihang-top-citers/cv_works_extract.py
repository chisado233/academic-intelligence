"""Extract numbered publication entries from CV segments, then batch-query Crossref
for DOI + is-referenced-by-count (lower-bound citation metric)."""
import json, re, time, urllib.request, urllib.parse, urllib.error

# 1) parse CV segments -> numbered entries (lines starting "N. ")
segs = [json.loads(l) for l in open("cv_segments.jsonl", encoding="utf-8")]
entries = {}
for s in segs:
    for m in re.finditer(r"(?m)^\s*(\d{1,2})\.\s+(.+)$", s.get("text", "")):
        entries[int(m.group(1))] = m.group(2).replace("\n", " ").strip()

print(f"parsed {len(entries)} numbered entries")

def split_title(raw):
    """Authors. Title. Venue... -> (authors, title)."""
    # Heuristic: split on '. ' — authors = leading segments that are names
    # (contain ',' or 'and', no ':', short); title = first long segment.
    parts = [p.strip() for p in raw.split(". ") if p.strip()]
    for i, p in enumerate(parts[:-1]):  # title cannot be the last (venue) part
        looks_title = len(p) > 15 and " " in p and not p.endswith(("*",))
        if looks_title and (i >= 1 or p[0].isupper()):
            # avoid grabbing venue-like parts
            if re.search(r"(Conference|Journal|Transactions|Symposium|Workshop|BMVC|ECCV|CVPR|ICCV|AAAI|ICPR|ICIP|MMM|letters?|Science)", p, re.I) and i > 1:
                continue
            return ". ".join(parts[:i]), p
    return "", parts[0] if parts else raw

works = {}
for n in sorted(entries):
    authors, title = split_title(entries[n])
    works[n] = {"raw": entries[n], "authors": authors, "title": title, "cv_no": n}

json.dump(works, open("cv_works_parsed.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
for n in sorted(works)[:5]:
    print(n, "|", works[n]["title"][:70])
print("...")

# 2) Crossref batch: query.bibliographic by title
UA = {"User-Agent": "paper-research-crawler/0.2 (academic-intelligence skill; mailto:lufeng-verify@example.org)"}
sel = "DOI,title,is-referenced-by-count,author,issued,container-title"
for n in sorted(works):
    t = works[n]["title"]
    if not t:
        works[n]["crossref"] = {"status": "no_title"}
        continue
    q = urllib.parse.urlencode({"query.bibliographic": t, "rows": 3, "select": sel})
    url = f"https://api.crossref.org/works?{q}"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        items = d.get("message", {}).get("items", [])
        best = None
        want = re.sub(r"[^a-z0-9]", "", t.lower())[:60]
        for it in items:
            got = re.sub(r"[^a-z0-9]", "", (it.get("title") or [""])[0].lower())[:60]
            if got == want:
                best = it
                break
        if best is None and items:
            # keep top hit but flag fuzzy
            best = dict(items[0]); best["_fuzzy"] = True
        if best:
            works[n]["crossref"] = {
                "doi": best.get("DOI"),
                "title_cr": (best.get("title") or [""])[0],
                "is_ref_by": best.get("is-referenced-by-count", 0),
                "container": (best.get("container-title") or [""])[0],
                "fuzzy": best.get("_fuzzy", False),
            }
        else:
            works[n]["crossref"] = {"status": "no_match"}
    except urllib.error.HTTPError as e:
        works[n]["crossref"] = {"status": f"http_{e.code}"}
    except Exception as e:
        works[n]["crossref"] = {"status": f"err_{e}"}
    time.sleep(0.4)

json.dump(works, open("cv_works_crossref.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

ranked = sorted(
    ((v["crossref"].get("is_ref_by", 0), v["cv_no"], v["title"][:60], v["crossref"].get("doi"), v["crossref"].get("fuzzy"))
     for v in works.values() if "is_ref_by" in v.get("crossref", {})),
    reverse=True)
print("\nTop 15 by Crossref is-referenced-by-count:")
for c, n, t, doi, fz in ranked[:15]:
    print(f"  {c:5d} | #{n:2d} | {t} | {doi}{' (fuzzy)' if fz else ''}")
