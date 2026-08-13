"""
Run this directly on the server to debug Afraid login:
  cd /opt/gbot-web-app && source venv/bin/activate && python3 debug_afraid.py
"""
import requests
import re

# ---- PUT YOUR CREDENTIALS HERE ----
USERNAME = "wbennettmary"  # Change if needed
PASSWORD = ""  # Leave empty – script will read from DB

# Try to read from DB if password empty
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
                print(f"[DB] Loaded credentials: user={USERNAME}")
            else:
                print("[DB] No afraid_config row found! Please save credentials in the UI first.")
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

print("\n===== STEP 1: Get login page for cookies =====")
r = session.get("https://freedns.afraid.org/zc.php?step=1")
print(f"  Status: {r.status_code}")

print("\n===== STEP 2: Submit login =====")
payload = {
    "username": USERNAME,
    "password": PASSWORD,
    "remember": "1",
    "submit": "Login",
    "remote": "",
    "from": "",
    "action": "auth"
}
r = session.post("https://freedns.afraid.org/zc.php?step=2", data=payload, allow_redirects=False)
print(f"  Status: {r.status_code}")
print(f"  Location header: {r.headers.get('Location', '(none)')}")
if r.status_code != 302:
    print(f"  HTML snippet: {r.text[:800]}")
else:
    print("  [OK] Login redirected – likely successful!")

print("\n===== STEP 3: Follow redirect and check auth =====")
r2 = session.get("https://freedns.afraid.org/subdomain/")
print(f"  Status: {r2.status_code}")
if "Logout" in r2.text or "logout" in r2.text:
    print("  [OK] 'Logout' found in page – we are authenticated!")
else:
    print("  [FAIL] No 'Logout' found – not authenticated.")
    print(f"  HTML snippet: {r2.text[:800]}")

print("\n===== STEP 4: Fetch domain_id select from edit.php =====")
r3 = session.get("https://freedns.afraid.org/subdomain/edit.php")
print(f"  Status: {r3.status_code}")
select_block = re.search(r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>', r3.text, re.IGNORECASE | re.DOTALL)
if select_block:
    print("  [OK] Found domain_id <select> block!")
    pattern = r'<option\s+[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>'
    options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
    print(f"  Found {len(options)} domain(s):")
    for val, name in options:
        print(f"    id={val}  name={name.strip()}")
else:
    print("  [FAIL] Could not find domain_id <select> block!")
    print(f"  HTML snippet: {r3.text[:1000]}")
