import config
import run_evm_replay


class _DB:
    def __init__(self, next_network="0"):
        self.meta = {"evm_replay_next_network": next_network}

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = str(value)

    def note_error(self, key, value):
        """نفس عقد `db.note_error`: يكتب ويعيد نجاحاً، ولا يرفع أبداً."""
        self.meta[key] = str(value)
        return True

    def evm_watched(self, _networks):
        return []


class _LockedDB(_DB):
    """قاعدةٌ مقفلة: كل كتابة دفتريّة تفشل بهدوء."""

    def note_error(self, _key, _value):
        return False

    def set_meta(self, key, value):  # noqa: ARG002 — لو نادى أحدٌ الطريق العاري
        raise RuntimeError("database is locked")


def _patch_live_evm(monkeypatch, calls=None):
    calls = calls if calls is not None else []

    async def fake_live(_rpc, _db, _recorded_at):
        calls.append("live")
        return {"evm_errors": 0}

    async def fake_bsc(_rpc, _db, _recorded_at):
        calls.append("bsc")
        return {"bsc_errors": 0}

    async def fake_contract(_rpc, _db, _recorded_at):
        calls.append("contract")
        return {"evm_contract_errors": 0}

    monkeypatch.setattr(run_evm_replay.evm_layer, "run_evm_cycle", fake_live)
    monkeypatch.setattr(run_evm_replay.bsc_layer, "run_bsc_cycle", fake_bsc)
    monkeypatch.setattr(run_evm_replay.evm_contract, "run_evm_contract_cycle", fake_contract)
    return calls


async def test_run_cycle_is_the_single_owner_of_live_evm_work(monkeypatch):
    db = _DB("8453")
    calls = []
    _patch_live_evm(monkeypatch, calls)

    async def fake_replay(*_args, **kwargs):
        calls.append(("replay", kwargs["networks"]))
        return {"tokens": 0, "written": 0, "errors": 0}

    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_replay)

    await run_evm_replay.run_cycle(object(), db, object())

    assert calls == ["live", "bsc", "contract", ("replay", ["8453"])]


async def test_cycle_rotates_replay_networks(monkeypatch):
    db = _DB("8453")
    calls = []
    _patch_live_evm(monkeypatch, calls)

    async def fake_run_replay(_rpc, _db, **kwargs):
        calls.append(kwargs)
        return {"tokens": 1, "written": 3, "errors": 0}

    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("4663", "8453", "143"))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_run_replay)

    stats = await run_evm_replay.run_cycle(object(), db, object())

    assert calls[:3] == ["live", "bsc", "contract"]
    assert calls[3:] == [{
        "networks": ["8453"], "limit": 1, "log": run_evm_replay._log,
        "budget_seconds": config.EVM_REPLAY_BUDGET_SECONDS_PER_CYCLE,
    }]
    assert db.get_meta("evm_replay_next_network") == "143"
    assert stats["tokens"] == 1
    assert stats["written"] == 3
    assert stats["errors"] == 0
    assert stats["network"] == "8453"


async def test_cycle_recovers_when_saved_network_is_no_longer_configured(monkeypatch):
    db = _DB("999")
    calls = []
    _patch_live_evm(monkeypatch, calls)

    async def fake_run_replay(_rpc, _db, **kwargs):
        calls.append(kwargs["networks"])
        return {"tokens": 0, "written": 0, "errors": 0}

    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("8453", "143"))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_run_replay)

    await run_evm_replay.run_cycle(object(), db, object())

    assert calls[:3] == ["live", "bsc", "contract"]
    assert calls[3:] == [["8453"]]
    assert db.get_meta("evm_replay_next_network") == "143"


async def test_cycle_runs_live_backfill_before_historical_replay(monkeypatch):
    db = _DB("8453")
    replay_calls = []
    _patch_live_evm(monkeypatch)

    async def fake_replay(*args, **kwargs):
        replay_calls.append((args, kwargs))
        return {"tokens": 1, "written": 2, "errors": 0}

    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", fake_replay)

    stats = await run_evm_replay.run_cycle(object(), db, object())

    assert replay_calls and replay_calls[0][1]["networks"] == ["8453"]
    assert stats["tokens"] == 1
    assert stats["network"] == "8453"


def test_monad_is_collected_live_but_not_replayed():
    """مونادْ تُجمَع حيّاً ولا تُعاد تاريخيّاً — وهذا فرقٌ مقصود لا سهو.

    الإعادة التاريخيّة تمشي دفتر كل عملة من نشأتها، وسجلّ المراقبة يحمل صفر عملة
    مونادْ نشطة (مقابل 49 روبن‑هود و23 Base و35 BSC) وثلاثاً خاملة. ومع ذلك أخذت
    مونادْ ثلث دورات الإعادة و2,950 نداءً مقابل **صفر** لقطة، تسجّل
    `كتل 38963051→38963050` أي مدًى مقلوباً = لا تقدّم. فالحياة تبقى: عملة جديدة
    قد تظهر غداً وتُجمَع من لحظتها. والإعادة تنتظر أن توجد عملة تستحقّها، ورجوعها
    سطرٌ في `config`.
    """
    assert {"4663", "8453", "143"} <= set(config.EVM_NETWORKS)
    assert config.EVM_RPC_URLS["143"].startswith("https://")
    assert {"4663", "8453"} <= set(config.EVM_REPLAY_NETWORKS)
    assert "143" not in config.EVM_REPLAY_NETWORKS


def test_live_assist_prioritizes_unstarted_then_nearest_completion():
    pending = [
        {"token_address": "far", "backfill_status": "partial",
         "from_block": 100, "to_block": 10_000, "backfill_calls": 10,
         "backfill_last_try_at": "a"},
        {"token_address": "near", "backfill_status": "partial",
         "from_block": 9_900, "to_block": 10_000, "backfill_calls": 1,
         "backfill_last_try_at": "z"},
        {"token_address": "new", "backfill_status": None,
         "from_block": None, "to_block": None, "backfill_last_try_at": None},
    ]

    pending.sort(key=run_evm_replay.evm_layer._assist_priority)

    assert [row["token_address"] for row in pending] == ["new", "near", "far"]


def test_worked_counts_live_assist_stats_not_just_replay_keys():
    """العطبُ الأصليّ: فرعُ المساعدة يعيد `evm_backfill_*` ولا `tokens` أصلاً،
    فشرطُ `tokens or errors` أسكت السجلَّ أربع ساعات والعامل يعمل."""
    assert run_evm_replay._worked({"evm_backfill_due": 3, "evm_backfilled": 1})
    assert run_evm_replay._worked({"tokens": 1})
    assert run_evm_replay._worked({"errors": 1})


def test_worked_ignores_labels_and_timing_so_idle_stays_idle():
    """اسمُ الشبكة والزمنُ حاضران في كل دورة؛ لو عُدّا عملاً لصار السطر دائماً."""
    assert not run_evm_replay._worked({
        "tokens": 0, "written": 0, "errors": 0, "network": "8453",
        "networks": 3, "refused_networks": 1, "seconds": 12.4,
    })
    # `isinstance(True, int)` صحيحٌ في بايثون ⇒ العلمُ المنطقيّ يُستثنى صراحةً.
    assert not run_evm_replay._worked({"skipped_all": True, "tokens": 0})


async def test_cycle_stamps_last_ok_only_when_no_errors(monkeypatch):
    """ختمُ «آخر نجاح» للإعادة من كاتبه — لا من المسجّل (حدُّ تقادم اللوحة)."""
    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    _patch_live_evm(monkeypatch)

    async def clean(_rpc, _db, **_kwargs):
        return {"tokens": 1, "written": 2, "errors": 0}

    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", clean)
    db = _DB("8453")
    await run_evm_replay.run_cycle(object(), db, object())
    assert db.get_meta("evm_replay_last_ok_at")

    async def failing(_rpc, _db, **_kwargs):
        return {"tokens": 1, "written": 0, "errors": 2}

    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", failing)
    fresh = _DB("8453")
    await run_evm_replay.run_cycle(object(), fresh, object())
    assert fresh.get_meta("evm_replay_last_ok_at") is None


async def test_cycle_survives_a_locked_database_at_stamp_time(monkeypatch):
    """القفلُ عند الختم لا يُسقط دورةً كُتبت صفوفُها فعلاً — ولا يكذب «تعثّرت»."""
    monkeypatch.setattr(config, "EVM_REPLAY_NETWORKS", ("8453",))
    monkeypatch.setattr(config, "EVM_REPLAY_TOKENS_PER_CYCLE", 1)
    _patch_live_evm(monkeypatch)

    async def clean(_rpc, _db, **_kwargs):
        return {"tokens": 1, "written": 2, "errors": 0}

    monkeypatch.setattr(run_evm_replay.evm_replay, "run_replay", clean)

    stats = await run_evm_replay.run_cycle(object(), _LockedDB("8453"), object())

    assert stats["tokens"] == 1
    assert stats["written"] == 2
    assert stats["errors"] == 0
    assert stats["network"] == "8453"
