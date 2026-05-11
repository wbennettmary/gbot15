"""
AWS Lambda Handler: Google Workspace Automation
- Logs into Google account
- Sets up 2-Step Verification with Authenticator
- Extracts TOTP secret and saves to SFTP
- Creates App Password
- Saves App Password to DynamoDB (reliable, atomic storage)

Usage:
Event must contain: {"email": "...", "password": "..."}
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
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 3rd-party libraries
import boto3
from botocore.exceptions import ClientError
import paramiko
import pyotp

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Global Constants
DEFAULT_TIMEOUT = 10

# =====================================================================
# Global boto3 clients/resources (reused across invocations for better performance)
# =====================================================================

# Lazy initialization of boto3 clients/resources
_dynamodb_resource = None
_s3_client = None

# Cache for Chrome paths to avoid repeated scanning in Lambda execution context
_cached_chrome_binary = None
_cached_chromedriver_path = None

def get_dynamodb_resource():
    """Get or create DynamoDB resource (reused across invocations)
    Uses a fixed region (eu-west-1) so all Lambda functions save to the same table
    This saves resources by having 1 table instead of 1 per region
    """
    global _dynamodb_resource
    if _dynamodb_resource is None:
        # Use fixed region for DynamoDB - all Lambda functions save to same table
        # This saves resources (1 table instead of 17 tables)
        dynamodb_region = os.environ.get("DYNAMODB_REGION", "eu-west-1")
        _dynamodb_resource = boto3.resource("dynamodb", region_name=dynamodb_region)
        logger.info(f"[DYNAMODB] Using DynamoDB region: {dynamodb_region} (centralized storage)")
    return _dynamodb_resource

def get_s3_client():
    """Get or create S3 client (reused across invocations)"""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client

def ensure_s3_bucket_exists(bucket_name, region='us-east-1'):
    """Create S3 bucket if it doesn't exist"""
    try:
        s3_client = get_s3_client()
        s3_client.head_bucket(Bucket=bucket_name)
        logger.debug(f"[S3] Bucket {bucket_name} already exists")
        return True
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        if error_code == '404' or error_code == 'NoSuchBucket':
            try:
                # Try to create bucket
                if region == 'us-east-1':
                    s3_client.create_bucket(Bucket=bucket_name)
                else:
                    s3_client.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={'LocationConstraint': region}
                    )
                logger.info(f"[S3] Created bucket {bucket_name} in region {region}")
                return True
            except Exception as create_err:
                logger.warning(f"[S3] Could not create bucket {bucket_name}: {create_err}")
                return False
        else:
            logger.warning(f"[S3] Could not access bucket {bucket_name}: {e}")
            return False
    except Exception as e:
        logger.warning(f"[S3] Error checking bucket {bucket_name}: {e}")
        return False

# =====================================================================
# Chrome Driver Initialization for AWS Lambda (with anti-detection)
# =====================================================================

# Global proxy list and rotation counter (for batch processing)
import threading
_proxy_list_cache = None
_proxy_rotation_counter = 0
_proxy_lock = threading.Lock()

def get_proxy_list_from_env():
    """Get and parse proxy list from environment variable"""
    global _proxy_list_cache
    
    if _proxy_list_cache is not None:
        return _proxy_list_cache
    
    proxy_enabled = os.environ.get('PROXY_ENABLED', 'false').lower() == 'true'
    if not proxy_enabled:
        _proxy_list_cache = []
        return []
    
    proxy_list_str = os.environ.get('PROXY_LIST', '').strip()
    if not proxy_list_str:
        _proxy_list_cache = []
        return []
    
    proxies = []
    for line in proxy_list_str.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        parts = line.split(':')
        if len(parts) == 4:
            ip, port, username, password = parts
            proxies.append({
                'ip': ip,
                'port': port,
                'username': username,
                'password': password,
                'full': line
            })
    
    _proxy_list_cache = proxies
    logger.info(f"[PROXY] Loaded {len(proxies)} proxy/proxies from environment")
    return proxies

def get_rotated_proxy_for_user():
    """Get next proxy from list using round-robin rotation (thread-safe)"""
    proxies = get_proxy_list_from_env()
    if not proxies:
        return None
    
    global _proxy_rotation_counter
    with _proxy_lock:
        proxy = proxies[_proxy_rotation_counter % len(proxies)]
        _proxy_rotation_counter += 1
        return proxy

def get_proxy_from_env():
    """
    Get proxy configuration for current user (with rotation).
    Format: IP:PORT:USERNAME:PASSWORD
    Returns: dict with proxy config or None if not set
    """
    proxy = get_rotated_proxy_for_user()
    if not proxy:
        return None
    
    try:
        return {
            'ip': proxy['ip'],
            'port': proxy['port'],
            'username': proxy['username'],
            'password': proxy['password'],
            'http': f'http://{proxy["username"]}:{proxy["password"]}@{proxy["ip"]}:{proxy["port"]}',
            'https': f'http://{proxy["username"]}:{proxy["password"]}@{proxy["ip"]}:{proxy["port"]}'  # Use http for SOCKS proxies
        }
    except Exception as e:
        logger.warning(f"[PROXY] Error formatting proxy config: {e}")
        return None

# =====================================================================
# Anti-Detection Constants
# =====================================================================

# Modern User-Agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
]

# Common Window Sizes for rotation
WINDOW_SIZES = [
    "1920,1080",
    "1366,768",
    "1440,900",
    "1536,864",
    "1280,800",
    "1280,720"
]

def cleanup_chrome_processes():
    """
    Forcefully kill any lingering Chrome or ChromeDriver processes.
    Crucial for Lambda environment to prevent memory leaks and zombie processes.
    
    NOTE: Disabled pkill calls as they typically fail in restricted Lambda environments
    and can cause unnecessary log noise.
    """
    pass
    # Original pkill logic removed for Lambda optimization
    # try:
    #     subprocess.run(['pkill', '-f', 'chromedriver'], capture_output=True)
    #     subprocess.run(['pkill', '-f', 'chrome'], capture_output=True)
    #     subprocess.run(['pkill', '-f', 'chromium'], capture_output=True)
    # except Exception as e:
    #     logger.warning(f"[LAMBDA] Error cleaning up processes: {e}")

def get_chrome_driver():
    """
    Initialize Selenium Chrome driver for AWS Lambda environment.
    Uses standard Selenium with CDP-based anti-detection (Lambda-compatible).
    Supports proxy configuration if PROXY_ENABLED environment variable is set.
    Integrates selenium-stealth for enhanced anti-detection.
    """
    # Import selenium-stealth for anti-detection
    try:
        from selenium_stealth import stealth
        stealth_available = True
        logger.info("[ANTI-DETECT] selenium-stealth library loaded successfully")
    except ImportError as e:
        stealth_available = False
        logger.warning(f"[ANTI-DETECT] selenium-stealth not available: {e}")
    
    # Force environment variables to prevent SeleniumManager
    os.environ['HOME'] = '/tmp'
    os.environ['XDG_CACHE_HOME'] = '/tmp/.cache'
    os.environ['SE_SELENIUM_MANAGER'] = 'false'
    os.environ['SELENIUM_MANAGER'] = 'false'
    os.environ['SELENIUM_DISABLE_DRIVER_MANAGER'] = '1'
    
    # Ensure /tmp cache directory exists
    os.makedirs('/tmp/.cache', exist_ok=True)
    
    # Locate Chrome binary and ChromeDriver
    global _cached_chrome_binary, _cached_chromedriver_path
    
    # Use cached paths if available
    if _cached_chrome_binary and _cached_chromedriver_path:
        chrome_binary = _cached_chrome_binary
        chromedriver_path = _cached_chromedriver_path
        logger.info(f"[LAMBDA] Using cached paths - Chrome: {chrome_binary}, Driver: {chromedriver_path}")
    else:
        logger.info("[LAMBDA] Checking /opt directory contents...")
        chrome_binary = None
        chromedriver_path = None
    
    # Log /opt contents for debugging
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
    
    # If not found by direct paths, try using 'which'
    if not chrome_binary:
        try:
            result = subprocess.run(['which', 'chrome'], capture_output=True, text=True)
            if result.returncode == 0:
                chrome_binary = result.stdout.strip()
                logger.info(f"[LAMBDA] Found Chrome via which: {chrome_binary}")
        except Exception as e:
            logger.debug(f"[LAMBDA] 'which chrome' failed: {e}")
    
    if not chrome_binary:
        logger.error("[LAMBDA] Chrome binary not found! Cannot proceed without Chrome binary path.")
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
                logger.info(f"[LAMBDA] Found ChromeDriver via which: {chromedriver_path}")
        except Exception as e:
            logger.debug(f"[LAMBDA] 'which chromedriver' failed: {e}")
    
    if not chromedriver_path:
        logger.error("[LAMBDA] ChromeDriver not found! This should not happen with umihico base image.")
        raise Exception("ChromeDriver not found in Lambda environment")
        
    # Cache the found paths
    _cached_chrome_binary = chrome_binary
    _cached_chromedriver_path = chromedriver_path

    # Get proxy configuration if enabled
    proxy_config = get_proxy_from_env()
    seleniumwire_options = None
    
    if proxy_config:
        logger.info(f"[PROXY] Using proxy: {proxy_config['ip']}:{proxy_config['port']}")
        seleniumwire_options = {
            'proxy': {
                'http': proxy_config['http'],
                'https': proxy_config['http'],
                'no_proxy': 'localhost,127.0.0.1'
            }
        }
    else:
        logger.info("[PROXY] Proxy disabled or not configured")

    # =========================================================================
    # MINIMAL OPTIONS ONLY - These are proven to work reliably in Lambda
    # DO NOT ADD MORE OPTIONS - extra options cause Chrome to crash!
    # =========================================================================
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--single-process")  # Critical for Lambda
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--lang=en-US")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")  # Hide automation

    try:
        # Create Service with explicit ChromeDriver path
        service = Service(executable_path=chromedriver_path)
        
        # Set browser executable path
        chrome_options.binary_location = chrome_binary
        
        logger.info(f"[LAMBDA] Initializing Chrome with minimal options...")
        logger.info(f"[LAMBDA] ChromeDriver: {chromedriver_path}, Chrome: {chrome_binary}")
        
        # Create driver - use selenium-wire if proxy is configured
        if seleniumwire_options:
            try:
                from seleniumwire import webdriver as wire_webdriver
                logger.info("[PROXY] Using selenium-wire for proxy authentication")
                driver = wire_webdriver.Chrome(
                    service=service,
                    options=chrome_options,
                    seleniumwire_options=seleniumwire_options
                )
                logger.info("[PROXY] ✓ selenium-wire driver created")
            except ImportError:
                logger.warning("[PROXY] selenium-wire not available, using regular selenium")
                driver = webdriver.Chrome(service=service, options=chrome_options)
        else:
            driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Set reasonable page load timeout
        driver.set_page_load_timeout(120)
        
        # Brief pause for Chrome to stabilize
        time.sleep(1)
        
        # Apply selenium-stealth for anti-detection (works after browser starts)
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
                logger.info("[ANTI-DETECT] ✓ selenium-stealth applied")
            except Exception as e:
                logger.warning(f"[ANTI-DETECT] stealth failed (non-critical): {e}")
        
        logger.info("[LAMBDA] ✓ Chrome driver created successfully")
        return driver
        
    except Exception as e:
        logger.error(f"[LAMBDA] Failed to create Chrome driver: {e}")
        logger.error(traceback.format_exc())
        raise


# =====================================================================
# Anti-Detection Helper Functions
# =====================================================================

def random_scroll_and_mouse_move(driver):
    """Perform random scroll and mouse movements to simulate human behavior"""
    try:
        # Random scroll
        scroll_amount = random.randint(100, 500)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(0.3, 0.8))
        
        # Random mouse move simulation via JavaScript
        driver.execute_script("""
            const event = new MouseEvent('mousemove', {
                view: window,
                bubbles: true,
                cancelable: true,
                clientX: Math.random() * window.innerWidth,
                clientY: Math.random() * window.innerHeight
            });
            document.dispatchEvent(event);
        """)
        time.sleep(random.uniform(0.2, 0.5))
    except Exception as e:
        logger.debug(f"[ANTI-DETECT] Random scroll/mouse move failed: {e}")

def adaptive_wait(driver, condition, timeout=8):
    """Optimized adaptive wait with shorter timeout"""
    try:
        return WebDriverWait(driver, timeout).until(condition)
    except TimeoutException:
        return None

def inject_randomized_javascript(driver):
    """Inject randomized JavaScript to make detection harder"""
    try:
        script = """
        // Modify navigator properties to make detection harder
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
        logger.debug("[ANTI-DETECT] Randomized JavaScript injected")
    except Exception as e:
        logger.debug(f"[ANTI-DETECT] Failed to inject randomized JS: {e}")

def simulate_human_typing(element, text, driver):
    """
    Enhanced human-like typing simulation with:
    - Variable typing speed (faster at start, slower for complex chars)
    - Occasional longer pauses (thinking)
    - Rare typing mistakes with backspace correction
    - Natural rhythm variation
    """
    try:
        # Don't clear here - should be cleared before calling
        time.sleep(random.uniform(0.2, 0.5))  # Initial pause before typing
        
        typed_chars = 0
        for i, char in enumerate(text):
            # Variable typing speed based on character type
            if char.isalnum():
                # Alphanumeric: faster (80-150ms)
                delay = random.uniform(0.08, 0.15)
            elif char in ['@', '.', '-', '_']:
                # Special chars: slower (150-250ms) - humans pause for these
                delay = random.uniform(0.15, 0.25)
            else:
                # Other chars: medium speed
                delay = random.uniform(0.1, 0.2)
            
            # Occasional longer pause (simulating thinking/reading) - 5% chance
            if random.random() < 0.05:
                delay += random.uniform(0.3, 0.8)
                logger.debug(f"[ANTI-DETECT] Human thinking pause at char {i+1}")
            
            element.send_keys(char)
            time.sleep(delay)
            typed_chars += 1
            
            # Rare typing mistake simulation (1% chance) - type wrong char, backspace, type correct
            if random.random() < 0.01 and i > 2 and i < len(text) - 2:
                wrong_char = random.choice('abcdefghijklmnopqrstuvwxyz')
                element.send_keys(wrong_char)
                time.sleep(random.uniform(0.1, 0.2))
                element.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.1, 0.2))
                element.send_keys(char)  # Type correct char
                time.sleep(delay)
                logger.debug(f"[ANTI-DETECT] Simulated typing mistake correction at char {i+1}")
        
        logger.debug(f"[ANTI-DETECT] Enhanced human typing completed for {len(text)} characters")
    except Exception as e:
        logger.warning(f"[ANTI-DETECT] Enhanced human typing failed, using normal send_keys: {e}")
        element.send_keys(text)

def add_random_delays():
    """Add random delays to simulate human behavior"""
    time.sleep(random.uniform(0.5, 1.5))

# =====================================================================
# Selenium Helper Functions
# =====================================================================

def wait_for_xpath(driver, xpath, timeout=30):
    """Wait for an element by XPath and return it."""
    try:
        element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        return element
    except TimeoutException:
        logger.error(f"[SELENIUM] Timeout waiting for XPath: {xpath}")
        return None

def wait_for_visible_and_interactable(driver, xpath, timeout=30):
    """Wait for an element to be visible and interactable, then return it."""
    try:
        # Use element_to_be_clickable which ensures element is both visible and interactable
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        # Scroll into view
        driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
        time.sleep(0.2)  # Reduced wait time
        # Focus the element to ensure it's ready for interaction
        try:
            element.click()  # Click to focus (will be cleared anyway)
            time.sleep(0.1)
        except:
            pass  # If click fails, try JavaScript focus
        return element
    except TimeoutException:
        logger.error(f"[SELENIUM] Timeout waiting for visible/interactable XPath: {xpath}")
        return None
    except Exception as e:
        logger.error(f"[SELENIUM] Error waiting for element: {e}")
        return None

def wait_for_password_clickable(driver, by_method, selector, timeout=10):
    """Wait for password field to be clickable using By.NAME or By.XPATH (like reference function)"""
    try:
        # Wait for element to be present
        element = WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by_method, selector))
        )
        
        # Skip hiddenPassword fields (Google's hidden security field)
        element_name = element.get_attribute('name') or ''
        if 'hiddenPassword' in element_name.lower() or 'hidden' in element_name.lower():
            logger.debug(f"[SELENIUM] Skipping hidden password field: {element_name}")
            return None
        
        # Ensure element is actually visible and interactable
        if not element.is_displayed():
            logger.debug(f"[SELENIUM] Password field found but not displayed: {element_name}")
            return None
        
        # Wait for it to be clickable
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((by_method, selector))
        )
        
        # Double-check it's not hiddenPassword
        element_name = element.get_attribute('name') or ''
        if 'hiddenPassword' in element_name.lower():
            return None
        
        # Focus the element
        element.click()  # Click to focus
        time.sleep(0.1)
        return element
    except TimeoutException:
        return None
    except Exception as e:
        logger.debug(f"[SELENIUM] Error waiting for password field: {e}")
        return None

def get_twocaptcha_config():
    """Get 2Captcha configuration from environment variables"""
    api_key = os.environ.get('TWOCAPTCHA_API_KEY', '').strip()
    enabled_str = os.environ.get('TWOCAPTCHA_ENABLED', 'false').strip().lower()
    enabled = enabled_str == 'true'
    
    # Debug logging to help diagnose issues
    logger.info(f"[2CAPTCHA CONFIG] Reading environment variables:")
    logger.info(f"[2CAPTCHA CONFIG]   TWOCAPTCHA_ENABLED = '{os.environ.get('TWOCAPTCHA_ENABLED', 'NOT_SET')}' (parsed as: {enabled})")
    logger.info(f"[2CAPTCHA CONFIG]   TWOCAPTCHA_API_KEY = '{api_key[:10]}...' (length: {len(api_key)})" if api_key else "[2CAPTCHA CONFIG]   TWOCAPTCHA_API_KEY = NOT_SET")
    
    if enabled and api_key:
        logger.info("[2CAPTCHA CONFIG] ✓ 2Captcha is ENABLED and API key is configured")
        return {'enabled': True, 'api_key': api_key}
    else:
        if not enabled:
            logger.warning("[2CAPTCHA CONFIG] ✗ 2Captcha is DISABLED (TWOCAPTCHA_ENABLED is not 'true')")
        if not api_key:
            logger.warning("[2CAPTCHA CONFIG] ✗ 2Captcha API key is NOT SET (TWOCAPTCHA_API_KEY is empty)")
        return {'enabled': False, 'api_key': None}

def solve_google_image_captcha(driver, api_key, email=None):
    """
    Solve Google's image-based CAPTCHA (not reCAPTCHA) using 2Captcha ImageToTextTask.
    
    This handles the CAPTCHA that shows "Type the text you hear or see" with a distorted text image.
    
    Args:
        driver: Selenium WebDriver instance
        api_key: 2Captcha API key
        email: User email (optional, for logging)
    
    Returns:
        (success: bool, solution: str|None, error: str|None)
    """
    try:
        logger.info("[2CAPTCHA] Detected Google image CAPTCHA - using ImageToTextTask...")
        
        # Find the CAPTCHA input field (name="ca" or id="ca")
        captcha_input = None
        try:
            captcha_input = driver.find_element(By.XPATH, "//input[@name='ca' or @id='ca']")
            if not captcha_input.is_displayed():
                logger.error("[2CAPTCHA] CAPTCHA input field found but not displayed")
                return False, None, "CAPTCHA input field not visible"
        except Exception as e:
            logger.error(f"[2CAPTCHA] Could not find CAPTCHA input field: {e}")
            return False, None, "CAPTCHA input field not found"
        
        # Find the CAPTCHA image element
        # The image is usually near the input field, could be in various structures
        captcha_image = None
        image_element = None
        
        try:
            # Try multiple strategies to find the CAPTCHA image
            # Strategy 1: Look for img tag near the input field
            parent = captcha_input.find_element(By.XPATH, "./ancestor::*[contains(@class, 'captcha') or contains(@id, 'captcha')][1]")
            if parent:
                images = parent.find_elements(By.TAG_NAME, "img")
                for img in images:
                    if img.is_displayed():
                        img_src = img.get_attribute('src') or ''
                        # Google CAPTCHA images are usually data URIs or from google.com
                        if 'data:image' in img_src or 'google' in img_src.lower() or 'captcha' in img_src.lower():
                            image_element = img
                            logger.info("[2CAPTCHA] Found CAPTCHA image via parent container")
                            break
        except:
            pass
        
        # Strategy 2: Look for img tags near the CAPTCHA input field
        if not image_element:
            try:
                # Find images near the input field (CAPTCHA image is usually above or next to the input)
                parent_container = captcha_input.find_element(By.XPATH, "./ancestor::form | ./ancestor::div[contains(@class, 'form')] | ./ancestor::div[contains(@role, 'form')] | ./ancestor::*[position()<=5]")
                if parent_container:
                    images = parent_container.find_elements(By.TAG_NAME, "img")
                    for img in images:
                        if img.is_displayed():
                            img_src = img.get_attribute('src') or ''
                            img_alt = img.get_attribute('alt') or ''
                            # Check if it looks like a CAPTCHA image
                            if ('captcha' in img_src.lower() or 'captcha' in img_alt.lower() or 
                                'data:image' in img_src or img_src.startswith('http')):
                                # Verify it's reasonably sized (CAPTCHA images are usually 200-400px wide)
                                size = img.size
                                if size['width'] > 150 and size['height'] > 30:
                                    image_element = img
                                    logger.info("[2CAPTCHA] Found CAPTCHA image near input field")
                                    break
            except:
                pass
        
        # Strategy 2b: Look for img tags with specific attributes (broader search)
        if not image_element:
            try:
                images = driver.find_elements(By.XPATH, "//img[contains(@src, 'captcha') or contains(@alt, 'captcha') or contains(@class, 'captcha')]")
                for img in images:
                    if img.is_displayed():
                        size = img.size
                        if size['width'] > 150 and size['height'] > 30:
                            image_element = img
                            logger.info("[2CAPTCHA] Found CAPTCHA image via img tag search")
                            break
            except:
                pass
        
        # Strategy 3: Look for canvas element (Google sometimes uses canvas for CAPTCHA)
        if not image_element:
            try:
                canvases = driver.find_elements(By.TAG_NAME, "canvas")
                for canvas in canvases:
                    if canvas.is_displayed():
                        size = canvas.size
                        # CAPTCHA canvas is usually reasonably sized (not tiny)
                        if size['width'] > 100 and size['height'] > 50:
                            image_element = canvas
                            logger.info("[2CAPTCHA] Found CAPTCHA canvas element")
                            break
            except:
                pass
        
        # Strategy 4: Screenshot the area around the input field (fallback)
        if not image_element:
            logger.warning("[2CAPTCHA] Could not find CAPTCHA image element, will screenshot area around input field")
            # We'll use the input field's location to screenshot the area
        
        # Extract the image
        image_base64 = None
        
        if image_element:
            try:
                # Try to get image from src attribute (if it's a data URI or URL)
                img_src = image_element.get_attribute('src') or ''
                if img_src.startswith('data:image'):
                    # Extract base64 from data URI
                    base64_data = img_src.split(',')[1] if ',' in img_src else img_src
                    image_base64 = base64_data
                    logger.info("[2CAPTCHA] Extracted CAPTCHA image from data URI")
                elif img_src.startswith('http'):
                    # Download the image
                    with urllib.request.urlopen(img_src) as response:
                        image_data = response.read()
                        image_base64 = base64.b64encode(image_data).decode('utf-8')
                    logger.info("[2CAPTCHA] Downloaded and encoded CAPTCHA image from URL")
                else:
                    # Screenshot the element
                    screenshot_path = f"/tmp/captcha_image_{int(time.time())}.png"
                    image_element.screenshot(screenshot_path)
                    with open(screenshot_path, 'rb') as f:
                        image_base64 = base64.b64encode(f.read()).decode('utf-8')
                    os.remove(screenshot_path)
                    logger.info("[2CAPTCHA] Screenshot CAPTCHA image element")
            except Exception as e:
                logger.warning(f"[2CAPTCHA] Error extracting image from element: {e}")
                image_element = None
        
        # Fallback: Screenshot area around input field
        if not image_base64 and captcha_input:
            try:
                logger.info("[2CAPTCHA] Using fallback: screenshot full page and extract CAPTCHA area")
                # Screenshot the full page
                full_screenshot_path = f"/tmp/captcha_full_{int(time.time())}.png"
                driver.save_screenshot(full_screenshot_path)
                
                # Try to use PIL for cropping, fallback to full screenshot if not available
                try:
                    from PIL import Image
                    full_img = Image.open(full_screenshot_path)
                    
                    # Get input field location
                    location = captcha_input.location
                    
                    # Calculate crop area (CAPTCHA image is usually above the input field)
                    x = max(0, location['x'] - 200)
                    y = max(0, location['y'] - 150)
                    width = min(full_img.width - x, 400)
                    height = min(full_img.height - y, 200)
                    
                    cropped_img = full_img.crop((x, y, x + width, y + height))
                    cropped_path = f"/tmp/captcha_cropped_{int(time.time())}.png"
                    cropped_img.save(cropped_path)
                    
                    # Convert to base64
                    with open(cropped_path, 'rb') as f:
                        image_base64 = base64.b64encode(f.read()).decode('utf-8')
                    
                    # Clean up
                    os.remove(cropped_path)
                    logger.info("[2CAPTCHA] Cropped and encoded CAPTCHA area from screenshot")
                except ImportError:
                    # PIL not available, use full screenshot
                    logger.warning("[2CAPTCHA] PIL not available, using full page screenshot")
                    with open(full_screenshot_path, 'rb') as f:
                        image_base64 = base64.b64encode(f.read()).decode('utf-8')
                    logger.info("[2CAPTCHA] Encoded full page screenshot (PIL not available for cropping)")
                
                # Clean up
                os.remove(full_screenshot_path)
            except Exception as e:
                logger.error(f"[2CAPTCHA] Error in fallback screenshot method: {e}")
                logger.error(traceback.format_exc())
                return False, None, f"Failed to extract CAPTCHA image: {e}"
        
        if not image_base64:
            return False, None, "Could not extract CAPTCHA image"
        
        # Send to 2Captcha Image CAPTCHA API (traditional API as per documentation)
        # Documentation: https://2captcha.com/p/image-picture-captcha-solver
        logger.info("[2CAPTCHA] Sending CAPTCHA image to 2Captcha using traditional Image CAPTCHA API...")
        submit_url = 'https://2captcha.com/in.php'
        
        # Prepare POST data with method=base64 as per documentation
        post_data = {
            'key': api_key,
            'method': 'base64',
            'body': image_base64,
            'json': 1  # Request JSON response
        }
        
        # Encode as form data
        post_data_encoded = urllib.parse.urlencode(post_data).encode('utf-8')
        logger.info(f"[2CAPTCHA] Image CAPTCHA payload size: {len(image_base64)} bytes (base64)")
        
        try:
            request = urllib.request.Request(
                submit_url,
                data=post_data_encoded,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                method='POST'
            )
            
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read().decode('utf-8')
                logger.info(f"[2CAPTCHA] Image CAPTCHA API response status: {response.status}")
                logger.info(f"[2CAPTCHA] Raw API response: {response_body}")
                
                try:
                    submit_result = json.loads(response_body)
                    status = submit_result.get('status')
                    request_id = submit_result.get('request')
                    
                    if status == 1 and request_id:
                        task_id = request_id
                        logger.info(f"[2CAPTCHA] Image CAPTCHA submitted successfully. Task ID: {task_id}")
                    else:
                        error_text = submit_result.get('request', 'Unknown error')
                        logger.error(f"[2CAPTCHA] Failed to submit Image CAPTCHA: {error_text}")
                        return False, None, f"2Captcha submission failed: {error_text}"
                except json.JSONDecodeError:
                    # Fallback: parse plain text response (OK|task_id or ERROR|error_message)
                    if response_body.startswith('OK|'):
                        task_id = response_body.split('|')[1].strip()
                        logger.info(f"[2CAPTCHA] Image CAPTCHA submitted successfully. Task ID: {task_id}")
                    else:
                        error_msg = response_body.replace('ERROR|', '').strip()
                        logger.error(f"[2CAPTCHA] Failed to submit Image CAPTCHA: {error_msg}")
                        return False, None, f"2Captcha submission failed: {error_msg}"
                        
        except urllib.error.HTTPError as e:
            logger.error(f"[2CAPTCHA] HTTP error submitting Image CAPTCHA: {e}")
            error_body = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
            logger.error(f"[2CAPTCHA] Error response: {error_body}")
            return False, None, f"HTTP error: {e}"
        except Exception as e:
            logger.error(f"[2CAPTCHA] Error submitting Image CAPTCHA: {e}")
            logger.error(traceback.format_exc())
            return False, None, f"Error submitting task: {e}"
        
        # Poll for solution using res.php endpoint
        get_result_url = 'https://2captcha.com/res.php'
        max_wait_time = 120  # Maximum wait time in seconds
        poll_interval = 3  # Poll every 3 seconds
        start_time = time.time()
        
        logger.info(f"[2CAPTCHA] Waiting for 2Captcha to solve image CAPTCHA (this may take 10-120 seconds)...")
        
        while time.time() - start_time < max_wait_time:
            time.sleep(poll_interval)
            elapsed = int(time.time() - start_time)
            
            if elapsed % 15 == 0:  # Log every 15 seconds
                logger.info(f"[2CAPTCHA] Still solving... (waited {elapsed}s/{max_wait_time}s)")
            
            try:
                # Build query parameters
                params = {
                    'key': api_key,
                    'action': 'get',
                    'id': task_id,
                    'json': 1  # Request JSON response
                }
                
                query_string = urllib.parse.urlencode(params)
                result_url = f"{get_result_url}?{query_string}"
                
                result_request = urllib.request.Request(result_url, method='GET')
                
                with urllib.request.urlopen(result_request, timeout=30) as response:
                    result_body = response.read().decode('utf-8')
                    
                    try:
                        result = json.loads(result_body)
                        status = result.get('status')
                        
                        if status == 1:  # Ready
                            solution = result.get('request')
                            if solution:
                                logger.info(f"[2CAPTCHA] ✓ Image CAPTCHA solved! Solution: {solution}")
                                return True, solution, None
                            else:
                                logger.error("[2CAPTCHA] Solution received but empty")
                                return False, None, "Empty solution received"
                        elif status == 0:  # Processing
                            continue  # Keep polling
                        else:
                            error_text = result.get('request', 'Unknown error')
                            logger.error(f"[2CAPTCHA] Error getting solution: {error_text}")
                            return False, None, f"2Captcha solution error: {error_text}"
                    except json.JSONDecodeError:
                        # Fallback: parse plain text response
                        # Format: OK|solution_text or CAPCHA_NOT_READY or ERROR|error_message
                        if result_body.startswith('OK|'):
                            solution = result_body.split('|')[1].strip()
                            logger.info(f"[2CAPTCHA] ✓ Image CAPTCHA solved! Solution: {solution}")
                            return True, solution, None
                        elif result_body == 'CAPCHA_NOT_READY':
                            continue  # Keep polling
                        else:
                            error_msg = result_body.replace('ERROR|', '').strip()
                            logger.error(f"[2CAPTCHA] Error getting solution: {error_msg}")
                            return False, None, f"2Captcha solution error: {error_msg}"
                        
            except Exception as e:
                logger.warning(f"[2CAPTCHA] Error polling for solution: {e}")
                continue
        
        logger.error(f"[2CAPTCHA] Timeout waiting for solution (waited {max_wait_time}s)")
        return False, None, "Timeout waiting for CAPTCHA solution"
        
    except Exception as e:
        logger.error(f"[2CAPTCHA] Exception in solve_google_image_captcha: {e}")
        logger.error(traceback.format_exc())
        return False, None, str(e)

def solve_recaptcha_v2(driver, api_key, site_key=None, page_url=None):
    """
    Solve reCAPTCHA v2 using 2Captcha API.
    
    Args:
        driver: Selenium WebDriver instance
        api_key: 2Captcha API key
        site_key: reCAPTCHA site key (optional, will be extracted from page if not provided)
        page_url: Current page URL (optional, will use driver.current_url if not provided)
    
    Returns:
        (success: bool, token: str|None, error: str|None)
    """
    try:
        logger.info("[2CAPTCHA] Starting reCAPTCHA v2 solving...")
        
        # Get page URL if not provided
        if not page_url:
            page_url = driver.current_url
        
        # Extract site key from page if not provided
        if not site_key:
            logger.info("[2CAPTCHA] Extracting reCAPTCHA site key from page...")
            try:
                # Method 1: Try JavaScript extraction first (most reliable for dynamic content)
                try:
                    logger.info("[2CAPTCHA] Attempting JavaScript-based site key extraction...")
                    js_extraction_script = """
                    (function() {
                        var siteKey = null;
                        
                        // Check window.grecaptcha configuration
                        if (window.grecaptcha && window.grecaptcha.ready) {
                            try {
                                window.grecaptcha.ready(function() {
                                    if (window.grecaptcha.getResponse) {
                                        // Try to get site key from grecaptcha instance
                                        var widgets = document.querySelectorAll('[data-sitekey]');
                                        if (widgets.length > 0) {
                                            siteKey = widgets[0].getAttribute('data-sitekey');
                                        }
                                    }
                                });
                            } catch(e) {}
                        }
                        
                        // Check ___grecaptcha_cfg (Google's internal config)
                        if (!siteKey && window.___grecaptcha_cfg) {
                            try {
                                var cfg = window.___grecaptcha_cfg;
                                if (cfg.clients && cfg.clients[0]) {
                                    var client = cfg.clients[0];
                                    if (client.sitekey) {
                                        siteKey = client.sitekey;
                                    }
                                }
                                // Also check for sitekey in config directly
                                if (!siteKey && cfg.sitekey) {
                                    siteKey = cfg.sitekey;
                                }
                            } catch(e) {}
                        }
                        
                        // Check data-sitekey attributes
                        if (!siteKey) {
                            var elements = document.querySelectorAll('[data-sitekey]');
                            if (elements.length > 0) {
                                siteKey = elements[0].getAttribute('data-sitekey');
                            }
                        }
                        
                        // Check scripts for site key
                        if (!siteKey) {
                            var scripts = document.getElementsByTagName('script');
                            for (var i = 0; i < scripts.length; i++) {
                                var src = scripts[i].src || '';
                                var content = scripts[i].innerHTML || '';
                                var match = src.match(/[?&]k=([a-zA-Z0-9_-]{20,})/) || content.match(/sitekey['"]\\s*[:=]\\s*['"]([^'"]+)['"]/i);
                                if (match && match[1]) {
                                    siteKey = match[1];
                                    break;
                                }
                            }
                        }
                        
                        return siteKey;
                    })();
                    """
                    extracted_key = driver.execute_script(js_extraction_script)
                    if extracted_key and len(extracted_key.strip()) >= 20:
                        site_key = extracted_key.strip()
                        logger.info(f"[2CAPTCHA] ✓ Found site key via JavaScript: {site_key[:50]}... (length: {len(site_key)})")
                except Exception as js_err:
                    logger.debug(f"[2CAPTCHA] JavaScript extraction failed: {js_err}")
                
                # Method 2: Try to find site key in page source (static HTML)
                if not site_key:
                    logger.info("[2CAPTCHA] Attempting HTML-based site key extraction...")
                    page_source = driver.page_source
                    
                    # Pattern 1: data-sitekey attribute (most common for reCAPTCHA v2)
                    site_key_match = re.search(r'data-sitekey=["\']([^"\']{20,})["\']', page_source, re.IGNORECASE)
                    if site_key_match:
                        site_key = site_key_match.group(1).strip()
                        logger.info(f"[2CAPTCHA] Found site key in data-sitekey: {site_key[:50]}... (length: {len(site_key)})")
                    else:
                        # Pattern 2: recaptcha/api.js?render=SITE_KEY or k=SITE_KEY parameter
                        patterns = [
                            r'recaptcha/api\.js[^"\'<>]*[?&]render=([a-zA-Z0-9_-]{20,})',  # reCAPTCHA v3
                            r'[?&]k=([a-zA-Z0-9_-]{20,})',  # k= parameter
                            r'__recaptcha_api\.js[^"\'<>]*[?&]k=([a-zA-Z0-9_-]{20,})',  # Legacy format
                            r'recaptcha\.google\.com/recaptcha/api\.js[^"\'<>]*[?&]render=([a-zA-Z0-9_-]{20,})',  # Full URL
                            r'sitekey["\']\\s*[:=]\\s*["\']([^"\']{20,})["\']',  # JSON-style sitekey
                        ]
                        
                        for pattern in patterns:
                            site_key_match = re.search(pattern, page_source, re.IGNORECASE)
                            if site_key_match:
                                site_key = site_key_match.group(1).strip()
                                logger.info(f"[2CAPTCHA] Found site key using pattern: {site_key[:50]}... (length: {len(site_key)})")
                                break
                        
                        # Pattern 3: Check iframe src (fallback)
                        if not site_key:
                            try:
                                iframes = driver.find_elements(By.XPATH, "//iframe[contains(@src, 'recaptcha') or contains(@src, 'google.com/recaptcha')]")
                                logger.info(f"[2CAPTCHA] Found {len(iframes)} reCAPTCHA iframe(s)")
                                for iframe in iframes:
                                    iframe_src = iframe.get_attribute('src')
                                    if iframe_src:
                                        logger.debug(f"[2CAPTCHA] Checking iframe src: {iframe_src[:100]}...")
                                        # Try multiple patterns in iframe src
                                        for pattern in [r'[?&]k=([a-zA-Z0-9_-]{20,})', r'render=([a-zA-Z0-9_-]{20,})']:
                                            site_key_match = re.search(pattern, iframe_src, re.IGNORECASE)
                                            if site_key_match:
                                                site_key = site_key_match.group(1).strip()
                                                logger.info(f"[2CAPTCHA] Found site key in iframe src: {site_key[:50]}... (length: {len(site_key)})")
                                                break
                                        if site_key:
                                            break
                            except Exception as iframe_err:
                                logger.debug(f"[2CAPTCHA] Error checking iframes: {iframe_err}")
                                pass
                
                # Method 3: For Google login pages, try to extract from ___grecaptcha_cfg or use known site keys
                if not site_key and 'accounts.google.com' in page_url:
                    logger.warning("[2CAPTCHA] Could not extract site key using standard methods on Google login page")
                    logger.info("[2CAPTCHA] Trying deep JavaScript extraction for Google's reCAPTCHA...")
                    
                    try:
                        # Deep extraction script for Google's reCAPTCHA implementation
                        deep_extract_script = """
                        (function() {
                            // Check all possible locations where Google might store the site key
                            var siteKey = null;
                            
                            // Method 1: Check ___grecaptcha_cfg deeply
                            if (window.___grecaptcha_cfg) {
                                var cfg = window.___grecaptcha_cfg;
                                // Check clients
                                if (cfg.clients) {
                                    for (var clientId in cfg.clients) {
                                        var client = cfg.clients[clientId];
                                        // Check different possible locations
                                        if (client && typeof client === 'object') {
                                            for (var key in client) {
                                                var val = client[key];
                                                if (val && typeof val === 'object' && val.sitekey) {
                                                    return val.sitekey;
                                                }
                                                // Check nested objects
                                                if (val && typeof val === 'object') {
                                                    for (var nestedKey in val) {
                                                        var nestedVal = val[nestedKey];
                                                        if (nestedVal && typeof nestedVal === 'object' && nestedVal.sitekey) {
                                                            return nestedVal.sitekey;
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            
                            // Method 2: Check grecaptcha.enterprise object
                            if (window.grecaptcha && window.grecaptcha.enterprise) {
                                try {
                                    var enterprise = window.grecaptcha.enterprise;
                                    if (enterprise.getClients) {
                                        var clients = enterprise.getClients();
                                        for (var i = 0; i < clients.length; i++) {
                                            if (clients[i].sitekey) return clients[i].sitekey;
                                        }
                                    }
                                } catch(e) {}
                            }
                            
                            // Method 3: Search for recaptcha divs with data-sitekey
                            var divs = document.querySelectorAll('div[data-sitekey], div.g-recaptcha');
                            for (var i = 0; i < divs.length; i++) {
                                var key = divs[i].getAttribute('data-sitekey');
                                if (key && key.length >= 20) return key;
                            }
                            
                            // Method 4: Check iframes for k= parameter
                            var iframes = document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"]');
                            for (var i = 0; i < iframes.length; i++) {
                                var src = iframes[i].src || '';
                                var match = src.match(/[?&]k=([a-zA-Z0-9_-]{20,})/);
                                if (match) return match[1];
                            }
                            
                            // Method 5: Search page source for site key patterns
                            var html = document.documentElement.innerHTML;
                            var patterns = [
                                /data-sitekey=["']([^"']{20,})["']/,
                                /sitekey["']?\\s*[:=]\\s*["']([^"']{20,})["']/,
                                /grecaptcha\\.(?:enterprise\\.)?render\\([^,]+,\\s*\\{[^}]*sitekey["']?\\s*:\\s*["']([^"']+)["']/
                            ];
                            for (var i = 0; i < patterns.length; i++) {
                                var match = html.match(patterns[i]);
                                if (match && match[1]) return match[1];
                            }
                            
                            return null;
                        })();
                        """
                        deep_extracted_key = driver.execute_script(deep_extract_script)
                        if deep_extracted_key and len(str(deep_extracted_key).strip()) >= 20:
                            site_key = str(deep_extracted_key).strip()
                            logger.info(f"[2CAPTCHA] ✓ Found site key via deep extraction: {site_key[:50]}... (length: {len(site_key)})")
                    except Exception as deep_err:
                        logger.warning(f"[2CAPTCHA] Deep extraction failed: {deep_err}")
                    
                    # If still no site key, check if CAPTCHA is actually visible
                    if not site_key:
                        logger.warning("[2CAPTCHA] Could not extract site key from Google login page")
                        logger.warning("[2CAPTCHA] This may be an invisible reCAPTCHA or reCAPTCHA Enterprise")
                        logger.warning("[2CAPTCHA] Google's reCAPTCHA Enterprise often doesn't require manual solving")
                        
                        # Check if there's actually a visible CAPTCHA challenge
                        try:
                            visible_captcha_check = """
                            (function() {
                                // Check for visible CAPTCHA challenge
                                var iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                                for (var i = 0; i < iframes.length; i++) {
                                    var iframe = iframes[i];
                                    var rect = iframe.getBoundingClientRect();
                                    if (rect.width > 100 && rect.height > 100) {
                                        return 'visible_captcha';
                                    }
                                }
                                
                                // Check for audio/image CAPTCHA challenge
                                var challenges = document.querySelectorAll('[data-testid*="captcha"], .captcha-container, #captcha');
                                if (challenges.length > 0) return 'visible_captcha';
                                
                                return 'no_visible_captcha';
                            })();
                            """
                            captcha_type = driver.execute_script(visible_captcha_check)
                            if captcha_type == 'no_visible_captcha':
                                logger.info("[2CAPTCHA] No visible CAPTCHA challenge detected - may be invisible reCAPTCHA")
                                logger.info("[2CAPTCHA] Invisible reCAPTCHA typically auto-solves; proceeding without 2Captcha")
                                return False, None, "Invisible reCAPTCHA detected - auto-solving may be active"
                        except Exception as check_err:
                            logger.debug(f"[2CAPTCHA] Error checking CAPTCHA visibility: {check_err}")
                        
                        # NOTE: Removed hardcoded fallback site keys as they are likely outdated
                        # Google frequently changes their reCAPTCHA Enterprise keys
                        # Using an incorrect key causes ERROR_CAPTCHA_UNSOLVABLE (waste of API credits)
                        # Instead, we'll report that we couldn't extract the site key
                        logger.warning("[2CAPTCHA] Could not extract site key from Google login page")
                        logger.warning("[2CAPTCHA] This may be an invisible reCAPTCHA or reCAPTCHA Enterprise")
                        logger.warning("[2CAPTCHA] Google's reCAPTCHA Enterprise often doesn't require manual solving")
                        logger.warning("[2CAPTCHA] The login may still proceed without CAPTCHA solving")
                        # Don't set a fallback key - return failure instead of wasting API credits
                        return False, None, "Could not extract reCAPTCHA site key from Google page. Login may still proceed."
                
                if not site_key:
                    logger.error("[2CAPTCHA] Could not extract reCAPTCHA site key from page")
                    logger.error(f"[2CAPTCHA] Page URL: {page_url}")
                    logger.error(f"[2CAPTCHA] Page title: {driver.title}")
                    # Log a sample of page source for debugging
                    try:
                        page_source_sample = driver.page_source[:1000]
                        logger.debug(f"[2CAPTCHA] Page source sample (first 1000 chars): {page_source_sample}")
                    except:
                        pass
                    return False, None, "Could not extract reCAPTCHA site key"
            except Exception as e:
                logger.error(f"[2CAPTCHA] Error extracting site key: {e}")
                logger.error(traceback.format_exc())
                return False, None, f"Error extracting site key: {e}"
        
        # Validate site key before proceeding
        if not site_key or len(site_key.strip()) == 0:
            logger.error(f"[2CAPTCHA] Site key is empty or None! Cannot proceed.")
            return False, None, "Site key is empty or None"
        
        # Clean and validate site key format (should be alphanumeric with dashes/underscores)
        site_key = site_key.strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', site_key):
            logger.warning(f"[2CAPTCHA] Site key format may be invalid: {site_key[:50]}...")
        
        logger.info(f"[2CAPTCHA] Site key: {site_key[:50]}... (length: {len(site_key)}), Page URL: {page_url[:80]}...")
        
        # Step 1: Create task to solve CAPTCHA using 2Captcha API v2
        # Official 2Captcha API v2 Documentation:
        # POST https://api.2captcha.com/createTask
        # Reference: https://2captcha.com/api-docs/recaptcha-v2
        logger.info("[2CAPTCHA] Creating task to solve reCAPTCHA using 2Captcha API v2...")
        create_task_url = 'https://api.2captcha.com/createTask'
        
        # Validate site key length (reCAPTCHA site keys are typically 40 characters)
        if len(site_key) < 20:
            logger.error(f"[2CAPTCHA] Site key is too short ({len(site_key)} chars). Expected 20+ characters. Value: {site_key}")
            return False, None, f"Site key is too short ({len(site_key)} chars)"
        
        # Determine task type based on the page
        # Google uses reCAPTCHA Enterprise, so we need RecaptchaV2EnterpriseTaskProxyless
        is_google_page = 'google.com' in page_url or 'google.' in page_url
        
        if is_google_page:
            task_type = 'RecaptchaV2EnterpriseTaskProxyless'
            logger.info("[2CAPTCHA] Detected Google page - using reCAPTCHA Enterprise task type")
        else:
            task_type = 'RecaptchaV2TaskProxyless'
        
        # API v2 uses JSON format with task object
        task_data = {
            'clientKey': api_key,
            'task': {
                'type': task_type,
                'websiteURL': page_url,
                'websiteKey': site_key  # This is the correct field name for API v2
            }
        }
        
        # For Enterprise, we may need to add additional parameters
        if is_google_page:
            # Add enterprise-specific options if needed
            task_data['task']['isInvisible'] = False  # Visible CAPTCHA
        
        # Log the request payload (without exposing full API key)
        logger.info(f"[2CAPTCHA] Request payload:")
        logger.info(f"[2CAPTCHA]   clientKey: {api_key[:10]}...")
        logger.info(f"[2CAPTCHA]   websiteURL: {page_url}")
        logger.info(f"[2CAPTCHA]   websiteKey: {site_key} (length: {len(site_key)})")
        logger.info(f"[2CAPTCHA]   task type: {task_type}")
        
        # Create JSON request
        json_data = json.dumps(task_data).encode('utf-8')
        logger.info(f"[2CAPTCHA] JSON payload (first 400 chars): {json_data.decode('utf-8')[:400]}")
        
        request = urllib.request.Request(
            create_task_url,
            data=json_data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        
        try:
            logger.info(f"[2CAPTCHA] Sending POST request to {create_task_url}...")
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read().decode('utf-8')
                logger.info(f"[2CAPTCHA] API response status: {response.status}")
                logger.info(f"[2CAPTCHA] Raw API response: {response_body}")
                create_result = json.loads(response_body)
                
                # Check for errors (errorId 0 = success)
                if create_result.get('errorId') != 0:
                    error_code = create_result.get('errorCode', 'Unknown')
                    error_desc = create_result.get('errorDescription', 'Unknown error')
                    logger.error(f"[2CAPTCHA] Failed to create task: {error_code} - {error_desc}")
                    return False, None, f"2Captcha task creation failed: {error_desc}"
                
                # Extract task ID from response
                task_id = create_result.get('taskId')
                if not task_id:
                    logger.error("[2CAPTCHA] No task ID received from 2Captcha")
                    return False, None, "No task ID received from 2Captcha"
                
                logger.info(f"[2CAPTCHA] Task created successfully. Task ID: {task_id}")
        except urllib.error.HTTPError as e:
            logger.error(f"[2CAPTCHA] HTTP error creating task: {e}")
            return False, None, f"HTTP error creating task: {e}"
        except json.JSONDecodeError as e:
            logger.error(f"[2CAPTCHA] Invalid JSON response from 2Captcha: {e}")
            return False, None, f"Invalid response from 2Captcha: {e}"
        except Exception as e:
            logger.error(f"[2CAPTCHA] Error creating task: {e}")
            return False, None, f"Error creating task: {e}"
        
        # Step 2: Poll for solution using 2Captcha API v2
        # Official 2Captcha API v2 Documentation:
        # POST https://api.2captcha.com/getTaskResult
        # Reference: https://2captcha.com/api-docs/recaptcha-v2
        logger.info("[2CAPTCHA] Waiting for 2Captcha to solve CAPTCHA (this may take 10-120 seconds)...")
        get_task_url = 'https://api.2captcha.com/getTaskResult'
        max_wait = 120  # 2 minutes (2Captcha typically solves in 10-30 seconds)
        poll_interval = 5  # Check every 5 seconds (2Captcha recommends 5-10 seconds)
        waited = 0
        
        while waited < max_wait:
            time.sleep(poll_interval)
            waited += poll_interval
            
            # API v2 uses JSON format
            request_data = {
                'clientKey': api_key,
                'taskId': task_id
            }
            
            json_data = json.dumps(request_data).encode('utf-8')
            request = urllib.request.Request(
                get_task_url,
                data=json_data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    get_result = json.loads(response.read().decode('utf-8'))
                    
                    # Check for errors
                    if get_result.get('errorId') != 0:
                        error_code = get_result.get('errorCode', 'Unknown')
                        error_desc = get_result.get('errorDescription', 'Unknown error')
                        logger.error(f"[2CAPTCHA] Error getting solution: {error_code} - {error_desc}")
                        return False, None, f"2Captcha solution error: {error_desc}"
                    
                    # Check status (ready = solved, processing = still solving)
                    status = get_result.get('status')
                    if status == 'ready':
                        # Solution is ready - extract token
                        solution = get_result.get('solution', {})
                        token = solution.get('gRecaptchaResponse') or solution.get('token')
                        if token:
                            logger.info(f"[2CAPTCHA] ✓✓✓ CAPTCHA solved successfully! Token received (waited {waited}s)")
                            return True, token, None
                        else:
                            logger.error("[2CAPTCHA] Solution received but token is empty")
                            return False, None, "Empty token received from 2Captcha"
                    elif status == 'processing':
                        # Still solving - continue polling
                        if waited % 15 == 0:  # Log progress every 15 seconds
                            logger.info(f"[2CAPTCHA] Still solving... (waited {waited}s/{max_wait}s)")
                    else:
                        # Unknown status
                        logger.warning(f"[2CAPTCHA] Unknown status: {status}")
            except urllib.error.HTTPError as e:
                logger.warning(f"[2CAPTCHA] HTTP error polling for solution: {e}")
                # Continue polling - might be transient error
            except json.JSONDecodeError as e:
                logger.warning(f"[2CAPTCHA] Invalid JSON response while polling: {e}")
                # Continue polling - might be transient error
            except Exception as e:
                logger.warning(f"[2CAPTCHA] Error polling for solution: {e}")
                # Continue polling - might be transient error
        
        logger.error(f"[2CAPTCHA] Timeout waiting for CAPTCHA solution (waited {waited}s)")
        return False, None, "Timeout waiting for 2Captcha solution"
        
    except Exception as e:
        logger.error(f"[2CAPTCHA] Exception solving CAPTCHA: {e}")
        logger.error(traceback.format_exc())
        return False, None, f"Exception: {e}"

def inject_recaptcha_token(driver, token):
    """
    Inject the solved reCAPTCHA token into the page.
    Google reCAPTCHA requires the token to be set in g-recaptcha-response textarea/input
    and the callback function to be executed.
    """
    try:
        logger.info("[2CAPTCHA] Injecting reCAPTCHA token into page...")
        
        # Comprehensive token injection script for Google login pages
        injection_script = f"""
        (function() {{
            var token = '{token}';
            var injected = false;
            
            // Method 1: Set token in g-recaptcha-response textarea/input (most important for Google)
            var recaptchaResponse = document.querySelector('textarea[name="g-recaptcha-response"]') || 
                                    document.querySelector('input[name="g-recaptcha-response"]') ||
                                    document.querySelector('textarea#g-recaptcha-response') ||
                                    document.querySelector('input#g-recaptcha-response');
            
            if (recaptchaResponse) {{
                recaptchaResponse.value = token;
                recaptchaResponse.innerHTML = token; // For textarea
                
                // Trigger all necessary events
                var events = ['input', 'change', 'keyup', 'blur'];
                events.forEach(function(eventType) {{
                    var event = new Event(eventType, {{ bubbles: true, cancelable: true }});
                    recaptchaResponse.dispatchEvent(event);
                }});
                
                injected = true;
                console.log('[2CAPTCHA] Token set in g-recaptcha-response element');
            }}
            
            // Method 2: Find and execute callback function
            var callbackName = null;
            
            // Search in scripts for callback
            var scripts = document.getElementsByTagName('script');
            for (var i = 0; i < scripts.length; i++) {{
                var scriptText = scripts[i].innerHTML || scripts[i].textContent || '';
                
                // Pattern 1: grecaptcha.execute with callback
                var match1 = scriptText.match(/grecaptcha\\.execute\\([^,]+,\\s*{{[^}}]*callback:\\s*['"]([^'"]+)['"]/);
                if (match1) {{
                    callbackName = match1[1];
                    break;
                }}
                
                // Pattern 2: callback in data attributes
                var match2 = scriptText.match(/callback['"]?\\s*[:=]\\s*['"]([^'"]+)['"]/);
                if (match2) {{
                    callbackName = match2[1];
                    break;
                }}
            }}
            
            // Method 3: Check window.grecaptcha configuration
            if (!callbackName && window.grecaptcha) {{
                try {{
                    // Check grecaptcha configuration
                    for (var key in window) {{
                        if (key.startsWith('___grecaptcha_cfg')) {{
                            var cfg = window[key];
                            if (cfg && cfg.callback) {{
                                callbackName = cfg.callback;
                                break;
                            }}
                        }}
                    }}
                }} catch (e) {{
                    console.log('[2CAPTCHA] Error checking grecaptcha config:', e);
                }}
            }}
            
            // Execute callback if found
            if (callbackName && window[callbackName]) {{
                try {{
                    window[callbackName](token);
                    console.log('[2CAPTCHA] Callback executed:', callbackName);
                    injected = true;
                }} catch (e) {{
                    console.log('[2CAPTCHA] Error executing callback:', e);
                }}
            }}
            
            // Method 4: Set token in window object for Google's scripts to find
            window.grecaptchaToken = token;
            window.__grecaptchaToken = token;
            
            // Method 5: Find all recaptcha-related inputs and set token
            var allRecaptchaInputs = document.querySelectorAll(
                'textarea[name*="recaptcha"], input[name*="recaptcha"], ' +
                'textarea[id*="recaptcha"], input[id*="recaptcha"], ' +
                'textarea[class*="recaptcha"], input[class*="recaptcha"]'
            );
            
            for (var i = 0; i < allRecaptchaInputs.length; i++) {{
                var inp = allRecaptchaInputs[i];
                inp.value = token;
                if (inp.tagName === 'TEXTAREA') {{
                    inp.innerHTML = token;
                }}
                
                // Trigger events
                ['input', 'change', 'keyup'].forEach(function(eventType) {{
                    var evt = new Event(eventType, {{ bubbles: true }});
                    inp.dispatchEvent(evt);
                }});
                
                injected = true;
            }}
            
            // Method 6: Try to find recaptcha widget and set response
            if (window.grecaptcha && window.grecaptcha.getResponse) {{
                try {{
                    // Get widget ID (usually 0 for first widget)
                    var widgetId = 0;
                    var response = window.grecaptcha.getResponse(widgetId);
                    if (!response) {{
                        // Try to set response directly if possible
                        console.log('[2CAPTCHA] Attempting to set grecaptcha response');
                    }}
                }} catch (e) {{
                    console.log('[2CAPTCHA] Error accessing grecaptcha widget:', e);
                }}
            }}
            
            return injected ? 'token_injected' : 'no_target_found';
        }})();
        """
        
        result = driver.execute_script(injection_script)
        logger.info(f"[2CAPTCHA] Token injection result: {result}")
        
        # Wait for page to process the token
        time.sleep(3)
        
        # Verify token was set by checking the element
        try:
            recaptcha_elements = driver.find_elements(By.XPATH, 
                "//textarea[@name='g-recaptcha-response'] | //input[@name='g-recaptcha-response']")
            if recaptcha_elements:
                for elem in recaptcha_elements:
                    current_value = elem.get_attribute('value') or driver.execute_script("return arguments[0].value || arguments[0].innerHTML;", elem)
                    if current_value == token:
                        logger.info("[2CAPTCHA] ✓ Verified token is set in g-recaptcha-response element")
                    else:
                        logger.warning(f"[2CAPTCHA] Token value mismatch. Expected: {token[:20]}..., Got: {str(current_value)[:20] if current_value else 'None'}...")
        except Exception as verify_err:
            logger.debug(f"[2CAPTCHA] Could not verify token: {verify_err}")
        
        # Additional method: Try to find and fill any hidden recaptcha inputs
        try:
            hidden_inputs = driver.find_elements(By.XPATH, 
                "//input[@type='hidden' and contains(@name, 'recaptcha')] | " +
                "//textarea[@style*='display: none' and contains(@name, 'recaptcha')]")
            for inp in hidden_inputs:
                driver.execute_script("arguments[0].value = arguments[1];", inp, token)
                logger.info("[2CAPTCHA] Token set in hidden recaptcha input")
        except Exception as hidden_err:
            logger.debug(f"[2CAPTCHA] Hidden input injection: {hidden_err}")
        
        return True
    except Exception as e:
        logger.error(f"[2CAPTCHA] Error injecting token: {e}")
        logger.error(traceback.format_exc())
        return False

def solve_captcha_with_2captcha(driver, email=None):
    """
    Detect and solve CAPTCHA using 2Captcha API if enabled.
    Automatically detects whether it's reCAPTCHA or Google image CAPTCHA.
    Returns (solved: bool, error: str|None)
    
    Args:
        driver: Selenium WebDriver instance
        email: User email (optional, for screenshot naming)
    """
    try:
        # Check if 2Captcha is enabled
        twocaptcha_config = get_twocaptcha_config()
        if not twocaptcha_config.get('enabled') or not twocaptcha_config.get('api_key'):
            logger.info("[2CAPTCHA] 2Captcha is not enabled or API key not configured")
            return False, "2Captcha not enabled"
        
        api_key = twocaptcha_config['api_key']
        logger.info("[2CAPTCHA] 2Captcha is enabled, detecting CAPTCHA type...")
        
        # First, check if it's Google's image CAPTCHA (not reCAPTCHA)
        # Use multiple detection methods for better reliability
        is_image_captcha = False
        captcha_input = None
        
        try:
            # Method 1: Check for input fields with name="ca" or id="ca"
            captcha_inputs = driver.find_elements(By.XPATH, "//input[@name='ca' or @id='ca']")
            for inp in captcha_inputs:
                try:
                    if inp.is_displayed():
                        aria_label = inp.get_attribute('aria-label') or ''
                        if 'hear or see' in aria_label.lower() or 'captcha' in aria_label.lower():
                            is_image_captcha = True
                            captcha_input = inp
                            logger.info("[2CAPTCHA] Detected Google image CAPTCHA via name/id='ca' (not reCAPTCHA)")
                            break
                except:
                    continue
            
            # Method 2: If not found, check for any input with aria-label containing "hear or see" or "captcha"
            if not is_image_captcha:
                try:
                    all_inputs = driver.find_elements(By.TAG_NAME, "input")
                    for inp in all_inputs:
                        try:
                            if inp.is_displayed():
                                aria_label = inp.get_attribute('aria-label') or ''
                                placeholder = inp.get_attribute('placeholder') or ''
                                name_attr = inp.get_attribute('name') or ''
                                
                                # Check for CAPTCHA-related text
                                if ('hear or see' in aria_label.lower() or 
                                    'captcha' in aria_label.lower() or
                                    'captcha' in placeholder.lower() or
                                    'ca' in name_attr.lower()):
                                    # Verify there's a CAPTCHA image nearby
                                    try:
                                        # Check if there's a CAPTCHA image on the page
                                        captcha_images = driver.find_elements(By.XPATH, 
                                            "//img[contains(@src, 'captcha') or contains(@alt, 'captcha')]")
                                        if captcha_images or 'captcha' in driver.page_source.lower():
                                            is_image_captcha = True
                                            captcha_input = inp
                                            logger.info("[2CAPTCHA] Detected Google image CAPTCHA via aria-label/placeholder (not reCAPTCHA)")
                                            break
                                    except:
                                        # If we can't verify image, still use it if aria-label matches
                                        if 'hear or see' in aria_label.lower():
                                            is_image_captcha = True
                                            captcha_input = inp
                                            logger.info("[2CAPTCHA] Detected Google image CAPTCHA via 'hear or see' aria-label")
                                            break
                        except:
                            continue
                except Exception as e:
                    logger.debug(f"[2CAPTCHA] Error in Method 2 detection: {e}")
            
            # Method 3: Check page source for CAPTCHA indicators
            if not is_image_captcha:
                try:
                    page_source = driver.page_source.lower()
                    # Check for Google's CAPTCHA indicators
                    if ('type the text you hear' in page_source or 
                        'type the text you see' in page_source or
                        'captcha' in page_source):
                        # Look for any visible input field that might be the CAPTCHA input
                        try:
                            # Try to find input near CAPTCHA-related text
                            captcha_inputs = driver.find_elements(By.XPATH, 
                                "//input[@type='text' or @type='password']")
                            for inp in captcha_inputs:
                                try:
                                    if inp.is_displayed():
                                        # Check if it's near CAPTCHA-related elements
                                        parent = inp.find_element(By.XPATH, "./..")
                                        parent_text = parent.text.lower() if parent else ''
                                        if 'captcha' in parent_text or 'hear' in parent_text or 'see' in parent_text:
                                            is_image_captcha = True
                                            captcha_input = inp
                                            logger.info("[2CAPTCHA] Detected Google image CAPTCHA via page source analysis")
                                            break
                                except:
                                    continue
                        except:
                            pass
                except Exception as e:
                    logger.debug(f"[2CAPTCHA] Error in Method 3 detection: {e}")
                    
        except Exception as e:
            logger.debug(f"[2CAPTCHA] Error checking for image CAPTCHA: {e}")
        
        if is_image_captcha and captcha_input:
            # Solve Google image CAPTCHA using ImageToTextTask
            logger.info("[2CAPTCHA] Solving Google image CAPTCHA using ImageToTextTask...")
            success, solution, error = solve_google_image_captcha(driver, api_key, email=email)
            
            if not success or not solution:
                logger.error(f"[2CAPTCHA] Failed to solve image CAPTCHA: {error}")
                capture_captcha_screenshot(driver, captcha_type="image_solve_failed", email=email)
                return False, error or "Failed to solve image CAPTCHA"
            
            # Enter the solution into the input field
            try:
                logger.info(f"[2CAPTCHA] Entering solution into CAPTCHA input field: {solution}")
                
                # RE-LOCATE the CAPTCHA input field to ensure we have a fresh reference
                # This prevents stale element issues
                captcha_input = None
                captcha_input_xpaths = [
                    "/html/body/div[2]/div[1]/div[1]/div[2]/c-wiz/main/div[2]/div/div/div/form/span/section[2]/div/div/div[2]/div[2]/div[1]/div[1]/div/div[1]/input", # User provided specific XPath
                    "//input[@name='ca']",
                    "//input[@id='ca']",
                    "//input[contains(@aria-label, 'Type the text you hear or see')]",
                    "//input[contains(@aria-label, 'hear or see')]",
                    "//input[contains(@aria-label, 'captcha')]",
                    "//input[@type='text' and contains(@placeholder, 'captcha')]"
                ]
                
                for xpath in captcha_input_xpaths:
                    try:
                        elements = driver.find_elements(By.XPATH, xpath)
                        for elem in elements:
                            if elem.is_displayed():
                                captcha_input = elem
                                logger.info(f"[2CAPTCHA] Re-located CAPTCHA input using: {xpath}")
                                break
                        if captcha_input:
                            break
                    except:
                        continue
                
                # Fallback: User provided JS Path
                if not captcha_input:
                    try:
                        logger.info("[2CAPTCHA] XPath failed, trying user-provided JS Path...")
                        js_selector = "#yDmH0d > c-wiz > main > div.UXFQgc > div > div > div > form > span > section:nth-child(2) > div > div > div.lbFS4d > div.AFTWye > div.rFrNMe.X3mtXb.UOsO2.zKHdkd.sdJrJc > div.aCsJod.oJeWuf"
                        captcha_input = driver.execute_script(f"""
                            var el = document.querySelector('{js_selector}');
                            if (el) {{
                                // If it's an input, return it
                                if (el.tagName === 'INPUT') return el;
                                // If it's a container, find the input inside
                                return el.querySelector('input');
                            }}
                            return null;
                        """)
                        if captcha_input:
                            logger.info("[2CAPTCHA] Found CAPTCHA input using user-provided JS Path")
                    except Exception as js_err:
                        logger.warning(f"[2CAPTCHA] User JS Path failed: {js_err}")

                if not captcha_input:
                    logger.error("[2CAPTCHA] Could not re-locate CAPTCHA input field!")
                    return False, "Could not locate CAPTCHA input field"
                
                # Click on the input to ensure it has focus
                try:
                    captcha_input.click()
                    time.sleep(0.3)
                    logger.info("[2CAPTCHA] Clicked on CAPTCHA input to ensure focus")
                except:
                    # Try JavaScript click as fallback
                    try:
                        driver.execute_script("arguments[0].click(); arguments[0].focus();", captcha_input)
                        time.sleep(0.3)
                        logger.info("[2CAPTCHA] Used JavaScript to focus CAPTCHA input")
                    except Exception as focus_err:
                        logger.warning(f"[2CAPTCHA] Could not focus CAPTCHA input: {focus_err}")
                
                # Clear the field first
                captcha_input.clear()
                time.sleep(0.3)
                
                # Type the solution using direct send_keys (most reliable)
                captcha_input.send_keys(solution)
                time.sleep(0.5)
                
                # VERIFICATION: Check if the value was actually typed
                typed_value = captcha_input.get_attribute('value')
                logger.info(f"[2CAPTCHA] Typed value in field: '{typed_value}'")
                
                if typed_value != solution:
                    logger.warning(f"[2CAPTCHA] Input mismatch! Typed: '{typed_value}', Expected: '{solution}'. Retrying with JavaScript...")
                    # Try JavaScript to set the value
                    driver.execute_script("""
                        var input = arguments[0];
                        var value = arguments[1];
                        input.value = value;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    """, captcha_input, solution)
                    time.sleep(0.3)
                    
                    # Verify again
                    typed_value = captcha_input.get_attribute('value')
                    if typed_value != solution:
                        logger.error(f"[2CAPTCHA] FAILED to type solution! Field value: '{typed_value}'")
                        return False, f"Failed to type solution into field. Field shows: '{typed_value}'"
                
                logger.info(f"[2CAPTCHA] ✓ Solution entered and verified: {solution}")
                
                # Now submit the CAPTCHA
                time.sleep(0.5)
                captcha_input.send_keys(Keys.RETURN)
                logger.info("[2CAPTCHA] Pressed Enter key to submit CAPTCHA solution")
                time.sleep(2)  # Wait for page to process
                
                # Check if page changed (CAPTCHA was submitted successfully)
                current_url = driver.current_url
                if "challenge/pwd" in current_url or "myaccount.google.com" in current_url:
                    logger.info("[2CAPTCHA] ✓✓✓ CAPTCHA submitted successfully with Enter key!")
                    return True, None
                    
            except Exception as enter_err:
                logger.error(f"[2CAPTCHA] Error entering CAPTCHA solution: {enter_err}")
                logger.error(traceback.format_exc())
                
                # If Enter didn't work, try finding and clicking Next/Continue button
                try:
                    # Look for Next/Continue button
                    next_button_xpaths = [
                        "//button[contains(., 'Next') or contains(., 'NEXT')]",
                        "//span[contains(., 'Next')]/ancestor::button",
                        "//div[@role='button' and contains(., 'Next')]",
                        "//button[contains(., 'Continue')]",
                        "//span[contains(., 'Continue')]/ancestor::button",
                        "//*[@id='identifierNext']",
                        "//*[@id='passwordNext']"
                    ]
                    
                    next_button = None
                    for xpath in next_button_xpaths:
                        try:
                            buttons = driver.find_elements(By.XPATH, xpath)
                            for btn in buttons:
                                if btn.is_displayed() and btn.is_enabled():
                                    next_button = btn
                                    break
                            if next_button:
                                break
                        except:
                            continue
                    
                    if next_button:
                        logger.info("[2CAPTCHA] Clicking Next/Continue button after entering CAPTCHA solution...")
                        driver.execute_script("arguments[0].click();", next_button)
                        time.sleep(2)
                    else:
                        logger.debug("[2CAPTCHA] No Next/Continue button found, solution already entered")
                except Exception as submit_err:
                    logger.debug(f"[2CAPTCHA] Could not find/click Next button: {submit_err}")
                    # Continue anyway - the solution is entered
                
                logger.info("[2CAPTCHA] ✓✓✓ Google image CAPTCHA solved and solution entered!")
                return True, None
                
            except Exception as enter_err:
                logger.error(f"[2CAPTCHA] Error entering solution into input field: {enter_err}")
                logger.error(traceback.format_exc())
                return False, f"Failed to enter solution: {enter_err}"
        
        # Otherwise, try to solve as reCAPTCHA
        logger.info("[2CAPTCHA] Attempting to solve as reCAPTCHA...")
        success, token, error = solve_recaptcha_v2(driver, api_key)
        
        if not success or not token:
            logger.error(f"[2CAPTCHA] Failed to solve CAPTCHA: {error}")
            # Capture screenshot of the CAPTCHA that couldn't be solved
            capture_captcha_screenshot(driver, captcha_type="solve_failed", email=email)
            return False, error or "Failed to solve CAPTCHA"
        
        # Inject the token into the page
        inject_success = inject_recaptcha_token(driver, token)
        
        if not inject_success:
            logger.warning("[2CAPTCHA] Token injection may have failed, but token was received")
            # Continue anyway - sometimes the page processes it even if injection seems to fail
        
        logger.info("[2CAPTCHA] ✓✓✓ CAPTCHA solved and token injected successfully!")
        return True, None
        
    except Exception as e:
        logger.error(f"[2CAPTCHA] Exception in solve_captcha_with_2captcha: {e}")
        logger.error(traceback.format_exc())
        return False, str(e)

def capture_captcha_screenshot(driver, captcha_type="unknown", email=None):
    """
    Capture a screenshot when CAPTCHA is detected and upload it to S3.
    
    DISABLED: Screenshot capture has been permanently disabled.
    
    Args:
        driver: Selenium WebDriver instance
        captcha_type: Type of CAPTCHA detected (e.g., "recaptcha", "audio", "image")
        email: User email (optional, for naming the screenshot)
    
    Returns:
        (success: bool, s3_path: str|None)
    """
    # Screenshot capture is permanently disabled
    return False, None

def detect_captcha(driver, email=None):
    """
    Detect if Google CAPTCHA is present on the page - more accurate detection to avoid false positives.
    
    Args:
        driver: Selenium WebDriver instance
        email: User email (optional, for screenshot naming)
    
    Returns True if:
    - Visible reCAPTCHA challenge is present
    - CAPTCHA-related error messages are shown
    - User is blocked due to automated detection
    
    Returns False if:
    - Only invisible reCAPTCHA badge is present (not a blocking CAPTCHA)
    - No CAPTCHA indicators found
    """
    try:
        # Log current page state for debugging
        current_url = driver.current_url
        page_title = driver.title
        logger.debug(f"[CAPTCHA] Checking for CAPTCHA on: {current_url[:80]}... (Title: {page_title})")
        
        # First check for explicit CAPTCHA challenge iframes (blocking CAPTCHA)
        try:
            captcha_iframes = driver.find_elements(By.XPATH, "//iframe[contains(@src, 'recaptcha') or contains(@src, 'google.com/recaptcha')]")
            if captcha_iframes:
                # Check if iframe is actually visible AND a blocking challenge (not just badge)
                for iframe in captcha_iframes:
                    try:
                        if iframe.is_displayed():
                            # Check iframe size - badge is small, challenge is large
                            iframe_size = iframe.size
                            if iframe_size['width'] > 100 and iframe_size['height'] > 100:
                                logger.warning(f"[CAPTCHA] Detected visible reCAPTCHA iframe (size: {iframe_size['width']}x{iframe_size['height']})")
                                # Capture screenshot for analysis
                                capture_captcha_screenshot(driver, captcha_type="recaptcha", email=email)
                                return True
                            else:
                                # This is likely just the reCAPTCHA badge (invisible reCAPTCHA)
                                logger.debug(f"[CAPTCHA] Found small reCAPTCHA iframe (badge): {iframe_size['width']}x{iframe_size['height']}")
                    except Exception as iframe_err:
                        logger.debug(f"[CAPTCHA] Error checking iframe: {iframe_err}")
                        continue
        except Exception as e:
            logger.debug(f"[CAPTCHA] Error checking for CAPTCHA iframes: {e}")
        
        # Check for CAPTCHA-specific error messages (high confidence - indicates blocking)
        high_confidence_indicators = [
            "//div[contains(text(), 'unusual traffic from your computer network')]",
            "//div[contains(text(), 'automated queries')]",
            "//div[contains(text(), 'verify you') and contains(text(), 'not a robot')]",
            "//span[contains(text(), 'unusual traffic from your computer network')]",
            "//span[contains(text(), 'automated queries')]",
            "//*[contains(text(), 'Try again later') and contains(text(), 'automated')]",
            "//*[contains(text(), 'complete a CAPTCHA')]",
            "//*[contains(text(), 'confirm you are not a robot')]",
        ]
        
        for indicator in high_confidence_indicators:
            try:
                elements = driver.find_elements(By.XPATH, indicator)
                if elements:
                    # Verify element is visible
                    for element in elements:
                        try:
                            if element.is_displayed():
                                element_text = element.text[:100] if element.text else "no text"
                                logger.warning(f"[CAPTCHA] Detected CAPTCHA message: {element_text}")
                                # Capture screenshot for analysis
                                capture_captcha_screenshot(driver, captcha_type="message", email=email)
                                return True
                        except:
                            continue
            except:
                continue
        
        # Check for reCAPTCHA challenge container (more specific than generic captcha class)
        try:
            recaptcha_containers = driver.find_elements(By.XPATH, "//div[contains(@class, 'rc-') or contains(@id, 'recaptcha')]")
            if recaptcha_containers:
                # Only return True if we also see CAPTCHA-related text
                page_text = driver.page_source.lower()
                if any(keyword in page_text for keyword in ['verify', 'robot', 'unusual traffic', 'automated']):
                    logger.warning("[CAPTCHA] Detected reCAPTCHA container with related text")
                    # Capture screenshot for analysis
                    capture_captcha_screenshot(driver, captcha_type="recaptcha_container", email=email)
                    return True
        except:
            pass
        
        # Last resort: check page source for multiple CAPTCHA indicators together (reduces false positives)
        page_source = driver.page_source.lower()
        captcha_keywords_found = []
        
        # Look for specific high-confidence phrases
        if 'unusual traffic from your computer network' in page_source:
            captcha_keywords_found.append('unusual traffic')
        if 'automated queries' in page_source and 'verify' in page_source:
            captcha_keywords_found.append('automated queries + verify')
        if 'try again later' in page_source and ('automated' in page_source or 'robot' in page_source):
            captcha_keywords_found.append('try again + automated/robot')
        
        # Only return True if we found multiple strong indicators
        if len(captcha_keywords_found) >= 1 and ('recaptcha' in page_source or 'captcha' in page_source):
            logger.warning(f"[CAPTCHA] Detected CAPTCHA indicators in page source: {', '.join(captcha_keywords_found)}")
            return True
        
        return False
    except Exception as e:
        logger.warning(f"[CAPTCHA] Error detecting CAPTCHA: {e}")
        return False

def wait_for_clickable_xpath(driver, xpath, timeout=30):
    """Wait for an element to be clickable and return it."""
    try:
        element = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath))
        )
        return element
    except TimeoutException:
        logger.error(f"[SELENIUM] Timeout waiting for clickable XPath: {xpath}")
        return None

def click_xpath(driver, xpath, timeout=30):
    """Click an element by XPath."""
    element = wait_for_clickable_xpath(driver, xpath, timeout=timeout)
    if element:
        element.click()
        return True
    return False

def element_exists(driver, xpath, timeout=10):
    """Check if an element exists without throwing exception."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
        return True
    except TimeoutException:
        return False

def find_element_with_fallback(driver, xpath_list, timeout=30, description="element"):
    """Try multiple XPaths and return the first found element."""
    for xpath in xpath_list:
        try:
            element = wait_for_xpath(driver, xpath, timeout=timeout)
            if element:
                logger.info(f"[STEP] Found {description} using xpath: {xpath}")
                return element
        except:
            continue
    logger.error(f"[STEP] Could not find {description} with any of the provided xpaths")
    return None


# =====================================================================
# SFTP upload for TOTP secrets
# =====================================================================

def upload_secret_to_sftp(email, secret_key):
    """
    Upload the TOTP secret key to SFTP server.
    Environment vars:
      SECRET_SFTP_HOST         (required)
      SECRET_SFTP_USER         (required)
      SECRET_SFTP_PASSWORD     (required)
      SECRET_SFTP_PORT         (optional, default 22)
      SECRET_SFTP_REMOTE_DIR   (optional, default /root/gw_secrets)
    """
    host = os.environ.get("SECRET_SFTP_HOST", "46.224.9.127")
    port = int(os.environ.get("SECRET_SFTP_PORT", "22"))
    user = os.environ.get("SECRET_SFTP_USER")
    password = os.environ.get("SECRET_SFTP_PASSWORD")
    remote_dir = os.environ.get("SECRET_SFTP_REMOTE_DIR", "/home/brightmindscampus/")

    # Extract alias from email (part before @)
    alias = email.split("@")[0] if "@" in email else email
    
    if not all([host, user, password]):
        logger.warning("[SFTP] Credentials not configured. Skipping upload.")
        return None, None
    
    try:
        transport = paramiko.Transport((host, port))
        # Set short timeouts to fail fast if blocked
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
            except Exception as mkdir_err:
                logger.warning(f"[SFTP] Could not create/chdir to {remote_dir}: {mkdir_err}")

        # Create alias folder (from reference script structure)
        alias_dir = f"{remote_dir.rstrip('/')}/{alias}"
        try:
            sftp.mkdir(alias_dir)
        except IOError:
            pass  # Directory probably exists
            
        # Define filename (matching reference script format)
        filename = f"{email}_authenticator_secret_key.txt"
        remote_path = f"{alias_dir}/{filename}"

        # Write secret to file
        with sftp.open(remote_path, 'w') as f:
            f.write(secret_key)
        
        logger.info(f"[SFTP] Secret uploaded to {host}:{remote_path}")
        sftp.close()
        transport.close()
        
        return host, remote_path

    except Exception as e:
        logger.error(f"[SFTP] Failed to upload secret: {e}")
        # Do NOT log full traceback for timeouts to keep logs clean
        return None, None


# =====================================================================
# S3 upload for App Passwords (REMOVED)
# =====================================================================
# Function append_app_password_to_s3 removed to prevent race conditions.
# We now use DynamoDB for reliable, atomic storage.



# =====================================================================
# Step 1: Login + optional existing 2FA handling
# =====================================================================


def handle_post_login_pages(driver, max_attempts=20):
    """
    Handle all intermediate pages after login (Speedbump, verification prompts, etc.)
    before reaching myaccount.google.com
    Returns (success: bool, error_code: str|None, error_message: str|None)
    """
    logger.info("[STEP] Handling post-login pages (Speedbump, verification, etc.)")
    
    for attempt in range(max_attempts):
        time.sleep(3)  # Wait between checks
        
        try:
            current_url = driver.current_url
            logger.info(f"[STEP] Post-login check {attempt + 1}/{max_attempts}: URL = {current_url}")
            
            # Check if we've reached myaccount
            if "myaccount.google.com" in current_url:
                logger.info("[STEP] Successfully reached myaccount.google.com")
                return True, None, None
            
            # Handle Speedbump page (especially gaplustos - Google Terms of Service)
            if "speedbump" in current_url:
                logger.info(f"[STEP] Speedbump page detected: {current_url}")
                
                # Check if it's the NEW Workspace Terms of Service page (added Jan 2026)
                if "speedbump/workspacetermsofservice" in current_url:
                    logger.info("[STEP] Workspace Terms of Service speedbump detected (NEW Page)...")
                    try:
                        # Try the specific XPath first
                        workspace_tos_xpath = "/html/body/div[2]/div[1]/div[1]/div[2]/c-wiz/main/div[3]/div/div/div/div/button"
                        if element_exists(driver, workspace_tos_xpath, timeout=3):
                            click_xpath(driver, workspace_tos_xpath, timeout=5)
                            logger.info("[STEP] Clicked 'I understand' button on Workspace TOS page via specific XPath")
                            time.sleep(2)
                            continue  # Go to next iteration of main loop
                        
                        # Fallback: Look for button with "I understand" text
                        understand_button_xpaths = [
                            "//button[contains(., 'I understand')]",
                            "//button[contains(., 'understand')]",
                            "//button[contains(., 'I understand')]",
                            "//button[contains(., 'understand')]",
                            "//span[contains(text(), 'I understand')]/ancestor::button",
                            "//div[@role='button' and contains(., 'I understand')]",
                            "//*[text()='I understand']",
                        ]
                        
                        clicked = False
                        for xpath in understand_button_xpaths:
                            if element_exists(driver, xpath, timeout=2):
                                click_xpath(driver, xpath, timeout=5)
                                logger.info(f"[STEP] Clicked 'I understand' button via fallback: {xpath}")
                                time.sleep(2)
                                clicked = True
                                break  # Exit the for loop
                        
                        if clicked:
                            continue  # Go to next iteration of main loop
                        
                        logger.warning("[STEP] Could not find 'I understand' button on Workspace TOS page")
                    except Exception as e:
                        logger.warning(f"[STEP] Failed to click Workspace TOS button: {e}")
                
                # Check if it's the gaplustos page specifically
                if "speedbump/gaplustos" in current_url:
                    logger.info("[STEP] Google+ TOS speedbump detected, using JavaScript click...")
                    try:
                        # Use JavaScript to click the confirm button (more reliable)
                        driver.execute_script("document.querySelector('#confirm').click()")
                        logger.info("[STEP] Clicked #confirm button via JavaScript")
                        time.sleep(2)
                        continue  # Go to next iteration
                    except Exception as e:
                        logger.warning(f"[STEP] JavaScript click failed: {e}")
                
                # Generic speedbump or fallback handling
                logger.info("[STEP] Attempting to click speedbump/confirmation buttons...")
                
                # Try multiple button selectors for Continue/Next/Confirm
                continue_button_xpaths = [
                    "//button[@id='confirm']",
                    "//button[contains(., 'Continue')]",
                    "//button[contains(., 'Next')]",
                    "//button[contains(., 'I agree')]",
                    "//span[contains(text(), 'Continue')]/ancestor::button",
                    "//span[contains(text(), 'Next')]/ancestor::button",
                    "//div[@role='button' and contains(., 'Continue')]",
                    "//div[@role='button' and contains(., 'Next')]",
                ]
                
                clicked = False
                for xpath in continue_button_xpaths:
                    try:
                        if element_exists(driver, xpath, timeout=2):
                            click_xpath(driver, xpath, timeout=5)
                            logger.info(f"[STEP] Clicked Continue/Next button using: {xpath}")
                            clicked = True
                            time.sleep(2)
                            break
                    except Exception as e:
                        logger.debug(f"[STEP] Could not click button with xpath {xpath}: {e}")
                        continue
                
                if not clicked:
                    logger.warning("[STEP] Could not find Continue/Next button, checking for 'Don't now' button")
                    # Try "Don't now" or "Not now" or "Skip"
                    skip_button_xpaths = [
                        "//button[contains(., \"Don't now\")]",
                        "//button[contains(., 'Not now')]",
                        "//button[contains(., 'Skip')]",
                        "//span[contains(text(), \"Don't now\")]/ancestor::button",
                        "//span[contains(text(), 'Not now')]/ancestor::button",
                        "//span[contains(text(), 'Skip')]/ancestor::button",
                    ]
                    
                    for xpath in skip_button_xpaths:
                        try:
                            if element_exists(driver, xpath, timeout=2):
                                click_xpath(driver, xpath, timeout=5)
                                logger.info(f"[STEP] Clicked Skip/Don't now button using: {xpath}")
                                time.sleep(2)
                                break
                        except Exception as e:
                            logger.debug(f"[STEP] Could not click skip button with xpath {xpath}: {e}")
                            continue
                
                continue  # Go to next iteration to check new page
            
            # Handle "Verify it's you" or recovery info pages
            if "verify" in current_url.lower() or element_exists(driver, "//h1[contains(., 'Verify')]", timeout=2):
                logger.info("[STEP] Verification page detected")
                
                # Try to click Continue/Next/Skip
                verify_button_xpaths = [
                    "//button[contains(., 'Continue')]",
                    "//button[contains(., 'Next')]",
                    "//button[contains(., 'Skip')]",
                    "//button[contains(., 'Not now')]",
                    "//span[contains(text(), 'Continue')]/ancestor::button",
                    "//span[contains(text(), 'Next')]/ancestor::button",
                ]
                
                for xpath in verify_button_xpaths:
                    try:
                        if element_exists(driver, xpath, timeout=2):
                            click_xpath(driver, xpath, timeout=5)
                            logger.info(f"[STEP] Clicked button on verification page: {xpath}")
                            time.sleep(2)
                            break
                    except Exception as e:
                        logger.debug(f"[STEP] Could not click verification button with xpath {xpath}: {e}")
                        continue
                
                continue
            
            # Handle "Review your account info" or similar pages
            if element_exists(driver, "//h1[contains(., 'Review')]", timeout=2):
                logger.info("[STEP] Review page detected")
                
                review_button_xpaths = [
                    "//button[contains(., 'Done')]",
                    "//button[contains(., 'Continue')]",
                    "//button[contains(., 'I agree')]",
                    "//span[contains(text(), 'Done')]/ancestor::button",
                ]
                
                for xpath in review_button_xpaths:
                    try:
                        if element_exists(driver, xpath, timeout=2):
                            click_xpath(driver, xpath, timeout=5)
                            logger.info(f"[STEP] Clicked button on review page: {xpath}")
                            time.sleep(2)
                            break
                    except Exception as e:
                        logger.debug(f"[STEP] Could not click review button with xpath {xpath}: {e}")
                        continue
                
                continue
            
            # Generic prompt handling - look for any Continue/Next/Done/Skip buttons
            generic_button_xpaths = [
                "//button[contains(., 'Continue')]",
                "//button[contains(., 'Next')]",
                "//button[contains(., 'Done')]",
                "//button[contains(., 'Skip')]",
                "//button[contains(., 'Not now')]",
                "//button[contains(., 'I agree')]",
            ]
            
            for xpath in generic_button_xpaths:
                try:
                    if element_exists(driver, xpath, timeout=2):
                        click_xpath(driver, xpath, timeout=5)
                        logger.info(f"[STEP] Clicked generic button: {xpath}")
                        time.sleep(2)
                        break  # Found and clicked a button, check new page
                except Exception as e:
                    logger.debug(f"[STEP] Could not click generic button with xpath {xpath}: {e}")
                    continue
            
            # If we're still not at myaccount after trying all buttons, try direct navigation
            if attempt >= max_attempts - 3:  # Last 3 attempts
                logger.warning(f"[STEP] Stuck on intermediate page, attempting direct navigation (attempt {attempt + 1})")
                try:
                    driver.get("https://myaccount.google.com/")
                    time.sleep(3)
                except Exception as e:
                    logger.error(f"[STEP] Direct navigation failed: {e}")
        
        except Exception as e:
            logger.error(f"[STEP] Error handling post-login pages: {e}")
            logger.error(traceback.format_exc())
    
    # If we've exhausted all attempts
    current_url = driver.current_url
    logger.error(f"[STEP] Failed to reach myaccount.google.com after {max_attempts} attempts. Last URL: {current_url}")
    return False, "POST_LOGIN_TIMEOUT", f"Could not bypass intermediate pages. Last URL: {current_url}"


def login_google(driver, email, password, known_totp_secret=None):
    """
    Login to Google. If a 2FA code is requested and we know a TOTP secret,
    we will try to solve it; otherwise we fail with an explicit error.
    
    Enhanced to handle challenge/pwd and other intermediate pages.
    
    CAPTCHA solving happens only once per user - tracked via flag.
    """
    logger.info(f"[STEP] Login started for {email}")
    
    # Flag to ensure CAPTCHA solving happens only once per user
    captcha_solved = False
    
    # Don't check driver health before navigation - it can cause crashes in Lambda
    # Just proceed directly to navigation
    
    # Navigate with timeout and error handling
    try:
        logger.info("[STEP] Navigating to Google login page (English)...")
        # Ensure hl=en is always present in URL
        login_url = "https://accounts.google.com/signin/v2/identifier?hl=en&flowName=GlifWebSignIn"
        driver.get(login_url)
        logger.info("[STEP] Navigation to Google login page completed")
        
        # Add random delay to simulate human behavior
        add_random_delays()
        
        # Inject additional anti-detection scripts after page load
        inject_randomized_javascript(driver)
        
        # Perform random scroll and mouse movements
        random_scroll_and_mouse_move(driver)
        
        time.sleep(1)  # Additional wait for page to stabilize
        logger.info("[STEP] Page stabilized, proceeding with login")
        
        # NOTE: CAPTCHA check removed from here - CAPTCHA rarely appears before email entry
        # CAPTCHA typically appears after email submission, so we'll check after that
    except Exception as nav_error:
        logger.error(f"[STEP] Navigation failed: {nav_error}")
        logger.error(traceback.format_exc())
        return False, "navigation_failed", str(nav_error)

    try:
        # ========== STEP 1: Find and Enter Email (with retry logic) ==========
        email_submission_success = False
        max_email_retries = 3
        
        for email_attempt in range(max_email_retries):
            try:
                logger.info(f"[STEP] Email submission attempt {email_attempt + 1}/{max_email_retries}")
                
                # 1. Find email input
                logger.info("[STEP] Locating email input field...")
                email_input = None
                email_input_xpaths = [
                    "//input[@id='identifierId']",
                    "//input[@type='email']",
                    "//input[@name='identifier']"
                ]
                
                for xpath in email_input_xpaths:
                    if element_exists(driver, xpath, timeout=15):
                        email_input = wait_for_xpath(driver, xpath, timeout=15)
                        break
                
                if not email_input:
                    logger.warning(f"[STEP] Email input not found on attempt {email_attempt + 1}")
                    if email_attempt < max_email_retries - 1:
                        driver.refresh()
                        time.sleep(3)
                        continue
                    else:
                        raise Exception("Email input field not found")

                # 2. Human-like interaction
                random_scroll_and_mouse_move(driver)
                time.sleep(random.uniform(0.5, 1.0))
                
                # 3. Clear and Type
                logger.info("[STEP] Clearing and typing email...")
                try:
                    email_input.clear()
                except:
                    pass # Ignore if clear fails
                    
                simulate_human_typing(email_input, email, driver)
                logger.info("[STEP] Email entered with human-like typing")
                time.sleep(random.uniform(0.5, 1.0))
                
                # 4. Find and Click Next Button (Explicit Click with Retry)
                logger.info("[STEP] Locating 'Next' button...")
                next_button = None
                next_button_xpaths = [
                    "//div[@id='identifierNext']//button",
                    "//button[span[text()='Next']]",
                    "//button[contains(., 'Next')]",
                    "//div[@id='identifierNext']"
                ]
                
                for xpath in next_button_xpaths:
                    try:
                        if element_exists(driver, xpath, timeout=3):
                            next_button = wait_for_clickable_xpath(driver, xpath, timeout=3)
                            if next_button:
                                logger.info(f"[STEP] Found Next button with xpath: {xpath}")
                                break
                    except:
                        continue
                
                if next_button:
                    # Retry clicking logic
                    click_success = False
                    for click_attempt in range(3):
                        logger.info(f"[STEP] Clicking 'Next' button (Attempt {click_attempt + 1})...")
                        try:
                            if click_attempt == 0:
                                next_button.click()
                            elif click_attempt == 1:
                                driver.execute_script("arguments[0].click();", next_button)
                            else:
                                ActionChains(driver).move_to_element(next_button).click().perform()
                            
                            # Wait and check for transition
                            time.sleep(3)
                            
                            # Check if we moved away from identifier page or if password field appeared
                            current_url_check = driver.current_url
                            if "challenge/pwd" in current_url_check or "password" in driver.page_source.lower():
                                logger.info("[STEP] ✓ Transitioned to password page/challenge")
                                click_success = True
                                break
                                
                            # Check for specific error before retrying click
                            error_xpath = "//*[contains(text(), 'find your Google Account') or contains(text(), 'Enter a valid email')]"
                            if element_exists(driver, error_xpath, timeout=1):
                                # Error found, no need to retry click, let the outer loop handle it
                                break
                            
                            # Check for Secure Browser Block
                            secure_browser_xpath = "//*[contains(text(), 'This browser or app may not be secure')]"
                            if element_exists(driver, secure_browser_xpath, timeout=1):
                                logger.error("[STEP] ✗ Secure Browser Block detected!")
                                return False, "SECURE_BROWSER_BLOCK", "Secure Browser Block detected"
                                
                            logger.warning("[STEP] Still on email page after click, retrying...")
                            
                        except Exception as click_err:
                            logger.warning(f"[STEP] Click attempt {click_attempt + 1} failed: {click_err}")
                            time.sleep(1)
                    
                    if not click_success:
                         logger.warning("[STEP] Failed to transition after multiple click attempts")
                else:
                    # Fallback to Enter key if button not found (but log it)
                    logger.warning("[STEP] 'Next' button not found, falling back to Enter key")
                    email_input.send_keys(Keys.RETURN)
                
                # 5. Wait for transition
                logger.info("[STEP] Waiting for page transition...")
                time.sleep(random.uniform(2, 3.5))
                
                # Add human-like behavior during wait
                random_scroll_and_mouse_move(driver)
                
                email_submission_success = True
                break
                
            except Exception as e:
                logger.warning(f"[STEP] Email submission attempt {email_attempt + 1} failed: {e}")
                if email_attempt < max_email_retries - 1:
                    driver.refresh()
                    time.sleep(3)
        
        if not email_submission_success:
            raise Exception("Failed to submit email after retries")
        
        # Check if we're still on the identifier page (email submission failed or CAPTCHA appeared)
        current_url = driver.current_url
        page_title = driver.title
        logger.info(f"[STEP] After email submission - URL: {current_url[:100]}..., Title: {page_title}")
        
        # Check for CAPTCHA after email submission (this is when it typically appears)
        # Only solve CAPTCHA once per user
        
        # CRITICAL: Check for "Couldn't find your Google Account" error BEFORE CAPTCHA check
        # This prevents false CAPTCHA detection or timeouts on invalid accounts
        try:
            # Robust XPath ignoring specific quote types (curly vs straight)
            # Matches "Couldn't find your Google Account" or "Enter a valid email"
            error_xpath = "//*[contains(text(), 'find your Google Account') or contains(text(), 'Enter a valid email')]"
            if element_exists(driver, error_xpath, timeout=3):
                error_element = driver.find_element(By.XPATH, error_xpath)
                error_text = error_element.text
                logger.error(f"[STEP] ✗ Login Error: {error_text}")
                return False, "EMAIL_ERROR", f"Google rejected email: {error_text}"
        except Exception as e:
            logger.warning(f"[STEP] Error checking failed: {e}")

        if not captcha_solved and detect_captcha(driver, email=email):
            logger.warning("[STEP] ⚠️ CAPTCHA detected after email submission!")
            
            # Try to solve CAPTCHA using 2Captcha if enabled (only once)
            solved, solve_error = solve_captcha_with_2captcha(driver, email=email)
            captcha_solved = True  # Mark as solved to prevent retry
            
            if solved:
                logger.info("[STEP] ✓✓✓ CAPTCHA solved using 2Captcha! Waiting for page to process and redirect...")
                # Wait longer for Google to process the token and potentially redirect
                time.sleep(5)
                
                # Check current URL to see if page redirected after CAPTCHA solve
                current_url_after = driver.current_url
                logger.info(f"[STEP] URL after CAPTCHA solve: {current_url_after[:100]}...")
                
                # If redirected to password page, we're good - skip email retry
                if "challenge/pwd" in current_url_after or "signin/challenge/pwd" in current_url_after:
                    logger.info("[STEP] ✓ Page redirected to password page after CAPTCHA solve! Proceeding to password entry...")
                    # Skip email retry - we're already on password page
                elif "myaccount.google.com" in current_url_after:
                    logger.info("[STEP] ✓ Already logged in after CAPTCHA solve!")
                    return True, None, None
                elif '/signin/identifier' in current_url_after or 'identifier' in current_url_after.lower():
                    logger.info("[STEP] Still on identifier page after CAPTCHA solve. Retrying email submission with solved CAPTCHA...")
                    try:
                        # Wait for page to be ready
                        time.sleep(2)
                        
                        # Find email input again
                        email_input_retry = wait_for_xpath(driver, "//input[@id='identifierId']", timeout=10)
                        if email_input_retry:
                            # Clear and re-enter email
                            email_input_retry.clear()
                            time.sleep(0.5)
                            simulate_human_typing(email_input_retry, email, driver)
                            logger.info("[STEP] Re-entered email after CAPTCHA solve")
                            time.sleep(1)
                            
                            # Submit email using Enter key (CAPTCHA token should now be set)
                            email_input_retry.send_keys(Keys.RETURN)
                            logger.info("[STEP] Retried email submission after CAPTCHA solve using Enter key")
                            time.sleep(5)  # Increased wait time for redirect after CAPTCHA solve
                    except Exception as retry_err:
                        logger.warning(f"[STEP] Could not retry email submission after CAPTCHA solve: {retry_err}")
                        # Continue anyway - token might already be processed
                
                # Check if CAPTCHA is still present (should be gone if solved correctly)
                # Note: We don't retry CAPTCHA solving - it's already been solved once
                if detect_captcha(driver, email=email):
                    logger.warning("[STEP] ⚠️ CAPTCHA still present after solving attempt. This may be a different CAPTCHA or page issue.")
                    # Don't retry - CAPTCHA solving happens only once per user
                    time.sleep(2)  # Wait briefly
                else:
                    logger.info("[STEP] ✓ CAPTCHA cleared after solving! Proceeding...")
            else:
                # CAPTCHA solving failed or not enabled
                logger.error(f"[STEP] ✗✗✗ CAPTCHA BLOCKING LOGIN - Solving failed: {solve_error}")
                # Check for CAPTCHA text in page
                try:
                    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                    if any(indicator in page_text for indicator in ['type the text you hear or see', 'verify you\'re not a robot', 'unusual traffic']):
                        logger.error("[STEP] ✗✗✗ CAPTCHA BLOCKING LOGIN - Page text confirms CAPTCHA presence")
                        return False, "CAPTCHA_DETECTED", f"CAPTCHA detected after email submission. 2Captcha solving failed: {solve_error}"
                except:
                    pass
                return False, "CAPTCHA_DETECTED", f"CAPTCHA detected after email submission. 2Captcha solving failed: {solve_error}"
        
        # Check if we're still on identifier page (email might not have been submitted or page redirected back)
        if '/signin/identifier' in current_url or 'identifier' in current_url.lower():
            logger.warning("[STEP] ⚠️ Still on identifier page after email submission - checking for issues...")
            
            # IMPORTANT: Wait for page to FULLY stabilize before checking for errors
            # This prevents false positives during parallel processing
            time.sleep(3)
            
            # Re-check current URL after wait (page might have transitioned)
            current_url_recheck = driver.current_url
            if "challenge/pwd" in current_url_recheck or "myaccount.google.com" in current_url_recheck:
                logger.info("[STEP] ✓ Page transitioned to password/account page during wait. Proceeding...")
                # Don't check for errors - page has moved on
            else:
                # Check for "Account not found" error - FATAL ERROR
                # Double-check to avoid false positives
                account_not_found = False
                error_text = ""
                
                try:
                    # Method 1: Check page source
                    page_source = driver.page_source
                    if "Couldn't find your Google Account" in page_source:
                        error_text = "Couldn't find your Google Account"
                        account_not_found = True
                    elif "Enter a valid email" in page_source:
                        error_text = "Enter a valid email or phone number"
                        account_not_found = True
                except:
                    pass
                
                # Method 2: Check for visible error element
                if not account_not_found:
                    if element_exists(driver, "//*[contains(text(), 'find your Google Account')]", timeout=2):
                        error_text = "Couldn't find your Google Account (element detected)"
                        account_not_found = True
                    elif element_exists(driver, "//*[contains(text(), 'Enter a valid email')]", timeout=2):
                        error_text = "Enter a valid email (element detected)"
                        account_not_found = True
                
                if account_not_found:
                    # DOUBLE CHECK: Wait a moment and verify error is still there
                    time.sleep(2)
                    still_on_identifier = '/signin/identifier' in driver.current_url or 'identifier' in driver.current_url.lower()
                    error_still_visible = (
                        "Couldn't find your Google Account" in driver.page_source or
                        "Enter a valid email" in driver.page_source or
                        element_exists(driver, "//*[contains(text(), 'find your Google Account')]", timeout=1) or
                        element_exists(driver, "//*[contains(text(), 'Enter a valid email')]", timeout=1)
                    )
                    
                    if still_on_identifier and error_still_visible:
                        logger.error(f"[STEP] ✗ Login Error (CONFIRMED): {error_text}")
                        return False, "ACCOUNT_NOT_FOUND", error_text
                    else:
                        logger.info("[STEP] Error was transient, page has moved on. Continuing...")
                    
                # Check for "Secure Browser" error - RETRY WITH NEW BROWSER
                if "This browser or app may not be secure" in driver.page_source or \
                   element_exists(driver, "//*[contains(text(), 'This browser or app may not be secure')]"):
                    logger.error("[STEP] ✗ Login Error: Secure Browser Block detected")
                    return False, "SECURE_BROWSER_BLOCK", "Secure Browser Block detected"

            # Check for image CAPTCHA input field (Google's own CAPTCHA, not reCAPTCHA)
            # This appears as: <input type="text" name="ca" id="ca" aria-label="Type the text you hear or see">
            try:
                image_captcha_input = driver.find_elements(By.XPATH, "//input[@name='ca' or @id='ca']")
                captcha_detected = False
                if image_captcha_input:
                    for inp in image_captcha_input:
                        if inp.is_displayed():
                            aria_label = inp.get_attribute('aria-label') or ''
                            if 'hear or see' in aria_label.lower() or 'captcha' in aria_label.lower():
                                captcha_detected = True
                                break
                
                if captcha_detected:
                    logger.warning("[STEP] ⚠️ Image CAPTCHA detected (input field visible)")
                    
                    # If 2Captcha is enabled, try to solve it
                    twocaptcha_config = get_twocaptcha_config()
                    if twocaptcha_config.get('enabled'):
                        # Retry loop for CAPTCHA solving
                        max_captcha_retries = 3
                        for captcha_attempt in range(max_captcha_retries):
                            try:
                                if captcha_attempt > 0:
                                    logger.info(f"[STEP] Retrying CAPTCHA solve (Attempt {captcha_attempt + 1}/{max_captcha_retries})...")
                                
                                logger.info("[STEP] Attempting to solve Google Image CAPTCHA using 2Captcha Image CAPTCHA solver...")
                                
                                # Capture screenshot for analysis
                                capture_captcha_screenshot(driver, captcha_type="image", email=email)
                                
                                # Solve the image CAPTCHA using 2Captcha
                                solved, error = solve_captcha_with_2captcha(driver, email=email)
                                
                                if solved:
                                    logger.info("[STEP] ✓ Image CAPTCHA solved successfully! Waiting for page response...")
                                    # Wait for Google to process the solution
                                    time.sleep(5)
                                    
                                    # CHECK FOR REJECTION ("Please enter the characters you see")
                                    rejection_xpath = "//*[contains(text(), 'enter the characters you see') or contains(text(), 'characters you entered')]"
                                    if element_exists(driver, rejection_xpath, timeout=2):
                                        logger.warning("[STEP] ✗ CAPTCHA solution rejected by Google (Incorrect solution). Retrying...")
                                        continue # Retry the loop
                                    
                                    # Check if page redirected after CAPTCHA solve
                                    current_url_after_captcha = driver.current_url
                                    logger.info(f"[STEP] URL after image CAPTCHA solve: {current_url_after_captcha[:100]}...")
                                    
                                    # If redirected to password page, we're good
                                    if "challenge/pwd" in current_url_after_captcha or "signin/challenge/pwd" in current_url_after_captcha:
                                        logger.info("[STEP] ✓ Page redirected to password page after image CAPTCHA solve!")
                                        break # Success, exit loop
                                    elif "myaccount.google.com" in current_url_after_captcha:
                                        logger.info("[STEP] ✓ Already logged in after image CAPTCHA solve!")
                                        return True, None, None
                                    elif '/signin/identifier' in current_url_after_captcha or 'identifier' in current_url_after_captcha.lower():
                                        logger.warning("[STEP] Still on identifier page after CAPTCHA solve. Attempting to click Next again...")
                                        
                                        # Retry clicking Next button
                                        next_button_xpaths = [
                                            "//div[@id='identifierNext']//button",
                                            "//button[span[text()='Next']]",
                                            "//button[contains(., 'Next')]",
                                            "//div[@id='identifierNext']"
                                        ]
                                        
                                        next_clicked = False
                                        for xpath in next_button_xpaths:
                                            try:
                                                if element_exists(driver, xpath, timeout=3):
                                                    btn = wait_for_clickable_xpath(driver, xpath, timeout=3)
                                                    if btn:
                                                        logger.info(f"[STEP] Found Next button for retry: {xpath}")
                                                        driver.execute_script("arguments[0].click();", btn)
                                                        next_clicked = True
                                                        break
                                            except:
                                                continue
                                        
                                        if next_clicked:
                                            logger.info("[STEP] Clicked Next button again. Waiting for transition...")
                                            time.sleep(5)
                                            
                                            # Check for rejection AGAIN after clicking Next
                                            if element_exists(driver, rejection_xpath, timeout=2):
                                                logger.warning("[STEP] ✗ CAPTCHA solution rejected after retry click. Retrying...")
                                                continue
                                            
                                            # Check URL again
                                            current_url_retry = driver.current_url
                                            if "challenge/pwd" in current_url_retry or "signin/challenge/pwd" in current_url_retry:
                                                logger.info("[STEP] ✓ Page redirected to password page after retry click!")
                                                break # Success
                                            else:
                                                logger.warning(f"[STEP] Still on identifier page after retry click. URL: {current_url_retry[:50]}...")
                                        else:
                                            logger.warning("[STEP] Could not find Next button for retry.")
                                else:
                                    logger.error(f"[STEP] ✗ Failed to solve Image CAPTCHA: {error}")
                                    
                            except Exception as captcha_err:
                                logger.error(f"[STEP] Error in CAPTCHA loop: {captcha_err}")
                        
                        # End of CAPTCHA loop
                    else:
                        logger.info("[STEP] 2Captcha not enabled, cannot solve CAPTCHA")
            except Exception as image_captcha_check:
                logger.debug(f"[STEP] Image CAPTCHA check error: {image_captcha_check}")
        
        # Wait longer for password field to appear (Google may be processing CAPTCHA or redirecting)
        # After CAPTCHA solve, page might redirect - wait longer and check for redirects
        wait_time = random.uniform(5, 8)  # Increased wait time after CAPTCHA
        logger.info(f"[STEP] Waiting {wait_time:.1f}s for password field to appear (after CAPTCHA solve)...")
        time.sleep(wait_time)
        
        # Check if page redirected to password page or account page
        current_url = driver.current_url
        if "challenge/pwd" in current_url or "signin/challenge/pwd" in current_url:
            logger.info("[STEP] ✓ Page redirected to password challenge page after CAPTCHA!")
        elif "myaccount.google.com" in current_url:
            logger.info("[STEP] ✓ Already logged in - no password needed!")
            return True, None, None
        elif "challenge/totp" in current_url:
            logger.info("[STEP] Page redirected to TOTP challenge - will handle in post-login")
        
        # Check for iframes first (Google sometimes uses iframes for password field)
        password_input = None
        try:
            # Primary method: Use By.NAME like reference function (most reliable)
            logger.info("[STEP] Trying to find password input using By.NAME='Passwd' (primary method)")
            password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=15)  # Increased timeout
            if password_input:
                logger.info("[STEP] Found password input using By.NAME='Passwd'")
        except Exception as primary_err:
            logger.warning(f"[STEP] Primary method failed: {primary_err}")
        
        if not password_input:
            # Fallback: Try XPath methods (fixed invalid XPath syntax)
            password_input_xpaths = [
                "//input[@name='Passwd']",
                "//input[@type='password']",
                "/html/body/div[2]/div[1]/div[1]/div[2]/c-wiz/main/div[2]/div/div/div/form/span/section[2]/div/div/div[1]/div[1]/div/div/div/div/div[1]/div/div[1]/input",  # User-provided working XPath
                "//input[@id='password']",
                "//input[@name='password']",
                "//input[contains(@aria-label, 'password')]",
                "//input[contains(@aria-label, 'Password')]",
            ]
            
            # Try to find visible and interactable password field
            for xpath in password_input_xpaths:
                try:
                    logger.info(f"[STEP] Trying to find password input with XPath: {xpath}")
                    password_input = wait_for_visible_and_interactable(driver, xpath, timeout=8)
                    if password_input:
                        logger.info(f"[STEP] Found password input using xpath: {xpath}")
                        break
                except Exception as e:
                    logger.warning(f"[STEP] Failed to find password with {xpath}: {e}")
                    continue
        
        # If not found in main document, check iframes (prioritize Google's bscframe)
        if not password_input:
            logger.info("[STEP] Password field not found in main document, checking iframes...")
            
            # Wait a bit more for iframes to load after CAPTCHA solve/redirect
            time.sleep(2)
            
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            logger.info(f"[STEP] Found {len(iframes)} iframe(s) to check")
            
            # First, try the bscframe iframe (Google's security frame where password field often appears)
            bscframe_found = False
            for iframe in iframes:
                try:
                    iframe_src = iframe.get_attribute('src') or ''
                    if '_/bscframe' in iframe_src:
                        logger.info(f"[STEP] Found bscframe iframe, checking for password field...")
                        driver.switch_to.frame(iframe)
                        # Try By.NAME first (most reliable)
                        try:
                            password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=8)  # Increased timeout after CAPTCHA
                            if password_input:
                                logger.info("[STEP] ✓ Found password input in bscframe iframe!")
                                bscframe_found = True
                                break
                        except:
                            pass
                        # Fallback to XPath
                        if not password_input:
                            for xpath in password_input_xpaths:
                                try:
                                    password_input = wait_for_visible_and_interactable(driver, xpath, timeout=5)  # Increased timeout
                                    if password_input:
                                        logger.info(f"[STEP] ✓ Found password input in bscframe iframe using: {xpath[:50]}...")
                                        bscframe_found = True
                                        break
                                except:
                                    continue
                        driver.switch_to.default_content()
                        if bscframe_found:
                            break
                except Exception as bsc_err:
                    logger.debug(f"[STEP] Error checking bscframe: {bsc_err}")
                    driver.switch_to.default_content()
                    continue
            
            # If still not found, check all other iframes
            if not password_input:
                for iframe in iframes:
                    try:
                        iframe_src = iframe.get_attribute('src') or ''
                        # Skip bscframe (already checked) and YouTube check connection iframes
                        if '_/bscframe' in iframe_src or 'youtube.com' in iframe_src:
                            continue
                        driver.switch_to.frame(iframe)
                        for xpath in password_input_xpaths:
                            try:
                                password_input = wait_for_visible_and_interactable(driver, xpath, timeout=3)
                                if password_input:
                                    logger.info(f"[STEP] Found password input in iframe using xpath: {xpath[:50]}...")
                                    break
                            except:
                                continue
                        if password_input:
                            break
                        driver.switch_to.default_content()
                    except Exception as iframe_err:
                        logger.debug(f"[STEP] Error checking iframe: {iframe_err}")
                        driver.switch_to.default_content()
                        continue
            
        if not password_input:
            # Last resort: try JavaScript to find and interact with password field
            logger.info("[STEP] Trying JavaScript method to find password field...")
            try:
                password_input = driver.execute_script("""
                    var inputs = document.querySelectorAll('input[type="password"], input[name="Passwd"], input[name="password"]');
                    for (var i = 0; i < inputs.length; i++) {
                        var input = inputs[i];
                        if (input.offsetParent !== null) { // Check if visible
                            input.scrollIntoView({behavior: 'smooth', block: 'center'});
                            input.focus();
                            return input;
                        }
                    }
                    return null;
                """)
                if password_input:
                    logger.info("[STEP] Found password input using JavaScript")
            except Exception as js_err:
                logger.error(f"[STEP] JavaScript method failed: {js_err}")
        
        if not password_input:
                # DEBUG: Capture what's actually on the page when password field is not found
                logger.error("=" * 80)
                logger.error("[DEBUG] Password field not found - capturing page state for diagnosis")
                logger.error("=" * 80)
                
                # ALWAYS log page source to CloudWatch (even if S3 fails)
                try:
                    page_source = driver.page_source
                    current_url = driver.current_url
                    page_title = driver.title
                    
                    # Log critical information to CloudWatch
                    logger.error(f"[DEBUG] Current URL: {current_url}")
                    logger.error(f"[DEBUG] Page title: {page_title}")
                    
                    # Check for CAPTCHA indicators in page source
                    captcha_indicators = [
                        'recaptcha', 'g-recaptcha', 'captcha', 'challenge', 
                        'unusual traffic', 'automated queries', 'verify you\'re not a robot'
                    ]
                    found_indicators = []
                    page_source_lower = page_source.lower()
                    for indicator in captcha_indicators:
                        if indicator in page_source_lower:
                            found_indicators.append(indicator)
                    
                    if found_indicators:
                        logger.error(f"[DEBUG] ⚠️ CAPTCHA/BLOCKER DETECTED! Found indicators: {found_indicators}")
                    
                    # Log first 5000 chars of page source to CloudWatch
                    logger.error(f"[DEBUG] Page source (first 5000 chars): {page_source[:5000]}")
                    
                    # Try to extract visible text
                    try:
                        page_text = driver.find_element(By.TAG_NAME, "body").text
                        logger.error(f"[DEBUG] Visible page text (first 2000 chars): {page_text[:2000]}")
                    except:
                        logger.error("[DEBUG] Could not extract page text")
                    
                except Exception as log_err:
                    logger.error(f"[DEBUG] Failed to log page state: {log_err}")
                
                # Save screenshot and page source to S3 for investigation (optional)
                screenshot_saved = False
                page_source_saved = False
                try:
                    s3_bucket = os.environ.get("S3_DEBUG_BUCKET", "dev-debug-screenshots")
                    
                    # Ensure bucket exists
                    s3_region = os.environ.get("AWS_REGION", "us-east-1")
                    ensure_s3_bucket_exists(s3_bucket, s3_region)
                    
                    timestamp = int(time.time())
                    email_safe = email.replace("@", "_at_").replace(".", "_")
                    screenshot_key = f"password-field-not-found/{email_safe}_{timestamp}_screenshot.png"
                    page_source_key = f"password-field-not-found/{email_safe}_{timestamp}_page_source.html"
                    
                    # Take screenshot
                    try:
                        screenshot_path = f"/tmp/password_not_found_{timestamp}.png"
                        driver.save_screenshot(screenshot_path)
                        
                        # Upload to S3
                        s3_client = get_s3_client()
                        s3_client.upload_file(
                            screenshot_path,
                            s3_bucket,
                            screenshot_key,
                            ExtraArgs={'ContentType': 'image/png'}
                        )
                        screenshot_saved = True
                        logger.error(f"[DEBUG] ✓ Screenshot saved to S3: s3://{s3_bucket}/{screenshot_key}")
                        
                        # Clean up local file
                        try:
                            os.remove(screenshot_path)
                        except:
                            pass
                    except Exception as screenshot_err:
                        logger.error(f"[DEBUG] ✗ Failed to save screenshot: {screenshot_err}")
                    
                    # Save page source (HTML) to S3
                    try:
                        if 'page_source' not in locals():
                            page_source = driver.page_source
                        page_source_path = f"/tmp/password_not_found_{timestamp}.html"
                        with open(page_source_path, 'w', encoding='utf-8') as f:
                            f.write(page_source)
                        
                        # Upload to S3
                        s3_client = get_s3_client()
                        s3_client.upload_file(
                            page_source_path,
                            s3_bucket,
                            page_source_key,
                            ExtraArgs={'ContentType': 'text/html'}
                        )
                        page_source_saved = True
                        logger.error(f"[DEBUG] ✓ Page source saved to S3: s3://{s3_bucket}/{page_source_key}")
                        
                        # Clean up local file
                        try:
                            os.remove(page_source_path)
                        except:
                            pass
                    except Exception as page_source_err:
                        logger.error(f"[DEBUG] ✗ Failed to save page source to S3: {page_source_err}")
                    
                    if screenshot_saved or page_source_saved:
                        logger.error(f"[DEBUG] Investigation files saved to S3 bucket: {s3_bucket}")
                        logger.error(f"[DEBUG] Check S3 for: {screenshot_key} and {page_source_key}")
                    
                except Exception as s3_err:
                    logger.error(f"[DEBUG] ✗ Error with S3 operations: {s3_err}")
                    logger.error(f"[DEBUG] Note: Page source and debug info are logged above in CloudWatch")
                
                # Additional detailed debugging: All input elements and their attributes
                try:
                    try:
                        all_inputs = driver.find_elements(By.TAG_NAME, "input")
                        logger.error(f"[DEBUG] Found {len(all_inputs)} input element(s) on page:")
                        for i, inp in enumerate(all_inputs[:20]):  # Limit to first 20
                            try:
                                inp_type = inp.get_attribute('type') or 'N/A'
                                inp_name = inp.get_attribute('name') or 'N/A'
                                inp_id = inp.get_attribute('id') or 'N/A'
                                inp_placeholder = inp.get_attribute('placeholder') or 'N/A'
                                inp_aria_label = inp.get_attribute('aria-label') or 'N/A'
                                is_displayed = inp.is_displayed()
                                is_enabled = inp.is_enabled()
                                logger.error(f"[DEBUG]   Input {i+1}: type={inp_type}, name={inp_name}, id={inp_id}, placeholder={inp_placeholder}, aria-label={inp_aria_label}, displayed={is_displayed}, enabled={is_enabled}")
                            except Exception as inp_err:
                                logger.error(f"[DEBUG]   Input {i+1}: Error reading attributes: {inp_err}")
                    except Exception as inputs_err:
                        logger.error(f"[DEBUG] Could not list input elements: {inputs_err}")
                    
                    # 5. Check for error messages or alerts
                    try:
                        error_selectors = [
                            "//*[contains(text(), 'error') or contains(text(), 'Error')]",
                            "//*[contains(text(), 'wrong') or contains(text(), 'Wrong')]",
                            "//*[contains(text(), 'invalid') or contains(text(), 'Invalid')]",
                            "//*[contains(text(), 'try again') or contains(text(), 'Try again')]",
                            "//*[@role='alert']",
                            "//*[contains(@class, 'error')]",
                        ]
                        for selector in error_selectors:
                            try:
                                error_elements = driver.find_elements(By.XPATH, selector)
                                if error_elements:
                                    for err_elem in error_elements[:5]:  # First 5 error elements
                                        try:
                                            err_text = err_elem.text.strip()
                                            if err_text:
                                                logger.error(f"[DEBUG] Error message found: {err_text}")
                                        except:
                                            pass
                            except:
                                pass
                    except Exception as err_check_err:
                        logger.error(f"[DEBUG] Could not check for error messages: {err_check_err}")
                    
                    # 6. Check for CAPTCHA elements
                    try:
                        captcha_indicators = [
                            "//iframe[contains(@src, 'recaptcha')]",
                            "//div[contains(@class, 'recaptcha')]",
                            "//*[contains(text(), 'unusual traffic')]",
                            "//*[contains(text(), 'verify you')]",
                        ]
                        for indicator in captcha_indicators:
                            try:
                                captcha_elements = driver.find_elements(By.XPATH, indicator)
                                if captcha_elements:
                                    logger.error(f"[DEBUG] CAPTCHA indicator found: {indicator}")
                            except:
                                pass
                    except Exception as captcha_check_err:
                        logger.error(f"[DEBUG] Could not check for CAPTCHA: {captcha_check_err}")
                    
                    # 7. Get page source snippet (first 5000 chars)
                    try:
                        page_source = driver.page_source
                        logger.error(f"[DEBUG] Page source snippet (first 5000 chars): {page_source[:5000]}")
                    except Exception as source_err:
                        logger.error(f"[DEBUG] Could not get page source: {source_err}")
                    
                    # 8. List all iframes
                    try:
                        iframes = driver.find_elements(By.TAG_NAME, "iframe")
                        logger.error(f"[DEBUG] Found {len(iframes)} iframe(s) on page")
                        for i, iframe in enumerate(iframes[:10]):  # First 10 iframes
                            try:
                                iframe_src = iframe.get_attribute('src') or 'N/A'
                                iframe_id = iframe.get_attribute('id') or 'N/A'
                                logger.error(f"[DEBUG]   Iframe {i+1}: src={iframe_src[:100]}, id={iframe_id}")
                            except:
                                pass
                    except Exception as iframe_err:
                        logger.error(f"[DEBUG] Could not list iframes: {iframe_err}")
                    
                    # 9. Check for any password-related elements (even if not visible)
                    try:
                        password_elements = driver.find_elements(By.XPATH, "//*[contains(@name, 'pass') or contains(@id, 'pass') or contains(@type, 'password') or contains(@aria-label, 'pass')]")
                        logger.error(f"[DEBUG] Found {len(password_elements)} password-related element(s):")
                        for i, pwd_elem in enumerate(password_elements[:10]):
                            try:
                                pwd_tag = pwd_elem.tag_name
                                pwd_type = pwd_elem.get_attribute('type') or 'N/A'
                                pwd_name = pwd_elem.get_attribute('name') or 'N/A'
                                pwd_id = pwd_elem.get_attribute('id') or 'N/A'
                                pwd_displayed = pwd_elem.is_displayed()
                                logger.error(f"[DEBUG]   Password element {i+1}: tag={pwd_tag}, type={pwd_type}, name={pwd_name}, id={pwd_id}, displayed={pwd_displayed}")
                            except:
                                pass
                    except Exception as pwd_elem_err:
                        logger.error(f"[DEBUG] Could not check password elements: {pwd_elem_err}")
                    
                    logger.error("=" * 80)
                    logger.error("[DEBUG] End of page state capture")
                    logger.error("=" * 80)
                except Exception as debug_err:
                    logger.error(f"[DEBUG] Error during page state capture: {debug_err}")
                    logger.error(traceback.format_exc())
                
                # Before failing, check if this is due to a CAPTCHA we can solve
                # Check for reCAPTCHA iframe (solvable) vs audio CAPTCHA (not solvable with reCAPTCHA API)
                try:
                    recaptcha_iframe = driver.find_elements(By.XPATH, "//iframe[contains(@src, 'recaptcha')]")
                    has_recaptcha = len(recaptcha_iframe) > 0
                    
                    if has_recaptcha:
                        logger.info("[STEP] reCAPTCHA iframe detected - attempting to solve...")
                        solved, solve_error = solve_captcha_with_2captcha(driver)
                        if solved:
                            logger.info("[STEP] ✓ CAPTCHA solved! Retrying password field detection...")
                            time.sleep(3)
                            # Retry password field detection
                            password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=10)
                            if password_input:
                                logger.info("[STEP] ✓ Password field found after CAPTCHA solving!")
                                # Continue with password entry (fall through to next section)
                            else:
                                # Retry password field detection after CAPTCHA solve with longer wait
                                logger.warning("[STEP] Password field not found immediately after CAPTCHA solve, retrying with longer wait...")
                                time.sleep(5)  # Wait longer for page redirect/load
                                
                                # Check URL again - might have redirected
                                current_url_retry = driver.current_url
                                logger.info(f"[STEP] URL after retry wait: {current_url_retry[:100]}...")
                                
                                # Try finding password field again with longer timeout
                                try:
                                    password_input_retry = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=15)
                                    if password_input_retry:
                                        logger.info("[STEP] ✓ Found password field on retry after CAPTCHA solve!")
                                        password_input = password_input_retry
                                    else:
                                        logger.error("[STEP] ✗ Password field still not found after CAPTCHA solving and retry")
                                        return False, "LOGIN_PASSWORD_FIELD_NOT_FOUND", "Password field not found even after CAPTCHA solving and retry"
                                except Exception as retry_err:
                                    logger.error(f"[STEP] ✗ Password field retry failed: {retry_err}")
                                    return False, "LOGIN_PASSWORD_FIELD_NOT_FOUND", f"Password field not found after CAPTCHA solving: {retry_err}"
                        else:
                            logger.warning(f"[STEP] CAPTCHA solving failed: {solve_error}")
                    else:
                        # Check if it's an audio CAPTCHA (not solvable via reCAPTCHA API)
                        # Check for the actual input field: <input type="text" name="ca" id="ca" aria-label="Type the text you hear or see">
                        audio_captcha_input = driver.find_elements(By.XPATH, "//input[@name='ca' or @id='ca']")
                        audio_captcha_text = driver.find_elements(By.XPATH, "//*[contains(text(), 'Type the text you hear or see')]")
                        
                        has_audio_captcha = False
                        for inp in audio_captcha_input:
                            try:
                                if inp.is_displayed():
                                    aria_label = inp.get_attribute('aria-label') or ''
                                    if 'hear or see' in aria_label.lower() or 'captcha' in aria_label.lower():
                                        has_audio_captcha = True
                                        break
                            except:
                                pass
                        
                        if has_audio_captcha or audio_captcha_text:
                            logger.warning("[STEP] Image CAPTCHA detected - attempting to solve using 2Captcha Image CAPTCHA solver...")
                            # Capture screenshot for analysis
                            capture_captcha_screenshot(driver, captcha_type="image", email=email)
                            
                            # Solve the image CAPTCHA using 2Captcha
                            solved, error = solve_captcha_with_2captcha(driver, email=email)
                            
                            if solved:
                                logger.info("[STEP] ✓ Image CAPTCHA solved successfully! Waiting for page to process...")
                                # Reduced wait time - check page state more frequently
                                time.sleep(2)  # Reduced from 5s to 2s
                                
                                # Check if page redirected to password page or TOTP challenge
                                current_url = driver.current_url
                                if "challenge/pwd" in current_url:
                                    logger.info("[STEP] ✓ Page redirected to password page after CAPTCHA solving")
                                    # Retry password field detection with multiple methods and shorter timeouts
                                    password_input = None
                                    
                                    # Try main document first with shorter timeout
                                    password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=8)
                                    if not password_input:
                                        # Try other common XPaths
                                        password_xpaths = [
                                            "//input[@type='password']",
                                            "//input[@name='password']",
                                            "//input[@id='password']",
                                            "//input[contains(@aria-label, 'Password')]"
                                        ]
                                        for xpath in password_xpaths:
                                            try:
                                                password_input = wait_for_password_clickable(driver, By.XPATH, xpath, timeout=3)
                                                if password_input:
                                                    logger.info(f"[STEP] ✓ Password field found via XPath: {xpath}")
                                                    break
                                            except:
                                                continue
                                    
                                    if password_input:
                                        logger.info("[STEP] ✓ Password field found after CAPTCHA solving and redirect!")
                                    else:
                                        # Try iframe method with shorter timeout
                                        logger.info("[STEP] Trying iframe method after CAPTCHA redirect...")
                                        try:
                                            driver.switch_to.default_content()
                                            iframes = driver.find_elements(By.TAG_NAME, "iframe")
                                            for iframe in iframes:
                                                try:
                                                    iframe_src = iframe.get_attribute('src') or ''
                                                    if '_/bscframe' in iframe_src:
                                                        driver.switch_to.frame(iframe)
                                                        password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=3)
                                                        if password_input:
                                                            logger.info("[STEP] ✓ Password field found in iframe after CAPTCHA solving!")
                                                            break
                                                        driver.switch_to.default_content()
                                                except:
                                                    driver.switch_to.default_content()
                                                    continue
                                        except:
                                            pass
                                elif "challenge/totp" in current_url:
                                    logger.info("[STEP] Page redirected to TOTP challenge after CAPTCHA solving")
                                    # Handle TOTP challenge if we have secret key
                                    known_totp_secret = get_secret_key_from_dynamodb(email)
                                    if known_totp_secret:
                                        logger.info("[STEP] TOTP secret found in DynamoDB, handling TOTP challenge...")
                                        # Generate and enter TOTP code
                                        try:
                                            import pyotp
                                            totp = pyotp.TOTP(known_totp_secret)
                                            otp_code = totp.now()
                                            logger.info(f"[STEP] Generated TOTP code: {otp_code}")
                                            
                                            # Find OTP input field
                                            otp_input = None
                                            otp_xpaths = [
                                                "//input[@type='tel']",
                                                "//input[contains(@aria-label, 'code') or contains(@aria-label, 'verification')]",
                                                "//input[@autocomplete='one-time-code']",
                                                "//input[@type='text']"
                                            ]
                                            for xpath in otp_xpaths:
                                                try:
                                                    otp_input = wait_for_xpath(driver, xpath, timeout=3)
                                                    if otp_input:
                                                        break
                                                except:
                                                    continue
                                            
                                            if otp_input:
                                                otp_input.clear()
                                                otp_input.send_keys(otp_code)
                                                logger.info("[STEP] Entered TOTP code")
                                                
                                                # Click Next/Submit button
                                                submit_xpaths = [
                                                    "//button[contains(., 'Next')]",
                                                    "//button[contains(., 'Verify')]",
                                                    "//button[@type='submit']"
                                                ]
                                                for xpath in submit_xpaths:
                                                    try:
                                                        submit_btn = wait_for_clickable_xpath(driver, xpath, timeout=3)
                                                        if submit_btn:
                                                            submit_btn.click()
                                                            logger.info("[STEP] Clicked submit button for TOTP")
                                                            time.sleep(3)
                                                            break
                                                    except:
                                                        continue
                                                
                                                # Wait for redirect to password page
                                                time.sleep(3)
                                                current_url = driver.current_url
                                                if "challenge/pwd" in current_url:
                                                    logger.info("[STEP] ✓ Redirected to password page after TOTP verification")
                                                    password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=15)
                                                    if password_input:
                                                        logger.info("[STEP] ✓ Password field found after TOTP verification!")
                                                elif "myaccount.google.com" in current_url:
                                                    logger.info("[STEP] ✓ Already logged in after TOTP verification")
                                                    return True, None, None, None
                                        except Exception as totp_err:
                                            logger.warning(f"[STEP] TOTP handling error: {totp_err}")
                                    else:
                                        logger.warning("[STEP] TOTP challenge detected but no secret key available in DynamoDB")
                                else:
                                    logger.info(f"[STEP] Page URL after CAPTCHA: {current_url}")
                                    # Still try to find password field
                                    password_input = wait_for_password_clickable(driver, By.NAME, "Passwd", timeout=10)
                                    if password_input:
                                        logger.info("[STEP] ✓ Password field found after CAPTCHA solving!")
                            else:
                                logger.error(f"[STEP] ✗ Failed to solve Image CAPTCHA: {error}")
                                logger.warning("[STEP] Google may be blocking automated access. Consider using different IP/proxy.")
                                logger.warning("[STEP] This is likely due to IP-based rate limiting. Some accounts may still succeed.")
                except Exception as captcha_retry_err:
                    logger.warning(f"[STEP] Error during CAPTCHA check: {captcha_retry_err}")
                
                # If we still don't have password_input, fail
                if not password_input:
                    error_msg = "Password field not found after email submission (checked main document and iframes)."
                    if screenshot_saved or page_source_saved:
                        error_msg += f" Screenshot and page source saved to S3 for investigation."
                    else:
                        error_msg += " See DEBUG logs above for page state."
                    return False, "LOGIN_PASSWORD_FIELD_NOT_FOUND", error_msg
        
        # Clear and enter password with multiple fallback methods
        try:
            # Add human-like behavior before password entry
            random_scroll_and_mouse_move(driver)
            add_random_delays()
            
            # Method 1: Focus and clear using JavaScript first (more reliable)
            driver.execute_script("arguments[0].focus();", password_input)
            time.sleep(random.uniform(0.1, 0.2))
            driver.execute_script("arguments[0].click();", password_input)
            time.sleep(random.uniform(0.1, 0.2))
            
            # Try standard clear first
            try:
                password_input.clear()
            except:
                # If clear fails, use JavaScript
                driver.execute_script("arguments[0].value = '';", password_input)
            
            time.sleep(random.uniform(0.2, 0.4))  # Random wait time
            
            # Enter password using human-like typing simulation
            simulate_human_typing(password_input, password, driver)
            logger.info("[STEP] Password entered using human-like typing simulation")
        except Exception as e1:
            logger.warning(f"[STEP] Standard method failed: {e1}, trying JavaScript...")
            try:
                # Method 2: JavaScript interaction (more reliable fallback)
                driver.execute_script("arguments[0].focus();", password_input)
                driver.execute_script("arguments[0].click();", password_input)
                driver.execute_script("arguments[0].value = '';", password_input)
                driver.execute_script("arguments[0].value = arguments[1];", password_input, password)
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", password_input)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", password_input)
                logger.info("[STEP] Password entered using JavaScript method")
            except Exception as e2:
                logger.error(f"[STEP] JavaScript method also failed: {e2}")
                return False, "LOGIN_PASSWORD_INPUT_FAILED", f"Could not enter password: {e2}"
        
        # Verify password was entered
        try:
            entered_password = password_input.get_attribute('value')
            if not entered_password or entered_password != password:
                logger.warning(f"[STEP] Password verification failed. Expected length: {len(password)}, Got: {len(entered_password) if entered_password else 0}")
                # Try one more time with JavaScript
                try:
                    driver.execute_script("arguments[0].value = arguments[1];", password_input, password)
                    entered_password = password_input.get_attribute('value')
                    if entered_password != password:
                        logger.error("[STEP] Password still not entered correctly after retry")
                except:
                    pass
        except Exception as verify_err:
            logger.warning(f"[STEP] Could not verify password entry: {verify_err}")
        
        logger.info("[STEP] Password entered successfully")
        time.sleep(random.uniform(0.3, 0.6))  # Reduced wait time
        
        # Submit password using Enter key (faster and more natural)
        password_input.send_keys(Keys.RETURN)
        logger.info("[STEP] Password submitted using Enter key")

        # Add human-like behavior after password submission
        add_random_delays()
        random_scroll_and_mouse_move(driver)

        # Wait for potential challenge pages, intermediate pages, or account home
        # Google may show: speedbump, verification, phone prompt, TOTP, recovery email, etc.
        # We'll wait longer and handle what we can, skip what we can't
        max_wait_attempts = 30  # Increased from 15 to 30 (60 seconds total with reduced interval)
        wait_interval = 2  # Reduced from 3s to 2s for faster processing
        current_url = None
        speedbump_count = 0  # Counter to prevent infinite speedbump loops
        max_speedbumps = 20  # Maximum number of speedbump redirects before giving up
        
        for attempt in range(max_wait_attempts):
            time.sleep(wait_interval)
            
            # Add occasional random behavior during wait
            if attempt % 3 == 0:
                random_scroll_and_mouse_move(driver)
            
            try:
                current_url = driver.current_url
                logger.info(f"[STEP] Post-login check {attempt + 1}/{max_wait_attempts}: URL = {current_url}")
            except Exception as e:
                error_str = str(e).lower()
                # Check for browser crash/session errors
                if 'invalid session' in error_str or 'session deleted' in error_str or 'browser has closed' in error_str:
                    logger.error(f"[STEP] Browser session crashed: {e}")
                    return False, "BROWSER_CRASHED", f"Browser session crashed: {e}"
                logger.error(f"[STEP] Failed to get current URL: {e}")
                return False, "driver_crashed", f"Driver crashed while checking URL: {e}"
            
            # Check for CAPTCHA after password submission (this is another common place for CAPTCHA)
            # Only solve CAPTCHA once per user
            if not captcha_solved and detect_captcha(driver, email=email):
                logger.warning("[STEP] ⚠️ CAPTCHA detected after password submission!")
                
                # Try to solve CAPTCHA using 2Captcha if enabled (only once)
                solved, solve_error = solve_captcha_with_2captcha(driver, email=email)
                captcha_solved = True  # Mark as solved
                
                if solved:
                    logger.info("[STEP] ✓✓✓ CAPTCHA solved using 2Captcha! Continuing with login...")
                    # Wait a moment for page to process the solved CAPTCHA
                    time.sleep(2)  # Reduced wait time
                    
                    # Check if CAPTCHA is still present (but don't retry - already solved once)
                    if detect_captcha(driver, email=email):
                        logger.warning("[STEP] ⚠️ CAPTCHA still present after solving. This may be a different CAPTCHA.")
                        time.sleep(1)  # Brief wait
                    else:
                        logger.info("[STEP] ✓ CAPTCHA cleared after solving! Proceeding...")
                else:
                    logger.error(f"[STEP] ✗✗✗ CAPTCHA solving failed: {solve_error}")
                    return False, "CAPTCHA_DETECTED", f"CAPTCHA detected after password submission. 2Captcha solving failed: {solve_error}"
            elif captcha_solved and detect_captcha(driver, email=email):
                logger.info("[STEP] CAPTCHA already solved once for this user, skipping additional solve attempts...")
            
            # Check for account verification/ID verification required
            if "speedbump/idvreenable" in current_url or "idvreenable" in current_url:
                logger.error("[STEP] ID verification required - manual intervention needed")
                return False, "ID_VERIFICATION_REQUIRED", "Manual ID verification required"
            
            # Success conditions - we're logged in
            if any(domain in current_url for domain in ["myaccount.google.com", "mail.google.com", "accounts.google.com/b/0", "accounts.google.com/servicelogin"]):
                logger.info("[STEP] Login success - reached account page")
                return True, None, None
            
            # Handle speedbump/gaplustos page (Google Terms of Service)
            if "speedbump" in current_url:
                speedbump_count += 1
                logger.info(f"[STEP] Speedbump page detected ({speedbump_count}/{max_speedbumps}): {current_url}")
                
                # Prevent infinite speedbump loops
                if speedbump_count > max_speedbumps:
                    logger.error(f"[STEP] Too many speedbump redirects ({speedbump_count}). Breaking loop to prevent infinite wait.")
                    logger.warning("[STEP] This may indicate Google is blocking automated access or requiring manual verification.")
                    break
                
                # Check if it's the gaplustos page specifically
                if "speedbump/gaplustos" in current_url:
                    logger.info("[STEP] Google+ TOS speedbump detected, clicking confirm with JavaScript...")
                    try:
                        driver.execute_script("document.querySelector('#confirm').click()")
                        logger.info("[STEP] Clicked #confirm button via JavaScript")
                        time.sleep(2)
                    except Exception as e:
                        logger.warning(f"[STEP] JavaScript click failed, trying XPath: {e}")
                        try:
                            if element_exists(driver, "//button[@id='confirm']", timeout=2):
                                click_xpath(driver, "//button[@id='confirm']", timeout=5)
                                logger.info("[STEP] Clicked #confirm button via XPath")
                                time.sleep(2)
                        except Exception as e2:
                            logger.warning(f"[STEP] XPath click also failed: {e2}")
                elif "speedbump/workspacetermsofservice" in current_url:
                    logger.info("[STEP] Workspace TOS speedbump detected, clicking 'I understand'...")
                    try:
                        # Try multiple selectors for the "I understand" button
                        js_clicks = [
                            "document.querySelector('button[aria-label*=\"understand\"]').click()",
                            "document.querySelector('button span:contains(\"I understand\")').parentElement.click()",  # Pseudo-selector won't work in pure JS, fixed below
                            "Array.from(document.querySelectorAll('button')).find(el => el.textContent.includes('understand')).click()"
                        ]
                        clicked = False
                        for js in js_clicks:
                            try:
                                driver.execute_script(js)
                                logger.info(f"[STEP] Clicked button via JS: {js}")
                                clicked = True
                                break
                            except:
                                pass
                        
                        if not clicked:
                            # Fallback to XPath
                            xpaths = [
                                "//button[span[contains(text(), 'understand')]]",
                                "//button[contains(., 'I understand')]",
                                "//span[contains(text(), 'I understand')]/.."
                            ]
                            for xpath in xpaths:
                                if element_exists(driver, xpath, timeout=2):
                                    click_xpath(driver, xpath, timeout=5)
                                    logger.info(f"[STEP] Clicked button via XPath: {xpath}")
                                    clicked = True
                                    break
                        
                        time.sleep(3)
                    except Exception as e:
                        logger.warning(f"[STEP] Failed to click Workspace TOS button: {e}")
                else:
                    # Generic speedbump handling
                    logger.info("[STEP] Generic speedbump page, attempting to continue...")
                    try:
                        # Try to click continue/confirm button
                        speedbump_xpaths = [
                            "//button[@id='confirm']",
                            "//button[contains(., 'Continue')]",
                            "//button[contains(., 'Next')]",
                            "//button[contains(., 'I agree')]",
                            "//div[@role='button' and contains(., 'Continue')]",
                        ]
                        for xpath in speedbump_xpaths:
                            if element_exists(driver, xpath, timeout=2):
                                click_xpath(driver, xpath, timeout=5)
                                logger.info(f"[STEP] Clicked speedbump button: {xpath}")
                                time.sleep(2)
                                break
                    except Exception as e:
                        logger.warning(f"[STEP] Could not click speedbump button: {e}")
                continue
            
            # Handle 2SV required page
            if "twosvrequired" in current_url:
                logger.info("[STEP] Two-step verification required page detected, navigating to setup...")
                try:
                    driver.get("https://myaccount.google.com/two-step-verification/authenticator?hl=en")
                    # Check for captcha
                    if detect_captcha(driver, email=email):
                        logger.warning("[STEP] ⚠️ CAPTCHA detected on 2SV authenticator page!")
                        
                        # Try to solve CAPTCHA using 2Captcha if enabled
                        solved, solve_error = solve_captcha_with_2captcha(driver)
                        
                        if solved:
                            logger.info("[STEP] ✓✓✓ CAPTCHA solved using 2Captcha! Continuing...")
                            time.sleep(3)
                            driver.refresh()
                            time.sleep(2)
                        else:
                            logger.error(f"[STEP] ✗✗✗ CAPTCHA solving failed: {solve_error}")
                            return False, None, "CAPTCHA_DETECTED", f"CAPTCHA detected on 2SV authenticator page. 2Captcha solving failed: {solve_error}"
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f"[STEP] Could not navigate from twosvrequired: {e}")
                continue
            
            # Handle challenge pages (TOTP, phone, recovery, etc.)
            if "challenge" in current_url or "signin/challenge" in current_url:
                logger.info(f"[STEP] Challenge page detected: {current_url}")
                
                # Check if it's challenge/pwd - this usually means additional verification
                if "challenge/pwd" in current_url:
                    logger.info("[STEP] Password challenge page detected - looking for continue buttons...")
                    
                    # Try to find and click any continue/next/skip buttons
                    continue_xpaths = [
                        "//button[contains(., 'Continue')]",
                        "//button[contains(., 'Next')]",
                        "//button[contains(., 'Skip')]",
                        "//button[contains(., 'Not now')]",
                        "//button[contains(., 'Done')]",
                        "//span[contains(text(), 'Continue')]/ancestor::button",
                        "//span[contains(text(), 'Next')]/ancestor::button",
                        "//span[contains(text(), 'Skip')]/ancestor::button",
                        "//div[@role='button' and contains(., 'Continue')]",
                        "//div[@role='button' and contains(., 'Next')]",
                    ]
                    
                    clicked = False
                    for xpath in continue_xpaths:
                        try:
                            if element_exists(driver, xpath, timeout=2):
                                click_xpath(driver, xpath, timeout=5)
                                logger.info(f"[STEP] Clicked button on challenge/pwd page: {xpath}")
                                clicked = True
                                time.sleep(2)
                                break
                        except Exception as e:
                            logger.debug(f"[STEP] Could not click xpath {xpath}: {e}")
                            continue
                    
                    if clicked:
                        continue  # Go to next iteration to check new page
                    else:
                        # No button found - try to navigate directly to myaccount
                        logger.info("[STEP] No actionable button found on challenge/pwd, attempting direct navigation...")
                        try:
                            driver.get("https://myaccount.google.com/")
                            time.sleep(3)
                            continue
                        except Exception as e:
                            logger.warning(f"[STEP] Direct navigation failed: {e}")
                
                # Check if it's a TOTP challenge (we can handle this)
                if "challenge/totp" in current_url:
                    logger.info("[STEP] TOTP challenge detected")
                    
                    # Check for OTP input field
                    otp_input_xpaths = [
                        "//input[@type='tel']",
                        "//input[@autocomplete='one-time-code']",
                        "//input[@type='text' and contains(@aria-label, 'code')]",
                        "//input[contains(@aria-label, 'Code')]",
                    ]
                    
                    otp_input = None
                    for xpath in otp_input_xpaths:
                        try:
                            otp_input = wait_for_xpath(driver, xpath, timeout=5)
                            if otp_input:
                                break
                        except:
                            continue
                    
                    if otp_input:
                        if not known_totp_secret:
                            logger.error("[STEP] 2FA is required but no TOTP secret is available")
                            return False, "2FA_REQUIRED", "2FA required but secret is unknown"
                        
                        # Generate and submit TOTP code with retries
                        for retry in range(3):
                            try:
                                # Generate fresh TOTP code
                                clean_secret = known_totp_secret.replace(" ", "").upper()
                                totp = pyotp.TOTP(clean_secret)
                                otp_code = totp.now()
                                logger.info(f"[STEP] Generated TOTP code (attempt {retry + 1}): {otp_code}")
                                
                                # Clear and enter OTP
                                driver.execute_script("arguments[0].value = '';", otp_input)
                                driver.execute_script("arguments[0].value = arguments[1];", otp_input, otp_code)
                                logger.info(f"[STEP] OTP code entered (attempt {retry + 1})")
                                
                                # Submit OTP
                                submit_btn_xpaths = [
                                    "//button[contains(@type,'submit')]",
                                    "//button[@role='button' and contains(., 'Next')]",
                                    "//span[contains(text(), 'Next')]/ancestor::button",
                                    "//button[contains(., 'Verify')]",
                                ]
                                
                                submitted = False
                                for btn_xpath in submit_btn_xpaths:
                                    if element_exists(driver, btn_xpath, timeout=5):
                                        click_xpath(driver, btn_xpath, timeout=10)
                                        submitted = True
                                        break
                                
                                if not submitted:
                                    otp_input.send_keys(Keys.RETURN)
                                
                                # Wait and check result
                                time.sleep(5)
                                current_url = driver.current_url
                                
                                # Check if we left the TOTP page
                                if "challenge/totp" not in current_url:
                                    logger.info("[STEP] OTP verified successfully")
                                    break
                                
                                # Still on TOTP page - generate new code for retry
                                if retry < 2:
                                    logger.warning(f"[STEP] Still on TOTP page, retrying with new code...")
                                    time.sleep(3)  # Wait for new time window
                                else:
                                    logger.error("[STEP] OTP verification failed after 3 attempts")
                                    return False, "OTP_REJECTED", "OTP code was rejected by Google"
                            
                            except Exception as otp_e:
                                logger.error(f"[STEP] OTP submission error (attempt {retry + 1}): {otp_e}")
                                if retry == 2:
                                    return False, "OTP_SUBMISSION_ERROR", str(otp_e)
                        
                        # After TOTP success, continue waiting loop
                        continue
                    else:
                        logger.warning("[STEP] On challenge/totp page but no OTP input found")
                
                # Other challenge types - log and continue waiting
                logger.info(f"[STEP] Unhandled challenge type: {current_url}, waiting to see if it auto-resolves...")
                continue
            
            # If we're here, not on any recognized page yet - keep waiting
            if attempt < max_wait_attempts - 1:
                logger.info(f"[STEP] Still waiting for login to complete... ({attempt + 1}/{max_wait_attempts})")
        
        # If we've exhausted all attempts and not logged in, fail
        logger.error(f"[STEP] Login failed - did not reach myaccount.google.com after {max_wait_attempts} attempts")
        logger.error(f"[STEP] Final URL: {current_url}")
        return False, "LOGIN_TIMEOUT", f"Login timed out after {max_wait_attempts * wait_interval} seconds. Last URL: {current_url}"

    except Exception as e:
        logger.error(f"[STEP] Login exception: {e}")
        logger.error(traceback.format_exc())
        return False, "LOGIN_EXCEPTION", str(e)


# =====================================================================
# Step 2: Setup Authenticator (extract TOTP secret)
# =====================================================================


def setup_authenticator(driver, email):
    """
    Navigate to the authenticator setup page and extract the secret key.
    Based on reference script G_Ussers_No_Timing.py
    Returns (success: bool, secret_key: str|None, error_code: str|None, error_message: str|None)
    """
    # Add human-like behavior before navigating
    add_random_delays()
    random_scroll_and_mouse_move(driver)
    logger.info(f"[STEP] Setting up Authenticator for {email}")
    
    try:
        # Navigate to authenticator setup page
        logger.info("[STEP] Navigating to Authenticator setup page...")
        target_url = "https://myaccount.google.com/two-step-verification/authenticator?hl=en"
        driver.get(target_url)
        time.sleep(2)
        
        # Check for TOTP challenge during navigation (like reference script)
        current_url = driver.current_url
        if "challenge/totp" in current_url:
            logger.info("[STEP] TOTP challenge detected during navigation to authenticator setup page")
            known_totp_secret = get_secret_key_from_dynamodb(email)
            if known_totp_secret:
                logger.info("[STEP] TOTP secret found in DynamoDB, handling TOTP challenge...")
                try:
                    import pyotp
                    totp = pyotp.TOTP(known_totp_secret.replace(" ", "").upper())
                    otp_code = totp.now()
                    logger.info(f"[STEP] Generated TOTP code: {otp_code}")
                    
                    # Find OTP input field
                    otp_input = None
                    otp_xpaths = [
                        "//input[@type='tel']",
                        "//input[contains(@aria-label, 'code') or contains(@aria-label, 'verification')]",
                        "//input[@autocomplete='one-time-code']",
                        "//input[@type='text']"
                    ]
                    for xpath in otp_xpaths:
                        try:
                            otp_input = wait_for_xpath(driver, xpath, timeout=3)
                            if otp_input:
                                break
                        except:
                            continue
                    
                    if otp_input:
                        otp_input.clear()
                        otp_input.send_keys(otp_code)
                        logger.info("[STEP] Entered TOTP code")
                        
                        # Click Next/Submit button
                        submit_xpaths = [
                            "//button[contains(., 'Next')]",
                            "//button[contains(., 'Verify')]",
                            "//button[@type='submit']"
                        ]
                        for xpath in submit_xpaths:
                            try:
                                submit_btn = wait_for_clickable_xpath(driver, xpath, timeout=3)
                                if submit_btn:
                                    submit_btn.click()
                                    logger.info("[STEP] Clicked submit button for TOTP")
                                    time.sleep(3)
                                    break
                            except:
                                continue
                        
                        # Wait for redirect to authenticator setup page
                        time.sleep(3)
                        current_url = driver.current_url
                        if "authenticator" in current_url:
                            logger.info("[STEP] ✓ Redirected to authenticator setup page after TOTP verification")
                        elif "myaccount.google.com" in current_url:
                            # Navigate again to authenticator setup page
                            driver.get(target_url)
                            time.sleep(2)
                except Exception as totp_err:
                    logger.warning(f"[STEP] TOTP handling error during navigation: {totp_err}")
            else:
                logger.warning("[STEP] TOTP challenge detected but no secret key available in DynamoDB")
        
        # Add human-like behavior after page load
        add_random_delays()
        random_scroll_and_mouse_move(driver)
        inject_randomized_javascript(driver)
        
        time.sleep(1)  # Reduced from 2 to 1
        
        # Step 1: Click "Set up authenticator" button
        # Try multiple XPath patterns for the setup button
        logger.info("[STEP] Looking for 'Set up authenticator' button...")
        setup_button_xpaths = [
            "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div/div[3]/div[2]/div/div/div/button/span[5]",
            "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div/div[3]/div[2]/div/div/div/button",
            "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div/div[3]/div[2]/div/div/div/button",
            "//button[contains(., 'Set up') or contains(., 'SET UP')]",
            "//span[contains(text(), 'Set up')]/ancestor::button",
            "//button[contains(., 'Get started') or contains(., 'GET STARTED')]",
        ]
        
        setup_clicked = False
        for xpath in setup_button_xpaths:
            try:
                if element_exists(driver, xpath, timeout=3):
                    # Use JavaScript click for better reliability
                    element = wait_for_xpath(driver, xpath, timeout=3)
                    if element:
                        driver.execute_script("arguments[0].click();", element)
                        logger.info(f"[STEP] Clicked 'Set up authenticator' button using: {xpath}")
                        time.sleep(2)
                        setup_clicked = True
                        break
            except Exception as e:
                logger.debug(f"[STEP] Could not click setup button with xpath {xpath}: {e}")
                continue
        
        if not setup_clicked:
            logger.warning("[STEP] Could not find 'Set up authenticator' button, continuing anyway...")
        
        # Step 2: Click "Can't scan it?" link to show text version
        logger.info("[STEP] Looking for 'Can't scan it?' link...")
        
        # First try: Use JavaScript with dynamic div indices (more robust)
        cant_scan_clicked = False
        
        # Try a range of div indices as the modal position can vary
        for div_index in range(8, 18):
            try:
                # Construct XPath for this index
                xpath = f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/center/div/div/button/span[5]"
                
                # Check if element exists
                if element_exists(driver, xpath, timeout=0.5):
                    logger.info(f"[STEP] Found 'Can't scan it?' button at div[{div_index}]")
                    element = driver.find_element(By.XPATH, xpath)
                    driver.execute_script("arguments[0].click();", element)
                    logger.info(f"[STEP] Clicked 'Can't scan it?' link using div[{div_index}]")
                    time.sleep(1)
                    cant_scan_clicked = True
                    break
            except:
                continue
        
        if not cant_scan_clicked:
            # Try generic XPaths
            cant_scan_xpaths = [
                "//button[contains(., 'scan it')]",
                "//button[contains(., 'Can') and contains(., 'scan')]",
                "//div[contains(text(), 'scan it')]/ancestor::button",
                "//span[contains(text(), 'scan it')]/ancestor::button"
            ]
            
            for xpath in cant_scan_xpaths:
                try:
                    if element_exists(driver, xpath, timeout=2):
                        click_xpath(driver, xpath)
                        logger.info(f"[STEP] Clicked 'Can't scan it?' using generic xpath: {xpath}")
                        cant_scan_clicked = True
                        break
                except:
                    continue
        if not cant_scan_clicked:
            logger.warning("[STEP] Could not find 'Can't scan it?' link")
        
        # Step 3: Extract the secret key
        # Use the EXACT XPath pattern from the reference script
        logger.info("[STEP] Extracting secret key...")
        secret_key = None
        
        # Optimization: Scan the most likely dynamic divs first with short timeout
        if not secret_key:
             # Common locations in recent Google updates
            target_indices = [11, 12, 10, 13, 9] 
            for div_index in target_indices:
                try:
                    xpath = f"/html/body/div[{div_index}]/div/div[2]/span/div/div/ol/li[2]/div/strong"
                    # Reduced timeout to 1s for fast scanning
                    element = wait_for_xpath(driver, xpath, timeout=1) 
                    if element:
                        text = element.text.strip()
                        cleaned = text.replace(" ", "").upper()
                        if len(cleaned) >= 16:
                            secret_key = cleaned
                            logger.info(f"[STEP] Extracted secret key using div[{div_index}]: {secret_key[:4]}****{secret_key[-4:]}")
                            break
                except:
                    pass
        
        # Fallback: Try alternative XPath patterns
        if not secret_key:
            logger.info("[STEP] Trying alternative secret key XPaths...")
            alternative_xpaths = [
                "//strong[string-length(normalize-space(text())) >= 16]",
                "//div[contains(@class, 'key')]//div[contains(@class, 'value')]",
                "//span[contains(@class, 'secret')]",
                "//code[string-length(normalize-space(text())) >= 16]",
                "//pre[string-length(normalize-space(text())) >= 16]",
            ]
            
            # Add more dynamic div patterns
            for div_index in range(9, 14):
                alternative_xpaths.extend([
                    f"/html/body/div[{div_index}]/div/div[2]/span/div/div/ol/li[2]/div",
                    f"/html/body/div[{div_index}]/div/div[2]/span/div/div/ol/li[2]",
                    f"/html/body/div[{div_index}]//strong",
                ])
            
            for xpath in alternative_xpaths:
                try:
                    element = wait_for_xpath(driver, xpath, timeout=2)
                    if element:
                        text = element.text.strip()
                        cleaned = text.replace(" ", "").upper()
                        if len(cleaned) >= 16 and cleaned.isalnum():
                            secret_key = cleaned
                            logger.info(f"[STEP] Extracted secret key using alternative XPath: {secret_key[:4]}****{secret_key[-4:]}")
                            break
                except:
                    continue
        
        if not secret_key:
            logger.error("[STEP] Could not extract secret key from authenticator setup page")
            return False, None, "SECRET_EXTRACTION_FAILED", "Failed to extract TOTP secret key"
        
        logger.info(f"[STEP] Secret key successfully extracted: {secret_key[:4]}****{secret_key[-4:]}")
        
        # Step 4: Click "Next" button to proceed to verification
        # Based on G_Ussers_No_Timing.py click_continue_button logic
        logger.info("[STEP] Clicking 'Next' button to proceed to verification...")
        next_clicked = False
        
        # Try dynamic div indices for the Next button
        for div_index in range(9, 14):
            try:
                # Reference script XPath for Next button
                xpath = f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[2]/button"
                if element_exists(driver, xpath, timeout=2):
                    element = wait_for_xpath(driver, xpath, timeout=2)
                    if element:
                        driver.execute_script("arguments[0].scrollIntoView(true);", element)
                        driver.execute_script("arguments[0].click();", element)
                        logger.info(f"[STEP] Clicked 'Next' button using div[{div_index}]")
                        time.sleep(2)
                        next_clicked = True
                        break
            except Exception as e:
                continue
        
        if not next_clicked:
            # Fallback generic Next buttons
            logger.info("[STEP] Trying generic Next button XPaths...")
            generic_next_xpaths = [
                "//button[contains(., 'Next')]",
                "//span[contains(text(), 'Next')]/ancestor::button",
                "//div[contains(text(), 'Next')]/ancestor::button"
            ]
            for xpath in generic_next_xpaths:
                try:
                    if element_exists(driver, xpath, timeout=2):
                        click_xpath(driver, xpath, timeout=5)
                        logger.info(f"[STEP] Clicked Next button: {xpath}")
                        time.sleep(2)
                        next_clicked = True
                        break
                except:
                    continue
            
        if not next_clicked:
            logger.warning("[STEP] Could not find/click 'Next' button. Verification might fail if we are not on the input screen.")

        return True, secret_key, None, None
    
    except Exception as e:
        logger.error(f"[STEP] Authenticator setup exception: {e}")
        logger.error(traceback.format_exc())
        return False, None, "AUTHENTICATOR_SETUP_EXCEPTION", str(e)


# =====================================================================
# Step 3: Enable 2-Step Verification
# =====================================================================


def verify_authenticator_setup(driver, email, secret_key):
    """
    Verify the Authenticator setup by entering the TOTP code.
    This happens on the modal after clicking "Next" in setup_authenticator.
    Returns (success: bool, error_code: str|None, error_message: str|None)
    """
    logger.info(f"[STEP] Verifying Authenticator setup for {email}")
    
    try:
        # Generate TOTP code from the secret we extracted
        totp = pyotp.TOTP(secret_key.replace(" ", ""))
        otp_code = totp.now()
        logger.info(f"[STEP] Generated TOTP code for verification: {otp_code}")
        
        # Find the OTP input field
        # Use comprehensive XPaths from reference script
        otp_input = None
        
        # Try dynamic div indices first (most specific)
        for div_index in range(9, 14):
            xpaths = [
                f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/label/input",
                f"/html/body/div[{div_index}]/div/div[2]/span/div/div/div/div[2]/div/div/div[1]/span[2]/input"
            ]
            for xpath in xpaths:
                if element_exists(driver, xpath, timeout=1):
                    otp_input = wait_for_xpath(driver, xpath, timeout=1)
                    if otp_input:
                        logger.info(f"[STEP] Found OTP input using div[{div_index}]")
                        break
            if otp_input: break
        
        if not otp_input:
            logger.info("[STEP] Trying generic OTP input XPaths...")
            otp_input_xpaths = [
                "//input[@type='tel']",
                "//input[@autocomplete='one-time-code']",
                "//input[@type='text' and contains(@aria-label, 'code')]",
                "//input"
            ]
            for xpath in otp_input_xpaths:
                try:
                    otp_input = wait_for_xpath(driver, xpath, timeout=2)
                    if otp_input:
                        logger.info(f"[STEP] Found OTP input field: {xpath}")
                        break
                except:
                    continue
        
        if not otp_input:
            logger.error("[STEP] Could not find OTP input field for verification")
            return False, "OTP_INPUT_NOT_FOUND", "OTP input field not found"
        
        # Enter the TOTP code
        otp_input.clear()
        time.sleep(0.5)
        otp_input.send_keys(otp_code)
        logger.info("[STEP] Entered TOTP code")
        time.sleep(1)
        
        # Click Verify button
        verify_clicked = False
        
        # Try dynamic div indices for Verify button
        for div_index in range(9, 14):
            xpath = f"/html/body/div[{div_index}]/div/div[2]/div[3]/div/div[2]/div[2]/button"
            try:
                if element_exists(driver, xpath, timeout=1):
                    btn = wait_for_xpath(driver, xpath, timeout=1)
                    if btn:
                        driver.execute_script("arguments[0].click();", btn)
                        logger.info(f"[STEP] Clicked Verify button using div[{div_index}]")
                        verify_clicked = True
                        break
            except: continue

        if not verify_clicked:
            verify_button_xpaths = [
                "//button[contains(., 'Verify')]",
                "//span[contains(text(), 'Verify')]/ancestor::button",
                "//div[contains(text(), 'Verify')]/ancestor::button",
                "//button[contains(., 'Next')]",
            ]
            
            for xpath in verify_button_xpaths:
                if element_exists(driver, xpath, timeout=2):
                    click_xpath(driver, xpath, timeout=5)
                    logger.info(f"[STEP] Clicked Verify button: {xpath}")
                    verify_clicked = True
                    time.sleep(2)
                    break
        
        if not verify_clicked:
             # Try hitting Enter key on the input if button fails
            logger.warning("[STEP] Could not click Verify button, trying Enter key...")
            otp_input.send_keys(Keys.RETURN)
        
        time.sleep(3)
        logger.info("[STEP] Authenticator verified successfully")
        return True, None, None
    
    except Exception as e:
        logger.error(f"[STEP] Authenticator verification exception: {e}")
        logger.error(traceback.format_exc())
        return False, "AUTH_VERIFY_EXCEPTION", str(e)


def enable_two_step_verification(driver, email):
    """
    Enable Two-Step Verification for the given account.
    Based on reference script G_Ussers_No_Timing.py enable_two_step_verification function.
    Navigates to 2SV page, clicks Turn On, and skips phone number.
    """
    logger.info(f"[STEP] Navigating to 2-Step Verification page for {email}...")
    
    try:
        # Navigate to 2-Step Verification page (with hl=en for English)
        driver.get("https://myaccount.google.com/signinoptions/twosv?hl=en")
        
        # Check for captcha
        if detect_captcha(driver, email=email):
            logger.warning("[STEP] ⚠️ CAPTCHA detected on 2SV page!")
            
            # Try to solve CAPTCHA using 2Captcha if enabled
            solved, solve_error = solve_captcha_with_2captcha(driver, email=email)
            
            if solved:
                logger.info("[STEP] ✓✓✓ CAPTCHA solved using 2Captcha! Continuing...")
                time.sleep(3)
                # Refresh page to ensure CAPTCHA is cleared
                driver.refresh()
                time.sleep(2)
            else:
                logger.error(f"[STEP] ✗✗✗ CAPTCHA solving failed: {solve_error}")
                return False, None, "CAPTCHA_DETECTED", f"CAPTCHA detected on 2SV page. 2Captcha solving failed: {solve_error}"
        time.sleep(3)
        
        # Check if 2-step verification is already enabled
        if element_exists(driver, "//button[contains(., 'Turn off')]", timeout=3):
            logger.info(f"[STEP] 2-Step Verification is already enabled for {email}")
            return True, None, None

        # Try XPaths with priority on updated XPath
        turn_on_clicked = False
        turn_on_xpaths = [
            '/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div[2]/div[4]/div/button',  # Priority XPath
            '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[2]/div[4]/div/button',
            '/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[2]/div[4]/div/button/span[6]',
            '/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div[2]/div[4]/div/button/span[6]',
            "//button[contains(., 'Turn on')]",
            "//button[contains(., 'TURN ON')]",
            "//span[contains(text(), 'Turn on')]/ancestor::button",
        ]
        
        for xpath in turn_on_xpaths:
            try:
                turn_on_button = wait_for_clickable_xpath(driver, xpath, timeout=5)
                if turn_on_button:
                    driver.execute_script("arguments[0].click();", turn_on_button)
                    logger.info(f"[STEP] Clicked on 'Turn On 2-Step Verification' using xpath: {xpath}")
                    turn_on_clicked = True
                    time.sleep(2)
                    break
            except TimeoutException:
                continue
            except Exception as e:
                logger.debug(f"[STEP] Error trying xpath {xpath}: {e}")
                continue

        # Handle skip phone number (from reference script handle_skip_phone_number)
        try:
            skip_link = wait_for_clickable_xpath(driver, '//button//span[contains(text(), "Skip")]', timeout=5)
            if skip_link:
                driver.execute_script("arguments[0].click();", skip_link)
                logger.info("[STEP] Clicked 'Skip' to bypass phone number setup.")
                time.sleep(2)
        except TimeoutException:
            logger.info("[STEP] No 'Skip' link found for phone number setup.")

        logger.info(f"[STEP] 2-Step Verification enabled successfully for {email}")
        return True, None, None

    except TimeoutException as e:
        logger.error(f"[STEP] Timeout while enabling 2-Step Verification for {email}: {e}")
        return False, "2SV_TIMEOUT", str(e)
    except Exception as e:
        logger.error(f"[STEP] Error during 2-Step Verification setup for {email}: {e}")
        logger.error(traceback.format_exc())
        return False, "2SV_EXCEPTION", str(e)


# =====================================================================
# Step 4: Generate App Password
# =====================================================================


def generate_app_password(driver, email, dynamodb_table=None):
    """
    Navigate to App Passwords page and generate a new app password.
    Based on reference script G_Ussers_No_Timing.py generate_app_password function.
    Returns (success: bool, app_password: str|None, error_code: str|None, error_message: str|None)
    """
    logger.info(f"[STEP] Generating App Password for {email}")
    
    try:
        # Wait up to 30 seconds after enabling 2SV for app password page to be ready
        logger.info("[STEP] Waiting for app password page to be ready (may take up to 30 seconds after enabling 2SV)...")
        
        # Navigate to app passwords page with hl=en for English
        # Check for TOTP challenge during navigation (like reference script)
        target_url = "https://myaccount.google.com/apppasswords?hl=en"
        driver.get(target_url)
        time.sleep(2)
        
        # Check for TOTP challenge after navigation (like reference script add_totp_check_to_navigation)
        current_url = driver.current_url
        if "challenge/totp" in current_url:
            logger.info("[STEP] TOTP challenge detected during navigation to app passwords page")
            known_totp_secret = get_secret_key_from_dynamodb(email, table_name=dynamodb_table)
            if known_totp_secret:
                logger.info("[STEP] TOTP secret found in DynamoDB, handling TOTP challenge...")
                try:
                    import pyotp
                    totp = pyotp.TOTP(known_totp_secret.replace(" ", "").upper())
                    otp_code = totp.now()
                    logger.info(f"[STEP] Generated TOTP code: {otp_code}")
                    
                    # Find OTP input field
                    otp_input = None
                    otp_xpaths = [
                        "//input[@type='tel']",
                        "//input[contains(@aria-label, 'code') or contains(@aria-label, 'verification')]",
                        "//input[@autocomplete='one-time-code']",
                        "//input[@type='text']"
                    ]
                    for xpath in otp_xpaths:
                        try:
                            otp_input = wait_for_xpath(driver, xpath, timeout=3)
                            if otp_input:
                                break
                        except:
                            continue
                    
                    if otp_input:
                        otp_input.clear()
                        otp_input.send_keys(otp_code)
                        logger.info("[STEP] Entered TOTP code")
                        
                        # Click Next/Submit button
                        submit_xpaths = [
                            "//button[contains(., 'Next')]",
                            "//button[contains(., 'Verify')]",
                            "//button[@type='submit']"
                        ]
                        for xpath in submit_xpaths:
                            try:
                                submit_btn = wait_for_clickable_xpath(driver, xpath, timeout=3)
                                if submit_btn:
                                    submit_btn.click()
                                    logger.info("[STEP] Clicked submit button for TOTP")
                                    time.sleep(3)
                                    break
                            except:
                                continue
                        
                        # Wait for redirect to app passwords page
                        time.sleep(3)
                        current_url = driver.current_url
                        if "apppasswords" in current_url:
                            logger.info("[STEP] ✓ Redirected to app passwords page after TOTP verification")
                        elif "myaccount.google.com" in current_url:
                            # Navigate again to app passwords page
                            driver.get(target_url)
                            time.sleep(2)
                except Exception as totp_err:
                    logger.warning(f"[STEP] TOTP handling error during navigation: {totp_err}")
            else:
                logger.warning("[STEP] TOTP challenge detected but no secret key available in DynamoDB")
        
        # Add human-like behavior after page load
        add_random_delays()
        random_scroll_and_mouse_move(driver)
        inject_randomized_javascript(driver)
        
        # Check for captcha
        if detect_captcha(driver, email=email):
            logger.warning("[STEP] ⚠️ CAPTCHA detected on app passwords page!")
            
            # Try to solve CAPTCHA using 2Captcha if enabled
            solved, solve_error = solve_captcha_with_2captcha(driver, email=email)
            
            if solved:
                logger.info("[STEP] ✓✓✓ CAPTCHA solved using 2Captcha! Continuing...")
                time.sleep(3)
                # Refresh page to ensure CAPTCHA is cleared
                driver.refresh()
                time.sleep(2)
            else:
                logger.error(f"[STEP] ✗✗✗ CAPTCHA solving failed: {solve_error}")
                return False, None, "CAPTCHA_DETECTED", f"CAPTCHA detected on app passwords page. 2Captcha solving failed: {solve_error}"
        
        # Wait for page to be ready
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2)  # Additional wait for dynamic content
            logger.info("[STEP] App passwords page loaded")
        except TimeoutException:
            logger.warning("[STEP] App passwords page load timeout, proceeding anyway...")
        
        max_retries = 3
        initial_timeout = 30
        
        for attempt in range(max_retries):
            try:
                # Comprehensive XPath variations for app name input (updated with priority XPath)
                app_name_xpath_variations = [
                    "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/div[1]/span[3]/input",  # Priority XPath
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/div[1]/span[3]/input",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/label/input",
                    "/html/body/c-wiz/div/div[2]/div[3]/c-wiz/div/div[4]/div/div[3]/div/div[1]/div/div/label/input",
                    "//input[@aria-label='App name']",
                    "//input[contains(@placeholder, 'app') or contains(@placeholder, 'name')]",
                    "//input[@type='text' and contains(@class, 'input')]",
                    "//input[@type='text']",
                    "//label[contains(text(), 'App name')]/following::input",
                    "//div[contains(@class, 'app')]//input[@type='text']",
                    "//form//input[@type='text'][1]",
                    "//c-wiz//input[@type='text']"
                ]
                
                app_name_field = None
                for xpath in app_name_xpath_variations:
                    try:
                        element = wait_for_xpath(driver, xpath, timeout=5)
                        if element:
                            # Check if element is interactable
                            try:
                                # Try to scroll into view and check if visible
                                driver.execute_script("arguments[0].scrollIntoView(true);", element)
                                time.sleep(0.5)
                                if element.is_displayed() and element.is_enabled():
                                    app_name_field = element
                                    logger.info(f"[STEP] Found app name input field: {xpath}")
                                    break
                            except:
                                continue
                    except:
                        continue
                
                if not app_name_field:
                    logger.warning(f"[STEP] App name input field not detected on attempt {attempt + 1}, refreshing page...")
                    driver.refresh()
                    time.sleep(3)
                    if attempt < max_retries - 1:
                        continue
                    else:
                        raise TimeoutException("Failed to locate app name input field after retries")
                
                # Generate random app name (matching reference script format)
                app_name = f"App-{int(time.time())}"
                logger.info(f"[STEP] Generated app name: {app_name}")
                
                # Clear and enter app name using JavaScript if regular methods fail
                try:
                    app_name_field.clear()
                    app_name_field.send_keys(app_name)
                    logger.info(f"[STEP] Entered app name using regular method")
                except Exception as clear_err:
                    # Fallback to JavaScript if element not interactable
                    logger.warning(f"[STEP] Regular input failed, using JavaScript: {clear_err}")
                    driver.execute_script("arguments[0].value = '';", app_name_field)
                    driver.execute_script("arguments[0].value = arguments[1];", app_name_field, app_name)
                    # Trigger input event
                    driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", app_name_field)
                    logger.info(f"[STEP] Entered app name using JavaScript")
                
                time.sleep(1)
                
                # Click Generate button with comprehensive XPaths (from reference script)
                generate_button_xpath_variations = [
                    "/html/body/c-wiz[1]/div/div[2]/div[3]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button/span[5]",
                    "/html/body/c-wiz/div/div[2]/div[2]/c-wiz/div/div[4]/div/div[3]/div/div[2]/div/div/div/button/span[2]",
                    "//button[contains(., 'Generate')]",
                    "//button[contains(@aria-label, 'Generate')]",
                    "//button[@type='button' and contains(text(), 'Generate')]",
                    "//span[contains(text(), 'Generate')]/parent::button",
                    "//div[contains(@class, 'generate')]//button",
                    "//button[contains(@class, 'generate')]",
                    "//form//button[@type='button']",
                    "//c-wiz//button[not(contains(@aria-label, 'Close'))]"
                ]
                
                # Click Generate button with retry logic
                dialog_appeared = False
                
                for click_attempt in range(3):
                    logger.info(f"[STEP] Attempting to click Generate button (Attempt {click_attempt + 1})...")
                    generate_clicked = False
                    
                    for xpath in generate_button_xpath_variations:
                        try:
                            if element_exists(driver, xpath, timeout=3):
                                element = wait_for_clickable_xpath(driver, xpath, timeout=5)
                                if element:
                                    driver.execute_script("arguments[0].scrollIntoView(true);", element)
                                    
                                    # Try different click methods based on attempt
                                    if click_attempt == 0:
                                        driver.execute_script("arguments[0].click();", element)
                                    elif click_attempt == 1:
                                        element.click()
                                    else:
                                        ActionChains(driver).move_to_element(element).click().perform()
                                        
                                    logger.info(f"[STEP] Clicked Generate button: {xpath}")
                                    generate_clicked = True
                                    time.sleep(2)
                                    break
                        except:
                            continue
                    
                    if not generate_clicked:
                        logger.warning(f"[STEP] Failed to click Generate button on attempt {click_attempt + 1}")
                        if click_attempt < 2:
                            continue
                        else:
                            raise TimeoutException("Failed to click Generate button after retries")
                    
                    # Wait for app password dialog to appear
                    logger.info("[STEP] Waiting for app password dialog to appear...")
                    
                    dialog_selectors = [
                        "//div[@aria-modal='true']",
                        "//div[@role='dialog']",
                        "//div[@class='uW2Fw-P5QLlc']",
                        "//span[contains(text(), 'Generated app password')]",
                        "//h2[contains(., 'Generated app password')]"
                    ]
                    
                    for selector in dialog_selectors:
                        try:
                            WebDriverWait(driver, 5).until( # Short timeout for retry loop
                                EC.presence_of_element_located((By.XPATH, selector))
                            )
                            logger.info(f"[STEP] App password dialog detected: {selector}")
                            dialog_appeared = True
                            break
                        except TimeoutException:
                            continue
                    
                    if dialog_appeared:
                        break
                    
                    logger.warning("[STEP] Dialog did not appear after click, retrying...")
                    time.sleep(2)
                
                if not dialog_appeared:
                    logger.error("[STEP] App password dialog did not appear after clicking Generate")
                    if attempt < max_retries - 1:
                        driver.refresh()
                        time.sleep(3)
                        continue
                    else:
                        raise TimeoutException("App password dialog did not appear")
                
                # Extract app password from spans first (from reference script extract_app_password_from_spans)
                logger.info("[STEP] Attempting to extract password from span elements...")
                app_password = None
                
                span_container_xpaths = [
                    "//strong[@class='v2CTKd KaSAf']//div[@dir='ltr']",
                    "//strong[@class='v2CTKd KaSAf']//div",
                    "//div[@class='lY6Rwe riHXqb']//strong//div",
                    "//h2[@class='XfTrZ']//strong//div",
                    "//article//strong//div[@dir='ltr']"
                ]
                
                for xpath in span_container_xpaths:
                    try:
                        container = WebDriverWait(driver, 5).until(
                            EC.presence_of_element_located((By.XPATH, xpath))
                        )
                        spans = container.find_elements(By.TAG_NAME, "span")
                        if spans:
                            password_chars = []
                            for span in spans:
                                char = span.text.strip()
                                if char:
                                    password_chars.append(char)
                            
                            if password_chars:
                                full_password = ''.join(password_chars)
                                clean_password = full_password.replace(' ', '')
                                
                                # Reconstruct dashes if needed
                                if len(clean_password) >= 16 and '-' not in clean_password:
                                    if len(clean_password) == 16:
                                        clean_password = f"{clean_password[:4]}-{clean_password[4:8]}-{clean_password[8:12]}-{clean_password[12:16]}"
                                
                                if len(clean_password) >= 16 and (clean_password.count('-') >= 3 or len(clean_password) == 19):
                                    app_password = clean_password
                                    logger.info(f"[STEP] Extracted app password from spans: {app_password[:4]}****{app_password[-4:]}")
                                    break
                    except:
                        continue
                
                # Fallback to dynamic XPath patterns if span extraction failed (updated with priority XPath)
                if not app_password:
                    logger.info("[STEP] Span extraction failed, trying dynamic XPath patterns...")
                    priority_xpaths = [
                        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong",  # Priority XPath
                        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong/div",
                        "/html/body/div[16]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div",
                        "/html/body/div[16]//strong[contains(text(), '-')]",
                        "//strong[@class='v2CTKd KaSAf']//div[@dir='ltr']",
                        "//strong[@class='v2CTKd KaSAf']//div",
                        "//strong[@class='v2CTKd KaSAf']",
                        "//div[@class='lY6Rwe riHXqb']//strong",
                        "//h2[@class='XfTrZ']//strong",
                        "//header[@class='VuF2Pd lY6Rwe']//strong",
                        "//article//strong[@class='v2CTKd KaSAf']",
                    ]
                    
                    # Add dynamic div patterns with retries (focusing around div[16] first, then expanding)
                    # Try div[16] variations first (priority)
                    for div_num in [16, 15, 17, 14, 18, 19, 20, 21, 22]:
                        priority_xpaths.extend([
                            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong/div",
                            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div/strong",
                            f"/html/body/div[{div_num}]/div[2]/div/div[1]/div/div[1]/article/header/div/h2/div",
                            f"/html/body/div[{div_num}]//strong[contains(text(), '-')]",
                        ])
                    
                    # Retry with different XPaths
                    for retry_attempt in range(3):
                        for i, xpath in enumerate(priority_xpaths):
                            try:
                                element = WebDriverWait(driver, 2).until(
                                    EC.presence_of_element_located((By.XPATH, xpath))
                                )
                                potential_password = element.text.strip().replace(" ", "")
                                if len(potential_password) >= 16 and '-' in potential_password and potential_password.count('-') >= 3:
                                    app_password = potential_password
                                    logger.info(f"[STEP] App password found using XPath #{i+1} (retry {retry_attempt + 1}): {app_password[:4]}****{app_password[-4:]}")
                                    break
                            except:
                                continue
                        
                        if app_password:
                            break
                        
                        if retry_attempt < 2:
                            logger.info(f"[STEP] Retry {retry_attempt + 1} failed, waiting before next attempt...")
                            time.sleep(2)
                
                if not app_password or len(app_password) < 16:
                    raise TimeoutException("Failed to locate valid app password element")
                
                logger.info("[STEP] App Password generated successfully")
                return True, app_password, None, None
                
            except TimeoutException as e:
                logger.warning(f"[STEP] Attempt {attempt + 1} failed to generate app password: {e}")
                if attempt < max_retries - 1:
                    driver.refresh()
                    time.sleep(3)
                else:
                    raise e
        
        logger.error("[STEP] App Password generation failed after all retries")
        return False, None, "APP_PASSWORD_GENERATION_FAILED", "Failed to generate app password after retries"
    
    except Exception as e:
        logger.error(f"[STEP] App Password generation exception: {e}")
        logger.error(traceback.format_exc())
        return False, None, "APP_PASSWORD_EXCEPTION", str(e)


# =====================================================================
# DynamoDB Storage
# =====================================================================

def ensure_dynamodb_table_exists(table_name="dev-app-passwords"):
    """
    Ensure DynamoDB table exists. Creates it if it doesn't exist.
    Uses a fixed region (eu-west-1) so all Lambda functions use the same table.
    Returns True if table exists or was created, False on error.
    Note: Table creation is asynchronous, so we don't wait for it to be active.
    """
    try:
        # Use fixed region for DynamoDB - centralized storage
        dynamodb_region = os.environ.get("DYNAMODB_REGION", "eu-west-1")
        dynamodb_client = boto3.client("dynamodb", region_name=dynamodb_region)
        
        # Check if table exists
        try:
            dynamodb_client.describe_table(TableName=table_name)
            logger.info(f"[DYNAMODB] Table {table_name} already exists")
            return True
        except ClientError as e:
            if e.response['Error']['Code'] != 'ResourceNotFoundException':
                logger.error(f"[DYNAMODB] Error checking table {table_name}: {e}")
                return False
            
            # Table doesn't exist, create it
            logger.info(f"[DYNAMODB] Table {table_name} not found. Creating...")
            try:
                dynamodb_client.create_table(
                    TableName=table_name,
                    KeySchema=[
                        {'AttributeName': 'email', 'KeyType': 'HASH'}  # Partition key
                    ],
                    AttributeDefinitions=[
                        {'AttributeName': 'email', 'AttributeType': 'S'}
                    ],
                    BillingMode='PAY_PER_REQUEST'  # On-demand pricing (no provisioned capacity)
                )
                logger.info(f"[DYNAMODB] ✓ Table {table_name} creation initiated (will be active in ~10-30 seconds)")
                # Don't wait for table to be active - it's asynchronous
                # The first save attempt might fail, but subsequent attempts will succeed
                return True
            except ClientError as create_error:
                if create_error.response['Error']['Code'] == 'ResourceInUseException':
                    # Table is being created by another process, that's fine
                    logger.info(f"[DYNAMODB] Table {table_name} is being created by another process")
                    return True
                else:
                    logger.error(f"[DYNAMODB] Failed to create table {table_name}: {create_error}")
                    return False
                    
    except Exception as e:
        logger.error(f"[DYNAMODB] Exception ensuring table exists: {e}")
        logger.error(f"[DYNAMODB] Traceback: {traceback.format_exc()}")
        return False

def get_secret_key_from_dynamodb(email, table_name=None):
    """
    Retrieve TOTP secret key from DynamoDB for the given email.
    Returns the full secret key (unmasked) if found, None otherwise.
    """
    table_name = table_name or os.environ.get("DYNAMODB_TABLE_NAME", "dev-app-passwords")
    
    try:
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        
        response = table.get_item(Key={"email": email})
        
        if "Item" in response:
            item = response["Item"]
            # Check if secret_key exists and is not masked (contains "****")
            if "secret_key" in item:
                secret_key = item["secret_key"]
                # If it's masked, we need to retrieve the full key from a different source
                # For now, return None if masked (we should save full key separately)
                if "****" in secret_key:
                    logger.warning(f"[DYNAMODB] Secret key for {email} is masked in DynamoDB")
                    return None
                return secret_key
            else:
                logger.info(f"[DYNAMODB] No secret key found for {email} in DynamoDB")
                return None
        else:
            logger.info(f"[DYNAMODB] No record found for {email} in DynamoDB")
            return None
            
    except ClientError as e:
        logger.error(f"[DYNAMODB] Error retrieving secret key for {email}: {e}")
        return None
    except Exception as e:
        logger.error(f"[DYNAMODB] Unexpected error retrieving secret key for {email}: {e}")
        return None


def save_to_dynamodb(email, app_password, secret_key=None, table_name=None):
    """
    Save app password to DynamoDB for reliable storage and retrieval.
    Table: dev-app-passwords (default) or dynamic
    Primary Key: email
    Attributes: email, app_password, secret_key, created_at, updated_at
    
    Automatically creates the table if it doesn't exist.
    """
    table_name = table_name or os.environ.get("DYNAMODB_TABLE_NAME", "dev-app-passwords")
    
    try:
        # Use shared DynamoDB resource for better connection pooling and performance
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(table_name)
        
        # Use Unix timestamp (integer) for better DynamoDB performance and querying
        timestamp = int(time.time())
        
        item = {
            "email": email,
            "app_password": app_password,
            "created_at": timestamp,
            "updated_at": timestamp
        }
        
        # Add secret_key if provided - save FULL key (unmasked) for TOTP generation
        if secret_key:
            item["secret_key"] = secret_key  # Save full key, not masked
        
        # Put item (upsert - creates or updates)
        table.put_item(Item=item)
        
        logger.info(f"[DYNAMODB] Successfully saved {email} to {table_name}")
        return True
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', '')
        
        if error_code == 'ResourceNotFoundException':
            # Table doesn't exist, try to create it
            logger.warning(f"[DYNAMODB] Table {table_name} not found. Attempting to create...")
            if ensure_dynamodb_table_exists(table_name):
                # Wait a moment for table to become available (if just created)
                time.sleep(2)
                # Retry the save operation
                try:
                    table = dynamodb.Table(table_name)
                    table.put_item(Item=item)
                    logger.info(f"[DYNAMODB] Successfully saved {email} to {table_name} after table creation")
                    return True
                except Exception as retry_error:
                    logger.error(f"[DYNAMODB] Failed to save {email} after table creation: {retry_error}")
                    logger.error(f"[DYNAMODB] ⚠️ Table {table_name} may still be initializing. Please wait 10-30 seconds and retry.")
                    return False
            else:
                logger.error(f"[DYNAMODB] Failed to create table {table_name}. Please create it manually in AWS Console.")
                logger.error(f"[DYNAMODB] Table name: {table_name}")
                logger.error(f"[DYNAMODB] Primary key: email (String)")
                logger.error(f"[DYNAMODB] Billing mode: PAY_PER_REQUEST")
                return False
        else:
            logger.error(f"[DYNAMODB] Failed to save {email}: {e}")
            logger.error(f"[DYNAMODB] Error code: {error_code}")
            return False
        
    except Exception as e:
        logger.error(f"[DYNAMODB] Failed to save {email}: {e}")
        logger.error(f"[DYNAMODB] Traceback: {traceback.format_exc()}")
        return False

# =====================================================================
# Lambda Handler
# =====================================================================


def handler(event, context):
    """
    AWS Lambda handler function.
    
    Expected event format (single user - backward compatible):
    {
        "email": "user@example.com",
        "password": "userpassword"
    }
    
    Expected event format (batch processing - up to 10 users):
    {
        "users": [
            {"email": "user1@example.com", "password": "password1"},
            {"email": "user2@example.com", "password": "password2"},
            ...
        ]
    }
    
    Returns JSON with status, results (for batch) or single user fields (for backward compatibility).
    """
    start_time = time.time()
    timings = {}
    
    logger.info("=" * 60)
    logger.info("[LAMBDA] Handler invoked")
    logger.info(f"[LAMBDA] Event type: {type(event)}")
    logger.info(f"[LAMBDA] Event content: {event}")
    logger.info(f"[LAMBDA] Context: {context}")
    logger.info("=" * 60)
    
    # Check if this is a batch request (new format) or single user (backward compatible)
    users_batch = event.get("users")
    
    # Get dynamic DynamoDB table from event (priority) or env var
    dynamodb_table = event.get("dynamodb_table")
    table_name = dynamodb_table or os.environ.get("DYNAMODB_TABLE_NAME", "dev-app-passwords")
    
    if users_batch:
        # Batch processing mode - UNLIMITED users, processed in sequential batches of 3
        if not isinstance(users_batch, list):
            return {
                "status": "failed",
                "error_message": "Invalid 'users' field - must be a list",
                "results": []
            }
        
        # Get BATCH_SIZE from environment (how many users to process in parallel at once)
        # Default is 3 for optimal Lambda memory usage
        PARALLEL_BATCH_SIZE = int(os.environ.get('PARALLEL_BATCH_SIZE', '3'))
        logger.info(f"[LAMBDA] PARALLEL_BATCH_SIZE setting: {PARALLEL_BATCH_SIZE}")
        
        total_users = len(users_batch)
        logger.info(f"[LAMBDA] Received {total_users} users to process in batches of {PARALLEL_BATCH_SIZE}")
        
        # Ensure DynamoDB table exists before processing
        logger.info(f"[LAMBDA] Ensuring DynamoDB table exists: {table_name}")
        ensure_dynamodb_table_exists(table_name)
        
        # Process ALL users in SEQUENTIAL BATCHES of PARALLEL_BATCH_SIZE
        all_results = []
        total_batches = (total_users + PARALLEL_BATCH_SIZE - 1) // PARALLEL_BATCH_SIZE  # Ceiling division
        
        def process_user_wrapper(user_data, idx, batch_num, users_in_batch, dynamodb_table=None):
            """Wrapper function to process a single user with proper error handling"""
            email = user_data.get("email", "").strip()
            password = user_data.get("password", "").strip()
            
            # Get proxy for this user (rotation happens automatically)
            proxy = get_rotated_proxy_for_user()
            if proxy:
                logger.info(f"[PROXY] [{email}] Using proxy: {proxy['ip']}:{proxy['port']}")
                # Set proxy in environment for this user's Chrome driver
                os.environ['PROXY_CONFIG'] = proxy['full']
            else:
                # Clear proxy config if not available
                os.environ.pop('PROXY_CONFIG', None)
            
            if not email or not password:
                return {
                    "email": email or "unknown",
                    "status": "failed",
                    "error_message": "Email or password not provided",
                    "app_password": None,
                    "secret_key": None
                }
            
            # OPTIMIZED STAGGER: Only stagger the first 4 users to avoid resource spike
            # After that, workers are naturally staggered as they complete at different times
            # In dynamic mode (batch_num=1 for all), idx is the global user index
            if idx < 4:
                stagger_delay = idx * 5.0  # 0s, 5s, 10s, 15s for first 4 users
                if stagger_delay > 0:
                    logger.info(f"[LAMBDA] Staggering Chrome start for user {idx + 1}: waiting {stagger_delay}s")
                    time.sleep(stagger_delay)
            
            logger.info(f"[LAMBDA] Starting processing of user {idx + 1}/{users_in_batch}: {email}")
            try:
                user_result = process_single_user(email, password, start_time, dynamodb_table=dynamodb_table)
                logger.info(f"[LAMBDA] [BATCH {batch_num}] Completed user {idx + 1}/{users_in_batch}: {email} - Status: {user_result.get('status', 'unknown')}")
                return user_result
            except Exception as e:
                logger.error(f"[LAMBDA] [BATCH {batch_num}] Exception processing user {idx + 1}/{users_in_batch}: {email} - {str(e)}")
                logger.error(f"[LAMBDA] [BATCH {batch_num}] Traceback: {traceback.format_exc()}")
                return {
                    "email": email,
                    "status": "failed",
                    "error_message": f"Exception during processing: {str(e)}",
                    "app_password": None,
                    "secret_key": None
                }
        
        # Check for SEQUENTIAL_PROCESSING override for maximum stability
        sequential_mode = os.environ.get('SEQUENTIAL_PROCESSING', 'false').lower() == 'true'
        
        if sequential_mode:
            # SEQUENTIAL MODE: Process one user at a time
            logger.info(f"[LAMBDA] SEQUENTIAL_PROCESSING enabled. Processing {total_users} users one by one.")
            for idx, user_data in enumerate(users_batch):
                result = process_user_wrapper(user_data, idx, 1, total_users, dynamodb_table=table_name)
                all_results.append(result)
                time.sleep(2)  # Cool-down between users
        else:
            # DYNAMIC PARALLEL MODE: Always maintain 4 concurrent workers
            # This ensures that as soon as 1 worker finishes, it immediately picks up the next user
            logger.info(f"[LAMBDA] DYNAMIC PARALLEL MODE: Maintaining {PARALLEL_BATCH_SIZE} concurrent workers")
            logger.info(f"[LAMBDA] Processing {total_users} users with rolling window approach")
            
            # Clean /tmp before starting
            try:
                subprocess.run(['rm', '-rf', '/tmp/chrome-data', '/tmp/data-path', '/tmp/cache-dir'], capture_output=True)
                os.makedirs('/tmp/chrome-data', exist_ok=True)
            except:
                pass
            
            completed_count = 0
            
            with ThreadPoolExecutor(max_workers=PARALLEL_BATCH_SIZE) as executor:
                # Submit all users to the executor
                # The executor will automatically maintain max_workers concurrent tasks
                future_to_user = {
                    executor.submit(process_user_wrapper, user_data, idx, 1, total_users, table_name): (idx, user_data)
                    for idx, user_data in enumerate(users_batch)
                }
                
                # Process results as they complete
                # This ensures workers immediately pick up new tasks when they finish
                for future in as_completed(future_to_user):
                    idx, user_data = future_to_user[future]
                    email = user_data.get("email", "unknown")
                    completed_count += 1
                    
                    try:
                        result = future.result()
                        all_results.append(result)
                        status = result.get('status', 'unknown')
                        logger.info(f"[LAMBDA] ✓ Completed {completed_count}/{total_users}: {email} - Status: {status}")
                    except Exception as e:
                        logger.error(f"[LAMBDA] ✗ Future exception for user {idx + 1}: {email} - {str(e)}")
                        all_results.append({
                            "email": email,
                            "status": "failed",
                            "error_message": f"Future exception: {str(e)}",
                            "app_password": None,
                            "secret_key": None
                        })
                    
                    # Log progress every 25%
                    if completed_count % max(1, total_users // 4) == 0 or completed_count == total_users:
                        logger.info(f"[LAMBDA] Progress: {completed_count}/{total_users} users completed ({int(completed_count/total_users*100)}%)")
        
        logger.info(f"[LAMBDA] All {total_users} users have been processed")

        
        # =====================================================================
        # RETRY MECHANISM: Retry failed users once at the end
        # =====================================================================
        failed_users = [(i, r) for i, r in enumerate(all_results) if r.get("status") != "success"]
        
        if failed_users:
            logger.info(f"\n{'='*60}")
            logger.info(f"[LAMBDA] RETRY PHASE: {len(failed_users)} failed user(s) will be retried once")
            logger.info(f"{'='*60}")
            
            # Clean /tmp before retry phase
            try:
                subprocess.run(['rm', '-rf', '/tmp/chrome-data', '/tmp/data-path', '/tmp/cache-dir'], capture_output=True)
                os.makedirs('/tmp/chrome-data', exist_ok=True)
            except:
                pass
            
            retry_success_count = 0
            
            for retry_num, (original_idx, failed_result) in enumerate(failed_users, 1):
                email = failed_result.get("email", "unknown")
                
                # Find the original user data from users_batch
                original_user_data = None
                for user_data in users_batch:
                    if user_data.get("email", "").strip() == email:
                        original_user_data = user_data
                        break
                
                if not original_user_data:
                    logger.warning(f"[RETRY] Could not find original user data for {email}, skipping retry")
                    continue
                
                password = original_user_data.get("password", "").strip()
                
                logger.info(f"[RETRY] ({retry_num}/{len(failed_users)}) Retrying user: {email}")
                
                try:
                    # Get a fresh proxy for retry
                    proxy = get_rotated_proxy_for_user()
                    if proxy:
                        logger.info(f"[RETRY] [{email}] Using proxy: {proxy['ip']}:{proxy['port']}")
                        os.environ['PROXY_CONFIG'] = proxy['full']
                    else:
                        os.environ.pop('PROXY_CONFIG', None)
                    
                    # Process the user again
                    retry_result = process_single_user(email, password, start_time, dynamodb_table=table_name)
                    
                    if retry_result.get("status") == "success":
                        logger.info(f"[RETRY] ✅ SUCCESS on retry: {email}")
                        retry_success_count += 1
                        # Update the original result in all_results
                        all_results[original_idx] = retry_result
                        all_results[original_idx]["retried"] = True
                    else:
                        logger.info(f"[RETRY] ❌ Still failed after retry: {email} - {retry_result.get('error_message', 'Unknown')}")
                        # Update with retry attempt info
                        all_results[original_idx]["retry_attempted"] = True
                        all_results[original_idx]["retry_error"] = retry_result.get("error_message", "Unknown")
                        
                except Exception as e:
                    logger.error(f"[RETRY] Exception during retry for {email}: {str(e)}")
                    all_results[original_idx]["retry_attempted"] = True
                    all_results[original_idx]["retry_error"] = str(e)
                
                # Brief pause between retries
                time.sleep(2)
            
            logger.info(f"[RETRY] Retry phase completed: {retry_success_count}/{len(failed_users)} recovered")
        
        # Calculate total time
        total_time = round(time.time() - start_time, 2)
        
        # Count successes and failures (after retries)
        success_count = sum(1 for r in all_results if r.get("status") == "success")
        failed_count = len(all_results) - success_count
        
        logger.info(f"\n{'='*60}")
        logger.info(f"[LAMBDA] ALL BATCHES COMPLETED (including retries): {success_count} success, {failed_count} failed in {total_time}s")
        logger.info(f"[LAMBDA] Processed {total_users} users in {total_batches} batch(es)")
        logger.info(f"{'='*60}")
        
        return {
            "status": "completed",
            "total_users": total_users,
            "batch_count": total_batches,
            "batch_size": PARALLEL_BATCH_SIZE,
            "success_count": success_count,
            "failed_count": failed_count,
            "total_time": total_time,
            "results": all_results
        }
    
    else:
        # Single user mode (backward compatible)
        email = event.get("email", os.environ.get("GW_EMAIL"))
        password = event.get("password", os.environ.get("GW_PASSWORD"))
        
        # Get dynamic DynamoDB table from event (priority) or env var
        dynamodb_table = event.get("dynamodb_table")
        table_name = dynamodb_table or os.environ.get("DYNAMODB_TABLE_NAME", "dev-app-passwords")
    
    if not email or not password:
        return {
            "status": "failed",
            "step_completed": "init",
            "error_step": "init",
            "error_message": "Email or password not provided in event or environment",
            "app_password": None,
            "secret_key": None,
            "timings": timings
        }
    
    logger.info(f"[LAMBDA] Single user mode: {email} (Table: {table_name})")
    
    # Ensure DynamoDB table exists
    ensure_dynamodb_table_exists(table_name)
    
    return process_single_user(email, password, start_time, dynamodb_table=table_name)


def process_single_user(email, password, batch_start_time=None, dynamodb_table=None):
    """
    Process a single user account through all steps.
    Returns result dictionary with status, app_password, secret_key, etc.
    """
    user_start_time = time.time() if batch_start_time is None else batch_start_time
    timings = {}
    
    driver = None
    secret_key = None
    app_password = None
    step_completed = "init"
    error_code = None
    error_message = None
    
    # Browser restart loop for "Secure Browser" blocks
    max_browser_attempts = 3
    login_success = False
    
    for browser_attempt in range(max_browser_attempts):
        try:
            # Ensure clean state before starting
            cleanup_chrome_processes()
            
            if browser_attempt > 0:
                logger.info(f"[LAMBDA] Restarting browser (Attempt {browser_attempt + 1}/{max_browser_attempts})...")
            
            # Step 0: Initialize Chrome driver
            step_start = time.time()
            driver = get_chrome_driver()
            timings["driver_init"] = round(time.time() - step_start, 2)
            logger.info(f"[LAMBDA] Chrome driver started for {email}")
            
            # Step 1: Login
            step_completed = "login"
            step_start = time.time()
            success, error_code, error_message = login_google(driver, email, password)
            timings["login"] = round(time.time() - step_start, 2)
            
            if success:
                login_success = True
                break
            
            # Handle Login Failures
            if error_code == "ACCOUNT_NOT_FOUND":
                logger.error(f"[LAMBDA] FATAL ERROR: Account not found for {email}. Aborting (no retry).")
                if driver:
                    driver.quit()
                return {
                    "email": email,
                    "status": "failed",
                    "step_completed": step_completed,
                    "error_step": step_completed,
                    "error_message": error_message,
                    "app_password": None,
                    "secret_key": None,
                    "timings": timings
                }
            
            # OPTIMIZATION: Skip retries for EMAIL_ERROR (email rejected by Google - permanent failure)
            if error_code == "EMAIL_ERROR":
                logger.error(f"[LAMBDA] PERMANENT ERROR: Email rejected by Google for {email}. Aborting (no retry).")
                if driver:
                    driver.quit()
                return {
                    "email": email,
                    "status": "failed",
                    "step_completed": step_completed,
                    "error_step": step_completed,
                    "error_message": error_message,
                    "app_password": None,
                    "secret_key": None,
                    "timings": timings
                }
            
            # For SECURE_BROWSER_BLOCK, CRASHES, TIMEOUTS, or any other error -> RETRY
            # We treat almost everything else as a transient failure worth retrying with a fresh browser
            logger.warning(f"[LAMBDA] Login failed with error: {error_code} - {error_message}")
            logger.warning(f"[LAMBDA] Retrying with fresh browser (Attempt {browser_attempt + 1}/{max_browser_attempts})...")
            
            if driver:
                try:
                    driver.quit()
                except:
                    pass
                driver = None
            
            # Force cleanup after failure
            cleanup_chrome_processes()
            continue # Restart loop for ALL other errors
                
        except Exception as e:
            logger.error(f"[LAMBDA] Exception during browser attempt {browser_attempt + 1}: {e}")
            if driver:
                try:
                    driver.quit()
                except:
                    pass
                driver = None
            
            if browser_attempt == max_browser_attempts - 1:
                return {
                    "email": email,
                    "status": "failed",
                    "step_completed": step_completed,
                    "error_step": step_completed,
                    "error_message": f"Max browser retries reached: {str(e)}",
                    "app_password": None,
                    "secret_key": None,
                    "timings": timings
                }
            time.sleep(2)
            
    if not login_success:
        return {
            "email": email,
            "status": "failed",
            "step_completed": "login",
            "error_step": "login",
            "error_message": "Login failed after all retries",
            "app_password": None,
            "secret_key": None,
            "timings": timings
        }

    try:
        # Step 2: Setup Authenticator (extract secret)
        step_completed = "authenticator_setup"
        step_start = time.time()
        success, secret_key, error_code, error_message = setup_authenticator(driver, email)
        timings["authenticator_setup"] = round(time.time() - step_start, 2)
        
        if not success:
            logger.error(f"[STEP] Authenticator setup failed: {error_message}")
            return {
                "email": email,
                "status": "failed",
                "step_completed": step_completed,
                "error_step": step_completed,
                "error_message": error_message,
                "app_password": None,
                "secret_key": None,
                "timings": timings
            }
        
        # Step 2.5: Upload secret to SFTP
        step_start = time.time()
        sftp_host, sftp_path = upload_secret_to_sftp(email, secret_key)
        timings["sftp_upload"] = round(time.time() - step_start, 2)
        
        if not sftp_host:
            logger.warning("[SFTP] Could not upload secret to SFTP, continuing anyway...")
        
        # Step 3a: Verify Authenticator Setup (Enter OTP and click Verify)
        step_completed = "verify_authenticator"
        step_start = time.time()
        success, error_code, error_message = verify_authenticator_setup(driver, email, secret_key)
        timings["verify_authenticator"] = round(time.time() - step_start, 2)
        
        if not success:
            logger.error(f"[STEP] Authenticator verification failed: {error_message}")
            return {
                "email": email,
                "status": "failed",
                "step_completed": step_completed,
                "error_step": step_completed,
                "error_message": error_message,
                "app_password": None,
                "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
                "timings": timings
            }
        
        # Step 3b: Enable 2-Step Verification (Navigate to 2SV page and click Turn On)
        step_completed = "enable_2sv"
        step_start = time.time()
        success, error_code, error_message = enable_two_step_verification(driver, email)
        timings["enable_2sv"] = round(time.time() - step_start, 2)
        
        if not success:
            logger.error(f"[STEP] 2-Step Verification enable failed: {error_message}")
            return {
                "email": email,
                "status": "failed",
                "step_completed": step_completed,
                "error_step": step_completed,
                "error_message": error_message,
                "app_password": None,
                "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
                "timings": timings
            }
        
        # Wait for app password page to be ready (may take up to 30 seconds after enabling 2SV)
        logger.info("[STEP] Waiting for app password authorization (may take up to 30 seconds)...")
        time.sleep(5)  # Initial wait
        
        # Step 4: Generate App Password (with retry logic for 2SV)
        step_completed = "app_password"
        max_app_password_retries = 2  # Maximum retries for app password generation
        app_password_retry_count = 0
        
        while app_password_retry_count <= max_app_password_retries:
            step_start = time.time()
            success, app_password, error_code, error_message = generate_app_password(driver, email, dynamodb_table=dynamodb_table)
            timings["app_password"] = round(time.time() - step_start, 2)
            
            if success:
                # App password generated successfully
                break
            elif error_code == "RETRY_2SV_REQUIRED" and app_password_retry_count < max_app_password_retries:
                # Retry 2-Step Verification and then retry app password generation
                logger.warning(f"[STEP] App password generation indicates 2SV retry needed (attempt {app_password_retry_count + 1}/{max_app_password_retries + 1})")
                logger.info("[STEP] Retrying 2-Step Verification step...")
                
                # Retry 2-Step Verification
                step_start_2sv = time.time()
                success_2sv, error_code_2sv, error_message_2sv = enable_two_step_verification(driver, email)
                timings["enable_2sv_retry"] = round(time.time() - step_start_2sv, 2)
                
                if not success_2sv:
                    logger.error(f"[STEP] 2-Step Verification retry failed: {error_message_2sv}")
                    return {
                        "email": email,
                        "status": "failed",
                        "step_completed": "enable_2sv_retry",
                        "error_step": "enable_2sv_retry",
                        "error_message": f"2SV retry failed: {error_message_2sv}",
                        "app_password": None,
                        "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
                        "timings": timings
                    }
                
                logger.info("[STEP] 2-Step Verification retry successful. Waiting before retrying app password...")
                time.sleep(5)  # Wait after re-enabling 2SV
                app_password_retry_count += 1
                continue
            else:
                # Other error or max retries reached
                logger.error(f"[STEP] App Password generation failed: {error_message}")
                return {
                    "email": email,
                    "status": "failed",
                    "step_completed": step_completed,
                    "error_step": step_completed,
                    "error_message": error_message,
                    "app_password": None,
                    "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
                    "timings": timings
                }
        
        if not success:
            # Should not reach here, but handle just in case
            logger.error(f"[STEP] App Password generation failed after all retries: {error_message}")
            return {
                "email": email,
                "status": "failed",
                "step_completed": step_completed,
                "error_step": step_completed,
                "error_message": error_message or "App password generation failed after all retries",
                "app_password": None,
                "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
                "timings": timings
            }
        
        # Step 4.5: Save App Password to DynamoDB
        step_start = time.time()
        dynamo_success = save_to_dynamodb(email, app_password, secret_key, table_name=dynamodb_table)
        timings["dynamodb_save"] = round(time.time() - step_start, 2)
        
        if dynamo_success:
            logger.info(f"[DYNAMODB] ✓ Password saved successfully for {email}")
        else:
            logger.warning(f"[DYNAMODB] ⚠️ Could not save to DynamoDB for {email}, continuing anyway...")
        
        # All steps completed successfully
        step_completed = "completed"
        total_time = round(time.time() - user_start_time, 2)
        timings["total"] = total_time
        
        logger.info(f"[LAMBDA] All steps completed successfully for {email} in {total_time} seconds")
        
        return {
            "email": email,
            "status": "success",
            "step_completed": step_completed,
            "error_step": None,
            "error_message": None,
            "app_password": app_password,
            "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,  # Masked for security
            "timings": timings
        }
    
    except Exception as e:
        logger.error(f"[LAMBDA] Unhandled exception for {email}: {e}")
        logger.error(traceback.format_exc())
        
        total_time = round(time.time() - user_start_time, 2)
        timings["total"] = total_time
        
        return {
            "email": email,
            "status": "failed",
            "step_completed": step_completed,
            "error_step": step_completed,
            "error_message": f"Unhandled exception: {str(e)}",
            "app_password": app_password,
            "secret_key": secret_key[:4] + "****" + secret_key[-4:] if secret_key else None,
            "timings": timings
        }
    
    finally:
        # Always cleanup driver
        if driver:
            try:
                driver.quit()
                logger.info(f"[LAMBDA] Chrome driver closed for {email}")
            except:
                pass