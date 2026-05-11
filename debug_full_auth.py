
import os
import sys
import logging
from app import app
from database import ServiceAccount
from services.simple_domain_service import SimpleDomainService

# Set up logging to stdout
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_manual_verification(admin_email, domain):
    print(f"\n=== TESTING MANUAL VERIFICATION FOR {domain} ===")
    print(f"Target Admin Email: {admin_email}")
    
    with app.app_context():
        # 1. Find account
        sa = ServiceAccount.query.filter_by(name=admin_email).first()
        if not sa:
            sa = ServiceAccount.query.filter_by(admin_email=admin_email).first()
            
        if not sa:
            print("❌ ERROR: Service Account not found in DB!")
            return
            
        print(f"✅ Found Service Account: {sa.name} (ID: {sa.id})")
        print(f"   Admin Email: {sa.admin_email}")
        
        # Check Client ID
        import json
        creds_json = json.loads(sa.json_content)
        print(f"   Client ID (from DB): {creds_json.get('client_id')}")
        print(f"   Project ID: {creds_json.get('project_id')}")
        
        # 2. Init Service
        svc = SimpleDomainService(sa.json_content, sa.admin_email)
        
        # 3. Test Auth & List Domains (Verify access)
        print("\n--- Testing Access & Listing Domains ---")
        try:
            admin_service = svc._get_admin_service()
            # Try to get users to find Customer ID
            try:
                user_res = admin_service.users().get(userKey=admin_email).execute()
                customer_id = user_res.get('customerId')
                print(f"✅ Resolved Customer ID: {customer_id}")
            except:
                print("⚠️ Could not resolve Customer ID from user, using 'my_customer'")
                customer_id = 'my_customer'

            results = admin_service.domains().list(customer=customer_id).execute()
            domains = results.get('domains', [])
            print(f"✅ Access OK! Found {len(domains)} domains.")
            print("Existing domains: " + ", ".join([d['domainName'] for d in domains[:5]]))
        except Exception as e:
            print(f"❌ AUTH ERROR: {e}")
            return

        # 4. Try Add Domain
        print(f"\n--- Adding Domain {domain} ---")
        # CRITICAL FIX: Always add the FULL subdomain, never the apex
        print(f"Adding Full Domain: {domain}")
        
        success, msg = svc.add_domain(domain)
        print(f"Result: {success} - {msg}")
        
        # 5. Get Token
        print(f"\n--- Getting Token for {domain} ---")
        token, msg = svc.get_verification_token(domain)
        print(f"Token: {token}")
        print(f"Msg: {msg}")
        
        if not token:
            print("❌ Stopping because no token.")
            return

        # 6. Verify (Check if it works immediately?)
        print(f"\n--- Checking Verification Status ---")
        verified, v_msg = svc.verify_domain(domain)
        print(f"Verified: {verified}")
        print(f"Message: {v_msg}")
        
        print("\n=== TEST COMPLETE ===")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python debug_full_auth.py <admin_email> <domain_to_add>")
        sys.exit(1)
        
    test_manual_verification(sys.argv[1], sys.argv[2])
