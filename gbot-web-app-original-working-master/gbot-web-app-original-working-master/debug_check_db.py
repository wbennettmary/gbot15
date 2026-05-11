from app import app, db
from database import UsedDomain

def check_db():
    with app.app_context():
        domains = UsedDomain.query.all()
        print(f"Total UsedDomains in DB: {len(domains)}")
        for d in domains:
            print(f"Domain: {d.domain_name}, Ever Used: {d.ever_used}, Verified: {d.is_verified}, Count: {d.user_count}")

if __name__ == "__main__":
    check_db()
