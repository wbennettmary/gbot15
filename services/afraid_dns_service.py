import requests
import re
import logging

logger = logging.getLogger(__name__)

class AfraidDNSService:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.session = requests.Session()
        # Set a common user agent
        self.session.headers.update({
            "Host": "freedns.afraid.org",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:102.0) Gecko/20100101 Firefox/102.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        })
        self.logged_in = False
        self._login()

    def _login(self):
        try:
            # Step 1: GET login page to grab session cookies + any hidden fields
            login_page = self.session.get("https://freedns.afraid.org/zc.php?step=1", allow_redirects=True)
            
            # Scrape hidden input fields (CSRF tokens, session IDs, etc.)
            hidden_fields = re.findall(r'<input[^>]+type=["\']?hidden["\']?[^>]*>', login_page.text, re.IGNORECASE)
            form_data = {}
            for field in hidden_fields:
                name_m = re.search(r'name=["\']([^"\']+)["\']', field)
                value_m = re.search(r'value=["\']([^"\']*)["\']', field)
                if name_m:
                    form_data[name_m.group(1)] = value_m.group(1) if value_m else ''

            # Step 2: POST login with credentials + hidden fields
            payload = {
                **form_data,
                "username": self.username,
                "password": self.password,
                "remember": "1",
                "submit": "Login",
                "remote": "",
                "from": "",
                "action": "auth"
            }
            
            resp = self.session.post(
                "https://freedns.afraid.org/zc.php?step=2",
                data=payload,
                allow_redirects=False
            )
            
            # FreeDNS returns HTTP 302 redirect ONLY on successful login.
            if resp.status_code == 302:
                self.logged_in = True
                logger.info("Successfully logged in to Afraid (FreeDNS).")
            else:
                error_m = re.search(r'bgcolor=["\']?#eeeeee["\']?[^>]*>(.*?)</td>', resp.text, re.IGNORECASE | re.DOTALL)
                if error_m:
                    msg = re.sub(r'<[^>]+>', '', error_m.group(1)).strip()
                    logger.error(f"FreeDNS login rejected: {msg}")
                else:
                    logger.error(f"FreeDNS login failed. Status: {resp.status_code}. HTML: {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Error logging into Afraid: {e}")

    def get_domains_with_ids(self):
        """Fetch all available domains and their IDs from the add subdomain page."""
        if not self.logged_in:
            logger.error("get_domains_with_ids called but not logged in.")
            return {}
        try:
            resp = self.session.get("https://freedns.afraid.org/subdomain/")
            
            domain_map = {}
            # Match options in domain_id select, handle single/double quotes or no quotes
            select_block = re.search(r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>', resp.text, re.IGNORECASE | re.DOTALL)
            
            if select_block:
                pattern = r'<option\s+[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>'
                options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
                for value, name in options:
                    clean_name = name.split()[0].strip()  # Remove (public) or (private)
                    domain_map[clean_name] = value
            else:
                logger.error(f"Could not find domain_id select in edit.php. HTML snippet: {resp.text[:500]}")
                
            return domain_map
        except Exception as e:
            logger.error(f"Error fetching Afraid domains: {e}")
            return {}

    def add_cname(self, subdomain, domain_id, destination, ttl=300):
        if not self.logged_in:
            return False, "Not logged in to Afraid."
        
        try:
            url = "https://freedns.afraid.org/subdomain/save.php?step=2"
            payload = {
                "type": "CNAME",
                "subdomain": subdomain,
                "domain_id": domain_id,
                "address": destination,
                "ttlalias": "For our premium supporters",  # Default or optional field
                "ref": "",
                "send": "Save!"
            }
            
            resp = self.session.post(url, data=payload, allow_redirects=False)
            
            if resp.status_code == 302:
                return True, f"Subdomain {subdomain} created successfully (redirected)."
                
            if "already exists" in resp.text:
                return False, "Subdomain already exists."
                
            # If no success message, check for errors
            error_match = re.search(r'<div class="error[^>]*>(.*?)</div>', resp.text, re.IGNORECASE | re.DOTALL)
            if error_match:
                return False, re.sub(r'<[^>]+>', '', error_match.group(1)).strip()
                
            return True, "Subdomain creation requested, but success message not explicitly found."
        except Exception as e:
            logger.error(f"Exception adding Afraid CNAME: {e}")
            return False, f"Exception: {str(e)}"
