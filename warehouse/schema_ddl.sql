-- =====================================================================
-- 电商数仓 DDL — DWD/DWS/ADS 三层
-- 数据特征: mock 合成,is_mock=true 标识
-- 业务范围: 订单 / 用户 / 商品 / 营销 / 流量
-- 时间范围: 2025-01-01 ~ 2026-07-31 (1.5 年, 含去年 30 天可做同比)
-- =====================================================================

-- ===== DWD (明细层) =====

-- 订单明细事实表
CREATE TABLE IF NOT EXISTS dwd_trade_order (
    order_id        VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL,
    sku_id          VARCHAR NOT NULL,
    category_l1     VARCHAR NOT NULL,   -- 大类(服饰/3C/美妆/食品/家电)
    category_l2     VARCHAR NOT NULL,   -- 中类
    category_l3     VARCHAR NOT NULL,   -- 小类
    channel         VARCHAR NOT NULL,   -- 渠道(APP/H5/小程序/PC)
    order_time      TIMESTAMP NOT NULL, -- 下单时间
    pay_time        TIMESTAMP,         -- 支付时间
    finish_time     TIMESTAMP,         -- 完成时间
    order_status    VARCHAR NOT NULL,   -- created/paid/finished/refunded/cancelled
    pay_amount      DECIMAL(12, 2),     -- 实付金额(已扣券/优惠)
    original_amount DECIMAL(12, 2),     -- 原价金额
    coupon_id       VARCHAR,            -- 使用的券
    coupon_amount   DECIMAL(12, 2) DEFAULT 0,  -- 券面额
    is_new_user     BOOLEAN NOT NULL,   -- 是否新客(首单)
    is_mock         BOOLEAN DEFAULT TRUE
);

-- 用户注册明细
CREATE TABLE IF NOT EXISTS dwd_user_register (
    user_id         VARCHAR PRIMARY KEY,
    register_time   TIMESTAMP NOT NULL,
    register_channel VARCHAR NOT NULL, -- 注册来源渠道
    age_group       VARCHAR,            -- 年龄段(18-24/25-30/31-40/41-50/50+)
    gender          VARCHAR,            -- M/F/U
    city_tier       VARCHAR,            -- 一线/新一线/二线/三线/四线及以下
    is_mock         BOOLEAN DEFAULT TRUE
);

-- 用户登录行为
CREATE TABLE IF NOT EXISTS dwd_user_login (
    login_id        VARCHAR PRIMARY KEY,
    user_id         VARCHAR NOT NULL,
    login_time      TIMESTAMP NOT NULL,
    device          VARCHAR,            -- ios/android/pc
    is_mock         BOOLEAN DEFAULT TRUE
);

-- 商品 SKU 维表
CREATE TABLE IF NOT EXISTS dwd_product_sku (
    sku_id          VARCHAR PRIMARY KEY,
    product_name    VARCHAR NOT NULL,
    category_l1     VARCHAR NOT NULL,
    category_l2     VARCHAR NOT NULL,
    category_l3     VARCHAR NOT NULL,
    brand           VARCHAR,
    price           DECIMAL(10, 2),
    cost_price      DECIMAL(10, 2),
    on_shelf_time   TIMESTAMP,
    is_mock         BOOLEAN DEFAULT TRUE
);

-- 优惠券使用明细
CREATE TABLE IF NOT EXISTS dwd_coupon_use (
    coupon_id       VARCHAR NOT NULL,
    user_id         VARCHAR NOT NULL,
    order_id        VARCHAR NOT NULL,
    coupon_type     VARCHAR NOT NULL,   -- 满减/折扣/现金
    coupon_amount   DECIMAL(10, 2) NOT NULL,
    threshold_amount DECIMAL(10, 2),    -- 满减门槛
    use_time        TIMESTAMP NOT NULL,
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (coupon_id, order_id)
);

-- 流量访问明细
CREATE TABLE IF NOT EXISTS dwd_traffic_visit (
    visit_id        VARCHAR PRIMARY KEY,
    user_id         VARCHAR,
    session_id      VARCHAR NOT NULL,
    visit_time      TIMESTAMP NOT NULL,
    page_type       VARCHAR NOT NULL,   -- home/category/product/cart/pay
    sku_id          VARCHAR,            -- 浏览的商品(若适用)
    channel         VARCHAR NOT NULL,
    device          VARCHAR,
    is_mock         BOOLEAN DEFAULT TRUE
);


-- ===== DWS (主题汇总层) =====

-- 交易×用户×日
CREATE TABLE IF NOT EXISTS dws_trade_user_day (
    stat_date       DATE NOT NULL,
    user_type       VARCHAR NOT NULL,   -- new/old
    category_l1     VARCHAR NOT NULL,
    channel         VARCHAR NOT NULL,
    gmv             DECIMAL(15, 2),
    order_cnt       INTEGER,
    user_cnt        INTEGER,
    refund_amt      DECIMAL(15, 2) DEFAULT 0,
    refund_cnt      INTEGER DEFAULT 0,
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, user_type, category_l1, channel)
);

-- 用户行为×日
CREATE TABLE IF NOT EXISTS dws_user_action_day (
    stat_date       DATE NOT NULL,
    user_id         VARCHAR NOT NULL,
    pv              INTEGER DEFAULT 0,
    cart_add        INTEGER DEFAULT 0,
    fav_add         INTEGER DEFAULT 0,
    pay_cnt         INTEGER DEFAULT 0,
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, user_id)
);

-- 商品销售×日
CREATE TABLE IF NOT EXISTS dws_product_sale_day (
    stat_date       DATE NOT NULL,
    sku_id          VARCHAR NOT NULL,
    category_l1     VARCHAR NOT NULL,
    sale_cnt        INTEGER,
    sale_amt        DECIMAL(12, 2),
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, sku_id)
);

-- 券效果×日
CREATE TABLE IF NOT EXISTS dws_coupon_effect_day (
    stat_date       DATE NOT NULL,
    coupon_type     VARCHAR NOT NULL,
    coupon_amt      DECIMAL(12, 2),     -- 券总面额
    gmv_driven      DECIMAL(15, 2),     -- 券带动 GMV
    use_cnt         INTEGER,            -- 核销次数
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, coupon_type)
);

-- 流量×渠道×日
CREATE TABLE IF NOT EXISTS dws_traffic_channel_day (
    stat_date       DATE NOT NULL,
    channel         VARCHAR NOT NULL,
    pv              INTEGER,
    uv              INTEGER,
    click_cnt       INTEGER,
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, channel)
);


-- ===== ADS (指标应用层) =====

-- 日 GMV 指标(主报表表)
CREATE TABLE IF NOT EXISTS ads_gmv_daily (
    stat_date       DATE NOT NULL,
    category_l1     VARCHAR NOT NULL,
    channel         VARCHAR NOT NULL,
    gmv             DECIMAL(15, 2),
    order_cnt       INTEGER,
    user_cnt        INTEGER,
    avg_order_value DECIMAL(12, 2),
    refund_rate     DECIMAL(5, 4),
    yoy_gmv         DECIMAL(15, 2),     -- 去年同期 GMV
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, category_l1, channel)
);

-- 用户留存(日粒度,按注册日 cohort)
CREATE TABLE IF NOT EXISTS ads_user_retention (
    register_date   DATE NOT NULL,
    cohort_day      INTEGER NOT NULL,   -- 注册后第 N 天
    user_cnt        INTEGER,            -- 注册当日用户数
    retained_cnt    INTEGER,            -- 第 N 天仍活跃的用户数
    retention_rate  DECIMAL(5, 4),
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (register_date, cohort_day)
);

-- 类目 GMV 排名
CREATE TABLE IF NOT EXISTS ads_category_rank (
    stat_date       DATE NOT NULL,
    category_l1     VARCHAR NOT NULL,
    category_l2     VARCHAR,
    gmv             DECIMAL(15, 2),
    gmv_rank        INTEGER,
    yoy_gmv         DECIMAL(15, 2),
    yoy_pct         DECIMAL(10, 2),
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, category_l1, category_l2)
);

-- 券 ROI
CREATE TABLE IF NOT EXISTS ads_coupon_roi (
    stat_date       DATE NOT NULL,
    coupon_type     VARCHAR NOT NULL,
    coupon_amt      DECIMAL(12, 2),
    gmv_driven      DECIMAL(15, 2),
    roi             DECIMAL(10, 2),      -- gmv_driven / coupon_amt
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, coupon_type)
);

-- 转化漏斗(浏览→加购→下单→支付)
CREATE TABLE IF NOT EXISTS ads_conversion_funnel (
    stat_date       DATE NOT NULL,
    channel         VARCHAR NOT NULL,
    pv_cnt          INTEGER,            -- 浏览
    cart_cnt        INTEGER,            -- 加购
    order_cnt       INTEGER,            -- 下单
    pay_cnt         INTEGER,            -- 支付
    pv_to_cart      DECIMAL(5, 4),      -- pv→cart 转化率
    cart_to_order   DECIMAL(5, 4),
    order_to_pay    DECIMAL(5, 4),
    is_mock         BOOLEAN DEFAULT TRUE,
    PRIMARY KEY (stat_date, channel)
);
