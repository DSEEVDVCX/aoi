import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

tabs = [r["name"] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("TABLES:", tabs)

for t in ("token_holders", "token_flow", "chain_concentration", "chain_authority",
          "evm_contract", "market_ticks", "token_static", "thesis_snapshots"):
    if t not in tabs:
        print(f"\n-- {t}: ABSENT")
        continue
    ci = [r["name"] for r in con.execute(f"PRAGMA table_info({t})")]
    tcol = "recorded_at" if "recorded_at" in ci else ("ts" if "ts" in ci else None)
    ncol = "network_id" if "network_id" in ci else None
    n = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    print(f"\n-- {t}: {n} rows; time col={tcol} net col={ncol}")
    if tcol:
        r = con.execute(f"SELECT MIN({tcol}) a, MAX({tcol}) b FROM {t}").fetchone()
        print(f"   range: {r['a']} .. {r['b']}")
    if ncol:
        print("   by network:")
        q = f"SELECT {ncol} net, COUNT(*) c" + (f", MIN({tcol}) a, MAX({tcol}) b" if tcol else "") + \
            f" FROM {t} GROUP BY 1 ORDER BY c DESC LIMIT 12"
        for r in con.execute(q):
            print("    ", dict(r))
con.close()
