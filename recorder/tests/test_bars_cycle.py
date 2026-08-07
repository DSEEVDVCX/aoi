"""اختبارات حلقة سحب الشموع (بلا شبكة).

تثبت السلوك الذي يحمي جمع البيانات: الجدولة الدوّارة تأخذ شريحة صغيرة، النافذة
المطلوبة تشمل سياقاً قبل الإشارة، فشل عملة واحدة لا يُسقط الشريحة، والعملة بلا
سلسلة سعرية تُوسَم no_data لتُستبعد لاحقاً.
"""
import os
from datetime import UTC, datetime, timedelta

import pytest

import config
import recorder
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
NOW = "2026-07-26T12:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


class _BarsClient:
    """عميل وهمي يسجّل أجسام الطلبات ويعيد مغلّفات مُعدّة سلفاً."""

    def __init__(self, replies=None, fail_on=None):
        self.bodies = []
        self._replies = replies or {}
        self._fail_on = fail_on or set()

    async def _post(self, path, body):
        self.bodies.append(body)
        addr = body["symbol"].split(":")[0]
        if addr in self._fail_on:
            raise RuntimeError("upstream boom")
        return self._replies.get(addr, _ok_envelope())


def _ok_envelope(n=3, base=1000):
    return {"responseObject": {
        "s": "ok",
        "t": [base + i * 300 for i in range(n)],
        "o": [1.0] * n, "h": [2.0] * n, "l": [0.5] * n,
        "c": [1.5] * n, "v": [9.0] * n,
    }}


async def _noop(_seconds):
    """يستبدل asyncio.sleep حتى لا تنتظر الاختبارات فواصل اللُّطف."""


def _watch(db, token, first_seen=NOW):
    db.upsert_watch(token, "56", "large_buy", "s1", 48, first_seen)


async def test_bars_cycle_writes_candles_and_marks_state(db):
    _watch(db, "tokA")
    client = _BarsClient()

    stats = await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["bars_tokens"] == 1
    assert stats["bars_rows"] == 3
    assert db.bars_count("tokA", "56") == 3
    state = db._conn.execute("SELECT * FROM bars_fetch_state").fetchone()
    assert state["last_status"] == "ok"
    assert state["candles"] == 3


async def test_bars_request_uses_pair_symbol_and_required_window(db):
    """symbol="address:networkId" و from/to إلزاميان — العنوان المجرّد يرمي 502
    وغيابهما يرمي 400 (كلاهما مؤكَّد حيّاً)."""
    _watch(db, "tokA")
    client = _BarsClient()

    await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    body = client.bodies[0]
    assert body["symbol"] == "tokA:56"
    assert body["resolution"] == config.BARS_RESOLUTION
    assert body["from"] < body["to"]
    # النافذة تبدأ قبل الإشارة بساعات السياق المضبوطة
    expected_from = datetime.fromisoformat(NOW) - timedelta(hours=config.BARS_PRE_SIGNAL_HOURS)
    assert body["from"] == int(expected_from.timestamp())
    assert body["to"] == int(datetime.fromisoformat(NOW).timestamp())


async def test_bars_window_is_capped_for_old_watches(db):
    """مراقبة قديمة جداً لا تطلب مدى أوسع ممّا يعيده fomo أصلاً."""
    old = (datetime.fromisoformat(NOW) - timedelta(days=30)).isoformat()
    _watch(db, "tokA", first_seen=old)
    client = _BarsClient()

    await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    span_hours = (client.bodies[0]["to"] - client.bodies[0]["from"]) / 3600
    assert span_hours == pytest.approx(config.BARS_MAX_SPAN_HOURS, abs=0.1)


async def test_bars_cycle_is_limited_to_slice_size(db):
    for i in range(config.BARS_PER_CYCLE + 4):
        _watch(db, f"tok{i}")
    client = _BarsClient()

    await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    assert len(client.bodies) == config.BARS_PER_CYCLE


async def test_one_failing_token_does_not_stop_the_slice(db):
    _watch(db, "tokA")
    _watch(db, "tokB")
    client = _BarsClient(fail_on={"tokA"})

    stats = await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["bars_errors"] == 1
    assert stats["bars_tokens"] == 1              # tokB نجحت رغم فشل tokA
    assert db.bars_count("tokB", "56") == 3
    rows = {r["token_address"]: r["last_status"]
            for r in db._conn.execute("SELECT * FROM bars_fetch_state")}
    assert rows["tokA"] == "error"
    assert rows["tokB"] == "ok"


async def test_token_without_series_is_marked_no_data(db):
    _watch(db, "tokA")
    client = _BarsClient(replies={"tokA": {"responseObject": {"s": "no_data", "t": []}}})

    stats = await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["bars_no_data"] == 1
    assert stats["bars_rows"] == 0
    state = db._conn.execute("SELECT last_status FROM bars_fetch_state").fetchone()
    assert state["last_status"] == "no_data"


async def test_freshly_fetched_token_is_not_refetched_next_cycle(db):
    _watch(db, "tokA")
    client = _BarsClient()

    await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)
    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    await recorder.run_bars_cycle(client, db, soon, sleep=_noop)
    assert len(client.bodies) == 1                # ما زالت طازجة

    later = (datetime.fromisoformat(NOW)
             + timedelta(seconds=config.BARS_REFRESH_SECONDS + 60)).isoformat()
    await recorder.run_bars_cycle(client, db, later, sleep=_noop)
    assert len(client.bodies) == 2                # حان وقت التحديث


async def test_refetch_revises_in_progress_candle_without_duplicating(db):
    """السحب المتكرّر يُراجع الشمعة الأخيرة ولا يُكرّر الصفوف."""
    _watch(db, "tokA")
    client = _BarsClient()
    await recorder.run_bars_cycle(client, db, NOW, sleep=_noop)

    client._replies["tokA"] = {"responseObject": {
        "s": "ok", "t": [1000, 1300, 1600], "o": [1.0] * 3, "h": [2.0] * 3,
        "l": [0.5] * 3, "c": [1.5, 1.5, 9.9], "v": [9.0] * 3,
    }}
    later = (datetime.fromisoformat(NOW)
             + timedelta(seconds=config.BARS_REFRESH_SECONDS + 60)).isoformat()
    await recorder.run_bars_cycle(client, db, later, sleep=_noop)

    assert db.bars_count("tokA", "56") == 3       # لا تكرار
    row = db._conn.execute("SELECT c FROM token_bars WHERE ts=1600").fetchone()
    assert row["c"] == 9.9                        # القيمة المُراجَعة


# --- شموع السوق الكلّي (macro) ---
async def test_macro_cycle_fetches_all_assets_hourly_resolution(db):
    """كل أصول config.MACRO_BARS تُسحب بالدقّة الساعية وتُخزَّن في token_bars."""
    client = _BarsClient()

    stats = await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["macro_errors"] == 0
    assert len(client.bodies) == len(config.MACRO_BARS)
    assert stats["macro_rows"] == 3 * len(config.MACRO_BARS)
    for body in client.bodies:
        assert body["resolution"] == config.MACRO_BARS_RESOLUTION
        assert ":" in body["symbol"]
    for _label, addr, net in config.MACRO_BARS:
        assert db.bars_count(addr, net) == 3
        # الدقّة الساعية لا تتصادم مع شموع المراقبة (5 دقائق)
        row = db._conn.execute(
            "SELECT DISTINCT resolution FROM token_bars WHERE token_address=?", (addr,)
        ).fetchone()
        assert row["resolution"] == config.MACRO_BARS_RESOLUTION


async def test_macro_cycle_paces_itself_via_meta(db):
    """دورة ضمن الساعة تخرج بلا نداء شبكة؛ بعد انتهاء الفاصل تسحب مجدّداً."""
    client = _BarsClient()
    await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    stats = await recorder.run_macro_bars_cycle(client, db, soon, sleep=_noop)
    assert len(client.bodies) == len(config.MACRO_BARS)   # لا سحب جديد
    assert stats["macro_rows"] == 0

    later = (datetime.fromisoformat(NOW)
             + timedelta(seconds=config.MACRO_BARS_REFRESH_SECONDS + 60)).isoformat()
    await recorder.run_macro_bars_cycle(client, db, later, sleep=_noop)
    assert len(client.bodies) == 2 * len(config.MACRO_BARS)


async def test_macro_one_failing_asset_does_not_stop_others(db):
    victim = config.MACRO_BARS[0][1]
    client = _BarsClient(fail_on={victim})

    stats = await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["macro_errors"] == 1
    assert stats["macro_rows"] == 3 * (len(config.MACRO_BARS) - 1)
    assert "last_error_macro" in {
        r["key"] for r in db._conn.execute("SELECT key FROM meta")
    }


async def test_macro_total_failure_retries_next_cycle_not_next_hour(db):
    """انقطاع كامل لا يختم last_macro_bars_at — وإلّا أُرجئت الاستعادة ساعة
    كاملة بسبب عابر (حدث فعلاً عند أول نشر)."""
    all_addrs = {addr for _l, addr, _n in config.MACRO_BARS}
    client = _BarsClient(fail_on=all_addrs)

    stats = await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["macro_errors"] == len(config.MACRO_BARS)
    assert db.get_meta("last_macro_bars_at") is None      # لا ختم بلا نجاح

    client._fail_on = set()                                # fomo تعافى
    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    stats = await recorder.run_macro_bars_cycle(client, db, soon, sleep=_noop)
    assert stats["macro_rows"] == 3 * len(config.MACRO_BARS)  # استعادة فورية


async def test_macro_all_empty_replies_are_not_stamped_as_success(db):
    """ردود «ناجحة» بلا شموع: بلا استثناء لكنها فشل. الختم عليها كان سيحوّل
    تهيئة خاطئة إلى فجوة صامتة أبدية بلا أي خطأ مسجَّل."""
    no_data = {addr: {"responseObject": {"s": "no_data", "t": []}}
               for _l, addr, _n in config.MACRO_BARS}
    client = _BarsClient(replies=no_data)

    stats = await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["macro_rows"] == 0
    assert stats["macro_errors"] == 0
    assert stats["macro_no_data"] == len(config.MACRO_BARS)
    assert db.get_meta("last_macro_bars_at") is None      # لا ختم على فراغ
    assert "no data" in (db.get_meta("last_error_macro") or "")

    soon = (datetime.fromisoformat(NOW) + timedelta(seconds=60)).isoformat()
    stats = await recorder.run_macro_bars_cycle(client, db, soon, sleep=_noop)
    assert len(client.bodies) == 2 * len(config.MACRO_BARS)   # يعيد لا ينتظر ساعة


async def test_macro_partial_success_still_stamps(db):
    """أصل نجح وأصلان فارغان: الختم يُكتب (إعادة ساعية للفارغين) ولا خطأ كليّ."""
    no_data = {config.MACRO_BARS[0][1]: {"responseObject": {"s": "no_data", "t": []}}}
    client = _BarsClient(replies=no_data)

    stats = await recorder.run_macro_bars_cycle(client, db, NOW, sleep=_noop)

    assert stats["macro_rows"] == 3 * (len(config.MACRO_BARS) - 1)
    assert stats["macro_no_data"] == 1
    assert db.get_meta("last_macro_bars_at") == NOW
    assert db.get_meta("last_error_macro") is None
