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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
        })
        self.logged_in = False
        self._login()

    def _login(self):
        try:
            # Get the login page first to establish cookies
            self.session.get("https://freedns.afraid.org/")
            
            login_url = "https://freedns.afraid.org/zc.php?step=2"
            payload = {
                "action": "login",
                "username": self.username,
                "password": self.password,
                "submit": "Login"
            }
            resp = self.session.post(login_url, data=payload)
            if "Logout" in resp.text or "Welcome" in resp.text or "freedns.afraid.org/logout" in resp.text:
                self.logged_in = True
                logger.info("Successfully logged in to Afraid (FreeDNS).")
            else:
                logger.error("Failed to log in to Afraid. Check credentials.")
        except Exception as e:
            logger.error(f"Error logging into Afraid: {e}")

    def get_domains_with_ids(self):
        """Fetch all available domains and their IDs from the add subdomain page."""
        if not self.logged_in:
            return {}
        try:
            resp = self.session.get("https://freedns.afraid.org/subdomain/edit.php")
            
            # The dropdown looks like: <select name="domain_id"><option value="12345">domain.com (public)</option>
            # Let's use regex to find them
            domain_map = {}
            # Match options in domain_id select
            # It might look like: <option value="99999" >artitech.com (public)</option>
            pattern = r'<option\s+value="(\d+)"[^>]*>([^<]+)</option>'
            
            # Narrow down to the select tag if possible, or just find all options
            select_block = re.search(r'<select[^>]*name="domain_id"[^>]*>(.*?)</select>', resp.text, re.IGNORECASE | re.DOTALL)
            if select_block:
                options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
                for value, name in options:
                    clean_name = name.split()[0]  # Remove (public) or (private)
                    domain_map[clean_name] = value
            return domain_map
        except Exception as e:
            logger.error(f"Error fetching Afraid domains: {e}")
            return {}

    def add_cname(self, subdomain, domain_id, destination, ttl=300):
        if not self.logged_in:
            return False, "Not logged in to Afraid."
        
        try:
            url = "https://freedns.afraid.org/subdomain/save.php"
            payload = {
                "action": "edit",
                "type": "CNAME",
                "subdomain": subdomain,
                "domain_id": domain_id,
                "address": destination,
                "ttl": str(ttl),
                "submit": "Save!"
            }
            
            # Afraid uses action=edit for adding?
            # Usually the URL is /subdomain/save.php?step=2
            # Let's check typical form submission on FreeDNS
            # It posts to save.php
            
            resp = self.session.post(url, data=payload)
            if "has been created" in resp.text or "Updated" in resp.text or "successfully" in resp.text.lower():
                return True, f"Subdomain {subdomain} created successfully."
            
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
