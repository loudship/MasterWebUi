import sqlite3
import json

with open("db_output.txt", "w", encoding="utf-8") as f:
    conn = sqlite3.connect(r'c:\open-webui-master\data\open-webui\webui.db')
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    f.write(f"Tables: {tables}\n")
    
    try:
        rows = conn.execute("SELECT * FROM config").fetchall()
        for row in rows:
            f.write(f"Config row: {row}\n")
    except Exception as e:
        f.write(f"Error querying config directly: {e}\n")

    conn.close()
