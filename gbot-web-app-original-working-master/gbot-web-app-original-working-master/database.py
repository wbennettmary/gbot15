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

