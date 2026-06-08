import sqlite3
import json

db_path = r'c:\open-webui-master\data\open-webui\webui.db'
conn = sqlite3.connect(db_path)

try:
    row = conn.execute("SELECT id, data FROM config").fetchone()
    if row:
        config_id, data_str = row
        data = json.loads(data_str)
        
        # Modify the UI config
        if 'ui' in data:
            data['ui']['enable_signup'] = False
            data['ui']['default_user_role'] = 'pending'
            
            new_data_str = json.dumps(data)
            
            conn.execute("UPDATE config SET data = ? WHERE id = ?", (new_data_str, config_id))
            conn.commit()
            print("Successfully updated database.")
            print(f"Set enable_signup to {data['ui']['enable_signup']}")
            print(f"Set default_user_role to {data['ui']['default_user_role']}")
        else:
            print("No 'ui' key in config data.")
    else:
        print("No config row found.")
except Exception as e:
    print(f"Error: {e}")

conn.close()
