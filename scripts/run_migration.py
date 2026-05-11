
import os
import sys

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db
from sqlalchemy import text

def run_migration():
    print("Starting migration...")
    try:
        with app.app_context():
            # Read the SQL file
            with open('migrations/add_secret_key_execution_id.sql', 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            # Split by statement if needed, or execute as one block if supported
            # SQLAlchemy execute might need individual statements for some drivers, 
            # but usually handles blocks if they are valid SQL.
            # However, the SQL file has comments which might cause issues if not handled.
            # Let's try executing the whole block first.
            print(f"Executing SQL from migrations/add_secret_key_execution_id.sql")
            
            # Use connection for DDL
            with db.engine.connect() as conn:
                # Transacions for DDL in Postgres
                trans = conn.begin()
                try:
                    conn.execute(text(sql_content))
                    trans.commit()
                    print("Migration executed successfully!")
                except Exception as e:
                    trans.rollback()
                    print(f"Error executing migration: {e}")
                    raise
                    
    except Exception as e:
        print(f"Migration failed: {e}")

if __name__ == "__main__":
    run_migration()
