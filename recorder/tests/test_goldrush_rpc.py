import json

import httpx
import pytest

import evm_rpc
import goldrush_rpc


NET = "143"
TOK = "0x350035555e10d9afaf1566aaebfced5ba6c27777"
TOPIC = evm_rpc.TRANSFER_TOPIC


def _event(block, *, topic=TOPIC, sender=TOK):
    return {
        "block_signed_at": "2025-11-25T23:20:03Z",
        "block_height": block,
        "tx_offset": 5,
        "log_offset": 44,
        "tx_hash": "0x" + "ab" * 32,
        "raw_log_topics": [
            topic,
            "0x" + "0" * 64,
            "0x" + "0" * 24 + "11" * 20,
        ],
        "raw_log_data": "0x01",
        "sender_address": sender,
    }


async def _noop(_seconds):
    return None


@pytest.fixture(autouse=True)
def _goldrush_enabled(monkeypatch):
    """تُبقي محوّل GoldRush مُختبَراً بعد رفعه من مسار الإنتاج.

    `config.GOLDRUSH_REPLAY_CHAINS` صار فارغاً لأنّ الحساب يردّ 402 منذ
    2026-08-17، ولو تُرك الاختبار معتمداً على القيمة الإنتاجيّة لصارت ثمانية
    اختبارات تمرّ **بلا أن تلمس سطراً** من المحوّل — تخضير كاذب. والفرق يظهر يوم
    يُشترى رصيد: إعادة التشغيل سطرٌ في `config` لا حَفرٌ في كودٍ لم يُختبَر منذ
    شهور. ومن يريد اختبار حالة الإنتاج نفسها يصفّر الخريطة في اختباره.
    """
    monkeypatch.setattr(
        goldrush_rpc.config, "GOLDRUSH_REPLAY_CHAINS", {NET: "monad-mainnet"},
    )


async def test_goldrush_filters_and_maps_transfer_logs(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    seen = []

    def handler(request):
        seen.append(request)
        items = [
            _event(10),
            _event(20, topic="0x" + "ff" * 32),
            _event(30, sender="0x" + "22" * 20),
        ]
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": items},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert calls == 1
    assert complete is True
    assert resume == end
    assert [int(log["blockNumber"], 16) for log in logs] == [10]
    assert logs[0]["address"] == TOK
    assert logs[0]["blockTimestamp"] == hex(1764112803)
    assert all(request.headers["authorization"] == "Bearer secret" for request in seen)
    assert seen[0].url.params["address"] == TOK
    assert seen[0].url.params["topics"] == TOPIC
    assert seen[0].url.params["starting-block"] == "earliest"
    stats = rpc.key_stats()
    assert stats["goldrush_calls"] == 1
    assert stats["goldrush_events"] == 1
    assert stats["fallback_calls"] == 0


async def test_goldrush_keeps_only_completed_chunks_at_call_cap(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 2_000)

    def handler(_request):
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(10)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 9_999, max_calls=1, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert (len(logs), calls, complete, resume) == (1, 1, False, 2_000)


async def test_goldrush_halves_a_range_when_provider_returns_range_hint(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 20_000)
    seen = []

    def handler(request):
        raw_lo = request.url.params["starting-block"]
        lo = 0 if raw_lo == "earliest" else int(raw_lo)
        hi = int(request.url.params["ending-block"])
        seen.append((lo, hi))
        if hi - lo + 1 > 10_000:
            return httpx.Response(200, json={
                "data": None,
                "error": False,
                "info": {"message": "Query returned more than 10000 results"},
            })
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(lo)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 19_999, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen[:3] == [(0, 19_999), (0, 9_999), (10_000, 19_999)]
    assert (len(logs), calls, complete, resume) == (2, 3, True, 19_999)


async def test_goldrush_uses_provider_range_hint_without_skipping_blocks(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 20_000)
    seen = []

    def handler(request):
        raw_lo = request.url.params["starting-block"]
        lo = 0 if raw_lo == "earliest" else int(raw_lo)
        hi = int(request.url.params["ending-block"])
        seen.append((lo, hi))
        if hi - lo + 1 > 4_000:
            return httpx.Response(200, json={
                "data": None,
                "error": False,
                "info": {"message": "Try with this block range [1000,4999]"},
            })
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(lo)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 5_999, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen[0] == (0, 5_999)
    assert seen[1:] == [(0, 999), (1000, 4999), (5000, 5_999)]
    assert (len(logs), calls, complete, resume) == (3, 4, True, 5_999)


async def test_goldrush_halves_when_provider_repeats_the_rejected_range(monkeypatch, tmp_path):
    """Monad قد تقترح النطاق المرفوض نفسه؛ لا نعيده حتى نفاد سقف النداءات."""
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 8_000)
    seen = []

    def handler(request):
        raw_lo = request.url.params["starting-block"]
        lo = 0 if raw_lo == "earliest" else int(raw_lo)
        hi = int(request.url.params["ending-block"])
        seen.append((lo, hi))
        if hi - lo + 1 > 4_000:
            return httpx.Response(200, json={
                "data": None,
                "error": False,
                "info": {"message": f"Try with this block range [{lo},{hi}]"},
            })
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(lo)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 7_999, max_calls=5, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen == [(0, 7_999), (0, 3_999), (4_000, 7_999)]
    assert (len(logs), calls, complete, resume) == (2, 3, True, 7_999)


async def test_a_truncated_page_shrinks_the_range_instead_of_losing_events(
    monkeypatch, tmp_path,
):
    """أخطرُ ردٍّ في هذا المحوّل: **نجاحٌ ناقص**.

    نهاية الأحداث تُصفِّح، و`_chunk` تقرأ صفحةً واحدة، فالمدى المطلوب (مليونا
    كتلة) يعني أنّ أيّ عملة متحرّكة تُقرأ نصفها. والدفتر تراكميّ فالفقد لا يظهر
    نقصاً في صفٍّ بل رصيداً سالباً أو تركّزاً كاذباً بعد آلاف النداءات — وهو ما
    وقع على Base فعلاً. فالاقتطاع يُعالَج كحدّ مدى: يُصغَّر ويُعاد كاملاً.
    """
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 20_000)
    seen = []

    def handler(request):
        raw_lo = request.url.params["starting-block"]
        lo = 0 if raw_lo == "earliest" else int(raw_lo)
        hi = int(request.url.params["ending-block"])
        seen.append((lo, hi))
        data = {"items": [_event(lo)]}
        if hi - lo + 1 > 10_000:
            # ردُّ 200 مع صفحةٍ تالية: لا خطأ، لا رسالة، وحدثٌ واحد ظاهر.
            data["links"] = {"prev": "https://api.covalenthq.com/v1/next", "next": None}
        return httpx.Response(200, json={"error": False, "data": data})

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, _calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 19_999, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen[:3] == [(0, 19_999), (0, 9_999), (10_000, 19_999)]
    assert (len(logs), complete, resume) == (2, True, 19_999)


async def test_a_count_larger_than_the_page_is_truncation_too(monkeypatch, tmp_path):
    """`total_count` يفضح البقيّة حين يسكت `links` — والشكُّ يُصغِّر المدى."""
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 20_000)
    seen = []

    def handler(request):
        raw_lo = request.url.params["starting-block"]
        lo = 0 if raw_lo == "earliest" else int(raw_lo)
        hi = int(request.url.params["ending-block"])
        seen.append((lo, hi))
        wide = hi - lo + 1 > 10_000
        return httpx.Response(200, json={"error": False, "data": {
            "items": [_event(lo)],
            "pagination": {
                "has_more": False, "page_number": 0, "page_size": 100,
                "total_count": 240 if wide else 1,
            },
        }})

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        logs, _calls, complete, _resume = await rpc.get_logs_paged(
            NET, [TOK], 0, 19_999, max_calls=10, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen[:2] == [(0, 19_999), (0, 9_999)]
    assert (len(logs), complete) == (2, True)


async def test_a_single_block_that_stays_truncated_fails_loudly(monkeypatch, tmp_path):
    """قاعُ التصغير: كتلةٌ واحدة لا تُصغَّر، فالإصرار حلقةٌ لا تنتهي.

    والبديل الآخر — قبولُ الصفحة — هو بالضبط العطبُ الذي يمنعه هذا الفرع. فيُرفَع
    خطأً صريحاً: العملة تُسجَّل `error` بسببٍ مقروء، ولا تُكتب أرقامٌ ناقصة.
    """
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    # فوق `GOLDRUSH_MIN_RANGE` وإلّا سقط المدى إلى RPC العادي ولم يُختبَر الفرع.
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_BLOCK_CHUNK", 2048)
    calls_made = []

    def handler(request):
        calls_made.append(request.url.params["ending-block"])
        return httpx.Response(200, json={"error": False, "data": {
            "items": [_event(0)],
            "links": {"prev": "https://api.covalenthq.com/v1/next"},
        }})

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(evm_rpc.EVMRPCError) as raised:
            await rpc.get_logs_paged(NET, [TOK], 0, 2047, max_calls=40, sleep=_noop)
    finally:
        await rpc.aclose()

    assert "مقتطعة" in str(raised.value)
    # 2048 كتلة تُنصَّف حتى الواحدة: 12 طلباً، ثمّ يُرفَع الخطأ ولا يُعاد.
    assert len(calls_made) == 12


async def test_non_goldrush_network_uses_normal_rpc(monkeypatch):
    rpc = goldrush_rpc.GoldRushReplayRPC()

    async def fallback(*args, **kwargs):
        return ["fallback"], 1, True, 9

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    try:
        result = await rpc.get_logs_paged("4663", [TOK], 0, 9)
    finally:
        await rpc.aclose()

    assert result == (["fallback"], 1, True, 9)


async def test_goldrush_retries_a_transient_transport_error(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_RETRIES", 1)
    monkeypatch.setattr(goldrush_rpc.config, "EVM_RATE_LIMIT_BACKOFF_SECONDS", 0)
    attempts = 0

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadError("reset")
        return httpx.Response(200, json={
            "error": False,
            "data": {"items": [_event(10)]},
        })

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=5, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert attempts == 2
    assert (len(logs), calls, complete, resume) == (1, 2, True, end)


async def test_small_range_falls_back_to_normal_rpc(monkeypatch):
    rpc = goldrush_rpc.GoldRushReplayRPC()

    async def fallback(*args, **kwargs):
        return ["fallback"], 1, True, 99

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    try:
        result = await rpc.get_logs_paged(NET, [TOK], 0, 9, max_calls=1)
    finally:
        await rpc.aclose()

    assert result == (["fallback"], 1, True, 99)


async def test_credit_exhaustion_disables_goldrush_and_falls_back(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_key": "secret"}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    goldrush_calls = 0
    fallback_calls = []

    def handler(_request):
        nonlocal goldrush_calls
        goldrush_calls += 1
        return httpx.Response(402, json={
            "data": None,
            "error": True,
            "error_message": "Credit limit exceeded for your account.",
        })

    async def fallback(_self, network_id, addresses, from_block, to_block, **kwargs):
        fallback_calls.append((network_id, tuple(addresses), from_block, to_block))
        return [{"blockNumber": hex(from_block)}], 1, True, to_block

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first = await rpc.get_logs_paged(NET, [TOK], 0, 9_999, max_calls=5, sleep=_noop)
        second = await rpc.get_logs_paged(NET, [TOK], 10_000, 19_999, max_calls=5, sleep=_noop)
    finally:
        await rpc.aclose()

    assert goldrush_calls == 1
    assert fallback_calls == [
        (NET, (TOK,), 0, 9_999),
        (NET, (TOK,), 10_000, 19_999),
    ]
    assert first == ([{"blockNumber": "0x0"}], 2, True, 9_999)
    assert second == ([{"blockNumber": hex(10_000)}], 1, True, 19_999)
    stats = rpc.key_stats()
    assert stats["goldrush_calls"] == 1
    assert stats["fallback_calls"] == 2
    assert stats["fallback_events"] == 2


async def test_goldrush_rotates_to_second_key_before_rpc_fallback(monkeypatch, tmp_path):
    key_path = tmp_path / "keys.json"
    key_path.write_text(json.dumps({"goldrush_api_keys": ["bad-key", "good-key"]}))
    monkeypatch.setattr(goldrush_rpc.config, "chain_keys_path", lambda: str(key_path))
    seen = []

    def handler(request):
        auth = request.headers["authorization"]
        seen.append(auth)
        if auth == "Bearer bad-key":
            return httpx.Response(402, json={"error": True, "error_message": "credits exhausted"})
        return httpx.Response(200, json={"error": False, "data": {"items": [_event(10)]}})

    rpc = goldrush_rpc.GoldRushReplayRPC()
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        end = goldrush_rpc.config.GOLDRUSH_BLOCK_CHUNK - 1
        logs, calls, complete, resume = await rpc.get_logs_paged(
            NET, [TOK], 0, end, max_calls=5, sleep=_noop,
        )
    finally:
        await rpc.aclose()

    assert seen == ["Bearer bad-key", "Bearer good-key"]
    assert (len(logs), calls, complete, resume) == (1, 2, True, end)


async def test_empty_chain_map_defers_every_network_to_normal_rpc(monkeypatch):
    """الخريطة الفارغة تمريرٌ شفّاف — وهي حالة الإنتاج الآن (402 منذ 2026-08-17).

    الرفع بالخريطة لا بحذف الملفّ: المحوّل يبقى كاملاً وتحت الاختبار، ولا يُستدعى
    منه شيء. فهذا اختبارُ **مسار العودة**: لا نداء GoldRush واحد، والعمل كلّه
    يُحسَب على مسار RPC العادي في `key_stats` كي يظهر في الرصد أين يجري العمل.
    """
    monkeypatch.setattr(goldrush_rpc.config, "GOLDRUSH_REPLAY_CHAINS", {})
    rpc = goldrush_rpc.GoldRushReplayRPC()

    async def fallback(*args, **kwargs):
        return ["fallback"], 3, True, 40_000

    monkeypatch.setattr(evm_rpc.EVMRPC, "get_logs_paged", fallback)
    try:
        result = await rpc.get_logs_paged(NET, [TOK], 0, 40_000, max_calls=9)
    finally:
        await rpc.aclose()

    assert result == (["fallback"], 3, True, 40_000)
    stats = rpc.key_stats()
    assert stats["goldrush_calls"] == 0
    assert stats["fallback_calls"] == 1
    assert stats["fallback_events"] == 1
