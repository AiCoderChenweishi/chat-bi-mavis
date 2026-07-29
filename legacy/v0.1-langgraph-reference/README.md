# v0.1 LangGraph Reference (DEPRECATED 2026-07-29)

> ⚠️ **DEPRECATED** — 2026-07-29 起归档, 不再维护。  
> 真实项目已迁到 `chat-bi-mavis` 仓库根目录 (4 agent 线性 workflow + DuckDB + ECharts)。  
> 本目录是 **历史参考** — 防止代码彻底丢失, 仅供阅读, 不再部署。

## 是什么 (2026-07-29 前)

`AiCoderChenweishi/data-analyst-mavis` 仓库 v1.0.x (LangGraph state machine + 11 agent 角色),  
部署在 **`8.153.192.136:7892`** (Phase 1 dev 限定)。

**架构**:
```
START → plan → dispatch_worker → run_worker → check_status → (loop | mark_done) → END
```

**11 个 agent 角色** (`agents/00-README.md` 介绍):
- **核心**: 01-pm (需求澄清) / 02-ba (业务分析) / 03-da (数据分析) / 04-dba (数仓) / 05-bi (商业洞察)
- **执行**: 06-str (策略) / 07-rec (建议)
- **支持**: 08-fe / 09-be / 10-ops

**关键技术**:
- `manager_langgraph.py` — LangGraph state machine (5 节点 + TypedDict state)
- `agent_runner.py` — 通用 agent 执行 (tool loop + retry + 反思)
- `agent_tools.py` — 工具集 (ask_user / query_data_source / preview / crawl_public)
- `knowledge_base.py` — v1 KB (跟 v0.2 chat-bi-mavis 不兼容, **别混用**)
- `workflow.py` — 16 步 DAG + EventBus + LockManager (异步 + 多租户)

## 跟 chat-bi-mavis v0.2 的关系

| 维度 | v0.1 (本目录, deprecated) | v0.2 (chat-bi-mavis, 当前) |
|------|--------------------------|---------------------------|
| **架构** | LangGraph 5 节点 state machine | 线性 4 agent 状态机 |
| **Agent 数** | 11 (PM/BA/DA/DBA/BI/STR/REC/FE/BE/OPS) | 4 (clarify/understand/sql/conclude) |
| **KB** | v1 (server 端 sqlite) | v0.2 (同, 兼容升级) |
| **RAG 召回** | 5 iter 兜底 + 反思 | 调 LLM 前 recall_for_context |
| **DBA** | 独立 agent (数仓沟通) | 内嵌进 understand agent |
| **BI 报告** | agent 直接出 | V2 BI 报告 + 反方意见 |
| **页面存储** | v1.0.15.5 page_extract button | v0.2 升级 (json_mode=False 兼容) |
| **部署** | 8.153.192.136:7892 (Phase 1 dev) | 8.153.192.136:8000 (dev) + 117.72.40.22:8000 (prod) |

## 为什么 deprecated

1. **架构过度工程**: 16 步 DAG + 11 agent + 5 节点 state machine, 对一个 4 阶段数据查询流程过重
2. **LangGraph 依赖链脆弱**: 装到 prod server 缺 8+ no-deps 包, Python 3.14 ↔ langgraph<0.5 兼容性坑
3. **minimax LLM 飘忽**: 在 5 agent 编排下容易陷入 tool loop, deliverable 假成功 (placeholder_count=0)
4. **真实使用不需要这么多 agent**: user 实际场景 = 反问 3 轮 → 出 SQL → 出报告, 4 agent 足够
5. **user 实际访问的是 8000 端口的 data-analyst-agent 项目**, 7892 一周没真用户访问, 改了个寂寞

## 何时回看本目录

- 想研究 LangGraph state machine 在数据 agent 场景怎么用 → 看 `mavis/manager_langgraph.py`
- 想对比 5 agent 协作 vs 4 agent 线性 → 看 `docs/v1.0.15-langgraph.md`
- 想看 v0.1 RAG 召回实现 (跟 v0.2 不一样) → 看 `mavis/knowledge_base.py` + `docs/v1.0.15.4-rag-closed-loop.md`
- 想拿 11 个 agent persona 模板 → 看 `agents/*.md` (跟 v0.2 的 4 个 prompt 对比)

## 不要做的事

- ❌ 不要把本目录的代码 cherry-pick 回 chat-bi-mavis 根目录
- ❌ 不要重启 8.153.192.136:7892 (2026-07-29 已 kill)
- ❌ 不要在 chat-bi-mavis 用 v0.1 KB (跟 v0.2 schema 冲突)
- ❌ 不要拿 v0.1 prompts 替换 v0.2 prompts (5 agent → 4 agent, 结构变)

## 归档时间线 (2026-07-29)

- 18:00 — `data-analyst-agent` (老) 仓库加 deprecated 标 (fa80c6b v0.2.1)
- 18:13 — `117.72.40.22:8000` server 切远端到 chat-bi-mavis
- 18:17 — `8.153.192.136:8000` server 切远端到 chat-bi-mavis
- 18:20 — **本目录 v0.1 LangGraph reference 创建** (旧 mavis-dev 代码归档)
- 18:25 — `8.153.192.136:7892` mavis-dev 服务 kill, `/opt/mavis-dev` 归档
- 18:30 — `117.72.40.22:8000` 加 systemd 自动启动
- 18:35 — `chat-bi-mavis` 仓库 push v0.2.2 (legacy reference 加进来)
