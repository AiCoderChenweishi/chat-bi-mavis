"""
Knowledge Base (KB) — v0.4
- 用户/agent 总结的洞察 / 方法论 / 模板 / 数据结果, 存 SQLite (source of truth) + ES (检索)
- 支持 RAG 召回: ES 混合检索 (BM25 + knn + RRF) 优先, ES down 时降级 sqlite LIKE
- 不存 mock 占位符, 必含具体数字/事实
"""
import os
import json
import sqlite3
import logging
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


DB_PATH = os.environ.get(
    "DATA_ANALYST_KB_DB",
    os.path.join(os.path.dirname(__file__), "..", "data", "knowledge_base.db"),
)


@dataclass
class KBEntry:
    id: int
    category: str        # 洞察 / 方法论 / 数据结果 / 模板 / 行业 / 竞品
    title: str           # 10-30 字, 含核心数字/结论
    content: str         # 200-500 字, 含具体数字/事实
    source: str          # manual / agent / page_extract:<label> / bootstrap
    tags: List[str]
    confidence: float    # 0.0-1.0
    created_at: str
    updated_at: str


class KnowledgeBase:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kb_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    tags TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.8,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(category, title)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_category ON kb_entries(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_source ON kb_entries(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_kb_created ON kb_entries(created_at DESC)")
            # v0.4.1: FTS5 虚表 (BM25 检索, 跟 faiss 向量检索 hybrid)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS kb_entries_fts USING fts5(
                    title, content, tags, category,
                    content='kb_entries', content_rowid='id',
                    tokenize='unicode61'
                )
            """)
            # 触发器: kb_entries 写入 → kb_entries_fts 同步
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS kb_entries_ai AFTER INSERT ON kb_entries
                BEGIN
                    INSERT INTO kb_entries_fts(rowid, title, content, tags, category)
                    VALUES (new.id, new.title, new.content, new.tags, new.category);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS kb_entries_ad AFTER DELETE ON kb_entries
                BEGIN
                    DELETE FROM kb_entries_fts WHERE rowid = old.id;
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS kb_entries_au AFTER UPDATE ON kb_entries
                BEGIN
                    DELETE FROM kb_entries_fts WHERE rowid = old.id;
                    INSERT INTO kb_entries_fts(rowid, title, content, tags, category)
                    VALUES (new.id, new.title, new.content, new.tags, new.category);
                END
            """)
            conn.commit()

    # ----- CRUD -----

    def add_entry(
        self,
        category: str,
        title: str,
        content: str,
        source: str = "manual",
        tags: Optional[List[str]] = None,
        confidence: float = 0.8,
    ) -> int:
        """添加 1 条; 已存在 (category+title) 就更新 content/source/tags/confidence. 双写 sqlite + ES."""
        tags_str = ",".join(t.strip() for t in (tags or []) if t and t.strip())
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id FROM kb_entries WHERE category = ? AND title = ?",
                (category, title),
            )
            row = cur.fetchone()
            if row:
                conn.execute(
                    "UPDATE kb_entries SET content=?, source=?, tags=?, confidence=?, updated_at=? WHERE id=?",
                    (content, source, tags_str, confidence, now, row[0]),
                )
                conn.commit()
                entry_id = row[0]
            else:
                cur = conn.execute(
                    "INSERT INTO kb_entries (category, title, content, source, tags, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (category, title, content, source, tags_str, confidence, now, now),
                )
                conn.commit()
                entry_id = cur.lastrowid
        # 同步 faiss (sqlite 是 source of truth, faiss 失败 log warn 不抛)
        self._rag_index_sync(entry_id, category, title, content, source, tags or [], confidence, now, now)
        return entry_id

    def _rag_index_sync(self, entry_id, category, title, content, source, tags, confidence, created_at, updated_at):
        """把单条 entry 同步到 faiss (懒加载 rag 模块, 失败 log warn 不抛)"""
        try:
            from . import rag  # 懒加载, 避免 import 链问题
            rag.index_kb_entry({
                "id": entry_id,
                "category": category,
                "title": title,
                "content": content,
                "source": source,
                "tags": tags,
                "confidence": confidence,
                "created_at": created_at,
                "updated_at": updated_at,
            })
        except Exception as e:
            logger.warning(f"KB #{entry_id} faiss 同步失败 (sqlite 已存): {e}")

    def search(self, query: str, limit: int = 10, category: Optional[str] = None) -> List[KBEntry]:
        """LIKE 全文搜索 (v0.2 兜底, FTS5 不可用时降级)."""
        like = f"%{query}%"
        with self._connect() as conn:
            if category:
                cur = conn.execute(
                    """SELECT id, category, title, content, source, tags, confidence, created_at, updated_at
                       FROM kb_entries
                       WHERE category = ? AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                       ORDER BY updated_at DESC LIMIT ?""",
                    (category, like, like, like, limit),
                )
            else:
                cur = conn.execute(
                    """SELECT id, category, title, content, source, tags, confidence, created_at, updated_at
                       FROM kb_entries
                       WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (like, like, like, limit),
                )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def search_fts5(self, query: str, limit: int = 10, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        v0.4.1: FTS5 全文检索 (BM25), 返 raw dict 给 faiss hybrid 用
        FTS5 unicode61 分词器不切中文, 所以 query 用 jieba 预分词 (空格 join)
        """
        # jieba 预分词 (跟 rag.py 共享同一函数)
        try:
            from . import rag as _rag
            fts_query = _rag._tokenize_chinese(query)
            if not fts_query:
                fts_query = query  # 降级
        except Exception:
            fts_query = query
        # FTS5 query 转义: 去掉特殊字符 + 加 * 后缀 (前缀匹配)
        fts_query_escaped = " ".join(f'"{t}"' for t in fts_query.split() if t)
        if not fts_query_escaped:
            return []
        with self._connect() as conn:
            if category:
                cur = conn.execute(
                    """SELECT k.id, k.category, k.title, k.content, k.source, k.tags, k.confidence, k.created_at, k.updated_at,
                              bm25(kb_entries_fts) AS bm25_score
                       FROM kb_entries_fts
                       JOIN kb_entries k ON k.id = kb_entries_fts.rowid
                       WHERE kb_entries_fts MATCH ? AND k.category = ?
                       ORDER BY bm25_score LIMIT ?""",
                    (fts_query_escaped, category, limit),
                )
            else:
                cur = conn.execute(
                    """SELECT k.id, k.category, k.title, k.content, k.source, k.tags, k.confidence, k.created_at, k.updated_at,
                              bm25(kb_entries_fts) AS bm25_score
                       FROM kb_entries_fts
                       JOIN kb_entries k ON k.id = kb_entries_fts.rowid
                       WHERE kb_entries_fts MATCH ?
                       ORDER BY bm25_score LIMIT ?""",
                    (fts_query_escaped, limit),
                )
            return [
                {
                    "id": r[0], "category": r[1], "title": r[2],
                    "content": r[3], "source": r[4], "tags": [t for t in (r[5] or "").split(",") if t],
                    "confidence": r[6], "created_at": r[7], "updated_at": r[8],
                    "score": -r[9] if r[9] is not None else 0,  # bm25() 返负数, 取反让越大越好
                }
                for r in cur.fetchall()
            ]

    def list_recent(self, limit: int = 20, category: Optional[str] = None) -> List[KBEntry]:
        with self._connect() as conn:
            if category:
                cur = conn.execute(
                    "SELECT id, category, title, content, source, tags, confidence, created_at, updated_at "
                    "FROM kb_entries WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT id, category, title, content, source, tags, confidence, created_at, updated_at "
                    "FROM kb_entries ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get(self, entry_id: int) -> Optional[KBEntry]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, category, title, content, source, tags, confidence, created_at, updated_at FROM kb_entries WHERE id=?",
                (entry_id,),
            )
            row = cur.fetchone()
            return self._row_to_entry(row) if row else None

    def delete_entry(self, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM kb_entries WHERE id=?", (entry_id,))
            conn.commit()
            deleted = cur.rowcount > 0
        # faiss 同步删
        if deleted:
            try:
                from . import rag
                rag.delete_kb_entry(entry_id)
            except Exception as e:
                logger.warning(f"KB #{entry_id} faiss 删失败 (sqlite 已删): {e}")
        return deleted

    def get_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
            by_category = dict(
                conn.execute("SELECT category, COUNT(*) FROM kb_entries GROUP BY category").fetchall()
            )
            by_source = dict(
                conn.execute("SELECT source, COUNT(*) FROM kb_entries GROUP BY source").fetchall()
            )
            avg_conf_row = conn.execute("SELECT AVG(confidence) FROM kb_entries").fetchone()
            avg_confidence = round(avg_conf_row[0], 3) if avg_conf_row[0] is not None else None
            # FTS5 同步状态
            fts_count = conn.execute("SELECT COUNT(*) FROM kb_entries_fts").fetchone()[0]
            stats = {
                "total": total,
                "by_category": by_category,
                "by_source": by_source,
                "avg_confidence": avg_confidence,
                "db_path": self.db_path,
                "fts5_count": fts_count,
                "fts5_in_sync": fts_count == total,
            }
            # 加 faiss 状态 (v0.4.1)
            try:
                from . import rag
                if rag.is_available():
                    rag_stats = rag.get_stats()
                    if "faiss" in rag_stats:
                        stats["faiss"] = rag_stats["faiss"]
                    stats["backend"] = rag_stats.get("backend", "faiss-cpu + FTS5")
            except Exception:
                pass
            return stats

    # ----- RAG 召回 (给 LLM 拼 markdown 上下文) -----

    def recall_for_context(self, query: str, limit: int = 5) -> str:
        """
        给 LLM 的 RAG 召回: faiss + FTS5 混合检索优先, 降级 sqlite LIKE.
        返回 markdown 段 (跟老 API 完全兼容).
        """
        if not query or not query.strip():
            return ""
        # 1. 优先 faiss 混合检索
        try:
            from . import rag
            if rag.is_available():
                results = rag.hybrid_search(query, top_k=limit)
                if results:
                    return self._format_rag_md(results, source_label="faiss + FTS5 混合")
        except Exception as e:
            logger.warning(f"faiss RAG 失败, 降级 sqlite: {e}")
        # 2. 降级 sqlite LIKE
        results = self.search(query, limit=limit)
        if not results:
            return ""
        return self._format_rag_md(
            [asdict(e) if hasattr(e, '__dataclass_fields__') else e for e in results],
            source_label="sqlite LIKE"
        )

    def _format_rag_md(self, entries: List[Dict[str, Any]], source_label: str) -> str:
        lines = [f"## 📚 知识库相关历史 ({source_label} · {len(entries)} 条)", ""]
        for i, e in enumerate(entries, 1):
            tag_str = ", ".join((e.get("tags") or [])[:5]) or "—"
            content_short = e.get("content", "")
            if len(content_short) > 400:
                content_short = content_short[:400] + "..."
            score_str = ""
            if e.get("score") is not None:
                score_str = f" | RRF 分数: {e['score']:.2f}"
            lines.append(
                f"### {i}. [{e.get('category', '')}] {e.get('title', '')}\n"
                f"- 来源: `{e.get('source', '')}` | 置信度: {e.get('confidence', 0):.2f}{score_str} | 标签: {tag_str}\n"
                f"- 内容: {content_short}\n"
            )
        return "\n".join(lines)

    def _row_to_entry(self, row) -> KBEntry:
        if not row:
            return None
        tags = [t for t in (row[5] or "").split(",") if t]
        return KBEntry(
            id=row[0],
            category=row[1],
            title=row[2],
            content=row[3],
            source=row[4],
            tags=tags,
            confidence=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    def get_categories(self) -> List[str]:
        """返所有 category (v0.4.1 兼容 rag.py get_categories)"""
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT category FROM kb_entries ORDER BY category").fetchall()]

    def rebuild_fts5(self) -> Tuple[int, int]:
        """
        v0.4.1: 重建 FTS5 索引 (从 kb_entries 全量塞)
        治本: 老数据是 v0.3 时期 INSERT, 触发器没建, FTS5 表空 / 索引空
        """
        with self._connect() as conn:
            # 先全删 FTS5 数据
            conn.execute("DELETE FROM kb_entries_fts")
            # 从 kb_entries 全量塞
            conn.execute("""
                INSERT INTO kb_entries_fts(rowid, title, content, tags, category)
                SELECT id, title, content, tags, category FROM kb_entries
            """)
            # 让 FTS5 optimize
            try:
                conn.execute("INSERT INTO kb_entries_fts(kb_entries_fts) VALUES('optimize')")
            except Exception:
                pass
            conn.commit()
            kb_count = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM kb_entries_fts").fetchone()[0]
        return kb_count, fts_count


# 单例
_kb_instance: Optional[KnowledgeBase] = None


def get_kb() -> KnowledgeBase:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance
