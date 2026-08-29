import os, sqlite3, config, sys

def p(*a):
    print(*a); sys.stdout.flush()

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
one = lambda s: con.execute(s).fetchone()[0]
rows = lambda s: con.execute(s).fetchall()

p("loading sets...")
static_keys = set(rows("SELECT token_address, COALESCE(network_id,'') FROM token_static"))
class_keys  = set(rows("SELECT token_address, COALESCE(network_id,'') FROM token_class"))
ww_keys     = set(rows("SELECT DISTINCT token_address, COALESCE(network_id,'') FROM watch_windows"))
ww_sig      = set(r[0] for r in rows("SELECT DISTINCT entry_signal_id FROM watch_windows WHERE entry_signal_id IS NOT NULL"))
ww_fullkeys = set(r[0] for r in rows("SELECT token_address||':'||network_id||':'||first_seen_at FROM watch_windows"))
p("static", len(static_keys), "class", len(class_keys), "ww_tokens", len(ww_keys),
  "ww_sig", len(ww_sig), "ww_fullkeys", len(ww_fullkeys))

p("\n=== signal_events reverse ===")
se = rows("SELECT id, token_address, COALESCE(network_id,'') FROM signal_events")
p("signal_events total:", len(se))
no_ref = sum(1 for i,t,n in se if i not in ww_sig)
p("signal_events never used as entry_signal_id by any window:", no_ref)
no_ww  = sum(1 for i,t,n in se if (t,n) not in ww_keys)
p("signal_events whose (token,net) has NO watch_window:", no_ww)
p("  distinct tokens:", len({(t,n) for i,t,n in se if (t,n) not in ww_keys}))
no_st  = sum(1 for i,t,n in se if (t,n) not in static_keys)
p("signal_events whose (token,net) has NO token_static:", no_st)
p("  distinct tokens:", len({(t,n) for i,t,n in se if (t,n) not in static_keys}))
no_cl  = sum(1 for i,t,n in se if (t,n) not in class_keys)
p("signal_events whose (token,net) has NO token_class:", no_cl)
p("  distinct tokens:", len({(t,n) for i,t,n in se if (t,n) not in class_keys}))

p("\n=== outcomes reverse ===")
oc = rows("SELECT kind, key, token_address, COALESCE(network_id,'') FROM outcomes")
p("outcomes total:", len(oc))
miss_st = [(k,key) for k,key,t,n in oc if (t,n) not in static_keys]
p("outcome rows whose token has no token_static:", len(miss_st))
p("  distinct tokens:", len({(t,n) for k,key,t,n in oc if (t,n) not in static_keys}))
from collections import Counter
p("  by kind:", Counter(k for k,key in miss_st))
miss_cl = [(k,key) for k,key,t,n in oc if (t,n) not in class_keys]
p("outcome rows whose token has no token_class:", len(miss_cl))
p("  by kind:", Counter(k for k,key in miss_cl))

p("\n=== outcomes(kind=watch).key vs watch_windows composite key ===")
wk = [key for k,key,t,n in oc if k=='watch']
p("watch outcomes:", len(wk))
p("  key not matching any watch_windows composite:", sum(1 for key in wk if key not in ww_fullkeys))

p("\n=== watch_windows with no watch outcome ===")
oc_watch = {key for k,key,t,n in oc if k=='watch'}
p("windows total:", len(ww_fullkeys), " windows with no watch outcome:",
  len(ww_fullkeys - oc_watch))

p("\n=== token_static with no token_class ===")
p(len(static_keys - class_keys))

p("\n=== training_rows asset_class distribution ===")
for r in rows("SELECT asset_class, COUNT(*) FROM training_rows GROUP BY asset_class ORDER BY 2 DESC"):
    p("   ", r)

p("\n=== outcomes with no training_row ===")
tr = set(rows("SELECT kind, key FROM training_rows"))
ocset = {(k,key) for k,key,t,n in oc}
p("outcomes lacking a training_row:", len(ocset - tr))
p("training_rows lacking an outcome:", len(tr - ocset))
lack = ocset - tr
if lack:
    ph = ",".join("?" for _ in range(min(len(lack),1)))
    c = Counter()
    for k,key in lack: c[k]+=1
    p("  by kind:", c)

p("\n=== signal_events with no outcome ===")
oc_sig = {key for k,key,t,n in oc if k=='signal'}
se_ids = {i for i,t,n in se}
p("signal_events with no outcome row:", len(se_ids - oc_sig))
con.close()
