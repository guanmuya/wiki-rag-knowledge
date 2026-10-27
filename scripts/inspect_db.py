import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1])
print("库路径:", db, "存在:", db.exists())
con = sqlite3.connect(str(db))
print("--- users ---")
for r in con.execute("SELECT id,username,role,status,must_change_password FROM users"):
    print(r)
print("--- knowledge_bases ---")
for r in con.execute("SELECT id,name,mode,status FROM knowledge_bases"):
    print(r)
print("--- counts ---")
for t in ("documents", "chunks", "wiki_pages", "messages"):
    try:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n}")
    except Exception as e:
        print(f"  {t}: 查询失败 {e}")
con.close()
