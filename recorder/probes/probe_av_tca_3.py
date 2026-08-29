import os, sys, io, sqlite3, config
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from features import epoch_of
from datetime import datetime, timezone

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

FV = 12
POP = ("kind='signal' AND is_live=1 AND asset_class='meme' AND status='ok' "
       f"AND is_independent=1 AND feature_version={FV}")

# ---- A) postdates-own-first-window, computed in PYTHON with epoch_of ----
print("=== A postdates own first window -- Python epoch_of, no CAST ===")
rows = con.execute("""
SELECT s.token_address a, s.network_id n, s.symbol sym, s.token_created_at tca,
       MIN(w.first_seen_at) fw_lexmin, COUNT(*) nw
  FROM token_static s JOIN watch_windows w
    ON w.token_address = s.token_address AND w.network_id = s.network_id
 WHERE s.token_created_at IS NOT NULL
 GROUP BY 1,2,3,4""").fetchall()
print("  coins in token_static that were ever watched, created_at present:", len(rows))

# true chronological min of first_seen_at per coin, in Python (no lexicographic trust)
firsts = {}
for r in con.execute("SELECT token_address a, network_id n, first_seen_at f FROM watch_windows"):
    e = epoch_of(r["f"])
    if e is None:
        e = int(datetime.fromisoformat(r["f"]).timestamp())
    k = (r["a"], r["n"])
    if k not in firsts or e < firsts[k]:
        firsts[k] = e

bad = []
for r in rows:
    created = epoch_of(r["tca"])
    fw = firsts[(r["a"], r["n"])]
    if created is not None and created > fw:
        bad.append((r["sym"], r["n"], r["tca"], created, fw, round((created - fw) / 3600.0, 2), r["nw"]))
bad.sort(key=lambda x: -x[5])
print(f"  coins whose created_at > true earliest first_seen_at: {len(bad)}")
for b in bad:
    print(f"    sym={b[0]} net={b[1]} created_raw={b[2]} created_epoch={b[3]} first_window_epoch={b[4]} hours_after={b[5]} windows={b[6]}")

# does lexicographic MIN differ from true chronological min anywhere?
diff = 0
for r in con.execute("SELECT token_address a, network_id n, MIN(first_seen_at) m FROM watch_windows GROUP BY 1,2"):
    if epoch_of(r["m"]) is None:
        t = int(datetime.fromisoformat(r["m"]).timestamp())
    else:
        t = epoch_of(r["m"])
    if t != firsts[(r["a"], r["n"])]:
        diff += 1
print("  coins where lexicographic MIN(first_seen_at) != chronological min:", diff)
print()

# ---- B) do those coins produce model rows, and with what age? ----
print("=== B model-population rows for the postdating coins ===")
for b in bad:
    sym = b[0]
    r = con.execute(f"""
      SELECT COUNT(*) n, SUM(token_age_h IS NULL) age_null,
             ROUND(MIN(token_age_h),2) mn, ROUND(MAX(token_age_h),2) mx
        FROM training_rows t
        JOIN token_static s ON s.token_address=t.token_address AND s.network_id=t.network_id
       WHERE s.symbol=? AND s.network_id=? AND {POP}""", (sym, b[1])).fetchone()
    r2 = con.execute("""
      SELECT COUNT(*) n FROM training_rows t
        JOIN token_static s ON s.token_address=t.token_address AND s.network_id=t.network_id
       WHERE s.symbol=? AND s.network_id=?""", (sym, b[1])).fetchone()
    print(f"    {sym}/net{b[1]}: all training_rows={r2['n']}  model rows={r['n']}  age_null={r['age_null']}  age range=[{r['mn']},{r['mx']}]")
print()

# ---- C) absolute future-dating: created_at > now ----
now = int(datetime.now(timezone.utc).timestamp())
cnt = 0
mx = None
for r in con.execute("SELECT token_created_at v, symbol s, network_id n FROM token_static WHERE token_created_at IS NOT NULL"):
    e = epoch_of(r["v"])
    if e is not None and e > now:
        cnt += 1
        if mx is None or e > mx[0]:
            mx = (e, r["s"], r["n"])
print(f"=== C created_at strictly in the future (epoch_of, now={now}) === count={cnt} max={mx}")
print()

# ---- D) cause decomposition of the 1294 NULL token_age_h in the model population ----
print("=== D cause decomposition of token_age_h NULL in the model population ===")
for r in con.execute(f"""
WITH p AS (SELECT token_address ta, network_id ni, entry_ts, token_age_h FROM training_rows WHERE {POP})
SELECT CASE WHEN s.token_address IS NULL THEN 'A: no token_static row at all'
            WHEN s.token_created_at IS NULL THEN 'B: token_static present, created_at NULL'
            ELSE 'C: created_at present -> age was negative and suppressed' END cause,
       COUNT(*) rows_
  FROM p LEFT JOIN token_static s ON s.token_address=p.ta AND s.network_id=p.ni
 WHERE p.token_age_h IS NULL GROUP BY 1 ORDER BY rows_ DESC"""):
    print(f"    {r['cause']}: {r['rows_']}")
print()

print("=== D2 same but case-insensitive join (EVM mixed-case trap) ===")
for r in con.execute(f"""
WITH p AS (SELECT token_address ta, network_id ni, token_age_h FROM training_rows WHERE {POP})
SELECT CASE WHEN s.token_address IS NULL THEN 'A: no token_static row (ci)'
            WHEN s.token_created_at IS NULL THEN 'B: created_at NULL (ci)'
            ELSE 'C: created_at present (ci)' END cause, COUNT(*) rows_
  FROM p LEFT JOIN token_static s
       ON lower(s.token_address)=lower(p.ta) AND s.network_id=p.ni
 WHERE p.token_age_h IS NULL GROUP BY 1 ORDER BY rows_ DESC"""):
    print(f"    {r['cause']}: {r['rows_']}")
con.close()
