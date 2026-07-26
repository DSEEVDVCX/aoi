"""طبقة قاعدة البيانات لمسجّل البيانات التاريخي.

مغلّف رقيق حول sqlite3: إنشاء المخطّط، إدراج idempotent (INSERT OR IGNORE /
UPSERT حسب الجدول)، وقراءة watchlist. لا منطق شبكة هنا — قابل للاختبار كاملاً
بقاعدة بيانات مؤقّتة.

المبدأ: كل صفّ يحمل raw_json الخام دائماً. الحقول المستخرجة للاستعلام السريع؛
الخام للحقيقة الكاملة وإعادة الاشتقاق.

**ضغط الخام**: الخام يُخزَّن مضغوطاً بـ zlib (BLOB) لا نصّاً. بلا ضغط كانت
القاعدة تكبر ~1.3 GB يومياً (`snapshots` وحدها 577 MB خلال 20 ساعة) لأنّ لقطة
كاملة من trending/verified/feed تُكتب كل دقيقة. الضغط بلا خسارة (نسبة ~4.7x)
فلا يُفقد أي بايت من الأرشيف — راجع `decode_raw` للقراءة. الصفوف القديمة
المكتوبة نصّاً تبقى مقروءة (decode_raw يقبل النوعين).
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

# مستوى ضغط zlib: 6 هو أفضل مقايضة قياساً على البيانات الحيّة
# (4.7x بـ 5.6ms/لقطة مقابل 5.9x بـ 49ms لـ lzma).
_ZLIB_LEVEL = 6
# بادئة zlib القياسية (0x78) — نميّز بها الخام المضغوط عن النصّ القديم.
_ZLIB_MAGIC = 0x78


def utcnow_iso() -> str:
    """الوقت الحالي ISO-8601 UTC — الختم الزمني الموحّد للمسجّل."""
    return datetime.now(UTC).isoformat()


def encode_raw(raw: Any) -> bytes:
    """أي كائن (أو نصّ JSON جاهز) → BLOB مضغوط للتخزين في عمود raw_json."""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return zlib.compress(text.encode("utf-8"), _ZLIB_LEVEL)


def decode_raw(value: Any) -> Any:
    """عمود raw_json → الكائن الأصلي. يقبل الصفوف الجديدة (BLOB مضغوط)
    والقديمة (نصّ JSON عاديّ) على السواء، فلا يُكسر الأرشيف الموجود."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if data[:1] and data[0] == _ZLIB_MAGIC:
            data = zlib.decompress(data)
        return json.loads(data.decode("utf-8"))
    return json.loads(value)


class RecorderDB:
    """اتصال SQLite واحد للمسجّل (حلقة أحادية الخيط، فلا حاجة لتجمّع اتصالات)."""

    def __init__(self, db_path: str, schema_path: str) -> None:
        # timeout=30: عمليتان تكتبان الآن (المسجّل + الموسِّم FomoLabeler)؛
        # WAL يسلسل الكتابات لكنّ مهلة بايثون الافتراضية (5ث) قد تضيق وقت
        # الازدحام فترمي "database is locked" بلا داعٍ.
        self._conn = sqlite3.connect(db_path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._batching = False
        self._apply_schema(schema_path)

    def _apply_schema(self, schema_path: str) -> None:
        # الترحيل **قبل** المخطّط: schema.sql ينشئ فهرساً على is_control، وهو
        # يفشل على جدول قديم لا يملك العمود بعد. على قاعدة جديدة الترحيل بلا أثر
        # (لا جداول بعد)، فالترتيب آمن في الحالتين.
        self._migrate()
        with open(schema_path, "r", encoding="utf-8") as fh:
            self._conn.executescript(fh.read())
        self._conn.commit()

    def _migrate(self) -> None:
        """يضيف الأعمدة الناقصة إلى قاعدة قائمة.

        `CREATE TABLE IF NOT EXISTS` لا يمسّ جدولاً موجوداً، فعمود يُضاف إلى
        schema.sql لا يظهر أبداً في قاعدة أُنشئت قبله — تعطب الاستعلامات في
        الإنتاج بينما تمرّ على قاعدة اختبار جديدة. الفحص هنا idempotent،
        والجدول الغائب يُتخطّى (`if cols`) فيتكفّل به المخطّط بعد قليل.
        """
        for table, column, ddl in _COLUMN_MIGRATIONS:
            cols = {
                r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if cols and column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        # outcomes v2: المفتاح القديم (token, entry_ts) يتصادم حتماً (201 إشارة
        # لعملة واحدة). الجدول فارغ بالتصميم، فإسقاطه آمن؛ وإن حمل بيانات على
        # غير المتوقّع نُبقيها باسم legacy بدل إتلافها.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(outcomes)")}
        if cols and "kind" not in cols:
            n = self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
            if n == 0:
                self._conn.execute("DROP TABLE outcomes")
            else:  # pragma: no cover - لا يُفترض حدوثه
                self._conn.execute("ALTER TABLE outcomes RENAME TO outcomes_legacy")

    def _commit(self) -> None:
        """يثبّت فوراً، إلّا داخل batch() حيث يؤجَّل التثبيت إلى نهاية الدفعة."""
        if not self._batching:
            self._conn.commit()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """يجمع إدراجات كثيرة في تثبيت واحد (fsync واحد بدل عشرات).

        الحلقة تكتب ~65 tick في الدورة؛ تثبيت كل صفّ على حدة كان يعني ~70
        fsync في الدقيقة. الخروج بخطأ يُرجع الدفعة كاملة (rollback) — لذا
        نستعملها حول حلقات الإدراج فقط، لا حول كتابة meta الخاصّة بالأخطاء.
        """
        if self._batching:  # متداخلة: الدفعة الخارجية تملك التثبيت
            yield
            return
        self._batching = True
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._batching = False

    def close(self) -> None:
        self._conn.close()

    # --- meta ---
    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def bump_counter(self, key: str, by: int = 1) -> None:
        cur = self.get_meta(key)
        n = (int(cur) if cur and cur.isdigit() else 0) + by
        self.set_meta(key, str(n))

    # --- signal_events ---
    def insert_signal(self, row: Mapping[str, Any]) -> bool:
        """إدراج idempotent حسب id (feed event id). يعيد True إن أُدرج صف جديد.

        نبني الوسائط من قائمة أعمدة صريحة (كبقيّة الإدراجات) لا من مفاتيح
        المُرسِل: تمرير القاموس كما هو كان يجعل أي عمود جديد يُسقط كل مُستدعٍ
        لا يعرف به بعد — `ProgrammingError` بدل حقل فارغ.
        """
        cols = _SIGNAL_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO signal_events({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- market_ticks ---
    def insert_tick(self, row: Mapping[str, Any]) -> bool:
        """مفتاح مركّب (token, network, recorded_at, source) يمنع التكرار في الدورة."""
        cols = _TICK_COLUMNS
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT OR IGNORE INTO market_ticks({', '.join(cols)}) "
            f"VALUES({placeholders})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- token_static ---
    def upsert_static(self, row: Mapping[str, Any]) -> None:
        """ثوابت العملة — تُكتب مرّة؛ نتركها كما هي إن وُجدت (INSERT OR IGNORE)."""
        cols = _STATIC_COLUMNS
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (
            f"INSERT OR IGNORE INTO token_static({', '.join(cols)}) "
            f"VALUES({placeholders})"
        )
        self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()

    def static_exists(self, token_address: str, network_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM token_static WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return row is not None

    # --- snapshots ---
    def insert_snapshot(self, source: str, raw: Any, recorded_at: str | None = None) -> None:
        """أرشيف خام كامل لمصدر. يُخزَّن مضغوطاً — هذا الجدول وحده كان 80% من
        حجم القاعدة (لقطة ~310 KB × 3 مصادر × 1440 دورة يومياً)."""
        self._conn.execute(
            "INSERT INTO snapshots(recorded_at, source, raw_json) VALUES(?, ?, ?)",
            (recorded_at or utcnow_iso(), source, encode_raw(raw)),
        )
        self._commit()

    def read_snapshot(self, snapshot_id: int) -> Any:
        """يقرأ لقطة ويفكّ ضغطها (يقبل الصفوف النصّية القديمة أيضاً)."""
        row = self._conn.execute(
            "SELECT raw_json FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        return decode_raw(row["raw_json"]) if row else None

    def prune_snapshots(self, older_than_iso: str) -> int:
        """يحذف اللقطات الأقدم من ختم معطى. يعيد عدد المحذوفة.

        اختياريّ ومعطّل افتراضياً (config.SNAPSHOT_RETENTION_DAYS = 0): اللقطات
        هي أرشيف إعادة الاشتقاق، فحذفها قرار المالك لا سلوك ضمنيّ.
        """
        cur = self._conn.execute(
            "DELETE FROM snapshots WHERE recorded_at < ?", (older_than_iso,)
        )
        self._commit()
        return cur.rowcount

    # --- token_bars ---
    def insert_bars(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """يُدرج شموع OHLCV. يعيد عدد الصفوف المكتوبة.

        OR REPLACE لا OR IGNORE: الشمعة الأحدث تكون قيد التكوّن وقت السحب،
        فقيمتها تُراجَع في السحب التالي — الأحدث هي الصحيحة.
        """
        if not rows:
            return 0
        cols = _BAR_COLUMNS
        sql = (
            f"INSERT OR REPLACE INTO token_bars({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        with self.batch():
            self._conn.executemany(sql, [{c: r.get(c) for c in cols} for r in rows])
        return len(rows)

    def bars_count(self, token_address: str, network_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_bars WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return int(row["n"])

    def set_bars_state(
        self, token_address: str, network_id: str, status: str, candles: int, now_iso: str
    ) -> None:
        """يسجّل نتيجة آخر محاولة سحب. `attempts` يتراكم لكشف العملات الميّتة."""
        self._conn.execute(
            """INSERT INTO bars_fetch_state(
                   token_address, network_id, last_fetch_at, last_status, candles, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   candles       = excluded.candles,
                   attempts      = bars_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, candles),
        )
        self._commit()

    def bars_fetch_due(
        self, limit: int, stale_before_iso: str, max_no_data_attempts: int
    ) -> list[dict[str, Any]]:
        """العملات المراقَبة النشطة الأولى بالسحب — الأقدم سحباً أوّلاً.

        جدولة دوّارة: كل دورة تأخذ شريحة صغيرة، فيكتمل المسح عبر عدّة دورات بلا
        دفقة تُثقل الحلقة أو تُغرق fomo. العملة التي ردّ عليها fomo `no_data`
        مراراً تُستبعد — لا معنى لإهدار محاولات على عملة بلا سلسلة سعرية.
        `NULLS FIRST` يضمن أنّ التي لم تُسحب قطّ تسبق الجميع (backfill الدخول).
        """
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.first_seen_at,
                      s.last_fetch_at, s.last_status, s.attempts
               FROM watchlist w
               LEFT JOIN bars_fetch_state s
                 ON s.token_address = w.token_address AND s.network_id = w.network_id
               WHERE w.active = 1
                 AND (s.last_fetch_at IS NULL OR s.last_fetch_at < ?)
                 -- COALESCE إلزاميّ: بلا صفّ حالة يصير الشرط
                 -- NOT (NULL AND NULL) = NULL، فتُستبعد بصمت كل عملة لم تُسحب
                 -- قطّ — وهي بالضبط الحالة التي يوجد الـ backfill من أجلها.
                 AND NOT (COALESCE(s.last_status, '') = 'no_data'
                          AND COALESCE(s.attempts, 0) >= ?)
               ORDER BY s.last_fetch_at IS NOT NULL, s.last_fetch_at
               LIMIT ?""",
            (stale_before_iso, max_no_data_attempts, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- token_social ---
    def insert_social(self, row: Mapping[str, Any]) -> bool:
        """لقطة اجتماعية. المفتاح (token, network, recorded_at) يمنع التكرار."""
        cols = _SOCIAL_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_social({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def insert_thesis_items(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """يُدرج أطروحات فردية. idempotent حسب id. يعيد عدد المُدرَج فعلاً."""
        if not rows:
            return 0
        cols = _THESIS_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_thesis({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        added = 0
        with self.batch():
            for r in rows:
                cur = self._conn.execute(
                    sql, _with_compressed_raw({c: r.get(c) for c in cols})
                )
                added += cur.rowcount
        return added

    def thesis_count_before(self, token_address: str, at_iso: str) -> int:
        """كم أطروحة كانت موجودة قبل لحظة معطاة — إعادة بناء العدد التاريخي."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_thesis "
            "WHERE token_address=? AND created_at <= ?",
            (token_address, at_iso),
        ).fetchone()
        return int(row["n"])

    def set_social_state(
        self, token_address: str, network_id: str, status: str, items: int, now_iso: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO social_fetch_state(
                   token_address, network_id, last_fetch_at, last_status, items, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   items         = excluded.items,
                   attempts      = social_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, items),
        )
        self._commit()

    def social_fetch_due(self, limit: int, stale_before_iso: str) -> list[dict[str, Any]]:
        """العملات المستحقّة للقطة اجتماعية — الأقدم سحباً أوّلاً.

        بخلاف الشموع لا نستبعد العملة الفارغة: غياب النقاش **إشارة بذاته**
        وتغيّره عبر الزمن هو المطلوب، فلا معنى لإسقاط عملة صامتة اليوم.
        """
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, s.last_fetch_at
               FROM watchlist w
               LEFT JOIN social_fetch_state s
                 ON s.token_address = w.token_address AND s.network_id = w.network_id
               WHERE w.active = 1
                 AND (s.last_fetch_at IS NULL OR s.last_fetch_at < ?)
               ORDER BY s.last_fetch_at IS NOT NULL, s.last_fetch_at
               LIMIT ?""",
            (stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def social_count(self, token_address: str, network_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM token_social WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return int(row["n"])

    # --- watchlist ---
    def upsert_watch(
        self,
        token_address: str,
        network_id: str,
        source: str,
        entry_signal_id: str | None,
        watch_hours: int,
        now_iso: str | None = None,
    ) -> bool:
        """يُدخل عملة للمراقبة، أو يُعيد تنشيط عملة انتهت مدّتها.

        يعيد True إن دخلت العملة المراقبة الآن (جديدة أو مُعاد تنشيطها).

        - عملة **نشطة** أصلاً: لا شيء — لا نمدّد مدّتها (أوّل ظهور يبقى المرجع).
        - عملة **معطّلة** (active=0 بعد 48س): تُعاد بنافذة جديدة وإشارة دخول
          جديدة. بدون هذا الفرع يبقى الصفّ القديم فيبتلع INSERT OR IGNORE كل
          إشارة لاحقة إلى الأبد، فتنزف القائمة حتى الصفر وتتوقّف الـ ticks.
        """
        now = now_iso or utcnow_iso()
        until = (datetime.fromisoformat(now) + timedelta(hours=watch_hours)).isoformat()
        cur = self._conn.execute(
            """INSERT INTO watchlist(
                token_address, network_id, first_seen_at, source,
                watch_until, entry_signal_id, active, is_control
            ) VALUES(?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(token_address, network_id) DO UPDATE SET
                first_seen_at   = excluded.first_seen_at,
                source          = excluded.source,
                watch_until     = excluded.watch_until,
                entry_signal_id = excluded.entry_signal_id,
                active          = 1,
                is_control      = 0
            WHERE watchlist.active = 0 OR watchlist.is_control = 1""",
            (token_address, network_id, now, source, until, entry_signal_id),
        )
        self._commit()
        return cur.rowcount > 0

    def admit_control(
        self,
        token_address: str,
        network_id: str,
        watch_hours: int,
        now_iso: str | None = None,
    ) -> bool:
        """يُدخل عملة **ضابطة** (بلا إشارة). يعيد True إن أُضيفت.

        لا تلمس صفّاً موجوداً إطلاقاً: عملة أشارت إليها إشارة يجب ألّا تُخفَّض
        إلى ضابطة، وعملة ضابطة نشطة لا تُعاد ضبط نافذتها. الترقية إلى «مُشار
        إليها» تحدث في الاتجاه الآخر فقط، عبر upsert_watch.
        """
        now = now_iso or utcnow_iso()
        until = (datetime.fromisoformat(now) + timedelta(hours=watch_hours)).isoformat()
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO watchlist(
                token_address, network_id, first_seen_at, source,
                watch_until, entry_signal_id, active, is_control
            ) VALUES(?, ?, ?, 'control', ?, NULL, 1, 1)""",
            (token_address, network_id, now, until),
        )
        self._commit()
        return cur.rowcount > 0

    def known_tokens(self) -> set[tuple[str, str]]:
        """كل عملة سبق أن دخلت (مُشار إليها أو ضابطة، نشطة أو منتهية).

        تُستعمل لاستبعاد المرشّحين: لا نُدخل عملة ضابطة رأيناها من قبل.
        """
        return {
            (r["token_address"], str(r["network_id"] or ""))
            for r in self._conn.execute("SELECT token_address, network_id FROM watchlist")
        }

    def signalled_tokens(self) -> set[str]:
        """عناوين كل عملة ورد عليها حدث إشارة — حتى لو لم تدخل المراقبة.

        المرشّح الضابط يجب ألّا يكون قد أُشير إليه أصلاً، وإلّا لم يعد ضابطاً.
        """
        return {
            r["token_address"]
            for r in self._conn.execute("SELECT DISTINCT token_address FROM signal_events")
        }

    def active_watch_count(self, is_control: int | None = None) -> int:
        """عدد النشطات. `is_control=None` يشمل الجميع؛ 0 المُشار إليها؛ 1 الضابطة.

        السقف (`WATCHLIST_CAP`) يُطبَّق على المُشار إليها وحدها، وإلّا زاحمتها
        الضابطة على مقاعدها.
        """
        if is_control is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM watchlist WHERE active=1"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM watchlist WHERE active=1 AND is_control=?",
                (is_control,),
            ).fetchone()
        return int(row["n"])

    def active_watches(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT token_address, network_id, source, watch_until, first_seen_at "
            "FROM watchlist WHERE active=1"
        ).fetchall()
        return [dict(r) for r in rows]

    def deactivate_expired(self, now_iso: str | None = None) -> int:
        """يعطّل كل عملة تجاوزت watch_until. يعيد عدد المعطّلة."""
        now = now_iso or utcnow_iso()
        cur = self._conn.execute(
            "UPDATE watchlist SET active=0 WHERE active=1 AND watch_until <= ?",
            (now,),
        )
        self._commit()
        return cur.rowcount


    # --- outcomes (يكتبها الـ labeler، عملية منفصلة) ---
    def insert_outcome(self, row: Mapping[str, Any]) -> bool:
        """إدراج نتيجة موسومة. idempotent حسب (kind, key) — التوسيم يُكتب مرّة
        ولا يُراجَع (النافذة مكتملة والتاريخ لا يتغيّر)."""
        cols = _OUTCOME_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO outcomes({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, {c: row.get(c) for c in cols})
        self._commit()
        return cur.rowcount > 0

    def signals_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """إشارات نضجت نافذتها ولم تُوسَم بعد — الأقدم أوّلاً.

        `prev_ts` = ختم الإشارة السابقة على نفس العملة (لحساب علم الاستقلال).
        strftime يقبل صيغتَي fomo (Z) والمسجّل (+00:00) على السواء (مُتحقَّق).
        """
        rows = self._conn.execute(
            """SELECT s.id, s.token_address, s.network_id, s.signal_type, s.ts,
                      CAST(strftime('%s', s.ts) AS INTEGER) AS entry_epoch,
                      (SELECT MAX(p.ts) FROM signal_events p
                        WHERE p.token_address = s.token_address AND p.ts < s.ts)
                        AS prev_ts
               FROM signal_events s
               WHERE s.ts IS NOT NULL
                 AND CAST(strftime('%s', s.ts) AS INTEGER) <= ?
                 AND NOT EXISTS (SELECT 1 FROM outcomes o
                                  WHERE o.kind = 'signal' AND o.key = s.id)
               ORDER BY s.ts LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def watches_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """دخولات مراقبة (إشارة وضابطة) نضجت نافذتها ولم تُوسَم — للمقارنة
        على نوافذ مكتملة بدل النافذة الجارية."""
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.source, w.is_control,
                      w.first_seen_at,
                      CAST(strftime('%s', w.first_seen_at) AS INTEGER) AS entry_epoch,
                      w.token_address || ':' || w.network_id || ':' || w.first_seen_at
                        AS key
               FROM watchlist w
               WHERE CAST(strftime('%s', w.first_seen_at) AS INTEGER) <= ?
                 AND NOT EXISTS (SELECT 1 FROM outcomes o
                                  WHERE o.kind = 'watch' AND o.key =
                                    w.token_address || ':' || w.network_id || ':' || w.first_seen_at)
               ORDER BY w.first_seen_at LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def bars_for(
        self, token_address: str, network_id: str, from_ts: int, to_ts: int,
        resolution: str = "5",
    ) -> list[dict[str, Any]]:
        """شموع عملة داخل مدى زمنيّ، مرتّبة. الحقول الناقصة تُسقط صفّها —
        شمعة بلا h/l/c لا تفيد التوسيم ولا نفبرك لها قيماً."""
        rows = self._conn.execute(
            """SELECT ts, o, h, l, c FROM token_bars
               WHERE token_address = ? AND network_id = ? AND resolution = ?
                 AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (token_address, network_id, resolution, from_ts, to_ts),
        ).fetchall()
        return [
            dict(r) for r in rows
            if r["h"] is not None and r["l"] is not None and r["c"] is not None
        ]


def _with_compressed_raw(row: Mapping[str, Any]) -> dict[str, Any]:
    """نسخة من الصفّ مع ضغط حقل raw_json (إن وُجد). الاستخراج ينتج نصّ JSON
    خالصاً وقابلاً للاختبار؛ الضغط يبقى مسؤولية طبقة التخزين وحدها."""
    out = dict(row)
    raw = out.get("raw_json")
    if raw is not None and not isinstance(raw, (bytes, bytearray)):
        out["raw_json"] = encode_raw(raw)
    return out


# ترتيب أعمدة market_ticks و token_static — مصدر واحد للحقيقة يطابق schema.sql.
_TICK_COLUMNS = (
    "token_address", "network_id", "recorded_at", "source", "price_usd",
    "liquidity", "market_cap", "holders", "top10_holders_pct",
    "change_5m", "change_1h", "change_4h", "change_12h", "change_24h",
    "volume_5m", "volume_1h", "volume_4h", "volume_12h", "volume_24h",
    "txn_count_1h", "txn_count_4h", "txn_count_12h", "txn_count_24h",
    "buy_count_1h", "buy_count_4h", "buy_count_12h", "buy_count_24h",
    "sell_count_1h", "sell_count_4h", "sell_count_12h", "sell_count_24h",
    "unique_buys_1h", "unique_buys_4h", "unique_buys_12h", "unique_buys_24h",
    "unique_sells_1h", "unique_sells_4h", "unique_sells_12h", "unique_sells_24h",
    "circulating_supply", "total_supply", "raw_json",
)

# ترحيلات الأعمدة: (الجدول، العمود، تعريفه). تُطبَّق مرّة واحدة عند الإقلاع.
_COLUMN_MIGRATIONS = (
    ("watchlist", "is_control", "is_control INTEGER NOT NULL DEFAULT 0"),
    # حقول حجم الصفقة — تُملأ رجعياً من raw_json عبر backfill_sizes.py
    ("signal_events", "size_usd", "size_usd REAL"),
    ("signal_events", "in_amount", "in_amount REAL"),
    ("signal_events", "in_token_address", "in_token_address TEXT"),
    ("signal_events", "out_amount", "out_amount REAL"),
    ("signal_events", "token_amount", "token_amount REAL"),
    ("signal_events", "realized_pnl_usd", "realized_pnl_usd REAL"),
    # العدد الحقيقي للأطروحات — أُضيف بعد اكتشاف تشبّع العدّ عند 100
    ("token_social", "thesis_total", "thesis_total INTEGER"),
    ("token_social", "thesis_sampled", "thesis_sampled INTEGER"),
    ("token_social", "has_next_page", "has_next_page INTEGER"),
)

_BAR_COLUMNS = (
    "token_address", "network_id", "resolution", "ts", "o", "h", "l", "c", "v", "fetched_at",
)

_SIGNAL_COLUMNS = (
    "id", "token_address", "network_id", "ts", "recorded_at", "signal_type",
    "ticker", "price_usd", "fdv", "market_cap", "num_trades", "unique_traders",
    "minutes", "price_change_pct", "total_volume", "are_top_traders",
    "top_trader_ids_json", "top_trader_match_count", "buyers_best_rank",
    "buyer_id", "buyer_handle", "num_swaps", "is_first_buy", "buyer_pnl_pct",
    "avg_cost", "size_usd", "in_amount", "in_token_address", "out_amount",
    "token_amount", "realized_pnl_usd", "raw_json",
)

_OUTCOME_COLUMNS = (
    "kind", "key", "token_address", "network_id", "signal_type", "is_control",
    "is_independent", "entry_ts", "entry_px", "entry_lag_s",
    "max_gain_1h", "max_gain_4h", "max_gain_24h", "max_gain_48h",
    "max_drawdown_48h", "final_return_48h", "time_to_peak_h",
    "candles_48h", "last_bar_lag_h", "bars_truncated", "is_rug",
    "split", "status", "labeled_at",
)

_THESIS_COLUMNS = (
    "id", "token_address", "network_id", "created_at", "user_handle", "user_id",
    "num_likes", "num_replies", "equity", "trade_id", "comment", "fetched_at",
    "raw_json",
)

_SOCIAL_COLUMNS = (
    "token_address", "network_id", "recorded_at",
    "thesis_total", "thesis_sampled", "has_next_page", "thesis_count",
    "thesis_likes", "thesis_replies", "thesis_authors", "holder_authors",
    "newest_thesis_at", "raw_json",
)

_STATIC_COLUMNS = (
    "token_address", "network_id", "recorded_at", "name", "symbol", "decimals",
    "mintable", "freezable", "is_scam", "creator_address", "launchpad_name",
    "migrated", "graduation_percent", "twitter", "telegram", "website", "discord",
    "token_created_at", "raw_json",
)
