import requests
import re
import logging
from http.cookies import SimpleCookie
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

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

    def fetch_account_domains(self):
        """Fetch the signed-in account's domain inventory from FreeDNS Domains."""
        self.last_error = None
        if not self.logged_in:
            self.last_error = "Account domains requested without an authenticated FreeDNS session."
            return []
        url = "https://freedns.afraid.org/domain/"
        try:
            pending_requests = [(url, 'get', None)]
            visited_requests = set()
            domains_by_name = {}
            first_page_html = ''
            while pending_requests and len(visited_requests) < 500:
                page_url, method, form_data = pending_requests.pop(0)
                request_key = (method, page_url, tuple(sorted((form_data or {}).items())))
                if request_key in visited_requests:
                    continue
                visited_requests.add(request_key)
                if method == 'post':
                    resp = self.session.post(page_url, data=form_data or {}, allow_redirects=False, timeout=20)
                else:
                    resp = self.session.get(page_url, allow_redirects=False, timeout=20)
                if resp.status_code in (301, 302, 303, 307, 308):
                    self.last_error = f"FreeDNS redirected the account Domains page to {resp.headers.get('Location', 'a login page')}."
                    return []
                if resp.status_code != 200:
                    self.last_error = f"FreeDNS returned HTTP {resp.status_code} for the account Domains page."
                    return []
                if not first_page_html:
                    first_page_html = resp.text
                for domain in self.parse_account_domains(resp.text):
                    previous = domains_by_name.get(domain['domain_name'], {})
                    domains_by_name[domain['domain_name']] = {
                        **previous,
                        **{key: value for key, value in domain.items() if value not in (None, '')},
                    }
                for next_request in self._account_domain_pagination_requests(resp.text, page_url):
                    next_key = (next_request[1], next_request[0], tuple(sorted((next_request[2] or {}).items())))
                    if next_key not in visited_requests and next_request not in pending_requests:
                        pending_requests.append(next_request)

            if pending_requests:
                self.last_error = 'FreeDNS account Domains pagination exceeded 500 pages.'
                return []
            domains = sorted(domains_by_name.values(), key=lambda domain: domain['domain_name'])
            if not domains and re.search(r"\b(?:domains|domain list)\b", re.sub(r'<[^>]+>', ' ', first_page_html), re.IGNORECASE):
                self.last_error = "FreeDNS loaded the account Domains page, but no domain rows could be parsed."
            return domains
        except requests.RequestException as exc:
            self.last_error = f"Could not load the FreeDNS account Domains page: {exc}"
            logger.error(self.last_error)
            return []
        except Exception as exc:
            self.last_error = f"Error reading FreeDNS account domains: {exc}"
            logger.error(self.last_error)
            return []

    @staticmethod
    def _account_domain_pagination_requests(html, current_url):
        """Find same-site account Domains pagination links and forms."""
        pagination_keys = {'page', 'page_num', 'p', 'start', 'offset', 'from', 'limit'}
        requests_to_make = []

        def add_page_url(candidate):
            resolved = urljoin(current_url, unescape(candidate))
            parsed = urlparse(resolved)
            if parsed.netloc.lower() not in {'freedns.afraid.org', 'www.freedns.afraid.org'}:
                return
            path = parsed.path.rstrip('/')
            if path not in {'/domain', '/domain/index.php'}:
                return
            if resolved != current_url:
                request_item = (resolved, 'get', None)
                if request_item not in requests_to_make:
                    requests_to_make.append(request_item)

        for _attributes, double_quoted, single_quoted, unquoted, anchor_html in re.findall(
            r'<a\b([^>]*?\bhref\s*=\s*)(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))[^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            anchor_text = re.sub(r'<[^>]+>', ' ', anchor_html)
            anchor_text = re.sub(r'\s+', ' ', unescape(anchor_text)).strip()
            href = double_quoted or single_quoted or unquoted
            resolved = urljoin(current_url, unescape(href))
            parsed = urlparse(resolved)
            if parsed.netloc.lower() not in {'freedns.afraid.org', 'www.freedns.afraid.org'}:
                continue
            if parsed.path.rstrip('/') not in {'/domain', '/domain/index.php'}:
                continue
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            has_page_parameter = any(key.lower() in pagination_keys for key in query)
            is_page_link = bool(re.fullmatch(r'\d+', anchor_text)) or bool(
                re.search(r'\b(?:next|previous|last|first)\b|[»›]', anchor_text, re.IGNORECASE)
            )
            if not has_page_parameter and not is_page_link:
                continue

            add_page_url(resolved)

        # Some FreeDNS layouts render pagination as a GET form with a hidden
        # current-page field instead of next/numbered links. Use its own page
        # count (when present) to request each page value.
        plain_text = re.sub(r'<[^>]+>', ' ', html)
        plain_text = re.sub(r'\s+', ' ', unescape(plain_text))
        pages_match = re.search(r'\bPage\s+\d+\s+of\s+(\d+)\b', plain_text, re.IGNORECASE)
        showing_match = re.search(
            r'\bShowing\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)\s+total\b',
            plain_text,
            re.IGNORECASE,
        )
        total_pages = int(pages_match.group(1)) if pages_match else 0
        page_size = 0
        total_rows = 0
        if showing_match:
            first_row, last_row, total_rows = (int(value.replace(',', '')) for value in showing_match.groups())
            page_size = max(1, last_row - first_row + 1)
            total_pages = max(total_pages, (total_rows + page_size - 1) // page_size)

        for form_attrs, form_html in re.findall(r'<form\b([^>]*)>(.*?)</form>', html, re.IGNORECASE | re.DOTALL):
            method_match = re.search(r'\bmethod\s*=\s*[\'\"]?([^\s\'\">]+)', form_attrs, re.IGNORECASE)
            method = method_match.group(1).lower() if method_match else 'get'
            if method not in {'get', 'post'}:
                continue
            action_match = re.search(r'\baction\s*=\s*(?:[\'\"]([^\'\"]+)[\'\"]|([^\s>]+))', form_attrs, re.IGNORECASE)
            action = (action_match.group(1) or action_match.group(2)) if action_match else current_url
            action_url = urljoin(current_url, unescape(action))
            action_parts = urlparse(action_url)
            if action_parts.netloc.lower() not in {'freedns.afraid.org', 'www.freedns.afraid.org'} or action_parts.path.rstrip('/') not in {'/domain', '/domain/index.php'}:
                continue

            form_values = dict(parse_qsl(action_parts.query, keep_blank_values=True))
            paging_fields = {}
            for input_attrs in re.findall(r'<input\b([^>]*)>', form_html, re.IGNORECASE | re.DOTALL):
                name_match = re.search(r'\bname\s*=\s*[\'\"]?([^\s\'\">]+)', input_attrs, re.IGNORECASE)
                value_match = re.search(r'\bvalue\s*=\s*[\'\"]([^\'\"]*)[\'\"]', input_attrs, re.IGNORECASE)
                if not name_match:
                    continue
                name = unescape(name_match.group(1))
                value = unescape(value_match.group(1)) if value_match else ''
                form_values[name] = value
                if name.lower() in pagination_keys:
                    paging_fields[name] = value

            for control_attrs in re.findall(r'<(?:button|input)\b([^>]*)>', form_html, re.IGNORECASE | re.DOTALL):
                name_match = re.search(r'\bname\s*=\s*[\'\"]?([^\s\'\">]+)', control_attrs, re.IGNORECASE)
                value_match = re.search(r'\bvalue\s*=\s*[\'\"]([^\'\"]*)[\'\"]', control_attrs, re.IGNORECASE)
                if not name_match or not value_match:
                    continue
                name = unescape(name_match.group(1))
                value = unescape(value_match.group(1))
                if name.lower() in pagination_keys and name not in paging_fields:
                    paging_fields[name] = [value]

            for select_match in re.finditer(r'<select\b([^>]*)>(.*?)</select>', form_html, re.IGNORECASE | re.DOTALL):
                name_match = re.search(r'\bname\s*=\s*[\'\"]?([^\s\'\">]+)', select_match.group(1), re.IGNORECASE)
                if not name_match:
                    continue
                name = unescape(name_match.group(1))
                if name.lower() in pagination_keys:
                    values = re.findall(r'<option\b[^>]*\bvalue\s*=\s*[\'\"]?([^\s\'\">]+)', select_match.group(2), re.IGNORECASE)
                    paging_fields[name] = values

            for name, current_value in paging_fields.items():
                if isinstance(current_value, list):
                    values = current_value
                elif name.lower() in {'page', 'page_num', 'p'} and total_pages:
                    values = [str(page) for page in range(1, total_pages + 1)]
                elif name.lower() in {'start', 'offset', 'from'} and total_pages and page_size:
                    values = [str(offset) for offset in range(0, total_rows, page_size)]
                else:
                    continue
                for value in values:
                    query = {**form_values, name: value}
                    if method == 'post':
                        request_item = (action_url, 'post', query)
                        if request_item not in requests_to_make:
                            requests_to_make.append(request_item)
                    else:
                        candidate = urlunparse(action_parts._replace(query=urlencode(query, doseq=True)))
                        add_page_url(candidate)
        return requests_to_make

    @staticmethod
    def parse_account_domains(html):
        """Read owned domains from the FreeDNS Domains page.

        FreeDNS has used more than one layout for this page. Older/current
        responses often expose each owned zone through a ``/subdomain/?limit``
        link, with the domain name in a preceding bold cell. Keep the table
        parser for layouts that provide proper rows, then fall back to those
        stable account-management links.
        """
        domains = []
        rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', html, re.IGNORECASE | re.DOTALL)
        domain_pattern = re.compile(
            r'(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?![\w.-])',
            re.IGNORECASE,
        )
        id_pattern = re.compile(r'(?:edit_domain_id|domain_id|data_id|\bid)\s*=\s*[\"\']?(\d+)', re.IGNORECASE)
        for row in rows:
            cells = re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', row, re.IGNORECASE | re.DOTALL)
            if not cells:
                continue
            cell_text = []
            for cell in cells:
                # FreeDNS has rendered the visibility label as text, an icon
                # title/alt, and (in some layouts) a class or image filename.
                # Preserve those hints before stripping the HTML tags.
                attributes = ' '.join(re.findall(
                    r'(?:title|alt|aria-label|data-status|class|src)\s*=\s*[\'\"]([^\'\"]+)',
                    cell,
                    re.IGNORECASE,
                ))
                text = re.sub(r'<[^>]+>', ' ', cell)
                cell_text.append(re.sub(r'\s+', ' ', unescape(text + ' ' + attributes)).strip())
            domain_name = next((
                match.group(0).lower()
                for cell in cell_text
                for match in [domain_pattern.search(cell)]
                if match
            ), None)
            if not domain_name:
                continue
            status = next((
                match.group(1).lower()
                for cell in cell_text
                for match in [re.search(r'\b(public|private)\b', cell, re.IGNORECASE)]
                if match
            ), '')
            id_match = id_pattern.search(row)
            domains.append({
                'domain_name': domain_name,
                'domain_id': id_match.group(1) if id_match else None,
                'status': status,
            })

        # FreeDNS' Domains page has also rendered its owned-zone list without
        # <tr> elements. In that layout the domain is shown in bold before a
        # link to its subdomain list. Match that relationship directly instead
        # of relying on table classes or a particular cell layout.
        owned_zone_pattern = re.compile(
            r'''["']2["']\s*>\s*<b\b[^>]*>\s*([^<]+?)\s*</b>(?:(?!["']2["']\s*>\s*<b\b).){0,2000}?'''
            r'''href\s*=\s*["']?/subdomain/\?limit=(\d+)''',
            re.IGNORECASE | re.DOTALL,
        )
        for match in owned_zone_pattern.finditer(html):
            domain_name = unescape(re.sub(r'<[^>]+>', ' ', match.group(1))).strip().lower().rstrip('.')
            if not domain_pattern.fullmatch(domain_name):
                continue
            # The status label is rendered near the domain name. Prefer a
            # nearby explicit status value, while allowing stealth domains to
            # remain unclassified (the UI only offers public/private lists).
            # Status metadata can appear immediately before the bold domain
            # (for example an icon in its own cell), so inspect both sides of
            # the owned-zone link. Keep this window local to avoid reading a
            # later account row's visibility label.
            row_start = html.lower().rfind('<tr', 0, match.start())
            previous_row_end = html.lower().rfind('</tr>', 0, match.start())
            if row_start > previous_row_end:
                row_end = html.lower().find('</tr>', match.end())
                nearby_start = row_start
                nearby_end = row_end + len('</tr>') if row_end >= 0 else min(len(html), match.end() + 500)
            else:
                # Non-table layouts place each domain in a short block. Bound
                # the context at common block separators so another domain's
                # status cannot be mistaken for this one.
                separators = ('<br', '<li', '<div', '<p', '</td>')
                starts = [html.lower().rfind(tag, 0, match.start()) for tag in separators]
                nearby_start = max([start for start in starts if start >= 0] or [max(0, match.start() - 250)])
                ends = [html.lower().find(tag, match.end()) for tag in separators]
                ends = [end for end in ends if end >= 0]
                nearby_end = min(ends) if ends else min(len(html), match.end() + 250)
            nearby_html = html[nearby_start:nearby_end]
            nearby_attributes = ' '.join(re.findall(
                r'(?:title|alt|aria-label|data-status|class|src)\s*=\s*[\'\"]([^\'\"]+)',
                nearby_html,
                re.IGNORECASE,
            ))
            nearby = re.sub(r'<[^>]+>', ' ', nearby_html)
            nearby = re.sub(r'\s+', ' ', unescape(nearby + ' ' + nearby_attributes)).strip()
            status_match = re.search(r'\b(public|private)\b', nearby, re.IGNORECASE)
            domains.append({
                'domain_name': domain_name,
                'domain_id': match.group(2),
                'status': status_match.group(1).lower() if status_match else '',
            })

        unique = {}
        for domain in domains:
            current = unique.get(domain['domain_name'])
            if not current or (not current.get('domain_id') and domain.get('domain_id')) or (not current.get('status') and domain.get('status')):
                unique[domain['domain_name']] = {**(current or {}), **domain}
        return sorted(unique.values(), key=lambda domain: domain['domain_name'])

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
        domains, _total_pages = self.fetch_registry_page_with_info(page_number)
        return domains

    def fetch_registry_page_with_info(self, page_number):
        """Fetch a registry page and discover the registry's current page count."""
        url = f"https://freedns.afraid.org/domain/registry/page-{page_number}.html"
        resp = self.session.get(url, allow_redirects=False, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"FreeDNS returned HTTP {resp.status_code} for {url}")
        return self.parse_registry_domains(resp.text), self.parse_registry_page_count(resp.text)

    @staticmethod
    def parse_registry_page_count(html):
        text = re.sub(r'<[^>]+>', ' ', html or '')
        text = re.sub(r'\s+', ' ', unescape(text)).strip()
        page_match = re.search(r'\bPage\s+\d+\s+of\s+([\d,]+)\b', text, re.IGNORECASE)
        if page_match:
            return int(page_match.group(1).replace(',', ''))
        showing_match = re.search(
            r'\bShowing\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)\s+total\b',
            text,
            re.IGNORECASE,
        )
        if showing_match:
            first_row, last_row, total_rows = (
                int(value.replace(',', '')) for value in showing_match.groups()
            )
            page_size = max(1, last_row - first_row + 1)
            return (total_rows + page_size - 1) // page_size
        linked_pages = re.findall(r'/domain/registry/page-(\d+)\.html', html or '', re.IGNORECASE)
        return max((int(page) for page in linked_pages), default=None)

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
            rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', resp.text, re.IGNORECASE | re.DOTALL)
            for row in rows:
                checkbox_tag = re.search(r'<input[^>]+type=["\']?checkbox["\']?[^>]*>', row, re.IGNORECASE)
                if not checkbox_tag:
                    continue
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.IGNORECASE | re.DOTALL)
                if len(cells) < 2:
                    continue
                delete_name = ''
                delete_value = ''
                if checkbox_tag:
                    name_match = re.search(r'\bname=["\']?([^"\'>\s]+)', checkbox_tag.group(0), re.IGNORECASE)
                    value_match = re.search(r'\bvalue=["\']?([^"\'>\s]+)', checkbox_tag.group(0), re.IGNORECASE)
                    delete_name = name_match.group(1) if name_match else 'delete[]'
                    delete_value = value_match.group(1) if value_match else ''

                clean_cells = []
                for cell in cells:
                    text = re.sub(r'<[^>]+>', ' ', cell)
                    clean_cells.append(re.sub(r'\s+', ' ', unescape(text)).strip())

                fqdn = ''
                link_texts = re.findall(r'<a\b[^>]*>(.*?)</a>', row, re.IGNORECASE | re.DOTALL)
                for link_text in link_texts:
                    clean_link = re.sub(r'<[^>]+>', ' ', link_text)
                    clean_link = re.sub(r'\s+', ' ', unescape(clean_link)).strip()
                    if re.match(r'^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$', clean_link, re.IGNORECASE):
                        fqdn = clean_link.lower()
                        break
                if not fqdn:
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
            payload = [('submit', 'delete selected')]
            for record in records:
                name = record.get('delete_name') or 'delete[]'
                value = record.get('delete_value')
                if name and value:
                    payload.append((name, value))
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

                page_text = re.sub(r'<script\b.*?</script>', ' ', resp.text, flags=re.IGNORECASE | re.DOTALL)
                page_text = re.sub(r'<style\b.*?</style>', ' ', page_text, flags=re.IGNORECASE | re.DOTALL)
                page_text = re.sub(r'<[^>]+>', ' ', page_text)
                page_text = re.sub(r'\s+', ' ', unescape(page_text)).strip()
                if re.search(r'no more subdomain capacity|subdomain capacity allocated|more hostnames', page_text, re.IGNORECASE):
                    return False, page_text[:500]

            return True, f"{subdomain} creation submitted."
        except Exception as e:
            logger.error(f"Exception adding Afraid CNAME: {e}")
            return False, f"Exception: {str(e)}"
