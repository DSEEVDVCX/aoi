import os, sqlite3, config, datetime, collections

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=600)
con.row_factory = sqlite3.Row

NOW = datetime.datetime.now(datetime.timezone.utc)

def parse(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d

stamps = sorted(parse(r[0]) for r in con.execute("SELECT DISTINCT recorded_at FROM snapshots"))
print("all cycle stamps:", len(stamps), stamps[0].isoformat(), "->", stamps[-1].isoformat())

def window(days):
    cut = NOW - datetime.timedelta(days=days)
    s = [x for x in stamps if x >= cut]
    if len(s) < 2:
        return
    span = (s[-1] - s[0]).total_seconds() / 60.0
    gl = [(b - a).total_seconds() / 60.0 for a, b in zip(s, s[1:]) if (b - a).total_seconds() > 600]
    tot = sum(gl)
    print(f"  last {days:>3}d: span={span:8.1f}min  gaps>10m={len(gl):3d}  down={tot:8.1f}min  = {100*tot/span:5.2f}%  up={100-100*tot/span:5.2f}%")

print("\n=== downtime by window (gaps > 10 min) ===")
for d in (1, 2, 3, 7, 14, 28):
    window(d)

print("\n=== UTC hour-of-day: missing minutes over last 7 days ===")
cut = NOW - datetime.timedelta(days=7)
s7 = [x for x in stamps if x >= cut]
byhour = collections.Counter()
for a, b in zip(s7, s7[1:]):
    g = (b - a).total_seconds()
    if g <= 600:
        continue
    t = a
    while t < b:
        nxt = min(b, (t + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        byhour[t.hour] += (nxt - t).total_seconds() / 60.0
        t = nxt
for h in range(24):
    bar = "#" * int(byhour[h] / 10)
    print(f"  {h:02d}:00Z  {byhour[h]:7.1f} min  {bar}")

print("\n=== per-day downtime, last 10 days (UTC) ===")
bydate = collections.Counter()
for a, b in zip(s7, s7[1:]):
    g = (b - a).total_seconds()
    if g <= 600:
        continue
    t = a
    while t < b:
        nxt = min(b, (t + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
        bydate[t.date().isoformat()] += (nxt - t).total_seconds() / 60.0
        t = nxt
for d in sorted(bydate):
    print(f"  {d}  {bydate[d]:7.1f} min down ({100*bydate[d]/1440:.1f}% of day)")
