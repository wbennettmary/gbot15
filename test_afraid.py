from app import app
from database import AfraidConfig
from services.afraid_dns_service import AfraidDNSService


with app.app_context():
    config = AfraidConfig.query.first()
    if not config or not config.cookies_str:
        print("No Afraid cookies saved in the database.")
        raise SystemExit(1)

    svc = AfraidDNSService(config.cookies_str)
    print(f"Logged in: {svc.logged_in}")
    if not svc.logged_in:
        print(f"Reason: {svc.auth_error}")
        raise SystemExit(1)

    domain_map = svc.get_domains_with_ids()
    print(f"Domains found: {len(domain_map)}")
    for domain, domain_id in domain_map.items():
        print(f"  {domain}: {domain_id}")
