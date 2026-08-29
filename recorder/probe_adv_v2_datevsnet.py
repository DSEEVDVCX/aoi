"""DECISIVE TEST: is the explanatory variable NETWORK or ENTRY DATE?
features.py docstrings give two hard collector start dates:
  holders_features/flow_features: "كل الصفوف قبل 2026-08-09 ستكون None هنا"
  onchain_features (EVM in chain_concentration): "الشبكتان معاً منذ 2026-08-13"
The 4663/8453 model rows all have entry_ts <= 2026-08-04. So compare
like-for-like: net 56 / Solana rows in the SAME pre-08-09 era."""
import os, sqlite3, datetime as dt
import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1 AND feature_version = CAST(COALESCE(
             (SELECT value FROM meta WHERE key='current_feature_version'),'0')
             AS INTEGER)"""

CUT_HOLD = int(dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc).timestamp())
CUT_CC = int(dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc).timestamp())
print("cutoff 2026-08-09 epoch =", CUT_HOLD, " 2026-08-13 epoch =", CUT_CC)

print("\n=== A. flow/holders fill by (network, entry before/after 2026-08-09) ===")
q = f"""SELECT network_id,
        CASE WHEN entry_ts < {CUT_HOLD} THEN 'pre-08-09' ELSE 'post-08-09' END era,
        COUNT(*) n,
        COUNT(flow_buy_volume_5m) flow,
        COUNT(chain_holder_count) chain_hc,
        COUNT(platform_holders) plat_h
        FROM training_rows WHERE {POP}
        GROUP BY 1,2 ORDER BY 1,2"""
for r in con.execute(q):
    n = r["n"]
    print(f"  net={str(r['network_id']):>12} {r['era']:>10} n={n:>5}  "
          f"flow={r['flow']:>5} ({100.0*r['flow']/n:5.1f}%)  "
          f"chain_hc={r['chain_hc']:>5} ({100.0*r['chain_hc']/n:5.1f}%)  "
          f"plat_h={r['plat_h']:>5} ({100.0*r['plat_h']/n:5.1f}%)")

print("\n=== B. onchain concentration fill by (network, entry before/after 08-13) ===")
q = f"""SELECT network_id,
        CASE WHEN entry_ts < {CUT_CC} THEN 'pre-08-13' ELSE 'post-08-13' END era,
        COUNT(*) n, COUNT(onchain_top10_pct) t10,
        COUNT(onchain_holder_count) o_hc,
        COUNT(onchain_has_mint_authority) mint_auth,
        COUNT(onchain_code_size) code_sz
        FROM training_rows WHERE {POP}
        GROUP BY 1,2 ORDER BY 1,2"""
for r in con.execute(q):
    n = r["n"]
    print(f"  net={str(r['network_id']):>12} {r['era']:>10} n={n:>5}  "
          f"t10={r['t10']:>5} ({100.0*r['t10']/n:5.1f}%)  o_hc={r['o_hc']:>5}  "
          f"mint_auth={r['mint_auth']:>5}  code_sz={r['code_sz']:>5}")

print("\n=== C. do 4663/8453 signals even exist after 2026-08-04? "
      "(unfiltered training_rows, then signal_events) ===")
for net in ("4663", "8453"):
    r = con.execute(
        """SELECT COUNT(*) n, MAX(entry_ts) mx FROM training_rows
            WHERE kind='signal' AND network_id=?""", (net,)).fetchone()
    print(f"  training_rows ALL filters off  net={net}: n={r['n']} "
          f"max entry_ts={dt.datetime.utcfromtimestamp(r['mx']).isoformat() if r['mx'] else None}")

print("\n  signal_events per network, monthly-ish buckets:")
for r in con.execute("""SELECT network_id, substr(ts_utc,1,10) d, COUNT(*) n
                          FROM signal_events
                         WHERE network_id IN ('4663','8453')
                           AND ts_utc >= '2026-08-01'
                         GROUP BY 1,2 ORDER BY 1,2"""):
    print(f"    net={r['network_id']:>6} {r['d']} n={r['n']}")
