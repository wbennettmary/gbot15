import os
import sys
import json
import math
import random, string, time
import logging
import threading
import tempfile
import re
import paramiko
import stat
import shutil
import hashlib
import subprocess  
import uuid
import requests
import platform
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from selenium.webdriver.common.action_chains import ActionChains
# from fake_useragent import UserAgent  # Removed for .exe compatibility
from PyQt5.QtCore import QTimer, pyqtSignal, QObject
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QCheckBox,
    QSpinBox,
    QGroupBox,
    QGridLayout
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QIcon
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
from selenium.webdriver.chrome.options import Options
import undetected_chromedriver as uc
import pyotp

# Server credentials (replace with actual values)
SERVER_ADDRESS = '46.224.9.127'
SERVER_PORT = 22
USERNAME = 'root'
PASSWORD = 'JnsQ3G98JU027QP'
REMOTE_DIR = '/home/brightmindscampus/'

# Remote account retrieval settings
REMOTE_ACCOUNT_SERVER = '159.89.19.179'  # Server for account retrieval
REMOTE_ACCOUNT_PORT = 22
REMOTE_ACCOUNT_USERNAME = 'root'
REMOTE_ACCOUNT_PASSWORD = 'L*tX--34GmtnSML'
REMOTE_ACCOUNT_DIR = '/home/Accounts'  # Directory containing accounts.json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Failure log files
AUTHENTICATOR_FAILURE_FILE = "authenticator_failures.log"
TWO_STEP_FAILURE_FILE = "two_step_verification_failures.log"

def inject_randomized_javascript(driver):
    script = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    setTimeout(function() {
        console.log("Randomized action");
    }, Math.floor(Math.random() * 3000) + 500);
    document.addEventListener('mousemove', function(e) {
        let randomX = Math.random() * window.innerWidth;
        let randomY = Math.random() * window.innerHeight;
        window.scrollBy(randomX, randomY);
    });
    """
    driver.execute_script(script)

class ServerDeviceSecurity:
    def __init__(self):
        # Your server URL - UPDATE THIS to your actual server!
        self.SERVER_URL = "http://159.89.19.179:5000/api"  # CHANGE THIS!
        self.REQUEST_TIMEOUT = 10
        
    def get_computer_id(self):
        """Get unique computer ID using Windows commands"""
        try:
            # Method 1: Windows Machine GUID (most reliable)
            result = subprocess.run(
                ['reg', 'query', 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Cryptography', '/v', 'MachineGuid'],
                capture_output=True, text=True, check=True
            )
            for line in result.stdout.split('\n'):
                if 'MachineGuid' in line:
                    return line.split()[-1].strip()
        except:
            pass
            
        try:
            # Method 2: WMIC UUID fallback
            result = subprocess.run(['wmic', 'csproduct', 'get', 'uuid'], 
                                  capture_output=True, text=True)
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                uuid_val = lines[1].strip()
                if uuid_val and uuid_val != "UUID":
                    return uuid_val
        except:
            pass
            
        # Final fallback
        return str(uuid.uuid4())

    def get_primary_mac_address(self):
        """Get the primary network adapter MAC address"""
        try:
            result = subprocess.run(['getmac', '/v', '/fo', 'csv'], 
                                  capture_output=True, text=True)
            lines = result.stdout.strip().split('\n')[1:]  # Skip header
            
            for line in lines:
                if 'Ethernet' in line and 'Connected' in line:
                    parts = line.split(',')
                    mac = parts[2].strip().replace('"', '')
                    if mac and mac != "N/A":
                        return mac.replace('-', ':').upper()
        except:
            pass
        
        # Fallback using uuid
        try:
            mac_int = uuid.getnode()
            mac_hex = f"{mac_int:012x}"
            return ":".join([mac_hex[i:i+2] for i in range(0, 12, 2)]).upper()
        except:
            return "00:00:00:00:00:00"

    def get_device_fingerprint(self):
        """Create device fingerprint for server verification"""
        computer_id = self.get_computer_id()
        mac_address = self.get_primary_mac_address()
        
        try:
            computer_name = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
        except:
            computer_name = "Unknown"
            
        return {
            "computer_id": computer_id,
            "mac_address": mac_address,
            "computer_name": computer_name,
            "platform": platform.system(),
            "app_version": "1.0"
        }

    def check_server_authorization(self, device_info):
        """Check with server if device is authorized"""
        try:
            logging.info(f"Checking authorization with server: {self.SERVER_URL}")
            
            request_data = {
                "action": "check_device",
                "device_info": device_info
            }
            
            response = requests.post(
                f"{self.SERVER_URL}/check-device",
                json=request_data,
                timeout=self.REQUEST_TIMEOUT,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'AdminSDK-App/1.0'
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                
                if result.get("authorized") == True:
                    logging.info("✅ Device authorized by server")
                    return True
                else:
                    logging.warning("❌ Device not authorized by server")
                    return False
                    
            else:
                logging.error(f"Server returned status code: {response.status_code}")
                return False
                
        except Exception as e:
            logging.error(f"Server check error: {e}")
            return False

    def validate_device_access(self):
        """Main method to validate device access"""
        try:
            logging.info("🔒 Starting device security validation...")
            
            device_info = self.get_device_fingerprint()
            logging.info(f"Device ID: {device_info['computer_id']}")
            logging.info(f"MAC Address: {device_info['mac_address']}")
            
            is_authorized = self.check_server_authorization(device_info)
            
            if is_authorized:
                logging.info("✅ Device access granted")
                return True
            else:
                logging.warning("❌ Device access denied")
                self.show_access_denied_dialog(device_info)
                return False
                
        except Exception as e:
            logging.error(f"Security validation error: {e}")
            return False

    def show_access_denied_dialog(self, device_info):
        """Show access denied message to user"""
        message = f"""🚫 ACCESS DENIED

This application is restricted to authorized company devices.

Your Device Information:
Computer ID: {device_info['computer_id']}
MAC Address: {device_info['mac_address']}
Computer Name: {device_info['computer_name']}

Contact IT Support to authorize this device."""
        
        QMessageBox.critical(None, "Access Denied", message)

class GoogleWorkspaceApp(QWidget):
    _driver_lock = threading.Lock()

    def __init__(self):
        super().__init__()
                # 🔒 ADD THIS SECURITY CHECK HERE - before everything else!
        try:
            security = ServerDeviceSecurity()
            
            if not security.validate_device_access():
                sys.exit()  # Close app if not authorized
                
            logging.info("✅ Device security validation passed")
        except Exception as e:
            QMessageBox.critical(None, "Security Error", f"Security validation failed: {e}")
            sys.exit()

        self.driver = None  # Used for single-account operations
        # self.local_profiles_dir = os.path.join(os.getcwd(), "profiles")
        self.local_profiles_dir = None  # Force SFTP-only mode
        self.remote_dir = REMOTE_DIR
        self.server_address = SERVER_ADDRESS
        self.server_port = SERVER_PORT
        self.username = USERNAME
        self.password = PASSWORD
        self.AUTHENTICATOR_FAILURE_FILE = "authenticator_failures.log"
        self.TWO_STEP_FAILURE_FILE = "two_step_verification_failures.log"
        self.APP_PASSWORD_FAILURE_FILE = "app_password_failures.log"
        
        # Initialize remote account manager
        self.remote_account_manager = RemoteAccountManager(
            REMOTE_ACCOUNT_SERVER,
            REMOTE_ACCOUNT_PORT,
            REMOTE_ACCOUNT_USERNAME,
            REMOTE_ACCOUNT_PASSWORD,
            REMOTE_ACCOUNT_DIR
        )
        
        # Global OTP detection state
        self.global_otp_detection_active = False
        self.otp_detection_timer = None
        self.active_drivers = []  # Track all active drivers for OTP detection
        self.driver_lock = threading.Lock()  # Thread-safe access to active_drivers
        self.driver_to_account = {}  # Map driver instances to account emails
        self.account_to_driver = {}  # Map account emails to driver instances
        
        # Thread-safe OTP handler
        self.otp_handler = OTPHandler(self)
        
        # Thread-safe error handler
        self.error_handler = ErrorHandler(self)
        
        # Initialize stored accounts dictionary for password caching
        self.stored_accounts = {}
        
        self.initUI()
        
    def init_driver(self):
        try:
            chrome_options = webdriver.ChromeOptions()
            chrome_options.add_argument("--disable-search-engine-choice-screen")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            if hasattr(self, 'headless_checkbox') and self.headless_checkbox.isChecked():
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--window-size=1920,1080")
            # Fixed user agent instead of random one
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            chrome_options.add_argument(f"user-agent={user_agent}")

            self.driver = uc.Chrome(options=chrome_options)
            # ADD THIS: Set zoom for all pages
            inject_randomized_javascript(self.driver)
            logging.info("Driver initialized successfully")
        except Exception as e:
            if not self.handle_driver_error(e):
                logging.error(f"Failed to initialize driver: {e}")
            self.driver = None
            
    def detect_existing_browser_windows(self, accounts):
        """Detect existing browser windows and associate them with accounts"""
        try:
            with self.driver_lock:
                existing_drivers = self.active_drivers.copy()
            
            logging.info(f"🔍 DETECT WINDOWS - Checking {len(existing_drivers)} drivers against {len(accounts)} accounts")
            logging.info(f"🔍 DETECT WINDOWS - Looking for accounts: {accounts}")
            
            if not existing_drivers:
                logging.info("No existing browser windows detected")
                return {}
            
            logging.info(f"Detected {len(existing_drivers)} existing browser windows")
            
            # Map existing drivers to accounts
            driver_account_map = {}
            
            for driver in existing_drivers:
                try:
                    # Check if driver is still responsive
                    current_url = driver.current_url
                    logging.info(f"Driver URL: {current_url}")
                    
                    # Try to extract account from the current page
                    account_found = None
                    
                    # Method 1: Check if we're on a Google account page and already logged in
                    if "myaccount.google.com" in current_url or "admin.google.com" in current_url:
                        try:
                            # Check if we're logged in by looking for account-specific elements
                            # Look for account email in various page elements
                            account_selectors = [
                                "//div[contains(@class, 'email') or contains(@class, 'account')]",
                                "//span[contains(@class, 'email') or contains(@class, 'account')]",
                                "//div[contains(text(), '@')]",
                                "//span[contains(text(), '@')]",
                                "//div[@data-email]",
                                "//span[@data-email]",
                                "//div[contains(@aria-label, '@')]",
                                "//span[contains(@aria-label, '@')]"
                            ]
                            
                            for selector in account_selectors:
                                try:
                                    account_elements = driver.find_elements(By.XPATH, selector)
                                    for element in account_elements:
                                        text = element.text.strip()
                                        if '@' in text and any(account in text for account in accounts):
                                            for account in accounts:
                                                if account in text:
                                                    account_found = account
                                                    break
                                            if account_found:
                                                break
                                    if account_found:
                                        break
                                except:
                                    continue
                        except:
                            pass
                    
                    # Method 2: Check window title for account information
                    if not account_found:
                        try:
                            title = driver.title
                            logging.info(f"Driver title: {title}")
                            for account in accounts:
                                if account in title:
                                    account_found = account
                                    break
                        except:
                            pass
                    
                    # Method 3: Check page source for account information
                    if not account_found:
                        try:
                            page_source = driver.page_source
                            for account in accounts:
                                if account in page_source:
                                    account_found = account
                                    break
                        except:
                            pass
                    
                    # Method 4: Check if we're on a specific account page by URL patterns
                    if not account_found:
                        try:
                            # Check for account-specific URL patterns
                            for account in accounts:
                                if account.replace('@', '%40') in current_url or account in current_url:
                                    account_found = account
                                    break
                        except:
                            pass
                    
                    # Method 5: Check for logged-in state indicators
                    if not account_found:
                        try:
                            # Look for common logged-in state indicators
                            logged_in_indicators = [
                                "//div[contains(@aria-label, 'Account')]",
                                "//div[contains(@aria-label, 'Profile')]",
                                "//div[contains(@class, 'profile')]",
                                "//div[contains(@class, 'avatar')]",
                                "//img[contains(@alt, 'Profile')]",
                                "//div[contains(@class, 'user')]"
                            ]
                            
                            for indicator in logged_in_indicators:
                                try:
                                    elements = driver.find_elements(By.XPATH, indicator)
                                    if elements:
                                        # If we find logged-in indicators, try to extract account from page
                                        page_text = driver.find_element(By.TAG_NAME, "body").text
                                        for account in accounts:
                                            if account in page_text:
                                                account_found = account
                                                break
                                        if account_found:
                                            break
                                except:
                                    continue
                        except:
                            pass
                    
                    if account_found:
                        # Verify the account is actually logged in and ready
                        try:
                            # Check if we can access account-specific pages
                            test_url = "https://myaccount.google.com/?hl=en"
                            driver.get(test_url)
                            time.sleep(2)
                            
                            # If we're redirected to login, this account is not properly logged in
                            if "accounts.google.com/signin" in driver.current_url:
                                logging.warning(f"Account {account_found} is not properly logged in, skipping")
                                continue
                            
                            driver_account_map[driver] = account_found
                            logging.info(f"✅ Successfully associated driver with logged-in account: {account_found}")
                        except Exception as verify_e:
                            logging.warning(f"Could not verify login status for {account_found}: {verify_e}")
                            continue
                    else:
                        logging.warning(f"Could not associate driver with any account")
                        
                except Exception as e:
                    logging.warning(f"Error checking driver: {e}")
                    continue
            
            logging.info(f"Successfully mapped {len(driver_account_map)} existing browser windows to accounts")
            return driver_account_map
            
        except Exception as e:
            logging.error(f"Error detecting existing browser windows: {e}")
            return {}
    
    def init_driver_instance(self, download_dir=None):
        """Create driver instance with concurrency protection"""
        max_retries = 3
        base_delay = 2
        
        for attempt in range(max_retries):
            try:
                # Add random delay to spread out initialization attempts
                if attempt > 0:
                    delay = base_delay * (attempt + 1) + random.uniform(0, 2)
                    logging.info(f"Retry {attempt + 1}/{max_retries} after {delay:.1f}s delay")
                    time.sleep(delay)
                
                # Use lock to prevent concurrent driver creation
                with self.driver_lock:
                    logging.info(f"Attempting to create driver instance (attempt {attempt + 1})")
                    
                    chrome_options = webdriver.ChromeOptions()
                    chrome_options.add_argument("--disable-search-engine-choice-screen")
                    chrome_options.add_argument("--disable-dev-shm-usage")
                    chrome_options.add_argument("--no-sandbox")
                    chrome_options.add_argument("--disable-blink-features=AutomationControlled")

                    # Add unique user data directory for each instance
                    unique_user_data = tempfile.mkdtemp(prefix="chrome_user_data_")
                    chrome_options.add_argument(f"--user-data-dir={unique_user_data}")
                    
                    # Configure download directory if provided
                    if download_dir:
                        prefs = {
                            "download.default_directory": download_dir,
                            "download.prompt_for_download": False,
                            "download.directory_upgrade": True,
                            "safebrowsing.enabled": True
                        }
                        chrome_options.add_experimental_option("prefs", prefs)
                        logging.info(f"Chrome configured to download to: {download_dir}")

                    if hasattr(self, 'headless_checkbox') and self.headless_checkbox.isChecked():
                        chrome_options.add_argument("--headless")
                        chrome_options.add_argument("--window-size=1920,1080")
                    
                    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    chrome_options.add_argument(f"user-agent={user_agent}")
                    
                    # Create driver with version_main parameter to avoid conflicts
                    driver = uc.Chrome(
                        options=chrome_options,
                        version_main=None,  # Let UC auto-detect
                        driver_executable_path=None  # Let UC handle path
                    )
                    
                    inject_randomized_javascript(driver)
                    logging.info("Driver instance created successfully")
                    return driver
                    
            except Exception as e:
                error_msg = str(e).lower()
                
                if "file already exists" in error_msg:
                    logging.warning(f"Driver creation conflict on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        continue  # Retry with delay
                elif "target window already closed" in error_msg:
                    logging.error(f"Window closed during creation: {e}")
                    return None
                else:
                    logging.error(f"Driver creation failed on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        continue  # Retry for other errors too
                
        logging.error(f"Failed to create driver after {max_retries} attempts")
        return None
        
    def initUI(self):
        main_layout = QVBoxLayout()
        
        # Define button style for consistent UI
        button_style = """
        QPushButton {
            background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #10002b, stop:1 #3c096c);
            color: white;
            border: 1px solid #ffecd1;
            border-radius: 5px;
            padding: 5px;
        }
        QPushButton:hover {
            background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #f7b801, stop:1 #f26419);
        }
        QPushButton:pressed {
            background-color: #ffecd1;
        }
        """
    
        # Single-account fields
        email_layout = QHBoxLayout()
        email_label = QLabel("Email:")
        email_label.setFont(QFont("Arial", 12))
        self.email_entry = QLineEdit()
        email_layout.addWidget(email_label)
        email_layout.addWidget(self.email_entry)
        main_layout.addLayout(email_layout)
    
        password_layout = QHBoxLayout()
        password_label = QLabel("Password:")
        password_label.setFont(QFont("Arial", 12))
        self.password_entry = QLineEdit()
        self.password_entry.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(password_label)
        password_layout.addWidget(self.password_entry)
        main_layout.addLayout(password_layout)
        
        # Headless mode checkbox
        headless_layout = QHBoxLayout()
        self.headless_checkbox = QCheckBox("Enable Headless Mode")
        headless_layout.addWidget(self.headless_checkbox)
        main_layout.addLayout(headless_layout)
        
        # Multi-account fields (bulk input)
        accounts_layout = QVBoxLayout()
        accounts_label = QLabel("Accounts (one per line, format: username,password or username:password):")
        accounts_label.setFont(QFont("Arial", 12))
        self.accounts_text = QTextEdit()
        accounts_layout.addWidget(accounts_label)
        accounts_layout.addWidget(self.accounts_text)
        main_layout.addLayout(accounts_layout)
        
        # Enhanced Account Management Section
        account_management_group = QGroupBox("Enhanced Account Management")
        account_management_group.setFont(QFont("Arial", 12, QFont.Bold))
        account_management_layout = QVBoxLayout()
        
        # Remote Account Retrieval Section
        remote_retrieval_layout = QHBoxLayout()
        self.retrieve_accounts_button = QPushButton("🔄 Retrieve Accounts")
        self.retrieve_accounts_button.setStyleSheet(button_style)
        self.retrieve_accounts_button.clicked.connect(self.retrieve_remote_accounts)
        remote_retrieval_layout.addWidget(self.retrieve_accounts_button)
        
        # Account Management Buttons
        account_buttons_layout = QHBoxLayout()
        self.delete_selected_button = QPushButton("🗑️ Delete Selected")
        self.delete_selected_button.setStyleSheet(button_style)
        self.delete_selected_button.clicked.connect(self.delete_selected_accounts)
        
        self.login_selected_button = QPushButton("🔑 Login (Selected)")
        self.login_selected_button.setStyleSheet(button_style)
        self.login_selected_button.clicked.connect(self.login_selected_account)
        
        self.login_multiple_enhanced_button = QPushButton("🚀 Add Bulk Sub")
        self.login_multiple_enhanced_button.setStyleSheet(button_style)
        self.login_multiple_enhanced_button.clicked.connect(self.login_multiple_enhanced)
        
        account_buttons_layout.addWidget(self.delete_selected_button)
        account_buttons_layout.addWidget(self.login_selected_button)
        account_buttons_layout.addWidget(self.login_multiple_enhanced_button)
        
        account_management_layout.addLayout(remote_retrieval_layout)
        account_management_layout.addLayout(account_buttons_layout)
        account_management_group.setLayout(account_management_layout)
        main_layout.addWidget(account_management_group)
        
        concurrent_layout = QHBoxLayout()
        concurrent_label = QLabel("Number of concurrent accounts:")
        concurrent_label.setFont(QFont("Arial", 12))
        self.concurrent_accounts_entry = QLineEdit()
        self.concurrent_accounts_entry.setPlaceholderText("Enter number")
        concurrent_layout.addWidget(concurrent_label)
        concurrent_layout.addWidget(self.concurrent_accounts_entry)
        main_layout.addLayout(concurrent_layout)
        
        # Loop Process checkbox
        loop_layout = QHBoxLayout()
        self.loop_checkbox = QCheckBox("Loop Process")
        loop_layout.addWidget(self.loop_checkbox)
        main_layout.addLayout(loop_layout)
        
        # Aliases input
        aliases_layout = QVBoxLayout()
        aliases_label = QLabel("Aliases (one per line):")
        aliases_label.setFont(QFont("Arial", 12))
        self.aliases_text = QTextEdit()
        aliases_layout.addWidget(aliases_label)
        aliases_layout.addWidget(self.aliases_text)
        main_layout.addLayout(aliases_layout)
        
        # Subdomain Generation fields
        subdomain_count_layout = QHBoxLayout()
        subdomain_count_label = QLabel("Number of Subdomains:")
        subdomain_count_label.setFont(QFont("Arial", 12))
        self.subdomain_count_entry = QLineEdit()
        self.subdomain_count_entry.setPlaceholderText("Enter number")
        subdomain_count_layout.addWidget(subdomain_count_label)
        subdomain_count_layout.addWidget(self.subdomain_count_entry)
        main_layout.addLayout(subdomain_count_layout)
        
        # Subdomain Alphabet Length field
        subdomain_alphabet_layout = QHBoxLayout()
        subdomain_alphabet_label = QLabel("Subdomain Alphabet Length:")
        subdomain_alphabet_label.setFont(QFont("Arial", 12))
        self.subdomain_alphabet_entry = QLineEdit()
        self.subdomain_alphabet_entry.setPlaceholderText("Enter length (e.g., 10)")
        self.subdomain_alphabet_entry.setText("10")  # Default value
        subdomain_alphabet_layout.addWidget(subdomain_alphabet_label)
        subdomain_alphabet_layout.addWidget(self.subdomain_alphabet_entry)
        main_layout.addLayout(subdomain_alphabet_layout)
        
        self.generate_subdomains_button = QPushButton("Generate Subdomains")
        button_style = """
        QPushButton {
            background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #10002b, stop:1 #3c096c);
            color: white;
            border: 1px solid #ffecd1;
            border-radius: 5px;
            padding: 5px;
        }
        QPushButton:hover {
            background-color: qlineargradient(spread:pad, x1:0, y1:0, x2:1, y2:0, stop:0 #f7b801, stop:1 #f26419);
        }
        QPushButton:pressed {
            background-color: #ffecd1;
        }
        """
        self.generate_subdomains_button.setStyleSheet(button_style)
        self.generate_subdomains_button.clicked.connect(self.generate_subdomains)
        main_layout.addWidget(self.generate_subdomains_button)
        
        subdomains_layout = QVBoxLayout()
        subdomains_label = QLabel("Generated Subdomains:")
        subdomains_label.setFont(QFont("Arial", 12))
        self.subdomains_text = QTextEdit()
        subdomains_layout.addWidget(subdomains_label)
        subdomains_layout.addWidget(self.subdomains_text)
        main_layout.addLayout(subdomains_layout)
        
        # Buttons for single-account operations
        button_layout = QHBoxLayout()
        self.login_button = QPushButton("Login")
        self.login_button.clicked.connect(self.login)
        self.add_aliases_button = QPushButton("Add Aliases")
        self.add_aliases_button.clicked.connect(self.add_aliases)
        self.activate_gmail_button = QPushButton("Activate Gmail")
        self.activate_gmail_button.clicked.connect(self.activate_gmail)
        self.setup_authenticator_button = QPushButton("Setup Authenticator")
        self.setup_authenticator_button.clicked.connect(self.setup_authenticator)
        self.enable_2sv_button = QPushButton("Enable Two-Step Verification")
        self.enable_2sv_button.clicked.connect(self.enable_two_step_verification)
        self.generate_app_password_button = QPushButton("Generate App Password")
        self.generate_app_password_button.clicked.connect(self.generate_app_password)
        self.login_button.setStyleSheet(button_style)
        self.add_aliases_button.setStyleSheet(button_style)
        self.activate_gmail_button.setStyleSheet(button_style)
        self.setup_authenticator_button.setStyleSheet(button_style)
        self.enable_2sv_button.setStyleSheet(button_style)
        self.generate_app_password_button.setStyleSheet(button_style)
        button_layout.addWidget(self.login_button)
        button_layout.addWidget(self.add_aliases_button)
        button_layout.addWidget(self.activate_gmail_button)
        button_layout.addWidget(self.setup_authenticator_button)
        button_layout.addWidget(self.enable_2sv_button)
        button_layout.addWidget(self.generate_app_password_button)
        main_layout.addLayout(button_layout)
        
        self.enable_admin_sdk_button = QPushButton("Enable Admin SDK (Single Account)")
        self.enable_admin_sdk_button.setStyleSheet(button_style)
        self.enable_admin_sdk_button.clicked.connect(self.enable_admin_sdk)
        main_layout.addWidget(self.enable_admin_sdk_button)
        
        self.login_multi_button = QPushButton("Login Multiple Accounts")
        self.login_multi_button.setStyleSheet(button_style)
        self.login_multi_button.clicked.connect(self.login_multiple_accounts)
        # Add this button to your layout (e.g. the same row as "Process Multiple Accounts")
        main_layout.addWidget(self.login_multi_button)
                
        # New button: Process Multiple Accounts (login + full Admin SDK enabling)
        self.process_multi_button = QPushButton("Process Multiple Accounts")
        self.process_multi_button.setStyleSheet(button_style)
        self.process_multi_button.clicked.connect(self.process_multiple_accounts)
        main_layout.addWidget(self.process_multi_button)
        
        # New button: Process Multiple Accounts WITHOUT App Password (Login -> Authenticator -> 2-Step)
        self.process_multi_no_app_password_button = QPushButton("Run All Steps (No App Password)")
        self.process_multi_no_app_password_button.setStyleSheet(button_style)
        self.process_multi_no_app_password_button.clicked.connect(self.process_multiple_accounts_without_app_password)
        self.process_multi_no_app_password_button.setToolTip("Run: Login -> Setup Authenticator -> Enable 2-Step (NO App Password generation)")
        main_layout.addWidget(self.process_multi_no_app_password_button)
    
        self.setLayout(main_layout)
        self.setWindowTitle("Google Workspace Automation")
        self.setGeometry(100, 100, 800, 600)
        self.show()

        # Add this after your existing buttons
        self.playwright_recorder_button = QPushButton("🎬 Playwright Recorder")
        self.playwright_recorder_button.setStyleSheet(button_style)
        self.playwright_recorder_button.clicked.connect(self.open_playwright_recorder)
        main_layout.addWidget(self.playwright_recorder_button)

        # In your initUI function, add this button:
        self.test_sftp_button = QPushButton("Test SFTP Folder Creation")
        self.test_sftp_button.setStyleSheet(button_style)
        self.test_sftp_button.clicked.connect(self.test_sftp_folder_creation)
        # main_layout.addWidget(self.test_sftp_button)
        
        # Auto-retrieve accounts at startup
        self.auto_retrieve_accounts_at_startup()

    def safe_navigate(self, driver, url):
        """Safely navigate to URL with error handling"""
        try:
            driver.get(url)
            logging.info(f"Successfully navigated to {url}")
            return True
        except Exception as e:
            if not self.handle_driver_error(e):
                logging.error(f"Failed to navigate to {url}: {e}")
            return False

    def handle_driver_error(self, error):
        """Handle driver errors gracefully without closing the app"""
        error_message = str(error).lower()
        
        if any(keyword in error_message for keyword in ["session deleted", "disconnected", "not connected", "chrome not reachable", "target window already closed", "web view not found", "no such window"]):
            # Browser was closed manually or crashed - don't show error, just reset
            logging.info("Browser connection lost. Driver reset. You can try logging in again.")
            self.driver = None
            # Use thread-safe error handler
            self.error_handler.request_error_handling("browser_closed", "")
            return True  # Handled gracefully
        else:
            # Real error - show to user but don't crash
            # Use thread-safe error handler
            self.error_handler.request_error_handling("general_error", str(error))
            logging.error(f"An error occurred: {error}")
            self.driver = None
            return False

    def add_random_delay(self, min_seconds=0, max_seconds=1):
        delay = random.uniform(min_seconds, max_seconds)
        logging.info(f"Waiting for {delay:.2f} seconds.")
        time.sleep(delay)
        
    def random_scroll_and_mouse_move(self):
        for _ in range(2):
            scroll_x = random.randint(0, 100)
            scroll_y = random.randint(0, 100)
            self.driver.execute_script(f"window.scrollBy({scroll_x}, {scroll_y});")
            self.add_random_delay()
            self.driver.execute_script("document.dispatchEvent(new MouseEvent('mousemove', {clientX: 100, clientY: 100}));")
    
    def locate_dynamic_element(self, driver, base_xpath, div_range=(9, 12), timeout=5):
        for div_index in range(div_range[0], div_range[1] + 1):
            try:
                xpath = base_xpath.format(div_index=div_index)
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                return element
            except TimeoutException:
                logging.warning(f"Element not found using XPath with div[{div_index}].")
                continue
        raise TimeoutException(f"Element not found within range {div_range}")
    
    def generate_subdomains(self):
        email = self.email_entry.text().strip()
        if "@" not in email:
            QMessageBox.warning(self, "Invalid Email", "Please enter a valid email address.")
            return
        try:
            count = int(self.subdomain_count_entry.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid Number", "Please enter a valid number for subdomains.")
            return
        domain = email.split("@")[-1]
        niche_words = [
            "technology",
            "healthcare",
            "entertainment",
            "sustainability",
            "infrastructure",
            "environment",
            "entrepreneur",
            "administrative",
            "communication",
            "innovation"
        ]
        generated = []
        for i in range(1, count + 1):
            base_word = niche_words[(i - 1) % len(niche_words)]
            suffix = ""
            if i > len(niche_words):
                suffix = str((i - 1) // len(niche_words) + 1)
            subdomain = f"{base_word}{suffix}.{domain}"
            generated.append(subdomain)
        self.subdomains_text.setPlainText("\n".join(generated))
        logging.info(f"Generated {count} subdomains for domain {domain}: {generated}")
    
    def generate_subdomains_with_alphabet(self, domain, count, alphabet_length):
        """Generate subdomains with specified alphabet length"""
        try:
            # Generate random subdomains with specified alphabet length
            generated = []
            for i in range(count):
                # Generate random string with specified length
                subdomain_name = ''.join(random.choices(string.ascii_lowercase, k=alphabet_length))
                subdomain = f"{subdomain_name}.{domain}"
                generated.append(subdomain)
            
            logging.info(f"Generated {count} subdomains with {alphabet_length} character alphabet for domain {domain}")
            return generated
            
        except Exception as e:
            logging.error(f"Error generating subdomains with alphabet: {e}")
            return []
    
    def add_subdomains_to_account(self, driver, email, subdomains):
        """Add subdomains to a specific account"""
        try:
            logging.info(f"Starting to add {len(subdomains)} subdomains to {email}")
            
            # Navigate to domain management page
            driver.get("https://admin.google.com/ac/domains/manage?hl=en")
            time.sleep(3)
            
            added_count = 0
            for i, subdomain in enumerate(subdomains, 1):
                try:
                    logging.info(f"Adding subdomain {i}/{len(subdomains)}: {subdomain}")
                    
                    # Multi-language Add button selectors
                    add_selectors = [
                        "//div[@jsname='w4Susd']",                    # Most reliable
                        "//div[@data-action-id='w4Susd']",           # Data attribute
                        "//div[@jscontroller='VXdfxd']",             # Controller
                        "//div[contains(@class, 'U26fgb') and contains(@class, 'O0WRkf')]",  # Classes
                        "//div[@role='button' and contains(@data-tooltip, 'omain')]",  # Tooltip backup
                        "//div[@aria-label='Een domein toevoegen']", # Original Dutch (fallback)
                    ]
                    
                    add_button = None
                    for selector in add_selectors:
                        try:
                            add_button = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            break
                        except:
                            continue
                    
                    if not add_button:
                        logging.error(f"Could not find add domain button for {subdomain}")
                        continue
                        
                    add_button.click()
                    time.sleep(random.uniform(3, 5))
                    
                    # Multi-language domain input selectors
                    input_selectors = [
                        "//input[@jsname='YPqjbf']",                 # Original
                        "//input[@type='text']",                    # Generic text input
                        "//input[contains(@placeholder, 'omain')]", # Placeholder with domain
                        "//input[contains(@name, 'domain')]",       # Name attribute
                        "//input[contains(@aria-label, 'omain')]",  # Aria label
                        "//div[contains(@class, 'dialog')]//input[@type='text']", # Dialog input
                    ]
                    
                    domain_input = None
                    for selector in input_selectors:
                        try:
                            domain_input = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            break
                        except:
                            continue
                    
                    if not domain_input:
                        logging.error(f"Could not find domain input field for {subdomain}")
                        continue
                    
                    # Human-like typing
                    domain_input.clear()
                    time.sleep(random.uniform(0.5, 1))
                    domain_input.send_keys(subdomain)
                    
                    # Multi-language submit button selectors
                    submit_selectors = [
                        "//button[@jsname='cRy3zd']",               # Original
                        "//button[@type='submit']",                # Submit type
                        "//button[contains(@class, 'submit')]",    # Submit class
                        "//button[contains(text(), 'Add') or contains(text(), 'Hinzufügen') or contains(text(), 'Toevoegen')]", # Multi-language text
                        "//div[contains(@class, 'dialog')]//button[last()]", # Last button in dialog
                        "//button[contains(@class, 'VfPpkd-LgbsSe')]", # Common Google button class
                    ]
                    
                    submit_button = None
                    for selector in submit_selectors:
                        try:
                            submit_button = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            break
                        except:
                            continue
                    
                    if not submit_button:
                        logging.error(f"Could not find submit button for {subdomain}")
                        continue
                        
                    submit_button.click()
                    logging.info(f"Successfully added subdomain: {subdomain}")
                    added_count += 1
                    
                    # Wait for processing
                    time.sleep(random.uniform(5, 8))
                    
                    # Navigate back to domains page for next subdomain
                    driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                    time.sleep(random.uniform(3, 5))
                    
                except Exception as e:
                    logging.error(f"Error adding subdomain '{subdomain}': {e}")
                    # Navigate back to domains page even if there was an error
                    try:
                        driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                        time.sleep(random.uniform(3, 5))
                    except:
                        pass
                    continue
                    
            logging.info(f"Finished adding subdomains to {email}. Successfully added {added_count}/{len(subdomains)} subdomains.")
            return added_count
            
        except Exception as e:
            logging.error(f"Error in add_subdomains_to_account for {email}: {e}")
            return 0
    
    # --- Existing functions for authenticator, aliases, activation, etc. remain unchanged ---
    def setup_authenticator(self):
        """Setup authenticator for single account or multiple accounts based on account field content"""
        try:
            # Check if we have multiple accounts in the field
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            if len(accounts_data) > 1:
                # Multiple accounts detected, use bulk operation
                logging.info(f"Multiple accounts detected ({len(accounts_data)}), starting bulk Setup Authenticator")
                self.setup_authenticator_bulk()
                return
            
            # Single account operation (original logic)
            email = self.email_entry.text().strip()
            if not email:
                # Get from accounts text area
                if accounts_data and accounts_data[0].strip():
                    line = accounts_data[0].strip()
                    if ',' in line:
                        email, _ = line.split(',', 1)
                    elif ':' in line:
                        email, _ = line.split(':', 1)
                    email = email.strip()

            logging.info(f"DEBUG: Using email for authenticator: '{email}'")
            logging.info(f"Navigating to 2FA setup page for {email}...")
            self.driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
            self.random_scroll_and_mouse_move()
            if self.is_authenticator_set_up():
                logging.info(f"Skipping Authenticator setup for {email} as it's already set up.")
                return True
            try:
                self.click_setup_authenticator_button()
                self.click_cant_scan_link()
                self.add_random_delay()
                secret_key = self.extract_and_save_secret_key()
                self.click_turn_on_button_single(driver)
                if secret_key:
                    logging.info(f"Secret key saved successfully: {secret_key}")
                    self.click_continue_button()
                    verified = self.enter_and_verify_totp_code()
                    if verified:
                        logging.info(f"Authenticator setup completed for {email}")
                        return True
                    else:
                        self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "TOTP Verification Failed")
                        return False
                else:
                    self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "Failed to extract secret key")
                    return False
            except TimeoutException as e:
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
                logging.error(f"Timeout during authenticator setup for {email}: {e}")
                return False
            except Exception as e:
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
                logging.error(f"Exception during authenticator setup for {email}: {e}")
                return False
                
        except Exception as e:
            logging.error(f"Error in setup_authenticator: {e}")
            return False

    def setup_authenticator_bulk(self):
        """Setup authenticator for multiple accounts with concurrency"""
        try:
            # Get accounts from the enhanced account field
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            accounts = []
            
            for line in accounts_data:
                line = line.strip()
                if not line:
                    continue
                    
                user = None
                pwd = None
                
                if ':' in line:
                    user, pwd = line.split(':', 1)
                elif ',' in line:
                    user, pwd = line.split(',', 1)
                else:
                    # Only email provided, try to fetch password from server
                    user = line.strip()
                    logging.info(f"🔍 Only email provided: {user}, attempting to fetch password from server")
                    pwd = self.get_account_password(user)
                    if not pwd:
                        logging.warning(f"🔍 Password not found for {user}, skipping this account")
                        continue
                    
                user = user.strip()
                pwd = pwd.strip()
                
                if user and pwd:
                    accounts.append((user, pwd))
                    
            if not accounts:
                QMessageBox.warning(self, "No Valid Accounts", 
                    "No valid accounts found. Please ensure accounts are in format 'email:password' or 'email' (if password exists on server).")
                return
                
            # Get concurrency limit from the existing spinbox
            try:
                concurrent_limit = int(self.concurrent_accounts_entry.text().strip())
                if concurrent_limit <= 0:
                    concurrent_limit = 1
            except ValueError:
                concurrent_limit = 1
                
            logging.info(f"Starting bulk Setup Authenticator with {len(accounts)} accounts, max {concurrent_limit} concurrent")
            
            # Use the bulk operation mechanism
            self.setup_authenticator_bulk_enhanced(accounts, concurrent_limit)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error in bulk Setup Authenticator: {str(e)}")
            logging.error(f"Error in setup_authenticator_bulk: {e}")
            
    def setup_authenticator_bulk_enhanced(self, accounts, concurrent_limit):
        """Enhanced version of bulk Setup Authenticator with proper window arrangement"""
        try:
            # Enable global OTP detection
            self.start_global_otp_detection()
            
            # Debug: Log initial state of active_drivers
            with self.driver_lock:
                logging.info(f"🔍 SETUP AUTHENTICATOR - Initial active_drivers count: {len(self.active_drivers)}")
                for i, driver in enumerate(self.active_drivers):
                    try:
                        logging.info(f"   Driver {i}: URL={driver.current_url}, Title={driver.title}")
                    except:
                        logging.info(f"   Driver {i}: [Error getting driver info]")
            
            screen_width = 1920
            screen_height = 1080
            # Use concurrent_limit for window arrangement
            positions = self.get_grid_positions(concurrent_limit, screen_width, screen_height)
            
            # Helper function for enhanced Setup Authenticator
            def setup_authenticator_account_enhanced(account, password, position, app_instance):
                driver = None
                operation_successful = False
                original_driver = None
                try:
                    # Create a new driver instance for the account
                    driver = app_instance.init_driver_instance()
                    if driver is None:
                        logging.error(f"Failed to create driver for {account}")
                        return
                    
                    # Add driver to active drivers list for OTP tracking
                    with app_instance.driver_lock:
                        app_instance.active_drivers.append(driver)
                        # Track driver-to-account mapping
                        app_instance.driver_to_account[driver] = account
                        app_instance.account_to_driver[account] = driver
                        
                    # Set the window geometry based on the computed position
                    driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                    
                    # Perform login first
                    logging.info(f"Starting login for {account} before Setup Authenticator")
                    app_instance.perform_login_enhanced(driver, account, password)
                    
                    # Wait for login to complete and page to load
                    time.sleep(5)
                    
                    # Wait for page to be stable before proceeding with operations
                    try:
                        WebDriverWait(driver, 10).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                        logging.info(f"Page loaded completely for {account}")
                    except:
                        logging.warning(f"Page load timeout for {account}, continuing anyway")
                    
                    # Temporarily set self.driver to the current account's driver for the original function
                    original_driver = app_instance.driver
                    app_instance.driver = driver
                    
                    # Set the email and password in the UI fields for the original function to use
                    original_email = app_instance.email_entry.text()
                    original_password = app_instance.password_entry.text()
                    app_instance.email_entry.setText(account)
                    app_instance.password_entry.setText(password)
                    
                    # Run Setup Authenticator using original function
                    logging.info(f"Starting Setup Authenticator for {account}")
                    try:
                        # Wait for page to be ready before starting authenticator setup
                        time.sleep(3)
                        authenticator_success = app_instance.setup_authenticator_single(driver, account)
                        if authenticator_success:
                            logging.info(f"Setup Authenticator completed successfully for {account}")
                            operation_successful = True
                        else:
                            logging.warning(f"Setup Authenticator failed for {account}")
                    except Exception as auth_e:
                        logging.error(f"Error in Setup Authenticator for {account}: {auth_e}")
                        authenticator_success = False
                    
                    # Restore original driver and UI fields
                    app_instance.driver = original_driver
                    app_instance.email_entry.setText(original_email)
                    app_instance.password_entry.setText(original_password)
                    
                    # Keep the browser open for user inspection
                    logging.info(f"Setup Authenticator completed for {account}. Browser window remains open for inspection.")
                    
                except Exception as e:
                    # Restore original driver and UI fields even on error
                    if original_driver is not None:
                        app_instance.driver = original_driver
                    if 'original_email' in locals():
                        app_instance.email_entry.setText(original_email)
                    if 'original_password' in locals():
                        app_instance.password_entry.setText(original_password)
                    
                    if not app_instance.handle_driver_error(e):
                        logging.error(f"Error running Setup Authenticator for account {account}: {e}")
                finally:
                    # Keep the browser open for successful operations, only close on failure
                    if not operation_successful and driver:
                        try:
                            driver.quit()
                            with app_instance.driver_lock:
                                if driver in app_instance.active_drivers:
                                    app_instance.active_drivers.remove(driver)
                                # Remove driver-to-account mapping
                                if driver in app_instance.driver_to_account:
                                    account_to_remove = app_instance.driver_to_account[driver]
                                    del app_instance.driver_to_account[driver]
                                    if account_to_remove in app_instance.account_to_driver:
                                        del app_instance.account_to_driver[account_to_remove]
                            logging.info(f"Browser closed for {account} due to operation failure.")
                        except Exception as quit_e:
                            logging.error(f"Error closing browser for {account}: {quit_e}")
                    elif operation_successful and driver:
                        # Keep the browser open for successful operations
                        logging.info(f"✅ Keeping browser open for {account} (operation successful)")
                        # Ensure driver is in active_drivers list
                        with app_instance.driver_lock:
                            if driver not in app_instance.active_drivers:
                                app_instance.active_drivers.append(driver)
                                logging.info(f"Added driver back to active_drivers for {account}")
                            else:
                                logging.info(f"Driver already in active_drivers for {account}")
                        # Log current active_drivers count
                        with app_instance.driver_lock:
                            logging.info(f"🔍 After keeping driver for {account}: active_drivers count = {len(app_instance.active_drivers)}")
            
            # Process accounts concurrently using a thread pool with concurrency limit
            with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
                futures = []
                for i, (account, pwd) in enumerate(accounts):
                    # Use modulo to cycle through positions based on concurrent_limit
                    pos_index = i % concurrent_limit
                    pos = positions[pos_index] if pos_index < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                    futures.append(executor.submit(setup_authenticator_account_enhanced, account, pwd, pos, self))
                # Wait for all accounts to complete processing
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(f"Thread execution error: {e}")
            
            # Disable global OTP detection
            self.stop_global_otp_detection()
            
            # Debug: Log state after stopping OTP detection
            with self.driver_lock:
                logging.info(f"🔍 SETUP AUTHENTICATOR - After stopping OTP detection: active_drivers count: {len(self.active_drivers)}")
                for i, driver in enumerate(self.active_drivers):
                    try:
                        logging.info(f"   Driver {i}: URL={driver.current_url}, Title={driver.title}")
                    except:
                        logging.info(f"   Driver {i}: [Error getting driver info]")
            
            # Debug: Log final state of active_drivers
            with self.driver_lock:
                logging.info(f"🔍 SETUP AUTHENTICATOR - Final active_drivers count: {len(self.active_drivers)}")
                for i, driver in enumerate(self.active_drivers):
                    try:
                        logging.info(f"   Driver {i}: URL={driver.current_url}, Title={driver.title}")
                    except:
                        logging.info(f"   Driver {i}: [Error getting driver info]")
            
            # Automatically continue with Enable Two-Step Verification
            logging.info("Automatically continuing with Enable Two-Step Verification")
            # Call the enable two-step verification function with the same accounts
            self.enable_two_step_verification_bulk_enhanced(accounts, concurrent_limit)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error in enhanced bulk Setup Authenticator: {str(e)}")
            logging.error(f"Error in setup_authenticator_bulk_enhanced: {e}")
            # Disable global OTP detection on error
            self.stop_global_otp_detection()
            
    def setup_authenticator_single(self, driver, email):
        """Setup authenticator for a single account using provided driver"""
        try:
            logging.info(f"DEBUG: Using email for authenticator: '{email}'")
            logging.info(f"Navigating to 2FA setup page for {email}...")
            driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
            # Use driver parameter instead of self.driver
            for _ in range(2):
                scroll_x = random.randint(0, 100)
                scroll_y = random.randint(0, 100)
                driver.execute_script(f"window.scrollBy({scroll_x}, {scroll_y});")
                self.add_random_delay()
                driver.execute_script("document.dispatchEvent(new MouseEvent('mousemove', {clientX: 100, clientY: 100}));")
            if self.is_authenticator_set_up_single(driver):
                logging.info(f"Skipping Authenticator setup for {email} as it's already set up.")
                return True
            try:
                self.click_setup_authenticator_button_single(driver)
                self.click_cant_scan_link_single(driver)
                self.add_random_delay()
                secret_key = self.extract_and_save_secret_key_single(driver, email)
                if secret_key:
                    logging.info(f"Secret key saved successfully: {secret_key}")
                    self.click_continue_button_single(driver)
                    verified = self.enter_and_verify_totp_code_single(driver, email)
                    if verified:
                        logging.info(f"Authenticator setup completed for {email}")
                        return True
                    else:
                        self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "TOTP Verification Failed")
                        return False
                else:
                    self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", "Failed to extract secret key")
                    return False
            except TimeoutException as e:
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
                logging.error(f"Timeout during authenticator setup for {email}: {e}")
                return False
            except Exception as e:
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, email, "Authenticator Setup", str(e))
                logging.error(f"Exception during authenticator setup for {email}: {e}")
                return False
            self.click_turn_on_button_single(driver)   
        except Exception as e:
            logging.error(f"Error in setup_authenticator_single: {e}")
            return False
    def is_authenticator_set_up_single(self, driver):
        logging.info("Checking if Authenticator is set up...")
        try:
            setup_button = WebDriverWait(driver, 5).until(
                EC.visibility_of_element_located((By.XPATH, '/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div[2]/div/div/div/button'))
            )
            logging.info("Authenticator setup is required.")
            return False
        except TimeoutException:
            logging.info("Authenticator is already set up, skipping setup.")
            return True
            
    def click_setup_authenticator_button_single(self, driver):
        try:
            setup_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div[2]/div/div/div/button"))
            )
            driver.execute_script("arguments[0].click();", setup_button)
            logging.info("Clicked on 'Set up authenticator' button.")
        except TimeoutException as e:
            logging.error(f"Timeout while clicking 'Set up authenticator' button: {e}")
            raise
            
    def click_cant_scan_link_single(self, driver):
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
                xpath_patterns.append(f"/html/body/div[11]/div/div[2]/span/div/div/div/div[2]/center/div/div/button")
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
    def extract_and_save_secret_key_single(self, driver, account_name):
        try:
            # Extract secret key logic (keep existing extraction code)
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            possible_xpaths = [
				"/html/body/div[11]/div/div[2]/span/div/div/ol/li[2]/div/strong",
                "/html/body/div[10]/div/div[2]/span/div/div/ol/li[2]/div/strong",
                "//div[contains(text(), 'Secret key')]/following-sibling::div//strong",
                "//ol/li[2]/div/strong",
                "//div[@id='totp-key']/div/strong",
                "//div[@id='totp-key']/strong",
                "//div[contains(@class, 'secret-key')]/strong",
                "//div[contains(text(), 'Key')]/following-sibling::div//strong",
                "//strong[contains(text(), ' ')]",
            ]
            secret_key = None
            for xpath in possible_xpaths:
                try:
                    secret_key_element = driver.find_element(By.XPATH, xpath)
                    secret_key_candidate = secret_key_element.text.strip()
                    if self.is_valid_secret_key(secret_key_candidate):
                        secret_key = secret_key_candidate
                        logging.info(f"Found secret key using XPath: {xpath}")
                        break
                except NoSuchElementException:
                    logging.warning(f"Secret key not found using XPath: {xpath}")
                    continue
            
            if not secret_key:
                raise ValueError("Secret key not found.")
            
            # Use the provided account_name directly - no guessing needed!
            logging.info(f"DEBUG: Using provided account name: '{account_name}'")

            if not account_name:
                raise ValueError("No account name provided - cannot save secret key")
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
                temp_file.write(secret_key)
                temp_file_path = temp_file.name
            
            # Save locally in profiles directory
            import os
            profiles_dir = os.path.join(os.getcwd(), "profiles")
            account_profile_dir = os.path.join(profiles_dir, account_name)
            
            # Create account profile directory if it doesn't exist
            if not os.path.exists(account_profile_dir):
                os.makedirs(account_profile_dir)
                logging.info(f"Created local profile directory: {account_profile_dir}")
            
            # Save secret key locally
            local_secret_key_path = os.path.join(account_profile_dir, f"{account_name}_authenticator_secret_key.txt")
            with open(local_secret_key_path, 'w') as local_file:
                local_file.write(secret_key)
            logging.info(f"Secret key saved locally: {local_secret_key_path}")
            
            try:
                # Direct SFTP connection and upload
                sftp = self.sftp_connect()
                try:
                    # Create the account directory
                    remote_account_dir = f"{self.remote_dir}{account_name}"
                    self.ensure_remote_directory_exists(sftp, remote_account_dir)
                    
                    # Upload to correct path with account name in filename
                    remote_file_path = f"{remote_account_dir}/{account_name}_authenticator_secret_key.txt"
                    sftp.put(temp_file_path, remote_file_path)
                    logging.info(f"Secret key saved to SFTP: {remote_file_path}")
                    
                finally:
                    sftp.close()
                    
            finally:
                # Always delete temp file
                import os
                os.unlink(temp_file_path)
                
            return secret_key
            
        except Exception as e:
            logging.error(f"Failed to extract and save secret key: {e}")
            return None
            # Single account operation (original logic)
            email = self.email_entry.text()
            logging.info(f"Navigating to 2-Step Verification page for {email}...")
            self.driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
            if self.is_two_step_verification_enabled():
                logging.info(f"Skipping Two-Step Verification for {email} as it's already enabled.")
                return True
            try:
                get_started_button = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[5]/div/button/span[6]"))
                )
                self.driver.execute_script("arguments[0].click();", get_started_button)
                logging.info(f"Clicked on 'Get Started' or 'Turn On' button for {email}.")
                self.handle_reauthentication()
                if self.detect_final_pop_up():
                    logging.info(f"2-Step Verification enabled successfully for {email} by detecting the final pop-up.")
                    return True
                else:
                    logging.error(f"Final pop-up not detected. 2-Step Verification may not have been enabled for {email}.")
                    return False
            except TimeoutException as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Timeout while enabling 2-Step Verification for {email}: {e}")
                return False
            except Exception as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Error during 2-Step Verification setup for {email}: {e}")
                return False
    def click_turn_on_button_single(self, driver):
        try:
            xpath = "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div/div/div[2]/div/div/span[4]"
            turn_on_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            driver.execute_script("arguments[0].scrollIntoView();", turn_on_btn)
            import time
            time.sleep(1)
            driver.execute_script("arguments[0].click();", turn_on_btn)
            logging.info("Clicked Turn ON button.")
            return True
        except Exception as e:
            logging.error(f"Failed to click Turn ON: {e}")
            return False
        
    def click_continue_button_single(self, driver):
        try:
            script = """
            var button = document.querySelector("#yDmH0d > div.oDVwOd.PHZhJd.iWO5td > div > div.GheHHf.GiAE0b.KNyhq.PjYkrd.iWO5td > div.sRKBBe > div > div:nth-child(2) > div:nth-child(2) > button > div.VfPpkd-Jh9lGc");
            if (button) { button.click(); return true; } else { return false; }
            """
            result = driver.execute_script(script)
            if result:
                logging.info("Clicked 'Continue' button using JavaScript.")
                return

            fallback_xpaths = [
                "/html/body/div[11]/div/div[2]/div[3]/div/div[2]/div[2]/button",
                "/html/body/div[10]/div/div[2]/div[3]/div/div[2]/div[2]/button",
                "//div[contains(@class,'sRKBBe')]//button[contains(@class,'VfPpkd-LgbsSe')]",
            ]
            for xp in fallback_xpaths:
                try:
                    btn = WebDriverWait(driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
                    driver.execute_script("arguments[0].click();", btn)
                    logging.info(f"Clicked 'Continue' button using XPath: {xp}")
                    return
                except Exception:
                    continue

            logging.error("Could not find 'Continue' button using JavaScript or XPath fallbacks.")
            raise Exception("Could not find 'Continue' button using JavaScript or XPath fallbacks.")
        except Exception as e:
            logging.error(f"Error while clicking 'Continue' button: {e}")
            raise
            
    def enter_and_verify_totp_code_single(self, driver, account_name):
        try:
            # Use the provided account_name directly - no guessing needed!
            logging.info(f"DEBUG: Using provided account name for OTP: '{account_name}'")

            if not account_name:
                logging.error("No account name provided - cannot generate TOTP code")
                return False
            secret_key_file = self.get_secret_key_file(account_name)
            if not secret_key_file:
                logging.error(f"Secret key file not found for {account_name}. Cannot generate TOTP code.")
                return False
            
            with open(secret_key_file, 'r') as f:
                secret_key = f.read().strip()
            
            totp_code = self.generate_otp_code(secret_key)
            if not totp_code:
                logging.error("Failed to generate TOTP code.")
                return False
            
            logging.info(f"Generated OTP code: {totp_code}")
            
            # STRATEGY 1: Target by specific attributes (most reliable)
            attribute_selectors = [
                # Primary selector using jsname and ID
                "//input[@jsname='YPqjbf' and @id='c0']",
                
                # Alternative using jsname only
                "//input[@jsname='YPqjbf']",
                
                # Target by ID (c0, c1, c2, etc. are common)
                "//input[@id='c0']",
                "//input[@id='c1']", 
                "//input[@id='c2']",
                
                # Target by class pattern
                "//input[contains(@class, 'VfPpkd-fmcmS-wGMbrd')]",
            ]
            
            # STRATEGY 2: Target by placeholder text (language-specific)
            placeholder_selectors = [
                # Dutch
                "//input[@placeholder='Geef de code op']",
                "//input[contains(@placeholder, 'code')]",
                
                # English
                "//input[@placeholder='Enter the code']",
                "//input[contains(@placeholder, 'Enter') and contains(@placeholder, 'code')]",
                
                # General code input patterns
                "//input[contains(@placeholder, 'Code') or contains(@placeholder, 'code')]",
            ]
            
            # STRATEGY 3: Target by input type and context
            context_selectors = [
                # Text input in authenticator dialog
                "//div[contains(@class, 'qPtGzb')]//input[@type='text']",
                
                # Input with autocomplete=off (common for OTP)
                "//input[@autocomplete='off']",
                
                # Input in dialog with specific controller
                "//div[@jscontroller='ieZWvb']//input[@type='text']",
                
                # Input with specific aria controls
                "//input[@aria-controls='c3']",
            ]
            
            # STRATEGY 4: Target by parent structure
            structural_selectors = [
                # Input inside label with specific classes
                "//label[contains(@class, 'VfPpkd-fmcmS-yrriRe')]//input",
                
                # Input in div with skQ8Ge class
                "//div[@class='skQ8Ge']//input",
                
                # Input in specific step container
                "//div[@wizard-step-uid and contains(@wizard-step-uid, 'verifyCode')]//input",
            ]
            
            # STRATEGY 5: Fallback selectors
            fallback_selectors = [
                # Any text input in the dialog
                "//div[@role='dialog']//input[@type='text']",
                
                # Any input that accepts text
                "//input[@type='text' and not(@style*='display: none')]",
                
                # Any visible input field
                "//input[@type='text' and not(@disabled)]",
				
				# Provided absolute fallback
				"/html/body/div[11]/div/div[2]/span/div/div/div/div[2]/div/div/div[1]/span[2]/input",
            ]
            
            # Combine all strategies
            all_selectors = attribute_selectors + placeholder_selectors + context_selectors + structural_selectors + fallback_selectors
            
            otp_input = None
            for i, selector in enumerate(all_selectors):
                try:
                    otp_input = WebDriverWait(driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    logging.info(f"✅ Found OTP input field with selector {i+1}: {selector}")
                    break
                except:
                    logging.info(f"❌ OTP input selector {i+1} failed")
                    continue
            
            if not otp_input:
                logging.error("Could not find OTP input field with any selector")
                return False
            
            # Clear and enter OTP code
            otp_input.clear()
            time.sleep(random.uniform(0.5, 1))
            otp_input.send_keys(totp_code)
            logging.info(f"Entered OTP code: {totp_code}")
            
            # Click verify button
            verify_selectors = [
                # Use the exact HTML you provided
                "//button[@data-id='dtOep' and @jsname='LgbsSe']",
                "//button[@data-id='dtOep']",
                "//span[text()='Verifiëren']//parent::button",
                "//div[@class='sRKBBe']//button[contains(@class, 'VfPpkd-LgbsSe')]",
            ]
            
            try:
                verify_button = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Verifiëren')]"))
                )
                # Scroll into view and click with JavaScript
                driver.execute_script("arguments[0].scrollIntoView(); arguments[0].click();", verify_button)
                logging.info("✅ Clicked verify button with JavaScript")
            except TimeoutException:
                # Fallback: use ENTER key
                otp_input.send_keys(Keys.ENTER)
                logging.info("✅ Used ENTER key to submit")
            
            time.sleep(3)  # Wait for processing

            # Wait for completion and verify success
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div/div[3]/div"))
                )
                logging.info(f"✅ TOTP verification completed successfully for {account_name}.")
                self.click_turn_on_button_single(driver)
                return True
            except TimeoutException as e:
                logging.error(f"❌ Timeout while verifying TOTP code: {e}")
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, account_name, "Authenticator Setup", "TOTP Verification Failed")
                return False
                
        except Exception as e:
            logging.error(f"Error in OTP verification: {e}")
            return False
        
    def click_setup_authenticator_button(self):
        try:
            setup_button = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div[2]/div/div/div/button"))
            )
            self.driver.execute_script("arguments[0].click();", setup_button)
            logging.info("Clicked on 'Set up authenticator' button.")
        except TimeoutException as e:
            logging.error(f"Timeout while clicking 'Set up authenticator' button: {e}")
            raise
    
    def click_cant_scan_link(self):
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
                xpath_patterns.append(f"/html/body/div[11]/div/div[2]/span/div/div/div/div[2]/center/div/div/button")
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

    def click_continue_button(self):
        try:
            script = """
            var button = document.querySelector("#yDmH0d > div.oDVwOd.PHZhJd.iWO5td > div > div.GheHHf.GiAE0b.KNyhq.PjYkrd.iWO5td > div.sRKBBe > div > div:nth-child(2) > div:nth-child(2) > button > div.VfPpkd-Jh9lGc");
            if (button) { button.click(); return true; } else { return false; }
            """
            result = self.driver.execute_script(script)
            if result:
                logging.info("Clicked 'Continue' button using JavaScript.")
                return

            fallback_xpaths = [
                "/html/body/div[11]/div/div[2]/div[3]/div/div[2]/div[2]/button",
                "/html/body/div[10]/div/div[2]/div[3]/div/div[2]/div[2]/button",
                "//div[contains(@class,'sRKBBe')]//button[contains(@class,'VfPpkd-LgbsSe')]",
            ]
            for xp in fallback_xpaths:
                try:
                    btn = WebDriverWait(self.driver, 3).until(EC.element_to_be_clickable((By.XPATH, xp)))
                    self.driver.execute_script("arguments[0].click();", btn)
                    logging.info(f"Clicked 'Continue' button using XPath: {xp}")
                    return
                except Exception:
                    continue

            logging.error("Could not find 'Continue' button using JavaScript or XPath fallbacks.")
            raise Exception("Could not find 'Continue' button using JavaScript or XPath fallbacks.")
        except Exception as e:
            logging.error(f"Error while clicking 'Continue' button: {e}")
            raise
        
    def extract_and_save_secret_key(self):
        try:
            # Extract secret key logic (keep existing extraction code)
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            possible_xpaths = [
				"/html/body/div[11]/div/div[2]/span/div/div/ol/li[2]/div/strong",
                "/html/body/div[10]/div/div[2]/span/div/div/ol/li[2]/div/strong",
                "//div[contains(text(), 'Secret key')]/following-sibling::div//strong",
                "//ol/li[2]/div/strong",
                "//div[@id='totp-key']/div/strong",
                "//div[@id='totp-key']/strong",
                "//div[contains(@class, 'secret-key')]/strong",
                "//div[contains(text(), 'Key')]/following-sibling::div//strong",
                "//strong[contains(text(), ' ')]",
            ]
            secret_key = None
            for xpath in possible_xpaths:
                try:
                    secret_key_element = self.driver.find_element(By.XPATH, xpath)
                    secret_key_candidate = secret_key_element.text.strip()
                    if self.is_valid_secret_key(secret_key_candidate):
                        secret_key = secret_key_candidate
                        logging.info(f"Found secret key using XPath: {xpath}")
                        break
                except NoSuchElementException:
                    logging.warning(f"Secret key not found using XPath: {xpath}")
                    continue
            
            if not secret_key:
                raise ValueError("Secret key not found.")
            
            # GET ACCOUNT NAME - same way as in login function
            account_name = self.email_entry.text().strip()
            if not account_name:
                # Get from accounts text area (same logic as login function)
                accounts_data = self.accounts_text.toPlainText().strip().splitlines()
                if accounts_data and accounts_data[0].strip():
                    line = accounts_data[0].strip()
                    if ',' in line:
                        account_name, _ = line.split(',', 1)
                    elif ':' in line:
                        account_name, _ = line.split(':', 1)
                    account_name = account_name.strip()

            logging.info(f"DEBUG: Using account name: '{account_name}'")

            if not account_name:
                raise ValueError("No account name found - cannot save secret key")
            
            # Create temporary file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
                temp_file.write(secret_key)
                temp_file_path = temp_file.name
            
            try:
                # Direct SFTP connection and upload
                sftp = self.sftp_connect()
                try:
                    # Create the account directory
                    remote_account_dir = f"{self.remote_dir}{account_name}"
                    self.ensure_remote_directory_exists(sftp, remote_account_dir)
                    
                    # Upload to correct path with account name in filename
                    remote_file_path = f"{remote_account_dir}/{account_name}_authenticator_secret_key.txt"
                    sftp.put(temp_file_path, remote_file_path)
                    logging.info(f"Secret key saved to SFTP: {remote_file_path}")
                    
                finally:
                    sftp.close()
                    
            finally:
                # Always delete temp file
                import os
                os.unlink(temp_file_path)
                
            return secret_key
            
        except Exception as e:
            logging.error(f"Failed to extract and save secret key: {e}")
            return None
    
    def upload_to_server(self, local_file_path, remote_file_path):
        try:
            sftp = self.sftp_connect()
            remote_directory = os.path.dirname(remote_file_path).replace("\\", "/")
            self.ensure_remote_directory_exists(sftp, remote_directory)
            sftp.put(local_file_path, remote_file_path)
            logging.info(f"Uploaded {local_file_path} to {remote_file_path} on the server.")
        except Exception as e:
            logging.error(f"Failed to upload {local_file_path} to server: {e}")
            raise e
        finally:
            if 'sftp' in locals():
                sftp.close()
                logging.info("SFTP connection closed after uploading file.")
    
    def is_valid_secret_key(self, key):
        return len(key) >= 16 and all(char.isalnum() or char.isspace() for char in key)
        
    def enter_and_verify_totp_code(self):
        try:
            account_name = self.email_entry.text().strip()
            if not account_name:
                # Get from accounts text area
                accounts_data = self.accounts_text.toPlainText().strip().splitlines()
                if accounts_data and accounts_data[0].strip():
                    line = accounts_data[0].strip()
                    if ',' in line:
                        account_name, _ = line.split(',', 1)
                    elif ':' in line:
                        account_name, _ = line.split(':', 1)
                    account_name = account_name.strip()

            logging.info(f"DEBUG: Using account name for OTP: '{account_name}'")

            if not account_name:
                logging.error("No account name found - cannot generate TOTP code")
                return False
            secret_key_file = self.get_secret_key_file(account_name)
            if not secret_key_file:
                logging.error(f"Secret key file not found for {account_name}. Cannot generate TOTP code.")
                return False
            
            with open(secret_key_file, 'r') as f:
                secret_key = f.read().strip()
            
            totp_code = self.generate_otp_code(secret_key)
            if not totp_code:
                logging.error("Failed to generate TOTP code.")
                return False
            
            logging.info(f"Generated OTP code: {totp_code}")
            
            # STRATEGY 1: Target by specific attributes (most reliable)
            attribute_selectors = [
                # Primary selector using jsname and ID
                "//input[@jsname='YPqjbf' and @id='c0']",
                
                # Alternative using jsname only
                "//input[@jsname='YPqjbf']",
                
                # Target by ID (c0, c1, c2, etc. are common)
                "//input[@id='c0']",
                "//input[@id='c1']", 
                "//input[@id='c2']",
                
                # Target by class pattern
                "//input[contains(@class, 'VfPpkd-fmcmS-wGMbrd')]",
            ]
            
            # STRATEGY 2: Target by placeholder text (language-specific)
            placeholder_selectors = [
                # Dutch
                "//input[@placeholder='Geef de code op']",
                "//input[contains(@placeholder, 'code')]",
                
                # English
                "//input[@placeholder='Enter the code']",
                "//input[contains(@placeholder, 'Enter') and contains(@placeholder, 'code')]",
                
                # General code input patterns
                "//input[contains(@placeholder, 'Code') or contains(@placeholder, 'code')]",
            ]
            
            # STRATEGY 3: Target by input type and context
            context_selectors = [
                # Text input in authenticator dialog
                "//div[contains(@class, 'qPtGzb')]//input[@type='text']",
                
                # Input with autocomplete=off (common for OTP)
                "//input[@autocomplete='off']",
                
                # Input in dialog with specific controller
                "//div[@jscontroller='ieZWvb']//input[@type='text']",
                
                # Input with specific aria controls
                "//input[@aria-controls='c3']",
            ]
            
            # STRATEGY 4: Target by parent structure
            structural_selectors = [
                # Input inside label with specific classes
                "//label[contains(@class, 'VfPpkd-fmcmS-yrriRe')]//input",
                
                # Input in div with skQ8Ge class
                "//div[@class='skQ8Ge']//input",
                
                # Input in specific step container
                "//div[@wizard-step-uid and contains(@wizard-step-uid, 'verifyCode')]//input",
            ]
            
            # STRATEGY 5: Fallback selectors
            fallback_selectors = [
                # Any text input in the dialog
                "//div[@role='dialog']//input[@type='text']",
                
                # Any input that accepts text
                "//input[@type='text' and not(@style*='display: none')]",
                
				# Last resort: any visible input
				"//input[not(@type='hidden') and not(@style*='display: none')]",
				
				# Provided absolute fallback
				"/html/body/div[11]/div/div[2]/span/div/div/div/div[2]/div/div/div[1]/span[2]/input",
            ]
            
            # Combine all selectors in priority order
            all_selectors = (
                attribute_selectors + 
                placeholder_selectors + 
                context_selectors + 
                structural_selectors + 
                fallback_selectors
            )
            
            totp_input = None
            
            # Try each selector with short timeout
            for i, selector in enumerate(all_selectors):
                try:
                    logging.info(f"Trying OTP input selector {i+1}: {selector}")
                    
                    totp_input = WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    
                    # Verify the element is actually visible and can accept input
                    if totp_input.is_displayed() and totp_input.is_enabled():
                        logging.info(f"✅ Found OTP input with selector {i+1}")
                        break
                    else:
                        logging.warning(f"⚠️ Input found but not interactable, trying next...")
                        totp_input = None
                        continue
                        
                except TimeoutException:
                    logging.info(f"❌ OTP input selector {i+1} failed")
                    continue
                except Exception as e:
                    logging.warning(f"⚠️ Error with OTP selector {i+1}: {e}")
                    continue
            
            if not totp_input:
                # Final attempt: JavaScript search
                try:
                    logging.info("🔍 Last resort: JavaScript search for input fields")
                    totp_input = self.driver.execute_script("""
                        // Find all visible text inputs
                        var inputs = document.querySelectorAll('input[type="text"]');
                        for (var i = 0; i < inputs.length; i++) {
                            var input = inputs[i];
                            // Check if visible and not hidden
                            var style = window.getComputedStyle(input);
                            if (style.display !== 'none' && style.visibility !== 'hidden' && 
                                input.offsetWidth > 0 && input.offsetHeight > 0) {
                                return input;
                            }
                        }
                        return null;
                    """)
                    
                    if totp_input:
                        logging.info("✅ Found OTP input using JavaScript")
                    else:
                        raise Exception("No visible input found")
                        
                except Exception as e:
                    logging.error(f"JavaScript search failed: {e}")
                    raise TimeoutException("Could not find OTP input field with any method")
            
            # Clear and enter the OTP code with multiple methods
            logging.info(f"Entering OTP code: {totp_code}")
            
            # Method 1: Clear with multiple approaches
            try:
                # Standard clear
                totp_input.clear()
                time.sleep(0.5)
                
                # Ensure it's really cleared with JavaScript
                self.driver.execute_script("arguments[0].value = '';", totp_input)
                time.sleep(0.5)
                
                # Select all and delete (fallback)
                totp_input.send_keys(Keys.CONTROL + "a")
                totp_input.send_keys(Keys.DELETE)
                time.sleep(0.5)
                
            except Exception as e:
                logging.warning(f"⚠️ Clear methods had issues: {e}")
            
            # Method 2: Enter the code with multiple approaches
            entry_successful = False
            
            # Approach 1: Regular send_keys
            try:
                totp_input.send_keys(totp_code)
                # Verify it was entered
                entered_value = totp_input.get_attribute('value')
                if entered_value == totp_code:
                    logging.info("✅ OTP entered successfully with send_keys")
                    entry_successful = True
                else:
                    logging.warning(f"⚠️ send_keys failed. Expected: {totp_code}, Got: {entered_value}")
            except Exception as e:
                logging.warning(f"⚠️ send_keys failed: {e}")
            
            # Approach 2: JavaScript entry (if send_keys failed)
            if not entry_successful:
                try:
                    self.driver.execute_script("arguments[0].value = arguments[1];", totp_input, totp_code)
                    # Trigger input event
                    self.driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", totp_input)
                    
                    # Verify
                    entered_value = totp_input.get_attribute('value')
                    if entered_value == totp_code:
                        logging.info("✅ OTP entered successfully with JavaScript")
                        entry_successful = True
                    else:
                        logging.warning(f"⚠️ JavaScript failed. Expected: {totp_code}, Got: {entered_value}")
                except Exception as e:
                    logging.warning(f"⚠️ JavaScript entry failed: {e}")
            
            # Approach 3: Character by character entry (most reliable)
            if not entry_successful:
                try:
                    totp_input.clear()
                    for char in totp_code:
                        totp_input.send_keys(char)
                        time.sleep(0.1)  # Small delay between characters
                    
                    # Verify
                    entered_value = totp_input.get_attribute('value')
                    if entered_value == totp_code:
                        logging.info("✅ OTP entered successfully character by character")
                        entry_successful = True
                    else:
                        logging.warning(f"⚠️ Character entry failed. Expected: {totp_code}, Got: {entered_value}")
                except Exception as e:
                    logging.warning(f"⚠️ Character entry failed: {e}")
            
            if not entry_successful:
                raise Exception("All OTP entry methods failed")
            
            logging.info("Entered the 6-digit TOTP code successfully.")
            
            # Wait a moment for the UI to process
            time.sleep(random.uniform(1, 2))
            
            # Find and click the Verify button with multiple strategies
            verify_selectors = [
                # Use the exact HTML you provided
                "//button[@data-id='dtOep' and @jsname='LgbsSe']",
                "//button[@data-id='dtOep']",
                "//span[text()='Verifiëren']//parent::button",
                "//div[@class='sRKBBe']//button[contains(@class, 'VfPpkd-LgbsSe')]",
            ]
            
            try:
                verify_button = WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//span[contains(text(), 'Verifiëren')]"))
                )
                # Scroll into view and click with JavaScript
                self.driver.execute_script("arguments[0].scrollIntoView(); arguments[0].click();", verify_button)
                logging.info("✅ Clicked verify button with JavaScript")
            except TimeoutException:
                # Fallback: use ENTER key
                totp_input.send_keys(Keys.ENTER)
                logging.info("✅ Used ENTER key to submit")
            
            time.sleep(3)  # Wait for processing

            # Wait for completion and verify success
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div/div[3]/div"))
                )
                logging.info(f"✅ TOTP verification completed successfully for {account_name}.")
                self.click_turn_on_button_single(driver)
                return True
            except TimeoutException as e:
                logging.error(f"❌ Timeout while verifying TOTP code: {e}")
                self.log_failure(AUTHENTICATOR_FAILURE_FILE, account_name, "Authenticator Setup", "TOTP Verification Failed")
                return False
        except Exception as e:
            logging.error(f"❌ Error during TOTP verification: {e}")
            self.log_failure(AUTHENTICATOR_FAILURE_FILE, account_name, "Authenticator Setup", str(e))
            return False

    def log_failure(self, failure_file, email, step, message):
        with open(failure_file, 'a') as f:
            f.write(f"{email},{step},{message}\n")
        logging.error(f"Logged failure for {email} at step {step}: {message}")
            
    def enable_two_step_verification(self):
        """Enable two-step verification for single account or multiple accounts based on account field content"""
        try:
            # Check if we have multiple accounts in the field
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            if len(accounts_data) > 1:
                # Multiple accounts detected, use bulk operation
                logging.info(f"Multiple accounts detected ({len(accounts_data)}), starting bulk Enable Two-Step Verification")
                self.enable_two_step_verification_bulk()
                return
            
            # Single account operation (original logic)
            email = self.email_entry.text()
            logging.info(f"Navigating to 2-Step Verification page for {email}...")
            self.driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
            if self.is_two_step_verification_enabled():
                logging.info(f"Skipping Two-Step Verification for {email} as it's already enabled.")
                return True
            try:
                get_started_button = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[5]/div/button/span[6]"))
                )
                self.driver.execute_script("arguments[0].click();", get_started_button)
                logging.info(f"Clicked on 'Get Started' or 'Turn On' button for {email}.")
                self.handle_reauthentication()
                if self.detect_final_pop_up():
                    logging.info(f"2-Step Verification enabled successfully for {email} by detecting the final pop-up.")
                    return True
                else:
                    logging.error(f"Final pop-up not detected. 2-Step Verification may not have been enabled for {email}.")
                    return False
            except TimeoutException as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Timeout while enabling 2-Step Verification for {email}: {e}")
                return False
            except Exception as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Error during 2-Step Verification setup for {email}: {e}")
                return False
                
        except Exception as e:
            logging.error(f"Error in enable_two_step_verification: {e}")
            return False
    
    def enable_two_step_verification_bulk(self):
        """Enable two-step verification for multiple accounts with concurrency"""
        try:
            # Get accounts from the enhanced account field
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            accounts = []
            
            for line in accounts_data:
                line = line.strip()
                if not line:
                    continue
                    
                user = None
                pwd = None
                
                if ':' in line:
                    user, pwd = line.split(':', 1)
                elif ',' in line:
                    user, pwd = line.split(',', 1)
                else:
                    # Only email provided, try to fetch password from server
                    user = line.strip()
                    logging.info(f"🔍 Only email provided: {user}, attempting to fetch password from server")
                    pwd = self.get_account_password(user)
                    if not pwd:
                        logging.warning(f"🔍 Password not found for {user}, skipping this account")
                        continue
                    
                user = user.strip()
                pwd = pwd.strip()
                
                if user and pwd:
                    accounts.append((user, pwd))
                    
            if not accounts:
                QMessageBox.warning(self, "No Valid Accounts", 
                    "No valid accounts found. Please ensure accounts are in format 'email:password' or 'email' (if password exists on server).")
                return
                
            # Get concurrency limit from the existing spinbox
            try:
                concurrent_limit = int(self.concurrent_accounts_entry.text().strip())
                if concurrent_limit <= 0:
                    concurrent_limit = 1
            except ValueError:
                concurrent_limit = 1
                
            logging.info(f"Starting bulk Enable Two-Step Verification with {len(accounts)} accounts, max {concurrent_limit} concurrent")
            
            # Use the bulk operation mechanism
            self.enable_two_step_verification_bulk_enhanced(accounts, concurrent_limit)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error in bulk Enable Two-Step Verification: {str(e)}")
            logging.error(f"Error in enable_two_step_verification_bulk: {e}")
            
    def enable_two_step_verification_bulk_enhanced(self, accounts, concurrent_limit):
        """Enhanced version of bulk Enable Two-Step Verification with proper window arrangement"""
        try:
            # Enable global OTP detection
            self.start_global_otp_detection()
            
            # Debug: Log initial state of active_drivers
            with self.driver_lock:
                logging.info(f"🔍 ENABLE 2SV - Initial active_drivers count: {len(self.active_drivers)}")
                for i, driver in enumerate(self.active_drivers):
                    try:
                        logging.info(f"   Driver {i}: URL={driver.current_url}, Title={driver.title}")
                    except:
                        logging.info(f"   Driver {i}: [Error getting driver info]")
            
            # Detect existing browser windows
            existing_driver_map = self.detect_existing_browser_windows([account for account, _ in accounts])
            logging.info(f"Found {len(existing_driver_map)} existing browser windows")
            
            # Track window reuse statistics
            reused_windows = 0
            new_windows = 0
            
            screen_width = 1920
            screen_height = 1080
            # Use concurrent_limit for window arrangement
            positions = self.get_grid_positions(concurrent_limit, screen_width, screen_height)
            
            # Helper function for enhanced Enable Two-Step Verification
            def enable_2sv_account_enhanced(account, password, position, app_instance):
                nonlocal reused_windows, new_windows
                driver = None
                operation_successful = False
                original_driver = None
                try:
                    # Check if we have an existing driver for this account
                    existing_driver = None
                    for existing_drv, existing_acc in existing_driver_map.items():
                        if existing_acc == account:
                            existing_driver = existing_drv
                            break
                    
                    if existing_driver:
                        logging.info(f"🔄 Reusing existing browser window for {account} (already logged in)")
                        reused_windows += 1
                        driver = existing_driver
                        # Remove from existing map to avoid reuse
                        existing_driver_map.pop(existing_driver, None)
                        
                        # Verify the account is still logged in and ready
                        try:
                            current_url = driver.current_url
                            logging.info(f"Current URL for {account}: {current_url}")
                            
                            # If we're on a login page, the session might have expired
                            if "accounts.google.com/signin" in current_url:
                                logging.warning(f"Session expired for {account}, will need to login again")
                                # Create new driver and login
                                driver = app_instance.init_driver_instance()
                                if driver is None:
                                    logging.error(f"Failed to create driver for {account}")
                                    return
                                
                                # Add driver to active drivers list for OTP tracking
                                with app_instance.driver_lock:
                                    app_instance.active_drivers.append(driver)
                                    # Track driver-to-account mapping
                                    app_instance.driver_to_account[driver] = account
                                    app_instance.account_to_driver[account] = driver
                                    
                                # Set the window geometry based on the computed position
                                driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                                
                                # Perform login first
                                logging.info(f"Starting login for {account} (session expired)")
                                app_instance.perform_login_enhanced(driver, account, password)
                                
                                # Wait for login to complete and page to load
                                time.sleep(5)
                            else:
                                logging.info(f"✅ Account {account} is still logged in and ready for 2SV")
                        except Exception as verify_e:
                            logging.warning(f"Error verifying login status for {account}: {verify_e}")
                            # Fall back to creating new driver
                            driver = app_instance.init_driver_instance()
                            if driver is None:
                                logging.error(f"Failed to create driver for {account}")
                                return
                            
                            # Add driver to active drivers list for OTP tracking
                            with app_instance.driver_lock:
                                app_instance.active_drivers.append(driver)
                                # Track driver-to-account mapping
                                app_instance.driver_to_account[driver] = account
                                app_instance.account_to_driver[account] = driver
                                
                            # Set the window geometry based on the computed position
                            driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                            
                            # Perform login first
                            logging.info(f"Starting login for {account} (verification failed)")
                            app_instance.perform_login_enhanced(driver, account, password)
                            
                            # Wait for login to complete and page to load
                            time.sleep(5)
                    else:
                        logging.info(f"🆕 Creating new browser window for {account} (no existing window found)")
                        new_windows += 1
                        # Create a new driver instance for the account
                        driver = app_instance.init_driver_instance()
                        if driver is None:
                            logging.error(f"Failed to create driver for {account}")
                            return
                        
                        # Add driver to active drivers list for OTP tracking
                        with app_instance.driver_lock:
                            app_instance.active_drivers.append(driver)
                            # Track driver-to-account mapping
                            app_instance.driver_to_account[driver] = account
                            app_instance.account_to_driver[account] = driver
                            
                        # Set the window geometry based on the computed position
                        driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                        
                        # Perform login first
                        logging.info(f"Starting login for {account} before Enable Two-Step Verification")
                        app_instance.perform_login_enhanced(driver, account, password)
                        
                        # Wait for login to complete and page to load
                        time.sleep(5)
                    
                    # Wait for page to be stable before proceeding with operations
                    try:
                        WebDriverWait(driver, 10).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                        logging.info(f"Page loaded completely for {account}")
                    except:
                        logging.warning(f"Page load timeout for {account}, continuing anyway")
                    
                    # Temporarily set self.driver to the current account's driver for the original function
                    original_driver = app_instance.driver
                    app_instance.driver = driver
                    
                    # Set the email and password in the UI fields for the original function to use
                    original_email = app_instance.email_entry.text()
                    original_password = app_instance.password_entry.text()
                    app_instance.email_entry.setText(account)
                    app_instance.password_entry.setText(password)
                    
                    # Run Enable Two-Step Verification using original function
                    logging.info(f"Starting Enable Two-Step Verification for {account}")
                    try:
                        # Wait for page to be ready before starting two-step verification
                        time.sleep(3)
                        two_step_success = app_instance.enable_two_step_verification_single(driver, account, password)
                        if two_step_success:
                            logging.info(f"Enable Two-Step Verification completed successfully for {account}")
                            operation_successful = True
                        else:
                            logging.warning(f"Enable Two-Step Verification failed for {account}")
                    except Exception as two_step_e:
                        logging.error(f"Error in Enable Two-Step Verification for {account}: {two_step_e}")
                        two_step_success = False
                    
                    # Restore original driver and UI fields
                    app_instance.driver = original_driver
                    app_instance.email_entry.setText(original_email)
                    app_instance.password_entry.setText(original_password)
                    
                    # Keep the browser open for user inspection
                    logging.info(f"Enable Two-Step Verification completed for {account}. Browser window remains open for inspection.")
                    
                except Exception as e:
                    # Restore original driver and UI fields even on error
                    if original_driver is not None:
                        app_instance.driver = original_driver
                    if 'original_email' in locals():
                        app_instance.email_entry.setText(original_email)
                    if 'original_password' in locals():
                        app_instance.password_entry.setText(original_password)
                    
                    if not app_instance.handle_driver_error(e):
                        logging.error(f"Error running Enable Two-Step Verification for account {account}: {e}")
                finally:
                    # Close driver after successful operation (as per user request)
                    if operation_successful and driver:
                        try:
                            driver.quit()
                            with app_instance.driver_lock:
                                if driver in app_instance.active_drivers:
                                    app_instance.active_drivers.remove(driver)
                                # Remove driver-to-account mapping
                                if driver in app_instance.driver_to_account:
                                    account_to_remove = app_instance.driver_to_account[driver]
                                    del app_instance.driver_to_account[driver]
                                    if account_to_remove in app_instance.account_to_driver:
                                        del app_instance.account_to_driver[account_to_remove]
                            logging.info(f"Browser closed for {account} after successful 2SV operation.")
                        except Exception as quit_e:
                            logging.error(f"Error closing browser for {account}: {quit_e}")
                    elif not operation_successful and driver and driver not in existing_driver_map:
                        try:
                            driver.quit()
                            with app_instance.driver_lock:
                                if driver in app_instance.active_drivers:
                                    app_instance.active_drivers.remove(driver)
                                # Remove driver-to-account mapping
                                if driver in app_instance.driver_to_account:
                                    account_to_remove = app_instance.driver_to_account[driver]
                                    del app_instance.driver_to_account[driver]
                                    if account_to_remove in app_instance.account_to_driver:
                                        del app_instance.account_to_driver[account_to_remove]
                            logging.info(f"Browser closed for {account} due to operation failure.")
                        except Exception as quit_e:
                            logging.error(f"Error closing browser for {account}: {quit_e}")
            
            # Process accounts concurrently using a thread pool with concurrency limit
            with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
                futures = []
                for i, (account, pwd) in enumerate(accounts):
                    # Use modulo to cycle through positions based on concurrent_limit
                    pos_index = i % concurrent_limit
                    pos = positions[pos_index] if pos_index < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                    futures.append(executor.submit(enable_2sv_account_enhanced, account, pwd, pos, self))
                # Wait for all accounts to complete processing
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(f"Thread execution error: {e}")
            
            # Disable global OTP detection
            self.stop_global_otp_detection()
            
            # Log summary of window reuse
            logging.info(f"📊 Enable Two-Step Verification Summary:")
            logging.info(f"   • Total accounts processed: {len(accounts)}")
            logging.info(f"   • Existing windows reused: {reused_windows}")
            logging.info(f"   • New windows created: {new_windows}")
            logging.info(f"   • Window reuse rate: {(reused_windows/len(accounts)*100):.1f}%")
            
            QMessageBox.information(self, "Done", f"Enable Two-Step Verification completed for {len(accounts)} accounts.\n\nWindow Reuse Summary:\n• Reused: {reused_windows} windows\n• Created: {new_windows} new windows\n• Reuse rate: {(reused_windows/len(accounts)*100):.1f}%\n\nBrowser windows have been closed after successful operations.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error in enhanced bulk Enable Two-Step Verification: {str(e)}")
            logging.error(f"Error in enable_two_step_verification_bulk_enhanced: {e}")
            # Disable global OTP detection on error
            self.stop_global_otp_detection()
            
    def enable_two_step_verification_single(self, driver, email, password):
        """Enable two-step verification for a single account using provided driver"""
        try:
            logging.info(f"Navigating to 2-Step Verification page for {email}...")
            driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
            if self.is_two_step_verification_enabled_single(driver):
                logging.info(f"Skipping Two-Step Verification for {email} as it's already enabled.")
                return True
            try:
                get_started_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[5]/div/button/span[6]"))
                )
                driver.execute_script("arguments[0].click();", get_started_button)
                logging.info(f"Clicked on 'Get Started' or 'Turn On' button for {email}.")
                self.handle_reauthentication_single(driver, password)
                if self.detect_final_pop_up_single(driver):
                    logging.info(f"2-Step Verification enabled successfully for {email} by detecting the final pop-up.")
                    return True
                else:
                    logging.error(f"Final pop-up not detected. 2-Step Verification may not have been enabled for {email}.")
                    return False
            except TimeoutException as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Timeout while enabling 2-Step Verification for {email}: {e}")
                return False
            except Exception as e:
                self.log_failure(TWO_STEP_FAILURE_FILE, email, "Two-Step Verification", str(e))
                logging.error(f"Error during 2-Step Verification setup for {email}: {e}")
                return False
                
        except Exception as e:
            logging.error(f"Error in enable_two_step_verification_single: {e}")
            return False
            
    def is_two_step_verification_enabled_single(self, driver):
        logging.info("Checking if 2-Step Verification is enabled...")
        driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            turn_off_buttons = driver.find_elements(By.XPATH, "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[5]/div/button/span[6]")
            if turn_off_buttons:
                logging.info("2-Step Verification is already enabled.")
                return True
            else:
                logging.info("2-Step Verification is NOT enabled.")
                return False
        except Exception as e:
            logging.error(f"Error checking 2SV status: {e}")
            return False
            
    def handle_reauthentication_single(self, driver, password):
        try:
            password_field = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.NAME, "password"))
            )
            password_field.clear()
            password_field.send_keys(password)
            password_field.send_keys(Keys.ENTER)
            logging.info("Re-entered password for reauthentication.")
        except TimeoutException:
            logging.info("No reauthentication prompt detected.")
            
    def detect_final_pop_up_single(self, driver):
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "/html/body/div[11]/div[2]/div/span"))
            )
            logging.info("Final pop-up detected after enabling 2-Step Verification.")
            return True
        except TimeoutException:
            logging.error("Final pop-up not detected. 2-Step Verification may not have been enabled.")
            return False
        
    def detect_final_pop_up(self):
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "/html/body/div[11]/div[2]/div/span"))
            )
            logging.info("Final pop-up detected after enabling 2-Step Verification.")
            return True
        except TimeoutException:
            logging.error("Final pop-up not detected. 2-Step Verification may not have been enabled.")
            return False
    
    def handle_reauthentication(self):
        try:
            password_field = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((By.NAME, "password"))
            )
            password_field.clear()
            password_field.send_keys(self.password_entry.text())
            password_field.send_keys(Keys.ENTER)
            logging.info("Re-entered password for reauthentication.")
        except TimeoutException:
            logging.info("No reauthentication prompt detected.")
    
    def generate_otp_code(self, secret_key):
        try:
            sanitized_key = ''.join([char.upper() for char in secret_key if char.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"])
            if not sanitized_key:
                logging.error("Sanitized secret key is empty.")
                return None
            totp = pyotp.TOTP(sanitized_key)
            otp_code = totp.now()
            logging.info(f"Generated OTP code: {otp_code}")
            return otp_code
        except Exception as e:
            logging.error(f"Error generating OTP code: {e}")
            return None

    def handle_otp_if_needed(self):
        """Check if OTP is needed and handle it automatically"""
        if "challenge/totp" in self.driver.current_url:
            logging.info("OTP challenge detected during operation...")
            
            # Get account name
            email = self.email_entry.text().strip()
            if not email:
                accounts_data = self.accounts_text.toPlainText().strip().splitlines()
                if accounts_data and accounts_data[0].strip():
                    line = accounts_data[0].strip()
                    if ',' in line:
                        email, _ = line.split(',', 1)
                    elif ':' in line:
                        email, _ = line.split(':', 1)
                    email = email.strip()
            
            secret_key_file = self.get_secret_key_file(email)
            if secret_key_file:
                try:
                    with open(secret_key_file, 'r') as f:
                        secret_key = f.read().strip()
                    otp_code = self.generate_otp_code(secret_key)
                    if otp_code:
                        otp_input = WebDriverWait(self.driver, 15).until(
                            EC.element_to_be_clickable((By.XPATH, '//input[@type="tel"]'))
                        )
                        otp_input.clear()
                        otp_input.send_keys(otp_code)
                        otp_input.send_keys(Keys.ENTER)
                        logging.info("Entered OTP during operation.")
                        time.sleep(3)  # Wait for processing
                        return True
                finally:
                    # Clean up temp file
                    import os
                    if secret_key_file and os.path.exists(secret_key_file):
                        os.unlink(secret_key_file)
        return False

    def get_secret_key_file(self, account_name):
        """Check local profiles directory first, then fetch from SFTP server if needed"""
        try:
            # First check local profiles directory
            import os
            profiles_dir = os.path.join(os.getcwd(), "profiles")
            local_account_dir = os.path.join(profiles_dir, account_name)
            local_secret_key_path = os.path.join(local_account_dir, f"{account_name}_authenticator_secret_key.txt")
            
            # Check if local file exists and is valid
            if os.path.exists(local_secret_key_path):
                try:
                    with open(local_secret_key_path, 'r') as f:
                        secret_key = f.read().strip()
                    
                    if self.is_valid_secret_key(secret_key):
                        logging.info(f"Secret key found locally: {local_secret_key_path}")
                        return local_secret_key_path
                    else:
                        logging.warning(f"Invalid secret key content in local file for {account_name}")
                except Exception as e:
                    logging.warning(f"Error reading local secret key file for {account_name}: {e}")
            
            # If local file doesn't exist or is invalid, try SFTP server
            logging.info(f"Local secret key not found for {account_name}, trying SFTP server...")
            sftp = self.sftp_connect()
            remote_dir = f"{self.remote_dir}{account_name}/"
            remote_file = f"{remote_dir}{account_name}_authenticator_secret_key.txt"
            
            # Download to temporary file
            with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt') as temp_file:
                temp_file_path = temp_file.name
            
            try:
                sftp.get(remote_file, temp_file_path)
                logging.info(f"Secret key fetched from SFTP: {remote_file}")
                
                # Validate the key
                with open(temp_file_path, 'r') as f:
                    secret_key = f.read().strip()
                
                if self.is_valid_secret_key(secret_key):
                    # Also save to local profiles directory for future use
                    if not os.path.exists(local_account_dir):
                        os.makedirs(local_account_dir)
                    
                    with open(local_secret_key_path, 'w') as local_file:
                        local_file.write(secret_key)
                    logging.info(f"Secret key saved locally from server: {local_secret_key_path}")
                    
                    return temp_file_path  # Return temp file path for immediate use
                else:
                    logging.error(f"Invalid secret key content for {account_name}")
                    return None
                    
            except Exception as e:
                logging.error(f"Failed to download secret key for {account_name}: {e}")
                return None
            finally:
                sftp.close()
                
        except Exception as e:
            logging.error(f"SFTP connection failed for {account_name}: {e}")
            return None
        
    def fetch_secret_key_from_server(self, account_name):
        try:
            sftp = self.sftp_connect()
            remote_profile_dir = os.path.join(self.remote_dir, account_name).replace("\\", "/")
            remote_key_file = os.path.join(remote_profile_dir, f"{account_name}_authenticator_secret_key.txt").replace("\\", "/")
            local_profile_dir = os.path.join(self.local_profiles_dir, account_name)
            local_key_file = os.path.join(local_profile_dir, f"{account_name}_authenticator_secret_key.txt")
            os.makedirs(local_profile_dir, exist_ok=True)
            logging.info(f"Attempting to fetch secret key for {account_name} from {remote_key_file}.")
            sftp.get(remote_key_file, local_key_file)
            logging.info(f"Secret key fetched for {account_name} and saved to {local_key_file}.")
            with open(local_key_file, "r") as f:
                secret_key = f.read().strip()
                if not secret_key or len(secret_key) < 10:
                    raise ValueError(f"Invalid secret key content: {secret_key}")
            return local_key_file
        except FileNotFoundError:
            logging.error(f"Secret key file not found for {account_name} on the server: {remote_key_file}")
        except Exception as e:
            logging.error(f"Failed to fetch secret key for {account_name}: {e}")
        finally:
            if 'sftp' in locals():
                sftp.close()
        return None
    
    def ensure_remote_directory_exists(self, sftp, remote_directory):
        dirs = remote_directory.replace("\\", "/").split('/')
        path = ''
        for dir in dirs:
            if dir == '':
                continue
            path += f'/{dir}'
            try:
                sftp.stat(path)
            except IOError:
                sftp.mkdir(path)
                logging.info(f"Created remote directory: {path}")
        
    def generate_app_password(self):
        email = self.email_entry.text().strip()
        if not email:
            # Get from accounts text area
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            if accounts_data and accounts_data[0].strip():
                line = accounts_data[0].strip()
                if ',' in line:
                    email, _ = line.split(',', 1)
                elif ':' in line:
                    email, _ = line.split(':', 1)
                email = email.strip()

        logging.info(f"DEBUG: Using email for app password: '{email}'")

        if not email:
            logging.error("No email found - cannot generate app password")
            return None
        try:
            logging.info(f"Navigating to the App Passwords page for {email}...")
            self.driver.get("https://myaccount.google.com/apppasswords?hl=en")
            
            # *** ADD THIS LINE HERE ***
            self.handle_otp_if_needed()
            
            max_retries = 3
            attempt = 0
            while attempt < max_retries:
                try:
                    app_name_field = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.XPATH, '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/label/input'))
                    )
                    logging.info(f"App name input field located on attempt {attempt + 1}.")
                    break
                except TimeoutException:
                    attempt += 1
                    logging.warning(f"App password page not fully loaded. Retrying... {attempt}/{max_retries}")
                    
                    # *** ADD THIS LINE HERE ***
                    if self.handle_otp_if_needed():
                        continue
                        
                    self.driver.refresh()
            if attempt == max_retries:
                logging.error(f"Failed to load the app password page after {max_retries} attempts.")
                self.log_failure(self.APP_PASSWORD_FAILURE_FILE, email, "App Password Generation", "Page did not load")
                return None
            app_name = f"App-{int(time.time())}"
            app_name_field.clear()
            app_name_field.send_keys(app_name)
            logging.info(f"App name '{app_name}' entered successfully.")
            self.add_random_delay(2)
            generate_button = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button/span[2]'))
            )
            self.driver.execute_script("arguments[0].click();", generate_button)
            logging.info("Clicked on 'Generate' to create the app password.")
            logging.info("Waiting for the App Password to be displayed...")
            password_selectors = [
                '/html/body/div[15]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong',  # Original
                "//strong[contains(@class, 'password') or contains(text(), '-')]",  # Password format
                "//div[contains(@class, 'dialog')]//strong",  # Strong in dialog
                "//h2//strong",  # Strong in heading
                "//article//strong",  # Strong in article
                "//div[@role='dialog']//strong",  # Strong in dialog role
                "//strong[string-length(text()) > 10]",  # Strong with long text (password length)
            ]
            
            app_password_element = None
            for i, selector in enumerate(password_selectors):
                try:
                    app_password_element = WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.XPATH, selector))
                    )
                    logging.info(f"✅ Found app password with selector {i+1}: {selector}")
                    break
                except:
                    logging.info(f"❌ Password selector {i+1} failed")
                    continue
            
            if not app_password_element:
                logging.error("Could not find app password element with any selector")
                return None
            
            app_password = app_password_element.text.replace(' ', '')
            logging.info(f"App password for {email}: {app_password}")
            self.save_app_password(email, app_password)
            QMessageBox.information(self, "Success", "App password generated and saved!")
            return app_password
        except TimeoutException as e:
            self.log_failure(self.APP_PASSWORD_FAILURE_FILE, email, "App Password Generation", f"Timeout: {e}")
            logging.error(f"Timeout while generating app password for {email}: {e}")
            QMessageBox.critical(self, "Error", f"Failed to generate app password: Timeout")
            return None
        except Exception as e:
            self.log_failure(self.APP_PASSWORD_FAILURE_FILE, email, "App Password Generation", f"Error: {e}")
            logging.error(f"Error while generating app password for {email}: {e}")
            QMessageBox.critical(self, "Error", f"Failed to generate app password: {e}")
            return None
    
    def load_app_passwords(self):
        password_file = os.path.join(self.local_profiles_dir, 'app_passwords.txt')
        app_passwords = {}
        if os.path.exists(password_file):
            with open(password_file, 'r') as f:
                for line in f:
                    if ':' in line:
                        email, password = line.strip().split(':', 1)
                        email = email.strip()
                        password = password.strip()
                        if email and password:
                            app_passwords[email] = password
        else:
            logging.info(f"No local app_passwords.txt found at {password_file}.")
        return app_passwords
    
    def save_app_password(self, email, app_password):
        """Save app password directly to SFTP, no local storage"""
        password_file_server = f"{self.remote_dir}app_passwords.txt"
        
        try:
            sftp = self.sftp_connect()
            
            # Download existing file to temp location
            server_app_passwords = []
            
            with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt') as temp_file:
                temp_download_path = temp_file.name
            
            try:
                sftp.get(password_file_server, temp_download_path)
                with open(temp_download_path, 'r') as f:
                    for line in f:
                        if ':' in line:
                            srv_email, srv_password = line.strip().split(':', 1)
                            srv_email = srv_email.strip()
                            srv_password = srv_password.strip()
                            if srv_email and srv_password:
                                server_app_passwords.append((srv_email, srv_password))
                logging.info("Existing app_passwords.txt downloaded from SFTP")
            except FileNotFoundError:
                logging.info("No existing app_passwords.txt found on SFTP")
            except Exception as e:
                logging.error(f"Error downloading app_passwords.txt from SFTP: {e}")
            finally:
                import os
                if os.path.exists(temp_download_path):
                    os.unlink(temp_download_path)
            
            # Remove old entry for this email and add new one
            server_app_passwords = [(e, p) for e, p in server_app_passwords if e != email]
            server_app_passwords.append((email, app_password))
            
            # Upload updated file
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
                for email_key, password in server_app_passwords:
                    temp_file.write(f"{email_key}: {password}\n")
                temp_upload_path = temp_file.name
            
            try:
                self.ensure_remote_directory_exists(sftp, os.path.dirname(password_file_server))
                sftp.put(temp_upload_path, password_file_server)
                logging.info(f"App password for {email} saved to SFTP: {password_file_server}")
            finally:
                import os
                os.unlink(temp_upload_path)
                sftp.close()
                
        except Exception as e:
            logging.error(f"Failed to save app password for {email} to SFTP: {e}")
            self.log_failure(self.APP_PASSWORD_FAILURE_FILE, email, "App Password SFTP Save Error", str(e))
    
    def sftp_connect(self):
        transport = paramiko.Transport((self.server_address, self.server_port))
        transport.connect(username=self.username, password=self.password)
        return paramiko.SFTPClient.from_transport(transport)
    
    def is_driver_valid(self):
        try:
            self.driver.current_url
            return True
        except (WebDriverException, AttributeError):
            logging.error("Driver is no longer valid or has been closed.")
            return False
    
    def login(self):
        email = self.email_entry.text().strip()
        password = self.password_entry.text().strip()
        
        logging.info(f"🔍 Login attempt - Email field: '{email}'")
        logging.info(f"🔍 Login attempt - Password field: {'*' * len(password) if password else 'EMPTY'}")

        # If no password provided, try to fetch from server
        if not password and email:
            logging.info(f"🔍 No password provided for {email}, attempting to fetch from server")
            fetched_password = self.get_account_password(email)
            if fetched_password:
                password = fetched_password
                self.password_entry.setText(password)  # Update the password field
                logging.info(f"🔍 Password fetched from server for {email}")
            else:
                QMessageBox.warning(self, "Password Not Found", 
                    f"Password not found for {email}. Please enter the password manually or ensure the account exists on the server.")
                return

        if not email or not password:
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            if accounts_data and accounts_data[0].strip():
                line = accounts_data[0].strip()
                if ',' in line:
                    email, password = line.split(',', 1)
                elif ':' in line:
                    email, password = line.split(':', 1)
                email = email.strip()
                password = password.strip()
                logging.info(f"🔍 Got from bulk text - Email: '{email}'")

        if not email or not password:
            QMessageBox.warning(self, "Missing Credentials", "Please enter both email and password.")
            return

        if self.driver is None or not self.is_driver_valid():
            self.init_driver()
        try:
            self.driver.get("https://accounts.google.com/ServiceLogin?hl=en")
            email_input = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.ID, "identifierId"))
            )
            logging.info(f"Found email input field. Attempting to enter: {email}")

            # Clear the field
            email_input.clear()
            time.sleep(1)

            # Try multiple methods to enter email
            try:
                # Method 1: Normal send_keys
                email_input.send_keys(email)
                logging.info("Used send_keys method")
            except:
                # Method 2: JavaScript fallback
                self.driver.execute_script("arguments[0].value = arguments[1];", email_input, email)
                logging.info("Used JavaScript method")

            # Verify email was entered
            entered_value = email_input.get_attribute('value')
            logging.info(f"Email field now contains: '{entered_value}'")

            if entered_value != email:
                # Force with JavaScript if send_keys failed
                self.driver.execute_script("arguments[0].value = arguments[1];", email_input, email)
                logging.info("Forced email entry with JavaScript")

            email_input.send_keys(Keys.ENTER)
            logging.info("Submitted email form.")
            password_input = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.NAME, "Passwd"))
            )
            password_input.clear()
            password_input.send_keys(password)
            password_input.send_keys(Keys.ENTER)
            logging.info("Entered password and submitted.")
            time.sleep(2)
            if "challenge/totp" in self.driver.current_url:
                logging.info("OTP challenge detected. Attempting to solve it...")
                # Use thread-safe OTP handler with synchronous waiting
                otp_event = self.otp_handler.request_otp_handling(self.driver, email)
                if not otp_event.wait(timeout=60):  # Wait for up to 60 seconds for OTP
                    logging.warning(f"OTP handling timed out for {email}, but continuing...")
                    # Don't raise exception, let the process continue
                else:
                    logging.info(f"OTP handling completed for {email}.")
                
                # Additional wait to ensure OTP processing is complete
                time.sleep(5)
            WebDriverWait(self.driver, 15).until(
                lambda driver: "myaccount.google.com" in driver.current_url or "admin.google.com" in driver.current_url
            )
            if "myaccount.google.com" in self.driver.current_url:
                # Stop at myaccount.google.com - don't redirect further
                QMessageBox.information(self, "Success", "Login successful! Navigated to My Account page.")
                logging.info(f"Login successful for {email}. Stopped at My Account page.")
            elif "admin.google.com" in self.driver.current_url:
                # If already at admin page, navigate to myaccount instead
                self.driver.get("https://myaccount.google.com/?hl=en")
                QMessageBox.information(self, "Success", "Login successful! Navigated to My Account page.")
                logging.info(f"Login successful for {email}. Redirected to My Account page.")
            else:
                logging.warning("Unexpected page after login. Redirecting to My Account page.")
                self.driver.get("https://myaccount.google.com/?hl=en")
                QMessageBox.information(self, "Success", "Login successful! Navigated to My Account page.")
                logging.info("Redirected to My Account page.")
        except TimeoutException as te:
            if not self.handle_driver_error(te):
                QMessageBox.critical(self, "Error", "Login timed out. Please check your credentials or OTP setup.")
                logging.error(f"Login process timed out: {te}")
        except Exception as e:
            # Don't reset the driver for minor errors - keep the browser window open
            logging.error(f"Error during login: {e}")
            QMessageBox.warning(self, "Login Warning", f"Login completed with some issues: {str(e)}")
            # Only reset driver for critical errors that require it
            if any(keyword in str(e).lower() for keyword in ["session deleted", "disconnected", "not connected", "chrome not reachable"]):
                self.driver = None

    def login_multiple_accounts(self):
        """
        Reads the 'Number of concurrent accounts' and the bulk accounts list,
        then logs in the specified number of accounts in parallel, each
        in a separate window, arranged in a grid or cascade.
        """
        # Import necessary modules
        from concurrent.futures import ThreadPoolExecutor, as_completed

        accounts_data = self.accounts_text.toPlainText().strip().splitlines()
        accounts = []
        for line in accounts_data:
            if ',' in line:
                user, pwd = line.split(',', 1)
            elif ':' in line:
                user, pwd = line.split(':', 1)
            else:
                continue
            accounts.append((user.strip(), pwd.strip()))

        num_accounts = len(accounts)
        if num_accounts == 0:
            logging.error("No valid accounts found.")
            QMessageBox.warning(self, "No Accounts", "Please enter valid accounts in the box.")
            return

        # Get concurrency limit from the existing entry
        try:
            concurrent_limit = int(self.concurrent_accounts_entry.text().strip())
            if concurrent_limit <= 0:
                concurrent_limit = 1
        except ValueError:
            concurrent_limit = 1

        screen_width = 1920
        screen_height = 1080

        # Fix: Use concurrent_limit for window arrangement, not total accounts
        positions = self.get_grid_positions(concurrent_limit, screen_width, screen_height)

        # Don't start global OTP detection for multiple logins to avoid conflicts
        # Each login will handle OTP directly
        # self.start_global_otp_detection()

        # Helper function for login only
        def login_account(account, password, position, app_instance): 
            driver = None
            login_successful = False
            try:
                # Create a new driver instance for the account
                driver = app_instance.init_driver_instance()
                if driver is None:
                    logging.error(f"Failed to create driver for {account}")
                    return
                
                # Add driver to active drivers list for OTP tracking
                with app_instance.driver_lock:
                    app_instance.active_drivers.append(driver)
                    
                # Set the window geometry based on the computed position
                driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                
                # Perform enhanced login for the account
                app_instance.perform_login_enhanced(driver, account, password)
                login_successful = True
                
            except Exception as e:
                if not app_instance.handle_driver_error(e):
                    logging.error(f"Thread error during login for {account}: {e}")
            finally:
                # Quit driver if login was not successful to respect concurrent_limit
                if not login_successful and driver:
                    try:
                        driver.quit()
                        with app_instance.driver_lock:
                            if driver in app_instance.active_drivers:
                                app_instance.active_drivers.remove(driver)
                        logging.info(f"Driver for {account} quit due to login failure.")
                    except Exception as quit_e:
                        logging.error(f"Error quitting driver for {account}: {quit_e}")

        try:
            # Process accounts concurrently using a thread pool with concurrency limit
            with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
                futures = []
                for i, (account, pwd) in enumerate(accounts):
                    # Fix: Use modulo to cycle through positions based on concurrent_limit
                    pos_index = i % concurrent_limit
                    pos = positions[pos_index] if pos_index < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                    futures.append(executor.submit(login_account, account, pwd, pos, self))
                # Wait for all accounts to complete processing.
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(f"Thread execution error: {e}")

            QMessageBox.information(self, "Done", f"All {len(accounts)} accounts have been logged in.")
        except Exception as e:
            logging.error(f"Error in multiple login process: {e}")
        # Don't stop global OTP detection since we didn't start it
        # self.stop_global_otp_detection()

    def add_aliases(self):
        try:
            aliases = self.aliases_text.toPlainText().strip().split('\n')
            for alias in aliases:
                if alias.strip():
                    try:
                        logging.info(f"Attempting to add alias: {alias}")
                        
                        # Multi-language Add button selectors
                        add_selectors = [
                            "//div[@jsname='w4Susd']",                    # Most reliable
                            "//div[@data-action-id='w4Susd']",           # Data attribute
                            "//div[@jscontroller='VXdfxd']",             # Controller
                            "//div[contains(@class, 'U26fgb') and contains(@class, 'O0WRkf')]",  # Classes
                            "//div[@role='button' and contains(@data-tooltip, 'omain')]",  # Tooltip backup
                            "//div[@aria-label='Een domein toevoegen']", # Original Dutch (fallback)
                        ]
                        
                        add_button = None
                        for i, selector in enumerate(add_selectors):
                            try:
                                add_button = WebDriverWait(self.driver, 3).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                logging.info(f"✅ Found add button with selector {i+1}: {selector}")
                                break
                            except:
                                logging.info(f"❌ Add button selector {i+1} failed")
                                continue
                        
                        if not add_button:
                            logging.error("Could not find add domain button with any selector")
                            continue
                            
                        add_button.click()
                        logging.info("Clicked add button")
                        
                        # Human-like delay - wait for page to load
                        time.sleep(random.uniform(3, 5))
                        
                        # Multi-language domain input selectors
                        input_selectors = [
                            "//input[@jsname='YPqjbf']",                 # Original
                            "//input[@type='text']",                    # Generic text input
                            "//input[contains(@placeholder, 'omain')]", # Placeholder with domain
                            "//input[contains(@name, 'domain')]",       # Name attribute
                            "//input[contains(@aria-label, 'omain')]",  # Aria label
                            "//div[contains(@class, 'dialog')]//input[@type='text']", # Dialog input
                        ]
                        
                        domain_input = None
                        for i, selector in enumerate(input_selectors):
                            try:
                                domain_input = WebDriverWait(self.driver, 3).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                logging.info(f"✅ Found domain input with selector {i+1}: {selector}")
                                break
                            except:
                                logging.info(f"❌ Input selector {i+1} failed")
                                continue
                        
                        if not domain_input:
                            logging.error("Could not find domain input field with any selector")
                            continue
                        
                        # Human-like typing
                        domain_input.clear()
                        time.sleep(random.uniform(0.5, 1))  # Pause before typing
                        
                        # Type slowly like a human
                        domain_input.send_keys(alias)
                        
                        logging.info(f"Entered domain: {alias}")
                        
                        # Human pause before clicking submit
                        time.sleep(random.uniform(2, 3))
                        
                        # Multi-language submit button selectors
                        submit_selectors = [
                            "//button[@jsname='cRy3zd']",               # Original
                            "//button[@type='submit']",                # Submit type
                            "//button[contains(@class, 'submit')]",    # Submit class
                            "//button[contains(text(), 'Add') or contains(text(), 'Hinzufügen') or contains(text(), 'Toevoegen')]", # Multi-language text
                            "//div[contains(@class, 'dialog')]//button[last()]", # Last button in dialog
                            "//button[contains(@class, 'VfPpkd-LgbsSe')]", # Common Google button class
                        ]
                        
                        submit_button = None
                        for i, selector in enumerate(submit_selectors):
                            try:
                                submit_button = WebDriverWait(self.driver, 3).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                logging.info(f"✅ Found submit button with selector {i+1}: {selector}")
                                break
                            except:
                                logging.info(f"❌ Submit selector {i+1} failed")
                                continue
                        
                        if not submit_button:
                            logging.error("Could not find submit button with any selector")
                            continue
                            
                        submit_button.click()
                        logging.info(f"Successfully clicked submit button for domain: {alias}")
                        
                        # Wait for processing like a human would
                        time.sleep(random.uniform(5, 8))
                        
                        # Navigate back to domains page for next alias
                        self.driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                        time.sleep(random.uniform(3, 5))
                        
                    except Exception as e:
                        logging.error(f"Error adding domain '{alias}': {e}")
                        # Navigate back to domains page even if there was an error
                        try:
                            self.driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                            time.sleep(random.uniform(3, 5))
                        except:
                            pass
                        continue
                        
            logging.info("Finished adding all aliases.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred while adding domains: {e}")
            logging.error(f"An error occurred while adding domains: {e}")

    def handle_popups(self, row_index):
        try:
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.execute_script("return document.querySelector('div[role=\"dialog\"]') !== null")
            )
            logging.info(f"First pop-up detected for row {row_index}.")
            option_selected = self.driver.execute_script(
                """
                var optionInput = document.querySelector('div[role="dialog"] input[value="SkipMXRecord"]');
                if (optionInput) { optionInput.click(); return true; } else { return false; }
                """
            )
            if option_selected:
                logging.info(f"Selected 'Skip MX record setup' in the first pop-up for row {row_index}.")
            else:
                logging.warning(f"'Skip MX record setup' option not found in the first pop-up for row {row_index}.")
                return False
            next_button_selector = "#yDmH0d > div.llhEMd.iWO5td > div > div.g3VIld.m378kd.bWzMRb.z7d5bc.Up8vH.Whe8ub.hFEqNb.J9Nfi.iWO5td > span > div > div.nL3Jpb.J9fJmf > div.U26fgb.O0WRkf.oG5Srb.HQ8yf.C0oVfc.kHssdc.HvOprf.sPNV2d.M9Bg4d"
            next_button = self.driver.execute_script(f"return document.querySelector('{next_button_selector}');")
            if next_button:
                self.driver.execute_script("arguments[0].click();", next_button)
                logging.info(f"Clicked 'Next' button in the first pop-up for row {row_index}.")
            else:
                logging.warning(f"'Next' button not found in the first pop-up for row {row_index}.")
                return False
            time.sleep(2)
            second_popup_selector = "#yDmH0d > div.llhEMd.iWO5td > div > div.g3VIld.m378kd.bWzMRb.zyZLSb.Up8vH.Whe8ub.hFEqNb.J9Nfi.iWO5td > span"
            second_popup_present = self.driver.execute_script(f"return document.querySelector('{second_popup_selector}') !== null;")
            if second_popup_present:
                logging.info(f"Second pop-up detected for row {row_index}.")
                activate_button_selector = "#yDmH0d > div.llhEMd.iWO5td > div > div.g3VIld.m378kd.bWzMRb.zyZLSb.Up8vH.Whe8ub.hFEqNb.J9Nfi.iWO5td > span > div > div.nL3Jpb.J9fJmf > div.U26fgb.O0WRkf.oG5Srb.HQ8yf.C0oVfc.kHssdc.HvOprf.sPNV2d.M9Bg4d"
                activate_button = self.driver.execute_script(f"return document.querySelector('{activate_button_selector}');")
                if activate_button:
                    self.driver.execute_script("arguments[0].click();", activate_button)
                    logging.info(f"Clicked 'Activate' button in the second pop-up for row {row_index}.")
                else:
                    logging.warning(f"'Activate' button not found in the second pop-up for row {row_index}.")
                    return False
            else:
                logging.info(f"No second pop-up for row {row_index}. Continuing.")
            WebDriverWait(self.driver, 10).until_not(
                lambda driver: driver.execute_script("return document.querySelector('div[role=\"dialog\"]') !== null")
            )
            return True
        except Exception as e:
            logging.warning(f"Error handling pop-ups for row {row_index}: {e}")
            return False
    
    def activate_gmail(self):
        try:
            while True:
                buttons_found = False
                rows = self.driver.execute_script("return document.querySelectorAll('table tbody tr')")
                for i in range(2, len(rows) + 1):
                    activate_css_selector = f"table tbody tr:nth-child({i}) td.doVMIb.XgRaPc.TJaxrc.Ee8sQ.LmV4df.fNbT4e div div a"
                    try:
                        activate_button = self.driver.find_element(By.CSS_SELECTOR, activate_css_selector)
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", activate_button)
                        button_text = self.driver.execute_script("return arguments[0].innerText;", activate_button).strip()
                        if button_text.lower() in ["learn more", "already activated"]:
                            logging.info(f"Row {i} already activated. Skipping.")
                            continue
                        self.driver.execute_script("arguments[0].click();", activate_button)
                        buttons_found = True
                        logging.info(f"Clicked 'Activate Gmail' for row {i}.")
                        time.sleep(2)
                        attempts = 0
                        max_attempts = 3
                        while attempts < max_attempts:
                            if self.handle_popups(i):
                                break
                            else:
                                attempts += 1
                                logging.warning(f"Pop-up handling failed for row {i}. Retrying ({attempts}/{max_attempts})...")
                                time.sleep(2)
                        else:
                            logging.error(f"Failed to handle pop-ups for row {i} after {max_attempts} attempts. Skipping row.")
                            continue
                        WebDriverWait(self.driver, 30).until(
                            EC.invisibility_of_element_located((By.CSS_SELECTOR, activate_css_selector))
                        )
                        logging.info(f"Activation process completed for row {i}.")
                        time.sleep(2)
                    except TimeoutException:
                        logging.warning(f"Activate Gmail button not found for row {i}. Skipping.")
                        continue
                    except Exception as e:
                        logging.warning(f"Error processing 'Activate Gmail' for row {i}: {e}")
                        continue
                if not buttons_found:
                    logging.info("No more 'Activate Gmail' buttons found. Activation process complete.")
                    break
                self.driver.refresh()
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr"))
                )
            self.driver.get("https://admin.google.com/ac/domains/manage?hl=en")
            logging.info("Navigated to the admin manage domains page.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An error occurred while activating Gmail: {e}")
            logging.error(f"Error during Gmail activation: {e}")

    
    # --- New methods for Multi-Account processing ---
    def perform_login(self, driver, account, password):
        """
        Worker function for concurrent usage, logs a single account using the given driver.
        """
        try:
            driver.get("https://accounts.google.com/ServiceLogin?hl=en")
            email_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.ID, "identifierId"))
            )
            email_input.clear()
            email_input.send_keys(account)
            email_input.send_keys(Keys.ENTER)
            logging.info(f"Entered email for {account} and submitted.")
            password_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.NAME, "Passwd"))
            )
            password_input.clear()
            password_input.send_keys(password)
            password_input.send_keys(Keys.ENTER)
            logging.info(f"Entered password for {account} and submitted.")
            time.sleep(2)

            if "challenge/totp" in driver.current_url:
                logging.info(f"OTP challenge detected for {account}.")
                secret_key_file = self.get_secret_key_file(account)
                if secret_key_file:
                    with open(secret_key_file, 'r') as f:
                        secret_key = f.read().strip()
                    otp_code = self.generate_otp_code(secret_key)
                    if otp_code:
                        otp_input = WebDriverWait(driver, 15).until(
                            EC.element_to_be_clickable((By.XPATH, '//input[@type="tel"]'))
                        )
                        otp_input.clear()
                        otp_input.send_keys(otp_code)
                        otp_input.send_keys(Keys.ENTER)
                        logging.info(f"Entered OTP for {account} and submitted.")
                    else:
                        logging.error(f"Failed to generate OTP for {account}.")
                else:
                    logging.error(f"Secret key file not found for {account}.")

            WebDriverWait(driver, 15).until(
                lambda d: "myaccount.google.com" in d.current_url or "admin.google.com" in d.current_url
            )
            if "myaccount.google.com" in driver.current_url or "admin.google.com" in driver.current_url:
                driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                logging.info(f"Login successful for {account}.")
            else:
                logging.warning(f"Unexpected page after login for {account}.")
        except Exception as e:
            logging.error(f"Error during login for {account}: {e}")
            raise e
   
    def enable_admin_sdk_for_driver(self, driver, account, password, download_dir):
        """
        Full Flow:
        1) Navigate to Cloud Console and accept TOS if present.
        2) Search for Admin SDK API and enable it (if not already).
        3) Handle any popup and wait for redirect to the metrics page.
        4) Navigate to the Overview page and, if the 'Premier pas' button is found,
            execute the wizard steps; otherwise skip wizard setup.
        5) Visit the Audience sidebar and publish the application if not in production.
        6) Create OAuth client (ID Client) via the wizard.
        7) Finally, click the download button to download the client secret key in JSON format;
            wait for the download, then upload the JSON file to the server under a directory named after the account,
            saving it as "account.json".
        """
        try:
            # ===== ADD THIS VALIDATION BLOCK RIGHT HERE =====
            # VALIDATE INPUTS AT START
            if not account or not account.strip():
                logging.error("❌ Account parameter is empty or None")
                return False
                
            if not password or not password.strip():
                logging.error("❌ Password parameter is empty or None")
                return False
            
            account = account.strip()  # Clean any whitespace
            password = password.strip()
            
            logging.info(f"🚀 Starting Admin SDK setup for account: '{account}'")

            # -------------------------------------------
            # 1) NAVIGATE & ACCEPT TOS IF ANY
            # -------------------------------------------
            cloud_console_url = "https://console.cloud.google.com/welcome?_hl=en"
            driver.get(cloud_console_url)
            logging.info("Navigated to Cloud Console.")
            time.sleep(2)
            try:
                tos_container = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//mat-dialog-container"))
                )
                logging.info("TOS dialog detected; accepting terms.")
                
                # Wait for dialog to fully load
                time.sleep(3)
                
                # Very gentle checkbox interaction
                try:
                    # Find all checkboxes and interact gently
                    checkboxes = WebDriverWait(driver, 10).until(
                        EC.presence_of_all_elements_located((By.XPATH, "//mat-checkbox//input[@type='checkbox']"))
                    )
                    
                    for i, checkbox in enumerate(checkboxes):
                        try:
                            # Scroll to checkbox first
                            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", checkbox)
                            time.sleep(1)
                            
                            # Check if already checked
                            if not checkbox.is_selected():
                                # Use the gentlest click method
                                ActionChains(driver).move_to_element(checkbox).pause(0.5).click().perform()
                                logging.info(f"Gently checked checkbox {i+1}")
                                time.sleep(1)
                            else:
                                logging.info(f"Checkbox {i+1} already checked")
                                
                        except Exception as e:
                            logging.warning(f"Could not interact with checkbox {i+1}: {e}")
                            continue
                    
                    time.sleep(2)

                    # Enhanced accept button finding with multiple selectors
                    accept_selectors = [
                        # Using the exact structure you provided
                        "//button[.//span[contains(text(), 'Agree and continue')]]",
                        "//span[contains(text(), 'Agree and continue')]/ancestor::button",
                        
                        # Fallback selectors
                        "//button[contains(@class, 'mat-mdc-button') and .//span[contains(text(), 'Agree')]]",
                        "//button[contains(@class, 'mdc-button') and .//span[contains(text(), 'continue')]]",
                        "//cfc-progress-button//button",
                        "//button[@color='primary']",
                        
                        # Last resort
                        "//mat-dialog-actions//button",
                        "//div[contains(@class, 'mat-dialog')]//button[last()]"
                    ]

                    button_clicked = False
                    for i, selector in enumerate(accept_selectors):
                        try:
                            accept_button = WebDriverWait(driver, 8).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            
                            # Scroll to button first
                            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", accept_button)
                            time.sleep(1)
                            
                            # Try multiple click methods
                            try:
                                # Method 1: ActionChains
                                ActionChains(driver).move_to_element(accept_button).pause(1).click().perform()
                                logging.info(f"Clicked accept button with selector {i+1} using ActionChains")
                                button_clicked = True
                                break
                            except:
                                # Method 2: JavaScript click
                                driver.execute_script("arguments[0].click();", accept_button)
                                logging.info(f"Clicked accept button with selector {i+1} using JavaScript")
                                button_clicked = True
                                break
                                
                        except TimeoutException:
                            logging.info(f"Accept button selector {i+1} failed")
                            continue
                        except Exception as e:
                            logging.warning(f"Error with accept selector {i+1}: {e}")
                            continue

                    if button_clicked:
                        # Wait longer for dialog to fully close and page to process
                        try:
                            WebDriverWait(driver, 20).until(
                                EC.invisibility_of_element_located((By.XPATH, "//mat-dialog-container"))
                            )
                            logging.info("TOS dialog closed successfully.")
                            
                            # Additional wait to ensure page processes the acceptance
                            time.sleep(5)
                            
                        except TimeoutException:
                            logging.warning("Dialog didn't close as expected, but continuing...")
                    else:
                        logging.error("Could not click any accept button")
                    
                except TimeoutException as e:
                    logging.error(f"Timeout during TOS interaction: {e}")
                    # Try to continue anyway
                    
            except TimeoutException:
                logging.info("No TOS dialog found; continuing.")
            except Exception as e:
                logging.error(f"Error during TOS handling: {e}")
                # Continue anyway to prevent total failure
        
            time.sleep(2)
        
            # -------------------------------------------
            # 2) SEARCH FOR ADMIN SDK API
            # -------------------------------------------
            # Navigate to the API library page
            driver.get("https://console.cloud.google.com/apis/library?inv=1&hl=en")
            logging.info("Navigated to the API library page.")
            time.sleep(3)
            
            # Locate the search input element using the provided XPath and type "Admin SDK API"
            search_input = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH,
                    "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/mp-api-lib-home-page/mp-browse-base/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/mp-api-library-search-banner/div/div[2]/div/mp-search-input-container/div/mat-form-field/div[1]/div/div[3]/input"
                ))
            )
            search_input.clear()
            search_input.send_keys("Admin SDK API")
            logging.info("Typed 'Admin SDK API' in the API library search bar.")
            time.sleep(1)
            search_input.send_keys(Keys.ENTER)
            logging.info("Pressed ENTER after entering the search term.")
            time.sleep(2)
            
            # Click on the first search result using the provided XPath
            result_button = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.XPATH,
                    "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/mp-api-lib-browse-page/mp-browse-base/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/div/div/div/mp-search-results-list/mp-search-results-list-item[1]/a"
                ))
            )
            result_button.click()
            logging.info("Clicked on the first search result for Admin SDK API.")
            time.sleep(2)
                    
            # -------------------------------------------
            # 3) CLICK "ENABLE" BUTTON & WAIT FOR REDIRECT
            # -------------------------------------------
            try:
                enable_button = WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/mp-details-page/mp-details-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/mp-product-details-banner/section/div/cfc-product-header/div/div/div/section/mp-product-details-cta-button-container/mp-product-details-cta-button[1]/cfc-progress-button/div[1]/button"
                    ))
                )
                ActionChains(driver).move_to_element(enable_button).pause(1).click().perform()
                logging.info("Clicked 'Enable' for Admin SDK.")
                time.sleep(2)
                try:
                    popup_option = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.XPATH,
                            "//mat-dialog-container//cfc-table-columns-presenter//table/tbody/tr/td[4]/div/a"))
                    )
                    logging.info("Pop-up detected after enabling Admin SDK.")
                    time.sleep(1)
                    ActionChains(driver).move_to_element(popup_option).pause(1).click().perform()
                    logging.info("Selected the required option in the pop-up.")
                except TimeoutException:
                    logging.info("No pop-up to select. Proceeding.")
        
                WebDriverWait(driver, 30).until(
                    lambda d: "admin.googleapis.com/metrics" in d.current_url.lower()
                )
                logging.info("Redirected to metrics page => Admin SDK is enabled.")
            except TimeoutException:
                logging.info("Enable button not found or already enabled, skipping enable step.")
        
            if "admin.googleapis.com/metrics" in driver.current_url.lower():
                logging.info("Confirmed: Admin SDK is enabled.")
            else:
                logging.warning("Did not see admin.googleapis.com/metrics; likely already enabled or UI changed.")
            time.sleep(2)
        
            # -------------------------------------------
            # 4) NAVIGATE TO OVERVIEW PAGE & DETECT 'PREMIER PAS'
            # -------------------------------------------
            overview_url = "https://console.cloud.google.com/auth/overview?inv=1&invt=&hl=en"
            driver.get(overview_url)
            logging.info("Navigated to Overview page.")
            time.sleep(5)
        
            premier_pas_elements = driver.find_elements(
                By.XPATH,
                "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/oauth-empty-state/cfc-empty-state/div/cfc-empty-state-actions/a"
            )
            normal_premier_pas_found = bool(premier_pas_elements)
        
            if normal_premier_pas_found:
                try:
                    get_started_button = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/oauth-empty-state/cfc-empty-state/div/cfc-empty-state-actions/a"
                        ))
                    )
                    ActionChains(driver).move_to_element(get_started_button).pause(1).click().perform()
                    logging.info("Clicked 'Premier pas' to open OAuth creation wizard.")
                except TimeoutException:
                    logging.warning("Premier pas button is found but not clickable; skipping wizard setup.")
            else:
                logging.info("'Premier pas' button not found; likely already done. Skipping wizard setup.")
        
            time.sleep(3)
        
            # -------------------------------------------
            # 5) COMPLETE THE OAUTH WIZARD STEPS (if applicable)
            # -------------------------------------------
            if normal_premier_pas_found:
                try:
                    # Step 8: Enter random name
                    random_name_field = WebDriverWait(driver, 30).until(
                        EC.presence_of_element_located((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[1]/div/div/div/div[1]/mat-form-field[1]/div[1]/div/div[2]/input"
                        ))
                    )
                    random_app_name = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
                    random_name_field.clear()
                    random_name_field.send_keys(random_app_name)
                    logging.info(f"Entered random name: {random_app_name}")
                    time.sleep(1)
        
                    # Handle dropdown selection via TAB/SPACE/ENTER
                    driver.switch_to.active_element.send_keys(Keys.TAB)
                    time.sleep(1)
                    driver.switch_to.active_element.send_keys(Keys.SPACE)
                    time.sleep(1)
                    driver.switch_to.active_element.send_keys(Keys.ENTER)
                    logging.info("Handled dropdown via TAB, SPACE, ENTER.")
                    time.sleep(1)
        
                    # Step 9: Click 'suivant' for Step 1
                    suivant_button_step1 = WebDriverWait(driver, 30).until(
                        EC.element_to_be_clickable((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[1]/div/div/div/div[2]/button"
                        ))
                    )
                    ActionChains(driver).move_to_element(suivant_button_step1).pause(1).click().perform()
                    logging.info("Clicked 'suivant' for Step 1.")
                    time.sleep(2)
        
                    # Step 10: Select second radio option
                    second_option = WebDriverWait(driver, 30).until(
                        EC.element_to_be_clickable((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[2]/div/div/div/div[1]/mat-radio-group/div[3]/mat-radio-button"
                        ))
                    )
                    second_option.click()
                    logging.info("Selected the second radio option.")
                    time.sleep(1)
        
                    # Step 11: Click 'suivant' for Step 2
                    suivant_button_step2 = WebDriverWait(driver, 30).until(
                        EC.element_to_be_clickable((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[2]/div/div/div/div[2]/button"
                        ))
                    )
                    ActionChains(driver).move_to_element(suivant_button_step2).pause(1).click().perform()
                    logging.info("Clicked 'suivant' for Step 2.")
                    time.sleep(3)
        
                    # Step 12: Enter login email
                    email = self.email_entry.text().strip()
                    
                    # If user didn't type anything, use a random Gmail address
                    if not email:
                        random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
                        email = f"random{random_suffix}@gmail.com"
                    
                    logging.info(f"Using email: {email} for OAuth setup")
                    
                    # Find and interact with the email field
                    email_field = WebDriverWait(driver, 30).until(
                        EC.presence_of_element_located((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[3]/div/div/div/div[1]/apis-email-chip-list/form/mat-form-field/div[1]/div/div[2]/mat-chip-grid/div/input"
                        ))
                    )
                    
                    # Use more robust input method
                    driver.execute_script("arguments[0].value = '';", email_field)  # Clear using JavaScript
                    email_field.send_keys(email)
                    email_field.send_keys(Keys.TAB)  # Add TAB to confirm the email entry
                    logging.info(f"Entered login email: {email}")
                    time.sleep(3)
                    
                    # Step 13: Click 'suivant' for Step 3
                    suivant_button_step3 = WebDriverWait(driver, 30).until(
                        EC.element_to_be_clickable((By.XPATH,
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[3]/div/div/div/div[2]/button"
                        ))
                    )
                    ActionChains(driver).move_to_element(suivant_button_step3).pause(1).click().perform()
                    logging.info("Clicked 'suivant' for Step 3.")
                    time.sleep(2)
                    
                    # Step 14: Accept conditions checkbox - Multiple strategies
                    checkbox_clicked = False

                    # Strategy 1: Direct click on the touch target area
                    try:
                        touch_target = WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.XPATH, "//div[@class='mat-mdc-checkbox-touch-target']"))
                        )
                        driver.execute_script("arguments[0].click();", touch_target)
                        logging.info("Clicked checkbox via touch target.")
                        checkbox_clicked = True
                    except:
                        logging.info("Touch target method failed.")

                    # Strategy 2: Click the mat-checkbox container if touch target failed
                    if not checkbox_clicked:
                        try:
                            checkbox_container = WebDriverWait(driver, 10).until(
                                EC.element_to_be_clickable((By.XPATH, "//mat-checkbox[@formcontrolname='termsAgreement']"))
                            )
                            driver.execute_script("arguments[0].click();", checkbox_container)
                            logging.info("Clicked checkbox via container.")
                            checkbox_clicked = True
                        except:
                            logging.info("Container method failed.")

                    # Strategy 3: Force click using coordinates if other methods failed
                    if not checkbox_clicked:
                        try:
                            checkbox_input = WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.ID, "_0rif_mat-mdc-checkbox-0-input"))
                            )
                            # Scroll into view first
                            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", checkbox_input)
                            time.sleep(1)
                            
                            # Use ActionChains to click at the element location
                            ActionChains(driver).move_to_element(checkbox_input).pause(0.5).click().perform()
                            logging.info("Clicked checkbox via ActionChains.")
                            checkbox_clicked = True
                        except:
                            logging.info("ActionChains method failed.")

                    # Strategy 4: Last resort - JavaScript property setting
                    if not checkbox_clicked:
                        try:
                            checkbox_input = driver.find_element(By.ID, "_0rif_mat-mdc-checkbox-0-input")
                            driver.execute_script("arguments[0].checked = true; arguments[0].dispatchEvent(new Event('change'));", checkbox_input)
                            logging.info("Set checkbox via JavaScript property.")
                            checkbox_clicked = True
                        except:
                            logging.info("JavaScript property method failed.")

                    # Step 4: Click 'Weiter' button using exact XPath like other steps
                    try:
                        suivant_button_step4 = WebDriverWait(driver, 30).until(
                            EC.element_to_be_clickable((By.XPATH,
                                "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/cfc-stepper-step[4]/div/div/div/div[2]/button"
                            ))
                        )
                        ActionChains(driver).move_to_element(suivant_button_step4).pause(1).click().perform()
                        logging.info("Clicked 'suivant' for Step 4.")
                        time.sleep(2)
                    except TimeoutException as e:
                        logging.error(f"Step 4 suivant button not found: {e}")
                        return False
                    except Exception as e:
                        logging.error(f"Error clicking Step 4 suivant button: {e}")
                        return False
        
                    # Step 15: Click 'Create' button (after Continue was clicked)
                    try:
                        # Wait a bit for the wizard to process the Continue click
                        time.sleep(2)
                        
                        # Look for Create button with multiple selectors
                        create_button_xpath = (
                            "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/cfc-stepper/div/div/cfc-progress-button/div[1]/button"
                        )

                        try:
                            create_button = WebDriverWait(driver, 20).until(
                                EC.element_to_be_clickable((By.XPATH, create_button_xpath))
                            )
                            ActionChains(driver).move_to_element(create_button).pause(1).click().perform()
                            logging.info("Clicked 'Create' button using exact XPath.")
                            create_clicked = True
                        except TimeoutException:
                            logging.error("Could not find Create button with exact XPath.")
                            create_clicked = False
                            
                        if not create_clicked:
                            logging.error("Could not find or click Create button.")
                            return False  # Stop the process if Create fails
                        
                        # VERIFY CREATE SUCCESS - Check for redirect or success indicators
                        success_verified = False
                        for attempt in range(3):  # Try 3 times, 5 seconds apart
                            time.sleep(5)
                            
                            try:
                                # Check for success indicators
                                if ("overview" in driver.current_url.lower() or 
                                    driver.find_elements(By.XPATH, "//a[contains(@aria-label, 'Audience')]")):
                                    success_verified = True
                                    logging.info("✅ Create button success verified - wizard completed")
                                    break
                            except:
                                pass
                            
                            logging.info(f"Verifying Create success - attempt {attempt + 1}")
                        
                        if not success_verified:
                            logging.error("❌ Create button may have failed - no success indicators found")
                            return False
                        
                        # Additional wait for all redirects to complete
                        time.sleep(5)
                        logging.info("✅ Create process completed successfully")
                            
                    except Exception as e:
                        logging.error(f"Error clicking Create button: {e}")
                        return False

                except TimeoutException as te:
                    logging.warning(f"Wizard steps timed out: {te}. Possibly already configured.")
                except Exception as ex:
                    logging.error(f"Error in 'Premier pas' wizard steps: {ex}")
        
            # -------------------------------------------
            # 6) VISIT 'AUDIENCE' AND PUBLISH IF NOT IN PRODUCTION
            # -------------------------------------------
            try:
                # Wait for wizard completion and auto-redirects (10 seconds)
                time.sleep(10)
                logging.info("Waiting for wizard completion and redirects...")
                
                # Step 1: Navigate to Audience using the link (not sidebar)
                audience_clicked = False
                audience_selectors = [
                    "//a[contains(@aria-label, 'Audience') and contains(@aria-label, '3 of')]",
                    "//a[contains(text(), 'Audience') and contains(@aria-label, '3 of')]",
                    "//a[@href and contains(@href, '/auth/audience')]"
                ]
                
                for selector in audience_selectors:
                    try:
                        audience_link = WebDriverWait(driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        driver.execute_script("arguments[0].click();", audience_link)
                        logging.info(f"✅ Clicked Audience link with selector: {selector}")
                        audience_clicked = True
                        break
                    except:
                        continue
                
                if not audience_clicked:
                    logging.error("❌ Could not find Audience link")
                    return False
                
                # Wait for page load
                time.sleep(5)
                
                # Step 2: Check if app needs publishing
                already_published = False
                try:
                    WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'En production') or contains(text(),'In production')]"))
                    )
                    already_published = True
                    logging.info("App is already published")
                except:
                    logging.info("App not published yet. Attempting to publish...")
                
                if not already_published:
                    # Step 3: Click "Publish app" button with multiple language support
                    publish_clicked = False
                    publish_selectors = [
                        # Based on your HTML structure - universal selectors first
                        "//button[contains(@class, 'mat-mdc-outlined-button') and contains(@class, 'mat-primary')]",
                        "//button[@mat-raised-button and contains(@class, 'cfc-button-small')]",
                        "//div[contains(@class, 'cfc-space-above-minus-3')]//button",
                        
                        # Language-specific selectors (multiple languages)
                        "//button[.//span[contains(text(), 'Publish app') or contains(text(), 'Publier') or contains(text(), 'Publicar') or contains(text(), 'Veröffentlichen')]]",
                        "//span[contains(text(), 'Publish app') or contains(text(), 'Publier')]/parent::button",
                        "//button[contains(text(), 'Publish') or contains(text(), 'Publier')]",
                        
                        # Fallback selectors
                        "//button[@jslog='236680;track:generic_click']",
                        "//button[contains(@class, 'mat-mdc-outlined-button')]"
                    ]
                    
                    for i, selector in enumerate(publish_selectors):
                        try:
                            publish_button = WebDriverWait(driver, 3).until(
                                EC.element_to_be_clickable((By.XPATH, selector))
                            )
                            driver.execute_script("arguments[0].click();", publish_button)
                            logging.info(f"✅ Clicked 'Publish app' button with selector {i+1}")
                            publish_clicked = True
                            break
                        except:
                            logging.info(f"❌ Publish selector {i+1} failed")
                            continue
                    
                    if not publish_clicked:
                        logging.error("❌ Could not find 'Publish app' button with any selector")
                        return False
                    
                    # Step 4: Handle "Confirm" dialog with multiple languages
                    try:
                        confirm_selectors = [
                            # Universal selectors first
                            "//div[contains(@class, 'cfc-progress-button-resolved')]//button",
                            "//button[contains(@class, 'mat-primary') and contains(@class, 'mdc-button')]",
                            
                            # Language-specific (multiple languages)
                            "//button[contains(text(), 'Confirm') or contains(text(), 'Confirmer') or contains(text(), 'Confirmar') or contains(text(), 'Bestätigen')]",
                            "//span[contains(text(), 'Confirm') or contains(text(), 'Confirmer')]/parent::button"
                        ]
                        
                        confirm_clicked = False
                        for i, selector in enumerate(confirm_selectors):
                            try:
                                confirm_button = WebDriverWait(driver, 3).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                driver.execute_script("arguments[0].click();", confirm_button)
                                logging.info(f"✅ Clicked Confirm button with selector {i+1}")
                                confirm_clicked = True
                                break
                            except:
                                continue
                        
                        if not confirm_clicked:
                            logging.error("❌ Could not find Confirm button")
                            return False
                        
                        # Wait for publish to complete
                        time.sleep(5)
                        logging.info("✅ Publish process completed")
                        
                    except Exception as e:
                        logging.error(f"❌ Error handling Confirm dialog: {e}")
                        return False
                
                # Step 5: Navigate to Clients section
                clients_clicked = False
                clients_selectors = [
                    "//a[@href and contains(@href, '/credentials')]",           # Most reliable
                    "//a[@href and contains(@href, '/auth/credentials')]",      # Alternative URL
                    "//a[contains(@aria-label, 'Clients')]",                   # Any clients mention
                    "//a[contains(text(), 'Clients') or contains(text(), 'Client')]"  # Text fallback
                ]                
                for selector in clients_selectors:
                    try:
                        clients_link = WebDriverWait(driver, 5).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        driver.execute_script("arguments[0].click();", clients_link)
                        logging.info(f"✅ Clicked Clients link")
                        clients_clicked = True
                        break
                    except:
                        continue
                
                if not clients_clicked:
                    logging.error("❌ Could not find Clients link")
                    return False
                
                # Wait for clients page to load
                time.sleep(5)
                logging.info("✅ Successfully navigated to Clients section")

            except Exception as e:
                logging.error(f"Error in Audience/Publish section: {e}")
                return False
        
            # =====================================
            # 7) OAUTH CLIENT CREATION & DOWNLOAD JSON KEY
            # =====================================
            logging.info("Proceeding to create OAuth client on the Clients page.")
            
            # We are already on the Clients page, so no need to navigate to sidebar
            # Just wait a moment for the page to fully load
            time.sleep(2)
            
            #
            # 7-B) Try multiple strategies to locate and click "+ Create client" button
            #
            did_click_add_client = False
            
            # Strategy 1: Search for "+ Create client" button by text (most reliable for new interface)
            try:
                create_client_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Create client') or contains(text(), '+ Create client')]"))
                )
                create_client_button.click()
                logging.info("Clicked '+ Create client' button using text-based locator.")
                did_click_add_client = True
            except TimeoutException:
                logging.warning("Text-based '+ Create client' button not found. Trying alternative strategies.")
            
            # Strategy 2: Try to find button with plus icon and "Create client" text
            if not did_click_add_client:
                try:
                    create_client_with_plus = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'mat-raised-button') and contains(text(), 'Create client')]"))
                    )
                    create_client_with_plus.click()
                    logging.info("Clicked '+ Create client' button using Material Design button locator.")
                    did_click_add_client = True
                except TimeoutException:
                    logging.warning("Material Design button locator failed. Trying XPath strategy.")
            
            # Strategy 3: Use the provided XPath as fallback
            if not did_click_add_client:
                try:
                    add_client_xpath = (
                        "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-action-bar/cfc-action-bar-legacy/mat-toolbar/div[3]/div/div/div[1]/div/clients-list-actions/a"
                    )
                    add_client_button = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, add_client_xpath))
                    )
                    add_client_button.click()
                    logging.info("Clicked 'Create client' link using provided XPath.")
                    did_click_add_client = True
                except TimeoutException:
                    logging.warning("Provided XPath 'Create client' link not found. Trying generic button strategies.")
            
            # Strategy 4: Try generic button strategies for Google Cloud interface
            if not did_click_add_client:
                try:
                    # Look for any button containing "Create" in the main content area
                    generic_create_button = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Create') and contains(text(), 'client')]"))
                    )
                    generic_create_button.click()
                    logging.info("Clicked 'Create client' button using generic button strategy.")
                    did_click_add_client = True
                except TimeoutException:
                    logging.warning("Generic button strategy failed. Will try fallback approaches.")
            
            time.sleep(1)
            
            #
            # 7-C) If normal link not found, do the SMALL WINDOW approach:
            #      1) Click the small menu button,
            #      2) Then click the "Add/Create client" from the drop-down.
            #
            if not did_click_add_client:
                try:
                    # This might be the smaller button in the toolbar that opens a drop-down
                    small_menu_button_xpath = (
                        "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-action-bar/cfc-action-bar-legacy/mat-toolbar/div[3]/div/div/div[1]/div/clients-list-actions/a/cfc-icon/mat-icon/svg"
                    )
                    small_menu_button = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, small_menu_button_xpath))
                    )
                    small_menu_button.click()
                    logging.info("Clicked the small menu button (fallback approach).")
            
                    time.sleep(1)  # Let the drop-down appear
            
                    # Now click the actual "Add/Create client" item in that drop-down
                    dropdown_item_xpath = (
                        "/html/body/div[10]/div[2]/div/div/cfc-action-bar-menu-item/div/clients-list-actions/a/span[2]"
                    )
                    dropdown_item = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, dropdown_item_xpath))
                    )
                    dropdown_item.click()
                    logging.info("Clicked the 'Add/Create client' item from the drop-down (fallback).")
            
                    did_click_add_client = True
                except TimeoutException:
                    logging.warning("Fallback approach also failed to find 'Add/Create client' in small window drop-down.")
            
            # If after both attempts, still not done, we bail.
            if not did_click_add_client:
                logging.error("Both normal approach and fallback approach failed.")
                QMessageBox.critical(self, "Error", "Cannot open the Add/Create client dialog by any approach.")
                return False
            
            time.sleep(2)
            
            #
            # 7-D) Now that we've triggered Add/Create client, continue with your existing logic:
            #      selecting client type, pressing 'Create', etc.
            #
            try:
                # Example: select client type from dropdown
                dropdown_xpath = (
                    "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/mat-form-field/div[1]/div/div[2]/cfc-select/div"
                )
                dropdown_field = WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable((By.XPATH, dropdown_xpath))
                )
                dropdown_field.click()
                time.sleep(1)
                actions = ActionChains(driver)
                actions.send_keys(Keys.SPACE).perform()
                logging.info("Selected 'Web application' as OAuth client type.")
            except TimeoutException:
                logging.error("Could not select the OAuth client type from dropdown.")
                QMessageBox.critical(self, "Error", "Cannot pick the correct client type.")
                return False
            
            time.sleep(1)
            # Force dismiss snackbar by multiple methods
            snackbar_dismissed = False
            max_attempts = 5

            for attempt in range(max_attempts):
                try:
                    # Check if snackbar is still present
                    snackbars = driver.find_elements(By.XPATH, "//div[contains(@class, 'mat-mdc-snack-bar-label')]")
                    
                    if not snackbars:
                        logging.info(f"No snackbar found on attempt {attempt + 1}")
                        snackbar_dismissed = True
                        break
                        
                    logging.info(f"Snackbar still present, attempt {attempt + 1} to dismiss")
                    
                    # Method 1: Try to click the snackbar action button if it exists
                    try:
                        action_button = driver.find_element(By.XPATH, "//div[contains(@class, 'mat-mdc-snack-bar-action')]//button")
                        action_button.click()
                        logging.info("Clicked snackbar action button")
                        time.sleep(1)
                    except:
                        pass
                        
                    # Method 2: Click outside the snackbar area
                    try:
                        # Click on the main content area, far from the snackbar
                        driver.execute_script("document.elementFromPoint(500, 300).click();")
                        time.sleep(0.5)
                    except:
                        pass
                        
                    # Method 3: Force remove snackbar with JavaScript
                    try:
                        driver.execute_script("""
                            var snackbars = document.querySelectorAll('.mat-mdc-snack-bar-container, .mdc-snackbar');
                            snackbars.forEach(function(snackbar) {
                                snackbar.remove();
                            });
                        """)
                        logging.info("Force removed snackbar with JavaScript")
                        time.sleep(1)
                    except Exception as e:
                        logging.warning(f"JavaScript removal failed: {e}")
                        
                    # Method 4: Press multiple keys to dismiss
                    try:
                        ActionChains(driver).send_keys(Keys.ESCAPE).send_keys(Keys.ESCAPE).perform()
                        time.sleep(0.5)
                    except:
                        pass
                        
                    # Wait and check again
                    time.sleep(2)
                    
                except Exception as e:
                    logging.warning(f"Error in snackbar dismissal attempt {attempt + 1}: {e}")
                    time.sleep(1)

            # Additional wait for UI to fully stabilize
            time.sleep(3)
            logging.info("Completed aggressive snackbar dismissal, UI should be stable now")

            # Click the correct "+ Add URI" under Authorized redirect URIs
            add_uri_xpath = (
                "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/services-oauth-client-form/form/services-oauth-client-editor/div/services-oauth-client-web/cfc-form-stack[2]/fieldset/cfc-form-section/div[2]/div/button"
            )

            add_uri_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, add_uri_xpath))
            )
            add_uri_button.click()
            logging.info("Clicked Add URI button using exact XPath")
            time.sleep(1)

            # Find the input field using exact XPath
            redirect_uri_input_xpath = (
                "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/services-oauth-client-form/form/services-oauth-client-editor/div/services-oauth-client-web/cfc-form-stack[2]/fieldset/cfc-form-section/div[2]/div[1]/cfc-form-stack-row/cfc-form-stack-input-wrapper/mat-form-field/div[1]/div/div[2]/input"
            )

            redirect_uri_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, redirect_uri_input_xpath))
            )

            redirect_uri_input.clear()
            redirect_uri_input.send_keys("https://ecochains.online/oauth-callback")
            logging.info("Entered redirect URI using exact XPath")
            time.sleep(1)

            try:
                # Final 'Create' button
                create_button_xpath = (
                    "/html/body/div[1]/div[3]/div[3]/div/div[1]/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/form/services-oauth-client-form/form/cfc-progress-button/div[1]/button"
                )
                create_client_button = WebDriverWait(driver, 20).until(
                    EC.element_to_be_clickable((By.XPATH, create_button_xpath))
                )
                create_client_button.click()
                logging.info("Clicked final 'Create' button for the OAuth client.")
            except TimeoutException:
                logging.error("Unable to find 'Create' button for OAuth client.")
                QMessageBox.critical(self, "Error", "Cannot finalize OAuth client creation.")
                return False
            
            time.sleep(2)
            
            logging.info("Proceeding with JSON key download & save logic...")
            
            
            # -------------------------------------------
            # DOWNLOAD & SAVE CLIENT SECRET KEY (JSON)
            # -------------------------------------------
            # Use the actual download directory where files are saved
            # download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            logging.info(f"Using download directory: {download_dir}")
            
            # First attempt: try to locate the primary download button using an explicit XPath
            download_buttons = driver.find_elements(
                By.XPATH, 
                "/html/body/div[1]/div[3]/div[3]/div/pan-shell/pcc-shell/cfc-panel-container/div/div/cfc-panel/div/div/div[3]/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-container/div/div/cfc-panel[2]/div/div/central-page-area/div/div/pcc-content-viewport/div/div/pangolin-home-wrapper/pangolin-home/cfc-router-outlet/div/ng-component/cfc-single-panel-layout/cfc-panel-container/div/div/cfc-panel/div/div/cfc-panel-body/cfc-virtual-viewport/div[1]/div/services-oauth-clients-table-oauth-clients-gql/cfc-table/div[3]/cfc-table-columns-presenter-v2/div/div[3]/table/tbody/tr[1]/td[6]/button[2]/cfc-icon/mat-icon"
            )
            if not download_buttons:
                # Fallback: look for a button with a specific track-name attribute or common download icons
                download_buttons = driver.find_elements(
                    By.XPATH, 
                    "//button[@track-name='oAuthSecretDownloadSuccess'] | //mat-icon[contains(text(), 'cloud_download') or contains(text(), 'download') or contains(text(), 'get_app')]/.."
                )
            if download_buttons:
                try:
                    ActionChains(driver).move_to_element(download_buttons[0]).pause(1).click().perform()
                    logging.info("Clicked download button to initiate client secret key download.")
                except Exception as e:
                    logging.error(f"Error clicking primary download button: {e}")
                    raise Exception("Download button not clickable")
            else:
                try:
                    download_button_xpath = (
                        "//table//tbody//tr[1]//td[last()]//button[2]//mat-icon | "
                        "//table//tbody//tr[1]//button[contains(@aria-label, 'Download')]"
                    )
                    download_button = WebDriverWait(driver, 20).until(
                        EC.element_to_be_clickable((By.XPATH, download_button_xpath))
                    )
                    ActionChains(driver).move_to_element(download_button).pause(1).click().perform()
                    logging.info("Clicked download button using alternative XPath.")
                except TimeoutException:
                    logging.error("Couldn't find download button with any method.")
                    raise Exception("Download button not found")
            
            time.sleep(2)
            
            # Look for the download dialog using multiple selectors
            dialog_found = False
            dialog_selectors = [
                "//mat-dialog-container",
                "//div[contains(@class, 'mat-dialog-container')]",
                "//div[contains(@role, 'dialog')]"
            ]
            for selector in dialog_selectors:
                try:
                    WebDriverWait(driver, 10).until(
                        EC.visibility_of_element_located((By.XPATH, selector))
                    )
                    dialog_found = True
                    logging.info(f"Found download dialog using selector: {selector}")
                    break
                except TimeoutException:
                    continue
            if not dialog_found:
                logging.warning("No dialog detected, trying to proceed anyway.")
            
            # Attempt to find and click the download JSON button based on its visible text
            download_json_selectors = [
                "//mat-dialog-container//button[contains(., 'Télécharger au format JSON')]",
                "//*[contains(text(),'Télécharger au format JSON') or contains(text(),'Download JSON')]",
                "//*[contains(text(),'JSON') and (contains(text(),'télécharger') or contains(text(),'download'))]",
                "//mat-dialog-container//button[contains(@class, 'mat-button-base')]",
                "//div[contains(@role, 'dialog')]//button"
            ]
            download_clicked = False
            for selector in download_json_selectors:
                try:
                    buttons = driver.find_elements(By.XPATH, selector)
                    for button in buttons:
                        try:
                            button_text = button.text.lower()
                            if 'json' in button_text or 'télécharger' in button_text or 'download' in button_text:
                                ActionChains(driver).move_to_element(button).pause(1).click().perform()
                                logging.info(f"Clicked download JSON button with text: {button.text}")
                                download_clicked = True
                                break
                        except Exception:
                            continue
                    if download_clicked:
                        break
                except Exception:
                    continue
            if not download_clicked:
                # Final attempt: click the first button found in a known dialog location
                try:
                    buttons = driver.find_elements(By.XPATH, "/html/body/div[10]/div[3]/div/mat-dialog-container/div/div/created-client-dialog/div[1]/oauth-created-client-dialog-actions/div[2]/oauth-download-created-client/div/div/div/button")
                    if buttons:
                        ActionChains(driver).move_to_element(buttons[0]).pause(1).click().perform()
                        logging.info("Last resort: clicked first button in dialog")
                        download_clicked = True
                except Exception:
                    pass
            if not download_clicked:
                logging.warning("Could not click download JSON button, but continuing to check for downloaded file...")
            
            # Wait for file download with better validation
            downloaded_file = None
            start_time = time.time()
            timeout_sec = 60

            while time.time() - start_time < timeout_sec:
                if os.path.exists(download_dir):
                    # Look for ALL JSON files, not just new ones
                    json_files = [f for f in os.listdir(download_dir) 
                                if f.endswith('.json') and not f.endswith('.crdownload')]
                    
                    if json_files:
                        # Get the most recently modified JSON file
                        newest_file = max(json_files, 
                                        key=lambda x: os.path.getmtime(os.path.join(download_dir, x)))
                        full_path = os.path.join(download_dir, newest_file)
                        
                        # Check if this file was modified within the last 2 minutes (recent download)
                        file_age = time.time() - os.path.getmtime(full_path)
                        if file_age < 120:  # File modified within last 2 minutes
                            # Verify file has content and validate JSON
                            if os.path.getsize(full_path) > 100:
                                try:
                                    with open(full_path, 'r', encoding='utf-8') as f:
                                        json_content = f.read()
                                    
                                    logging.info(f"JSON file content preview: {json_content[:200]}...")  # First 200 chars
                                    
                                    # VALIDATE the JSON belongs to our process
                                    json_data = json.loads(json_content)
                                    
                                    logging.info(f"JSON keys found: {list(json_data.keys())}")  # Show what keys exist
                                    
                                    # FIXED: Check for nested structure (web.client_id)
                                    has_client_data = False
                                    if 'web' in json_data:
                                        web_data = json_data['web']
                                        if 'client_id' in web_data and 'client_secret' in web_data:
                                            has_client_data = True
                                            logging.info("✅ Found OAuth credentials in 'web' object")
                                    elif 'client_id' in json_data and 'client_secret' in json_data:
                                        has_client_data = True
                                        logging.info("✅ Found OAuth credentials at root level")
                                    
                                    if has_client_data:
                                        downloaded_file = full_path
                                        logging.info(f"✅ Found recent JSON file for {account}: {newest_file}")
                                        break
                                    else:
                                        logging.warning(f"❌ No valid OAuth credentials found in JSON structure")
                                        
                                except Exception as e:
                                    logging.error(f"JSON parsing error: {e}")
                                    continue                
                time.sleep(1)

            # THIS SECTION MUST BE OUTSIDE THE WHILE LOOP
            if not downloaded_file:
                # Fallback: Check default Downloads folder
                default_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                logging.info(f"File not found in custom dir, checking default Downloads: {default_downloads}")
                
                if os.path.exists(default_downloads):
                    json_files = [f for f in os.listdir(default_downloads) 
                                if f.endswith('.json') and not f.endswith('.crdownload')]
                    
                    if json_files:
                        newest_file = max(json_files, 
                                        key=lambda x: os.path.getmtime(os.path.join(default_downloads, x)))
                        full_path = os.path.join(default_downloads, newest_file)
                        
                        file_age = time.time() - os.path.getmtime(full_path)
                        if file_age < 120:  # Modified within last 2 minutes
                            if os.path.getsize(full_path) > 100:
                                try:
                                    with open(full_path, 'r', encoding='utf-8') as f:
                                        json_content = f.read()
                                    json_data = json.loads(json_content)
                                    
                                    # Check for valid OAuth credentials
                                    if 'web' in json_data:
                                        web_data = json_data['web']
                                        if 'client_id' in web_data and 'client_secret' in web_data:
                                            downloaded_file = full_path
                                            logging.info(f"Found JSON file in Downloads folder: {newest_file}")
                                except:
                                    pass

            if not downloaded_file:
                logging.error("Download of client secret key JSON file timed out or file was empty.")
                raise Exception("JSON file download failed")

            logging.info(f"Successfully downloaded file: {downloaded_file}")

            # Read and validate JSON content
            with open(downloaded_file, 'r') as f:
                json_content = f.read()

            # FIXED: Validate JSON and extract client_id for original filename
            try:
                json_data = json.loads(json_content)
                
                # Handle nested structure
                if 'web' in json_data:
                    oauth_data = json_data['web']
                    if 'client_id' not in oauth_data or 'client_secret' not in oauth_data:
                        raise ValueError("Invalid OAuth JSON - missing client_id or client_secret in 'web' object")
                    client_id = oauth_data['client_id']
                elif 'client_id' in json_data and 'client_secret' in json_data:
                    # Root level structure
                    client_id = json_data['client_id']
                else:
                    raise ValueError("Invalid OAuth JSON - no valid credentials found")
                
                original_filename = f"client_secret_{client_id.split('-')[0]}.json"
                logging.info(f"✅ JSON validated for {account}, client_id: {client_id[:20]}...")
                logging.info(f"✅ Original filename: {original_filename}")
                
            except Exception as e:
                logging.error(f"❌ Invalid JSON content for {account}: {e}")
                os.unlink(downloaded_file)
                return False

            # Clean up downloaded file
            os.unlink(downloaded_file)
            logging.info("Downloaded file deleted immediately")

            # FIXED: Ensure account variable is properly set
            if not account or account.strip() == "":
                logging.error("❌ Account name is empty! Cannot proceed with upload.")
                return False

            logging.info(f"🔍 Processing account: '{account}'")

            # Create remote directory paths
            remote_account_dir = f"{self.remote_dir}{account}"  # /home/brightmindscampuss/admin@example.com
            shared_credentials_dir = f"{self.remote_dir}shared_credentials"  # /home/brightmindscampuss/shared_credentials

            # Extract client_id for original filename
            try:
                json_data = json.loads(json_content)
                
                # Handle nested structure
                if 'web' in json_data:
                    oauth_data = json_data['web']
                    client_id = oauth_data['client_id']
                elif 'client_id' in json_data:
                    client_id = json_data['client_id']
                else:
                    raise ValueError("No client_id found in JSON")
                                
            except Exception as e:
                logging.error(f"❌ Error extracting client_id: {e}")
                return False

            sftp = self.sftp_connect()
            try:
                # Create both directories
                logging.info(f"📁 Creating account directory: {remote_account_dir}")
                self.ensure_remote_directory_exists(sftp, remote_account_dir)
                
                logging.info(f"📁 Creating shared credentials directory: {shared_credentials_dir}")
                self.ensure_remote_directory_exists(sftp, shared_credentials_dir)
                
                # 1. Upload JSON file to account folder with account name as filename
                account_json_path = f"{remote_account_dir}/{account}.json"
                logging.info(f"📤 Uploading to account folder: {account_json_path}")
                success, message = self.upload_and_validate_to_sftp(sftp, json_content, account_json_path)
                if not success:
                    raise Exception(f"Account JSON upload failed: {message}")
                
                # 2. Upload JSON file to shared_credentials with account name
                shared_account_path = f"{shared_credentials_dir}/{account}.json"
                logging.info(f"📤 Uploading to shared folder (account name): {shared_account_path}")
                success, message = self.upload_and_validate_to_sftp(sftp, json_content, shared_account_path)
                if not success:
                    raise Exception(f"Shared account JSON upload failed: {message}")
                                
                # 3. Upload login.txt with credentials to account folder
                login_content = f"{account}:{password}"
                login_txt_path = f"{remote_account_dir}/login.txt"
                logging.info(f"📤 Uploading login credentials: {login_txt_path}")
                success, message = self.upload_and_validate_to_sftp(sftp, login_content, login_txt_path)
                if not success:
                    raise Exception(f"Login credentials upload failed: {message}")
                
                logging.info(f"✅ All files uploaded successfully for {account}")
                logging.info(f"   📁 Account folder: {remote_account_dir}")
                logging.info(f"     📄 {account}.json")
                logging.info(f"     📄 login.txt")
                logging.info(f"   📁 Shared folder: {shared_credentials_dir}")
                logging.info(f"     📄 {account}.json")
                return True
                
            finally:
                sftp.close()

        except Exception as e:
            logging.error(f"Error enabling Admin SDK for {account}: {e}")
            return False
        
    def enable_admin_sdk(self):
        if self.driver is None or not self.is_driver_valid():
            QMessageBox.warning(self, "No Driver", "Please login first.")
            return
        
        # GET ACCOUNT FROM EMAIL FIELD
        account = self.email_entry.text().strip()
        password = self.password_entry.text().strip()
        
        # If single fields are empty, get from bulk text area
        if not account or not password:
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            if accounts_data and accounts_data[0].strip():
                line = accounts_data[0].strip()
                if ',' in line:
                    account, password = line.split(',', 1)
                elif ':' in line:
                    account, password = line.split(':', 1)
                account = account.strip()
                password = password.strip()
        
        if not account:
            QMessageBox.warning(self, "Missing Account", "Please enter an account email.")
            return
            
        if not password:
            QMessageBox.warning(self, "Missing Password", "Please enter a password.")
            return
        
        logging.info(f"🎯 Single account Admin SDK setup for: '{account}'")
        
        download_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        result = self.enable_admin_sdk_for_driver(self.driver, account, password, download_dir)
        if result:
            QMessageBox.information(self, "Success", f"Admin SDK enabled successfully for {account}!")
        else:
            QMessageBox.critical(self, "Error", f"Admin SDK setup failed for {account}")


    # This helper function calculates grid positions for a given number of windows.
    def get_grid_positions(self, num_accounts, screen_width, screen_height):
        """
        Calculate grid positions for num_accounts windows
        based on the screen dimensions.
        Returns a list of dictionaries with 'x', 'y', 'width', 'height' keys.
        """
        import math
        
        # Example: generate a near-square layout
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
        return positions
            
            
    def process_account(self, account, password, position, app_instance):
        driver = None
        try:
            # VALIDATE INPUTS
            if not account or not account.strip():
                logging.error("❌ Empty account passed to process_account")
                return
                
            if not password or not password.strip():
                logging.error(f"❌ Empty password for account: {account}")
                return
            
            account = account.strip()
            password = password.strip()
            
            logging.info(f"🎯 Processing account: '{account}'")
            
            # CREATE UNIQUE DOWNLOAD DIRECTORY PER ACCOUNT
            account_safe = account.replace('@', '_at_').replace('.', '_').replace(':', '_')
            timestamp = int(time.time())
            thread_id = threading.current_thread().ident
            
            app_download_dir = os.path.join(os.path.expanduser("~"), "Downloads")

            os.makedirs(app_download_dir, exist_ok=True)
            logging.info(f"📁 Created download directory for {account}: {app_download_dir}")
            
            # Create driver with better error handling
            max_driver_attempts = 3
            for driver_attempt in range(max_driver_attempts):
                driver = app_instance.init_driver_instance(app_download_dir)
                if driver is not None:
                    break
                    
                if driver_attempt < max_driver_attempts - 1:
                    wait_time = (driver_attempt + 1) * 5  # 5s, 10s, 15s
                    logging.info(f"⏳ Driver creation failed, waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
            
            if driver is None:
                logging.error(f"❌ Failed to create driver for {account} after {max_driver_attempts} attempts")
                return
                
            # Set the window geometry
            try:
                driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
            except Exception as e:
                logging.warning(f"⚠️ Could not set window position for {account}: {e}")
            
            # Perform login
            app_instance.perform_login(driver, account, password)
            
            # Enable Admin SDK
            result = app_instance.enable_admin_sdk_for_driver(driver, account, password, app_download_dir)
            
            # Clean up download directory
            try:
                import shutil
                if os.path.exists(app_download_dir):
                    shutil.rmtree(app_download_dir)
                    logging.info(f"🧹 Cleaned up download directory for {account}")
            except Exception as e:
                logging.warning(f"⚠️ Could not clean up download directory for {account}: {e}")
            
            # Log results
            if result is not False:
                logging.info(f"✅ Successfully processed account: {account}")
            else:
                logging.error(f"❌ Admin SDK setup failed for {account}")
            
        except Exception as e:
            logging.error(f"❌ Error processing account {account}: {e}")
        finally:
            # Driver stays open for user interaction
            if driver:
                logging.info(f"🌐 Driver kept open for {account}")
            else:
                logging.error(f"❌ No driver available for {account}")

    def process_account_without_app_password(self, account, password, position, app_instance):
        """
        Process account with: Login -> Setup Authenticator -> Enable 2-Step Verification
        WITHOUT generating App Password
        """
        driver = None
        try:
            # VALIDATE INPUTS
            if not account or not account.strip():
                logging.error("❌ Empty account passed to process_account_without_app_password")
                return
            
            if not password or not password.strip():
                logging.error(f"❌ Empty password for account: {account}")
                return
            
            account = account.strip()
            password = password.strip()
            
            logging.info(f"🎯 Processing account (without app password): '{account}'")
            
            # CREATE UNIQUE DOWNLOAD DIRECTORY PER ACCOUNT
            account_safe = account.replace('@', '_at_').replace('.', '_').replace(':', '_')
            timestamp = int(time.time())
            thread_id = threading.current_thread().ident
            
            app_download_dir = os.path.join(os.path.expanduser("~"), "Downloads")

            os.makedirs(app_download_dir, exist_ok=True)
            logging.info(f"📁 Created download directory for {account}: {app_download_dir}")
            
            # Create driver with better error handling
            max_driver_attempts = 3
            for driver_attempt in range(max_driver_attempts):
                driver = app_instance.init_driver_instance(app_download_dir)
                if driver is not None:
                    break
                    
                if driver_attempt < max_driver_attempts - 1:
                    wait_time = (driver_attempt + 1) * 5  # 5s, 10s, 15s
                    logging.info(f"⏳ Driver creation failed, waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
            
            if driver is None:
                logging.error(f"❌ Failed to create driver for {account} after {max_driver_attempts} attempts")
                return
                
            # Set the window geometry
            try:
                driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
            except Exception as e:
                logging.warning(f"⚠️ Could not set window position for {account}: {e}")
            
            # Step 1: Perform login
            logging.info(f"🔐 Step 1: Logging in for {account}...")
            app_instance.perform_login(driver, account, password)
            
            # Step 2: Setup Authenticator
            logging.info(f"🔑 Step 2: Setting up Authenticator for {account}...")
            authenticator_result = app_instance.setup_authenticator_single(driver, account)
            if not authenticator_result:
                logging.error(f"❌ Authenticator setup failed for {account}")
                return
            
            # Step 3: Enable 2-Step Verification
            logging.info(f"🔒 Step 3: Enabling 2-Step Verification for {account}...")
            two_step_result = app_instance.enable_two_step_verification_single(driver, account, password)
            if not two_step_result:
                logging.error(f"❌ 2-Step Verification setup failed for {account}")
                return
            
            # Clean up download directory
            try:
                import shutil
                if os.path.exists(app_download_dir):
                    shutil.rmtree(app_download_dir)
                    logging.info(f"🧹 Cleaned up download directory for {account}")
            except Exception as e:
                logging.warning(f"⚠️ Could not clean up download directory for {account}: {e}")
            
            # Log results
            logging.info(f"✅ Successfully processed account (without app password): {account}")
            logging.info(f"✅ Completed: Login -> Authenticator Setup -> 2-Step Verification")
            
        except Exception as e:
            logging.error(f"❌ Error processing account (without app password) {account}: {e}")
        finally:
            # Driver stays open for user interaction
            if driver:
                logging.info(f"🌐 Driver kept open for {account}")
            else:
                logging.error(f"❌ No driver available for {account}")

    def upload_and_validate_to_sftp(self, sftp, local_content, remote_path):
        """Upload to SFTP and validate it was successful"""
        try:
            # Create temp file for upload
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_file:
                temp_file.write(local_content)
                temp_file_path = temp_file.name
            
            try:
                # Upload to SFTP
                sftp.put(temp_file_path, remote_path)
                logging.info(f"Uploaded to SFTP: {remote_path}")
                
                # VALIDATE: Check file exists and has correct size
                remote_stat = sftp.stat(remote_path)
                local_size = os.path.getsize(temp_file_path)
                
                if remote_stat.st_size == local_size:
                    logging.info(f"✅ SFTP upload validated: {remote_path} ({remote_stat.st_size} bytes)")
                    return True, f"Upload successful ({remote_stat.st_size} bytes)"
                else:
                    logging.error(f"❌ Size mismatch: local={local_size}, remote={remote_stat.st_size}")
                    return False, "Size mismatch after upload"
                    
            finally:
                os.unlink(temp_file_path)  # Clean temp file
                
        except Exception as e:
            logging.error(f"❌ SFTP upload failed: {e}")
            return False, f"Upload failed: {e}"

    def process_multiple_accounts(self):
        """
        Reads concurrency and the bulk accounts, then does:
        1) login
        2) enable_admin_sdk_for_driver
        in parallel, each in a separate window, arranged in a grid or cascade.
        """
        # Import necessary modules
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        accounts_data = self.accounts_text.toPlainText().strip().splitlines()
        accounts = []
        for line in accounts_data:
            line = line.strip()
            if not line:
                continue
                
            user = None
            pwd = None
            
            if ',' in line:
                user, pwd = line.split(',', 1)
            elif ':' in line:
                user, pwd = line.split(':', 1)
            else:
                # Only email provided, try to fetch password from server
                user = line.strip()
                logging.info(f"🔍 Only email provided: {user}, attempting to fetch password from server")
                pwd = self.get_account_password(user)
                if not pwd:
                    logging.warning(f"🔍 Password not found for {user}, skipping this account")
                    continue
                
            user = user.strip()
            pwd = pwd.strip()
            
            if user and pwd:
                accounts.append((user, pwd))
    
        num_accounts = len(accounts)
        if num_accounts == 0:
            logging.error("No valid accounts found.")
            QMessageBox.warning(self, "No Valid Accounts", 
                "No valid accounts found. Please ensure accounts are in format 'email:password' or 'email' (if password exists on server).")
            return
    
        screen_width = 1920
        screen_height = 1080
    
        positions = self.get_grid_positions(num_accounts, screen_width, screen_height)
        
        # Process accounts concurrently using a thread pool.
        with ThreadPoolExecutor(max_workers=num_accounts) as executor:
            futures = []
            for i, (account, pwd) in enumerate(accounts):
                pos = positions[i] if i < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                futures.append(executor.submit(self.process_account, account, pwd, pos, self))
            # Wait for all accounts to complete processing.
            for future in as_completed(futures):
                future.result()
        
        QMessageBox.information(self, "Done", "All requested accounts have been processed.")

    def process_multiple_accounts_without_app_password(self):
        """
        Reads concurrency and the bulk accounts, then does:
        1) login
        2) setup_authenticator_single
        3) enable_two_step_verification_single
        WITHOUT generating app password
        in parallel, each in a separate window, arranged in a grid or cascade.
        """
        # Import necessary modules
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        accounts_data = self.accounts_text.toPlainText().strip().splitlines()
        accounts = []
        for line in accounts_data:
            line = line.strip()
            if not line:
                continue
                
            user = None
            pwd = None
            
            if ',' in line:
                user, pwd = line.split(',', 1)
            elif ':' in line:
                user, pwd = line.split(':', 1)
            else:
                # Only email provided, try to fetch password from server
                user = line.strip()
                logging.info(f"🔍 Only email provided: {user}, attempting to fetch password from server")
                pwd = self.get_account_password(user)
                if not pwd:
                    logging.warning(f"🔍 Password not found for {user}, skipping this account")
                    continue
                
            user = user.strip()
            pwd = pwd.strip()
            
            if user and pwd:
                accounts.append((user, pwd))
    
        num_accounts = len(accounts)
        if num_accounts == 0:
            logging.error("No valid accounts found.")
            QMessageBox.warning(self, "No Valid Accounts", 
                "No valid accounts found. Please ensure accounts are in format 'email:password' or 'email' (if password exists on server).")
            return
    
        screen_width = 1920
        screen_height = 1080
    
        positions = self.get_grid_positions(num_accounts, screen_width, screen_height)
        
        # Process accounts concurrently using a thread pool.
        with ThreadPoolExecutor(max_workers=num_accounts) as executor:
            futures = []
            for i, (account, pwd) in enumerate(accounts):
                pos = positions[i] if i < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                futures.append(executor.submit(self.process_account_without_app_password, account, pwd, pos, self))
            # Wait for all accounts to complete processing.
            for future in as_completed(futures):
                future.result()
        
        QMessageBox.information(self, "Done", "All requested accounts have been processed (without app password generation).")

    def open_playwright_recorder(self):
        """Open Playwright codegen for recording browser interactions"""
        try:
            import subprocess
            
            # Use the correct full path to playwright executable
            playwright_path = "C:\\Users\\Latitude\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\playwright.exe"
            
            # Open Playwright codegen recorder
            url = "https://console.cloud.google.com/apis/credentials/consent?hl=en"
            subprocess.Popen([
                playwright_path, "codegen", 
                "--target", "python",
                "--browser", "chromium",
                url
            ])
            
            QMessageBox.information(self, "Playwright Recorder", 
                                "Playwright recorder opened!\n\n"
                                "1. Login to your Google account\n"
                                "2. Complete the problematic steps manually\n"
                                "3. Copy the generated code\n"
                                "4. We'll integrate it into the app\n\n"
                                "Focus on: Audience → Publish → Confirm → ID Client")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open Playwright recorder: {e}")

    def test_sftp_folder_creation(self):
        """Test function to verify SFTP folder creation works"""
        try:
            # Test account
            test_account = "test@example.com"
            
            # Connect to SFTP
            sftp = self.sftp_connect()
            
            # Create account directory
            remote_account_dir = f"{self.remote_dir}{test_account}"
            self.ensure_remote_directory_exists(sftp, remote_account_dir)
            logging.info(f"✅ Created account directory: {remote_account_dir}")
            
            # Create shared credentials directory  
            shared_credentials_dir = f"{self.remote_dir}shared_credentials"
            self.ensure_remote_directory_exists(sftp, shared_credentials_dir)
            logging.info(f"✅ Created shared directory: {shared_credentials_dir}")
            
            # Test file upload
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as temp_file:
                temp_file.write('{"test": "data"}')
                temp_file_path = temp_file.name
            
            try:
                # Upload to account folder
                remote_file_path = f"{remote_account_dir}/{test_account}.json"
                sftp.put(temp_file_path, remote_file_path)
                logging.info(f"✅ Uploaded test file: {remote_file_path}")
                
                # Upload to shared folder
                shared_file_path = f"{shared_credentials_dir}/{test_account}_client_secret.json"
                sftp.put(temp_file_path, shared_file_path)
                logging.info(f"✅ Uploaded to shared: {shared_file_path}")
                
            finally:
                import os
                os.unlink(temp_file_path)
                sftp.close()
                
            QMessageBox.information(self, "SFTP Test", "SFTP folder creation and upload test completed successfully!")
            
        except Exception as e:
            logging.error(f"❌ SFTP test failed: {e}")
            QMessageBox.critical(self, "SFTP Test Failed", f"Error: {e}")

    # Enhanced Account Management Functions
    
    def retrieve_remote_accounts(self):
        """Retrieve accounts from remote server and populate the account field"""
        try:
            self.retrieve_accounts_button.setEnabled(False)
            self.retrieve_accounts_button.setText("🔄 Retrieving...")
            
            # Retrieve accounts from server
            accounts, message = self.remote_account_manager.retrieve_accounts()
            
            if accounts:
                # Store full accounts (with passwords) for later use
                self.stored_accounts = {}
                display_accounts = []
                
                for account_line in accounts:
                    if ':' in account_line:
                        email, password = account_line.split(':', 1)
                        email = email.strip()
                        password = password.strip()
                        self.stored_accounts[email] = password
                        display_accounts.append(email)  # Only display email
                    elif ' ' in account_line:
                        parts = account_line.split(' ', 1)
                        if len(parts) == 2:
                            email, password = parts
                            email = email.strip()
                            password = password.strip()
                            self.stored_accounts[email] = password
                            display_accounts.append(email)  # Only display email
                
                # Populate the account field with emails only
                current_content = self.accounts_text.toPlainText().strip()
                if current_content:
                    # Append to existing content
                    new_content = current_content + "\n" + "\n".join(display_accounts)
                else:
                    new_content = "\n".join(display_accounts)
                    
                self.accounts_text.setPlainText(new_content)
                QMessageBox.information(self, "Success", f"{message}\nAccounts have been loaded into the account field (passwords hidden).")
                logging.info(f"Retrieved and loaded {len(display_accounts)} accounts (passwords stored securely)")
            else:
                QMessageBox.warning(self, "Retrieval Failed", f"Failed to retrieve accounts: {message}")
                logging.error(f"Account retrieval failed: {message}")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error retrieving accounts: {str(e)}")
            logging.error(f"Error in retrieve_remote_accounts: {e}")
        finally:
            self.retrieve_accounts_button.setEnabled(True)
            self.retrieve_accounts_button.setText("🔄 Retrieve Accounts")
            
    def delete_selected_accounts(self):
        """Delete selected accounts from the account field"""
        try:
            # Get current cursor position and selected text
            cursor = self.accounts_text.textCursor()
            selected_text = cursor.selectedText()
            
            if selected_text:
                # Delete selected text
                cursor.removeSelectedText()
                QMessageBox.information(self, "Success", "Selected accounts have been deleted.")
                logging.info("Selected accounts deleted")
            else:
                # If no selection, ask user to select accounts first
                QMessageBox.information(self, "No Selection", 
                    "Please select the accounts you want to delete by highlighting them in the account field.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting selected accounts: {str(e)}")
            logging.error(f"Error in delete_selected_accounts: {e}")
            
    def login_selected_account(self):
        """Login with the currently selected account or multiple accounts if none selected"""
        try:
            cursor = self.accounts_text.textCursor()
            selected_text = cursor.selectedText()
            
            if not selected_text:
                # No account selected - login to multiple accounts based on concurrent limit
                logging.info("🔍 No account selected, logging in to multiple accounts based on concurrent limit")
                self.login_multiple_enhanced()
                return
                
            # Parse selected account - handle both email:password and email-only formats
            email = selected_text.strip()
            password = None
            
            if ':' in selected_text:
                email, password = selected_text.split(':', 1)
                email = email.strip()
                password = password.strip()
            elif ',' in selected_text:
                email, password = selected_text.split(',', 1)
                email = email.strip()
                password = password.strip()
            else:
                # Only email provided, try to fetch password from server
                email = selected_text.strip()
                logging.info(f"🔍 Only email selected: {email}, attempting to fetch password from server")
                password = self.get_account_password(email)
                if not password:
                    QMessageBox.warning(self, "Password Not Found", 
                        f"Password not found for {email}. Please ensure the account exists on the server or select an account with password.")
                    return
                
            if not email:
                QMessageBox.warning(self, "Invalid Format", 
                    "Selected text must be a valid email address or in format: account:password")
                return
                
            # Set the account in the single account fields
            self.email_entry.setText(email)
            self.password_entry.setText(password)
            
            # Perform login
            self.login()
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error logging in selected account: {str(e)}")
            logging.error(f"Error in login_selected_account: {e}")
            
    def login_multiple_enhanced(self):
        """Enhanced multiple account login with better concurrency handling"""
        try:
            # Get accounts from the enhanced account field
            accounts_data = self.accounts_text.toPlainText().strip().splitlines()
            accounts = []
            
            for line in accounts_data:
                line = line.strip()
                if not line:
                    continue
                    
                user = None
                pwd = None
                
                if ':' in line:
                    user, pwd = line.split(':', 1)
                elif ',' in line:
                    user, pwd = line.split(',', 1)
                else:
                    # Only email provided, try to fetch password from server
                    user = line.strip()
                    logging.info(f"🔍 Only email provided: {user}, attempting to fetch password from server")
                    pwd = self.get_account_password(user)
                    if not pwd:
                        logging.warning(f"🔍 Password not found for {user}, skipping this account")
                        continue
                    
                user = user.strip()
                pwd = pwd.strip()
                
                if user and pwd:
                    accounts.append((user, pwd))
                    
            if not accounts:
                QMessageBox.warning(self, "No Valid Accounts", 
                    "No valid accounts found. Please ensure accounts are in format 'email:password' or 'email' (if password exists on server).")
                return
                
            # Get concurrency limit from the existing spinbox
            try:
                concurrent_limit = int(self.concurrent_accounts_entry.text().strip())
                if concurrent_limit <= 0:
                    concurrent_limit = 1
            except ValueError:
                concurrent_limit = 1
                
            logging.info(f"Starting enhanced multiple login with {len(accounts)} accounts, max {concurrent_limit} concurrent")
            
            # Use the existing login_multiple_accounts function with enhanced OTP detection
            self.login_multiple_accounts_enhanced(accounts, concurrent_limit)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error in enhanced multiple login: {str(e)}")
            logging.error(f"Error in login_multiple_enhanced: {e}")
            
    def login_multiple_accounts_enhanced(self, accounts, concurrent_limit):
        """Enhanced version of multiple account login with global OTP detection"""
        try:
            # Enable global OTP detection
            self.start_global_otp_detection()
            
            screen_width = 1920
            screen_height = 1080
            # Fix: Use concurrent_limit for window arrangement, not total accounts
            positions = self.get_grid_positions(concurrent_limit, screen_width, screen_height)
            
            # Helper function for enhanced login
            def login_account_enhanced(account, password, position, app_instance):
                driver = None
                login_successful = False
                try:
                    # Create a new driver instance for the account
                    driver = app_instance.init_driver_instance()
                    if driver is None:
                        logging.error(f"Failed to create driver for {account}")
                        return
                    
                    # Add driver to active drivers list for OTP tracking
                    with app_instance.driver_lock:
                        app_instance.active_drivers.append(driver)
                        
                    # Set the window geometry based on the computed position
                    driver.set_window_rect(position['x'], position['y'], position['width'], position['height'])
                    
                    # Perform enhanced login with OTP detection
                    app_instance.perform_login_enhanced(driver, account, password)
                    login_successful = True
                    
                    # Keep the browser open after successful login - don't close it
                    logging.info(f"Login successful for {account}. Browser window kept open for continued use.")
                    
                except Exception as e:
                    if not app_instance.handle_driver_error(e):
                        logging.error(f"Error logging in account {account}: {e}")
                finally:
                    # Quit driver if login was not successful to respect concurrent_limit
                    if not login_successful and driver:
                        try:
                            driver.quit()
                            with app_instance.driver_lock:
                                if driver in app_instance.active_drivers:
                                    app_instance.active_drivers.remove(driver)
                            logging.info(f"Driver for {account} quit due to login failure.")
                        except Exception as quit_e:
                            logging.error(f"Error quitting driver for {account}: {quit_e}")
                    
            # Process accounts with concurrency limit
            with ThreadPoolExecutor(max_workers=concurrent_limit) as executor:
                futures = []
                for i, (account, pwd) in enumerate(accounts):
                    # Fix: Use modulo to cycle through positions based on concurrent_limit
                    pos_index = i % concurrent_limit
                    pos = positions[pos_index] if pos_index < len(positions) else {'x': 0, 'y': 0, 'width': 800, 'height': 600}
                    futures.append(executor.submit(login_account_enhanced, account, pwd, pos, self))
                    
                # Wait for all accounts to complete processing
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(f"Thread execution error: {e}")
                        
            # Stop global OTP detection
            self.stop_global_otp_detection()
            
            QMessageBox.information(self, "Done", f"All {len(accounts)} accounts have been processed.")
            
        except Exception as e:
            self.stop_global_otp_detection()
            QMessageBox.critical(self, "Error", f"Error in enhanced multiple login: {str(e)}")
            logging.error(f"Error in login_multiple_accounts_enhanced: {e}")
            
    def start_global_otp_detection(self):
        """Start global OTP detection across all browser instances"""
        if not self.global_otp_detection_active:
            self.global_otp_detection_active = True
            self.otp_detection_timer = QTimer()
            self.otp_detection_timer.timeout.connect(self.check_all_drivers_for_otp)
            self.otp_detection_timer.start(2000)  # Check every 2 seconds
            logging.info("Global OTP detection started")
            
    def stop_global_otp_detection(self):
        """Stop global OTP detection"""
        if self.global_otp_detection_active:
            self.global_otp_detection_active = False
            if self.otp_detection_timer:
                self.otp_detection_timer.stop()
                self.otp_detection_timer = None
            # Don't clear active drivers - they should remain for window reuse
            logging.info("Global OTP detection stopped")
            
    def check_all_drivers_for_otp(self):
        """Check all active drivers for OTP challenges"""
        if not self.global_otp_detection_active:
            return
            
        try:
            # Check all active drivers for OTP challenges
            with self.driver_lock:
                drivers_to_check = self.active_drivers.copy()
            
            for driver in drivers_to_check:
                try:
                    if driver and self.is_driver_valid_for_driver(driver):
                        # Use thread-safe OTP handler for global detection
                        # Pass None for email since we don't have it in this context
                        self.otp_handler.request_otp_handling(driver, None)
                except Exception as e:
                    logging.error(f"Error checking driver for OTP: {e}")
                    # Remove invalid driver from list
                    with self.driver_lock:
                        if driver in self.active_drivers:
                            self.active_drivers.remove(driver)
        except Exception as e:
            logging.error(f"Error in global OTP detection: {e}")
            
    def handle_otp_directly(self, driver, email):
        """Handle OTP directly without global detection conflicts"""
        try:
            logging.info(f"Starting direct OTP handling for {email}")
            
            # Try OTP handling up to 3 times
            for attempt in range(3):
                try:
                    logging.info(f"Direct OTP attempt {attempt + 1}/3 for {email}")
                    
                    secret_key_file = self.get_secret_key_file(email)
                    if not secret_key_file:
                        logging.error(f"No secret key file found for {email}")
                        return False
                    
                    try:
                        with open(secret_key_file, 'r') as f:
                            secret_key = f.read().strip()
                        otp_code = self.generate_otp_code(secret_key)
                        
                        if not otp_code:
                            logging.error(f"Failed to generate OTP for {email}")
                            return False
                        
                        # Try multiple OTP input selectors
                        otp_selectors = [
                            '//input[@type="tel"]',
                            '//input[@name="totpPin"]',
                            '//input[@id="totpPin"]',
                            '//input[contains(@class, "totp")]',
                            '//input[@placeholder*="code"]',
                            '//input[@placeholder*="Code"]'
                        ]
                        
                        otp_entered = False
                        for selector in otp_selectors:
                            try:
                                otp_input = WebDriverWait(driver, 10).until(
                                    EC.element_to_be_clickable((By.XPATH, selector))
                                )
                                otp_input.clear()
                                time.sleep(1)
                                otp_input.send_keys(otp_code)
                                time.sleep(1)
                                otp_input.send_keys(Keys.ENTER)
                                logging.info(f"OTP entered for {email} (attempt {attempt + 1})")
                                otp_entered = True
                                break
                            except TimeoutException:
                                continue
                        
                        if not otp_entered:
                            logging.error(f"Could not find OTP input field for {email}")
                            continue
                        
                        # Wait for OTP processing and verify success
                        time.sleep(5)  # Wait for initial processing
                        
                        # Check if we're still on OTP page or if login succeeded
                        max_wait_time = 30
                        wait_interval = 2
                        waited_time = 0
                        
                        while waited_time < max_wait_time:
                            try:
                                current_url = driver.current_url
                                
                                # Check if login succeeded
                                if "myaccount.google.com" in current_url or "admin.google.com" in current_url:
                                    logging.info(f"Direct OTP verification successful for {email}")
                                    return True
                                
                                # Check if still on OTP page
                                otp_indicators = ["challenge/totp", "challenge/2sv", "signin/v2/challenge"]
                                if any(indicator in current_url for indicator in otp_indicators):
                                    # Still on OTP page, wait more
                                    time.sleep(wait_interval)
                                    waited_time += wait_interval
                                    continue
                                
                                # Check for error messages
                                try:
                                    error_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'incorrect') or contains(text(), 'Invalid') or contains(text(), 'wrong')]")
                                    if error_elements:
                                        logging.warning(f"OTP error detected for {email}, retrying...")
                                        break  # Break inner loop to retry
                                except:
                                    pass
                                
                                # If we're here, something unexpected happened
                                time.sleep(wait_interval)
                                waited_time += wait_interval
                                
                            except Exception as e:
                                logging.error(f"Error checking OTP status for {email}: {e}")
                                break
                        
                        # If we get here, OTP might have succeeded but we're not sure
                        # Let's check one more time
                        try:
                            final_url = driver.current_url
                            if "myaccount.google.com" in final_url or "admin.google.com" in final_url:
                                logging.info(f"Direct OTP verification successful for {email} (final check)")
                                return True
                        except:
                            pass
                        
                        # If we're still on OTP page after all attempts, this attempt failed
                        otp_indicators = ["challenge/totp", "challenge/2sv", "signin/v2/challenge"]
                        if any(indicator in driver.current_url for indicator in otp_indicators):
                            logging.warning(f"Direct OTP attempt {attempt + 1} failed for {email}, retrying...")
                            time.sleep(2)  # Wait before retry
                            continue
                        else:
                            # We're not on OTP page anymore, might have succeeded
                            logging.info(f"Direct OTP processing completed for {email}")
                            return True
                            
                    finally:
                        # Clean up temp file
                        if os.path.exists(secret_key_file):
                            os.unlink(secret_key_file)
                            
                except Exception as e:
                    logging.error(f"Error in direct OTP attempt {attempt + 1} for {email}: {e}")
                    if attempt < 2:  # Don't sleep after last attempt
                        time.sleep(2)
                    continue
            
            # If we get here, all 3 attempts failed
            logging.error(f"All direct OTP attempts failed for {email}")
            return False
            
        except Exception as e:
            logging.error(f"Error in direct OTP handling for {email}: {e}")
            return False
            
    def handle_otp_if_needed_global(self, driver, email=None):
        """Enhanced OTP handling for global detection with verification and retry"""
        try:
            current_url = driver.current_url
            
            # Check for OTP challenges in various scenarios
            otp_indicators = [
                "challenge/totp",
                "challenge/2sv",
                "signin/v2/challenge",
                "accounts.google.com/signin/v2/challenge"
            ]
            
            for indicator in otp_indicators:
                if indicator in current_url:
                    logging.info(f"Global OTP challenge detected: {current_url}")
                    
                    # Use provided email or try to get from driver
                    if email:
                        current_email = email
                    else:
                        current_email = self.get_current_account_from_driver(driver)
                    
                    if current_email:
                        # Try OTP handling up to 3 times
                        for attempt in range(3):
                            try:
                                logging.info(f"Global OTP attempt {attempt + 1}/3 for {current_email}")
                                
                                secret_key_file = self.get_secret_key_file(current_email)
                                if not secret_key_file:
                                    logging.error(f"No secret key file found for {current_email}")
                                    return False
                                
                                try:
                                    with open(secret_key_file, 'r') as f:
                                        secret_key = f.read().strip()
                                    otp_code = self.generate_otp_code(secret_key)
                                    
                                    if not otp_code:
                                        logging.error(f"Failed to generate OTP for {current_email}")
                                        return False
                                    
                                    # Try multiple OTP input selectors
                                    otp_selectors = [
                                        '//input[@type="tel"]',
                                        '//input[@name="totpPin"]',
                                        '//input[@id="totpPin"]',
                                        '//input[contains(@class, "totp")]',
                                        '//input[@placeholder*="code"]',
                                        '//input[@placeholder*="Code"]'
                                    ]
                                    
                                    otp_entered = False
                                    for selector in otp_selectors:
                                        try:
                                            otp_input = WebDriverWait(driver, 10).until(
                                                EC.element_to_be_clickable((By.XPATH, selector))
                                            )
                                            otp_input.clear()
                                            time.sleep(1)
                                            otp_input.send_keys(otp_code)
                                            time.sleep(1)
                                            otp_input.send_keys(Keys.ENTER)
                                            logging.info(f"Global OTP entered for {current_email} (attempt {attempt + 1})")
                                            otp_entered = True
                                            break
                                        except TimeoutException:
                                            continue
                                    
                                    if not otp_entered:
                                        logging.error(f"Could not find OTP input field for {current_email}")
                                        continue
                                    
                                    # Wait for OTP processing and verify success
                                    time.sleep(5)  # Wait for initial processing
                                    
                                    # Check if we're still on OTP page or if login succeeded
                                    max_wait_time = 30
                                    wait_interval = 2
                                    waited_time = 0
                                    
                                    while waited_time < max_wait_time:
                                        try:
                                            current_url = driver.current_url
                                            
                                            # Check if login succeeded
                                            if "myaccount.google.com" in current_url or "admin.google.com" in current_url:
                                                logging.info(f"Global OTP verification successful for {current_email}")
                                                return True
                                            
                                            # Check if still on OTP page
                                            if any(indicator in current_url for indicator in otp_indicators):
                                                # Still on OTP page, wait more
                                                time.sleep(wait_interval)
                                                waited_time += wait_interval
                                                continue
                                            
                                            # Check for error messages
                                            try:
                                                error_elements = driver.find_elements(By.XPATH, "//*[contains(text(), 'incorrect') or contains(text(), 'Invalid') or contains(text(), 'wrong')]")
                                                if error_elements:
                                                    logging.warning(f"Global OTP error detected for {current_email}, retrying...")
                                                    break  # Break inner loop to retry
                                            except:
                                                pass
                                            
                                            # If we're here, something unexpected happened
                                            time.sleep(wait_interval)
                                            waited_time += wait_interval
                                            
                                        except Exception as e:
                                            logging.error(f"Error checking global OTP status for {current_email}: {e}")
                                            break
                                    
                                    # If we get here, OTP might have succeeded but we're not sure
                                    # Let's check one more time
                                    try:
                                        final_url = driver.current_url
                                        if "myaccount.google.com" in final_url or "admin.google.com" in final_url:
                                            logging.info(f"Global OTP verification successful for {current_email} (final check)")
                                            return True
                                    except:
                                        pass
                                    
                                    # If we're still on OTP page after all attempts, this attempt failed
                                    if any(indicator in driver.current_url for indicator in otp_indicators):
                                        logging.warning(f"Global OTP attempt {attempt + 1} failed for {current_email}, retrying...")
                                        time.sleep(2)  # Wait before retry
                                        continue
                                    else:
                                        # We're not on OTP page anymore, might have succeeded
                                        logging.info(f"Global OTP processing completed for {current_email}")
                                        return True
                                        
                                finally:
                                    # Clean up temp file
                                    if os.path.exists(secret_key_file):
                                        os.unlink(secret_key_file)
                                        
                            except Exception as e:
                                logging.error(f"Error in global OTP attempt {attempt + 1} for {current_email}: {e}")
                                if attempt < 2:  # Don't sleep after last attempt
                                    time.sleep(2)
                                continue
                        
                        # If we get here, all 3 attempts failed
                        logging.error(f"All global OTP attempts failed for {current_email}")
                        return False
                    else:
                        logging.error(f"Could not determine email for global OTP challenge")
                        return False
                    
            return False
            
        except Exception as e:
            logging.error(f"Error in global OTP handling: {e}")
            return False
            
    def get_current_account_from_driver(self, driver):
        """Try to extract current account from driver (thread-safe)"""
        try:
            # Try to get from URL or page content
            current_url = driver.current_url
            
            # Check if we can extract from URL
            if "accounts.google.com" in current_url:
                # Try to find email in page elements
                try:
                    email_element = driver.find_element(By.CSS_SELECTOR, '[data-email]')
                    return email_element.get_attribute('data-email')
                except:
                    pass
                    
                try:
                    email_element = driver.find_element(By.CSS_SELECTOR, '.email')
                    return email_element.text
                except:
                    pass
                    
            # Return None instead of accessing UI elements from different thread
            return None
            
        except Exception as e:
            logging.error(f"Error getting current account from driver: {e}")
            return None
            
    def perform_login_enhanced(self, driver, email, password):
        """Enhanced login function with direct OTP handling (no global detection conflicts)"""
        try:
            driver.get("https://accounts.google.com/ServiceLogin?hl=en")
            
            # Enter email
            email_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.ID, "identifierId"))
            )
            email_input.clear()
            time.sleep(1)
            email_input.send_keys(email)
            email_input.send_keys(Keys.ENTER)
            
            # Enter password
            password_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.NAME, "Passwd"))
            )
            password_input.clear()
            time.sleep(1)
            password_input.send_keys(password)
            password_input.send_keys(Keys.ENTER)
            
            # Wait and check for OTP
            time.sleep(3)
            
            # Direct OTP handling - handle OTP directly without global detection conflicts
            if "challenge/totp" in driver.current_url:
                logging.info(f"OTP challenge detected for {email}, handling directly...")
                
                # Handle OTP directly without using the global handler to avoid conflicts
                otp_success = self.handle_otp_directly(driver, email)
                
                if otp_success:
                    logging.info(f"OTP handling completed successfully for {email}.")
                else:
                    logging.warning(f"OTP handling failed for {email}, but continuing...")
                
                # Additional wait to ensure OTP processing is complete
                time.sleep(5)
                
            # Wait for successful login with more flexible conditions
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: "myaccount.google.com" in d.current_url or 
                             "admin.google.com" in d.current_url or
                             "accounts.google.com/signin/oauth/consent" in d.current_url
                )
            except TimeoutException:
                # Check if we're still on login page or OTP page
                current_url = driver.current_url
                if "challenge/totp" in current_url or "accounts.google.com/signin" in current_url:
                    logging.warning(f"Login may not have completed for {email}, but continuing...")
                    # Don't raise exception, let the process continue
                else:
                    # We're on some other page, might be successful
                    logging.info(f"Login appears to have succeeded for {email} (on page: {current_url})")
            
            # Handle billing page if encountered
            billing_handled = self.handle_billing_page(driver, email)
            if not billing_handled:
                logging.warning(f"Billing page handling failed for {email}, but continuing...")
            
            # Force navigation to admin page if we're stuck on billing page
            try:
                current_url = driver.current_url
                if any(indicator in current_url.lower() for indicator in ["billing", "payment", "subscription", "setup", "welcome"]):
                    logging.info(f"Forcing navigation to admin page for {email} to bypass billing page...")
                    driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                    time.sleep(3)
            except Exception as e:
                logging.warning(f"Error forcing navigation for {email}: {e}")
            
            # Try to navigate to admin page, but don't fail if it doesn't work
            try:
                driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                time.sleep(3)  # Wait for page load
                
                # Check for second OTP challenge after redirection to admin page
                current_url = driver.current_url
                otp_indicators = ["challenge/totp", "challenge/2sv", "signin/v2/challenge"]
                
                if any(indicator in current_url for indicator in otp_indicators):
                    logging.info(f"Second OTP challenge detected for {email} after redirection, handling directly...")
                    
                    # Handle second OTP directly
                    otp_success = self.handle_otp_directly(driver, email)
                    
                    if otp_success:
                        logging.info(f"Second OTP handling completed successfully for {email}.")
                        # Try to navigate to admin page again after OTP
                        try:
                            driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                            time.sleep(3)
                        except Exception as e:
                            logging.warning(f"Could not navigate to admin page after second OTP for {email}: {e}")
                    else:
                        logging.warning(f"Second OTP handling failed for {email}, but continuing...")
                
                # Handle billing page after second OTP if encountered
                billing_handled = self.handle_billing_page(driver, email)
                if not billing_handled:
                    logging.warning(f"Billing page handling failed for {email} after second OTP, but continuing...")
                
                # Force navigation to admin page if we're stuck on billing page after second OTP
                try:
                    current_url = driver.current_url
                    if any(indicator in current_url.lower() for indicator in ["billing", "payment", "subscription", "setup", "welcome"]):
                        logging.info(f"Forcing navigation to admin page for {email} after second OTP to bypass billing page...")
                        driver.get("https://admin.google.com/ac/domains/manage?hl=en")
                        time.sleep(3)
                except Exception as e:
                    logging.warning(f"Error forcing navigation after second OTP for {email}: {e}")
                
                logging.info(f"Enhanced login successful for {email}")
            except Exception as e:
                logging.warning(f"Could not navigate to admin page for {email}: {e}")
                # Don't raise exception, consider login successful if we got this far
                
            # Generate and add subdomains if login was successful
            try:
                # Get subdomain settings from UI
                try:
                    subdomain_count = int(self.subdomain_count_entry.text().strip())
                    alphabet_length = int(self.subdomain_alphabet_entry.text().strip())
                except ValueError:
                    logging.warning(f"Invalid subdomain settings for {email}, skipping subdomain generation")
                    return
                
                if subdomain_count > 0 and alphabet_length > 0:
                    logging.info(f"Starting subdomain generation and addition for {email}")
                    
                    # Extract domain from email
                    domain = email.split("@")[-1]
                    
                    # Generate subdomains
                    subdomains = self.generate_subdomains_with_alphabet(domain, subdomain_count, alphabet_length)
                    
                    if subdomains:
                        # Add subdomains to the account
                        added_count = self.add_subdomains_to_account(driver, email, subdomains)
                        logging.info(f"Successfully added {added_count}/{len(subdomains)} subdomains to {email}")
                    else:
                        logging.warning(f"Failed to generate subdomains for {email}")
                        
            except Exception as e:
                logging.error(f"Error in subdomain generation/addition for {email}: {e}")
                # Don't raise exception, continue with the process
                
        except Exception as e:
            logging.error(f"Error in enhanced login for {email}: {e}")
            raise
            
    def handle_billing_page(self, driver, email):
        """Handle billing page by clicking 'Do it Later' button"""
        try:
            current_url = driver.current_url
            logging.info(f"Checking for billing page for {email} at URL: {current_url}")
            
            # Check if we're on a billing page
            billing_indicators = [
                "billing",
                "payment",
                "subscription",
                "setup",
                "welcome",
                "admin.google.com/ac/",
                "admin.google.com/ServiceLogin"
            ]
            
            if any(indicator in current_url.lower() for indicator in billing_indicators):
                logging.info(f"Billing page detected for {email}, attempting to handle...")
                
                # Force English language by adding ?hl=en to URL
                if "?" in current_url:
                    english_url = current_url + "&hl=en"
                else:
                    english_url = current_url + "?hl=en"
                
                try:
                    driver.get(english_url)
                    time.sleep(1)  # Minimal wait time - was 2 seconds
                    logging.info(f"Navigated to English version of billing page for {email}")
                except Exception as e:
                    logging.warning(f"Could not navigate to English version for {email}: {e}")
                
                # Try JavaScript click first - fastest approach
                try:
                    logging.info(f"Attempting JavaScript click for {email}...")
                    # Try to click any button with relevant text
                    js_script = """
                    var buttons = document.querySelectorAll('button, div[role="button"], a[role="button"]');
                    for (var i = 0; i < buttons.length; i++) {
                        var button = buttons[i];
                        var text = button.textContent.toLowerCase();
                        if (text.includes('do it later') || text.includes('skip') || text.includes('later') || 
                            text.includes('not now') || text.includes('continue') || text.includes('next')) {
                            button.click();
                            return true;
                        }
                    }
                    return false;
                    """
                    result = driver.execute_script(js_script)
                    if result:
                        logging.info(f"Successfully clicked button via JavaScript for {email}")
                        time.sleep(1)  # Minimal wait
                        return True
                except Exception as e:
                    logging.warning(f"JavaScript click failed for {email}: {e}")
                
                # Enhanced selectors for "Do it Later" button with faster detection
                do_it_later_selectors = [
                    # Direct text matches
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'do it later')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'skip')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'later')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'not now')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]",
                    
                    # Case-insensitive text matches
                    "//button[contains(text(), 'Do it later')]",
                    "//button[contains(text(), 'Do it Later')]",
                    "//button[contains(text(), 'Skip')]",
                    "//button[contains(text(), 'Later')]",
                    "//button[contains(text(), 'Not now')]",
                    "//button[contains(text(), 'Continue')]",
                    "//button[contains(text(), 'Next')]",
                    
                    # Div elements with button role
                    "//div[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'do it later')]",
                    "//div[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'skip')]",
                    "//div[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'later')]",
                    
                    # Link elements
                    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'do it later')]",
                    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'skip')]",
                    "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'later')]",
                    
                    # Span elements with clickable parent
                    "//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'do it later')]/parent::*",
                    "//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'skip')]/parent::*",
                    "//span[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'later')]/parent::*",
                    
                    # Generic button selectors
                    "//button[contains(@class, 'VfPpkd-LgbsSe')]",
                    "//button[contains(@class, 'submit')]",
                    "//button[@type='submit']",
                    
                    # Last resort - any clickable element
                    "//*[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'do it later')]",
                    "//*[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'skip')]",
                    "//*[@role='button' and contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'later')]"
                ]
                
                button_clicked = False
                for i, selector in enumerate(do_it_later_selectors):
                    try:
                        # Very fast timeout - 1 second max (was 2 seconds)
                        button = WebDriverWait(driver, 1).until(
                            EC.element_to_be_clickable((By.XPATH, selector))
                        )
                        button.click()
                        logging.info(f"Successfully clicked 'Do it Later' button for {email} using selector {i+1}")
                        button_clicked = True
                        time.sleep(1)  # Minimal wait time (was 2 seconds)
                        break
                    except TimeoutException:
                        continue
                    except Exception as e:
                        logging.warning(f"Error clicking button with selector {i+1} for {email}: {e}")
                        continue
                
                # If no button found, try JavaScript click on any visible button
                if not button_clicked:
                    try:
                        logging.info(f"No specific button found for {email}, trying JavaScript click on any button...")
                        buttons = driver.find_elements(By.TAG_NAME, "button")
                        for button in buttons:
                            try:
                                if button.is_displayed() and button.is_enabled():
                                    driver.execute_script("arguments[0].click();", button)
                                    logging.info(f"Clicked button via JavaScript for {email}")
                                    button_clicked = True
                                    time.sleep(1)  # Minimal wait time (was 2 seconds)
                                    break
                            except:
                                continue
                    except Exception as e:
                        logging.warning(f"JavaScript click failed for {email}: {e}")
                
                if button_clicked:
                    logging.info(f"Successfully handled billing page for {email}")
                    return True
                else:
                    logging.warning(f"Could not find 'Do it Later' button for {email}, but continuing...")
                    return True  # Continue anyway to avoid blocking
            
            return True  # Not a billing page, continue normally
            
        except Exception as e:
            logging.error(f"Error handling billing page for {email}: {e}")
            return True  # Continue anyway to avoid blocking
            
    def auto_retrieve_accounts_at_startup(self):
        """Automatically retrieve accounts from server at application startup"""
        try:
            logging.info("Attempting to auto-retrieve accounts at startup...")
            
            # Use a timer to delay the retrieval until after UI is fully loaded
            QTimer.singleShot(2000, self._perform_auto_retrieve)
            
        except Exception as e:
            logging.error(f"Error setting up auto-retrieve: {e}")
            
    def fetch_password_from_server(self, email):
        """Fetch password for a specific email from the server"""
        try:
            if hasattr(self, 'stored_accounts') and email in self.stored_accounts:
                logging.info(f"Password found in local cache for {email}")
                return self.stored_accounts[email]
            
            # If not in local cache, try to fetch from server
            logging.info(f"Fetching password from server for {email}")
            accounts, message = self.remote_account_manager.retrieve_accounts()
            
            if accounts:
                # Update local cache
                if not hasattr(self, 'stored_accounts'):
                    self.stored_accounts = {}
                
                for account_line in accounts:
                    if ':' in account_line:
                        account_email, password = account_line.split(':', 1)
                        account_email = account_email.strip()
                        password = password.strip()
                        self.stored_accounts[account_email] = password
                    elif ' ' in account_line:
                        parts = account_line.split(' ', 1)
                        if len(parts) == 2:
                            account_email, password = parts
                            account_email = account_email.strip()
                            password = password.strip()
                            self.stored_accounts[account_email] = password
                
                # Check if email exists in fetched accounts
                if email in self.stored_accounts:
                    logging.info(f"Password successfully fetched from server for {email}")
                    return self.stored_accounts[email]
            
            logging.warning(f"Password not found on server for {email}")
            return None
            
        except Exception as e:
            logging.error(f"Error fetching password from server for {email}: {e}")
            return None
    
    def get_account_password(self, email):
        """Get password for an account, trying local cache first, then server"""
        try:
            # First check if we have stored accounts
            if hasattr(self, 'stored_accounts') and email in self.stored_accounts:
                return self.stored_accounts[email]
            
            # If not found, try to fetch from server
            return self.fetch_password_from_server(email)
            
        except Exception as e:
            logging.error(f"Error getting password for {email}: {e}")
            return None
            
    def _perform_auto_retrieve(self):
        """Perform the actual auto-retrieve operation"""
        try:
            accounts, message = self.remote_account_manager.retrieve_accounts()
            
            if accounts:
                # Store full accounts (with passwords) for later use
                self.stored_accounts = {}
                display_accounts = []
                
                for account_line in accounts:
                    if ':' in account_line:
                        email, password = account_line.split(':', 1)
                        email = email.strip()
                        password = password.strip()
                        self.stored_accounts[email] = password
                        display_accounts.append(email)  # Only display email
                    elif ' ' in account_line:
                        parts = account_line.split(' ', 1)
                        if len(parts) == 2:
                            email, password = parts
                            email = email.strip()
                            password = password.strip()
                            self.stored_accounts[email] = password
                            display_accounts.append(email)  # Only display email
                
                # Populate the account field with emails only
                self.accounts_text.setPlainText("\n".join(display_accounts))
                logging.info(f"Auto-retrieved {len(display_accounts)} accounts at startup (passwords stored securely)")
                
                # Show a non-intrusive message
                QMessageBox.information(self, "Accounts Loaded", 
                    f"Successfully loaded {len(display_accounts)} accounts from server at startup (passwords hidden).")
            else:
                logging.info("No accounts found during auto-retrieve at startup")
                
        except Exception as e:
            logging.error(f"Error in auto-retrieve at startup: {e}")
            # Don't show error message for auto-retrieve to avoid being intrusive
            
    def is_driver_valid_for_driver(self, driver):
        """Check if a specific driver is still valid"""
        try:
            driver.current_url
            return True
        except (WebDriverException, AttributeError):
            return False

class RemoteAccountManager:
    """Handles remote account retrieval and management"""
    
    def __init__(self, server_address, port, username, password, remote_dir):
        self.server_address = server_address
        self.port = port
        self.username = username
        self.password = password
        self.remote_dir = remote_dir
        
    def connect_sftp(self):
        """Establish SFTP connection for account retrieval"""
        try:
            transport = paramiko.Transport((self.server_address, self.port))
            transport.connect(username=self.username, password=self.password)
            return paramiko.SFTPClient.from_transport(transport)
        except Exception as e:
            logging.error(f"Failed to connect to SFTP server: {e}")
            return None
            
    def retrieve_accounts(self):
        """Retrieve accounts from remote server"""
        try:
            sftp = self.connect_sftp()
            if not sftp:
                return None, "Failed to connect to server"
                
            # Try different possible account file names
            possible_files = ['accounts.json', 'accounts.txt', 'google_accounts.json', 'google_accounts.txt']
            accounts_file = None
            
            for filename in possible_files:
                try:
                    remote_path = f"{self.remote_dir}/{filename}"
                    sftp.stat(remote_path)
                    accounts_file = filename
                    logging.info(f"Found accounts file: {remote_path}")
                    break
                except FileNotFoundError:
                    continue
                    
            if not accounts_file:
                sftp.close()
                return None, "No accounts file found on server"
                
            # Download to temporary file
            with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt') as temp_file:
                temp_file_path = temp_file.name
                
            try:
                remote_path = f"{self.remote_dir}/{accounts_file}"
                sftp.get(remote_path, temp_file_path)
                
                # Read and parse accounts
                with open(temp_file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    
                accounts = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line:
                        # Handle both email:password and email password formats
                        if ':' in line:
                            # Format: email:password
                            account, password = line.split(':', 1)
                            accounts.append(f"{account.strip()}:{password.strip()}")
                        elif ' ' in line:
                            # Format: email password
                            parts = line.split(' ', 1)
                            if len(parts) == 2:
                                account, password = parts
                                accounts.append(f"{account.strip()}:{password.strip()}")
                        # Skip lines that don't match either format
                        
                logging.info(f"Retrieved {len(accounts)} accounts from server")
                return accounts, f"Successfully retrieved {len(accounts)} accounts"
                
            finally:
                # Clean up temp file
                if os.path.exists(temp_file_path):
                    os.unlink(temp_file_path)
                sftp.close()
                
        except Exception as e:
            logging.error(f"Error retrieving accounts: {e}")
            return None, f"Error: {str(e)}"
            
    def validate_account_format(self, account_line):
        """Validate account format (account:password or account password)"""
        if not account_line:
            return False
            
        # Check for email:password format
        if ':' in account_line:
            parts = account_line.split(':', 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return True
                
        # Check for email password format
        if ' ' in account_line:
            parts = account_line.split(' ', 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return True
                
        return False

class OTPHandler(QObject):
    """Thread-safe OTP handler for cross-thread communication"""
    otp_requested = pyqtSignal(object, str)  # Signal to request OTP handling with email
    
    def __init__(self, app_instance):
        super().__init__()
        self.app_instance = app_instance
        self.otp_requested.connect(self.handle_otp_safe)
        self.pending_otp_events = {}  # Dictionary to store threading.Event objects
        self.request_count = 0  # Add counter for debugging
    
    def request_otp_handling(self, driver, email):
        """Request OTP handling from main thread and return an event to wait on"""
        self.request_count += 1
        request_id = f"OTP_REQ_{self.request_count}"
        logging.info(f"[{request_id}] OTP handling requested for driver {id(driver)}, email: {email}")
        
        otp_event = threading.Event()
        self.pending_otp_events[driver] = otp_event
        logging.info(f"[{request_id}] Created event for driver {id(driver)}, total pending: {len(self.pending_otp_events)}")
        
        self.otp_requested.emit(driver, email)
        logging.info(f"[{request_id}] Signal emitted for driver {id(driver)}")
        
        return otp_event  # Return the event for the worker thread to wait on
    
    def handle_otp_safe(self, driver, email):
        """Handle OTP in main thread"""
        request_id = f"OTP_HANDLE_{id(driver)}"
        logging.info(f"[{request_id}] Starting safe OTP handling for driver {id(driver)}, email: {email}")
        
        try:
            if driver is not None:
                logging.info(f"[{request_id}] Calling handle_otp_if_needed_global for driver {id(driver)}")
                result = self.app_instance.handle_otp_if_needed_global(driver, email)
                logging.info(f"[{request_id}] handle_otp_if_needed_global returned: {result}")
            else:
                logging.warning(f"[{request_id}] Driver is None, skipping OTP handling")
        except Exception as e:
            logging.error(f"[{request_id}] Error in safe OTP handling: {e}")
        finally:
            # Set the event to signal completion
            if driver in self.pending_otp_events:
                logging.info(f"[{request_id}] Setting event for driver {id(driver)}")
                self.pending_otp_events[driver].set()
                del self.pending_otp_events[driver]
                logging.info(f"[{request_id}] Removed event for driver {id(driver)}, remaining: {len(self.pending_otp_events)}")
            else:
                logging.warning(f"[{request_id}] No pending event found for driver {id(driver)}")

class ErrorHandler(QObject):
    """Thread-safe error handler for cross-thread communication"""
    error_occurred = pyqtSignal(str, str)  # Signal to request error handling (error_type, error_message)
    
    def __init__(self, app_instance):
        super().__init__()
        self.app_instance = app_instance
        self.error_occurred.connect(self.handle_error_safe)
    
    def request_error_handling(self, error_type, error_message):
        """Request error handling from main thread"""
        self.error_occurred.emit(error_type, error_message)
    
    def handle_error_safe(self, error_type, error_message):
        """Handle error in main thread"""
        try:
            if error_type == "browser_closed":
                logging.info("Browser connection lost. Driver reset. You can try logging in again.")
                self.app_instance.driver = None
                QMessageBox.information(self.app_instance, "Browser Closed", 
                                    "Browser connection lost. Click Login to open a new browser window.")
            elif error_type == "general_error":
                QMessageBox.critical(self.app_instance, "Error", f"An error occurred: {error_message}")
                logging.error(f"An error occurred: {error_message}")
                self.app_instance.driver = None
        except Exception as e:
            logging.error(f"Error in safe error handling: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    ex = GoogleWorkspaceApp()
    sys.exit(app.exec_())




