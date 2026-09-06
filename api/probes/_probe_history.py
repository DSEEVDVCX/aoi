import asyncio
import sqlite3
import sys
import time

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings


def items_of(env):
    ro = (env or {}).get("responseObject") or {}
    for k in ("feed", "items", "data"):
        if isinstance(ro.get(k), list):
            return ro[k]
    return []

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)

    # 1) paging the feed backwards
    print("=== FEED HISTORY (proper unwrap) ===")
    base = await client._get(settings.upstream_feed_path,
                             {"feedTypes": ["large_buy"], "limit": 5})
    items0 = items_of(base)
    print("base items:", len(items0), "oldest:", items0[-1].get("createdAt") if items0 else None)
    if items0:
        last = items0[-1]
        for pname, val in (("lastId", last.get("id")), ("cursor", last.get("id")),
                           ("before", last.get("createdAt")), ("page", 2), ("offset", 5)):
            try:
                r = await client._get(settings.upstream_feed_path,
                                      {"feedTypes": ["large_buy"], "limit": 5, pname: val})
                its = items_of(r)
                ids0 = {e.get("id") for e in items0}
                new_ids = [e.get("id") for e in its if e.get("id") not in ids0]
                print(f"{pname:8s}: items={len(its)} new_vs_base={len(new_ids)} oldest={its[-1].get('createdAt') if its else None}")
            except Exception as e:
                print(f"{pname:8s}: ERR {type(e).__name__}: {str(e)[:70]}")
            await asyncio.sleep(1.2)

    # 2) candles of a genuinely old coin (2025-08/2025-11 theses) at their writing time
    print("=== BARS AT OLD THESIS TIME ===")
    db_uri = "file:C:/Users/rr/Desktop/aoi/recorder/recorder.db?mode=ro"
    db = sqlite3.connect(db_uri, uri=True)
    olds = db.execute(
        "SELECT token_address, network_id, MIN(created_at) FROM token_thesis "
        "GROUP BY token_address ORDER BY 3 LIMIT 4"
    ).fetchall()
    for addr, net, created in olds:
        t = int(time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))) - 4*3600  # rough UTC+4? no — created is UTC
        body = {"symbol": f"{addr}:{net}", "resolution": "60",
                "from": t - 3600*12, "to": t + 3600*12, "countBack": 50}
        try:
            r = await client._client.post(client._url(settings.upstream_get_bars_path), json=body)
            if r.status_code == 200:
                ro = r.json().get("responseObject") or {}
                print(f"{str(addr)[:12]}... net={net} thesis={created[:10]}: candles={len(ro.get('c') or [])} s={ro.get('s')}")
            else:
                print(f"{str(addr)[:12]}... net={net}: HTTP {r.status_code}")
        except Exception as e:
            print(f"{str(addr)[:12]}...: ERR {type(e).__name__}")
        await asyncio.sleep(1.5)
    await client.aclose()

asyncio.run(main())
