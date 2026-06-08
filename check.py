import sqlite3
try:
    c = sqlite3.connect('./data/open-webui/webui.db')
    for row in c.execute("SELECT sql FROM sqlite_master WHERE type='table';").fetchall():
        print(row[0])
except Exception as e:
    print(e)
