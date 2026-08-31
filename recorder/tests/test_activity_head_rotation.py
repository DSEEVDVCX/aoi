"""Credential-rotation guard for the forward tradingActivity worker.

The worker is long-lived; Privy rotates the access_token on disk, so the client
built at boot must not keep the old token forever. The live failure
(2026-08-29): the process Running since 10:48, but the last success was
2026-08-27 and an UnauthorizedError every five minutes.
"""
import run_activity_head as head


class _Client:
    def __init__(self, token: str, *, close_error: bool = False) -> None:
        self.token = token
        self.closed = False
        self.close_error = close_error

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise RuntimeError("close failed")


async def test_unchanged_token_keeps_the_current_client(monkeypatch):
    old = _Client("same")
    monkeypatch.setattr(head, "_load_access_token", lambda: "same")
    monkeypatch.setattr(head, "_build_client", lambda token: (_ for _ in ()).throw(
        AssertionError("no new client may be built while the token is unchanged")
    ))

    client, token = await head._maybe_rotate_client(old, "same")

    assert client is old
    assert token == "same"
    assert old.closed is False


async def test_changed_token_rebuilds_and_closes_the_old_client(monkeypatch):
    old = _Client("old")
    fresh = _Client("new")
    monkeypatch.setattr(head, "_load_access_token", lambda: "new")
    monkeypatch.setattr(head, "_build_client", lambda token: fresh)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is fresh
    assert token == "new"
    assert old.closed is True


async def test_changed_token_rebuilds_even_if_old_client_close_fails(monkeypatch):
    """Closing the old transport is only cleanup; it must not waste a fresh client that was built successfully."""
    old = _Client("old", close_error=True)
    fresh = _Client("new")
    monkeypatch.setattr(head, "_load_access_token", lambda: "new")
    monkeypatch.setattr(head, "_build_client", lambda token: fresh)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is fresh
    assert token == "new"
    assert old.closed is True


async def test_token_read_error_does_not_destroy_a_working_client(monkeypatch):
    """A transient read failure must not block the cycle while the current client is still valid."""
    old = _Client("old")

    def fail_load():
        raise OSError("sharing violation")

    monkeypatch.setattr(head, "_load_access_token", fail_load)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is old
    assert token == "old"
    assert old.closed is False


async def test_missing_token_does_not_destroy_a_working_client(monkeypatch):
    """The atomic file write may be observed mid-rename; a transient absence must not throw away the current client."""
    old = _Client("old")
    monkeypatch.setattr(head, "_load_access_token", lambda: None)

    client, token = await head._maybe_rotate_client(old, "old")

    assert client is old
    assert token == "old"
    assert old.closed is False
