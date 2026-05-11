
import sys
import os
import logging

# Add current directory to path
sys.path.append(os.getcwd())

from app import app, db
from services.google_domains_service import GoogleDomainsService
from database import ServiceAccount, GoogleAccount

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_verify():
    # Force POSTGRES connection
    app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://gbot_user:gbot_password@localhost:5432/gbot_db'
    
    with app.app_context():
        print(f"DEBUG: Active Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
        
        target_email = "admin@nbgbnvgklkjkeher34gsxscyc.accesscam.org"
        domain = "dfgvbdfh.mentorcrafter.it.com"
        
        print(f"\n--- LOOKING FOR ACCOUNT: {target_email} ---")
        
        # Check ServiceAccount
        sa = ServiceAccount.query.filter_by(name=target_email).first()
        if not sa:
            sa = ServiceAccount.query.filter_by(admin_email=target_email).first()
            
        if sa:
            print(f"FOUND in ServiceAccount! ID: {sa.id}, Name: {sa.name}")
            account_name = sa.name
        else:
            print("NOT FOUND in ServiceAccount table.")
            
            # Check GoogleAccount
            ga = GoogleAccount.query.filter_by(account_name=target_email).first()
            if ga:
                 print(f"FOUND in GoogleAccount! ID: {ga.id}, Account Name: {ga.account_name}")
                 account_name = ga.account_name
            else:
                print("NOT FOUND in GoogleAccount table either.")
                print("\nListing ALL ServiceAccounts:")
                for s in ServiceAccount.query.all():
                    print(f" - SA: {s.name} (Admin: {s.admin_email})")
                print("\nListing ALL GoogleAccounts:")
                for g in GoogleAccount.query.all():
                    print(f" - GA: {g.account_name}")
                return

        print(f"\n--- STARTING VERIFICATION FOR {domain} ---")
        service = GoogleDomainsService(account_name)
        
        try:
            print("Calling verify_domain()...")
            # We assume apex is domain for simpler test, or let service calc it
            result = service.verify_domain(domain)
            print("\n--- RESULT ---")
            print(result)
            
        except Exception as e:
            print("\n--- EXCEPTION ---")
            print(e)
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    debug_verify()
