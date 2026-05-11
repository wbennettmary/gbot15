from app import app, db
from database import UsedDomain
import sys

def test_write():
    with app.app_context():
        print("Checking initial count...")
        initial_count = UsedDomain.query.count()
        print(f"Initial count: {initial_count}")

        print("Attempting to add test domain...")
        test_domain = "test-db-write-verification.com"
        
        # Cleanup
        existing = UsedDomain.query.filter_by(domain_name=test_domain).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()
            print("Deleted existing test domain.")

        # Create
        new_domain = UsedDomain(
            domain_name=test_domain,
            user_count=0,
            is_verified=True,
            ever_used=True
        )
        db.session.add(new_domain)
        try:
            db.session.commit()
            print("Successfully committed to DB.")
        except Exception as e:
            print(f"FAILED to commit: {e}")
            sys.exit(1)

        # Verify
        final_count = UsedDomain.query.count()
        print(f"Final count: {final_count}")
        
        retrieved = UsedDomain.query.filter_by(domain_name=test_domain).first()
        if retrieved and retrieved.ever_used:
             print("VERIFICATION SUCCESS: Domain retrieved and matches.")
        else:
             print("VERIFICATION FAILED: Domain not found or wrong data.")

if __name__ == "__main__":
    test_write()
