"""صيغةُ ملفّ مفاتيح المزوّدين على القرص — قراءةً وكتابةً — **بلا أيّ اعتمادٍ محلّيّ**.

هذا الملف لا يستورد `config` ولا أيَّ وحدةٍ من المشروع، وذلك عن قصد: اللوحة
مشروعٌ منفصل له `config.py` خاصٌّ به، فإضافةُ `recorder/` إلى `sys.path` كانت
ستجعل `import config` داخل وحدةٍ مشتركة تُصيب أحدَ الملفّين بحسب الترتيب — عطبٌ
يظهر متأخّراً وفي عمليّةٍ واحدةٍ فقط. ولأنّه بلا اعتمادات، تحمّله اللوحة بمسارٍ
صريح (`importlib`) بلا لمس `sys.path` أصلاً، فتبقى صيغةُ الملفّ **معرّفةً في
مكانٍ واحد** يقرأه المسجّل ويكتبه اللوحة، لا في مكانين يتباعدان بهدوء.

الصيغة تقبل ثلاثة أشكالٍ لكلّ مزوّد، لأنّ الملفّ الموجود كُتب بيدٍ قبل التعدّد:

    {"helius_api_key": "..."}                        المفرد القديم
    {"helius_api_keys": ["...", "..."]}              الجمع
    {"helius_api_keys": [{"key": "...", "label": "حساب رئيسي", "enabled": true}]}

الشكل الثالث هو ما تكتبه اللوحة: `label` اسمُ الحساب الذي جُلب منه المفتاح —
وهو المعرّف البشريّ الوحيد المسموح، إذ لا تُعرض القيمة نفسها (FR-013). و`enabled`
إيقافٌ مؤقّت: المفتاح يبقى في الملفّ ولا يدخل الحوض، فيُوقف مفتاحٌ مشتبَهٌ به بلا
فقدان قيمته.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

# المزوّدون الثلاثة الذين لهم مفاتيح على هذا الجهاز. `plural`/`singular` أسماءُ
# الحقول في الملفّ (يجب أن تطابق نداءات `provider_keys.read_keys`)، و`env` متغيّرُ
# البيئة الذي **يتقدّم على الملفّ** — فإن كان مضبوطاً فلا معنى لتحرير الملفّ من
# اللوحة، ويجب أن تقول اللوحة ذلك صراحةً بدل أن تكتب في فراغ.
#
# `probe` نداءُ التحقّق الأرخص لكلّ مزوّد، وعنوانُه يطابق ما يستعمله العميل فعلاً
# (`config.SOLANA_RPC_URL`, `nodereal_rpc._call`) — فحصٌ
# لعنوانٍ آخر كان سيقول «سليم» عن مفتاحٍ لا يعمل حيث يُستعمل. يحرسه
# `tests/test_key_file.py::test_probe_endpoints_match_the_clients`.
PROVIDERS: dict[str, dict[str, Any]] = {
    "helius": {
        "plural": "helius_api_keys",
        "singular": "helius_api_key",
        "env": "HELIUS_API_KEY",
        "title": "Helius · سولانا",
        "probe": {
            "method": "POST",
            "url": "https://mainnet.helius-rpc.com/?api-key={key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        },
    },
    "nodereal": {
        "plural": "nodereal_api_keys",
        "singular": "nodereal_api_key",
        "env": "NODEREAL_API_KEY",
        "title": "NodeReal · BSC",
        "probe": {
            "method": "POST",
            "url": "https://bsc-mainnet.nodereal.io/v1/{key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
    # المزوّدان التاليان لا يبنيان بياناً: يدقّقان دفترَ EVM بحالةٍ أرشيفيّة
    # (`audit_evm_ledger.py`) لأنّ عقدَنا العامّة بلا أرشيف — عقدةُ روبن‑هود
    # عمقُها ~128 كتلة. والطبقةُ الحيّة تبقى بلا مفتاح (FR-012).
    "alchemy": {
        "plural": "alchemy_api_keys",
        "singular": "alchemy_api_key",
        "env": "ALCHEMY_API_KEY",
        "title": "Alchemy · أرشيف EVM",
        "probe": {
            "method": "POST",
            # الفحصُ على Base لا على روبن‑هود: كلاهما يخدمه هذا المفتاح، وBase
            # شريحةٌ عامّةٌ مستقرّة — فسقوطُ الفحص يعني المفتاحَ لا الشبكة.
            "url": "https://base-mainnet.g.alchemy.com/v2/{key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
    "drpc": {
        "plural": "drpc_api_keys",
        "singular": "drpc_api_key",
        "env": "DRPC_API_KEY",
        "title": "dRPC · أرشيف Base",
        "probe": {
            "method": "POST",
            "url": "https://lb.drpc.org/ogrpc?network=base&dkey={key}",
            "json": {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        },
    },
}

# أقصرُ مفتاحٍ يجوز إظهارُ ذيله. مفتاحٌ قصير (≤11) ذيلُه الرباعيّ جزءٌ معتبَرٌ من
# مادّته، فلا يُظهر شيءٌ منه أصلاً: التمييز يسقط، والسرّ يبقى.
_MIN_LENGTH_FOR_TAIL = 12
TAIL_LENGTH = 4


def tail(key: str) -> str:
    """آخرُ أربعة أحرف للتمييز البشريّ بين مفتاحين — لا أكثر، وليس بصمة.

    عرضُ كسرٍ من السرّ خفضٌ مقصودٌ لِـ FR-013 طلبَه المستخدم للتمييز، ومحصورٌ
    هنا في دالّةٍ واحدة: لا يُكتب هذا الذيل في سجلٍّ ولا في `meta` ولا في تقرير
    الأحواض، بل يُحسب لحظةَ طلبِ اللوحة ويعيش في استجابةٍ واحدة.
    """
    text = key.strip()
    if len(text) < _MIN_LENGTH_FOR_TAIL:
        return ""
    return text[-TAIL_LENGTH:]


def load(path: str) -> dict[str, Any]:
    """محتوى الملفّ كما هو. الغياب والعطب سواءٌ: قاموسٌ فارغ لا استثناء.

    الغيابُ حالةٌ عاديّة (جهازٌ بلا مفاتيح بعد)، والعطبُ لا يجوز أن يُسقط
    اللوحةَ ولا المسجّل — من يكتب هو من يجب أن يتعثّر، لا من يقرأ.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def entries(data: dict[str, Any], plural: str, singular: str) -> list[dict[str, Any]]:
    """أسطرُ مزوّدٍ واحد موحَّدةً: `{key, label, enabled}` — **بما فيها المعطّلة**.

    الجمعُ يحجب المفردَ حتى لو كان فارغاً: قائمةٌ فارغة تعني «حُذف آخرُ مفتاح»
    وليست «ارجع إلى القيمة القديمة»، وإلّا عاد المحذوفُ من قبره عند أوّل قراءة.

    والمعطّلةُ مُدرَجةٌ هنا لأنّ اللوحة تحتاج أن تعرضها لتُعيد تشغيلها؛ ومَن
    يبني الحوض (`provider_keys.read_keys`) هو مَن يُصفّيها.
    """
    raw = data.get(plural)
    candidates = raw if isinstance(raw, list) else [data.get(singular)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            key, label, enabled = item.strip(), "", True
        elif isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            label = str(item.get("label") or "").strip()
            # الغيابُ تشغيلٌ: سطرٌ كتبه إنسانٌ بيده بلا `enabled` مفتاحٌ عامل.
            enabled = item.get("enabled", True) is not False
        else:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "label": label, "enabled": enabled})
    return out


def load_entries(path: str, plural: str, singular: str) -> list[dict[str, Any]]:
    return entries(load(path), plural, singular)


def save_entries(
    path: str, plural: str, singular: str, rows: list[dict[str, Any]],
) -> None:
    """يكتب أسطرَ مزوّدٍ واحد ويُبقي كلَّ ما عداه في الملفّ كما هو.

    ثلاثةُ احتياطاتٍ لأنّ هذا الملفّ يقرأه المسجّل **عند كلّ نداء**:

    1. `os.replace` على ملفٍّ مؤقّتٍ في نفس المجلّد — استبدالٌ ذرّيّ. الكتابةُ
       فوق الأصل مباشرةً تعني نافذةً يرى فيها القارئُ ملفّاً نصفَ مكتوب فتختفي
       كلُّ المفاتيح، أي عطلٌ كاملٌ في الجمع لأجل تعديلٍ في مفتاحٍ واحد.
    2. تحقّقٌ قبل الاستبدال: نُعيد تحليل ما كتبناه ونطابق المفاتيح المفعّلة على
       المقصود. تسلسلٌ ناقص يُكتشف قبل أن يصير هو الملفَّ.
    3. المفردُ القديم يُحذف عند أوّل كتابة: بقاؤه بجانب الجمع يعني مصدرين
       للحقيقة، والجمعُ يحجبه، فيُحرَّر أحدُهما ولا يتغيّر شيء.
    """
    data = load(path)
    normalized = [
        {
            "key": str(row["key"]).strip(),
            "label": str(row.get("label") or "").strip(),
            "enabled": bool(row.get("enabled", True)),
        }
        for row in rows
        if str(row.get("key") or "").strip()
    ]
    data[plural] = normalized
    data.pop(singular, None)

    blob = json.dumps(data, ensure_ascii=False, indent=1)
    check = entries(json.loads(blob), plural, singular)
    if [row["key"] for row in check] != [row["key"] for row in normalized]:
        raise ValueError("تحقّق الكتابة فشل: الملفّ المسلسل لا يعيد نفس المفاتيح")

    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".keys-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(blob + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
