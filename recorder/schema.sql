-- مخطّط مسجّل البيانات التاريخي (recorder.db)
-- المبدأ الحاكم: نحفظ الخام (raw_json) دائماً بجانب الحقول المستخرجة، حتى نستطيع
-- إعادة اشتقاق أي ميزة لاحقاً من الأرشيف دون خسارة الماضي. المسجّل لا يحسب أي
-- label (منع تسرّب المستقبل) — يسجّل خاماً بختم زمني دقيق فقط.
-- كل الأختام الزمنية نصّية ISO-8601 UTC (recorded_at) لتفادي الغموض.
--
-- تنبيه على أعمدة raw_json: قيمتها **BLOB مضغوط بـ zlib** لا نصّ. SQLite
-- ديناميكيّ الأنواع فيقبل ذلك في عمود TEXT. لا تقرأها بـ json.loads مباشرة —
-- استعمل db.decode_raw() الذي يفكّ الضغط ويقبل أيضاً الصفوف القديمة المكتوبة
-- نصّاً قبل تفعيل الضغط. سبب الضغط: لقطة كاملة كل دقيقة كانت تُنمّي القاعدة
-- ~1.3 GB يومياً؛ الضغط بلا خسارة يخفضها ~4.7 أضعاف. المفتاح meta.raw_encoding
-- يوثّق الترميز الفعّال.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- من نراقب ومتى تنتهي المراقبة (48 ساعة لكل عملة).
CREATE TABLE IF NOT EXISTS watchlist (
    token_address    TEXT    NOT NULL,
    network_id       TEXT    NOT NULL,
    first_seen_at    TEXT    NOT NULL,           -- ISO UTC وقت أول دخول
    source           TEXT    NOT NULL,           -- الإشارة التي أدخلتها، أو 'control'
    watch_until      TEXT    NOT NULL,           -- ISO UTC = first_seen_at + 48h
    entry_signal_id  TEXT,                       -- signal_events.id الذي أدخلها (NULL للضابطة)
    active           INTEGER NOT NULL DEFAULT 1, -- 1 نشط، 0 انتهت مدّته
    -- 1 = عملة **ضابطة**: دخلت بالاختيار العشوائي لا بإشارة.
    -- بلا هذا الصنف السالب لا يمكن للنموذج أن يعرف هل الإشارة تعني شيئاً أصلاً:
    -- كل عملة لها سلسلة سعرية كانت قد دخلت بإشارة، فلا مرجع للمقارنة.
    is_control       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist (active, watch_until);
CREATE INDEX IF NOT EXISTS idx_watchlist_control ON watchlist (is_control, active);

-- سجلّ immutable لكل نافذة مراقبة. `watchlist` حالة تشغيل قابلة للترقية وإعادة
-- التنشيط، أما هذا الجدول فهو مصدر الحقيقة للمقارنة والتوسيم ولا تُحدَّث نافذته
-- بعد الإدراج. الفصل يمنع ترقية الضابطة إلى إشارة من محو نافذتها الأصلية.
CREATE TABLE IF NOT EXISTS watch_windows (
    token_address    TEXT    NOT NULL,
    network_id       TEXT    NOT NULL,
    first_seen_at    TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    watch_until      TEXT    NOT NULL,
    entry_signal_id  TEXT,
    is_control       INTEGER NOT NULL DEFAULT 0,
    admission_price_usd REAL,
    admission_source TEXT,
    design_version   INTEGER NOT NULL DEFAULT 2,
    PRIMARY KEY (token_address, network_id, first_seen_at)
);
CREATE INDEX IF NOT EXISTS idx_watch_windows_pending
    ON watch_windows (first_seen_at, source, is_control);

-- لحظة القرار t=0: حدث إشارة من الـ feed (شراء متعدد / شراء كبير).
CREATE TABLE IF NOT EXISTS signal_events (
    id                     TEXT PRIMARY KEY,     -- feed event id (idempotent)
    token_address          TEXT NOT NULL,
    network_id             TEXT,
    ts                     TEXT,                 -- createdAt الأصلي من fomo
    recorded_at            TEXT NOT NULL,        -- متى سجّلناه نحن (ISO UTC)
    signal_type            TEXT NOT NULL,        -- type من الـ feed
    ticker                 TEXT,
    price_usd              REAL,
    fdv                    REAL,
    market_cap             REAL,
    num_trades             INTEGER,
    unique_traders         INTEGER,
    minutes                INTEGER,
    price_change_pct       REAL,
    total_volume           REAL,
    are_top_traders        INTEGER,              -- حكم fomo نفسها (bool)
    top_trader_ids_json    TEXT,                 -- topTraders[].id كمصفوفة JSON
    top_trader_match_count INTEGER,              -- كم منهم في صدارتنا (المطابقة)
    buyers_best_rank       INTEGER,              -- أفضل (أصغر) رتبة بين المشترين
    -- حقول الشراء المفرد (large_buy): مشترٍ واحد بدل topTraders[].
    buyer_id               TEXT,                 -- userId للمشتري (مطابَق بالصدارة أيضاً)
    buyer_handle           TEXT,
    num_swaps              INTEGER,              -- عدد صفقات هذا المشتري
    is_first_buy           INTEGER,              -- أوّل شراء له لهذه العملة؟
    buyer_pnl_pct          REAL,                 -- ربح/خسارة المشتري وقت الحدث
    avg_cost               REAL,                 -- متوسّط تكلفة المشتري
    -- حجم الصفقة: كان مفقوداً كلياً، فكانت صفقة بـ 1,000$ وأخرى بـ 141,000$
    -- متطابقتين تماماً أمام النموذج رغم أنّ "شراء كبير" وسيطه 3,448$ فقط.
    size_usd               REAL,                 -- currentSizeUsd: حجم مركزه بعد الشراء
    in_amount              REAL,                 -- inHumanAmount: ما دفعه فعلاً
    in_token_address       TEXT,                 -- بماذا دفع (USDC/عملة أخرى)
    out_amount             REAL,                 -- outHumanAmount: ما استلمه
    token_amount           REAL,                 -- humanTokenAmount: إجمالي ما يملكه
    realized_pnl_usd       REAL,                 -- realizedPnlUsd وقت الحدث
    raw_json               TEXT NOT NULL         -- الحدث الخام كاملاً
);
CREATE INDEX IF NOT EXISTS idx_signal_token ON signal_events (token_address, network_id);

-- لقطة سوق كاملة لكل عملة مراقَبة، كل دقيقة. المصدر الأساسي للسلاسل الزمنية.
CREATE TABLE IF NOT EXISTS market_ticks (
    token_address      TEXT NOT NULL,
    network_id         TEXT,
    recorded_at        TEXT NOT NULL,            -- ISO UTC وقت اللقطة
    source             TEXT NOT NULL,            -- trending / verified / getBars ...
    price_usd          REAL,
    liquidity          REAL,
    market_cap         REAL,
    holders            INTEGER,
    top10_holders_pct  REAL,
    change_5m          REAL,
    change_1h          REAL,
    change_4h          REAL,
    change_12h         REAL,
    change_24h         REAL,
    volume_5m          REAL,
    volume_1h          REAL,
    volume_4h          REAL,
    volume_12h         REAL,
    volume_24h         REAL,
    txn_count_1h       INTEGER,
    txn_count_4h       INTEGER,
    txn_count_12h      INTEGER,
    txn_count_24h      INTEGER,
    buy_count_1h       INTEGER,
    buy_count_4h       INTEGER,
    buy_count_12h      INTEGER,
    buy_count_24h      INTEGER,
    sell_count_1h      INTEGER,
    sell_count_4h      INTEGER,
    sell_count_12h     INTEGER,
    sell_count_24h     INTEGER,
    unique_buys_1h     INTEGER,
    unique_buys_4h     INTEGER,
    unique_buys_12h    INTEGER,
    unique_buys_24h    INTEGER,
    unique_sells_1h    INTEGER,
    unique_sells_4h    INTEGER,
    unique_sells_12h   INTEGER,
    unique_sells_24h   INTEGER,
    circulating_supply REAL,
    total_supply       REAL,
    raw_json           TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at, source)
);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON market_ticks (token_address, recorded_at);

-- ثوابت العملة: تُلتقط مرّة واحدة عند الدخول (mint/freeze/creator/socials...).
CREATE TABLE IF NOT EXISTS token_static (
    token_address      TEXT NOT NULL,
    network_id         TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    name               TEXT,
    symbol             TEXT,
    decimals           INTEGER,
    mintable           INTEGER,
    freezable          INTEGER,
    is_scam            INTEGER,
    creator_address    TEXT,
    launchpad_name     TEXT,
    migrated           INTEGER,
    graduation_percent REAL,
    twitter            TEXT,
    telegram           TEXT,
    website            TEXT,
    discord            TEXT,
    token_created_at   TEXT,                     -- createdAt للعملة من fomo
    raw_json           TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id)
);

-- أرشيف خام دوري لكل مصدر كاملاً (لإعادة الاشتقاق مستقبلاً).
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    source      TEXT NOT NULL,                   -- trending / verified / feed ...
    raw_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_source_ts ON snapshots (source, recorded_at);

-- شموع OHLCV لكل عملة مراقَبة — مصدر الحقيقة السعرية للتوسيم لاحقاً.
--
-- لماذا جدول منفصل عن market_ticks: الأخير مصدره قوائم trending/verified، وهي
-- قوائم منسَّقة من ~46/~37 عملة لا خلاصة أسعار. فربع العملات المراقَبة لم تحصل
-- على أي نقطة سعرية إطلاقاً، والباقي تغطيته 52-75% من الدقائق. أمّا getBarsNew
-- فيعطي سلسلة سعرية حقيقية لأي عملة، وبتاريخ يسبق الإشارة.
--
-- ts هو epoch seconds لفتح الشمعة (كما يعيده fomo) — لا نصّ ISO، لأنّ المقارنات
-- الرقمية هنا في صميم حساب max_gain/drawdown.
-- الشمعة الأحدث قد تكون قيد التكوّن فتُراجَع في السحب التالي، لذا الإدراج
-- OR REPLACE لا OR IGNORE: القيمة الأحدث لنفس الختم هي الصحيحة.
CREATE TABLE IF NOT EXISTS token_bars (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    ts            INTEGER NOT NULL,       -- ختم الشمعة (epoch ثوانٍ، UTC)
    o             REAL,
    h             REAL,
    l             REAL,
    c             REAL,
    v             REAL,
    -- تشوّه المنبع: قيمة تتجاوز جارتيها بـ×10 ولا تستمرّ (انظر bar_context_flags).
    -- تُستبعد من الحساب فقط — القيمة الخام تبقى كما وردت: الخام لا يُصلَح.
    h_suspect     INTEGER NOT NULL DEFAULT 0,   -- قمّة مستحيلة (شوهد ×119 مليون)
    l_suspect     INTEGER NOT NULL DEFAULT 0,   -- قاع مستحيل
    c_suspect     INTEGER NOT NULL DEFAULT 0,   -- الإغلاق نفسه مشوّه (شوهد 12,052.5)
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, resolution, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_token_ts ON token_bars (token_address, ts);

-- حالة سحب الشموع لكل عملة: تُقاد بها الجدولة الدوّارة (نسحب الأقدم أوّلاً).
-- last_status يفرّق بين "لا بيانات لدى fomo" (no_data — نهائيّ غالباً) و"خطأ"
-- (عابر) حتى لا نُهدر محاولات على عملات ميّتة.
CREATE TABLE IF NOT EXISTS bars_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                    -- ok / no_data / error
    candles       INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- استرجاع التاريخ الطويل بدقّة يومية لحساب ATH الحقيقي قبل t0.
-- تبقى الحالة `partial` حتى نصل إلى بداية السلسلة؛ لا يجوز للمستخرج استعمال
-- الشموع اليومية قبل `ok` لأنّ أقدم (وربما أعلى) جزء قد يكون لم يُسحب بعد.
CREATE TABLE IF NOT EXISTS historical_bars_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    cursor_to     INTEGER,
    oldest_ts     INTEGER,
    last_status   TEXT NOT NULL,       -- partial / ok / empty_retry / no_data / error
    candles       INTEGER NOT NULL DEFAULT 0,
    calls         INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, resolution)
);

-- الطبقة الاجتماعية لكل عملة مراقَبة (POST/GET /feed/token/thesis).
--
-- عملات الميم تحرّكها الحشود لا الأساسيات، وهذا البُعد كان غائباً كلياً: نسجّل
-- السعر والحجم والحائزين، ولا نسجّل كم شخصاً يتحدّث عن العملة ولا كم يُعجَب
-- بحديثه. `equity` = حصّة كاتب الأطروحة نفسه فيها (هل يروّج لما يملك؟).
-- سلسلة زمنية: الفرق بين لقطتين يعطي **تسارع** الزخم الاجتماعي، وهو أهمّ من
-- المستوى المطلق.
CREATE TABLE IF NOT EXISTS token_social (
    token_address    TEXT NOT NULL,
    network_id       TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    -- العدد **الحقيقي** من المغلّف (responseObject.count). الاستجابة تعيد 100
    -- عنصر كحدّ أقصى بينما count قد يبلغ الآلاف (شوهد 3111)، فعدّ العناصر
    -- وحده يتشبّع ويُفقد أقوى تمييز في هذه الطبقة.
    thesis_total     INTEGER,
    thesis_sampled   INTEGER,                    -- كم عنصراً رأينا فعلاً (≤100)
    has_next_page    INTEGER,                    -- هل بقيت صفحات (عيّنة مبتورة)
    thesis_count     INTEGER,                    -- = thesis_sampled (توافق قديم)
    -- ما يلي محسوب على **العيّنة** (أحدث 100) لا على الكلّ:
    thesis_likes     INTEGER,                    -- مجموع الإعجابات
    thesis_replies   INTEGER,                    -- مجموع الردود
    thesis_authors   INTEGER,                    -- كتّاب مميّزون (لا تكرار)
    holder_authors   INTEGER,                    -- منهم من يملك حصّة (equity>0)
    newest_thesis_at TEXT,                       -- أحدث أطروحة (طزاجة النقاش)
    raw_json         TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_social_token_ts ON token_social (token_address, recorded_at);

-- أطروحة واحدة لكل صفّ — يتيح إعادة بناء **العدد التاريخي**: كم أطروحة كانت
-- موجودة لحظة الإشارة؟ (`SELECT COUNT(*) ... WHERE created_at <= :t`).
-- هذا ما جعل استرجاع الماضي ممكناً: كل أطروحة تحمل ختم كتابتها.
--
-- تحذير جوهريّ: `num_likes`/`num_replies` هي **القيمة وقت السحب** لا وقت
-- الكتابة. أطروحة عمرها يومان لها 50 إعجاباً اليوم — كم كان لها ساعة الإشارة؟
-- لا سجلّ لذلك لدى fomo. لذا `fetched_at` مسجَّل: هو زمن قياس الإعجابات.
CREATE TABLE IF NOT EXISTS token_thesis (
    id            TEXT PRIMARY KEY,             -- معرّف الأطروحة (idempotent)
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,                -- وقت الكتابة — تاريخيّ حقيقيّ
    user_handle   TEXT,
    user_id       TEXT,
    num_likes     INTEGER,                      -- قيمة وقت السحب لا وقت الكتابة
    num_replies   INTEGER,
    equity        REAL,
    trade_id      TEXT,
    comment       TEXT,
    fetched_at    TEXT NOT NULL,                -- زمن قياس الإعجابات
    raw_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thesis_token_created
    ON token_thesis (token_address, created_at);

-- أحداث النشاط التاريخية من GET /feed/tradingActivity (مشيّاط lastId رجوعاً في
-- الزمن — مُثبت 2026-07-28؛ الـ /feed الأماميّ «أحدث فقط» وترقيمه مُتجاهَل).
-- مصدر مستقلّ عن signal_events: يجمع أحداث multi_user_* نفسها (تطابق مُثبت
-- بالمعرّف حيّاً: 32/42) **و** أحداث swap_buy/swap_sell الفردية بـusd_amount
-- التي لا يعرضها /feed إطلاقاً (0/42 تطابق). جدول منفصل حتى تبقى مجموعة الجمع
-- الأماميّة نقيّة؛ يُملأ بأثر رجعيّ عبر backfill_activity.py فقط.
-- شكلان: مسطّح (swap_*/thesis: usdAmount/marketCap/price في الأعلى) ومتداخٍ
-- (multi_user_*: body بنفس حقول /feed). رتبة المتصدّر وقت الحدث غير متاحة
-- تاريخياً — لا top_trader_match_count هنا (تُشتقّ لاحقاً من أرشيف الصدارة).
CREATE TABLE IF NOT EXISTS activity_events (
    id                  TEXT PRIMARY KEY,       -- معرّف الحدث (idempotent)
    event_type          TEXT NOT NULL,          -- swap_buy/swap_sell/multi_user_buy/thesis/...
    token_address       TEXT,
    network_id          TEXT,
    ts                  TEXT,                   -- createdAt الأصلي (ISO UTC)
    recorded_at         TEXT NOT NULL,          -- متى سحبناه نحن (ISO UTC)
    user_id             TEXT,
    user_handle         TEXT,
    trade_id            TEXT,
    usd_amount          REAL,                   -- usdAmount (الأحداث المسطّحة)
    price_usd           REAL,                   -- من الأعلى أو body حسب الشكل
    market_cap          REAL,
    fdv                 REAL,
    equity              REAL,
    num_trades          INTEGER,                -- حقول body لأحداث multi_user_*
    unique_traders      INTEGER,
    minutes             INTEGER,
    price_change_pct    REAL,
    total_volume        REAL,
    are_top_traders     INTEGER,
    top_trader_ids_json TEXT,
    ticker              TEXT,
    raw_json            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_token_ts ON activity_events (token_address, ts);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_events (ts);

-- تصنيف الأصل لكل عملة: fomo منصّة متعدّدة الأصول لا سوق ميمات.
-- مقيس 2026-07-30: BTC/ETH/SOL/USDT وذهب PAXG وأسهم مرمّزة (AAPL/MSTR/HOOD/
-- INTC/META/SNDK/MU) داخل أرشيفنا. أصلٌ بتريليون أو سهم آبل لا يسلك سلوك عملة
-- عمرها ساعتان، فخلطهما يفسد التدريب وأيّ استنتاج عن أثر القيمة السوقية.
-- مشتقّ بالكامل من المشاهدات (extract.classify_asset) ⇒ يُعاد بناؤه متى شئنا
-- عبر classify_tokens.py، والقيم الخام لا تتغيّر.
CREATE TABLE IF NOT EXISTS token_class (
    token_address  TEXT NOT NULL,
    network_id     TEXT NOT NULL,
    asset_class    TEXT NOT NULL,   -- meme | major | stable | priced
    reason         TEXT,            -- القاعدة التي حكمت (للتدقيق لا للتجميل)
    symbol         TEXT,
    price_min      REAL,
    price_max      REAL,
    market_cap_max REAL,
    observations   INTEGER,
    classified_at  TEXT NOT NULL,
    PRIMARY KEY (token_address, network_id)
);
CREATE INDEX IF NOT EXISTS idx_token_class_class ON token_class (asset_class);

-- حالة سحب الشموع الرجعية لأحداث activity_events (نفس نمط bars_fetch_state).
-- يكتبها backfill_activity_bars.py فقط. الموسِّم لا يوسم حدث نشاط إلّا بعد
-- status='ok' لعملته — وإلّا كتب no_entry أبديّاً قبل وصول الشموع (التوسيم
-- idempotent لا يُراجَع). no_data بعد MAX_ATTEMPTS = العملة بلا سلسلة عند
-- fomo (ميّتة/مُتآكلة) — تُقاس كنسبة انحياز بقاء في الرجعيّ، لا تُخفى.
CREATE TABLE IF NOT EXISTS activity_bars_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                    -- ok / no_data / error
    candles       INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- حالة سحب الطبقة الاجتماعية (نفس نمط bars_fetch_state، بدورة أبطأ).
CREATE TABLE IF NOT EXISTS social_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                          -- ok / empty / error
    items         INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- لقطات بوابة مخاطر التداول من POST /proxy/tokenWarnings.
--
-- هذه **مشاهدة زمنية بعد القبول** وليست ميزة معروفة بأثر رجعي عند t0. لذلك
-- نحفظ first_seen_at/entry_signal_id كي يستطيع التحليل اللاحق ضبط زمن الدخول
-- على recorded_at (اكتمال الفحص)، ولا يلصق التحذير لاحقاً بصفّ الإشارة القديم
-- فينشأ تسرّب للمستقبل.
--
-- `gate_status='pass'` يعني أن مزوّد التحذيرات لم يمنع الشراء/البيع ولم يرجع
-- تحذيراً عالي الخطورة في تلك اللحظة؛ لا يعني أن البيع مثبت بمحاكاة معاملة.
CREATE TABLE IF NOT EXISTS token_risk_assessments (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    disable_buying      INTEGER,                 -- NULL = لم يصرّح المزود
    disable_selling     INTEGER,                 -- NULL = مجهول، لا نفترض False
    warning_count       INTEGER NOT NULL,
    severe_count        INTEGER NOT NULL,
    high_count          INTEGER NOT NULL,
    gate_status         TEXT NOT NULL,           -- pass / blocked / review / unknown
    warning_types_json  TEXT NOT NULL,
    warnings_json       TEXT NOT NULL,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_risk_token_ts
    ON token_risk_assessments (token_address, network_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_risk_gate_ts
    ON token_risk_assessments (gate_status, recorded_at);

-- حالة الجدولة الدوّارة لبوابة المخاطر. الخطأ يُعاد سريعاً، لأن غياب نتيجة
-- الفحص يجب أن يبقى `unknown` ولا يجوز أن يمرّ كعملة آمنة.
CREATE TABLE IF NOT EXISTS risk_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                          -- ok / blocked / review / unknown / error
    warnings      INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- فحص مباشر على السلسلة، منفصل عن تحذيرات مزوّد Fomo. يشمل صلاحيات SPL/
-- Token-2022، كود ERC-20/proxy/owner، ومحاكاة نقل حائز حين تتاح. لا نسمّيها
-- sell simulation: نقل التوكن لا يثبت مسار بيع DEX ورسومه وسيولته.
CREATE TABLE IF NOT EXISTS token_chain_assessments (
    token_address              TEXT NOT NULL,
    network_id                 TEXT NOT NULL,
    recorded_at                TEXT NOT NULL,
    watch_first_seen_at        TEXT NOT NULL,
    entry_signal_id            TEXT,
    is_control                 INTEGER NOT NULL DEFAULT 0,
    chain_kind                 TEXT NOT NULL,       -- solana / evm / unsupported
    rpc_chain_id               TEXT,                -- قد يختلف عن networkId (1337→999)
    gate_status                TEXT NOT NULL,       -- pass/review/blocked/unknown/unsupported
    reason_codes_json          TEXT NOT NULL,
    contract_exists            INTEGER,
    token_standard             TEXT,
    program_or_implementation  TEXT,
    owner_authority            TEXT,
    owner_renounced            INTEGER,
    mint_authority             TEXT,
    freeze_authority           TEXT,
    paused                     INTEGER,
    upgradeable                INTEGER,
    dangerous_capabilities_json TEXT NOT NULL,
    transfer_simulation_status TEXT NOT NULL,
    top1_account_pct           REAL,
    top10_accounts_pct         REAL,
    details_json               TEXT NOT NULL,
    raw_json                   BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_chain_assessment_token_ts
    ON token_chain_assessments (token_address, network_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_chain_assessment_gate_ts
    ON token_chain_assessments (gate_status, recorded_at);

CREATE TABLE IF NOT EXISTS chain_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                            -- ok/review/blocked/unknown/error/unsupported
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- أحدث حكم مركّب صالح للعرض/بوابة التنفيذ. النجاح يحتاج نجاح المصدرين؛ غياب
-- أحدهما أو unsupported يبقى unknown (fail-closed).
CREATE VIEW IF NOT EXISTS latest_token_safety AS
WITH provider_latest AS (
    SELECT token_address, network_id, MAX(recorded_at) AS recorded_at
      FROM token_risk_assessments GROUP BY token_address, network_id
), provider AS (
    SELECT a.token_address, a.network_id, a.recorded_at, a.gate_status
      FROM token_risk_assessments a JOIN provider_latest l
        ON l.token_address=a.token_address AND l.network_id=a.network_id
       AND l.recorded_at=a.recorded_at
), chain_latest AS (
    SELECT token_address, network_id, MAX(recorded_at) AS recorded_at
      FROM token_chain_assessments GROUP BY token_address, network_id
), chain_scan AS (
    SELECT a.token_address, a.network_id, a.recorded_at, a.gate_status
      FROM token_chain_assessments a JOIN chain_latest l
        ON l.token_address=a.token_address AND l.network_id=a.network_id
       AND l.recorded_at=a.recorded_at
)
SELECT w.token_address, w.network_id, w.first_seen_at, w.entry_signal_id,
       w.is_control, p.recorded_at AS provider_recorded_at,
       p.gate_status AS provider_status, c.recorded_at AS chain_recorded_at,
       c.gate_status AS chain_status,
       CASE
         WHEN p.gate_status='blocked' OR c.gate_status='blocked' THEN 'blocked'
         WHEN p.gate_status IS NULL OR c.gate_status IS NULL THEN 'unknown'
         WHEN p.gate_status='unknown'
           OR c.gate_status IN ('unknown','unsupported') THEN 'unknown'
         WHEN p.gate_status='review' OR c.gate_status='review' THEN 'review'
         WHEN p.gate_status='pass' AND c.gate_status='pass' THEN 'pass'
         ELSE 'unknown'
       END AS combined_status
  FROM watchlist w
  LEFT JOIN provider p
    ON p.token_address=w.token_address AND p.network_id=w.network_id
  LEFT JOIN chain_scan c
    ON c.token_address=w.token_address AND c.network_id=w.network_id;

-- النتائج (labels): يملؤها الـ labeler (عملية FomoLabeler المنفصلة)، لا المسجّل
-- — المسجّل يسجّل خاماً فقط (منع تسرّب المستقبل)، والتوسيم لا يجري إلّا بعد
-- اكتمال نافذة الـ48 ساعة.
--
-- المفتاح (kind, key) لا (token, entry_ts): القديم كان سيتصادم حتماً — 201
-- إشارة على عملة واحدة. صنفان يُوسمان:
--   kind='signal': key = signal_events.id — صفّ تدريب لكل إشارة.
--   kind='watch' : key = token:network:first_seen — لكل دخول مراقبة (بما فيها
--                  المجموعة الضابطة) — لمقارنة الإشارة/الضابطة على نوافذ مكتملة.
-- العملة الواحدة تظهر في الصنفين عمداً؛ لكلٍّ غرضه.
CREATE TABLE IF NOT EXISTS outcomes (
    kind             TEXT NOT NULL,             -- 'signal' | 'watch'
    key              TEXT NOT NULL,
    token_address    TEXT NOT NULL,
    network_id       TEXT,
    signal_type      TEXT,                      -- نوع الإشارة أو 'control*'
    is_control       INTEGER NOT NULL DEFAULT 0,
    -- 1 = أوّل إشارة للعملة أو بعد فجوة ≥30 دقيقة عن سابقتها. 69% من الإشارات
    -- متباعدة <5 دقائق (تكرار زائف) — درّب على المستقلّة، لا تحذف البقيّة.
    is_independent   INTEGER,                   -- NULL لصفوف watch
    entry_ts         INTEGER NOT NULL,          -- epoch لحظة الإشارة/الدخول
    entry_px         REAL,                      -- إغلاق أوّل شمعة عند/بعد الدخول
    entry_lag_s      INTEGER,                   -- تأخّر شمعة الدخول عن اللحظة
    max_gain_1h      REAL,                      -- أقصى ارتفاع في النافذة (نسبة)
    max_gain_4h      REAL,
    max_gain_24h     REAL,
    max_gain_48h     REAL,
    max_drawdown_48h REAL,                      -- أدنى قاع (نسبة، سالبة)
    final_return_48h REAL,                      -- إغلاق نهاية النافذة / الدخول - 1
    time_to_peak_h   REAL,                      -- ساعات حتى القمّة
    candles_48h      INTEGER,                   -- شموع مرصودة داخل النافذة
    suspect_bars     INTEGER,                   -- شموع بذيل مستحيل داخل النافذة
                                                --   (استُبعدت ذيولها من القمّة/القاع)
    last_bar_lag_h   REAL,                      -- كم قبل نهاية النافذة انتهت السلسلة
    bars_truncated   INTEGER,                   -- 1 = السلسلة انتهت مبكراً (>1س)
                                                --   غالباً موت العملة — إشارة لا نقص!
    is_rug           INTEGER,                   -- 1 = العائد النهائي ≤ -90%
    split            TEXT,                      -- train/val/test (تجزئة ثابتة بالعملة)
    status           TEXT NOT NULL,             -- ok | no_entry | no_bars
    labeled_at       TEXT NOT NULL,
    design_version   INTEGER NOT NULL DEFAULT 1,
    analysis_eligible INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_token ON outcomes (token_address);
CREATE INDEX IF NOT EXISTS idx_outcomes_split ON outcomes (split, kind, status);

-- واجهة القراءة الآمنة للمرحلة 1: لا تُظهر الأرشيف القديم أو الصفوف غير المؤهلة.
DROP VIEW IF EXISTS phase1_watch_outcomes;
CREATE VIEW phase1_watch_outcomes AS
SELECT o.*
  FROM outcomes o
 WHERE o.kind = 'watch'
   AND o.analysis_eligible = 1
   AND o.design_version >= 3;

-- جدول التدريب: صفّ لكل قرار، عمود لكل ميزة معلومة **عند t=0 أو قبلها**،
-- والليبل ملصوقاً من outcomes. يُبنى بـ features.py عبر build_training_rows.py.
--
-- مشتقّ بالكامل ⇒ يُحذف ويُعاد بناؤه في أيّ وقت بلا نداء شبكة. الأعمدة نصّية
-- أو رقمية بحسب طبيعتها، والغائب NULL (FR-007: لا فبركة — الغياب معلومة).
--
-- ⚠️ لا تكتب فيه يدوياً ولا تُضِف عموداً محسوباً من بعد t=0: كل عمود هنا يجب
-- أن يجيب «هل كان يُعرف لحظة القرار؟» بنعم. الاختبارات تزرع بيانات بعد t0
-- وتتأكّد أنّها لا تظهر.
CREATE TABLE IF NOT EXISTS training_rows (
    -- وسم الصفّ
    kind             TEXT NOT NULL,      -- signal | activity | watch
    key              TEXT NOT NULL,      -- مفتاح القرار (= outcomes.key)
    token_address    TEXT NOT NULL,
    network_id       TEXT,
    entry_ts         INTEGER NOT NULL,   -- t=0 (epoch)
    asset_class      TEXT,               -- meme | major | priced | stable
    split            TEXT,               -- train/val/test (بتجزئة العملة)
    is_independent   INTEGER,
    is_live          INTEGER NOT NULL DEFAULT 0,  -- 1 = حيّ (≥LIVE_START_TS)، 0 = رجعيّ
    status           TEXT,
    suspect_bars     INTEGER,
    feature_version  INTEGER NOT NULL DEFAULT 1,
    built_at         TEXT NOT NULL,
    -- أ) الحدث المُشغِّل
    signal_type      TEXT,
    size_usd         REAL,
    in_amount        REAL,
    out_amount       REAL,
    token_amount     REAL,
    avg_cost         REAL,
    price_to_avg_cost REAL,
    realized_pnl_usd REAL,
    num_swaps        INTEGER,
    is_first_buy     INTEGER,
    buyer_pnl_pct    REAL,
    market_cap       REAL,
    fdv              REAL,
    price_usd        REAL,
    log_market_cap   REAL,
    log_size_usd     REAL,
    size_to_mcap     REAL,
    unique_traders   INTEGER,
    num_trades       INTEGER,
    minutes          INTEGER,
    price_change_pct REAL,
    total_volume     REAL,
    volume_per_trader REAL,
    are_top_traders  INTEGER,
    top_trader_match_count INTEGER,
    top_traders_listed INTEGER,
    top_trader_match_ratio REAL,
    buyers_best_rank INTEGER,
    rank_le_10       INTEGER,
    rank_le_50       INTEGER,
    ticker_len       INTEGER,
    ticker_has_digit INTEGER,
    ticker_non_ascii INTEGER,
    hour_utc         INTEGER,
    dow              INTEGER,
    -- ب) ثوابت العملة وبصمة المُنشئ
    token_age_h      REAL,
    launchpad_name   TEXT,
    migrated         INTEGER,
    graduation_percent REAL,
    is_scam          INTEGER,
    mintable         INTEGER,
    freezable        INTEGER,
    socials_count    INTEGER,
    has_twitter      INTEGER,
    creator_prior_tokens INTEGER,
    name_len         INTEGER,
    name_non_ascii   INTEGER,
    decimals         INTEGER,
    -- ج) الزخم الاجتماعيّ: عدّ تاريخيّ (عيّنة) + لقطة حقيقية قبل t0
    -- (بلا إعجابات الأطروحات — ممنوعة: قيمتها وقت السحب لا الكتابة)
    thesis_counted   INTEGER,
    thesis_counted_capped INTEGER,
    thesis_authors_before INTEGER,
    thesis_1h        INTEGER,
    thesis_24h       INTEGER,
    thesis_accel     REAL,
    hours_since_last_thesis REAL,
    thesis_history_days REAL,
    social_thesis_total INTEGER,
    social_thesis_authors INTEGER,
    social_holder_authors INTEGER,
    social_holder_ratio REAL,
    social_replies   INTEGER,
    social_snapshot_age_min REAL,
    social_total_delta_1h INTEGER,
    social_total_growth_1h REAL,
    social_authors_delta_1h INTEGER,
    -- د) مسار السعر والحجم قبل الإشارة
    ret_1h_before    REAL,
    ret_4h_before    REAL,
    ret_24h_before   REAL,
    ret_7d_before    REAL,
    vol_24h_before   REAL,
    flat_ratio_24h   REAL,
    up_candle_ratio_24h REAL,
    dist_from_ath    REAL,
    ath_history_complete INTEGER,
    ath_history_days REAL,
    bars_history_h   REAL,
    bars_count_24h   INTEGER,
    bar_vol_1h       REAL,
    bar_vol_24h      REAL,
    vol_surge_1h     REAL,
    -- هـ) لقطة السوق الأخيرة قبل t0
    liquidity        REAL,
    holders          INTEGER,
    top10_holders_pct REAL,
    volume_24h       REAL,
    buy_count_24h    INTEGER,
    sell_count_24h   INTEGER,
    buy_sell_ratio_24h REAL,
    unique_buys_24h  INTEGER,
    unique_sells_24h INTEGER,
    tick_age_min     REAL,
    tick_change_1h   REAL,
    tick_change_4h   REAL,
    tick_change_24h  REAL,
    tick_volume_1h   REAL,
    tick_volume_4h   REAL,
    tick_txn_1h      INTEGER,
    tick_txn_24h     INTEGER,
    volume_to_liquidity REAL,
    liquidity_to_mcap REAL,
    float_ratio      REAL,
    -- و) النظام السوقيّ
    sol_ret_4h       REAL,
    sol_ret_24h      REAL,
    eth_ret_24h      REAL,
    -- ز) كثافة الإشارات
    prior_signals_token INTEGER,
    minutes_since_prior_signal REAL,
    global_signals_1h INTEGER,
    -- الليبل (من outcomes — ما بعد t0، هدفٌ لا ميزة)
    final_return_48h REAL,
    max_gain_1h      REAL,
    max_gain_4h      REAL,
    max_gain_24h     REAL,
    max_gain_48h     REAL,
    max_drawdown_48h REAL,
    time_to_peak_h   REAL,
    is_rug           INTEGER,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_training_split
    ON training_rows (asset_class, split, status, is_independent);

-- واجهة النموذج الآمنة: لا تعتمد على تذكّر ستة فلاتر عند كل تدريب.
DROP VIEW IF EXISTS model_training_rows;
CREATE VIEW model_training_rows AS
SELECT * FROM training_rows
 WHERE kind = 'signal'
   AND is_live = 1
   AND asset_class = 'meme'
   AND status = 'ok'
   AND is_independent = 1
   AND feature_version >= 2
   AND NOT EXISTS (
       SELECT 1
         FROM signal_events current_event
         JOIN signal_events earlier_event
           ON earlier_event.token_address = current_event.token_address
          AND COALESCE(earlier_event.network_id, '') =
              COALESCE(current_event.network_id, '')
          AND earlier_event.ts = current_event.ts
          AND earlier_event.signal_type = current_event.signal_type
          AND earlier_event.id < current_event.id
        WHERE current_event.id = training_rows.key
   );

-- حالة التشغيل: آخر تشغيل، عدّادات، حالة getBars، إصدار المخطّط.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
