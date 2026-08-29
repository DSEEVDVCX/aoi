import os, sqlite3, json, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version = {FV}"""

# cohorts by measured feature value
zero = con.execute(f"SELECT token_address a, network_id n, COUNT(*) rows_n {POP} AND socials_count=0 GROUP BY a,n").fetchall()
pos  = con.execute(f"SELECT token_address a, network_id n, COUNT(*) rows_n {POP} AND socials_count>0 GROUP BY a,n").fetchall()
print("cohort sizes: sc=0 tokens", len(zero), " sc>0 tokens", len(pos))

def probe_token(addr, net, per_token=6):
    """Scan up to per_token market_ticks envelopes spread over time + the frozen
    token_static envelope. Return presence facts."""
    out = {"ticks": 0, "block_present": 0, "block_missing": 0, "any_social_val": 0,
           "twitter_str_anywhere": 0, "sources": set(), "static_block": None,
           "static_twitter_anywhere": None}
    tot = con.execute("SELECT COUNT(*) c FROM market_ticks WHERE token_address=? AND network_id=?",
                      (addr, net)).fetchone()["c"]
    out["ticks"] = tot
    if tot:
        step = max(1, tot // per_token)
        rows = con.execute(
            """SELECT raw_json, source, recorded_at FROM market_ticks
                WHERE token_address=? AND network_id=? ORDER BY recorded_at""",
            (addr, net)).fetchall()
        picked = rows[::step][:per_token]
        if rows and rows[-1] not in picked:
            picked = list(picked) + [rows[-1]]
        for r in picked:
            try:
                item = json.loads(dbmod.decode_raw(r["raw_json"]))
            except Exception:
                continue
            out["sources"].add(r["source"])
            tok = item.get("token") if isinstance(item.get("token"), dict) else {}
            if "socialLinks" in tok:
                out["block_present"] += 1
                sl = tok.get("socialLinks")
                if isinstance(sl, dict) and any(sl.get(k) for k in ("twitter","telegram","website","discord")):
                    out["any_social_val"] += 1
            else:
                out["block_missing"] += 1
            blob = json.dumps(item).lower()
            if "twitter" in blob:
                out["twitter_str_anywhere"] += 1
    s = con.execute("SELECT raw_json FROM token_static WHERE token_address=? AND network_id=?",
                    (addr, net)).fetchone()
    if s:
        try:
            item = json.loads(dbmod.decode_raw(s["raw_json"]))
            tok = item.get("token") if isinstance(item.get("token"), dict) else {}
            out["static_block"] = ("present" if "socialLinks" in tok else "missing")
            out["static_twitter_anywhere"] = ("twitter" in json.dumps(item).lower())
        except Exception:
            out["static_block"] = "decode_fail"
    return out

def run(cohort, label, limit=None):
    agg = {"tokens":0, "tok_all_missing":0, "tok_all_present":0, "tok_mixed":0,
           "tok_any_social_val":0, "tok_twitter_str":0, "tok_no_ticks":0,
           "env_present":0, "env_missing":0, "static_missing":0, "static_present":0,
           "static_twitter_anywhere":0, "sources":{}}
    sample = cohort if limit is None else cohort[:limit]
    for row in sample:
        f = probe_token(row["a"], row["n"])
        agg["tokens"] += 1
        agg["env_present"] += f["block_present"]
        agg["env_missing"] += f["block_missing"]
        if f["ticks"] == 0:
            agg["tok_no_ticks"] += 1
        elif f["block_present"] and f["block_missing"]:
            agg["tok_mixed"] += 1
        elif f["block_present"]:
            agg["tok_all_present"] += 1
        else:
            agg["tok_all_missing"] += 1
        if f["any_social_val"]:
            agg["tok_any_social_val"] += 1
        if f["twitter_str_anywhere"]:
            agg["tok_twitter_str"] += 1
        if f["static_block"] == "missing":
            agg["static_missing"] += 1
        elif f["static_block"] == "present":
            agg["static_present"] += 1
        if f["static_twitter_anywhere"]:
            agg["static_twitter_anywhere"] += 1
        for s in f["sources"]:
            agg["sources"][s] = agg["sources"].get(s, 0) + 1
    print(f"\n=== {label} (n={agg['tokens']}) ===")
    for k, v in agg.items():
        print("  ", k, "=", v)
    return agg

run(zero, "cohort socials_count=0", limit=None)
run(pos,  "cohort socials_count>0 (control)", limit=None)
con.close()
