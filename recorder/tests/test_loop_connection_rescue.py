"""يدُ الإنقاذ في الحلقات الأربع الطويلة — الانحدارُ الذي كلّف 382 دورة.

`db.recover_connection` موجودةٌ منذ 2026-08-19، لكنّها كانت موصولةً بـ
`recorder.py` وحدها. فحين عَلِق اتّصالُ `FomoChain` بلقطةِ قراءةٍ سُبقت في WAL
بقيت كلُّ دورةٍ تنهار عند `set_chain_state` بـ`database is locked` من
2026-08-22T19:26:58Z إلى 2026-08-23T11:13:47Z — **382 دورةً متتالية** — ولم
يُفرج عنها إلّا إعادةُ تشغيل المهمّة عند 10:57Z، لا الحلقة. والقفلُ كان حرّاً
طولَ ذلك: اتّصالٌ جديد أخذ `BEGIN IMMEDIATE` في صفرِ ثانية في 12 من 12 عيّنة،
فالعالقُ لقطةُ قراءتنا لا القاعدة — وهي الحالة التي **لا تنفع فيها المهلة**
لأنّ معالجَ الانتظار لا يُنادى لها (`SQLITE_BUSY_SNAPSHOT`).

الغيابُ لا يظهر في أيّ اختبارٍ آخر: الحلقاتُ الأربع تسجّل الانهيار وتُكمل،
فتبدو سليمةً في كلّ فحصٍ إلّا فحصَ السجلّ بعد ساعات. فيُثبَّت النداءُ نفسه.
"""
import asyncio
import sqlite3
import sys

import config
import db as db_module
import pytest


class _FakeDB:
    """أضعفُ ما يكفي: عدّادُ إنقاذٍ وأختامٌ لا ترفع."""

    def __init__(self) -> None:
        self.recoveries = 0
        self.closed = False
        self.meta: dict[str, str] = {}

    def set_meta(self, key, value) -> None:
        self.meta[key] = value

    def note_error(self, key, value) -> bool:
        self.meta[key] = value
        return True

    def bump_counter(self, key, n=1) -> None:
        pass

    def recover_connection(self) -> str:
        self.recoveries += 1
        return "ok"

    def close(self) -> None:
        self.closed = True


class _FakeRPC:
    def key_stats(self):
        return {}

    async def aclose(self) -> None:
        pass


def _locked(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


async def _locked_async(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


@pytest.fixture()
def fake_db(monkeypatch):
    """قاعدةٌ وهميّة عبر `db.RecorderDB`: الحلقاتُ تستورده **داخل** الدالّة."""
    fake = _FakeDB()
    monkeypatch.setattr(db_module, "RecorderDB", lambda *a, **k: fake)
    return fake


def test_chain_loop_rescues_the_connection_after_every_crash(fake_db, monkeypatch):
    """السلسلةُ بعينها: 382 دورةً انهارت هنا بلا إنقاذٍ واحد."""
    import chain_layer
    import provider_keys
    import run_chain
    import solana_rpc

    monkeypatch.setattr(run_chain, "_log", lambda msg: None)
    monkeypatch.setattr(solana_rpc, "SolanaRPC", lambda *a, **k: _FakeRPC())
    monkeypatch.setattr(chain_layer, "run_chain_cycle", _locked_async)
    monkeypatch.setattr(provider_keys, "write_pool_report", lambda *a, **k: None)
    monkeypatch.setattr(config, "CHAIN_INTERVAL_SECONDS", 0)

    asyncio.run(run_chain.main_loop(cycles=3))

    assert fake_db.recoveries == 3      # إنقاذٌ لكلّ انهيار، لا واحدٌ للحلقة
    assert fake_db.closed is True       # وخرجت بنظافة لا بانفجار


def test_evm_replay_loop_rescues_before_it_stamps_the_error(fake_db, monkeypatch):
    """الترتيبُ مقصود: `note_error` نفسها تفشل على اتّصالٍ عالق."""
    import evm_rpc
    import nodereal_rpc
    import run_evm_replay

    order: list[str] = []

    def _rescue() -> str:
        order.append("rescue")
        fake_db.recoveries += 1
        return "ok"

    def _note(key, value) -> bool:
        order.append("note")
        return True

    monkeypatch.setattr(fake_db, "recover_connection", _rescue, raising=False)
    monkeypatch.setattr(fake_db, "note_error", _note, raising=False)
    monkeypatch.setattr(run_evm_replay, "_log", lambda msg: None)
    monkeypatch.setattr(evm_rpc, "EVMRPC", lambda *a, **k: _FakeRPC())
    monkeypatch.setattr(nodereal_rpc, "NodeRealRPC", lambda *a, **k: _FakeRPC())
    monkeypatch.setattr(run_evm_replay, "run_cycle", _locked_async)
    monkeypatch.setattr(config, "EVM_REPLAY_INTERVAL_SECONDS", 0)

    asyncio.run(run_evm_replay._main(cycles=2))

    assert fake_db.recoveries == 2
    assert order == ["rescue", "note", "rescue", "note"]


def test_labeler_loop_rescues_the_connection_after_every_crash(fake_db, monkeypatch):
    import labeler
    import run_labeler

    monkeypatch.setattr(run_labeler, "_log", lambda msg: None)
    monkeypatch.setattr(run_labeler, "_log_boot", lambda msg: None)
    monkeypatch.setattr(labeler, "label_pending", _locked)
    monkeypatch.setattr(config, "LABEL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", ["run_labeler.py", "2"])

    run_labeler.main()

    assert fake_db.recoveries == 2
    assert fake_db.closed is True


def test_build_rows_loop_rescues_the_connection_after_every_crash(fake_db, monkeypatch):
    """فترةُ هذه الحلقة ساعة: كلُّ دورةٍ ساقطة بلا إنقاذٍ ساعةُ صفوفٍ لا تُبنى."""
    import run_build_rows

    monkeypatch.setattr(run_build_rows, "_log", lambda msg: None)
    monkeypatch.setattr(run_build_rows, "_log_boot", lambda msg: None)
    monkeypatch.setattr(run_build_rows, "run_cycle", _locked)
    monkeypatch.setattr(config, "BUILD_ROWS_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(sys, "argv", ["run_build_rows.py", "2"])

    run_build_rows.main()

    assert fake_db.recoveries == 2
    assert fake_db.closed is True


def test_every_long_lived_loop_wires_the_rescue():
    """حرسٌ على الطبقة لا على الحالة: حلقةٌ جديدة تنسى النداءَ فيسقط هذا."""
    import os

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = []
    for name in ("run_chain.py", "run_evm_replay.py", "run_labeler.py",
                 "run_build_rows.py", "recorder.py"):
        with open(os.path.join(here, name), encoding="utf-8") as fh:
            if "recover_connection()" not in fh.read():
                missing.append(name)
    assert missing == []
