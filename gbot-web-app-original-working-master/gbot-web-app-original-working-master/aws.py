"""
AWS Lambda Controller with Account Creation - PyQt5 Edition
Unified interface for Google Workspace Education automation
Integrates Education_creation.py functionality with AWS Lambda support
"""

import sys
import os
import json
import time
import random
import string
import logging
import traceback
import threading
import io
import zipfile
import hashlib
import math
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

# PyQt5 imports
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtCore import QThread, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTabWidget, QGroupBox, QLabel, QLineEdit, QPushButton, QTextEdit,
    QComboBox, QSpinBox, QRadioButton, QFrame, QScrollArea, QSplitter,
    QMessageBox, QApplication
)

# AWS imports
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

# Selenium imports for local account creation
try:
    from seleniumwire import webdriver as wire_webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException
    from webdriver_manager.chrome import ChromeDriverManager
    from fake_useragent import UserAgent
    from faker import Faker
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ======================================================================
# Config / Constants
# ======================================================================

APP_TITLE = "Google Workspace Automation – DEV Lambda Controller"

# Core resources (for Production/App Passwords) - DEV prefix to avoid touching existing resources
LAMBDA_ROLE_NAME = "dev-app-password-lambda-role"
PRODUCTION_LAMBDA_NAME = "dev-chromium"
S3_BUCKET_NAME = "dev-app-passwords"

# ECR (Production)
ECR_REPO_NAME = "dev-app-password-worker-repo"
ECR_IMAGE_TAG = "latest"

# Prep Process Resources
PREP_LAMBDA_PREFIX = "dev-prep-worker"
PREP_ECR_REPO_NAME = "dev-prep-worker-repo"

# ===== CREATION TAB RESOURCES (completely separate) =====
CREATION_LAMBDA_NAME = "dev-creation-worker"
CREATION_ECR_REPO_NAME = "dev-creation-repo"
CREATION_S3_BUCKET_NAME = "dev-creation-bucket"
CREATION_LAMBDA_ROLE_NAME = "dev-creation-lambda-role"
CREATION_EC2_ROLE_NAME = "dev-creation-ec2-role"
CREATION_EC2_INSTANCE_PROFILE_NAME = "dev-creation-ec2-profile"
CREATION_EC2_INSTANCE_NAME = "dev-creation-ec2-build"
CREATION_EC2_SG_NAME = "dev-creation-ec2-sg"
CREATION_EC2_KEY_NAME = "dev-creation-key"
CREATION_EC2_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{CREATION_EC2_KEY_NAME}.pem")

# EC2 build box configuration (for Production/App Passwords)
EC2_INSTANCE_NAME = "dev-ec2-build-box"
EC2_ROLE_NAME = "dev-ec2-build-role"
EC2_INSTANCE_PROFILE_NAME = "dev-ec2-build-instance-profile"
EC2_SECURITY_GROUP_NAME = "dev-ec2-build-sg"
EC2_KEY_PAIR_NAME = "dev-ec2-build-key"
EC2_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{EC2_KEY_PAIR_NAME}.pem")


# Global sets for unique generation
_used_school_names = set()
_used_person_names = set()

# Proxy rotation system
_proxy_queue = deque()
_proxy_lock = threading.Lock()
_proxy_initialized = False

# Region to country mapping
REGION_COUNTRY_MAP = {
    "nl": "Netherlands", "fr": "France", "de": "Germany", "es": "Spain",
    "it": "Italy", "en-gb": "UK", "pl": "Poland", "sv": "Sweden", "da": "Denmark",
    "en": "US", "en-us": "US", "pt-br": "Brazil", "ja": "Japan"
}

# Region to Faker locale mapping
REGION_FAKER_MAP = {
    "nl": "nl_NL", "fr": "fr_FR", "de": "de_DE", "es": "es_ES",
    "it": "it_IT", "en-gb": "en_GB", "pl": "pl_PL", "sv": "sv_SE",
    "da": "da_DK", "en": "en_US", "pt-br": "pt_BR", "ja": "ja_JP"
}


# ======================================================================
# Account Creation Helper Functions (from Education_creation.py)
# ======================================================================

def load_proxy_list(file_path="proxies.txt"):
    """Load proxies from file."""
    try:
        with open("working_proxies.txt", "r") as f:
            working_proxies = [line.strip() for line in f if line.strip()]
        if working_proxies:
            return working_proxies
    except FileNotFoundError:
        pass
    
    try:
        with open(file_path, "r") as f:
            proxies = [line.strip() for line in f if line.strip()]
        return proxies if proxies else []
    except FileNotFoundError:
        return []


def initialize_proxy_queue(force_reload=False):
    """Initialize the proxy rotation queue."""
    global _proxy_queue, _proxy_initialized
    
    with _proxy_lock:
        if _proxy_initialized and len(_proxy_queue) > 0 and not force_reload:
            return
        
        proxies = load_proxy_list()
        if proxies:
            _proxy_queue = deque(proxies)
        else:
            _proxy_queue = deque()
        _proxy_initialized = True


def get_random_proxy():
    """Get the next proxy from the rotation queue."""
    global _proxy_queue, _proxy_initialized
    
    if not _proxy_initialized:
        initialize_proxy_queue()
    
    with _proxy_lock:
        if len(_proxy_queue) == 0:
            return None
        proxy = _proxy_queue.popleft()
        _proxy_queue.append(proxy)
        return proxy


def configure_seleniumwire_proxy(proxy_line):
    """Configure selenium-wire proxy options."""
    parts = proxy_line.split(":")
    try:
        if len(parts) == 2:
            ip, port = parts
            proxy_url = f'http://{ip}:{port}'
            return {'proxy': {'http': proxy_url, 'https': proxy_url, 'no_proxy': 'localhost,127.0.0.1'}}
        elif len(parts) == 4:
            ip, port, user, password = parts
            proxy_url = f'http://{user}:{password}@{ip}:{port}'
            return {'proxy': {'http': proxy_url, 'https': proxy_url, 'no_proxy': 'localhost,127.0.0.1'}}
    except:
        pass
    return None


def add_random_delay(min_delay=1.5, max_delay=3.5):
    """Add human-like random delay."""
    delay = random.uniform(min_delay, max_delay)
    time.sleep(delay)


def human_like_typing(element, text, min_delay=0.05, max_delay=0.2):
    """Type text character by character with human-like delays."""
    for i, char in enumerate(text):
        element.send_keys(char)
        if i < len(text) - 1:
            time.sleep(random.uniform(0.2, 0.4) if char == ' ' else random.uniform(min_delay, max_delay))
    return True


def safe_type(element, text, driver, clear_first=True):
    """Safely type text into an input field."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.2)
        try:
            element.click()
        except:
            driver.execute_script("arguments[0].click();", element)
        time.sleep(0.3)
        
        if clear_first:
            try:
                element.clear()
                element.send_keys(Keys.CONTROL + 'a')
                element.send_keys(Keys.DELETE)
            except:
                driver.execute_script("arguments[0].value = '';", element)
        
        driver.execute_script("arguments[0].focus();", element)
        human_like_typing(element, text)
        return True
    except Exception as e:
        logging.error(f"Error typing text: {e}")
        return False


def safe_click(element, driver, max_attempts=3):
    """Safely click an element with retry logic."""
    for attempt in range(max_attempts):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            element.click()
            return True
        except ElementClickInterceptedException:
            try:
                driver.execute_script("arguments[0].click();", element)
                return True
            except:
                time.sleep(1)
        except:
            time.sleep(1)
    return False


def clean_name_for_email(name):
    """Clean name for email by removing accents and special characters."""
    import unicodedata
    name = unicodedata.normalize('NFD', name)
    name = ''.join(char for char in name if unicodedata.category(char) != 'Mn')
    name = name.replace(" ", "").replace("-", "").replace("'", "").replace(".", "")
    return name.lower()


def get_faker_for_region(region_code):
    """Get Faker instance for the given region."""
    locale = REGION_FAKER_MAP.get(region_code.lower(), "en_US")
    return Faker(locale)


def generate_subdomain():
    """Generate professional subdomain names."""
    educational = ["academy", "college", "institute", "school", "university", "learning", "campus", "scholars"]
    scientific = ["research", "laboratory", "science", "technology", "innovation", "discovery"]
    professional = ["enterprise", "solutions", "systems", "services", "consulting", "management"]
    descriptive = ["advanced", "premium", "elite", "professional", "modern", "innovative"]
    
    all_words = educational + scientific + professional + descriptive
    patterns = [
        lambda: random.choice(all_words),
        lambda: random.choice(all_words) + random.choice(all_words),
        lambda: random.choice(all_words) + str(random.randint(1, 999)),
    ]
    
    subdomain = random.choice(patterns)()
    return ''.join(c for c in subdomain if c.isalnum()).lower()[:50]


def generate_unique_school_name(faker_instance):
    """Generate unique school/institution name."""
    global _used_school_names
    base_nouns = ["Academy", "School", "College", "Institute", "University", "Campus", "Center"]
    descriptors = ["International", "Global", "National", "Advanced", "Modern", "Technical"]
    
    for _ in range(100):
        pattern = random.randint(1, 3)
        if pattern == 1:
            name = f"{faker_instance.company()} {random.choice(base_nouns)}"
        elif pattern == 2:
            name = f"{random.choice(descriptors)} {random.choice(base_nouns)}"
        else:
            name = f"{faker_instance.city()} {random.choice(base_nouns)}"
        
        if name.lower() not in _used_school_names:
            _used_school_names.add(name.lower())
            return name
    return f"{faker_instance.company()} {random.randint(1000, 99999)}"


def generate_unique_person_name(faker_instance):
    """Generate unique person name."""
    global _used_person_names
    for _ in range(100):
        first = faker_instance.first_name()
        last = faker_instance.last_name()
        full = f"{first} {last}".lower()
        if full not in _used_person_names:
            _used_person_names.add(full)
            return first, last
    return faker_instance.first_name(), faker_instance.last_name()


def get_phone_file_for_country(country):
    """Map country name to phone number file."""
    return {
        "Netherlands": "NumNL.txt", "France": "NumFR.txt", "Germany": "NumDE.txt",
        "Spain": "NumES.txt", "Italy": "NumIT.txt", "UK": "NumUK.txt",
        "Poland": "NumPL.txt", "Sweden": "NumSE.txt", "Denmark": "NumDK.txt",
        "US": "NumUS.txt"
    }.get(country)


def generate_phone_number(country):
    """Generate phone number for the given country."""
    file_name = get_phone_file_for_country(country)
    if file_name and os.path.exists(file_name):
        try:
            with open(file_name, 'r') as f:
                numbers = [line.strip() for line in f if line.strip()]
            if numbers:
                phone = random.choice(numbers)
                numbers.remove(phone)
                with open(file_name, 'w') as f:
                    f.write('\n'.join(numbers))
                return phone
        except:
            pass
    
    # Fallback: generate random phone
    formats = {
        "Netherlands": "+316{:08d}", "France": "+336{:08d}", "Germany": "+4915{:09d}",
        "Spain": "+346{:08d}", "Italy": "+393{:09d}", "UK": "+447{:09d}",
        "US": "+1{:010d}"
    }
    fmt = formats.get(country, "+1{:010d}")
    return fmt.format(random.randint(10000000, 99999999))


def get_country_from_url(url):
    """Determine country from the signup URL."""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        code = qs.get("region", qs.get("hl", ["en"]))[0].lower()
        return REGION_COUNTRY_MAP.get(code, "US")
    except:
        return "US"


def get_all_phone_file_counts():
    """Return list of (country, file_name, count) for phone files."""
    country_files = {
        "Netherlands": "NumNL.txt", "France": "NumFR.txt", "Germany": "NumDE.txt",
        "Spain": "NumES.txt", "Italy": "NumIT.txt", "UK": "NumUK.txt",
        "Poland": "NumPL.txt", "Sweden": "NumSE.txt", "Denmark": "NumDK.txt", "US": "NumUS.txt"
    }
    result = []
    for country, file_name in country_files.items():
        try:
            with open(file_name, 'r') as f:
                count = sum(1 for line in f if line.strip())
        except:
            count = 0
        result.append((country, file_name, count))
    return result


def find_size_dropdown(driver):
    """Find the company size dropdown."""
    for selector in [".rHGeGc-aPP78e", "[role='button'][aria-haspopup='listbox']", "button[data-value]"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            if el.is_displayed():
                return el
        except:
            continue
    return None


def select_company_size_option(driver):
    """Select 1-100 company size option."""
    try:
        options = WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'li[role="option"]')))
        for opt in options:
            if any(s in opt.text for s in ["1 à 100", "1 to 100", "1-100", "1 bis 100", "1 tot 100"]):
                safe_click(opt, driver)
                return True
        for opt in options:
            if any(c.isdigit() for c in opt.text):
                safe_click(opt, driver)
                return True
    except:
        pass
    return False


def click_next_button(driver):
    """Click next/continue button."""
    for text in ["Next", "Continue", "Suivant", "Weiter", "Volgende", "Siguiente", "Avanti"]:
        try:
            btn = driver.find_element(By.XPATH, f'//button[span[contains(text(), "{text}")]]')
            if btn.is_enabled() and btn.is_displayed():
                safe_click(btn, driver)
                return True
        except:
            continue
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for b in reversed(buttons):
            if b.is_displayed() and b.is_enabled():
                safe_click(b, driver)
                return True
    except:
        pass
    return False


def click_domain_confirmation_button(driver):
    """Click domain confirmation button."""
    for xpath in ['//button[contains(text(), "Set up account")]', '//button[contains(text(), "domein")]']:
        try:
            btn = driver.find_element(By.XPATH, xpath)
            if btn.is_displayed():
                safe_click(btn, driver)
                return True
        except:
            continue
    return click_next_button(driver)


def is_account_denied(url):
    """Check if URL indicates account was denied."""
    if not url:
        return False
    return any(p in url.lower() for p in ["signup/deny", "/edu/signup/deny"])


def generate_account_details(base_domain, count):
    """Generate unique account details with professional subdomains."""
    if not SELENIUM_AVAILABLE:
        return []
    fake = Faker()
    details = []
    used = set()
    for _ in range(count * 10):
        if len(details) >= count:
            break
        sub = generate_subdomain()
        if sub not in used:
            used.add(sub)
            pwd = fake.password(length=12, special_chars=False, digits=True, upper_case=True, lower_case=True)
            details.append(f"{sub}.{base_domain}:{pwd}")
    return details


# ======================================================================
# Account Creator Class (from Education_creation.py)
# ======================================================================

class AccountCreator:
    """Core account creation logic with Selenium automation."""
    
    def __init__(self, domain, password, slot_index, total_slots, admin_username, email_provider, signup_url, browser, use_headless=False):
        self.domain = domain
        self.password = password
        self.slot_index = slot_index
        self.total_slots = total_slots
        self.admin_username = admin_username
        self.email_provider = email_provider
        self.signup_url = signup_url
        self.browser = browser.lower()
        self.use_headless = use_headless
        self.driver = None

    def setup_driver(self):
        """Initialize the browser driver."""
        if not SELENIUM_AVAILABLE:
            raise Exception("Selenium not available. Install: pip install selenium selenium-wire webdriver-manager fake-useragent faker")
        
        proxy_line = get_random_proxy()
        seleniumwire_options = configure_seleniumwire_proxy(proxy_line) if proxy_line else None
        
        if self.browser == "chrome":
            options = ChromeOptions()
            if self.use_headless:
                options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--lang=en-US")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            
            ua = UserAgent()
            options.add_argument(f"--user-agent={ua.chrome}")
            
            service = ChromeService(ChromeDriverManager().install())
            if seleniumwire_options:
                self.driver = wire_webdriver.Chrome(service=service, options=options, seleniumwire_options=seleniumwire_options)
            else:
                from selenium import webdriver
                self.driver = webdriver.Chrome(service=service, options=options)
        else:
            # Firefox
            options = FirefoxOptions()
            if self.use_headless:
                options.add_argument("--headless")
            options.set_preference("dom.webdriver.enabled", False)
            
            if seleniumwire_options:
                self.driver = wire_webdriver.Firefox(options=options, seleniumwire_options=seleniumwire_options)
            else:
                from selenium import webdriver
                self.driver = webdriver.Firefox(options=options)
        
        # Anti-detection
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # Window positioning for concurrent runs
        if not self.use_headless and self.total_slots > 1:
            rows = math.ceil(math.sqrt(self.total_slots))
            cols = math.ceil(self.total_slots / rows)
            w, h = 1920 // cols, 1080 // rows
            x, y = (self.slot_index % cols) * w, (self.slot_index // cols) * h
            self.driver.set_window_position(x, y)
            self.driver.set_window_size(w, h)

    def cleanup(self):
        """Close the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None

    def create_account(self):
        """Main account creation flow."""
        try:
            self.setup_driver()
            if not self.driver:
                raise Exception("Failed to initialize driver")
            
            logging.info(f"Starting account creation for {self.domain}")
            self.driver.get(self.signup_url)
            
            if is_account_denied(self.driver.current_url):
                raise Exception("Account creation denied - flagged as spam")
            
            add_random_delay()
            faker = get_faker_for_region(self.signup_url.split("region=")[-1][:2] if "region=" in self.signup_url else "en")
            
            # Step 1: School Name
            school_input = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[2]/div/div/span[2]/input'))
            )
            school_name = generate_unique_school_name(faker)
            safe_type(school_input, school_name, self.driver)
            logging.info(f"Entered school: {school_name}")
            add_random_delay()
            
            # Step 2: Company size dropdown
            dropdown = find_size_dropdown(self.driver)
            if dropdown:
                safe_click(dropdown, self.driver)
                add_random_delay()
                select_company_size_option(self.driver)
            add_random_delay()
            
            # Step 3: Checkbox
            try:
                checkbox = self.driver.find_element(By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/fieldset/div/label[2]/div[2]')
                safe_click(checkbox, self.driver)
            except:
                pass
            add_random_delay()
            
            # Step 4: Next
            click_next_button(self.driver)
            add_random_delay()
            
            # Step 5: First and Last Name
            first_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[3]/div[1]/div[1]/span[2]/input'))
            )
            last_input = self.driver.find_element(By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[3]/div[2]/div[1]/span[2]/input')
            first_name, last_name = generate_unique_person_name(faker)
            safe_type(first_input, first_name, self.driver)
            safe_type(last_input, last_name, self.driver)
            logging.info(f"Entered name: {first_name} {last_name}")
            add_random_delay()
            
            # Step 6: Email
            email_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[4]/div[1]/span[2]/input'))
            )
            generated_email = f"{clean_name_for_email(first_name)}{clean_name_for_email(last_name)}{self.email_provider}"
            safe_type(email_input, generated_email, self.driver)
            logging.info(f"Entered email: {generated_email}")
            add_random_delay()
            
            # Step 7: Phone Number
            country = get_country_from_url(self.signup_url)
            phone_input = WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable((By.ID, "phone-input")))
            phone = generate_phone_number(country)
            safe_type(phone_input, phone, self.driver)
            phone_input.send_keys(Keys.RETURN)
            logging.info(f"Entered phone: {phone}")
            add_random_delay()
            
            # Step 8: Additional button
            try:
                btn = self.driver.execute_script('return document.querySelector("#yDmH0d > c-wiz.SSPGKf > div > div > div.LPrTZd > main > div > div > section > div:nth-child(3) > button")')
                if btn:
                    safe_click(btn, self.driver)
            except:
                pass
            add_random_delay()
            
            # Step 9: Domain
            domain_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[2]/div[2]/div/div[1]/span[2]/input'))
            )
            safe_type(domain_input, self.domain, self.driver)
            domain_input.send_keys(Keys.RETURN)
            logging.info(f"Entered domain: {self.domain}")
            add_random_delay(2.5, 5.0)
            
            # Step 10: Domain confirmation
            click_domain_confirmation_button(self.driver)
            add_random_delay(3.0, 6.0)
            
            # Step 11: Accept
            try:
                btn = self.driver.execute_script('return document.querySelector("#yDmH0d > c-wiz.SSPGKf > div > div > div.LPrTZd > main > div > div > div > nav > span > div > button")')
                if btn:
                    safe_click(btn, self.driver)
            except:
                pass
            add_random_delay(2.5, 5.0)
            
            # Step 12: Admin credentials
            admin_input = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[1]/div/div/div[1]/span[2]/input'))
            )
            safe_type(admin_input, self.admin_username, self.driver)
            
            pwd_inputs = self.driver.find_elements(By.XPATH, '//input[@type="password"]')
            if len(pwd_inputs) >= 2:
                safe_type(pwd_inputs[0], self.password, self.driver)
                safe_type(pwd_inputs[1], self.password, self.driver)
            else:
                pwd_input = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[2]/div/div[1]/span[2]/input'))
                )
                safe_type(pwd_input, self.password, self.driver)
            
            logging.info("Entered admin credentials")
            time.sleep(3)
            
            # Agree button
            try:
                agree = self.driver.find_element(By.XPATH, '//button[span[contains(text(), "Agree") or contains(text(), "Akkoord")]]')
                safe_click(agree, self.driver)
                time.sleep(2)
            except:
                pass
            
            # Final checkbox
            try:
                final_cb = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[3]/div/div/span[1]'))
                )
                safe_click(final_cb, self.driver)
            except:
                pass
            
            # Final button
            try:
                final_btn = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[5]/div/button/span[6]'))
                )
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", final_btn)
                try:
                    final_btn.click()
                except:
                    self.driver.execute_script("arguments[0].click();", final_btn)
            except Exception as e:
                logging.error(f"Could not click final button: {e}")
            
            add_random_delay(2, 4)
            
            # Check result
            if is_account_denied(self.driver.current_url):
                created_email = f"{self.admin_username}@{self.domain}"
                with open("account_denied.txt", "a") as f:
                    f.write(f"{created_email}:{self.password}\n")
                return {"status": "denied", "email": created_email}
            else:
                created_email = f"{self.admin_username}@{self.domain}"
                with open("account_valid.txt", "a") as f:
                    f.write(f"{created_email}:{self.password}\n")
                logging.info(f"SUCCESS: {created_email}")
                return {"status": "success", "email": created_email}
                
        except Exception as e:
            logging.error(f"Account creation failed for {self.domain}: {e}")
            return {"status": "error", "message": str(e), "domain": self.domain}
        finally:
            self.cleanup()


class AccountCreationWorker(QThread):
    """QThread worker for concurrent account creation."""
    finished_signal = pyqtSignal(int, str)
    
    def __init__(self, domain, password, slot_index, total_slots, admin_username, email_provider, signup_url, browser, use_headless=False):
        super().__init__()
        self.domain = domain
        self.password = password
        self.slot_index = slot_index
        self.total_slots = total_slots
        self.admin_username = admin_username
        self.email_provider = email_provider
        self.signup_url = signup_url
        self.browser = browser
        self.use_headless = use_headless

    def run(self):
        try:
            creator = AccountCreator(
                self.domain, self.password, self.slot_index, self.total_slots,
                self.admin_username, self.email_provider, self.signup_url, 
                self.browser, self.use_headless
            )
            result = creator.create_account()
            if result.get("status") == "success":
                self.finished_signal.emit(self.slot_index, f"✅ SUCCESS: {self.domain}")
            elif result.get("status") == "denied":
                self.finished_signal.emit(self.slot_index, f"❌ DENIED: {self.domain}")
            else:
                self.finished_signal.emit(self.slot_index, f"⚠️ ERROR: {self.domain} - {result.get('message', 'Unknown')}")
        except Exception as e:
            self.finished_signal.emit(self.slot_index, f"❌ FAILED: {self.domain} - {e}")


# ======================================================================
# Dark Theme Stylesheet (from Education_creation.py)
# ======================================================================

DARK_THEME_STYLE = """
QWidget {
    background-color: #232323;
    color: #e5e5e5;
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
}
QGroupBox {
    background-color: #2d2d2d;
    border: 1.5px solid #444;
    border-radius: 10px;
    margin-top: 18px;
    font-weight: bold;
    color: #e5e5e5;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top center;
    padding: 0 8px;
    background-color: #232323;
    color: #bdbdbd;
    font-size: 14px;
    font-weight: bold;
    border-radius: 6px;
}
QLabel {
    color: #e5e5e5;
    font-weight: 500;
    padding: 2px 0;
}
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background-color: #232323;
    border: 1.5px solid #444;
    border-radius: 6px;
    color: #e5e5e5;
    padding: 8px 10px;
    font-size: 13px;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1.5px solid #888;
}
QComboBox QAbstractItemView {
    background-color: #232323;
    border: 1.5px solid #444;
    color: #e5e5e5;
    selection-background-color: #444;
}
QPushButton {
    background-color: #353535;
    color: #e5e5e5;
    border: 1.5px solid #444;
    border-radius: 6px;
    padding: 10px 18px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #444;
    color: #fff;
    border: 1.5px solid #888;
}
QPushButton:pressed {
    background-color: #222;
}
QPushButton#startButton {
    background-color: #22c55e;
    color: #fff;
    border: 1.5px solid #22c55e;
}
QPushButton#startButton:hover {
    background-color: #16a34a;
}
QPushButton#generateButton {
    background-color: #e5e5e5;
    color: #232323;
}
QTabWidget::pane {
    border: 1px solid #444;
    background-color: #232323;
}
QTabBar::tab {
    background-color: #2d2d2d;
    color: #e5e5e5;
    padding: 10px 20px;
    border: 1px solid #444;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #353535;
    color: #fff;
}
QScrollBar:vertical {
    background-color: #232323;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background-color: #444;
    border-radius: 5px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background-color: #888;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""


# ======================================================================
# Main Application Class
# ======================================================================

class AwsEducationApp(QMainWindow):
    """Main PyQt5 application window."""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setGeometry(100, 100, 1400, 950)
        self.setMinimumSize(1200, 800)
        
        self.session = None
        self.aws_account_id = None
        self.worker_queue = deque()
        self.active_workers = {}
        self.available_slots = []
        self.concurrent_count = 0
        self.selected_url = "https://workspace.google.com/edu/signup?hl=en&region=nl"
        self.local_prep_thread = None
        self.stop_event = None
        
        self._build_ui()
        self.setStyleSheet(DARK_THEME_STYLE)
        
        # Initialize proxy queue
        if SELENIUM_AVAILABLE:
            initialize_proxy_queue()
        
        # Phone count timer
        self.phone_timer = QTimer(self)
        self.phone_timer.timeout.connect(self.update_phone_counts)
        self.phone_timer.start(5000)
        self.update_phone_counts()

    def _build_ui(self):
        """Build the main UI."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(15, 15, 15, 15)
        
        # Header
        header = QLabel("🚀 Google Workspace Automation – Lambda Controller")
        header.setStyleSheet("font-size: 22px; font-weight: bold; color: #fff; padding: 10px;")
        main_layout.addWidget(header)
        
        # Splitter for content and logs
        splitter = QSplitter(QtCore.Qt.Vertical)
        main_layout.addWidget(splitter, 1)
        
        # Top content area
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        
        # Credentials frame
        creds_group = QGroupBox("🔐 AWS & Configuration")
        creds_layout = QGridLayout(creds_group)
        
        creds_layout.addWidget(QLabel("Access Key ID:"), 0, 0)
        self.access_key_input = QLineEdit()
        creds_layout.addWidget(self.access_key_input, 0, 1)
        
        creds_layout.addWidget(QLabel("Secret Access Key:"), 0, 2)
        self.secret_key_input = QLineEdit()
        self.secret_key_input.setEchoMode(QLineEdit.Password)
        creds_layout.addWidget(self.secret_key_input, 0, 3)
        
        creds_layout.addWidget(QLabel("Region:"), 1, 0)
        self.region_input = QLineEdit("eu-west-1")
        creds_layout.addWidget(self.region_input, 1, 1)
        self.connect_btn = QPushButton("🔗 Test Connection")
        self.connect_btn.clicked.connect(self.on_test_connection)
        creds_layout.addWidget(self.connect_btn, 0, 4, 2, 1)
        
        creds_layout.addWidget(QLabel("S3 Bucket:"), 1, 2)
        self.s3_bucket_input = QLineEdit(S3_BUCKET_NAME)
        creds_layout.addWidget(self.s3_bucket_input, 1, 3)
        
        self.save_creds_btn = QPushButton("💾 Save Credentials")
        self.save_creds_btn.clicked.connect(self.on_save_credentials)
        self.save_creds_btn.setStyleSheet("background-color: #28a745; color: white;")
        creds_layout.addWidget(self.save_creds_btn, 1, 4)
        
        # Load saved credentials on startup
        self._load_saved_credentials()
        
        top_layout.addWidget(creds_group)
        
        # Tab widget
        self.tabs = QTabWidget()
        top_layout.addWidget(self.tabs, 1)
        
        # Add tabs
        self._build_infra_tab()
        self._build_lambda_tab()
        self._build_ec2_tab()
        self._build_prep_tab()
        self._build_creation_tab()
        
        splitter.addWidget(top_widget)
        
        # Log area
        log_group = QGroupBox("📋 Log Output")
        log_layout = QVBoxLayout(log_group)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setStyleSheet("font-family: Consolas, monospace; font-size: 12px; background-color: #18181b;")
        log_layout.addWidget(self.log_output)
        splitter.addWidget(log_group)
        
        splitter.setSizes([600, 250])
        
        # Status bar
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888; padding: 5px;")
        main_layout.addWidget(self.status_label)

    def _load_saved_credentials(self):
        """Load saved AWS credentials from file."""
        creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_credentials.json")
        try:
            if os.path.exists(creds_file):
                with open(creds_file, 'r') as f:
                    creds = json.load(f)
                self.access_key_input.setText(creds.get('access_key_id', ''))
                self.secret_key_input.setText(creds.get('secret_access_key', ''))
                self.region_input.setText(creds.get('region', 'eu-west-1'))
                self.s3_bucket_input.setText(creds.get('s3_bucket', S3_BUCKET_NAME))
                print("Loaded saved AWS credentials")
        except Exception as e:
            print(f"Could not load saved credentials: {e}")
    
    def on_save_credentials(self):
        """Save AWS credentials to file."""
        creds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aws_credentials.json")
        creds = {
            'access_key_id': self.access_key_input.text().strip(),
            'secret_access_key': self.secret_key_input.text().strip(),
            'region': self.region_input.text().strip(),
            's3_bucket': self.s3_bucket_input.text().strip()
        }
        try:
            with open(creds_file, 'w') as f:
                json.dump(creds, f, indent=2)
            self.log("✅ AWS credentials saved successfully!")
            QMessageBox.information(self, "Saved", "AWS credentials saved successfully!")
        except Exception as e:
            self.log(f"❌ Failed to save credentials: {e}")
            QMessageBox.warning(self, "Error", f"Failed to save credentials: {e}")

    def _build_infra_tab(self):
        """Build Infrastructure tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        group = QGroupBox("⚙️ Core AWS Resources")
        group_layout = QVBoxLayout(group)
        
        btn1 = QPushButton("Create Core Resources (IAM, ECR, S3)")
        btn1.clicked.connect(self.on_create_infrastructure)
        group_layout.addWidget(btn1)
        
        btn2 = QPushButton("Inspect Resources")
        btn2.clicked.connect(self.on_inspect_resources)
        group_layout.addWidget(btn2)
        
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "1) Infrastructure")

    def _build_lambda_tab(self):
        """Build Lambda tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        group = QGroupBox("⚡ Production Lambda Management")
        group_layout = QVBoxLayout(group)
        
        info = QLabel("Workflow: Create Resources → Build EC2 Image → Create Lambda → Invoke")
        info.setWordWrap(True)
        group_layout.addWidget(info)
        
        btn1 = QPushButton("Create/Update Production Lambda")
        btn1.clicked.connect(self.on_create_lambdas)
        group_layout.addWidget(btn1)
        
        # Users input
        group_layout.addWidget(QLabel("Account Input (email:password, one per line):"))
        self.lambda_users_input = QTextEdit()
        self.lambda_users_input.setMaximumHeight(120)
        self.lambda_users_input.setPlaceholderText("user@domain.com:password123\nuser2@domain.com:password456")
        group_layout.addWidget(self.lambda_users_input)
        
        # Batch size setting
        batch_layout = QHBoxLayout()
        batch_layout.addWidget(QLabel("🔢 Users per batch:"))
        self.lambda_batch_size = QSpinBox()
        self.lambda_batch_size.setRange(1, 50)
        self.lambda_batch_size.setValue(3)
        self.lambda_batch_size.setToolTip("Number of users to process before waiting for completion")
        batch_layout.addWidget(self.lambda_batch_size)
        batch_layout.addStretch()
        group_layout.addLayout(batch_layout)
        
        btn2 = QPushButton("Invoke Production Lambda")
        btn2.clicked.connect(self.on_invoke_lambda)
        group_layout.addWidget(btn2)
        
        btn3 = QPushButton("Delete All Lambdas")
        btn3.clicked.connect(self.on_delete_lambdas)
        group_layout.addWidget(btn3)
        
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "2) Lambda")

    def _build_ec2_tab(self):
        """Build EC2 tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        group = QGroupBox("🖥️ EC2 Build Box (Docker Image Builder)")
        group_layout = QVBoxLayout(group)
        
        info = QLabel("Launches EC2 to build and push Docker images to ECR.")
        info.setWordWrap(True)
        group_layout.addWidget(info)
        
        btn1 = QPushButton("Create/Prepare EC2 Build Box")
        btn1.clicked.connect(self.on_ec2_create)
        group_layout.addWidget(btn1)
        
        btn2 = QPushButton("Show EC2 Status")
        btn2.clicked.connect(self.on_ec2_status)
        group_layout.addWidget(btn2)
        
        btn3 = QPushButton("Terminate EC2 Build Box")
        btn3.clicked.connect(self.on_ec2_terminate)
        group_layout.addWidget(btn3)
        
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "3) EC2 Build")

    def _build_prep_tab(self):
        """Build Prep Process tab."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        group = QGroupBox("🔧 Prep Process")
        group_layout = QVBoxLayout(group)
        
        btn1 = QPushButton("Create Prep Infrastructure")
        btn1.clicked.connect(self.on_prep_create_infrastructure)
        group_layout.addWidget(btn1)
        
        btn2 = QPushButton("Launch Prep Build Box")
        btn2.clicked.connect(self.on_prep_launch_build)
        group_layout.addWidget(btn2)
        
        group_layout.addWidget(QLabel("Users (email:password):"))
        self.prep_users_input = QTextEdit()
        self.prep_users_input.setMaximumHeight(100)
        group_layout.addWidget(self.prep_users_input)
        
        btn3 = QPushButton("Invoke Prep Lambda")
        btn3.clicked.connect(self.on_prep_invoke)
        group_layout.addWidget(btn3)
        
        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("background-color: #444;")
        group_layout.addWidget(separator)
        
        # Local prep section header
        local_label = QLabel("🖥️ Run Locally (Parallel)")
        local_label.setStyleSheet("font-weight: bold; font-size: 14px; margin-top: 10px;")
        group_layout.addWidget(local_label)
        
        # Concurrency setting
        conc_layout = QHBoxLayout()
        conc_layout.addWidget(QLabel("Concurrent Accounts:"))
        self.prep_concurrent_spin = QSpinBox()
        self.prep_concurrent_spin.setRange(1, 10)
        self.prep_concurrent_spin.setValue(4)
        self.prep_concurrent_spin.setToolTip("Number of browser windows to run in parallel")
        conc_layout.addWidget(self.prep_concurrent_spin)
        conc_layout.addWidget(QLabel("(Windows will be tiled)"))
        conc_layout.addStretch()
        group_layout.addLayout(conc_layout)
        
        # Buttons
        btn_layout = QHBoxLayout()
        
        btn4 = QPushButton("▶️ Run Prep Locally (Parallel)")
        btn4.setStyleSheet("background-color: #2563eb; color: white;")
        btn4.clicked.connect(self.on_prep_local)
        btn_layout.addWidget(btn4)
        
        self.prep_stop_btn = QPushButton("⏹️ Stop All")
        self.prep_stop_btn.setStyleSheet("background-color: #dc2626; color: white;")
        self.prep_stop_btn.setEnabled(False)
        self.prep_stop_btn.clicked.connect(self.on_prep_stop)
        btn_layout.addWidget(self.prep_stop_btn)
        
        group_layout.addLayout(btn_layout)
        
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(tab, "4) Prep Process")

    def _build_creation_tab(self):
        """Build Account Creation tab with scrollable layout and AWS deployment."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        
        # Scrollable area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        layout.setSpacing(12)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # ===== SECTION 1: Local Configuration =====
        config_group = QGroupBox("⚙️ Configuration Settings")
        config_layout = QGridLayout(config_group)
        
        config_layout.addWidget(QLabel("👤 Admin Username:"), 0, 0)
        self.admin_combo = QComboBox()
        self.admin_combo.addItems(["admin", "webmail", "administrator", "support", "info"])
        self.admin_combo.setEditable(True)
        config_layout.addWidget(self.admin_combo, 0, 1)
        
        config_layout.addWidget(QLabel("📧 Email Provider:"), 0, 2)
        self.email_combo = QComboBox()
        self.email_combo.addItems(["@kpnmail.nl", "@gmail.com", "@outlook.com", "@hotmail.com"])
        self.email_combo.setEditable(True)
        config_layout.addWidget(self.email_combo, 0, 3)
        
        config_layout.addWidget(QLabel("🌍 Region:"), 1, 0)
        self.geo_combo = QComboBox()
        self.geo_combo.addItems(["Netherlands - nl", "France - fr", "Germany - de", "Spain - es", "Italy - it", "UK - en-gb", "Poland - pl"])
        self.geo_combo.currentTextChanged.connect(self.on_geo_changed)
        config_layout.addWidget(self.geo_combo, 1, 1)
        
        config_layout.addWidget(QLabel("🌐 Browser:"), 1, 2)
        self.browser_combo = QComboBox()
        self.browser_combo.addItems(["Chrome", "Firefox"])
        config_layout.addWidget(self.browser_combo, 1, 3)
        
        config_layout.addWidget(QLabel("🔲 Headless:"), 2, 0)
        self.headless_combo = QComboBox()
        self.headless_combo.addItems(["No", "Yes"])
        config_layout.addWidget(self.headless_combo, 2, 1)
        
        layout.addWidget(config_group)
        
        # ===== SECTION 2: Auto-Generate =====
        gen_group = QGroupBox("🎯 Auto-Generate Account Details")
        gen_layout = QHBoxLayout(gen_group)
        gen_layout.addWidget(QLabel("Base Domain:"))
        self.base_domain_input = QLineEdit()
        self.base_domain_input.setPlaceholderText("example.com")
        gen_layout.addWidget(self.base_domain_input)
        gen_layout.addWidget(QLabel("Count:"))
        self.account_count_spin = QSpinBox()
        self.account_count_spin.setRange(1, 100)
        self.account_count_spin.setValue(5)
        gen_layout.addWidget(self.account_count_spin)
        gen_btn = QPushButton("🔄 Generate")
        gen_btn.setObjectName("generateButton")
        gen_btn.clicked.connect(self.generate_accounts)
        gen_layout.addWidget(gen_btn)
        layout.addWidget(gen_group)
        
        # ===== SECTION 3: Account Details Input =====
        acc_group = QGroupBox("📋 Account Details (domain:password)")
        acc_layout = QVBoxLayout(acc_group)
        self.creation_domain_input = QTextEdit()
        self.creation_domain_input.setPlaceholderText("subdomain.example.com:password123\nsubdomain2.example.com:password456")
        self.creation_domain_input.setMinimumHeight(100)
        self.creation_domain_input.setMaximumHeight(150)
        acc_layout.addWidget(self.creation_domain_input)
        
        conc_layout = QHBoxLayout()
        conc_layout.addWidget(QLabel("🔄 Concurrent:"))
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        self.concurrent_spin.setValue(1)
        conc_layout.addWidget(self.concurrent_spin)
        conc_layout.addStretch()
        acc_layout.addLayout(conc_layout)
        layout.addWidget(acc_group)
        
        # ===== SECTION 4: Local Execution =====
        local_group = QGroupBox("🖥️ Local Account Creation")
        local_layout = QVBoxLayout(local_group)
        
        start_btn = QPushButton("🚀 Start Local Account Creation")
        start_btn.setObjectName("startButton")
        start_btn.clicked.connect(self.start_creation_process)
        start_btn.setMinimumHeight(45)
        local_layout.addWidget(start_btn)
        
        self.phone_count_label = QLabel()
        self.phone_count_label.setStyleSheet("font-size: 12px; color: #888; background: #18181b; padding: 8px; border-radius: 6px;")
        local_layout.addWidget(self.phone_count_label)
        layout.addWidget(local_group)
        
        # ===== SECTION 5: AWS Infrastructure =====
        aws_group = QGroupBox("☁️ AWS Infrastructure (Account Creation Lambda)")
        aws_layout = QVBoxLayout(aws_group)
        
        info_label = QLabel("Build Docker image with Dockercreation, deploy to ECR, and create Lambda for cloud-based account creation.")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #888; font-style: italic;")
        aws_layout.addWidget(info_label)
        
        btn_layout1 = QHBoxLayout()
        
        core_btn = QPushButton("⚡ Create Core Resources (IAM, ECR, S3)")
        core_btn.clicked.connect(self.on_creation_create_core_resources)
        btn_layout1.addWidget(core_btn)
        
        ec2_btn = QPushButton("🖥️ Launch EC2 Build Box")
        ec2_btn.clicked.connect(self.on_creation_launch_ec2)
        btn_layout1.addWidget(ec2_btn)
        
        aws_layout.addLayout(btn_layout1)
        
        btn_layout2 = QHBoxLayout()
        
        ec2_status_btn = QPushButton("📊 EC2 Status")
        ec2_status_btn.clicked.connect(self.on_creation_ec2_status)
        btn_layout2.addWidget(ec2_status_btn)
        
        ec2_term_btn = QPushButton("🛑 Terminate EC2")
        ec2_term_btn.clicked.connect(self.on_creation_terminate_ec2)
        btn_layout2.addWidget(ec2_term_btn)
        
        aws_layout.addLayout(btn_layout2)
        layout.addWidget(aws_group)
        
        # ===== SECTION 6: Lambda Management =====
        lambda_group = QGroupBox("⚡ Creation Lambda Management")
        lambda_layout = QVBoxLayout(lambda_group)
        
        lambda_btn = QPushButton("🔧 Create/Update Creation Lambda")
        lambda_btn.clicked.connect(self.on_creation_create_lambda)
        lambda_layout.addWidget(lambda_btn)
        
        # Lambda invoke section
        lambda_layout.addWidget(QLabel("Accounts for Lambda invocation (domain:password):"))
        self.creation_lambda_input = QTextEdit()
        self.creation_lambda_input.setPlaceholderText("subdomain.domain.com:Password123\nsubdomain2.domain.com:Password456")
        self.creation_lambda_input.setMinimumHeight(80)
        self.creation_lambda_input.setMaximumHeight(120)
        lambda_layout.addWidget(self.creation_lambda_input)
        
        # Batch size setting
        batch_layout = QHBoxLayout()
        batch_layout.addWidget(QLabel("🔢 Accounts per batch:"))
        self.creation_batch_size = QSpinBox()
        self.creation_batch_size.setRange(1, 20)
        self.creation_batch_size.setValue(3)
        self.creation_batch_size.setToolTip("Number of accounts to process per batch. Waits for completion before next batch.")
        batch_layout.addWidget(self.creation_batch_size)
        batch_layout.addStretch()
        lambda_layout.addLayout(batch_layout)
        
        invoke_btn_layout = QHBoxLayout()
        
        invoke_btn = QPushButton("🚀 Invoke Creation Lambda")
        invoke_btn.setStyleSheet("background-color: #2563eb; color: white;")
        invoke_btn.clicked.connect(self.on_creation_invoke_lambda)
        invoke_btn_layout.addWidget(invoke_btn)
        
        self.creation_status_label = QLabel("Ready")
        self.creation_status_label.setStyleSheet("color: #888;")
        invoke_btn_layout.addWidget(self.creation_status_label)
        
        lambda_layout.addLayout(invoke_btn_layout)
        layout.addWidget(lambda_group)
        
        # ===== SECTION 7: S3 Results =====
        s3_group = QGroupBox("📁 S3 Results")
        s3_layout = QVBoxLayout(s3_group)
        
        s3_btn_layout = QHBoxLayout()
        
        fetch_btn = QPushButton("📥 Fetch Results from S3")
        fetch_btn.clicked.connect(self.on_creation_fetch_s3_results)
        s3_btn_layout.addWidget(fetch_btn)
        
        clear_btn = QPushButton("🗑️ Clear S3 Results")
        clear_btn.clicked.connect(self.on_creation_clear_s3_results)
        s3_btn_layout.addWidget(clear_btn)
        
        s3_layout.addLayout(s3_btn_layout)
        
        self.s3_results_display = QTextEdit()
        self.s3_results_display.setReadOnly(True)
        self.s3_results_display.setPlaceholderText("S3 results will appear here...")
        self.s3_results_display.setMinimumHeight(100)
        self.s3_results_display.setMaximumHeight(150)
        self.s3_results_display.setStyleSheet("background-color: #18181b; font-family: Consolas, monospace;")
        s3_layout.addWidget(self.s3_results_display)
        
        layout.addWidget(s3_group)
        
        # Add stretch at end
        layout.addStretch()
        
        scroll.setWidget(scroll_content)
        tab_layout.addWidget(scroll)
        
        self.tabs.addTab(tab, "5) Account Creation")

    # ======================================================================
    # Helper Methods
    # ======================================================================

    def log(self, message):
        """Thread-safe logging."""
        timestamp = time.strftime("%H:%M:%S")
        text = f"[{timestamp}] {message}"
        print(text)
        QTimer.singleShot(0, lambda: self._append_log(text))

    def _append_log(self, text):
        self.log_output.append(text)
        self.status_label.setText(text[:100])

    def get_session(self):
        """Get boto3 session."""
        access_key = self.access_key_input.text().strip()
        secret_key = self.secret_key_input.text().strip()
        region = self.region_input.text().strip()
        if not all([access_key, secret_key, region]):
            raise ValueError("Please provide AWS credentials")
        self.session = boto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region)
        return self.session

    def _get_account_id(self, session):
        if self.aws_account_id:
            return self.aws_account_id
        sts = session.client("sts")
        self.aws_account_id = sts.get_caller_identity()["Account"]
        return self.aws_account_id

    def update_phone_counts(self):
        """Update phone number counts display."""
        counts = get_all_phone_file_counts()
        text = "<b>📱 Phone Numbers:</b> "
        for country, _, count in counts:
            if count > 0:
                text += f"{country}: <b>{count}</b> | "
        self.phone_count_label.setText(text.rstrip(" | "))

    def on_geo_changed(self, text):
        """Handle geo selection change."""
        if " - " in text:
            code = text.split(" - ")[1]
            self.selected_url = f"https://workspace.google.com/edu/signup?hl=en&region={code}"

    # ======================================================================
    # AWS Operations
    # ======================================================================

    def on_test_connection(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            self.log(f"✅ Connected to AWS account {account_id}")
            QMessageBox.information(self, "Success", f"Connected to AWS account {account_id}")
        except Exception as e:
            self.log(f"❌ Connection failed: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_create_infrastructure(self):
        try:
            session = self.get_session()
            self.log("Creating core infrastructure...")
            
            # IAM Role
            iam = session.client("iam")
            policies = [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            ]
            self._create_iam_role(session, LAMBDA_ROLE_NAME, "lambda.amazonaws.com", policies)
            
            # ECR
            self._create_ecr_repo(session, ECR_REPO_NAME)
            
            # S3
            self._create_s3_bucket(session)
            
            self.log("✅ Infrastructure created")
            QMessageBox.information(self, "Success", "Infrastructure created")
        except Exception as e:
            self.log(f"❌ Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_inspect_resources(self):
        try:
            session = self.get_session()
            self.log("Inspecting resources...")
            
            # IAM
            iam = session.client("iam")
            for page in iam.get_paginator("list_roles").paginate():
                for role in page.get("Roles", []):
                    if "edu-gw" in role["RoleName"]:
                        self.log(f"IAM Role: {role['RoleName']}")
            
            # ECR
            ecr = session.client("ecr")
            for page in ecr.get_paginator("describe_repositories").paginate():
                for repo in page.get("repositories", []):
                    self.log(f"ECR Repo: {repo['repositoryName']}")
            
            # Lambda
            lam = session.client("lambda")
            for page in lam.get_paginator("list_functions").paginate():
                for fn in page.get("Functions", []):
                    if "edu-gw" in fn["FunctionName"]:
                        self.log(f"Lambda: {fn['FunctionName']}")
            
            self.log("✅ Inspection complete")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_create_lambdas(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_input.text().strip()
            
            role_arn = self._ensure_lambda_role(session)
            image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO_NAME}:{ECR_IMAGE_TAG}"
            
            self._create_or_update_lambda(session, PRODUCTION_LAMBDA_NAME, role_arn, 600, image_uri=image_uri)
            
            self.log("✅ Lambda created/updated")
            QMessageBox.information(self, "Success", "Lambda created/updated")
        except Exception as e:
            self.log(f"❌ Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_invoke_lambda(self):
        """Invoke Lambda with ALL users in batches. Each Lambda processes multiple users internally (in batches of 3).
        
        The batch_size controls how many users to send per Lambda invocation.
        The Lambda will internally process them in parallel batches of 3.
        This significantly reduces costs by minimizing Lambda cold starts.
        """
        try:
            session = self.get_session()
            lam = session.client("lambda")
            
            text = self.lambda_users_input.toPlainText().strip()
            if not text:
                QMessageBox.warning(self, "Warning", "Enter users first")
                return
            
            users = [line.strip().split(":", 1) for line in text.split("\n") if ":" in line]
            batch_size = self.lambda_batch_size.value()  # Users per Lambda invocation
            total_users = len(users)
            
            if total_users == 0:
                QMessageBox.warning(self, "Warning", "No valid email:password pairs found")
                return
            
            # Calculate number of Lambda invocations needed
            total_invocations = (total_users + batch_size - 1) // batch_size
            
            self.log(f"📊 Processing {total_users} users in {total_invocations} Lambda invocation(s)")
            self.log(f"   Each Lambda will process up to {batch_size} users (in internal batches of 3)")
            
            total_success = 0
            total_failed = 0
            
            # Process in batches - each batch is ONE Lambda invocation with multiple users
            for batch_num, i in enumerate(range(0, total_users, batch_size), 1):
                batch = users[i:i + batch_size]
                batch_end = min(i + batch_size, total_users)
                
                self.log(f"\n{'='*50}")
                self.log(f"🚀 Lambda Invocation {batch_num}/{total_invocations}: Users {i+1}-{batch_end}")
                self.log(f"{'='*50}")
                
                # Build the batch event with ALL users for this Lambda
                users_list = [
                    {"email": email.strip(), "password": password.strip()}
                    for email, password in batch
                ]
                
                event = {"users": users_list}
                
                try:
                    # Single Lambda invocation with multiple users
                    self.log(f"   Invoking Lambda with {len(users_list)} users...")
                    QApplication.processEvents()  # Keep UI responsive
                    
                    resp = lam.invoke(
                        FunctionName=PRODUCTION_LAMBDA_NAME, 
                        InvocationType="RequestResponse",  # Wait for completion
                        Payload=json.dumps(event)
                    )
                    
                    if resp.get("StatusCode") == 200:
                        payload = json.loads(resp["Payload"].read().decode())
                        
                        if payload.get("status") == "completed":
                            batch_success = payload.get("success_count", 0)
                            batch_failed = payload.get("failed_count", 0)
                            batch_time = payload.get("total_time", "N/A")
                            
                            total_success += batch_success
                            total_failed += batch_failed
                            
                            self.log(f"✅ Lambda completed: {batch_success} success, {batch_failed} failed in {batch_time}s")
                            
                            # Log individual results
                            results = payload.get("results", [])
                            for r in results:
                                email = r.get("email", "unknown")
                                status = r.get("status", "unknown")
                                if status == "success":
                                    self.log(f"   ✅ {email}")
                                else:
                                    error = r.get("error_message", "Unknown error")
                                    self.log(f"   ❌ {email}: {error[:50]}...")
                        else:
                            # Single user format or other status
                            self.log(f"⚠️ Response: {payload.get('status', 'unknown')}")
                            if payload.get("status") == "success":
                                total_success += 1
                            else:
                                total_failed += len(users_list)
                    else:
                        self.log(f"❌ Lambda invocation failed: Status {resp.get('StatusCode')}")
                        total_failed += len(users_list)
                        
                except Exception as e:
                    self.log(f"❌ Error invoking Lambda: {e}")
                    total_failed += len(users_list)
                
                QApplication.processEvents()  # Keep UI responsive
            
            self.log(f"\n{'='*50}")
            self.log(f"🏁 COMPLETED: {total_success} success, {total_failed} failed out of {total_users}")
            self.log(f"   Used {total_invocations} Lambda invocation(s)")
            self.log(f"{'='*50}")
            
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_delete_lambdas(self):
        try:
            session = self.get_session()
            lam = session.client("lambda")
            
            for page in lam.get_paginator("list_functions").paginate():
                for fn in page.get("Functions", []):
                    if "edu-gw" in fn["FunctionName"]:
                        lam.delete_function(FunctionName=fn["FunctionName"])
                        self.log(f"Deleted: {fn['FunctionName']}")
            
            self.log("✅ Lambdas deleted")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_ec2_create(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_input.text().strip()
            
            self._create_ecr_repo(session, ECR_REPO_NAME)
            role_arn = self._ensure_ec2_role(session)
            sg_id = self._ensure_ec2_sg(session)
            self._ensure_ec2_key(session)
            self._launch_ec2_build(session, account_id, region, sg_id)
            
            self.log("✅ EC2 build box launched")
            QMessageBox.information(self, "Success", "EC2 launched. Wait 5-10 min for build.")
        except Exception as e:
            self.log(f"❌ Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_ec2_status(self):
        try:
            session = self.get_session()
            ec2 = session.client("ec2")
            
            resp = ec2.describe_instances(Filters=[
                {"Name": "tag:Name", "Values": [EC2_INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]}
            ])
            
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    self.log(f"EC2: {inst['InstanceId']} - {inst['State']['Name']}")
                    return
            
            self.log("No EC2 build box found")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_ec2_terminate(self):
        try:
            session = self.get_session()
            ec2 = session.client("ec2")
            
            resp = ec2.describe_instances(Filters=[
                {"Name": "tag:Name", "Values": [EC2_INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]}
            ])
            
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    ec2.terminate_instances(InstanceIds=[inst["InstanceId"]])
                    self.log(f"Terminated: {inst['InstanceId']}")
            
            self.log("✅ EC2 terminated")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_prep_create_infrastructure(self):
        try:
            session = self.get_session()
            self._create_ecr_repo(session, PREP_ECR_REPO_NAME)
            self.log("✅ Prep infrastructure created")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_prep_launch_build(self):
        try:
            session = self.get_session()
            self.log("Launching prep build box...")
            # Similar to EC2 build but for prep
            QMessageBox.information(self, "Info", "Use EC2 tab for now, prep build coming soon")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_prep_invoke(self):
        self.log("Prep invoke - use text area users")

    def on_prep_local(self):
        self.log("Local prep - launching prep_local.py")

    # ======================================================================
    # Account Creation Operations
    # ======================================================================

    def generate_accounts(self):
        """Generate account details."""
        base = self.base_domain_input.text().strip()
        count = self.account_count_spin.value()
        if not base:
            QMessageBox.warning(self, "Warning", "Enter base domain")
            return
        
        details = generate_account_details(base, count)
        self.creation_domain_input.setPlainText("\n".join(details))
        self.log(f"Generated {len(details)} accounts")

    def start_creation_process(self):
        """Start account creation process."""
        if not SELENIUM_AVAILABLE:
            QMessageBox.critical(self, "Error", "Selenium not available. Install required packages.")
            return
        
        text = self.creation_domain_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "Enter domain:password pairs")
            return
        
        pairs = [line.strip().split(":", 1) for line in text.split("\n") if ":" in line.strip()]
        if not pairs:
            QMessageBox.warning(self, "Warning", "No valid pairs found")
            return
        
        self.concurrent_count = self.concurrent_spin.value()
        self.worker_queue = deque(pairs)
        self.available_slots = list(range(self.concurrent_count))
        self.active_workers = {}
        
        admin = self.admin_combo.currentText()
        email_prov = self.email_combo.currentText()
        browser = self.browser_combo.currentText()
        headless = self.headless_combo.currentText() == "Yes"
        
        self.log(f"Starting creation for {len(pairs)} accounts...")
        self._start_next_worker(admin, email_prov, browser, headless)

    def _start_next_worker(self, admin, email_prov, browser, headless):
        """Start next worker from queue."""
        while self.available_slots and self.worker_queue:
            domain, password = self.worker_queue.popleft()
            slot = self.available_slots.pop(0)
            
            worker = AccountCreationWorker(
                domain, password, slot, self.concurrent_count,
                admin, email_prov, self.selected_url, browser, headless
            )
            worker.finished_signal.connect(lambda s, m, a=admin, e=email_prov, b=browser, h=headless: self._worker_finished(s, m, a, e, b, h))
            self.active_workers[slot] = worker
            worker.start()

    def _worker_finished(self, slot, message, admin, email_prov, browser, headless):
        """Handle worker completion."""
        self.log(message)
        if slot in self.active_workers:
            del self.active_workers[slot]
        self.available_slots.append(slot)
        QTimer.singleShot(1500, lambda: self._start_next_worker(admin, email_prov, browser, headless))

    # ======================================================================
    # Account Creation AWS Operations
    # ======================================================================

    def on_creation_create_core_resources(self):
        """Create all core resources for account creation Lambda with dedicated resources."""
        try:
            session = self.get_session()
            region = self.region_input.text().strip()
            self.log("Creating dedicated resources for Account Creation...")
            
            # 1. Create dedicated IAM role for Creation Lambda (with S3 full access)
            self.log("1/4: Creating Creation Lambda IAM role...")
            self._create_creation_lambda_role(session)
            
            # 2. Create ECR repository
            self.log("2/4: Creating ECR repository...")
            self._create_ecr_repo(session, CREATION_ECR_REPO_NAME)
            
            # 3. Create dedicated S3 bucket
            self.log("3/4: Creating dedicated S3 bucket...")
            self._create_creation_s3_bucket(session, region)
            
            # 4. Create EC2 role/profile for build box (with S3 full access)
            self.log("4/4: Creating EC2 build role...")
            self._create_creation_ec2_role(session)
            
            self.log("✅ All Creation resources ready!")
            self.creation_status_label.setText("Core Resources Ready")
            QMessageBox.information(self, "Success", 
                f"Creation resources created:\n• Lambda Role: {CREATION_LAMBDA_ROLE_NAME}\n• ECR Repo: {CREATION_ECR_REPO_NAME}\n• S3 Bucket: {CREATION_S3_BUCKET_NAME}\n• EC2 Role: {CREATION_EC2_ROLE_NAME}")
        except Exception as e:
            self.log(f"❌ Error creating core resources: {e}")
            traceback.print_exc()
            QMessageBox.critical(self, "Error", str(e))

    def on_creation_launch_ec2(self):
        """Launch EC2 to build the creation Lambda Docker image using dedicated resources."""
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_input.text().strip()
            
            # Ensure ECR repo exists
            self._create_ecr_repo(session, CREATION_ECR_REPO_NAME)
            
            # Use dedicated Creation resources
            sg_id = self._ensure_creation_ec2_sg(session)
            self._ensure_creation_ec2_key(session)
            
            # Launch EC2 with Dockercreation using dedicated profile
            self._launch_creation_ec2_build(session, account_id, region, sg_id)
            
            self.log("✅ Creation EC2 build box launched")
            QMessageBox.information(self, "Success", f"EC2 launched: {CREATION_EC2_INSTANCE_NAME}\nWait 5-10 min for build.")
        except Exception as e:
            self.log(f"❌ Error: {e}")
            traceback.print_exc()
            QMessageBox.critical(self, "Error", str(e))

    def on_creation_ec2_status(self):
        """Check creation EC2 build box status."""
        try:
            session = self.get_session()
            ec2 = session.client("ec2")
            
            resp = ec2.describe_instances(Filters=[
                {"Name": "tag:Name", "Values": [CREATION_EC2_INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
            ])
            
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    state = inst["State"]["Name"]
                    iid = inst["InstanceId"]
                    self.log(f"Creation EC2: {iid} - {state}")
                    
                    # Check console output for build progress
                    try:
                        output = ec2.get_console_output(InstanceId=iid).get("Output", "")
                        if "ECR_PUSH_DONE" in output or "Successfully" in output:
                            self.log("✅ BUILD COMPLETED!")
                            self.creation_status_label.setText("✅ Build Complete")
                        elif state == "running":
                            self.creation_status_label.setText("⏳ Building...")
                    except:
                        pass
                    return
            
            self.log("No Creation EC2 build box found")
            self.creation_status_label.setText("No EC2 found")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_creation_terminate_ec2(self):
        """Terminate creation EC2 build box."""
        try:
            session = self.get_session()
            ec2 = session.client("ec2")
            
            resp = ec2.describe_instances(Filters=[
                {"Name": "tag:Name", "Values": [CREATION_EC2_INSTANCE_NAME]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]}
            ])
            
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    ec2.terminate_instances(InstanceIds=[inst["InstanceId"]])
                    self.log(f"Terminated Creation EC2: {inst['InstanceId']}")
            
            self.log("✅ Creation EC2 terminated")
            self.creation_status_label.setText("EC2 Terminated")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_creation_create_lambda(self):
        """Create/update the account creation Lambda using dedicated resources."""
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_input.text().strip()
            
            role_arn = self._create_creation_lambda_role(session)
            image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{CREATION_ECR_REPO_NAME}:{ECR_IMAGE_TAG}"
            
            # Get selected region from geo combo for Lambda env var
            geo_text = self.geo_combo.currentText()
            geo_code = geo_text.split(" - ")[1] if " - " in geo_text else "nl"
            
            env_vars = {
                "S3_BUCKET": CREATION_S3_BUCKET_NAME,
                "S3_KEY_PREFIX": "creation_results",
                "REGION_CODE": geo_code
            }
            
            self._create_or_update_lambda(session, CREATION_LAMBDA_NAME, role_arn, 900, image_uri=image_uri, env_vars=env_vars)
            
            self.log(f"✅ Creation Lambda ready: {CREATION_LAMBDA_NAME}")
            self.creation_status_label.setText("Lambda Ready")
            QMessageBox.information(self, "Success", f"Lambda created: {CREATION_LAMBDA_NAME}")
        except Exception as e:
            self.log(f"❌ Error: {e}")
            QMessageBox.critical(self, "Error", str(e))

    def on_creation_invoke_lambda(self):
        """Invoke the creation Lambda for accounts in batches."""
        try:
            session = self.get_session()
            lam = session.client("lambda")
            
            text = self.creation_lambda_input.toPlainText().strip()
            if not text:
                QMessageBox.warning(self, "Warning", "Enter domain:password pairs")
                return
            
            pairs = [line.strip().split(":", 1) for line in text.split("\n") if ":" in line.strip()]
            if not pairs:
                QMessageBox.warning(self, "Warning", "No valid pairs found")
                return
            
            admin = self.admin_combo.currentText()
            email_prov = self.email_combo.currentText()
            geo_text = self.geo_combo.currentText()
            geo_code = geo_text.split(" - ")[1] if " - " in geo_text else "nl"
            
            batch_size = self.creation_batch_size.value()
            total_accounts = len(pairs)
            
            self.log(f"Processing {total_accounts} accounts in batches of {batch_size}...")
            self.creation_status_label.setText(f"Processing {total_accounts} accounts...")
            
            success = 0
            failed = 0
            
            # Process in batches
            for batch_num, i in enumerate(range(0, total_accounts, batch_size), 1):
                batch = pairs[i:i + batch_size]
                batch_end = min(i + batch_size, total_accounts)
                self.log(f"\n=== Batch {batch_num}: Accounts {i+1}-{batch_end} ===")
                self.creation_status_label.setText(f"Batch {batch_num}: {i+1}-{batch_end}")
                
                # Invoke Lambda for each account in batch synchronously
                for domain, password in batch:
                    event = {
                        "domain": domain.strip(),
                        "password": password.strip(),
                        "admin_username": admin,
                        "email_provider": email_prov,
                        "region": geo_code
                    }
                    try:
                        # Use RequestResponse to wait for completion
                        resp = lam.invoke(
                            FunctionName=CREATION_LAMBDA_NAME, 
                            InvocationType="RequestResponse",
                            Payload=json.dumps(event)
                        )
                        if resp.get("StatusCode") == 200:
                            payload = json.loads(resp["Payload"].read().decode())
                            if payload.get("status") == "success":
                                self.log(f"✅ Success: {domain}")
                                success += 1
                            else:
                                self.log(f"⚠️ {domain}: {payload.get('message', 'Unknown')}")
                                failed += 1
                        else:
                            self.log(f"❌ Failed for {domain}: Status {resp.get('StatusCode')}")
                            failed += 1
                    except Exception as invoke_e:
                        self.log(f"❌ Error invoking for {domain}: {invoke_e}")
                        failed += 1
                    
                    QApplication.processEvents()  # Keep UI responsive
                
                self.log(f"✅ Batch {batch_num} complete ({len(batch)} accounts)")
                
                if batch_end < total_accounts:
                    self.log(f"Starting next batch...")
            
            self.log(f"\n✅ Completed: {success} success, {failed} failed out of {total_accounts}")
            self.creation_status_label.setText(f"Done: {success}/{total_accounts}")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def on_creation_fetch_s3_results(self):
        """Fetch account creation results from S3."""
        try:
            session = self.get_session()
            s3 = session.client("s3")
            bucket = CREATION_S3_BUCKET_NAME
            prefix = "creation_results/"
            
            self.log(f"Fetching results from s3://{bucket}/{prefix}")
            results = ""
            
            # Fetch valid accounts
            try:
                obj = s3.get_object(Bucket=bucket, Key=f"{prefix}valid_accounts.txt")
                valid = obj["Body"].read().decode("utf-8")
                results += "=== VALID ACCOUNTS ===\n" + valid + "\n\n"
            except:
                results += "=== VALID ACCOUNTS ===\n(none)\n\n"
            
            # Fetch failed accounts
            try:
                obj = s3.get_object(Bucket=bucket, Key=f"{prefix}failed_accounts.txt")
                failed = obj["Body"].read().decode("utf-8")
                results += "=== FAILED ACCOUNTS ===\n" + failed
            except:
                results += "=== FAILED ACCOUNTS ===\n(none)"
            
            self.s3_results_display.setPlainText(results)
            self.log("✅ S3 results fetched")
        except Exception as e:
            self.log(f"❌ Error fetching S3 results: {e}")

    def on_creation_clear_s3_results(self):
        """Clear account creation results from S3."""
        try:
            session = self.get_session()
            s3 = session.client("s3")
            bucket = CREATION_S3_BUCKET_NAME
            prefix = "creation_results/"
            
            for key in [f"{prefix}valid_accounts.txt", f"{prefix}failed_accounts.txt"]:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    self.log(f"Deleted: s3://{bucket}/{key}")
                except:
                    pass
            
            self.s3_results_display.clear()
            self.log("✅ S3 results cleared")
        except Exception as e:
            self.log(f"❌ Error: {e}")

    def _launch_creation_ec2_build(self, session, account_id, region, sg_id):
        """Launch EC2 to build the creation Lambda Docker image using dedicated resources."""
        ec2 = session.client("ec2")
        ssm = session.client("ssm")
        s3 = session.client("s3")
        
        ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2")["Parameter"]["Value"]
        bucket = CREATION_S3_BUCKET_NAME
        
        # Upload creation files to S3
        repo_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_aws_files")
        
        for fname in ["creation_lambda.py", "Dockercreation", "requirements_creation.txt"]:
            fpath = os.path.join(repo_folder, fname)
            if os.path.exists(fpath):
                s3.upload_file(fpath, bucket, f"creation-build-files/{fname}")
                self.log(f"Uploaded {fname} to S3")
        
        user_data = f"""#!/bin/bash
set -xe
exec > >(tee /var/log/user-data.log) 2>&1
echo "=== Creation EC2 Build Started ==="
yum update -y
amazon-linux-extras install docker -y
systemctl start docker
yum install -y git unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && ./aws/install
mkdir -p /home/ec2-user/build && cd /home/ec2-user/build

# Download creation files from S3
aws s3 cp s3://{bucket}/creation-build-files/creation_lambda.py ./creation_lambda.py
aws s3 cp s3://{bucket}/creation-build-files/Dockercreation ./Dockerfile
aws s3 cp s3://{bucket}/creation-build-files/requirements_creation.txt ./requirements_creation.txt

# Login to ECR
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com

# Build and push
docker build -t {CREATION_ECR_REPO_NAME}:latest .
docker tag {CREATION_ECR_REPO_NAME}:latest {account_id}.dkr.ecr.{region}.amazonaws.com/{CREATION_ECR_REPO_NAME}:latest
docker push {account_id}.dkr.ecr.{region}.amazonaws.com/{CREATION_ECR_REPO_NAME}:latest

echo "ECR_PUSH_DONE"
echo "=== Creation EC2 Build Complete ==="
"""
        
        ec2.run_instances(
            ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1,
            IamInstanceProfile={"Name": CREATION_EC2_INSTANCE_PROFILE_NAME},
            SecurityGroupIds=[sg_id], KeyName=CREATION_EC2_KEY_NAME, UserData=user_data,
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": CREATION_EC2_INSTANCE_NAME}]}]
        )
        self.log(f"Creation EC2 build box launched: {CREATION_EC2_INSTANCE_NAME}")

    # ======================================================================
    # AWS Helper Methods
    # ======================================================================

    def _create_iam_role(self, session, role_name, service, policies):
        iam = session.client("iam")
        doc = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
        
        try:
            iam.get_role(RoleName=role_name)
            self.log(f"IAM role exists: {role_name}")
        except iam.exceptions.NoSuchEntityException:
            iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(doc))
            self.log(f"Created IAM role: {role_name}")
        
        for p in policies:
            try:
                iam.attach_role_policy(RoleName=role_name, PolicyArn=p)
            except:
                pass
        time.sleep(5)

    def _ensure_lambda_role(self, session):
        iam = session.client("iam")
        try:
            resp = iam.get_role(RoleName=LAMBDA_ROLE_NAME)
            return resp["Role"]["Arn"]
        except:
            policies = [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            ]
            self._create_iam_role(session, LAMBDA_ROLE_NAME, "lambda.amazonaws.com", policies)
            return iam.get_role(RoleName=LAMBDA_ROLE_NAME)["Role"]["Arn"]

    def _create_creation_lambda_role(self, session):
        """Create dedicated IAM role for Creation Lambda with S3 full access."""
        iam = session.client("iam")
        policies = [
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        ]
        self._create_iam_role(session, CREATION_LAMBDA_ROLE_NAME, "lambda.amazonaws.com", policies)
        return iam.get_role(RoleName=CREATION_LAMBDA_ROLE_NAME)["Role"]["Arn"]

    def _create_creation_s3_bucket(self, session, region):
        """Create dedicated S3 bucket for Creation tab."""
        s3 = session.client("s3")
        bucket = CREATION_S3_BUCKET_NAME
        
        try:
            s3.head_bucket(Bucket=bucket)
            self.log(f"S3 bucket exists: {bucket}")
        except:
            try:
                if region == "us-east-1":
                    s3.create_bucket(Bucket=bucket)
                else:
                    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
                self.log(f"Created S3 bucket: {bucket}")
                
                # Block public access
                s3.put_public_access_block(
                    Bucket=bucket,
                    PublicAccessBlockConfiguration={
                        'BlockPublicAcls': True,
                        'IgnorePublicAcls': True,
                        'BlockPublicPolicy': True,
                        'RestrictPublicBuckets': True
                    }
                )
            except Exception as e:
                self.log(f"S3 error (may already exist): {e}")

    def _create_creation_ec2_role(self, session):
        """Create dedicated EC2 role with S3 full access for Creation build box."""
        iam = session.client("iam")
        
        # EC2 role with S3 full access
        policies = [
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",  # Full access for uploads
        ]
        self._create_iam_role(session, CREATION_EC2_ROLE_NAME, "ec2.amazonaws.com", policies)
        
        # Create instance profile
        try:
            iam.get_instance_profile(InstanceProfileName=CREATION_EC2_INSTANCE_PROFILE_NAME)
            self.log(f"Instance profile exists: {CREATION_EC2_INSTANCE_PROFILE_NAME}")
        except:
            iam.create_instance_profile(InstanceProfileName=CREATION_EC2_INSTANCE_PROFILE_NAME)
            self.log(f"Created instance profile: {CREATION_EC2_INSTANCE_PROFILE_NAME}")
        
        # Attach role to profile
        try:
            iam.add_role_to_instance_profile(InstanceProfileName=CREATION_EC2_INSTANCE_PROFILE_NAME, RoleName=CREATION_EC2_ROLE_NAME)
            self.log(f"Added role to instance profile")
        except:
            pass  # Already attached
        
        time.sleep(10)  # Wait for propagation

    def _ensure_creation_ec2_sg(self, session):
        """Create dedicated security group for Creation EC2."""
        ec2 = session.client("ec2")
        vpcs = ec2.describe_vpcs()
        vpc_id = vpcs["Vpcs"][0]["VpcId"]
        
        try:
            resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [CREATION_EC2_SG_NAME]}])
            if resp["SecurityGroups"]:
                return resp["SecurityGroups"][0]["GroupId"]
        except:
            pass
        
        resp = ec2.create_security_group(GroupName=CREATION_EC2_SG_NAME, Description="Creation EC2 Build Box SG", VpcId=vpc_id)
        sg_id = resp["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        return sg_id

    def _ensure_creation_ec2_key(self, session):
        """Create dedicated EC2 key pair for Creation tab."""
        ec2 = session.client("ec2")
        try:
            ec2.describe_key_pairs(KeyNames=[CREATION_EC2_KEY_NAME])
        except:
            resp = ec2.create_key_pair(KeyName=CREATION_EC2_KEY_NAME)
            with open(CREATION_EC2_KEY_PATH, "w") as f:
                f.write(resp["KeyMaterial"])

    def _ensure_ec2_role(self, session):
        policies = [
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",
        ]
        self._create_iam_role(session, EC2_ROLE_NAME, "ec2.amazonaws.com", policies)
        
        iam = session.client("iam")
        try:
            iam.get_instance_profile(InstanceProfileName=EC2_INSTANCE_PROFILE_NAME)
        except:
            iam.create_instance_profile(InstanceProfileName=EC2_INSTANCE_PROFILE_NAME)
        
        try:
            iam.add_role_to_instance_profile(InstanceProfileName=EC2_INSTANCE_PROFILE_NAME, RoleName=EC2_ROLE_NAME)
        except:
            pass
        
        time.sleep(10)
        return True

    def _ensure_ec2_sg(self, session):
        ec2 = session.client("ec2")
        vpcs = ec2.describe_vpcs()
        vpc_id = vpcs["Vpcs"][0]["VpcId"]
        
        try:
            resp = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [EC2_SECURITY_GROUP_NAME]}])
            if resp["SecurityGroups"]:
                return resp["SecurityGroups"][0]["GroupId"]
        except:
            pass
        
        resp = ec2.create_security_group(GroupName=EC2_SECURITY_GROUP_NAME, Description="Build box SG", VpcId=vpc_id)
        sg_id = resp["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[{"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
        return sg_id

    def _ensure_ec2_key(self, session):
        ec2 = session.client("ec2")
        try:
            ec2.describe_key_pairs(KeyNames=[EC2_KEY_PAIR_NAME])
        except:
            resp = ec2.create_key_pair(KeyName=EC2_KEY_PAIR_NAME)
            with open(EC2_KEY_PATH, "w") as f:
                f.write(resp["KeyMaterial"])

    def _create_ecr_repo(self, session, repo_name):
        ecr = session.client("ecr")
        try:
            ecr.describe_repositories(repositoryNames=[repo_name])
            self.log(f"ECR repo exists: {repo_name}")
        except:
            ecr.create_repository(repositoryName=repo_name)
            self.log(f"Created ECR repo: {repo_name}")

    def _create_s3_bucket(self, session):
        s3 = session.client("s3")
        region = self.region_input.text().strip()
        bucket = self.s3_bucket_input.text().strip()
        
        try:
            s3.head_bucket(Bucket=bucket)
            self.log(f"S3 bucket exists: {bucket}")
        except:
            try:
                if region == "us-east-1":
                    s3.create_bucket(Bucket=bucket)
                else:
                    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
                self.log(f"Created S3 bucket: {bucket}")
            except Exception as e:
                self.log(f"S3 error: {e}")

    def _create_or_update_lambda(self, session, func_name, role_arn, timeout, image_uri=None, code_str=None, env_vars=None):
        lam = session.client("lambda")
        
        if image_uri:
            code = {"ImageUri": image_uri}
            pkg = "Image"
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("lambda_function.py", code_str or "def lambda_handler(e,c): return {}")
            code = {"ZipFile": buf.getvalue()}
            pkg = "Zip"
        
        try:
            lam.get_function(FunctionName=func_name)
            lam.update_function_code(FunctionName=func_name, **code, Publish=True)
            
            # Wait for update to complete
            import time as t
            t.sleep(5)
            
            # Update configuration if env_vars provided
            if env_vars:
                lam.update_function_configuration(
                    FunctionName=func_name,
                    Environment={"Variables": env_vars}
                )
            
            self.log(f"Updated Lambda: {func_name}")
        except:
            params = {
                "FunctionName": func_name, "Role": role_arn, "Code": code,
                "Timeout": timeout, "MemorySize": 2048, "PackageType": pkg
            }
            if pkg == "Image":
                params["EphemeralStorage"] = {"Size": 2048}
            if env_vars:
                params["Environment"] = {"Variables": env_vars}
            lam.create_function(**params)
            self.log(f"Created Lambda: {func_name}")

    def _launch_ec2_build(self, session, account_id, region, sg_id):
        ec2 = session.client("ec2")
        ssm = session.client("ssm")
        
        ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2")["Parameter"]["Value"]
        
        # Upload files to S3
        s3 = session.client("s3")
        bucket = self.s3_bucket_input.text().strip()
        repo_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_aws_files")
        
        for fname in ["main.py", "Dockerfile"]:
            fpath = os.path.join(repo_folder, fname)
            if os.path.exists(fpath):
                self.log(f"[EC2] Found {fname} at: {fpath}")
                self.log(f"[EC2] Uploading {fname} (Size: {os.path.getsize(fpath)} bytes) to S3...")
                s3.upload_file(fpath, bucket, f"ec2-build-files/{fname}")
        
        user_data = f"""#!/bin/bash
set -xe
yum update -y
amazon-linux-extras install docker -y
systemctl start docker
yum install -y git unzip
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && ./aws/install
cd /home/ec2-user
git clone https://github.com/umihico/docker-selenium-lambda.git
cd docker-selenium-lambda
aws s3 cp s3://{bucket}/ec2-build-files/main.py ./main.py
aws s3 cp s3://{bucket}/ec2-build-files/Dockerfile ./Dockerfile
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com
docker build -t {ECR_REPO_NAME}:{ECR_IMAGE_TAG} .
docker tag {ECR_REPO_NAME}:{ECR_IMAGE_TAG} {account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO_NAME}:{ECR_IMAGE_TAG}
docker push {account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO_NAME}:{ECR_IMAGE_TAG}
"""
        
        ec2.run_instances(
            ImageId=ami, InstanceType="t3.small", MinCount=1, MaxCount=1,
            IamInstanceProfile={"Name": EC2_INSTANCE_PROFILE_NAME},
            SecurityGroupIds=[sg_id], KeyName=EC2_KEY_PAIR_NAME, UserData=user_data,
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": EC2_INSTANCE_NAME}]}]
        )

    # ------------------------------------------------------------------
    # Prep Process Local Execution (Parallel)
    # ------------------------------------------------------------------
    
    def on_prep_local(self):
        """Run prep process locally with parallel browser windows."""
        text = self.prep_users_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Error", "Please enter users (email:password)")
            return
        
        lines = [l.strip() for l in text.split('\n') if ':' in l]
        if not lines:
            QMessageBox.warning(self, "Error", "No valid users found (format: email:password)")
            return
        
        # Check if already running
        if self.local_prep_thread and self.local_prep_thread.is_alive():
            QMessageBox.warning(self, "Running", "A prep process is already running. Stop it first.")
            return
        
        # Import prep_local
        try:
            import prep_local
        except ImportError:
            self.log("ERROR: prep_local.py not found. Make sure it's in the same directory.")
            QMessageBox.critical(self, "Error", "prep_local.py not found")
            return
        
        # Get settings
        max_concurrent = self.prep_concurrent_spin.value()
        screen_width = QApplication.desktop().screenGeometry().width()
        screen_height = QApplication.desktop().screenGeometry().height()
        
        # Get AWS session
        try:
            session = self.get_session()
            s3_bucket = self.s3_bucket_input.text().strip()
        except Exception as e:
            self.log(f"AWS session error: {e}")
            session = None
            s3_bucket = ""
        
        self.log(f"Starting PARALLEL prep for {len(lines)} account(s)")
        self.log(f"Concurrent windows: {max_concurrent}, Screen: {screen_width}x{screen_height}")
        
        # Set up stop event
        self.stop_event = threading.Event()
        self.prep_stop_btn.setEnabled(True)
        
        # Start thread
        def run_parallel_prep():
            try:
                total_accounts = len(lines)
                results = {'success': 0, 'failed': 0, 'stopped': 0}
                
                def process_single_account(args):
                    window_index, line = args
                    
                    if self.stop_event.is_set():
                        return ('stopped', None, None)
                    
                    email, password = line.split(':', 1)
                    email = email.strip()
                    password = password.strip()
                    
                    self.log(f"[Window {window_index + 1}] Starting prep for {email}...")
                    
                    try:
                        result = prep_local.run_prep_process(
                            email, password, session, s3_bucket,
                            stop_event=self.stop_event,
                            window_index=window_index,
                            total_windows=max_concurrent,
                            screen_width=screen_width,
                            screen_height=screen_height
                        )
                        self.log(f"[Window {window_index + 1}] {email}: {result}")
                        return ('success' if result and 'Failed' not in str(result) else 'failed', email, result)
                    except Exception as e:
                        self.log(f"[Window {window_index + 1}] {email}: ERROR - {e}")
                        return ('failed', email, str(e))
                
                # Process in batches
                for batch_start in range(0, total_accounts, max_concurrent):
                    if self.stop_event.is_set():
                        self.log("Process stopped by user.")
                        break
                    
                    batch_end = min(batch_start + max_concurrent, total_accounts)
                    batch_lines = lines[batch_start:batch_end]
                    batch_size = len(batch_lines)
                    
                    self.log(f"\n{'='*50}")
                    self.log(f"Processing batch: accounts {batch_start + 1}-{batch_end} of {total_accounts}")
                    self.log(f"{'='*50}")
                    
                    batch_tasks = [(i, line) for i, line in enumerate(batch_lines)]
                    
                    with ThreadPoolExecutor(max_workers=batch_size) as executor:
                        futures = {executor.submit(process_single_account, task): task for task in batch_tasks}
                        
                        for future in as_completed(futures):
                            status, email, result = future.result()
                            if status == 'success':
                                results['success'] += 1
                            elif status == 'stopped':
                                results['stopped'] += 1
                            else:
                                results['failed'] += 1
                    
                    if batch_end < total_accounts and not self.stop_event.is_set():
                        self.log("Batch complete. Waiting 5 seconds before next batch...")
                        time.sleep(5)
                
                self.log(f"\n{'='*50}")
                self.log(f"PARALLEL PREP COMPLETE")
                self.log(f"Success: {results['success']}, Failed: {results['failed']}, Stopped: {results['stopped']}")
                self.log(f"{'='*50}")
                
            except Exception as e:
                self.log(f"Error in prep thread: {e}")
                traceback.print_exc()
            finally:
                # Re-enable UI on main thread
                QTimer.singleShot(0, lambda: self.prep_stop_btn.setEnabled(False))
        
        self.local_prep_thread = threading.Thread(target=run_parallel_prep, daemon=True)
        self.local_prep_thread.start()
    
    def on_prep_stop(self):
        """Stop all running prep processes."""
        if self.stop_event:
            self.log("Stopping all prep processes...")
            self.stop_event.set()
            self.prep_stop_btn.setEnabled(False)


# ======================================================================
# Entry Point
# ======================================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AwsEducationApp()
    window.show()
    sys.exit(app.exec_())

