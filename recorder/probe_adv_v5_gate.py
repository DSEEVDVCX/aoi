"""Why has nothing on 4663/8453 been built since 08-14? Read the gate."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

print("config.EVM_NETWORKS        =", getattr(config, "EVM_NETWORKS", "MISSING"))
print("config.EVM_REPLAY_NETWORKS =", getattr(config, "EVM_REPLAY_NETWORKS", "MISSING"))

print("\n-- meta keys governing the EVM training rebuild --")
for r in con.execute("""SELECT key, substr(value,1,80) v FROM meta
                         WHERE key LIKE '%rebuild%' OR key LIKE '%replay%'
                            OR key LIKE '%ledger%' OR key='current_feature_version'
                         ORDER BY key"""):
    print(f"   {r['key']:<48} = {r['v']}")

print("\n-- outcomes waiting to be built, by network (the held-back backlog) --")
for r in con.execute("""SELECT COALESCE(o.network_id,'?') net, COUNT(*) n,
                          MIN(o.entry_ts) mn, MAX(o.entry_ts) mx
                        FROM outcomes o
                       WHERE o.status IN ('ok','no_bars') AND o.kind='signal'
                         AND NOT EXISTS (SELECT 1 FROM training_rows r
                              WHERE r.kind=o.kind AND r.key=o.key
                                AND r.feature_version=12)
                       GROUP BY 1 ORDER BY n DESC"""):
    import datetime as dt
    f = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "?"
    print(f"   net={r['net']:>12} pending={r['n']:>6}  entry {f(r['mn'])} -> {f(r['mx'])}")
