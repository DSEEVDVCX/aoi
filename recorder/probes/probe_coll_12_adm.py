import os, sqlite3, config, datetime as dt, sys, collections

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row
NOW = dt.datetime.now(dt.timezone.utc)
def P(*a): print(*a); sys.stdout.flush()

P("TRIGGER_SIGNAL_TYPES =", config.TRIGGER_SIGNAL_TYPES)
P("WATCHLIST_CAP =", config.WATCHLIST_CAP, " WATCH_HOURS =", config.WATCH_HOURS)

wlk = {(str(r[0]).lower(), str(r[1] or '')) for r in con.execute("SELECT token_address, network_id FROM watchlist")}
P(f"watchlist rows(any state) = {len(wlk)}")

marks = ",".join("?" for _ in config.TRIGGER_SIGNAL_TYPES)
for hours in (6, 24, 72):
    cut = (NOW - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = con.execute(
        f"SELECT DISTINCT token_address, network_id FROM signal_events "
        f"WHERE signal_type IN ({marks}) AND recorded_at >= ?",
        tuple(config.TRIGGER_SIGNAL_TYPES) + (cut,)).fetchall()
    toks = {(str(r[0]).lower(), str(r[1] or '')) for r in rows}
    new = [k for k in toks if k not in wlk]
    P(f"\nlast {hours}h: trigger signals on {len(toks)} distinct tokens; NEVER in watchlist (blocked/never admitted) = {len(new)} ({100*len(new)/max(1,len(toks)):.0f}%)")
    P("   by network:", dict(collections.Counter(k[1] for k in new)))

P("\n=== when did the last brand-new coin enter the watchlist? ===")
for r in con.execute("""SELECT token_address, network_id, first_seen_at, is_control FROM watchlist
                        ORDER BY first_seen_at DESC LIMIT 5"""):
    P("  ", tuple(r))
P("  earliest window per coin -> newest 'first ever' admission:")
for r in con.execute("""SELECT MIN(first_seen_at) f, token_address, network_id, is_control
                        FROM watch_windows GROUP BY token_address, network_id
                        ORDER BY f DESC LIMIT 8"""):
    P("  ", tuple(r))

P("\n=== active-op watches: is the current window a REVIVAL (an earlier window closed first)? ===")
act = [dict(r) for r in con.execute("SELECT token_address, network_id, first_seen_at FROM watchlist WHERE active=1 AND is_control=0")]
firsts = {}
for r in con.execute("SELECT token_address, network_id, MIN(first_seen_at) f, COUNT(*) n FROM watch_windows GROUP BY token_address, network_id"):
    firsts[(str(r[0]).lower(), str(r[1] or ''))] = (r["f"], r["n"])
def pp(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None
older = 0
newer = 0
for w in act:
    k = (str(w["token_address"]).lower(), str(w["network_id"] or ""))
    f = firsts.get(k, (None, 0))[0]
    a, b = pp(f), pp(w["first_seen_at"])
    if a and b and (b-a).total_seconds() > config.WATCH_HOURS*3600:
        older += 1
    else:
        newer += 1
P(f"  active-op={len(act)}: first-ever window older than WATCH_HOURS({config.WATCH_HOURS}h) before current = {older} (true revivals); within one window = {newer}")

P("\n=== evm backfill backlog (drives evm_admission_paused) ===")
r = con.execute("""SELECT COUNT(*) FROM watchlist w LEFT JOIN evm_backfill_state b
      ON b.network_id=w.network_id AND lower(b.token_address)=lower(w.token_address)
     WHERE w.active=1 AND w.network_id IN ('4663','8453','143') AND COALESCE(b.status,'')<>'done'""").fetchone()[0]
P(f"  backlog now = {r}  (PAUSE_AT={config.EVM_ADMISSION_PAUSE_AT}, RESUME_BELOW={config.EVM_ADMISSION_RESUME_BELOW})")
for r in con.execute("""SELECT COALESCE(b.status,'MISSING') st, COUNT(*) n FROM watchlist w
      LEFT JOIN evm_backfill_state b ON b.network_id=w.network_id AND lower(b.token_address)=lower(w.token_address)
     WHERE w.active=1 AND w.network_id IN ('4663','8453','143') GROUP BY 1 ORDER BY n DESC"""):
    P(f"     {r['st']:10s} {r['n']}")
