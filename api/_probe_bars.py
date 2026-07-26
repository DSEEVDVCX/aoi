import asyncio, sys, time
sys.path.insert(0, "src")
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings
from fomo_api.auth.credential_store import CredentialStore

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)
    tr = await client.get_trending_tokens()
    # try several different tokens, well-formed body, spaced out
    now = int(time.time())
    oks = 0
    for tok in tr[:8]:
        addr = tok.get("address")
        if not addr:
            continue
        body = {"symbol": addr, "resolution":"60","from":now-3600*100,"to":now,"countBack":100}
        try:
            r = await client._client.post(client._url(settings.upstream_get_bars_path), json=body)
            if r.status_code == 200:
                j = r.json()
                ro = j.get("responseObject") or {}
                print(f"OK {tok.get('symbol')}: candles={len(ro.get('c') or [])} s={ro.get('s')}")
                oks += 1
            else:
                print(f"{r.status_code} {tok.get('symbol')} {r.text[:60].replace(chr(10),' ')}")
        except Exception as e:
            print("EXC", e)
        await asyncio.sleep(3)
    print("TOTAL OK:", oks)
    await client.aclose()

asyncio.run(main())
