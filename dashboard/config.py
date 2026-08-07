"""إعدادات لوحة معلومات المسجّل.

مشروع منفصل بجانب recorder/ و api/. يقرأ recorder.db للقراءة فقط ويفحص صحّة
الـ API المحلّي. كل المسارات مطلقة ومشتقّة من موقع هذا الملف حتى تعمل اللوحة
بصرف النظر عن مجلّد العمل (مهمّ للمهمّة المجدولة).
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))          # .../aoi/dashboard
ROOT = os.path.dirname(HERE)                               # .../aoi
RECORDER_DIR = os.path.join(ROOT, "recorder")              # .../aoi/recorder

# قاعدة بيانات المسجّل — تُفتح للقراءة فقط (mode=ro) حتى لا تعطّل كتابات المسجّل.
DB_PATH = os.path.join(RECORDER_DIR, "recorder.db")

# صحّة الـ API المحلّي (خادم fomo_api على 8080).
API_HEALTH_URL = "http://127.0.0.1:8080/health"
API_HEALTH_TIMEOUT = 3.0  # ثوانٍ

# منفذ اللوحة نفسها (محلّي فقط).
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8090

# ملفات السجلّ (المهمّة المجدولة تُخفي stderr عبر pythonw).
LOG_PATH = os.path.join(HERE, "dashboard.log")
BOOT_LOG_PATH = os.path.join(HERE, "dashboard_boot.log")

STATIC_DIR = os.path.join(HERE, "static")

# مخزن مفاتيح Helius المشترك مع crib. المسار والإعداد الأساسي محليان في بيئة
# المستخدم ولا يُحفظان في Git. إن غاب المسار تبقى بطاقة الإدارة معطّلة بوضوح.
HELIUS_KEYS_PATH = os.environ.get("AOI_HELIUS_KEYS_PATH")
SOLANA_RPC_URL = os.environ.get("AOI_SOLANA_RPC_URL")

# حدّ الحِقبة الحيّة: أوّل إشارة جمعها البوت لحظياً (2026-07-25T22:35:27Z).
# اللوحة تعرض **بيانات البوت فقط**؛ ما قبل هذا الختم بيانات رجعيّة (backfill)
# تفتقر للعائلات اللحظية، وقد أُقصيت من التدريب وحُذفت صفوفُها من القاعدة. يبقى
# جدول token_bars وحده يحمل شموعاً رجعيّة (تاريخ سعر سابق للإشارة)، فنقصر عدّ
# الشموع المعروض على هذا الحدّ حتى لا تختلط الرجعيّة ببيانات البوت.
# القيمة تطابق recorder/config.py:LIVE_START_TS (حدّ حِقبة واحد للمشروع كلّه).
LIVE_START_TS = 1785018927

# "حيّ" = آخر دورة مسجّل خلال هذه المهلة (الدورة كل 60ث، فنسمح بضعف + هامش).
RECORDER_ALIVE_WINDOW_SECONDS = 150

# "حيّ" للموسِّم = آخر دورة خلال هذه المهلة (دورته كل 900ث، فنسمح بضعف + هامش).
# موته صامت تماماً — لا أخطاء ولا انهيار — بينما تتوقّف النتائج عن التراكم.
LABELER_ALIVE_WINDOW_SECONDS = 2000

# بوابات الضابطة v3 المثبتة في docs/PLAN.md: 100 فحص أولي، 500 قرار أساسي.
CONTROL_PRELIMINARY_TARGET = 100
CONTROL_DECISION_TARGET = 500
CONTROL_DESIGN_VERSION = 3

# التخزين والنسخ الاحتياطي. يطابق الافتراضي recorder/backup_db.py: وجهة
# متزامنة خارج المستودع، مع إنذار إن مرّ أكثر من يوم ونصف بلا نسخة سليمة.
BACKUP_DIR = os.environ.get("AOI_BACKUP_DIR") or (
    os.path.join(os.environ["OneDrive"], "aoi-backups")
    if os.environ.get("OneDrive")
    else None
)
BACKUP_MAX_AGE_HOURS = 36.0
DISK_FREE_WARN_GB = 25.0

# مصادر المسجّل التي نعرض آخر خطأ لكلٍّ منها (تطابق مفاتيح meta: last_error_<src>).
# القائمة تغطّي كل ما يكتبه المسجّل فعلاً؛ bars/social كانا يُسجَّلان بلا عرض.
RECORDER_SOURCES = (
    "feed", "trending", "verified", "leaderboard", "control",
    "bars", "social", "chain_security", "macro", "cleanup",
)
