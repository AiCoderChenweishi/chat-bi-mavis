"""
reindex_to_es.py — 把 sqlite 知识库全量灌到 Elasticsearch
用法:
  python -m scripts.reindex_to_es
  CHAT_BI_ES_URL=http://127.0.0.1:9200 python -m scripts.reindex_to_es

依赖: agent/rag.py + agent/knowledge_base.py
"""
import os
import sys
import importlib

# 把项目根加进 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    print("=== 1. 检查 ES ===")
    # 直接 import agent.rag (绕开 __init__.py 的 workflow 链)
    rag_mod = importlib.import_module("agent.rag")
    if not rag_mod.is_available():
        print(f"  ✗ ES 不可用, 请先启 ES (CHAT_BI_ES_URL={os.environ.get('CHAT_BI_ES_URL', 'http://127.0.0.1:9200')})")
        sys.exit(1)
    print(f"  ✓ ES 可用, 索引={rag_mod.ES_INDEX}")
    if not rag_mod.ensure_index():
        print("  ✗ 索引建失败")
        sys.exit(1)
    print(f"  ✓ 索引就绪")

    print("\n=== 2. 读 sqlite ===")
    kb_mod = importlib.import_module("agent.knowledge_base")
    kb = kb_mod.get_kb()
    entries = kb.list_recent(limit=10000)  # 拉全量
    print(f"  sqlite 共 {len(entries)} 条")
    if not entries:
        print("  (空, 没东西可灌)")
        return

    print("\n=== 3. 灌 ES (每条 embed + write) ===")
    ok, fail = 0, 0
    for i, e in enumerate(entries, 1):
        d = {
            "id": e.id,
            "category": e.category,
            "title": e.title,
            "content": e.content,
            "source": e.source,
            "tags": e.tags,
            "confidence": e.confidence,
            "created_at": e.created_at,
            "updated_at": e.updated_at,
        }
        if rag_mod.index_kb_entry(d):
            ok += 1
            print(f"  [{i}/{len(entries)}] ✓ KB #{e.id}: {e.title[:40]}")
        else:
            fail += 1
            print(f"  [{i}/{len(entries)}] ✗ KB #{e.id}: {e.title[:40]}")
    print(f"\n=== 4. 完成 ===")
    print(f"  灌入: {ok} 成功 / {fail} 失败 / {len(entries)} 总")
    print(f"  ES 索引 refresh: 1-2s 后可查")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
