import os, sqlite3, config, datetime as dt, sys, collections

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a): print(*a); sys.stdout.flush()

P("=== brand-new FIRST-EVER operational (is_control=0) admissions per day ===")
for r in con.execute("""SELECT substr(f,1,10) d, COUNT(*) n FROM (
      SELECT MIN(first_seen_at) f, token_address, network_id
        FROM watch_windows WHERE is_control=0 GROUP BY token_address, network_id)
      GROUP BY 1 ORDER BY 1"""):
    P(f"   {r['d']}  first-ever operational admissions = {r['n']}")

P("\n=== newest first-ever operational admissions ===")
for r in con.execute("""SELECT MIN(first_seen_at) f, token_address, network_id FROM watch_windows
      WHERE is_control=0 GROUP BY token_address, network_id ORDER BY f DESC LIMIT 10"""):
    P("   ", tuple(r))

P("\n=== control admissions per day ===")
for r in con.execute("""SELECT substr(f,1,10) d, COUNT(*) n FROM (
      SELECT MIN(first_seen_at) f, token_address, network_id
        FROM watch_windows WHERE is_control=1 GROUP BY token_address, network_id)
      GROUP BY 1 ORDER BY 1"""):
    P(f"   {r['d']}  first-ever control admissions = {r['n']}")

# why were the last-24h trigger tokens never admitted?
P("\n=== last 24h trigger tokens never in watchlist: age-gate vs other ===")
marks = ",".join("?" for _ in config.TRIGGER_SIGNAL_TYPES)
cut = (NOW - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
wlk = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT token_address, network_id FROM watchlist")}
rows = con.execute(
    f"SELECT token_address, network_id, MIN(recorded_at) first_sig FROM signal_events "
    f"WHERE signal_type IN ({marks}) AND recorded_at >= ? GROUP BY token_address, network_id",
    tuple(config.TRIGGER_SIGNAL_TYPES) + (cut,)).fetchall()
stat = {}
for r in con.execute("SELECT token_address, network_id, token_created_at FROM token_static"):
    stat[(str(r[0]).lower(), str(r[1] or ''))] = r["token_created_at"]
def pp(s):
    if s is None: return None
    try: return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception:
        try: return dt.datetime.fromtimestamp(float(s), dt.timezone.utc)
        except Exception: return None
young = old = unknown = 0
examples = []
for r in rows:
    k = (str(r["token_address"]).lower(), str(r["network_id"] or ""))
    if k in wlk: continue
    ca = pp(stat.get(k))
    sig = pp(r["first_sig"])
    if ca is None:
        unknown += 1
        examples.append(("UNKNOWN_AGE", r["token_address"], r["network_id"]))
    else:
        days = (sig - ca).total_seconds()/86400.0
        if days < config.MIN_TOKEN_AGE_DAYS:
            young += 1
        else:
            old += 1
            examples.append((f"OLD {days:.1f}d", r["token_address"], r["network_id"]))
P(f"  never-admitted trigger tokens (24h) = {young+old+unknown}")
P(f"     younger than MIN_TOKEN_AGE_DAYS={config.MIN_TOKEN_AGE_DAYS} at signal (BY DESIGN) = {young}")
P(f"     old enough but still not admitted (cap / evm-pause)      = {old}")
P(f"     age unknown, no token_static row                          = {unknown}")
for e in examples[:12]:
    P("      ", e)
