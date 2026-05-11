
import sqlite3
import os

db_path = os.path.join('gbot-web-app-original-working-master', 'gbot-web-app-original-working-master', 'instance', 'gbot.db')
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- DigitalOcean Execution Table (NESTED) ---")
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='digital_ocean_execution'")
        if not cursor.fetchone():
            print("Table 'digital_ocean_execution' does not exist!")
        else:
            cursor.execute("SELECT id, task_id, status, started_at FROM digital_ocean_execution ORDER BY started_at DESC LIMIT 10")
            rows = cursor.fetchall()
            print(f"Found {len(rows)} records.")
            for row in rows:
                print(row)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
