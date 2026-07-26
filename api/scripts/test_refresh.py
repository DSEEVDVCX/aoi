"""Confirm Privy's session-refresh endpoint using the values already captured
in privy_storage_dump.json. Prints only status + response KEY NAMES (never the
secret token values). Run once: python scripts\\test_refresh.py"""
from __future__ import annotations

import json
import re
import sys

import httpx

DUMP = "privy_storage_dump.json"
APPID_RE = re.compile(r"^privy:([a-z0-9]{20,}):", re.I)


def load() -> tuple[str, str]:
    with open(DUMP, encoding="utf-8") as f:
        data = json.load(f)
    ls = data.get("_full_localStorage", {})
    rt = ls.get("privy:refresh_token")
    if not rt:
        sys.exit("no privy:refresh_token in dump")
    app_id = None
    for k in ls:
        m = APPID_RE.match(k)
        if m:
            app_id = m.group(1)
            break
    if not app_id:
        sys.exit("could not derive app_id from localStorage keys")
    return rt.strip().strip('"'), app_id


def try_call(method: str, url: str, rt: str, app_id: str) -> None:
    headers = {
        "privy-app-id": app_id,
        "content-type": "application/json",
        "origin": "https://fomo.family",
        "referer": "https://fomo.family/",
    }
    try:
        resp = httpx.request(method, url, json={"refresh_token": rt}, headers=headers, timeout=15.0)
    except Exception as exc:
        print(f"  {method} {url} -> ERROR {exc}")
        return
    print(f"  {method} {url} -> HTTP {resp.status_code}")
    if resp.status_code == 200:
        try:
            body = resp.json()
            print(f"    response keys: {sorted(body.keys())}")
        except Exception:
            print(f"    (non-JSON body, {len(resp.text)} chars)")
    else:
        print(f"    body[:200]: {resp.text[:200]}")


def main() -> None:
    rt, app_id = load()
    print(f"app_id = {app_id}")
    print(f"refresh_token length = {len(rt)}")
    print("Testing candidate endpoints:")
    for method, url in [
        ("POST", "https://auth.privy.io/api/v1/sessions"),
        ("PATCH", "https://auth.privy.io/api/v1/sessions"),
        ("POST", "https://auth.privy.io/api/v1/sessions/refresh"),
    ]:
        try_call(method, url, rt, app_id)


if __name__ == "__main__":
    main()
