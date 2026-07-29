"""
数仓元数据抽取器
- 从 DuckDB 读所有表+字段+样本
- 输出 metadata.json,给数仓理解 agent 当 LLM 上下文
"""
import duckdb
import json
import os
import re

WAREHOUSE_PATH = os.path.join(os.path.dirname(__file__), "ecommerce.duckdb")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "metadata.json")


def parse_ddl_for_comments(sql_text):
    """
    从 DDL 里抽字段注释 — 用 '--' 行注释
    """
    field_comments = {}
    current_table = None
    for line in sql_text.split("\n"):
        line_strip = line.strip()
        if line_strip.startswith("CREATE TABLE"):
            m = re.search(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)", line_strip)
            if m:
                current_table = m.group(1)
                field_comments[current_table] = {}
        elif current_table and "--" in line_strip:
            # 尝试找 "field_name ... -- comment"
            parts = line_strip.split("--", 1)
            field_part = parts[0]
            comment = parts[1].strip()
            fm = re.search(r"^\s*(\w+)", field_part)
            if fm:
                field_comments[current_table][fm.group(1)] = comment
    return field_comments


def extract_metadata():
    if not os.path.exists(WAREHOUSE_PATH):
        raise FileNotFoundError(f"库不存在,先跑 seed_data.py: {WAREHOUSE_PATH}")

    con = duckdb.connect(WAREHOUSE_PATH, read_only=True)

    # 读 DDL 拿注释
    ddl_path = os.path.join(os.path.dirname(__file__), "schema_ddl.sql")
    with open(ddl_path) as f:
        ddl_text = f.read()
    field_comments = parse_ddl_for_comments(ddl_text)

    tables = con.execute("""
        SELECT table_name, estimated_size
        FROM duckdb_tables()
        WHERE schema_name = 'main'
        ORDER BY table_name
    """).fetchall()

    metadata = {
        "warehouse_name": "ecommerce_mock_warehouse",
        "is_mock": True,
        "data_range": "2025-01-01 ~ 2026-07-31 (含去年数据,可做同比)",
        "tables": [],
        "layer_priority": [
            "🥇 优先 ADS: 直接出报表,无聚合",
            "🥈 次选 DWS: 主题宽表,轻度聚合",
            "🥉 末选 DWD: 原始明细,自由度高但要自己 join"
        ]
    }

    for tbl_name, _ in tables:
        # 行数
        n_rows = con.execute(f"SELECT COUNT(*) FROM {tbl_name}").fetchone()[0]

        # 字段
        cols = con.execute(f"DESCRIBE {tbl_name}").fetchall()
        fields = []
        for col in cols:
            fname = col[0]
            ftype = col[1]
            nullable = col[2]
            comment = field_comments.get(tbl_name, {}).get(fname, "")
            fields.append({
                "name": fname,
                "type": ftype,
                "nullable": nullable,
                "comment": comment,
            })

        # 分层
        if tbl_name.startswith("dwd_"):
            layer = "DWD"
            desc_suffix = "原始明细,需自行 join 维表"
        elif tbl_name.startswith("dws_"):
            layer = "DWS"
            desc_suffix = "主题宽表,已按日×维度聚合"
        else:
            layer = "ADS"
            desc_suffix = "指标应用层,直接出报表"

        # 用途建议(基于表名启发式)
        use_cases = []
        if "trade_order" in tbl_name:
            use_cases = ["订单明细分析", "GMV 自由聚合", "新老客拆分"]
        elif "user_register" in tbl_name:
            use_cases = ["用户增量", "注册渠道分析", "cohort 留存起点"]
        elif "user_login" in tbl_name:
            use_cases = ["DAU", "留存计算", "用户活跃度"]
        elif "product_sku" in tbl_name:
            use_cases = ["商品维表", "类目下钻", "品牌分析"]
        elif "coupon_use" in tbl_name:
            use_cases = ["券核销明细", "ROI 计算", "券→订单关联"]
        elif "traffic_visit" in tbl_name:
            use_cases = ["UV/PV 流量分析", "漏斗上游", "渠道分布"]
        elif "trade_user_day" in tbl_name:
            use_cases = ["交易主题", "新老客对比", "渠道×类目"]
        elif "user_action_day" in tbl_name:
            use_cases = ["用户行为汇总", "复购率计算"]
        elif "product_sale_day" in tbl_name:
            use_cases = ["商品日销", "动销分析", "爆款识别"]
        elif "coupon_effect_day" in tbl_name:
            use_cases = ["券效果日表", "券 ROI"]
        elif "traffic_channel_day" in tbl_name:
            use_cases = ["渠道流量日表", "UV 趋势"]
        elif "gmv_daily" in tbl_name:
            use_cases = ["GMV 主报表", "日粒度", "含同比"]
        elif "user_retention" in tbl_name:
            use_cases = ["用户留存", "cohort 分析"]
        elif "category_rank" in tbl_name:
            use_cases = ["类目 GMV 排名", "含同比", "下钻到 L2"]
        elif "coupon_roi" in tbl_name:
            use_cases = ["券 ROI 直接看", "按券类型拆"]
        elif "conversion_funnel" in tbl_name:
            use_cases = ["转化漏斗", "4 步转化率", "按渠道"]
        else:
            use_cases = [desc_suffix]

        metadata["tables"].append({
            "name": tbl_name,
            "layer": layer,
            "row_count": n_rows,
            "fields": fields,
            "use_cases": use_cases,
            "note": desc_suffix,
        })

    con.close()

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    print(f"✅ 元数据已导出: {OUTPUT_PATH}")
    print(f"   共 {len(metadata['tables'])} 张表 (DWD/DWS/ADS)")


if __name__ == "__main__":
    extract_metadata()
