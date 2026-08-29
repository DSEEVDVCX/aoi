import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = con.execute
W = 48*3600
MODEL = """
  r.kind='signal' AND r.is_live=1 AND r.asset_class='meme' AND r.status='ok'
  AND r.is_independent=1
  AND r.feature_version = CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)
"""
# same probe, but only bars the labeler would actually see (bars_for drops NULL h/l/c)
for flag in (1, 0):
    r = q(f"""SELECT COUNT(*) tot,
      SUM(CASE WHEN EXISTS (SELECT 1 FROM token_bars b
            WHERE b.token_address=r.token_address AND b.network_id=COALESCE(r.network_id,'')
              AND b.resolution='5' AND b.h IS NOT NULL AND b.l IS NOT NULL AND b.c IS NOT NULL
              AND b.ts > r.entry_ts + {W} - 3600 AND b.ts <= r.entry_ts + {W})
          THEN 1 ELSE 0 END) usable_tail
      FROM training_rows r, outcomes o
      WHERE o.kind=r.kind AND o.key=r.key AND {MODEL} AND o.bars_truncated={flag}""").fetchone()
    print(f"bars_truncated={flag}: n={r['tot']}  usable 5m bar in final hour now = {r['usable_tail']}"
          f" ({100.0*r['usable_tail']/r['tot']:.1f}%)")
con.close()
