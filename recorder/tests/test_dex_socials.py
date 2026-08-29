"""اختبارات طبقة DEX Screener socials (fv16)."""
import asyncio

import pytest
from db import RecorderDB

import dex_screener
import recorder


class _FakeDex:
    def __init__(self, channels):
        self._channels = channels
        self.asked = []

    async def social_channels(self, address, network_id):
        self.asked.append(address)
        return self._channels

    async def aclose(self):
        pass


@pytest.fixture()
def db(tmp_path):
    value = RecorderDB(str(tmp_path / "t.db"), "schema.sql")
    yield value
    value.close()


def _static(db, token, *, twitter=None, dex_channels=None):
    db._conn.execute(
        """INSERT INTO token_static(token_address, network_id, recorded_at,
               name, symbol, raw_json, twitter, social_channels_dex)
           VALUES(?,?,?,?,?,?,?,?)""",
        (token, "1399811149", "2026-08-29T00:00:00+00:00",
         "n", "S", "{}", twitter, dex_channels),
    )
    db._commit()


def _with_fake(monkeypatch, fake):
    monkeypatch.setattr(dex_screener, "DexScreenerClient", lambda: fake)


async def test_enrich_stores_channels_and_match(db, monkeypatch):
    """fomo يرى تويتر وDEX يرى قناتين ⇒ قنوات=2 وتوافق=1."""
    _static(db, "BOTHA", twitter="https://x.com/a")
    fake = _FakeDex(2)
    _with_fake(monkeypatch, fake)
    await recorder._enrich_dex_socials(db, "BOTHA", "1399811149")
    row = db._conn.execute(
        "SELECT social_channels_dex, social_match_fomo_dex FROM token_static "
        "WHERE token_address='BOTHA'").fetchone()
    assert row["social_channels_dex"] == 2
    assert row["social_match_fomo_dex"] == 1


async def test_enrich_conflict_detected(db, monkeypatch):
    """fomo يرى تويتر وDEX يرى صفرًا ⇒ تعارض=0 (نمط ملف مزور)."""
    _static(db, "FAKEx", twitter="https://x.com/fake")
    fake = _FakeDex(0)
    _with_fake(monkeypatch, fake)
    await recorder._enrich_dex_socials(db, "FAKEx", "1399811149")
    row = db._conn.execute(
        "SELECT social_channels_dex, social_match_fomo_dex FROM token_static "
        "WHERE token_address='FAKEx'").fetchone()
    assert row["social_channels_dex"] == 0
    assert row["social_match_fomo_dex"] == 0       # تعارض — الإشارة الخطرة


async def test_enrich_asked_once_only(db, monkeypatch):
    """العملة المسؤول عنها سابقًا لا تُسأل ثانية (نداء واحد في عمرها)."""
    _static(db, "ONCEx", dex_channels=3)           # سُئلت سابقًا
    fake = _FakeDex(9)
    _with_fake(monkeypatch, fake)
    await recorder._enrich_dex_socials(db, "ONCEx", "1399811149")
    assert fake.asked == []                        # لم تُسأل أبدًا
    row = db._conn.execute(
        "SELECT social_channels_dex FROM token_static "
        "WHERE token_address='ONCEx'").fetchone()
    assert row["social_channels_dex"] == 3         # القيمة الأصلية لم تُمسّ


async def test_enrich_failure_leaves_null(db, monkeypatch):
    """DEX لا يجيب (None) ⇒ العمودان يبقيان NULL — لم يُقس، لا صفر."""
    _static(db, "NOANx", twitter=None)
    fake = _FakeDex(None)
    _with_fake(monkeypatch, fake)
    await recorder._enrich_dex_socials(db, "NOANx", "1399811149")
    row = db._conn.execute(
        "SELECT social_channels_dex, social_match_fomo_dex FROM token_static "
        "WHERE token_address='NOANx'").fetchone()
    assert row["social_channels_dex"] is None
    assert row["social_match_fomo_dex"] is None


async def test_enrich_both_absent_is_match(db, monkeypatch):
    """كلا المصدرين لا يرى socials ⇒ توافق=1 (صدق متبادل على الفراغ)."""
    _static(db, "EMPTYx", twitter=None)
    fake = _FakeDex(0)
    _with_fake(monkeypatch, fake)
    await recorder._enrich_dex_socials(db, "EMPTYx", "1399811149")
    row = db._conn.execute(
        "SELECT social_match_fomo_dex FROM token_static "
        "WHERE token_address='EMPTYx'").fetchone()
    assert row["social_match_fomo_dex"] == 1


def test_features_declared():
    import features

    assert "social_channels_dex" in features.FEATURE_COLUMNS
    assert "social_match_fomo_dex" in features.FEATURE_COLUMNS
    assert features.FEATURE_VERSION >= 16
