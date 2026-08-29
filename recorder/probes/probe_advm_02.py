import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

# --- 1) my own NOT EXISTS test for "no tick ever for that token"
q = con.execute(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP}
   AND t.tick_age_min IS NULL
   AND NOT EXISTS (SELECT 1 FROM market_ticks m
        WHERE m.token_address = t.token_address AND m.network_id = t.network_id)""").fetchone()["c"]
print("market-NULL rows with NO tick for that token EVER (my NOT EXISTS):", q)

# same but ignoring network (looser)
q2 = con.execute(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP}
   AND t.tick_age_min IS NULL
   AND NOT EXISTS (SELECT 1 FROM market_ticks m WHERE m.token_address = t.token_address)""").fetchone()["c"]
print("  ... ignoring network_id:", q2)

# case-insensitive
q3 = con.execute(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP}
   AND t.tick_age_min IS NULL
   AND NOT EXISTS (SELECT 1 FROM market_ticks m
        WHERE lower(m.token_address) = lower(t.token_address))""").fetchone()["c"]
print("  ... case-insensitive, ignoring network:", q3)

# --- 2) THE STRUCTURAL TEST: market_ticks is written only for coins already
# in the watchlist, so the first tick cannot precede the signal that admitted
# the coin. Prediction: market-NULL rows are the FIRST signal for their token.
print("\n=== prior_signals_token: market-NULL vs market-present ===")
for lab, cond in (("market NULL", "t.tick_age_min IS NULL"),
                  ("market present", "t.tick_age_min IS NOT NULL")):
    r = con.execute(f"""SELECT COUNT(*) n,
       SUM(CASE WHEN prior_signals_token = 0 THEN 1 ELSE 0 END) first_sig,
       SUM(CASE WHEN prior_signals_token > 0 THEN 1 ELSE 0 END) repeat_sig,
       SUM(CASE WHEN prior_signals_token IS NULL THEN 1 ELSE 0 END) nul
       FROM training_rows t WHERE {POP} AND {cond}""").fetchone()
    n = r["n"]
    print(f"{lab:16s} n={n:6d}  prior=0 (first signal): {r['first_sig']:6d} ({100*r['first_sig']/n:.1f}%)"
          f"  prior>0: {r['repeat_sig']:6d} ({100*r['repeat_sig']/n:.1f}%)  NULL:{r['nul']}")

print("\n=== same for token_static family (has_twitter IS NULL) ===")
for lab, cond in (("static NULL", "t.has_twitter IS NULL"),
                  ("static present", "t.has_twitter IS NOT NULL")):
    r = con.execute(f"""SELECT COUNT(*) n,
       SUM(CASE WHEN prior_signals_token = 0 THEN 1 ELSE 0 END) first_sig
       FROM training_rows t WHERE {POP} AND {cond}""").fetchone()
    n = r["n"]
    print(f"{lab:16s} n={n:6d}  prior=0 (first signal): {r['first_sig']:6d} ({100*r['first_sig']/n:.1f}%)")

# --- 3) does a watchlist row exist for the market-NULL tokens?
r = con.execute(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN EXISTS (SELECT 1 FROM watchlist w
        WHERE w.token_address=t.token_address AND w.network_id=t.network_id)
       THEN 1 ELSE 0 END) has_watch
   FROM training_rows t WHERE {POP} AND t.tick_age_min IS NULL""").fetchone()
print(f"\nmarket-NULL rows: {r['n']} total, {r['has_watch']} have a watchlist row")

# and for the never-a-tick hard subset
r = con.execute(f"""SELECT COUNT(*) n,
   SUM(CASE WHEN EXISTS (SELECT 1 FROM watchlist w
        WHERE w.token_address=t.token_address AND w.network_id=t.network_id)
       THEN 1 ELSE 0 END) has_watch,
   SUM(CASE WHEN prior_signals_token=0 THEN 1 ELSE 0 END) firstsig
   FROM training_rows t WHERE {POP} AND t.tick_age_min IS NULL
   AND NOT EXISTS (SELECT 1 FROM market_ticks m
        WHERE m.token_address=t.token_address AND m.network_id=t.network_id)""").fetchone()
print(f"never-a-tick subset: {r['n']} total, {r['has_watch']} have a watchlist row, {r['firstsig']} are first signal")

con.close()
