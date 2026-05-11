import sys
import os

# Mock logger
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add current directory to path
sys.path.append(os.getcwd())

try:
    import publicsuffix2
    print(f"publicsuffix2 version: {publicsuffix2.__file__}")
except ImportError:
    print("publicsuffix2 not installed")

def extract_tld(domain):
    try:
        psl = publicsuffix2.PublicSuffixList()
        suffix = psl.get_public_suffix(domain)
        return suffix
    except Exception as e:
        print(f"Error: {e}")
        return None

def extract_sld(domain):
    try:
        psl = publicsuffix2.PublicSuffixList()
        suffix = psl.get_public_suffix(domain)
        
        if not suffix:
            parts = domain.split('.')
            if len(parts) >= 2:
                return parts[-2]
            return parts[0]
        
        if domain == suffix:
            return domain.split('.')[0]
            
        prefix = domain[:-len(suffix)].rstrip('.')
        prefix_parts = prefix.split('.')
        return prefix_parts[-1]
    except Exception as e:
        print(f"Error: {e}")
        return None

domain = "estifania.amasahistoricalsociety.space"
apex = "amasahistoricalsociety.space"

print(f"Domain: {domain}")
print(f"TLD (from domain): {extract_tld(domain)}")
print(f"SLD (from domain): {extract_sld(domain)}")

print(f"Apex: {apex}")
print(f"TLD (from apex): {extract_tld(apex)}")
print(f"SLD (from apex): {extract_sld(apex)}")
