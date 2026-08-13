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
                print(f"[DB] Loaded credentials: user={USERNAME}, password_length={len(PASSWORD)}, password_preview={PASSWORD[:3]}***")
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

print("\n===== STEP 1: Get login page =====")
r = session.get("https://freedns.afraid.org/zc.php?step=1", allow_redirects=True)
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
print(f"  Location: {r.headers.get('Location', '(none)')}")
if r.status_code == 302:
    print("  [OK] 302 redirect – login successful!")
    logged_in = True
else:
    logged_in = False
    print("  [FAIL] No redirect – bad credentials or wrong form params")
    # Show the actual error text from the page
    # First try the known CSS selector structure
    import lxml.html
    try:
        doc = lxml.html.fromstring(r.text)
        tables = doc.cssselect('table[width="95%"]')
        if tables:
            cells = tables[0].cssselect('td[bgcolor="#eeeeee"]')
            if cells:
                print(f"  FreeDNS says: '{cells[0].text_content().strip()}'")
            else:
                print("  Could not find error cell via CSS selector")
        else:
            print("  Could not find error table")
    except Exception as e:
        print(f"  lxml parse error: {e}")
    print(f"\n  Raw HTML (first 800 chars):\n{r.text[:800]}")

print("\n===== STEP 3: Check auth via /subdomain/ page =====")
r2 = session.get("https://freedns.afraid.org/subdomain/", allow_redirects=False)
print(f"  Status: {r2.status_code}")
if r2.status_code == 302:
    print(f"  [FAIL] Redirected to: {r2.headers.get('Location')} – NOT authenticated")
elif r2.status_code == 200:
    # The subdomain page will have the domain_id select if logged in
    select_block = re.search(r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>', r2.text, re.IGNORECASE | re.DOTALL)
    if select_block:
        print("  [OK] Authenticated + found domain_id select on /subdomain/ page!")
        pattern = r'<option\s+[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>'
        options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
        print(f"  Found {len(options)} domain(s):")
        for val, name in options:
            print(f"    id={val}  name={name.strip()}")
    else:
        title_m = re.search(r'<title>(.*?)</title>', r2.text, re.I)
        print(f"  Status 200 but no domain select. Page title: {title_m.group(1) if title_m else 'unknown'}")
        print(f"  HTML snippet: {r2.text[:400]}")
