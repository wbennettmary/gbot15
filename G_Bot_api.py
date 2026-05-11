import sys
import os
import re
import json
import csv
import random
import time
import base64
import mimetypes
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import concurrent.futures
import threading


# --- PyQt5 Imports ---
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QTextEdit, QFileDialog, QTabWidget,
    QGroupBox, QSpinBox, QMessageBox, QStatusBar, QFormLayout, QProgressBar, QStyle,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMenu,
    QCheckBox, QSplitter, QListWidget, QListWidgetItem, QDateTimeEdit, QSlider,
    QScrollArea
)
from PyQt5.QtCore import QObject, QThread, pyqtSignal, Qt, pyqtSlot, QDateTime, QTimer
from PyQt5.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QTextDocument

# --- Google API Imports ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Constants ---
DEFAULT_ADMIN_EMAIL = ""
DEFAULT_DATA_FILE = 'config/data.json'
SETTINGS_FILE = 'gui_settings.json'

# =============================================================================
# WORKER CLASS (Contains all Google API logic)
# =============================================================================
class GWorkspaceWorker(QObject):
    # Signals for feedback to the GUI
    finished = pyqtSignal()
    log_message = pyqtSignal(str, QColor)
    error = pyqtSignal(str)
    connection_successful = pyqtSignal(object)
    domains_fetched = pyqtSignal(list)
    ous_fetched = pyqtSignal(list)
    dashboard_stats_updated = pyqtSignal(dict)
    all_users_fetched = pyqtSignal(list)
    task_result = pyqtSignal(str, str)
    progress_update = pyqtSignal(int, int)
    email_sent = pyqtSignal(str, str)  # email, status
    email_failed = pyqtSignal(str, str)  # email, error
    campaign_progress = pyqtSignal(int, int)  # current, total
    campaign_finished = pyqtSignal(dict)  # summary stats
    campaign_paused = pyqtSignal()
    campaign_resumed = pyqtSignal()
    lightning_mode_ready = pyqtSignal(int)  # number of emails prepared

    def __init__(self):
        super().__init__()
        self.service = None
        self.gmail_service = None
        self.credentials = None
        self.campaign_state = "stopped"  # stopped, preparing, ready, sending, paused
        self.prepared_emails = []  # Store prepared emails for lightning mode
        self.campaign_threads = []  # Store active sending threads
        self.csv_lock = threading.Lock()


    def _parse_gsuite_error(self, e):
        if isinstance(e, HttpError):
            try:
                error_details = json.loads(e.content).get('error', {})
                return error_details.get('message', 'An unknown Google API error occurred.')
            except: return str(e)
        return str(e)

    def _load_user_data(self):
        try:
            with open(DEFAULT_DATA_FILE, encoding='utf-8') as f: return json.load(f)
        except Exception as e:
            self.error.emit(f"Could not load {DEFAULT_DATA_FILE}: {e}"); return None
    def _generate_password(self):
        chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
        return ''.join(random.choice(chars) for _ in range(random.randint(11, 14)))
    def _sanitize_email(self, email):
        email = email.encode('ascii', 'ignore').decode('ascii')
        return re.sub(r'[^a-zA-Z0-9@.-]', '', email)
    def _save_to_csv(self, email, password, domain):
        file_name, is_new = f"{domain}_user_data.csv", not os.path.exists(f"{domain}_user_data.csv")
        try:
            with open(file_name, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if is_new: writer.writerow(["Email", "Password"])
                writer.writerow([email, password])
        except Exception as e: self.error.emit(f"Failed to save to CSV {file_name}: {e}")

    # --- Worker Slots (Triggered by signals) ---
    @pyqtSlot(str, str)
    def connect_to_google(self, key_file, admin_email):
        try:
            self.log_message.emit("Connecting to Google Workspace...", QColor("cyan"))
            scopes = [
                "https://www.googleapis.com/auth/admin.directory.user", 
                "https://www.googleapis.com/auth/admin.directory.user.security", 
                "https://www.googleapis.com/auth/admin.directory.orgunit", 
                "https://www.googleapis.com/auth/admin.directory.domain.readonly",
                "https://www.googleapis.com/auth/gmail.send"
            ]
            self.credentials = service_account.Credentials.from_service_account_file(key_file, scopes=scopes).with_subject(admin_email)
            self.service = build('admin', 'directory_v1', credentials=self.credentials)
            self.gmail_service = build('gmail', 'v1', credentials=self.credentials)
            self.service.domains().list(customer='my_customer').execute()
            self.log_message.emit(f"Successfully connected as {admin_email}", QColor("lightgreen"))
            self.connection_successful.emit(self.service)
        except Exception as e:
            self.error.emit(f"Connection Failed: {self._parse_gsuite_error(e)}")
        finally:
            self.finished.emit()
    
    @pyqtSlot()
    def fetch_initial_data(self):
        try:
            domains = [d['domainName'] for d in self.service.domains().list(customer='my_customer').execute().get('domains', [])]
            self.domains_fetched.emit(domains)
            ous = self.service.orgunits().list(customerId='my_customer').execute().get('organizationUnits', [])
            ou_choices = [{"name": "Root (No specific OU)", "value": "/"}] + [{"name": ou['name'], "value": ou['orgUnitPath']} for ou in ous]
            self.ous_fetched.emit(ou_choices)
        except Exception as e:
            self.error.emit(f"Error fetching initial data: {self._parse_gsuite_error(e)}")
        finally:
            self.finished.emit()

    @pyqtSlot()
    def fetch_dashboard_stats(self):
        stats = {'total_users': 'N/A', 'suspended_users': 'N/A'}
        try:
            total_count = 0; page_token = None
            while True:
                res = self.service.users().list(customer='my_customer', pageToken=page_token, projection='basic', fields='nextPageToken,users(id)').execute()
                total_count += len(res.get('users', []))
                page_token = res.get('nextPageToken');
                if not page_token: break
            stats['total_users'] = str(total_count)
            suspended_count = 0; page_token = None
            while True:
                res = self.service.users().list(customer='my_customer', pageToken=page_token, projection='basic', query='isSuspended=true', fields='nextPageToken,users(id)').execute()
                suspended_count += len(res.get('users', []))
                page_token = res.get('nextPageToken');
                if not page_token: break
            stats['suspended_users'] = str(suspended_count)
            self.dashboard_stats_updated.emit(stats)
        except Exception as e:
            self.error.emit(f"Failed to fetch dashboard stats: {self._parse_gsuite_error(e)}")
        finally:
            self.finished.emit()
    
    @pyqtSlot(str)
    def fetch_all_users(self, domain):
        all_users_list = []
        try:
            page_token = None
            while True:
                results = self.service.users().list(
                    customer='my_customer',
                    domain=domain,
                    maxResults=500,  # Max allowed page size
                    pageToken=page_token,
                    orderBy='email'
                ).execute()
                all_users_list.extend(results.get('users', []))
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            self.all_users_fetched.emit(all_users_list)
        except Exception as e:
            self.error.emit(f"Failed to fetch all users: {self._parse_gsuite_error(e)}")
        finally:
            self.finished.emit()

    @pyqtSlot(list, str)
    def perform_user_action(self, user_emails, action):
        total = len(user_emails)
        for i, email in enumerate(user_emails):
            self.progress_update.emit(i + 1, total)
            try:
                if action == 'suspend':
                    self.service.users().update(userKey=email, body={'suspended': True}).execute()
                    self.log_message.emit(f"Suspended: {email}", QColor("orange"))
                elif action == 'reactivate':
                    self.service.users().update(userKey=email, body={'suspended': False}).execute()
                    self.log_message.emit(f"Reactivated: {email}", QColor("lightgreen"))
                elif action == 'force_password_reset':
                    self.service.users().update(userKey=email, body={'changePasswordAtNextLogin': True}).execute()
                    self.log_message.emit(f"Forced password reset for: {email}", QColor("cyan"))
                elif action == 'delete':
                    self.service.users().delete(userKey=email).execute()
                    self.log_message.emit(f"DELETED: {email}", QColor("red"))
            except Exception as e:
                self.error.emit(f"Action '{action}' failed for {email}: {self._parse_gsuite_error(e)}")
            time.sleep(0.1)
        self.task_result.emit("Action Complete", f"Performed '{action}' on {total} user(s).")
        self.finished.emit()
    
    @pyqtSlot(str, int, str, bool)
    def create_multiple_users(self, domain, num_users, org_unit_path, lightning_mode=False):
        data = self._load_user_data()
        if not data: return self.finished.emit()
        
        created_count = 0
        
        if lightning_mode:
            self.log_message.emit(f"⚡ LIGHTNING MODE: Creating {num_users} users in parallel...", QColor("yellow"))
            
            # 1. Pre-generate all user data
            users_data = []
            used_names = set()
            
            for _ in range(num_users):
                full_name = ""
                # Try to generate unique name
                for _ in range(20): 
                    gender = random.choice(['male', 'female'])
                    first_name = random.choice(data[f"{gender}_first_names"])
                    last_name = random.choice(data['last_names'])
                    full_name = f"{first_name} {last_name}"
                    if full_name not in used_names:
                        used_names.add(full_name)
                        break
                
                email = self._sanitize_email(f"{first_name.lower()}.{last_name.lower()}@{domain}")
                password = self._generate_password()
                
                users_data.append({
                    'email': email,
                    'firstName': first_name,
                    'lastName': last_name,
                    'password': password
                })
            
            # 2. Define the worker task
            def create_single_user_task(user_info, creds):
                try:
                    # Create a thread-local service instance to avoid race conditions
                    local_service = build('admin', 'directory_v1', credentials=creds, cache_discovery=False)
                    
                    user_body = {
                        "primaryEmail": user_info['email'],
                        "name": {
                            "givenName": user_info['firstName'],
                            "familyName": user_info['lastName']
                        },
                        "password": user_info['password'],
                        "changePasswordAtNextLogin": True,
                        "orgUnitPath": org_unit_path
                    }
                    
                    local_service.users().insert(body=user_body).execute()
                    
                    # Thread-safe CSV writing
                    with self.csv_lock:
                        self._save_to_csv(user_info['email'], user_info['password'], domain)
                        
                    return True, user_info['email']
                except Exception as e:
                    return False, f"{user_info['email']}: {str(e)}"

            # 3. Execute in parallel
            max_workers = min(50, num_users) # Cap at 50 threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all tasks
                future_to_email = {
                    executor.submit(create_single_user_task, u_data, self.credentials): u_data['email'] 
                    for u_data in users_data
                }
                
                # Process results as they complete
                for i, future in enumerate(concurrent.futures.as_completed(future_to_email)):
                    self.progress_update.emit(i + 1, num_users)
                    success, result = future.result()
                    
                    if success:
                        created_count += 1
                        # Optional: Don't log every single success in lightning mode to save UI updates, 
                        # or log every 5-10? For now, let's log errors only or batch success.
                        # self.log_message.emit(f"Created: {result}", QColor("green")) 
                    else:
                        self.log_message.emit(f"Failed: {result}", QColor("red"))

        else:
            # ORIGINAL SEQUENTIAL LOGIC
            created_count = 0
            used_names = set()
            for i in range(num_users):
                self.progress_update.emit(i + 1, num_users)
                full_name = ""
                for _ in range(10): # Try 10 times to find a unique name
                    gender = random.choice(['male', 'female'])
                    first_name = random.choice(data[f"{gender}_first_names"])
                    last_name = random.choice(data['last_names'])
                    full_name = f"{first_name} {last_name}"
                    if full_name not in used_names:
                        used_names.add(full_name); break
                else:
                    self.log_message.emit("Could not generate a unique name, skipping.", QColor("orange"))
                    continue
                email = self._sanitize_email(f"{first_name.lower()}.{last_name.lower()}@{domain}")
                password = self._generate_password()
                try:
                    user_body = {"primaryEmail": email, "name": {"givenName": first_name, "familyName": last_name}, "password": password, "changePasswordAtNextLogin": True, "orgUnitPath": org_unit_path}
                    self.service.users().insert(body=user_body).execute()
                    created_count += 1; self._save_to_csv(email, password, domain)
                except Exception as e:
                    self.log_message.emit(f"Error creating {email}: {self._parse_gsuite_error(e)}", QColor("red"))
                time.sleep(0.1)

        self.task_result.emit("Creation Complete", f"Created {created_count} of {num_users} users.")
        self.finished.emit()

    @pyqtSlot(str, str, str)
    def delete_users(self, domain, file_path, admin_email):
        emails = []
        try:
            if file_path:
                with open(file_path, 'r') as f: emails = [l.strip() for l in f if l.strip()]
            else:
                page_token = None
                while True:
                    res = self.service.users().list(customer='my_customer', domain=domain, pageToken=page_token).execute()
                    emails.extend([u['primaryEmail'] for u in res.get('users', []) if u['primaryEmail'].lower() != admin_email.lower()])
                    page_token = res.get('nextPageToken');
                    if not page_token: break
        except Exception as e: return self.error.emit(f"Failed to get user list: {self._parse_gsuite_error(e)}") or self.finished.emit()
        if not emails: return self.log_message.emit("No users to delete.", QColor("yellow")) or self.finished.emit()
        deleted = 0
        for i, email in enumerate(emails):
            self.progress_update.emit(i + 1, len(emails))
            try:
                self.service.users().delete(userKey=email).execute(); deleted += 1
            except Exception as e:
                self.log_message.emit(f"Error deleting {email}: {self._parse_gsuite_error(e)}", QColor("red"))
            time.sleep(0.1)
        self.task_result.emit("Deletion Complete", f"Deleted {deleted} of {len(emails)} users.")
        self.finished.emit()

    @pyqtSlot(str, str)
    def change_user_domain(self, source_email, new_domain):
        self.progress_update.emit(0, 1)
        try:
            new_email = self._sanitize_email(f"{source_email.split('@')[0]}@{new_domain}")
            self.service.users().update(userKey=source_email, body={"primaryEmail": new_email}).execute()
            self.task_result.emit("Success", f"Changed {source_email} -> {new_email}")
        except Exception as e: self.error.emit(f"Failed for {source_email}: {self._parse_gsuite_error(e)}")
        self.progress_update.emit(1, 1)
        self.finished.emit()
    
    @pyqtSlot(str, str, int, str)
    def bulk_change_domain(self, source, target, limit, admin_email):
        users = []
        try:
            page_token = None
            while True:
                res = self.service.users().list(customer='my_customer', domain=source, pageToken=page_token).execute()
                users.extend([u['primaryEmail'] for u in res.get('users', []) if u['primaryEmail'].lower() != admin_email.lower()])
                page_token = res.get('nextPageToken');
                if not page_token: break
        except Exception as e: return self.error.emit(f"Failed to get user list: {self._parse_gsuite_error(e)}") or self.finished.emit()
        if 0 < limit < len(users): users = users[:limit]
        if not users: return self.log_message.emit("No users to migrate.", QColor("yellow")) or self.finished.emit()
        migrated = 0
        for i, email in enumerate(users):
            self.progress_update.emit(i + 1, len(users))
            try:
                new_email = f"{email.split('@')[0]}@{target}"
                self.service.users().update(userKey=email, body={"primaryEmail": new_email}).execute(); migrated += 1
            except Exception as e: self.log_message.emit(f"Failed {email}: {self._parse_gsuite_error(e)}", QColor("red"))
            time.sleep(0.1)
        self.task_result.emit("Migration Complete", f"Migrated {migrated} of {len(users)} users.")
        self.finished.emit()
    
    @pyqtSlot(str, str)
    def bulk_change_domain_from_file(self, file_path, target_domain):
        try:
            with open(file_path, 'r') as f: lines = [l.strip() for l in f if l.strip()]
        except Exception as e: return self.error.emit(f"Failed to read file: {e}") or self.finished.emit()
        output_file, processed = f"{target_domain}_migrated_users.txt", 0
        with open(output_file, 'w') as outfile:
            for i, line in enumerate(lines):
                self.progress_update.emit(i + 1, len(lines))
                if len(line.split(',')) < 5: continue
                smtp, port, email, password, _ = line.split(',')[:5]
                new_email = f"{email.split('@')[0]}@{target_domain}"
                try:
                    self.service.users().update(userKey=email, body={"primaryEmail": new_email}).execute()
                except Exception as e: self.log_message.emit(f"GWS Error for {email}: {self._parse_gsuite_error(e)}. Updating file only.", QColor("orange"))
                outfile.write(f"{smtp},{port},{new_email},{password},{new_email}\n"); processed += 1
        self.task_result.emit("File Migration Complete", f"Processed {processed} records. Output: {output_file}")
        self.finished.emit()
    
    @pyqtSlot(str, str)
    def count_users(self, domain, count_type):
        query, desc = (None, "total") if count_type == 'total' else ("isSuspended=true", "suspended")
        count, page_token = 0, None
        try:
            self.log_message.emit(f"Counting {desc} users... this may take a moment.", QColor("cyan"))
            while True:
                res = self.service.users().list(customer='my_customer', domain=domain, query=query, pageToken=page_token, projection='basic', fields='nextPageToken,users(id)').execute()
                count += len(res.get('users', []))
                page_token = res.get('nextPageToken');
                if not page_token: break
            self.task_result.emit("Count Complete", f"Found {count} {desc} users in {domain}.")
        except Exception as e: self.error.emit(f"Error counting users: {self._parse_gsuite_error(e)}")
        finally: self.finished.emit()
    
    @pyqtSlot(str)
    def reactivate_suspended_users(self, domain):
        suspended, page_token = [], None
        try:
            while True:
                res = self.service.users().list(customer='my_customer', domain=domain, query='isSuspended=true', pageToken=page_token).execute()
                suspended.extend([u['primaryEmail'] for u in res.get('users', [])])
                page_token = res.get('nextPageToken');
                if not page_token: break
            if not suspended: return self.log_message.emit("No suspended users found.", QColor("yellow")) or self.finished.emit()
            reactivated = 0
            for i, email in enumerate(suspended):
                self.progress_update.emit(i + 1, len(suspended))
                try:
                    self.service.users().update(userKey=email, body={"suspended": False}).execute(); reactivated += 1
                except Exception as e: self.log_message.emit(f"Error reactivating {email}: {self._parse_gsuite_error(e)}", QColor("red"))
            self.task_result.emit("Reactivation Complete", f"Reactivated {reactivated} of {len(suspended)} users.")
        except Exception as e: self.error.emit(f"Error during reactivation: {self._parse_gsuite_error(e)}")
        finally: self.finished.emit()

    # --- Email Sending Methods ---
    def _create_mime_message(self, sender, to, cc, bcc, subject, body, is_html=False, attachments=None, custom_headers=None):
        """Create a MIME message for sending via Gmail API"""
        message = MIMEMultipart()
        message['From'] = sender
        message['To'] = ', '.join(to) if isinstance(to, list) else to
        if cc:
            message['Cc'] = ', '.join(cc) if isinstance(cc, list) else cc
        if bcc:
            message['Bcc'] = ', '.join(bcc) if isinstance(bcc, list) else bcc
        message['Subject'] = subject
        
        # Add custom headers
        if custom_headers:
            for key, value in custom_headers.items():
                message[key] = value
        
        # Add body
        if is_html:
            message.attach(MIMEText(body, 'html'))
        else:
            message.attach(MIMEText(body, 'plain'))
        
        # Add attachments
        if attachments:
            for attachment_path in attachments:
                try:
                    with open(attachment_path, 'rb') as f:
                        attachment = MIMEBase('application', 'octet-stream')
                        attachment.set_payload(f.read())
                        encoders.encode_base64(attachment)
                        attachment.add_header(
                            'Content-Disposition',
                            f'attachment; filename= {os.path.basename(attachment_path)}'
                        )
                        message.attach(attachment)
                except Exception as e:
                    self.log_message.emit(f"Failed to attach {attachment_path}: {e}", QColor("orange"))
        
        return message

    def _send_message_with_retry(self, user_email, message, max_retries=3):
        """Send a message with exponential backoff retry logic"""
        for attempt in range(max_retries):
            try:
                # Create Gmail service with the specific user
                if not self.credentials:
                    raise Exception("No credentials available for Gmail service")
                
                # Create new credentials for the specific user
                user_creds = self.credentials.with_subject(user_email)
                gmail_service = build('gmail', 'v1', credentials=user_creds)
                
                # Encode the message
                raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
                
                # Send the message
                result = gmail_service.users().messages().send(
                    userId='me',
                    body={'raw': raw_message}
                ).execute()
                
                return result
                
            except HttpError as e:
                if e.resp.status in [429, 500, 502, 503, 504]:  # Rate limit or server errors
                    if attempt < max_retries - 1:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff with jitter
                        self.log_message.emit(f"Rate limit hit for {user_email}, retrying in {wait_time:.1f}s...", QColor("yellow"))
                        time.sleep(wait_time)
                        continue
                    else:
                        raise e
                else:
                    raise e
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    self.log_message.emit(f"Error sending to {user_email}, retrying in {wait_time:.1f}s...", QColor("yellow"))
                    time.sleep(wait_time)
                    continue
                else:
                    raise e
        
        return None

    @pyqtSlot(str, str, str, str, str, str, bool, list, dict)
    def send_test_email(self, sender_email, to_email, subject, body, cc="", bcc="", is_html=False, attachments=None, custom_headers=None):
        """Send a test email to verify the setup"""
        try:
            self.log_message.emit(f"Sending test email from {sender_email} to {to_email}...", QColor("cyan"))
            
            # Parse recipients
            to_list = [to_email.strip()] if to_email.strip() else []
            cc_list = [email.strip() for email in cc.split(',') if email.strip()] if cc else []
            bcc_list = [email.strip() for email in bcc.split(',') if email.strip()] if bcc else []
            
            # Create message
            message = self._create_mime_message(
                sender_email, to_list, cc_list, bcc_list, 
                subject, body, is_html, attachments, custom_headers
            )
            
            # Send with retry
            result = self._send_message_with_retry(sender_email, message)
            
            if result:
                self.log_message.emit(f"Test email sent successfully! Message ID: {result.get('id', 'Unknown')}", QColor("lightgreen"))
                self.email_sent.emit(to_email, "Test email sent successfully")
            else:
                self.email_failed.emit(to_email, "Failed to send test email")
                
        except Exception as e:
            error_msg = f"Test email failed: {self._parse_gsuite_error(e)}"
            self.log_message.emit(error_msg, QColor("red"))
            self.email_failed.emit(to_email, error_msg)
        finally:
            self.finished.emit()

    @pyqtSlot(list, list, str, str, str, str, dict, list, bool, int, int, int)
    def send_bulk_emails(self, sender_emails, recipients, subject, body, cc="", bcc="", custom_headers=None, 
                        attachments=None, is_html=False, concurrency=1, rate_limit=60, delay_between_batches=1):
        """Send bulk emails with rotation, rate limiting, and concurrency control"""
        try:
            total_recipients = len(recipients)
            self.log_message.emit(f"Starting bulk email campaign to {total_recipients} recipients...", QColor("cyan"))
            
            # Parse recipients and CC/BCC
            cc_list = [email.strip() for email in cc.split(',') if email.strip()] if cc else []
            bcc_list = [email.strip() for email in bcc.split(',') if email.strip()] if bcc else []
            
            # Statistics tracking
            sent_count = 0
            failed_count = 0
            sender_index = 0
            
            # Process recipients in batches
            for i, recipient in enumerate(recipients):
                try:
                    # Select sender (round-robin if multiple senders)
                    sender_email = sender_emails[sender_index % len(sender_emails)]
                    sender_index += 1
                    
                    # Create personalized message (support for variables like {{name}})
                    personalized_subject = subject
                    personalized_body = body
                    
                    # Simple variable substitution (can be enhanced)
                    if isinstance(recipient, dict):
                        recipient_email = recipient.get('email', '')
                        recipient_name = recipient.get('name', '')
                        personalized_subject = personalized_subject.replace('{{name}}', recipient_name)
                        personalized_body = personalized_body.replace('{{name}}', recipient_name)
                    else:
                        recipient_email = str(recipient)
                    
                    # Create message
                    message = self._create_mime_message(
                        sender_email, [recipient_email], cc_list, bcc_list,
                        personalized_subject, personalized_body, is_html, attachments, custom_headers
                    )
                    
                    # Send with retry
                    result = self._send_message_with_retry(sender_email, message)
                    
                    if result:
                        sent_count += 1
                        self.email_sent.emit(recipient_email, "Sent successfully")
                        self.log_message.emit(f"Sent to {recipient_email} via {sender_email}", QColor("lightgreen"))
                    else:
                        failed_count += 1
                        self.email_failed.emit(recipient_email, "Failed to send")
                        self.log_message.emit(f"Failed to send to {recipient_email}", QColor("red"))
                    
                    # Update progress
                    self.campaign_progress.emit(i + 1, total_recipients)
                    
                    # Rate limiting
                    if rate_limit > 0:
                        time.sleep(60.0 / rate_limit)  # Convert emails/minute to seconds between emails
                    
                    # Batch delay
                    if (i + 1) % concurrency == 0:
                        time.sleep(delay_between_batches)
                        
                except Exception as e:
                    failed_count += 1
                    error_msg = f"Error sending to {recipient}: {self._parse_gsuite_error(e)}"
                    self.email_failed.emit(str(recipient), error_msg)
                    self.log_message.emit(error_msg, QColor("red"))
                    self.campaign_progress.emit(i + 1, total_recipients)
            
            # Campaign finished
            summary = {
                'total': total_recipients,
                'sent': sent_count,
                'failed': failed_count,
                'success_rate': (sent_count / total_recipients * 100) if total_recipients > 0 else 0
            }
            
            self.log_message.emit(f"Campaign completed! Sent: {sent_count}, Failed: {failed_count}", QColor("cyan"))
            self.campaign_finished.emit(summary)
            
        except Exception as e:
            error_msg = f"Bulk email campaign failed: {self._parse_gsuite_error(e)}"
            self.log_message.emit(error_msg, QColor("red"))
            self.error.emit(error_msg)
        finally:
            self.finished.emit()

    @pyqtSlot(list, list, str, str, str, str, dict, list, bool, int, int, int, bool)
    def send_bulk_emails_lightning(self, sender_emails, recipients, subject, body, cc="", bcc="", custom_headers=None, 
                                 attachments=None, is_html=False, concurrency=1, rate_limit=60, delay_between_batches=1, lightning_mode=False):
        """Professional bulk email sending with lightning mode for 1.9k+ emails in <10 seconds"""
        try:
            total_recipients = len(recipients)
            self.campaign_state = "preparing"
            
            if lightning_mode:
                self.log_message.emit(f"⚡ LIGHTNING MODE: Preparing {total_recipients} emails for instant delivery...", QColor("yellow"))
                
                # Prepare all emails in advance (like PowerMTA)
                self.prepared_emails = []
                cc_list = [email.strip() for email in cc.split(',') if email.strip()] if cc else []
                bcc_list = [email.strip() for email in bcc.split(',') if email.strip()] if bcc else []
                
                # Distribute recipients equally among senders
                recipients_per_sender = total_recipients // len(sender_emails)
                remainder = total_recipients % len(sender_emails)
                
                recipient_index = 0
                for sender_index, sender_email in enumerate(sender_emails):
                    # Calculate how many recipients this sender will handle
                    sender_recipient_count = recipients_per_sender + (1 if sender_index < remainder else 0)
                    sender_recipients = recipients[recipient_index:recipient_index + sender_recipient_count]
                    recipient_index += sender_recipient_count
                    
                    # Prepare emails for this sender
                    for recipient in sender_recipients:
                        # Create personalized message
                        personalized_subject = subject
                        personalized_body = body
                        
                        if isinstance(recipient, dict):
                            recipient_email = recipient.get('email', '')
                            recipient_name = recipient.get('name', '')
                            personalized_subject = personalized_subject.replace('{{name}}', recipient_name)
                            personalized_body = personalized_body.replace('{{name}}', recipient_name)
                        else:
                            recipient_email = str(recipient)
                        
                        # Create MIME message
                        message = self._create_mime_message(
                            sender_email, [recipient_email], cc_list, bcc_list,
                            personalized_subject, personalized_body, is_html, attachments, custom_headers
                        )
                        
                        # Store prepared email
                        self.prepared_emails.append({
                            'sender': sender_email,
                            'recipient': recipient_email,
                            'message': message,
                            'status': 'prepared'
                        })
                
                self.campaign_state = "ready"
                self.lightning_mode_ready.emit(len(self.prepared_emails))
                self.log_message.emit(f"⚡ LIGHTNING READY: {len(self.prepared_emails)} emails prepared for instant delivery!", QColor("lightgreen"))
                return
            
            # Regular sending mode
            self._send_bulk_emails_regular(sender_emails, recipients, subject, body, cc, bcc, custom_headers, 
                                       attachments, is_html, concurrency, rate_limit, delay_between_batches)
            
        except Exception as e:
            error_msg = f"Lightning mode preparation failed: {self._parse_gsuite_error(e)}"
            self.log_message.emit(error_msg, QColor("red"))
            self.error.emit(error_msg)
        finally:
            self.finished.emit()

    def _send_bulk_emails_regular(self, sender_emails, recipients, subject, body, cc="", bcc="", custom_headers=None, 
                                attachments=None, is_html=False, concurrency=1, rate_limit=60, delay_between_batches=1):
        """Regular bulk email sending with rate limiting"""
        total_recipients = len(recipients)
        self.log_message.emit(f"Starting regular bulk email campaign to {total_recipients} recipients...", QColor("cyan"))
        
        # Parse recipients and CC/BCC
        cc_list = [email.strip() for email in cc.split(',') if email.strip()] if cc else []
        bcc_list = [email.strip() for email in bcc.split(',') if email.strip()] if bcc else []
        
        # Statistics tracking
        sent_count = 0
        failed_count = 0
        sender_index = 0
        
        # Process recipients in batches
        for i, recipient in enumerate(recipients):
            try:
                # Select sender (round-robin if multiple senders)
                sender_email = sender_emails[sender_index % len(sender_emails)]
                sender_index += 1
                
                # Create personalized message
                personalized_subject = subject
                personalized_body = body
                
                if isinstance(recipient, dict):
                    recipient_email = recipient.get('email', '')
                    recipient_name = recipient.get('name', '')
                    personalized_subject = personalized_subject.replace('{{name}}', recipient_name)
                    personalized_body = personalized_body.replace('{{name}}', recipient_name)
                else:
                    recipient_email = str(recipient)
                
                # Create message
                message = self._create_mime_message(
                    sender_email, [recipient_email], cc_list, bcc_list,
                    personalized_subject, personalized_body, is_html, attachments, custom_headers
                )
                
                # Send with retry
                result = self._send_message_with_retry(sender_email, message)
                
                if result:
                    sent_count += 1
                    self.email_sent.emit(recipient_email, "Sent successfully")
                    self.log_message.emit(f"Sent to {recipient_email} via {sender_email}", QColor("lightgreen"))
                else:
                    failed_count += 1
                    self.email_failed.emit(recipient_email, "Failed to send")
                    self.log_message.emit(f"Failed to send to {recipient_email}", QColor("red"))
                
                # Update progress
                self.campaign_progress.emit(i + 1, total_recipients)
                
                # Rate limiting
                if rate_limit > 0:
                    time.sleep(60.0 / rate_limit)
                
                # Batch delay
                if (i + 1) % concurrency == 0:
                    time.sleep(delay_between_batches)
                    
            except Exception as e:
                failed_count += 1
                error_msg = f"Error sending to {recipient}: {self._parse_gsuite_error(e)}"
                self.email_failed.emit(str(recipient), error_msg)
                self.log_message.emit(error_msg, QColor("red"))
                self.campaign_progress.emit(i + 1, total_recipients)
        
        # Campaign finished
        summary = {
            'total': total_recipients,
            'sent': sent_count,
            'failed': failed_count,
            'success_rate': (sent_count / total_recipients * 100) if total_recipients > 0 else 0
        }
        
        self.log_message.emit(f"Campaign completed! Sent: {sent_count}, Failed: {failed_count}", QColor("cyan"))
        self.campaign_finished.emit(summary)

    @pyqtSlot()
    def pause_campaign(self):
        """Pause the current campaign"""
        if self.campaign_state == "sending":
            self.campaign_state = "paused"
            self.campaign_paused.emit()
            self.log_message.emit("Campaign paused", QColor("yellow"))

    @pyqtSlot()
    def resume_campaign(self):
        """Resume the paused campaign"""
        if self.campaign_state == "paused":
            self.campaign_state = "sending"
            self.campaign_resumed.emit()
            self.log_message.emit("Campaign resumed", QColor("cyan"))

    @pyqtSlot()
    def release_lightning_emails(self):
        """Release all prepared emails instantly (like PowerMTA resume)"""
        if self.campaign_state != "ready":
            self.log_message.emit("No prepared emails to release", QColor("yellow"))
            return
        
        self.campaign_state = "sending"
        self.log_message.emit(f"⚡ RELEASING {len(self.prepared_emails)} emails instantly...", QColor("yellow"))
        
        # Send all prepared emails in parallel
        import threading
        
        def send_prepared_email(email_data):
            try:
                result = self._send_message_with_retry(email_data['sender'], email_data['message'])
                if result:
                    self.email_sent.emit(email_data['recipient'], "Sent successfully")
                    email_data['status'] = 'sent'
                else:
                    self.email_failed.emit(email_data['recipient'], "Failed to send")
                    email_data['status'] = 'failed'
            except Exception as e:
                self.email_failed.emit(email_data['recipient'], str(e))
                email_data['status'] = 'failed'
        
        # Create threads for parallel sending
        threads = []
        for email_data in self.prepared_emails:
            thread = threading.Thread(target=send_prepared_email, args=(email_data,))
            thread.start()
            threads.append(thread)
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Calculate results
        sent_count = sum(1 for email in self.prepared_emails if email['status'] == 'sent')
        failed_count = sum(1 for email in self.prepared_emails if email['status'] == 'failed')
        
        summary = {
            'total': len(self.prepared_emails),
            'sent': sent_count,
            'failed': failed_count,
            'success_rate': (sent_count / len(self.prepared_emails) * 100) if self.prepared_emails else 0
        }
        
        self.log_message.emit(f"⚡ LIGHTNING COMPLETE: {sent_count} sent, {failed_count} failed in seconds!", QColor("lightgreen"))
        self.campaign_finished.emit(summary)

# =============================================================================
# MAIN APPLICATION WINDOW CLASS (PYQT5) - FINAL STABLE VERSION
# =============================================================================
class GUserAdminApp(QMainWindow):
    # --- Signals to trigger tasks on the persistent worker ---
    trigger_connect = pyqtSignal(str, str)
    trigger_fetch_initial_data = pyqtSignal()
    trigger_fetch_dashboard_stats = pyqtSignal()
    trigger_fetch_all_users = pyqtSignal(str)
    trigger_user_action = pyqtSignal(list, str)
    trigger_create_users = pyqtSignal(str, int, str, bool)
    trigger_delete_users = pyqtSignal(str, str, str)
    trigger_change_user_domain = pyqtSignal(str, str)
    trigger_bulk_change_domain = pyqtSignal(str, str, int, str)
    trigger_bulk_change_domain_from_file = pyqtSignal(str, str)
    trigger_count_users = pyqtSignal(str, str)
    trigger_reactivate_users = pyqtSignal(str)
    # Email sending signals
    trigger_send_test_email = pyqtSignal(str, str, str, str, str, str, bool, list, dict)
    trigger_send_bulk_emails = pyqtSignal(list, list, str, str, str, str, dict, list, bool, int, int, int)
    trigger_send_bulk_emails_lightning = pyqtSignal(list, list, str, str, str, str, dict, list, bool, int, int, int, bool)
    trigger_pause_campaign = pyqtSignal()
    trigger_resume_campaign = pyqtSignal()
    trigger_release_lightning = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.service = None
        self.all_users = []
        self.filtered_users = []
        self.current_page_index = 0
        self._setup_ui()
        self._setup_persistent_worker()
        self._connect_signals()
        self._load_settings()
        self._set_controls_enabled_state(False)

    def _setup_ui(self):
        self.setWindowTitle("GUserAdmin Pro - User Management Console (PyQt5)")
        self.setGeometry(100, 100, 1100, 900)
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self.progress_bar = QProgressBar(); self.progress_bar.setVisible(False)
        status_bar.addPermanentWidget(self.progress_bar, 1)
        self.status_label = QLabel("Ready. Please connect to Google Workspace.")
        status_bar.addWidget(self.status_label)
        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)
        self._create_all_tabs()
        log_group, log_layout = QGroupBox("Activity Log"), QVBoxLayout()
        self.log_area = QTextEdit(); self.log_area.setReadOnly(True); self.log_area.setFont(QFont("Courier New", 9))
        self.export_log_btn = QPushButton("Export Log to File"); self.export_log_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        log_layout.addWidget(self.log_area); log_layout.addWidget(self.export_log_btn)
        log_group.setLayout(log_layout)
        self.main_layout.addWidget(log_group, 1)

    def _setup_persistent_worker(self):
        self.thread = QThread()
        self.worker = GWorkspaceWorker()
        self.worker.moveToThread(self.thread)
        self.worker.finished.connect(lambda: self._set_ui_for_task(False))
        self.worker.log_message.connect(self._log)
        self.worker.error.connect(self._log_error)
        self.worker.task_result.connect(lambda title, msg: QMessageBox.information(self, title, msg))
        self.worker.progress_update.connect(lambda cur, tot: self.progress_bar.setValue(int(cur * 100 / tot) if tot > 0 else 0))
        self.worker.dashboard_stats_updated.connect(self._update_dashboard_labels)
        self.worker.all_users_fetched.connect(self._cache_and_display_users)
        self.trigger_connect.connect(self.worker.connect_to_google)
        self.trigger_fetch_initial_data.connect(self.worker.fetch_initial_data)
        self.trigger_fetch_dashboard_stats.connect(self.worker.fetch_dashboard_stats)
        self.trigger_fetch_all_users.connect(self.worker.fetch_all_users)
        self.trigger_user_action.connect(self.worker.perform_user_action)
        self.trigger_create_users.connect(self.worker.create_multiple_users)
        self.trigger_delete_users.connect(self.worker.delete_users)
        self.trigger_change_user_domain.connect(self.worker.change_user_domain)
        self.trigger_bulk_change_domain.connect(self.worker.bulk_change_domain)
        self.trigger_bulk_change_domain_from_file.connect(self.worker.bulk_change_domain_from_file)
        self.trigger_count_users.connect(self.worker.count_users)
        self.trigger_reactivate_users.connect(self.worker.reactivate_suspended_users)
        self.thread.start()

    def _create_all_tabs(self):
        # Tab 1: Dashboard
        tab1, t1_layout = QWidget(), QVBoxLayout()
        conn_group, cg_form = QGroupBox("1. Connection Settings"), QFormLayout()
        self.admin_email_input = QLineEdit(DEFAULT_ADMIN_EMAIL); self.admin_email_input.setToolTip("The admin email address that will be impersonated.")
        self.key_file_label = QLabel("<i>No key file selected</i>")
        self.load_key_btn = QPushButton("Load Service Account Key (.json)"); self.load_key_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.connect_btn = QPushButton("Connect to Google Workspace"); self.connect_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
        cg_form.addRow("Admin Email to Impersonate:", self.admin_email_input); cg_form.addRow("Service Account Key File:", self.key_file_label); cg_form.addRow(self.load_key_btn); cg_form.addRow(self.connect_btn)
        conn_group.setLayout(cg_form); t1_layout.addWidget(conn_group)
        status_group, sg_form = QGroupBox("2. Workspace Status"), QFormLayout()
        self.total_users_label = QLabel("N/A"); self.suspended_users_label = QLabel("N/A")
        self.domain_list_label = QLabel("Not Connected")
        self.refresh_stats_btn = QPushButton("Refresh Stats"); self.refresh_stats_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        sg_form.addRow("Total Users:", self.total_users_label); sg_form.addRow("Suspended Users:", self.suspended_users_label); sg_form.addRow("Connected Domains:", self.domain_list_label); sg_form.addRow(self.refresh_stats_btn)
        status_group.setLayout(sg_form); t1_layout.addWidget(status_group); t1_layout.addStretch()
        tab1.setLayout(t1_layout); self.tabs.addTab(tab1, "Dashboard")
        
        # Tab 2: User Browser
        tab_browser, browser_layout = QWidget(), QVBoxLayout()
        filter_group, filter_layout = QGroupBox("Filters"), QHBoxLayout()
        self.browse_domain_combo = QComboBox(); self.browse_domain_combo.setToolTip("Filter users by domain.")
        self.browse_search_input = QLineEdit(); self.browse_search_input.setPlaceholderText("Search by Email, Name...")
        filter_layout.addWidget(QLabel("Domain:")); filter_layout.addWidget(self.browse_domain_combo); filter_layout.addWidget(QLabel("Search:")); filter_layout.addWidget(self.browse_search_input, 1)
        filter_group.setLayout(filter_layout)
        self.user_table = QTableWidget(); self.user_table.setColumnCount(4); self.user_table.setHorizontalHeaderLabels(["Full Name", "Primary Email", "Status", "Last Login"])
        self.user_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.user_table.setSelectionBehavior(QAbstractItemView.SelectRows); self.user_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.user_table.setContextMenuPolicy(Qt.CustomContextMenu)
        nav_layout = QHBoxLayout()
        self.prev_page_btn = QPushButton("Previous"); self.prev_page_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowLeft))
        self.next_page_btn = QPushButton("Next"); self.next_page_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowRight))
        self.page_label = QLabel("Page 1")
        nav_layout.addWidget(self.prev_page_btn); nav_layout.addStretch(); nav_layout.addWidget(self.page_label); nav_layout.addStretch(); nav_layout.addWidget(self.next_page_btn)
        browser_layout.addWidget(filter_group); browser_layout.addWidget(self.user_table); browser_layout.addLayout(nav_layout)
        tab_browser.setLayout(browser_layout); self.tabs.addTab(tab_browser, "Browse & Manage Users")

        # Other tabs
        tab_create, t3_layout, create_group, cr_form = QWidget(), QVBoxLayout(), QGroupBox("Bulk User Creation"), QFormLayout()
        self.create_domain_combo, self.create_ou_combo = QComboBox(), QComboBox()
        self.create_user_count_spin = QSpinBox(); self.create_user_count_spin.setRange(1, 1000); self.create_user_count_spin.setValue(10)
        self.create_lightning_checkbox = QCheckBox("⚡ Lightning Mode (Fast Parallel Creation)")
        self.create_lightning_checkbox.setToolTip("Enable to use parallel threads for much faster user creation (recommended for >10 users).")
        self.create_users_btn = QPushButton("Create Users"); self.create_users_btn.setIcon(self.style().standardIcon(QStyle.SP_ToolBarHorizontalExtensionButton)); self.create_users_btn.setToolTip("Create multiple users with random names from the data file.")
        cr_form.addRow("Target Domain:", self.create_domain_combo); cr_form.addRow("Organizational Unit:", self.create_ou_combo); cr_form.addRow("Number of Users to Create:", self.create_user_count_spin); cr_form.addRow(self.create_lightning_checkbox); cr_form.addRow(self.create_users_btn)
        create_group.setLayout(cr_form); t3_layout.addWidget(create_group); t3_layout.addStretch()
        tab_create.setLayout(t3_layout); self.tabs.addTab(tab_create, "Bulk Create")
        tab_delete, t4_layout = QWidget(), QVBoxLayout()
        del_domain_group, ddg_form = QGroupBox("Delete All Users in a Domain"), QFormLayout()
        self.delete_domain_combo = QComboBox()
        self.delete_all_btn = QPushButton("Delete All Users from Domain"); self.delete_all_btn.setStyleSheet("background-color: #8B0000; color: white;"); self.delete_all_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon)); self.delete_all_btn.setToolTip("WARNING: Deletes all non-admin users in the selected domain.")
        ddg_form.addRow("Target Domain:", self.delete_domain_combo); ddg_form.addRow(self.delete_all_btn)
        del_domain_group.setLayout(ddg_form); t4_layout.addWidget(del_domain_group)
        del_file_group, dfg_form = QGroupBox("Delete Specific Users from a File"), QFormLayout()
        self.delete_file_path_label = QLabel("<i>No file selected...</i>")
        self.delete_load_file_btn = QPushButton("Select File (.txt) with Emails"); self.delete_load_file_btn.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.delete_from_file_btn = QPushButton("Delete Users from File"); self.delete_from_file_btn.setStyleSheet("background-color: #8B0000; color: white;"); self.delete_from_file_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon)); self.delete_from_file_btn.setToolTip("Deletes all users whose emails are listed in the selected text file.")
        dfg_form.addRow(self.delete_load_file_btn); dfg_form.addRow("File with Emails:", self.delete_file_path_label); dfg_form.addRow(self.delete_from_file_btn)
        del_file_group.setLayout(dfg_form); t4_layout.addWidget(del_file_group); t4_layout.addStretch()
        tab_delete.setLayout(t4_layout); self.tabs.addTab(tab_delete, "Bulk Delete")
        tab_modify, t5_layout = QWidget(), QVBoxLayout()
        single_group, s_form = QGroupBox("Change a Single User's Domain"), QFormLayout()
        self.mod_single_email_input = QLineEdit(); self.mod_single_email_input.setPlaceholderText("user.name@old-domain.com")
        self.mod_single_new_domain_combo = QComboBox()
        self.mod_single_change_btn = QPushButton("Change User's Domain"); self.mod_single_change_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowRight))
        s_form.addRow("Current User Email:", self.mod_single_email_input); s_form.addRow("New Domain:", self.mod_single_new_domain_combo); s_form.addRow(self.mod_single_change_btn)
        single_group.setLayout(s_form); t5_layout.addWidget(single_group)
        bulk_group, b_form = QGroupBox("Bulk Migrate Domain (for all users)"), QFormLayout()
        self.mod_bulk_source_domain, self.mod_bulk_target_domain = QComboBox(), QComboBox()
        self.mod_bulk_limit_spin = QSpinBox(); self.mod_bulk_limit_spin.setRange(0, 50000); self.mod_bulk_limit_spin.setSpecialValueText("Migrate All")
        self.mod_bulk_change_btn = QPushButton("Bulk Migrate Domain"); self.mod_bulk_change_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowRight))
        b_form.addRow("Source Domain:", self.mod_bulk_source_domain); b_form.addRow("Target Domain:", self.mod_bulk_target_domain); b_form.addRow("Limit (0 for all):", self.mod_bulk_limit_spin); b_form.addRow(self.mod_bulk_change_btn)
        bulk_group.setLayout(b_form); t5_layout.addWidget(bulk_group)
        file_group, f_form = QGroupBox("Bulk Migrate Domain (from file)"), QFormLayout()
        self.mod_file_path_label = QLabel("<i>No file selected...</i>")
        self.mod_file_load_btn = QPushButton("Select File with User Info"); self.mod_file_load_btn.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.mod_file_target_domain, self.mod_file_change_btn = QComboBox(), QPushButton("Migrate Users from File"); self.mod_file_change_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowRight))
        f_form.addRow(self.mod_file_load_btn); f_form.addRow("User Info File:", self.mod_file_path_label); f_form.addRow("Target Domain:", self.mod_file_target_domain); f_form.addRow(self.mod_file_change_btn)
        file_group.setLayout(f_form); t5_layout.addWidget(file_group)
        tab_modify.setLayout(t5_layout); self.tabs.addTab(tab_modify, "Bulk Modify")
        tab_report, t6_layout = QWidget(), QVBoxLayout()
        count_group, c_form = QGroupBox("User Counts by Domain"), QFormLayout()
        self.report_domain_combo = QComboBox()
        self.count_total_btn = QPushButton("Count Total Users in Domain"); self.count_total_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogInfoView))
        self.count_suspended_btn = QPushButton("Count Suspended Users in Domain"); self.count_suspended_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogInfoView))
        c_form.addRow("Domain:", self.report_domain_combo); c_form.addRow(self.count_total_btn); c_form.addRow(self.count_suspended_btn)
        count_group.setLayout(c_form); t6_layout.addWidget(count_group)
        action_group, a_form = QGroupBox("Bulk Actions by Domain"), QFormLayout()
        self.action_domain_combo = QComboBox()
        self.reactivate_btn = QPushButton("Reactivate All Suspended Users in Domain"); self.reactivate_btn.setStyleSheet("background-color: #006400; color: white;"); self.reactivate_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogYesButton))
        a_form.addRow("Domain:", self.action_domain_combo); a_form.addRow(self.reactivate_btn)
        action_group.setLayout(a_form); t6_layout.addWidget(action_group); t6_layout.addStretch()
        tab_report.setLayout(t6_layout); self.tabs.addTab(tab_report, "Bulk Reporting & Actions")
        
        # Tab 7: Email Sender
        self._create_email_sender_tab()
    
    def _create_email_sender_tab(self):
        """Create the Email Sender tab with comprehensive email functionality"""
        tab_email = QWidget()
        main_layout = QVBoxLayout(tab_email)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # Create scroll area for the entire tab content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: #2b2b2b;
            }
            QScrollBar:vertical {
                background-color: #3c3f41;
                width: 12px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background-color: #555;
                border-radius: 6px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #666;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        
        # Create main content widget
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(10, 10, 10, 10)
        content_layout.setSpacing(15)
        
        # Create main splitter with modern styling
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(12)
        main_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #555;
                border: 1px solid #777;
                border-radius: 2px;
            }
            QSplitter::handle:hover {
                background-color: #666;
            }
        """)
        
        # Left panel - Email Composer
        left_panel = QWidget()
        left_panel.setMinimumWidth(450)
        left_panel.setMaximumWidth(900)
        left_panel.setStyleSheet("""
            QWidget {
                background-color: #3c3f41;
                border-radius: 5px;
            }
        """)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        
        # Sender Management Group
        sender_group = QGroupBox("📧 Sender Management")
        sender_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 8px;
                padding-top: 10px;
                background-color: #404040;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 6px;
                left: 8px;
                background-color: #3c3f41;
                border-radius: 3px;
            }
        """)
        sender_layout = QFormLayout()
        
        # Sender selection (multi-select from workspace users)
        self.sender_list = QListWidget()
        self.sender_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.sender_list.setMaximumHeight(100)
        self.refresh_senders_btn = QPushButton("Refresh Senders")
        self.refresh_senders_btn.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        
        sender_layout.addRow("Select Sender(s):", self.sender_list)
        sender_layout.addRow(self.refresh_senders_btn)
        sender_group.setLayout(sender_layout)
        left_layout.addWidget(sender_group)
        
        # Test Email Group
        test_group = QGroupBox("Test Email")
        test_layout = QFormLayout()
        
        self.test_recipient_input = QLineEdit()
        self.test_recipient_input.setPlaceholderText("test@example.com")
        self.send_test_btn = QPushButton("Send Test Email")
        self.send_test_btn.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        
        test_layout.addRow("Test Recipient:", self.test_recipient_input)
        test_layout.addRow(self.send_test_btn)
        test_group.setLayout(test_layout)
        left_layout.addWidget(test_group)
        
        # Email Composer Group
        composer_group = QGroupBox("✉️ Email Composer")
        composer_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 8px;
                padding-top: 10px;
                background-color: #404040;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 6px;
                left: 8px;
                background-color: #3c3f41;
                border-radius: 3px;
            }
        """)
        composer_layout = QFormLayout()
        
        # From field (auto-filled)
        self.from_input = QLineEdit()
        self.from_input.setReadOnly(True)
        self.from_input.setPlaceholderText("Select sender(s) first")
        
        # Recipients
        self.to_input = QTextEdit()
        self.to_input.setMaximumHeight(60)
        self.to_input.setPlaceholderText("Enter recipient emails (one per line or comma-separated)")
        
        self.cc_input = QLineEdit()
        self.cc_input.setPlaceholderText("CC recipients (comma-separated)")
        
        self.bcc_input = QLineEdit()
        self.bcc_input.setPlaceholderText("BCC recipients (comma-separated)")
        
        # Subject
        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Email subject")
        
        # Body with HTML/Plain text toggle
        body_layout = QVBoxLayout()
        body_toggle_layout = QHBoxLayout()
        self.html_toggle = QCheckBox("HTML Mode")
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        body_toggle_layout.addWidget(self.html_toggle)
        body_toggle_layout.addWidget(self.preview_btn)
        body_toggle_layout.addStretch()
        
        self.body_editor = QTextEdit()
        self.body_editor.setMinimumHeight(150)
        self.body_editor.setMaximumHeight(300)
        self.body_editor.setPlaceholderText("Enter your email content here...")
        
        body_layout.addLayout(body_toggle_layout)
        body_layout.addWidget(self.body_editor)
        
        composer_layout.addRow("From:", self.from_input)
        composer_layout.addRow("To:", self.to_input)
        composer_layout.addRow("CC:", self.cc_input)
        composer_layout.addRow("BCC:", self.bcc_input)
        composer_layout.addRow("Subject:", self.subject_input)
        composer_layout.addRow("Body:", body_layout)
        
        composer_group.setLayout(composer_layout)
        left_layout.addWidget(composer_group)
        
        # Custom Headers Group
        headers_group = QGroupBox("Custom Headers")
        headers_layout = QVBoxLayout()
        
        self.headers_table = QTableWidget()
        self.headers_table.setColumnCount(2)
        self.headers_table.setHorizontalHeaderLabels(["Header", "Value"])
        self.headers_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.headers_table.setMaximumHeight(100)
        
        headers_btn_layout = QHBoxLayout()
        self.add_header_btn = QPushButton("Add Header")
        self.remove_header_btn = QPushButton("Remove Header")
        headers_btn_layout.addWidget(self.add_header_btn)
        headers_btn_layout.addWidget(self.remove_header_btn)
        headers_btn_layout.addStretch()
        
        headers_layout.addWidget(self.headers_table)
        headers_layout.addLayout(headers_btn_layout)
        headers_group.setLayout(headers_layout)
        left_layout.addWidget(headers_group)
        
        # Attachments Group
        attachments_group = QGroupBox("Attachments")
        attachments_layout = QVBoxLayout()
        
        self.attachments_list = QListWidget()
        self.attachments_list.setMaximumHeight(80)
        
        attachments_btn_layout = QHBoxLayout()
        self.add_attachment_btn = QPushButton("Add Attachment")
        self.add_attachment_btn.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.remove_attachment_btn = QPushButton("Remove")
        self.remove_attachment_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        attachments_btn_layout.addWidget(self.add_attachment_btn)
        attachments_btn_layout.addWidget(self.remove_attachment_btn)
        attachments_btn_layout.addStretch()
        
        attachments_layout.addWidget(self.attachments_list)
        attachments_layout.addLayout(attachments_btn_layout)
        attachments_group.setLayout(attachments_layout)
        left_layout.addWidget(attachments_group)
        
        # Campaign Controls Group
        campaign_group = QGroupBox("🚀 Campaign Controls")
        campaign_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 8px;
                padding-top: 10px;
                background-color: #404040;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 6px;
                left: 8px;
                background-color: #3c3f41;
                border-radius: 3px;
            }
        """)
        campaign_layout = QFormLayout()
        
        # Concurrency and rate limiting
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 20)
        self.concurrency_spin.setValue(1)
        self.concurrency_spin.setToolTip("Number of concurrent email sending threads")
        
        self.rate_limit_spin = QSpinBox()
        self.rate_limit_spin.setRange(1, 1000)
        self.rate_limit_spin.setValue(60)
        self.rate_limit_spin.setToolTip("Emails per minute")
        
        # Lightning mode toggle
        self.lightning_mode_checkbox = QCheckBox("⚡ Lightning Mode (1.9k+ emails in <10s)")
        self.lightning_mode_checkbox.setStyleSheet("color: #FFD700; font-weight: bold;")
        self.lightning_mode_checkbox.setToolTip("Professional mode for high-volume sending like PowerMTA")
        
        # Campaign buttons
        campaign_btn_layout = QHBoxLayout()
        self.start_campaign_btn = QPushButton("Start Campaign")
        self.start_campaign_btn.setStyleSheet("background-color: #006400; color: white;")
        self.start_campaign_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        
        self.pause_campaign_btn = QPushButton("Pause")
        self.pause_campaign_btn.setStyleSheet("background-color: #FF8C00; color: white;")
        self.pause_campaign_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        self.pause_campaign_btn.setEnabled(False)
        
        self.resume_campaign_btn = QPushButton("Resume")
        self.resume_campaign_btn.setStyleSheet("background-color: #32CD32; color: white;")
        self.resume_campaign_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.resume_campaign_btn.setEnabled(False)
        
        self.release_lightning_btn = QPushButton("⚡ Release Lightning")
        self.release_lightning_btn.setStyleSheet("background-color: #FFD700; color: black; font-weight: bold;")
        self.release_lightning_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.release_lightning_btn.setEnabled(False)
        
        self.cancel_campaign_btn = QPushButton("Cancel")
        self.cancel_campaign_btn.setStyleSheet("background-color: #8B0000; color: white;")
        self.cancel_campaign_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogCancelButton))
        self.cancel_campaign_btn.setEnabled(False)
        
        campaign_btn_layout.addWidget(self.start_campaign_btn)
        campaign_btn_layout.addWidget(self.pause_campaign_btn)
        campaign_btn_layout.addWidget(self.resume_campaign_btn)
        campaign_btn_layout.addWidget(self.release_lightning_btn)
        campaign_btn_layout.addWidget(self.cancel_campaign_btn)
        
        campaign_layout.addRow("Concurrency:", self.concurrency_spin)
        campaign_layout.addRow("Rate Limit (emails/min):", self.rate_limit_spin)
        campaign_layout.addRow("Lightning Mode:", self.lightning_mode_checkbox)
        campaign_layout.addRow("Controls:", campaign_btn_layout)
        
        campaign_group.setLayout(campaign_layout)
        left_layout.addWidget(campaign_group)
        
        # Right panel - Recipients and Logs
        right_panel = QWidget()
        right_panel.setMinimumWidth(400)
        right_panel.setMaximumWidth(700)
        right_panel.setStyleSheet("""
            QWidget {
                background-color: #3c3f41;
                border-radius: 5px;
            }
        """)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(10)
        
        # Recipient Management Group
        recipients_group = QGroupBox("👥 Recipient Management")
        recipients_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 8px;
                padding-top: 10px;
                background-color: #404040;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 6px;
                left: 8px;
                background-color: #3c3f41;
                border-radius: 3px;
            }
        """)
        recipients_layout = QVBoxLayout()
        
        # Manual recipient input
        manual_input_group = QGroupBox("Add Recipients Manually")
        manual_input_layout = QVBoxLayout()
        
        # Text input for manual recipients
        self.manual_recipients_input = QTextEdit()
        self.manual_recipients_input.setMaximumHeight(100)
        self.manual_recipients_input.setPlaceholderText("Enter recipients manually (one per line or comma-separated):\nExample:\njohn@example.com\njane@example.com, bob@example.com")
        
        # Add manual recipients button
        self.add_manual_recipients_btn = QPushButton("Add Recipients")
        self.add_manual_recipients_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
        
        manual_input_layout.addWidget(QLabel("Manual Recipients:"))
        manual_input_layout.addWidget(self.manual_recipients_input)
        manual_input_layout.addWidget(self.add_manual_recipients_btn)
        manual_input_group.setLayout(manual_input_layout)
        
        # Import recipients
        import_layout = QHBoxLayout()
        self.import_csv_btn = QPushButton("Import CSV")
        self.import_csv_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.clear_recipients_btn = QPushButton("Clear All")
        self.clear_recipients_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        import_layout.addWidget(self.import_csv_btn)
        import_layout.addWidget(self.clear_recipients_btn)
        import_layout.addStretch()
        
        # Recipients table
        self.recipients_table = QTableWidget()
        self.recipients_table.setColumnCount(4)
        self.recipients_table.setHorizontalHeaderLabels(["Email", "Name", "Status", "Error"])
        self.recipients_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.recipients_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.recipients_table.setMinimumHeight(200)
        self.recipients_table.setMaximumHeight(400)
        
        recipients_layout.addWidget(manual_input_group)
        recipients_layout.addLayout(import_layout)
        recipients_layout.addWidget(self.recipients_table)
        recipients_group.setLayout(recipients_layout)
        right_layout.addWidget(recipients_group)
        
        # Campaign Progress Group
        progress_group = QGroupBox("📊 Campaign Progress")
        progress_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                color: #f0f0f0;
                border: 1px solid #555;
                border-radius: 5px;
                margin-top: 8px;
                padding-top: 10px;
                background-color: #404040;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 6px;
                left: 8px;
                background-color: #3c3f41;
                border-radius: 3px;
            }
        """)
        progress_layout = QVBoxLayout()
        
        self.campaign_progress_bar = QProgressBar()
        self.campaign_progress_label = QLabel("Ready to start campaign")
        
        # Campaign stats
        stats_layout = QHBoxLayout()
        self.sent_count_label = QLabel("Sent: 0")
        self.failed_count_label = QLabel("Failed: 0")
        self.success_rate_label = QLabel("Success Rate: 0%")
        stats_layout.addWidget(self.sent_count_label)
        stats_layout.addWidget(self.failed_count_label)
        stats_layout.addWidget(self.success_rate_label)
        stats_layout.addStretch()
        
        progress_layout.addWidget(self.campaign_progress_label)
        progress_layout.addWidget(self.campaign_progress_bar)
        progress_layout.addLayout(stats_layout)
        progress_group.setLayout(progress_layout)
        right_layout.addWidget(progress_group)
        
        # Export logs
        export_group = QGroupBox("Export Campaign Data")
        export_layout = QHBoxLayout()
        
        self.export_logs_btn = QPushButton("Export Logs")
        self.export_logs_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.export_recipients_btn = QPushButton("Export Recipients")
        self.export_recipients_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        
        export_layout.addWidget(self.export_logs_btn)
        export_layout.addWidget(self.export_recipients_btn)
        export_layout.addStretch()
        
        export_group.setLayout(export_layout)
        right_layout.addWidget(export_group)
        
        # Add panels to splitter
        main_splitter.addWidget(left_panel)
        main_splitter.addWidget(right_panel)
        main_splitter.setSizes([600, 500])  # Set initial sizes
        
        # Add splitter to content layout
        content_layout.addWidget(main_splitter)
        
        # Set content widget to scroll area
        scroll_area.setWidget(content_widget)
        
        # Add scroll area to main layout
        main_layout.addWidget(scroll_area)
        
        # Add tab to main tabs widget
        self.tabs.addTab(tab_email, "Email Sender")
    
    def _connect_signals(self):
        self.load_key_btn.clicked.connect(self._load_key_file)
        self.connect_btn.clicked.connect(self._connect_to_google)
        self.export_log_btn.clicked.connect(self._export_log)
        self.refresh_stats_btn.clicked.connect(self._refresh_dashboard_stats)
        self.browse_domain_combo.currentIndexChanged.connect(self._fetch_all_users_for_browser)
        self.browse_search_input.textChanged.connect(self._filter_and_display_users)
        self.next_page_btn.clicked.connect(self._go_to_next_page)
        self.prev_page_btn.clicked.connect(self._go_to_prev_page)
        self.user_table.customContextMenuRequested.connect(self._show_user_context_menu)
        self.create_users_btn.clicked.connect(self._create_users)
        self.delete_all_btn.clicked.connect(self._delete_all_users_in_domain)
        self.delete_load_file_btn.clicked.connect(self._load_deletion_file)
        self.delete_from_file_btn.clicked.connect(self._delete_users_from_file)
        self.mod_single_change_btn.clicked.connect(self._change_single_user_domain)
        self.mod_bulk_change_btn.clicked.connect(self._bulk_change_domain)
        self.mod_file_load_btn.clicked.connect(self._load_modification_file)
        self.mod_file_change_btn.clicked.connect(self._bulk_change_domain_from_file)
        self.count_total_btn.clicked.connect(lambda: self._count_users('total'))
        self.count_suspended_btn.clicked.connect(lambda: self._count_users('suspended'))
        self.reactivate_btn.clicked.connect(self._reactivate_users)
        self.worker.connection_successful.connect(self._on_connection_successful)
        self.worker.domains_fetched.connect(self._update_domain_lists)
        self.worker.ous_fetched.connect(self._update_ou_lists)
        self.worker.all_users_fetched.connect(self._cache_and_display_users)
        
        # Email functionality signals
        self.refresh_senders_btn.clicked.connect(self._refresh_senders)
        self.send_test_btn.clicked.connect(self._send_test_email)
        self.start_campaign_btn.clicked.connect(self._start_campaign)
        self.pause_campaign_btn.clicked.connect(self._pause_campaign)
        self.resume_campaign_btn.clicked.connect(self._resume_campaign)
        self.release_lightning_btn.clicked.connect(self._release_lightning)
        self.cancel_campaign_btn.clicked.connect(self._cancel_campaign)
        self.add_manual_recipients_btn.clicked.connect(self._add_manual_recipients)
        self.import_csv_btn.clicked.connect(self._import_recipients_csv)
        self.clear_recipients_btn.clicked.connect(self._clear_recipients)
        self.add_header_btn.clicked.connect(self._add_custom_header)
        self.remove_header_btn.clicked.connect(self._remove_custom_header)
        self.add_attachment_btn.clicked.connect(self._add_attachment)
        self.remove_attachment_btn.clicked.connect(self._remove_attachment)
        self.export_logs_btn.clicked.connect(self._export_campaign_logs)
        self.export_recipients_btn.clicked.connect(self._export_recipients)
        self.preview_btn.clicked.connect(self._preview_email)
        self.sender_list.itemSelectionChanged.connect(self._update_from_field)
        
        # Connect worker email signals
        self.trigger_send_test_email.connect(self.worker.send_test_email)
        self.trigger_send_bulk_emails.connect(self.worker.send_bulk_emails)
        self.trigger_send_bulk_emails_lightning.connect(self.worker.send_bulk_emails_lightning)
        self.trigger_pause_campaign.connect(self.worker.pause_campaign)
        self.trigger_resume_campaign.connect(self.worker.resume_campaign)
        self.trigger_release_lightning.connect(self.worker.release_lightning_emails)
        self.worker.email_sent.connect(self._on_email_sent)
        self.worker.email_failed.connect(self._on_email_failed)
        self.worker.campaign_progress.connect(self._on_campaign_progress)
        self.worker.campaign_finished.connect(self._on_campaign_finished)
        self.worker.campaign_paused.connect(self._on_campaign_paused)
        self.worker.campaign_resumed.connect(self._on_campaign_resumed)
        self.worker.lightning_mode_ready.connect(self._on_lightning_ready)

    def _set_ui_for_task(self, is_starting):
        self.central_widget.setEnabled(not is_starting)
        self.progress_bar.setVisible(is_starting)
        if is_starting: self.progress_bar.setRange(0,100); self.progress_bar.setValue(0)

    def _set_controls_enabled_state(self, enabled):
        self.refresh_stats_btn.setEnabled(enabled)
        for i in range(1, self.tabs.count()): self.tabs.setTabEnabled(i, enabled)

    def _log(self, message, color):
        self.log_area.moveCursor(QTextCursor.End); self.log_area.setTextColor(color)
        self.log_area.insertPlainText(f"[{time.strftime('%H:%M:%S')}] {message}\n"); self.log_area.moveCursor(QTextCursor.End)
        self.status_label.setText(message.split('\n')[0])

    def _log_error(self, message):
        self._log(message, QColor("red")); QMessageBox.critical(self, "Error", message)

    def _load_key_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Service Account Key", "", "JSON Files (*.json)");
        if path: self.key_file_path = path; self.key_file_label.setText(f"<b>{os.path.basename(path)}</b>"); self._log(f"Key loaded: {path}", QColor("cyan"))
    
    def _connect_to_google(self):
        email = self.admin_email_input.text().strip()
        if not hasattr(self, 'key_file_path') or not self.key_file_path or not email: return self._log_error("Please select a key file and enter an admin email.")
        self._set_ui_for_task(True)
        self._save_settings()
        self.trigger_connect.emit(self.key_file_path, email)
    
    def _on_connection_successful(self, service_object):
        self.worker.service = service_object
        self._set_controls_enabled_state(True);
        self.trigger_fetch_initial_data.emit()
        self.trigger_fetch_dashboard_stats.emit()

    def _update_dashboard_labels(self, stats):
        self.total_users_label.setText(f"<b>{stats.get('total_users', 'N/A')}</b>")
        self.suspended_users_label.setText(f"<b>{stats.get('suspended_users', 'N/A')}</b>")

    def _update_domain_lists(self, domains):
        for combo in self.findChildren(QComboBox):
            if combo is self.create_ou_combo: continue
            current_text = combo.currentText(); combo.blockSignals(True)
            combo.clear(); combo.addItems(domains)
            if current_text in domains: combo.setCurrentText(current_text)
            combo.blockSignals(False)
        self.domain_list_label.setText(", ".join(domains))
        if domains: self._fetch_all_users_for_browser()

    def _update_ou_lists(self, ous):
        self.create_ou_combo.blockSignals(True)
        self.create_ou_combo.clear()
        for ou in ous: self.create_ou_combo.addItem(ou["name"], ou["value"])
        self.create_ou_combo.blockSignals(False)

    def _fetch_all_users_for_browser(self):
        domain = self.browse_domain_combo.currentText()
        if not domain: return
        self._set_ui_for_task(True)
        self.status_label.setText(f"Fetching all users from {domain}...")
        self.progress_bar.setRange(0, 0)
        self.trigger_fetch_all_users.emit(domain)

    def _cache_and_display_users(self, user_list):
        self.all_users = user_list
        self._log(f"Cached {len(self.all_users)} users for browsing.", QColor("cyan"))
        self._filter_and_display_users()

    def _filter_and_display_users(self):
        search_term = self.browse_search_input.text().strip().lower()
        if not self.all_users: return
        if not search_term:
            self.filtered_users = self.all_users
        else:
            self.filtered_users = [user for user in self.all_users if search_term in user.get('name', {}).get('fullName', '').lower() or search_term in user.get('primaryEmail', '').lower()]
        self.current_page_index = 0
        self._populate_user_table()

    def _populate_user_table(self):
        self.user_table.setRowCount(0)
        page_size = 100
        start_index = self.current_page_index * page_size
        end_index = start_index + page_size
        users_to_display = self.filtered_users[start_index:end_index]
        for user in users_to_display:
            row = self.user_table.rowCount()
            self.user_table.insertRow(row)
            self.user_table.setItem(row, 0, QTableWidgetItem(user.get('name', {}).get('fullName', 'N/A')))
            self.user_table.setItem(row, 1, QTableWidgetItem(user.get('primaryEmail', '')))
            status_item = QTableWidgetItem("Suspended" if user.get('suspended') else "Active")
            status_item.setForeground(QColor("orange") if user.get('suspended') else QColor("lightgreen"))
            self.user_table.setItem(row, 2, status_item)
            last_login_str = user.get('lastLoginTime', 'Never')
            if 'T' in last_login_str:
                try:
                    last_login_dt = datetime.fromisoformat(last_login_str.replace('Z', '+00:00'))
                    last_login_str = last_login_dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError: last_login_str = "Invalid Date"
            self.user_table.setItem(row, 3, QTableWidgetItem(last_login_str))
        total_pages = (len(self.filtered_users) + page_size - 1) // page_size
        self.page_label.setText(f"Page {self.current_page_index + 1} of {max(1, total_pages)}")
        self.prev_page_btn.setEnabled(self.current_page_index > 0)
        self.next_page_btn.setEnabled(end_index < len(self.filtered_users))
        self.progress_bar.setRange(0, 100)
        self._set_ui_for_task(False)

    def _go_to_next_page(self):
        self.current_page_index += 1; self._populate_user_table()

    def _go_to_prev_page(self):
        if self.current_page_index > 0: self.current_page_index -= 1; self._populate_user_table()

    def _show_user_context_menu(self, pos):
        selected_items = self.user_table.selectedItems()
        if not selected_items: return
        menu = QMenu()
        copy_action = menu.addAction(self.style().standardIcon(QStyle.SP_FileDialogContentsView), "Copy Email(s) to Clipboard")
        menu.addSeparator()
        suspend_action = menu.addAction(self.style().standardIcon(QStyle.SP_DialogCancelButton), "Suspend Selected User(s)")
        reactivate_action = menu.addAction(self.style().standardIcon(QStyle.SP_DialogApplyButton), "Reactivate Selected User(s)")
        force_pw_reset_action = menu.addAction(self.style().standardIcon(QStyle.SP_BrowserReload), "Force Password Reset for User(s)")
        menu.addSeparator()
        delete_action = menu.addAction(self.style().standardIcon(QStyle.SP_TrashIcon), "DELETE Selected User(s)...")
        action = menu.exec_(self.user_table.mapToGlobal(pos))
        if not action: return
        selected_rows = sorted(list(set(item.row() for item in selected_items)))
        selected_emails = [self.user_table.item(row, 1).text() for row in selected_rows]
        if action == copy_action:
            QApplication.clipboard().setText("\n".join(selected_emails))
            self._log(f"Copied {len(selected_emails)} email(s) to clipboard.", QColor("cyan"))
        elif action == suspend_action:
            self._generic_task_runner("Confirm Suspension", f"Suspend {len(selected_emails)} user(s)?", self.trigger_user_action, selected_emails, "suspend")
        elif action == reactivate_action:
            self._generic_task_runner("Confirm Reactivation", f"Reactivate {len(selected_emails)} user(s)?", self.trigger_user_action, selected_emails, "reactivate")
        elif action == force_pw_reset_action:
            self._generic_task_runner("Confirm Password Reset", f"Force password reset for {len(selected_emails)} user(s)?", self.trigger_user_action, selected_emails, "force_password_reset")
        elif action == delete_action:
            self._generic_task_runner("DANGER", f"PERMANENTLY DELETE {len(selected_emails)} selected user(s)?\nThis cannot be undone.", self.trigger_user_action, selected_emails, "delete")
    
    def _generic_task_runner(self, confirm_title, confirm_msg, trigger_signal, *args):
        reply = QMessageBox.question(self, confirm_title, confirm_msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self._set_ui_for_task(True)
            trigger_signal.emit(*args)

    def _create_users(self):
        args = (self.create_domain_combo.currentText(), self.create_user_count_spin.value(), self.create_ou_combo.currentData(), self.create_lightning_checkbox.isChecked())
        if not args[0] or not args[2]: return self._log_error("Please wait for Domains and OUs to load.")
        mode_str = " (LIGHTNING MODE)" if args[3] else ""
        self._generic_task_runner("Confirm Creation", f"Are you sure you want to create {args[1]} users{mode_str}?", self.trigger_create_users, *args)

    def _delete_all_users_in_domain(self):
        domain = self.delete_domain_combo.currentText()
        if not domain: return self._log_error("Please select a domain.")
        args = (domain, None, self.admin_email_input.text().strip())
        self._generic_task_runner("DANGER", f"This will PERMANENTLY delete ALL non-admin users in '{domain}'.\nThis action cannot be undone. Proceed?", self.trigger_delete_users, *args)

    def _load_deletion_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select File with Emails", "", "Text Files (*.txt)");
        if path: self.delete_file_path = path; self.delete_file_path_label.setText(f"<b>{os.path.basename(path)}</b>")

    def _delete_users_from_file(self):
        if not hasattr(self, 'delete_file_path'): return self._log_error("Please select a file first.")
        args = (None, self.delete_file_path, self.admin_email_input.text().strip())
        self._generic_task_runner("Confirm Deletion", f"Delete all users listed in '{os.path.basename(self.delete_file_path)}'?", self.trigger_delete_users, *args)

    def _change_single_user_domain(self):
        email, new_domain = self.mod_single_email_input.text().strip(), self.mod_single_new_domain_combo.currentText()
        if not email or '@' not in email: return self._log_error("Invalid source email.")
        if not new_domain: return self._log_error("Please select a new domain.")
        self._generic_task_runner("Confirm Change", f"Change domain for {email} to {new_domain}?", self.trigger_change_user_domain, email, new_domain)

    def _bulk_change_domain(self):
        source, target, limit = self.mod_bulk_source_domain.currentText(), self.mod_bulk_target_domain.currentText(), self.mod_bulk_limit_spin.value()
        if not source or not target: return self._log_error("Please select source and target domains.")
        if source == target: return self._log_error("Source and Target domains cannot be the same.")
        args = (source, target, limit, self.admin_email_input.text().strip())
        self._generic_task_runner("Confirm Bulk Migration", f"Migrate {'ALL' if limit == 0 else limit} users from {source} to {target}?", self.trigger_bulk_change_domain, *args)

    def _load_modification_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select File with User Info", "", "Text Files (*.txt)")
        if path: self.mod_file_path = path; self.mod_file_path_label.setText(f"<b>{os.path.basename(path)}</b>")

    def _bulk_change_domain_from_file(self):
        if not hasattr(self, 'mod_file_path'): return self._log_error("Please select a user info file.")
        target_domain = self.mod_file_target_domain.currentText()
        if not target_domain: return self._log_error("Please select a target domain.")
        args = (self.mod_file_path, target_domain)
        self._generic_task_runner("Confirm File Migration", f"Migrate users in file to {args[1]}?", self.trigger_bulk_change_domain_from_file, *args)

    def _count_users(self, count_type):
        domain = self.report_domain_combo.currentText()
        if not domain: return self._log_error("Please select a domain.")
        self._set_ui_for_task(True)
        self.trigger_count_users.emit(domain, count_type)

    def _reactivate_users(self):
        domain = self.action_domain_combo.currentText()
        if not domain: return self._log_error("Please select a domain.")
        self._generic_task_runner("Confirm Reactivation", f"Reactivate all suspended users in '{domain}'?", self.trigger_reactivate_users, domain)

    def _refresh_dashboard_stats(self):
        self._set_ui_for_task(True)
        self.trigger_fetch_dashboard_stats.emit()
        
    def _export_log(self):
        log_content = self.log_area.toPlainText()
        if not log_content: return QMessageBox.information(self, "Export Log", "Log is empty. Nothing to export.")
        path, _ = QFileDialog.getSaveFileName(self, "Save Log File", "GUserAdmin_Log.txt", "Text Files (*.txt);;All Files (*)")
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f: f.write(log_content)
                self._log(f"Log successfully exported to {path}", QColor("lightgreen"))
            except Exception as e: self._log_error(f"Failed to export log: {e}")

    def _load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f: settings = json.load(f)
                self.admin_email_input.setText(settings.get('admin_email', ''))
                key_path = settings.get('key_file_path', '')
                if key_path and os.path.exists(key_path):
                    self.key_file_path = key_path
                    self.key_file_label.setText(f"<b>{os.path.basename(key_path)}</b>")
                self._log("Loaded saved settings.", QColor("cyan"))
            except Exception as e: self._log_error(f"Could not load settings file: {e}")

    def _save_settings(self):
        settings = {'admin_email': self.admin_email_input.text(), 'key_file_path': getattr(self, 'key_file_path', '')}
        try:
            with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f, indent=4)
        except Exception as e: self._log_error(f"Could not save settings: {e}")

    # --- Email Functionality Methods ---
    def _refresh_senders(self):
        """Refresh the list of available senders from workspace users"""
        if not hasattr(self, 'all_users') or not self.all_users:
            self._log_error("Please fetch users first from the Browse & Manage Users tab")
            return
        
        self.sender_list.clear()
        for user in self.all_users:
            email = user.get('primaryEmail', '')
            name = user.get('name', {}).get('fullName', '')
            if email:
                item_text = f"{name} ({email})" if name else email
                item = QListWidgetItem(item_text)
                item.setData(Qt.UserRole, email)
                self.sender_list.addItem(item)
        
        self._log(f"Loaded {self.sender_list.count()} potential senders", QColor("cyan"))

    def _update_from_field(self):
        """Update the From field based on selected senders"""
        selected_items = self.sender_list.selectedItems()
        if selected_items:
            emails = [item.data(Qt.UserRole) for item in selected_items]
            self.from_input.setText(", ".join(emails))
        else:
            self.from_input.setText("")

    def _send_test_email(self):
        """Send a test email to verify the setup"""
        # Get selected senders
        selected_items = self.sender_list.selectedItems()
        if not selected_items:
            self._log_error("Please select at least one sender")
            return
        
        test_recipient = self.test_recipient_input.text().strip()
        if not test_recipient:
            self._log_error("Please enter a test recipient email")
            return
        
        # Get email content
        subject = self.subject_input.text().strip()
        body = self.body_editor.toPlainText().strip()
        
        if not subject or not body:
            self._log_error("Please enter both subject and body")
            return
        
        # Get additional parameters
        cc = self.cc_input.text().strip()
        bcc = self.bcc_input.text().strip()
        is_html = self.html_toggle.isChecked()
        
        # Get attachments
        attachments = []
        for i in range(self.attachments_list.count()):
            attachments.append(self.attachments_list.item(i).text())
        
        # Get custom headers
        custom_headers = {}
        for row in range(self.headers_table.rowCount()):
            header = self.headers_table.item(row, 0)
            value = self.headers_table.item(row, 1)
            if header and value and header.text().strip() and value.text().strip():
                custom_headers[header.text().strip()] = value.text().strip()
        
        # Send test emails from all selected senders
        self._set_ui_for_task(True)
        sender_emails = [item.data(Qt.UserRole) for item in selected_items]
        self._log(f"Sending test emails from {len(sender_emails)} sender(s) to {test_recipient}", QColor("cyan"))
        
        # Send test email from each selected sender
        for i, sender_email in enumerate(sender_emails):
            self.trigger_send_test_email.emit(
                sender_email, test_recipient, subject, body, cc, bcc, is_html, attachments, custom_headers
            )

    def _start_campaign(self):
        """Start the bulk email campaign"""
        # Get selected senders
        selected_items = self.sender_list.selectedItems()
        if not selected_items:
            self._log_error("Please select at least one sender")
            return
        
        sender_emails = [item.data(Qt.UserRole) for item in selected_items]
        
        # Get recipients
        recipients = self._get_recipients_from_table()
        if not recipients:
            self._log_error("Please add recipients first")
            return
        
        # Get email content
        subject = self.subject_input.text().strip()
        body = self.body_editor.toPlainText().strip()
        
        if not subject or not body:
            self._log_error("Please enter both subject and body")
            return
        
        # Get additional parameters
        cc = self.cc_input.text().strip()
        bcc = self.bcc_input.text().strip()
        is_html = self.html_toggle.isChecked()
        
        # Get attachments
        attachments = []
        for i in range(self.attachments_list.count()):
            attachments.append(self.attachments_list.item(i).text())
        
        # Get custom headers
        custom_headers = {}
        for row in range(self.headers_table.rowCount()):
            header = self.headers_table.item(row, 0)
            value = self.headers_table.item(row, 1)
            if header and value and header.text().strip() and value.text().strip():
                custom_headers[header.text().strip()] = value.text().strip()
        
        # Get campaign settings
        concurrency = self.concurrency_spin.value()
        rate_limit = self.rate_limit_spin.value()
        
        # Update UI for campaign
        self.start_campaign_btn.setEnabled(False)
        self.pause_campaign_btn.setEnabled(True)
        self.cancel_campaign_btn.setEnabled(True)
        self.campaign_progress_bar.setRange(0, len(recipients))
        self.campaign_progress_bar.setValue(0)
        self.campaign_progress_label.setText(f"Starting campaign to {len(recipients)} recipients...")
        
        # Reset stats
        self.sent_count_label.setText("Sent: 0")
        self.failed_count_label.setText("Failed: 0")
        self.success_rate_label.setText("Success Rate: 0%")
        
        # Check if lightning mode is enabled
        lightning_mode = self.lightning_mode_checkbox.isChecked()
        
        # Start campaign
        self._set_ui_for_task(True)
        if lightning_mode:
            self.trigger_send_bulk_emails_lightning.emit(
                sender_emails, recipients, subject, body, cc, bcc, custom_headers, 
                attachments, is_html, concurrency, rate_limit, 1, lightning_mode
            )
        else:
            self.trigger_send_bulk_emails.emit(
                sender_emails, recipients, subject, body, cc, bcc, custom_headers, 
                attachments, is_html, concurrency, rate_limit, 1
            )

    def _pause_campaign(self):
        """Pause the current campaign"""
        self.trigger_pause_campaign.emit()
        self._log("Campaign pause requested", QColor("yellow"))

    def _resume_campaign(self):
        """Resume the paused campaign"""
        self.trigger_resume_campaign.emit()
        self._log("Campaign resume requested", QColor("cyan"))

    def _release_lightning(self):
        """Release lightning emails instantly"""
        self.trigger_release_lightning.emit()
        self._log("⚡ Lightning release requested", QColor("yellow"))

    def _cancel_campaign(self):
        """Cancel the current campaign"""
        self._log("Campaign cancellation requested", QColor("red"))
        self._reset_campaign_ui()

    def _reset_campaign_ui(self):
        """Reset the campaign UI to initial state"""
        self.start_campaign_btn.setEnabled(True)
        self.pause_campaign_btn.setEnabled(False)
        self.resume_campaign_btn.setEnabled(False)
        self.release_lightning_btn.setEnabled(False)
        self.cancel_campaign_btn.setEnabled(False)
        self.campaign_progress_label.setText("Ready to start campaign")

    def _on_campaign_paused(self):
        """Handle campaign paused signal"""
        self.pause_campaign_btn.setEnabled(False)
        self.resume_campaign_btn.setEnabled(True)
        self.campaign_progress_label.setText("Campaign Paused")

    def _on_campaign_resumed(self):
        """Handle campaign resumed signal"""
        self.pause_campaign_btn.setEnabled(True)
        self.resume_campaign_btn.setEnabled(False)
        self.campaign_progress_label.setText("Campaign Resumed")

    def _on_lightning_ready(self, email_count):
        """Handle lightning mode ready signal"""
        self.start_campaign_btn.setEnabled(False)
        self.pause_campaign_btn.setEnabled(False)
        self.resume_campaign_btn.setEnabled(False)
        self.release_lightning_btn.setEnabled(True)
        self.cancel_campaign_btn.setEnabled(True)
        self.campaign_progress_label.setText(f"⚡ Lightning Ready: {email_count} emails prepared!")
        self._set_ui_for_task(False)

    def _get_recipients_from_table(self):
        """Get recipients from the recipients table"""
        recipients = []
        for row in range(self.recipients_table.rowCount()):
            email_item = self.recipients_table.item(row, 0)
            name_item = self.recipients_table.item(row, 1)
            
            if email_item and email_item.text().strip():
                recipient = {
                    'email': email_item.text().strip(),
                    'name': name_item.text().strip() if name_item else ''
                }
                recipients.append(recipient)
        
        return recipients

    def _import_recipients_csv(self):
        """Import recipients from a CSV file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Import Recipients CSV", "", "CSV Files (*.csv);;All Files (*)"
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                imported_count = 0
                
                for row in reader:
                    email = row.get('email', '').strip()
                    name = row.get('name', '').strip()
                    
                    if email and '@' in email:
                        # Add to table
                        row_count = self.recipients_table.rowCount()
                        self.recipients_table.insertRow(row_count)
                        self.recipients_table.setItem(row_count, 0, QTableWidgetItem(email))
                        self.recipients_table.setItem(row_count, 1, QTableWidgetItem(name))
                        self.recipients_table.setItem(row_count, 2, QTableWidgetItem("Queued"))
                        self.recipients_table.setItem(row_count, 3, QTableWidgetItem(""))
                        imported_count += 1
                
                self._log(f"Imported {imported_count} recipients from CSV", QColor("lightgreen"))
                
        except Exception as e:
            self._log_error(f"Failed to import CSV: {e}")

    def _add_manual_recipients(self):
        """Add recipients manually by typing"""
        manual_text = self.manual_recipients_input.toPlainText().strip()
        if not manual_text:
            self._log_error("Please enter recipients to add")
            return
        
        # Parse recipients (support both line breaks and commas)
        recipients = []
        for line in manual_text.split('\n'):
            line = line.strip()
            if line:
                # Split by comma if present, otherwise treat as single email
                if ',' in line:
                    recipients.extend([email.strip() for email in line.split(',') if email.strip()])
                else:
                    recipients.append(line)
        
        # Add valid email addresses to table
        added_count = 0
        for recipient in recipients:
            if '@' in recipient and '.' in recipient.split('@')[1]:
                # Extract name from email if possible
                name = recipient.split('@')[0].replace('.', ' ').title()
                
                # Add to table
                row_count = self.recipients_table.rowCount()
                self.recipients_table.insertRow(row_count)
                self.recipients_table.setItem(row_count, 0, QTableWidgetItem(recipient))
                self.recipients_table.setItem(row_count, 1, QTableWidgetItem(name))
                self.recipients_table.setItem(row_count, 2, QTableWidgetItem("Queued"))
                self.recipients_table.setItem(row_count, 3, QTableWidgetItem(""))
                added_count += 1
            else:
                self._log(f"Invalid email format: {recipient}", QColor("orange"))
        
        if added_count > 0:
            self._log(f"Added {added_count} recipients manually", QColor("lightgreen"))
            self.manual_recipients_input.clear()  # Clear the input after adding
        else:
            self._log_error("No valid email addresses found")

    def _clear_recipients(self):
        """Clear all recipients from the table"""
        self.recipients_table.setRowCount(0)
        self._log("Cleared all recipients", QColor("cyan"))

    def _add_custom_header(self):
        """Add a new custom header row"""
        row_count = self.headers_table.rowCount()
        self.headers_table.insertRow(row_count)
        self.headers_table.setItem(row_count, 0, QTableWidgetItem(""))
        self.headers_table.setItem(row_count, 1, QTableWidgetItem(""))

    def _remove_custom_header(self):
        """Remove selected custom header row"""
        current_row = self.headers_table.currentRow()
        if current_row >= 0:
            self.headers_table.removeRow(current_row)

    def _add_attachment(self):
        """Add an attachment file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Attachment", "", "All Files (*)"
        )
        
        if file_path and os.path.exists(file_path):
            self.attachments_list.addItem(file_path)
            self._log(f"Added attachment: {os.path.basename(file_path)}", QColor("cyan"))

    def _remove_attachment(self):
        """Remove selected attachment"""
        current_row = self.attachments_list.currentRow()
        if current_row >= 0:
            self.attachments_list.takeItem(current_row)

    def _export_campaign_logs(self):
        """Export campaign logs to file"""
        # This would export the campaign results
        self._log("Export campaign logs functionality not yet implemented", QColor("yellow"))

    def _export_recipients(self):
        """Export recipients table to CSV"""
        if self.recipients_table.rowCount() == 0:
            self._log_error("No recipients to export")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Recipients", "recipients.csv", "CSV Files (*.csv)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Email', 'Name', 'Status', 'Error'])
                    
                    for row in range(self.recipients_table.rowCount()):
                        row_data = []
                        for col in range(4):
                            item = self.recipients_table.item(row, col)
                            row_data.append(item.text() if item else '')
                        writer.writerow(row_data)
                
                self._log(f"Exported recipients to {file_path}", QColor("lightgreen"))
                
            except Exception as e:
                self._log_error(f"Failed to export recipients: {e}")

    def _preview_email(self):
        """Preview the email content"""
        # This would show a preview dialog
        self._log("Email preview functionality not yet implemented", QColor("yellow"))

    def _on_email_sent(self, email, status):
        """Handle successful email sending"""
        self._update_recipient_status(email, "Sent", "")

    def _on_email_failed(self, email, error):
        """Handle failed email sending"""
        self._update_recipient_status(email, "Failed", error)

    def _update_recipient_status(self, email, status, error):
        """Update recipient status in the table"""
        for row in range(self.recipients_table.rowCount()):
            email_item = self.recipients_table.item(row, 0)
            if email_item and email_item.text() == email:
                self.recipients_table.setItem(row, 2, QTableWidgetItem(status))
                self.recipients_table.setItem(row, 3, QTableWidgetItem(error))
                
                # Color code the status
                status_item = self.recipients_table.item(row, 2)
                if status == "Sent":
                    status_item.setForeground(QColor("lightgreen"))
                elif status == "Failed":
                    status_item.setForeground(QColor("red"))
                else:
                    status_item.setForeground(QColor("yellow"))
                break

    def _on_campaign_progress(self, current, total):
        """Handle campaign progress updates"""
        self.campaign_progress_bar.setValue(current)
        self.campaign_progress_label.setText(f"Campaign Progress: {current}/{total}")
        
        # Update stats
        sent_count = 0
        failed_count = 0
        
        for row in range(self.recipients_table.rowCount()):
            status_item = self.recipients_table.item(row, 2)
            if status_item:
                if status_item.text() == "Sent":
                    sent_count += 1
                elif status_item.text() == "Failed":
                    failed_count += 1
        
        self.sent_count_label.setText(f"Sent: {sent_count}")
        self.failed_count_label.setText(f"Failed: {failed_count}")
        
        if current > 0:
            success_rate = (sent_count / current) * 100
            self.success_rate_label.setText(f"Success Rate: {success_rate:.1f}%")

    def _on_campaign_finished(self, summary):
        """Handle campaign completion"""
        self._log(f"Campaign completed! Sent: {summary['sent']}, Failed: {summary['failed']}", QColor("cyan"))
        self._reset_campaign_ui()
        self._set_ui_for_task(False)

    def closeEvent(self, event):
        self.thread.quit()
        if not self.thread.wait(5000): self.thread.terminate()
        event.accept()

# --- Application Entry Point ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet("""
        QWidget { background-color: #3c3f41; color: #f0f0f0; font-size: 10pt; }
        QWidget:disabled { color: #888; }
        QMainWindow, QStatusBar { background-color: #2b2b2b; }
        QTabWidget::pane { border: 1px solid #555; border-top: 1px solid #666;}
        QTabBar::tab { background: #45494a; border: 1px solid #555; border-bottom: none; padding: 8px 12px; min-width: 100px; }
        QTabBar::tab:selected { background: #5a5e60; margin-bottom: 0px; }
        QTabBar::tab:!selected:hover { background: #505456; }
        QPushButton { background-color: #5a5e60; border: 1px solid #666; padding: 6px; border-radius: 2px; text-align: left; padding-left: 10px;}
        QPushButton:hover { background-color: #6a6e70; }
        QPushButton:pressed { background-color: #4d5052; }
        QLineEdit, QComboBox, QSpinBox { background-color: #45494a; border: 1px solid #666; padding: 5px; }
        QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background-color: #333; }
        QGroupBox { font-weight: bold; border: 1px solid #555; margin-top: 10px; padding-top: 15px; border-radius: 4px;}
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; left: 10px;}
        QTextEdit { background-color: #2b2b2b; color: #a9b7c6; border: 1px solid #555; }
        QProgressBar { text-align: center; color: white; border-radius: 4px; border: 1px solid #555;} 
        QProgressBar::chunk { background-color: #007acc; border-radius: 3px;}
        QTableWidget { gridline-color: #555; }
        QHeaderView::section { background-color: #45494a; padding: 4px; border: 1px solid #555; }
        QMenu { background-color: #3c3f41; border: 1px solid #555; }
        QMenu::item:selected { background-color: #007acc; }
    """)
    window = GUserAdminApp()
    window.show()
    sys.exit(app.exec_())