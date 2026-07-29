"""
FastAPI Server — Chat UI 后端
路由:
  POST /api/chat/new         - 新会话,首次问需求
  POST /api/chat/{sid}/step  - 推进一步(多轮用)
  GET  /api/chat/{sid}       - 查状态
  GET  /api/chart/{name}     - 查图表 PNG
  GET  /                    - 首页 HTML
  GET  /static/*             - 静态资源
"""
import os
import json
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent.workflow import DataAnalystWorkflow
from agent.llm_client import LLMClient

BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

app = FastAPI(title="Data Analyst Agent", version="0.1.0")
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

    # 如果已 ready,自动跑完后续阶段
    if st.phase not in ("clarifying", "done", "error"):
        st = workflow.run_through(session_id)

    is_question = (st.phase == "clarifying")
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
    path = os.path.join(REPORTS_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "chart not found")
    return FileResponse(path, media_type="image/png")


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
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
