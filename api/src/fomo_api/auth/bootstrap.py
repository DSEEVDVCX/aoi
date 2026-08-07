"""Startup bootstrap for fully-unattended operation.

On process start this seeds the SessionStore from the persistent credential file
(and, on first run, from the one-time browser storage dump), performs an
immediate token refresh so the very first request already has a valid access
token, and creates a STABLE consumer key at a fixed path so a long-running
extractor never has to log in or re-issue a key.

After this runs, the background TokenRefresher keeps the access token fresh
every minute with no browser and no human — that is what makes the extraction
"automatic, no intervention" (the user's requirement).
"""
from __future__ import annotations

import logging

from fomo_api.auth.credential_store import CredentialStore, StoredCredentials
from fomo_api.auth.session import SessionStore
from fomo_api.config import settings

logger = logging.getLogger(__name__)

_STABLE_KEY_FILE_SUFFIX = ".consumer_key"


async def bootstrap_session(store: SessionStore) -> str | None:
    """Seed Redis from persisted creds, refresh once, and mint a stable session.

    Returns the stable consumer key (also written next to the state file so the
    operator can read it), or None if no credentials are available yet.
    """
    cred_store = CredentialStore(settings.credential_state_file)
    creds = cred_store.load()

    # First run: no state file yet. Seed it from the one-time browser capture.
    if creds is None and settings.credential_bootstrap_dump:
        seeded = CredentialStore.from_local_storage_dump(settings.credential_bootstrap_dump)
        if seeded and seeded.refresh_token:
            cred_store.save(seeded)
            creds = seeded
            logger.info("Seeded credential state from one-time storage dump")

    if creds is None or not creds.refresh_token:
        logger.warning(
            "No stored Privy credentials found (%s). Auto-extraction is idle until "
            "a login populates them; run the login flow once.",
            cred_store.path,
        )
        return None

    # Persist the refresh creds into Redis so the background refresher can renew.
    await store.store_refresh_creds(
        creds.refresh_token,
        creds.app_id,
        pat=creds.pat,
        client_id=creds.client_id,
        ca_id=creds.ca_id,
    )

    access_token = creds.access_token
    # Refresh immediately if we can, so we never start on a possibly-stale token.
    if creds.is_refreshable():
        refreshed = await _refresh_and_persist(cred_store, creds)
        if refreshed:
            access_token = refreshed

    if not access_token:
        logger.warning("Bootstrap could not obtain an access token; extraction idle.")
        return None

    consumer_key = await _mint_stable_key(store, cred_store, access_token)
    logger.info("Unattended session ready; auto-refresh active.")
    return consumer_key


async def _refresh_and_persist(
    cred_store: CredentialStore, creds: StoredCredentials
) -> str | None:
    """Do one browserless refresh and write the rotated secrets back to disk."""
    from fomo_api.auth.token_refresher import _call_privy_refresh

    if not creds.refresh_token or not creds.app_id:
        return None
    try:
        result = await _call_privy_refresh(
            refresh_token=creds.refresh_token,
            app_id=creds.app_id,
            pat=creds.pat,
            client_id=creds.client_id,
            ca_id=creds.ca_id,
            current_access=creds.access_token,
        )
    except Exception as exc:
        logger.warning("Startup refresh failed (%s); using stored access token", exc)
        return None

    cred_store.save(
        StoredCredentials(
            access_token=result.get("access"),
            refresh_token=result.get("refresh"),
            pat=result.get("pat"),
        )
    )
    logger.info("Startup token refresh OK")
    return result.get("access")


async def _mint_stable_key(
    store: SessionStore, cred_store: CredentialStore, access_token: str
) -> str:
    """Reuse a previously-minted consumer key if it is still valid, else create
    one and record it next to the state file so the extractor can read it."""
    import os

    key_path = cred_store.path + _STABLE_KEY_FILE_SUFFIX
    existing = None
    if os.path.isfile(key_path):
        try:
            with open(key_path, encoding="utf-8") as fh:
                existing = fh.read().strip() or None
        except OSError:
            existing = None

    if existing and await store.verify(existing):
        # Key still live: just refresh its stored token + TTL.
        await store.update_access_tokens(access_token)
        return existing

    consumer_key = await store.create(access_token)
    try:
        with open(key_path, "w", encoding="utf-8") as fh:
            fh.write(consumer_key)
        os.chmod(key_path, 0o600)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("Could not persist stable consumer key: %s", exc)
    return consumer_key
