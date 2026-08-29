import os, sqlite3, config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = (f"FROM training_rows WHERE kind='signal' AND is_live=1 AND asset_class='meme' "
       f"AND status='ok' AND is_independent=1 AND feature_version = {FV}")

print("=== log_size_usd NULL breakdown ===")
x = con.execute(f"""SELECT
  SUM(CASE WHEN log_size_usd IS NULL THEN 1 ELSE 0 END) log_null,
  SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) sz_null,
  SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) sz_zero,
  SUM(CASE WHEN size_usd<0 THEN 1 ELSE 0 END) sz_neg,
  SUM(CASE WHEN size_usd<0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) neg_lognull,
  SUM(CASE WHEN size_usd>0 AND log_size_usd IS NULL THEN 1 ELSE 0 END) pos_lognull
  {POP}""").fetchone()
print("  ", dict(x))
print("  min/max size_usd:", dict(con.execute(
    f"SELECT MIN(size_usd) mn, MAX(size_usd) mx {POP}").fetchone()))
print("\n  by signal_type where size_usd<=0:")
for r in con.execute(f"""SELECT signal_type, COUNT(*) n,
      SUM(CASE WHEN size_usd=0 THEN 1 ELSE 0 END) z,
      SUM(CASE WHEN size_usd<0 THEN 1 ELSE 0 END) neg
      {POP} GROUP BY signal_type ORDER BY n DESC LIMIT 10""").fetchall():
    print("   ", dict(r))
con.close()
