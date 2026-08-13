import requests
import re
import logging

logger = logging.getLogger(__name__)

class AfraidDNSService:
    def __init__(self, cookies_str: str):
        """
        Initialize with a raw cookie string copied from browser DevTools.
        Example: "dns_id=abc123; dns_cookie=xyz456; ..."
        """
        self.session = requests.Session()
        self.session.headers.update({
            "Host": "freedns.afraid.org",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:102.0) Gecko/20100101 Firefox/102.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        })
        self.logged_in = False
        self._load_cookies(cookies_str)

    def _load_cookies(self, cookies_str: str):
        """Parse cookie string and load into session."""
        if not cookies_str or not cookies_str.strip():
            logger.error("No cookies provided.")
            return

        try:
            # Parse "key=value; key2=value2; ..." format
            for part in cookies_str.split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    self.session.cookies.set(name.strip(), value.strip(), domain="freedns.afraid.org")

            # Verify cookies work by checking a protected page
            resp = self.session.get("https://freedns.afraid.org/subdomain/", allow_redirects=False)
            if resp.status_code == 302:
                logger.error("Cookies are expired or invalid - got redirect to login page.")
            elif resp.status_code == 200:
                self.logged_in = True
                logger.info("Cookie-based authentication to FreeDNS successful.")
            else:
                logger.error(f"Unexpected status {resp.status_code} when verifying cookies.")
        except Exception as e:
            logger.error(f"Error loading cookies into session: {e}")

    def get_domains_with_ids(self):
        """Fetch all available domains and their IDs from the subdomain management page."""
        if not self.logged_in:
            logger.error("get_domains_with_ids called but not authenticated.")
            return {}
        try:
            resp = self.session.get("https://freedns.afraid.org/subdomain/")
            
            domain_map = {}
            select_block = re.search(
                r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>',
                resp.text, re.IGNORECASE | re.DOTALL
            )
            
            if select_block:
                pattern = r'<option\s+[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>'
                options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
                for value, name in options:
                    clean_name = name.split()[0].strip()
                    domain_map[clean_name] = value
                logger.info(f"Found {len(domain_map)} domain(s): {list(domain_map.keys())}")
            else:
                logger.error(f"Could not find domain_id select. HTML snippet: {resp.text[:400]}")
                
            return domain_map
        except Exception as e:
            logger.error(f"Error fetching Afraid domains: {e}")
            return {}

    def add_cname(self, subdomain, domain_id, destination, ttl=300):
        if not self.logged_in:
            return False, "Not authenticated. Please re-import cookies."
        
        try:
            url = "https://freedns.afraid.org/subdomain/save.php?step=2"
            payload = {
                "type": "CNAME",
                "subdomain": subdomain,
                "domain_id": domain_id,
                "address": destination,
                "ttlalias": "",
                "ref": "",
                "send": "Save!"
            }
            
            resp = self.session.post(url, data=payload, allow_redirects=False)
            
            if resp.status_code == 302:
                return True, f"{subdomain} created successfully."
                
            if resp.status_code == 200:
                # Check for error messages on the page
                error_m = re.search(
                    r'bgcolor=["\']?#eeeeee["\']?[^>]*>(.*?)</td>',
                    resp.text, re.IGNORECASE | re.DOTALL
                )
                if error_m:
                    msg = re.sub(r'<[^>]+>', '', error_m.group(1)).strip()
                    if msg:
                        return False, msg

            return True, f"{subdomain} creation submitted."
        except Exception as e:
            logger.error(f"Exception adding Afraid CNAME: {e}")
            return False, f"Exception: {str(e)}"
