"""A separation experiment: is the block on the account, or on something else?

It opens a real Chrome window on fomo.family with a profile dedicated to the
experiment (not the current account's cookies, and the session survives
between runs), you sign in **with a different account**, and it then hits the
same four paths that were hit with the blocked account and prints the status
code explicitly for each.

The logic that makes the experiment decisive: the IP, the machine, the
location, and the fingerprint are all constant — the only variable is the
identity. If the new account answers 200, the block is on the first account
alone; if it answers 403 too, the block is on something that doesn't change
when the account changes.

And this script never writes to `.privy_state.json`: the working account
stays as it is, and the experiment is pure reading. It doesn't print the
token either — the DID fingerprint alone, so it's known that the account
changed.

    py check_account_block.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import sys

# The Arabic Windows terminal cp1256 doesn't know U+2192, so it raised
# UnicodeEncodeError **after** capturing the token and killed the experiment
# at the first result line. The encoding is explicit here, and
# `errors="replace"` keeps any other character from killing a measurement
# after it completed.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

os.environ["FOMO_API_UPSTREAM_IMPERSONATE"] = "true"
os.environ["FOMO_API_DEV"] = "false"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

APP = "https://fomo.family"
BASE = "https://prod-api.fomo.family"
LOGIN_TIMEOUT = 600

# Privy's anonymous session identity — measured identical in two consecutive
# runs (2026-08-20T00:11Z and 00:14Z), so it's an application constant, not a
# user. And the project's docs said `privy:refresh_token` is written only
# after a successful sign-in (confirmed 2026-07-25) — that's void now: the
# anonymous session writes it too. So the distinction is by identity, not by
# the key's presence.
ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

# A persistent browser profile: manual sign-in is the most expensive part of
# the experiment, so it isn't wasted if a rerun is needed — the session stays
# in this folder.
PROFILE_DIR = pathlib.Path(__file__).with_name(".chk_profile")

# The same four paths that were measured on the blocked account — the
# comparison is only valid on the very same paths in the very same way.
PROBES: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", "/v2/leaderboard?limit=3", None),
    ("GET", "/proxy/verifiedTokens", None),
    ("POST", "/proxy/trendingTokens", {}),
    ("GET", "/feed", None),
)


def _did_of(token: str) -> str:
    """The identity's fingerprint from the JWT payload — not the token itself (FR-013)."""
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return str(json.loads(base64.urlsafe_b64decode(body)).get("sub") or "?")
    except Exception:  # a fingerprint for display, not for verification
        return "?"


def _blocked_did() -> str:
    """The blocked account's identity from the state file — so we confirm the account really changed."""
    p = pathlib.Path(__file__).with_name(".privy_state.json")
    try:
        return _did_of(json.loads(p.read_text(encoding="utf-8"))["access_token"])
    except Exception:  # the file's absence doesn't fail the experiment
        return "?"


async def _harvest_token(context) -> str | None:
    """Waits for an identity that isn't the anonymous one, and reads **every** page of the context, not one.

    Measured 2026-08-20T00:16Z twice in a row:

    First — the presence of `privy:token` with `privy:refresh_token` fired
    the capture after a few seconds with the `ANON_DID` identity, so the
    browser was closed before anyone signed in. So the condition became a
    specific known identity, not a key's presence.

    And second — after a real human sign-in the probe went completely
    silent: `page.evaluate` started throwing every second because Privy's
    flow moves the page or opens another window, and the measurement had
    been tied to a single page taken at launch. And `continue` came before
    the tick line, so the news cut off at the very failure — meaning the
    diagnosis went blind exactly when it was needed. So now: every page of
    the context is read, the last failure's cause is printed, and the tick
    is never skipped.
    """
    read_state = (
        "() => ({"
        "  token: localStorage.getItem('privy:token'),"
        "  refresh: localStorage.getItem('privy:refresh_token')"
        "})"
    )
    last_err = ""
    for tick in range(LOGIN_TIMEOUT):
        seen: list[str] = []
        for page in list(context.pages):
            url = ""
            try:
                url = page.url or ""
                if "fomo.family" not in url:
                    continue                      # another origin ⇒ another store
                state = await page.evaluate(read_state)
            except Exception as exc:  # the page moves or closes during sign-in
                last_err = f"{type(exc).__name__} @ {url[:40]}"
                continue
            token = (state or {}).get("token")
            if not token or not (state or {}).get("refresh"):
                continue
            token = str(token).strip().strip('"')
            did = _did_of(token)
            seen.append(did)
            if did != ANON_DID:
                print(f">>> human sign-in after {tick}s — identity {did}", flush=True)
                return token
        if tick and tick % 20 == 0:
            state_txt = "anonymous only" if seen else f"no readable store ({last_err or '—'})"
            print(f">>> waiting for sign-in… ({tick}/{LOGIN_TIMEOUT}s) "
                  f"pages={len(context.pages)} {state_txt}", flush=True)
        await asyncio.sleep(1.0)
    return None


async def _probe(token: str) -> list[int | str]:
    """Hits the paths with the same FomoClient headers and returns the raw status codes.

    FomoClient is bypassed here on purpose: it translates codes into
    exceptions with one message for both block and outage, and what the
    experiment needs is the code itself, not its translation.
    """
    from curl_cffi.requests import AsyncSession

    from fomo_api.config import settings

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-supported-chains": settings.upstream_supported_chains,
        "origin": APP,
        "referer": f"{APP}/",
    }
    out: list[int | str] = []
    async with AsyncSession(impersonate="chrome124", headers=headers, timeout=20) as s:
        for method, path, body in PROBES:
            try:
                r = await (
                    s.get(BASE + path) if method == "GET"
                    else s.post(BASE + path, json=body)
                )
                out.append(r.status_code)
                msg = ""
                if r.status_code != 200:
                    msg = "  " + r.text[:90].replace("\n", " ")
                print(f"    {method:4s} {path:28s} -> {r.status_code}{msg}", flush=True)
            except Exception as exc:  # a transport failure is an answer too
                out.append(type(exc).__name__)
                print(f"    {method:4s} {path:28s} -> {type(exc).__name__}: {exc}", flush=True)
    return out


def _verdict(codes: list[int | str], same_account: bool) -> None:
    ok = sum(1 for c in codes if c == 200)
    forbidden = sum(1 for c in codes if c == 403)
    print("\n" + "=" * 70, flush=True)
    if same_account:
        print("!! The account didn't change — this is the blocked account's own identity.", flush=True)
        print("   Rerun and sign in with a different account for the comparison to be valid.", flush=True)
    elif ok == len(codes):
        print(f"Verdict: the new account works ({ok}/{len(codes)} answered 200).", flush=True)
        print("⇒ The block is on the first account alone — a suspended identity, not an IP or a network.", flush=True)
        print("  And the cure: switch the recorder's credential to this account, and lower the request rate", flush=True)
        print("  before starting collection, or the block catches up with the new account as it did the first.", flush=True)
    elif forbidden == len(codes):
        print(f"Verdict: the new account is blocked too ({forbidden}/{len(codes)} answered 403).", flush=True)
        print("⇒ Not the account. The only variable was the identity and the answer stayed 403,", flush=True)
        print("  so the block is on what didn't change: the IP, the location, or the platform as a whole.", flush=True)
        print("  Next check: try another network (a phone, say) with the same account.", flush=True)
    else:
        print(f"Verdict: mixed — 200:{ok} 403:{forbidden} of {len(codes)}.", flush=True)
        print("⇒ A block at the path level, not the account; read the table above path by path.", flush=True)
    print("=" * 70, flush=True)


async def main() -> None:
    from playwright.async_api import async_playwright

    blocked = _blocked_did()
    print("=" * 70, flush=True)
    print(f"Blocked account: {blocked}", flush=True)
    print(">>> A Chrome window will open with a profile dedicated to the experiment. Sign in with a **different** account.",
          flush=True)
    print(">>> (the session stays saved, so no re-login if the experiment needs repeating)",
          flush=True)
    print(f">>> (timeout {LOGIN_TIMEOUT // 60} minutes; the check starts automatically after sign-in)", flush=True)
    print("=" * 70, flush=True)

    async with async_playwright() as pw:
        PROFILE_DIR.mkdir(exist_ok=True)
        launch = dict(
            user_data_dir=str(PROFILE_DIR), headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = await pw.chromium.launch_persistent_context(channel="chrome", **launch)
        except Exception:  # installed Chrome first, then chromium
            context = await pw.chromium.launch_persistent_context(**launch)
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(APP, wait_until="domcontentloaded")
        token = await _harvest_token(context)
        with contextlib.suppress(Exception):   # closing doesn't fail the experiment
            await context.close()

    if not token:
        print(">>> No sign-in token captured within the timeout — the experiment didn't run.", flush=True)
        return

    new_did = _did_of(token)
    print(f"\n>>> Sign-in token captured. Identity: {new_did}", flush=True)
    print(">>> Hitting the same paths with the new account:\n", flush=True)
    codes = await _probe(token)
    _verdict(codes, same_account=(new_did == blocked and new_did != "?"))


if __name__ == "__main__":
    asyncio.run(main())
