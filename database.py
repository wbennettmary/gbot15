from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(80), nullable=False, default='support')
    last_login = db.Column(db.DateTime, nullable=True)  # Track last login time
    active_aws_config_id = db.Column(db.Integer, db.ForeignKey('aws_config.id'), nullable=True) # Selected AWS account
    
    # Relationship to easily access the config
    active_aws_config = db.relationship('AwsConfig', foreign_keys=[active_aws_config_id])

class WhitelistedIP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class UsedDomain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain_name = db.Column(db.String(255), unique=True, nullable=False)
    user_count = db.Column(db.Integer, default=0)
    is_verified = db.Column(db.Boolean, default=False)
    ever_used = db.Column(db.Boolean, default=False)  # Track if domain was ever used
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class GoogleAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_name = db.Column(db.String(255), unique=True, nullable=False)
    client_id = db.Column(db.String(255), nullable=False)
    client_secret = db.Column(db.String(255), nullable=False)
    tokens = db.relationship('GoogleToken', backref='account', lazy=True, cascade="all, delete-orphan")

google_token_scopes = db.Table('google_token_scopes',
    db.Column('google_token_id', db.Integer, db.ForeignKey('google_token.id'), primary_key=True),
    db.Column('scope_id', db.Integer, db.ForeignKey('scope.id'), primary_key=True)
)

class Scope(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)

class GoogleToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('google_account.id'), nullable=False)
    token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text)
    token_uri = db.Column(db.Text, nullable=False)
    scopes = db.relationship('Scope', secondary=google_token_scopes, lazy='subquery',
                             backref=db.backref('google_tokens', lazy=True))

class ServerConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    host = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, default=22)
    username = db.Column(db.String(255), nullable=False)
    auth_method = db.Column(db.String(50), default='password')  # 'password' or 'key'
    password = db.Column(db.Text)  # Encrypted password
    private_key = db.Column(db.Text)  # Encrypted private key
    json_path = db.Column(db.String(500), nullable=False)
    file_pattern = db.Column(db.String(100), default='*.json')
    is_configured = db.Column(db.Boolean, default=False)
    last_tested = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class UserAppPassword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), nullable=False)  # username part (before @)
    domain = db.Column(db.String(255), nullable=False)   # domain part (after @) or '*' wildcard
    app_password = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    
    # Composite unique constraint on username + domain
    __table_args__ = (db.UniqueConstraint('username', 'domain', name='unique_user_domain'),)

class AwsGeneratedPassword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    app_password = db.Column(db.String(255), nullable=False)
    secret_key = db.Column(db.String(100), nullable=True)
    execution_id = db.Column(db.String(100), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class AutomationAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_name = db.Column(db.String(255), unique=True, nullable=False)
    client_id = db.Column(db.String(255), nullable=False)
    client_secret = db.Column(db.String(255), nullable=False)
    accounts_list = db.Column(db.Text, nullable=False)  # Column-based storage, one account per line
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    last_retrieval = db.Column(db.DateTime)
    retrieval_count = db.Column(db.Integer, default=0)
    
    # Relationship to store retrieved users
    retrieved_users = db.relationship('RetrievedUser', backref='automation_account', lazy=True, cascade="all, delete-orphan")

class RetrievedUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    automation_account_id = db.Column(db.Integer, db.ForeignKey('automation_account.id'), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255))
    domain = db.Column(db.String(255))
    status = db.Column(db.String(50), default='active')  # active, suspended, etc.
    retrieved_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    
    # Composite unique constraint on automation_account_id + email
    __table_args__ = (db.UniqueConstraint('automation_account_id', 'email', name='unique_automation_user'),)

class NamecheapConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_user = db.Column(db.String(255), nullable=False)
    api_key = db.Column(db.String(255), nullable=False)
    username = db.Column(db.String(255), nullable=False)
    client_ip = db.Column(db.String(45), nullable=False)
    is_configured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class CloudflareConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_token = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    is_configured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class AwsConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), default='Default Account') # Friendly name
    access_key_id = db.Column(db.String(255), nullable=False)
    secret_access_key = db.Column(db.Text, nullable=False)  # Encrypted
    region = db.Column(db.String(50), nullable=False, default='us-east-1')
    ecr_uri = db.Column(db.String(500))
    s3_bucket = db.Column(db.String(255), default='edu-gw-app-passwords')
    is_configured = db.Column(db.Boolean, default=False)
    # Multi-tenant naming configuration
    instance_name = db.Column(db.String(100), default='default')  # Unique identifier for this instance (e.g., "client-acme", "user1")
    ecr_repo_name = db.Column(db.String(255), default='gbot-app-password-worker')  # Custom ECR repository name
    lambda_prefix = db.Column(db.String(100), default='gbot-chromium')  # Lambda function prefix
    dynamodb_table = db.Column(db.String(255), default='gbot-app-passwords')  # DynamoDB table name
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class ProxyConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    proxies = db.Column(db.Text)  # One proxy per line: IP:PORT:USERNAME:PASSWORD
    enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class TwoCaptchaConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_key = db.Column(db.Text)  # 2captcha API key
    enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class DomainGenConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tlds = db.Column(db.Text, nullable=False)  # Comma-separated list: .asia,.fun,...
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class DomainOperation(db.Model):
    id = db.Column(db.String(36), primary_key=True)  # UUID as string
    job_id = db.Column(db.String(36), nullable=False, index=True)
    input_domain = db.Column(db.String(255), nullable=False)
    apex_domain = db.Column(db.String(255), nullable=False)
    txt_record_value = db.Column(db.String(255))  # Store the Google verification token
    workspace_status = db.Column(db.String(50), default='pending')  # pending, success, failed, skipped
    dns_status = db.Column(db.String(50), default='pending')  # pending, success, failed, dry-run
    verify_status = db.Column(db.String(50), default='pending')  # pending, success, failed, skipped
    message = db.Column(db.Text)
    raw_log = db.Column(db.JSON)  # JSONB in PostgreSQL, JSON in SQLite
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), index=True)
    
    __table_args__ = (db.Index('idx_domain_operation_job_id', 'job_id'),)

class ServiceAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)  # Display name
    admin_email = db.Column(db.String(255), nullable=False)  # Admin email to impersonate
    project_id = db.Column(db.String(255), nullable=False)
    client_email = db.Column(db.String(255), nullable=False)
    private_key_id = db.Column(db.String(255))
    json_content = db.Column(db.Text, nullable=False)  # Full JSON content
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class Notification(db.Model):
    """Store notifications for login events and system alerts"""
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False, default='system')  # login, system, alert
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    icon = db.Column(db.String(50), default='fa-bell')  # FontAwesome icon class
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=True)  # Who triggered it - CASCADE deletes notifications when user is deleted
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class DomainVerificationOperation(db.Model):
    """Track domain verification-only operations (for verify-unverified endpoint)"""
    id = db.Column(db.String(36), primary_key=True)  # UUID as string
    job_id = db.Column(db.String(36), nullable=False, index=True)
    domain = db.Column(db.String(255), nullable=False)
    apex_domain = db.Column(db.String(255), nullable=False)
    account_name = db.Column(db.String(255), nullable=False)
    workspace_status = db.Column(db.String(50), default='skipped')
    dns_status = db.Column(db.String(50), default='skipped')
    verify_status = db.Column(db.String(50), default='pending')
    message = db.Column(db.Text)
    raw_log = db.Column(db.JSON)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp(), index=True)
    
    __table_args__ = (db.Index('idx_domain_verification_op_job_id', 'job_id'),)

class WorkspaceList(db.Model):
    """Workspace account lists with 14-day lifecycle and 24-hour usage tracking"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    raw_accounts = db.Column(db.Text, nullable=False)  # email:password per line
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    lifetime_expires_at = db.Column(db.DateTime, nullable=False)  # 14-day expiration
    active_24h_expires_at = db.Column(db.DateTime, nullable=True)  # 24h timer (null = not started)
    status = db.Column(db.String(50), default='ready')  # ready, in_use, expired
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    
    def get_account_count(self):
        """Return the number of accounts in this list"""
        if not self.raw_accounts:
            return 0
        # Support both comma and colon as separators (email,password or email:password)
        return len([line for line in self.raw_accounts.strip().split('\n') if line.strip() and (',' in line or ':' in line)])
    
    def compute_status(self):
        """Compute current status based on timestamps"""
        from datetime import datetime
        now = datetime.utcnow()
        
        # Check if 14-day lifetime expired
        if self.lifetime_expires_at and now >= self.lifetime_expires_at:
            return 'expired'
        
        # Check if 24h timer is running
        if self.active_24h_expires_at:
            if now < self.active_24h_expires_at:
                return 'in_use'
            # 24h timer finished, list is ready again
        
        return 'ready'
    
    def to_dict(self):
        """Convert to dictionary for JSON response"""
        from datetime import datetime
        # Helper to format datetime as ISO string with UTC indicator
        def format_utc(dt):
            return dt.isoformat() + 'Z' if dt else None
        
        return {
            'id': self.id,
            'name': self.name,
            'raw_accounts': self.raw_accounts,
            'account_count': self.get_account_count(),
            'created_at': format_utc(self.created_at),
            'lifetime_expires_at': format_utc(self.lifetime_expires_at),
            'active_24h_expires_at': format_utc(self.active_24h_expires_at),
            'status': self.compute_status(),
            'updated_at': format_utc(self.updated_at)
        }


# DigitalOcean Management Models
class DigitalOceanConfig(db.Model):
    """DigitalOcean API configuration and settings"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), default='Default DigitalOcean Account')
    api_token = db.Column(db.Text, nullable=False)  # Encrypted
    default_region = db.Column(db.String(50), default='nyc3')
    default_size = db.Column(db.String(50), default='s-1vcpu-1gb')
    automation_snapshot_id = db.Column(db.String(255))
    ssh_key_id = db.Column(db.String(255))
    ssh_private_key_path = db.Column(db.String(500))
    auto_destroy_droplets = db.Column(db.Boolean, default=True)
    parallel_users = db.Column(db.Integer, default=5)
    users_per_droplet = db.Column(db.Integer, default=50)
    is_configured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())


class DigitalOceanDroplet(db.Model):
    """Track DigitalOcean droplets created for automation"""
    id = db.Column(db.Integer, primary_key=True)
    droplet_id = db.Column(db.String(255), unique=True, nullable=False)
    droplet_name = db.Column(db.String(255), nullable=False)
    ip_address = db.Column(db.String(45))
    region = db.Column(db.String(50))
    size = db.Column(db.String(50))
    status = db.Column(db.String(50), default='pending')
    assigned_users_count = db.Column(db.Integer, default=0)
    execution_task_id = db.Column(db.String(36), db.ForeignKey('digital_ocean_execution.task_id'))
    created_by_username = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    destroyed_at = db.Column(db.DateTime)
    auto_destroy = db.Column(db.Boolean, default=True)


class DigitalOceanExecution(db.Model):
    """Track bulk automation execution tasks"""
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(36), unique=True, nullable=False)
    username = db.Column(db.String(255))
    total_users = db.Column(db.Integer, default=0)
    droplets_created = db.Column(db.Integer, default=0)
    users_per_droplet = db.Column(db.Integer, default=0)
    snapshot_id = db.Column(db.String(255))
    region = db.Column(db.String(50))
    size = db.Column(db.String(50))
    status = db.Column(db.String(50), default='pending')
    results_json = db.Column(db.Text)
    success_count = db.Column(db.Integer, default=0)
    failure_count = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    completed_at = db.Column(db.DateTime)
    error_message = db.Column(db.Text)
    droplets_destroyed = db.Column(db.Boolean, default=False)

# Afraid (FreeDNS) Models
class AfraidConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(255), nullable=True)
    password = db.Column(db.String(255), nullable=True)
    cookies_str = db.Column(db.Text, nullable=True)  # Raw browser cookie string
    is_configured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class AfraidDomain(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    domain_name = db.Column(db.String(255), unique=True, nullable=False)
    domain_id = db.Column(db.String(50), nullable=True)
    tld = db.Column(db.String(50), nullable=True, index=True)
    source = db.Column(db.String(50), nullable=True, default='manual')
    rotation_count = db.Column(db.Integer, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    registry_status = db.Column(db.String(50), nullable=True)
    registry_owner = db.Column(db.String(255), nullable=True)
    hosts_in_use = db.Column(db.Integer, nullable=True)
    registry_age_text = db.Column(db.String(255), nullable=True)
    registry_created_on = db.Column(db.String(50), nullable=True)
    delivery_status = db.Column(db.String(20), nullable=True, default='inbox')
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class AfraidResultList(db.Model):
    """Lists created by the Afraid subdomain process."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    created_by = db.Column(db.String(255), nullable=True)
    raw_results = db.Column(db.Text, nullable=False)
    results_json = db.Column(db.Text, nullable=True)
    created_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'created_by': self.created_by,
            'raw_results': self.raw_results,
            'results_json': self.results_json,
            'created_count': self.created_count or 0,
            'failed_count': self.failed_count or 0,
            'created_at': self.created_at.isoformat() + 'Z' if self.created_at else None
        }

class AfraidCloudflareDomainUsage(db.Model):
    """Track Cloudflare destination usage for Afraid CNAME batches."""
    id = db.Column(db.Integer, primary_key=True)
    domain_name = db.Column(db.String(255), unique=True, nullable=False)
    use_count = db.Column(db.Integer, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class InboxImapAccount(db.Model):
    """Authorized IMAP inboxes used by Inbox Intelligence."""
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(80), nullable=False, default='generic')
    email = db.Column(db.String(255), unique=True, nullable=False)
    imap_host = db.Column(db.String(255), nullable=False)
    imap_port = db.Column(db.Integer, default=993)
    tls_enabled = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(255), nullable=False)
    encrypted_password = db.Column(db.Text, nullable=False)
    connection_status = db.Column(db.String(50), default='configured')
    auto_sync_enabled = db.Column(db.Boolean, default=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    message_count = db.Column(db.Integer, default=0)
    folder_count = db.Column(db.Integer, default=0)
    inbox_count = db.Column(db.Integer, default=0)
    spam_count = db.Column(db.Integer, default=0)
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class InboxEmailMessage(db.Model):
    """Sanitized metadata and content snippets synchronized from authorized inboxes."""
    id = db.Column(db.Integer, primary_key=True)
    imap_account_id = db.Column(db.Integer, db.ForeignKey('inbox_imap_account.id'), nullable=False, index=True)
    provider = db.Column(db.String(80), nullable=True)
    folder = db.Column(db.String(255), nullable=False, default='INBOX')
    uid = db.Column(db.String(255), nullable=True)
    message_id = db.Column(db.String(500), nullable=True, index=True)
    sender = db.Column(db.String(500), nullable=True)
    sender_domain = db.Column(db.String(255), nullable=True, index=True)
    recipient = db.Column(db.String(500), nullable=True)
    subject = db.Column(db.Text, nullable=True)
    preview = db.Column(db.Text, nullable=True)
    plain_text = db.Column(db.Text, nullable=True)
    html_content = db.Column(db.Text, nullable=True)
    headers_json = db.Column(db.Text, nullable=True)
    x_test_id = db.Column(db.String(255), nullable=True, index=True)
    uid_validity = db.Column(db.String(64), nullable=True, index=True)
    is_read = db.Column(db.Boolean, default=False, index=True)
    has_attachments = db.Column(db.Boolean, default=False)
    received_at = db.Column(db.DateTime, nullable=True, index=True)
    synced_at = db.Column(db.DateTime, default=db.func.current_timestamp())

    __table_args__ = (db.UniqueConstraint('imap_account_id', 'folder', 'uid', name='unique_inbox_message_uid'),)

class InboxStaticTemplate(db.Model):
    """Static HTML templates owned by the SaaS administrator."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    html_content = db.Column(db.Text, nullable=False)
    plain_text = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='active')
    is_default = db.Column(db.Boolean, default=False)
    editable_regions_json = db.Column(db.Text, nullable=True)
    locked_regions_json = db.Column(db.Text, nullable=True)
    version = db.Column(db.Integer, default=1)
    last_tested_at = db.Column(db.DateTime, nullable=True)
    inbox_rate = db.Column(db.Float, nullable=True)
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class InboxUserTemplate(db.Model):
    """Reusable templates owned by the User Inbox Test workflow."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.Text, nullable=True)
    html_content = db.Column(db.Text, nullable=True)
    plain_text = db.Column(db.Text, nullable=True)
    custom_headers = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='active')
    is_liked = db.Column(db.Boolean, default=False)
    use_count = db.Column(db.Integer, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class InboxOpenRouterConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    encrypted_api_key = db.Column(db.Text, nullable=True)
    default_model = db.Column(db.String(255), nullable=True)
    custom_model = db.Column(db.String(255), nullable=True)
    fallback_model = db.Column(db.String(255), nullable=True)
    temperature = db.Column(db.Float, default=0.7)
    max_tokens = db.Column(db.Integer, default=1800)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class InboxDeliverabilityTest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.String(80), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('inbox_static_template.id'), nullable=True)
    subject = db.Column(db.Text, nullable=False)
    html_body = db.Column(db.Text, nullable=True)
    text_body = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='queued')
    total_messages = db.Column(db.Integer, default=0)
    sent_count = db.Column(db.Integer, default=0)
    inbox_count = db.Column(db.Integer, default=0)
    spam_count = db.Column(db.Integer, default=0)
    pending_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)
    test_type = db.Column(db.String(50), default='user_inbox', index=True)
    strategy = db.Column(db.String(50), nullable=True)
    total_email_sources = db.Column(db.Integer, default=0)
    total_users = db.Column(db.Integer, default=0)
    total_recipients = db.Column(db.Integer, default=0)
    inbox_threshold = db.Column(db.Integer, default=80)
    spam_threshold = db.Column(db.Integer, default=40)
    minimum_observations = db.Column(db.Integer, default=10)
    created_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    completed_at = db.Column(db.DateTime, nullable=True)

class TestEmailSource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_imap_account_id = db.Column(db.Integer, db.ForeignKey('inbox_imap_account.id'), nullable=True, index=True)
    source_message_id = db.Column(db.Integer, db.ForeignKey('inbox_email_message.id'), nullable=True, index=True)
    source_sender = db.Column(db.String(500), nullable=True)
    source_sender_domain = db.Column(db.String(255), nullable=True, index=True)
    original_subject = db.Column(db.Text, nullable=True)
    html_snapshot = db.Column(db.Text, nullable=True)
    text_snapshot = db.Column(db.Text, nullable=True)
    preview_snapshot = db.Column(db.Text, nullable=True)
    source_folder = db.Column(db.String(255), nullable=True)
    source_provider = db.Column(db.String(80), nullable=True)
    original_received_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), default='AVAILABLE', index=True)
    last_test_id = db.Column(db.String(80), nullable=True, index=True)
    last_verdict = db.Column(db.String(50), nullable=True, index=True)
    last_inbox_percentage = db.Column(db.Float, nullable=True)
    last_spam_percentage = db.Column(db.Float, nullable=True)
    pushed_by = db.Column(db.String(255), nullable=True)
    pushed_at = db.Column(db.DateTime, default=db.func.current_timestamp(), index=True)

class InboxDeliverabilityMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.String(80), db.ForeignKey('inbox_deliverability_test.test_id'), nullable=False, index=True)
    source_email_id = db.Column(db.Integer, db.ForeignKey('test_email_source.id'), nullable=True, index=True)
    workspace_sender = db.Column(db.String(255), nullable=False)
    imap_account_id = db.Column(db.Integer, db.ForeignKey('inbox_imap_account.id'), nullable=True)
    recipient = db.Column(db.String(255), nullable=False)
    test_identifier = db.Column(db.String(120), unique=True, nullable=False, index=True)
    provider_message_id = db.Column(db.String(500), nullable=True)
    subject = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    detected_at = db.Column(db.DateTime, nullable=True)
    placement = db.Column(db.String(50), default='PENDING')
    folder = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default='QUEUED')
    error_message = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())

class InboxPollJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    test_id = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(30), default='running', nullable=False)
    message = db.Column(db.Text, nullable=True)
    result_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    finished_at = db.Column(db.DateTime, nullable=True)

class ImapFolderSyncState(db.Model):
    """Tracks IMAP UID cursors per folder for incremental sync."""
    __tablename__ = 'imap_folder_sync_state'
    id = db.Column(db.Integer, primary_key=True)
    imap_account_id = db.Column(db.Integer, db.ForeignKey('inbox_imap_account.id'), nullable=False, index=True)
    folder_name = db.Column(db.String(255), nullable=False)
    uid_validity = db.Column(db.String(64), nullable=True)
    last_seen_uid = db.Column(db.Integer, default=0)
    last_sync_started_at = db.Column(db.DateTime, nullable=True)
    last_sync_completed_at = db.Column(db.DateTime, nullable=True)
    sync_status = db.Column(db.String(30), default='idle')
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, default=db.func.current_timestamp(), onupdate=db.func.current_timestamp())
    __table_args__ = (db.UniqueConstraint('imap_account_id', 'folder_name', name='uq_imap_folder_sync'),)

class InboxAiJob(db.Model):
    """AI Placement Optimization job: one auditable provider call."""
    __tablename__ = 'inbox_ai_job'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    job_type = db.Column(db.String(50), nullable=False, index=True)
    status = db.Column(db.String(30), default='running', nullable=False, index=True)
    test_id = db.Column(db.String(80), nullable=True, index=True)
    source_template_id = db.Column(db.Integer, nullable=True)
    created_by = db.Column(db.String(255), nullable=True)
    provider = db.Column(db.String(50), nullable=True)
    model = db.Column(db.String(255), nullable=True)
    input_summary_json = db.Column(db.Text, nullable=True)
    output_json = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class InboxAiSuggestion(db.Model):
    """Persisted AI suggestion so results stay visible across refreshes."""
    __tablename__ = 'inbox_ai_suggestion'
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(64), db.ForeignKey('inbox_ai_job.job_id'), nullable=False, index=True)
    test_id = db.Column(db.String(80), nullable=True, index=True)
    suggestion_type = db.Column(db.String(50), default='region_text', nullable=False)
    target_region = db.Column(db.String(50), nullable=True)
    content_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending', nullable=False, index=True)
    confidence = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp(), index=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by = db.Column(db.String(255), nullable=True)

class InboxAiAuditEvent(db.Model):
    """Audit trail for AI generation and later suggestion decisions."""
    __tablename__ = 'inbox_ai_audit_event'
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(50), nullable=False, index=True)
    job_id = db.Column(db.String(64), nullable=True, index=True)
    test_id = db.Column(db.String(80), nullable=True, index=True)
    suggestion_id = db.Column(db.Integer, nullable=True)
    user_email = db.Column(db.String(255), nullable=True)
    payload_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())

class InboxAiPromptVersion(db.Model):
    """Version history for saved AI prompts, tied to user and time."""
    __tablename__ = 'inbox_ai_prompt_version'
    id = db.Column(db.Integer, primary_key=True)
    prompt_key = db.Column(db.String(80), nullable=False, index=True)
    prompt_text = db.Column(db.Text, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_by = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
