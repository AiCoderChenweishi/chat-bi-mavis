#!/usr/bin/env python3
import sys
sys.path.insert(0, '/opt/data-analyst-agent')
from agent.knowledge_base import get_kb
import os

kb = get_kb()
print("path=", kb.db_path)
print("exists=", os.path.exists(kb.db_path))
print("readable=", os.access(kb.db_path, os.R_OK))
c = kb._connect()
print("connect ok")
cur = c.execute("SELECT 1").fetchone()
print("query ok:", cur)
# 看 FTS5 表
try:
    cur = c.execute("SELECT 1 FROM kb_entries_fts LIMIT 1")
    print("FTS5 ok:", cur.fetchone())
except Exception as e:
    print("FTS5 err:", e)
# 测 search
try:
    r = kb.search("复购率")
    print(f"LIKE 搜 '复购率': {len(r)} 条")
except Exception as e:
    print(f"LIKE err: {e}")
# 测 FTS5
try:
    r = kb.search_fts5("复购率")
    print(f"FTS5 搜 '复购率': {len(r)} 条")
except Exception as e:
    print(f"FTS5 err: {e}")
