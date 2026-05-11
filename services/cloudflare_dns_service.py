import logging
import requests
from typing import Dict, Optional, List
from database import CloudflareConfig

logger = logging.getLogger(__name__)

class CloudflareDNSService:
    """Service for managing DNS records via Cloudflare API."""
    
    BASE_URL = "https://api.cloudflare.com/client/v4"
    
    def __init__(self):
        """Initialize service with configuration from database."""
        try:
            config = CloudflareConfig.query.first()
            if not config or not config.is_configured:
                raise Exception("Cloudflare configuration not found or not configured")
            
            self._config = config
            
            # Determine Auth Headers based on API Token format
            # If it looks like a Global API Key (37 chars hex) and we have email, use X-Auth-Key
            # Otherwise assume it's an API Token (Bearer)
            
            # Cloudflare Global API Key is typically 37 chars hex
            # API Tokens are usually 40 chars alphanumeric
            
            api_token = self._config.api_token.strip()
            email = self._config.email.strip() if self._config.email else None
            
            # Heuristic: If email is provided and token is 37 chars, try Global API Key
            # Or if user explicitly wants to use Global Key (we don't have a flag for that yet)
            
            # For now, we'll use a safer approach:
            # If we have email, we can try to support Global API Key if Bearer fails? 
            # No, that's too complex for init.
            
            # Let's assume:
            # If length is 37 and all hex -> Global API Key
            # Else -> API Token
            
            is_global_key = False
            if len(api_token) == 37 and all(c in '0123456789abcdefABCDEF' for c in api_token):
                is_global_key = True
                
            if is_global_key and email:
                logger.info("Detected Cloudflare Global API Key format")
                self._headers = {
                    "X-Auth-Email": email,
                    "X-Auth-Key": api_token,
                    "Content-Type": "application/json"
                }
            else:
                logger.info("Using Cloudflare API Token (Bearer)")
                self._headers = {
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json"
                }
            # Some endpoints might require X-Auth-Email and X-Auth-Key (Global API Key)
            # But for API Tokens, Bearer auth is standard. 
            # If using Global API Key, we'd need X-Auth-Email and X-Auth-Key.
            # Assuming API Token for now as it's more secure.
            
            logger.info("Cloudflare configuration loaded")
        
        except Exception as e:
            logger.error(f"Error loading Cloudflare config: {e}")
            raise

    def get_zone_id(self, domain: str) -> Optional[str]:
        """
        Get Zone ID for a given domain.
        
        Args:
            domain: The domain name (e.g., example.com)
            
        Returns:
            Zone ID string or None if not found
        """
        try:
            # Search for the zone
            url = f"{self.BASE_URL}/zones"
            params = {'name': domain, 'status': 'active'}
            
            response = requests.get(url, headers=self._headers, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get('success'):
                logger.error(f"Cloudflare API error getting zone for {domain}: {data.get('errors')}")
                return None
            
            result = data.get('result', [])
            if not result:
                logger.warning(f"No active zone found for {domain} in Cloudflare")
                return None
            
            return result[0]['id']
            
        except Exception as e:
            logger.error(f"Error getting zone ID for {domain}: {e}")
            raise

    def get_dns_records(self, zone_id: str, type: str = None, name: str = None) -> List[Dict]:
        """
        Get DNS records for a zone.
        
        Args:
            zone_id: The Zone ID
            type: Record type (e.g., TXT)
            name: Record name (e.g., sub.example.com)
            
        Returns:
            List of record dictionaries
        """
        try:
            url = f"{self.BASE_URL}/zones/{zone_id}/dns_records"
            params = {'per_page': 100, 'page': 1}
            if type:
                params['type'] = type
            if name:
                params['name'] = name
            
            all_records = []
            
            while True:
                response = requests.get(url, headers=self._headers, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                if not data.get('success'):
                    logger.error(f"Cloudflare API error getting records: {data.get('errors')}")
                    # If partial failure, maybe break? Or just return what we have? 
                    # Returning empty list or raising might be better depending on severity.
                    # For now, let's stop and log
                    break
                    
                records = data.get('result', [])
                if not records:
                    break
                    
                all_records.extend(records)
                
                # Check pagination
                result_info = data.get('result_info', {})
                total_pages = result_info.get('total_pages', 1)
                current_page = result_info.get('page', params['page'])
                
                if current_page >= total_pages:
                    break
                    
                params['page'] += 1
                
            return all_records
            
        except Exception as e:
            logger.error(f"Error getting DNS records: {e}")
            raise

    def upsert_txt_record(self, apex: str, host: str, value: str, ttl: int = 1) -> Dict:
        """
        Create or update TXT record in Cloudflare.
        
        Args:
            apex: The apex domain (zone name)
            host: The host part (subdomain or @). 
                  Note: Cloudflare expects the full record name (e.g., sub.example.com)
                  or just example.com for root.
            value: The TXT record value
            ttl: TTL in seconds (1 for automatic)
            
        Returns:
            Dict with 'success', 'message', 'record'
        """
        try:
            # 1. Get Zone ID
            zone_id = self.get_zone_id(apex)
            if not zone_id:
                raise Exception(f"Could not find active zone for {apex} in Cloudflare")
            
            # 2. Construct full record name
            # If host is '@', record name is apex.
            # If host is 'sub', record name is sub.apex
            if host == '@':
                record_name = apex
            else:
                record_name = f"{host}.{apex}"
            
            # 3. Check for existing record
            existing_records = self.get_dns_records(zone_id, type='TXT', name=record_name)
            
            # Prepare both quoted and unquoted versions for comparison
            # Cloudflare stores TXT records with quotes, so we need to check both formats
            quoted_value = f'"{value}"' if not (value.startswith('"') and value.endswith('"')) else value
            unquoted_value = value.strip('"')
            
            # Check if exact match exists (with or without quotes)
            for record in existing_records:
                record_content = record['content']
                if record_content == value or record_content == quoted_value or record_content.strip('"') == unquoted_value:
                    logger.info(f"TXT record already exists for {record_name} with value {value}")
                    return {'success': True, 'message': 'Record already exists', 'record': record}
            
            # 4. If we are adding a Google verification token, remove any existing ones for this name
            # (Similar logic to Namecheap fix)
            # Check for google-site-verification in both quoted and unquoted formats
            is_google_verification = 'google-site-verification=' in value
            if is_google_verification:
                for record in existing_records:
                    record_content = record['content'].strip('"')
                    if 'google-site-verification=' in record_content:
                        logger.info(f"Deleting existing Google verification token: {record['id']}")
                        self.delete_record(zone_id, record['id'])

            # 5. Create new record
            url = f"{self.BASE_URL}/zones/{zone_id}/dns_records"
            
            # Cloudflare TXT records need the value wrapped in double quotes
            # If value doesn't already have quotes, add them
            if not (value.startswith('"') and value.endswith('"')):
                quoted_value = f'"{value}"'
            else:
                quoted_value = value
            
            payload = {
                'type': 'TXT',
                'name': record_name,
                'content': quoted_value,
                'ttl': ttl  # 1 = Automatic in Cloudflare
            }
            
            response = requests.post(url, headers=self._headers, json=payload, timeout=30)
            
            if not response.ok:
                error_details = response.text
                try:
                    error_json = response.json()
                    error_details = error_json.get('errors', error_details)
                except:
                    pass
                
                error_msg = f"Cloudflare API error creating record: {error_details}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            data = response.json()
            if not data.get('success'):
                error_msg = f"Cloudflare API error creating record: {data.get('errors')}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            logger.info(f"Successfully created TXT record for {record_name}")
            return {'success': True, 'message': 'Record created successfully', 'record': data.get('result')}

        except Exception as e:
            logger.error(f"Error upserting TXT record for {apex}: {e}")
            raise

    def delete_record(self, zone_id: str, record_id: str):
        """Delete a DNS record."""
        try:
            url = f"{self.BASE_URL}/zones/{zone_id}/dns_records/{record_id}"
            response = requests.delete(url, headers=self._headers, timeout=30)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Error deleting record {record_id}: {e}")
            # Don't raise, just log

    def get_zones(self) -> List[Dict]:
        """
        Get list of all zones (domains) in Cloudflare account.
        
        Returns:
            List of dictionaries containing zone info (id, name, status, etc.)
        """
        try:
            url = f"{self.BASE_URL}/zones"
            url = f"{self.BASE_URL}/zones"
            params = {'per_page': 50} # Removed status='active' to see all zones
            
            all_zones = []
            page = 1
            
            while True:
                params['page'] = page
                response = requests.get(url, headers=self._headers, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                
                if not data.get('success'):
                    error_msg = f"Cloudflare API error getting zones: {data.get('errors')}"
                    logger.error(error_msg)
                    raise Exception(error_msg)
                
                zones = data.get('result', [])
                if not zones:
                    break
                    
                all_zones.extend(zones)
                
                # Check pagination
                result_info = data.get('result_info', {})
                total_pages = result_info.get('total_pages', 1)
                
                if page >= total_pages:
                    break
                    
                page += 1
                
            return all_zones
            
        except Exception as e:
            logger.error(f"Error getting Cloudflare zones: {e}")
            raise

    def delete_all_txt_records(self, domain: str) -> Dict:
        """
        Delete ALL TXT records for a domain in Cloudflare.
        
        Args:
            domain: The domain name
            
        Returns:
            Dict details about deleted records
        """
        try:
            # 1. Get Zone ID
            zone_id = self.get_zone_id(domain)
            if not zone_id:
                return {'success': False, 'error': f"Zone not found for {domain}"}
            
            # 2. Get all TXT records
            records = self.get_dns_records(zone_id, type='TXT')
            if not records:
                return {'success': True, 'deleted': 0, 'message': 'No TXT records found'}
            
            # 3. Delete each record
            deleted_count = 0
            errors = []
            
            for record in records:
                try:
                    self.delete_record(zone_id, record['id'])
                    deleted_count += 1
                except Exception as e:
                    errors.append(f"Failed to delete {record['name']}: {str(e)}")
            
            if errors:
                return {
                    'success': False, 
                    'deleted': deleted_count, 
                    'total': len(records),
                    'errors': errors
                }
                
            return {
                'success': True, 
                'deleted': deleted_count, 
                'total': len(records),
                'message': f"Successfully deleted {deleted_count} TXT records"
            }
            
        except Exception as e:
            logger.error(f"Error deleting all TXT records for {domain}: {e}")
            return {'success': False, 'error': str(e)}

    def yield_delete_all_txt_records(self, domain: str):
        """
        Yield deletion progress for all TXT records of a domain.
        
        Args:
            domain: The domain name
            
        Yields:
            Dict details about progress/result
        """
        try:
            # 1. Get Zone ID
            yield {'type': 'info', 'message': f"Fetching zone ID for {domain}..."}
            zone_id = self.get_zone_id(domain)
            if not zone_id:
                yield {'type': 'error', 'message': f"Zone not found for {domain}"}
                return
            
            # 2. Get all TXT records
            yield {'type': 'info', 'message': "Fetching existing TXT records..."}
            records = self.get_dns_records(zone_id, type='TXT')
            if not records:
                yield {'type': 'success', 'message': 'No TXT records found to delete.'}
                return
            
            yield {'type': 'info', 'message': f"Found {len(records)} TXT records. Starting deletion..."}
            
            # 3. Delete each record
            deleted_count = 0
            errors = []
            
            for i, record in enumerate(records, 1):
                try:
                    self.delete_record(zone_id, record['id'])
                    deleted_count += 1
                    yield {'type': 'progress', 'message': f"Deleted {i}/{len(records)}: {record['name']}", 'current': i, 'total': len(records)}
                except Exception as e:
                    error_msg = f"Failed to delete {record['name']}: {str(e)}"
                    errors.append(error_msg)
                    yield {'type': 'warning', 'message': error_msg}
            
            if errors:
                yield {
                    'type': 'complete',
                    'success': False, 
                    'deleted': deleted_count, 
                    'total': len(records),
                    'errors': errors,
                    'message': f"Finished with {len(errors)} errors. Deleted {deleted_count}/{len(records)}."
                }
            else:
                yield {
                    'type': 'complete',
                    'success': True, 
                    'deleted': deleted_count, 
                    'total': len(records),
                    'message': f"Successfully deleted all {deleted_count} TXT records."
                }
            
        except Exception as e:
            logger.error(f"Error yielding delete all TXT records for {domain}: {e}")
            yield {'type': 'error', 'message': str(e)}

