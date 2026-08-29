import os, sqlite3, config

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

COLS = ["onchain_code_size","onchain_function_count","onchain_is_proxy",
        "onchain_owner_renounced","onchain_has_mint_fn","onchain_has_pause_fn",
        "onchain_has_blacklist_fn","onchain_has_fee_setter",
        "onchain_has_limit_setter","onchain_has_trading_switch",
        "onchain_contract_age_min"]

def q(label, sql, args=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        rows = con.execute(sql, args).fetchall()
        for r in rows[:40]:
            print("   ", dict(r))
        if len(rows) > 40:
            print("    ... %d more rows" % (len(rows) - 40))
    except Exception as e:
        print("    FAILED:", e)
    print()

# --- 0. live table columns vs features.ROW_COLUMNS -------------------------
import features
live = [r["name"] for r in con.execute("PRAGMA table_info(training_rows)")]
extra = [c for c in live if c not in features.ROW_COLUMNS]
missing = [c for c in features.ROW_COLUMNS if c not in live]
print("PRAGMA live cols:", len(live), "ROW_COLUMNS:", len(features.ROW_COLUMNS))
print("live-only (migration ghosts):", extra)
print("ROW_COLUMNS-only (absent live):", missing)
print("11 contract cols present live:", [c for c in COLS if c in live])
print()

# --- 1. my own NULL measurement, whole table, one pass ---------------------
sel = ", ".join(f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END) nn_{c}" for c in COLS)
q("1. NON-NULL counts for all 11 cols, whole training_rows",
  f"SELECT COUNT(*) total, {sel} FROM training_rows")

# --- 2. model population --------------------------------------------------
FV = ("feature_version = CAST(COALESCE((SELECT value FROM meta "
      "WHERE key='current_feature_version'),'0') AS INTEGER)")
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND " + FV)
q("2. model population size + non-null contract cols",
  f"SELECT COUNT(*) pop, {sel} FROM training_rows WHERE {POP}")

# --- 3. network mix, all rows and model population ------------------------
q("3a. training_rows by network_id",
  "SELECT network_id, COUNT(*) n, MIN(entry_ts) min_ts, MAX(entry_ts) max_ts, "
  "datetime(MIN(entry_ts),'unixepoch') min_iso, datetime(MAX(entry_ts),'unixepoch') max_iso "
  "FROM training_rows GROUP BY 1 ORDER BY n DESC")
q("3b. model population by network_id",
  f"SELECT network_id, COUNT(*) n, datetime(MAX(entry_ts),'unixepoch') max_iso "
  f"FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY n DESC")
q("3c. kind x feature_version totals",
  "SELECT kind, feature_version, COUNT(*) n FROM training_rows GROUP BY 1,2 ORDER BY 3 DESC")
q("3d. current_feature_version meta", "SELECT key, value FROM meta WHERE key LIKE '%feature_version%'")

# --- 4. the source table, my own aggregation ------------------------------
q("4a. evm_contract shape",
  "SELECT network_id, COUNT(*) n, COUNT(DISTINCT token_address) toks, "
  "MIN(recorded_at) first, MAX(recorded_at) last FROM evm_contract GROUP BY 1")
q("4b. evm_contract non-null payload (is the collector actually producing values?)",
  "SELECT SUM(code_size IS NOT NULL) cs, SUM(function_count IS NOT NULL) fc, "
  "SUM(is_proxy IS NOT NULL) pxy, SUM(is_ownership_renounced IS NOT NULL) own, "
  "SUM(has_mint IS NOT NULL) mint, COUNT(*) n FROM evm_contract")
q("4c. evm_contract mixed case addresses?",
  "SELECT SUM(token_address <> lower(token_address)) upper_any, COUNT(*) n FROM evm_contract")

# --- 5. Base rows in training_rows: is there ANY row at/after the collector?
q("5a. earliest contract snapshot as epoch",
  "SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) first_e, "
  "MAX(CAST(strftime('%s',recorded_at) AS INTEGER)) last_e FROM evm_contract")
q("5b. Base training rows straddling the collector start (my own count, no EXISTS)",
  "SELECT COUNT(*) base_rows, "
  "SUM(entry_ts >= (SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) FROM evm_contract)) "
  "at_or_after_collector FROM training_rows WHERE network_id='8453'")
q("5c. ANY training row (any network) with entry_ts >= collector start",
  "SELECT COUNT(*) n, COUNT(DISTINCT network_id) nets FROM training_rows "
  "WHERE entry_ts >= (SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) FROM evm_contract)")
q("5d. networks of rows built at/after collector start",
  "SELECT network_id, COUNT(*) n FROM training_rows "
  "WHERE entry_ts >= (SELECT MIN(CAST(strftime('%s',recorded_at) AS INTEGER)) FROM evm_contract) "
  "GROUP BY 1 ORDER BY n DESC")

# --- 6. token overlap (case-insensitive, my own join) ---------------------
q("6a. distinct Base tokens in training_rows vs in evm_contract (lower())",
  "SELECT (SELECT COUNT(DISTINCT lower(token_address)) FROM training_rows WHERE network_id='8453') tr_base_toks, "
  "(SELECT COUNT(DISTINCT lower(token_address)) FROM evm_contract) ec_toks, "
  "(SELECT COUNT(*) FROM (SELECT DISTINCT lower(token_address) a FROM training_rows WHERE network_id='8453' "
  "INTERSECT SELECT DISTINCT lower(token_address) FROM evm_contract)) shared")
q("6b. rows joinable ignoring time (case-insensitive)",
  "SELECT COUNT(*) n FROM training_rows tr JOIN (SELECT DISTINCT lower(token_address) a, network_id nid "
  "FROM evm_contract) c ON lower(tr.token_address)=c.a AND tr.network_id=c.nid")

# --- 7. is Base still producing signals now? (the real defect test) -------
q("7a. signal_events by network, last 10 days",
  "SELECT network_id, COUNT(*) n, MAX(created_at) last FROM signal_events "
  "WHERE created_at >= '2026-08-12' GROUP BY 1 ORDER BY n DESC")
q("7b. signal_events Base overall span",
  "SELECT COUNT(*) n, MIN(created_at) first, MAX(created_at) last FROM signal_events "
  "WHERE network_id='8453'")
q("7c. most recent training rows overall (build is current?)",
  "SELECT datetime(MAX(entry_ts),'unixepoch') newest_entry FROM training_rows")

# --- 8. Solana counterpart for contrast (documented NULL-by-design) -------
q("8. onchain_authority family coverage for contrast",
  "SELECT COUNT(*) n, SUM(onchain_has_mint_authority IS NOT NULL) auth_nn, "
  "SUM(onchain_holder_count IS NOT NULL) holder_nn FROM training_rows")

con.close()
