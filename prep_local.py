"""
prep_local.py - Local Desktop version of the Cloud Shell Automation

This script runs LOCALLY on your Windows machine.
It uses Selenium to:
1. Login to Google
2. Open Cloud Shell
3. Execute gcloud commands to create resources
4. Download the key
5. Upload the key to S3 (using provided AWS credentials)

Dependencies:
    pip install selenium undetected-chromedriver boto3
"""

import os
import time
import json
import logging
import threading
import boto3
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()

import math
import tempfile
import uuid

# Global lock for driver initialization (undetected_chromedriver patches files and can't be parallel)
_driver_init_lock = threading.Lock()

def get_local_driver(window_index=0, total_windows=1, screen_width=1920, screen_height=1080):
    """Initialize local Chrome driver with window positioning for parallel execution
    
    Args:
        window_index: Index of this window (0-based)
        total_windows: Total number of windows to tile
        screen_width: Screen width in pixels
        screen_height: Screen height in pixels
    """
    # Calculate grid layout for tiling windows
    if total_windows <= 1:
        cols, rows = 1, 1
    elif total_windows == 2:
        cols, rows = 2, 1
    elif total_windows <= 4:
        cols, rows = 2, 2
    elif total_windows <= 6:
        cols, rows = 3, 2
    elif total_windows <= 9:
        cols, rows = 3, 3
    else:
        cols, rows = 4, 3
    
    # Calculate window dimensions
    win_width = screen_width // cols
    win_height = screen_height // rows
    
    # Calculate position
    col = window_index % cols
    row = window_index // cols
    x_pos = col * win_width
    y_pos = row * win_height
    
    print(f"Window {window_index}: Position ({x_pos}, {y_pos}), Size ({win_width}x{win_height})")
    
    # Create unique user data directory for this instance (REQUIRED for parallel Chrome)
    unique_id = f"{window_index}_{uuid.uuid4().hex[:8]}"
    user_data_dir = os.path.join(tempfile.gettempdir(), f"chrome_prep_{unique_id}")
    
    options = webdriver.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={win_width},{win_height}")
    options.add_argument(f"--window-position={x_pos},{y_pos}")
    options.add_argument("--disable-gpu")
    # Unique user data dir for parallel execution
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    # Allow popups and disable popup blocking
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.popups": 1,
        "profile.default_content_settings.popups": 1
    })
    
    print(f"Initializing Chrome Driver (Window {window_index + 1}/{total_windows})...")
    driver = None
    
    # Use lock to serialize driver creation (undetected_chromedriver patches files)
    with _driver_init_lock:
        print(f"[Window {window_index}] Acquired init lock, creating driver...")
        try:
            # Try with use_subprocess=True first (recommended for uc)
            driver = uc.Chrome(options=options, use_subprocess=True)
            print(f"[Window {window_index}] Driver created, releasing lock...")
        except Exception as e:
            print(f"Initial driver initialization failed: {e}")
            print("Retrying with alternative configuration...")
            try:
                # Fallback: try without use_subprocess
                driver = uc.Chrome(options=options, use_subprocess=False)
                print(f"[Window {window_index}] Driver created (fallback), releasing lock...")
            except Exception as e2:
                print(f"Retry failed: {e2}")
                raise e
    
    # SET WINDOW SIZE AND POSITION AFTER CREATION (more reliable than Chrome options)
    try:
        driver.set_window_position(x_pos, y_pos)
        driver.set_window_size(win_width, win_height)
        print(f"Driver {window_index} initialized successfully. Window set to {win_width}x{win_height} at ({x_pos}, {y_pos})")
    except Exception as pos_err:
        print(f"Warning: Could not set window position/size: {pos_err}")
    
    return driver

def login_google(driver, email, password):
    """Login to Google account"""
    print(f"Logging in as {email}...")
    driver.get("https://accounts.google.com/signin")
    time.sleep(3)
    
    try:
        email_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "identifierId"))
        )
        email_field.send_keys(email)
        email_field.send_keys(Keys.ENTER)
        time.sleep(3)
        
        password_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "Passwd"))
        )
        password_field.send_keys(password)
        password_field.send_keys(Keys.ENTER)
        
        print("Waiting for login to complete... (If 2FA appears, please handle it manually in the browser window)")
        time.sleep(8)
        print("Login successful (or proceeded)")
        return True
    except Exception as e:
        print(f"Login error: {e}")
        return False

def open_cloud_shell(driver):
    """Navigate to Cloud Shell and handle all pop-ups"""
    print("Opening Cloud Shell...")
    driver.get("https://shell.cloud.google.com/?hl=en_US&fromcloudshell=true&show=terminal")
    
    print("Waiting for Cloud Shell to load...")
    time.sleep(10)
    
    try:
        # Handle Welcome/Terms pop-up
        print("Checking for Welcome/Terms pop-ups...")
        try:
            checkbox_xpath = "/html/body/div/div[2]/div/mat-dialog-container/div/div/dialog-overlay/div[3]/div[3]/mat-checkbox/div/div/input"
            checkbox = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, checkbox_xpath))
            )
            driver.execute_script("arguments[0].click();", checkbox)
            print("Clicked specific Terms checkbox")
            time.sleep(1)
            
            button_xpath = "/html/body/div/div[2]/div/mat-dialog-container/div/div/dialog-overlay/div[5]/modal-action[1]/button"
            button = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath))
            )
            button.click()
            print("Clicked specific 'Agree and continue' button")
            time.sleep(3)
        except:
            print("Specific checkbox not found, trying generic...")
            try:
                welcome_selectors = [
                    "//button[contains(text(), 'Agree and continue')]",
                    "//span[contains(text(), 'Agree and continue')]/parent::button",
                    "//button[contains(text(), 'Continue')]"
                ]
                for selector in welcome_selectors:
                    try:
                        button = WebDriverWait(driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        button.click()
                        print(f"Clicked pop-up button: {selector}")
                        time.sleep(2)
                        break
                    except:
                        pass
            except:
                print("No welcome pop-up found")

        # Handle Intermediate Dialog
        print("Checking for intermediate dialog...")
        try:
            intermediate_checkbox_xpath = "/html/body/div/div[2]/div/mat-dialog-container/div/div/dialog-overlay/div[3]/div[3]/mat-checkbox/div/div/input"
            try:
                checkbox = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, intermediate_checkbox_xpath))
                )
                driver.execute_script("arguments[0].click();", checkbox)
                print("Clicked intermediate dialog checkbox")
                time.sleep(1)
            except:
                print("Intermediate checkbox not found (may not be required)")
            
            intermediate_button_xpath = "/html/body/div/div[2]/div/mat-dialog-container/div/div/dialog-overlay/div[5]/modal-action[1]"
            try:
                button = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, intermediate_button_xpath))
                )
                driver.execute_script("arguments[0].click();", button)
                print("Clicked intermediate dialog 'Continue' button")
                time.sleep(3)
            except:
                print("Intermediate dialog button not found (may not exist)")
        except Exception as e:
            print(f"Error handling intermediate dialog: {e}")

        # Handle Cloud Shell intro dialog
        print("Checking for secondary Cloud Shell intro dialog...")
        try:
            intro_text_keywords = ["Cloud Shell", "editor", "environment", "Welcome"]
            
            dialogs = driver.find_elements(By.XPATH, "//mat-dialog-container | //div[contains(@class, 'dialog')] | //div[contains(@class, 'modal')]")
            for dialog in dialogs:
                dialog_text = dialog.text
                if any(keyword in dialog_text for keyword in intro_text_keywords):
                    print(f"Found Cloud Shell intro dialog")
                    continue_buttons = dialog.find_elements(By.XPATH, ".//button")
                    for btn in continue_buttons:
                        if any(word in btn.text for word in ["Continue", "Got it", "OK", "Start"]):
                            btn.click()
                            print("Clicked 'Continue' button")
                            time.sleep(2)
                            break
                    break
        except:
            print("Not found in main context, checking iframes...")
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for i, iframe in enumerate(iframes):
                try:
                    driver.switch_to.frame(iframe)
                    dialogs = driver.find_elements(By.XPATH, "//mat-dialog-container | //div[contains(@class, 'dialog')]")
                    for dialog in dialogs:
                        dialog_text = dialog.text
                        if any(keyword in dialog_text for keyword in ["Cloud Shell", "editor", "environment"]):
                            print(f"Found Cloud Shell intro dialog (matched text content)")
                            continue_buttons = dialog.find_elements(By.XPATH, ".//button")
                            for btn in continue_buttons:
                                try:
                                    driver.execute_script("arguments[0].click();", btn)
                                    print("Clicked Continue via JS")
                                    time.sleep(2)
                                    break
                                except:
                                    pass
                            print(f"Handled dialog in iframe {i}")
                            driver.switch_to.default_content()
                            break
                    driver.switch_to.default_content()
                except:
                    driver.switch_to.default_content()

        # Handle Authorize dialog
        print("Checking for Authorize button...")
        def find_and_click_authorize(drv):
            auth_selectors = [
                "//button[contains(text(), 'Authorize')]",
                "//span[contains(text(), 'Authorize')]/parent::button",
                "//button[contains(@class, 'authorize')]"
            ]
            for selector in auth_selectors:
                try:
                    elements = drv.find_elements(By.XPATH, selector)
                    for elem in elements:
                        if elem.is_displayed():
                            try:
                                parent = elem.find_element(By.XPATH, "./ancestor::*[contains(@class, 'dialog') or contains(@class, 'modal') or contains(@class, 'overlay')]")
                                dialog_text = parent.text
                                if "Authorize" in dialog_text and "Cloud Shell" in dialog_text:
                                    print(f"Found Authorize dialog with text: {dialog_text[:50]}...")
                                    print(dialog_text[50:100] + "...")
                            except:
                                pass
                            try:
                                elem.click()
                                print(f"Clicked Authorize via specific XPath")
                                return True
                            except:
                                try:
                                    drv.execute_script("arguments[0].click();", elem)
                                    print(f"Clicked Authorize via JS")
                                    return True
                                except:
                                    pass
                except:
                    pass
            return False
        
        for attempt in range(10):
            if find_and_click_authorize(driver):
                print("Authorize found in main context")
                time.sleep(3)
                break
            
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            found_in_iframe = False
            for i, iframe in enumerate(iframes):
                try:
                    driver.switch_to.frame(iframe)
                    if find_and_click_authorize(driver):
                        print(f"Authorize found in iframe {i}")
                        driver.switch_to.default_content()
                        found_in_iframe = True
                        time.sleep(3)
                        break
                    driver.switch_to.default_content()
                except:
                    driver.switch_to.default_content()
            
            if found_in_iframe:
                break
            
            print(f"Authorize dialog not found, attempt {attempt + 1}/10, waiting...")
            time.sleep(1)
        else:
            print("Authorize dialog not found after 10 attempts")
        
        # Handle re-login pop-up
        print("Checking for re-login pop-up...")
        try:
            email_field = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "identifierId"))
            )
            print("Re-login required, entering credentials...")
        except:
            print("No re-login required")
        
        # Handle Allow dialog
        print("Checking for Allow dialog...")
        try:
            allow_selectors = [
                "//button[contains(text(), 'Allow')]",
                "//span[contains(text(), 'Allow')]/parent::button",
                "//button[@id='submit_approve_access']"
            ]
            for selector in allow_selectors:
                try:
                    allow_btn = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    allow_btn.click()
                    print(f"Clicked 'Allow' button: {selector}")
                    time.sleep(3)
                    break
                except:
                    continue
        except:
            print("No Allow dialog found")
            
    except Exception as e:
        print(f"Error handling Authorize: {e}")
        
    # Wait for terminal
    print("Waiting for terminal...")
    try:
        print("Looking for Cloud Shell iframe...")
        iframe = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "iframe.cloudshell-frame"))
        )
        print("Found Cloud Shell iframe, switching context...")
        driver.switch_to.frame(iframe)
        
        print("Looking for terminal inside iframe...")
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".xterm-screen"))
        )
        print("Terminal found! Ready to send commands.")
        time.sleep(3)
        return True
    except Exception as e:
        print(f"Primary terminal detection failed: {e}")
        print("Trying fallback terminal detection...")
        try:
            driver.switch_to.default_content()
            # Fallback XPath for terminal
            fallback_xpath = "/html/body/cloud-shell-root/div/stand-alone/div[1]/div/horizontal-split/div[2]/devshell/terminal-container/div/xterm-terminal-tab/div/xterm-terminal"
            terminal = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, fallback_xpath))
            )
            print("Terminal found via fallback XPath!")
            time.sleep(3)
            return True
        except Exception as e2:
            print(f"Fallback terminal detection also failed: {e2}")
            try:
                driver.switch_to.default_content()
            except:
                pass
            return False

def send_terminal_command(driver, command):
    """Send command to terminal"""
    print(f"Executing: {command}")
    try:
        textarea = driver.find_element(By.CSS_SELECTOR, "textarea.xterm-helper-textarea")
        
        driver.execute_script("""
            var textarea = arguments[0];
            var command = arguments[1];
            textarea.focus();
            if (window.term) {
                window.term.paste(command + '\\n');
            } else {
                var clipboardData = new DataTransfer();
                clipboardData.setData('text/plain', command);
                var pasteEvent = new ClipboardEvent('paste', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: clipboardData
                });
                textarea.dispatchEvent(pasteEvent);
                setTimeout(function() {
                    var enterEvent = new KeyboardEvent('keydown', {
                        key: 'Enter',
                        code: 'Enter',
                        keyCode: 13,
                        which: 13,
                        bubbles: true
                    });
                    textarea.dispatchEvent(enterEvent);
                }, 100);
            }
        """, textarea, command)
        
        time.sleep(3)
        print(f"Command sent successfully")
    except Exception as e:
        print(f"Error sending command: {e}")

def run_prep_process(email, password, aws_session, s3_bucket, stop_event=None,
                     window_index=0, total_windows=1, screen_width=1920, screen_height=1080):
    """Main execution function called by aws.py
    
    Args:
        email: Google account email
        password: Google account password
        aws_session: boto3 AWS session
        s3_bucket: S3 bucket name for uploads
        stop_event: Threading event to signal stop
        window_index: Index of this window for tiling (0-based)
        total_windows: Total number of parallel windows
        screen_width: Screen width in pixels
        screen_height: Screen height in pixels
    """
    driver = None
    try:
        if stop_event and stop_event.is_set():
            return "Stopped by User"
            
        driver = get_local_driver(window_index, total_windows, screen_width, screen_height)
        
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
        
        if not login_google(driver, email, password):
            return "Login Failed"
            
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
            
        if not open_cloud_shell(driver):
            return "Cloud Shell Failed"
            
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
            
        # Generate IDs - use full email for easy identification
        timestamp = str(int(time.time()))
        
        project_id = f"edu-gw-{timestamp}"
        sa_name = f"sa-{timestamp}"
        # Name JSON file by full email address for easy filtering
        # e.g., admin@example.com.json
        key_path = f"~/{email}.json"
        
        # Press Enter to clear terminal
        print("Pressing Enter to clear terminal...")
        send_terminal_command(driver, "")
        time.sleep(2)
        
        # ============================================================
        # STEP 1: Create Project and Service Account FIRST
        # ============================================================
        print("\n" + "="*80)
        print("STEP 1: Creating GCP Project and Service Account")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"gcloud projects create {project_id} --name 'my first project'")
        time.sleep(10)
        
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
        
        send_terminal_command(driver, f"gcloud config set project {project_id}")
        time.sleep(5)
        
        # ============================================================
        # STEP 1b: Enable Required APIs
        # ============================================================
        print("\n" + "="*80)
        print("STEP 1b: Enabling Required APIs (Admin SDK, Gmail, Site Verification)")
        print("="*80 + "\n")
        
        # Enable Admin SDK API
        send_terminal_command(driver, f"gcloud services enable admin.googleapis.com --project {project_id}")
        time.sleep(8)
        
        # Enable Gmail API
        send_terminal_command(driver, f"gcloud services enable gmail.googleapis.com --project {project_id}")
        time.sleep(8)
        
        # Enable Site Verification API
        send_terminal_command(driver, f"gcloud services enable siteverification.googleapis.com --project {project_id}")
        time.sleep(8)
        
        # Enable Cloud Resource Manager API (needed for org policies)
        send_terminal_command(driver, f"gcloud services enable cloudresourcemanager.googleapis.com --project {project_id}")
        time.sleep(8)
        
        # Enable IAM API
        send_terminal_command(driver, f"gcloud services enable iam.googleapis.com --project {project_id}")
        time.sleep(5)
        
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
        
        send_terminal_command(driver, f"gcloud iam service-accounts create {sa_name} --project {project_id} --display-name 'Automation SA'")
        time.sleep(10)
        
        if stop_event and stop_event.is_set():
            if driver: driver.quit()
            return "Stopped by User"
        
        # ============================================================
        # STEP 2: Grant Organization Permissions
        # ============================================================
        print("\n" + "="*80)
        print("STEP 2: Granting Org Policy Administrator Permission")
        print("="*80 + "\n")
        
        # List organizations and get ORG_ID
        send_terminal_command(driver, "gcloud organizations list")
        time.sleep(5)
        
        send_terminal_command(driver, "ORG_ID=$(gcloud organizations list --format='value(name)' --limit=1)")
        time.sleep(2)
        
        # Grant Org Policy Admin role at ORG level (REQUIRED to disable policy)
        send_terminal_command(driver, f"gcloud organizations add-iam-policy-binding $ORG_ID --member='user:{email}' --role='roles/orgpolicy.policyAdmin'")
        time.sleep(5)
        
        # Optional but safe: also grant org admin
        send_terminal_command(driver, f"gcloud organizations add-iam-policy-binding $ORG_ID --member='user:{email}' --role='roles/resourcemanager.organizationAdmin'")
        time.sleep(5)
        
        # ============================================================
        # STEP 3: Disable LEGACY Constraint at ORGANIZATION Level
        # THIS IS THE CRITICAL STEP - MUST BE AT ORG LEVEL
        # ============================================================
        print("\n" + "="*80)
        print("STEP 3: Disabling LEGACY iam.disableServiceAccountKeyCreation at ORG level")
        print("="*80 + "\n")
        
        # LEGACY constraint at ORGANIZATION level (NOT project level!)
        send_terminal_command(driver, "gcloud resource-manager org-policies disable-enforce iam.disableServiceAccountKeyCreation --organization=$ORG_ID")
        time.sleep(8)
        
        # ============================================================
        # STEP 3b: Get Service Account Unique ID (needed for delegation)
        # ============================================================
        print("\nGetting Service Account Unique ID...")
        send_terminal_command(driver, f"gcloud iam service-accounts describe {sa_name}@{project_id}.iam.gserviceaccount.com --format='value(uniqueId)'")
        time.sleep(5)
        
        # Store the SA email for later use
        sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
        
        # ============================================================
        # STEP 3c: Verify the policy is disabled (authoritative check)
        # ============================================================
        print("\nVerifying policy is disabled...")
        send_terminal_command(driver, "gcloud resource-manager org-policies describe iam.disableServiceAccountKeyCreation --effective --organization=$ORG_ID")
        time.sleep(5)
        
        # ============================================================
        # STEP 4: Configure Domain-Wide Delegation in Admin Console
        # ============================================================
        print("\n" + "="*80)
        print("STEP 4: Configuring Domain-Wide Delegation")
        print("="*80 + "\n")
        
        # First, get the Service Account Unique ID from Cloud Shell
        # The unique ID was already output in the terminal from earlier command
        print("Getting Service Account Unique ID...")
        sa_unique_id = None
        sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
        
        # Run the command again to ensure fresh output in terminal
        unique_id_cmd = f"gcloud iam service-accounts describe {sa_email} --format='value(uniqueId)'"
        print(f"Running: {unique_id_cmd}")
        send_terminal_command(driver, unique_id_cmd)
        time.sleep(8)  # Wait for command to complete
        
        # Switch to default content first
        try:
            driver.switch_to.default_content()
            time.sleep(1)
        except:
            pass
        
        # Find and switch to Cloud Shell iframe
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            cloudshell_iframe = None
            for iframe in iframes:
                try:
                    src = iframe.get_attribute("src") or ""
                    class_attr = iframe.get_attribute("class") or ""
                    if "cloudshell" in src.lower() or "cloudshell" in class_attr.lower():
                        cloudshell_iframe = iframe
                        break
                except:
                    pass
            
            if cloudshell_iframe:
                driver.switch_to.frame(cloudshell_iframe)
                print("Switched to Cloud Shell iframe")
                time.sleep(2)
                
                import re
                
                # Method 1: Try JavaScript to get all terminal text
                for attempt in range(15):
                    try:
                        # Try JavaScript to get terminal content
                        js_methods = [
                            "return document.body.innerText;",
                            "return document.querySelector('.xterm-screen')?.innerText || '';",
                            "return document.querySelector('.xterm-rows')?.innerText || '';",
                            "return Array.from(document.querySelectorAll('[class*=\"xterm\"]')).map(e => e.innerText).join('\\n');",
                            "return Array.from(document.querySelectorAll('div')).filter(e => e.innerText && e.innerText.length > 5).map(e => e.innerText).join('\\n');",
                        ]
                        
                        for js in js_methods:
                            try:
                                terminal_text = driver.execute_script(js)
                                if terminal_text and len(terminal_text) > 20:
                                    # Look for unique ID pattern (15-23 digit number)
                                    # The unique ID should be on its own line or after the prompt
                                    unique_id_match = re.search(r'(?:^|\n|\s)(\d{18,23})(?:\s|\n|$)', terminal_text)
                                    if unique_id_match:
                                        potential_id = unique_id_match.group(1)
                                        # Validate: starts with 10 or 11 (typical for SA unique IDs)
                                        if potential_id.startswith(('10', '11', '12')) and len(potential_id) >= 18:
                                            sa_unique_id = potential_id
                                            print(f"✓ Found Unique ID via JavaScript: {sa_unique_id}")
                                            break
                            except Exception as js_err:
                                if attempt == 0:
                                    print(f"JS method failed: {js_err}")
                        
                        if sa_unique_id:
                            break
                        
                        # Method 2: Try element.text on various selectors
                        if not sa_unique_id:
                            selectors = [".xterm-screen", ".xterm-rows", "[class*='xterm']", "body"]
                            for sel in selectors:
                                try:
                                    elements = driver.find_elements(By.CSS_SELECTOR, sel)
                                    for elem in elements:
                                        text = elem.text
                                        if text and len(text) > 20:
                                            match = re.search(r'(?:^|\n|\s)(\d{18,23})(?:\s|\n|$)', text)
                                            if match:
                                                potential_id = match.group(1)
                                                if potential_id.startswith(('10', '11', '12')) and len(potential_id) >= 18:
                                                    sa_unique_id = potential_id
                                                    print(f"✓ Found Unique ID via element.text: {sa_unique_id}")
                                                    break
                                    if sa_unique_id:
                                        break
                                except:
                                    pass
                        
                        if sa_unique_id:
                            break
                            
                    except Exception as e:
                        if attempt == 0:
                            print(f"Attempt {attempt+1} error: {e}")
                    
                    time.sleep(1.5)
                
                driver.switch_to.default_content()
            else:
                print("Could not find Cloud Shell iframe")
                
        except Exception as e:
            print(f"Error extracting unique ID: {e}")
            try:
                driver.switch_to.default_content()
            except:
                pass
        
        # If still not found, try one more time with page source
        if not sa_unique_id:
            try:
                driver.switch_to.default_content()
                page_source = driver.page_source
                import re
                matches = re.findall(r'\b(\d{18,23})\b', page_source)
                for match in matches:
                    if match.startswith(('10', '11', '12')) and len(match) >= 18:
                        sa_unique_id = match
                        print(f"✓ Found Unique ID from page source: {sa_unique_id}")
                        break
            except:
                pass
        
        # Final fallback - prompt user (only if all automatic methods fail)
        if not sa_unique_id:
            print("\n" + "="*60)
            print("IMPORTANT: Could not automatically extract Unique ID")
            print("="*60)
            print(f"\nLook for the 18-21 digit number in Cloud Shell after this command:")
            print(f"\n{unique_id_cmd}")
            print("\nExample: 117196340698205052125")
            print("="*60)
            
            while True:
                user_input = input("\nPlease enter the Unique ID from Cloud Shell (or 'q' to quit): ").strip()
                
                if user_input.lower() == 'q':
                    print("Exiting...")
                    return None
                
                if user_input.isdigit() and 15 <= len(user_input) <= 25:
                    sa_unique_id = user_input
                    print(f"✓ Using Unique ID: {sa_unique_id}")
                    break
                else:
                    print("Invalid. Enter the 18-21 digit number.")
        
        # Make sure we're in default content before any window operations
        try:
            driver.switch_to.default_content()
            print("Ensured we're in default content")
        except:
            pass
        
        # Save current window handle (Cloud Shell tab)
        original_window = driver.current_window_handle
        original_window_count = len(driver.window_handles)
        print(f"Saved Cloud Shell window handle: {original_window}")
        print(f"Current window count: {original_window_count}")
        
        # Open NEW TAB using Selenium's native method (more reliable than JS)
        print("Opening NEW TAB for Admin Console Domain-Wide Delegation...")
        
        # Method 1: Using Selenium's new_window command (most reliable)
        new_tab = None
        try:
            driver.switch_to.new_window('tab')
            new_tab = driver.current_window_handle
            print(f"Created new tab via Selenium: {new_tab}")
            
            # Navigate to the DWD page
            driver.get("https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en")
            print("Navigated to Domain-Wide Delegation page")
        except Exception as e:
            print(f"Selenium new_window method failed: {e}")
            
            # Method 2: Use keyboard shortcut Ctrl+T
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                actions = ActionChains(driver)
                actions.key_down(Keys.CONTROL).send_keys('t').key_up(Keys.CONTROL).perform()
                time.sleep(2)
                
                all_windows = driver.window_handles
                for window in all_windows:
                    if window != original_window:
                        new_tab = window
                        break
                
                if new_tab:
                    driver.switch_to.window(new_tab)
                    driver.get("https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en")
                    print(f"Created new tab via Ctrl+T: {new_tab}")
            except Exception as e2:
                print(f"Ctrl+T method failed: {e2}")
                
                # Method 3: Fallback to JS with explicit wait
                try:
                    driver.execute_script("window.open('about:blank', '_blank');")
                    time.sleep(3)
                    
                    all_windows = driver.window_handles
                    for window in all_windows:
                        if window != original_window:
                            new_tab = window
                            break
                    
                    if new_tab:
                        driver.switch_to.window(new_tab)
                        driver.get("https://admin.google.com/u/0/ac/owl/domainwidedelegation?hl=en")
                        print(f"Created new tab via JS fallback: {new_tab}")
                except Exception as e3:
                    print(f"JS fallback method failed: {e3}")
        
        # Wait for page to load
        time.sleep(5)
        
        # Verify we have a new tab
        all_windows = driver.window_handles
        print(f"Window handles after open: {len(all_windows)} windows")
        
        if not new_tab and len(all_windows) > original_window_count:
            for window in all_windows:
                if window != original_window:
                    new_tab = window
                    driver.switch_to.window(new_tab)
                    print(f"Found new tab: {window}")
                    break
        
        if new_tab:
            
            # Wait for Domain-Wide Delegation page to load
            print("Waiting for Domain-Wide Delegation page to load...")
            time.sleep(10)
            
            # Click "Add new" button
            print("Looking for 'Add new' button...")
            add_clicked = False
            try:
                add_new_xpath = "/html/body/div[9]/c-wiz[2]/div/div[1]/div/div[2]/div[1]/div/div[2]/div/div[2]/div/div/div[2]/div[1]/div[2]/div/div/div/div[1]/div"
                add_new_btn = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, add_new_xpath))
                )
                add_new_btn.click()
                print("Clicked 'Add new' button via XPath")
                add_clicked = True
                time.sleep(3)
            except Exception as e:
                print(f"XPath failed: {e}")
            
            if not add_clicked:
                try:
                    add_new_btn = driver.find_element(By.XPATH, "//*[contains(text(), 'Add new')]")
                    add_new_btn.click()
                    print("Clicked 'Add new' via text search")
                    add_clicked = True
                    time.sleep(3)
                except:
                    print("Could not find 'Add new' button")
            
            # Wait for popup dialog
            print("Waiting for popup dialog...")
            try:
                popup_xpath = "/html/body/div[9]/div[6]/div/div[2]"
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, popup_xpath))
                )
                print("Popup dialog appeared")
                time.sleep(2)
            except:
                print("Popup not detected via specific XPath, continuing anyway...")
            
            # Enter the Client ID (SA Unique ID)
            print(f"Entering Service Account Client ID: {sa_unique_id}")
            try:
                # User provided XPath: /html/body/div[9]/div[5]/div/div[2]/span/c-wiz/div/span/c-wiz/div[2]/div/div[1]/div/div[1]/input
                client_id_xpath = "/html/body/div[9]/div[5]/div/div[2]/span/c-wiz/div/span/c-wiz/div[2]/div/div[1]/div/div[1]/input"
                client_id_input = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, client_id_xpath))
                )
                client_id_input.clear()
                client_id_input.send_keys(sa_unique_id)
                print("Entered Client ID successfully")
                time.sleep(2)
            except Exception as e:
                print(f"XPath for Client ID failed: {e}, trying alternative...")
                try:
                    # Try to find by placeholder or aria-label
                    client_id_input = driver.find_element(By.XPATH, "//input[contains(@placeholder, 'Client') or contains(@aria-label, 'Client')]")
                    client_id_input.clear()
                    client_id_input.send_keys(sa_unique_id)
                    print("Entered Client ID via alternative method")
                    time.sleep(2)
                except:
                    print("Could not find Client ID input field")
            
            # Enter OAuth Scopes - Google's Admin Console shows "OAuth scopes (comma-delimited)" label
            # After entering each scope and pressing Tab/Enter, a NEW input field appears below for the next scope
            # IMPORTANT: We must stay WITHIN the popup dialog and not click outside
            scopes_list = [
                "https://www.googleapis.com/auth/admin.directory.user.security",
                "https://www.googleapis.com/auth/admin.directory.orgunit",
                "https://www.googleapis.com/auth/admin.directory.domain.readonly",
                "https://www.googleapis.com/auth/admin.directory.domain",
                "https://www.googleapis.com/auth/admin.directory.user",
                "https://www.googleapis.com/auth/siteverification",
                # Gmail scopes
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.insert",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.readonly"
            ]
            
            print(f"Entering {len(scopes_list)} OAuth Scopes...")
            
            # First, find the popup dialog container to constrain our search
            popup_container = None
            popup_selectors = [
                "//div[contains(@role, 'dialog')]",
                "//div[contains(@class, 'modal')]",
                "//div[contains(@class, 'popup')]",
                "//div[contains(@class, 'dialog')]",
                "/html/body/div[9]/div[5]/div/div[2]",
                "/html/body/div[9]/div[6]/div/div[2]",
                "//c-wiz[contains(@class, 'dialog') or .//input]"
            ]
            
            for sel in popup_selectors:
                try:
                    popup_container = driver.find_element(By.XPATH, sel)
                    if popup_container.is_displayed():
                        print(f"Found popup container with selector: {sel[:40]}...")
                        break
                except:
                    continue
            
            # Function to find scope inputs WITHIN the popup only
            def find_scope_inputs_in_popup():
                """Find ALL input fields within the popup that could be scope inputs"""
                inputs = []
                
                if popup_container:
                    # Search only within popup container
                    try:
                        all_inputs = popup_container.find_elements(By.TAG_NAME, "input")
                        inputs = [inp for inp in all_inputs if inp.is_displayed()]
                    except:
                        pass
                
                if not inputs:
                    # Fallback: Find inputs near "OAuth scopes" text
                    try:
                        labels = driver.find_elements(By.XPATH, "//*[contains(text(), 'OAuth scopes') or contains(text(), 'comma-delimited')]")
                        for label in labels:
                            try:
                                # Get all inputs that come after this label
                                following_inputs = label.find_elements(By.XPATH, "following::input")
                                for inp in following_inputs[:5]:  # Only check first 5
                                    if inp.is_displayed() and inp not in inputs:
                                        inputs.append(inp)
                            except:
                                pass
                    except:
                        pass
                
                return inputs
            
            # Get the initial count of visible inputs to track new ones
            initial_inputs = find_scope_inputs_in_popup()
            print(f"Found {len(initial_inputs)} initial inputs in popup")
            
            # Find the scope input - should be the last input in the list (not the first which is Client ID)
            if len(initial_inputs) >= 2:
                # First input is Client ID, second and onwards are scope inputs
                current_scope_input = initial_inputs[-1]  # Get the last one (newest scope field)
            elif len(initial_inputs) == 1:
                # Only one input visible, might need to look differently
                current_scope_input = initial_inputs[0]
            else:
                current_scope_input = None
            
            # Enter each scope
            for i, scope in enumerate(scopes_list):
                print(f"Entering scope {i+1}/{len(scopes_list)}...")
                
                # Re-find inputs to get the latest list (new inputs may have appeared)
                if i > 0:
                    time.sleep(0.5)
                    current_inputs = find_scope_inputs_in_popup()
                    print(f"Now have {len(current_inputs)} inputs in popup")
                    
                    # The new scope input is the LAST one in the list
                    if current_inputs:
                        current_scope_input = current_inputs[-1]
                    else:
                        print("No inputs found, trying to continue with keyboard input...")
                        current_scope_input = None
                
                if current_scope_input:
                    try:
                        # Don't click, just send keys directly to avoid losing focus
                        # First scope: click to focus, subsequent: already focused from Tab
                        if i == 0:
                            current_scope_input.click()
                            time.sleep(0.3)
                        
                        # Type the scope
                        current_scope_input.send_keys(scope)
                        print(f"Typed: {scope}")
                        time.sleep(0.5)
                        
                        # Press TAB (not Enter) to move to the next field without closing popup
                        # This should create the new scope input field
                        current_scope_input.send_keys(Keys.TAB)
                        print(f"Pressed TAB after scope {i+1}")
                        time.sleep(1)
                        
                    except Exception as e:
                        print(f"Error entering scope {i+1}: {e}")
                        # Fallback: use ActionChains to type
                        try:
                            from selenium.webdriver.common.action_chains import ActionChains
                            actions = ActionChains(driver)
                            actions.send_keys(scope)
                            actions.send_keys(Keys.TAB)
                            actions.perform()
                            print(f"Used ActionChains for scope {i+1}")
                            time.sleep(1)
                        except Exception as e2:
                            print(f"ActionChains fallback failed: {e2}")
                else:
                    # No input found, try just sending keys to the active element
                    print(f"No scope input found, attempting keyboard input...")
                    try:
                        from selenium.webdriver.common.action_chains import ActionChains
                        actions = ActionChains(driver)
                        actions.send_keys(scope)
                        actions.send_keys(Keys.TAB)
                        actions.perform()
                        print(f"Used ActionChains for scope {i+1}")
                        time.sleep(1)
                    except Exception as e:
                        print(f"Keyboard fallback failed: {e}")
            
            print(f"Finished entering all {len(scopes_list)} scopes")
            time.sleep(2)
            
            # Click AUTHORIZE button
            print("Clicking AUTHORIZE button...")
            auth_clicked = False
            
            # Multiple selectors for Authorize button
            authorize_selectors = [
                "/html/body/div[9]/div[6]/div/div[2]/span/c-wiz/div/div[2]/div[2]",
                "/html/body/div[9]/div[5]/div/div[2]/span/c-wiz/div/div[2]/div[2]",
                "//button[contains(text(), 'Authorize')]",
                "//button[contains(text(), 'AUTHORIZE')]",
                "//div[text()='AUTHORIZE']",
                "//div[text()='Authorize']",
                "//*[text()='AUTHORIZE']",
                "//*[text()='Authorize']",
                "//button[@type='submit']"
            ]
            
            for selector in authorize_selectors:
                if auth_clicked:
                    break
                try:
                    authorize_btn = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    authorize_btn.click()
                    print(f"Clicked AUTHORIZE via: {selector[:40]}...")
                    auth_clicked = True
                    time.sleep(3)
                except:
                    continue
            
            # Fallback: search all buttons by text content
            if not auth_clicked:
                try:
                    buttons = driver.find_elements(By.TAG_NAME, "button")
                    for btn in buttons:
                        btn_text = btn.text.upper()
                        if "AUTHORIZE" in btn_text or "SAVE" in btn_text:
                            btn.click()
                            print(f"Clicked button with text: {btn.text}")
                            auth_clicked = True
                            time.sleep(3)
                            break
                except Exception as e:
                    print(f"Button search failed: {e}")
            
            # Fallback: search all divs with button-like behavior
            if not auth_clicked:
                try:
                    divs = driver.find_elements(By.TAG_NAME, "div")
                    for div in divs:
                        div_text = div.text.strip().upper()
                        if div_text in ["AUTHORIZE", "SAVE"]:
                            div.click()
                            print(f"Clicked div with text: {div.text}")
                            auth_clicked = True
                            time.sleep(3)
                            break
                except:
                    pass
            
            if not auth_clicked:
                print("Could not find AUTHORIZE button - DWD may need manual completion")
            
            # Click CONFIRM button (if a confirmation dialog appears)
            print("Checking for CONFIRM button...")
            time.sleep(2)
            confirm_clicked = False
            
            confirm_selectors = [
                "/html/body/div[9]/div[7]/div/div[2]/span/div/div[2]/div[2]",
                "/html/body/div[9]/div[6]/div/div[2]/span/div/div[2]/div[2]",
                "//button[contains(text(), 'Confirm')]",
                "//button[contains(text(), 'CONFIRM')]",
                "//div[text()='CONFIRM']",
                "//div[text()='Confirm']",
                "//*[text()='CONFIRM']",
                "//*[text()='Confirm']"
            ]
            
            for selector in confirm_selectors:
                if confirm_clicked:
                    break
                try:
                    confirm_btn = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    confirm_btn.click()
                    print(f"Clicked CONFIRM via: {selector[:40]}...")
                    confirm_clicked = True
                    time.sleep(3)
                except:
                    continue
            
            # Fallback: search elements by text
            if not confirm_clicked:
                try:
                    elements = driver.find_elements(By.XPATH, "//*[text()='CONFIRM' or text()='Confirm' or text()='OK' or text()='ok']")
                    for elem in elements:
                        try:
                            elem.click()
                            print(f"Clicked element with text: {elem.text}")
                            confirm_clicked = True
                            time.sleep(3)
                            break
                        except:
                            continue
                except:
                    pass
            
            if not confirm_clicked:
                print("No CONFIRM button found (may not be needed)")
            
            print("Domain-Wide Delegation configuration complete!")
            
            # Close the Admin Console TAB and switch back to Cloud Shell
            print("Closing Admin Console tab and returning to Cloud Shell...")
            driver.close()
            time.sleep(1)
        else:
            print("ERROR: Could not create new tab for Domain-Wide Delegation!")
            print("SKIPPING Domain-Wide Delegation setup - you may need to configure this manually.")
            print(f"  Service Account: {sa_email}")
            print(f"  Unique ID: {sa_unique_id}")
            print("  Required Scopes:")
            print("    - https://www.googleapis.com/auth/admin.directory.domain")
            print("    - https://www.googleapis.com/auth/admin.directory.user")
            print("    - https://www.googleapis.com/auth/siteverification")
            print("    - https://www.googleapis.com/auth/gmail.send")
        
        # Switch back to the original Cloud Shell window
        driver.switch_to.window(original_window)
        print("Switched back to Cloud Shell window")
        time.sleep(2)
        
        # Switch back to Cloud Shell iframe to continue terminal commands
        try:
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "iframe.cloudshell-frame"))
            )
            driver.switch_to.frame(iframe)
            print("Switched back to Cloud Shell terminal iframe")
            time.sleep(2)
        except Exception as e:
            print(f"Could not switch back to Cloud Shell iframe: {e}")
        
        # ============================================================
        # STEP 5: Wait for Policy Propagation (single check)
        # ============================================================
        print("\n" + "="*80)
        print("STEP 5: CHECKING ORGANIZATION POLICY PROPAGATION...")
        print("="*80 + "\n")
        
        print("Checking if policy has propagated...")
        send_terminal_command(driver, f"gcloud iam service-accounts keys list --iam-account={sa_name}@{project_id}.iam.gserviceaccount.com 2>&1")
        time.sleep(3)
        
        print("\n Policy propagation check complete\n")
        
        # ============================================================
        # STEP 6: Enable APIs and Create Key
        # ============================================================
        print("\n" + "="*80)
        print("STEP 6: Enabling APIs and Creating Service Account Key")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"gcloud services enable admin.googleapis.com --project {project_id}")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud services enable siteverification.googleapis.com --project {project_id}")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud services enable gmail.googleapis.com --project {project_id}")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud iam service-accounts keys create {key_path} --project {project_id} --iam-account {sa_name}@{project_id}.iam.gserviceaccount.com")
        time.sleep(10)
        
        # ============================================================
        # STEP 7: Download Key
        # ============================================================
        print("\n" + "="*80)
        print("STEP 7: Downloading Service Account Key")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"cloudshell download {key_path}")
        time.sleep(3)
        
        # Switch out of iframe to click download button
        driver.switch_to.default_content()
        time.sleep(2)
        
        # Click download button
        clicked = False
        
        # Strategy 1: User-provided XPaths - detect container first, then click button
        try:
            container_xpath = "/html/body/div[3]/div[2]/div/mat-dialog-container"
            container = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, container_xpath))
            )
            print("Found download dialog container")
            
            # Try the specific button XPath
            button_xpath = "/html/body/div[3]/div[2]/div/mat-dialog-container/div/div/dialog-overlay/div[5]/modal-action[1]/button"
            download_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath))
            )
            download_btn.click()
            print("Clicked Download button via specific XPath!")
            clicked = True
        except Exception as e:
            print(f"Strategy 1 failed: {e}")
        
        # Strategy 2: Find Download text within the container
        if not clicked:
            try:
                container_xpath = "/html/body/div[3]/div[2]/div/mat-dialog-container"
                container = driver.find_element(By.XPATH, container_xpath)
                download_btn = container.find_element(By.XPATH, ".//button[contains(., 'Download')]")
                download_btn.click()
                print("Clicked Download button via text match in container!")
                clicked = True
            except Exception as e:
                print(f"Strategy 2 failed: {e}")
        
        # Strategy 3: Fallback - any mat-dialog-container with Download button
        if not clicked:
            try:
                download_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//mat-dialog-container//button[contains(., 'Download')]"))
                )
                download_btn.click()
                print("Clicked Download button via fallback!")
                clicked = True
            except:
                print("Could not click download button with any strategy")
        
        time.sleep(10)
        
        # ============================================================
        # STEP 8: Upload to S3
        # ============================================================
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        # File is named by email (e.g., admin@example.com.json)
        downloaded_file = os.path.join(downloads_dir, f"{email}.json")
        
        print(f"\nLooking for downloaded file: {downloaded_file}")
        
        # Wait a bit for download to complete
        for i in range(5):
            if os.path.exists(downloaded_file):
                break
            print(f"Waiting for download... ({i+1}/5)")
            time.sleep(2)
        
        if os.path.exists(downloaded_file):
            print(f"\n✓ Found downloaded key: {downloaded_file}")
            
            if aws_session and s3_bucket:
                print(f"Uploading to S3 bucket: {s3_bucket}...")
                
                s3 = aws_session.client("s3")
                s3_key = f"workspace-keys/{email}.json"
                s3.upload_file(downloaded_file, s3_bucket, s3_key)
                
                print(f"SUCCESS! Key uploaded to s3://{s3_bucket}/{s3_key}")
                return f"Success! Key uploaded to s3://{s3_bucket}/{s3_key}"
            else:
                print("AWS session or S3 bucket not provided - skipping S3 upload")
                return f"Success! Key downloaded to {downloaded_file} (S3 upload skipped)"
        else:
            print(f"✗ File not found in {downloads_dir}")
            # List files in downloads for debugging
            if os.path.exists(downloads_dir):
                files = os.listdir(downloads_dir)
                json_files = [f for f in files if f.endswith('.json')]
                print(f"JSON files in Downloads: {json_files[:10]}")
            return "Failed to find downloaded key file"

    except Exception as e:
        print(f"Error: {e}")
        return str(e)
    finally:
        if driver:
            print("\nClosing browser...")
            try:
                driver.quit()
            except OSError:
                pass
            except Exception as e:
                print(f"Error closing driver: {e}")

if __name__ == "__main__":
    print("This script is intended to be run from aws.py, but you can run it standalone if you set up AWS credentials.")
