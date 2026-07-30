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
from .knowledge_base import get_kb

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

        # v0.2 RAG 召回 — 在调 LLM 之前, 把 KB 相关历史拼到 prompt 末尾
        rag_query = original_query
        rag_context = get_kb().recall_for_context(rag_query, limit=5)
        if rag_context:
            prompt = prompt + "\n\n---\n\n" + rag_context + "\n\n(以上为知识库相关历史, 仅作参考, 不要直接抄, 跟当前需求不符可忽略)"

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
            # 兜底 open_questions 字段 (LLM 没填时根据 spec 反推)
            if "open_questions" not in result or not result["open_questions"]:
                spec = result.get("spec")
                if spec and isinstance(spec, dict):
                    # 已给 spec 但 open_questions 空, 推算还有啥没答
                    needed = []
                    if not spec.get("time_range") or spec.get("time_range", {}).get("_assumption"):
                        needed.append("time_range")
                    if not spec.get("dimensions"):
                        needed.append("dimensions")
                    if not spec.get("metrics"):
                        needed.append("metric_definition")
                    if not spec.get("comparison"):
                        needed.append("comparison")
                    if not spec.get("segmentation") or spec.get("segmentation") == "(未指定,默认全部)":
                        needed.append("segmentation")
                    result["open_questions"] = needed
                else:
                    result["open_questions"] = []
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
        # v0.6: 计算 pending_options (UI 按钮列表)
        # v0.6.2 治本: 用 hardcoded 7-slot check 覆盖 LLM 返的 open_questions
        # (LLM 老分轮问, user 体验灾难, 这里强制 7 槽位全列出)
        spec = result.get("spec")
        if result.get("phase") == "clarifying":
            computed_slots = self._compute_open_slots(spec)
            # 优先用 hardcoded (7 槽位全), 覆盖 LLM 返的 open_questions
            result["open_questions"] = computed_slots
            result["pending_options"] = self._build_pending_options("clarifying", computed_slots, spec)
        elif result.get("phase") == "awaiting_confirmation":
            result["pending_options"] = self._build_pending_options("awaiting_confirmation", [], spec)
        else:
            result["pending_options"] = []
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

    def _compute_open_slots(self, spec) -> list:
        """
        v0.6.2: 不信 LLM 决定问啥, hardcoded 7-slot 检查 spec 缺啥
        返回: [slot_name, ...] 按 7 槽位固定顺序
        """
        if not spec or not isinstance(spec, dict):
            # 初始没有任何 spec
            return ["time_range", "metrics", "metric_definition", "dimensions", "comparison", "segmentation", "filters"]
        open_slots = []
        # 1. time_range: 必填, value 不能含 _assumption
        tr = spec.get("time_range") or {}
        if not tr or not tr.get("value") or tr.get("_assumption") or tr.get("value", "").endswith("d") is False and not tr.get("value", "").startswith("20"):
            if not tr.get("value") or tr.get("_assumption"):
                open_slots.append("time_range")
        # 2. metrics: 必填
        if not spec.get("metrics"):
            open_slots.append("metrics")
        # 3. metric_definition: 必填, 每个 metric 都要有 definition
        mdef = spec.get("metric_definition") or {}
        metrics = spec.get("metrics") or []
        if not mdef or len(mdef) < len(metrics) or any(not v for v in mdef.values()):
            open_slots.append("metric_definition")
        # 4. dimensions: 没说就问
        if not spec.get("dimensions"):
            open_slots.append("dimensions")
        # 5. comparison: 没说就问
        if not spec.get("comparison"):
            open_slots.append("comparison")
        # 6. segmentation: 涉及"用户/新老客/渠道"才问
        orig = (spec.get("original_query", "") or "").lower()
        if any(kw in orig for kw in ["新客", "老客", "用户", "会员", "留存", "复购", "拉新", "激活", "客群"]):
            if not spec.get("segmentation") or spec.get("segmentation") == "(未指定,默认全部)":
                open_slots.append("segmentation")
        # 7. filters: 默认空 (不强制问, 但要确认 user 不需要)
        if "filters" not in spec:
            open_slots.append("filters")
        return open_slots

    def _build_pending_options(self, phase: str, open_questions: list, spec=None) -> list:
        """
        v0.6.2: 给 server 返 pending_options, UI 直接渲染 button 列表
        - clarifying: 列齐 7 槽位里所有缺的, 每个 slot 给 2 个最相关 (7×2+1=15, 不刷屏)
        - awaiting_confirmation: [确认, 修改, 跳过] 3 个
        - ready: [] (无, user 应该已经确认)
        """
        opts = []
        if phase == "clarifying":
            # v0.6.2 治本: 列齐所有缺 (不只 3 个)
            option_map = {
                "time_range": [
                    ("📅 最近 30 天 (默认)", "30天"),
                    ("📅 最近 7 天", "7天"),
                ],
                "metrics": [
                    ("📊 GMV (销售额)", "GMV"),
                    ("📊 订单量", "订单量"),
                ],
                "metric_definition": [
                    ("💰 不含退单 (默认)", "不含退单"),
                    ("💰 含退单", "含退单"),
                ],
                "dimensions": [
                    ("📂 按品类拆", "按品类"),
                    ("📂 不拆维度 (汇总)", "不拆维度"),
                ],
                "comparison": [
                    ("📈 同比 (vs 去年)", "同比"),
                    ("📈 不对比 (裸趋势)", "不对比"),
                ],
                "segmentation": [
                    ("👥 不分层 (全部)", "不分层"),
                    ("👥 新客 (首单)", "新客按首单"),
                ],
                "filters": [
                    ("🚫 不额外过滤 (默认)", "不过滤"),
                    ("🚫 排除测试", "排除测试"),
                ],
            }
            seen = set()
            # 一次列所有缺 (不限 3)
            for q in (open_questions or []):
                for label, val in option_map.get(q, []):
                    if val in seen:
                        continue
                    seen.add(val)
                    opts.append({
                        "kind": "select",
                        "question": q,
                        "label": label,
                        "value": val,
                    })
            # 永远加一个"自由输入"按钮 (放最后)
            opts.append({
                "kind": "free_input",
                "label": "✍️ 自由回答",
                "value": None,
            })
        elif phase == "awaiting_confirmation":
            opts = [
                {"kind": "confirm", "label": "✅ 确认,开干", "value": "确认"},
                {"kind": "edit", "label": "✏️ 修改某条", "value": None},
                {"kind": "free_input", "label": "💬 补充说明", "value": None},
            ]
        # ready / 其他: [] 不返
        return opts

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

        # v0.3 RAG 召回 — 写 SQL 前先去 KB 找历史类似的 SQL 模板/口径
        # 跟 Clarify/Conclude 一样, 调 LLM 前 recall_for_context
        rag_query = (spec or {}).get("original_query", "") + " " + " ".join(
            (m.get("name", "") for m in (spec or {}).get("metrics", []))
        )
        rag_context = get_kb().recall_for_context(rag_query, limit=5)
        if rag_context:
            user = user + "\n\n---\n\n" + rag_context + "\n\n(以上为知识库相关历史 SQL 模板/口径, 可参考复用 SQL 模式, 但不要直接抄, 跟当前数仓/口径不符可忽略)"

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
    """Agent 04: 结论撰写 — BI 商业洞察版,接入 bi-analyst skill"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system = load_prompt("04_conclusion_writer")
        # 加载 bi-analyst skill 内容
        skill_path = os.path.join(os.path.dirname(__file__), "..", ".skills", "bi-analyst-SKILL.md")
        if not os.path.exists(skill_path):
            skill_path = os.path.join(os.path.dirname(__file__), "..", "..", ".skills", "bi-analyst", "SKILL.md")
        self.skill = ""
        if os.path.exists(skill_path):
            with open(skill_path) as f:
                self.skill = f.read()
        # 业务场景背景知识
        self.business_contexts = self._load_business_contexts()

    def _load_business_contexts(self) -> Dict:
        """业务场景背景知识 — mock 数仓有但 agent 不知道的"业务背景"信息"""
        return {
            "seasonal": {
                "618": "6 月 1-18 日,平台级大促月,行业 GMV 普遍涨 30-50%,需要同比看淡季 vs 大促月",
                "双11": "11 月 11 日,年度最大促,GMV 涨 100%+",
                "春节": "1-2 月,电商淡季(物流/用户活跃下降),整体 GMV 跌 20-30%",
                "Q1 淡季": "3-4 月,节后效应,服装/3C 跌幅明显",
                "Q3 平稳": "7-9 月,相对平稳,无重大促销",
            },
            "category_dynamics": {
                "服饰": "季节性强,Q1 换季/Q2 夏装/Q4 冬装,大促月尤其敏感",
                "3C数码": "客单价高,新客少但客单高,大促能拉新客",
                "美妆": "复购率高(月 30-50%),大促囤货,客单提升明显",
                "食品": "高频低客单,新客驱动,节日(端午/中秋)有品类高峰",
                "家电": "低频高客单,大促(618/双11)集中购买,Q1-Q3 淡季",
            },
            "channel_dynamics": {
                "APP": "主战场,流量最大,新客老客都有",
                "H5": "营销落地页,新客为主,转化高但复购弱",
                "微信小程序": "私域,老客为主,复购率最高",
                "PC": "3C/家电等大件,客单高但流量下降",
            },
            "user_dynamics": {
                "新客": "拉新预算驱动,1 个月内转化难,需关注 7/30 日留存",
                "老客": "复购驱动,占 GMV 60-80%,客单稳定但增长空间有限",
                "高价值用户": "前 10% 用户贡献 40-50% GMV,需要单独运营策略",
            },
            "mock_data_disclaimer": [
                "本数据为 mock 合成(2025-01-01 ~ 2026-07-31),不代表真实业务",
                "mock 用 uniform 分布生成,真实业务尾部长尾(80/20)更明显",
                "mock 无大促事件/无竞品数据/无流量来源细分,业务背景靠 agent 经验",
                "结论仅供参考,真实决策需用线上真实数仓",
            ],
        }

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
                # 算一些基础统计
                "numeric_stats": self._calc_basic_stats(rows, cols),
            }
        else:
            data_summary = {"empty": True, "error": sql_result.get("error", "no rows")}

        # 决定业务场景背景(根据 spec)
        relevant_context = self._pick_relevant_context(spec)

        user = f"""## 需求
{json.dumps(spec, ensure_ascii=False, indent=2)}

## SQL 执行结果
{json.dumps(data_summary, ensure_ascii=False, indent=2)}

## 图表路径
{chart_path or '未生成'}

## 业务场景背景(可能相关)
{json.dumps(relevant_context, ensure_ascii=False, indent=2)}

## bi-analyst skill 思维框架(必用)
- 5 段式:业务背景 + 核心数字 + 异常信号 + 可能原因 + 可执行建议
- 数字精确,不四舍五入
- 至少 2-3 个可能原因,每个含支持证据 + 反证
- 每个建议含:动作 + 量化目标 + 责任方 + 时间窗口
- 必标 mock 数据局限

请按 system prompt 的 JSON schema 输出完整 BI 洞察报告。"""

        # v0.2 RAG 召回 — 跟 ConclusionWriter 看历史洞察 / 模板, 复用 + 避免重复
        rag_query = (spec or {}).get("original_query", "") + " " + " ".join(
            (m.get("name", "") for m in (spec or {}).get("metrics", []))
        )
        rag_context = get_kb().recall_for_context(rag_query, limit=5)
        if rag_context:
            user = user + "\n\n---\n\n" + rag_context + "\n\n(以上为知识库相关历史结论, 可参考 + 引用, 但不要直接抄, 跟当前数据/场景不符可忽略)"

        raw = self.llm.call(self.system, user, json_mode=True, temperature=0.5)
        result = safe_json_loads(raw)
        if not isinstance(result, dict) or "summary" not in result:
            # 兼容 deepseek 返的 {"report": [{section, content}]} 格式 — 解析出 contrarian_views
            report_list = result.get("report") if isinstance(result, dict) else None
            if isinstance(report_list, list) and report_list:
                return self._normalize_report_format(report_list, sql_result, spec, chart_path)
            return self._bi_fallback(sql_result, spec, chart_path)
        if chart_path:
            result["chart_path"] = chart_path
        return result

    def _normalize_report_format(self, report_list: list, sql_result: dict, spec: dict, chart_path: Optional[str]) -> Dict:
        """
        兼容 deepseek 等 LLM 返的 {"report": [{section, content}]} 自由格式
        解析出 6 段式 BI 报告的 schema 字段
        """
        sections_map = {}
        for item in report_list:
            if not isinstance(item, dict):
                continue
            sec = (item.get("section") or "").strip()
            content = (item.get("content") or "").strip()
            if sec and content:
                sections_map[sec] = content

        # 6 段式 schema 拼回去
        def _split_to_list(text: str) -> list:
            """按数字编号 / 换行 / 句号拆 list"""
            if not text:
                return []
            # 优先按 "1. xxx" "2. xxx" 拆
            import re
            parts = re.split(r"\n?\s*\d+[\.、]\s+", text)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) > 1:
                return parts
            # 否则按句号拆
            parts = [p.strip() for p in re.split(r"[。\n]+", text) if p.strip()]
            return parts

        result = {
            "summary": sections_map.get("业务背景", "")[:200] + " | " + sections_map.get("关键数字", "")[:200],
            "business_background": sections_map.get("业务背景", ""),
            "key_findings": _split_to_list(sections_map.get("关键数字", "")) or _split_to_list(sections_map.get("关键发现", "")),
            "abnormal_signals": _split_to_list(sections_map.get("异常", "")) or _split_to_list(sections_map.get("异常信号", "")),
            "possible_causes": [],  # 自由格式下不确定结构, 留空
            "contrarian_views": _split_to_list(sections_map.get("反方意见", "")) or _split_to_list(sections_map.get("反方", "")) or [
                "⚠️ LLM 走自由格式输出, 反方意见未明确 — 建议用 schema 模式重跑"
            ],
            "business_recommendations": _split_to_list(sections_map.get("可执行建议", "")) or _split_to_list(sections_map.get("建议", "")),
            "data_limitations": [
                "⚠️ LLM 走自由格式输出, 部分字段 (possible_causes 结构化) 无法提取, 已用 fallback",
            ],
            "chart_caption": "图表展示了主要指标的横向对比",
            "follow_up_questions": [
                "想下钻到具体维度吗?",
                "想对比不同时间窗口(7/30/90 天)吗?",
            ],
        }
        if chart_path:
            result["chart_path"] = chart_path
        return result

    def _calc_basic_stats(self, rows, cols):
        """从数据算基础统计 — 给 LLM 用"""
        stats = {}
        for c in cols:
            vals = [r.get(c) for r in rows if isinstance(r.get(c), (int, float))]
            if not vals:
                continue
            stats[c] = {
                "max": max(vals),
                "min": min(vals),
                "sum": sum(vals),
                "avg": sum(vals) / len(vals) if vals else 0,
            }
        return stats

    def _pick_relevant_context(self, spec):
        """从 spec 抽出相关的业务背景"""
        context = {}
        q = json.dumps(spec, ensure_ascii=False).lower()

        # 类目相关
        for cat in self.business_contexts["category_dynamics"]:
            if cat in q:
                context.setdefault("category", {})[cat] = self.business_contexts["category_dynamics"][cat]
        # 渠道相关
        for ch in self.business_contexts["channel_dynamics"]:
            if ch in q:
                context.setdefault("channel", {})[ch] = self.business_contexts["channel_dynamics"][ch]
        # 用户分层相关
        for u in self.business_contexts["user_dynamics"]:
            if u in q:
                context.setdefault("user", {})[u] = self.business_contexts["user_dynamics"][u]
        # 季节相关(根据时间范围推)
        tr = spec.get("time_range", {})
        val = tr.get("value", "") if isinstance(tr, dict) else ""
        if "30d" in val or "最近 30" in val or "30 天" in val:
            # 默认加一个一般性背景
            context.setdefault("seasonal", "当前分析最近 30 天,需考虑 7-8 月通常是电商平稳期(无大促),对比 6 月 618 大促月可能下滑属正常")

        # 必带 mock 局限
        context["mock_data_disclaimer"] = self.business_contexts["mock_data_disclaimer"]
        return context

    def _bi_fallback(self, sql_result, spec, chart_path) -> Dict:
        """5 段式 BI fallback — 当 LLM 不工作或返的格式不对时使用,基于数据生成智能洞察"""
        rows = sql_result.get("rows", [])
        cols = sql_result.get("columns", [])

        # 算核心数字
        if rows and cols:
            num_cols = [c for c in cols if any(isinstance(r.get(c), (int, float)) for r in rows)]
            if num_cols:
                main_col = num_cols[0]
                vals = [r.get(main_col) for r in rows if isinstance(r.get(main_col), (int, float))]
                total = sum(vals) if vals else 0
                avg = total / len(vals) if vals else 0
                max_row = max(rows, key=lambda r: r.get(main_col) or 0) if rows else {}
                min_row = min(rows, key=lambda r: r.get(main_col) or 0) if rows else {}
                # 集中度(Top 1 占比)
                top1_pct = (max_row.get(main_col, 0) / total * 100) if total > 0 else 0
                # max/min 倍数
                ratio = (max_row.get(main_col, 0) / min_row.get(main_col, 1)) if min_row.get(main_col, 0) else 0
            else:
                main_col = cols[0] if cols else "value"
                total = len(rows)
                avg = 0
                max_row = rows[0] if rows else {}
                min_row = rows[-1] if rows else {}
                top1_pct = 0
                ratio = 0
        else:
            main_col = "value"
            total = 0
            avg = 0
            max_row = {}
            min_row = {}
            top1_pct = 0
            ratio = 0

        metrics_names = [m.get("name", "") for m in (spec or {}).get("metrics", [])]
        main_metric = metrics_names[0] if metrics_names else main_col

        # 找分类列(非数值的第 1 列)
        cat_col = None
        for c in cols:
            if c not in (num_cols if num_cols else []):
                cat_col = c
                break

        max_cat = max_row.get(cat_col, "Top 项") if cat_col else "Top 项"
        min_cat = min_row.get(cat_col, "底部项") if cat_col else "底部项"

        # ===== 智能判断异常信号 =====
        abnormal = []
        if top1_pct > 50:
            abnormal.append(f"⚠️ {max_cat} 单项占 {top1_pct:.1f}%,超过 50% 阈值,有集中度风险(鸡蛋放一个篮子)")
        elif top1_pct > 30:
            abnormal.append(f"⚠️ {max_cat} 占比 {top1_pct:.1f}%,需关注业务是否过度依赖")
        if ratio > 3 and len(rows) > 2:
            abnormal.append(f"⚠️ 最大/最小差距 {ratio:.1f} 倍,数据显著不均,长尾贡献度低")
        if total == 0:
            abnormal.append("⚠️ 没拿到数据,可能 SQL 过滤条件太严 / 时间窗无数据")

        # 如果差异很小(< 30%),标"基本均匀"
        if not abnormal:
            abnormal.append(f"📊 各项 {main_metric} 分布相对均匀(Top 1 占 {top1_pct:.1f}%,最高/最低 {ratio:.2f} 倍),无明显异常")

        # ===== 业务建议 — 智能判断 =====
        recs = []
        if top1_pct > 50:
            recs.append(f"1. 1 个月内(责任人:运营 + 商品)对 {max_cat} 拆 L2 看是否过度集中,考虑扶持次大类分散风险,目标 Top 1 占比降到 40% 以下")
        elif top1_pct > 30:
            recs.append(f"1. 2 周内(责任人:运营)评估 {max_cat} 主导地位的可持续性,扶持 2-3 个二级梯队类目,目标 Top 1 占比降至 25% 以下")
        else:
            recs.append(f"1. 持续观察各类目表现,当前 {max_cat} 略领先但未到集中度警戒线,保持现有运营节奏")

        if ratio > 3 and len(rows) > 2:
            recs.append(f"2. 1 个月内(责任人:运营)评估 {min_cat} 投入产出比,如果持续低贡献考虑资源回收,目标整体资源效率提升 10%")
        else:
            recs.append(f"2. 关注 {min_cat} 是否有结构性原因(季节性/小众品类),不要盲目砍掉,建议拆月度看趋势")

        recs.append(f"3. 下一步下钻到 L2 类目 + 时间序列,看 {max_cat} 的领先是结构性还是偶发性")

        return {
            "summary": f"📊 {main_metric} 总量 {total:,.0f}(基于 {len(rows)} 行数据)。{max_cat} 最高 {max_row.get(main_col, 0):,.0f}(占 {top1_pct:.1f}%),{min_cat} 最低 {min_row.get(main_col, 0):,.0f}。{abnormal[0]}",
            "business_background": f"最近 30 天是电商平稳期(7-8 月无 618/双11 大促月),整体行业 GMV 通常比 6 月/11 月低 20-30%。本次数据为 mock 合成,不代表真实业务;真实业务需用线上数仓验证。",
            "key_findings": [
                f"📊 {main_metric} 合计 {total:,.2f},平均每 {cat_col or '项'} {avg:,.2f}",
                f"🔝 最高 {max_cat}: {max_row.get(main_col, 0):,.2f}(占 {top1_pct:.1f}%)",
                f"🔻 最低 {min_cat}: {min_row.get(main_col, 0):,.2f}(占 {(min_row.get(main_col, 0) / total * 100) if total else 0:.1f}%)",
            ],
            "abnormal_signals": abnormal,
            "possible_causes": [
                {
                    "hypothesis": "假设 1:季节性 / 大促周期影响",
                    "evidence": "30 天如果是 7-8 月,行业通常处于平稳期(无 618/双11 那种大促月),整体 GMV 比大促月低 20-30% 属正常",
                    "counter_evidence": "数据为 mock,无大促事件标记,无法直接验证"
                },
                {
                    "hypothesis": "假设 2:类目/渠道结构变化",
                    "evidence": f"不同类目在不同时点表现差异大({max_cat} 略领先),需下钻到 L2 渠道级看是否某个细分品类异动",
                    "counter_evidence": "mock 数据为均匀分布,真实业务可能尾部分布更明显(80/20),模拟不能体现真实结构"
                },
                {
                    "hypothesis": "假设 3:用户分层变化(新客/老客)",
                    "evidence": "如果新客/老客比例变化,GMV 组成会变(老客复购驱动 vs 新客拉新驱动),需拆 user_type 维度",
                    "counter_evidence": "本次未拆 user_type 维度,需进一步下钻"
                },
            ],
            "business_recommendations": recs,
            "data_limitations": [
                "⚠️ 数据为 mock 合成(2025-01-01 ~ 2026-07-31),不代表真实业务",
                "⚠️ mock 用 uniform 分布生成,真实业务尾部分布更明显(80/20 法则)",
                "⚠️ mock 无大促事件/无竞品数据,业务背景靠 agent 经验,可能不准",
                "⚠️ 同比/环比基准可能为 NULL(去年同一天无数据),已用 COALESCE 兜底",
                "⚠️ 行业 benchmark(电商平稳期 GMV 跌 20-30%)为经验值,非权威数据",
            ],
            "chart_caption": f"图表展示了 {len(rows)} 个 {cat_col or '维度'} 的 {main_metric} 横向对比,数字已标注在条形末端",
            "follow_up_questions": [
                f"想下钻到 {max_cat} 的 L2 子类目看细分吗?",
                "想看新客/老客拆分吗?",
                "想对比去年同期(2025 同期)吗?",
                "想按 channel 渠道再拆一层吗?",
            ],
        }
