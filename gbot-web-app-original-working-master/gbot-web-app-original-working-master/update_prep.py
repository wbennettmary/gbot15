"""
Script to add Step 3 (domain-wide delegation) to prep.py
"""

# Read the current file
with open('repo_aws_files/prep.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the process_single_user function
start_line = None
end_line = None
for i, line in enumerate(lines):
    if 'def process_single_user(email, password):' in line:
        start_line = i
    if start_line is not None and i > start_line and line.startswith('def '):
        end_line = i
        break

if start_line is None:
    print("ERROR: Could not find process_single_user function")
    exit(1)

if end_line is None:
    # Function is at the end of the file
    end_line = len(lines)

print(f"Found process_single_user at lines {start_line+1} to {end_line}")

# New function code
new_function = '''def process_single_user(email, password):
    """Process a single user through the prep workflow"""
    print(f"\\n{'='*60}")
    print(f"Processing account: {email}")
    print(f"{'='*60}")
    
    # Step 1: Authenticate gcloud
    print("\\n[STEP 1] Authenticating gcloud...")
    if not gcloud_auth_flow(email, password):
        error_msg = "Failed to authenticate gcloud"
        print(f"Error: {error_msg}")
        return {
            "email": email,
            "status": "error",
            "message": error_msg
        }
    
    # Step 2: Setup GCP Resources
    print("\\n[STEP 2] Setting up GCP resources...")
    result = setup_gcp_resources(email)
    
    if not result.get("success"):
        error_msg = result.get("error", "Failed to setup GCP resources")
        print(f"\\nError for {email}: {error_msg}")
        return {
            "email": email,
            "status": "error",
            "message": error_msg
        }
    
    # Step 3: Configure Domain-Wide Delegation
    print("\\n[STEP 3] Configuring domain-wide delegation...")
    client_id = result.get("client_id")
    delegation_result = {"success": False, "message": "Skipped - no client ID"}
    
    if client_id:
        try:
            # Import delegation config module
            import sys
            sys.path.insert(0, os.path.dirname(__file__))
            from delegation_config import configure_domain_wide_delegation, REQUIRED_SCOPES
            
            # Initialize Chrome driver for delegation config
            driver = None
            try:
                driver = get_chrome_driver()
                delegation_result = configure_domain_wide_delegation(
                    driver, email, password, client_id, REQUIRED_SCOPES
                )
            finally:
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
            
            if delegation_result.get("success"):
                print("✓ Domain-wide delegation configured successfully")
            else:
                print(f"⚠ Domain-wide delegation automation failed: {delegation_result.get('message')}")
                if delegation_result.get("manual_instructions"):
                    print(delegation_result["manual_instructions"])
        except Exception as e:
            print(f"⚠ Exception during delegation configuration: {e}")
            import traceback
            traceback.print_exc()
            delegation_result = {"success": False, "message": f"Exception: {e}"}
    else:
        print("⚠ Skipping domain-wide delegation (no client ID available)")
    
    # Final result
    print(f"\\n{'='*60}")
    if delegation_result.get("success"):
        print(f"✓✓✓ Prep Process Completed Successfully for {email}!")
        print(f"    - GCP Project: {result.get('project_id')}")
        print(f"    - Service Account: {result.get('service_account')}")
        print(f"    - Domain-Wide Delegation: Configured")
    else:
        print(f"⚠ Prep Process Partially Completed for {email}")
        print(f"    - GCP Project: {result.get('project_id')}")
        print(f"    - Service Account: {result.get('service_account')}")
        print(f"    - Domain-Wide Delegation: MANUAL CONFIGURATION REQUIRED")
    print(f"{'='*60}\\n")
    
    return {
        "email": email,
        "status": "success" if delegation_result.get("success") else "partial_success",
        "message": "GCP resources created" + (" and delegation configured" if delegation_result.get("success") else " - manual delegation config needed"),
        "project_id": result.get("project_id"),
        "service_account": result.get("service_account"),
        "client_id": client_id,
        "key_path": result.get("key_path"),
        "delegation_configured": delegation_result.get("success", False),
        "delegation_message": delegation_result.get("message"),
        "manual_instructions": delegation_result.get("manual_instructions")
    }

'''

# Replace the function
new_lines = lines[:start_line] + [new_function] + lines[end_line:]

# Write back
with open('repo_aws_files/prep.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f"✓ Successfully updated prep.py")
print(f"  - Replaced lines {start_line+1} to {end_line}")
print(f"  - Added Step 3: Domain-Wide Delegation")
