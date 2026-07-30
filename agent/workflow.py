"""
Agentic Workflow 编排 — 状态机
状态:
  idle → clarifying → awaiting_confirmation → ready → warehouse_understanding
        → sql_generating → executing → visualizing → writing_conclusion → done
        ↘ 任何阶段失败 → error → 可重试
"""
import os
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime

from .agents import (
    RequirementClarifier, WarehouseUnderstander, SQLGenerator, ConclusionWriter
)


def _dict_to_markdown(d: Dict[str, Any]) -> str:
    """
    v0.6.10: dict → markdown 字符串 (auto_extract 按 ## 段切)
    策略: 顶层 key 当段头 (## key), value 字符串化
    - list: bullet list (- item)
    - dict: 嵌套 (## key + 缩进)
    """
    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"\n## {k}\n")
            for item in v:
                if isinstance(item, dict):
                    # list 里的 dict → 嵌套成 sub-bullet
                    lines.append(f"- **{item.get('hypothesis', item.get('name', str(list(item.keys())[0])))}**")
                    for ik, iv in item.items():
                        if ik not in ('hypothesis', 'name'):
                            lines.append(f"  - {ik}: {iv}")
                else:
                    lines.append(f"- {item}")
        elif isinstance(v, dict):
            lines.append(f"\n## {k}\n")
            for ik, iv in v.items():
                lines.append(f"- **{ik}**: {iv}")
        else:
            lines.append(f"\n## {k}\n")
            lines.append(str(v))
    return "\n".join(lines).strip()


from .llm_client import LLMClient, safe_json_loads
from .sql_executor import SQLExecutor
from .chart_renderer import render_echart


class WorkflowState:
    """工作流状态对象"""

    def __init__(self, session_id: str, user_query: str):
        self.session_id = session_id
        self.history: List[Dict] = [{"role": "user", "content": user_query}]
        self.phase = "clarifying"  # 当前阶段
        self.spec: Optional[Dict] = None           # 需求规格
        self.warehouse_plan: Optional[Dict] = None
        self.sql_result: Optional[Dict] = None
        self.sql: Optional[str] = None
        self.chart_path: Optional[str] = None
        self.conclusion: Optional[Dict] = None
        self.assistant_message: str = ""
        self.round = 0
        self.llm_calls = 0
        self.created_at = datetime.now().isoformat()
        self.error: Optional[str] = None
        # v0.6: UI 按钮选项 (clarifying / awaiting_confirmation 时填)
        self.pending_options: List[Dict] = []

    def add_assistant(self, content: str, pending_options: List[Dict] = None):
        self.history.append({"role": "assistant", "content": content})
        self.assistant_message = content
        if pending_options is not None:
            self.pending_options = pending_options

    def add_user(self, content: str):
        self.history.append({"role": "user", "content": content})

    def to_public_dict(self) -> Dict:
        """对外暴露的状态(脱敏)"""
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "round": self.round,
            "llm_calls": self.llm_calls,
            "spec": self.spec,
            "warehouse_plan_summary": {
                "tables": [t["name"] for t in (self.warehouse_plan or {}).get("selected_tables", [])],
                "confidence": (self.warehouse_plan or {}).get("confidence"),
            } if self.warehouse_plan else None,
            "sql": self.sql,
            "sql_result_summary": {
                "ok": (self.sql_result or {}).get("ok"),
                "row_count": (self.sql_result or {}).get("row_count"),
                "columns": (self.sql_result or {}).get("columns"),
                "error": (self.sql_result or {}).get("error"),
            } if self.sql_result else None,
            "chart_path": self.chart_path,
            "conclusion": self.conclusion,
            "assistant_message": self.assistant_message,
            "error": self.error,
        }


class DataAnalystWorkflow:
    """主工作流"""

    def __init__(self, llm: Optional[LLMClient] = None, executor: Optional[SQLExecutor] = None):
        self.llm = llm or LLMClient()
        self.executor = executor or SQLExecutor()
        self.clarifier = RequirementClarifier(self.llm)
        self.understander = WarehouseUnderstander(self.llm)
        self.sql_gen = SQLGenerator(self.llm)
        self.writer = ConclusionWriter(self.llm)
        self.sessions: Dict[str, WorkflowState] = {}

    def new_session(self, user_query: str) -> WorkflowState:
        sid = f"sess_{int(time.time() * 1000)}"
        st = WorkflowState(sid, user_query)
        self.sessions[sid] = st
        return st

    def step(self, session_id: str, user_message: Optional[str] = None) -> WorkflowState:
        """
        单步推进
        """
        st = self.sessions.get(session_id)
        if not st:
            raise ValueError(f"session 不存在: {session_id}")

        if st.error:
            return st

        if st.phase == "clarifying":
            self._do_clarify(st, user_message or "")
        elif st.phase == "awaiting_confirmation":
            # 收到 user 消息,让 clarifier 决定是确认 / 还是改某条
            self._do_awaiting_confirmation(st, user_message or "")
        elif st.phase == "warehouse_understanding":
            self._do_understand(st)
        elif st.phase == "sql_generating":
            self._do_sql(st)
        elif st.phase == "executing":
            self._do_execute(st)
        elif st.phase == "visualizing":
            self._do_visualize(st)
        elif st.phase == "writing_conclusion":
            self._do_conclusion(st)
        elif st.phase == "done":
            # v0.6.9 治本: done 阶段 user 再发 = 开新分析 (重置 session)
            # 旧 bug: SSE 流 yield 上次 assistant_message, user 看到同样回复
            if user_message and user_message.strip():
                # 显式 user 发消息 (不是空 push), 视为新需求
                # 重置 session state
                st.history = [{"role": "user", "content": user_message.strip()}]
                st.spec = None
                st.warehouse_plan = None
                st.sql = None
                st.sql_result = None
                st.chart_path = None
                st.conclusion = None
                st.assistant_message = ""
                st.error = None
                st.round = 0
                st.llm_calls = 0
                st.pending_options = []
                st.phase = "clarifying"
                # 跑一轮 _do_clarify
                self._do_clarify(st, user_message.strip())
            # else: 空消息, 啥也不做 (防止 SSE 流误推老 reply)

        return st

    def reset_session(self, session_id: str, new_query: str) -> WorkflowState:
        """v0.6.9: 显式重置 session 到新 query (新分析)"""
        st = self.sessions.get(session_id)
        if not st:
            raise ValueError(f"session 不存在: {session_id}")
        st.history = [{"role": "user", "content": new_query.strip()}]
        st.spec = None
        st.warehouse_plan = None
        st.sql = None
        st.sql_result = None
        st.chart_path = None
        st.conclusion = None
        st.assistant_message = ""
        st.error = None
        st.round = 0
        st.llm_calls = 0
        st.pending_options = []
        st.phase = "clarifying"
        self._do_clarify(st, new_query.strip())
        return st

    # ---- 阶段实现 ----

    def _do_clarify(self, st: WorkflowState, user_message: str):
        if user_message:
            st.add_user(user_message)
        result = self.clarifier.parse(st.history, user_message or st.history[0]["content"])
        st.llm_calls += 1
        st.round = result.get("round", st.round)

        phase = result.get("phase")
        pending_opts = result.get("pending_options", [])
        spec = result.get("spec")
        # v0.6.5 治本: 不信 LLM 跳 confirmation, spec 完整 + user 没显式确认 → 强制 awaiting_confirmation
        if spec and isinstance(spec, dict):
            # user 显式确认了 ("确认" / "OK" / "就这样" / button value 包含 "确认") → 走 ready
            user_explicitly_confirmed = self.clarifier._is_confirm(user_message or "") or "确认" in (user_message or "")
            # LLM 标 user_confirmed 不算, 必须 user 显式
            if phase == "ready" and user_explicitly_confirmed:
                spec["user_confirmed"] = True
                st.spec = spec
                st.phase = "warehouse_understanding"
                st.add_assistant(result.get("reply", "口径已确认,开始理解数仓。"), pending_options=[])
                return
            elif phase == "ready" or (spec and spec.get("user_confirmed")):
                # spec 完整但 user 没点确认 → 强制 awaiting_confirmation
                st.spec = spec
                spec["user_confirmed"] = False  # 强制覆盖 LLM
                confirm_opts = self.clarifier._build_pending_options("awaiting_confirmation", [], spec)
                st.phase = "awaiting_confirmation"
                rc = self.clarifier
                confirm_reply = rc._format_spec_for_confirm(spec)
                st.add_assistant(confirm_reply, pending_options=confirm_opts)
                return
        if phase == "awaiting_confirmation" and result.get("spec"):
            # 新阶段:等 user 显式确认
            st.spec = result["spec"]
            st.phase = "awaiting_confirmation"
            st.add_assistant(result.get("reply", "请确认口径。"), pending_options=pending_opts)
        else:
            reply = result.get("reply", "请补充信息")
            st.add_assistant(reply, pending_options=pending_opts)
            # 还在 clarifying

    def _do_awaiting_confirmation(self, st: WorkflowState, user_message: str):
        """user 看到口径卡,可以确认 / 改某条"""
        if user_message:
            st.add_user(user_message)
        user_text = (user_message or "").strip()
        # v0.6.7 治本: 区分 3 种意图
        # 1. 显式确认 ("确认" / "OK" / "就这样" / button value "确认") → 走 ready
        if self.clarifier._is_confirm(user_text) or "确认" in user_text:
            st.spec["user_confirmed"] = True
            st.phase = "warehouse_understanding"
            st.add_assistant("✅ 确认收到,开始理解数仓。", pending_options=[])
            return
        # 2. 显式说改某条 ("改 X" / "换成 Y" / "不是 Z") → 走 clarifying
        if any(kw in user_text for kw in ["改", "换成", "不是", "别的", "instead", "不是这个", "换个", "修改", "改一下"]):
            result = self.clarifier.parse(st.history, user_message)
            st.llm_calls += 1
            st.round = result.get("round", st.round)
            st.phase = "clarifying"
            st.add_assistant(result.get("reply", "好的, 改完再问。"), pending_options=result.get("pending_options", []))
            return
        # 3. 其他 (button value / 模糊话) → 不动, 强制再走 awaiting_confirmation
        # 不调 clarifier (避免 LLM 误判 spec 改了), 简单提示 user 明确说改 / 确认
        spec = st.spec or {}
        st.phase = "awaiting_confirmation"
        confirm_opts = self.clarifier._build_pending_options("awaiting_confirmation", [], spec)
        st.add_assistant(
            "我没动你的口径, 因为你说的不是 '确认' 也不是 '改某条'。"
            "请点 ✅ 确认开干, 或明确说'改时间' / '改维度' 等。",
            pending_options=confirm_opts,
        )
        return

    def _do_understand(self, st: WorkflowState):
        st.warehouse_plan = self.understander.understand(st.spec)
        st.llm_calls += 1
        st.phase = "sql_generating"

    def _do_sql(self, st: WorkflowState):
        sql_obj = self.sql_gen.generate(st.spec, st.warehouse_plan)
        st.llm_calls += 1
        st.sql = sql_obj.get("sql", "")
        st.phase = "executing"

    def _do_execute(self, st: WorkflowState):
        result = self.executor.execute(st.sql)
        if not result.get("ok"):
            sql_obj_fb = self.sql_gen.generate(st.spec, st.warehouse_plan)
            st.llm_calls += 1
            fb = sql_obj_fb.get("fallback_sql")
            if fb:
                result = self.executor.execute(fb)
                if result.get("ok"):
                    st.sql = f"-- [fallback]\n{fb}"
        st.sql_result = result
        if not result.get("ok"):
            st.error = f"SQL 执行失败: {result.get('error')}"
            st.phase = "error"
        else:
            st.phase = "visualizing"

    def _do_visualize(self, st: WorkflowState):
        metrics_names = [m.get("name", "") for m in (st.spec or {}).get("metrics", [])]
        title = " / ".join(metrics_names) if metrics_names else "数据结果"
        try:
            # ECharts: 返 /api/echart/<session_id> 路径
            # v0.6.12: 传 spec 让 chart_renderer 决定图类型 (grain=day → line, 推 fallback)
            st.chart_path = render_echart(st.sql_result, title=title, session_id=st.session_id, spec=st.spec)
        except Exception as e:
            st.chart_path = None
            print(f"[warn] ECharts 渲染失败: {e}")
        st.phase = "writing_conclusion"

    def _do_conclusion(self, st: WorkflowState):
        st.conclusion = self.writer.write(st.spec, st.sql_result, st.chart_path)
        st.llm_calls += 1
        st.phase = "done"
        # v0.5: 自动整理 BI 报告关键洞察入 KB (faiss + FTS5), 失败不抛
        try:
            from . import auto_extract
            # v0.6.10 治本: writer.write 返 dict (BI 报告 JSON), 但 auto_extract
            # 期望 markdown 字符串 (.strip() 才能用). 之前传 dict 报
            # "'dict' object has no attribute 'strip'", added=0, 假装写但没真写.
            # 治本: dict → markdown 字符串 (用 json.dumps + ## 段头格式, 让 auto_extract 能按段切)
            if isinstance(st.conclusion, dict):
                conclusion_md = _dict_to_markdown(st.conclusion)
            else:
                conclusion_md = st.conclusion or ""
            added, skipped = auto_extract.auto_extract_to_kb(
                conclusion_md,
                spec=st.spec,
                session_id=st.session_id,
            )
            if added:
                st.add_assistant(
                    f"\n\n---\n💾 **已自动入知识库** {added} 条洞察 (下次 query RAG 自动召回, 标记 [auto] 区分手动/自动)"
                )
        except Exception as e:
            # 治本: auto_extract 失败不阻断流程, 只 log
            import logging
            logging.getLogger(__name__).warning(f"auto_extract 失败, 不影响结论: {e}")

    def run_through(self, session_id: str) -> WorkflowState:
        """一路跑到 done(在 ready 后)"""
        st = self.sessions[session_id]
        max_steps = 20
        while st.phase not in ("done", "error", "clarifying", "awaiting_confirmation") and max_steps > 0:
            self.step(session_id)
            max_steps -= 1
        return st

    def get_state(self, session_id: str) -> WorkflowState:
        return self.sessions[session_id]
