"""Agent runner (v1.0.11+) - 真智能体模式

不是固定 executor 路径, 而是给 LLM 一组 tool, 让它自主决策:
  LLM 看到 task → 决定调哪个 tool → 拿结果 → 再决定 → 循环到 finish_reason='stop'

这是从"剧本模式" 改成"智能体模式" 的核心.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .agent_tools import ToolRegistry, get_global_registry
from .llm import get_llm, is_llm_available

log = logging.getLogger("mavis.agent_runner")


@dataclass
class AgentStep:
    """agent 跑一步的轨迹 (调试 + UI 显示用)"""
    iter: int
    role: str  # "llm_call" | "tool_call" | "final"
    content: str = ""
    tool_name: Optional[str] = None
    tool_args: Optional[Dict] = None
    tool_result: Optional[Dict] = None
    duration_sec: float = 0.0
    error: Optional[str] = None


@dataclass
class AgentRunResult:
    """agent 跑完的结果"""
    final_content: str = ""               # LLM 最后一次 text 输出
    final_data: Dict[str, Any] = field(default_factory=dict)  # 解析出的 deliverable (如果有)
    tool_calls_made: List[Dict] = field(default_factory=list)  # [ {name, args, result} ]
    steps: List[AgentStep] = field(default_factory=list)
    total_iters: int = 0
    is_fallback: bool = False
    fallback_reason: str = ""
    error: Optional[str] = None
    placeholder_count: int = 0    # v1.0.13.4: final_content 里 null/占位符计数
    placeholder_paths: List[str] = field(default_factory=list)  # 具体位置 (e.g. "monthly_trend[9].avg_dau")


async def run_agent(
    system_prompt: str,
    user_task: str,
    tool_names: List[str],
    context: Optional[Dict] = None,
    max_iters: int = 8,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    on_step: Optional[Callable[[AgentStep], None]] = None,
    registry: Optional[ToolRegistry] = None,
    agent_role: str = "",  # v1.0.15.3: 用于"先 ask_user 再 query" 强制 (DA/BA/BI)
) -> AgentRunResult:
    """跑 agent loop: LLM 自主调 tool 直到 stop

    Args:
        system_prompt: agent 角色 system prompt
        user_task: 给 agent 的具体任务
        tool_names: 该 agent 能用的 tool 列表 (从 get_global_registry().get(name))
        context: 额外 context (e.g. form, 前序 step 输出)
        max_iters: 最大循环次数 (防 LLM 永远不 finish)
        temperature: LLM 温度
        max_tokens: LLM 每次 max_tokens
        on_step: 每步回调 (UI 实时显示)
        registry: tool 注册中心, 默认 global

    Returns:
        AgentRunResult: 含 final_content / tool_calls_made / steps / error
    """
    result = AgentRunResult()
    reg = registry or get_global_registry()
    tools = reg.to_openai_tools(names=tool_names)

    if not tools:
        result.error = f"agent 没注册任何 tool (names={tool_names})"
        _finalize_scan(result)
        return result

    # 拼 messages
    context_str = ""
    if context:
        context_str = "\n\n## 上下文:\n" + json.dumps(context, ensure_ascii=False, default=str)[:3000]
    messages: List[Dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_task + context_str},
    ]

    if not is_llm_available():
        result.error = "LLM 不可用 (api_key 缺失)"
        _finalize_scan(result)
        return result

    llm = get_llm()

    for it in range(max_iters):
        result.total_iters = it + 1
        t0 = time.time()
        try:
            # 跑 LLM (chat_async 包 to_thread, 不阻塞 event loop)
            resp = await llm.chat_async(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as e:
            err = f"LLM 失败: {str(e)[:200]}"
            step = AgentStep(iter=it, role="llm_call", error=err, duration_sec=time.time() - t0)
            result.steps.append(step)
            if on_step:
                on_step(step)
            result.error = err
            _finalize_scan(result)
            return result

        content = resp.get("content", "") or ""
        tool_calls = resp.get("tool_calls") or []
        finish_reason = resp.get("finish_reason", "stop")
        step = AgentStep(
            iter=it,
            role="llm_call",
            content=content[:500],
            duration_sec=time.time() - t0,
        )
        result.steps.append(step)
        if on_step:
            on_step(step)

        # 1. LLM 决定 finish (没 tool_calls)
        if not tool_calls:
            # v1.0.15.4 RAG 闭环: 5 agent 没调 save_kb_entry 就 finish → 强制续跑 1 轮
            # minimax 飘忽: 1 轮 final_content, 忘 save
            # 治本: 把 final_content 转成 "system reminder" 强制调 save_kb_entry 再 finish
            if agent_role in ("pm", "ba", "dba", "da", "bi") and agent_role != "":
                has_save = any(tc.get("name") == "save_kb_entry" for tc in result.tool_calls_made)
                if not has_save and it < max_iters - 1:
                    # 不 return, 强制下一轮
                    reminder = f"v1.0.15.4 RAG 闭环强约束: 你要 finish 但还没调 save_kb_entry! 必须先调 save_kb_entry 工具把 1 条结论写入知识库 (category: insight/方法论/数据规律/模板/竞品/行业, title: 10-30字, content: 50-500字含数据/方法). 写完再 final_content."
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": reminder})
                    # log 一下
                    import sys as _sys
                    print(f"[RAG-fallback] {agent_role} iter {it+1}: 强制 save_kb_entry", file=_sys.stderr, flush=True)
                    continue  # 续跑下一轮
            result.final_content = content
            step.role = "final"
            _finalize_scan(result)
            return result

        # 2. LLM 决定调 tool(s) - 加入 assistant 消息
        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": tool_calls,
        })

        # 3. 执行每个 tool_call, 把结果塞回 messages
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            fn = tc.get("function", {})
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}") or "{}")
            except Exception:
                fn_args = {}

            t1 = time.time()
            tool = reg.get(fn_name)
            if tool is None:
                tool_result = {"ok": False, "error": f"tool '{fn_name}' not registered"}
            else:
                tool_result = tool.run(**fn_args)
            dur = time.time() - t1

            tool_step = AgentStep(
                iter=it,
                role="tool_call",
                tool_name=fn_name,
                tool_args=fn_args,
                tool_result=tool_result,
                duration_sec=dur,
            )
            result.steps.append(tool_step)
            if on_step:
                on_step(tool_step)
            result.tool_calls_made.append({
                "name": fn_name,
                "args": fn_args,
                "result": tool_result,
                "duration_sec": dur,
            })

            # v1.0.15.3 强约束: DA/BA/BI agent 没调过 ask_user 之前, 不准调 query_data_source
            if agent_role in ("da", "ba", "bi") and fn_name in ("query_data_source", "preview_data_source"):
                if not any(tc.get("name") == "ask_user" for tc in result.tool_calls_made):
                    tool_result = {"ok": False, "error": "v1.0.15.3 强约束: DA/BA/BI agent 必须先调 ask_user tool 确认口径才能查数据. 你现在调了 query 但 ask_user 还没调过, 请先调 ask_user 问 user 1 个关键问题 (时间窗口/指标定义/维度/过滤/聚合 5 维度之一), 等 user 回答再继续."}
                    result.steps[-1].tool_result = tool_result
                    messages[-1] = {"role": "tool", "tool_call_id": tc_id, "content": json.dumps(tool_result, ensure_ascii=False)[:4000]}

            # v1.0.15.4 RAG 闭环: 5 agent (pm/ba/dba/da/bi) 跑了 5 iter 还没 save_kb_entry → 强制 reminder
            #   minimax 飘忽: 跑 1 轮 final_content, 忘 save_kb_entry
            #   server 端兜底: 跑了 N iter 还没调 save_kb_entry → tool result 返 error 强制调
            if agent_role in ("pm", "ba", "dba", "da", "bi") and agent_role != "":
                has_save = any(tc.get("name") == "save_kb_entry" for tc in result.tool_calls_made)
                iter_count = it + 1
                if not has_save and iter_count >= 5:
                    tool_result = {"ok": False, "error": f"v1.0.15.4 RAG 闭环强约束: {agent_role} agent 跑了 {iter_count} 轮还没调 save_kb_entry 写结论到知识库. 你已经做完分析, 必须调 save_kb_entry 工具把 1 条结论写入知识库 (category: insight/方法论/数据规律/模板/竞品/行业, title: 10-30字, content: 50-500字含数据/方法). 写完再 final_content 结束 task."}
                    result.steps[-1].tool_result = tool_result
                    messages[-1] = {"role": "tool", "tool_call_id": tc_id, "content": json.dumps(tool_result, ensure_ascii=False)[:4000]}

            # v1.0.15.4 强约束: PM agent 必须先 ask_user 才能 done
            #   minimax 飘忽 — 跑 search_kb 后一直不调 ask_user
            #   server 端兜底: PM 跑了 3 轮还没调过 ask_user → 强制 error 提示
            if agent_role == "pm":
                has_ask = any(tc.get("name") == "ask_user" for tc in result.tool_calls_made)
                iter_count = it + 1
                if not has_ask and iter_count >= 3:
                    tool_result = {"ok": False, "error": "v1.0.15.4 强约束: PM agent 跑了 3 轮还没调过 ask_user 确认 user 需求. 你现在调了 search_kb 但 ask_user 还没调过, 请先调 ask_user 问 user 1 个关键问题 (目标/口径/范围/期望产出), 等 user 回答再继续. 注意: search_kb 是 RAG 召回, 不替代 ask_user."}
                    result.steps[-1].tool_result = tool_result
                    messages[-1] = {"role": "tool", "tool_call_id": tc_id, "content": json.dumps(tool_result, ensure_ascii=False)[:4000]}
                # 重复 search_kb query 防循环
                if fn_name == "search_kb":
                    recent = [tc.get("args", {}).get("query", "") for tc in result.tool_calls_made[-4:] if tc.get("name") == "search_kb"]
                    if recent.count(fn_args.get("query", "")) >= 2:
                        tool_result = {"ok": False, "error": f"v1.0.15.4 强约束: 你已经跑了 2 次完全相同的 search_kb query '{fn_args.get('query','')[:50]}', 这是循环. 请换不同 query 或直接调 ask_user 问 user."}
                        result.steps[-1].tool_result = tool_result
                        messages[-1] = {"role": "tool", "tool_call_id": tc_id, "content": json.dumps(tool_result, ensure_ascii=False)[:4000]}

            # 把 tool result 加回 messages (OpenAI 格式)
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": json.dumps(tool_result, ensure_ascii=False, default=str)[:4000],
            })

    # 达到 max_iters 还没 finish - 警告
    result.error = f"agent 跑了 {max_iters} 轮还没 finish, 强制结束 (可能 LLM 卡循环)"
    result.is_fallback = True
    result.fallback_reason = result.error
    _finalize_scan(result)
    return result




def _finalize_scan(result: "AgentRunResult") -> None:
    """扫 final_content 的 placeholder, 设 placeholder_count + paths"""
    if not result.final_content:
        return
    data = extract_final_data(result.final_content)
    paths = _scan_placeholders(data)
    if paths:
        result.placeholder_count = len(paths)
        result.placeholder_paths = paths[:10]  # 最多 10 个
        result.is_fallback = True
        result.fallback_reason = f"DA/worker 输出了 {len(paths)} 个占位符 (e.g. {paths[0]!r}), 数据不完整"


def extract_final_data(content: str) -> Dict[str, Any]:
    """从 LLM final content 提取结构化 deliverable

    尝试:
    1. 严格 JSON
    2. markdown code block ```json ... ```
    3. 兜底: {content: content}
    """
    content = (content or "").strip()
    # 1. 严格 JSON
    if content.startswith("{"):
        try:
            return json.loads(content)
        except Exception:
            pass
    # 2. markdown code block
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return {"content": content}


_PLACEHOLDER_VALUES = {"待补充", "N/A", "n/a", "未知", "未知数据", "略", "...", "暂无", "暂无数据", "见报告", "TBD", "tbd", "—", "-", "null", "None"}


def _scan_placeholders(obj, path: str = "") -> List[str]:
    """递归扫 dict/list, 找 null/占位符值, 返 path 列表 (e.g. "monthly_trend[9].avg_dau")

    占位符: None / "待补充" / "N/A" / "未知" / "略" / "TBD" 等
    """
    paths = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            paths.extend(_scan_placeholders(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            paths.extend(_scan_placeholders(v, f"{path}[{i}]"))
    elif obj is None:
        paths.append(path)
    elif isinstance(obj, str):
        s = obj.strip()
        # 检查 null 字符串 / 占位符
        if s == "" or s in _PLACEHOLDER_VALUES or s.lower() in _PLACEHOLDER_VALUES:
            paths.append(path)
    return paths


def scan_deliverable_placeholders(data: Dict) -> List[str]:
    """扫 deliverable dict 找 null/占位符, 返 path 列表

    用法: paths = scan_deliverable_placeholders(result.deliverable)
    """
    return _scan_placeholders(data)
