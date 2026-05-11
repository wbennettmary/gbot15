"""
Database migration script to add domain verification tables.
Creates NamecheapConfig and DomainOperation tables.
"""
import os
import sys
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import OperationalError

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SQLALCHEMY_DATABASE_URI
from database import db

def migrate_domain_verification_tables():
    """Add NamecheapConfig and DomainOperation tables."""
    try:
        engine = create_engine(SQLALCHEMY_DATABASE_URI)
        inspector = inspect(engine)
        
        with engine.connect() as conn:
            # Check if using PostgreSQL
            is_postgresql = 'postgresql' in SQLALCHEMY_DATABASE_URI.lower()
            
            print("🔄 Starting domain verification tables migration...")
            
            # Create NamecheapConfig table
            if 'namecheap_config' not in [t.lower() for t in inspector.get_table_names()]:
                print("📝 Creating namecheap_config table...")
                if is_postgresql:
                    conn.execute(text("""
                        CREATE TABLE namecheap_config (
                            id SERIAL PRIMARY KEY,
                            api_user VARCHAR(255) NOT NULL,
                            api_key VARCHAR(255) NOT NULL,
                            username VARCHAR(255) NOT NULL,
                            client_ip VARCHAR(45) NOT NULL,
                            is_configured BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                else:
                    # SQLite
                    conn.execute(text("""
                        CREATE TABLE namecheap_config (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            api_user VARCHAR(255) NOT NULL,
                            api_key VARCHAR(255) NOT NULL,
                            username VARCHAR(255) NOT NULL,
                            client_ip VARCHAR(45) NOT NULL,
                            is_configured BOOLEAN DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                conn.commit()
                print("✅ Created namecheap_config table")
            else:
                print("ℹ️  namecheap_config table already exists")
            
            # Create DomainOperation table
            if 'domain_operation' not in [t.lower() for t in inspector.get_table_names()]:
                print("📝 Creating domain_operation table...")
                if is_postgresql:
                    conn.execute(text("""
                        CREATE TABLE domain_operation (
                            id VARCHAR(36) PRIMARY KEY,
                            job_id VARCHAR(36) NOT NULL,
                            input_domain VARCHAR(255) NOT NULL,
                            apex_domain VARCHAR(255) NOT NULL,
                            workspace_status VARCHAR(50) DEFAULT 'pending',
                            dns_status VARCHAR(50) DEFAULT 'pending',
                            verify_status VARCHAR(50) DEFAULT 'pending',
                            message TEXT,
                            raw_log JSONB,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                    # Create indexes
                    conn.execute(text("CREATE INDEX idx_domain_operation_job_id ON domain_operation(job_id)"))
                    conn.execute(text("CREATE INDEX idx_domain_operation_updated_at ON domain_operation(updated_at)"))
                else:
                    # SQLite
                    conn.execute(text("""
                        CREATE TABLE domain_operation (
                            id VARCHAR(36) PRIMARY KEY,
                            job_id VARCHAR(36) NOT NULL,
                            input_domain VARCHAR(255) NOT NULL,
                            apex_domain VARCHAR(255) NOT NULL,
                            workspace_status VARCHAR(50) DEFAULT 'pending',
                            dns_status VARCHAR(50) DEFAULT 'pending',
                            verify_status VARCHAR(50) DEFAULT 'pending',
                            message TEXT,
                            raw_log TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """))
                    # Create indexes
                    conn.execute(text("CREATE INDEX idx_domain_operation_job_id ON domain_operation(job_id)"))
                    conn.execute(text("CREATE INDEX idx_domain_operation_updated_at ON domain_operation(updated_at)"))
                conn.commit()
                print("✅ Created domain_operation table")
            else:
                print("ℹ️  domain_operation table already exists")
            
            print("✅ Domain verification tables migration completed successfully!")
            return True
    
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    print("🚀 Domain Verification Database Migration Tool")
    print("=" * 50)
    success = migrate_domain_verification_tables()
    if success:
        print("\n✅ Migration completed successfully!")
        sys.exit(0)
    else:
        print("\n❌ Migration failed!")
        sys.exit(1)
