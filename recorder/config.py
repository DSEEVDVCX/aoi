"""إعدادات مسجّل البيانات التاريخي.

المسجّل مشروع منفصل بجانب `api/` لكنه يعيد استخدام كود `api` (FomoClient،
CredentialStore، config) عبر إضافة `api/src` إلى sys.path. لا يكرّر أي منطق
وصول موجود بالفعل.

كل المسارات مطلقة ومشتقّة من موقع هذا الملف، حتى يعمل المسجّل بصرف النظر عن
مجلّد العمل الحالي (مهمّ للمهمّة المجدولة).
"""
from __future__ import annotations

import os
import sys

# --- المسارات ---
HERE = os.path.dirname(os.path.abspath(__file__))          # .../aoi/recorder
ROOT = os.path.dirname(HERE)                               # .../aoi
API_DIR = os.path.join(ROOT, "api")                        # .../aoi/api
API_SRC = os.path.join(API_DIR, "src")                     # .../aoi/api/src

# نضيف api/src حتى نستورد fomo_api. يُنفّذ عند الاستيراد لأن config/db يحتاجانه.
if API_SRC not in sys.path:
    sys.path.insert(0, API_SRC)

# قاعدة البيانات بجانب هذا الملف.
DB_PATH = os.path.join(HERE, "recorder.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

# ملف السجلّ (المهمّة المجدولة تُخفي stderr).
LOG_PATH = os.path.join(HERE, "recorder.log")

# ملف حالة الاعتماد (session_token). افتراضي fomo_api نسبيّ (.privy_state.json)،
# وهو يقع داخل api/، فنحلّه بالنسبة إلى api/ ما لم يكن مطلقاً بالفعل.
def credential_state_path() -> str:
    from fomo_api.config import settings  # noqa: E402  (بعد إضافة sys.path)

    raw = settings.credential_state_file
    if os.path.isabs(raw):
        return raw
    return os.path.join(API_DIR, raw)


# --- تواترات ومحدّدات (بالثواني/الساعات) ---
CYCLE_SECONDS = 60                 # دورة كل دقيقة
WATCH_HOURS = 48                   # مدّة مراقبة كل عملة
WATCHLIST_CAP = 150                # حدّ العملات النشطة المتزامنة
LEADERBOARD_REFRESH_SECONDS = 3600 # تحديث صدارة المتصدّرين كل ساعة
LEADERBOARD_SIZE = 200             # عدد المتصدّرين المحمّلين للمطابقة

# --- شموع OHLCV (getBarsNew) ---
# مصدر الحقيقة السعرية للتوسيم. قوائم trending/verified لا تكفي: ربع العملات
# المراقَبة لم تظهر فيها قطّ فبقيت بلا أي سعر.
BARS_RESOLUTION = "5"              # 5 دقائق: 48 ساعة = 576 شمعة، تحت سقف 900
BARS_PER_CYCLE = 9                 # عملات لكل دورة — رُفعت من 6 لاستيعاب الضابطة
BARS_REFRESH_SECONDS = 900         # نُحدّث شموع كل عملة كل ~15 دقيقة
BARS_PACING_SECONDS = 1.5          # فاصل بين نداءات getBarsNew (لُطف مع fomo)
BARS_PRE_SIGNAL_HOURS = 2          # نسحب سياقاً قبل الإشارة أيضاً
BARS_MAX_SPAN_HOURS = 72           # سقف النافذة المطلوبة (دون سقف 900 شمعة)
BARS_COUNT_BACK = 900              # أقصى ما يعيده fomo في نداء واحد
# عملة يردّ عليها fomo `no_data` هذا العدد من المرّات تُستبعد: لا سلسلة لها.
BARS_MAX_NO_DATA_ATTEMPTS = 3

# --- الموسِّم (labeler — عملية FomoLabeler المنفصلة) ---
# منفصل عن المسجّل عمداً: المسجّل يسجّل خاماً فقط (منع تسرّب المستقبل)،
# والتوسيم لا يجري إلّا بعد اكتمال النافذة.
LABEL_WINDOW_HOURS = 48            # نافذة النتيجة — تطابق نافذة المراقبة
LABEL_MARGIN_SECONDS = 900         # هامش بعد النافذة: تُغلق آخر شمعة ويلتقطها المسح
LABEL_INTERVAL_SECONDS = 900       # دورة الموسِّم (كل 15 دقيقة)
LABEL_BATCH = 500                  # حدّ التوسيم في الدورة الواحدة
LABEL_ENTRY_MAX_LAG_SECONDS = 1800 # أقصى تأخّر لشمعة الدخول وإلّا status=no_entry
LABEL_INDEPENDENCE_GAP_SECONDS = 1800  # فجوة اعتبار الإشارة مستقلّة (69% < 5 دقائق!)
LABEL_RUG_THRESHOLD = -0.90        # عائد نهائي ≤ -90% ⇒ is_rug=1
LABEL_LOG_PATH = os.path.join(HERE, "labeler.log")

# --- الاحتفاظ بالأرشيف ---
# حذف اللقطات الخام (snapshots) الأقدم من هذا العدد من الأيام. 0 = بلا حذف
# (الافتراضي): اللقطات هي أرشيف إعادة الاشتقاق، فحذفها قرار صريح للمالك.
# مع ضغط zlib تكبر القاعدة ~280 MB يومياً بدل ~1.3 GB، فالاحتفاظ الأبديّ معقول.
SNAPSHOT_RETENTION_DAYS = 0
# تدوير recorder.log عند تجاوزه هذا الحجم (بايت). 0 = بلا تدوير.
LOG_MAX_BYTES = 5 * 1024 * 1024

# --- الطبقة الاجتماعية (/feed/token/thesis) ---
# عملات الميم تحرّكها الحشود؛ كنّا نسجّل السعر والحجم ولا نسجّل النقاش حولها.
# دورة أبطأ من الشموع: الزخم الاجتماعي يتغيّر بالساعات لا بالدقائق.
SOCIAL_PER_CYCLE = 4               # عملات لكل دورة
SOCIAL_REFRESH_SECONDS = 1800      # لقطة اجتماعية كل ~30 دقيقة لكل عملة
SOCIAL_PACING_SECONDS = 1.0
SOCIAL_THRESHOLD = 0               # 0 = بلا حدّ أدنى لحصّة الكاتب (نريد الكلّ)

# --- المجموعة الضابطة (الصنف السالب) ---
# عملات تدخل المراقبة **بالاختيار العشوائي لا بإشارة**، وتُسجَّل بنفس الطريقة.
# بلا هذا الصنف يستطيع النموذج تعلّم "أي عملة مُشار إليها ترتفع أكثر"، لكنّه لا
# يستطيع أبداً الإجابة عن "هل الإشارة تعني شيئاً أصلاً" — إذ كانت كل عملة لها
# سلسلة سعرية قد دخلت بإشارة، فلا مرجع للمقارنة.
CONTROL_GROUP_SIZE = 40            # عدد الضابطة النشطة المستهدَف
CONTROL_PER_CYCLE = 2              # حدّ الإدخال في الدورة — يوزّع العيّنة على الزمن
                                   # بدل التقاط 40 عملة من لحظة سوقية واحدة
CONTROL_WATCH_HOURS = 48           # نفس نافذة المُشار إليها (مقارنة عادلة)

# الإشارات التي تُدخل عملة إلى المراقبة (المُشغّلات). الرواج/التحذيرات سياق لا مُشغّل.
TRIGGER_SIGNAL_TYPES = ("multi_user_buy", "large_buy")
# كل الأنواع التي نطلبها من الـ feed (نسجّل sell أيضاً كسياق، لا يُشغّل مراقبة).
FEED_TYPES = ("multi_user_buy", "large_buy", "multi_user_sell")
FEED_LIMIT = 50
