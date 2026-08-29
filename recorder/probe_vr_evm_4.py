import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(s,p=()): return con.execute(s,p).fetchall()
for t in ("chain_concentration","evm_backfill_state","evm_replay_state"):
    cols = [r["name"] for r in q(f"PRAGMA table_info({t})")]
    print(t, "->", cols)
con.close()
