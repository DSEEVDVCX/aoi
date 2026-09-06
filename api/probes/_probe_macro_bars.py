import asyncio
import sys
import time

sys.path.insert(0, "src")
from fomo_api.auth.credential_store import CredentialStore
from fomo_api.clients.fomo_client import FomoClient
from fomo_api.config import settings

CANDIDATES = [
    ("SOL",  "So11111111111111111111111111111111111111112", "1399811149"),
    ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "1"),
    ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "1"),
]

async def main():
    creds = CredentialStore(settings.credential_state_file).load()
    client = FomoClient(session_token=creds.access_token)
    now = int(time.time())
    for name, addr, net in CANDIDATES:
        body = {"symbol": f"{addr}:{net}", "resolution": "60",
                "from": now - 7*24*3600, "to": now, "countBack": 200}
        try:
            r = await client._client.post(client._url(settings.upstream_get_bars_path), json=body)
            if r.status_code == 200:
                ro = r.json().get("responseObject") or {}
                closes = ro.get("c") or []
                print(f"{name}: 200 candles={len(closes)} s={ro.get('s')} last_c={closes[-1] if closes else None}")
            else:
                print(f"{name}: {r.status_code} {r.text[:80].replace(chr(10),' ')}")
        except Exception as e:
            print(f"{name}: EXC {type(e).__name__}: {e}")
        await asyncio.sleep(2)
    await client.aclose()

asyncio.run(main())
