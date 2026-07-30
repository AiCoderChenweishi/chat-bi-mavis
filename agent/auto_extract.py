"""
v0.5 自动整理: Conclude 阶段 BI 报告自动入 KB (faiss + FTS5)

触发: chat-bi-mavis 完成 4 阶段 (clarify → understand → sql → conclude) 后
从 conclusion markdown 解析 1-3 条关键洞察, 调 kb.add_entry 写 faiss + FTS5

设计 (治本 'user 反馈一次, 手工补充一次'):
- 完全自动: 不调 LLM (避免多 1 次 call), 直接 markdown 解析 (## 段 + 数字)
- 自动去重: UNIQUE(category, title) 触发, 重复 update
- 自动分类: 根据 ## 段头 (洞察/数据结果/反方意见/业务建议) 分类
- 自动置信度: 看内容含具体数字 (+0.2) + 反方意见 (+0.1) + 业务建议 (+0.1) + 字数 (够 200 字 +0.1) 累加
- 自动 tags: 从 spec 抽 (metrics/dimensions/filters) + 全文高频词
- source 标记: 'auto_extract:conclude:<session_id>' (UI 区分手动 vs 自动)
- 失败安全: try/except 包, 失败 log warn 不抛 (不影响流程)
- 开关: AUTO_EXTRACT_ENABLED env (默认 1, 关 0)
"""
import os
import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# 默认开, 配 0 关
AUTO_EXTRACT_ENABLED = os.environ.get("CHAT_BI_AUTO_EXTRACT", "1") == "1"


def _extract_insights_from_conclusion(
    conclusion_md: str,
    spec: Optional[Dict[str, Any]] = None,
    session_id: str = "",
) -> List[Dict[str, Any]]:
    """
    从 conclusion markdown 抽 1-3 条 KBEntry
    策略: 按 ## 段切, 每段算一个 insight (截前 800 字)
    """
    if not conclusion_md or not conclusion_md.strip():
        return []

    # 按 ## 切段 (二级标题)
    # 例: "## 📊 数据概览\n...\n## 💡 核心洞察\n...\n## 🔄 反方意见\n..."
    sections = re.split(r"\n##\s+", conclusion_md)
    # 第一段通常是报告标题/导语, 跳过
    if sections and not sections[0].lstrip().startswith("#"):
        # 第一段没 ## 开头, 跳 (是导语)
        sections = sections[1:]

    # 段头 → 分类映射
    section_to_category = [
        (("数据概览", "核心结论", "数据结果", "📊"), "数据结果"),
        (("核心洞察", "业务洞察", "💡", "洞察"), "洞察"),
        (("反方意见", "反向观点", "🔄", "⚠️", "局限"), "洞察"),
        (("业务建议", "建议", "行动", "✅", "💼"), "洞察"),
        (("方法论", "📚", "知识"), "方法论"),
        (("模板", "📝", "sop"), "模板"),
        (("行业", "🏢"), "行业"),
        (("竞品", "🥊", "对比"), "竞品"),
    ]

    insights: List[Dict[str, Any]] = []
    spec = spec or {}
    # v0.6.11 治本: dimensions/filters 可能是 list of str (e.g. "category_l1")
    # 之前 .get("name") 报 'str has no attribute 'get'', added=0
    def _name(x):
        if isinstance(x, dict): return x.get("name", "") or x.get("value", "")
        return str(x) if x else ""
    spec_metrics = [_name(m) for m in spec.get("metrics", []) if _name(m)]
    spec_dimensions = [_name(d) for d in spec.get("dimensions", []) if _name(d)]
    spec_filters = [_name(f) for f in spec.get("filters", []) if _name(f)]

    for sec in sections[:5]:  # 最多 5 段
        sec = sec.strip()
        if len(sec) < 30:
            continue
        # 切段头 + 内容
        first_nl = sec.find("\n")
        if first_nl < 0:
            title_part = sec
            content_part = ""
        else:
            title_part = sec[:first_nl].strip()
            content_part = sec[first_nl:].strip()
        # 段头去 emoji + 序号
        title_clean = re.sub(r"[📊💡🔄✅💼📚📝🏢🥊⚠️\d\.\s]", "", title_part).strip()[:40]
        if not title_clean:
            title_clean = title_part[:40]
        # 分类
        category = "洞察"
        for keys, cat in section_to_category:
            if any(k in title_part for k in keys):
                category = cat
                break
        # 内容截 600 字 (kb entry content 字段)
        content_short = content_part[:600].strip()
        if not content_short or len(content_short) < 20:
            continue
        # 算 confidence (4 个维度加分, 最高 1.0)
        conf = 0.4  # 基础分 (auto extract 默认偏低)
        # 1. 含具体数字 (+0.2)
        if re.search(r"\d+\.?\d*[%万亿kKmM份元岁月日小时分钟秒]", content_short):
            conf += 0.2
        # 2. 字数 > 100 (+0.1)
        if len(content_short) > 100:
            conf += 0.1
        # 3. 含反方意见 / 业务建议 (+0.1)
        if any(k in title_part for k in ("反方", "建议", "局限")):
            conf += 0.1
        # 4. 含 spec metrics (+0.1)
        if spec_metrics and any(m in content_short for m in spec_metrics):
            conf += 0.1
        conf = min(conf, 0.95)  # 上限 0.95 (auto 不能 1.0)

        # 抽 tags: 段内容 jieba 分词, 取 2-4 字词 (跟 FTS5 一致)
        tags = []
        try:
            from .rag import _tokenize_chinese
            tokens = _tokenize_chinese(content_short).split()
            for t in tokens:
                if t not in tags:
                    tags.append(t)
        except Exception:
            # 降级: regex 粗抽
            for t in re.findall(r"[\u4e00-\u9fff]{2,4}", content_short):
                if t not in tags:
                    tags.append(t)
        # 加 spec metrics 当 hint (只在段内容出现时才保留)
        if spec_metrics:
            for m in spec_metrics:
                if m in content_short and m not in tags:
                    tags.insert(0, m)
        if spec_dimensions:
            for d in spec_dimensions:
                if d in content_short and d not in tags:
                    tags.insert(0, d)
        tags = list(dict.fromkeys(tags))[:6]  # 去重 + 上限 6

        insights.append({
            "category": category,
            # v0.6.13 治本: 加 session_id[:8] 后缀, 避免 UNIQUE(category, title) 触发 update
            # (之前每次 LLM conclusion 段头一样, 全部 update 同一 record, KB 永远 5 条)
            "title": f"{title_clean} [auto:{session_id[:8] if session_id else 'na'}]",
            "content": content_short,
            "source": f"auto_extract:conclude:{session_id}" if session_id else "auto_extract:conclude",
            "tags": tags,
            "confidence": round(conf, 2),
        })

    return insights


def auto_extract_to_kb(
    conclusion_md: str,
    spec: Optional[Dict[str, Any]] = None,
    session_id: str = "",
) -> Tuple[int, int]:
    """
    Conclude 阶段自动整理进 KB (faiss + FTS5)
    返 (added, skipped)
    - added: 实际写入 KB 的条数
    - skipped: 跳过 (空内容 / 已存在)
    """
    if not AUTO_EXTRACT_ENABLED:
        logger.info("auto_extract 关闭 (CHAT_BI_AUTO_EXTRACT=0), 跳过")
        return 0, 0
    if not conclusion_md or not conclusion_md.strip():
        return 0, 0

    try:
        from .knowledge_base import get_kb
    except ImportError as e:
        logger.error(f"knowledge_base import 失败: {e}")
        return 0, 0

    insights = _extract_insights_from_conclusion(conclusion_md, spec, session_id)
    if not insights:
        logger.info(f"session {session_id}: conclusion 抽 0 条 insight, 跳过")
        return 0, 0

    kb = get_kb()
    added = 0
    skipped = 0
    for ins in insights:
        try:
            kb.add_entry(
                category=ins["category"],
                title=ins["title"],
                content=ins["content"],
                source=ins["source"],
                tags=ins["tags"],
                confidence=ins["confidence"],
            )
            added += 1
        except Exception as e:
            logger.warning(f"auto_extract 写 KB 失败 ({ins['title'][:30]}): {e}")
            skipped += 1
    logger.info(f"session {session_id}: auto_extract 写 {added} 条 KB, 跳过 {skipped}")
    return added, skipped


def get_auto_extract_config() -> Dict[str, Any]:
    """返当前配置 (给 UI 显示)"""
    return {
        "enabled": AUTO_EXTRACT_ENABLED,
        "env_var": "CHAT_BI_AUTO_EXTRACT",
        "default": 1,
    }
