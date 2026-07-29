"""
4 个 Agent 节点实现
- 接收上游输出 + 系统 prompt
- 调用 LLM
- 解析 JSON
- 返回结构化结果
"""
import os
import json
import re
from typing import Dict, Any, Optional
from datetime import datetime

from .llm_client import LLMClient, safe_json_loads

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")
METADATA_PATH = os.path.join(os.path.dirname(__file__), "..", "warehouse", "metadata.json")


def load_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    with open(path) as f:
        return f.read()


def load_metadata() -> Dict[str, Any]:
    if not os.path.exists(METADATA_PATH):
        return {"tables": [], "is_mock": True, "_warning": "metadata.json not generated yet"}
    with open(METADATA_PATH) as f:
        return json.load(f)


class RequirementClarifier:
    """Agent 01: 多轮需求澄清"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system = load_prompt("01_requirement_clarifier")

    def parse(self, history: list, new_message: str) -> Dict[str, Any]:
        """
        处理一轮对话
        history: 之前的对话 [{"role": "assistant", "content": "..."}]
        new_message: 用户这一轮的话
        returns: {
            "phase": "clarify" | "ready",
            "reply": "<给用户看的话>",
            "spec": <最终 JSON 或 None>,
            "round": <轮次>,
        }
        """
        # 数已问了几轮(assistant 主动反问的次数)
        round_count = sum(1 for m in history if m.get("role") == "assistant" and "?" in m.get("content", ""))

        # 拼 user 上下文
        conversation = "\n".join(
            f"[{m['role']}] {m['content']}" for m in history + [{"role": "user", "content": new_message}]
        )

        # 给 LLM 的 user 消息:包含原始 query + 完整对话
        original_query = (history[0]["content"] if history else new_message) or new_message
        prompt = f"""{self.system}

## 用户的原始问题
{original_query}

## 当前对话历史(可能多轮)
{conversation}

## 累计反问轮次
{round_count} / 3

## 你的任务
1. 复述你理解的部分
2. 如果还有关键槽位缺失,反问(最多 2 个)
3. 如果已经够 3 轮或槽位齐了,直接输出最终 JSON
4. 输出严格用以下 JSON 格式:

```json
{{
  "phase": "clarify" | "ready",
  "reply": "<给用户看的文字>",
  "spec": <如果是 ready,填完整规格 JSON;如果是 clarify,填 null>,
  "round": {round_count}
}}
```"""

        raw = self.llm.call(self.system, prompt, json_mode=True, temperature=0.2)
        result = safe_json_loads(raw)

        # 兜底:如果 LLM 没按格式来,降级处理
        if not isinstance(result, dict) or "phase" not in result:
            # 简单启发:第 1 轮反问,后面给规格
            if round_count >= 2:
                # 找原始 query
                orig = (history[0]["content"] if history else new_message) or new_message
                result = {
                    "phase": "ready",
                    "reply": "好的,我按默认来出报表。",
                    "spec": self._fallback_spec(orig),
                    "round": round_count,
                }
            else:
                result = {
                    "phase": "clarify",
                    "reply": f"我理解你想看:{new_message}\n请告诉我:\n1. 时间范围(默认 30 天)?\n2. 要不要拆维度(默认按品类)?",
                    "spec": None,
                    "round": round_count,
                }
        return result

    def _fallback_spec(self, query: str) -> Dict:
        # 根据 query 关键词调整 metrics/dimensions
        q = (query or "").lower()
        if "复购" in q:
            metrics = [{"name": "复购率", "definition": "30 天内下过 ≥2 单的用户 / 总下单用户", "unit": "%"}]
            dimensions = ["user_type"]
        elif "留存" in q:
            metrics = [{"name": "次日留存率", "definition": "注册后第 2 天仍登录用户 / 注册用户", "unit": "%"}]
            dimensions = ["register_date"]
        elif "券" in q or "coupon" in q or "roi" in q:
            metrics = [{"name": "券 ROI", "definition": "券带动 GMV / 券面额", "unit": "倍"}]
            dimensions = ["coupon_type"]
        elif "漏斗" in q or "转化" in q:
            metrics = [
                {"name": "浏览 PV", "definition": "流量访问计数", "unit": "次"},
                {"name": "加购率", "definition": "加购 / 浏览", "unit": "%"},
                {"name": "下单率", "definition": "下单 / 加购", "unit": "%"},
                {"name": "支付率", "definition": "支付 / 下单", "unit": "%"},
            ]
            dimensions = ["channel"]
        elif "客单" in q:
            metrics = [{"name": "客单价", "definition": "GMV / 订单量", "unit": "元"}]
            dimensions = ["category_l1"]
        else:
            metrics = [{"name": "GMV", "definition": "已支付订单金额,扣减退单", "unit": "元"}]
            dimensions = ["category_l1"]
        return {
            "requirement_id": f"req_{int(datetime.now().timestamp())}",
            "original_query": query,
            "time_range": {"type": "relative", "value": "30d", "grain": "day"},
            "dimensions": dimensions,
            "metrics": metrics,
            "comparison": ["yoy", "mom"],
            "output_preference": "both",
            "is_mock_data": True,
            "notes": "fallback 规格,槽位不齐时使用",
        }


class WarehouseUnderstander:
    """Agent 02: 数仓理解,定位表+字段+口径"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system = load_prompt("02_warehouse_understander")
        self.metadata = load_metadata()

    def understand(self, spec: Dict) -> Dict[str, Any]:
        meta_str = json.dumps(self.metadata, ensure_ascii=False, indent=2)
        user = f"""## 结构化需求
{json.dumps(spec, ensure_ascii=False, indent=2)}

## 数仓元数据(完整)
{meta_str}

请按 system prompt 的 JSON schema 输出选表+字段+口径。"""

        raw = self.llm.call(self.system, user, json_mode=True, temperature=0.1)
        result = safe_json_loads(raw)
        if not isinstance(result, dict) or "selected_tables" not in result:
            # fallback
            return self._fallback(spec)
        return result

    def _fallback(self, spec: Dict) -> Dict:
        q = (spec.get("original_query", "") + " " + str(spec.get("metrics", ""))).lower()
        # 决定表
        if "复购" in q:
            tbl = "dwd_trade_order"
            alias = "o"
            fields = ["user_id", "order_id", "pay_time", "order_status"]
            metrics_def = {"复购率": "COUNT(DISTINCT CASE WHEN order_cnt >= 2 THEN user_id END) / COUNT(DISTINCT user_id)"}
            pre = ["o.pay_time >= CURRENT_DATE - INTERVAL 30 DAY", "o.is_mock = TRUE"]
        elif "券" in q or "roi" in q:
            tbl = "ads_coupon_roi"
            alias = "c"
            fields = ["stat_date", "coupon_type", "coupon_amt", "gmv_driven", "roi"]
            metrics_def = {"ROI": "AVG(c.roi)"}
            pre = ["c.stat_date >= CURRENT_DATE - INTERVAL 30 DAY", "c.is_mock = TRUE"]
        elif "留存" in q:
            tbl = "ads_user_retention"
            alias = "r"
            fields = ["register_date", "cohort_day", "user_cnt", "retained_cnt", "retention_rate"]
            metrics_def = {"次日留存率": "AVG(CASE WHEN cohort_day=1 THEN retention_rate END)"}
            pre = ["r.cohort_day IN (1, 7, 30)", "r.is_mock = TRUE"]
        elif "漏斗" in q or "转化" in q:
            tbl = "ads_conversion_funnel"
            alias = "f"
            fields = ["stat_date", "channel", "pv_cnt", "cart_cnt", "order_cnt", "pay_cnt", "pv_to_cart", "cart_to_order", "order_to_pay"]
            metrics_def = {
                "浏览→加购": "AVG(f.pv_to_cart)",
                "加购→下单": "AVG(f.cart_to_order)",
                "下单→支付": "AVG(f.order_to_pay)",
            }
            pre = ["f.stat_date >= CURRENT_DATE - INTERVAL 30 DAY", "f.is_mock = TRUE"]
        else:
            tbl = "ads_gmv_daily"
            alias = "g"
            fields = ["stat_date", "category_l1", "channel", "gmv", "order_cnt", "user_cnt", "yoy_gmv"]
            metrics_def = {"GMV": "SUM(g.gmv)"}
            pre = ["g.stat_date >= CURRENT_DATE - INTERVAL 30 DAY", "g.is_mock = TRUE"]
        return {
            "selected_tables": [{
                "layer": tbl.split("_")[0].upper(),
                "name": tbl,
                "alias": alias,
                "row_count_estimate": "~10000",
                "use_reason": f"fallback — 匹配 {q[:30]!r}"
            }],
            "join_relations": [],
            "selected_fields": {alias: fields},
            "pre_filters": pre,
            "metrics_definition": metrics_def,
            "risks": ["fallback 选择,confidence low"],
            "confidence": "low",
            "fallback_plan": "看实际跑出来的数据,调整选表"
        }


class SQLGenerator:
    """Agent 03: SQL 生成"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system = load_prompt("03_sql_generator")

    def generate(self, spec: Dict, warehouse_plan: Dict) -> Dict[str, Any]:
        user = f"""## 结构化需求
{json.dumps(spec, ensure_ascii=False, indent=2)}

## 数仓理解结果
{json.dumps(warehouse_plan, ensure_ascii=False, indent=2)}

请按 system prompt 的 JSON schema 输出 DuckDB SQL,必须含 5 项自检全过。"""
        raw = self.llm.call(self.system, user, json_mode=True, temperature=0.1)
        result = safe_json_loads(raw)
        if not isinstance(result, dict) or "sql" not in result:
            return self._fallback(spec, warehouse_plan)
        return result

    def _fallback(self, spec, plan) -> Dict:
        # 拿第一个表的别名和名字
        tables = plan.get("selected_tables", [])
        if not tables:
            return {"sql": "SELECT 1", "error": "no tables selected"}
        tbl = tables[0]["name"]
        alias = tables[0].get("alias", "t")
        q = (spec.get("original_query", "") or "").lower()

        if "ads_coupon_roi" in tbl:
            sql = f"""-- 券 ROI: 近 30 天按券类型
SELECT
    coupon_type,
    SUM(coupon_amt) AS total_coupon_amt,
    SUM(gmv_driven) AS total_gmv_driven,
    ROUND(SUM(gmv_driven) * 1.0 / NULLIF(SUM(coupon_amt), 0), 2) AS roi
FROM {tbl}
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY coupon_type
ORDER BY roi DESC;"""
            cols = ["coupon_type", "total_coupon_amt", "total_gmv_driven", "roi"]
        elif "ads_conversion_funnel" in tbl:
            sql = f"""-- 转化漏斗: 近 30 天按渠道
SELECT
    channel,
    SUM(pv_cnt) AS total_pv,
    SUM(cart_cnt) AS total_cart,
    SUM(order_cnt) AS total_order,
    SUM(pay_cnt) AS total_pay,
    ROUND(SUM(cart_cnt) * 1.0 / NULLIF(SUM(pv_cnt), 0), 4) AS pv_to_cart,
    ROUND(SUM(order_cnt) * 1.0 / NULLIF(SUM(cart_cnt), 0), 4) AS cart_to_order,
    ROUND(SUM(pay_cnt) * 1.0 / NULLIF(SUM(order_cnt), 0), 4) AS order_to_pay
FROM {tbl}
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY channel
ORDER BY total_pv DESC;"""
            cols = ["channel", "total_pv", "total_cart", "total_order", "total_pay", "pv_to_cart", "cart_to_order", "order_to_pay"]
        elif "ads_user_retention" in tbl:
            sql = f"""-- 用户留存: 各 cohort_day 平均
SELECT
    cohort_day,
    SUM(user_cnt) AS cohort_size,
    SUM(retained_cnt) AS total_retained,
    ROUND(SUM(retained_cnt) * 1.0 / NULLIF(SUM(user_cnt), 0), 4) AS avg_retention_rate
FROM {tbl}
WHERE cohort_day IN (1, 7, 30)
  AND register_date >= CURRENT_DATE - INTERVAL 90 DAY
  AND is_mock = TRUE
GROUP BY cohort_day
ORDER BY cohort_day;"""
            cols = ["cohort_day", "cohort_size", "total_retained", "avg_retention_rate"]
        elif "dwd_trade_order" in tbl and "复购" in q:
            sql = f"""-- 复购率: 近 30 天下过 ≥2 单的用户 / 总下单用户
WITH user_orders AS (
    SELECT
        user_id,
        COUNT(DISTINCT order_id) AS order_cnt
    FROM {tbl}
    WHERE pay_time >= CURRENT_DATE - INTERVAL 30 DAY
      AND order_status IN ('paid', 'finished')
      AND is_mock = TRUE
    GROUP BY user_id
)
SELECT
    COUNT(*) AS total_buyer,
    SUM(CASE WHEN order_cnt >= 2 THEN 1 ELSE 0 END) AS repeat_buyer,
    ROUND(SUM(CASE WHEN order_cnt >= 2 THEN 1 ELSE 0 END) * 1.0
          / NULLIF(COUNT(*), 0), 4) AS repurchase_rate
FROM user_orders;"""
            cols = ["total_buyer", "repeat_buyer", "repurchase_rate"]
        elif "ads_gmv_daily" in tbl or "ads_category_rank" in tbl:
            sql = f"""-- GMV 分品类: 近 30 天
SELECT
    category_l1,
    SUM(gmv) AS gmv,
    SUM(order_cnt) AS order_cnt,
    ROUND(SUM(gmv) / NULLIF(SUM(order_cnt), 0), 2) AS avg_order_value,
    ROUND((SUM(gmv) - SUM(COALESCE(yoy_gmv, 0))) * 100.0
          / NULLIF(SUM(COALESCE(yoy_gmv, 0)), 0), 2) AS yoy_pct
FROM {tbl}
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY category_l1
ORDER BY gmv DESC;"""
            cols = ["category_l1", "gmv", "order_cnt", "avg_order_value", "yoy_pct"]
        else:
            sql = f"SELECT * FROM {tbl} WHERE is_mock = TRUE LIMIT 20"
            cols = ["*"]
        return {
            "sql": sql,
            "language": "duckdb",
            "cte_count": sql.upper().count("WITH"),
            "estimated_rows": 20,
            "estimated_columns": cols,
            "self_check": {"syntax_ok": True, "_fallback": True},
            "risks": ["fallback SQL — LLM 未返回,使用规则生成"],
            "fallback_sql": None,
        }


class ConclusionWriter:
    """Agent 04: 结论撰写"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system = load_prompt("04_conclusion_writer")

    def write(self, spec: Dict, sql_result: Dict, chart_path: Optional[str] = None) -> Dict[str, Any]:
        # 摘数据要点
        rows = sql_result.get("rows", [])
        cols = sql_result.get("columns", [])
        if rows:
            data_summary = {
                "columns": cols,
                "row_count": sql_result.get("row_count", len(rows)),
                "sample_first_5": rows[:5],
                "sample_last_3": rows[-3:] if len(rows) > 5 else [],
            }
        else:
            data_summary = {"empty": True, "error": sql_result.get("error", "no rows")}

        user = f"""## 需求
{json.dumps(spec, ensure_ascii=False, indent=2)}

## SQL 执行结果
{json.dumps(data_summary, ensure_ascii=False, indent=2)}

## 图表路径
{chart_path or '未生成'}

请按 system prompt 输出完整 markdown 报告(JSON schema 严格)。"""

        raw = self.llm.call(self.system, user, json_mode=True, temperature=0.5)
        result = safe_json_loads(raw)
        if not isinstance(result, dict) or "summary" not in result:
            return {
                "summary": "数据已计算完成,请查看下表。",
                "key_findings": ["数据表展示了主要结果"],
                "business_recommendations": ["结合业务上下文判断下一步"],
                "data_limitations": ["⚠️ 数据为 mock 生成"],
                "chart_caption": "图表已生成",
                "follow_up_questions": ["想看其他维度吗?"]
            }
        if chart_path:
            result["chart_path"] = chart_path
        return result
