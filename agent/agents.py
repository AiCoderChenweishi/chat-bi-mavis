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
            "phase": "clarifying" | "awaiting_confirmation" | "ready",
            "reply": "<给用户看的话>",
            "spec": <最终 JSON 或 None>,
            "round": <轮次>,
            "open_questions": <还没答的>
        }
        """
        # 数已问了几轮
        round_count = sum(1 for m in history if m.get("role") == "assistant" and "?" in m.get("content", ""))

        # 拼 user 上下文
        conversation = "\n".join(
            f"[{m['role']}] {m['content']}" for m in history + [{"role": "user", "content": new_message}]
        )

        # 原始 query
        original_query = (history[0]["content"] if history else new_message) or new_message

        # 7 槽位自查(规则)
        # 优先从 history 里看 assistant 之前的反问(挑 7 槽位里有歧义的关键词),以及 user 答过什么
        # 这里做一个简单的"已问 / 已答"统计,辅助 LLM 决定是否再问
        asked_keywords = set()
        for m in history:
            if m.get("role") != "assistant":
                continue
            c = m.get("content", "")
            for kw in ["GMV", "退单", "新客", "老客", "留存", "复购", "漏斗", "转化", "ROI", "维度", "时间", "对比", "渠道"]:
                if kw in c:
                    asked_keywords.add(kw)

        prompt = f"""{self.system}

## 用户的原始问题
{original_query}

## 当前对话历史(可能多轮)
{conversation}

## 累计反问轮次
{round_count}(没有硬上限,可以继续问)

## 之前已问过的关键词
{', '.join(asked_keywords) or '(无)'}

## 你的任务
1. 复述你理解的部分
2. **检查 7 槽位(time_range / dimensions / metrics / comparison / metric_definition / segmentation / filters)哪些还不明确**
3. 关键槽位缺失 → 反问 1-3 个,**不要因为轮次多就放弃追问**
4. 7 槽位都明确(从对话历史可读出) → 输出 phase: "awaiting_confirmation",让 user 显式确认
5. user 已经显式确认("确认/OK/就这样") → phase: "ready",user_confirmed: true
6. 输出严格用以下 JSON 格式:

```json
{{
  "phase": "clarifying" | "awaiting_confirmation" | "ready",
  "reply": "<给用户看的文字,中文,自然对话>",
  "spec": <如果是 awaiting_confirmation 或 ready,填完整规格 JSON;否则填 null>,
  "open_questions": [<还没答清的槽位名列表>],
  "round": {round_count}
}}
```"""

        raw = self.llm.call(self.system, prompt, json_mode=True, temperature=0.2)
        result = safe_json_loads(raw)

        # 兜底:如果 LLM 没按格式来
        if not isinstance(result, dict) or "phase" not in result:
            orig = (history[0]["content"] if history else new_message) or new_message
            # 第一轮就反问关键口径
            if round_count == 0:
                result = {
                    "phase": "clarifying",
                    "reply": f"我理解你想看:{orig}\n\n但有几个口径必须先确认,我不能猜:\n1. 时间窗口?(最近 7/30/90 天?)\n2. 指标怎么算?(含/不含退单?窗口多少天?)\n3. 拆维度吗?(品类/渠道/新客老客?)\n4. 跟谁比?(同比/环比/TopN?)",
                    "spec": None,
                    "open_questions": ["time_range", "metric_definition", "dimensions", "comparison"],
                    "round": round_count,
                }
            else:
                # 第 2 轮+
                # user 是不是"显式确认"
                if self._is_confirm(new_message):
                    # 用上一轮(LLM 给的)spec;如果拿不到,临时造一个
                    last_spec = None
                    for m in reversed(history):
                        if m.get("role") == "assistant":
                            pass
                    # 从 history 找最近一个 spec
                    for m in reversed(history):
                        pass
                    # 兜底:用 _build_spec_from_answer 把上一条 user msg 当作答案
                    last_user = ""
                    for m in reversed(history):
                        if m.get("role") == "user":
                            last_user = m.get("content", "")
                            break
                    spec = self._build_spec_from_answer(orig, last_user, history)
                    spec["user_confirmed"] = True
                    result = {
                        "phase": "ready",
                        "reply": "✅ 口径已确认,开始理解数仓。",
                        "spec": spec,
                        "open_questions": [],
                        "round": round_count,
                    }
                else:
                    is_substantive = self._is_substantive_answer(new_message)
                    if is_substantive:
                        spec = self._build_spec_from_answer(orig, new_message, history)
                        result = {
                            "phase": "awaiting_confirmation",
                            "reply": self._format_spec_for_confirm(spec),
                            "spec": spec,
                            "open_questions": [],
                            "round": round_count,
                        }
                    else:
                        result = {
                            "phase": "clarifying",
                            "reply": f"我还在等你的口径回答。还差:\n{self._list_unclear(new_message, asked_keywords)}\n\n(我不会自己猜,有歧义必须问清)",
                            "spec": None,
                            "open_questions": list(self._unclear_questions(asked_keywords, orig)),
                            "round": round_count,
                        }
        else:
            # 校验 spec.user_confirmed 标记
            spec = result.get("spec")
            if spec and isinstance(spec, dict):
                # 如果 user 答了 "确认/OK/就这样" 这种词,标 user_confirmed: true + phase: ready
                user_text = new_message or ""
                if self._is_confirm(user_text) and not spec.get("user_confirmed"):
                    spec["user_confirmed"] = True
                    if result.get("phase") == "awaiting_confirmation":
                        result["phase"] = "ready"
                elif not spec.get("user_confirmed"):
                    # 默认 false,要求 user 显式确认
                    spec["user_confirmed"] = False
                # 7 槽位兜底
                spec.setdefault("time_range", {"type": "relative", "value": "30d", "grain": "day"})
                spec.setdefault("dimensions", [])
                spec.setdefault("metrics", [])
                spec.setdefault("comparison", [])
                spec.setdefault("metric_definition", {})
                spec.setdefault("segmentation", "")
                spec.setdefault("filters", [])
                spec.setdefault("assumptions", [])
                spec.setdefault("user_confirmed", False)
                spec.setdefault("is_mock_data", True)
        return result

    def _is_confirm(self, text: str) -> bool:
        """user 这句话是不是"显式确认"了"""
        t = (text or "").strip()
        if not t:
            return False
        keywords = ["确认", "OK", "ok", "就这样", "可以", "你看着办", "对的", "对", "yes", "Yes", "YES", "按这个跑", "开干", "go", "Go", "GO"]
        for kw in keywords:
            if kw in t:
                # 但如果有"改"就不算
                if any(mod in t for mod in ["改", "不是", "不对", "换成", "instead"]):
                    return False
                return True
        return False

    def _is_substantive_answer(self, text: str) -> bool:
        """user 的回答是否有实质内容(可以拿来构 spec)"""
        t = (text or "").strip()
        if len(t) < 6:
            return False
        # 含数字
        import re
        if re.search(r'\d', t):
            return True
        # 含业务关键词
        kws = ["天", "品类", "渠道", "同比", "环比", "含", "不含", "新客", "老客", "复购", "留存", "转化", "ROI", "券", "GMV", "退单"]
        return any(kw in t for kw in kws)

    def _build_spec_from_answer(self, original_query, answer, history) -> Dict:
        """从 user 回答里组装 spec(规则化,不需要 LLM)"""
        import re
        q = (original_query or "").lower()
        a = answer or ""

        # time_range
        m = re.search(r'(\d+)\s*天', a)
        if m:
            tr = {"type": "relative", "value": f"{m.group(1)}d", "grain": "day"}
        else:
            tr = {"type": "relative", "value": "30d", "grain": "day", "_assumption": "未明确,默认 30 天"}

        # comparison
        comp = []
        if "同比" in a: comp.append("yoy")
        if "环比" in a: comp.append("mom")
        if "top" in a.lower(): comp.append("top_n")
        if not comp:
            comp = ["yoy", "mom"]
            tr["_assumption"] = tr.get("_assumption", "") + " | 对比默认同比+环比"

        # dimensions
        dims = []
        if "品类" in a or "类目" in a: dims.append("category_l1")
        if "渠道" in a: dims.append("channel")
        if "新客" in a or "老客" in a: dims.append("user_type")
        if not dims and "维度" not in a:
            dims = ["category_l1"]
            tr["_assumption"] = tr.get("_assumption", "") + " | 维度默认按品类"

        # metrics
        metrics = []
        if "复购" in q or "复购" in a:
            window = 30
            m2 = re.search(r'复购[^0-9]*(\d+)\s*天', a)
            if m2: window = int(m2.group(1))
            metrics.append({
                "name": "复购率",
                "definition": f"{window} 天内下过 ≥2 单的用户 / 总下单用户(从回答提取)",
                "unit": "%",
                "user_confirmed_definition": False,
            })
        if "留存" in q or "留存" in a:
            metrics.append({
                "name": "留存率",
                "definition": "cohort 留存(从回答提取窗口)",
                "unit": "%",
                "user_confirmed_definition": False,
            })
        if "roi" in q or "roi" in a or "券" in q:
            metrics.append({
                "name": "券 ROI",
                "definition": "券带动 GMV / 券面额(从回答提取)",
                "unit": "倍",
                "user_confirmed_definition": False,
            })
        if "漏斗" in q or "转化" in q or "漏斗" in a or "转化" in a:
            metrics.append({
                "name": "转化漏斗",
                "definition": "4 步漏斗(浏览→加购→下单→支付)",
                "unit": "%",
                "user_confirmed_definition": False,
            })
        if "客单" in q or "客单" in a:
            metrics.append({
                "name": "客单价",
                "definition": "GMV / 订单量",
                "unit": "元",
                "user_confirmed_definition": False,
            })
        if not metrics:
            # 默认 GMV
            gmv_def = "已支付订单金额,扣减退单"
            if "含退单" in a or "含退" in a:
                gmv_def = "已支付订单金额(含退单,不扣减)"
            elif "不退" in a:
                gmv_def = "已支付订单金额,不含退单"
            metrics.append({
                "name": "GMV",
                "definition": gmv_def,
                "unit": "元",
                "user_confirmed_definition": False,
            })

        # segmentation
        seg = ""
        if "新客按首单" in a or ("新客" in a and "首单" in a):
            seg = "新客 = 首单用户;老客 = 历史下过单"
        elif "新客" in a or "老客" in a:
            seg = "新客/老客 拆分(从回答提取)"

        # filters
        filters = []
        for ex_kw in ["排除", "不看", "不含"]:
            if ex_kw in a:
                idx = a.find(ex_kw)
                filters.append(a[idx:idx+30].split(",")[0].split("。")[0])

        # metric_definition
        mdef = {}
        for m3 in metrics:
            mdef[m3["name"]] = m3["definition"]

        return {
            "requirement_id": f"req_{int(datetime.now().timestamp())}",
            "original_query": original_query,
            "time_range": tr,
            "dimensions": dims,
            "metrics": metrics,
            "comparison": comp,
            "metric_definition": mdef,
            "segmentation": seg or "(未指定,默认全部)",
            "filters": filters,
            "assumptions": [v for k, v in tr.items() if k.startswith("_assumption")] or ["口径从 user 回答提取,可能需调整"],
            "user_confirmed": False,
            "output_preference": "both",
            "is_mock_data": True,
        }

    def _format_spec_for_confirm(self, spec) -> str:
        """格式化 spec 给 user 确认用"""
        lines = ["📋 我整理了一份口径,请确认:\n"]
        lines.append(f"📌 时间: {spec.get('time_range', {}).get('value', '?')}")
        lines.append(f"📊 指标: " + ", ".join(f"{m['name']}({m['definition']})" for m in spec.get('metrics', [])))
        lines.append(f"🔍 维度: {', '.join(spec.get('dimensions', [])) or '(无)'}")
        if spec.get('segmentation') and spec['segmentation'] != "(未指定,默认全部)":
            lines.append(f"👥 分层: {spec['segmentation']}")
        if spec.get('filters'):
            lines.append(f"🚫 过滤: {', '.join(spec['filters'])}")
        if spec.get('comparison'):
            lines.append(f"📈 对比: {', '.join(spec['comparison'])}")
        if spec.get('assumptions'):
            lines.append(f"\n⚠ 默认假设:\n" + "\n".join(f"  - {a}" for a in spec['assumptions']))
        lines.append("\n✅ 回 \"确认\" 开干 / ✏️ \"改 X\" 我重问")
        return "\n".join(lines)

    def _unclear_questions(self, asked_keywords, original_query):
        """根据查询关键词 + 已问关键词,列出还没答清的"""
        q = (original_query or "").lower()
        out = []
        if "时间" not in asked_keywords and "time" not in str(asked_keywords):
            out.append("time_range")
        if "对比" not in asked_keywords and "比" not in str(asked_keywords):
            out.append("comparison")
        # 指标 / 维度 / 口径
        if any(kw in q for kw in ["gmv", "复购", "留存", "roi", "漏斗", "转化", "客单"]):
            if "metric_definition" not in str(asked_keywords) and "退单" not in asked_keywords and "口径" not in asked_keywords:
                out.append("metric_definition")
        if "新客" in q or "老客" in q or "用户" in q:
            if "新客" not in asked_keywords and "老客" not in asked_keywords:
                out.append("segmentation")
        if "维度" not in asked_keywords and "品类" not in asked_keywords and "渠道" not in asked_keywords:
            out.append("dimensions")
        return out

    def _list_unclear(self, last_msg, asked_keywords):
        """给 user 看,还差哪些口径"""
        questions = self._unclear_questions(asked_keywords, last_msg)
        labels = {
            "time_range": "时间窗口",
            "comparison": "对比基准",
            "metric_definition": "指标计算口径(怎么算)",
            "segmentation": "用户/对象分层(新客/老客怎么定义)",
            "dimensions": "拆解维度(品类/渠道等)",
            "filters": "过滤条件(排除什么)",
        }
        return "\n".join(f"- {labels.get(q, q)}" for q in questions) or "(看起来都齐了,你可以显式确认)"

    def _fallback_spec(self, query: str) -> Dict:
        """
        保留兼容,实际不再用 — 改成走"反问+等确认"路径
        """
        q = (query or "").lower()
        if "复购" in q:
            metrics = [{"name": "复购率", "definition": "30 天内下过 ≥2 单的用户 / 总下单用户(待 user 确认)", "unit": "%", "user_confirmed_definition": False}]
        elif "留存" in q:
            metrics = [{"name": "次日留存率", "definition": "注册后第 2 天仍登录用户 / 注册用户(待 user 确认)", "unit": "%", "user_confirmed_definition": False}]
        elif "券" in q or "coupon" in q or "roi" in q:
            metrics = [{"name": "券 ROI", "definition": "券带动 GMV / 券面额(待 user 确认)", "unit": "倍", "user_confirmed_definition": False}]
        elif "漏斗" in q or "转化" in q:
            metrics = [{"name": "转化漏斗", "definition": "4 步漏斗(待 user 确认)", "unit": "%", "user_confirmed_definition": False}]
        else:
            metrics = [{"name": "GMV", "definition": "已支付订单金额,扣减退单(待 user 确认)", "unit": "元", "user_confirmed_definition": False}]
        return {
            "requirement_id": f"req_{int(datetime.now().timestamp())}",
            "original_query": query,
            "time_range": {"type": "relative", "value": "30d", "grain": "day"},
            "dimensions": ["category_l1"],
            "metrics": metrics,
            "comparison": ["yoy", "mom"],
            "metric_definition": {},
            "segmentation": "",
            "filters": [],
            "assumptions": ["默认 30 天", "默认按品类拆", "默认同比+环比", "GMV 默认含退单扣减"],
            "user_confirmed": False,
            "output_preference": "both",
            "is_mock_data": True,
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
