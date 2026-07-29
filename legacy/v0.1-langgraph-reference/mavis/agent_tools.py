"""Agent tool registry (v1.0.11+)

每个 agent (PM/BA/DBA/DA/BI) 注册自己的 tool, agent loop 让 LLM 自主选 tool 调用.
这是从"剧本模式" (固定 executor 路径) 改成"智能体模式" (LLM 自主决策) 的关键.
"""
from __future__ import annotations
import json
import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("mavis.agent_tools")


class Tool:
    """单个 tool 定义: name + description + parameters (JSON Schema) + handler"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_openai_schema(self) -> Dict[str, Any]:
        """转 OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, **kwargs) -> Any:
        """执行 tool, 自动序列化结果"""
        try:
            result = self.handler(**kwargs)
            if not isinstance(result, dict):
                result = {"value": result}
            return {"ok": True, "tool": self.name, "result": result}
        except Exception as e:
            log.exception(f"tool {self.name} failed")
            return {"ok": False, "tool": self.name, "error": str(e)[:300]}


class ToolRegistry:
    """tool 注册中心 (线程不安全, 启动时一次性注册)"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            log.warning(f"tool {tool.name} re-registered, override")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def to_openai_tools(self, names: Optional[List[str]] = None) -> List[Dict]:
        """转 OpenAI tools 格式. names=None 返全部"""
        tools = self.list() if names is None else [t for t in self.list() if t.name in names]
        return [t.to_openai_schema() for t in tools]


# ============================================================
# 全局 registry + 注册
# ============================================================
_global_registry = ToolRegistry()


def get_global_registry() -> ToolRegistry:
    return _global_registry


def _register_builtin_tools():
    """启动时注册内置 tool (DBA / DA / BI 等 agent 用)"""
    from .data_connectors import (
        get_connector_for_data_source,
        execute_standard_queries,
        suggest_data_sources_for_form,
    )
    from .crawler import (
        crawl_eastmoney_quote,
        crawl_cninfo_announcements,
        crawl_xueqiu_hot,
        crawl_sina_finance_news,
        crawl_industry_news,
        fetch_public_url,
    )
    from .data_sources import get_data_source_store

    reg = _global_registry

    # 1. list_data_sources - 列出所有可用数仓
    def _list_ds() -> dict:
        store = get_data_source_store()
        ds = store.list_all()
        return {"count": len(ds), "sources": [{"id": d.id, "name": d.name, "kind": d.kind, "tags": d.tags, "description": d.description} for d in ds[:20]]}

    reg.register(Tool(
        name="list_data_sources",
        description="列出所有已注册的 data source (内部数仓 + 用户上传的 CSV). 返回 id/name/kind/tags, 用 id 进一步 preview/query.",
        parameters={"type": "object", "properties": {}},
        handler=_list_ds,
    ))

    # 2. preview_data_source - 拿 schema + 前 N 行样例
    def _preview(ds_id: int, sample_limit: int = 5) -> dict:
        ds = get_data_source_store().get(ds_id)
        if not ds:
            return {"error": f"data_source id={ds_id} not found"}
        connector = get_connector_for_data_source(ds_id)
        if not connector:
            return {"error": f"no connector for kind={ds.kind}"}
        tables = connector.list_tables()
        row_counts = {}
        samples = {}
        for t in tables:
            try:
                row_counts[t] = connector.get_table_count(t)
                samples[t] = connector.get_sample(t, limit=sample_limit)
            except Exception as e:
                samples[t] = [{"_err": str(e)[:100]}]
        return {
            "ds_id": ds_id,
            "ds_name": ds.name,
            "kind": ds.kind,
            "tables": tables,
            "row_counts": row_counts,
            "samples": samples,
        }

    reg.register(Tool(
        name="preview_data_source",
        description="拿 data source 的 schema (表名 + 列 + 前 N 行样例). 调这个看数仓结构再决定 SQL.",
        parameters={
            "type": "object",
            "properties": {
                "ds_id": {"type": "integer", "description": "data_source id (从 list_data_sources 拿)"},
                "sample_limit": {"type": "integer", "description": "每个表返回前几行 (默认 5)"},
            },
            "required": ["ds_id"],
        },
        handler=_preview,
    ))

    # 3. query_data_source - 真 SQL
    def _query(ds_id: int, sql: str, max_rows: int = 100) -> dict:
        connector = get_connector_for_data_source(ds_id)
        if not connector:
            return {"error": f"no connector for ds_id={ds_id}"}
        try:
            rows = connector.query(sql)
            return {"rows": rows[:max_rows], "total_returned": len(rows), "sql": sql}
        except Exception as e:
            return {"error": f"SQL 失败: {str(e)[:200]}", "sql": sql}

    reg.register(Tool(
        name="query_data_source",
        description="对 data source 跑 SELECT SQL (只能 SELECT, 防注入). 返回 rows. preview 后再调这个.",
        parameters={
            "type": "object",
            "properties": {
                "ds_id": {"type": "integer", "description": "data source id"},
                "sql": {"type": "string", "description": "SELECT 语句"},
                "max_rows": {"type": "integer", "description": "最多返回几行 (默认 100)"},
            },
            "required": ["ds_id", "sql"],
        },
        handler=_query,
    ))

    # 4. suggest_data_sources - 按 form 关键词推荐
    def _suggest(title: str, context: str = "", metrics: Optional[List[str]] = None) -> dict:
        r = suggest_data_sources_for_form(title=title, context=context, metrics=metrics or [])
        return {
            "matched_count": len(r.get("matched_sources", [])),
            "matched": [{"id": m["id"], "name": m["name"], "relevance": m["relevance"], "matched_keywords": m.get("matched_keywords", [])} for m in r.get("matched_sources", [])[:5]],
            "total_sources": r.get("total_sources", 0),
        }

    reg.register(Tool(
        name="suggest_data_sources",
        description="按 form (title/context/metrics) 推荐最匹配的 data source. user 没指定数仓时先调这个.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "form 标题"},
                "context": {"type": "string", "description": "form 背景"},
                "metrics": {"type": "array", "items": {"type": "string"}, "description": "form 关注的指标列表"},
            },
            "required": ["title"],
        },
        handler=_suggest,
    ))

    # 5. crawl_public - 公网爬虫 (金价/股价/新闻/公告/行业/汇率)
    def _crawl(query: str, source: str = "auto") -> dict:
        text = query.lower()
        try:
            if any(kw in text for kw in ["金价", "金", "黄金"]):
                r = fetch_public_url("https://quote.eastmoney.com/gold/default.html", prompt="提取黄金价格")
                return {"source": "eastmoney_gold", "data": (r.get("content", "") if r else "")[:1000], "is_mock": (r or {}).get("is_mock", True), "error": (r or {}).get("error", "")}
            if any(kw in text for kw in ["股票", "股价", "a 股", "港股", "美股", "财报"]):
                import re as _re
                codes = _re.findall(r"\b\d{6}\b", text) or _re.findall(r"[0356]\d{5}", text)
                code = codes[0] if codes else "000001"
                r = crawl_eastmoney_quote(code)
                return {"source": f"eastmoney_{code}", "data": r, "is_mock": (r or {}).get("is_mock", True)}
            if any(kw in text for kw in ["公告", "披露"]):
                import re as _re
                codes = _re.findall(r"\b\d{6}\b", text)
                code = codes[0] if codes else "000001"
                r = crawl_cninfo_announcements(code, limit=10)
                return {"source": f"cninfo_{code}", "data": r.get("announcements", [])[:10]}
            if any(kw in text for kw in ["新闻", "头条", "舆情"]):
                r = crawl_sina_finance_news(limit=10)
                return {"source": "sina_news", "data": r.get("news", [])[:10]}
            if any(kw in text for kw in ["行业", "对标", "竞品"]):
                kw = query.split()[-1] if query.split() else "互联网"
                r = crawl_industry_news(kw, top_k=5)
                return {"source": f"industry_{kw}", "data": r.get("news", [])[:5]}
            if any(kw in text for kw in ["汇率", "比特币", "btc", "加密", "币"]):
                r = fetch_public_url("https://quote.eastmoney.com/center/forex.html", prompt="提取汇率/币")
                return {"source": "eastmoney_forex", "data": (r.get("content", "") if r else "")[:1000]}
            # 兜底: 雪球热股
            r = crawl_xueqiu_hot(limit=10)
            return {"source": "xueqiu_hot", "data": r.get("hot_stocks", [])[:10]}
        except Exception as e:
            return {"error": f"爬取出错: {str(e)[:200]}"}

    reg.register(Tool(
        name="crawl_public",
        description="爬公网 (金价/股价/汇率/比特币/天气/新闻/公告/行业/竞品). query 写你想查的内容关键词.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询关键词 (如: '金价 2023', '比亚迪 002594', '行业 benchmark')"},
            },
            "required": ["query"],
        },
        handler=_crawl,
    ))

    # 6. mock_dau - 假 DAU (兜底, agent 不会主动调除非真的没数据)
    def _mock_dau(months: int = 3, channels: str = "wechat,douyin,appstore,huawei", base: int = 10000) -> dict:
        chs = [c.strip() for c in channels.split(",") if c.strip()]
        import random as _r
        _r.seed(42)
        rows = []
        for m in range(months):
            for c in chs:
                rows.append({"month": f"M{m+1}", "channel": c, "dau": int(base * _r.uniform(0.7, 1.3) * (1 - m * 0.1))})
        return {"rows": rows, "is_mock": True, "warning": "这是 mock DAU, 真场景应优先用 query_data_source 或 crawl_public"}

    reg.register(Tool(
        name="mock_dau",
        description="生成 mock DAU 数据 (3 个月 × 4 渠道). 仅当 user 明确要 mock / 内部没数仓 / 公网不可访时用. 优先用 query_data_source.",
        parameters={
            "type": "object",
            "properties": {
                "months": {"type": "integer", "description": "月份数"},
                "channels": {"type": "string", "description": "渠道列表, 逗号分隔"},
                "base": {"type": "integer", "description": "基础 DAU"},
            },
        },
        handler=_mock_dau,
    ))

    # 7. ask_user - 问 user (智能体遇到不确定时主动问)
    def _ask_user(question: str, context: str = "") -> dict:
        # 实际场景下应该走 user_chat / requires_user_input
        return {"_note": "ask_user 走 workflow requires_user_input 路径", "question": question, "context": context}

    reg.register(Tool(
        name="ask_user",
        description="当你不确定 user 真实意图 / 数据源选哪个 / SQL 该怎么写时, 调这个问 user (而不是瞎猜).",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问 user 的问题"},
                "context": {"type": "string", "description": "为什么问 / 已知背景"},
            },
            "required": ["question"],
        },
        handler=_ask_user,
    ))


# 启动时注册
_register_builtin_tools()


# v1.0.11: 补充 search_kb + generate_chart tool (其他 agent 用)
def _register_supplementary_tools():
    from .rag import search as _rag_search

    reg = _global_registry

    # 8. search_kb - 查知识库
    def _search_kb(query: str, top_k: int = 3) -> dict:
        try:
            hits = _rag_search(query, top_k=top_k)
            return {
                "hits": [{"title": h.get("title"), "score": h.get("score"), "content": h.get("content", "")[:300]} for h in hits]
            }
        except Exception as e:
            return {"error": f"KB 搜索失败: {str(e)[:200]}"}

    reg.register(Tool(
        name="search_kb",
        description="查项目知识库 (历史项目 / 方法论 / RAG). 给 query 关键词, 返 top-k 命中.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询关键词"},
                "top_k": {"type": "integer", "description": "返回几个结果 (默认 3)"},
            },
            "required": ["query"],
        },
        handler=_search_kb,
    ))

    # 9. save_kb_entry - 把分析结论写入知识库 (RAG 反哺)
    def _save_kb_entry(
        category: str,
        title: str,
        content: str,
        tags: str = "",
        source: str = "agent",
        confidence: float = 0.8,
    ) -> dict:
        """把结论/方法论/数据规律写入项目知识库. 下次跑相似项目 RAG 召回."""
        try:
            from .knowledge_base import get_kb
            tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
            eid = get_kb().add_entry(
                category=category,
                title=title,
                content=content,
                source=source,
                tags=tag_list,
                confidence=confidence,
            )
            return {"ok": True, "id": eid, "category": category, "title": title}
        except Exception as e:
            return {"ok": False, "error": f"KB 写入失败: {str(e)[:200]}"}

    reg.register(Tool(
        name="save_kb_entry",
        description="把分析结论/方法论/数据规律写入项目知识库. 下次跑相似项目 RAG 召回. (v1.0.15.4 RAG 闭环)",
        parameters={
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "分类: insight/方法论/数据规律/模板/竞品/行业/用户偏好"},
                "title": {"type": "string", "description": "一句话标题 (10-30 字符)"},
                "content": {"type": "string", "description": "结论正文 (50-500 字符, 含数据/方法/可复用洞察)"},
                "tags": {"type": "string", "description": "逗号分隔标签, e.g. 'DAU,留存,智能投顾'"},
                "confidence": {"type": "number", "description": "置信度 0-1, 默认 0.8"},
            },
            "required": ["category", "title", "content"],
        },
        handler=_save_kb_entry,
    ))


_register_supplementary_tools()
