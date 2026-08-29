import asyncio
import sys

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings

CANDIDATES = ["large_sell", "largeSell", "whale_sell", "sell", "multi_sell",
              "multi_user_buy"]  # الأخير معروف — شاهد إثبات أن المسبار يعمل

async def probe(client, ftype):
    # نوع مجهول وحده → 400 (موثّق في config: all-unknown list 400s).
    # نوع صالح → 200 حتى لو بلا أحداث حديثة.
    try:
        r = await client._client.get(
            client._url(settings.upstream_feed_path),
            params=[("feedTypes", ftype), ("limit", 5)],
        )
    except Exception as e:
        return f"EXC {type(e).__name__}: {e}"
    if r.status_code != 200:
        return f"{r.status_code} {r.text[:80].replace(chr(10), ' ')}"
    j = r.json()
    ro = j.get("responseObject") or {}
    items = ro.get("items") or ro.get("feed") or ro.get("data") or []
    types = {e.get("type") for e in items if isinstance(e, dict)}
    return f"200 items={len(items)} types={types}"

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)
    for ftype in CANDIDATES:
        print(f"{ftype:20s} -> {await probe(client, ftype)}")
        await asyncio.sleep(1.5)
    await client.aclose()

asyncio.run(main())
