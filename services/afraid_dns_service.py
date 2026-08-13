import requests
import re
import logging
from http.cookies import SimpleCookie
from html import unescape

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
        self.last_delete_url = "https://freedns.afraid.org/subdomain/delete2.php"
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
        """Fetch available add-subdomain domains and their internal IDs."""
        self.last_error = None
        if not self.logged_in:
            self.last_error = "get_domains_with_ids called but not authenticated."
            logger.error(self.last_error)
            return {}
        try:
            url = "https://freedns.afraid.org/subdomain/edit.php"
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
            
            domain_map = self._parse_domain_select(resp.text)
            if domain_map:
                logger.info(f"Found {len(domain_map)} add-subdomain domain(s): {list(domain_map.keys())}")
            else:
                self.last_error = self._describe_unexpected_page(resp.text, url)
                logger.error(self.last_error)
                
            return domain_map
        except Exception as e:
            self.last_error = f"Error fetching Afraid domains: {e}"
            logger.error(self.last_error)
            return {}

    @staticmethod
    def _parse_domain_select(html):
        domain_map = {}
        select_block = re.search(
            r'<select[^>]*name=[\'"]?domain_id[\'"]?[^>]*>(.*?)</select>',
            html,
            re.IGNORECASE | re.DOTALL
        )
        if not select_block:
            return domain_map

        option_pattern = re.compile(
            r'<option\b[^>]*value=[\'"]?(\d+)[\'"]?[^>]*>(.*?)</option>',
            re.IGNORECASE | re.DOTALL
        )
        for value, label in option_pattern.findall(select_block.group(1)):
            clean_name = re.sub(r'<[^>]+>', ' ', label)
            clean_name = unescape(clean_name)
            clean_name = re.sub(r'\([^)]*\)', ' ', clean_name)
            clean_name = re.sub(r'\s+', ' ', clean_name).strip().lower()
            if clean_name and "." in clean_name:
                domain_map[clean_name] = value
        return domain_map

    def fetch_registry_page(self, page_number):
        """Fetch and parse one FreeDNS public registry page."""
        url = f"https://freedns.afraid.org/domain/registry/page-{page_number}.html"
        resp = self.session.get(url, allow_redirects=False, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"FreeDNS returned HTTP {resp.status_code} for {url}")
        return self.parse_registry_domains(resp.text)

    @staticmethod
    def parse_registry_domains(html):
        domains = []
        row_pattern = re.compile(r'<tr[^>]*class=["\']?tr[ld]["\']?[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
        link_pattern = re.compile(
            r'href=["\']?/subdomain/edit\.php\?edit_domain_id=(\d+)["\']?[^>]*>\s*([^<]+?)\s*</a>',
            re.IGNORECASE | re.DOTALL
        )
        cell_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)

        for row in row_pattern.findall(html):
            link = link_pattern.search(row)
            if not link:
                continue
            domain_id, domain_name = link.groups()
            domain_name = unescape(domain_name).strip().lower()
            cells = cell_pattern.findall(row)
            status = ''
            owner = ''
            hosts_in_use = None
            age_text = ''
            created_on = ''
            if len(cells) > 1:
                status = re.sub(r'<[^>]+>', ' ', cells[1])
                status = re.sub(r'\s+', ' ', unescape(status)).strip().lower()
            if len(cells) > 2:
                owner = re.sub(r'<[^>]+>', ' ', cells[2])
                owner = re.sub(r'\s+', ' ', unescape(owner)).strip()
            if cells:
                hosts_match = re.search(r'\(([\d,]+)\s+hosts?\s+in\s+use\)', cells[0], re.IGNORECASE)
                if hosts_match:
                    hosts_in_use = int(hosts_match.group(1).replace(',', ''))
            if len(cells) > 3:
                age_text = re.sub(r'<[^>]+>', ' ', cells[3])
                age_text = re.sub(r'\s+', ' ', unescape(age_text)).strip()
                date_match = re.search(r'\(([^)]+)\)', age_text)
                if date_match:
                    created_on = date_match.group(1)
            if domain_name and "." in domain_name:
                domains.append({
                    'domain_name': domain_name,
                    'domain_id': domain_id,
                    'tld': domain_name.rsplit('.', 1)[-1].lower(),
                    'status': status,
                    'owner': owner,
                    'hosts_in_use': hosts_in_use,
                    'age_text': age_text,
                    'created_on': created_on,
                })
        return domains

    def get_existing_subdomains(self):
        """Parse current FreeDNS subdomain records from the account page."""
        self.last_error = None
        url = "https://freedns.afraid.org/subdomain/"
        try:
            resp = self.session.get(url, allow_redirects=False, timeout=20)
            if resp.status_code in (301, 302, 303, 307, 308):
                self.last_error = f"FreeDNS redirected {url} to {resp.headers.get('Location', 'login page')}."
                return []
            if resp.status_code != 200:
                self.last_error = f"FreeDNS returned HTTP {resp.status_code} for {url}."
                return []

            action_match = re.search(r'<form[^>]+action=["\']?([^"\'>\s]+)', resp.text, re.IGNORECASE)
            if action_match:
                action = unescape(action_match.group(1))
                if 'delete' in action.lower() or 'subdomain' in action.lower():
                    if action.startswith('/'):
                        self.last_delete_url = f"https://freedns.afraid.org{action}"
                    elif action.startswith('http'):
                        self.last_delete_url = action

            records = []
            rows = re.findall(r'<tr[^>]*class=["\']?tr[ld]["\']?[^>]*>(.*?)</tr>', resp.text, re.IGNORECASE | re.DOTALL)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.IGNORECASE | re.DOTALL)
                if len(cells) < 2:
                    continue
                checkbox_tag = re.search(r'<input[^>]+type=["\']?checkbox["\']?[^>]*>', row, re.IGNORECASE)
                delete_name = ''
                delete_value = ''
                if checkbox_tag:
                    name_match = re.search(r'\bname=["\']?([^"\'>\s]+)', checkbox_tag.group(0), re.IGNORECASE)
                    value_match = re.search(r'\bvalue=["\']?([^"\'>\s]+)', checkbox_tag.group(0), re.IGNORECASE)
                    delete_name = name_match.group(1) if name_match else ''
                    delete_value = value_match.group(1) if value_match else ''

                clean_cells = []
                for cell in cells:
                    text = re.sub(r'<[^>]+>', ' ', cell)
                    clean_cells.append(re.sub(r'\s+', ' ', unescape(text)).strip())

                fqdn = ''
                fqdn_match = re.search(r'([a-z0-9][a-z0-9.-]+\.[a-z]{2,})', ' '.join(clean_cells), re.IGNORECASE)
                if fqdn_match:
                    fqdn = fqdn_match.group(1).lower()

                record_type = ''
                destination = ''
                for index, text in enumerate(clean_cells):
                    if text.upper() in {'A', 'AAAA', 'CNAME', 'MX', 'TXT', 'NS'}:
                        record_type = text.upper()
                        if index + 1 < len(clean_cells):
                            destination = clean_cells[index + 1]
                        break

                if fqdn and delete_value:
                    records.append({
                        'fqdn': fqdn,
                        'type': record_type,
                        'destination': destination,
                        'delete_name': delete_name,
                        'delete_value': delete_value,
                    })
            if not records:
                self.last_error = self._describe_unexpected_page(resp.text, url)
            return records
        except Exception as e:
            self.last_error = f"Error fetching existing FreeDNS subdomains: {e}"
            logger.error(self.last_error)
            return []

    def delete_subdomains(self, records):
        """Delete selected records from FreeDNS subdomain page."""
        if not records:
            return True, "No records selected."
        try:
            payload = {'submit': 'delete selected'}
            for record in records:
                name = record.get('delete_name')
                value = record.get('delete_value')
                if name and value:
                    payload[name] = value
            resp = self.session.post(self.last_delete_url, data=payload, allow_redirects=False, timeout=20)
            if resp.status_code in (200, 302):
                return True, f"Submitted deletion for {len(records)} subdomain(s)."
            return False, f"FreeDNS returned HTTP {resp.status_code} while deleting subdomains."
        except Exception as e:
            logger.error(f"Exception deleting Afraid subdomains: {e}")
            return False, f"Exception: {str(e)}"

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

        owned_domain_id = self.get_domains_with_ids().get(domain_name)
        if owned_domain_id:
            return owned_domain_id

        return self.get_public_registry_domain_id(domain_name)

    def get_public_registry_domain_id(self, domain_name):
        """Resolve public registry domains such as chickenkiller.com to domain_id."""
        self.last_error = None
        domain_name = (domain_name or "").strip().lower()
        if not domain_name:
            return None

        try:
            url = "https://freedns.afraid.org/domain/registry/"
            resp = self.session.get(url, allow_redirects=False, timeout=20)

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "login page")
                self.last_error = f"FreeDNS redirected registry lookup to {location}; copy a fresh full Cookie request header from Network."
                logger.error(self.last_error)
                return None

            if resp.status_code != 200:
                self.last_error = f"FreeDNS returned HTTP {resp.status_code} while looking up '{domain_name}' in the registry."
                logger.error(self.last_error)
                return None

            escaped_domain = re.escape(domain_name)
            patterns = [
                rf'href=["\']?/subdomain/edit\.php\?edit_domain_id=(\d+)["\']?[^>]*>\s*{escaped_domain}\s*</a>',
                rf'<a[^>]+href=["\'][^"\']*edit_domain_id=(\d+)[^"\']*["\'][^>]*>\s*{escaped_domain}\s*</a>',
            ]
            for pattern in patterns:
                match = re.search(pattern, resp.text, re.IGNORECASE)
                if match:
                    return match.group(1)

            if re.search(rf'\b{escaped_domain}\b', resp.text, re.IGNORECASE):
                self.last_error = f"FreeDNS registry found '{domain_name}', but it is not exposed as an attachable public domain."
            else:
                self.last_error = f"FreeDNS registry did not find '{domain_name}'. Check the spelling or choose a public FreeDNS registry domain."
            logger.error(self.last_error)
            return None

        except Exception as e:
            self.last_error = f"Error looking up '{domain_name}' in FreeDNS registry: {e}"
            logger.error(self.last_error)
            return None

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
