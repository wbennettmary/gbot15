import json
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build
from database import ServiceAccount

logger = logging.getLogger(__name__)

class GoogleServiceAccount:
    SCOPES = [
        "https://www.googleapis.com/auth/admin.directory.user", 
        "https://www.googleapis.com/auth/admin.directory.user.security", 
        "https://www.googleapis.com/auth/admin.directory.orgunit", 
        "https://www.googleapis.com/auth/admin.directory.domain",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/siteverification"
    ]

    def __init__(self, service_account_id):
        self.service_account_db = ServiceAccount.query.get(service_account_id)
        if not self.service_account_db:
            raise ValueError(f"Service Account with ID {service_account_id} not found")
        
        try:
            self.credentials_info = json.loads(self.service_account_db.json_content)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON content in Service Account")

        self.admin_email = self.service_account_db.admin_email

    def get_credentials(self):
        """
        Returns credentials with Domain-Wide Delegation for the admin user.
        """
        creds = service_account.Credentials.from_service_account_info(
            self.credentials_info, scopes=self.SCOPES
        )
        # Delegate to the admin user
        logger.info(f"Attempting DWD Auth - Client ID: {self.credentials_info.get('client_id')} | Subject: {self.admin_email}")
        logger.info(f"Scopes: {self.SCOPES}")
        delegated_creds = creds.with_subject(self.admin_email)
        return delegated_creds

    def build_service(self, service_name, version):
        """
        Builds a Google API service object using the delegated credentials.
        """
        try:
            creds = self.get_credentials()
            service = build(service_name, version, credentials=creds)
            return service
        except Exception as e:
            logger.error(f"Failed to build service {service_name}: {str(e)}")
            raise

    def verify_connection(self):
        """
        Verifies that the service account can authenticate and delegate.
        Tries to list users (limit 1) to confirm access.
        """
        try:
            service = self.build_service('admin', 'directory_v1')
            # Match G_Bot_api.py verification method exactly
            service.domains().list(customer='my_customer').execute()
            return True, "Connection successful"
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Service Account verification failed: {error_msg}")
            return False, error_msg
