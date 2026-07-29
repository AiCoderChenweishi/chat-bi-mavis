"""
mavis/knowledge_base.py — 知识库 (v1.0.7+ agentic)

设计目标:
- 持续积累 (每 workflow 跑完自动 extract learn)
- 新增已有 (启动时从 /workspace 现有内容 bootstrap 索引)
- agent 可调 (反思 / 拆 plan 时 recall)
- user 可见 (UI 侧栏 + 搜索 + 添加)

数据模型 (sqlite, knowledge_base.db):
- kb_entries(id, category, title, content, source, tags, created_at, updated_at, confidence)
  - category: 'project' / 'workflow' / 'domain' / 'code' / 'lesson' / 'reference'
  - source: 'bootstrap' / 'auto' / 'manual' / 'agent'
  - tags: comma-separated
  - confidence: 0-1 (0.8=auto-extracted, 1.0=manual, 0.5=heuristic)

API:
- add_entry(category, title, content, source, tags, confidence) → entry_id
- search(query, limit, category) → list of entries
- list_recent(limit, category) → list of entries
- bootstrap_from_workspace() → int (count of entries added)
- extract_learn_from_workflow(wf_id) → entry_id (called after workflow done)
- recall_for_context(context_text, limit) → str (markdown formatted for LLM prompt)
- delete_entry(entry_id)
- get_stats() → dict

记忆区别:
- user_memory: user 偏好 / 历史需求 / 决策 (短期, 跟 user 走)
- project_memory: 项目配置 (中长, 跟项目走)
- knowledge_base: 通用知识库 (长期, 跨项目, agent/user 共用)
- RAG: 8 个 bootstrap doc, 向量检索
"""
import os
import re
import time
import sqlite3
import json
import glob
from typing import Optional, List, Dict, Any
from pathlib import Path
from dataclasses import dataclass, asdict


KB_DB_PATH = os.environ.get(
    "MAVIS_KB_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "knowledge_base.db")
)
# Ensure data dir
os.makedirs(os.path.dirname(KB_DB_PATH), exist_ok=True)


@dataclass
class KBEntry:
    id: int
    category: str  # project / workflow / domain / code / lesson / reference
    title: str
    content: str
    source: str  # bootstrap / auto / manual / agent
    tags: List[str]
    confidence: float
    created_at: str
    updated_at: str


class KnowledgeBase:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.environ.get("MAVIS_KB_DB", KB_DB_PATH)
        self.db_path = db_path
        self._init_db()
        self._bootstrap_done = False
        self._bootstrap_db = None

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

    # === CRUD ===

    def add_entry(
        self,
        category: str,
        title: str,
        content: str,
        source: str = "manual",
        tags: Optional[List[str]] = None,
        confidence: float = 0.8,
    ) -> int:
        """添加一条知识, 返回 entry_id. 已存在 (category+title) 就更新."""
        tags_str = ",".join(tags or [])
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id FROM kb_entries WHERE category = ? AND title = ?",
                (category, title),
            )
            row = cur.fetchone()
            if row:
                # Update
                conn.execute(
                    "UPDATE kb_entries SET content=?, source=?, tags=?, confidence=?, updated_at=? WHERE id=?",
                    (content, source, tags_str, confidence, now, row[0]),
                )
                conn.commit()
                return row[0]
            # Insert
            cur = conn.execute(
                "INSERT INTO kb_entries (category, title, content, source, tags, confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (category, title, content, source, tags_str, confidence, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def search(self, query: str, limit: int = 10, category: Optional[str] = None) -> List[KBEntry]:
        """关键字搜索 (LIKE 全文, 不分大小写). 简单但够用."""
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
                    "SELECT * FROM kb_entries WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM kb_entries ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def get(self, entry_id: int) -> Optional[KBEntry]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM kb_entries WHERE id = ?", (entry_id,))
            row = cur.fetchone()
            return self._row_to_entry(row) if row else None

    def delete_entry(self, entry_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM kb_entries WHERE id = ?", (entry_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_stats(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
            by_category = dict(conn.execute(
                "SELECT category, COUNT(*) FROM kb_entries GROUP BY category"
            ).fetchall())
            by_source = dict(conn.execute(
                "SELECT source, COUNT(*) FROM kb_entries GROUP BY source"
            ).fetchall())
            return {
                "total": total,
                "by_category": by_category,
                "by_source": by_source,
                "db_path": self.db_path,
            }

    # === Bootstrap: 启动时从 /workspace 现有内容建索引 ===

    def bootstrap_from_workspace(self, workspace: str = None) -> int:
        """从现有项目记忆 + RAG bootstrap + 代码注释提取知识, 一次性执行"""
        # 自动检测 workspace: /workspace → /opt/mavis-dev → cwd
        if workspace is None:
            for cand in ["/workspace", "/opt/mavis-dev", os.getcwd()]:
                if os.path.isdir(cand) and os.path.isdir(os.path.join(cand, "mavis")):
                    workspace = cand
                    break
            if workspace is None:
                workspace = "/workspace"
        if self._bootstrap_done and self._bootstrap_db == self.db_path:
            return 0
        count = 0

        # 1. 现有 RAG bootstrap docs
        rag_path = os.path.join(workspace, "mavis", "rag.py")
        if os.path.exists(rag_path):
            try:
                with open(rag_path) as f:
                    content = f.read()
                # 找 _bootstrap_docs 列表
                m = re.search(r"_bootstrap_docs\s*=\s*\[(.*?)\]", content, re.DOTALL)
                if m:
                    # 简单 parse: 找 ('title', 'content') 元组
                    for tmatch in re.finditer(r"\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", m.group(1)):
                        title, c = tmatch.group(1), tmatch.group(2)
                        # 截短 content
                        c_short = c[:500] if len(c) > 500 else c
                        self.add_entry(
                            category="reference",
                            title=f"RAG: {title}",
                            content=c_short,
                            source="bootstrap",
                            tags=["rag", "bootstrap"],
                            confidence=1.0,
                        )
                        count += 1
            except Exception:
                pass

        # 2. AGENTS.md / README.md / docs/ 目录
        doc_files = []
        for pat in ["README.md", "AGENTS.md", "data-analyst-workflow.md", "Phase1-summary.md"]:
            p = os.path.join(workspace, pat)
            if os.path.exists(p):
                doc_files.append(p)
        # docs/ 目录下所有 .md
        docs_dir = os.path.join(workspace, "docs")
        if os.path.isdir(docs_dir):
            doc_files.extend(sorted(glob.glob(os.path.join(docs_dir, "**", "*.md"), recursive=True)))

        for fp in doc_files:
            try:
                with open(fp) as f:
                    content = f.read()
                # 取前 800 字作为概要
                if len(content) < 100:
                    continue
                title = os.path.basename(fp)
                snippet = content[:800]
                # 长 doc 分段入
                if len(content) > 2000:
                    # 按 ## 标题分段
                    sections = re.split(r"\n##\s+", content)
                    for i, sec in enumerate(sections):
                        if len(sec) < 50:
                            continue
                        first_line = sec.split("\n", 1)[0].strip()
                        if i == 0:
                            sec_title = f"Doc: {title}"
                        else:
                            sec_title = f"Doc: {title} - {first_line[:40]}"
                        self.add_entry(
                            category="reference",
                            title=sec_title,
                            content=sec[:1200],
                            source="bootstrap",
                            tags=["doc", title.replace(".md", "")],
                            confidence=0.9,
                        )
                        count += 1
                else:
                    self.add_entry(
                        category="reference",
                        title=f"Doc: {title}",
                        content=snippet,
                        source="bootstrap",
                        tags=["doc", title.replace(".md", "")],
                        confidence=0.9,
                    )
                    count += 1
            except Exception:
                pass

        # 3. 已有 user_memory → 知识
        try:
            from .user_memory import get_user_memory
            um = get_user_memory()
            recent_reqs = um.search_recent(limit=10)
            for r in recent_reqs:
                title = r.get("title", "?")
                ctx = r.get("context", "")
                scenario = r.get("scenario", "")
                if not title or len(title) < 3:
                    continue
                self.add_entry(
                    category="workflow",
                    title=f"历史需求: {title[:60]}",
                    content=f"场景: {scenario}\n背景: {ctx[:400]}",
                    source="bootstrap",
                    tags=["history", "requirement"],
                    confidence=0.85,
                )
                count += 1
        except Exception:
            pass

        # 4. 已有 deliverable_versions → 已有分析模板
        try:
            from .persistence_sqlite import SQLitePersistence
            persist = SQLitePersistence()
            # 简单 LIKE 查 (不用 engine)
            with self._connect() as conn:
                # 不动 workflow 自己的 db, 写一个 knowledge entry 标记 deliverable 有数据
                self.add_entry(
                    category="code",
                    title="可用 deliverable 模板 (从历史 workflow 抽)",
                    content="历史上保存的所有 deliverable_versions 都在 mavis-workflow.db 的 deliverable_versions 表. 用 `deliverable_history(wf_id, step_id)` 调取, 恢复时创建新版本.",
                    source="bootstrap",
                    tags=["deliverable", "version"],
                    confidence=0.9,
                )
                count += 1
        except Exception:
            pass

        self._bootstrap_done = True
        self._bootstrap_db = self.db_path
        return count

    # === Auto: workflow 跑完自动 extract ===

    def extract_learn_from_workflow(self, wf_id: str) -> Optional[int]:
        """从已完成 workflow 提炼 1 条 learn, 返回 entry_id"""
        try:
            from .persistence_sqlite import SQLitePersistence
            persist = SQLitePersistence()
            wf = persist.get_workflow(wf_id)
            if not wf:
                return None
            name = getattr(wf, "name", wf_id) or wf_id
            steps = getattr(wf, "steps", []) or []
            results = getattr(wf, "results", {}) or {}
            # 找 user 决策点 + 实际产物
            learnings = []
            for s in steps:
                rid = getattr(s, "id", None) or s.get("id") if isinstance(s, dict) else None
                if not rid:
                    continue
                r = results.get(rid) if isinstance(results, dict) else None
                if not r:
                    continue
                summary = getattr(r, "summary", "") or ""
                output = getattr(r, "output", "") or ""
                deliverable = getattr(r, "deliverable", "") or output
                if deliverable and len(deliverable) > 50:
                    learnings.append({
                        "step": rid,
                        "name": getattr(s, "name", "") if not isinstance(s, dict) else s.get("name", ""),
                        "agent": getattr(s, "agent", "") if not isinstance(s, dict) else s.get("agent", ""),
                        "summary": summary[:300],
                        "deliverable_excerpt": deliverable[:500],
                    })
            if not learnings:
                return None
            content = f"Workflow: {name}\nWf ID: {wf_id}\n"
            for l in learnings[:5]:
                content += f"\n- {l['step']} [{l['agent']}] {l['name']}: {l['summary']}\n  Output: {l['deliverable_excerpt'][:200]}..."
            entry_id = self.add_entry(
                category="workflow",
                title=f"Workflow 总结: {name[:50]}",
                content=content[:2000],
                source="auto",
                tags=["history", "workflow_result", wf_id],
                confidence=0.75,
            )
            return entry_id
        except Exception as e:
            return None

    # === Recall: 喂给 LLM 的 KB 上下文 ===

    def recall_for_context(self, context_text: str, limit: int = 5) -> str:
        """根据 context 找相关 KB 条目, 拼成 markdown 段落给 LLM prompt 用"""
        if not context_text or not context_text.strip():
            return ""
        # 简单分词 (中文按 char, 英文按 word)
        keywords = _extract_keywords(context_text)
        if not keywords:
            return ""
        # 取前 3 个 keyword 搜索
        all_entries = []
        seen_ids = set()
        for kw in keywords[:3]:
            for e in self.search(kw, limit=3):
                if e.id not in seen_ids:
                    seen_ids.add(e.id)
                    all_entries.append(e)
        if not all_entries:
            return ""
        md = "**📚 相关知识库条目 (供参考):**\n"
        for e in all_entries[:limit]:
            tag_str = ", ".join(e.tags[:3]) if e.tags else ""
            md += f"\n- [{e.category}] **{e.title}** (来源: {e.source}, 置信度: {e.confidence:.1f}{', 标签: ' + tag_str if tag_str else ''})\n"
            md += f"  {e.content[:300]}{'...' if len(e.content) > 300 else ''}\n"
        return md

    # === Helpers ===

    def _row_to_entry(self, row) -> KBEntry:
        if not row:
            return None
        return KBEntry(
            id=row[0],
            category=row[1],
            title=row[2],
            content=row[3],
            source=row[4],
            tags=[t for t in (row[5] or "").split(",") if t],
            confidence=row[6],
            created_at=row[7],
            updated_at=row[8],
        )


def _now() -> str:
    """ISO timestamp (UTC)"""
    import datetime
    return datetime.datetime.utcnow().isoformat() + "Z"


def _extract_keywords(text: str) -> List[str]:
    """从 text 提取关键词 (中英混合)"""
    if not text:
        return []
    # 中文字符 2-gram
    keywords = []
    # 英文 words
    en_words = re.findall(r"[A-Za-z]{3,}", text)
    keywords.extend([w for w in en_words[:5]])
    # 中文 2-gram
    cn_chars = re.findall(r"[\u4e00-\u9fff]", text)
    text_cn = "".join(cn_chars)
    for i in range(len(text_cn) - 1):
        gram = text_cn[i:i+2]
        if gram not in keywords:
            keywords.append(gram)
    return keywords[:10]


# === Singleton ===

_kb_instance: Optional[KnowledgeBase] = None


def get_kb() -> KnowledgeBase:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance
