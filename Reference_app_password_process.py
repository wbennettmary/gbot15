import os
import sys
import json
import time
import collections
import random
import logging
import threading
import re
import paramiko
import glob
import fnmatch
import stat
import shutil
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from fake_useragent import UserAgent
import pyotp

from PyQt5.QtCore import QMetaObject, Q_ARG, Qt, pyqtSlot
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QTextEdit, QPushButton,
                             QVBoxLayout, QHBoxLayout, QScrollArea, QSizePolicy,
                             QProgressBar, QSpinBox, QLineEdit, QCheckBox)
from PyQt5.QtGui import QFont, QColor, QPalette
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
import atexit

# ====== DISABLE ALL SLEEPS (MAKE SCRIPT MAX SPEED) ======
_original_sleep = time.sleep

def no_sleep(seconds=0):
    return  # disable all sleep() calls

time.sleep = no_sleep
# ========================================================


def remove_duplicate_app_passwords(file_path):
    """Removes duplicate lines (same email) from the app_passwords.txt file."""
    import os
    try:
        if not os.path.exists(file_path):
            print(f"⚠️ File not found: {file_path}")
            return

        unique_lines = {}
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if ':' in line:
                    email, password = line.strip().split(':', 1)
                    unique_lines[email] = password  # keeps last one for each email

        temp_file = file_path + '.tmp'
        with open(temp_file, 'w', encoding='utf-8') as f:
            for email, password in unique_lines.items():
                f.write(f"{email}:{password}\n")

        os.replace(temp_file, file_path)
        print(f"✅ Duplicates removed successfully from {file_path}")
    except Exception as e:
        print(f"❌ Error removing duplicates: {e}")



# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Global headless mode flag (default False)
HEADLESS_MODE = False

# Global stop flag for halting all processes
STOP_ALL_PROCESSES = False

# Constants and global variables
PROFILE_FOLDER = os.path.join(os.getcwd(), "profiles")
if not os.path.exists(PROFILE_FOLDER):
    os.makedirs(PROFILE_FOLDER)

PROGRESS_FILE = os.path.join(PROFILE_FOLDER, 'progress.json')

LOGIN_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'login_failures.txt')
AUTHENTICATOR_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'authenticator_failures.txt')
TWO_STEP_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'two_step_failures.txt')
APP_PASSWORD_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'app_password_failures.txt')
REJECTED_ACCOUNT_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'rejected_accounts.txt')
FAILED_OTP_FILE = os.path.join(PROFILE_FOLDER, 'failed_otp_verifications.txt')

# Enhanced error files for credentials only
CREDENTIAL_ERROR_FILE = os.path.join(PROFILE_FOLDER, 'credential_errors.txt')
STEP_FAILURE_FILE = os.path.join(PROFILE_FOLDER, 'step_failures.txt')
RETRY_ACCOUNTS_FILE = os.path.join(PROFILE_FOLDER, 'retry_accounts.txt')

# Server credentials (Replace these placeholders with your actual server information)
SFTP_HOST = '46.101.170.250'
SFTP_PORT = 22
SFTP_USERNAME = 'root'
SFTP_PASSWORD = 'L*tX--34GmtnSML'
REMOTE_DIR = '/home/Api_Appas/'

active_accounts_lock = threading.Lock()
active_accounts = set()

# Define a global lock for driver initialization
driver_init_lock = threading.Lock()

def sftp_connect():
    """Establish an SFTP connection to the server."""
    try:
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USERNAME, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        logging.info("SFTP connection established.")
        return sftp
    except Exception as e:
        logging.error(f"Error establishing SFTP connection: {e}")
        raise

def delete_empty_folders_on_server():
    """Send command to delete empty folders on the server within the specified remote directory."""
    try:
        # Set up SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USERNAME, password=SFTP_PASSWORD)

        # Execute the delete command for empty directories using nohup to run in the background
        ssh.exec_command(f"nohup find {REMOTE_DIR} -type d -empty -delete > /dev/null 2>&1 &")
        logging.info("Empty folder deletion command sent to the server in background.")
    except Exception as e:
        logging.error(f"Error deleting empty folders on server: {e}")
    finally:
        ssh.close()

def upload_profiles_to_server():
    """
    Upload profile directories and their contents to the server, excluding app_passwords.txt.
    App passwords are handled separately by the save_app_password function.
    """
    try:
        sftp = sftp_connect()

        local_password_file = os.path.join(PROFILE_FOLDER, 'app_passwords.txt')
        remote_password_file = os.path.join(REMOTE_DIR, 'app_passwords.txt').replace("\\", "/")

        # No need to handle app_passwords.txt here since it's managed by save_app_password
        # Proceed with uploading other profile directories and files

        for root, dirs, files in os.walk(PROFILE_FOLDER):
            relative_path = os.path.relpath(root, PROFILE_FOLDER)
            remote_directory = os.path.join(REMOTE_DIR, relative_path).replace("\\", "/")

            # Ensure remote directory exists
            ensure_remote_dir(sftp, remote_directory)

            for file in files:
                if file == 'app_passwords.txt':
                    continue  # Skip the app_passwords.txt file

                local_file_path = os.path.join(root, file)
                remote_file_path = os.path.join(remote_directory, file).replace("\\", "/")

                try:
                    sftp.stat(remote_file_path)
                    logging.info(f"{file} already exists at {remote_file_path}. Skipping upload.")
                except FileNotFoundError:
                    sftp.put(local_file_path, remote_file_path)
                    logging.info(f"Uploaded {file} to {remote_file_path}")
    except Exception as e:
        logging.error(f"Error during upload_profiles_to_server: {e}")
    finally:
        sftp.close()
        logging.info("SFTP connection closed after uploading profiles.")

def download_profiles_from_server(download_path):
    """Download all profiles from the server to the specified location."""
    try:
        sftp = sftp_connect()

        def download_recursive(remote_dir, local_dir):
            """Recursively download files and directories from the server."""
            os.makedirs(local_dir, exist_ok=True)  # Ensure local directory exists

            for item in sftp.listdir_attr(remote_dir):
                remote_path = os.path.join(remote_dir, item.filename)
                local_path = os.path.join(local_dir, item.filename)

                try:
                    if stat.S_ISDIR(item.st_mode):
                        # Recur if it's a directory
                        download_recursive(remote_path, local_path)
                    else:
                        # Download the file
                        sftp.get(remote_path, local_path)
                except FileNotFoundError:
                    logging.warning(f"File not found: {remote_path}. Skipping...")
                except Exception as e:
                    logging.error(f"Error downloading {remote_path}: {e}")

        # Start downloading recursively from the main directory
        download_recursive(REMOTE_DIR, download_path)

    finally:
        sftp.close()

def inject_randomized_javascript(driver):
    # Modify navigator properties to make detection harder
    script = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

    // Randomize the timing of JavaScript actions
    setTimeout(function() {
        console.log("Randomized action");
    }, Math.floor(Math.random() * 3000) + 500);

    // Random mouse movements
    document.addEventListener('mousemove', function(e) {
        let randomX = Math.random() * window.innerWidth;
        let randomY = Math.random() * window.innerHeight;
        window.scrollBy(randomX, randomY);
    });
    """
    driver.execute_script(script)

def log_credential_error(email, password, error_type, details=""):
    """Log credential errors with account details only (no full logs)."""
    try:
        with open(CREDENTIAL_ERROR_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{email}:{password} - {error_type} - {details}\n")
        logging.info(f"Logged credential error for {email}: {error_type}")
    except Exception as e:
        logging.error(f"Error writing to credential error file: {e}")

def log_step_failure(email, password, step, error_details=""):
    """Log step failures with account details for retry."""
    try:
        with open(STEP_FAILURE_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{email}:{password} - Step: {step} - {error_details}\n")
        logging.info(f"Logged step failure for {email}: {step}")
    except Exception as e:
        logging.error(f"Error writing to step failure file: {e}")

def save_for_retry(email, password, reason=""):
    """Save account for retry attempt."""
    try:
        with open(RETRY_ACCOUNTS_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{email}:{password} - {reason}\n")
        logging.info(f"Saved {email} for retry: {reason}")
    except Exception as e:
        logging.error(f"Error writing to retry file: {e}")

def find_element_with_fallback(driver, xpath_variations, timeout=10, element_description="element"):
    """
    Try multiple XPath variations to find an element with enhanced flexibility.
    Uses shorter timeouts per XPath for faster detection.
    Returns the element if found, None otherwise.
    """
    # Use shorter timeout per XPath for faster detection
    per_xpath_timeout = min(2, timeout // len(xpath_variations)) if len(xpath_variations) > 1 else timeout
    per_xpath_timeout = max(1, per_xpath_timeout)  # Minimum 1 second
    
    for i, xpath in enumerate(xpath_variations):
        try:
            element = WebDriverWait(driver, per_xpath_timeout).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
            logging.info(f"{element_description} found using XPath variation {i+1}: {xpath}")
            return element
        except TimeoutException:
            logging.debug(f"{element_description} not found using XPath {i+1}: {xpath}")
            continue
    
    logging.error(f"Failed to locate {element_description} with any of {len(xpath_variations)} XPath variations")
    return None

def click_element_with_fallback(driver, xpath_variations, timeout=10, element_description="element"):
    """
    Try multiple XPath variations to click an element with enhanced flexibility.
    Uses shorter timeouts per XPath for faster detection.
    Returns True if successful, False otherwise.
    """
    # Use shorter timeout per XPath for faster detection
    per_xpath_timeout = min(2, timeout // len(xpath_variations)) if len(xpath_variations) > 1 else timeout
    per_xpath_timeout = max(1, per_xpath_timeout)  # Minimum 1 second
    
    for i, xpath in enumerate(xpath_variations):
        try:
            element = WebDriverWait(driver, per_xpath_timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            driver.execute_script("arguments[0].scrollIntoView(true);", element)
            time.sleep(0.5)  # Small delay for scroll
            driver.execute_script("arguments[0].click();", element)
            logging.info(f"{element_description} clicked using XPath variation {i+1}: {xpath}")
            return True
        except (TimeoutException, Exception) as e:
            logging.debug(f"{element_description} not clickable using XPath {i+1}: {xpath} - {e}")
            continue
    
    logging.error(f"Failed to click {element_description} with any of {len(xpath_variations)} XPath variations")
    return False

def verify_step_completion(driver, verification_selectors, step_name, timeout=10):
    """
    Verify that a step has been completed successfully by checking for specific elements.
    Returns True if verification passes, False otherwise.
    """
    for selector_type, selector_value in verification_selectors:
        try:
            if selector_type == "url_contains":
                WebDriverWait(driver, timeout).until(
                    EC.url_contains(selector_value)
                )
                logging.info(f"Step '{step_name}' verified: URL contains '{selector_value}'")
                return True
            elif selector_type == "element_present":
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, selector_value))
                )
                logging.info(f"Step '{step_name}' verified: Element present '{selector_value}'")
                return True
            elif selector_type == "element_not_present":
                # Element should NOT be present (inverse verification)
                try:
                    WebDriverWait(driver, 3).until(
                        EC.presence_of_element_located((By.XPATH, selector_value))
                    )
                    # If we reach here, element was found, so step not completed
                    continue
                except TimeoutException:
                    # Element not found, which means step is completed
                    logging.info(f"Step '{step_name}' verified: Element not present '{selector_value}'")
                    return True
        except TimeoutException:
            continue
    
    logging.warning(f"Step '{step_name}' verification failed")
    return False

def process_account_comprehensive(driver, email, password, profile_path, max_step_retries=3, local_only=False):
    """
    Comprehensive account processing with step verification and retry logic.
    Passes the local_only flag to downstream functions.
    """
    start_time = time.time()
    alias = extract_alias_from_email(email)
    step_completed = "login"
    logging.info(f"🚀 Starting comprehensive processing for {email}")
    
    try:
        # Step 1: Verify login (unchanged)
        current_url = driver.current_url
        if "accounts.google.com/speedbump/idvreenable" in current_url:
            log_step_failure(email, password, "login", "ID verification required")
            return False, "login", "id_verification", "Manual ID verification required"
        
        login_verification = [("url_contains", "myaccount.google.com"), ("element_not_present", "//input[@id='identifierId']")]
        if not verify_step_completion(driver, login_verification, "login"):
            log_step_failure(email, password, "login", "Login verification failed")
            return False, "login", "login_failed", "Login verification failed"
        
        if not navigate_to_correct_sequence(driver):
            log_step_failure(email, password, "navigation", "Navigation sequence failed")
            return False, "navigation", "navigation_failed", "Navigation sequence failed"
        
        # Step 2: Setup Authenticator, passing the local_only flag
        step_completed = "authenticator_setup"
        for retry in range(max_step_retries):
            try:
                if is_authenticator_set_up(driver):
                    logging.info(f"✅ Authenticator already set up for {email}")
                    break
                else:
                    if setup_authenticator(driver, profile_path, email, local_only=local_only):
                        logging.info(f"✅ Authenticator setup completed for {email}")
                        break
                    elif retry == max_step_retries - 1:
                        log_step_failure(email, password, "authenticator_setup", f"Failed after {max_step_retries} attempts")
                        return False, "authenticator_setup", "setup_failed", "Authenticator setup failed"
                time.sleep(0.5)
            except Exception as e:
                if retry == max_step_retries - 1:
                    log_step_failure(email, password, "authenticator_setup", f"Exception: {str(e)}")
                    return False, "authenticator_setup", "exception", str(e)
                time.sleep(0.5)
        
        # Step 3: Setup 2-Step Verification (unchanged)
        step_completed = "2step_verification"
        for retry in range(max_step_retries):
            try:
                if is_two_step_verification_enabled(driver):
                    logging.info(f"✅ 2-Step Verification already enabled for {email}")
                    break
                else:
                    if enable_two_step_verification(driver, profile_path, email):
                        logging.info(f"✅ 2-Step Verification setup completed for {email}")
                        break
                    elif retry == max_step_retries - 1:
                        log_step_failure(email, password, "2step_verification", f"Failed after {max_step_retries} attempts")
                        return False, "2step_verification", "setup_failed", "2-Step verification setup failed"
                time.sleep(0.5)
            except Exception as e:
                if retry == max_step_retries - 1:
                    log_step_failure(email, password, "2step_verification", f"Exception: {str(e)}")
                    return False, "2step_verification", "exception", str(e)
                time.sleep(0.5)

        # Step 4: Generate App Password, passing the local_only flag
        step_completed = "app_password"
        for retry in range(max_step_retries):
            try:
                app_password = generate_app_password(driver, email, profile_path, local_only=local_only)
                if app_password:
                    logging.info(f"✅ App Password generated for {email}")
                    try:
                        driver.quit()
                        logging.info(f"🚪 Browser window closed for {email} after successful app password save")
                    except Exception as e:
                        logging.warning(f"⚠️ Error closing browser for {email}: {e}")
                    break
                elif retry == max_step_retries - 1:
                    log_step_failure(email, password, "app_password", f"Generation failed after {max_step_retries} attempts")
                    return False, "app_password", "generation_failed", "App password generation failed"
                time.sleep(0.5)
            except Exception as e:
                if retry == max_step_retries - 1:
                    log_step_failure(email, password, "app_password", f"Exception: {str(e)}")
                    return False, "app_password", "exception", str(e)
                time.sleep(0.5)
        
        elapsed_time = time.time() - start_time
        logging.info(f"🎉 All steps completed successfully for {email} in {elapsed_time:.2f} seconds")
        return True, "completed", "success", "All steps completed"
        
    except Exception as e:
        logging.error(f"Unexpected error in comprehensive processing for {email}: {e}")
        log_step_failure(email, password, step_completed, f"Unexpected error: {str(e)}")
        return False, step_completed, "unexpected_error", str(e)


def quick_page_check(driver, expected_elements, page_name, timeout=3):
    """
    Quick check to see if a page contains any of the expected elements.
    Returns True if any element is found, False otherwise.
    Uses very short timeouts for fast detection.
    """
    for element in expected_elements:
        try:
            driver.find_element(By.XPATH, element)
            logging.info(f"Quick check: {page_name} element found immediately")
            return True
        except NoSuchElementException:
            continue
    
    # If immediate check fails, try a short wait
    try:
        for element in expected_elements:
            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, element))
                )
                logging.info(f"Quick check: {page_name} element found after wait")
                return True
            except TimeoutException:
                continue
    except Exception:
        pass
    
    logging.debug(f"Quick check: No {page_name} elements found")
    return False

def load_retry_accounts():
    """Load accounts marked for retry."""
    retry_accounts = []
    if os.path.exists(RETRY_ACCOUNTS_FILE):
        try:
            with open(RETRY_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line:
                        parts = line.split(' - ', 1)
                        if len(parts) >= 1:
                            email_pass = parts[0]
                            if ':' in email_pass:
                                email, password = email_pass.split(':', 1)
                                retry_accounts.append((email.strip(), password.strip()))
        except Exception as e:
            logging.error(f"Error loading retry accounts: {e}")
    return retry_accounts

def clear_retry_accounts():
    """Clear the retry accounts file after processing."""
    try:
        if os.path.exists(RETRY_ACCOUNTS_FILE):
            os.remove(RETRY_ACCOUNTS_FILE)
            logging.info("Retry accounts file cleared.")
    except Exception as e:
        logging.error(f"Error clearing retry accounts file: {e}")

def clear_error_files():
    """Clear all error files to start fresh."""
    error_files = [CREDENTIAL_ERROR_FILE, STEP_FAILURE_FILE, RETRY_ACCOUNTS_FILE]
    for file_path in error_files:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logging.info(f"Cleared error file: {file_path}")
        except Exception as e:
            logging.error(f"Error clearing {file_path}: {e}")

def navigate_to_correct_sequence(driver):
    """
    Ensure correct navigation sequence: Authenticator -> 2-Step -> App Password
    Avoids enrollment pages that can cause issues.
    """
    logging.info("🧭 Ensuring correct navigation sequence...")
    
    try:
        # Step 1: Go to authenticator setup page first
        logging.info("📍 Step 1: Navigating to authenticator setup...")
        driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
        time.sleep(1)
        
        # Step 2: Check if we need to go through 2-step verification
        logging.info("📍 Step 2: Checking 2-step verification...")
        driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
        time.sleep(1)
        
        # Check if we're on an enrollment page and skip it
        current_url = driver.current_url
        if "enroll" in current_url.lower():
            logging.info("🔄 Detected enrollment page, skipping...")
            driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
            time.sleep(1)
        
        # Step 3: Finally navigate to app passwords (this will be done after setup)
        logging.info("✅ Navigation sequence prepared")
        return True
        
    except Exception as e:
        logging.error(f"❌ Navigation sequence failed: {e}")
        return False

def debug_app_password_save():
    """Debug function to check app password saving capabilities."""
    logging.info("🔍 Testing app password save functionality...")
    
    # Test local file operations
    test_email = "test@example.com"
    test_password = "abcd-efgh-ijkl-mnop"
    
    try:
        # Test folder creation
        os.makedirs(PROFILE_FOLDER, exist_ok=True)
        logging.info(f"✅ Profile folder accessible: {PROFILE_FOLDER}")
        
        # Test file write
        test_file = os.path.join(PROFILE_FOLDER, 'test_write.txt')
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(f"{test_email}: {test_password}\n")
        logging.info(f"✅ File write test successful")
        
        # Test file read
        with open(test_file, 'r', encoding='utf-8') as f:
            content = f.read()
            if test_email in content:
                logging.info(f"✅ File read test successful")
            else:
                logging.error(f"❌ File read test failed")
        
        # Clean up
        os.remove(test_file)
        logging.info(f"✅ File cleanup successful")
        
    except Exception as e:
        logging.error(f"❌ Debug test failed: {e}")
        return False
    
    return True

def generate_dynamic_app_password_xpaths():
    """Generate dynamic XPaths for app password extraction with variable div indices."""
    xpaths = []
    
    # PRIORITY XPaths - Based on the actual HTML structure you provided
    priority_xpaths = [
        # New structure based on your HTML: strong class="v2CTKd KaSAf" with div containing spans
        "//strong[@class='v2CTKd KaSAf']//div[@dir='ltr']",
        "//strong[@class='v2CTKd KaSAf']//div",
        "//strong[@class='v2CTKd KaSAf']",
        "//div[@class='lY6Rwe riHXqb']//strong",
        "//h2[@class='XfTrZ']//strong",
        "//header[@class='VuF2Pd lY6Rwe']//strong",
        "//article//strong[@class='v2CTKd KaSAf']",
        "//div[@class='VfPpkd-WsjYwc VfPpkd-WsjYwc-OWXEXe-INsAgc KC1dQ Usd1Ac AaN0Dd  F2KCCe NkyfNe yOXhRb hG48Q th4kpc CAOh2c']//strong",
        
        # Backup XPaths for the specific div structure you mentioned
        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong/div",
        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong",
        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div",
        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/span"
    ]
    xpaths.extend(priority_xpaths)
    
    # Dynamic div indices from 14 to 22 for comprehensive coverage
    for div_num in range(14, 23):
        # Your specific required XPaths with dynamic div indices
        xpaths.extend([
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong/div",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/span",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/div",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div",
            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article",
            f"/html/body/div[{div_num}]//strong[contains(text(), '-')]",
            f"/html/body/div[{div_num}]//span[contains(text(), '-')]",
            f"/html/body/div[{div_num}]//div[contains(text(), '-')]"
        ])
    
    # Add generic patterns that work across different UI versions
    xpaths.extend([
        "//strong[contains(text(), '-') and string-length(text()) >= 16]",
        "//div[contains(text(), '-') and string-length(text()) >= 16]",
        "//span[contains(text(), '-') and string-length(text()) >= 16]",
        "//h2//strong[contains(text(), '-')]",
        "//h2//div[contains(text(), '-')]",
        "//h2//span[contains(text(), '-')]",
        "//article//strong[contains(text(), '-')]",
        "//article//div[contains(text(), '-')]",
        "//article//span[contains(text(), '-')]",
        "//div[@role='dialog']//strong[contains(text(), '-')]",
        "//div[@role='dialog']//div[contains(text(), '-')]",
        "//div[@role='dialog']//span[contains(text(), '-')]",
        "//div[@aria-modal='true']//strong[contains(text(), '-')]",
        "//div[@aria-modal='true']//div[contains(text(), '-')]",
        "//div[@aria-modal='true']//span[contains(text(), '-')]",
        "//div[contains(@class, 'password')]//strong",
        "//div[contains(@class, 'password')]//div",
        "//div[contains(@class, 'password')]//span",
        "//div[contains(@class, 'generated')]//strong",
        "//div[contains(@class, 'generated')]//div",
        "//div[contains(@class, 'generated')]//span"
    ])
    
    return xpaths

def extract_app_password_from_spans(driver):
    """
    Extract app password from the new Google UI structure where password is split across multiple spans.
    Based on the HTML structure: <strong><div dir="ltr"><span>r</span><span>l</span>...</div></strong>
    """
    logging.info("🔍 Attempting to extract password from span elements...")
    
    try:
        # Try to find the container with individual span elements
        span_container_xpaths = [
            "//strong[@class='v2CTKd KaSAf']//div[@dir='ltr']",
            "//strong[@class='v2CTKd KaSAf']//div",
            "//div[@class='lY6Rwe riHXqb']//strong//div",
            "//h2[@class='XfTrZ']//strong//div",
            "//article//strong//div[@dir='ltr']"
        ]
        
        for xpath in span_container_xpaths:
            try:
                container = WebDriverWait(driver, 3).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                
                # Get all span elements within the container
                spans = container.find_elements(By.TAG_NAME, "span")
                if spans:
                    # Extract text from each span and concatenate
                    password_chars = []
                    for span in spans:
                        char = span.text.strip()
                        if char:  # Only add non-empty characters
                            password_chars.append(char)
                    
                    if password_chars:
                        # Join all characters (including spaces between groups)
                        full_password = ''.join(password_chars)
                        logging.debug(f"Raw extracted text: '{full_password}'")
                        
                        # Clean up the password - the HTML shows spaces between character groups
                        # Pattern: "r l k r   j h h b   d r j o   g e c w " becomes "rlkr-jhhb-drjo-gecw"
                        clean_password = full_password.replace(' ', '')
                        
                        # If we have characters but no dashes, try to reconstruct the dashes
                        if len(clean_password) >= 16 and '-' not in clean_password:
                            # Insert dashes every 4 characters for app password format
                            if len(clean_password) == 16:
                                clean_password = f"{clean_password[:4]}-{clean_password[4:8]}-{clean_password[8:12]}-{clean_password[12:16]}"
                                logging.info(f"🔧 Reconstructed app password format: {clean_password}")
                        
                        # Validate the cleaned password
                        if len(clean_password) >= 16:
                            if clean_password.count('-') >= 3 or len(clean_password) == 19:  # 16 chars + 3 dashes = 19
                                logging.info(f"✅ Password extracted from spans: {clean_password}")
                                return clean_password
                            else:
                                logging.debug(f"Extracted text doesn't have proper dash format: {clean_password}")
                        else:
                            logging.debug(f"Extracted text too short: {clean_password} (length: {len(clean_password)})")
                
            except (TimeoutException, NoSuchElementException):
                continue
        
        logging.warning("Could not extract password from span elements")
        return None
        
    except Exception as e:
        logging.error(f"Error extracting password from spans: {e}")
        return None

def wait_for_app_password_dialog(driver, timeout=15):
    """Wait for the app password dialog to appear after clicking Generate."""
    logging.info("⏳ Waiting for app password dialog to appear...")
    
    try:
        # Wait for the modal dialog to appear
        dialog_selectors = [
            "//div[@aria-modal='true']",
            "//div[@role='dialog']",
            "//div[@class='uW2Fw-P5QLlc']",
            "//span[contains(text(), 'Generated app password')]",
            "//h2[contains(., 'Generated app password')]"
        ]
        
        for selector in dialog_selectors:
            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                logging.info(f"✅ App password dialog detected with selector: {selector}")
                return True
            except TimeoutException:
                continue
        
        logging.warning("❌ App password dialog did not appear")
        return False
        
    except Exception as e:
        logging.error(f"Error waiting for dialog: {e}")
        return False

def add_random_delay(max_delays=1):
    """Disabled: No random delays."""
    return
def random_scroll_and_mouse_move(driver):
    """Disabled: No fake interactions."""
    return

def adaptive_wait(driver, condition, timeout=8):
    """Optimized adaptive wait with shorter timeout."""
    try:
        return WebDriverWait(driver, timeout).until(condition)
    except TimeoutException:
        return None
        
def log_failure(file_path, email, reason, details=None):
    """
    Log failure information to the specified file.
    
    Parameters:
        file_path (str): The path to the log file.
        email (str): The email address associated with the failure.
        reason (str): The reason for the failure.
        details (str, optional): Additional details about the failure.
    """
    try:
        with open(file_path, 'a') as f:
            log_message = f"{email}:{reason}"
            if details:
                log_message += f":{details}"
            f.write(log_message + "\n")
        logging.info(f"Logged failure for {email} in {file_path}: {reason}")
    except Exception as e:
        logging.error(f"Error writing to {file_path}: {e}")

def load_progress():
    """Load the progress file if it exists, otherwise return an empty dictionary."""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return {}

def is_valid_email(email):
    """
    Validate the email format using regex.
    
    Parameters:
        email (str): The email address to validate.
    
    Returns:
        bool: True if the email format is valid, False otherwise.
    """
    regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(regex, email) is not None
    
def sanitize_input(text):
    """
    Sanitize input to remove potentially dangerous characters or malformed input.
    For emails, only allow safe characters. For passwords, preserve all characters.
    
    Parameters:
        text (str): The input text to sanitize.
    
    Returns:
        str: The sanitized text.
    """
    # If it looks like an email (contains @), sanitize it
    if '@' in text:
        return re.sub(r'[^a-zA-Z0-9@:._-]', '', text)
    # For passwords, return as-is to preserve special characters
    return text

def extract_alias_from_email(email):
    """
    Extract the alias (part before '@') from the email address.
    
    Parameters:
        email (str): The email address.
    
    Returns:
        str: The alias extracted from the email.
    """
    return email.split('@')[0]

def fetch_secret_key_from_server(alias):
    """
    Fetch the secret key from the server using the alias only.
    Returns the secret key string if successful, None otherwise.
    """
    sftp = sftp_connect()
    if not sftp:
        logging.error("SFTP connection could not be established.")
        return None

    try:
        remote_dir = os.path.join(REMOTE_DIR, alias).replace("\\", "/")
        secret_key_filename_pattern = "*_authenticator_secret_key.txt"

        # List files in the remote directory
        files = sftp.listdir(remote_dir)
        secret_key_file = None
        for filename in files:
            if fnmatch.fnmatch(filename, secret_key_filename_pattern):
                secret_key_file = filename
                break

        if not secret_key_file:
            logging.error(f"Secret key file not found in {remote_dir}")
            return None

        remote_secret_key_file = os.path.join(remote_dir, secret_key_file).replace("\\", "/")
        logging.debug(f"Attempting to read remote file: {remote_secret_key_file}")

        with sftp.open(remote_secret_key_file, 'r') as f:
            secret_key = f.read().strip()
            if isinstance(secret_key, bytes):
                secret_key = secret_key.decode('utf-8')  # Decode bytes to string if necessary
            logging.info(f"Secret key fetched successfully from {remote_secret_key_file}.")
            return secret_key
    except FileNotFoundError:
        logging.error(f"Secret key file not found in {remote_dir}")
        return None
    except Exception as e:
        logging.error(f"Error reading secret key file in {remote_dir}: {e}")
        return None
    finally:
        sftp.close()
        logging.info("SFTP connection closed.")

def generate_pyotp_code(profile_path, email):
    """
    Generate a TOTP code using the secret key for the given account.
    Prioritizes local secret key and fetches from the server if not found.

    Parameters:
        profile_path (str): The local path where the secret key is stored.
        email (str): The email address associated with the account.

    Returns:
        str: The generated OTP code if successful, None otherwise.
    """
    try:
        alias = extract_alias_from_email(email)
        secret_key_filename_pattern = "*_authenticator_secret_key.txt"
        local_secret_key_file = None

        # Search for the secret key file in the alias directory
        secret_key_files = glob.glob(os.path.join(profile_path, secret_key_filename_pattern))
        if secret_key_files:
            local_secret_key_file = secret_key_files[0]  # Take the first match
            logging.info(f"Found local secret key file: {local_secret_key_file}")
        else:
            logging.info(f"No local secret key file found for alias {alias}.")

        # Check if the secret key file exists locally first
        if local_secret_key_file and os.path.exists(local_secret_key_file):
            # Read the secret key from the local file
            with open(local_secret_key_file, 'r') as f:
                secret_key = f.read().strip()
            logging.info(f"Secret key found locally for {alias}.")
        else:
            # If not found locally, fetch the secret key from the server
            secret_key = fetch_secret_key_from_server(alias)
            if secret_key:
                logging.info(f"Secret key fetched from server for {alias}.")

                # Save the fetched secret key locally for future use
                try:
                    # Save the secret key file with the email in the filename
                    secret_key_filename = f"{email}_authenticator_secret_key.txt"
                    local_secret_key_file = os.path.join(profile_path, secret_key_filename)
                    os.makedirs(os.path.dirname(local_secret_key_file), exist_ok=True)  # Ensure local directory exists
                    with open(local_secret_key_file, 'w') as f:
                        f.write(secret_key)
                    logging.info(f"Secret key saved locally at {local_secret_key_file} after fetching from server.")
                except Exception as e:
                    logging.error(f"Failed to save fetched secret key locally for {alias}: {e}")
                    log_failure(FAILED_OTP_FILE, email, f"Local Secret Key Save Error: {e}")
                    return None
            else:
                logging.error(f"Secret key could not be retrieved for {alias}. Cannot proceed with OTP.")
                log_failure(FAILED_OTP_FILE, email, "OTP Verification Failed: Secret key not found")
                return None

        # Ensure secret_key is a string
        if isinstance(secret_key, bytes):
            secret_key = secret_key.decode('utf-8')

        logging.debug(f"Secret key type after decoding: {type(secret_key)}")
        logging.debug(f"Secret key value: {secret_key}")

        # Generate the TOTP code
        secret_key = secret_key.replace(" ", "").upper()
        totp = pyotp.TOTP(secret_key)
        otp_code = totp.now()
        logging.info(f"Generated OTP code for {alias}: {otp_code}")
        return otp_code
    except Exception as e:
        logging.error(f"Error generating OTP code for {alias}: {e}")
        log_failure(FAILED_OTP_FILE, email, f"OTP Generation Error: {e}")
        return None
        
def delete_empty_folders(root_folder):
    """Delete all empty folders in the specified root folder."""
    deleted_folders = []  # List to keep track of deleted folders
    for foldername, subfolders, filenames in os.walk(root_folder, topdown=False):
        if not subfolders and not filenames:  # If the folder is empty
            try:
                os.rmdir(foldername)
                deleted_folders.append(foldername)
                logging.info(f"Deleted empty folder: {foldername}")
            except OSError as e:
                logging.error(f"Error deleting folder {foldername}: {e}")

    return deleted_folders

def ensure_remote_dir(sftp, remote_directory):
    """
    Ensure that the remote directory exists on the server. Create it if it does not exist.
    
    Parameters:
        sftp (paramiko.SFTPClient): The active SFTP connection.
        remote_directory (str): The remote directory path to ensure.
    """
    remote_directory = remote_directory.replace("\\", "/")
    dirs = remote_directory.strip('/').split('/')
    path = ''
    for dir in dirs:
        path += '/' + dir
        try:
            sftp.stat(path)
        except FileNotFoundError:
            try:
                sftp.mkdir(path)
                logging.info(f"Created directory on server: {path}")
            except Exception as e:
                logging.error(f"Failed to create directory {path}: {e}")

def generate_app_password(driver, email, profile_path, max_retries=3, initial_timeout=30, local_only=False):
    """
    Generate an app password for the account with enhanced XPath flexibility and better error handling.
    """
    try:
        logging.info(f"🌐 Navigating to the App Passwords page for {email}...")
        # Use navigation with TOTP check
        if not add_totp_check_to_navigation(driver, profile_path, email, 
                                           "https://myaccount.google.com/apppasswords?hl=en", 
                                           "app_password_generation"):
            logging.error(f"Failed to navigate to app passwords page for {email}")
            return None
        
        try:
            WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") == "complete")
            time.sleep(1)
            logging.info("✅ App passwords page loaded successfully")
        except TimeoutException:
            logging.warning("⚠️ App passwords page load timeout, proceeding anyway...")

        for attempt in range(max_retries):
            try:
                # ... (rest of the function's try block is unchanged)
                app_name_xpath_variations = [
                    "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/div[1]/span[3]/input",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/label/input",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/div[1]/span[3]/input",
                    "//input[@aria-label='App name']", "//input[contains(@placeholder, 'app') or contains(@placeholder, 'name')]",
                    "//input[@type='text' and contains(@class, 'input')]", "//input[@type='text']",
                    "//label[contains(text(), 'App name')]/following::input", "//div[contains(@class, 'app')]//input[@type='text']",
                    "//form//input[@type='text'][1]", "//c-wiz//input[@type='text']"
                ]
                app_name_field = find_element_with_fallback(driver, app_name_xpath_variations, timeout=10, element_description="app name input field")
                if not app_name_field: 
                    logging.warning(f" App name input field not detected on attempt {attempt + 1}, refreshing page...")
                    driver.refresh()
                    time.sleep(0.5)
                    raise TimeoutException("Failed to locate app name input field")
                
                app_name = f"App-{int(time.time())}"
                app_name_field.clear()
                app_name_field.send_keys(app_name)
                time.sleep(0.5)

                generate_button_xpath_variations = [
                    "/html/body/c-wiz[1]/div/div[2]/div[3]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button/span[5]",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button/span[2]",
                    "//button[contains(., 'Generate')]", "//button[contains(@aria-label, 'Generate')]",
                    "//button[@type='button' and contains(text(), 'Generate')]", "//span[contains(text(), 'Generate')]/parent::button",
                    "//div[contains(@class, 'generate')]//button", "//button[contains(@class, 'generate')]",
                    "//form//button[@type='button']", "//c-wiz//button[not(contains(@aria-label, 'Close'))]"
                ]
                if not click_element_with_fallback(driver, generate_button_xpath_variations, timeout=initial_timeout, element_description="Generate button"):
                    raise TimeoutException("Failed to click Generate button")

                if not wait_for_app_password_dialog(driver, timeout=10):
                    logging.error("❌ App password dialog did not appear after clicking Generate")
                    continue
                
                app_password = extract_app_password_from_spans(driver)
                if not app_password:
                    app_password_xpath_variations = generate_dynamic_app_password_xpaths()
                    for i, xpath in enumerate(app_password_xpath_variations):
                        try:
                            app_password_element = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, xpath)))
                            potential_password = app_password_element.text.strip().replace(" ", "")
                            if len(potential_password) >= 16 and '-' in potential_password and potential_password.count('-') >= 3:
                                app_password = potential_password
                                logging.info(f"✅ App password found using XPath #{i+1}")
                                break
                        except: continue

                if not app_password or len(app_password) < 16:
                    raise TimeoutException("Failed to locate valid app password element")

                # Save the app password with MULTIPLE ATTEMPTS and pass the local_only flag
                save_successful = False
                for save_attempt in range(3):
                    if save_app_password(email, app_password, local_only=local_only):
                        logging.info(f"✅ App password for {email} saved successfully on attempt {save_attempt + 1}.")
                        save_successful = True
                        break
                    else:
                        logging.warning(f"❌ Save attempt {save_attempt + 1} failed for {email}.")
                        time.sleep(0.5)
                
                if not save_successful:
                    logging.error(f"🚨 CRITICAL: Failed to save app password for {email} after 3 attempts!")

                return app_password

            except TimeoutException as e:
                logging.warning(f"Attempt {attempt + 1} failed to generate app password for {email}: {e}")
                if attempt < max_retries - 1:
                    driver.refresh()
                    time.sleep(0.5)
                else: raise e
        
        log_failure(APP_PASSWORD_FAILURE_FILE, email, "App Password Generation Failed after retries")
        return None

    except Exception as e:
        logging.error(f"Error while generating app password for {email}: {e}")
        log_failure(APP_PASSWORD_FAILURE_FILE, email, "App Password Generation Error", str(e))
        return None


def load_app_passwords():
    """
    Load app passwords from the local app_passwords.txt file.

    Returns:
        OrderedDict: An ordered dictionary mapping emails to their app passwords.
    """
    from collections import OrderedDict
    password_file = os.path.join(PROFILE_FOLDER, 'app_passwords.txt')
    app_passwords = OrderedDict()  # Use OrderedDict to preserve insertion order

    if os.path.exists(password_file):
        with open(password_file, 'r') as f:
            for line in f:
                if ':' in line:
                    email, password = line.strip().split(':', 1)
                    email = email.strip()
                    password = password.strip()
                    if is_valid_email(email) and password:
                        app_passwords[email] = password
    else:
        logging.info(f"No local app_passwords.txt found at {password_file}.")

    return app_passwords

def save_app_password(email, app_password, local_only=False):
    """
    Save or update the app password. Conditionally saves to the server.
    """
    logging.info(f"💾 Starting save process for {email}")
    
    # Input validation
    if not email or not app_password or not is_valid_email(email):
        logging.error(f"❌ Invalid inputs for save_app_password: email={email}")
        return False
    
    password_file_local = os.path.join(PROFILE_FOLDER, 'app_passwords.txt')
    
    try:
        os.makedirs(PROFILE_FOLDER, exist_ok=True)
    except Exception as e:
        logging.error(f"❌ Failed to create profile folder: {e}")
        return False

        # --- Local save (replace this block) ---
    local_save_success = False
    try:
        logging.info(f"💾 Attempting local save for {email}...")
        # Load existing (keeps order and removes duplicates by email key)
        existing_passwords = load_app_passwords()

        # Normalize password (remove dashes) and set/overwrite entry
        clean_app_password = app_password.replace('-', '')
        existing_passwords[email] = clean_app_password

        # Write atomically to avoid races and duplicates
        temp_file = password_file_local + '.tmp'
        os.makedirs(os.path.dirname(password_file_local), exist_ok=True)
        with open(temp_file, 'w', encoding='utf-8') as f:
            for saved_email, saved_password in existing_passwords.items():
                f.write(f"{saved_email}:{saved_password}\n")
            f.flush()
            os.fsync(f.fileno())
        # Atomic replace
        os.replace(temp_file, password_file_local)

        logging.info(f"✅ Local save successful for {email}")
        local_save_success = True
    except Exception as e:
        logging.error(f"❌ Local save failed for {email}: {e}")
        return False
    # --- end local save ---


    # Conditionally attempt server upload
    if local_save_success and not local_only:
        logging.info(f"🌐 Attempting server upload for {email}...")
        try:
            sftp = sftp_connect()
            if sftp:
                password_file_server = os.path.join(REMOTE_DIR, 'app_passwords.txt').replace("\\", "/")
                ensure_remote_dir(sftp, os.path.dirname(password_file_server))
                sftp.put(password_file_local, password_file_server)
                logging.info(f"✅ Server upload successful for {email}")
                sftp.close()
            else:
                logging.warning(f"⚠️ No SFTP connection available for server upload")
        except Exception as server_error:
            logging.warning(f"⚠️ Server upload failed (continuing anyway): {server_error}")
    elif local_only:
        logging.info(f"Skipping server save for app password because 'Save Locally Only' is enabled.")
        
    return True


def is_authenticator_set_up(driver):
    """Check if Authenticator is set up or not with optimized fast detection."""
    logging.info("🔍 Quick check: Authenticator setup status...")

    # Check if we're already on the right page to avoid unnecessary navigation
    current_url = driver.current_url
    if "two-step-verification/authenticator" not in current_url:
        driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
    else:
        logging.info("Already on authenticator page, skipping navigation")
    
    # Wait for basic page load
    try:
        WebDriverWait(driver, 5).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        logging.warning("Page load timeout, proceeding with check...")
    
    # Quick check first - immediate element detection
    quick_setup_elements = [
        "//button[contains(., 'Set up')]",
        "//span[contains(text(), 'Set up')]"
    ]
    
    # Use quick check first
    if quick_page_check(driver, quick_setup_elements, "authenticator setup", timeout=2):
        logging.info("✅ Quick check: Authenticator setup is required.")
        return False
    
    # If quick check doesn't find setup buttons, likely already set up
    # Do a final verification with minimal timeout
    setup_button_xpaths = [
        "//button[contains(., 'Set up')]",
        "//span[contains(text(), 'Set up')]/parent::button"
    ]

    # Final check with very short timeout
    setup_button = find_element_with_fallback(driver, setup_button_xpaths, timeout=3, element_description="authenticator setup button")
    
    if setup_button:
        logging.info("Authenticator setup is required.")
        return False  # Authenticator is not set up, button exists
    else:
        logging.info("✅ Authenticator is already set up, skipping setup.")
        return True  # Authenticator is already set up, no button found

def is_two_step_verification_enabled(driver):
    """Check if 2-Step Verification is enabled with proper navigation handling."""
    logging.info("🔍 Checking 2-Step Verification status...")

    # Navigate directly to the main 2-step verification page (not the enrollment page)
    # Note: This function doesn't have access to profile_path and email, so we'll use the old method
    driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
    
    # Wait for page load
    try:
        WebDriverWait(driver, 8).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(0.5)  # Additional wait for dynamic content
    except TimeoutException:
        logging.warning("Page load timeout, proceeding with check...")
    
    # Check current URL to understand what page we're on
    current_url = driver.current_url
    logging.info(f"Current URL after navigation: {current_url}")
    
    # If we're on the enrollment page, skip it by navigating to authenticator first
    if "enroll" in current_url.lower() or "verification" in current_url.lower():
        logging.info("🔄 Detected enrollment page, navigating to authenticator setup first...")
        driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
        time.sleep(1)
        # Then navigate back to check 2-step status
        driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
        time.sleep(1)
    
    # Look for indicators that 2-step verification is already enabled
    enabled_indicators = [
        "//div[contains(text(), 'On')]",
        "//span[contains(text(), 'On')]",
        "//div[contains(text(), 'Enabled')]", 
        "//span[contains(text(), 'Enabled')]",
        "//button[contains(., 'Turn off')]",
        "//div[contains(@class, 'enabled')]"
    ]
    
    # Check if already enabled
    if quick_page_check(driver, enabled_indicators, "2-step verification enabled status", timeout=3):
        logging.info("✅ 2-Step Verification is already enabled.")
        return True
    
    # Look for turn on/setup buttons
    turn_on_button_xpaths = [
        "//button[contains(., 'Turn on')]",
        "//button[contains(., 'GET STARTED')]",
        "//button[contains(., 'Set up')]",
        "//span[contains(text(), 'Turn on')]/parent::button",
        "//span[contains(text(), 'GET STARTED')]/parent::button"
    ]

    turn_on_button = find_element_with_fallback(driver, turn_on_button_xpaths, timeout=5, element_description="2-step verification turn on button")
    
    if turn_on_button:
        logging.info("2-Step Verification is NOT enabled - setup required.")
        return False  # Two-Step Verification is not enabled, button exists
    else:
        logging.info("✅ 2-Step Verification appears to be already enabled.")
        return True  # Two-Step Verification is already enabled, no button found

def login_gmail(driver, email, password, profile_path, max_retries=3):
    """
    Attempt to log in to Gmail with a retry mechanism.
    Modified so that after handling OTP the browser window stays open.
    In headless mode, uses JavaScript to set the OTP input value.
    """
    from selenium.webdriver.common.keys import Keys
    ua = UserAgent()
    user_agent = ua.random

    inject_randomized_javascript(driver)
    alias = extract_alias_from_email(email)

    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Attempt {attempt} to log in for {email}")
            driver.get("https://accounts.google.com/ServiceLogin?hl=en")
            random_scroll_and_mouse_move(driver)

            # Enter email
            email_input = adaptive_wait(driver, EC.element_to_be_clickable((By.ID, "identifierId")))
            if not email_input:
                logging.warning(f"Email input field not found on attempt {attempt}. Refreshing.")
                driver.refresh()
                continue

            email_input.clear()
            email_input.send_keys(email)
            add_random_delay()
            email_input.send_keys(Keys.ENTER)

            # Check if email exists
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), \"Couldn't find your Google Account\")]") )
                )
                logging.error(f"Email doesn't exist: {email}")
                log_failure(LOGIN_FAILURE_FILE, email, "Email does not exist")
                return False
            except TimeoutException:
                pass

            # Enter password
            password_input = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.NAME, "Passwd"))
            )
            password_input.send_keys(password)
            add_random_delay()
            password_input.send_keys(Keys.ENTER)

            # Check if password is wrong
            try:
                WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Wrong password. Try again')]"))
                )
                logging.error(f"Incorrect password for {email}")
                log_failure(LOGIN_FAILURE_FILE, email, "Incorrect password")
                return False
            except TimeoutException:
                pass

            # Check for account rejection (e.g. due to two-step or admin issues)
            if "https://accounts.google.com/v3/signin/rejected" in driver.current_url:
                logging.error(f"Account rejected for {email} due to two-step verification or admin issue.")
                log_failure(REJECTED_ACCOUNT_FAILURE_FILE, email, "Two-step verification/admin issue")
                return False

            handle_speedbump_page(driver, email)
            handle_twosvrequired_page(driver, email)

            # Handle OTP challenge if present during login
            if not check_and_handle_totp_challenge(driver, profile_path, email, "login"):
                log_failure(FAILED_OTP_FILE, email, "OTP Verification Failed")
                logging.error(f"TOTP verification failed for {email}.")
                return False

            # Wait until login is successful and URL reflects that
            WebDriverWait(driver, 30).until(
                lambda d: "myaccount.google.com" in d.current_url or "speedbump" in d.current_url
            )
            current_url = driver.current_url
            if "myaccount.google.com" in current_url:
                logging.info(f"Logged in successfully for {email}")
            elif "speedbump" in current_url:
                logging.info(f"ID verification page detected for {email}, login considered successful.")
            else:
                logging.error(f"Unexpected URL after login for {email}: {current_url}")
                return False

            # Do not quit the driver here so that the window remains open
            return True

        except (TimeoutException, WebDriverException) as e:
            logging.error(f"Attempt {attempt} failed for {email}: {e}")
            if driver:
                driver.save_screenshot(f"error_{email}_attempt{attempt}.png")
            if attempt == max_retries:
                log_failure(LOGIN_FAILURE_FILE, email, f"Failed after {max_retries} attempts")
                return False
            else:
                logging.info(f"Retrying login for {email}...")
                driver.refresh()
                time.sleep(0.5)
                continue

        except Exception as e:
            logging.error(f"Error during login for {email}: {e}")
            if driver:
                driver.save_screenshot(f"error_{email}_exception.png")
            log_failure(LOGIN_FAILURE_FILE, email, f"Error: {e}")
            return False

def handle_speedbump_page(driver, email):
    try:
        WebDriverWait(driver, 5).until(
            EC.url_contains("https://accounts.google.com/speedbump/gaplustos")
        )
        logging.info(f"Speedbump page detected for {email}, attempting to click the button...")
        driver.execute_script("document.querySelector('#confirm').click()")
        logging.info("Handled the speedbump page successfully.")
        add_random_delay()
    except TimeoutException:
        logging.info(f"No speedbump encountered for {email}. Continuing login.")

def handle_twosvrequired_page(driver, email):
    try:
        WebDriverWait(driver, 5).until(
            EC.url_contains("https://myaccount.google.com/interstitials/twosvrequired")
        )
        logging.info(f"Two-step verification required page detected for {email}, skipping and redirecting to setup authenticator page...")
        # Skip clicking anything and redirect directly to the setup authenticator page
        driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
        logging.info("Redirected to setup authenticator page successfully.")
        add_random_delay()
    except TimeoutException:
        logging.info(f"No two-step verification required page encountered for {email}. Continuing login.")

def setup_authenticator(driver, profile_path, email, local_only=False):
    """Setup Google Authenticator for the given account."""
    alias = extract_alias_from_email(email)
    logging.info(f"⚡ Fast setup: Navigating to 2FA setup page for alias {alias}..")
    
    # Use navigation with TOTP check
    if not add_totp_check_to_navigation(driver, profile_path, email, 
                                       "https://myaccount.google.com/two-step-verification/authenticator?hl=en", 
                                       "authenticator_setup"):
        logging.error(f"Failed to navigate to authenticator setup page for {email}")
        return False
    
    random_scroll_and_mouse_move(driver)

    if is_authenticator_set_up(driver):
        logging.info(f"Authenticator already set up for alias {alias}, skipping setup.")
        return True

    try:
        click_setup_authenticator_button(driver)
        click_cant_scan_link(driver)
        add_random_delay(1)  # Reduced delay

        # Pass the local_only flag to the saving function
        secret_key = extract_and_save_secret_key(driver, profile_path, email, local_only=local_only)
        if secret_key:
            logging.info(f"Secret key saved successfully for alias {alias}")
            click_continue_button(driver)

            verified = enter_and_verify_totp_code(driver, profile_path, email)
            if verified:
                logging.info(f"Authenticator setup completed for alias {alias}")
                return True
            else:
                log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "TOTP Verification Failed")
                return False
        else:
            log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "Failed to extract secret key")
            return False

    except TimeoutException as e:
        log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
        logging.error(f"Timeout during authenticator setup for alias {alias}: {e}")
        return False
    except Exception as e:
        log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
        logging.error(f"Error during authenticator setup for alias {alias}: {e}")
        return False


def click_setup_authenticator_button(driver):
    try:
        # Try the original xpath first
        try:
            setup_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((
                    By.XPATH, "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div/div[3]/div[2]/div/div/div/button/span[5]"
                ))
            )
            driver.execute_script("arguments[0].click();", setup_button)
            logging.info("Clicked on 'Set up authenticator' button using original xpath.")
            return
        except TimeoutException:
            logging.info("Original xpath not found, trying fallback xpath...")
        
        # Fallback to the new xpath for updated accounts
        setup_button = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((
                By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div[2]/div/div/div/button"
            ))
        )
        driver.execute_script("arguments[0].click();", setup_button)
        logging.info("Clicked on 'Set up authenticator' button using fallback xpath.")
        
    except TimeoutException as e:
        logging.error(f"Timeout while clicking 'Set up authenticator' button with both xpaths: {e}")
        raise

def click_cant_scan_link(driver):
    """Click the 'Can't scan it?' link with multiple fallback strategies."""
    try:
        # Strategy 1: Try multiple flexible XPath patterns
        xpath_patterns = [
            "//span[contains(text(), 'Can't scan it?')]",
            "//a[contains(text(), 'Can't scan it?')]",
            "//button[contains(text(), 'Can't scan it?')]",
            "//*[contains(text(), 'Can't scan it?')]",
            "//span[contains(text(), 'Can\'t scan it?')]",
            "//a[contains(text(), 'Can\'t scan it?')]",
            "//button[contains(text(), 'Can\'t scan it?')]",
            "//*[contains(text(), 'Can\'t scan it?')]",
            "//span[contains(text(), 'cant scan')]",
            "//a[contains(text(), 'cant scan')]",
            "//button[contains(text(), 'cant scan')]",
            "//*[contains(text(), 'cant scan')]"
        ]
        
        # Strategy 2: Try dynamic div paths (fixed syntax)
        for div_index in range(9, 14):
            xpath_patterns.append(f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/center/div/div/button/span[5]")
            xpath_patterns.append(f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/center/div/div/button/span[4]")
            xpath_patterns.append(f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/center/div/div/button/span[3]")
        
        # Strategy 3: Try common button patterns
        xpath_patterns.extend([
            "//button[contains(@class, 'VfPpkd-LgbsSe')]//span[contains(text(), 'Can')]",
            "//button[contains(@class, 'VfPpkd-LgbsSe')]//span[contains(text(), 'scan')]",
            "//div[contains(@class, 'VfPpkd-LgbsSe')]//span[contains(text(), 'Can')]",
            "//div[contains(@class, 'VfPpkd-LgbsSe')]//span[contains(text(), 'scan')]"
        ])
        
        logging.info("🔍 Searching for 'Can't scan it?' link with multiple strategies...")
        
        for i, xpath in enumerate(xpath_patterns):
            try:
                logging.debug(f"Trying XPath pattern {i+1}: {xpath}")
                cant_scan_link = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                
                # Try clicking with JavaScript first
                try:
                    driver.execute_script("arguments[0].click();", cant_scan_link)
                    logging.info(f"✅ Successfully clicked 'Can't scan it?' link using XPath pattern {i+1} with JavaScript.")
                    add_random_delay()
                    return True
                except Exception as js_error:
                    logging.debug(f"JavaScript click failed, trying regular click: {js_error}")
                    cant_scan_link.click()
                    logging.info(f"✅ Successfully clicked 'Can't scan it?' link using XPath pattern {i+1} with regular click.")
                    add_random_delay()
                    return True
                    
            except TimeoutException:
                logging.debug(f"XPath pattern {i+1} not found, trying next...")
                continue
            except Exception as e:
                logging.debug(f"Error with XPath pattern {i+1}: {e}")
                continue
        
        # Strategy 4: Try finding by partial text match in all clickable elements
        logging.info("🔍 Trying to find link by searching all clickable elements...")
        try:
            all_clickable = driver.find_elements(By.XPATH, "//*[@onclick or @href or self::button or self::a]")
            for element in all_clickable:
                try:
                    text = element.text.lower()
                    if "can't scan" in text or "cant scan" in text or "scan" in text:
                        driver.execute_script("arguments[0].click();", element)
                        logging.info("✅ Found and clicked 'Can't scan it?' link by text search.")
                        add_random_delay()
                        return True
                except:
                    continue
        except Exception as e:
            logging.debug(f"Text search strategy failed: {e}")
        
        raise TimeoutException("Failed to locate 'Can't scan it?' link with all strategies.")
        
    except TimeoutException as e:
        logging.error(f"❌ Timeout while clicking 'Can't scan it?' link: {e}")
        logging.info("💡 The 'Can't scan it?' link might not be available on this page.")
        return False
    except Exception as e:
        logging.error(f"❌ Unexpected error while clicking 'Can't scan it?' link: {e}")
        return False

def extract_and_save_secret_key(driver, profile_path, email, local_only=False):
    """Optimized secret key extraction with faster processing."""
    try:
        # Try multiple div indices to find the secret key element
        secret_key = None
        for div_index in range(9, 14):
            try:
                xpath = f"/html/body/div[{div_index}]/div/div[2]/span/div/div/ol/li[2]/div/strong"
                logging.debug(f"Trying to find secret key with XPath: {xpath}")
                secret_key_element = WebDriverWait(driver, 2).until(  # Reduced timeout
                    EC.visibility_of_element_located((By.XPATH, xpath))
                )
                secret_key = secret_key_element.text.strip()
                logging.info(f"Secret key extracted for {email} using div[{div_index}]: {secret_key}")
                break
            except TimeoutException:
                logging.debug(f"Secret key not found with div[{div_index}], trying next...")
                continue
            except Exception as e:
                logging.debug(f"Error with div[{div_index}]: {e}")
                continue
        
        if not secret_key:
            raise Exception("Secret key not found with any div index from 9 to 13")
            
    except Exception as e:
        logging.error(f"Error extracting secret key for {email}: {e}")
        log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Secret Key Extraction Error", str(e))
        return None


    alias = extract_alias_from_email(email)
    secret_key_filename = f"{email}_authenticator_secret_key.txt"

    # Save the secret key locally (always)
    try:
        local_secret_key_file = os.path.join(profile_path, secret_key_filename)
        os.makedirs(os.path.dirname(local_secret_key_file), exist_ok=True)
        with open(local_secret_key_file, 'w') as f:
            f.write(secret_key)
        logging.info(f"Secret key saved locally at {local_secret_key_file}.")
    except Exception as e:
        logging.error(f"Failed to save secret key locally for {alias}: {e}")
        log_failure(AUTHENTICATOR_FAILURE_FILE, email, f"Local Secret Key Save Error: {e}")
        return None

    # Conditionally save the secret key on the server
    if not local_only:
        remote_dir = os.path.join(REMOTE_DIR, alias).replace("\\", "/")
        remote_secret_key_file = os.path.join(remote_dir, secret_key_filename).replace("\\", "/")
        logging.debug(f"Remote directory: {remote_dir}")
        logging.debug(f"Remote secret key file path: {remote_secret_key_file}")
        try:
            sftp = sftp_connect()
            if not sftp:
                logging.error("Cannot upload secret key without SFTP connection.")
                return None # Return the key, but acknowledge server save failure
            ensure_remote_dir(sftp, remote_dir)

            with sftp.open(remote_secret_key_file, 'w') as f:
                if isinstance(secret_key, bytes):
                    secret_key = secret_key.decode('utf-8')
                f.write(secret_key)
            logging.info(f"Secret key uploaded to server at {remote_secret_key_file}.")
        except Exception as e:
            logging.error(f"Failed to upload secret key for {alias}: {e}")
            log_failure(AUTHENTICATOR_FAILURE_FILE, email, f"Secret Key Upload Error: {e}")
        finally:
            if sftp:
                try:
                    sftp.close()
                    logging.info("SFTP connection closed after uploading secret key.")
                except Exception as e:
                    logging.error(f"Error closing SFTP connection: {e}")
    else:
        logging.info("Skipping server save for secret key because 'Save Locally Only' is enabled.")

    return secret_key


def click_continue_button(driver):
    """
    Clicks the 'Continue' or 'Next' button, handling dynamic div indices for modals.
    """
    try:
        # Dynamically attempt possible div indices
        for div_index in range(9, 14):  # Adjust the range if needed
            try:
                continue_button_xpath = f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[2]/button"
                continue_button = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, continue_button_xpath))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                driver.execute_script("arguments[0].click();", continue_button)
                logging.info(f"Clicked 'Next' button using div[{div_index}].")
                return  # Exit function once button is clicked successfully
            except TimeoutException:
                logging.debug(f"'Next' button not found in div[{div_index}], trying next...")

        # Raise exception if no button is found after trying all possible div indices
        raise TimeoutException("Failed to locate 'Next' button.")

    except TimeoutException as e:
        logging.error(f"Timeout while clicking 'Next' button: {e}")
        raise

def enter_and_verify_totp_code(driver, profile_path, account_name, max_retries=3):
    """
    Enter and verify the TOTP code during authenticator setup with speed optimization and retry logic.

    Parameters:
        driver (WebDriver): The Selenium WebDriver instance.
        profile_path (str): The local path where the secret key is stored.
        account_name (str): The account name associated with the TOTP.
        max_retries (int): Maximum number of retry attempts for wrong codes.

    Returns:
        bool: True if the TOTP verification is successful, False otherwise.
    """
    try:
        for attempt in range(max_retries):
            try:
                logging.info(f"🔄 TOTP attempt {attempt + 1}/{max_retries} for {account_name}")
                
                # Generate fresh TOTP code for each attempt
                totp_code = generate_pyotp_code(profile_path, account_name)
                if not totp_code:
                    logging.error("Failed to generate TOTP code.")
                    return False

                logging.info(f"⚡ Generated fresh OTP code: {totp_code}")

                # Dynamically find the OTP input field with multiple possible XPath variations
                totp_input = None
                for div_index in range(9, 14):
                    otp_xpath_variations = [
                        f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/label/input",
                        f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/div[1]/span[2]/input",
                        "//input[@type='tel']",
                        "//input[contains(@aria-label, 'code') or contains(@aria-label, 'verification')]",
                        "//input[@autocomplete='one-time-code']"
                    ]
                    
                    for xpath in otp_xpath_variations:
                        try:
                            totp_input = WebDriverWait(driver, 1).until(  # Ultra-fast timeout
                                EC.element_to_be_clickable((By.XPATH, xpath))
                            )
                            logging.info(f"⚡ OTP input field detected using XPath: {xpath}")
                            break
                        except TimeoutException:
                            logging.debug(f"OTP input field not found using XPath: {xpath}")
                            continue
                    
                    if totp_input:
                        break

                if not totp_input:
                    logging.error("Failed to locate OTP input field. Verification cannot proceed.")
                    return False

                # Clear and enter the TOTP code quickly
                totp_input.clear()
                totp_input.send_keys(totp_code)
                logging.info(f"⚡ Entered the 6-digit TOTP code: {totp_code}")

                # Dynamically find the Verify button
                verify_button = None
                for div_index in range(9, 14):
                    try:
                        verify_button_xpath = f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[3]/button/span"
                        verify_button = WebDriverWait(driver, 1).until(  # Ultra-fast timeout
                            EC.element_to_be_clickable((By.XPATH, verify_button_xpath))
                        )
                        logging.info(f"⚡ 'Verify' button detected using div[{div_index}].")
                        break
                    except TimeoutException:
                        logging.debug(f"'Verify' button not found using div[{div_index}]. Trying next...")
                        continue

                if not verify_button:
                    logging.error("Failed to locate 'Verify' button. Verification cannot proceed.")
                    return False

                # Click the Verify button
                verify_button.click()
                logging.info("⚡ Clicked the 'Verify' button.")

                # Wait briefly and check for error messages
                time.sleep(0.5)
                
                # Check for "Wrong code. Try again." error message
                error_detected = False
                for div_index in range(9, 14):
                    try:
                        error_xpath = f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/div[2]/p"
                        error_element = WebDriverWait(driver, 2).until(
                            EC.presence_of_element_located((By.XPATH, error_xpath))
                        )
                        error_text = error_element.text.strip()
                        if "Wrong code" in error_text or "Try again" in error_text:
                            logging.warning(f"❌ Wrong code detected: '{error_text}' - Attempt {attempt + 1}")
                            error_detected = True
                            break
                    except TimeoutException:
                        continue

                if not error_detected:
                    # Check if we successfully moved past the TOTP challenge
                    current_url = driver.current_url
                    if "challenge/totp" not in current_url:
                        logging.info("✅ TOTP verification successful - moved past challenge page")
                        return True
                    else:
                        # Still on TOTP page, might be successful but need to wait
                        time.sleep(1)
                        current_url = driver.current_url
                        if "challenge/totp" not in current_url:
                            logging.info("✅ TOTP verification successful - moved past challenge page")
                            return True
                        else:
                            logging.warning("⚠️ Still on TOTP challenge page, might need retry")
                            if attempt < max_retries - 1:
                                continue
                else:
                    # Wrong code detected, try again with fresh code
                    if attempt < max_retries - 1:
                        logging.info(f"🔄 Generating new OTP code for retry attempt {attempt + 2}")
                        time.sleep(1)  # Brief pause before retry
                        continue
                    else:
                        logging.error("❌ All TOTP attempts failed - wrong codes")
                        return False

            except TimeoutException as e:
                logging.warning(f"Timeout on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                else:
                    raise e

        logging.error("❌ All TOTP verification attempts failed")
        return False

    except TimeoutException as e:
        logging.error(f"Timeout while entering TOTP code: {e}")
        return False
    except Exception as e:
        logging.error(f"Error during TOTP verification: {e}")
        return False

def enable_two_step_verification(driver, profile_path, email):
    """Enable Two-Step Verification for the given account."""
    alias = extract_alias_from_email(email)
    logging.info(f"Navigating to 2-Step Verification page for {email}...")
    # Use navigation with TOTP check
    if not add_totp_check_to_navigation(driver, profile_path, email, 
                                       "https://myaccount.google.com/signinoptions/twosv?hl=en", 
                                       "2step_verification_setup"):
        logging.error(f"Failed to navigate to 2-step verification page for {email}")
        return False

    # Check if 2-step verification is already enabled
    if is_two_step_verification_enabled(driver):
        logging.info(f"Skipping Two-Step Verification for {email} as it's already enabled.")
        return True

    try:
        # Try the original xpath first
        try:
            turn_on_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[2]/div[4]/div/button/span[6]'))
            )
            driver.execute_script("arguments[0].click();", turn_on_button)
            logging.info(f"Clicked on 'Turn On 2-Step Verification' using original xpath for {email}")
        except TimeoutException:
            logging.info("Original 2-step verification xpath not found, trying fallback xpath...")
            
            # Fallback to the new xpath for updated accounts
            turn_on_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[2]/div[4]/div/button'))
            )
            driver.execute_script("arguments[0].click();", turn_on_button)
            logging.info(f"Clicked on 'Turn On 2-Step Verification' using fallback xpath for {email}")

        # Handle the final pop-up and click OK
        # Omitted code for handling pop-up, as it's not defined in the original script

        handle_skip_phone_number(driver)
        # handle_continue_anyway(driver)  # Omitted as it's not defined

        logging.info(f"2-Step Verification enabled successfully for {email}")
        return True

    except TimeoutException as e:
        log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
        logging.error(f"Timeout while enabling 2-Step Verification for {email}: {e}")
        return False
    except Exception as e:
        log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
        logging.error(f"Error during 2-Step Verification setup for {email}: {e}")
        return False

def handle_skip_phone_number(driver):
    try:
        skip_link = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, '//button//span[contains(text(), "Skip")]'))
        )
        driver.execute_script("arguments[0].click();", skip_link)
        logging.info("Clicked 'Skip' to bypass phone number setup.")
    except TimeoutException:
        logging.info("No 'Skip' link found for phone number setup.")

def check_and_handle_totp_challenge(driver, profile_path, email, context="unknown"):
    """
    Universal TOTP challenge detection and handling function.
    Can be called from anywhere in the process to handle TOTP challenges.
    
    Parameters:
        driver: WebDriver instance
        profile_path: Path to profile directory
        email: Account email
        context: Context where this is called (for logging)
    
    Returns:
        bool: True if TOTP challenge was handled successfully or no challenge found
              False if TOTP challenge failed
    """
    try:
        current_url = driver.current_url
        logging.info(f"🔍 Checking for TOTP challenge in {context} for {email}")
        logging.info(f"Current URL: {current_url}")
        
        # Check if we're on a TOTP challenge page
        if "challenge/totp" in current_url:
            logging.info(f"🚨 TOTP challenge detected in {context} for {email}")
            return handle_totp_challenge_universal(driver, profile_path, email, context)
        else:
            logging.debug(f"No TOTP challenge found in {context} for {email}")
            return True
            
    except Exception as e:
        logging.error(f"Error checking TOTP challenge in {context} for {email}: {e}")
        return False

def handle_totp_challenge_universal(driver, profile_path, email, context="unknown", max_retries=3):
    """
    Universal TOTP challenge handler with retry logic.
    
    Parameters:
        driver: WebDriver instance
        profile_path: Path to profile directory
        email: Account email
        context: Context where this is called (for logging)
        max_retries: Maximum number of retry attempts
    
    Returns:
        bool: True if TOTP challenge was handled successfully
              False if TOTP challenge failed after all retries
    """
    try:
        logging.info(f"🔄 Handling TOTP challenge in {context} for {email}")
        
        for attempt in range(max_retries):
            try:
                logging.info(f"⚡ TOTP attempt {attempt + 1}/{max_retries} in {context} for {email}")
                
                # Generate fresh TOTP code
                totp_code = generate_pyotp_code(profile_path, email)
                if not totp_code:
                    logging.error(f"Failed to generate TOTP code for {email}")
                    return False

                logging.info(f"⚡ Generated OTP for {context}: {totp_code}")

                # Find OTP input field with multiple strategies
                totp_input = None
                otp_xpath_variations = [
                    "//input[@type='tel']",
                    "//input[contains(@aria-label, 'code') or contains(@aria-label, 'verification')]",
                    "//input[@autocomplete='one-time-code']",
                    "//input[@type='text' and contains(@class, 'input')]",
                    "//input[@type='text']"
                ]
                
                # Also try dynamic div paths
                for div_index in range(9, 14):
                    otp_xpath_variations.extend([
                        f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/label/input",
                        f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/div[1]/span[2]/input"
                    ])
                
                for xpath in otp_xpath_variations:
                    try:
                        totp_input = WebDriverWait(driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH, xpath))
                        )
                        logging.info(f"⚡ OTP input found in {context}: {xpath}")
                        break
                    except TimeoutException:
                        continue

                if not totp_input:
                    logging.error(f"Failed to locate OTP input field in {context}")
                    return False

                # Clear and enter OTP code
                totp_input.clear()
                totp_input.send_keys(totp_code)
                logging.info(f"⚡ Entered OTP in {context}: {totp_code}")

                # Find and click submit/next button
                submit_button = None
                submit_xpath_variations = [
                    "//button[contains(., 'Next')]",
                    "//button[contains(., 'Submit')]",
                    "//button[contains(., 'Verify')]",
                    "//button[@type='submit']",
                    "//button[contains(@aria-label, 'Next')]",
                    "//button[contains(@aria-label, 'Submit')]"
                ]
                
                # Also try dynamic div paths for submit button
                for div_index in range(9, 14):
                    submit_xpath_variations.extend([
                        f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[3]/button/span",
                        f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[2]/button/span"
                    ])
                
                for xpath in submit_xpath_variations:
                    try:
                        submit_button = WebDriverWait(driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH, xpath))
                        )
                        logging.info(f"⚡ Submit button found in {context}: {xpath}")
                        break
                    except TimeoutException:
                        continue

                if not submit_button:
                    logging.error(f"Failed to locate submit button in {context}")
                    return False

                # Click submit button
                submit_button.click()
                logging.info(f"⚡ Clicked submit button in {context}")

                # Wait and check for errors or success
                time.sleep(1)
                
                # Check for error messages
                error_detected = False
                for div_index in range(9, 14):
                    try:
                        error_xpath = f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/div[2]/p"
                        error_element = WebDriverWait(driver, 1).until(
                            EC.presence_of_element_located((By.XPATH, error_xpath))
                        )
                        error_text = error_element.text.strip()
                        if "Wrong code" in error_text or "Try again" in error_text or "Invalid" in error_text:
                            logging.warning(f"❌ Wrong code in {context}: '{error_text}' - Retry {attempt + 1}")
                            error_detected = True
                            break
                    except TimeoutException:
                        continue

                if not error_detected:
                    # Check if we successfully moved past the TOTP challenge
                    time.sleep(1)
                    current_url = driver.current_url
                    if "challenge/totp" not in current_url:
                        logging.info(f"✅ TOTP challenge successful in {context} for {email}")
                        return True
                    else:
                        time.sleep(2)
                        current_url = driver.current_url
                        if "challenge/totp" not in current_url:
                            logging.info(f"✅ TOTP challenge successful in {context} for {email}")
                            return True
                        else:
                            logging.warning(f"⚠️ Still on TOTP page in {context}, might need retry")
                            if attempt < max_retries - 1:
                                continue
                else:
                    # Wrong code detected, try again with fresh code
                    if attempt < max_retries - 1:
                        logging.info(f"🔄 Generating new OTP for retry {attempt + 2} in {context}")
                        time.sleep(0.5)
                        continue
                    else:
                        logging.error(f"❌ All TOTP attempts failed in {context} for {email}")
                        return False

            except TimeoutException as e:
                logging.warning(f"Timeout on attempt {attempt + 1} in {context}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                else:
                    raise e

        logging.error(f"❌ All TOTP verification attempts failed in {context} for {email}")
        return False

    except Exception as e:
        logging.error(f"Error during TOTP challenge handling in {context} for {email}: {e}")
        return False

def add_totp_check_to_navigation(driver, profile_path, email, target_url, context="navigation"):
    """
    Navigate to a URL and handle any TOTP challenges that occur during navigation.
    
    Parameters:
        driver: WebDriver instance
        profile_path: Path to profile directory
        email: Account email
        target_url: URL to navigate to
        context: Context for logging
    
    Returns:
        bool: True if navigation successful and any TOTP challenges handled
              False if navigation failed or TOTP challenge failed
    """
    try:
        logging.info(f"🧭 Navigating to {target_url} in {context} for {email}")
        driver.get(target_url)
        time.sleep(1)
        
        # Check for TOTP challenge after navigation
        if not check_and_handle_totp_challenge(driver, profile_path, email, f"{context}_after_navigation"):
            logging.error(f"TOTP challenge failed during {context} navigation for {email}")
            return False
        
        logging.info(f"✅ Navigation to {target_url} successful in {context} for {email}")
        return True
        
    except Exception as e:
        logging.error(f"Error during navigation in {context} for {email}: {e}")
        return False

def handle_authentication_challenge(driver, profile_path, email):
    """
    Handle the OTP (TOTP) challenge using the saved secret key.
    In headless mode, the OTP is entered via JavaScript to ensure proper value assignment.
    This function does not close the window after submission.
    """
    from selenium.webdriver.common.keys import Keys
    try:
        totp_code = generate_pyotp_code(profile_path, email)
        if not totp_code:
            logging.error(f"Failed to generate TOTP code for {email}.")
            return False

        otp_input = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, '//input[@type="tel"]'))
        )
        logging.info("OTP input field detected.")

        # In headless mode, set the OTP value via JavaScript
        driver.execute_script("arguments[0].value = '';", otp_input)
        driver.execute_script("arguments[0].value = arguments[1];", otp_input, totp_code)
        logging.info(f"Entered OTP code for {email} using JavaScript.")
        add_random_delay()

        # Try to click a 'Next' or 'Submit' button if available
        try:
            submit_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//button[contains(., "Next")]'))
            )
            driver.execute_script("arguments[0].click();", submit_button)
        except TimeoutException:
            # Fallback to sending ENTER key if button not found
            otp_input.send_keys(Keys.ENTER)

        logging.info(f"Submitted OTP code for {email}.")

        # Wait until the URL indicates successful login
        WebDriverWait(driver, 20).until(
            lambda d: "myaccount.google.com" in d.current_url or "speedbump" in d.current_url
        )
        logging.info(f"OTP verification completed successfully for {email}.")
        return True

    except TimeoutException as e:
        logging.error(f"Timeout during OTP verification for {email}: {e}")
        log_failure(FAILED_OTP_FILE, email, "OTP Verification Timeout")
        return False

    except Exception as e:
        logging.error(f"Error during OTP verification for {email}: {e}")
        log_failure(FAILED_OTP_FILE, email, f"OTP Verification Error: {e}")
        return False

class GmailAutomationApp(QWidget):
    def __init__(self):
        super().__init__()
        self.drivers = {}
        self.progress = load_progress()
        self.current_executor = None  # For tracking active ThreadPoolExecutor
        
        # Run debug test for app password saving
        debug_app_password_save()
        
        self.initUI()

    def initUI(self):
        """Initialize the UI for Gmail Automation App with responsive layout."""
        self.setWindowTitle("Gmail Automation App")
        self.setMinimumSize(450, 750)

        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(240, 240, 245))
        palette.setColor(QPalette.WindowText, QColor(28, 28, 30))
        self.setPalette(palette)

        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)

        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(10, 10, 10, 10)
        scroll_layout.setSpacing(8)

        title_label = QLabel("Gmail Automation App")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setFont(QFont("Helvetica Neue", 14, QFont.Bold))
        scroll_layout.addWidget(title_label)

        self.accounts_text = QTextEdit()
        self.accounts_text.setPlaceholderText('email1:password1\nemail2:password2\n...')
        self.accounts_text.setFont(QFont("Helvetica Neue", 10))
        self.accounts_text.setStyleSheet("border-radius: 8px; padding: 10px; background-color: #FFFFFF;")
        self.accounts_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_layout.addWidget(self.accounts_text, stretch=2)

        def create_button(text, color):
            button = QPushButton(text)
            button.setFont(QFont("Helvetica Neue", 9))
            button.setFixedHeight(32)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color};
                    color: white;
                    border-radius: 6px;
                    padding: 4px;
                }}
                QPushButton:hover {{
                    background-color: #4c4c4c;
                }}
                QPushButton:pressed {{
                    background-color: #333;
                }}
            """)
            return button

        self.login_button = create_button("Login to Accounts", "#0078D7")
        self.login_button.clicked.connect(self.start_login_multiple_accounts_thread)
        scroll_layout.addWidget(self.login_button)

        self.authenticator_button = create_button("Setup Authenticator", "#28A745")
        self.authenticator_button.clicked.connect(self.start_setup_authenticator_thread)
        scroll_layout.addWidget(self.authenticator_button)

        self.two_step_button = create_button("Enable Two-Step Verification", "#FFC107")
        self.two_step_button.clicked.connect(self.enable_two_step_verification_for_all_accounts)
        scroll_layout.addWidget(self.two_step_button)

        self.password_button = create_button("Generate App Passwords", "#6C757D")
        self.password_button.clicked.connect(self.start_generate_app_password_thread)
        scroll_layout.addWidget(self.password_button)

        run_all_row_layout = QHBoxLayout()

        self.bulk_execution_button = create_button("Run All Steps in Bulk", "#17A2B8")
        self.bulk_execution_button.clicked.connect(self.start_bulk_execute_all_thread)
        run_all_row_layout.addWidget(self.bulk_execution_button)
        
        self.retry_failed_button = create_button("Retry Failed Accounts", "#FFC107")
        self.retry_failed_button.clicked.connect(self.start_retry_failed_thread)
        run_all_row_layout.addWidget(self.retry_failed_button)
        
        self.clear_errors_button = create_button("Clear Error Files", "#DC3545")
        self.clear_errors_button.clicked.connect(self.clear_error_files_ui)
        run_all_row_layout.addWidget(self.clear_errors_button)

        concurrent_label = QLabel("Concurrent Accounts:")
        concurrent_label.setFont(QFont("Helvetica Neue", 9))
        run_all_row_layout.addWidget(concurrent_label)

        self.concurrent_spinbox = QSpinBox()
        self.concurrent_spinbox.setRange(1, 100)
        self.concurrent_spinbox.setValue(10)
        self.concurrent_spinbox.setToolTip("Set the number of concurrent accounts to run (no limit)")
        run_all_row_layout.addWidget(self.concurrent_spinbox)

        scroll_layout.addLayout(run_all_row_layout)

        self.headless_checkbox = QCheckBox("Headless Mode")
        self.headless_checkbox.setChecked(False)
        scroll_layout.addWidget(self.headless_checkbox)

        self.auto_arrange_checkbox = QCheckBox("Auto Arrange Sessions on Screen")
        self.auto_arrange_checkbox.setChecked(True)
        self.auto_arrange_checkbox.setToolTip("Automatically arrange browser windows on screen when new sessions open")
        scroll_layout.addWidget(self.auto_arrange_checkbox)
        
        # --- THIS IS THE NEW CHECKBOX ---
        self.local_only_checkbox = QCheckBox("Save Credentials Locally Only")
        self.local_only_checkbox.setChecked(False)
        self.local_only_checkbox.setToolTip("If checked, secret keys and app passwords will NOT be saved to the server.")
        scroll_layout.addWidget(self.local_only_checkbox)
        # --------------------------------

        self.upload_profiles_button = create_button("Upload Profiles to Server", "#DA7B93")
        self.upload_profiles_button.clicked.connect(upload_profiles_to_server)
        scroll_layout.addWidget(self.upload_profiles_button)

        self.load_all_button = create_button("Retrieve and Load All App Passwords", "#9A5D9C")
        self.load_all_button.clicked.connect(self.load_and_display_all_app_passwords)
        scroll_layout.addWidget(self.load_all_button)

        self.retrieve_button = create_button("Retrieve App Passwords from Server", "#5C6BC0")
        self.retrieve_button.clicked.connect(self.retrieve_app_passwords_from_server)
        scroll_layout.addWidget(self.retrieve_button)

        domain_row_layout = QHBoxLayout()
        
        domain_label = QLabel("Domain:")
        domain_label.setFont(QFont("Helvetica Neue", 9))
        domain_row_layout.addWidget(domain_label)
        
        self.domain_input = QLineEdit()
        self.domain_input.setPlaceholderText("Enter domain (e.g., example.com)")
        domain_row_layout.addWidget(self.domain_input)
        
        self.retrieve_domain_button = create_button("Retrieve App Passwords for Domain", "#FF5722")
        self.retrieve_domain_button.clicked.connect(self.retrieve_app_passwords_for_domain)
        domain_row_layout.addWidget(self.retrieve_domain_button)
        
        scroll_layout.addLayout(domain_row_layout)
        
        self.delete_folders_button = create_button("Delete Empty Folders", "#E65100")
        self.delete_folders_button.clicked.connect(self.delete_empty_folders_in_profiles)
        scroll_layout.addWidget(self.delete_folders_button)

        self.download_profiles_button = create_button("Download Profiles to Desktop", "#0288D1")
        self.download_profiles_button.clicked.connect(self.download_profiles_to_desktop)
        scroll_layout.addWidget(self.download_profiles_button)

        self.arrange_sessions_button = create_button("Arrange Sessions on Screen", "#FF9800")
        self.arrange_sessions_button.clicked.connect(self.arrange_sessions_based_on_accounts)
        self.arrange_sessions_button.setToolTip("Manually arrange browser sessions on screen based on account count")
        scroll_layout.addWidget(self.arrange_sessions_button)

        self.show_windows_button = create_button("Show All Windows", "#4CAF50")
        self.show_windows_button.clicked.connect(self.show_all_windows)
        self.show_windows_button.setToolTip("Force show all browser windows")
        scroll_layout.addWidget(self.show_windows_button)

        self.stop_button = create_button("STOP ALL PROCESSES", "#DC3545")
        self.stop_button.clicked.connect(self.stop_all_processes)
        self.stop_button.setToolTip("Stop all running processes immediately")
        scroll_layout.addWidget(self.stop_button)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.progress_bar.setStyleSheet("QProgressBar {border-radius: 5px;} QProgressBar::chunk {background-color: #0A84FF;}")
        scroll_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setFont(QFont("Helvetica Neue", 10))
        self.status_label.setAlignment(Qt.AlignCenter)
        scroll_layout.addWidget(self.status_label)

        self.app_password_display = QTextEdit()
        self.app_password_display.setReadOnly(True)
        self.app_password_display.setStyleSheet("""
            background-color: #F9F9F9;
            border: 1px solid #D1D1D6;
            border-radius: 10px;
            padding: 12px;
            font-size: 12px;
            color: #1C1C1E;
        """)
        self.app_password_display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_layout.addWidget(self.app_password_display, stretch=2)

        scroll_area.setWidget(scroll_widget)

        main_layout = QVBoxLayout(self)
        main_layout.addWidget(scroll_area, stretch=1)
        self.setLayout(main_layout)

    def download_profiles_to_desktop(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        download_path = os.path.join(script_dir, "profiles")
        os.makedirs(download_path, exist_ok=True)
        download_profiles_from_server(download_path)
        logging.info(f"Profiles have been downloaded to {download_path}")
        self.update_status(f"Profiles downloaded to {download_path}")

    def parse_accounts_input(self):
        accounts = []
        input_text = self.accounts_text.toPlainText().strip()
        lines = input_text.split("\n")

        for line in lines:
            if ':' in line:
                email, password = line.split(':', 1)
                email = sanitize_input(email.strip())
                password = sanitize_input(password.strip())
                accounts.append((email, password))
        return accounts

    def update_status(self, message, progress=None):
        QMetaObject.invokeMethod(self.status_label, "setText", Qt.QueuedConnection, Q_ARG(str, message))
        if progress is not None:
            QMetaObject.invokeMethod(self.progress_bar, "setValue", Qt.QueuedConnection, Q_ARG(int, progress))

    def get_accounts_from_input(self):
        accounts = []
        input_text = self.accounts_text.toPlainText().strip()
        lines = input_text.split("\n")

        for line in lines:
            if ':' in line:
                email, password = line.split(':', 1)
                email = sanitize_input(email.strip())
                password = password.strip()  # Don't sanitize password - keep it exactly as typed

                if is_valid_email(email) and password:
                    accounts.append((email, password))
                else:
                    self.thread_safe_log_output(f"Invalid email or missing password in line: {line}")
            else:
                self.thread_safe_log_output(f"Invalid format in line: {line}")
        return accounts

    def thread_safe_log_output(self, message):
        logging.info(message)
        QMetaObject.invokeMethod(self.app_password_display, "append", Qt.QueuedConnection, Q_ARG(str, message))

    def delete_empty_folders_in_profiles(self):
        deleted_folders = delete_empty_folders(PROFILE_FOLDER)
        if deleted_folders:
            self.log_output(f"Deleted {len(deleted_folders)} empty folders locally.")
            for folder in deleted_folders:
                self.log_output(f"Deleted locally: {folder}")
        else:
            self.log_output("No empty folders found to delete locally.")

        try:
            delete_empty_folders_on_server()
            self.log_output("Deleted empty folders on the server.")
        except Exception as e:
            self.log_output(f"Error deleting empty folders on server: {e}")

        self.update_status("Empty folder deletion completed locally and on the server.")

    def log_output(self, message):
        logging.info(message)
        QMetaObject.invokeMethod(self.app_password_display, "append", Qt.QueuedConnection, Q_ARG(str, message))
        
    def start_login_multiple_accounts_thread(self):
        """Start login_multiple_accounts in a separate thread to prevent UI freezing."""
        thread = threading.Thread(target=self.login_multiple_accounts, daemon=True)
        thread.start()
    
    def start_bulk_execute_all_thread(self):
        """Start bulk_execute_all in a separate thread to prevent UI freezing."""
        thread = threading.Thread(target=self.bulk_execute_all, daemon=True)
        thread.start()
    
    def start_retry_failed_thread(self):
        """Start retry of failed accounts in a separate thread."""
        thread = threading.Thread(target=self.retry_failed_accounts, daemon=True)
        thread.start()
    
    def clear_error_files_ui(self):
        """Clear error files and update UI."""
        clear_error_files()
        self.log_output("🗑️ Error files cleared.")
    
    def retry_failed_accounts(self):
        """Retry accounts that previously failed (excluding credential errors)."""
        retry_accounts = load_retry_accounts()
        if not retry_accounts:
            self.log_output("No accounts found for retry.")
            return
        
        self.log_output(f"🔄 Starting retry for {len(retry_accounts)} failed accounts...")
        
        # Create a temporary input for retry accounts
        retry_input = ""
        for email, password in retry_accounts:
            retry_input += f"{email}:{password}\n"
        
        # Temporarily store original input and use retry accounts
        original_input = self.accounts_text.toPlainText()
        self.accounts_text.setPlainText(retry_input)
        
        try:
            # Run bulk execution for retry accounts
            self.bulk_execute_all()
            
            # Clear retry file after successful processing attempt
            clear_retry_accounts()
            self.log_output("🎉 Retry processing completed. Retry file cleared.")
            
        finally:
            # Restore original input
            self.accounts_text.setPlainText(original_input)
    
    def start_setup_authenticator_thread(self):
        """Start setup_authenticator_for_all_accounts in a separate thread to prevent UI freezing."""
        thread = threading.Thread(target=self.setup_authenticator_for_all_accounts, daemon=True)
        thread.start()
    
    def start_generate_app_password_thread(self):
        """Start generate_app_password_for_all_accounts in a separate thread to prevent UI freezing."""
        thread = threading.Thread(target=self.generate_app_password_for_all_accounts, daemon=True)
        thread.start()
        
    def bulk_execute_all(self):
            accounts = self.get_accounts_from_input()
            max_concurrent_sessions = self.concurrent_spinbox.value()
        
            if not accounts:
                self.log_output("No valid accounts to process.")
                self.update_status("No accounts found.")
                return
        
            total_accounts = len(accounts)
            completed_accounts = 0
            
            screen = QApplication.primaryScreen()
            if screen:
                geom = screen.availableGeometry()
                screen_width = geom.width()
                screen_height = geom.height()
            else:
                screen_width = 1920
                screen_height = 1080
            
            grid_size = min(max_concurrent_sessions, total_accounts)
            positions = self.get_grid_positions(grid_size, screen_width, screen_height)
            
            self.log_output(f"🔥 BULK: Creating {grid_size} concurrent windows in grid layout")
            self.log_output(f"📱 Screen: {screen_width}x{screen_height}")
        
            def update_progress():
                nonlocal completed_accounts
                completed_accounts += 1
                progress_percent = int((completed_accounts / total_accounts) * 100)
                self.update_status(f"Processing... ({completed_accounts}/{total_accounts})", progress_percent)
        
            def process_account(email, password, position, window_index):
                driver = None
                keep_driver_open = False
                max_retries = 3
                attempt = 0
                
                # --- THIS IS THE KEY LOGIC CHANGE ---
                # Get the state of the checkbox from the UI
                save_locally_only = self.local_only_checkbox.isChecked()
                if save_locally_only:
                    self.log_output(f"💡 Local-only saving is ENABLED for {email}")
                # ------------------------------------
            
                self.log_output(f"🚀 Starting processing for {email} (Window {window_index+1})")
            
                while attempt < max_retries:
                    if STOP_ALL_PROCESSES:
                        self.log_output(f"Process stopped for {email}")
                        return
                    
                    attempt += 1
                    try:
                        alias = extract_alias_from_email(email)
                        with active_accounts_lock:
                            if email in active_accounts:
                                self.log_output(f"Account {email} is already being processed.")
                                return
                            active_accounts.add(email)
            
                        if self.headless_checkbox.isChecked():
                            ua = UserAgent()
                            user_agent = ua.random
                            chrome_options = uc.ChromeOptions()
                            chrome_options.add_argument("--disable-search-engine-choice-screen")
                            chrome_options.add_argument("--headless=new")
                            driver = uc.Chrome(options=chrome_options)
                        else:
                            from selenium import webdriver
                            chrome_options = webdriver.ChromeOptions()
                            driver = webdriver.Chrome(options=chrome_options)
                            try:
                                driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                            except Exception as e:
                                self.log_output(f"❌ Failed to position BULK window {window_index+1}: {e}")
            
                        profile_path = os.path.join(PROFILE_FOLDER, alias)
                        os.makedirs(profile_path, exist_ok=True)
            
                        login_success = login_gmail(driver, email, password, profile_path)
                        if not login_success:
                            if "wrong password" in driver.page_source.lower():
                                log_credential_error(email, password, "login_failed", "Wrong password")
                                return
                            if attempt >= max_retries:
                                save_for_retry(email, password, f"Login failed after {max_retries} attempts")
                                return
                            else:
                                driver.quit()
                                driver = None
                                continue
            
                        # Pass the checkbox state to the comprehensive processor
                        success, step_completed, error_type, error_details = process_account_comprehensive(
                            driver, email, password, profile_path, local_only=save_locally_only
                        )
                        
                        if success:
                            self.log_output(f"🎉 All steps completed successfully for {email}")
                            keep_driver_open = False
                            return
                        else:
                            self.log_output(f"❌ Failed at step '{step_completed}' for {email}: {error_details}")
                            if error_type == "id_verification":
                                keep_driver_open = True
                                self.drivers[email] = driver
                                return
                            elif error_type in ["login_failed", "wrong_password"]:
                                log_credential_error(email, password, error_type, error_details)
                                return
                            else:
                                if attempt >= max_retries:
                                    save_for_retry(email, password, f"Failed at {step_completed}: {error_details}")
                                    return
                                else:
                                    driver.quit()
                                    driver = None
                                    continue
            
                    except Exception as e:
                        logging.error(f"Error processing account {email} on attempt {attempt}: {e}")
                        if attempt >= max_retries:
                            return
                        if driver:
                            driver.quit()
                        driver = None
                        continue
            
                    finally:
                        if driver and not keep_driver_open:
                            driver.quit()
                        with active_accounts_lock:
                            active_accounts.discard(email)
                        update_progress()
                        if attempt >= max_retries:
                            break
            
            self.current_executor = ThreadPoolExecutor(max_workers=max_concurrent_sessions)
            futures = {}
            
            for i, (email, password) in enumerate(accounts):
                position_index = i % len(positions)
                pos = positions[position_index]
                future = self.current_executor.submit(process_account, email, password, pos, position_index)
                futures[future] = (email, password)
    
            for future in concurrent.futures.as_completed(futures):
                if STOP_ALL_PROCESSES:
                    self.log_output("Bulk execution stopped by user.")
                    for f in futures:
                        f.cancel()
                    break
                
                email, password = futures[future]
                try:
                    future.result()
                except Exception as e:
                    self.log_output(f"Exception for {email}: {e}")
                
                QApplication.processEvents()
            
            self.current_executor.shutdown(wait=False)
            self.current_executor = None
            
            self.log_output("🧹 Cleaning up browser windows after bulk execution...")
            self.close_all_browser_sessions()
            
            self.drivers.clear()
            self.log_output("✅ Drivers dictionary cleared to prevent arrangement attempts")
        
            self.log_output("Bulk execution completed for all accounts.")
            self.update_status("Bulk execution completed.")
    

    def close_all_browser_sessions(self):
        """Close all browser sessions gracefully after completion."""
        self.log_output("🌐 Closing all browser sessions...")
        
        # Close all browser drivers gracefully
        closed_count = 0
        for email, driver in list(self.drivers.items()):
            try:
                driver.quit()
                self.log_output(f"✅ Closed browser for {email}")
                closed_count += 1
            except Exception as e:
                self.log_output(f"⚠️ Error closing browser for {email}: {e}")
        
        # Clear the drivers dictionary
        self.drivers.clear()
        
        if closed_count > 0:
            self.log_output(f"🎉 Successfully closed {closed_count} browser session(s)")
        else:
            self.log_output("ℹ️ No browser sessions to close")

    def login_multiple_accounts(self):
        accounts = self.get_accounts_from_input()
        max_concurrent_sessions = self.concurrent_spinbox.value()
        if not accounts:
            self.log_output("No valid accounts to process.")
            self.update_status("No accounts found.")
            return
        
        total_accounts = len(accounts)
        
        # Get screen dimensions
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            screen_width = geom.width()
            screen_height = geom.height()
        else:
            screen_width = 1920
            screen_height = 1080
        
        # Calculate grid positions for the number of concurrent windows (not total accounts)
        # This ensures windows fit properly on screen based on user's concurrent setting
        grid_size = min(max_concurrent_sessions, total_accounts)
        positions = self.get_grid_positions(grid_size, screen_width, screen_height)
        
        self.log_output(f"🔥 Creating {grid_size} concurrent windows in grid layout")
        self.log_output(f"📱 Screen: {screen_width}x{screen_height}")
        
        def login_single_account(email, password, position, window_index):
            driver = None
            try:
                # Check if stop flag is set
                if STOP_ALL_PROCESSES:
                    self.log_output(f"Login process stopped for {email}")
                    return
                    
                alias = extract_alias_from_email(email)
                with active_accounts_lock:
                    if email in active_accounts:
                        self.log_output(f"Account {email} is already being processed.")
                        return
                    active_accounts.add(email)
        
                if self.headless_checkbox.isChecked():
                    self.log_output("Headless mode is enabled. Disable it to view the browser windows.")
                    # Use undetected chrome for headless (positioning doesn't matter)
                    chrome_options = uc.ChromeOptions()
                    chrome_options.add_argument("--headless")
                    driver = uc.Chrome(options=chrome_options)
                else:
                    # USE REGULAR CHROME WEBDRIVER (like the working test)
                    from selenium import webdriver
                    chrome_options = webdriver.ChromeOptions()
                    chrome_options.add_argument("--no-first-run")
                    chrome_options.add_argument("--no-default-browser-check")
                    chrome_options.add_argument("--disable-infobars")
                    chrome_options.add_argument("--disable-notifications")
                    
                    # Create regular Chrome driver (not undetected)
                    driver = webdriver.Chrome(options=chrome_options)
                    
                    # IMMEDIATELY set window position (like working test)
                    try:
                        driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                        self.log_output(f"✅ Window {window_index+1}: positioned at ({position['x']},{position['y']}) size {position['width']}x{position['height']}")
                    except Exception as e:
                        try:
                            driver.set_window_size(position['width'], position['height'])
                            driver.set_window_position(position['x'], position['y'])
                            self.log_output(f"✅ Window {window_index+1}: positioned with separate calls")
                        except Exception as e2:
                            self.log_output(f"❌ Failed to position window {window_index+1}: {e2}")
                
                profile_path = os.path.join(PROFILE_FOLDER, alias)
                if not os.path.exists(profile_path):
                    os.makedirs(profile_path)
        
                success = login_gmail(driver, email, password, profile_path)
                if success:
                    self.drivers[email] = driver
                    self.log_output(f"✅ Logged in and positioned: {email}")
                else:
                    self.log_output(f"❌ Failed to log in: {email}")
                    if driver:
                        driver.quit()
            except Exception as e:
                self.log_output(f"❌ Error processing {email}: {e}")
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
            finally:
                with active_accounts_lock:
                    active_accounts.discard(email)
        
        # Store executor reference for stop functionality
        self.current_executor = ThreadPoolExecutor(max_workers=max_concurrent_sessions)
        futures = []
        
        # Assign positions in round-robin fashion for concurrent windows
        for i, (email, password) in enumerate(accounts):
            position_index = i % len(positions)  # Round-robin through available positions
            pos = positions[position_index]
            futures.append(self.current_executor.submit(login_single_account, email, password, pos, position_index))
        
        # Monitor futures with UI responsiveness
        for fut in concurrent.futures.as_completed(futures):
            # Check for stop flag and keep UI responsive
            if STOP_ALL_PROCESSES:
                self.log_output("Login process stopped by user.")
                # Cancel remaining futures
                for f in futures:
                    f.cancel()
                break
            
            try:
                fut.result()
            except Exception as e:
                self.log_output(f"Exception: {e}")
            
            # Keep UI responsive
            QApplication.processEvents()
        
        self.current_executor.shutdown(wait=False)
        self.current_executor = None
        self.log_output("🎉 Login process completed for all accounts.")
        self.update_status("Login process completed.")
    
    def verify_app_password_saved(self, email, app_password):
        """
        Verify that the app password was actually saved both locally and on server.
        Returns True if verified, False otherwise.
        """
        try:
            # Check local file
            password_file_local = os.path.join(PROFILE_FOLDER, 'app_passwords.txt')
            if os.path.exists(password_file_local):
                with open(password_file_local, 'r', encoding='utf-8') as f:
                    local_content = f.read()
                    # Check for clean password (without dashes) since that's how it's saved
                    clean_app_password = app_password.replace('-', '')
                    if f"{email}: {clean_app_password}" in local_content:
                        self.log_output(f"📝 Local save verified for {email} (clean format: {clean_app_password})")
                        return True
                    else:
                        self.log_output(f"❌ Local save NOT verified for {email}")
                        self.log_output(f"🔍 Looking for: {email}: {clean_app_password}")
                        return False
            else:
                self.log_output(f"❌ Local app_passwords.txt file does not exist!")
                return False
        except Exception as e:
            self.log_output(f"❌ Error verifying app password save for {email}: {e}")
            return False

    def get_grid_positions(self, num_accounts, screen_width, screen_height):
        """
        Calculate grid positions for num_accounts windows based on the screen dimensions.
        Copied from admin_sdk.py - this is the working implementation!
        Returns a list of dictionaries with 'x', 'y', 'width', 'height' keys.
        """
        import math
        
        # Generate a near-square layout
        cols = math.ceil(math.sqrt(num_accounts))
        rows = math.ceil(num_accounts / cols)
    
        cell_width = screen_width // cols
        cell_height = screen_height // rows
    
        positions = []
        for i in range(num_accounts):
            col = i % cols
            row = i // cols
            x = col * cell_width
            y = row * cell_height
            positions.append({'x': x, 'y': y, 'width': cell_width, 'height': cell_height})
        
        self.log_output(f"Grid calculation: {rows} rows x {cols} columns, cell size: {cell_width}x{cell_height}")
        return positions

    def set_window_grid_position(self, driver, slot_index, total_slots, screen_width, screen_height):
        """
        Position a single browser window in a proper grid layout.
        Completely rewritten to prevent maximization and ensure proper arrangement.
        """
        try:
            # Simple window restore - no complex operations that might fail
            try:
                driver.execute_script("if (window.outerHeight == screen.availHeight) { window.resizeTo(800, 600); }")
                time.sleep(0.1)
            except Exception as e:
                self.log_output(f"Warning: Could not restore window {slot_index+1}: {e}")
            
            # Calculate optimal grid dimensions based on number of windows
            self.log_output(f"Grid calculation for {total_slots} windows:")
            if total_slots == 1:
                rows, cols = 1, 1
            elif total_slots == 2:
                rows, cols = 1, 2
            elif total_slots == 3:
                rows, cols = 1, 3
            elif total_slots == 4:
                rows, cols = 2, 2
            elif total_slots == 5:
                rows, cols = 2, 3
            elif total_slots == 6:
                rows, cols = 2, 3
            elif total_slots <= 9:
                rows, cols = 3, 3
            else:
                # For 10+ windows: calculate optimal grid
                rows = math.ceil(math.sqrt(total_slots))
                cols = math.ceil(total_slots / rows)
            
            self.log_output(f"Grid layout: {rows} rows x {cols} columns")
            
            # Calculate cell dimensions with proper spacing
            padding = 30  # Increased padding between windows
            available_width = screen_width - (padding * (cols + 1))
            available_height = screen_height - (padding * (rows + 1))
            
            cell_width = available_width // cols
            cell_height = available_height // rows
            
            self.log_output(f"Screen: {screen_width}x{screen_height}, Available: {available_width}x{available_height}")
            self.log_output(f"Cell size (before min): {cell_width}x{cell_height}")
            
            # Ensure reasonable minimum window size
            cell_width = max(cell_width, 500)
            cell_height = max(cell_height, 350)
            
            # Calculate position in grid
            row = slot_index // cols
            col = slot_index % cols
            x = padding + (col * (cell_width + padding))
            y = padding + (row * (cell_height + padding))
            
            self.log_output(f"Window {slot_index+1}: row={row}, col={col}, position=({x},{y}), size={cell_width}x{cell_height}")
            
            # Ensure window doesn't go off-screen
            x = max(0, min(x, screen_width - cell_width - 20))
            y = max(0, min(y, screen_height - cell_height - 20))
            
            self.log_output(f"Final position after bounds check: ({x},{y})")
            
            # Use the most reliable method: Selenium separate calls
            try:
                # First set size, then position - this is most reliable
                driver.set_window_size(cell_width, cell_height)
                time.sleep(0.1)
                driver.set_window_position(x, y)
                time.sleep(0.1)
                self.log_output(f"Window {slot_index+1}/{total_slots} positioned at ({x},{y}) with size ({cell_width}x{cell_height})")
            except Exception as e:
                self.log_output(f"Positioning failed for window {slot_index+1}: {e}")
                # Simple fallback
                try:
                    fallback_x = (slot_index % 3) * 400
                    fallback_y = (slot_index // 3) * 300
                    driver.set_window_position(fallback_x, fallback_y)
                    driver.set_window_size(500, 350)
                    self.log_output(f"Fallback: positioned window {slot_index+1} at ({fallback_x},{fallback_y})")
                except Exception:
                    self.log_output(f"All positioning methods failed for window {slot_index+1}")
            
            # Ensure window is visible and focused
            try:
                driver.execute_script("""
                    window.focus();
                    if (document.body) {
                        document.body.style.display = 'block';
                        document.body.style.visibility = 'visible';
                        document.body.style.opacity = '1';
                        document.body.style.zIndex = '9999';
                    }
                    if (document.documentElement) {
                        document.documentElement.style.display = 'block';
                        document.documentElement.style.visibility = 'visible';
                        document.documentElement.style.opacity = '1';
                        document.documentElement.style.zIndex = '9999';
                    }
                """)
            except Exception as e:
                self.log_output(f"Warning: Could not ensure visibility for window {slot_index+1}: {e}")
            
        except Exception as e:
            self.log_output(f"Error positioning window {slot_index+1}: {e}")
    
    @pyqtSlot()
    def arrange_sessions_based_on_accounts(self):
        """
        Arrange browser sessions in a proper grid layout based on the number of accounts.
        Simplified and fixed to ensure all windows are visible and properly positioned.
        """
        self.log_output("=== Starting window arrangement ===")
        
        if not self.drivers:
            self.log_output("No active sessions to arrange.")
            return

        # Filter out closed/inactive drivers
        active_drivers = []
        for email, driver in self.drivers.items():
            try:
                # Test if driver is still responsive
                driver.current_url
                active_drivers.append(driver)
            except Exception as e:
                self.log_output(f"Driver for {email} is no longer active, removing from arrangement")
                # Remove inactive driver from the dictionary
                try:
                    del self.drivers[email]
                except:
                    pass
        
        total_windows = len(active_drivers)
        
        self.log_output(f"Found {total_windows} active browser windows to arrange")
        if total_windows > 0:
            self.log_output(f"Available driver emails: {list(self.drivers.keys())}")
        
        if total_windows == 0:
            self.log_output("No active browser windows found to arrange.")
            return

        self.log_output(f"=== Arranging {total_windows} windows in grid layout ===")
        
        # Get screen dimensions
        screen = QApplication.primaryScreen()
        if not screen:
            self.log_output("No primary screen detected.")
            return

        geom = screen.availableGeometry()
        screen_width = geom.width()
        screen_height = geom.height()
        
        self.log_output(f"Screen dimensions: {screen_width} x {screen_height}")
        
        # Wait a moment for all windows to be ready
        time.sleep(0.5)
        
        # Arrange windows in proper grid
        for index, driver in enumerate(active_drivers):
            # Check for stop flag and keep UI responsive
            if STOP_ALL_PROCESSES:
                self.log_output("Window arrangement stopped by user.")
                return
            
            try:
                self.log_output(f">>> Positioning window {index + 1} of {total_windows}")
                self.set_window_grid_position(driver, index, total_windows, screen_width, screen_height)
                self.log_output(f">>> Successfully positioned window {index + 1}")
                time.sleep(0.3)  # Small delay between window positioning
            except Exception as e:
                self.log_output(f"!!! ERROR positioning window {index + 1}: {e}")
            
            # Keep UI responsive
            QApplication.processEvents()
        
        self.log_output(f"=== Completed arranging {len(active_drivers)} browser windows ===")
        
        # Force bring all windows to front
        self.bring_windows_to_front(active_drivers)
        
        # Additional window management for better visibility
        self.ensure_windows_visible(active_drivers)
        
        # Final check - ensure all windows are visible
        self.log_output("Final window visibility check...")
        for i, driver in enumerate(active_drivers):
            # Check for stop flag and keep UI responsive
            if STOP_ALL_PROCESSES:
                self.log_output("Final window check stopped by user.")
                return
            
            try:
                driver.execute_script("window.focus();")
                self.log_output(f"Ensured window {i+1} is focused")
            except Exception as e:
                self.log_output(f"Could not focus window {i+1}: {e}")
    
    def ensure_windows_visible(self, drivers):
        """
        Ensure all windows are visible and properly displayed.
        """
        try:
            for i, driver in enumerate(drivers):
                try:
                    # Multiple methods to ensure window visibility
                    driver.execute_script("""
                        window.focus();
                        window.moveTo(0, 0);
                        window.resizeTo(800, 600);
                        document.body.style.display = 'block';
                        document.documentElement.style.display = 'block';
                        document.body.style.visibility = 'visible';
                        document.documentElement.style.visibility = 'visible';
                        document.body.style.opacity = '1';
                        document.documentElement.style.opacity = '1';
                    """)
                    
                    # Additional visibility checks
                    driver.execute_script("""
                        if (document.body) {
                            document.body.style.zIndex = '9999';
                        }
                        if (document.documentElement) {
                            document.documentElement.style.zIndex = '9999';
                        }
                    """)
                    
                    self.log_output(f"Ensured visibility for window {i+1}")
                    time.sleep(0.1)  # Small delay between windows
                    
                except Exception as e:
                    self.log_output(f"Error ensuring visibility for window {i+1}: {e}")
                    
        except Exception as e:
            self.log_output(f"Error in ensure_windows_visible: {e}")
    
    def bring_windows_to_front(self, drivers):
        """
        Force bring all browser windows to the front with enhanced methods.
        """
        try:
            for i, driver in enumerate(drivers):
                try:
                    # Multiple methods to bring window to front
                    driver.execute_script("window.focus();")
                    driver.execute_script("window.moveTo(0, 0);")
                    driver.execute_script("window.resizeTo(800, 600);")
                    
                    # Enhanced JavaScript for better window management
                    driver.execute_script("""
                        window.focus();
                        window.moveTo(0, 0);
                        window.resizeTo(800, 600);
                        document.body.style.display = 'block';
                        document.documentElement.style.display = 'block';
                        document.body.style.visibility = 'visible';
                        document.documentElement.style.visibility = 'visible';
                        document.body.style.opacity = '1';
                        document.documentElement.style.opacity = '1';
                        document.body.style.zIndex = '9999';
                        document.documentElement.style.zIndex = '9999';
                    """)
                    
                    self.log_output(f"Brought window {i+1} to front")
                    time.sleep(0.2)  # Small delay between windows
                    
                except Exception as e:
                    self.log_output(f"Error bringing window {i+1} to front: {e}")
                    
        except Exception as e:
            self.log_output(f"Error in bring_windows_to_front: {e}")
    
    def show_all_windows(self):
        """
        Force show all browser windows and arrange them properly on screen.
        """
        if not self.drivers:
            self.log_output("No active sessions to show.")
            return
        
        self.log_output(f"Showing and arranging {len(self.drivers)} browser windows...")
        
        # First, ensure all windows are visible
        for email, driver in self.drivers.items():
            try:
                # Force the window to be visible
                driver.execute_script("window.focus();")
                driver.execute_script("""
                    window.focus();
                    if (document.body) {
                        document.body.style.display = 'block';
                        document.body.style.visibility = 'visible';
                        document.body.style.opacity = '1';
                        document.body.style.zIndex = '9999';
                    }
                    if (document.documentElement) {
                        document.documentElement.style.display = 'block';
                        document.documentElement.style.visibility = 'visible';
                        document.documentElement.style.opacity = '1';
                        document.documentElement.style.zIndex = '9999';
                    }
                """)
                
                self.log_output(f"Made window visible for {email}")
                time.sleep(0.2)  # Small delay between windows
                
            except Exception as e:
                self.log_output(f"Error showing window for {email}: {e}")
        
        # Then arrange them properly
        self.arrange_sessions_based_on_accounts()
        
        self.log_output("Finished showing and arranging all windows.")
    
    
    def retrieve_app_passwords_for_domain(self):
        """Retrieve and display app passwords for the specified domain."""
        domain = self.domain_input.text().strip()
        if not domain:
            self.log_output("Please enter a domain.")
            return
    
        # Load the app passwords
        app_passwords = load_app_passwords()
    
        # Filter app passwords based on domain
        domain_app_passwords = {}
        for email, password in app_passwords.items():
            # Extract the domain from the email
            email_domain = email.split('@')[1]
            if email_domain.lower().endswith(domain.lower()):
                domain_app_passwords[email] = password
    
        # Display the filtered app passwords
        self.app_password_display.clear()
        if domain_app_passwords:
            for email, password in domain_app_passwords.items():
                self.app_password_display.append(f"{email}: {password}")
            self.log_output(f"App passwords for domain '{domain}' retrieved and displayed.")
        else:
            self.app_password_display.append(f"No app passwords found for domain '{domain}'.")
            self.log_output(f"No app passwords found for domain '{domain}'.")
            
        
    def enable_two_step_verification_for_all_accounts(self):
        """Enable Two-Step Verification for all accounts listed in the input field."""
        accounts = self.get_accounts_from_input()
        for email, password in accounts:
            driver = self.drivers.get(email)
            if driver:
                alias = extract_alias_from_email(email)
                profile_path = os.path.join(PROFILE_FOLDER, alias)
                if not os.path.exists(profile_path):
                    os.makedirs(profile_path)

                # Check if Two-Step Verification is already enabled
                if is_two_step_verification_enabled(driver):
                    self.log_output(f"Skipping Two-Step Verification for {email} as it's already enabled.")
                    continue  # Skip to the next account if already enabled

                # Proceed with enabling Two-Step Verification if not already enabled
                if not enable_two_step_verification(driver, profile_path, email):
                    self.log_output(f"Failed to enable Two-Step Verification for {email}.")
                else:
                    self.log_output(f"Two-Step Verification enabled successfully for {email}.")
            else:
                self.log_output(f"No active session found for {email}. Please log in first.")
        self.update_status("Two-Step Verification process completed for all accounts.")

    def setup_authenticator_for_all_accounts(self):
        """Setup Authenticator for all accounts listed in the input field."""
        accounts = self.get_accounts_from_input()
        for email, password in accounts:
            driver = self.drivers.get(email)
            if driver:
                alias = extract_alias_from_email(email)
                profile_path = os.path.join(PROFILE_FOLDER, alias)
                if not os.path.exists(profile_path):
                    os.makedirs(profile_path)

                # Check if Authenticator is already set up
                if is_authenticator_set_up(driver):
                    self.log_output(f"Skipping Authenticator setup for {email} as it's already set up.")
                    continue  # Skip to the next account if already set up

                # Proceed with setting up Authenticator if not already set up
                if not setup_authenticator(driver, profile_path, email):
                    self.log_output(f"Failed to set up Authenticator for {email}.")
                else:
                    self.log_output(f"Authenticator set up successfully for {email}.")
            else:
                self.log_output(f"No active session found for {email}. Please log in first.")
        self.update_status("Authenticator setup process completed for all accounts.")

    def generate_app_password_for_all_accounts(self):
        """Generate app passwords for all accounts listed in the input field."""
        accounts = self.get_accounts_from_input()  # Now this will get the email:password data
        for email, password in accounts:
            driver = self.drivers.get(email)
            if driver:
                alias = extract_alias_from_email(email)
                profile_path = os.path.join(PROFILE_FOLDER, alias)
                app_password = generate_app_password(driver, email, profile_path)  # Call the function to generate app passwords
                if app_password:
                    self.log_output(f"✅ {email}: {app_password}")
                    # The save is already handled inside generate_app_password with retries
                    # But let's verify it worked
                    if self.verify_app_password_saved(email, app_password):
                        self.log_output(f"✅ CONFIRMED: App password for {email} saved successfully!")
                    else:
                        self.log_output(f"⚠️ WARNING: App password generated for {email} but may not be saved properly!")
                    self.load_and_display_all_app_passwords()  # Load and display all app passwords in the UI
                else:
                    self.log_output(f"❌ Failed to generate app password for {email}")
            else:
                self.log_output(f"No active session found for {email}. Please log in first.")

    def retrieve_app_passwords_from_server(self):
        """
        Download app passwords from the server and merge them with local app_passwords.txt.
        Local entries overwrite server entries if duplicates exist.
        Display the merged app passwords in the application's GUI.
        """
        try:
            sftp = sftp_connect()
            remote_password_file = os.path.join(REMOTE_DIR, 'app_passwords.txt').replace("\\", "/")
            local_temp_password_file = os.path.join(PROFILE_FOLDER, 'app_passwords_server.txt')
            local_password_file = os.path.join(PROFILE_FOLDER, 'app_passwords.txt')

            # Download the app_passwords.txt from the server to a temporary local file
            try:
                sftp.get(remote_password_file, local_temp_password_file)
                logging.info(f"Downloaded app_passwords.txt from {remote_password_file} to {local_temp_password_file}")
            except FileNotFoundError:
                logging.error(f"app_passwords.txt not found on the server at {remote_password_file}")
                self.log_output("app_passwords.txt not found on the server.")
                return
            except Exception as e:
                logging.error(f"Error downloading app_passwords.txt from server: {e}")
                self.log_output(f"Error downloading app_passwords.txt from server: {e}")
                return

            # Load server app passwords
            server_app_passwords = {}
            with open(local_temp_password_file, 'r') as f:
                for line in f:
                    if ':' in line:
                        email, password = line.strip().split(':', 1)
                        email = email.strip()
                        password = password.strip()
                        if is_valid_email(email) and password:
                            server_app_passwords[email] = password

            # Load local app passwords
            local_app_passwords = load_app_passwords()

            # Merge passwords: local passwords overwrite server passwords
            merged_app_passwords = server_app_passwords.copy()
            merged_app_passwords.update(local_app_passwords)

            # Save the merged app_passwords.txt locally
            try:
                with open(local_password_file, 'w') as f:
                    for email_key, password in sorted(merged_app_passwords.items()):
                        f.write(f"{email_key}: {password}\n")
                logging.info(f"Merged app_passwords.txt saved locally at {local_password_file}")
            except Exception as e:
                logging.error(f"Failed to save merged app_passwords.txt locally: {e}")
                self.log_output(f"Failed to save merged app_passwords.txt locally: {e}")
                return

            # Upload the merged app_passwords.txt back to the server
            try:
                sftp.put(local_password_file, remote_password_file)
                logging.info(f"Merged app_passwords.txt uploaded to {remote_password_file} on the server.")
            except Exception as e:
                logging.error(f"Failed to upload merged app_passwords.txt to server: {e}")
                self.log_output(f"Failed to upload merged app_passwords.txt to server: {e}")
                return
            finally:
                # Remove the temporary server password file
                if os.path.exists(local_temp_password_file):
                    os.remove(local_temp_password_file)

            # Display the merged app passwords in the GUI
            self.load_and_display_all_app_passwords()
            self.log_output("App passwords retrieved, merged, and displayed successfully.")

        except Exception as e:
            logging.error(f"Error during retrieve_app_passwords_from_server: {e}")
            self.log_output(f"Error during retrieval: {e}")
        finally:
            sftp.close()
            logging.info("SFTP connection closed after retrieving app_passwords.txt.")

    def load_and_display_all_app_passwords(self):
        """
        Load all app passwords from the local app_passwords.txt file and display them in the GUI.
        Ensures that each email appears only once with its latest app password.
        """
        self.app_password_display.clear()  # Clear the display area
    
        # Load the app passwords
        app_passwords = load_app_passwords()
    
        if app_passwords:
            # Display all app passwords
            for email, password in app_passwords.items():
                self.app_password_display.append(f"{email}: {password}")
        else:
            self.app_password_display.append("No app passwords found.")
    
        logging.info("App passwords loaded and displayed.")
        
    def auto_arrange_new_sessions(self):
        """
        Automatically arrange new sessions when they are opened.
        This function is called when auto-arrange is enabled.
        """
        if not self.auto_arrange_checkbox.isChecked():
            return
            
        if self.headless_checkbox.isChecked():
            return  # Don't arrange in headless mode
            
        # Get current active drivers
        accounts = self.get_accounts_from_input()
        account_emails = [email for email, password in accounts]
        active_drivers = []
        
        for email, driver in self.drivers.items():
            if email in account_emails:
                active_drivers.append(driver)
        
        if len(active_drivers) > 1:
            # Add a small delay to ensure all windows are loaded
            def delayed_arrange():
                time.sleep(0.5)  # Wait for windows to be fully loaded
                # Check if drivers are still active before arranging
                if self.drivers and any(self.drivers.values()):
                    QMetaObject.invokeMethod(self, "arrange_sessions_based_on_accounts", Qt.QueuedConnection)
                else:
                    self.log_output("No active drivers found, skipping window arrangement")
            
            # Run the delayed arrange in a separate thread
            threading.Thread(target=delayed_arrange, daemon=True).start()
    
    def on_session_opened(self, email):
        """
        Called when a new session is opened to trigger auto-arrangement.
        """
        if self.auto_arrange_checkbox.isChecked() and not self.headless_checkbox.isChecked():
            self.auto_arrange_new_sessions()

    def stop_all_processes(self):
        """
        Stop all running processes immediately - FORCE KILL EVERYTHING.
        """
        global STOP_ALL_PROCESSES
        STOP_ALL_PROCESSES = True
        self.log_output("🛑 EMERGENCY STOP - KILLING ALL PROCESSES...")
        self.update_status("🛑 EMERGENCY STOP")
        
        # FORCE kill all Chrome processes
        try:
            import subprocess
            import sys
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True)
                subprocess.run(["taskkill", "/f", "/im", "chromedriver.exe"], capture_output=True)
                self.log_output("Force killed all Chrome processes on Windows")
            else:
                subprocess.run(["pkill", "-f", "chrome"], capture_output=True)
                subprocess.run(["pkill", "-f", "chromedriver"], capture_output=True)
                self.log_output("Force killed all Chrome processes on Unix")
        except Exception as e:
            self.log_output(f"Error force killing Chrome: {e}")
        
        # Stop ThreadPoolExecutors if running
        if hasattr(self, 'current_executor') and self.current_executor:
            try:
                self.current_executor.shutdown(wait=False)
                self.log_output("Stopped ThreadPoolExecutor")
            except Exception as e:
                self.log_output(f"Error stopping executor: {e}")
        
        # Close all browser drivers (in case some are still open)
        for email, driver in list(self.drivers.items()):
            try:
                driver.quit()
                self.log_output(f"Closed browser for {email}")
            except Exception as e:
                pass  # Ignore errors since we force killed already
        
        self.drivers.clear()
        
        # Clear active accounts
        with active_accounts_lock:
            active_accounts.clear()
        
        self.log_output("🛑 ALL PROCESSES FORCE STOPPED.")
        self.update_status("🛑 ALL PROCESSES STOPPED")
        
        # Reset stop flag after a delay
        def reset_stop_flag():
            time.sleep(1)
            global STOP_ALL_PROCESSES
            STOP_ALL_PROCESSES = False
            self.update_status("Ready")
        
        threading.Thread(target=reset_stop_flag, daemon=True).start()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = GmailAutomationApp()
    window.show()
    sys.exit(app.exec_())













atexit.register(lambda: remove_duplicate_app_passwords("app_passwords.txt"))
