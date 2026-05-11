from app import app, db
from database import CloudflareConfig

def create_table():
    with app.app_context():
        print("Creating CloudflareConfig table if not exists...")
        try:
            # Create table
            CloudflareConfig.__table__.create(db.session.bind, checkfirst=True)
            print("CloudflareConfig table created successfully (or already existed).")
        except Exception as e:
            print(f"Error creating table: {e}")

if __name__ == "__main__":
    create_table()
