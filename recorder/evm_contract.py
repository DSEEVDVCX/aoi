"""سلامة عقد EVM — من البايت‑كود مباشرة، بلا مزوّد تحقّق.

**لماذا من البايت‑كود لا من كود المصدر؟** لأنّ كل مزوّدي التحقّق قياساً دونه:
Etherscan V2 يرفض بلا مفتاح، وV1 مُلغى، وSourcify يعرف 1 من 10 عملات. أمّا
`eth_getCode` فحرّ ومضمون ⇒ التغطية 100% لا 10%.

**والفحص على Base وحدها**، وهذا نتيجة قياس لا تقصير (2026-08-13، المراقَبة
الحيّة): على BSC 21 من 27 وكيلاً صغيراً مطابقاً (EIP-1167) يشير إلى **عقدَي
تنفيذ** فقط، 20 منها إلى واحد، والملكيّة متروكة في كليهما ولا مُعرّف إيقاف أو
عمولة أو حدّ في أيّهما — فالعمود ثابت لا معلومة فيه. وعلى روبن‑هود 51 من 57
عقداً كاملاً لكنّ أحجامها تتكرّر في ستّة قوالب متطابقة و`owner` في 5 من 51.
أمّا Base: 19 من 22 عقداً كاملاً بأحجام 135B–14.8KB و`owner` في 7 من 19
و`mint` في 2 ⇒ التباين حقيقيّ فالعمود يفرّق.

المُعرّفات تُحسب هنا بـkeccak-256 **بتنفيذ داخليّ**: لا `pycryptodome` ولا
`eth-hash` في البيئة، و`hashlib.sha3_256` هو SHA3 المعياريّ لا Keccak الأصليّ
(الحشو يختلف) فلا يصلح بديلاً.

قراءة فقط (FR-012).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import config
from db import RecorderDB
from evm_rpc import EVMRateLimit

# ---------------------------------------------------------------------------
# keccak-256 (Keccak-f[1600], rate 136) — التنفيذ الأصليّ لا SHA3 المعياريّ.
# ---------------------------------------------------------------------------
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROT = (
    (0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56), (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK if n else x


def _keccak_f(a: list[list[int]]) -> None:
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y] & _MASK)
        a[0][0] ^= _RC[rnd]


def keccak256(data: bytes) -> bytes:
    """keccak-256 لبايتات. الحشو `0x01` (لا `0x06` كما في SHA3 المعياريّ)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80
    state = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)
    out = bytearray()
    for i in range(4):
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


def selector(signature: str) -> str:
    """`owner()` → `0x8da5cb5b` — أوّل أربع بايتات من keccak التوقيع."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


# دوالّ الملكيّة التي نناديها فعلاً (نداء لكل صيغة، والارتداد جوابٌ لا خطأ).
_OWNER_CALLS = ("owner()", "getOwner()", "_owner()")

# المُعرّفات التي نبحث عنها في جدول توزيع البايت‑كود. المجموعة مقيسة: هذه
# بالضبط ما تباين حضورها على Base، والبقيّة حضرت في كل عقد أو غابت عن كلّها.
_RISK_SELECTORS: dict[str, tuple[str, ...]] = {
    "has_mint": ("mint(address,uint256)", "mint(uint256)"),
    "has_pause": ("pause()", "unpause()", "setPaused(bool)"),
    "has_blacklist": (
        "blacklist(address)", "setBlacklist(address,bool)", "addBlackList(address)",
        "isBlacklisted(address)",
    ),
    "has_fee_setter": (
        "setFees(uint256,uint256)", "setFee(uint256)", "setTaxes(uint256,uint256)",
        "setBuyTax(uint256)", "setSellTax(uint256)",
    ),
    "has_limit_setter": (
        "setMaxTxAmount(uint256)", "setMaxWallet(uint256)",
        "setMaxWalletAmount(uint256)", "removeLimits()",
    ),
    "has_trading_switch": (
        "enableTrading()", "openTrading()", "setTradingEnabled(bool)",
        "startTrading()",
    ),
}

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# EIP-1167: وكيل صغير طوله 45 بايتاً بالضبط — بادئة، ثمّ 20 بايت عنوان، ثمّ لاحقة.
# مطابقة الطرفين لا الطول وحده: عقد بطول 45 وبايتات أخرى ليس وكيلاً.
_PROXY_PREFIX = "363d3d373d3d3d363d73"
_PROXY_SUFFIX = "5af43d82803e903d91602b57fd5bf3"


def _code_selectors(code_hex: str) -> set[str]:
    """مُعرّفات الدوالّ الظاهرة في البايت‑كود.

    جدول التوزيع يقارن أربع بايتات بـ`PUSH4` قبل كل فرع، فمُعرّفات العقد هي
    عمليّاً كل immediate من `PUSH4`. وتخطّي immediates إلزاميّ: بايتة بقيمة
    `0x63` **داخل** بيانات دفعٍ أخرى ليست تعليمة، وقراءتها كتعليمة تُنتج مُعرّفات
    وهميّة تجعل كل عقد يبدو حاملاً كل شيء.
    """
    body = code_hex[2:] if code_hex.startswith("0x") else code_hex
    try:
        code = bytes.fromhex(body)
    except ValueError:
        return set()
    out: set[str] = set()
    i = 0
    n = len(code)
    while i < n:
        op = code[i]
        if op == 0x63 and i + 5 <= n:                  # PUSH4
            out.add("0x" + code[i + 1:i + 5].hex())
            i += 5
            continue
        if 0x60 <= op <= 0x7F:                         # PUSH1..PUSH32
            i += 1 + (op - 0x5F)
            continue
        i += 1
    return out


def analyze_code(code_hex: str) -> dict[str, Any]:
    """بايت‑كود → أعمدة `evm_contract` الحسابيّة (بلا نداء شبكة إضافيّ)."""
    body = (code_hex or "0x")[2:] if (code_hex or "0x").startswith("0x") else code_hex
    body = (body or "").lower()
    size = len(body) // 2
    out: dict[str, Any] = {
        "code_size": size,
        "is_proxy": 0,
        "impl_address": None,
        "code_hash": None,
        "function_count": None,
    }
    if size == 0:
        # ليس عقداً. ليس خطأً ولا فراغاً: عنوان قد يكون محفظةً أو عقداً حُذف
        # (SELFDESTRUCT) — والصفر هنا **قياس** فيُكتب.
        return out
    out["code_hash"] = "0x" + keccak256(bytes.fromhex(body)).hex()
    if size == 45 and body.startswith(_PROXY_PREFIX) and body.endswith(_PROXY_SUFFIX):
        out["is_proxy"] = 1
        out["impl_address"] = "0x" + body[20:60]
    sels = _code_selectors(body)
    out["function_count"] = len(sels)
    for col, sigs in _RISK_SELECTORS.items():
        out[col] = 1 if any(selector(s) in sels for s in sigs) else 0
    return out


async def _read_owner(
    rpc: Any, network_id: str, address: str, sleep=asyncio.sleep,
) -> str | None:
    """أوّل صيغة ملكيّة تُجيب. الارتداد جواب («لا هذه الدالّة») لا خطأ.

    العنوان الصفر جوابٌ صحيح لا فراغ — هو بالضبط ما يعني «تُركت الملكيّة».

    وإيقاع بين الصيغ لا نداءات متلاصقة. لكنّ الإيقاع **ليس** ما يمنع الكتم: قياس
    2026-08-13 على `mainnet.base.org` أعطى تسعة نداءات ناجحة ثمّ 429 عند 0.4ث
    وعند 1.0ث سواءً ⇒ الحصّة **بالعدد** في نافذة زمنيّة لا بالتباعد. فالحامي
    الحقيقيّ هو `EVM_CONTRACT_PER_CYCLE` (عملتان = ثمانية نداءات) وقطعُ الخطوة
    عند أوّل كتم؛ والإيقاع يبقى لأنّ ثلاث محاولات في لحظة واحدة اندفاعٌ بلا داعٍ.
    """
    for i, sig in enumerate(_OWNER_CALLS):
        if i:
            await sleep(config.EVM_PACING_SECONDS)
        result = await rpc.eth_call(network_id, address, selector(sig))
        if isinstance(result, str) and len(result) >= 66:
            return "0x" + result[-40:].lower()
    return None


async def run_evm_contract_cycle(
    rpc: Any, db: RecorderDB, recorded_at: str, sleep=asyncio.sleep,
) -> dict[str, int]:
    """الطبقة البطيئة على EVM: شكل العقد وصلاحياته، ساعيّاً، على Base وحدها.

    صفٌّ لكل قياس لا صفٌّ يُحدَّث: `renounceOwnership()` **حدث** يقع وسط النافذة،
    وصفّ واحد يُكتب فوق نفسه يمحو أنّه وقع. نفس علّة `chain_authority`.

    والكتم (429) يقطع الخطوة كلّها لا العملة وحدها: حصّة العقدة العامّة بالعدد في
    نافذة، فما بعد أوّل كتم مكتومٌ سلفاً — ونداءاتٌ نعرف أنّها ستُرفض ثمنُها
    عملات تُوسَم خطأً بلا ذنب.
    """
    stats = {"evm_contract_due": 0, "evm_contract_rows": 0,
             "evm_contract_errors": 0, "evm_contract_proxies": 0,
             # عملات أُخِّرت لأنّ العقدة كتمت الحصّة — لا فشل ولا حالة تُكتب.
             "evm_contract_throttled": 0}
    networks = [str(n) for n in config.EVM_CONTRACT_NETWORKS]
    if not networks:
        return stats
    now_dt = datetime.fromisoformat(recorded_at)
    stale_before = (
        now_dt - timedelta(seconds=config.EVM_CONTRACT_REFRESH_SECONDS)
    ).isoformat()
    error_stale_before = (
        now_dt - timedelta(seconds=config.EVM_CONTRACT_ERROR_RETRY_SECONDS)
    ).isoformat()
    due = db.evm_contract_due(
        limit=config.EVM_CONTRACT_PER_CYCLE,
        stale_before_iso=stale_before,
        error_stale_before_iso=error_stale_before,
        networks=networks,
    )
    stats["evm_contract_due"] = len(due)

    for i, w in enumerate(due):
        addr = str(w["token_address"])
        net = str(w["network_id"] or "")
        status = "error"
        try:
            code = await rpc.get_code(net, addr)
            shape = analyze_code(code)
            owner = None
            if shape["code_size"] > 0:
                await sleep(config.EVM_PACING_SECONDS)
                owner = await _read_owner(rpc, net, addr, sleep)
            row = {
                "token_address": addr,
                "network_id": net,
                "recorded_at": recorded_at,
                "watch_first_seen_at": w["first_seen_at"],
                "entry_signal_id": w.get("entry_signal_id"),
                "is_control": int(w.get("is_control") or 0),
                "owner_address": owner,
                # لا مالك ⇒ NULL لا 1: «لا دالّة ملكيّة» و«الملكيّة متروكة»
                # حالتان مختلفتان، ودمجهما يجعل العمود كذباً (FR-007).
                "is_ownership_renounced": (
                    None if owner is None else (1 if owner == _ZERO_ADDRESS else 0)
                ),
                "raw_json": {
                    "code_size": shape["code_size"],
                    "code_hash": shape["code_hash"],
                    "is_proxy": shape["is_proxy"],
                    "impl_address": shape["impl_address"],
                    "owner": owner,
                },
                **{k: v for k, v in shape.items() if k != "raw_json"},
            }
            db.insert_evm_contract(row)
            status = "ok"
            stats["evm_contract_rows"] += 1
            if shape["is_proxy"]:
                stats["evm_contract_proxies"] += 1
        except EVMRateLimit:
            # الكتم ليس فشل هذه العملة: هو **نهاية حصّتنا** في هذه النافذة، وما
            # بعدها سيُكتم كذلك (مقيس: تسعة نداءات ثمّ 429 على `mainnet.base.org`
            # مهما كان الإيقاع). فلا حالة تُكتب — كتابة `error` تدفع العملة خلف
            # `EVM_CONTRACT_ERROR_RETRY_SECONDS` (15 دقيقة) بلا ذنب، وهي بلا حالة
            # تبقى أوّل المستحقّين في الدورة القادمة ⇒ تقدّمٌ مضمون بلا حلقة.
            stats["evm_contract_throttled"] = len(due) - i
            db.note_error("evm_contract_last_throttle_at", recorded_at)
            break
        except Exception as exc:  # noqa: BLE001 — عملة واحدة لا تُسقط الدورة
            stats["evm_contract_errors"] += 1
            db.note_error(
                "last_error_evm_contract",
                f"{recorded_at}: {addr}: {type(exc).__name__}: {exc}"[:400],
            )
        db.set_evm_contract_state(addr, net, status, recorded_at)
        if i + 1 < len(due):
            await sleep(config.EVM_PACING_SECONDS)
    return stats
