"""
Run this directly on the server to debug Afraid login:
  cd /opt/gbot-web-app && source venv/bin/activate && python3 debug_afraid.py
"""
import requests
import re

USERNAME = "wbennettmary"
PASSWORD = ""

if not PASSWORD:
    import sys
    sys.path.insert(0, '.')
    try:
        from app import app
        from database import AfraidConfig
        with app.app_context():
            cfg = AfraidConfig.query.first()
            if cfg:
                USERNAME = cfg.username
                PASSWORD = cfg.password
                print(f"[DB] Loaded credentials: user={USERNAME}, password_length={len(PASSWORD)}, preview={PASSWORD[:3]}***")
            else:
                print("[DB] No afraid_config row found!")
                sys.exit(1)
    except Exception as e:
        print(f"[DB] Could not load from DB: {e}")
        sys.exit(1)

headers = {
    "Host": "freedns.afraid.org",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:102.0) Gecko/20100101 Firefox/102.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

session = requests.Session()
session.headers.update(headers)

print("\n===== STEP 1: Get login page + scrape hidden fields =====")
r = session.get("https://freedns.afraid.org/zc.php?step=1", allow_redirects=True)
print(f"  Status: {r.status_code}")

# Find ALL hidden input fields to include in login POST
hidden_fields = re.findall(r'<input[^>]+type=["\']?hidden["\']?[^>]*>', r.text, re.IGNORECASE)
form_data = {}
for field in hidden_fields:
    name_m = re.search(r'name=["\']([^"\']+)["\']', field)
    value_m = re.search(r'value=["\']([^"\']*)["\']', field)
    if name_m:
        form_data[name_m.group(1)] = value_m.group(1) if value_m else ''

print(f"  Hidden fields found: {form_data}")

print("\n===== STEP 2: Submit login (with hidden fields) =====")
payload = {
    **form_data,  # Include any hidden CSRF/session fields
    "username": USERNAME,
    "password": PASSWORD,
    "remember": "1",
    "submit": "Login",
    "remote": "",
    "from": "",
    "action": "auth"
}
print(f"  Full payload keys: {list(payload.keys())}")

r2 = session.post("https://freedns.afraid.org/zc.php?step=2", data=payload, allow_redirects=False)
print(f"  Status: {r2.status_code}")
print(f"  Location: {r2.headers.get('Location', '(none)')}")

if r2.status_code == 302:
    print("  [OK] 302 redirect – login SUCCESSFUL!")
    logged_in = True
else:
    logged_in = False
    print("  [FAIL] No redirect – printing raw response:")
    # Extract error using regex only (no lxml)
    error_m = re.search(r'bgcolor=["\']?#eeeeee["\']?[^>]*>(.*?)</td>', r2.text, re.IGNORECASE | re.DOTALL)
    if error_m:
        msg = re.sub(r'<[^>]+>', '', error_m.group(1)).strip()
        print(f"  FreeDNS error text: '{msg}'")
    print(f"\n  Raw HTML:\n{r2.text[:1200]}")

print("\n===== STEP 3: Check auth via /subdomain/ =====")
r3 = session.get("https://freedns.afraid.org/subdomain/", allow_redirects=False)
print(f"  Status: {r3.status_code}")
if r3.status_code == 302:
    print(f"  [FAIL] Redirected to login: {r3.headers.get('Location')}")
else:
    select_block = re.search(r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>', r3.text, re.IGNORECASE | re.DOTALL)
    if select_block:
        print("  [OK] Authenticated + domain_id select found!")
        options = re.findall(r'<option\s+[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>', select_block.group(1), re.IGNORECASE)
        for val, name in options:
            print(f"    id={val}  name={name.strip()}")
    else:
        title = re.search(r'<title>(.*?)</title>', r3.text, re.I)
        print(f"  Status 200 but no domain select. Title: {title.group(1) if title else 'unknown'}")
        print(f"  HTML snippet:\n{r3.text[:600]}")
