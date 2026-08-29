"""Check the always-constant source fields. Read-only."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("token_social columns:", [c["name"] for c in con.execute("PRAGMA table_info(token_social)")])
for r in con.execute("""SELECT COUNT(*) n,
   SUM(CASE WHEN thesis_replies IS NULL THEN 1 ELSE 0 END) nul,
   SUM(CASE WHEN thesis_replies=0 THEN 1 ELSE 0 END) z,
   COUNT(DISTINCT thesis_replies) d, MAX(thesis_replies) mx FROM token_social"""):
    print("token_social.thesis_replies:", dict(r))
print()
print("token_static.exchanges_count distribution:")
for r in con.execute("""SELECT exchanges_count, COUNT(*) n FROM token_static
   GROUP BY 1 ORDER BY 1"""):
    print("  ", r["exchanges_count"], r["n"])
print()
print("token_static.is_scam distribution:")
for r in con.execute("SELECT is_scam, COUNT(*) n FROM token_static GROUP BY 1 ORDER BY 2 DESC"):
    print("  ", r["is_scam"], r["n"])
