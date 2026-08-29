import os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config, features

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
q = lambda s, p=(): con.execute(s, p).fetchall()

def show(title, rows):
    print("\n== " + title)
    for r in rows:
        print("   ", dict(r))

print("DB:", config.DB_PATH, os.path.getsize(config.DB_PATH))

# --- table existence / sizes
show("tables", q("SELECT name,type FROM sqlite_master WHERE name IN "
                 "('outcomes','watch_windows','training_rows','model_training_rows',"
                 "'phase1_watch_outcomes','signal_events','bars_fetch_state')"))

for t in ("outcomes","watch_windows","training_rows","signal_events"):
    print(t, "count =", q(f"SELECT COUNT(*) c FROM {t}")[0]["c"])

print("\ncurrent_feature_version =",
      q("SELECT value FROM meta WHERE key='current_feature_version'")[0]["value"])
print("features.FEATURE_VERSION =", features.FEATURE_VERSION)

# --- extra columns in live training_rows vs features.ROW_COLUMNS
live_cols = [r["name"] for r in q("PRAGMA table_info(training_rows)")]
rc = set(features.ROW_COLUMNS)
print("\ntraining_rows live cols =", len(live_cols), " ROW_COLUMNS =", len(features.ROW_COLUMNS))
print("in table NOT in ROW_COLUMNS:", [c for c in live_cols if c not in rc])
print("in ROW_COLUMNS NOT in table:", [c for c in features.ROW_COLUMNS if c not in set(live_cols)])

# --- outcomes breakdown
show("outcomes by kind,status", q(
    "SELECT kind, status, COUNT(*) n FROM outcomes GROUP BY kind, status ORDER BY kind, n DESC"))
show("outcomes by kind,is_control,design_version", q(
    "SELECT kind, is_control, design_version, COUNT(*) n FROM outcomes "
    "GROUP BY 1,2,3 ORDER BY 1,2,3"))

# --- training_rows breakdown
show("training_rows by kind,status,is_live", q(
    "SELECT kind, status, is_live, COUNT(*) n FROM training_rows GROUP BY 1,2,3 ORDER BY n DESC"))

MODEL = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
         "AND is_independent=1 AND feature_version = CAST(COALESCE("
         "(SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)")
print("\nmodel population (base table, no dedup) =",
      q(f"SELECT COUNT(*) c FROM training_rows WHERE {MODEL}")[0]["c"])

# --- join integrity training_rows -> outcomes
print("\ntraining_rows with NO outcomes row on (kind,key) =", q(
    "SELECT COUNT(*) c FROM training_rows r WHERE NOT EXISTS "
    "(SELECT 1 FROM outcomes o WHERE o.kind=r.kind AND o.key=r.key)")[0]["c"])
show("training_rows.status != outcomes.status", q(
    "SELECT r.kind, r.status rstat, o.status ostat, COUNT(*) n FROM training_rows r "
    "JOIN outcomes o ON o.kind=r.kind AND o.key=r.key WHERE r.status IS NOT o.status "
    "GROUP BY 1,2,3"))

# --- outcomes(kind=watch) -> watch_windows
print("\noutcomes kind=watch with no watch_windows parent =", q(
    "SELECT COUNT(*) c FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS "
    "(SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at = o.key)")[0]["c"])
# --- outcomes(kind=signal) -> signal_events
print("outcomes kind=signal with no signal_events parent =", q(
    "SELECT COUNT(*) c FROM outcomes o WHERE o.kind='signal' AND NOT EXISTS "
    "(SELECT 1 FROM signal_events s WHERE s.id=o.key)")[0]["c"])

print("\nwatch_windows by source,is_control,design_version:")
show("", q("SELECT source, is_control, design_version, COUNT(*) n FROM watch_windows GROUP BY 1,2,3 ORDER BY n DESC"))
con.close()
