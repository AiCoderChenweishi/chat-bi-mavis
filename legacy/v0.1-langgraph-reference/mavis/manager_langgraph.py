"""
v1.0.15 manager_agent 用 LangGraph 重写

State machine:
  START → plan → dispatch_worker → run_worker → check_status → (loop | mark_done) → END

State: {tasks, log, iteration, is_done, final_deliverable, form}
"""

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, TypedDict
from langgraph.graph import StateGraph, START, END


class ManagerState(TypedDict, total=False):
    """LangGraph state — 共享 state"""
    tasks: List[Dict]              # task list (跟 TaskBoard 一致)
    log: List[Dict]                # manager log
    iteration: int                 # 循环次数
    is_done: bool                  # wf 跑完?
    final_deliverable: Optional[Dict]  # final report
    form: Dict                     # user form
    pending_question: Optional[str]   # 问 user 的问题
    plan_recommended_count: int    # plan_recommended 数
    total_placeholders: int        # 占位符总数


def make_state(form: Dict, initial_plan: List[Dict]) -> ManagerState:
    """初始化 state"""
    tasks = []
    for step in initial_plan:
        # v1.0.15 fix: 用 plan 里的 id (s1/s2) 作为 task id, depends_on 才能正确匹配
        step_id = step.get("id") or f"step-{len(tasks)+1}"
        tasks.append({
            "id": step_id,
            "role": step.get("agent", "dba"),
            "name": step.get("name", step_id),
            "task": step.get("goal") or step.get("task") or step.get("name", ""),
            "depends_on": step.get("depends_on", []),
            "plan_recommended": True,
            "status": "pending",
            "duration_sec": 0.0,
            "result": None,
            "output": None,
            "report": "",
            "retry_count": 0,
        })
    if not tasks:
        # fallback: 1 个 PM 任务
        tasks.append({
            "id": "t-pm-1",
            "role": "pm",
            "name": "需求理解",
            "task": f"理解 user 需求: {form.get('title', '')}",
            "depends_on": [],
            "plan_recommended": True,
            "status": "pending",
            "duration_sec": 0.0,
        })
    return {
        "tasks": tasks,
        "log": [],
        "iteration": 0,
        "is_done": False,
        "final_deliverable": None,
        "form": form,
        "pending_question": None,
        "plan_recommended_count": len(tasks),
        "total_placeholders": 0,
    }


# === Node functions ===

async def node_dispatch(state: ManagerState) -> ManagerState:
    """派下一个 ready task (plan_recommended 优先, depends_on 顺序)"""
    state["iteration"] += 1

    # 找 ready task
    done_ids = {t["id"] for t in state["tasks"] if t["status"] == "done"}
    ready = [t for t in state["tasks"] if t["status"] == "pending" and all(d in done_ids for d in (t.get("depends_on") or []))]
    ready.sort(key=lambda t: (not t.get("plan_recommended", False), t["id"]))

    if not ready:
        # 没 ready task, 走 mark_done
        state["log"].append({"type": "system", "msg": "无 ready task, 进入 mark_done"})
        return state

    # 派第一个 ready task
    task = ready[0]
    task["status"] = "running"
    task["dispatched_at"] = time.time()
    state["log"].append({
        "type": "tool_call",
        "tool": "dispatch_task",
        "args": {"role": task["role"], "name": task["name"], "task_id": task["id"]},
    })
    return state


async def node_run_worker(state: ManagerState) -> ManagerState:
    """跑 running task, 调 worker, 写回 result"""
    running = [t for t in state["tasks"] if t["status"] == "running"]
    if not running:
        return state

    task = running[0]
    from .agent_runner import run_agent, extract_final_data
    from .agents_registry import AGENT_REGISTRY

    agent_cfg = AGENT_REGISTRY.get(task["role"], {})

    # 收集所有 done task 的产出 (v1.0.14 多智能体协作)
    prior_outputs = []
    for t_other in state["tasks"]:
        if t_other["id"] == task["id"] or t_other["status"] != "done":
            continue
        out_summary = ""
        if t_other.get("output") and isinstance(t_other["output"], dict):
            content_v = t_other["output"].get("content", "")
            if isinstance(content_v, str) and content_v.strip():
                out_summary = content_v[:1500]
        prior_outputs.append({
            "id": t_other["id"],
            "name": t_other["name"],
            "role": t_other["role"],
            "report": (t_other.get("report") or "")[:500],
            "output": out_summary,
            "is_direct_dep": t_other["id"] in (task.get("depends_on") or []),
        })

    direct_deps = [p for p in prior_outputs if p["is_direct_dep"]]
    other_priors = [p for p in prior_outputs if not p["is_direct_dep"]]

    user_task = f"任务名: {task['name']}\n任务描述: {task['task']}\n"
    if direct_deps:
        user_task += "\n=== 你直接依赖的前序 agent 产出 (必读) ===\n"
        for p in direct_deps:
            user_task += f"\n--- [{p['role']}] {p['name']} ---\n前序 agent 报告: {p['report']}\n前序 agent 产出: {p['output']}\n"
    if other_priors:
        user_task += "\n=== 团队其他成员已完成的工作 ===\n"
        for p in other_priors[:4]:
            user_task += f"\n[{p['role']}] {p['name']}: {p['report'][:200]}\n"

    # v1.0.15.4 RAG: 跑前从知识库召回历史 5 条相关结论
    try:
        from .rag import search as _rag_search
        kb_query = f"{task['name']} {task['task']} {task['role']}"
        kb_hits = _rag_search(kb_query, top_k=5)
        if kb_hits:
            user_task += "\n=== KB 历史 (RAG 召回, 5 条) ===\n"
            for h in kb_hits:
                title = h.get('title', '')[:30]
                content = h.get('content', '')[:300]
                score = h.get('score', 0)
                user_task += f"\n- [{title}] (score={score:.2f}) {content}\n"
            import sys as _sys
            print(f"[RAG] {task['role']} {task['name']}: 召回 {len(kb_hits)} 条 KB 历史", file=_sys.stderr, flush=True)
        else:
            import sys as _sys
            print(f"[RAG] {task['role']} {task['name']}: 0 条命中", file=_sys.stderr, flush=True)
    except Exception as e:
        import sys as _sys
        print(f"[RAG] {task['role']} {task['name']}: 召回失败 {e}", file=_sys.stderr, flush=True)

    user_task += "\n请用 tool 拿真实数据, 跑完写 'report' + 'output' + 调 save_kb_entry 写一条结论到知识库."

    try:
        r = await run_agent(
            system_prompt=agent_cfg.get("system_prompt", "你是 Mavis 智能体"),
            user_task=user_task,
            tool_names=agent_cfg.get("tools", []),
            context={"task_id": task["id"], "form": state["form"]},
            max_iters=10,
            agent_role=task["role"],  # v1.0.15.3: server 端兜底, 强制 DA/BA/BI 先 ask_user
        )
        task["duration_sec"] = time.time() - task.get("dispatched_at", time.time())
        task["finished_at"] = time.time()
        task["result"] = {"content": r.final_content, "tool_calls": r.tool_calls_made, "iters": r.total_iters, "error": r.error or ""}
        task["placeholder_count"] = r.placeholder_count
        if r.error:
            task["status"] = "failed"
            task["report"] = f"❌ 失败: {r.error[:200]}"
        else:
            task["status"] = "done"
            task["output"] = extract_final_data(r.final_content)
            # 抽 report 段
            import re
            m = re.search(r"(?:##\s*Report|##\s*报告)\s*[:：]?\s*(.+?)(?:\n##|\Z)", r.final_content or "", re.DOTALL | re.IGNORECASE)
            if m:
                task["report"] = m.group(1).strip()[:500]
            else:
                task["report"] = (r.final_content or "")[:500]
    except Exception as e:
        task["status"] = "failed"
        task["result"] = {"error": str(e)}
        task["report"] = f"❌ exception: {str(e)[:200]}"

    # v1.0.15.1: failed 自动重试 1 次 (minimax 偶发 fail, 给机会再来)
    if task["status"] == "failed" and task.get("retry_count", 0) < 1:
        task["retry_count"] = task.get("retry_count", 0) + 1
        task["status"] = "pending"
        task["report"] = ""
        state["log"].append({
            "type": "retry",
            "task_id": task["id"],
            "msg": f"task {task['id']} 失败, 重试 1 次 (retry_count={task['retry_count']})",
        })

    # 更新总 placeholder
    state["total_placeholders"] = sum(t.get("placeholder_count", 0) for t in state["tasks"])
    state["log"].append({
        "type": "worker_done",
        "task_id": task["id"],
        "status": task["status"],
        "duration_sec": task.get("duration_sec", 0),
    })
    return state


def should_continue(state: ManagerState) -> str:
    """check_status: 决定下一步"""
    # 找 running task
    if any(t["status"] == "running" for t in state["tasks"]):
        return "run_worker"
    # 找 ready pending task (v1.0.15.1: dep done OR failed 都算 ready)
    completed_ids = {t["id"] for t in state["tasks"] if t["status"] in ("done", "failed")}
    has_ready = any(
        t["status"] == "pending" and all(d in completed_ids for d in (t.get("depends_on") or []))
        for t in state["tasks"]
    )
    if has_ready:
        return "dispatch"
    # 全部 done 或 failed
    return "mark_done"


async def node_mark_done(state: ManagerState) -> ManagerState:
    """收尾: 拼 final_deliverable, 标 is_done
    v1.0.15.1: 拼所有 done task 的 report (不只 BI), 给真 fallback
    """
    done_tasks = [t for t in state["tasks"] if t["status"] == "done"]
    failed_tasks = [t for t in state["tasks"] if t["status"] == "failed"]
    summary = f"完成 {len(done_tasks)} 个任务"
    if failed_tasks:
        summary += f", {len(failed_tasks)} 个失败 (已自动重试 1 次)"

    # 1. 优先用最后 1 个 BI 任务的 output (v1.0.15.1: 兼容 3 种格式: content/deliverable/report)
    bi_tasks = [t for t in done_tasks if t["role"] == "bi"]
    deliverable = ""
    if bi_tasks:
        out = bi_tasks[-1].get("output", {})
        if isinstance(out, dict):
            deliverable = out.get("deliverable") or out.get("content") or out.get("summary") or ""
        if not deliverable.strip():
            deliverable = bi_tasks[-1].get("report", "") or ""

    # 2. 没 BI 或 BI output 空, 拼所有 done task 的 report (按 plan 顺序)
    if not deliverable.strip():
        parts = ["## 任务产出汇总 (按 plan 顺序)"]
        for t in state["tasks"]:
            if t["status"] != "done":
                continue
            role = t.get("role", "")
            name = t.get("name", t["id"])
            out_content = ""
            if isinstance(t.get("output"), dict):
                for k in ("deliverable", "content", "summary"):
                    v = t["output"].get(k, "")
                    if v:
                        out_content = str(v) if not isinstance(v, str) else v
                        break
            elif isinstance(t.get("output"), str):
                out_content = t["output"]
            report = (t.get("report") or "").strip()
            if out_content or report:
                parts.append(f"\n### [{role.upper()}] {name}\n")
                if report:
                    parts.append(report)
                if out_content and out_content != report:
                    parts.append("\n详细产出: " + str(out_content)[:800])
        deliverable = "\n".join(parts).strip() if len(parts) > 1 else "所有 task 都失败, 无法生成报告"

    state["final_deliverable"] = {
        "summary": summary,
        "deliverable": deliverable,
        "next_suggestion": "建议定期更新分析报告以反映最新的市场情况。",
        "is_fallback": len(bi_tasks) == 0 or not deliverable.strip(),
    }
    state["is_done"] = True
    state["log"].append({"type": "mark_done", "summary": summary})
    return state


def build_manager_graph() -> StateGraph:
    """build LangGraph state machine"""
    g = StateGraph(ManagerState)

    g.add_node("dispatch", node_dispatch)
    g.add_node("run_worker", node_run_worker)
    g.add_node("mark_done", node_mark_done)

    g.add_edge(START, "dispatch")
    g.add_conditional_edges("dispatch", should_continue, {
        "run_worker": "run_worker",
        "mark_done": "mark_done",
    })
    g.add_conditional_edges("run_worker", should_continue, {
        "run_worker": "run_worker",
        "dispatch": "dispatch",
        "mark_done": "mark_done",
    })
    g.add_edge("mark_done", END)

    return g.compile()


async def run_manager_langgraph(
    initial_plan: List[Dict],
    form: Dict,
    user_question_callback: Optional[callable] = None,
    workflow_id: str = "",
    max_iterations: int = 20,
    on_log: Optional[callable] = None,
    on_board_update: Optional[callable] = None,
) -> Dict:
    """用 LangGraph 跑 manager agent

    Returns: {final_deliverable, board_summary, log, steps}
    """
    state = make_state(form, initial_plan)
    graph = build_manager_graph()

    last_iteration = 0
    final_state = dict(state)  # 复制初态
    async for step in graph.astream(state, config={"recursion_limit": max_iterations * 5}):
        node_name = list(step.keys())[0] if step else None
        if node_name:
            new_state = step[node_name]
            # merge new_state 到 final_state (v1.0.15 fix: 累积 state)
            for k, v in new_state.items():
                if v is not None:
                    final_state[k] = v
            # 回调
            # v1.0.15.1 fix: last_iteration 跟 final_state (累积) 同步, 不是 new_state
            final_state["log"] = final_state.get("log") or []
            if on_log and new_state.get("log"):
                new_logs = new_state["log"]
                # on_log 推 last_iteration 之后的 (new_state 增量)
                for x in new_logs[last_iteration:]:
                    try:
                        on_log(x)
                    except Exception:
                        pass
                last_iteration = len(new_logs)
            if on_board_update:
                try:
                    on_board_update({
                        "tasks": new_state["tasks"],
                        "is_done": new_state.get("is_done", False),
                        "total_placeholders": new_state.get("total_placeholders", 0),
                        "plan_recommended_count": new_state.get("plan_recommended_count", 0),
                        "plan_recommended_done": sum(1 for t in new_state["tasks"] if t.get("plan_recommended") and t["status"] == "done"),
                    })
                except Exception:
                    pass

    final = {
        "final_deliverable": final_state.get("final_deliverable"),
        "board_summary": {
            "tasks": final_state["tasks"],
            "is_done": final_state.get("is_done", False),
            "total_placeholders": final_state.get("total_placeholders", 0),
            "plan_recommended_count": final_state.get("plan_recommended_count", 0),
            "plan_recommended_done": sum(1 for t in final_state["tasks"] if t.get("plan_recommended") and t["status"] == "done"),
        },
        "log": final_state["log"],
    }
    return final
