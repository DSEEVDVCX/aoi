"""يجعل حزمة dashboard قابلة للاستيراد في الاختبارات دون تثبيت، ويعزلها عن داتا التشغيل.

يضيف مجلّد dashboard/ (الأب) إلى sys.path حتى تعمل `import dao` و`import config`
كما يستوردها كود اللوحة نفسه (استيراد مسطّح، لا حزمة) — نفس نمط recorder/tests.
بدونه كانت اختبارات اللوحة تفشل في التجميع أصلاً عند تشغيل pytest من جذر المشروع.
"""
import os
import sqlite3
import sys

import pytest

DASHBOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)

import config  # noqa: E402 — بعد إضافة المسار، وإلّا لا يُوجَد


@pytest.fixture(autouse=True)
def isolate_live_state(tmp_path, monkeypatch):
    """يحوّل مسارَي القاعدة وملفِّ المفاتيح إلى المؤقّت في **كلّ** اختبار.

    `_with_conn` يفتح `config.DB_PATH` عند كلّ طلب، فاختبارٌ ينادي مساراً يقرأ
    القاعدة كان يقرأ `recorder.db` الحقيقيّة. محلياً هي موجودةٌ فيمرّ الاختبار
    وهو معلَّقٌ بداتا حيّة بلا أن يقول، وعلى الخادم غائبةٌ (مُستثناة من git)
    فسقطت تسعةُ اختباراتٍ للوحة المفاتيح بـ
    `sqlite3.OperationalError: unable to open database file` في أوّل مرّةٍ
    شغّل فيها الخادمُ اختباراتِ اللوحة فعلاً.

    والحرسُ تلقائيٌّ لا اختياريّ عن قصد: النسيانُ هنا لا يُرى — الاختبارُ يمرّ.
    وهو في ملفّ المفاتيح أخطرُ منه في القاعدة: القاعدةُ تُفتح للقراءة فقط، أمّا
    `/api/provider-keys/{add,delete}` فيكتب، واختبارٌ نسي العزل يحذف مفتاحاً
    عاملاً من ملفّ التشغيل. (لهذا تبقى `keys_file`: هذه تضمن العزل، وتلك تزرع
    محتوىً وتصفّي الفحوص والبيئة.)

    وفيه جدولُ `meta` وحده لأنّه كلُّ ما تقرأه `dao.provider_keys`؛ ومن احتاج
    جدولاً آخر يبني قاعدتَه كـ`db` في `test_app_cache.py` — يكفي أن يضبط
    `config.DB_PATH` بعد هذا فيغلبه.
    """
    path = tmp_path / "isolated.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    # غيرُ موجودٍ عن قصد: الغيابُ حالةٌ صالحةٌ يجب أن تُحتمَل، ولا يصحّ أن يكون
    # البديلُ الصامتُ هو ملفَّ الأسرار الحقيقيّ.
    monkeypatch.setattr(config, "CHAIN_KEYS_PATH", str(tmp_path / "isolated_keys.json"))
    return path
