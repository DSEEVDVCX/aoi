import features
raw = ["flow_buy_volume_5m","flow_sell_volume_5m","flow_buy_count_5m","flow_sell_count_5m",
       "flow_unique_buys_5m","flow_unique_sells_5m","flow_net_volume_5m","flow_age_min",
       "social_holder_authors","social_thesis_authors","platform_holders"]
fc = set(features.FEATURE_COLUMNS)
rc = set(features.ROW_COLUMNS)
for c in raw:
    print(f"{c:28s} FEATURE_COLUMNS={c in fc}  ROW_COLUMNS={c in rc}")
print()
print("len(FEATURE_COLUMNS) =", len(features.FEATURE_COLUMNS))
print("len(ROW_COLUMNS)     =", len(features.ROW_COLUMNS))
