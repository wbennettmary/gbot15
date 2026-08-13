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
                print(f"[DB] Loaded credentials: user={USERNAME}")
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
if r.status_code == 302:
    print("  [OK] Got 302 redirect – login successful!")
    logged_in = True
else:
    # Try to extract the specific error message from the "Problems!" page
    error_match = re.search(r'<td[^>]*bgcolor="#eeeeee"[^>]*>(.*?)</td>', r.text, re.IGNORECASE | re.DOTALL)
    if error_match:
        msg = re.sub(r'<[^>]+>', '', error_match.group(1)).strip()
        print(f"  [FAIL] FreeDNS error: {msg}")
    else:
        print(f"  [FAIL] Login did NOT redirect. HTML title: {re.search(r'<title>(.*?)</title>', r.text, re.I).group(1) if re.search(r'<title>(.*?)</title>', r.text, re.I) else 'unknown'}")
    logged_in = False

print("\n===== STEP 3: Check auth by visiting profile page =====")
r2 = session.get("https://freedns.afraid.org/profile/", allow_redirects=False)
print(f"  Status: {r2.status_code}")
if r2.status_code == 200 and "username" in r2.text.lower():
    print("  [OK] Profile page loaded – authenticated!")
    logged_in = True
elif r2.status_code == 302:
    print(f"  [FAIL] Profile redirected to: {r2.headers.get('Location')} – NOT authenticated")
else:
    print(f"  [?] Status: {r2.status_code}, Content snippet: {r2.text[:300]}")

print("\n===== STEP 4: Fetch domain_id select from add.php =====")
r3 = session.get("https://freedns.afraid.org/subdomain/add.php")
print(f"  Status: {r3.status_code}")
print(f"  Page title: {re.search(r'<title>(.*?)</title>', r3.text, re.I).group(1).strip() if re.search(r'<title>(.*?)</title>', r3.text, re.I) else 'unknown'}")

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
    print(f"  Full HTML (first 1000 chars):\n{r3.text[:1000]}")
