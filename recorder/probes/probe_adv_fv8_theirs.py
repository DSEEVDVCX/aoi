import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
print("--- their Q1 verbatim ---")
for r in con.execute("SELECT kind, feature_version, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY 1,2"):
    print("  ", r["kind"], r["feature_version"], r["n"])
print("--- their Q2 verbatim ---")
for r in con.execute("SELECT kind, network_id, COUNT(*) n FROM training_rows WHERE feature_version=8 GROUP BY 1,2 ORDER BY n DESC"):
    print("  ", r["kind"], r["network_id"], r["n"])
print("--- their Q3 verbatim ---")
print("  ", con.execute("SELECT COUNT(*) FROM training_rows WHERE feature_version<>12").fetchone()[0])
print("--- finalize_training blockers: non-terminal replay/backfill on the held nets ---")
for r in con.execute("""SELECT 'replay' src, COUNT(*) n FROM evm_replay_state
                        WHERE status NOT IN ('done','empty')
                        UNION ALL SELECT 'backfill', COUNT(*) FROM evm_backfill_state
                        WHERE status NOT IN ('done',)"""):
    print("  ", r["src"], r["n"])
con.close()
