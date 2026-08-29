import os, sqlite3, json, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = "CAST(COALESCE((SELECT value FROM meta WHERE key='current_feature_version'),'0') AS INTEGER)"
POP = f"""FROM training_rows
 WHERE kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok'
   AND is_independent=1 AND feature_version = {FV}"""

zero = con.execute(f"SELECT token_address a, network_id n, COUNT(*) rows_n {POP} AND socials_count=0 GROUP BY a,n").fetchall()
pos  = con.execute(f"SELECT token_address a, network_id n, COUNT(*) rows_n {POP} AND socials_count>0 GROUP BY a,n").fetchall()
nul  = con.execute(f"SELECT token_address a, network_id n, COUNT(*) rows_n {POP} AND socials_count IS NULL GROUP BY a,n").fetchall()
print("cohorts: sc=0", len(zero), "sc>0", len(pos), "sc NULL", len(nul))

SOC_KEYS = ("twitter", "telegram", "website", "discord")
errors = {"n": 0, "last": ""}

def probe_token(addr, net, per_token=6):
    out = {"ticks": 0, "present": 0, "missing": 0, "null_block": 0, "any_val": 0,
           "tw_str": 0, "sources": set(), "static": None, "static_tw_str": None,
           "decoded": 0}
    tot = con.execute("SELECT COUNT(*) c FROM market_ticks WHERE token_address=? AND network_id=?",
                      (addr, net)).fetchone()["c"]
    out["ticks"] = tot
    if tot:
        rows = con.execute(
            """SELECT raw_json, source, recorded_at FROM market_ticks
                WHERE token_address=? AND network_id=? ORDER BY recorded_at""",
            (addr, net)).fetchall()
        step = max(1, len(rows) // per_token)
        picked = rows[::step][:per_token]
        if rows and rows[-1] not in picked:
            picked = list(picked) + [rows[-1]]
        for r in picked:
            try:
                item = dbmod.decode_raw(r["raw_json"])
            except Exception as e:
                errors["n"] += 1; errors["last"] = repr(e)[:120]; continue
            out["decoded"] += 1
            out["sources"].add(r["source"])
            tok = item.get("token") if isinstance(item.get("token"), dict) else {}
            if "socialLinks" in tok:
                sl = tok.get("socialLinks")
                if sl is None:
                    out["null_block"] += 1
                else:
                    out["present"] += 1
                    if isinstance(sl, dict) and any(sl.get(k) for k in SOC_KEYS):
                        out["any_val"] += 1
            else:
                out["missing"] += 1
            if "twitter" in json.dumps(item, ensure_ascii=False).lower():
                out["tw_str"] += 1
    s = con.execute("SELECT raw_json FROM token_static WHERE token_address=? AND network_id=?",
                    (addr, net)).fetchone()
    if s:
        try:
            item = dbmod.decode_raw(s["raw_json"])
            tok = item.get("token") if isinstance(item.get("token"), dict) else {}
            if "socialLinks" not in tok:
                out["static"] = "missing"
            elif tok.get("socialLinks") is None:
                out["static"] = "null"
            elif any((tok.get("socialLinks") or {}).get(k) for k in SOC_KEYS):
                out["static"] = "present_with_val"
            else:
                out["static"] = "present_all_null"
            out["static_tw_str"] = "twitter" in json.dumps(item, ensure_ascii=False).lower()
        except Exception as e:
            errors["n"] += 1; errors["last"] = repr(e)[:120]
            out["static"] = "decode_fail"
    else:
        out["static"] = "no_row"
    return out

def run(cohort, label):
    agg = {"tokens":0, "envs_decoded":0, "tok_all_missing":0, "tok_all_present":0,
           "tok_mixed":0, "tok_any_val":0, "tok_tw_str":0, "tok_no_ticks":0,
           "env_present":0, "env_missing":0, "env_null_block":0}
    static = {}
    srcs = {}
    for row in cohort:
        f = probe_token(row["a"], row["n"])
        agg["tokens"] += 1
        agg["envs_decoded"] += f["decoded"]
        agg["env_present"] += f["present"]
        agg["env_missing"] += f["missing"]
        agg["env_null_block"] += f["null_block"]
        if f["decoded"] == 0:
            agg["tok_no_ticks"] += 1
        elif (f["present"] or f["null_block"]) and f["missing"]:
            agg["tok_mixed"] += 1
        elif f["missing"]:
            agg["tok_all_missing"] += 1
        else:
            agg["tok_all_present"] += 1
        if f["any_val"]:
            agg["tok_any_val"] += 1
        if f["tw_str"]:
            agg["tok_tw_str"] += 1
        static[f["static"]] = static.get(f["static"], 0) + 1
        for s in f["sources"]:
            srcs[s] = srcs.get(s, 0) + 1
    print(f"\n=== {label} (tokens={agg['tokens']}) ===")
    for k, v in agg.items():
        print("  ", k, "=", v)
    print("   token_static socialLinks state:", static)
    print("   tick sources:", srcs)

run(zero, "cohort socials_count=0")
run(pos,  "cohort socials_count>0 (control)")
run(nul,  "cohort socials_count IS NULL")
print("\ndecode errors:", errors)
con.close()
