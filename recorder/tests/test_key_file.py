"""صيغةُ ملفّ المفاتيح: ما تقرأه ثلاثُ عمليّاتٍ وتكتبه اللوحة.

الاختبارات هنا لا تلمس مفتاحاً حقيقيّاً ولا الملفَّ الحقيقيّ — `tmp_path` دائماً.
"""
import json

import key_file


def test_tail_shows_four_characters_and_nothing_more():
    """الخفضُ المسموح من FR-013 محدودٌ بأربعة أحرف — لا خامسٍ ولا طول."""
    assert key_file.tail("abcdefghijkl-WXYZ") == "WXYZ"
    assert len(key_file.tail("x" * 64)) == 4


def test_tail_refuses_short_keys_because_four_of_twelve_is_a_third_of_the_secret():
    """مفتاحٌ قصير: التمييزُ يسقط والسرُّ يبقى — لا نصفَ حلٍّ بينهما."""
    assert key_file.tail("short") == ""
    assert key_file.tail("x" * 11) == ""
    assert key_file.tail("x" * 12) == "xxxx"


def test_load_treats_a_missing_or_broken_file_as_empty():
    """القارئ لا يتعثّر: جهازٌ بلا مفاتيح حالةٌ عاديّة، والعطبُ مسؤوليّة الكاتب."""
    assert key_file.load("no-such-file.json") == {}


def test_load_treats_a_corrupt_file_as_empty(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert key_file.load(str(path)) == {}

    path.write_text('["list at top level"]', encoding="utf-8")
    assert key_file.load(str(path)) == {}


def test_entries_reads_all_three_shapes_and_defaults_enabled_to_true():
    """الملفّ الموجود كُتب بيدٍ قبل التعدّد؛ السطرُ اليدويّ بلا `enabled` عامل."""
    plain = key_file.entries({"helius_api_key": " one "}, "helius_api_keys", "helius_api_key")
    assert plain == [{"key": "one", "label": "", "enabled": True}]

    listed = key_file.entries(
        {"helius_api_keys": ["a", {"key": "b", "label": "حساب ثانٍ"}]},
        "helius_api_keys", "helius_api_key",
    )
    assert [row["key"] for row in listed] == ["a", "b"]
    assert [row["enabled"] for row in listed] == [True, True]
    assert listed[1]["label"] == "حساب ثانٍ"


def test_entries_keeps_disabled_rows_for_the_dashboard_to_re_enable():
    """المعطّل يبقى معروضاً وإلّا لم يكن للإيقاف المؤقّت طريقُ رجعة."""
    rows = key_file.entries(
        {"k": [{"key": "a", "enabled": False}, "b"]}, "k", "k1",
    )
    assert [(row["key"], row["enabled"]) for row in rows] == [("a", False), ("b", True)]


def test_entries_drops_blanks_and_duplicates():
    rows = key_file.entries(
        {"k": ["a", " a ", "", {"key": ""}, None, 7, {"key": "b"}]}, "k", "k1",
    )
    assert [row["key"] for row in rows] == ["a", "b"]


def test_an_empty_plural_list_hides_the_legacy_singular():
    """حذفُ آخرِ مفتاح يجب أن يبقى محذوفاً — لا يعود المفردُ من قبره."""
    assert key_file.entries(
        {"k": [], "k1": "old-value"}, "k", "k1",
    ) == []


def test_save_entries_round_trips_and_drops_the_legacy_singular(tmp_path):
    """المفردُ يُحذف عند أوّل كتابة: مصدرُ حقيقةٍ واحد لا اثنان يتباعدان."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"helius_api_key": "old", "other_setting": 7}), encoding="utf-8")

    key_file.save_entries(str(path), "helius_api_keys", "helius_api_key", [
        {"key": "new-one-123456", "label": "الرئيسي"},
        {"key": "new-two-123456", "label": "", "enabled": False},
    ])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "helius_api_key" not in data
    # ما لا يملكه هذا المزوّد لا يُمَسّ: الملفّ مشتركٌ بين ثلاثة مزوّدين وإعداداتٍ أخرى.
    assert data["other_setting"] == 7
    rows = key_file.load_entries(str(path), "helius_api_keys", "helius_api_key")
    assert [(row["key"], row["label"], row["enabled"]) for row in rows] == [
        ("new-one-123456", "الرئيسي", True),
        ("new-two-123456", "", False),
    ]


def test_save_entries_leaves_other_providers_untouched(tmp_path):
    path = tmp_path / "keys.json"
    key_file.save_entries(str(path), "helius_api_keys", "helius_api_key",
                          [{"key": "helius-key-1234"}])
    key_file.save_entries(str(path), "nodereal_api_keys", "nodereal_api_key",
                          [{"key": "nodereal-key-12"}])

    assert [r["key"] for r in key_file.load_entries(
        str(path), "helius_api_keys", "helius_api_key")] == ["helius-key-1234"]
    assert [r["key"] for r in key_file.load_entries(
        str(path), "nodereal_api_keys", "nodereal_api_key")] == ["nodereal-key-12"]


def test_save_entries_replaces_atomically_and_leaves_no_temp_file(tmp_path):
    """المسجّل يقرأ هذا الملفّ عند **كلّ نداء**؛ ملفٌّ نصفَ مكتوبٍ = عطلٌ كامل."""
    path = tmp_path / "keys.json"
    key_file.save_entries(str(path), "k", "k1", [{"key": "value-123456"}])
    key_file.save_entries(str(path), "k", "k1", [{"key": "value-123456"},
                                                 {"key": "second-123456"}])

    assert [p.name for p in tmp_path.iterdir()] == ["keys.json"]
    assert path.read_text(encoding="utf-8").endswith("\n")


def _source(name: str) -> str:
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, name), encoding="utf-8") as fh:
        return fh.read()


def test_probe_endpoints_match_the_clients_that_use_the_keys():
    """فحصُ عنوانٍ آخر يقول «سليم» عن مفتاحٍ لا يعمل حيث يُستعمل فعلاً.

    نطابق المضيف لا الرابط كاملاً: العميل يبني مساره بنفسه (مفتاحٌ في الاستعلام،
    أو في المسار، أو في ترويسة) — المضيفُ هو ما يجب ألّا يتباعد. وقراءةُ النصّ
    مقصودة: العناوين في العملاء نصوصٌ داخل الدوالّ لا ثوابتُ وحدة، وفحصٌ نصّيّ
    يمسك التباعدَ بلا إعادة هيكلةِ عميلٍ يعمل الآن.
    """
    import config

    assert "helius-rpc.com" in key_file.PROVIDERS["helius"]["probe"]["url"]
    assert "helius-rpc.com" in config.SOLANA_RPC_URL
    assert "nodereal.io" in key_file.PROVIDERS["nodereal"]["probe"]["url"]
    assert "nodereal.io" in _source("nodereal_rpc.py")
    audit = _source("audit_evm_ledger.py")
    assert "g.alchemy.com" in key_file.PROVIDERS["alchemy"]["probe"]["url"]
    assert "g.alchemy.com" in audit
    assert "lb.drpc.org" in key_file.PROVIDERS["drpc"]["probe"]["url"]
    assert "lb.drpc.org" in audit


def test_provider_field_names_match_what_read_keys_is_actually_called_with():
    """اسمُ حقلٍ مختلفٌ حرفاً واحداً = لوحةٌ تكتب في مكانٍ لا يقرأه أحد.

    نفحص نداءات `read_keys` في كلّ من يقرأ مفاتيح: كلُّ زوج (جمع، مفرد) يُنادى
    به فعلاً يجب أن يعرفه `PROVIDERS`، وإلّا فمزوّدٌ يقرؤه المسجّل ولا تراه
    اللوحة — أو أسوأ: اللوحة تكتب حقلاً لا يقرؤه أحد ويبدو أنّ المفتاح أُضيف.
    """
    import re

    known = {(meta["plural"], meta["singular"]) for meta in key_file.PROVIDERS.values()}
    pattern = re.compile(r"""read_keys\(\s*["'](\w+)["'],\s*["'](\w+)["']""")
    found = set()
    for name in ("solana_rpc.py", "nodereal_rpc.py", "run_chain.py",
                 "run_evm_replay.py", "audit_evm_ledger.py"):
        found |= {tuple(match) for match in pattern.findall(_source(name))}

    assert found, "لم يُعثر على أيّ نداء read_keys — تغيّر شكل النداء فالفحص أعمى"
    assert found <= known, f"حقولٌ لا تعرفها اللوحة: {found - known}"


def test_the_audit_tools_field_triplets_are_the_same_ones_the_dashboard_writes():
    """المدقّق ينادي `read_keys` بمتغيّراتٍ من جدولٍ خاصّ، فالفحصُ النصّيّ أعمى عنه.

    فحصُ النداءات فوق يقرأ حروفاً بين قوسين، و`audit_evm_ledger` يمرّر أسماءَ
    حقولٍ من `PROVIDER_FIELDS` — لا يراها ذاك النمط. فيُطابَق الجدولان مباشرةً:
    حرفٌ واحد يفترق هنا يعني لوحةً تكتب مفتاحاً في حقلٍ لا يقرؤه المدقّق، فيبدو
    المفتاحُ مُضافاً وهو غيرُ موجود.
    """
    import audit_evm_ledger

    for name, (plural, singular, env) in audit_evm_ledger.PROVIDER_FIELDS.items():
        assert name in key_file.PROVIDERS, name
        meta = key_file.PROVIDERS[name]
        assert (meta["plural"], meta["singular"], meta["env"]) == (
            plural, singular, env,
        ), name
    # وكلُّ شبكةٍ في المدقّق مسارُها مزوّدٌ مُسجَّل — لا اسمٌ مخترَعٌ عند التشغيل.
    for network, routes in audit_evm_ledger.ARCHIVE_ROUTES.items():
        for provider, _ in routes:
            assert provider in key_file.PROVIDERS, (network, provider)


def test_every_provider_has_the_five_fields_the_dashboard_reads():
    """اللوحة تقرأ الخمسةَ بلا حماية، فمزوّدٌ ناقصُ حقلٍ يُسقطها بـKeyError."""
    for name, meta in key_file.PROVIDERS.items():
        assert set(meta) == {"plural", "singular", "env", "title", "probe"}, name
        assert meta["probe"]["method"] == "POST", name
        assert "{key}" in meta["probe"]["url"], name
        assert meta["probe"]["json"]["method"], name
