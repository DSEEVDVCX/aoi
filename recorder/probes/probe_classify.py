"""Classify columns from probe_health.json into buckets."""
import json

d = json.load(open("probe_health.json", encoding="utf-8"))
M = d["model"]["cols"]; MT = d["model"]["total"]
A = d["all"]["cols"];   AT = d["all"]["total"]

def pct(a, b):
    return 0.0 if not b else 100.0 * a / b

buckets = {"a_100null": [], "b_gt95null": [], "c_gt90zero": [], "d_constant": [], "e_healthy": []}

for name, m in M.items():
    nn = MT - m["nulls"]                     # non-null in model pop
    a = A[name]
    a_nn = AT - a["nulls"]
    nullpct = pct(m["nulls"], MT)
    zeropct = pct(m["zeros"], MT)
    line = {
        "col": name, "type": m["type"],
        "m_null": m["nulls"], "m_nullpct": round(nullpct, 2),
        "m_zero": m["zeros"], "m_zeropct": round(zeropct, 2),
        "m_distinct": m["distinct"], "m_min": m["min"], "m_max": m["max"],
        "all_null": a["nulls"], "all_nullpct": round(pct(a["nulls"], AT), 2),
        "all_zero": a["zeros"], "all_distinct": a["distinct"],
        "all_min": a["min"], "all_max": a["max"], "all_nonnull": a_nn,
    }
    if m["nulls"] == MT:
        buckets["a_100null"].append(line)
    elif nullpct > 95:
        buckets["b_gt95null"].append(line)
    elif zeropct > 90:
        buckets["c_gt90zero"].append(line)
    elif m["distinct"] <= 1:
        buckets["d_constant"].append(line)
    else:
        buckets["e_healthy"].append(line)

# constant check is also relevant even when in another bucket
also_constant = [n for n, m in M.items() if m["distinct"] <= 1]

for k in ["a_100null", "b_gt95null", "c_gt90zero", "d_constant"]:
    print(f"\n===== {k}  ({len(buckets[k])}) =====")
    for l in sorted(buckets[k], key=lambda x: -x["m_nullpct"]):
        print(f"{l['col']:38s} type={l['type']:8s} "
              f"mNULL={l['m_null']}/{MT}({l['m_nullpct']}%) mZERO={l['m_zero']}({l['m_zeropct']}%) "
              f"mDIST={l['m_distinct']} rng=[{l['m_min']}..{l['m_max']}] | "
              f"ALL nonnull={l['all_nonnull']}/{AT} dist={l['all_distinct']} rng=[{l['all_min']}..{l['all_max']}]")

print(f"\n===== healthy ({len(buckets['e_healthy'])}) =====")
for l in sorted(buckets["e_healthy"], key=lambda x: -x["m_nullpct"]):
    print(f"{l['col']:38s} mNULL={l['m_nullpct']}% mZERO={l['m_zeropct']}% mDIST={l['m_distinct']} rng=[{l['m_min']}..{l['m_max']}]")

print("\nALL columns with <=1 distinct in model pop:", also_constant)
json.dump(buckets, open("probe_buckets.json", "w", encoding="utf-8"), indent=0, default=str)
