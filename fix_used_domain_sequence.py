import sys
import os

# Ensure we can import from the main app
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app import app
from database import db

def reset_sequences():
    with app.app_context():
        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        is_postgres = 'postgre' in db_uri.lower()
        is_sqlite = 'sqlite' in db_uri.lower()

        # List of common tables that might have sequence issues
        tables = [
            'user_app_password',
            'aws_generated_password',
            'used_domain',
            'google_account',
            'google_token',
            'automation_account',
            'retrieved_user',
            'namecheap_config',
            'cloudflare_config',
            'aws_config',
            'server_config',
            'user',
            'proxy_config',
            'two_captcha_config'
        ]

        success_count = 0
        for table in tables:
            try:
                if is_postgres:
                    seq_name = f"{table}_id_seq"
                    sql = f"""
                    SELECT setval(
                        '{seq_name}',
                        COALESCE((SELECT MAX(id) + 1 FROM {table}), 1),
                        false
                    );
                    """
                    db.session.execute(db.text(sql))
                elif is_sqlite:
                    # In SQLite, sequences are in sqlite_sequence
                    # We can update it to be MAX(id) to ensure next insert gets MAX(id)+1
                    sql = f"""
                    UPDATE sqlite_sequence 
                    SET seq = (SELECT COALESCE(MAX(id), 0) FROM {table}) 
                    WHERE name = '{table}';
                    """
                    res = db.session.execute(db.text(sql))
                    
                    # If the table wasn't in sqlite_sequence yet (no inserts ever done), it does no harm
                else:
                    print(f"Unsupported database wrapper: {db_uri}")
                    return

                db.session.commit()
                print(f"✅ Reset sequence for: {table}")
                success_count += 1
            except Exception as e:
                db.session.rollback()
                print(f"⚠️ Could not reset {table} (Might not exist or different schema): {e}")

        print(f"\nDone! Successfully reset {success_count} sequences.")

if __name__ == '__main__':
    print("Starting database sequence fix...")
    reset_sequences()
