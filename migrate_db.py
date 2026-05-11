from app import app
from database import db
from sqlalchemy import text

def migrate():
    """
    Database migration script for GBot.
    Run this after deploying to add new columns/tables.
    
    Usage: python3 migrate_db.py
    """
    with app.app_context():
        print("=" * 60)
        print("GBot Database Migration")
        print("=" * 60)
        
        # Step 1: Create all missing tables first
        print("\n[Step 1] Ensuring all tables exist...")
        db.create_all()
        print("✅ All tables created/verified.")
        
        # Step 2: Refresh inspector to get current state
        inspector = db.inspect(db.engine)
        tables = inspector.get_table_names()
        print(f"\n[Step 2] Found {len(tables)} tables in database.")
        
        # Step 3: Add multi-tenant naming columns to aws_config
        print("\n[Step 3] Checking aws_config for multi-tenant columns...")
        
        if 'aws_config' in tables:
            # Refresh columns list
            columns = [col['name'] for col in inspector.get_columns('aws_config')]
            print(f"   Current columns: {columns}")
            
            # Define new columns for multi-tenant support
            new_columns = [
                ('instance_name', "VARCHAR(100) DEFAULT 'default'"),
                ('ecr_repo_name', "VARCHAR(255) DEFAULT 'gbot-app-password-worker'"),
                ('lambda_prefix', "VARCHAR(100) DEFAULT 'gbot-chromium'"),
                ('dynamodb_table', "VARCHAR(255) DEFAULT 'gbot-app-passwords'"),
            ]
            
            added_count = 0
            for col_name, col_type in new_columns:
                if col_name not in columns:
                    print(f"   Adding column: {col_name}...")
                    try:
                        db.session.execute(text(f'ALTER TABLE aws_config ADD COLUMN {col_name} {col_type}'))
                        db.session.commit()
                        print(f"   ✅ Column {col_name} added successfully.")
                        added_count += 1
                    except Exception as e:
                        db.session.rollback()
                        # Check if column already exists (race condition)
                        if 'duplicate column' in str(e).lower() or 'already exists' in str(e).lower():
                            print(f"   ✅ Column {col_name} already exists (verified).")
                        else:
                            print(f"   ❌ Error adding column {col_name}: {e}")
                else:
                    print(f"   ✅ Column {col_name} already exists.")
            
            if added_count > 0:
                print(f"\n   Added {added_count} new column(s) to aws_config.")
            else:
                print(f"\n   All multi-tenant columns already exist.")
        else:
            print("   ❌ aws_config table not found (should have been created in Step 1)")
        
        # Step 4: Verify final state
        print("\n[Step 4] Verifying migration...")
        inspector = db.inspect(db.engine)
        if 'aws_config' in inspector.get_table_names():
            final_columns = [col['name'] for col in inspector.get_columns('aws_config')]
            required = ['instance_name', 'ecr_repo_name', 'lambda_prefix', 'dynamodb_table']
            missing = [c for c in required if c not in final_columns]
            
            if not missing:
                print("   ✅ All required columns present in aws_config!")
                print(f"   Final columns: {final_columns}")
            else:
                print(f"   ❌ Missing columns: {missing}")
        
        print("\n" + "=" * 60)
        print("Migration complete!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Restart the service: sudo systemctl restart gbot")
        print("2. Hard refresh browser: Ctrl+Shift+R")
        print("3. Go to Settings page to configure multi-tenant naming")
        print("=" * 60)

if __name__ == "__main__":
    migrate()
