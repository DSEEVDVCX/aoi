"""Adversarial verification, step 2: is the EVM ledger walk still pending, or latched?
READ-ONLY. Re-implements repair_evm_ledger.inspect() in raw SQL (no RecorderDB)."""
import os, sqlite3, json, time, config
from datetime import UTC, datetime
import db as dbmod
import evm_replay

uri = f"file:{config.DB_PATH.replace(os.sep,'/')}?mode=ro"
con = sqlite3.connect(uri, uri=True, timeout=120)
con.row_factory = sqlite3.Row

cohort = json.loads(con.execute(
    "SELECT value FROM meta WHERE key='evm_repair_cohort'").fetchone()[0])
print("cohort version      :", cohort.get("version"))
print("cohort captured_at  :", cohort.get("captured_at"))
print("cohort networks     :", cohort.get("networks"))
ab = cohort.get("active_backfills", [])
rw = cohort.get("replay_windows", [])
print(f"cohort active_backfills: {len(ab)}   replay_windows: {len(rw)}")


def epoch(v):
    return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())


cutoff = epoch(cohort["captured_at"])
print("cohort cutoff epoch :", cutoff, "=", datetime.fromtimestamp(cutoff, UTC).isoformat())

# ---- active_pending: cohort backfills whose evm_backfill_state.status != 'done'
bf = {}
for r in con.execute("SELECT network_id, token_address, status FROM evm_backfill_state"):
    bf[(str(r["network_id"]), str(r["token_address"]).lower())] = r["status"]
active_pending = 0
by_net = {}
for item in ab:
    k = (str(item["network_id"]), str(item["token_address"]).lower())
    st = bf.get(k)
    if st != "done":
        active_pending += 1
        by_net[k[0]] = by_net.get(k[0], 0) + 1
print(f"\nactive_pending = {active_pending}   by network: {by_net}")

# ---- replay_pending: cohort windows not final and checkpoint behind last mature grid
now = int(time.time())
step = max(1, int(config.EVM_REPLAY_STEP_SECONDS))
final = set(evm_replay.FINAL_STATUSES)
state = {}
for r in con.execute(
    "SELECT network_id, token_address, status AS replay_status, "
    "checkpoint_json AS replay_checkpoint_json, last_try_at FROM evm_replay_state"
):
    state[(str(r["network_id"]), str(r["token_address"]).lower())] = r
wanted = {}
for w in rw:
    wanted.setdefault((str(w["network_id"]), str(w["token_address"]).lower()), []).append(w)
replay_pending = 0
rp_by_net = {}
final_ct = 0
no_state = 0
for k, windows in wanted.items():
    mature = [w for w in windows if epoch(w["watch_until"]) <= now]
    st = state.get(k)
    status = (st["replay_status"] if st else None) or ""
    if not mature:
        continue
    if status in final:
        final_ct += 1
        continue
    ck = None
    if st is not None and st["replay_checkpoint_json"] is not None:
        try:
            d = dbmod.decode_raw(st["replay_checkpoint_json"])
            ck = d if isinstance(d, dict) else None
        except Exception:
            ck = None
    if st is None:
        no_state += 1
    last_mature = max(epoch(w["watch_until"]) for w in mature)
    last_mature = (last_mature // step) * step
    nxt = int((ck or {}).get("next_grid") or 0)
    if nxt <= last_mature:
        replay_pending += 1
        rp_by_net[k[0]] = rp_by_net.get(k[0], 0) + 1
print(f"replay_pending = {replay_pending}   by network: {rp_by_net}")
print(f"  (cohort keys with mature windows already FINAL: {final_ct}; "
      f"keys with no evm_replay_state row at all: {no_state})")
print(f"  distinct cohort replay keys: {len(wanted)}")
con.close()
