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
    token_address TEXT    NOT NULL,
    network_id    TEXT    NOT NULL,
    resolution    TEXT    NOT NULL,        -- "5" = خمس دقائق (الافتراضي)
    ts            INTEGER NOT NULL,        -- epoch seconds، فتح الشمعة
    o             REAL,
    h             REAL,
    l             REAL,
    c             REAL,
    v             REAL,
    fetched_at    TEXT    NOT NULL,        -- متى سحبناها (ISO UTC)
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
    last_bar_lag_h   REAL,                      -- كم قبل نهاية النافذة انتهت السلسلة
    bars_truncated   INTEGER,                   -- 1 = السلسلة انتهت مبكراً (>1س)
                                                --   غالباً موت العملة — إشارة لا نقص!
    is_rug           INTEGER,                   -- 1 = العائد النهائي ≤ -90%
    split            TEXT,                      -- train/val/test (تجزئة ثابتة بالعملة)
    status           TEXT NOT NULL,             -- ok | no_entry | no_bars
    labeled_at       TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_token ON outcomes (token_address);
CREATE INDEX IF NOT EXISTS idx_outcomes_split ON outcomes (split, kind, status);

-- حالة التشغيل: آخر تشغيل، عدّادات، حالة getBars، إصدار المخطّط.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
