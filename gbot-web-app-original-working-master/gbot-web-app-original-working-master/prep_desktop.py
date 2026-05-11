"""
Google Workspace Prep - Desktop Script
Runs gcloud CLI commands AFTER user has manually authenticated.

USAGE:
1. Open terminal and run: gcloud auth login
2. Complete the browser login manually
3. Then run this script to create project, service account, enable APIs
4. Upload key.json to S3
5. Manually configure domain-wide delegation in Admin Console

NO SELENIUM. NO OAUTH AUTOMATION. JUST CLI COMMANDS.
"""

import os
import subprocess
import time
import json
import random
import string
import boto3
from botocore.config import Config

# S3 Configuration
S3_BUCKET = "glowedu"
S3_KEY_PREFIX = "workspace-keys"

# AWS Region
AWS_REGION = "eu-north-1"

def run_command(command, capture_output=True, timeout=300):
    """Run a shell command and return result"""
    print(f"  → Running: {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            shell=True if isinstance(command, str) else False
        )
        if result.returncode != 0:
            print(f"  ✗ Error: {result.stderr}")
            return None
        print(f"  ✓ Success")
        return result.stdout.strip() if result.stdout else ""
    except subprocess.TimeoutExpired:
        print(f"  ✗ Command timed out")
        return None
    except Exception as e:
        print(f"  ✗ Exception: {e}")
        return None

def check_gcloud_auth():
    """Check if user is authenticated with gcloud"""
    print("\n[STEP 0] Checking gcloud authentication...")
    result = run_command(["gcloud", "auth", "list", "--format=value(account)"])
    if result:
        print(f"  ✓ Authenticated as: {result}")
        return result
    else:
        print("  ✗ Not authenticated!")
        print("\n" + "="*60)
        print("MANUAL ACTION REQUIRED:")
        print("  1. Open a terminal (cmd or PowerShell)")
        print("  2. Run: gcloud auth login")
        print("  3. Complete the browser login")
        print("  4. Then run this script again")
        print("="*60)
        return None

def generate_project_id():
    """Generate a unique project ID"""
    timestamp = int(time.time())
    random_suffix = ''.join(random.choices(string.ascii_lowercase, k=4))
    return f"edu-gw-{timestamp}-{random_suffix}"

def generate_service_account_name():
    """Generate a unique service account name"""
    timestamp = int(time.time())
    return f"sa-{timestamp}"

def create_project(project_id, display_name):
    """Create a new GCP project"""
    print(f"\n[STEP 1] Creating GCP project: {project_id}")
    result = run_command([
        "gcloud", "projects", "create", project_id,
        "--name", display_name
    ])
    if result is None:
        # Check if project already exists
        check = run_command(["gcloud", "projects", "describe", project_id, "--format=value(projectId)"])
        if check:
            print(f"  ℹ Project already exists: {project_id}")
            return True
        return False
    
    # Set as current project
    run_command(["gcloud", "config", "set", "project", project_id])
    
    # Wait for project to be ready
    print("  Waiting for project to be ready...")
    time.sleep(5)
    return True

def create_service_account(project_id, sa_name):
    """Create a service account"""
    print(f"\n[STEP 2] Creating service account: {sa_name}")
    
    result = run_command([
        "gcloud", "iam", "service-accounts", "create", sa_name,
        "--display-name", "Workspace Automation Service Account",
        "--project", project_id
    ])
    
    sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
    
    if result is None:
        # Check if SA already exists
        check = run_command([
            "gcloud", "iam", "service-accounts", "describe", sa_email,
            "--project", project_id, "--format=value(email)"
        ])
        if check:
            print(f"  ℹ Service account already exists: {sa_email}")
            return sa_email
        return None
    
    return sa_email

def disable_org_policy(project_id):
    """Attempt to disable org policy that prevents key creation"""
    print(f"\n[STEP 3] Attempting to disable org policy (may fail if not enforced)...")
    
    result = run_command([
        "gcloud", "resource-manager", "org-policies", "disable-enforce",
        "iam.disableServiceAccountKeyCreation",
        "--project", project_id
    ])
    
    if result is None:
        print("  ℹ Org policy not enforced or couldn't disable (this is usually OK)")
    return True  # Continue even if this fails

def create_service_account_key(project_id, sa_email, key_path):
    """Create service account key"""
    print(f"\n[STEP 4] Creating service account key...")
    
    result = run_command([
        "gcloud", "iam", "service-accounts", "keys", "create", key_path,
        "--iam-account", sa_email,
        "--project", project_id
    ])
    
    if result is None:
        return False
    
    if os.path.exists(key_path):
        print(f"  ✓ Key saved to: {key_path}")
        return True
    return False

def get_service_account_client_id(project_id, sa_email):
    """Get the unique ID (client ID) of the service account"""
    print(f"\n[STEP 5] Getting service account client ID...")
    
    result = run_command([
        "gcloud", "iam", "service-accounts", "describe", sa_email,
        "--project", project_id, "--format=value(uniqueId)"
    ])
    
    if result:
        print(f"  ✓ Client ID: {result}")
        return result
    return None

def enable_apis(project_id):
    """Enable required APIs"""
    print(f"\n[STEP 6] Enabling required APIs...")
    
    apis = [
        "admin.googleapis.com",
        "siteverification.googleapis.com"
    ]
    
    for api in apis:
        print(f"  Enabling {api}...")
        result = run_command([
            "gcloud", "services", "enable", api,
            "--project", project_id
        ])
        if result is None:
            print(f"  ⚠ Warning: Failed to enable {api}")
        else:
            print(f"  ✓ Enabled {api}")
        time.sleep(2)  # APIs need time to enable
    
    return True

def upload_to_s3(key_path, email, project_id):
    """Upload key.json to S3"""
    print(f"\n[STEP 7] Uploading key to S3...")
    
    try:
        s3_client = boto3.client(
            's3',
            region_name=AWS_REGION,
            config=Config(signature_version='s3v4')
        )
        
        s3_key = f"{S3_KEY_PREFIX}/{email}/{project_id}.json"
        
        with open(key_path, 'rb') as f:
            s3_client.upload_fileobj(f, S3_BUCKET, s3_key)
        
        print(f"  ✓ Uploaded to: s3://{S3_BUCKET}/{s3_key}")
        return True
    except Exception as e:
        print(f"  ✗ Failed to upload: {e}")
        return False

def print_domain_delegation_instructions(client_id, sa_email):
    """Print manual instructions for domain-wide delegation"""
    print("\n" + "="*70)
    print("MANUAL ACTION REQUIRED: Configure Domain-Wide Delegation")
    print("="*70)
    print("""
Follow these steps in Google Admin Console:

1. Go to: https://admin.google.com

2. Navigate to: Security → API Controls → Domain-wide Delegation
   (Or directly: https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en)

3. Click "Add new"

4. Enter the following:
""")
    print(f"   Client ID: {client_id}")
    print("""
   OAuth Scopes (copy the entire line):
   https://www.googleapis.com/auth/admin.directory.domain,https://www.googleapis.com/auth/admin.directory.user,https://www.googleapis.com/auth/siteverification

5. Click "Authorize"

6. Done! The service account can now impersonate users in your domain.
""")
    print("="*70)
    print(f"Service Account Email: {sa_email}")
    print("="*70)

def prepare_workspace_account(email):
    """
    Main function to prepare a Google Workspace account.
    
    PREREQUISITE: User must have run 'gcloud auth login' manually first!
    """
    print("\n" + "="*70)
    print("GOOGLE WORKSPACE ACCOUNT PREPARATION")
    print(f"Account: {email}")
    print("="*70)
    
    # Step 0: Check authentication
    auth_account = check_gcloud_auth()
    if not auth_account:
        return {
            "success": False,
            "error": "Not authenticated. Run 'gcloud auth login' first."
        }
    
    # Generate unique IDs
    project_id = generate_project_id()
    sa_name = generate_service_account_name()
    key_path = f"{project_id}.json"
    
    print(f"\n  Project ID: {project_id}")
    print(f"  Service Account: {sa_name}")
    
    # Step 1: Create project
    if not create_project(project_id, f"Workspace {email}"):
        return {"success": False, "error": "Failed to create project"}
    
    # Step 2: Create service account
    sa_email = create_service_account(project_id, sa_name)
    if not sa_email:
        return {"success": False, "error": "Failed to create service account"}
    
    # Step 3: Try to disable org policy (may fail, that's OK)
    disable_org_policy(project_id)
    
    # Step 4: Create service account key
    if not create_service_account_key(project_id, sa_email, key_path):
        return {"success": False, "error": "Failed to create service account key"}
    
    # Step 5: Get client ID
    client_id = get_service_account_client_id(project_id, sa_email)
    if not client_id:
        print("  ⚠ Warning: Could not get client ID")
    
    # Step 6: Enable APIs
    enable_apis(project_id)
    
    # Step 7: Upload to S3
    if upload_to_s3(key_path, email, project_id):
        # Clean up local file
        try:
            os.remove(key_path)
            print(f"  ✓ Cleaned up local key file")
        except:
            pass
    
    # Print manual instructions
    if client_id:
        print_domain_delegation_instructions(client_id, sa_email)
    
    print("\n" + "="*70)
    print("✓ PREPARATION COMPLETE")
    print("="*70)
    print(f"""
Summary:
  - Project ID: {project_id}
  - Service Account: {sa_email}
  - Client ID: {client_id or 'N/A'}
  - Key Location: s3://{S3_BUCKET}/{S3_KEY_PREFIX}/{email}/{project_id}.json
  
Next Steps:
  1. Complete domain-wide delegation in Admin Console (see instructions above)
  2. Wait ~5 minutes for APIs to fully activate
  3. Test using Lambda with the service account key
""")
    
    return {
        "success": True,
        "project_id": project_id,
        "service_account": sa_email,
        "client_id": client_id,
        "key_path": f"s3://{S3_BUCKET}/{S3_KEY_PREFIX}/{email}/{project_id}.json"
    }

# For testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python prep_desktop.py <email@domain.com>")
        print("\nBefore running this script:")
        print("  1. Open terminal")
        print("  2. Run: gcloud auth login")
        print("  3. Complete browser login")
        print("  4. Then run this script")
        sys.exit(1)
    
    email = sys.argv[1]
    result = prepare_workspace_account(email)
    
    if result["success"]:
        print("\n✓ Success!")
    else:
        print(f"\n✗ Failed: {result.get('error', 'Unknown error')}")
