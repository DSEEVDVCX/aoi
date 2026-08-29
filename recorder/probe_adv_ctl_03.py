import os, sys, sqlite3, config
from datetime import datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

MIN = config.MIN_TOKEN_AGE_DAYS
GATE_ON = "2026-08-22T13:46:30"      # first cycle whose stats carry age_rejected
THEIRS = "2026-08-21"                # the cutoff the claim used


def age_days(created, now_iso):
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


rows = con.execute(
    """
    WITH firsts AS (
      SELECT token_address, network_id, is_control, source, design_version,
             admission_source, first_seen_at,
             ROW_NUMBER() OVER (PARTITION BY token_address, network_id
                                ORDER BY first_seen_at) rn
        FROM watch_windows)
    SELECT f.*, s.token_created_at
      FROM firsts f
      LEFT JOIN token_static s
        ON s.token_address=f.token_address AND s.network_id=f.network_id
     WHERE f.rn=1
    """
).fetchall()


def report(label, cutoff):
    print(f"\n=== first windows with first_seen_at >= {cutoff}  ({label}) ===")
    for ctl in (0, 1):
        xs = [r for r in rows if r["is_control"] == ctl and r["first_seen_at"] >= cutoff]
        ages = [age_days(r["token_created_at"], r["first_seen_at"]) for r in xs]
        known = [a for a in ages if a is not None]
        under = [a for a in known if a < MIN]
        print(f"  is_control={ctl}: new coins={len(xs)}  under2={len(under)}"
              f"  unknown={len(ages)-len(known)}"
              f"  youngest={round(min(known),4) if known else None}")


report("claim's cutoff", THEIRS)
report("TRUE gate onset", GATE_ON)

print("\n=== every first window since the TRUE gate onset, chronological ===")
for r in sorted([r for r in rows if r["first_seen_at"] >= GATE_ON],
                key=lambda r: r["first_seen_at"]):
    a = age_days(r["token_created_at"], r["first_seen_at"])
    print(f"  {r['first_seen_at'][:19]}  ctl={r['is_control']} "
          f"src={str(r['admission_source']):9s} dv={r['design_version']} "
          f"age_d={'None' if a is None else round(a,4)}  {r['source']}")

print("\n=== gate leak test: signal-arm first windows on sub-2-day coins AFTER onset ===")
leaks = [r for r in rows if r["is_control"] == 0 and r["first_seen_at"] >= GATE_ON
         and (lambda a: a is not None and a < MIN)(age_days(r["token_created_at"], r["first_seen_at"]))]
print("  leaks:", len(leaks))

print("\n=== control coins admitted BETWEEN the claim's cutoff and the real onset"
      " (counted by the claim as 'since the gate went live', but pre-gate) ===")
mid = [r for r in rows if r["is_control"] == 1 and THEIRS <= r["first_seen_at"] < GATE_ON]
midu = [r for r in mid if (lambda a: a is not None and a < MIN)(age_days(r["token_created_at"], r["first_seen_at"]))]
print(f"  control coins in that pre-gate stretch: {len(mid)}, of them under 2 days: {len(midu)}")

print("\n=== signal-arm first windows in the same pre-gate stretch (ungated too) ===")
sig = [r for r in rows if r["is_control"] == 0 and THEIRS <= r["first_seen_at"] < GATE_ON]
sigu = [r for r in sig if (lambda a: a is not None and a < MIN)(age_days(r["token_created_at"], r["first_seen_at"]))]
print(f"  signal coins: {len(sig)}, under 2 days: {len(sigu)}")

con.close()
