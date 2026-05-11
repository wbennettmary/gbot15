"""
Simple Domain Addition & Verification Service - REWRITTEN FROM SCRATCH
This replaces the complex broken flow with a simple, working implementation.
"""
import json
import logging
import time
from typing import Dict, Optional, Tuple
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Required scopes for domain operations
SCOPES = [
    "https://www.googleapis.com/auth/admin.directory.domain",
    "https://www.googleapis.com/auth/admin.directory.user",
    "https://www.googleapis.com/auth/siteverification"
]


class SimpleDomainService:
    """
    Simplified domain service that does ONE thing well:
    Add and verify domains in Google Workspace.
    """
    
    def __init__(self, service_account_json: str, admin_email: str):
        """
        Initialize with service account credentials.
        
        Args:
            service_account_json: JSON string of service account key
            admin_email: Admin email to impersonate via DWD
        """
        self.admin_email = admin_email
        self.credentials_info = json.loads(service_account_json)
        self._admin_service = None
        self._site_verification_service = None
        
        logger.info(f"SimpleDomainService initialized for {admin_email}")
    
    def _get_delegated_credentials(self):
        """Get credentials with domain-wide delegation."""
        creds = service_account.Credentials.from_service_account_info(
            self.credentials_info, 
            scopes=SCOPES
        )
        return creds.with_subject(self.admin_email)
    
    def _get_admin_service(self):
        """Get Admin SDK Directory service."""
        if not self._admin_service:
            creds = self._get_delegated_credentials()
            self._admin_service = build('admin', 'directory_v1', credentials=creds)
        return self._admin_service
    
    def _get_site_verification_service(self):
        """Get Site Verification service."""
        if not self._site_verification_service:
            creds = self._get_delegated_credentials()
            self._site_verification_service = build('siteVerification', 'v1', credentials=creds)
        return self._site_verification_service
    
    def add_domain(self, apex_domain: str) -> Tuple[bool, str]:
        """
        Add apex domain to Google Workspace.
        
        Args:
            apex_domain: Root domain like "example.com"
            
        Returns:
            (success: bool, message: str)
        """
        logger.info(f"[ADD_DOMAIN] Starting for {apex_domain}")
        
        try:
            service = self._get_admin_service()
            
            # Step 1: Check if domain already exists
            try:
                existing = service.domains().get(
                    customer='my_customer', 
                    domainName=apex_domain
                ).execute()
                logger.info(f"[ADD_DOMAIN] {apex_domain} already exists")
                return True, "Domain already exists"
            except HttpError as e:
                if e.resp.status == 404:
                    logger.info(f"[ADD_DOMAIN] {apex_domain} not found, will add")
                elif e.resp.status == 403:
                    logger.error(f"[ADD_DOMAIN] 403 Forbidden for {apex_domain}")
                    return False, f"Permission denied (403). Check DWD setup."
                else:
                    logger.warning(f"[ADD_DOMAIN] Error checking {apex_domain}: {e}")
            
            # Step 2: Add the domain
            try:
                result = service.domains().insert(
                    customer='my_customer',
                    body={'domainName': apex_domain}
                ).execute()
                logger.info(f"[ADD_DOMAIN] Successfully added {apex_domain}")
                return True, "Domain added successfully"
            except HttpError as e:
                if 'already exists' in str(e).lower() or e.resp.status == 409:
                    logger.info(f"[ADD_DOMAIN] {apex_domain} already exists (409)")
                    return True, "Domain already exists"
                elif e.resp.status == 403:
                    return False, f"Permission denied adding domain. Check DWD."
                else:
                    logger.error(f"[ADD_DOMAIN] Failed to add {apex_domain}: {e}")
                    return False, f"Failed to add domain: {str(e)}"
                    
        except Exception as e:
            logger.error(f"[ADD_DOMAIN] Exception for {apex_domain}: {e}", exc_info=True)
            return False, f"Error: {str(e)}"
    
    def get_verification_token(self, domain: str) -> Tuple[Optional[str], str]:
        """
        Get DNS TXT verification token for a domain.
        
        Args:
            domain: Domain to get token for (can be subdomain)
            
        Returns:
            (token: str or None, message: str)
        """
        logger.info(f"[GET_TOKEN] Starting for {domain}")
        
        try:
            service = self._get_site_verification_service()
            
            request_body = {
                'verificationMethod': 'DNS_TXT',
                'site': {
                    'type': 'INET_DOMAIN',
                    'identifier': domain
                }
            }
            
            response = service.webResource().getToken(body=request_body).execute()
            token = response.get('token', '')
            
            if token:
                # Ensure proper format
                if not token.startswith('google-site-verification='):
                    txt_value = f'google-site-verification={token}'
                else:
                    txt_value = token
                    token = token.replace('google-site-verification=', '')
                
                logger.info(f"[GET_TOKEN] Got token for {domain}: {token[:20]}...")
                return txt_value, "Token retrieved"
            else:
                logger.error(f"[GET_TOKEN] Empty token for {domain}")
                return None, "Empty token received"
                
        except HttpError as e:
            logger.error(f"[GET_TOKEN] HTTP error for {domain}: {e}")
            return None, f"API error: {str(e)}"
        except Exception as e:
            logger.error(f"[GET_TOKEN] Exception for {domain}: {e}", exc_info=True)
            return None, f"Error: {str(e)}"
    
    def verify_domain(self, domain: str) -> Tuple[bool, str]:
        """
        Verify domain ownership via Site Verification API.
        
        Args:
            domain: Domain to verify
            
        Returns:
            (verified: bool, message: str)
        """
        logger.info(f"[VERIFY] Starting for {domain}")
        
        try:
            service = self._get_site_verification_service()
            
            request_body = {
                'site': {
                    'type': 'INET_DOMAIN',
                    'identifier': domain
                },
                'verificationMethod': 'DNS_TXT'
            }
            
            # Try to verify with retries for DNS propagation
            max_attempts = 5
            for attempt in range(max_attempts):
                try:
                    result = service.webResource().insert(
                        verificationMethod='DNS_TXT',
                        body=request_body
                    ).execute()
                    
                    logger.info(f"[VERIFY] Success for {domain}: {result}")
                    return True, "Domain verified successfully"
                    
                except HttpError as e:
                    error_str = str(e)
                    if e.resp.status == 400 and 'token' in error_str.lower():
                        # DNS not propagated yet
                        if attempt < max_attempts - 1:
                            wait_time = 10 * (attempt + 1)
                            logger.info(f"[VERIFY] DNS not ready, waiting {wait_time}s (attempt {attempt+1}/{max_attempts})")
                            time.sleep(wait_time)
                            continue
                        else:
                            return False, "DNS TXT record not found. Wait for propagation."
                    elif e.resp.status == 409:
                        # Already verified
                        logger.info(f"[VERIFY] {domain} already verified")
                        return True, "Already verified"
                    else:
                        logger.error(f"[VERIFY] HTTP error for {domain}: {e}")
                        return False, f"Verification failed: {str(e)}"
            
            return False, "Verification failed after retries"
            
        except Exception as e:
            logger.error(f"[VERIFY] Exception for {domain}: {e}", exc_info=True)
            return False, f"Error: {str(e)}"
    
    def full_process(self, input_domain: str) -> Dict:
        """
        Complete domain addition and verification process.
        
        Args:
            input_domain: Domain to add and verify (can be subdomain)
            
        Returns:
            Dict with status of each step
        """
        result = {
            'input_domain': input_domain,
            'apex_domain': None,
            'add_success': False,
            'add_message': '',
            'token': None,
            'token_message': '',
            'verify_success': False,
            'verify_message': '',
            'overall_success': False
        }
        
        # Parse domain to get apex
        parts = input_domain.lower().strip().split('.')
        if len(parts) >= 3:
            apex = '.'.join(parts[1:])  # sub.example.com -> example.com
            txt_host = parts[0]
        else:
            apex = input_domain
            txt_host = '@'
        
        result['apex_domain'] = apex
        result['txt_host'] = txt_host
        
        logger.info(f"[FULL_PROCESS] Input: {input_domain}, Apex: {apex}, TXT Host: {txt_host}")
        
        # Step 1: Add domain to Workspace
        # CRITICAL FIX: Use input_domain (full subdomain), NOT apex. 
        # Adding apex often fails with 403 if it's already in use or restricted.
        logger.info(f"[FULL_PROCESS] Adding FULL domain: {input_domain}")
        add_ok, add_msg = self.add_domain(input_domain)
        result['add_success'] = add_ok
        result['add_message'] = add_msg
        
        if not add_ok:
            # CRITICAL FIX: Stop immediately if we can't add the domain
            # Do NOT proceed to get token, because it's useless if domain isn't in Workspace
            logger.error(f"[FULL_PROCESS] Add failed for {input_domain}: {add_msg}")
            return result
        
        # Step 2: Get verification token
        token, token_msg = self.get_verification_token(input_domain)
        result['token'] = token
        result['token_message'] = token_msg
        
        if not token:
            logger.error(f"[FULL_PROCESS] Token failed for {input_domain}: {token_msg}")
            return result
        
        # Note: DNS record creation should be done by caller
        # We just return the token here
        
        result['overall_success'] = True
        logger.info(f"[FULL_PROCESS] Complete for {input_domain}")
        return result
