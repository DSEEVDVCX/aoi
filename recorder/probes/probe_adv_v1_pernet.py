"""ADVERSARIAL RE-MEASURE: per-network fill of EVERY family the claim names,
plus the comparison columns the claim omitted (mint_authority / code_size on
the *other* networks), plus entry_ts ranges. Independent phrasing: CASE/COUNT
not SUM(IS NOT NULL), and I read the version instead of hardcoding 12."""
import os, sqlite3
import config

uri = f"file:{config.DB_PATH.replace(os.sep, '/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=300)
con.row_factory = sqlite3.Row

fv = con.execute(
    "SELECT value FROM meta WHERE key='current_feature_version'").fetchone()
print("current_feature_version =", None if fv is None else fv["value"])

print("\n-- feature_version histogram over live signal meme ok independent --")
for r in con.execute("""
    SELECT feature_version, COUNT(*) n FROM training_rows
     WHERE kind='signal' AND is_live=1 AND asset_class='meme'
       AND status='ok' AND is_independent=1
     GROUP BY 1 ORDER BY 1"""):
    print(f"   fv={r['feature_version']}  n={r['n']}")

POP = """kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
         AND is_independent=1
         AND feature_version = CAST(COALESCE(
             (SELECT value FROM meta WHERE key='current_feature_version'),'0')
             AS INTEGER)"""

tot = con.execute(f"SELECT COUNT(*) c FROM training_rows WHERE {POP}").fetchone()["c"]
print(f"\nmodel population total = {tot}")

COLS = [
    ("flow_buy_volume_5m", "token_flow"),
    ("flow_age_min", "token_flow"),
    ("chain_holder_count", "token_holders/details"),
    ("platform_holders", "token_holders/hodlers"),
    ("onchain_top10_pct", "chain_concentration"),
    ("onchain_holder_count", "chain_concentration"),
    ("onchain_has_mint_authority", "chain_authority(SOL only)"),
    ("onchain_dev_holding_pct", "chain_authority(SOL only)"),
    ("onchain_code_size", "evm_contract(Base only)"),
    ("onchain_has_mint_fn", "evm_contract(Base only)"),
]

sel = ", ".join(
    f"COUNT({c}) AS \"{c}\"" for c, _ in COLS)
q = f"""SELECT network_id, COUNT(*) n, {sel},
        MIN(entry_ts) e_min, MAX(entry_ts) e_max,
        MIN(built_at) b_min, MAX(built_at) b_max
        FROM training_rows WHERE {POP} GROUP BY 1 ORDER BY n DESC"""
rows = [dict(r) for r in con.execute(q)]

import datetime as dt
def iso(ts):
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"

hdr = f"{'network':>12} {'n':>6} " + " ".join(f"{c[:14]:>15}" for c, _ in COLS)
print("\n" + hdr)
for r in rows:
    line = f"{str(r['network_id']):>12} {r['n']:>6} " + " ".join(
        f"{r[c]:>15}" for c, _ in COLS)
    print(line)

print("\n-- same rows: entry_ts and built_at spans --")
for r in rows:
    print(f"   net={str(r['network_id']):>12} n={r['n']:>6} "
          f"entry {iso(r['e_min'])} -> {iso(r['e_max'])} | "
          f"built {str(r['b_min'])[:19]} -> {str(r['b_max'])[:19]}")

print("\n-- % fill per network for the 3 network-agnostic families --")
for r in rows:
    n = r["n"]
    def pct(c):
        return f"{100.0*r[c]/n:5.1f}%"
    print(f"   net={str(r['network_id']):>12} n={n:>6} "
          f"flow={pct('flow_buy_volume_5m')} chain_hc={pct('chain_holder_count')} "
          f"plat_h={pct('platform_holders')} cc_top10={pct('onchain_top10_pct')}")
