import sqlite3
import os
from pathlib import Path

# Use environment variable or default path
DB_PATH = os.getenv('WEBUI_DB_PATH', 'data/open-webui/webui.db')
USER_EMAIL = os.getenv('RESET_USER_EMAIL')
PASSWORD_HASH = os.getenv('PASSWORD_HASH')

if not USER_EMAIL or not PASSWORD_HASH:
    raise ValueError("RESET_USER_EMAIL and PASSWORD_HASH environment variables must be set")

# Ensure DB exists before attempting connection
db_path = Path(DB_PATH)
if not db_path.exists():
    raise FileNotFoundError(f"Database not found at {DB_PATH}")

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Verify email exists before updating
    cursor.execute("SELECT id FROM auth WHERE email = ?", (USER_EMAIL,))
    user = cursor.fetchone()
    
    if not user:
        raise ValueError(f"User with email {USER_EMAIL} not found")

    # Update password
    cursor.execute("UPDATE auth SET password = ? WHERE email = ?", (PASSWORD_HASH, USER_EMAIL))
    conn.commit()

    # Verify update
    cursor.execute("SELECT id, email FROM auth WHERE email = ?", (USER_EMAIL,))
    updated = cursor.fetchone()
    
    if updated:
        print(f"Successfully reset password for user: {updated[1]}")
    else:
        print("Warning: Update executed but user not found on verification")

except sqlite3.DatabaseError as e:
    print(f"Database error: {e}")
    raise
except Exception as e:
    print(f"Error: {e}")
    raise
finally:
    if conn:
        conn.close()
