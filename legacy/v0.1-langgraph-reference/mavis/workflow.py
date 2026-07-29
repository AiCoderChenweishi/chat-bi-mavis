"""
异步工作流引擎 (v1.0 强化)

按 docs/async-workflow.md
- DAG 依赖 + asyncio 并发
- 事件总线 + 实时推进
- 16 步工作流可异步跑
"""
import asyncio
import functools
import os
import uuid
import json as _json
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable, Any

# v1.0.6.1 根本性 refactor: 统一 step output schema
from .types import normalize_step_output, StepOutput


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"  # v1.0.9 T2 Smart Retry: escalate 后等 user 介入
    DATA_FALLBACK = "data_fallback"  # v1.0.11 fix: step 跑通了, 但数据是 mock fallback (公网不可访, 不应该当成真数据)


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_USER = "awaiting_user"  # 等待用户确认再继续
    AWAITING_AUTO_DECISION = "awaiting_auto_decision"  # v1.0.8+ auto-continue: 调 auto_decide_for_step 拿 Decision, 24h 可 override
    CANCELLED = "cancelled"  # 用户终止


@dataclass
class Step:
    """工作流的一个 step"""
    id: str
    name: str
    depends_on: list = field(default_factory=list)
    agent: str = "ba"  # 默认 BA agent
    timeout_sec: int = 300
    max_retries: int = 2
    critical: bool = True  # critical 失败阻塞整个 workflow
    requires_user_input: bool = False  # 跑完这步后等用户确认(per step)
    params: dict = field(default_factory=dict)
    func: Optional[Callable] = None  # step 执行函数(Phase 1 用 mock)
    custom_type: Optional[str] = None  # 调 CustomStepRegistry 的 type
    custom_inputs: dict = field(default_factory=dict)  # custom step 的输入
    skip_llm_executor: bool = False  # True = 不调 LLM (step-1 由 PM 调研填结果)
    skip_pm_interview: bool = False  # v1.0.9 T3: True = 启动时如果 form 完整, 直接标 step-1 COMPLETED 跳过 PM 调研


@dataclass
class StepResult:
    """step 执行结果"""
    step_id: str
    status: StepStatus
    output: Any = None
    error: str = ""
    summary: str = ""  # Mavis 自动总结的一句话
    next_suggestion: str = ""  # 下一步建议
    started_at: str = ""
    finished_at: str = ""
    duration_sec: float = 0.0
    retries: int = 0
    reflection: Optional[dict] = None  # v1.0.7 Phase B: 反思结果 {verdict, reason, feedback, confidence}
    metadata: Optional[dict] = None  # v1.0.8+ agentic: 存 decision / override 等


@dataclass
class WorkflowEvent:
    """工作流事件"""
    type: str  # step.started, step.completed, step.failed, step.blocked, workflow.progress
    workflow_id: str
    step_id: str = ""
    data: dict = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Workflow:
    """一个完整工作流"""
    id: str = field(default_factory=lambda: f"wf-{uuid.uuid4().hex[:8]}")
    name: str = "数据分析 16 步"
    steps: list = field(default_factory=list)  # list[Step]
    status: WorkflowStatus = WorkflowStatus.CREATED
    results: dict = field(default_factory=dict)  # {step_id: StepResult}
    started_at: str = ""
    finished_at: str = ""
    paused_at_layer: int = -1  # -1 = 不暂停, >=0 = 在第 N 层后等用户
    current_layer: int = 0
    metadata: dict = field(default_factory=dict)

    def progress(self) -> dict:
        """整体进度"""
        total = len(self.steps)
        done = sum(1 for r in self.results.values() if r.status == StepStatus.COMPLETED)
        failed = sum(1 for r in self.results.values() if r.status == StepStatus.FAILED)
        blocked = sum(1 for r in self.results.values() if r.status == StepStatus.SKIPPED)
        return {
            "total": total,
            "completed": done,
            "failed": failed,
            "blocked": blocked,
            "pending": total - done - failed - blocked,
            "percent": f"{(done/total)*100:.0f}%" if total else "0%",
        }


class EventBus:
    """简单事件总线 (in-memory,Phase 2 加 Redis)"""

    def __init__(self):
        self._subscribers: dict = defaultdict(list)
        self._history: list = []

    def subscribe(self, event_type: str, handler: Callable):
        self._subscribers[event_type].append(handler)

    def publish(self, event: WorkflowEvent):
        self._history.append(event)
        for handler in self._subscribers.get(event.type, []):
            try:
                handler(event)
            except Exception as e:
                print(f"[EventBus] handler 异常: {e}")

    def history(self) -> list:
        return self._history[-100:]  # 最近 100 条

    def last_event(self, event_type: str = None) -> dict:
        """拿最近一个事件 (SSE heartbeat 用). 按 type 过滤可选."""
        for e in reversed(self._history):
            if event_type is None or e.type == event_type:
                return {
                    "type": e.type,
                    "workflow_id": e.workflow_id,
                    "step_id": e.step_id,
                    "ts": e.ts,
                    "data": e.data,
                }
        return {}


# === 公共辅助: emit heartbeat (v1.0.8+) ===

def publish_heartbeat(event_bus: EventBus, workflow_id: str, step_id: str = "", data: dict = None):
    """发个 workflow.heartbeat 事件, SSE 长连接推给前端

    推的内容: step 状态 / progress / decision / override 倒计时
    """
    event_bus.publish(WorkflowEvent(
        type="workflow.heartbeat",
        workflow_id=workflow_id,
        step_id=step_id,
        data=data or {},
    ))


# === 持久化 (Phase 1.5: 重启可恢复) ===

class WorkflowPersistence:
    """workflow 状态持久化 (JSONL append-only)"""

    def __init__(self, path: str = "/tmp/mavis-workflow.jsonl"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 确保文件存在
        if not os.path.exists(path):
            open(path, "w").close()

    def save_workflow(self, wf: "Workflow"):
        """存一个 workflow(覆盖写同 id)"""
        import json as _json
        d = {
            "id": wf.id,
            "name": wf.name,
            "status": wf.status.value,
            "started_at": wf.started_at,
            "finished_at": wf.finished_at,
            "metadata": wf.metadata,
            "steps": [
                {
                    "id": s.id,
                    "name": s.name,
                    "depends_on": s.depends_on,
                    "agent": s.agent,
                    "custom_type": s.custom_type,
                    "custom_inputs": s.custom_inputs,
                    "timeout_sec": s.timeout_sec,
                    "max_retries": s.max_retries,
                    "critical": s.critical,
                }
                for s in wf.steps
            ],
            "results": {
                sid: {
                    "step_id": r.step_id,
                    "status": r.status.value,
                    "output": r.output,
                    "error": r.error,
                    "summary": r.summary,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "duration_sec": r.duration_sec,
                    "retries": r.retries,
                }
                for sid, r in wf.results.items()
            },
        }
        # 读所有行,替换同 id,再写回
        with open(self.path, "r") as f:
            lines = [l for l in f if l.strip() and _json.loads(l).get("id") != wf.id]
        lines.append(_json.dumps(d, ensure_ascii=False))
        with open(self.path, "w") as f:
            f.write("\n".join(lines) + "\n")

    def load_all(self) -> list:
        """加载所有 workflow"""
        import json as _json
        out = []
        if not os.path.exists(self.path):
            return out
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(_json.loads(line))
                except Exception:
                    continue
        return out

    def clear(self):
        """清空 (测试用)"""
        if os.path.exists(self.path):
            os.remove(self.path)
            open(self.path, "w").close()


# === DAG 构建 ===

def build_default_16_step_dag() -> list:
    """v1.0 16 步的 DAG 依赖
    按用户 2026-07-18 反馈: 每步跑完跟用户确认
    - 同层内部并行 (已实现)
    - 跨层暂停等用户 (requires_user_input)
    - 关键 step (建模/分析/结论/交付) 必确认
    - 中间辅助 step (WBS/质检) 自动
    """
    # 跨层必确认 (关键)
    CRITICAL = True
    # 自动
    AUTO = False
    return [
        # v1.0.6.1 改: step-1 改名 'PM 调研' + skip_llm_executor
        #   PM 调研是独立路径 (/workflow/pm_interview/stream), step-1 的 LLM executor
        #   不应跑 (否则会跟 PM 调研重复生成“接需求”JSON, user 看到两份报告).
        Step(id="step-1", name="PM 调研", depends_on=[], agent="pm", requires_user_input=CRITICAL, skip_llm_executor=True),
        Step(id="step-2", name="了解业务背景", depends_on=["step-1"], agent="ba", requires_user_input=AUTO),
        Step(id="step-3", name="了解数据源", depends_on=["step-1"], agent="dba", requires_user_input=AUTO),
        Step(id="step-4", name="数仓沟通 + 清洗", depends_on=["step-2", "step-3"], agent="dba", requires_user_input=AUTO),
        Step(id="step-5", name="WBS 拆解", depends_on=["step-1"], agent="pm", requires_user_input=AUTO),
        Step(id="step-6", name="确认建模", depends_on=["step-2", "step-3", "step-4"], agent="da", requires_user_input=CRITICAL),
        Step(id="step-7", name="报表蓝图", depends_on=["step-6"], agent="bi", requires_user_input=AUTO),
        Step(id="step-8", name="报表开发", depends_on=["step-7"], agent="bi", requires_user_input=CRITICAL),
        Step(id="step-9", name="分析思路", depends_on=["step-2", "step-5"], agent="da", requires_user_input=CRITICAL),
        Step(id="step-10", name="进行分析", depends_on=["step-6", "step-9"], agent="da", requires_user_input=CRITICAL),
        Step(id="step-11", name="得出结论", depends_on=["step-10"], agent="da", requires_user_input=CRITICAL),
        Step(id="step-12", name="质量检测", depends_on=["step-11"], agent="da", requires_user_input=AUTO),
        Step(id="step-13", name="进行沟通", depends_on=["step-11"], agent="ba", requires_user_input=AUTO),
        Step(id="step-14", name="交付报告", depends_on=["step-12", "step-13"], agent="pm", requires_user_input=CRITICAL),
        Step(id="step-15", name="提出改进", depends_on=["step-11"], agent="ba", requires_user_input=AUTO),
        Step(id="step-16", name="追踪改进后数据", depends_on=["step-15"], agent="da", requires_user_input=CRITICAL),
    ]


def build_scenario_a_dag() -> list:
    """场景 A: 紧急 ad-hoc (5 步快速版)

    按 user PRD (data-analyst-workflow.md section 3):
    - 跳过的步骤: 1(简化)→ 3→ 4(轻清洗)→ 10→ 13→ 14, 直接出
    - Mavis 行为: 5 分钟内出答复 + 1 张图 + 3 句话结论
    - 关键风险: 数据口径不对 → 强制要求看 step 2 的口径表

    5 步:
    - a-1 PM 调研 (简化 1 轮)
    - a-2 数据源 (DBA, 假数据)
    - a-3 轻清洗 (DA, 聚合)
    - a-4 分析 (DA, critical, 出图+结论)
    - a-5 出答复 (BI, executive_brief)
    """
    CRITICAL = True
    AUTO = False
    return [
        Step(id="a-1", name="接需求", agent="pm", depends_on=[], requires_user_input=CRITICAL, skip_llm_executor=True),
        Step(id="a-2", name="数据源", agent="dba", depends_on=["a-1"], requires_user_input=AUTO,
             custom_type="mock_dau_data", custom_inputs={"days": 7, "channels": ["wechat", "douyin", "appstore", "huawei"]}),
        Step(id="a-3", name="轻清洗", agent="da", depends_on=["a-2"], requires_user_input=AUTO,
             custom_type="aggregate_by", custom_inputs={"group_by": "day", "metric": "dau", "agg": "sum"}),
        Step(id="a-4", name="分析", agent="da", depends_on=["a-3"], requires_user_input=CRITICAL,
             custom_inputs={"format": "chart_plus_3_sentences"}),
        Step(id="a-5", name="出答复", agent="bi", depends_on=["a-4"], requires_user_input=AUTO,
             custom_inputs={"format": "executive_brief"}),
    ]


def build_dag_for_scenario(scenario: str = "default") -> list:
    """根据 scenario 选不同 DAG
    
    Args:
        scenario: 
          - "ad_hoc" / "A" / "emergency" → 5 步快速版
          - "应急响应" / "ad-hoc 取数" / "紧急" / "紧急 ad-hoc" / "紧急查询" → 5 步快速版 (中文 alias)
          - 其他 → 16 步完整版
    """
    scenario_lower = (scenario or "").lower().strip()
    # ad_hoc 5 步快速版的触发词
    AD_HOC_TRIGGERS = {"ad_hoc", "a", "emergency", "urgent", "p0"}
    AD_HOC_TRIGGERS_CN = {"应急响应", "ad-hoc 取数", "紧急", "紧急 ad-hoc", "紧急查询", "ad-hoc", "adhoc"}
    if scenario_lower in AD_HOC_TRIGGERS or scenario in AD_HOC_TRIGGERS_CN:
        return build_scenario_a_dag()
    return build_default_16_step_dag()


def build_dag_from_plan(plan) -> list:
    """v1.0.7+: 从 PlanResult (agentic 自主规划) 转成 workflow.Step list
    
    让 PM agent 自己决定多少步/谁做/依赖关系, 替代 hardcoded 5/16 步.
    """
    from .planner import plan_to_workflow_steps
    return plan_to_workflow_steps(plan)

# === 拓扑排序(分层,每层是当前可并行的 step) ===

def get_affected_steps(steps: list, target_step_id: str) -> list:
    """找所有 transitively 依赖 target_step_id 的 step
    返回: list of step id (含 target 自己)
    """
    step_map = {s.id: s for s in steps}
    affected = {target_step_id}
    queue = [target_step_id]
    while queue:
        cur = queue.pop(0)
        for s in steps:
            if cur in (s.depends_on or []) and s.id not in affected:
                affected.add(s.id)
                queue.append(s.id)
    return list(affected)


def get_step_layer_index(steps: list, step_id: str) -> int:
    """取 step 在 topological_layers 里的 layer 索引"""
    layers = topological_layers(steps)
    for i, layer in enumerate(layers):
        for s in layer:
            if s.id == step_id:
                return i
    return 0


def topological_layers(steps: list) -> list:
    """
    返回分层 DAG,每层是可并行的 step 列表
    """
    step_map = {s.id: s for s in steps}
    in_degree = defaultdict(int)
    for s in steps:
        in_degree[s.id] = 0
    for s in steps:
        for dep in s.depends_on:
            in_degree[s.id] += 1

    layers = []
    current = [s.id for s in steps if in_degree[s.id] == 0]

    while current:
        layers.append([step_map[sid] for sid in current])
        next_layer = []
        for sid in current:
            for s in steps:
                if sid in s.depends_on:
                    in_degree[s.id] -= 1
                    if in_degree[s.id] == 0:
                        next_layer.append(s.id)
        current = next_layer

    return layers


# === 模拟 step 执行(Phase 1 用 mock) ===

async def mock_step_executor(step: Step, inputs: dict, wf: Optional[Workflow] = None) -> dict:
    """
    Phase 1 模拟执行

    - 如果 step.custom_type 存在 → 调 CustomStepRegistry
    - 否则 → mock
    """
    # v1.0.11 智能体模式优先: 如果 step.agent 是 PM/BA/DBA/DA/BI 且 agent_loop=True, 优先走 agent_loop
    #   只有 agent_loop=False 或 step.agent 不在 5 个 agent 里时, 才走 custom_step
    #   这样 plan LLM 写的 custom_type=public_data_crawl 也会被 agent 接管 (DBA agent 有 crawl_public tool)
    if step.custom_type:
        # 读 agent 配置, 看是否优先 agent_loop
        _agent_cfg = None
        try:
            from .agents_registry import get_agent as _ga
            _agent_cfg = _ga(step.agent) if step.agent else None
        except Exception:
            pass
        defn = None
        if not (_agent_cfg and _agent_cfg.get("agent_loop", False)):
            from .custom_step import get_custom_step_registry
            registry = get_custom_step_registry()
            defn = registry.get(step.custom_type)
            if defn is None:
                # v1.0.11 修: custom step 找不到时不 fail, 降级到 LLM agent 跑
                #   之前 raise ValueError, plan LLM 生成 'clean_outliers' 等未注册 type 会直接 fail step
                #   现在: 警告 + 设 defn=None → 跳到下面 LLM 路径
                import logging as _logging
                _logging.getLogger("mavis.workflow").warning(
                    f"custom step type '{step.custom_type}' not registered, fallback to LLM agent for step {step.id}"
                )
        if defn is not None:
            # 合并 inputs (step 自带 + 依赖输出)
            call_inputs = {**step.custom_inputs, **inputs}
            # v1.0.11 A+: aggregate_by 等 custom step 需要 rows, 但 plan 时常没填
            #    兜底: 从 inputs 顶层拿 rows, 或从 dep_outputs (前序 DBA step 的 rows) 拿
            if step.custom_type == "aggregate_by" and not call_inputs.get("rows"):
                if isinstance(inputs, dict) and inputs.get("rows"):
                    call_inputs["rows"] = inputs["rows"]
                else:
                    try:
                        for dep_id in (step.depends_on or []):
                            dep_out = (inputs.get(dep_id) or {}) if isinstance(inputs, dict) else {}
                            if isinstance(dep_out, dict) and dep_out.get("rows"):
                                call_inputs["rows"] = dep_out["rows"]
                                break
                            if isinstance(dep_out, dict):
                                for k, v in dep_out.items():
                                    if isinstance(v, dict) and v.get("rows"):
                                        call_inputs["rows"] = v["rows"]
                                        break
                    except Exception:
                        pass
            # 校验
            errors = defn.validate_inputs(call_inputs)
            if errors:
                raise ValueError(f"input validation failed for {step.custom_type}: {errors}")
            # 调 handler
            import asyncio as _asyncio
            result = defn.handler(call_inputs)
            if _asyncio.iscoroutine(result):
                result = await result
            # v1.0.6.1 修: 用 normalize_step_output 统一处理
            #           handler 返 dict 时直接归一化, 没有 dict 包装成 dict
            if not isinstance(result, dict):
                result = {"value": result}
            _custom_out = normalize_step_output(result, step)
            api_dict = _custom_out.to_api_dict()
            api_dict["custom_type"] = step.custom_type
            # 保留原始 output 供前端 use (e.g. 真实 rows 数据)
            api_dict["output"] = result
            return api_dict
        # defn is None → 降级到下面 LLM agent 路径 (plan 用了未注册的 custom_type)

    # 1.5 v1.0.6.1 修: skip_llm_executor 标记的 step (e.g. step-1 PM 调研) 不调 LLM
    #       v1.0.6.1 根本性 refactor: 仍走 normalize_step_output, 但带 pending 标记
    if step.skip_llm_executor:
        # 检查 PM 调研是否已经完成 (如果完成就返真实内容, 否则返"调研中"提示)
        # 这个 context 在 _run_step 外部传入, 但目前没有. 简单点: 返调研中提示
        # 真实填充在 continue_workflow 里调 (那里有 pm_report)
        return {
            "step_id": step.id,
            "agent": step.agent,
            "agent_title": "产品经理",
            "summary": f"[{step.name}] PM 调研中 (请看 step-1 卡片 chat)",
            "deliverable": "PM 调研中, 请在 step-1 卡片回答 PM 的问题或点『结束调研, 生成报告』",
            "next_suggestion": "回答 PM",
            "source": "pm_interview",
        }

    # 2. Agent 真干活 — v1.0.11 智能体模式: LLM 自主调 tool
    #    代替之前"prompt + LLM 返 JSON" 的剧本模式
    #    如果 agent 配置 agent_loop=True, 走真 agent runner
    try:
        from .agents_registry import get_agent as _get_agent
        from .llm import get_llm as _get_llm, is_llm_available as _is_llm
    except Exception:
        _get_agent = _get_llm = _is_llm = None
    if _is_llm and _is_llm() and step.agent in ("pm", "ba", "dba", "da", "bi"):
        agent = _get_agent(step.agent)
        # user 自定义 prompt 优先
        try:
            customs = ((wf.metadata.get('agent_prompts', {}) if wf and isinstance(wf.metadata, dict) else {}))
            if customs.get((step.agent or "").lower()):
                agent = {**agent, 'system_prompt': customs[step.agent.upper()]}
        except Exception:
            pass
        # v1.0.11: 智能体模式 - LLM 自主调 tool
        if agent.get("agent_loop", False) and agent.get("tools"):
            from .agent_runner import run_agent, extract_final_data
            _user_task = f"任务: {step.name}\n任务 ID: {step.id}\n上游输入: {json.dumps(inputs, ensure_ascii=False)[:2500]}\n请用你的 tool 自主完成这个任务, 最后返 JSON {{summary, deliverable, next_suggestion}}."
            try:
                _agent_result = await run_agent(
                    system_prompt=agent["system_prompt"],
                    user_task=_user_task,
                    tool_names=agent.get("tools", []),
                    context={"step_id": step.id, "form": inputs.get("form", {}) if isinstance(inputs, dict) else {}},
                    max_iters=8,
                    temperature=0.3,
                    max_tokens=2000,
                )
                if not _agent_result.error:
                    # 解析 LLM final_content 为 deliverable
                    _final_data = extract_final_data(_agent_result.final_content)
                    _out = normalize_step_output(
                        _final_data, step,
                        default_agent=agent.get("name", ""),
                        default_agent_title=agent.get("title", ""),
                    )
                    api_dict = _out.to_api_dict()
                    api_dict["raw_response"] = _agent_result.final_content[:2000]
                    api_dict["source"] = "agent_loop"
                    api_dict["agent_iters"] = _agent_result.total_iters
                    api_dict["tool_calls_made"] = [
                        {"name": t["name"], "args": t["args"], "ok": t["result"].get("ok"), "dur": round(t["duration_sec"], 2)}
                        for t in _agent_result.tool_calls_made
                    ]
                    api_dict["agent_steps_count"] = len(_agent_result.steps)
                    return api_dict
            except Exception as _e:
                # agent 挂了 - 走老路径
                pass
        # 老剧本模式 (agent_loop=False 或 tool 列表空)
        llm = _get_llm()
        # 查 RAG 拿相关 context
        rag_context = ""
        try:
            from .rag import search as _rag_search
            query = f"{step.name} {agent['title']} "
            if isinstance(step.custom_inputs, dict):
                query += (step.custom_inputs.get("text", "") or json.dumps(step.custom_inputs, ensure_ascii=False)[:200])
            hits = _rag_search(query, top_k=3)[:3]
            if hits:
                rag_context = "\n\n## 相关知识 (RAG):\n\n" + "\n\n".join(
                    f"- 《{h['title']}》 (score={h['score']}): {h['content'][:300]}" for h in hits
                )
        except Exception:
            pass
        sys_p = agent["system_prompt"] + "\n\n严格返回 JSON: {summary, deliverable, next_suggestion}。summary 一句话, deliverable 字典/list, next_suggestion 短句。"
        user_p = f"任务: {step.name}\n任务 ID: {step.id}\n上游输入: {json.dumps(inputs, ensure_ascii=False)[:1500]}{rag_context}\n\n请作为 {agent['title']} 完成这个任务, 返回 JSON。"
        try:
            r = await llm.chat_async(
                messages=[
                    {"role": "system", "content": sys_p},
                    {"role": "user", "content": user_p},
                ],
                temperature=0.4,
                max_tokens=1200,
            )
            content = r["content"].strip()
            # v1.0.6.1 修: 用 normalize_step_output 统一处理所有 LLM 形态
            #           之前 80+ 行 if-else 全删 (markdown 包裹 / 嵌套 / placeholder / dict 嵌套)
            try:
                # 先尝解析外层 (LLM 可能包在 ```json``` 里, 后面追加 '### 解释: ' 等)
                # v1.0.6.1 修: 容忍 markdown 块后面有 extra text
                content_for_parse = content
                if content.startswith("```"):
                    import re as _re_md
                    # 先试严格匹配 (到行尾)
                    _om = _re_md.match(r"^\`\`\`\w*\n(.*?)\n\`\`\`$", content.strip(), _re_md.DOTALL)
                    if not _om:
                        # fallback: 只匹配第一个 ``` 块, 后面 extra text 丢掉
                        _om = _re_md.match(r"^\`\`\`\w*\n(.*?)\n\`\`\`", content.strip(), _re_md.DOTALL)
                    if _om:
                        content_for_parse = _om.group(1)
                        if content_for_parse.startswith("json"):
                            content_for_parse = content_for_parse[4:]
                        content_for_parse = content_for_parse.strip()
                parsed = json.loads(content_for_parse)
            except Exception:
                parsed = {"raw": content[:1000]}
            # 唯一一行归一化 (代替之前 80+ 行 if-else)
            _out = normalize_step_output(
                parsed, step,
                default_agent=agent.get("name", ""),
                default_agent_title=agent.get("title", ""),
            )
            api_dict = _out.to_api_dict()
            api_dict["raw_response"] = content[:1500]
            return api_dict
        except Exception as e:
            # LLM 失败 → 走 mock
            pass

    # 3. Mock fallback (LLM 不可用时)
    #    v1.0.6.1 修: 用 normalize_step_output 统一处理
    await asyncio.sleep(0.1 + (hash(step.id) % 7) * 0.05)
    _mock_out = normalize_step_output({"__mock__": True}, step)
    return _mock_out.to_api_dict()


# === WorkflowEngine ===

class WorkflowEngine:
    """异步工作流引擎"""

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        executor: Optional[Callable] = None,
        persistence=None,  # WorkflowPersistence (JSONL) 或 SQLitePersistence
    ):
        self.event_bus = event_bus or EventBus()
        self.executor = executor
        # 默认 SQLite (更可查), 兼容旧 JSONL
        if persistence is None:
            from .persistence_sqlite import SQLitePersistence
            persistence = SQLitePersistence()
        self.persistence = persistence
        self.workflows: dict = {}  # {workflow_id: Workflow}

    def create(self, steps: list = None, name: str = "数据分析 16 步") -> Workflow:
        """创建一个工作流(不立即跑)"""
        wf = Workflow(name=name, steps=steps or build_default_16_step_dag())
        self.workflows[wf.id] = wf
        self.persistence.save_workflow(wf)
        return wf

    def get(self, workflow_id: str) -> Optional[Workflow]:
        return self.workflows.get(workflow_id)

    def cancel(self, workflow_id: str) -> Optional[Workflow]:
        """终止 workflow"""
        wf = self.workflows.get(workflow_id)
        if not wf:
            return None
        wf.status = WorkflowStatus.CANCELLED
        wf.finished_at = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        if self.persistence:
            self.persistence.save_workflow(wf)
        return wf

    async def run(self, workflow_id: str, initial_inputs: dict = None) -> Workflow:
        """异步跑整个 workflow"""
        wf = self.workflows[workflow_id]
        wf.status = WorkflowStatus.RUNNING
        wf.started_at = datetime.now().isoformat()
        inputs = initial_inputs or {}

        layers = topological_layers(wf.steps)
        # 从 paused_at_layer+1 继续
        start_layer = max(0, wf.paused_at_layer + 1) if wf.paused_at_layer >= 0 else 0
        # 跳过初始所有 step 已 completed 的层 (jump 后或 restart 场景)
        while start_layer < len(layers):
            layer = layers[start_layer]
            all_done = all(wf.results.get(s.id) and wf.results[s.id].status == StepStatus.COMPLETED for s in layer)
            if all_done:
                start_layer += 1
            else:
                break
        # v1.0.6.1 修: 起始层如果有 skip_llm_executor=True 的 step (e.g. step-1 PM 调研)
        #   且 PM 未完成, 只跑该 skip step, 其他 step 等 PM 完成后由 continue 跳过
        #   不应该: 拿个 PM 报告 + 同时让 LLM 假起 step-2/3/5 出假数据
        if start_layer < len(layers):
            start_layer_steps = layers[start_layer]
            skip_steps = [s for s in start_layer_steps if getattr(s, "skip_llm_executor", False)]
            if skip_steps:
                md = wf.metadata if isinstance(wf.metadata, dict) else {}
                pmi = md.get("pm_interview") or {}
                if not pmi.get("done"):
                    # PM 未做, 只跑 skip step (标记 RUNNING, 不动其他 step)
                    for s in skip_steps:
                        if s.id not in wf.results or wf.results[s.id].status != StepStatus.RUNNING:
                            r = StepResult(
                                step_id=s.id,
                                status=StepStatus.RUNNING,
                                started_at=datetime.now().isoformat(),
                                output={"step_id": s.id, "agent": s.agent, "summary": f"[{s.name}] PM 调研中, 请看卡片 chat", "deliverable": "PM 调研中, 请在 step-1 卡片回答 PM 的问题或点【结束调研】", "next_suggestion": "回答 PM"},
                            )
                            wf.results[s.id] = r
                            self.event_bus.publish(WorkflowEvent(type="step.pending_user", workflow_id=wf.id, step_id=s.id, data={"summary": r.output["summary"]}))
                    wf.paused_at_layer = start_layer
                    wf.status = WorkflowStatus.AWAITING_USER
                    self.event_bus.publish(WorkflowEvent(
                        type="workflow.paused",
                        workflow_id=wf.id,
                        data={"layer": start_layer, "reason": "pm_interview_pending", "paused_steps": [s.id for s in skip_steps]},
                    ))
                    self.persistence.save_workflow(wf)
                    return wf
        import sys as _sys
        print(f"[run] wf={wf.id} paused_at_layer={wf.paused_at_layer} start_layer={start_layer} layers={len(layers)} step1_status={wf.results.get('step-1').status if wf.results.get('step-1') else 'NONE'} all_l0_done={all(wf.results.get(s.id) and wf.results[s.id].status == StepStatus.COMPLETED for s in layers[0])}", file=_sys.stderr, flush=True)

        for layer_idx in range(start_layer, len(layers)):
            layer = layers[layer_idx]
            wf.current_layer = layer_idx
            # 这层所有 step 并行跑
            tasks = [self._run_step(wf, step, inputs) for step in layer]
            await asyncio.gather(*tasks)

            # v1.0.8+ 修: 任意 step 标了 requires_user_input (CRITICAL 步) -> 调 auto_decide_for_step 拿 Decision
            #   24h 内 user 可 override, 没 override 就执行 chosen.
            #   不再卡死在 AWAITING_USER, 走 AWAITING_AUTO_DECISION 后立即继续
            if any(
                s.requires_user_input
                for s in layer
                if wf.results.get(s.id) and wf.results[s.id].status == StepStatus.COMPLETED
            ):
                # 拿所有需要决策的 step
                decision_steps = [
                    s for s in layer
                    if s.requires_user_input
                    and wf.results.get(s.id)
                    and wf.results[s.id].status == StepStatus.COMPLETED
                ]
                # 给每个 critical step 调 auto_decide_for_step
                from .auto_decide import auto_decide_for_step
                for s in decision_steps:
                    if not s.critical:
                        # 非 critical 的还是要 user 确认 (旧逻辑)
                        continue
                    ctx = {
                        "step": s,
                        "workflow": wf,
                        "upstream_outputs": {
                            d: (wf.results[d].output if d in wf.results else None)
                            for d in (s.depends_on or [])
                        },
                        "last_user_feedback": wf.metadata.get("last_user_feedback", ""),
                    }
                    try:
                        decision = auto_decide_for_step(s, ctx)
                    except Exception as _e:
                        decision = None
                    if decision is not None:
                        # v1.0.8+ T1: 设置 24h override 截止时间 (T2 auto_decide 不填这个)
                        if not getattr(decision, "override_deadline", ""):
                            from datetime import datetime as _dt, timedelta as _td
                            decision.override_deadline = (_dt.now() + _td(hours=24)).isoformat()
                        # 补 step_id / step_name (T2 auto_decide 不填这两个)
                        if not getattr(decision, "step_id", ""):
                            decision.step_id = s.id
                        if not getattr(decision, "step_name", ""):
                            decision.step_name = s.name
                        r = wf.results[s.id]
                        if not hasattr(r, "metadata") or r.metadata is None:
                            r.metadata = {}
                        r.metadata["decision"] = decision
                        # 也存 wf.metadata.decision 镜像
                        wf.metadata.setdefault("decisions", []).append({
                            "step_id": decision.step_id,
                            "step_name": decision.step_name,
                            "chosen": decision.chosen,
                            "reason": decision.reason,
                            "confidence": decision.confidence,
                            "source": decision.source,
                            "ts": decision.ts,
                            "override_deadline": decision.override_deadline,
                        })
                        # emit heartbeat
                        publish_heartbeat(
                            self.event_bus, wf.id, s.id,
                            {
                                "event": "decision_made",
                                "step_id": s.id,
                                "step_name": s.name,
                                "chosen": decision.chosen,
                                "reason": decision.reason,
                                "confidence": decision.confidence,
                                "override_deadline": decision.override_deadline,
                            },
                        )
                # emit heartbeat: layer 完成
                publish_heartbeat(
                    self.event_bus, wf.id, "",
                    {
                        "event": "layer_completed",
                        "layer": layer_idx,
                        "progress": wf.progress(),
                    },
                )
                # 不 return, 让 workflow 继续 (auto-continue)
                # 24h 内 user 可调用 /workflow/override/{wid}/{step_id} 回滚

            # v1.0.6.1 修: skip_llm_executor=True 的 step (e.g. step-1 PM 调研)
            #   不调 LLM, 也不该让 workflow 继续跑后续 step (其他 step 跑会生成假数据
            #   而 PM 调研还没做完 — 业务上不合理, 调研后才有背景/数仓/建模)
            #   如果 step 还在 RUNNING 且 PM 调研未完成, pause at 当前 layer
            if any(
                getattr(s, "skip_llm_executor", False)
                for s in layer
                if wf.results.get(s.id) and wf.results[s.id].status == StepStatus.RUNNING
            ):
                md = wf.metadata if isinstance(wf.metadata, dict) else {}
                pmi = md.get("pm_interview") or {}
                if not pmi.get("done"):
                    wf.paused_at_layer = layer_idx
                    wf.status = WorkflowStatus.AWAITING_USER
                    self.event_bus.publish(WorkflowEvent(
                        type="workflow.paused",
                        workflow_id=wf.id,
                        data={"layer": layer_idx, "reason": "pm_interview_pending", "paused_steps": [s.id for s in layer if getattr(s, "skip_llm_executor", False)]},
                    ))
                    self.persistence.save_workflow(wf)
                    return wf

        # 检查 critical 失败
        has_critical_fail = any(
            r.status == StepStatus.FAILED and self._step_by_id(wf, r.step_id).critical
            for r in wf.results.values()
        )
        wf.finished_at = datetime.now().isoformat()
        wf.status = WorkflowStatus.FAILED if has_critical_fail else WorkflowStatus.COMPLETED

        # 最终事件
        self.event_bus.publish(WorkflowEvent(
            type="workflow.progress",
            workflow_id=wf.id,
            data={"status": wf.status.value, "progress": wf.progress()},
        ))
        # v1.0.8+ heartbeat: workflow 完成
        publish_heartbeat(
            self.event_bus, wf.id, "",
            {
                "event": "workflow_finished",
                "status": wf.status.value,
                "progress": wf.progress(),
            },
        )
        # v1.0.8+ 生成 executive brief 摘要 (供 T3 proactive-communicator 用)
        try:
            wf.metadata["executive_brief"] = _build_executive_brief(wf)
        except Exception:
            pass
        if hasattr(self.persistence, "add_event"):
            self.persistence.add_event(wf.id, "workflow.completed", "", {"status": wf.status.value})
        self.persistence.save_workflow(wf)
        return wf

    async def _run_step(self, wf: Workflow, step: Step, inputs: dict):
        """跑单个 step,带超时 + 重试 + 事件"""
        # 检查 abort (jump 发起后旧 run 需要退出)
        if getattr(wf, '_abort_run', False):
            return
        result = StepResult(
            step_id=step.id,
            status=StepStatus.RUNNING,
            started_at=datetime.now().isoformat(),
        )
        wf.results[step.id] = result

        self.event_bus.publish(WorkflowEvent(
            type="step.started",
            workflow_id=wf.id,
            step_id=step.id,
            data={"name": step.name, "agent": step.agent},
        ))
        # v1.0.8+ heartbeat: step 开始
        publish_heartbeat(
            self.event_bus, wf.id, step.id,
            {"event": "step_started", "step_id": step.id, "step_name": step.name, "agent": step.agent},
        )

        # v1.0.7 Phase 6: 多 agent 协作 — step 开始时查 inbox (上游给我留了什么)
        inbox_msgs = []
        try:
            from .agent_chat import AgentChat
            chat = AgentChat(wf)
            inbox_msgs = chat.inbox(step.id, step.agent or "", limit=5)
            if inbox_msgs:
                result.metadata = result.metadata or {}
                result.metadata["inbox"] = inbox_msgs
        except Exception:
            pass

        # 收集依赖 step 的输出 (深展开, 包括嵌套的 'output' 字段)
        # 例如 step-3 mock_step_executor 返回 {"output": {"rows": [...], "summary": "..."}, "step_id": ...}
        # 需要把内层 "output" 也展开, 这样 builtin 拿得到 rows
        def _flatten_outputs(d, prefix=""):
            for k, v in d.items():
                key = f"{prefix}{k}"
                if isinstance(v, dict) and v:  # 嵌套 dict 继续展开
                    # 如果 v 是 step executor output 格式 (有 'output' / 'summary'), 用 v 本身
                    if any(inner in v for inner in ("rows", "summary", "output", "data", "groups", "total", "total_dau")):
                        for ik, iv in v.items():
                            dep_outputs[f"{prefix}{ik}"] = iv
                    else:
                        _flatten_outputs(v, f"{prefix}{k}.")
                else:
                    dep_outputs[f"{prefix}{k}"] = v

        dep_outputs = {}
        for dep in step.depends_on:
            if dep in wf.results:
                out = wf.results[dep].output
                if isinstance(out, dict):
                    _flatten_outputs(out)
                else:
                    dep_outputs[dep] = out

        last_err = ""
        for attempt in range(step.max_retries + 1):
            try:
                # v1.0.7+ user 新需求: step 开始前, 拉 user pre-action (3 选 1)
                # 注入到 inputs 让 executor 知道 user 的选择
                if step.agent == "dba" or "数据源" in step.name:
                    try:
                        from .data_sources import get_data_source_store
                        action = get_data_source_store().get_action(wf.id, step.id)
                        if action.get("pre_action"):
                            dep_outputs["_user_data_action"] = action
                    except Exception:
                        pass
                # 用 executor(可注入,默认 mock)
                _exec = self.executor or mock_step_executor
                # 临时闭包 wf (v1.0.11 智能体模式需要 wf 拿 metadata)
                _exec = functools.partial(_exec, wf=wf) if _exec is mock_step_executor else _exec
                # v1.0.11 A: 把 form 跟 wf_id 注入 inputs, 让 step-3 (DBA) 真用 connector
                # v1.0.11 C+: 改成对所有 step 都注入 form (public/internal 分类 + 数据源选择都需要 form)
                _exec_inputs = {**inputs, **dep_outputs}
                md = wf.metadata if isinstance(wf.metadata, dict) else {}
                _form = md.get("form") or {}
                if _form and "form" not in _exec_inputs:
                    _exec_inputs["form"] = _form
                if step.agent == "dba" or "数据源" in (step.name or ""):
                    _exec_inputs["_workflow_id"] = wf.id
                output = await asyncio.wait_for(
                    _exec(step, _exec_inputs),
                    timeout=step.timeout_sec,
                )
                # v1.0.6.1 修: skip_llm_executor=True (e.g. step-1 PM 调研) 走"空占位"
                #   不标 completed, 后续 PM 调研结束 (auto-continue) 时 wf 会从 paused 续
                #   这里状态用 AWAITING_USER + 提示性 summary, UI 会看到 "PM 调研中"
                if step.skip_llm_executor:
                    md = wf.metadata if isinstance(wf.metadata, dict) else {}
                    pmi = md.get("pm_interview") or {}
                    if pmi.get("done") and pmi.get("report"):
                        # PM 调研已完成, 拿报告当 deliverable
                        # v1.0.6.1 修: 走 normalize_step_output 统一
                        _pm_out = normalize_step_output(
                            {}, step,
                            default_agent=step.agent or "pm",
                            default_agent_title="产品经理",
                            pm_report=pmi.get("report", ""),
                        )
                        result.status = StepStatus.COMPLETED
                        api_dict = _pm_out.to_api_dict()
                        api_dict["raw_response"] = pmi.get("report", "")[:1500]
                        result.output = api_dict
                        result.summary = _pm_out.summary
                        result.next_suggestion = _pm_out.next_suggestion or "看报告"
                    else:
                        # PM 调研未完成, 不标 completed, 走后续 paused 检查
                        result.status = StepStatus.RUNNING  # 暂标记 RUNNING, 后续会跳
                        # v1.0.6.1 修: pending 状态不走 normalize (因为没真报告可归一)
                        result.output = {
                            "step_id": step.id,
                            "agent": step.agent or "pm",
                            "agent_title": "产品经理",
                            "summary": f"[{step.name}] PM 调研中 (请看 step-1 卡片 chat)",
                            "deliverable": "PM 调研中, 请在 step-1 卡片回答 PM 的问题或点『结束调研, 生成报告』",
                            "next_suggestion": "回答 PM",
                            "source": "pm_interview",
                        }
                else:
                    # v1.0.11 fix: output 里如果 status=data_fallback, 不刷成 completed
                    _out_status = (output.get("status") or "").lower() if isinstance(output, dict) else ""
                    if _out_status == "data_fallback":
                        result.status = StepStatus.DATA_FALLBACK
                    else:
                        result.status = StepStatus.COMPLETED
                    result.output = output
                    result.summary = output.get("summary", "")
                    result.next_suggestion = output.get("next_suggestion", "")
                result.finished_at = datetime.now().isoformat()
                result.duration_sec = (
                    datetime.fromisoformat(result.finished_at)
                    - datetime.fromisoformat(result.started_at)
                ).total_seconds()
                result.retries = attempt

                # v1.0.7 Phase B: 反思循环 — LLM review 这个 step 输出
                # 失败默认 ok, 不阻塞原 step
                # v1.0.7 Phase D: needs_retry → 真的重做 (最多 max_retries+2 次)
                reflection = None
                if result.status == StepStatus.COMPLETED and not getattr(step, "skip_reflection", False):
                    try:
                        from .reflector import reflect_on_step, recall_kb_for_reflection
                        kb_ctx = recall_kb_for_reflection(step.id, step.name, step.agent or "", output)
                        reflection = await reflect_on_step(
                            step_id=step.id,
                            step_name=step.name,
                            agent=step.agent or "pm",
                            output=output,
                            summary=result.summary or "",
                            kb_context=kb_ctx,
                        )
                        # 存到 wf.metadata
                        if not hasattr(wf, "metadata") or wf.metadata is None:
                            wf.metadata = {}
                        wf.metadata.setdefault("reflections", []).append({
                            "step_id": reflection.step_id,
                            "step_name": reflection.step_name,
                            "verdict": reflection.verdict,
                            "reason": reflection.reason,
                            "feedback": reflection.feedback,
                            "confidence": reflection.confidence,
                            "kb_hits": reflection.kb_hits,
                            "ts": reflection.ts,
                            "source": reflection.source,
                        })
                        # 把 reflection 记到 result
                        result.reflection = {
                            "verdict": reflection.verdict,
                            "reason": reflection.reason,
                            "feedback": reflection.feedback,
                            "confidence": reflection.confidence,
                        }
                    except Exception as _ref_err:
                        # 反思本身崩了, 不阻塞
                        pass

                # v1.0.7.3+ Loop Engine: 反思执行 — needs_retry 真的重做
                # 升级: 限 1 次 → 限 3 次, 每次 feedback 更具体 (progressive escalation)
                # v1.0.9 T2 Smart Retry: 4 档 strategy (same_prompt / simplified_prompt / with_tool / escalate)
                # 替代 v1.0.7 "3 次同 prompt" 死循环
                _reflection_retry_count = step.metadata.get("reflection_retry_count", 0) if (hasattr(step, "metadata") and step.metadata) else 0
                if (reflection and reflection.verdict == "needs_retry"
                        and _reflection_retry_count < 4  # 4 档 strategy 覆盖到 attempt=3
                        and attempt < step.max_retries + 4):
                    # v1.0.9 T2: 调 run_smart_retry 决策 strategy
                    from .loop_engine import run_smart_retry, RetryStrategy
                    _smart = await run_smart_retry(
                        step=step,
                        attempt=_reflection_retry_count,
                        last_output=output,
                        last_error=result.error or last_err or "",
                        base_reason=reflection.reason or "",
                        base_feedback=reflection.feedback or "",
                        wf=wf,
                    )
                    if not hasattr(step, "metadata") or step.metadata is None:
                        step.metadata = {}

                    if _smart.strategy == RetryStrategy.ESCALATE.value:
                        # 兜底: 不再重试, 标 blocked, 推 proactive_message, break
                        result.status = StepStatus.BLOCKED
                        result.error = f"反思 {_reflection_retry_count} 次后仍: {reflection.verdict} - {reflection.reason[:200]}"
                        result.summary = f"需要 user 介入: {reflection.reason[:100]}"
                        step.metadata["needs_user_intervention"] = True
                        step.metadata["user_intervention_reason"] = reflection.reason[:200]
                        step.metadata["smart_retry_strategy"] = _smart.strategy
                        step.metadata["smart_retry_escalate_msg"] = _smart.escalate_msg
                        # 不 continue, 直接进后续的 "user_intervention" 块写 wf.metadata
                    else:
                        # 重做: 重置 result, 保留 reflection + 注入新 strategy 产物
                        step.metadata["last_reflection_feedback"] = _smart.augmented_feedback
                        step.metadata["reflection_retry_count"] = _reflection_retry_count + 1
                        step.metadata["reflection_retry_attempt"] = _reflection_retry_count + 1
                        step.metadata["smart_retry_strategy"] = _smart.strategy
                        if _smart.simplified_prompt:
                            step.metadata["smart_retry_simplified_prompt"] = _smart.simplified_prompt
                        if _smart.tool_results:
                            step.metadata["smart_retry_tool_results"] = _smart.tool_results
                        if _smart.tool_query:
                            step.metadata["smart_retry_tool_query"] = _smart.tool_query
                        # 不 return, 不写 completed 状态, 重新进 for 循环
                        last_err = f"reflection needs_retry (attempt {_reflection_retry_count+1}/4, strategy={_smart.strategy}): {_smart.augmented_feedback[:120]}"
                        # 重新进 loop (attempt +1)
                        continue

                # v1.0.9 T2: 4 档 strategy 全走完 (attempt >= 4) 仍 needs_retry → 标 blocked + 通知 user
                if reflection and reflection.verdict in ("needs_retry", "needs_replan") and _reflection_retry_count >= 4:
                    result.status = StepStatus.BLOCKED
                    result.error = f"反思 {result.retries} 次后仍: {reflection.verdict} - {reflection.reason[:200]}"
                    result.summary = f"需要 user 介入: {reflection.reason[:100]}"
                    if not hasattr(step, "metadata") or step.metadata is None:
                        step.metadata = {}
                    step.metadata["needs_user_intervention"] = True
                    step.metadata["user_intervention_reason"] = reflection.reason[:200]
                    # 存 wf.metadata
                    if not hasattr(wf, "metadata") or wf.metadata is None:
                        wf.metadata = {}
                    wf.metadata.setdefault("needs_user_intervention", []).append({
                        "step_id": step.id,
                        "step_name": step.name,
                        "agent": step.agent,
                        "reason": reflection.reason[:200],
                        "feedback": reflection.feedback[:200],
                        "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                    })

                self.event_bus.publish(WorkflowEvent(
                    type="step.completed" if result.status == StepStatus.COMPLETED else "step.pending_user",
                    workflow_id=wf.id,
                    step_id=step.id,
                    data={"summary": result.summary, "duration_sec": result.duration_sec},
                ))
                # v1.0.8+ heartbeat: step 完成/状态切换
                publish_heartbeat(
                    self.event_bus, wf.id, step.id,
                    {
                        "event": "step_completed" if result.status == StepStatus.COMPLETED else "step_state_changed",
                        "step_id": step.id,
                        "step_name": step.name,
                        "status": result.status.value,
                        "summary": result.summary,
                        "duration_sec": result.duration_sec,
                    },
                )
                # v1.0.7 Phase 6: step 完成后, handoff 给下游 + 存 KB lesson
                if result.status == StepStatus.COMPLETED:
                    try:
                        from .agent_chat import AgentChat
                        chat = AgentChat(wf)
                        # 找下游 (谁 depends on 我)
                        downstream = [s for s in wf.steps if step.id in (s.depends_on or [])]
                        for ds in downstream:
                            chat.handoff(
                                from_step=step.id, from_agent=step.agent or "pm",
                                to_step=ds.id, to_agent=ds.agent or "pm",
                                msg=f"[{step.name}] 完成. summary: {result.summary[:150]}",
                            )
                        # 如果反思 verdict == ok, 存 1 条 lesson 到 KB
                        if result.reflection and result.reflection.get("verdict") == "ok":
                            try:
                                from .knowledge_base import get_kb
                                kb = get_kb()
                                kb.add_entry(
                                    category="workflow",
                                    title=f"成功模式: {step.name}",
                                    content=f"step={step.id} agent={step.agent}\nsummary={result.summary[:300]}",
                                    source="auto",
                                    tags=["success_pattern", step.agent or "pm"],
                                    confidence=0.7,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                return

            except asyncio.TimeoutError:
                last_err = f"timeout after {step.timeout_sec}s"
            except Exception as e:
                last_err = str(e)
                # v1.0.10 B1: step 执行失败 (非 LLM 反思) → 走 run_smart_retry_for_execution
                if attempt < step.max_retries + 3:
                    try:
                        from .loop_engine import run_smart_retry_for_execution
                        _form = None
                        try:
                            if hasattr(wf, "metadata") and wf.metadata:
                                _form = wf.metadata.get("form")
                        except Exception:
                            _form = None
                        _exec_action = await run_smart_retry_for_execution(
                            failed_step=step, error=e, attempt=attempt,
                            wf=wf, form=_form,
                        )
                        if _exec_action.action == "replan" and _exec_action.new_steps:
                            try:
                                old_idx = next(
                                    (i for i, s in enumerate(wf.steps) if s.id == step.id),
                                    -1,
                                )
                                if old_idx >= 0:
                                    wf.steps = wf.steps[:old_idx] + list(_exec_action.new_steps) + wf.steps[old_idx + 1:]
                                    result.metadata = result.metadata or {}
                                    result.metadata["replanned"] = True
                                    result.metadata["replan_reason"] = _exec_action.reason
                                    result.error = f"replanned: {_exec_action.reason}"
                                    last_err = result.error
                                    result.status = StepStatus.FAILED
                                    break
                            except Exception:
                                pass
                        elif _exec_action.action == "block":
                            last_err = f"blocked: {_exec_action.reason} | {_exec_action.escalate_msg[:100]}"
                            try:
                                from .loop_engine import escalate_to_pm
                                _rh = []
                                if hasattr(wf, "metadata") and wf.metadata:
                                    _rh = list(wf.metadata.get("retry_history") or [])
                                escalate_to_pm(
                                    wf=wf, step=step, attempt=attempt,
                                    last_error=last_err, retry_history=_rh,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass

        # 所有重试都失败
        if step.critical:
            result.status = StepStatus.FAILED
            self.event_bus.publish(WorkflowEvent(
                type="step.failed",
                workflow_id=wf.id,
                step_id=step.id,
                data={"error": last_err, "critical": True},
            ))
        else:
            result.status = StepStatus.SKIPPED
            self.event_bus.publish(WorkflowEvent(
                type="step.blocked",
                workflow_id=wf.id,
                step_id=step.id,
                data={"error": last_err, "critical": False},
            ))

        result.error = last_err
        result.finished_at = datetime.now().isoformat()
        result.retries = step.max_retries
        self.persistence.save_workflow(wf)

        # v1.0.13.5: 老 fixed DAG step failed → 派 manager agent 接管, 防止卡死下游
        # 仅 critical 步骤 + 还在 active workflow 时触发 (避免 pause/cancel wf 误派)
        if step.critical and wf.status not in (WorkflowStatus.PAUSED, WorkflowStatus.CANCELLED, WorkflowStatus.FAILED, WorkflowStatus.COMPLETED):
            try:
                import asyncio as _aio
                from .manager_agent import run_manager_for_step
                _step_meta = (result.metadata or {}) | {
                    "fallback_reason": f"step failed: {last_err[:200]}",
                    "original_step_id": step.id,
                    "original_agent": step.agent,
                }
                try:
                    _loop = _aio.get_event_loop()
                except RuntimeError:
                    _loop = None
                if _loop and _loop.is_running():
                    # 在 async 上下文里: 直接 await
                    _loop.create_task(run_manager_for_step(
                        wf_id=wf.id,
                        step_id=step.id,
                        step_name=step.name,
                        agent=step.agent or "pm",
                        original_task=getattr(step, "custom_inputs", {}).get("task") or step.name,
                        last_error=last_err,
                        metadata=_step_meta,
                    ))
                else:
                    # 在 sync 上下文: fire-and-forget daemon thread
                    import threading
                    def _run_mgr():
                        _aio.run(run_manager_for_step(
                            wf_id=wf.id,
                            step_id=step.id,
                            step_name=step.name,
                            agent=step.agent or "pm",
                            original_task=getattr(step, "custom_inputs", {}).get("task") or step.name,
                            last_error=last_err,
                            metadata=_step_meta,
                        ))
                    threading.Thread(target=_run_mgr, daemon=True).start()
                result.status = StepStatus.FAILED  # 暂保持, manager 接管后改成 COMPLETED
                result.metadata = _step_meta
                result.metadata["manager_fallback_dispatched"] = True
                print(f"[workflow] step {step.id} failed, dispatched to manager agent (v1.0.13.5 fallback)")
            except Exception as _fb_err:
                print(f"[workflow] manager fallback dispatch failed: {_fb_err}")
        # === v1.0.13.5 manager 接管 end ===

    def _step_by_id(self, wf: Workflow, step_id: str) -> Optional[Step]:
        for s in wf.steps:
            if s.id == step_id:
                return s
        return None

    def list(self) -> list:
        return [
            {
                "id": wf.id,
                "name": wf.name,
                "status": wf.status.value,
                "progress": wf.progress(),
            }
            for wf in self.workflows.values()
        ]

    def reload_from_disk(self):
        """重启时从磁盘恢复所有 workflow"""
        import json as _json
        for d in self.persistence.load_all():
            wf = Workflow(
                id=d["id"],
                name=d["name"],
                steps=[
                    Step(
                        id=s["id"],
                        name=s["name"],
                        depends_on=s["depends_on"],
                        agent=s["agent"],
                        custom_type=s.get("custom_type"),
                        custom_inputs=s.get("custom_inputs", {}),
                        timeout_sec=s.get("timeout_sec", 60),
                        max_retries=s.get("max_retries", 2),
                        critical=s.get("critical", True),
                    )
                    for s in d["steps"]
                ],
                status=WorkflowStatus(d["status"]),
                started_at=d.get("started_at", ""),
                finished_at=d.get("finished_at", ""),
                metadata=d.get("metadata", {}),
            )
            # 恢复 results
            for sid, r in d.get("results", {}).items():
                wf.results[sid] = StepResult(
                    step_id=r["step_id"],
                    status=StepStatus(r["status"]),
                    output=r.get("output"),
                    error=r.get("error", ""),
                    summary=r.get("summary", ""),
                    started_at=r.get("started_at", ""),
                    finished_at=r.get("finished_at", ""),
                    duration_sec=r.get("duration_sec", 0),
                    retries=r.get("retries", 0),
                )
            self.workflows[wf.id] = wf

    async def continue_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """用户确认后, 从 paused_at_layer 继续跑"""
        wf = self.workflows.get(workflow_id)
        if wf is None:
            return None
        # v1.0.6.1 修: 去掉 wf.status != AWAITING_USER 检查
        #   PM 调研 done 时, wf 可能 RUNNING (layer 0 不会 pause, step-1 是 RUNNING)
        #   这时也要让 continue 接着跑, 并更新 step-1 为 COMPLETED
        if wf.status == WorkflowStatus.COMPLETED:
            return wf
        # v1.0.6.1 修: PM 调研 完成 (done=True) 时, 先把 step-1 状态填为 COMPLETED
        md = wf.metadata if isinstance(wf.metadata, dict) else {}
        pmi = md.get("pm_interview") or {}
        if pmi.get("done") and pmi.get("report"):
            s1_result = wf.results.get("step-1")
            if s1_result and s1_result.status != StepStatus.COMPLETED:
                from dataclasses import asdict as _asdict
                s1_result.status = StepStatus.COMPLETED
                # v1.0.6.1 修: 用 normalize_step_output 统一处理 PM 调研报告
                _pmi_report = pmi.get("report", "")
                _step1 = next((s for s in wf.steps if s.id == "step-1"), None)
                if _step1:
                    _pm_out = normalize_step_output(
                        {}, _step1,
                        default_agent="pm",
                        default_agent_title="产品经理",
                        pm_report=_pmi_report,
                    )
                else:
                    # fallback (不应该走这里)
                    _pm_out = normalize_step_output(
                        {"raw": _pmi_report},
                        type("FakeStep", (), {"id": "step-1", "name": "PM 调研", "agent": "pm"})(),
                        default_agent="pm",
                        default_agent_title="产品经理",
                    )
                s1_result.output = _pm_out.to_api_dict()
                s1_result.output["raw_response"] = _pmi_report[:1500]
                s1_result.summary = _pm_out.summary
                s1_result.next_suggestion = _pm_out.next_suggestion or "看报告"
                if not s1_result.finished_at:
                    s1_result.finished_at = datetime.now().isoformat()
                # 标记 step-1 完成后重新 saved
                self.persistence.save_workflow(wf)
        # 重置 paused_at_layer, 调 run (run 会从 paused+1 继续)
        # 但 paused_at_layer 在 run 里有保留, 所以 run 会从 layer+1 开始
        return await self.run(workflow_id)

    async def jump_to_step(self, workflow_id: str, target_step_id: str) -> Optional[Workflow]:
        """跳回任意 step 重跑 (该 step 之后所有 step reset 为 pending)"""
        wf = self.workflows.get(workflow_id)
        if not wf:
            return None
        # 找 target
        target_idx = None
        for i, s in enumerate(wf.steps):
            if s.id == target_step_id:
                target_idx = i
                break
        if target_idx is None:
            raise ValueError(f"step {target_step_id} not found")
        # 标志 in-flight run() 需要退出 (避免和 jump run 竞争)
        wf._abort_run = True
        await asyncio.sleep(0.5)  # 让旧 run 检测到 abort 后退出
        # 重置 target 之后所有 step 状态
        for s in wf.steps[target_idx:]:
            if s.id in wf.results:
                del wf.results[s.id]
        # 重置 workflow 状态
        wf.status = WorkflowStatus.RUNNING
        wf.paused_at_layer = -1
        wf.current_layer = 0
        wf.started_at = datetime.now().isoformat()
        wf.finished_at = ""
        wf._abort_run = False
        self.persistence.save_workflow(wf)
        # 从头跑 (但 target 之前依赖已 cached 在 wf.results / 之前已被 del)
        # 实际 run() 会从 layer 0 开始, 找到 target 时重跑
        return await self.run(workflow_id)

    # === v1.0.8+ agentic: override + skip + heartbeat ===

    async def override_step(self, workflow_id: str, step_id: str, action: str = "redo", feedback: str = "", new_payload: dict = None) -> dict:
        """User 24h 内 override auto-decision: 回滚 + 重新跑这个 step

        Args:
            workflow_id: workflow id
            step_id: 哪个 step 要重跑
            action: "redo" (重跑当前 step + 下游) / "skip" (跳过, 走下个) / "consult" (暂停等 user)
            feedback: user 反馈, 写进 metadata.override_feedback
            new_payload: 可选, 替换 step.custom_inputs (e.g. user 改完 step-6 建模方案)

        Returns:
            {ok, workflow_id, step_id, action, rolled_back_steps: [...]}
        """
        wf = self.workflows.get(workflow_id)
        if not wf:
            return {"ok": False, "error": "workflow not found"}
        step = self._step_by_id(wf, step_id)
        if not step:
            return {"ok": False, "error": f"step {step_id} not found"}
        # 检查 24h 窗口
        decision = None
        r = wf.results.get(step_id)
        if r and getattr(r, "metadata", None):
            decision = (r.metadata or {}).get("decision")
        if decision is None:
            # 也看 wf.metadata.decisions
            for d in wf.metadata.get("decisions", []):
                if d.get("step_id") == step_id:
                    decision = d
                    break
        deadline = decision.get("override_deadline") if decision else ""
        if deadline:
            from datetime import datetime as _dt
            try:
                dl = _dt.fromisoformat(deadline)
                if _dt.now() > dl:
                    return {
                        "ok": False,
                        "error": f"24h override 窗口已过 (deadline={deadline}), 不能 override",
                    }
            except Exception:
                pass
        # 写 override 记录
        wf.metadata.setdefault("overrides", []).append({
            "step_id": step_id,
            "action": action,
            "feedback": feedback,
            "new_payload": new_payload,
            "ts": datetime.now().isoformat(),
        })
        if feedback:
            wf.metadata["last_user_feedback"] = feedback
            wf.metadata["last_user_action"] = action
        # 找出受影响的 step (target + 所有下游)
        affected = get_affected_steps(wf.steps, step_id)
        # action = skip -> 标 completed 不重跑; redo -> reset + rerun; consult -> pause
        if action == "skip":
            for sid in affected:
                sr = wf.results.get(sid)
                if sr:
                    sr.status = StepStatus.COMPLETED
                    sr.output = {"skipped": True, "note": f"override skip by user: {feedback[:80]}", "step_id": sid}
                    sr.summary = f"[跳过] override skip: {feedback[:80]}"
            wf.metadata.setdefault("skipped_steps", []).extend(affected)
            self.persistence.save_workflow(wf)
            publish_heartbeat(
                self.event_bus, wf.id, step_id,
                {"event": "override_skip", "step_id": step_id, "affected": affected, "feedback": feedback},
            )
            return {"ok": True, "workflow_id": wf.id, "step_id": step_id, "action": "skip", "rolled_back_steps": []}
        if action == "consult":
            # 暂停在 step 所在 layer
            layer_idx = get_step_layer_index(wf.steps, step_id)
            wf.paused_at_layer = layer_idx
            wf.status = WorkflowStatus.AWAITING_USER
            self.persistence.save_workflow(wf)
            publish_heartbeat(
                self.event_bus, wf.id, step_id,
                {"event": "override_consult", "step_id": step_id, "feedback": feedback},
            )
            return {"ok": True, "workflow_id": wf.id, "step_id": step_id, "action": "consult", "rolled_back_steps": []}
        # default: redo
        # 1. reset 所有 affected step 的 result
        for sid in affected:
            if sid in wf.results:
                del wf.results[sid]
        # 2. 替换 custom_inputs (如果 user 给了 new_payload)
        if new_payload and isinstance(new_payload, dict):
            step.custom_inputs = {**step.custom_inputs, **new_payload}
        # 3. 重置 workflow 状态
        wf.status = WorkflowStatus.RUNNING
        wf.paused_at_layer = -1
        wf.finished_at = ""
        # 标志旧 run 退出
        wf._abort_run = True
        await asyncio.sleep(0.1)
        wf._abort_run = False
        self.persistence.save_workflow(wf)
        publish_heartbeat(
            self.event_bus, wf.id, step_id,
            {"event": "override_redo", "step_id": step_id, "affected": affected, "feedback": feedback},
        )
        # 异步重跑
        import asyncio as _aio
        _aio.create_task(self.run(workflow_id))
        return {
            "ok": True,
            "workflow_id": wf.id,
            "step_id": step_id,
            "action": "redo",
            "rolled_back_steps": affected,
        }

    async def skip_step(self, workflow_id: str, step_id: str, note: str = "") -> dict:
        """跳过当前 step, 标 completed 走下个 (新接口, 区别于 /workflow/skip-step)
        主要为前端 '下一个' 按钮用
        """
        wf = self.workflows.get(workflow_id)
        if not wf:
            return {"ok": False, "error": "workflow not found"}
        step = self._step_by_id(wf, step_id)
        if not step:
            return {"ok": False, "error": f"step {step_id} not found"}
        r = wf.results.get(step_id) or StepResult(
            step_id=step_id, status=StepStatus.SKIPPED,
        )
        r.status = StepStatus.COMPLETED
        if r.output is None:
            r.output = {}
        if isinstance(r.output, dict):
            r.output["skipped"] = True
            r.output["note"] = note or f"user 选择跳过 {step_id}"
        r.summary = f"[跳过] {note or f'user 选择跳过 {step_id}'}"
        wf.results[step_id] = r
        wf.metadata.setdefault("user_actions", []).append({
            "action": "skip_step",
            "step_id": step_id,
            "note": note,
            "ts": datetime.now().isoformat(),
        })
        # emit heartbeat
        publish_heartbeat(
            self.event_bus, wf.id, step_id,
            {"event": "skipped", "step_id": step_id, "note": note},
        )
        self.persistence.save_workflow(wf)
        return {"ok": True, "workflow_id": wf.id, "step_id": step_id, "action": "skipped", "note": note}

    def heartbeat_snapshot(self, workflow_id: str) -> dict:
        """拿当前 workflow 的心跳快照 (SSE 初始 payload 用)

        Returns:
            {
              workflow_id, status, current_layer, paused_at_layer, progress,
              steps: [...],  # 每步 status / progress / decision / override 倒计时
              recent_events: [...],  # 最近 N 个事件
            }
        """
        wf = self.workflows.get(workflow_id)
        if not wf:
            return {"workflow_id": workflow_id, "error": "not found"}
        steps_info = []
        for s in wf.steps:
            r = wf.results.get(s.id)
            entry = {
                "step_id": s.id,
                "name": s.name,
                "status": r.status.value if r else "pending",
                "summary": r.summary if r else "",
                "agent": s.agent,
            }
            # decision + override deadline
            if r and getattr(r, "metadata", None):
                dec = (r.metadata or {}).get("decision")
                if dec:
                    entry["decision"] = {
                        "chosen": dec.chosen,
                        "reason": dec.reason,
                        "confidence": dec.confidence,
                        "source": dec.source,
                        "ts": dec.ts,
                        "override_deadline": dec.override_deadline,
                    }
                    # 算还剩几秒
                    if dec.override_deadline:
                        from datetime import datetime as _dt
                        try:
                            dl = _dt.fromisoformat(dec.override_deadline)
                            entry["decision"]["seconds_remaining"] = max(0, int((dl - _dt.now()).total_seconds()))
                        except Exception:
                            pass
            steps_info.append(entry)
        return {
            "workflow_id": wf.id,
            "status": wf.status.value,
            "current_layer": wf.current_layer,
            "paused_at_layer": wf.paused_at_layer,
            "progress": wf.progress(),
            "steps": steps_info,
            "decisions": wf.metadata.get("decisions", []),
            "overrides": wf.metadata.get("overrides", []),
            "executive_brief": wf.metadata.get("executive_brief", ""),
        }


def _build_executive_brief(wf: Workflow) -> str:
    """v1.0.8+ workflow 跑完时生成简短 executive brief

    1-3 句话总结, 供 T3 proactive-communicator 推送给 user
    """
    lines = [f"## {wf.name}", "", f"状态: {wf.status.value} | {wf.progress().get('percent', '0%')} 完成", ""]
    # 关键 step summary (CRITICAL 且 completed)
    for s in wf.steps:
        r = wf.results.get(s.id)
        if r and r.status == StepStatus.COMPLETED and s.critical:
            lines.append(f"- **{s.name}** ({s.agent}): {r.summary or '已完成'}")
    if not any(r and r.status == StepStatus.COMPLETED and s.critical for s in wf.steps for r in [wf.results.get(s.id)]):
        lines.append("(无 critical step 输出)")
    return "\n".join(lines)
