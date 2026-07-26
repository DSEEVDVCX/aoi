"""يجعل حزمة recorder قابلة للاستيراد في الاختبارات دون تثبيت.

يضيف مجلّد recorder/ (الأب) إلى sys.path حتى تعمل `import extract` و`import db`
كما يستوردها كود المسجّل نفسه (استيراد مسطّح، لا حزمة).
"""
import os
import sys

RECORDER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RECORDER_DIR not in sys.path:
    sys.path.insert(0, RECORDER_DIR)
