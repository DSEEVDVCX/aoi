"""Honest scope: how many model rows are empty across ALL FIVE families the
claim names, split by era rather than by network."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row
POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version = CAST(COALESCE(
             (SELECT value FROM meta WHERE key='current_feature_version'),'0')
             AS INTEGER)"""
CUT = int(dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc).timestamp())
EMPTY = """flow_buy_volume_5m IS NULL AND chain_holder_count IS NULL
           AND platform_holders IS NULL AND onchain_top10_pct IS NULL
           AND onchain_has_mint_authority IS NULL AND onchain_code_size IS NULL"""

r = con.execute(f"""SELECT
    COUNT(*) tot,
    SUM(CASE WHEN {EMPTY} THEN 1 ELSE 0 END) all_empty,
    SUM(CASE WHEN entry_ts < {CUT} THEN 1 ELSE 0 END) pre,
    SUM(CASE WHEN entry_ts < {CUT} AND {EMPTY} THEN 1 ELSE 0 END) pre_empty,
    SUM(CASE WHEN entry_ts >= {CUT} AND {EMPTY} THEN 1 ELSE 0 END) post_empty,
    SUM(CASE WHEN network_id IN ('4663','8453') AND {EMPTY} THEN 1 ELSE 0 END) claim_nets
    FROM training_rows WHERE {POP}""").fetchone()
print(dict(r))
print(f"\nmodel pop            = {r['tot']}")
print(f"empty in ALL 5 fams  = {r['all_empty']}  ({100.0*r['all_empty']/r['tot']:.2f}%)")
print(f"  of which pre-08-09 = {r['pre_empty']}")
print(f"  of which post      = {r['post_empty']}")
print(f"pre-08-09 rows total = {r['pre']}")
print(f"claim's 4663+8453    = {r['claim_nets']}  "
      f"({100.0*r['claim_nets']/r['all_empty']:.1f}% of the all-empty set)")
