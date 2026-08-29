import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(s,p=()): return con.execute(s,p).fetchall()
cols = [r["name"] for r in q("PRAGMA table_info(training_rows)")]
tcol = "entry_ts" if "entry_ts" in cols else ("t0" if "t0" in cols else None)
print("time col =", tcol)
print("\n=== model population (fv12) per net: newest signal time ===")
for r in q(f"""SELECT COALESCE(network_id,'(null)') net, COUNT(*) n,
                      MAX(datetime({tcol},'unixepoch')) newest,
                      MIN(datetime({tcol},'unixepoch')) oldest
               FROM training_rows
               WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                 AND is_independent=1 AND feature_version=12
               GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  net={r['net']:<12} n={r['n']:<6} {str(r['oldest'])[:16]} .. {str(r['newest'])[:16]}")
print("\n=== labelled live indep signal supply per net: newest ===")
for r in q("""SELECT COALESCE(network_id,'(null)') net, COUNT(*) n,
                     MAX(datetime(entry_ts,'unixepoch')) newest
              FROM outcomes WHERE kind='signal' AND status='ok' AND is_independent=1
                AND entry_ts>=? GROUP BY 1 ORDER BY 2 DESC""", (config.LIVE_START_TS,)):
    print(f"  net={r['net']:<12} n={r['n']:<6} newest={str(r['newest'])[:16]}")
con.close()
