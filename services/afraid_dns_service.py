import requests
import re
import logging
from http.cookies import SimpleCookie

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
        self.auth_error = None
        self.cookies_str = ""
        self.last_error = None
        self._load_cookies(cookies_str)

    @staticmethod
    def normalize_cookie_string(cookies_str: str) -> str:
        """
        Accept either a raw Cookie header value or text copied from DevTools.
        Returns the "name=value; name2=value2" format FreeDNS expects.
        """
        if not cookies_str:
            return ""

        raw = cookies_str.strip()
        if raw.lower().startswith("cookie:"):
            raw = raw.split(":", 1)[1].strip()

        # DevTools sometimes copies request headers as multiple lines. Keep only
        # the Cookie header if a full header block was pasted.
        for line in raw.splitlines():
            if line.lower().startswith("cookie:"):
                raw = line.split(":", 1)[1].strip()
                break

        raw = raw.replace("\r", "").replace("\n", "; ")
        parsed = SimpleCookie()
        try:
            parsed.load(raw)
        except Exception:
            parsed = SimpleCookie()

        pairs = []
        if parsed:
            for name, morsel in parsed.items():
                if name and morsel.value:
                    pairs.append(f"{name}={morsel.value}")
        else:
            for part in raw.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, _, value = part.partition("=")
                name = name.strip()
                value = value.strip()
                if name and value:
                    pairs.append(f"{name}={value}")

        # Preserve order while dropping duplicate names.
        seen = set()
        normalized = []
        for pair in pairs:
            name = pair.split("=", 1)[0]
            if name in seen:
                continue
            seen.add(name)
            normalized.append(pair)
        return "; ".join(normalized)

    def _load_cookies(self, cookies_str: str):
        """Parse cookie string and load into session."""
        normalized = self.normalize_cookie_string(cookies_str)
        if not normalized:
            self.auth_error = "No valid cookie pairs were found. Paste the full Cookie request header, not just one cookie value."
            logger.error(self.auth_error)
            return

        try:
            self.cookies_str = normalized
            self.session.headers["Cookie"] = normalized

            # Also populate the cookie jar for any follow-up requests that
            # requests prepares without the explicit Cookie header.
            for part in normalized.split(";"):
                part = part.strip()
                if "=" in part:
                    name, _, value = part.partition("=")
                    name = name.strip()
                    value = value.strip()
                    self.session.cookies.set(name, value, domain="freedns.afraid.org", path="/")
                    self.session.cookies.set(name, value, domain=".freedns.afraid.org", path="/")

            # Verify cookies work by checking a protected page
            resp = self.session.get("https://freedns.afraid.org/subdomain/", allow_redirects=False, timeout=20)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "login page")
                self.auth_error = f"FreeDNS redirected to {location}; the cookies are expired or not for the logged-in FreeDNS session."
                logger.error(self.auth_error)
            elif resp.status_code == 200 and self._looks_logged_in(resp.text):
                self.logged_in = True
                logger.info("Cookie-based authentication to FreeDNS successful.")
            elif resp.status_code == 200:
                self.auth_error = self._extract_auth_error(resp.text)
                logger.error(self.auth_error)
            else:
                self.auth_error = f"FreeDNS returned unexpected HTTP {resp.status_code} while verifying cookies."
                logger.error(self.auth_error)
        except requests.RequestException as e:
            self.auth_error = f"Could not reach FreeDNS while verifying cookies: {e}"
            logger.error(self.auth_error)
        except Exception as e:
            self.auth_error = f"Error loading cookies into session: {e}"
            logger.error(self.auth_error)

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        return bool(
            re.search(r'<select[^>]*name=[\'"]?domain_id[\'"]?', html, re.IGNORECASE)
            or re.search(r'href=["\']/logout/?["\']', html, re.IGNORECASE)
            or "Logout" in html
        )

    @staticmethod
    def _extract_auth_error(html: str) -> str:
        title_m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
        if re.search(r"name=[\"']?username[\"']?", html, re.IGNORECASE) or "login" in title.lower():
            return "FreeDNS returned the login page; the cookies are invalid, expired, or copied from the wrong browser profile."
        if title:
            return f"FreeDNS returned a page that did not look logged in: {title}"
        return "FreeDNS returned HTTP 200, but the account domain controls were not found."

    def get_domains_with_ids(self):
        """Fetch all available domains and their IDs from the subdomain management page."""
        self.last_error = None
        if not self.logged_in:
            self.last_error = "get_domains_with_ids called but not authenticated."
            logger.error(self.last_error)
            return {}
        try:
            url = "https://freedns.afraid.org/subdomain/add.php"
            resp = self.session.get(url, allow_redirects=False, timeout=20)

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "login page")
                self.last_error = f"FreeDNS redirected {url} to {location}; the saved cookies cannot access the add-subdomain form."
                logger.error(self.last_error)
                return {}

            if resp.status_code != 200:
                self.last_error = f"FreeDNS returned HTTP {resp.status_code} for {url}."
                logger.error(self.last_error)
                return {}
            
            domain_map = {}
            select_block = re.search(
                r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>',
                resp.text, re.IGNORECASE | re.DOTALL
            )
            
            if select_block:
                pattern = r'<option\b[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>([^<]+)</option>'
                options = re.findall(pattern, select_block.group(1), re.IGNORECASE)
                for value, name in options:
                    clean_name = name.split()[0].strip()
                    if clean_name:
                        domain_map[clean_name.lower()] = value
                logger.info(f"Found {len(domain_map)} domain(s): {list(domain_map.keys())}")
            else:
                self.last_error = self._describe_unexpected_page(resp.text, url)
                logger.error(self.last_error)
                
            return domain_map
        except Exception as e:
            self.last_error = f"Error fetching Afraid domains: {e}"
            logger.error(self.last_error)
            return {}

    @staticmethod
    def _describe_unexpected_page(html, url):
        title_m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else "unknown title"
        text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        snippet = re.sub(r"\s+", " ", text).strip()[:300]
        if re.search(r"name=[\"']?username[\"']?", html, re.IGNORECASE) or "login" in title.lower():
            return f"FreeDNS returned the login page for {url}; copy a fresh full Cookie request header from Network."
        return f"FreeDNS page did not contain a domain_id dropdown for {url}. Title: {title}. Text: {snippet}"

    def get_domain_id(self, domain_name):
        """Resolve a FreeDNS domain name to its internal domain_id."""
        domain_name = (domain_name or "").strip().lower()
        if not domain_name:
            return None
        return self.get_domains_with_ids().get(domain_name)

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
            
            resp = self.session.post(url, data=payload, allow_redirects=False, timeout=20)
            
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
