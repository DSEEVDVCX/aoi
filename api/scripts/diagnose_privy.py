from __future__ import annotations

import asyncio
import json
import re

URL = "https://fomo.family"
DATA_HOST = "https://prod-api.fomo.family"
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
OUT = "privy_storage_dump.json"


def looks_like_jwt(v: object) -> bool:
    return isinstance(v, str) and bool(JWT_RE.match(v.strip().strip('"')))


async def main() -> None:
    from playwright.async_api import async_playwright

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
        page = await context.new_page()

        bearers: list[str] = []

        def on_request(req: object) -> None:
            if req.url.startswith(DATA_HOST):  # type: ignore[attr-defined]
                auth = req.headers.get("authorization")  # type: ignore[attr-defined]
                if auth and auth.lower().startswith("bearer "):
                    bearers.append(auth.split(" ", 1)[1].strip())

        page.on("request", on_request)
        await page.goto(URL, wait_until="domcontentloaded")
        print(">>> افتح النافذة وسجّل دخولك الآن. بانتظار اكتشاف جلسة مصادَق عليها (حتى 5 دقائق)...", flush=True)

        _LS_SCRIPT = (
            "() => { const o={}; for(let i=0;i<localStorage.length;i++)"
            "{const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }"
        )

        async def read_ls() -> dict:
            """Read localStorage, tolerating in-flight navigations."""
            for _ in range(5):
                try:
                    return await page.evaluate(_LS_SCRIPT)
                except Exception:
                    await asyncio.sleep(1)
            return {}

        found = False
        for _ in range(150):  # 150 * 2s = 5 min
            ls = await read_ls()
            if any("privy" in k.lower() and looks_like_jwt(v) for k, v in ls.items()):
                found = True
                break
            await asyncio.sleep(2)

        await asyncio.sleep(3)  # let SDK persist refresh token
        ls = await read_ls()
        cookies = await context.cookies()

        summary = {
            "authenticated_session_detected": found,
            "bearer_requests_seen": len(bearers),
            "localStorage_keys": {
                k: {"len": len(v or ""), "is_jwt": looks_like_jwt(v)} for k, v in ls.items()
            },
            "cookie_names": [
                {"name": c["name"], "domain": c["domain"], "httpOnly": c.get("httpOnly")}
                for c in cookies
            ],
        }
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "_full_localStorage": ls, "_cookies": cookies}, f, indent=2, default=str)

        print(">>> تم الحفظ في", OUT, flush=True)
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
