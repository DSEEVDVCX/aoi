"""ما نقضه القياس: الشبكات الثلاث، والعمود المتروك عن قصد.

القياسُ الأوّل (2026-08-13) حصر فحصَ العقد على Base لأنّ BSC وروبن‑هود ظهرتا
«قوالب متكرّرة». وأُعيد القياس (2026-08-22) فتبيّن أنّ التكرار قِيس في `code_size`
وحده، وأنّ `is_proxy` **هو نفسه المعلومة**: 26 من 40 على BSC مقابل 3 من 40 على
Base. فهذان الاختبارانِ يمنعان العودةَ إلى الحصر بلا قياسٍ جديد.
"""
import config
import features


def test_contract_scan_covers_the_three_measured_networks():
    """حصرُ الفحص على Base يُفقد 40% من صفوف النموذج بلا سبب مقيس."""
    assert set(config.EVM_CONTRACT_NETWORKS) == {"8453", "56", "4663"}


def test_every_scanned_network_has_an_rpc_endpoint():
    """شبكةٌ تُفحص بلا عقدة = خطأٌ كلّ دورة لا عمودٌ فارغ."""
    for network in config.EVM_CONTRACT_NETWORKS:
        assert config.EVM_RPC_URLS.get(str(network)), network


def test_contract_batch_stays_under_the_measured_base_quota():
    """حصّة `mainnet.base.org` تسعة نداءات في نافذة، والعملة تكلّف أربعة ⇒ اثنتان.

    توسيعُ الشبكات لا يبرّر رفعَ الدفعة: الرقم حصّةُ عقدةٍ واحدة لا سعةُ الطبقة،
    وقد كُتمت العقدةُ فعلاً عند أربع في دورتين حيّتين متتاليتين.
    """
    assert config.EVM_CONTRACT_PER_CYCLE * 4 < 9


def test_retired_dead_column_is_out_of_the_feature_list():
    """`top10_holders_pct` صفرٌ من 3.8 مليون صفّ — المصدر لا يرسل المفتاح."""
    assert "top10_holders_pct" not in features.FEATURE_COLUMNS
    assert "top10_holders_pct" not in features.ROW_COLUMNS


def test_its_two_live_replacements_are_still_features():
    """الإسقاط لا يجوز إلّا والبديل قائم — وإلّا فقدنا التركّز لا العمودَ الميّت."""
    assert "chain_top10_pct" in features.FEATURE_COLUMNS
    assert "onchain_top10_pct" in features.FEATURE_COLUMNS
