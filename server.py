"""
FastAPI Server — Chat UI 后端
路由:
  POST /api/chat/new         - 新会话,首次问需求
  POST /api/chat/{sid}/step  - 推进一步(多轮用)
  GET  /api/chat/{sid}       - 查状态
  GET  /api/chart/{name}     - 查图表 PNG
  GET  /api/echart/{sid}     - ECharts option JSON
  GET  /                    - 首页 HTML
  GET  /health              - 健康
  GET  /static/*             - 静态资源

v0.2 KB + RAG (2026-07-29):
  GET  /api/kb/stats                  - KB 统计
  GET  /api/kb/list                   - 列表
  GET  /api/kb/search                 - 搜
  POST /api/kb/add                    - 手动加
  DELETE /api/kb/{id}                 - 删
  POST /api/kb/recall                 - RAG 召回给 LLM
  POST /api/extract-insight-from-page - UI button 触发, AI 总结页面存 KB
"""
import os
import json
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.workflow import DataAnalystWorkflow
from agent.llm_client import LLMClient
from agent.knowledge_base import get_kb

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

app = FastAPI(title="Data Analyst Agent", version="0.2.0")
workflow = DataAnalystWorkflow()


class NewChatReq(BaseModel):
    message: str


class StepReq(BaseModel):
    message: Optional[str] = None


class NewChatResp(BaseModel):
    session_id: str
    state: dict
    reply: str
    phase: str
    is_question: bool


class KBEntryIn(BaseModel):
    category: str
    title: str
    content: str
    source: str = "manual"
    tags: Optional[List[str]] = None
    confidence: float = 0.8


class KBRecallIn(BaseModel):
    query: str
    limit: int = 5
    category: Optional[str] = None


class PageExtractIn(BaseModel):
    page_text: str
    source_label: str = "page_top"  # page_top | report_area | chat_area
    max_chars: int = 12000


# ---- API ----

@app.post("/api/chat/new", response_model=NewChatResp)
def chat_new(req: NewChatReq):
    """开新会话,跑一轮"""
    st = workflow.new_session(req.message)
    workflow.step(st.session_id, req.message)
    is_question = (st.phase == "clarifying")
    return NewChatResp(
        session_id=st.session_id,
        state=st.to_public_dict(),
        reply=st.assistant_message,
        phase=st.phase,
        is_question=is_question,
    )


@app.post("/api/chat/{session_id}/step", response_model=NewChatResp)
def chat_step(session_id: str, req: StepReq):
    """继续推进一轮"""
    try:
        st = workflow.step(session_id, req.message or "")
    except ValueError as e:
        raise HTTPException(404, str(e))

    # 如果 phase 已经在 ready 之后的阶段(数仓理解/SQL/执行/图表/结论),自动跑到底
    # 只在 clarifying / awaiting_confirmation 停
    if st.phase not in ("clarifying", "awaiting_confirmation", "done", "error"):
        st = workflow.run_through(session_id)

    is_question = st.phase in ("clarifying", "awaiting_confirmation")
    return NewChatResp(
        session_id=st.session_id,
        state=st.to_public_dict(),
        reply=st.assistant_message or _phase_default_message(st.phase),
        phase=st.phase,
        is_question=is_question,
    )


@app.get("/api/chat/{session_id}")
def chat_get(session_id: str):
    try:
        st = workflow.get_state(session_id)
    except KeyError:
        raise HTTPException(404, "session not found")
    return st.to_public_dict()


def _phase_default_message(phase: str) -> str:
    return {
        "warehouse_understanding": "正在理解数仓,选表定位中...",
        "sql_generating": "正在生成 SQL...",
        "executing": "正在执行查询...",
        "visualizing": "正在生成图表...",
        "writing_conclusion": "正在撰写分析结论...",
        "done": "分析完成",
        "error": "出错了",
    }.get(phase, phase)


# ---- 静态资源 ----

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/chart/{name}")
def chart_get(name: str):
    """兼容老路由 — PNG 图表(老格式,fallback)"""
    path = os.path.join(REPORTS_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "chart not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/echart/{session_id}")
def echart_get(session_id: str):
    """ECharts option JSON 端点(主路由)"""
    import json
    path = os.path.join(REPORTS_DIR, "echarts", f"{session_id}.json")
    if not os.path.exists(path):
        raise HTTPException(404, "echart not found")
    with open(path, encoding="utf-8") as f:
        option = json.load(f)
    return JSONResponse(content=option)


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        with open(html_path) as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Data Analyst Agent</h1><p>前端待开发</p>")


@app.get("/health")
def health():
    return {
        "ok": True,
        "llm_provider": workflow.llm.provider,
        "is_mock_llm": workflow.llm.is_mock(),
        "kb_total": get_kb().get_stats()["total"],
    }


# ============================================================
# v0.2 KB 知识库 API (2026-07-29)
# ============================================================

@app.get("/api/kb/stats")
def kb_stats():
    return get_kb().get_stats()


@app.get("/api/kb/list")
def kb_list(limit: int = 20, category: Optional[str] = None):
    entries = get_kb().list_recent(limit=limit, category=category)
    return {
        "count": len(entries),
        "entries": [
            {
                "id": e.id, "category": e.category, "title": e.title,
                "content": e.content, "source": e.source, "tags": e.tags,
                "confidence": e.confidence, "created_at": e.created_at,
                "updated_at": e.updated_at,
            }
            for e in entries
        ],
    }


@app.get("/api/kb/search")
def kb_search(q: str, limit: int = 10, category: Optional[str] = None):
    entries = get_kb().search(q, limit=limit, category=category)
    return {
        "query": q,
        "count": len(entries),
        "entries": [
            {
                "id": e.id, "category": e.category, "title": e.title,
                "content": e.content, "source": e.source, "tags": e.tags,
                "confidence": e.confidence,
            }
            for e in entries
        ],
    }


@app.post("/api/kb/add")
def kb_add(body: KBEntryIn):
    eid = get_kb().add_entry(
        category=body.category, title=body.title, content=body.content,
        source=body.source, tags=body.tags, confidence=body.confidence,
    )
    return {"ok": True, "id": eid}


@app.get("/api/kb/{entry_id}")
def kb_get(entry_id: int):
    """单条 KB 详情 (UI 详情页用)"""
    e = get_kb().get(entry_id)
    if not e:
        raise HTTPException(404, f"KB entry {entry_id} not found")
    return {
        "id": e.id, "category": e.category, "title": e.title,
        "content": e.content, "source": e.source, "tags": e.tags,
        "confidence": e.confidence, "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


@app.delete("/api/kb/{entry_id}")
def kb_delete(entry_id: int):
    ok = get_kb().delete_entry(entry_id)
    return {"ok": ok}


@app.get("/api/kb/categories/list")
def kb_categories():
    """所有 category 列表 (UI 下拉过滤用)"""
    stats = get_kb().get_stats()
    return {"categories": list(stats.get("by_category", {}).keys())}


@app.post("/api/kb/recall")
def kb_recall(body: KBRecallIn):
    md = get_kb().recall_for_context(body.query, limit=body.limit)
    return {"query": body.query, "markdown": md, "category": body.category}


# ============================================================
# v0.2 页面内容一键存 KB (UI button 触发)
# ============================================================

def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    """minimax 返的可能: 1) 裸 JSON 2) ```json ... ``` 包装 3) 含前后杂文字
    4) bullet list 格式 (- key: value) — 拼成 dict 兜底"""
    if not raw:
        return None
    s = raw.strip()
    # 1. 裸 JSON
    try:
        return json.loads(s)
    except Exception:
        pass
    # 2. ```json ... ``` 包装
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s, re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3. 找第一个 { 到最后一个 }
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(s[a:b + 1])
        except Exception:
            pass
    # 4. bullet list / markdown 兜底 — LLM 返的格式: 段头 (## 洞察) + bullet list (- text)
    #    拼成 {category: 段头, title: 第一行, content: 全文, tags: 抽关键词}
    out: Dict[str, Any] = {}
    category = None
    title = None
    body_lines: List[str] = []
    for line in s.splitlines():
        line = line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        # 段头: ### 洞察 / ## 洞察 / # 洞察
        m = re.match(r"^#{1,3}\s*([\w\u4e00-\u9fa5]+)\s*$", stripped)
        if m:
            category = m.group(1).strip()
            continue
        # 标题: 第一行非空非 bullet 非 markdown 标记 的纯文字
        if title is None and not stripped.startswith(("-", "*", "•", "·", ">", "```", "#")):
            # 不含 "key: value" 形式
            if not re.match(r"^[\w\u4e00-\u9fa5]+\s*[:：]\s*\S", stripped):
                title = stripped
                continue
        # bullet: - xxx / * xxx / • xxx
        if stripped.startswith(("-", "*", "•", "·")):
            content_line = re.sub(r"^[-*•·]\s*", "", stripped)
            body_lines.append(content_line)
            continue
        # 其它
        body_lines.append(stripped)
    if body_lines:
        out["content"] = "\n".join(body_lines)
    if category:
        out["category"] = category
    if title:
        out["title"] = title
    # tags: 抽 category 里的关键词 + content 里常出现的 2-4 字中文词
    if "tags" not in out and out.get("content"):
        tags = re.findall(r"[\u4e00-\u9fa5]{2,4}", out["content"])
        freq: Dict[str, int] = {}
        for t in tags:
            freq[t] = freq.get(t, 0) + 1
        # 取出现 ≥2 次的 top 5
        top = sorted(freq.items(), key=lambda x: -x[1])
        out["tags"] = [t for t, c in top if c >= 2][:5]
    if out:
        return out
    return None


@app.post("/api/extract-insight-from-page")
def extract_insight_from_page(body: PageExtractIn):
    """UI 端 '存储洞察到知识库' button 触发 (v0.2 2026-07-29)

    流程:
    1. 收 page_text (整个页面 或 报告区) + source_label
    2. 调 LLM 总结成 KBEntry JSON
    3. 调 KB.add_entry (source='page_extract:<label>')
    4. 返 {ok, id, category, title, ...}
    """
    page_text = (body.page_text or "").strip()
    if not page_text:
        return {"ok": False, "error": "page_text is empty"}
    if len(page_text) > body.max_chars:
        page_text = page_text[:body.max_chars] + "\n...[内容已截断, 仅取前 12K 字符]"

    # 加载专用的 page-extract prompt
    prompt_path = os.path.join(BASE_DIR, "prompts", "05_kb_extractor.md")
    sys_prompt = ""
    if os.path.exists(prompt_path):
        with open(prompt_path) as f:
            sys_prompt = f.read()

    user_prompt = f"网页内容来源: {body.source_label}\n\n{page_text}"
    messages = [
        {"role": "system", "content": sys_prompt or "你是知识整理专家, 输出严格 JSON {category, title, content, tags, confidence}"},
        {"role": "user", "content": user_prompt},
    ]

    fallback_str = '{"category":"洞察","title":"页面提取失败","content":"LLM 调用失败, 内容为占位符","tags":["提取失败"],"confidence":0.2}'

    # 走 LLMClient (跟现有 4 agent 用同 LLM)
    # 注: minimax abab5.5s 不严格支持 response_format={"type":"json_object"}, 用 json_mode=False 走自由格式
    r = workflow.llm.call(sys_prompt or messages[0]["content"], user_prompt, json_mode=False, temperature=0.2)

    raw = (r or "").strip()
    parsed = _parse_llm_json(raw)
    if not parsed:
        return {"ok": False, "error": "LLM JSON parse failed", "raw": raw[:500]}

    # 容错
    category = (parsed.get("category") or "洞察").strip()
    if category not in ("洞察", "方法论", "数据结果", "模板", "行业", "竞品"):
        category = "洞察"
    title = (parsed.get("title") or "").strip()
    if not title or len(title) < 5:
        title = f"页面提取 {body.source_label} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    title = title[:80]
    content = (parsed.get("content") or "").strip()
    if not content or len(content) < 20:
        content = f"[内容过短] {content[:200]}"
    if len(content) > 2000:
        content = content[:2000]
    tags = parsed.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    if not isinstance(tags, list):
        tags = []
    tags = [str(t)[:20] for t in tags[:8]]
    try:
        confidence = float(parsed.get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))

    eid = get_kb().add_entry(
        category=category, title=title, content=content,
        source=f"page_extract:{body.source_label}",
        tags=tags, confidence=confidence,
    )
    return {
        "ok": True,
        "id": eid,
        "category": category, "title": title, "content": content,
        "tags": tags, "confidence": confidence,
        "source_label": body.source_label,
        "llm_provider": workflow.llm.provider,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
