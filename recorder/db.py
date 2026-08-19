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
import time
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


class StaleEVMState(RuntimeError):
    """تغيّرت حالة دفتر EVM أثناء نداء شبكة؛ لا يجوز تثبيت جوابه القديم."""


def utcnow_iso() -> str:
    """الوقت الحالي ISO-8601 UTC — الختم الزمني الموحّد للمسجّل."""
    return datetime.now(UTC).isoformat()


def encode_raw(raw: Any) -> bytes:
    """أي كائن (أو نصّ JSON جاهز) → BLOB مضغوط للتخزين في عمود raw_json."""
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    # بعض تعليقات المنبع تحمل نصف زوج UTF-16 مثل `\ud83d`. هذا ليس Unicode
    # صالحاً للـUTF-8، لكن تمثيله كـJSON escape يحفظ الخام ولا يسقط لقطة العملة.
    return zlib.compress(text.encode("utf-8", errors="backslashreplace"), _ZLIB_LEVEL)


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
        self._savepoint_counter = 0
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
        with open(schema_path, encoding="utf-8") as fh:
            script = fh.read()
        # سبعُ عمليّات تفتح القاعدة الآن، وإقلاعها قد يتزامن (إعادة تشغيل مهمّة،
        # أو دخول Windows). المخطّط يحوي `DROP VIEW IF EXISTS v; CREATE VIEW v`
        # لأنّ العرض يجب أن يُعاد بناؤه كي يرى الأعمدة الجديدة — وهذا الزوج غير
        # ذرّي بين عمليّتين: تُسقط A ثم تُسقط B ثم تنشئ A، فيفشل إنشاء B بـ
        # "view ... already exists". حدث فعلاً: FomoBuildRows مات عند الإقلاع
        # وبقي ميّتاً 13 ساعة (2026-08-13، 02:09 ← 15:12) بلا سطر في سجلّه
        # الدوريّ، فالفشل كان في سجلّ الإقلاع وحده. النافذة أجزاء من الثانية
        # فإعادة المحاولة تكفي: الجارّ يكون قد أكمل.
        for attempt in range(3):
            try:
                self._conn.executescript(script)
                break
            except sqlite3.OperationalError as exc:
                if "already exists" not in str(exc) or attempt == 2:
                    raise
                time.sleep(0.4 * (attempt + 1))
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
        if self._batching:
            self._savepoint_counter += 1
            savepoint = f"batch_{self._savepoint_counter}"
            self._conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        owns_transaction = not self._conn.in_transaction
        savepoint: str | None = None
        self._batching = True
        try:
            # احجز الكاتب قبل أي فحص حالة داخل الدفعة. بدء المعاملة عند أول
            # INSERT يترك نافذة بين SELECT والكتابة يستطيع فيها reset/عامل آخر
            # تغيير المؤشر، فتُطبّق نفس السجلات مرتين.
            if owns_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._savepoint_counter += 1
                savepoint = f"batch_{self._savepoint_counter}"
                self._conn.execute(f"SAVEPOINT {savepoint}")
            yield
            if owns_transaction:
                self._conn.commit()
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            if owns_transaction:
                self._conn.rollback()
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
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

    def note_error(self, key: str, value: str) -> bool:
        """ختمٌ دفتريّ لا يجوز أن يُسقط مَن يكتبه. يعيد True إن كُتب.

        `set_meta` العاديّة تُستعمل لحالةٍ يُعتمد عليها (`schema_version`، أختام
        الدورات، جيل الدفتر) فيجب أن يُسمَع فشلها. أمّا أسطر `last_error_*` فهي
        **وصفٌ لفشلٍ وقع أصلاً**، وتُكتب من داخل معالج الاستثناء — فإن كانت
        القاعدة هي المورد المتعطّل رفعت هي أيضاً، فأسقطت المعالجَ ومعه ما بقي
        من الدورة، ثم أسقطت درع الحلقة نفسه. قِيس 2026-08-17: `database is
        locked` بهذا الطريق أخرج المسجّل بالرمز 1 فبقيت المهمّة `Ready` ثلاث
        ساعات صامتة. فقدُ سطرٍ وصفيّ أرخص من فقد الدورة، والقياس لا يُكتب بهذه.
        """
        try:
            self.set_meta(key, value)
            return True
        except Exception:  # noqa: BLE001 — دفترٌ لا قياس
            return False

    def evm_ledger_generation(self) -> int:
        value = self.get_meta("evm_ledger_generation")
        return int(value) if value and value.isdigit() else 0

    def assert_evm_ledger_generation(self, expected: int) -> None:
        current = self.evm_ledger_generation()
        if current != int(expected):
            raise StaleEVMState(
                f"تغيّر جيل دفتر EVM أثناء الدورة: {expected} -> {current}"
            )

    def assert_evm_cursor(self, network_id: str, expected_block: int | None) -> None:
        row = self.evm_cursor(network_id)
        current = int(row["last_block"]) if row is not None else None
        if current != expected_block:
            raise StaleEVMState(
                f"تغيّر مؤشر EVM [{network_id}] أثناء الجلب: "
                f"{expected_block} -> {current}"
            )

    def assert_evm_backfill_state(
        self, network_id: str, token_address: str,
        expected_status: str | None, expected_from: int | None,
        expected_to: int | None,
    ) -> None:
        row = self.evm_backfill_state(network_id, token_address)
        current = (
            None if row is None else
            (row.get("status"), row.get("from_block"), row.get("to_block"))
        )
        expected = (
            None if expected_status is None and expected_from is None and expected_to is None
            else (expected_status, expected_from, expected_to)
        )
        if current != expected:
            raise StaleEVMState(
                f"تغيّرت حالة تعبئة EVM للعملة {token_address} أثناء الجلب"
            )

    def bump_evm_ledger_generation(self) -> int:
        current = self.evm_ledger_generation() + 1
        self.set_meta("evm_ledger_generation", str(current))
        return current

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
                series, bar_context_flags(series, max_ratio=ratio), strict=True
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

    def social_fetch_due(
        self, limit: int, stale_before_iso: str,
        error_stale_before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """العملات المستحقّة للقطة اجتماعية — الأقدم سحباً أوّلاً.

        بخلاف الشموع لا نستبعد العملة الفارغة: غياب النقاش **إشارة بذاته**
        وتغيّره عبر الزمن هو المطلوب، فلا معنى لإسقاط عملة صامتة اليوم.
        """
        error_stale = error_stale_before_iso or stale_before_iso
        rows = self._conn.execute(
            """SELECT w.token_address, w.network_id, s.last_fetch_at
               FROM watchlist w
               LEFT JOIN social_fetch_state s
                  ON s.token_address = w.token_address AND s.network_id = w.network_id
               WHERE w.active = 1
                 AND (s.last_fetch_at IS NULL
                      OR (s.last_status='error' AND s.last_fetch_at < ?)
                      OR (COALESCE(s.last_status, '') <> 'error'
                          AND s.last_fetch_at < ?))
               ORDER BY s.last_fetch_at IS NOT NULL, s.last_fetch_at
               LIMIT ?""",
            (error_stale, stale_before_iso, limit),
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
                         OR (COALESCE(s.last_status, '') NOT IN ('error', 'unsupported')
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

    # --- chain_concentration (قياس السلسلة المباشر) ---
    def insert_chain_concentration(self, row: Mapping[str, Any]) -> bool:
        """لقطة تركّز من السلسلة. المفتاح (عنوان، شبكة، وقت) فالتكرار لا يضرّ."""
        cols = _CHAIN_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO chain_concentration({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        payload = {c: row.get(c) for c in cols}
        # صفر لا NULL: العمود `NOT NULL DEFAULT 0` وكاتبو الصفوف ثلاثة (سولانا،
        # دفتر EVM الحيّ، الإعادة الرجعيّة) — والتطبيع هنا يعفيهم من تذكّره،
        # وNULL صريح من أيّهم كان سيرفع `NOT NULL constraint failed`.
        payload["is_replay"] = 1 if payload.get("is_replay") else 0
        cur = self._conn.execute(sql, _with_compressed_raw(payload))
        self._commit()
        return cur.rowcount > 0

    def set_chain_state(
        self, token_address: str, network_id: str, status: str,
        top1_pct: float | None, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO chain_fetch_state(
                   token_address, network_id, last_fetch_at, last_status,
                   top1_pct, attempts)
               VALUES(?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   top1_pct      = excluded.top1_pct,
                   attempts      = chain_fetch_state.attempts + 1""",
            (token_address, network_id, now_iso, status, top1_pct),
        )
        self._commit()

    def chain_fetch_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """المراقَبات المستحقّة لقياس السلسلة — **من الشبكات المدعومة وحدها**.

        الشبكات تُمرَّر ولا تُقرأ من `config` هنا: هذه الوحدة بلا معرفة بالمصادر
        (انظر مقدّمة الملفّ) فتبقى قابلة للاختبار بقاعدة مؤقّتة. وقائمة فارغة
        تعيد لا شيء بدل أن تعني «كل الشبكات» — الصمت أصدق من مسح شبكة لا
        يعمل عليها المصدر.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN chain_fetch_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def evm_snapshot_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """عملات EVM ذات دفتر مكتمل والمستحقّة للقطة، مع التصفية قبل `LIMIT`."""
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  JOIN evm_backfill_state b
                    ON b.token_address=w.token_address AND b.network_id=w.network_id
                   AND b.status='done'
                  LEFT JOIN chain_fetch_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- chain_authority (الطبقة البطيئة: صلاحيات وقابليّة تعديل) ---
    def insert_chain_authority(self, row: Mapping[str, Any]) -> bool:
        """لقطة صلاحيات. صفّ لكل قياس لا صفّ واحد للعملة: شطبُ صلاحية السكّ
        **حدث** يقع وسط النافذة، وصفّ واحد يُحدَّث فوق نفسه يمحو تاريخه."""
        cols = _CHAIN_AUTH_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO chain_authority({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_chain_auth_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO chain_auth_state(
                   token_address, network_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = chain_auth_state.attempts + 1""",
            (token_address, network_id, now_iso, status),
        )
        self._commit()

    def chain_auth_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """نفس منطق `chain_fetch_due` على جدول الحالة الساعيّ.

        استعلام منفصل لا معامل `table` مُصاغ في النصّ: الجدول لا يُبنى من مُدخل
        أبداً، وتكرار عشرة أسطر أرخص من فتح باب حقن.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN chain_auth_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- طبقة EVM: دفتر الأرصدة ومؤشّر الكتل ---
    def evm_cursor(self, network_id: str) -> dict[str, Any] | None:
        """مؤشّر كتل الشبكة، أو None إن لم يُبنَ بعد."""
        row = self._conn.execute(
            "SELECT * FROM evm_block_cursor WHERE network_id=?", (str(network_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_cursor(
        self, network_id: str, last_block: int, now_iso: str,
        status: str, logs_applied: int = 0, last_error: str | None = None,
    ) -> None:
        """يحرّك المؤشّر. `logs_applied` يُجمَع تراكميّاً لا يُستبدل — رقم الدورة
        الواحدة بلا معنى تشخيصيّ، والمجموع يقول هل الطبقة تعمل أصلاً."""
        self._conn.execute(
            """INSERT INTO evm_block_cursor(
                   network_id, last_block, last_run_at, last_status,
                   logs_applied, last_error)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id) DO UPDATE SET
                   last_block   = excluded.last_block,
                   last_run_at  = excluded.last_run_at,
                   last_status  = excluded.last_status,
                   logs_applied = evm_block_cursor.logs_applied + excluded.logs_applied,
                   last_error   = excluded.last_error""",
            (str(network_id), int(last_block), now_iso, status,
             int(logs_applied), last_error),
        )
        self._commit()

    def evm_apply_transfers(
        self, network_id: str, token_address: str,
        deltas: Mapping[str, tuple[int, ...]], now_iso: str,
        allow_negative: Sequence[str] = (),
    ) -> int:
        """يطبّق تغييرات أرصدة عملة واحدة. `deltas` = عنوان ⇒ (تغيّر، رقم كتلة).

        التغيّر **موقَّع** ويُجمَع على الرصيد المخزَّن بحساب بايثون لا SQL: القيم
        uint256 تتجاوز 64 بتّاً فـ`balance_hex + ?` في SQLite يفيض بصمت. القراءة
        والكتابة في معاملة واحدة عبر `batch()` من المُنادي.

        الرصيد السالب مستحيل لعنوان عاديّ في ERC-20 صحيح؛ ظهوره يعني أنّ سجلاً
        فُقد أو تكرّر، فيُرفع خطأ وتُرجَع المعاملة بدل تخزين دفتر يبدو سليماً.
        وحدها عناوين السكّ/الحرق الممرّرة في `allow_negative` يجوز أن تنزل تحت
        الصفر: رصيدها لا يدخل اللقطة أصلاً، فنثبّته عند صفر بلا إخفاء فساد حائز.
        """
        if not deltas:
            return 0
        token = token_address.lower()
        net = str(network_id)
        allowed = {str(a).lower() for a in allow_negative}
        holders = list(deltas)
        variable_limit = self._conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        chunk_size = max(1, int(variable_limit) - 2)
        current: dict[str, tuple[str, int | None]] = {}
        for offset in range(0, len(holders), chunk_size):
            chunk = holders[offset:offset + chunk_size]
            marks = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                f"""SELECT holder_address, balance_hex, first_seen_block
                      FROM evm_balances
                     WHERE network_id=? AND token_address=?
                       AND holder_address IN ({marks})""",
                (net, token, *chunk),
            ).fetchall()
            current.update({
                row["holder_address"]: (row["balance_hex"], row["first_seen_block"])
                for row in rows
            })
        rows = []
        for holder, change in deltas.items():
            delta, block = change[:2]
            received_block = change[2] if len(change) > 2 else None
            prev_hex, first_block = current.get(holder, (None, None))
            prev = int(prev_hex, 16) if prev_hex else 0
            new = prev + int(delta)
            if new < 0:
                if holder.lower() not in allowed:
                    raise ValueError(
                        f"رصيد EVM سالب للعنوان {holder} في {token} [{net}]"
                    )
                new = 0
            # أوّل استلام: تُسجَّل الكتلة مرّة واحدة ولا تُحدَّث بعدها — «حائز جديد»
            # يعني أوّل دخول لا آخر حركة.
            if first_block is None:
                if received_block is not None:
                    first_block = int(received_block)
                elif delta > 0:
                    first_block = int(block)
            rows.append((net, token, holder, f"{new:064x}",
                         first_block, int(block), now_iso))
        self._conn.executemany(
            """INSERT INTO evm_balances(
                   network_id, token_address, holder_address, balance_hex,
                   first_seen_block, updated_block, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id, token_address, holder_address) DO UPDATE SET
                   balance_hex      = excluded.balance_hex,
                   first_seen_block = COALESCE(evm_balances.first_seen_block,
                                               excluded.first_seen_block),
                   updated_block    = excluded.updated_block,
                   updated_at       = excluded.updated_at""",
            rows,
        )
        self._commit()
        return len(rows)

    def evm_top_balances(
        self, network_id: str, token_address: str, limit: int,
        exclude: Sequence[str] = (),
    ) -> list[tuple[str, int]]:
        """أعلى `limit` رصيداً بترتيب تنازليّ.

        الترتيب معجميّ على نصّ محشوّ بعرض 64 ⇒ مطابق للترتيب العدديّ، فالفهرس
        `idx_evm_bal_rank` يخدمه بلا فرز. الرصيد صفر يُستثنى: عنوان باع كل شيء
        يبقى صفّه في الدفتر (تاريخه معلومة) لكنّه ليس حائزاً.
        """
        net, token = str(network_id), token_address.lower()
        params: list[Any] = [net, token]
        clause = ""
        skip = [a.lower() for a in exclude]
        if skip:
            clause = f" AND holder_address NOT IN ({', '.join('?' for _ in skip)})"
            params.extend(skip)
        params.append(int(limit))
        rows = self._conn.execute(
            f"""SELECT holder_address, balance_hex FROM evm_balances
                 WHERE network_id=? AND token_address=?
                   AND balance_hex <> '{'0' * 64}'{clause}
                 ORDER BY balance_hex DESC
                 LIMIT ?""",
            params,
        ).fetchall()
        return [(r["holder_address"], int(r["balance_hex"], 16)) for r in rows]

    def evm_ledger_stats(
        self, network_id: str, token_address: str, exclude: Sequence[str] = (),
    ) -> dict[str, Any]:
        """عدد الحائزين والمعروض المتداول من الدفتر — بنداء SQL واحد.

        `supply` هنا مجموع الأرصدة الحيّة لا `totalSupply()` من العقد: عناوين
        الحرق تُستثنى، فالنسب تُحسب على ما يمكن بيعه فعلاً. عملة حُرق نصفها
        تظهر بتركّز حقيقيّ لا مخفَّف بالنصف الميّت.
        """
        net, token = str(network_id), token_address.lower()
        params: list[Any] = [net, token]
        clause = ""
        skip = [a.lower() for a in exclude]
        if skip:
            clause = f" AND holder_address NOT IN ({', '.join('?' for _ in skip)})"
            params.extend(skip)
        rows = self._conn.execute(
            f"""SELECT balance_hex FROM evm_balances
                 WHERE network_id=? AND token_address=?
                   AND balance_hex <> '{'0' * 64}'{clause}""",
            params,
        ).fetchall()
        total = 0
        for r in rows:
            total += int(r["balance_hex"], 16)
        return {"holder_count": len(rows), "supply": total}

    def evm_new_holders_since(
        self, network_id: str, token_address: str, since_block: int,
    ) -> int:
        """كم عنواناً استلم العملة أوّل مرّة بعد كتلة معيّنة.

        هذا ما لا يعطيه أي مزوّد: كلّهم لقطة بلا تاريخ دخول. يُحسب من
        `first_seen_block` وحده فلا يكلّف نداءً.
        """
        row = self._conn.execute(
            """SELECT COUNT(*) c FROM evm_balances
                WHERE network_id=? AND token_address=? AND first_seen_block > ?""",
            (str(network_id), token_address.lower(), int(since_block)),
        ).fetchone()
        return int(row["c"] or 0)

    def evm_backfill_state(
        self, network_id: str, token_address: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM evm_backfill_state
                WHERE network_id=? AND token_address=?""",
            (str(network_id), token_address.lower()),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_backfill_state(
        self, network_id: str, token_address: str, status: str, now_iso: str,
        from_block: int | None = None, to_block: int | None = None,
        transfers: int | None = None, calls: int | None = None,
        last_error: str | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_backfill_state(
                   network_id, token_address, status, from_block, to_block,
                   transfers, calls, last_try_at, last_error)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(network_id, token_address) DO UPDATE SET
                   status      = excluded.status,
                   from_block  = COALESCE(excluded.from_block,
                                          evm_backfill_state.from_block),
                   to_block    = COALESCE(excluded.to_block,
                                          evm_backfill_state.to_block),
                   transfers   = COALESCE(excluded.transfers,
                                          evm_backfill_state.transfers),
                   calls       = COALESCE(excluded.calls, evm_backfill_state.calls),
                   last_try_at = excluded.last_try_at,
                   last_error  = excluded.last_error""",
            (str(network_id), token_address.lower(), status, from_block, to_block,
             transfers, calls, now_iso, last_error),
        )
        self._commit()

    def evm_watched(self, networks: Sequence[str]) -> list[dict[str, Any]]:
        """كل المراقَبات النشطة على شبكات EVM المفعَّلة، مع حالة تعبئتها.

        صفٌّ واحد يجمع المراقبة والتعبئة: الطبقة تحتاج الاثنين في كل دورة (من
        يُطبَّق عليه السجلّ، ومن ينتظر تعبئة)، ونداءان يفترقان بينهما.
        قائمة شبكات فارغة تعيد لا شيء — لا «كل الشبكات».
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control,
                       b.status AS backfill_status, b.from_block, b.to_block,
                       b.transfers AS backfill_transfers,
                       b.calls AS backfill_calls,
                       b.last_try_at AS backfill_last_try_at
                  FROM watchlist w
                  LEFT JOIN evm_backfill_state b
                    ON b.token_address=w.token_address AND b.network_id=w.network_id
                 WHERE w.active=1 AND w.network_id IN ({marks})
                 ORDER BY w.is_control, w.first_seen_at DESC""",
            nets,
        ).fetchall()
        return [dict(r) for r in rows]

    def assert_evm_done_tokens(
        self, network_id: str, expected_tokens: Sequence[str],
    ) -> None:
        current = sorted(
            str(row["token_address"]).lower()
            for row in self.evm_watched([network_id])
            if (row.get("backfill_status") or "") == "done"
        )
        expected = sorted(str(token).lower() for token in expected_tokens)
        if current != expected:
            raise StaleEVMState(
                f"تغيّرت مجموعة دفاتر EVM المكتملة [{network_id}] أثناء الجلب"
            )

    def evm_replay_windows(
        self, token_address: str, network_id: str,
    ) -> list[dict[str, str]]:
        rows = self._conn.execute(
            """SELECT first_seen_at, watch_until FROM watch_windows
                WHERE token_address=? AND network_id=? ORDER BY first_seen_at""",
            (token_address.lower(), str(network_id)),
        ).fetchall()
        return [
            {"first_seen_at": str(row["first_seen_at"]),
             "watch_until": str(row["watch_until"])}
            for row in rows
        ]

    def assert_evm_replay_windows(
        self, token_address: str, network_id: str,
        expected_windows: Sequence[Mapping[str, Any]],
    ) -> None:
        expected = sorted(
            (str(window["first_seen_at"]), str(window["watch_until"]))
            for window in expected_windows
        )
        current = sorted(
            (window["first_seen_at"], window["watch_until"])
            for window in self.evm_replay_windows(token_address, network_id)
        )
        if current != expected:
            raise StaleEVMState(
                f"تغيّرت نوافذ replay للعملة {token_address} أثناء الجلب"
            )

    def evm_training_rebuild_state(self) -> tuple[int, str, str]:
        return (
            self.evm_ledger_generation(),
            self.get_meta("evm_ledger_rebuild_required") or "0",
            self.get_meta("evm_training_rebuild_started") or "0",
        )

    def assert_evm_training_rebuild_state(
        self, expected: tuple[int, str, str],
    ) -> None:
        if self.evm_training_rebuild_state() != expected:
            raise StaleEVMState("تغيّرت حالة إعادة بناء التدريب أثناء حساب الدفعة")

    # --- الإعادة الرجعيّة (evm_replay) ---
    def evm_replay_targets(self, networks: Sequence[str]) -> list[dict[str, Any]]:
        """كل نافذة مراقبة على شبكات الإعادة — **المنتهية والنشطة معاً**.

        `active=1` غير مشروط هنا بخلاف `evm_watched`: العملة المنتهية هي بالضبط
        من فاتنا قياسها (طبقة EVM وُلدت بعدها)، وصفوف تدريبها موجودة بانتظار
        أعمدتها. وقائمة شبكات فارغة تعيد لا شيء.

        الأقدم أوّلاً: تلك أبعد ما تكون عن التغطية الحيّة فلا تنافسها.
        """
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.watch_until, w.entry_signal_id, w.is_control,
                       COALESCE(l.active, 0) AS active,
                       r.status AS replay_status, r.last_try_at AS replay_last_try_at,
                       r.snapshots AS replay_snapshots,
                       r.from_block AS replay_from_block,
                       r.to_block AS replay_to_block,
                       r.transfers AS replay_transfers,
                       r.calls AS replay_calls,
                       r.checkpoint_json AS replay_checkpoint_json,
                       r.revision AS replay_revision
                  FROM watch_windows w
                  LEFT JOIN watchlist l
                    ON l.token_address=w.token_address AND l.network_id=w.network_id
                  LEFT JOIN evm_replay_state r
                    ON r.token_address=w.token_address AND r.network_id=w.network_id
                 WHERE w.network_id IN ({marks})
                 ORDER BY w.first_seen_at""",
            nets,
        ).fetchall()
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            key = (str(row["token_address"]).lower(), str(row["network_id"]))
            current = grouped.get(key)
            window = {
                "first_seen_at": row["first_seen_at"],
                "watch_until": row["watch_until"],
            }
            if current is None:
                row["replay_windows"] = [window]
                grouped[key] = row
                continue
            current["replay_windows"].append(window)
            current["first_seen_at"] = min(
                current["first_seen_at"], row["first_seen_at"]
            )
            current["watch_until"] = max(current["watch_until"], row["watch_until"])
            current["is_control"] = min(
                int(current.get("is_control") or 0), int(row.get("is_control") or 0)
            )
        return list(grouped.values())

    def evm_replay_state(
        self, token_address: str, network_id: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM evm_replay_state
                WHERE token_address=? AND network_id=?""",
            (token_address.lower(), str(network_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def set_evm_replay_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
        from_block: int | None = None, to_block: int | None = None,
        transfers: int | None = None, snapshots: int | None = None,
        calls: int | None = None, balance_check: str | None = None,
        last_error: str | None = None, checkpoint: Any = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_replay_state(
                   token_address, network_id, status, from_block, to_block,
                   transfers, snapshots, calls, balance_check, last_try_at,
                   last_error, checkpoint_json, revision)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   status        = excluded.status,
                   from_block    = COALESCE(excluded.from_block,
                                            evm_replay_state.from_block),
                   to_block      = COALESCE(excluded.to_block,
                                            evm_replay_state.to_block),
                   transfers     = COALESCE(excluded.transfers,
                                            evm_replay_state.transfers),
                   snapshots     = COALESCE(excluded.snapshots,
                                            evm_replay_state.snapshots),
                   calls         = COALESCE(excluded.calls, evm_replay_state.calls),
                   balance_check = COALESCE(excluded.balance_check,
                                            evm_replay_state.balance_check),
                   last_try_at   = excluded.last_try_at,
                   last_error    = excluded.last_error,
                   checkpoint_json = excluded.checkpoint_json,
                   revision = evm_replay_state.revision + 1""",
            (token_address.lower(), str(network_id), status, from_block, to_block,
             transfers, snapshots, calls, balance_check, now_iso, last_error,
             encode_raw(checkpoint) if checkpoint is not None else None),
        )
        self._commit()

    def mark_evm_replay_error(
        self, token_address: str, network_id: str, now_iso: str, error: str,
    ) -> None:
        """يسجّل خطأً عابراً مع إبقاء checkpoint ونقطة الاستئناف السابقة."""
        self._conn.execute(
            """INSERT INTO evm_replay_state(
                   token_address, network_id, status, last_try_at, last_error)
               VALUES(?, ?, 'error', ?, ?)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   status='error', last_try_at=excluded.last_try_at,
                   last_error=excluded.last_error,
                   revision=evm_replay_state.revision + 1""",
            (token_address.lower(), str(network_id), now_iso, error),
        )
        self._commit()

    def stale_evm_replay_verdict(self, token_address: str, network_id: str) -> None:
        """نافذةٌ جديدة تُبطل الحكمَ وحدَه — والمشيُ يبقى.

        كان هذا حذفاً للصفّ كلِّه، والفرقُ بين الحذف وهذا هو الفرقُ بين حقيقتين
        خُلطتا في عمودٍ واحد: `status` جوابُ «هل غطّيتُ كلَّ نوافذ العملة؟» وهو
        يبطل فعلاً بنافذةٍ جديدة، أمّا `from_block`/`to_block`/`checkpoint_json`
        فجوابُ «إلى أين بلغ مشيي في السلسلة؟» وهو عن الكتل لا عن النوافذ، فلا
        تُبطله نافذةٌ أُضيفت. والحذفُ كان يرمي الثاني مع الأوّل.

        وثمنُ ذلك مقيسٌ لا متوقّع: العملةُ الساخنة تُشار إليها كلَّ دقائق، فصفُّ
        حالتها يُحذف أسرعَ من أن يُكتب — واحدةٌ على Base لها 522 نافذة، وواحدةٌ
        1440 — فتبدأ الإعادةُ من النشأة كلَّ مرّة ولا تبلغ أوّل لقطةٍ أبداً. ذلك
        سببُ أنّ 51 عملةً على Base كانت `partial` بمراجعةٍ = 1 ومدًى = ‎−1‎:
        محاولةٌ واحدةٌ بعد كلّ حذف، بلا تقدّم، إلى الأبد.

        والأسوأُ أنّ الحذفَ كان يُبطل الحارسَ الموضوع لهذا بعينه: سقفُ
        `EVM_REPLAY_TOKEN_CALL_CAP` يُجمَع من `calls` في الصفّ، فمحوُ الصفّ يصفّر
        العدّاد — فعملةٌ لا تكتمل أبداً لا تبلغ سقفَها أبداً. وبإبقاء الصفّ يتراكم
        العدّادُ فتُحال إلى `budget` وتخرج من الطريق.

        و`window` ليست في `FINAL_STATUSES` فالجدولةُ لا تتغيّر: تُنتقى كما كانت
        تُنتقى وهي بلا صفّ. وهي في مجموعةِ استئناف الـcheckpoint في
        `evm_replay._replay_token` — وبغير ذلك يُقرأ الصفُّ ويُهمَل مشيُه.

        و`budget` وحدَها تُستثنى. هي ليست حكماً على التغطية بل قراراً بوقف
        الإنفاق: بلغت العملةُ `EVM_REPLAY_TOKEN_CALL_CAP` فتوقّفت بنقطة استئناف
        محفوظة. وإبطالُ حكمها يعيدها إلى الطابور فتُنتقى، وتمشي شوطاً، ويعيدها
        السقفُ إلى `budget` — بكلفةِ انتقاءٍ كامل لكلّ نافذة. والعملةُ التي تبلغ
        السقفَ هي بطبيعتها العملةُ الحارّة صاحبةُ مئات النوافذ (522 لإحدى عملات
        Base)، فذلك يُبطل السقفَ ثانيةً على مهل — وهو عينُ ما استعاده هذا
        التغيير. فتبقى `budget` نهائيّةً حتى يُرفع السقفُ أو يُطلَب `--redo`،
        وهو ما يقوله تعليقُ السقف في `config` أصلاً.

        والمراجعةُ تُزاد: تشغيلٌ جارٍ يحمل لقطةً أقدم يسقط في `StaleEVMState`
        فيُترك للدورة التالية، بدل أن يكتب فوق نافذةٍ لم يرها. ولا `_commit` هنا:
        النداءُ من داخل معاملة الإدخال، والالتزامُ لها.
        """
        self._conn.execute(
            """UPDATE evm_replay_state
                  SET status = 'window', revision = revision + 1
                WHERE token_address = ? AND network_id = ?
                  AND status <> 'budget'""",
            (token_address.lower(), str(network_id)),
        )

    def reset_evm_replay_token(self, token_address: str, network_id: str) -> None:
        """يمحو صفوف وحالة replay لعملة واحدة كي يكون `--redo` إعادة حقيقية."""
        token, net = token_address.lower(), str(network_id)
        with self.batch():
            self._conn.execute(
                """DELETE FROM chain_concentration
                    WHERE token_address=? AND network_id=? AND is_replay=1""",
                (token, net),
            )
            self._conn.execute(
                "DELETE FROM evm_replay_state WHERE token_address=? AND network_id=?",
                (token, net),
            )

    def assert_evm_replay_state(
        self, token_address: str, network_id: str,
        expected_status: str | None, expected_from: int | None,
        expected_checkpoint: Any, expected_revision: int | None,
    ) -> None:
        row = self.evm_replay_state(token_address, network_id)
        current = None if row is None else (
            row.get("status"), row.get("from_block"), row.get("checkpoint_json"),
            row.get("revision"),
        )
        expected = (
            None if expected_status is None and expected_from is None
            and expected_checkpoint is None
            else (
                expected_status, expected_from, expected_checkpoint,
                expected_revision,
            )
        )
        if current != expected:
            raise StaleEVMState(
                f"تغيّرت حالة replay للعملة {token_address} أثناء الجلب"
            )

    def delete_evm_replay_rows(self, token_address: str, network_id: str) -> int:
        """يحذف الصفوف المشتقّة لهذه الإعادة فقط عند اكتشاف فساد متأخر."""
        cur = self._conn.execute(
            """DELETE FROM chain_concentration
                WHERE token_address=? AND network_id=? AND is_replay=1""",
            (token_address.lower(), str(network_id)),
        )
        self._commit()
        return cur.rowcount

    def add_block_anchor(
        self, network_id: str, block_number: int, block_ts: int, now_iso: str,
    ) -> None:
        """مرساة وقت↔كتلة. `OR IGNORE`: الكتلة لا يتغيّر طابعها أبداً."""
        self._conn.execute(
            """INSERT OR IGNORE INTO evm_block_time(
                   network_id, block_number, block_ts, fetched_at)
               VALUES(?, ?, ?, ?)""",
            (str(network_id), int(block_number), int(block_ts), now_iso),
        )
        self._commit()

    def block_anchors(
        self, network_id: str, from_block: int | None = None,
        to_block: int | None = None,
    ) -> list[tuple[int, int]]:
        """المراسي مرتّبة بالكتلة، مع مرساة واحدة **خارج** كل طرف إن وُجدت.

        الطرفان مقصودان: الاستقراء يحتاج مرساةً على كل جانب من الكتلة المطلوبة،
        وقصُّ القائمة على المدى بالضبط يترك أطرافه بلا جانب فيصير الاستقراء
        امتداداً — وهو أوسع خطأً.
        """
        net = str(network_id)
        if from_block is None or to_block is None:
            rows = self._conn.execute(
                """SELECT block_number, block_ts FROM evm_block_time
                    WHERE network_id=? ORDER BY block_number""", (net,),
            ).fetchall()
            return [(int(r["block_number"]), int(r["block_ts"])) for r in rows]
        lo, hi = int(from_block), int(to_block)
        out: set[tuple[int, int]] = set()
        for sql, params in (
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number BETWEEN ? AND ?""",
             (net, lo, hi)),
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number < ?
                 ORDER BY block_number DESC LIMIT 1""", (net, lo)),
            ("""SELECT block_number, block_ts FROM evm_block_time
                 WHERE network_id=? AND block_number > ?
                 ORDER BY block_number LIMIT 1""", (net, hi)),
        ):
            for r in self._conn.execute(sql, params).fetchall():
                out.add((int(r["block_number"]), int(r["block_ts"])))
        return sorted(out)

    def chain_first_recorded_at(
        self, token_address: str, network_id: str, live_only: bool = True,
    ) -> str | None:
        """أوّل لقطة تركّز موجودة لهذه العملة — حدُّ الإعادة الأعلى.

        الإعادة تتوقّف حيث تبدأ التغطية الحيّة: قياسان لنفس اللحظة من طريقين
        مختلفين (الحيّ بتأخير تأكيد، والمُعاد عند الكتلة بالضبط) يتفاوتان قليلاً،
        وتشابكهما في سلسلة واحدة يخلق فروق خمس‑دقائق وهميّة.
        """
        sql = """SELECT MIN(recorded_at) m FROM chain_concentration
                  WHERE token_address=? AND network_id=?"""
        if live_only:
            sql += " AND COALESCE(is_replay, 0)=0"
        row = self._conn.execute(
            sql, (token_address.lower(), str(network_id)),
        ).fetchone()
        return row["m"] if row is not None and row["m"] else None

    def chain_live_coverage(
        self, token_address: str, network_id: str,
    ) -> dict[str, str]:
        rows = self._conn.execute(
            """SELECT watch_first_seen_at, MIN(recorded_at) AS first_recorded_at
                  FROM chain_concentration
                 WHERE token_address=? AND network_id=?
                   AND COALESCE(is_replay, 0)=0
                 GROUP BY watch_first_seen_at""",
            (token_address.lower(), str(network_id)),
        ).fetchall()
        return {
            str(row["watch_first_seen_at"]): str(row["first_recorded_at"])
            for row in rows if row["first_recorded_at"]
        }

    def assert_chain_live_coverage(
        self, token_address: str, network_id: str,
        expected: Mapping[str, str],
    ) -> None:
        if self.chain_live_coverage(token_address, network_id) != dict(expected):
            raise StaleEVMState(
                f"تغيّرت التغطية الحيّة للعملة {token_address} أثناء الجلب"
            )

    # --- evm_contract (سلامة العقد — Base وحدها) ---
    def insert_evm_contract(self, row: Mapping[str, Any]) -> bool:
        cols = _EVM_CONTRACT_COLUMNS
        sql = (
            f"INSERT OR IGNORE INTO evm_contract({', '.join(cols)}) "
            f"VALUES({', '.join(f':{c}' for c in cols)})"
        )
        cur = self._conn.execute(sql, _with_compressed_raw({c: row.get(c) for c in cols}))
        self._commit()
        return cur.rowcount > 0

    def set_evm_contract_state(
        self, token_address: str, network_id: str, status: str, now_iso: str,
    ) -> None:
        self._conn.execute(
            """INSERT INTO evm_contract_state(
                   token_address, network_id, last_fetch_at, last_status, attempts)
               VALUES(?, ?, ?, ?, 1)
               ON CONFLICT(token_address, network_id) DO UPDATE SET
                   last_fetch_at = excluded.last_fetch_at,
                   last_status   = excluded.last_status,
                   attempts      = evm_contract_state.attempts + 1""",
            (token_address, network_id, now_iso, status),
        )
        self._commit()

    def evm_contract_due(
        self, limit: int, stale_before_iso: str, error_stale_before_iso: str,
        networks: Sequence[str],
    ) -> list[dict[str, Any]]:
        """نفس منطق `chain_auth_due` على جدول حالة العقود."""
        nets = [str(n) for n in networks]
        if not nets:
            return []
        marks = ", ".join("?" for _ in nets)
        rows = self._conn.execute(
            f"""SELECT w.token_address, w.network_id, w.first_seen_at,
                       w.entry_signal_id, w.is_control, s.last_fetch_at, s.last_status
                  FROM watchlist w
                  LEFT JOIN evm_contract_state s
                    ON s.token_address=w.token_address AND s.network_id=w.network_id
                 WHERE w.active=1
                   AND w.network_id IN ({marks})
                   AND (s.last_fetch_at IS NULL
                        OR (s.last_status='error' AND s.last_fetch_at < ?)
                        OR (COALESCE(s.last_status, '') <> 'error'
                            AND s.last_fetch_at < ?))
                 ORDER BY s.last_fetch_at IS NOT NULL,
                          w.is_control,
                          s.last_fetch_at,
                          w.first_seen_at DESC
                 LIMIT ?""",
            (*nets, error_stale_before_iso, stale_before_iso, limit),
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
            # إعادة التنشيط قد تأتي بعد فجوة كان دفتر EVM خلالها غير مراقَب.
            # الحالة النهائية القديمة لا تثبت تغطية تلك الفجوة، لذلك نعيد دفتر
            # العملة وreplay من genesis بدلاً من قبول رصيد ناقص بصمت.
            self._conn.execute(
                "DELETE FROM evm_balances WHERE token_address=? AND network_id=?",
                (token_address.lower(), str(network_id)),
            )
            self._conn.execute(
                "DELETE FROM evm_backfill_state WHERE token_address=? AND network_id=?",
                (token_address.lower(), str(network_id)),
            )
            self._conn.execute(
                "DELETE FROM evm_replay_state WHERE token_address=? AND network_id=?",
                (token_address.lower(), str(network_id)),
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
        cur = self._conn.execute(
            """INSERT OR IGNORE INTO watch_windows(
                   token_address, network_id, first_seen_at, source,
                   watch_until, entry_signal_id, is_control, admission_price_usd,
                   design_version, admission_source
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_address, network_id, first_seen_at, source,
             watch_until, entry_signal_id, is_control, admission_price_usd,
             design_version, admission_source),
        )
        if cur.rowcount > 0:
            # حالة replay تخص اتحاد نوافذ العملة. إضافة نافذة تجعل أي حكم نهائي
            # سابق قديماً؛ الصفوف السابقة تبقى صحيحة، والحكم وحده يُبطَل كي
            # يضيف التشغيل التالي نقاط النافذة الجديدة بلا فجوة ولا حذف تاريخ.
            self.stale_evm_replay_verdict(token_address, network_id)

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
    ("training_rows", "chain_holders_delta_1h", "chain_holders_delta_1h INTEGER"),
    ("training_rows", "chain_holders_growth_1h", "chain_holders_growth_1h REAL"),
    ("training_rows", "chain_holders_span_min", "chain_holders_span_min REAL"),
    ("training_rows", "holders_age_min", "holders_age_min REAL"),
    ("training_rows", "platform_holders", "platform_holders INTEGER"),
    ("training_rows", "platform_penetration", "platform_penetration REAL"),
    ("training_rows", "platform_underwater_ratio", "platform_underwater_ratio REAL"),
    ("training_rows", "platform_value_usd", "platform_value_usd REAL"),
    ("training_rows", "platform_median_hold_h", "platform_median_hold_h REAL"),
    ("training_rows", "platform_dev_holding", "platform_dev_holding INTEGER"),
    # الملكية من السلسلة (v10): كانت سولانا وحدها، وصارت الشبكتين معاً في v12 —
    # دفتر أرصدة من سجلّات Transfer (evm_layer) يكتب في نفس chain_concentration.
    ("training_rows", "onchain_top1_pct", "onchain_top1_pct REAL"),
    ("training_rows", "onchain_top5_pct", "onchain_top5_pct REAL"),
    ("training_rows", "onchain_top10_pct", "onchain_top10_pct REAL"),
    ("training_rows", "onchain_top20_pct", "onchain_top20_pct REAL"),
    ("training_rows", "onchain_top_accounts", "onchain_top_accounts INTEGER"),
    ("training_rows", "onchain_age_min", "onchain_age_min REAL"),
    ("training_rows", "onchain_top1_delta_5m", "onchain_top1_delta_5m REAL"),
    ("training_rows", "onchain_top10_delta_5m", "onchain_top10_delta_5m REAL"),
    ("training_rows", "onchain_delta_span_min", "onchain_delta_span_min REAL"),
    # (v12) عدد الحائزين مضبوطاً من الدفتر — EVM وحدها، وسولانا تبقى NULL إذ
    # getTokenLargestAccounts يعيد 20 حساباً بحدّ أقصى ولا يعرف الإجمال.
    ("training_rows", "onchain_holder_count", "onchain_holder_count INTEGER"),
    ("training_rows", "onchain_holders_delta_5m", "onchain_holders_delta_5m INTEGER"),
    # هـ٢-ج) الخطر البنيويّ من السلسلة (chain_authority، ساعيّ).
    ("training_rows", "onchain_has_mint_authority", "onchain_has_mint_authority INTEGER"),
    ("training_rows", "onchain_has_freeze_authority", "onchain_has_freeze_authority INTEGER"),
    ("training_rows", "onchain_is_mutable", "onchain_is_mutable INTEGER"),
    ("training_rows", "onchain_is_token2022", "onchain_is_token2022 INTEGER"),
    ("training_rows", "onchain_dev_holding_pct", "onchain_dev_holding_pct REAL"),
    ("training_rows", "onchain_auth_age_min", "onchain_auth_age_min REAL"),
    # هـ٢-د) نظيرها على EVM (evm_contract، ساعيّ، Base وحدها): من البايت‑كود
    # مباشرة — ERC-20 لا يحمل صلاحيات معلنة، ووجود المُعرّف هو الدليل.
    ("training_rows", "onchain_code_size", "onchain_code_size INTEGER"),
    ("training_rows", "onchain_function_count", "onchain_function_count INTEGER"),
    ("training_rows", "onchain_is_proxy", "onchain_is_proxy INTEGER"),
    ("training_rows", "onchain_owner_renounced", "onchain_owner_renounced INTEGER"),
    ("training_rows", "onchain_has_mint_fn", "onchain_has_mint_fn INTEGER"),
    ("training_rows", "onchain_has_pause_fn", "onchain_has_pause_fn INTEGER"),
    ("training_rows", "onchain_has_blacklist_fn", "onchain_has_blacklist_fn INTEGER"),
    ("training_rows", "onchain_has_fee_setter", "onchain_has_fee_setter INTEGER"),
    ("training_rows", "onchain_has_limit_setter", "onchain_has_limit_setter INTEGER"),
    ("training_rows", "onchain_has_trading_switch", "onchain_has_trading_switch INTEGER"),
    ("training_rows", "onchain_contract_age_min", "onchain_contract_age_min REAL"),
    # صدارات المدد (v7): سقف المصدر 50 في الصدارة الأساسيّة وكل صيغ الترقيم
    # مُهمَلة بصمت، لكنّ /24h و/7d و/30d تعيد كلٌّ 100 فاتّحاد الأربع 214 متداولاً
    # (المطابقة 3.68% ← 15.26% على 7,200 حدثاً). لا سبيل لتعبئة الماضي: الأرشيف
    # حفظ totalPnL وحدها فلا تاريخ لرتب المدد — الصفوف القديمة تبقى NULL بحقّ.
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
    #    كلّها NULL قبل بدء الجمع (FR-007: «لم نقس» لا «صفر»).
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
    # عدد الحائزين **المضبوط** من طبقة EVM. القاعدة الحيّة أنشأت
    # chain_concentration قبل وجود هذه الطبقة، فبلا هذا السطر يبقى العمود
    # مفقوداً هناك و`insert_chain_concentration` يرفع «no such column».
    # يبقى NULL على سولانا: `getTokenLargestAccounts` يعيد 20 حساباً بحدّ أقصى
    # ولا يعرف الإجمال (غياب مقيس لا صفر، FR-007).
    ("chain_concentration", "holder_count", "holder_count INTEGER"),
    # علامة الصفّ المُعاد (`evm_replay.py`). القاعدة الحيّة أنشأت الجدول قبل
    # وجود الإعادة، و`NOT NULL DEFAULT 0` يجعل كل صفوفها القديمة «حيّة» — وهي
    # كذلك فعلاً. بلا العمود لا يمكن تدريب النموذج على المقيس حيّاً وحده.
    ("chain_concentration", "is_replay",
     "is_replay INTEGER NOT NULL DEFAULT 0"),
    ("evm_replay_state", "checkpoint_json", "checkpoint_json BLOB"),
    ("evm_replay_state", "revision", "revision INTEGER NOT NULL DEFAULT 0"),
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

# ترتيب أعمدة chain_concentration — يطابق schema.sql. جدول منفصل عن
# token_holders لأنّ ذاك مصدره FOMO بإيقاع 25 دقيقة ويخلط سكانَين (السلسلة
# كاملةً ومستخدمي المنصّة)، وهذا قياس سلسلة مباشر بإيقاع 5 دقائق.
_CHAIN_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "supply", "decimals",
    "top1_pct", "top5_pct", "top10_pct", "top20_pct", "holder_count",
    "top_accounts", "is_replay", "raw_json",
)

# ترتيب أعمدة evm_contract — يطابق schema.sql. Base وحدها بقياس: هي الشبكة
# الوحيدة التي تباينت فيها العقود فعلاً (19 عقداً كاملاً بأحجام 135B–14.8KB)،
# أمّا BSC فوكلاء متطابقون وروبن‑هود ستّة قوالب مكرّرة ⇒ عمود ثابت لا معلومة.
_EVM_CONTRACT_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "code_size", "function_count",
    "is_proxy", "impl_address", "code_hash", "owner_address",
    "is_ownership_renounced", "has_mint", "has_pause", "has_blacklist",
    "has_fee_setter", "has_limit_setter", "has_trading_switch", "raw_json",
)

# ترتيب أعمدة chain_authority — يطابق schema.sql. الطبقة البطيئة (ساعيّة):
# صلاحية السكّ/التجميد و`mutable` تتغيّر مرّة في العمر، فلا معنى لسؤالها بإيقاع
# التركّز.
_CHAIN_AUTH_COLUMNS = (
    "token_address", "network_id", "recorded_at", "watch_first_seen_at",
    "entry_signal_id", "is_control", "token_program", "mint_authority",
    "freeze_authority", "update_authority", "is_mutable", "creator_address",
    "creator_count", "supply", "decimals", "dev_owner", "dev_holding_pct",
    "raw_json",
)
