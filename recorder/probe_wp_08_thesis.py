import os, sqlite3, config
import db as dbmod

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
def q(sql, args=()): return con.execute(sql, args).fetchall()

POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       "AND is_independent=1 AND feature_version=12")

print("=== A) root cause of thesis_counted=0: token_thesis token coverage ===")
r = q("SELECT COUNT(*) rows, COUNT(DISTINCT token_address||'|'||network_id) toks FROM token_thesis")[0]
print(f"   token_thesis rows={r['rows']} distinct tokens={r['toks']}")
r = q("SELECT COUNT(DISTINCT token_address||'|'||network_id) t FROM token_social")[0]
print(f"   token_social distinct tokens={r['t']}")
r = q(f"SELECT COUNT(DISTINCT token_address||'|'||network_id) t FROM training_rows WHERE {POP}")[0]
print(f"   model-population distinct tokens={r['t']}")
r = q(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP} AND thesis_counted=0
            AND EXISTS (SELECT 1 FROM token_thesis h
                         WHERE h.token_address=t.token_address AND h.network_id=t.network_id)""")[0]
print(f"   model rows with thesis_counted=0 whose token DOES appear in token_thesis: {r['c']}")
r = q(f"""SELECT COUNT(*) c FROM training_rows t WHERE {POP} AND thesis_counted=0
            AND NOT EXISTS (SELECT 1 FROM token_thesis h
                         WHERE h.token_address=t.token_address AND h.network_id=t.network_id)""")[0]
print(f"   model rows with thesis_counted=0 whose token NEVER appears in token_thesis: {r['c']}")

print()
print("=== B) the thesis family, side by side, on the model population ===")
r = q(f"""SELECT COUNT(*) tot,
                 SUM(CASE WHEN thesis_counted=0 THEN 1 ELSE 0 END) tc0,
                 SUM(CASE WHEN thesis_authors_before=0 THEN 1 ELSE 0 END) ta0,
                 SUM(CASE WHEN thesis_1h=0 THEN 1 ELSE 0 END) t1h0,
                 SUM(CASE WHEN thesis_24h=0 THEN 1 ELSE 0 END) t24h0,
                 SUM(CASE WHEN hours_since_last_thesis IS NULL THEN 1 ELSE 0 END) hslN,
                 SUM(CASE WHEN thesis_history_days IS NULL THEN 1 ELSE 0 END) thdN,
                 SUM(CASE WHEN social_thesis_total IS NOT NULL THEN 1 ELSE 0 END) stt,
                 SUM(CASE WHEN thesis_counted_capped=1 THEN 1 ELSE 0 END) cap
            FROM training_rows WHERE {POP}""")[0]
for k in r.keys(): print(f"   {k:26s} {r[k]}")

print()
print("=== C) authorTrade absence: does 'unknown holding' get counted as 'not a holder'? ===")
rows = q("""SELECT raw_json FROM token_social
             WHERE thesis_sampled > 0 ORDER BY recorded_at DESC LIMIT 40""")
tot_a = have_trade = trade_absent = pos = zero = 0
for r in rows:
    try:
        raw = dbmod.decode_raw(r["raw_json"])
    except Exception:
        continue
    ro = raw.get("responseObject") if isinstance(raw, dict) else None
    items = None
    if isinstance(ro, dict):
        for k in ("theses", "items", "data", "thesis"):
            if isinstance(ro.get(k), list):
                items = ro[k]; break
    elif isinstance(ro, list):
        items = ro
    if not items: continue
    for it in items:
        if not isinstance(it, dict) or not it.get("userHandle"): continue
        tot_a += 1
        tr = it.get("authorTrade")
        if not isinstance(tr, dict):
            trade_absent += 1
        else:
            have_trade += 1
            amt = tr.get("humanTokenAmount")
            try: amtf = float(amt)
            except (TypeError, ValueError): amtf = None
            if amtf is None: trade_absent += 1; have_trade -= 1
            elif amtf > 0: pos += 1
            else: zero += 1
print(f"   sampled thesis authors={tot_a}: authorTrade/amount ABSENT={trade_absent}"
      f"  amount>0={pos}  amount==0={zero}")
if tot_a:
    print(f"   -> {100.0*trade_absent/tot_a:.1f}% of authors are silently counted as NON-holders")

print()
print("=== D) numReplies presence in the thesis payload (root cause of social_replies==0) ===")
tot_i = with_rep = rep_gt0 = 0
for r in rows:
    try: raw = dbmod.decode_raw(r["raw_json"])
    except Exception: continue
    ro = raw.get("responseObject") if isinstance(raw, dict) else None
    items = None
    if isinstance(ro, dict):
        for k in ("theses", "items", "data", "thesis"):
            if isinstance(ro.get(k), list): items = ro[k]; break
    elif isinstance(ro, list): items = ro
    if not items: continue
    for it in items:
        if not isinstance(it, dict): continue
        tot_i += 1
        if "numReplies" in it and it["numReplies"] is not None:
            with_rep += 1
            try:
                if float(it["numReplies"]) > 0: rep_gt0 += 1
            except (TypeError, ValueError): pass
print(f"   sampled thesis items={tot_i}  numReplies present={with_rep}  present and >0={rep_gt0}")

print()
print("=== E) flow / market / holders family coverage on the model population ===")
for c in ("flow_age_min","flow_buy_volume_5m","flow_net_volume_1h","liquidity","volume_24h",
          "buy_count_24h","holders","chain_top10_pct","chain_holder_count","platform_holders",
          "platform_penetration","social_thesis_total","token_age_h","bars_count_24h",
          "dist_from_ath","sol_ret_24h"):
    r = q(f"""SELECT SUM(CASE WHEN "{c}" IS NOT NULL THEN 1 ELSE 0 END) nn, COUNT(*) tot
                FROM training_rows WHERE {POP}""")[0]
    print(f"   {c:26s} {r['nn']:>6}/{r['tot']} ({100.0*r['nn']/r['tot']:5.1f}%)")

con.close()
