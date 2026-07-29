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

    def add_assistant(self, content: str):
        self.history.append({"role": "assistant", "content": content})
        self.assistant_message = content

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
            pass

        return st

    # ---- 阶段实现 ----

    def _do_clarify(self, st: WorkflowState, user_message: str):
        if user_message:
            st.add_user(user_message)
        result = self.clarifier.parse(st.history, user_message or st.history[0]["content"])
        st.llm_calls += 1
        st.round = result.get("round", st.round)

        phase = result.get("phase")
        if phase == "ready" and result.get("spec"):
            st.spec = result["spec"]
            st.phase = "warehouse_understanding"
            st.add_assistant(result.get("reply", "口径已确认,开始理解数仓。"))
        elif phase == "awaiting_confirmation" and result.get("spec"):
            # 新阶段:等 user 显式确认
            st.spec = result["spec"]
            st.phase = "awaiting_confirmation"
            st.add_assistant(result.get("reply", "请确认口径。"))
        else:
            reply = result.get("reply", "请补充信息")
            st.add_assistant(reply)
            # 还在 clarifying

    def _do_awaiting_confirmation(self, st: WorkflowState, user_message: str):
        """user 看到口径卡,可以确认 / 改某条"""
        if user_message:
            st.add_user(user_message)
        # 再让 clarifier 处理一次:可能是 "确认" → ready,可能是 "改 X" → 回到 clarifying
        result = self.clarifier.parse(st.history, user_message)
        st.llm_calls += 1
        st.round = result.get("round", st.round)

        phase = result.get("phase")
        if phase == "ready" and result.get("spec"):
            st.spec = result["spec"]
            st.phase = "warehouse_understanding"
            st.add_assistant(result.get("reply", "✅ 确认收到,开始理解数仓。"))
        elif phase == "awaiting_confirmation" and result.get("spec"):
            st.spec = result["spec"]
            st.phase = "awaiting_confirmation"
            st.add_assistant(result.get("reply", "已更新口径,请再次确认。"))
        else:
            # user 改某条 → 回到 clarifying
            st.phase = "clarifying"
            st.add_assistant(result.get("reply", "好的,改完再问。"))

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
            st.chart_path = render_echart(st.sql_result, title=title, session_id=st.session_id)
        except Exception as e:
            st.chart_path = None
            print(f"[warn] ECharts 渲染失败: {e}")
        st.phase = "writing_conclusion"

    def _do_conclusion(self, st: WorkflowState):
        st.conclusion = self.writer.write(st.spec, st.sql_result, st.chart_path)
        st.llm_calls += 1
        st.phase = "done"

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
