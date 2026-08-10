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
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._migrate()
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        with open(schema_path, "r", encoding="utf-8") as fh:
            self._conn.executescript(fh.read())
        self._backfill_watch_windows()
        self._quarantine_legacy_outcomes()
        self._conn.commit()

    def _quarantine_legacy_outcomes(self) -> None:
        """يعزل كل نتيجة watch أقدم من تصميم المقارنة الحالي."""
        from config import CONTROL_DESIGN_VERSION

        self._conn.execute(
            """UPDATE outcomes
                  SET analysis_eligible=0,
                      exclusion_reason='superseded_comparison_design'
                WHERE kind='watch'
                  AND (design_version IS NULL OR design_version < ?)""",
            (CONTROL_DESIGN_VERSION,),
        )

    def _backfill_watch_windows(self) -> None:
        """يحفظ نوافذ الإشارات القديمة فقط قبل اعتماد السجلّ immutable.

        الضابطة v1 حُذفت بقرار المشروع ولا يجوز إعادتها من watchlist قديمة؛
        وحدها نوافذ الإشارة غير الضابطة تُرحّل لأغراض التشغيل التاريخيّ.
        """
        self._conn.execute(
            """INSERT OR IGNORE INTO watch_windows(
                   token_address, network_id, first_seen_at, source,
                   watch_until, entry_signal_id, is_control, admission_price_usd,
                   design_version
               )
               SELECT token_address, network_id, first_seen_at, source,
                      watch_until, entry_signal_id, is_control, NULL, 1
                 FROM watchlist
                WHERE is_control=0"""
        )

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
        duplicate = self._conn.execute(
            """SELECT 1 FROM signal_events
                WHERE token_address=? AND COALESCE(network_id, '')=COALESCE(?, '')
                  AND ts=? AND signal_type=? LIMIT 1""",
            (row.get("token_address"), row.get("network_id"),
             row.get("ts"), row.get("signal_type")),
        ).fetchone()
        if duplicate:
            return False
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

    def recompute_bar_flags(
        self, token_address: str, network_id: str, resolution: str = "5"
    ) -> int:
        """يعيد حساب أعلام التشوّه لسلسلة عملة كاملة. يعيد عدد الصفوف المحدَّثة.

        لازم بعد كل إدراج: حكم الجار (`bar_context_flags`) يحتاج الشمعة التالية،
        والشمعة الأخيرة في أيّ دفعة بلا تالية بعد — فتُحكم عند وصولها. القيم
        الخام لا تُلمس، الأعلام فقط.
        """
        from extract import bar_context_flags  # استيراد موضعيّ: db لا يعتمد extract

        rows = self._conn.execute(
            "SELECT ts, o, h, l, c, h_suspect, l_suspect, c_suspect FROM token_bars "
            "WHERE token_address=? AND network_id=? AND resolution=? ORDER BY ts",
            (token_address, network_id, resolution),
        ).fetchall()
        if not rows:
            return 0
        series = [dict(r) for r in rows]
        import config
        ratio = (
            config.DAILY_BAR_WICK_MAX_RATIO
            if resolution == "1D" else config.BAR_WICK_MAX_RATIO
        )
        changes = [
            (h, low, c, token_address, network_id, resolution, b["ts"])
            for b, (h, low, c) in zip(
                series, bar_context_flags(series, max_ratio=ratio)
            )
            if (h, low, c) != (b["h_suspect"], b["l_suspect"], b["c_suspect"])
        ]
        if changes:
            with self.batch():
                self._conn.executemany(
                    "UPDATE token_bars SET h_suspect=?, l_suspect=?, c_suspect=? "
                    "WHERE token_address=? AND network_id=? AND resolution=? AND ts=?",
                    changes,
                )
        return len(changes)

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

    # --- activity_events (يكتبها backfill_activity.py فقط، بأثر رجعيّ) ---
    def insert_activity_events(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """يُدرج أحداث tradingActivity. idempotent حسب id — إعادة المشيّاط من
        نقطة الاستئناف تمرّ على المدرَج بلا أثر. يعيد عدد المُدرَج فعلاً."""
        if not rows:
            return 0
        cols = _ACTIVITY_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO activity_events({', '.join(cols)}) "
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

    def activity_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM activity_events").fetchone()
        return int(row["n"])

    def activity_event_tokens(self) -> list[dict[str, Any]]:
        """العملات ذات الأحداث القابلة للتوسيم مع مدى أزمنتها — لسحب الشموع."""
        rows = self._conn.execute(
            """SELECT token_address, network_id, MIN(ts) AS min_ts, MAX(ts) AS max_ts,
                      COUNT(*) AS n
                 FROM activity_events
                WHERE token_address IS NOT NULL AND ts IS NOT NULL
                GROUP BY token_address, network_id
                ORDER BY n DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def activity_events_for_token(self, token_address: str, network_id: str) -> list[dict[str, Any]]:
        """أحداث عملة مرتّبة زمنياً (لعناقيد سحب الشموع)."""
        rows = self._conn.execute(
            """SELECT id, ts FROM activity_events
                WHERE token_address=? AND network_id=? AND ts IS NOT NULL
                ORDER BY ts""",
            (token_address, network_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_activity_bars_state(
        self, token_address: str, network_id: str, status: str, candles: int, now_iso: str
    ) -> None:
        self._conn.execute(
            """INSERT INTO activity_bars_state(
                   token_address, network_id, last_fetch_at, last_status, candles, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   candles       = excluded.candles,
                   attempts      = activity_bars_state.attempts + 1""",
            (token_address, network_id, now_iso, status, candles),
        )
        self._commit()

    def activity_bars_state(self, token_address: str, network_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM activity_bars_state WHERE token_address=? AND network_id=?",
            (token_address, network_id),
        ).fetchone()
        return dict(row) if row else None

    def activities_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """أحداث نشاط نضجت ولم تُوسَم وشموع عملتها مسحوبة (status='ok').

        البوّابة الأخيرة حاسمة: التوسيم idempotent لا يُراجَع، فتوسيم حدث قبل
        وصول شموع عملته يكتب no_entry أبديّاً. `prev_ts` = ختم حدث النشاط
        السابق على نفس العملة (لعلم الاستقلال بنفس قاعدة الإشارات).
        """
        rows = self._conn.execute(
            """SELECT a.id, a.event_type, a.token_address, a.network_id, a.ts,
                      CAST(strftime('%s', a.ts) AS INTEGER) AS entry_epoch,
                      (SELECT MAX(p.ts) FROM activity_events p
                        WHERE p.token_address = a.token_address AND p.ts < a.ts)
                        AS prev_ts
                 FROM activity_events a
                WHERE a.ts IS NOT NULL
                  AND a.token_address IS NOT NULL
                  AND CAST(strftime('%s', a.ts) AS INTEGER) <= ?
                  AND NOT EXISTS (SELECT 1 FROM outcomes o
                                   WHERE o.kind = 'activity' AND o.key = a.id)
                  AND EXISTS (SELECT 1 FROM activity_bars_state s
                               WHERE s.token_address = a.token_address
                                 AND s.network_id = a.network_id
                                 AND s.last_status = 'ok')
                ORDER BY a.ts LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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

    # --- token_holders ---
    def insert_holders(self, row: Mapping[str, Any]) -> bool:
        """لقطة تركّز حيازة؛ المفتاح يشمل source فلا يطمس مصدرٌ الآخر."""
        cols = _HOLDERS_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_holders({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_holders_state(
        self, token_address: str, network_id: str, status: str,
        top10_pct: float | None, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO holders_fetch_state(
                   token_address, network_id, last_fetch_at, last_status,
                   top10_pct, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   top10_pct     = excluded.top10_pct,
                   attempts      = holders_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, top10_pct),
        )
        self._commit()

    # --- token_flow ---
    def insert_flow(self, row: Mapping[str, Any]) -> bool:
        """صفّ تدفّق تداول (شراء/بيع). نفس ردّ tokenDetails الذي يغذّي الحيازة."""
        cols = _FLOW_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO token_flow({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    # --- traders ---
    def upsert_trader(self, row: Mapping[str, Any]) -> None:
        """ملفّ متداول. الملفّ **يتغيّر** (متابعون، عدد صفقات) فنكتب فوقه —
        بخلاف token_static الثابت بطبعه."""
        cols = _TRADER_COLUMNS
        sql = (
            f"INSERT OR REPLACE INTO traders({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()

    def set_trader_state(self, trader_id: str, status: str, now_iso: str) -> None:
        self._conn.execute(
            """INSERT INTO traders_fetch_state(
                   trader_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, 1)
               ON CONFLICT(trader_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = traders_fetch_state.attempts + 1""",
            (trader_id, now_iso, status),
        )
        self._commit()

    def traders_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        min_events: int = 3,
    ) -> list[dict[str, Any]]:
        """المتداولون المستحقّون للجلب: **المتكرّرون وحدهم** (≥`min_events` حدثاً).

        مقيس: 5,572 مشترياً مميّزاً لكنّ 3,202 فقط بـ≥3 أحداث — ومن ظهر مرّة
        واحدة لا سلوك له نتعلّمه، فجلبه يستهلك ميزانية الدورة بلا مقابل.
        الترتيب: من لم يُجلَب قطّ أولاً، ثم الأكثر نشاطاً (أحداثه أكثر معلومة).

        `COALESCE(s.last_status,'') <> 'error'` إلزاميّ: بدونه يصير الشرط NULL
        لمن لا صفّ حالة له فيُستبعد إلى الأبد.
        """
        rows = self._conn.execute(
            """SELECT e.buyer_id AS trader_id, e.n AS event_count,
                      s.last_fetch_at, s.last_status
                 FROM (SELECT buyer_id, COUNT(*) n FROM signal_events
                        WHERE buyer_id IS NOT NULL AND buyer_id <> ''
                        GROUP BY buyer_id HAVING COUNT(*) >= ?) e
                 LEFT JOIN traders_fetch_state s ON s.trader_id = e.buyer_id
                WHERE (s.last_fetch_at IS NULL
                       OR (s.last_status='error' AND s.last_fetch_at < ?)
                       OR (COALESCE(s.last_status, '') <> 'error'
                           AND s.last_fetch_at < ?))
                ORDER BY s.last_fetch_at IS NOT NULL,
                         s.last_fetch_at,
                         e.n DESC
                LIMIT ?""",
            (min_events, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_static_protocol(
        self, token_address: str, network_id: str, protocol: str
    ) -> bool:
        """يملأ `dex_protocol` **حين يكون فارغاً فقط**.

        `upsert_static` هو INSERT OR IGNORE فلا يحدّث صفّاً قائماً، والبروتوكول
        لا يأتي إلّا من filterTokens (غائب من خام trending: صفر من 3,000) —
        فيلزم مسار تحديث ضيّق. شرط `IS NULL` يمنع الكتابة فوق قياس قائم.
        """
        cur = self._conn.execute(
            """UPDATE token_static SET dex_protocol=?
                WHERE token_address=? AND network_id=? AND dex_protocol IS NULL""",
            (protocol, token_address, network_id),
        )
        self._commit()
        return cur.rowcount > 0

    def holders_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
    ) -> list[dict[str, Any]]:
        """المراقَبات المستحقّة لقياس التركّز؛ الجديدة أولاً والإشارات قبل الضابطة."""
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.first_seen_at,
                      w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                 FROM watchlist w
                 LEFT JOIN holders_fetch_state s
                   ON s.token_address=w.token_address AND s.network_id=w.network_id
                WHERE w.active=1
                  AND (s.last_fetch_at IS NULL
                       OR (s.last_status='error' AND s.last_fetch_at < ?)
                       OR (COALESCE(s.last_status, '') <> 'error'
                           AND s.last_fetch_at < ?))
                ORDER BY s.last_fetch_at IS NOT NULL,
                         w.is_control,
                         s.last_fetch_at,
                         w.first_seen_at DESC
                LIMIT ?""",
            (error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- watchlist ---
    def upsert_watch(
        self,
        token_address: str,
        network_id: str,
        source: str,
        entry_signal_id: str | None,
        watch_hours: int,
        now_iso: str | None = None,
        admission_price_usd: float | None = None,
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
        if cur.rowcount > 0:
            # نافذة جديدة تحتاج دورة سحب جديدة؛ حالة no_data/طزاجة النافذة
            # السابقة لا يجوز أن تمنعها من الجدولة.
            self._conn.execute(
                "DELETE FROM bars_fetch_state WHERE token_address=? AND network_id=?",
                (token_address, network_id),
            )
            self._insert_watch_window(
                token_address, network_id, now, source, until, entry_signal_id, 0,
                admission_price_usd,
            )
        self._commit()
        return cur.rowcount > 0

    def admit_control(
        self,
        token_address: str,
        network_id: str,
        watch_hours: int,
        now_iso: str | None = None,
        admission_price_usd: float | None = None,
        source: str = "control",
        design_version: int = 2,
        admission_source: str | None = None,
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
            ) VALUES(?, ?, ?, ?, ?, NULL, 1, 1)""",
            (token_address, network_id, now, source, until),
        )
        if cur.rowcount > 0:
            self._insert_watch_window(
                token_address, network_id, now, source, until, None, 1,
                admission_price_usd, design_version, admission_source,
            )
        self._commit()
        return cur.rowcount > 0

    def add_signal_comparison_window(
        self,
        token_address: str,
        network_id: str,
        source: str,
        entry_signal_id: str,
        watch_hours: int,
        now_iso: str,
        admission_price_usd: float,
        design_version: int,
        admission_source: str | None = None,
    ) -> bool:
        """يضيف نافذة مقارنة مستقلة لإشارة من نفس كون الضابطة.

        لا يغيّر `watchlist`: التسجيل التشغيلي بدأ عند وصول الإشارة، أما هذه
        النافذة فتوثق لحظة ظهور العملة في trending/verified وسعرها القابل للرصد.
        """
        until = (
            datetime.fromisoformat(now_iso) + timedelta(hours=watch_hours)
        ).isoformat()
        existing = self._conn.execute(
            """SELECT token_address, network_id, first_seen_at
                 FROM watch_windows
                WHERE token_address=? AND network_id=? AND first_seen_at=?""",
            (token_address, network_id, now_iso),
        ).fetchone()
        if existing is not None:
            self._conn.execute(
                """DELETE FROM watch_windows
                    WHERE token_address=? AND network_id=? AND first_seen_at=?""",
                (token_address, network_id, now_iso),
            )
        self._insert_watch_window(
            token_address, network_id, now_iso, source, until, entry_signal_id,
            0, admission_price_usd, design_version, admission_source,
        )
        self._commit()
        return True

    def _insert_watch_window(
        self,
        token_address: str,
        network_id: str,
        first_seen_at: str,
        source: str,
        watch_until: str,
        entry_signal_id: str | None,
        is_control: int,
        admission_price_usd: float | None,
        design_version: int = 2,
        admission_source: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO watch_windows(
                   token_address, network_id, first_seen_at, source,
                   watch_until, entry_signal_id, is_control, admission_price_usd,
                   design_version, admission_source
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_address, network_id, first_seen_at, source,
             watch_until, entry_signal_id, is_control, admission_price_usd,
             design_version, admission_source),
        )

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
        """يعطّل النافذة بعد اكتمال سحب شموعها النهائيّ، لا عند الساعة 48 فوراً."""
        now = now_iso or utcnow_iso()
        cur = self._conn.execute(
            """UPDATE watchlist SET active=0
                WHERE active=1 AND watch_until <= ?
                  AND EXISTS (
                      SELECT 1 FROM bars_fetch_state s
                       WHERE s.token_address = watchlist.token_address
                         AND s.network_id = watchlist.network_id
                         AND ((s.last_status='ok' AND s.last_fetch_at >= watchlist.watch_until)
                              OR (s.last_status='no_data' AND s.attempts >= 3))
                  )""",
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
        values = {c: row.get(c) for c in cols}
        if values["design_version"] is None:
            values["design_version"] = 1
        if values["analysis_eligible"] is None:
            values["analysis_eligible"] = 0
        if values["exclusion_reason"] is None and values["analysis_eligible"] == 0:
            values["exclusion_reason"] = "legacy_or_non_phase1_outcome"
        cur = self._conn.execute(sql, values)
        self._commit()
        return cur.rowcount > 0

    def signals_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """إشارات نضجت نافذتها ولم تُوسَم بعد — الأقدم أوّلاً.

        زمن القرار هو `recorded_at`: أول لحظة صارت فيها الإشارة متاحة للنظام.
        استعمال `ts` الأصلي يجعل دخولاً متخيلاً قبل وصول حدث متأخر إلى ساعتين.
        `prev_ts` = وقت وصول القرار السابق على نفس العملة لحساب الاستقلال.
        """
        rows = self._conn.execute(
            """SELECT s.id, s.token_address, s.network_id, s.signal_type,
                      s.ts AS source_ts, s.recorded_at,
                      CAST(strftime('%s', s.recorded_at) AS INTEGER) AS entry_epoch,
                      (SELECT MAX(p.recorded_at) FROM signal_events p
                        WHERE p.token_address = s.token_address
                          AND p.network_id = s.network_id
                          AND p.recorded_at < s.recorded_at)
                        AS prev_ts
               FROM signal_events s
               WHERE s.recorded_at IS NOT NULL
                  AND CAST(strftime('%s', s.recorded_at) AS INTEGER) <= ?
                  AND NOT EXISTS (SELECT 1 FROM outcomes o
                                   WHERE o.kind = 'signal' AND o.key = s.id)
               ORDER BY s.recorded_at LIMIT ?""",
            (mature_before_epoch, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def watches_pending_label(self, mature_before_epoch: int, limit: int) -> list[dict[str, Any]]:
        """دخولات مراقبة (إشارة وضابطة) نضجت نافذتها ولم تُوسَم — للمقارنة
        على نوافذ مكتملة بدل النافذة الجارية."""
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, w.source, w.is_control,
                      w.first_seen_at, w.admission_price_usd, w.design_version,
                      CAST(strftime('%s', w.first_seen_at) AS INTEGER) AS entry_epoch,
                      w.token_address || ':' || w.network_id || ':' || w.first_seen_at
                        AS key
                 FROM watch_windows w
                WHERE CAST(strftime('%s', w.first_seen_at) AS INTEGER) <= ?
                  AND EXISTS (
                      SELECT 1 FROM bars_fetch_state s
                       WHERE s.token_address = w.token_address
                         AND s.network_id = w.network_id
                         AND ((s.last_status = 'ok' AND s.last_fetch_at >= w.watch_until)
                              OR (s.last_status = 'no_data' AND s.attempts >= 3))
                  )
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
        شمعة بلا h/l/c لا تفيد التوسيم ولا نفبرك لها قيماً.

        `h_suspect`/`l_suspect` تُمرَّر كما هي: الموسِّم يستبعد الذيل المعلَّم من
        القمّة/القاع ويبقي جسم الشمعة (o/c) صالحاً — ذيل مستحيل من المنبع
        (شوهد ×119 مليون) كان يسمّم max_gain بمليارات النسب المئوية.
        """
        rows = self._conn.execute(
            """SELECT ts, o, h, l, c, h_suspect, l_suspect, c_suspect FROM token_bars
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
    ("signal_events", "out_token_address", "out_token_address TEXT"),
    ("signal_events", "token_amount", "token_amount REAL"),
    ("signal_events", "realized_pnl_usd", "realized_pnl_usd REAL"),
    # العدد الحقيقي للأطروحات — أُضيف بعد اكتشاف تشبّع العدّ عند 100
    ("token_social", "thesis_total", "thesis_total INTEGER"),
    ("token_social", "thesis_sampled", "thesis_sampled INTEGER"),
    ("token_social", "has_next_page", "has_next_page INTEGER"),
    # أعلام الذيول المستحيلة — تُحسب رجعياً من o/h/l/c المخزّنة عبر
    # backfill_bar_flags.py (لا شبكة: الخام يكفي).
    ("token_bars", "h_suspect", "h_suspect INTEGER NOT NULL DEFAULT 0"),
    ("token_bars", "l_suspect", "l_suspect INTEGER NOT NULL DEFAULT 0"),
    ("token_bars", "c_suspect", "c_suspect INTEGER NOT NULL DEFAULT 0"),
    ("outcomes", "suspect_bars", "suspect_bars INTEGER"),
    # راية الحيّ/الرجعيّ — تفصل حِقبة البيانات بوضوح للتدريب (قرار المشروع:
    # الحيّ وحده)، لا تسريب حِقبة من نمط الغياب.
    ("training_rows", "is_live", "is_live INTEGER NOT NULL DEFAULT 0"),
    ("training_rows", "feature_version", "feature_version INTEGER NOT NULL DEFAULT 1"),
    ("training_rows", "ath_history_complete", "ath_history_complete INTEGER"),
    ("training_rows", "ath_history_days", "ath_history_days REAL"),
    ("watch_windows", "admission_price_usd", "admission_price_usd REAL"),
    ("watch_windows", "admission_source", "admission_source TEXT"),
    ("watch_windows", "design_version", "design_version INTEGER NOT NULL DEFAULT 1"),
    ("outcomes", "design_version", "design_version INTEGER NOT NULL DEFAULT 1"),
    ("outcomes", "analysis_eligible", "analysis_eligible INTEGER NOT NULL DEFAULT 0"),
    ("outcomes", "exclusion_reason", "exclusion_reason TEXT"),
    # التفاعل على حدث الإشارة — كان في الخام (100% تغطية) ولا يُستخرج.
    # يُملأ رجعياً من raw_json عبر backfill_engagement.py (لا شبكة).
    ("signal_events", "likes", "likes INTEGER"),
    ("signal_events", "views", "views INTEGER"),
    ("signal_events", "num_replies", "num_replies INTEGER"),
    ("signal_events", "pinned", "pinned INTEGER"),
    # إشارات الشرعية الخارجية — كذلك من الخام المحفوظ، بلا شبكة.
    ("token_static", "exchanges_count", "exchanges_count INTEGER"),
    ("token_static", "exchanges_json", "exchanges_json TEXT"),
    ("token_static", "cmc_id", "cmc_id TEXT"),
    ("token_static", "description", "description TEXT"),
    ("token_static", "description_len", "description_len INTEGER"),
    ("token_static", "has_banner", "has_banner INTEGER"),
    ("token_static", "has_image", "has_image INTEGER"),
    # تموضع حشد المنصّة من /hodlers/top. أُضيفت بعد قياس حيّ أثبت أنّ المصدر
    # لا يعطي نِسب معروض إطلاقاً (فلا top1_pct منه)، بل مراكز مستخدمي fomo.
    # الجدول قد يكون أُنشئ بالشكل الأول، فالترحيل يكمله بلا فقد بيانات.
    ("token_holders", "platform_holders", "platform_holders INTEGER"),
    ("token_holders", "platform_holders_listed", "platform_holders_listed INTEGER"),
    ("token_holders", "platform_value_usd", "platform_value_usd REAL"),
    ("token_holders", "platform_underwater", "platform_underwater INTEGER"),
    ("token_holders", "platform_median_hold_seconds",
     "platform_median_hold_seconds REAL"),
    ("token_holders", "platform_dev_holding", "platform_dev_holding INTEGER"),
    # ميزات الإصدار 4 على صفوف التدريب القائمة (20,303 صفّاً). الصفوف القديمة
    # تبقى NULL هنا — غائب ≠ صفر، والفارز يميّز feature_version.
    ("training_rows", "exchanges_count", "exchanges_count INTEGER"),
    ("training_rows", "listed_on_exchange", "listed_on_exchange INTEGER"),
    ("training_rows", "has_cmc_id", "has_cmc_id INTEGER"),
    ("training_rows", "description_len", "description_len INTEGER"),
    ("training_rows", "has_banner", "has_banner INTEGER"),
    ("training_rows", "chain_top10_pct", "chain_top10_pct REAL"),
    ("training_rows", "chain_holder_count", "chain_holder_count INTEGER"),
    ("training_rows", "holders_age_min", "holders_age_min REAL"),
    ("training_rows", "platform_holders", "platform_holders INTEGER"),
    ("training_rows", "platform_penetration", "platform_penetration REAL"),
    ("training_rows", "platform_underwater_ratio", "platform_underwater_ratio REAL"),
    ("training_rows", "platform_value_usd", "platform_value_usd REAL"),
    ("training_rows", "platform_median_hold_h", "platform_median_hold_h REAL"),
    ("training_rows", "platform_dev_holding", "platform_dev_holding INTEGER"),
    # صدارات المدد (v7): سقف المصدر 50 في الصدارة الأساسيّة وكل صيغ الترقيم
    # مُهمَلة بصمت، لكنّ /24h و/7d و/30d تعيد كلٌّ 100 فاتّحاد الأربع 214 متداولاً
    # (المطابقة 3.68% ← 15.26% على 7,200 حدثاً). لا سبيل لتعبئة الماضي: الأرشيف
    # حفظ totalPnL وحدها فلا تاريخ لرتب المدد — الصفوف القديمة تبقى NULL بحقّ،
    # وprune_dead_features يُسقط العائلة حتى تُقاس في نصفَي المجموعة (نمط حِقبة،
    # لا ميزة).
    ("signal_events", "top_trader_match_count_24h", "top_trader_match_count_24h INTEGER"),
    ("signal_events", "buyers_best_rank_24h", "buyers_best_rank_24h INTEGER"),
    ("signal_events", "top_trader_match_count_7d", "top_trader_match_count_7d INTEGER"),
    ("signal_events", "buyers_best_rank_7d", "buyers_best_rank_7d INTEGER"),
    ("signal_events", "top_trader_match_count_30d", "top_trader_match_count_30d INTEGER"),
    ("signal_events", "buyers_best_rank_30d", "buyers_best_rank_30d INTEGER"),
    ("signal_events", "top_trader_periods_matched", "top_trader_periods_matched INTEGER"),
    ("training_rows", "top_trader_match_count_24h", "top_trader_match_count_24h INTEGER"),
    ("training_rows", "buyers_best_rank_24h", "buyers_best_rank_24h INTEGER"),
    ("training_rows", "top_trader_match_count_7d", "top_trader_match_count_7d INTEGER"),
    ("training_rows", "buyers_best_rank_7d", "buyers_best_rank_7d INTEGER"),
    ("training_rows", "top_trader_match_count_30d", "top_trader_match_count_30d INTEGER"),
    ("training_rows", "buyers_best_rank_30d", "buyers_best_rank_30d INTEGER"),
    ("training_rows", "top_trader_periods_matched", "top_trader_periods_matched INTEGER"),
    ("training_rows", "top_trader_any_period", "top_trader_any_period INTEGER"),
    ("training_rows", "best_rank_any_period", "best_rank_any_period INTEGER"),
    # v8 — سدّ فجوة الجمع. ثلاثة أشياء كانت متاحة ولا تُجمع:
    # 1) بروتوكول الحوض: غائب من خام trending (صفر من 3,000) ويأتي من
    #    filterTokens وحده. الصفوف القائمة تُملأ حين نراها لاحقاً (set_static_protocol).
    ("token_static", "dex_protocol", "dex_protocol TEXT"),
    # 2) وسم الحدث: body.tag بقيمة وحيدة 'Top Trader' في 3.4% من 4,000 حدث ⇒
    #    الوجود هو المعلومة. الصفوف القديمة تبقى NULL (الخام يحفظها لو أُريد ملؤها).
    ("signal_events", "is_top_trader_tagged", "is_top_trader_tagged INTEGER"),
    # 3) ميزات التدفّق والدمج على صفوف التدريب — من tokenDetails المجلوب أصلاً.
    #    كلّها NULL قبل بدء الجمع، وprune_dead_features يُسقطها حتى تُقاس في
    #    نصفَي المجموعة (نمط حِقبة لا ميزة) — كما حدث لعائلة الحيازة.
    ("training_rows", "tick_rich_age_min", "tick_rich_age_min REAL"),
    ("training_rows", "flow_age_min", "flow_age_min REAL"),
    ("training_rows", "flow_buy_volume_5m", "flow_buy_volume_5m REAL"),
    ("training_rows", "flow_sell_volume_5m", "flow_sell_volume_5m REAL"),
    ("training_rows", "flow_net_volume_5m", "flow_net_volume_5m REAL"),
    ("training_rows", "flow_net_volume_1h", "flow_net_volume_1h REAL"),
    ("training_rows", "flow_net_volume_24h", "flow_net_volume_24h REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_5m",
     "flow_buy_sell_volume_ratio_5m REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_1h",
     "flow_buy_sell_volume_ratio_1h REAL"),
    ("training_rows", "flow_buy_sell_volume_ratio_24h",
     "flow_buy_sell_volume_ratio_24h REAL"),
    ("training_rows", "flow_buy_count_5m", "flow_buy_count_5m INTEGER"),
    ("training_rows", "flow_sell_count_5m", "flow_sell_count_5m INTEGER"),
    ("training_rows", "flow_unique_buys_5m", "flow_unique_buys_5m INTEGER"),
    ("training_rows", "flow_unique_sells_5m", "flow_unique_sells_5m INTEGER"),
    ("training_rows", "flow_buy_sell_count_ratio_5m",
     "flow_buy_sell_count_ratio_5m REAL"),
    ("training_rows", "flow_unique_ratio_5m", "flow_unique_ratio_5m REAL"),
    ("training_rows", "flow_trade_size_5m", "flow_trade_size_5m REAL"),
    ("training_rows", "flow_is_low_fees", "flow_is_low_fees INTEGER"),
    # ملحوظة: `dex_protocol` و`is_top_trader_tagged` **ليسا** هنا. هما عمودا
    # جمعٍ على token_static وsignal_events (وهناك مكانهما في الهجرة أعلاه)، ولا
    # يُنتجهما `build_features`؛ فعمودٌ لهما في training_rows يبقى NULL أبداً —
    # وهو بالضبط عطب top10_holders_pct (1.43 مليون صفّ فارغ). عمودُ صفوف تدريب
    # لا يُضاف إلّا ومعه مفتاحٌ في ROW_COLUMNS يكتبه.
)

_BAR_COLUMNS = (
    "token_address", "network_id", "resolution", "ts", "o", "h", "l", "c", "v",
    "h_suspect", "l_suspect", "c_suspect", "fetched_at",
)

_SIGNAL_COLUMNS = (
    "id", "token_address", "network_id", "ts", "recorded_at", "signal_type",
    "ticker", "price_usd", "fdv", "market_cap", "num_trades", "unique_traders",
    "minutes", "price_change_pct", "total_volume", "are_top_traders",
    "top_trader_ids_json", "top_trader_match_count", "buyers_best_rank",
    "top_trader_match_count_24h", "buyers_best_rank_24h",
    "top_trader_match_count_7d", "buyers_best_rank_7d",
    "top_trader_match_count_30d", "buyers_best_rank_30d",
    "top_trader_periods_matched",
    "buyer_id", "buyer_handle", "num_swaps", "is_first_buy", "buyer_pnl_pct",
    "avg_cost", "size_usd", "in_amount", "in_token_address", "out_amount",
    "out_token_address", "token_amount", "realized_pnl_usd", "likes", "views",
    "num_replies", "pinned", "is_top_trader_tagged", "raw_json",
)

# ترتيب أعمدة token_flow — يطابق schema.sql (بلا طبقة 12h: المصدر لا يعطيها).
_FLOW_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control",
    "buy_count_5m", "buy_count_1h", "buy_count_4h", "buy_count_24h",
    "sell_count_5m", "sell_count_1h", "sell_count_4h", "sell_count_24h",
    "buy_volume_5m", "buy_volume_1h", "buy_volume_4h", "buy_volume_24h",
    "sell_volume_5m", "sell_volume_1h", "sell_volume_4h", "sell_volume_24h",
    "unique_buys_5m", "unique_buys_1h", "unique_buys_4h", "unique_buys_24h",
    "unique_sells_5m", "unique_sells_1h", "unique_sells_4h", "unique_sells_24h",
    "is_low_fees", "raw_json",
)

# ترتيب أعمدة traders — يطابق schema.sql، ويطابق ما يعطيه /v2/users/{id}
# **مقيساً حيّاً** (26 مفتاحاً): لا ربح ولا نسبة نجاح في هذا الردّ إطلاقاً.
_TRADER_COLUMNS = (
    "trader_id", "recorded_at", "handle", "display_name", "followers_count",
    "following_count", "swap_count", "num_trades", "total_volume_usd",
    "avg_hold_seconds", "is_restricted", "is_private", "wallet_address",
    "evm_address", "twitter_url", "created_at", "raw_json",
)

_OUTCOME_COLUMNS = (
    "kind", "key", "token_address", "network_id", "signal_type", "is_control",
    "is_independent", "entry_ts", "entry_px", "entry_lag_s",
    "max_gain_1h", "max_gain_4h", "max_gain_24h", "max_gain_48h",
    "max_drawdown_48h", "final_return_48h", "time_to_peak_h",
    "candles_48h", "suspect_bars", "last_bar_lag_h", "bars_truncated", "is_rug",
    "split", "status", "labeled_at",
    "design_version", "analysis_eligible", "exclusion_reason",
)

_THESIS_COLUMNS = (
    "id", "token_address", "network_id", "created_at", "user_handle", "user_id",
    "num_likes", "num_replies", "equity", "trade_id", "comment", "fetched_at",
    "raw_json",
)

_ACTIVITY_COLUMNS = (
    "id", "event_type", "token_address", "network_id", "ts", "recorded_at",
    "user_id", "user_handle", "trade_id", "usd_amount", "price_usd",
    "market_cap", "fdv", "equity", "num_trades", "unique_traders", "minutes",
    "price_change_pct", "total_volume", "are_top_traders", "top_trader_ids_json",
    "ticker", "raw_json",
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
    "token_created_at", "exchanges_count", "exchanges_json", "cmc_id",
    "description", "description_len", "has_banner", "has_image",
    "dex_protocol", "raw_json",
)

_HOLDERS_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "source", "top10_pct", "holder_count",
    "platform_holders", "platform_holders_listed", "platform_value_usd",
    "platform_underwater", "platform_median_hold_seconds",
    "platform_dev_holding", "top_holders_json", "raw_json",
)
