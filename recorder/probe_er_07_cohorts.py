"""Signal-type shapes, top-trader network gap, barren cohort, strict usability."""
import os, sqlite3
import config
from probe_er_fam import FAMILIES, WITNESS, POP, empty_expr, n_empty_expr

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

print("=== signal_type shapes in the population ===")
q = f"""SELECT signal_type, COUNT(*) n,
   SUM(CASE WHEN minutes IS NULL THEN 1 ELSE 0 END) nul_minutes,
   SUM(CASE WHEN total_volume IS NULL THEN 1 ELSE 0 END) nul_totvol,
   SUM(CASE WHEN unique_traders IS NULL THEN 1 ELSE 0 END) nul_uniqtr,
   SUM(CASE WHEN size_usd IS NULL THEN 1 ELSE 0 END) nul_size,
   SUM(CASE WHEN are_top_traders IS NULL THEN 1 ELSE 0 END) nul_att,
   SUM(CASE WHEN buyers_best_rank IS NULL THEN 1 ELSE 0 END) nul_rank
  FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print("  ", dict(r))
print()

print("=== top_trader_periods: NULL witness by network AND by entry day >= 2026-08-11 ===")
q = f"""SELECT network_id, COUNT(*) n,
   SUM(CASE WHEN top_trader_periods_matched IS NULL THEN 1 ELSE 0 END) nul
  FROM training_rows WHERE {POP} AND entry_ts >= strftime('%s','2026-08-11')
  GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    print(f"  net={r['network_id']:12s} n={r['n']:6d} witness_NULL={r['nul']:6d} ({100.0*r['nul']/r['n']:.1f}%)")
print()

print("=== chain_ownership / flow / onchain_conc witness NULL by network, entry >= 2026-08-14 ===")
q = f"""SELECT network_id, COUNT(*) n,
   SUM(CASE WHEN holders_age_min IS NULL THEN 1 ELSE 0 END) nul_chain,
   SUM(CASE WHEN flow_age_min IS NULL THEN 1 ELSE 0 END) nul_flow,
   SUM(CASE WHEN onchain_age_min IS NULL THEN 1 ELSE 0 END) nul_onc,
   SUM(CASE WHEN onchain_auth_age_min IS NULL THEN 1 ELSE 0 END) nul_auth,
   SUM(CASE WHEN tick_age_min IS NULL THEN 1 ELSE 0 END) nul_tick,
   SUM(CASE WHEN decimals IS NULL THEN 1 ELSE 0 END) nul_static
  FROM training_rows WHERE {POP} AND entry_ts >= strftime('%s','2026-08-14')
  GROUP BY 1 ORDER BY 2 DESC"""
for r in con.execute(q):
    d = dict(r)
    n = d.pop("n"); net = d.pop("network_id")
    print(f"  net={net:12s} n={n:6d} " + " ".join(f"{k[4:]}={v}({100.0*v/n:.0f}%)" for k, v in d.items()))
print()

# ---------- barren cohort ----------
core8 = ["top_trader_periods", "token_static", "market_snapshot", "chain_ownership",
         "onchain_concentration", "onchain_sol_auth", "onchain_evm_contract", "flow"]
cond = " AND ".join(empty_expr(f) for f in core8)
q = f"SELECT COUNT(*) n FROM training_rows WHERE {POP} AND {cond}"
print("=== BARREN cohort: all 8 of", core8, "entirely NULL ===")
print("  count:", con.execute(q).fetchone()["n"])
q = f"""SELECT date(entry_ts,'unixepoch') d, network_id, COUNT(*) n
   FROM training_rows WHERE {POP} AND {cond} GROUP BY 1,2 ORDER BY 1,2"""
for r in con.execute(q):
    print(f"   {r['d']}  net={r['network_id']:12s} {r['n']}")
print()
q = f"""SELECT key, token_address, network_id, entry_ts, built_at,
   date(entry_ts,'unixepoch') d FROM training_rows WHERE {POP} AND {cond}
   ORDER BY entry_ts LIMIT 8"""
for r in con.execute(q):
    print("   sample:", dict(r))
print()

# ---------- barren: do these tokens have ANY market_ticks / token_static row, ever? ----------
print("=== barren-cohort tokens: any source row at all (any time)? ===")
q = f"""SELECT COUNT(*) rows_total,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM market_ticks m WHERE m.token_address=t.token_address) THEN 1 ELSE 0 END) has_tick_ever,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM market_ticks m WHERE m.token_address=t.token_address
        AND CAST(strftime('%s',m.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) has_tick_before,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM token_static s WHERE s.token_address=t.token_address) THEN 1 ELSE 0 END) has_static_ever,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM token_static s WHERE s.token_address=t.token_address
        AND CAST(strftime('%s',s.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) has_static_before,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM watchlist w WHERE w.token_address=t.token_address) THEN 1 ELSE 0 END) in_watchlist
  FROM training_rows t WHERE {POP} AND {cond}"""
print("  ", dict(con.execute(q).fetchone()))
print()

# ---------- same question for ALL market_snapshot-empty rows ----------
print("=== market_snapshot-empty rows (%s): source availability ===" % "tick_age_min IS NULL")
q = f"""SELECT COUNT(*) rows_total,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM market_ticks m WHERE m.token_address=t.token_address) THEN 1 ELSE 0 END) has_tick_ever,
  SUM(CASE WHEN EXISTS(SELECT 1 FROM market_ticks m WHERE m.token_address=t.token_address
        AND CAST(strftime('%s',m.recorded_at) AS INTEGER) <= t.entry_ts) THEN 1 ELSE 0 END) has_tick_before
  FROM training_rows t WHERE {POP} AND tick_age_min IS NULL"""
print("  ", dict(con.execute(q).fetchone()))
print()

# ---------- STRICT usability ----------
print("=== STRICT usability ladder ===")
tests = {
    "A no empty family at all": n_empty_expr() + " = 0",
    "B only onchain_evm_contract empty": n_empty_expr() + " = 1 AND " + empty_expr("onchain_evm_contract"),
    "C <=1 empty (i.e. A or B)": n_empty_expr() + " <= 1",
    "D no empty family outside {onchain_evm_contract, onchain_sol_auth, onchain_concentration}":
        " AND ".join("NOT " + empty_expr(f) for f in FAMILIES
                     if f not in ("onchain_evm_contract", "onchain_sol_auth", "onchain_concentration")),
    "E core present: trigger+static+social+price+market+labels+macro+density":
        " AND ".join("NOT " + empty_expr(f) for f in
                     ("trigger_event", "token_static", "social_thesis", "price_path",
                      "market_snapshot", "labels", "macro_regime", "signal_density")),
    "F E + chain_ownership + flow":
        " AND ".join("NOT " + empty_expr(f) for f in
                     ("trigger_event", "token_static", "social_thesis", "price_path",
                      "market_snapshot", "labels", "macro_regime", "signal_density",
                      "chain_ownership", "flow")),
    "G F + onchain_concentration":
        " AND ".join("NOT " + empty_expr(f) for f in
                     ("trigger_event", "token_static", "social_thesis", "price_path",
                      "market_snapshot", "labels", "macro_regime", "signal_density",
                      "chain_ownership", "flow", "onchain_concentration")),
}
Ntot = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP}").fetchone()["n"]
print("  population:", Ntot)
for name, c in tests.items():
    n = con.execute(f"SELECT COUNT(*) n FROM training_rows WHERE {POP} AND ({c})").fetchone()["n"]
    print(f"  {name:78s} {n:6d}  {100.0*n/Ntot:5.1f}%")
