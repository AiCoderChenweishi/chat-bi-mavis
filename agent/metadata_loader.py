"""
v0.6.17 数仓 metadata 加载器

数仓有 16 张表 (ADS/DWD/DWS 层), metadata.json 描述 schema.
启动时加载一次, 缓存到全局 _META, 给 LLM / button / SQL gen 用.

API:
- get_metadata() -> dict (raw)
- get_table(name) -> dict | None
- list_tables() -> list[str]
- get_numeric_metrics() -> list[dict]  # 所有数值字段 (gmv, order_cnt, roi, pv_cnt...)
- get_dimensions() -> list[dict]        # 所有 string/date 字段 (category_l1, channel, stat_date)
- get_tables_for_field(field) -> list[str]  # 含某字段的表
- format_for_prompt() -> str  # 给 LLM 用的 markdown 摘要
"""
import json
import os
from functools import lru_cache
from typing import Dict, List, Optional

# metadata.json 路径 (相对 chat-bi-mavis 项目根)
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "warehouse", "metadata.json"
)

# 全局缓存
_META: Optional[dict] = None
_META_PATH: Optional[str] = None


def load(path: Optional[str] = None, force: bool = False) -> dict:
    """加载 metadata.json, 缓存到全局"""
    global _META, _META_PATH
    if _META is not None and not force:
        return _META
    p = path or _DEFAULT_PATH
    if not os.path.exists(p):
        _META = {"tables": [], "warehouse_name": "(metadata not found)", "is_mock": True}
        _META_PATH = p
        return _META
    with open(p, "r", encoding="utf-8") as f:
        _META = json.load(f)
    _META_PATH = p
    return _META


def get_metadata() -> dict:
    if _META is None:
        load()
    return _META


def get_table(name: str) -> Optional[dict]:
    for t in get_metadata().get("tables", []):
        if t.get("name") == name:
            return t
    return None


def list_tables() -> List[str]:
    return [t.get("name", "?") for t in get_metadata().get("tables", [])]


def list_active_tables() -> List[str]:
    """排除 row_count=0 的空表, 只返回有数仓数据的表"""
    return [t.get("name", "?") for t in get_metadata().get("tables", []) if t.get("row_count", 0) > 0]


def _is_numeric_type(t: str) -> bool:
    """判断字段类型是不是数值"""
    t = (t or "").upper()
    return any(k in t for k in [
        "INT", "DECIMAL", "DOUBLE", "FLOAT", "NUMERIC", "REAL",
        "BIGINT", "SMALLINT", "TINYINT", "BIGSERIAL"
    ])


def _is_text_type(t: str) -> bool:
    t = (t or "").upper()
    return any(k in t for k in ["VARCHAR", "TEXT", "CHAR"])


def _is_date_type(t: str) -> bool:
    t = (t or "").upper()
    return any(k in t for k in ["DATE", "TIMESTAMP", "TIME"])


# 排除干扰字段: id 主键 / is_mock 标志 / 重复 measure (如 avg_order_value vs gmv)
_EXCLUDE_FIELDS = {"id", "is_mock", "create_time", "update_time", "dt"}


def get_numeric_metrics() -> List[dict]:
    """
    拿所有数值字段 + 来自哪张表 + 描述
    返回: [{name, type, table, comment, use_case}, ...] 去重 by name
    """
    seen = {}
    for t in get_metadata().get("tables", []):
        tname = t.get("name", "?")
        ucs = t.get("use_cases", [])
        for f in t.get("fields", []):
            fname = f.get("name", "")
            if fname in _EXCLUDE_FIELDS:
                continue
            if not _is_numeric_type(f.get("type", "")):
                continue
            # 去重 (e.g. gmv 在多张表都出现, 取第一张 + 累加 table list)
            if fname not in seen:
                seen[fname] = {
                    "name": fname,
                    "type": f.get("type", ""),
                    "comment": f.get("comment", ""),
                    "tables": [tname],
                    "use_case": ucs[0] if ucs else "",
                }
            else:
                if tname not in seen[fname]["tables"]:
                    seen[fname]["tables"].append(tname)
    return sorted(seen.values(), key=lambda x: x["name"])


def get_dimensions() -> List[dict]:
    """
    拿所有 string/date 字段 (候选维度)
    返回: [{name, type, table, comment}, ...] 去重 by name
    """
    seen = {}
    for t in get_metadata().get("tables", []):
        tname = t.get("name", "?")
        for f in t.get("fields", []):
            fname = f.get("name", "")
            if fname in _EXCLUDE_FIELDS:
                continue
            if not (_is_text_type(f.get("type", "")) or _is_date_type(f.get("type", ""))):
                continue
            if fname not in seen:
                seen[fname] = {
                    "name": fname,
                    "type": f.get("type", ""),
                    "comment": f.get("comment", ""),
                    "tables": [tname],
                }
            else:
                if tname not in seen[fname]["tables"]:
                    seen[fname]["tables"].append(tname)
    return sorted(seen.values(), key=lambda x: x["name"])


def get_tables_for_field(field_name: str) -> List[str]:
    """含某字段的所有表"""
    out = []
    for t in get_metadata().get("tables", []):
        for f in t.get("fields", []):
            if f.get("name") == field_name:
                out.append(t.get("name", "?"))
                break
    return out


def get_table_summary(name: str) -> str:
    """表摘要 (一行) 给 LLM 看"""
    t = get_table(name)
    if not t:
        return f"❌ 表 {name} 不存在"
    fields = ", ".join(
        f.get("name", "?") for f in t.get("fields", [])[:8]
    )
    layer = t.get("layer", "?")
    rc = t.get("row_count", 0)
    note = t.get("note", "")
    return f"**{name}** [{layer}, {rc}行] 字段: {fields}... {note}".strip()


def format_for_prompt() -> str:
    """
    给 LLM 看的完整 metadata 摘要
    按表分块, 每表一行: name [layer] fields...
    """
    lines = ["## 数仓表清单 (共 {} 张表)\n".format(len(list_tables()))]
    # 按 layer 排序
    tables = sorted(get_metadata().get("tables", []), key=lambda x: (x.get("layer", "Z"), x.get("name", "")))
    for t in tables:
        layer = t.get("layer", "?")
        name = t.get("name", "?")
        rc = t.get("row_count", 0)
        fields = t.get("fields", [])
        # 关键字段: 数值 + 维度
        key_fields = []
        for f in fields:
            fname = f.get("name", "?")
            ftype = f.get("type", "?")
            comment = f.get("comment", "")
            if fname in _EXCLUDE_FIELDS:
                continue
            if comment:
                key_fields.append(f"{fname}({ftype}, {comment})")
            else:
                key_fields.append(f"{fname}({ftype})")
        # 截前 8 个
        fields_str = ", ".join(key_fields[:8])
        if len(key_fields) > 8:
            fields_str += f"... +{len(key_fields)-8} more"
        ucs = t.get("use_cases", [])
        uc_str = f" 用途: {'/'.join(ucs[:2])}" if ucs else ""
        lines.append(f"- **{name}** [{layer}, {rc}行] 字段: {fields_str}{uc_str}")
    return "\n".join(lines)


# === 按业务关键词从 metadata 推 table (轻量索引) ===
_KEYWORD_TABLE_HINTS = {
    "gmv": ["ads_gmv_daily", "ads_category_rank", "ads_coupon_roi"],
    "销售": ["ads_gmv_daily", "ads_category_rank"],
    "订单": ["dwd_trade_order", "ads_gmv_daily"],
    "用户": ["dwd_user_login", "dwd_user_register", "ads_user_retention", "dws_trade_user_day"],
    "留存": ["ads_user_retention", "dwd_user_login"],
    "复购": ["dwd_trade_order"],
    "活跃": ["dws_trade_user_day", "dwd_user_login"],
    "dau": ["dws_trade_user_day", "dwd_user_login"],
    "mau": ["dws_trade_user_day"],
    "注册": ["dwd_user_register"],
    "券": ["ads_coupon_roi", "dwd_coupon_use", "dws_coupon_effect_day"],
    "coupon": ["ads_coupon_roi", "dwd_coupon_use", "dws_coupon_effect_day"],
    "优惠": ["ads_coupon_roi", "dws_coupon_effect_day"],
    "roi": ["ads_coupon_roi"],
    "漏斗": ["ads_conversion_funnel"],
    "转化": ["ads_conversion_funnel"],
    "funnel": ["ads_conversion_funnel"],
    "流量": ["dwd_traffic_visit", "dws_traffic_channel_day"],
    "pv": ["dwd_traffic_visit", "ads_conversion_funnel"],
    "uv": ["dwd_traffic_visit"],
    "访问": ["dwd_traffic_visit"],
    "traffic": ["dwd_traffic_visit", "dws_traffic_channel_day"],
    "visit": ["dwd_traffic_visit", "dws_traffic_channel_day"],
    "浏览": ["dwd_traffic_visit", "ads_conversion_funnel"],
    "商品": ["dwd_product_sku", "ads_category_rank", "dws_product_sale_day"],
    "品类": ["ads_category_rank", "ads_gmv_daily"],
    "渠道": ["dwd_traffic_visit", "ads_gmv_daily", "ads_conversion_funnel"],
    "category": ["ads_category_rank", "ads_gmv_daily"],
    "channel": ["dwd_traffic_visit", "ads_gmv_daily", "ads_conversion_funnel"],
}


def suggest_tables(query: str, top_k: int = 3) -> List[str]:
    """
    按 query 关键词从 metadata 推最相关 N 张表
    不靠 LLM, 直接 keyword 匹配表名
    v0.6.17: 排除 row_count=0 的空表 (DWS 层汇总表经常没数据)
    """
    q = (query or "").lower()
    active = set(list_active_tables())  # 只推有数仓的表
    scores = {}
    for kw, tables in _KEYWORD_TABLE_HINTS.items():
        if kw in q:
            for t in tables:
                if t not in active:
                    continue
                scores[t] = scores.get(t, 0) + 1
    # 按分数排序, 取 top_k
    sorted_t = sorted(scores.items(), key=lambda x: -x[1])
    out = [t for t, _ in sorted_t[:top_k]]
    # 兜底: 如果没匹配, 推主表 (ads_gmv_daily 永远有数)
    if not out:
        out = ["ads_gmv_daily"]
    return out
