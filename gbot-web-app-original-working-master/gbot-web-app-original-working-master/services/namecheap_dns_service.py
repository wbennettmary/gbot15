"""
Namecheap DNS management service.
Handles DNS record retrieval and TXT record creation/updates.
"""
import logging
import requests
from typing import List, Dict, Optional
from database import NamecheapConfig

logger = logging.getLogger(__name__)

class HostRecord:
    """Represents a DNS host record."""
    def __init__(self, host: str, record_type: str, address: str, mx_pref: int = None, ttl: int = 300):
        self.host = host
        self.record_type = record_type
        self.address = address
        self.mx_pref = mx_pref
        self.ttl = ttl
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for Namecheap API."""
        result = {
            'HostName': self.host,
            'RecordType': self.record_type,
            'Address': self.address,
            'TTL': self.ttl
        }
        if self.mx_pref is not None:
            result['MXPref'] = self.mx_pref
        return result

class NamecheapDNSService:
    """Service for managing Namecheap DNS records."""
    
    BASE_URL = "https://api.namecheap.com/xml.response"
    
    def __init__(self):
        """Initialize service with credentials from database."""
        self._config = None
        self._load_config()
    
    def _load_config(self):
        """Load Namecheap configuration from database."""
        try:
            config = NamecheapConfig.query.filter_by(is_configured=True).first()
            if not config:
                raise Exception("Namecheap configuration not found. Please configure in Settings.")
            
            self._config = config
            logger.info("Namecheap configuration loaded")
        
        except Exception as e:
            logger.error(f"Error loading Namecheap config: {e}")
            raise
    
    def _make_request(self, command: str, extra_params: Dict = None) -> Dict:
        """
        Make API request to Namecheap.
        
        Args:
            command: API command name
            extra_params: Additional parameters
        
        Returns:
            Parsed XML response as dict (simplified - returns raw response for now)
        """
        if not self._config:
            raise Exception("Namecheap configuration not loaded")
        
        params = {
            'ApiUser': self._config.api_user,
            'ApiKey': self._config.api_key,
            'UserName': self._config.username,
            'Command': command,
            'ClientIp': self._config.client_ip
        }
        
        if extra_params:
            params.update(extra_params)
        
        try:
            # Use POST to avoid 414 Request-URI Too Long errors with large host lists
            response = requests.post(self.BASE_URL, data=params, timeout=30)
            response.raise_for_status()
            
            # Parse XML response
            # Remove namespaces to simplify parsing
            response_text = response.text
            import re
            response_text = re.sub(r' xmlns="[^"]+"', '', response_text, count=1)
            
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(response_text)
            except ET.ParseError as parse_error:
                logger.error(f"Failed to parse XML response: {parse_error}")
                logger.error(f"Response text (first 500 chars): {response.text[:500]}")
                raise Exception(f"Invalid XML response from Namecheap API: {str(parse_error)}")
            
            # Check for errors in response
            errors = root.findall('.//Error')
            if errors:
                error_messages = []
                for error_elem in errors:
                    error_text = error_elem.text or error_elem.get('Number', '') or 'Unknown error'
                    error_num = error_elem.get('Number', '')
                    if error_num:
                        error_messages.append(f"[{error_num}] {error_text}")
                    else:
                        error_messages.append(error_text)
                
                full_error = ', '.join(error_messages) if error_messages else 'Unknown error'
                logger.error(f"Namecheap API returned errors: {full_error}")
                logger.error(f"Full XML response: {response.text}")
                raise Exception(f"Namecheap API error: {full_error}. Raw response: {response.text}")
            
            # Check Status attribute
            status = root.get('Status', '').upper()
            if status and status != 'OK':
                logger.warning(f"Namecheap API status is not OK: {status}")
                logger.warning(f"Raw XML response for non-OK status: {response.text}")
                # Don't fail if status is not OK, but log it
            
            # Check for empty CommandResponse (silent failure)
            command_response = root.find(f'.//CommandResponse')
            if command_response is not None and not list(command_response):
                # It's empty. For setHosts, this might be OK, but for others it's suspicious.
                pass

            # Log successful response (first 200 chars for debugging)
            logger.debug(f"Namecheap API response Status: {root.get('Status', 'N/A')}")
            logger.debug(f"Response XML (first 200 chars): {response.text[:200]}")
            
            return {'success': True, 'xml': root, 'raw': response.text}
        
        except requests.RequestException as e:
            logger.error(f"Namecheap API request failed: {e}")
            raise Exception(f"Namecheap API request failed: {str(e)}")
    
    def get_hosts(self, apex: str) -> List[HostRecord]:
        """
        Get all DNS records for a domain.
        
        Args:
            apex: Apex domain (zone name)
        
        Returns:
            List of HostRecord objects
        """
        try:
            result = self._make_request('namecheap.domains.dns.getHosts', {
                'SLD': self._extract_sld(apex),
                'TLD': self._extract_tld(apex)
            })
            
            root = result['xml']
            hosts = []
            
            # Parse host records from XML
            # Namecheap API returns hosts in <host> elements with attributes
            # Format: <host Name="@" Type="A" Address="1.2.3.4" MXPref="10" TTL="300" />
            for host_elem in root.findall('.//host'):
                # Try both attribute access methods
                host = host_elem.get('Name') or host_elem.attrib.get('Name', '@')
                record_type = host_elem.get('Type') or host_elem.attrib.get('Type', '')
                address = host_elem.get('Address') or host_elem.attrib.get('Address', '')
                mx_pref = host_elem.get('MXPref') or host_elem.attrib.get('MXPref')
                ttl_str = host_elem.get('TTL') or host_elem.attrib.get('TTL', '300')
                
                # Skip empty records
                if not record_type or not address:
                    continue
                
                try:
                    ttl = int(ttl_str) if ttl_str else 300
                except (ValueError, TypeError):
                    ttl = 300
                
                hosts.append(HostRecord(
                    host=host or '@',
                    record_type=record_type,
                    address=address,
                    mx_pref=int(mx_pref) if mx_pref and str(mx_pref).isdigit() else None,
                    ttl=ttl
                ))
            
            logger.info(f"Retrieved {len(hosts)} DNS records for {apex}")
            return hosts
        
        except Exception as e:
            logger.error(f"Error getting hosts for {apex}: {e}")
            raise
    
    def upsert_txt_record(self, apex: str, host: str, value: str, ttl: int = 1799) -> Dict:
        """
        Create or update TXT record, preserving all existing records.
        
        Args:
            apex: Apex domain (zone name)
            host: Host name ('@' for apex)
            value: TXT record value
            ttl: TTL in seconds (default 1799/Automatic)
        
        Returns:
            Dict with 'updated' (bool)
        """
        try:
            # Get all existing records
            existing_hosts = self.get_hosts(apex)
            
            if not existing_hosts:
                logger.warning(f"No existing DNS records found for {apex}. This might be a new domain or an API error.")
                # We proceed, but logging this is important.
            
            # Check if TXT record with same host and value already exists
            for record in existing_hosts:
                if record.host == host and record.record_type == 'TXT' and record.address == value:
                    logger.info(f"TXT record already exists for {apex} @ {host} with value {value}")
                    return {'updated': True, 'action': 'no-op', 'message': 'Record already exists'}
            
            # Create updated host list
            updated_hosts = []
            
            # Preserve existing records, but replace old Google verification tokens
            for record in existing_hosts:
                should_keep = True
                
                # If we are adding a TXT record
                if record.host == host and record.record_type == 'TXT':
                    # If we are adding a Google verification token, remove any existing Google tokens for this host
                    if value.startswith('google-site-verification=') and record.address.startswith('google-site-verification='):
                        logger.info(f"Replacing existing Google verification token: {record.address}")
                        should_keep = False
                
                if should_keep:
                    updated_hosts.append(record)
            
            # Add new TXT record
            new_txt = HostRecord(host=host, record_type='TXT', address=value, ttl=ttl)
            updated_hosts.append(new_txt)
            
            # Convert to Namecheap API format
            sld = self._extract_sld(apex)
            tld = self._extract_tld(apex)
            
            # Build host list parameter (Namecheap expects specific format)
            host_list = []
            for i, record in enumerate(updated_hosts, start=1):
                host_list.append(f"{record.host},{record.record_type},{record.address},{record.ttl}")
                if record.mx_pref is not None:
                    host_list[-1] += f",{record.mx_pref}"
            
            # Set hosts via API
            params = {
                'SLD': sld,
                'TLD': tld
            }
            
            # Namecheap API expects hosts in specific numbered format
            # Format: HostName1, RecordType1, Address1, TTL1, MXPref1 (optional), ...
            for i, record in enumerate(updated_hosts, start=1):
                params[f'HostName{i}'] = record.host
                params[f'RecordType{i}'] = record.record_type
                params[f'Address{i}'] = record.address
                params[f'TTL{i}'] = str(record.ttl)
                if record.mx_pref is not None:
                    params[f'MXPref{i}'] = str(record.mx_pref)
            
            logger.info(f"Setting hosts for {apex} (SLD={sld}, TLD={tld})")
            logger.info(f"Parameters sent to Namecheap: {params}")
            
            result = self._make_request('namecheap.domains.dns.setHosts', params)
            
            logger.info(f"Successfully updated DNS records for {apex}, added TXT record @ {host}")
            return {'updated': True, 'action': 'added', 'message': 'TXT record added'}
        
        except Exception as e:
            logger.error(f"Error upserting TXT record for {apex}: {e}")
            raise
    
    def _extract_sld(self, domain: str) -> str:
        """
        Extract second-level domain (e.g., 'example' from 'example.com').
        For complex TLDs like 'co.uk', extracts correctly.
        """
        try:
            import publicsuffix2
            psl = publicsuffix2.PublicSuffixList()
            
            # Use publicsuffix2 to get the public suffix (TLD)
            psl = publicsuffix2.PublicSuffixList()
            suffix = psl.get_public_suffix(domain)
            
            if not suffix:
                # Fallback: simple extraction
                parts = domain.split('.')
                if len(parts) >= 2:
                    return parts[-2]
                return parts[0]
            
            # If domain IS the suffix, return first part (though invalid)
            if domain == suffix:
                return domain.split('.')[0]
                
            # Extract the part before the suffix
            prefix = domain[:-len(suffix)].rstrip('.')
            
            # Get the last part of the prefix (the SLD)
            prefix_parts = prefix.split('.')
            return prefix_parts[-1]
        except Exception as e:
            logger.warning(f"Error extracting SLD for {domain}, using fallback: {e}")
            # Fallback
            parts = domain.split('.')
            if len(parts) >= 2:
                return parts[-2]
            return parts[0]
    
    def get_domains_list(self) -> List[Dict]:
        """
        Get list of all domains in the Namecheap account.
        
        Returns:
            List of domain dictionaries with domain name and status
        """
        try:
            logger.info("Fetching domains list from Namecheap API...")
            result = self._make_request('namecheap.domains.getList', {
                'PageSize': 100,  # Maximum per page
                'SortBy': 'NAME'
            })
            
            root = result['xml']
            raw_xml = result.get('raw', '')
            
            # Log raw XML for debugging (first 500 chars)
            logger.debug(f"Namecheap API response (first 500 chars): {raw_xml[:500]}")
            
            domains = []
            
            # Check API response status
            api_status = root.get('Status', '')
            if api_status and api_status.upper() != 'OK':
                error_msg = f"Namecheap API returned status: {api_status}. Raw response: {raw_xml}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # Parse domain list from XML
            # Namecheap returns domains with namespaces (ns0:Domain)
            # Format: <ns0:Domain Name="domain.com" Expires="..." Created="..." />
            import xml.etree.ElementTree as ET
            
            # Register namespace to handle ns0: prefix
            # Namecheap uses: http://api.namecheap.com/xml.response
            namespace = {'ns0': 'http://api.namecheap.com/xml.response'}
            
            domain_elements = []
            
            # Try different possible paths (with and without namespaces)
            possible_paths = [
                # With namespace (most common)
                './/ns0:Domain',
                './/{http://api.namecheap.com/xml.response}Domain',
                'ns0:CommandResponse/ns0:DomainGetListResult/ns0:Domain',
                '{http://api.namecheap.com/xml.response}CommandResponse/{http://api.namecheap.com/xml.response}DomainGetListResult/{http://api.namecheap.com/xml.response}Domain',
                # Without namespace (fallback)
                './/Domain',
                './/DomainGetListResult/Domain',
                'CommandResponse/DomainGetListResult/Domain'
            ]
            
            for path in possible_paths:
                try:
                    if path.startswith('ns0:') or '{http://api.namecheap.com/xml.response}' in path:
                        # Use namespace
                        domain_elements = root.findall(path, namespace)
                    else:
                        # No namespace
                        domain_elements = root.findall(path)
                    
                    if domain_elements:
                        logger.info(f"Found {len(domain_elements)} domains using path: {path}")
                        break
                except Exception as path_error:
                    logger.debug(f"Path {path} failed: {path_error}")
                    continue
            
            if not domain_elements:
                # Log the XML structure for debugging
                logger.warning(f"No domains found. XML structure: {ET.tostring(root, encoding='unicode')[:1000]}")
                # Check if there's an error message in the response
                error_elem = root.find('.//Error') or root.find('.//ns0:Error', namespace)
                if error_elem is not None:
                    error_text = error_elem.text or ''
                    raise Exception(f"Namecheap API error: {error_text}")
                else:
                    # Return empty list with warning
                    logger.warning("No domains found in response. This might be normal if account has no domains.")
                    return []
            
            for domain_elem in domain_elements:
                try:
                    # Get domain name from Name attribute
                    domain_name = domain_elem.get('Name') or domain_elem.attrib.get('Name', '')
                    if not domain_name:
                        # Try text content as fallback
                        domain_name = domain_elem.text or ''
                    
                    if not domain_name:
                        logger.warning(f"Skipping domain element without Name: {ET.tostring(domain_elem, encoding='unicode')[:200]}")
                        continue
                    
                    # Get other attributes (Namecheap uses: Expires, Created, IsLocked, AutoRenew)
                    # Note: Namecheap API uses "Expires" not "ExpiredDate", "Created" not "CreatedDate"
                    is_locked = (domain_elem.get('IsLocked', 'false') or domain_elem.attrib.get('IsLocked', 'false')).lower() == 'true'
                    auto_renew = (domain_elem.get('AutoRenew', 'false') or domain_elem.attrib.get('AutoRenew', 'false')).lower() == 'true'
                    expire_date = domain_elem.get('Expires', '') or domain_elem.attrib.get('Expires', '') or domain_elem.get('ExpiredDate', '') or domain_elem.attrib.get('ExpiredDate', '')
                    created_date = domain_elem.get('Created', '') or domain_elem.attrib.get('Created', '') or domain_elem.get('CreatedDate', '') or domain_elem.attrib.get('CreatedDate', '')
                    
                    domains.append({
                        'name': domain_name.strip(),
                        'is_locked': is_locked,
                        'auto_renew': auto_renew,
                        'expire_date': expire_date,
                        'created_date': created_date
                    })
                    
                    logger.debug(f"Parsed domain: {domain_name.strip()}")
                except Exception as parse_error:
                    logger.warning(f"Error parsing domain element: {parse_error}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    continue
            
            logger.info(f"Successfully retrieved {len(domains)} domains from Namecheap")
            if len(domains) == 0:
                logger.warning("No domains found. This might indicate:")
                logger.warning("  1. Account has no domains")
                logger.warning("  2. API credentials are incorrect")
                logger.warning("  3. Client IP is not whitelisted")
                logger.warning(f"  4. XML structure is different. Raw response: {raw_xml[:500]}")
            
            return domains
        
        except Exception as e:
            error_msg = f"Error getting domains list: {str(e)}"
            logger.error(error_msg, exc_info=True)
            # Include more context in the error
            if hasattr(e, '__cause__') and e.__cause__:
                error_msg += f" (Caused by: {str(e.__cause__)})"
            raise Exception(error_msg)

    def _extract_tld(self, domain: str) -> str:
        """
        Extract top-level domain (e.g., 'com' from 'example.com').
        For complex TLDs like 'co.uk', returns 'co.uk'.
        """
        try:
            # Manual parsing strategy to avoid publicsuffix2 issues
            # 1. Define known multi-part TLDs (same as in zone_utils)
            known_multipart_tlds = {
                'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'me.uk', 'ltd.uk', 'plc.uk', 'net.uk', 'sch.uk',
                'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au',
                'co.nz', 'net.nz', 'org.nz',
                'co.jp', 'ne.jp', 'or.jp', 'go.jp', 'ac.jp',
                'com.br', 'net.br', 'org.br', 'gov.br',
                'com.sg', 'edu.sg', 'gov.sg', 'net.sg', 'org.sg',
                'co.za', 'org.za', 'gov.za',
                'co.in', 'net.in', 'org.in', 'gen.in', 'ind.in',
                'com.cn', 'net.cn', 'org.cn', 'gov.cn'
            }
            
            parts = domain.split('.')
            if len(parts) < 2:
                return domain
                
            # Check if the last two parts form a known multi-part TLD
            last_two = '.'.join(parts[-2:])
            
            if last_two in known_multipart_tlds:
                logger.info(f"_extract_tld (manual): {domain} -> {last_two} (Multi-part TLD)")
                return last_two
                
            # Standard TLD (e.g. example.com, example.space)
            # TLD is just the last part
            tld = parts[-1]
            logger.info(f"_extract_tld (manual): {domain} -> {tld} (Standard TLD)")
            return tld
            
        except Exception as e:
            logger.warning(f"Error extracting TLD for {domain}, using fallback: {e}")
            # Fallback
            parts = domain.split('.')
            if len(parts) >= 1:
                return parts[-1]
            return domain
    
    def _extract_sld(self, domain: str) -> str:
        """
        Extract second-level domain (e.g., 'example' from 'example.com').
        For complex TLDs like 'co.uk', extracts correctly.
        """
        try:
            # Manual parsing strategy
            known_multipart_tlds = {
                'co.uk', 'org.uk', 'gov.uk', 'ac.uk', 'me.uk', 'ltd.uk', 'plc.uk', 'net.uk', 'sch.uk',
                'com.au', 'net.au', 'org.au', 'edu.au', 'gov.au',
                'co.nz', 'net.nz', 'org.nz',
                'co.jp', 'ne.jp', 'or.jp', 'go.jp', 'ac.jp',
                'com.br', 'net.br', 'org.br', 'gov.br',
                'com.sg', 'edu.sg', 'gov.sg', 'net.sg', 'org.sg',
                'co.za', 'org.za', 'gov.za',
                'co.in', 'net.in', 'org.in', 'gen.in', 'ind.in',
                'com.cn', 'net.cn', 'org.cn', 'gov.cn'
            }
            
            parts = domain.split('.')
            if len(parts) < 2:
                return parts[0]
                
            # Check if the last two parts form a known multi-part TLD
            last_two = '.'.join(parts[-2:])
            
            if last_two in known_multipart_tlds:
                # It's a multi-part TLD (e.g. example.co.uk)
                # SLD is the part before the TLD (parts[-3])
                if len(parts) >= 3:
                    sld = parts[-3]
                    logger.info(f"_extract_sld (manual): {domain} -> {sld} (Multi-part TLD)")
                    return sld
            
            # Standard TLD (e.g. example.com, example.space)
            # SLD is the part before the TLD (parts[-2])
            if len(parts) >= 2:
                sld = parts[-2]
                logger.info(f"_extract_sld (manual): {domain} -> {sld} (Standard TLD)")
                return sld
            
            return parts[0]
            
        except Exception as e:
            logger.warning(f"Error extracting SLD for {domain}, using fallback: {e}")
            # Fallback
            parts = domain.split('.')
            if len(parts) >= 2:
                return parts[-2]
            return parts[0]
    
    def get_domains_list(self) -> List[Dict]:
        """
        Get list of all domains in the Namecheap account.
        
        Returns:
            List of domain dictionaries with domain name and status
        """
        try:
            logger.info("Fetching domains list from Namecheap API...")
            result = self._make_request('namecheap.domains.getList', {
                'PageSize': 100,  # Maximum per page
                'SortBy': 'NAME'
            })
            
            root = result['xml']
            raw_xml = result.get('raw', '')
            
            # Log raw XML for debugging (first 500 chars)
            logger.debug(f"Namecheap API response (first 500 chars): {raw_xml[:500]}")
            
            domains = []
            
            # Check API response status
            api_status = root.get('Status', '')
            if api_status and api_status.upper() != 'OK':
                error_msg = f"Namecheap API returned status: {api_status}. Raw response: {raw_xml}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # Parse domain list from XML
            # Namecheap returns domains with namespaces (ns0:Domain)
            # Format: <ns0:Domain Name="domain.com" Expires="..." Created="..." />
            import xml.etree.ElementTree as ET
            
            # Register namespace to handle ns0: prefix
            # Namecheap uses: http://api.namecheap.com/xml.response
            namespace = {'ns0': 'http://api.namecheap.com/xml.response'}
            
            domain_elements = []
            
            # Try different possible paths (with and without namespaces)
            possible_paths = [
                # With namespace (most common)
                './/ns0:Domain',
                './/{http://api.namecheap.com/xml.response}Domain',
                'ns0:CommandResponse/ns0:DomainGetListResult/ns0:Domain',
                '{http://api.namecheap.com/xml.response}CommandResponse/{http://api.namecheap.com/xml.response}DomainGetListResult/{http://api.namecheap.com/xml.response}Domain',
                # Without namespace (fallback)
                './/Domain',
                './/DomainGetListResult/Domain',
                'CommandResponse/DomainGetListResult/Domain'
            ]
            
            for path in possible_paths:
                try:
                    if path.startswith('ns0:') or '{http://api.namecheap.com/xml.response}' in path:
                        # Use namespace
                        domain_elements = root.findall(path, namespace)
                    else:
                        # No namespace
                        domain_elements = root.findall(path)
                    
                    if domain_elements:
                        logger.info(f"Found {len(domain_elements)} domains using path: {path}")
                        break
                except Exception as path_error:
                    logger.debug(f"Path {path} failed: {path_error}")
                    continue
            
            if not domain_elements:
                # Log the XML structure for debugging
                logger.warning(f"No domains found. XML structure: {ET.tostring(root, encoding='unicode')[:1000]}")
                # Check if there's an error message in the response
                error_elem = root.find('.//Error') or root.find('.//ns0:Error', namespace)
                if error_elem is not None:
                    error_text = error_elem.text or ''
                    raise Exception(f"Namecheap API error: {error_text}")
                else:
                    # Return empty list with warning
                    logger.warning("No domains found in response. This might be normal if account has no domains.")
                    return []
            
            for domain_elem in domain_elements:
                try:
                    # Get domain name from Name attribute
                    domain_name = domain_elem.get('Name') or domain_elem.attrib.get('Name', '')
                    if not domain_name:
                        # Try text content as fallback
                        domain_name = domain_elem.text or ''
                    
                    if not domain_name:
                        logger.warning(f"Skipping domain element without Name: {ET.tostring(domain_elem, encoding='unicode')[:200]}")
                        continue
                    
                    # Get other attributes (Namecheap uses: Expires, Created, IsLocked, AutoRenew)
                    # Note: Namecheap API uses "Expires" not "ExpiredDate", "Created" not "CreatedDate"
                    is_locked = (domain_elem.get('IsLocked', 'false') or domain_elem.attrib.get('IsLocked', 'false')).lower() == 'true'
                    auto_renew = (domain_elem.get('AutoRenew', 'false') or domain_elem.attrib.get('AutoRenew', 'false')).lower() == 'true'
                    expire_date = domain_elem.get('Expires', '') or domain_elem.attrib.get('Expires', '') or domain_elem.get('ExpiredDate', '') or domain_elem.attrib.get('ExpiredDate', '')
                    created_date = domain_elem.get('Created', '') or domain_elem.attrib.get('Created', '') or domain_elem.get('CreatedDate', '') or domain_elem.attrib.get('CreatedDate', '')
                    
                    domains.append({
                        'name': domain_name.strip(),
                        'is_locked': is_locked,
                        'auto_renew': auto_renew,
                        'expire_date': expire_date,
                        'created_date': created_date
                    })
                    
                    logger.debug(f"Parsed domain: {domain_name.strip()}")
                except Exception as parse_error:
                    logger.warning(f"Error parsing domain element: {parse_error}")
                    import traceback
                    logger.debug(traceback.format_exc())
                    continue
            
            logger.info(f"Successfully retrieved {len(domains)} domains from Namecheap")
            if len(domains) == 0:
                logger.warning("No domains found. This might indicate:")
                logger.warning("  1. Account has no domains")
                logger.warning("  2. API credentials are incorrect")
                logger.warning("  3. Client IP is not whitelisted")
                logger.warning(f"  4. XML structure is different. Raw response: {raw_xml[:500]}")
            
            return domains
        
        except Exception as e:
            error_msg = f"Error getting domains list: {str(e)}"
            logger.error(error_msg, exc_info=True)
            # Include more context in the error
            if hasattr(e, '__cause__') and e.__cause__:
                error_msg += f" (Caused by: {str(e.__cause__)})"
            raise Exception(error_msg)
