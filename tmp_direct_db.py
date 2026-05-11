
import sqlite3
import os

db_path = os.path.join('instance', 'gbot.db')
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- DigitalOcean Execution Table ---")
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='digital_ocean_execution'")
        if not cursor.fetchone():
            print("Table 'digital_ocean_execution' dose not exist!")
        else:
            cursor.execute("SELECT id, task_id, status, started_at FROM digital_ocean_execution ORDER BY started_at DESC LIMIT 10")
            rows = cursor.fetchall()
            print(f"Found {len(rows)} records.")
            for row in rows:
                print(row)
                
            print("\n--- DigitalOcean Droplet Table ---")
            cursor.execute("SELECT id, droplet_id, ip_address, status, execution_task_id FROM digital_ocean_droplet LIMIT 10")
            droplets = cursor.fetchall()
            for d in droplets:
                print(d)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
