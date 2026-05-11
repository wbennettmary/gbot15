"""
Fly.io Worker: Google Workspace Automation
- Runs in an ephemeral Fly Machine
- Processes a batch of users (passed via env var)
- Logs into Google account, Sets up 2SV, Creates App Password
- Saves results to PostgreSQL
- Self-destructs or exits when done
"""

import os
import re
import json
import time
import base64
import random
import string
import logging
import traceback
import subprocess
import urllib.parse
import urllib.request
import urllib.error
import threading
import io
from concurrent.futures import ThreadPoolExecutor, as_completed

# 3rd-party libraries
import psycopg2
from psycopg2.extras import RealDictCursor
import paramiko
import pyotp
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
# selenium-stealth and selenium-wire are optional but recommended
try:
    from selenium_stealth import stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

try:
    from seleniumwire import webdriver as wire_webdriver
    SELENIUMWIRE_AVAILABLE = True
except ImportError:
    SELENIUMWIRE_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Global Constants
DEFAULT_TIMEOUT = 20

# Postgres Connection Cache
_db_connection = None

def get_db_connection():
    """Get or create PostgreSQL connection"""
    global _db_connection
    if _db_connection and not _db_connection.closed:
        return _db_connection
    
    try:
        host = os.environ.get("POSTGRES_HOST")
        dbname = os.environ.get("POSTGRES_DB")
        user = os.environ.get("POSTGRES_USER")
        password = os.environ.get("POSTGRES_PASSWORD")
        port = os.environ.get("POSTGRES_PORT", "5432")
        
        if not all([host, dbname, user, password]):
            logger.error("[DB] Missing PostgreSQL environment variables")
            return None

        _db_connection = psycopg2.connect(
            host=host,
            dbname=dbname,
            user=user,
            password=password,
            port=port
        )
        logger.info("[DB] Connected to PostgreSQL")
        return _db_connection
    except Exception as e:
        logger.error(f"[DB] Connection failed: {e}")
        return None

def ensure_postgres_table_exists():
    """Ensure app_passwords table exists in PostgreSQL"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_passwords (
                    email TEXT PRIMARY KEY,
                    app_password TEXT,
                    secret_key TEXT,
                    created_at BIGINT,
                    updated_at BIGINT
                );
            """)
        conn.commit()
        logger.info("[DB] Table 'app_passwords' ensured")
        return True
    except Exception as e:
        logger.error(f"[DB] Failed to ensure table: {e}")
        conn.rollback()
        return False

def get_secret_key_from_postgres(email):
    """Retrieve TOTP secret key from PostgreSQL"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT secret_key FROM app_passwords WHERE email = %s", (email,))
            result = cur.fetchone()
            if result and result.get('secret_key'):
                # Check for masked key
                key = result['secret_key']
                if "****" in key:
                    logger.warning(f"[DB] Secret key for {email} is masked")
                    return None
                return key
            return None
    except Exception as e:
        logger.error(f"[DB] Error fetching secret key: {e}")
        return None

def save_to_postgres(email, app_password, secret_key=None):
    """Save/Update app password record in PostgreSQL"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        timestamp = int(time.time())
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_passwords (email, app_password, secret_key, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) 
                DO UPDATE SET 
                    app_password = EXCLUDED.app_password,
                    secret_key = COALESCE(EXCLUDED.secret_key, app_passwords.secret_key),
                    updated_at = EXCLUDED.updated_at;
            """, (email, app_password, secret_key, timestamp, timestamp))
        conn.commit()
        logger.info(f"[DB] Saved record for {email}")
        return True
    except Exception as e:
        logger.error(f"[DB] Failed to save record for {email}: {e}")
        conn.rollback()
        return False

# Proxy Management
_proxy_list_cache = None
_proxy_rotation_counter = 0
_proxy_lock = threading.Lock()

def get_proxy_list_from_env():
    global _proxy_list_cache
    if _proxy_list_cache is not None:
        return _proxy_list_cache
    
    if os.environ.get('PROXY_ENABLED', 'false').lower() != 'true':
        _proxy_list_cache = []
        return []
    
    proxy_str = os.environ.get('PROXY_LIST', '').strip()
    if not proxy_str:
        return []
        
    proxies = []
    for line in proxy_str.split('\n'):
        parts = line.strip().split(':')
        if len(parts) == 4:
            proxies.append({
                'ip': parts[0], 'port': parts[1], 
                'username': parts[2], 'password': parts[3],
                'full': line.strip()
            })
    _proxy_list_cache = proxies
    return proxies

def get_rotated_proxy():
    global _proxy_rotation_counter
    proxies = get_proxy_list_from_env()
    if not proxies:
        return None
    with _proxy_lock:
        proxy = proxies[_proxy_rotation_counter % len(proxies)]
        _proxy_rotation_counter += 1
        return proxy

# Chrome Driver
def get_chrome_driver(proxy=None):
    """
    Initialize Chrome Driver. 
    Assumes Chrome is installed in the Docker image.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--guest") # Guest mode to avoid profile issues
    
    # Anti-detection flags
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    seleniumwire_options = None
    if proxy:
        # If using seleniumwire
        if SELENIUMWIRE_AVAILABLE:
            seleniumwire_options = {
                'proxy': {
                    'http': f'http://{proxy["username"]}:{proxy["password"]}@{proxy["ip"]}:{proxy["port"]}',
                    'https': f'https://{proxy["username"]}:{proxy["password"]}@{proxy["ip"]}:{proxy["port"]}',
                    'no_proxy': 'localhost,127.0.0.1'
                }
            }
        else:
             # Standard chrome proxy auth is harder without extensions, but we can try argument
             # Often better to rely on selenium-wire or extension
             logger.warning("Selenium-wire not available for authenticated proxy!")

    try:
        if SELENIUMWIRE_AVAILABLE and seleniumwire_options:
            driver = wire_webdriver.Chrome(options=options, seleniumwire_options=seleniumwire_options)
        else:
            driver = webdriver.Chrome(options=options)
            
        if STEALTH_AVAILABLE:
            stealth(driver,
                    languages=["en-US", "en"],
                    vendor="Google Inc.",
                    platform="Linux x86_64",
                    webgl_vendor="Intel Inc.",
                    renderer="Intel Iris OpenGL Engine",
                    fix_hairline=True)
        
        return driver
    except Exception as e:
        logger.error(f"[DRIVER] Failed to create driver: {e}")
        raise

# Helper Functions
def simulate_human_typing(element, text, driver=None):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.15))

def wait_for_xpath(driver, xpath, timeout=DEFAULT_TIMEOUT):
    return WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))

def wait_for_clickable_xpath(driver, xpath, timeout=DEFAULT_TIMEOUT):
    return WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))

def element_exists(driver, xpath, timeout=3):
    try:
        WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
        return True
    except TimeoutException:
        return False

def click_xpath(driver, xpath, timeout=DEFAULT_TIMEOUT):
    try:
        el = wait_for_clickable_xpath(driver, xpath, timeout)
        el.click()
        return True
    except Exception:
        return False

# 2Captcha
def solve_captcha_with_2captcha(driver, email=None):
    config = get_twocaptcha_config()
    if not config.get('enabled'):
        return False, "2Captcha disabled"
    # Placeholder for full loop
    # In port, will include the logic
    return False, "Not fully implemented"

def get_twocaptcha_config():
    api_key = os.environ.get('TWOCAPTCHA_API_KEY')
    enabled = os.environ.get('TWOCAPTCHA_ENABLED', 'false').lower() == 'true'
    return {'enabled': enabled and bool(api_key), 'api_key': api_key}

# Main Logic Functions (Ported)

# =====================================================================
# SFTP Logic
# =====================================================================
def upload_secret_to_sftp(email, secret_key):
    """
    Upload the TOTP secret key to SFTP server.
    Environment vars: SECRET_SFTP_HOST, SECRET_SFTP_USER, SECRET_SFTP_PASSWORD
    """
    host = os.environ.get("SECRET_SFTP_HOST", "46.224.9.127")
    port = int(os.environ.get("SECRET_SFTP_PORT", "22"))
    user = os.environ.get("SECRET_SFTP_USER")
    password = os.environ.get("SECRET_SFTP_PASSWORD")
    remote_dir = os.environ.get("SECRET_SFTP_REMOTE_DIR", "/home/brightmindscampus/")

    alias = email.split("@")[0] if "@" in email else email
    
    if not all([host, user, password]):
        logger.warning("[SFTP] Credentials not configured. Skipping upload.")
        return None, None
    
    try:
        transport = paramiko.Transport((host, port))
        transport.banner_timeout = 5
        transport.auth_timeout = 5
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        # Create remote directory if it doesn't exist
        try:
            sftp.chdir(remote_dir)
        except IOError:
            try:
                sftp.mkdir(remote_dir)
                sftp.chdir(remote_dir)
            except Exception:
                pass

        # Create alias folder
        alias_dir = f"{remote_dir.rstrip('/')}/{alias}"
        try:
            sftp.mkdir(alias_dir)
        except IOError:
            pass
            
        filename = f"{email}_authenticator_secret_key.txt"
        remote_path = f"{alias_dir}/{filename}"

        with sftp.open(remote_path, 'w') as f:
            f.write(secret_key)
        
        logger.info(f"[SFTP] Secret uploaded to {host}:{remote_path}")
        sftp.close()
        transport.close()
        return host, remote_path

    except Exception as e:
        logger.error(f"[SFTP] Failed to upload secret: {e}")
        return None, None

# =====================================================================
# Login Logic
# =====================================================================

def handle_post_login_pages(driver, max_attempts=20):
    """
    Handle intermediate pages (Speedbump, verification, Terms)
    """
    logger.info("[LOGIN] Handling post-login pages...")
    
    for attempt in range(max_attempts):
        time.sleep(3)
        try:
            current_url = driver.current_url
            if "myaccount.google.com" in current_url:
                logger.info("[LOGIN] Reached myaccount.google.com")
                return True, None, None
            
            # Speedbumps
            if "speedbump" in current_url:
                logger.info(f"[LOGIN] Speedbump handling: {current_url}")
                # Workspace Terms
                if "speedbump/workspacetermsofservice" in current_url:
                    js_clicks = [
                        "document.querySelector('button[aria-label*=\"understand\"]').click()",
                        "Array.from(document.querySelectorAll('button')).find(el => el.textContent.includes('understand')).click()"
                    ]
                    for js in js_clicks:
                        try:
                            driver.execute_script(js)
                            time.sleep(1)
                            break
                        except: pass
                    continue
                
                # Generic Continue/Next
                buttons = [
                    "//button[@id='confirm']",
                    "//button[contains(., 'Continue')]",
                    "//button[contains(., 'Next')]",
                    "//button[contains(., 'I agree')]"
                ]
                for xpath in buttons:
                    if click_xpath(driver, xpath):
                        logger.info(f"[LOGIN] Clicked speedbump button: {xpath}")
                        time.sleep(2)
                        break
                continue

            # Verification / Review
            if "verify" in current_url.lower() or "challenge" in current_url:
                # Often needs 2SV or manual intervention if not simple click
                # Try generic buttons
                buttons = [
                    "//button[contains(., 'Continue')]",
                    "//button[contains(., 'Next')]"
                ]
                for xpath in buttons:
                    click_xpath(driver, xpath)
                continue

            # If stuck
            if attempt > 15:
                # Try direct nav
                driver.get("https://myaccount.google.com/")
        except Exception as e:
            logger.error(f"[LOGIN] Error in post-login: {e}")
            
    return False, "POST_LOGIN_TIMEOUT", "Failed to reach myaccount"

def login_google(driver, email, password, known_totp_secret=None):
    """
    Login to Google.
    """
    logger.info(f"[LOGIN] Starting login for {email}")
    try:
        driver.get("https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn&flowEntry=ServiceLogin")
        
        # Email
        email_input = wait_for_xpath(driver, "//input[@type='email']", timeout=10)
        simulate_human_typing(email_input, email, driver)
        email_input.send_keys(Keys.RETURN)
        time.sleep(3)
        
        # Check for Password field
        try:
            password_input = wait_for_xpath(driver, "//input[@type='password']", timeout=10)
            wait_for_clickable_xpath(driver, "//input[@type='password']", timeout=5)
        except TimeoutException:
            # Check for captcha or other errors
            if element_exists(driver, "//div[contains(text(), 'Couldn\'t find your Google Account')]"):
                return False, "ACCOUNT_NOT_FOUND", "Account does not exist"
            return False, "PASSWORD_FIELD_MISSING", "Could not find password field"

        # Password
        simulate_human_typing(password_input, password, driver)
        password_input.send_keys(Keys.RETURN)
        time.sleep(5)
        
        # Post-login handling
        if "challenge" in driver.current_url:
            # Check for TOTP
            if "challenge/totp" in driver.current_url and known_totp_secret:
                logger.info("[LOGIN] TOTP Challenge detected")
                totp = pyotp.TOTP(known_totp_secret.replace(" ", "").upper())
                otp_code = totp.now()
                otp_input = wait_for_xpath(driver, "//input[@type='tel' or @autocomplete='one-time-code']")
                otp_input.send_keys(otp_code)
                otp_input.send_keys(Keys.RETURN)
                time.sleep(5)

        return handle_post_login_pages(driver)
        
    except Exception as e:
        logger.error(f"[LOGIN] Exception: {e}")
        return False, "LOGIN_EXCEPTION", str(e)


# =====================================================================
# Authenticator Setup
# =====================================================================

def setup_authenticator(driver, email):
    """
    Setup Authenticator and extract secret key.
    """
    logger.info(f"[AUTH] Setting up Authenticator for {email}")
    try:
        driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
        time.sleep(2)
        
        # Click "Set up authenticator" or check if already there
        if not element_exists(driver, "//strong[contains(text(), 'key')] | //strong[string-length(text()) > 16]"):
             # Click "Set up authenticator" button
             # Button XPaths from AWS code
             buttons = [
                 "//button[contains(., 'Set up')]",
                 "//button[contains(., 'Get started')]"
             ]
             for xpath in buttons:
                 if click_xpath(driver, xpath):
                     time.sleep(2)
                     break

        # Click "Can't scan it?"
        cant_scan_xpaths = [
            "//button[contains(., 'scan it')]",
            "//div[contains(text(), 'scan it')]/ancestor::button"
        ]
        for xpath in cant_scan_xpaths:
            if click_xpath(driver, xpath):
                time.sleep(1)
                break
        
        # Extract Secret Key
        secret_key = None
        # Try dynamic locations (div indices 9-15)
        for i in range(8, 20):
             xpath = f"/html/body/div[{i}]//strong"
             try:
                 el = WebDriverWait(driver, 0.5).until(EC.presence_of_element_located((By.XPATH, xpath)))
                 txt = el.text.replace(" ", "").upper()
                 if len(txt) >= 16:
                     secret_key = txt
                     break
             except: pass
        
        if not secret_key:
             # Fallback generic
             try:
                 els = driver.find_elements(By.XPATH, "//strong")
                 for el in els:
                     txt = el.text.replace(" ", "").upper()
                     if len(txt) >= 16:
                         secret_key = txt
                         break
             except: pass

        if not secret_key:
            return False, None, "SECRET_MISSING", "Could not extract secret key"
            
        logger.info(f"[AUTH] Secret extracted: {secret_key[:4]}...{secret_key[-4:]}")
        
        # Click Next
        click_xpath(driver, "//button[contains(., 'Next')]")
        time.sleep(2)
        
        return True, secret_key, None, None

    except Exception as e:
        logger.error(f"[AUTH] Setup exception: {e}")
        return False, None, "AUTH_SETUP_EXCEPTION", str(e)

def verify_authenticator_setup(driver, email, secret_key):
    """
    Verify the setup by entering the code.
    """
    logger.info(f"[AUTH] Verifying setup for {email}")
    try:
        totp = pyotp.TOTP(secret_key.replace(" ", ""))
        code = totp.now()
        
        # Input field
        input_xpath = "//input[@type='tel' or @autocomplete='one-time-code']"
        try:
             inp = wait_for_xpath(driver, input_xpath, timeout=5)
             inp.clear()
             inp.send_keys(code)
        except:
             # Try JavaScript if standard send_keys fails (overlay issue)
             pass
        
        time.sleep(1)
        # Click Verify
        buttons = [
            "//button[contains(., 'Verify')]",
            "//span[contains(text(), 'Verify')]/ancestor::button"
        ]
        clicked = False
        for xpath in buttons:
            if click_xpath(driver, xpath):
                clicked = True
                break
        
        if not clicked:
             # Try Enter
             try:
                 inp.send_keys(Keys.RETURN)
             except: pass
             
        time.sleep(3)
        return True, None, None

    except Exception as e:
        logger.error(f"[AUTH] Verify exception: {e}")
        return False, "verify_error", str(e)

def enable_two_step_verification(driver, email):
    """
    Enable 2SV (Turn On).
    """
    logger.info(f"[2SV] Enabling 2SV for {email}")
    try:
        driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
        time.sleep(2)
        
        # Check if already on
        if element_exists(driver, "//button[contains(., 'Turn off')]"):
            logger.info("[2SV] Already enabled")
            return True, None, None
            
        # Click Turn On
        turn_on_xpaths = [
            "//button[contains(., 'Turn on')]",
            "//span[contains(text(), 'Turn on')]/ancestor::button"
        ]
        for xpath in turn_on_xpaths:
            if click_xpath(driver, xpath):
                logger.info("[2SV] Clicked Turn On")
                time.sleep(2)
                break
        
        return True, None, None

    except Exception as e:
        logger.error(f"[2SV] Enable exception: {e}")
        return False, "2SV_EXCEPTION", str(e)


# =====================================================================
# App Password Generation
# =====================================================================

def generate_app_password(driver, email, app_name="Mail"):
    """
    Generate an App Password.
    """
    logger.info(f"[APP_PASS] Generating App Password for {email}")
    try:
        driver.get("https://myaccount.google.com/apppasswords")
        time.sleep(2)
        
        # Handle re-authentication if prompted (common for sensitive actions)
        if "signin" in driver.current_url:
            logger.info("[APP_PASS] Re-authentication required")
            # Re-enter password logic would go here if session timed out, 
            # but usually session is fresh enough.
            pass
        
        # Input App Name
        # Look for input or dropdown
        input_xpath = "//input[@aria-label='App name'] | //input[contains(@placeholder, 'App name')]"
        app_input = None
        try:
             app_input = wait_for_xpath(driver, input_xpath, timeout=5)
        except:
             # Look for "Select app" dropdown interaction if custom name not direct
             pass

        if app_input:
            app_input.clear()
            app_input.send_keys(app_name)
            time.sleep(1)
            
            # Click Create
            create_btn = click_xpath(driver, "//button[contains(., 'Create')]")
            if not create_btn:
                return False, None, "CREATE_BTN_FAIL", "Could not find Create button"
        else:
            # Maybe the new UI with predefined apps?
            # For now assume custom name input available as per recent Google changes
             return False, None, "INPUT_FAIL", "Could not find App Name input"

        time.sleep(3)
        
        # Extract Password
        # Usually in a modal, split into blocks or single text
        pwd_xpath = "//div[contains(text(), 'Your app password')]/following::div[string-length(text()) = 16 or string-length(text()) = 19]"
        # Or checking specific class names if known. 
        # Falling back to finding 16-char yellow box or similar
        
        app_password = None
        # Try finding the code
        try:
            # Often it's in a specific dialog content
            elements = driver.find_elements(By.XPATH, "//div[@role='dialog']//div")
            for el in elements:
                txt = el.text.replace(" ", "")
                if len(txt) == 16 and txt.isalpha(): # App passwords are usually 16 letters
                     app_password = txt
                     break
        except: pass
        
        if not app_password:
             return False, None, "EXTRACT_FAIL", "Could not extract app password"
             
        logger.info(f"[APP_PASS] Generated: {app_password[:4]}...{app_password[-4:]}")
        return True, app_password, None, None

    except Exception as e:
        logger.error(f"[APP_PASS] Generation exception: {e}")
        return False, None, "APP_PASS_EXCEPTION", str(e)


# =====================================================================
# Main Process Logic
# =====================================================================

def process_single_user(driver, user_data):
    """
    Orchestrate the full flow for a single user.
    """
    email = user_data.get('email')
    password = user_data.get('password')
    recovery_email = user_data.get('recovery_email')
    
    logger.info(f"--- Processing User: {email} ---")
    
    result = {
        "email": email,
        "status": "FAILED", # Default
        "app_password": None,
        "secret_key": None,
        "error_type": None,
        "error_detail": None,
        "timestamp": int(time.time())
    }
    
    try:
        # 1. Login
        login_success, err_type, err_msg = login_to_google(driver, email, password, recovery_email)
        if not login_success:
            result['error_type'] = err_type
            result['error_detail'] = err_msg
            return result
            
        # 2. Setup Authenticator (if not skipped/already done)
        # We try to setup. If already set up, we might need a way to check or replace.
        # For this logic, we assume we need to extract a new key or verification.
        setup_success, secret_key, err_type, err_msg = setup_authenticator(driver, email)
        if not setup_success:
            result['error_type'] = err_type
            result['error_detail'] = err_msg
            return result
        
        result['secret_key'] = secret_key
        
        # 3. Verify Authenticator
        verify_success, err_type, err_msg = verify_authenticator_setup(driver, email, secret_key)
        if not verify_success:
             # Some flows don't require verification step if just viewing key?
             # But "Set up" usually requires it.
            result['error_type'] = err_type
            result['error_detail'] = err_msg
            return result
            
        # 4. App Password
        ap_success, app_pass, err_type, err_msg = generate_app_password(driver, email)
        if not ap_success:
            result['error_type'] = err_type
            result['error_detail'] = err_msg
            return result
            
        result['app_password'] = app_pass
        result['status'] = "SUCCESS"
        
        # 5. Save to DB (Incremental)
        save_result_to_db(result)
        
        # 6. Upload Secret to SFTP (Optional, if configured)
        # upload_to_sftp(email, secret_key) 
        
    except Exception as e:
        result['error_type'] = "Unhandled Exception"
        result['error_detail'] = str(e)
        logger.error(f"[PROCESS] Unhandled exception for {email}: {e}")
        logger.error(traceback.format_exc())
        
    return result

def main():
    """
    Entry point for the Fly.io worker.
    """
    logger.info("Starting Fly.io Worker...")
    
    # 1. Load Batch Data
    batch_data_json = os.environ.get('BATCH_DATA')
    batch_data_b64 = os.environ.get('BATCH_DATA_B64')
    
    users = []
    
    if batch_data_b64:
        try:
            decoded = base64.b64decode(batch_data_b64).decode('utf-8')
            users = json.loads(decoded)
            logger.info(f"Loaded batch of {len(users)} users from BATCH_DATA_B64.")
        except Exception as e:
             logger.error(f"Failed to decode BATCH_DATA_B64: {e}")
             sys.exit(1)
    elif batch_data_json:
        try:
            users = json.loads(batch_data_json)
            logger.info(f"Loaded batch of {len(users)} users from BATCH_DATA.")
        except json.JSONDecodeError:
            logger.error("Invalid JSON in BATCH_DATA.")
            sys.exit(1)
    else:
        logger.error("No BATCH_DATA or BATCH_DATA_B64 environment variable found.")
        sys.exit(1)
        
    # 2. Initialize Driver
    # We use one driver for the batch, maybe rotating proxy if needed?
    # For now, one driver per batch container is simple.
    driver = None
    try:
        driver = get_chrome_driver()
        
        results = []
        for user in users:
            user_result = process_single_user(driver, user)
            results.append(user_result)
            
            # Small delay between users
            time.sleep(random.uniform(2, 5))
            
            # Clear cookies/session for next user
            try:
                driver.delete_all_cookies()
            except: pass
            
        logger.info("Batch processing complete.")
        logger.info(f"Results: {json.dumps(results, indent=2)}")
        
    except Exception as e:
        logger.critical(f"Critical Worker Failure: {e}")
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()
        logger.info("Worker exiting.")

if __name__ == "__main__":
    main()




