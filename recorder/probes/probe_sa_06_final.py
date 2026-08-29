import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (f"FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
       f"AND status='ok' AND is_independent=1 AND feature_version = {FV}")

pop = con.execute(f"""SELECT token_address, network_id, COUNT(*) n,
        SUM(CASE WHEN socials_count=0 THEN 1 ELSE 0 END) sc0,
        SUM(CASE WHEN has_twitter=0 THEN 1 ELSE 0 END) tw0,
        SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0
      {POP} GROUP BY token_address, network_id""").fetchall()
tot = sum(r["n"] for r in pop)

fab_sc = fab_tw = meas_sc = 0
for r in pop:
    st = con.execute("SELECT raw_json FROM token_static WHERE token_address=? AND network_id=?",
                     (r["token_address"], r["network_id"])).fetchone()
    if st is None:
        continue
    try:
        obj = dbmod.decode_raw(st["raw_json"])
    except Exception:
        continue
    tok = obj.get("token") if isinstance(obj, dict) else {}
    tok = tok if isinstance(tok, dict) else {}
    sl = tok.get("socialLinks", "__MISSING__")
    if sl == "__MISSING__" or sl is None:
        fab_sc += r["sc0"]; fab_tw += r["tw0"]
    elif isinstance(sl, dict) and not any(
            sl.get(k) for k in ("twitter", "telegram", "website", "discord")):
        meas_sc += r["sc0"]
print("model rows:", tot)
print(f"socials_count=0 FABRICATED (socialLinks key absent from envelope): {fab_sc}"
      f"  ({100.0*fab_sc/tot:.1f}% of population)")
print(f"socials_count=0 MEASURED   (socialLinks present, every link null) : {meas_sc}")
print(f"has_twitter=0 FABRICATED                                          : {fab_tw}")

# --- labeler: candles_48h / suspect_bars = 0 on rows that never got a window --
print("\n=== labeler.compute_labels defaults on non-ok statuses (outcomes) ===")
r = con.execute("""SELECT status, COUNT(*) n,
      SUM(CASE WHEN candles_48h=0 THEN 1 ELSE 0 END) c48_zero,
      SUM(CASE WHEN candles_48h IS NULL THEN 1 ELSE 0 END) c48_null,
      SUM(CASE WHEN suspect_bars=0 THEN 1 ELSE 0 END) sb_zero,
      SUM(CASE WHEN entry_lag_s=0 THEN 1 ELSE 0 END) lag0
    FROM outcomes GROUP BY status ORDER BY n DESC""").fetchall()
for x in r:
    print("  ", dict(x))

# --- entry_lag_s = 0 definitional vs measured -------------------------------
print("\n=== outcomes.entry_lag_s = 0 by kind (watch rows use admission price) ===")
for x in con.execute("""SELECT kind, COUNT(*) n,
      SUM(CASE WHEN entry_lag_s=0 THEN 1 ELSE 0 END) lag0,
      SUM(CASE WHEN entry_lag_s IS NULL THEN 1 ELSE 0 END) lagnull
    FROM outcomes GROUP BY kind""").fetchall():
    print("  ", dict(x))

# --- reverse error: evm_contract.function_count NULL where code_size=0 -------
print("\n=== evm_contract: code_size=0 (analyze_code early return) ===")
try:
    x = con.execute("""SELECT COUNT(*) n,
        SUM(CASE WHEN code_size=0 THEN 1 ELSE 0 END) size0,
        SUM(CASE WHEN code_size=0 AND function_count IS NULL THEN 1 ELSE 0 END) size0_fcnull,
        SUM(CASE WHEN code_size=0 AND has_mint IS NULL THEN 1 ELSE 0 END) size0_mintnull,
        SUM(CASE WHEN function_count IS NULL THEN 1 ELSE 0 END) fcnull
      FROM evm_contract""").fetchone()
    print("  ", dict(x))
except Exception as e:
    print("   FAILED:", e)

# --- flow coverage denominator for context ----------------------------------
print("\n=== flow family coverage in the model population ===")
x = con.execute(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN flow_age_min IS NOT NULL THEN 1 ELSE 0 END) has_flow,
   SUM(CASE WHEN flow_sell_volume_5m=0 THEN 1 ELSE 0 END) sell0,
   SUM(CASE WHEN flow_buy_count_5m=0 THEN 1 ELSE 0 END) buycnt0
   {POP}""").fetchone()
print("  ", dict(x))

# --- size_usd=0 -> log_size_usd NULL, exact ---------------------------------
print("\n=== size_usd = 0 vs log_size_usd (log1p(0)=0.0 is well defined) ===")
x = con.execute(f"""SELECT
   SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) sz0,
   SUM(CASE WHEN size_usd=0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) sz0_null,
   SUM(CASE WHEN size_usd IS NULL AND log_size_usd IS NULL THEN 1 ELSE 0 END) both_null,
   SUM(CASE WHEN log_size_usd IS NULL THEN 1 ELSE 0 END) log_null,
   SUM(CASE WHEN size_usd=0 AND size_to_mcap=0 THEN 1 ELSE 0 END) s2m_zero_ok
   {POP}""").fetchone()
print("  ", dict(x))
con.close()
