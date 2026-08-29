import os, sqlite3, config
from collections import Counter
import phase1_analysis as p1

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, pr=(): con.execute(s, pr).fetchall()

print("=== 1. outcomes kind='watch': my own (window fn) earlier-within-48h ===")
r = q("""WITH o AS (
           SELECT token_address, COALESCE(network_id,'') net, entry_ts, is_control,
                  MAX(entry_ts) OVER (
                    PARTITION BY token_address, COALESCE(network_id,'')
                    ORDER BY entry_ts
                    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prev_ts
             FROM outcomes WHERE kind='watch')
         SELECT COUNT(*) total,
                SUM(prev_ts IS NOT NULL AND entry_ts-prev_ts < 48*3600) within48
           FROM o""")[0]
print(dict(r))

print("\n=== 2. phase1_watch_outcomes: rows/coins per arm + top10 concentration ===")
r = q("SELECT COUNT(*) n, COUNT(DISTINCT token_address||':'||COALESCE(network_id,'')) coins FROM phase1_watch_outcomes")[0]
print("view total:", dict(r))
for r in q("""SELECT is_control, COUNT(*) n,
                     COUNT(DISTINCT token_address||':'||COALESCE(network_id,'')) coins
                FROM phase1_watch_outcomes GROUP BY 1"""):
    print(dict(r))
r = q("""SELECT SUM(c) top10 FROM (SELECT COUNT(*) c FROM phase1_watch_outcomes
          GROUP BY token_address, COALESCE(network_id,'') ORDER BY c DESC LIMIT 10)""")[0]
tot = q("SELECT COUNT(*) n FROM phase1_watch_outcomes")[0]["n"]
print("top10 rows:", r["top10"], "of", tot, "=", round(100.0*r["top10"]/tot,2), "%")

print("\n=== 3. THE ACTUAL PHASE-1 SAMPLE (phase1_analysis._read_rows -> _deduplicate_tokens) ===")
raw = p1._read_rows(config.DB_PATH, include_retro_controls=False)
print("raw rows read by the analysis:", len(raw))
print("raw distinct coins:", len({(r['token_address'], str(r['network_id'] or '')) for r in raw}))
c = Counter((r['token_address'], str(r['network_id'] or '')) for r in raw)
print("raw top10 share:", sum(v for _, v in c.most_common(10)), "of", len(raw),
      "=", round(100.0*sum(v for _, v in c.most_common(10))/len(raw), 2), "%")

kept, excl = p1._deduplicate_tokens(raw)
print("\nAFTER dedup -> rows entering the statistics:", len(kept))
print("exclusions reported by the analysis:", excl)
kc = Counter((r['token_address'], str(r['network_id'] or '')) for r in kept)
print("max windows per coin AFTER dedup:", max(kc.values()) if kc else 0)
print("top10 share AFTER dedup:", sum(v for _, v in kc.most_common(10)), "of", len(kept),
      "=", round(100.0*sum(v for _, v in kc.most_common(10))/len(kept), 2), "%")
arms = Counter(r['is_control'] for r in kept)
print("arm sizes after dedup (0=signal,1=control):", dict(arms))
okarms = Counter(r['is_control'] for r in kept if r['status'] == 'ok')
print("status=ok per arm (the numbers the report's stats use):", dict(okarms))

print("\n=== 4. training_rows: are kind='watch' rows in the model population? ===")
for r in q("""SELECT kind, COUNT(*) n FROM training_rows GROUP BY 1 ORDER BY n DESC"""):
    print(dict(r))
fv = q("SELECT CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER) v")[0]["v"]
print("current_feature_version:", fv)
r = q("""SELECT COUNT(*) n FROM training_rows
          WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
            AND is_independent=1 AND feature_version=?""", (fv,))[0]
print("model population (kind='signal' filters):", dict(r))
r = q("""SELECT COUNT(*) n FROM training_rows
          WHERE kind='watch' AND is_live=1 AND asset_class='meme' AND status='ok'
            AND is_independent=1 AND feature_version=?""", (fv,))[0]
print("kind='watch' rows that could pass the model filters:", dict(r))
for r in q("""SELECT kind, is_independent, COUNT(*) n FROM training_rows
                GROUP BY 1,2 ORDER BY kind, n DESC"""):
    print(dict(r))

con.close()
