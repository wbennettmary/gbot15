import os
import json
import sys
from google.oauth2 import service_account
from googleapiclient.discovery import build

def debug_auth():
    print("=== Google Service Account Debugger ===")
    
    # 1. Get JSON File
    json_path = input("Enter path to JSON key file: ").strip()
    if not os.path.exists(json_path):
        print(f"Error: File not found at {json_path}")
        return

    try:
        with open(json_path, 'r') as f:
            info = json.load(f)
            print(f"\n[JSON Loaded]")
            print(f"Client Email: {info.get('client_email')}")
            print(f"Project ID: {info.get('project_id')}")
            print(f"Client ID: {info.get('client_id')}")
            print(f"Private Key ID: {info.get('private_key_id')}")
    except Exception as e:
        print(f"Error reading JSON: {e}")
        return

    # 2. Get Admin Email
    admin_email = input("\nEnter Admin Email to impersonate: ").strip()
    print(f"Subject: {admin_email}")

    # 3. Define Scopes (Exact match to G_Bot_api.py)
    SCOPES = [
        "https://www.googleapis.com/auth/admin.directory.user", 
        "https://www.googleapis.com/auth/admin.directory.user.security", 
        "https://www.googleapis.com/auth/admin.directory.orgunit", 
        "https://www.googleapis.com/auth/admin.directory.domain.readonly",
        "https://www.googleapis.com/auth/gmail.send"
    ]
    print(f"\nScopes: {SCOPES}")

    # 4. Attempt Auth
    print("\nAttempting Authentication...")
    try:
        creds = service_account.Credentials.from_service_account_file(
            json_path, scopes=SCOPES
        ).with_subject(admin_email)
        
        service = build('admin', 'directory_v1', credentials=creds)
        
        print("Listing domains (limit 1)...")
        results = service.domains().list(customer='my_customer', maxResults=1).execute()
        
        print("\nSUCCESS! Authentication worked.")
        print(f"Domains found: {results.get('domains', [])}")
        
    except Exception as e:
        print(f"\nFAILURE: {e}")
        if hasattr(e, 'content'):
            print(f"Error Content: {e.content}")

if __name__ == "__main__":
    debug_auth()
