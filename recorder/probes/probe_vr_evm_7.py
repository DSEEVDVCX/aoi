import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(s,p=()): return con.execute(s,p).fetchall()

CC = ("onchain_top1_pct","onchain_top5_pct","onchain_top10_pct","onchain_top20_pct",
      "onchain_top_accounts","onchain_age_min","onchain_top1_delta_5m",
      "onchain_top10_delta_5m","onchain_delta_span_min","onchain_holder_count",
      "onchain_holders_delta_5m")
sel = ", ".join(f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END) AS f_{c}" for c in CC)

print("=== the 1,699 fv=8 model-shaped rows: were the concentration cols stripped by reset()? ===")
for net in ("4663","8453"):
    r = q(f"""SELECT COUNT(*) n, {sel} FROM training_rows
              WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                AND is_independent=1 AND feature_version=8 AND network_id=?""", (net,))[0]
    print(f"  net={net}  rows={r['n']}")
    for c in CC:
        print(f"      {c:<26} non-null={r['f_'+c]}")

print("\n=== same family on the CURRENT-fv model rows, per net (control comparison) ===")
for net in ("1399811149","56","4663","8453"):
    r = q(f"""SELECT COUNT(*) n, {sel} FROM training_rows
              WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                AND is_independent=1 AND feature_version=12 AND network_id=?""", (net,))[0]
    nn = {c: r["f_"+c] for c in CC}
    print(f"  net={net:<12} rows={r['n']:<6} onchain_top1_pct non-null={nn['onchain_top1_pct']}"
          f"  top10={nn['onchain_top10_pct']}  holder_count={nn['onchain_holder_count']}")
con.close()
