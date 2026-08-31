"""Makes the dashboard package importable in tests without installation, and isolates it from live data.

It adds the dashboard/ folder (the parent) to sys.path so that `import dao` and
`import config` work the same way the dashboard's own code imports them (flat
import, not a package) — the same pattern as recorder/tests. Without it the
dashboard's tests failed to even collect when pytest ran from the project root.
"""
import os
import sqlite3
import sys

import pytest

DASHBOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)

import config  # noqa: E402 — after the path is added, or it isn't found


@pytest.fixture(autouse=True)
def isolate_live_state(tmp_path, monkeypatch):
    """Points the database and key-file paths at a temporary one in **every** test.

    `_with_conn` opens `config.DB_PATH` on every request, so any test calling a
    path that reads the database was reading the real `recorder.db`. Locally it
    exists, so the test passed — silently pegged to live data; on the server it
    was absent (excluded from git), so nine key-panel tests fell over with
    `sqlite3.OperationalError: unable to open database file` the first time the
    server actually ran the dashboard's tests.

    And the guard is automatic, not opt-in on purpose: forgetting it here is
    invisible — the test passes. It is more dangerous for the key file than for
    the database: the database is opened read-only, while
    `/api/provider-keys/{add,delete}` writes, and a test that forgot the
    isolation would delete a working key from the live file. (That's why
    `keys_file` stays: this one guarantees isolation, that one seeds content
    and clears probes and the environment.)

    And it holds the `meta` table alone because that's all `dao.provider_keys`
    reads; whoever needs another table builds their own database like `db` in
    `test_app_cache.py` — it's enough to set `config.DB_PATH` after this one to
    override it.
    """
    path = tmp_path / "isolated.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    # absent on purpose: absence is a valid state that must be tolerated, and
    # the silent fallback must not be the real secrets file.
    monkeypatch.setattr(config, "CHAIN_KEYS_PATH", str(tmp_path / "isolated_keys.json"))
    # And the Privy credential file with it, for literally the same reason —
    # stronger, even: `keystore` deletes one key, but `account.switch` writes
    # the whole account identity over `api/.privy_state.json` — and a test that
    # forgot the isolation would have silenced the whole collection on this
    # machine, while the test passed. Absent on purpose here too: absence is a
    # valid state that must be tolerated (not signed in yet).
    monkeypatch.setattr(config, "PRIVY_STATE_PATH", str(tmp_path / "isolated_privy.json"))
    return path
