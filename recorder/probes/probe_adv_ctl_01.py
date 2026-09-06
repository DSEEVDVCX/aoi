import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("DB:", config.DB_PATH)
print("MIN_TOKEN_AGE_DAYS =", config.MIN_TOKEN_AGE_DAYS)
print("LIVE_START_TS =", getattr(config, "LIVE_START_TS", None))

print("\n=== A) meta keys about the age gate / recorder start ===")
for r in con.execute(
    "SELECT key, value FROM meta WHERE key LIKE '%age%' OR key IN "
    "('started_at','last_cycle_at','recorder_last_run_at') ORDER BY key"
):
    print(f"  {r['key']:38s} = {r['value']}")

print("\n=== B) watch_windows by design_version x is_control ===")
for r in con.execute(
    "SELECT design_version dv, is_control, COUNT(*) windows, "
    "COUNT(DISTINCT token_address||':'||network_id) coins, "
    "MIN(first_seen_at) lo, MAX(first_seen_at) hi "
    "FROM watch_windows GROUP BY dv, is_control ORDER BY dv, is_control"
):
    print(f"  dv={r['dv']} ctl={r['is_control']}  windows={r['windows']:5d} coins={r['coins']:5d}  {r['lo']} .. {r['hi']}")

print("\n=== C) watch_windows by admission_source x is_control (dv>=3) ===")
for r in con.execute(
    "SELECT COALESCE(admission_source,'(null)') src, is_control, COUNT(*) n "
    "FROM watch_windows WHERE design_version>=3 GROUP BY src, is_control ORDER BY src, is_control"
):
    print(f"  src={r['src']:10s} ctl={r['is_control']}  n={r['n']}")

print("\n=== D) token_created_at magnitude (digit length) for coins that have windows ===")
for r in con.execute(
    """SELECT LENGTH(CAST(CAST(s.token_created_at AS INTEGER) AS TEXT)) digits,
              COUNT(*) n, MIN(s.token_created_at) lo, MAX(s.token_created_at) hi
         FROM token_static s
         JOIN (SELECT DISTINCT token_address, network_id FROM watch_windows) w
           ON w.token_address=s.token_address AND w.network_id=s.network_id
        WHERE s.token_created_at IS NOT NULL AND s.token_created_at <> ''
        GROUP BY digits ORDER BY digits"""
):
    print(f"  digits={r['digits']}  n={r['n']}  range {r['lo']} .. {r['hi']}")

print("\n=== E) window coins missing a token_static row / missing created_at ===")
r = con.execute(
    """SELECT
         (SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM watch_windows)) coins_total,
         (SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM watch_windows) w
            WHERE NOT EXISTS (SELECT 1 FROM token_static s
                               WHERE s.token_address=w.token_address AND s.network_id=w.network_id)) no_static,
         (SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM watch_windows) w
            WHERE EXISTS (SELECT 1 FROM token_static s
                           WHERE s.token_address=w.token_address AND s.network_id=w.network_id
                             AND (s.token_created_at IS NULL OR s.token_created_at=''))) static_no_date
    """
).fetchone()
print(dict(r))
con.close()
