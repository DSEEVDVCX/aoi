import os, sqlite3, json, config, datetime as dt

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

c = json.loads(con.execute("SELECT value FROM meta WHERE key='evm_repair_cohort'").fetchone()[0])
cut = int(dt.datetime.fromisoformat(c["captured_at"].replace("Z", "+00:00")).timestamp())
print("cohort cutoff epoch =", cut, dt.datetime.utcfromtimestamp(cut))

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")
NETS = ("4663", "8453", "143")
marks = ",".join("?" for _ in NETS)

print("\nEVM-repair-set population rows, all inside finalize cutoff?")
for r in con.execute(f"""SELECT network_id, COUNT(*) n,
                                SUM(entry_ts <= ?) inside,
                                SUM(onchain_age_min IS NULL) nulls
                           FROM training_rows WHERE {POP} AND network_id IN ({marks})
                          GROUP BY network_id""", (cut, *NETS)):
    print("  ", dict(r))

# the 659 specifically
n659 = con.execute(f"""SELECT COUNT(*) FROM training_rows t
        WHERE {POP} AND t.onchain_age_min IS NULL AND t.entry_ts <= ?
          AND EXISTS (SELECT 1 FROM chain_concentration c
                       WHERE c.token_address=t.token_address AND c.network_id=t.network_id
                         AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts)""",
    (cut,)).fetchone()[0]
print("\nrecoverable rows that are ALSO inside the finalize cutoff:", n659)

# whole-table scope of the deliberate NULLing
r = con.execute(f"""SELECT COUNT(*) n, SUM(onchain_age_min IS NOT NULL) filled
                     FROM training_rows WHERE network_id IN ({marks})""", NETS).fetchone()
print(f"\nwhole training_rows on nets {NETS}: {r['n']} rows, {r['filled']} with any conc value")

# their exact SQL, verbatim
their = """SELECT COUNT(*) n, COUNT(DISTINCT t.token_address) tokens,
 MIN(date(t.entry_ts,'unixepoch')) a, MAX(date(t.entry_ts,'unixepoch')) b
 FROM training_rows t WHERE t.kind='signal' AND t.is_live=1 AND t.asset_class='meme'
 AND t.status='ok' AND t.is_independent=1
 AND t.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
 AND t.onchain_top1_pct IS NULL AND t.onchain_top5_pct IS NULL AND t.onchain_top10_pct IS NULL
 AND t.onchain_top20_pct IS NULL AND t.onchain_top_accounts IS NULL AND t.onchain_age_min IS NULL
 AND t.onchain_top1_delta_5m IS NULL AND t.onchain_top10_delta_5m IS NULL
 AND t.onchain_delta_span_min IS NULL AND t.onchain_holder_count IS NULL
 AND t.onchain_holders_delta_5m IS NULL
 AND EXISTS (SELECT 1 FROM chain_concentration c WHERE c.token_address=t.token_address
   AND c.network_id=t.network_id AND c.top1_pct IS NOT NULL
   AND CAST(strftime('%s',c.recorded_at) AS INTEGER) <= t.entry_ts)"""
print("\ntheir verbatim SQL:", dict(con.execute(their).fetchone()))
