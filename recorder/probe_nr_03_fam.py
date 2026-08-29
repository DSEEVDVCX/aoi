import pickle, collections, features

FC = list(features.FEATURE_COLUMNS)
LC = list(features.LABEL_COLUMNS)


def seg(a, b):
    i = FC.index(a)
    j = FC.index(b)
    assert i <= j, (a, b)
    return FC[i:j + 1]


FAM = {
    "trigger": seg("signal_type", "dow"),
    "static": seg("token_age_h", "has_banner"),
    "social": seg("thesis_counted", "social_authors_delta_1h"),
    "price_path": seg("ret_1h_before", "vol_surge_1h"),
    "market": seg("liquidity", "tick_rich_age_min"),
    "chain_own": seg("chain_top10_pct", "platform_dev_holding"),
    "onchain_conc": seg("onchain_top1_pct", "onchain_holders_delta_5m"),
    "onchain_auth": seg("onchain_has_mint_authority", "onchain_auth_age_min"),
    "onchain_contract": seg("onchain_code_size", "onchain_contract_age_min"),
    "flow": seg("flow_age_min", "flow_is_low_fees"),
    "macro": seg("sol_ret_4h", "eth_ret_24h"),
    "density": seg("prior_signals_token", "global_signals_1h"),
    "labels": LC,
}
covered = [c for f in FAM.values() for c in f]
missing = [c for c in FC if c not in covered]
print("uncovered feature cols:", missing)
print("family sizes:", {k: len(v) for k, v in FAM.items()})
dup = [c for c, n in collections.Counter(covered).items() if n > 1]
print("dupes:", dup)
with open("probe_nr_fam.pkl", "wb") as f:
    pickle.dump(FAM, f)
