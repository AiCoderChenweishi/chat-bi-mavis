"""
Knowledge Base (KB) — v0.2
- 用户/agent 总结的洞察 / 方法论 / 模板 / 数据结果, 存 SQLite
- 支持 RAG 召回 (给 LLM 拼历史知识上下文)
- 不存 mock 占位符, 必含具体数字/事实
"""
import os
import json
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any


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
        """添加 1 条; 已存在 (category+title) 就更新 content/source/tags/confidence."""
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
                return row[0]
            cur = conn.execute(
                "INSERT INTO kb_entries (category, title, content, source, tags, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (category, title, content, source, tags_str, confidence, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def search(self, query: str, limit: int = 10, category: Optional[str] = None) -> List[KBEntry]:
        """LIKE 全文搜索 (不区分大小写, 简单但够用)."""
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
            return cur.rowcount > 0

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
            return {
                "total": total,
                "by_category": by_category,
                "by_source": by_source,
                "avg_confidence": avg_confidence,
                "db_path": self.db_path,
            }

    # ----- RAG 召回 (给 LLM 拼 markdown 上下文) -----

    def recall_for_context(self, query: str, limit: int = 5) -> str:
        """给 LLM 的 RAG 召回: top-N 相关条目拼成 markdown."""
        if not query or not query.strip():
            return ""
        results = self.search(query, limit=limit)
        if not results:
            return ""
        lines = [f"## 📚 知识库相关历史 ({len(results)} 条)", ""]
        for i, e in enumerate(results, 1):
            tag_str = ", ".join(e.tags[:5]) if e.tags else "—"
            content_short = e.content if len(e.content) <= 400 else e.content[:400] + "..."
            lines.append(
                f"### {i}. [{e.category}] {e.title}\n"
                f"- 来源: `{e.source}` | 置信度: {e.confidence:.2f} | 标签: {tag_str}\n"
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


# 单例
_kb_instance: Optional[KnowledgeBase] = None


def get_kb() -> KnowledgeBase:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance
