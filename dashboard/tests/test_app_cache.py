"""مسارات اللوحة الثقيلة: تُحسب مرّةً كلَّ مدّة، وطزاجتُها معلَنة.

الخلفيّة المقيسة: `/api/networks` كان يستهلك 2.2 ثانية من أصل 2.2 ثانية في كلّ
تحديثٍ للوحة، وسببُها عقدةٌ تمسح ثلاثةَ ملايين سطرٍ لتُخرج خمسة. والقاعدةُ لا
تُلمس (لا فهرسَ ولا كتابة)، فالحلُّ في الكود: ناتجٌ مخزَّنٌ في ذاكرة العمليّة،
وفوقه ختمُ طزاجةٍ حيٌّ يكلّف 0.4 مللي ثانية.

وهنا نختبر ما يُرى من الخارج: أنّ الحسابَ لا يتكرّر، وأنّ الحمولةَ تقول عمرَها،
وأنّ «آخر سوق» يبقى حيّاً فوق العدّ البائت **بلا أن يُفسد المخزَّن**.

المُشعِلُ محقون في كلّ اختبار: بلا ذلك يقرأ خيطُ التجديد الخلفيّ `config.DB_PATH`
بعد أن يُرجعه `monkeypatch` إلى القاعدة الحقيقيّة — أي أنّ اختباراً يفتح قاعدةَ
15 جيجابايت في الخلف ويصير فشلُه غيرَ مفهوم.
"""
import sqlite3

import app as dashboard_app
import cache
import config
import dao
import pytest
from fastapi.testclient import TestClient

HOST = {"Host": "127.0.0.1:8090"}

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE watchlist (
  token_address TEXT, network_id TEXT, first_seen_at TEXT, source TEXT,
  watch_until TEXT, entry_signal_id TEXT, active INTEGER,
  is_control INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE market_ticks (
  token_address TEXT, network_id TEXT, recorded_at TEXT, source TEXT,
  price_usd REAL, holders INTEGER, change_24h REAL, volume_24h REAL,
  buy_count_24h INTEGER, sell_count_24h INTEGER
);
"""


class Spawner:
    """يمسك التجديدَ الخلفيّ حتى يقرّر الاختبار تنفيذَه."""

    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> int:
        queued, self.pending = self.pending, []
        for run in queued:
            run()
        return len(queued)


@pytest.fixture()
def spawner(monkeypatch):
    spawn = Spawner()
    monkeypatch.setattr(dashboard_app.cache, "MEMO", cache.TTLMemo(spawn=spawn))
    return spawn


@pytest.fixture()
def db(tmp_path, monkeypatch, spawner):
    """قاعدةٌ صغيرة محلَّ القاعدة الحقيقيّة، وذاكرةٌ مؤقّتة نظيفة لكلّ اختبار."""
    path = tmp_path / "rec.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO watchlist VALUES('sol','1399811149','t','large_buy','t2','s',1,0)"
    )
    conn.execute(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at)"
        " VALUES('sol','1399811149','2026-08-18T09:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    return path


def _tick(db, stamp: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO market_ticks(token_address, network_id, recorded_at)"
        " VALUES('sol','1399811149',?)", (stamp,),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def client(db):
    return TestClient(dashboard_app.app)


def _counted(monkeypatch, name: str) -> list[int]:
    """يغلّف دالةَ dao بعدّادٍ — به نعرف «حُسب مرّةً» من «حُسب في كلّ طلب»."""
    calls: list[int] = []
    original = getattr(dao, name)

    def wrapper(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(dao, name, wrapper)
    return calls


# --- الحساب لا يتكرّر ---
def test_networks_computes_the_heavy_query_once(client, monkeypatch):
    calls = _counted(monkeypatch, "network_summary")

    for _ in range(6):
        assert client.get("/api/networks", headers=HOST).status_code == 200

    assert len(calls) == 1


def test_labeling_is_cached_like_other_heavy_panels(client, db, monkeypatch):
    """ملخّص التوسيم أثقلُ لوحةٍ بعد الشبكات (1.2 ثانية) — يُخزَّن كذلك.

    وآخرُ توسيمٍ فوقه حيٌّ: نبضُ الموسِّم يُقرأ في كلّ طلبٍ بلا انتظار التجديد.
    """
    calls = _counted(monkeypatch, "labeling_outcomes")

    first = client.get("/api/labeling", headers=HOST).json()
    second = client.get("/api/labeling", headers=HOST).json()

    assert len(calls) == 1
    assert first["live"] is False            # لا جدول outcomes في قاعدة الاختبار
    assert second == first
    assert first["cache"]["ttl_seconds"] == config.LABELING_TTL_SECONDS


def test_counts_computes_once_within_its_ttl(client, monkeypatch):
    calls = _counted(monkeypatch, "table_counts")

    first = client.get("/api/counts", headers=HOST).json()
    second = client.get("/api/counts", headers=HOST).json()

    assert len(calls) == 1
    assert first["market_ticks"] == second["market_ticks"] == 1


def test_ticks_summary_is_cached_too(client, monkeypatch):
    """لا تناديه الصفحة اليوم، لكنّه يكلّف ثانيةً ونصفاً لمن يناديه غداً."""
    calls = _counted(monkeypatch, "ticks_summary")

    body = client.get("/api/ticks-summary", headers=HOST).json()
    client.get("/api/ticks-summary", headers=HOST)

    assert len(calls) == 1
    assert body["total"] == 1


# --- الحمولةُ تقول عمرَها ---
@pytest.mark.parametrize(
    ("path", "ttl_attr"),
    [
        ("/api/networks", "NETWORK_SUMMARY_TTL_SECONDS"),
        ("/api/counts", "TABLE_COUNTS_TTL_SECONDS"),
        ("/api/ticks-summary", "TICKS_SUMMARY_TTL_SECONDS"),
        ("/api/labeling", "LABELING_TTL_SECONDS"),
    ],
)
def test_cached_routes_publish_their_freshness(client, path, ttl_attr):
    """رقمٌ يُظنّ لحظيّاً وهو ليس كذلك أسوأُ من رقمٍ يقول عمرَه."""
    meta = client.get(path, headers=HOST).json()["cache"]

    assert meta["ttl_seconds"] == getattr(config, ttl_attr)
    assert meta["stale"] is False
    assert meta["error"] is None
    assert meta["computed_at"].endswith("+00:00")
    assert meta["age_seconds"] >= 0


# --- الطزاجةُ الحيّة فوق العدّ البائت ---
def test_latest_tick_stays_live_while_counts_are_cached(client, db):
    """جوهرُ التصميم: العدُّ يُخزَّن، والنبضُ يُقرأ في كلّ طلب."""
    first = client.get("/api/networks", headers=HOST).json()["networks"][0]
    assert (first["tick_rows"], first["latest_tick"]) == (1, "2026-08-18T09:00:00+00:00")

    _tick(db, "2026-08-18T11:00:00+00:00")
    second = client.get("/api/networks", headers=HOST).json()["networks"][0]

    assert second["tick_rows"] == 1                            # العدّ بائتٌ عن قصد
    assert second["latest_tick"] == "2026-08-18T11:00:00+00:00"  # والنبضُ حيّ


def test_the_live_overlay_does_not_corrupt_the_cached_rows(client, db, monkeypatch):
    """الصفوفُ المخزَّنة يتشاركها كلُّ طلب؛ رفعُ الختم فيها كان سيُثبِّت الحيَّ.

    نُعمي القفزةَ الحيّة بعد أن ترفع الختمَ مرّة: لو كانت قد عدّلت المخزَّن في
    مكانه لبقي الوقتُ المرفوع؛ والصحيحُ أن يرجع الختمُ المخزَّن كما هو.
    """
    client.get("/api/networks", headers=HOST)
    _tick(db, "2026-08-18T11:00:00+00:00")
    assert client.get("/api/networks", headers=HOST).json()["networks"][0][
        "latest_tick"
    ] == "2026-08-18T11:00:00+00:00"

    monkeypatch.setattr(dao, "latest_tick_per_active_network", lambda conn: {})
    blind = client.get("/api/networks", headers=HOST).json()["networks"][0]
    assert blind["latest_tick"] == "2026-08-18T09:00:00+00:00"


# --- البياتةُ والفشل كما يظهران في الحمولة ---
def test_stale_payload_is_served_immediately_then_refreshed(client, db, spawner, monkeypatch):
    """أحدَ عشرَ طلباً من الذاكرة والثاني عشر ينتظر ثانيتين — هذا ما نمنعه."""
    monkeypatch.setattr(config, "NETWORK_SUMMARY_TTL_SECONDS", 0.0)
    client.get("/api/networks", headers=HOST)
    _tick(db, "2026-08-18T11:00:00+00:00")

    body = client.get("/api/networks", headers=HOST).json()
    assert body["networks"][0]["tick_rows"] == 1      # البائتةُ رجعت بلا انتظار
    assert body["cache"]["stale"] is True
    assert body["cache"]["refreshing"] is True

    assert spawner.run_all() == 1
    assert client.get("/api/networks", headers=HOST).json()["networks"][0]["tick_rows"] == 2


def test_a_failed_refresh_keeps_the_last_good_numbers(client, spawner, monkeypatch):
    """قاعدةٌ مشغولة تُسقط تجديداً: نُبقي القديمةَ ونقول إنّ التجديد تعذّر."""
    monkeypatch.setattr(config, "NETWORK_SUMMARY_TTL_SECONDS", 0.0)
    client.get("/api/networks", headers=HOST)

    def boom(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(dao, "network_summary", boom)
    client.get("/api/networks", headers=HOST)
    spawner.run_all()

    body = client.get("/api/networks", headers=HOST).json()
    assert body["networks"][0]["tick_rows"] == 1
    assert "database is locked" in body["cache"]["error"]


# --- ما تفعله الصفحة نفسها ---
def test_the_page_asks_for_the_heavy_panels_less_often(client):
    """الخادمُ يخزّن 60–120 ثانية، فسؤالُه كلَّ عشرٍ يجلب نفسَ الرقم ستَّ مرّات.

    والحمولةُ الأخيرة تُعاد بينهما — بلا ذلك تفرغ اللوحةُ دقيقةً كاملة.
    """
    page = client.get("/", headers=HOST).text

    assert "const HEAVY_EVERY = 6;" in page
    assert "heavyTick % HEAVY_EVERY === 0" in page
    # الحرسُ الثاني: عدّادٌ وحدَه كان سيُبقي اللوحةَ فارغةً دقيقةً بعد جلبٍ فاشل.
    assert "heavy.counts === null" in page
    assert 'wantHeavy ? getJSON("/api/counts") : heavy.counts' in page
    # ولوحةُ التوسيم كذلك: 5 دقائق عمرُها أطولُ من دورةِ الشبكات، لكنّها
    # تركب نفس الإيقاع — لا طلبَ جديد إلا في دورةٍ «ثقيلة».
    assert 'getOptionalJSON("/api/labeling", heavy.labeling' in page
    assert "paint(\"labeling\", () => renderLabeling(labeling))" in page


def test_the_page_says_which_numbers_are_cached_and_which_are_live(client):
    """رقمٌ يُظنّ لحظيّاً وهو ليس كذلك أسوأُ من رقمٍ يقول عمرَه."""
    page = client.get("/", headers=HOST).text

    assert "renderNetworks(networks.networks || [], networks.cache)" in page
    assert "أعدادُ التغطية محسوبةٌ منذ" in page
    assert "«آخر سوق» حيٌّ في كلّ تحديث" in page
    assert "network-note" in page


def test_a_cold_failure_is_not_hidden_behind_stale_numbers(client, monkeypatch):
    """لا قيمةَ سابقة ⇒ الخطأ يظهر. كتمانُه بصفرٍ كان سيقرأ «لا شبكات»."""
    monkeypatch.setattr(
        dao, "network_summary",
        lambda conn: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        client.get("/api/networks", headers=HOST)


# --- الحدُّ الذي لا يُساوَم عليه ---
def test_caching_writes_nothing_to_the_database(client, db):
    """لا جدولَ تخزينٍ ولا ختمَ في `meta`: القاعدةُ تبقى كما وجدناها.

    الحارسُ الحقيقيّ هو `mode=ro`، لكنّ هذا يمسك ما هو أخفى: مساراً يظنّ أنّه
    يقرأ وهو يختم شيئاً — وأيُّ كاتبٍ ثانٍ يزاحم المسجّلَ على قفلٍ رأينا موتَه
    ثلاثَ ساعاتٍ مرّة.
    """
    before = sorted(
        sqlite3.connect(db)
        .execute("SELECT name FROM sqlite_master ORDER BY name")
        .fetchall()
    )
    for path in ("/api/networks", "/api/counts", "/api/ticks-summary", "/api/labeling"):
        client.get(path, headers=HOST)

    conn = sqlite3.connect(db)
    after = sorted(conn.execute("SELECT name FROM sqlite_master ORDER BY name").fetchall())
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    conn.close()
    assert after == before
