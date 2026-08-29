"""Why the time filter kills the EVM contract family; plus the always-constant columns."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
def d(e):
    return None if e is None else dt.datetime.fromtimestamp(int(e), dt.timezone.utc).isoformat()

r = con.execute("""SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) mn,
                          MAX(CAST(strftime('%s',recorded_at) AS INTEGER)) mx,
                          COUNT(*) n, COUNT(DISTINCT token_address) tok
                     FROM evm_contract""").fetchone()
print("evm_contract  first snapshot:", d(r["mn"]), " last:", d(r["mx"]), " rows:", r["n"], " tokens:", r["tok"])

r = con.execute("SELECT MIN(entry_ts) mn, MAX(entry_ts) mx, COUNT(*) n FROM training_rows WHERE network_id='8453'").fetchone()
print("training_rows Base(8453) entry_ts:", d(r["mn"]), "->", d(r["mx"]), " rows:", r["n"])
r = con.execute("SELECT MIN(entry_ts) mn, MAX(entry_ts) mx FROM training_rows").fetchone()
print("training_rows ALL entry_ts     :", d(r["mn"]), "->", d(r["mx"]))
r = con.execute("SELECT COUNT(*) n FROM training_rows WHERE network_id='8453' AND entry_ts >= (SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) FROM evm_contract)").fetchone()
print("Base training rows with entry_ts >= first contract snapshot:", r["n"])

print("\n-- per matched token: entry_ts vs that token's earliest snapshot --")
q = """SELECT COUNT(*) n,
              MIN((SELECT MIN(CAST(strftime('%s',c.recorded_at) AS INTEGER)) FROM evm_contract c
                    WHERE c.token_address=tr.token_address) - tr.entry_ts)/60.0 min_gap_min,
              MAX((SELECT MIN(CAST(strftime('%s',c.recorded_at) AS INTEGER)) FROM evm_contract c
                    WHERE c.token_address=tr.token_address) - tr.entry_ts)/60.0 max_gap_min
         FROM training_rows tr
        WHERE EXISTS (SELECT 1 FROM evm_contract c WHERE c.token_address=tr.token_address)"""
print("   ", dict(con.execute(q).fetchone()))

print("\n-- signal_events on Base: is the source still producing? --")
for r in con.execute("""SELECT substr(created_at,1,10) day, COUNT(*) n FROM signal_events
                         WHERE network_id='8453' GROUP BY 1 ORDER BY 1 DESC LIMIT 12"""):
    print("   ", dict(r))

print("\n-- always-constant columns: verify over the WHOLE table --")
for c in ["social_replies", "listed_on_exchange", "are_top_traders", "is_scam",
          "rank_le_50", "minutes", "top_traders_listed", "top_trader_match_count",
          "onchain_has_freeze_authority", "suspect_bars"]:
    q = f"""SELECT COUNT(*) n,
                   SUM(CASE WHEN "{c}" IS NULL THEN 1 ELSE 0 END) nulls,
                   SUM(CASE WHEN "{c}"=0 THEN 1 ELSE 0 END) zeros,
                   COUNT(DISTINCT "{c}") dist, MIN("{c}") mn, MAX("{c}") mx
              FROM training_rows"""
    r = con.execute(q).fetchone()
    print(f"   {c:32s} n={r['n']} nulls={r['nulls']} zeros={r['zeros']} dist={r['dist']} rng=[{r['mn']}..{r['mx']}]")

print("\n-- source columns behind social_replies / listed_on_exchange --")
for tbl, col in [("social_snapshots", "replies"), ("token_social", "replies")]:
    try:
        r = con.execute(f"SELECT COUNT(*) n, SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) nulls, COUNT(DISTINCT {col}) d, MAX({col}) mx FROM {tbl}").fetchone()
        print(f"   {tbl}.{col}: {dict(r)}")
    except Exception as e:
        print(f"   {tbl}.{col}: FAILED {e}")
print("   tables:", [x["name"] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%social%' OR name LIKE '%thesis%')")])
