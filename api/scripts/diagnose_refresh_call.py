"""Capture the EXACT session-refresh request the fomo.family SPA sends to
Privy, so we can replicate it server-side. Log in when the browser opens; the
script then reloads the page to trigger Privy's own token refresh and records
the request method/URL/headers/body + response keys.

Secrets (token values) are written only to the local dump file, never printed.
Run: python scripts\\diagnose_refresh_call.py
"""
from __future__ import annotations

import asyncio
import json

URL = "https://fomo.family"
PRIVY_SESSIONS = "auth.privy.io/api/v1/sessions"
OUT = "privy_refresh_call.json"


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
        page = await context.new_page()

        calls: list[dict] = []

        async def on_response(resp) -> None:
            if PRIVY_SESSIONS in resp.url:
                req = resp.request
                entry = {
                    "method": req.method,
                    "url": resp.url,
                    "status": resp.status,
                    "request_headers": dict(req.headers),
                    "request_body": req.post_data,
                    "response_headers": dict(resp.headers),
                }
                try:
                    entry["response_json_keys"] = sorted((await resp.json()).keys())
                except Exception:
                    entry["response_json_keys"] = None
                calls.append(entry)
                print(f">>> captured {req.method} {PRIVY_SESSIONS} -> {resp.status}", flush=True)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        await page.goto(URL, wait_until="domcontentloaded")
        print(">>> سجّل دخولك الآن. بانتظار جلسة مصادَق عليها (حتى 5 دقائق)...", flush=True)

        # wait for login (refresh token present)
        for _ in range(150):
            try:
                rt = await page.evaluate("() => localStorage.getItem('privy:refresh_token')")
                if rt:
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        print(">>> تم تسجيل الدخول. إعادة تحميل الصفحة لتحفيز تجديد التوكن...", flush=True)
        for _ in range(3):
            await asyncio.sleep(2)
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
        await asyncio.sleep(3)

        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"calls": calls}, f, indent=2, default=str)

        # print sanitized summary (redact token values in bodies/headers)
        for c in calls:
            safe = {
                "method": c["method"],
                "status": c["status"],
                "url": c["url"],
                "request_header_names": sorted(c["request_headers"].keys()),
                "has_authorization": "authorization" in {k.lower() for k in c["request_headers"]},
                "request_body_keys": _body_keys(c.get("request_body")),
                "response_json_keys": c.get("response_json_keys"),
            }
            print(json.dumps(safe, indent=2, ensure_ascii=False), flush=True)
        print(f">>> التفاصيل الكاملة في {OUT}", flush=True)
        await browser.close()


def _body_keys(body):
    if not body:
        return None
    try:
        return sorted(json.loads(body).keys())
    except Exception:
        return "(non-json)"


if __name__ == "__main__":
    asyncio.run(main())
