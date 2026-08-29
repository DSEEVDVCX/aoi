import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = """CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"""
POP = f"""kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version = {FV}"""

def q(sql, args=()):
    return con.execute(sql, args).fetchall()

print("=== A. my own framing: NOT NULL count, min/max, per-slice ===")
r = q("""SELECT COUNT(*) total,
                COUNT(top10_holders_pct) notnull_cnt,
                MIN(top10_holders_pct) mn, MAX(top10_holders_pct) mx
           FROM training_rows""")[0]
print("training_rows ALL:", dict(r))

r = q(f"""SELECT COUNT(*) total, COUNT(top10_holders_pct) notnull_cnt
            FROM training_rows WHERE {POP}""")[0]
print("training_rows MODEL POPULATION:", dict(r))

print("\n-- training_rows slices (kind / is_live / feature_version) --")
for row in q("""SELECT kind, is_live, feature_version, COUNT(*) n,
                       COUNT(top10_holders_pct) nn
                  FROM training_rows GROUP BY kind, is_live, feature_version
                 ORDER BY n DESC LIMIT 20"""):
    print(dict(row))

print("\n=== B. market_ticks, my framing + per source ===")
r = q("""SELECT COUNT(*) total, COUNT(top10_holders_pct) notnull_cnt
           FROM market_ticks""")[0]
print("market_ticks ALL:", dict(r))
for row in q("""SELECT source, COUNT(*) n, COUNT(top10_holders_pct) nn
                  FROM market_ticks GROUP BY source ORDER BY n DESC"""):
    print(dict(row))

print("\n=== C. also the `holders` sibling in the same rich family ===")
r = q("""SELECT COUNT(*) total, COUNT(holders) nn FROM market_ticks""")[0]
print("market_ticks.holders:", dict(r))

print("\n=== D. the documented REPLACEMENTS in the model population ===")
r = q(f"""SELECT COUNT(*) n,
                 COUNT(chain_top10_pct) chain_top10,
                 COUNT(chain_holder_count) chain_hc,
                 COUNT(onchain_top10_pct) onchain_top10,
                 COUNT(onchain_top1_pct) onchain_top1,
                 COUNT(platform_holders) plat_h
            FROM training_rows WHERE {POP}""")[0]
print("model population replacements:", dict(r))
n = r["n"]
for k in ("chain_top10", "onchain_top10", "onchain_top1", "plat_h", "chain_hc"):
    print(f"  {k}: {r[k]}/{n} = {100.0*r[k]/n:.2f}%")

print("\n=== E. any coalesced ownership-concentration signal at all? ===")
r = q(f"""SELECT COUNT(*) n,
                 SUM(chain_top10_pct IS NOT NULL OR onchain_top10_pct IS NOT NULL) either
            FROM training_rows WHERE {POP}""")[0]
print(dict(r), f"= {100.0*r['either']/r['n']:.2f}%")

print("\n=== F. token_holders.top10_pct — the live source that replaced it ===")
r = q("""SELECT COUNT(*) n, COUNT(top10_pct) nn, MIN(top10_pct) mn,
                MAX(top10_pct) mx FROM token_holders""")[0]
print("token_holders:", dict(r))
for row in q("""SELECT source, COUNT(*) n, COUNT(top10_pct) nn
                  FROM token_holders GROUP BY source"""):
    print(dict(row))

print("\n=== G. their exact SQL, for agreement check ===")
print(dict(q("""SELECT COUNT(*) n, SUM(top10_holders_pct IS NULL) nulls,
                       COUNT(DISTINCT top10_holders_pct) d FROM training_rows""")[0]))
print(dict(q("""SELECT COUNT(*) n, SUM(top10_holders_pct IS NULL) nulls,
                       SUM(top10_holders_pct=0) zeros,
                       COUNT(DISTINCT top10_holders_pct) d FROM market_ticks""")[0]))

print("\n=== H. current_feature_version ===")
print(dict(q("SELECT value FROM meta WHERE key='current_feature_version'")[0]))
con.close()
