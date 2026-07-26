"""يجعل حزمة dashboard قابلة للاستيراد في الاختبارات دون تثبيت.

يضيف مجلّد dashboard/ (الأب) إلى sys.path حتى تعمل `import dao` و`import config`
كما يستوردها كود اللوحة نفسه (استيراد مسطّح، لا حزمة) — نفس نمط recorder/tests.
بدونه كانت اختبارات اللوحة تفشل في التجميع أصلاً عند تشغيل pytest من جذر المشروع.
"""
import os
import sys

DASHBOARD_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, DASHBOARD_DIR)
