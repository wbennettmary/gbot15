import sys
import os
import logging

# Mock logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add current directory to path
sys.path.append(os.getcwd())

try:
    import publicsuffix2
    print(f"publicsuffix2 version: {publicsuffix2.__file__}")
except ImportError:
    print("publicsuffix2 not installed")

# Import the functions to test
# We'll copy the logic here to test it in isolation if imports fail, 
# but ideally we import from the files.
# Since we are in the root, we can try importing.

from services.zone_utils import to_apex
from services.namecheap_dns_service import NamecheapDNSService

def test_parsing(domain):
    print(f"\n--- Testing {domain} ---")
    
    # Test to_apex
    apex = to_apex(domain)
    print(f"to_apex('{domain}') -> '{apex}'")
    
    # Test Namecheap extraction (mocking the service)
    service = NamecheapDNSService()
    # We can't easily instantiate service because it loads config from DB.
    # So we'll access the methods directly or mock the class.
    
    sld = service._extract_sld(apex)
    tld = service._extract_tld(apex)
    
    print(f"Namecheap Extraction for apex '{apex}':")
    print(f"  SLD: '{sld}'")
    print(f"  TLD: '{tld}'")
    
    # Check if this matches Namecheap expectations
    # Namecheap expects SLD + TLD to equal the registered domain (apex)
    print(f"  Reconstructed: {sld}.{tld}")
    
    if f"{sld}.{tld}" != apex:
        print("  ❌ MISMATCH: Reconstructed domain does not match apex!")
    else:
        print("  ✅ MATCH")

# Test cases
domains = [
    "carlos.amasahistoricalsociety.space",
    "amasahistoricalsociety.space",
    "sub.example.co.uk",
    "example.com"
]

# We need to mock NamecheapConfig for NamecheapDNSService init
from unittest.mock import MagicMock
import sys
sys.modules['database'] = MagicMock()
sys.modules['database'].NamecheapConfig = MagicMock()

# Now run tests
if __name__ == "__main__":
    for d in domains:
        test_parsing(d)
