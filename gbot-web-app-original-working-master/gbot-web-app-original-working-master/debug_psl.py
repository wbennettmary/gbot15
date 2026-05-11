import publicsuffix2
from services.zone_utils import to_apex

domain = "pareja.amasahistoricalsociety.space"
print(f"Input: {domain}")

psl = publicsuffix2.PublicSuffixList()
suffix = psl.get_public_suffix(domain)
print(f"psl.get_public_suffix('{domain}'): {suffix}")

apex = to_apex(domain)
print(f"to_apex('{domain}'): {apex}")

# Test logic in namecheap_dns_service
def extract_sld(domain):
    registrable = psl.get_public_suffix(domain)
    if registrable:
        domain_parts = domain.split('.')
        registrable_parts = registrable.split('.')
        if len(domain_parts) > len(registrable_parts):
            return domain_parts[-(len(registrable_parts) + 1)]
        elif len(registrable_parts) >= 2:
            return registrable_parts[0]
    return "fallback"

print(f"extract_sld('{domain}'): {extract_sld(domain)}")
