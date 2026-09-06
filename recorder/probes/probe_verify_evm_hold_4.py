import os, sqlite3, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
COHORT = int(dt.datetime.fromisoformat("2026-08-20T17:29:42.250141+00:00").timestamp())
print("cohort epoch =", COHORT)
q = """SELECT COUNT(*) FROM training_rows WHERE network_id IN ('143','4663','8453') AND entry_ts <= ?"""
print("EVM training rows finalize_training would DELETE:", con.execute(q, (COHORT,)).fetchone()[0])
q2 = """SELECT COUNT(*) FROM training_rows WHERE network_id IN ('143','4663','8453') AND entry_ts <= ?
          AND kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
          AND is_independent=1 AND feature_version=12"""
print("  of which currently IN the model population:", con.execute(q2, (COHORT,)).fetchone()[0])
print("total EVM(in-cohort) training rows any fv:",
      con.execute("SELECT COUNT(*) FROM training_rows WHERE network_id IN ('143','4663','8453')").fetchone()[0])
print("model population net 56 (BSC, EVM but outside repair scope):",
      con.execute("""SELECT COUNT(*), MAX(entry_ts) FROM training_rows WHERE network_id='56'
                      AND kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
                      AND is_independent=1 AND feature_version=12""").fetchone())
con.close()
