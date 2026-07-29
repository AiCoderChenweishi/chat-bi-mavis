import sys
sys.path.insert(0, '/opt/data-analyst-agent')
from agent.knowledge_base import get_kb
kb = get_kb()
with kb._connect() as c:
    print("FTS5 raw rows:", c.execute("SELECT rowid, title, content FROM kb_entries_fts LIMIT 5").fetchall())
    print("FTS5 match 智能投顾:", c.execute("SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", ("智能投顾",)).fetchall())
    print("FTS5 match 投顾:", c.execute("SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", ("投顾",)).fetchall())
    print("FTS5 match DAU:", c.execute("SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", ("DAU",)).fetchall())
    print("FTS5 match chinese-quote:", c.execute('SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?', ('"智能"',)).fetchall())
    print("FTS5 match 智能*:", c.execute('SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?', ('智能*',)).fetchall())
    # 测 jieba 预分词
    from agent.rag import _tokenize_chinese
    q = _tokenize_chinese("智能投顾")
    print("jieba 智能投顾 ->", q)
    if q:
        print("FTS5 match jieba 智能投顾:", c.execute("SELECT rowid, title FROM kb_entries_fts WHERE kb_entries_fts MATCH ?", (q,)).fetchall())
