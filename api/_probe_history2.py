import asyncio
import sys

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings


def ro_items(env):
    ro = (env or {}).get("responseObject") or {}
    if isinstance(ro, list):
        return ro, ro
    for k in ("items", "feed", "activities", "tradingActivity", "data", "trades"):
        if isinstance(ro.get(k), list):
            return ro, ro[k]
    return ro, []

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)

    print("=== /feed/tradingActivity PAGINATION ===")
    last_id = None
    for page in range(1, 6):
        params = {"limit": 50, "threshold": 0}
        if last_id:
            params["lastId"] = last_id
        try:
            env = await client._get(settings.upstream_alerts_path, params)
            ro, items = ro_items(env)
            if not items:
                print(f"page {page}: EMPTY ro_keys={list(ro.keys()) if isinstance(ro, dict) else type(ro)}")
                break
            ts = [e.get("createdAt") for e in items if isinstance(e, dict) and e.get("createdAt")]
            types = {}
            for e in items:
                if isinstance(e, dict):
                    t = e.get("type") or e.get("action") or "?"
                    types[t] = types.get(t, 0) + 1
            print(f"page {page}: n={len(items)} oldest={min(ts) if ts else '?'} newest={max(ts) if ts else '?'} types={types}")
            has_next = isinstance(ro, dict) and ro.get("hasNextPage")
            last_id = items[-1].get("id") if isinstance(items[-1], dict) else None
            if not has_next and page > 1:
                print("hasNextPage=False -> end")
                break
        except Exception as e:
            print(f"page {page}: ERR {type(e).__name__}: {str(e)[:100]}")
            break
        await asyncio.sleep(1.5)

    print("=== /trades?tokenAddress= DEPTH ===")
    import sqlite3
    db = sqlite3.connect(r"C:\Users\rr\Desktop\aoi\recorder\recorder.db")
    tok = db.execute(
        "SELECT token_address, network_id FROM signal_events ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if tok:
        addr, _net = tok
        try:
            env = await client._get("/trades", {"tokenAddress": addr, "limit": 100})
            ro, items = ro_items(env)
            ts = [e.get("createdAt") for e in items if isinstance(e, dict) and e.get("createdAt")]
            keys = list(ro.keys()) if isinstance(ro, dict) else None
            print(f"token {addr[:12]}...: n={len(items)} oldest={min(ts) if ts else '?'} newest={max(ts) if ts else '?'} ro_keys={keys}")
            if items:
                e0 = items[0]
                print("sample fields:", sorted(e0.keys())[:20])
        except Exception as e:
            print(f"trades: ERR {type(e).__name__}: {str(e)[:100]}")
    await client.aclose()

asyncio.run(main())
