import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from features import epoch_of

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")

print("=== A model-pop rows where the guard-VISIBLE static row has created_at present but age is NULL ===")
print("    (this is the ONLY way the 'postdating created_at' can cost the model a row)")
for r in con.execute(f"""
WITH p AS (SELECT token_address ta, network_id ni, entry_ts, token_age_h FROM training_rows WHERE {POP}),
     v AS (SELECT p.*, (SELECT s.token_created_at FROM token_static s
                         WHERE s.token_address=p.ta AND s.network_id=p.ni
                           AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= p.entry_ts
                         ORDER BY s.recorded_at DESC LIMIT 1) tca,
                       (SELECT 1 FROM token_static s
                         WHERE s.token_address=p.ta AND s.network_id=p.ni
                           AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= p.entry_ts
                         LIMIT 1) visible FROM p)
SELECT SUM(CASE WHEN visible IS NULL THEN 1 ELSE 0 END) guard_excluded_any_age,
       SUM(CASE WHEN visible=1 AND token_age_h IS NULL AND tca IS NULL THEN 1 ELSE 0 END) visible_created_null,
       SUM(CASE WHEN visible=1 AND token_age_h IS NULL AND tca IS NOT NULL THEN 1 ELSE 0 END) visible_created_present_age_null,
       SUM(CASE WHEN visible IS NULL AND token_age_h IS NULL THEN 1 ELSE 0 END) guard_excluded_age_null,
       COUNT(*) total
  FROM v"""):
    print(f"    guard-excluded rows (whole static family NULL by design): {r['guard_excluded_any_age']}")
    print(f"      of which token_age_h NULL: {r['guard_excluded_age_null']}")
    print(f"    visible static + created_at NULL  -> age NULL: {r['visible_created_null']}")
    print(f"    visible static + created_at PRESENT -> age NULL (negative suppressed): {r['visible_created_present_age_null']}")
    print(f"    model population total: {r['total']}")
print()

print("=== B active watches missing token_created_at (the claim's '0 of 201') ===")
for r in con.execute("""
SELECT COUNT(*) active_watches,
       SUM(CASE WHEN s.token_created_at IS NULL THEN 1 ELSE 0 END) missing_created,
       SUM(CASE WHEN s.token_address IS NULL THEN 1 ELSE 0 END) no_static_row
  FROM watch_windows w LEFT JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE w.watch_until > strftime('%Y-%m-%dT%H:%M:%S','now')"""):
    print(f"    active windows={r['active_watches']} missing created_at={r['missing_created']} no static row={r['no_static_row']}")
print()

print("=== C did any window open for an unknown-age coin AFTER the gate went live? ===")
for r in con.execute("""
SELECT s.symbol, w.network_id, w.first_seen_at, w.source, w.is_control, w.design_version
  FROM watch_windows w JOIN token_static s
    ON s.token_address=w.token_address AND s.network_id=w.network_id
 WHERE s.token_created_at IS NULL AND w.first_seen_at >= '2026-08-22'
 ORDER BY w.first_seen_at"""):
    print(f"    {r['symbol']}/net{r['network_id']} first_seen={r['first_seen_at']} source={r['source']} is_control={r['is_control']} dv={r['design_version']}")
print()

print("=== D the 5 postdating coins: how many windows/rows each, and is 4663 special? ===")
for r in con.execute("""
SELECT network_id, COUNT(*) coins,
       SUM(CASE WHEN token_created_at IS NULL THEN 1 ELSE 0 END) created_null
  FROM token_static GROUP BY 1 ORDER BY coins DESC"""):
    print(f"    net={r['network_id']:>12} coins={r['coins']:5d} created_at NULL={r['created_null']}")
con.close()
