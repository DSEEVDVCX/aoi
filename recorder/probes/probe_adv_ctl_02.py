import os, sqlite3, config
from datetime import datetime

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

MIN = config.MIN_TOKEN_AGE_DAYS


def age_days(created, now_iso):
    """Exact copy of recorder.token_age_days semantics."""
    if created in (None, ""):
        return None
    try:
        c = int(float(created))
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    a = (int(datetime.fromisoformat(now_iso).timestamp()) - c) / 86400.0
    return a if a >= 0 else None


# First window per coin (my own query: window functions, no HAVING trick,
# LEFT JOIN so coins without static are not silently dropped).
rows = con.execute(
    """
    WITH firsts AS (
      SELECT token_address, network_id, is_control, source, design_version,
             admission_source, first_seen_at,
             ROW_NUMBER() OVER (PARTITION BY token_address, network_id
                                ORDER BY first_seen_at) AS rn
        FROM watch_windows
    )
    SELECT f.token_address, f.network_id, f.is_control, f.source,
           f.design_version, f.admission_source, f.first_seen_at,
           s.token_created_at, s.symbol
      FROM firsts f
      LEFT JOIN token_static s
        ON s.token_address = f.token_address AND s.network_id = f.network_id
     WHERE f.rn = 1
    """
).fetchall()
print("first windows (= distinct coins) total:", len(rows))

buckets = {}
raw_negative = {0: 0, 1: 0}
for r in rows:
    a = age_days(r["token_created_at"], r["first_seen_at"])
    ctl = r["is_control"]
    if r["token_created_at"] not in (None, ""):
        try:
            raw = (int(datetime.fromisoformat(r["first_seen_at"]).timestamp())
                   - int(float(r["token_created_at"]))) / 86400.0
            if raw < 0:
                raw_negative[ctl] += 1
        except Exception:
            pass
    if a is None:
        b = "unknown/future"
    elif a < 1:
        b = "a <1 day"
    elif a < 2:
        b = "b 1-2 days"
    elif a < 7:
        b = "c 2-7 days"
    else:
        b = "d >7 days"
    buckets.setdefault(ctl, {}).setdefault(b, 0)
    buckets[ctl][b] += 1

print("\n=== 1) ALL-TIME first-window age of each coin, by arm ===")
for ctl in sorted(buckets):
    tot = sum(buckets[ctl].values())
    under = buckets[ctl].get("a <1 day", 0) + buckets[ctl].get("b 1-2 days", 0)
    print(f"  is_control={ctl}  coins={tot}  under 2 days={under} ({under/tot*100:.1f}%)")
    for b in sorted(buckets[ctl]):
        print(f"       {b:14s} {buckets[ctl][b]:5d}")
    print(f"       raw-negative (future created_at, their SQL calls these <1 day): {raw_negative[ctl]}")

print("\n=== 2) same split, restricted to what phase1 actually consumes"
      " (design_version>=3, admission_source in trending/verified) ===")
sub = [r for r in rows if r["design_version"] >= 3
       and r["admission_source"] in ("trending", "verified")]
agg = {}
for r in sub:
    a = age_days(r["token_created_at"], r["first_seen_at"])
    key = r["is_control"]
    d = agg.setdefault(key, {"n": 0, "under2": 0, "unknown": 0, "min": None})
    d["n"] += 1
    if a is None:
        d["unknown"] += 1
    else:
        if a < MIN:
            d["under2"] += 1
        if d["min"] is None or a < d["min"]:
            d["min"] = a
for k in sorted(agg):
    d = agg[k]
    print(f"  is_control={k} coins={d['n']} under2={d['under2']} "
          f"({d['under2']/d['n']*100:.1f}%) unknown={d['unknown']} "
          f"youngest={None if d['min'] is None else round(d['min'],3)}")

print("\n=== 3) chronology: every FIRST window on a coin younger than 2 days,"
      " last 60, with arm — shows exactly when each arm stopped ===")
young = []
for r in rows:
    a = age_days(r["token_created_at"], r["first_seen_at"])
    if a is not None and a < MIN:
        young.append((r["first_seen_at"], r["is_control"], round(a, 4),
                      r["source"], r["admission_source"], r["symbol"]))
young.sort()
for y in young[-60:]:
    print("  ", y)

print("\n=== 4) LAST first-window on a sub-2-day coin, per arm ===")
for ctl in (0, 1):
    xs = [y for y in young if y[1] == ctl]
    print(f"  is_control={ctl}: n={len(xs)} last={xs[-1] if xs else None}")

con.close()
