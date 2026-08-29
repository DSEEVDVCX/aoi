import os, sqlite3, json, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

Q = {}

# --- 1. watch_windows -> token_static (exact join) ---
Q["ww_no_static_exact"] = """
SELECT COUNT(*) FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
    WHERE s.token_address = w.token_address AND s.network_id = w.network_id)
"""
Q["ww_no_static_exact_distinct_tokens"] = """
SELECT COUNT(*) FROM (SELECT DISTINCT w.token_address, w.network_id FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
    WHERE s.token_address = w.token_address AND s.network_id = w.network_id))
"""
Q["ww_no_static_lower"] = """
SELECT COUNT(*) FROM watch_windows w
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
    WHERE lower(s.token_address) = lower(w.token_address)
      AND CAST(s.network_id AS TEXT) = CAST(w.network_id AS TEXT))
"""

# --- 2. watchlist -> watch_windows ---
Q["wl_no_window"] = """
SELECT COUNT(*) FROM watchlist l
 WHERE NOT EXISTS (SELECT 1 FROM watch_windows w
    WHERE w.token_address = l.token_address AND w.network_id = l.network_id)
"""
Q["wl_no_static"] = """
SELECT COUNT(*) FROM watchlist l
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
    WHERE s.token_address = l.token_address AND s.network_id = l.network_id)
"""
# reverse: watch_windows token/net not present in watchlist at all
Q["ww_token_not_in_watchlist"] = """
SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM watch_windows) w
 WHERE NOT EXISTS (SELECT 1 FROM watchlist l
    WHERE l.token_address = w.token_address AND l.network_id = w.network_id)
"""
Q["static_not_in_watchlist"] = """
SELECT COUNT(*) FROM token_static s
 WHERE NOT EXISTS (SELECT 1 FROM watchlist l
    WHERE l.token_address = s.token_address AND l.network_id = s.network_id)
"""

# --- 3. entry_signal_id dangling ---
Q["ww_entry_sig_dangling"] = """
SELECT COUNT(*) FROM watch_windows w
 WHERE w.entry_signal_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = w.entry_signal_id)
"""
Q["wl_entry_sig_dangling"] = """
SELECT COUNT(*) FROM watchlist l
 WHERE l.entry_signal_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = l.entry_signal_id)
"""

# --- 4. training_rows -> signal_events / outcomes ---
Q["tr_kinds"] = "SELECT kind, COUNT(*) c FROM training_rows GROUP BY kind"
Q["tr_signal_key_no_event"] = """
SELECT COUNT(*) FROM training_rows t
 WHERE t.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = t.key)
"""
Q["tr_no_outcome"] = """
SELECT COUNT(*) FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind=t.kind AND o.key=t.key)
"""
Q["out_no_tr"] = """
SELECT o.kind, COUNT(*) c FROM outcomes o
 WHERE NOT EXISTS (SELECT 1 FROM training_rows t WHERE t.kind=o.kind AND t.key=o.key)
 GROUP BY o.kind
"""
Q["out_kinds"] = "SELECT kind, status, COUNT(*) c FROM outcomes GROUP BY kind, status"

# --- 5. outcomes signal key -> signal_events ---
Q["out_signal_key_no_event"] = """
SELECT COUNT(*) FROM outcomes o
 WHERE o.kind='signal'
   AND NOT EXISTS (SELECT 1 FROM signal_events e WHERE e.id = o.key)
"""

# --- 6. signal_events -> watch_windows / watchlist ---
Q["sig_tokens_distinct"] = "SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM signal_events)"
Q["sig_token_not_in_watchlist"] = """
SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM signal_events) e
 WHERE NOT EXISTS (SELECT 1 FROM watchlist l
    WHERE l.token_address = e.token_address AND l.network_id = e.network_id)
"""
Q["sig_token_not_in_static"] = """
SELECT COUNT(*) FROM (SELECT DISTINCT token_address, network_id FROM signal_events) e
 WHERE NOT EXISTS (SELECT 1 FROM token_static s
    WHERE s.token_address = e.token_address AND s.network_id = e.network_id)
"""

# --- 7. training_rows -> token_class ---
Q["tr_no_class"] = """
SELECT COUNT(*) FROM training_rows t
 WHERE NOT EXISTS (SELECT 1 FROM token_class c
    WHERE c.token_address = t.token_address
      AND CAST(c.network_id AS TEXT) = CAST(t.network_id AS TEXT))
"""
Q["tr_null_asset_class"] = "SELECT COUNT(*) FROM training_rows WHERE asset_class IS NULL"

for name, sql in Q.items():
    try:
        rows = con.execute(sql).fetchall()
        if len(rows) == 1 and len(rows[0]) == 1:
            print(f"{name} = {rows[0][0]}")
        else:
            print(f"{name} = {[tuple(r) for r in rows]}")
    except Exception as e:
        print(f"{name} = ERR {e}")
