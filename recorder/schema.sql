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
    -- صدارات المدد: المصدر يسقّف الصدارة الأساسيّة عند 50 ويُهمل كل صيغ الترقيم
    -- بصمت، لكنّ /24h و/7d و/30d تعيد كلٌّ 100 — الاتّحاد 214 متداولاً مميّزاً
    -- ومقيس على 7,200 حدث شراء: المطابقة 3.68% ← 15.26% (×4.15). تبقى منفصلة لا
    -- مدموجة: رتبة 7 في 24h ليست رتبة 7 في totalPnL، والدمج يخلط قياسين.
    -- NULL في كل الصفوف السابقة لإضافة الأعمدة — غائب ≠ صفر (FR-007).
    top_trader_match_count_24h INTEGER,
    buyers_best_rank_24h   INTEGER,
    top_trader_match_count_7d  INTEGER,
    buyers_best_rank_7d    INTEGER,
    top_trader_match_count_30d INTEGER,
    buyers_best_rank_30d   INTEGER,
    top_trader_periods_matched INTEGER,          -- في كم صدارة (0-4) ظهر مشترٍ
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
    -- ماذا استلم. مقيس على 51,066 حدثاً: الطرف المقابل USDC في 100% والاتجاه
    -- يحدّده signal_type وحده — بلا تباين فلا فيتشر عليه. نلتقطه ليكشف بدء
    -- المصدر بتوجيه أزواج عملة↔عملة (غير USDC) فينقلب مفيداً.
    out_token_address      TEXT,
    token_amount           REAL,                 -- humanTokenAmount: إجمالي ما يملكه
    realized_pnl_usd       REAL,                 -- realizedPnlUsd وقت الحدث
    -- التفاعل الاجتماعي على الحدث نفسه — متاح في 100% من أحداث الـ feed وكان
    -- مُهدَراً بالكامل. `likes`/`views` يقيسان انتشار الإشارة لا قوّتها المالية:
    -- شراء بـ3,000$ شوهده 40,000 مستخدم يفوق أثره شراءً بـ30,000$ لم يره أحد.
    -- `pinned` قرار تحريريّ من fomo (تثبيت الحدث) = تضخيم مقصود للانتشار.
    likes                  INTEGER,              -- likes (المستوى الأعلى للحدث)
    views                  INTEGER,              -- views (المستوى الأعلى للحدث)
    num_replies            INTEGER,              -- numReplies إن وُجد
    pinned                 INTEGER,              -- مثبَّت من fomo (bool)
    -- وسم fomo للحدث (body.tag). مقيس على 4,000 حدث: قيمة **وحيدة** هي
    -- 'Top Trader' في 3.4% منها — فالمعلومة وجود الوسم لا نصّه، ونخزّنه علماً
    -- منطقياً لا نصّاً. (لو ظهرت قيم أخرى لاحقاً فالخام يحفظها.)
    is_top_trader_tagged   INTEGER,
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
    -- إشارات شرعية خارجية: كانت في الخام ولا تُستخرج. عملة مدرَجة في
    -- CoinMarketCap أو متداولة على عدّة منصّات ليست عملة أُطلقت قبل ساعة.
    -- نخزّن الأسماء لا العدد وحده: «Uniswap» ليست «PumpSwap».
    exchanges_count    INTEGER,                  -- كم منصّة تتداولها
    exchanges_json     TEXT,                     -- أسماء المنصّات (مصفوفة JSON)
    cmc_id             TEXT,                     -- معرّف CoinMarketCap (إدراج خارجي)
    -- الوصف والصور: **وجودها** إشارة جدّية لا محتواها. مقيس بعد التعبئة الرجعية
    -- على 517 عملة (لا على عيّنة الـ45 الأولى): وصف في 217 (42%) وبانر في 181
    -- (35%) — تباين صالح للتعلّم. أمّا `has_image` فثابت: 1 في 517 من 517، فلا
    -- تميّز فيه ⇒ يُؤرشف ولا يصير ميزة (كما `out_token_address`).
    description        TEXT,
    description_len    INTEGER,                  -- طول الوصف (0 = لا وصف)
    has_banner         INTEGER,                  -- imageBannerUrl موجود (bool)
    has_image          INTEGER,                  -- أي صورة للعملة موجودة (bool)
    -- بروتوكول الحوض (PumpAmm/Uniswap…). مقيس أنّه **غائب تماماً من خام
    -- trending** (صفر من 3,000 عنصر) ويأتي من filterTokens وحده — وفي المستوى
    -- الأعلى للعنصر لا تحت token. حوض المُطلِق ليس حوضاً مهاجَراً.
    dex_protocol       TEXT,
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
-- بحديثه. ونميّز **هل يروّج لما يملك؟** من مركز الكاتب نفسه في المغلّف.
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
    thesis_replies   INTEGER,                    -- مجموع الردود (ميت من المنبع: 0 دائماً)
    thesis_authors   INTEGER,                    -- كتّاب مميّزون (لا تكرار)
    -- منهم من يملك كمية موجبة الآن (authorTrade.humanTokenAmount > 0).
    -- **ليس** `equity`: ذاك الحقل موجود في المغلّف وقيمته صفر في 28,186/28,186
    -- أطروحة مقيسة، فكان هذا العمود ثابتاً على 0 حتى 2026-08-09.
    holder_authors   INTEGER,
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
    -- عائلة صدارات المدد (v7): رتبة كل مدّة قياس مستقلّ، وعرض الحضور
    -- (periods_matched) يميّز متصدّر الأربع كلّها من متصدّر 24h وحدها.
    -- كلّها NULL في الصفوف المبنيّة قبل تشغيل الجمع (FR-007: «لم نقس» لا «صفر»).
    top_trader_match_count_24h INTEGER,
    buyers_best_rank_24h INTEGER,
    top_trader_match_count_7d INTEGER,
    buyers_best_rank_7d INTEGER,
    top_trader_match_count_30d INTEGER,
    buyers_best_rank_30d INTEGER,
    top_trader_periods_matched INTEGER,
    top_trader_any_period INTEGER,
    best_rank_any_period INTEGER,
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
    -- شرعية خارجية: إدراج مركزيّ ومعرّف CMC لا يمنحهما المطلق لنفسه بضغطة زرّ
    exchanges_count  INTEGER,
    listed_on_exchange INTEGER,
    has_cmc_id       INTEGER,
    description_len  INTEGER,
    has_banner       INTEGER,
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
    -- طزاجة العدّادات وحدها: بعد دمج المصادر قد تأتي من صفّ أقدم من الأحدث،
    -- وبلا هذا يظنّ النموذج أنّ عدّاداً عمره ساعتان طازج كسعرٍ عمره دقيقة.
    tick_rich_age_min REAL,
    -- هـ٢) الملكية: تركيز السلسلة (يُصلح top10_holders_pct الميّت) وتموضع الحشد
    chain_top10_pct  REAL,                 -- أكبر 10 % من المعروض (token_details)
    chain_holder_count INTEGER,            -- حائزو السلسلة الكلّي
    holders_age_min  REAL,                 -- طزاجة أحدث قياس حيازة
    platform_holders INTEGER,              -- حائزو fomo (hodlers/top)
    platform_penetration REAL,             -- حائزو المنصّة ÷ حائزي السلسلة
    platform_underwater_ratio REAL,        -- نصيب المراكز الخاسرة (عرض زائد)
    platform_value_usd REAL,               -- مجموع قيمة مراكز المنصّة
    platform_median_hold_h REAL,           -- وسيط مدّة الحمل (ساعات)
    platform_dev_holding INTEGER,          -- 1 = المطوّر بين الحائزين
    -- هـ٣) التدفّق (token_flow): من يشتري ومن يبيع — كل حجم آخر عندنا **مجموع**،
    -- وطبقة 5 دقائق لم نملك مثلها قطّ (أقصر ما عندنا ساعة، عمياء عن الانعطاف).
    flow_age_min     REAL,
    flow_buy_volume_5m  REAL,
    flow_sell_volume_5m REAL,
    flow_net_volume_5m  REAL,              -- شراء − بيع (الغائب لا يصير صفراً)
    flow_net_volume_1h  REAL,
    flow_net_volume_24h REAL,
    flow_buy_sell_volume_ratio_5m  REAL,   -- النسبة هي المعلومة لا القيمة المطلقة
    flow_buy_sell_volume_ratio_1h  REAL,
    flow_buy_sell_volume_ratio_24h REAL,
    flow_buy_count_5m   INTEGER,
    flow_sell_count_5m  INTEGER,
    flow_unique_buys_5m  INTEGER,
    flow_unique_sells_5m INTEGER,
    flow_buy_sell_count_ratio_5m REAL,
    flow_unique_ratio_5m REAL,             -- فريدون ÷ صفقات: منخفض = غسل/بوت
    flow_trade_size_5m   REAL,             -- حيتان قليلة أم حشد صغير؟
    flow_is_low_fees     INTEGER,
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
   )
   -- إشارات متزامنة لنفس العملة واللحظة تُعدّ مكررة: نحتفظ بأصغر key فقط.
   AND training_rows.key = (
       SELECT MIN(t2.key)
         FROM training_rows t2
        WHERE t2.kind = 'signal'
          AND t2.is_live = 1
          AND t2.asset_class = 'meme'
          AND t2.status = 'ok'
          AND t2.is_independent = 1
          AND t2.feature_version >= 2
          AND t2.token_address = training_rows.token_address
          AND COALESCE(t2.network_id, '') =
              COALESCE(training_rows.network_id, '')
          AND t2.entry_ts = training_rows.entry_ts
   );

-- تركّز الحيازة وتموضع الحشد — مقياس خطر rug وكانا مفقودين كلياً.
-- `market_ticks.top10_holders_pct` ميّت: المصدر لا يوفّر المفتاح إطلاقاً
-- (0 من 1,430,475 صف). فحص السلسلة يغطّي Solana وحدها وصامت عن EVM كله.
-- المصدران هنا يعملان على الشبكتين لكنّهما يقيسان شيئين مختلفين (مؤكَّد حيّاً):
-- tokenDetails يعطي تركّز السلسلة (top10HoldersPercent+holders)، و/hodlers/top
-- يعطي مراكز مستخدمي fomo بلا نسبة من المعروض — تموضع الحشد لا التركّز.
-- كلٌّ في صفّ مستقلّ (source داخل المفتاح) فلا يُحسب مقياس مكان الآخر.
CREATE TABLE IF NOT EXISTS token_holders (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL,               -- token_details / hodlers_top
    -- تركّز السلسلة — من token_details وحده؛ NULL من المصدر الآخر
    top10_pct           REAL,                        -- أكبر 10 % من المعروض
    holder_count        INTEGER,                     -- إجمالي الحائزين على السلسلة
    -- تموضع حشد المنصّة — من hodlers_top وحده؛ NULL من الآخر
    platform_holders             INTEGER,            -- totalHolders على المنصّة
    platform_holders_listed      INTEGER,            -- المُفصَّل في الردّ (~50)
    platform_value_usd           REAL,               -- مجموع قيمة المراكز
    platform_underwater          INTEGER,            -- عدد المراكز الخاسرة
    platform_median_hold_seconds REAL,               -- وسيط مدّة الحمل
    platform_dev_holding         INTEGER,            -- 1 = المطوّر بين الحائزين
    top_holders_json    TEXT,                        -- مراكز مُلخَّصة (بلا كتلة user)
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at, source)
);
CREATE INDEX IF NOT EXISTS idx_holders_token_ts
    ON token_holders (token_address, network_id, recorded_at);

-- حالة الجدولة الدوّارة لتركّز الحيازة. الخطأ يُعاد سريعاً (مجهول ≠ آمن).
CREATE TABLE IF NOT EXISTS holders_fetch_state (
    token_address TEXT NOT NULL,
    network_id    TEXT NOT NULL,
    last_fetch_at TEXT,
    last_status   TEXT,                              -- ok / empty / error
    top10_pct     REAL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_address, network_id)
);

-- تدفّق التداول: انقسام الشراء/البيع من ردّ tokenDetails **نفسه** الذي تجلبه
-- دورة الحيازة — صفر نداء إضافي. كان الردّ يُستخرج منه حقلان (top10/holders)
-- ويُرمى الباقي، ومقيس على 300 ردّ مؤرشف أنّه يحمل بحضور 100%:
--   * انقسام الحجم شراءً وبيعاً (المصادر الأخرى تعطي الحجم الكلّي وحده — فحجم
--     مليون دولار بيعاً كان يبدو كمليون شراءً أمام النموذج)، و
--   * **طبقة 5 دقائق كاملة** لا يعطيها أيّ مصدر آخر (أقرب نافذة إلى لحظة القرار).
-- جدول مستقلّ لا أعمدة على token_holders: ذاك يقيس تركّز الملكية ومستخرِجه يعيد
-- None حين تغيب بيانات الحيازة، فكان سيبتلع التدفّق معها. ولا صفوف على
-- market_ticks: لا سعر هنا فتُفاقم صفوفاً فقيرة.
-- لا طبقة 12h في هذا المصدر إطلاقاً ⇒ لا عمود لها (غياب دائم ≠ قياس).
CREATE TABLE IF NOT EXISTS token_flow (
    token_address       TEXT NOT NULL,
    network_id          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    watch_first_seen_at TEXT NOT NULL,
    entry_signal_id     TEXT,
    is_control          INTEGER NOT NULL DEFAULT 0,
    buy_count_5m        INTEGER,
    buy_count_1h        INTEGER,
    buy_count_4h        INTEGER,
    buy_count_24h       INTEGER,
    sell_count_5m       INTEGER,
    sell_count_1h       INTEGER,
    sell_count_4h       INTEGER,
    sell_count_24h      INTEGER,
    -- تصل **نصوصاً** من المصدر ('90135') — يحوّلها _num
    buy_volume_5m       REAL,
    buy_volume_1h       REAL,
    buy_volume_4h       REAL,
    buy_volume_24h      REAL,
    sell_volume_5m      REAL,
    sell_volume_1h      REAL,
    sell_volume_4h      REAL,
    sell_volume_24h     REAL,
    unique_buys_5m      INTEGER,
    unique_buys_1h      INTEGER,
    unique_buys_4h      INTEGER,
    unique_buys_24h     INTEGER,
    unique_sells_5m     INTEGER,
    unique_sells_1h     INTEGER,
    unique_sells_4h     INTEGER,
    unique_sells_24h    INTEGER,
    -- يتباين فعلاً: 11 True من 800 ردّ — رسوم منخفضة تعني حوضاً مختلفاً
    is_low_fees         INTEGER,
    raw_json            BLOB NOT NULL,
    PRIMARY KEY (token_address, network_id, recorded_at)
);
CREATE INDEX IF NOT EXISTS idx_flow_token_ts
    ON token_flow (token_address, network_id, recorded_at);

-- ملفّ المتداول. مقيس: 5,572 معرّف مشترٍ مميّز في signal_events و**3,202 منهم
-- متكرّرون (≥3 أحداث)** — ولا جدول تجّار في القاعدة إطلاقاً، فكان «من اشترى؟»
-- سؤالاً بلا جواب رغم أنّ المعرّف مخزَّن في كل حدث. المتكرّرون وحدهم يُجلبون:
-- من يظهر مرّة واحدة لا سلوك له نتعلّمه.
--
-- الأعمدة تطابق ردّ /v2/users/{id} **المقيس حيّاً 2026-08-10 على متداولَين**
-- (26 مفتاحاً). ما ليس في الردّ فلا عمود له: **لا win_rate ولا realized_pnl**
-- — الملفّ لا يحمل ربحاً إطلاقاً (لذلك يوجد endpoint منفصل
-- aggregatedSnapshot). وضعُ عمودٍ لحقل غير موجود يصنع عموداً ميتاً كـ
-- top10_holders_pct الذي كلّفنا 1.43 مليون صفّ فارغ.
CREATE TABLE IF NOT EXISTS traders (
    trader_id        TEXT PRIMARY KEY,
    recorded_at      TEXT NOT NULL,             -- وقت آخر تحديث للملفّ
    handle           TEXT,                      -- userHandle
    display_name     TEXT,
    followers_count  INTEGER,                   -- followers: 2,143 و214,422 مقيسان
    following_count  INTEGER,                   -- following
    swap_count       INTEGER,                   -- swapCount: كل مبادلاته
    num_trades       INTEGER,                   -- numTrades: أقلّ من swapCount دائماً
    total_volume_usd REAL,                      -- totalVolume: 12.99M و5.45M مقيسان
    avg_hold_seconds REAL,                      -- averageHoldTimeSeconds: 38k و170k
    is_restricted    INTEGER,                   -- isRestricted (bool)
    is_private       INTEGER,                   -- private (bool)
    wallet_address   TEXT,                      -- address (Solana)
    evm_address      TEXT,                      -- evmAddress
    twitter_url      TEXT,                      -- حضوره من عدمه إشارة سمعة
    created_at       TEXT,                      -- عمر الحساب
    raw_json         BLOB NOT NULL
);

-- حالة الجدولة الدوّارة للتجّار. **مفتاحها معرّف المتداول وحده** لا
-- (عملة، شبكة) كبقيّة جداول الحالة — التاجر عابر للعملات.
CREATE TABLE IF NOT EXISTS traders_fetch_state (
    trader_id     TEXT PRIMARY KEY,
    last_fetch_at TEXT,
    last_status   TEXT,                         -- ok / empty / error
    attempts      INTEGER NOT NULL DEFAULT 0
);

-- حالة التشغيل: آخر تشغيل، عدّادات، حالة getBars، إصدار المخطّط.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
