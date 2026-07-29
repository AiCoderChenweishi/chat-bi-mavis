"""
reindex_faiss.py — 把 sqlite 知识库全量灌到 faiss 索引
(替代之前的 ES reindex, v0.4.1 用 faiss-cpu + FTS5, 跟 chat-bi-mavis 同进程)

用法:
  python -m scripts.reindex_faiss
  CHAT_BI_EMBED_DIR=/opt/models/bge-small-zh-v1.5 python -m scripts.reindex_faiss
"""
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    print("=== 1. 检查 faiss + 模型 ===")
    rag_mod = importlib.import_module("agent.rag")
    if not rag_mod._load_embed_model()[0]:
        print(f"  ✗ bge 模型加载失败, 请检查 CHAT_BI_EMBED_DIR={os.environ.get('CHAT_BI_EMBED_DIR', '(未设)')}")
        sys.exit(1)
    if not rag_mod._load_faiss_index():
        print("  ✗ faiss 索引加载失败")
        sys.exit(1)
    print(f"  ✓ faiss + bge 都就绪")

    print("\n=== 2. 读 sqlite ===")
    kb_mod = importlib.import_module("agent.knowledge_base")
    kb = kb_mod.get_kb()
    entries = kb.list_recent(limit=10000)
    print(f"  sqlite 共 {len(entries)} 条")
    if not entries:
        print("  (空, 没东西可灌)")
        return

    print("\n=== 3. 重建 faiss 索引 (清空 + 全量重灌) ===")
    ok, fail = rag_mod.reindex_all([
        {
            "id": e.id, "category": e.category, "title": e.title,
            "content": e.content, "source": e.source, "tags": e.tags,
            "confidence": e.confidence, "created_at": e.created_at,
            "updated_at": e.updated_at,
        }
        for e in entries
    ])
    print(f"\n=== 4. 完成 ===")
    print(f"  灌入: {ok} 成功 / {fail} 失败 / {len(entries)} 总")
    print(f"  faiss 索引: {rag_mod.FAISS_INDEX_PATH}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
