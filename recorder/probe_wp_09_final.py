import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== A) rebuild-gate meta keys read by build_training_rows.pending_outcomes ===")
for k in ("evm_ledger_rebuild_required", "evm_training_rebuild_started",
          "current_feature_version", "evm_ledger_generation"):
    r = q("SELECT value FROM meta WHERE key=?", (k,))
    print(f"   {k:32s} = {r[0]['value'] if r else '<absent>'}")

print()
print("=== B) are the 659 permanently stuck? (they already exist at feature_version=12) ===")
r = q(f"""SELECT COUNT(*) c FROM outcomes o
           WHERE o.status IN ('ok','no_bars')
             AND EXISTS (SELECT 1 FROM training_rows r
                          WHERE r.kind=o.kind AND r.key=o.key AND r.feature_version=12)""")[0]
print(f"   outcomes already having a feature_version=12 training row (never revisited): {r['c']}")

print()
print("=== C) platform_holders_listed 'or None' -> does it cost platform_underwater_ratio? ===")
r = q("""SELECT COUNT(*) c FROM token_holders
          WHERE source='hodlers_top' AND platform_holders_listed IS NULL
            AND platform_underwater IS NOT NULL""")[0]
print(f"   hodlers_top rows with listed NULL but platform_underwater NOT NULL: {r['c']}")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN platform_underwater_ratio IS NULL THEN 1 ELSE 0 END) rN,
                 SUM(CASE WHEN platform_holders IS NOT NULL
                           AND platform_underwater_ratio IS NULL THEN 1 ELSE 0 END) both
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']} platform_underwater_ratio NULL={r['rN']}"
      f"  (of which platform_holders IS NOT NULL: {r['both']})")

print()
print("=== D) chain_concentration: does the live (non-replay) writer ever produce top_accounts=0? ===")
r = q("""SELECT MIN(top_accounts) mn, MAX(top_accounts) mx, COUNT(*) c
           FROM chain_concentration WHERE COALESCE(is_replay,0)=0""")[0]
print(f"   live rows={r['c']} top_accounts min={r['mn']} max={r['mx']}")
r = q("""SELECT MIN(supply) mn, COUNT(*) c FROM chain_concentration""")[0]
print(f"   all rows={r['c']} min(supply)={r['mn']}")

print()
print("=== E) token_created_at magnitude — recorder.token_age_days() has no ms branch ===")
r = q("""SELECT MIN(CAST(token_created_at AS INTEGER)) mn,
                MAX(CAST(token_created_at AS INTEGER)) mx,
                SUM(CASE WHEN CAST(token_created_at AS INTEGER) > 100000000000 THEN 1 ELSE 0 END) ms
           FROM token_static WHERE token_created_at IS NOT NULL AND token_created_at<>''""")[0]
print(f"   token_created_at min={r['mn']} max={r['mx']}  values > 1e11 (milliseconds) = {r['ms']}")

print()
print("=== F) token_age_h: negative-age rows nulled, and coverage ===")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN token_age_h IS NULL THEN 1 ELSE 0 END) n,
                 MIN(token_age_h) mn, MAX(token_age_h) mx
            FROM training_rows WHERE {POP}""")[0]
print(f"   model rows={r['tot']} token_age_h NULL={r['n']} min={r['mn']} max={r['mx']}")

con.close()
