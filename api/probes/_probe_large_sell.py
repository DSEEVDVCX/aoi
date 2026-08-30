import asyncio
import json
import sys

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings


async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)
    r = await client._client.get(
        client._url(settings.upstream_feed_path),
        params=[("feedTypes", "large_sell"), ("limit", 10)],
    )
    j = r.json()
    ro = j.get("responseObject") or {}
    items = ro.get("items") or ro.get("feed") or ro.get("data") or []
    for e in items:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        body = e.get("body") or {}
        print(f"type={t} keys={sorted(body.keys())}")
        if t == "large_sell":
            print("SAMPLE:", json.dumps(e, ensure_ascii=False)[:900])
            break
    await client.aclose()

asyncio.run(main())
