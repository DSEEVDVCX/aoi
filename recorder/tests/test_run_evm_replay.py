import config
import run_evm_replay


class _DB:
    def __init__(self, next_network="0"):
        self.meta = {"evm_replay_next_network": next_network}

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = str(value)


async def test_cycle_rotates_replay_networks(monkeypatch):
    db = _DB("8453")
    calls = []

    async def fake_run_replay(_rpc, _db, **kwargs):
        calls.append(kwargs)
        return {"tokens": 1, "written": 3, "errors": 0}

    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("4663", "8453", "143"))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_run_replay)

    stats = await run_evm_replay.run_cycle(object(), db)

    assert calls == [{
        "networks": ["8453"], "limit": 1, "log": run_evm_replay._log,
        "budget_seconds": config.EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE,
    }]
    assert db.get_meta("evm_replay_next_network") == "143"
    assert stats == {"tokens": 1, "written": 3, "errors": 0, "network": "8453"}


async def test_cycle_recovers_when_saved_network_is_no_longer_configured(monkeypatch):
    db = _DB("999")
    calls = []

    async def fake_run_replay(_rpc, _db, **kwargs):
        calls.append(kwargs["networks"])
        return {"tokens": 0, "written": 0, "errors": 0}

    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("8453", "143"))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_run_replay)

    await run_evm_replay.run_cycle(object(), db)

    assert calls == [["8453"]]
    assert db.get_meta("evm_replay_next_network") == "143"


def test_base_and_monad_are_enabled_for_live_and_historical_collection():
    assert {"4663", "8453", "143"} <= set(config.EVM_NETWORKS)
    assert {"4663", "8453", "143"} <= set(config.EVM_REPLAY_NETWORKS)
    assert config.EVM_RPC_URLS["143"].startswith("https://")
