import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from features import epoch_of

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")

print("=== A does the anti-leakage guard (token_static.recorded_at <= entry_ts) explain the NULLs? ===")
for r in con.execute(f"""
WITH p AS (SELECT token_address ta, network_id ni, entry_ts, token_age_h,
                  decimals, launchpad_name, socials_count, name_len
             FROM training_rows WHERE {POP})
SELECT CASE WHEN p.token_age_h IS NULL THEN 'age NULL' ELSE 'age present' END grp,
       CASE WHEN s.token_address IS NULL THEN 'no static row'
            WHEN CAST(strftime('%s', s.recorded_at) AS INTEGER) > p.entry_ts
                 THEN 'static recorded AFTER t0 (guard excludes it)'
            ELSE 'static recorded at/before t0 (visible)' END guard,
       COUNT(*) n,
       SUM(p.decimals IS NULL) dec_null,
       SUM(p.socials_count IS NULL) soc_null,
       SUM(p.name_len IS NULL) namelen_null
  FROM p LEFT JOIN token_static s ON s.token_address=p.ta AND s.network_id=p.ni
 GROUP BY 1,2 ORDER BY 1, n DESC"""):
    print(f"    {r['grp']:12} | {r['guard']:42} | n={r['n']:6d} | decimals NULL={r['dec_null']:5d} | socials NULL={r['soc_null']:5d} | name_len NULL={r['namelen_null']:5d}")
print()

print("=== B the 29 rows of the 12 created_at-NULL coins: is the REST of the static family present? ===")
for r in con.execute(f"""
WITH bad AS (SELECT token_address, network_id FROM token_static WHERE token_created_at IS NULL)
SELECT COUNT(*) n, SUM(t.token_age_h IS NULL) age_null,
       SUM(t.decimals IS NULL) dec_null, SUM(t.socials_count IS NULL) soc_null,
       SUM(t.name_len IS NULL) namelen_null, SUM(t.is_scam IS NULL) scam_null
  FROM training_rows t JOIN bad b
    ON b.token_address=t.token_address AND b.network_id=t.network_id
 WHERE {POP}"""):
    print(f"    n={r['n']} age_null={r['age_null']} decimals_null={r['dec_null']} socials_null={r['soc_null']} name_len_null={r['namelen_null']} is_scam_null={r['scam_null']}")
print()

print("=== C Liluni + HOODER (the postdating coins with rows): static family present, age suppressed? ===")
for r in con.execute(f"""
SELECT s.symbol, COUNT(*) n, SUM(t.token_age_h IS NULL) age_null,
       SUM(t.decimals IS NULL) dec_null, SUM(t.socials_count IS NULL) soc_null,
       ROUND(MIN(t.token_age_h),2) mn_age, ROUND(MAX(t.token_age_h),2) mx_age
  FROM training_rows t JOIN token_static s
    ON s.token_address=t.token_address AND s.network_id=t.network_id
 WHERE {POP} AND s.symbol IN ('Liluni','HOODER') AND s.network_id='4663'
 GROUP BY 1"""):
    print(f"    {r['symbol']}: n={r['n']} age_null={r['age_null']} decimals_null={r['dec_null']} socials_null={r['soc_null']} age range [{r['mn_age']},{r['mx_age']}]")
print()

print("=== D how far after t0 was the static row recorded, for the 1243 ===")
rows = con.execute(f"""
SELECT t.entry_ts, s.recorded_at, s.token_created_at tca
  FROM training_rows t JOIN token_static s
    ON s.token_address=t.token_address AND s.network_id=t.network_id
 WHERE {POP} AND t.token_age_h IS NULL AND s.token_created_at IS NOT NULL""").fetchall()
after = [0, 0]
gaps = []
for r in rows:
    rec = epoch_of(r["recorded_at"])
    if rec is None:
        from datetime import datetime
        rec = int(datetime.fromisoformat(r["recorded_at"]).timestamp())
    if rec > r["entry_ts"]:
        after[0] += 1
        gaps.append(rec - r["entry_ts"])
    else:
        after[1] += 1
print(f"    static recorded AFTER t0: {after[0]}   at/before t0: {after[1]}   (total {len(rows)})")
if gaps:
    gaps.sort()
    print(f"    gap seconds: min={gaps[0]} p50={gaps[len(gaps)//2]} p90={gaps[int(len(gaps)*0.9)]} max={gaps[-1]}")
con.close()
