"""File-backed persistence for Privy credentials so the API can auto-renew its
access token across restarts WITHOUT a browser or any manual login.

The background TokenRefresher already renews the access token every minute using
the stored refresh_token + pat (CONFIRMED browserless flow, see
token_refresher.py). The only piece missing for fully unattended operation was
durability: Privy ROTATES both the refresh_token and the pat on every renewal,
so the current values must survive process restarts. Redis alone is not enough
(it may be volatile, or FakeRedis in dev), so we mirror the rotating secrets to
a local JSON state file and reload them on startup.

Security (FR-013): the state file holds live credentials. It is written with
0600 permissions where the OS supports it, never logged, and only its *key
names* are ever printed. Delete the file to fully revoke.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

# localStorage key names as written by fomo.family's Privy SDK (CONFIRMED via
# storage dump 2026-07-25).
_LS_TOKEN = "privy:token"
_LS_REFRESH = "privy:refresh_token"
_LS_PAT = "privy:pat"
_LS_CAID = "privy:caid"
_APP_ID_RE = re.compile(r"^privy:([a-z0-9]{20,}):recent-login-method$", re.IGNORECASE)
_CLIENT_ID_RE = re.compile(r"client-[A-Za-z0-9]{20,}")


@dataclass
class StoredCredentials:
    """The complete set of Privy secrets needed to renew a session headlessly."""

    access_token: str | None = None
    refresh_token: str | None = None
    pat: str | None = None
    app_id: str | None = None
    client_id: str | None = None
    ca_id: str | None = None

    def is_refreshable(self) -> bool:
        """True when we have enough to renew without a browser."""
        return bool(self.refresh_token and self.app_id and self.pat)


def _clean(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip().strip('"')
    return v or None


class CredentialStore:
    """Loads/saves Privy credentials to a JSON file on disk."""

    def __init__(self, path: str) -> None:
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def exists(self) -> bool:
        return os.path.isfile(self._path)

    def load(self) -> StoredCredentials | None:
        """Read stored credentials, or None if the file is absent/unreadable."""
        if not self.exists():
            return None
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning("Could not read credential state file: %s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        return StoredCredentials(
            access_token=_clean(raw.get("access_token")),
            refresh_token=_clean(raw.get("refresh_token")),
            pat=_clean(raw.get("pat")),
            app_id=_clean(raw.get("app_id")),
            client_id=_clean(raw.get("client_id")),
            ca_id=_clean(raw.get("ca_id")),
        )

    def save(self, creds: StoredCredentials) -> None:
        """Persist credentials atomically with restrictive permissions.

        Merges with any existing file so a partial update (e.g. only the rotated
        refresh_token + pat after a renewal) never wipes fields we still need.
        """
        current = self.load() or StoredCredentials()
        merged = asdict(current)
        for key, val in asdict(creds).items():
            if val:  # only overwrite with a real value; keep prior otherwise
                merged[key] = val

        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
        # pragma: no cover - chmod is a no-op/failure on some platforms (Windows)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        logger.info("Persisted rotated Privy credentials (%s)", ", ".join(k for k, v in merged.items() if v))

    @staticmethod
    def from_local_storage_dump(dump_path: str) -> StoredCredentials | None:
        """Extract credentials from a captured `privy_storage_dump.json` (the
        one-time browser capture). Reads only the fields we need; the dump also
        contains unrelated keys we ignore."""
        try:
            with open(dump_path, encoding="utf-8") as fh:
                dump = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.warning("Could not read storage dump: %s", exc)
            return None
        ls = dump.get("_full_localStorage") if isinstance(dump, dict) else None
        if not isinstance(ls, dict):
            return None

        app_id = None
        client_id = None
        for key, val in ls.items():
            m = _APP_ID_RE.match(key)
            if m:
                app_id = m.group(1)
            if client_id is None and isinstance(val, str):
                cm = _CLIENT_ID_RE.search(val)
                if cm:
                    client_id = cm.group(0)

        return StoredCredentials(
            access_token=_clean(ls.get(_LS_TOKEN)),
            refresh_token=_clean(ls.get(_LS_REFRESH)),
            pat=_clean(ls.get(_LS_PAT)),
            app_id=app_id,
            client_id=client_id,
            ca_id=_clean(ls.get(_LS_CAID)),
        )
