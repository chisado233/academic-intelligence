"""Flatten unique citing authors from s2_citing_full.json -> authors pool CSV."""
import csv, json, sys
from collections import defaultdict

citing = json.load(open("s2_citing_full.json", encoding="utf-8"))
print(f"citing papers: {len(citing)}")

pool = defaultdict(lambda: {"ids": set(), "affs": set(), "n": 0, "papers": []})
for p in citing:
    for a in p.get("authors", []):
        name = a.get("name", "").strip()
        if not name:
            continue
        rec = pool[name]
        rec["n"] += 1
        if a.get("authorId"):
            rec["ids"].add(a["authorId"])
        for aff in a.get("affiliations") or []:
            rec["affs"].add(aff)
        rec["papers"].append(p.get("title", "")[:60])

rows = []
for name, r in sorted(pool.items(), key=lambda kv: -kv[1]["n"]):
    rows.append({
        "name": name,
        "author_ids": ";".join(sorted(r["ids"])),
        "n_citing_papers": r["n"],
        "affiliations": " | ".join(sorted(r["affs"]))[:150],
        "sample_papers": " ;; ".join(r["papers"][:3])[:200],
    })
with open("citing_authors_pool.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
print(f"unique author names: {len(rows)}")
print("top by #citing papers:")
for r in rows[:15]:
    print(" ", r["name"], "|", r["n_citing_papers"], "|", r["affiliations"][:60])
