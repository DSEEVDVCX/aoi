import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("=== EVM repair pipeline: status + freshness ===")
for t in ("evm_replay_state", "evm_backfill_state"):
    for r in q(f"""SELECT network_id, status, COUNT(*) n,
                          MAX(last_try_at) newest, MIN(last_try_at) oldest
                   FROM {t} GROUP BY 1,2 ORDER BY 1,2"""):
        print(f"   {t:20s} net={r['network_id']:6s} {r['status']:9s} n={r['n']:5d} "
              f"last_try {r['oldest']} .. {r['newest']}")

print("\n=== meta: anything that timestamps the builder / repair / recorder ===")
for r in q("""SELECT key, value FROM meta
              WHERE key LIKE '%last_run%' OR key LIKE '%_at' OR key LIKE '%evm%'
                 OR key LIKE '%rebuild%' OR key LIKE '%replay%'
              ORDER BY key"""):
    v = r["value"]
    print(f"   {r['key']:44s} = {(v[:90] if isinstance(v,str) else v)}")

print("\n=== do the held EVM outcomes even have signal_events / would they build? ===")
r = q("""SELECT COUNT(*) n FROM outcomes o
         WHERE o.status IN ('ok','no_bars')
           AND COALESCE(o.network_id,'') IN ('4663','8453','143')
           AND NOT EXISTS (SELECT 1 FROM training_rows r
                WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)""")[0]
print("   EVM outcomes lacking an fv=12 row:", r["n"])
r = q("""SELECT COUNT(*) n FROM outcomes o
         WHERE o.status IN ('ok','no_bars')
           AND COALESCE(o.network_id,'') NOT IN ('4663','8453','143')
           AND NOT EXISTS (SELECT 1 FROM training_rows r
                WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)""")[0]
print("   non-EVM outcomes lacking an fv=12 row (builder backlog):", r["n"])

print("\n=== fv=8 rows: were the concentration cols already NULLed by the repair? ===")
cols = ("onchain_top1_pct","onchain_top10_pct","onchain_top_accounts",
        "onchain_holder_count","onchain_age_min")
sel = ", ".join(f"COALESCE(SUM({c} IS NOT NULL),0) {c}" for c in cols)
for fv in (8, 12):
    r = q(f"""SELECT COUNT(*) n, {sel} FROM training_rows
              WHERE feature_version=? AND COALESCE(network_id,'') IN ('4663','8453')""",
          (fv,))[0]
    print(f"   fv={fv} n={r['n']}: " +
          "  ".join(f"{c}={r[c]}" for c in cols))
con.close()
