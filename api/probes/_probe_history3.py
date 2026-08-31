import asyncio
import json
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

    # 1) deeper: how many days back? (15 pages × 50)
    print("=== DEEP WALK ===")
    last_id, oldest_seen = None, None
    total = 0
    for page in range(1, 16):
        params = {"limit": 50, "threshold": 0}
        if last_id:
            params["lastId"] = last_id
        try:
            env = await client._get(settings.upstream_alerts_path, params)
            ro, items = ro_items(env)
            if not items:
                print(f"page {page}: EMPTY -> end of history")
                break
            total += len(items)
            ts = [e.get("createdAt") for e in items if isinstance(e, dict) and e.get("createdAt")]
            oldest_seen = min(ts) if ts else oldest_seen
            last_id = items[-1].get("id") if isinstance(items[-1], dict) else None
            if page in (5, 10, 15):
                print(f"page {page}: total={total} oldest so far={oldest_seen}")
            has_next = isinstance(ro, dict) and ro.get("hasNextPage")
            if has_next is False:
                print(f"page {page}: hasNextPage=False -> END at total={total}, oldest={oldest_seen}")
                break
        except Exception as e:
            print(f"page {page}: ERR {type(e).__name__} (stopping; total={total}, oldest={oldest_seen})")
            break
        await asyncio.sleep(1.5)

    # 2) event fields: swap_buy and multi_user_buy
    print("=== EVENT FIELD SHAPES ===")
    env = await client._get(settings.upstream_alerts_path, {"limit": 50, "threshold": 0})
    ro, items = ro_items(env)
    seen = set()
    for e in items:
        if not isinstance(e, dict):
            continue
        t = e.get("type") or "?"
        if t in seen or t == "?":
            continue
        seen.add(t)
        body = e.get("body") or {}
        print(f"type={t}: top_keys={sorted(k for k in e if k != 'body')}")
        print(f"  body_keys={sorted(body.keys())}")
        if t == "swap_buy":
            print("  sample:", json.dumps(e, ensure_ascii=False)[:500])
    await client.aclose()

asyncio.run(main())
