"""
Run this directly on the server to debug Afraid cookie authentication:
  cd /opt/gbot-web-app && source venv/bin/activate && python3 debug_afraid.py
"""
from app import app
from database import AfraidConfig
from services.afraid_dns_service import AfraidDNSService


with app.app_context():
    cfg = AfraidConfig.query.first()
    if not cfg or not cfg.cookies_str:
        print("[DB] No afraid_config row with cookies_str found.")
        raise SystemExit(1)

    print(f"[DB] Loaded cookie string length: {len(cfg.cookies_str)}")
    svc = AfraidDNSService(cfg.cookies_str)
    print(f"[AUTH] Logged in: {svc.logged_in}")

    if not svc.logged_in:
        print(f"[AUTH] Reason: {svc.auth_error}")
        raise SystemExit(1)

    print("[DOMAINS] Fetching available FreeDNS domains...")
    domains = svc.get_domains_with_ids()
    print(f"[DOMAINS] Found {len(domains)} domain(s).")
    for domain, domain_id in domains.items():
        print(f"  id={domain_id}  name={domain}")
