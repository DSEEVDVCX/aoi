"""Interactive login + immediate live verification.

Opens a real Chrome window. YOU log in to fomo.family. As soon as a real user
session exists (privy:token + privy:refresh_token in localStorage), it harvests
the fresh access token and runs the real data-extraction checks through
FomoClient — leaderboard, profile, activity, trades.

Prints only shapes and non-secret fields (handles/counts) — never the token.
Run:  python login_and_verify.py
"""
from __future__ import annotations

import asyncio
import os
import sys

os.environ["FOMO_API_UPSTREAM_IMPERSONATE"] = "true"
os.environ["FOMO_API_DEV"] = "false"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

APP = "https://fomo.family"
LOGIN_TIMEOUT = 300  # 5 minutes to complete login


async def _harvest_token(page) -> str | None:
    read_state = (
        "() => ({"
        "  token: localStorage.getItem('privy:token'),"
        "  refresh: localStorage.getItem('privy:refresh_token')"
        "})"
    )
    for _ in range(LOGIN_TIMEOUT):
        try:
            state = await page.evaluate(read_state)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        token = (state or {}).get("token")
        refresh = (state or {}).get("refresh")
        if token and refresh:
            return token.strip().strip('"')
        await asyncio.sleep(1.0)
    return None


async def _run_checks(token: str) -> None:
    from fomo_api.clients.fomo_client import FomoClient

    client = FomoClient(token)
    try:
        print("\n=== /v1/leaderboard (all) ===", flush=True)
        lb = await client.get_leaderboard(page=1, page_size=5)
        traders = lb["traders"]
        print(f"got {len(traders)} traders; total_items={lb['total_items']}", flush=True)
        for t in traders[:5]:
            print(f"  #{t['rank']:<2} @{t['handle']:<18} followers={t['followers_count']:<6} "
                  f"pnl={t['metrics']['realized_pnl_usd']} vol={t['metrics']['volume_usd']}", flush=True)

        if traders:
            tid = traders[0]["id"]
            print(f"\n=== /v1/traders/{tid} (profile) ===", flush=True)
            prof = await client.get_trader_profile(tid)
            if prof:
                print(f"  @{prof['handle']} display={prof.get('display_name')} "
                      f"followers={prof['followers_count']} num_trades={prof.get('num_trades')}", flush=True)

            print(f"\n=== /v1/traders/{tid}/activity (swaps) ===", flush=True)
            act = await client.get_trader_activity(tid, page=1, page_size=5)
            if act:
                print(f"  got {act['total_items']} swaps (showing up to 5)", flush=True)
                for a in act["actions"][:5]:
                    print(f"    swap {a['timestamp']} out_token={a['token']['address']} "
                          f"usd={a['amount_usd']} chain={a['chain']}", flush=True)

            print(f"\n=== /trades?userId={tid} ===", flush=True)
            tr = await client.get_trader_trades(tid)
            print(f"  active={len(tr['active_trades'])} closed={len(tr['closed_trades'])} "
                  f"closed_count={tr['closed_count']}", flush=True)
        print("\n[OK] LIVE VERIFICATION SUCCEEDED - real data extracted.", flush=True)
    finally:
        await client.aclose()


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(channel="chrome", headless=False,
                                                args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            browser = await pw.chromium.launch(headless=False,
                                               args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context()
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        print("=" * 70, flush=True)
        print(">>> سجّل دخولك في نافذة المتصفح الآن. سيبدأ الاستخراج تلقائياً بعد الدخول.", flush=True)
        print(f">>> (مهلة {LOGIN_TIMEOUT//60} دقائق)", flush=True)
        print("=" * 70, flush=True)

        token = await _harvest_token(page)
        try:
            await browser.close()
        except Exception:
            pass

    if not token:
        print(">>> لم يُلتقط أي رمز دخول ضمن المهلة.", flush=True)
        return
    print(">>> تم التقاط رمز دخول جديد. بدء الفحص الحيّ...", flush=True)
    await _run_checks(token)


if __name__ == "__main__":
    asyncio.run(main())
