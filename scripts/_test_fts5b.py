import sys
sys.path.insert(0, '/opt/data-analyst-agent')
from agent.knowledge_base import get_kb
kb = get_kb()
with kb._connect() as c:
    # 1 字符
    for q in ['智', '能', '投', '顾', 'D', 'A', 'U', 'm', 'o', 'c', 'k', '数', '据']:
        r = c.execute("SELECT rowid FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", (q,)).fetchall()
        print(f"  {q!r}: {len(r)}")
    # 英文
    print()
    for q in ['page', 'page_top', 'GMV', 'DAU', 'APP']:
        r = c.execute("SELECT rowid FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", (q,)).fetchall()
        print(f"  {q!r}: {len(r)}")
    # 看 fts5 schema
    print()
    print("FTS5 schema:", c.execute("SELECT sql FROM sqlite_master WHERE name='kb_entries_fts'").fetchall())
    # 看 unicode61 options
    print()
    print("FTS5 rowid 1 content detail:")
    r = c.execute("SELECT rowid, title, content FROM kb_entries_fts WHERE rowid=2").fetchall()
    print(r)
    # 直接 LIKE 对比
    print()
    print("LIKE 智能:", c.execute("SELECT id, title FROM kb_entries WHERE title LIKE '%智能%' OR content LIKE '%智能%'").fetchall())
