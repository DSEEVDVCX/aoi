"""Capture the REAL prod-api.fomo.family endpoints the SPA calls, so we can
replace the UNVERIFIED best-guess paths in config.py (leaderboard / activity /
alerts) with confirmed ones.

Opens a real browser. YOU log in, then navigate the site:
  1. Leaderboard page   -> reveals the leaderboard endpoint
  2. Open a trader/profile, scroll their activity -> reveals profile + activity
  3. Open the alerts/notifications view -> reveals the alerts endpoint

Every request to https://prod-api.fomo.family is logged with method, path,
query params, and a small shape sample of the JSON response (KEY NAMES ONLY,
never values). Results are written to captured_endpoints.json.

Run:  python scripts\\capture_endpoints.py
Press Ctrl+C in the terminal (or close the browser) when done capturing.
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse, parse_qs

DATA_HOST = "https://prod-api.fomo.family"
OUT = "captured_endpoints.json"
_MAX_KEYS = 40


def _shape(obj: object, depth: int = 0) -> object:
    """Return a structural sketch of a JSON value: dict->key names (recursing
    one level into the first item of lists), list->['<len=N>', <item shape>].
    Never includes scalar VALUES, only types/keys, so no secrets leak."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= _MAX_KEYS:
                out["..."] = f"(+{len(obj) - _MAX_KEYS} more keys)"
                break
            out[k] = _shape(v, depth + 1)
        return out
    if isinstance(obj, list):
        if not obj:
            return ["<empty list>"]
        return [f"<len={len(obj)}>", _shape(obj[0], depth + 1)]
    return type(obj).__name__


async def main() -> None:
    from playwright.async_api import async_playwright

    captured: dict[str, dict] = {}  # key: "METHOD /path" -> record

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                channel="chrome", headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            browser = await pw.chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"],
            )
        context = await browser.new_context()
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        async def on_response(resp: object) -> None:
            url = resp.url  # type: ignore[attr-defined]
            if not url.startswith(DATA_HOST):
                return
            req = resp.request  # type: ignore[attr-defined]
            parsed = urlparse(url)
            key = f"{req.method} {parsed.path}"
            params = sorted(parse_qs(parsed.query).keys())
            rec = captured.get(key)
            if rec is None:
                rec = {
                    "method": req.method,
                    "path": parsed.path,
                    "query_params": params,
                    "hit_count": 0,
                    "status": None,
                    "response_shape": None,
                }
                captured[key] = rec
            rec["hit_count"] += 1
            rec["status"] = resp.status  # type: ignore[attr-defined]
            if params:
                rec["query_params"] = sorted(set(rec["query_params"]) | set(params))
            if rec["response_shape"] is None and 200 <= resp.status < 300:  # type: ignore[attr-defined]
                try:
                    body = await resp.json()  # type: ignore[attr-defined]
                    rec["response_shape"] = _shape(body)
                except Exception:
                    pass
            # live feedback
            print(f"  [{resp.status}] {key}  ?{','.join(params)}", flush=True)  # type: ignore[attr-defined]

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        await page.goto("https://fomo.family", wait_until="domcontentloaded")
        print("=" * 70, flush=True)
        print(">>> سجّل دخولك، ثم تنقّل في الموقع لالتقاط المسارات:", flush=True)
        print(">>>   1) افتح صفحة المتصدرين (Leaderboard)", flush=True)
        print(">>>   2) افتح ملف متداول ومرّر نشاطه (activity)", flush=True)
        print(">>>   3) افتح التنبيهات/الإشعارات (Alerts)", flush=True)
        print(">>> اضغط Ctrl+C هنا عند الانتهاء (يُحفظ تلقائياً كل 5 ثوانٍ).", flush=True)
        print("=" * 70, flush=True)

        def _dump() -> None:
            with open(OUT, "w", encoding="utf-8") as f:
                json.dump(
                    {"data_host": DATA_HOST, "endpoints": list(captured.values())},
                    f, indent=2, ensure_ascii=False,
                )

        try:
            while True:
                await asyncio.sleep(5)
                _dump()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            _dump()
            print(f"\n>>> تم حفظ {len(captured)} مسار في {OUT}", flush=True)
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
