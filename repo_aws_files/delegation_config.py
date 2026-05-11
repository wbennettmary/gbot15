"""
Domain-Wide Delegation Configuration for Google Workspace
This module provides automation for configuring OAuth scopes in Google Workspace Admin Console
"""

import time
import subprocess
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# Required OAuth scopes for domain-wide delegation
REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.domain",
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/siteverification"
]


def get_client_id_from_gcloud(sa_email, project_id):
    """Get service account unique ID (client ID) using gcloud"""
    try:
        print(f"Getting client ID for {sa_email}...")
        result = subprocess.run(
            ["gcloud", "iam", "service-accounts", "describe", sa_email, 
             "--project", project_id, "--format=value(uniqueId)"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            client_id = result.stdout.strip()
            print(f"Client ID: {client_id}")
            return client_id
        else:
            print(f"Failed to get client ID: {result.stderr}")
            return None
    except Exception as e:
        print(f"Exception getting client ID: {e}")
        return None


def configure_domain_wide_delegation(driver, email, password, client_id, scopes=None):
    """
    Configure domain-wide delegation in Google Workspace Admin Console
    
    Args:
        driver: Selenium WebDriver instance
        email: Workspace admin email
        password: Workspace admin password
        client_id: Service account client ID (unique ID)
        scopes: List of OAuth scopes to grant (defaults to REQUIRED_SCOPES)
    
    Returns:
        dict: {"success": bool, "message": str, "manual_instructions": str (if failed)}
    """
    if scopes is None:
        scopes = REQUIRED_SCOPES
    
    print(f"\\n{'='*60}")
    print(f"Configuring domain-wide delegation...")
    print(f"Client ID: {client_id}")
    print(f"Scopes: {', '.join(scopes)}")
    print(f"{'='*60}\\n")
    
    # Prepare manual instructions in case automation fails
    scopes_string = ",".join(scopes)
    manual_instructions = f"""
{'='*60}
MANUAL CONFIGURATION REQUIRED
{'='*60}

The automated configuration failed. Please complete these steps manually:

1. Go to: https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en
2. Login with: {email}
3. Click "Add new"
4. Enter Client ID: {client_id}
5. Enter OAuth Scopes (comma-separated):
   {scopes_string}
6. Click "Authorize"

Required Scopes:
{chr(10).join('  - ' + scope for scope in scopes)}

{'='*60}
"""
    
    try:
        # Navigate to Admin Console
        print("Step 1: Navigating to Google Workspace Admin Console...")
        driver.get("https://admin.google.com")
        time.sleep(3)
        
        # Check if login is needed
        current_url = driver.current_url
        if "accounts.google.com" in current_url or "ServiceLogin" in current_url:
            print("Step 2: Login required, performing Google login...")
            # Import google_login from prep.py context
            from prep import google_login
            if not google_login(driver, email, password):
                print("✗ Login failed")
                return {
                    "success": False,
                    "message": "Failed to login to Admin Console",
                    "manual_instructions": manual_instructions
                }
            print("✓ Login successful")
        else:
            print("✓ Already logged in")
        
        # Navigate to Domain-wide Delegation page
        print("Step 3: Navigating to Domain-wide Delegation settings...")
        delegation_url = "https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en"
        driver.get(delegation_url)
        time.sleep(5)
        
        # Find and click "Add new" button
        print("Step 4: Looking for 'Add new' button...")
        add_button = None
        selectors = [
            "//button[contains(text(), 'Add new')]",
            "//button[contains(text(), 'ADD NEW')]",
            "//span[contains(text(), 'Add new')]/ancestor::button",
            "//button[contains(@aria-label, 'Add')]"
        ]
        
        for selector in selectors:
            try:
                add_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                print(f"✓ Found 'Add new' button")
                break
            except:
                continue
        
        if not add_button:
            # Fallback: search all buttons
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if "add" in btn.text.lower() or "new" in btn.text.lower():
                    add_button = btn
                    print(f"✓ Found button: {btn.text}")
                    break
        
        if add_button:
            add_button.click()
            print("✓ Clicked 'Add new'")
            time.sleep(2)
        else:
            raise Exception("Could not find 'Add new' button")
        
        # Enter Client ID
        print("Step 5: Entering client ID...")
        client_id_input = None
        selectors = [
            "//input[@name='clientId']",
            "//input[@aria-label='Client ID']",
            "//input[contains(@placeholder, 'Client ID')]",
            "//input[@type='text'][1]"
        ]
        
        for selector in selectors:
            try:
                client_id_input = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                break
            except:
                continue
        
        if not client_id_input:
            inputs = driver.find_elements(By.TAG_NAME, "input")
            for inp in inputs:
                if inp.get_attribute("type") in ["text", ""] and inp.is_displayed():
                    client_id_input = inp
                    break
        
        if client_id_input:
            client_id_input.clear()
            client_id_input.send_keys(client_id)
            print(f"✓ Entered client ID")
            time.sleep(1)
        else:
            raise Exception("Could not find client ID input field")
        
        # Enter OAuth Scopes
        print("Step 6: Entering OAuth scopes...")
        scopes_input = None
        selectors = [
            "//textarea[@name='scopes']",
            "//textarea[@aria-label='OAuth scopes']",
            "//textarea[contains(@placeholder, 'scope')]",
            "//textarea[1]"
        ]
        
        for selector in selectors:
            try:
                scopes_input = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                break
            except:
                continue
        
        if not scopes_input:
            textareas = driver.find_elements(By.TAG_NAME, "textarea")
            if textareas:
                scopes_input = textareas[0]
        
        if scopes_input:
            scopes_input.clear()
            scopes_input.send_keys(scopes_string)
            print(f"✓ Entered scopes")
            time.sleep(1)
        else:
            raise Exception("Could not find scopes input field")
        
        # Click Authorize button
        print("Step 7: Clicking Authorize button...")
        authorize_button = None
        selectors = [
            "//button[contains(text(), 'Authorize')]",
            "//button[contains(text(), 'AUTHORIZE')]",
            "//button[contains(text(), 'Save')]",
            "//span[contains(text(), 'Authorize')]/ancestor::button",
            "//button[@type='submit']"
        ]
        
        for selector in selectors:
            try:
                authorize_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                break
            except:
                continue
        
        if not authorize_button:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                btn_text = btn.text.lower()
                if any(word in btn_text for word in ["authorize", "save", "submit"]):
                    authorize_button = btn
                    break
        
        if authorize_button:
            authorize_button.click()
            print("✓ Clicked Authorize")
            time.sleep(3)
            
            print("\\n" + "="*60)
            print("✓✓✓ Domain-wide delegation configured successfully!")
            print("="*60 + "\\n")
            
            return {
                "success": True,
                "message": "Domain-wide delegation configured successfully",
                "client_id": client_id,
                "scopes": scopes
            }
        else:
            raise Exception("Could not find Authorize/Save button")
            
    except Exception as e:
        print(f"\\n✗ Automation failed: {e}")
        import traceback
        traceback.print_exc()
        print(manual_instructions)
        return {
            "success": False,
            "message": f"Automation failed: {e}",
            "manual_instructions": manual_instructions
        }
