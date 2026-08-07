"""اختبارات مستخرج الميزات — **حرس التسرّب أولاً**.

القانون المُختبَر: كل ميزة قابلة للمعرفة عند t=0 أو قبلها. كل اختبار «تسرّب»
أدناه يزرع بيانات **بعد** t0 ويتأكّد أنّ الصفّ لا يتغيّر — هذه الاختبارات هي
خطّ الدفاع الوحيد ضدّ دقّة وهمية لا تُكتشف إلّا بالمال.
"""
import os

import pytest

import features
from db import RecorderDB

SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")
T0 = 1_785_000_000                     # لحظة القرار في كل الاختبارات
TOK, NET = "0xtok", "56"
H = 3600


@pytest.fixture()
def db(tmp_path):
    d = RecorderDB(str(tmp_path / "t.db"), SCHEMA)
    yield d
    d.close()


def _iso(epoch: int) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _bars(db, start, n, step=300, c=1.0, grow=0.0, tok=TOK, net=NET, res="5"):
    """`grow` نموّ مركّب لكل شمعة (عائد ثابت) — لا خطّي، وإلّا تضاءل العائد
    النسبيّ مع الزمن فبدت السلسلة الصاعدة «ساكنة» في آخرها."""
    rows = []
    for i in range(n):
        px = c * ((1 + grow) ** i)
        rows.append({
            "token_address": tok, "network_id": net, "resolution": res,
            "ts": start + i * step, "o": px, "h": px * 1.01, "l": px * 0.99,
            "c": px, "v": 10.0,
            "h_suspect": 0, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
        })
    db.insert_bars(rows)


def _thesis(db, epoch, tid, user="u1"):
    db.insert_thesis_items([{
        "id": tid, "token_address": TOK, "network_id": NET,
        "created_at": _iso(epoch), "user_handle": user, "user_id": user,
        "num_likes": 5, "num_replies": 0, "equity": 0.0, "trade_id": None,
        "comment": "x", "fetched_at": "t", "raw_json": "{}"}])


def _signal(db, sid, epoch, **kw):
    row = {
        "id": sid, "token_address": TOK, "network_id": NET, "ts": _iso(epoch),
        "recorded_at": _iso(epoch), "signal_type": "large_buy", "ticker": "T",
        "price_usd": 0.001, "fdv": None, "market_cap": 500_000.0,
        "size_usd": 5_000.0, "unique_traders": None, "num_trades": None,
        "minutes": None, "price_change_pct": None, "total_volume": None,
        "are_top_traders": None, "top_trader_ids_json": None,
        "top_trader_match_count": 0, "buyers_best_rank": None,
        "buyer_id": None, "buyer_handle": None, "num_swaps": 2,
        "is_first_buy": 1, "buyer_pnl_pct": None, "avg_cost": None,
        "in_amount": 4_900.0, "in_token_address": None, "out_amount": None,
        "token_amount": None, "realized_pnl_usd": None, "raw_json": "{}",
    }
    row.update(kw)
    db.insert_signal(row)
    return row


# ---------------------------------------------------------------------------
# حرس التسرّب — أهمّ ما في الملفّ
# ---------------------------------------------------------------------------
def test_social_ignores_theses_after_t0(db):
    _thesis(db, T0 - 3600, "before")
    before = features.social_features(db, TOK, NET, T0)
    _thesis(db, T0 + 60, "after")          # وصلت بعد لحظة القرار
    assert features.social_features(db, TOK, NET, T0) == before
    assert before["thesis_counted"] == 1


def test_price_history_ignores_bars_after_t0(db):
    _bars(db, T0 - 86400, 288, c=1.0)      # يوم كامل قبل t0
    before = features.price_history_features(db, TOK, NET, T0)
    _bars(db, T0 + 300, 20, c=99.0)        # انفجار بعد القرار
    assert features.price_history_features(db, TOK, NET, T0) == before


def test_price_history_ignores_open_bar_at_t0(db):
    """إغلاق شمعة بدأت قبل القرار ولم تنته بعد غير معروف عند t0."""
    _bars(db, T0 - 600, 2, c=1.0)
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 60, "o": 1.0, "h": 9.0, "l": 0.5, "c": 8.0,
        "v": 9999.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "t",
    }])
    result = features.price_history_features(db, TOK, NET, T0)
    assert result["bar_vol_1h"] == pytest.approx(20.0)
    assert result["ret_1h_before"] is None


def test_static_snapshot_after_t0_is_invisible(db):
    db._conn.execute(
        """INSERT INTO token_static(
               token_address,network_id,recorded_at,name,symbol,decimals,
               creator_address,raw_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (TOK, NET, _iso(T0 + 60), "future", "FUT", 9, "creator", "{}"),
    )
    db._conn.commit()
    result = features.static_features(db, TOK, NET, T0)
    assert result["name_len"] is None
    assert result["creator_prior_tokens"] is None


def test_market_tick_after_t0_is_invisible(db):
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 120),
        "source": "trending", "price_usd": 0.001, "liquidity": 50_000.0,
        "holders": 100, "raw_json": "{}"})
    before = features.market_features(db, TOK, NET, T0)
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 + 120),
        "source": "trending", "price_usd": 9.9, "liquidity": 9_000_000.0,
        "holders": 9999, "raw_json": "{}"})
    assert features.market_features(db, TOK, NET, T0) == before
    assert before["liquidity"] == 50_000.0


def test_density_ignores_later_signals_and_self(db):
    _signal(db, "s_prev", T0 - 1800)
    _signal(db, "s_self", T0)
    d = features.density_features(db, TOK, NET, T0, exclude_key="s_self")
    _signal(db, "s_after", T0 + 60)
    assert features.density_features(db, TOK, NET, T0, exclude_key="s_self") == d
    assert d["prior_signals_token"] == 1          # السابقة فقط، لا نفسها


def test_creator_prior_tokens_respects_t0(db):
    db.upsert_static({
            "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 3600), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": "Pump.fun",
        "migrated": 1, "graduation_percent": 100.0, "twitter": "x", "telegram": None,
        "website": None, "discord": None, "token_created_at": _iso(T0 - 86400),
            "raw_json": "{}",    })
    db.upsert_static({                       # عملة أقدم لنفس المُنشئ
            "token_address": "0xold", "network_id": NET, "recorded_at": _iso(T0 - 3600), "name": "B",
        "symbol": "B", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": _iso(T0 - 200_000), "raw_json": "{}"})
    s = features.static_features(db, TOK, NET, T0)
    assert s["creator_prior_tokens"] == 1
    db.upsert_static({                       # عملة **لاحقة** لنفس المُنشئ
        "token_address": "0xnew", "network_id": NET,
        "recorded_at": _iso(T0 - 3600), "name": "C",
        "symbol": "C", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": _iso(T0 + 100_000), "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["creator_prior_tokens"] == 1


# ---------------------------------------------------------------------------
# صحّة الحساب
# ---------------------------------------------------------------------------
def test_event_features_derive_ratios_and_logs():
    f = features.event_features(
        {"signal_type": "multi_user_buy", "size_usd": 10_000.0,
         "market_cap": 1_000_000.0, "total_volume": 60_000.0,
         "unique_traders": 12, "buyers_best_rank": 7}, T0)
    assert f["size_to_mcap"] == pytest.approx(0.01)
    assert f["volume_per_trader"] == pytest.approx(5_000.0)
    assert f["rank_le_10"] == 1 and f["rank_le_50"] == 1
    assert f["log_market_cap"] > f["log_size_usd"]


def test_event_features_missing_stay_none_not_zero():
    f = features.event_features({"signal_type": "large_buy"}, T0)
    assert f["size_to_mcap"] is None
    assert f["volume_per_trader"] is None
    assert f["rank_le_10"] is None          # لا رتبة ⇒ لا علم (لا صفر)


def test_price_history_computes_returns_and_flatness(db):
    _bars(db, T0 - 86400, 288, c=1.0, grow=0.0)     # مسطّح تماماً
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_24h_before"] == pytest.approx(0.0)
    assert f["flat_ratio_24h"] == pytest.approx(1.0)   # كل الشموع ساكنة
    assert f["bars_count_24h"] > 200
    assert f["bars_history_h"] == pytest.approx(24.0, abs=0.1)


def test_price_history_detects_pre_signal_pump(db):
    _bars(db, T0 - 86400, 288, c=1.0, grow=0.01)      # صعود متسارع قبل الإشارة
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_24h_before"] > 1.0
    assert f["flat_ratio_24h"] < 0.5
    assert f["dist_from_ath"] is not None and f["dist_from_ath"] <= 0


def test_price_history_skips_suspect_closes(db):
    _bars(db, T0 - 3600, 12, c=1.0)
    db.insert_bars([{                                  # إغلاق مشوّه معلَّم
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 300, "o": 1.0, "h": 9e9, "l": 1.0, "c": 12_052.5,
        "h_suspect": 1, "l_suspect": 0, "c_suspect": 1, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["ret_1h_before"] == pytest.approx(0.0)     # لم يُستعمل الإغلاق الفاسد


def test_social_accel_and_history(db):
    _thesis(db, T0 - 90_000, "old", user="a")          # قبل يوم ونصف
    _thesis(db, T0 - 7200, "mid", user="b")
    _thesis(db, T0 - 600, "new", user="c")
    f = features.social_features(db, TOK, NET, T0)
    assert f["thesis_counted"] == 3
    assert f["thesis_authors_before"] == 3
    assert f["thesis_1h"] == 1 and f["thesis_24h"] == 2
    assert f["thesis_accel"] == pytest.approx(0.5)
    assert f["hours_since_last_thesis"] == pytest.approx(600 / 3600)
    assert f["thesis_history_days"] > 1.0


def test_social_features_do_not_cross_networks(db):
    db.insert_thesis_items([{
        "id": "other-network", "token_address": TOK, "network_id": "1",
        "created_at": _iso(T0 - 60), "user_id": "u", "raw_json": "{}",
    }])
    assert features.social_features(db, TOK, NET, T0)["thesis_counted"] == 0


def test_macro_features_read_hourly_reference(db):
    label, addr, net = features.config.MACRO_BARS[0]
    _bars(db, T0 - 86400 * 2, 48, step=3600, c=100.0, grow=0.01,
          tok=addr, net=net, res=features.config.MACRO_BARS_RESOLUTION)
    f = features.macro_features(db, T0)
    assert f["sol_ret_24h"] is not None


def test_macro_features_ignore_open_hour(db):
    label, addr, net = features.config.MACRO_BARS[0]
    assert label == "SOL"
    _bars(db, T0 - 25 * H, 25, step=H, c=100.0, grow=0.0,
          tok=addr, net=net, res=features.config.MACRO_BARS_RESOLUTION)
    db.insert_bars([{
        "token_address": addr, "network_id": net,
        "resolution": features.config.MACRO_BARS_RESOLUTION,
        "ts": T0 - 60, "o": 100.0, "h": 1000.0, "l": 10.0, "c": 900.0,
        "v": 1.0, "h_suspect": 0, "l_suspect": 0, "c_suspect": 0,
        "fetched_at": "t",
    }])
    assert features.macro_features(db, T0)["sol_ret_24h"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# الصفّ الكامل
# ---------------------------------------------------------------------------
def _outcome(db, key="s1", kind="signal"):
    db.insert_outcome({
        "kind": kind, "key": key, "token_address": TOK, "network_id": NET,
        "signal_type": "large_buy", "is_control": 0, "is_independent": 1,
        "entry_ts": T0, "entry_px": 1.0, "entry_lag_s": 60,
        "max_gain_1h": 0.1, "max_gain_4h": 0.2, "max_gain_24h": 0.5,
        "max_gain_48h": 0.5, "max_drawdown_48h": -0.3, "final_return_48h": 0.4,
        "time_to_peak_h": 3.0, "candles_48h": 500, "suspect_bars": 0,
        "last_bar_lag_h": 0.1, "bars_truncated": 0, "is_rug": 0,
        "split": "train", "status": "ok", "labeled_at": "t",
    })
    return dict(db._conn.execute(
        "SELECT * FROM outcomes WHERE kind=? AND key=?", (kind, key)).fetchone())


def test_build_training_row_has_every_declared_column(db):
    _signal(db, "s1", T0)
    _bars(db, T0 - 3600, 12)
    row = features.build_training_row(db, _outcome(db))
    assert row is not None
    missing = [c for c in features.ROW_COLUMNS if c not in row]
    assert missing == []
    assert row["kind"] == "signal" and row["entry_ts"] == T0
    assert row["final_return_48h"] == 0.4          # الليبل ملصوق
    assert row["market_cap"] == 500_000.0          # الميزة من الحدث


def test_build_training_row_returns_none_without_source_event(db):
    assert features.build_training_row(db, _outcome(db, key="ghost")) is None


def test_build_training_row_marks_asset_class(db):
    _signal(db, "s1", T0, ticker="MEME", price_usd=0.001, market_cap=500_000.0)
    db._conn.execute(
        "INSERT INTO token_class(token_address, network_id, asset_class, classified_at) "
        "VALUES(?,?,?,?)", (TOK, NET, "meme", "t"))
    db._conn.commit()
    row = features.build_training_row(db, _outcome(db))
    assert row["asset_class"] == "meme"


def test_asset_class_uses_only_event_time_observations(db):
    _signal(db, "s1", T0, price_usd=0.001, market_cap=500_000.0, ticker="MEME")
    db._conn.execute(
        "INSERT INTO token_class(token_address,network_id,asset_class,classified_at) "
        "VALUES(?,?,?,?)",
        (TOK, NET, "priced", _iso(T0 + 86400)),
    )
    db._conn.commit()
    row = features.build_training_row(db, _outcome(db))
    assert row["asset_class"] == "meme"


def test_is_live_flag_splits_on_live_start(db):
    """الراية تفصل الحِقبتين عند LIVE_START_TS بالضبط: قبله رجعيّ، عنده/بعده حيّ.
    هذا الفلتر هو ما يُقصي البيانات الرجعية من التدريب — كسره يعيد تسريب الحِقبة."""
    cut = features.config.LIVE_START_TS
    # رجعيّ: قبل الحدّ بثانية
    _signal(db, "retro", cut - 1)
    _bars(db, cut - 3600, 12)
    r_retro = features.build_training_row(
        db, _outcome_at(db, "retro", cut - 1))
    assert r_retro["is_live"] == 0
    # حيّ: عند الحدّ تماماً
    _signal(db, "live", cut)
    r_live = features.build_training_row(db, _outcome_at(db, "live", cut))
    assert r_live["is_live"] == 1


def test_activity_rows_are_never_marked_live(db):
    db.insert_activity_events([{
        "id": "retro", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0), "recorded_at": _iso(T0 + 3600),
        "ticker": "MEME", "price_usd": 0.001, "market_cap": 500_000.0,
        "raw_json": "{}",
    }])
    outcome = _outcome(db, key="retro", kind="activity")
    assert features.build_training_row(db, outcome)["is_live"] == 0


def _outcome_at(db, key, entry_ts):
    o = _outcome(db, key=key)
    db._conn.execute(
        "UPDATE outcomes SET entry_ts=? WHERE kind='signal' AND key=?",
        (entry_ts, key))
    db._conn.commit()
    o["entry_ts"] = entry_ts
    return o


def test_label_columns_are_never_features():
    """حرس بنيويّ: لا عمود ليبل يتسلّل إلى قائمة الميزات."""
    assert not set(features.LABEL_COLUMNS) & set(features.FEATURE_COLUMNS)
    for banned in ("final_return", "max_gain", "max_drawdown", "is_rug", "time_to_peak"):
        assert not any(banned in c for c in features.FEATURE_COLUMNS)


def test_num_likes_is_not_a_feature():
    """الإعجابات ممنوعة: قيمتها وقت السحب لا وقت الكتابة (README §9)."""
    assert not any("like" in c.lower() for c in features.FEATURE_COLUMNS)


# ---------------------------------------------------------------------------
# إصلاحات التدقيق (2026-07-30) — كلّ اختبار يمنع عودة عيب مقيس
# ---------------------------------------------------------------------------
def test_ath_ignores_suspect_high(db):
    """39 شمعة إغلاقها سليم وقمّتها مشوّهة كانت تعطي dist_from_ath = −0.99999997
    على 49 صفّاً (القمّة 96,311 بدل 0.0143)."""
    _bars(db, T0 - 3600, 12, c=1.0)
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 600, "o": 1.0, "h": 96_311.0, "l": 0.9, "c": 1.0,
        "h_suspect": 1, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["dist_from_ath"] > -0.5          # لا −0.99999997


def test_measured_zero_size_is_kept_not_nulled(db):
    """1,640 حدثاً حجمه 0.0 فعلاً (خروج كامل): `a or b` كان يحوّله None."""
    f = features.event_features({"signal_type": "large_sell", "size_usd": 0.0}, T0)
    assert f["size_usd"] == 0.0
    # ويقع الرجوع إلى usd_amount فقط حين الغياب الحقيقيّ
    f2 = features.event_features({"event_type": "swap_buy", "usd_amount": 26.7}, T0)
    assert f2["size_usd"] == 26.7


def test_density_counts_both_sources(db):
    """الحساب من signal_events وحده جعل 88% من الصفوف الرجعية أصفاراً بنيويّة
    مقابل 4% للأمامية — فصار العمود دالّاً على نوع الصفّ لا على نشاط العملة."""
    db.insert_activity_events([{
        "id": "a1", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0 - 1800), "recorded_at": "t", "raw_json": "{}",
    }])
    d = features.density_features(db, TOK, NET, T0, exclude_key=None)
    assert d["prior_signals_token"] == 1              # الحدث الرجعيّ محسوب
    assert d["global_signals_1h"] == 1
    assert d["minutes_since_prior_signal"] == pytest.approx(30.0)


def test_density_deduplicates_same_event_across_sources(db):
    _signal(db, "same", T0 - 1800)
    db.insert_activity_events([{
        "id": "same", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0 - 1800), "recorded_at": "t",
        "raw_json": "{}",
    }])
    result = features.density_features(db, TOK, NET, T0, exclude_key=None)
    assert result["prior_signals_token"] == 1
    assert result["global_signals_1h"] == 1


def test_density_does_not_cross_networks(db):
    db.insert_activity_events([{
        "id": "other", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": "1", "ts": _iso(T0 - 1800), "recorded_at": "t",
        "raw_json": "{}",
    }])
    assert features.density_features(
        db, TOK, NET, T0, exclude_key=None
    )["prior_signals_token"] == 0


def test_density_still_excludes_self(db):
    db.insert_activity_events([{
        "id": "self", "event_type": "multi_user_buy", "token_address": TOK,
        "network_id": NET, "ts": _iso(T0), "recorded_at": "t", "raw_json": "{}",
    }])
    assert features.density_features(
        db, TOK, NET, T0, exclude_key="self")["prior_signals_token"] == 0


def _social(db, epoch, total, authors, holders, replies=0):
    db.insert_social({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(epoch),
        "thesis_count": min(total, 100), "thesis_total": total,
        "thesis_sampled": min(total, 100), "has_next_page": 1 if total > 100 else 0,
        "thesis_likes": 0, "thesis_replies": replies, "thesis_authors": authors,
        "holder_authors": holders, "newest_thesis_at": _iso(epoch), "raw_json": "{}",
    })


def test_social_snapshot_gives_true_total_and_holder_ratio(db):
    """`token_thesis` عيّنة (سقف ~400) بينما `thesis_total` هو الحقيقة (29,595)."""
    _social(db, T0 - 600, total=29_595, authors=80, holders=20, replies=7)
    f = features.social_features(db, TOK, NET, T0)
    assert f["social_thesis_total"] == 29_595
    assert f["social_holder_authors"] == 20
    assert f["social_holder_ratio"] == pytest.approx(0.25)
    assert f["social_replies"] == 7
    assert f["social_snapshot_age_min"] == pytest.approx(10.0)


def test_social_momentum_delta_from_two_snapshots(db):
    _social(db, T0 - 7200, total=100, authors=10, holders=3)
    _social(db, T0 - 300, total=160, authors=18, holders=6)
    f = features.social_features(db, TOK, NET, T0)
    assert f["social_total_delta_1h"] == 60
    assert f["social_total_growth_1h"] == pytest.approx(0.6)
    assert f["social_authors_delta_1h"] == 8


def test_social_snapshot_after_t0_is_invisible(db):
    _social(db, T0 - 300, total=50, authors=5, holders=2)
    before = features.social_features(db, TOK, NET, T0)
    _social(db, T0 + 300, total=9_999, authors=500, holders=400)
    assert features.social_features(db, TOK, NET, T0) == before


def test_thesis_cap_flag_marks_saturated_count(db):
    for i in range(_cap := features._THESIS_PAGE_CAP):
        _thesis(db, T0 - 1000 - i, f"t{i}", user=f"u{i % 7}")
    f = features.social_features(db, TOK, NET, T0)
    assert f["thesis_counted"] >= _cap
    assert f["thesis_counted_capped"] == 1


def test_market_dense_windows_and_ratios(db):
    db.insert_tick({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 60),
        "source": "trending", "price_usd": 0.001, "liquidity": 50_000.0,
        "market_cap": 500_000.0, "holders": 100, "change_1h": 0.12,
        "change_4h": 0.4, "change_24h": 1.2, "volume_1h": 20_000.0,
        "volume_4h": 60_000.0, "volume_24h": 150_000.0, "txn_count_1h": 300,
        "txn_count_24h": 2_000, "circulating_supply": 600_000_000.0,
        "total_supply": 1_000_000_000.0, "raw_json": "{}"})
    f = features.market_features(db, TOK, NET, T0)
    assert f["tick_change_1h"] == 0.12 and f["tick_volume_4h"] == 60_000.0
    assert f["tick_txn_1h"] == 300
    assert f["volume_to_liquidity"] == pytest.approx(3.0)
    assert f["liquidity_to_mcap"] == pytest.approx(0.1)
    assert f["float_ratio"] == pytest.approx(0.6)


def test_bar_volume_surge_before_signal(db):
    _bars(db, T0 - 86400, 288, c=1.0)                 # حجم 10 لكل شمعة
    db.insert_bars([{
        "token_address": TOK, "network_id": NET, "resolution": "5",
        "ts": T0 - 300, "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 5_000.0,
        "h_suspect": 0, "l_suspect": 0, "c_suspect": 0, "fetched_at": "t",
    }])
    f = features.price_history_features(db, TOK, NET, T0)
    assert f["bar_vol_1h"] >= 5_000.0
    assert f["bar_vol_24h"] > f["bar_vol_1h"]
    assert f["vol_surge_1h"] > 0.5                     # الساعة الأخيرة تطغى
    assert 0.0 <= f["up_candle_ratio_24h"] <= 1.0


def test_daily_ath_is_used_only_after_history_is_complete(db):
    _bars(db, T0 - 3600, 12, c=1.5)
    db.insert_bars([
        {
            "token_address": TOK, "network_id": NET, "resolution": "1D",
            "ts": T0 - 3 * 86400, "o": 4.0, "h": 10.0, "l": 3.0,
            "c": 5.0, "v": 1.0, "h_suspect": 0, "l_suspect": 0,
            "c_suspect": 0, "fetched_at": "t",
        },
        {   # اليوم لم يغلق بعد t0؛ لا يجوز أن يتسرّب ATH منه
            "token_address": TOK, "network_id": NET, "resolution": "1D",
            "ts": T0 - 12 * 3600, "o": 5.0, "h": 99.0, "l": 4.0,
            "c": 90.0, "v": 1.0, "h_suspect": 0, "l_suspect": 0,
            "c_suspect": 0, "fetched_at": "t",
        },
    ])
    db._conn.execute(
        """INSERT INTO historical_bars_state(
               token_address,network_id,resolution,cursor_to,oldest_ts,last_status,
               candles,calls,attempts,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (TOK, NET, "1D", T0, T0 - 3 * 86400, "partial", 2, 1, 1, "t"),
    )
    partial = features.price_history_features(db, TOK, NET, T0)
    assert partial["ath_history_complete"] == 0
    assert partial["dist_from_ath"] > -0.5

    db._conn.execute(
        "UPDATE historical_bars_state SET last_status='ok' "
        "WHERE token_address=? AND network_id=? AND resolution='1D'",
        (TOK, NET),
    )
    complete = features.price_history_features(db, TOK, NET, T0)
    assert complete["ath_history_complete"] == 1
    assert complete["ath_history_days"] == pytest.approx(3.0)
    assert complete["dist_from_ath"] == pytest.approx(1.5 / 10.0 - 1.0)


def test_buyer_and_text_features(db):
    f = features.event_features({
        "signal_type": "large_buy", "price_usd": 0.002, "avg_cost": 0.001,
        "token_amount": 5_000.0, "out_amount": 4_900.0, "ticker": "PEPE2é",
        "top_trader_ids_json": '["a","b","c"]', "top_trader_match_count": 1,
    }, T0)
    assert f["price_to_avg_cost"] == pytest.approx(2.0)   # يشتري أعلى من متوسّطه
    assert f["token_amount"] == 5_000.0
    assert f["top_traders_listed"] == 3
    assert f["top_trader_match_ratio"] == pytest.approx(1 / 3)
    assert f["ticker_len"] == 6
    assert f["ticker_has_digit"] == 1
    assert f["ticker_non_ascii"] == 1


def test_static_name_and_decimals(db):
    db.upsert_static({
        "token_address": TOK, "network_id": NET, "recorded_at": _iso(T0 - 60),
        "name": "Café Coin", "symbol": "CAFE", "decimals": 9, "mintable": None,
        "freezable": None, "is_scam": None, "creator_address": None,
        "launchpad_name": None, "migrated": None, "graduation_percent": None,
        "twitter": None, "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 - 3600, "raw_json": "{}"})
    f = features.static_features(db, TOK, NET, T0)
    assert f["decimals"] == 9
    assert f["name_len"] == 9
    assert f["name_non_ascii"] == 1


def test_no_feature_column_lost_in_refactor():
    """كل ميزة معلَنة تُنتَج فعلاً — يمنع عموداً معلَناً بلا حساب (NULL أبديّ)."""
    produced = set()
    produced |= set(features.event_features({}, T0))
    assert set(features.FEATURE_COLUMNS) >= produced


def test_model_training_view_enforces_safe_cohort(db):
    cols = features.ROW_COLUMNS
    base = {c: None for c in cols}
    base.update({
        "key": "safe", "kind": "signal", "token_address": TOK,
        "network_id": NET, "entry_ts": T0, "asset_class": "meme",
        "split": "train", "is_independent": 1, "is_live": 1,
        "status": "ok", "feature_version": features.FEATURE_VERSION,
        "built_at": _iso(T0 + 49 * H),
    })
    excluded = dict(base, key="activity", kind="activity", is_live=0)
    db._conn.executemany(
        f"INSERT INTO training_rows({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        [tuple(row[c] for c in cols) for row in (base, excluded)],
    )
    rows = db._conn.execute("SELECT key FROM model_training_rows").fetchall()
    assert [row["key"] for row in rows] == ["safe"]


def test_model_training_view_deduplicates_semantic_feed_events(db):
    _signal(db, "a", T0)
    event = _signal(db, "b", T0)
    db._conn.execute(
        "INSERT INTO signal_events(id,token_address,network_id,ts,recorded_at,"
        "signal_type,raw_json) VALUES(?,?,?,?,?,?,?)",
        ("b", TOK, NET, event["ts"], event["recorded_at"],
         event["signal_type"], "{}"),
    )
    db._conn.commit()
    cols = features.ROW_COLUMNS
    rows = []
    for key in ("a", "b"):
        row = {c: None for c in cols}
        row.update({
            "kind": "signal", "key": key, "token_address": TOK,
            "network_id": NET, "entry_ts": T0, "asset_class": "meme",
            "split": "train", "is_independent": 1, "is_live": 1,
            "status": "ok", "feature_version": features.FEATURE_VERSION,
            "built_at": _iso(T0 + 49 * H),
        })
        rows.append(row)
    db._conn.executemany(
        f"INSERT INTO training_rows({', '.join(cols)}) "
        f"VALUES({', '.join('?' for _ in cols)})",
        [tuple(row[c] for c in cols) for row in rows],
    )
    kept = db._conn.execute("SELECT key FROM model_training_rows").fetchall()
    assert [row["key"] for row in kept] == ["a"]


def test_incremental_builder_rebuild_does_not_repeat_current_version(db):
    from build_training_rows import pending_outcomes

    _signal(db, "s1", T0, ticker="MEME", price_usd=0.001, market_cap=500_000.0)
    _outcome(db, key="s1")
    first = pending_outcomes(db, rebuild=False, limit=10)
    assert [row["key"] for row in first] == ["s1"]
    db._conn.execute(
        "INSERT INTO training_rows(kind,key,token_address,network_id,entry_ts,"
        "feature_version,built_at) VALUES(?,?,?,?,?,?,?)",
        ("signal", "s1", TOK, NET, T0, features.FEATURE_VERSION, _iso(T0)),
    )
    db._conn.commit()
    assert pending_outcomes(db, rebuild=False, limit=10) == []


def test_incremental_builder_can_limit_work_to_live_independent_signals(db):
    from build_training_rows import pending_outcomes

    cut = features.config.LIVE_START_TS
    for key, ts in (("old", cut - 1), ("live", cut), ("duplicate", cut + 1)):
        _signal(db, key, ts)
        _outcome_at(db, key, ts)
    db._conn.execute(
        "UPDATE outcomes SET is_independent=0 WHERE kind='signal' AND key='duplicate'"
    )
    _outcome_at(db, "watch", cut + 2)
    db._conn.execute("UPDATE outcomes SET kind='watch' WHERE key='watch'")
    db._conn.commit()

    rows = pending_outcomes(
        db, rebuild=False, limit=10, model_candidates_only=True
    )
    assert [row["key"] for row in rows] == ["live"]


# ---------------------------------------------------------------------------
# اختلاف صيغ الأختمة من fomo — كشفه تقرير التغطية (token_age_h فارغاً 100%)
# ---------------------------------------------------------------------------
def test_epoch_of_accepts_iso_and_numeric_epoch():
    assert features.epoch_of("2026-07-30T10:00:00Z") == 1785405600
    assert features.epoch_of("2026-07-30T10:00:00+00:00") == 1785405600
    assert features.epoch_of(1784983617) == 1784983617       # epoch رقميّ
    assert features.epoch_of("1784983617") == 1784983617     # نصّ رقميّ
    assert features.epoch_of(1784983617000) == 1784983617    # مللي ثانية
    assert features.epoch_of(None) is None
    assert features.epoch_of("") is None
    assert features.epoch_of("not-a-date") is None


def test_token_age_computed_from_numeric_created_at(db):
    """fomo تخزّن `token_created_at` epoch رقمياً — كان يُقرأ None فيضيع العمر."""
    db.upsert_static({
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso(T0 - 60), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": None, "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 - 7200,          # رقميّ لا ISO
        "raw_json": "{}"})
    f = features.static_features(db, TOK, NET, T0)
    assert f["token_age_h"] == pytest.approx(2.0)


def test_negative_token_age_is_dropped_not_learned(db):
    """ختم fomo قد يخصّ إدراجاً لا إنشاءً فيسبق t0 (مقيس: −177 ساعة).
    عمر سالب مستحيل ⇒ None لا رقم يتعلّمه النموذج."""
    db.upsert_static({
        "token_address": TOK, "network_id": NET,
        "recorded_at": _iso(T0 - 60), "name": "A",
        "symbol": "A", "decimals": 18, "mintable": None, "freezable": None,
        "is_scam": None, "creator_address": None, "launchpad_name": None,
        "migrated": None, "graduation_percent": None, "twitter": None,
        "telegram": None, "website": None, "discord": None,
        "token_created_at": T0 + 86400,          # «أُنشئت» بعد الإشارة
        "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["token_age_h"] is None


def test_creator_prior_tokens_with_numeric_timestamps(db):
    for addr, created in ((TOK, T0 - 3600), ("0xold", T0 - 100_000), ("0xnew", T0 + 5)):
        db.upsert_static({
            "token_address": addr, "network_id": NET,
            "recorded_at": _iso(T0 - 60), "name": "x",
            "symbol": "x", "decimals": 18, "mintable": None, "freezable": None,
            "is_scam": None, "creator_address": "0xdev", "launchpad_name": None,
            "migrated": None, "graduation_percent": None, "twitter": None,
            "telegram": None, "website": None, "discord": None,
            "token_created_at": created, "raw_json": "{}"})
    assert features.static_features(db, TOK, NET, T0)["creator_prior_tokens"] == 1
