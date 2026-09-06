"""Switching the fomo account from the dashboard, and finding out whether it is blocked.

This is the dashboard's **second** write path after `keystore.py`, and its
limits are the same: the database stays `mode=ro` with not one write line, and
the target is a single file, `api/.privy_state.json`.

And since what's written here is more dangerous than what's in the key store —
it is the account's whole identity, not a provider key replaced with one
click — three extra constraints were added that don't exist there:

1. **A backup before every write.** The old file is copied to
   `.privy_state.json.bak-*` before being touched, and restoring is one
   button. Breaking a working account with a bad paste would have meant
   stopping all collection with no way back.
2. **The identity is read from the token, not from the user.** No name field
   typed by hand: the `did:privy:…` fingerprint is extracted from the JWT
   payload, so nobody mislabels what they switched.
3. **Rejecting the anonymous session.** Privy writes `privy:token` for an
   anonymous session when the SDK boots before any sign-in (measured
   2026-08-20; the `privy_login.py` doc says otherwise and its claim is
   void). So pasting a store before signing in would have written an identity
   that owns nothing, and the user would read "done" and then find collection
   dead.

And the value is never returned: what leaves here is the identity
fingerprint, the last four characters of the token for telling it apart, the
expiry time, and the probe's status codes. No value is ever logged or echoed
in an error message.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import UTC, datetime
from typing import Any

import config

# The six fields `CredentialStore` reads. `access_token`, `refresh_token` and
# `pat` are secrets; the remaining three are app identifiers with no secret in them.
_SECRET_FIELDS = ("access_token", "refresh_token", "pat")
_PLAIN_FIELDS = ("app_id", "client_id", "ca_id")
_FIELDS = _SECRET_FIELDS + _PLAIN_FIELDS

# And a missing field is not one degree: these four alone have no substitute,
# while the other two do — `client_id` is a fixed app identifier that
# `api/config.py` fills in by default when it isn't captured, and `ca_id`
# isn't even sent if absent (`if ca_id`).
# The verdict used to say "missing" in red for any of them, so on 2026-08-20 it
# lit up the dashboard on a system that renewed its token every minute with no
# `client_id` in the file: a false alarm that teaches the user to ignore red,
# which is worse than having no verdict at all.
_REQUIRED_FIELDS = ("access_token", "refresh_token", "pat", "app_id")

# The localStorage keys Privy writes — the same names as `credential_store.py`.
_LS_TOKEN = "privy:token"
_LS_REFRESH = "privy:refresh_token"
_LS_PAT = "privy:pat"
# **The real name is `privy:caid`, not `privy:ca_id`** — measured on a live
# store 2026-08-20: Privy's seven keys in it include `privy:caid`, so the
# first constant never matched anything, and `ca_id` was silently inherited
# from the old file on every switch: the switch was writing a previous
# account's identifier along with a new account's token. Both names are listed
# because `credential_store` knows the first, and there's no telling which SDK
# version the user has — whichever is found gets used.
_LS_CAID = ("privy:caid", "privy:ca_id")
_APP_ID_RE = re.compile(r"^privy:([a-z0-9]{20,30}):")
_CLIENT_ID_RE = re.compile(r"client-[A-Za-z0-9]{10,}")

# Privy's anonymous session identity — written when the SDK boots before any
# sign-in; measuring it twice in a row (2026-08-20T00:11Z and 00:14Z) gave the
# same constant.
_ANON_DID = "did:privy:cmt0phbhk00080dla83dtghph"

_BAK_PREFIX = ".privy_state.json.bak-"
_BAK_KEEP = 5          # backups kept; anything beyond that is deleted oldest-first

# One lock per modification: a switch is read-then-copy-then-write, and two
# concurrent requests would have tangled.
_LOCK = threading.Lock()

# The last probe's result — in memory, not in the database or the file, like
# `_PROBES` in the key store: a point-in-time check result, and writing it to
# the database would drag it into backups for no reason. It's lost on a
# dashboard restart, and re-checked with one click.
_LAST_PROBE: dict[str, Any] = {}


class AccountError(RuntimeError):
    """A rejected request, with a reason shown to the user as-is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _path() -> str:
    return config.PRIVY_STATE_PATH


def _tail(value: str | None) -> str:
    """The last four characters — for telling two tokens apart; it's all that's shown of the value.

    The same FR-013 exception allowed in `key_file.tail`: computed at request
    time and never written to a log or to meta.
    """
    text = str(value or "")
    return text[-4:] if len(text) >= 8 else ""


def _claims(token: str | None) -> dict[str, Any]:
    """The JWT payload without signature verification — for display, not authorization.

    And it must not be verified: this is another service's token, we don't
    have its key, and the purpose is showing the user the identity and expiry
    time, not allowing anything.
    """
    try:
        body = str(token).split(".")[1]
        body += "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # noqa: BLE001 — an unreadable token ⇒ no claims, no crash
        return {}
    return data if isinstance(data, dict) else {}


def _did(token: str | None) -> str:
    return str(_claims(token).get("sub") or "")


def _expiry(token: str | None) -> dict[str, Any]:
    """The token's expiry time and whether it has expired — the token lives an hour, and the gap helps diagnosis."""
    exp = _claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return {"expires_at": None, "expired": None, "seconds_left": None}
    when = datetime.fromtimestamp(float(exp), UTC)
    left = (when - datetime.now(UTC)).total_seconds()
    return {
        "expires_at": when.isoformat(),
        "expired": left <= 0,
        "seconds_left": int(left),
    }


def _written_seconds_ago() -> int | None:
    """The age of the file's last write in seconds, or None if it doesn't exist.

    And this is the difference between two diagnoses the dashboard used to
    conflate even though they're opposites: an expired token and a file
    untouched for hours ⇒ **nobody is refreshing** (the api server is dead).
    An expired token and a file written every minute ⇒ **the refresh works and
    is being rejected** (the Privy session has ended and won't be extended;
    only a new sign-in saves it). And saying "is the api server running?" in
    the second case sends the user entirely the wrong way — which is what
    happened on 2026-08-20: the server was running, refreshing every 60s, and
    being silently rejected.
    """
    with contextlib.suppress(OSError):
        return max(0, int(time.time() - os.path.getmtime(_path())))
    return None


def _read() -> dict[str, Any]:
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise AccountError(f"failed to read the credential file: {type(exc).__name__}", 500) from exc
    return data if isinstance(data, dict) else {}


def _backups() -> list[dict[str, Any]]:
    """The saved backups, newest first — by fingerprint, not by value."""
    folder = os.path.dirname(_path())
    out: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for name in os.listdir(folder):
            if not name.startswith(_BAK_PREFIX):
                continue
            full = os.path.join(folder, name)
            try:
                with open(full, encoding="utf-8") as fh:
                    raw = json.load(fh)
                did = _did(raw.get("access_token"))
            except (OSError, ValueError):
                did = ""
            out.append({
                "name": name,
                "did": did,
                "saved_at": datetime.fromtimestamp(
                    os.path.getmtime(full), UTC
                ).isoformat(),
            })
    out.sort(key=lambda row: row["saved_at"], reverse=True)
    return out


def _backup_now(reason: str) -> str | None:
    """Copies the current file before touching it, keeping the last `_BAK_KEEP` backups."""
    path = _path()
    if not os.path.exists(path):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^a-z0-9-]+", "", reason.lower())[:12] or "switch"
    name = f"{_BAK_PREFIX}{stamp}-{safe}"
    target = os.path.join(os.path.dirname(path), name)
    try:
        shutil.copy2(path, target)
    except OSError as exc:
        raise AccountError(f"failed to save a backup: {type(exc).__name__}", 500) from exc
    for old in _backups()[_BAK_KEEP:]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(os.path.dirname(path), old["name"]))
    return name


def _write(fields: dict[str, Any]) -> None:
    """An atomic write, in the same style as `key_file.save_entries`.

    A temporary file in the same folder, then `os.replace`: the reader either
    sees the whole old file or the whole new one, never half. And the recorder
    reads this file every cycle, so a half-written file would have stopped it.
    """
    path = _path()
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".privy-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(fields, fh, indent=2)
        with contextlib.suppress(OSError):   # no-op on Windows
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise AccountError(f"failed to write: {type(exc).__name__}", 500) from exc


def _from_local_storage(dump: dict[str, Any]) -> dict[str, Any]:
    """Extracts the six fields from the browser store.

    Accepts two shapes: the raw store `{"privy:token": …}` as the user copies
    it from the console, and the wrapper `{"_full_localStorage": {…}}`, the
    shape `credential_store.from_local_storage_dump` already understands —
    both are accepted because the user can't know which one they have.
    """
    inner = dump.get("_full_localStorage")
    ls = inner if isinstance(inner, dict) else dump
    app_id = client_id = None
    for key, val in ls.items():
        match = _APP_ID_RE.match(str(key))
        if match:
            app_id = match.group(1)
        if client_id is None and isinstance(val, str):
            found = _CLIENT_ID_RE.search(val)
            if found:
                client_id = found.group(0)
    return {
        "access_token": _clean(ls.get(_LS_TOKEN)),
        "refresh_token": _clean(ls.get(_LS_REFRESH)),
        "pat": _clean(ls.get(_LS_PAT)),
        "app_id": app_id,
        "client_id": client_id,
        "ca_id": next((c for c in (_clean(ls.get(k)) for k in _LS_CAID) if c), None),
    }


def _clean(value: Any) -> str | None:
    """Strips the quote marks Privy puts around its localStorage values."""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text or None


def status() -> dict[str, Any]:
    """The current account's status — with no secret values."""
    raw = _read()
    access = raw.get("access_token")
    present = {name: bool(raw.get(name)) for name in _FIELDS}
    return {
        "exists": bool(raw),
        "path": _path(),
        "did": _did(access),
        "token_tail": _tail(access),
        "refreshable": all(bool(raw.get(k)) for k in ("refresh_token", "app_id", "pat")),
        "present": present,
        "missing": [name for name in _FIELDS if not raw.get(name)],
        "missing_required": [name for name in _REQUIRED_FIELDS if not raw.get(name)],
        **_expiry(access),
        "backups": _backups(),
        "last_probe": dict(_LAST_PROBE) or None,
        "anon_did": _ANON_DID,
        "written_seconds_ago": _written_seconds_ago() if raw else None,
    }


def _validate(fields: dict[str, Any], current_access: str | None) -> tuple[str, bool]:
    """Rejects, before writing, anything that would produce a non-working file; returns (fingerprint, is it a refresh).

    And "a refresh" = the same identity with a **newer** token. This used to be
    rejected with 409 "nothing to switch", which was a mistake: when Privy's
    refresh broke on 2026-08-20 (a 200 response with
    `session_update_action=ignore` and no token, and the old one expired), the
    only way out was for the user to sign in again **with the same account**
    and paste their store — and the dashboard refused. Whoever was in the
    right found a closed door.

    And the comparison uses `iat` then `exp`, not the token's text: two
    textually different tokens can be for the same moment, and the older one
    must not overwrite the newer (a paste from an old window left open).
    """
    missing = [name for name in ("access_token", "refresh_token", "pat") if not fields.get(name)]
    if missing:
        raise AccountError(
            "Store is incomplete: did not find " + ", ".join(missing)
            + ". Make sure you copied it from fomo.family after signing in."
        )
    if not fields.get("app_id"):
        raise AccountError(
            "Did not find `privy:<app_id>:…` in the store — copy the whole store, not one line of it."
        )
    did = _did(fields["access_token"])
    if not did:
        raise AccountError("The token is unreadable — not a valid JWT.")
    if did == _ANON_DID:
        raise AccountError(
            "This is an anonymous session, not an account: Privy writes it before "
            "sign-in. Sign in on the page first, then copy the store."
        )
    if did != _did(current_access):
        return did, False
    if not _is_newer(fields["access_token"], current_access):
        raise AccountError(
            "This is the same identity as the current account and its token is not "
            "newer — nothing to switch. To renew a stuck session, sign in again and "
            "then paste the store.",
            409,
        )
    return did, True


def _is_newer(candidate: str | None, current: str | None) -> bool:
    """Is it a newer token for the same account? By `iat`, else by `exp`, and nothing else."""
    if not current:
        return True
    new_claims, old_claims = _claims(candidate), _claims(current)
    for claim in ("iat", "exp"):
        fresh, stale = new_claims.get(claim), old_claims.get(claim)
        if (
            isinstance(fresh, (int, float))
            and isinstance(stale, (int, float))
            and fresh != stale
        ):
            return fresh > stale
    return False


def switch(dump: dict[str, Any]) -> dict[str, Any]:
    """Switches the account from a pasted browser store, after a backup.

    The merge is deliberate: fields absent from the paste stay from the old
    file — `client_id`, for instance, may not appear in every store. But the
    three secrets are required present by `_validate` before that, so no file
    goes out mixing one account's token with another's update.
    """
    if not isinstance(dump, dict) or not dump:
        raise AccountError("No store received — paste the full localStorage content.")
    fields = _from_local_storage(dump)
    with _LOCK:
        current = _read()
        did, is_refresh = _validate(fields, current.get("access_token"))
        backup = _backup_now("refresh" if is_refresh else "switch")
        merged = {name: current.get(name) for name in _FIELDS}
        merged.update({k: v for k, v in fields.items() if v})
        _write(merged)
    _LAST_PROBE.clear()          # the previous account's result doesn't describe the new one
    return {
        "ok": True,
        "did": did,
        "token_tail": _tail(fields["access_token"]),
        "backup": backup,
        "refreshed": is_refresh,
        "message": (
            (
                "The same account's session was renewed with a newer token. "
                if is_refresh
                else "Switched. "
            )
            + "The recorder reads the file every cycle and picks it up within a "
            "minute with no restart, and the api server adopts the identity on "
            "its next refresh."
        ),
    }


_PROBE_PATHS: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", "/v2/leaderboard?limit=3", None),
    ("GET", "/proxy/verifiedTokens", None),
    ("POST", "/proxy/trendingTokens", {}),
)


def _verdict(codes: list[int | None]) -> tuple[str, str]:
    """Separates "the identity is blocked" from "the upstream is struggling" — conflating them is costly.

    Measured 2026-08-19: an identity block returns 403 on every path, and the
    recorder logged it as "unreachable", so the network was hunted for an hour
    while it was fine. So the code is the verdict:

    - 403 on all ⇒ the identity is suspended, and the cure is another
      account, not patience.
    - 401 ⇒ the token is invalid or expired, and the cure is a refresh, not a
      switch.
    - 5xx or no answer ⇒ the upstream itself; the account is left alone.
    """
    live = [c for c in codes if c is not None]
    if not live:
        return "warn", "no response from upstream — not the account"
    if all(c == 200 for c in live):
        return "good", f"account works — {len(live)}/{len(live)} answered 200"
    if all(c == 403 for c in live):
        return "bad", f"blocked — 403 on {len(live)} paths; the identity is suspended"
    if any(c == 401 for c in live):
        return "bad", "token invalid or expired — HTTP 401 (renew, don't switch)"
    if any(c == 403 for c in live):
        forbidden = sum(1 for c in live if c == 403)
        return "bad", f"partial block — 403 on {forbidden} of {len(live)}"
    if all(c >= 500 for c in live):
        return "warn", "upstream struggling — 5xx, not the account"
    counts = ", ".join(f"{c}×{live.count(c)}" for c in sorted(set(live)))
    return "warn", f"mixed — {counts}"


def probe() -> dict[str, Any]:
    """Hits three paths with the current token and returns the raw codes.

    With `curl_cffi`, not `httpx` — the Chrome fingerprint is the condition for
    passing Cloudflare, and an ordinary call would have returned a challenge
    page read as "blocked" for a healthy account.

    And `/feed` is excluded from the probe: it requires `feedTypes`, and
    without them it returns 400, which would be read as failure though it's
    application etiquette, not denial. Three paths are enough for a verdict.
    """
    raw = _read()
    token = raw.get("access_token")
    if not token:
        raise AccountError("No token in the file — nothing to probe.")

    try:
        from curl_cffi.requests import Session
    except ImportError as exc:  # pragma: no cover - the library ships with the api
        raise AccountError("curl_cffi is not installed — probe failed.", 500) from exc

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": config.FOMO_APP_ORIGIN,
        "referer": config.FOMO_APP_ORIGIN + "/",
    }
    rows: list[dict[str, Any]] = []
    codes: list[int | None] = []
    with Session(
        impersonate="chrome124", headers=headers, timeout=config.ACCOUNT_PROBE_TIMEOUT
    ) as session:
        for method, path, body in _PROBE_PATHS:
            url = config.FOMO_UPSTREAM_BASE + path
            try:
                response = (
                    session.get(url) if method == "GET" else session.post(url, json=body)
                )
                code: int | None = response.status_code
                note = ""
            except Exception as exc:  # noqa: BLE001 — a transport failure is an answer too
                code, note = None, type(exc).__name__
            codes.append(code)
            rows.append({"method": method, "path": path, "status": code, "note": note})

    level, detail = _verdict(codes)
    result = {
        "level": level,
        "detail": detail,
        "rows": rows,
        "did": _did(token),
        "at": datetime.now(UTC).isoformat(),
    }
    _LAST_PROBE.clear()
    _LAST_PROBE.update(result)
    return result


def restore(name: str) -> dict[str, Any]:
    """Reverts to a saved backup — because a bad paste silences all collection."""
    safe = os.path.basename(str(name or ""))
    if not safe.startswith(_BAK_PREFIX):
        raise AccountError("Unknown backup name.", 404)
    source = os.path.join(os.path.dirname(_path()), safe)
    if not os.path.exists(source):
        raise AccountError("The backup no longer exists.", 404)
    with _LOCK:
        try:
            with open(source, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise AccountError(f"Backup unreadable: {type(exc).__name__}", 500) from exc
        if not isinstance(raw, dict) or not raw.get("access_token"):
            raise AccountError("The backup has no token — it can't be restored.")
        _backup_now("restore")
        _write({name: raw.get(name) for name in _FIELDS})
    _LAST_PROBE.clear()
    return {
        "ok": True,
        "did": _did(raw.get("access_token")),
        "token_tail": _tail(raw.get("access_token")),
        "message": f"Backup {safe} restored.",
    }
