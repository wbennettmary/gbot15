"""
Google Workspace domain management service.
Handles domain addition, verification token retrieval, and domain verification.
"""
import logging
import time
import random
from typing import Dict, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.auth.transport.requests
from database import GoogleAccount, GoogleToken, ServiceAccount

logger = logging.getLogger(__name__)

class GoogleDomainsService:
    """Service for managing Google Workspace domains."""
    
    def __init__(self, account_name: str):
        """
        Initialize service with Google account credentials.
        
        Args:
            account_name: Name of the Google account to use
        """
        self.account_name = account_name
        self._admin_service = None
        self._site_verification_service = None
    
    def _get_credentials(self) -> Optional[Credentials]:
        """Get and refresh Google credentials."""
        try:
            # Try Service Account first
            service_account = ServiceAccount.query.filter_by(name=self.account_name).first()
            if service_account:
                logger.info(f"Auth path: ServiceAccount DWD for '{self.account_name}'")
                from services.google_service_account import GoogleServiceAccount
                gsa = GoogleServiceAccount(service_account.id)
                return gsa.get_credentials()

            # Fallback to Google Account (deprecated)
            account = GoogleAccount.query.filter_by(account_name=self.account_name).first()
            if account and account.tokens:
                logger.info(f"Auth path: GoogleAccount OAuth for '{self.account_name}'")
            if not account or not account.tokens:
                logger.error(f"No tokens found for account: {self.account_name}")
                return None
            
            token = account.tokens[0]
            scopes = [scope.name for scope in token.scopes]
            
            creds = Credentials(
                token=token.token,
                refresh_token=token.refresh_token,
                token_uri=token.token_uri,
                client_id=account.client_id,
                client_secret=account.client_secret,
                scopes=scopes
            )
            
            if creds.expired and creds.refresh_token:
                creds.refresh(google.auth.transport.requests.Request())
            
            return creds if creds.valid else None
        
        except Exception as e:
            logger.error(f"Error getting credentials for {self.account_name}: {e}")
            return None
    
    def _get_admin_service(self):
        """Get or create Admin SDK service."""
        if self._admin_service:
            return self._admin_service
        
        creds = self._get_credentials()
        if not creds:
            raise Exception("Failed to get valid credentials")
        
        self._admin_service = build('admin', 'directory_v1', credentials=creds)
        return self._admin_service
    
    def _get_site_verification_service(self, force_refresh=False, without_delegation=False):
        """Get or create Site Verification API service.
        
        Args:
            force_refresh: Force rebuild the service with new credentials
            without_delegation: If True, use service account without impersonating admin
        """
        cache_key = 'without_delegation' if without_delegation else 'with_delegation'
        
        if not force_refresh and hasattr(self, '_sv_cache') and cache_key in self._sv_cache:
            return self._sv_cache[cache_key]
        
        if force_refresh:
            logger.info(f"Force refreshing Site Verification service (without_delegation={without_delegation})")
        
        try:
            # Get credentials for Site Verification
            creds = self._get_credentials_for_site_verification(without_delegation)
            if not creds:
                raise Exception("Failed to get valid credentials for Site Verification API")
            
            service = build('siteVerification', 'v1', credentials=creds)
            
            # Cache the service
            if not hasattr(self, '_sv_cache'):
                self._sv_cache = {}
            self._sv_cache[cache_key] = service
            
            logger.info(f"Site Verification service built successfully (without_delegation={without_delegation})")
            return service
        except Exception as e:
            logger.error(f"Failed to build Site Verification service: {e}")
            raise
    
    def _get_credentials_for_site_verification(self, without_delegation=False):
        """Get credentials specifically for Site Verification API."""
        try:
            service_account_row = ServiceAccount.query.filter_by(name=self.account_name).first()
            if service_account_row:
                from services.google_service_account import GoogleServiceAccount
                import json
                from google.oauth2 import service_account as sa_lib
                
                gsa = GoogleServiceAccount(service_account_row.id)
                
                if without_delegation:
                    # Use service account credentials WITHOUT impersonating a user
                    # Some APIs work better with direct service account auth
                    logger.info("Using Site Verification credentials WITHOUT domain delegation")
                    creds = sa_lib.Credentials.from_service_account_info(
                        gsa.credentials_info,
                        scopes=['https://www.googleapis.com/auth/siteverification']
                    )
                    return creds
                else:
                    # Use standard delegated credentials (impersonate admin)
                    logger.info(f"Using Site Verification credentials WITH domain delegation to {gsa.admin_email}")
                    return gsa.get_credentials()
            
            # Fallback to standard credentials
            return self._get_credentials()
        except Exception as e:
            logger.error(f"Error getting Site Verification credentials: {e}")
            return None
    
    def ensure_domain_added(self, apex: str) -> Dict:
        """
        Add domain to Google Workspace if not already present.
        If domain already exists or we get permission errors, treat as success and continue.
        
        Args:
            apex: Apex domain to add
        
        Returns:
            Dict with 'created' (bool) and 'already_exists' (bool)
        """
        try:
            service = self._get_admin_service()
            
            # First, check if domain already exists by trying to get it
            try:
                domain_info = service.domains().get(customer='my_customer', domainName=apex).execute()
                logger.info(f"Domain {apex} already exists in Workspace (verified via get)")
                return {'created': False, 'already_exists': True}
            except HttpError as get_error:
                if get_error.resp.status == 404:
                    # Domain doesn't exist, continue to add it
                    logger.info(f"Domain {apex} not found, will attempt to add")
                elif get_error.resp.status == 403:
                    # Permission denied
                    # Try listing domains to check if it really exists
                    logger.warning(f"403 error getting domain {apex}, checking via list...")
                    try:
                        domains = service.domains().list(customer='my_customer').execute()
                        existing_domains = [d.get('domainName', '') for d in domains.get('domains', [])]
                        if apex in existing_domains:
                            logger.info(f"Domain {apex} found in domain list - already exists")
                            return {'created': False, 'already_exists': True}
                        else:
                            # Domain doesn't exist and we got 403. 
                            # DO NOT assume success. This is a hard failure.
                            error_msg = f"Permission denied (403) accessing Google Workspace. Domain {apex} not found in account."
                            logger.error(error_msg)
                            raise Exception(error_msg)
                    except Exception as list_error:
                        # If we can't even list domains, we definitely don't have access
                        logger.error(f"Error listing domains: {list_error}")
                        raise Exception(f"Permission denied (403) and unable to list domains: {str(list_error)}")
                else:
                    # Other error, continue to try adding
                    logger.warning(f"Error getting domain {apex}: {get_error}")
            
            # Try to list all domains to check existence
            try:
                domains = service.domains().list(customer='my_customer').execute()
                existing_domains = [d.get('domainName', '') for d in domains.get('domains', [])]
                
                if apex in existing_domains:
                    logger.info(f"Domain {apex} already exists in Workspace (from list)")
                    return {'created': False, 'already_exists': True}
            except HttpError as list_error:
                logger.warning(f"Error listing domains: {list_error}")
                # Continue to try adding
            
            # Add domain
            domain_body = {'domainName': apex}
            try:
                result = service.domains().insert(customer='my_customer', body=domain_body).execute()
                logger.info(f"Successfully added domain {apex} to Workspace")
                return {'created': True, 'already_exists': False, 'domain': result}
            
            except HttpError as e:
                error_str = str(e)
                status_code = e.resp.status if hasattr(e, 'resp') else None
                
                if 'already exists' in error_str.lower() or 'duplicate' in error_str.lower():
                    logger.info(f"Domain {apex} already exists (caught during insert)")
                    return {'created': False, 'already_exists': True}
                elif status_code == 403:
                    # Permission denied during insert
                    logger.error(f"403 Forbidden adding domain {apex}. Check permissions/scopes.")
                    raise Exception(f"Permission denied (403) adding domain. Check Service Account scopes and Domain-Wide Delegation.")
                else:
                    raise
        
        except HttpError as e:
            error_str = str(e)
            status_code = e.resp.status if hasattr(e, 'resp') else None
            
            if status_code == 403:
                logger.error(f"403 Forbidden accessing Google Workspace API for {apex}")
                raise Exception(f"Permission denied (403) accessing Google Workspace. Check credentials.")
            
            logger.error(f"HTTP error adding domain {apex}: {e}")
            raise Exception(f"Failed to add domain: {str(e)}")
        
        except Exception as e:
            logger.error(f"Error adding domain {apex}: {e}")
            raise
    
    def get_verification_token(self, domain: str, apex_domain: str = None) -> Dict:
        """
        Get DNS TXT verification token from Google Site Verification API.
        
        Args:
            domain: Domain to verify (can be subdomain or apex)
            apex_domain: Optional apex domain to fall back to if subdomain fails
        
        Returns:
            Dict with 'token' (str), 'host' (str, default '@'), 'method' (str)
        """
        # Try to get token, with fallback to apex domain if subdomain fails
        domains_to_try = [domain]
        if apex_domain and apex_domain != domain:
            domains_to_try.append(apex_domain)
        
        last_error = None
        
        for try_domain in domains_to_try:
            try:
                logger.info(f"Requesting verification token for: {try_domain}")
                token_result = self._request_verification_token(try_domain)
                if token_result:
                    return token_result
            except Exception as e:
                last_error = e
                logger.warning(f"Failed to get token for {try_domain}: {e}")
                if try_domain != domains_to_try[-1]:
                    logger.info(f"Trying next domain in fallback list...")
                    continue
                raise
        
        if last_error:
            raise last_error
        raise Exception("Failed to get verification token from Google")
    
    def _request_verification_token(self, domain: str) -> Dict:
        """Internal method to request verification token for a specific domain.
        
        Tries with domain delegation first, then without if that fails with 503.
        """
        # Try with delegation first, then without
        delegation_modes = [False, True]  # Try WITH delegation, then WITHOUT
        
        for without_delegation in delegation_modes:
            try:
                service = self._get_site_verification_service(without_delegation=without_delegation)
                
                verification_request = {
                    'site': {
                        'type': 'INET_DOMAIN',
                        'identifier': domain
                    },
                    'verificationMethod': 'DNS_TXT'
                }
                
                # Retry logic for 503 errors
                token_response = None
                max_retries = 3  # Fewer retries per mode
                last_error = None
                
                for attempt in range(max_retries):
                    try:
                        token_response = service.webResource().getToken(body=verification_request).execute()
                        logger.info(f"Successfully got token for {domain} (without_delegation={without_delegation})")
                        break
                    except HttpError as e:
                        last_error = e
                        if e.resp.status == 503:
                            if attempt < max_retries - 1:
                                wait_time = 2 + (attempt * 2) + random.uniform(0, 1)
                                logger.warning(f"503 error for {domain} (mode={without_delegation}), retry in {wait_time:.1f}s (Attempt {attempt+1}/{max_retries})")
                                time.sleep(wait_time)
                                continue
                            else:
                                # All retries for this mode failed
                                logger.warning(f"503 errors exhausted for {domain} with without_delegation={without_delegation}")
                                break  # Try next delegation mode
                        raise e
                
                if token_response:
                    token = token_response.get('token', '')
                    if token:
                        logger.info(f"Got verification token for {domain}: {token[:40]}... (delegation_mode={without_delegation})")
                        
                        # Fix: Check if token already contains the prefix (Google sometimes returns the full value)
                        if token.startswith('google-site-verification='):
                            txt_value = token
                            # Extract the actual token for the return value
                            actual_token = token.replace('google-site-verification=', '', 1)
                        else:
                            txt_value = f'google-site-verification={token}'
                            actual_token = token
                        
                        return {
                            'token': actual_token,
                            'host': '@',
                            'method': 'DNS_TXT',
                            'txt_value': txt_value,
                            'without_delegation': without_delegation  # Track which mode was used
                        }
                    else:
                        logger.error(f"Empty token for {domain}. Response: {token_response}")
                        raise Exception("No token returned from Google")
                
            except HttpError as e:
                if e.resp.status != 503:
                    raise  # Non-503 error, don't retry with different mode
                last_error = e
            except Exception as e:
                last_error = e
                logger.error(f"Error with without_delegation={without_delegation}: {e}")
        
        # All modes failed
        if last_error:
            raise last_error
        raise Exception("Failed to get verification token from Google Site Verification API")
    
    
    def verify_domain(self, domain: str, apex_domain: str = None, without_delegation: bool = None) -> Dict:
        """
        Verify domain in Google Workspace after DNS TXT record is created.
        
        This is a TWO-STEP process:
        1. Verify domain ownership via Site Verification API (proves we own the domain)
        2. Check the domain verification status in Google Workspace Admin SDK
        
        Args:
            domain: Domain to verify (can be subdomain like 'sub.example.com' or apex 'example.com')
            apex_domain: Optional pre-calculated apex domain.
            without_delegation: If specified, use this specific delegation mode (should match the mode
                               used when getToken was called for consistent token identity).
        
        Returns:
            Dict with 'verified' (bool) and 'status' (str)
        """
        logger.info(f"Starting domain verification for {domain}")
        
        # Use provided apex or calculate it
        if apex_domain:
            apex = apex_domain
            logger.info(f"Using provided apex domain: {apex}")
        else:
            from services.zone_utils import to_apex
            apex = to_apex(domain)
            logger.info(f"Calculated apex domain: {apex}")
        
        # Determine which domain to verify with Site Verification API
        # For subdomains, we verify the SUBDOMAIN because that's where the TXT record is
        verification_domain = domain
        logger.info(f"Will verify domain: {verification_domain}")
        
        # STEP 1: Site Verification API - Verify ownership using webResource().insert()
        site_verification_success = False
        site_verification_error = None
        
        # If specific delegation mode provided (from getToken), prioritize it but allow fallback
        if without_delegation is not None:
            # Try the specific mode first, then the other one as fallback
            delegation_modes = [without_delegation, not without_delegation]
            logger.info(f"Prioritizing delegation mode from getToken: without_delegation={without_delegation} (fallback to {not without_delegation})")
        else:
            delegation_modes = [False, True]  # WITH delegation first, then WITHOUT
            logger.info(f"No specific delegation mode, trying both modes")
        
        for mode in delegation_modes:
            if site_verification_success:
                break
                
            try:
                logger.info(f"Site Verification for {verification_domain} (without_delegation={mode})")
                service = self._get_site_verification_service(without_delegation=mode)
                
                # Get admin email for owner field
                service_account_row = ServiceAccount.query.filter_by(name=self.account_name).first()
                admin_email = service_account_row.admin_email if service_account_row else None
                
                # CRITICAL FIX: Create verification resource with ONLY the site object
                # The verificationMethod is a parameter, NOT part of the body
                verification_resource = {
                    'site': {
                        'type': 'INET_DOMAIN',
                        'identifier': verification_domain
                    }
                }
                
                # Note: owners field is not required and may cause issues
                # Only add if we want to explicitly set owners
                # if admin_email:
                #     verification_resource['owners'] = [admin_email]
                #     logger.info(f"Using admin email as owner: {admin_email}")
                
                # Retry with exponential backoff for webResource().insert()
                # Increased to 8 retries to handle slow DNS propagation (total wait ~540s)
                max_retries = 8
                for attempt in range(max_retries):
                    try:
                        # CRITICAL FIX: Only pass site in body, verificationMethod is parameter only
                        result = service.webResource().insert(
                            verificationMethod='DNS_TXT',
                            body=verification_resource
                        ).execute()
                        
                        logger.info(f"✅ Site Verification API response: {result}")
                        
                        # Check if we got a valid response
                        if result.get('id') or result.get('site', {}).get('identifier'):
                            logger.info(f"✅ Site Verification succeeded for {verification_domain}")
                            site_verification_success = True
                            break
                        else:
                            logger.warning(f"Unexpected verification response: {result}")
                            # Even without expected fields, if no error, consider success
                            site_verification_success = True
                            break
                            
                    except HttpError as e:
                        error_str = str(e)
                        status = e.resp.status if hasattr(e, 'resp') else 'unknown'
                        logger.warning(f"Site Verification HTTP {status} (attempt {attempt+1}/{max_retries}): {error_str}")
                        
                        # 409 means already verified - that's success!
                        if status == 409 or 'already exists' in error_str.lower() or 'already verified' in error_str.lower():
                            logger.info(f"✅ Domain {verification_domain} already verified in Site Verification (409)")
                            site_verification_success = True
                            break
                        
                        # 400 - usually DNS not propagated or validation failed
                        if status == 400:
                            # Always retry on 400 because it means DNS is not propagated yet
                            # Google's error messages vary and might not always contain "token"
                            if attempt < max_retries - 1:
                                wait_time = 15 * (attempt + 1)  # 15s, 30s, 45s, 60s, 75s...
                                logger.info(f"⏳ Verification failed (400) for {verification_domain}. DNS likely not ready. Waiting {wait_time}s before retry... Error: {error_str[:150]}")
                                time.sleep(wait_time)
                                continue
                            else:
                                site_verification_error = f"Verification failed after {max_retries} attempts (DNS likely not propagated). Please wait for DNS propagation (can take up to 48 hours). Error: {error_str}"
                                break
                        
                        # 403 - permission denied
                        if status == 403:
                            site_verification_error = f"Permission denied (403). Check service account permissions and Domain-Wide Delegation."
                            logger.error(f"❌ 403 Forbidden for {verification_domain}")
                            break
                        
                        # 503 - service unavailable, retry
                        if status == 503:
                            if attempt < max_retries - 1:
                                wait_time = 5 * (attempt + 1)
                                logger.info(f"⏳ 503 Service unavailable, waiting {wait_time}s before retry...")
                                time.sleep(wait_time)
                                continue
                            else:
                                site_verification_error = "Google API service unavailable after multiple retries"
                                break
                        
                        # Other HTTP error - don't retry
                        site_verification_error = f"HTTP {status}: {error_str}"
                        logger.error(f"❌ Non-retryable error: {site_verification_error}")
                        break
                        
                    except Exception as inner_e:
                        site_verification_error = f"Unexpected error: {str(inner_e)}"
                        logger.error(f"❌ Exception during verification attempt: {inner_e}")
                        break
                        
            except Exception as e:
                site_verification_error = str(e)
                logger.error(f"Site Verification error: {e}")
        
        if not site_verification_success:
            error_msg = f"Site Verification failed: {site_verification_error or 'Unknown error'}"
            logger.error(error_msg)
            return {'verified': False, 'status': 'failed', 'error': error_msg}
        
        # STEP 2: Check verification status in Google Workspace Admin SDK
        try:
            logger.info(f"Checking verification status in Workspace Admin SDK for {verification_domain}")
            admin_service = self._get_admin_service()
            
            # Loop to check domain status because Workspace Admin SDK can take a few minutes to sync
            max_sync_checks = 12 # 12 attempts * 10 seconds = up to 2 minutes of waiting for Google's internal sync
            current_verified = False
            domain_not_found = False
            
            for check in range(max_sync_checks):
                try:
                    domain_info = admin_service.domains().get(customer='my_customer', domainName=verification_domain).execute()
                    current_verified = domain_info.get('verified', False)
                    logger.info(f"Workspace verification status for {verification_domain} (check {check+1}): {current_verified}")
                    
                    if current_verified:
                        logger.info(f"✅ Domain {verification_domain} is fully verified in Google Workspace!")
                        return {'verified': True, 'status': 'verified'}
                    elif check < max_sync_checks - 1:
                        logger.info(f"⏳ Site Verification succeeded, but Workspace still says 'unverified'. Waiting 10s for Google to internally sync...")
                        time.sleep(10)
                        
                except HttpError as e:
                    if e.resp.status == 404:
                        logger.info(f"Domain {verification_domain} not found in Workspace (404) during check {check+1}")
                        domain_not_found = True
                        break # Domain not in workspace, exit loop to handle below
                    elif check < max_sync_checks - 1:
                        logger.warning(f"Error checking status: {e}, will retry...")
                        time.sleep(5)
                    else:
                        raise e
            
            if domain_not_found:
                # Domain not in Workspace - add it
                logger.info(f"Domain {verification_domain} not in Workspace, adding...")
                try:
                    self.ensure_domain_added(verification_domain)
                    # We added it, but it might still need time to sync internally
                    # Return verified: False so the UI knows it's not ready yet
                    return {
                        'verified': False,
                        'status': 'syncing',
                        'error': 'Domain just added to Workspace. Verification syncing - may take a few minutes to complete before you can use it.'
                    }
                except Exception as add_error:
                    logger.warning(f"Could not add domain: {add_error}")
                    return {
                        'verified': False,
                        'status': 'failed',
                        'error': f'Site Verification complete, but could not add domain to Workspace automatically: {add_error}'
                    }
            
            # If we exhausted the loop and it's still not verified
            if not current_verified:
                logger.warning(f"Workspace Admin SDK did not sync verification for {verification_domain} within limit.")
                return {
                    'verified': False, 
                    'status': 'syncing', 
                    'error': 'Site Verification succeeded, but Google Workspace Admin is taking a long time to sync. Please wait up to 15-30 minutes.'
                }
                    
        except Exception as e:
            logger.error(f"Workspace/Site verification error for {verification_domain}: {e}", exc_info=True)
            return {
                'verified': False,
                'status': 'failed', 
                'error': f'Verification error: {str(e)}'
            }
    
    def is_verified(self, apex: str) -> bool:
        """
        Check if domain is already verified in Google Workspace.
        
        Args:
            apex: Apex domain to check
        
        Returns:
            True if verified, False otherwise
        """
        try:
            admin_service = self._get_admin_service()
            
            # Get domain info
            try:
                domain_info = admin_service.domains().get(customer='my_customer', domainName=apex).execute()
                verified = domain_info.get('verified', False)
                logger.info(f"Domain {apex} verification status: {verified}")
                return verified
            
            except HttpError as e:
                if e.resp.status == 404:
                    logger.info(f"Domain {apex} not found in Workspace")
                    return False
                else:
                    logger.error(f"Error checking verification status for {apex}: {e}")
                    return False
        
        except Exception as e:
            logger.error(f"Error checking if domain {apex} is verified: {e}")
            return False
