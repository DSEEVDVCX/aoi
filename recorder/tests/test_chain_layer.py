"""Tests for the chain layer: the extractor, the cycle, and key redaction (no network)."""
import os
from datetime import datetime, timedelta

import chain_layer
import config
import extract
import httpx
import pytest
import solana_rpc
from db import RecorderDB, decode_raw

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-08-13T12:00:00+00:00"
SOL = config.SOLANA_NETWORK_ID


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _watch(db, token, *, control=False, network=SOL):
    db.upsert_watch(token, network, "large_buy", f"sig-{token}", 48, NOW)
    if control:
        db._conn.execute(
            "UPDATE watchlist SET is_control=1, entry_signal_id=NULL WHERE token_address=?",
            (token,),
        )
        db._conn.commit()


def _envelope(amounts, supply, decimals=6, ui_supply=None, ui_amounts=None):
    """Mimics a `getTokenSupply` + `getTokenLargestAccounts` batch.

    `ui_supply`/`ui_amounts` are deliberately passed **wrong** in some tests:
    the measurement must come from the correct `amount`, not from the
    ready-made decimal.
    """
    unit = 10 ** decimals
    return {
        "supply": {
            "context": {"slot": 400},
            "value": {
                "amount": str(supply),
                "decimals": decimals,
                "uiAmount": ui_supply if ui_supply is not None else supply / unit,
                "uiAmountString": str(supply / unit),
            },
        },
        "largest": {
            "context": {"slot": 400},
            "value": [
                {
                    "address": f"acc{i}",
                    "amount": str(a),
                    "decimals": decimals,
                    "uiAmount": (
                        ui_amounts[i] if ui_amounts is not None else a / unit
                    ),
                }
                for i, a in enumerate(amounts)
            ],
        },
    }


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------
def test_extract_computes_four_tiers():
    """20 equal accounts at 2% each ⇒ 2/10/20/40%."""
    amounts = [2_000_000] * 20                      # each 2% of 100 million
    row = extract.extract_chain_concentration(
        _envelope(amounts, 100_000_000), "tok", SOL, NOW, NOW, "sig-1",
    )
    assert row is not None
    assert row["top1_pct"] == pytest.approx(2.0)
    assert row["top5_pct"] == pytest.approx(10.0)
    assert row["top10_pct"] == pytest.approx(20.0)
    assert row["top20_pct"] == pytest.approx(40.0)
    assert row["top_accounts"] == 20
    assert row["supply"] == pytest.approx(100.0)     # 6 decimal places
    assert row["decimals"] == 6
    assert row["is_control"] == 0
    assert row["entry_signal_id"] == "sig-1"


def test_extract_sorts_descending_before_slicing():
    """Ordering is the whole meaning of "largest one" — a scattered reply must not break top1."""
    row = extract.extract_chain_concentration(
        _envelope([100, 900, 300, 200], 2_000), "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(45.0)    # 900 of 2000
    assert row["top5_pct"] == pytest.approx(75.0)    # all four = 1500


def test_extract_top_accounts_exposes_short_lists():
    """A token with three holders: top20 = the sum of all of them, not "largest
    20" — the column exposes that."""
    row = extract.extract_chain_concentration(
        _envelope([500, 300, 200], 1_000), "tok", SOL, NOW, NOW, None,
    )
    assert row["top_accounts"] == 3
    assert row["top1_pct"] == pytest.approx(50.0)
    assert row["top5_pct"] == pytest.approx(100.0)
    assert row["top10_pct"] == pytest.approx(100.0)
    assert row["top20_pct"] == pytest.approx(100.0)


def test_extract_uses_base_units_not_ui_amount():
    """The measurement comes from the correct `amount`, not from `uiAmount` —
    here the decimal is planted wrong."""
    row = extract.extract_chain_concentration(
        _envelope(
            [400, 100], 1_000,
            ui_supply=999999.0,                     # a completely wrong supply decimal
            ui_amounts=[0.000001, 0.000001],        # and wrong amount decimals
        ),
        "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(40.0)
    assert row["top5_pct"] == pytest.approx(50.0)
    assert row["supply"] == pytest.approx(0.001)     # 1000 ÷ 10^6, not 999999


def test_extract_keeps_precision_at_eighteen_decimals():
    """An 18-decimal supply loses digits as a float; integers lose nothing."""
    supply = 10 ** 27 + 1                            # a number no float can represent
    row = extract.extract_chain_concentration(
        _envelope([supply], supply, decimals=18), "tok", SOL, NOW, NOW, None,
    )
    assert row["top1_pct"] == pytest.approx(100.0)


def test_extract_rejects_envelope_without_measurement():
    assert extract.extract_chain_concentration(
        None, "tok", SOL, NOW, NOW, None
    ) is None
    assert extract.extract_chain_concentration(
        {}, "tok", SOL, NOW, NOW, None
    ) is None
    empty = {"supply": {"value": None}, "largest": {"value": []}}
    assert extract.extract_chain_concentration(
        empty, "tok", SOL, NOW, NOW, None
    ) is None


def test_extract_zero_supply_keeps_row_with_null_ratios():
    """A full burn: zero is a **measurement**, so the row stays and the ratios
    stay NULL (no division by zero)."""
    row = extract.extract_chain_concentration(
        _envelope([0], 0), "tok", SOL, NOW, NOW, None,
    )
    assert row is not None
    assert row["supply"] == 0.0
    assert row["top1_pct"] is None
    assert row["top20_pct"] is None
    assert row["top_accounts"] == 1


def test_extract_missing_accounts_leaves_ratios_null_but_keeps_supply():
    row = extract.extract_chain_concentration(
        _envelope([], 1_000_000), "tok", SOL, NOW, NOW, None,
    )
    assert row is not None
    assert row["top_accounts"] == 0
    assert row["top1_pct"] is None
    assert row["supply"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Key redaction (FR-013)
# ---------------------------------------------------------------------------
def test_redact_removes_key_from_any_message():
    key = "778a585b-4825-4df1-80c1-964d45b475cf"
    msg = f"ConnectError: https://mainnet.helius-rpc.com/?api-key={key} failed"
    out = solana_rpc._redact(msg, key)
    assert key not in out
    assert "api-key=<redacted>" in out


def test_redact_scrubs_url_even_when_key_rotated():
    """If the key was rotated on disk between the call and the exception, the
    URL text no longer matches."""
    out = solana_rpc._redact("HTTP 401: .../?api-key=OLDVALUE&x=1", "NEWVALUE")
    assert "OLDVALUE" not in out
    assert "&x=1" in out                              # redaction does not swallow the rest of the text


def test_read_keys_prefers_environment(monkeypatch):
    """The environment beats the file, and the value is returned in a **list**
    (the pool does not accept a scalar)."""
    monkeypatch.setenv("HELIUS_API_KEY", "env-secret")
    monkeypatch.setattr(config, "chain_keys_path", lambda: "missing.json")
    assert solana_rpc._read_keys() == ["env-secret"]


def test_read_keys_splits_multiple_environment_keys(monkeypatch):
    """Two comma-separated keys in the environment ⇒ a pool with two keys, not
    one odd string."""
    monkeypatch.setenv("HELIUS_API_KEY", "one, two ,one")
    monkeypatch.setattr(config, "chain_keys_path", lambda: "missing.json")
    assert solana_rpc._read_keys() == ["one", "two"]   # with the duplicate dropped


async def test_helius_rotates_to_second_key_on_retryable_rpc_error(monkeypatch):
    monkeypatch.setattr(solana_rpc, "_read_keys", lambda: ["bad-key", "good-key"])
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "bad-key" in str(request.url):
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32603, "message": "account index service overloaded"},
            })
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = solana_rpc.SolanaRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await rpc._post({"jsonrpc": "2.0", "id": 1, "method": "test"})
    finally:
        await rpc.aclose()

    assert result["result"] == "ok"
    assert len(seen) == 2
    assert "bad-key" in seen[0] and "good-key" in seen[1]


# ---------------------------------------------------------------------------
# A transient fault ≠ key rejection: retry with the same key, no cooldown
# (measured 2026-08-17)
# ---------------------------------------------------------------------------
async def test_helius_retries_transient_522_with_a_single_key(monkeypatch):
    """522 is Cloudflare timing out to the origin: it used to be a fatal error
    costing 15 minutes of staleness."""
    monkeypatch.setattr(solana_rpc, "_read_keys", lambda: ["only-key"])
    monkeypatch.setattr(config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(522, text="error code: 522")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = solana_rpc.SolanaRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await rpc._post({"jsonrpc": "2.0", "id": 1, "method": "test"})
    finally:
        await rpc.aclose()

    assert result["result"] == "ok"
    assert len(seen) == 2
    # The key is fine ⇒ it is reused, with no cooldown period robbing us of it
    # for a minute.
    assert all("only-key" in url for url in seen)
    assert rpc._keys._blocked_until == {}


async def test_helius_retries_read_timeout_with_a_single_key(monkeypatch):
    monkeypatch.setattr(solana_rpc, "_read_keys", lambda: ["only-key"])
    monkeypatch.setattr(config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = solana_rpc.SolanaRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await rpc._post({"jsonrpc": "2.0", "id": 1, "method": "test"})
    finally:
        await rpc.aclose()

    assert result["result"] == "ok"
    assert len(calls) == 2


async def test_helius_deprioritized_retries_when_there_is_no_second_key(monkeypatch):
    """"Slow down requests" is a load signal: with one key it used to be a
    single attempt with no backoff."""
    monkeypatch.setattr(solana_rpc, "_read_keys", lambda: ["only-key"])
    monkeypatch.setattr(config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32600,
                "message": "Request deprioritized due to number of accounts requested",
            }})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    rpc = solana_rpc.SolanaRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await rpc._post({"jsonrpc": "2.0", "id": 1, "method": "test"})
    finally:
        await rpc.aclose()

    assert result["result"] == "ok"
    assert len(calls) == 2


async def test_helius_transient_failure_message_still_hides_the_key(monkeypatch):
    """Retrying does not weaken FR-013: the ReadTimeout text carries the full URL."""
    monkeypatch.setattr(solana_rpc, "_read_keys", lambda: ["s3cret-key"])
    monkeypatch.setattr(config, "CHAIN_TRANSIENT_BACKOFF_SECONDS", 0)

    def handler(request):
        raise httpx.ReadTimeout(f"timed out for {request.url}", request=request)

    rpc = solana_rpc.SolanaRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(solana_rpc.ChainRPCError) as err:
            await rpc._post({"jsonrpc": "2.0", "id": 1, "method": "test"})
    finally:
        await rpc.aclose()

    assert "s3cret-key" not in str(err.value)


async def test_concentration_rejects_materially_different_slots(monkeypatch):
    rpc = solana_rpc.SolanaRPC.__new__(solana_rpc.SolanaRPC)

    async def _post(_payload):
        supply = _envelope([1], 10)["supply"]
        largest = _envelope([1], 10)["largest"]
        largest["context"]["slot"] = 400 + config.CHAIN_MAX_SLOT_LAG + 1
        return [
            {"id": "supply", "result": supply},
            {"id": "largest", "result": largest},
        ]

    rpc._post = _post
    with pytest.raises(solana_rpc.ChainRPCError, match="not synchronized"):
        await rpc.fetch_concentration_raw("mint")


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
class _RPC:
    """Fake chain client. `replies` are dicts; `fail`/`key_missing` are exceptions."""

    def __init__(self, replies=None, fail=None, key_missing=False):
        self.calls = []
        self._replies = replies or {}
        self._fail = fail or set()
        self._key_missing = key_missing

    async def fetch_concentration_raw(self, mint):
        self.calls.append(mint)
        if self._key_missing:
            raise solana_rpc.ChainKeyMissing("the chain key file is missing")
        if mint in self._fail:
            raise solana_rpc.ChainRPCError("getTokenSupply: JSON-RPC -32603: boom")
        return self._replies.get(mint, _envelope([], 0))


async def _noop(_seconds):
    pass


async def test_cycle_writes_row_and_marks_state_ok(db):
    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([3_221_000, 1_000_000], 10_000_000)})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats == {
        "chain_due": 1, "chain_rows": 1, "chain_empty": 0,
        "chain_unsupported": 0, "chain_errors": 0,
    }
    row = db._conn.execute(
        "SELECT token_address, network_id, top1_pct, top20_pct, top_accounts "
        "FROM chain_concentration"
    ).fetchone()
    assert row["token_address"] == "mint1"
    assert row["network_id"] == SOL
    assert row["top1_pct"] == pytest.approx(32.21)
    assert row["top20_pct"] == pytest.approx(42.21)
    assert row["top_accounts"] == 2
    state = db._conn.execute(
        "SELECT last_status, top1_pct, attempts FROM chain_fetch_state"
    ).fetchone()
    assert state["last_status"] == "ok"
    assert state["top1_pct"] == pytest.approx(32.21)
    assert state["attempts"] == 1


async def test_cycle_skips_non_solana_networks(db):
    """EVM is never asked at all: the ERC-20 standard has no on-chain holders list."""
    _watch(db, "bsc", network="56")
    _watch(db, "robinhood", network="4663")
    _watch(db, "sol", network=SOL)
    rpc = _RPC(replies={"sol": _envelope([1], 10)})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == ["sol"]
    assert stats["chain_due"] == 1
    # And no state row for EVM: we do not mark a token failed that we never asked.
    assert db._conn.execute("SELECT COUNT(*) FROM chain_fetch_state").fetchone()[0] == 1


async def test_cycle_failure_marks_error_and_records_meta(db):
    _watch(db, "boom")
    rpc = _RPC(fail={"boom"})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["chain_errors"] == 1
    assert stats["chain_rows"] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM chain_concentration").fetchone()[0] == 0
    state = db._conn.execute("SELECT last_status FROM chain_fetch_state").fetchone()
    assert state["last_status"] == "error"
    err = db.get_meta("last_error_chain")
    assert err is not None and "boom" in err


async def test_cycle_marks_known_unsupported_mint_without_rpc_or_error(db, monkeypatch):
    token = "So11111111111111111111111111111111111111112"
    _watch(db, token)
    monkeypatch.setattr(config, "CHAIN_UNSUPPORTED_TOKENS", frozenset({token.lower()}))
    rpc = _RPC()

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == []
    assert stats["chain_errors"] == 0
    assert stats["chain_unsupported"] == 1
    state = db._conn.execute(
        "SELECT last_status FROM chain_fetch_state WHERE token_address=?", (token,)
    ).fetchone()
    assert state["last_status"] == "unsupported"

    later = (datetime.fromisoformat(NOW) + timedelta(days=1)).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, later, sleep=_noop)
    assert rpc.calls == []


async def test_cycle_empty_reply_is_not_an_error(db):
    """A reply without a measurement ≠ failure: `empty` reschedules at the
    normal pace, not every two minutes."""
    _watch(db, "nothing")
    rpc = _RPC(replies={"nothing": {"supply": {"value": None}, "largest": {"value": []}}})

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert stats["chain_empty"] == 1
    assert stats["chain_errors"] == 0
    state = db._conn.execute("SELECT last_status FROM chain_fetch_state").fetchone()
    assert state["last_status"] == "empty"


async def test_missing_key_propagates_without_marking_tokens(db):
    """A setup defect, not a token defect: the queue is not marked `error`
    because a file is missing."""
    _watch(db, "mint1")
    rpc = _RPC(key_missing=True)

    with pytest.raises(solana_rpc.ChainKeyMissing):
        await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert db._conn.execute("SELECT COUNT(*) FROM chain_fetch_state").fetchone()[0] == 0


async def test_cycle_archives_raw_envelope(db):
    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([7, 3], 100)})

    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    raw = decode_raw(
        db._conn.execute("SELECT raw_json FROM chain_concentration").fetchone()["raw_json"]
    )
    assert raw["supply"]["value"]["amount"] == "100"
    assert raw["largest"]["value"][0]["address"] == "acc0"


async def test_refresh_and_error_retry_windows(db):
    from datetime import datetime, timedelta

    _watch(db, "mint1")
    rpc = _RPC(replies={"mint1": _envelope([1], 10)})
    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.calls) == 1

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, soon, sleep=_noop)
    assert len(rpc.calls) == 1                      # still fresh

    later = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_REFRESH_SECONDS + 1)
    ).isoformat()
    await chain_layer.run_chain_cycle(rpc, db, later, sleep=_noop)
    assert len(rpc.calls) == 2


async def test_errored_token_retries_faster_than_refresh(db):
    from datetime import datetime, timedelta

    _watch(db, "boom")
    rpc = _RPC(fail={"boom"})
    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)
    assert len(rpc.calls) == 1

    retry = (
        datetime.fromisoformat(NOW)
        + timedelta(seconds=config.CHAIN_ERROR_RETRY_SECONDS + 1)
    ).isoformat()
    assert config.CHAIN_ERROR_RETRY_SECONDS < config.CHAIN_REFRESH_SECONDS
    await chain_layer.run_chain_cycle(rpc, db, retry, sleep=_noop)
    assert len(rpc.calls) == 2                      # before the freshness window


async def test_signal_token_precedes_control_when_capped(db, monkeypatch):
    _watch(db, "control", control=True)
    _watch(db, "signal")
    monkeypatch.setattr(config, "CHAIN_PER_CYCLE", 1)
    rpc = _RPC()

    await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == ["signal"]


async def test_empty_networks_tuple_fetches_nothing(db, monkeypatch):
    """An empty networks list means "nothing", not "everything" — silence is
    more honest than a blind sweep."""
    _watch(db, "mint1")
    monkeypatch.setattr(config, "CHAIN_NETWORKS", ())
    rpc = _RPC()

    stats = await chain_layer.run_chain_cycle(rpc, db, NOW, sleep=_noop)

    assert rpc.calls == []
    assert stats["chain_due"] == 0


# --- "Last success" stamps per queue (the staleness threshold in the dashboard) ---
def test_ok_stamps_are_written_per_queue_not_once_for_the_process():
    """The dashboard used to clear chain/evm errors by the **recorder's** stamp,
    which nothing else writes; when the recorder died the threshold froze and
    the badges stayed red. Now each queue gets its stamp from its own writer."""
    import run_chain

    stats = {"chain_errors": 0, "auth_errors": 0}

    assert run_chain._ok_stamps(stats, NOW) == {
        "chain_last_ok_at": NOW, "chain_auth_last_ok_at": NOW,
    }


def test_a_failing_queue_gets_no_stamp_while_its_neighbours_do():
    """One queue succeeding does not heal another's error: the failing one's
    stamp is withheld alone."""
    import run_chain

    out = run_chain._ok_stamps({
        "chain_errors": 2, "auth_errors": 0, "evm_errors": 0,
        "evm_backfill_errors": 0,
    }, NOW)

    assert "chain_last_ok_at" not in out
    assert out["chain_auth_last_ok_at"] == NOW
    assert "evm_last_ok_at" not in out


def test_absent_counter_yields_no_stamp():
    """A cycle that ran no queue (an exception cut it short) does not stamp a
    false success for it."""
    import run_chain

    assert run_chain._ok_stamps({}, NOW) == {}


def test_stamps_survive_a_locked_database():
    """A database lock at stamping does not raise: the cycle really did
    succeed and is not recorded as "stalled"."""
    import run_chain

    class _Locked:
        def note_error(self, _key, _value):
            return False

    run_chain._stamp(_Locked(), {"chain_last_run_at": NOW})   # no exception
