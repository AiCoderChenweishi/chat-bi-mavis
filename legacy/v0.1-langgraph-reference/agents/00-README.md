# Mavis Agent Personas (v0.1 deprecated)

> ⚠️ **DEPRECATED 2026-07-29** — 这 11 个 persona 是 v0.1 LangGraph 5 agent 编排用的,  
> chat-bi-mavis v0.2 已迁到 4 agent 线性 workflow (clarify/understand/sql/conclude)。  
> 本目录仅作历史参考, 不再部署 / 不再调用。

## 核心 agent (5)
- **01-pm** — Product Manager: 需求澄清 + 反问 (5 维度口径)
- **02-ba** — Business Analyst: 业务分析 + 指标定义
- **03-da** — Data Analyst: 数据查询 + SQL 生成
- **04-dba** — Database Admin: 数仓理解 + 表/字段映射
- **05-bi** — Business Intelligence: 商业洞察 + 报告

## 执行 agent (2)
- **06-str** — Strategist: 策略建议
- **07-rec** — Recommender: 行动推荐

## 支持 agent (3)
- **08-fe** — Frontend Engineer: UI/UX
- **09-be** — Backend Engineer: API/数据流
- **10-ops** — Operations: 部署/监控

## v0.2 怎么简化
v0.2 把 5 个核心 agent 合并成 4 阶段:
- **clarify** = pm (5 维度口径追问)
- **understand** = ba + dba (业务 + 数仓一起)
- **sql** = da (只管 SQL)
- **conclude** = bi (报告 + 反方意见)

执行/支持 agent 在 v0.2 不再使用, 4 agent 足够覆盖 user 实际场景。
