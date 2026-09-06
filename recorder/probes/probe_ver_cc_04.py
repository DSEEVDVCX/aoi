import os, sqlite3, json, config
from collections import Counter

print("EVM_NETWORKS       =", getattr(config, "EVM_NETWORKS", None))
print("EVM_REPLAY_NETWORKS=", getattr(config, "EVM_REPLAY_NETWORKS", None))
print("EVM_REPLAY_STEP_SECONDS=", getattr(config, "EVM_REPLAY_STEP_SECONDS", None))

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

raw = con.execute("SELECT value FROM meta WHERE key='evm_repair_cohort'").fetchone()
if raw is None:
    print("\nevm_repair_cohort: ABSENT")
else:
    c = json.loads(raw[0])
    print("\nevm_repair_cohort captured_at =", c.get("captured_at"),
          "networks =", c.get("networks"),
          "active_backfills =", len(c.get("active_backfills", [])),
          "replay_windows =", len(c.get("replay_windows", [])))

FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("\nper-network: populated vs NULL onchain-conc, with built_at range")
for r in con.execute(f"""SELECT network_id,
        SUM(onchain_age_min IS NULL) nulls, SUM(onchain_age_min IS NOT NULL) filled,
        MIN(CASE WHEN onchain_age_min IS NULL THEN built_at END) nb0,
        MAX(CASE WHEN onchain_age_min IS NULL THEN built_at END) nb1,
        MIN(CASE WHEN onchain_age_min IS NOT NULL THEN built_at END) fb0,
        MAX(CASE WHEN onchain_age_min IS NOT NULL THEN built_at END) fb1
      FROM training_rows WHERE {POP} GROUP BY network_id ORDER BY 2 DESC"""):
    print(f"  net={r['network_id']:<12} null={r['nulls']:<5} filled={r['filled']:<5} "
          f"nullbuilt={str(r['nb0'])[:16]}..{str(r['nb1'])[:16]}  "
          f"filledbuilt={str(r['fb0'])[:16]}..{str(r['fb1'])[:16]}")

# whole table (not just population) for EVM networks
print("\nwhole training_rows (any kind/fv) by network: conc filled?")
for r in con.execute("""SELECT network_id, feature_version,
        SUM(onchain_age_min IS NOT NULL) filled, COUNT(*) n
      FROM training_rows GROUP BY network_id, feature_version ORDER BY n DESC LIMIT 20"""):
    print("  ", dict(r))
