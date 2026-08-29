"""Live end-to-end verification that the fomo.family extraction works against the
REAL production API. Reads the harvested Privy token from privy_storage_dump.json,
refreshes it if needed (CONFIRMED refresh flow), then calls FomoClient for real.

Prints only data SHAPES and a few non-secret fields (handles, counts) — never the
token. Run:  python verify_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

DUMP = os.path.join(os.path.dirname(__file__), "privy_storage_dump.json")

# Force production path: curl_cffi impersonation, no dev shims.
os.environ["FOMO_API_UPSTREAM_IMPERSONATE"] = "true"
os.environ["FOMO_API_DEV"] = "false"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))


def _load_creds() -> dict[str, str | None]:
    with open(DUMP, encoding="utf-8") as fh:
        d = json.load(fh)
    ls = d.get("_full_localStorage", {})

    def _clean(v):
        return v.strip().strip('"') if isinstance(v, str) else v

    token = _clean(ls.get("privy:token"))
    refresh = _clean(ls.get("privy:refresh_token"))
    pat = _clean(ls.get("privy:pat"))
    ca_id = _clean(ls.get("privy:caid"))
    # app id is embedded in a key like privy:<appId>:recent-login-method
    app_id = None
    for k in ls:
        m = k.split(":")
        if len(m) >= 3 and m[0] == "privy" and m[2] == "recent-login-method":
            app_id = m[1]
            break
    client_id = None
    for _k, v in ls.items():
        if isinstance(v, str) and "client-" in v:
            import re

            mm = re.search(r"client-[A-Za-z0-9]{20,}", v)
            if mm:
                client_id = mm.group(0)
                break
    return {
        "token": token,
        "refresh": refresh,
        "pat": pat,
        "app_id": app_id,
        "client_id": client_id,
        "ca_id": ca_id,
    }


async def _maybe_refresh(creds: dict[str, str | None]) -> str:
    """Return a working access token, refreshing first if we have the pieces."""
    from fomo_api.auth.token_refresher import _call_privy_refresh

    if creds["refresh"] and creds["app_id"] and creds["pat"]:
        try:
            res = await _call_privy_refresh(
                refresh_token=creds["refresh"],
                app_id=creds["app_id"],
                pat=creds["pat"],
                client_id=creds["client_id"],
                ca_id=creds["ca_id"],
            )
            print("[refresh] OK — using freshly minted access token")
            return res["access"]
        except Exception as exc:
            print(f"[refresh] failed ({exc}); falling back to stored token")
    return creds["token"]


async def main() -> None:
    creds = _load_creds()
    print("[creds] token:", "present" if creds["token"] else "MISSING",
          "| refresh:", bool(creds["refresh"]), "| pat:", bool(creds["pat"]),
          "| app_id:", bool(creds["app_id"]))
    if not creds["token"]:
        print("No access token in dump; cannot verify. Run capture/login first.")
        return

    token = await _maybe_refresh(creds)

    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(token)
    try:
        print("\n=== /v1/leaderboard (all) ===")
        lb = await client.get_leaderboard(page=1, page_size=5)
        traders = lb["traders"]
        print(f"got {len(traders)} traders; total_items={lb['total_items']}")
        for t in traders[:5]:
            print(f"  #{t['rank']:<2} @{t['handle']:<18} followers={t['followers_count']:<6} "
                  f"pnl={t['metrics']['realized_pnl_usd']} vol={t['metrics']['volume_usd']}")

        if traders:
            tid = traders[0]["id"]
            print(f"\n=== /v1/traders/{tid} (profile) ===")
            prof = await client.get_trader_profile(tid)
            if prof:
                print(f"  @{prof['handle']} display={prof.get('display_name')} "
                      f"followers={prof['followers_count']} num_trades={prof.get('num_trades')}")

            print(f"\n=== /v1/traders/{tid}/activity (swaps) ===")
            act = await client.get_trader_activity(tid, page=1, page_size=5)
            if act:
                print(f"  got {act['total_items']} swaps (showing up to 5)")
                for a in act["actions"][:5]:
                    print(f"    swap {a['timestamp']} out_token={a['token']['address']} "
                          f"usd={a['amount_usd']} chain={a['chain']}")

            print(f"\n=== /trades?userId={tid} ===")
            tr = await client.get_trader_trades(tid)
            print(f"  active={len(tr['active_trades'])} closed={len(tr['closed_trades'])} "
                  f"closed_count={tr['closed_count']}")
        print("\n[OK] LIVE VERIFICATION SUCCEEDED - real data extracted.")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
