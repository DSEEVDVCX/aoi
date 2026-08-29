import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

print("LIVE_START_TS =", config.LIVE_START_TS, " LAG =", config.LABEL_ENTRY_MAX_LAG_SECONDS)

print("\n=== A) my own pivot: kind x status over ALL outcomes ===")
for r in q("""SELECT kind,
                     SUM(status='ok')         AS ok,
                     SUM(status='no_bars')    AS no_bars,
                     SUM(status='no_entry')   AS no_entry,
                     SUM(status='incomplete') AS incomplete,
                     COUNT(*) AS total
                FROM outcomes GROUP BY kind ORDER BY kind"""):
    print(dict(r))

print("\n=== B) signal no_entry decomposed by live / independent (my CASE form) ===")
for r in q("""SELECT (entry_ts >= ?) AS is_live,
                     COALESCE(is_independent,-1) AS indep,
                     COUNT(*) AS n
                FROM outcomes
               WHERE kind='signal' AND status='no_entry'
               GROUP BY 1,2 ORDER BY 1,2""", (config.LIVE_START_TS,)):
    print(dict(r))

print("\n=== C) their exact SQL, for agreement check ===")
print("their 924 ->", q("SELECT COUNT(*) c FROM outcomes WHERE kind='signal' AND status='no_entry' AND entry_ts>=1785018927 AND is_independent=1")[0]["c"])
for r in q("SELECT kind, COUNT(*) c FROM outcomes WHERE status='no_entry' GROUP BY 1"):
    print("  ", r["kind"], r["c"])
for r in q("SELECT COALESCE(network_id,'') n, COUNT(*) c FROM outcomes WHERE kind='signal' AND status='no_entry' GROUP BY 1 ORDER BY 2 DESC"):
    print("  net", r["n"], r["c"])

print("\n=== D) network breakdown of the 924 subset only (they showed the 1554 split) ===")
for r in q("""SELECT COALESCE(network_id,'') n, COUNT(*) c
                FROM outcomes
               WHERE kind='signal' AND status='no_entry'
                 AND entry_ts >= ? AND is_independent=1
               GROUP BY 1 ORDER BY 2 DESC""", (config.LIVE_START_TS,)):
    print("  net", r["n"], r["c"])

print("\n=== E) the live independent signal denominator by status ===")
for r in q("""SELECT status, COUNT(*) c
                FROM outcomes
               WHERE kind='signal' AND entry_ts >= ? AND is_independent=1
               GROUP BY 1 ORDER BY 2 DESC""", (config.LIVE_START_TS,)):
    print("  ", r["status"], r["c"])

print("\n=== F) model population (base table, cheap filters) ===")
fv = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
print("current_feature_version =", fv)
print("model population =", q("""SELECT COUNT(*) c FROM training_rows
     WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
       AND is_independent=1 AND feature_version=?""", (fv,))[0]["c"])
print("training_rows kind=signal live indep, any status/class:",
      q("SELECT COUNT(*) c FROM training_rows WHERE kind='signal' AND is_live=1 AND is_independent=1 AND feature_version=?", (fv,))[0]["c"])
con.close()
