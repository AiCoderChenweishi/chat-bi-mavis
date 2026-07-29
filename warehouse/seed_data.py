"""
Mock 数据生成脚本 — V2 优化版
- DWD 全部用 generate_series 笛卡尔积在 SQL 端生成,Python 端只跑配置
- DWS/ADS 全部走 SQL
- 全部带 is_mock=TRUE
- 目标: < 30 秒跑完
"""
import duckdb
import os
from datetime import datetime, timedelta

WAREHOUSE_PATH = os.path.join(os.path.dirname(__file__), "ecommerce.duckdb")
START_DATE = "2025-01-01"
END_DATE = "2026-07-31"
N_DAYS = 578  # 2025-01-01 ~ 2026-07-31


def build_warehouse():
    if os.path.exists(WAREHOUSE_PATH):
        os.remove(WAREHOUSE_PATH)
        print(f"[reset] {WAREHOUSE_PATH}")

    con = duckdb.connect(WAREHOUSE_PATH)
    print(f"[connect] {WAREHOUSE_PATH}")

    # ===== 1. 跑 DDL =====
    ddl_path = os.path.join(os.path.dirname(__file__), "schema_ddl.sql")
    with open(ddl_path) as f:
        ddl = f.read()
    con.execute(ddl)
    print(f"[DDL] 17 张表创建完成 (DWD 6 + DWS 5 + ADS 5)")

    # ===== 2. 用户注册 (5000 用户) =====
    print("[seed] dwd_user_register (SQL bulk)...")
    con.execute(f"""
        INSERT INTO dwd_user_register
        SELECT
            'U' || LPAD(CAST(rownum AS VARCHAR), 6, '0') AS user_id,
            -- 注册时间: 均匀分布
            TIMESTAMP '2025-01-01' + INTERVAL (CAST(rownum AS BIGINT) * {N_DAYS * 86400 / 5000} || ' seconds') AS register_time,
            ['APP', 'H5', '微信小程序', 'PC'][(rownum * 7) % 4 + 1] AS register_channel,
            ['18-24', '25-30', '31-40', '41-50', '50+'][(rownum * 11) % 5 + 1] AS age_group,
            ['M', 'F', 'U'][(rownum * 13) % 3 + 1] AS gender,
            ['一线', '新一线', '二线', '三线', '四线及以下'][(rownum * 17) % 5 + 1] AS city_tier,
            TRUE
        FROM generate_series(1, 5000) t(rownum)
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_user_register").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 3. 商品 SKU (1200 个, 5 类目 × 4 L2 × 3 L3 × 20) =====
    print("[seed] dwd_product_sku (SQL bulk)...")
    con.execute("""
        INSERT INTO dwd_product_sku
        WITH cat_l1 AS (SELECT UNNEST(['服饰', '3C数码', '美妆', '食品', '家电']) AS cat_l1),
             cat_l2 AS (SELECT UNNEST(['女装', '男装', '童装', '内衣', '手机', '电脑', '配件', '智能设备',
                                       '护肤', '彩妆', '香水', '美妆工具', '零食', '生鲜', '饮料', '酒水',
                                       '大家电', '小家电', '厨电', '个护']) AS cat_l2),
             cat_l3 AS (SELECT UNNEST(['普通款', '升级款', 'Pro款']) AS cat_l3),
             cat AS (
                 SELECT cat_l1.cat_l1, cat_l2.cat_l2, cat_l3.cat_l3
                 FROM cat_l1, cat_l2, cat_l3
             ),
             sku_idx AS (
                 SELECT
                     cat.*,
                     ROW_NUMBER() OVER () AS rn
                 FROM cat
                 CROSS JOIN generate_series(1, 20) AS g
             )
        SELECT
            'S' || LPAD(CAST(rn AS VARCHAR), 5, '0') AS sku_id,
            '品牌' || CHR(CAST(65 + (rn % 6) AS INTEGER)) || '-' || cat_l2 || '-' || cat_l3 || '-' || rn AS product_name,
            cat_l1, cat_l2, cat_l3,
            '品牌' || CHR(CAST(65 + (rn % 6) AS INTEGER)) AS brand,
            CAST(50 + (rn * 17) % 4950 AS DECIMAL(10, 2)) AS price,
            CAST(20 + (rn * 7) % 2000 AS DECIMAL(10, 2)) AS cost_price,
            TIMESTAMP '2025-01-01' + INTERVAL ((rn * 86400) || ' seconds') AS on_shelf_time,
            TRUE
        FROM sku_idx
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_product_sku").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 4. 订单 (8 万) =====
    print("[seed] dwd_trade_order (SQL bulk)...")
    con.execute("""
        INSERT INTO dwd_trade_order
        WITH order_base AS (
            SELECT
                ROW_NUMBER() OVER (ORDER BY random()) AS rownum,
                'O' || LPAD(CAST(ROW_NUMBER() OVER (ORDER BY random()) AS VARCHAR), 7, '0') AS order_id,
                'U' || LPAD(CAST(1 + (random() * 4999)::INT AS VARCHAR), 6, '0') AS user_id,
                'S' || LPAD(CAST(1 + (random() * 1199)::INT AS VARCHAR), 5, '0') AS sku_id,
                -- 时间: 2025-01-01 + 随机天数(注册后 1 天)
                TIMESTAMP '2025-01-01' + INTERVAL (CAST((random() * 578 * 86400)::BIGINT AS VARCHAR) || ' seconds') AS order_time,
                ['APP', 'H5', '微信小程序', 'PC'][(random() * 4)::INT % 4 + 1] AS channel
            FROM generate_series(1, 80000) g(n)
        )
        SELECT
            o.order_id,
            o.user_id,
            o.sku_id,
            p.category_l1, p.category_l2, p.category_l3,
            o.channel,
            o.order_time AS order_time,
            -- 60% 概率已支付
            CASE WHEN random() < 0.6
                 THEN o.order_time + INTERVAL (CAST((random() * 3600)::INT AS VARCHAR) || ' seconds')
                 ELSE NULL END AS pay_time,
            CASE WHEN random() < 0.6
                 THEN o.order_time + INTERVAL (CAST((3600 + random() * 7 * 86400)::INT AS VARCHAR) || ' seconds')
                 ELSE NULL END AS finish_time,
            -- 状态
            CASE
                WHEN random() < 0.05 THEN 'cancelled'
                WHEN random() < 0.10 THEN 'refunded'
                WHEN random() < 0.40 THEN 'paid'
                ELSE 'finished'
            END AS order_status,
            -- 金额
            CASE WHEN random() < 0.6
                 THEN p.price - (CASE WHEN random() < 0.3
                                      THEN (10 + random() * 200)::INT
                                      ELSE 0 END)
                 ELSE NULL END AS pay_amount,
            p.price AS original_amount,
            CASE WHEN random() < 0.3
                 THEN 'C' || LPAD(CAST(1 + (random() * 19)::INT AS VARCHAR), 3, '0')
                 ELSE NULL END AS coupon_id,
            CASE WHEN random() < 0.3
                 THEN (10 + random() * 200)::INT::DECIMAL(12, 2)
                 ELSE 0 END AS coupon_amount,
            -- 新客: 后面 UPDATE 校正
            TRUE AS is_new_user,
            TRUE
        FROM order_base o
        JOIN dwd_product_sku p ON o.sku_id = p.sku_id
    """)
    # 校正 is_new_user(每个 user 的首单)
    con.execute("""
        UPDATE dwd_trade_order
        SET is_new_user = (order_id IN (
            SELECT order_id FROM (
                SELECT order_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_time) AS rn
                FROM dwd_trade_order
            ) WHERE rn = 1
        ))
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_trade_order").fetchone()[0]
    print(f"        ✓ {n} 行 (含 60% 支付/退单/取消)")

    # ===== 5. 券使用 (从订单抽 30%) =====
    print("[seed] dwd_coupon_use...")
    con.execute("""
        INSERT INTO dwd_coupon_use
        SELECT
            coupon_id,
            user_id,
            order_id,
            ['满减', '折扣', '现金'][(random() * 3)::INT % 3 + 1] AS coupon_type,
            coupon_amount,
            coupon_amount * (2 + random() * 3) AS threshold_amount,
            order_time + INTERVAL '5 minutes' AS use_time,
            TRUE
        FROM dwd_trade_order
        WHERE coupon_id IS NOT NULL
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_coupon_use").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 6. 流量访问 (20 万) =====
    print("[seed] dwd_traffic_visit...")
    con.execute("""
        INSERT INTO dwd_traffic_visit
        SELECT
            'V' || LPAD(CAST(rownum AS VARCHAR), 8, '0'),
            CASE WHEN random() < 0.7
                 THEN 'U' || LPAD(CAST(1 + (random() * 4999)::INT AS VARCHAR), 6, '0')
                 ELSE NULL END,
            'SES' || LPAD(CAST(1 + (random() * 49999)::INT AS VARCHAR), 5, '0'),
            TIMESTAMP '2025-01-01' + INTERVAL (CAST((random() * 578 * 86400)::INT AS VARCHAR) || ' seconds'),
            ['home', 'category', 'product', 'cart', 'pay'][(random() * 5)::INT % 5 + 1],
            CASE WHEN random() < 0.5
                 THEN 'S' || LPAD(CAST(1 + (random() * 1199)::INT AS VARCHAR), 5, '0')
                 ELSE NULL END,
            ['APP', 'H5', '微信小程序', 'PC'][(random() * 4)::INT % 4 + 1],
            ['iOS', 'Android', 'PC'][(random() * 3)::INT % 3 + 1],
            TRUE
        FROM generate_series(1, 200000) g(rownum)
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_traffic_visit").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 7. 用户登录 (3 万) =====
    print("[seed] dwd_user_login...")
    con.execute("""
        INSERT INTO dwd_user_login
        SELECT
            'L' || LPAD(CAST(rownum AS VARCHAR), 6, '0'),
            'U' || LPAD(CAST(1 + (random() * 4999)::INT AS VARCHAR), 6, '0'),
            TIMESTAMP '2025-01-01' + INTERVAL (CAST((random() * 578 * 86400)::INT AS VARCHAR) || ' seconds'),
            ['iOS', 'Android', 'PC'][(random() * 3)::INT % 3 + 1],
            TRUE
        FROM generate_series(1, 30000) g(rownum)
    """)
    n = con.execute("SELECT COUNT(*) FROM dwd_user_login").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 8. DWS trade_user_day =====
    print("[agg] dws_trade_user_day...")
    con.execute("""
        INSERT INTO dws_trade_user_day
        SELECT
            DATE_TRUNC('day', pay_time)::DATE AS stat_date,
            CASE WHEN is_new_user THEN 'new' ELSE 'old' END AS user_type,
            category_l1,
            channel,
            SUM(pay_amount) AS gmv,
            COUNT(DISTINCT order_id) AS order_cnt,
            COUNT(DISTINCT user_id) AS user_cnt,
            SUM(CASE WHEN order_status = 'refunded' THEN pay_amount ELSE 0 END) AS refund_amt,
            SUM(CASE WHEN order_status = 'refunded' THEN 1 ELSE 0 END) AS refund_cnt,
            TRUE
        FROM dwd_trade_order
        WHERE pay_time IS NOT NULL
        GROUP BY 1, 2, 3, 4
    """)
    n = con.execute("SELECT COUNT(*) FROM dws_trade_user_day").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 9. ADS gmv_daily =====
    print("[agg] ads_gmv_daily...")
    con.execute("""
        INSERT INTO ads_gmv_daily
        WITH cur AS (
            SELECT
                DATE_TRUNC('day', pay_time)::DATE AS stat_date,
                category_l1,
                channel,
                SUM(pay_amount) AS gmv,
                COUNT(DISTINCT order_id) AS order_cnt,
                COUNT(DISTINCT user_id) AS user_cnt,
                SUM(CASE WHEN order_status = 'refunded' THEN 1 ELSE 0 END) * 1.0
                    / NULLIF(COUNT(*), 0) AS refund_rate
            FROM dwd_trade_order
            WHERE pay_time IS NOT NULL
            GROUP BY 1, 2, 3
        ),
        yoy AS (
            SELECT
                DATE_TRUNC('day', pay_time)::DATE AS stat_date,
                category_l1,
                channel,
                SUM(pay_amount) AS gmv
            FROM dwd_trade_order
            WHERE pay_time IS NOT NULL
              AND pay_time < DATE '2026-01-01'
            GROUP BY 1, 2, 3
        )
        SELECT
            c.stat_date, c.category_l1, c.channel,
            c.gmv, c.order_cnt, c.user_cnt,
            ROUND(c.gmv / NULLIF(c.order_cnt, 0), 2) AS avg_order_value,
            ROUND(c.refund_rate, 4),
            y.gmv AS yoy_gmv,
            TRUE
        FROM cur c
        LEFT JOIN yoy y
            ON c.stat_date = y.stat_date + INTERVAL 1 YEAR
            AND c.category_l1 = y.category_l1
            AND c.channel = y.channel
    """)
    n = con.execute("SELECT COUNT(*) FROM ads_gmv_daily").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 10. ADS category_rank =====
    print("[agg] ads_category_rank...")
    con.execute("""
        INSERT INTO ads_category_rank
        WITH daily AS (
            SELECT
                DATE_TRUNC('day', pay_time)::DATE AS stat_date,
                category_l1, category_l2,
                SUM(pay_amount) AS gmv
            FROM dwd_trade_order
            WHERE pay_time IS NOT NULL
            GROUP BY 1, 2, 3
        ),
        yoy AS (
            SELECT
                DATE_TRUNC('day', pay_time)::DATE AS stat_date,
                category_l1, category_l2,
                SUM(pay_amount) AS yoy_gmv
            FROM dwd_trade_order
            WHERE pay_time IS NOT NULL
              AND pay_time < DATE '2026-01-01'
            GROUP BY 1, 2, 3
        )
        SELECT
            d.stat_date, d.category_l1, d.category_l2,
            d.gmv,
            RANK() OVER (PARTITION BY d.stat_date ORDER BY d.gmv DESC) AS gmv_rank,
            y.yoy_gmv,
            ROUND((d.gmv - y.yoy_gmv) * 100.0 / NULLIF(y.yoy_gmv, 0), 2) AS yoy_pct,
            TRUE
        FROM daily d
        LEFT JOIN yoy y
            ON d.stat_date = y.stat_date + INTERVAL 1 YEAR
            AND d.category_l1 = y.category_l1
            AND d.category_l2 = y.category_l2
    """)
    n = con.execute("SELECT COUNT(*) FROM ads_category_rank").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 11. ADS coupon_roi =====
    print("[agg] ads_coupon_roi...")
    con.execute("""
        INSERT INTO ads_coupon_roi
        SELECT
            DATE_TRUNC('day', c.use_time)::DATE AS stat_date,
            c.coupon_type,
            SUM(c.coupon_amount) AS coupon_amt,
            SUM(o.pay_amount) AS gmv_driven,
            ROUND(SUM(o.pay_amount) / NULLIF(SUM(c.coupon_amount), 0), 2) AS roi,
            TRUE
        FROM dwd_coupon_use c
        JOIN dwd_trade_order o ON c.order_id = o.order_id
        GROUP BY 1, 2
    """)
    n = con.execute("SELECT COUNT(*) FROM ads_coupon_roi").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 12. ADS conversion_funnel =====
    print("[agg] ads_conversion_funnel...")
    con.execute("""
        INSERT INTO ads_conversion_funnel
        WITH pv AS (
            SELECT DATE_TRUNC('day', visit_time)::DATE AS stat_date, channel, COUNT(*) AS pv_cnt
            FROM dwd_traffic_visit
            GROUP BY 1, 2
        ),
        cart AS (
            SELECT DATE_TRUNC('day', order_time)::DATE AS stat_date, channel, COUNT(DISTINCT order_id) AS cart_cnt
            FROM dwd_trade_order
            WHERE order_status IN ('paid', 'finished')
            GROUP BY 1, 2
        ),
        order_ AS (
            SELECT DATE_TRUNC('day', order_time)::DATE AS stat_date, channel, COUNT(DISTINCT order_id) AS order_cnt
            FROM dwd_trade_order
            GROUP BY 1, 2
        ),
        pay AS (
            SELECT DATE_TRUNC('day', pay_time)::DATE AS stat_date, channel, COUNT(DISTINCT order_id) AS pay_cnt
            FROM dwd_trade_order
            WHERE pay_time IS NOT NULL
            GROUP BY 1, 2
        )
        SELECT
            COALESCE(p.stat_date, c.stat_date, o.stat_date, pay.stat_date),
            COALESCE(p.channel, c.channel, o.channel, pay.channel),
            p.pv_cnt, c.cart_cnt, o.order_cnt, pay.pay_cnt,
            ROUND(c.cart_cnt * 1.0 / NULLIF(p.pv_cnt, 0), 4),
            ROUND(o.order_cnt * 1.0 / NULLIF(c.cart_cnt, 0), 4),
            ROUND(pay.pay_cnt * 1.0 / NULLIF(o.order_cnt, 0), 4),
            TRUE
        FROM pv p
        FULL OUTER JOIN cart c USING (stat_date, channel)
        FULL OUTER JOIN order_ o USING (stat_date, channel)
        FULL OUTER JOIN pay USING (stat_date, channel)
    """)
    n = con.execute("SELECT COUNT(*) FROM ads_conversion_funnel").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 13. ADS user_retention =====
    print("[agg] ads_user_retention...")
    con.execute("""
        INSERT INTO ads_user_retention
        WITH reg AS (
            SELECT user_id, DATE_TRUNC('day', register_time)::DATE AS register_date
            FROM dwd_user_register
        ),
        logins AS (
            SELECT user_id, DATE_TRUNC('day', login_time)::DATE AS login_date
            FROM dwd_user_login
            GROUP BY 1, 2
        ),
        pairs AS (
            SELECT
                r.register_date,
                DATEDIFF('day', r.register_date, l.login_date) AS cohort_day,
                r.user_id AS reg_user,
                l.user_id AS login_user
            FROM reg r
            INNER JOIN logins l
                ON r.user_id = l.user_id
                AND l.login_date BETWEEN r.register_date AND r.register_date + INTERVAL 30 DAY
        )
        SELECT
            p.register_date,
            p.cohort_day,
            COUNT(DISTINCT p.reg_user) AS user_cnt,
            COUNT(DISTINCT p.login_user) AS retained_cnt,
            ROUND(COUNT(DISTINCT p.login_user) * 1.0 / NULLIF(COUNT(DISTINCT p.reg_user), 0), 4),
            TRUE
        FROM pairs p
        GROUP BY 1, 2
    """)
    n = con.execute("SELECT COUNT(*) FROM ads_user_retention").fetchone()[0]
    print(f"        ✓ {n} 行")

    # ===== 统计 =====
    print("\n" + "=" * 60)
    print("📊 库表统计")
    print("=" * 60)
    for layer in ["DWD", "DWS", "ADS"]:
        rows = con.execute(f"""
            SELECT table_name FROM duckdb_tables()
            WHERE schema_name = 'main' AND table_name LIKE '{layer.lower()}%'
            ORDER BY table_name
        """).fetchall()
        for (tbl,) in rows:
            n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl:<30} {n:>10,} 行")

    con.close()
    print(f"\n✅ 库已落盘: {WAREHOUSE_PATH}")


if __name__ == "__main__":
    build_warehouse()
