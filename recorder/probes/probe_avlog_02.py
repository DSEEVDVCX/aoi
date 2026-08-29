import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = 12
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version={FV}"""

print("=== D2) population control, fixed ===")
for label, extra in [
    ("ALL training_rows",                 "1=1"),
    ("kind='signal' ANY fv",              "kind='signal'"),
    ("kind='signal' is_live=0 (retro)",   "kind='signal' AND is_live=0"),
    ("kind='activity'",                   "kind='activity'"),
    ("model pop (fv=12)",                 POP),
    ("signal live meme ok indep ANY fv",  "kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' AND is_independent=1"),
]:
    r = con.execute(f"""SELECT COUNT(*) n, SUM(log_size_usd IS NULL) ln,
                               SUM(size_usd=0.0) z, SUM(size_usd<0) neg,
                               SUM(size_usd IS NULL) nul
                          FROM training_rows WHERE {extra}""").fetchone()
    n = r["n"] or 0
    ln = r["ln"] or 0
    pct = (100.0*ln/n) if n else 0.0
    print(f"  {label:<34} n={n:>7} log_null={ln:>6} ({pct:5.2f}%) zero={r['z']} neg={r['neg']} sizenull={r['nul']}")

print("\n=== E) the 374 negatives: is log1p even defined there? ===")
r = con.execute(f"""SELECT COUNT(*) n,
        SUM(size_usd > -1)  AS gt_minus1,       -- log1p defined
        SUM(size_usd <= -1) AS le_minus1,       -- log1p mathematically undefined
        SUM(size_usd > -0.01) AS tiny,
        MIN(size_usd) mn, MAX(size_usd) mx
      FROM training_rows WHERE {POP} AND size_usd < 0""").fetchone()
print("  ", dict(r))

print("\n=== F) how tiny are the positives that DID get a log? ===")
r = con.execute(f"""SELECT SUM(size_usd < 1) lt1, SUM(size_usd < 0.01) lt001,
                           SUM(size_usd < 1e-6) lt1e6, COUNT(*) n
                      FROM training_rows WHERE {POP} AND size_usd > 0""").fetchone()
print("  ", dict(r))

print("\n=== G) same _log1p guard on market_cap (sibling call, same helper) ===")
r = con.execute(f"""SELECT COUNT(*) n,
        SUM(market_cap IS NULL) mc_null, SUM(market_cap=0.0) mc_zero,
        SUM(market_cap<0) mc_neg, SUM(log_market_cap IS NULL) lmc_null
      FROM training_rows WHERE {POP}""").fetchone()
print("  ", dict(r))

print("\n=== H) is size_usd itself intact on the 2030 rows (recoverability)? ===")
r = con.execute(f"""SELECT COUNT(*) n, SUM(size_usd IS NULL) sz_null,
                           SUM(size_usd=0.0) exact_zero
                      FROM training_rows
                     WHERE {POP} AND log_size_usd IS NULL AND size_usd = 0.0""").fetchone()
print("  ", dict(r))

print("\n=== I) source table: do the raw signal_events agree size 0 is measured? ===")
r = con.execute("""SELECT COUNT(*) n, SUM(size_usd IS NULL) nul, SUM(size_usd=0.0) z,
                          SUM(size_usd<0) neg, MIN(size_usd) mn
                     FROM signal_events WHERE signal_type='large_sell'""").fetchone()
print("  large_sell in signal_events:", dict(r))
r = con.execute("""SELECT COUNT(*) n, SUM(size_usd IS NULL) nul, SUM(size_usd=0.0) z,
                          SUM(size_usd<0) neg
                     FROM signal_events WHERE signal_type='large_buy'""").fetchone()
print("  large_buy  in signal_events:", dict(r))
con.close()
