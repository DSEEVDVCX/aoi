"""Test the claim's one remaining nugget: "659 of the 4663 rows would gain the
on-chain concentration family immediately on a rebuild."
Two ways that can be false:
 (1) it is not 4663-specific -- the same is true for pre-08-13 rows on 56/SOL;
 (2) the reachable chain_concentration rows are replay rows whose top10_pct is
     itself NULL, so a rebuild would copy NULL into NULL.
Uses features.py's EXACT predicate (no staleness cap, no is_replay filter)."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=600)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version = CAST(COALESCE(
             (SELECT value FROM meta WHERE key='current_feature_version'),'0')
             AS INTEGER)"""
CUT_CC = int(dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc).timestamp())

print("=== chain_concentration inventory per network (is_replay split) ===")
for r in con.execute("""SELECT network_id, is_replay, COUNT(*) n,
                          COUNT(top10_pct) has_t10, COUNT(top1_pct) has_t1,
                          COUNT(holder_count) has_hc,
                          MIN(recorded_at) mn, MAX(recorded_at) mx
                        FROM chain_concentration GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"  net={str(r['network_id']):>12} is_replay={r['is_replay']} n={r['n']:>7} "
          f"top10 non-null={r['has_t10']:>7} top1={r['has_t1']:>7} hc={r['has_hc']:>7} "
          f"| {str(r['mn'])[:19]} -> {str(r['mx'])[:19]}")

print("\n=== reachable-NOW at/before entry_ts, and whether the snapshot carries "
      "a non-null top10_pct, for PRE-08-13 model rows on every network ===")
q = f"""SELECT network_id, COUNT(*) n,
        SUM(EXISTS(SELECT 1 FROM chain_concentration c
                    WHERE c.token_address=tr.token_address
                      AND c.network_id=tr.network_id
                      AND CAST(strftime('%s',c.recorded_at) AS INTEGER)
                          <= tr.entry_ts)) reachable_any,
        SUM(COALESCE((SELECT c.top10_pct IS NOT NULL FROM chain_concentration c
                       WHERE c.token_address=tr.token_address
                         AND c.network_id=tr.network_id
                         AND CAST(strftime('%s',c.recorded_at) AS INTEGER)
                             <= tr.entry_ts
                       ORDER BY CAST(strftime('%s',c.recorded_at) AS INTEGER) DESC
                       LIMIT 1),0)) would_fill_t10,
        COUNT(onchain_top10_pct) filled_today
        FROM training_rows tr
        WHERE {POP} AND entry_ts < {CUT_CC}
        GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print(f"  net={str(r['network_id']):>12} pre0813_rows={r['n']:>5} "
          f"reachable_any={r['reachable_any']:>5} would_fill_top10={r['would_fill_t10']:>5} "
          f"filled_today={r['filled_today']:>5}")
