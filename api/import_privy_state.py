"""Import Privy credentials captured manually from the browser Console.

Companion to extract_from_console.js. After you paste that snippet in the
fomo.family Console and it downloads privy_state.json, run:

    python import_privy_state.py <path-to-downloaded-privy_state.json>

It saves the creds to the persistent state file, proves a browserless refresh
works, and reads live leaderboard data. No automation, no Google block. From
then on the server auto-refreshes forever. Prints only handles/counts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ.setdefault("FOMO_API_UPSTREAM_IMPERSONATE", "true")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def _find_default_download() -> str | None:
    """If no path is given, look for privy_state.json in common spots."""
    candidates = [
        "privy_state.json",
        os.path.join(os.path.expanduser("~"), "Downloads", "privy_state.json"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


async def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else _find_default_download()
    if not src or not os.path.isfile(src):
        print("Usage: python import_privy_state.py <path to privy_state.json>", flush=True)
        print("(or put the file in the api/ folder or Downloads and run with no arguments)", flush=True)
        return

    from fomo_api.auth.credential_store import CredentialStore, StoredCredentials
    from fomo_api.config import settings

    with open(src, encoding="utf-8") as fh:
        raw = json.load(fh)

    def _c(v):
        return v.strip().strip('"') if isinstance(v, str) and v.strip() else None

    creds = StoredCredentials(
        access_token=_c(raw.get("access_token")),
        refresh_token=_c(raw.get("refresh_token")),
        pat=_c(raw.get("pat")),
        app_id=_c(raw.get("app_id")),
        client_id=_c(raw.get("client_id")),
        ca_id=_c(raw.get("ca_id")),
    )
    if not creds.refresh_token:
        print(">>> The file has no refresh_token — re-extract after signing in.", flush=True)
        return

    store = CredentialStore(settings.credential_state_file)
    store.save(creds)
    print(f">>> Credentials saved to {store.path} | refreshable: {creds.is_refreshable()}", flush=True)

    from fomo_api.auth.token_refresher import _call_privy_refresh

    try:
        r = await _call_privy_refresh(
            refresh_token=creds.refresh_token, app_id=creds.app_id, pat=creds.pat,
            client_id=creds.client_id, ca_id=creds.ca_id,
            current_access=creds.access_token,
        )
        store.save(StoredCredentials(access_token=r.get("access"), refresh_token=r.get("refresh"), pat=r.get("pat")))
        access = r.get("access")
        print(">>> [OK] Browserless refresh succeeded — the system renews itself automatically from now on.", flush=True)
    except Exception as exc:
        print(f">>> Warning: the immediate refresh failed ({exc}); the captured token will be used.", flush=True)
        access = creds.access_token

    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(access)
    try:
        lb = await client.get_leaderboard(page=1, page_size=5, period="all")
        traders = lb["traders"] if isinstance(lb, dict) else lb.traders
        total = lb["total_items"] if isinstance(lb, dict) else lb.total_items
        print(f">>> Live data: {total} traders on the leaderboard", flush=True)
        for t in (traders[:3] if isinstance(traders, list) else traders):
            print(f"      @{t['handle'] if isinstance(t, dict) else t.handle}", flush=True)
        print(">>> Setup complete. Start the server and extraction runs automatically with no intervention.", flush=True)
    except Exception as exc:
        print(f">>> Live read failed ({type(exc).__name__}): the token is invalid or expired.", flush=True)
        print(">>> Re-extract from the Console after confirming you are signed in to fomo.family.", flush=True)
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
