import os
import json
import logging
import uuid
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.auth.transport.requests
from flask import session
from database import db, GoogleAccount, GoogleToken, ServiceAccount
from services.google_service_account import GoogleServiceAccount
from config import SCOPES

# Use a more robust session service storage
_session_services = {}

class WebGoogleAPI:
    def get_credentials(self, account_name):
        account = GoogleAccount.query.filter_by(account_name=account_name).first()
        if not account or not account.tokens:
            return None
        
        token = account.tokens[0]
        scopes = [scope.name for scope in token.scopes]
        
        return Credentials(
            token=token.token,
            refresh_token=token.refresh_token,
            token_uri=token.token_uri,
            client_id=account.client_id,
            client_secret=account.client_secret,
            scopes=scopes
        )

    def has_valid_tokens(self, account_name):
        creds = self.get_credentials(account_name)
        if creds:
            return creds.valid
            
        # Check if it's a service account
        service_account = ServiceAccount.query.filter_by(name=account_name).first()
        if service_account:
            return True
            
        return False

    def is_token_valid(self, account_name):
        """Alias for has_valid_tokens for backward compatibility"""
        return self.has_valid_tokens(account_name)

    def authenticate_with_tokens(self, account_name):
        # Try OAuth first
        creds = self.get_credentials(account_name)
        if creds:
            if creds.expired and creds.refresh_token:
                creds.refresh(google.auth.transport.requests.Request())

            if creds.valid:
                service = build('admin', 'directory_v1', credentials=creds)
                self._set_current_service(account_name, service)
                return True
        
        # Try Service Account
        service_account = ServiceAccount.query.filter_by(name=account_name).first()
        if service_account:
            try:
                gsa = GoogleServiceAccount(service_account.id)
                service = gsa.build_service('admin', 'directory_v1')
                self._set_current_service(account_name, service)
                return True
            except Exception as e:
                logging.error(f"Failed to authenticate service account {account_name}: {e}")
                return False
                
        return False

    def get_oauth_url(self, account_name, creds_data):
        flow_config = {
            "installed": {
                "client_id": creds_data['client_id'],
                "project_id": "gbot-project",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_secret": creds_data['client_secret'],
                "redirect_uris": ["https://ecochains.online/oauth-callback"]
            }
        }
        flow = InstalledAppFlow.from_client_config(flow_config, scopes=SCOPES)
        flow.redirect_uri = "https://ecochains.online/oauth-callback"
        auth_url, state = flow.authorization_url(
            access_type='offline',
            prompt='consent',
            include_granted_scopes='true'
        )
        return auth_url

    def _get_session_id(self):
        if 'session_id' not in session:
            session['session_id'] = str(uuid.uuid4())
        return session['session_id']

    def _get_session_key(self, account_name):
        session_id = self._get_session_id()
        return f"{session_id}_{account_name}"

    def _get_current_service(self):
        current_account = session.get('current_account_name')
        if not current_account:
            logging.warning("No current account in session")
            return None
        
        service_key = self._get_session_key(current_account)
        service = _session_services.get(service_key)
        
        if service is None:
            logging.info(f"No service found for account {current_account}, attempting to recreate...")
            # Try to recreate the service if it doesn't exist
            if self.is_token_valid(current_account):
                success = self.authenticate_with_tokens(current_account)
                if success:
                    service = _session_services.get(service_key)
                    logging.info(f"Successfully recreated service for account {current_account}")
                else:
                    logging.error(f"Failed to recreate service for account {current_account}")
            else:
                logging.error(f"No valid tokens for account {current_account}")
        
        return service

    def _set_current_service(self, account_name, service):
        service_key = self._get_session_key(account_name)
        _session_services[service_key] = service
        session['current_account_name'] = account_name
        logging.info(f"Service set for account {account_name} with key {service_key}")

    @property
    def service(self):
        return self._get_current_service()
    
    def clear_invalid_services(self):
        """Clear any invalid or expired services from the session storage"""
        current_account = session.get('current_account_name')
        if current_account:
            service_key = self._get_session_key(current_account)
            if service_key in _session_services:
                del _session_services[service_key]
                logging.info(f"Cleared invalid service for account {current_account}")
    
    def validate_and_recreate_service(self, account_name):
        """Validate current service and recreate if necessary"""
        if not account_name:
            return False
            
        # Check if we have a valid service
        service = self._get_current_service()
        if service is not None:
            return True
            
        # Try to recreate the service
        if self.is_token_valid(account_name):
            success = self.authenticate_with_tokens(account_name)
            if success:
                logging.info(f"Successfully recreated service for account {account_name}")
                return True
            else:
                logging.error(f"Failed to recreate service for account {account_name}")
                return False
        else:
            logging.error(f"No valid tokens for account {account_name}")
            return False

    def create_gsuite_user(self, first_name, last_name, email, password):
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        # user_body matches Bot_V13.py exactly: suspended=False, orgUnitPath, changePasswordAtNextLogin
        user_body = {
            "primaryEmail": email,
            "name": {
                "givenName": first_name,
                "familyName": last_name
            },
            "password": password,
            "changePasswordAtNextLogin": False,
            "orgUnitPath": "/",
            "suspended": False  # Explicitly create user as ACTIVE (critical!)
        }
        
        try:
            user = self.service.users().insert(body=user_body).execute()
            
            # Post-creation safety check (from Bot_V13.py): Google sometimes creates users
            # in suspended state even when suspended=False is set. Immediately unsuspend if needed.
            # NOTE: No sleep here — gunicorn workers can't use blocking sleeps safely.
            try:
                user_check = self.service.users().get(userKey=email, projection='full').execute()
                if user_check.get('suspended'):
                    logging.warning(f"User {email} created in suspended state. Unsuspending immediately.")
                    self.service.users().update(userKey=email, body={'suspended': False}).execute()
                    logging.info(f"User {email} successfully unsuspended after creation.")
                else:
                    logging.info(f"User {email} created in active state.")
            except HttpError as e_check:
                if e_check.resp.status == 404:
                    # User not yet propagated — try to unsuspend anyway after a moment
                    logging.warning(f"User {email} not yet retrievable (404) — attempting unsuspend anyway.")
                    try:
                        import time; time.sleep(3)
                        self.service.users().update(userKey=email, body={'suspended': False}).execute()
                        logging.info(f"User {email} unsuspended after propagation delay.")
                    except Exception:
                        pass
                else:
                    logging.error(f"Post-creation check/unsuspend failed for {email}: {e_check}")
            except Exception as e_inner:
                logging.error(f"Unexpected error during post-creation check for {email}: {e_inner}")
            
            return {"success": True, "user": user}
        except HttpError as e:
            # Parse specific error types for better user feedback
            error_message = str(e)
            
            # Check for domain user limit error
            if "Domain user limit reached" in error_message or "limitExceeded" in error_message:
                return {
                    "success": False, 
                    "error": "Domain user limit reached. Please upgrade to a paid Google Workspace subscription to create more users.",
                    "error_type": "domain_limit",
                    "raw_error": error_message
                }
            
            # Check for authentication errors
            elif "Not authenticated" in error_message or "unauthorized" in error_message.lower():
                return {
                    "success": False,
                    "error": "Authentication failed. Please re-authenticate your account.",
                    "error_type": "auth_error",
                    "raw_error": error_message
                }
            
            # Check for duplicate user errors
            elif "already exists" in error_message.lower() or "duplicate" in error_message.lower():
                return {
                    "success": False,
                    "error": f"User {email} already exists in this domain.",
                    "error_type": "duplicate_user",
                    "raw_error": error_message
                }
            
            # Check for invalid domain errors
            elif "invalid domain" in error_message.lower() or "domain not found" in error_message.lower():
                return {
                    "success": False,
                    "error": f"Domain not found or invalid. Please check the domain name.",
                    "error_type": "invalid_domain",
                    "raw_error": error_message
                }
            
            # Default error handling
            else:
                return {
                    "success": False, 
                    "error": f"Failed to create user: {error_message}",
                    "error_type": "unknown",
                    "raw_error": error_message
                }

    def get_domain_info(self):
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            domains = self.service.domains().list(customer="my_customer").execute()
            return {"success": True, "domains": domains.get("domains", [])}
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def get_domains_batch(self, page_token=None):
        """Retrieve domains in batches to avoid timeouts with large domain lists.
        
        Args:
            page_token: Optional page token to start from.
            
        Returns:
            dict: { success, domains, next_page_token, total_fetched }
        """
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            request_params = {
                'customer': 'my_customer'
            }
            
            if page_token:
                request_params['pageToken'] = page_token
            
            domains_result = self.service.domains().list(**request_params).execute()
            domains = domains_result.get('domains', [])
            next_token = domains_result.get('nextPageToken')
            
            logging.info(f"Retrieved {len(domains)} domains in batch")
            
            return {
                'success': True,
                'domains': domains,
                'next_page_token': next_token,
                'total_fetched': len(domains)
            }
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def add_domain_alias(self, domain_alias):
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        domain_body = {
            "domainName": domain_alias
        }
        
        try:
            domain = self.service.domains().insert(customer="my_customer", body=domain_body).execute()
            return {"success": True, "domain": domain}
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def delete_domain(self, domain_name):
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            self.service.domains().delete(customer="my_customer", domainName=domain_name).execute()
            return {"success": True, "message": f"Domain {domain_name} deleted successfully."}
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def get_users(self, max_results=None):
        """Retrieve all users from the authenticated Google account (unlimited)"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            all_users = []
            page_token = None
            page_count = 0
            
            while True:
                # Request parameters
                request_params = {
                    'customer': 'my_customer',
                    'maxResults': 500  # Google's maximum per request
                }
                
                if page_token:
                    request_params['pageToken'] = page_token
                
                # Make the API request
                users_result = self.service.users().list(**request_params).execute()
                
                # Add users from this page
                users = users_result.get("users", [])
                all_users.extend(users)
                page_count += 1
                
                # Log progress for large user bases
                if page_count % 10 == 0:  # Log every 10 pages (5000 users)
                    logging.info(f"Retrieved {len(all_users)} users so far...")
                
                # Check if there are more pages
                page_token = users_result.get("nextPageToken")
                if not page_token:
                    break
            
            logging.info(f"Successfully retrieved {len(all_users)} total users across {page_count} pages")
            return {"success": True, "users": all_users, "total_count": len(all_users)}
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def get_users_batch(self, page_token=None, max_pages=5):
        """Retrieve users in batches to avoid long-running single requests.

        Args:
            page_token: Optional page token to start from.
            max_pages: Maximum number of 500-user pages to fetch in this call.

        Returns:
            dict: { success, users, next_page_token, fetched_pages }
        """
        if not self.service:
            raise Exception("Not authenticated or session expired.")

        try:
            batch_users = []
            pages_fetched = 0
            current_token = page_token

            while pages_fetched < max_pages:
                request_params = {
                    'customer': 'my_customer',
                    'maxResults': 500
                }
                if current_token:
                    request_params['pageToken'] = current_token

                users_result = self.service.users().list(**request_params).execute()
                users = users_result.get('users', [])
                batch_users.extend(users)

                current_token = users_result.get('nextPageToken')
                pages_fetched += 1

                if not current_token:
                    break

            return {
                'success': True,
                'users': batch_users,
                'next_page_token': current_token,
                'fetched_pages': pages_fetched
            }
        except HttpError as e:
            return {"success": False, "error": str(e)}

    def create_random_users(self, num_users, domain, password=None):
        """Create random users — profile fields limited to what the standard Admin SDK scope supports."""
        if not self.service:
            raise Exception("Not authenticated or session expired.")

        import random
        import string
        from faker import Faker

        fake = Faker('en_US')

        if not password:
            password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))

        results          = []
        successful_count = 0
        used_emails      = set()
        used_phones      = set()

        DEPARTMENTS = ["Engineering", "Marketing", "Sales", "Operations",
                       "Finance", "HR", "Product", "Customer Success", "Legal", "IT"]

        def _phone():
            """Return a unique E.164 US phone number."""
            for _ in range(40):
                area = random.randint(200, 999)
                num  = random.randint(2000000, 9999999)
                p    = f"+1{area}{num}"
                if p not in used_phones:
                    used_phones.add(p)
                    return p
            p = f"+1{random.randint(2000000000, 9999999999)}"
            used_phones.add(p)
            return p

        for i in range(num_users):
            # ── Unique name + email ───────────────────────────────────────────
            first_name = fake.first_name()
            last_name  = fake.last_name()
            email = None
            for _ in range(15):
                c = f"{first_name.lower()}{last_name.lower()}{random.randint(1000, 9999)}@{domain}"
                if c not in used_emails:
                    email = c
                    used_emails.add(c)
                    break
            if not email:
                email = f"user{i}{random.randint(100, 999)}@{domain}"
                used_emails.add(email)

            # ── Profile info (Faker-generated, all unique) ────────────────────
            work_phone   = _phone()
            home_phone   = _phone()
            mobile_phone = _phone()

            street   = fake.street_address()
            city     = fake.city()
            state    = fake.state_abbr()
            zipcode  = fake.zipcode()

            home_street  = fake.street_address()
            home_city    = fake.city()
            home_state   = fake.state_abbr()
            home_zipcode = fake.zipcode()

            job_title  = fake.job()[:80]
            department = random.choice(DEPARTMENTS)

            # ── User body — ONLY fields supported by standard Admin SDK scope ─
            # Matches Bot_V13.py baseline exactly, plus safe profile extensions.
            # Fields that require extra scopes (recoveryEmail, recoveryPhone,
            # relations, locations, externalIds, emails) are intentionally omitted.
            user_body = {
                "primaryEmail":              email,
                "name": {
                    "givenName":  first_name,
                    "familyName": last_name
                },
                "password":                  password,
                "changePasswordAtNextLogin": False,
                "orgUnitPath":               "/",
                "suspended":                 False,

                # Phone numbers (work, home, mobile)
                "phones": [
                    {"value": work_phone,   "type": "work",   "primary": True},
                    {"value": home_phone,   "type": "home"},
                    {"value": mobile_phone, "type": "mobile"}
                ],

                # Addresses (work + home)
                "addresses": [
                    {
                        "streetAddress": street,
                        "locality":      city,
                        "region":        state,
                        "postalCode":    zipcode,
                        "countryCode":   "US",
                        "type":          "work"
                    },
                    {
                        "streetAddress": home_street,
                        "locality":      home_city,
                        "region":        home_state,
                        "postalCode":    home_zipcode,
                        "countryCode":   "US",
                        "type":          "home"
                    }
                ],

                # Job / organisation info
                "organizations": [
                    {
                        "title":      job_title,
                        "department": department,
                        "primary":    True,
                        "type":       "work"
                    }
                ]
            }

            try:
                self.service.users().insert(body=user_body).execute()

                # Post-creation safety: unsuspend if Google still marks user suspended
                try:
                    chk = self.service.users().get(userKey=email, projection='full').execute()
                    if chk.get('suspended'):
                        logging.warning(f"User {email} created suspended — unsuspending.")
                        self.service.users().update(userKey=email, body={'suspended': False}).execute()
                except HttpError as ec:
                    if ec.resp.status == 404:
                        try:
                            import time; time.sleep(3)
                            self.service.users().update(userKey=email, body={'suspended': False}).execute()
                        except Exception:
                            pass
                    else:
                        logging.error(f"Post-creation check failed for {email}: {ec}")
                except Exception as ei:
                    logging.error(f"Post-creation check error for {email}: {ei}")

                successful_count += 1
                results.append({
                    'email':      email,
                    'first_name': first_name,
                    'last_name':  last_name,
                    'phone':      work_phone,
                    'address':    f"{street}, {city}, {state} {zipcode}",
                    'result':     {'success': True, 'message': 'User created successfully'}
                })

            except HttpError as e:
                err_msg  = str(e)
                err_type = 'unknown'
                if 'limitExceeded' in err_msg or 'Domain user limit' in err_msg:
                    err_type = 'domain_limit'
                elif 'duplicate' in err_msg.lower() or 'already exists' in err_msg.lower():
                    err_type = 'duplicate_user'
                logging.error(f"Failed to create user {email}: {err_msg}")
                results.append({
                    'email':      email,
                    'first_name': first_name,
                    'last_name':  last_name,
                    'result':     {'success': False, 'error': err_msg, 'error_type': err_type}
                })
            except Exception as e:
                logging.error(f"Unexpected error creating user {email}: {e}")
                results.append({
                    'email':      email,
                    'first_name': first_name,
                    'last_name':  last_name,
                    'result':     {'success': False, 'error': str(e), 'error_type': 'unknown'}
                })

        return {
            'success':          True,
            'password':         password,
            'total_requested':  num_users,
            'successful_count': successful_count,
            'failed_count':     num_users - successful_count,
            'results':          results
        }


    def create_random_admin_users(self, num_users, domain, password=None, admin_role='SUPER_ADMIN'):

        """Create multiple random admin users with specified admin roles"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        import random
        import string
        
        # Use provided password or generate a random one
        if not password:
            password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        
        # Common first and last names for random generation
        first_names = [
            "James", "John", "Robert", "Michael", "William", "David", "Richard", "Charles", "Joseph", "Thomas",
            "Christopher", "Daniel", "Paul", "Mark", "Donald", "George", "Kenneth", "Steven", "Edward", "Brian",
            "Ronald", "Anthony", "Kevin", "Jason", "Matthew", "Gary", "Timothy", "Jose", "Larry", "Jeffrey",
            "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen",
            "Nancy", "Lisa", "Betty", "Helen", "Sandra", "Donna", "Carol", "Ruth", "Sharon", "Michelle",
            "Laura", "Sarah", "Kimberly", "Deborah", "Dorothy", "Lisa", "Nancy", "Karen", "Betty", "Helen"
        ]
        
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
            "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
            "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
            "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
            "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts"
        ]
        
        results = []
        successful_count = 0
        
        for i in range(num_users):
            try:
                # Generate random name
                first_name = random.choice(first_names)
                last_name = random.choice(last_names)
                
                # Generate unique email
                base_email = f"{first_name.lower()}.{last_name.lower()}"
                email = f"{base_email}@{domain}"
                
                # Add random number if email might be duplicate
                counter = 1
                while any(result.get('email') == email for result in results):
                    email = f"{base_email}{counter}@{domain}"
                    counter += 1
                
                # Create user with admin privileges (matches Bot_V13.py user_body structure)
                user_body = {
                    'name': {
                        'givenName': first_name,
                        'familyName': last_name
                    },
                    'primaryEmail': email,
                    'password': password,
                    'changePasswordAtNextLogin': False,
                    'orgUnitPath': '/',
                    'isAdmin': True,
                    'isDelegatedAdmin': False,
                    'suspended': False  # Explicitly create as ACTIVE (critical!)
                }
                
                # Create the user
                created_user = self.service.users().insert(body=user_body).execute()
                
                # Post-creation safety check (from Bot_V13.py): immediately unsuspend if needed
                try:
                    user_check = self.service.users().get(userKey=email, projection='full').execute()
                    if user_check.get('suspended'):
                        logging.warning(f"Admin user {email} created suspended. Unsuspending immediately.")
                        self.service.users().update(userKey=email, body={'suspended': False}).execute()
                        logging.info(f"Admin user {email} successfully unsuspended after creation.")
                except HttpError as e_check:
                    if e_check.resp.status == 404:
                        try:
                            import time; time.sleep(3)
                            self.service.users().update(userKey=email, body={'suspended': False}).execute()
                        except Exception:
                            pass
                    else:
                        logging.error(f"Post-creation unsuspend failed for admin user {email}: {e_check}")
                except Exception as e_inner:
                    logging.error(f"Post-creation check error for admin user {email}: {e_inner}")
                
                # For now, we'll create the user as a basic admin
                # Role-specific assignments require additional setup in Google Admin Console
                results.append({
                    'email': email,
                    'admin_role': admin_role,
                    'result': {
                        'success': True,
                        'user_id': created_user.get('id'),
                        'message': f'Admin user created successfully with basic admin privileges. Note: Specific role assignment ({admin_role}) requires additional Google Admin Console configuration.'
                    }
                })
                successful_count += 1
                
                # Small delay to avoid rate limiting
                import time
                time.sleep(0.2)
                
            except HttpError as e:
                error_type = 'unknown'
                if 'domain' in str(e).lower() and 'limit' in str(e).lower():
                    error_type = 'domain_limit'
                elif 'duplicate' in str(e).lower() or 'already exists' in str(e).lower():
                    error_type = 'duplicate_user'
                elif 'permission' in str(e).lower() or 'admin' in str(e).lower():
                    error_type = 'admin_permission_error'
                
                results.append({
                    'email': email if 'email' in locals() else f'user{i+1}@{domain}',
                    'admin_role': admin_role,
                    'result': {
                        'success': False,
                        'error': str(e),
                        'error_type': error_type
                    }
                })
                
            except Exception as e:
                results.append({
                    'email': email if 'email' in locals() else f'user{i+1}@{domain}',
                    'admin_role': admin_role,
                    'result': {
                        'success': False,
                        'error': str(e),
                        'error_type': 'unknown'
                    }
                })
        
        return {
            'success': True,
            'password': password,
            'admin_role': admin_role,
            'total_requested': num_users,
            'successful_count': successful_count,
            'failed_count': num_users - successful_count,
            'results': results
        }

    def update_user_passwords(self, users, new_password):
        """Update passwords for specific users"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        results = []
        successful_count = 0
        
        for email in users:
            try:
                # Update user password using Google Admin SDK
                user_body = {
                    'password': new_password
                }
                
                self.service.users().update(
                    userKey=email,
                    body=user_body
                ).execute()
                
                results.append({
                    'email': email,
                    'success': True
                })
                successful_count += 1
                
                # Small delay to avoid rate limiting
                import time
                time.sleep(0.1)
                
            except Exception as e:
                results.append({
                    'email': email,
                    'success': False,
                    'error': str(e)
                })
        
        return {
            'success': True,
            'total_requested': len(users),
            'successful_count': successful_count,
            'failed_count': len(users) - successful_count,
            'results': results
        }

    def suspend_user(self, email):
        """Suspend a user account"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            # Suspend user by setting suspended to True
            user_body = {
                'suspended': True,
                'suspensionReason': 'Suspended via GBot Web App'
            }
            
            self.service.users().update(
                userKey=email,
                body=user_body
            ).execute()
            
            return {
                'success': True,
                'email': email,
                'message': f'User {email} has been suspended successfully'
            }
            
        except Exception as e:
            return {
                'success': False,
                'email': email,
                'error': str(e)
            }

    def unsuspend_user(self, email):
        """Unsuspend a user account"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            # Unsuspend user by setting suspended to False
            user_body = {
                'suspended': False
            }
            
            self.service.users().update(
                userKey=email,
                body=user_body
            ).execute()
            
            return {
                'success': True,
                'email': email,
                'message': f'User {email} has been unsuspended successfully'
            }
            
        except Exception as e:
            return {
                'success': False,
                'email': email,
                'error': str(e)
            }


    def get_suspended_users(self):
        """Get all suspended users"""
        if not self.service:
            raise Exception("Not authenticated or session expired.")
        
        try:
            # Get all users with suspended status
            users_result = self.service.users().list(
                customer='my_customer',
                maxResults=500,
                orderBy='email'
            ).execute()
            
            users = users_result.get('users', [])
            suspended_users = []
            
            for user in users:
                if user.get('suspended', False):
                    suspended_users.append({
                        'email': user.get('primaryEmail', ''),
                        'name': user.get('name', {}).get('fullName', ''),
                        'firstName': user.get('name', {}).get('givenName', ''),
                        'lastName': user.get('name', {}).get('familyName', ''),
                        'suspended': user.get('suspended', False),
                        'suspensionReason': user.get('suspensionReason', 'No reason provided'),
                        'lastLoginTime': user.get('lastLoginTime', 'Never'),
                        'creationTime': user.get('creationTime', ''),
                        'orgUnitPath': user.get('orgUnitPath', '/'),
                        'isAdmin': user.get('isAdmin', False),
                        'isDelegatedAdmin': user.get('isDelegatedAdmin', False)
                    })
            
            return {
                'success': True,
                'suspended_users': suspended_users,
                'total_count': len(suspended_users)
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def store_app_password(self, user_alias, app_password, domain=None):
        """Store app password for a user alias in PostgreSQL UserAppPassword table"""
        try:
            from database import UserAppPassword, db
            
            # Parse user_alias to get username and domain
            if '@' in user_alias:
                username, user_domain = user_alias.split('@', 1)
            else:
                username = user_alias
                user_domain = domain or '*'
            
            # Check if record exists
            existing = UserAppPassword.query.filter_by(
                username=username.lower(),
                domain=user_domain.lower()
            ).first()
            
            if existing:
                # Update existing record
                existing.app_password = app_password
                existing.updated_at = db.func.current_timestamp()
            else:
                # Create new record
                new_password = UserAppPassword(
                    username=username.lower(),
                    domain=user_domain.lower(),
                    app_password=app_password
                )
                db.session.add(new_password)
            
            db.session.commit()
            
            return {
                'success': True,
                'message': f'App password stored for user alias: {user_alias}'
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def get_app_password(self, user_alias):
        """Get app password for a user alias from PostgreSQL UserAppPassword table"""
        try:
            from database import UserAppPassword
            
            # Parse user_alias to get username and domain
            if '@' in user_alias:
                username, domain = user_alias.split('@', 1)
            else:
                username = user_alias
                domain = '*'
            
            # Try exact match first
            record = UserAppPassword.query.filter_by(
                username=username.lower(),
                domain=domain.lower()
            ).first()
            
            # If no exact match, try with wildcard domain
            if not record and domain != '*':
                record = UserAppPassword.query.filter_by(
                    username=username.lower(),
                    domain='*'
                ).first()
            
            if record:
                return {
                    'success': True,
                    'app_password': record.app_password,
                    'domain': record.domain
                }
            else:
                return {
                    'success': False,
                    'error': f'No app password found for alias: {user_alias}'
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def get_all_app_passwords(self):
        """Get all stored app passwords from PostgreSQL UserAppPassword table"""
        try:
            from database import UserAppPassword
            
            # Query the PostgreSQL UserAppPassword table
            app_password_records = UserAppPassword.query.order_by(UserAppPassword.updated_at.desc()).all()
            
            app_passwords = []
            for record in app_password_records:
                # Create user_alias from username and domain
                user_alias = f"{record.username}@{record.domain}" if record.domain != '*' else record.username
                
                app_passwords.append({
                    'user_alias': user_alias,
                    'app_password': record.app_password,
                    'domain': record.domain,
                    'created_at': record.created_at.isoformat() if record.created_at else None,
                    'updated_at': record.updated_at.isoformat() if record.updated_at else None
                })
            
            return {
                'success': True,
                'app_passwords': app_passwords,
                'total_count': len(app_passwords)
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

google_api = WebGoogleAPI()