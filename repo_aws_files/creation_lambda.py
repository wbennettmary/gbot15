"""
creation_lambda.py - AWS Lambda handler for Google Workspace Education account creation
Uses the same Chrome setup as main.py for Lambda compatibility.
Includes all logic in one file.
"""

import os
import re
import json
import time
import random
import string
import logging
import traceback
import subprocess
from urllib.parse import urlparse, parse_qs

# 3rd-party libraries
import boto3
from faker import Faker
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException, WebDriverException

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 Configuration
S3_BUCKET = os.environ.get('S3_BUCKET', 'edu-gw-creation-bucket')
S3_KEY_PREFIX = os.environ.get('S3_KEY_PREFIX', 'creation_results')
REGION_CODE = os.environ.get('REGION_CODE', 'nl')

# Global sets to reduce repetition
_used_school_names = set()
_used_person_names = set()

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

# Modern User-Agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

WINDOW_SIZES = ["1920,1080", "1366,768", "1440,900", "1536,864"]


# =====================================================================
# Chrome Driver Initialization - COPIED FROM main.py (Lambda-compatible)
# =====================================================================

def cleanup_chrome_processes():
    """Forcefully kill any lingering Chrome or ChromeDriver processes."""
    try:
        subprocess.run(['pkill', '-f', 'chromedriver'], capture_output=True)
        subprocess.run(['pkill', '-f', 'chrome'], capture_output=True)
        subprocess.run(['pkill', '-f', 'chromium'], capture_output=True)
        logger.info("[LAMBDA] Cleaned up Chrome processes")
    except Exception as e:
        logger.warning(f"[LAMBDA] Error cleaning up processes: {e}")


def get_chrome_driver():
    """
    Initialize Selenium Chrome driver for AWS Lambda environment.
    COPIED FROM main.py with full Lambda compatibility.
    """
    # Import selenium-stealth if available
    try:
        from selenium_stealth import stealth
        from fake_useragent import UserAgent
        stealth_available = True
        logger.info("[ANTI-DETECT] selenium-stealth library loaded successfully")
    except ImportError as e:
        stealth_available = False
        logger.warning(f"[ANTI-DETECT] selenium-stealth not available: {e}")
    
    # Force environment variables to prevent SeleniumManager issues
    os.environ['HOME'] = '/tmp'
    os.environ['XDG_CACHE_HOME'] = '/tmp/.cache'
    os.environ['SELENIUM_MANAGER_CACHE'] = '/tmp/.cache/selenium'
    os.environ['SE_SELENIUM_MANAGER'] = 'false'
    os.environ['SELENIUM_MANAGER'] = 'false'
    os.environ['SELENIUM_DISABLE_DRIVER_MANAGER'] = '1'
    
    # Ensure /tmp directories exist
    os.makedirs('/tmp/.cache/selenium', exist_ok=True)
    os.makedirs('/tmp/chrome-data', exist_ok=True)
    
    # Locate Chrome binary and ChromeDriver
    logger.info("[LAMBDA] Checking /opt directory contents...")
    chrome_binary = None
    chromedriver_path = None
    
    if os.path.exists('/opt'):
        logger.info(f"[LAMBDA] Contents of /opt: {os.listdir('/opt')}")
        if os.path.exists('/opt/chrome'):
            logger.info(f"[LAMBDA] Contents of /opt/chrome: {os.listdir('/opt/chrome')}")
    
    # Common paths for Chrome binary
    chrome_paths = [
        '/opt/chrome/chrome',
        '/opt/chrome/headless-chromium',
        '/opt/chrome/chrome-wrapper',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
    ]
    
    for path in chrome_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            chrome_binary = path
            logger.info(f"[LAMBDA] Found Chrome binary at: {chrome_binary}")
            break
    
    if not chrome_binary:
        try:
            result = subprocess.run(['which', 'chrome'], capture_output=True, text=True)
            if result.returncode == 0:
                chrome_binary = result.stdout.strip()
                logger.info(f"[LAMBDA] Found Chrome via which: {chrome_binary}")
        except Exception as e:
            logger.debug(f"[LAMBDA] 'which chrome' failed: {e}")
    
    if not chrome_binary:
        logger.error("[LAMBDA] Chrome binary not found!")
        raise Exception("Chrome binary not found in Lambda environment")
    
    # Common paths for ChromeDriver
    chromedriver_paths = [
        '/opt/chromedriver',
        '/usr/bin/chromedriver',
        '/usr/local/bin/chromedriver',
    ]
    
    for path in chromedriver_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            chromedriver_path = path
            logger.info(f"[LAMBDA] Found ChromeDriver at: {chromedriver_path}")
            break
    
    if not chromedriver_path:
        try:
            result = subprocess.run(['which', 'chromedriver'], capture_output=True, text=True)
            if result.returncode == 0:
                chromedriver_path = result.stdout.strip()
        except:
            pass
    
    if not chromedriver_path:
        logger.error("[LAMBDA] ChromeDriver not found!")
        raise Exception("ChromeDriver not found in Lambda environment")

    # Use Selenium Chrome options with anti-detection
    chrome_options = Options()
    
    # Randomize User-Agent
    if stealth_available:
        try:
            ua = UserAgent()
            user_agent = ua.random
        except:
            user_agent = random.choice(USER_AGENTS)
    else:
        user_agent = random.choice(USER_AGENTS)
    
    chrome_options.add_argument(f"--user-agent={user_agent}")
    logger.info(f"[ANTI-DETECT] Using User-Agent: {user_agent[:50]}...")

    # Randomize Window Size
    window_size = random.choice(WINDOW_SIZES)
    chrome_options.add_argument(f"--window-size={window_size}")
    
    # Core stability options for Lambda
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--lang=en-US")
    
    # Memory Optimization for Lambda (2GB limit)
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-application-cache")
    chrome_options.add_argument("--disk-cache-size=0")
    chrome_options.add_argument("--no-zygote")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-notifications")
    
    # Additional stability options for Lambda
    chrome_options.add_argument("--single-process")  # Critical for Lambda
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--safebrowsing-disable-auto-update")
    chrome_options.add_argument("--disable-software-rasterizer")
    
    # Enhanced Anti-detection options
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--disable-site-isolation-trials")
    chrome_options.add_argument("--ignore-certificate-errors")
    chrome_options.add_argument("--allow-running-insecure-content")
    
    # User data directory
    chrome_options.add_argument("--user-data-dir=/tmp/chrome-data")
    chrome_options.add_argument("--profile-directory=Profile1")
    
    # Remove automation flags
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # Enhanced prefs
    chrome_options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    })

    try:
        # Create Service with explicit ChromeDriver path
        service = Service(executable_path=chromedriver_path)
        
        # Set browser executable path
        chrome_options.binary_location = chrome_binary
        
        logger.info(f"[LAMBDA] Initializing Chrome: ChromeDriver={chromedriver_path}, Chrome={chrome_binary}")
        
        # Create driver
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Set page load timeout (increased for Lambda)
        driver.set_page_load_timeout(120)
        
        # Wait for Chrome to fully initialize
        time.sleep(2)
        
        # Apply selenium-stealth if available
        if stealth_available:
            try:
                stealth(
                    driver,
                    languages=["en-US", "en"],
                    vendor="Google Inc.",
                    platform="Linux x86_64",
                    webgl_vendor="Intel Inc.",
                    renderer="Intel Iris OpenGL Engine",
                    fix_hairline=True,
                )
                logger.info("[ANTI-DETECT] selenium-stealth patch applied successfully")
            except Exception as e:
                logger.warning(f"[ANTI-DETECT] Could not apply selenium-stealth: {e}")
        else:
            # Fallback: Inject basic anti-detection scripts
            try:
                anti_detection_script = '''
                    (function() {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true
                        });
                        try {
                            delete document.$cdc_asdjflasutopfhvcZLmcfl_;
                            delete document.$chrome_asyncScriptInfo;
                        } catch(e) {}
                        if (!window.chrome) {
                            window.chrome = { runtime: {} };
                        }
                    })();
                '''
                driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                    'source': anti_detection_script
                })
                logger.info("[ANTI-DETECT] Basic anti-detection script injected")
            except Exception as e:
                logger.warning(f"[LAMBDA] Could not inject anti-detection script: {e}")
        
        logger.info("[LAMBDA] Chrome driver created successfully")
        return driver
        
    except Exception as e:
        logger.error(f"[LAMBDA] Failed to initialize Chrome driver: {e}")
        logger.error(traceback.format_exc())
        
        # Last resort: try with absolute minimal options
        try:
            logger.info("[LAMBDA] Retrying with minimal options...")
            minimal_options = Options()
            minimal_options.add_argument("--headless=new")
            minimal_options.add_argument("--no-sandbox")
            minimal_options.add_argument("--disable-dev-shm-usage")
            minimal_options.add_argument("--disable-gpu")
            minimal_options.add_argument("--single-process")
            
            if chrome_binary:
                minimal_options.binary_location = chrome_binary
            
            service = Service(executable_path=chromedriver_path)
            driver = webdriver.Chrome(service=service, options=minimal_options)
            
            time.sleep(3)
            logger.info("[LAMBDA] Chrome driver created with minimal options")
            return driver
        except Exception as e2:
            logger.error(f"[LAMBDA] Final retry also failed: {e2}")
            raise Exception(f"Chrome driver initialization failed: {e2}")


# =====================================================================
# Helper Functions
# =====================================================================

def get_faker_for_region(region_code):
    """Get Faker instance for the given region"""
    locale = REGION_FAKER_MAP.get(region_code.lower(), "en_US")
    return Faker(locale)


def add_random_delay(min_delay=1.5, max_delay=3.5):
    """Add human-like random delay"""
    time.sleep(random.uniform(min_delay, max_delay))


def human_like_typing(element, text, min_delay=0.05, max_delay=0.15):
    """Type text character by character with human-like delays"""
    for i, char in enumerate(text):
        element.send_keys(char)
        if i < len(text) - 1:
            if char == ' ':
                time.sleep(random.uniform(0.15, 0.3))
            else:
                time.sleep(random.uniform(min_delay, max_delay))
    return True


def safe_type(element, text, driver, clear_first=True):
    """Safely type text into an input field"""
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
        logger.error(f"Error typing text: {e}")
        return False


def safe_click(element, driver, max_attempts=3):
    """Safely click an element with retry logic"""
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
        except Exception as e:
            logger.warning(f"Click attempt {attempt+1} failed: {e}")
            time.sleep(1)
    return False


def clean_name_for_email(name):
    """Clean name for email by removing accents and special characters"""
    import unicodedata
    name = unicodedata.normalize('NFD', name)
    name = ''.join(char for char in name if unicodedata.category(char) != 'Mn')
    name = name.replace(" ", "").replace("-", "").replace("'", "").replace(".", "")
    return name.lower()


def generate_unique_school_name(faker_instance):
    """Generate unique school/institution name"""
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
    """Generate unique person name"""
    global _used_person_names
    
    for _ in range(100):
        first = faker_instance.first_name()
        last = faker_instance.last_name()
        full = f"{first} {last}".lower()
        if full not in _used_person_names:
            _used_person_names.add(full)
            return first, last
    
    return faker_instance.first_name(), faker_instance.last_name()


def generate_phone_number(country):
    """Generate random phone number for the given country"""
    country_formats = {
        "Netherlands": "+316{:08d}",
        "France": "+336{:08d}",
        "Germany": "+4915{:09d}",
        "Spain": "+346{:08d}",
        "Italy": "+393{:09d}",
        "UK": "+447{:09d}",
        "Poland": "+485{:08d}",
        "Sweden": "+467{:08d}",
        "Denmark": "+454{:07d}",
        "US": "+1{:010d}",
    }
    
    format_str = country_formats.get(country, "+1{:010d}")
    return format_str.format(random.randint(10000000, 99999999))


def find_size_dropdown(driver):
    """Find the company size dropdown"""
    selectors = [".rHGeGc-aPP78e", "[role='button'][aria-haspopup='listbox']", "button[data-value]"]
    for selector in selectors:
        try:
            element = driver.find_element(By.CSS_SELECTOR, selector)
            if element.is_displayed():
                return element
        except:
            continue
    return None


def select_company_size_option(driver):
    """Select 1-100 company size option"""
    size_options = ["1 à 100", "1 to 100", "1-100", "1 bis 100", "1 tot 100"]
    try:
        options = WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'li[role="option"]'))
        )
        for option in options:
            if any(size in option.text.strip() for size in size_options):
                safe_click(option, driver)
                return True
        # Fallback: select first numeric option
        for option in options:
            if any(c.isdigit() for c in option.text):
                safe_click(option, driver)
                return True
    except Exception as e:
        logger.error(f"Error selecting company size: {e}")
    return False


def click_next_button(driver):
    """Click next/continue button"""
    next_texts = ["Next", "Continue", "Suivant", "Weiter", "Volgende", "Siguiente", "Avanti"]
    for text in next_texts:
        try:
            button = driver.find_element(By.XPATH, f'//button[span[contains(text(), "{text}")]]')
            if button.is_enabled() and button.is_displayed():
                safe_click(button, driver)
                return True
        except:
            continue
    
    # Fallback: click last visible button
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
    """Click domain confirmation button"""
    selectors = [
        '//button[contains(text(), "Set up account with this domain")]',
        '//button[contains(text(), "Use this domain")]',
        '//button[contains(text(), "domein")]',
        '//button[contains(text(), "Configurer le compte")]',
    ]
    for selector in selectors:
        try:
            button = driver.find_element(By.XPATH, selector)
            if button.is_displayed() and button.is_enabled():
                safe_click(button, driver)
                return True
        except:
            continue
    return click_next_button(driver)


def is_account_denied(url):
    """Check if URL indicates account was denied"""
    if not url:
        return False
    deny_patterns = ["workspace.google.com/edu/signup/deny", "/edu/signup/deny", "signup/deny"]
    return any(pattern in url.lower() for pattern in deny_patterns)


# =====================================================================
# Main Account Creation Logic
# =====================================================================

def create_account(domain, password, admin_username, email_provider, region_code):
    """Main account creation logic - mirrors Education_creation.py"""
    driver = None
    result = {"status": "error", "message": "", "email": "", "domain": domain}
    
    try:
        # Clean up any lingering processes
        cleanup_chrome_processes()
        
        driver = get_chrome_driver()
        faker = get_faker_for_region(region_code)
        country = REGION_COUNTRY_MAP.get(region_code.lower(), "US")
        
        signup_url = f"https://workspace.google.com/edu/signup?hl=en&region={region_code}"
        logger.info(f"Starting account creation for {domain}")
        driver.get(signup_url)
        
        if is_account_denied(driver.current_url):
            raise Exception("Account creation denied - flagged as spam")
        
        add_random_delay()
        
        # Step 1: Enter School Name
        logger.info("Step 1: Entering school name...")
        school_input = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[2]/div/div/span[2]/input'))
        )
        school_name = generate_unique_school_name(faker)
        safe_type(school_input, school_name, driver)
        logger.info(f"Entered school name: {school_name}")
        add_random_delay()
        
        # Step 2: Select company size
        logger.info("Step 2: Selecting company size...")
        dropdown = find_size_dropdown(driver)
        if dropdown:
            safe_click(dropdown, driver)
            add_random_delay()
            select_company_size_option(driver)
        add_random_delay()
        
        # Step 3: Click checkbox
        logger.info("Step 3: Clicking checkbox...")
        try:
            checkbox = driver.find_element(By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/fieldset/div/label[2]/div[2]')
            safe_click(checkbox, driver)
        except:
            pass
        add_random_delay()
        
        # Step 4: Click Next
        logger.info("Step 4: Clicking Next...")
        click_next_button(driver)
        add_random_delay()
        
        # Step 5: Enter First and Last Name
        logger.info("Step 5: Entering name...")
        first_name_input = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[3]/div[1]/div[1]/span[2]/input'))
        )
        last_name_input = driver.find_element(By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[3]/div[2]/div[1]/span[2]/input')
        first_name, last_name = generate_unique_person_name(faker)
        safe_type(first_name_input, first_name, driver)
        safe_type(last_name_input, last_name, driver)
        logger.info(f"Entered name: {first_name} {last_name}")
        add_random_delay()
        
        # Step 6: Enter Email
        logger.info("Step 6: Entering email...")
        gmail_input = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[4]/div[1]/span[2]/input'))
        )
        generated_email = f"{clean_name_for_email(first_name)}{clean_name_for_email(last_name)}{email_provider}"
        safe_type(gmail_input, generated_email, driver)
        logger.info(f"Entered email: {generated_email}")
        add_random_delay()
        
        # Step 7: Enter Phone Number
        logger.info("Step 7: Entering phone...")
        phone_input = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "phone-input"))
        )
        phone_number = generate_phone_number(country)
        safe_type(phone_input, phone_number, driver)
        phone_input.send_keys(Keys.RETURN)
        logger.info(f"Entered phone: {phone_number}")
        add_random_delay()
        
        # Step 8: Additional step button
        logger.info("Step 8: Clicking additional button...")
        try:
            additional_btn = driver.execute_script('return document.querySelector("#yDmH0d > c-wiz.SSPGKf > div > div > div.LPrTZd > main > div > div > section > div:nth-child(3) > button")')
            if additional_btn:
                safe_click(additional_btn, driver)
        except:
            pass
        add_random_delay()
        
        # Step 9: Enter Domain
        logger.info("Step 9: Entering domain...")
        domain_input = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/div/section/div[2]/div[2]/div/div[1]/span[2]/input'))
        )
        safe_type(domain_input, domain, driver)
        domain_input.send_keys(Keys.RETURN)
        logger.info(f"Entered domain: {domain}")
        add_random_delay(2.5, 5.0)
        
        # Step 10: Click domain confirmation
        logger.info("Step 10: Confirming domain...")
        click_domain_confirmation_button(driver)
        add_random_delay(3.0, 6.0)
        
        # Step 11: Accept button
        logger.info("Step 11: Clicking accept...")
        try:
            accept_btn = driver.execute_script('return document.querySelector("#yDmH0d > c-wiz.SSPGKf > div > div > div.LPrTZd > main > div > div > div > nav > span > div > button")')
            if accept_btn:
                safe_click(accept_btn, driver)
        except:
            pass
        add_random_delay(2.5, 5.0)
        
        # Step 12: Enter Admin credentials
        logger.info("Step 12: Entering admin credentials...")
        admin_input = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[1]/div/div/div[1]/span[2]/input'))
        )
        safe_type(admin_input, admin_username, driver)
        
        # Password fields
        password_inputs = driver.find_elements(By.XPATH, '//input[@type="password"]')
        if len(password_inputs) >= 2:
            safe_type(password_inputs[0], password, driver)
            safe_type(password_inputs[1], password, driver)
        else:
            password_input = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[2]/div/div[1]/span[2]/input'))
            )
            safe_type(password_input, password, driver)
        
        logger.info("Entered admin credentials")
        time.sleep(3)
        
        # Agree and continue
        try:
            agree_btn = driver.find_element(By.XPATH, '//button[span[contains(text(), "Agree and continue") or contains(text(), "Akkoord")]]')
            safe_click(agree_btn, driver)
            time.sleep(2)
        except:
            pass
        
        # Final checkbox
        try:
            final_checkbox = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[3]/div/div/span[1]'))
            )
            safe_click(final_checkbox, driver)
        except:
            pass
        
        # Final button
        try:
            final_btn = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, '/html/body/c-wiz[1]/div/div/div[2]/main/div/span[1]/div/section/div[5]/div/button/span[6]'))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", final_btn)
            try:
                final_btn.click()
            except:
                driver.execute_script("arguments[0].click();", final_btn)
        except Exception as e:
            logger.error(f"Could not click final button: {e}")
        
        add_random_delay(2, 4)
        
        # Check result
        if is_account_denied(driver.current_url):
            result["status"] = "denied"
            result["message"] = "Account flagged as spam"
        else:
            created_email = f"{admin_username}@{domain}"
            result["status"] = "success"
            result["email"] = created_email
            result["message"] = f"Account created: {created_email}"
            logger.info(f"SUCCESS: {created_email}")
        
    except Exception as e:
        result["status"] = "error"
        result["message"] = str(e)
        logger.error(f"Account creation failed: {e}")
        traceback.print_exc()
    
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        cleanup_chrome_processes()
    
    return result


# =====================================================================
# S3 Result Saving
# =====================================================================

def save_result_to_s3(result, s3_client=None):
    """Save account creation result to S3"""
    if not s3_client:
        s3_client = boto3.client('s3')
    
    if result.get("status") == "success":
        key = f"{S3_KEY_PREFIX}/valid_accounts.txt"
        line = f"{result['email']}:{result.get('password', '')}\n"
    else:
        key = f"{S3_KEY_PREFIX}/failed_accounts.txt"
        line = f"{result['domain']}:{result.get('message', 'Unknown error')}\n"
    
    try:
        # Append to existing file
        try:
            existing = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            content = existing['Body'].read().decode('utf-8') + line
        except:
            content = line
        
        s3_client.put_object(Bucket=S3_BUCKET, Key=key, Body=content.encode('utf-8'))
        logger.info(f"Saved result to s3://{S3_BUCKET}/{key}")
    except Exception as e:
        logger.error(f"Failed to save to S3: {e}")


# =====================================================================
# Lambda Entry Point
# =====================================================================

def handler(event, context):
    """Lambda entry point"""
    domain = event.get('domain')
    password = event.get('password')
    admin_username = event.get('admin_username', 'admin')
    email_provider = event.get('email_provider', '@gmail.com')
    region_code = event.get('region', REGION_CODE)
    
    logger.info(f"Lambda invoked for domain: {domain}, region: {region_code}")
    
    if not domain or not password:
        return {"status": "error", "message": "Missing domain or password"}
    
    result = create_account(domain, password, admin_username, email_provider, region_code)
    result["password"] = password
    
    # Save to S3
    save_result_to_s3(result)
    
    return result
