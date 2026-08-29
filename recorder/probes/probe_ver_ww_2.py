import os, sqlite3, config
from collections import Counter

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

def q(label, sql, params=()):
    print("=" * 70)
    print(label)
    print("SQL:", " ".join(sql.split()))
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception as e:
        print("FAILED:", e)
        return []
    for r in rows[:30]:
        print("   ", dict(r))
    if len(rows) > 30:
        print("   ... (%d rows)" % len(rows))
    return rows

# D. WAS watch_windows pre-history? when did each design generation start being
#    inserted natively (dv>=2) vs backfilled (dv=1)?
q("D0 watch_windows first_seen_at range per design_version",
  """SELECT design_version, MIN(first_seen_at) mn, MAX(first_seen_at) mx, COUNT(*) n
       FROM watch_windows GROUP BY 1 ORDER BY 1""")
q("D1 outcomes kind=watch labeled_at range per design_version",
  """SELECT design_version, MIN(labeled_at) mn, MAX(labeled_at) mx, COUNT(*) n
       FROM outcomes WHERE kind='watch' GROUP BY 1 ORDER BY 1""")

# E. THE MECHANISM: watchlist PK is (token,network) = ONE row per token, mutable.
#    The dv=1 windows were backfilled from watchlist. So a token that entered the
#    watch more than once can have at most ONE dv=1 window.
q("E0 watchlist rows vs distinct tokens", "SELECT COUNT(*) rows_, COUNT(DISTINCT token_address||':'||network_id) toks FROM watchlist")

wkeys = {}
for r in con.execute("SELECT token_address, network_id, first_seen_at, design_version FROM watch_windows"):
    wkeys[f"{r['token_address']}:{r['network_id']}:{r['first_seen_at']}"] = r["design_version"]

wl = {}
for r in con.execute("SELECT token_address, network_id, first_seen_at, is_control, active FROM watchlist"):
    wl[(r["token_address"], str(r["network_id"] or ""))] = dict(r)

# windows keyed by token/net only (any first_seen)
wtok = {}
for r in con.execute("SELECT token_address, network_id, first_seen_at, design_version FROM watch_windows"):
    wtok.setdefault((r["token_address"], str(r["network_id"] or "")), []).append(
        (r["first_seen_at"], r["design_version"]))

orph = []
byv1 = {}
for r in con.execute(
    """SELECT key, token_address, network_id, is_control, design_version, status, entry_ts
         FROM outcomes WHERE kind='watch' AND design_version=1"""):
    d = dict(r)
    tk = (d["token_address"], str(d["network_id"] or ""))
    byv1.setdefault(tk, []).append(d)
    if d["key"] not in wkeys:
        orph.append(d)

print("=" * 70)
print("E1 v1 watch outcomes:", sum(len(v) for v in byv1.values()),
      "over", len(byv1), "distinct (token,network)")
print("E2 orphans:", len(orph), "over", len(set((o['token_address'], str(o['network_id'] or '')) for o in orph)), "distinct tokens")

# For each orphan token: does SOME window exist for that token (different first_seen)?
has_other_window = sum(1 for o in orph if wtok.get((o["token_address"], str(o["network_id"] or ""))))
print("E3 orphans whose token HAS a watch_windows row under a different first_seen_at:", has_other_window)
print("E4 orphans whose token has NO window at all:", len(orph) - has_other_window)

# Is the orphan token in watchlist with a different first_seen_at (proving mutation of
# the OPERATIONAL table, not of watch_windows)?
in_wl = 0
wl_differs = 0
wl_matches_a_nonorphan = 0
for o in orph:
    tk = (o["token_address"], str(o["network_id"] or ""))
    w = wl.get(tk)
    if w:
        in_wl += 1
        if w["first_seen_at"] != o["key"].rsplit(":", 1)[-1]:
            wl_differs += 1
print("E5 orphan rows whose token is still in watchlist:", in_wl)
print("E6   ...of those, watchlist.first_seen_at != the orphan's first_seen_at:", wl_differs)

# multiplicity: how many v1 outcomes per token, for tokens that have orphans?
mult = Counter()
for o in orph:
    tk = (o["token_address"], str(o["network_id"] or ""))
    mult[len(byv1[tk])] += 1
print("E7 orphan rows by #v1-watch-outcomes on the same token:", dict(sorted(mult.items())))

# how many v1 windows exist per orphan token (should be <=1 because watchlist PK)
wcnt = Counter()
for tk in set((o["token_address"], str(o["network_id"] or "")) for o in orph):
    wcnt[sum(1 for _, dv in wtok.get(tk, []) if dv == 1)] += 1
print("E8 orphan tokens by #design_version=1 windows they own:", dict(sorted(wcnt.items())))

# F. impact: are the 167 training_rows in the model population?
q("F0 training_rows for orphan keys: kind/is_live/feature_version",
  """SELECT kind, is_live, COUNT(*) n FROM training_rows WHERE kind='watch' GROUP BY 1,2""")
q("F1 model population never contains kind='watch'",
  """SELECT COUNT(*) n FROM training_rows
      WHERE kind='watch' AND is_live=1 AND asset_class='meme' AND status='ok'
        AND is_independent=1""")
q("F2 phase1 view excludes all v1",
  "SELECT COUNT(*) n FROM phase1_watch_outcomes WHERE design_version=1")

# G. their SQL verbatim
q("G0 THEIR query verbatim",
  """SELECT COUNT(*) n FROM outcomes o WHERE o.kind='watch' AND NOT EXISTS
       (SELECT 1 FROM watch_windows w WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at = o.key)""")

# H. does the same orphan condition exist for the CURRENT design (dv=3)?
q("H0 orphans per design_version (SQL anti-join)",
  """SELECT o.design_version, COUNT(*) n FROM outcomes o
      WHERE o.kind='watch' AND NOT EXISTS
        (SELECT 1 FROM watch_windows w
          WHERE w.token_address||':'||w.network_id||':'||w.first_seen_at = o.key)
      GROUP BY 1""")

con.close()
