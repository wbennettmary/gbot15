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
        self._site_verification_services = {}
        self._last_token_without_delegation = None
        
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
    
    def _get_site_verification_service(self, without_delegation: bool = False):
        """Get Site Verification service."""
        cache_key = 'without_delegation' if without_delegation else 'with_delegation'
        if cache_key not in self._site_verification_services:
            if without_delegation:
                creds = service_account.Credentials.from_service_account_info(
                    self.credentials_info,
                    scopes=["https://www.googleapis.com/auth/siteverification"]
                )
            else:
                creds = self._get_delegated_credentials()
            self._site_verification_services[cache_key] = build('siteVerification', 'v1', credentials=creds)
        return self._site_verification_services[cache_key]
    
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
            request_body = {
                'verificationMethod': 'DNS_TXT',
                'site': {
                    'type': 'INET_DOMAIN',
                    'identifier': domain
                }
            }

            last_error = None
            # Prefer direct service-account Site Verification for token creation.
            # Delegated Site Verification can return repeat 503s for insert even
            # when Admin SDK DWD is configured correctly.
            for without_delegation in (True, False):
                service = self._get_site_verification_service(without_delegation=without_delegation)

                for attempt in range(3):
                    try:
                        response = service.webResource().getToken(body=request_body).execute()
                        token = response.get('token', '')

                        if token:
                            # Ensure proper format
                            if not token.startswith('google-site-verification='):
                                txt_value = f'google-site-verification={token}'
                            else:
                                txt_value = token
                                token = token.replace('google-site-verification=', '')

                            self._last_token_without_delegation = without_delegation
                            logger.info(f"[GET_TOKEN] Got token for {domain}: {token[:20]}... (without_delegation={without_delegation})")
                            return txt_value, "Token retrieved"

                        logger.error(f"[GET_TOKEN] Empty token for {domain}")
                        return None, "Empty token received"

                    except HttpError as e:
                        last_error = e
                        if e.resp.status == 503 and attempt < 2:
                            wait_time = 2 + (attempt * 2)
                            logger.warning(f"[GET_TOKEN] 503 for {domain} (without_delegation={without_delegation}), waiting {wait_time}s")
                            time.sleep(wait_time)
                            continue
                        if e.resp.status == 503:
                            logger.warning(f"[GET_TOKEN] 503 exhausted for {domain} (without_delegation={without_delegation}), trying fallback identity")
                            break
                        raise

            if last_error:
                logger.error(f"[GET_TOKEN] HTTP error for {domain}: {last_error}")
                return None, f"API error: {str(last_error)}"

            return None, "Token not received"
                
        except HttpError as e:
            logger.error(f"[GET_TOKEN] HTTP error for {domain}: {e}")
            return None, f"API error: {str(e)}"
        except Exception as e:
            logger.error(f"[GET_TOKEN] Exception for {domain}: {e}", exc_info=True)
            return None, f"Error: {str(e)}"
    
    def verify_domain(self, domain: str) -> Tuple[bool, str]:
        """
        Verify domain ownership and confirm it in Google Workspace.
        
        Args:
            domain: Domain to verify
            
        Returns:
            (verified: bool, message: str)
        """
        logger.info(f"[VERIFY] Starting for {domain}")
        
        try:
            # CRITICAL FIX: The body should only contain the 'site' object
            # The verificationMethod is passed as a parameter, NOT in the body
            request_body = {
                'site': {
                    'type': 'INET_DOMAIN',
                    'identifier': domain
                }
            }
            if self.admin_email:
                request_body['owners'] = [self.admin_email]
            
            # Try to verify with retries for DNS propagation
            if self._last_token_without_delegation is None:
                delegation_modes = (False, True)
            else:
                delegation_modes = (self._last_token_without_delegation, not self._last_token_without_delegation)

            last_503_error = None
            for without_delegation in delegation_modes:
                service = self._get_site_verification_service(without_delegation=without_delegation)
                max_attempts = 8  # Increased to give DNS more time to propagate

                for attempt in range(max_attempts):
                    try:
                        # CRITICAL FIX: Only pass 'site' in body, method is a parameter
                        result = service.webResource().insert(
                            verificationMethod='DNS_TXT',
                            body=request_body
                        ).execute()

                        logger.info(f"[VERIFY] ✅ Success for {domain}: {result} (without_delegation={without_delegation})")

                        # Additional check: Verify the response contains confirmation
                        if result.get('id') or result.get('site', {}).get('identifier') == domain:
                            return self._confirm_workspace_verification(domain)
                        else:
                            logger.warning(f"[VERIFY] Unexpected response: {result}")
                            return self._confirm_workspace_verification(domain)

                    except HttpError as e:
                        error_str = str(e)
                        status = e.resp.status

                        # Handle specific error cases
                        if status == 400:
                            # Always retry on 400 because it usually means DNS is not propagated yet
                            # Google's error messages vary and might not always contain "token"
                            if attempt < max_attempts - 1:
                                wait_time = 15 * (attempt + 1)
                                logger.info(f"[VERIFY] Verification failed (400). DNS probably not ready. Waiting {wait_time}s (attempt {attempt+1}/{max_attempts}, without_delegation={without_delegation}). error: {error_str[:150]}")
                                time.sleep(wait_time)
                                continue
                            else:
                                return False, f"Verification failed after {max_attempts} retries (DNS likely not propagated yet). Error from Google: {error_str}"

                        elif status == 409:
                            # Already verified - this is success!
                            logger.info(f"[VERIFY] ✅ {domain} already verified (409)")
                            self._ensure_workspace_admin_owner(domain, without_delegation=without_delegation)
                            return self._confirm_workspace_verification(domain)

                        elif status == 403:
                            # Permission denied
                            logger.error(f"[VERIFY] 403 Forbidden for {domain}")
                            return False, "Permission denied. Check service account permissions and Domain-Wide Delegation setup."

                        elif status == 503:
                            # Service unavailable - retry, then try the alternate Site Verification identity
                            last_503_error = error_str
                            if attempt < max_attempts - 1:
                                wait_time = 5 * (attempt + 1)
                                logger.warning(f"[VERIFY] 503 Service unavailable, waiting {wait_time}s (without_delegation={without_delegation})")
                                time.sleep(wait_time)
                                continue

                            logger.warning(f"[VERIFY] 503 retries exhausted for {domain} (without_delegation={without_delegation}), trying fallback identity")
                            break

                        else:
                            # Other HTTP errors
                            logger.error(f"[VERIFY] HTTP {status} error for {domain}: {e}")
                            return False, f"Verification failed: HTTP {status} - {error_str}"

            if last_503_error:
                return False, f"Google Site Verification API returned 503 for both delegated and service-account identities. Last error: {last_503_error}"
            
            # If we get here, all retries exhausted
            return False, "Verification failed after maximum retries"
            
        except Exception as e:
            logger.error(f"[VERIFY] Exception for {domain}: {e}", exc_info=True)
            return False, f"Error: {str(e)}"

    def _ensure_workspace_admin_owner(self, domain: str, without_delegation: bool = False) -> bool:
        """
        Add the Workspace admin as a direct Site Verification owner when the
        resource already exists. This helps Workspace consume the verified state.
        """
        if not self.admin_email:
            return False

        try:
            service = self._get_site_verification_service(without_delegation=without_delegation)
            resources = service.webResource().list().execute().get('items', [])

            for resource in resources:
                site = resource.get('site', {})
                identifier = (site.get('identifier') or '').lower()
                if site.get('type') == 'INET_DOMAIN' and identifier == domain.lower():
                    current_owners = resource.get('owners') or []
                    owners = list(dict.fromkeys(current_owners + [self.admin_email]))
                    if owners == current_owners:
                        logger.info(f"[VERIFY] Workspace admin owner already present for {domain}")
                        return True

                    resource['owners'] = owners
                    service.webResource().update(
                        id=resource.get('id'),
                        body=resource
                    ).execute()
                    logger.info(f"[VERIFY] Added Workspace admin owner for {domain}: {self.admin_email}")
                    return True

            logger.warning(f"[VERIFY] Existing Site Verification resource not found for {domain}")
            return False

        except Exception as e:
            logger.warning(f"[VERIFY] Could not update Site Verification owners for {domain}: {e}")
            return False

    def _confirm_workspace_verification(self, domain: str) -> Tuple[bool, str]:
        """
        Confirm the Admin SDK domain record is marked verified after Site Verification.
        """
        try:
            admin_service = self._get_admin_service()
            max_checks = 12

            for check in range(1, max_checks + 1):
                try:
                    domain_info = admin_service.domains().get(
                        customer='my_customer',
                        domainName=domain
                    ).execute()

                    if domain_info.get('verified', False):
                        logger.info(f"[VERIFY] ✅ Workspace account shows {domain} as verified")
                        return True, "Domain verified successfully in Workspace"

                    logger.info(f"[VERIFY] Workspace still shows {domain} as unverified (check {check}/{max_checks})")
                    if check < max_checks:
                        time.sleep(10)

                except HttpError as e:
                    status = e.resp.status
                    if status == 404:
                        logger.warning(f"[VERIFY] {domain} is not found in Workspace during verification confirmation")
                        return False, "Site Verification succeeded, but the domain is not found in Workspace"

                    logger.warning(f"[VERIFY] Workspace verification check HTTP {status}: {e}")
                    if check < max_checks:
                        time.sleep(5)
                        continue
                    return False, f"Site Verification succeeded, but Workspace status check failed: {str(e)}"

            return False, "Site Verification succeeded, but Workspace has not marked the domain verified yet"

        except Exception as e:
            logger.error(f"[VERIFY] Workspace confirmation error for {domain}: {e}", exc_info=True)
            return False, f"Site Verification succeeded, but Workspace confirmation failed: {str(e)}"
    
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
