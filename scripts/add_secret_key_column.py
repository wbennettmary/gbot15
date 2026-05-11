
import os
import sys
import sqlite3
from sqlalchemy import create_engine, text

# Add parent directory to path to import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db

def add_column():
    print("Attempting to add secret_key column to aws_generated_password table...")
    
    with app.app_context():
        # Get the database URI
        db_uri = app.config['SQLALCHEMY_DATABASE_URI']
        
        if db_uri.startswith('sqlite'):
            # SQLite specific handling
            db_path = db_uri.replace('sqlite:///', '')
            print(f"Detected SQLite database at: {db_path}")
            
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # Check if column exists
                cursor.execute("PRAGMA table_info(aws_generated_password)")
                columns = [info[1] for info in cursor.fetchall()]
                
                if 'secret_key' in columns:
                    print("Column 'secret_key' already exists. Skipping.")
                else:
                    print("Adding 'secret_key' column...")
                    cursor.execute("ALTER TABLE aws_generated_password ADD COLUMN secret_key VARCHAR(100)")
                    conn.commit()
                    print("Column added successfully!")
                    
                conn.close()
            except Exception as e:
                print(f"Error accessing SQLite DB: {e}")
                
        else:
            # PostgreSQL/MySQL handling via SQLAlchemy
            try:
                with db.engine.connect() as conn:
                    # Check if column exists (this is a rough check, might fail on some DBs)
                    try:
                        conn.execute(text("SELECT secret_key FROM aws_generated_password LIMIT 1"))
                        print("Column 'secret_key' already exists. Skipping.")
                    except Exception:
                        print("Adding 'secret_key' column...")
                        conn.execute(text("ALTER TABLE aws_generated_password ADD COLUMN secret_key VARCHAR(100)"))
                        conn.commit()
                        print("Column added successfully!")
            except Exception as e:
                print(f"Error accessing DB: {e}")

if __name__ == "__main__":
    add_column()
