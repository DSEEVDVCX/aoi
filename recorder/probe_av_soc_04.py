import os, sqlite3, json, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
SOC = ("twitter", "telegram", "website", "discord")

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version = {FV}"""

toks = con.execute(f"""SELECT token_address a, network_id n, COUNT(*) rows_n,
       MIN(entry_ts) t_min, MAX(entry_ts) t_max {POP} AND socials_count=0 GROUP BY a,n""").fetchall()
print("sc=0 tokens:", len(toks), " rows:", sum(t["rows_n"] for t in toks))

tot_ticks_scanned = 0
res = []
for t in toks:
    a, n = t["a"], t["n"]
    rows = con.execute(
        """SELECT recorded_at, source, raw_json FROM market_ticks
            WHERE token_address=? AND network_id=? ORDER BY recorded_at""",
        (a, n)).fetchall()
    ev_pre_min = None   # earliest evidence timestamp with a real social value
    ev_any = None
    n_scanned = 0
    for r in rows:
        try:
            item = dbmod.decode_raw(r["raw_json"])
        except Exception:
            continue
        n_scanned += 1
        tok = item.get("token") if isinstance(item.get("token"), dict) else {}
        sl = tok.get("socialLinks")
        if isinstance(sl, dict):
            vals = {k: sl.get(k) for k in SOC if sl.get(k)}
            if vals:
                ts = int(con.execute("SELECT CAST(strftime('%s',?) AS INTEGER)",
                                     (r["recorded_at"],)).fetchone()[0])
                if ev_any is None:
                    ev_any = (ts, r["recorded_at"], r["source"], vals)
                if ts <= t["t_min"] and ev_pre_min is None:
                    ev_pre_min = (ts, r["recorded_at"], r["source"], vals)
    tot_ticks_scanned += n_scanned
    res.append({"a": a, "n": n, "rows_n": t["rows_n"], "t_min": t["t_min"], "t_max": t["t_max"],
                "ticks": n_scanned, "ev_pre": ev_pre_min, "ev_any": ev_any})

pre = [r for r in res if r["ev_pre"]]
anyev = [r for r in res if r["ev_any"]]
print("ticks decoded:", tot_ticks_scanned)
print("tokens with REAL social value in an envelope at/BEFORE their first sc=0 row t0:",
      len(pre), "/", len(res), " rows affected:", sum(r["rows_n"] for r in pre))
print("tokens with REAL social value in ANY envelope (any time):",
      len(anyev), "/", len(res), " rows:", sum(r["rows_n"] for r in anyev))
print("tokens with no real social value ever:", len(res) - len(anyev),
      " rows:", sum(r["rows_n"] for r in res if not r["ev_any"]))

print("\nfirst 8 pre-t0 examples:")
for r in pre[:8]:
    ts, ra, src, vals = r["ev_pre"]
    print(f"  {r['a'][:14]}.. net={r['n']} rows={r['rows_n']} t0_min={r['t_min']} "
          f"evidence@{ra}({src}) keys={list(vals)}")
con.close()
