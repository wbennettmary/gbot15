from app import app, db
from sqlalchemy import text

def fix_sequence():
    with app.app_context():
        print("Fixing whitelisted_ip_id_seq sequence...")
        try:
            # Get the maximum ID from the table
            result = db.session.execute(text("SELECT MAX(id) FROM whitelisted_ip"))
            max_id = result.scalar() or 0
            
            # Reset the sequence to max_id + 1
            new_val = max_id + 1
            print(f"Max ID is {max_id}. Resetting sequence to {new_val}...")
            
            db.session.execute(text(f"SELECT setval('whitelisted_ip_id_seq', {new_val}, false)"))
            db.session.commit()
            
            print("Sequence fixed successfully.")
        except Exception as e:
            print(f"Error fixing sequence: {e}")
            db.session.rollback()

if __name__ == "__main__":
    fix_sequence()
