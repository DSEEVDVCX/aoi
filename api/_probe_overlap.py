import asyncio
import sqlite3
import sys

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings


def ro_items(env):
    ro = (env or {}).get("responseObject") or {}
    for k in ("items", "feed", "activities", "data"):
        if isinstance(ro.get(k), list):
            return ro[k]
    return []

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)
    db = sqlite3.connect(r"C:\Users\rr\Desktop\aoi\recorder\recorder.db")

    # اجمع ~200 حدثاً (4 صفحات) وقاطع معرفاتها مع signal_events
    ids_by_type = {}
    last_id = None
    for _ in range(4):
        params = {"limit": 50, "threshold": 0}
        if last_id:
            params["lastId"] = last_id
        env = await client._get(settings.upstream_alerts_path, params)
        items = ro_items(env)
        if not items:
            break
        for e in items:
            if isinstance(e, dict) and e.get("id"):
                ids_by_type.setdefault(e.get("type") or "?", set()).add(e["id"])
        last_id = items[-1].get("id")
        await asyncio.sleep(1.5)
    await client.aclose()

    print("overlap with signal_events (recorded from /feed):")
    for t, ids in sorted(ids_by_type.items()):
        hits = sum(
            db.execute("SELECT COUNT(*) FROM signal_events WHERE id=?", (i,)).fetchone()[0]
            for i in ids
        )
        print(f"  {t:16s}: {hits}/{len(ids)} موجودة في signal_events")

    # وهل swap_buy له createdAt ضمن مدى تغطيتنا؟
    r = db.execute("SELECT MIN(ts), MAX(ts) FROM signal_events").fetchone()
    print("signal_events ts range:", r)

asyncio.run(main())
