import sqlite3
import json

db_path = r'c:\open-webui-master\data\open-webui\webui.db'
conn = sqlite3.connect(db_path)

try:
    row = conn.execute("SELECT id, data FROM config").fetchone()
    if row:
        config_id, data_str = row
        data = json.loads(data_str)
        
        updated = False
        if 'direct' in data:
            data['direct']['enable'] = False
            updated = True
        
        if updated:
            new_data_str = json.dumps(data)
            conn.execute("UPDATE config SET data = ? WHERE id = ?", (new_data_str, config_id))
            conn.commit()
            print("Successfully updated database for direct connections.")
            print(f"Set direct.enable to {data['direct']['enable']}")
        else:
            print("No updates needed.")
except Exception as e:
    print(f"Error: {e}")

conn.close()
