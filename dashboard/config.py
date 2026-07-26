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

# "حيّ" = آخر دورة مسجّل خلال هذه المهلة (الدورة كل 60ث، فنسمح بضعف + هامش).
RECORDER_ALIVE_WINDOW_SECONDS = 150

# مصادر المسجّل التي نعرض آخر خطأ لكلٍّ منها (تطابق مفاتيح meta: last_error_<src>).
RECORDER_SOURCES = ("feed", "trending", "verified", "leaderboard", "cleanup")
