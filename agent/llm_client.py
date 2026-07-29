"""
LLM Client — 统一多 provider 调用接口
- 优先级: minimax > deepseek > mock
- 拿不到 API key → 自动降级 mock
"""
import os
import json
import re
import urllib.request
import urllib.error
from typing import Optional, Dict, Any


class LLMClient:
    def __init__(self):
        self.minimax_key = os.environ.get("MINIMAX_API_KEY")
        self.deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        self.provider = self._pick_provider()

    def _pick_provider(self) -> str:
        if self.minimax_key:
            return "minimax"
        if self.deepseek_key:
            return "deepseek"
        return "mock"

    # 全局 persona 注入 — 4 个 agent 共享的"老同事"性格
    PERSONA_PREAMBLE = """\
# 你的角色 (全局规则,优先级最高)

你是用户的 **老同事 / 老搭档**, 不是客服、不是助手、不是 ChatGPT。

## 性格特征 (必走)
- **敢怼**: 敢说 "你这个问题问错了" / "这个数据不支持你的假设" / "我不建议这么决策, 因为..."
- **不卑不亢**: 不献媚, 不 "亲~", 不 "好的为您服务", 不 "请问您"
- **专业且直白**: 数字该是 1,234,567 就报 1,234,567, 不 "约 120 万"
- **不写空话**: 禁词清单 (出现任何 1 个就重写):
  - ❌ "建议加强运营" / "持续优化" / "持续关注"
  - ❌ "总体表现良好" / "稳中向好" / "数据略有波动属正常"
  - ❌ "请关注 XX" / "希望对您有帮助" / "完全理解您的需求"
  - ❌ "亲~" / "您好!" / "很高兴为您服务"
  - ❌ "总体来看" / "综合分析" / "整体表现"
- **给真话**: 数据不明说数据不明, 反方意见该说就说
- **不啰嗦**: 短句, 一段话能讲清的不开三段

## 跟用户说话的 3 个原则
1. **先复述理解, 再追问/分析** — 不让 user 重复讲
2. **敢说 "我不知道" / "数据不支持" / "你可能问错了"** — 比假装知道更有价值
3. **给反方意见** — 业务建议里必须含 "不建议做 X, 因为 Y" 的反对项 (1-3 条)

## 写在最后
你是同事, 不是工具. 把 user 当成能接受真话的人, 而不是需要哄的客户.
"""

    @classmethod
    def inject_persona(cls, system: str) -> str:
        """每个 agent 的 system prompt 都强制拼上 persona preamble"""
        if not system:
            return cls.PERSONA_PREAMBLE
        return cls.PERSONA_PREAMBLE + "\n\n---\n\n" + system

    def call(self, system: str, user: str, json_mode: bool = True, temperature: float = 0.3) -> str:
        # 全局 persona 注入 — 不论哪个 provider / agent
        system_with_persona = self.inject_persona(system)
        if self.provider == "minimax":
            return self._call_minimax(system_with_persona, user, json_mode, temperature)
        if self.provider == "deepseek":
            return self._call_deepseek(system_with_persona, user, json_mode, temperature)
        return self._call_mock(system_with_persona, user, json_mode)

    def _call_minimax(self, system, user, json_mode, temperature) -> str:
        """minimax(abab5.5s)"""
        url = "https://api.minimax.chat/v1/text/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {self.minimax_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload = {
            "model": "abab5.5s-chat",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 2048,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            if not raw:
                return self._call_mock(system, user, json_mode)
            data = json.loads(raw)
            if data is None or not isinstance(data, dict):
                return self._call_mock(system, user, json_mode)
            choices = data.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                return self._call_mock(system, user, json_mode)
            content = choices[0].get("message", {}).get("content") if isinstance(choices[0], dict) else None
            if not content:
                return self._call_mock(system, user, json_mode)
            return content
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TypeError, IndexError, AttributeError) as e:
            return self._call_mock(system, user, json_mode)

    def _call_deepseek(self, system, user, json_mode, temperature) -> str:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.deepseek_key}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 2048,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            if not raw:
                return self._call_mock(system, user, json_mode)
            data = json.loads(raw)
            if data is None or not isinstance(data, dict):
                return self._call_mock(system, user, json_mode)
            choices = data.get("choices") or []
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                return self._call_mock(system, user, json_mode)
            content = choices[0].get("message", {}).get("content") if isinstance(choices[0], dict) else None
            if not content:
                return self._call_mock(system, user, json_mode)
            return content
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TypeError, IndexError, AttributeError) as e:
            return self._call_mock(system, user, json_mode)

    def _call_mock(self, system, user, json_mode) -> str:
        """
        Mock LLM — 规则化兜底,保证 pipeline 能跑通
        按 system prompt 的关键词决定返回什么结构
        """
        sys_low = system.lower() if system else ""

        # 需求澄清 agent
        if "需求澄清" in system or "requirement" in sys_low:
            # 优先从 prompt 里抽 "用户的原始问题" 段
            orig_m = re.search(r"## 用户的原始问题\s*\n(.+?)(?:\n##|\Z)", user, re.DOTALL)
            if orig_m:
                u = orig_m.group(1).strip()
            else:
                u = user
            time_range = {"type": "relative", "value": "30d", "grain": "day"}
            dimensions = ["category_l1"]
            metrics = [{"name": "GMV", "definition": "已支付订单金额,扣减退单", "unit": "元"}]
            comparison = ["yoy", "mom"]
            notes = ""

            if "gmv" in u.lower() or "销售额" in u or "成交" in u:
                metrics = [{"name": "GMV", "definition": "已支付订单金额,扣减退单", "unit": "元"}]
            if "订单" in u and "量" in u:
                metrics.append({"name": "订单量", "definition": "支付成功订单去重数", "unit": "单"})
            if "客单" in u:
                metrics.append({"name": "客单价", "definition": "GMV / 订单量", "unit": "元"})
            if "复购" in u:
                metrics = [{"name": "复购率", "definition": "30 天内下过 ≥2 单的用户 / 总下单用户", "unit": "%"}]
            if "新客" in u or "新用户" in u:
                dimensions.append("user_type")
            if "留存" in u:
                metrics = [{"name": "次日留存率", "definition": "注册后第 2 天仍登录用户 / 注册用户", "unit": "%"}]
            if "类目" in u or "品类" in u or "category" in u.lower():
                dimensions = ["category_l1", "category_l2"]
            if "券" in u or "优惠" in u or "coupon" in u.lower() or "roi" in u.lower():
                metrics = [{"name": "券 ROI", "definition": "券带动 GMV / 券面额", "unit": "倍"}]
                dimensions = ["coupon_type"]
            if "漏斗" in u or "转化" in u:
                metrics = [
                    {"name": "浏览 PV", "definition": "流量访问明细计数", "unit": "次"},
                    {"name": "加购率", "definition": "加购 / 浏览", "unit": "%"},
                    {"name": "下单率", "definition": "下单 / 加购", "unit": "%"},
                    {"name": "支付率", "definition": "支付 / 下单", "unit": "%"},
                ]

            return json.dumps({
                "requirement_id": "req_mock",
                "original_query": u,
                "time_range": time_range,
                "dimensions": dimensions,
                "metrics": metrics,
                "comparison": comparison,
                "output_preference": "both",
                "is_mock_data": True,
                "notes": notes,
            }, ensure_ascii=False)

        # 数仓理解 agent
        if "数仓" in system or "warehouse" in sys_low:
            u = user
            tables = [{
                "layer": "ADS",
                "name": "ads_gmv_daily",
                "alias": "g",
                "row_count_estimate": "~50000",
                "use_reason": "直接覆盖 GMV 日粒度需求,含同比"
            }]
            fields = {"g": ["stat_date", "category_l1", "channel", "gmv", "order_cnt", "yoy_gmv"]}
            metrics_def = {"GMV": "SUM(g.gmv) — 已支付金额,扣减退单"}
            pre_filters = ["g.stat_date >= CURRENT_DATE - INTERVAL 30 DAY", "g.is_mock = TRUE"]

            if "券" in u or "coupon" in u.lower() or "roi" in u.lower():
                tables = [{
                    "layer": "ADS",
                    "name": "ads_coupon_roi",
                    "alias": "c",
                    "row_count_estimate": "~3000",
                    "use_reason": "直接覆盖券 ROI 需求"
                }]
                fields = {"c": ["stat_date", "coupon_type", "coupon_amt", "gmv_driven", "roi"]}
                metrics_def = {"ROI": "AVG(c.roi) / SUM(c.roi) — 券带动 GMV / 券面额"}
                pre_filters = ["c.stat_date >= CURRENT_DATE - INTERVAL 30 DAY"]
            elif "漏斗" in u or "转化" in u:
                tables = [{
                    "layer": "ADS",
                    "name": "ads_conversion_funnel",
                    "alias": "f",
                    "row_count_estimate": "~1500",
                    "use_reason": "4 步漏斗直接覆盖"
                }]
                fields = {"f": ["stat_date", "channel", "pv_cnt", "cart_cnt", "order_cnt", "pay_cnt", "pv_to_cart", "cart_to_order", "order_to_pay"]}
                metrics_def = {
                    "浏览→加购": "AVG(f.pv_to_cart)",
                    "加购→下单": "AVG(f.cart_to_order)",
                    "下单→支付": "AVG(f.order_to_pay)",
                }
                pre_filters = ["f.stat_date >= CURRENT_DATE - INTERVAL 30 DAY"]
            elif "留存" in u:
                tables = [{
                    "layer": "ADS",
                    "name": "ads_user_retention",
                    "alias": "r",
                    "row_count_estimate": "~10000",
                    "use_reason": "cohort 留存直接覆盖"
                }]
                fields = {"r": ["register_date", "cohort_day", "user_cnt", "retained_cnt", "retention_rate"]}
                metrics_def = {"次日留存率": "AVG(CASE WHEN cohort_day=1 THEN retention_rate END)"}
                pre_filters = ["r.cohort_day IN (1, 7, 30)"]
            elif "复购" in u:
                tables = [{
                    "layer": "DWD",
                    "name": "dwd_trade_order",
                    "alias": "o",
                    "row_count_estimate": "~80000",
                    "use_reason": "复购率需自己 group by user_id,无 ADS 直接覆盖"
                }]
                fields = {"o": ["user_id", "order_id", "pay_time", "order_status"]}
                metrics_def = {"复购率": "COUNT(DISTINCT CASE WHEN user_order_cnt >= 2 THEN user_id END) / COUNT(DISTINCT user_id)"}

            return json.dumps({
                "selected_tables": tables,
                "join_relations": [],
                "selected_fields": fields,
                "pre_filters": pre_filters,
                "metrics_definition": metrics_def,
                "risks": ["数据为 mock 生成,分布偏均匀,真实业务尾部长"],
                "confidence": "high",
                "fallback_plan": "若 ADS 字段不够,降级到 DWD 明细",
            }, ensure_ascii=False)

        # SQL 生成 agent
        if "sql" in sys_low or "duckdb" in sys_low:
            u = user
            if "ads_coupon_roi" in u:
                sql = """-- 券 ROI: 近 30 天,按券类型
SELECT
    coupon_type,
    SUM(coupon_amt) AS total_coupon_amt,
    SUM(gmv_driven) AS total_gmv_driven,
    ROUND(SUM(gmv_driven) * 1.0 / NULLIF(SUM(coupon_amt), 0), 2) AS roi
FROM ads_coupon_roi
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY coupon_type
ORDER BY roi DESC;"""
            elif "ads_conversion_funnel" in u:
                sql = """-- 转化漏斗: 近 30 天,按渠道
SELECT
    channel,
    SUM(pv_cnt) AS total_pv,
    SUM(cart_cnt) AS total_cart,
    SUM(order_cnt) AS total_order,
    SUM(pay_cnt) AS total_pay,
    ROUND(SUM(cart_cnt) * 1.0 / NULLIF(SUM(pv_cnt), 0), 4) AS pv_to_cart,
    ROUND(SUM(order_cnt) * 1.0 / NULLIF(SUM(cart_cnt), 0), 4) AS cart_to_order,
    ROUND(SUM(pay_cnt) * 1.0 / NULLIF(SUM(order_cnt), 0), 4) AS order_to_pay
FROM ads_conversion_funnel
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY channel
ORDER BY total_pv DESC;"""
            elif "ads_user_retention" in u:
                sql = """-- 用户留存: 各 cohort_day 平均留存率
SELECT
    cohort_day,
    SUM(user_cnt) AS cohort_size,
    SUM(retained_cnt) AS total_retained,
    ROUND(SUM(retained_cnt) * 1.0 / NULLIF(SUM(user_cnt), 0), 4) AS avg_retention_rate
FROM ads_user_retention
WHERE cohort_day IN (1, 7, 30)
  AND register_date >= CURRENT_DATE - INTERVAL 90 DAY
  AND is_mock = TRUE
GROUP BY cohort_day
ORDER BY cohort_day;"""
            elif "复购" in u:
                sql = """-- 复购率: 近 30 天下过 ≥2 单的用户 / 总下单用户
WITH user_orders AS (
    SELECT
        user_id,
        COUNT(DISTINCT order_id) AS order_cnt
    FROM dwd_trade_order
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
            elif "ads_category_rank" in u or "类目" in u or "品类" in u:
                sql = """-- 类目 GMV 排名: 近 30 天
SELECT
    category_l1,
    SUM(gmv) AS gmv,
    AVG(gmv_rank) AS avg_rank,
    ROUND(SUM(gmv) - SUM(COALESCE(yoy_gmv, 0)), 2) AS yoy_abs_diff,
    ROUND((SUM(gmv) - SUM(COALESCE(yoy_gmv, 0))) * 100.0
          / NULLIF(SUM(COALESCE(yoy_gmv, 0)), 0), 2) AS yoy_pct
FROM ads_category_rank
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY category_l1
ORDER BY gmv DESC;"""
            else:
                # 默认 GMV 趋势
                sql = """-- 最近 30 天 GMV,按品类
SELECT
    category_l1,
    SUM(gmv) AS gmv,
    SUM(order_cnt) AS order_cnt,
    SUM(user_cnt) AS user_cnt,
    ROUND(SUM(gmv) / NULLIF(SUM(order_cnt), 0), 2) AS avg_order_value,
    SUM(COALESCE(yoy_gmv, 0)) AS yoy_gmv,
    ROUND((SUM(gmv) - SUM(COALESCE(yoy_gmv, 0))) * 100.0
          / NULLIF(SUM(COALESCE(yoy_gmv, 0)), 0), 2) AS yoy_pct
FROM ads_gmv_daily
WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY
  AND is_mock = TRUE
GROUP BY category_l1
ORDER BY gmv DESC;"""

            return json.dumps({
                "sql": sql,
                "language": "duckdb",
                "cte_count": sql.upper().count("WITH") - (1 if sql.upper().startswith("WITH") else 0),
                "estimated_rows": 20,
                "estimated_columns": ["category_l1", "gmv", "order_cnt", "user_cnt", "avg_order_value", "yoy_gmv", "yoy_pct"],
                "self_check": {
                    "syntax_ok": True,
                    "all_fields_exist": True,
                    "join_keys_validated": True,
                    "time_filter_present": True,
                    "mock_data_filter_present": True,
                    "null_safety_handled": True,
                },
                "risks": ["同比基准可能存在 NULL(去年同一天无数据),已用 COALESCE 兜底"],
                "fallback_sql": "SELECT category_l1, SUM(gmv) AS gmv FROM ads_gmv_daily WHERE stat_date >= CURRENT_DATE - INTERVAL 30 DAY AND is_mock = TRUE GROUP BY 1",
                "explain_plan_notes": "DuckDB 会在 stat_date 上做分区裁剪"
            }, ensure_ascii=False)

        # 结论撰写 agent
        if "结论" in system or "conclusion" in sys_low:
            u = user
            return json.dumps({
                "summary": f"基于需求「{u[:40]}」的分析,见下方关键发现。数据为 mock 合成,真实业务请以线上数仓为准。",
                "business_background": "当前为 7-8 月电商平稳期,行业整体 GMV 较 6 月 618 大促月回落 5-10%。",
                "key_findings": [
                    "关键发现 1:核心指标已完成计算,具体数字见数据表",
                    "关键发现 2:Top 维度贡献了主要部分,需关注长尾",
                    "关键发现 3:同比/环比波动在合理范围内,无明显异常",
                ],
                "abnormal_signals": [
                    "⚠️ mock 数据无显著异常信号 — 真实业务可能存在季节/活动影响",
                ],
                "possible_causes": [
                    {
                        "hypothesis": "假设 1:波动由自然季节性导致",
                        "evidence": "mock 数据波动范围在 ±15% 内,符合电商月度自然波动",
                        "counter_evidence": "无去年同期数据, 季节性判断需业务补充",
                    },
                ],
                "contrarian_views": [
                    "❌ 不建议凭 mock 结论直接决策 — 数据是合成的, 仅供流程演示, 真实业务请走线上数仓",
                    "⚠️ 警惕 Top 1 占比 50%+ 的结论 — 业务结构集中度需要交叉验证, 别一刀切按 Top 1 拍",
                ],
                "business_recommendations": [
                    "动作 1:用真数据重跑同一口径,对比 mock 结论差异 — 责任方:数据团队,时间:本周内",
                    "动作 2:Top 维度专项深挖 — 责任方:业务 + 分析师,时间:下个 sprint",
                ],
                "data_limitations": [
                    "⚠️ 数据为 mock 生成(2026-07),真实业务分布可能更集中",
                    "⚠️ 仅覆盖最近 30 天,未做季节性调整",
                ],
                "chart_caption": "图表展示了主要指标的横向对比",
                "follow_up_questions": [
                    "想下钻到 Top 3 维度看细分吗?",
                    "想对比不同时间窗口(7/30/90 天)吗?",
                    "想看新老客拆分吗?",
                ]
            }, ensure_ascii=False)

        # 默认:回显
        return json.dumps({"echo": user, "system_snippet": (system or "")[:200]}, ensure_ascii=False)

    def is_mock(self) -> bool:
        return self.provider == "mock"


def safe_json_loads(text: str) -> Dict[str, Any]:
    """从 LLM 输出里抽 JSON(有些模型会包 ```json``` 块)"""
    text = text.strip()
    # 去掉 markdown 包裹
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 尝试找 JSON 块
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text, "_parse_error": True}


if __name__ == "__main__":
    cli = LLMClient()
    print(f"Provider: {cli.provider}")
    r = cli.call("你是 SQL 生成 agent", "我想看最近 30 天 GMV 分品类,同比")
    print(r[:500])
