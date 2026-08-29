"""Decisive test: do large_buy/large_sell raw payloads actually carry the six
multi_user body fields? If yes, the extractor is dropping them (real bug).
If no, NULL is the correct FR-007 encoding of an absent upstream field."""
import os, sqlite3, collections, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

TARGETS = ["numTrades", "uniqueTraders", "minutes", "priceChangePercent",
           "totalVolume", "areTopTraders"]

def body_of(obj):
    if not isinstance(obj, dict):
        return None
    b = obj.get("body")
    return b if isinstance(b, dict) else obj

for st in ("large_buy", "large_sell", "multi_user_buy", "multi_user_sell"):
    rows = con.execute(
        "SELECT id, raw_json FROM signal_events WHERE signal_type=? "
        "AND raw_json IS NOT NULL ORDER BY recorded_at DESC LIMIT 400",
        (st,)).fetchall()
    keyfreq = collections.Counter()
    hits = collections.Counter()
    n_dec = 0
    sample_keys = None
    for r in rows:
        try:
            obj = dbmod.decode_raw(r["raw_json"])
        except Exception as e:                       # noqa: BLE001
            continue
        b = body_of(obj)
        if b is None:
            continue
        n_dec += 1
        if sample_keys is None:
            sample_keys = sorted(b.keys())
        low = {k.lower(): k for k in b.keys()}
        for k in b:
            keyfreq[k] += 1
        for t in TARGETS:
            if t.lower() in low:
                hits[t] += 1
    print("=" * 78)
    print(f"{st}: fetched {len(rows)} rows, decoded bodies {n_dec}")
    print("  target-key presence (case-insensitive):",
          {t: hits[t] for t in TARGETS})
    print("  first body key set:", sample_keys)
    print()

con.close()
